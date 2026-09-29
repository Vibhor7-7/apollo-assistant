"""Local WebSocket server the notch app connects to (ws://127.0.0.1:8765).

Text frames are JSON control messages; binary frames are one channel byte followed by
PCM16 mono 24 kHz audio. Protocol: see PRD.md §8.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request

from .agent import ClaudeAgent
from .approvals import ApprovalBroker
from .composio_tools import ComposioTools
from .config import Settings, new_session_token
from .dictation import Dictation
from .mac_tools import MacTools
from .voice import VoiceSession

log = logging.getLogger("apollo.server")

CH_ASSISTANT_MIC = 0x01
CH_DICTATION_MIC = 0x02
CH_ASSISTANT_SPEAKER = 0x03


class Hub:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.token = new_session_token()
        self.clients: set[ServerConnection] = set()
        self.status = "idle"

        self.mac = MacTools(emit=self.emit)
        self.approvals = ApprovalBroker(emit=self.emit, has_ui=lambda: bool(self.clients), on_request=self._approval_requested)
        self.composio = self._start_composio()
        self.agent = ClaudeAgent(settings, self.mac, self.composio, self.approvals)
        self.voice = VoiceSession(settings, self, self.agent, self.mac)
        self.dictation = Dictation(settings)
        self._dictation_owner: ServerConnection | None = None
        self._background: set[asyncio.Task] = set()

    def _start_composio(self) -> ComposioTools | None:
        try:
            tools = ComposioTools(self.settings)
            tools.start()
            log.info("Composio ready: %d tools (%s)", len(tools.tools), ", ".join(sorted(tools.names)))
            return tools
        except Exception as exc:
            log.warning("Composio unavailable, continuing with Mac tools only: %s", exc)
            return None

    # ── outbound ──

    async def emit(self, message: dict) -> None:
        data = json.dumps(message)
        for client in list(self.clients):
            try:
                await client.send(data)
            except ConnectionClosed:
                self.clients.discard(client)

    async def send_audio(self, pcm: bytes) -> None:
        frame = bytes([CH_ASSISTANT_SPEAKER]) + pcm
        for client in list(self.clients):
            try:
                await client.send(frame)
            except ConnectionClosed:
                self.clients.discard(client)

    async def set_status(self, state: str) -> None:
        if state != self.status:
            self.status = state
            await self.emit({"type": "status", "state": state})

    async def _approval_requested(self, title: str) -> None:
        await self.voice.notify(f"Approval needed on the notch before continuing: {title}.")

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ── inbound ──

    async def handle(self, ws: ServerConnection) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        except Exception:
            await ws.close(1008, "expected hello")
            return
        if hello.get("type") != "hello" or not hmac.compare_digest(str(hello.get("token", "")), self.token):
            await ws.close(1008, "bad token")
            return

        self.clients.add(ws)
        log.info("Notch connected")
        await ws.send(json.dumps({
            "type": "ready",
            "status": self.status,
            "composio": self.composio is not None,
            "user": self.settings.user_name,
        }))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    await self._on_audio(ws, message)
                else:
                    await self._on_message(ws, json.loads(message))
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            log.info("Notch disconnected")
            if self.voice.capturing:
                await self.voice.cancel_listening()
            if self.dictation.active and self._dictation_owner is ws:
                await self.dictation.cancel()

    async def _on_audio(self, ws: ServerConnection, frame: bytes) -> None:
        if not frame:
            return
        channel, pcm = frame[0], frame[1:]
        if channel == CH_ASSISTANT_MIC:
            await self.voice.append_audio(pcm)
        elif channel == CH_DICTATION_MIC and ws is self._dictation_owner:
            await self.dictation.append(pcm)

    @staticmethod
    def _reply_to(ws: ServerConnection):
        async def reply(message: dict) -> None:
            try:
                await ws.send(json.dumps(message))
            except ConnectionClosed:
                pass
        return reply

    async def _on_message(self, ws: ServerConnection, msg: dict) -> None:
        kind = msg.get("type")
        try:
            match kind:
                case "assistant.start":
                    await self.voice.start_listening()
                case "assistant.stop":
                    await self.voice.stop_listening()
                case "assistant.cancel":
                    await self.voice.cancel_listening()
                    await self.voice.interrupt()
                case "assistant.text":
                    if text := str(msg.get("text", "")).strip():
                        await self.voice.send_text(text)
                case "assistant.reset":
                    await self.voice.reset()
                case "approval.response":
                    self.approvals.resolve(str(msg.get("id")), bool(msg.get("approved")))
                case "dictation.start":
                    # Only the notch that started a dictation feeds it audio and gets the text back,
                    # so a second connected client can never paste a duplicate.
                    self._dictation_owner = ws
                    await self.dictation.start(reply=self._reply_to(ws))
                case "dictation.stop" if ws is self._dictation_owner:
                    self.spawn(self.dictation.stop())  # transcribe + polish without blocking audio
                case "dictation.cancel" if ws is self._dictation_owner:
                    await self.dictation.cancel()
                case _:
                    log.debug("Ignoring message %s", kind)
        except Exception as exc:
            log.exception("Failed handling %s", kind)
            await self.emit({"type": "error", "message": f"{kind}: {exc}"})


def _reject_browsers(connection: ServerConnection, request: Request):
    # Browsers always send Origin; the notch app doesn't. This stops web pages from driving Apollo.
    if request.headers.get("Origin"):
        return connection.respond(403, "Forbidden\n")
    return None


async def serve_forever(settings: Settings) -> None:
    hub = Hub(settings)
    watchdog = asyncio.create_task(hub.voice.idle_watchdog())
    async with serve(hub.handle, settings.host, settings.port, process_request=_reject_browsers, max_size=2**22):
        log.info("Apollo listening on ws://%s:%d", settings.host, settings.port)
        try:
            await asyncio.Future()
        finally:
            watchdog.cancel()
            await hub.voice.close()

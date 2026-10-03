"""Realtime voice front-end (OpenAI gpt-realtime), push-to-talk.

The voice model handles conversation and quick actions itself, and hands everything else
to the Claude agent through one tool, `delegate_to_agent`. Delegated tasks run in the
background; when one finishes, its result is injected as a system message and spoken.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from .agent import ClaudeAgent
from .config import Settings
from .mac_tools import MacTools, validate

if TYPE_CHECKING:
    from .server import Hub

log = logging.getLogger("apollo.voice")

BYTES_PER_MS = 48  # PCM16 mono @ 24 kHz
MIN_TURN_BYTES = 200 * BYTES_PER_MS  # ignore accidental sub-200 ms presses
IDLE_DISCONNECT_S = 600
BENIGN_ERRORS = {"response_cancel_not_active", "input_audio_buffer_commit_empty"}

INSTRUCTIONS = """You are Apollo, {user}'s personal voice assistant on their Mac. Sound warm, \
quick, and natural, like a sharp human assistant. Keep replies to one or two short sentences \
unless asked for more. Never read out URLs, IDs, or markdown.

Handle these yourself with your quick tools: music (Spotify), volume, opening apps and links, \
the time, date and battery, adding tasks to the notch, focus timers, reminders, and casual \
conversation.

For anything else, call delegate_to_agent: email, calendar, messages, documents, files, \
GitHub, Slack, Notion, the web, research, or any multi-step work. Write the task as a complete, \
self-contained instruction that includes every relevant detail from the conversation (names, \
times, what "it" or "that" refers to). After delegating, give a very short acknowledgement \
like "On it." and stop.

When a system message reports a background task result, relay it naturally and briefly in your \
own words. When a system message says approval is needed, tell {user} to check the notch.

Never invent email, calendar, or file contents, and never say something was done unless a tool \
or task result confirms it."""

DELEGATE_TOOL = {
    "type": "function",
    "name": "delegate_to_agent",
    "description": (
        "Hand a task to Apollo's background agent, which can use email, calendar, Slack, Notion, "
        "GitHub, Google Drive, files, the web, and the Mac. Returns immediately; the result arrives "
        "later as a system message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Complete, self-contained description of what to do."}
        },
        "required": ["task"],
        "additionalProperties": False,
    },
}


class VoiceSession:
    def __init__(self, settings: Settings, hub: Hub, agent: ClaudeAgent, mac: MacTools):
        self.settings = settings
        self.hub = hub
        self.agent = agent
        self.mac = mac
        self.client = AsyncOpenAI()

        self.conn = None
        self._manager = None
        self._reader: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()

        self.capturing = False
        self._turn_bytes = 0
        self.response_active = False
        self._pending_system: list[str] = []
        self._transcripts: dict[str, str] = {}

        # What's currently playing on the notch, to truncate correctly on interruption.
        self._play_item: str | None = None
        self._play_started = 0.0
        self._play_ms = 0.0

        self.tasks: dict[int, asyncio.Task] = {}
        self._task_counter = 0
        self._last_activity = time.monotonic()

    # ── connection lifecycle ──

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self.conn is not None:
                return
            self._manager = self.client.realtime.connect(model=self.settings.realtime_model)
            self.conn = await self._manager.__aenter__()
            await self.conn.session.update(session=self._session_config())
            self._reader = asyncio.create_task(self._read_events())
            log.info("Realtime session connected (%s)", self.settings.realtime_model)

    async def close(self) -> None:
        if self.conn is None:
            return
        conn, manager, self.conn, self._manager = self.conn, self._manager, None, None
        self.response_active = False
        if self._reader:
            self._reader.cancel()
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass
        log.info("Realtime session closed")

    async def idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(30)
            idle = time.monotonic() - self._last_activity > IDLE_DISCONNECT_S
            busy = self.capturing or self.response_active or any(not t.done() for t in self.tasks.values())
            if self.conn is not None and idle and not busy:
                await self.close()

    async def reset(self) -> None:
        await self.close()
        self.agent.reset()
        self._pending_system.clear()
        await self.hub.emit({"type": "transcript.reset"})

    def _session_config(self) -> dict[str, Any]:
        return {
            "type": "realtime",
            "instructions": INSTRUCTIONS.format(user=self.settings.user_name),
            "output_modalities": ["audio"],
            "reasoning": {"effort": self.settings.realtime_effort},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": self.settings.realtime_transcribe_model, "language": "en"},
                    "turn_detection": None,  # push-to-talk: the notch decides when a turn ends
                    "noise_reduction": {"type": "near_field"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.settings.realtime_voice},
            },
            "tools": [DELEGATE_TOOL] + [t.realtime_def() for t in self.mac.quick_tools],
            "tool_choice": "auto",
        }

    # ── input from the notch ──

    async def start_listening(self) -> None:
        self._last_activity = time.monotonic()
        await self.ensure_connected()
        await self.interrupt()
        await self.conn.input_audio_buffer.clear()
        self.capturing = True
        self._turn_bytes = 0
        await self.hub.set_status("listening")

    async def append_audio(self, pcm: bytes) -> None:
        if self.capturing and self.conn is not None:
            self._turn_bytes += len(pcm)
            await self.conn.input_audio_buffer.append(audio=base64.b64encode(pcm).decode())

    async def stop_listening(self) -> None:
        if not self.capturing or self.conn is None:
            return
        self.capturing = False
        self._last_activity = time.monotonic()
        if self._turn_bytes < MIN_TURN_BYTES:
            await self.conn.input_audio_buffer.clear()
            await self.hub.set_status(self._idle_state())
            return
        await self.conn.input_audio_buffer.commit()
        await self._create_response()
        await self.hub.set_status("thinking")

    async def cancel_listening(self) -> None:
        if self.capturing and self.conn is not None:
            self.capturing = False
            await self.conn.input_audio_buffer.clear()
        await self.hub.set_status(self._idle_state())

    async def send_text(self, text: str) -> None:
        self._last_activity = time.monotonic()
        await self.ensure_connected()
        await self.interrupt()
        await self.hub.emit({"type": "transcript", "role": "user", "id": f"typed-{time.time()}", "text": text, "final": True})
        await self.conn.conversation.item.create(
            item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
        )
        await self._create_response()
        await self.hub.set_status("thinking")

    async def interrupt(self) -> None:
        """Stop Apollo mid-sentence and trim its context to what was actually heard."""
        if self.conn is None:
            return
        if self.response_active:
            await self.conn.response.cancel()
            self.response_active = False
        if self._play_item:
            heard_ms = min(self._play_ms, (time.monotonic() - self._play_started) * 1000)
            if heard_ms < self._play_ms - 50:
                await self.conn.conversation.item.truncate(
                    item_id=self._play_item, content_index=0, audio_end_ms=int(heard_ms)
                )
            self._play_item = None
        await self.hub.emit({"type": "audio.interrupt"})

    async def notify(self, text: str) -> None:
        """Queue a system message for the voice model and have it speak about it."""
        if self.conn is None:
            return
        self._pending_system.append(text)
        await self._flush_system()

    # ── realtime events ──

    async def _create_response(self) -> None:
        self.response_active = True  # set before the server confirms, to avoid double-creates
        await self.conn.response.create()

    async def _flush_system(self) -> None:
        if not self._pending_system or self.response_active or self.capturing or self.conn is None:
            return
        while self._pending_system:
            text = self._pending_system.pop(0)
            await self.conn.conversation.item.create(
                item={"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}
            )
        await self._create_response()

    async def _read_events(self) -> None:
        try:
            async for event in self.conn:
                try:
                    await self._handle(event)
                except Exception:
                    log.exception("Error handling realtime event %s", getattr(event, "type", "?"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Realtime connection dropped")
        # Connection ended on its own; next use reconnects.
        self.conn, self._manager = None, None
        self.response_active = False
        await self.hub.set_status(self._idle_state())

    async def _handle(self, event) -> None:
        kind = event.type

        if kind == "response.created":
            self.response_active = True

        elif kind == "response.output_audio.delta":
            pcm = base64.b64decode(event.delta)
            if event.item_id != self._play_item:
                self._play_item, self._play_started, self._play_ms = event.item_id, time.monotonic(), 0.0
            self._play_ms += len(pcm) / BYTES_PER_MS
            await self.hub.send_audio(pcm)

        elif kind == "response.output_audio_transcript.delta":
            text = self._transcripts.get(event.item_id, "") + (event.delta or "")
            self._transcripts[event.item_id] = text
            await self.hub.emit({"type": "transcript", "role": "assistant", "id": event.item_id, "text": text, "final": False})

        elif kind == "response.output_audio_transcript.done":
            self._transcripts.pop(event.item_id, None)
            await self.hub.emit({"type": "transcript", "role": "assistant", "id": event.item_id, "text": event.transcript, "final": True})

        elif kind == "input_audio_buffer.committed":
            # The user's transcription finishes after the reply starts streaming. Reserve their
            # slot now so the bubble sits above the reply; the transcript fills it in by id.
            await self.hub.emit({"type": "transcript", "role": "user", "id": event.item_id, "text": "…", "final": False})

        elif kind == "conversation.item.input_audio_transcription.completed":
            await self.hub.emit({"type": "transcript", "role": "user", "id": event.item_id, "text": event.transcript, "final": True})

        elif kind == "response.done":
            self.response_active = False
            self._last_activity = time.monotonic()
            response = event.response
            calls = [item for item in (response.output or []) if item.type == "function_call"]
            if response.status == "completed" and calls:
                await self._handle_calls(calls)
            else:
                await self._flush_system()
                if not self.response_active:
                    await self.hub.set_status(self._idle_state())

        elif kind == "error":
            code = getattr(event.error, "code", None)
            if code not in BENIGN_ERRORS:
                log.warning("Realtime error: %s", event.error.message)
                await self.hub.emit({"type": "error", "message": event.error.message})

    async def _handle_calls(self, calls: list) -> None:
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            output = await self._run_quick_tool(call.name, args)
            await self.conn.conversation.item.create(
                item={"type": "function_call_output", "call_id": call.call_id, "output": output}
            )
        await self._create_response()

    async def _run_quick_tool(self, name: str, args: Any) -> str:
        if name == "delegate_to_agent":
            if not isinstance(args, dict) or not isinstance(args.get("task"), str):
                return json.dumps({"error": "delegate_to_agent needs a 'task' string"})
            task_id = self.start_task(args["task"])
            return json.dumps({"status": "started", "task_id": task_id,
                               "note": "Running in the background. Briefly acknowledge; the result comes later."})
        tool = self.mac.get(name)
        if tool is None or not tool.quick:
            return json.dumps({"error": f"unknown tool {name}"})
        if error := validate(args, tool.schema):
            return json.dumps({"error": error})
        try:
            result = await tool.fn(**args)
            return result if isinstance(result, str) else json.dumps(result)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ── delegated tasks ──

    def start_task(self, task: str) -> int:
        self._task_counter += 1
        task_id = self._task_counter
        self.tasks[task_id] = asyncio.create_task(self._run_task(task_id, task))
        return task_id

    async def _run_task(self, task_id: int, task: str) -> None:
        await self.hub.emit({"type": "task.started", "id": task_id, "task": task})
        await self.hub.set_status("working")

        async def on_event(event: dict) -> None:
            await self.hub.emit({**event, "id": task_id})

        try:
            result = await self.agent.run(task, on_event)
            await self.hub.emit({"type": "task.done", "id": task_id, "result": result})
            await self.notify(f"Background task {task_id} finished. Result: {result}")
        except Exception as exc:
            log.exception("Task %s failed", task_id)
            await self.hub.emit({"type": "task.failed", "id": task_id, "error": str(exc)})
            await self.notify(f"Background task {task_id} failed: {exc}. Tell {self.settings.user_name} briefly.")
        finally:
            self.tasks.pop(task_id, None)
            await self.hub.set_status(self._idle_state())

    def _idle_state(self) -> str:
        if self.capturing:
            return "listening"
        if self.response_active:
            return "thinking"
        return "working" if any(not t.done() for t in self.tasks.values()) else "idle"

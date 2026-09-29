"""Human-in-the-loop approval for write actions (send, delete, post, run scripts...)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, Callable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

Emit = Callable[[dict], Awaitable[None]]

APPROVAL_TIMEOUT = 120  # seconds; no answer means "no"


class ApprovalBroker:
    """Routes approval requests to the notch UI when it's connected, else to the terminal."""

    def __init__(
        self,
        emit: Emit | None = None,
        has_ui: Callable[[], bool] = lambda: False,
        on_request: Callable[[str], Awaitable[None]] | None = None,
    ):
        self._emit = emit
        self._has_ui = has_ui
        self._on_request = on_request
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._terminal_lock = asyncio.Lock()

    async def request(self, title: str, detail: str) -> bool:
        if self._emit and self._has_ui():
            return await self._ask_ui(title, detail)
        return await self._ask_terminal(title, detail)

    def resolve(self, approval_id: str, approved: bool) -> None:
        future = self._pending.get(approval_id)
        if future and not future.done():
            future.set_result(approved)

    async def _ask_ui(self, title: str, detail: str) -> bool:
        approval_id = uuid.uuid4().hex[:8]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        await self._emit({"type": "approval.request", "id": approval_id, "title": title, "detail": detail})
        if self._on_request:
            await self._on_request(title)
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT)
        except TimeoutError:
            return False
        finally:
            self._pending.pop(approval_id, None)
            await self._emit({"type": "approval.resolved", "id": approval_id})

    async def _ask_terminal(self, title: str, detail: str) -> bool:
        async with self._terminal_lock:  # one prompt at a time when tools run in parallel
            def ask() -> bool:
                console = Console()
                console.print(Panel(detail, title=f"[bold yellow]Approve: {title}", border_style="yellow"))
                return Confirm.ask("Run this?", default=False)

            return await asyncio.to_thread(ask)

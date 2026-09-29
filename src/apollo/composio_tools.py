"""Composio integration (Gmail, Calendar, Slack, Notion, GitHub, ...) via a tool-router session.

The session exposes a few meta tools instead of thousands of app tools:
COMPOSIO_SEARCH_TOOLS finds the right app action, COMPOSIO_MULTI_EXECUTE_TOOL runs actions,
COMPOSIO_MANAGE_CONNECTIONS returns auth links for accounts that aren't connected yet.
Every app action that changes something is routed through the approval broker.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from composio import Composio
from composio_anthropic import AnthropicProvider

from .config import Settings

SAFE_META_TOOLS = {
    "COMPOSIO_SEARCH_TOOLS",
    "COMPOSIO_GET_TOOL_SCHEMAS",
    "COMPOSIO_MANAGE_CONNECTIONS",
    "COMPOSIO_WAIT_FOR_CONNECTIONS",
}
EXECUTE_TOOL = "COMPOSIO_MULTI_EXECUTE_TOOL"

READ_VERBS = {
    "GET", "LIST", "FETCH", "SEARCH", "FIND", "READ", "RETRIEVE", "QUERY", "DESCRIBE",
    "VIEW", "CHECK", "COUNT", "LOOKUP", "DOWNLOAD", "EXPORT", "WHOAMI", "INFO", "STATUS",
}
WRITE_VERBS = {
    "SEND", "CREATE", "DELETE", "REMOVE", "UPDATE", "EDIT", "MODIFY", "PATCH", "POST", "PUT",
    "REPLY", "FORWARD", "MOVE", "ADD", "INSERT", "UPLOAD", "SET", "TRASH", "ARCHIVE", "INVITE",
    "SHARE", "PUBLISH", "MERGE", "CLOSE", "APPROVE", "SCHEDULE", "CANCEL", "ACCEPT", "DECLINE",
    "STAR", "UNSTAR", "LABEL", "MARK", "BATCH", "EXECUTE", "RUN", "TRIGGER", "REVOKE", "TRANSFER",
}
URL_RE = re.compile(r"https://[^\s\"'<>\\]+")


def action_needs_approval(slug: str) -> bool:
    """Classify an app action by the verbs in its slug. Unknown actions need approval."""
    words = set(slug.upper().split("_"))
    writes = words & WRITE_VERBS
    if writes == {"CREATE"} and "DRAFT" in words:
        return False  # drafting is harmless; sending it is what needs approval
    if writes:
        return True
    return not (words & READ_VERBS)


def pretty_action(slug: str) -> str:
    toolkit, _, action = slug.partition("_")
    return f"{toolkit.title()} · {action.replace('_', ' ').title()}" if action else slug.title()


def _preview(arguments: Any, limit: int = 1200) -> str:
    text = json.dumps(arguments, indent=2, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n…"


@dataclass
class Classification:
    needs_approval: bool
    step: str
    title: str = ""
    detail: str = ""


class ComposioTools:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.composio = Composio(provider=AnthropicProvider())
        self.session = None
        self.tools: list[dict] = []

    def start(self) -> None:
        kwargs: dict[str, Any] = {"user_id": self.settings.composio_user_id, "sandbox": {"enable": False}}
        if self.settings.composio_toolkits:
            kwargs["toolkits"] = self.settings.composio_toolkits
        self.session = self.composio.create(**kwargs)
        self.tools = [dict(t) for t in self.session.tools()]
        for tool in self.tools:
            tool["eager_input_streaming"] = True

    @property
    def names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    def classify(self, name: str, args: dict) -> Classification:
        if name in SAFE_META_TOOLS:
            steps = {
                "COMPOSIO_SEARCH_TOOLS": "Finding the right app action",
                "COMPOSIO_GET_TOOL_SCHEMAS": "Looking up how to use it",
            }
            return Classification(False, steps.get(name, "Checking connected accounts"))

        if name == EXECUTE_TOOL:
            calls = [c for c in args.get("tools", []) if isinstance(c, dict)]
            slugs = [str(c.get("tool_slug", "UNKNOWN")) for c in calls]
            step = ", ".join(pretty_action(s) for s in slugs) or "Running app action"
            risky = [c for c in calls if action_needs_approval(str(c.get("tool_slug", "")))] or ([] if calls else [args])
            if not risky:
                return Classification(False, step)
            detail = "\n\n".join(
                f"{pretty_action(str(c.get('tool_slug', '?')))}\n{_preview(c.get('arguments', {}))}" for c in risky
            )
            return Classification(True, step, title=", ".join(pretty_action(str(c.get("tool_slug", "?"))) for c in risky), detail=detail)

        # Direct app tools (if a session exposes them) or anything unexpected.
        return Classification(action_needs_approval(name), pretty_action(name),
                              title=pretty_action(name), detail=_preview(args))

    def execute(self, name: str, args: dict) -> str:
        """Blocking; call via asyncio.to_thread."""
        call = SimpleNamespace(name=name, input=args, id="")
        result = self.composio.provider.execute_tool_call(session=self.session, tool_call=call)
        return json.dumps(result, ensure_ascii=False, default=str)

    @staticmethod
    def auth_links(result: str) -> list[str]:
        return list(dict.fromkeys(URL_RE.findall(result)))

    # ── account management (CLI) ──

    def connect(self, toolkit: str):
        return self.session.authorize(toolkit)

    def connections(self) -> list[tuple[str, bool]]:
        kwargs = {"toolkits": self.settings.composio_toolkits} if self.settings.composio_toolkits else {}
        page = self.session.toolkits(**kwargs)
        return [(item.slug, bool(item.connection and item.connection.is_active)) for item in page.items]

"""Local macOS tools: apps, files, clipboard, Spotify, Reminders, Shortcuts, screenshots, notch.

Each tool declares a risk level: "safe" tools run immediately, "confirm" tools need the
user's approval first. Tools marked `quick` are also exposed to the realtime voice model
so simple requests ("pause the music") never need a Claude round-trip.
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

Emit = Callable[[dict], Awaitable[None]]
ToolOutput = str | list[dict]

HOME = Path.home().resolve()


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Awaitable[ToolOutput]]
    risk: Literal["safe", "confirm"] = "safe"
    quick: bool = False
    label: Callable[[dict], str] | None = None

    def __post_init__(self) -> None:
        # Parameters marked "x-optional" are left out of `required`; the marker isn't sent to the APIs.
        required = [k for k, v in self.parameters.items() if not v.get("x-optional")]
        props = {k: {kk: vv for kk, vv in v.items() if kk != "x-optional"} for k, v in self.parameters.items()}
        self.schema: dict[str, Any] = {
            "type": "object", "properties": props, "required": required, "additionalProperties": False,
        }

    def anthropic_def(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
            "eager_input_streaming": True,
        }

    def realtime_def(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name, "description": self.description, "parameters": self.schema}

    def describe(self, args: dict) -> str:
        return self.label(args) if self.label else self.name.replace("_", " ").capitalize()


def _str(desc: str, optional: bool = False, **extra: Any) -> dict:
    return {"type": "string", "description": desc, **({"x-optional": True} if optional else {}), **extra}


def _int(desc: str, optional: bool = False, **extra: Any) -> dict:
    return {"type": "integer", "description": desc, **({"x-optional": True} if optional else {}), **extra}


def validate(args: Any, schema: dict) -> str | None:
    """Minimal check of model-supplied input against a tool schema. Returns an error or None."""
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for key in schema.get("required", []):
        if key not in args:
            return f"Missing required argument '{key}'."
    for key, value in args.items():
        spec = schema.get("properties", {}).get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"Unknown argument '{key}'."
            continue
        expected = types.get(spec.get("type", ""))
        if expected and (not isinstance(value, expected) or (spec.get("type") == "integer" and isinstance(value, bool))):
            return f"Argument '{key}' must be of type {spec['type']}."
        if "enum" in spec and value not in spec["enum"]:
            return f"Argument '{key}' must be one of {spec['enum']}."
    return None


# ── process helpers ────────────────────────────────────────────


async def run(*cmd: str, stdin: str | None = None, timeout: float = 30) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"{cmd[0]} exited with {proc.returncode}")
    return out.decode().strip()


async def applescript(script: str, timeout: float = 30) -> str:
    return await run("osascript", "-e", script, timeout=timeout)


def as_string(value: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── the toolset ────────────────────────────────────────────────


class MacTools:
    def __init__(self, emit: Emit | None = None):
        self._emit = emit
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    @property
    def quick_tools(self) -> list[Tool]:
        return [t for t in self.tools.values() if t.quick]

    async def emit(self, message: dict) -> None:
        if self._emit:
            await self._emit(message)

    def _build(self) -> list[Tool]:
        return [
            Tool("open_app", "Open or bring to front a Mac application by name (e.g. 'Safari', 'Figma').",
                 {"name": _str("Application name")}, self.open_app, quick=True,
                 label=lambda a: f"Opening {a.get('name', 'app')}"),
            Tool("open_url", "Open a URL in the default browser or its handler app (https, mailto, zoommtg, etc.).",
                 {"url": _str("The URL to open")}, self.open_url, quick=True, label=lambda a: "Opening link"),
            Tool("system_status", "Get the current date/time, battery level, output volume, and frontmost app.",
                 {}, self.system_status, quick=True, label=lambda a: "Checking system status"),
            Tool("set_volume", "Set the Mac's output volume.",
                 {"level": _int("Volume from 0 to 100", minimum=0, maximum=100)}, self.set_volume, quick=True,
                 label=lambda a: f"Setting volume to {a.get('level')}"),
            Tool("spotify", "Control Spotify playback or get the current track.",
                 {"action": _str("What to do", enum=["play", "pause", "toggle", "next", "previous", "now_playing"])},
                 self.spotify, quick=True, label=lambda a: f"Spotify: {a.get('action', '')}"),
            Tool("add_notch_task", "Add a to-do item to the user's task list in the notch.",
                 {"title": _str("The task")}, self.add_notch_task, quick=True,
                 label=lambda a: f"Adding task: {a.get('title', '')}"),
            Tool("start_focus_timer", "Start a focus (Pomodoro) session in the notch.",
                 {"minutes": _int("Session length in minutes", minimum=1, maximum=180)}, self.start_focus_timer,
                 quick=True, label=lambda a: f"Starting {a.get('minutes')}-minute focus session"),
            Tool("add_reminder", "Create a reminder in Apple Reminders, optionally with a due date/time.",
                 {"title": _str("Reminder text"),
                  "due": _str("Due date/time in ISO 8601 local time, e.g. 2026-10-02T15:00", optional=True),
                  "list": _str("Reminders list name (defaults to the default list)", optional=True)},
                 self.add_reminder, quick=True, label=lambda a: f"Adding reminder: {a.get('title', '')}"),
            Tool("search_files", "Search files in the user's home folder with Spotlight (names and contents).",
                 {"query": _str("Search text"), "limit": _int("Max results (default 20)", optional=True)},
                 self.search_files, label=lambda a: f"Searching files for '{a.get('query', '')}'"),
            Tool("read_file", "Read a text file from the user's home folder.",
                 {"path": _str("Absolute path or ~/path"),
                  "max_chars": _int("Max characters to return (default 20000)", optional=True)},
                 self.read_file, label=lambda a: f"Reading {Path(a.get('path', '')).name}"),
            Tool("get_clipboard", "Get the current clipboard text.", {}, self.get_clipboard,
                 label=lambda a: "Reading clipboard"),
            Tool("set_clipboard", "Replace the clipboard contents with text.",
                 {"text": _str("Text to copy")}, self.set_clipboard, risk="confirm", label=lambda a: "Copying to clipboard"),
            Tool("notify", "Show a macOS notification.",
                 {"title": _str("Notification title"), "message": _str("Notification body")}, self.notify,
                 label=lambda a: "Sending notification"),
            Tool("screenshot", "Take a screenshot of the main display to see what the user is looking at.",
                 {}, self.screenshot, label=lambda a: "Looking at your screen"),
            Tool("list_shortcuts", "List the user's Apple Shortcuts.", {}, self.list_shortcuts,
                 label=lambda a: "Listing Shortcuts"),
            Tool("run_shortcut", "Run one of the user's Apple Shortcuts by name, optionally with text input.",
                 {"name": _str("Shortcut name"), "input": _str("Text input for the shortcut", optional=True)},
                 self.run_shortcut, risk="confirm", label=lambda a: f"Running shortcut '{a.get('name', '')}'"),
            Tool("run_applescript",
                 "Run an AppleScript to control Mac apps (Mail, Messages, Finder, Calendar, Music, System Events...). "
                 "Use only when no more specific tool fits.",
                 {"script": _str("AppleScript source")}, self.run_applescript, risk="confirm",
                 label=lambda a: "Running AppleScript"),
        ]

    # ── implementations ──

    async def open_app(self, name: str) -> str:
        await run("open", "-a", name)
        return f"Opened {name}."

    async def open_url(self, url: str) -> str:
        if url.split(":", 1)[0].lower() in {"file", "javascript"}:
            raise ValueError("That URL scheme isn't allowed.")
        await run("open", url)
        return f"Opened {url}."

    async def system_status(self) -> str:
        now = datetime.now().astimezone()
        lines = [f"Now: {now.strftime('%A, %B %-d %Y, %-I:%M %p %Z')}"]
        try:
            batt = await run("pmset", "-g", "batt")
            lines.append("Battery: " + batt.splitlines()[-1].split("\t")[-1].replace(" present: true", ""))
        except Exception:
            pass
        try:
            lines.append("Volume: " + await applescript("output volume of (get volume settings)") + "%")
        except Exception:
            pass
        try:
            info = await run("lsappinfo", "info", "-only", "name", await run("lsappinfo", "front"))
            lines.append("Frontmost app: " + info.split("=", 1)[-1].strip('"'))
        except Exception:
            pass
        return "\n".join(lines)

    async def set_volume(self, level: int) -> str:
        await applescript(f"set volume output volume {max(0, min(100, level))}")
        return f"Volume set to {level}%."

    async def spotify(self, action: str) -> str:
        running = await applescript('application "Spotify" is running')
        if running != "true" and action != "play":
            return "Spotify isn't running."
        if action == "now_playing":
            state = await applescript(
                'tell application "Spotify" to return (player state as string) & " — " & '
                '(name of current track) & " by " & (artist of current track)')
            return state
        command = {"play": "play", "pause": "pause", "toggle": "playpause",
                   "next": "next track", "previous": "previous track"}[action]
        await applescript(f'tell application "Spotify" to {command}')
        return f"Spotify: {action} done."

    async def add_notch_task(self, title: str) -> str:
        await self.emit({"type": "notch.add_task", "title": title})
        return f"Added task: {title}"

    async def start_focus_timer(self, minutes: int) -> str:
        await self.emit({"type": "notch.focus", "minutes": minutes})
        return f"Started a {minutes}-minute focus session."

    async def add_reminder(self, title: str, due: str | None = None, list: str | None = None) -> str:
        props = f"name:{as_string(title)}"
        date_setup = ""
        if due:
            d = datetime.fromisoformat(due)
            date_setup = (
                "set d to current date\n"
                f"set year of d to {d.year}\nset month of d to {d.month}\nset day of d to {d.day}\n"
                f"set hours of d to {d.hour}\nset minutes of d to {d.minute}\nset seconds of d to 0\n"
            )
            props += ", due date:d"
        target = f"list {as_string(list)}" if list else "default list"
        await applescript(
            f'{date_setup}tell application "Reminders" to make new reminder at end of {target} '
            f"with properties {{{props}}}"
        )
        return f"Reminder added: {title}" + (f" (due {due})" if due else "")

    async def search_files(self, query: str, limit: int | None = None) -> str:
        out = await run("mdfind", "-onlyin", str(HOME), query, timeout=20)
        skip = ("/Library/", "/.", "/node_modules/", "/.venv/", "/build/")
        paths = [p for p in out.splitlines() if not any(s in p for s in skip)][: limit or 20]
        return "\n".join(paths) if paths else "No files found."

    async def read_file(self, path: str, max_chars: int | None = None) -> str:
        target = Path(path).expanduser().resolve()
        if not target.is_relative_to(HOME):
            raise PermissionError("Only files inside the home folder can be read.")
        if not target.is_file():
            raise FileNotFoundError(f"No file at {target}")
        text = target.read_text(errors="replace")
        limit = max_chars or 20_000
        return text if len(text) <= limit else text[:limit] + f"\n\n[... {len(text) - limit} more characters not shown]"

    async def get_clipboard(self) -> str:
        return await run("pbpaste") or "(clipboard is empty)"

    async def set_clipboard(self, text: str) -> str:
        await run("pbcopy", stdin=text)
        return "Copied to clipboard."

    async def notify(self, title: str, message: str) -> str:
        await applescript(f"display notification {as_string(message)} with title {as_string(title)}")
        return "Notification shown."

    async def screenshot(self) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "screen.jpg")
            await run("screencapture", "-x", "-m", "-t", "jpg", path)
            await run("sips", "-Z", "1568", path)  # keep within the model's preferred image size
            data = base64.standard_b64encode(Path(path).read_bytes()).decode()
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": "Screenshot of the user's main display."},
        ]

    async def list_shortcuts(self) -> str:
        return await run("shortcuts", "list") or "No shortcuts."

    async def run_shortcut(self, name: str, input: str | None = None) -> str:
        if input is None:
            out = await run("shortcuts", "run", name, timeout=120)
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
                f.write(input)
            try:
                out = await run("shortcuts", "run", name, "--input-path", f.name, timeout=120)
            finally:
                os.unlink(f.name)
        return out or f"Shortcut '{name}' finished."

    async def run_applescript(self, script: str) -> str:
        return await applescript(script, timeout=60) or "Done."

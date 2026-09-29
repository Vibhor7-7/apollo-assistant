"""`apollo` command line: serve, chat, connect, status, install/uninstall the launch agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from rich.console import Console

from .config import PROJECT_ROOT, settings

console = Console()

LAUNCH_AGENT_LABEL = "com.vibhor.apollo"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _require_keys(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        console.print(f"[red]Missing {', '.join(missing)}.[/] Copy .env.example to .env and add your keys.")
        sys.exit(1)


def cmd_serve(_: argparse.Namespace) -> None:
    _require_keys("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    from .server import serve_forever

    try:
        asyncio.run(serve_forever(settings))
    except KeyboardInterrupt:
        pass


def cmd_chat(_: argparse.Namespace) -> None:
    """Text-only agent in the terminal: no voice, approvals asked here."""
    _require_keys("ANTHROPIC_API_KEY")
    from .agent import ClaudeAgent
    from .approvals import ApprovalBroker
    from .composio_tools import ComposioTools
    from .mac_tools import MacTools

    async def show(event: dict) -> None:
        if event.get("type") == "task.step":
            console.print(f"  [dim]→ {event['text']}[/]")
        elif event.get("type") == "link":
            console.print(f"  [cyan]Connect link:[/] {event['url']}")
        elif event.get("type", "").startswith("notch."):
            console.print(f"  [dim](notch) {event}[/]")

    async def main() -> None:
        composio = None
        if os.getenv("COMPOSIO_API_KEY"):
            with console.status("Starting Composio session…"):
                try:
                    composio = ComposioTools(settings)
                    composio.start()
                except Exception as exc:
                    console.print(f"[yellow]Composio unavailable: {exc}[/]")
                    composio = None
        agent = ClaudeAgent(settings, MacTools(emit=show), composio, ApprovalBroker())
        console.print(f"[bold]Apollo[/] ({settings.agent_model}) — type a request, 'reset' to clear context, Ctrl-D to quit.")
        while True:
            try:
                task = await asyncio.to_thread(console.input, "[bold cyan]you ›[/] ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not task.strip():
                continue
            if task.strip().lower() == "reset":
                agent.reset()
                console.print("[dim]Context cleared.[/]")
                continue
            try:
                answer = await agent.run(task, show)
                console.print(f"[bold magenta]apollo ›[/] {answer}")
            except Exception as exc:
                console.print(f"[red]Error:[/] {exc}")

    asyncio.run(main())


def _composio():
    _require_keys("COMPOSIO_API_KEY")
    from .composio_tools import ComposioTools

    tools = ComposioTools(settings)
    tools.start()
    return tools


def cmd_connect(args: argparse.Namespace) -> None:
    tools = _composio()
    request = tools.connect(args.toolkit)
    if not request.redirect_url:
        console.print(f"[green]{args.toolkit} is already connected (or needs no auth).[/]")
        return
    console.print(f"Opening the {args.toolkit} sign-in page…\n{request.redirect_url}")
    webbrowser.open(request.redirect_url)
    with console.status("Waiting for you to finish signing in (5 min)…"):
        request.wait_for_connection(timeout=300)
    console.print(f"[green]✓ {args.toolkit} connected.[/]")


def cmd_status(_: argparse.Namespace) -> None:
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COMPOSIO_API_KEY"):
        mark = "[green]✓[/]" if os.getenv(key) else "[red]✗ missing[/]"
        console.print(f"{key:20} {mark}")
    if os.getenv("COMPOSIO_API_KEY"):
        console.print("\n[bold]Composio accounts[/]")
        for slug, active in _composio().connections():
            console.print(f"  {slug:18} {'[green]connected[/]' if active else '[dim]not connected — apollo connect ' + slug + '[/]'}")
    loaded = subprocess.run(["launchctl", "list", LAUNCH_AGENT_LABEL], capture_output=True).returncode == 0
    console.print(f"\nLaunch agent: {'[green]running at login[/]' if loaded else '[dim]not installed[/]'}")


def cmd_install_agent(_: argparse.Namespace) -> None:
    uv = shutil.which("uv")
    if not uv:
        console.print("[red]uv not found on PATH.[/]")
        sys.exit(1)
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [uv, "run", "--project", str(PROJECT_ROOT), "apollo", "serve"],
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / "apollo.log"),
        "StandardErrorPath": str(logs / "apollo.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)], capture_output=True)
    LAUNCH_AGENT_PATH.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)], check=True)
    console.print(f"[green]✓ Apollo will run at login.[/] Logs: {logs / 'apollo.log'}")


def cmd_uninstall_agent(_: argparse.Namespace) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)], capture_output=True)
    LAUNCH_AGENT_PATH.unlink(missing_ok=True)
    console.print("[green]✓ Launch agent removed.[/]")


def cmd_restart(_: argparse.Namespace) -> None:
    """Restart the background server (after code or .env changes) and wait until it's back up."""
    service = f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
    if subprocess.run(["launchctl", "print", service], capture_output=True).returncode != 0:
        console.print("[yellow]The launch agent isn't installed.[/] If `apollo serve` is running in a terminal, "
                      "press Ctrl-C and start it again, or run `apollo install-agent`.")
        sys.exit(1)

    subprocess.run(["launchctl", "kickstart", "-k", service], check=True)
    with console.status("Restarting Apollo…"):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                socket.create_connection((settings.host, settings.port), timeout=0.5).close()
                console.print(f"[green]✓ Apollo restarted[/] (ws://{settings.host}:{settings.port}); the notch reconnects automatically.")
                return
            except OSError:
                continue

    log = PROJECT_ROOT / "logs" / "apollo.log"
    console.print(f"[red]Apollo didn't come back within 30s.[/] Last log lines ({log}):")
    if log.exists():
        console.print("\n".join(log.read_text(errors="replace").splitlines()[-15:]), markup=False, highlight=False)
    sys.exit(1)

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "websockets", "composio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="apollo", description="Apollo personal voice assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run the assistant server the notch connects to").set_defaults(fn=cmd_serve)
    sub.add_parser("chat", help="Text chat with the agent in the terminal").set_defaults(fn=cmd_chat)
    connect = sub.add_parser("connect", help="Connect an account via Composio (e.g. gmail)")
    connect.add_argument("toolkit")
    connect.set_defaults(fn=cmd_connect)
    sub.add_parser("status", help="Show keys, connected accounts, and launch agent").set_defaults(fn=cmd_status)
    sub.add_parser("install-agent", help="Start Apollo automatically at login").set_defaults(fn=cmd_install_agent)
    sub.add_parser("uninstall-agent", help="Stop starting Apollo at login").set_defaults(fn=cmd_uninstall_agent)
    sub.add_parser("restart", help="Restart the background server after changes").set_defaults(fn=cmd_restart)

    args = parser.parse_args()
    args.fn(args)

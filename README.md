# Apollo

A personal voice assistant for macOS, controlled from the notch.

- **Hold right ⌘** and talk. Apollo answers out loud. Quick things (music, volume, apps, timers, tasks, reminders) happen instantly. Bigger jobs across Gmail, Calendar, Slack, Notion, GitHub and your Mac are handed to a Claude agent.
- **Hold Fn** anywhere and speak. Cleaned-up text is typed into whatever app you're in, like Wispr Flow.
- **Approve before anything is sent.** Emails, messages, posts, deletes and scripts show an approval card in the notch first.

The full design is in [PRD.md](PRD.md).

```
Notch app (Swift) ⇄ ws://127.0.0.1:8765 ⇄ apollo serve (Python)
                                           ├─ OpenAI gpt-realtime-2.1 (voice)
                                           ├─ Claude claude-opus-5 (agent + dictation polish)
                                           ├─ Composio (your online accounts)
                                           └─ Mac tools (AppleScript, files, Spotify, ...)
```

## Setup

1. **Keys.** Copy the example env file and fill in your keys:
   ```bash
   cp .env.example .env
   ```
   You need `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `COMPOSIO_API_KEY`.

2. **Install** (needs [uv](https://docs.astral.sh/uv/)):
   ```bash
   uv sync
   ```

3. **Connect your accounts.** Each one opens a browser sign-in:
   ```bash
   uv run apollo connect gmail
   uv run apollo connect googlecalendar
   uv run apollo connect slack      # etc.
   uv run apollo status             # check keys + connections
   ```
   You can also just ask Apollo to use an app. If it isn't connected yet, a **Connect** link appears in the notch.

4. **Try the agent in the terminal first** (text only; approvals are asked here):
   ```bash
   uv run apollo chat
   ```

5. **Run the server** the notch connects to:
   ```bash
   uv run apollo serve              # foreground
   uv run apollo install-agent      # or: start automatically at login (logs in ./logs)
   ```

6. **Notch app.** The Apollo tab (✨) is built into Vibhor's Notch. Rebuild it with `./build.sh --install` in that repo.

## macOS permissions (first run)

| Permission | Why | Where |
|---|---|---|
| Microphone | Assistant + dictation | Prompted on first use |
| Accessibility | Hold-key hotkeys + pasting dictation | System Settings → Privacy & Security → Accessibility → enable **Vibhor's Notch** |
| Automation | Spotify, Reminders, System Events via AppleScript | Prompted per app on first use |
| Screen Recording | "What's on my screen?" (screenshot tool) | Grant to the app running `apollo serve` (Terminal, or `uv` for the launch agent) |

For **Fn dictation**, set System Settings → Keyboard → **Press 🌐 key to** → **Do Nothing**, so macOS doesn't open the emoji picker or its own dictation.

> The notch app is ad-hoc signed, so macOS treats each rebuild as a new app. After `./build.sh --install`, you may need to toggle Vibhor's Notch off and on again under Accessibility.

## Commands

| Command | What it does |
|---|---|
| `apollo serve` | Run the assistant server (voice, agent, dictation) |
| `apollo chat` | Text chat with the agent in the terminal |
| `apollo connect <toolkit>` | Connect an account through Composio |
| `apollo status` | Show API keys, connected accounts and launch-agent status |
| `apollo install-agent` / `uninstall-agent` | Start or stop running at login |
| `apollo restart` | Restart the background server after code or `.env` changes, and wait until it's back |

## Configuration

Everything is set in `.env`. See [.env.example](.env.example) for models, voice, reasoning effort, dictation polish on/off, keyword hints, and which Composio toolkits are enabled.

## Project layout

```
src/apollo/
  cli.py            apollo command
  server.py         WebSocket hub: auth, routing, audio channels
  voice.py          OpenAI Realtime session: push-to-talk, interruption, quick tools, delegation
  agent.py          Claude tool loop: streaming, approvals, parallel tools, refusal fallback
  composio_tools.py Composio session + read/write classification of app actions
  mac_tools.py      Local Mac tools
  approvals.py      Approval broker (notch card or terminal prompt)
  dictation.py      Transcribe (gpt-transcribe) + polish (Claude)
  config.py         Settings from .env
```

## Security notes

- The server listens only on `127.0.0.1`. It requires a per-launch token (`~/Library/Application Support/Apollo/token`, mode 600) and rejects browser connections.
- Anything that sends, posts, deletes or schedules waits for your approval in the notch. It is denied automatically after 2 minutes.
- The agent treats email, document and web content as data, not instructions.
- `.env` is gitignored. Keep your keys out of commits.

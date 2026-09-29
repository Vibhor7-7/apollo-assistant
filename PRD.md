# Apollo: Product Requirements Document

**Owner:** Vibhor
**Status:** v1 in build
**Last updated:** 2026-09-29

---

## 1. Summary

Apollo is a personal voice assistant for macOS. You hold a key, say what you want, and Apollo does it across your online accounts (Gmail, Calendar, Slack, Notion, GitHub and more, through Composio) and on your Mac (apps, files, Spotify, Reminders, Shortcuts). It talks back in a natural voice. Everything is controlled from a panel in the MacBook notch, built into the existing **Vibhor's Notch** app.

Apollo also has a **dictation mode**, similar to Wispr Flow. Hold `Fn`, speak, and release, and cleaned-up text is typed into whatever app you're using.

## 2. Goals

1. **Voice-first assistant with real reach.** One key press should be enough to get a multi-step task done across apps. For example: "find the email from Prof. Chen about the midterm and draft a reply saying I'll be there".
2. **Natural conversation.** Low-latency speech in and out. You can interrupt, and quick actions happen instantly.
3. **Safe by default.** Nothing is sent, deleted, posted or scheduled without an explicit approval in the notch.
4. **Fast dictation anywhere.** Text appears in the focused app within about 1–2 seconds of releasing `Fn`.
5. **Use existing credits.** Anthropic for reasoning and polish, OpenAI for voice, Composio for integrations.

### Non-goals (v1)
- Multi-user or cloud hosting. Apollo runs locally, for one user.
- Always-on wake word. v1 is push-to-talk (see §10).
- Mobile or iPhone companion.
- Full computer-use control (mouse and keyboard automation). This is deferred (§10).

## 3. Users and key scenarios

The only user is Vibhor, a student and developer who works on a MacBook with a notch.

| # | Scenario | Path |
|---|---|---|
| S1 | "Pause the music" / "Volume to 30" / "Open Figma" | Realtime quick tool, instant (under 1 s) |
| S2 | "What's on my calendar tomorrow?" | Delegated to Claude, then Composio Google Calendar (read), then spoken summary |
| S3 | "Reply to Sarah's last email saying Thursday works" | Claude reads the thread, drafts, and shows an **approval card**. It sends only after Approve. |
| S4 | "Add 'finish CS 341 assignment' to my tasks" / "Start a 50-minute focus session" | Quick tool that drives the notch Tasks and Focus tabs |
| S5 | "What's on my screen?" | Claude takes a screenshot (vision) and answers |
| S6 | Holds `Fn` in Slack and says "um so I think we should uh ship it Friday no Thursday" | Dictation inserts "I think we should ship it Thursday." |
| S7 | "Connect my Notion" | Composio auth link opens in the browser. Future Notion requests then work. |

## 4. Architecture

```
┌──────────── Vibhor's Notch (Swift / SwiftUI) ────────────┐
│ Hotkeys: hold Right ⌘ = assistant, hold Fn = dictation   │
│ AVAudioEngine with voice processing (echo cancellation)  │
│   mic → PCM16 24 kHz → WebSocket ; speaker ← PCM16 24 kHz│
│ Assistant tab: transcript, tasks, approval cards, input  │
│ Collapsed notch: Listening / Thinking / Speaking / etc.  │
│ Pastes dictation results into the frontmost app (⌘V)     │
└───────────────┬──────────────────────────────────────────┘
                │ ws://127.0.0.1:8765  (JSON control + binary audio,
                │ token auth, browser origins rejected)
┌───────────────▼──────────── apollo serve (Python) ───────┐
│ Hub: routes messages, approval broker                    │
│                                                          │
│ VoiceSession: OpenAI Realtime (gpt-realtime-2.1)          │
│   push-to-talk, speaks replies, quick tools,             │
│   delegate_to_agent(task) ─────────────┐                 │
│                                        ▼                 │
│ ClaudeAgent (claude-opus-5, streaming tool loop)         │
│   Composio session tools (meta tools: search / execute / │
│   manage connections), Mac tools (AppleScript, files,    │
│   Spotify, Reminders, Shortcuts, screenshot, notch)      │
│   Every write action goes through ApprovalBroker to the  │
│   notch card                                             │
│                                                          │
│ Dictation: gpt-transcribe (file), then Claude polish     │
└──────────────────────────────────────────────────────────┘
```

### Why this split
- **Voice model up front, Claude behind it.** The realtime model gives low latency and interruption. Claude is more reliable at long, multi-step tool chains. The voice model hands anything non-trivial to Claude through a single tool, `delegate_to_agent`.
- **Composio lives only on the Claude side.** All risky actions pass through one approval gate, and untrusted content (emails, web pages) never reaches the voice model with direct tool access.
- **Audio in Swift.** macOS voice processing (built-in echo cancellation) is a single call on `AVAudioEngine`. Without it, the assistant hears its own voice through the speakers.
- **Python for the brain.** The official Anthropic, OpenAI and Composio SDKs are all Python. You can also test from the terminal (`apollo chat`) without any UI.

## 5. Functional requirements

### 5.1 Assistant (voice)
- **FR-1** Holding **Right ⌘** (alone) starts listening. Releasing it sends the request. Pressing another key while holding Right ⌘ cancels (so ⌘-shortcuts still work). Taps shorter than 150 ms are ignored.
- **FR-2** Speaking while Apollo is talking interrupts it immediately: playback stops and the model's context is truncated to what was actually heard.
- **FR-3** Quick tools run inside the realtime session with no Claude call: Spotify control, volume, open app or URL, system status (time, battery), add a notch task, start the focus timer.
- **FR-4** Anything else goes to `delegate_to_agent(task)`. Apollo says a short acknowledgement ("On it"). When Claude finishes, the result is added to the voice conversation and spoken.
- **FR-5** You can type a request in the Assistant tab instead of speaking.
- **FR-6** The voice connection is opened on first use and closed after 10 minutes idle, so no credits are spent while idle.

### 5.2 Agent (Claude)
- **FR-7** Streaming tool loop with parallel tool calls, adaptive thinking, and prompt caching. Server-side refusal fallback (`fallbacks: "default"`) is enabled.
- **FR-8** Composio **session** mode: meta tools (`COMPOSIO_SEARCH_TOOLS`, `COMPOSIO_MULTI_EXECUTE_TOOL`, `COMPOSIO_MANAGE_CONNECTIONS`, `COMPOSIO_GET_TOOL_SCHEMAS`) find the right tool from thousands without loading them all. The remote sandbox or workbench is disabled.
- **FR-9** Mac tools: `open_app`, `open_url`, `search_files`, `read_file`, `get_clipboard`, `set_clipboard`, `notify`, `system_status`, `set_volume`, `spotify`, `add_reminder`, `list_shortcuts`, `run_shortcut`, `run_applescript`, `screenshot`, `add_notch_task`, `start_focus_timer`.
- **FR-10** Progress events (the tool being used, the account being accessed) stream to the notch while a task runs.
- **FR-11** Conversation context persists across requests within a server run, so "reply to that one" works. It resets on restart or with "new conversation".

### 5.3 Safety and approvals
- **FR-12** Every tool call is classified as **read** (auto-runs) or **write** (needs approval).
  - Composio app actions are classified by their slug verbs: `SEND`, `CREATE`, `DELETE`, `UPDATE`, `REPLY` and similar are write. `FETCH`, `LIST`, `GET`, `SEARCH` and similar are read. Unknown actions are treated as write.
  - Mac tools declare their risk statically. `run_applescript`, `run_shortcut` and `set_clipboard` are write.
- **FR-13** Approval requests pop the notch open to an approval card showing the action and its key arguments (recipient, subject, body preview). Approve runs it, Deny returns "user declined" to Claude, and a timeout after 2 minutes denies.
- **FR-14** Without the notch connected (`apollo chat`), approvals are asked in the terminal.
- **FR-15** The local WebSocket requires a per-launch random token (stored at `~/Library/Application Support/Apollo/token`, mode 0600) and rejects any request with a browser `Origin` header. This stops websites from driving the agent.
- **FR-16** The system prompt tells Claude to treat email, document and web content as data, never as instructions.

### 5.4 Dictation
- **FR-17** Holding **Fn** (alone) records and streams audio to `gpt-live-transcribe` while you talk, over a connection kept warm between dictations. Releasing Fn commits the audio, so the transcript is ready about 0.5 s later. The text is polished if needed and pasted into the frontmost app, and the clipboard is restored afterwards. If live transcription fails, the recorded clip is transcribed with `gpt-transcribe` instead. Results go only to the notch that started the dictation.
- **FR-18** Light polish (`claude-haiku-4-5`, about 0.6–1 s) runs only when the transcript needs it: fillers, self-corrections, spoken formatting or stutters. Clean speech is inserted as-is. Polish removes fillers and false starts, applies spoken self-corrections ("3, no 4" becomes "4"), and fixes punctuation, capitalization and lists. It keeps your wording and never answers or acts on the dictated content.
- **FR-19** If polishing fails or is refused, the raw transcript is inserted. Clips under 0.3 s are ignored.
- **FR-20** The collapsed notch shows "Dictating…", then "Transcribing…", while dictation is in progress.

### 5.5 Notch UI
- **FR-21** New **Assistant** tab showing:
  - connection status
  - live transcript (you and Apollo)
  - running tasks with their steps
  - approval cards
  - a text input
  - a mic button
- **FR-22** The collapsed notch shows assistant status (Listening, Thinking, Speaking, Working) and dictation status in its side areas. These take priority over the focus timer.
- **FR-23** The notch accepts `notch.add_task` and `notch.focus` commands from Apollo.
- **FR-24** If the service is offline, the tab says so and shows how to start it.

## 6. Non-functional requirements

| Area | Target |
|---|---|
| Quick-action latency (release to first audio) | < 1.2 s |
| Delegated task acknowledgement | < 1.5 s spoken; completion depends on the task |
| Dictation (release to text inserted) | < 1 s for clean speech, < 2 s when polished (measured: 0.65 s and 1.1–1.7 s) |
| Idle cost | $0 (no open realtime connection) |
| Privacy | Everything runs locally except the model and API calls. Keys live in `.env` (gitignored). No audio is stored. |
| Reliability | Notch reconnects to the service automatically. The service restarts via launchd. |

## 7. Models and services

| Role | Model / service | Env var |
|---|---|---|
| Agent reasoning | `claude-opus-5` (adaptive thinking, effort `high`) | `APOLLO_AGENT_MODEL`, `APOLLO_AGENT_EFFORT` |
| Dictation polish | `claude-haiku-4-5` | `APOLLO_POLISH_MODEL` |
| Voice | `gpt-realtime-2.1`, voice `marin` | `APOLLO_REALTIME_MODEL`, `APOLLO_VOICE` |
| Voice transcript (display) | `gpt-4o-mini-transcribe` | `APOLLO_REALTIME_TRANSCRIBE_MODEL` |
| Dictation STT | `gpt-live-transcribe` (streaming), with `gpt-transcribe` as fallback | `APOLLO_DICTATION_LIVE_MODEL`, `APOLLO_DICTATION_MODEL` |
| Integrations | Composio sessions | `COMPOSIO_API_KEY`, `APOLLO_COMPOSIO_TOOLKITS` |

## 8. Wire protocol (notch ↔ service)

Text frames are JSON `{"type": ...}`. Binary frames are `[1-byte channel][PCM16 LE mono 24 kHz]`.

**Client → server:**
- `hello{token}`
- `assistant.start` / `assistant.stop` / `assistant.cancel` / `assistant.text{text}` / `assistant.reset`
- `approval.response{id, approved}`
- `dictation.start` / `dictation.stop` / `dictation.cancel`
- Binary channel `0x01` is assistant mic audio, `0x02` is dictation mic audio.

**Server → client:**
- `ready`, `status{state}`, `transcript{role, text, final}`
- `task.started{id, task}` / `task.step{id, text}` / `task.done{id, result}` / `task.failed{id, error}`
- `approval.request{id, title, detail}` / `approval.resolved{id}`
- `link{title, url}`
- `audio.interrupt`
- `dictation.state{state}` / `dictation.result{text}` / `dictation.error{message}`
- `notch.add_task{title}` / `notch.focus{minutes}`
- `error{message}`
- Binary channel `0x03` is assistant speaker audio.

## 9. Milestones

| Phase | Deliverable | Status |
|---|---|---|
| P1 | `apollo chat`: Claude agent with Composio and Mac tools, with terminal approvals | ✅ v1 |
| P2 | `apollo serve`: WebSocket hub, realtime voice, delegation, approval broker | ✅ v1 |
| P3 | Notch Assistant tab, hotkeys, audio engine, collapsed-notch status | ✅ v1 |
| P4 | Dictation mode (Fn): transcribe, polish, paste | ✅ v1 |
| P5 | launchd auto-start, `apollo connect` / `apollo status` | ✅ v1 |
| P6 | Streaming dictation (`gpt-live-transcribe`), polish only when needed | ✅ v1.1 (live captions in the notch still planned) |
| P7 | Long-term memory (preferences, people), conversation compaction | Planned |
| P8 | Wake word, computer use, context-aware dictation | Planned |

## 10. Risks and open questions

- **Transcription model in realtime sessions.** OpenAI's docs don't say which STT model is best inside a push-to-talk realtime session. The default is `gpt-4o-mini-transcribe`, and it's configurable.
- **Composio meta-tool schemas** are sent by the server and not guaranteed stable. The approval classifier is defensive and treats anything unknown as write.
- **Echo and barge-in.** This depends on macOS voice processing. With push-to-talk, the worst case is the assistant hearing itself briefly while you hold the key.
- **Prompt injection** through emails and documents. Mitigated by the approval gate on every write, the "content is data" system rules, and keeping Composio away from the voice model.
- **Permissions** needed on first run:
  - Microphone
  - Accessibility (hotkeys and paste)
  - Automation (AppleScript control of Spotify, Reminders and System Events)
  - Screen Recording (screenshot tool)
- **Cost.** The realtime session re-bills conversation audio as cached input. The 10-minute idle disconnect bounds this.

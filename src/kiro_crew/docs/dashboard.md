# Web Dashboard

The dashboard is a React SPA at `http://localhost:5476` (or your configured URL). It provides chat, configuration, scheduled jobs, agent capabilities, and system/developer views.

## Accessing the Dashboard

- **Local**: open the dashboard URL after authenticating through a dashboard link; loopback requests are not exempt from token authentication.
- **SSH tunnel**: `ssh -NL 5476:localhost:5476 <host>` then open localhost:5476.
- **Remote**: type `!dashboard` in Slack to get an HMAC-SHA256-signed, IP-pinned, single-use link. The link is valid for 5 minutes and establishes a session cookie for up to 20 hours.
- **Custom domain**: after `kirocrew setup`, optionally use `http://kirocrew.localhost:5476`.
- **Custom URL**: set `dashboard.url` in config.json for non-localhost access.

### Remote Access Troubleshooting

If the dashboard doesn't load after setup:

1. Confirm the gateway is running: `kirocrew status`.
2. Test the API: `curl http://localhost:5476/api/status`.
3. Check for port conflicts: `lsof -i :5476`.
4. On remote dev desktops, you must use an SSH tunnel — the dashboard binds to localhost by default.
5. Run `kirocrew gateway -vv` for debug output.

## Pages

### Chat (`/chat`)

Multi-session parallel chat with full Markdown rendering, syntax-highlighted code blocks, Mermaid diagrams, and clickable file paths. A confirmed local file link in an agent reply opens that file in the chat side panel; a `:line` suffix opens the file at that line instead of navigating the browser back to Chat.

- **Multiple tabs**: each tab runs its own agent session in parallel.
- **Agent selection**: pick an agent before starting a chat, or switch mid-session.
- **Session history**: closed sessions appear in the collapsible history sidebar.
- **Resume**: click a history item to restore the full conversation.
- **Notifications**: click a notification to view it in the main pane.
- **Auto-titles**: sessions get auto-generated titles after a few turns.
- **Edit & resend**: edit and resend previous user messages with history preserved.
- **Fork session**: fork a session into a new tab with full context carried over.
- **Regenerate replies**: regenerate assistant replies with variant history navigation.
- **Prompt history**: ↑/↓ arrow keys navigate through previous prompts.
- **Tool purpose pills**: tool call labels show purpose text, persisted across reloads.
- **Batch tool rejection**: reject multiple pending tool approvals at once.
- **Cancel queued messages**: cancel button for messages waiting in the queue.
- **Edit queued messages**: edit a message waiting in the queue in place before it runs (order preserved); automatic recovery entries stay immutable so their delivery receipts remain bound to the original message.
- **iOS-style queue stack**: queued messages displayed as a visual stack.
- **Streaming transcription**: live speech-to-text partials via WebSocket.
- **Weighted content search**: session content search with weighted ranking.
- **Memory mode**: per-session choice — persistent (default), incognito (blocks learn_add), or temporary (no memory consolidation).
- **Merge queued messages**: optionally merge queued messages into a single prompt.
- **Cooperative stop**: soft-stop sends cancel first, falls back to hard kill after budget (preserves session state).
- **Tool input preview**: expandable tool input display in approval cards.
- **File upload**: on desktop, use **Upload file** from the `+` menu or drop a file into Chat. On a phone or other touch device, tap `+` to open the native system Files picker directly. Both paths accept images and regular files such as `.zip`, `.csv`, and `.docx`.
- **Folder management**: create, rename, and organize sessions into sidebar folders with indent borders.
- **Per-channel session folders**: optional, off by default — each channel's settings panel (Slack, Discord, Telegram, Teams, Webex, WeCom, WeChat) can file conversations that start there into a named sidebar folder, marked with the channel's brand mark. Config key: `<channel>.session_folder` ("" = off). The folder is created when the setting is saved, so the surfacing path only ever reads the folder store; a configured folder that no longer exists (config.json hand-edited, or the folder deleted) leaves conversations unfiled until the next save recreates it. Filing applies as each conversation is first surfaced; a session moved by hand afterwards stays where it was put.
- **Session colors**: per-session color picker for visual organization.

### Settings (`/settings/*`)

Settings uses tabbed panels for Overview, Imports, Chat, Display, Voice, Notifications, Shortcuts, Skills, Channels, Browser, Computer Use, Webhooks, Instances, Privacy, Security, Secrets, Developer, Releases, and About. `/overview` redirects to `/settings/overview`.

### Agent Capabilities (`/capabilities`)

Tabbed management for crews, agent templates, MCP connections, skills, steering, hooks, prompts, and workflow libraries. `/agents` and `/connections` redirect here.

### Schedule (`/schedule`)

Create and manage cron jobs, organize them in folders, and switch between list, calendar, and execution views.

### Developer (`/developer`)

Tabbed developer views for logs, system metrics, telemetry, storage, MCP pooling, memory, configuration, feature previews, and the session archive. The former standalone `/system` page is now the System tab here.

### Logs (`/logs`)

Live gateway log stream with level selection, filtering, wrapping, ordering, and tail controls.

### Hooks (`/hooks`)

Create, edit, test, enable, and delete chat lifecycle hooks; provider hooks are displayed read-only when supported.

### Apps (`/apps`)

Browse discoverable apps, manage the installed-app library, and open app detail or migration views.

### Other shipped routes

`/knowledge` opens the Knowledge Library, `/notifications` opens notifications, `/artifacts` opens artifact management, and `/deploy` opens artifact deployment. Built-in app routes are registered dynamically.

## Real-Time Updates

The dashboard uses a single WebSocket connection for all real-time events: chat streaming, status updates, notifications, slot changes, and log streaming. Reconnects automatically with exponential backoff — no page reload needed.

## Terminal

Terminal tabs in the chat side panel host a real shell (PTY) bound to the chat's working directory. Each session has its own WebSocket at `/api/ws/terminal/{sessionId}`. Binary frames carry raw PTY I/O; JSON text frames carry control messages:

| Frame | Direction | Payload | Meaning |
|---|---|---|---|
| `resize` | client → server | `{cols, rows}` | Viewport size change |
| `title` | server → client | `{text}` | Live tab title: foreground command name while one runs, else the shell cwd basename (polled ~1/s, pushed on change) |
| `cwd` | server → client | `{path}` | The shell's full live working directory (same poll, pushed on change) |
| `error` | server → client | `{message}` | Session-level failure |
| `pong` | server → client | — | Keepalive reply |

**Selection toolbar.** Highlighting text in a terminal shows a floating toolbar with **Send to chat** and **Copy**. Send to chat appends the selection to the chat composer draft (never overwrites the draft, never auto-sends), annotated with a `Terminal output (path):` header — using the live `cwd` value when the backend has reported one, else the terminal's spawn directory — and wrapped in a code fence so the agent reads it as literal output. Copy places the raw selection on the clipboard.

**Credential redaction.** The terminal shows exactly what your shell wrote. The live stream is not scanned, so a token you printed on purpose (`gh auth token`), a device-code login or presigned URL you are mid-flow on, and high-entropy build output such as an npm `integrity sha512-…` line all render as themselves. Nothing is gained by hiding them here: this panel is your own interactive shell, and anything that could read it could read the terminal app next to it.

The scan runs where the output actually leaves your machine's screen — the selection hand-off above, the one path by which terminal output reaches the agent. That re-scan is unconditional, has no setting to disable it, and reads the whole contiguous selection rather than one 4096-byte read at a time, so a credential split across a read boundary cannot slip past it.

## Dark/Light Theme

Toggle via the theme button in the topbar. Persists across sessions.

## Self-Update

The topbar shows the current version. When a newer version is available, a badge appears. Click to view the changelog and update with one click.

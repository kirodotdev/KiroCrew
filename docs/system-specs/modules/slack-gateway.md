# Slack Gateway Module

## Overview

The Slack integration (`kiro_crew/slack/`) connects KiroCrew to Slack via Socket Mode. DMs are routed through ACP to kiro-cli with real-time streaming and interactive tool approval.

## Architecture

```
Slack Socket Mode → events.py (dispatch) → handler.py → SessionManager → AcpClient → kiro-cli
                  ↘ interactive payloads → interactions.py (dispatch) → approve/reject/ack
                  ↘ member_joined_channel → allowlist.py (prompt_allowlist) → owner DM
```

## Files

| File | Purpose |
|------|---------|
| `slack/__init__.py` | Package (no eager imports to avoid aiohttp at import time) |
| `slack/client.py` | `SlackClientOps` ABC + `RealSlackClient` (slack-sdk wrapper) |
| `slack/files.py` | Non-audio file attachment processing — images (download → temp → ACP base64 inline), text (download → content inject), unsupported (metadata note). Size limits, mimetype allowlist, credential redaction, SEL audit |
| `slack/format.py` | Markdown → Slack mrkdwn conversion (headings, links, strike, tables, mermaid, ANSI strip, truncation) |
| `slack/handler.py` | `handle_message()` — streams ACP response, `handle_interaction()` — button clicks (with None provider guard) |
| `slack/gateway.py` | `GatewayOrchestrator` — service lifecycle, cron/heartbeat/secretary/subagent/task callbacks, shutdown, auto-update. Entry point: `run_gateway()` |
| `slack/events.py` | Socket Mode event routing — dedup (`SeenCache`), slash commands, `member_joined_channel` tracking, message dispatch |
| `slack/interactions.py` | Block Kit button routing — tool approval, OPTIONS choices, cron/subagent ack, allowlist approve/deny, track channel approve/deny |
| `slack/blocks.py` | Reusable Block Kit dict builders for slash command UIs (session list, send-to-slack). Action IDs: `mc_<command>_<action>[_<id>]` |
| `slack/allowlist.py` | Tracking-channel allowlist prompts (`prompt_allowlist`, `prompt_track_channel`) + config persistence (`persist_allowed_user`, `persist_tracking_channel`) |
| `slack/scope_probe.py` | Tracked-channel history-readability probe (`warn_unreadable_tracked_channels`) — warns when the installed token cannot read a tracked channel (e.g. a private channel on an install predating `groups:history`) |
| `slack/enterprise.py` | Enterprise Grid workspace validation — `validate_enterprise()` (startup auth.test + cache) + `check_message_origin()` (per-message team_id check). SEL audit on all outcomes. See V2160269460 |

## APIs

### Slack App OAuth Contract

The bundled `slack-manifest.yaml` is the setup source of truth. Its bot scopes
are `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`,
`commands`, `files:read`, `files:write`, `groups:history`, `groups:read`,
`im:history`, `im:read`, `im:write`, `reactions:write`, and `users:read`.
`message.groups` is subscribed alongside `message.channels` so private-channel
turns and thread continuation are delivered.

The manifest also requests user scopes `channels:history`, `channels:read`,
`groups:history`, `groups:read`, `im:history`, `im:read`, `mpim:history`,
`mpim:read`, `search:read`, and `users:read`. These scopes apply only to a
separately configured Slack MCP/search integration's `xoxp-...` token. The
gateway constructs every Slack client with `SLACK_BOT_TOKEN`; it does not read
or store the user token.

### `run_gateway(cfg: KiroCrewConfig, *, no_dashboard=False, no_crons=False) -> None`
Starts the Socket Mode listener. Blocks until SIGINT/SIGTERM. When `no_crons=True`, the `CronService` is instantiated but not started — cron jobs are visible in the dashboard but not executed. Use for multi-instance setups where a single primary instance handles cron execution. On shutdown, calls `dashboard_state.close_all_ws()` before `AppRunner.cleanup()` to prevent 30s hang from blocked WebSocket `async for msg` loops.

### Shutdown Sequence

1. First Ctrl+C sets `shutdown_event` → graceful shutdown begins (10s deadline)
2. Second Ctrl+C calls `os._exit(0)` immediately (force exit)
3. `_shutdown()` **first disarms the loop-stall watchdog** (`dashboard_state._loop_watchdog.stop()` + cancels `_loop_heartbeat`), then saves active chat slots, cancels handler tasks, stops cron/heartbeat, closes sessions. The watchdog MUST be disarmed before `close_all()`/`cancel_all()` because that teardown deliberately kills every kiro-cli child — the same `os.waitpid` reaping burst the watchdog guards against — and a slow teardown would otherwise let the armed `faulthandler.dump_traceback_later(exit=True)` timer `_exit(1)` the process mid-shutdown (a clean quit would look like a crash). The watchdog's own `on_cleanup` hook fires too late (inside `AppRunner.cleanup()`, gathered concurrently with the reaping).
4. The gateway clears its port-keyed run marker in both dashboard and API-only
   modes, then `cleanup_orphaned_sessions()` kills any kiro-cli PIDs tracked in
   the PID file before `os._exit(0)`.

### Event-loop stall watchdog & blocking-work executors

The gateway runs a single asyncio loop, so any blocking call on the loop thread freezes the whole backend. Two mechanisms contain this (see `dashboard/loop_watchdog.py`, `executors.py`):

- **`LoopStallWatchdog`** — armed only when `faulthandler.is_enabled()` (the real `gateway` entrypoint; not `chat`/`tui`). The async heartbeat (`dashboard/server.py`, 5s interval) `beat()`s it each tick, re-arming a C-level `dump_traceback_later(exit=True)` timer that dumps all thread stacks and `_exit()`s if the loop goes silent. Desktop/foreground launches automatically use 25s; managed systemd/launchd gateways automatically use 90s because they have no Electron probe and WSL, VM, or heavy disk pressure can suspend scheduling long enough to make 25s a false death. The config value is nullable/automatic so an unrelated full config save cannot pin either launch-class default; any explicit `dashboard.loop_stall_exit_after_secs` value, including 25, overrides both. Older full-config saves may have materialized the former 25-second default; Kiro Crew reports that through the read-only superseded-default warning and `doctor` rather than guessing whether the value was deliberate. The managed path emits a non-fatal all-thread dump to stderr at `stall_after=30s`, never to the fatal crash-sentinel file, then exits at its service budget if the loop has not recovered. If the hard timer is disabled or fails to arm, that soft-only fallback is written to the dedicated dump file as well as stderr so it remains discoverable. `KIROCREW_SERVICE_MANAGED=1` in the generated systemd unit or launchd plist is the sole managed-launch authority; inherited systemd metadata is deliberately ignored because descendants receive it too. `kirocrew doctor` detects an installed definition without the marker and tells the operator to run `kirocrew service install` once to regenerate it and adopt the managed-service default.
- **Bounded executors** — blocking maintenance work is offloaded off the default executor (which the loop uses for DNS) into two separate bounded pools: `maintenance_executor()` (`mc-maint`, fast orphan-reaping sweeps + agent-overlay rewrites) and `cron_executor()` (`mc-cron`, long/concurrent cron command & script jobs). Kept separate so a burst of cron jobs cannot starve the orphan sweeps. MCP `probe_all()` fan-out is bounded by `asyncio.Semaphore(5)`.

### `handle_message(slack, sessions, channel, text, thread_ts, msg_ts, user_id, approval_mode, ..., subagent_manager) -> None`
Processes a single incoming message with streaming:

**Session key discipline:** the handler derives two values at entry —
`reply_ts = thread_ts or msg_ts` (the bare Slack thread timestamp, used for
posting replies and as the key of thread-indexed maps: `SessionMap`'s
thread→session index and the dashboard `_slack_to_slot` map) and
`session_key = canonical_key(reply_ts)` (the namespaced `slack:<ts>` form,
used for everything session-scoped: `SessionManager` registry, conversation
log, per-thread override maps, trust set). The canonical form is stable
across all messages of a thread; the legacy bare form is folded onto the same
live session by `SessionManager._fold_key` (see session.md).

1. Check hooks for auto-reply
2. Check `status` keyword — reply with stats summary
3. Check owner-only `!` commands (`!yolo`, `!agent`, `!ta`, `!allowlist`, `!dashboard`)
4. Check spawn/bg commands (subagent manager)
5. Check cron keyword commands (`cron list`, `cron remove`, `cron pause`, `cron resume`)
6. Check task runner commands (`task run <path>`, `run status`)
7. Initialize `StatusReactionController` → set phase "queued" (👀)
8. Post "Thinking…" message
9. Acquire per-session semaphore (via `get_or_create`) to serialize concurrent messages
10. Create `Task` for lifecycle tracking
11. Stream events from provider
12. Progressive message edits (~1/sec) with cursor indicator (▍)
13. On `text_chunk` event: accumulate response text, set phase "thinking" (🤔)
14. On `thinking_chunk` event: accumulate thinking separately, set phase "thinking" (🤔)
15. On `tool_call` event: set phase based on tool type — coding (👨‍💻), browsing (🌐), or generic tool (🔧)
16. On `permission_request` event: pause stall watchdog, auto-approve or post Block Kit buttons, resume watchdog
17. On `complete`: record success, check context usage
18. On error: record failure (circuit breaker trips at 5 consecutive)
19. Finalize status reactions in `finally` block → done (🦞) or error (😱); release semaphore
20. Strip inline `<thinking>` tags from accumulated text
21. Final update with mrkdwn-converted response (split into multiple messages if over 3900 chars)
22. Post thinking content as 💭 thread reply (if any, and `slack.show_thinking` is true)

### `StatusReactionController`
Phase-aware Slack reaction manager with stall detection. Manages emoji lifecycle per message:
- **Phases**: queued (👀) → thinking (🤔) → coding (👨‍💻) / browsing (🌐) / tool (🔧) → done (🦞) / error (😱). All phase emojis are configurable via `slack.reactions` in `config.json`.
- **Debouncing**: Intermediate phase transitions debounced at 700ms to prevent flickering from rapid tool calls. Terminal states fire immediately.
- **Stall detection**: Soft stall (🥱) at 15s, hard stall (😨) at 45s of no progress. Resets on any ACP event. Paused during tool approval waits.
- **Tool mapping**: `_tool_to_phase(tool_name, tool_kind)` maps tools to phases — prefers `tool_kind` from ACP, falls back to tool name with MCP `__` separator handling.

### LLM-Initiated Commands

The LLM executes cron and spawn operations via bash using the `kirocrew` CLI:
- `kirocrew cron add "name" "message" --every 300` — writes to crons.json, gateway auto-detects via mtime sync
- `kirocrew spawn "task"` — POSTs to dashboard API at localhost:5476, gateway spawns subagent

### `handle_interaction(channel, msg_ts, action_id) -> None`
Routes Block Kit button clicks to pending tool approvals:
- `approve_tool` action → `AcpClient.approve_tool()`, resumes streaming
- `reject_tool` action → `AcpClient.reject_tool()`, stops streaming

### `SlackClientOps` (ABC)
Testable interface for Slack Web API:
- `post_message(channel, text, thread_ts) -> str`
- `post_blocks(channel, blocks, text, thread_ts) -> str`
- `update_message(channel, ts, text)`
- `delete_message(channel, ts)`
- `add_reaction(channel, ts, emoji)`
- `remove_reaction(channel, ts, emoji)`

## Per-Channel Activation Modes

Each channel can have its own activation mode controlling when the bot responds:

| Mode | Behavior |
|------|----------|
| `always` | Process every message from allowed users |
| `mention` | Only respond when @mentioned; continue in thread replies if bot has active session |
| `observe` | Passively record all messages with deep history buffer; respond only when @mentioned (like `mention` but with richer context) |
| `off` | Ignore all messages completely — no history recorded |

**Defaults**: DMs (`D`-prefix) default to `always`. Group channels (`C`/`G`-prefix) default to `mention`.

**Config** (`config.json`):
```json
{
  "slack": {
    "channels": {
      "C0123ONCALL": { "activation": "always", "agent": "ops" },
      "C0456REVIEWS": { "activation": "mention", "agent": "reviewer" },
      "C0789GENERAL": { "activation": "off" }
    },
    "dm_activation": "always"
  }
}
```

**Per-channel agent override**: Each channel can specify an agent that overrides the global default. The agent is passed to `SessionManager.get_or_create()`.

**Thread reply behavior** (mention mode): When the bot is @mentioned in a group channel, it responds in a thread. Subsequent replies in that thread are processed without needing @mention, as long as the bot has an active session for that thread (`SessionManager.has_session(thread_ts)`). Replies in threads where the bot was never mentioned are ignored.

**Owner commands** (`!channel`):
- `!channel` — show current channel activation mode and agent
- `!channel always|mention|observe|off` — set activation mode, persisted to `config.json`
- `!channel agent <name>` — set per-channel agent override
- `!channel agent off` — remove per-channel agent override

**Implementation**: `events.py:_route_message()` checks `orch._cfg.channel_config(channel)` before dispatching. The `@mention` prefix is stripped from text before sending to the LLM. `_persist_channel_config()` in `handler.py` writes to `config.json` atomically via tmp+rename.

## Tracking Channel Monitoring

### Slack Commands

#### Slash Command (`events.py`)

Command name configurable via `slack.command` in config (default: `kirocrew`).

| Command | Handler | Purpose |
|---------|---------|---------|
| `/<command> @user` | `_handle_slash` | Allowlist prompt (Allow/Deny) to owner |
| `/<command> #channel` | `_handle_slash` | Tracking-channel prompt (Track/Ignore) to owner |
| `/<command> sessions` | `_handle_slash` | List active sessions with Slack link status (Block Kit) |
| `/<command> sessions resume <key>` | `_handle_slash` | Resume a session in the current Slack thread |
| `/<command> dashboard` | `_handle_slash` | Generate presigned dashboard link (DM'd to user) |
| `/<command> restart` | `_handle_restart` | Restart the gateway (owner-only; requires an `INVOCATION_ID` / systemd supervisor, else refuses). SEL-audited (approved/denied). Best-effort `save_all_slots` + `close_all` + `sel.flush` (each bounded by `wait_for`), then `os._exit(1)` so the supervisor respawns |

#### Owner-Only `!` Commands (`handler.py`)

Restricted to `KIROCREW_OWNER_ID`. Processed before keyword commands.

| Command | Purpose |
|---------|---------|
| `!yolo on/off/status` | Toggle global auto-approve for all tool calls |
| `!agent <name>` / `!agent off` | Switch kiro-cli agent globally (all new sessions) |
| `!ta <name>` / `!ta off` | Switch agent for current thread only |
| `!allowlist @user` | Grant/revoke user access |
| `!allowlist #channel` | Add/remove tracking channel |
| `!restart` | Restart the gateway. Bang alias intercepted in `events.py` before the LLM session; delegates to `/kirocrew restart` (`_handle_restart`) so owner-check + supervisor guard stay a single source of truth (`handler.py:_BANG_TO_SLASH`) |

#### Allowed-User `!` Commands (`handler.py`)

Available to any user on the allowlist (not just owner).

| Command | Purpose |
|---------|---------|
| `!dashboard [duration]` | Get a presigned dashboard link (DM'd to you) — **deprecated, use `/kirocrew dashboard`** |
| `!stop` | Force-halt the active agent execution in the current thread. Sends cooperative `session/cancel`; falls back to hard kill if not acked within `agent.soft_stop_budget_secs`. Posts ephemeral Block Kit stopping message with Kill Now button. If no execution is running, replies "Nothing running." |

#### Keyword Commands (`handler.py`)

Available to all allowed users.

| Command | Handler | Purpose |
|---------|---------|---------|
| `status` | `handle_message` | Runtime stats summary |
| `spawn <task>` / `bg <task>` | `_handle_spawn` | Run subagent (blocking / async) |
| `spawn list` / `spawn status` | `_handle_spawn` | List active subagents |
| `cron list` | `_handle_cron` | List cron jobs |
| `cron remove <id>` | `_handle_cron` | Remove a cron job |
| `cron pause <id>` | `_handle_cron` | Pause a cron job |
| `cron resume <id>` | `_handle_cron` | Resume a paused cron job |
| `task run <path>` | `_handle_task_run` | Start autonomous task runner |
| `run status` | `_handle_task_run` | Check task runner status |

### Channel Monitoring
### Channel Monitoring
- Config: `config.json → slack.tracking_channels` — list of channel IDs to watch
- Event: `member_joined_channel` — fires when a user joins a channel the bot is in
- Requires `channels:read` scope (for public channels) and `groups:read` (for private)
- When a user joins a monitored channel, `prompt_allowlist()` sends Allow/Deny to the owner
- Users already on the allowlist are silently skipped
- If `tracking_channels` is empty, no monitoring occurs
- Tracked channels are capability-probed (`slack/scope_probe.py`, one `conversations.history` call with `limit=1`) after the socket connects at startup and whenever a channel is added to tracking. A `missing_scope`/`channel_not_found` result logs a warning and pushes a dashboard notification — a private channel tracked under an install predating `groups:history` would otherwise fail silently. Deferred (`asyncio.create_task`), best-effort: transient network errors report nothing
- `/<command> @user` still works as a manual trigger (command name configurable via `slack.command` in config, default: `kirocrew`)
- `/<command> #channel` adds a tracking channel via owner approval

## File Attachment Processing

Slack `file_share` messages are processed in `_route_message()` after dedup + auth. Three categories handled in order:

### Voice / Audio (`transcribe.py`)
- **Mimetypes**: `audio/*`, `video/webm`
- **Flow**: Download via `SlackClientOps.download_file()` → `transcribe.transcribe_audio()` → transcription text prepended as `[Voice memo transcription]...[End of transcription]`
- **Config**: Enabled by default (`stt.enabled = true`). `stt.provider` decides where recognition runs, and the default `local` runs it in this process on a resident whisper.cpp model, so a memo costs one model download (`stt.model`, `base` by default) and nothing after that. A stored retired provider degrades to `local`; there is no binary to put on `PATH`. Availability per provider comes from `transcribe.availability_detail()`, which distinguishes a missing `voice` extra from a platform with no prebuilt recognizer and from a macOS too old for the `apple` provider, because those need different fixes. The pinned `imageio-ffmpeg` wheel decodes the memo's ogg/Opus or webm internally and is bundled in desktop releases; users do not install system FFmpeg. Setup: [configuration](../../../src/kiro_crew/docs/configuration.md) § Speech-to-text.
- **Provider-independent guards**: `transcribe_audio` refuses a sensitive `audio_path` and redacts every provider's output before returning, both before/after dispatch rather than inside a branch, so a provider cannot be added that skips either. See [stt-streaming](../features/stt-streaming.md).
- **Security**: Transcription output run through `redact_credentials()` + `redact_exfiltration_urls()` before injection. Audio file suffix sanitized to alphanumeric only. `_transcribe_audio_files` records a `slack.download_file` and a transcription SEL entry per memo.

### Images (`files.py`)
- **Mimetypes**: `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `image/bmp` (aligned with `AcpClient._send_prompt()` regex)
- **Size limit**: 10 MB (checked from Slack metadata before download)
- **Flow**: Download to temp file → inject local path into message text → `_send_prompt()` detects path, base64-encodes, sends as `{"type": "image"}` content block to kiro-cli
- **Temp lifecycle**: Caller (`_route_message`) owns cleanup. Done callback on `handle_message` task cleans up after `_send_prompt()` reads the file. Early-return paths and `create_task` failures also clean up. Queued messages carry their paths in the entry's `image_temp_paths` kwargs; `_dispatch_queued` unlinks after the turn consumes them, and the queue-discard paths — `cancel_queued`, `clear_queue`, `dequeue`'s cancelled-skip, and the `_pending_queue` drops in `_handle_message_deleted` and the `!stop` handler — unlink via `session.unlink_queued_temp_paths()` so entries that never dispatch don't leak files. Known gap: session-teardown paths (restart/remove/destroy/idle sweep) drop `session.queue` without unlinking.
- **Unsupported image types** (`image/svg+xml`, `image/tiff`, etc.) fall through to unsupported handler — metadata note only, no download

### Text / Code Files (`files.py`)
- **Mimetypes**: `text/*`, `application/json`, `application/xml`, `application/javascript`
- **Size limit**: 512 KB download cap, 50 KB injection cap (truncated with `[… truncated]` marker)
- **Flow**: Download to temp → read with `errors="replace"` → redact credentials/URLs → inject as `[File: name]\ncontent\n[End of file]`
- **Temp lifecycle**: Always cleaned in `finally` block (text content is read into memory, file not needed after)

### Unsupported Types
- All other mimetypes: no download, inject `[Attached file: name (mimetype, size) — unsupported type]` metadata note
- SEL audit logged with `operation="slack.file_skip"`

### Safety Controls
- Mimetype allowlist — only known-safe types processed
- File size checked from Slack metadata *before* download
- Filetype suffix sanitized to alphanumeric only (prevents path traversal)
- `tempfile.mkstemp()` for all downloads — never uses original Slack filename
- `redact_credentials()` + `redact_exfiltration_urls()` on all text content
- SEL audit on every download, skip, and error

## Streaming UX

- Response streams in real-time via progressive Slack message edits
- Edit throttled to ~1/sec to avoid Slack rate limits (Tier 3: ~50 req/min)
- Cursor indicator (▍) shown during streaming, removed on completion
- Tool calls shown inline as 🔧 _tool name_
- **Thinking/reasoning content** filtered from the main response — accumulated separately and posted as a 💭 thread reply after the main message. Inline `<thinking>` / `</thinking>` tags are also stripped as a safety net. The thread reply is suppressed when `slack.show_thinking` is `false` (default `true`).
- Final message split into multiple posts if over 3900 chars (via `split_message()`)
- **Credential-redaction notice** — when the delivered text (answer or thinking) still carries a `security.CREDENTIAL_REDACTION_TAGS` placeholder, one `messaging.renderer.credential_redaction_notice` message is posted in the thread after the answer is committed, so the reader knows a command they copy will not run as pasted. Redaction is NOT relaxed — Slack is an egress path. Counted from the tag in the sent text rather than the redactor's warnings list, which is empty on the streaming path because each chunk was already redacted upstream. **One notice per turn**: answer and thinking share a single tally. Approving a review-mode draft (`interactions.py`) posts the same notice for the same reason, since that publishes to the whole channel. Both posts are best-effort — a failed notice must never turn a delivered answer into a failed turn

## Message Queue (`session.py` + `events.py`)

When a message arrives while a session is actively processing, it's queued instead of spawning a competing session:

- **Session-level queue**: `enqueue()` / `dequeue()` on `SessionManager` using a per-session `deque` + cancelled set
- **Orchestrator-level queue**: `_pending_queue` dict for the startup race (task running but session object not yet created)
- **⏳ reaction**: added to queued messages so the user sees visual feedback
- **FIFO drain**: `_on_done` callback drains both queue levels after each handler completes
- **Cancellation**: `message_deleted` event removes queued messages or marks in-flight messages as cancelled; first `!stop` press clears the queue (via `stop_turn` which calls `clear_queue` unconditionally)
- **`is_cancelled()` check**: handler checks before responding and before the LLM call to suppress responses for deleted messages

## Linked Thread Sync (`handler.py` + `interactions.py`)

Bidirectional message mirroring between dashboard chat sessions and Slack threads:

- **Slack → Dashboard**: `handle_message()` checks `_slack_to_slot` reverse lookup; if linked, routes message to dashboard slot's `_run_chat()` queue
- **Dashboard → Slack**: `_run_chat()` mirrors user messages and agent responses to the linked thread via `start_stream()` / `append_task()` / `stop_stream()`
- **Link to Dashboard button**: `LINK_DASHBOARD_ACTION` in timing footer imports thread history into a new dashboard slot
- **`!link-to-dashboard` command**: same as button but triggered via bang command inside a thread
- **Session resume**: shows Thread/DM choice buttons; `_handle_resume_choice()` with per-session lock for idempotency
- **Fresh-anchor title** (`dashboard/chat_slack.py` slack-link endpoint): the new-thread anchor message title uses the fallback chain slot.title → first-prompt snippet (60 chars, whitespace-collapsed) → `"New session"` — the raw slot key is never user-visible (untitled slots default their title to the key, so the endpoint gates on `display_title != NEW_SESSION_TITLE`)

## Sessions View (`sessions_view.py`)

Shared data-collection and Block Kit rendering for recent sessions, used by three surfaces:

- **`/<command> sessions` slash command** — `_handle_sessions` in `events.py`
- **`sessions` keyword in DMs** — `_handle_sessions_command` in `handler.py`
- **App Home Tab** — 🧵 Sessions section in `_publish_home_tab` (split into "Main chat" and "Autopilot / task runner" sub-lists)

The collector and renderer live in `kiro_crew/slack/sessions_view.py` so both `events.py` and `handler.py` can import them at module top-level without forming a circular import. `sessions_view.py` depends only on `kiro_crew.slack.blocks` and `kiro_crew.security` — it knows nothing about `events` or `handler`, which is what keeps the import graph acyclic.

All three surfaces call `await _collect_recent_sessions_off_loop(sessions, *, limit, kind)` — the required entry point for async callers, which runs the synchronous collector `_collect_recent_sessions` in a worker thread via `asyncio.to_thread` — to read JSONL files under `~/.kiro/crew/sessions/`, classify them as `dashboard` (main chat slots), `taskrunner` (autopilot/task runner steps), or `other`, and `_build_sessions_blocks(rows, *, for_home_tab=False)` to render them. The sync collector does unbounded-size transcript reads and is worker-thread-only: never call it directly from an `async def`. It pre-scans the directory (kind from the filename stem, mtime from `stat`) and reads only the newest `limit` matching transcripts.

The slash command and keyword (which post via `chat.postMessage`) use the shared `blocks.session_task_card` builder. The Home Tab calls with `for_home_tab=True` and uses `section` blocks instead — Slack's `views.publish` API rejects `task_card` with `unsupported type: task_card`. Both paths keep the canonical `mc_session_resume_{key}` action ID handled by `interactions.py:_handle_session_resume`.

The Home Tab requests up to `_HOME_TAB_SESSIONS_PER_KIND = 5` rows per kind so both surfaces stay well under Slack's 100-block view limit. The slash command and keyword each request `_SESSIONS_DEFAULT_LIMIT = 10` rows.

**At most `_HOME_TAB_COLLECT_CONCURRENCY` Home Tab collections run at once.** Every `app_home_opened` from an allowed user schedules its own publish with no dedupe, and each collection reads up to `limit` transcripts on the process-wide default executor — shared with history appends, cron store writes and session storage. Ungated, a burst of tab opens fills that executor with multi-MB reads and unrelated `asyncio.to_thread` callers queue behind them. The gate wraps only the collection; the Slack API calls around it stay unserialized. It is created lazily rather than at import, because a module-level `asyncio.Semaphore` binds to whichever loop is current when the module loads and the gateway's loop does not exist yet.

Each surface emits a SEL audit event for the data-access via `sel.log_api_access`:

- Slash command: `slack.sessions_slash_data_access` (caller = Slack user id)
- Keyword: `slack.sessions_data_access` (caller = session key)
- Home Tab: `slack.home_tab_sessions_data_access` (caller = Slack user id)

Sharing the builder also means the `sessions` keyword now displays the same 🟢 active / ⚫ inactive marker as the slash command. Previously the keyword path rendered every card as inactive regardless of session state.

## `!compact` Command (`handler.py`)

Triggers in-place ACP `/compact` on the current thread's session:

1. Adds ♻️ reaction, posts "Compacting context…"
2. Streams `/compact` command, waits for `compaction_status` event
3. Falls back to `wait_for_compaction()` (shared `COMPACT_WAIT_TIMEOUT_SECS` budget) if no inline status
4. Posts result (✅/❌) + timing footer
5. On failure: `sessions.discard_conversation(session_key)` — kills the session and drops only the resume sid, so the next message cold-starts. The session-map ENTRY survives, keeping the thread↔session linkage `get_session_for_thread` routes later replies through; `destroy` here would fork the thread into a fresh session with none of its context. Housekeeping never removes a channel identity (see [session](session.md))

## Wedged-Session Recovery (`AcpPromptBusy`)

When kiro-cli reports a prompt is still in flight ("already in progress" — a tool stall, timeout, or message race), `AcpClient` raises `AcpPromptBusy` (`acp/client.py`) with a friendly "I'm still processing a previous request… it clears on its own once the stale turn expires" message. `handle_message` catches it and auto-resets the wedged session via `sessions.reset(session_key)` so the next message cold-starts cleanly, then records the failure (the reset itself is best-effort — a reset failure is logged, not raised). The message deliberately names no command: the auto-reset above is what recovers the session, so the text has nothing to ask the user for (it used to say `!restart`, which is Slack-only, owner-gated, and restarts the gateway rather than the session -- see `common/error-handling.md`).

## OPTIONS Buttons (`format.py`)

LLM responses ending with `[OPTIONS: choice1 | choice2 | choice3]` are rendered as interactive Block Kit checkboxes with a Send button:

1. `extract_options()` parses the `[OPTIONS: ...]` tag from the response text
2. Tag is stripped from the displayed message
3. `build_options_blocks()` creates Block Kit checkboxes (max 10) + primary Send button
4. Checkboxes posted as a follow-up message in the thread
5. Send click → `_handle_options_submit()` → reads checkbox state → posts styled selection → routes combined selection to handler
6. Legacy single-choice buttons still supported via `OPTIONS_ACTION_PREFIX`

Action IDs: `options_checkboxes` (toggle), `options_submit` (send). Checkbox `value` contains the choice text.

Beyond the reply-finalization path in `handler.py`, two other Slack delivery paths also render `[OPTIONS: ...]` as buttons: the dashboard `send_message` MCP tool (`api_send_message` in `dashboard/handlers/messaging.py`) and cron subagent delivery (`_deliver_cron_response` in `gateway.py`). Both call `extract_options()` / `build_options_blocks()`, skip the tag parse when the caller supplies explicit `blocks` (those own their own layout), and wrap the follow-up options post in `try/except` so a failed options post never fails the primary message.

### The marker family, and the streaming suppressor

Three protocol markers share one grammar, defined once in `constants.py` and iterated via `MARKER_PREFIXES` rather than spelled as a literal at each site: `[OPTIONS: ...]` (content choices, rendered as checkboxes), `[OPTION-ACTIONS: close=<label>]` (a LOCAL dashboard UI action — Slack renders no button for it and strips it, because a click here could not be honoured), and `[STEERING ...]`. `"[OPTIONS"` is not a prefix of `"[OPTION-ACTIONS"` (they diverge at `S` vs `-`), so any scan naming only the first head misses the second silently.

Two grammar properties differ from a naive reading and are pinned by tests:

- **Casing is decided PER HEAD.** The action patterns carry `re.IGNORECASE`, so `[option-actions: ...]` IS a live marker that the parser strips; `[OPTIONS:` is deliberately case-SENSITIVE, a pre-existing divergence from the frontend that is recorded rather than widened. Every head scan therefore goes through `constants.rfind_marker_head` / `starts_with_marker_head`, never a bare `rfind`/`startswith`: a case-sensitive scan missed a mixed-case fragment that the parser would later strip, so the stream sealed raw protocol text into a message Slack cannot edit.
- **The live-stream hold settles at end of line, not at the first `]`.** `settle_marker_hold` keeps a bracket run until the line ends, so a `]` inside a choice label no longer releases a partial marker as visible text. `_is_marker_candidate` decides what is still held, cased per head so the stream holds exactly the heads the batch parser will strip — holding more is not harmless caution: the final message comes from the case-sensitive `extract_options`, so an over-held run reappears as a visible pop-in of raw protocol text.

## Messaging Transport (`messaging.use_transport`)

A channel-neutral dispatch path that replaces the native `handle_message` stream loop with a shared `SlackTransport → TurnDriver → SlackRenderer` pipeline. Gated by `messaging.use_transport` (`MessagingConfig`, default `True` in KiroCrew — the transport abstraction is the canonical path; set `false` to fall back to the legacy native handler — `config/loader.py`). When the flag is on, `events.py:_route_message` routes the message to `handle_message_transport`; when off, nothing in the live gateway path imports the transport (it is purely additive).

- **`SlackTransport`** (`slack/transport.py`): wraps `SlackClientOps` in the neutral `MessagingTransport` contract (dependency direction `slack → messaging`; the `messaging` package never imports Slack). `authorize()` is **owner-only, deny-by-default** — an empty allow-list authorizes nobody, and it SEL-audits **every** rejection (`operation="slack_transport.authorize"`, `outcome="denied"`), including empty/missing `user_id`, so the deny-by-default control is observable.
- **`TurnDriver`** (`messaging/driver.py`): channel-neutral turn loop converting provider `AcpEvent`s into abstract `OutputEvent`s. Approval ladder mirrors the native `APPROVAL_*` contract — `APPROVAL_AUTO` / `APPROVAL_TRUST` (approve all), `APPROVAL_TRUST_READS` (approve `tool_kind == "read"`), `APPROVAL_INTERACTIVE` (deny-by-default unless the injected decider approves). Two injected predicates keep the driver channel-neutral: `auto_approve_tool` (the `spawn_run` / `auto_approve_subagent_spawn` hook predicate) and `auto_approve_session` (per-session Trust). Interactive buttons are rendered only when a decider is present — without one, `_approve()` denies by default so posting buttons would leave dead controls.
- **`SlackRenderer` + `SlackApprovalDecider`** (`slack/renderer.py`): renders abstract output onto a Slack thread and holds the underlying `SlackClientOps` so the dashboard→Slack mirror keeps working. Approval buttons use `mc_tool_approve_` / `mc_tool_trust_` (per-session Trust) / `mc_tool_deny_` action prefixes. `SlackApprovalDecider` maintains a process-global `_REGISTRY` keyed by request id so the module-level interaction handler can `resolve_global()` a click without a direct reference to the per-turn decider; `session_for()` maps a click back to its session for per-session Trust. The decider is **deny-by-default** — it `wait_for`s the button future and returns `False` on timeout.
- **`handle_message_transport`** (`slack/transport_dispatch.py`): agent resolution order is thread override (`!agent`) → per-channel override (`slack.channels.<id>.agent`) → configured default → canonical `"kirocrew"` (`_DEFAULT_KIROCREW_AGENT`). The final fallback matters: without it an empty `agent.default_agent` makes kiro-cli launch its bare built-in default with no `kirocrew-core` server, so `spawn_run` would be missing. Fires the ack reaction + working status before the (cold-start) session acquisition, matching native ordering.
- **`_resolve_approval_mode(orch)`** (`events.py`): the single per-message chokepoint that folds runtime YOLO (owner-toggled `/kirocrew yolo`, TTL-capped `safety_override`) into `APPROVAL_AUTO`, evaluated fresh each message. The transport `TurnDriver` only sees this resolved mode, so both the native and transport paths honor the runtime toggle consistently rather than an unconditional auto-approve. Deny-by-default unless auto-approve is explicitly active.

## Tool Approval Flow

1. ACP sends `permission_request` event during streaming
2. `events.py:_resolve_approval_mode()` evaluates runtime YOLO, then the CLI `--approval` override, then `agent.approval_mode`; only an explicit auto policy yields `APPROVAL_AUTO`, otherwise it yields `APPROVAL_INTERACTIVE`. Native and transport dispatch both use this chokepoint, preventing an operator policy from being silently bypassed.
3. Handler posts Block Kit message with ✅ Approve / 🤝 Trust / 🚀 YOLO / 🚫 Reject buttons
4. `events.py` routes `interactive` Socket Mode event to `interactions.dispatch()`
5. Approval/rejection sent to ACP, streaming resumes or stops
6. Approval button message replaced with outcome text
7. 120s timeout — auto-rejects if no click

## Session Management

See `session.py` module spec. Each Slack thread_ts maps to a separate AcpClient instance with idle timeout cleanup.

### Message Queue

Messages arriving while a session is busy are queued with ⏳ reaction and drained FIFO after each handler completes. See [Message Queue](#message-queue-sessionpy--eventspy) above.

### Startup

`start_pool()` creates the background session for cron/heartbeat. Chat sessions cold-start on first message — no warm pool, no MCP reset hack.

## Subagent & Cron Acknowledgment

Subagent completion and cron execution results post to both dashboard (WebSocket) and Slack (DM with ack button). Shared `ack_button()` helper in `interactions.py` handles button replacement:

1. Try `response_url` first (instant, works for 30 min)
2. Fallback: `chat.update` via Slack API (works indefinitely)
3. Section text truncated to 2990 chars (Slack's 3000 char limit)

Bidirectional sync: Slack ack → resolves dashboard approval future + broadcasts `notification_ack` WS event. Dashboard ack → resolves Slack pending future.

### Subagent Slack Replies

When a subagent with a Slack parent session completes, the synthesized LLM response is posted to the owner's DM thread. Long replies are split into multiple messages using `_split_message()` from `handler.py` (3900 chars per chunk, split on newline boundaries), matching the behavior of final chat messages.

A parent session born on any other channel (Telegram, Discord, `unified:` DM buckets, …) delivers the same synthesized reply through the governed cross-surface transport ladder instead (`_deliver_channel_reply` in `gateway.py`): the conversation is resolved via origin link (recorded by Discord's inbound dispatch) → non-Slack mirror link (e.g. a Telegram `/link` binding) → for direct (1:1) sessions only, the stored `"{namespace}:{user_id}"` channel value resolved through `transport.resolve_configured_target`; the target is vetted by `_resolve_channel_target` (SEL-audited, fail-closed, capability-gated on `supports_proactive_send`), then redacted and chunked to the transport's `max_message_chars`. Delivery is best-effort and fail-closed on ambiguity — group/forum sessions without an origin or mirror link, dispatchers that record neither, and denied egress all degrade to the dashboard notification (never a cross-conversation send), and the injected ACP turn still keeps the parent session aware of the result.

## Tool Approval via Slack

### Structured monitor completion adapters

The AutoNudge router keeps its historical `on_fire -> bool`, `cycle_count`,
`fired`, and rearm contracts. A separate runtime-only hook is supplied only when
a structured monitor already has an actionable fingerprint marked in-flight;
legacy loops and ordinary channel messages receive none. `MonitorController`
runs the typed GitHub probe off the event loop, persists the decision and
in-flight claim, and calls the Slack/Discord or dashboard adapter only for
`WAKE_ACTIONABLE`. The adapter receives the already formatted envelope and does
not add the legacy cycle tag. Every non-actionable, retry, and terminal decision
dispatches zero turns.

Slack's structured inline nudge runs through `TurnDriver` with the shared,
session-bound directive consumer. Genuine core-MCP `monitor_update`,
`monitor_stop`, and structured `autonudge_stop` tool results therefore mutate
the authoritative Slack monitor before any later raw completion; forged or
sub-agent results retain the driver's fail-closed behavior. Legacy nudges keep
their collector path. Both paths consume `provider_last_turn_usage(client)`
exactly once. That one `TurnUsage` object is fanned out to the existing usage-row
writer and, when the stream observed safe completion evidence, the monitor hook.
ACP-synthesized terminals are excluded. Because stale-stream synthesis reuses
`end_turn`, that reason remains uncharged until ACP events expose provenance;
other safe reasons determine cancellation or failure.
Stream exhaustion and timeout before that event still write the existing usage
row but do not report monitor completion or charge the monitor budget. Callback
or usage-row persistence failure does not change the Slack delivery result. A
structured stream that started reports `DISPATCHED` even if it exhausts or raises
before `EVENT_COMPLETE`; the controller's persisted evidence deadline resolves
the missing callback. Legacy callers retain their historical boolean result.

Discord synthetic nudge injection passes the same hook through
`DiscordDispatcher` to `TurnDriver`. Only a safe `EVENT_COMPLETE` reason reports
completion; a command return, dispatch exception, or renderer
`close()` is not completion evidence. Thus dashboard, Slack, and Discord all
reach the same typed controller callback even though their transport lifecycles
remain different. A queued dashboard turn revalidates its claim after background
admission and before entering `_run_chat`, so a stopped monitor cannot run
prompt-submit hooks; `_run_chat` revalidates again immediately before provider
entry to cover revocation during turn setup. Their pre-completion delivery
contract is also shared:
`DISPATCHED`, `BUSY`, or `UNAVAILABLE`; BUSY is an ordinary durable retry of the
same claimed wake, while only UNAVAILABLE terminates the monitor.

Background task approvals (subagent, cron, task runner, and AutoNudge) post approval buttons to Slack DM via `_interactive_approval()`, racing with dashboard approval:

1. Posts ✅ Approve / 🚫 Reject buttons to owner DM
2. Creates `_PendingApproval` entry for interactive handler
3. Dashboard callback resolves Slack future on dashboard approve
4. Slack button click resolves dashboard future
5. `handle_interaction()` guards against None provider and double-set on futures

### Background Deny-Fast (Unattended Sources)

`_interactive_approval(source)` is used by both interactive UI/slack and
**unattended** background sources. For background sources there is no human
responder, so waiting the interactive approval window on every approval would
stall cron, heartbeat, task-runner, or AutoNudge turns.

- `_BACKGROUND_APPROVAL_SOURCES = {"cron", "heartbeat", "taskrunner", "autonudge", ""}` (module
  constant in `gateway.py`). `is_background = source in _BACKGROUND_APPROVAL_SOURCES`.
- `subagent` is **NOT** background: subagent approvals route to the dashboard
  where the spawning human is present (via the parent slot), so they keep the long
  interactive window.
- When `is_background`, both the Slack `wait_for(pending.future, ...)` and
  `DashboardState.request_approval(..., is_background=True)` use
  `DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS` and then **deny** on expiry —
  letting the turn proceed/fail rather than hang. `test/test_dashboard_approval.py::TestBackgroundApprovalDenyFast` pins the bounded background window and the unchanged interactive window.
- The Slack and dashboard windows reference `DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS`
  / `DashboardState._APPROVAL_TIMEOUT` as the single source of truth.

### Heartbeat Tool Allowlist (`HEARTBEAT_SAFE_TOOLS`)

Heartbeat sessions run unattended and cannot prompt a human for tool approval. `_is_heartbeat_safe_tool(event_title)` checks whether a tool is safe to auto-approve using a strict **exact-match** against the `HEARTBEAT_SAFE_TOOLS` frozenset — no verb/heuristic fallback (deny-by-default, per security-controls).

**Title normalization** (applied before the set lookup):

1. Strip leading status prefix (`Running: `) via `_HEARTBEAT_STATUS_PREFIXES`.
2. Strip ACP `mcp__<server>__<Tool>` prefix.
3. Strip runtime `@<server>/<Tool>` prefix (kiro-cli titles arrive as `Running: @internal-mcp/ReadInternalWebsites`).

Only the **bare tool name** (e.g. `ReadInternalWebsites`) is tested against the frozenset. Unknown tools are denied and a SEL audit event (`outcome: denied`, `reason: not_in_heartbeat_safe_tools`) is emitted so operators can tune the list. SEL failure on the approve path fails closed (denies the tool).

## Dashboard Token Authentication

### `!dashboard [duration]` Command (deprecated → `/kirocrew dashboard`)

Owner command in `handler.py` that generates a time-limited token URL for dashboard access:

1. Parses optional duration argument via `parse_duration()` — accepts `<N>h` or `<N>m` format (default: `1h`)
2. On invalid duration, replies with usage message
3. Calls `generate_token(user_id, ttl)` to create an HMAC-SHA256 signed token
4. Constructs URL using configured host from `dashboard.url`, or machine hostname for remote access, or `localhost` for local-only
5. Logs via SEL with `operation='slack.dashboard_token'`
6. Posts the URL as an ephemeral-style message in the Slack thread

### Token Auth Middleware

`token_auth_middleware(local_only)` in `token_auth.py` — aiohttp middleware in the explicit middleware chain:

- **Auth required when**: not local-only (i.e. bound to all interfaces)
- **Loopback trusted when**: local-only mode (SSH tunnel access)
- **Bypassed for**: static assets (`/assets/`, `/static/`, `/logo.png`, `/manifest.json`, `/sw.js`, `/icon-*.png`)
- **Token sources**: `?token=` query param (first use) or `mc_token_{port}` cookie (subsequent requests)
- **First query-param use**: binds token to client IP, marks consumed, sets `HttpOnly; SameSite=Strict; Path=/` cookie
- **Cookie use**: validates token + IP binding, allows repeated access
- **Rejection**: returns 403 HTML page with instructions to run `/kirocrew dashboard` in Slack; API paths get JSON error

Token format: `base64url(payload).base64url(HMAC-SHA256-signature)` with per-process secret (`os.urandom(32)`).

### Dashboard URL Config

Single `dashboard.url` field on `KiroCrewConfig` (default: `""`), loaded from `config.json → dashboard.url`.

`is_local_only(dashboard_host, slack_connected)` determines the mode:
- No Slack → local-only (no auth layer)
- Loopback host → local-only
- Non-loopback host → all interfaces, token auth required

```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```
- `"auto"` + Slack + remote host → `"0.0.0.0"`
- `"auto"` + Slack + localhost → `"127.0.0.1"`

### Tunnel URL in Slack Links (`slack.use_tunnel_url`)

`SlackConfig.use_tunnel_url` (bool, default `False`) gates whether the AEA
tunnel URL is used when building dashboard links posted to Slack:

- `false` (default) — `send_dashboard_link()` ignores any active tunnel and
  builds links from `dashboard.url` (if set) or the resolved host:port.
  Disabled by default until the tunnel mechanism is scaled for general use.
- `true` — `send_dashboard_link()` prefers `get_tunnel_url()` when a tunnel is
  active, falling back to `dashboard.url`/host:port when the tunnel is down.

The setting is independent of `tunnel.enabled` (which controls whether the
tunnel itself runs). A user may run a tunnel for direct browser access while
keeping Slack links pointed at the local origin.

`--no-tunnel` overrides it. When `use_tunnel_url` is on, the box is
localhost-only and no tunnel is live, `send_dashboard_link()` offers a composed
edition an on-demand provisioning seam (`current_context().tunnel
.ensure_available()`) — a second door out that bypasses `setup_tunnel` entirely,
provisioning straight on the provider without ever constructing a
`TunnelManager`. On a process booted with `--no-tunnel` that seam is not reached
at all (`tunnel.publish_disabled()`), the refusal is SEL-audited as
`tunnel.provision_denied` / `no_tunnel_boot_flag` — the same control as the boot
refusal, so neither door's denials are missing from the trail — and the link is
composed from the local origin instead. The DM also carries a line naming
`--no-tunnel` and the `ssh -L` form: every other route to a local link can still
become reachable (the edition seam re-issues once its tunnel connects), but this
one never will, so without it the requester taps a link that times out every time
with the explanation only in the log. Without that check the flag would be a
promise the product does not keep: an instance that refused to publish at boot
would publish the first time anyone asked for a dashboard link.

**Slack connect is non-fatal** (`GatewayOrchestrator._connect_slack`): the
initial socket-mode `connect()` is wrapped so a network/proxy/timeout failure
(e.g. a stale `HTTPS_PROXY` in the launching shell — slack_sdk's aiohttp client
honours proxy env vars via `trust_env`) logs a warning and the gateway
continues in **dashboard-only mode** instead of crashing the whole process.
Only ordinary `Exception`s are swallowed; `CancelledError` (BaseException)
still propagates so real task cancellation is not masked. There is no
background retry of the initial connect — Slack DM stays disabled until the
next gateway restart. The "connected to Slack" banner prints only after a
confirmed connect.

Config example (remote access via URL):
```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```

## Security

- Owner-locked via `KIROCREW_OWNER_ID` in `.env` (supports W/U prefix cross-matching)
- **Enterprise Grid validation** (`slack/enterprise.py`): Two-layer defence against data exfiltration to personal/external Slack workspaces:
  1. **Startup gate**: `validate_enterprise()` calls `auth.test` with the bot token, verifies `enterprise_id` matches the configured production (`E0123ABC456`) or sandbox (`E0456DEF789`) grid. Caches `team_id` and `enterprise_id` in memory. Clears cache before each validation attempt so re-validation failures are fail-closed. Gateway refuses to connect if validation fails.
  2. **Per-message gate**: `check_message_origin()` compares each incoming event's `team` field against the cached `team_id`. Catches `.env` hot-swap while running. Zero-cost in-memory string comparison, no API call. Deny-by-default: empty `team` field is rejected.
  - Configurable extra enterprise IDs via `slack.allowed_enterprise_ids` in config.json (for additional subsidiary grids)
  - **Corrupt-config fail-closed**: `KiroCrewConfig.load()` degrades a torn/corrupt `config.json` (or `config.local.json` overlay) to a defaults object rather than raising, so `slack.allowed_enterprise_ids` would come back empty. `_load_allowed_team_ids()` positively detects that degraded read (a config file that exists on disk but does not parse) and fails CLOSED -- the allowlist stays enforced and admits NO origin (not even the just-validated workspace, which would answer the allowlist's own question) so startup is refused, and the degradation is SEL-audited (`operation=slack.allowed_team_ids_load`, `error=config_load_degraded_fail_closed`) -- instead of silently reverting to default-open. A genuinely unconfigured allowlist (no config file, or a clean file listing none) stays default-open.
  - **One reader owns the allowlist**: the admitted set comes only from that validated read of `slack.allowed_enterprise_ids`. Caller-supplied `extra_ids` -- the caller's own earlier `KiroCrewConfig.load()` snapshot of the same key -- does not contribute to it. The validated read is never older than the snapshot, so ids the snapshot holds and the read does not are ids the operator REMOVED, and unioning them would undo the removal. Consequence in both directions: removing one id takes effect at validation, and emptying the list returns to default-open, matching what a restart does. `extra_ids` does not contribute on the `auth.test`-failure path either, so the validated read is the sole source on every path: that path decides fail-open vs fail-closed by asking whether a restriction is configured, and counting an older snapshot there would manufacture a restriction the file does not list. An UNREADABLE config still refuses there -- a config that cannot be honoured is not one that honestly lists no restriction -- and a configured allowlist still fails closed.
  - All validation outcomes logged to SEL (`operation=slack.enterprise_validation`)
  - `kirocrew doctor` includes workspace validation check
- **Deny-by-default**: if `KIROCREW_OWNER_ID` is unset or empty, Slack is disabled entirely at startup (`init_socket_mode` refuses to connect). The access check in `_route_message` also rejects all messages when owner ID is missing, as a secondary guard.
- **Interactive payload access check**: `interactions.dispatch()` uses deny-by-default — rejects unless the clicking user is positively confirmed as allowed. Non-allowed users receive an ephemeral message ("⛔ You are not authorized to use these buttons.") and the original buttons remain intact for the owner to click later.
- Dedup cache (`SeenCache`) prevents processing duplicate Slack events
- Bot self-message filtering via `bot_id` check
- **Trusted bot IDs** (`slack.trusted_bot_ids` in config): allows specific bot IDs to bypass the blanket `bot_id` filter, enabling multi-node mesh communication. Empty list = all bot messages dropped (default), and a bot id NOT in the list is denied exactly as with no list (fail-closed, `error=untrusted_bot`). Admission requires a positive `bot_id` match against the allowlist; the match sets `from_trusted_bot`, which lets the `bot_id` stand in as `sender_id` and grants access equivalent to an allowed user — authorization is explicit via the `trusted_bot_ids` config allowlist, not the `slack_allowed_users` list. All trusted-bot permission decisions emit SEL audit events (allowed decisions carry `resources="trusted_bot"` so the decision basis is traceable). Echo protection: error replies to trusted-bot messages are suppressed on both dispatch routes — the native path (`from_trusted_bot` in `handle_message`) and the default transport path (`from_trusted_bot` in `handle_message_transport`, threaded through the immediate call, both session queues, and `_dispatch_queued`; the error message is suppressed but the thread status is still cleared). Successful-reply loops are bounded by the **per-thread turn cap** (`slack.trusted_bot_turn_limit`, default 5, minimum 1): a thread that has run that many consecutive trusted-bot turns admits no more (`error=trusted_bot_turn_limit_reached`) until an allowed human posts in it, which resets the count — without the cap, two mutually trusted gateways would admit each other's replies as fresh turns indefinitely. Only a message that actually dispatches a turn moves the count (Slack retries, message/app_mention duplicate pairs, and activation-dropped messages do not). Review-mode channels deny trusted bots outright (`error=trusted_bot_denied_in_review_channel`): the review draft flow delivers via an ephemeral to the sender, which requires a human user id. The gateway's own bot id (cached from the startup `auth.test` that enterprise validation already performs) is never trusted even when listed (`error=own_bot_id_never_trusted`) — otherwise every reply would re-enter the handler as fresh input, a self-reply loop; when `auth.test` was unavailable the self identity is unverified and the admission FAILS CLOSED, trusting nobody (`error=trusted_bot_requires_verified_self_id`) — the same posture enterprise validation takes for a configured allowlist with unverifiable workspace identity. The (unwired) `SlackTransport.receive` inbound path applies the same admission — positive allow-list match via its `trusted_bot_ids` constructor param, own-bot exclusion, fail-closed unverified self id, audited decisions, trust before the subtype filter — so the two Slack inbound paths agree about which peer bots are admissible.
- Socket Mode — no public URL exposed
- Credentials stored in `~/.kiro/crew/.env` with `chmod 600`

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `slack_sdk` | >= 3.0 | Socket Mode + Web API |
| `aiohttp` | — | Dashboard HTTP server |
| `websockets` | — | Socket Mode transport |
| `croniter` | — | Cron expression matching |
| `snowballstemmer` | — | Snowball stemming for semantic KV keyword scoring |
| `pysqlite3-binary` | — | FTS5/UPSERT compat on AL2 (Linux only) |

### Secretary Service (`secretary.py`)

Background Slack inbox manager initialized via `_init_secretary()` if `secretary.enabled` is true:

- **Polling**: discovers unread channels via `slack_unreads.mjs`, fetches messages + thread replies
- **Classification**: batch LLM prompt classifies messages as `needs_reply` / `fyi` / `noise`
- **Draft generation**: on-demand via `draft_reply()` with confidence tiers
- **Alerts**: keyword matching + name mention detection → dashboard notification
- **Self-healing**: reinitializes Slack client after 3 consecutive poll failures
- **WS events**: `secretary_new_item`, `secretary_item_updated`, `secretary_item_sent`

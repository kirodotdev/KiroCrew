# Self-Learning, Cron & Dashboard Modules

Last Updated: 2026-07-13 (silent-cron failure-alert suppression, _deliver_cron_response OPTIONS buttons, theme + slot-mode + folder + agents-roster endpoints, state.py display_title/branch-commit; CHAT_TURN_TIMEOUT applied uniformly to all _run_chat dispatch sites; bumped to 7200s to match ACP _DEFAULT_PROMPT_TIMEOUT)

## Overview

Phase 5 adds self-learning from corrections, scheduled cron jobs, and a web dashboard.

## Self-Learning (`learn.py`)

Detects user corrections (e.g. "use X instead of Y", "remember that X", "never use X") and stores them in `~/.kirocrew/lessons.jsonl`. Categories: `tool`, `preference`, `knowledge`. Injected into LLM context as `[Learned corrections:]` block (max 50). Detection runs after each ACP response.

### `learn_add` Session Authorization

The `learn_add` MCP tool (backed by `POST /api/lessons`) is subject to session-scope checks in `dashboard/handlers/cron.py:api_lessons_create`:

1. `X-Session-Key` header is required; missing → HTTP 400 `missing X-Session-Key`.
2. `dashboard:ui` (browser UI's static key) is always allowed.
3. Otherwise the slot name (portion after the `:` prefix, or the whole key) must satisfy at least one of:
   - Present in `state._slots` (live in-memory slot), **OR**
   - Key is in `state._restricted_keys`, **OR**
   - Key is in the Slack namespace — either it starts with `slack:`, or it is a bare Slack `thread_ts` matching `validation.SLACK_THREAD_TS_RE` (`^\d{10,}\.\d{6,}$`), **OR**
   - The corresponding JSONL file exists under `~/.kirocrew/sessions/{slot_name}.jsonl` or `~/.kirocrew/sessions/dashboard_{slot_name}.jsonl` — resolved by `_session_has_persisted_history()` in `handlers/_shared.py`.

   If none match → HTTP 400 `unknown session`.

A Slack thread keys its session off the **bare** `thread_ts` (e.g. `1781215864.487849`), set in `slack/handler.py` and frozen into the MCP subprocess's `KIROCREW_SESSION_KEY` env var; the `slack:<chan>:<ts>` form is only a `send_message` delivery target, never the session key. Recognising the bare-`thread_ts` shape is required because the session JSONL is written *after* the LLM turn completes, so the first `learn_add` in a fresh Slack thread would otherwise race the flush and fail with `unknown session` until the transcript lands on disk (then succeed minutes later). Dashboard keys are always prefixed (`dashboard:*`, `chat-N-*`), never a bare `digits.digits`, so the regex cannot widen authorization for dashboard or forged keys.

The JSONL-existence check exists because MCP subprocesses retain their original `KIROCREW_SESSION_KEY` env var for the life of the process, but the gateway's idle-sweep loop evicts in-memory slots after ~60 minutes of inactivity (see `session.py`). Without this fallback, reopened dashboard tabs would deterministically fail `learn_add` once the slot is swept, even though the user is actively engaged. Ephemeral (incognito/temporary) sessions never write to disk, so JSONL presence is a reliable positive signal of a non-ephemeral established session.

The `learn_add` tool wrapper in `mcp_core.py` maps the backend `unknown session` error into a user-actionable message that accurately reflects the post-check semantics — the error is returned only when the key matches none of the accepted forms above (live slot, restricted key, Slack namespace, or persisted JSONL), so the message tells the LLM to re-state the lesson in a new thread rather than promising an automatic retry that would cross a fresh LLM context boundary. This mapping depends on the transport helpers (`_post`/`_get`/`_delete`) decoding the structured `{"error": ...}` JSON body out of `urllib.error.HTTPError` (via `_http_error_body()`) rather than surfacing the opaque `"HTTP Error 400: Bad Request"` string — without that, the `unknown session` match never fires and the LLM sees a generic transport error. Because an HTTP response body is content originating outside KiroCrew, `_http_error_body()` redacts the decoded message with `redact_exfiltration_urls()` + `redact_credentials()` at that trust boundary before returning it, so every tool branch that echoes a transport `error` value inherits the redaction (per the `security-controls` rule: never trust output from outside KiroCrew on an external surface).

Every `learn_add` session-scope permission decision — both allows and the deny — emits a SEL audit event via `_sel().log_api_access()` with a distinct `resources` tag, so the full authorization flow is traceable in the audit log:

| Condition | Outcome | `resources` tag |
|---|---|---|
| `sk == "dashboard:ui"` | allowed | `dashboard_ui` |
| `slot_name in state._slots` | allowed | `live_slot` |
| `sk in state._restricted_keys` | allowed | `restricted_key` |
| `sk.startswith("slack:")` or `SLACK_THREAD_TS_RE` match (bare `thread_ts`) | allowed | `slack_namespace` |
| JSONL exists under `~/.kirocrew/sessions/` | allowed | `jsonl_fallback_recovery` |
| None of the above | denied | `unknown_session` |

The downstream `_is_restricted_session` check can still reject the call with HTTP 403 after the session-scope *allow* decision (e.g. for an incognito/temporary slot), emitting a separate `restricted_session_block` deny event; that is a distinct write-scope authorization, not a session-scope one.

## Cron Service (`cron.py`)

Scheduled job execution with three schedule types: `every` (interval, min 60s), `at` (one-shot timestamp), `cron` (5-field expression). Supports natural language parsing via `parse_wakeup()`.

- Persistence: `~/.kirocrew/crons.json` with atomic writes and cross-process file locking
- Mtime-based sync: auto-reloads when file modified externally (by CLI or LLM)
- Timer restore: `_load()` re-arms the timer loop for active (enabled) jobs when the service is already running, ensuring jobs resume after gateway restart
- Timer cap: `_arm_timer()` caps delay at `_TIMER_POLL_SECS` (30s) so the timer always wakes to `_sync()` and detect external file changes — fixes one-shot reminders set via Slack silently failing
- Timer loop: non-blocking execution via independent `asyncio.create_task()` per job (via `_run_job_isolated()`), with strong refs in `_running_tasks` to prevent GC. One hung ACP session no longer freezes the entire cron system. Per-job timeout increased from 5 min to 30 min (`_JOB_TIMEOUT_SECS`); zombie detection (`is_responsive()` after 10 min inactivity) provides additional defense-in-depth
- Semaphore safety: `_acquired` flag pattern in gateway.py, handler.py, chat.py, task_executor.py prevents over-release when `get_or_create()` throws
- ACP zombie detection: `_last_activity` timestamp on AcpClient, `is_responsive()` returns False after 10 minutes of inactivity
- Job execution: resets LLM session, streams response, posts to Slack/dashboard (unless `silent`). Delivery via `_deliver_cron_response` (CR-279806938): attempts configured `channel`/`thread_ts` first, falls back to owner-DM if channel delivery fails; applies boundary redaction to all output before posting. It also renders any `[OPTIONS: ...]` tags as interactive Slack buttons — `extract_options()` strips them from the text and `build_options_blocks()` posts them under a try/except guard so a Block Kit failure never blocks the text delivery (`gateway.py`)
- Session scoping: each job tagged with `session_key` from `KIROCREW_SESSION_KEY` env var; `cron_remove_all` only removes jobs matching the calling session's key (CLI/admin with no key still removes all)
- Silent mode: `CronJob.silent = True` suppresses auto-delivery of results; the agent decides when to notify the user via the `send_message` MCP tool. Silent also gates **failure** broadcasts: the failure-alert sites — the dup-failure dashboard bell, the fresh-failure dashboard bell, and the fresh-failure Slack DM (`gateway.py`) — are wrapped in `not job.silent`, so a silent cron's failure is *recorded but not broadcast*. The dedup-state advance (`last_failure_hash`/`last_failure_at`), the consecutive-failure auto-pause, the SEL `cron_failure_alert` audit, and the re-raise still fire regardless of `silent`
- Per-agent cron: jobs store optional `agent_id`; gateway passes `agent=job.agent_id` to `get_or_create()` so each job runs with its configured agent
- Handler intercepts cron commands before ACP (no LLM round-trip needed)
- CLI: `kirocrew cron {list|add|update|remove|pause|resume|trigger}`. `add` and `update` accept `--every`, `--cron`, `--channel`, `--agent`, `--approval-mode`. CLI flags mirror the corresponding `cron_add`/`cron_update` MCP tool parameters.
- Update: `CronService.update_job(job_id, **kwargs)` for partial updates (name, message, schedule, agent, channel, approval_mode, silent) with file locking and cron expression validation
- Async stop: `stop()` is now async, cancels and awaits `_running_tasks` before returning

### Per-Job Timezone

Each job stores an optional `timezone` field (IANA name, e.g. `America/Los_Angeles`). Schedule evaluation, next-run computation, and display all use the job's timezone rather than the server timezone. The `/api/crons` response includes both the per-job `timezone` and a top-level `server_tz` field so frontends can render correctly.

### Skip Dates

Jobs can define `skip_dates` — a list of dates (YYYY-MM-DD) on which the job should not fire. The executor already skipped these at runtime; `compute_next_run_ts` now also advances past skip_dates when computing the display/preview "next run" time (capped at 52 iterations). The `/api/crons` response includes `skip_dates` per job.

### Strict Schedule (`strict_schedule`)

Jobs receive random jitter by default (0-5min hourly, 0-59min daily) to spread load. Set `strict_schedule: true` on a job to disable jitter entirely — the job fires at the exact cron/interval time.

### Hide in Chat (`hide_in_chat`)

By default a persistent-session agent cron auto-creates a linked dashboard chat slot (`cron-{job_id}`) on first delivery (see Auto-inject), so its runs appear in the active session list. Set `hide_in_chat: true` to suppress that slot creation — the run's result still reaches Slack/dashboard notifications, and the run stays visible in the History tab via the **cron execution-history store** (`CronHistoryStore`, written unconditionally by the executor and surfaced at `GET /api/crons/{id}/history`), but no entry clutters the Chats sidebar. Useful for fire-and-forget jobs (daily digests, log cleanups, polling). Default `false` (preserves prior behavior; absent field reads as `false`). Orthogonal to `silent`: `silent` suppresses the push notification, `hide_in_chat` suppresses the chat slot. The flag is a no-op for `script`/`command` crons, which never create a slot. The executor gates all three `inject_cron_result_to_dashboard` call sites on `not job.hide_in_chat`; the dashboard notification's CTA falls into the pre-existing no-slot branch ("View last result", which lazily rebuilds a slot from history on click) instead of "Continue session". Note: the `cron:{job_id}` *dashboard conversation_log* is written ONLY by `inject_cron_result_to_dashboard`, so it is intentionally empty for a hidden cron — it exists solely to give a dashboard follow-up turn context, which a no-slot cron never has. Hidden-cron result persistence is the execution-history store, not `cron:{job_id}`.

### Code-Based Script Execution (`cron_script.py`)

Deterministic cron jobs that bypass the LLM entirely:

- **Script mode**: `script` field specifies a Python callable path (`~/.kirocrew/crons/file.py:function`). The function receives a `ScriptContext` with `ctx.call_tool()` (MCP tool access), `ctx.notify()` (deliver message), `ctx.message` (arguments from the `message` field). Control flow via exceptions: `raise Skip()` to silently retry next tick, `raise Done()` to complete and remove the job, `raise Report("msg")` to deliver a message and keep running.
- **Command mode**: `command` field specifies a shell command to run (mutually exclusive with `script`). Stdout captured as result.
- **Timeout**: configurable per job (default 30s for scripts, 300s for commands).
- **Safety**: scripts must live under `~/.kirocrew/crons/`. `is_sensitive_path()` blocks credential file access. SEL audit on every invocation. Auto-pause after 5 consecutive failures. Concurrent execution guard prevents double-fire.
- **Kind tag**: `cron_list` labels each job as `script`, `command`, or `agent` based on which mode is configured.

### Trigger Command

On-demand job execution via CLI (`kirocrew cron trigger <job_id>`) and MCP tool (`cron_trigger`). Delegates to `POST /api/crons/{id}/run`. The endpoint returns job name and uses `create_task` for non-blocking execution. Both CLI and MCP paths include SEL audit logging.

### Compact `cron_list` MCP Response

Default response is a compact one-line-per-job summary (id, name, status, schedule, next-run, kind, agent, channel, last-status, error/result preview, message preview). Stays under ~30KB for 50+ job registries. Options:
- `verbose: true` — legacy multi-line format (byte-identical to pre-change output)
- `ids: ["<job_id>", ...]` — drill into specific jobs with full bodies (takes precedence over `verbose`, max 200 items)

Security: sanitize-then-truncate ordering enforced for all user-controlled fields (message <=80 chars, last_error <=200 chars, last_result <=120 chars) so credentials straddling truncation boundaries cannot leak as partial fragments.

## Dashboard (`dashboard/`)

Modular aiohttp package at `127.0.0.1:5476` (configurable). Split into:
- `state.py` — `_ChatSlot` and `DashboardState` data classes; in-memory message buffer (5000 per slot); WS client tracking (`_ws_clients`, `_ws_log_subscribers`); `_broadcast()` sends to both SSE queues and WS clients (dual path); `broadcast_ws()` for WS-only events (chat_chunk, chat_done, refine); `close_all_ws()` for clean shutdown; `_slack_linked` bool on `_ChatSlot` (set from `SessionMap.get_slack_link()` on slot init); `linked_session_key` str on `_ChatSlot` (when set, `_run_chat` uses this as session key instead of deriving from slot name — enables cron slots to share the cron's persistent session); `_artifact` str on `_ChatSlot` (Mesh-2772 companion chat: the artifact slug this slot is bound to; parsed at slot create against the slug grammar, exposed as `artifact` in `to_dict()`/WS `slots`, persisted in history meta — see `modules/artifacts.md` § Companion Chat); `push_artifact_update(slug, version, deleted=False)` broadcasts the typed `artifact_update` WS envelope from the artifact mutation funnel; **approval queue**: `_pending_approvals` dict + `_approval_futures` (asyncio.Future per request), `request_approval(..., is_background=False)` creates future + broadcasts WS `approval` event, `resolve_approval()` resolves future. Timeout auto-denies. Interactive sources wait `_APPROVAL_TIMEOUT` (7200s / 2h, pauses for resume); **unattended background sources** (cron/heartbeat/taskrunner — passed `is_background=True` by the gateway) deny-fast after `_BACKGROUND_APPROVAL_TIMEOUT_SECS` (180s / 3 min) since no human is present to respond. **Slot titles**: untitled slots serialize as `NEW_SESSION_TITLE` (`"New Session…"`) via the `_ChatSlot.display_title` property — applied at the serialization boundary so brand-new empty sessions and the pre-LLM-title window all read the same, while slots with a real (non-key) title are unaffected; `push_slot_title(key, title, *, full=True)` broadcasts a title update; callers pass `full=False` to emit only the lightweight `slot_title` event for high-frequency streaming title partials (word-by-word reveal), then finalize with one default `full=True` call (which also fires a `push_slots_update()`). **Status snapshot**: `status_snapshot()` also carries `branch` and `commit` (from `_build_info`) so clients can detect an actual code update. **Sidebar preview**: `_ChatSlot.to_dict()` skips `assistant`-role turns tagged `meta.kind=="compaction"` (auto-compact notices, `/compact` banners) when picking the `last_message` preview and its OPTIONS, mirroring the frontend's `deriveFollowUpOptions` skip so the sidebar shows the last *real* message
- `chat.py` — multi-slot chat with per-tab kiro-cli sessions (`dashboard:{slot.key}`), background LLM streaming (survives browser disconnect), session lifecycle management (active ↔ history), chunk cleanup, tool approval flow. Each tab gets its own kiro-cli process for true multi-agent parallelism — tabs can run tools simultaneously. Sessions idle-expire; on restart, the live tab set is restored from `~/.kirocrew/open_slots.json` (snapshotted on every flush + shutdown by `DashboardState._persist_open_slots()`, replayed on startup by `restore_open_slots()` before the legacy mtime-based `restore_recent_sessions()` so long-running tabs survive regardless of message age), and full tab history is re-injected. Cross-tab context (recent messages from other dashboard sessions, capped at 5k chars) is injected at session start for continuity. `?ws=1` mode returns JSON immediately and pushes chunks via WS. `_prepare_messages()` collapses `chunk` entries into `streaming` role for API responses during active streaming. Timestamp preservation on resume (original `ts` from JSONL) and save (single-pass JSONL write preserving `ts` and `created_at`). **Agent persistence**: `slot.agent` saved to JSONL metadata on close, restored on resume — custom agent sessions survive close/reopen. Pushes `refresh("history")` after chat completion. **Bidirectional Slack sync**: mirrors user messages to linked Slack threads when `slack_client` is available. Stop resets the per-tab session; delete kills the per-tab session via `sessions.remove()` to free resources. **Slash commands**: `_SLASH_COMMANDS` frozenset skips context injection (sent verbatim to kiro-cli). `_BLOCKED_SLASH_COMMANDS` (`/quit`, `/exit`, `/q`, `/chat`, `/paste`, `/reply`, `/editor`) are rejected before session acquisition — returns warning message without touching kiro-cli. **ACP extension events**: `_run_chat` handles `compaction_status` (shows ✅/❌ completion/failure), `clear_status` (clears slot messages + broadcasts `slot_clear`), `agent_switched` (updates `slot.agent` + resets session + broadcasts `slot_agent_switch`).
- `ws.py` — WebSocket endpoint at `/api/ws`. Single multiplexed connection for all real-time events. Pushes dashboard status every 5s, current slots on connect, log ring buffer replay on subscribe. Server→Client: `{"type": "dashboard|slots|slot_title|notification|refresh|chat_message|chat_chunk|chat_done|log|refine|sessions_restarting|slot_clear|slot_agent_switch", "data": {...}}`. Client→Server: `{"type": "subscribe_logs|unsubscribe_logs"}`. `sessions_restarting` event pushed by `_reset_all_sessions()` with `{"status": "restarting"|"ready"}` so the frontend knows when sessions are being recycled. Each provider shutdown is bounded by `_SHUTDOWN_TIMEOUT_SECS` (5s) via `asyncio.wait_for`; on timeout, `_sync_kill_provider` force-kills the process tree to prevent leaks (see `docs/resource-protection.md`). `slot_clear` pushed on `/clear` (frontend clears messages for active slot). `slot_agent_switch` pushed on `/agent` switch (frontend re-fetches slots for updated agent label). Uses `asyncio.ensure_future(ws.send_str())` because aiohttp 3.13's `send_str()` is a coroutine. **Security**: `_check_ws_origin()` validates the `Origin` header before accepting the upgrade — rejects missing or cross-origin requests (allowed: `127.0.0.1`, `localhost`, `kirocrew.localhost`). Max 5 concurrent WS connections (`_MAX_WS_CLIENTS`).
- `handlers.py` — status, system (live CPU/memory/network), memory CRUD, cron CRUD, lesson CRUD, skills, agent config (save + auto-restart sessions), logs SSE with persistent ring buffer (1000 entries, replays on connect) + queue-based handler (also pushes to WS log subscribers via `ensure_future`), log level control, session delete, refine status push via `broadcast_ws` with throttled chunks (~4/sec). `start_time` included in SSE/WS dashboard status payload. MCP management: probe cache (`_bg_mcp_probe()` at startup, 10-min TTL, merges enabled/disabledTools from global mcp.json), server/tool toggle writes to `~/.kiro/settings/mcp.json` + syncs to kirocrew.json, bulk toggle-all, `_sync_mcp_to_agent()` helper.
- `server.py` — app factory, route registration, startup, SPA fallback middleware for React Router, `/api/ws` WebSocket route, Midway auth middleware, loopback-only binding (`127.0.0.1`). Fires background MCP probe at startup via `asyncio.create_task()`. Honors `agent.yolo=true` config at startup via `_apply_startup_yolo()` — attempts SEL audit first and only activates dashboard YOLO (6h TTL) if the audit succeeds (fail-closed).

### Security

- **Network binding**: dashboard binds to `127.0.0.1` only (loopback), never `0.0.0.0`. Prevents unauthenticated remote access from network-adjacent attackers.
- **Midway authentication**: `dashboard/midway.py` validates `~/.midway/cookie` (written by `mwinit`) on every request. Checks that `session`, `amazon_enterprise_access`, and `user_name` cookies are present and not expired. Pure stdlib — no external deps. Returns `401` if invalid. Controlled by `require_midway_auth` config flag (default `true`).
- **WebSocket origin validation**: `ws.py:_check_ws_origin()` validates the `Origin` header on every WebSocket upgrade request before accepting the connection. Rejects missing Origin (non-browser clients) and cross-origin requests. Only allows `http://127.0.0.1:{port}`, `http://localhost:{port}`, and `http://kirocrew.localhost:5476`. Prevents cross-origin WebSocket hijacking where a malicious page could connect to `ws://127.0.0.1:5476/api/ws` and passively exfiltrate conversation data.
- **Midway authentication**: `dashboard/midway.py` validates `~/.midway/cookie` (written by `mwinit`) on every request including WebSocket upgrades. Checks that `session`, `amazon_enterprise_access`, and `user_name` cookies are present and not expired. Pure stdlib — no external deps. Returns `401` if invalid. Controlled by `require_midway_auth` config flag (default `true`).
- **CSRF protection**: `server.py` CSRF middleware validates `Origin` header on all non-safe HTTP methods (POST, PUT, DELETE). Same allowed origins as WebSocket.
- Static assets (`/assets/`, `/static/`) bypass auth check.

### Session Lifecycle

Each chat tab gets its own kiro-cli session keyed by `dashboard:{slot_key}` with a corresponding JSONL file (`~/.kirocrew/history/dashboard:{key}.jsonl`). Sessions move between active and history:

1. **New**: user clicks + → creates slot with unique key → `get_or_create("dashboard:{key}")` assigns a kiro-cli session (cold start)
2. **Chat**: messages accumulate in-memory (`slot.messages`, max 5000); kiro-cli session is per-tab so tabs run tools in parallel
3. **Close**: user clicks ✕ → slot saved to JSONL (overwrites, preserves `created_at`) → per-tab session killed via `sessions.remove()` → removed from active → appears in history
4. **Resume**: user clicks history item → JSONL loaded into slot (same key) → new kiro-cli session created with history re-injected (if already active, returns existing — no duplicate)
5. **Close again**: saved back to SAME JSONL file → same history entry (no duplicates)
6. **Gateway shutdown**: all active slots saved to JSONL
7. **Idle expiry**: per-tab sessions expire after `session.timeout_secs` (default 60 min) like any other session; on next message a fresh session is created with history re-injected

Cross-tab context: **removed** (budget redistributed to other caps). Previously injected recent messages from other dashboard tabs; this block was eliminated and its 6,000-char budget absorbed into the raised memory/lessons caps above.

**Context budget** (`context.py`): total cap 165,000 chars (~55k tokens). Priority order: critical rules → memory (preferences 4,250, projects 6,400, history 26,600) → skills (on-demand, few always-on) → lessons (37,250) → conversation history (8k budget, 8,000 chars/message cap, most-recent-first fill) → provenance. Individual messages exceeding 8,000 chars are truncated with `…[truncated]`. If total exceeds 165,000, hard-truncated at nearest newline.

**Per-turn timeout** (`constants.py:CHAT_TURN_TIMEOUT`): every `_run_chat` invocation is wrapped with `asyncio.wait_for(timeout=CHAT_TURN_TIMEOUT)` regardless of dispatch site. This applies uniformly to: primary user-typed turn (`chat_handlers.py`), queue-drain (`chat_runner.py` finally block), cron injection (`handlers/messaging.py`), Slack/dashboard nudge (`slack/gateway.py` autonudge path), and subagent injection (`slack/gateway.py` two paths). The cap (7200s, 2 hours) is sized to match the inner ACP `_DEFAULT_PROMPT_TIMEOUT` so the dashboard layer does not bound below the transport. The `_STALE_TURN_TIMEOUT` (90s, in `acp/client.py`) is the real wedged-session guard — it fires when streaming has gone silent. `CHAT_TURN_TIMEOUT` is the upper safety ceiling for genuinely runaway work, not a "this turn took too long" guard.

**Custom agent context**: When a dashboard slot uses a non-kirocrew agent, `build_message()` and `build_session_context()` skip only skills and workspace identity (custom agents load their own via kiro-cli). All other context is injected for all agents: critical rules (diff rendering, OPTIONS buttons), memory (preferences, projects, history, semantic, episodic), lessons, hooks, and OPTIONS reminder. This ensures custom agents, cron jobs, and task runners all benefit from the user's learned preferences and project context.

Streaming chunks are cleaned up after each response (only final assistant message kept). Transient roles (`chunk`, `done`, `queued`, `permission`) are excluded from history saves.

**Agent Config**: PUT saves to `~/.kiro/agents/kirocrew.json` and auto-restarts all kiro-cli sessions so changes take effect immediately.

### Key Endpoints

**Status/System**: `/api/status`, `/api/system` (live metrics, 1s refresh, static fields cached), `/api/stream` (SSE), `/api/ws` (WebSocket — single multiplexed connection replacing SSE + polling for React SPA)
**Theme**: GET `/api/theme/boot` (**unauthenticated** — same boundary as `/api/health`; returns `{mode, color, onboarded}` so the SPA can apply the workspace theme before the token flow completes; no secrets), GET/PUT `/api/config/theme` (read/persist workspace theme `{mode?, color?, onboarded?}`; `mode` restricted to `""`/`dark`/`light`/`system`)
**Memory**: GET/PUT `/api/memory/preferences`, GET/PUT `/api/memory/projects`, GET/PUT `/api/memory/history`, GET/PUT `/api/memory/settings` (consolidation config: `history_idle_hours`, `history_max_days`; writes to config.json, applies immediately to running consolidator)
**Cron**: GET `/api/crons` (includes `last_run_ts`, `has_result`, `has_slot`, `hide_in_chat`, per-job `timezone`, top-level `server_tz`, `skip_dates`), POST `/api/crons` (create, optional `agent`, `hide_in_chat` fields), DELETE `/api/crons/{id}`, DELETE `/api/crons` (batch delete; body `{"ids": [...]}`, ids de-duplicated, capped at 500, per-id failure isolation, returns `{ok, deleted, failed}` with `ok` true iff anything was deleted; history purged per removed job; single `crons` refresh push; SEL-audited), PATCH `/api/crons/{id}` (partial update: name, message, cron, every, agent (or agent_id), channel, approval_mode, silent, strict_schedule, hide_in_chat, timezone), POST `/api/crons/{id}/enable`, POST `/api/crons/{id}/run` (immediate execution, returns `{name}`, non-blocking via `create_task`), POST `/api/crons/{id}/to-chat` (creates linked chat slot `cron-{id}` with `linked_session_key="cron:{id}"`, hydrates from session history, reuses existing slot)
**Messaging**: POST `/api/send-message` (send to Slack DM + dashboard notification; body: `{text, title?, blocks?}`; when `blocks` provided, sends Block Kit message via `post_blocks()` with `text` as fallback; used by `send_message` MCP tool in `kirocrew-core`)
**Lessons**: GET `/api/lessons`, POST `/api/lessons` (add), DELETE `/api/lessons` (remove by substring)
**Skills (CRUD)**: GET `/api/skills`, POST `/api/skills` (create), GET/PUT/DELETE `/api/skills/{name}`
**MCP Servers**: GET `/api/mcp` (list with enabled/disabledTools state from `~/.kiro/settings/mcp.json`), GET `/api/mcp/active` (per-agent MCP servers — reads from agent config for non-kirocrew agents, global mcp.json for kirocrew), GET `/api/mcp/probe` (cached probe results, non-blocking, 10-min TTL, preserves enabled state), POST `/api/mcp/probe` (live probe all, merges enabled/disabledTools from global config), POST `/api/mcp/sync` (discover + add to both kirocrew.json AND global mcp.json + session reset), POST `/api/mcp/toggle` (enable/disable server in global mcp.json + sync tools/allowedTools to kirocrew.json), POST `/api/mcp/toggle-tool` (enable/disable specific tool via disabledTools in global mcp.json), POST `/api/mcp/toggle-all` (bulk enable/disable all servers in global mcp.json + sync to kirocrew.json)
**Agent Config**: GET/PUT `/api/agent/config` (read/write `~/.kiro/agents/kirocrew.json`, PUT auto-restarts sessions)
**Chat**: POST `/api/chat` (SSE stream, or JSON with `?ws=1` — chunks via WebSocket), `/api/chat/slots` (CRUD, POST accepts optional `agent` field to set agent at creation), resume from history, POST `/api/chat/slots/{slot}/generate-title`, POST `/api/chat/slots/{slot}/agent` (switch agent for slot), POST `/api/chat/slots/{slot}/fork` (fork session — copies visible messages into new slot, body: `{at_message_index?, prompt?}`, returns `{ok, key, title, messages, prompt}`, new slot has `forked_from` metadata), POST `/api/chat/slots/{slot}/edit-resend` (edit a user message and re-run; in-place truncation of `slot.messages`, body: `{index?, ts?, content}`), POST `/api/chat/slots/{slot}/rewind` (edit any past user message and re-run; fork-and-swap — truncates `slot.messages`, removes the slot's ACP session via `SessionManager.remove`, deletes orphaned kiro-cli session JSONL at `~/.kiro/sessions/cli/<id>.json[l]`, then runs the edited prompt against a fresh ACP session under the same slot key/title/folder. Mirrors kiro-cli `/rewind`. Body: `{at_message_index?, ts?, content}`), PATCH `/api/chat/slots/{slot}/mode` (switch session mode between `""` and `"orchestrator"` — `_VALID_MODES`; 404 missing slot, 400 invalid mode, 409 while the session is running)
**Chat Folders**: GET `/api/chat/folders` (list project folders, each enriched with a computed non-persisted `history_count` — the authoritative on-disk archived-session count per folder from `ConversationLog.list_sessions()`), POST `/api/chat/folders` (create; body `{name, parent_id?, project_dir?}`, background LLM emoji-icon generation), PATCH `/api/chat/folders/{id}` (update — accepts `hidden` (bool) alongside `name`/`collapsed`/`order`/`default_agent`/`project_dir`/`icon`; moving or reviving a session into a folder auto-unhides it via `_unhide_folder`), DELETE `/api/chat/folders/{id}` (delete + ungroup its slots)
**Agents**: GET `/api/agents` (KiroCrew agent roster ordered **most-used-first** — reorders config agents + discovered project agents by `ConversationLog.agent_usage()` (turn count, then recency), falling back gracefully to config-insertion order on any failure so the dropdown never breaks or drops agents), GET `/api/agents/installed` (list all kiro-cli agents from `~/.kiro/agents/`, with `package` field extracted from filename), GET/DELETE `/api/agents/detail/{name}` (full agent config JSON; DELETE removes the config file, protected for kirocrew/kirocrew-lite)
**AIM Integration**: GET `/api/aim/mcp` (list installed AIM MCP servers), POST `/api/aim/mcp/install` (install via `aim mcp install`, pushes `refresh("agents")`), POST `/api/aim/mcp/uninstall` (pushes `refresh("agents")`), GET `/api/aim/mcp/registry` (browse available MCP servers from registry, parsed into structured JSON with `details` array for all tab-indented lines), GET `/api/aim/skills` (list installed AIM skills), POST `/api/aim/skills/install` (install + regenerate agent config, optional `version_set`, pushes `refresh("agents")`, friendly error messages via `_friendly_aim_error`), POST `/api/aim/skills/uninstall` (pushes `refresh("agents")`), GET `/api/aim/agents` (list installed AIM agent packages), POST `/api/aim/agents/install` (install + regenerate agent config, optional `version_set`, pushes `refresh("agents")`, friendly error messages), POST `/api/aim/agents/uninstall` (pushes `refresh("agents")`), POST `/api/aim/update` (update agents/skills/mcp packages by `kind`, optional `package` for individual updates, pushes `refresh("agents")`)
**Claude Code AIM sync** (kiro→CC mirror **removed**): the `/api/cc/mirror/preview` + `/api/cc/mirror/run` routes, `mirror_kiro_to_cc`, the `ProviderPanel.tsx` "Migrate from kiro to Claude Code" UI, and the `kirocrew mirror kiro-to-cc` CLI were **deleted** when KiroCrew collapsed to a single KiroACP / `kiro-cli` backend — there is no Claude Code provider to mirror to. Still registered: GET `/api/cc/aim/missing` (lists kiro AIM packages not installed for Claude Code via `installed_kiro_packages_missing_from_cc`) and POST `/api/cc/aim/sync` (installs AIM packages as CC plugins via `install_cc_plugin(standalone=True)`, body `{packages: [str]|null}`, validates names against `_VALID_PACKAGE_RE`, audit-logs each install). In the public OSS fork both `install_cc_plugin` and `installed_kiro_packages_missing_from_cc` are **no-op/empty stubs** (`aim_agents.py`) — the optional plugin CLI is absent — so these endpoints succeed trivially with nothing to do.
**Sessions**: GET `/api/sessions` (paginated list), GET `/api/sessions/{key}` (detail), DELETE `/api/sessions/{key}` (permanent delete), GET `/api/sessions/context` (context usage), GET `/api/sessions/usage` (kiro credit usage via background `kiro-cli chat --no-interactive --agent kirocrew-lite /usage`, cached 10 min)
**Logs**: GET `/api/logs` (SSE), GET/POST `/api/logs/level` (runtime log level control)
**Task Runner**: GET `/api/taskrunner` (status with runs[], includes `agent`), POST `/api/taskrunner` (start, optional `agent` field), POST `/api/taskrunner/cancel` (per-task or all), DELETE `/api/taskrunner/{task_id}` (remove finished run), POST `/api/taskrunner/refine` (dynamic multi-turn with tool access), GET `/api/taskrunner/refine` (status with `waiting` field), POST `/api/taskrunner/refine/cancel`, POST `/api/taskrunner/refine/answer` (answer clarifying question)
**Approvals**: GET `/api/approvals` (pending list), POST `/api/approvals/{id}/approve`, POST `/api/approvals/{id}/reject`
**Reveal**: POST `/api/reveal` (open file in Finder/file manager)
**File Picker**: POST `/api/upload` (macOS only — opens native osascript file picker, returns absolute paths)
**Screenshot**: POST `/api/screenshot` (macOS only — `screencapture -i`, returns path to `~/.kirocrew/screenshots/`)
**Misc**: `/api/spawn`, DELETE `/api/spawn/{id}`, POST `/api/spawn/clear`, `/api/notifications`, DELETE `/api/notifications/{ts}`, POST `/api/notifications/clear`, DELETE `/api/sessions/clear`, `/api/update/check` (git remote check), `/api/update` (pull + rebuild + restart, dirty-tree guard returns 409 if uncommitted changes)
**Webhook Hooks**: POST `/api/hooks/agent` (external trigger — runs ephemeral agent turn; body: `{message, sessionKey?, name?, agent?, deliver?, timeoutSeconds?}`; requires `Authorization: Bearer <hooks.webhook_token>`; sessionKey must start with `hook:`; max 6 concurrent; session destroyed after turn; context injected from `~/.kirocrew/hooks.json`)

### Frontend (React SPA)

React 18 + TypeScript + Vite 5 + Redux Toolkit + React Router v7 + Tailwind CSS 3 + DOMPurify. Source in `frontend/`, builds to `src/kiro_crew/static/dist/`.

**Styling** — Tailwind CSS with custom theme in `tailwind.config.js`. CSS custom properties (design tokens) defined in `index.css` for dark/light themes. `darkMode: ['selector', '[data-theme="dark"]']` enables Tailwind `dark:` variant with the `data-theme` attribute. Tailwind utility classes used throughout components (no separate CSS files per component). PostCSS + autoprefixer for processing. Theme toggle smoothly crossfades via `transition: background-color .25s, color .25s` on `body`.

**Design tokens** — All colors use CSS custom properties mapped in `tailwind.config.js`:
- Core: `--bg`, `--card`, `--text`, `--muted`, `--border`, `--accent` (amber/orange)
- Semantic: `--ok` (green), `--warn` (amber), `--danger` (red), `--info` (blue)
- AIM: `--aim` / `--aim-subtle` (purple) — used for AIM agent badges, MCP server pills
- Clarify: `--clarify` / `--clarify-subtle` (amber) — used for task refine Q&A box
- Diff: `--diff-add/del/hunk/meta` — theme-aware diff colors for MarkdownRenderer

**CSS utilities** — Defined in `index.css`, used across components:
- `.topbar-glass` — frosted glass effect (`backdrop-filter: blur(12px) saturate(1.4)`) on topbar
- `.scroll-shadow` — gradient mask fade at top/bottom of scrollable panels
- `.table-striped` — alternating row backgrounds via `nth-child(even)`
- `.skeleton` — shimmer animation placeholder for loading states
- `.focus-ring` — unified focus outline (`border-color + box-shadow + glow`) for all inputs/textareas
- `.card-glow`, `.stat-accent`, `.btn-sweep`, `.streaming-cursor`, `.think-bar`, `.typing-dots`

**Shared UI components** (`frontend/src/components/`):
- `ui.tsx` — `Card`, `CardTitle`, `Btn`, `SendBtn`, `Input`, `Badge`, `AimBadge`, `StatCard` (with skeleton loading), `Skeleton`, `EmptyState`, `PageHeader`
- `AgentSelector.tsx` — reusable agent dropdown with portal positioning, ARIA roles (`listbox`/`option`/`aria-selected`), `AimBadge` source pills, outside-click-to-close
- `ProjectAnimation.tsx` — image-based animation: KiroCrew logo (`icon-192.png`) with orbiting rings and pulsing glow. Theme-aware via `var(--accent-glow)`. Used in ProjectsPage empty state.
- `PixelCanvas.tsx` — pixel-art office canvas (256×256 at 3× scale) with 7 character sprites. States: typing (working), looking (reviewing), celebrate (passed), idle, empty. Loads `/sprites/floor.png` + `char{0-6}-frame{1,2}.png`. Renders via `requestAnimationFrame` loop.
- `PixelCanvasWidget.tsx` — 🎮 button with active-agent badge + modal overlay wrapping `PixelCanvas`. Maps `ProjectRun` task statuses to character slots.
- `SubAgentActivity.tsx` — live subagent status table with polling (2s). Status pills: Running (warn, animate-pulse), Done (ok), Failed (danger). Shown below running projects.
- `layout.ts` — `LAYOUT` constants: `NAV_WIDTH` (220), `NAV_COLLAPSED_WIDTH` (56), `CHAT_SIDEBAR_WIDTH` (260), `MAX_MESSAGE_WIDTH` (820), `AGENT_LIST_HEIGHT` (420), `LOG_LINE_CAP` (500), `TOPBAR_HEIGHT` (52)
- `InfoTip.tsx` — portal-rendered `?` tooltip
- `MarkdownRenderer.tsx` — block-assembled rendering: `useBlockAssembler` hook splits raw text into structured `ContentBlock[]` (markdown/code/diff/mermaid) via state machine, then renders each block with specialized renderer. During streaming, unclosed code fences render as provisional blocks; on completion, full reparse from rawText produces clean final output. react-markdown + remark-gfm + rehype-raw for markdown blocks, `HighlightedCode` for syntax-highlighted code blocks, `DiffBlock` for diffs, `MermaidBlock` for diagrams. `fixCodeFences()` repairs malformed LLM output.
- `DiffBlock.tsx` — dedicated diff renderer with line number gutter (old/new), `+`/`-`/context line classification, hunk header parsing, file meta headers, "Copy patch" button, provisional "generating diff…" indicator for incomplete streaming blocks. Supports both standard unified diff and kiro-cli `+N:`/`-N:` format.
- `TypewriterText.tsx` — animated title reveal

**Syntax highlighting** — `highlight.js` (tree-shaken: js/ts/py/bash/json/yaml/html/css/sql/rust/java/md). Custom One Dark / One Light theme in `index.css` using design tokens. `hljs.highlight()` for known languages, `hljs.highlightAuto()` for unknown. Output sanitized via DOMPurify.

**Streaming rendering** — Two-layer architecture: (1) `useBlockAssembler` hook parses raw text into `ContentBlock[]` via state machine tracking `paragraph`/`fenced_code` states; (2) `BlockRenderer` dispatches each block to specialized renderers (`MarkdownBlock`, `CodeBlock`, `DiffBlock`, `MermaidBlock`). During streaming (`streaming=true`), unclosed code fences produce blocks with `complete: false` that render as provisional views. On completion (`streaming=false`), full reparse from content produces clean final blocks. `ChatMessage.rawText` preserves the original unprocessed text as source of truth for reparse.

**Security** — All `dangerouslySetInnerHTML` content sanitized via DOMPurify (`frontend/src/api/helpers.ts`):
- `md()` — renders markdown-like formatting (code blocks, bold, italic) + DOMPurify sanitize
- `sanitize()` — DOMPurify wrapper for pre-escaped HTML
- `esc()` — plain text HTML escaping
- CLI: `/etc/hosts` update uses `sudo tee -a` (not `sh -c echo`) to prevent shell injection

**State management** — Redux store with three slices:
- `dashboardSlice`: SSE/WS connection state, chat slots array, approval mode (synced from backend `yolo` field in status — no localStorage), optimistic slot add/remove reducers (`addSlotOptimistic`/`removeSlotOptimistic`), async thunks for slot fetch / approval mode change. YOLO state is backend-authoritative: `sseStatus` reducer syncs `approvalMode` from `status.yolo`; page load fetches `/api/status` immediately for instant sync.
- `chatSlice`: active slot, messages, session history (paginated), WS chunk/done handling (accumulate chunks into streaming, finalize on done), optimistic slot mutations, WS-ahead guard on `switchSlot.fulfilled`, async thunks for all slot/history CRUD
- `notificationsSlice`: notification list with add/delete/clear/ack/unack, async thunks for fetch/delete/clear. `addNotification` deduplicates by `ts`. `ackNotification`/`unackNotification` are optimistic (`.pending` case). WS events `notification_ack`/`notification_unack` sync ack state across tabs; `ackNotificationByTs("*")` handles bulk ack-all. On WS reconnect, `fetchNotifications()` re-fetches to recover missed notifications during disconnect.

**Real-time updates** — Single WebSocket at `/api/ws` (`useWebSocket` hook) multiplexes all events: `dashboard`, `slots`, `slot_title`, `notification`, `notification_ack`, `notification_unack`, `refresh`, `chat_message`, `chat_chunk`, `chat_done`, `log`, `refine`, `sessions_restarting`, `heartbeat`, `tool_call`, `context_usage`. Exponential backoff reconnect (1s→2s→4s→max 10s); on reconnect re-fetches slots via Redux dispatch — **no page reload** unless the server `version` field in the `dashboard` status message changes (actual code update). This preserves unsent messages, scroll position, and form state across transient disconnects. `WsContext` provides log subscribe/unsubscribe to `LogsPage`. SSE (`useSSE` at `/api/stream`, `useLogSSE` at `/api/logs`) remains as a secondary transport. Chat send uses `AbortController` with 10-second timeout — if the backend is busy starting kiro sessions, the fetch times out gracefully without showing an error (the message was received server-side; chunks arrive via WS when the session is ready).

**Routing** — `App.tsx` uses React Router `<Routes>` with paths: `/chat`, `/notifications`, `/overview`, `/worlds`, `/system`, `/agents`, `/projects`, `/logs`, `/hooks`. Default redirects to `/chat`. SPA fallback middleware in `server.py` catches 404s on non-API GET requests and serves `index.html`.

**Nav sidebar** — Collapsible: full mode (220px with labels) or icon-only mode (56px). Chevron toggle button. State persisted in `localStorage('mc-nav')`. The **Apps** group is drag-reorderable via dnd-kit sortable (`SortableAppNavRow` + `DndContext`/`SortableContext`/`DragOverlay`, `PointerSensor` with an 8px activation distance so a plain click still navigates): rows reflow to open a gap as one is dragged, the source dims, and a `DragOverlay` ghost follows the cursor; the order persists to `localStorage('mc-app-nav-order')` (`arrayMove`). Reorder is scoped to the currently visible Apps rows (the "N more" overflow collapse hides the rest). Main/Platform groups are static (no drag).

**Session titles** — auto-generated after a few turns via background LLM call in `chat_title.py` (`_maybe_auto_title`), pushed to all clients via `slot_title` WS/SSE event, persisted in chat history JSONL metadata via `ConversationLog.set_title()`. Title input scans reserve a bounded allowance for the dashboard's 20-file upload limit, remove generated image/file references, and then cap retained user text before prompting or fallback selection. Non-image metadata is validated, length-limited, and stored in token-index order so each generated reference resolves directly without scanning every path. Manual trigger via `POST /api/chat/slots/{slot}/generate-title`; generation errors use the same sanitized first-user-message fallback, while attachment-only placeholder results remain untitled so a later automatic attempt can retry. Cancellation releases the in-flight guard without starting a pending retry. Max 5 auto-title attempts before falling back to the first usable user message.

### UI Pages

- **Chat** (`/chat`, default) — multi-session parallel chat, Slack-style grouped messages with timestamps (MMM DD, YYYY, HH:MM), KiroCrew logo as assistant avatar, full Markdown rendering via `react-markdown` + `remark-gfm` + `rehype-raw` with Mermaid diagram support, syntax-highlighted code blocks (highlight.js), and clickable file paths (inline `<code>` containing paths → reveal in Finder via `/api/reveal`), session sidebar with titles and scroll-shadow panels (notifications moved to dedicated page), collapsible history section (default collapsed) with source tags (🖥 dashboard / 💬 slack) and creation dates, session delete from history, `EmptyState` component when no session is active. Chat uses WS for streaming (`?ws=1` mode): POST returns immediately, chunks arrive via WebSocket. Auto-approved tool calls broadcast via WS as ephemeral cards (not persisted to messages), inserted before streaming message in Virtuoso list. **Agent selection**: WelcomeView (pre-first-message) has agent picker that sets `pendingAgent` state — on first send, slot is created with that agent via `POST /api/chat/slots {agent}`. Agent selector dropdown also in top bar next to session title for mid-session switching. Agent badge (aim-colored pill) in sidebar slot list. **MCP info button** shows per-agent MCP servers: non-kirocrew agents show only their own MCPs from agent config; kirocrew shows all global MCPs. **Tool/approval payload viewer** (`ToolDetails`, website SPA): tool-call and pending-approval cards render the payload with a **Raw / Formatted** toggle (beside the Input/Output control, shown only for JSON-ish payloads where the two modes differ). Formatted renders the parsed JSON object as a key→value table — multi-line command values show real line breaks and quotes (JSON-decoded), with bash syntax highlighting on command-bearing keys (`command`/`cmd`/`script`/`shell`/`bash`) via the shared worker highlighter; Raw shows the exact verbatim payload with escaping intact (and is the fallback for truncated/streaming or non-object payloads).
- **Notifications** (`/notifications`) — dedicated page with left/right split layout. Left: category tabs (All/Cron/Hooks/Heartbeat/Agent/Approval/Subagent/Tasks), search filter, date-grouped list (Today/Yesterday/This Week/Older). Right: detail panel with source label, full timestamp, Read/Unread badge, markdown-rendered body, and jump-to-source buttons. Jump logic: `slot` meta → "💬 Go to Chat" (active tab) or "💬 Resume Chat" (from history); `slack_link` meta → "💬 Open in Slack" (deep link); `task_id` → "💬 Continue in Chat"; `job_id` → "⏰ View Cron Jobs". Cron notifications have `CronAckBar` for acknowledge/delete. Notification meta includes `slot` (subagent/heartbeat from dashboard), `slack_link` (subagent from Slack), `session_key` (webhook), `job_id` (cron), `task_id` (task runner). `_notif_meta()` helper on `GatewayOrchestrator` builds meta from `parent_key`. StatCard row: Total/Unread/Cron/Hooks/Heartbeat. Nav badge shows unread count.
- **Overview** (`/overview`) — `StatCard` components (with skeleton loading) + tabbed management console:
  - **Memory tab**: editable preferences.md / projects.md with Save buttons, read-only daily history. **Memory Graph Explorer**: vis.js network visualization of semantic memory relationships (nodes = memory entries, edges = similarity).
  - **Cron tab**: add job form (with shared `AgentSelector` component) + striped job table with Pause/Resume/Delete actions
  - **Lessons tab**: add lesson form + lesson table with Delete actions
  - **Skills tab (CRUD)**: + New button with create form (name + SKILL.md editor), installed skill list with click-to-view, ✏ Edit button with inline textarea editor + Save, ✕ Delete with confirmation, name sanitized to lowercase + hyphens. AIM Skills section shows skills from `~/.aim/` grouped by package with Uninstall button per package. Skills are fully AIM-managed — no bundled skills; `AIPowerUserCapabilities` installed by default via setup/update.
  - **MCP Servers tab**: Controls `~/.kiro/settings/mcp.json` (global config that kiro-cli ACP loads at runtime). Server-level enable/disable sets `disabled: true/false` in global config and syncs `@server` to kirocrew.json `tools`/`allowedTools`. Per-tool enable/disable sets `disabledTools` array in global config. Probe All discovers tools per server, preserves enabled/disabledTools state across probes. Enable All / Disable All bulk buttons. Tool chips: green = enabled (clickable to disable), strikethrough = disabled (clickable to enable). Apply & Restart at top bar resets all active sessions. Live server badges (🔌 color-coded by status).
  - **Slack tab**: STT (Speech-to-Text) settings card — toggle enabled/disabled, provider selector (`whisper` / `mlx` / `transcribe`), model selector (turbo ~1.6 GB), status badge (ready/not installed), provider-aware install button (`brew install openai-whisper` for `whisper`, `pipx install mlx-whisper` for `mlx`). The `mlx` provider (Apple Silicon Metal GPU) uses the `mlx_model` config key (default `mlx-community/whisper-large-v3-turbo`). Endpoints: `GET/PUT /api/config/stt`, `POST /api/stt/install`.
  - **Agent Config tab**: JSON editor with Save + warning about `kirocrew setup --agent-only`
- **System** (`/system`) — live metrics (1s refresh): CPU %, memory used/total, network RX/TX stat cards; host info with correct Apple Silicon arch detection, load averages; memory, process, network, storage detail cards; uptime ticking every 1s via `useUptime` hook (client-side from `start_time`)
- **Agents** (`/agents`) — side-by-side layout: installed agents list (left) with detail panel (right, height from `LAYOUT.AGENT_LIST_HEIGHT`). Installed agents card shows each agent with name, `AimBadge` source pill, package badge (📦 for AIM packages, 📌 local for local agents), model, description, skill count, MCP server count. Click to view full agent config in detail panel: system prompt, tools, auto-approved tools, MCP servers with `--aim` token colors (hover tooltip showing tool list), expandable denied commands list (`<details>` with all patterns). Per-agent ⬆ Update and package-level 🗑 Uninstall buttons for AIM agents — uninstall uses package name (e.g. `Customer360GenAIContext`) or `local/agent-name` for local agents; also refreshes skills and re-syncs agent config. AIM package manager section: ⬆ Update All Agents / ⬆ Update All Skills bulk buttons; install agents/skills/MCP with version set support; friendly error messages for invalid packages (`_friendly_aim_error`). MCP registry browser: click-to-expand descriptions with all detail lines, clickable URLs (DOMPurify-sanitized), direct Install button, tier badges at `text-[11px]` minimum. Striped subagent table with `EmptyState` when empty, kiro credit usage card, context window usage bars per session (agent name in `--aim` color).
- **Tasks** (`/tasks`) — redirects to `/projects`
- **Projects** (`/projects`) — autonomous multi-step task execution with left/right split layout (260px sidebar + detail/compose area). Sidebar shows compact project cards with status icons, progress bars, cancel/delete buttons, and "＋ New Project" button. Compose area has two modes: ✨ Compose (free-text with refine-to-spec) and 📄 From Spec (paste/upload). Shared `AgentSelector` for agent selection. Plan generation with cancel, auto-polling for planned runs. Selected project detail view (`ProjectDetailPage`) with Idea/Tasks tabs: Idea tab shows spec content read-only with "Edit in Chat" button; Tasks tab has DAG/Phased view toggle. **🎮 button** (right-aligned in tab bar) opens pixel-art office animation modal (`PixelCanvasWidget`) showing character sprites working at desks based on task status. Action buttons: Execute/Chat/Discard (planned), Cancel (running), Restart/Schedule (completed/failed). Execute stays on project (no navigation). `SubAgentActivity` table shown below running projects. `ProjectAnimation` shown in empty compose state. Session storage persistence for mode, input, spec text, and planning state. 3-second auto-refresh polling.
- **Hooks** (`/hooks`) — script hook management following standard page layout: `PageHeader` + `StatCard` row (Total, Enabled, Total Runs, Errors) + `Card`/`CardTitle`/`InfoTip` wrapping a `table-striped` hooks table with `SearchInput` filter. Toggle switches for enable/disable, `Badge` status pills (ok/err/warn), `Btn` actions (▶ Test, Edit, ✕ Delete). Create/edit form uses `Card` wrapper with `Input`, styled `select` for event type, `SendBtn` for save. Test results shown in card-like panel below table with `Badge` exit status and dismissible stdout/stderr output.
- **Logs** (`/logs`) — live gateway log stream via WebSocket (subscribe/unsubscribe via `WsContext`), server-side log level control (DEBUG/INFO/WARNING/ERROR buttons). SSE (`/api/logs`) remains as a secondary transport.
- **Worlds** (`/worlds`) — agent world scenes with themed 3D-style environments (neural, wizard, underwater). Decorative page for visual personality.

### Agent Selector Component

Shared `AgentSelector` component (`frontend/src/components/AgentSelector.tsx`) used by Chat, Tasks, and Cron:
- `createPortal` renders to `document.body` — escapes `overflow` clipping
- `fixed` positioning with viewport-aware placement (flips up if overflows bottom, aligns right edge)
- `z-[9999]`, `max-w-[340px]`, agent name truncation for long names
- `AimBadge` source pills (aim=purple, kirocrew=amber, builtin=gray) using `--aim` design token
- ARIA: `role="listbox"`, `role="option"`, `aria-selected`, `aria-expanded`, `aria-haspopup`
- Outside-click-to-close via `setTimeout` + `document.addEventListener`
- Props: `agents`, `value`, `onChange`, `exclude` (filter out agent names)
- Agent list refreshes on `refreshTrigger` (WebSocket-driven after AIM mutations)

### InfoTip Component

Reusable `?` button (`frontend/src/components/InfoTip.tsx`) for contextual help across all pages:
- Portal-rendered to `document.body` — escapes `overflow: hidden` on `card-glow` parents
- `fixed` positioning with viewport-aware placement
- Solid background (`var(--card)`), strong shadow, `z-[9999]`
- Click to toggle, outside-click to close
- Used on: Sessions (Chat), Preferences/Lessons/Cron/Skills/MCP (Overview), AIM/Agents/Context/Usage (Agents), Task Runner (Tasks), Process (System)

### MCP Info Button (Chat)

The ℹ button next to chat session titles shows MCP servers for the current agent:
- Calls `GET /api/mcp/active?agent=<name>` — for non-kirocrew agents, reads from agent config in `~/.kiro/agents/`; for kirocrew, reads from global `~/.kiro/settings/mcp.json`
- Per-agent MCP scoping works: `kiro-cli acp --agent <name>` loads only that agent's `mcpServers`
- Footer note explains scoping: custom agents load only their own MCPs; kirocrew loads all global MCPs
- Visual: green dot = enabled, gray dot + "disabled" label = disabled
- Count shows `enabled/total`

### MCP Global Config (`~/.kiro/settings/mcp.json`)

For the default kirocrew agent, kiro-cli ACP loads MCP servers from the global config. For non-kirocrew agents (e.g. AIM-installed agents), kiro-cli loads only the `mcpServers` defined in the agent's own config file in `~/.kiro/agents/`. The dashboard MCP tab controls the global config used by kirocrew:
- `disabled: true` on a server prevents kiro-cli from loading it (kirocrew only)
- `disabledTools: [...]` on a server prevents specific tools from being registered (kirocrew only)
- `kirocrew-cron` and `kirocrew-core` are synced to global mcp.json at gateway startup (`_sync_kirocrew_mcps_to_global`)

#### `kirocrew-cron` Notable Parameters

- `cron_add`: accepts optional `silent` (boolean), `hide_in_chat` (boolean, keeps the cron out of the active session list — result still goes to Slack/bell + History), `script` (Python callable path), `command` (shell command — mutually exclusive with `script`), `timeout` (seconds), `strict_schedule` (boolean, disables jitter), `timezone` (IANA name), `skip_dates` (list of YYYY-MM-DD strings). When `silent=true`, results are not auto-delivered — the agent decides when to notify via `send_message`. When `script` or `command` is set, the job bypasses the LLM entirely (deterministic execution).
- `cron_update`: accepts `agent_id`, `timezone`, `skip_dates`, `strict_schedule`, `hide_in_chat` in addition to existing fields.
- `cron_trigger`: on-demand execution of a specific job by ID. Returns `{ok, name}`. Non-blocking (fires via `create_task`).
- `cron_list`: returns a compact one-line-per-job summary by default — id, name, status, schedule, next-run, kind (`script`/`command`/`agent`), optional `agent` / `channel` / `last=<status>` / `err=<preview>` / `result=<preview>` extras, message preview (<=80 chars), last_error preview (<=200 chars), last_result preview (<=120 chars). Full bodies are intentionally omitted so the response stays under ~30 KB for 50-job registries (the LLM tool-call budget would otherwise drop calls on large registries — `_render_cron_list_compact` in `mcp_cron.py`). Callers opt back into the legacy multi-line format with `verbose: true` (regression-safe — byte-identical to the pre-change shape, including the `[kind]` tag and `last error` / `last result` lines), or pass `ids: ["<job_id>", ...]` to drill into specific jobs and receive full bodies for matches only (`ids` takes precedence over `verbose`). Both modes go through `redact_credentials` / `redact_exfiltration_urls` on every user-supplied string. Sanitize-then-truncate ordering is enforced for the message, last_error, and last_result previews so a credential straddling the truncation boundary cannot leak as a partial fragment. Schema: `validation.CRON_LIST_SCHEMA` enforces `_JOB_ID_RE` on `ids` items (max 200) and rejects non-bool `verbose`.
- Auto-inject: when `persistent_session=True` (and `hide_in_chat=False`), cron results are auto-injected into a linked dashboard chat slot (`cron-{job_id}`). The slot is auto-created on first delivery for persistent crons — no user action required. Dashboard notifications include `meta.slot` for frontend "Continue session" navigation. For dedup-suppressed and silent runs, injection only occurs if the slot already exists (avoids creating slots for suppressed output). When `hide_in_chat=True`, all three injection call sites are skipped, so no slot is created and the notification CTA shows "View last result" (no-slot branch) instead.

#### `kirocrew-core`: `local_knowledge_search` Tool

Searches the Knowledge Library for relevant content. Escalated from App Store to built-in tool in `mcp_core.py`.

- **Trigger rules**: strict — only fires on explicit user signals (e.g. "search my knowledge", "check my docs on X"), not on general questions
- **Schema**: `LOCAL_KNOWLEDGE_SEARCH_SCHEMA` in `validation.py`
- **Parameters**: `query` (required string), `limit` (optional integer, default 3, hard max 5)
- **Confidence threshold**: `MIN_SCORE=0.012` filters noise
- **Output format**: source + content only, no score metadata (~2500 token budget)
- **Graceful degradation**: returns helpful message when knowledge DB not configured
- **Security**: credentials/exfiltration URL redaction on all results; SEL audit events for all outcomes (`success`, `no_results`, `not_configured`)
- **Store/embedder cache**: the `KnowledgeStore` (schema DDL + orphan-cleanup migration + in-memory graph load) and the embedder are cached process-wide in `mcp_core.py` (`_get_knowledge_search`) instead of rebuilt per call. Keyed on a signature of `knowledge.db`, its `-wal` sidecar, and `config.json`, so out-of-band dashboard ingestion (which writes the DB/WAL) or a config change triggers a rebuild on the next search; the prior connection is closed on rebuild. Avoids the per-call DDL/migrate/graph-load and the Ollama `/api/tags` availability probe (up to 3s when configured).

#### Knowledge De-duplication (`knowledge/dedup.py`)

Collapses the same document ingested from more than one source (e.g. an upload AND a folder-synced copy) down to a single canonical copy so retrieval stops returning duplicates.

- **De-dup key**: an `items.content_hash` column (whole-doc extracted-text sha256, stamped on every chunk of a document at ingest, on both ingest paths; index `idx_items_content_hash`). Legacy rows have NULL `content_hash` and fall back to the fuzzy tier.
- **Two-tier match** (`dedup_sweep`): Tier 1 -- identical `content_hash` (name-independent). Tier 2 -- a filename near-match (`filename_near_match`: date-free normalized stems equal, or a difflib ratio >= 0.9, after stripping `(1)`/`copy`/`copy of` modifiers and dates) AND a doc-level mean-pooled embedding cosine >= `DEFAULT_FUZZY_THRESHOLD` (0.95), compared only between docs with the same `embedding_sig`. Dates are not collapsed to a placeholder -- they are parsed out and compared separately: when both filenames carry a date, the closest pair must be within `_DATE_MATCH_MAX_DAYS` (7) or the fuzzy match is rejected, so distinct instances of a series that share a title (e.g. `...Apr 2026` vs `...Dec25`) do not collapse. Month-only dates pin to mid-month, so adjacent months land ~30 days apart and are rejected while a few-days drift (including across a month boundary) still matches; extraction uses non-alphanumeric boundaries so underscore-delimited names (`Status_Apr_2026`) are not missed. The filename gate is an AND on Tier 2 -- it can only narrow matches, never create them.
- **Document granularity**: one folder file (a `folder_file_state` row) for folder sources, the whole source for upload/chat sources. All chunk rows of a document share one `content_hash`.
- **Priority** (`pick_winner`): a persistent source (`PERSISTENT_SOURCE_TYPES` = `local_folder`/`obsidian_vault`/`quip`) beats a transient one (upload/chat); within a class the newest `mtime` wins; on an `mtime` tie the oldest-resident copy wins.
- **Action**: the losing document is hard-deleted -- `delete_source_cascade` for a whole-source doc, or `delete_items_batch` + its `folder_file_state` row for a folder file. The file on disk is never touched; there is no soft-supersede and no resurrection.
- **Triggers**: ingest-time after a successful whole-source ingest (`IngestionPipeline._maybe_dedup`, gated by `dedup_enabled`, default on); a sweep at the end of a `FolderWatcher` scan that ingested/changed files; and a one-time backfill via the CLI/MCP tool. All call the same idempotent `dedup_sweep`.
- **Surfaces**: MCP tool `knowledge_dedup` (`apply` bool, default false = dry-run preview; schema `KNOWLEDGE_DEDUP_SCHEMA`) and CLI `kirocrew knowledge dedup [--apply]` (dry-run by default). Both emit SEL audit events and redact credentials/exfiltration URLs in their output.

#### `kirocrew-core`: `send_message` Tool

Sends a message to the user via Slack DM and dashboard notification. Exposed as MCP tool in `mcp_core.py`, backed by `POST /api/send-message` in `handlers.py`.

- **Parameters**: `text` (required), `title` (optional), `blocks` (optional), `session` (optional: `"origin"` or `"slack"`), `channel` (optional), `user` (optional)
- **Delivery** (default, no session/channel/user): dashboard notification only via `state.notify()`
- **session="slack"**: Slack DM + dashboard notification
- **session="origin"**: inject into the dashboard session that spawned this cron. Falls through to notification-only if origin is unreachable.
- **Security**: blocks content is deep-walked through `redact_exfiltration_urls()` and `redact_credentials()` via `_sanitize_blocks()`, truncated to 50 blocks with recursion depth limit
- **Response**: `{ok: true, slack: bool, session: bool}`
- **Primary use case**: silent cron jobs where the agent controls notification timing

#### Silent Cron Flow

1. User (or agent) creates a cron job with `silent: true` via `cron_add`
2. Cron timer fires → agent session runs the job message as normal
3. Gateway skips auto-delivery (no Slack post, no dashboard notification)
4. Agent processes the result, applies judgment (e.g. "nothing changed, skip")
5. When the agent decides the user should know, it calls `send_message` with the relevant output
6. `send_message` → `POST /api/send-message` → dashboard notification (default) or Slack DM (if `session="slack"`)
- Toggle syncs to `kirocrew.json` `tools`/`allowedTools` for consistency
- ACP `session/new` passes `mcpServers: []` (required field); `set_mode` activates the agent
- MCP server init drain: 10s after `set_mode`/`set_model` to wait for all servers to load
- Drain logs loaded MCP server names at INFO level

### Context Window Usage

The Agents page context window section shows per-session info:
- Agent name (purple) for custom agents, hidden for kirocrew
- Model read from agent config file when `_model` is "auto" (custom agents)
- `agent` field in API response from `GET /api/sessions/context`

### Security Enforcement

`_enforce_denied_commands()` in `agent.py`:
- Injects `deniedCommands` from bundled defaults into ALL agent configs (security)
- Runs at install, gateway startup, and every ~60s
- MCP server isolation removed — kiro-cli ACP ignores per-agent `disabled` overrides; control is centralized in global mcp.json

### Build & Development

- **Dev mode**: `./dev-frontend.sh` runs Vite dev server on port 3000 with API proxy to backend on 5476
- **Production build**: `./build-frontend.sh` (called by `setup.py` during `pip install`) runs `tsc -b && vite build`, outputs to `src/kiro_crew/static/dist/`
- **Static serving**: `server.py` serves `/assets` from `dist/assets/` (Vite hashed bundles), `/static` from the static dir (theme images, logo)
- **Missing-bundle behavior**: if `static/dist/index.html` is absent, `handlers.py` (`index()`) serves a static "not found" guidance page (restart/rebuild hint). The legacy `static/dashboard.html` server-rendered fallback was removed — Talos V2285871874 (stored-XSS follow-up); the React SPA is the only shell.

### Resilience

- **Standalone fallbacks**: Memory and Skills APIs work even without `ContextBuilder` — handlers create standalone `MemoryStore` and `SkillsLoader` instances pointing to `~/.kirocrew/` defaults
- **Agent config path discovery**: `_find_agent_config()` searches `$KIROCREW_PROJECT_DIR` → `~/.kirocrew/project_dir` saved path → package-relative fallback
- **Static system info caching**: Hostname, OS, arch, CPU count, total memory computed once; only dynamic metrics (load, vm_stat, netstat) fetched per request

### Tool-Refusal Recovery

When `_run_chat` refuses a tool call for a **recoverable, system-side** reason —
a host-gate policy deny (`hooks.on_tool_call` → `TOOL_DENY`) or the read-only
bash safety gate (`is_read_only_bash` / `unsafe_bash_reason`) — kiro-cli ends
the turn early by emitting the attribution-free marker `Tool uses were
interrupted, waiting for the next user prompt`. Historically the refusal reason
reached only the dashboard pill and the SEL audit log, never the model, so the
agent stalled and the user had to manually prompt it to continue (and the model,
lacking any cause in its context, often misattributed the stop to the user).

`_run_chat` now records each recoverable refusal as a redacted `(title, reason)`
tuple in a per-turn `_refusal_reasons` list. When the turn ends — and the user
did **not** stop it (`slot._stopping` is false) and no session reset is already
re-queuing — it builds a continuation via `context.build_refusal_recovery_prompt`,
prepends `REFUSAL_RECOVERY_PREFIX`, and `queue_insert(0, …)`s it. The existing
finally-block dequeue loop renders it as an `inject` message (not a user bubble)
and re-dispatches it on the same session, so the model receives the reason and
can adapt (an allowed alternative, a different tool) or stop on its own with a
stated reason. The synthetic prompt is never mirrored to a linked Slack thread
as user input (`_is_recovery` guard).

By design there is **no retry cap**: the model decides when to stop, and the
user's Stop button remains the hard breaker (a stop clears the queue and aborts
the chain). Scope is the two reason-bearing gates above; pre-tool-use hook
`BLOCKED:` results are not yet wired for recovery (consistent treatment across
all three hook branches is a follow-up).

### Design

- OpenClaw-inspired: Space Grotesk + JetBrains Mono, dark/light theme with amber accent
- Tailwind CSS 3 with custom theme (`tailwind.config.js`) — design tokens as CSS custom properties, utility classes throughout
- **Typography scale**: body 14px, descriptions/details 14px (`text-sm`), labels/buttons/sidebar 13px, badges/captions 12px, decorative icons 10-11px. Minimum readable text: 11px. Code blocks: 13px mono. No text below 10px anywhere.
- CSS grid shell: topbar + nav sidebar + content area
- Nav sidebar: collapsible (220px full / 56px icon-only), prominent logo (frameless, radial gradient accent wash, 80px with drop-shadow)
- Animations: rise, slide-up, slide-in, scale-in, shimmer thinking bar, dot-breathe health indicator, brand-glow

### Custom Domain

`kirocrew setup` uses `kirocrew.localhost` (RFC 6761 reserved, resolves to loopback natively) for `http://kirocrew.localhost:5476` (macOS/Linux via `sudo tee -a /etc/hosts`). Gateway startup also prints the hostname URL for remote desktop access.

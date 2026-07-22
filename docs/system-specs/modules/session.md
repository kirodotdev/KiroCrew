# Session Manager Module

Last Updated: 2026-07-14 (cross-platform process management via platform_compat — Mesh-2329; warm pool / model precedence / orphan-sweep companion runtimes; DM channel session-key model + dm_scope + generation reset + mid-turn steer/queue; Slack thread linking, bidirectional dashboard-Slack sync, slash commands)

## Overview

Maps thread keys to LLMProvider instances (`session.py`). Each thread gets
its own kiro-cli session with idle expiry, context compaction, circuit
breaker, per-session semaphore, and persistent background session.

Chat sessions are served from the warm pool when eligible (default pool
agent, default cwd, no resume mapping); otherwise they cold-start on first
message via `get_or_create()`.

## Background Session

`BACKGROUND_KEY = "_bg"` is a persistent shared session for lightweight
background work. It is:

- **Created on startup** by `start_pool()` alongside the warm pool
- **Never expired** by idle cleanup (`_expire_idle` skips it)
- **Serialized** by the per-session semaphore (one background task at a time)
  — applies to the **non-kiro** `_bg` path only; see "Multiplexed _bg runtime"
- **Shared by**: heartbeat tasks, lesson extraction (NOT cron — see below)

This eliminates the cost of spawning/tearing down a kiro-cli process for
every cron job or heartbeat tick. Background tasks acquire the semaphore,
do their work, and release — the process stays warm.

### Context Overflow Protection

`recycle_background()` is called after every background task completes.
It checks context usage and **recycles** (kill + fresh spawn) the session
if needed — no compaction, since background tasks are stateless:

- At ≥ 70% context → recycle (more aggressive than chat's 90% compaction)
- After 20 prompts with no metadata → recycle (blind fallback)
- Below thresholds → no-op (session stays warm)

Callers: heartbeat callback, taskrunner lesson extraction.

### Multiplexed _bg runtime

`get_bg_session()` acquires a `_bg` handle, dispatching by provider backend and
returning `AcpSessionHandle | _ProviderBgSession`. Provider dispatch is via
`_bg_provider_is_kiro()`, which resolves the `kirocrew-lite` agent backend:

- **kiro (`acp`)** — the only backend the multiplexed `AcpRuntime` supports.
  Each caller (title generation, suggestions, folders, nav) gets its **own**
  ephemeral `sessionId` multiplexed on a single shared `_bg_runtime` (an
  `AcpRuntime`, kiro-cli only), created lazily under `_bg_runtime_lock`.
  `create_session()` runs **outside** the lock so independent callers aren't
  serialized. The runtime is respawned-and-retried once on `AcpRuntimeDead`
  (`max_retries=1`, 2 attempts total).
- **non-kiro** — falls back to a `_ProviderBgSession` over the shared
  `BACKGROUND_KEY` `_Session`, serialized by its `Semaphore(1)`. `AcpRuntime` is
  kiro-only, so any non-kiro backend must use the provider path. In the public
  KiroCrew edition `agent.provider` is fixed to `acp`, so this branch is the
  dormant fallback for the reserved `ACP_BACKEND_CLAUDE` seam only.

Both paths yield `AcpEvent` through the shared
`acp/_dispatch.parse_session_update` parser, so there is no behavioral drift
between them. Callers **MUST** call `session.destroy()` in a `finally` block
when done. See [acp-client.md](acp-client.md) for `AcpRuntime` /
`AcpSessionHandle`.

**Cheapest-model bg tasks**: the categorical/classification background tasks
force `claude-haiku-4.5` via a best-effort per-session `set_model` (guarded so
backends that can't switch fall through to their default): folder-icon
(`chat_folders.py`), link-summary (`chat_nav.py`), and lesson-contradiction
check (`dashboard/handlers/cron.py`).

## Key Behaviors

- **Context compaction**: at ≥ configured threshold (`session.autocompact_pct`, default 90%, valid 5–90), compacts **in place** on both
  backends: kiro-cli via native `/compact` (command execute +
  `_kiro.dev/compaction/status` wait), claude via SDK `/compact`. The
  process and session ID survive, so queued/agentic work continues
  automatically. kiro-cli only: if the in-place compact fails, times out,
  or the provider lacks native support, falls back to the legacy
  **recycle** (kill session; context re-injected via
  `build_session_context()` on next message). A recycle is never forced
  through a live turn — if the turn semaphore cannot be acquired within
  the budget, the attempt is deferred to the next turn-end check. Blind
  fallback after 40 prompts if metadata never reports %.
- **Circuit breaker**: force-resets session after 5 consecutive failures.
- **Dead provider detection**: `get_or_create()` checks `provider.is_alive()`
  on the fast path. If the backing process died (crash, SIGKILL, orphan
  cleanup), the stale session entry is removed and a fresh cold-start
  occurs with `is_new=True` — ensuring full context re-injection. Without
  this, the context builder would see `is_new=False` and skip episodic
  memory, leaving the new ACP process with zero history.
- **Per-session semaphore**: serializes concurrent messages on the same
  thread key. `get_or_create()` acquires; caller must `release()` when done.
- **Idle cleanup**: expires sessions after `session.timeout_secs` (default
  60min). Never expires `BACKGROUND_KEY`. Dashboard per-tab sessions
  (`dashboard:{slot_key}`) idle-expire like any other session.
- **Session Watchdog** (`watchdog.py`): the cleanup loop delegates its periodic
  behaviours to a `SessionWatchdog` — a stateless sequential dispatcher over
  named `CleanupHook(name, run)` entries (Command pattern; `tick()` isolates a
  hook failure with a debug-level backstop only, never promoting the severity
  of errors the lifted inline blocks swallowed). Hooks registered in
  `SessionManager.__init__`: `idle_expiry` (gate + clamped timeout published
  onto `self._idle_sweep_enabled`/`self._idle_timeout` by `_cleanup_loop`),
  `orphan_mcp` (maintenance-executor offload, Mesh-1968), `denied_commands`
  (re-enforcement offloaded to the maintenance executor — deliberate
  sync→thread change from the old inline block), and `rss_threshold`. The
  orphan-PID / session-root / sandbox-profile sweeps remain inline in
  `_cleanup_loop` (CR 2 extracts them).
- **RSS-threshold recycle** (`_rss_threshold_check`, config
  `session.watchdog_rss_max_mb`, default 0 = disabled): recycles non-busy
  sessions whose `/proc` process-tree RSS (MiB) exceeds the ceiling. Skips
  persistent (`_PERSISTENT_KEYS`) and `channel:`-prefixed keys — the same
  protected set as the idle sweep — and any session whose turn is in flight.
  The `/proc` parent→child map is built ONCE per tick off-loop
  (`_build_child_map` on the maintenance executor) and shared across
  candidate trees (`_rss_mb_from_tree`); resident pages are summed across the
  tree and converted to MiB once at the end. Measurement happens off-lock, so
  the victim's session object is captured at collection time and handed to
  `reset(expect_session=..., skip_if_busy=True)`, which re-verifies identity +
  not-busy atomically under the lock; a recycle that actually happened logs a
  warning, bumps `Stats().inc_session_cleaned()`, and fires the recycle
  callback (`set_recycle_callback` — mirrors the compact callback; wired by
  `dashboard/state.wire_session_recycle_callback()` from both `server.py`
  start paths to post a user-visible "session recycled" notice into
  `dashboard:` slots, tagged `meta={"kind": "compaction"}` so the [OPTIONS:]
  backward scan skips it). Idle/orphan sweeps do NOT fire the recycle
  callback. Linux-only measurement (`get_session_rss_mb` returns 0 elsewhere),
  so the feature is inert off-Linux.

## APIs

| Method | Purpose |
|--------|---------|
| `start_pool(blocking=True)` | Pre-spawn warm + background sessions. `blocking=False` for non-blocking mode. |
| `get_or_create(key, agent=None, approval_policy="")` | Returns `(LLMProvider, is_new, resumed)`. Uses warm pool for new sessions (default agent only). Sessions with a resume mapping skip warm pool (cold start needed for `session/load`). Non-default agents skip warm pool and resolve their model by precedence via `_model_fallback()` — caller model > per-agent pin > global default: `model=None` (defer to kiro's agent-JSON resolution) only when the agent pins its own model, otherwise the global default, unless that default is the `"auto"` sentinel (also `None`). The per-agent pin is resolved off the event loop via `run_in_executor` using `_resolve_named_agent_model`; blank agents inherit the global, and `kirocrew` is excluded (tracks the global). `approval_policy` is persisted on the new `_Session` — callers (e.g. subagent) pass parent policy so the session inherits it. |
| `check_context_usage(key, provider)` | Returns %. Triggers compaction at configured threshold (default 90%), warns at 75%. |
| `record_success(key)` / `record_failure(key)` | Circuit breaker tracking. |
| `release(key)` | Release per-session semaphore (must call in `finally`). |
| `cancel_current(key, *, wait_ack_timeout=0.0)` | Cancel in-flight operation without destroying session. Returns `CancelOutcome`. Default `wait_ack_timeout=0.0` preserves fire-and-forget behavior for internal callers (taskrunner, subagent, llm_helpers). |
| `stop_turn(key, *, force=False, on_soft=None, on_hard=None)` | Cooperative stop with kill fallback. Returns `StopOutcome` (`"soft"`, `"hard"`, or `"idle"`). Clears queue unconditionally, then sends `session/cancel` and waits up to `agent.soft_stop_budget_secs`; falls back to `reset()` + eager respawn on timeout or error. `force=True` skips cancel and goes straight to hard kill. `on_soft`/`on_hard` callbacks fire before return. |
| `reset(key, *, expect_session=None, skip_if_busy=False)` | Kill session; returns `bool` (True iff a session was actually torn down). Does NOT delete session map entry (kiro-cli file persists for future resume). Optional guards evaluated atomically under the lock with the pop, used by the RSS-recycle watchdog: `expect_session` only resets if that exact session object still occupies the key (guards against recycling a reset+recreated session on a stale off-lock RSS reading); `skip_if_busy` skips when the current session's semaphore is held so a live stream is never cut mid-turn. |
| `remove(key)` | Kill session AND delete session map entry (explicit tab delete — no resume expected). |
| `close_all()` | Save all active session mappings, then shut down every session and drain warm pool. |
| `warm_pool_size` | Property: number of warm sessions available. |

## Stop Orchestration

`stop_turn()` is the shared orchestration layer for both dashboard and Slack stop surfaces. Sequence:

1. `clear_queue(key)` — queue drop is unconditional on first press.
2. If `force=True`: skip cancel, go straight to hard kill (step 4).
3. Send `session/cancel` via `provider.cancel(wait_ack_timeout=budget)`:
   - `"acked"` → set `session.prev_turn_cancelled = True`, call `on_soft` callback, return `"soft"`.
   - `"no_turn"` → return `"idle"`.
   - `"timeout"` or `"error"` → fall through to hard kill.
4. Hard kill: `reset(key)` → fire-and-forget `_eager_respawn(key)` task → call `on_hard` callback → return `"hard"`.

### Cancelled-turn context restore

`_Session.prev_turn_cancelled` is a one-shot flag set on soft-cancel
success. The next prompt handler (dashboard `_run_chat`, Slack
`handle_message`) reads and clears it, then calls
`context.build_cancelled_turn_preamble(conversation_log, session_key)` to
re-inject the cancelled user prompt and partial assistant output. This is
necessary because kiro-cli discards cancelled turns from its own ACP
conversation log, so the LLM has no memory of the interrupted request.

### Eager Respawn

After a hard kill, `_eager_respawn(key)` calls `get_or_create(key)` in a background task so the next user message finds a warm session. On failure, logs at debug and does nothing — the next message triggers `get_or_create` again via the normal path.

## Session Resume (SessionMap)

Persistent mapping of `session_key → kiro_session_id` stored at
`~/.kirocrew/session_map.json`. Enables `session/load` to restore full
kiro-cli conversation history when a session is recycled.

**Only long-lived conversational sessions are mapped.** Stateless sessions
(cron, subagent, taskrunner, channel, secretary, side, heartbeat/background,
`wf-pool:` warm workflow-pool workers) are excluded via `_STATELESS_PREFIXES`.
The `wf-pool:` prefix keeps per-run pooled workers (workflows/agent_pool.py)
from persisting a session_map entry or resuming a prior transcript — their
hard-reset fallback must hand the next task a clean session, never a
`session/load` replay of the previous task's conversation. The `side:` prefix is included so
`/side` conversations never resume across KiroCrew restarts — each cold-start
triggers `is_first_turn=True` in `build_side_message` which re-seeds the
parent snapshot + accumulated side history.

**Lifecycle:**
- `get_or_create()`: looks up mapping → if found and `.json` file exists,
  sets `resume_session_id` on the ACP client and skips warm pool. After
  `ensure_ready()`, saves the new `session_key → session_id` mapping.
- `reset()`: does NOT delete mapping — the kiro-cli session file persists
  on disk. Next `get_or_create` will try `session/load`.
- `remove()`: deletes mapping — explicit tab delete, no resume expected.
- `close_all()`: saves all active mappings before killing processes.
- `start_pool()`: prunes stale entries (files deleted by kiro-cli GC).

### Cross-Provider Continuity

kiro session IDs and Claude Code session IDs are NOT interchangeable:
- kiro: arbitrary string, stored in `~/.kiro/sessions/cli/<sid>.{json,jsonl}`
- Claude Code: UUID v4, stored in `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`

When a user switches provider mid-session (e.g. config change from `acp` to
`claude_code`), conversation continuity is maintained via **history replay**,
never via session_id translation.

**Detection:** `detect_provider_switch(session_map, key, new_provider)` in
`session.py` compares the stored provider against the new one. Returns True
when a switch is detected (stored SID exists AND providers differ).

**Behavior on switch:**
1. `resume_sid` is discarded (not passed to the new provider process)
2. `SessionMap.clear_sid(key)` removes the stale SID from persistent state
3. `_Session.provider_switch_replay = True` flags the session for replay
4. The new provider's session_id (once obtained) is saved with the correct
   provider label
5. On the first prompt after the switch, `chat_runner` detects the flag and
   injects history from `compress_thread_history()` (KiroCrew's conversation_log)
6. The flag is consumed (set to False) — replay fires exactly once per switch

**Same-provider resume:** unaffected. Normal `session/load` path with full
native fidelity.

**Audit:** A `provider_switch_detected` SEL event is emitted with both the
stored and new provider names for observability.

**Atomic write:** tmp file + `os.replace()` prevents corruption on crash.

**Auto-prune:** `SessionMap.get()` auto-removes entries whose `.json` file
no longer exists. `SessionMap.prune()` bulk-removes all stale entries at
startup.

**Dashboard history key round-trip:** Session keys use `:` (e.g.
`dashboard:chat-1-xxx`) but JSONL filenames use `_safe_key()` which replaces
`:` with `_`. When a session is resumed from history, the slot name comes from
the filename stem (`dashboard_chat-1-xxx`), producing session key
`dashboard:dashboard_chat-1-xxx`. `SessionMap.get()` handles this by falling
back to the canonical form (`dashboard:chat-1-xxx`) when the direct lookup
fails.

**Slot-key filename normalization:** `get_or_create_slot()` folds every
caller-provided slot name to the `_safe_key()` filename charset
(`[A-Za-z0-9_\-.]`, via `_normalize_slot_key()` — `dashboard:`/`dashboard_`
transport-prefix strip mirroring `_history_key_for()`, then ASCII fold, then
filename fold), so a slot key always equals its persisted filename stem. Without this,
display-style slot names (e.g. `Artifact: My Doc` from the artifact iterate
flow) diverged from their sanitized filename: after a gateway restart,
`restore_open_slots()` rehydrated the raw key from `open_slots.json` while
`restore_recent_sessions()` derived a second slot from the filename stem,
producing duplicate sidebar sessions backed by one transcript.
`restore_open_slots()` and `_rehydrate_slot_from_history()` apply the same
fold on read so pre-fix snapshots carrying both key forms self-heal (the
second form hits the dedup guard). When normalization changes the name, the
original pretty form is preserved as the slot's initial title
(redaction-scrubbed, non-pinned so auto-title can still override).

## Slack Thread Linking

Sessions can be linked to Slack threads via `SessionMap` fields
`slack_thread_ts` and `slack_channel_id`. This enables bidirectional sync
between dashboard chat and Slack.

**API:**
- `SessionManager.set_slack_link(key, thread_ts, channel_id)` — persists to session map
- `SessionManager.get_slack_link(key) -> (thread_ts | None, channel_id | None)`
- `SessionManager.get_session_for_thread(thread_ts) -> key | None` — reverse lookup,
  keyed by the **bare** Slack `thread_ts`; returns the linked session key
  (canonical `slack:<ts>` for self-linked Slack threads, `dashboard:chat-N`
  for dashboard-linked threads)
- `SessionManager.set_channel(key, channel_id)` — backward-compat alias

**Slack handler:** calls `set_slack_link(session_key, reply_ts, channel)`
(where `reply_ts` is the bare Slack thread_ts and `session_key` is the
canonical `slack:<ts>` form) outside the `if is_new` guard so every message
refreshes the link.

## Slack Session-Key Alias Fold

Slack thread sessions have two historical key forms: the legacy bare
`thread_ts` (`"1783733803.877979"`) and the canonical namespaced form
(`"slack:1783733803.877979"`, `messaging/link.py`). The Slack handler derives
the canonical form at message entry (`canonical_key(thread_ts or msg_ts)`),
but legacy callers and persisted state may still present bare keys.

`SessionManager._fold_key(key)` resolves the two alias forms onto whichever
form is live in the in-memory registry (exact match → canonical alias →
legacy bare alias; unknown keys pass through unchanged, so non-Slack
namespaces are never rewritten). Every public key-taking method
(`get_or_create`, `has_session`, `get_provider`, `get_pid`, `release`,
`stop_turn`, `enqueue`/`dequeue`/queue helpers, `reset`, `remove`, `destroy`,
approval-policy accessors, `record_success`/`record_failure`,
`check_context_usage`, `cancel_current`, `is_provider_alive`) folds at entry.

Without the fold, the thread-index lookup (which returns canonical keys) and
a live session registered under the bare key disagree, so the second
in-thread message misses the live session, the disk resume is rejected by
kiro-cli ("Session is active in another process"), and a brand-new
context-free session silently splits the thread.

`ConversationLog._path()` applies the same back-compat: a canonical key whose
file doesn't exist yet falls back to the legacy bare-`thread_ts` filename
when that exists, so a thread active across the migration keeps one log file.

**Dashboard chat:** mirrors user messages to linked Slack threads via
`slack_client.post_message()`. The "Send to Slack" button (`slack/blocks.py`)
opens a DM thread, links the session, and posts the last 5 messages as context.

**Dashboard state:** `ChatSlot.summary()` includes `slack_linked: bool` so
the frontend can show a link indicator.

**Slash commands** (`slack/events.py`):
- `/kirocrew sessions` — lists active sessions with Slack link status
- `/kirocrew sessions resume <key>` — resumes a session in the current thread

**Block Kit builders** (`slack/blocks.py`): reusable Block Kit dict builders
for slash command UIs. Action IDs follow `mc_<command>_<action>[_<id>]`.

## DM Channel Session Keys & Mid-Turn Handling

DM channels (Telegram, WeCom) have no thread concept, so `messaging/link.py`
derives the session key with `build_dm_session_key(channel, agent, user, *,
gen, dm_scope)`:

- **Shape** (channel-first): `{channel}:{agent}:{chatType}:{user}` plus an
  optional `:gen{N}` suffix. The part before the suffix is a durable **bucket**
  (history and channel links hang off it); the **generation** rotates to start a
  fresh transcript within the bucket. `chatType` is `direct` today; `group` is
  reserved.
- **`dm_scope`** (`MessagingConfig.dm_scope`): `per-channel-peer` (default) —
  one bucket per `(channel, user)`; `unified` — all DMs collapse into a single
  `unified:{agent}` bucket for cross-surface continuity. `agent` is part of the
  bucket by design, so switching the configured agent starts a fresh session
  rather than replaying another agent's context.
- **Generation reset** rotates on `/new`, an idle window
  (`MessagingConfig.idle_reset_minutes`), or a daily boundary
  (`daily_reset_hour`), decided by `should_rotate_generation()`.
- **Restart-safe generation seeding.** The generation counter is in-memory (per
  `ConversationState`), so it resets on gateway restart. To stop `/new` from
  bumping a reset counter (0→1) straight onto a still-persisted generation and
  resurrecting that old conversation, the counter is seeded on first access to a
  bucket from the highest persisted generation via
  `SessionMap.max_generation(bucket)` (shared helper
  `messaging.link.seed_generation`, used by every DM dispatcher). A normal
  post-restart message then resumes the latest generation (continuity); `/new`
  always advances past every persisted generation, minting a genuinely fresh sid.

Legacy bare-thread Slack keys are unaffected — they keep the
`canonical_key`/`legacy_key` shim. The DM channels are recent, so the key shape
carries no prior persisted history to migrate.

### Mid-turn messages (steer / queue)

`SessionManager.is_busy(key)` reports whether a turn holds the session
semaphore. When a DM arrives mid-turn, the dispatcher acts on
`MessagingConfig.queue_mode`:

- `steer` (default): fold the message into the running turn via the provider's
  steer channel.
- `queue`: enqueue it — checked atomically against the semaphore, so a turn
  that finishes in the window runs the message instead of stranding it — and
  drain it after the turn, iteratively and capped (not recursively).

WeCom always steers regardless of `queue_mode`: its replies are bound to the
inbound request, so a queued-then-drained reply can't be delivered later
(capability-driven, like `supports_proactive_send=False`).

## Cross-Surface Reply Mirror

The same conversation can appear on a channel (Telegram/WeCom) and in the
dashboard. Two models relate the surfaces:

- **Slack — one session, two surfaces (fold-in).** A linked Slack thread folds
  into the dashboard session: the handler swaps the session key to the linked
  dashboard session via `get_session_for_thread`, so there is a single backing
  sid and Slack is a projection of it (see *Slack Thread Linking*).
- **Telegram / WeCom — two sessions, bridged by a mirror.** The channel message
  runs under its own channel session (`{channel}:…:genN` → its own sid); the
  dashboard surfaces it as a separate slot with its own sid. One logical
  conversation therefore has two backing sids, bridged by the mirror.

`messaging.link.dashboard_mirror_key(channel_session_key)` computes the
dashboard-side key: `"dashboard:" + history._safe_key(channel_session_key)`. It
MUST use the same `_safe_key` sanitizer as the slot-naming path (every non-word
char → `_`, not only `:`); a narrower sanitizer silently mismatches for keys
containing spaces/unicode, so the mirror never fires despite `/link` succeeding.

**Directions.** Inbound (channel → dashboard display) is independent of the
mirror link and always on — the channel turn writes the shared `conv_log`, which
the dashboard rehydrates as a slot. Outbound (dashboard → channel echo) fires
only when a `mirror` `ChannelLink` exists on the dashboard-side key:

```
   Telegram / WeCom                              Dashboard tab
  ┌────────────────────┐   inbound: ALWAYS ON   ┌────────────────────┐
  │ channel session    │ ═════════════════════▶ │ dashboard slot     │
  │ …:genN  (sid A)    │                        │ dashboard:…_genN   │
  │                    │ ◀── outbound: only ──  │ (sid B)            │
  └────────────────────┘      when /link is ON   └────────────────────┘
```

**API:**
- `SessionManager.set_mirror_link(key, link)` / `clear_mirror_link(key)` /
  `get_mirror_link(key)` — persist/read the outbound `ChannelLink` (Slack routes
  to `set_slack_link` so its reverse index stays intact).
- `POST /api/chat/slots/{name}/mirror-link` | `mirror-unlink` — dashboard-side
  endpoints (auth posture matches `slack-link`: under the `/api/chat`
  `mixed_internal_paths` prefix, never the strict `internal_paths` set).
- In-channel `/link` / `/unlink` — write/clear the link on the current
  conversation's `dashboard_mirror_key`. `/link` does not control display,
  history, or the inbound direction — only the outbound echo; `/unlink` changes
  nothing else, since the two sids already exist independently.

**Delivery** (`chat_runner._deliver_cross_surface_reply` /
`_deliver_cross_surface_user_message`, via the shared `_resolve_mirror_target`
preamble) is best-effort and gated on: Slack skipped (its own inline mirror); a
registered transport with `supports_proactive_send` (WeCom is False → `/link`
rejected there); and the `channels` governance ceiling via
`governance_permits("channels", channel_type)`, so an operator policy
restricting outbound messaging is honored on this egress too (fail-closed on any
governance error — matching the Slack path). Egress text is redacted through the
canonical `redact_via_context` shim so a loaded companion's extra
credential/token regexes apply.

**Known asymmetry / future work.** Slack already runs the unified one-session
model; Telegram/WeCom run two sessions bridged by the mirror. Folding the
dashboard channel tab into the channel session (as Slack does) would remove the
second sid and the live render-duplication it can cause, at the cost of a
dashboard-turn-loop refactor.

## Session Lifecycle at Startup

```
start_pool()
  ├── _enforce_denied_commands()  → inject deniedCommands into ALL agent configs
  ├── _spawn_warm() × pool_size   → warm pool queue (instant assignment)
  └── _ensure_background()        → BACKGROUND_KEY session (persistent)
```

## Security: deniedCommands Enforcement

`_enforce_denied_commands()` (from `agent.py`) injects the bundled `deniedCommands`
patterns into agent configs in `~/.kiro/agents/`. The scope is controlled by
`agent.enforce_denied_commands` config option:

- `"all"` (default): enforce on ALL agent configs (kirocrew + AIM + third-party)
- `"kirocrew"`: only enforce on `kirocrew.json`, skip other agents (lite agents always skipped)

This addresses user complaints about KiroCrew overwriting customizations on non-KiroCrew agents every ~60 seconds.

- **At startup**: `start_pool()` calls it before spawning any sessions
- **Periodic**: `_cleanup_loop()` calls it every ~60s (catches manual edits)
- **At install**: `install_agent()` calls it after writing `kirocrew.json`
- **Mtime-based**: skips unchanged files for efficiency
- **Merge semantics**: union of existing + bundled patterns (never removes agent's own)
- **Targets**: both `execute_bash` and `shell` tool settings
- **Config**: set via `~/.kirocrew/config.json` or Dashboard Config Summary

## Orphaned MCP Server Cleanup

`_cleanup_orphaned_mcp_servers()` kills MCP server processes that survived
session teardown.  kiro-cli-chat spawns MCP servers (kiro_crew mcp-core/cron,
builder-mcp, andes-mcp, aim slack-mcp) in separate process groups.  When a
session dies, `killpg` only reaches the kiro-cli process group — MCP servers
in other groups get reparented to init and leak memory.

**Tracking**: at session init, `AcpClient.ensure_ready()` snapshots all
descendant PIDs and persists them to `kiro_pids.txt` as `child_pid:parent_pid`
pairs via `_track_child_pids(pids, parent_pid=self._pid)`.  On clean shutdown,
`_reset_state()` removes them via `_untrack_child_pids()`.  If the gateway
crashes, the entries remain in the file for the next startup.

**Detection**: reads `kiro_pids.txt`, processes only `child:parent` lines
(bare PID lines are kiro-cli parents handled by `cleanup_orphaned_sessions()`).
If the child is alive but its parent PID is dead, the child is orphaned and
killed.

**Why not ancestor walk?** MCP servers are spawned in separate process groups
and immediately reparented to init (ppid=1) even while the session is alive.
Walking the process tree would always conclude they are orphaned.  Storing the
parent PID explicitly avoids this.

**Safety**:
- Zero false positives — only kills PIDs we tracked, only when the specific
  parent session that spawned them is confirmed dead
- Dead children are silently pruned from the file
- Bare PID lines (kiro-cli parents) are ignored by MCP cleanup

**Invocation**:
- **At startup**: `cleanup_orphaned_sessions()` calls it after PID-file cleanup
- **Periodic**: `_cleanup_loop()` calls it alongside idle session expiry (~60s)
- **At shutdown**: `cleanup_orphaned_sessions()` on signal/exit

### Orphan Sweep Active Set

The periodic sweep of `kiro_session_pids.txt` (which kills tracked kiro-cli
PIDs no longer in `self._sessions`) builds its active set as the union of
`_collect_active_pids(self._sessions)` + `_pool_pids()` + `_in_flight_pids()`
+ `_companion_runtime_pids()`, re-checked against the same union in phase 2
before any kill. `_companion_runtime_pids()` returns the live PIDs of
`self._subagent_runtimes` (companion runtimes multiplexing a parent session's
subagents) and `self._bg_runtime` (the multiplexed `_bg` runtime), each guarded
on `is_alive()` — only alive runtimes are shielded, so dead ones are still
reaped.

**Failure it fixes**: since the `AcpRuntime` unify, *every* runtime records its
PID in `kiro_session_pids.txt` at spawn. These two runtime kinds live outside
`self._sessions`, so before this union the sweep saw their live PIDs as
untracked orphans and SIGKILLed them mid-chat (surfacing as
`process exited (rc=-9)`).

### Cross-platform process management (platform_compat)

All process liveness/kill/PID-file-lock operations in `session.py` and
`session_pid.py` go through `kiro_crew.platform_compat` so KiroCrew runs natively on
Windows as well as macOS/Linux (Mesh-2329). The critical correctness reason is that
**`os.kill(pid, 0)` is NOT a liveness probe on Windows — it terminates the process** —
so every liveness check uses `platform_compat.pid_exists(pid)` (or the tri-state
`pid_liveness`) instead, kills use `kill_pid` / `kill_process_tree`, the PID-reuse
guard reads the parent via `get_ppid`, the managed-agent check uses
`process_matches(pid, ("kiro-cli","claude"))`, and the PID-file locks use
`platform_compat.file_lock` / `acquire_lock` / `try_acquire_lock` (POSIX `flock`
vs Windows `msvcrt`). On POSIX the behavior is unchanged.

## Resource Budget (Gateway Mode)

| Session | Key Pattern | Lifetime | Process |
|---------|-------------|----------|---------|
| User chat | `slack:{thread_ts}` (legacy bare `{thread_ts}` folded) | Idle timeout (60 min) | Own kiro-cli |
| Dashboard tab | `dashboard:{slot_key}` | Idle timeout (60 min) | Own kiro-cli (from warm pool) |
| Cron job | `cron:{job_id}` | One-shot (reset after) | Own kiro-cli (from warm pool) |
| Background | `_bg` | Entire runtime (recycled at 70%) | Shared kiro-cli |
| Heartbeat | `_bg` | Shared | Shared kiro-cli |
| Lesson extract | `_bg` | Shared | Shared kiro-cli |
| Subagent | `subagent:{uuid}` | Task duration | Own kiro-cli |
| TaskRunner step | `taskrunner:{task_id}:step{N}` | Step duration (reset after) | Own kiro-cli (max 2 concurrent via semaphore) |
| TaskRunner decompose | `taskrunner:{task_id}:decompose` | Seconds | Own kiro-cli |
| TaskRunner review | `taskrunner:{task_id}:review` | Seconds | Own kiro-cli |
| TaskRunner acceptance | `taskrunner:{task_id}:acceptance` | Seconds | Own kiro-cli |
| Warm spare | _(in pool queue)_ | Until assigned | Pre-started kiro-cli |

**Cold-start semaphore**: `_start_sem = Semaphore(2)` limits concurrent
`provider.start()` calls to 2 for memory safety. This
prevents resource exhaustion when multiple sessions cold-start simultaneously,
while still allowing 3 parallel subagents to all run concurrently once started
(they queue briefly during cold-start).

**Parallel step throttling**: TaskRunner limits concurrent step sessions
to `max_parallel_steps` (default 2) via `asyncio.Semaphore`. Cold starts
are staggered by 3s. A system load guard pauses spawning when CPU load
exceeds 85% of available cores.

## Compaction Race Handling

In-place compaction (both backends) keeps the `_sessions` entry healthy:
a concurrent `get_or_create()` reuses it, queueing on the session
semaphore behind the compact, then continues on the compacted session.

Only the kiro-cli recycle fallback tears the entry down. It records the
exact session object under teardown in `_recycling` (distinct from
`_compacting`, which is just the trigger dedup gate): `get_or_create()`
skips reuse only when the map still holds that exact object, then
cold-starts fresh — a healthy replacement registered under the same key
during the teardown is reused normally, never overwritten. The recycle
pops by object identity — if a racing cold-start already replaced the
entry, only the old session object is shut down; the fresh replacement
and its session_map entry survive (the old provider is still reaped so
its process never leaks).

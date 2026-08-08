# Session Manager Module

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
(folder-icon `chat_folders.py`, link-summary `chat_nav.py`, session title
`chat_title.py`, session-summary `handlers/sessions.py`, STT endpointing
`stt_stream.py`, and the lesson-contradiction check `dashboard/handlers/cron.py`,
plus tips generation) express a `"auto"` model preference and pass it to a
best-effort per-session `set_model`. The wire chokepoint
(`AcpSessionHandle.set_model` → `resolve_usable_model`) mirrors the interactive
`_wire_model_id`: it sends a served id, sends `"auto"` only when the backend
advertises it, and for anything else — `"auto"` where a partition doesn't serve
it, or an unentitled concrete id — resolves to `""` and **skips the
send**, inheriting the session's served backend default. So these tasks never
put an unserved model or a literal unavailable `"auto"` on the wire (which would
fail with `Invalid model ID`). A reactive retry in `run_bg_oneliner`
(retry once with the first advertised model on a mid-prompt rejection) remains a
thin backstop for the fail-open case where the advertised set was unknown at
send time.

## Key Behaviors

- **Empty-response recovery ladder** (dashboard chat runner, depth-0 turns
  only): a completed turn with no visible output, no refusal reasons, and no
  cancellation is treated as a transient provider failure and recovered
  through a bounded three-rung ladder driven by `slot._empty_response_retries`:
  1. **first empty** → the ORIGINAL message is silently re-queued at the
     front of the slot queue (no visible card);
  2. **second empty** (the same-message retry also produced nothing) → ONE
     synthetic continue nudge (`_EMPTY_AUTO_CONTINUE_MSG` — a DIFFERENT
     message, since re-sending the identical prompt tends to reproduce the
     identical empty generation) is queued on the SAME live session, with a
     transcript-visible notice card ("auto-continuing once"). Gated by
     `session.empty_response_auto_continue` (default ON; the gate fails open),
     and suppressed while a Stop is active;
  3. **third empty** (the nudge also produced nothing) → terminal notice card
     asking the user to send a message; the counter resets so the next
     genuine user turn gets a fresh budget.
  Recovery rungs 1–2 skip persistence/consolidation/success-recording (the
  empty turn is never saved) and preserve all other retry budgets. Synthetic
  recovery messages (`_SYNTHETIC_RECOVERY_MSGS`: the post-transient CONTINUE
  instruction and the empty-response nudge) are excluded from the
  genuine-new-turn allowance reset, so a recovery turn can never refresh its
  own budget; on the queue-drain path they classify as **recovery**
  STRUCTURALLY — ``queue_insert`` tags the entry ``kind="synthetic_recovery"``
  and every queue consumer (merge predicate, sub-agent hold, drain-role
  assignment, reset-notice consumption) dispatches on that metadata, never on
  content equality, so classification survives queue transformations and a
  user pasting the recovery text verbatim still classifies as plain user
  speech. The transcript append uses the `inject` role (never `user`, so an
  internal orchestration instruction is never persisted as user-authored
  history or mirrored to linked channels), draining one does not cancel a
  pending synthesis, and the tag is merge-breaking so a nudge is never folded
  into a `[N queued messages merged]` user turn. At `_prompt_depth > 0` the ladder is disabled entirely (terminal
  notice on the first empty) to prevent nested-turn re-queue loops.
- **Context compaction**: at ≥ configured threshold (`session.autocompact_pct`, default 90%, valid 5–90), compacts **in place** on both
  backends: kiro-cli via a `/compact` **prompt** (`session/prompt` +
  `_kiro.dev/compaction/status` watch — never the string form of
  `_kiro.dev/commands/execute`, which kiro-cli 2.14.0 exits rc=0 on),
  claude via SDK `/compact`. The
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
- **Post-semaphore revalidation** (`_reacquire_and_validate`): the per-session
  semaphore may be held for a full turn, so it is ALWAYS acquired with the
  global `self._lock` RELEASED (pinning the lock across that wait would freeze
  session creation for every key and reintroduce a lock-ordering deadlock).
  Because a session can be recycled/removed or its backing process can die
  while a caller waits on the semaphore, every reuse path re-checks identity +
  liveness AFTER acquiring it, through the single shared helper
  `_reacquire_and_validate(key, sess)`. Its contract: it returns `True` with
  the semaphore **still held** (caller MUST `release`), or `False` having
  **already released** it (session went stale — caller evicts via
  `_evict_stale_session` and cold-starts). Cancellation while parked on
  `self._lock` after the acquire releases the semaphore before propagating, so
  the key never stays permanently locked. Liveness uses
  `_provider_effectively_alive` (a dead Claude-Code `per_session` process
  counts as alive — it reconnects lazily on the next `stream()`).
  Consolidating this acquire→relock→revalidate dance in ONE place is
  deliberate: a divergent copy is exactly how the stale-provider bug class gets
  reintroduced. ALL three multiplexing reuse paths route through it — the
  `get_or_create` fast path, its won-by-another-coroutine race path, and
  `open_task_session` (both its fast path AND its lost-race branch, where a task
  step that loses the registration race would otherwise wait a turn on the
  winner's semaphore and be multiplexed onto a recycled/dead runtime). A stale
  winner triggers a bounded cold-start retry (`_WON_RACE_MAX_RETRIES`). The
  only bare `semaphore.acquire()` sites are: the helper itself; a
  brand-new session the caller just created and registered (no recycle window);
  and `try_acquire` (a non-blocking, no-`await`-suspension atomic take used by
  out-of-band `/compact`, which returns `False` on contention rather than
  waiting, so it has no stale-while-waiting window).
- **Agent-model resolution cache** (`_resolve_agent_model`, class-level
  `_agent_model_cache`): the per-agent model pin resolved from agent JSON is
  cached but invalidated on BOTH the agents-dir mtime changing (a new agent
  JSON appearing bumps the dir mtime) AND a TTL (`_AGENT_MODEL_CACHE_TTL`, for
  in-place edits that leave the dir mtime unchanged). Without invalidation an
  early `"auto"` miss (agent JSON not yet present) would be pinned forever, so a
  later create/edit of the agent config would never be observed.
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
  `orphan_mcp` (maintenance-executor offload), `denied_commands`
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
| `get_or_create(key, agent=None, approval_policy="")` | Returns `(LLMProvider, is_new, resumed)`. Uses warm pool for new sessions (default agent only). Sessions with a resume mapping skip warm pool (cold start needed for `session/load`). Every decision is counted via `_record_pool_decision` (`kirocrew.session.pool.decision`) with the single disqualifying reason, so the pool's hit rate and the frequency of the `bypass_resume` case are observable. Non-default agents skip warm pool and resolve their model by precedence via `_model_fallback()` — caller model > per-agent pin > global default: `model=None` (defer to kiro's agent-JSON resolution) only when the agent pins its own model, otherwise the global default, unless that default is the `"auto"` sentinel (also `None`). The per-agent pin is resolved off the event loop via `run_in_executor` using `_resolve_named_agent_model`; blank agents inherit the global, and `kirocrew` is excluded (tracks the global). `approval_policy` is persisted on the new `_Session` — callers (e.g. subagent) pass parent policy so the session inherits it. |
| `check_context_usage(key, provider)` | Returns %. Triggers compaction at configured threshold (default 90%), warns at 75%. |
| `record_success(key)` / `record_failure(key)` | Circuit breaker tracking. |
| `release(key)` | Release per-session semaphore (must call in `finally`). |
| `cancel_current(key, *, wait_ack_timeout=0.0)` | Cancel in-flight operation without destroying session. Returns `CancelOutcome`. Default `wait_ack_timeout=0.0` preserves fire-and-forget behavior for internal callers (taskrunner, subagent, llm_helpers). |
| `stop_turn(key, *, force=False, on_soft=None, on_hard=None)` | Cooperative stop with kill fallback. Returns `StopOutcome` (`"soft"`, `"hard"`, or `"idle"`). Clears queue unconditionally, then sends `session/cancel` and waits up to `agent.soft_stop_budget_secs`; falls back to `reset()` + eager respawn on timeout or error. `force=True` skips cancel and goes straight to hard kill. `on_soft`/`on_hard` callbacks fire before return. |
| `reset(key, *, expect_session=None, skip_if_busy=False)` | Kill session; returns `bool` (True iff a session was actually torn down). Does NOT delete session map entry (kiro-cli file persists for future resume). Optional guards evaluated atomically under the lock with the pop, used by the RSS-recycle watchdog: `expect_session` only resets if that exact session object still occupies the key (guards against recycling a reset+recreated session on a stale off-lock RSS reading); `skip_if_busy` skips when the current session's semaphore is held so a live stream is never cut mid-turn. |
| `remove(key)` | Kill session AND delete session map entry (explicit tab delete — no resume expected). |
| `close_all(drain_timeout=None)` | Pre-shutdown **drain** of in-flight turns (via `drain_active_turns`), then save all active session mappings, shut down every session, and drain the warm pool. `drain_timeout` bounds that drain (`None` = full default budget); a caller wrapping `close_all()` in its own hard deadline (Slack's restart wraps it in `wait_for(..., 5s)`) passes a smaller budget (e.g. `2.0`) so the kill path still fits inside the deadline. A cancel that fires mid-drain (outer deadline) **propagates** (CancelledError is deliberately not caught) so the caller's hard deadline stays honest; recovery of a still-held native-session lock is the next-startup orphan reaper's job. |
| `drain_active_turns(timeout=None)` | Best-effort co-operative drain that brings in-flight prompts to a safe turn boundary **before** teardown, so kiro-cli closes its native turn and releases its session lock (`~/.kiro/sessions/cli/<uuid>.json`) on the subsequent SIGTERM — otherwise the next gateway's `session/load` hits "active in another process" and the slot returns empty completions (the Make-Live empty-response incident, #200). For each registered session with an **unfinished** turn (native turn-done not yet acked — independent of cancel state, so an already-cancelled-but-not-acked turn is still drained), it issues a graceful `session/cancel` and waits (bounded) for the ack; a turn already cancelled (`cancel()` → `"no_turn"`) is waited on directly via `wait_turn_done`. The whole operation is bounded by `timeout` (`None` → `_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS`, default 5.0s; internal cap is `timeout+1.0`); on timeout it logs and returns so the caller falls through to the SIGTERM-first kill path — never hangs teardown, never raises. `timeout <= 0` disables the drain. Returns the count of unfinished turns (observability/tests). Only registered user sessions are drained; the warm pool holds never-prompted processes. |
| `begin_turn(key)` | **Synchronous** pre-dispatch gate against the lease-dispatch race (#200 / Codex HIGH). A caller holds the per-session semaphore *lease* from `get_or_create` through the whole turn, but the native turn only opens on the first `provider.stream(...)` iteration; the `get_or_create` `_closing` gate cannot revoke a lease already issued before `close_all` set `_closing`. Callers (dashboard `chat_runner`, Slack handler) MUST call `begin_turn` synchronously — **no `await` between it and the `async for` stream drive** — so the `_closing` read and the stream's turn registration (`AcpClient.stream_events` clears `_turn_done` before its first `await`) form one yield-free span, strictly ordered w.r.t. `close_all`'s `_closing` set: the turn is either registered before the drain snapshot (and drained) or the caller aborts. Raises `SessionClosingError` (a `RuntimeError`) when closing; the caller's `finally` releases the lease. Deliberately NOT `async`/lock-guarded (an `await` would reopen the race). |
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
`~/.kiro/crew/session_map.json`. Enables `session/load` to restore full
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

### Load Recovery (stale native session lock — F2)

On restart / Make-Live cutover the previous gateway's kiro-cli is killed. If it
died uncleanly (SIGKILL, crash, OOM, or a drain timeout), its per-session lock
can stay held briefly, so the new gateway's `session/load` is rejected with an
**"active in another process"** error. Recovery happens at the resume
chokepoint (`AcpProvider._load_session_with_retry`, `providers/acp.py`) and
self-heals regardless of *why* the resume failed — it never depends on the dead
holder cooperating (unlike cooperative drain), so it covers every kill mode:

1. **Phase 1 — bounded retry (lossless).** Re-issue `session/load` up to
   `_RESUME_MAX_ATTEMPTS` (4) times with exponential backoff
   (`_RESUME_BACKOFF_BASE_S` → 1s, 2s, 4s). If the stale lock releases, the
   session resumes with full native history. A genuine (non-lock) load error is
   **not** retried, and a dead runtime aborts the loop immediately (the caller's
   respawn path takes over).
2. **Phase 2 — fresh session + history replay (backstop).** If the lock never
   clears, `_start_kiro_runtime_impl` falls through to a fresh `session/new` and
   sets `AcpProvider._history_replay_needed`. `get_or_create` reads that flag and
   sets `_Session.provider_switch_replay = True`, so `build_session_replay`
   injects KiroCrew's `conversation_log` into the new native session on the first
   prompt (the same replay path used for cross-provider switches). The slot
   resumes seamlessly instead of returning empty completions.

Observability: a successful Phase-1 recovery logs at INFO; exhausting all
attempts logs a single grep-able WARNING before migrating to Phase 2.

### Cross-Provider Continuity

kiro session IDs and the removed provider's session IDs are NOT interchangeable:
- kiro: arbitrary string, stored in `~/.kiro/sessions/cli/<sid>.{json,jsonl}`
- removed provider: UUID v4, stored in `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`

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

**Mapped-session enumeration:** `SessionMap.mapped_sids_by_key()` returns session
key → kiro-cli session ID for every entry that has one. Disk accounting
([session-storage](session-storage.md)) needs both halves of that relation: the IDs
to exclude from reclaiming (a mapped session is resumable), and the key each ID
belongs to so a session's transcript can be paired with its replay log. Returning
the mapping rather than only the ID set is what lets a caller reclaim a session
whole instead of leaving one half behind.

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

The same conversation can appear on a channel and in the
dashboard. Two models relate the surfaces:

- **Slack — one session, two surfaces (fold-in).** A linked Slack thread folds
  into the dashboard session: the handler swaps the session key to the linked
  dashboard session via `get_session_for_thread`, so there is a single backing
  sid and Slack is a projection of it (see *Slack Thread Linking*).
- **Discord / Telegram / Webex / Teams / WeCom / Weixin — two sessions, bridged by a mirror.** The channel message
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
   Messaging channel                            Dashboard tab
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
- `SessionManager.clear_mirror_links_at(link)` — value-keyed sweep: clears
  EVERY session whose mirror targets that exact non-Slack location and returns
  the cleared keys. The write counterpart of `find_mirror_sessions`, and the
  only clear that reaches a binding stranded under a key spelling the
  conversation no longer derives (a rotated DM generation, a pre-unification
  `dashboard:` row).
- `POST /api/chat/slots/{name}/mirror-link` | `mirror-unlink` — dashboard-side
  endpoints (auth posture matches `slack-link`: under the `/api/chat`
  `mixed_internal_paths` prefix, never the strict `internal_paths` set).
  New links use `{channel_type, target_id}` and resolve the opaque configured
  target server-side; the legacy `{conversation_id, thread_id?}` body remains
  accepted for compatibility. A successful new link posts an anchor plus the
  last five redacted messages before persisting the mirror.
- `GET /api/chat/channel-targets` — owner-authenticated union of Slack
  destinations and every registered transport's configured targets. The
  dashboard session menu renders this list with per-channel brand icons.
  Unavailable configured destinations are returned with a reason rather than
  silently omitted (Teams before first inbound; WeCom proactive send); the menu
  keeps those rows keyboard-focusable, shows the reason inline, and announces
  the same reason instead of presenting an unexplained disabled action.
- In-channel `/link` / `/unlink` — `/link` writes the link on the current
  conversation's `dashboard_mirror_key`; it does not control display, history,
  or the inbound direction — only the outbound echo. `/unlink` frees the
  LOCATION via the shared `messaging.link.release_conversation_location`
  helper (one implementation for every DM dispatcher): after the key-addressed
  clears it sweeps every binding whose mirror targets this conversation
  (`clear_mirror_links_at`), including a binding stranded under a rotated DM
  generation and another dashboard session's outbound mirror into the
  conversation — the same occupant set the Discord resume conflict check
  refuses on, so its "Run `!unlink` first" guidance is always followable. The
  reply reports the count when more than one binding was cleared.

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
model; the other transports run two sessions bridged by the mirror. Folding the
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
- **Config**: set via `~/.kiro/crew/config.json` or Dashboard Config Summary

## Orphaned MCP Server Cleanup

`_cleanup_orphaned_mcp_servers()` kills MCP server processes that survived
session teardown.  kiro-cli-chat spawns MCP servers (kiro_crew mcp-core/cron,
the internal MCP server, slack-mcp) in separate process groups.  When a
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

### session_pid sidecar contract (`session_pid_sig.py`)

The gateway maps its direct child pid to a session key by publishing
`config_dir()/session_pid_<pid>.txt` on session claim (writers:
`dashboard/chat_runner.py`, `slack/handler.py` — both route through
`session_pid_sig.publish_session_pid`, the single legitimate publish path).
Because the `.txt` lives in the same-uid agent-writable config dir it is NOT
a trust root on its own; publication therefore also writes a
`session_pid_<pid>.sig` sidecar:

- **MAC**: HMAC-SHA256 over `"<pid>:<session_key>"` — the pid is bound into
  the MAC so one pid's pair cannot be replayed under another pid.
- **Key**: a purpose-specific subkey derived from the SEL trust root via a
  domain-separation label (`HMAC(sel_hmac.key, "kirocrew.session_pid.sig.v1")`).
  The raw root never signs a sidecar; the sidecar protocol and the SEL audit
  chain never share a signing key (see `sel.md`). Only `SecurityEventLog`
  ever *creates* the key file.
- **Writes are atomic** (`atomic_write` → `os.replace`): a pre-planted
  symlink at the predictable paths is replaced, never followed.
- **Consumers**: STRICT identity resolvers accept the direct
  `KIROCREW_HOST_PID` → mapping lookup only via
  `session_pid_sig.verify_session_pid`, which fails closed to `""` on a
  missing/short key, missing files, or MAC mismatch. Their remaining callers
  are the computer-use MCP tools (`mcp_computer.py`, for audit attribution)
  and the dashboard messaging-identity path (`dashboard/handlers/messaging.py`).
  The former state-mutating session-bound tools that resolved identity here —
  `monitor_start`, `monitor_update`, `autonudge_stop`, `set_project` (plus
  `suggest_followup` and `ask_question`) — became STATELESS directive-return
  tools in issue #755 (see "Stateless session-directive tools" below); they
  still call the strict resolver, but only as a context guard, and no longer
  bind any effect to the key it returns. Lenient (read-only)
  resolvers keep reading the `.txt` without a signature check, but through
  the same hardened reader (`session_pid_sig.read_session_pid_txt`:
  no-follow, regular-file, size-bounded) — `session_pid_sig` owns both the
  read and write discipline for the file family. Every `.txt` reader routes
  through it: `mcp_core._resolve_session_key` (host-pid + walk),
  `mcp_shared._resolve_excluded_tools` (policy walk),
  `mcp_caller.CallerContext.from_env` (host-pid + walk; also serves
  `mcp_gateway/stub.py`), and `mcp_gateway/gatewayd._resolve_peer_identity`
  (server-side peer walk). The sidecar is additive.
- **Unsigned degrade**: if the SEL key is unavailable at publish time the
  `.txt` is still written (lenient readers keep working) and any stale
  sidecar is removed — strict resolvers fail closed for that pid.
- **Key rotation**: rotating/regenerating `sel_hmac.key` (e.g. snapshot
  restore, which deliberately excludes the key) invalidates every existing
  sidecar; strict resolvers fail closed until the next turn's publish
  re-signs the mapping. Benign and self-healing — no migration step.
- **Stale cleanup**: the orphan sweep removes `session_pid_<pid>.sig`
  alongside its `.txt` for dead pids (`session_pid.py`).
- **Threat model** (full version in the `session_pid_sig.py` module
  docstring): file forgery, cross-pid replay, tampering, and symlink
  planting are blocked; deliberate same-uid impersonation via
  attacker-chosen env in self-launched processes is out of scope (identical
  capability exists against env-only resolution) and is tracked as the
  SO_PEERCRED gateway-authentication follow-up (issue #302).

### Stateless session-directive tools (`session_directive.py`, #755)

Six session-bound MCP tools — `monitor_start`, `monitor_update`, `autonudge_stop`, `set_project`, `suggest_followup`, `ask_question` — used to resolve their OWN session identity (the strict sidecar resolver above) and call a loopback HTTP endpoint, which only produced a usable per-call caller when MCP-gateway **pooling** was enabled. They are now **stateless**: the tool validates its arguments and returns a *directive* — a human-readable confirmation line plus a machine-readable marker (`session_directive.encode`) carrying the validated payload and NO session key. The session-aware consumer, `dashboard/chat_runner._run_chat`'s `EVENT_TOOL_RESULT` handler, decodes the marker (`session_directive.decode`) and applies the effect IN-PROCESS against ITS OWN `slot`/`session_key` via `dashboard/session_directive_apply.py`, then strips the marker from the stored transcript. This works with pooling OFF (the default) because the consumer already owns the session, so no per-process identity source is needed.

Subagent isolation is therefore **structural, not cryptographic**: a subagent's tool result flows through the subagent's own runner and can only ever bind to the subagent's session, never its parent's — there is no `/proc` walk to get wrong. The tools still call `_resolve_session_key_strict()`, but only as a context guard to short-circuit sessions where a directive can never be applied (cron/hook/subagent) and to steer non-`dashboard:` `ask_question` callers to the `[OPTIONS:]` tag — not to bind the effect.

Security properties (enforced in `session_directive.decode` plus the applier):

- **Forgery gate keyed on canonical identity**: because the marker is model-visible (it returns as the tool-result text), a directive is honoured ONLY when the tool call was recorded — via kiro-cli's out-of-band `_meta` channel — as an MCP call whose canonical `_meta.kiro.toolName` (with `_meta.kiro.mcpServerName` set) is in `DIRECTIVE_TOOLS`, never the LLM-authored `title`. A shell command titled `monitor_start` whose stdout forges the marker resolves to no directive tool and is ignored; the gate fails closed when `_meta` identity is absent.
- **Native sub-agent calls refused**: they surface as flat events in the parent loop but have no independently bindable slot, so the applier declines them.
- **SEL audit on every application**: `apply_session_directive` emits a tool-invocation event tagged `source="mcp-directive"` with outcome `success` / `denied` (e.g. a `set_project` sensitive-path block) / `error`, since the effect now runs in the consumer rather than in the tool body or an HTTP endpoint.

The applier reuses the SAME effect cores the HTTP endpoints call — `authorize_and_add_nudge` / `authorize_and_update_nudge` / `svc.remove` for the monitor trio, `slot.project` plus the recent-projects save for `set_project`, `deliver_ws_owners` for `suggest_followup`, and `post_question_card` for `ask_question` — so behavior is unchanged except that `ask_question` is now non-blocking (full contract in `learn-cron-dashboard.md` → "Agent Questions").

Gateway-off (the default topology this targets), the model's tool result is the tool's OWN returned line delivered over kiro-cli's MCP pipe; the applier's confirmation string and SEL audit are recorded on KiroCrew's own surfaces (transcript / WS / hooks) and do NOT rewrite the model's tool result. Each tool therefore phrases its own message as a *request* that the consumer applies (and may refuse — no interactive session, invalid/sensitive path, capped/paused loop) rather than asserting the effect already landed.

```mermaid
sequenceDiagram
    participant M as Model
    participant T as MCP tool (kirocrew-core)
    participant R as chat_runner._run_chat<br/>(EVENT_TOOL_RESULT)
    participant A as session_directive_apply
    M->>T: call e.g. monitor_start(args)
    T->>T: validate args (resolves NO session identity for the effect)
    T-->>R: tool result = human line + directive marker
    R->>R: decode(result, canonical _meta.kiro.toolName)
    Note over R: forgery gate — canonical name in DIRECTIVE_TOOLS,<br/>not the LLM title; native sub-agent calls refused
    R->>A: apply_session_directive(slot, session_key, kind, args)
    A->>A: run effect core against the consumer's OWN slot
    A-->>R: confirmation string + SEL audit (source="mcp-directive")
    R->>R: strip marker from stored transcript
```

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
Windows as well as macOS/Linux. The critical correctness reason is that
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

Only the kiro-cli failure recycle tears the entry down, and it runs inside
`_compact_in_place` under the turn semaphore that the compact attempt
already holds — never after releasing it. That is load-bearing: releasing
first and re-acquiring for the recycle leaves a gap a queued turn wins, and
that turn is then dispatched into a kiro-cli still finishing its compaction,
receives the late `completed` status instead of an `end_turn`, and hangs
holding the semaphore until the prompt timeout.

The recycle records the
exact session object under teardown in `_recycling` (distinct from
`_compacting`, which is just the trigger dedup gate): `get_or_create()`
skips reuse only when the map still holds that exact object, then
cold-starts fresh — a healthy replacement registered under the same key
during the teardown is reused normally, never overwritten. The recycle
pops by object identity — if a racing cold-start already replaced the
entry, only the old session object is shut down; the fresh replacement
and its session_map entry survive (the old provider is still reaped so
its process never leaks).

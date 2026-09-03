# Session Manager Module

## Overview

Maps thread keys to LLMProvider instances (`session.py`). Each thread gets
its own kiro-cli session with idle expiry, context compaction, circuit
breaker, per-session semaphore, and persistent background session.

Chat sessions are served from the warm pool when eligible (default pool
agent, default cwd, no resume mapping); otherwise they cold-start on first
message via `get_or_create()`.

## Implementation Boundaries

`SessionManager` remains the compatibility facade in `session.py`; callers keep
using its existing public, import, and monkeypatch seams. Mutable policy and
state are composed behind that facade:

- `session_allocation.py` — live registry, key folding, semaphore leases,
  cold-start allocation, claims, and companion runtimes
- `session_pool.py` — warm-provider spawn, inventory, claim, health, and discard
- `session_background.py` — persistent and multiplexed background runtimes
- `session_compaction.py` — context gates, compaction execution, verdicts,
  cooldowns, and guarded escalation
- `session_lifecycle.py` — refresh/reload, reset/remove/destroy/discard,
  identity retirement, stop, drain, and close ordering
- `session_cleanup.py` — cleanup-task state, watchdog hooks, idle/RSS/stuck-turn
  policy, and process/filesystem sweeps

Cross-boundary calls that were observable on `SessionManager` route back through
the facade, and patchable module dependencies are resolved through injected
call-time functions. Persistence remains owned by the existing `SessionMap`
contract; this decomposition does not change its stored format.

The owner protocols and forwarding dependency adapters are transitional
compatibility boundaries, not extension points. New internal behavior belongs
on the service that owns its state; widen a protocol or adapter only when an
existing facade/import/monkeypatch seam requires it. Individual adapters may be
retired in follow-up changes after repository-wide callers and characterization
tests have moved off the corresponding legacy seam.

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

- At ≥ 70% context → recycle (same threshold as chat's default compaction)
- After 20 prompts with no metadata → recycle (blind fallback)
- Below thresholds → no-op (session stays warm)

Callers: heartbeat callback, taskrunner lesson extraction.

### Multiplexed _bg runtime

`get_bg_session()` acquires a `_bg` handle, dispatching by `agent.acp_backend`
and returning `AcpSessionHandle | _ProviderBgSession`. Dispatch is via
`_bg_backend_supports_runtime()` — positive membership in
`ACP_BACKENDS_ACP_RUNTIME`, never an inequality (harness parity):

- **runtime-capable backend** (`ACP_BACKENDS_ACP_RUNTIME`) — each caller (title
  generation, suggestions, folders, nav) gets its **own** ephemeral `sessionId`
  multiplexed on a single shared `_bg_runtime` (an `AcpRuntime` spawned under
  the CONFIGURED backend), created lazily under `_bg_runtime_lock`.
  `create_session()` runs **outside** the lock so independent callers aren't
  serialized. The runtime is respawned-and-retried once on `AcpRuntimeDead`
  (`max_retries=1`, 2 attempts total).
- **any other backend** — falls back to a `_ProviderBgSession` over the shared
  `BACKGROUND_KEY` `_Session`, serialized by its `Semaphore(1)`. In the public
  Kiro Crew edition `agent.provider` is fixed to `acp` and only kiro and KAS are
  selectable, so this branch is the dormant fallback for the reserved
  `ACP_BACKEND_CLAUDE` seam only.

A backend switch displaces the cached `_bg_runtime`. The displacement policy
has ONE implementation, `_displace_bg_runtime_locked()`, reached from
`_retire_stale_backend_bg_runtime()` and from the mismatch check inside
`get_bg_session()`'s runtime branch: a runtime whose `acp_backend` no longer
matches config is killed if idle, and **parked on `_draining_bg_runtimes` if it
has live or initializing handles** — parked runtimes never receive a new
session (only `_bg_runtime` is offered to callers), their in-flight work
finishes untouched (killing mid-turn would abort an in-flight title
generation), and `_reap_drained_bg_runtimes_locked()` kills each once its last
handle drains. Either way the slot is freed, so the very next background call
runs under the configured backend even while the old runtime is still
draining. Parked runtimes stay shielded from the orphan-PID sweep
(`_companion_runtime_pids`), block the account-identity sweep's completeness
(`_retire_kiro_bg_runtime`) while they drain, and are reaped by a periodic
watchdog hook (`bg_drain_reap`) as the backstop for an idle gateway where no
other trigger runs. `close_all()` detaches both holders atomically under
`_bg_runtime_lock` and kills the detached snapshot; its counterpart `_closing`
gate in `get_bg_session()` refuses to spawn or park once shutdown has started.
Note there is currently no dashboard edit surface for
`agent.acp_backend` (a file/CLI edit lands at the next gateway start, where
`_cfg` is fresh); `refresh_defaults()` re-reads config, so any invocation of it
picks up a backend change, and a future edit surface gets retirement for free
by routing through it like the other `agent.*` defaults. The provider-path
retirement trigger is dormant in the public edition for the same reason the
`ACP_BACKEND_CLAUDE` branch is: every selectable backend is runtime-capable.

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
- **Leaked tool-call notice** (dashboard chat runner, depth-0 turns only,
  issue #6112): a turn that ends normally with an invoke block emitted as
  TEXT and zero tool calls executed — the model wrote its invocation into the
  prose channel instead of dispatching it (observed with deferred MCP tools
  whose schema is not yet bound, and with large nested arguments) — surfaces
  a visible notice card and is marked un-landed (no success recording, no
  budget reset, no consolidation), so an unattended monitor/autonudge cycle
  that leaked never lands silently. Detection is machine-shaped
  (`chat_utils.has_leaked_tool_call`): an unquoted invoke open tag plus a
  parameter or close tag, with fenced code blocks and inline code spans
  stripped first so a pasted transcript or explained example never matches;
  the gate (`should_notice_leaked_tool_call`) is claimed ahead of the
  promise-only guard and excludes stage-execution turns (the orchestrator's
  stage loop reads the turn result for stage accounting). Deliberately **notice-only** — no continuation is
  queued, because an injected "re-issue that call" would carry runtime
  authority into sessions where the call auto-approves (slot trust, global
  yolo, or a static agent tool allowlist, the last invisible at the runner
  layer, so no fail-closed downgrade condition exists) and the leaked block
  may be untrusted external content the model merely reproduced. A loop loses
  one cycle, visibly, and retries on its own schedule. Scope limit: the
  notice/un-landing applies only to ZERO-tool-call turns — a mixed turn that
  executed tools and then leaked its final dispatch as text lands normally
  (un-landing a turn whose earlier calls had real side effects would
  misdescribe it) and is logged at WARNING as a diagnostic instead.
- **Context compaction**: at ≥ configured threshold (`session.autocompact_pct`, default 70%, valid 5–90), compacts **in place** on both
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
  the budget, the attempt is deferred to the next turn-end check. A
  compaction whose IMMEDIATELY-MEASURED effect verdict (a confirmed reading
  taken right after the attempt, showing a real but < 5-point drop) is still
  ≥ `_POST_COMPACT_RESET_PCT` (95%) escalates to a reset with the native
  resume sid cleared in the same tick as the pop
  (`reset(clear_conversation=True)` via `_reset_still_critical` — promoted
  from the task runner's post-check, #4686). Deferred (next-reading) verdict
  settles are deliberately damping-only: that reading includes the following
  turn's own growth, so it cannot distinguish a failed compaction from a
  successful one regrown by a large turn — it arms the cooldown but never
  resets. The escalation is AWAITED (the `compact_if_needed` seam returns
  `"reset"`), performed BEFORE the compaction callback fires (the callback
  awaits arbitrary surface I/O, and a turn completing inside that window
  must not be erased by a verdict measured before it ran), pins the measured
  session's identity (`expect_session`) so a stale escalation never destroys
  a replacement registered under the same key, and honors `skip_if_busy` (a
  declined reset maps back to `"ok"`; the still-critical session re-attempts
  the whole compact-and-escalate cycle at its next threshold crossing after
  the cooldown, with the mid-stream overflow guard covering the interim).
  Blind
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
  later create/edit of the agent config would never be observed. The scan reads
  each spec through `agent_discovery._read_agent_spec`, the hardened reader that
  module documents as the one reader for both agent scopes: `~/.kiro/agents` is
  user-writable and shared with kiro-cli, so the read is size-capped and refuses
  a link resolving onto a sensitive target rather than resolving a model out of
  whatever the link names. A refused spec is skipped like a malformed one, so
  the resolution falls through to `"auto"` exactly as an absent spec does.
- **Idle cleanup**: expires sessions after `session.timeout_secs` (default
  60min). Never expires `BACKGROUND_KEY`. Dashboard per-tab sessions
  (`dashboard:{slot_key}`) idle-expire like any other session.
- **Session Watchdog** (`watchdog.py`): `SessionCleanup` owns the cleanup-loop
  state and delegates named periodic behaviours to a `SessionWatchdog` — a
  stateless sequential dispatcher over `CleanupHook(name, run)` entries
  (`tick()` isolates a hook failure with a debug-level backstop only, never
  promoting the severity of errors the lifted inline blocks swallowed). The
  hooks are assembled through the `SessionManager` facade so existing
  monkeypatch seams remain observable: `idle_expiry`, `orphan_mcp`,
  `rss_threshold`, `stuck_turn`, and `bg_drain_reap`.
  `SessionCleanup._cleanup_loop` then directly coordinates the session-root,
  sandbox-artifact, bytecode-cache, periodic tracked-PID, and untracked-MCP
  sweeps.
- **Stuck-turn reporting** (`_stuck_turn_check`, threshold
  `_STUCK_TURN_REPORT_SECS` = 300s, not configurable): reports a turn whose
  consumer has stopped pulling events. Exists because the per-turn watchdog in
  `acp-client.md` cannot report on itself — it is the `TimeoutError` arm of an
  async generator, so a consumer awaiting inside its own `async for` body
  freezes the generator and that arm never runs again for the turn, which is why
  such a turn emits no stall WARNING at all. This loop has its own timer and no
  dependency on any consumer. Considers only sessions whose semaphore is held
  (the only in-flight signal at this layer), reads `parked_for_secs()` /
  `parked_since` / `awaiting_permission` duck-typed off `provider._handle` so any
  transport growing those accessors is covered, and latches on the park's
  monotonic start so a park outliving the tick is reported once rather than every
  pass. **Detection only**, deliberately: a turn awaiting a human is excluded
  because `agent.tool_approval_timeout_secs` already bounds that wait; ending a
  live turn stays with the in-band path that owns the terminal-event seam and the
  non-lethal continue-nudge; and what the park is blocked on is not knowable from
  here. Logs at WARNING and fires the optional `on_stuck_turn(key, parked_secs)`
  callback — a seam so a surface that can reach the user decides what to do,
  keeping the session layer free of any dashboard import. Swallows its own errors
  like its sibling hooks. The reasoning behind putting this check here rather
  than in the read loop — the placement criterion, what a hook may honestly read
  at this layer, and how out-of-band action stays clear of the in-band recovery
  path — is recorded in
  `../../architecture/design-notes/tool-stall-watchdog-placement.md`.
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
| `get_or_create(key, agent=None, approval_policy="", speculative=False, speculative_resume=False)` | Returns `(LLMProvider, is_new, resumed)`. Uses warm pool for new sessions (default agent only). Sessions with a resume mapping skip warm pool (cold start needed for `session/load`). A `reasoning_effort_override` also skips the warm pool (`bypass_effort`): a pre-warmed provider was built without the override and post-claim fixups never touch effort, so the override must reach a fresh provider-factory call to be delivered — which also keeps the factory's effort gate the single authority reporting a dropped level. Every decision is counted via `_record_pool_decision` (`kirocrew.session.pool.decision`) with the single disqualifying reason, so the pool's hit rate and the frequency of the `bypass_resume` case are observable. Non-default agents skip warm pool and resolve their model by precedence via `_model_fallback()` — caller model > per-agent pin > global default: `model=None` (defer to kiro's agent-JSON resolution) only when the agent pins its own model, otherwise the global default, unless that default is the `"auto"` sentinel (also `None`). The per-agent pin is resolved off the event loop via `run_in_executor` using `_resolve_named_agent_model`; blank agents inherit the global, and `kirocrew` is excluded (tracks the global). `approval_policy` is persisted on the new `_Session` — callers (e.g. subagent) pass parent policy so the session inherits it. `speculative=True` (eager spawn) pre-creates ahead of a real first turn: the one-shot `_Session.first_turn` observation — a single three-member `FirstTurnState` enum (`NOTHING_ARMED` / `FRESH` / `RESUMED`), so a resume marker on an already-claimed session is unrepresentable rather than forbidden by convention — is registered ARMED (`FRESH`) and never consumed by speculative callers, and a resumable key raises `SpeculativeResumeRefused` — unless `speculative_resume=True` (resume prefetch) opts in, in which case the speculative creator performs the `session/load` and registers the observation as `RESUMED` when the load restored the transcript. The observation is consumed in one read-then-clear by the first real claimant under the per-session semaphore (fast path and won-race path alike), with the returned booleans derived from it at the return boundary — so that turn observes `(is_new=True, resumed=True)` exactly as if it had resumed itself, preserving its history-injection decision. |
| `get_or_create(..., wait_if_busy=False)` | Optional non-waiting reuse for structured monitor delivery. It raises `SessionBusyError` at the semaphore claim boundary instead of waiting; same-key cold-start races apply the same rule. Channel callers pass the exact generation authorized by the gateway and refuse if it changes before this claim, rather than recomputing a newer conversation key. The default remains `True` for every ordinary caller. |
| `check_context_usage(key, provider)` | Returns %. Triggers compaction at configured threshold (default 70%), warns one `CONTEXT_WARN_MARGIN_PCT` below it. |
| `compact_if_needed(key)` | Awaitable twin of the `check_context_usage` trigger for callers that must not start their next turn while a compaction is pending (the task runner's between-steps check, #4686). Same gates in the same order — both entry points consume the shared `_compaction_gate_decision` ladder, the single owner of the gate order (its docstring documents each rung) — then AWAITS `_compact_session`. Returns the outcome: `"absent"`, `"reset"` (the settled verdict on the prior attempt was ineffective-and-still-critical and the promoted escalation reset the session here, awaited), `"cc_managed"` (checked before the threshold, mirroring `check_context_usage`), `"below_threshold"`, `"unconfirmed"`, `"in_progress"`, `"cooldown"`, `"ok"`, `"busy"`, `"recycled"`, `"failed"`. A `"busy"` decline means a turn holds the semaphore — the caller leaves the session alone and retries later, never falls back to a direct `provider.compact()`. |
| `record_success(key)` / `record_failure(key)` | Circuit breaker tracking. |
| `release(key)` | Release per-session semaphore (must call in `finally`). |
| `cancel_current(key, *, wait_ack_timeout=0.0)` | Cancel in-flight operation without destroying session. Returns `CancelOutcome`. Default `wait_ack_timeout=0.0` preserves fire-and-forget behavior for internal callers (taskrunner, subagent, llm_helpers). |
| `stop_turn(key, *, force=False, on_soft=None, on_hard=None)` | Cooperative stop with kill fallback. Returns `StopOutcome` (`"soft"`, `"hard"`, or `"idle"`). Clears queue unconditionally, then sends `session/cancel` and waits up to `agent.soft_stop_budget_secs`; falls back to `reset()` + eager respawn on timeout or error. `force=True` skips cancel and goes straight to hard kill. `on_soft`/`on_hard` callbacks fire before return. |
| `reset(key, *, expect_session=None, skip_if_busy=False, clear_conversation=False)` | Kill session; returns `bool` (True iff a session was actually torn down). Does NOT delete session map entry (kiro-cli file persists for future resume). Optional guards evaluated atomically under the lock with the pop, used by the RSS-recycle watchdog: `expect_session` only resets if that exact session object still occupies the key (guards against recycling a reset+recreated session on a stale off-lock RSS reading); `skip_if_busy` skips when the current session's semaphore is held so a live stream is never cut mid-turn. `clear_conversation=True` additionally clears the native resume sid in the SAME event-loop tick as the pop (entry + channel bindings survive, as in `_recycle_held`) — used by the still-critical post-compaction escalation so the overflowed conversation is not reloaded, without a delayed clear ever erasing a racing successor's sid. |
| `discard_conversation(key)` | Kill session AND clear only the resume sid (`SessionMap.clear_sid`) — the map ENTRY survives, preserving Slack thread/channel linkage and the reverse thread→session index. The cleared sid is stashed as `discarded_sid` in the entry, so the discard is diagnosable and manually reversible (the native conversation persists on disk; only the pointer is dropped). The next turn cold-starts a fresh native conversation instead of `session/load`-ing the old one. Used by the poisoned-conversation escalation in `chat_runner` (canary-verified backend rejection of a specific persisted conversation) and by the Slack / Discord / Telegram `/compact` failure recovery: the conversation is unusable but the session's channel identity must persist. This is the shape every HOUSEKEEPING teardown takes — `SessionMap.prune` refuses to delete an entry carrying a channel binding, and `_recycle_held` clears the sid for the same reason. Only an explicit user action (`destroy`) may remove a channel identity. Sits between `reset` (sid kept, resume expected) and `remove` (entry deleted, no resume). |
| `remove(key)` | Shut down a session but PRESERVE the session map entry — the kiro-cli session files remain on disk, so a future `get_or_create` restores the conversation losslessly via `session/load`. For revivable teardown (tab close, agent switch, idle kill). Permanent deletion is `destroy(key)`. |
| `remove_if_unclaimed(key)` | Conditional `remove` for the resume-prefetch TTL: removes the session only if the one-shot `first_turn` observation is still armed (not `NOTHING_ARMED` — no real turn claimed it) AND the per-session semaphore is unheld, checked atomically under the manager lock. Preserves the session map (mirrors `remove`'s revivable shape), so the next focus or first message resumes normally. Returns `True` iff a session was removed. A claimant handed the session object but not yet holding the semaphore loses benignly: its re-validate fails and it cold-starts. |
| `close_all(drain_timeout=None)` | Pre-shutdown **drain** of in-flight turns (via `drain_active_turns`), then save all active session mappings, shut down every session, and drain the warm pool. `drain_timeout` bounds that drain (`None` = full default budget); a caller wrapping `close_all()` in its own hard deadline (Slack's restart wraps it in `wait_for(..., 5s)`) passes a smaller budget (e.g. `2.0`) so the kill path still fits inside the deadline. A cancel that fires mid-drain (outer deadline) **propagates** (CancelledError is deliberately not caught) so the caller's hard deadline stays honest; recovery of a still-held native-session lock is the next-startup orphan reaper's job. |
| `drain_active_turns(timeout=None)` | Best-effort co-operative drain that brings in-flight prompts to a safe turn boundary **before** teardown, so kiro-cli closes its native turn and releases its session lock (`~/.kiro/sessions/cli/<uuid>.json`) on the subsequent SIGTERM — otherwise the next gateway's `session/load` hits "active in another process" and the slot returns empty completions (the Make-Live empty-response incident, #200). For each registered session with an **unfinished** turn (native turn-done not yet acked — independent of cancel state, so an already-cancelled-but-not-acked turn is still drained), it issues a graceful `session/cancel` and waits (bounded) for the ack; a turn already cancelled (`cancel()` → `"no_turn"`) is waited on directly via `wait_turn_done`. The whole operation is bounded by `timeout` (`None` → `_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS`, default 5.0s; internal cap is `timeout+1.0`); on timeout it logs and returns so the caller falls through to the SIGTERM-first kill path — never hangs teardown, never raises. `timeout <= 0` disables the drain. Returns the count of unfinished turns (observability/tests). Only registered user sessions are drained; the warm pool holds never-prompted processes. |
| `begin_turn(key)` | **Synchronous** pre-dispatch gate against the lease-dispatch race (#200 / Codex HIGH). A caller holds the per-session semaphore *lease* from `get_or_create` through the whole turn, but the native turn only opens on the first `provider.stream(...)` iteration; the `get_or_create` `_closing` gate cannot revoke a lease already issued before `close_all` set `_closing`. Callers (dashboard `chat_runner`, Slack handler, and structured Slack/Discord monitor adapters through `TurnDriver.closing_gate`) MUST call `begin_turn` synchronously — **no `await` between it and the `async for` stream drive** — so the `_closing` read and the stream's turn registration (`AcpClient.stream_events` clears `_turn_done` before its first `await`) form one yield-free span, strictly ordered w.r.t. `close_all`'s `_closing` set: the turn is either registered before the drain snapshot (and drained) or the caller aborts. Raises `SessionClosingError` (a `RuntimeError`) when closing; the caller's `finally` releases the lease. Deliberately NOT `async`/lock-guarded (an `await` would reopen the race). |
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
`wf-author:` workflow authoring, and `wf-pool:` warm workflow-pool workers) are
excluded via `_STATELESS_PREFIXES`. A `wf-author:` session is also explicitly
destroyed after each authoring attempt, which shuts down its provider, removes its
registry entry, and deletes any stale map entry; stateless classification prevents
resume lookup or persistence during acquisition. The `wf-pool:` prefix keeps
per-run pooled workers (workflows/agent_pool.py) from persisting a session_map entry
or resuming a prior transcript — their hard-reset fallback must hand the next task
a clean session, never a `session/load` replay of the previous task's conversation.
The `side:` prefix is included so
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

### Asking for a fresh conversation on a slot that stays open

`POST /api/chat/slots/{slot}/reset-conversation` drops one slot's resume pointer
through the LIVE manager (`discard_conversation`), so its next turn cold-starts
instead of `session/load`-ing the accumulated conversation. The slot stays open,
the transcript stays on disk, and the map ENTRY survives with its channel
linkage.

This closes a gap rather than adding a capability: resume is key-driven and a
slot key is stable by design, which is correct for a tab reopened later and wrong
once a long-lived conversation has drifted, filled up, or outlived what it was
about. The only reachable way to break the link was `DELETE /api/sessions/{key}`,
which destroys the record in order to reset the pointer — so "start over" and
"erase this" were the same button.

**`replay` is what the caller means by "fresh", and it has to be asked for.**
Clearing the sid stops the provider resuming its own conversation — and "the
provider has no history" is precisely the condition that makes the next cold
start rebuild one from `conversation_log` as a `[CONVERSATION HISTORY]` block
(`chat_runner`, injected OUTSIDE the capped session context). So the two
mechanisms work against each other by construction: the caller discards the
conversation and the next turn is handed a reconstruction of it. Measured on one
app-owned session, that replay was 80,359 characters — 76% of the first turn's
injected context, and most of what discarding the conversation was meant to
reclaim. `discard_conversation(key, replay=False)` records a ONE-SHOT
suppression, consumed at the replay gate inside the cold-start branch so a warm
turn cannot spend it, and the route threads it from an optional `replay` field on
the request body. The default is `True`, which keeps every existing caller and
the dashboard's own copy ("Conversation history is preserved — your next message
starts a fresh process") true. Only the RE-INJECTION is suppressed: the
transcript is untouched, so the conversation stays readable in the dashboard and
on disk.

The flag cannot live on the session object the way `needs_context_reinjection`
does, because `discard_conversation` POPS that session — the decision is made by
the turn that tears the conversation down and acted on by the next turn, which
builds a new one. It is therefore a manager-level set, process-scoped on purpose:
a gateway restart also cold-starts the session, but there the replay is
legitimate, since nobody asked for a fresh conversation and re-anchoring is what
that surface has always done. Every teardown path that already clears the
compaction cooldown clears it too (`reset`, `remove`,
`retire_kiro_identity_sessions`, `remove_if_unclaimed`, `destroy`, and
`close_all`), because slot keys ARE reused and a leaked flag would starve the
NEXT holder of that key of its re-anchor.

Three properties the route holds, each of which fails silently if broken:

- The key comes from `effective_session_key(slot)`, never a derived
  `dashboard:<slot>`: a channel-born slot's turns run on the channel's session,
  and the derived form yields a key no session ever had — the clear finds nothing
  and the call still reports success.
- `discard_conversation`, never `destroy`: the entry carries the Slack
  thread/channel linkage and the reverse index built from it.
- It is nonetheless a FULL teardown (provider shutdown plus
  `release_subagent_runtime`), so it takes the same guards the sibling `reload`
  route does, through the same shared helpers rather than a third policy:
  `_app_cancel_denied` on the resolved SESSION key, `provider.has_active_turn()`,
  `slot.running` widened with `slot._in_stage_execution`, and
  `_subagents_attached_response`. Each protects work invisible from outside — a
  turn on the session with no dashboard task behind it (an inbound channel
  message, which `slot.running` cannot see), a turn mid-write, a plan between
  stages, and children still running after their parent's turn ended.
  The four probes above are best-effort fast paths; the authoritative guard is
  the fifth, `discard_conversation(..., skip_if_busy=True)`, which probes the
  per-session SEMAPHORE atomically with the session pop and refuses with the
  same `turn_in_flight` 409. It closes the edge the fast paths share: a turn
  holding the semaphore before its prompt is in flight is invisible to
  `has_active_turn`. This is the same contract the sibling reload route rests
  on, so the two teardowns keep one notion of "busy". Of the route's refusal
  paths, only the atomic one emits a SEL `denied` record — it is the sole
  refusal that occurs after the route has committed to the teardown; the
  fast-path 409s are pre-checks and stay unlogged, as they are on the sibling.

Authorization is `_app_cancel_denied`, not a slot-ownership check, and that
distinction is load-bearing: `get_or_create_slot` resolves `linked_session_key`
from the session map for a name shaped like a channel stem, so an app that names a
live channel thread ends up OWNING a slot bound to a conversation it has no claim
on. Ownership alone would let it wipe that channel conversation's resume pointer.
The helper tests the key the caller will actually act on, and runs BEFORE the 409s
so a refusal cannot confirm the slot exists. Reaching the route needs
`/api/chat/slots` in the app's manifest `permissions.api`, and the capability it
grants is strictly smaller than the delete it already implies.

The transcript is deliberately left in place, so the tab still shows earlier
messages the model no longer remembers. That is the honest rendering — the record
is the user's, the context was the conversation's — and it is why this is an
explicit request rather than something the gateway does on its own.

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

**Deferred flush (event loop only):** a mutation made on the event loop marks
the map dirty and schedules a debounced flush task; the task serializes the map
under `_MAP_LOCK` into an immutable JSON payload, then performs the tmp+rename
in a worker thread — the loop never pays the file write inline, and `_data`
never crosses the thread boundary. Coalescing never drops a trailing mutation
(the task loops until it observes a clean map), and a per-snapshot ticket keeps
a slow in-flight write from landing an older map over a newer forced one.
`SessionMap.flush()` (sync contexts) and `SessionMap.aflush()` (awaited) are the
deterministic durability points. `SessionMap.aclose()` is the shutdown boundary
used by `SessionManager.close_all()`: it cancels and awaits the registered
debounce task, preserves an unstarted or claimed-but-unwritten snapshot, lands
it through `aflush()`, and returns only after the task registration is retired.
Off the loop (CLI, tests, worker threads) every mutation still writes inline. Losing a pending
flush on a crash leaves a well-formed older map, never a truncated file.

**Auto-prune:** `SessionMap.get()` auto-removes entries whose `.json` file
no longer exists (the entry drops from memory immediately; the file write rides
the deferred flush). `SessionMap.prune()` bulk-removes all stale entries at
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
between dashboard chat and Slack. Slack is the legacy special case: other
channels link through the generic ChannelLink mirror map (see
[messaging.md](messaging.md)). The `slack_*` fields are retained for backward
compatibility.

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
the frontend can show a link indicator. `_ChatSlot.task` publishes ownership through a
property that increments `_turn_generation` for every new non-null task. The counter is
process-local and monotonic for the slot lifetime; unlike `task`, normal turn teardown
does not clear it, so code spanning an await can detect a turn that started and finished
inside that interval.

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
- `POST /api/chat/slots/{name}/slack-pause` | `mirror-pause` — disconnect (or
  reconnect) a channel while **retaining** its binding, so inbound still routes
  to the same session and a later reconnect needs no re-link. Same auth posture
  as the link/unlink pair. Body `{paused: bool}`; `mirror-pause` also takes
  `{origin: bool}` naming WHICH non-Slack delivery is meant, because a session
  can hold two at once and they mute independently. Returns 409
  `mirror_not_linked` when the named delivery does not exist. The disconnect
  itself is never governance-gated (it only ever reduces egress); a denial
  silences the courtesy note posted into the conversation and keeps the
  disconnect. That note is skipped entirely for an `origin` disconnect, since the
  mirror resolver addresses the EXPLICIT mirror — a different conversation.
- **Three persisted pause markers, each keyed differently.** A mute must live and
  die with the binding the user muted, so the key follows what the flag is about:
  - `slack_paused` — the Slack thread. Cleared when the binding is REBOUND
    (different ts or channel), NOT on an identical-coordinate write: the Slack
    inbound path re-writes the same ts/channel every turn as its thread registry,
    so clearing on any write let one inbound message silently un-disconnect a
    thread.
  - `mirror_paused` — the explicit `mirror` binding. Read/written through
    `_mirror_key`, following the binding between the canonical row and the legacy
    `dashboard:` spelling.
  - `origin_paused` — the conversation the session was BORN in. Read/written on
    the CANONICAL row, never through `_mirror_key`: that helper migrates rows
    depending on where a MIRROR lives, which stranded the pause the moment a
    mirror landed on the canonical row.
  Each existence check is per flag: a born-in conversation is permanent, while an
  explicit mirror must actually exist, so a flag with nothing behind it reads as
  connected rather than reporting a session that delivers nowhere as merely quiet.
  Enforcement lives at the send sites, not in storage — see
  [messaging](messaging.md) for the `SilentRenderer` substitution that stops a
  disconnected non-Slack conversation being written to.
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
  ├── _spawn_warm() × pool_size   → warm pool queue (instant assignment)
  └── _ensure_background()        → BACKGROUND_KEY session (persistent)
```

## Removing a session from the registry: record the end

**Every path that removes an entry from `_sessions` must report that removal to
`metrics/sessions.py`** — with `await record_session_ended(key, end_reason=...)` for
a session that lived, `await record_sessions_ended(keys, end_reason=...)` for a path
that drains many at once, or `await discard_session_start(key)` for a registration
being rolled back before it ever became one. All three are coroutines: the
breadcrumb unlink is a filesystem syscall and must not run on the event loop, where
a slow or network-backed data home would park every gateway task behind one closing
session. This is a correctness requirement on lifecycle code, not a telemetry
nicety, and it is documented here rather than only in the metrics module because the
people who can break it are editing this subsystem.

The reason it matters more than a missing sample: a session start writes a
breadcrumb file that survives process death, which is what lets a session killed
with its gateway be counted at all. A removal that reports nothing leaves that
breadcrumb behind, and the next boot reads a surviving breadcrumb as a crash. So
an unrecorded removal does not lose a data point — it **fabricates a crash that
never happened**, inside the one population the instrument exists to expose.

Practical rules when you add or change a removal path:

- Report it while you still hold the session registry lock, in the same tick as the
  `pop` / `del` / `clear`. The call's own pop and histogram record happen before its
  single suspension point, and holding the lock across that point is what keeps a
  racing cold start from registering a successor under the same key and having its
  record consumed by the predecessor's teardown. Reporting AFTER the path's other
  awaits is the bug this rule exists to prevent.
- Drain many keys with ONE `record_sessions_ended` call, never a loop of
  `record_session_ended` awaits. A per-key await puts a cancellation point between
  two keys, so every key after it is popped but unrecorded — and on `close_all`,
  which a cancellation reaches by design, that fabricates a crash per remaining
  session.
- Keep your MANDATORY post-pop cleanup reachable. All three recording coroutines
  absorb a cancellation at their crumb hop for exactly this reason, so the call
  itself will not abort you — but the rule that makes that safe is yours to hold:
  once you have popped the registry you are past the point of no return, so the
  session-map mutation that finishes the teardown (`destroy`'s `delete`,
  `discard_conversation`'s and `reset`'s `clear_sid`) must not sit behind a
  suspension point that can be skipped. Put it in a `finally` that covers every
  await after the pop, and never add a bare `await` between the pop and it.
- Give it its own `end_reason` if it is genuinely a different event. The enum is
  closed and lives in `metrics/sessions.py`; reusing a label merges two
  populations, and a metric is not a good reason to grow a lifecycle signature.
- A registration cancelled mid-flight is NOT an end. Use `discard_session_start`,
  which consumes the breadcrumb without emitting a lifetime.

`test/metrics/test_session_duration.py::TestEveryRegistryRemovalRecordsAnEnd`
enforces this. It is fail-closed and discovers its own scope: an AST walk over
every module under `src/kiro_crew` that mentions `_sessions`, recognising the
`pop`, `del` and `clear` spellings, so a new removal path anywhere fails the gate
the day it lands rather than waiting to be added to a list. A container that
merely shares the attribute name can be exempted with a stated reason, and the
exemption self-voids if that module ever starts writing breadcrumbs.

## Security: PreToolUse Command Enforcement

Command denial is enforced by Kiro Crew's bundled `hooks.py` `PreToolUse` gate,
not by injecting `deniedCommands` into kiro agent specs. Agent config generation
keeps the bundled security hooks as the immutable base, merges user hooks after
them, and strips retired `deniedCommands` / `autoAllowReadonly` fields left by an
older installation. Session startup and periodic cleanup do not rewrite agent
configs.

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

### Unreachable gatewayd reclamation

`mcp_gateway.gatewayd` daemons are their own session/process-group leaders
(`start_new_session=True`), so a launcher that dies without signalling one
(pytest teardown is the common case) leaves it resident forever — `killpg`
from the launcher's tree cannot reach it, and the marker-based orphan sweep
excludes gateway entrypoints (`_GATEWAY_MARKERS`) because a cmdline alone
cannot distinguish a live dev pod's daemon from a dead launcher's. Two layers
close the leak, both keyed on the one reachability signal that IS observable:
the daemon's `--socket` path. gatewayd creates that socket at bind, so once
the path is absent from disk no stub can ever connect again — the process is
provably unreachable regardless of who launched it.

- **Self-exit (primary, in-daemon)**: `gatewayd._socket_liveness_sweeper`
  stats its own socket path on the idle-sweep cadence, armed only after a
  successful bind. Three CONSECUTIVE `ENOENT` observations set `stop_event`,
  taking the same graceful drain as SIGTERM (backends drained and reaped).
  Any other stat failure (EACCES/EIO) is inconclusive and never counts.
  POSIX-only — a Windows named pipe has no directory entry to observe.
- **Sweep-side reap (defense in depth)**: `_is_sweepable_orphan_gatewayd` is
  a fourth positive-identity path in the untracked orphan sweep. It overrides
  the `_GATEWAY_MARKERS` exclusion only for a structural
  `-m kiro_crew.mcp_gateway.gatewayd` argv whose `--socket` path is gone
  (NUL-separated argv only — the space-joined `ps` fallback cannot delimit
  paths safely and fails closed, so the path is effectively Linux-only).
  `kiro_crew.cli` / `kiro_crew.__main__` stay unconditionally excluded. The
  kill is TERM-first (`_kill_orphan_gatewayd`) so the daemon drains its own
  pooled backends, escalating to `killpg` SIGKILL only after the daemon's full
  `TOTAL_SHUTDOWN_BUDGET_SECS` (shared with the supervisor's SIGTERM→SIGKILL
  grace, so a correctly-draining daemon is never killed mid-drain), with a
  cmdline re-verify guarding PID recycling. Same-uid + reparented-to-init
  candidacy, the age floor, and the kill budget all still apply.

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

Seven session-bound MCP tools — `monitor_start`, `monitor_update`, `autonudge_stop`, `set_project`, `suggest_followup`, `ask_question`, `reset_conversation` — used to resolve their OWN session identity (the strict sidecar resolver above) and call a loopback HTTP endpoint, which only produced a usable per-call caller when MCP-gateway **pooling** was enabled. They are now **stateless**: the tool validates its arguments and returns a *directive* — a human-readable confirmation line plus a machine-readable marker (`session_directive.encode`) carrying the validated payload and NO session key. The session-aware consumer, `dashboard/chat_runner._run_chat`'s `EVENT_TOOL_RESULT` handler, decodes the marker (`session_directive.decode`) and applies the effect IN-PROCESS against ITS OWN `slot`/`session_key` via `dashboard/session_directive_apply.py`, then strips the marker from the stored transcript. This works with pooling OFF (the default) because the consumer already owns the session, so no per-process identity source is needed.

Subagent isolation is therefore **structural, not cryptographic**: a subagent's tool result flows through the subagent's own runner and can only ever bind to the subagent's session, never its parent's — there is no `/proc` walk to get wrong. The tools still call `_resolve_session_key_strict()`, but only as a context guard to short-circuit sessions where a directive can never be applied (cron/hook/subagent) and to steer non-`dashboard:` `ask_question` callers to the `[OPTIONS:]` tag — not to bind the effect.

Security properties (enforced in `session_directive.decode` plus the applier):

- **Forgery gate keyed on canonical identity**: because the marker is model-visible (it returns as the tool-result text), a directive is honoured ONLY when the tool call was recorded — via kiro-cli's out-of-band `_meta` channel — as an MCP call whose canonical `_meta.kiro.toolName` (with `_meta.kiro.mcpServerName` set) is in `DIRECTIVE_TOOLS`, never the LLM-authored `title`. A shell command titled `monitor_start` whose stdout forges the marker resolves to no directive tool and is ignored; the gate fails closed when `_meta` identity is absent.
- **Native sub-agent calls refused**: they surface as flat events in the parent loop but have no independently bindable slot, so the applier declines them.
- **SEL audit on every application**: `apply_session_directive` emits a tool-invocation event tagged `source="mcp-directive"` with outcome `success` / `denied` (e.g. a `set_project` sensitive-path block) / `error`, since the effect now runs in the consumer rather than in the tool body or an HTTP endpoint.

The applier reuses the SAME effect cores the HTTP endpoints call — `authorize_and_add_nudge` / `authorize_and_update_nudge` / `svc.remove` for the monitor trio, `slot.project` plus the recent-projects save for `set_project`, `deliver_ws_owners` for `suggest_followup`, and `post_question_card` for `ask_question` — so behavior is unchanged except that `ask_question` is now non-blocking (full contract in `learn-cron-dashboard.md` → "Agent Questions"). `reset_conversation` is the one directive whose core is not reachable inline: it queues `SessionManager.discard_conversation` on the slot for `chat_runner._consume_pending_reset` to apply at a turn boundary, because the discard is a full provider teardown and the producer is mid-turn — the same deferral `set_project` uses, and the reason the immediate route (`POST /api/chat/slots/{slot}/reset-conversation`) answers 409 on a busy slot rather than tearing down a turn mid-write. It queues the session key THIS TURN ran on, passed in by the consumer, never re-resolved from the slot: `linked_session_key` is mutable, so a cron or workflow injection that rebinds the slot between the request and the consume would otherwise discard whatever the slot points at by then and leave the caller's conversation alone. Only the END-OF-TURN consume may apply a discard (`allow_discard`); the two earlier consume points run just before a turn acquires the session, where a teardown lands under a channel turn already streaming on it. Even at that boundary it does not assume: the discard goes through `discard_conversation(..., skip_if_busy=True)`, which refuses under the same session lock that pops the session, mirroring `reset`'s own guard. Probing from the consumer and tearing down afterwards would leave a window in which a channel message acquires the session's semaphore and begins streaming a reply the teardown then destroys — and the semaphore is the stricter signal anyway, since `provider.has_active_turn()` cannot see a turn holding the semaphore with no prompt in flight yet. A refusal returns False and changes nothing, replay flag and session map included, so the consumer leaves the flag armed for a later boundary. The sid clear runs in the SAME tick as the pop, with no await between them — deferring it past the shutdown awaits lets a concurrent channel turn map a SUCCESSOR session under the key while the old provider is still shutting down, and the clear then erases the successor's pointer instead of the discarded one. Sub-agent children are the other wait — `discard_conversation` releases the shared runtime they run on, so a running or queued child, or an in-flight completion-event delivery, also leaves the flag ARMED rather than killing the child's work. Both that consume and the route's 409 read one predicate, `chat_utils.subagents_attached`, so the two cannot drift. The queued flag is in-memory slot state: a gateway restart while it sits armed drops the reset the confirmation promised, which is accepted rather than persisted — the cost is one un-applied reset the caller can ask for again, against durable state for a transient intent. `set_project` and `reset_conversation` additionally require structural user-turn provenance: injected cron, task-runner, sub-agent, auto-nudge, orchestration, app-authenticated unattended turns, and app-authored Spec Builder seed/handoff prompts cannot retarget a borrowed destination slot even when its session key is user-facing. Spec Builder rejects app-token message and decision submissions before they can enter its human-provenance relay or durable decision ledger. Queue entries preserve this provenance, replacement text adopts the editor's provenance, and mixed or untagged merges fail closed.

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

## Bytecode-Cache GC (periodic sweep hook)

The desktop app launches the gateway with `PYTHONPYCACHEPREFIX` pointed at
`<data home>/cache/pycache` (keeps the embedded interpreter's bytecode out of
the codesigned bundle). CPython only ever adds to that PEP 3147 mirror, so the
periodic sweep in `_cleanup_loop()` owns eviction: it calls
`pycache_gc.prune_pycache` (mtime TTL + oldest-first total-size cap; limits
owned by `pycache_gc.py`) on the maintenance executor, gated to at most once
per `PYCACHE_GC_INTERVAL_SECS` because the prune walks the whole cache tree —
far heavier than the sweep's ~5-minute tick. `_last_pycache_gc` starts `None`
so the first tick after the first session starts prunes pre-existing bloat
(`_cleanup_loop()` is launched by session registration, not gateway start),
and is stamped **before** the prune runs so a failing walk retries at GC
cadence, not every tick. The traversal is anchored to no-follow directory
handles (`O_NOFOLLOW | O_DIRECTORY` + `dir_fd`-relative unlink/rmdir), and
the root is opened component by component from the filesystem root, so a
symlink or junction substituted anywhere — under the cache root mid-walk, or
swapped into a writable ancestor such as `cache/` itself — fails the open
instead of redirecting deletion outside the cache (a legitimately symlinked
ancestor thus makes the prune a conservative no-op); on platforms without
`dir_fd` support (Windows) the prune is a fail-closed no-op. Deleting entries
is always safe: a `.pyc` regenerates on the next
import. The unbounded-growth *input* (foreign interpreters in the agent
subtree inheriting the prefix) is closed separately by the sandbox env scrub —
see [security](security.md) § Conditional Python-interpreter env strip.

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

**Cold-start admission**: `SessionManager._start_sem` bounds provider starts local
to one manager. The narrower common runtime chokepoint adds a gateway-wide
`AcpRuntime.spawn()` coordinator capped at 2 concurrent spawn + `initialize`
handshakes, matching worker-pool `max_starting=min(workers, 2)`. Authoring,
interactive, background, shared-runtime, and unpooled callers therefore share the
same expensive-start bound even when they bypass this manager or a worker pool.
Queued cancellation returns the permit, and runtime startup retains its existing
subprocess cleanup on cancellation or failure.

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

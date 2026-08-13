# Subagent Module

## Overview

The subagent module (`kiro_crew/subagent.py`) spawns isolated background agents for parallel task execution. Each subagent gets its own LLM session via `SessionManager`, runs a focused task, and announces the result via callback.

Supports `on_tool_approval` callback for interactive tool approval (routed through gateway's approval system in Normal/Trust modes).

## Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_MAX_CONCURRENT` | 3 | Legacy fallback / auto-size floor. `agent.max_subagents` now defaults to `0` = auto-size the cap at startup (floor 3, ceiling `agent.subagent_auto_max`, default 32); a positive value pins a fixed cap. Session-shared subagents are cost-sampled as the runtime's measured RSS/CPU divided by the live shared-session count on that PID (`_live_shared_count`), so the memory term no longer binds and the cap rises to the provider-concurrency ceiling. |
| `_TIMEOUT_SECS` | 1800 | Hard timeout per subagent (30 minutes) |
| `_ON_DONE_TIMEOUT` | 1200 | Outer cap: max total seconds for semaphore wait + injection (20 minutes) |
| `INJECTION_TIMEOUT` | 900 | Inner cap: max seconds for a single `stream_and_collect` call (15 minutes); default `_DEFAULT_INJECTION_TIMEOUT = 900.0`, tunable via `KIROCREW_INJECTION_TIMEOUT` (float seconds, clamped to `_ON_DONE_TIMEOUT`) |
| `_RESET_TIMEOUT` | 30 | Max seconds for session reset in finally block |
| `_TURN_LIMIT` | 100 | Default tool-call budget per subagent (configurable via `agent.subagent_max_turns`, per-spawn via `max_turns`) |
| `_STALL_IDLE_SECS` | 120 | Seconds with no stream activity before a running subagent is surfaced as **stalled** in the running-card (configurable via `agent.subagent_stall_idle_secs`). Surface-only — a stalled subagent is never auto-terminated; the user closes it from the UX (per-row stop / Stop-all) and the 30-min `_TIMEOUT_SECS` ceiling still applies. |
| `_SYSTEM_PREFIX` | (string) | Injected before task text to prevent spawn recursion |
| `COMPLETION_KEEP_DEFAULT_CHARS` | 3000 | Default character cap for the completion event injected into the parent session (configurable via `agent.completion_keep_chars`). Lives in `context_management.py` alongside the helper. |

### Turn Limit Resolution Chain

Priority (highest wins): **per-spawn `max_turns`** → **config `agent.subagent_max_turns`** → **hardcoded default (100)**

A value of `0` means "not set" and falls through to the next level. Implemented as `SubagentManager._effective_turn_limit()`, shared by the enforcement path in `_run_inner()` and the timeout/reap error strings (`_timeout_context()`).

### Concurrency Auto-Sizing — Memory Probe (per platform)

When `agent.max_subagents == 0`, `compute_max_subagents()` sizes the cap from
host memory and CPU, clamped to `[3, agent.subagent_auto_max]`. The
available-memory term is read by `_available_memory_gb()`, which is dispatched
per operating system (see `dynamic-subagent-sizing.md`):

- **Linux** — `/proc/meminfo` `MemAvailable`, then clamped by cgroup headroom.
- **macOS** — reclaimable memory (free + inactive + speculative + purgeable
  pages) via the Mach `host_statistics64` syscall through `ctypes`/`libSystem`
  (`_macos_vm_reclaimable_pages`), combined with the `os.sysconf` page size.
  This is **in-process, non-blocking, no subprocess** — required because the
  probe runs on the gateway event loop at startup and the spawn-audit guard
  rejects unrouted subprocess spawns.
- **Other (e.g. Windows)** — no probe yet; returns `-1.0` and the cap fails
  open to the legacy floor of 3.

Hard floor: the auto-sized cap is always ≥ 3 — `compute_max_subagents` clamps to
`[3, hard_cap]` and the config loader clamps `subagent_auto_max` UP to 3 (with a
warning + `config_bounds_clamped` SEL event, mirroring the > 64 ceiling clamp).
Applies only to auto-sizing (`max_subagents=0`); an explicit `max_subagents` pin
is unrestricted (any 0..64).

Limitation: the per-spawn `spawn_min_memory_gb` admission gate
(`check_memory_available`) still reads `/proc/meminfo` and so remains inert
(fails open) on non-Linux hosts. Auto-sizing and the runtime gate are
independent guards; unifying them is out of scope for the sizing probe.

## APIs

### `SubagentManager.__init__(sessions, ctx_builder, on_done, max_concurrent)`
- `sessions: SessionManager` — provides isolated LLM sessions
- `ctx_builder: ContextBuilder` — builds context with memory/skills/hooks
- `on_done: AnnounceCallback | None` — called with `SubagentInfo` when done
- `max_concurrent: int` — capacity limit (default 3)

### `spawn(task, parent_session_key="") -> SubagentInfo | None`
Spawns a background agent. Returns `SubagentInfo` or `None` if at capacity. Uses atomic `_running_count` to prevent race conditions. `parent_session_key` tracks the originating session for completion injection.

Spawn flow:
1. **YOLO mode**: skips approval, runs immediately
2. **Parent trusted**: parent session has `approval_policy="auto"` (set by
   dashboard trust toggle) → skips approval, runs immediately
3. **Non-YOLO, non-trusted**: enters `_spawn_with_approval`, which re-checks
   YOLO (defense-in-depth against toggle race), then requests interactive
   approval with a 2-minute timeout. Timeout or rejection frees the
   concurrency slot.

### Tool Approval Cascade

When a subagent's tool call triggers `EVENT_PERMISSION_REQUEST`, approval
is decided in strict priority order:

1. **Hook deny** — `hooks.on_tool_call()` returns `TOOL_DENY` → reject
2. **YOLO mode** — `is_yolo()` (live check) → auto-approve
3. **Parent policy** — `parent_policy == "auto"` (snapshot at spawn) → auto-approve
4. **Interactive callback** — `on_tool_approval` (races dashboard + Slack, 2h timeout)
5. **Deny by default** — none of the above matched → reject

`parent_policy` is resolved once when `_run_inner` starts, using this chain:
1. Read from parent session via `get_approval_policy(parent_session_key)`
2. If empty and YOLO mode active → `"auto"`
3. If still empty **and subagent has no parent session key** → use the cached `KiroCrewConfig.agent.approval_mode` (snapshotted at `SubagentManager` init); if `"auto"` → `"auto"`

Step 3 ensures parentless subagents (e.g. cron jobs) respect the user's
global approval mode instead of falling through to interactive approval.

The `is_yolo()` check in the cascade is live (reads current gateway state),
providing coverage if YOLO is toggled mid-execution.

### `cancel_all() -> None`
Cancels all running subagents, stops the reaper loop, and awaits their cleanup. Handles `CancelledError` gracefully — sessions released, count decremented.

### `steer_run(agent_id, message) -> (ok, detail)` / `follow_up_run(agent_id, message) -> (ok, detail)`
Two delivery modes for `spawn_steer` (REST `POST /api/spawn/{id}/steer`, body `mode`: `"interrupt"` default / `"follow_up"`). `steer_run` injects into the RUNNING turn via the provider's `steer`, with a bounded startup-grace poll for a live run whose session has not registered yet (#1113). `follow_up_run` never interrupts: it queues the message on `SubagentInfo.pending_followups` and arms a one-per-run watcher (`_deliver_followups`, registered in the manager-owned `_followup_watchers` dict — NOT the global `_safe_fire` set — because a watcher can spawn a brand-new run and must therefore be reachable by `cancel_all()`, per the same containment contract as `_schedule_cancel_recovery`; `cancel_all` cancels watchers BEFORE the run tasks so none can dispatch into a shutting-down gateway, and the watcher re-checks `_shutting_down` before dispatch). The watcher waits for the run to complete (`info.done` AND its task popped from `_tasks`, so teardown is finished), then dispatches the whole queue as ONE `continue_conversation` on the run's own conversation (messages joined in arrival order — three corrections cost one continuation, not three). The continuation is a normal new run on the same parent session, so its result arrives as a separate completion event. OUTCOME-AWARE: a run the user explicitly STOPPED (`user_stopped`) suppresses dispatch (`followup_suppressed` audit) — resurrecting killed work is the opposite of "the correction can wait"; error/timeout terminals still dispatch (the continuation carries the conversation's context, so "fix what broke" is legitimate). NEVER SILENT: every undeliverable path (suppressed, watcher expiry, dispatch failure) announces a SYNTHETIC failure completion event through the normal `_on_done` path, because the spawn_steer reply promised the parent an event — `followup_expired`/`followup_failed`/`followup_suppressed` SEL audits alone would leave the parent blocked on an event that never comes. Deliberately a per-run poller, NOT a hook in `_run`'s 3-guard finalization: completion is reached from many terminal paths (normal/error/timeout/cancel-recovery/reaper) and a watcher observes the outcome without adding an obligation to any of them. Bounded everywhere: poll cadence 2s, hard deadline `default_timeout + 300s`, and residual `conversation_busy` after done gets a bounded retry. Typed refusals mirror steer: `not_found`, and `not_running` (use `spawn_continue` directly on a finished run).

### Properties
- `running -> list[SubagentInfo]` — currently running agents
- `count -> int` — number of running agents
- `max_concurrent -> int` — capacity limit

## SubagentInfo

```python
@dataclass
class SubagentInfo:
    id: str               # 8-char hex UUID
    task: str             # original task text
    started: float        # time.time() at spawn
    done: bool            # True when finished (success or error)
    result: str           # LLM response text (trimmed to completion_keep for the event)
    result_path: str      # ~/.kiro/crew/subagents/<id>/result.txt (full transcript)
    result_truncated: bool  # completion copy dropped content → event carries summary+path
    error: str            # error message if failed
    elapsed: float        # seconds from start to completion (set in _run finally)
    tool_count: int       # observed tool calls (incl. auto-approved); drives running-card progress
    last_activity: float  # time.time() of last stream event; reset to _exec_started; drives idle-stall
    stalled: bool         # reaper flagged this subagent as idle/stalled (UI signal)
    _awaiting_approval: bool  # blocked on a human tool-approval prompt → exempt from idle-stall
```

## Session Lifecycle

1. `spawn()` increments `_running_count`, creates asyncio task
2. `_spawn_with_approval()` (non-YOLO): re-checks YOLO, requests approval with 2-min timeout
3. `_run()` wraps `_run_inner()` with `asyncio.wait_for(_TIMEOUT_SECS)`
4. `_run_inner()` resolves `parent_policy` (parent session → YOLO fallback → config fallback), creates session `subagent:{id}` via `SessionManager.get_or_create(approval_policy=parent_policy)` — policy is persisted on the new session
5. Streams through ACP with context injection, tool approval cascade, and turn counting
6. On completion (in `_run` finally block): fire `subagent_done` WS event immediately (before slow reset + on_done), then `sessions.release()` → `_running_count -= 1` → `sessions.reset()` → call `on_done` callback
7. On timeout: `error = "Timed out after 30 minutes"`
8. On turn limit: `error = "turn_limit:{turn_limit}"` (default 100)
9. On `CancelledError`: three-way, by cancellation source (see **Terminal-State Contract** below) — user stop → neutral `user_stopped` record (NO error); shutdown / spent one-shot → `error = "cancelled"`; any other (unexpected) cancel → one-shot auto-continue via `_schedule_cancel_recovery`

**Early WS event firing**: `subagent_done` WS event is fired in the `_run` finally block BEFORE the slow `reset()` + `on_done()` path. This ensures the dashboard receives completion status within seconds, not 30-90s later when `stream_and_collect` finishes processing.

## Terminal-State Contract (stopped vs failed vs completed)

A record's terminal outcome is three-way, with a **single canonical source**: the `SubagentInfo.outcome` property (`"stopped" | "failed" | "completed"`). Every `subagent_done` emission (live, `_run` finally, `_force_reap`, WS reconnect replay managed + native), `native_subagent_snapshots`, the `/api/spawn` listing, and tombstones carry `outcome` explicitly. Consumers MUST use `outcome` — never re-derive from `error`-nullability (the legacy `error ? failed : completed` idiom misreports a stopped agent as completed). `stopped`/`error` remain on the wire for compatibility:

| Outcome | Record shape | UI/consumer meaning |
|---|---|---|
| `stopped` | `user_stopped=True`, `error` **unset** | neutral: user killed it; partial result preserved; NOT a success, NOT a failure |
| `failed` | `error` set | failure (tombstoned, counted in Stats) |
| `completed` | neither | success |

- A user stop is neutral **in the record itself**: `cancel()` sets `user_stopped=True` and neither it nor `_force_reap` ever synthesizes an `error` for it.
- Every emission carries the flag explicitly: live `subagent_done` events, the `_run` finally emit, `_force_reap`'s emit, WS **reconnect replay** (managed and native), `native_subagent_snapshots`, and the `/api/spawn` listing all include `stopped`. Cancelling a native card persists `stopped` on the slot tracker record so replay reconstructs it as stopped.
- The gateway completion consumer (`_subagent_done`) classifies three-way: a stopped agent is announced as "stopped by user ⏹" with partial output flagged, and in orchestrator mode records **neither** `record_success` nor `record_failure`.
- **Intentional-cancel rule**: every code path that cancels a subagent task on purpose MUST set a terminal marker first — `cancel()` → `user_stopped`, `cancel_all()` → `_shutting_down`, `_force_reap` → `reaped`. An unmarked cancel is treated as unexpected and recovered once (below). Enforced MECHANICALLY, not by convention: all in-module intentional cancels route through the `_cancel_task_intentionally(task, info, reason=...)` chokepoint, which verifies a marker is visible before cancelling (a missing marker logs an error and consumes the recovery budget defensively so a mis-marked cancel can never zombie-respawn), and a source-scan test asserts no raw `.cancel()` on a managed run task exists outside the chokepoint.

## Transient Retry (mid-stream 5xx)

`_run_inner` streams through `_stream_with_transient_retry`, mirroring the main path's retry ladder: transient backend errors (the `-32603` class, per `acp_error_is_transient`) are retried with exponential backoff on the same live session; each retry fires a `subagent_retrying` WS event (chip shows `⟳ retrying`) and a SEL audit record. **Replay-safety**: if ANY activity was observed (text chunk, approved tool turn, or auto-allowed tool call), the retry sends `_TRANSIENT_CONTINUE_MSG` instead of the original prompt — a mutating tool may have executed before the first text chunk, and replaying the full prompt would re-run it. **Budget**: `TRANSIENT_RETRIES` applies only while ZERO activity was observed (replaying the bare prompt is side-effect-free); after any activity, recovery is ONE-SHOT — exactly one continuation turn, matching the main path's `_posttoken_retry_used` rule, since each post-activity continuation is an independent opportunity to repeat a side effect. The two ladders (this one and `dashboard/chat_runner.py`'s) are intentionally-identical copies cross-referenced in both sources; a change to either's predicate or budget must be mirrored. Non-transient errors and exhausted budgets propagate to the generic error arm.

## Unexpected-Cancel Recovery (one-shot auto-continue)

An unmarked `CancelledError` (see intentional-cancel rule) triggers `_schedule_cancel_recovery`: exists for cancellations arriving from outside the manager's lifecycle (parent task-tree teardown around a live subagent), mirroring the main path's PR #173 recovery. Mechanics:

- **Side-effect gate**: recovery fires ONLY when `tool_count == 0`. The respawn runs on a fresh session with no ledger of prior tool calls, so once any tool has executed the model cannot verify which side effects already happened — the run is finalized instead (error `"cancelled (auto-continue suppressed: tools already executed …)"`, partial output preserved and delivered). Text-only activity is safe to resume.
- One-shot: gated by `info._cancel_retry_used`; the recovered run's own cancel is terminal.
- Explicit handshake: `_resume` awaits the ORIGINAL task's full teardown (session release/reset, slot decrement, registry pop) before respawning — never a timed sleep.
- Slot re-acquisition: waits (bounded, `_RECOVERY_SLOT_WAIT_SECS`) for free capacity; the slot claim and `create_task` are ATOMIC (no await between) so a concurrent `_drain_queue` cannot overshoot `max_concurrent`.
- Shutdown-reachable: the pending `_resume` task is registered in `_tasks` under `"{id}:recovery"` so `cancel_all()` cancels it; a cancelled recovery finalizes the record terminally and never respawns.
- Failed recovery (no slot / teardown timeout) still fully finalizes: `subagent_done` emitted, tombstoned, delivered via `on_done`.
- Replay-safety at respawn: when the first attempt streamed partial text, the respawned prompt is prefixed with `_CANCEL_RESUME_PREFIX` so the model continues instead of restarting (the prefix also gates on `tool_count` as defense-in-depth, though the side-effect gate above means a tool-activity run never reaches respawn). A bare original prompt is re-sent only for a zero-activity first attempt.

## Reaper Loop

`start_reaper()` launches a periodic loop (60s interval) that force-kills subagents exceeding the 30-minute timeout deadline. Defense-in-depth for cases where `asyncio.wait_for` fails to fire due to event-loop saturation or orphaned tasks.

- `_reaper_loop`: sweeps every 60s, calls `_force_reap` on expired agents
- `_force_reap`: reset with 30s timeout → SIGKILL fallback → mark done → fire `subagent_done` WS event
- **Terminal completion is arbitrated by FOUR separate guards, not by `reaped` alone.** Two paths can finish a subagent — `_force_reap` and `_run`'s `finally` — and between them there are four distinct one-time concerns. Earlier revisions tried to arbitrate them with `reaped` plus `done` and every attempt satisfied two while breaking a third (duplicate delivery when the marker was set late; a lost outcome when it was set early and the reaper was cancelled; a lost outcome when the claim was handed back to a run that had already exited; and finally **no reporter at all plus a leaked concurrency slot** when the report claim was gated on `not info.done`). The guards are now:
  1. **`info.reaped` — classification.** Was this a deliberate reap? The cancel-recovery scheduler reads it, and the marker MUST precede the intentional cancel (see the intentional-cancel rule above) or an unexpected-cancel respawn fires on the run being killed. Unchanged.
  2. **`if not info.done` — the terminal RECORD.** Error synthesis, failure stat, tombstone, cost. First-arrival-wins, so it is never written twice (pinned by `test_subagent.py::TestOnDoneTimeout::test_force_reap_skips_tombstone_when_already_done`).
  3. **`_release_slot(info)` — SLOT accounting.** A one-shot token per `SubagentInfo`; the winner decrements `_running_count` once and drains the queue. Deliberately independent of both flags above: inferring slot ownership from `done` or `reaped` produced a double decrement in one interleaving and none at all in another. A leaked slot permanently starves the spawn queue, which matters far more at the 60-100 concurrent agents the scale work targets. The cancel-recovery respawn **re-arms** this token when it re-admits a slot (`_running_count += 1`), because the respawned run occupies a fresh slot and needs its own release.
  4. **`_claim_finalize(info)` — REPORT ownership** (`subagent_done` + the `_on_done` injection, plus wave-digest settling and the result.txt TTL bookkeeping). Granted to exactly one caller; contains no `await` so the check-and-set is atomic on the loop. It does **not** consult `info.done` — that was the last defect. It returns False while `_recovering` *without consuming itself*, so a pending respawn is not reported done and its respawned run can claim later.
- **A claimed report is atomic, not merely exclusive.** The claim alone still lost outcomes when the claimer was cancelled mid-report. `_report_terminal` therefore runs on a strongly-referenced task under `asyncio.shield`, spawned by `_run` **before** its teardown awaits so the task is already live wherever a cancellation lands; the caller still receives `CancelledError` while the report completes. `cancel_all()` drains outstanding reports with a bounded timeout and then **cancels and gathers** any straggler, so none is left invoking `_on_done` against tearing-down state or killed by a closing loop. Because the awaiter is shielded, shutdown is bounded by that drain rather than the `_ON_DONE_TIMEOUT` injection cap. Enforced by `test_subagent_reap_race.py`.
- **An undelivered report abandoned at shutdown is made RECOVERABLE, not silently dropped.** The terminal record — including the tombstone — is written before delivery is attempted, and a tombstone is exactly what `list_orphans()` uses to EXCLUDE a folder from the next start's reconciliation. So cancelling a still-pending report at the drain deadline would leave an outcome that was never injected *and* invisible to the only path that could still inject it. `cancel_all()` therefore calls `clear_tombstone(id)` for each report it cancels, re-admitting that agent to the next start's orphan reconciliation (which finds `result.txt` and re-delivers). Extending the drain to `_ON_DONE_TIMEOUT` instead was rejected: it would hold gateway shutdown for up to 20 minutes on one wedged injection, which is the exact failure the bounded drain exists to prevent. Only reports cancelled **before** `_on_done` returned are re-admitted — `info._reported_to_parent` is set the moment the injection returns, so a cancellation in the later teardown/tombstone waits cannot cause a duplicate delivery on restart.
- **Every reporter goes through the claim — including cancel-recovery failure.** There are more terminal paths than the two obvious ones: when a cancel-recovery respawn cannot happen, its `except` arm also finalizes the agent. That site previously fired `subagent_done` and `_on_done` directly, gated only on `done`/`reaped`, so a reaper racing a failed respawn delivered the outcome twice. It now takes `_claim_finalize` like every other reporter and reports through the shielded helper (which matters because `_force_reap` cancels that very task). `_resume_guarded`'s CancelledError arm writes only the RECORD and deliberately never reports — during shutdown the drain owns delivery.
- **The reaped marker and the recovery cancel precede every `await` in `_force_reap`.** Both used to sit after the session teardown, which yields for up to `_RESET_TIMEOUT` (longer on the SIGKILL path). A recovery task whose bounded handshake expired inside that window observed `reaped == False` and respawned the run being killed — tools executing after a user Stop, strictly worse than a duplicate report.
- **Delivery bookkeeping trails teardown.** Spawning the report ahead of teardown opens a window the older ordering did not have: writing the "delivered" tombstone before the session is torn down would hide a surviving child from orphan reconciliation if the process died in between. The report therefore waits on a `teardown_done` event (set in `_run`'s `finally`, so it fires even under cancellation, and bounded so the report can never wedge) before marking delivery. A reaped or recovery-failed member still settles its **siblings'** digest holds, since those siblings' results did reach the parent even though this member's did not.
- `_sigkill_session`: best-effort SIGKILL when graceful reset hangs
- After decrementing `_running_count`, `_force_reap` calls `_drain_queue()` so the freed slot immediately starts a queued spawn. Normal completion pumps the queue via its `finally` block, but that block is gated on `not info.reaped`; a reap sets `reaped=True` and decrements the count itself, so without this explicit drain a queued spawn would sit stranded until an unrelated agent finished or a new spawn arrived.
- Wired up in `gateway.py` after `SubagentManager` init

### Idle-Stall Detection

The main-agent watchdog stack (`tool_stall_suspect`) does **not** govern subagents; `_maybe_flag_stall(agent_id, info, now)` (called from the reaper sweep) is their equivalent. It does, however, consult the *same* liveness oracle (`acp/liveness.py`) — see the attribution note below. Each stream event calls `_touch_activity(info)`, which updates `info.last_activity`, clears a prior `stalled` flag (re-emitting `subagent_stalled {stalled: false}` when work resumes), retires the agent's oracle and bumps `info._stall_gen`. `info.last_activity` is (re)initialised to `_exec_started` at the top of `_run_inner` so a queue / spawn-approval wait is never counted as idle.

Per sweep, for an agent that has actually started (`turns > 0` or a live `_pid`) and is **not** blocked on a human approval prompt (`_awaiting_approval`):
- `idle > _stall_idle_secs` and not already flagged → consult liveness (below), and unless the verdict clears it, set `info.stalled = True`, emit `subagent_stalled {stalled: true, idle_secs}` (surface-only; the card shows a "no activity" warning), and append a record of the slow command to `~/.kiro/crew/subagents/slow_commands.jsonl` for later analysis.
- Detection is **surface-only**: `_maybe_flag_stall` never terminates the agent. A genuinely-hung subagent is closed by the user from the UX (per-row stop → `spawnDelete` → `SubagentManager.cancel(agent_id)`, or header Stop-all). The wall-clock reaper at `_TIMEOUT_SECS` remains the only automatic terminator; a `DEAD` liveness verdict deliberately does **not** escalate to a kill, because that would be a change to reap semantics rather than to the signal.

#### Liveness attribution (why idle time alone is not the detector)

Idle time cannot separate a wedged tool call from a slow, silent one, so the flag is gated on a liveness verdict. Attribution is per-CHILD, not per-runtime-PID: the subagent event loop records the in-flight tool's dispatch snapshot (`_inflight_tool` — the trusted `is_shell`, the command, `tool_name`, dispatch time, taken from the same `AcpEvent` the main agent's `ToolCallState` is built from), and `_stall_verdict` hands it to a per-agent `LivenessOracle`. With `is_shell` set this takes the oracle's shell-child branch, which cmdline-matches a live descendant and then tracks that pid.

This is what makes the verdict meaningful even though **session-sharing subagents share the parent's runtime PID**: the match keys on the command, not the runtime. A whole-subtree aggregate would be useless here — it is dominated by kiro-cli's own background socket/keepalive traffic, so a `sleep`-only subagent reads as "working".

Verdict → action:
- `WORKING` — an attributable live child, so the agent is progressing: **not** flagged (suspicion stays open so the badge appears as soon as that child stops).
- `DEAD` / `STUCK_INPUT` — positive evidence of a wedge, so it flags **immediately**, skipping the two-sweep confirmation that exists to dampen guesses. That skip is **withdrawn whenever the runtime is session-shared** (see the third bound below), because the trust it assumes is exactly what a shared runtime removes — the parent session is always a co-tenant of that process, so a lone subagent is no safer than one with siblings.
- `UNKNOWN` — no attributable evidence (no tool in flight, a non-shell tool with no child to match, unreadable `/proc`, or a refused executor): falls back to idle-time-only with two-sweep confirmation.

Four bounds keep this honest, and each exists for a failure that was observed rather than imagined:

- **`_SUPPRESS_CEILING`** — a `WORKING` verdict only suppresses while `idle < _stall_idle_secs * _SUPPRESS_CEILING`. Attribution is not infallible: two siblings running *similar* commands under `session_sharing` can cmdline-match the same child, so a wedged agent could read `WORKING` for as long as its sibling's child lives. Unbounded that would convert a case the idle-time-only path DID badge into a permanent false negative — worse than a spurious badge, since the badge is self-clearing and a missing one is not. Past the ceiling the badge wins, so misattribution costs latency, not the signal.
- **The wedged skip is withdrawn under a shared runtime.** The same fallible match runs in the other direction: a `DEAD` reading can describe *another session's* child that exited, and because `DEAD`/`STUCK_INPUT` normally bypass the two-sweep confirmation, that would raise an immediate badge on a healthy agent — defeating the dampening that keeps the badge trustworthy at 60-100 agents. Granting one path immediate trust in a signal the ceiling exists because it is unreliable is incoherent, so when `info._session_sharing` is set the wedged verdict earns its badge the same way a guess does: by holding across two sweeps (~60s). **The gate keys on the flag, not on a sibling count.** `_create_shared_session` puts the subagent on the **parent's** AcpRuntime — one process hosts everything — so `info._pid` is the parent's process and the parent's own tool children are descendants of it too. `_live_shared_count` iterates the subagent registry and therefore cannot see the parent, so an earlier `_live_shared_count(pid) > 1` form left a *lone* session-shared subagent on the fast path while it could still cmdline-match the parent's child and flag the instant that child exited. Since a shared runtime always contains the parent, "could this match belong to someone else?" holds for every session-sharing agent; only a dedicated-process subagent (`session_sharing` false, or a per-spawn model/effort override that forces its own process) keeps the immediate flag.
- **The walk is offloaded, never inline.** `check_tool` is a synchronous `/proc` walk (`iter_descendants`, plus `os.readlink` on `/proc/<pid>/fd/*`, which can block on the very wedged fd being investigated) and the reaper runs on the same event loop that serves every chat turn. It is submitted through **`consult_offloaded` (`acp/liveness.py`) — the one shared guard, not a local mirror of it**: the same helper the main-agent watchdog reaches via `AcpSessionHandle._consult_oracle_offloaded`, so `SubagentInfo` supplies the `_consult_future` its `ConsultFutureHolder` protocol requires and a fix to the guard lands on every caller at once. The guard owns submission-inside-the-guard, exception retrieval attached at submission, the bound (`OFFLOADED_CONSULT_TIMEOUT_SECS`, 10s) via `wait_for(shield(...))`, at most **one outstanding walk per holder** so a permanently wedged read cannot leave a new blocked worker behind on every sweep, and degrade-to-`UNKNOWN` on any failure. Failure mode to be aware of: consults are awaited serially within a sweep, so if many agents cross the idle threshold while their `/proc` reads wedge, a single sweep can stretch toward N×10s and delay the wall-clock reap for the other agents in it. Bounded and unlikely (one walk per agent, later sweeps short-circuit on the in-flight guard), but it is the cost of doing this in the sweep rather than out of band.
- **Generation counter.** The awaited verdict is discarded (`superseded mid-consult`) when `info._stall_gen` moved during the walk — i.e. activity, a final tool result, or the next dispatch retired the snapshot it was submitted for. Without this the walk's own latency is enough to flag an agent that has resumed working, and `DEAD`/`STUCK_INPUT` skip two-sweep dampening, so a stale one would flag instantly.

The snapshot is retired only on a **`tool_final`** result. `EVENT_TOOL_RESULT` is also emitted for non-completed progress updates (`_dispatch` sets `tool_final = status == "completed"`), and treating one of those as the end of the tool would drop attribution mid-command — degrading exactly the long silent command this detection exists to judge. `acp.client` gates on the same field. On each retirement the oracle is replaced via `fresh()` rather than mutated, so a walk still running against the previous command cannot write its late sample into the next tool's baseline.

The verdict and its evidence are recorded in the reaper's log line but are deliberately **not** on the `subagent_stalled` wire: no consumer reads them today (the frontend narrows the payload on arrival and the coalesced batch update forwards only `stalled`), and the event is app-sdk-forwarded, so unread keys would become semi-permanent surface.


The slow-command record (`record_slow_command`, `subagent_persistence.py`) is append-only and deliberately NOT a tombstone: a tombstone marks an agent dead and is consumed by orphan-reconciliation / TTL cleanup, whereas a stalled subagent is still running. Fields: `id`, `flagged` (ts), `last_tool` (redacted), `tool_count`, `turns`, `idle_secs`, `elapsed_secs`, `parent_session`, `session_sharing`.

`_awaiting_approval` is set around the human tool-approval await in the `EVENT_PERMISSION_REQUEST` branch (reset in `finally`, which also refreshes `last_activity`), so a slow approval never looks stalled.

### Running-card progress events

`subagent_tool` is fired on **`EVENT_TOOL_CALL`** (not only `EVENT_PERMISSION_REQUEST`) — kiro-auto-allowed tools surface only as informational `tool_call` updates, so this is the sole progress signal a simple/read-only task emits. Payload carries `{tool, tool_kind, turns, tool_count}`; `info.tool_count` increments per observed tool call. The `subagent_snapshot` reconnect payload (`dashboard/ws.py`) also carries `tool_count` and `stalled` so a reloading client recovers progress/stall state (a transition-only WS signal always needs a matching snapshot field).

## Completion Injection

Subagent results are routed back to the **originating session** via
`_subagent_done` in `gateway.py`. The `parent_session_key` on `SubagentInfo`
tracks which session spawned the subagent.

### Two-Level Timeout

| Timeout | Location | Duration | Scope |
|---|---|---|---|
| Outer cap | `subagent.py _run()` | 1200s (20 min) | Semaphore wait + injection combined |
| Inner cap | `gateway.py _subagent_done()` | 900s (`INJECTION_TIMEOUT`, tunable via `KIROCREW_INJECTION_TIMEOUT`) | Single `stream_and_collect` call |

On timeout (inner or outer):
1. Kill stuck kiro-cli process via `sessions.reset()`
2. Queue failure event into `slot._pending_subagent_failures`
3. Next `_run_chat` drains the queue into LLM context with `result_path`
4. LLM reads result from disk if needed

### Prompt-Busy Recovery

`_inject_with_retry()` in `gateway.py` makes up to 3 attempts (1 initial + 2 retries) of `stream_and_collect` on AcpError. Between retries: cancels orphaned prompt, exponential backoff. On `PromptBusyExhaustedError`: kills provider, queues failure event. Note: the 1200s outer cap (`_ON_DONE_TIMEOUT`) bounds total wall-clock time, so not all retries may fire if earlier attempts consume the budget.

**Reconnect recovery**: `subscribe_subagents` in `ws.py` restores both managed and native subagent cards. Managed subagents are authoritative in `SubagentManager`: running records replay as `subagent_snapshot`, and recently completed records replay as `subagent_done`. Managed results remain disk-backed and are not copied into inline Redux card payloads.

Native kiro-cli subagents run inside the parent ACP turn and are owned by the parent dashboard slot. `DashboardState.native_subagent_snapshots()` replays running native cards as `subagent_snapshot` and recent terminal cards as `subagent_done`. A native `subagent_done` payload may include optional `task`, `agent`, and `result` fields. `result` is a redacted output tail bounded to 8,000 characters, with an explicit truncation marker when earlier output was dropped. Running output retained for replay is bounded to 40,000 characters, with an 80,000-character hard accumulation ceiling. Terminal native records are retained globally up to 50 cards for at most one hour. The client treats `done` and `error` as monotonic terminal states, so a stale running snapshot interleaved after a live completion cannot demote the card.

**Redaction**: All subagent event payloads (running snapshots and done events) have the `agent` field redacted before sending to the dashboard. Task text is redacted before truncation to prevent credential patterns spanning the boundary.

| Parent Session | Backend Delivery | Client Follow-up | User Sees |
|---|---|---|---|
| Dashboard (`dashboard:*`) | Append as user message + broadcast via WS | TUI/web re-injects via `sendMessage` → LLM round-trip | LLM's response summarizing the result |
| Slack (thread ts) | Post to Slack channel thread + dashboard notification | _(none — raw result posted directly)_ | Raw subagent result text |
| Non-Slack channel (`telegram:*`, `discord:*`, `unified:*`, …) | Inject into the parent ACP session, then send the synthesized reply through the governed cross-surface transport ladder (`_deliver_channel_reply` → `_resolve_channel_target` → `MessagingTransport.send_message`) + dashboard notification. Target resolution: origin link (recorded by Discord's inbound dispatch) → non-Slack mirror link (e.g. Telegram `/link`) → for **direct (1:1) sessions only**, the stored `"{namespace}:{user_id}"` channel value, resolved to a postable conversation via `transport.resolve_configured_target`. Group/forum sessions without an origin/mirror link, channels whose dispatcher records neither, and denied/unsupported egress all degrade to notification-only — never a cross-conversation send. | _(none)_ | LLM's synthesized reply in the channel conversation |
| Cron/no parent | Dashboard notification only | _(none)_ | Notification panel entry |

### Post-fan-out Synthesis Turn

After a fan-out of sub-agents, a single dedicated **synthesis turn** produces
the user-facing summary (restate goal → synthesize across all results →
recommend next actions), instead of leaving the last visible message as a
per-sub-agent completion note. Dashboard chat only (orchestrator mode has its
own stage synthesis).

- **Arm** — in `_subagent_done` (chat mode, `not _is_orchestrator`), when the
  last outstanding sub-agent for the parent completes
  (`running_agents_for(parent_key) == []`), set `slot._pending_synthesis = True`.
- **Fire** — in `chat_runner._run_chat`'s drain/idle branch, once the queue is
  empty, no agents are running, `_pending_synthesis` is set, **and**
  `slot._subagent_deliveries_inflight == 0`, launch exactly one tracked synthesis
  task. `_synthesis_inflight` prevents duplicates. There is **no readiness wait**:
  readiness is latched at gateway boot and refreshed only on explicit user action,
  so parking the arm on it would strand the synthesis indefinitely. The task
  clears the arm once the delivery guards pass, immediately before starting one
  timeout-bounded `_run_chat` turn with `SUBAGENT_SYNTHESIS_PROMPT`; a signed-out
  CLI surfaces as an `AcpAuthRequired` error card from that turn.
- **Per-result turns kept** — each completion is still processed in its own turn
  (no raw buffering) to avoid a context-window blowup; the synthesis works over
  the already-condensed per-result turns.
- **Delivery-race guard** — `_subagent_deliveries_inflight` is incremented in
  `_subagent_done` from entry until the completion is queued/launched
  (try/finally). Because a concurrently-finishing sibling holds this count while
  it awaits the current turn (busy path), an earlier turn cannot fire synthesis
  before that sibling's result is delivered.
- **Cancellation** — a real user message draining first clears
  `_pending_synthesis` (user takes over); a newer in-flight batch defers
  synthesis until it too completes (only one synthesis fires, after all work).
- **Linked surfaces** — `SUBAGENT_SYNTHESIS_PROMPT` begins with
  `SUBAGENT_SYNTHESIS_PREFIX`, marking it a synthetic continuation that is NOT
  mirrored to Slack/Telegram as a user message (only its reply is delivered).

### Parent Session Discovery

The gateway sets the `KIROCREW_SESSION_KEY` env var when spawning kiro-cli,
and `mcp_core.py` reads it via `os.environ.get()`. If the env var is missing
(e.g. older gateway), it falls back to reading
`~/.kiro/crew/session_pid_{getppid()}.txt` for backward compatibility. The
session key flows through the `/api/spawn` endpoint as `parent_session`.

## Scale Plumbing (60-100 concurrent agents)

Large waves must not flood the WS socket, the parent LLM's context, or the UI. Five mechanisms — WS coalescing/replay-batching and UI caps are inert below their thresholds; digest chunking applies uniformly to every multi-task wave (single-task spawns behave byte-identical to legacy):

- **Batch identity**: `spawn(batch_id=..., batch_total=...)` (threaded from `spawn_run tasks=[...]` — one 12-hex id per multi-task call — via `POST /api/spawn` transport params; survives the stagger queue). `spawn_batch_started {batch_id, count}` fires once per batch on its first started member; the id rides every WS frame (`base["batch_id"]`).
- **Event coalescing** (`subagent_scale.SubagentEventCoalescer`, wired in the gateway's `_subagent_event`): above 8 active agents, `subagent_tool`/`subagent_stalled`/`subagent_retrying` buffer per-agent (latest state wins, merged) and flush every ~1s as ONE `subagent_batch_update {updates:[...]}` frame to all clients; `subagent_chunk` text buffers append-concatenated (16KB/agent cap) and flushes as `subagent_batch_chunks {chunks:[...]}` to subagent subscribers only. Lifecycle events (`spawn`/`done`/`recovering`/`injection_failed`/`batch_*`) are NEVER coalesced, and a `done`/`spawn` flushes buffered state first so ordering is preserved. Non-int active-count fails open to pass-through.
- **Chunked wave-digest completion injection** (gateway `_subagent_done`): every batch member is accounted per `batch_id` (this is the single completion consumer for all terminal paths). Every multi-task wave (`batch_total > 1`) delivers results to the parent queue-style: completed members are HELD, and every `SUBAGENT_DIGEST_CHUNK_SIZE` completions (default 10, env `KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE`, clamped 1..1000) flush ONE `[Subagent batch completion event]` chunk digest — failures first with detail, successes as one-line `result_path` pointers (60KB cap per chunk); the final member flushes the remaining partial chunk. A 60-agent wave = 6 digest turns spread across the wave's runtime — bounded chunk size, incremental signal, and no straggler-gated mega-digest. Chunk buffers (`fail_lines`/`ok_lines`/`guard_msgs`/`held_ok_ids`) reset per flush; cumulative `ok`/`err`/`stopped` counts ride the final chunk's summary. **Spawn discipline**: non-final chunks instruct the parent NOT to spawn new sub-agents while batches are still arriving; the final chunk releases the gate ("finish processing all results before spawning follow-ups") — mirrored by a line in the `spawn_run` tool description. **Chunk order is FIFO**: the injection busy-check (`_injection_slot_busy`) treats a live `slot.task` — the claim assigned synchronously at dispatch — as busy in addition to `slot.running`, so a later chunk waits behind an injection that is dispatched but still inside `bounded_chat_turn`'s off-loop timeout resolution, instead of racing ahead of it or assigning `slot.task` over the earlier chunk's still-pending task. Single-task spawns have no batch identity and keep the plain per-agent injection. A batch member rejected at spawn (empty task, low memory, cwd, governance, bad agent) is counted as submitted AND announced through the done callback with its batch identity (`_announce_rejection`) — so a rejection that closes the wave still reaches the consumer and releases held sibling results (non-batch rejections do not announce; the caller gets the error synchronously). `batch_finished {batch_id, total, ok, err, stopped}` broadcasts for every batch regardless of size. **Wave liveness (lost-submission backstop)**: a member rejected before reaching `spawn()` or lost during transport is counted in every sibling's `batch_total` but never in `submitted` — un-reconciled, the count-driven `batch_members_pending()` wedges the wave forever. Three layers close it: (1) `api_spawn` marks in-process rejections/capacity with `counted: true` (preserved through the MCP client's error flattening); (2) `spawn_run` best-effort POSTs `/api/spawn/lost` for each explicit UNcounted rejection, which calls `record_lost_submission` — counts the member as submitted and announces a synthetic terminal failure through the completion consumer so the wave closes; uncertain transport failures are not immediately reconciled because the gateway may have accepted them; (3) the reaper's `_sweep_stuck_waves` (every sweep) force-reconciles uncertain or lost submissions when `submitted < expected`, all registered members are terminal, nothing is queued, and no submission progress occurred for `_WAVE_STUCK_SECS` (1800s / 30 minutes — deliberately generous, symmetric with the per-agent hard ceiling) — one lost member per sweep, converging across sweeps; this also bounds the `_batch_submitted`/`_batch_progress_ts` leak. Straggler-held partial chunks are bounded by the **hold deadline** (below), not by the member's 30-minute hard ceiling.

- **Digest hold deadline (straggler escape hatch)**: both chunk triggers are event-driven — a COUNT trigger (`SUBAGENT_DIGEST_CHUNK_SIZE` pending completions) and wave close — so neither can fire while a straggler is simply *not finishing*. With the default count (10) above any wave size the concurrency cap realistically produces (2–5), the count trigger is unreachable and wave close becomes the ONLY flush: every sibling's finished result is withheld for the slowest member's entire remaining runtime, and a member that HANGS rather than fails withholds them for the full `_TIMEOUT_SECS` reap — up to 30 minutes of total silence, indistinguishable from a dead session (issue #2215). The reaper's `_sweep_digest_holds` supplies the LATENCY trigger the count lacks: when the OLDEST outstanding hold in a live wave ages past `DIGEST_HOLD_SECS` (default 120s, env `KIROCREW_SUBAGENT_DIGEST_HOLD_SECS`, clamped to `_TIMEOUT_SECS`; `0` opts back out to count-trigger-only), `force_digest_flush` announces a synthetic **flush-only** record through the single completion consumer — the same re-entry mechanism `record_lost_submission` uses, so digest composition, routing, and the held-tombstone settle contract stay in one place. The record carries the wave's `batch_id` but is NOT a member: `_digest_flush_only` makes the gateway skip every per-member side effect (terminal WS event, orchestration tracker accounting, `done`/`ok`/`err` counters, digest lines) and only force the pending chunk out. **One knob, two jobs, now split**: the count keeps bounding digest SIZE for large waves; the deadline caps worst-case delivery LATENCY at every wave size. A wave whose members all finish within the deadline of each other still delivers ONE consolidated digest, so the deliberate small-wave behavior is unchanged. The forced chunk is labelled honestly as a PARTIAL release (`k/k+1`, "N of M delivered, R still running") and tells the parent to synthesize what it has rather than keep waiting. Hold bookkeeping: the gateway stamps `_digest_held_at` when it holds a member and clears it when that member's chunk fires — deliberately separate from `_digest_held`, which is the restart-safety flag the run loop reads and which the sweep must never mutate. The sweep is skipped entirely when `batch_members_pending()` is False, so it can never race the real wave-close digest into a duplicate delivery.
- **Reconnect replay batching** (`ws.py`): more than `SUBAGENT_REPLAY_BATCH_THRESHOLD` (8) replay frames collapse into ONE `subagent_snapshot_batch {items:[{type, data}]}` frame; the client fans items into the per-frame reducers.
- **Stall two-sweep confirmation** (`_maybe_flag_stall`): the first reaper sweep past `_stall_idle_secs` only marks `_stall_suspect_at`; the second consecutive idle sweep flags `stalled` (event + slow-command record). Any stream activity (`_touch_activity`) resets the suspicion. Adds ≤1 sweep interval (~60s) latency; prevents alarm fatigue from healthy-slow agents ambering at scale.

**Retry endpoint**: `POST /api/spawn/{agent_id}/retry` re-spawns a terminal FAILED agent's original task (never running — would double work; never user-stopped — deliberately killed; native rejected). New id, no batch identity carried (a finished wave's digest is never reopened). Backs the UI's "Retry failed (N)" control.

## Hook Integration

### PostToolUse Firing

The subagent loop fires both `PreToolUse` (on `EVENT_TOOL_CALL`) and
`PostToolUse` (on `EVENT_TOOL_RESULT`), mirroring `chat_runner.py`. The
tool name is cached on `EVENT_TOOL_CALL` by `tool_call_id` and looked up
when the result arrives. The `Running: ` prefix is stripped so both hooks
receive identical tool_name strings. Hook errors are caught at debug level
to prevent misbehaving hooks from breaking the subagent loop.

### Hook Payload Metadata

Three optional fields are passed to `ScriptHookStore.fire()` and the
`fire_tool_hooks()` wrapper when called from subagent context:

| Field | Source | Description |
|-------|--------|-------------|
| `subagent_id` | `SubagentInfo.id` | 8-char hex ID of the firing subagent (None for parent) |
| `parent_session_key` | `SubagentInfo.parent_session_key` | Session key of the parent that spawned this subagent |
| `agent_role` | `SubagentInfo.agent` | Agent role name configured for the subagent |

All three default to `None` and are only emitted into `hook_event` when
truthy. Payloads are byte-identical for callers that do not supply them,
preserving backward compatibility for existing hook scripts.

Caller sites:
- `subagent.py`: passes all three at both `fire_tool_hooks` (PreToolUse)
  and `hook_store.fire` (PostToolUse) call sites
- `task_executor.py`: passes `session_key` and `agent` (no `subagent_id`)
- `chat_runner.py` / `llm_helpers.py`: unchanged (parent context, defaults to None)

## Skill Integration

`skills/subagent/SKILL.md` (project-level) triggers on keywords: `background`, `spawn`, `bg`, `subtask`, `parallel`, `separately`, `concurrently`. Instructs the LLM to use `kirocrew spawn "task"` via bash to spawn subagents.

### CLI: `kirocrew spawn "task"`

POSTs to `http://localhost:5476/api/spawn` (dashboard API). Returns immediately with subagent ID. Gateway runs the task async and posts result to Slack when done.

### MCP Tool: `spawn_run`

Exposed via `kirocrew-core` MCP server. Always fire-and-forget — results
are delivered back to the calling session via completion event injection.

**Single task:**
```python
spawn_run(task="search docs for X")
```

**Batch parallel:**
```python
spawn_run(tasks=["search docs for X", "check pipeline status", "review CR-123"])
```

All agents spawn at once. The tool returns immediately with agent IDs.
Results arrive as `[Subagent completion event]` messages in the session,
processed by the LLM automatically.

Parameters:
- `task` (str): single task description
- `tasks` (list[str]): multiple tasks for parallel execution
- `cwd` (str, optional): absolute path to launch subagent in. Must be under a configured `subagent_cwd_allowed_roots` entry (default: `~/workspace`, `~/workspaces`, `~/workplace`, `~/workplaces`). Validated via realpath + prefix match. Pool skipped when cwd is set. These roots are a least-privilege allowlist and are never widened automatically: a persisted list whose roots all fail to exist on the host rejects every cwd, and the operator must edit `agent.subagent_cwd_allowed_roots` (or delete the key to take the shipped default). Neither the loader nor the guard stats the configured roots.
- `max_turns` (int, optional): override tool-call budget for this spawn (default: config or 100)
- `agent` (str, optional): agent name for the subagent
- `include_memory` / `include_lessons` / `include_project` (bool, optional, default `true`): which switchable context groups the subagent inherits, applied to every task in a batch spawn. All-on is byte-identical to the injection a normal session gets, so a caller that omits them changes nothing. `include_memory=false` drops preferences, projects, daily history, semantic and episodic memory, and prior-session provenance — the normal choice for fan-out whose task text is self-contained. `include_lessons=false` additionally drops the user's learned corrections and profile, so keep it on for any subagent that writes code, edits files, or runs git. `include_project=false` drops the docs pointer and the project-directory line. It also drops the injected steering block, but ONLY on the Claude Code backend: on the ACP/kiro backend `kiro-cli --agent` loads the agent's `resources` (including steering globs) itself, which Kiro Crew cannot suppress from here, so steering still reaches an ACP sub-agent regardless of this flag. The conduct group — critical output-format rules, date, agent identity, runtime, workspace identity, and the skills index — is never switchable, because a subagent without it cannot discover its own capabilities or format what it reports back. A subagent is told by name which groups were withheld (`[CONTEXT SCOPE]`) so it reports the gap rather than guessing. Resolved once at spawn, carried through the capacity-queue round-trip and `POST /api/spawn/{id}/retry` like `approval_mode`/`silent`/`keep`. `spawn_continue` does not take the flags but does **inherit** them from the run it continues: a continuation rebuilds session context (`get_or_create` reports `is_new=True` even when it restores the session via `session/load`), so without inheritance a scoped-down run would regain a group on its follow-up turn. See `memory-skills-hooks.md` § Switchable context groups for the section-by-section mapping.

Response semantics:
- An ID means the submission was accepted. A running subagent returns its durable agent ID; capacity/stagger queueing returns a temporary `qN` receipt that is replaced by the durable ID when the queue drains. Use `spawn_list` or the completion event to discover the durable ID rather than treating the receipt as a result path.
- An explicit HTTP error response means the submission was rejected and is reported as `failed to start`; rejected work is never described as queued.
- A transport failure has unknown acceptance status because the gateway may have accepted the work before the response failed. The response warns against automatic retries and directs callers to wait and recheck `spawn_list` or completion events first. An empty immediate `spawn_list` result is inconclusive because the stagger queue is not listed. If the request was truly lost, accepted siblings may remain held until the `_WAVE_STUCK_SECS` backstop (1800s / 30 minutes) reconciles the wave.
- If every submission is explicitly rejected (with no transport uncertainty), the response states that none of the requested subagents were started and does not promise completion events or suggest polling.
- For a partial batch, accepted IDs remain paired with their tasks, rejected tasks appear in a separate failure section, and completion guidance applies only to accepted submissions.

### MCP Tool: `spawn_sub_agents`

Exposed via `kirocrew-core` MCP server. Unlike fire-and-forget `spawn_run`,
`spawn_sub_agents` is **blocking**: it spawns one or more sub-agents in
parallel, waits until all of them finish, then returns their collected
results inline to the calling tool invocation.

Each sub-agent runs as its own KiroCrew-owned ACP session (via
`SubagentManager`), so its text and tool calls stream live to the Activity
tab (`subagent_spawn` / `subagent_chunk` / `subagent_tool` / `subagent_done`
WS events) while the parent blocks.

Native kiro-cli `subagent`/`use_subagent` crews run inside the parent's
kiro-cli process rather than as KiroCrew-owned sessions. KiroCrew surfaces
those in the Activity tab too, by observing kiro-cli's sub-agent
notifications — one card per sub-agent, with each inner tool call and its
output attributed to the right card.

```python
spawn_sub_agents(agents=[
    {"agent_or_mode": "gpu-multiagent-explorer", "prompt": "list python modules"},
    {"agent_or_mode": "gpu-multiagent-explorer", "prompt": "summarize last 5 commits"},
])
```

Parameters:
- `agents` (list[dict], required): each item is `{prompt: str, agent_or_mode?: str}`. `prompt` is truncated to `MAX_MEDIUM_STRING`; `agent_or_mode` to `MAX_SHORT_STRING`. Entries with an empty prompt are skipped.
- `cwd` (str, optional): absolute path to launch all sub-agents in. Must be under a configured `subagent_cwd_allowed_roots` entry (default: `~/workspace`, `~/workspaces`, `~/workplace`, `~/workplaces`), same validation as `spawn_run`.

Blocking poll semantics:
- Each sub-agent is spawned via `POST /api/spawn` (with `parent_session`), then the handler polls `GET /api/spawn/{id}` every 2s until every sub-agent reports `done` (or `error`).
- An errored/crashed sub-agent is treated as settled so one bad agent cannot keep the loop spinning until the deadline.
- The loop pings `POST /api/session-keepalive` every 60s so the gateway's `is_responsive()` does not flag the (legitimately long-blocked) session as stale and SIGTERM the ACP subprocess mid-poll. The `wait` tool pings the same endpoint for the same reason but on a **5s** interval and with a body, because there the reply doubles as an early-end control channel (see `modules/learn-cron-dashboard.md` § Wait countdown and early end); this loop sends `{}` and ignores the reply, so 60s is sufficient.
- `max_wait` defaults to 7200s (2 hours), clamped to `[60, 7200]`, and is configurable via the `KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT` environment variable. The deadline uses `time.monotonic()`.
- Returns a newline-separated list of per-agent JSON results (`status`: `completed` / `error` / `timed_out`), all redacted for credentials and exfiltration URLs.

Difference from `spawn_run`: `spawn_run` returns immediately and delivers
results later via completion-event injection; `spawn_sub_agents` blocks and
returns the aggregated results directly, so the calling agent can reason over
them in the same turn.

## Orphan Recovery & Tombstoning

Folder-per-agent persistence at `~/.kiro/crew/subagents/{id}/`:

```
~/.kiro/crew/subagents/{id}/
  state.json      # {task, parent_session_key, started, pid}
  result.txt      # full result text (written on completion)
  tombstone.json  # {error, elapsed, timestamp} (written on failure/orphan)
```

### Gateway Restart Reconciliation

On startup, `SubagentManager` scans `~/.kiro/crew/subagents/` and reconciles:

1. **PID alive** → kill process group, deliver result if available, tombstone if not
2. **PID dead + result.txt exists** → deliver result to parent session
3. **PID dead + no result** → write tombstone with "orphaned" error

**Orphan delivery is wired** (not a stub): the gateway registers `on_orphan_notify` (session injection — rides the parent slot's batched pending-failures drain) and `on_orphan_dm` (fallback). The DM fallback collects every undelivered orphan across the reconciliation scan and sends ONE digest message (`"N subagent(s)…"`) — never N pings; a lone orphan keeps the plain per-agent message.

### Tombstone Lifecycle

- Created on: process death without result, delivery failure, timeout (`cause` =
  `error` / `timeout` / `cancelled` / `reaped` / `gateway_restart`), **and on
  successful delivery** (`cause="delivered"`, via `mark_delivered`) so `result.txt`
  is retained for the grace window instead of deleted immediately.
- Pruned by reaper: `delivered` tombstones after `agent.subagent_result_ttl_secs`
  (default 1h); all other tombstones after 7 days. `prune_stale_tombstones` takes
  a per-cause cutoff for this.
- `spawn_status` falls back to persistence layer for completed/tombstoned agents,
  reading the retained `result.txt` (and honoring offset/limit/grep).

### MCP Tool: `spawn_status`

Retrieves a completed subagent's transcript by ID. The completion event now
carries a **summary + the `result_path`** whenever the completion copy was
truncated (`result_truncated`) or in orchestrator mode, so the parent reads the
full transcript on demand instead of re-running the subagent.

The full transcript stays in `~/.kiro/crew/subagents/<id>/result.txt` for a
**retention grace window** after delivery — on success the folder is *not*
deleted immediately; `mark_delivered` writes a `cause="delivered"` tombstone and
the reaper prunes it after `agent.subagent_result_ttl_secs` (default 3600s / 1h).
This fixes the prior day-1 bug where `delete_agent_folder` ran immediately on
delivery, so a later `spawn_status` found no file and silently fell back to the
truncated in-memory `info.result` ("truncated at the same place").

Parameters:
- `agent_id` (str, required): subagent ID from the completion event (alnum, max 64 chars)
- `offset` (int, optional): 0-based start line for a paged read (line-oriented, like reading code)
- `limit` (int, optional): max lines to return (1–2000). Omit for the full transcript.
- `grep` (str, optional): case-insensitive regex; return only matching transcript lines (offset/limit then apply to the matches)

When any of `offset`/`limit`/`grep` is set, the `/api/spawn/{id}` response
includes a `result_meta` block (`total_lines`, `matched_lines`, `offset`,
`returned_lines`, `has_more`) and the tool output is prefixed with a one-line
continuation header (`showing lines X-Y of N | more available — call again with
offset=Y`). With no paging params the full-transcript contract is unchanged. The
line split + regex run via `asyncio.to_thread` so a pathological pattern never
stalls the event loop.

### Completion Event Truncation Modes

The character cap and which end of the transcript to keep are both
configurable. Defaults preserve original behavior — opt-in to the others
when a particular agent style benefits from the change.

When truncation drops content (`SubagentInfo.result_truncated`), the completion
event is not a raw truncated blob: it carries a **first+last-words preview + the
`result_path`** (via `context_management.summarize_result`) so the parent reads
the full transcript on demand (read / grep / `spawn_status`) instead of
re-running the subagent. This is the same shape orchestrator-mode deliveries
have always used, now applied to chat mode too (gated on `result_truncated` so
small results still inline in full).

| Config key | Values | Default | Effect |
|------------|--------|---------|--------|
| `agent.completion_keep` | `head` / `tail` / `both` | `head` | Which end of the transcript to keep when the cap is exceeded |
| `agent.completion_keep_chars` | int (`0` disables truncation) | `3000` | Character cap applied after `completion_keep` |

The helper `apply_completion_keep(text, mode, max_chars)` lives in
`context_management.py`. `head` is identical to the earlier
behavior. `tail` is appropriate for agents that summarize at the end
(developer/reviewer/on-call). `both` keeps roughly half the budget at
each end with a middle elision marker.

Unknown `agent.completion_keep` values cause `kirocrew gateway` to fail
at startup via `_validated_completion_keep` in `config/loader.py`. The
dashboard PATCH endpoint enforces the same enum via
`_EDITABLE_CONFIG["agent.completion_keep"]`.

The values are threaded into `SubagentManager.__init__` from
`gateway.py` (`completion_keep=`, `completion_keep_chars=` constructor
kwargs sourced from `cfg.agent.*`). User-facing docs:
[`src/kiro_crew/docs/configuration.md`](../../../src/kiro_crew/docs/configuration.md),
[`src/kiro_crew/docs/subagents.md`](../../../src/kiro_crew/docs/subagents.md),
[`src/kiro_crew/docs/troubleshooting.md`](../../../src/kiro_crew/docs/troubleshooting.md).

### Dashboard API: `POST /api/spawn`

Request: `{"task": "..."}`
Response: `{"id": "abc123", "task": "...", "status": "spawned"}`
Errors: 400 (missing task), 429 (capacity reached), 503 (subagents not available)

### Handler keywords (instant, no LLM)

User-typed `spawn <task>`, `bg <task>`, `spawn list`, `spawn status` are intercepted by the handler for instant execution.

## Session sharing (shared AcpRuntime)

When `agent.session_sharing` is enabled (default **on** for the kiro backend) and
the parent session is kiro-backed, subagents no longer spawn a fresh `kiro-cli`
process each. Instead they open an additional ACP session on a **shared
`AcpRuntime`** — one process multiplexes the parent session plus all of its
subagents. Startup drops from ~3–5 s to ~200 ms and per-subagent memory from
~400 MB to near-zero.

Decision + lifecycle:

- `SubagentManager._should_use_session_sharing(info)` gates the path: config flag
  on, parent session eligible (`SessionManager.is_session_sharing_eligible`), and
  no backend-specific overrides (`model` / `allowed_tools` / `bare`).
- `_create_shared_session()` resolves the parent's `AcpRuntime` via
  `_get_parent_runtime()` (falling back to `SessionManager.get_subagent_runtime()`
  — a companion runtime), calls `runtime.create_session()`, and wraps the handle
  in `AcpSessionProvider`. `SubagentInfo._session_sharing` / `_shared_provider`
  record the shared path.
- On any failure the code falls back transparently to the legacy
  per-process path (`get_or_create`).
- Cleanup (`_run` finally + `_force_reap`) calls `_shared_provider.shutdown()` to
  tear down only the session — it never kills the shared runtime, which other
  subagents may still use. The runtime is killed when the parent session ends
  (`SessionManager.release_subagent_runtime`, invoked from `reset()`).

Non-kiro (alternate ACP backend) parents are never eligible and always use the
legacy `AcpClient` per-process path regardless of the flag.

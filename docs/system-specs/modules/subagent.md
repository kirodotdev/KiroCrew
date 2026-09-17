# Subagent Module

## Overview

The subagent module (`kiro_crew/subagent.py`) spawns isolated background agents for parallel task execution. Each subagent gets its own LLM session via `SessionManager`, runs a focused task, and announces the result via callback.

Supports `on_tool_approval` callback for interactive tool approval (routed through gateway's approval system in Normal/Trust modes).

Private member memory is a durable run identity. `spawn_run(crew=...)` carries
the canonical member name separately from an optional template override through
admission, queueing and provider allocation. The runner revalidates the member's
store and resolves its current template before publishing `kind=member` in the
protected selection. An explicit `agent` overrides only that turn; implicit
continuation after restart returns to the recorded member and its capabilities.
An ordinary spawn inherits the calling session's recorded store. Admission and provider allocation both
validate named stores, and an unavailable store refuses execution instead of
using Global Memory V1. The run transcript carries the same binding for tools
and consolidation.

`create_agent_folder` publishes `memory.json` under the run's protected
`member-memory-bindings/<id>/` directory before dispatch. This top-level tree is
precreated and mounted read-only inside the sandbox, while remaining readable
for cron metadata lookup. The writable `trust/` tree is never a migration source
for memory authority. Continuation
reads that record after a gateway restart, never an agent-edited `state.json`
store field. An absent protected record for a V2 run returns
`memory_unavailable`; unreadable run metadata also refuses rather than proving
legacy absence. A readable legacy run with no memory binding keeps V1. Retry and
continuation preserve the store even when the parent uses different memory.

The protected run record also carries the admitted `memory_mode`. Recreating an
existing record can only retain or tighten that mode; replacing the editable
`state.json` or changing its parent cannot change the protected value. Mode
restoration refuses missing, malformed and legacy records without a mode rather
than interpreting unknown admission as persistent. Store-only legacy lookup keeps
its separate compatibility contract. The creation caller must supply the mode
captured at admission, not re-read a replacement parent when queued work starts.
`SubagentManager` accepts a trusted `memory_mode_for_session` resolver and freezes
its answer before queueing or waiting for approval. The queue carries that value
to the started `SubagentInfo`, and `_log_spawned` publishes it with the binding.
An invalid answer refuses admission; failure to publish the binding blocks provider
allocation for Global runs as well as named stores. Standalone embedders without
a resolver use persistent mode; the gateway must wire its own session-policy resolver.
Before allocating a continuation's provider, the shared run path restores the
original conversation's protected mode off-loop and combines it with the new
caller's admitted mode. It tightens both protected records without rewriting the
original `state.json`; later continuations, including after restart, cannot undo
that restriction. Memory calls refuse while this recovery is pending. Failure
prevents provider allocation, and repeated cancellation drains an in-flight mode
publication before returning. The same allocation boundary covers direct manager
calls and automatic follow-ups, not only the HTTP continuation route.

Dedicated subagents call `messaging.identity.publish_turn_identity` with their
own session key before every stream attempt, including continuation and
transient retries. The publisher resolves the currently registered provider's
host PID through `SessionManager.get_pid`, so a replaced process receives its
own process-start-bound record before its next prompt. Cleanup identity and the
protected dispatch binding do not substitute for this publication. Shared
sessions skip this writer: they do not own the runtime's PID mapping.

Memory-route session recognition checks the full dedicated child key in
`SessionManager`, including the original conversation key reused on continuation.
Shared children are not entries in that manager: `has_live_shared_session` checks
an active, unreaped run and the exact handle queue in the live runtime registry.
A completed run, retained transcript, parent PID, or destroyed/replaced handle
cannot provide this recognition. The existing handle destruction and dedicated
reset paths revoke it without an extra registry or persistence lifecycle.
Private member sessions still require dedicated runtimes and their own proof;
recognition does not bypass store ownership or incognito/temporary policy.

The private-workflow E2E attaches an authenticated owner WebSocket before nested
spawn dispatch, waits for registration, and approves only the returned spawn's
matching request through the existing one-shot HTTP approval route. It grants
no session trust or global auto-approval. Result polling reads the complete
unchanged JSON stream, stops on a failed terminal child, and reports only fixed
state/reason labels plus missing-file and non-JSON poll counts.

The same protected `memory.json` records the run's `app` owner. An explicit
empty string identifies a person-owned run; a missing value does not. Continuation
inherits ownership from the live run or this protected record before spawn
admission, so both governance and internal HTTP attribution retain the app scope.
Tightening a run's memory mode preserves its app owner and selected identity.
It never accepts `state.json` as ownership authority. After eviction or restart,
older runs without a protected app field refuse continuation with `resume_failed`
and require a new conversation. Supplying an explicit template cannot bypass
that refusal.

Continuations retain the original run's selected template or member when `agent`
is omitted.
The runner reads `member-memory-bindings/<id>/agent.json` off the event loop and
validates availability before provider allocation. This record shares the
sandbox-readonly memory identity tree; `state.json` remains diagnostic and never
selects an identity. The private record format is version 2, with `kind` equal to
`template` or `member` and `agent` carrying the selected literal or canonical
member name. An explicit spawn `agent` selects a template. Initial implicit
inheritance takes an immutable `(kind, name)` snapshot from SessionManager's
allocation-owned state: `capability_member` identifies a member even before
capability enrollment, while non-member allocations retain their template name.
Neither current roster membership nor provider attribution fields can change
that namespace. An absent parent retains the default template; malformed or
unavailable selection on a present parent refuses execution.

An initial run publishes that selection; a follow-up copies the original selection,
independently of its one-turn override, before allocating a provider. Continuing
that follow-up, live or after restart, therefore retains the original identity.
Publication failure refuses execution, and cancellation drains the protected
writer before finalization. A recorded empty template keeps the default.
Version-1 nonempty records are ambiguous: earlier writers could record a crew
alias as a template. They refuse implicit continuation without guessing from
today's roster. A version-1 empty record retains its default-template meaning.
An absent, unreadable or invalid protected record likewise refuses implicit
continuation; the caller can supply an explicit template through normal admission
and governance checks. When the original selection is unavailable, that explicit
turn records unknown lineage (`agent: null`, `kind: null`), which refuses later
implicit continuation.
It still publishes through the drained writer, including when reusing a
preassigned run id. Neither writable diagnostics nor the temporary override can
establish the missing lineage.
A caller-supplied template overrides the current turn without changing the
conversation's selection or its independently protected private-memory identity.

Template allocations explicitly pass `crew_agent=""`, including dedicated
fallback after a shared-runtime failure. A same-named member therefore cannot
replace a retained template. Member selections resolve strictly in the member
namespace and must still match the protected memory assignment; a removed member
or changed assignment refuses rather than selecting a same-named template or
rebinding memory. Members use dedicated SessionManager allocation with an explicit
canonical crew claim, preserving capability preparation, generation adoption and
startup verification. The generated provider template never replaces the durable
member identity. Missing templates refuse turns that would execute them.
For an inherited selection, the runner checks its logical name (literal template
or canonical member) against the caller's spawn policy and app scope before
allocation. Omitting the name cannot bypass that check.
Channel `spawn` and `bg` commands resolve the parent's recorded memory binding
off-loop and pass that store into admission. Private bindings retain their
protected-assignment checks; unreadable or mismatched metadata stops dispatch
rather than falling back to Global Memory V1.

Continuation tools keep the original `subagent:<conversation-id>` session key,
while the manager registers each follow-up under its new run id. Internal HTTP
caller recognition resolves a missing original record through the unique active
continuation with that exact conversation key. App ownership uses the same
lookup. A queued or completed continuation cannot replace a missing caller
record, and private memory still requires its canonical protected session/store
binding and verified process proof.

Every backend records the provider's actual working directory alongside its
session id. The next continuation uses this directory even when its target is
itself a completed follow-up and the gateway has restarted. This uses the common
provider `cwd` property, falling back to the admitted directory when unavailable;
the legacy Claude client fallback remains for providers without that property.
When the recorded directory resolves to the current pool default, continuation
omits the redundant override. Disabled overrides therefore do not reject a turn
that stays in the default directory. A different recorded directory remains an
explicit override and must pass current directory policy, including after the
pool default changes.

## Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_MAX_CONCURRENT` | 3 | Legacy fallback / auto-size floor. `agent.max_subagents` defaults to `0` = auto-size the cap (floor 3, ceiling `agent.subagent_auto_max`, default 32); a positive value pins a fixed cap. The cap is re-derived on every config reload, not only at boot — see [`reconfigure`](#reconfigurecfg--max_concurrentnone--live-config). Session-shared subagents are cost-sampled as the runtime's measured RSS/CPU divided by the live shared-session count on that PID (`_live_shared_count`), so the memory term no longer binds and the cap rises to the provider-concurrency ceiling. |
| `_TIMEOUT_SECS` | 10800 | Hard timeout per subagent (3 hours), from `constants.SUBAGENT_TIMEOUT_SECS` |
| `_ON_DONE_TIMEOUT` | 1200 | Outer cap: max total seconds for semaphore wait + injection (20 minutes) |
| `INJECTION_TIMEOUT` | 900 | Inner cap: max seconds for a single `stream_and_collect` call (15 minutes); default `_DEFAULT_INJECTION_TIMEOUT = 900.0`, tunable via `KIROCREW_INJECTION_TIMEOUT` (float seconds, clamped to `_ON_DONE_TIMEOUT`) |
| `_RESET_TIMEOUT` | 30 | Max seconds for session reset in finally block |
| `_TURN_LIMIT` | 100 | Default tool-call budget per subagent (configurable via `agent.subagent_max_turns`, per-spawn via `max_turns`) |
| `_STALL_IDLE_SECS` | 120 | Seconds with no stream activity before a running subagent is surfaced as **stalled** in the running-card (configurable via `agent.subagent_stall_idle_secs`). Surface-only — the idle badge itself never terminates or yields anything; the user closes it from the UX (per-row stop / Stop-all) and the 30-min `_TIMEOUT_SECS` ceiling still applies. A WATCHDOG stall (`EVENT_COMPLETE` with `error: tool stall`) is a different signal and IS acted on: see *Stop reason → state*. |
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
  rejects unrouted subprocess spawns. A reload re-runs it off the loop
  (`asyncio.to_thread`), since by then the loop is serving turns.
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

The constructor also registers the manager on the process config watcher
(`live.watch_object(self, *self.LIVE_CONFIG_PATHS, name="SubagentManager")`, kept
on `self._config_sub`; the watcher holds the owner weakly, so a discarded manager
drops out on its own). The prefixes ARE the watched-path list, so the dispatcher
filters an unrelated `agent.*` write rather than the applier re-deriving on it.

### `reconfigure(cfg)` / `apply_limits(cfg, *, max_concurrent=None)` — live config

`reconfigure` is the watcher's entry point and is `async`: the concurrent cap can
auto-size from host memory, which is filesystem I/O, so it is resolved off the
loop and handed to the synchronous `apply_limits`, which does the whole apply.

Every limit the constructor copies out of `config.json` is a LIVE value: a
reload that touches any path in `SubagentManager.LIVE_CONFIG_PATHS` re-derives
ALL of them from the new config, whichever writer produced it (dashboard,
`kirocrew config set`, `$EDITOR`). No gateway restart is needed for:

| Config path | Manager field | Normalization (same as the constructor) |
|---|---|---|
| `agent.max_subagents`, `agent.subagent_auto_max`, `agent.subagent_mem_buffer_pct`, `agent.subagent_cost_gb`, `agent.subagent_cpu_cost_cores`, `session.pool_size` | `_user_max_concurrent` (and `_max_concurrent` re-clamped) | `resolve_max_subagents(cfg)` — explicit pin (floored at 3) or the host-sized auto value |
| `agent.subagent_max_turns` | `_default_turn_limit` | `int` |
| `agent.subagent_timeout_secs` | `_default_timeout` | `0` keeps `_TIMEOUT_SECS` |
| `agent.subagent_stall_idle_secs` | `_stall_idle_secs` | `0` keeps `_STALL_IDLE_SECS` |
| `agent.subagent_spawn_stagger_secs` | `_spawn_stagger_secs` | floored at `0.0` |
| `agent.subagent_result_ttl_secs` | `_result_ttl_secs` | `int` |
| `agent.completion_keep`, `agent.completion_keep_chars` | `_completion_keep`, `_completion_keep_chars` | via `update_completion_keep` |

`agent.approval_mode` is NOT in this table on purpose: it is boot-only (schema
`restart=True`), because every channel dispatcher resolves it once at start
together with the CLI `--approval` override, and one consumer taking it live
while the others keep the boot value would make the UI's "restart required"
true for some tool calls and false for others. `_global_approval_mode` stays the
value cached at construction.

The applier (`reconfigure`) resolves the cap with `asyncio.to_thread` because
auto-sizing reads `/proc/meminfo` / cgroup files, then calls
`apply_limits(cfg, max_concurrent=cap)`; a direct `apply_limits(cfg)` resolves
the cap inline. A resolution failure keeps the current cap and logs at WARNING.

Every consumer reads the manager attribute at the point of use — the admission
gate (`_should_stagger_queue_impl`, `_drain_queue_impl`), the run timeout
(`asyncio.wait_for(..., timeout=_default_timeout)`), the reaper's TTL prune
(`_result_ttl_secs`) and stall detector (`_stall_idle_secs`), the parentless
approval policy — so the assignment is the whole apply. Two cap-change
invariants:

- **Raising the cap admits queued spawns.** `reconfigure` calls
  `_notify_cap_raised()` when the new cap exceeds the old one; it drains a
  non-empty queue through the pump, which re-checks the gate and honours the
  stagger interval, so freed capacity fills one start per
  `subagent_spawn_stagger_secs`, never in a burst.
- **A raise also reaches the gate that is not this manager's.** The same
  `_notify_cap_raised()` rings `set_cap_raise_listener`'s hook — the runner lane
  (TaskRunner steps, workflow `ctx.agent()` calls), which is bounded by
  `_max_concurrent` but keeps its own waiters. It reads the cap live, so it
  always SEES a raise, but a waiter parked while the cap was `0` holds no slot
  and no release of its own will ever wake it: the raise is its only edge, and
  an empty subagent queue must not suppress it. `_notify_cap_raised` therefore
  runs on the event loop, never from a worker thread — the lane resolves
  futures. Details: [taskq.md](taskq.md) § Runner adapters.
- **A resume is not a start.** A `_resume_id` window entry (a live run whose
  wait ended) is granted before the stagger check and does not bump
  `_last_spawn_ts`: the run is already resident, so there is no process burst
  to smooth, and an in-place recovery no longer pays the stagger interval.
- **Lowering the cap cancels nothing.** In-flight runs keep going; the gate
  simply admits no new spawn until `_running_count` drains below the new cap on
  its own.

The advisory cap the `spawn_run` tool description advertises (`mcp_tools/spawn.py`)
re-resolves through the same `resolve_max_subagents(KiroCrewConfig.load())` on each
tool listing, so the advertised and enforced numbers agree after a write.

### `set_effective_cap(cap | None) -> int` — the adaptive-controller seam

The resolved user cap is a **ceiling**, not the live value. Two fields:
`_user_max_concurrent` is what `apply_limits` writes (the pin or the auto-sized
value); `_adaptive_cap` is what the adaptive concurrency controller
([`adaptive-concurrency.md`](adaptive-concurrency.md)) writes through
`set_effective_cap`. `_max_concurrent` -- the attribute every admission read
site consults -- is always `min(_user_max_concurrent, _adaptive_cap)` (or the
user cap alone when no bound is set). The two writers never touch each other's
field: a config raise cannot lift the adaptive bound, a config cut below the
bound clamps it, and the controller never writes `config.json`. `None` removes
the bound; `0` pauses new grants (in-flight runs finish; nothing is cancelled).
A raise goes through the same `_notify_cap_raised` a config raise uses: the
staggered queue drain, plus the runner lane's `pump()` (see the two cap-change
invariants above). Neither raise site grants past the new cap.
`reconfigure`'s "sizing unchanged" path re-applies `_user_max_concurrent`, not
`_max_concurrent`, so a reload can never shrink the ceiling to the bound.
`user_max_concurrent` exposes the ceiling; `max_concurrent` stays the effective
value the gate enforces and the dashboard's capacity error reports. A fresh
gateway starts bounded at `min(user_max, agent.adaptive_initial)` and earns its
way up.

### `spawn(task, parent_session_key="") -> SubagentInfo | None`
Spawns a background agent. Returns `SubagentInfo` or `None` if at capacity. Uses atomic `_running_count` to prevent race conditions. `parent_session_key` tracks the originating session for completion injection.

Admission order (`subagent_manager/admission/gate.py::spawn_impl`):
1. Policy refusals that leave no durable trace: empty task, memory identity,
   `cwd` outside `subagent_cwd_allowed_roots`, spawn governance.
2. **Persist** the row in the task store (write-before-ack; see § Durable task
   queue). A store write failure is a refusal with
   `error_code="task_store_unavailable"`; the id is never handed out as accepted.
3. Memory floor (`spawn_min_memory_gb`) and posture gate (`admission_gate`,
   `cached_admission_check`): with a store, **defer** (row stays `queued`,
   `next_run_at = now + admit_wait_secs`, pump wake-up armed, caller gets a
   `queued` id); without one, refuse as before.
4. Capacity / stagger gate: queue (window or store-only) or proceed.
5. Agent name validation (a failure marks the row `failed`), then the **atomic
   claim** (`admitted`, generation++). A row cancelled while it waited fails the
   claim here and is never started.
6. Register, take the slot, `starting`; then the approval branch below.

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
2. **Hook auto-approve** — `hooks.on_tool_call()` returns `TOOL_AUTO_APPROVE`
   (the `auto_approve_tools` globs / read-only allowlist — a grant made by
   program NAME), honoured only after `name_grant.refusal_for_event(event)`
   confirms each program name in the shell command still resolves to the
   program it appears to name. A refusal DOWNGRADES to rungs 3–5 (never a hard
   block) and is audited as `outcome=auto_approve_declined` with
   `reason=name_grant`, the refusal code, and `tier=hook_auto_approve`. This
   matters most here: the subagent surface runs unattended, so an unverified
   shadowed name would be honoured with nobody watching. On Windows the check
   cannot model the shell's lookup at all, so it declines every name-based
   shell grant there — a headless subagent (no parent `auto` policy, no
   interactive approver) then rejects shell tools its allowlist used to grant.
3. **Parent policy** — `parent_policy == "auto"` → auto-approve. Resolved once
   at `_run_inner` start (see the chain below); an active global YOLO folds
   into this snapshot rather than being re-read per event.
4. **Interactive callback** — `on_tool_approval` (races dashboard + Slack, 2h timeout)
5. **Deny by default** — none of the above matched → reject

`parent_policy` is resolved once when `_run_inner` starts, using this chain:
1. Read from parent session via `get_approval_policy(parent_session_key)`
2. If empty and YOLO mode active → `"auto"`
3. If still empty **and subagent has no parent session key** → use the cached `KiroCrewConfig.agent.approval_mode` (snapshotted at `SubagentManager` init); if `"auto"` → `"auto"`

Step 3 ensures parentless subagents (e.g. cron jobs) respect the user's
global approval mode instead of falling through to interactive approval.

**Child-fidelity gate.** A child-origin permission event whose SECURITY context
is absent (`AcpEvent.child_low_fidelity`: structured params never reached the
tool_call cache, unresolved shell classification, or a shell without a
recoverable command) skips steps 2–3 and is handed to the interactive callback
with an "UNVERIFIED child request" annotation (headless: rejected), because
every field a shortcut would judge is agent-authored. One carve-out: when the
event's canonical MCP identity IS verified (`child_mcp_identity_trusted` — the
`_meta.kiro` server/tool pair resolved from the tool_call cache, carrying the
explicit `mcp_identity_trusted` provenance flag those cache hits set, resolved
non-shell; the shape a remote MCP server produces by streaming empty
`rawInput`), the **unconditional** `parent_policy == "auto"` grant still
auto-approves — the call site reads the hoisted
`AcpEvent.child_unconditional_grant_eligible` property: its decision consumes
no agent-authored event data, only the
arguments remain unverified. The hook auto-approve (title-pattern-matched) and
every content-matching path stay fail-closed on the composite fidelity.

The `is_yolo()` read happens once, when `parent_policy` is resolved at
`_run_inner` start — a YOLO toggle mid-execution takes effect on the next
subagent run, not on the current run's remaining tools.

### `cancel_for_parent(parent_session_key) -> (running, queued)`
Stops every running agent and removes every not-yet-started stagger/concurrency
queue entry owned by one parent session. A `_resume_id` entry is NOT one of
those: it is a RESIDENT run asking for the lane slot it yielded back, filed
under that run's own `_preassigned_id` and its parent key, so it matches both
terms of the queued scan and is skipped there — exactly as the pump's grant
loop, the refill's lane census, the eviction, the child reserve and
`_conversation_busy` all separate it out. The running sweep stops such a run instead, where its intact `_agents`
record still is: routing it through the queued-stop path publishes a synthetic
`queued=True` "never started" terminal OVER that record, so the coroutine keeps
executing, the parent is told the work never began, and `resume_grant` can never
return the slot because the record it reads is gone. Pinned by
`test_overload_integration_glue.py::test_stop_all_reaps_a_resident_resumed_run_it_never_treats_as_queued`
and `::test_stop_all_never_takes_the_queued_stop_path_for_a_claimable_resident_row`. Queue removal happens before the first
suspending await, so a scheduled drain cannot start work after the stop request.
Each removed queue entry emits a neutral stopped terminal record through the
normal completion consumer, which closes batch accounting instead of stranding a
wave. Those synthetic records remain marked as never started while their terminal
reports are pending, so bulk cancellation cannot rediscover them as running work.
Agents waiting on spawn approval are excluded; their approval card remains the
authority for approve/reject.

The dashboard exposes this through `POST /api/spawn/stop-all` with a validated
slot name. App tokens are denied before slot lookup because ownership of an app
slot does not imply ownership of its linked session; a request missing the
authentication middleware's app claim is denied on the same fail-closed path.
This bulk control remains a dashboard-only capability. The server resolves that
slot's effective session key, including channel-linked chats, rather than
accepting a client-supplied parent key. The in-chat Stop all control uses this
endpoint and remains available for queued-only waves.

### `cancel_all() -> None`
Cancels all running subagents, stops the reaper loop, and awaits their cleanup. Handles `CancelledError` gracefully — sessions released, count decremented.

### `steer_run(agent_id, message) -> (ok, detail)` / `follow_up_run(agent_id, message) -> (ok, detail)`
Two delivery modes for `spawn_steer` (REST `POST /api/spawn/{id}/steer`, body `mode`: `"interrupt"` default / `"follow_up"`). `steer_run` injects into the RUNNING turn via the provider's `steer`, with a bounded startup-grace poll for a live run whose session has not registered yet (#1113). `follow_up_run` never interrupts: it queues the message on `SubagentInfo.pending_followups` and arms a one-per-run watcher (`_deliver_followups`, registered in the manager-owned `_followup_watchers` dict — NOT the global `_safe_fire` set — because a watcher can spawn a brand-new run and must therefore be reachable by `cancel_all()`, per the same containment contract as `_schedule_cancel_recovery`; `cancel_all` cancels watchers BEFORE the run tasks so none can dispatch into a shutting-down gateway, and the watcher re-checks `_shutting_down` before dispatch). The watcher waits for the run to complete (`info.done` AND its task popped from `_tasks`, so teardown is finished), then dispatches the whole queue as ONE `continue_conversation` on the run's own conversation (messages joined in arrival order — three corrections cost one continuation, not three). The continuation is a normal new run on the same parent session, so its result arrives as a separate completion event. OUTCOME-AWARE: a run the user explicitly STOPPED (`user_stopped`) suppresses dispatch (`followup_suppressed` audit) — resurrecting killed work is the opposite of "the correction can wait"; error/timeout terminals still dispatch (the continuation carries the conversation's context, so "fix what broke" is legitimate). NEVER SILENT: every undeliverable path (suppressed, watcher expiry, dispatch failure) announces a SYNTHETIC failure completion event through the normal `_on_done` path, because the spawn_steer reply promised the parent an event — `followup_expired`/`followup_failed`/`followup_suppressed` SEL audits alone would leave the parent blocked on an event that never comes. Deliberately a per-run poller, NOT a hook in `_run`'s 3-guard finalization: completion is reached from many terminal paths (normal/error/timeout/cancel-recovery/reaper) and a watcher observes the outcome without adding an obligation to any of them. Bounded everywhere: poll cadence 2s, hard deadline `default_timeout + 300s`, and residual `conversation_busy` after done gets a bounded retry. Typed refusals mirror steer: `not_found`, and `not_running` (use `spawn_continue` directly on a finished run).

### Properties
- `running -> list[SubagentInfo]` — currently running agents
- `count -> int` — number of running agents
- `max_concurrent -> int` — the EFFECTIVE capacity limit (`min(user cap, adaptive cap)`)
- `user_max_concurrent -> int` — the user's resolved cap, the ceiling the adaptive bound sits under

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
    stop_reason: str      # ACP stop_reason of the completion that ended the run
    stop_class: str       # classify_stop_reason() class (succeeded|stalled|recovering|cancelled|failed)
    partial: bool         # result is text streamed before a non-success completion
    _awaiting_approval: bool  # blocked on a human approval prompt (spawn gate or mid-run tool) → exempt from idle-stall
```

## Session Lifecycle

1. `spawn()` increments `_running_count`, creates asyncio task
2. `_spawn_with_approval()` (non-YOLO): re-checks YOLO, requests approval with 2-min timeout
3. `_run()` wraps `_run_inner()` with `asyncio.wait_for(_TIMEOUT_SECS)`
4. `_run_inner()` resolves `parent_policy` (parent session → YOLO fallback → config fallback), creates session `subagent:{id}` via `SessionManager.get_or_create(approval_policy=parent_policy)` — policy is persisted on the new session
5. Streams through ACP with context injection, tool approval cascade, and turn counting
6. On completion (in `_run` finally block): fire `subagent_done` WS event immediately (before slow reset + on_done), then `sessions.release()` → `_running_count -= 1` → `sessions.reset()` → call `on_done` callback
7. On timeout: `error = "Timed out after 180 minutes"`
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

## Stop reason → state (`classify_stop_reason`)

`EVENT_COMPLETE` only says the stream ENDED. The ACP layer sets `stop_reason` on it (`acp/types.py`: `end_turn`, `cancelled`, `stale_recover`, `refusal`, `error: tool stall`, `error: compaction failed`, `error: …`), and every completion consumer — the main chat (`dashboard/chat_runner.py`), the sub-agent run (`subagent_manager/run.py`), nested children (same path) and the task runner (`task_executor.py`) — maps it through ONE function, `acp.types.classify_stop_reason(stop_reason, *, compaction_transient=False) -> StopClass`. No entry spells its own `startswith("error:")` or a private retry budget; `test_subagent_stop_reason_consistency.py` pins both the table and each entry's use of it.

| `stop_reason` | `StopClass.name` | recoverable | Sub-agent run | Main chat | Task runner |
|---|---|---|---|---|---|
| `end_turn`, absent | `succeeded` | — | `outcome=completed`, `record_success`, taskq `done` | normal completion | step `PASSED` |
| `error: tool stall` | `stalled` | yes | continue-nudge in place ×`STOP_RECOVERY_MAX_RETRIES`, then `failed` (partial flagged) | `slot._tool_stall_retries` continue-nudge ×`STOP_RECOVERY_MAX_RETRIES`, then "Session stuck" | attempt fails → existing bounded retry ladder (retry prompt names the stall) |
| `stale_recover` | `recovering` | yes | same budget as `stalled` (the sub-agent has no reset+resume ladder; a truly wedged session stalls again and exhausts the budget) | session reset + resume + continue-nudge, `slot._stale_recovery_retries` | attempt fails → retry ladder |
| `cancelled` | `cancelled` | no | `user_stopped` → neutral `stopped` (error unset); otherwise `error="cancelled (stop_reason=cancelled)…"`, tombstone `cancelled` | user stop | attempt fails → retry ladder |
| `error: compaction failed` | `failed`; `recovering` only with `compaction_transient=True` | only transient | terminal `failed` (verdict not passed: no in-place replay is safe) | passes the ACP verdict; transient + nothing emitted → re-queue | attempt fails |
| `refusal` | `failed` (`retryable=False`) | no | `failed` | actionable refusal notice, no retry | attempt fails |
| other `error:*` | `failed` (`retryable=True`) | no | `failed` | pipe-death re-queue (`_acp_pipe_death_retries`) | attempt fails |
| anything else | `failed` (`known=False`) | no | `failed`, error names the unexpected reason | logged as unexpected, handled as `failed` | attempt fails |

`STOP_RECOVERY_MAX_RETRIES` (3) is the one continue-nudge budget shared by the main chat and the sub-agent run. It is a re-export of `recovery.ladder.SESSION_RECOVERY_MAX_ATTEMPTS`, the ladder's L3 in-place budget, which the main chat's pipe-death re-queue (`slot._acp_pipe_death_retries`) reads as well — one number for every "continue this session on the same runtime" count (`test_runloop_integration.py` pins the identity).

**Sub-agent in-place recovery** (`_stream_with_transient_retry` → `_yield_for_stop_recovery`): a `recoverable` completion is WITHHELD from the run loop while budget remains and no terminal marker (`user_stopped`, `_reap_started`, `reaped`, `_shutting_down`) is set. The run then

1. **yields its LANE slot through admission** — `admission.yield_slot(info, WaitRecord.dependency("session:<stop class>", source=liveness_oracle))`: `_release_slot` + `_running_count -= 1` + `_drain_queue()`, so queued work starts; the session (process, FDs, memory) stays alive and keeps its residency charge (SPEC-ADDENDUM §2: the logical slot is released, real resources are not pretended away). If a typed `waiting_input` status already yielded the slot earlier in the stream (below), this step is skipped;
2. the durable row goes **`running -> waiting_dependency` under the run's own lease** (`wait_json` names the scope and the oracle as evidence) and a `stop_recovery` task event + progress marker (`{phase: yielded|readmitted, stop_class, attempt, slot_released}`) is appended. It deliberately does NOT transition to taskq `recovering`: that is the LOST-OWNER state, drops the lease and makes the id claimable — i.e. a duplicate run of the same task while the owner is alive;
3. emits `subagent_waiting {state, reason, resume, residency_charged}` (from `yield_slot`), `subagent_recovering {attempt, max, stop_reason, stop_class}` and a SEL `subagent.stop_recovery` record;
4. **re-admits through the pump** (`_await_lane_resume`: `admission.request_resume` queues a `_resume_id` entry at the front of the window; the coroutine parks on `info._resume_event` until `resume_grant` sets it — slot held again, row `running` under a NEW generation, `subagent_resumed`; bounded by `_RECOVERY_SLOT_WAIT_SECS`; never a poll of the running count) and sends `TOOL_STALL_RECOVERY_PREFIX` + `build_tool_stall_recovery_prompt(...)` on the SAME session — the preserved partial is finished, the original task is never re-sent. `stuck_input` is read from the completion's typed `status.wait_reason == waiting_input` first, the evidence-text marker second. If re-admission is refused (deadline, shutdown, stop, reap) the resume entry is withdrawn, the budget is spent and the withheld completion is surfaced as `failed`.

**Typed input wait (W4)**: an `EVENT_STRUCTURED_STATUS` frame whose `status.wait_reason` is `waiting_input` (execution layer or liveness oracle; `acp-client.md` § Structured status protocol) makes the run yield its lane slot at once — `WaitRecord.input(tool_call_id)`, row `waiting_input`, `subagent_waiting {state: waiting_input, resume: input}` — while the blocked process stays resident. Under the default `cancel` policy the `error: tool stall` completion that follows re-enters through step 4 above.

**L1 — a tool call the gateway refused** (`_yield_for_infra_retry`): a completion that ended NORMALLY (`end_turn`) while the session handle's `last_infra_error` is an `InfraError` (the MCP stub's `-32001 capacity`, backend gone, spawn-queue timeout) is withheld the same way. `default_ladder().observe_failure(L1_tool_call, unit="subagent:<id>", retry_after_secs=…)` decides retry-or-escalate and gives the jittered delay; on `retry` the delay becomes the `retry_at` of a `DependencySignal` on scope `mcp_gateway:<class>` and the run parks on the coordinator (below) so every sub-agent refused by the same gateway waits on ONE schedule and wakes by capacity; the continuation is `build_infra_retry_prompt` (re-issue exactly the refused call) on the same session. Once the ladder escalates, the completion is surfaced as the normal completion it was — the parent sees the refused call in the result. This is in-place, not a `retry_wait` re-dispatch: a registered run re-dispatched under its own id would double-report.

**Durable row timing**: the row is `starting` from claim through session creation, the session-start gate and any late adoption; it becomes `running` at the run's FIRST stream event (`ensure_running_marked`, `SubagentInfo._taskq_running_marked`) — or, for a provider error / stall that lands before any frame, at the moment the wait is recorded (the turn WAS issued on a live session, and a wait can only be written on a `running` row; a `starting` row would be parked `retry_wait` instead). A late adoption writes `running` at adoption (it leaves the claimable `recovering`).

The MECHANISM that delivers that invariant is the write channel, not luck. `ensure_running_marked` POSTS the mark and `yield_slot` POSTS the wait write, so on the store's single writer thread the wait lands behind the mark even when both are issued in ONE synchronous block — which is the normal case, because `session_handle` yields every event of one update back to back and appends the structured status to that same list. The dependency path is the exception and does not come through `ensure_running_marked` at all: `coordinator.report`'s wait write chooses wait-vs-park from its own result in the same critical section, so `_dependency_verdict` hands `(coordinator, store, task_id, signal, generation)` into `_dependency_report_db` and the mark plus `report` run as ONE unit on the writer thread. The mark it hands over is UNCONDITIONAL — `TaskStore.advance` is what decides, never the in-memory `info._taskq_running_marked`, which is published ahead of a posted write whose refusal reaches nobody, so a flag-gated park would write `retry_wait` on a live resident run. Details and the refused-edge consequences: [taskq.md](taskq.md) § Off the event loop.

**Record**: `SubagentInfo.stop_reason`, `stop_class` and `partial` (True when text streamed before a non-success completion) are set on the completion that ended the run and carried on `subagent_done`; the gateway's parent injection appends the partial under an explicit "did NOT finish" banner when `partial` is set. `outcome` stays the three-way canonical source (`stopped` / `failed` / `completed`); the class says WHY.

**Task runner**: `task_executor.execute_single_task` raises `_TurnNotCompleted(stop_class, stop_reason, partial=…)` inside the attempt's `try` for any non-success class, so the existing ladder (`MAX_RETRIES`, same-error loop detection, session reset between attempts) owns the recovery and `task.result` keeps the partial.

## Transient Retry (mid-stream 5xx) and dependency waits

`_run_inner` streams through `_stream_with_transient_retry`. A transient backend error (the `-32603` class, per `acp_error_is_transient`) is first shown to the dependency adapters (`taskq.dependency.classify_exception`, [taskq.md](taskq.md) § Dependency waits). When the manager has a durable store and an adapter recognises the error — a provider throttle (`rate_limited`, `concurrency_exceeded`), a 5xx / connection loss (`dependency_unavailable`) — the run does NOT retry in the turn: `_yield_for_dependency` calls `coordinator.report(id, signal, generation)` (ONE store write: `running -> waiting_dependency` with the scope's `retry_at`), releases its lane slot through `admission.yield_slot(persist=False)`, reports a typed throttle to `AdaptiveController.record_provider_throttle(scope)`, arms the pump and parks on `_await_lane_resume(request=False)`. The coordinator wakes the scope by capacity — one probe, then `wake_per_tick` per `wake_spacing_secs` — and a live run's wake comes back through admission (`monitoring.taskq_wake_through` → `request_resume`; admission's `resume_grant` writes the `wake_wait` under the run's generation, so one wake is one write). After the grant the run replays the original prompt (zero activity) or sends `_TRANSIENT_CONTINUE_MSG` (any activity, one-shot, same rule as below). A scope the coordinator fails (attempts cap, wall-clock deadline) releases the parked run through `on_fail` (`SubagentInfo._wait_failed`) and the run ends `failed` with the provider error; a terminal signal (`auth_failed`, `permanent_param_error`, quota with no reset) propagates at once. Two runs throttled by the same provider therefore share one schedule and never two timers; the main chat reads the same schedule (`chat_runner._shared_dependency_delay`) without joining it.

The in-turn ladder below handles what no adapter classifies (and every transient when the durable queue is off — `agent.task_queue_enabled=false`): errors are retried with exponential backoff on the same live session; each retry fires a `subagent_retrying` WS event (chip shows `⟳ retrying`) and a SEL audit record. **Replay-safety**: if ANY activity was observed (text chunk, approved tool turn, or auto-allowed tool call), the retry sends `_TRANSIENT_CONTINUE_MSG` instead of the original prompt — a mutating tool may have executed before the first text chunk, and replaying the full prompt would re-run it. **Budget**: `TRANSIENT_RETRIES` applies only while ZERO activity was observed (replaying the bare prompt is side-effect-free); after any activity, recovery is ONE-SHOT — exactly one continuation turn, matching the main path's `_posttoken_retry_used` rule, since each post-activity continuation is an independent opportunity to repeat a side effect. The two ladders (this one and `dashboard/chat_runner.py`'s) are intentionally-identical copies cross-referenced in both sources; a change to either's predicate or budget must be mirrored. Non-transient errors and exhausted budgets propagate to the generic error arm.

## Unexpected-Cancel Recovery (one-shot auto-continue)

An unmarked `CancelledError` (see intentional-cancel rule) triggers `_schedule_cancel_recovery`: exists for cancellations arriving from outside the manager's lifecycle (parent task-tree teardown around a live subagent), mirroring the main path's PR #173 recovery. Mechanics:

- **Side-effect gate**: recovery fires ONLY when `tool_count == 0`. The respawn runs on a fresh session with no ledger of prior tool calls, so once any tool has executed the model cannot verify which side effects already happened — the run is finalized instead (error `"cancelled (auto-continue suppressed: tools already executed …)"`, partial output preserved and delivered). Text-only activity is safe to resume.
- One-shot: gated by `info._cancel_retry_used`; the recovered run's own cancel is terminal.
- Explicit handshake: `_resume` awaits the ORIGINAL task's full teardown (session release/reset, slot decrement, registry pop) before respawning — never a timed sleep.
- Slot re-acquisition: waits (bounded, `_RECOVERY_SLOT_WAIT_SECS`) for free capacity; the slot claim and `create_task` are ATOMIC (no await between) so a concurrent `_drain_queue` cannot overshoot `max_concurrent`. The dispatcher honours the same invariant from its side with reserve-then-commit: when `spawn_impl` stops at the store claim (`ClaimPoint`) it has ALREADY taken the slot (`_running_count += 1`, stagger token) synchronously, the claim is awaited on the writer thread, and the re-entry (`_claimed=`) consumes that reservation instead of counting again; a claim the store refused or could not take, or a refusal at re-entry, releases it (`release_reservation`). So nothing admitted during the await -- a `spawn`, a `resume_grant`, a recovery re-acquisition -- can take a slot the dispatcher is about to use.
- Shutdown-reachable: the pending `_resume` task is registered in `_tasks` under `"{id}:recovery"` so `cancel_all()` cancels it; a cancelled recovery finalizes the record terminally and never respawns.
- Failed recovery (no slot / teardown timeout) still fully finalizes: `subagent_done` emitted, tombstoned, delivered via `on_done`.
- Replay-safety at respawn: when the first attempt streamed partial text, the respawned prompt is prefixed with `_CANCEL_RESUME_PREFIX` so the model continues instead of restarting (the prefix also gates on `tool_count` as defense-in-depth, though the side-effect gate above means a tool-activity run never reaches respawn). A bare original prompt is re-sent only for a zero-activity first attempt.

## Reaper Loop

`start_reaper()` launches a periodic loop (60s interval) that force-kills subagents exceeding the configured timeout deadline. Defense-in-depth for cases where `asyncio.wait_for` fails to fire due to event-loop saturation or orphaned tasks.

- `_reaper_loop`: sweeps every 60s, calls `_force_reap` on expired agents
- **taskq pump** (`OrphanStallMonitor.taskq_pump`, facade `_taskq_pump`): `start_reaper` runs it once after `taskq_boot_dispatch` (building the manager's `DependencyCoordinator` from `agent.dependency_*` over the admission store, `capacity = _max_concurrent`, and running `coordinator.rebuild()` after `open_default_store` ran `WaitLedger.rebuild()`), every sweep re-runs it as the backstop, and every run that parks on a wait calls it. One pass = `admission.taskq_expire_waits()` (wait deadlines) + `coordinator.tick()` (due scopes) + a one-shot `loop.call_later` re-armed at `coordinator.next_deadline()`, so a scope is woken when it is due, not on the next 60s sweep. The coordinator is registered process-wide (`taskq.dependency.register_coordinator`) for the main chat's read of scope schedules. Terminal runs call `coordinator.forget(id)` from `_run`'s finally (a finished probe is the scope's recovery signal) and withdraw any pending resume entry.
- `_force_reap`: reset with 30s timeout → SIGKILL fallback → mark done → fire `subagent_done` WS event
- **Startup-stall admission ends when the first provider stream begins.** A provider may create its child process lazily from `stream()`, so a missing PID before the first response is not proof that execution never started. The marker resets for every recovery execution; the startup watchdog may reap only a subagent with no first stream, no runtime PID, and no completed turn. The ordinary wall-clock deadline remains unchanged.
- **Terminal completion is arbitrated by FOUR separate guards, not by `reaped` alone.** Two paths can finish a subagent — `_force_reap` and `_run`'s `finally` — and between them there are four distinct one-time concerns. Earlier revisions tried to arbitrate them with `reaped` plus `done` and every attempt satisfied two while breaking a third (duplicate delivery when the marker was set late; a lost outcome when it was set early and the reaper was cancelled; a lost outcome when the claim was handed back to a run that had already exited; and finally **no reporter at all plus a leaked concurrency slot** when the report claim was gated on `not info.done`). The guards are now:
  1. **`info.reaped` — classification.** Was this a deliberate reap? The cancel-recovery scheduler reads it, and the marker MUST precede the intentional cancel (see the intentional-cancel rule above) or an unexpected-cancel respawn fires on the run being killed. Unchanged.
  2. **`if not info.done` — the terminal RECORD.** Error synthesis, failure stat, tombstone, cost. First-arrival-wins, so it is never written twice (pinned by `test_subagent.py::TestOnDoneTimeout::test_force_reap_skips_tombstone_when_already_done`).
  3. **`_release_slot(info)` — SLOT accounting.** A one-shot token per `SubagentInfo`; the winner decrements `_running_count` once and drains the queue. Deliberately independent of both flags above: inferring slot ownership from `done` or `reaped` produced a double decrement in one interleaving and none at all in another. A leaked slot permanently starves the spawn queue, which matters far more at the 60-100 concurrent agents the scale work targets. The cancel-recovery respawn **re-arms** this token when it re-admits a slot (`_running_count += 1`), because the respawned run occupies a fresh slot and needs its own release.
  4. **`_claim_finalize(info)` — REPORT ownership** (`subagent_done` + the `_on_done` injection, plus wave-digest settling and the result.txt TTL bookkeeping). Granted to exactly one caller; contains no `await` so the check-and-set is atomic on the loop. It does **not** consult `info.done` — that was the last defect. It returns False while `_recovering` *without consuming itself*, so a pending respawn is not reported done and its respawned run can claim later.
- **A claimed report is atomic, not merely exclusive.** The claim alone still lost outcomes when the claimer was cancelled mid-report. `_report_terminal` therefore runs on a strongly-referenced task under `asyncio.shield`, spawned by `_run` **before** its teardown awaits so the task is already live wherever a cancellation lands; the caller still receives `CancelledError` while the report completes. `cancel_all()` drains outstanding reports with a bounded timeout and then **cancels and gathers** any straggler, so none is left invoking `_on_done` against tearing-down state or killed by a closing loop. Because the awaiter is shielded, shutdown is bounded by that drain rather than the `_ON_DONE_TIMEOUT` injection cap. Enforced by `test_subagent_reap_race.py`.
- **An undelivered report abandoned at shutdown is made RECOVERABLE, not silently dropped.** The terminal record — including the tombstone — is written before delivery is attempted, and a tombstone is exactly what `list_orphans()` uses to EXCLUDE a folder from the next start's reconciliation. So cancelling a still-pending report at the drain deadline would leave an outcome that was never injected *and* invisible to the only path that could still inject it. `cancel_all()` therefore calls `clear_tombstone(id)` for each report it cancels, re-admitting that agent to the next start's orphan reconciliation (which finds `result.txt` and re-delivers). Extending the drain to `_ON_DONE_TIMEOUT` instead was rejected: it would hold gateway shutdown for up to 20 minutes on one wedged injection, which is the exact failure the bounded drain exists to prevent. Only reports cancelled **before** `_on_done` returned are re-admitted — `info._reported_to_parent` is set the moment the injection returns, so a cancellation in the later teardown/tombstone waits cannot cause a duplicate delivery on restart.
- **Every reporter goes through the claim — including cancel-recovery failure.** There are more terminal paths than the two obvious ones: when a cancel-recovery respawn cannot happen, its `except` arm also finalizes the agent. That site previously fired `subagent_done` and `_on_done` directly, gated only on `done`/`reaped`, so a reaper racing a failed respawn delivered the outcome twice. It now takes `_claim_finalize` like every other reporter and reports through the shielded helper (which matters because `_force_reap` cancels that very task). `_resume_guarded`'s CancelledError arm writes only the RECORD and deliberately never reports — during shutdown the drain owns delivery.
- **The reaped marker and the recovery cancel precede every `await` in `_force_reap`.** Both used to sit after the session teardown, which yields for up to `_RESET_TIMEOUT` (longer on the SIGKILL path). A recovery task whose bounded handshake expired inside that window observed `reaped == False` and respawned the run being killed — tools executing after a user Stop, strictly worse than a duplicate report.
- **Delivery bookkeeping trails teardown.** Spawning the report ahead of teardown opens a window the older ordering did not have: writing the "delivered" tombstone before the session is torn down would hide a surviving child from orphan reconciliation if the process died in between. The report therefore waits on a `teardown_done` event (set in `_run`'s `finally`, so it fires even under cancellation, and bounded so the report can never wedge) before marking delivery. A reaped or recovery-failed member still settles its **siblings'** digest holds, since those siblings' results did reach the parent even though this member's did not. On the dashboard routes the report's own settle and `mark_delivered` are no-ops by design: `_subagent_done` defers the delivery bookkeeping — the completed member's own tombstone AND any held wave siblings — to the parent's CONSUMPTION of the announce via `_defer_queued_delivery` (the #4839 content-keyed slot ledger + `_delivery_queued`), on the queue branch settled by the drain and on the direct-injection branch by `_arm_queued_delivery_settlement` armed on the injection task (#2233). A bare `_on_done` return is a local routing success, not evidence the parent received anything; an unconfirmed hand-off leaves the debt parked and orphan-recoverable rather than tombstoned.
- **A synthesized reap error names only the cause the observed state supports.** The wall clock fires at the configured deadline, but a run parked on a never-answered spawn approval has reached no execution deadline: `turns == 0`, `_pid is None`, `_exec_started is None`, and the dashboard's approval window is still open. So `_force_reap`'s error synthesis tests `_awaiting_approval and _exec_started is None` **first** and reports the unanswered spawn approval, before the `startup_timeout` and generic-deadline arms. The predicate is captured **above** the intentional cancel, because the flag's owner clears it in a `finally` the cancel schedules; reading it at the record site would hold only while no `await` sits in between.
- `_sigkill_session`: best-effort SIGKILL when graceful reset hangs
- After decrementing `_running_count`, `_force_reap` calls `_drain_queue()` so the freed slot immediately starts a queued spawn. Normal completion pumps the queue via its `finally` block, but that block is gated on `not info.reaped`; a reap sets `reaped=True` and decrements the count itself, so without this explicit drain a queued spawn would sit stranded until an unrelated agent finished or a new spawn arrived.
- Wired up in `gateway.py` after `SubagentManager` init

### Idle-Stall Detection

The main-agent watchdog stack (`tool_stall_suspect_secs`) does **not** govern subagents; `_maybe_flag_stall(agent_id, info, now)` (called from the reaper sweep) is their equivalent. It does, however, consult the *same* liveness oracle (`acp/liveness.py`) — see the attribution note below. Each **session-scoped** stream event calls `_touch_activity(info)`, which updates `info.last_activity`, clears a prior `stalled` flag (re-emitting `subagent_stalled {stalled: false}` when work resumes), retires the agent's oracle and bumps `info._stall_gen`. `info.last_activity` is (re)initialised to `_exec_started` at the top of `_run_inner` so a queue / spawn-approval wait is never counted as idle.

Event kinds are NOT the discriminator: the same `EVENT_SUBAGENT_LIST` also reaches a session through the routed KAS sub-agent lifecycle path (`_handle_kas_subagent`, off a `session/update` frame), where it IS that session's own progress -- excluding by kind would falsely badge a working KAS agent. Provenance is carried instead: `AcpRuntime._reader_loop` sets `JsonRpcMessage.fanout_no_owner` when it fans an ownerless frame out to MORE THAN ONE registered session (a lone session is the sole owner, so it stays unmarked), the dispatch loop copies that onto `AcpEvent.runtime_global`, and `_run_inner` skips the refresh only for a `runtime_global` event. Everything else stays fail-open: an event kind the dispatch switch does not special-case still counts as activity, so a new session-scoped kind can never invent a false stall.

Why the exclusion exists: `_kiro.dev/subagent/list_update` carries no `sessionId`, so the runtime broadcasts it to *every* session queue, and under `agent.session_sharing` one roster notification lands on every co-tenant subagent's stream. Counting it as activity refreshed `last_activity` for a whole batch of wedged subagents at the same instant and cleared their badge, so the badge flapped and the reported `idle_secs` measured time since an unrelated agent's roster churn (`#4841`; the plateau measured in `#2854`).

Per sweep, for an agent that has actually started (`turns > 0` or a live `_pid`) and is **not** blocked on a human approval prompt (`_awaiting_approval`):
- `idle > _stall_idle_secs` and not already flagged → consult liveness (below), and unless the verdict clears it, set `info.stalled = True`, emit `subagent_stalled {stalled: true, idle_secs}` (surface-only; the card shows a "no activity" warning), and append a record of the slow command to `~/.kiro/crew/subagents/slow_commands.jsonl` for later analysis (rotated at 1 MiB keeping one previous generation, `.jsonl.1`, so total disk stays bounded at ~2 MiB; a reader wanting full available history must consume both generations).
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

`_awaiting_approval` is set around **both** human approval awaits — the mid-run tool approval in the `EVENT_PERMISSION_REQUEST` branch (reset in `finally`, which also refreshes `last_activity`) and the pre-execution spawn gate in `_spawn_with_approval` (also reset in `finally`) — so a slow approval never looks stalled. The two are told apart by `_exec_started`: it is set for the mid-run prompt and `None` at the spawn gate, which is what lets the reaper name the right cause (see Reaper Loop).

### Running-card progress events

`subagent_tool` is fired on **`EVENT_TOOL_CALL`** (not only `EVENT_PERMISSION_REQUEST`) — kiro-auto-allowed tools surface only as informational `tool_call` updates, so this is the sole progress signal a simple/read-only task emits. Payload carries `{tool, tool_kind, turns, tool_count}`; `info.tool_count` increments per observed tool call. The `subagent_snapshot` reconnect payload (`dashboard/ws.py`, built by `build_subagent_snapshot()`) also carries `tool_count`, `stalled`, and — only while stalled — `idle_secs`, recomputed at replay time from `last_activity` (clamped at 0, omitted entirely for a healthy agent) so a reloading client recovers progress/stall state including the span that justifies the stall badge (a transition-only WS signal always needs a matching snapshot field).


### Model Provenance (#3582)

Every subagent card names the model the run actually ran on, so a model-pinned
review's real model is auditable. `SubagentInfo` carries two fields: `requested_model`
— the EFFECTIVE pin, i.e. the per-spawn `model` OR, when empty, the
`agent.role_models['subagent']` config pin ([model-selection](../common/model-selection.md) is the documented way to pin a
subagent model), resolved once at spawn; `"auto"` when completely unpinned (no
per-spawn model, no role pin) — and `resolved_model`, the id the live
session actually served, read via the provider's public `served_model` accessor
(`_resolved_model_of`, which normalizes the `DEFAULT_MODEL` "auto" sentinel to `""`
= unknown). `resolved_model` is captured at spawn (ACP reports it immediately) and
refreshed on the first text chunk (covers the CC path); a known value is never
clobbered back to `""`.

The resolved id rides the wire as a `model` field on the `subagent_spawn`,
`subagent_done`, and reconnect `subagent_snapshot` payloads, and the requested pin
rides alongside it as a `requested_model` field on those same payloads (both are
`_redact()`-ed, since the pin is caller-supplied). The single-completion
meta (`subagent_completion_meta.single_completion_meta`, mirrored by
`website/src/pages/chat/subagentCompletion.ts`) additionally carries `requestedModel`
and `resolvedModel`. The frontend renders the resolved model as a chip beside the
agent pill and flags a **downgrade** — an amber chip plus a persistent
`role="status"` "Requested X, served Y" banner — when the two name different models,
on BOTH the completion card AND the **live** Subagents-panel row (`ActivityViewer`),
so a mis-pinned run is visible mid-flight, not only at completion. Because the pin
rides `subagent_done` (and its reconnect replay), a downgraded run that completes
before a reconnect rehydrates its card with the amber flag intact. For unpinned
spawns `requested_model="auto"` records the sentinel so the frontend shows a neutral
chip rather than hiding the column. "Same model" is decided by the shared
`normalizeModelKey`
(`website/src/lib/model.ts`, mirroring the backend `_normalize_model_key`): dotted vs
dashed spellings and case fold, and `auto`/`default` fold to "no pin", so an honored
pin whose wire spelling differs does not false-flag. Wave-digest completions
(`wave_chunk_meta`/`wave_final_meta`) carry no structured model field, but each
member's **served** model is surfaced inline in the digest body
(`ok_lines`/`fail_lines`) that both the parent LLM and the card already read —
`— \`id\` ✅ task · model <served>` — so batch members are auditable for which
model actually ran. Only the served id is shown (no requested/downgrade
qualifier): a raw requested-vs-resolved inequality is not the card's downgrade
fold and would false-amber every member of a normal `auto`-pinned wave, so the
amber-downgrade signal stays a single-completion concern until this shares the
card's fold (or #5339's registry fold). The value is redacted through the
display context before it enters the broadcast digest text.
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

## Durable task queue (`kiro_crew.taskq`)

Specified in [taskq.md](taskq.md); this section is the manager's side of it.

- **Store.** `SubagentManager.__init__` opens `$KIROCREW_HOME/tasks/tasks.db`
  through `taskq.open_default_store` (schema → legacy import → reconcile) when
  `agent.task_queue_enabled` is true. With a running event loop, the constructor
  schedules `_initialize_taskq`: `asyncio.to_thread(_open_taskq)` owns config,
  SQLite open, integrity checking, migrations, import, reconcile and wait rebuild.
  It attaches the result on the loop only after that work finishes. Until then,
  spawns return `task_store_unavailable`; `wait_taskq_ready` lets startup await
  attachment without cancelling the worker, and is also the point at which the
  gateway binds the runner admission to the now-existing dependency coordinator
  and runs its adoption sweep ([taskq.md](taskq.md) § Runner adapters).
  `dependency_coordinator_async()` is the loop-side accessor that builds the
  coordinator on the store's writer thread, because the first build rebuilds the
  wait schedule from every waiting row; the run loop's own dependency arms
  (`_run_inner`'s coordinator read and `_yield_for_dependency`) take it, and the
  gateway calls it as `_ensure_subagent_coordinator()` before each loop-side
  wiring pass so the sync `dependency_coordinator()` those passes read is a
  hit on the built one. Synchronous constructors without a
  running loop open inline, as does a loop caller under
  `SpawnAdmissionCoordinator.open_store_off_loop=False` (the test suite's root
  fixture, next to `pump_off_loop`; `test_taskq_startup.py` turns it back on to
  pin the worker path). A store that cannot be opened while the
  queue is ENABLED is recorded as `_taskq_unavailable` and every `spawn` /
  `spawn_async` is refused typed (`error_code=task_store_unavailable`, the
  cause in `error`) -- accepted work must never live only in memory. That
  refusal is fail-closed but NOT until the next restart: `_taskq_init_task` is
  re-armed from the reaper sweep (`taskq_reopen_if_due`) on the shared recovery
  schedule until the open succeeds, with `_taskq_reopen_attempts` /
  `_taskq_reopen_at` as its state and `taskq_arm_reopen` -- called by whoever
  recorded the failure, off the loop -- as the one place the deadline is set. The
  retry deliberately does NOT live inside `_initialize_taskq`: the gateway boot
  path awaits `wait_taskq_ready`, so a loop there would hang boot instead of
  recovering. `_initialize_taskq` also never un-attaches: a re-open that comes
  back empty leaves an attached store alone. Why re-opening mid-life is the boot
  open repeated: [taskq.md](taskq.md) § An enabled queue never falls back.
  `agent.task_queue_enabled=false` is the one way to run on the in-memory queue
  alone; `_taskq is None and _taskq_unavailable is None` is the test for "legacy".
- **`spawn_async` (event-loop callers, `POST /api/spawn`).** `prepare_spawn`
  runs every policy gate and returns a `PreparedSpawn`; the row is written on
  the store's writer thread (`TaskStore.run`); then `spawn(**params,
  _preassigned_id=id, _store_accepted=True)` starts the run. The SQLite lock
  wait never blocks the loop and the caller is acked only once the row exists.
  **Batch accounting happens exactly once, on the FIRST entry** (`not
  _from_queue and not _store_accepted`): the prepare pass counts the member,
  so a member `prepare_spawn` refuses is counted like any other refusal and
  `/api/spawn`'s `counted: true` is true for it; the `_store_accepted`
  re-entry and a drained row never count again. **The mutable policy gates
  (memory identity, cwd allowlist, governance) run once per submission**: on
  the first entry and again when the pump drains a stored row (the re-check
  before dispatch). The `_store_accepted` re-entry skips them -- a refusal
  AFTER the row was committed would leave executable work queued behind a
  refusal the caller already saw. A drained row the pump refuses is marked
  `failed` in the store in the same step (`_refuse_row` -> `taskq_fail`), so
  the caller's verdict and the store's agree. Capacity is never a refusal for a
  committed row: a prevalidated app spawn (`_agent_prevalidated`, the SpawnSDK)
  that finds no slot queues like any other row, with the flag CLEARED in its
  queue entry so the drain re-validates the agent and re-proves app ownership
  (`_validate_app_agent_ownership`, the SpawnSDK's filename-prefix test) before
  starting; an entry that fails that re-check is refused and its row failed.
  The durable row never carries the flag at all (`taskq_build_record` strips
  it; `_window_entry` drops it from any row that still holds one), so a start
  rebuilt from the store -- window refill or restart -- runs the same gates
  (`test_overload_integration_glue.py::test_durable_row_never_carries_prevalidation`).
  `approval_mode` is the OTHER process-local param and is stripped by both, for
  the same reason at higher stakes: an ad-hoc `approval_mode="auto"` skips the
  spawn gate AND pre-approves the run's tools, so a row carrying it replays one
  request's consent into a start nobody authorised. A row is written by one build
  and started by another, which is why the READ side strips it too rather than
  trusting the row (`test_taskq_admission_integration.py::test_a_row_on_disk_carrying_auto_approval_faces_the_spawn_gate`
  drains a row that still holds the grant and asserts the approval callback ran).
  The value stays recorded in `scope_ref`, which the schema defines as references
  rather than grants and which no start path reads.
- **Wakes.** Only `resume_grant` writes `running` (`wake_wait(to=running)`,
  slot reserved, runtime resident) — and the landed wake is the PRECONDITION
  for the publish: the pump reserves the lane slot on the loop
  (`resume_reserve`), the wake runs on the writer thread
  (`resume_grant_async`), and `_slot_released` / `_wait_record` /
  `_taskq_generation` / `_resume_event` are written only from its result. A
  refused or unreachable wake releases the reservation and leaves the run parked
  on its bounded `_await_lane_resume` timeout, so the no-overshoot invariant
  above is unchanged and a grant is never reported for a slot the store does not
  back. The publish re-tests the run's LIVENESS across that split for the same
  reason it re-tests the row: a reservation is not a grant, `resume_reserve`'s
  own `done` / `reaped` / `user_stopped` gate ran on the near side of an await,
  and the pump reserves EVERY queued resume in one synchronous pass before
  granting them one at a time — so a run whose entry sits behind another's can
  end while its own slot is already reserved. `yield_slot` spent that run's
  one-shot release token, so publishing `_slot_released = False` onto it charges
  a lane slot no terminal path can hand back and the cap falls by one per such
  resume. `_resume_publish` gives the reservation back instead, never with a
  re-arm, and adopts a wake that already LANDED even then, so the run's own
  terminal write is fenced by the generation the row now carries rather than by
  the stale one. The row-state refusal beside it reads the same way: `retry=False`
  says the row is not this run's to resume any more, NOT that the row left a wait.
  A row still IN a wait passes `retry=False` too, because the only wait state that
  can reach the refusal is one a re-dispatch entered under a newer generation, and
  re-arming would ask the pump for a lane slot on another owner's row once a second
  until the waiter's own bound gave up. `resume_reserve` reserves nothing while gateway admission is CLOSED (the
  updater's boundary, [session.md](session.md) § Update pause): the run stays
  parked with its wait intact and the next wake asks again, because a slot taken
  behind the census the updater just read is work a restart would interrupt
  mid-turn. The window refill answers the same gate — a row hydrated into
  `_queue` after the close would only be refused by the spawn gate, and left on
  disk it is the next boot's work. A live parent whose last child ended stays
  `waiting_children` (`on_child_terminal(defer_wake=True)`) until the pump
  grants its slot; every other wake lands in claimable `retry_wait` (see
  `taskq.md` § Waits). `taskq_excluded_ids` keeps the refill from claiming a
  row whose run is live in this process.
- **`_queue` is a bounded window**, never the whole backlog. It holds at most
  `agent.task_dispatch_window` (64) dicts — the same dict shape as before,
  `_preassigned_id` included, plus `_lane` on entries the refill brought in
  (popped before `spawn(**params)`) — and `_drain_queue_impl` refills it
  lane-fairly from the store at the start of every pump and after every pop
  (`taskq_refill_window`; see § Fairness lanes and the child reserve). A new
  spawn joins the window directly only when there is room AND no older row
  waits outside it (`taskq_should_window`), so dispatch is FIFO inside a lane
  across the boundary. `queued_count_for` /
  `has_pending_work_for` / `batch_members_pending` / the stuck-wave and
  digest-hold sweeps /
  `cancel_for_parent` all add the store-only rows (`taskq_overflow`,
  `taskq_batch_pending`, `taskq_pending_ids_for`). An event-loop caller takes the
  `*_async` sibling instead — `queued_count_for_async` /
  `has_pending_work_for_async` over `taskq_overflow_async`,
  `taskq_pending_ids_for_async` for `cancel_for_parent`, and
  `batch_members_pending_async` / `_sweep_stuck_waves_async` /
  `_sweep_digest_holds_async` over `taskq_batch_pending_async` (the reaper and
  the gateway's completion consumer are the coroutines that hold them) — each of
  which snapshots the
  in-memory exclusion set on the loop and runs only the `count_pending` /
  `list_pending` / `fetch_pending_by_batch` half on the store's writer thread.
  Each wave helper's own split is the same: the candidates come from manager
  state on the loop (`_batch_pending_in_memory`, `_stuck_wave_candidates`,
  `_expired_digest_holds`) and only the per-wave store read is offloaded. The
  sync entries stay for the sync callers.
- **A store-only count answers "some" while the store cannot be read, never
  "none".** No store at all is a queue with no rows in it; a locked, busy or full
  one is a queue whose rows nobody can see, and only the first is evidence that
  nothing is waiting. So `taskq_overflow` / `taskq_overflow_async` answer
  `taskq_bridge.UNKNOWN_PENDING` (1) on a `TaskStoreUnavailable`, and
  `taskq_batch_pending` / `taskq_batch_pending_async` answer True. Every consumer
  is a fail-closed PREDICATE — the attached-children guard before a session
  teardown (`chat_utils.subagents_attached`, whose own `except` arm therefore
  covers a probe that raises for some other reason rather than this one), the cron
  reset-deferral guards (`has_pending_work_for`), Slack's pending probe, and the
  wave digest, whose bookkeeping is PRUNED when it closes so an early close is not
  one wrong message but a SECOND digest for the same batch. Two readers take
  `taskq_batch_pending` with OPPOSITE polarity and True is the safer failure for
  both: `_sweep_stuck_waves` skips its reconcile (the next sweep re-examines the
  wave), and `_sweep_digest_holds` — reached only for a hold already past
  `DIGEST_HOLD_SECS` — forces the partial digest out, so the price is a chunk that
  may race the wave-close flush rather than every finished sibling's result staying
  undelivered for the length of the outage. The queue-depth chip is the only reader
  for which the value is a number, and one advisory unit during an outage the log
  already names is the price. `taskq_pending_ids_for` is the
  exception and stays `[]`: it enumerates ids to cancel, and inventing one would
  cancel a row nobody read. Pinned by
  `test_taskq_admission_integration.py::test_an_unreadable_overflow_keeps_the_attached_children_guard_closed`
  and `::test_an_unreadable_batch_read_holds_the_wave_open`.
- **Claim before registration.** `taskq_claim(agent_id)` runs right before the
  info is registered; it returns `(generation, proceed)`. `proceed=False`
  means the store knows the row and refuses it (cancelled while waiting): spawn
  returns a `queued`+`done`+`user_stopped` info without registering and the
  drain takes the next row. A row the store never saw (a legacy in-memory
  entry, as tests inject) proceeds with generation 0. The generation is stored
  on `SubagentInfo._taskq_generation`.
- **State marks.** `starting` when the run task (or the approval prompt) is
  created; `running` at `_exec_started` in `_run_inner`; terminal via
  `_claim_finalize` → `taskq_settle` (`user_stopped → cancelled`, `error →
  failed`, else `done`, `result_ref` = the run's `result.txt`). Because the
  one-shot report token is the writer, exactly the reporter of the outcome
  writes it, and a stale generation (a superseded dispatch of the same id) is
  fenced out by the store.
- **The settle's PROPAGATION is owed by the reporter, not by the row's write.**
  Both settle paths have the SAME three arms — committed, `TaskStoreUnavailable`,
  unexpected exception — and the propagation runs on all of them, because the
  one-shot `_claim_finalize` token means a propagation this settle skips is one
  nothing anywhere retries: a waiting parent is left parked on a wait no wake
  will ever end, and a cancelled parent's children keep running unnoticed. The
  unexpected-exception arm costs more than the propagation, which is why neither
  path may let one leave: the token is already spent when `taskq_settle` runs, so
  an escape loses the TERMINAL REPORT too — the claimer never sees True, reports
  nothing, and no second claimer can. Both halves decide from the child ID they
  are handed rather than from the child's row (`on_child_terminal` adds it to the
  terminal set itself), and both absorb a store outage of their own.
- **The one arm that does NOT propagate is the one where the reporter is not the
  owner**: `taskq_superseded_by_live_owner` — the terminal write was refused AND
  the row now carries a newer generation AND is not terminal. A live replacement
  holds a `_claim_finalize` token of its own, and here the propagation is not
  merely redundant — because `on_child_terminal` takes the report as the
  evidence, it marks the child terminal and wakes a parent awaiting it as its
  last child WHILE the replacement is still running. The predicate is the row's
  LIVENESS, never "my generation is stale", because the other two refusals still
  owe the propagation: a transition the table refused from a PARKED row
  (`retry_wait -> done` is closed) leaves the row at this run's own generation,
  so nobody else will ever report it; and a fenced row that is already TERMINAL
  was settled by another owner — the shape of an operator cancel through
  `/api/tasks`, whose `store.cancel` bumps the generation — where re-running the
  propagation decides the same thing (`on_child_terminal` rebuilds the terminal
  set from the store) while skipping it parks the parent until the next boot's
  `WaitLedger.rebuild`. One row read, shared by both paths and reached through
  `store.run` on the posted one; an unreadable store answers "not superseded",
  since an outage leaves this generation on the row. Pinned on both settle paths
  by `test_taskq_admission_integration.py::test_a_terminal_write_the_store_refused_still_resumes_the_parent`
  and `::test_a_fenced_settle_propagates_unless_a_live_owner_holds_the_row`.
- **Cancel.** `_unqueue_impl` cancels the store row FIRST (`taskq_cancel_queued`,
  which also returns the params for a store-only row so the queued-stop report
  is whole), then drops the window entry. `cancelled` beats the drain in either
  order (see taskq.md § Atomic claim). It matches UNSTARTED entries only: a
  `_resume_id` entry carries a resident run's own id, so an id match cannot tell
  the two apart, and both callers would then convert a live or already-reported
  run into a synthetic queued terminal — `cancel`'s fall-through for a `done` or
  absent record (a resume entry outlives its run's terminal; nothing on that path
  withdraws one) and the parent sweep. Pinned by
  `test_overload_integration_glue.py::test_cancel_never_reports_a_queued_stop_over_a_run_that_already_ended`.
- **Restart.** `start_reaper` calls `taskq_boot_dispatch`, which arms one pump
  wake-up when persisted rows are pending. Rows in `queued` re-dispatch under
  their original ids and params; rows a dead incarnation had `starting`/`running`
  are settled by reconcile — a subagent's default side-effect class is
  `unknown`, so such a run ends `unknown_side_effect` (never silently re-run)
  unless its tombstone proves an outcome; the existing orphan reconciliation
  still delivers the notification.
- **Memory pressure defers.** See admission order step 3. SEL outcomes:
  `deferred_low_memory` / `deferred_memory_critical` (store) vs the legacy
  `refused_low_memory` / `refused_memory_critical`.
- **Nested tree.** `taskq_accept` sets `parent_id` (and inherits `root_id`)
  when the spawning session is `subagent:<id>` and that id has a row, so the
  store holds the S → A → B links a restart rebuilds from.
- **Waits (taskq.md § Waits).** `admission.yield_slot(info, WaitRecord)`
  releases the lane slot (`_release_slot` token, `_running_count -= 1`, pump)
  and writes the record (`running → waiting_*`, same generation); the runtime is
  untouched and its residency stays charged. `request_resume(info)` puts a
  `_resume_id` entry at the FRONT of the window; `_drain_queue_impl` grants it
  by capacity through `resume_grant` (`_running_count += 1`, store `wake` under
  a NEW generation adopted into `info._taskq_generation`). `resume_granted(id)`
  tells a caller whether the run holds its slot again. Events:
  `subagent_waiting`, `subagent_resumed`.
- **A re-armed resume belongs to the WAITER that asked.** A grant the pump
  refused is asked for again on a timer (`_rearm_resume`, `_RESUME_REARM_SECS`),
  and the timer checks ownership when it FIRES: an `info._resume_event` that is
  no longer the one it was armed under means the waiter is gone and it asks for
  nothing. `request_resume`'s own three gates do not cover that — a bounded
  `_await_lane_resume` that gave up is neither `done` nor holding its slot nor
  already queued — and it withdraws its QUEUE entry, which a `TimerHandle` is
  not in. Granted after the give-up, the lane slot is charged to a run whose
  `_release_slot` token `yield_slot` already spent, so nothing gives it back and
  the fresh entry re-arms in turn. A request made with no event of its own (the
  one-shot children wake, whose holder is a `/api/spawn/{id}/resume` long poll)
  keeps retrying. Pinned by
  `test_runloop_integration.py::test_a_rearm_outliving_its_bounded_waiter_grants_nothing`.
- **`waiting_children` (W3) is detected from the execution layer.** When a
  child registers or queues under `subagent:<parent>`, `taskq_child_registered`
  reads the parent's trusted in-flight tool snapshot (`_inflight_tool.tool_name`
  from `_meta.kiro`): a name ending in `spawn_sub_agents` means the parent is
  blocked on its children and has no runnable work of its own, so it yields
  with `WaitRecord.children(outstanding ids, tool_call_id)`; later children of
  the same call join the awaited set (`update_wait`). A parent that used
  `spawn_run` keeps running and keeps its slot. `taskq_settle` then calls
  `taskq_child_terminal`: the parent wakes on its LAST awaited child (each
  parent re-admitted individually, no tree-wide burst); `on_child_failure`
  (params, default `continue`) `fail_parent` fails the parent now
  (`reason=child_failed`) and cancels the remaining children, children first.
  A CANCELLED parent cascades to its children (`taskq_cancel_children_of`:
  live runs through `cancel`, store-only rows through `cancel_tree`); a `done`
  or `failed` parent leaves them running. `taskq_expire_waits` (once per second
  on the pump path) fails waits past `deadline_at` and cancels their live runs.
  `spawn_sub_agents`' own blocking wait is this W3 record; its `still_running`
  return (wait expiry) stays as specified below and cancels nothing.

## Fairness lanes and the child reserve (RFC §6, §13 Q3 / Q5)

The dispatcher's order is not global FIFO. The store side is in
[taskq.md](taskq.md) § Fairness lanes; this is the manager's side
(`subagent_manager/admission/fairness.py`).

- **Lane of a spawn.** `lane_for_session(parent_session_key)`: a
  `subagent:<id>` key walks the live parent chain (`_agents`, then the store
  row) to the root session; any other key is a root and maps by
  `lanes.lane_key_for` (cron / hook / heartbeat / background / empty →
  `system`). Entries the refill brought in carry the row's `lane` as `_lane`.
- **Pick order (`pick_window_index`).** A queued resume (`_resume_id`, FIFO
  among resumes) first — its run is already resident — then `LaneScheduler`
  weighted round-robin over the lanes with eligible window entries, oldest
  head winning a tie. `_drain_queue_impl` pops that index, not index 0. With
  only the child reserve left, "eligible" means nested (`entry_is_child`:
  `parent_session_key` starts with `subagent:`); when no window entry
  qualifies the window is topped up with `children_only` rows and the pick
  runs once more.
- **Refill (`taskq_refill_window`).** Every lane with a store row waiting gets
  its head into the window (a window full of one lane evicts that lane's
  YOUNGEST entries back to store-only — they are queued rows, refetched later
  in FIFO order — never a resume entry), then the remaining room is filled by
  `fetch_dispatchable_fair`. Two scheduler
  balances (`lane_scheduler()` for the pick, `lane_refill_scheduler()` for
  the refill) so the refill never spends the pick's credit.
- **The eviction has a floor, and it is not "keep every head"
  (`_evict_for_lanes`).** A lane keeps its head for as long as ANY lane holds a
  spare entry. A full window holding exactly one entry per lane has no spare, and
  there keeping every head frees nothing — so the `children_only` top-up hydrates
  no row, the slot the child reserve is holding open can never be filled from
  disk, and a tree waits on its own child for as long as the process lives, which
  is the deadlock the reserve exists to prevent. The youngest head goes instead,
  at most ONE per call, so the window churns by a single entry and that entry is a
  queued row the refill refetches in FIFO order. Pinned end to end by
  `test_fairness_lanes.py::test_reserve_pulls_a_child_into_a_window_holding_one_entry_per_lane`
  and directly by `::test_eviction_frees_one_lane_head_per_call_and_never_a_resume`.
- **`CapacityView` (`capacity_view()`).** One reading per decision:
  `cap_total` = `_max_concurrent`, lifted to `min(user_max_concurrent,
  adaptive_floor + child_reserve)` ONLY while a parent is in
  `waiting_children` AND `_adaptive_cap` is set (a cap the user or a test
  pinned is never lifted); `roots_cap` = what a depth-0 start may fill:
  `cap_total - child_reserve` while the reserve is active (a nested row or a
  resume is waiting for a slot), else the whole cap, and never more than the
  unlifted cap. `any_slot` gates children and resumes; `root_slot` gates
  roots. `_should_stagger_queue_impl` reads `any_slot`; `spawn_impl`
  additionally queues a root that `root_may_start()` refuses. A parent that
  merely waits while its children RUN reserves nothing — unrelated sessions
  fill the cap (RFC §14.3). `agent.child_reserve=0` disables the rule.
- **Why the reserve.** A parent that yields holds no slot, so the rule bites
  when starts compete: with cap 2, S → A → B and roots R1, R2 queued, S yields
  for A, A takes the reserve while R2 waits; A yields for B, B takes it; B
  ends → A's resume, then S's, before R2. Roots fill the cap again once
  nothing nested is pending. Under an adaptive squeeze to 1 with a parent
  waiting, the child gets a second slot the roots never see, so the tree
  progresses instead of waiting behind a root's whole run.
- **No checkpoint-pause (RFC Q3).** A dedicated-runtime parent that waits on
  its children keeps its resident process and its host charge; nothing closes
  or resumes that runtime, and no flag or seam for it ships.
- **Resume hold (`wait_resume_granted`, `GET /api/spawn/{id}/resume`).**
  Between the store `wake` (last child ended) and the pump's grant the parent
  must not be handed its tool result. `wait_resume_granted(id, timeout)`
  awaits the per-run `_resume_event` that `resume_grant` sets (one event per
  wait; retired on the grant); True at once for a run holding its slot or
  unknown here. `dashboard/handlers/spawn_resume.py` serves
  `GET /api/spawn/{agent_id}/resume?wait_secs=N` (held server-side up to
  `MAX_HOLD_SECS` = 8 s, under the MCP client's 10 s GET timeout) →
  `{known, granted, slot_released, resume_pending, done}`, and
  `GET /api/spawn/lanes` → `admission.lane_snapshot_async()`.
- **Settings.** `FairnessSettings.from_agent_config(cfg.agent)` (cached on
  the manager for `FAIRNESS_SETTINGS_TTL_SECS` = 2 s; `set_fairness_settings`
  pins a value for live reload and tests): `lane_weights`, `child_reserve`,
  `adaptive_floor`.

## Scale Plumbing (60-100 concurrent agents)

Large waves must not flood the WS socket, the parent LLM's context, or the UI. Five mechanisms — WS coalescing/replay-batching and UI caps are inert below their thresholds; digest chunking applies uniformly to every multi-task wave (single-task spawns behave byte-identical to legacy):

- **Batch identity**: `spawn(batch_id=..., batch_total=...)` (threaded from `spawn_run tasks=[...]` — one 12-hex id per multi-task call — via `POST /api/spawn` transport params; survives the stagger queue). `spawn_batch_started {batch_id, count}` fires once per batch on its first started member; the id rides every WS frame (`base["batch_id"]`).
- **Event coalescing** (`subagent_scale.SubagentEventCoalescer`, wired in the gateway's `_subagent_event`): above 8 active agents, `subagent_tool`/`subagent_stalled`/`subagent_retrying` buffer per-agent (latest state wins, merged) and flush every ~1s as ONE `subagent_batch_update {updates:[...]}` frame to all clients; `subagent_chunk` text buffers append-concatenated (16KB/agent cap) and flushes as `subagent_batch_chunks {chunks:[...]}` to subagent subscribers only. Lifecycle events (`spawn`/`done`/`recovering`/`injection_failed`/`batch_*`) are NEVER coalesced, and a `done`/`spawn` flushes buffered state first so ordering is preserved. Non-int active-count fails open to pass-through.
- **Chunked wave-digest completion injection** (gateway `_subagent_done`): every batch member is accounted per `batch_id` (this is the single completion consumer for all terminal paths). Every multi-task wave (`batch_total > 1`) delivers results to the parent queue-style: completed members are HELD, and every `SUBAGENT_DIGEST_CHUNK_SIZE` completions (default 10, env `KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE`, clamped 1..1000) flush ONE `[Subagent batch completion event]` chunk digest — failures first with detail, successes as one-line `result_path` pointers (60KB cap per chunk); the final member flushes the remaining partial chunk. A 60-agent wave = 6 digest turns spread across the wave's runtime — bounded chunk size, incremental signal, and no straggler-gated mega-digest. Chunk buffers (`fail_lines`/`ok_lines`/`guard_msgs`/`held_ok_ids`) reset per flush; cumulative `ok`/`err`/`stopped` counts ride the final chunk's summary. **Spawn discipline**: non-final chunks instruct the parent NOT to spawn new sub-agents while batches are still arriving; the final chunk releases the gate ("finish processing all results before spawning follow-ups") — mirrored by a line in the `spawn_run` tool description. **Chunk order is FIFO**: the injection busy-check (`_injection_slot_busy`) treats a live `slot.task` — the claim assigned synchronously at dispatch — as busy in addition to `slot.running`, so a later chunk waits behind an injection that is dispatched but still inside `bounded_chat_turn`'s off-loop timeout resolution, instead of racing ahead of it or assigning `slot.task` over the earlier chunk's still-pending task. Single-task spawns have no batch identity and keep the plain per-agent injection. A batch member rejected at spawn (empty task, low memory, cwd, governance, bad agent) is counted as submitted AND announced through the done callback with its batch identity (`_announce_rejection`) — so a rejection that closes the wave still reaches the consumer and releases held sibling results (non-batch rejections do not announce; the caller gets the error synchronously). `batch_finished {batch_id, total, ok, err, stopped}` broadcasts for every batch regardless of size. **Wave liveness (lost-submission backstop)**: a member rejected before reaching `spawn()` or lost during transport is counted in every sibling's `batch_total` but never in `submitted` — un-reconciled, the count-driven `batch_members_pending()` wedges the wave forever. Three layers close it: (1) `api_spawn` marks in-process rejections/capacity with `counted: true` (preserved through the MCP client's error flattening); (2) `spawn_run` best-effort POSTs `/api/spawn/lost` for each explicit UNcounted rejection, which calls `record_lost_submission` — counts the member as submitted and announces a synthetic terminal failure through the completion consumer so the wave closes; uncertain transport failures are not immediately reconciled because the gateway may have accepted them; (3) the reaper's `_sweep_stuck_waves` (every sweep) force-reconciles uncertain or lost submissions when `submitted < expected`, all registered members are terminal, nothing is queued, and no submission progress occurred for `_WAVE_STUCK_SECS` (1800s / 30 minutes — deliberately generous; it is a lost-submission backstop, not an execution deadline, and it only fires once every registered member is already terminal, so it never cuts a live member) — one lost member per sweep, converging across sweeps; this also bounds the `_batch_submitted`/`_batch_progress_ts` leak. Straggler-held partial chunks are bounded by the **hold deadline** (below), not by the member's hard ceiling.

- **Digest hold deadline (straggler escape hatch)**: both chunk triggers are event-driven — a COUNT trigger (`SUBAGENT_DIGEST_CHUNK_SIZE` pending completions) and wave close — so neither can fire while a straggler is simply *not finishing*. With the default count (10) above any wave size the concurrency cap realistically produces (2–5), the count trigger is unreachable and wave close becomes the ONLY flush: every sibling's finished result is withheld for the slowest member's entire remaining runtime, and a member that HANGS rather than fails withholds them for the full `_TIMEOUT_SECS` reap — up to 3 hours of total silence, indistinguishable from a dead session (issue #2215). The reaper's `_sweep_digest_holds` supplies the LATENCY trigger the count lacks: when the OLDEST outstanding hold in a live wave ages past `DIGEST_HOLD_SECS` (default 120s, env `KIROCREW_SUBAGENT_DIGEST_HOLD_SECS`, clamped to `_TIMEOUT_SECS`; `0` opts back out to count-trigger-only), `force_digest_flush` announces a synthetic **flush-only** record through the single completion consumer — the same re-entry mechanism `record_lost_submission` uses, so digest composition, routing, and the held-tombstone settle contract stay in one place. The record carries the wave's `batch_id` but is NOT a member: `_digest_flush_only` makes the gateway skip every per-member side effect (terminal WS event, orchestration tracker accounting, `done`/`ok`/`err` counters, digest lines) and only force the pending chunk out. **One knob, two jobs, now split**: the count keeps bounding digest SIZE for large waves; the deadline caps worst-case delivery LATENCY at every wave size. A wave whose members all finish within the deadline of each other still delivers ONE consolidated digest, so the deliberate small-wave behavior is unchanged. The forced chunk is labelled honestly as a PARTIAL release (`k/k+1`, "N of M delivered, R still running") and tells the parent to synthesize what it has rather than keep waiting. Hold bookkeeping: the gateway stamps `_digest_held_at` when it holds a member and clears it when that member's chunk fires — deliberately separate from `_digest_held`, which is the restart-safety flag the run loop reads and which the sweep must never mutate. The sweep is skipped entirely when `batch_members_pending()` is False, so it can never race the real wave-close digest into a duplicate delivery.
- **Reconnect replay batching** (`ws.py`): more than `SUBAGENT_REPLAY_BATCH_THRESHOLD` (8) replay frames collapse into ONE `subagent_snapshot_batch {items:[{type, data}]}` frame; the client fans items into the per-frame reducers.
- **Stall two-sweep confirmation** (`_maybe_flag_stall`): the first reaper sweep past `_stall_idle_secs` only marks `_stall_suspect_at`; the second consecutive idle sweep flags `stalled` (event + slow-command record). Any stream activity that BELONGS to the session (`_touch_activity`) resets the suspicion; a `runtime_global` frame fanned out to co-tenants does not. Adds ≤1 sweep interval (~60s) latency; prevents alarm fatigue from healthy-slow agents ambering at scale.

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
- `reasoning_effort` (str, optional): per-call reasoning-effort override (`low`/`medium`/`high`/`xhigh`/`max`), batch-wide like `model`. Precedence: per-call value → `agent.role_efforts['subagent']` pin → provider default; `""`/absent changes nothing. Like a model/effort role pin, a non-empty value forces the dedicated-process path (the parent's shared runtime cannot switch effort per session), so a wide fan-out pays a full process per subagent — and that cost is paid even when the resolved model turns out not to support effort (the level is then dropped at the provider factory). Carried through the stagger queue and the retry endpoint like the context-group flags. NOT inherited by `spawn_continue` — a continuation resolves effort fresh (role pin, else default), the same parity as `model`. When the requested effort cannot take effect, the gateway says so: `/api/spawn` resolves the model the factory's effort gate will see (per-call value, else the subagent role pin, else the selected member's own model pin, the provider template's pin, and the global fallback) and returns an `effort_dropped` reason on the success response, which the tool renders as one attributed line per distinct verdict — subagents sharing an identical verdict (the usual case, since the value is batch-wide) are collapsed into a single line naming all of them, while differing verdicts keep their own attributed lines — including the default case where nothing is pinned and the model resolves to "auto". When the effort WILL apply, the response instead carries an `effort_applied` note naming the resolved model and the family-specific settings key (`reasoning` for GPT, `output_config` for Claude) it is delivered under, rendered the same way — so both outcomes of a requested effort are visible in the tool result. A role-pinned effort that will be dropped (no per-call effort involved) still surfaces in the gateway log at warning level, since the tool caller never asked for it — that warning is emitted by the provider factory's effort gate itself (`config/loader.py`), the single authority that drops the level, so one log line covers every surface that funnels through it (spawn, dashboard slot, cron) and cannot drift from the decision it reports on. The provider factory remains the single dropping authority; the report never rejects or alters a spawn. Per-TASK variation inside one call is deliberately not supported (see issue #2140).
- `include_memory` / `include_lessons` / `include_project` (bool, optional, default `true`): which switchable context groups the subagent inherits, applied to every task in a batch spawn. All-on is byte-identical to the injection a normal session gets, so a caller that omits them changes nothing. `include_memory=false` drops preferences, projects, daily history, semantic and episodic memory, and prior-session provenance — the normal choice for fan-out whose task text is self-contained. `include_lessons=false` additionally drops the user's learned corrections and profile, so keep it on for any subagent that writes code, edits files, or runs git. `include_project=false` drops the docs pointer and the project-directory line. It also drops the injected steering block, but ONLY on the Claude Code backend: on the ACP/kiro backend `kiro-cli --agent` loads the agent's `resources` (including steering globs) itself, which Kiro Crew cannot suppress from here, so steering still reaches an ACP sub-agent regardless of this flag. The conduct group — critical output-format rules, date, agent identity, runtime, workspace identity, and the skills index — is never switchable, because a subagent without it cannot discover its own capabilities or format what it reports back. A subagent is told by name which groups were withheld (`[CONTEXT SCOPE]`) so it reports the gap rather than guessing. Resolved once at spawn, carried through the capacity-queue round-trip and `POST /api/spawn/{id}/retry` like `approval_mode`/`silent`/`keep`. `spawn_continue` does not take the flags but does **inherit** them from the run it continues: a continuation rebuilds session context (`get_or_create` reports `is_new=True` even when it restores the session via `session/load`), so without inheritance a scoped-down run would regain a group on its follow-up turn. See `memory-skills-hooks.md` § Switchable context groups for the section-by-section mapping.

Effort receipts retain the runner's selection namespace. An explicit `agent`,
including a template already resolved from `crew`, uses an empty crew claim;
implicit inheritance uses `SessionManager.get_agent_selection()` rather than the
parent's raw agent string. A member keeps its canonical claim and resolves the
bound provider template's model when its own model is unpinned. An absent parent
keeps the default template. If selection or model resolution is unavailable,
both optional receipt fields are omitted; this is distinct from a successfully
resolved `auto`, which reports the effort drop. Receipt lookup runs off the event
loop after the live selection snapshot, never prepares capabilities, and never
changes submission, allocation, governance, or private-memory authority.

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
- Returns a newline-separated list of per-agent JSON results (`status`: `completed` / `error`), all redacted for credentials and exfiltration URLs.
- **The result is held until the parent holds its slot again.** A `subagent:<id>` parent yielded its lane slot for this wait (W3); once every child is settled the tool long-polls `GET /api/spawn/{parent}/resume?wait_secs=8` (`_hold_for_parent_resume`) until `granted` — each request is held server-side on the grant event, so there is no grant interval — bounded by the same `max_wait` deadline. `known=False` (finished run, other incarnation, legacy gateway error payload) and a chat-turn parent release the hold at once. If the deadline passes while the slot is still ungranted, the children's results are still returned, followed by `{"status": "resume_pending", "parent": id, "note": ...}`.
- **The wait expiring is a fact about the call, not about the children.** When `max_wait` passes with sub-agents unsettled, the result ends with ONE envelope `{"status": "still_running", "task_ids": [...], "states": {id: "queued" | "running" | "waiting_permission"}, "waited_secs": N, "query": "spawn_status/spawn_list", "note": ...}`. Those children are NOT cancelled, NOT marked failed or timed out, NOT marked collected (so their completion events still inject), and keep their own execution budget (`subagent_timeout_secs`); the SEL outcome is `partial` with a `still_running` count. Settled siblings' results are still returned inline in the same reply.

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
  is retained for the grace window instead of deleted immediately. The generic
  writer snapshots any non-empty session ID, provider, and CWD from readable
  state; live abnormal-exit values captured immediately after session acquisition
  (before resume validation and context construction) override that fallback.
  Cancel recovery can acquire multiple sessions under one run ID, so persistence
  atomically records complete per-session cleanup generations in an owner-only
  record below the file-gated `trust/` root, outside the agent-writable run folder.
  Every read and write first applies the repository's fail-loud owner-only directory
  restriction, including inheritable Windows DACLs, and re-locks an existing record
  because tightening its parent does not retrofit an older file ACL. Only a missing
  record reads as empty; I/O, parse, or schema failures propagate, so an append can
  never rewrite unreadable history as a fresh one-generation record. Prune catches
  those failures per tombstone and continues later entries without altering the
  corrupt record; shared-session setup logs them as best-effort and keeps the live
  handle instead of falling into dedicated fallback.
  Each generation also records the run's retention intent and continuation owner
  key before the later best-effort combined state update. Protected generation
  ownership is authoritative even when readable agent-writable state supplies an
  empty or conflicting key; only the referenced owner's current readable state
  decides retention. The generation `keep` value is a fallback when local state
  lacks that field. Session acquisition publishes the new generation
  synchronously in memory before submitting durable work, so executor
  saturation or cancellation cannot prevent a terminal tombstone from seeing it.
  The in-memory fallback is append-only; the worker deduplicates only while
  serializing the protected record off-loop and never replaces the live list, so
  an older writer cannot discard a concurrent recovery SID. On the dedicated arm,
  durable generation persistence follows the cancellation-drained provenance
  write; both dedicated and shared identity workers are shielded and fully drained
  before cancellation is re-raised, so restart cannot precede protected authority.
  Cancellation cannot skip required model fields, and the already-published
  memory record still feeds the terminal tombstone. Slow storage therefore cannot
  stall chat/heartbeat or hide a just-acquired session from a contending tombstone,
  and a transient SID1 generation-write failure cannot be lost when SID2 later
  succeeds. Shared-handle ownership and provider references are attached
  immediately after handle creation, before any cancellable persistence
  await, so force-reap always destroys the shared handle rather than resetting a
  nonexistent dedicated session. Identity persistence errors are logged without
  triggering dedicated fallback or abandoning the live handle. Event-loop tombstone snapshots are memory-only: they acquire the identity
  lock non-blocking and use already-published in-memory generations, never reading
  the protected durable record. Executor-owned prune independently merges that
  record after restart before cleanup. The protected record and in-memory fallback
  are evicted when prune or explicit folder deletion succeeds. Tombstones expose
  the latest identity in compatibility fields and snapshot the full list for
  diagnostics/restart hints, but those agent-folder fields cannot authorize
  provider deletion. Prune's deletion set comes only from the protected record
  and synchronous live gateway publication, and reclaims every trusted generation.
  Retention fallback selects a protected generation matching the current readable-state
  SID, or the latest protected generation when the SID is absent or mismatched;
  agent-writable state/tombstone SID and owner fields never select a victim identity
  or suppress trusted owner/`keep` metadata.
- For readable state, only a literal current `keep is True` is retained; strings
  such as `"false"` are non-retention rather than truthy policy. `true` preserves
  the identity folder for restart registry rebuild; release writes `false`,
  allowing prune to retry provider cleanup.
  Every completed plain run records `false`; a readable legacy/failed-write
  record with no key is treated as non-retained and prunes at the normal cutoff.
  Readable `true` always defers disk prune; release or the conversation TTL writes
  `false` and owns deletion. This arbitration is part of the cleanup fix rather
  than a separate retention feature: once durable identity makes provider files
  reachable, a stale `keep=False` prune racing a continuation promotion could
  destroy the newly reachable resume material. Within the single gateway process,
  promotion and prune arbitrate under per-agent short-held state transactions.
  When a continuation tombstone points at an original owner, prune pre-resolves
  that owner and acquires both per-agent locks in stable order, then re-reads under
  lock; unrelated agents never contend. Promotion writes `true` before that locked
  read, or prune keeps arbitration through provider cleanup and folder removal so a
  later promotion returns retryable instead of racing deletion. On the event loop,
  promotion probes arbitration and the per-agent off-loop-writer lock non-blocking.
  Contention returns retryable `conversation_busy`, so a later retry writes
  `keep=True` only after every older writer completes.
- **A resume entry never holds a conversation.** `_conversation_busy`'s `_queue`
  scan matches UNSTARTED entries only. A `_resume_id` entry carries no
  `conversation_key`, so its synthetic key is the RUN's id — which IS the
  conversation id of a first-generation continuable run. While that run is live
  the `_agents` scan answers first, so the case that reaches the queue branch is
  an entry whose run has ENDED: only `_await_lane_resume`'s give-up arm withdraws
  one, the queued-stop path deliberately leaves it alone (dropping it published a
  "never started" terminal over a live run), and the pump returns above its
  resume loop whenever no slot is free — so under a full pool the entry outlives
  its run indefinitely. Counted, it answered
  `conversation_busy: run X is in flight — use spawn_steer` for a run that is
  done (and `spawn_steer` then answers `not_running`, a dead end), refused
  `release_conversation`, and made the TTL sweep refresh `last_used` on a
  conversation nothing holds, so its session files were never deleted. Pinned by
  `test_subagent_continuable.py::test_a_stale_resume_entry_does_not_hold_a_conversation`,
  which also pins that an UNSTARTED entry on the same conversation still holds it.
  Off-loop promotion lets `update_state` acquire that non-reentrant writer lock
  normally, avoiding self-deadlock while preserving serialization.
  Transient persistence errors likewise return retryable without dispatch. Retry
  restores the exact pre-attempt SessionManager and TTL-registry ownership; an
  already-retained conversation is never unmarked. The facade returns the result
  directly, so concurrent callers carry independent outcomes without a hidden
  clear/call/read side channel. A process crash leaves no half-committed claim
  format: the next prune re-reads the current owner state under arbitration.
  If state and a rewritten tombstone both lack a top-level SID, prune derives
  retention and owner from the latest valid durable cleanup generation instead of
  treating the record as non-retained. Provider cleanup runs before lock release;
  folder and protected-record removal follow only when every trusted generation
  reports success. Unsupported providers and transient deletion failures preserve
  both retry surfaces for later sweeps, capped at 90 days so a permanently missing
  cleanup route cannot accumulate private run folders forever. A legacy SID present
  only in agent-folder state/tombstone likewise preserves the folder inside that
  window: it cannot authorize deletion, but the lookup gives a later trusted
  migration time to reclaim the transcript. At the hard ceiling, only the run
  folder and protected metadata are reaped; untrusted identity is never used for
  provider-file deletion. Restart registry rebuild likewise accepts only literal
  `keep is True`, requires the state SID and conversation owner to match a
  protected/live generation, and sources provider/CWD from that trusted record;
  agent-folder state cannot seed a victim SID into the later TTL release path. An
  explicitly injected noncanonical state reader is an application-owned trusted
  seam; the canonical disk reader never takes that compatibility fallback.
  A continuation follows its original conversation's
  readable `keep` value directly: `false` or a missing key is non-retention, while
  unreadable owner state receives the bounded grace below instead of inheriting
  the continuation's stale local `true`. Registry rebuild, prune, and TTL sweep
  share `subagent_id_from_conversation_key`; malformed keys are dropped per entry
  so one corrupt record cannot abort later cleanup.
- Pruned by reaper: `delivered` tombstones after `agent.subagent_result_ttl_secs`
  (default 1h); all other tombstones after 7 days. `prune_stale_tombstones` takes
  a per-cause cutoff for this and treats timestamps exactly at the cutoff as
  eligible, avoiding platform clock-resolution gaps. Tombstone `died` must be numeric, positive, and
  non-future; string, NaN, infinity, future, and oversized values fall back to the
  validated tombstone-file mtime, or the current sweep time when no valid bounded
  time exists. That final fallback preserves unknown-retention grace across wall-clock
  rollback instead of making the record immediately eligible.
  Missing, malformed, deeply nested, or non-object tombstones are skipped for that
  entry without aborting later entries in the sweep.
  Missing, malformed, deeply nested, Unicode-invalid, or non-object `state.json`
  is unreadable. An acquisition-time `keep=false` generation remains unknown in
  this branch because a later promotion may have landed only in the now-unreadable
  state; only `keep=true` may collapse uncertainty, since it can only preserve data.
  A tombstone with a SID publishes only a SID-less in-memory retention hint, so the
  SessionManager file-deletion exemption performs no tombstone read on the gateway
  event loop without laundering agent-folder identity into provider-deletion
  authority; the executor-owned restart scan
  rehydrates that hint from durable tombstones before registry rebuild completes.
  A readable legacy continuation whose owner
  state is unreadable uses its resolved local-state SID for the same bounded
  protection, even when its pre-upgrade tombstone has no SID. A malformed
  continuation owner ID is likewise bounded as unknown retention for that entry;
  it cannot abort processing of later tombstones. At the cutoff, unknown intent receives one extra 24-hour grace anchored on tombstone death time;
  after that bounded window, trusted cleanup-generation metadata drives best-effort
  provider cleanup while tombstone metadata drives folder removal eligibility.
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
kwargs sourced from `cfg.agent.*`), and a later write to either field is
adopted live by `reconfigure` through `update_completion_keep`. User-facing docs:
[`src/kiro_crew/docs/configuration.md`](../../../src/kiro_crew/docs/configuration.md),
[`src/kiro_crew/docs/subagents.md`](../../../src/kiro_crew/docs/subagents.md),
[`src/kiro_crew/docs/troubleshooting.md`](../../../src/kiro_crew/docs/troubleshooting.md).

### Dashboard API: `POST /api/spawn`

Crew binding resolution and inherited memory lookup for persisted parent runs
execute off the gateway event loop. Unavailable member memory remains a typed
refusal before any child provider is allocated.

Request: `{"task": "..."}`
Response: `{"id": "abc123", "task": "...", "status": "spawned"}`
Errors: 400 (missing task), 429 (capacity reached), 503 (subagents not available)

**Typed rejections.** A rejection raised INSIDE `spawn()` answers 400 with a
machine-readable `code` beside the advisory `error` prose (plus `counted: true` —
see Wave liveness above): `agent_not_found` for a named-but-unknown agent,
`spawn_rejected` for every other kind (empty task, low memory, cwd refusal,
governance). `code` is the contract and `error` is advisory (RFC 9457 3.1.3),
which is what lets the refusal sentence be reworded without breaking a client.
The identifier is minted AT the decision — `subagent.AGENT_NOT_FOUND_CODE`,
returned by `_validate_agent` — carried on `SubagentInfo.error_code`, and
forwarded by the handler without being respelled there, so the value has exactly
one spelling in the tree.

`spawn_run` switches on that code for the wave short-circuit (#4842): once the
gateway has refused an agent name, the remaining members of a wave sharing it are
not re-posted. Fail-soft in both version directions — an old client still
text-matches the unchanged prose, and a new client against a gateway that sends
no `code` loses only the short-circuit (every member is dispatched and refused
individually) and never refuses a name the gateway would have accepted. That
asymmetry is why a missing code is safe here, and why a code is never used to
REJECT a spawn.

The request-validation errors (bad JSON, missing task, bad `approval_mode` /
`batch_id`), the 429 capacity answer and the 503 are prose-only today; converting
them is Track B work tracked by `error-code-baseline.json`.

Not yet true of the sibling endpoints: `POST /api/spawn/{id}/continue` and
`.../release` DO answer with a `code`, but they derive it by prefix-matching the
manager's prose (`info.error.startswith("conversation_busy")`), because
`continue_conversation` mints those two decisions as sentences rather than
returning an identifier. `SubagentInfo.error_code` is the carrier that would let
them be minted at the decision the way the unknown-agent refusal now is; until
that migration, treat `conversation_busy` / `conversation_gone` as inferred, not
minted. Two more consumers reconstruct the same two decisions from prose
internally (`crew_chat`'s queue hold, `continuation`'s busy retry), so the
migration has to move them together.

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
- `runtime.create_session()` runs under the ACP `SessionStartGate`
  (`agent.session_start_concurrency`, see acp-client.md). `_create_shared_session`
  passes `on_gate_acquired`, which resets `info._exec_started` / `last_activity`
  at gate EXIT so the 120s startup watchdog and the stall clock never count
  queue time, and records the wait in `info._start_queue_wait_ms`.
- **A `session/new` timeout is congestion, never a reason for a dedicated
  process.** `AcpRequestTimeout` from the shared runtime goes to
  `_await_late_start`: the row is marked `recovering`, and the run waits for
  the `StartCollector` that still owns the outstanding request. A late answer
  the collector adopts (the run is still live: not stopped, reaped or shut
  down) continues the run on that session under `_late_start_provider` and
  resets the start clock again; any other verdict (`torn_down`, `abandoned`,
  `runtime_dead`) ends the attempt with a `start_abandoned:` error. No second
  `session/new` and no `get_or_create` is issued for congestion.
- Only a NON-timeout failure of the shared runtime itself (dead, spawn failed)
  still falls back to the legacy per-process path (`get_or_create`); the
  explicit `model` / `reasoning_effort` / `allowed_tools` / `bare` / private
  memory constraints take the dedicated path by decision, not by fallback.
- Cleanup (`_run` finally + `_force_reap`) calls `_shared_provider.shutdown()` to
  tear down only the session — it never kills the shared runtime, which other
  subagents may still use. The runtime is killed when the parent session ends
  (`SessionManager.release_subagent_runtime`, invoked from `reset()`).

Non-kiro (alternate ACP backend) parents are never eligible and always use the
legacy `AcpClient` per-process path regardless of the flag.

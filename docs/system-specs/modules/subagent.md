# Subagent Module

## Overview

The subagent module (`kiro_crew/subagent.py`) spawns isolated background agents for parallel task execution. Each subagent gets its own LLM session via `SessionManager`, runs a focused task, and announces the result via callback.

Supports `on_tool_approval` callback for interactive tool approval (routed through gateway's approval system in Normal/Trust modes).

Subagent admission captures a frozen `ExecutionContext` before queueing,
approval or provider allocation. An ordinary child inherits its parent's member
and store. Explicit `target_member` (and Crew dispatch's member selector) resolves
an existing target under the ordinary spawn, owner/app and governance checks.
An `agent` override changes the execution template only; a project or template
name never selects memory. The queued task parameters carry the serialized
snapshot, so restarting or draining the queue never re-resolves a replacement
parent or current member display label.
Async spawn and cold continuation read missing canonical records off-loop once.
Session-map, busy, retention and policy mutations stay on-loop, with busy/admission
rechecked after the read. Retry carries the original run's member, app and mode
even after its parent closes; it never reconstructs them from the current parent.

Incognito and temporary spawns retain queued work only in memory. They always
keep their queue entry, including when the durable dispatch window is full, and
refill never evicts that sole copy. Only persistent entries consume the durable
window budget; the shared running cap, stagger, child reserve and memory floor
still apply. A restricted start that does not fit the floor waits in that
in-memory window instead of a store deferral: its entry carries
`MEMORY_WAIT_UNTIL_KEY` (monotonic, `now + admit_wait_secs`), the pump's pick
skips it until then, and a timer re-pumps when it passes
(`arm_memory_wait`), unless a memory wake clears the stamp sooner (*Memory
waits: event wake and the per-lane share*). That timer is armed one clock tick past the stamp, and a
wake that still finds the stamp ahead re-arms instead of draining: asyncio may
run a handle up to its clock resolution early (15.6 ms on Windows), and a pass
that skipped the entry would arm nothing. A restricted start never claims a
nonexistent row. Cancellation
removes its in-memory entry; a process restart cannot replay it. Restricted
admission diagnostics retain identifiers and lifecycle outcomes, not task text
or callback exception bodies.

`create_agent_folder` writes the canonical `execution_context` inside the run's
ordinary `state.json`. This record also owns selected template, stable member ID,
store ID, privacy mode and app attribution. New runs create no `memory.json` or
`agent.json` grant registry and no private/public body copy. Continuation reads
this owner record, retains its member/store after the original parent closes,
and fails on unknown or malformed member identity rather than choosing Global.
Existing V1 run records remain readable; old V2 formats are not migrated. The
pre-canonical writer marked Global and named V1 runs with `memory_binding_version=2`
as well: those runs read their existing `member-memory-bindings/<id>/memory.json`
only to retain the matching V1 store, original app owner and retention mode. A
missing, redirected or malformed sidecar refuses continuation. This is an existing
V1 consumer dependency, not V2 compatibility or a new registry writer; neither
the sidecar nor real memory data is migrated or repaired. Follow-up runs use the
canonical execution record.
Run execution publication and continuation read/owner-check/publication run in
one worker block using the captured context. Cancellation drains publication
before cleanup; filesystem locks and durable writes do not block the gateway loop.
The selection namespace describes persona selection independently of memory:
an explicitly selected template can inherit its parent's member/store. A one-turn
template override does not rewrite continuation lineage. A new continuation may
refresh ordinary member persona/capability configuration before dispatch, while
retaining the original member/store from the captured owner record.

The run's mode can only retain or tighten its original restriction. Incognito
and Temporary runs have live state and suppress persisted task, result and
transcript bodies; Incognito may recall memory, Temporary cannot. Neither can
write learned memory. Transient run state is discarded once terminal writers
settle; a retained conversation keeps only its original run state until release
or the existing conversation TTL. The canonical app owner remains separate from memory
routing: an absent or invalid app field in a new record is not a person-owned
run. Explicit template selection cannot remove app scope or bypass governance.

The ordinary `messaging.identity.publish_turn_identity` still publishes strict
transport/session identity before each dedicated attempt. Shared runtime handles
carry their own session key and ordinary token; member confidentiality proof and
PID ancestry publication are removed. Shared runtime ownership, unreaped handle
recognition, cleanup identities, spawn approval and callback delivery remain
unchanged. Member capability and native prompt documents still require a runtime
prepared for that member before launch.

Prompt construction receives the captured context explicitly and reads permanent
rules/brief/profile documents directly. Learned database preparation is optional
for this prompt; an explicit learned-memory operation fails on unavailable or
wrong-store data without selecting Global. Provider privacy mode is supplied
before shared-session creation or dedicated startup.

Continuation tools retain the original `subagent:<conversation-id>` while each
follow-up has its own run ID. Ordinary HTTP caller recognition uses the unique
active continuation for that conversation; queued or completed runs cannot supply
a live caller. The frozen record supplies memory and app scope independently of
that liveness check.

Every backend records the provider's actual working directory alongside its
session id. The next continuation uses this directory even when its target is
itself a completed follow-up and the gateway has restarted. This uses the common
provider `cwd` property, falling back to the admitted directory when unavailable;
the legacy Claude client fallback remains for providers without that property.
That record (`session_id` + `cwd` in `state.json`) is written once, after session
acquisition, with one bounded retry on an exception or a skipped merge, the same
as the pre-spawn model-provenance write; a record that still fails to land is
logged at WARNING rather than debug, so a continuation that loses its project is
visible in the log. Persistence never blocks the run.
When the recorded directory resolves to the current pool default, continuation
omits the redundant override. Disabled overrides therefore do not reject a turn
that stays in the default directory. A different recorded directory remains an
explicit override and must pass current directory policy, including after the
pool default changes.

## Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_MAX_CONCURRENT` | 3 | The `SubagentManager` constructor's `max_concurrent` default. The resolved cap's fallback and floor is `_LEGACY_DEFAULT_MAX` (also 3), and a pinned cap's floor is `config.sections.MAX_SUBAGENTS_FIXED_FLOOR` (3). `agent.max_subagents` defaults to `0` = auto: the cap is `agent.subagent_auto_max` (default 32) as written, a high count ceiling standing in for provider concurrency and fd / PID limits; memory bounds starts beneath it through the spawn floor (*Memory guard*), never the count. 3 applies when host memory cannot be read. A positive value pins a fixed cap (floored at 3). The cap is re-derived on every config reload, not only at boot — see [`reconfigure`](#reconfigurecfg-apply_limitscfg-max_concurrentnone-live-config). |
| `_TIMEOUT_SECS` | 10800 | Hard timeout per subagent (3 hours), from `constants.SUBAGENT_TIMEOUT_SECS` |
| `DEFAULT_SPAWN_MIN_MEMORY_GB` | 2.0 | Default of `agent.spawn_min_memory_gb`, from `constants.DEFAULT_SPAWN_MIN_MEMORY_GB`: GiB that must remain available AFTER an admitted start. The one source for the dataclass default, the loader fallback, the gate's fallback and `check_memory_available`'s default |
| `_UNLEARNED_DEDICATED_START_GB` | 1.0 | Dedicated start price for a cost bucket with no learned settled figure yet: the default `kirocrew` agent's measured first-session tree (kiro-cli 2.26.1, USS, its MCP roster included), rounded up. See *Memory guard* |
| `_SETTLED_PROJECTION_CEILING_GB` | 2.0 | The most a learned settled figure may raise a dedicated start's price |
| `_SETTLE_AFTER_SECS` | 120 | A row whose own session answered this long ago counts as settled where no sweep can measure it (macOS, Windows) |
| `_SHARED_START_SAVING_GB` | 0.35 | What a start that shares its parent's runtime does not launch (the kiro-cli process and its first-session warm-up, measured); a shared start is priced at the dedicated projection less this, never below `_SHARED_START_MIN_GB` (0.05). See *Memory guard* |
| `_RSS_SAMPLES_TO_SETTLE` | 2 | Sweeps before a dedicated worker counts as settled and owes nothing |
| `_DEDICATED_TOPUP_WAIT_SECS` | 60 | Longest a start admitted at the shared price waits for the floor before launching a dedicated process anyway (polls every `_DEDICATED_TOPUP_POLL_SECS`, 2 s) |
| `_ON_DONE_TIMEOUT` | 1200 | Outer cap (`subagent._ON_DONE_TIMEOUT`): max total seconds for semaphore wait + injection (20 minutes), enforced by `asyncio.wait_for` around `_on_done` in `subagent_manager/terminal.py`. Inside it, a channel or no-tab parent gets up to `_MAX_INJECT_ATTEMPTS` (2, `slack/gateway.py`) injection attempts, the second only after a timeout, before the failure is queued |
| `INJECTION_TIMEOUT` | 900 | Inner cap: max seconds for a single `stream_and_collect` call (15 minutes); default `_DEFAULT_INJECTION_TIMEOUT = 900.0`, tunable via `KIROCREW_INJECTION_TIMEOUT` (float seconds, clamped to `_ON_DONE_TIMEOUT`) |
| `_RESET_TIMEOUT` | 30 | Max seconds for session reset in finally block |
| `_TURN_LIMIT` | 1000 | Default tool-call budget per subagent, from `constants.DEFAULT_SUBAGENT_MAX_TURNS` (configurable via `agent.subagent_max_turns`, per-spawn via `max_turns`) |
| `_STALL_IDLE_SECS` | 120 | Seconds with no stream activity before a running subagent is surfaced as **stalled** in the running-card (configurable via `agent.subagent_stall_idle_secs`). The badge never terminates anything. Its one effect beyond the card: a stalled child no longer holds the parent's user messages (`chat_runner.subagents_hold_user_messages`): a new send is dispatched, and a message already parked when the flag is set drains immediately — the `subagent_stalled` event fires `chat_runner.drain_idle_parent_queue_for_stall`, which starts the idle parent slot's queued-turn drain (a running slot and a live sibling are left alone). The user closes it from the UX (per-row stop / Stop-all) and the three-hour default `_TIMEOUT_SECS` ceiling still applies. A WATCHDOG stall (`EVENT_COMPLETE` with `error: tool stall`) is a different signal and IS acted on: see *Stop reason → state*. |
| `_SYSTEM_PREFIX` | (string) | Injected before task text to prevent spawn recursion |
| `COMPLETION_KEEP_DEFAULT_CHARS` | 3000 | Default character cap for the completion event injected into the parent session (configurable via `agent.completion_keep_chars`). Lives in `context_management.py` alongside the helper. |

### Turn Limit Resolution Chain

Priority (highest wins): **per-spawn `max_turns`** → **config `agent.subagent_max_turns`** → **shared default (1000)**

A value of `0` means "not set" and falls through to the next level. Implemented as `SubagentManager._effective_turn_limit()`, shared by the enforcement path in `_run_inner()` and the timeout/reap error strings (`_timeout_context()`).

The larger tool-call budget does not disable the three-hour default execution
deadline, reaper, cancellation or provider stall handling. A missing config key
automatically uses 1000 when the updated build loads. Every valid stored budget,
including 100, remains authoritative. Older full-config saves did not record
whether 100 was chosen or merely materialized, so the superseded-default registry
reports that value without rewriting it. The existing `kirocrew config defaults
--adopt agent.subagent_max_turns` command removes that pin when the operator chooses
to follow the current default; `--keep` affirms it.

### Memory, not a count cap, bounds concurrency

When `agent.max_subagents == 0`, `compute_max_subagents()` answers
`agent.subagent_auto_max` (never below 3). It is not sized from host memory: the
spawn floor (*Memory guard*, below) prices every start at what it will settle at
and queues the ones that would not leave `spawn_min_memory_gb` free, so on any
16-32 GB host memory is reached long before 32 starts. A count sized from the
same memory on top of the floor -- and an adaptive execution cap that started
at 4 and halved on loop lag or low memory -- is what throttled a second chat's
subagents behind the first chat's wave while memory was still free. The ceiling
still bounds what the floor does not model: provider 429 storms and fd / PID
exhaustion ([adaptive-concurrency.md](adaptive-concurrency.md) cuts beneath it
on those). Where `_available_memory_gb()` cannot read host memory at all the
floor fails open, so the count is the only guard left and the auto cap is 3.
A floor the operator disabled (`agent.spawn_min_memory_gb <= 0`) admits every
start the same way, so there the auto cap is sized from host memory
(`compute_memory_sized_parallel_cap`, the TaskRunner's auto figure), and the
floor switch plus that figure's inputs (`subagent_mem_buffer_pct`,
`subagent_cost_gb`, `session.pool_size`) are live sizing paths.

Every consumer of the resolved cap (`resolve_max_subagents`, never 0) has a
defined figure: the manager's ceiling; the `{{MAX_SUBAGENTS}}` prompt token
and the spawn tool descriptions, which also say each start waits until host
memory can hold it; `resource_status`; the dashboard's `subagent_cap` readout
(`/api/system`, now the resolved cap, explicit pin included); and the MCP
gateway spawn gate's ceiling, raised to it
([adaptive-concurrency.md](adaptive-concurrency.md), *The spawn gate's ceiling
follows the subagent ceiling*). The TaskRunner's auto
`taskrunner.max_parallel_steps` keeps a memory-sized figure,
`compute_memory_sized_parallel_cap` (`[3, subagent_auto_max]`), because no
per-start floor prices a TaskRunner step ([taskrunner.md](taskrunner.md)).
Workflow agent concurrency stays the dashboard's fixed 4, and a non-positive
figure is bounded there, never read as unlimited ([workflows.md](workflows.md)).

**Accepted residuals.** Windows sizes nothing from commit charge: the floor
reads available physical memory (`platform_compat.host_available_mib`), so a
host near its commit limit with physical memory free can still admit starts
that later fail to commit; tracked as
[#16983](https://github.com/kirodotdev/KiroCrew/issues/16983). And under an explicit
`max_subagents` N, a start parked on the spawn-approval prompt holds its
running slot for as long as the prompt is unanswered (`pump.py`'s released-start
path re-enters with its slot already held), so N unanswered prompts hold all N
slots; with the auto ceiling the same parking costs slots no one is short of.

The available-memory reading itself is `_available_memory_gb()` (the TaskRunner
figure and the unreadable-host fallback), dispatched per operating system (see
`dynamic-subagent-sizing.md`); the spawn floor reads the same sources through
`check_memory_available`:

- **Linux** — `/proc/meminfo` `MemAvailable`, then clamped by cgroup headroom.
  `/proc/self/cgroup` and `/proc/self/mountinfo` locate the process's v1/v2
  memory controller, including bind mounts. The tightest readable headroom
  across its cgroup and visible ancestors binds; each limit uses usage from
  that same level. V1 ancestors bind when `memory.use_hierarchy` is enabled.
  A finite limit with unreadable or invalid usage contributes zero headroom:
  spare capacity cannot be established. The spawn that this defers says so
  rather than reporting a figure: its detail reads `memory headroom unknown: a
  finite cgroup memory limit is set but its usage is unreadable; restore read
  access to memory.current / memory.usage_in_bytes`, and the
  `deferred_low_memory` SEL row carries `cause: cgroup_usage_unreadable`
  (`MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE`). Measured zero usage retains the full
  limit as headroom. Missing discovery retains the conventional root-path
  fallback; ancestors hidden above a mount cannot be measured. Usage at every
  level is the working set: `memory.current` (v1 `memory.usage_in_bytes`)
  minus the group's inactive page cache (`inactive_file` in v2 `memory.stat`,
  `total_inactive_file` in v1), floored at zero, via `_working_set()`. The
  kernel drops that cache on demand, so counting it as used made a slice full
  of cold build cache read as 0 GB free. An unreadable `memory.stat` subtracts
  nothing. The agents slice's own ceiling
  (`min(memory.high, memory.max) - working set` on `kirocrew-agents.slice`,
  read via `_agents_slice_available_gb()`) is a second clamp: the slice is a
  sibling of the gateway's cgroup, not an ancestor, and the walk never reads
  `memory.high`, so the slice term is what lets the posture go `critical` on
  a bare host whose `MemAvailable` is still large while agent process memory
  has filled that ceiling.
- **macOS** — reclaimable memory (free + inactive + speculative + purgeable
  pages) via the Mach `host_statistics64` syscall through `ctypes`/`libSystem`
  (`_macos_vm_reclaimable_pages`), combined with the `os.sysconf` page size.
  This is **in-process, non-blocking, no subprocess** — required because the
  probe runs on the gateway event loop at startup and the spawn-audit guard
  rejects unrouted subprocess spawns. A reload re-runs it off the loop
  (`asyncio.to_thread`), since by then the loop is serving turns.
- **Windows** — available physical memory from
  `platform_compat.host_available_mib`, converted from MiB to GiB. An unreadable
  result returns `-1.0`, and the auto cap falls back to the legacy floor of 3.
- **Other** — no probe yet; returns `-1.0`, and the auto cap falls back to 3.

Hard floor: the auto cap is always ≥ 3 — `compute_max_subagents` floors
`subagent_auto_max` at 3 and the config loader clamps it UP to 3 (with a
warning + `config_bounds_clamped` SEL event, mirroring the > 64 ceiling clamp).
Zero remains the auto sentinel; an explicit `max_subagents` pin is clamped to
3..64 (and dashboard writes must also fit under the configured
`subagent_auto_max`). The floor of 3 for an explicit pin is kept as it is.

The per-spawn `spawn_min_memory_gb` admission gate (`check_memory_available`)
uses the same cgroup headroom on Linux and native memory readers on macOS and
Windows. A known cgroup bound still applies when the Linux host reading fails;
readings fail open only when neither host memory nor a finite cgroup limit is
available.

#### Memory guard: what must remain after the start

`agent.spawn_min_memory_gb` (default `DEFAULT_SPAWN_MIN_MEMORY_GB`, 2.0 GiB) is the
memory that must remain available AFTER a start is admitted; `0` disables the
floor, the reserve and the host reading together (no read is taken, so an
unanswered one cannot hold a start back). When enabled, the per-spawn guard adds
`_startup_memory_reserve_gb` to it, so a start is admitted only if
`available − Σ outstanding start prices − price(this start) ≥ spawn_min_memory_gb`.
Below that a spawn waits in the queue (`low_memory`), durable or not; a capacity
verdict never fails it, but the wait is bounded by `agent.subagent_queue_max_wait_secs`
(§ Durable task queue, "A memory deferral has a max wait"). The governance, cwd and memory-identity checks that run
earlier in the same gate are refusals and stay refusals. A non-durable
`low_memory` wait (an incognito or temporary start, or any start with no store)
lives only in the in-memory window, so a gateway restart drops it like any other
in-memory queued entry: the id the parent was handed is gone after the restart,
and no completion arrives for it.
`test_spawn_admission_verdict_census.py` pins the complete set of verdicts the
gate can give at defaults (its SEL outcomes and those written elsewhere on the
spawn path, its wait labels, every refusal text `spawn_impl` can return, and
what each capacity and policy condition returns), so a new gate or a new
refusal goes red there first, whether or not it writes an SEL outcome. A durable
deferral the store could not write (no row behind a `_queue` entry, or the store
unavailable for the write) is the one memory-path refusal, and it is the
store's verdict: `error_code="task_store_unavailable"`, worded as the store
failing to record the wait, with the memory figures as context.

**The floor is read off the event loop.** The reader walks cgroup files on
Linux. The event-loop entries (`spawn_async`, so `/api/spawn`, channel and app
spawns, and the coroutine pump re-checking a queued row) pass
`_stop_before_memory_read`: the gate runs every policy check, computes the bar,
and returns a `MemoryReadPoint` (bar + params) with nothing reserved. The caller
reads the host on a worker (`_host_memory_reading_off_loop`, bounded by
`_HOST_READ_OFF_LOOP_SECS`) and re-enters with `_memory_reading`. There is no
on-loop fallback: a worker that misses the bound, or a pool that cannot start a
thread, comes back as `MEMORY_CAUSE_READ_UNANSWERED`, and the start waits as
`low_memory` ("memory headroom unknown"), re-checked after the admit wait. An
unanswered read is never the reader's "unmeasurable" -1, which fails open. The
read is single-flight per process, whatever the bar: the figure and its cause do
not depend on it, so a caller that arrives while any read is in flight awaits
that one (shielded, so its own timeout never cancels it) and compares the figure
against its own bar. The bound ends a caller's wait and not the worker, and the
bar differs per agent bucket and moves as warming rows settle, so a reader that
hangs would otherwise take one more executor thread at every re-check and for
every distinct bar. The
re-entry re-runs the policy gates on every path, so a governance change made
during the read still refuses it; only the batch count and the row write are not
repeated. A refused spawn whose row already exists has that row failed in the
same step (`_refuse_row` -> `taskq_fail`): a drained row, and a row `spawn_async`
committed before the read (`_store_accepted`, the default persistent path). A
committed row is failed the same way when gateway admission closes during the
read, so a row the caller was told was refused never runs once admission
reopens. `spawn_async` hands the re-entry the parent's declaration and the
agent check it read off the loop (`_parent_spawn_policy`, `_agent_check`), so
neither the allowlist vet nor agent validation scans the agents directory on the
loop. A spawn with no row to write (non-persistent, or the task queue off) runs
both passes with `_child_registration=False` and awaits
`taskq_child_registered_async` itself, as the durable path does, so the W3
registration's ledger reads never run on the loop. The fit is decided on the loop against the bar recomputed
then, so a start admitted during the read is charged. On a running loop the pump is
the coroutine with or without a store, so the timer that re-pumps an in-memory wait
(the task queue off) never reads on the loop either. Synchronous `spawn()` callers, and
the inline pump (no running loop, or `pump_off_loop` off), still read on the calling
thread.

**A popped row with no durable record.** The coroutine pump pops a window row,
awaits the resume grants of the same pass, then awaits the row's off-loop reads
(parent policy, agent check, the floor) before the gate starts it. A durable row
survives that gap in the store, and its claim re-checks a stop. A row with none
(incognito or temporary memory, or the task queue off) is held in
`_undurable_in_dispatch` from its pop until the gate takes it, as its only copy.
Every stop path finds it there: `cancel` through `_unqueue`, and Stop all and
the parent-end snapshot through their selection. The pump checks the entry after each
await (the read's re-entry through `_spawn_after_memory_read`'s `proceed`) and
does not start a row a stop took, whether the stop landed during a grant or a
read. A raise in the gap (a pool that cannot start a thread, in a read or in a
grant ahead of the dispatch) puts the row back at the front of the window, not
eligible again until the admit wait passes (`_requeue_undispatched`), instead of
dropping it.

**Prices.** Decided per start (`_spawn_memory_floor_and_cost` reads the floor and
`subagent_cost_gb` for every caller) and carried on the row as `_start_price_gb`,
so every later admission charges a row at the price it was admitted at:

- **Dedicated, or undecided.** The dedicated projection for the start's cost
  bucket (`_cost_bucket`, `_dedicated_start_price_gb`): the bucket's learned
  settled RSS once it has three readings, capped at
  `_SETTLED_PROJECTION_CEILING_GB`, else `_UNLEARNED_DEDICATED_START_GB`; never
  below `agent.subagent_cost_gb`. The cap exists because a bucket priced out of
  admission runs no dedicated starts and so never relearns: a few mis-measured
  readings would otherwise lock it out for good. A non-finite learned figure is
  ignored. These are internal admission prices with no config key of their own.
- **Shared.** A start that will take a session on its parent's runtime launches
  no kiro-cli process, but it does launch the agent's MCP servers: kiro-cli
  starts a fresh copy of every declared server for each session, shared or not.
  So it is priced at the dedicated projection less `_SHARED_START_SAVING_GB`, the
  process it skips, never below `_SHARED_START_MIN_GB`
  (`_shared_start_price_gb`), and flagged `_start_priced_shared`. Whether it will
  share is the run's own decision, `_sharing_plan`: template execution, not
  `keep`, `_should_use_session_sharing`, and no per-spawn or `agent.role_models` /
  `agent.role_efforts` pin for `subagent`. `_should_use_session_sharing` in turn
  requires no `_force_dedicated` (a recovery respawn), no member execution context
  (`execution_context.member_id`, whose capability and prompt documents are
  prepared at launch), `agent.session_sharing` on, no per-spawn `model` /
  `allowed_tools` / `bare`, a parent session key, and, as its last term, the
  backend's opt-in `is_session_sharing_eligible`. `_run_inner` takes the shared arm from
  the same plan, so the price cannot follow a copy of the rule. Only a real
  `True` prices a start shared; an unknown answer is priced dedicated. Native
  subtasks (kiro-cli `use_subagent` and similar) never reach this gate.

At defaults with nothing warming or learned: a shared start needs 2.65 GiB
free, the first dedicated start 3.0 GiB, a second while the first warms
4.0 GiB, and a nested start that shares its parent's runtime 2.0 GiB (*Memory
waits: event wake and the per-lane share*; RFC Q12, D4).

**What each outstanding start owes.** The next start its own price. A claim
admitted but not registered yet (a `ClaimPoint` awaiting the taskq writer) is
charged the price its admission checked (`_claim_prices`), and the re-entry that
registers it stores that same price rather than computing a new one nobody
tested; a retained claim keeps its price for as long as it holds its slot; any
other unregistered slot is charged `subagent_cost_gb`. A live row owes its price
IN FULL until it settles (`_row_settled`): two sweeps measured it, or, where
nothing can measure it (macOS and Windows have no `/proc` subtree reading), its
current process's own session answered `_SETTLE_AFTER_SECS` (two sweep
intervals) ago on the monotonic clock (`_first_stream_mono`, tied to the
process's `_rss_generation`, so a laptop's sleep or a respawn never settles a
warming row). No credit is taken for the RSS a warming row already shows: the
sweep reads summed VmRSS, which counts a tree's shared pages once per process
and runs ~1.5x the PSS the prices are in, and a shared row's reading is a share
of a runtime others use (its MCP servers also start after it binds). That
double-counts a warming row's memory for at most two sweeps, toward reserving
more. A settled row owes nothing, since its memory is already inside the
free-memory reading. A dedicated row with no admitted price owes the dedicated
projection; a shared one, nothing.
Yielded parents retain their reservation; queued/terminal rows contribute none.
A row parked at the spawn-approval prompt is registered and owes its price while
it waits for an answer.

What the prices are measured against (a `kiro-cli acp` 2.26.1 process driven
with Kiro Crew's own `initialize` / `session/new` shape, sessions added one at a
time, process-tree USS/PSS, which count shared code pages once): an extra session
on a running runtime costs ~0.01 GiB with no MCP servers and ~0.45-0.6 GiB with
the default `kirocrew` agent's roster, every server a fresh process per session;
a dedicated start costs that plus ~0.2-0.35 GiB for its own process, ~0.96 GiB in
all. **The prices are projections**, from those measured defaults until a bucket
has learned its own settled size. A bucket lighter than the default agent (few or
no MCP servers) is over-priced until it learns; an install that only ever runs
shared starts never learns (shared runs are not captured), and neither does one
on macOS or Windows (no subtree reading), so both stay at the measured defaults.
Learning a per-session shared price is a follow-up, not part of this guard.

**The learned settled size** is what a dedicated runtime holds once it is up. The
reaper's sweep captures one reading per process (`settled_rss_gb`): the first
subtree sample after the run's own session answered (`_first_stream_started`)
with no tool in flight before the off-loop read, none after it and no activity
during it (`_stall_gen` unchanged). It is read in PSS
(`platform_compat.proc_subtree_sample(..., pss=True)`, summed `smaps_rollup`), the unit the
unlearned price is measured in, because summed VmRSS counts the code pages a tree
of node / MCP-server processes shares once per process and reads ~1.6x high; the
RSS figure stands in only where PSS cannot be read. Not captured: a shared run,
whose pid's tree holds the other tenants and the parent's own tools, and a
dedicated run whose own children share its runtime, whose tree holds their
per-session servers. `_record_cost` writes it as the cost record's `settled_gb`
beside the whole-run peak `mem_gb`, which the auto cap still reads unchanged. The
admission side reads the per-bucket p90 of dedicated `settled_gb` (at least three
readings) from `_learned_settled_gb`, which the reaper refreshes off the loop at
start and after every sweep; the gate never opens the cost log. A start is never
priced at the whole-run peak or a whole-tree p90: a run's peak RSS is its whole
process subtree, test suites, builds and MCP servers it launched included, and
pricing the next start at that held ordinary spawns at 10 GB+ on a laptop.
`_inflight_tool` is empty only when none of the agent's own calls is in
flight, parallel calls included, so a "quiet" reading never overlaps one of
its running tools. A cancel-recovery
respawn resets the run's sample count and last reading and bumps
`_rss_generation`, which the sweep re-checks after its off-loop `/proc` read so a
reading of the dead process cannot settle the new one; the dead process's settled
reading stands until the new one is captured.

**A shared price is topped up before the start turns dedicated.** A start
admitted at the shared price can still launch a process: `_create_shared_session`
fails with the shared runtime unavailable (not a `session/new` timeout, which
never takes that path), or the plan has changed by the time the run starts
(parent gone, sharing turned off, a role pin set while the row waited). Before
that process spawns, `_ensure_dedicated_start_priced` raises the row's price to
the dedicated projection, so every later admission charges it, and re-checks the
floor with this row at that price. The plain dedicated arm also clears a
`_session_sharing` flag left by an earlier shared attempt of the same run, which
would otherwise hand the process's teardown to the shared arm. Only a row
flagged `_start_priced_shared` is topped up; a dedicated admission whose bucket
learned a higher figure since was checked at its own price and is not re-checked.
Below the floor the start waits with its start clock frozen, re-reading every
`_DEDICATED_TOPUP_POLL_SECS` off the loop (`_host_memory_reading_off_loop`, the
gate's own reader: one read waits at most `_HOST_READ_OFF_LOOP_SECS` for the
executor, and an unanswered read is headroom unknown, so the start keeps waiting
for the next poll; it is never read on the loop and never taken as the reader's
fail-open -1; the fit is decided on the loop against the need recomputed after
the read), one such start at a time (`_dedicated_topup_lock`;
the one holding the turn does not count the ones queued behind it, which have
launched nothing), for at most `_DEDICATED_TOPUP_WAIT_SECS`; then it starts anyway
with a WARNING and a `dedicated_start_below_floor` SEL row (carrying the
read's `cause` and no `available_gb` when the last read did not answer). The
start clock resumes when the wait ends, a cancel included: the wait is added to
`_start_queue_wait_ms`, like any start-queue wait.

**What the floor guarantees, honestly.** Admission never takes the host below
the floor at the prices above, and the prices are projections, with one stated
exception: a nested start that shares its parent's runtime is checked with its
own price at 0 (*Memory waits* below), so it can take the host to
the floor less its shared price (the floor gives way by one shared start, not
by a fan-out). The floor is not
above `resource_critical_gb` (both 2.0 at defaults, and posture counts equal as
critical), so a host admission has filled to the floor reads `critical`: cron
defers its firings and the adaptive controller cuts and pauses the MCP spawn
gate (never the subagent cap, [adaptive-concurrency.md](adaptive-concurrency.md))
until 4 GiB is free. Spawns do not read the posture (admission step 4), so the
floor alone decides them. It does not shed
running work: a settled child that runs builds or tests can still push the host
below it. And a start admitted shared whose runtime then dies may, after its
bounded wait, launch a dedicated process below the floor rather than fail.

**Accepted decision: starts are priced settled, not at peak.** The memory-sized
auto cap this floor replaced priced every slot at a whole-run figure up front (a
typical run, with the heaviest agent's p90 peak reserved once), so growth after
admission was bounded by the count. The floor prices
a start at what it settles at (learned, capped at
`_SETTLED_PROJECTION_CEILING_GB`), and the auto ceiling no longer stands under
it, so a wave admitted at about 1-2 GB each that later runs builds can grow past
the floor, swap, or meet the OOM killer, and nothing lowers the count of
running agents in response. This is accepted, not measured: peak pricing is
what held a second chat's subagents behind the first chat's wave while memory
was free (#16480 symptom b), and the RFC already records that a prediction from
the busiest agent's burst pins a host with 96 GB free at its fresh-start cap
([rfc-overload-resilience §5.2, Q11](../../request-for-change/rfc-overload-resilience.md#52-two-actuators-one-policy-object)).
What still answers growth: every later start re-reads live free memory, so
running children that grow make the next start wait; the spawn gate halves and
then pauses backend starts at the critical line; on macOS the kernel
memory-pressure hold vetoes starts at WARN. Memory pressure is deliberately not
an execution-cap cut: a cut stops nothing already running (both actuators only
shrink as work finishes), so it would throttle new starts the floor already
gates, which is the count bound this change removed. An operator whose fan-outs
are build-heavy pins `agent.max_subagents` or raises
`agent.spawn_min_memory_gb`.

**Memory waits: event wake and the per-lane share.** A start the floor defers,
durable or not, is recorded from the deferral that BEGINS its wait until it
fits, starts or ends (`_memory_waits`, one `MemoryWait` per start;
`memory_wait_began` in the gate, `memory_wait_ended` when it fits and from
`_forget_pending_start`; `subagent_manager/admission/memory_wake.py`). The
record is process-local, like the pressure hold's clocks.

- **The gate audits every deferral it decides.** Each one writes
  the WARNING and the `deferred_low_memory` SEL row (cause `lane_share` with no
  figures for a share hold) and refreshes the record; a wait that ends because
  the start fits logs one INFO line with how long it waited. A fit pass is not
  a gate decision and audits nothing (below).
- **An event wake is a fit pass, and a fit pass that finds no room writes
  nothing.** A run reaching its terminal (`_run`'s `finally` after the session
  teardown, and the reap, for a run whose own `finally` never comes), a wait
  ending while a root start waits in another lane (it may be what the share
  held that start for; a wait ending frees no memory, so only the share can
  have moved, and a same-lane end wakes nothing) and each
  sampler tick call `wake_memory_waits`, which runs one `_fit_pass` (one at a
  time; a wake during one runs one more after it). The pass takes ONE host
  reading through the gate's own single-flight reader
  (`_host_memory_reading_off_loop`) and rebuilds each wait's bar from the
  current state the way the gate builds it (the floor, every start still owed
  but not in the reading, and the start's own charge, `MemoryWait.start_gb`).
  Oldest wait first, it brings forward a wait the reading clears, counting what
  the waits it already brought forward will charge (`price_gb`), unless the
  per-lane share would hold it. A wait that does not fit is LEFT PARKED: no
  store write, no `deferred` event, no depth frame, no audit row, so a host
  that stays short costs one reading per event. A reading with a cause
  (unanswered, an unreadable cgroup) brings nothing forward, as the gate would
  defer on it; an unmeasurable one (-1), or a floor turned off, brings every
  wait forward, as the gate admits then. Brought forward means: a window
  entry's `MEMORY_WAIT_UNTIL_KEY` is set to 0.0 (still a floor wait, so the
  pick leaves it to the gate) and its `_floor_waits` park is cut at now; a
  durable row is brought forward with `TaskStore.expedite` on the writer
  thread, which writes no event, and the pump runs once that has landed so the
  refill can take the row; the gate then decides it as always.
- **The admit wait stays as the backstop, and it is the one re-check that
  still writes.** A row re-checked when its deferral lapses and still short is
  re-parked through `store.defer`, which appends a `deferred` event, and its
  depth frame is re-published: one per `agent.admit_wait_secs`, the cadence
  before event wake. That event is the park record the max wait measures (§
  Durable task queue, *A memory deferral has a max wait*): `task_events` is
  append-only and `deferred_longer_than` sums parked spans from it, so making
  the backstop write nothing needs a parked-since record in the store, a
  schema change this does not make. A restart drops the in-process record; a
  parked row is then re-checked at its lapse and its wait recorded again.
- **The sampler runs only while a start waits.** The first recorded wait arms
  it and the last one ending cancels it (`_arm_memory_sampler`,
  `_disarm_memory_sampler`). Every
  `MEMORY_SAMPLER_SECS` (5 s) it prunes records whose start no longer waits (a
  registered run; a restricted entry in neither the window nor dispatch; a
  durable row no longer `CLAIMABLE`, read on the writer thread), the backstop
  for an exit that raced the deferral, and runs a fit pass, so memory another
  program frees is seen within a tick. A warming row that settles (it owes no
  start price any more, so every waiting start's bar drops) is seen the same
  way: the pass rebuilds the bar from `_row_settled` each tick, so the reaper's
  RSS sweep needs no wake of its own, and a settle costs at most one tick.
- **The per-lane memory share** (`lane_share_holds`). With count
  caps gone, memory is the pool every chat's subagents share, so one chat's
  wave could still take every start that fits while another chat waited. While
  another lane has a recorded wait and runs fewer dedicated children than its
  share, a ROOT start of a lane already running at least its own share waits as
  a memory wait, whatever the host reads, and no reading is taken: the cause is
  `lane_share`, the label `low_memory` with no figures, the detail "waiting for
  memory: subagents of another chat are waiting for memory and this chat
  already runs its share". A lane's share is its `agent.lane_weights` weight's
  part of the dedicated children running across the contending lanes (a
  dedicated child running, `_holds_dedicated_runtime`, the same row predicate
  the macOS hold counts, or a recorded wait): with three equal lanes running 2,
  1 and 0, each share is 1, so both the lane at 2 and the lane at exactly 1
  are held while the third waits. The starved lane is below its share by
  construction and is never held, so the share decides which lane the memory
  goes to next and cannot deadlock: the held lane waits for the starved lane's
  wait to end (it starts, or reaches the max wait) or for its own children to
  end. A nested start is never held (its parent is a running tree of that
  lane, which frees its memory only once its children have run), nor is a
  claim re-entry. It applies whenever the floor is on, with or without an
  explicit `max_subagents`, and adds no count cap; the weighted round-robin
  still orders the pick. **Accepted trade-off:** the hold does not ask whether
  the starved lane's start could fit now. A held lane whose start would fit
  (a shared start, 2.65 GiB at defaults) can wait behind a starved lane whose
  start cannot (a dedicated one, 3.0 GiB) until memory frees, the starved wait
  ends, or its own max wait does (time held by the share counts toward it);
  that is what keeps a chat that fits small starts from taking every start
  while another chat waits. Why both halves stay:
  [RFC Q12](../../request-for-change/rfc-overload-resilience.md).
- **A nested child that shares its parent's runtime is admitted at ~0.**
  Children-first ordering does not free memory: a parent that
  yielded still holds its RSS. So a nested start whose `_sharing_plan` is shared
  is checked against the floor with its own price at 0, which lets it take the
  host down to the floor less its shared price, while its row still carries the
  shared price (`_start_price_gb`), so every later admission charges it until it
  settles. At defaults it is admitted at 2.0 GiB free, and a sibling arriving
  while it warms needs 2.65 GiB, so the floor gives way by one shared start, not
  by a fan-out. A dedicated nested child is priced as before.

**macOS: the kernel memory-pressure hold.** The macOS reading of the floor has
a second input: the kernel's `kern.memorystatus_vm_pressure_level`, read fresh
on every check through `resource_status.read_memory_pressure_level()`
(`platform_compat.memory_pressure_level()`, a `sysctlbyname` on the cached libc
handle; it says once per process, at WARNING, when macOS cannot answer it). This
section is the one statement of the hold's rules; the code points here.

The hold applies (`_memory_pressure_hold`) while all of these are true:

- the floor is on (`spawn_min_memory_gb > 0`);
- the level is WARN or CRITICAL;
- a dedicated runtime of this gateway is running or warming
  (`_owns_dedicated_runtime`): a live row admitted at the dedicated price and
  not confirmed shared, or a claim admitted at the dedicated price
  (`_claim_prices`) and not registered yet. A row with no process does not
  count: one parked at the spawn approval (`_parked_at_spawn_approval`) or one
  approved and waiting for the pump to release it (`_start_release`). Neither
  do shared-priced starts: their end returns little memory, and counting them
  would hold a shared wave behind its own first member.

It keeps a start (`_memory_pressure_holds`) when, in addition:

- the start is a root (not nested, `taskq_parent_id_for` / `entry_is_child`);
  a child's parent is a live runtime of ours waiting on it, so holding the
  child would hold the parent on an episode only the child can end;
- it is not a claim re-entry (`_dispatch_now`), which already holds its slot;
- the start's own wait has not run out. A row's wait is clocked from the first
  time it is held, and past `agent.subagent_queue_max_wait_secs` (read live off
  the manager at each check through `taskq_memory_wait_bound_secs`, the bound
  the store deferrals use; `0` is no bound) it never proceeds
  into the pressure it waited on: it is ended, never started
  (`MEMORY_PRESSURE_NEVER_STARTED`, which OPENS with its verdict because a
  surface grouping terminal runs would otherwise read it as a success; its row
  failed, its parent's depth recounted and the terminal record registered so a
  read by id answers the outcome rather than a 404; the run card headlines a
  wave whose every member ended this way as "N agents never started", and a
  settled wave where only some did as "M of N agents ran", matching
  the error's opening words through `NEVER_STARTED_PREFIX`, which lives in
  `website/src/lib/backendPhrases.json` because it is backend prose the UI
  matches rather than copy it renders; the error itself opens with the owner's
  wording, "never started: waiting for memory", and names both ways out, the
  level easing and a running agent finishing, which the card states whenever any member
  ended this way, as an inline `ErrorNotice` directly under the card's button
  (never inside it), counted ("1 never started — ...") and naming the panel's
  Retry failed control,
  so a mixed wave's "finished" header cannot hide it and the retry is not read
  as automatic), with a WARNING and
  a `never_started_memory_pressure` SEL row
  (`_pressure_hold_expired`) whose `expired_by` says which bound ended it:
  `wait` (its own wait ran out) or `episode` (the spent episode ended it at
  once, so `waited_secs` is near 0 and `episode_secs` carries the episode's
  length). An approval-released start past its bound ends the same way. The
  pump's pick only classifies an expired row (it is picked); the gate's
  re-check is what ends it and writes the record, so a row whose level eased
  in between starts with no "never started" audit. The clock survives a pause
  in the hold, such as our last runtime ending between two of a wave's
  starts, so a wave released one runtime at a time still meets the bound; only
  a clock older than `_PRESSURE_HOLD_PRUNE_FACTOR` times the bound (the key's
  default when the bound is lower or `0`), a row that left with no
  registration or refusal, is dropped.

**Foreign pressure with a runtime of ours alive, weighed.** The hold cannot
tell its own load from foreign load (a browser, a build). So with one
long-lived dedicated subagent of ours running (a `keep` conversation, say) on a
Mac held at WARN by other apps, every new root start waits, up to its bound,
even though ending our runtime might not end the episode. That is the chosen
side: a new runtime under WARN slows the whole machine, not just Kiro Crew, and
the per-row bound caps the wait at `agent.subagent_queue_max_wait_secs` (30
minutes by default),
after which the start is ended rather than launched into the pressure. A
chronic episode does not make every new start wait that long in turn: once the
hold has applied without a break for the same bound
(`_pressure_episode_since`, from the first read at which it applied; a read at
which it does not apply, because the level eased or nothing of ours runs, ends
the episode, and so does a gap between reads longer than
`_PRESSURE_EPISODE_MAX_GAP_SECS`, a break nobody observed), the episode is spent (`_pressure_episode_spent`, one WARNING) and
every start the hold would keep is ended at once, never started, until the hold
stops applying. The bound is live here too: a read under a bound the episode no
longer outlives (one raised past it, or `0`) un-spends it, so new starts are
held again rather than ended.
With no runtime of ours the hold never applies, so foreign pressure alone never
delays or ends a start. `spawn_min_memory_gb = 0` turns it off. The
shared-to-dedicated top-up is not a held start: it is an admitted run already
starting, so past its own bound it starts and says so
(`dedicated_start_under_memory_pressure`), as it does below the floor.

The floor's exit runs first: below the floor a start waits as `low_memory`
(deferred when it has a row, stamped in the window when it has none), and that
wins over this hold: such a start is not held, so it carries no pressure clock
and is never ended "never started". A clock an earlier hold started for it is
dropped when it goes back to waiting on the floor, and the pump's pick never
asks the hold about a stamped floor wait (`MEMORY_WAIT_UNTIL_KEY`): the gate
re-checks the floor first, so time spent below the floor never counts toward
the hold's bound. A durable row deferred on the floor keeps only `next_run_at`
in the store, so the manager remembers it (`_floor_deferred_ids`) and the
window refill stamps it as a floor wait; the mark is one-shot (the pick drops
it and a fresh floor defer sets it again) and process-local, like the hold's
clocks: a restart refills the row unmarked, as one this process never
checked. Spawns do not read the posture tier, so no posture verdict precedes
the hold. Agent validation runs before both, so an
unknown agent is refused `agent_not_found` instead of waiting.

The level is the kernel's own verdict, and it lags. A 16 GB Mac has read NORMAL
with 4.9 GB in the compressor and 4.8 of 6.0 GB of swap in use, so the level is
no measure of free memory and does not replace the figure. It is a backstop
beside the floor: when the kernel does say WARN, the host is short whatever the
page counters add up to. A start the floor priced shared is held too: it skips
the kiro-cli process but still launches a fresh copy of the agent's MCP servers
(~0.45-0.6 GiB with the default roster, see *Prices* above).

A held start waits like a capacity wait, not a store deferral. It joins the
in-memory window (`_queue`) exactly as a `concurrency_limit` row does, durable
or restricted alike, labelled `QUEUED_REASON_MEMORY_PRESSURE` with the
figure-free detail `MEMORY_PRESSURE_DETAIL`, and the pump's pick passes over
held roots (`pick_window_index(root_held=...)`) the way the child reserve does.
Every slot release and pump pass re-checks it, and while the hold applies one
timer (`_pressure_recheck_handle`, cancelled at shutdown) pumps every
`MEMORY_PRESSURE_RECHECK_SECS`, so a level that eases with nothing finishing is
noticed. The label is the binding reason: a start the hold keeps is labelled
`memory_pressure` even when a full cap or the stagger would also have queued
it. A row first held at the
pump's pick (queued earlier for capacity) is relabelled there. When the hold
stops applying, a parent still labelled `memory_pressure` is relabelled
`concurrency_limit`. Because the row never leaves the window
machinery, its crew-log pin, its parent's teardown and the continuation checks
see it as they see any capacity wait. The same rule is re-checked where a start
could otherwise pass it later: an approval-released start
(`_release_admitted_start_impl`, so an approval answered after the hold began
waits too; it is decided per entry, so a held root is passed over rather than
blocking the nested starts released behind it) and a shared start turning
dedicated (`_ensure_dedicated_start_priced`, under that wait's own
`_DEDICATED_TOPUP_WAIT_SECS` bound).

What a held start reports: `deferred_memory_pressure` in SEL on its first hold
(with the floor's `available_gb`, null when that figure was unreadable, and
absent when the hold was decided at the pump, which reads none), no GB figures in its
label, detail or event (the figure cleared the floor, so any "N GB free, needs
M GB" pair would contradict the verdict), and WARNING once per level
(`_pressure_hold_level`), DEBUG for every later row. When the hold stops
applying, the WARNING latch resets, so the next episode warns again; each row
keeps its wait clock (see the bound above) unless it is older than the prune
age (`_PRESSURE_HOLD_PRUNE_FACTOR` times the bound).

An unreadable figure on macOS is reported like Linux's: a WARNING and a
`memory_check_unavailable` SEL row, and the start proceeds on the fail-open
contract.

The level never rewrites the figure and never moves the posture, which stays
figure-based. It is reported beside them: `summary_lines` (the
`resource_status` tool, whose guidance then names the pressure instead of
"heavy work is fine"), the `[RESOURCES]` context line, and the diagnostics
bundle. The eager-spawn admission (`chat_runner._pressure_hold_blocks_prewarm`)
admits no new speculative pre-warm while the hold applies, and leaves the idle
ones already live to the host bands; the re-probe after a
pre-warm registered does not read it. The cron, runner and adaptive-controller
gates do not act on the level (the probe they share reports it).

The adaptive growth bound is the user's ceiling itself (`user_max_concurrent`),
with no static host prediction under it: the controller climbs on live pressure
signals and this spawn-time reservation queues what the host cannot absorb yet.
Auto-sizing's startup formula still sizes the AUTO ceiling from memory. See
[adaptive-concurrency](adaptive-concurrency.md#growth-ceiling-the-users-pin-judged-live).

## APIs

### `SubagentManager.__init__(sessions, ctx_builder, on_done, max_concurrent, ...)`

The four leading parameters are shown; the constructor takes further keyword
parameters (turn limit, timeouts, callbacks, the approval hooks).

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

`reconfigure` is the watcher's entry point and is `async`: resolving the auto cap
checks that host memory is readable, which is filesystem I/O, so it is resolved
off the loop and handed to the synchronous `apply_limits`, which does the whole
apply.

Every limit the constructor copies out of `config.json` is a LIVE value: a
reload that touches any path in `SubagentManager.LIVE_CONFIG_PATHS` re-derives
ALL of them from the new config, whichever writer produced it (dashboard,
`kirocrew config set`, `$EDITOR`). No gateway restart is needed for:

| Config path | Manager field | Normalization (same as the constructor) |
|---|---|---|
| `agent.max_subagents`, `agent.subagent_auto_max`, `agent.spawn_min_memory_gb`, `agent.subagent_mem_buffer_pct`, `agent.subagent_cost_gb`, `session.pool_size` (`SIZING_CONFIG_PATHS`) | `_user_max_concurrent` (and `_max_concurrent` re-clamped) | `resolve_max_subagents(cfg)` — explicit pin (floored at 3) or the `subagent_auto_max` auto ceiling. The last four size the cap only while `spawn_min_memory_gb <= 0` disables the floor (the auto cap is then `compute_memory_sized_parallel_cap`), but all six are watched so a reload that turns the floor off or on, or moves a memory term under a disabled floor, re-resolves the cap; a reload that moves none of them keeps the cap without a memory probe |
| `agent.subagent_max_turns` | `_default_turn_limit` | `int` |
| `agent.subagent_timeout_secs` | `_default_timeout` | `0` keeps `_TIMEOUT_SECS` |
| `agent.subagent_stall_idle_secs` | `_stall_idle_secs` | `0` keeps `_STALL_IDLE_SECS` |
| `agent.subagent_spawn_stagger_secs` | `_spawn_stagger_secs` | floored at `0.0` |
| `agent.subagent_result_ttl_secs` | `_result_ttl_secs` | `int` |
| `agent.subagent_queue_max_wait_secs` | `_subagent_queue_max_wait_secs` | `max(0, int)` |
| `agent.completion_keep`, `agent.completion_keep_chars` | `_completion_keep`, `_completion_keep_chars` | via `update_completion_keep` |

`agent.approval_mode` is NOT in this table on purpose: it is boot-only (schema
`restart=True`), because every channel dispatcher resolves it once at start
together with the CLI `--approval` override, and one consumer taking it live
while the others keep the boot value would make the UI's "restart required"
true for some tool calls and false for others. `_global_approval_mode` stays the
value cached at construction.

The applier (`reconfigure`) resolves the cap with `asyncio.to_thread` because the
readability check reads `/proc/meminfo` / cgroup files, then calls
`apply_limits(cfg, max_concurrent=cap)`; a direct `apply_limits(cfg)` resolves
the cap inline. A resolution failure keeps the current cap and logs at WARNING.

Every consumer reads the manager attribute at the point of use — the admission
gate (`_should_stagger_queue_impl`, `_drain_queue_impl`), the run timeout
(`asyncio.wait_for(..., timeout=_default_timeout)`), the reaper's TTL prune
(`_result_ttl_secs`) and stall detector (`_stall_idle_secs`), the parentless
approval policy — so the assignment is the whole apply. Cap-change
invariants:

- **Raising the cap admits queued spawns.** `apply_limits` calls
  `_notify_cap_raised()` when the new cap exceeds the old one; it drains a
  non-empty queue through the pump, which re-checks the gate and honours the
  stagger interval, so freed capacity fills one start per
  `subagent_spawn_stagger_secs` (default `0.25`), never in a burst. The interval
  decides fill RATE, not concurrency: a cap of N fills in N x
  `subagent_spawn_stagger_secs`, so the stagger is never the bound on how many
  run. It is a smoothing interval and not the memory guard — every spawn
  still has to leave `spawn_min_memory_gb` free after its start and clear the host budget, and the adaptive
  controller cuts the cap on corroborated pressure. Raise it on a host (or
  against a provider) where overlapping cold starts, not the cap, are the
  bottleneck.
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
- **The in-startup population is bounded separately from both the cap and the
  stagger.** The cap bounds how many RUN, the stagger bounds the RATE of
  starts, and neither bounds how many admitted agents are still STARTING:
  one start was admitted per interval however long each took, so under slow
  starts (a dedicated process per `model` / `reasoning_effort` override, a
  queue at the `SessionStartGate`, a throttled handshake) a wide fan-out piled
  dozens of agents into startup at once and the fixed 120s startup watchdog
  reaped healthy ones as `Failed to start within 120s` (measured ~50% loss on a
  120-item wave against ~2% at 24-45). `_should_stagger_queue_impl` therefore
  has a third clause: `_startup_population() >= _startup_cap()` queues the
  spawn like a full cap does, and `_drain_queue_sync_impl` holds its pick under
  the same test (after the resume grants -- a resume is not a start). The
  population is `_in_startup`: `_exec_started` set, `turns == 0`, no `_pid`,
  no `_first_stream_started`, not done or reaping -- the watchdog's own shape
  -- plus `_startup_reservations` (a `ClaimPoint` reserved but not yet
  re-entered and registered, whose re-entry skips this gate). An agent parked
  at the spawn-approval prompt is NOT counted: it starts nothing, and counting
  it would let N unanswered prompts stall every spawn on the host (pinned by
  `test_ignored_prompts_do_not_block_an_unrelated_auto_approved_spawn`). Its
  RELEASE is what is bounded: `tool_approval:bulk_trust` / `bulk_yolo` resolves
  every pending prompt in one pass, so `_spawn_with_approval` does not go
  straight to `_run` -- it awaits `_admit_released_start`, which appends a
  resident entry (`_resume_id` + `_startup_release` + the waiter's info, so
  every unstarted-spawn scan already leaves it alone) to the EXISTING queue
  and waits on `info._start_release`. The pump's first phase
  (`_release_admitted_start`, before the capacity check because the run
  already holds its slot, before the resume grants because it is older) meters
  ONE released start per pass under the same stagger and
  `_startup_population() >= _startup_cap()` test a fresh spawn faces, stamps
  `_last_spawn_ts`, wakes the waiter, and re-arms at the stagger boundary. It
  answers `"released"`, `"held"` or `""` (none waiting), and the pass continues
  on every answer: a resume waits on a lane slot, never on the startup bound or
  the stagger, so the resume grants run whether a start was released or is
  being held (pinned by `test_a_held_release_does_not_starve_a_queued_resume`);
  the fresh-spawn pick applies the same two checks itself, so a hold here holds
  it too. `_admit_released_start` answers `"admitted"`, `"admission_closed"`
  or `"ended"`, and `_spawn_with_approval` treats each for what it is: an
  admitted start is recorded as spawned (`_log_spawned`, at admission and never earlier -- a start
  refused or ended while waiting is never counted) and run; a start whose
  prompt resolves while gateway admission is closed is refused at release
  (`spawn rejected: the gateway closed admission ...`, `rejected` /
  `admission_closed` audited, parent announced) -- the same guard every
  registration in the admission package sits behind; a start ended by a user
  stop or a reap while it waited writes nothing, since `_force_reap` owns that
  record (a stop is neutral, a reap names the interrupted wait) and its
  announce -- such a start was never recorded, so it has no agent folder and
  (like a declined prompt, or an unapproved spawn across a restart) no orphan
  entry, and its tombstone
  follows the run's own memory mode (`_write_tombstone`), so a non-persistent
  one leaves nothing on disk (pinned by
  `test_a_temporary_start_reaped_while_waiting_leaves_nothing_on_disk`); a stop or reap while waiting wakes it with False and drops
  the entry, and a slow self-re-arming re-pump (`_RELEASE_REPUMP_SECS`) backs
  the edge-driven wake. Pinned by
  `test_bulk_approval_cannot_release_more_than_the_startup_cap` and
  `test_a_released_start_that_is_stopped_while_waiting_never_runs`, both
  driving `spawn()` through the real `_spawn_with_approval`. The watchdog
  (`_is_startup_stalled`) stays blind to a parked or released-waiting agent: a
  human prompt has no deadline. The bound is `_startup_cap()` =
  `2 × session_start_concurrency` (`_STARTUP_CAP_GATE_ROUNDS` rounds of the
  gate's width) clamped to `[1, _max_concurrent]` -- so `1`, not `0`, at a cap
  of `0`. With `session_start_concurrency = "auto"` the width `_startup_cap`
  reads is an in-memory read of the gateway's one cached host sizing
  (`session_start_sizing.cached_session_start_concurrency`, taken in a worker
  thread after the socket binds), and the floor `2` until that reading exists;
  it never probes the host on the event loop. It is tied to the GATE and not to the cap because the gate is the
  one resource every start in startup contends for: `session/new` runs under
  `G` permits, so at most `G` starts progress at once and every other admitted
  start is a spawned process or a claimed slot holding a place in the gate's
  queue. That queue time is not charged to the startup deadline (the watchdog's
  clock pauses at queue entry, `_gate_wait_mark`, and resumes at acquisition,
  `_gate_exit_reset`), so the queue's length alone reaps nothing -- what bounds a
  start parked there is the paused total's own cap, `_START_QUEUE_MAX_SECS`,
  below the default `subagent_timeout_secs`, so a saturated start ends with a
  terminal that names the queues rather than its turn deadline (a run whose
  `subagent_timeout_secs` is at or under the cap ends on its turn deadline
  first); the bound decides
  how much of the running cap may sit in startup contending for `G` permits.
  `2G` is the smallest value that never idles the gate (one round holding, one
  admitted to take over) and admitting more buys no starts -- the gate serves
  `G` per round however many queue -- only a longer queue of idle admitted
  starts. A cap-derived term (`ceil(cap / 4)`, say) would admit 16 into startup
  at cap 64 against a 2-permit gate -- seven rounds queued for two permits --
  which is why the bound is gate-tied and never cap-tied. There is deliberately
  no config key: `2G` is both floor and ceiling of the useful range (below
  idles the gate, above adds only idle admitted starts), so an override could
  only make it worse, and `agent.session_start_concurrency`
  is already the operator's lever -- the bound tracks it. No second queue and
  no timer: a held
  drain arms nothing, because every edge that frees a startup slot already pumps --
  `_note_startup_progress` at the runtime PID record (`_run_inner` on the
  dedicated path, `_bind_shared_handle` on the shared one) and at the first
  answer on the run's own session (`_leave_startup`), and the slot-release drain on every terminal, including the
  watchdog's reap of a wedged start, so a wedged population cannot hold the
  queue past its reap. Pinned by `test_subagent_startup_pressure.py`.
- **Lowering the cap cancels nothing.** In-flight runs keep going; the gate
  simply admits no new spawn until `_running_count` drains below the new cap on
  its own.

The advisory figure the `spawn_run` tool description advertises
(`mcp_tools/spawn.py::schemas`) prefers the execution cap IN FORCE
(`resource_status.adaptive_exec_cap`, the in-process controller registry) and
falls back to `resolve_max_subagents(KiroCrewConfig.load())`, re-resolved on
each tool listing and LABELLED as a ceiling, so the advertised and enforced
numbers agree after a write and a ceiling is never presented as the live cap.
The registry read is the only live path here: `schemas()` also runs on the
gateway's own discovery cycle, where a loopback request would dial the gateway
from inside the gateway. In a tool server the registry is empty, so the model
sees the labelled ceiling and is pointed at `resource_status` for the live cap.

### `set_effective_cap(cap | None) -> int` — the adaptive-controller seam

The resolved user cap is a **ceiling**, not the live value. Two fields:
`_user_max_concurrent` is what `apply_limits` writes (the pin or the
`subagent_auto_max` auto ceiling); `_adaptive_cap` is what the adaptive concurrency controller
([`adaptive-concurrency.md`](adaptive-concurrency.md)) writes through
`set_effective_cap`. `_max_concurrent` -- the attribute every admission read
site consults -- is always `min(_user_max_concurrent, _adaptive_cap)` (or the
user cap alone when no bound is set). The two writers never touch each other's
field: a config raise cannot lift the adaptive bound, a config cut below the
bound clamps it, and the controller never writes `config.json`. `None` removes
the bound; `0` admits nothing new (in-flight runs finish; nothing is cancelled)
-- the controller no longer writes it, because its execution cap has no pause.
A raise goes through the same `_notify_cap_raised` a config raise uses: the
staggered queue drain, plus the runner lane's `pump()` (see the cap-change
invariants above). Neither raise site grants past the new cap.
`reconfigure`'s "sizing unchanged" path re-applies `_user_max_concurrent`, not
`_max_concurrent`, so a reload can never shrink the ceiling to the bound.
`user_max_concurrent` exposes the ceiling; `max_concurrent` stays the effective
value the gate enforces and the dashboard's capacity error reports. A fresh
gateway's controller applies the ceiling itself (`agent.adaptive_initial` is
inert) and cuts beneath it only on failing work.

### `spawn(task, parent_session_key="") -> SubagentInfo | None`
Spawns a background agent. Accepted running or queued work returns a stable
`SubagentInfo`; a legacy in-memory capacity refusal can return `None`. Uses atomic
`_running_count` to prevent race conditions. `parent_session_key` tracks the
originating session for completion injection.

Admission order (`subagent_manager/admission/gate.py::spawn_impl`):
1. Policy refusals that leave no durable trace: empty task, memory identity,
   `cwd` outside `subagent_cwd_allowed_roots`, spawn governance, and the parent
   agent spec's `toolsSettings.subagent.availableAgents` (see § Parent agent
   spec allowlist below).
2. For persistent work, **persist** the row in the task store (write-before-ack; see § Durable task
   queue). A store write failure is a refusal with
   `error_code="task_store_unavailable"`; the id is never handed out as accepted.
3. Agent name validation (a failure marks the row `failed`, refused
   `agent_not_found` before any wait). The event-loop callers (`spawn_async`
   and the coroutine pump's dispatch) take both agent-directory scans, app
   ownership and `_validate_agent`, on a worker thread and hand the gate the
   answer (`AgentCheck`, keyed by the agent, the cwd it runs in and the app the
   gate settles on, the captured execution's app winning over the caller's); a
   synchronous caller validates inline. The memory read's re-entry
   (`MemoryReadPoint`) is handed the same answer.
4. Memory floor (`spawn_min_memory_gb`, what must remain after the start's
   price; see *Memory guard*): with a persistent row, **defer** (row stays
   `queued`, `next_run_at = now + admit_wait_secs`, pump wake-up armed, caller
   gets a `queued` id); without one, **queue** in the in-memory window, not
   eligible until the same admit wait passes. Either is re-checked sooner on a
   memory wake, and a root start the per-lane memory share holds waits the same
   way (*Memory waits: event wake and the per-lane share*). The memory posture tier
   (`resource_critical_gb`, `cached_admission_check`) is NOT consulted for
   spawns: the floor equals it at defaults, so a host admission has filled to
   the floor would read `critical` and hold every start at the line the floor
   guarantees. Cron still defers its firings on posture (`agent.admission_gate`).
5. Capacity / stagger gate, with the child reserve and the macOS kernel
   memory-pressure hold (see *Memory guard*): queue (persistent window/store-only
   or restricted memory-only) or proceed. A start already waiting on the floor
   (step 4, `low_memory`) is not also held: the floor's verdict outranks the
   kernel's, and that wait carries no pressure clock. Then the **atomic
   claim** for persistent work (`admitted`, generation++). A row cancelled while it waited fails the
   claim here and is never started. Every claim the store took then re-reads
   its row (`taskq_claim_still_current`: still `admitted`, same generation, our
   lease; a generation-0 claim of a row the store never saw has nothing to
   re-read and skips it:
   `test_taskq_admission_integration.py::test_a_claim_of_a_row_the_store_never_saw_still_starts`)
   and checks the loop for a stop recorded while the claim was in flight
   (`_stopped_while_claimed`: a stopped or ended `_agents` record, which is what a
   queued-stop report installs). Either one refuses the start, so a row Stop all
   cancelled between the claim and the registration ends stopped and never
   starts. Nothing awaits between these checks and registration. If that post-claim store step is
   unavailable, `_retained_claims` keeps the admitted generation and its reserved
   slot, and the next pump settlement pass retries it before ordinary refill.
   Registration consumes the reservation; a durable refusal releases it.
6. Register, take the slot, `starting`; then the approval branch below.

**Every wait is labelled with the verdict that caused it.** Steps 4 and 5 set
`SubagentInfo.queued_reason` on the `queued` record they return — one of the
kinds defined in the leaf module `kiro_crew.subagent_wait_reasons` (re-exported by
`kiro_crew.subagent`; the channel command layer reads them from the leaf so it never
imports `kiro_crew.subagent` at runtime): `QUEUED_REASON_LOW_MEMORY` (step 4,
with `queued_reason_detail` = the gate's own sentence, the same text the task
store's `deferred` event records), `QUEUED_REASON_MEMORY_PRESSURE` (step 5 while
the kernel pressure hold keeps the start, with its figure-free detail),
or `QUEUED_REASON_CONCURRENCY_LIMIT` (step 5 otherwise: a taken slot or the
stagger tick). There is no pause kind (`adaptive_cap_zero` is gone): the
adaptive controller never takes the execution cap to 0, so a cap of 0 a caller
pinned is an ordinary capacity wait; cron never used the kind. The label is a report of a decision already
made; no gate reads it back. Two consumers:

- The advisory `subagent_queued` lifecycle event (`_emit_queue_depth`) carries
  `reason` and, for the low-memory kind, `available_gb` /
  `required_gb` beside `queued`; the memory-pressure kind carries no figures.
  The label is remembered per parent (`_queue_wait`) so the drain's,
  the claim path's, the cancel paths' and the terminal reports' requests —
  which carry no verdict of their own — keep it. A frame at depth 0 is the
  bare `{"queued": 0}`, and a 0 read no request overlapped forgets the label.
  A 0 read that a request overlapped (published once the withhold cap passes)
  keeps it: that request may be the verdict which wrote the label, for a row
  the read predates, and the burst's next read answers it.
  The count is pushed, not polled, and the client otherwise resets it only from
  a reconnect's snapshot, so the authoritative depth is re-published whether or
  not it changed at every point a wave settles: every terminal report of a
  run that started, every queued-stop report (the record of a waiting row
  stopped before it started, whichever path removed the row — so that
  terminal adds no request of its own), every granted resume (`_resume_publish`:
  the resume entry was never counted, but the wait that ended — a dependency, a
  permission answer, an input — has no terminal of its own), and every
  `cancel_for_parent` (Stop all) that stopped nothing. A
  frame the client missed or received out of order therefore cannot leave "N
  waiting to start" on the card once the wave has settled.
  **The emit is coalesced per parent into a burst.** At most one read is in
  flight for a parent; a request while one is marks the burst to read again
  instead of reading itself. A read answers every request made before it
  started: it is queued on the store's writer thread behind every write those
  requests followed (a posted write is queued by the call that posts it,
  `_post_store_write` → `TaskStore.post`, not by a task that runs later), and
  it takes the window and the exclusion set as they stand then. A read a
  request overlapped is therefore not published, and the burst reads again —
  unless it has withheld frames for `_QUEUE_DEPTH_MAX_WITHHOLD_SECS`, when it
  publishes the read and then reads again. A request while a frame is being
  sent is answered the same way. The last frame of a burst is always read after
  its last request; intermediate frames are withheld by design. Stop all therefore
  costs about one frame however many rows it stops; a
  teardown and the pump ask one row at a time and can cost a frame per row. A
  start the drain popped asks when it is popped and marked dispatching (which
  already leaves it out) and again at its registration, so a pop read the store
  could not answer is corrected by the start itself, not by the delayed
  re-read. A frame's `batch_id` is the wave's when every request it answers named
  the same one, and empty when they named several; its count is parent-wide
  either way.
  **The count is unstarted spawns only:** window entries that are not a
  resident run's resume entry (`entry_is_resident_resume`; an
  approval-released `_startup_release` start still counts) plus the store's
  waiting rows outside the window (claimable rows and `admitted` ones no run is
  registered for, such as a claim retained across an outage: the same
  "accepted, no run yet" definition as `taskq_overflow`), less a row the pump
  has popped and not yet claimed (`taskq_chip_excluded_ids`, the refill's never-claim set with one
  part narrowed). A row a `spawn_async` caller is still admitting is left out
  until the gate has queued it (`_admitting_waiting`, marked by that call as
  the gate returns the queued record and before it awaits anything): the
  refill must never claim it, but once it is deferred or behind the cap it is
  waiting, and its own labelled request counts it. A `recovering` row is not
  counted either, on disk (`count_pending(include_recovering=False)`) or
  hydrated into the window (`WINDOW_ENTRY_RECOVERING`, set from the row's
  state; it is also `spawn`'s keyword, so a gate that re-queues the drained,
  still-unclaimed row puts the mark back on the entry it appends, pinned by
  `test_a_restart_survivor_the_gate_requeues_stays_off_the_card`): it is a
  run that had started before its owner was lost (a gateway restart) and is
  being rebuilt, which the queued
  listing names "waiting to resume", and counting it would put "N waiting to
  start" on the card after every restart. The pending-work guards keep counting it:
  it is still work the parent is owed. The count answers how
  many spawns wait to START; the lanes API's census of what each lane holds
  counts a resident run's resume entry as that lane's waiting work, a
  different question. **A store that cannot be read publishes nothing** and
  keeps the label (a false 0 would clear a card whose rows still wait), and
  arms the parent's one delayed re-read, `_QUEUE_DEPTH_RETRY_SECS` later, up
  to `_QUEUE_DEPTH_RETRIES` in a row; a frame published first disarms it, and
  a fresh request restores the budget.
  One label per parent, last writer wins: it is the verdict on the most recent
  row the gate judged for that parent, not a per-row ledger. A parent holding a
  memory-deferred row and then a capacity-queued one shows `concurrency_limit`
  until the deferred row is re-checked — which the pump does on the next memory
  wake, and at the latest within `admit_wait_secs` (default 30 s), re-labelling
  it or starting it — so the label is never more than one admit wait stale. This is accepted: the
  alternative is a per-row label reconciled on every emit, for a chip that
  states a count, and a stale-by-one-wait label always reads as a wait that
  does exist for that parent.
  An event without `reason` (nothing labelled, or an older gateway) leaves the
  dashboard on its default "queued behind the concurrency limit" text; the
  memory, memory-pressure and adaptive kinds render their own sentence
  (`website/src/pages/chat/subagentQueuedReason.ts`), visibly on the run card
  and the composer chip as well as in their tooltips, and with a figure-less
  sentence when the event names the kind but not the numbers. The dashboard
  keeps the count and its parsed reason per parent slot
  (`chat.subagentQueued` / `chat.subagentQueuedReason`) in the
  `sseSubagentQueued` reducer (`website/src/store/chat/subagents.ts`), which
  rewrites or clears both on every frame, so a count never sits under a stale
  reason.
  **The slot list carries the published depth, so a missed frame heals.** Every
  frame goes out through `_fire_event`, which first records its `queued` in the
  manager's `PublishedQueueDepths` (`subagent_manager/published_depth.py`): the
  last published depth per parent, never a fresh read, so the slot list needs no
  store read on the loop and cannot disagree with the frame stream. 0 is not
  stored, a parent end forgets its entry (`cancel_for_teardown`), and the table
  is capped at `MAX_PUBLISHED_PARENTS` (oldest publisher dropped; it reads 0 until
  its next frame). A held entry is re-derived from state: when a child of a
  parent with a non-zero entry starts or ends (`subagent_spawn`,
  `subagent_done`), `_fire_event` asks the parent's burst for a heal read, which
  publishes only a depth that differs from the held entry, unless it answers the
  parent's armed re-read after an unreadable store: that re-read is disarmed by
  the heal's read, so the heal publishes the frame it owed, including a new wait
  label at an unchanged count. A path that pops or
  ends a row without its own emit therefore publishes the real count at the next
  lifecycle edge instead of leaving a count the slot list (and a reload's first
  `slots` frame) would keep re-sending, and a path that did emit is not
  answered with the same frame twice. `serialize_slots` puts it on each row as
  `subagents_queued` (an int, 0 when none) beside `subagents_running`, routed the
  way the frames are: each entry lands on `subagent_event_slot(parent)`, and
  entries sharing a tab sum, so a cron tab (`cron-<job>`) reports the runs that
  publish under `cron:<job>:<run>` or `cron:<job>:<agent>`, not its linked
  `cron:<job>` key alone. The gateway schedules its debounced slots push on
  `subagent_queued` as it does on spawn/done. The dashboard applies the field on
  every `slots` push (`reconcileSubagentQueuedFromSlots`, from
  `hooks/websocket/slotList.ts`) to every row in the list, ahead of the
  identical-frame skip (a push equal to the previous one still corrects a count
  a frame moved in between), so a client holding a stale count — on screen or a
  background session — is corrected by the next push. A row without the field (a
  `slot_patch`, an older gateway) leaves its count alone, and the wait label is
  kept while the count is non-zero. Only the pushed frame reconciles, never the
  `fetchSlots` GET: the push is ordered with the `subagent_queued` frames on the
  same socket, and a GET answer can land after a newer frame.
  **A queued child is never Working.** The board's Working lane and the row's
  live-work checks read `selectSidebarStartedSubagentCounts` (running, tool and
  pending cards), never the queued count, so a parent is Working only through its
  own turn (`running`, which stays true while it blocks in `spawn_sub_agents`) or a
  started child. A row whose children are all queued shows a static
  "Waiting for free RAM" badge for a memory wait (`isMemoryWait`: `low_memory`,
  `memory_pressure`) and the queued count otherwise, with the wait's sentence as
  its tooltip. `selectSidebarSubagentCounts` (started plus queued) stays the
  reading for "this session still owns sub-agent work": the row's count label, the
  turn-done chime, stale collapse; the ⌘W close gate confirms on queued children on
  their own term.
- `POST /api/spawn` answers the DEFERRED kinds (`DEFERRED_QUEUED_REASONS`)
  with `status: "queued"`, `reason` and `reason_detail` under the same `id`;
  every reader of that answer relays it: `spawn_run` prints a
  `Queued N subagent(s). Not started yet: <detail> …` group apart from the
  `Spawned` group (same `N subagent(s).` marker and `  <id> (<agent>): <task>`
  lines, which is what the dashboard's inline run card parses — a queued-only
  wave still gets its card), `spawn_sub_agents` appends a
  `{"status": "queued", ...}` record for a member that had not started when its
  wait ended (and reports it only there, never also as an error), `kirocrew spawn run` prints `Queued subagent <id> …`, and the
  channel `spawn <task>` keyword (`messaging/commands.py`) replies
  `⏳ Queued subagent …` with the reason instead of `🚀 Spawned subagent …`, and
  `⚠️ Subagent … was not started: <error>` for a refusal (a terminal record, not
  None). A
  `concurrency_limit` wait keeps `status: "spawned"`: it is the ordinary wave
  shape and clears within seconds. Neither the admission verdicts nor the memory
  pricing (`_startup_memory_reserve_gb`) are touched by the label.

### Parent agent spec allowlist (`toolsSettings.subagent.availableAgents`)

kiro-cli defines `toolsSettings.subagent.availableAgents` for its own built-in
`subagent` tool: a glob list of the agents an agent may spawn, and **omitting it
allows all**. Kiro Crew's sub-agents come through `spawn_run` /
`spawn_sub_agents` rather than that tool, so the gate honours the same
declaration itself (`subagent._vet_parent_available_agents`, called from
`spawn_impl` right after `_vet_spawn_governance`):

- **Parent** = the kiro agent template the CALLING session runs as, read from
  its execution record (`read_session_execution(parent_session_key).template_id`)
  and resolved in the user-level agents directory the way `agent_spec_path`
  resolves a name (declared `name` wins, filename stem is the fallback; two
  specs declaring the name are both applied, tightest-wins). Read with one
  fresh walk through the hardened reader (`_scan_parent_spawn_allowlists`),
  memoized per directory in an `AgentsDirMemo` pinned to
  `agents_dir_revision` (names, mtime AND ctime, size, inode, mode, the
  in-process spec generation, nothing younger than the racy window) — a
  blocking read, so it never runs on the gateway loop: the
  event-loop entry points resolve `subagent.parent_spawn_policy` (parent
  template + declaration) through `asyncio.to_thread` and hand it to
  `spawn_impl` as `_parent_spawn_policy` — `spawn_async` derives the template
  from the parent record it already read off-loop, `/api/spawn` from the
  `parent_execution` it admitted (a parentless request passes `("", ())`),
  and the durable pump re-resolves it off-loop before each drained row's
  re-check — while the synchronous `spawn()` computes it inline for callers
  that are not on the loop. The policy a row was admitted under is NOT stored
  with it: a queued row waits on capacity, so the wait is unbounded in time
  and the declaration may be tightened while it waits. Every drain reads it
  fresh -- the durable pump off-loop before its re-check, the in-memory
  queue's synchronous drain through the gate's inline fallback, a memo-pinned
  read (`_PARENT_ALLOWLIST_MEMO`) that is a `scandir` in the ordinary case and
  a full parse only when the memo declines to pin. Neither catalog snapshot is
  used: `parsed_agent_specs` revalidates on
  entry names and mtime alone, so an mtime-preserving rewrite of the parent's
  spec (`cp -p`, `rsync -t`, a restore) would serve its previous, permissive
  allowlist for ever; the non-blocking `cached_agent_specs` serves empty rows
  on a cold cache, and an empty read would admit a spawn the declaration
  forbids. When the revision cannot prove freshness (Windows, a symlinked
  spec, an entry younger than two seconds) the walk simply runs uncached.
- **Target** = the EFFECTIVE child template (`execution.template_id`: explicit
  `agent`, the inherited parent template, or a `crew=`/`target_member` member's
  `kiro_agent`), matched with `fnmatch.fnmatchcase` against each glob. Only
  when the VERIFIED calling app (`execution.app`, bound by the SpawnSDK and
  re-proved by `_validate_app_agent_ownership`) is spawning its own
  `<app>--<name>` is the bare `<name>` tried too — the name the app's own spec
  lists. A `--` inside any other installed name is just a name: an installed
  `rogue--reviewer` does not satisfy a `reviewer`-only list.
- **Unreadable parent spec = refuse.** The hardened reader folds an
  unparseable, oversized, broken-link or sensitive-symlink spec into "no
  spec". kiro-cli resolves a name by its DECLARED `name` first, under any
  filename, so when no readable spec declares the parent's name and any spec
  file in the directory (`iter_agent_spec_files`, minus the `._` AppleDouble
  sidecars the reader rejects by design) did not parse, that answer is UNKNOWN
  — the declaration may be inside the very file that did not parse — and the
  gate refuses with the same `agent_not_available` code, logging the
  unreadable file, rather than failing open. A parent whose declaration is
  readable is never refused by an unrelated broken file. The accepted cost: a
  parent with no user-level spec at all (a project-scope or edition agent)
  cannot be told apart from one whose declaring spec broke, so it is refused
  too while any spec file is unreadable, and admitted again once the file is
  repaired or removed. An agents directory that exists but cannot be scanned
  (probed with `os.scandir` before the snapshot read, which folds a walk
  failure into "no specs") refuses too, while an absent directory declares
  nothing; and so does a parent session whose execution record exists but
  cannot be read (`ParentRecordUnreadable` → policy `("", None)`): an
  unreadable parent is not a parent without a spec. A caller with no session,
  or a session with no record, is parentless and unrestricted.
- **When it bites**: ONLY when the parent's spec declares the key. No parent
  (a direct `POST /api/spawn` with no session, a test), no spec for the
  parent, or a spec without the key is "allow all" — every session whose agent
  never wrote the key keeps exactly the admission it had. A declared value that
  is not a list is an EMPTY allowlist (declared, nothing allowed).
- **`trustedAgents` is not read.** Upstream it means "run these sub-agents
  without permission prompts"; treating it as an allowlist would refuse spawns
  the operator never meant to forbid.
- **Intersection, not replacement**: the governance gate
  (`capabilities.spawn.scopes.agents`) runs first and both must admit. This
  check only narrows what the spec grants; it never widens a governance denial.
- **Refusal**: a policy refusal before the row is persisted, audited as
  `outcome="denied"`, with `error_code=agent_not_available`
  (`subagent.AGENT_NOT_AVAILABLE_CODE`) and prose naming the parent, the key
  and the allowed globs (grammar-checked, redacted, bounded like every rendered
  roster). `spawn_run` short-circuits the rest of a wave on this code exactly
  as it does on `agent_not_found`.
- **Not covered yet**: a parent whose spec lives only in a project's
  `.kiro/agents` (the gate does not know the parent session's project dir);
  its declaration is not honoured. There is no warning when a spec declares
  the key but the session's `tools` lack the kiro-cli `subagent` tool.

The spawn tools advertise the same view: `spawn_run`'s "Valid names right now"
roster and `spawn_list`'s "Available agents" line drop the names the parent's
declaration forbids and say so
(`mcp_tools.spawn._parent_allowlist_filter`). That is advisory — the tool
server resolves its own session's template best-effort and filters nothing
when it cannot — and the gate above is the decision.

### Kiro Crew's own generated specs are not sub-agents

Two kinds of spec Kiro Crew writes into the user-level agents directory — the
only place kiro-cli loads a spec from — exist for its own machinery:
the side turn's derived read-only spec (`<agent>--readonly`, recognised by the
owner marker its `description` opens with; `side.md`) and a skill-view alias
(`kirocrew-skill-view-*`). `agent_discovery.is_internal_agent_spec` is the one
predicate for both. A roster that offered one sent the model into a spawn that
failed: the read-only spec is written per side turn, and a kiro-cli that listed
its agents before then refuses the mode. So:

- every spawn roster leaves them out — `spawn_run`'s parameter roster,
  `spawn_list` (whose listing is otherwise unfiltered) and the unknown-agent
  refusal's `available:` list;
- `_validate_agent` refuses a named one with `agent_internal`
  (`subagent.AGENT_INTERNAL_CODE`), naming the base agent a read-only spec was
  derived from when that base is on offer. It is refused, not mapped onto the
  base: the base carries the grants the read-only copy strips, so a silent
  substitution would run the caller under more than it named. A PROJECT agent
  declaring the same name is the user's own — kiro-cli resolves it first — and
  is accepted. `spawn_run` stops the rest of a wave on this code, as it does on
  `agent_not_found`;
- an app does not own a spec derived from one of its agents
  (`<app>--<agent>--readonly` shares the prefix): both the SpawnSDK's
  ownership set and `_validate_app_agent_ownership` leave it out.

The match is on the owner marker, never the name alone: a hand-authored agent
that merely ends in `--readonly` stays listed and spawnable.

Spawn flow (`subagent_manager/admission/gate.py`), first matching rung wins:
1. **YOLO mode**: skips approval, runs immediately
2. **Per-spawn `approval_mode="auto"`**: skips approval, runs immediately
3. **Parent trusted**: parent session has `approval_policy="auto"` (set by
   dashboard trust toggle) → skips approval, runs immediately
4. **`hooks.auto_approve_subagent_spawn`**: skips approval, runs immediately
5. **`on_spawn_approval` configured**: enters `_spawn_with_approval`, which
   requests interactive approval through that callback. The wait is the
   callback's own: the dashboard gate allows 2 hours for an attended prompt
   (`DashboardState._APPROVAL_TIMEOUT`) and 180 seconds for a background run
   (`_BACKGROUND_APPROVAL_TIMEOUT_SECS`). Timeout or rejection frees the
   concurrency slot.
6. **Otherwise**: rejected with `spawn rejected: no approval mechanism configured`.

**Channel-side approval delivery (issue #2381 item 1).** The single host-wide
`on_spawn_approval` callback (built in `slack/gateway.py`) consults a
channel-neutral delivery seam (`messaging/spawn_approval_delivery.py`) FIRST,
given the spawn's `parent_session_key`. A channel dispatcher registers a delivery
hook keyed by its channel namespace (`register_channel_delivery("telegram", …)`);
the seam resolves the hook whose channel owns the parent session
(`messaging.link.channel_namespace_of`). A hook that returns `True`/`False` is the
user's in-channel decision; `None` falls through to the pre-existing
Slack-DM/dashboard gate, which still raises `SpawnApprovalUnreachable` when no
surface is attached. A hook answers `None` when no hook is registered for that
channel, when it cannot address the parent key (a `unified` dm_scope key) or the
originating conversation is no longer an authorized destination, when it could not surface the prompt — a send that raised, or a refused send its client
reports by returning no message id — and when its deny-by-default wait elapsed
unpressed after the channel's governance ceiling began to deny it: the hook asks
`spawn_approval_delivery.unpressed_wait_answer`, which answers `False` (a real
deny-by-default) while the channel is still permitted and `None` once it is not,
so an unanswerable prompt never becomes a refusal in the operator's name (see
[messaging](messaging.md)). Telegram implements the hook over its existing
Approve/Deny/Trust inline keyboard (`TelegramDispatcher.deliver_spawn_approval`):
the press resolves through the same `on_callback` `a:` path as a tool approval, so
**Trust** grants parent-session trust via `add_trusted_session` and a later spawn
from that session is auto-approved by the parent-trusted rung. Discord implements it
too (`DiscordDispatcher.deliver_spawn_approval`), over its existing Approve/Deny
buttons on the same `on_interaction` `a:` path, with one difference: no Trust rung
(standing spawn trust is granted from the dashboard). Consulting the channels
governance ceiling (`channel_inbound_permitted`) before anything is armed is not a
Discord peculiarity but a requirement of the seam: every channel's press path drops a
non-reject press under a governance deny, so a prompt posted into a denied channel
can never be answered and its wait hands the gate a deny-by-default nobody pressed.
The seam reads it once for every hook, before invoking one. Discord adds a single
re-read of its own on the direct route, after the peer's DM channel is opened: that
open is a full round trip inside the hook, so the seam's answer can go stale across a
gap the seam cannot see. The seam is in-memory
only (dies with the process); each hook is registered on its channel's startup and
unregistered on client shutdown. The per-agent `auto_approve_spawn` rung (issue #2381
item 2) is deferred to #4751/#4693 and is NOT added here.

**Delivery order.** A spawn-approval prompt that reaches `_spawn_with_approval`
is offered to surfaces in this fixed order, and the search stops at the first one
that answers:

1. **Originating channel hook** — the channel-neutral seam above. A `True`/`False`
   return is the user's in-channel decision and is used verbatim; `None` (no hook
   for that channel, or it could not surface the prompt) falls through.
2. **Slack owner DM / dashboard fallback** — the pre-existing gate in
   `slack/gateway.py` (`_interactive_approval`), which races a Slack owner DM
   against an attached dashboard client. A configured-but-unpostable Slack DM and
   an unattached dashboard both count as "no surface".
3. **`#8914` fast-fail backstop** — reached only when neither a channel hook nor a
   Slack/dashboard surface could show the prompt. Rather than park the run at
   turn 0 until the reaper's ~30-minute deadline, the gate (`raise_when_unreachable=True`)
   raises `SpawnApprovalUnreachable`, and `subagent_manager/admission/pump.py`
   turns it into an immediate refusal audited with `reason="no_approval_surface"`.

The backstop keeps two audiences apart on purpose (Design Review r3, PR #8914).
The **operator** WARNING log names every auto-approve rung that would have let the
spawn through (`approval_mode="auto"`, parent-session **Trust**,
`hooks.auto_approve_subagent_spawn`, `hooks.auto_approve_sources`). The
**agent-facing** `info.error` names none of them: two of those rungs are
`config.json` edits — the sandbox seals that file read-only against an in-sandbox
agent shell, but a bypass recipe in the completion event would still hand an
unattended or prompt-injected agent the exact edit to ask the operator for, or to
make from an unsandboxed spawn, that removes its own gate. The agent error stays
terse ("ask the operator to open the dashboard and spawn again, or to enable spawn
auto-approval"). Because the channel hook and the Slack/dashboard gate own the
"which surface was missing" half while the backstop owns the rung list, the
refusal wording stays truthful as channels learn to deliver the prompt: the gate
never names a surface it does not know about, and the fast-fail sentence ("no
surface could show the approval prompt") is only ever emitted once every surface
above has genuinely declined to carry it.

### Tool Approval Cascade

When a subagent's tool call triggers `EVENT_PERMISSION_REQUEST`, the run hands
it to `tool_permission.settle` with the subagent's ladder
(`RunEventCoordinator._permission_policy` in `subagent_manager/run.py`). The
ladder decides it in strict priority order and answers it on the wire:

0. **Spec hooks** — on a turn whose agent spec's PreToolUse hooks gate the call
   (`hooks.permission_pre_tool_block`), a delivered deny, or hooks that cannot
   be read, → reject with the gate's own reason
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
   models the shell's lookup and returns per-command verdicts as it does on
   POSIX, except in two host states that still decline every name grant:
   `windows_lookup_not_modelled` when Windows cannot report where the user's
   Documents folder is, and `inherited_env_can_redefine_programs` (`AMBIGUOUS_ENV`) when a per-user PowerShell profile
   sits at one of the paths derived from it. In those two states a headless
   subagent (no parent `auto` policy, no interactive approver) rejects shell
   tools its allowlist would otherwise grant.
3. **Parent policy** — `parent_policy == "auto"` → auto-approve. Resolved once
   at `_run_inner` start (see the chain below); an active global YOLO folds
   into this snapshot rather than being re-read per event.
4. **Interactive callback** — the per-subagent `on_tool_approval_factory` when
   one is set, else `on_tool_approval` (races dashboard + Slack, 2h timeout)
5. **Deny by default** — none of the above matched → reject

`parent_policy` is resolved once when `_run_inner` starts, using this chain:
1. Read from parent session via `get_approval_policy(parent_session_key)`
2. If empty and the spawn carried `approval_mode="auto"` → `"auto"`
3. If empty and YOLO mode active → `"auto"`
4. If still empty and the cached `KiroCrewConfig.agent.approval_mode` (snapshotted at `SubagentManager` init) is `"auto"` → `"auto"`, but **only when the subagent has no parent session key or its parent session is gone** (`has_session(...) is False`, audited `subagent.config_policy_fallback` with `no_parent` / `parent_gc`). A live parent whose session carries no policy, or a store error, is denied that fallback (`reason=parent_alive_or_store_error`).
5. If still empty and `hooks.auto_approve_subagent_tools` is set → `"auto"`

Step 4 ensures parentless and orphaned subagents (e.g. cron jobs) respect the user's
global approval mode instead of falling through to interactive approval, without
widening a live parent that chose no policy.

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
`rawInput`), two grants still stand. The **unconditional** `parent_policy == "auto"` grant still
auto-approves — the call site reads the hoisted
`AcpEvent.child_unconditional_grant_eligible` property: its decision consumes
no agent-authored event data, only the
arguments remain unverified. For such a child it is tried BEFORE the hook's
grant. The hook's identity-keyed grant (`ToolHookResult.identity_grant`: the
app-own-server rule, or an `auto_approve_tools` pattern matched against
`@server/tool`) also stands, audited as `hook_identity_auto_approve`, because its
matched input is that same verified identity. Every other hook auto-approve
(title-pattern-matched) and every content-matching path stay fail-closed on the
composite fidelity, and no program name is vouched for. An approver that raises
on such a request counts as its rejection.

**Answering.** The audit row is written before any wire I/O for a refusal, and
after the wire answered for an approval (an approval the transport floor turns
into a rejection is audited as `rejected_transport_floor`). A HOST refusal (the
spec hooks, a hook deny, the fail-closed child answer, deny by default) steers
the in-band deny notice before the reject; an approver's no, and the reject that
precedes a `turn_limit` / `child_escalation_limit` bail, stay bare. A refusal row
the audit cannot write is logged and the request is still answered; an approver
that raises outside the fidelity gate, or a cancelled wait, leaves the request to
the run's teardown. An approved
backend-child escalation counts in `tool_count`, and every refusal of a backend
child's request is one point of `kirocrew.acp.child_permission.denied`. The
ladder's stages, its answering sequence, the shared adapters and this surface's
SEL row vocabulary (`SubagentRows`) live in `tool_permission.py`; which stages
the subagent runs and in which order live in `_permission_policy`, and what reads
or moves this run's own state (the approval prompt's wait flag and crew-log
pair, the log lines, the counter, `tool_count`) lives beside it in `run.py`.

The `is_yolo()` read happens once, when `parent_policy` is resolved at
`_run_inner` start — a YOLO toggle mid-execution takes effect on the next
subagent run, not on the current run's remaining tools.

### `snapshot_teardown_children(parent_session_key) -> (agent_id, ...)`
The SELECTION half of a parent-end teardown, and synchronous on purpose: the
session lifecycle calls it while it still holds the registry lock, in the same
hold as the pop that retires the key. Every await after that point is a window in
which a cold start can register a SUCCESSOR under the same key, so an answer
computed later can name the successor's runs. Returns the live and the queued runs
both — a queued run's stagger timer would otherwise start work for a parent that
is gone. Beside the selection it opens the snapshot's FENCE (`note_teardown_snapshot`,
kept per key at the latest snapshot in `_teardown_store_fences`): from then on every
row the store accepts for this key is recorded in it, by `taskq_accept_record` before
the row is written. The cancel takes it (`take_teardown_snapshot`) to sweep the rows no
selection can name, and it keeps recording (`_teardown_store_sweeps`) until that
cancel returns. A recycle takes no snapshot, so it opens no fence and its children's
rows are left to the resumed conversation.

What it MARKS is wider than what it returns, and the two questions are different:
the return value is what to cancel, the mark is whose delivery to drop. A run that is
`done` but whose outcome has not reached the parent has nothing to cancel and everything
to gate. Selecting on "not done" alone left that class free to inject, and the injector
resolves the parent key through the session registry and CREATES a session when none is
live, so the delivery rebuilt the conversation the teardown had just taken down.
The rows a Stop all batch is still cancelling for this parent are the second class of
that kind: in neither `_queue` nor `_agents` until the batch reports them, and past the
store sweep once its cancel lands, so the mark takes them from `_batched_stop_parents`
(see `cancel_for_parent`) and the return value leaves them to the batch.
A run parked on its spawn-approval prompt and never started (`_exec_started is None`)
is the third class: it is marked, so its delivery is gated, but neither cancelled nor
returned. Cancelling it would answer a decision a person was asked for (the approval
then reads "not found or expired"), so the prompt keeps its own approve/reject; if it is
approved later, the result goes to the run's own file and tombstone, not into the
retired conversation. A run that has started and is parked on a later approval is live
work and is returned for cancellation.

"Has the outcome reached the parent" is asked through `delivery_is_parked`, which reads
one declared table, `DELIVERY_ROUTING_FIELDS`. The table is enumerated from the PRODUCING
side: every `SubagentInfo` attribute written by the four modules that own terminal-outcome
routing (`subagent_manager/terminal.py`, `subagent_manager/waves.py`,
`subagent_manager/cancellation.py`, `slack/gateway.py`), classified as `PARKS_WHEN_SET`,
`PARKS_WHEN_UNSET` or `NOT_DELIVERY_STATE`.

That direction matters because the question has five representations, not one, and reading
them off failures found one per round:

| state | what parks |
|---|---|
| `_reported_to_parent` | falsy — its own report never returned. The only positive evidence, which is why it reads the other way round |
| `_digest_held` | the gateway held this member's injection for the wave digest (the restart-safety contract the run loop reads) |
| `_digest_held_at` | the same hold's timestamp, kept separate because the hold-deadline sweep must not mutate the flag |
| `_digest_settle_deliveries` | non-empty — `SubagentDelivery` snapshots of other runs' deliveries are parked ON this record |
| `_delivery_queued` | the announce sits in the parent's slot queue until a turn drains it |

`_digest_flush_only` is classified as not-a-parked-state: it marks the synthetic record
`force_digest_flush` builds, which is a CARRIER of a future injection with a fresh id, so
an id-keyed gate can never recognise it — that path is disarmed at its source in
`_expired_digest_holds` instead.

The union is deliberately conservative. Reading "parked" for a run whose delivery did land
costs nothing, because the gate only skips an injection and a delivered run does not inject
again; reading "landed" for a parked one rebuilds a retired conversation.

`test_the_delivery_parked_states_are_enumerated_from_the_producers` recomputes the write
set from those four modules' AST and fails when it stops matching the table, so a new
parked state cannot be added by a producer without a teardown rule. A companion test drives
each rule in the table on its own, so a classification nothing reads cannot go unchecked.

### `cancel_for_teardown(agent_ids) -> stopped`
The CANCELLATION half. Takes ids rather than a parent key, because a key would be
re-resolved here and that is the defect the snapshot exists to avoid. Each run is
marked `_teardown_cancelled`, has its stop cause and origin written on it
(`_reap_reason = "parent_end"`, `_stop_origin = "parent conversation ended (<verb>)"`,
which `cancel` carries into the reap and the tombstone — see the Terminal-State Contract)
and is then stopped through the ordinary `cancel` machinery; no second reap path
exists and one would drift. The one `parent-end teardown: verb=… key=… snapshot_ids=…`
audit line is logged at **WARNING when the snapshot names work to discard** (INFO for a
childless parent end): the gateway log's default level is WARNING, and at INFO the only
record of an action that discards live work was invisible in every field report of it. A queued run's store phase
goes through `taskq_cancel_queued_async`, which is `taskq_cancel_queued` handed whole to
`store.run` rather than a second copy of its transaction — one hop onto the writer
thread, and the race-safety argument (the state test and the cancel sharing one
`only_from` under the generation the read returned) is the same code rather than the same
intent restated.

**After the snapshot's ids, the store rows accepted before the snapshot.** A row held
only by the store — a memory-deferred spawn, or one waiting past the window — is in
neither the queue nor `_agents`, so no snapshot names it, and left alone it would stay
queued for a conversation that has ended: it would count on the card ("1 waiting") under
a key that has moved on, and start into whatever the key serves next. So the cancel then
reads this parent's waiting rows through `taskq_pending_ids_for_async(…, include_window=True)`
and stops each one through the same per-id path (marked first, `allow_admitted=False`,
a queued-stop report with no injection), every one except the rows the snapshot's fence
recorded. So a row a successor under the same key queued after the snapshot is never
swept, even one accepted while the cancel runs, and a row the refill hydrated into the
window after the snapshot is swept like one on disk (the window's own rows are kept in
the read, since the teardown's `_unqueue` drops a window entry with its row). The read
leaves `admitted` rows out, where Stop all's includes them: the teardown refuses a
claimed-not-started row, so naming one would only mark it in the delivery gate and
count it in the audit line
(`test_queue_depth_reconcile.py::test_a_parent_end_sweep_never_names_a_claimed_row`).
The fence orders by ACCEPT, never by `created_at`: a wall clock stepped back during the teardown
stamps the successor's row earlier than the retired conversation's, and a time cutoff
would sweep it. It runs after the
named runs are stopped, so it never delays a live reap behind a store read, and it logs
one `parent-end teardown: … store_rows=… store_ids=…` WARNING when it finds any. A store
it cannot read sweeps nothing yet (the snapshot's ids are already stopped): the teardown's
reading lets `TaskStoreUnavailable` propagate rather than answer "nothing to stop", which
would release the fence over rows nobody stopped (Stop all's reading, `cancel_for_parent`,
answers `[]` instead). The sweep is recorded as owed (`_teardown_sweeps_owed`) and its fence stays open, still recording and
still gating a max-wait expiry of the retired rows (`accepted_before_open_teardown`), and
each reaper sweep runs it again (`retry_owed_teardown_sweeps`, the same cancel with no
snapshot ids under the kept fence) until a read lands and the fence is released. The
first refusal warns; a refused retry logs at DEBUG. Pinned by
`test_subagent_queue_max_wait.py::TestARefusedTeardownSweepIsRetried`, and by
`test_queue_depth_reconcile.py::test_after_each_exit_the_published_depth_equals_the_store_count`
(`parent_end`, `session_reset`),
`test_a_parent_end_stops_its_store_rows_without_reporting_them_home`, for a row the
refill windows between the snapshot and the sweep,
`test_a_parent_end_sweeps_a_row_the_refill_windowed_after_its_snapshot`, for a clock
stepped back mid-teardown,
`test_a_wall_clock_stepped_back_mid_teardown_never_sweeps_the_successors_row`, and for a
successor spawn during the cancel,
`test_a_successor_row_accepted_while_the_teardown_cancel_runs_is_spared`.

The mark is what separates this from `cancel_for_parent`. A user pressing Stop all
wants the outcome reported back into a conversation they are still looking at, and
`_on_done` resolves the parent key through the session registry and injects,
CREATING a session when none is live. At a parent end that would rebuild the
conversation the teardown just took down and seed it with a retired run's terminal
text, so `_report_terminal_impl` drops the injection for a marked run. The
`subagent_done` event still goes out, so a dashboard watching the card sees it end,
and the run's own result file and tombstone are unaffected.

The mark gates the FAILURE announce on the same grounds. `notify_injection_failed`
is the one choke point every undeliverable-report caller funnels through — the
`_on_done` timeout in `_report_terminal_impl` and the gateway's five injection
paths — and it queues a synthetic completion into the parent's dashboard slot for
the LLM to drain on that key's next turn. A queued notice therefore outlives the
conversation it describes: the next turn on the key belongs to whatever session the
key serves next, which would read a retired run's completion text as its own. So a
marked run announces nothing, and the gate sits in the choke point rather than at
the six callers, where it would have to be restated and could drift.

The WAVE DIGEST needs more than the id gate, because its flush record is synthetic. A
member parks its siblings' announces on its own digest (`_digest_held_at`,
`_digest_settle_deliveries`), and when the hold ages out the reaper arms
`force_digest_flush`, which builds a fresh `SubagentInfo` with a new id and announces
through `_on_done` directly — an id-keyed gate can never recognise it, so it would
rebuild the retired parent's conversation minutes after the skip. Two guards close it at
the source: a suppressed report drops its own hold and marks the siblings it was holding,
and `_expired_digest_holds` skips a marked member so a batch of them produces no expiry
at all. The held siblings are marked, never tombstoned: their results reached no parent,
so restart orphan reconciliation must still be able to find them.

ACCEPTED RESIDUAL: the gate is in-memory, so it does not survive a restart. A run left
recoverable this way is found by the next start's reconciliation, which reads `result.txt`
and re-delivers — into whatever the key serves by then. The alternative is a durable
"do not deliver" mark in the run folder that `list_orphans` reads, and that is a worse
trade here: it converts a recoverable result into a discarded one on the strength of a
flag written by a process that has since died, and the failure it prevents (one stale
completion in a later conversation on the same key, after a gateway restart) is visible
and correctable, while a wrongly-marked result is silently lost. The `on_orphan_notify`
DM fallback is the honest channel for the same outcome and stays. Revisit if reconciliation
gains a durable notion of which conversation a result belongs to.

The gate set is bounded by AGE, never by count (`_AgingIdSet`, TTL
`_TEARDOWN_GATE_TTL_SECS`, one day against an `_ON_DONE_TIMEOUT` of twenty minutes plus
the digest hold). A capacity rule evicts by arrival order regardless of whether the run
can still announce, so one parent with more queued children than the capacity would drop
its own earliest ids while their reports were still being spawned — and those reports then
walk through the gate and rebuild the conversation the teardown took down. The lifetime is
not tied to the run's `_agents` record either: that record is popped while a run is still
tearing down (a dashboard "clear completed" does it), which is the same reason
`_teardown_gates` outlives those records. A read does not refresh an entry, or the TTL
would stop describing what is retained.

### RESIDUAL: what a parent end does NOT stop

A parent end arms its mark and takes its snapshot at the one synchronous point available:
inside the registry lock hold that retires the key. Anything already IN FLIGHT at that
instant sees neither. That leaves two halves, and they are the same defect at opposite ends
of the same window:

- **Admitted late may start.** A spawn between its row write and its registration is in
  neither the queue nor `_agents` — `spawn_async` persists the row and then re-enters
  `spawn` to register — so no selection can name it, and it starts into whatever the key
  serves next. A durable row held only by the store is outside this half when its
  `taskq_accept_record` ran before the snapshot: the snapshot's fence never recorded it,
  so the cancel's sweep stops it. A row is spared exactly when `taskq_accept_record`
  runs after `note_teardown_snapshot`, and that includes an accept from the RETIRED
  conversation still in flight at the snapshot (a `spawn_async` row write queued on the
  store's writer thread behind it), which the fence cannot tell from a successor's and
  spares. So is a row a `spawn_async` caller is still admitting. No clock is read, so a
  wall clock stepped back cannot make the sweep take a successor's row.
- **Reporting late may deliver.** A report that has already passed the delivery gate and
  is suspended inside `_on_done` is not stopped by marking its id afterwards. The injector
  resolves the parent through `get_or_create`, which CREATES a session when none is live,
  and never re-reads the mark — so it can rebuild the conversation the teardown just took
  down and seed it with the retired run's text.

Both are bounded by the run's own timeout. The delivery gate is a backstop for the FIRST
half only when the run was selected: a run the snapshot never named is never marked, and a
report already past the gate is not reached by marking it later.

Neither half is closed by another recheck at one end. Selecting more, or re-testing before
injecting, both need an await, and an await here cannot tell work belonging to the retired
conversation from work a successor under the same key has just started: a run registered
after the snapshot looks exactly like one the successor started, and a report resolving
its parent looks the same whichever conversation it belongs to. A durable row is the one
exception, because every accept is recorded against the snapshot's fence as it happens
and every conversation that held the key before the snapshot had ended by it; that record
is the whole of the store sweep's fence. The answer is one
identity every path can test, not a recheck per path — a conversation-incarnation counter
that does not exist today. Tracked in #12069, which carries both halves.

What makes that unanswerable today is that the session layer has no counter for it.
`session_generation` reads `_ownership_generations`, which is an ALLOCATION-OWNERSHIP
counter: its own docstring says "every reservation publication/removal advances the
canonical key's counter", and `get_or_create` advances it twice per call — once taking
its allocation reservation token and once in `_remove_reservation_now` releasing it — with
no teardown in the path. `reset` advances it on a RECYCLE too, where the children are
guaranteed to survive. Fencing on it therefore refuses ordinary queued work rather than
successors' work, which is the opposite of the intent — so no fence is better than that
fence, and the residual is carried openly instead. #12069 carries the counter design and
its cost.

### `cancel_for_parent(parent_session_key) -> (running, queued)`
Stops every running agent and removes every not-yet-started stagger/concurrency
queue entry owned by one parent session. A `_resume_id` entry is NOT one of
those: it is a RESIDENT run asking for the lane slot it yielded back, filed
under that run's own `_preassigned_id` and its parent key, so it matches both
terms of the queued scan and is skipped there — exactly as the pump's grant
loop, the refill's lane census, the eviction, the child reserve,
`_conversation_busy` and the queue depth (`_window_depth`) all separate it out. The running sweep stops such a run instead, where its intact `_agents`
record still is: routing it through the queued-stop path publishes a synthetic
`queued=True` "never started" terminal OVER that record, so the coroutine keeps
executing, the parent is told the work never began, and `resume_grant` can never
return the slot because the record it reads is gone. Pinned by
`test_overload_integration_glue.py::test_stop_all_reaps_a_resident_resumed_run_it_never_treats_as_queued`
and `::test_stop_all_never_takes_the_queued_stop_path_for_a_claimable_resident_row`. Queue removal happens before the first
suspending await, so a scheduled drain cannot start work after the stop request.
The store pass (`taskq_pending_ids_for`) names every accepted row with no run
yet, the same set the queued count reads: claimable rows and `admitted` rows
nothing registered (a claim the pump is awaiting, or a retained one; a direct
`spawn_async` claim is still in `_admitting_ids`, which `taskq_excluded_ids`
leaves out of this pass). Stop all cancels such a row, and the claimer's
post-claim re-read (step 5 of `spawn`) then refuses it, so a row stopped
between its claim and its start never starts. A row the pump registered while the store read was in flight is a
live run: the queued pass leaves it out of its cancel job and the running
sweep reaps it. A claimer whose re-read was queued ahead of the cancel job
answers before the cancel lands, and it can resume before the batch's answer
installs any record; it finds the row in `_batched_stops`, waits for that
answer and re-reads behind the cancel, so it refuses the start instead of
registering a run the cancel ends under it (counted once as queued, once as
running). The
queued-stop report never installs its synthetic record over a registered
run's `_agents` record, since every running sweep skips a `queued` record and
nothing would stop the run behind it, and it leaves what this process kept for
that run's start (`_forget_pending_start`: a memory-pressure hold the run may
still be waiting under) to the run's own start. Pinned by
`test_taskq_admission_integration.py::test_stop_all_between_claim_and_start_keeps_the_row_stopped`,
`::test_a_row_registered_during_stop_alls_read_is_reaped_not_replaced`,
`::test_a_claim_resumed_between_stop_alls_post_and_answer_refuses_the_start`,
`::test_a_queued_stop_never_replaces_a_registered_record` and, for a cancel that
reaches the row through the store alone,
`::test_a_store_only_cancel_between_claim_and_start_refuses_the_start`.
The rows' store cancels never run on the loop: a synchronous cancel there holds
it for the store's busy timeout, once per row, whenever the store is contended
(one measured Stop all froze the gateway for 10 s that way). Each pass
(the window's rows, then this parent's rows waiting outside it) queues ONE job
on the store's writer thread for all its rows (`taskq_post_cancel_queued` →
`taskq_cancel_queued_each`, which runs `taskq_cancel_queued` per row) and only
then drops the window entries. Both happen before the pass first awaits, and
the writer thread runs jobs in submission order, so a refill or claim queued
afterwards lands behind the cancels and finds the rows cancelled. With the pump
inline the job runs inline too, because there the pump's own store calls are
not ordered behind that thread. A refill fetch queued BEFORE the job can still
run first and read the rows as waiting, so the parent is held in
`_stopping_parents` across both passes and `_refill_apply` windows none of its
rows meanwhile: such a fetch can neither put back a row the stop is cancelling
nor window a store-only row the pending read would then skip. A pump pass that
ran under that fence windowed nothing of this parent's, so a row the parent
spawned after the passes read their ids could wait on disk beside a free slot;
releasing the fence schedules one more pump pass, on the next loop turn, after
the running sweep has taken its ids (pinned by
`test_queue_depth_reconcile.py::test_a_row_spawned_after_stop_alls_read_starts_once_the_stop_ends`).
The job is queued before the entries are dropped so that a post that raises leaves the window
whole. The answers are applied by a tracked task the
request awaits through a shield, so a request cancelled after the job was
queued still reports every row the job cancelled. A cancel that landed is the
row's terminal write, so its queued-stop report skips the settle's `finish`
(`row_settled`; it could only be refused) and keeps the propagation to a waiting
parent. The stage Cancel, the parent-end teardown and a single `cancel` pass the
same flag for the rows whose own cancel landed. A cancel that did not land
leaves the settle in place and is re-posted (`_repost_unlanded_cancel`). Until the
applier has reported a row, that row is in neither `_queue` nor `_agents`, so
every id of the job is filed in `_batched_stops` for that span: the claimer
above joins it, and a single `cancel` of the row joins the batch's answer
instead of cancelling it on the loop. For a row whose window entry the batch
popped, a single cancel that did not join would land first and report the row,
and the batch, finding the row already cancelled, would re-post the cancel and
report it a second time (two `subagent_done`, two completions to the parent). A
store-only row is not reported twice that way, since the batch reports nothing
for a row it holds no entry for and whose cancel came back empty, but its single
cancel joins as well, so no cancel of a batched row runs a store call on the
loop. Each id is filed with its parent in `_batched_stop_parents` too, for a
third reader: a parent-end teardown's snapshot (`snapshot_teardown_children`)
reads only `_queue` and `_agents`, and once the batch's cancels land its store
sweep cannot name the rows either. The snapshot puts this parent's batched ids
in the delivery gate (`_teardown_cancelled_ids`) without returning them for
cancellation, so the batch still cancels and reports each row once, its
`subagent_done` still goes out, and nothing is injected into the conversation
that ended. The other
stops that cancel through the writer thread (a parent-end teardown, a stage
Cancel) can land their cancel ahead of the batch the same way, so the
queued-stop claim is also taken per ROW: `_report_queued_stop` reports nothing
for a row that already holds its finalized queued-stop record, and the batch
re-posts nothing for one but still counts it, since it took the row from the
window: Stop all's queued count does not depend on which stop reported the row
first. Each report builds a fresh `SubagentInfo`, so the
record's own one-shot claim cannot see an earlier report of the row. A store that
closes under the batch (`TaskStore.close` cancels the queued job) reports
nothing: the rows stay `queued` on disk for the next start, their entries are
already gone, and a joined `cancel` returns False. Pinned by
`test_queue_depth_reconcile.py::test_a_single_cancel_during_stop_all_joins_the_batch_and_reports_once`,
`::test_a_single_cancel_joined_to_a_batch_that_never_answers_returns`,
`::test_a_teardown_cancel_queued_before_stop_all_reports_its_row_once`,
`::test_a_parent_end_during_stop_all_gates_the_batchs_reports`,
`::test_stop_all_over_store_only_rows_never_blocks_the_loop`,
`::test_stop_all_queues_its_cancels_and_drops_its_entries_before_it_suspends`,
`::test_a_refill_queued_before_stop_all_windows_none_of_the_parents_rows`,
`::test_a_stop_all_whose_post_raises_leaves_the_window_whole`,
`::test_stop_all_reposts_a_row_cancel_that_did_not_land`,
`::test_a_stop_all_cancelled_mid_batch_still_reports_every_row_it_cancelled`,
`::test_shutdown_during_stop_all_drains_the_reports_its_applier_spawns` (see
`cancel_all`) and
`::test_a_queued_stop_skips_the_settle_its_landed_cancel_already_wrote`.
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
endpoint and remains available for queued-only waves. Each row it stops asks
for the depth in its queued-stop report; a queued pass that stopped none —
nothing was left, or it failed or was cancelled first — asks itself, before
any reap, so pressing it on a card whose count has gone stale repairs the card
even when there was nothing left to stop. A queued pass that raises ends the
call before the running sweep, and the request reports the failure: reaping
first would free slots the pump fills at once with the very rows the failed
pass did not reach. One row that fails does not stop the pass: a row whose
store cancel raised is still waiting (the store still holds it), so its window
entry goes back at the tail, where a refill would put it, the pass goes on and
then raises that failure; a row whose report raised was removed and counts as
stopped. Pinned by
`test_queue_depth_reconcile.py::test_a_row_that_could_not_be_unqueued_fails_the_stop_and_reaps_nothing`.

### `cancel_all() -> None`
Cancels all running subagents, stops the reaper loop, and awaits their cleanup. Handles `CancelledError` gracefully — sessions released, count decremented.
The shielded terminal reports (`_report_tasks`) are then drained inside ONE
`_REPORT_DRAIN_TIMEOUT` budget, and the drain re-reads the set after each wait:
a drained task can start another (a Stop all's tracked applier spawns each row's
queued-stop report once its writer-thread job answers), so a report that joined
during the drain is waited for too, and one still pending at the deadline is a
straggler like the rest. Pinned by
`test_queue_depth_reconcile.py::test_shutdown_during_stop_all_drains_the_reports_its_applier_spawns`.

### `steer_run(agent_id, message) -> (ok, detail)` / `follow_up_run(agent_id, message) -> (ok, detail)`
Two delivery modes for `spawn_steer` (REST `POST /api/spawn/{id}/steer`, body `mode`: `"interrupt"` default / `"follow_up"`). `steer_run` injects into the RUNNING turn via the provider's `steer`, with a bounded startup-grace poll for a live run whose session has not registered yet (#1113). `follow_up_run` never interrupts: it queues the message on `SubagentInfo.pending_followups` and arms a one-per-run watcher (`_deliver_followups`, registered in the manager-owned `_followup_watchers` dict — NOT the global `_safe_fire` set — because a watcher can spawn a brand-new run and must therefore be reachable by `cancel_all()`, per the same containment contract as `_schedule_cancel_recovery`; `cancel_all` cancels watchers BEFORE the run tasks so none can dispatch into a shutting-down gateway, and the watcher re-checks `_shutting_down` before dispatch). The watcher waits for the run to complete (`info.done` AND its task popped from `_tasks`, so teardown is finished), then dispatches the whole queue as ONE `continue_conversation` on the run's own conversation (messages joined in arrival order — three corrections cost one continuation, not three). Companion `_followup_watcher_parents` and `_followup_watcher_infos` maps retain the parent and exact run record independently of `_agents`; pending-work queries count each live parent-owned watcher, and parent-end cancellation can still reach an owner after completed-record eviction. The continuation is a normal new run on the same parent session, so its result arrives as a separate completion event. OUTCOME-AWARE: a run the user explicitly STOPPED (`user_stopped`) suppresses dispatch (`followup_suppressed` audit) — resurrecting killed work is the opposite of "the correction can wait"; error/timeout terminals still dispatch (the continuation carries the conversation's context, so "fix what broke" is legitimate). Other undeliverable paths (user stop, watcher expiry, dispatch failure) announce a SYNTHETIC failure completion event through the normal `_on_done` path, because the spawn_steer reply promised the parent an event — `followup_expired`/`followup_failed`/`followup_suppressed` SEL audits alone would leave the parent blocked on an event that never comes. Deliberately a per-run poller, NOT a hook in `_run`'s 3-guard finalization: completion is reached from many terminal paths (normal/error/timeout/cancel-recovery/reaper) and a watcher observes the outcome without adding an obligation to any of them. Bounded everywhere: poll cadence 2s, hard deadline `default_timeout + 300s`, and residual `conversation_busy` after done gets a bounded retry. Typed refusals mirror steer: `not_found`, and `not_running` (use `spawn_continue` directly on a finished run). `steer_run` additionally answers `session_starting` when a live run's session has not registered within the startup grace, and `steer_unsupported` when the run's backend cannot guarantee a mid-turn steer (`provider.steer_needs_loss_recovery`, codex): the caller sends `mode="follow_up"` instead.

### Properties

**Run controls are scoped to the originating session, for every internal caller.**
`spawn_steer`, `spawn_release`, `spawn_status`, `spawn_retry`, `spawn_delete` and
`spawn_continue` run behind `_spawn_scope_refusal`, and `spawn_list` filters by the
same predicate (`_run_belongs_to_caller`): the caller must be the run's
`parent_session_key`, or the run itself. This holds whatever memory store the
caller resolved to, and a caller with no `X-Session-Key` reaches only a run no
session started (a CLI run, whose record carries an empty parent). A run with no
managed state and no persisted record is owned by nobody and refused; an accepted
spawn that has not started yet (`queued_run_async`) is owned by its row's session
key; a
harness-native child (`native:*`) is owned by the dashboard slot tracking its card. KNOWN CONSEQUENCE in the pooled multiplexing shape
without caller injection: a kiro-cli process that multiplexes sessions
(`acp/runtime.py`) is session-unbound, and `mcp_core._post` sends no
`X-Session-Key` there unless `mcp_gateway.stub_servers` caller injection is on, so
from such a process `spawn_steer` against a run it cannot prove it owns is refused
with 404 `task_scope_denied` and an error text naming the identity gap and the
`kirocrew doctor` strict-identity diagnosis, and `spawn_list` shows it only
parentless runs. That is the intended fail-closed posture: a run's task text, id
and parent key are what a steer needs, so an identity-less caller is shown none of
another session's. The default install is not that shape: `_resolve_session_key`
reads the gateway-injected caller context, the MAC-signed per-session token, the
`KIROCREW_SESSION_KEY` environment and the PID-file ancestor walk in that order, so
a session's own kiro-cli process carries its key and its run controls are
unaffected.

- `running -> list[SubagentInfo]` — currently running agents
- `count -> int` — number of running agents
- `max_concurrent -> int` — the EFFECTIVE capacity limit (`min(user cap, adaptive cap)`)
- `user_max_concurrent -> int` — the user's resolved cap, the ceiling the adaptive bound sits under

## SubagentInfo

```python
@dataclass
class SubagentInfo:
    id: str               # 16-char hex run id (see _RUN_ID_HEX_CHARS)
    task: str             # original task text
    started: float        # time.time() at spawn
    done: bool            # True when finished (success or error)
    result: str           # LLM response text (trimmed to completion_keep for the event)
    result_path: str      # ~/.kiro/crew/subagents/<id>/result.txt (full transcript)
    result_truncated: bool  # completion copy dropped content → event carries summary+path
    error: str            # error message if failed
    elapsed: float        # seconds from start to completion (set in _run finally)
    credits: float        # cumulative reported credits across all attempted turns
    tool_count: int       # observed tool calls (incl. auto-approved); drives running-card progress
    last_activity: float  # time.time() of last stream event; reset to _exec_started; drives idle-stall
    stalled: bool         # reaper flagged this subagent as idle/stalled (UI signal)
    stop_reason: str      # ACP stop_reason of the completion that ended the run
    stop_class: str       # classify_stop_reason() class (succeeded|stalled|recovering|cancelled|failed)
    partial: bool         # result is text streamed before a non-success completion, or before a transient generate failure the run kept
    _awaiting_approval: bool  # blocked on a human approval prompt (spawn gate or mid-run tool) → exempt from idle-stall
```

## Session Lifecycle

1. `spawn()` increments `_running_count`, creates asyncio task
2. `_spawn_with_approval()` (no auto rung matched): requests approval through `on_spawn_approval`, whose callback sets the wait
3. `_run()` wraps `_run_inner()` with `asyncio.wait_for(_TIMEOUT_SECS)`
4. `_run_inner()` resolves `parent_policy` (the chain under the tool approval ladder), creates session `subagent:{id}` via `SessionManager.get_or_create(approval_policy=parent_policy)` — policy is persisted on the new session
5. Streams through ACP with context injection, tool approval cascade, and turn counting
6. On completion (in `_run` finally block): spawn a shielded report that fires `subagent_done` with terminal `elapsed` and cumulative `credits`, then calls `on_done`. Session release/reset and concurrency-slot release proceed independently of the report; successful delivery waits for teardown before marking the result delivered and stores both usage values in that delivery tombstone.
7. On timeout: `error = "Timed out after 180 minutes"`, unless the run already claimed its completed ending (the ending claim below), which the deadline only cuts short
8. On turn limit: `error = "turn_limit:{turn_limit}"` (default 1000)
9. On `CancelledError`: by cancellation source (see **Terminal-State Contract** below) — user stop → neutral `user_stopped` record (NO error); shutdown / spent one-shot → `error = "cancelled"`; anything after the run claimed its completed ending (the ending claim below) → completed; any other unexpected cancel → one-shot auto-continue via `_schedule_cancel_recovery`

**Early WS event firing**: `subagent_done` WS event is fired in the `_run` finally block BEFORE the slow `reset()` + `on_done()` path. This ensures the dashboard receives completion status within seconds, not 30-90s later when `stream_and_collect` finishes processing.

Credit accounting is per attempted provider turn, not only per successful run.
It uses the shared provider-billing snapshot, attempt-usage, and `TurnUsage`
accumulation path from `llm_helpers`, preserving every billing dimension while
exposing cumulative credits on `SubagentInfo`. Each `EVENT_COMPLETE` contributes
its event usage; an attempt that exits through an exception, cancellation, or a
run limit reads the provider's current prompt stats instead. This includes billed
transient retries without double-counting the final completion. A completion
withheld for infrastructure retry or stop recovery settles before re-admission,
so refusal or cancellation during that wait cannot discard its event usage.
Providers that do not bill in credits report `0`; display surfaces omit the credit
label for that value rather than claiming the run was free. The cumulative credit total is
included in live and reconnect `subagent_done` frames, shown in the expanded
terminal card body as the localized “Used … credits” summary, and included in
completion, delivery-failure, and wave-digest messages. Restart-orphan notices
omit usage because a run the restart caught has no settled terminal billing record.
Card headers retain elapsed time only so billing text does not crowd the status
and model chips in a narrow rail. A managed terminal card without positive reported
credits identifies the usage as unreported in the expanded card body. Backend
and dashboard duration formatting both round to
whole seconds before applying the one-minute display boundary. Native harness
subagents omit the usage summary because they share the parent billed turn and
have no independent run record.

The run total is a completion summary, not another billing-ledger entry: it is
never added to account-level usage totals. Successful-turn records and this
summary use the same reported provider usage, but failed-attempt credits can
make the run total larger than the successful-turn records alone.

Successful delivery stores elapsed time and credits in the existing delivery
tombstone; abnormal terminal paths store them in their failure tombstone. No
post-completion `state.json` writer is created, so terminal reporting cannot hold
parent delivery or later resurrect a pruned state file. A crash before delivery
can leave a legacy-shaped record without terminal usage; readers omit missing
fields rather than inventing zeros. The status endpoint `GET /api/spawn/{id}`
reads terminal usage from the live record, and for a run no longer registered
from the tombstone only (`messaging_api/run_views.py`); usage in `state.json` is
never read, and a non-finite or negative tombstone value is omitted. `GET /api/spawn` keeps its existing
fields because no list consumer displays terminal billing.

An attempt settles once at the outer run boundary as well as before a retry or
limit return. This includes cancellation or failure while handling an event
(approval waits, state writes, and chunk delivery), not only inside the provider
stream. An unchanged prompt-stats holder is not charged again when an attempt
fails before starting a new prompt. The active accounting record is shared with
terminal reporting: the reaper settles it before recording a tombstone or
capturing a completion payload, even when the cancelled consumer is still
draining a state writer. The consumer's eventual cleanup cannot count that
attempt again.

## Terminal-State Contract (stopped vs failed vs completed)

A record's terminal outcome is three-way, with a **single canonical source**: the `SubagentInfo.outcome` property (`"stopped" | "failed" | "completed"`). Every `subagent_done` emission (live, `_run` finally, `_force_reap`, WS reconnect replay managed + native), `native_subagent_snapshots`, the `/api/spawn` listing, and tombstones carry `outcome` explicitly. Consumers MUST use `outcome` — never re-derive from `error`-nullability (the legacy `error ? failed : completed` idiom misreports a stopped agent as completed). `stopped`/`error` remain on the wire for compatibility:

| Outcome | Record shape | UI/consumer meaning |
|---|---|---|
| `stopped` | `user_stopped=True`, `error` **unset** | neutral: user killed it; partial result preserved; NOT a success, NOT a failure |
| `failed` | `error` set | failure (tombstoned, counted in Stats) |
| `completed` | neither | success |

- A user stop is neutral **in the record itself**: `cancel()` sets `user_stopped=True` and neither it nor `_force_reap` ever synthesizes an `error` for it.
- **A completed ending is a claim, taken first or not at all.** A run whose stream delivered a whole answer takes `SubagentInfo._ending_claimed` synchronously, before any await, unless a stop already did (`done`, `_reap_started`, `user_stopped`, or an `error` a failed child under `on_child_failure=fail_parent` or an expired wait stamped while its cancel is still scheduled); every stop path then treats the claim as `done` — a Stop returns False (with no stagger-queue lookup, since a registered run is never queued), a parent end and a reap stamp nothing, and neither a failed child under `on_child_failure=fail_parent` nor an expired wait stamps an `error` on it — so a stop landing while the claimed run writes its result can no longer leave the run returning with no ending recorded (see the ending claim under Unexpected-Cancel Recovery).
- **A reap's echo is recorded as the reap, never as a runtime death.** `_force_reap` tears a dedicated run's session down (`sessions.reset` → `provider.shutdown()` → `runtime.kill(reason="provider shutdown")`) BEFORE it cancels the run task, so the in-flight `client.stream` observes the kill first and raises `AcpProcessDied` — `Runtime process died during prompt — killed (provider shutdown) [returncode=<not reaped>]` — inside `_run`'s `except Exception` arm, ahead of the reaper's own record. That arm reads `_reap_started` together with `agent_sdk.drivers.acp_vocab.is_runtime_death(exc)` (the `AcpProcessDied` test, offered from the driver vocabulary so application code never names the ACP class): both true, the exception is the ECHO of our own teardown; any other exception under a reap is the run's own fault and keeps the existing failure path and traceback and the record names the stop — `_stop_origin` ("stopped by user", "parent conversation ended (<verb>)", "reaped after Ns (<reason>)", written by `cancel()` / `cancel_for_teardown` / `_force_reap` next to the `_reap_started` marker) and the reap's own tombstone cause `_reap_reason` (`user_stop` / `parent_end` / `reaped` / `startup_timeout`; the parent-end teardown writes it before calling `cancel`, a bare `cancel` is the user's Stop, and `_force_reap` fills in its own reason only when none is set — nothing is inferred from the origin text, and every writer assigns only when the field is still empty, so the FIRST stopper keeps the attribution when a user Stop and a parent end race). `tombstone_terminal_state` maps `parent_end` to the task queue's CANCELLED like `user_stop`, so boot reconciliation settles such a row instead of recovering a deliberately ended run. Nothing writes `stage_cancel` any more; tombstones older versions wrote with it still map to CANCELLED (`subagent_manager/admission/types.py`). Neutrality is decided by the FIRST stopper, `SubagentInfo.stop_is_neutral` (`_reap_reason in _NEUTRAL_REAP_REASONS` = `user_stop` / `parent_end`), never by `user_stopped` alone: a Stop that lands while a deadline reap is already tearing the run down sets `user_stopped` too, and both the echo arm and `_force_reap`'s own record put the flag back so the late Stop cannot convert a claimed deadline failure into a neutral stop. A user stop and a parent end stay neutral (`error` unset, `outcome == "stopped"`, partial output preserved); a deadline reap is a failure whose `error` names the deadline. The gateway log gets ONE line — INFO for a user's own stop, WARNING for a parent end or a deadline reap — never `Subagent X failed` at ERROR with a traceback. Recording the death text as the run's error, tombstoned `cause="error"`, sends every reader of a run "dying at random" (a user Stop-all, an identity-sweep parent end) to the provider, the OOM killer and the leak reaper in turn. `_reap_started` (not `reaped`) is the gate because the reaper sets `reaped` late, after the awaits; the record is still first-arrival (`if not info.done`) so the reaper's own synthesis is never duplicated. Pinned by `test_subagent_reap_attribution.py`.
- Every emission carries the flag explicitly: live `subagent_done` events, the `_run` finally emit, `_force_reap`'s emit, WS **reconnect replay** (managed and native), `native_subagent_snapshots`, and the `/api/spawn` listing all include `stopped`. Cancelling a native card persists `stopped` on the slot tracker record so replay reconstructs it as stopped.
- The gateway completion consumer (`_subagent_done`) classifies three-way: a stopped agent is announced ⏹ with the record's own `_stop_origin` as its status ("stopped by user" when a user pressed Stop; a parent-end verb otherwise, so the announce never credits the user with a stop they did not press), and partial output flagged.
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

**Durable row timing**: the row is `starting` from claim through session creation, the session-start gate and any late adoption; it becomes `running` at the run's FIRST stream event addressed to its own session (`ensure_running_marked`, `SubagentInfo._taskq_running_marked`; a `runtime_global` co-tenant broadcast does not count) — or, for a provider error / stall that lands before any frame, at the moment the wait is recorded (the turn WAS issued on a live session, and a wait can only be written on a `running` row; a `starting` row would be parked `retry_wait` instead). A late adoption writes `running` at adoption (it leaves the claimable `recovering`).

The MECHANISM that delivers that invariant is the write channel, not luck. `ensure_running_marked` POSTS the mark and `yield_slot` POSTS the wait write, so on the store's single writer thread the wait lands behind the mark even when both are issued in ONE synchronous block — which is the normal case, because `session_handle` yields every event of one update back to back and appends the structured status to that same list. The dependency path is the exception and does not come through `ensure_running_marked` at all: `coordinator.report`'s wait write chooses wait-vs-park from its own result in the same critical section, so `_dependency_verdict` hands `(coordinator, store, task_id, signal, generation)` into `_dependency_report_db` and the mark plus `report` run as ONE unit on the writer thread. The mark it hands over is UNCONDITIONAL — `TaskStore.advance` is what decides, never the in-memory `info._taskq_running_marked`, which is published ahead of a posted write whose refusal reaches nobody, so a flag-gated park would write `retry_wait` on a live resident run. Details and the refused-edge consequences: [taskq.md](taskq.md) § Off the event loop.

**Record**: `SubagentInfo.stop_reason`, `stop_class` and `partial` (True when text streamed before a non-success completion, or before a transient generate failure the run kept — see Transient Retry) are set on the completion that ended the run and carried on `subagent_done`; the gateway's parent injection appends the partial under an explicit "did NOT finish" banner when `partial` is set on a failed run. On the kept-success path (`outcome == "completed"`, `partial` set, `error` unset) that banner does not fire: the warning line that leads the result is the marker, and the batch digest tags the member `✅ (partial: backend failed to generate the final response)`. `outcome` stays the three-way canonical source (`stopped` / `failed` / `completed`); the class says WHY.

**Task runner**: `task_executor.execute_single_task` raises `_TurnNotCompleted(stop_class, stop_reason, partial=…)` inside the attempt's `try` for any non-success class, so the existing ladder (`MAX_RETRIES`, same-error loop detection, session reset between attempts) owns the recovery and `task.result` keeps the partial.

## Transient Retry (mid-stream 5xx) and dependency waits

`_run_inner` streams through `_stream_with_transient_retry`. A transient backend error (the `-32603` class, per `acp_error_is_transient`) is first shown to the dependency adapters (`taskq.dependency.classify_exception`, [taskq.md](taskq.md) § Dependency waits). When the manager has a durable store and an adapter recognises the error — a provider throttle (`rate_limited`, `concurrency_exceeded`), a 5xx / connection loss (`dependency_unavailable`) — the run does NOT retry in the turn: `_yield_for_dependency` calls `coordinator.report(id, signal, generation)` (ONE store write: `running -> waiting_dependency` with the scope's `retry_at`), releases its lane slot through `admission.yield_slot(persist=False)`, reports a typed throttle to `AdaptiveController.record_provider_throttle(scope)`, arms the pump and parks on `_await_lane_resume(request=False)`. The coordinator wakes the scope by capacity — one probe, then `wake_per_tick` per `wake_spacing_secs` — and a live run's wake comes back through admission (`monitoring.taskq_wake_through` → `request_resume`; admission's `resume_grant` writes the `wake_wait` under the run's generation, so one wake is one write). After the grant the run replays the original prompt (zero activity) or sends `_TRANSIENT_CONTINUE_MSG` (any activity, one-shot, same rule as below). A scope the coordinator fails (attempts cap, wall-clock deadline) releases the parked run through `on_fail` (`SubagentInfo._wait_failed`) and the run ends `failed` with the provider error; a terminal signal (`auth_failed`, `permanent_param_error`, quota with no reset) propagates at once. Two runs throttled by the same provider therefore share one schedule and never two timers; the main chat reads the same schedule (`chat_runner._shared_dependency_delay`) without joining it.

The in-turn ladder below handles what no adapter classifies (and every transient when the durable queue is off — `agent.task_queue_enabled=false`): errors are retried with exponential backoff on the same live session; each retry fires a `subagent_retrying` WS event (chip shows `⟳ retrying`) and a SEL audit record. **Replay-safety**: if ANY activity was observed (text chunk, approved tool turn, or auto-allowed tool call), the retry sends `_TRANSIENT_CONTINUE_MSG` instead of the original prompt — a mutating tool may have executed before the first text chunk, and replaying the full prompt would re-run it. **Budget**: `TRANSIENT_RETRIES` applies only while ZERO activity was observed (replaying the bare prompt is side-effect-free); after any activity, recovery is ONE-SHOT — exactly one continuation turn, matching the main path's `_posttoken_retry_used` rule, since each post-activity continuation is an independent opportunity to repeat a side effect. The two ladders (this one and `dashboard/chat_runner.py`'s) are intentionally-identical copies cross-referenced in both sources; a change to either's predicate or budget must be mirrored. Non-transient errors and exhausted budgets propagate to the generic error arm, with ONE deliberate divergence: the sub-agent ladder has a terminal keep-output branch that `chat_runner`'s ladder does not. When the one post-activity continue is spent, the streamed output is non-empty and the transient error says "failed to generate a response", the stream ends and the run is kept: the warning "_Warning: the backend failed to generate a final response; this is the output streamed before that._" is added to the delivered result after the `completion_keep` cap and to `result.txt`, `partial` is set, and `result_complete` stays false (no complete event). The kept run still counts as a success for `record_success` and `inc_subagent_completed`; that is intended, because the parent got the output. The sibling "failed to process the request" is scoped out and still fails. The interactive chat has its own recovery for this error, so it does not mirror the branch.

## First-Turn Context Overflow Recovery

An `AcpError` carrying `context_overflow=True` is deterministic on the native
session that raised it. A subagent may recover once only when the failure came
from its first attempt on a session-shared runtime (`_session_sharing`), before
any text, tool call, or completed turn, and the run has no `conversation_key`.
A continuation carries the established conversation's identity and history in a
non-empty `conversation_key`; rebuilding it cannot satisfy the
fresh-first-attempt premise, so even a zero-activity overflow is terminal
without consuming the one-shot recovery. A first attempt that already ran on a
dedicated process is terminal the same way, without scheduling recovery or
consuming the one-shot: no transition is evidenced that would be expected to
make a replacement's envelope small enough or different enough to fit, so
another teardown, capacity-wait and spawn cycle is not justified.
The proven invariant is narrower than any diagnosis: a replay on the same native
session cannot shrink the envelope that session was already rejected on, while
one fresh dedicated retry of a shared first attempt rebuilds session and
projection state from scratch. Why the first envelope was too large is not
established here, so this is best-effort recovery, not a diagnosis of the cause.
`_run` marks the run recovering, lets its own teardown
finish (the shared handle is shut), then
reuses the fresh-task recovery scheduler with `_force_dedicated=True`. After
teardown, the scheduler reads the fresh run's recorded `_session_id` and removes
that exact resumable SID through
`SessionManager.forget_conversation_if_sid`. The live `SessionMap` compares and
removes the entry in one `_MAP_LOCK` critical section, so a worker-thread writer
cannot place a successor between the check and deletion. An actual deletion is
then made durable with `await SessionManager.aflush()` before the scheduler
clears startup or process state, waits for capacity, allocates the replacement,
or emits recovery. This is the session registry's persist-before-publish point:
a process exit after recovery can no longer restore the rejected SID from the
last on-disk map. An absent mapping changes nothing and skips the flush; a
different mapped SID is left untouched, skips the flush, and recovery fails
closed. If the flush raises, the scheduler takes its existing terminal
recovery-failure path without allocating or reporting recovery, and the finalize
claim still reports that failure exactly once. A MISSING `_session_id` fails
closed the same way, before any capacity wait or replacement allocation and
without reading, altering, or flushing any `SessionMap` entry:
the identity capture after session acquisition is best-effort while the
allocation may already have persisted a resumable mapping, and with no rejected
SID to compare, a mapped SID cannot be proven to be the rejected attempt rather
than a successor, so a replacement could `session/load` state the run does not
own. Both closed outcomes take the scheduler's terminal recovery-failure arm
(`info.error` names the dedicated-session recovery that could not start) and
report exactly once through the finalize claim.
A `keep=True` run never reaches this recovery: `_sharing_plan_impl` starts a
kept run on a dedicated process, so its first-turn overflow is terminal under the
shared-first-attempt gate above.
The scheduler next clears the first attempt's startup clock
(`_startup_deadline_stamp`, then `_exec_started`), then the retired
runtime's `_pid`, then `_session_sharing` and `_shared_provider`, with every
update before any capacity-wait suspension. The clock goes first because the
startup watchdog (`_is_startup_stalled`) reaps a run with `_exec_started` set,
no PID, no first stream and no turn once that clock passes the startup deadline:
with the PID cleared and the first attempt's clock left in place, a capacity wait
longer than the deadline would be force-reaped as a stalled start. A cleared
clock reads as a run that has not entered `_run_inner`, which the replacement
has not; `_run_inner_impl` re-stamps it as its first statement. The PID-first
order among the identity fields makes concurrent samplers stop
before they can interpret the cleared sharing state; the cleared PID is the
retired attempt's whether it named a shared runtime or the run's own ended
process, and the replacement records its own. The
replacement process therefore starts inside the startup watchdog and receives
fresh RSS sampling instead of inheriting the first attempt's identity. It then
rebuilds native skill projection and MCP Tool Search state rather than replaying
on the session that overflowed. The `_pid` clear is an ordinary
counted site in `test_pid_reader_ratchet.py`, not an owner-marked one: the
marker is reserved for the module that owns a runtime and the function that
ends a process, and retiring a stale reading is neither.

The retry is one-shot (`_context_overflow_retry_used`). A second overflow,
which can only come from the forced-dedicated replacement, or any overflow after
observed activity is terminal. Partial text is preserved as the result, and the
error names the reason replay was withheld, using the same side-effect reading
as the unexpected-cancel gate below: when a tool ran (`tool_count > 0`) the
error says replay could repeat side effects, because the fresh runtime has no
ledger of what the tool changed; when only text, a result or a completed turn
was observed, no state changed, so the error says the produced work was
preserved and replay was withheld to avoid duplicating it, without claiming a
side effect. Replay stays withheld in both cases; only the stated reason differs.

## Unexpected-Cancel Recovery (one-shot auto-continue)

An unmarked `CancelledError` (see intentional-cancel rule) triggers `_schedule_cancel_recovery`: exists for cancellations arriving from outside the manager's lifecycle (parent task-tree teardown around a live subagent), mirroring the main path's PR #173 recovery. Mechanics:

- **Side-effect gate**: recovery fires ONLY when `tool_count == 0`. The respawn runs on a fresh session with no ledger of prior tool calls, so once any tool has executed the model cannot verify which side effects already happened — the run is finalized instead (error `"cancelled (auto-continue suppressed: tools already executed …)"`, partial output preserved and delivered). Text-only activity is safe to resume.
- One-shot: gated by `info._cancel_retry_used`; the recovered run's own cancel is terminal.
- **Ending claim**: a whole answer — a successful complete event, post-processed into `result` with no await since — takes `info._ending_claimed` synchronously unless a stop got there first (`done`, `_reap_started`, `user_stopped`, a stamped `error`), and records the success stats there. From the claim on, the run ends completed whatever lands: `cancel()`, `cancel_for_teardown` and `_force_reap` (the reaper's too) treat the claim as `done` and stamp nothing, and a cancel, the shutdown or the deadline only cut the tail short — the result write is drained, `done` is set in its `finally`, and every arm of `_run_impl` treats an already-set `done` as first-arrival-wins, its log line saying the run completed. A respawn would re-run finished work, and a failure would discard a whole answer. Because nothing else can end it, the claimed result write is bounded by `_STATE_DRAIN_TIMEOUT` even uncancelled: past it the worker finishes detached under the `_abandoned_state_writers` hold, `done` is set and the flag lands late or not at all, so a wedged filesystem holds the run, its lane slot and its parent for seconds, not until the run deadline. While a claimed run is not yet `done`, `settle_before_delete` answers `pending` (the DELETE route's 409 `completion_delivery_pending`), since its report does not exist yet. A whole answer a stop got to first claims nothing and counts no success, so the stop owns its ending: under a reap in flight the tail records nothing and `_run` writes the reap's ending with the reap-echo arm's own record (`_record_reap_ending`: neutral for a user Stop, a parent end or a stage cancel, a failure naming the deadline otherwise) and leaves the report to the reap, its record naming how far the answer got (`SubagentInfo._answer_finished`, set by the tail: "the run was stopped as it finished its answer" after a complete event, "the runtime was torn down before the run finished" for a stream that just stopped, which also classifies as a normal end of turn; `test_a_reap_in_flight_names_whether_the_answer_had_finished`); a user stop no reap has reached yet records the neutral stop, since `cancel()` stamps `user_stopped` synchronously before it awaits its reap (`test_a_user_stop_no_reap_has_reached_yet_records_the_neutral_stop`); a run another path already ended keeps that record and counts no success (`test_a_run_another_path_already_ended_counts_no_success`). None records `result_complete` (see Gateway Restart Reconciliation). Recording a bare `done` there let the reap's record guard skip, so a deadline-stopped run was reported completed and cleared its session's failure count (`test_a_deadline_reap_that_got_there_first_owns_the_ending`). The per-turn usage row is read synchronously and appended on a task held in `_report_tasks`, so no ending waits for it and `cancel_all()` drains it within its report bound.
- Explicit handshake: `_resume` awaits the ORIGINAL task's full teardown (session release/reset, slot decrement, registry pop) before respawning — never a timed sleep.
- The original task ends CANCELLED: after scheduling the recovery, `_run`'s CancelledError arm re-raises (its finally still runs and reports nothing while `_recovering`). A waiter holding the original task handle therefore sees the cancel (`await task` raises `CancelledError`, a `wait_for`/`timeout` that fired raises `TimeoutError`) instead of a clean completion while the run has moved to `"{id}:recovery"` and has not yet acquired its session (#12607). `_resume` waits with `asyncio.wait`, which is indifferent to how the task ended. The terminal arms (user stop, shutdown, spent one-shot, tool-activity suppression) still return normally.
- Slot re-acquisition: waits (bounded, `_RECOVERY_SLOT_WAIT_SECS`) for free capacity; the slot claim and `create_task` are ATOMIC (no await between) so a concurrent `_drain_queue` cannot overshoot `max_concurrent`. The dispatcher honours the same invariant from its side with reserve-then-commit: when `spawn_impl` stops at the store claim (`ClaimPoint`) it has ALREADY taken the slot (`_running_count += 1`, stagger token) synchronously, the claim is awaited on the writer thread, and the re-entry (`_claimed=`) consumes that reservation instead of counting again; a claim the store refused or could not take, or a refusal at re-entry, releases it (`release_reservation`). So nothing admitted during the await -- a `spawn`, a `resume_grant`, a recovery re-acquisition -- can take a slot the dispatcher is about to use.
- Shutdown-reachable: the pending `_resume` task is registered in `_tasks` under `"{id}:recovery"` so `cancel_all()` cancels it; a cancelled recovery finalizes the record terminally and never respawns.
- Failed recovery (no slot / teardown timeout) still fully finalizes: `subagent_done` emitted, tombstoned, delivered via `on_done`.
- Replay-safety at respawn: when the first attempt streamed partial text, the respawned prompt is prefixed with `_CANCEL_RESUME_PREFIX` so the model continues instead of restarting (the prefix also gates on `tool_count` as defense-in-depth, though the side-effect gate above means a tool-activity run never reaches respawn). A bare original prompt is re-sent only for a zero-activity first attempt.

## Reaper Loop

`start_reaper()` launches a periodic loop (60s interval) that force-kills subagents exceeding the configured timeout deadline. Defense-in-depth for cases where `asyncio.wait_for` fails to fire due to event-loop saturation or orphaned tasks.

- `_reaper_loop`: sweeps every 60s, calls `_force_reap` on expired agents
- **taskq pump** (`OrphanStallMonitor.taskq_pump`, facade `_taskq_pump`): `start_reaper` runs it once after `taskq_boot_dispatch` (building the manager's `DependencyCoordinator` from `agent.dependency_*` over the admission store, `capacity = _max_concurrent`, and running `coordinator.rebuild()` after `open_default_store` ran `WaitLedger.rebuild()`), every sweep re-runs it as the backstop, and every run that parks on a wait calls it. One pass = `admission.taskq_expire_waits()` (wait deadlines) + `coordinator.tick()` (due scopes) + a one-shot `loop.call_later` re-armed at `coordinator.next_deadline()`, so a scope is woken when it is due, not on the next 60s sweep. The coordinator is registered process-wide (`taskq.dependency.register_coordinator`) for the main chat's read of scope schedules. Terminal runs call `coordinator.forget(id)` from `_run`'s finally (a finished probe is the scope's recovery signal) and withdraw any pending resume entry.
- `_force_reap`: ONE reap per run -- the first caller runs it (`_reap_once`) behind a per-run future in `_reaps_in_flight`, settled on every exit; a second stop path arriving while it is pending (a dashboard Stop racing a deadline reap, a parent-end cancel racing either) joins that future (shielded, so a cancelled joiner leaves the reap untouched) and returns once the record is final -- written, audited, the report released: `_reap_once` settles the join there, not when the report's delivery returns, because that delivery is an unbounded shield over a parent injection capped at `_ON_DONE_TIMEOUT` (20 min) and the joiner may be a Stop request or the reaper's own sweep, which must not stand behind one run's delivery once the kill is decided and written -- and a caller arriving after the reap finished finds `_reap_started` set and does nothing -- because two reaps over one run both retained, reset and killed, and a failed kill was appended to the record twice while only the first reap published, so the record on disk and the completion the parent received disagreed. A Stop that joins stamps nothing (`cancel()` leaves `user_stopped`, `_reap_reason` and `_stop_origin` alone once `_reap_started` is set): the record follows the FIRST stopper, and `outcome` reads `user_stopped` directly, so a late stamp would publish a claimed deadline failure as a neutral stop. Then, under the key's ENDING FENCE (`process_identity.ending_fence` → `SessionManager.ending_key`, raised synchronously before the first await and held through the record, the audit and the release -- the cron reaper's shape, and its second consumer): take the process handles (every process the key names) → reset with 30s timeout → SIGKILL fallback on a hang, a raised reset or a survivor → name a start past its spawn door (`process_identity.spawn_in_flight` → `SessionManager._spawn_in_flight`, read after the passes with the fence still up) → mark done → audit on the kill's result → fire `subagent_done` WS event. While the fence is up a claim or a cold start under the key -- a queued `spawn_continue` on the conversation key, a parent turn re-claiming it -- is HELD at the door of `get_or_create` and lands only once the run is recorded, and a start already inside `provider.start()` when it went up (nothing published for the snapshot to see) is refused at registration, its provider hard-killed by the allocation path, its call allocating again after the lift; the record names it (`…; kill failed: a cold start under the key was past its spawn door when the run was ended (fenced: <reason>); not signalled`) and audits `failed`, never `reaped` over a process that then ran on holding its turn permit with nothing left to reclaim it (the run's own teardown is skipped once `reaped` is set; the idle sweep skips a held session; the startup-stall reap fires exactly while such a start is in flight). A session manager without the fence (a test double) is not fenced: the passes and their post-pass read are the whole answer. **The force-stop path records what actually happened to the process, in the cron reaper's vocabulary** (the two paths must not diverge): `_sigkill_session(session_key, handle)` returns `None` once the process group has been signalled or there was nothing to kill, and otherwise the failure it hit — the broadcast guard's refusal of the group (`ValueError: kill_process_group: refusing …`), a group kill that raised with no pid-scoped fallback landing (`PermissionError`; on Windows a pinned tree drain that raised or could not pin a pending tree, since the drain is the only tree walker there and nothing signals the root alone), a live pid whose identity cannot be confirmed as the run's (no start id recorded, or none readable now), or an error in the kill path ahead of the signal — and never raises, because its caller owns a teardown it must still finish. `_force_reap` folds that into the run's error text (`…; kill failed: <reason>`, appended after its own synthesis and before the tombstone; a user stop keeps `outcome == "stopped"` and gains the text; the joined field is held to `MAX_ERROR_DETAIL_LEN` at retention by `process_identity.with_kill_failure`, the one spelling of the suffix both supervisors write (the cron reaper's `last_error` goes through the same call) -- the reason keeps at least `KILL_FAILURE_RESERVE_LEN` and the run's error is trimmed to make room, because a failed Windows `taskkill /T` carries one stderr line per process the run spawned and the error it joins may already fill the bound) and audits `reaper_force_kill` as `failed` rather than `reaped`, with the reason on the row's `kill_failed` metadata (held to `MAX_ERROR_DETAIL_LEN`). **A record another arm wrote ahead of the kill's decision is re-written only while that arm left the report to the reap**: the run whose stream died under the reap's reset (the reap-echo arm, finalize token not taken) gets the failure appended and its tombstone re-written under the same cause before the reap publishes; a run that finished on its own inside the window and published ITS OWN report (`_run` took the finalize token) keeps the record it delivered -- re-writing it left the tombstone on disk saying `failed` against a completion the parent had already received, with nothing to re-reconcile the two -- and the failure lives on the audit row and in the log; `_teardown_run_session` (the run's own `finally`) audits `run_finally_force_kill` as `failed` rather than `sigkill` with the reason in the row's `error`, AND folds it into the run's record ahead of the report: the failure joins `info.error` through `process_identity.with_kill_failure`, a tombstone the run's own arm already wrote (an error, a timeout, a cancel) is re-written under its cause with the failure in its `detail`, and a completed run -- which has no tombstone yet -- gets none here, so its folder stays in orphan reconciliation (`list_orphans` skips any folder with a tombstone), which is what ends the survivor at the next start; a teardown CANCELLED inside its reset, probe or signal (a gateway shutdown cancelling the run task) records the undecided kill the same way (`…; kill failed: CancelledError: the teardown was cancelled before its kill decided`) before re-raising, unless a reap started and owns the record (it consumed the retained handle before cancelling, and its own arm names the kill), because the gate below is released by the `finally` whatever the teardown did and a clean record there published a success and a `delivered` tombstone over a process nothing had stopped; the report the run's `finally` spawned AHEAD of this teardown waits on `teardown_done` (set in that `finally` whatever the teardown did; the wait is a grace of `_RESET_TIMEOUT + _TEARDOWN_REPORT_GRACE`, which bounds the teardown's RESET half only -- the kill half's executor hops are sequential over every handle and queue behind whatever closes are wedged there) BEFORE it reads the record, so the `subagent_done` event, the completion the parent receives and the delivered-or-not decision all see the kill's outcome -- and a wait that runs out with the kill still undecided publishes the record with the kill named undecided (`…; kill failed: the teardown had not decided its kill 60s after the record; not confirmed`), never clean: `outcome` reads `failed`, `mark_delivered` is skipped, and the teardown's eventual decision joins that record, where a clean publication wrote the `delivered` tombstone that the decision could never re-write and the survivor was recorded nowhere; `outcome` reads `failed` and `mark_delivered` is skipped for a run whose process the kill left standing, where publishing first handed the parent a clean completion and a `delivered` tombstone that hid the process from reconciliation for good. Nothing to kill is not a failure — no pre-reset handle, no usable pid, a leader already exited or recycled at either read or gone at the signal itself, whose tree is gone too — and keeps `reaped` / `sigkill`. **The kill handle is taken BEFORE the reset, retained under the run's id, and is the only thing that names the process**: `SessionLifecycle.reset` pops the session from the map under its lock before the awaits that can hang, and the common shape is the run's own `finally` being the reset that hangs while the reaper (a deadline, a user Stop) arrives to act on it — so a handle the reaper took before ITS reset would see an empty map. Both callers therefore go through `_retain_process_handles(agent_id, session_key)` immediately ahead of their reset, which returns EVERY process the key names at that point, the run's own first, each as a kill handle (pid, recorded start id, the process group it leads while its identity holds, child records — `kiro_crew.process_identity.ProcessHandle`, read by `process_handle_of`, which also reads the harness pid off an owned `Popen` when the provider has no client-recorded pid): the entry the other path RETAINED in `_process_handles` under the run's id before its own reset, every session the session manager is tearing down under the key (`SessionManager.tearing_down`, the list in pop order, for a reset started outside the two paths -- two teardowns can hang under one key when a cold start registered a successor while the first hung and the successor's own reset popped it too, and each is a process of its own; each entry is a `session_lifecycle.TornDown` record and the kill handle is the entry's `handle`, the identity captured AT THE POP -- never a re-read of the popped session, whose pid the hung teardown clears in its own awaits with the process still standing, so a snapshot that read the record as a session named no pid, dropped the teardown's process, and recorded `reaped` over it), and the session still live under the key — the run's own, or a successor a cold start registered during the other path's awaits, which the reap's reset pops too and which is therefore verified and killed on its own handle, read now, before the reset pops it (preferring either alone would leave the other's process unrecorded); distinct processes only -- one pid under one start id is one handle however many sources name it, its child records and captured group the UNION of every source's reading (`process_identity.add_handle`: the entry the other path retained names the children the client had recorded then, the live session's handle names the ones recorded since, and keeping the first reading alone dropped the later children from the sweep while the record said `reaped` over them), while the same pid under two DIFFERENT start ids is two processes and BOTH handles are kept (the later reading names the live successor the pid was handed to, the only identity the kill can verify and signal; the earlier names a leader that exited, and what it left behind is reached only through that handle -- on Windows the exact-tree cleanup pin reserved under (pid, old start id), keyed by identity and so still pending beside the successor, on POSIX the group id the leader led; keeping only the stale handle let the successor run on under a `reaped` record, replacing it with the later reading let the old tree run on under one) -- and a session with no recorded pid names no process. What the path holds is recorded back under the run's id, so the other path finds it on its miss; the map is read once, there, and never after a reset — a session under the key afterwards is a successor registered during the reset's awaits, whose teardown is its own. The entry is cleared once the path holding it has decided (the handles were consumed by the kill, or the survivor check found the processes gone); the run's `finally` clears in a `finally` of its own, so a teardown the reaper cancels mid-reset leaves nothing behind. **The snapshot is not the last word: each reset runs under a caller-owned teardown scope** (`process_identity.teardown_capture` → `SessionManager.teardown_scope(on_pop=…)`, the one definition the cron reaper uses too) whose hook takes the handle of the exact session the reset pops, in the same registry-lock hold as the pop -- because a cold start (a queued `spawn_continue` dispatching as the run's record flips) can register a successor under the key between the snapshot and the pop, and it is THAT session the reset then pops and hangs on: unnamed by the snapshot, so a fallback fed the snapshot alone killed the stale process and recorded the run `reaped` while the successor ran on. The kill set (`process_identity.kill_set`) is the snapshot plus the popped session's handle, one per process incarnation (keyed by the handle, never the pid; a popped handle EQUAL to a snapshot handle is the same incarnation read later and its child records and group join that entry rather than being dropped with it); the scope is handed to `reset` only by a manager that has the torn-down table (`tearing_down(key)` answers a list -- a scope is a hold on that table), so a double that answers every attribute produces no scope, gets the plain call, and the snapshot alone applies, as for a manager without scopes; a popped session with no recorded pid that the snapshot NEVER SAW (`_sessions_under`, the session objects compared by identity) is a process the run cannot name -- a cold start registered after the snapshot and still spawning -- and is a named kill failure (`process_identity.POPPED_WITHOUT_HANDLE`: `the session the reset popped had no process handle yet; not signalled`), never `reaped`; one the snapshot DID see names no process now as it named none then -- a provider that runs no process of its own, a client already reset -- and is nothing to kill, because calling it a failure audited every such run's ordinary teardown as `failed`. Several candidates are killed one by one (`_sigkill_sessions` → `process_identity.kill_each` over this manager's `_sigkill_session`) and their failures joined with `; ` into the one record (`process_identity.join_failures`, the joiner the kill itself and the cron reaper use). The glue -- kill set, capture scope, the kill loop, the joiner, the fence, the spawn-in-flight read -- has ONE definition in `kiro_crew.process_identity` for both supervisors; what stays here is the single-handle kill (`_sigkill_session`: this manager's log prefix, and the seam its tests drive) and the record the failure lands in. **The kill and the survival check are `kiro_crew.process_identity`'s, the one home shared with the cron reaper, so the two paths cannot diverge**: `_sigkill_session` hands the handle to `process_identity.kill_verified_process` (with the client's child helpers resolved through `session.child_process_helpers` at call time), which reads the root's start id (`platform_compat.get_process_start_id`, the recycling detector) against the recorded one both before anything reads through the pid and again immediately before the signal — not through the child verifier, which denies a root that has no recorded basename — and, for a pid whose identity does not read, asks `platform_compat.pid_exists` (on Windows the exit-code-confirmed `OpenProcess` + `GetExitCodeProcess` probe, because a start id reads back for an EXITED process while any handle to it is open). A leader found gone at any read, or at the signal itself, hands the decision to its TREE: on POSIX the group it led is signalled by the id retained while it was alive once a verified member vouches for it, an empty group is nothing to kill, a live group that cannot be verified as the run's is a failure, and a child the walk itself found under a leader that exited before the signal is named in the failure, unattributed and not signalled (it was read through a pid that may not have been the run's any more), never `reaped` over; on Windows a pending exact-tree cleanup pin is drained and only a completed drain is a kill. A reset that returned — `True`, or `False` for a key the other path had already popped — is not proof the process is gone (its own shutdown can fail without raising out of it; a `False` one stopped nothing), so both callers ask the handle afterwards (`process_survived_async`, the probe run off the loop as the cron reaper runs it: gone only on evidence — no usable pid, or a leader that is gone by identity AND exit-code-confirmed liveness whose tree is gone too; an existing pid whose identity cannot be read, or a gone leader whose tree still stands, still stands and the kill then decides) and run the fallback for a survivor, whose outcome is what the record says.
- **Startup ends at a runtime PID or at the first answer on the run's own session -- never at an opened stream, and never at a co-tenant's frame.** A provider may create its child process lazily from `stream()`, so a missing PID before the first response is not proof that execution never started -- but an opened stream that has yielded nothing is not proof of progress either. `_leave_startup` stamps `_first_stream_started` (and, on a first turn, wakes the queue) at the two moments the backend is shown to have answered the prompt: the first frame out of the stream that is addressed to THIS session, taken on the raw frame so a completion withheld for in-place recovery counts; and a dependency verdict on the prompt (`_yield_for_dependency`), the step that also takes the durable row from `starting` to `running`. The same provenance test gates the stream's `running` mark (`ensure_running_marked`), so the durable row and the in-memory startup state leave `starting` on one predicate. A `runtime_global` frame -- an ownerless frame a shared runtime fanned out to every co-tenant (`fanout_no_owner`, below) -- is another tenant's traffic and ends neither. So a stream that opens and hangs before any answer keeps the run in startup (`_in_startup`) and reapable (`TestStreamOpenIsNotProgress`) -- once per execution: after a first answer or a dependency park, a later stream of the same execution that hangs is outside startup and is bounded by the run's ordinary deadline; a roster broadcast from a co-tenant leaves it there (`TestOnlyThisSessionsFrameIsItsAnswer`, driven through a real `AcpRuntime` reader loop and a real `AcpSessionHandle`); and a start parked on a provider throttle before its first frame is out of startup for the whole wait, which its dependency scope bounds and ends in its own error (`test_a_throttle_before_the_first_frame_ends_startup_at_the_park`). **Which starts this governs:** the startup predicates read the marker only while `_pid` is None, and every AcpRuntime-backed start records the PID its runtime reports before its stream opens (a runtime reports one from spawn until its process is reset, and a session is created only on a spawned runtime) -- `get_pid` after `get_or_create` on the dedicated path, `runtime.pid` in `_bind_shared_handle` on the shared path (`TestAPidEndsStartupBeforeTheStream`) -- so for those the startup window closes at the PID and never reaches the turn's first frame. The first-answer edge is the startup exit only for a provider that publishes no PID at acquisition. For such a provider the window IS a first-output budget: the recorded live turns in `test/fixtures/acp_frames/` (kiro-cli 2.21.4, the KAS 0.63.3 relay, claude-agent-acp 0.76.0) open with model output -- a tool-call chunk or an `agent_message_chunk` -- and carry no turn-start frame; `_kiro.dev/metadata` yields no event. The startup reap's error and the reaper's warning name how many `runtime_global` frames the start received while in startup (`_startup_cotenant_frames`), so a start that saw only co-tenant traffic reads apart from one whose stream stayed silent. The marker and that count reset for every recovery execution; the startup watchdog may reap only a subagent with no answer on its own session, no runtime PID, and no completed turn. The ordinary wall-clock deadline remains unchanged.
- **A start whose runtime is up but whose first prompt gets no answer is reaped at `_FIRST_PROMPT_SILENT_SECS` (300 s), not at the run deadline.** The startup watchdog stops at the PID, so a runtime that finished its handshake and then wedged on its first prompt used to run to the full `subagent_timeout_secs`. `OrphanStallMonitor._is_first_prompt_silent` closes that gap: `_exec_started` and `_pid` set, `turns == 0`, no `_first_stream_started`, not queued for a start permit (`_gate_wait_started`), not parked on an approval, and the activity clock `last_activity` still for longer than the window. `_note_startup_progress` restarts that clock when the PID is recorded, so a long `session/new` handshake is never charged to the first-prompt window. The reap uses the same `startup_timeout` cause and death-log path; its error reads `Runtime launched but its first prompt got no answer within 300s` because a PID is recorded. Pinned by `TestFirstPromptSilent` and `test_reaper_sweep_reaps_a_runtime_that_never_answers_its_first_prompt`.
- **The startup deadline is fixed; the clock pauses while queued.** `_is_startup_stalled` compares `now - _exec_started - _start_queue_wait_ms` against the bare `_startup_deadline` however many other agents are `_in_startup`. Its size follows the start budgets instead, and covers every phase the clock RUNS through: `_STARTUP_HANDSHAKES` (two) rounds of the spawn's own `initialize` budget (`_INITIALIZE_TIMEOUT`) plus `agent.session_start_timeout_secs`, then the late-start collector's wait (`agent.start_collect_timeout_secs` plus its grace), plus a launch margin, never below `_STARTUP_TIMEOUT_SECS`, read from the live config snapshot. The `initialize` term is what keeps the watchdog from reaping a start whose own budgets have not expired, because one clock spans spawn, `initialize` and `session/new`; it counts twice because a retried start runs two rounds (the companion spawn's dead-runtime retry, the spawn's one re-derive retry, a resume whose runtime died respawning before `session/new`, a re-projected claim). The watchdog stamps that value onto each start clock it sees, so a config write moves only the windows of starts that begin after it. The crowd is handled at the two ends of the start, not in the deadline: the clock pauses while the start is queued for a permit -- at all three start queues on the dedicated path (cold-start semaphore, spawn admission, `session/new` gate), on the shared path at the gate and, when it needs a companion runtime, at that runtime's per-parent lock and its spawn's admission; `_gate_exit_reset` adds each wait to `_start_queue_wait_ms` -- so the deadline measures time spent starting until the start's exit (its runtime PID, or for a start that publishes none its first answer), and the in-startup population is bounded at admission (`_startup_cap`). The paused total is bounded as well: past `monitoring._START_QUEUE_MAX_SECS` (a capacity cap of its own, not `agent.subagent_queue_max_wait_secs`, which bounds only a memory deferral before the start) the run is reaped as "Never started: start queues saturated", because a start parked behind holders that no watchdog bounds (a cron run, a workflow stage, the task runner, a pool fill) otherwise waits with its clock paused until the run's own `subagent_timeout_secs` ends it -- which reports the turn deadline rather than the queue that held it. That holds while `subagent_timeout_secs` is above the cap, as the default is; a run configured at or under it ends on its turn deadline first. The deadline is deliberately NOT pressure-aware (no term per other agent in startup): with queue time uncharged and the crowd bounded there is no evidence that a healthy start misses the base deadline, and a term sampled at sweep time against a clock spanning the whole crowded period would not be monotonic -- it would shrink as the crowd drained and could reap at one sweep an agent the sweep before had left inside its window. The reaper's warning names the in-startup population, as diagnostics only. Pinned by `test_subagent_startup_pressure.py` and `test_subagent_startup_watchdog.py`.
- **Terminal completion is arbitrated by FOUR separate guards, not by `reaped` alone.** Two paths can finish a subagent — `_force_reap` and `_run`'s `finally` — and between them there are four distinct one-time concerns. Earlier revisions tried to arbitrate them with `reaped` plus `done` and every attempt satisfied two while breaking a third (duplicate delivery when the marker was set late; a lost outcome when it was set early and the reaper was cancelled; a lost outcome when the claim was handed back to a run that had already exited; and finally **no reporter at all plus a leaked concurrency slot** when the report claim was gated on `not info.done`). The guards are now:
  1. **`info.reaped` — classification.** Was this a deliberate reap? The cancel-recovery scheduler reads it, and the marker MUST precede the intentional cancel (see the intentional-cancel rule above) or an unexpected-cancel respawn fires on the run being killed. Unchanged.
  2. **`if not info.done` — the terminal RECORD.** Error synthesis, failure stat, tombstone, cost. First-arrival-wins, so it is never written twice (pinned by `test_subagent.py::TestOnDoneTimeout::test_force_reap_skips_tombstone_when_already_done`).
  3. **`_release_slot(info)` — SLOT accounting.** A one-shot token per `SubagentInfo`; the winner decrements `_running_count` once and drains the queue. Deliberately independent of both flags above: inferring slot ownership from `done` or `reaped` produced a double decrement in one interleaving and none at all in another. A leaked slot permanently starves the spawn queue, which matters far more at the 60-100 concurrent agents the scale work targets. The cancel-recovery respawn **re-arms** this token when it re-admits a slot (`_running_count += 1`), because the respawned run occupies a fresh slot and needs its own release.
  4. **`_claim_finalize(info)` — REPORT ownership** (`subagent_done` + the `_on_done` injection, plus wave-digest settling and the result.txt TTL bookkeeping). Granted to exactly one caller; contains no `await` so the check-and-set is atomic on the loop. It does **not** consult `info.done` — that was the last defect. It returns False while `_recovering` *without consuming itself*, so a pending respawn is not reported done and its respawned run can claim later.
- **A claimed report is atomic, not merely exclusive.** The claim alone still lost outcomes when the claimer was cancelled mid-report. `_report_terminal` therefore runs on a strongly-referenced task under `asyncio.shield`, spawned by `_run` **before** its teardown awaits so the task is already live wherever a cancellation lands; the caller still receives `CancelledError` while the report completes. `cancel_all()` drains outstanding reports with a bounded timeout and then **cancels and gathers** any straggler, so none is left invoking `_on_done` against tearing-down state or killed by a closing loop. Because the awaiter is shielded, shutdown is bounded by that drain rather than the `_ON_DONE_TIMEOUT` injection cap. Enforced by `test_subagent_reap_race.py`.
- **Finished-run deletion waits for its report.** Explicit finished-run deletion (`settle_before_delete`) first waits for any active terminal-report task of that run, then records the panel dismissal and removes the record.
- **An undelivered report abandoned at shutdown is made RECOVERABLE, not silently dropped.** The terminal record — including the tombstone — is written before delivery is attempted, and a tombstone is exactly what `list_orphans()` uses to EXCLUDE a folder from the next start's reconciliation. So cancelling a still-pending report at the drain deadline would leave an outcome that was never injected *and* invisible to the only path that could still inject it. `cancel_all()` therefore calls `clear_tombstone(id)` for each report it cancels, re-admitting that agent to the next start's orphan reconciliation (which finds `result.txt` and re-delivers). Extending the drain to `_ON_DONE_TIMEOUT` instead was rejected: it would hold gateway shutdown for up to 20 minutes on one wedged injection, which is the exact failure the bounded drain exists to prevent. Only reports cancelled **before** `_on_done` returned are re-admitted — `info._reported_to_parent` is set the moment the injection returns, so a cancellation in the later teardown/tombstone waits cannot cause a duplicate delivery on restart.
- **Every reporter goes through the claim — including cancel-recovery failure.** There are more terminal paths than the two obvious ones: when a cancel-recovery respawn cannot happen, its `except` arm also finalizes the agent. That site previously fired `subagent_done` and `_on_done` directly, gated only on `done`/`reaped`, so a reaper racing a failed respawn delivered the outcome twice. It now takes `_claim_finalize` like every other reporter and reports through the shielded helper (which matters because `_force_reap` cancels that very task). `_resume_guarded`'s CancelledError arm writes only the RECORD and deliberately never reports — during shutdown the drain owns delivery.
- **The reaped marker and the recovery cancel precede every `await` in `_force_reap`.** Both used to sit after the session teardown, which yields for up to `_RESET_TIMEOUT` (longer on the SIGKILL path). A recovery task whose bounded handshake expired inside that window observed `reaped == False` and respawned the run being killed — tools executing after a user Stop, strictly worse than a duplicate report.
- **The reap's report exists before any point the reap can be cancelled at, and publishes only once the record is final.** A gateway shutdown runs `cancel_all()`, which cancels the reaper task while `_force_reap` awaits the reset or the fallback kill after it -- and, beside it, every reap running OUTSIDE the reaper task: a dashboard Stop or a parent-end cancel each run `_force_reap` as a task tracked in `_reap_tasks` and awaited in the caller's own task (so the caller's cancellation still reaches the reap), and `cancel_all` cancels and gathers those first, because a reap the shutdown never touched sat in its hanging reset with its report waiting on a gate nobody released until the gateway's shutdown budget (`GRACEFUL_SHUTDOWN_SECS`, 10 s, in `gateway_shutdown_budget.py`) hard-exited the process ahead of the report drain (`_REPORT_DRAIN_TIMEOUT`, 30 s, in `subagent.py`) (the tail also tolerates a gate the drain already cancelled: nothing is released, the report's ownership is dropped, and the drain's `clear_tombstone` has re-admitted the run to orphan recovery). By then the reset has usually already killed the run's runtime, so the run's reap-echo arm has written the record and the tombstone and left the report to the reap. A report the reap launched only after that window was a report no cancellation point inside it could reach: a `CancelledError` let straight out left nobody to report, and the tombstone already on disk excluded the folder from the next start's orphan recovery — the completion was gone for good. So `_force_reap` now spawns the shielded report task FIRST, before its reset and kill awaits, strongly held in `_report_tasks` (drained by `cancel_all()`, re-admitted to orphan recovery if that drain has to abandon it), gated on a future it resolves at the end: `True` once it holds the finalize claim and the record is written (the payload is built from `info` only then, so the parent is never told the run was reaped before the kill decided), `False` when the claim went to another path that reports instead (the run finished on its own inside the window) — a dismissed task is disowned from `_report_owners` before it is released, so its silent exit latches no delivery failure. The teardown awaits sit in a `try` whose `CancelledError` arm treats the kill the reap did not get to decide as a FAILURE the record names (`…; kill failed: CancelledError: the stop was cancelled before its kill decided`, audited `reaper_force_kill` = `failed`, never `reaped`: the process may well be alive), marks `reaped`, cancels the run task, writes the record and the tombstone, releases the report, and only then re-raises — without awaiting the report, since the caller is inside `cancel_all()`'s own gather and a shielded await there would hold shutdown for the injection cap the drain exists to bound. Pinned by `test_subagent_force_stop_audit.py::TestAStopCancelledMidTeardownStillReports` (a cancel inside the reset, one inside the kill, and one landing before the run's own stream has died).
- **The report trails teardown.** Spawning the report ahead of teardown opens a window the older ordering did not have: publishing before the session is torn down handed the parent a completion that could not name a process the teardown's kill then failed to end, and writing the "delivered" tombstone before then hid that survivor from orphan reconciliation. The report therefore waits on a `teardown_done` event (set in `_run`'s `finally`, so it fires even under cancellation, and bounded so the report can never wedge) BEFORE it builds its payload: the `subagent_done` event, the parent's completion and the delivered-or-not decision all see the teardown's outcome (a kill the run's `finally` could not deliver sits in `info.error` by then, and such a run is not marked delivered). A reaped or recovery-failed member still settles its **siblings'** digest holds, since those siblings' results did reach the parent even though this member's did not. On the dashboard routes the report's own settle and `mark_delivered` are no-ops by design: `_subagent_done` defers the delivery bookkeeping — the completed member's own tombstone AND any held wave siblings — to the parent's CONSUMPTION of the announce via `_defer_queued_delivery` (the #4839 content-keyed slot ledger + `_delivery_queued`), on the queue branch settled by the drain and on the direct-injection branch by `_arm_queued_delivery_settlement` armed on the injection task (#2233). A bare `_on_done` return is a local routing success, not evidence the parent received anything; an unconfirmed hand-off leaves the debt parked and orphan-recoverable rather than tombstoned.
- **A synthesized reap error names only the cause the observed state supports.** The wall clock fires at the configured deadline, but a run parked on a never-answered spawn approval has reached no execution deadline: `turns == 0`, `_pid is None`, `_exec_started is None`, and the dashboard's approval window is still open. So `_force_reap`'s error synthesis tests `_awaiting_approval and _exec_started is None` **first** and reports the unanswered spawn approval, before the `startup_timeout` and generic-deadline arms. The predicate is captured **above** the intentional cancel, because the flag's owner clears it in a `finally` the cancel schedules; reading it at the record site would hold only while no `await` sits in between.
- `_sigkill_session(session_key, handle)`: best-effort SIGKILL when graceful reset hangs or did not end the process; acts on one pre-reset handle only (`_sigkill_sessions` runs it per candidate and joins the failures), returns the failure it hit instead of swallowing it (contract in the `_force_reap` bullet above). It releases the run's own lease first, then passes the runtime-ownership gate (`authorize_runtime_kill`) and the teardown barrier ([runtime ownership](runtime-ownership.md)); a gate refusal (another session's lease or a tenancy) or a barrier refusal (a tenant arrived after the gate allowed) sends no signal and is returned as a kill failure, so the record never says the run was reaped
- After decrementing `_running_count`, `_force_reap` calls `_drain_queue()` so the freed slot immediately starts a queued spawn. Normal completion pumps the queue via its `finally` block, but that block is gated on `not info.reaped`; a reap sets `reaped=True` and decrements the count itself, so without this explicit drain a queued spawn would sit stranded until an unrelated agent finished or a new spawn arrived.
- Wired up in `slack/gateway.py` after `SubagentManager` init

### Idle-Stall Detection

The main-agent watchdog stack (`tool_stall_suspect_secs`) does **not** govern subagents; `_maybe_flag_stall(agent_id, info, now)` (called from the reaper sweep) is their equivalent. It does, however, consult the *same* liveness oracle (`acp/liveness.py`) — see the attribution note below. Each **session-scoped** stream event calls `_touch_activity(info)`, which updates `info.last_activity`, clears a prior `stalled` flag (re-emitting `subagent_stalled {stalled: false}` when work resumes), retires the agent's oracle and bumps `info._stall_gen`. `info.last_activity` is (re)initialised to `_exec_started` at the top of `_run_inner` so a queue / spawn-approval wait is never counted as idle.

Event kinds are NOT the discriminator: the same `EVENT_SUBAGENT_LIST` also reaches a session through the routed KAS sub-agent lifecycle path (`_handle_kas_subagent`, off a `session/update` frame), where it IS that session's own progress -- excluding by kind would falsely badge a working KAS agent. Provenance is carried instead: `AcpRuntime._reader_loop` sets `JsonRpcMessage.fanout_no_owner` when it fans an ownerless frame out to MORE THAN ONE registered session (a lone session is the sole owner, so it stays unmarked), the dispatch loop copies that onto `AcpEvent.runtime_global` for the notice kinds such a frame yields (the roster, the compaction, clear and agent-switch notices, the steer echoes; the MCP registration notifications use the stricter must-name-this-session test; a `session/update` is routed by its `sessionId`), and `_run_inner` skips the refresh only for a `runtime_global` event. Everything else stays fail-open: an event kind the dispatch switch does not special-case still counts as activity, so a new session-scoped kind can never invent a false stall.

Why the exclusion exists: `_kiro.dev/subagent/list_update` carries no `sessionId`, so the runtime broadcasts it to *every* session queue, and under `agent.session_sharing` one roster notification lands on every co-tenant subagent's stream. Counting it as activity refreshed `last_activity` for a whole batch of wedged subagents at the same instant and cleared their badge, so the badge flapped and the reported `idle_secs` measured time since an unrelated agent's roster churn (`#4841`; the plateau measured in `#2854`).

Per sweep, for an agent that has actually started (`turns > 0` or a live `_pid`) and is **not** blocked on a human approval prompt (`_awaiting_approval`):
- `idle > _stall_idle_secs` and not already flagged → consult liveness (below), and unless the verdict clears it, set `info.stalled = True`, emit `subagent_stalled {stalled: true, idle_secs}` (surface-only; the card shows a "no activity" warning), and append a record of the slow command to `~/.kiro/crew/subagents/slow_commands.jsonl` for later analysis (rotated at 1 MiB keeping one previous generation, `.jsonl.1`, so total disk stays bounded at ~2 MiB; a reader wanting full available history must consume both generations).
- Detection is **surface-only**: `_maybe_flag_stall` never terminates the agent. A genuinely-hung subagent is closed by the user from the UX (per-row stop → `spawnDelete` → `SubagentManager.cancel(agent_id)`, or header Stop-all). The wall-clock reaper at `_TIMEOUT_SECS` remains the only automatic terminator; a `DEAD` liveness verdict deliberately does **not** escalate to a kill, because that would be a change to reap semantics rather than to the signal.

#### Liveness attribution (why idle time alone is not the detector)

Idle time cannot separate a wedged tool call from a slow, silent one, so the flag is gated on a liveness verdict. Attribution is per-CHILD, not per-runtime-PID: the subagent event loop records the in-flight tool's dispatch snapshot (`_inflight_tool` — the trusted `is_shell`, the command, `tool_name`, dispatch time, taken from the same `AcpEvent` the main agent's `ToolCallState` is built from), and `_stall_verdict` hands it to a per-agent `LivenessOracle`. Calls dispatched in parallel finish in any order, so each is kept by toolCallId in `_active_tool_calls` until its terminal result (`completed`, `failed`, `cancelled` or `refused`), and `_inflight_tool` is the most recently dispatched call still running: a file write returning beside a `wait` hands the slot back to the wait instead of emptying it, the same rule `AcpSessionHandle` applies to the main agent. A native child's call, seen only through its permission request, is judged but never enters the set, because its results never reach the sub-agent's loop; the agent's own next terminal result moves the slot off it. Each prompt starts with the set empty, so a call an earlier turn never closed is not handed the slot later. With `is_shell` set this takes the oracle's shell-child branch, which cmdline-matches a live descendant and then tracks that pid. The fragment key is cut from the command TEXT, not from the cached input as rendered: the dispatch layer caches a dict tool input as its JSON rendering, so `match_fragment` decodes the `command` value back out of it (`_command_text`, the same decode the interactive-command classifier reads) before taking the longest quote-, backslash- and newline-free fragment. A key cut from the rendering would straddle the `\n` escapes, so a multi-line command would match only when its longest line is its first — every other line's fragment begins with the stray `n` of an escape that no real cmdline contains — and a working sub-agent would read `no matching shell child` for the whole run of a long command. The program-name fallback (`first_program`) reads the input as cached, so for a JSON-rendered input its first token is the opening brace and the weaker key is deliberately empty: that key is shared by every child of the same interpreter, under a shared runtime other sessions' children too, and a wrongly tracked child's exit would read as this command's death.

This is what makes the verdict meaningful even though **session-sharing subagents share the parent's runtime PID**: the match keys on the command, not the runtime. A whole-subtree aggregate would be useless here — it is dominated by kiro-cli's own background socket/keepalive traffic, so a `sleep`-only subagent reads as "working".

Verdict → action:
- `WORKING` — an attributable live child, so the agent is progressing: **not** flagged (suspicion stays open so the badge appears as soon as that child stops).
- `DEAD` / `STUCK_INPUT` — positive evidence of a wedge, so it flags **immediately**, skipping the two-sweep confirmation that exists to dampen guesses. That skip is **withdrawn whenever the runtime is session-shared** (see the third bound below), because the trust it assumes is exactly what a shared runtime removes — the parent session is always a co-tenant of that process, so a lone subagent is no safer than one with siblings.
- `UNKNOWN` — no attributable evidence (no tool in flight, a non-shell tool with no child to match, unreadable `/proc`, or a refused executor): falls back to idle-time-only with two-sweep confirmation.

The one non-shell tool that is attributable is the kirocrew-core `wait`. Its declared-duration contract (`ToolCallState.declared_wait_verdict`, shared with the main-agent oracle) reads only the call's own `seconds` and dispatch instant, so `_stall_verdict` answers it directly, with no `/proc` walk: `WORKING` until `min(seconds, WAIT_TOOL_MAX_SECS) + WAIT_TOOL_SLACK_SECS` has elapsed, then `UNKNOWN`. The reaper selects it by the adapter-authored identity (`ToolCallState.is_trusted_wait`: `mcp_server_name` is `kirocrew-core` from a provenance-verified frame and `tool_name` is `wait`, bare or server-qualified such as `kirocrew-core___wait`), never by the model-authored title, because this verdict is exempt from the ceiling below.

Four bounds keep this honest, and each exists for a failure that was observed rather than imagined:

- **`_SUPPRESS_CEILING`** — a `WORKING` verdict only suppresses while `idle < _stall_idle_secs * _SUPPRESS_CEILING`. Attribution is not infallible: two siblings running *similar* commands under `session_sharing` can cmdline-match the same child, so a wedged agent could read `WORKING` for as long as its sibling's child lives. Unbounded that would convert a case the idle-time-only path DID badge into a permanent false negative — worse than a spurious badge, since the badge is self-clearing and a missing one is not. Past the ceiling the badge wins, so misattribution costs latency, not the signal. The trusted `wait` contract is the one exception: it cannot land on another session's process and it ends by itself at its declared duration plus slack, so its `WORKING` suppresses past the ceiling.
- **The wedged skip is withdrawn under a shared runtime.** The same fallible match runs in the other direction: a `DEAD` reading can describe *another session's* child that exited, and because `DEAD`/`STUCK_INPUT` normally bypass the two-sweep confirmation, that would raise an immediate badge on a healthy agent — defeating the dampening that keeps the badge trustworthy at 60-100 agents. Granting one path immediate trust in a signal the ceiling exists because it is unreliable is incoherent, so when `info._session_sharing` is set the wedged verdict earns its badge the same way a guess does: by holding across two sweeps (~60s). **The gate keys on the flag, not on a sibling count.** `_create_shared_session` puts the subagent on the **parent's** AcpRuntime — one process hosts everything — so `info._pid` is the parent's process and the parent's own tool children are descendants of it too. `_live_shared_count` iterates the subagent registry and therefore cannot see the parent, so an earlier `_live_shared_count(pid) > 1` form left a *lone* session-shared subagent on the fast path while it could still cmdline-match the parent's child and flag the instant that child exited. Since a shared runtime always contains the parent, "could this match belong to someone else?" holds for every session-sharing agent; only a dedicated-process subagent (`session_sharing` false, or a per-spawn model/effort override that forces its own process) keeps the immediate flag.
- **The walk is offloaded, never inline.** `check_tool` is a synchronous `/proc` walk (`iter_descendants`, plus `os.readlink` on `/proc/<pid>/fd/*`, which can block on the very wedged fd being investigated) and the reaper runs on the same event loop that serves every chat turn. It is submitted through **`consult_offloaded` (`acp/liveness.py`) — the one shared guard, not a local mirror of it**: the same helper the main-agent watchdog reaches via `AcpSessionHandle._consult_oracle_offloaded`, so `SubagentInfo` supplies the `_consult_future` its `ConsultFutureHolder` protocol requires and a fix to the guard lands on every caller at once. The guard owns submission-inside-the-guard, exception retrieval attached at submission, the bound (`OFFLOADED_CONSULT_TIMEOUT_SECS`, 10s) via `wait_for(shield(...))`, at most **one outstanding walk per holder** so a permanently wedged read cannot leave a new blocked worker behind on every sweep, and degrade-to-`UNKNOWN` on any failure. Failure mode to be aware of: consults are awaited serially within a sweep, so if many agents cross the idle threshold while their `/proc` reads wedge, a single sweep can stretch toward N×10s and delay the wall-clock reap for the other agents in it. Bounded and unlikely (one walk per agent, later sweeps short-circuit on the in-flight guard), but it is the cost of doing this in the sweep rather than out of band.
- **Generation counter.** The awaited verdict is discarded (`superseded mid-consult`) when `info._stall_gen` moved during the walk — i.e. activity, a final tool result, or the next dispatch retired the snapshot it was submitted for. Without this the walk's own latency is enough to flag an agent that has resumed working, and `DEAD`/`STUCK_INPUT` skip two-sweep dampening, so a stale one would flag instantly.

The snapshot is retired only on a **`tool_final`** result. `EVENT_TOOL_RESULT` is also emitted for non-completed progress updates (`_dispatch` sets `tool_final = status == "completed"`), and treating one of those as the end of the tool would drop attribution mid-command — degrading exactly the long silent command this detection exists to judge. `acp.client` gates on the same field. On each retirement the oracle is replaced via `fresh()` rather than mutated, so a walk still running against the previous command cannot write its late sample into the next tool's baseline.

The verdict and its evidence are recorded in the reaper's log line but are deliberately **not** on the `subagent_stalled` wire: no consumer reads them today (the frontend narrows the payload on arrival and the coalesced batch update forwards only `stalled`), and the event is app-sdk-forwarded, so unread keys would become semi-permanent surface.


The slow-command record (`record_slow_command`, `subagent_persistence.py`) is append-only and deliberately NOT a tombstone: a tombstone marks an agent dead and is consumed by orphan-reconciliation / TTL cleanup, whereas a stalled subagent is still running. Fields: `id`, `flagged` (ts), `last_tool` (redacted), `tool_count`, `turns`, `idle_secs`, `elapsed_secs`, `parent_session`, `session_sharing`.

`_awaiting_approval` is set around **both** human approval awaits — the mid-run tool approval in the `EVENT_PERMISSION_REQUEST` branch (reset in `finally`, which also refreshes `last_activity`) and the pre-execution spawn gate in `_spawn_with_approval` (also reset in `finally`) — so a slow approval never looks stalled. The two are told apart by `_exec_started`: it is set for the mid-run prompt and `None` at the spawn gate, which is what lets the reaper name the right cause (see Reaper Loop).

### Running-card progress events

`subagent_tool` is fired on **`EVENT_TOOL_CALL`** (not only `EVENT_PERMISSION_REQUEST`) — kiro-auto-allowed tools surface only as informational `tool_call` updates, so this is the sole progress signal a simple/read-only task emits. Payload carries `{tool, tool_kind, turns, tool_count}`; `info.tool_count` increments per observed tool call. The `subagent_snapshot` reconnect payload (`dashboard/ws.py`, built by `build_subagent_snapshot()`) also carries `tool_count`, `stalled`, and — only while stalled — `idle_secs`, recomputed at replay time from `last_activity` (clamped at 0, omitted entirely for a healthy agent) so a reloading client recovers progress/stall state including the span that justifies the stall badge (a transition-only WS signal always needs a matching snapshot field).

An incremental progress frame **creates** the panel entry when the client holds none for the id it names, rather than being discarded. The store's incremental reducers (`sseSubagentTool`, `sseSubagentStalled`, `sseSubagentRetrying`, `sseSubagentBatchUpdate`, `sseSubagentBatchChunks`) resolve through `upsertSlotSub` (`website/src/store/chat/subagents.ts`, the sub-agent reducer family the `website/src/store/chatSlice.ts` facade composes into the chat slice), which returns the existing entry or mints a minimal one (`status: 'running'`, empty `task`/`agent`, filled in by any later frame that carries them). This is required because these frames are the only evidence the panel receives between one `subagent_spawn` and one `subagent_done`, and `clearSubagentsForSnapshot` keeps only `pending` entries across a reconnect — so an agent already running at that moment has its entry discarded while every frame it has left is an incremental one, and a reducer that refused to create would leave it invisible for the rest of its run. The prototype-pollution contract is unchanged: `upsertSlotSub` refuses a poisoned slot or id via `isUnsafeKey` and routes any write through `safeKey`, so such a frame creates nothing. Reducers whose frame only decorates an existing card (`markSubagentApproving`) keep the read-only `getSlotSub` and still require one.


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
`_subagent_done` in `slack/gateway.py`. The `parent_session_key` on `SubagentInfo`
tracks which session spawned the subagent.

### Two-Level Timeout

| Timeout | Location | Duration | Scope |
|---|---|---|---|
| Outer cap | `subagent.py _run()` | 1200s (20 min) | Semaphore wait + injection combined |
| Inner cap | `slack/gateway.py _subagent_done()` | 900s (`INJECTION_TIMEOUT`, tunable via `KIROCREW_INJECTION_TIMEOUT`) | Single `stream_and_collect` call |

On timeout (inner or outer):
1. Kill stuck kiro-cli process via `sessions.reset()`
2. Queue failure event into `slot._pending_subagent_failures`
3. Next `_run_chat` drains the queue into LLM context with `result_path`
4. LLM reads result from disk if needed

### Prompt-Busy Recovery

`_inject_with_retry()` in `slack/gateway.py` makes up to 3 attempts (1 initial + 2 retries) of `stream_and_collect` on AcpError. Between retries: cancels orphaned prompt, exponential backoff. On `PromptBusyExhaustedError` or `AcpProcessDied`: resets the parent session only when `runtime_death.caused_by_this_session(client)` says the death was the parent's own runtime; on a shared runtime the parent is left to re-acquire on its next turn, so co-tenant sessions survive a death they did not cause. Either way the failure event is queued (`notify_injection_failed`). Note: the 1200s outer cap (`_ON_DONE_TIMEOUT`) bounds total wall-clock time, so not all retries may fire if earlier attempts consume the budget.

**Reconnect recovery**: `subscribe_subagents` in `ws.py` restores both managed and native subagent cards. Managed subagents are authoritative in `SubagentManager` while the gateway that ran them is alive: running records replay as `subagent_snapshot`, and recently completed records replay as `subagent_done`, including terminal `elapsed` and cumulative `credits`. Managed results remain disk-backed and are not copied into inline Redux card payloads.

A replacement gateway process has neither, so the replay has a second, durable source for managed runs the live manager does not know, and that source is the CREW LOG: `ws.read_fold_subagent_records` folds each live slot's session for the `subagents` and `class` projections in one pass and maps the closed rows to `subagent_done` frames, which join the same replay list and therefore pass the same ownership filter, per-socket scope gate and `subagent_snapshot_batch` packaging as the live frames. The log is the record, so a card's outcome, duration and cost are a fold of it rather than a store kept in step with it by hand -- which is also what lets the frames carry the per-child `credits` and `model` the folder reader never held. Live state wins whole: a row is admitted only for an id the live manager does not hold, and no fields are merged. Only a CLOSED row is replayed: a row with no outcome is a child still in flight that this process is not tracking, so a card drawn from it would wear a running pill nothing will ever advance, and the fold's own `running` count is what states those instead. Native cards are not in the session's log -- nothing on the native path emits `subagent/spawned` -- so they are still not recovered across a restart. A log the operator turned off (`KIROCREW_CREW_LOG` falsy) folds nothing, and the replay falls back to `subagent_persistence.read_panel_records` over the run folders on the same bounds and through the same gates: the opt-out is about the RECORD, not about the panel, so it costs the rebuild its per-child `credits` and `model` rather than the rebuild itself. That branch is also the only path on which the replay reads folders, which is what keeps exactly one dismissal record authoritative at a time.

`GET /api/spawn` keeps reading the run folders through `subagent_persistence.read_panel_records`, and that is not a second record of the same state. What the folders hold that the log does not is the run's own OUTPUT TEXT: `result.txt`, which that endpoint returns and the log deliberately never carries. The state facts stay off it -- outcome, timing and cost are read from the fold by the surface that draws them -- so the folder reader is a body store the endpoint consults for a body, not a parallel answer to "what happened to this child".

The rebuild is bounded by `PERSISTED_SUBAGENT_REPLAY_KEEP` (50 newest rows, capping the burst one connect delivers) and `PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS` (one day, capping how far back it reaches). The age bound is separate from the native terminal TTL because it answers after the process is replaced, which is routinely more than an hour later. Both are measured on each row's `started_ms`, the `subagent/spawned` entry's own envelope stamp, which is also what orders the cap newest-first -- `seq_spawned` orders rows inside one log and says nothing across two, so a cap applied over several sessions needs the stamp. What actually survives to be replayed is decided by the log's own retention plus the fold's `OPEN_RETAIN_LIMIT`, not by this bound: a session that dispatched past the cap keeps its totals exact and reports the shortfall as `omitted`, which the panel section draws. A row with NO outcome is not given one: it is skipped until a closer lands, because a card claiming `completed` would contradict the orphan notice injected for that same run; a row whose closer recorded an outcome outside the three a card can draw becomes `failed` with the orphan wording, which is the same answer `classify_persisted_ending` gave an unreadable ending, and never `done` -- the client's terminal precedence falls through to success for a value it does not recognise. What the row cap cut is COUNTED and returned with the rows, and the consumer says it once per rebuild as a WARNING naming the cap and the count, because a truncated tail otherwise reads exactly like a population that never held those runs. There is no second scan-window bound to report: the fold read enumerates the live slots rather than walking a directory, so the count is exact over what the folds hold. The report is addressed to the operator, not to the client: no panel acts on the number, and a field no consumer reads is a field that silently rots. It is a WARNING and nothing else: a truncation refuses nobody, so it is not a permission decision and is kept out of the SEL deny stream, which carries the ownership and scope refusals an operator reads that stream to find. The count is computed over the CALLER'S OWN admissible set, because the caller's visibility filter runs before the cap: filtering afterwards would let a record the caller may not see occupy a slot its own runs need, and would make the number disclose how many foreign runs exist.

Ownership of a replayed record is decided by `ws_event_scope.persisted_replay_denial_reason`, not by the per-frame scope gate alone. It answers `""` for an admitted record, `"slot_missing"` when the slot is absent or still under construction, and `"persisted_owner_mismatch"` when the run's recorded app differs from the slot's present owner, so a slot that has not hydrated yet is not recorded as an ownership breach. `_subagent_visible` admits on the slot's **current** `_app`, which is the right question for a live run and the wrong one for a record read back off disk: slot keys are caller-supplied and are not namespaced by app, so the key an app's run was recorded under can later be created by a different app, and the gate would then hand the old run to the new owner. The predicate therefore requires the run's own recorded app to equal the slot's present owner and fails closed both ways, so a run no app owns matches only a slot no app owns. On the folded path that recorded app is the `class` fold's `app`, which holds the FIRST owner the session ever had and never moves off it -- a log that was an app's stays an app's, which is exactly the property the equality needs, and it is read from the same one-pass fold the rows come from. Any future change to slot-key allocation has to preserve that equality. The predicate is evaluated ON THE EVENT LOOP, twice, and never from the worker thread. Once before the off-loop scan, as `ws_event_scope.persisted_precap_readings` (which pairs `slot_owner_snapshot` with `visible_subagent_slot_keys`) fed to `persisted_precap_denial_reason`, whose answer sizes the row cap over the records this caller may see; and once again on each surviving record with `persisted_replay_denial_reason` before its frame is kept, which is the authoritative decision. A slot's owner can be reclaimed while the scan runs, so a decision taken inside the worker thread would be read from state the caller no longer describes. Both halves answer the SAME reason shape, because they are two halves of one decision and withholding a record IS the permission decision: a bool on the snapshot half would force a caller to drop a record with no record of why, which is the normal case, since the two apply the same equality and most refusals never reach the loop half. The two sets are disjoint, so nothing is audited twice. Each refusal emits a SEL event through the same `_audit_deny` chokepoint the per-frame gate uses, under its own reason so an ownership refusal and a scope refusal are not read for one another; and an ADMISSION emits `_audit_allow`, because a stream carrying only refusals can show what was blocked and never what was released. `GET /api/spawn` shares the loop-taken ownership half and the same audit chokepoint, but NOT the pre-cap pair: it consults `persisted_snapshot_denial_reason`, which answers on ownership alone, where the replay's `persisted_precap_denial_reason` adds the visibility half (`persisted_not_visible`). The replay needs that half because a socket subscribes to declared event types; the REST caller declares none, so there is no declaration for a record to fall outside of.

The pre-cap decision carries TWO independent bounds, ownership and VISIBILITY, and both are needed before the cap: a record may name a slot the caller owns while carrying an event the caller never declared, and may be visible under a declaration while belonging to another app. Sizing the cap on ownership alone lets a burst of records this client cannot see spend the slots its own visible runs need, and the cut count is then computed over records that were never its to receive. Visibility is reported first because it is the broader refusal — a declaration gap must not enter the stream as a cross-app breach. Using it before the cap is safe despite the answer being time-varying, because of which way it moves: `app_events_revoked` reports NOT revoked on a cold cache and schedules the refresh, so the cold answer is the OPEN one and warming can only narrow it, and pre-cap open then post-cap closed costs a cap slot the authoritative gate reclaims. The losing direction needs an app re-enabled mid-scan, which costs one run one replay that the next reconnect carries.

A terminal outcome, once recorded on disk, is not erased by a later tombstone write that carries none. `write_tombstone` REPLACES the file and only its `**extra` can supply `outcome`, so a caller with no opinion about the outcome — a delivery acknowledgement is the ordinary one — would otherwise drop what an earlier write recorded, and `classify_persisted_ending` would fall back to deriving an outcome from `cause`, where `delivered` derives `completed`. A user stop reaped and then acknowledged would read as a success. The carry-forward lives at the single write rather than in each call site's argument list.

`died` is carried on the same grounds and through the same read, with one difference: every write stamps it, so the carry is what keeps the field meaning the run's ENDING rather than the moment of the most recent bookkeeping write. The read is taken on exactly ONE condition, `outcome` absent, and that condition is load-bearing rather than incidental: a caller supplying an outcome is recording an ending, so its own fresh stamp IS that ending and there is nothing to carry, while widening the condition to "`died` absent" would take the read on every such caller — including the sync writers reached from a coroutine, where this function's synchronous file I/O must not land. A panel card's duration is the run's, and a delivery acknowledgement lands whenever the parent got round to the result. Where no write happened at the ending at all the carry has nothing to preserve — the queued and digest-held paths skip `mark_delivered` at completion (`_defer_queued_delivery`, settled later by the drain), so the acknowledgement is their FIRST tombstone and its stamp is the delivery wait. The run's own `result.txt` is the completion evidence that survives there: the run appends it as its output streams, and its ending writes it last: a claimed completed ending rewrites it whole from memory after post-processing (or removes it, for an answer with no text), and any other ending caps it in place from `_run`'s `finally`, while delivery and retention only read or remove it. `_panel_record` therefore reports `elapsed` to the EARLIER of the tombstone's `died` and that file's last write, and a run with no file -- no output, or a whole answer with no text, even when an earlier attempt streamed some -- falls back to the tombstone. Only the duration uses it. The record's own retention window stays on the latest timestamp, because a run whose result reached its parent moments ago belongs in the panel whenever it happens to have finished.

That carry-forward adds a disk READ to a function that already wrote, so it sharpens an obligation the write already carried: `write_tombstone` is synchronous and must not run on the gateway's event loop. The invariant is REACHABILITY, not enclosure — a sync helper called from a coroutine still runs on the loop — so every path that takes this disk READ crosses an offload. Four of them do so explicitly: `mark_delivered` on the delivery path, the two `gateway_restart` writes in orphan reconciliation and orphan notification, and the digest-hold settlement, which is itself a coroutine for this reason and whose re-entry guard still holds because the id swap precedes every await. The settlement carries a SECOND obligation the others do not, and it follows from that same swap: the held deliveries are detached from `info` irrevocably, so the batch is handed to ONE `settle_delivered_batch` worker operation rather than awaited per delivery. A per-delivery await makes every one after the first a cancellation point, `CancelledError` is not an `Exception` and so escapes the per-delivery guard, and nothing re-arms the deliveries already taken off `info` — each one then keeps no `delivered` tombstone, which is the marker restart reconciliation uses to EXCLUDE a folder, so it replays as a duplicate completion. Handed over whole, the worker finishes the batch whether or not its waiter is still waiting. The batch takes the `SubagentDelivery` records rather than bare ids because the tombstone carries the run's terminal usage: settling by id alone would write a `delivered` tombstone with no `elapsed` or `credits`, so a held wave member would show no usage in the panel while its siblings carry theirs. That batch writes through the `mark_delivered` the MANAGER resolved, passed in as `writer`: the manager's modules take their names from the facade, the facade is the single point anything substituting the write replaces, and a batch reaching past it would be the one delivery path ignoring the substitution. The drained queue offloads with its own `await asyncio.to_thread(mark_delivered, agent_id, elapsed=…, credits=…)`, the same pattern; it stays per delivery because its deliveries come from its caller rather than from detached state, and because each one must first await its own teardown gate on the loop. Note the namespace: a manager method whose name ends in `_impl` executes with the FACADE's globals, so inside one the module's own private alias is not defined and the offload must name `asyncio`. The remaining `write_tombstone` callers pass an explicit `outcome`, take no read, and keep their existing behaviour — which means they still write synchronously on the loop. That is scoped deliberately, not claimed as covered: such coroutine call sites remain in `subagent_manager/run.py`, `cancellation.py` and `terminal.py`, the structural test's `WRITERS`/`MODULES` sets do not reach them, and offloading them is separate work.

`GET /api/spawn` bounds an app caller by its app claim UNCONDITIONALLY, so the reused-slot-key exposure is closed for an app whatever its session scope resolved to: a run recorded by another app fails `persisted_app_mismatch` and never reaches the session or ownership checks. The SESSION bound is the conditional one, on `internal_auth` — the same condition the live branch applies to the same field, so the durable half of one listing is neither wider nor narrower than the live half beside it. It is deliberately not conditional on the resolved memory store, which answers empty for a verified internal caller on the default store exactly as it does for the dashboard owner and would therefore lift the bound for nearly every attested caller it exists to bind. The uncovered case is the DASHBOARD OWNER, deliberately: a record's app is whatever spawned the run, which need not match the app of the tab now reading it, so narrowing the owner here would empty the panel on exactly the cold start the durable listing exists to serve.

The REST listing's app bound gates on the presence of the caller's app CLAIM, never on a transport flag. `derive_caller_app` states that app-ownership checks read `request["app"]`, and publication is narrowing-only: every arm sets the claim only for a positively resolved app and leaves it absent for the dashboard user, which is the same fact the middleware inverts into `is_dashboard_user`. A flag answers which credential arrived, and four arms publish an app claim while only the internal-secret arm sets `internal_auth`, so a bound keyed to that flag is absent on exactly the cookie and token transports an installed app normally arrives over.

**Dismissal**: because a durable reader answers from something that outlives the process, dropping a run from the manager is only half of a dismissal: `settle_before_delete` pops the run and leaves the durable trace, so on its own the dismissal would last only as long as the process and the next rebuild would send the card again. The PANEL's half of a dismissal is therefore an entry in the owning session's crew log, `subagent/dismissed`, written by the dismiss route: the panel's durable half is a fold of that log, the `subagents` fold adds the type to `affects` and marks the row dismissed, and its render stops offering the row while leaving every total where it was. Keeping it anywhere else is a second record of a fact about the session. `subagent_persistence.record_panel_dismissal` is such a registry, keyed on the run's FOLDER at both ends: the write refuses without one, and `prune_orphan_panel_dismissals` drops a record whose folder has gone. A `delivered` folder is reclaimed within the hour while the log keeps the child for the replay's whole day-long window, so a dismissal held only there would let the card come back. The owning session is resolved cheapest-first, from the emitter's own in-process spawn pin and then from the live slots' logs, because the case that matters has neither a manager entry nor a pin: both died with the gateway that wrote them. That registry stays, because `GET /api/spawn` reads the folders and `read_panel_records` skips a recorded id there. A dismissal the registry holds and the log does not is corrected INTO the log rather than read around: on each panel replay `ws.backfill_legacy_panel_dismissals` appends the missing `subagent/dismissed` for a child the registry marks dismissed and the log does not, and excludes the ids it corrected from that same read, because the append is queued and the fold that follows may not carry it yet. It is idempotent through the fold's own behaviour: the render stops offering a dismissed row, so a corrected child is not among the rows a later pass walks, and the registry is read at most once per child and never written from that path. The record is written at the pop rather than in the route, so the two halves cannot come apart, and `DELETE /api/spawn/{id}` writes it directly for an id that exists only on disk — which after a restart is every finished run. The route answers 404 only when NEITHER record can be written, since a log that records the child is enough to keep the card gone whether or not a folder survives. Each crew-log append is WAITED on until it commits, bounded, because the queue returns as soon as the entry is handed over and once the folder is reclaimed that entry is the only record of the dismissal -- publishing on the handover reports a dismissal the next reconnect undoes, with nothing left to explain it and nothing for a retry to act on. A half that was owed and did not land answers the retryable 503 whichever half it was. That arm is the dashboard owner's alone, exactly as wide as what the owner can already see, since an app token holding a reach over runs it cannot list would be a new capability; an absent app claim is refused rather than trusted, and an id with no folder keeps its 404 only when no unit holds it either, because then nothing durable can rebuild that card. A run a unit DOES hold is not an unknown id: if its append does not commit the route answers the retryable 503, not 404. Both arms are audited under `spawn.dismiss`.

The record lives in its own top-level `panel-dismissals/` leaf of the data home, OUTSIDE the run folder, and deliberately NOT under `trust/`. A dismissal is an OWNER decision about what the panel hides, which makes it the same class of record as `crew-panels`, `crew-teams` and `tag-grants` — and each of those is its own top-level leaf precisely because `trust/` is a declared sandbox READ-WRITE exception (in-sandbox `verify_session_pid` reads `trust/sel_hmac.key` and the in-sandbox MCP servers append to the audit log). A record under `trust/` stays writable by a sandboxed command that builds the path at runtime, which command matching cannot catch because it has no literal path to match; the cleanup identities that live there are not a precedent for adding records to it. So the leaf is registered in four places, and because nothing in the code fails when one is missing, each is pinned by name in `TestTheDismissalStoreIsRegisteredEverywhereItMustBe`: `paths._CREW_SECRET_LEAVES` for the file gate, `sandbox._CREW_HIDDEN_LEAVES` for the bind mask, `_CREW_PRECREATE_HIDDEN_DIR_LEAVES` because the directory is created on the first dismissal and the mask loop SKIPS a name that does not exist, and `_CREW_NO_ALIAS_LEAVES` because a symlink would attach the mask to the target and leave the name writable. `state.json` and `tombstone.json`, by contrast, are agent-writable, so a marker kept in there would let a run hide its own card from every later rebuild. The write goes through `atomic_write(..., restrict_to_owner=True)` so the lockdown lands on the temp file before the payload and before the rename. The run FOLDER is deliberately left in place rather than deleted — `spawn_continue` reseeds its session map from that folder's `state.json`, so removing it to hide a card would also destroy a conversation the user can still continue, and would take the result text and the prune bookkeeping with it. The reader fails OPEN: an unreadable record yields no id, so a filesystem fault resurrects a dismissed card, which the user can dismiss again, rather than hiding a run nobody dismissed. Records are reclaimed with their folder at both removal sites (`delete_agent_folder` and `prune_stale_tombstones`), and `prune_orphan_panel_dismissals` sweeps once per prune cycle for a folder that left by some other route.

Native kiro-cli subagents run inside the parent ACP turn and are owned by the parent dashboard slot. `DashboardState.native_subagent_snapshots()` replays running native cards as `subagent_snapshot` and recent terminal cards as `subagent_done`. A native `subagent_done` payload may include optional `task`, `agent`, and `result` fields. `result` is a redacted output tail bounded to 8,000 characters, with an explicit truncation marker when earlier output was dropped. Running output retained for replay is bounded to 40,000 characters, with an 80,000-character hard accumulation ceiling. Terminal native records are retained globally up to 50 cards for at most one hour. The client treats `done` and `error` as monotonic terminal states, so a stale running snapshot interleaved after a live completion cannot demote the card.

**Redaction**: All subagent event payloads (running snapshots and done events) have the `agent` field redacted before sending to the dashboard. Task text is redacted before truncation to prevent credential patterns spanning the boundary.

| Parent Session | Backend Delivery | Client Follow-up | User Sees |
|---|---|---|---|
| Dashboard (`dashboard:*`) | Append as user message + broadcast via WS | TUI/web re-injects via `sendMessage` → LLM round-trip | LLM's response summarizing the result |
| Slack (`slack:*`, a parent with a mapped Slack thread, or a legacy bare-ts key) | Inject into the parent ACP session (`_inject_with_retry`, up to `_MAX_INJECT_ATTEMPTS`), then post the synthesized reply into that Slack thread, in the session's stored channel or else the owner DM + dashboard notification | _(none)_ | LLM's synthesized reply in the Slack thread |
| Non-Slack channel (`telegram:*`, `discord:*`, `unified:*`, …) | Inject into the parent ACP session, then send the synthesized reply through the governed cross-surface transport ladder (`_deliver_channel_reply` → `_resolve_channel_target` → `MessagingTransport.send_message`) + dashboard notification. Target resolution: origin link (recorded by Discord's inbound dispatch) → non-Slack mirror link (e.g. Telegram `/link`) → for **direct (1:1) sessions only**, the stored `"{namespace}:{user_id}"` channel value, resolved to a postable conversation via `transport.resolve_configured_target`. Group/forum sessions without an origin/mirror link, channels whose dispatcher records neither, and denied/unsupported egress all degrade to notification-only — never a cross-conversation send. | _(none)_ | LLM's synthesized reply in the channel conversation |
| Other machine parent with no channel namespace or Slack thread (e.g. `taskrunner:*`) | Inject into the parent ACP session; no channel post | _(none)_ | Nothing in any channel |
| Cron/no parent | Dashboard notification only | _(none)_ | Notification panel entry |

### Post-fan-out Synthesis Turn

After a fan-out of sub-agents, a single dedicated **synthesis turn** produces
the user-facing summary (restate goal → synthesize across all results →
recommend next actions), instead of leaving the last visible message as a
per-sub-agent completion note. Dashboard chat only.

- **Arm** — in `_subagent_done`, IN MEMORY ONLY and with no await, cheapest
  check first: not already armed, not a flush-only record, not an id the
  blocking tool collected inline, `running_agents_for(parent_key) == []`, and no
  `has_in_memory_pending_work_for(parent_key, exclude_id=info.id)` (a spawn in
  the dispatch window, another child whose report still waits on its teardown,
  a live follow-up watcher) — then set `slot._pending_synthesis = True`. The arm
  sits on the delivery path, so it never waits on the task store's writer, and
  with no await the tab cannot close nor a sibling register under it. A child
  only the store holds is the fire gate's to see. A probe that raises leaves
  the synthesis disarmed.
- **Fire** — `chat_runner._finish_queue_cycle` asks the ONE store-reading
  check, `chat_utils.synthesis_fire_verdict`, on the slot's real session key
  (`effective_session_key`, which a channel- or cron-born tab does not spell
  `dashboard:<slot>`). It holds for a running child or an in-flight delivery,
  for the same in-memory terms, and for a child only the store holds
  (gate-deferred, waiting for a slot, or claimed and not registered), and
  answers `unknown` for a store nobody could read. Only `clear` launches the one
  tracked synthesis task; `_run_pending_synthesis` reuses that verdict and reads
  no probe of its own. On `unknown` the idle slot re-checks on a timer
  (`_SYNTHESIS_RECHECK_SECS`, at most `_SYNTHESIS_RECHECK_MAX` times, one timer
  per slot, cancelled by `begin_close`), because no completion is left to
  re-trigger it; a real "attached" answer waits for that child's completion.
  When synthesis does not fire, a user message queued while the gate read the
  store is drained as at a normal turn end (`_start_next_queued_turn`, which
  keeps its own `hold_users` rule). `_synthesis_inflight` prevents duplicates.
  There is **no readiness wait**:
  readiness is latched at gateway boot and refreshed only on explicit user action,
  so parking the arm on it would strand the synthesis indefinitely. The task
  clears the arm once the delivery guards pass, immediately before starting one
  timeout-bounded `_run_chat` turn with `SUBAGENT_SYNTHESIS_PROMPT`; a signed-out
  CLI surfaces as an `AcpAuthRequired` error card from that turn.
- **One-turn batch** — the gateway counts each completion it queues or launches
  into the slot (`slot._synthesis_completion_turns`; a wave digest is one). When
  the turn-end fire gate (`_finish_queue_cycle`) answers `clear` and the count
  is exactly 1, that one turn already reported the whole batch:
  `_drop_single_turn_synthesis` consumes the arm and the cycle ends as an
  ordinary one. Any other count fires as before, and so does the outage
  re-check: it runs only after an `unknown` verdict, so the occasional extra
  synthesis it allows is one main already ran. The count resets when the
  synthesis runs or is dropped, and when a user message drained at a turn's end
  disarms an armed synthesis after the fire gate answered `clear`: the turn-end
  hand-off (`_hand_off_queue`) reads `synthesis_fire_verdict` while the ending
  turn still holds `slot.task`, so no turn can start during the read, and
  passes the verdict to the drain as `end_batch`. The drain itself
  (`_start_next_queued_turn`) reads no store and keeps origin/main's await-free
  dequeue; every other drain (Run now, Continue, rewind, regenerate, the parked
  drain) keeps the count. A `held` or `unknown` answer, as for a user message
  drained while a child is still out, leaves the count alone, so that batch
  keeps its synthesis.
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

The spawn tools name their parent with `mcp_core._resolve_session_key()`, the
shared client-side identity ladder (gateway-injected caller context, the signed
per-session token, `KIROCREW_SESSION_KEY`, then the gateway-published pid
mapping); [MCP: the shared-backend invariant](../../architecture/mcp.md#why-the-shared-backend-invariant)
owns that ladder and the strict variant that refuses an unsigned
`session_pid_<pid>.txt`. The session key flows through the `/api/spawn` endpoint as `parent_session`.

## Run state file (`state.json`) write model

**Decision: `state.json` stays a WHOLE-FILE rewrite.** No revision counter, no
compare-and-swap retry loop, no per-field or append-merge format. This is a
recorded choice, not a deferral.

**Why.** `state.json` is a run's artifact and evidence record. Scheduling's
source of truth is `tasks.db` (next section), which already carries generation
fencing. Building a second coordination protocol one layer below it would order
writes this file does not need, and would cost a format that `read_state`, the
tombstone pruner, the keep scan, orphan recovery and the legacy-record migration
must all agree on — an irreversible on-disk migration for a class with no
reachable defect today.

**The invariant that keeps the rewrite safe.**

> Every whole-file `state.json` write happens at a KNOWN site, and each site
> reachable from the event loop carries its own fence.

`update_state` and `update_execution_context` both read, merge, then land a
blocking fsync-and-rename. Two writers on one `agent_id` therefore interleave,
and the later one restores a snapshot predating the other's write — rolling back
fields *neither* writer touched, which is the damaging half. Off-loop callers are
serialized by the per-agent lock (`_STATE_LOCKS`). An on-loop caller must not wait
on that lock, because parking the gateway's only event loop behind a pool
thread's fsync is the `no-blocking-call-on-event-loop` class, so each on-loop
site carries its own fence instead -- with exactly one named exception, the
spawn-path acquire in the third row:

| Site | Fence |
|---|---|
| `_release_disk_sync` → `update_state` | off-loop from every event-loop caller: `release_conversation_async` runs the release's disk half (the `keep=False` demote and the session-file unlink) on a worker thread, so it inherits the lock, and holds the conversation in `_abandoned_state_writers` until that worker lands. The `SessionMap` forget stays on the loop and commits first |
| `promote_retention` → injected writer | probes the state lock non-blocking, returns `RETRYABLE` on contention |
| `create_agent_folder` → `update_execution_context` | the ONE on-loop acquire that can wait; bounded by sitting on the spawn and admission path, never a per-turn one |
| `create_agent_folder` → `_atomic_write` | creation path; writes the initial file before any writer for the agent exists |
| every writer inside a run | goes off-loop and is drained through `_drain_state_writer`, inheriting the lock; it enters through `_write_state_off_loop`, or through `_write_finished_result_off_loop` for the run's ending (next row) |
| `write_finished_result` → `state_writer` | the run's ending: reached only from the worker `_write_finished_result_off_loop` hands `update_state` to (as `state_writer`), so it is off-loop, inherits the lock and shares the same drain, bounded even uncancelled |

`update_execution_context`'s other three callers are absent from that table
deliberately. `bind_session_execution`, `tighten_run_memory_mode` and
`write_run_agent` each reach it from a pool thread at every call site
(`asyncio.to_thread` or `drained_to_thread`), so the callee's unconditional lock
serializes them exactly like an off-loop `update_state` and needs no fence of its
own. An unconditional blocking acquire is a fence only off the loop; the single
on-loop exception is the row above, and `_STATE_LOCKS` records the same bound at
the lock itself.

**How the asymmetry closes.** By moving the remaining on-loop sites OFF the loop,
where each inherits the per-agent lock and needs no fence of its own — never by
changing the on-disk format. The ON-LOOP rows are therefore expected to shrink
and never grow. A new row is admissible only as a site that runs on a pool thread
behind the same drain and lock, like the run's ending above.

**Enforcement.** `test_subagent_state_write_model` is a static AST gate over
`kiro_crew` source. It pins the write-site census with a per-site call count, so
a new write anywhere fails and its author must state the fence; it separately
refuses any write sitting directly in an `async def` body, of which there are
none. Every current site lives in a *synchronous* function that a coroutine
calls, which is why the gate pins sites rather than trying to decide statically
whether a given call runs on the loop.

**What the gate does not check.** The fence column above is prose, derived by
reading each call site's callers; no test re-derives it. A site that moves on or
off the loop keeps the same `(module, function, call)` key, so the stale-entry
test cannot see a fence go out of date — only a site that moves or disappears
entirely. A commit that changes a site's loop status therefore updates that row
in the same commit, and a commit that takes the last on-loop site off the loop
deletes its row, which is how the census shrinks. The gates' own assertions are
pinned by meta-tests that drive each gate against a census or a site list that
must fail it, so dropping an assertion reddens the suite instead of quietly
disabling the gate.

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
  (cwd allowlist, governance, the parent spec's allowlist) run before
  anything starts, after the commit and the memory read**: on the first
  entry, on the second half of every memory read (the `_store_accepted`
  row's included), and again when the pump drains a stored row (the re-check
  before dispatch). Only the `_store_accepted` row's first pass skips them,
  straight after `prepare_spawn` ran them: it never starts the run itself, it
  stops at the memory read or leaves the row queued. The claim await
  (`store.run(self.taskq_claim)`) is NOT re-vetted: its second half re-enters
  with `_claimed` set, and the gates do not run again. That gap predates the
  off-loop memory read. A refusal of a row that
  already exists -- a committed `_store_accepted` row not yet claimed, or a
  drained row -- marks it `failed` in the store in the same step
  (`_refuse_row` -> `taskq_fail`), so the caller's verdict and the store's
  agree and no refused work stays executable. Capacity is never a refusal for a
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
  rather than grants and which no start path reads. One in-process exception: the
  process that ACCEPTED a durable row keeps its `approval_mode` in
  `_held_approval_modes` (keyed by the freshly minted run id) for as long as the
  row waits, and its window refill (`_refill_apply`) puts it back on that row's
  entry. That is the same request's consent, which an entry that never left the
  in-memory window carries anyway; without it a held or deferred App Kit spawn
  would drain into a prompt nobody can answer. It is dropped when the row
  registers, is refused or is stopped (`_forget_pending_start`), and a restart
  replays the row without it, as above.
- **A run id is 16 hex characters, minted at one site**
  (`SubagentManager._mint_agent_id`, `_RUN_ID_HEX_CHARS`; the gate, the
  continuation coordinator and the wave digest all call it and none of them
  draws). The width IS the uniqueness argument: 16 hex characters are 64 bits, so
  2000 spawns on one host collide with probability about 1 in 10**13. At the 8
  characters this replaces it was 32 bits and about 1 in 2,100, and the collision
  did not read as one -- identity is assigned before registration, so the caller
  was handed the id and the accept then failed on the duplicate primary key,
  reaching the user as `task store write failed`, naming a subsystem that was
  working correctly. A narrow id plus a uniqueness check is the alternative, and
  it is more mechanism for less: the check would have to know which ids are
  taken, a durable task row outlives the process that wrote it, and the spawn
  path cannot ask the store because taking a task-store connection on the event
  loop is refused (`kiro_crew.on_loop_db`). Nothing pins the width -- every
  consumer prints the id or passes it through -- so ids written before this are
  8 characters and stay valid; `spawn_status`, `spawn_continue` and the dashboard
  wave roster read both.
- **Wakes.** Only `resume_grant` writes `running` (`wake_wait(to=running)`,
  slot reserved, runtime resident) — and the landed wake is the PRECONDITION
  for the publish: the pump reserves the lane slot on the loop
  (`resume_reserve`), the wake runs on the writer thread
  (`resume_grant_async`), and `_slot_released` / `_wait_record` /
  `_taskq_generation` / `_resume_event` are written only from its result. A
  wake the store could not reach releases the reservation, leaves the run parked
  with its wait intact, and re-arms the request through `_rearm_resume` after
  `_RESUME_REARM_SECS` (1 second), because no other caller re-issues a one-shot
  wake; the waiter's own bounded `_await_lane_resume` timeout still ends the wait.
  So the no-overshoot invariant above is unchanged and a grant is never reported
  for a slot the store does not back. Only a row that moved out of this run's
  wait and a run that ended while its slot was reserved release with
  `retry=False`. The publish re-tests the run's LIVENESS across that split for the same
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
  updater's boundary, [session.md](session.md) § APIs (`pause_turn_admission_for_update`)): the run stays
  parked with its wait intact and the same `_rearm_resume` asks again, because a slot taken
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
  `cancel_for_parent` / `cancel_for_teardown` all add the store-only rows
  (`taskq_overflow`, `taskq_batch_pending`, `taskq_pending_ids_for`; the teardown
  fences its read with the ids the snapshot's fence recorded as accepted after it). An event-loop caller takes the
  `*_async` sibling instead — `queued_count_for_async` /
  `has_pending_work_for_async` over `taskq_overflow_async`,
  `taskq_pending_ids_for_async` for `cancel_for_parent` and `cancel_for_teardown`, and
  `batch_members_pending_async` / `_sweep_stuck_waves_async` /
  `_sweep_digest_holds_async` over `taskq_batch_pending_async` (the reaper and
  the gateway's completion consumer are the coroutines that hold them) — each of
  which snapshots the
  in-memory exclusion set on the loop and runs only the `count_pending` /
  `list_pending` / `fetch_pending_by_batch` half on the store's writer thread.
  `queued_run_async` / `queued_runs_async`, which the spawn status and list
  routes read, take the same split, and the queued count counts `admitted`
  rows no run is registered for (`count_pending(include_admitted=True)`).
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
  undelivered for the length of the outage. The queue-depth chip does not take this
  answer: its reader (`taskq_chip_overflow_async`) answers `None` and the chip
  publishes nothing until a retry reads the store. `taskq_pending_ids_for` is the
  exception and stays `[]`: it enumerates ids to cancel, and inventing one would
  cancel a row nobody read. Pinned by
  `test_taskq_admission_integration.py::test_an_unreadable_overflow_keeps_the_attached_children_guard_closed`
  and `::test_an_unreadable_batch_read_holds_the_wave_open`.
- **Claim before registration.** `taskq_claim(agent_id)` runs right before the
  info is registered; it returns `(generation, proceed, reason)`. With
  `proceed=False` the reason decides what happens:
  - `CLAIM_UNAVAILABLE` — the store could not be reached for the claim. The row
    stays queued (an unclaimed start would hold no lease reconcile could fence),
    spawn returns a `queued` info, and the drain runs again after
    `taskq_admit_wait_secs()`.
  - `CLAIM_RETAINED` — the row is already admitted under this process but its
    post-claim durable check could not finish. The admitted generation and
    reservation are kept, spawn returns a `queued` info, and the retained-claim
    retry pass owns the wake.
  - `CLAIM_REFUSED` — the store knows the row and refuses it (cancelled while
    waiting, or claimed by another dispatcher): spawn returns a
    `queued`+`done`+`user_stopped` info without registering and the drain takes
    the next row.

  A row the store never saw (a legacy in-memory entry, as tests inject), or no
  durable store at all, proceeds with generation 0; a row this dispatcher already
  admitted on an earlier pass keeps its generation. The generation is stored
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
- **Cancel.** The store row is cancelled FIRST (`taskq_cancel_queued`, which
  also returns the params for a store-only row so the queued-stop report is
  whole), then `_unqueue_impl` drops the window entry. A single `cancel` takes
  the store phase itself and hands it to `_unqueue` (`store_cancelled=True`), so
  its report knows whether the cancel landed. Stop all keeps that order for a
  whole pass in one writer-thread job (see `cancel_for_parent`). `cancelled` beats the drain in either
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
  unless its artifacts prove an outcome (`taskq_artifact_probe`): a readable
  tombstone's recorded outcome, then its cause; with no tombstone, or cause
  `gateway_restart`, `done` when `subagent_persistence.result_is_whole` holds.
  The existing orphan reconciliation still delivers the notification.
- **Boot rows wait for the memory fence.** That boot wake-up is armed while the
  gateway is still inside its memory barrier, so the gateway builds the manager
  with `defer_queue_dispatch=True`: `_queue_dispatch_held` makes
  `_drain_queue_impl` refuse every pass, claiming nothing, and
  `release_queue_dispatch()` — called only from
  `_start_subagent_dispatch_after_memory_ready()` once the fence is ready — opens
  the pump and drains whatever accumulated. A manager built without the flag
  (tests, tools) pumps as soon as it can. The first refused pass logs one debug
  line naming the queue depth and store state, so a hold that is never released
  is visible instead of presenting as rows accepted but never claimed.
- **Memory pressure defers.** See admission order step 4. SEL outcome:
  `deferred_low_memory`, durable or not; no capacity outcome is a refusal
  (`test_spawn_admission_verdict_census.py`). The macOS kernel pressure
  hold is a capacity-style wait (step 5), audited `deferred_memory_pressure`.
- **A memory deferral has a max wait** (owner decision, RFC Q10):
  `agent.subagent_queue_max_wait_secs`, default
  `DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS` (1800 s), `0` for no bound, read live
  (`LIVE_CONFIG_PATHS`, adopted by `apply_limits`; no restart mark). Each re-park
  writes `now + admit_wait`, so on its own a deferral renews itself for as long as
  the host stays short. The pump ends it: at the END of every pass, after that pass
  has re-checked and re-parked what it picked, `taskq_expire_memory_waits_async`
  (the inline pump calls `taskq_expire_memory_waits`) reads
  `TaskStore.deferred_longer_than` -- `queued` rows still parked that have spent
  the bound PARKED in their current wait (the `deferred` events after the row's
  last `claimed`/`transition`, each counted from its `ts` to its `until`) -- and
  fails each one in the same writer-thread call, fenced by the generation it read.
  Each `finish` commits on its own, so a store refusal partway through never
  discards the rows already failed: their reports are still made (no later read
  names them -- the sweep reads queued rows, the replay another incarnation's).
  A refused `finish` ends that sweep and leaves the row and the rest queued for
  the next one.
  A memory wake that brings a parked row forward (`TaskStore.expedite`) writes
  no event and leaves the row unparked, so it accrues nothing until its next
  deferral starts the next span.
  A re-check does not restart the clock; a claim does. What is measured is parked
  time, not time since the first deferral: a row whose deferral merely LAPSED is
  eligible again and waits for a slot, so it is not ended, and the time it spends
  that way does not count once it is parked again (a row held back once, then
  queued for 30 minutes behind a full cap, is not ended by its next memory
  re-check). The cost is in the other direction: a pass picks one row, so a long
  backlog of deferred rows is re-checked one stagger apart, and the time a row
  spends eligible waiting for its re-check is not counted either. Live runs,
  accepts in flight and rows the pump has popped are excluded. Each failed row is reported
  through `_report_queued_stop(params, error=QUEUED_WAIT_EXPIRED_TEXT)`: the
  queued-stop report with `never started: waiting for memory` as its `error`
  (outcome `failed`), so batch accounting, the digest and a waiting parent's wake
  are the stop's, and so is the depth: `_report_queued_stop` re-publishes the
  parent's queued depth itself, once per row and before the terminal record is
  built (the sweep emits none of its own), which reaches 0 once nothing else
  waits. The sweep's `failed` is the row's terminal write, so the report passes
  `row_settled=True` and its settle writes no second `finish`. Rows are bounded alike whether they were accepted by this process
  or restored from `tasks.db` (the clock is the store's event time). **The
  report survives the process that owes it:** the failure commits before its
  report runs, so it is written with `finish(..., report_owed=True)`, which puts
  the owing incarnation on the terminal `transition` event in the same
  transaction; once the report has reached the parent the mark is cleared
  (`taskq_clear_owed_reports` posts `TaskStore.mark_reported`). For a report
  the gateway delivered directly that is when the report task has returned
  True; one that returned False (the injection timed out, or `_on_done`
  raised), raised or was cancelled stays owed. So does one whose task returned
  True although the gateway gave up on the injection: a channel or cron
  parent's failed attempts are swallowed inside `_on_done`, so
  `notify_injection_failed` (and the cron arm that swallows a failure without
  a notice) marks the record `_report_undelivered`, and
  `taskq_clear_owed_report_if_delivered` leaves the mark. A report it PARKED is not
  delivered when its task returns: a wave member held for its digest
  (`_digest_held`), an announce queued behind a busy slot
  (`_delivery_queued`) or one handed to an idle slot's turn is then only in
  this process's memory. The synthetic record carries `_report_owed`, so the
  gateway hands the debt on as
  a `SubagentDelivery(..., report_owed=True)` with the digest's held deliveries
  or the announce's ledger entry, and the settle that delivers it
  (`_settle_digest_holds`, `settle_queued_delivery`) clears the mark instead of
  writing a `delivered` tombstone (the row has no run folder). The digest settle
  clears it only on delivery: it runs whenever `_on_done` returns, so when the
  gateway gave up on the digest's channel or cron injection (the flusher, the
  last member or the reaper's forced flush, carries `_report_undelivered`) it
  detaches the held expiries without clearing them, and they stay owed. A
  digest whose flusher reports to a parent a teardown retired clears the held
  expiry's mark, since its parent is gone. A process lost in between, or a report the
  shutdown drain cancelled, leaves the row owed, and the next start reports it:
  `taskq_schedule_owed_replay` (from `taskq_boot_dispatch`, from
  `_initialize_taskq` when the store attaches after the reaper started, which is
  the gateway's boot order and every re-open, and from each reaper sweep) runs
  `taskq_replay_owed_expiries_async`, which reports every `failed` row
  `TaskStore.owed_reports` names exactly as a live expiry is. It reads a page
  at a time, resuming after the last row it reported, and is marked done only
  once a page comes back short; a read the store refuses leaves the replay
  unfinished for the next sweep, and one in flight is never started twice. No
  replay runs while the gateway holds dispatch for its memory barrier
  (`_queue_dispatch_held`): a report injected then fails on
  `MemoryStartupUnavailable`, which the injector swallows, so the report would
  look run and its owed mark would be cleared undelivered.
  `release_queue_dispatch` schedules the replay instead. Rows
  this incarnation owes are never named, since their reports are in flight
  here. The guarantee is at least once: a clear that is itself lost (the store
  unreachable, or the process ending between the report and the write) makes
  the next start report the expiry again, never not at all. **A parent that ended is not rebuilt:** a parent-end
  teardown's store sweep stops every waiting row of that parent its snapshot's
  fence does not record, so such a row is cancelled, not expired. Between the
  snapshot and that sweep the row is still queued and can expire; an expired row
  whose parent has a teardown open and that the fence does not record
  (`CancellationCoordinator.accepted_before_open_teardown`; a fence whose store
  read was refused stays open until the reaper's retry lands) is added to
  `_teardown_cancelled_ids`, so its card ends but nothing injects into the
  retired conversation, while a successor's own rows under the same key report
  as usual. The fences are this process's only: after a gateway restart, an
  owed report of a parent torn down before it is replayed to that key like any
  restored row. A floor wait with no durable row (incognito or temporary
  memory, or the task queue off) is not in the store, so the gate bounds it
  itself: `_floor_waits` keeps its parked time the same way (each park from its
  start to its planned end, cut by now, so time waiting for a slot between
  parks is not counted), and the re-check that finds the host still short once
  that time reaches the bound ends it with the same `never started: waiting for
  memory`, through `_announce_rejection` like the pressure hold's expiry.
  `_forget_pending_start` drops the clock. The macOS kernel memory-pressure
  hold reads the same key for its own per-start and per-episode bound (*macOS:
  the kernel memory-pressure hold*). No separate "still waiting" notice is sent while a row waits: the
  queued card and `queued_wait_text` say why it waits until the bound ends it.
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
  head winning a tie, restricted to the highest per-chat priority tier present
  (`low` / `medium` / `high`, read from the lane priority source; the
  `system` lane is never filtered out by a tier and keeps its weighted share,
  so a `high` chat cannot hold waiting cron and hook spawns back indefinitely;
  see [taskq.md](taskq.md) § Fairness lanes). `_drain_queue_impl` pops that index, not index 0. With
  only the child reserve left, "eligible" means nested (`entry_is_child`:
  `parent_session_key` starts with `subagent:`) **and the tier is not applied**
  (`tiered=view.root_slot`; the `children_only` top-up likewise gets a refill
  scheduler with the tiers cleared): every candidate there is a nested row of a
  tree already in flight, and a tier would let one chat's queued child take the
  reserve pass after pass while another chat's tree waits on its own child. A
  tier reorders new work; it never withholds the reserve a running tree needs.
  When no window entry
  qualifies the window is topped up with `children_only` rows and the pick
  runs once more.
- **The lane weights also weigh memory.** The round-robin orders picks among
  rows already queued; it cannot stop one lane's wave taking every start memory
  allows. The per-lane memory share in the gate does (*Memory waits: event wake
  and the per-lane share*), from the same `agent.lane_weights`.
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
- **An id stays pending wherever the row waits (`is_queued`).** A spawn the
  cap queued, or the memory / posture guard deferred, already returned its id
  but has no `_agents` row, and the eviction above moves a windowed row back to
  store-only. The app SDK's serial-lock done-probe (`spawn_sdk.build_done_probe`)
  reads an untracked id as done UNLESS `SubagentManager.is_queued` names it, so
  every such place must answer: a missed one releases the caller's guard and lets
  a duplicate of not-yet-run work be queued (#15668). `is_queued` is False for
  an id with a live `_agents` row, then True when any of three holds:
  - **`_queue`** names it (a fresh entry; a `_resume_id` entry is a resident
    run and never counts). The window add / pop and the eviction move it in and
    out.
  - **`_dispatch_window_ids`** names it. The pump adds the id when it pops a row
    (beside `_dispatching_ids`, the depth exclusion). It drops it when that one
    attempt ends: a refused or re-queued `spawn`, a claim that did not proceed,
    or the inner or outer `_unmark_dispatching`. A RETAINED claim is the
    exception and keeps it until `retry_retained_claims` settles the row.
    `_dispatching_ids` is dropped at every attempt end, retained or not. For a
    non-durable row (`incognito` / `temporary`, or no store at all) this set is
    the only record between pop and registration.
  - **`TaskStore.is_unstarted`** names it, for every durable row. The store
    keeps an in-memory index of the ids in a claimable or `admitted` state (the
    `_SQL_UNSTARTED` set `count_pending(include_admitted=True)` reads). It loads
    the index at `open` and updates it after the commit of every state write
    that can cross that set: `accept`, `insert_if_absent`, `transition` (and so
    `finish` / `advance` / `enter_wait`), `cancel`, `wake_wait`. A `claim` stays
    inside it (claimable to `admitted`). So the cap's store-only
    branch, a pressure deferral at accept (sync `spawn` or `spawn_async`) or at
    drain time (`park_defer`), the eviction and a retained claim all stay named
    without a manager-side add. Every terminal write unnames the row: settle,
    queued cancel, boundary cancel, `cancel_tree`, wait expiry. The manager
    keeps no set of its own for this, so a new path that moves a row on disk
    needs no extra bookkeeping. The read is a set lookup under a lock that no
    I/O is ever done under, never SQLite, because the probe runs on the gateway
    loop. A write by another connection on the same file is not seen until the
    next `open`.

  Pinned by `test_taskq_admission_integration.py::test_is_queued_*` and
  `test_taskq_store.py::test_unstarted_index_*`.
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

Large waves must not flood the WS socket, the parent LLM's context, or the UI. The mechanisms below do not change small spawns: WS coalescing/replay-batching are inert below their thresholds; digest chunking applies uniformly to every multi-task wave (single-task spawns behave byte-identical to legacy):

- **Batch identity**: `spawn(batch_id=..., batch_total=...)` (threaded from `spawn_run tasks=[...]` — one 12-hex id per multi-task call — via `POST /api/spawn` transport params; survives the stagger queue). `spawn_batch_started {batch_id, count}` fires once per batch on its first started member; the id rides every WS frame (`base["batch_id"]`).
- **Event coalescing** (`subagent_scale.SubagentEventCoalescer`, wired in the gateway's `_subagent_event`): above 8 active agents, `subagent_tool`/`subagent_stalled`/`subagent_retrying` buffer per-agent (latest state wins, merged) and flush every ~1s as ONE `subagent_batch_update {updates:[...]}` frame to all clients; `subagent_chunk` text buffers append-concatenated (16KB/agent cap) and flushes as `subagent_batch_chunks {chunks:[...]}` to subagent subscribers only. Lifecycle events (`spawn`/`done`/`recovering`/`injection_failed`/`batch_*`) are NEVER coalesced, and a `done`/`spawn` flushes buffered state first so ordering is preserved. Non-int active-count fails open to pass-through.
- **Chunked wave-digest completion injection** (gateway `_subagent_done`): every batch member is accounted per `batch_id` (this is the single completion consumer for all terminal paths). Every multi-task wave (`batch_total > 1`) delivers results to the parent queue-style: completed members are HELD, and every `SUBAGENT_DIGEST_CHUNK_SIZE` completions (default 10, env `KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE`, clamped 1..1000) flush ONE `[Subagent batch completion event]` chunk digest — failures first with detail, successes as one-line `result_path` pointers (60KB cap per chunk); the final member flushes the remaining partial chunk. A 60-agent wave = 6 digest turns spread across the wave's runtime — bounded chunk size, incremental signal, and no straggler-gated mega-digest. Chunk buffers (`fail_lines`/`ok_lines`/`guard_msgs`/`held_ok_deliveries`) reset per flush; cumulative `ok`/`err`/`stopped` counts ride the final chunk's summary. **Spawn discipline**: non-final chunks instruct the parent NOT to spawn new sub-agents while batches are still arriving; the final chunk releases the gate ("finish processing all results before spawning follow-ups") — mirrored by a line in the `spawn_run` tool description. **Chunk order is FIFO**: the injection busy-check (`_injection_slot_busy`) treats a live `slot.task` — the claim assigned synchronously at dispatch — as busy in addition to `slot.running`, so a later chunk waits behind an injection that is dispatched but still inside `bounded_chat_turn`'s off-loop timeout resolution, instead of racing ahead of it or assigning `slot.task` over the earlier chunk's still-pending task. Single-task spawns have no batch identity and keep the plain per-agent injection. A batch member rejected at spawn (empty task, low memory, cwd, governance, bad agent) is counted as submitted AND announced through the done callback with its batch identity (`_announce_rejection`) — so a rejection that closes the wave still reaches the consumer and releases held sibling results (non-batch rejections do not announce; the caller gets the error synchronously). `batch_finished {batch_id, total, ok, err, stopped}` broadcasts for every batch regardless of size. **Wave liveness (lost-submission backstop)**: a member rejected before reaching `spawn()` or lost during transport is counted in every sibling's `batch_total` but never in `submitted` — un-reconciled, the count-driven `batch_members_pending()` wedges the wave forever. Three layers close it: (1) `api_spawn` marks in-process rejections/capacity with `counted: true` (preserved through the MCP client's error flattening); (2) `spawn_run` best-effort POSTs `/api/spawn/lost` for each explicit UNcounted rejection, which calls `record_lost_submission` — counts the member as submitted and announces a synthetic terminal failure through the completion consumer so the wave closes; uncertain transport failures are not immediately reconciled because the gateway may have accepted them; (3) the reaper's `_sweep_stuck_waves` (every sweep) force-reconciles uncertain or lost submissions when `submitted < expected`, all registered members are terminal, nothing is queued, and no submission progress occurred for `_WAVE_STUCK_SECS` (1800s / 30 minutes — deliberately generous; it is a lost-submission backstop, not an execution deadline, and it only fires once every registered member is already terminal, so it never cuts a live member) — one lost member per sweep, converging across sweeps; this also bounds the `_batch_submitted`/`_batch_progress_ts` leak. Straggler-held partial chunks are bounded by the **hold deadline** (below), not by the member's hard ceiling.

- **Digest hold deadline (straggler escape hatch)**: both chunk triggers are event-driven — a COUNT trigger (`SUBAGENT_DIGEST_CHUNK_SIZE` pending completions) and wave close — so neither can fire while a straggler is simply *not finishing*. With the default count (10) above any wave size the concurrency cap realistically produces (2–5), the count trigger is unreachable and wave close becomes the ONLY flush: every sibling's finished result is withheld for the slowest member's entire remaining runtime, and a member that HANGS rather than fails withholds them for the full `_TIMEOUT_SECS` reap — up to 3 hours of total silence, indistinguishable from a dead session (issue #2215). The reaper's `_sweep_digest_holds` supplies the LATENCY trigger the count lacks: when the OLDEST outstanding hold in a live wave ages past `DIGEST_HOLD_SECS` (default 120s, env `KIROCREW_SUBAGENT_DIGEST_HOLD_SECS`, clamped to `_TIMEOUT_SECS`; `0` opts back out to count-trigger-only), `force_digest_flush` announces a synthetic **flush-only** record through the single completion consumer — the same re-entry mechanism `record_lost_submission` uses, so digest composition, routing, and the held-tombstone settle contract stay in one place. The record carries the wave's `batch_id` but is NOT a member: `_digest_flush_only` makes the gateway skip every per-member side effect (terminal WS event, orchestration tracker accounting, `done`/`ok`/`err` counters, digest lines) and only force the pending chunk out. **One knob, two jobs, now split**: the count keeps bounding digest SIZE for large waves; the deadline caps worst-case delivery LATENCY at every wave size. A wave whose members all finish within the deadline of each other still delivers ONE consolidated digest, so the deliberate small-wave behavior is unchanged. The forced chunk is labelled honestly as a PARTIAL release (`k/k+1`, "N of M delivered, R still running") and tells the parent to synthesize what it has rather than keep waiting. Hold bookkeeping: the gateway stamps `_digest_held_at` when it holds a member and clears it when that member's chunk fires — deliberately separate from `_digest_held`, which is the restart-safety flag the run loop reads and which the sweep must never mutate. The sweep is skipped entirely when `batch_members_pending()` is False, so it can never race the real wave-close digest into a duplicate delivery.
- **Reconnect replay batching** (`ws.py`): more than `SUBAGENT_REPLAY_BATCH_THRESHOLD` (8) replay frames collapse into ONE `subagent_snapshot_batch {items:[{type, data}]}` frame; the client fans items into the per-frame reducers.
- **Stall two-sweep confirmation** (`_maybe_flag_stall`): a quiet agent is flagged `stalled` only on the second consecutive idle reaper sweep, so healthy-slow agents do not amber at scale. [Idle-Stall Detection](#idle-stall-detection) owns the verdicts, the activity that resets suspicion and the cases that skip the confirmation.

**Retry endpoint**: `POST /api/spawn/{agent_id}/retry` re-spawns a terminal FAILED agent's original task (never running — would double work; never user-stopped — deliberately killed; native rejected). New id, no batch identity carried (a finished wave's digest is never reopened). Backs the UI's "Retry failed (N)" control. A failed run has at most one successor (`SubagentManager.claim_retry`, taken before the first await): a second retry, or a retry of a run already continued or whose `subagent:<id>` conversation another run holds, answers `409` with `code: "retry_superseded"` naming the run that owns the task. A retry that started nothing releases its claim (`settle_retry(old, None)`); one that started records the new id, and one whose start raised keeps the claim as `SUCCESSOR_UNKNOWN`, because its durable row may already exist. The other direction is symmetric: `spawn_continue` of a retried run answers `conversation_busy: run X was retried as run Y, which owns its task now`.

Delivery debt captures the member id, elapsed time, and credits when a completion is queued or held for a digest. Settlement uses that immutable snapshot rather than the live manager entry, which may be evicted before the parent consumes the completion.

## Hook Integration

### PostToolUse Firing

The subagent loop fires `PreToolUse` and `PostToolUse` (on `EVENT_TOOL_RESULT`),
mirroring `chat_runner.py`. Where `PreToolUse` runs depends on the backend
(`agent_sdk/spec_hooks.py:turn_spec_hooks`):

- On a backend that receives the agent spec's `hooks` itself, `PreToolUse` is an
  informational fire on `EVENT_TOOL_CALL` (`fire_tool_hooks`) and cannot refuse
  the call.
- On an `ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS` backend (KAS, goose, opencode) the
  turn is gated: the Hooks page's hooks and the run's spec hooks run together,
  fail-closed, on the call's permission request
  (`hook_runtime/hook_dispatch.py:permission_pre_tool_block`, the ladder's
  spec-hooks floor), and can refuse it. The informational `EVENT_TOOL_CALL` fire
  is skipped on such a turn, so each hook runs once.

The tool name is cached on `EVENT_TOOL_CALL` by `tool_call_id` and looked up
when the result arrives. The `Running: ` prefix is stripped so both hooks
receive identical tool_name strings. Hook errors on the informational fires are
caught at debug level so a misbehaving hook cannot break the subagent loop.

### Hook Payload Metadata

Three optional fields are passed to `ScriptHookStore.fire()` and the
`fire_tool_hooks()` wrapper when called from subagent context:

| Field | Source | Description |
|-------|--------|-------------|
| `subagent_id` | `SubagentInfo.id` | 16-char hex id of the firing subagent (None for parent) |
| `parent_session_key` | `SubagentInfo.parent_session_key` | Session key of the parent that spawned this subagent |
| `agent_role` | `SubagentInfo.agent` | Agent role name configured for the subagent |

All three default to `None` and are only emitted into `hook_event` when
truthy. Payloads are byte-identical for callers that do not supply them,
preserving backward compatibility for existing hook scripts.

Caller sites:
- `subagent_manager/run.py` (`_run_inner_impl`): passes all three at the
  informational `fire_tool_hooks` (PreToolUse, ungated turns only) and the
  `hook_store.fire` (PostToolUse) call sites; a gated turn passes them to
  `permission_pre_tool_block` through the ladder's `SpecHooks` floor
- `task_executor.py`: passes `parent_session_key` and `agent_role` (no
  `subagent_id`), with the same gated/ungated split
- `chat_runner.py` / `llm_helpers.py`: unchanged (parent context, defaults to None)

## Prompt and CLI integration

Delegation guidance is injected from `src/kiro_crew/config/prompt.md`; no dedicated
`skills/subagent/SKILL.md` ships.

### CLI: `kirocrew spawn run "task"`

Posts to the dashboard API on the configured `--port`. By default it polls until
the run finishes and prints the result; `--async` returns immediately with the
subagent ID. `kirocrew spawn list` lists current runs.

### MCP Tool: `spawn_run`

Exposed via `kirocrew-core` MCP server. Always fire-and-forget — results
are delivered back to the calling session via completion event injection.

**Single task** (bulk output the parent should not hold):
```python
spawn_run(task="grep the 2 GB build log for the first traceback")
```

**Batch parallel:**
```python
spawn_run(tasks=["search docs for X", "check pipeline status", "review CR-123"])
```

All tasks are submitted in one call; admission, staggering, and the concurrency
cap decide when each starts. The tool returns immediately with stable agent IDs.
Results arrive as `[Subagent completion event]` messages in the session,
processed by the LLM automatically.

The startup memory floor uses native Windows/macOS readings and Linux cgroup
headroom. Fresh stream progress can earn a bounded extra execution slot without
waiting for a whole long task to finish; see [adaptive-concurrency.md](adaptive-concurrency.md).

**Delegation policy.** Focused work stays in the parent by default; one task
is faster done there. The prompts license a spawn for two or more independent
tasks, bulk output the parent only needs distilled, an independent review or
reproduction whose result would be wrong if it saw this session's context, an
explicit user request, or a different agent/model/crew. This is prompt guidance only: no runtime gate
refuses a one-task call. Both spawn tools write one allowed-only SEL record,
`operation="spawn.solo"` (`source` `spawn_run` or `spawn_sub_agents`), for a
one-task call, so the one-task rate stays measurable. A runtime gate could check
only the shape of a stated reason, not its value. The `solo_reason` /
`solo_details` fields are accepted by validation and ignored, so skills and
workflows that send them are not refused as unknown fields. New spawns never set
the run record's `delegation` field; a persisted record that carries one keeps
it, and a retry or continuation inherits it.

**Parent work and event delivery.** `POST /api/spawn` returns
`parent_work_supported=true` only for a parent resolved to a dashboard-owned
slot (including linked channels). `spawn_run` then allows one short step of
ready, non-overlapping parent work, at most one minute, before yielding the
turn. This is model guidance, not a runtime timer. Without that receipt, or
without useful parent work, yield immediately. Channel-only, nested and
background callers retain the immediate-yield boundary. `spawn_continue` also retains its
immediate-yield guidance. Do not duplicate delegated work or poll to stay busy.

`POST /api/spawn` answers `{id, task, status, parent_work_supported}`. `status` is
`spawned`, or `queued` with `reason` and `reason_detail` when the gate deferred or held
the row (`DEFERRED_QUEUED_REASONS`); a row waiting only for a slot or the stagger tick
keeps `spawned`. Optional keys are `effort_dropped` / `effort_applied` for a
`reasoning_effort` request and `conversation` for a continuable spawn. Refusals carry a
`code`: `400 invalid_target_member` (conflicting `target_member` and `crew`),
`404 unknown_member`, `409 member_identity_unavailable`, `429` with `counted: true` at
capacity, and `400` with the rejection's `code` and `counted: true` for a spawn rejected
inside the manager.

| Owner | Enforced behavior and evidence |
|---|---|
| `dashboard/handlers/messaging.py::parent_work_supported`, `dashboard/messaging_api/spawn.py::api_spawn` | Resolves the actual parent slot and reports delivery capability in the receipt. |
| `subagent_manager/admission`, `subagent_persistence.py` | Existing capacity/queue/depth/cleanup rules remain authoritative; delegation metadata follows the run. |
| `slack/gateway.py::_subagent_done` | Busy dashboard turns are awaited through `asyncio.shield`, then completion is injected or queued; the parent edit is not interrupted. Delivery retention starts on consumption. |

The existing full-batch barrier remains: collect all terminal outcomes before
new dispatch. A failed/cancelled child is terminal for the barrier, not a
successful task. Parent verifies artifacts and actual execution evidence,
revalidates stale results against new instructions, and checks side effects
before retrying. Dispatch, yielding and a child's success claim are not final
task completion. The default prompt carries this policy. Conductor skills retain
their explicitly selected coordination role; this policy does not convert them
into implementation workers.

Parameters:
- `task` (str): single task description
- `tasks` (list[str | dict]): multiple tasks for parallel execution, dispatched as one wave (one `batch_id`, one completion digest). An entry is a prompt string, or an object `{task: str, model?: str, reasoning_effort?: str}` validated by `SPAWN_RUN_TASK_ITEM_SCHEMA` (unknown keys refused, each field under the same rule as its top-level counterpart; errors name the path, e.g. `tasks[0].cwd`). An object's non-empty field overrides the call's batch-wide value for that task only, so the same prompt can run on two models and be reviewed together. A per-task template stays in `agents`. Every other parameter (`max_turns`, `cwd`, `keep`, `crew`, `target_member`, `include_*`) stays batch-wide; it can join the object form later without another shape change. Effort verdict lines name each task's own level.
- `cwd` (str, optional): absolute path to launch subagent in. Must be under a configured `subagent_cwd_allowed_roots` entry (default: `~/workspace`, `~/workspaces`, `~/workplace`, `~/workplaces`). Validated via realpath + prefix match. Pool skipped when cwd is set. These roots are a least-privilege allowlist and are never widened automatically: a persisted list whose roots all fail to exist on the host rejects every cwd, and the operator must edit `agent.subagent_cwd_allowed_roots` (or delete the key to take the shipped default). Neither the loader nor the guard stats the configured roots.
- `max_turns` (int, optional): override tool-call budget for this spawn (default: config or 1000)
- `agent` (str, optional): one agent template applied to every task.
- `agents` (list[str], optional): per-task agent templates; length must match `tasks`.
- `crew` (str, optional): target Crew Member whose memory and provider template apply to every task. Any existing member can be named; its `triggers` only decide automatic routing (`select_crew` / `route_crew`), not whether it can be named here. An unknown name is refused with `unknown_member`.
- `target_member` (str, optional): explicit Crew Member selector; it must not conflict with `crew`.
- `model` (str, optional): batch-wide model override; a non-empty effective model pin forces a dedicated process.
- `keep` (bool, optional): request guaranteed resumability on a dedicated process and extend retention; ordinary runs remain continuable best-effort during their result-retention window.
- `reasoning_effort` (str, optional): per-call reasoning-effort override (`low`/`medium`/`high`/`xhigh`/`max`), batch-wide like `model`. Precedence: per-call value → `agent.role_efforts['subagent']` pin → provider default; `""`/absent changes nothing. Like a model/effort role pin, a non-empty value forces the dedicated-process path (the parent's shared runtime cannot switch effort per session), so a wide fan-out pays a full process per subagent — and that cost is paid even when the resolved model turns out not to support effort (the level is then dropped at the provider factory). Carried through the stagger queue and the retry endpoint like the context-group flags. NOT inherited by `spawn_continue` — a continuation resolves effort fresh (role pin, else default), the same parity as `model`. When the requested effort cannot take effect, the gateway says so: `/api/spawn` resolves the model the factory's effort gate will see (per-call value, else the subagent role pin, else the selected member's own model pin, the provider template's pin, and the global fallback) and returns an `effort_dropped` reason on the success response, which the tool renders as one attributed line per distinct verdict — subagents sharing an identical verdict (the usual case, since the value is batch-wide) are collapsed into a single line naming all of them, while differing verdicts keep their own attributed lines — including the default case where nothing is pinned and the model resolves to "auto". When the effort WILL apply, the response instead carries an `effort_applied` note naming the resolved model and the family-specific settings key (`reasoning` for GPT, `output_config` for Claude) it is delivered under, rendered the same way — so both outcomes of a requested effort are visible in the tool result. A role-pinned effort that will be dropped (no per-call effort involved) still surfaces in the gateway log at warning level, since the tool caller never asked for it — that warning is emitted by the provider factory's effort gate itself (`config/loader.py`), the single authority that drops the level, so one log line covers every surface that funnels through it (spawn, dashboard slot, cron) and cannot drift from the decision it reports on. The provider factory remains the single dropping authority; the report never rejects or alters a spawn. A `tasks[]` object entry may override it for one task (see `tasks` above).
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
changes submission, allocation, governance, or the captured memory binding.

Response semantics:
- An ID means the submission was accepted. Running and queued work return the same stable agent ID; capacity or stagger queueing preserves that ID when the row drains. Treat it as an identifier, not a result path.
- An explicit HTTP error response means the submission was rejected and is reported as `failed to start`; rejected work is never described as queued.
- A transport failure has unknown acceptance status because the gateway may have accepted the work before the response failed. The response warns against automatic retries and directs callers to wait and recheck `spawn_list` or completion events first. An empty immediate `spawn_list` result is inconclusive: accepted spawns that have not started are listed as `[queued]`, but a submission still in flight is not listed yet. If the request was truly lost, accepted siblings may remain held until the `_WAVE_STUCK_SECS` backstop (1800s / 30 minutes) reconciles the wave.
- If every submission is explicitly rejected (with no transport uncertainty), the response states that none of the requested subagents were started and does not promise completion events or suggest polling.
- For a partial batch, accepted IDs remain paired with their tasks, rejected tasks appear in a separate failure section, and completion guidance applies only to accepted submissions.

### MCP Tool: `spawn_sub_agents`

Exposed via `kirocrew-core` MCP server. Unlike fire-and-forget `spawn_run`,
`spawn_sub_agents` is **blocking**: it spawns one or more sub-agents in
parallel, waits until all of them finish, then returns their collected
results inline to the calling tool invocation.

Each sub-agent runs as its own Kiro Crew-owned ACP session (via
`SubagentManager`), so its text and tool calls stream live to the Activity
tab (`subagent_spawn` / `subagent_chunk` / `subagent_tool` / `subagent_done`
WS events) while the parent blocks.

Native kiro-cli `subagent`/`use_subagent` crews run inside the parent's
kiro-cli process rather than as Kiro Crew-owned sessions. Kiro Crew surfaces
those in the Activity tab too, by observing kiro-cli's sub-agent
notifications — one card per sub-agent, with each inner tool call and its
output attributed to the right card.

```python
spawn_sub_agents(agents=[
    {"prompt": "list python modules"},
    {"prompt": "summarize last 5 commits"},
])
```

Parameters:
- `agents` (list[dict], required): each item is `{prompt: str, agent_or_mode?: str}`. `prompt` is truncated to `MAX_MEDIUM_STRING`; `agent_or_mode` to `MAX_SHORT_STRING`. Entries with an empty prompt are skipped.
- `cwd` (str, optional): absolute path to launch all sub-agents in. Must be under a configured `subagent_cwd_allowed_roots` entry (default: `~/workspace`, `~/workspaces`, `~/workplace`, `~/workplaces`), same validation as `spawn_run`.
- `include_memory` / `include_lessons` / `include_project` (bool, optional, default `true`): the same batch-wide context switches as `spawn_run`.

Blocking poll semantics:
- Each sub-agent is spawned via `POST /api/spawn` (with `parent_session`), then the handler polls `GET /api/spawn/{id}` every 2s until every sub-agent reports `done` (or `error`).
- An errored/crashed sub-agent is treated as settled so one bad agent cannot keep the loop spinning until the deadline.
- A member that is accepted but not started answers `queued: true` with `done: false`, and when the gate DEFERRED it (its own `reason_detail` sentence, sent only while the deferral is in force; the parent-wide `reason` label is last-writer-wins and not trusted for this) it SETTLES the wait: the call returns once every member is done, errored or deferral-held, so one gate-deferred member does not hold a dashboard parent's turn inside the tool for up to `max_wait`. A member queued only behind the concurrency cap (`concurrency_limit`) or waiting to resume is NOT settled: it drains with the wave, and the tool returns its result (a sub-agent parent still waits for its own slot grant afterwards, `_hold_for_parent_resume`, which follows its children). Done and queued members are not polled again; an error settles only the pass it was seen in, since it can be one failed poll of a running member. A queued member is reported ONCE, in the `{"status": "queued", "agents": {id: reason}}` record (the reason is its current wait, `queued_wait_text`), never as an `error` member too: a caller shown an error for accepted work dispatches it again. Its result arrives later as a completion event, and it is never marked collected. A queued member that already ran and waits to resume (`resuming: true`) is reported under `still_running` as `waiting_to_resume`, never as not started. "Held" is read off `queued: true` only, never off a 404's prose; a member the accept answer marked deferred whose final poll failed in transport keeps its error entry plus the hint "accepted at spawn time; its state couldn't be read now; check spawn_status before re-spawning".
- The loop pings `POST /api/session-keepalive` every 60s so the gateway's `is_responsive()` does not flag the (legitimately long-blocked) session as stale and SIGTERM the ACP subprocess mid-poll. The `wait` tool pings the same endpoint for the same reason but on a **5s** interval and with a body, because there the reply doubles as an early-end control channel (see `modules/learn-cron-dashboard.md` § Wait countdown and early end); this loop sends `{}` and ignores the reply, so 60s is sufficient.
- `max_wait` defaults to 7200s (2 hours), clamped to `[60, 7200]`, and is configurable via the `KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT` environment variable. The deadline uses `time.monotonic()`.
- Returns a newline-separated list of per-agent JSON results (`status`: `completed` / `error`), followed when they apply by the `still_running` envelope, the `queued` record of members not yet started, a `resume_pending` note and a `spawn_errors` record, all redacted for credentials and exfiltration URLs.
- **The result is held until the parent holds its slot again.** A `subagent:<id>` parent yielded its lane slot for this wait (W3); once every child is settled the tool long-polls `GET /api/spawn/{parent}/resume?wait_secs=8` (`_hold_for_parent_resume`) until `granted` — each request is held server-side on the grant event, so there is no grant interval — bounded by the same `max_wait` deadline. `known=False` (finished run, other incarnation, legacy gateway error payload) and a chat-turn parent release the hold at once. If the deadline passes while the slot is still ungranted, the children's results are still returned, followed by `{"status": "resume_pending", "parent": id, "note": ...}`.
- **The wait expiring is a fact about the call, not about the children.** When `max_wait` passes with sub-agents unsettled, the result ends with ONE envelope `{"status": "still_running", "task_ids": [...], "states": {id: "running" | "waiting_permission" | "waiting_to_resume"}, "waited_secs": N, "query": "spawn_status/spawn_list", "note": ...}`. Members accepted but not started are not in it: they are reported in the separate `{"status": "queued", "agents": {id: reason}}` record described above. Those children are NOT cancelled, NOT marked failed or timed out, NOT marked collected (so their completion events still inject), and keep their own execution budget (`subagent_timeout_secs`); the SEL outcome is `partial` with a `still_running` count. Settled siblings' results are still returned inline in the same reply.

Difference from `spawn_run`: `spawn_run` returns immediately and delivers
results later via completion-event injection; `spawn_sub_agents` blocks and
returns the aggregated results directly, so the calling agent can reason over
them in the same turn.

## Orphan Recovery & Tombstoning

Folder-per-agent persistence at `~/.kiro/crew/subagents/{id}/`:

```
~/.kiro/crew/subagents/{id}/
  state.json      # {task, parent_session_key, started, pid}
  result.txt      # result text (APPENDED per streamed chunk, started afresh at each attempt's first chunk; rewritten whole from memory by a claimed completed ending, capped in place by any other)
  tombstone.json  # {error, elapsed, timestamp} (written on failure/orphan)
```

### Gateway Restart Reconciliation

On startup, `SubagentManager` scans `~/.kiro/crew/subagents/` and reconciles:

1. **PID alive** → kill process group, deliver result if available, tombstone if not
2. **PID dead + result.txt exists** → deliver result to parent session
3. **PID dead + no result** → tombstone (`gateway_restart`) and the `lost to gateway restart` notice, unless the run recorded a whole answer without text (below)

The kill in step 1 is audited on its result, in the reaper's vocabulary: `_kill_orphan_pid`
returns `None` once the process is gone (signalled, or already exited by the time the signal
went out — nothing to kill is not a failure) and otherwise the failure it raised
(`PermissionError: …`). Before the signal the kill passes the runtime-ownership gate
(`authorize_runtime_kill`: a shared runtime carries other tenants, and the per-run
`state.json` is no authority over them) and the teardown barrier
([runtime ownership](runtime-ownership.md)). `orphan_reconcile_kill` is audited `refused`
(the gate or the barrier declined, and no signal was sent), `killed`, or `failed` with that
reason in the row's `error` — never `killed` for a process the kill left standing. The folder
is tombstoned either way, so the row is the only place the process's fate is recorded. The
signal is awaited through `platform_compat.kill_pid_async`: the reconciliation runs on the event
loop and the Windows kill is a `taskkill` spawn that waits up to five seconds for its target,
so that wait runs on the subprocess executor while the loop keeps serving (POSIX `os.kill` is
non-blocking and runs inline).

A surviving `result.txt` is not by itself a result. It is appended per streamed
chunk, so it is non-empty from the agent's first token and a size check cannot
tell a finished answer from an opening sentence, and an append that failed leaves
a hole. So a run that claimed its completed ending (the ending claim above)
rewrites `result.txt` whole from the text it holds in memory — capped in bytes at
character boundaries (`cap_result_bytes`), written by temp file and rename,
fsynced with its folder; an empty answer leaves no `result.txt` at all — and only
then records `result_complete` in `state.json`, both in one drained worker
(`write_finished_result`). A failed write records `result_complete=False` and
falls back to capping the streamed file in place. Every other ending -- the
tail's, the turn-limit and escalation bails, the deadline, a cancel, a raised
error, a reap -- caps the streamed file in place (`cap_result_file`) and records
`result_complete=False` in the same worker, once, from `_run`'s `finally` before
its report is spawned, so the 512 KB bound holds for partials too, and a whole
answer a stop got to first is never flagged. The run is `done` by then, so
`_conversation_busy` does not count it, and the cap holds its conversation
(`_abandoned_state_writers`) until the worker lands: a release mid-write would
write `keep` on the loop for the worker's whole-file `update_state` to roll back
(`test_the_final_cap_of_a_done_run_holds_its_conversation`). The hold is kept
per worker, so an earlier write of the run still detached past its drain
landing first does not release the cap's hold; the last worker to land does
(`test_the_hold_lasts_until_the_last_writer_of_a_run_lands`). Only a
cancel-recovery respawn skips it, since its next attempt starts the file
afresh. The cap rewrites the file in place rather than by rename: it only
shrinks, so a full disk cannot refuse it, and on Windows a reader holding the
file shares writes but not the delete a rename needs. An attempt's first text chunk starts the in-memory partial
(`streaming_text`) and `result.txt` afresh, so a respawn never glues its text to
the interrupted attempt's, and that partial stays where readers look until the
new attempt has text. The two restart on their own facts: the partial on the
attempt's first chunk whatever the disk does, the file on the first write that
succeeds, so a `result.txt` that cannot be written (a full disk, a removed
folder) never costs the partial a Stop delivers, which is then the only copy.
Until a write succeeds, each chunk starts the file with everything the attempt
has streamed so far, not with that chunk alone, so a refused first write leaves
no hole at the head of the fragment a restart finds. That write grows with the
answer, so it runs off the loop in a worker drained like a state write
(`_start_result_file`), and the live partial takes the chunk before it, so a
Stop landing in that wait still delivers the chunk. Later chunks append on the
loop, and an append the disk refuses sends the next chunk back to that start
write, so a lost chunk leaves no hole mid-file either. While the disk keeps
refusing, each chunk retries the start write in that worker; a refusing disk
fails at the open (a removed folder) or the first block (a full disk), so a
retry writes nothing and never runs on the loop.
That first write is a temp file and rename (`write_result_chunk(fresh=True)`),
so a write that fails leaves the earlier attempt's partial on disk rather than
an emptied file. The file is written as streamed (`newline=""`), LF on every
platform, as the whole rewrite is. `subagent_persistence.result_is_whole` reads the flag.
Reconciliation classifies a non-empty `result.txt` with it —
`result_available` with it, `partial_result` without — and the
`partial_result` notice tells the parent the text is an unfinished fragment
rather than pointing it at a result to read. A finished run's
`gateway_restart` tombstone records `outcome: completed` and its notice is `ok`
(✅), with or without text, so the panel, the notice and the task queue's boot
probe give one verdict; the notice is chosen from the same record
(`result_is_whole`), never from a caller's say-so. What remains: a restart
between the claim and the flag write reads a finished answer as a fragment, the
safe direction; the flag never vouches for bytes an OS crash lost. A stream that
ended with no complete event (the transport died between chunks) classifies as a
success live but is not whole, so it is completed live and announced as a
fragment after a restart.
`test_subagent_stop_reason_consistency.py` pins the order, the endings and the
write; `test_taskq_reconcile.py` pins the probe.

No result is not no work. A run the restart caught before its first token has no
`result.txt`, but its CONVERSATION — every turn and tool call kiro-cli persisted
under `~/.kiro/sessions/cli/{sid}.json` (+ `.jsonl`) — is a file reconciliation
deliberately keeps (retain-by-default), and `spawn_continue` re-seeds the session
map from the run's `state.json` to resume it after a restart. The `lost to gateway
restart` notice therefore carries the run's progress (`turns`, `last_tool`) and the
resume handle (`spawn_continue(conversation="<owner>", task=...)`, where the owner is
the `conversation_key`'s subagent id when the run was itself minted by `spawn_continue`,
else the run's own id — one session-map key per sid) — `orphan_resume_hint` — but ONLY when the conversation is resumable by the one rule
`SessionMap.get` applies before it hands a sid out, `session_map.session_files_resumable`
(for kiro-cli the `.json` present and the `.jsonl` holding at least
`_RESUMABLE_JSONL_MIN_BYTES`; for any other backend `session/load` decides, so the
handle is offered and `spawn_continue` refuses typed if the session is gone). A pruned,
released or never-started kiro-cli conversation is therefore never advertised. Without
the hint the parent re-spawns from scratch and pays for the same tool calls twice.

**Orphan delivery is wired** (not a stub): the gateway registers `on_orphan_notify` (session injection — rides the parent slot's batched pending-failures drain) and `on_orphan_dm` (fallback). The DM fallback collects every undelivered orphan across the reconciliation scan and sends ONE digest message (`"N subagent(s)…"`) — never N pings; a lone orphan keeps the plain per-agent message.

### Tombstone Lifecycle

- Created on: process death without result, delivery failure, timeout, a stop (`cause` =
  `error` / `timeout` / `turn_limit` / `child_escalation_limit` / `cancelled` /
  `reaped` / `startup_timeout` / `start_queue_saturated` / `user_stop` /
  `parent_end` / `gateway_restart`; `stage_cancel` tombstones from the retired
  chat Autopilot still read as cancelled), **and on
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
  reading the retained `result.txt` (and honoring offset/limit/grep). New terminal
  records also return `elapsed` and `credits`; legacy records omit both.

### MCP Tool: `spawn_status`

Retrieves live status and a redacted partial transcript for a running subagent, or
the retained full transcript for a completed subagent. The completion event carries
a **summary + the `result_path`** whenever the completion copy was truncated
(`result_truncated`), so the parent reads the full transcript
on demand instead of re-running the subagent.

For a running in-memory record, `GET /api/spawn/{id}` keeps `done: false` and
returns the manager's bounded `streaming_text` as `result` alongside `turns`,
`last_tool`, `elapsed`, and present-only `awaiting_approval`; the tool branches on
`done` alone, so there is no separate running flag. `turns` counts permission-gated
turns charged against the run's turn budget, not tool invocations (auto-allowed
tool calls bump `tool_count`, not `turns`). Redaction runs before optional line
paging, so the response never exposes an unredacted partial and
`offset`/`limit`/`grep` work in both running and completed states. The partial
transcript is a live view that grows (and past the manager's bound is
truncated from the front), so line offsets can shift between polls and
`offset`/`limit` paging is best-effort until completion. The MCP tool labels this
output with a `RUNNING` header — or `AWAITING-APPROVAL` in its place when
`awaiting_approval` is present, because that flag means the run is parked on the
spawn gate with no process and no turn, the state `spawn_list` renders as
`awaiting-approval` and the CLI waiter reports as "approve it ... to start this
run"; when no text chunk has arrived, it says that explicitly instead of
rendering the completed-empty sentinel `_No result._`, and for the
awaiting-approval case it says to approve the run in the dashboard (Approvals) to
start it rather than promising a transcript with the completion event.

**An accepted spawn that has not started is `queued`, not "not found".** The gate
may defer a spawn (memory floor, macOS memory pressure) or queue it
for a slot, and until the pump claims and registers it, its only record is a
window entry or a task-store row. It has no `SubagentInfo` and no run folder.
"Accepted, no run yet" has ONE definition, shared by the by-id read, the listing
and the queued count (`taskq_overflow`, which every pending-work guard, the fire
gate and the depth chip read): a claimable row, or an `admitted` one (a claim in
flight, or one retained across a store outage), that no live run in this process
is registered for (`_live_run_ids`). `SubagentManager.queued_run_async(id)` /
`queued_runs_async(parent, app=)` read the store and the window, and:

- `GET /api/spawn/{id}` answers such an id, before its persistence fallback, with
  `{"id", "task", "done": false, "status": "queued", "queued": true, "agent",
  "started", "elapsed", "reason"?, "reason_detail"?, "resuming"?,
  "resuming_reason"?}` (`status` matches the accept answer, `queued` is what the
  poll loops read). When the task store cannot be read and the window has no
  entry for the id, "not queued" is unknowable, so the status, steer and retry
  routes and the ownership check answer `503 taskq_unavailable` (retryable),
  never a definitive 404. A row this build cannot model (an unknown state, kind
  or side-effect class) reads as that same outage, with a WARNING naming it. `started` is the
  row's accept time. `reason` is the parent's current wait kind (per parent, last
  writer wins, as on `subagent_queued`), and `reason_detail` is the gate's
  sentence from the row's latest `deferred` event while that deferral is in force
  AND newer than the row's last claim or transition (one batched event read per
  page). A row that already ran — `recovering` after a restart, or `retry_wait`
  with attempts — reports `resuming: true` with `resuming_reason`
  (`gateway_restart` / `retry`) instead, and no tool calls it "not started". The
  ownership check (`_spawn_scope_refusal`) takes the row's session key as the
  parent and keeps its lookup on the request for the handler. A run the pump
  registers while that lookup awaits is answered from the registry, and the
  persistence fallback answers `done: true` only for a recorded ending (a
  tombstone); a folder with none is not done. `POST .../steer` and
  `POST .../retry` on a queued id answer `409 queued_not_started`. A row the
  pump refuses at drain time (its re-check of memory mode, execution, cwd or
  governance) is failed in the store and registered as a terminal record with
  the refusal as its error, so the next poll reads the failure, not a 404.
- `GET /api/spawn?queued=1` lists them under their own `queued` key, present
  only when non-empty and bounded by the same caller scope and app claim as the
  run rows (the app filter runs inside the store read, before its cap), each row
  audited like the persisted half (`queued_app_mismatch` /
  `queued_scope_mismatch` denials, an allow per listed row). Without the flag the
  route does not touch the task store: only the spawn tools read the half, not
  the dashboard's pollers. The queued half is read BEFORE the live registry, so a
  run that registers between the two is listed live rather than in neither;
  each id is listed once — live over queued, queued over a persisted record. It
  is a page of the oldest `QUEUED_LISTING_CAP` store rows, and a cut-off page or
  an unreadable store carries `queued_truncated: true` (the bridge logs the
  transition once), because a tail read as complete says accepted spawns were
  never accepted. They are kept out of `agents` because every reader of that
  list takes a not-done entry for a run in progress.
- The `spawn_status` tool prints `[QUEUED · <elapsed>]` with the reason and a
  note not to spawn the run again; `spawn_list` (and `kirocrew spawn list`)
  prints each with why it waits, says when the list is partial, and says
  `No subagents running.` only when nothing runs AND nothing is queued.
- A blocking `spawn_sub_agents` that collects a member inline whose announce had
  already queued on the slot (its delivery timed out waiting on the tool's turn)
  has that queued announce removed and its delivery marks settled at
  `mark-collected`, and keeps its id out of `_subagents_inline_collected`. That
  set takes only ids the gateway knows, each at most `_COLLECTED_ID_MAX_LEN`
  long, and at most `_COLLECTED_IDS_CAP` in total, since only a matching
  completion evicts one.

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

When positive terminal credits were recorded, the MCP tool prefixes the full transcript
with `[usage: <credits> credits · <elapsed>]`; paged responses include the same
fact in their existing continuation header. Error responses also carry this
prefix when terminal usage is available. Historical records without both fields
keep their previous byte shape. Zero credits produce an elapsed-only usage
prefix because zero can mean billing was not reported. Elapsed time switches to
minutes and seconds at 60 seconds. Credit precision matches the dashboard: two
decimals below 10 credits, one decimal at or above 10.

### Completion Event Truncation Modes

The character cap and which end of the transcript to keep are both
configurable. Defaults preserve original behavior — opt-in to the others
when a particular agent style benefits from the change.

When truncation drops content (`SubagentInfo.result_truncated`), the completion
event is not a raw truncated blob: it carries a **first+last-words preview + the
`result_path`** (via `context_management.summarize_result`) so the parent reads
the full transcript on demand (read / grep / `spawn_status`) instead of
re-running the subagent. It is gated on `result_truncated`, so small results
still inline in full.

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
at startup via `_validated_completion_keep` in `config/sections.py`. The
dashboard PATCH endpoint enforces the same enum via
`_EDITABLE_CONFIG["agent.completion_keep"]`.

The values are threaded into `SubagentManager.__init__` from
`slack/gateway.py` (`completion_keep=`, `completion_keep_chars=` constructor
kwargs sourced from `cfg.agent.*`), and a later write to either field is
adopted live by `reconfigure` through `update_completion_keep`. User-facing docs:
[`src/kiro_crew/docs/configuration.md`](../../../src/kiro_crew/docs/configuration.md),
[`src/kiro_crew/docs/subagents.md`](../../../src/kiro_crew/docs/subagents.md),
[`src/kiro_crew/docs/troubleshooting.md`](../../../src/kiro_crew/docs/troubleshooting.md).

### Dashboard API: `POST /api/spawn`

The spawn routes are composed into `dashboard/handlers/messaging.py`, which stays
their import path. Admission and continue live in `dashboard/messaging_api/spawn.py`,
the run controls and their session fence (`_spawn_scope_refusal`) in
`run_control.py`, and the status and list views in `run_views.py`, beside it.

The view-only Agent Worlds hook polls the global `GET /api/spawn` list and reads
its `{"agents": [...]}` envelope; it does not call the parent-scoped
`running_agents_for(parent_key)`, so both dedicated and shared-process children
are eligible for sprites.

Crew binding resolution and inherited memory lookup for persisted parent runs
execute off the gateway event loop. Unavailable member memory remains a typed
refusal before any child provider is allocated.

Request: `{"task": "..."}`
Response: `{"id": "abc123", "task": "...", "status": "spawned"}`
Errors: 400 (missing task), 429 (capacity reached), 503 (subagents not available)

**Typed rejections.** A rejection raised INSIDE `spawn()` answers 400 with a
machine-readable `code` beside the advisory `error` prose (plus `counted: true` —
see Wave liveness above): `agent_not_found` for a named-but-unknown agent,
`agent_not_available` for a target the parent agent spec's
`toolsSettings.subagent.availableAgents` forbids (§ Parent agent spec allowlist),
`agent_internal` for one of Kiro Crew's own generated specs (§ Kiro Crew's own
generated specs are not sub-agents),
`spawn_rejected` for every other kind (empty task, low memory, cwd refusal,
governance). `code` is the contract and `error` is advisory (RFC 9457 3.1.3),
which is what lets the refusal sentence be reworded without breaking a client.
The identifiers are minted AT the decision — `subagent.AGENT_NOT_FOUND_CODE`
and `subagent.AGENT_INTERNAL_CODE`, returned by `_validate_agent`, and
`subagent.AGENT_NOT_AVAILABLE_CODE`, set by the gate — carried on `SubagentInfo.error_code`, and
forwarded by the handler without being respelled there, so each value has exactly
one spelling in the tree.

`spawn_run` switches on those codes for the wave short-circuit (#4842): once the
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
them be minted at the decision the way the unknown-agent refusal is; while they
are not, treat `conversation_busy` / `conversation_gone` as inferred, not
minted. Two more consumers reconstruct the same two decisions from prose
internally (`crew_chat`'s queue hold, `continuation`'s busy retry), so minting
them at the decision has to move those consumers too.

### Handler keywords (instant, no LLM)

User-typed `spawn <task>`, `bg <task>`, `spawn list`, `spawn status` are intercepted by the handler for instant execution.

## Session sharing (shared AcpRuntime)

When `agent.session_sharing` is enabled (default **on**) and the parent uses a
backend in `ACP_BACKENDS_SESSION_SHARING` (currently `kiro` or `codex`), subagents
open an additional ACP session on a **shared `AcpRuntime`** instead of spawning a
fresh process each. One process multiplexes the parent session plus all of its
subagents. Startup drops from ~3–5 s to ~200 ms and per-subagent memory from
~400 MB to near-zero.

Decision + lifecycle:

- Whether a run shares is one decision, `_sharing_plan`, which `_run_inner` and
  admission pricing both read; its terms are listed under the *Shared* start price in
  [Memory guard: what must remain after the start](#memory-guard-what-must-remain-after-the-start).
- `_create_shared_session()` resolves the parent's `AcpRuntime` via
  `_get_parent_runtime()` (falling back to `SessionManager.get_subagent_runtime()`
  — a companion runtime), calls `runtime.create_session()`, and wraps the handle
  in `AcpSessionProvider`. `SubagentInfo._session_sharing` / `_shared_provider`
  record the shared path.
- `runtime.create_session()` runs under the ACP `SessionStartGate`
  (`agent.session_start_concurrency`, see acp-client.md). `_create_shared_session`
  passes `on_gate_acquired` = `_gate_exit_reset(info)`, which at gate EXIT adds
  the wait to `info._start_queue_wait_ms` (the paused part of the start clock)
  and refreshes `last_activity`; its companion `on_gate_queued` =
  `_gate_wait_mark(info)` fires immediately before the wait for a permit begins
  and stamps `info._gate_wait_started`, and while that is set
  `_is_startup_stalled` reads the start clock as paused at that moment. So the
  startup deadline never counts time queued for a permit, however long the
  queue: time spent starting before the gate and after the permit counts; time
  queued does not. When the parent has no `AcpSessionProvider` runtime,
  `get_subagent_runtime` takes the same pair and pauses only its WAITS: the
  per-parent lock, behind a sibling spawning the runtime (queue name
  `START_QUEUE_COMPANION`), and this start's own spawn admission. The spawn's
  own work -- killing a dead runtime, the process spawn, `initialize`, the
  dead-runtime retry -- stays on the running clock, which the deadline budgets,
  so a companion spawn that hangs is reaped at the startup deadline rather than
  left to the queue cap. The paused total is capped (`_START_QUEUE_MAX_SECS`, above),
  and each queue's own holders finish within one start.
- **The dedicated-process path pauses at all three start queues.** Every `model` /
  `reasoning_effort` / `allowed_tools` / `bare` spawn takes `get_or_create`,
  which waits at `SessionManager._start_sem`, then its provider's runtime spawn
  waits at the cold-start admission and its `create_session` at the same
  `SessionStartGate`; without the pause that queue time would be charged to the
  fixed startup deadline, and a wide model-pinned fan-out would reap healthy
  starts as `Failed to start within 120s` -- the strongest single mechanism
  behind the measured ~50% loss at 120 items, and later chats overtaking a
  queued start (acp-client.md § Session-start gate) would add to it.
  `_run_inner` passes `on_gate_queued` / `on_gate_acquired` into `get_or_create`,
  which fires them around `_start_sem` itself; they ride `extra_factory_kwargs`
  to the provider factory (`config/loader.py` `_acp`, where they are NAMED
  parameters -- the `**_kwargs` catch-all would swallow them silently), into
  `AcpProvider(on_gate_acquired=...)`, and from `_start_kiro_runtime_impl` into
  both `runtime.spawn` calls and the process's `create_session`. Each callback
  receives the queue's name. They are not passed to `load_session`: a
  `session/load` resume takes no gate permit.
  ONE definition each (`RunEventCoordinator._gate_exit_reset_impl`,
  `_gate_wait_mark_impl`) serves both paths, so the clock rule cannot drift
  between them; `on_gate_queued` rides the same plumbing as `on_gate_acquired`.
  Pinned by `test_subagent_startup_pressure.py` (`TestDedicatedPathGateExitReset`,
  `TestGateExitResetIsOneDefinition`) and `test_session_start_gate.py`
  (`test_gate_entry_callback_fires_before_the_wait_and_exit_after`).
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
  (`SessionManager.release_subagent_runtime`).

Backends outside `ACP_BACKENDS_SESSION_SHARING` are not eligible and use the
per-process path regardless of the flag.

### One `$KIROCREW_SCRATCH` per session tree

A parent and every process spawned on its behalf read and write ONE work
directory under one name, however the run was placed. `agent_scratch` allocates
scratch per PROCESS and the sandbox masks the whole scratch root, re-exposing
each process only its own directory (`extra_private_dirs`); on their own, that
would give a dedicated subagent process and a companion runtime an EMPTY
`$KIROCREW_SCRATCH`, so a brief the parent staged there would be unreadable to the
child, and a `_bg` runtime recycled for age or RSS mid-task would hand every
session it took over an empty directory as well.

The mechanism is one extra window plus one env value, applied at three seams:

- **What is inherited.** `work_scratch_dir`, declared on the `LLMProvider`
  ABC with a `None` default (harness-parity H14, like `tool_search_settings`)
  and answered by `AcpProvider` / `AcpSessionProvider` from the process that
  actually serves the session (`AcpRuntime.work_scratch_dir`,
  `AcpClient.work_scratch_dir`): the directory it exposes as
  `$KIROCREW_SCRATCH` — the one it inherited when it joined a tree, else its
  own allocation. `session_allocation.parent_work_scratch_dir(owner, parent_key)`
  reads the capability off the parent's live provider, never probes a client
  attribute (`None` when the parent has none).
- **How a spawn takes it.** `AcpRuntime(shared_scratch=…)` and
  `AcpClient(shared_scratch=…)`, threaded through `AcpProvider` and the `_acp`
  provider factory. `AcpProvider` keeps the value itself, because on the kiro
  backend `_start_kiro_runtime_impl` REPLACES the placeholder `AcpClient` with
  an `AcpRuntime` it constructs (twice: the first spawn and the resume
  respawn), and that runtime is the process the dedicated subagent runs in.
  At spawn the path is re-validated by
  `agent_scratch.shared_scratch_window` (a plain directory directly under the
  managed root, never a link, never re-created when swept — a stale path is
  dropped and the spawn keeps its own directory alone) and marked ACTIVE for
  the sweep's grace window (its directory mtime is refreshed under
  `_SWEEP_LOCK`, the lock the sweep also takes for its final look-and-delete):
  the allocator may already be dead and the tree idle past the grace window —
  the crash-recovery case — and an hourly sweep landing between the mount and
  the adoption would otherwise delete it. The sweep's own rule (a fresh mtime is
  a live user, whoever owns it) then holds the tree for the hour a spawn needs
  seconds of; nothing is released, a second heir simply refreshes again, and the
  lock never covers marker I/O. If the refresh itself fails (`os.utime` raises)
  the window is handed out only when the sweep could not take the tree anyway —
  a live owner, or an absent/garbled marker — and refused for a dead-owner tree,
  since mounting one without the hold is the deletion-under-a-mount this exists
  to prevent; the spawn then falls back to its own directory. It is mounted as
  a SECOND
  `extra_private_dirs` window beside the process's own, and named by
  `KIROCREW_SCRATCH` (`agent_scratch.scratch_env(own, shared=…)`). `TMPDIR` /
  `TMP` / `TEMP` and `KIRO_CHAT_LOG_FILE` stay on the process's own directory:
  temp files are per-process by construction and two live processes appending
  to one kiro-cli log is the sharing the log pin exists to prevent.
- **Who passes it.** A companion runtime gets it from
  `_collect_parent_runtime_kwargs`; a dedicated subagent process from
  `subagent_manager/run.py`, which adds `shared_scratch` to the `get_or_create`
  kwargs (and which is also the signal that skips the warm pool —
  `pool_decision = "bypass_shared_scratch"` — because a pooled child's mounts were
  fixed when it was pre-spawned with no parent); a shared-session subagent needs
  nothing, it already runs in the parent's process. The `_bg` runtime's
  replacement in `session_background.get_bg_session` inherits its predecessor's
  `work_scratch_dir`, recorded on `BackgroundRuntimeState.inherited_scratch` from
  every runtime seen in the slot and from each replacement the moment it takes
  the slot (a backend switch retires a runtime without another acquisition
  reading it as a predecessor) — state rather than a call-local, because the
  stale runtime is detached and the slot cleared before the replacement spawns,
  and a replacement that fails to spawn must not leave the next call with
  nothing to hand on. The marker lives inside a directory mounted read-write
  into every agent the predecessor served, so it can be replaced with a link
  or garbage from inside the sandbox; when a replacement's spawn refuses to
  join it (`SharedScratchJoinError`, the subclass the spawners raise at their
  adopt site and nowhere else — a failure on the replacement's OWN marker is a
  plain `ScratchBoundaryError` and keeps the inherit, since that tree still
  holds the sessions' work), `get_bg_session` abandons the inherit and
  spawns with the replacement's own directory — the sessions lose their staged
  files to the tampering, the tree stays on disk unowned for a human, and the
  `_bg` slot is never locked out of a runtime.
- **Every process seam is enumerated.** A seam that starts a kiro-cli process
  and forgets `shared_scratch` reproduces the bug with no red test, so
  `test_subagent_shared_scratch.py::TestEveryProcessSpawnSeamIsAccountedFor`
  scans `src/` for every `AcpRuntime(` / `AcpClient(` construction: each passes
  `shared_scratch=` (or a caller-filled `**kwargs`) or is listed as a standalone
  process with the reason it has no session tree to join (the review-sage
  worker pool, the knowledge LLM pool). A new construction site fails the test
  until it is placed.

Ownership names every user. Each process that mounts a tree it did not allocate
ADDS itself to the tree's `.owner` marker once live (`agent_scratch.adopt_owner`;
one pid per line, read-modify-write under a process-wide lock, dead pids pruned
on the way, and `sweep_dead_scratch` keeps a directory while ANY named pgroup
lives). Naming only one side is wrong whichever process dies first: a parent
that crashes under running dedicated children, or a successor that fails beside
its draining predecessor, would leave a dead-only marker over a live user and
the sweep reads dead-plus-idle as reclaimable. Outcomes: `"refused"` (a link
where the marker belongs) reaps the spawn exactly as the own-directory marker
does; `"stale"` (the append failed AND the old marker could not be cleared) reaps
too, since that is precisely the deletion-under-a-live-process state;
`"unwritable"` leaves the tree UNOWNED — never swept, a visible leak rather than
a loss — and warns. `"garbled"` (a marker this module did not write: not
integers, oversized, not a regular file) also only warns and proceeds: the sweep
skips such a tree for good, so mounting it risks a leak and never a deletion,
and a fatal answer would let any agent process the tree is mounted into veto
every later spawn on it by scribbling over the marker. The marker is read to
EOF under the size cap, never in one `read`: a short read hands back a prefix,
and a prefix naming only dead pids reads as a dead owner over the live pid the
tail names. It is opened `O_NONBLOCK`: a FIFO planted at the marker's name
passes the link check and `O_NOFOLLOW`, and a blocking open with no writer
would park the sweep or a spawn's adopt forever; non-blocking, the open returns
and the `fstat` regular-file check rejects it.

A ROOT that respawns is still its tree. An `AcpClient` whose process exited and
is respawned by `ensure_ready`, an `AcpRuntime` respawned on the same object,
and an `AcpProvider` restarting the kiro runtime that served a session all
promote the directory the previous process exposed to `shared_scratch` and join
it like any inherited tree (validated, dropped if swept, adopted once live) —
the children spawned before the restart mounted that directory, and the work
staged in it is what the session was doing. `AcpProvider` records the tree off
the LIVE runtime once it takes over (`runtime.work_scratch_dir`), not the value
it was constructed with: an inherited window that was swept is dropped by the
spawn, and a restart that re-sent the swept path would drop it again and start a
third tree, losing what the first runtime staged.

Lifetime, stated on purpose: the `_bg` runtime's tree chains across every
recycle, so it lives as long as the gateway (and any agent process that outlives
it) rather than rotating with each recycle. Only the
work products under `$KIROCREW_SCRATCH` chain; `TMPDIR` temp files and the
kiro-cli log stay on each process's own directory and still rotate per recycle,
and `cap_kiro_cli_logs` still bounds the logs. Work products are what a session
deliberately keeps for its own duration, so their lifetime is the session
tree's — the accepted trade for a directory that never vanishes under a live
session. For the `_bg` runtime specifically that tree is the gateway's: every
dashboard session's work products accumulate in one directory under
`<data home>/scratch/` for as long as the gateway (or any agent process that
outlives it) runs, and nothing in-tree prunes or size-caps it — a bound would
delete a live session's files, which is the defect this mechanism removes. The
operator monitors disk for it as for the rest of the data home
(`src/kiro_crew/docs/troubleshooting.md` names the path and the remedy); the
directory is reclaimed by the ordinary liveness sweep once the gateway and its
runtimes are gone, and a gateway restart starts a fresh one. Siblings from OTHER trees stay masked either way: the mask is a
per-tree boundary, and this widens the window to the tree, never lifts the
mask. Pinned by `test/test_subagent_shared_scratch.py`.

### Parent end ends the children, on every backend

Reaping the companion runtime ends the children of a harness that multiplexes
them onto one process, because killing that process is what ends them — a side
effect, not a decision. A harness running one process per child has no entry in
`_subagent_runtimes`, so the reap reaches nothing and its children outlive the
conversation that asked for them, each holding an agent process and that process's
MCP fleet until its own `agent.subagent_timeout_secs` expires. Backends outside
the positively named sharing set take that path; the set currently contains
`kiro` and `codex`.

So every lifecycle site that releases a companion runtime also ends the parent's
runs, through the two halves above. The boundary is not re-derived per surface:
because it rides the release, the dashboard, a channel command and the idle sweep
all inherit it without a call of their own, and no backend is named anywhere in
it. `session.md` lists the sites and the two exemptions.

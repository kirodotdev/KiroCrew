# Adaptive concurrency (`kiro_crew.adaptive`)

The controller that turns the user's concurrency configuration into a runtime
cap the host can actually sustain. It implements RFC
[overload-resilience §5](../../request-for-change/rfc-overload-resilience.md#5-adaptive-controller):
the user's `agent.max_subagents` (or `agent.subagent_auto_max` when it is 0) is
the **ceiling and is never written**; a live **effective cap** starts AT it,
halves when admitted work keeps failing, and earns its way back one step per
clean window. A second track shapes the MCP gateway daemon's `SpawnGate`
capacity. That track reads free memory and the daemon's own spawn evidence
(failing and slow backend inits), halves on any one of them alone, and pauses
when memory stays at the critical line. It never reads the gateway's loop lag. Memory is bounded per start by the
spawn floor (`agent.spawn_min_memory_gb`, [subagent.md](subagent.md) *Memory
guard*), never by this cap. Nothing is ever killed to fit a smaller cap: both
actuators shrink naturally as in-flight work finishes.

**Why the execution cap does not read memory or loop lag.** It used to: it
started at `min(user_max, 4)`, halved on 250 ms of gateway loop lag alone or on
memory at the critical line, and paused dispatch at 0 under severe pressure, on
top of an auto ceiling that was itself sized from host memory. Every one of
those is a COUNT bound sitting under the per-start memory floor, so one chat's
wave held the slots (or the halved cap) another chat's subagents waited for
while memory was still free -- the "more than two chats with subagents throttle
each other" report. The floor already prices each start at what it will settle
at and queues the ones that would not leave it free, so a count moved by the
same memory only stalls work early; and the gateway's loop lag says nothing
about whether a subagent's own process is healthy. What the floor cannot see --
provider 429s (scoped, below), attributable timeouts, slow or failing MCP
servers, fd and process exhaustion -- is what still cuts the execution cap.

**Why the spawn gate does not read loop lag.** The gate admits forks inside the
MCP gateway daemon, a separate process; the lag is the dashboard gateway's own
event loop. On one idle host (0 spawn failures in 621, load 0.09 per
core) a single 468 ms lag sample cut the gate to 1, 49 times in a day, and with
no idle recovery it stayed there: backends started one at a time and new chats
missed the 90 s MCP readiness deadline. The gate now moves on what says its
starts are in trouble -- memory, gate init failures, slow inits -- and a
cut gate climbs back once those stay quiet.

## Modules

| Module | Role |
|---|---|
| `adaptive/signals.py` | `Sample` (one observation), `Thresholds`, `SpawnGateStats` (typed `stats.admission.spawn_gate`), and the pure `classify(sample, thresholds) -> PressureReport`. |
| `adaptive/policy.py` | `AdaptivePolicy` -- the deterministic AIMD state machine over two tracks (execution cap, spawn-gate capacity). `PolicyParams`, `Decision`, `params_from_config`. No clock, no I/O. |
| `adaptive/controller.py` | `AdaptiveController` -- the gateway-loop task: samples the host off-loop, feeds the policy, applies decisions through `SubagentManager.set_effective_cap` and `GatewayManager.set_spawn_capacity`, exposes `state()`, and the process registry `register/current/current_state` that `resource_status` reads. |

## Signals

Sampled every `agent.controller_sample_secs` (5 s) into a ring of 60. Every field
has a "not measured" value and an unmeasured field never fires.

| Signal | Source | Read by | Fires when |
|---|---|---|---|
| `loop_lag` | how late the controller's own timer fired on the gateway loop | **neither track** for a cap decision (`HOST_ONLY_SIGNALS` for exec, `GATE_IGNORED_SIGNALS` for the gate); reported on the decision and retires slow start | `>= DEFAULT_LAG_DECREASE_MS` (250) |
| `memory` | `resource_status._read_available_gb` (cgroup-clamped) | **spawn gate only** (`HOST_ONLY_SIGNALS`) | `<= resource_critical_gb` (2 GB); always **severe** |
| `fds` | `/proc/self/fd` or `/dev/fd` count vs `RLIMIT_NOFILE` soft | both tracks | `>= 80 %` of the limit |
| `procs` | daemon `stats.admission.host_budget` (`procs` / `max_procs`) | both tracks | `>= 90 %` of the budget |
| `start_latency` | `record_start(duration_ms, ...)` hook (session/new, backend initialize) | both tracks | p95 over the window `>= 30 s` |
| `timeouts` | attributable start timeouts + attributable run failures / finished in window | both tracks | rate `>= DEFAULT_TIMEOUT_RATE` (0.2) |
| `completion_rate` | successful runs / runs finished in window (needs >= 5 finished) | both tracks | `< 0.5` |
| `slow_keys` | distinct PoolKeys with a slow or failing start in the window | both tracks | `>= 2` |
| `gate_failures` | `SpawnGate` `failure` outcomes (daemon snapshot, or `note_gate_outcome` for an in-process gate) | both tracks; **sufficient alone for the gate** | `>= 2` in window |
| `gate_slow_inits` | `SpawnGate` snapshot `slow_inits`: successful inits in the daemon's last `SLOW_INIT_WINDOW_SECS` (60) that took `>= SLOW_INIT_FRACTION` (80 %) of their own `initialize_timeout_secs` | both tracks; **sufficient alone for the gate** | `>= gate_slow_inits` (2), so one slow but healthy server does not cut it |
| provider 429 | `record_provider_throttle(scope)` | **per provider** | reported on `Decision.throttled_providers`; **never** a host signal |

`PressureReport.exec_signals` is the set minus `HOST_ONLY_SIGNALS`, and
`exec_corroborated` is two or more of them in one sample: nothing is sufficient
alone for the execution cap. The spawn gate reads `gate_signals` (the set minus
`GATE_IGNORED_SIGNALS`, i.e. minus `loop_lag`): `gate_corroborated` is one of
`GATE_SUFFICIENT_ALONE` (`memory`, `gate_failures`, `gate_slow_inits`) or two
distinct gate signals, `gate_severe` is memory at the critical line, and
`gate_clear_for_increase` is no gate signal with memory at or above the pressure
line. `corroborated` (which `SUFFICIENT_ALONE` still feeds) only retires slow
start.

Attributable means a congestion failure: a start or turn that timed out, a
stall, a backend that never initialised (`controller.ATTRIBUTABLE_MARKERS`).
Permission denials, invalid parameters, context-length errors, deny-rule
refusals, turn limits and cancellations are `non_congestion` and never feed the
controller (`classify_run_outcome`). Completions are inferred each tick by
diffing `SubagentManager._agents` (`done` transitions), so the run loop needs no
hook; `record_completion` exists for work the manager does not track. Both
spawn-gate outcome counters are the DAEMON's LIFETIME totals, so the policy
diffs them: `failure` against the value one window old, `success` against the
value at the last cap change. A counter that went DOWN is a daemon respawn under
a live policy and both diffs rebase onto the fresh value -- otherwise two
lifetime failures read as permanent pressure (the experiment's D1) and a restart
makes the gate cap re-earn the vanished daemon's whole success total on top of
`DEFAULT_INCREASE_SUCCESSES` before its next `+1`. Silence is not a restart: a
failed `stats()` read arrives as the all-zero `SpawnGateStats` default, a shape a
live daemon never reports (it always carries its capacity), and rebasing the
success base onto it would let the same daemon's unchanged total buy a `+1` the
moment it answers again. The exec track's `completions` needs no rebase at all:
it is the controller's own in-process counter, built beside the policy in the
same constructor and only ever incremented, so it cannot pass its base from
below.

## Decision rules

Parameters are the `agent.adaptive_*` keys; numbers below are their defaults.

Each sample is judged once per track (`AdaptivePolicy._exec_step` /
`_gate_step`); the decision's `action` is the stronger of the two (pause > probe
> resume > decrease > increase > hold) and its `reason` names each track that
moved. Each track keeps its own decrease cooldown, clean-window clock and
one-increase-per-window clock.

| Rule | When | Effect |
|---|---|---|
| **Decrease (exec)** | **>= 2 distinct work signals** in one sample (`exec_corroborated`) and >= `DEFAULT_DECREASE_COOLDOWN_SECS` (30) since the last exec decrease | exec cap -> `clamp(max(ceil(cap x 0.5), healthy_in_flight), floor, cap - 1)`. Successes counted before the cut are discarded on the exec track only |
| **Decrease (gate)** | **`gate_corroborated`**: a signal from `GATE_SUFFICIENT_ALONE` (`memory`, `gate_failures`, `gate_slow_inits`) or >= 2 distinct gate signals; and >= 30 s since the last gate decrease | gate capacity -> `max(gate_floor, ceil(cap x 0.5))` at most `cap - 1`; its successes are discarded on the gate track only |
| **Hold** | a single soft signal for that track, or pressure inside its cooldown, or (gate) clear but inside the hysteresis band | nothing moves on that track |
| **Progress probe** (exec) | new stream activity since the previous sample, active work at the cap, queued ready work, no provider throttle, and the same clear 5 s / 30 s window as the current regime. Free memory is not a term: the spawn floor prices every start | at most `+1` exec slot without waiting for a whole task to finish; never doubles or relaxes the init-gate success bar |
| **Increase (slow start)** | `adaptive_slow_start` is on AND this process has never met corroborated pressure or a pause, AND the sample is clear for the track AND >= `DEFAULT_SLOW_START_CLEAN_SECS` (5) since that track's last pressure AND last increase AND (exec) >= `DEFAULT_SLOW_START_SUCCESSES` (1) since the last change AND demand at the cap. The eased success bar is the **exec track only** -- the gate still owes its flat `increase_successes` (below) | `x DEFAULT_SLOW_START_FACTOR` (2) on the track that qualified, bounded by its ceiling |
| **Increase (congestion avoidance)** | after the first corroborated pressure or pause: the sample is clear for the track -- exec: no work signal; gate: no gate signal AND `memory >= resource_pressure_gb` (4 GB) -- AND >= `DEFAULT_INCREASE_CLEAN_SECS` (30) since that track's last pressure AND last increase AND enough work since the last change (exec: `min(DEFAULT_INCREASE_SUCCESSES, cap)`, i.e. one wave of the CURRENT cap; gate: `DEFAULT_INCREASE_SUCCESSES` (20) backend inits) AND demand at the cap (exec: `running + queued >= cap`; gate: `queued > 0` or `in_flight >= capacity`) | `+1` on the track that qualified, bounded by its ceiling; at most one increase per window per track |
| **Idle recovery** (exec) | the exec cap is below the fresh-start value (the ceiling), the exec track has had NO demand (`running + queued == 0`) and no work signal for `DEFAULT_IDLE_RECOVERY_SECS` (60), AND >= one window since the last exec increase | `+1` toward the fresh-start value, never past it. Demand or any work signal restarts the idle clock; loop lag and memory do not |
| **Idle recovery** (gate) | the gate is below `spawn_concurrency_initial`, the sample is clear for the gate, and `DEFAULT_IDLE_RECOVERY_SECS` (60) have passed since its last gate signal AND its last cut, AND >= one window since its last increase. Demand is not required: a cut must not outlive its evidence because no start happens to be queued at the cap | `+1` toward `spawn_concurrency_initial`, never past it; growth beyond still earns on backend inits |
| **Pause** (gate) | memory below critical (`gate_severe`) for `severe_samples` (2) consecutive samples; loop lag never pauses it | gate at its floor; `paused`. The exec cap is untouched; running work untouched |
| **Probe** (gate) | paused and the severe condition cleared | `probing`; the gate stays at its floor |
| **Resume** (gate) | the probe completed (completions advanced) with no signal; or the host is idle and clear for `DEFAULT_IDLE_RECOVERY_SECS` with no probe result | gate to `floor + 1` (idle: left at its floor, to earn on inits); normal AIMD resumes. A probe that meets corroborated pressure re-pauses |
| **Fresh start** | process start | exec cap AT its ceiling (`user_max`); gate at `mcp_gateway.spawn_concurrency_initial`; the first clean window is measured from the first sample |
| **Ceiling raised** | `user_max` rises (a hot reload of `agent.max_subagents`) while the exec cap sat at the old ceiling | the exec cap follows to the new ceiling at once; a cap below the old ceiling (a cut) keeps earning its room |
| **Fixed** | `adaptive_concurrency_mode = "fixed"` | both caps pinned at their initial values on every tick -- the exec cap at its ceiling (Q2 reversal) |

`healthy_in_flight` (running minus stalled) bounds a decrease from below because
natural shrink cannot free what is currently working; that is what turns "10
concurrent starts, 4 time out" into 6, then 4, rather than 5, then 3. A decrease
always cuts by at least one so pressure with all work healthy still makes progress.

Bounds: exec track `user_max` / `adaptive_floor` (1) / `user_max` (start / floor
/ ceiling); gate track `spawn_concurrency_initial` (4) / `spawn_concurrency_min`
(1) / the derived gate ceiling, `max(spawn_concurrency_max, user_max)`
(`mcp_gateway.admission.derive_spawn_gate_ceiling`, 32 at defaults; below). The
user's ceiling is re-read from `SubagentManager.user_max_concurrent` on every
tick, so a hot reload of `agent.max_subagents` clamps the live cap immediately
and never writes the adaptive bound into the config.

`PolicyParams.exec_initial` is `None` in production (start at the ceiling);
`agent.adaptive_initial` is inert, kept on load and save so an existing config
is not rewritten. It is deliberately not marked `deprecated`: `to_dict()` writes
it into every save, so a deprecation notice would fire on every launch for a
value the operator cannot clear. A test that pins a rule from a lower cap
passes `exec_initial`.

### The spawn gate's ceiling follows the subagent ceiling

The gate bounds how many MCP backend processes fork and initialize at once,
daemon-wide. With its old ceiling of 8 under a subagent ceiling of 32, a fan-out
the subagent cap admitted in full had its backend initializations queued behind
eight windows, so the gate, not memory, bounded how many subagents got started.
The broker therefore launches the daemon with `--spawn-concurrency-max` raised
to the subagent ceiling at launch (`slack/gateway_runtime/mcp_broker.py`), and
the controller's gate track uses the same figure (`gateway_runtime/admission.py`).
Both read that ceiling from CONFIG alone (`subagent_count_ceiling`: the explicit
pin floored at 3, else `agent.subagent_auto_max`), never from the subagent
manager and never through a host-memory probe: on the boot path the broker
starts before `_init_subagents` builds the manager, so a manager-derived figure
launched the daemon at the plain 8 while the controller targeted 32, and the
daemon clamped every raise back; and the boot path before readiness takes no
memory reading. The gate needs only the upper bound the resolved cap can reach,
which that figure is. Pinned by
`test_the_daemon_is_launched_with_the_subagent_ceiling_at_boot` and
`test_the_boot_path_sizes_the_gate_without_probing_host_memory`.
The gate still STARTS at `spawn_concurrency_initial`, still halves on critical
memory and on failing or slow inits, and climbs only on clean backend inits with demand
(slow start doubles its step, never its bar), so a 20-wide fan-out takes it past
8 within a few windows. Like every other `mcp_gateway` admission key the
daemon's ceiling is fixed at its launch; a later `max_subagents` change reaches
it at the next broker start. The host budget is unchanged: at 40 GiB available
it derives a 273-process ceiling, far above 20 subagents with five private
servers each. Pinned by `test_subagent_memory_bounds_concurrency.py`
(`test_the_spawn_gate_ceiling_reaches_the_subagent_ceiling`,
`test_twenty_backend_initializations_run_at_once_under_the_derived_ceiling`,
`test_the_gate_track_climbs_past_eight_on_clean_init_evidence`,
`test_the_host_budget_admits_twenty_subagents_of_backends_at_40_gib`).

The degrade reason the gateway publishes to session health
(`_wire_overload_health`) keeps its closed set, but `adaptive_pause` and
`adaptive_probe` now come only from the spawn gate, since the exec cap is never
paused. Their Tasks Capacity card copy therefore leads with the lane's own
label, "Backend starts" ("Backend starts held to the minimum until this
computer frees up memory or load eases"), not runs. A paused gate still admits
its floor, so the copy says "held to the minimum" rather than "paused", which
would contradict the lane's own "Up to 1 at once". The pause and probe flags
sit on the `spawn_gate` cap source. The Degrade reason tip names the split:
Subagent runs are lowered only by work evidence, Backend starts also by low
memory or load.

### Growth ceiling: the user's pin, judged live

An exec increase climbs toward `user_max` -- the resolved `agent.max_subagents`
(an explicit pin, or `agent.subagent_auto_max` when it is 0) -- and nothing
else. No host prediction sits under that ceiling. The point of the loop is that
many sessions may ask for many workers at once, the controller admits them up to
the ceiling the user chose, the spawn floor admits each start only while memory
holds it, and the WORK signals in each 5 s sample -- attributable timeouts, slow
starts, fd and process exhaustion -- are what withhold the next increase or cut
the cap. Work that cannot be admitted yet queues on the manager and the spawn
gate; it is never refused for a guessed number.

An earlier reading clamped the climb to `min(user_max, Sample.host_cap)`, a
figure predicted from each agent's p90 peak memory AND peak CPU (the auto-sizing
arithmetic without its `subagent_auto_max` clamp). It was removed because it
inverted the loop: one build-heavy agent's one-minute burst (20 cores, 9 GB)
priced every slot at that burst, so a 32-core host with 96 GB free computed a
CPU term of 4 and held the cap at its fresh-start value for the life of the
process, while the controller it sat under saw nothing but clean samples. Memory
over-commit is the one unrecoverable failure and it is guarded live, once, where
the start is decided: the spawn gate defers every
start that would not leave `spawn_min_memory_gb` free after its own price and
the running agents' unobserved growth (`_startup_memory_reserve_gb`: a start that
has not settled -- the next one, a claim awaiting registration, a worker fewer
than two sweeps have measured -- owes the price it was admitted at, a dedicated
projection from the measured default or the learned settled RSS and never below
`agent.subagent_cost_gb`, or that less the process a shared start skips; never a
whole-run peak, in full until it settles; a settled worker owes nothing, since
its memory is already inside the free-memory reading; yielded parents included --
see `subagent.md`). The execution cap does not guard memory a second time: an
increase gated on the pressure line and a halving at the critical line were
count bounds on top of that floor, and they are what throttled one chat's
subagents behind another's. CPU over-commit only slows work, and slowness is
exactly the pressure the loop already backs off from. `compute_max_subagents`
is therefore not a memory figure at all: it is `agent.subagent_auto_max` as
written (3 when host memory cannot be read, because the floor then fails open,
and sized from host memory when `agent.spawn_min_memory_gb <= 0` turns the floor
off);
`agent.subagent_cpu_cost_cores` is deprecated and inert, preserved on load and
save so an existing config is not rewritten.

`probe_host` reads memory, RSS and fds only -- live signals -- and no longer
loads config or the learned-cost store on the worker thread.
`test_adaptive_policy.py::TestSlowStart::
test_no_static_host_prediction_sits_under_the_user_ceiling`,
`test_adaptive_controller.py::TestTick::
test_the_climb_is_bounded_by_the_user_ceiling_alone` and
`test_subagent_sizing.py::TestMemoryIsTheOnlyHostTerm` pin it.

This is what makes `max_subagents = 64` on a clear host start at 64, and on a
host short of memory run however many starts the floor admits, rather than a
number guessed before the work existed.

### Why the increase is not symmetric with the decrease

A decrease is multiplicative on ONE corroborated sample. With a flat `+1` per
`DEFAULT_INCREASE_CLEAN_SECS` (30 s) window gated on a flat
`DEFAULT_INCREASE_SUCCESSES` (20) completions, crossing a 64 ceiling from the
fresh-start `adaptive_initial` (4) is 60 windows: 60 x 30 s = 30 minutes of
perfectly clean samples and 60 x 20 = 1200 finished runs -- and at the floor,
`1 -> 2` costs 20 runs each executed ALONE, so a cap cut to the floor on a
transient lag spike stays there. The exec track now STARTS at its ceiling, so these rules matter only after a
cut, but they still close that gap without weakening any decrease:

- **slow start** doubles per 5 s window until this process has evidence the host
  refuses (its first corroborated pressure or pause);
- the exec bar scales to the cap (`min(increase_successes, cap)`), so a step is
  earned by one full wave of the cap being tested rather than by a constant that
  is cheap at 30 and prohibitive at 1. It is never above the old constant;
- fresh stream progress can earn one probe slot, so a long task need not
  finish before the next queued task can begin. Queued,
  parked, stalled and unstarted records do not supply progress evidence. An
  unchanged activity timestamp never buys another probe.

### Idle recovery: a cut the exec track earns back only under real load

Every increase rule above is earned on the exec track's own evidence -- demand
at the cap and completions. That evidence comes from two admission points on
the same effective cap: the `SubagentManager._agents` run table, and the runner
lane (workflow `ctx.agent()` calls, TaskRunner steps; the lane is bounded by the
effective cap, [taskq.md](taskq.md) § Runner adapters). A workflow agent holds a
lane slot and a sub-agent holds a manager slot, never both, so the two
populations are disjoint and nothing is counted twice. The lane folds its
occupancy into demand, its committed `done` settles (`RunnerLane.settled_ok`,
counted at the one settle choke point -- never a grant, fail, cancel, or a
claim-only container row that holds no slot) into completions, and a fresh
settle into progress; its session starts already feed `record_start`. So a
process whose load runs only on the lane can both CUT the cap and earn a step
back, under the same rules the manager's load obeys. The per-point at-cap tests
read the busier admission point, never the sum, so two manager plus two lane
runs at cap 4 earn nothing (neither point is saturated) while a point that is
genuinely at its cap still earns.

A cut is still evidence only about the load that was running when it fired, and
an idle process produces none of the evidence that retires it. Overlapping long
workflows that timed out while the lane sat otherwise quiet could leave the cap
low until fresh lane or manager load earned it back.

Idle recovery retires that stale evidence without trusting the host with more
than a restart would: after `DEFAULT_IDLE_RECOVERY_SECS` with no exec demand on
EITHER admission point and no work signal, the cap climbs `+1` per clean window
back to the fresh-start value -- the ceiling -- never past it. The next
corroborated work pressure cuts as before, and one work signal restarts the idle
clock; loop lag and memory do not, because the exec track does not read them.
Idle means neither point has demand -- live runner-lane load keeps the track out
of idle recovery, so under load a cut is earned back by the rules above.

### Accepted scope: one-way slow-start retirement

Slow start is one-way for the process lifetime after the first corroborated
pressure or pause. The controller then stays in congestion avoidance and earns
`+1` per clear window; only a process restart restores doubling. This is
acceptable because congestion avoidance still climbs, so the cap is not pinned.
The target defect is a cap that cannot climb at all, not slower growth after the
host has rejected doubling.

The gate track keeps the flat `increase_successes` and slow start does not ease
it: its counter is backend inits, which land far faster than runs finish. Easing
it would double the gate on a single init inside one clean window, and hand a
respawned daemon its capacity back for one success, against the restart-rebase
contract above. Slow start does double the gate's STEP once the bar is paid,
which is how a fan-out takes it from 4 past 8 toward the derived ceiling.

## Actuators

- **Execution cap.** `SubagentManager.set_effective_cap(cap | None)` sets
  `_adaptive_cap`; `_max_concurrent` -- the attribute every admission read site
  consults -- becomes `min(_user_max_concurrent, _adaptive_cap)`. `apply_limits`
  writes only `_user_max_concurrent` (the ceiling) and re-clamps. A raise pumps
  the queue through the staggered drain exactly as a config raise does, AND
  pumps the runner lane (TaskRunner steps, workflow `ctx.agent()` calls) through
  the manager's `set_cap_raise_listener` hook: that edge is the only wake for a
  runner waiter parked while the cap was `0`, because such a waiter holds no
  lane slot and nothing else will release one
  ([taskq.md](taskq.md) § Runner adapters). The two gates count their own
  occupancy under the shared ceiling; it does not bound their total. A cut
  admits nothing new and cancels nothing; `0` admits nothing (the controller no
  longer produces it: the exec track has no pause). `None` removes the bound.
  Applied synchronously at controller construction so the first spawn already
  sees the fresh-start cap -- the ceiling -- before the gateway's wiring pass,
  so that first application legitimately finds no lane hook and needs none (no
  waiters exist yet).
- **Spawn gate.** `GatewayManager.set_spawn_capacity(n)` sends
  `{"type": "set-spawn-capacity", "capacity": n}` on a one-shot control
  connection; gatewayd answers `{"type": "spawn-capacity", "capacity": <clamped>,
  ...gate snapshot}` after `admission.gate.set_capacity(n)`, or
  `spawn-capacity-rejected`. `None` (daemon down, rejected) leaves the value
  **pending** on the controller and it is retried on the next tick -- the
  controller never assumes a capacity the daemon did not confirm.

## Configuration (`agent.*`, all live -- no restart)

| Key | Default | Meaning |
|---|---|---|
| `adaptive_concurrency` | `true` | run the controller; `false` = user cap only, gate back to its initial |
| `adaptive_concurrency_mode` | `"aimd"` | `"fixed"` pins both caps (plain semaphore; the exec cap at its ceiling) |
| `adaptive_floor` | `1` | lowest exec cap under sustained work pressure (clamped 1..64) |
| `adaptive_initial` | `4` | **inert** (not flagged deprecated, see above): the exec cap starts at its ceiling. Kept on load and save |
| `adaptive_slow_start` | `true` | before the first corroborated pressure, double a cap below its ceiling per clear 5 s window instead of `+1` per 30 s; `false` = `+1` per clear 30 s window from the start, with the exec success bar still scaled to the live cap (`min(DEFAULT_INCREASE_SUCCESSES, cap)`) |
| `controller_sample_secs` | `5` | sampling interval (1..300) |

`adaptive_concurrency = false` costs the launch nothing, not even an import.
`slack.gateway` names `AdaptiveController` under `TYPE_CHECKING` only and imports
`adaptive.{controller,policy,signals}` inside `_start_adaptive_controller`'s
ENABLED branch — the `adaptive` package and roughly 15 ms of first-load work
(about 8 ms with bytecode cached) that the
gateway boot path would otherwise pay before the dashboard socket binds, on every
launch, for a subsystem the switch turned off (`AUTOSDE.yaml`'s
`no-new-work-on-gateway-boot-path`, clause 5). Flipping the switch at runtime
loses nothing: the disabled branch registers a `live.watch_object`
(`GatewayAdaptiveStart`) whose callback re-enters that same branch and takes the
import then. Shutdown's `adaptive_controller.register(None)` imports locally too
and is reached only past a live controller, so it is a `sys.modules` lookup.
Pinned by
`test_slack_gateway_overload_wiring.py::test_importing_the_gateway_loads_no_adaptive_module`,
in a subprocess because this suite's `sys.modules` already holds whatever else
imported it.

Memory thresholds reuse `resource_pressure_gb` / `resource_critical_gb`; they
shape the spawn gate only. The controller subscribes to all of these (bar the
inert `adaptive_initial`) through `live.watch_object` and
re-parameterises the policy in place (`update_params`): a lowered ceiling clamps,
a raised one does not lift the live cap (it is earned), and a mode switch to
`fixed` snaps on the next tick.

## Fixed tuning (`adaptive/policy.py`)

These are module constants, not settings. Tests and experiments may pass
`PolicyParams` and `Thresholds` directly without adding config keys.

| Constant | Value |
|---|---|
| `DEFAULT_DECREASE_FACTOR` | 0.5 |
| `DEFAULT_DECREASE_COOLDOWN_SECS` | 30 seconds |
| `DEFAULT_INCREASE_CLEAN_SECS` | 30 seconds |
| `DEFAULT_INCREASE_SUCCESSES` | 20 (exec: capped at the live cap) |
| `DEFAULT_SLOW_START` | `True` |
| `DEFAULT_SLOW_START_CLEAN_SECS` | 5 seconds |
| `DEFAULT_SLOW_START_SUCCESSES` | 1 |
| `DEFAULT_SLOW_START_FACTOR` | 2 |
| `DEFAULT_LAG_DECREASE_MS` | 250 ms |
| `DEFAULT_LAG_INCREASE_MS` | 100 ms |
| `DEFAULT_LAG_SEVERE_MS` | 2000 ms |
| `DEFAULT_TIMEOUT_RATE` | 0.2 |
| `DEFAULT_IDLE_RECOVERY_SECS` | 60 seconds |

## Visibility

`AdaptiveController.state()` carries the enabled flag, mode, effective exec cap
vs ceiling, the growth regime (`slow_start`), gate
capacity vs ceiling, the gate's paused/probing flags, decision counts, the applied and pending
actuator values, the last error, the last sample, and `recent_decisions`: the
last 32 cap-changing decisions, newest last, each with its wall-clock time,
action, reason, the caps the actuators confirmed after it (a gate update the
daemon did not answer shows the previous gate cap), and the loop lag of the
sample that produced it. `last_cut` is the decision that last LOWERED the exec
cap (action, reason, signals, the sample time `t`, and in `state()` its
`age_secs`); it is never cleared by an increase, so the answer to "why is the
cap low" survives both the holds that follow and the decision ring rolling
over. A clear hold's `reason` says what the next exec step waits for: the
ceiling, the idle clock ("idle; restoring toward 32 in 25s"), demand at the cap,
or the completions still owed; when only host-only evidence (loop lag, memory)
fired, the gate's hold is named instead. Cap decreases, pauses and resumes are logged at
WARNING (growth at INFO) so `gateway.log` keeps them.
`resource_status.adaptive_state()` reads it from the registry and
`adaptive_summary_lines()` renders it at the end of the `resource_status` MCP
tool's report ("Execution cap: 64/64   MCP spawn gate: 4/32 (active)", the
gate's state in brackets -- `paused` or `probing` under severe host pressure --
then "Growth toward ceiling: slow start (x2/window)", the last decision and
its signals, throttled provider scopes, and the last five of
`recent_decisions` under "Recent cap changes"). While the exec cap is below the
ceiling and a cut is on record it adds "Last pressure cut: decrease 42s ago
(corroborated pressure: slow_keys,timeouts); signals: ..."). Only an exec cut
is on record: a gate cut or pause lowers no exec cap.

**A tool server is not the gateway process,** so that registry is empty there:
`mcp_tools/spawn.py::_live_adaptive_state` falls back to `GET
/api/spawn/adaptive` (`dashboard/handlers/spawn_resume.py`), which returns
`{"adaptive": adaptive_state() or {}}`. Its own route under the `/api/spawn`
prefix, which is on `_MIXED_INTERNAL_API_PATHS`, so a loopback
`X-Internal-Secret` caller reaches it with no allowlist change.
`GET /api/tasks/summary` carries the same object and is deliberately NOT used
here: it is in neither internal bucket (and also lists per-task session keys and
lease owners), so reading it would 403 -- silently, since the fallback swallows
failures -- and leave the tool printing the ceiling it exists to replace. Every
failure on this path is logged at debug for that reason. Only when neither path
answers does the tool print `agent.max_subagents`, and then it is labelled a
ceiling rather than the cap in force -- the number a caller sizes a fan-out from
has to be the effective one.

The two other places the model is handed a fan-out figure -- the spawn tool
descriptions (`mcp_tools/spawn.py::schemas`, "You can run up to N sub-agents
concurrently", followed in both branches by the note that each start also waits
until host memory can hold it) and the `{{MAX_SUBAGENTS}}` prompt token
(`context.py::_resolve_prompt_templates`) -- read the in-process registry only,
through `resource_status.adaptive_exec_cap()`, and never the loopback API: the
prompt token is resolved once per session -- a session start takes the reading
and the restore of the contract after compaction reuses it, unless the memo has
since evicted that session, in which case the restore takes a live reading and
the two renders can differ -- and `schemas()`
runs on the gateway's own discovery cycle as well as in a tool server. In the
gateway that read is the live cap (a disabled controller reports the user's max,
which is then the cap in force; a cap of 0, which the controller no longer
produces, reads as unknown). Where it is empty the configured ceiling is printed
and labelled as one -- "N (configured ceiling)" in the prompt, "Your configured
sub-agent ceiling is N; the cap actually in force may be lower" in the tool
description. `resolve_max_subagents` never answers 0, so the figure is always a
number (`subagent_auto_max`, 32, at defaults on any host; 3 where host memory
cannot be read); "several" is left only for a config that cannot be loaded.
Pinned by `test_subagent_memory_bounds_concurrency.py::
test_the_prompt_figure_is_the_auto_ceiling_on_any_host` and
`test_the_spawn_tool_names_the_figure_and_the_memory_wait`.

Metrics: `kirocrew.adaptive.decisions{action}` (`metrics/events.py::ADAPTIVE_DECISIONS`)
increments once per decision that changed a cap or the paused flag; `action` is
the closed policy enum. Holds and fixed-mode ticks are not counted.

## Dependency signals (area L seam)

Per-provider throttling stays in that provider's dependency channel: the policy
reports `throttled_providers` and the controller calls its
`on_provider_throttle(scope, count)` listener on every `record_provider_throttle`.
The `DependencyCoordinator` of [`taskq.md`](taskq.md) is the intended subscriber;
the controller itself never lowers a host cap for a 429.

## Tests

`test_adaptive_startup_memory.py` runs the real manager, durable pump, admission
guard and controller with virtual time, 0.25-second starts and five-second RSS
growth, under a cap of 64 the controller never moves. The floor's reserve, not
a count, stops the wave; a post-sample host-memory shock stops the drain at
once (nothing starts after it), and a shock inside the headroom the floor kept
leaves the floor intact -- a larger one is another application's memory, which
admission does not shed. Small cases cover unregistered claims, yielded parents,
shared sessions and observed RSS replacing reservations.

`test_subagent_memory_bounds_concurrency.py` is the acceptance map for memory,
not a count, bounding concurrency: 20 auto-sized spawns on a host simulated at
40 GiB all start, paced only by the stagger, with the real controller attached;
3 s of loop lag leaves both caps unchanged, and memory at the critical line
leaves the exec cap unchanged (the gate backs off); an explicit `max_subagents=3` still caps and the
child reserve still hands a yielded slot to the waiting parent's child; the gate
ceiling and host budget carry the fan-out; timeouts on two servers and fd plus
process exhaustion still cut the exec cap, a 429 still never does; the prompt
and spawn-tool figures are defined; the TaskRunner auto value is never 0 and a
non-positive workflow concurrency is bounded.

`test_adaptive_policy.py` (pure, injected timestamps): fresh start at the
ceiling (or a pinned `exec_initial`); the 10 -> 6 -> 4 descent produced by
`observe` under injected concurrency timeouts, the plain x0.5 descent to the
floor, minimum progress and the healthy-work bound; a single soft signal never
cuts, one slow server is not the host, memory and the gate's own failing or slow
inits cut the spawn gate alone and never the exec cap, loop lag (even severe)
moves neither cap and does not restart the gate's recovery clock, one slow init
is no evidence, a cut gate climbs back to its fresh
start with no demand, and nothing is sufficient alone for the exec cap; a
single provider's 429s never move either
cap but are reported; cooldown blocks a second cut and discards pre-cut
successes on the cut track only -- a gate-only cut with exec at its floor leaves
the exec completions counted, so the 20th completion afterwards still buys
exec its `+1`, and a pause / resume likewise resets only the tracks it moved;
a noisy lag series around the threshold never oscillates the gate, the gate's
increase side needs the hysteresis band and the exec track has none; +1 per
clean window with demand and successes, no demand no increase, the gate earns
on inits; the gate's pause -> probe -> resume with the exec cap untouched, a
probe meeting pressure re-pauses, one severe sample is not a pause;
the ceiling is never exceeded, a lowered ceiling clamps, `fixed` disables
adaptation; `TestIdleRecovery`: a cut cap climbs back one step per window to the
fresh-start value and stops there, an unreported gate probe resumes once idle
and clear, demand or a lone soft work signal restarts the idle clock, work below the cap
is not idle, a gate step on the same sample does not suppress the idle exec step due on it, and every increase names each track it moved (a gate step as its `spawn gate target X -> Y`, never under an exec note; `TestSlowStart` pins the doubled-gate variant), a cap already at the fresh-start value never drifts, and
`last_cut` names the exec cut behind a low cap, never a gate pause; `Decision.changed` and the snapshot shape. Those cases construct
their params with slow start OFF, because each one pins a congestion-avoidance
rule; `TestSlowStart` owns the other regime: doubling per window up to the
user ceiling with no static host prediction under it, free memory neither
braking nor cutting the exec track, one corroborated pressure ending
slow start for the life of the process (including pressure the cooldown only
holds, and a config edit that turns the key back on), a pause ending it too, the
exec bar scaling to the cap instead of a flat 20, demand and a clear sample still
being required, and the snapshot carrying the regime. `params_from_config` is
where the shipped default (slow start ON) is pinned. A daemon restart is
pinned on both gate counters: fresh failures after the drop still fire the
signal, and the gate cap owes only `increase_successes` fresh inits for its next
`+1` (11 samples at 2 inits per sample, not the 21 a stale base costs), rebased
onto whatever the fresh counter reads rather than onto zero -- while a sample
carrying no gate snapshot at all leaves the base alone, so a failed stats read
buys the cap nothing.

`test_adaptive_controller.py` (fakes for the manager and daemon, injected clock
and host probe): the fresh-start cap -- the ceiling, whatever `adaptive_initial`
says -- is applied synchronously; a decrease reaches `set_effective_cap` and
`set_capacity`; low memory moves the gate and never the exec cap;
the shaped descent is driven end to end by the controller; a gate value stays
pending until the daemon answers; a pause holds the gate at its floor and
leaves the exec cap; the ceiling is re-read every tick and a raise is followed; disabling
removes the bound and re-enabling restores the earned position; fixed mode pins
both caps; one full `tick` reads host, gate and manager runs without
double-counting; the hooks feed the sample; the area-L listener fires; the run
loop measures lag and survives a failing probe; the outcome classifier's
buckets; the real `SubagentManager` seam (min of user and adaptive, `apply_limits`
moves only the ceiling, a raise pumps the queue, `reconfigure` never shrinks the
ceiling to the bound); the gatewayd frame clamps and rejects; the manager
actuator's round trip; `resource_status` rendering and the registry; config
defaults, parse clamps and that every live path is a schema key; an idle
controller restores the cap after a cut on failing starts and `resource_status`
names the cut ("Last pressure cut") until the cap is back at the ceiling. The climb is
pinned to the user's ceiling alone: the sizing helpers are not consulted on the
way up and `probe_host` reads only live signals.
`test_mcp_core_more_coverage.py::TestResourceStatusTool` /
`TestLiveAdaptiveState` pin the tool's side: the live cap is reported when the
gateway answers, the in-process registry wins without a request, an unreachable
gateway labels the number a ceiling, and a gateway-owned block is not rendered
twice.
`test_subagent_config_hot_reload.py::TestCapChangeVersusAdaptiveClamp` and
`test_subagent_sizing.py::test_manager_effective_cap_sits_under_the_resolved_ceiling`
pin the seam from the manager's side; `test/metrics/test_business_counters.py`
pins the counter's owner and bounded attributes.

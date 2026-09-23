# Adaptive concurrency (`kiro_crew.adaptive`)

The controller that turns the user's concurrency configuration into a runtime
cap the host can actually sustain. It implements RFC
[overload-resilience §5](../../request-for-change/rfc-overload-resilience.md#5-adaptive-controller):
the user's `agent.max_subagents` (or its auto-sized value) is the **ceiling and
is never written**; beneath it a live **effective cap** halves on corroborated
host pressure, earns its way back one step per clean window, pauses dispatch
under severe pressure and re-opens with a single probe. The same verdict shapes
the MCP gateway daemon's `SpawnGate` capacity. Nothing is ever killed to fit a
smaller cap: both actuators shrink naturally as in-flight work finishes.

## Modules

| Module | Role |
|---|---|
| `adaptive/signals.py` | `Sample` (one observation), `Thresholds`, `SpawnGateStats` (typed `stats.admission.spawn_gate`), and the pure `classify(sample, thresholds) -> PressureReport`. |
| `adaptive/policy.py` | `AdaptivePolicy` -- the deterministic AIMD state machine over two tracks (execution cap, spawn-gate capacity). `PolicyParams`, `Decision`, `params_from_config`. No clock, no I/O. |
| `adaptive/controller.py` | `AdaptiveController` -- the gateway-loop task: samples the host off-loop, feeds the policy, applies decisions through `SubagentManager.set_effective_cap` and `GatewayManager.set_spawn_capacity`, exposes `state()`, and the process registry `register/current/current_state` that `resource_status` reads. |

## Signals

Sampled every `agent.controller_sample_secs` (5 s) into a ring of 60. Every field
has a "not measured" value and an unmeasured field never fires.

| Signal | Source | Scope | Fires when |
|---|---|---|---|
| `loop_lag` | how late the controller's own timer fired on the gateway loop | host | `>= DEFAULT_LAG_DECREASE_MS` (250); **severe** `>= DEFAULT_LAG_SEVERE_MS` (2000) |
| `memory` | `resource_status._read_available_gb` (cgroup-clamped) | host | `<= resource_critical_gb` (2 GB); always **severe** |
| `fds` | `/proc/self/fd` or `/dev/fd` count vs `RLIMIT_NOFILE` soft | host | `>= 80 %` of the limit |
| `procs` | daemon `stats.admission.host_budget` (`procs` / `max_procs`) | host | `>= 90 %` of the budget |
| `start_latency` | `record_start(duration_ms, ...)` hook (session/new, backend initialize) | host | p95 over the window `>= 30 s` |
| `timeouts` | attributable start timeouts + attributable run failures / finished in window | host | rate `>= DEFAULT_TIMEOUT_RATE` (0.2) |
| `completion_rate` | successful runs / runs finished in window (needs >= 5 finished) | host | `< 0.5` |
| `slow_keys` | distinct PoolKeys with a slow or failing start in the window | host | `>= 2` |
| `gate_failures` | `SpawnGate` `failure` outcomes (daemon snapshot, or `note_gate_outcome` for an in-process gate) | host | `>= 2` in window |
| provider 429 | `record_provider_throttle(scope)` | **per provider** | reported on `Decision.throttled_providers`; **never** a host signal |

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

| Rule | When | Effect |
|---|---|---|
| **Decrease** | pressure is **corroborated**: a signal from `SUFFICIENT_ALONE` (`loop_lag`, `memory`) or **>= 2 distinct** signals in one sample; and >= `DEFAULT_DECREASE_COOLDOWN_SECS` (30) since the last decrease | exec cap -> `clamp(max(ceil(cap x 0.5), healthy_in_flight), floor, cap - 1)`; gate capacity -> `max(gate_floor, ceil(cap x 0.5))` at most `cap - 1`. Successes counted before the cut are discarded on the track whose cap moved; a track already at its floor keeps its earned successes. |
| **Hold** | a single soft signal, or pressure inside the cooldown, or clear but inside the hysteresis band | nothing moves |
| **Progress probe** | new stream activity since the previous sample, active work at the cap, queued ready work, measured memory above the pressure line, no provider throttle, and the same clear 5 s / 30 s window as the current regime | at most `+1` exec slot without waiting for a whole task to finish; never doubles or relaxes the init-gate success bar |
| **Increase (slow start)** | `adaptive_slow_start` is on AND this process has never met corroborated pressure or a pause, AND the sample is clear (as below) AND >= `DEFAULT_SLOW_START_CLEAN_SECS` (5) since the last pressure AND since the last increase AND >= `DEFAULT_SLOW_START_SUCCESSES` (1) since the last change AND demand at the cap. The eased success bar is the **exec track only** -- the gate still owes its flat `increase_successes` (below) | `x DEFAULT_SLOW_START_FACTOR` (2) on the track that qualified, bounded by its growth ceiling (see below) |
| **Increase (congestion avoidance)** | after the first corroborated pressure or pause: the sample is clear -- no signal at all AND `loop_lag < DEFAULT_LAG_INCREASE_MS` (100) AND `memory >= resource_pressure_gb` (4 GB) -- AND >= `DEFAULT_INCREASE_CLEAN_SECS` (30) since the last pressure AND since the last increase AND enough work since the last change (exec: `min(DEFAULT_INCREASE_SUCCESSES, cap)`, i.e. one wave of the CURRENT cap; gate: `DEFAULT_INCREASE_SUCCESSES` (20) backend inits) AND demand at the cap (exec: `running + queued >= cap`; gate: `queued > 0` or `in_flight >= capacity`) | `+1` on the track that qualified, bounded by its growth ceiling; at most one increase per window |
| **Pause** | severe pressure for `severe_samples` (2) consecutive samples | exec cap `0` (no new grants), gate at its floor; running work untouched |
| **Probe** | paused and the severe condition cleared | exec cap `1`; the gate stays at floor |
| **Resume** | the probe completed (completions advanced) with no signal | caps to `floor + 1`; normal AIMD resumes. A probe that meets corroborated pressure re-pauses |
| **Fresh start** | process start | exec cap `min(user_max, adaptive_initial=4)`, gate at `mcp_gateway.spawn_concurrency_initial`; the first clean window is measured from the first sample |
| **Fixed** | `adaptive_concurrency_mode = "fixed"` | both caps pinned at their initial values on every tick (Q2 reversal) |

`healthy_in_flight` (running minus stalled) bounds a decrease from below because
natural shrink cannot free what is currently working; that is what turns "10
concurrent starts, 4 time out" into 6, then 4, rather than 5, then 3. A decrease
always cuts by at least one so pressure with all work healthy still makes progress.

Bounds: exec track `min(user_max, 4)` / `adaptive_floor` (1) / `user_max`; gate
track `spawn_concurrency_initial` (4) / `spawn_concurrency_min` (1) /
`spawn_concurrency_max` (8). The user's ceiling is re-read from
`SubagentManager.user_max_concurrent` on every tick, so a hot reload of
`agent.max_subagents` clamps the live cap immediately and never writes the
adaptive bound into the config.

### Growth ceiling: the user's pin, judged live

An exec increase climbs toward `user_max` -- the resolved `agent.max_subagents`
(an explicit pin, or `compute_max_subagents`'s memory-sized value when it is 0)
-- and nothing else. No static host prediction sits under that ceiling. The
point of the loop is that many sessions may ask for many workers at once, the
controller admits them up to the ceiling the user chose, and the LIVE pressure
signals in each 5 s sample -- free memory against the pressure line, loop lag,
attributable timeouts, slow starts -- are what withhold the next increase or
cut the cap. Work that cannot be admitted yet queues on the manager and the
spawn gate; it is never refused for a guessed number.

An earlier reading clamped the climb to `min(user_max, Sample.host_cap)`, a
figure predicted from each agent's p90 peak memory AND peak CPU (the auto-sizing
arithmetic without its `subagent_auto_max` clamp). It was removed because it
inverted the loop: one build-heavy agent's one-minute burst (20 cores, 9 GB)
priced every slot at that burst, so a 32-core host with 96 GB free computed a
CPU term of 4 and held the cap at its fresh-start value for the life of the
process, while the controller it sat under saw nothing but clean samples. Memory
over-commit is the one unrecoverable failure and it is guarded live, three
times: an increase needs a measured `free_mem_mb` at or above the pressure line
(an unreadable reading, `-1`, fails open, as it does everywhere else the
sample is unmeasurable -- `classify` treats it as clear), corroborated
pressure at the critical line halves the cap, and the spawn gate defers every
cold start that would not leave `spawn_min_memory_gb` plus the running dedicated
agents' unobserved growth free (`_startup_memory_reserve_gb`: the greater of
`subagent_cost_gb` and live dedicated peak RSS, each observed byte replacing a
reserved one, claims awaiting registration and yielded parents included). CPU
over-commit only slows work, and slowness is exactly the pressure the loop
already backs off from. `compute_max_subagents` therefore sizes the AUTO ceiling
from memory alone as well; `agent.subagent_cpu_cost_cores` is deprecated and
inert, preserved on load and save so an existing config is not rewritten.

`probe_host` reads memory, RSS and fds only -- live signals -- and no longer
loads config or the learned-cost store on the worker thread.
`test_adaptive_policy.py::TestSlowStart::
test_no_static_host_prediction_sits_under_the_user_ceiling`,
`test_adaptive_controller.py::TestTick::
test_the_climb_is_bounded_by_the_user_ceiling_alone` and
`test_subagent_sizing.py::TestMemoryIsTheOnlyHostTerm` pin it.

This is what makes `max_subagents = 64` on a clear host with demand reach 64,
and on a host under memory pressure settle wherever the pressure line says,
rather than at a number guessed before the work existed.

### Why the increase is not symmetric with the decrease

A decrease is multiplicative on ONE corroborated sample. With a flat `+1` per
`DEFAULT_INCREASE_CLEAN_SECS` (30 s) window gated on a flat
`DEFAULT_INCREASE_SUCCESSES` (20) completions, crossing a 64 ceiling from the
fresh-start `adaptive_initial` (4) is 60 windows: 60 x 30 s = 30 minutes of
perfectly clean samples and 60 x 20 = 1200 finished runs -- and at the floor,
`1 -> 2` costs 20 runs each executed ALONE, so a cap cut to the floor on a
transient lag spike stays there. Three rules close that gap without weakening any
decrease:

- **slow start** doubles per 5 s window until this process has evidence the host
  refuses (its first corroborated pressure or pause);
- the exec bar scales to the cap (`min(increase_successes, cap)`), so a step is
  earned by one full wave of the cap being tested rather than by a constant that
  is cheap at 30 and prohibitive at 1. It is never above the old constant;
- fresh stream progress can earn one probe slot within measured headroom, so
  a long task need not finish before the next queued task can begin. Queued,
  parked, stalled and unstarted records do not supply progress evidence. An
  unchanged activity timestamp never buys another probe.

### Accepted scope: one-way slow-start retirement

Slow start is one-way for the process lifetime after the first corroborated
pressure or pause. The controller then stays in congestion avoidance and earns
`+1` per clear window; only a process restart restores doubling. This is
acceptable because congestion avoidance still climbs, so the cap is not pinned.
The target defect is a cap that cannot climb at all, not slower growth after the
host has rejected doubling.

The gate track keeps the flat `increase_successes` and slow start does not ease
it: its counter is backend inits, which land far faster than runs finish, and its
whole range is `4 -> 8` -- one doubling, not the tens of steps slow start exists
to cross. Easing it would take the gate to its ceiling on a single init inside
one clean window, and hand a respawned daemon its full capacity back for one
success, against the restart-rebase contract above.

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
  admits nothing new and cancels nothing; `0` pauses grants. `None` removes the
  bound. Applied synchronously at controller construction so the first spawn
  already sees the fresh-start cap -- before the gateway's wiring pass, so that
  first application legitimately finds no lane hook and needs none (no waiters
  exist yet).
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
| `adaptive_concurrency_mode` | `"aimd"` | `"fixed"` pins both caps (plain semaphore) |
| `adaptive_floor` | `1` | lowest exec cap under sustained pressure (clamped 1..64) |
| `adaptive_initial` | `4` | fresh-start exec cap, bounded by `max_subagents` (1..64) |
| `adaptive_slow_start` | `true` | before the first corroborated pressure, double the exec cap per clear 5 s window instead of `+1` per 30 s; `false` = `+1` per clear 30 s window from the start, with the exec success bar still scaled to the live cap (`min(DEFAULT_INCREASE_SUCCESSES, cap)`) |
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

Memory thresholds reuse `resource_pressure_gb` / `resource_critical_gb`. The
controller subscribes to all of these through `live.watch_object` and
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

## Visibility

`AdaptiveController.state()` carries the enabled flag, mode, effective exec cap
vs ceiling, the growth regime (`slow_start`), gate
capacity vs ceiling, paused/probing, decision counts, the applied and pending
actuator values, the last error and the last sample.
`resource_status.adaptive_state()` reads it from the registry and
`adaptive_summary_lines()` renders it at the end of the `resource_status` MCP
tool's report ("Execution cap: 8/64   MCP spawn gate: 4/8   Dispatch: active",
then "Growth toward ceiling: slow start (x2/window)", the last decision and
its signals, throttled provider scopes).

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
concurrently") and the `{{MAX_SUBAGENTS}}` prompt token
(`context.py::_resolve_prompt_templates`) -- read the in-process registry only,
through `resource_status.adaptive_exec_cap()`, and never the loopback API: the
token is resolved on every session assembly, and `schemas()` runs on the
gateway's own discovery cycle as well as in a tool server. In the gateway that
read is the live cap (a disabled controller reports the user's max, which is
then the cap in force; a paused dispatch reads as unknown). Where it is empty
the configured ceiling is printed and labelled as one -- "N (configured
ceiling)" in the prompt, "Your configured sub-agent ceiling is N; the cap
actually in force may be lower" in the tool description.

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
growth. A post-sample host-memory shock must leave the configured memory floor
intact; the ample-host case still fills the earned execution slots. Small cases
cover unregistered claims, yielded parents, shared sessions and observed RSS
replacing reservations.

`test_adaptive_policy.py` (pure, injected timestamps): fresh start at
`min(user_max, 4)`; the 10 -> 6 -> 4 descent produced by `observe` under injected
concurrency timeouts, the plain x0.5 descent to the floor, minimum progress and
the healthy-work bound; a single soft signal never cuts, one slow server is not
the host, lag and memory cut alone; a single provider's 429s never move either
cap but are reported; cooldown blocks a second cut and discards pre-cut
successes on the cut track only -- a gate-only cut with exec at its floor leaves
the exec completions counted, so the 20th completion afterwards still buys
exec its `+1`, and a pause / resume likewise resets only the tracks it moved;
a noisy lag series around the threshold never oscillates and the
increase side needs the hysteresis band; +1 per clean window with demand and
successes, no demand no increase, the gate earns on inits; pause -> probe ->
resume, a probe meeting pressure re-pauses, one severe sample is not a pause;
the ceiling is never exceeded, a lowered ceiling clamps, `fixed` disables
adaptation; `Decision.changed` and the snapshot shape. Those cases construct
their params with slow start OFF, because each one pins a congestion-avoidance
rule; `TestSlowStart` owns the other regime: doubling per window up to the
user ceiling with no static host prediction under it, free memory under the
pressure line braking growth without cutting, one corroborated pressure ending
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
and host probe): the fresh-start cap is applied synchronously; a decrease reaches
`set_effective_cap` and `set_capacity`; the shaped descent is driven end to end
by the controller; a gate value stays pending until the daemon answers; pause
sets 0 grants and the gate floor; the ceiling is re-read every tick; disabling
removes the bound and re-enabling restores the earned position; fixed mode pins
both caps; one full `tick` reads host, gate and manager runs without
double-counting; the hooks feed the sample; the area-L listener fires; the run
loop measures lag and survives a failing probe; the outcome classifier's
buckets; the real `SubagentManager` seam (min of user and adaptive, `apply_limits`
moves only the ceiling, a raise pumps the queue, `reconfigure` never shrinks the
ceiling to the bound); the gatewayd frame clamps and rejects; the manager
actuator's round trip; `resource_status` rendering and the registry; config
defaults, parse clamps and that every live path is a schema key. The climb is
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

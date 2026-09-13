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
| **Decrease** | pressure is **corroborated**: a signal from `SUFFICIENT_ALONE` (`loop_lag`, `memory`) or **>= 2 distinct** signals in one sample; and >= `DEFAULT_DECREASE_COOLDOWN_SECS` (30) since the last decrease | exec cap -> `clamp(max(ceil(cap x 0.5), healthy_in_flight), floor, cap - 1)`; gate capacity -> `max(gate_floor, ceil(cap x 0.5))` at most `cap - 1`. Successes counted before the cut are discarded. |
| **Hold** | a single soft signal, or pressure inside the cooldown, or clear but inside the hysteresis band | nothing moves |
| **Increase** | no signal at all AND `loop_lag < DEFAULT_LAG_INCREASE_MS` (100) AND `memory >= resource_pressure_gb` (4 GB) AND >= `DEFAULT_INCREASE_CLEAN_SECS` (30) since the last pressure AND since the last increase AND >= `DEFAULT_INCREASE_SUCCESSES` (20) since the last change AND demand at the cap (exec: `running + queued >= cap`; gate: `queued > 0` or `in_flight >= capacity`) | `+1` on the track that qualified, never above its ceiling; at most one increase per window |
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
| `DEFAULT_INCREASE_SUCCESSES` | 20 |
| `DEFAULT_LAG_DECREASE_MS` | 250 ms |
| `DEFAULT_LAG_INCREASE_MS` | 100 ms |
| `DEFAULT_LAG_SEVERE_MS` | 2000 ms |
| `DEFAULT_TIMEOUT_RATE` | 0.2 |

## Visibility

`AdaptiveController.state()` carries the enabled flag, mode, effective exec cap
vs ceiling, gate capacity vs ceiling, paused/probing, decision counts, the
applied and pending actuator values, the last error and the last sample.
`resource_status.adaptive_state()` reads it from the registry and
`adaptive_summary_lines()` renders it at the end of the `resource_status` MCP
tool's report ("Execution cap: 4/16   MCP spawn gate: 4/8   Dispatch: active",
the last decision and its signals, throttled provider scopes). When no controller
runs in the process (CLI, tests) the section is absent.

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

`test_adaptive_policy.py` (pure, injected timestamps): fresh start at
`min(user_max, 4)`; the 10 -> 6 -> 4 descent produced by `observe` under injected
concurrency timeouts, the plain x0.5 descent to the floor, minimum progress and
the healthy-work bound; a single soft signal never cuts, one slow server is not
the host, lag and memory cut alone; a single provider's 429s never move either
cap but are reported; cooldown blocks a second cut and discards pre-cut
successes; a noisy lag series around the threshold never oscillates and the
increase side needs the hysteresis band; +1 per clean window with demand and
successes, no demand no increase, the gate earns on inits; pause -> probe ->
resume, a probe meeting pressure re-pauses, one severe sample is not a pause;
the ceiling is never exceeded, a lowered ceiling clamps, `fixed` disables
adaptation; `Decision.changed` and the snapshot shape. A daemon restart is
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
defaults, parse clamps and that every live path is a schema key.
`test_subagent_config_hot_reload.py::TestCapChangeVersusAdaptiveClamp` and
`test_subagent_sizing.py::test_manager_effective_cap_sits_under_the_resolved_ceiling`
pin the seam from the manager's side; `test/metrics/test_business_counters.py`
pins the counter's owner and bounded attributes.

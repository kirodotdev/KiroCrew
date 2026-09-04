# Babysit PR watch

## Purpose

The babysit feature offers two current monitoring modes.

* `monitor_start` creates a same-session AutoNudge loop. Its stateless MCP
  directive is validated by `mcp_tools.control.monitor_start`, then applied by
  `dashboard.session_directive_apply._monitor_start` through
  `autonudge_authz.authorize_and_add_nudge`. `AutoNudgeService` persists and
  schedules the loop. When the loop's own instruction names exactly one public
  GitHub pull request, the service attaches a monitor and OBSERVES that pull
  request on each tick instead of firing: a tick the probe calls quiet spends no
  agent turn at all. That is the default for `monitor_start`, whose directive asks
  for the gate; `gate: false` is its opt-out. Every OTHER caller defaults to
  UNGATED and must ask for the gate explicitly -- the generic REST route included --
  because gating is the state that can silently STOP work, so an uncertain caller
  must resolve toward spending rather than toward a watch that deactivates itself.
* `pr_watch.py:watch` is a script cron for a quiet pull-request waiting phase.
  `probes.gh_pr.PrWatchProbe` holds the GitHub knowledge and is driven two ways:
  `builtin_skills/kirocrew-dev/babysit/scripts/pr_watch.py:watch` is a thin cron
  adapter over it via `irq.run`, and the AutoNudge scheduler drives the same
  probe in-process via `irq.poll`. The probe lives in the package because a
  hyphenated skill directory is not importable, so a copy beside each driver
  would have meant two copies of one classifier.

The modes are not interchangeable, but they no longer overlap for the common
case. A gated `monitor_start` loop re-injects its instruction only on a cycle
the probe judged worth a turn, so a manual `pr_watch` cron is now for what the
gate cannot reach: an enterprise host, detection with no owning loop, or the
cron's own `known_reds` / `note` / `wake_on_green` knobs. `PrWatchProbe` reports
only a state that needs judgment. This boundary is load-bearing: repeated quiet
CI polls do not grow the session transcript, while a wake retains the session
that owns the work and its tools. The babysit skill documents when to use each
mode.

### What a gated loop changes about the numbers

`max_cycles` counts DELIVERED cycles, so for a gated loop it bounds delivered
TURNS rather than intervals elapsed -- one field bounding two different quantities
depending on whether inference fired, which any budget UI or operator reasoning
has to know. Not wakes: a wake is only one of the four things that consume the
budget, alongside a streak-floor delivery, a gate fallback and a post-wake
follow-up, so reading the cap as a wake count under-states what it spends.
A gated loop is never starved: after `_MAX_QUIET_STREAK` consecutive quiet
observations it is delivered anyway, counted apart from wakes in `floor_ticks` so
a periodic delivery is never read as a real signal. Every uncertain path -- no
probe, no inferable target, a probe defect, a kernel that reached no verdict --
fires as before, because a wrongly-quiet tick is silence with half-finished work
behind it while a wrongly-spent tick costs what every tick costs today.

## Same-session monitor contract

`monitor_start`, `monitor_update`, and `autonudge_stop` are session directives,
not direct AutoNudge mutations. `mcp_tools.control` validates the tool payload,
uses strict session-key resolution only as a context guard, and returns an
encoded directive. `dashboard.session_directive_apply.apply_session_directive`
applies that directive on the user-facing session. The split prevents a cron,
hook, or subagent from using inherited process identity to arm, rewrite, or
stop another session's unattended loop; `test_autonudge_stop_auth.py` pins the
binding-key-only targeting and the non-nudgeable-session refusals.

A directive travels to the consumer as a marker inside the tool's own RESULT
TEXT, and the marker's payload is the only thing that names the record the MCP
stub parked out of band. A backend whose tool results arrive already serialised
as JSON escapes every quote in that payload while leaving the quote-free sentinel
intact, so the frame still looks like it carries a directive and names nothing:
the tool answers "requested" and no loop arms. `acp/_dispatch._repair_escaped_marker`
recovers it at every `EVENT_TOOL_RESULT` builder, `test_session_directive_transport.py`
pins that recovery and ratchets it over any builder a new provider adds, and
[agent-host-contract.md](agent-host-contract.md) §9 states the requirement a
provider must declare. The consumer-side failure paths log at `warning`
(`session-directive NOT APPLIED`, `SELECTOR UNREADABLE`, `CLAIM MISS`, `DENIED`),
which is what makes this class of drop visible in `gateway.log` instead of
silent.

`monitor_start` binds one loop to the calling session. `AutoNudgeService._add_unserialized`
replaces an existing loop on that binding before it persists and arms the new
one. `test_autonudge_stop_auth.py::test_applier_monitor_start_arms_via_the_session_binding_key`
pins that the binding comes from the session rather than tool input.

`NudgeLoop.next_due_ts`, `notify_user_input`, and `notify_turn_complete` make
dashboard-loop cadence deadline-preserving: user activity cancels a pending
timer but does not move its deadline, and a delivered nudge begins its next
full interval when that nudge turn ends. This prevents active conversation
from postponing monitoring forever while avoiding a nudge racing a user turn;
`test_autonudge_deadline.py::test_user_turn_resumes_remaining_time_not_full_interval`
and `test_delivered_fire_clears_deadline_then_turn_end_starts_fresh` pin both
sides of the contract. Channel-bound loops re-arm after their unattended turn
in `AutoNudgeService._run_fire_cycle` because they do not use the dashboard
turn-lifecycle hooks.

The schemas in `validation.MONITOR_START_SCHEMA` and
`validation.MONITOR_UPDATE_SCHEMA` bound the message, interval, cycle cap, and
wall-clock budget. `mcp_tools.control.monitor_start` supplies the bounded
cycle-cap default from `mcp_tools._limits`; its default and explicit unlimited
form are pinned by `test_autonudge_stop_auth.py::test_monitor_start_defaults_interval_300_and_bounded_cap`
and `test_monitor_start_explicit_zero_cap_stays_zero`. The cap is a runaway
backstop, not evidence that the watched work completed: `AutoNudgeService._timer`
deactivates a capped loop and emits `expired`.

`AutoNudgeService.runtime_budget_exceeded` measures a configured wall-clock
budget from the persisted creation time. `_timer` checks it before a fire and
`_run_fire_cycle` checks it after a delivered turn, so a running turn is not
cancelled but an expired loop is not re-armed. `test_autonudge.py` pins budget
persistence across restart and the post-delivery check.

`monitor_update` resolves the loop only by the calling session's binding and
patches its message or limits through `authorize_and_update_nudge`. It does
not accept a loop identifier. `_monitor_update` refuses a new cap or budget
that cannot yield another fire and never revives a manual pause as a side
effect. It may re-arm a loop stopped by its own cycle cap or runtime budget
only when the relevant bound is raised; the paused-loop and bound-revival
tests in `test_autonudge_stop_auth.py` pin those distinctions.

`autonudge_stop` is deliberately non-confirming at tool-call time because the
consumer applies it after the turn result is processed. The applier removes an
ordinary monitor loop on the calling binding and reports an idempotent local
miss. It never exposes a cross-session target; `test_autonudge_stop_auth.py`
pins both the request wording and the local-binding behavior.

## PR watch probe

`PrWatchProbe.identity` accepts a JSON cron message describing one GitHub
repository and pull request, optional inherited-red check names, green-wake
preference, coalescing override, and contextual note. Invalid permanent
configuration raises `ValueError`; `irq.run` converts that to `Done`, so a
malformed job removes itself instead of retrying indefinitely. The malformed
message tests in `test_babysit_pr_watch.py` pin this behavior.

`PrWatchProbe._fetch` runs one bounded `gh pr view` through
`github_runner.resolve_gh` and `github_runner.run_gh`. The shared runner
validates the executable, supplies the restricted GitHub environment, and
audits the spawn. A failed or malformed fetch becomes `Tick(fetch_ok=False)`;
it does not make the script crash.

`PrWatchProbe.observe` produces these observations:

* A merged PR or a closed unmerged PR is `Severity.TERMINAL`, so `irq.run`
  reports it and removes the cron job. `test_merged_pr_completes_the_watch` and
  `test_closed_unmerged_completes_the_watch` pin both terminal paths.
* A conflicting or dirty PR is `Severity.NMI`. `irq.run` bypasses coalescing
  delay but still deduplicates it, because waiting cannot produce checks on a
  dirty PR and unmasked repetition would wake every tick. The conflict and
  re-alert tests pin this behavior.
* `_collapse` buckets check-rollup rows, retains the current row for a check
  identity, treats unknown conclusion vocabulary conservatively, and treats
  cancelled or stale rows as noise. A failure that is not listed in
  `known_reds` produces a `red:` wake; matching accepts the qualified alert
  identity or a bare UI check name. Tests cover inherited-red filtering,
  same-name workflow separation, rerun handling, unknown conclusions, and
  cancelled rows.
* With a non-empty rollup, no pending rows, and no unexpected failures,
  `wake_on_green` permits a `ready` wake. An empty rollup is not evidence that
  checks passed; `test_empty_rollup_never_reports_ready` pins that guard.
* `_conversation` emits epoch-independent wakes for recent comments not
  authored by the viewer and for recent submitted reviews. It identifies the
  author and timestamp but does not quote comment text. `reviewDecision` is
  not observed because it has no timestamp suitable for age filtering. The
  comment-horizon and conversation tests in `test_babysit_pr_watch.py` pin
  these rules.

The probe returns observations, not cron-control exceptions. `irq.Probe` and
`irq.run` own the verdict so every probe receives the same terminal handling,
deduplication, coalescing, state persistence, and failure backstop.

## Watch kernel invariants

`irq.state_path` includes the subject identity and cron job identifier. Two
jobs watching one PR therefore do not suppress each other's alerts. `load_state`
treats missing or malformed state as fresh and `save_state` uses `atomic_write`;
the degradation is a possible duplicate wake, not a crash-loop. If persistence
fails while a coalescing window is open, `irq.run` reports immediately with a
warning rather than delaying an observation into state it cannot recover.

A `Tick.epoch` changes when the PR head changes. `irq.run` clears
epoch-scoped dedupe and coalescing state on that change, so failures on the new
head can wake again. Conversation observations set `epoch_scoped=False`, so a
force-push does not replay an already-seen comment or review. These distinct
key spaces are load-bearing: treating every signal as head-scoped loses
conversation dedupe, while treating every signal as sticky hides failures on a
new head.

`irq.run` coalesces ordinary wake observations until the configured floor has
elapsed and the check rollup settles, or until its hard wall elapses. The hard
wall ensures a permanently pending check delays a wake instead of losing it.
Sticky conversation observations can fire once the floor elapses even while
checks remain pending; they do not become more informative by waiting for CI.
`Severity.NMI` and `Severity.TERMINAL` bypass the ordinary window. The
coalescing and sticky-observation tests in `test_irq.py` pin these cases.

Dedupe is time-bounded. The kernel re-alerts a persistent condition after its
window because a script cannot observe whether gateway delivery succeeded;
permanent acknowledgement could turn one lost delivery into permanent silence.
The comment horizon in `pr_watch.py` is asserted below the kernel's re-alert
window, so stale conversation entries age out before a sticky dedupe key is
removed. `test_alert_rearms_after_the_dedupe_window` and the horizon tests pin
both sides.

`Tick(fetch_ok=False)` increments the kernel-owned error streak. A persistent
failure reports that the watch is blind; a successful fetch clears the streak
and its blind marker. If state cannot be written, the kernel reports on the
first failed tick because a counted threshold would otherwise be unreachable.
The watch-health tests in `test_babysit_pr_watch.py` pin recovery, re-alerting,
and the unwritable-state path.

## Delivery and lifecycle

Script cron execution maps `Skip` to no delivery, `Report` to a result while
keeping the job, and `Done` to a result whose successful delivery removes the
job. The script-cron branch in `slack.gateway._init_cron` delivers a result to
the originating dashboard slot, queues it if that slot is busy, and rehydrates
a closed slot from history when possible. If no slot is available, it sends a
notification instead. This makes the arming session the normal wake target
without claiming that headless delivery can start a session.

The bundled script is a source asset, not a gateway import. `cron_script.resolve_script_path`
requires a registered script file under the configured cron directory, so the
babysit skill's watch recipe copies the bundled `pr_watch.py` there before
registering it. The cron gateway revalidates and scans the current script body
at fire time, then executes it through the script sandbox.

## Non-goals

The PR watch does not parse comment bodies or decide whether a review finding
is valid. It reports a change and leaves judgment, source inspection, and any
reply to the reactivated babysit session. It also watches one pull request per
cron job. `monitor_start` remains the appropriate mechanism when each cycle
requires the agent to make progress or when the watched subject is not a pull
request.

# Babysit PR watch

## Purpose

The agent-facing babysit flow prefers `monitor_watch`. It creates a durable,
typed structured monitor through a session-bound directive. The controller probes
the provider before invoking the model, persists canonical observations and
budgets, and wakes the owning session only for a new actionable fingerprint.
Provider-fact-only GitHub review readiness therefore spends no agent turn while
the pull request is unchanged.

`monitor_start` creates a finite same-session AutoNudge loop for objectives or
evidence the structured provider cannot decide, including generic comments and
advisory review text. Its stateless directive is validated by
`mcp_tools.control.monitor_start`, then applied by
`dashboard.session_directive_apply._monitor_start` through
`autonudge_authz.authorize_and_add_nudge`. `AutoNudgeService` persists and
schedules the loop. A loop whose instruction names exactly one public GitHub pull
request may still attach `PrWatchProbe` as a compatibility gate, but that path is
the bounded legacy fallback rather than the babysit recipe.

The bundled `pr_watch.py:watch` cron adapter remains a compatibility asset for
existing registered jobs. New babysit requests do not copy or register it; they
use `monitor_watch` or a finite `monitor_start` loop owned by the session that can
inspect and act on a wake. `probes.gh_pr.PrWatchProbe` stays in the package because
the legacy AutoNudge gate and existing script jobs share its classifier.

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

`monitor_start` binds one loop to the calling session and is create-only. It
refuses when either automation kind already occupies the binding, preserving the
existing record and its evidence. `monitor_update` is the only way to revise or
re-arm the bound legacy loop. The binding-key and collision tests in
`test_autonudge_stop_auth.py` pin that behavior.

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
wall-clock budget. `mcp_tools.control.monitor_start` supplies bounded positive
defaults from `mcp_tools._limits`; zero and negative cycle or runtime limits are
rejected. The cap is a runaway backstop, not evidence that the watched work
completed: `AutoNudgeService._timer` deactivates a capped loop and emits
`expired`.

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

The bundled script is a source asset, not a gateway import. Existing jobs must
still resolve a registered copy under the configured cron directory through
`cron_script.resolve_script_path`. The cron gateway revalidates and scans that
current script body at fire time, then executes it through the script sandbox.
The babysit skill no longer registers new script jobs.

## Non-goals

The structured GitHub monitor does not parse generic comment bodies or decide
whether an advisory finding is valid. It reports typed provider facts and leaves
judgment, source inspection, and any reply to the reactivated babysit session.
`monitor_start` remains appropriate when each delivered cycle requires the agent
to make progress, the objective requires untyped evidence, or the watched subject
is unsupported by a structured provider.

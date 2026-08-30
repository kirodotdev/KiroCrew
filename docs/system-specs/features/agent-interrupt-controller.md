# Agent Interrupt Controller (`kiro_crew.irq`)

Status: implemented

Owners: `kiro_crew.irq`; in-tree GitHub pull-request probe:
`builtin_skills/kirocrew-dev/babysit/scripts/pr_watch.py`.

## Purpose

`kiro_crew.irq` lets a script cron perform cheap observation and request an
agent turn only when a probe reports an actionable condition. `Probe` supplies
subject identity and a `Tick`; `irq.run` owns persisted state, deduplication,
coalescing, and the `Skip` / `Report` / `Done` verdict. The controller's
verdict ownership keeps probes from each encoding different retry and delivery
policies. See `irq.Probe`, `irq.Tick`, and `irq.run`.

The in-tree consumer is `PrWatchProbe`. It reads a pull request through `gh`,
classifies pull-request state and checks, and passes its result to `irq.run`
through `watch`. See `pr_watch.PrWatchProbe.observe` and `pr_watch.watch`.

## Authoring contract

`Observation` carries a stable key, a severity, a delivery brief, and whether
its identity belongs to the current epoch. `Tick` carries the current epoch,
observations, pending work, fetch status, and a quiet-tick detail. The public
surface is `irq.__all__`; `test_probe_tuning_overrides_a_bound` and
`test_probe_tuning_cannot_hand_the_kernel_a_fatal_bound` pin the exercised
probe-tuning contract.

A probe implements:

* `Probe.identity(ctx) -> (subject_kind, subject_id)`. `irq.run` calls it once
  per tick. A `ValueError` becomes `Done`, so a permanently invalid cron
  message removes the job instead of raising on every tick. This is pinned by
  `test_identity_value_error_becomes_done` and
  `test_identity_called_exactly_once_per_tick`.
* `Probe.observe(ctx) -> Tick`. A probe that cannot read its subject returns
  `Tick(fetch_ok=False)`. `irq.run` ignores observations in such a tick and
  uses the persisted error streak instead; treating an unreadable subject as a
  quiet subject would hide a blind watch. `test_observations_ignored_when_fetch_failed`
  pins that distinction.
* Optional `Probe.tuning()` and `Probe.wake_suffix()`. `irq.run` calls both
  after identity parsing, accepts only the exercised tuning key, validates
  numeric bounds, and drops a broken or non-string suffix. The suffix is
  appended once to a delivered wake, not once for each observation. See
  `irq.run`, `test_probe_tuning_raising_does_not_kill_the_tick`, and
  `test_the_wake_footer_is_emitted_once_not_once_per_observation`.

`Severity.TERMINAL` ends the watch with `Done`. `Severity.NMI` reports
immediately but still participates in deduplication. `Severity.WAKE` enters
the regular coalescing path. Terminal handling precedes NMI and coalescing in
`irq.run`; `test_terminal_wins_over_an_open_window` and
`test_nmi_bypasses_the_coalescing_window` pin the ordering.

## State identity and recovery

`state_path` creates one state path for each subject and cron job. It folds the
human-readable path components and includes the unfolded identity in its digest.
This keeps watches of the same subject independent and prevents folded subject
identities from sharing state; see `test_state_path_separates_two_jobs_on_one_subject`
and `test_state_path_does_not_collide_on_fold_equivalent_subjects`.

`load_state` accepts only the expected state shapes and converts malformed or
unusable persisted data to fresh state. This trades possible repeat delivery
for keeping the cron alive; a parse failure must not become a crash loop. See
`irq.load_state` and `test_malformed_state_degrades_to_fresh`.

`save_state` persists state through `atomic_write` with owner-only file mode.
If persistence fails, the watch remains active and wakes include the persistence
warning. Coalescing then delivers the current window rather than retaining an
unrememberable delay. This is load-bearing because a new cron process otherwise
loads an empty window on each tick and the withheld signal never reaches its
fire condition. See `irq.save_state`, `irq.run`,
`test_unwritable_state_delivers_instead_of_swallowing_the_window`, and
`test_a_partial_fire_defers_to_the_unwritable_state_fallback`.

## Dedupe and epochs

`irq.run` stores epoch-scoped and epoch-independent keys in separate sentinel
spaces. The same probe key in both spaces remains two signals; see
`test_the_two_key_spaces_do_not_collide`.

When a nonempty `Tick.epoch` changes, `irq.run` removes epoch-scoped alerts and
open-window entries, then retains epoch-independent alerts and open-window
entries. Check-derived observations must not survive a head change, or an old
head can be reported as current. Conversation-derived observations must survive,
or a head change replays already-reported discussion. The carried window receives
a fresh start stamp: this preserves the pending delivery while ensuring a fresh
head receives its full settling floor. These invariants are pinned by
`test_an_open_epoch_scoped_window_is_dropped_by_an_epoch_change`,
`test_a_sticky_key_survives_an_epoch_change`,
`test_a_fresh_epoch_anomaly_still_gets_a_full_settling_floor`, and
`test_a_carried_sticky_entry_is_delayed_not_lost_by_the_restart`.

Dedupe re-arms after the configured re-alert interval. Future timestamps read
as stale, and recovery clears the blind marker. The error threshold comparison
uses `>=`, so a missed delivery cannot leave a persisted count permanently past
the only reporting value. See `irq.run`,
`test_dedupe_rearms_after_the_realert_window`,
`test_future_timestamp_reads_as_stale_not_as_forever_suppression`,
`test_recovered_streak_clears_blind_marker`, and
`test_blind_probe_reports_at_threshold_not_only_at_equality`.

## Coalescing

A non-NMI `WAKE` observation opens a persisted window. The window fires all
entries when the convergence floor has passed and `Tick.pending` is zero, or
when the hard cap has passed; the hard cap is independent of the floor. The
floor prevents a newly changed subject from reporting a transient empty rollup
as convergence. The cap prevents a permanently pending subject from losing an
otherwise actionable wake. These properties are pinned by
`test_floor_blocks_a_premature_converged_wake`,
`test_hard_cap_fires_when_pending_never_drains`, and
`test_hard_cap_outranks_a_floor_set_above_it`.

While pending work remains after the floor, epoch-independent entries fire and
epoch-scoped entries remain in the window. The partial fire retains the original
window start time for the remaining entries, so repeated discussion cannot keep
postponing a check-derived observation. See
`test_a_sticky_wake_fires_at_the_floor_while_checks_are_still_pending` and
`test_the_sticky_half_fires_while_the_epoch_scoped_half_keeps_waiting`.

`irq.run` prunes an epoch-scoped entry when the probe no longer observes it,
which prevents a cleared check from appearing in a later wake. It retains an
epoch-independent entry that the probe stops observing, because a conversation
horizon means "not currently inspected," not "cleared." See
`test_cleared_anomaly_is_pruned_from_an_open_window`,
`test_an_open_sticky_wake_is_not_pruned_when_the_probe_stops_reporting_it`, and
`test_an_open_epoch_scoped_wake_is_still_pruned_when_it_clears`.

A zero coalescing floor uses immediate `WAKE` delivery. The behavior is pinned
by `test_coalesce_secs_zero_restores_fire_on_first_anomaly`.

## GitHub pull-request probe

`PrWatchProbe.identity` validates the message's repository, pull-request
identifier, inherited-failure list, and coalescing value before returning the
watch identity. `PrWatchProbe.tuning` supplies the message-derived coalescing
override. `watch` constructs the probe and calls `irq.run`.

`PrWatchProbe.observe`:

* returns terminal observations for merged and closed pull requests;
* emits an NMI observation for conflicting or dirty pull requests;
* collapses duplicate check rows, filters known inherited failures, and emits
  `WAKE` observations for unexpected failures;
* emits a review-ready observation only when checks are present, no checks are
  pending, and no unexpected failures remain; and
* emits epoch-independent observations for recent comments and submitted
  reviews, without including their bodies in a wake brief.

`_collapse`, `_conversation`, and `observe` implement those classifications.
The comment horizon has an import-time assertion that it expires before the
controller's re-alert interval, so expired sticky dedupe state cannot replay an
old comment. See `pr_watch.DEFAULT_COMMENT_HORIZON_SECS` and
`PrWatchProbe._conversation`.

`PrWatchProbe._fetch` returns `None` for an unavailable or malformed `gh`
response. `PrWatchProbe.observe` converts that result to an unreadable tick,
feeding the controller's error backstop rather than raising from the cron entry
point. `pr_watch._run_gh` routes the command through `github_runner.run_gh`.

## Non-goals

The controller runs a single tick; cron cadence, retries, and job registration
belong to the cron service. The pull-request probe detects comment and review
metadata, not their prose. The woken agent reads and judges discussion after a
wake; `PrWatchProbe._conversation` deliberately keeps body text out of the
probe's observations.

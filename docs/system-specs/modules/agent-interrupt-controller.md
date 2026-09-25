# Agent Interrupt Controller (`kiro_crew.irq`)

Status: implemented

Owners: `kiro_crew.irq`; in-tree GitHub pull-request probe:
`kiro_crew.probes.gh_pr`.

## Purpose

`kiro_crew.irq` lets a driver perform cheap observation and request an agent
turn only when a probe reports an actionable condition. `Probe` supplies
subject identity and a `Tick`; the kernel owns persisted state, deduplication,
coalescing, and the verdict. The controller's verdict ownership keeps probes
from each encoding different retry and delivery policies. See `irq.Probe`,
`irq.Tick`, and `irq.run`.

Two drivers, one kernel, and the difference is only how the verdict is
delivered:

* `irq.run` RAISES `Skip` / `Report` / `Done`, which is what the cron runner
  consumes. Unchanged.
* `irq.poll` RETURNS a `Verdict(outcome, body)` with outcome `QUIET`, `WAKE`,
  `TERMINAL`, or `FALLBACK`, for an in-process driver that owns its own wake
  mechanism and only needs the decision -- the AutoNudge scheduler's probe gate.
  Anything unexpected resolves to `FALLBACK`, telling the driver to keep the
  schedule it already had, because the alternative default would convert a bug
  into silence. A redundant cycle costs tokens; a lost wake costs the task. The
  kernel's bounds are deliberately not forwarded through `poll`: a probe already
  declares what it needs through `Probe.tuning`.

The in-tree consumer is `PrWatchProbe`. It FETCHES a pull request through `gh` and
classifies nothing: it publishes the reading on itself and returns a tick carrying
only what the kernel needs of its own -- an epoch, a pending count, and whether the
subject was reachable. The driver (`irq.poll`, from `autonudge.py`) reads the
published reading and the wake judge decides on it.

So the kernel serves this consumer for two things a stateless reading cannot hold:
the epoch, so dedupe memory resets on a new head, and the consecutive-failure
backstop, which turns a run of unreadable ticks into one report that the watch is
blind. See `probes.gh_pr.fetch` and `probes.gh_pr.PrWatchProbe.observe`.

## Authoring contract

`Observation` carries a stable key, a severity, a delivery brief, and whether
its identity belongs to the current epoch. `Tick` carries the current epoch,
observations, pending work, fetch status, and a quiet-tick detail. The public
**probe-authoring** surface is `irq.__all__`; the qualified `irq.poll` entry point
is the in-process driver's internal seam. `test_probe_tuning_overrides_a_bound` and
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

`Severity.TERMINAL` ends the watch with `Done`. `Severity.IMMEDIATE` reports
immediately but still participates in deduplication. `Severity.WAKE` enters
the regular coalescing path. Terminal handling precedes `IMMEDIATE` and coalescing
in `irq.run`; `test_terminal_wins_over_an_open_window` and
`test_immediate_bypasses_the_coalescing_window` pin the ordering.

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

`irq.run` stores `REVISION` and `NEVER` keys in separate sentinel
spaces. The same probe key in both spaces remains two signals; see
`test_the_two_key_spaces_do_not_collide`.

When a nonempty `Tick.epoch` changes, `irq.run` removes `REVISION` alerts and
open-window entries, then retains `NEVER` alerts and open-window
entries. Check-derived observations must not survive a head change, or an old
head can be reported as current. Conversation-derived observations must survive,
or a head change replays already-reported discussion. Each carried entry keeps
its own open time, so a force-push does not make a settled discussion wake serve
the floor again. A fresh head observation is a separate entry with a fresh open
time, so it still receives the full settling floor; on the transition tick it
cannot ride on a carried entry that is already ready to fire.
These invariants are pinned by
`test_an_open_revision_window_is_dropped_by_an_epoch_change`,
`test_a_sticky_key_survives_an_epoch_change`,
`test_a_fresh_epoch_anomaly_still_gets_a_full_settling_floor`, and
`test_a_carried_sticky_entry_keeps_its_served_floor_across_epoch_change`.

Dedupe re-arms after the configured re-alert interval. Future timestamps read
as stale, and recovery clears the blind marker. The error threshold comparison
uses `>=`, so a missed delivery cannot leave a persisted count permanently past
the only reporting value. See `irq.run`,
`test_dedupe_rearms_after_the_realert_window`,
`test_future_timestamp_reads_as_stale_not_as_forever_suppression`,
`test_recovered_streak_clears_blind_marker`, and
`test_blind_probe_reports_at_threshold_not_only_at_equality`.

## Coalescing

A `WAKE` observation that is not `IMMEDIATE` opens a persisted window, and each
entry records its
own open time. An entry triggers delivery when its own age has passed the
convergence floor and `Tick.pending` is zero, or, for a `NEVER` entry,
when its own age has passed the floor alone. Separately, when the oldest entry's
age passes the hard cap the whole window flushes; the hard cap is independent of
the floor and remains window level. The floor prevents a newly changed subject
from reporting a transient empty rollup as convergence. The cap prevents a
permanently pending subject from losing an otherwise actionable wake, and stays a
window-level flush because withholding an unconverged entry from a cap wake would
turn one flush into one wake per entry.
The floor is per entry because one window holds signals of different ages: a
partial fire leaves entries that have already waited, and a later tick can add one
that has not. A shared window age let such an entry trigger a wake with no
settling window of its own, so signals arriving one at a time each produced a
wake. Once an entry triggers, the wake carries every entry the population gate
admits, aged or not; riding along cannot add a wake, while withholding an admitted
entry produces another one later. These properties are pinned by
`test_floor_blocks_a_premature_converged_wake`,
`test_hard_cap_fires_when_pending_never_drains`,
`test_hard_cap_outranks_a_floor_set_above_it`,
`test_the_hard_cap_flushes_the_WHOLE_window_not_only_the_capped_entry`,
`test_an_entry_joining_after_a_partial_fire_serves_its_own_floor`, and
`test_coalescing_folds_staggered_reds_into_one_wake`.

`coalesce_started_at` is never written. It survives as a read in `load_state`
only, to seed entries persisted before they carried their own open time, so an
upgrade does not restart an in-flight window; see
`test_a_window_written_before_per_entry_ages_keeps_the_age_it_had`. Because it is
not stored, the legacy field falls off at the first write after the upgrade.

The window age the cap reads is the oldest SURVIVING entry's age, so a partial
fire that delivers the entry which opened the window moves the cap clock onto the
survivor. That is a bound rather than a reset: an entry is flushed no later than
the cap measured from its own arrival. See
`test_an_entry_left_by_a_partial_fire_still_reaches_the_cap`.

While pending work remains after the floor, `NEVER` entries fire and
`REVISION` entries remain in the window. The remaining entries keep their own
open times, so repeated discussion cannot keep postponing a check-derived
observation. See
`test_a_sticky_wake_fires_at_the_floor_while_checks_are_still_pending` and
`test_the_sticky_half_fires_while_the_revision_half_keeps_waiting`.

`irq.run` prunes a `REVISION` entry when the probe no longer observes it,
which prevents a cleared check from appearing in a later wake. It retains a
`NEVER` entry that the probe stops observing, because a conversation
horizon means "not currently inspected," not "cleared." See
`test_cleared_anomaly_is_pruned_from_an_open_window`,
`test_an_open_sticky_wake_is_not_pruned_when_the_probe_stops_reporting_it`, and
`test_an_open_revision_wake_is_still_pruned_when_it_clears`.

A zero coalescing floor uses immediate `WAKE` delivery. The behavior is pinned
by `test_coalesce_secs_zero_restores_fire_on_first_anomaly`.

## GitHub pull-request fetcher

`PrWatchProbe.identity` validates the message's repository, pull request, and
optional pinnable host before returning the watch identity. `PrWatchProbe.observe`
calls `probes.gh_pr.fetch`, stores the result on `PrWatchProbe.observation`, and
returns `Tick(observations=[])` -- the fetcher raises no wake, so nothing here is
deduped or coalesced.

`fetch` reads, in one bounded tick budget: the pull request's state, mergeability,
merge state, review decision, draft flag and head; its check runs, paginated
against the API's own `total_count`; its commit statuses, which are a separate
sequence a required gate can be published in; and its comments and reviews WITH
bodies, clipped per item and in total, inside a fetch horizon, skipping the bot's
own comments.

Every call goes through `_Transport`: `github_runner.run_gh` with the validated
absolute path, the restricted environment, an SEL audit record and the pinned
host; a per-call timeout under the tick budget; bounded retry with jittered
exponential backoff; and rate limits read off the response headers so a call backs
off on a nearly-spent window rather than being refused by it.

One `status` per reading says how complete it is: `ok`, `partial`, or
`unavailable`. A forge refusal becomes a status, never an exception; only
`unavailable` reaches the kernel as `Tick(fetch_ok=False)` and feeds the error
backstop. `PrObservation.as_facts` is the durable half and carries no prose;
`PrObservation.bodies` is a separate call, and what it returns stays in the process
that fetched it.

## Non-goals

The controller runs a single tick; cadence, retries and job registration belong to
its driver. The pull-request fetcher makes no wake decision at all: it reports what
it read, including comment and review bodies, and the wake judge decides against
the loop's own criteria. The one deterministic mapping -- a merged or closed pull
request ends the watch -- belongs to the auto-nudge core, not to this controller
and not to the fetcher.

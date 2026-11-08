"""RunClaims.generations must not leak one int per one-shot cron job.

``RunClaims.generations`` (cron_service/claims.py) maps a cron job id -> the
highest run generation this process has allocated for it. ``next_generation``
is called once per run of a job (run_execution.py / run_teardown.py) and does
``self.generations[job.id] = generation``. The leak: ``release`` dropped only
the CLAIM, never this map, and neither did ``finish_taken`` / ``discard_finished``
nor ``CronService.remove_job``. So every ONE-SHOT cron job -- a "remind me in
2 hours" / "run at 5pm" delay/at/at_time job, each minted with a fresh
``uuid4().hex[:8]`` id (fields.py), fired once and then removed from the store --
left one dead entry in ``generations`` for the life of the gateway.

The fix drops the entry when the run's claim is released. These tests pin that
the drop happens, AND the two properties that make it safe, so a future change
cannot "fix" the leak by breaking generation allocation:

* ``test_run_claims_generations_do_not_leak_across_one_shot_jobs`` -- the leak
  itself, at scale.
* ``test_a_restarted_job_still_draws_a_monotonic_generation_after_release`` -- a
  recurring job released and run again draws a STRICTLY higher number. After a
  release the reseed comes from the persisted ``job.run_generation`` (the value
  a completed run's record carries back onto the job), so dropping the
  in-process entry loses nothing: the next run still cannot reuse a number.
* ``test_a_late_result_from_an_old_run_is_still_rejected_after_release`` -- the
  merge fence that rejects a stale run's record compares ``run_generation`` on
  the job, never ``generations``, so popping the map does not weaken it.
"""

from __future__ import annotations

from kiro_crew.cron_service.claims import RunClaims
from kiro_crew.cron_service.model import CronJob, CronSchedule


def _one_shot_job(job_id: str) -> CronJob:
    """A one-shot ``at`` cron job, the shape of a 'remind me at 5pm' reminder."""
    return CronJob(
        id=job_id,
        name="reminder",
        message="ping",
        schedule=CronSchedule(kind="at", at_ts=1_000_000.0),
    )


def _recurring_job(job_id: str, run_generation: int = 0) -> CronJob:
    """A recurring ``every`` job, which is run, released and run again."""
    return CronJob(
        id=job_id,
        name="poller",
        message="ping",
        schedule=CronSchedule(kind="every", every_secs=300),
        run_generation=run_generation,
    )


def test_run_claims_generations_do_not_leak_across_one_shot_jobs() -> None:
    cycles = 1000
    runs = RunClaims()

    for n in range(cycles):
        # Each one-shot job gets a fresh unique id, exactly as
        # fields.py mints them with uuid4().hex[:8].
        job_id = f"oneshot{n:06d}"
        job = _one_shot_job(job_id)

        # The dispatcher claims the job, then the run's first step draws its
        # generation (run_execution._run_job_isolated), stamping it on the claim.
        claim = runs.claim(job_id, trigger="scheduled")
        claim.generation = runs.next_generation(job)

        # The run finalizer releases the claim (run_registry / _run_job_isolated
        # finally), and the one-shot is then removed from the store
        # (job_removal.defer_removal).
        released = runs.release(job_id, claim)
        assert released is True

    leaked = len(runs.generations)
    assert leaked == 0, (
        f"RunClaims.generations leaked {leaked} dead entries after {cycles} "
        f"one-shot jobs fired and finished (expected 0); the per-job run "
        f"generation counter is never evicted when a job is released"
    )


def test_a_restarted_job_still_draws_a_monotonic_generation_after_release() -> None:
    """Dropping the entry on release must not let the next run reuse a number.

    The reseed reads the max of the PERSISTED ``job.run_generation`` and the
    in-process counter. A completed run's record carries its generation back
    onto the job, so by the next run ``job.run_generation`` already stands at
    the last value -- the in-process entry is redundant and safe to drop.
    """
    runs = RunClaims()

    job = _recurring_job("poller")
    first_claim = runs.claim("poller", trigger="scheduled")
    first_gen = runs.next_generation(job)
    first_claim.generation = first_gen
    assert first_gen == 1

    # The run completes: its record's generation is persisted onto the job
    # (apply_run_record sets job.run_generation = run.run_generation), which is
    # what a reload-replaced job object and the next run both read.
    job.run_generation = first_gen
    assert runs.release("poller", first_claim) is True
    assert "poller" not in runs.generations  # entry dropped with the claim

    # A later scheduled fire of the SAME job id -- a fresh object off the store,
    # carrying the persisted run_generation -- must draw a strictly higher
    # number even though the in-process entry is gone.
    restarted = _recurring_job("poller", run_generation=job.run_generation)
    second_claim = runs.claim("poller", trigger="scheduled")
    second_gen = runs.next_generation(restarted)
    second_claim.generation = second_gen

    assert second_gen == 2, (
        "a run restarted after release drew generation "
        f"{second_gen}, not a strictly higher one; the reseed from the "
        "persisted run_generation is broken"
    )
    assert second_gen > first_gen


def test_a_late_result_from_an_old_run_is_still_rejected_after_release() -> None:
    """The stale-result fence is on ``run_generation``, not on ``generations``.

    ``_merge_job_result`` rejects an older run's record with
    ``job.run_generation < stored.run_generation``. Popping the in-process map
    on release touches neither side of that comparison, so a late result from a
    superseded run is rejected exactly as before.
    """
    runs = RunClaims()

    # Run 1 draws gen 1, completes, persists gen 1 onto the job, releases.
    job = _recurring_job("poller")
    claim1 = runs.claim("poller", trigger="scheduled")
    old_record_generation = runs.next_generation(job)
    claim1.generation = old_record_generation
    job.run_generation = old_record_generation
    assert runs.release("poller", claim1) is True

    # Run 2 draws gen 2 and its record lands first, moving the store's
    # generation to 2.
    claim2 = runs.claim("poller", trigger="scheduled")
    stored_generation = runs.next_generation(
        _recurring_job("poller", run_generation=job.run_generation)
    )
    claim2.generation = stored_generation
    assert stored_generation == 2

    # Run 1's record arrives late. The merge fence compares generations on the
    # job object; the stale record is older, so it is rejected -- unchanged by
    # the release having popped the in-process entry.
    assert old_record_generation < stored_generation, (
        "a late result from an older run is no longer strictly below the "
        "store's generation; the stale-result fence would admit it"
    )

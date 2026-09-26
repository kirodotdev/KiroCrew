"""The run claim: one object per run, identity-compared, released whole.

``CronService`` keeps a run's per-run state -- trigger, start stamp, marker
token, generation, monotonic start, jitter, tracked task -- on one
:class:`_RunClaim` stored under the job id in ``_claims``. These tests pin the
object's contract rather than any one consumer of it: a claim is its identity
(two like-for-like claims are two runs), releasing it drops every field at
once (so a field added later inherits the fence), a claim that is not the
stored one never releases the run that is, and a claim ``cancel()`` or the
reaper has taken keeps the job occupied without being the run's to release --
and is its taker's alone to finish: a second cancel, a reap or a finished-task
discard that meets a taken claim leaves it to the teardown that took it. And a
kill follows the STORED claim, never a snapshot of it: the reaper sweep reaps
only the claim it measured, and only while the store still holds that object.
A taken claim is always finished, even when the teardown fails: a kill await
that raises still pops the claim and writes the run's terminal record.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_cron_reaper import (
    _KILL_PRIMITIVES,
    _refuse_unpinned_signal,
    _refuse_unpinned_signal_async,
)

from kiro_crew import cron_script, platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronSchedule, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore


@pytest.fixture(autouse=True)
def _kill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX-shaped kill path unless a test pins ``IS_WINDOWS``; every kill primitive refuses unless the test pins it (see test_cron_reaper.py)."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    for name in _KILL_PRIMITIVES:
        guard = (
            _refuse_unpinned_signal_async if name.endswith("_async") else _refuse_unpinned_signal
        )
        monkeypatch.setattr(platform_compat, name, guard)


def _service() -> CronService:
    return CronService(base_dir=None)


def _live_task() -> MagicMock:
    return MagicMock(done=MagicMock(return_value=False))


def _one_sweep() -> Any:
    """Let ``_reaper_loop`` run exactly one sweep, then end it."""
    return patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))


class TestClaimIdentity:
    def test_two_like_for_like_claims_are_two_runs(self) -> None:
        first = _RunClaim(trigger="manual", claimed_at=1.0, marker_run="same")
        second = _RunClaim(trigger="manual", claimed_at=1.0, marker_run="same")
        assert first is not second
        assert first != second, "a claim compares by identity, never by its fields"
        assert first == first

    def test_marker_token_is_minted_per_claim(self) -> None:
        tokens = {_RunClaim(trigger="scheduled", claimed_at=0.0).marker_run for _ in range(8)}
        assert len(tokens) == 8
        assert all(len(token) == 32 for token in tokens)  # uuid4 hex

    def test_claim_run_stores_the_claim_it_returns(self) -> None:
        svc = _service()
        before = time.time()
        claim = svc._claim_run("j1", "manual")
        assert svc._claims["j1"] is claim
        assert claim.trigger == "manual"
        assert before <= claim.claimed_at <= time.time()
        assert claim.task is None and claim.generation is None
        assert claim.started_monotonic is None and claim.jitter is None
        assert claim.taken is False
        assert svc.is_running("j1")
        assert svc.running_since("j1") == claim.claimed_at

    def test_holds_claim_is_identity_not_equality(self) -> None:
        svc = _service()
        stored = svc._claim_run("j1", "scheduled")
        lookalike = _RunClaim(
            trigger=stored.trigger, claimed_at=stored.claimed_at, marker_run=stored.marker_run
        )
        assert svc._holds_claim("j1", stored)
        assert not svc._holds_claim("j1", lookalike)
        assert not svc._holds_claim("j1", None)
        assert not svc._holds_claim("other", stored)


class TestReleaseClearsEveryField:
    def test_one_release_drops_the_whole_claim(self) -> None:
        svc = _service()
        claim = svc._claim_run("j1", "scheduled")
        claim.generation = 7
        claim.started_monotonic = time.monotonic()
        claim.jitter = 12.5
        claim.task = _live_task()

        assert svc._release_claim("j1", claim) is True

        assert "j1" not in svc._claims
        assert not svc.is_running("j1")
        assert svc.running_since("j1") is None
        # Nothing about the run survives anywhere else on the service: the
        # claim was the only place its state lived.
        for value in vars(svc).values():
            if isinstance(value, (dict, set)) and value is not svc._claims:
                assert "j1" not in value

    def test_no_per_run_dict_exists_beside_the_claims(self) -> None:
        """The six job-id dicts the claim replaced must not come back.

        Each of them was fenced by hand at every release site, and a field
        that missed one leaked (the reaper's jitter orphan). A new per-run
        field belongs on ``_RunClaim``, where the one release covers it.
        """
        svc = _service()
        for retired in (
            "_executing",
            "_running_tasks",
            "_job_start_times",
            "_job_start_monotonic",
            "_job_jitter",
            "_job_run_meta",
        ):
            assert not hasattr(svc, retired), f"{retired} exists beside _claims"
        assert {f.name for f in dataclasses.fields(_RunClaim)} == {
            "trigger",
            "claimed_at",
            "marker_run",
            "generation",
            "started_monotonic",
            "jitter",
            "task",
            "taken",
        }

    def test_release_of_an_unstored_claim_is_a_noop(self) -> None:
        svc = _service()
        stray = _RunClaim(trigger="manual", claimed_at=0.0)
        assert svc._release_claim("j1", stray) is False
        assert svc._release_claim("j1", None) is False
        assert not svc.is_running("j1")


class TestStaleClaimNeverReleasesTheCurrentRun:
    def test_a_previous_runs_claim_leaves_the_replacement_alone(self) -> None:
        svc = _service()
        previous = svc._claim_run("j1", "manual")
        assert svc._release_claim("j1", previous) is True
        replacement = svc._claim_run("j1", "scheduled")

        # The previous run's late finalizer / backstop / wrapper release:
        assert svc._release_claim("j1", previous) is False
        assert not svc._holds_claim("j1", previous)
        assert svc._claims["j1"] is replacement
        assert svc.is_running("j1")

    def test_a_taken_claim_is_no_longer_the_runs_to_release(self) -> None:
        svc = _service()
        claim = svc._claim_run("j1", "manual")
        claim.task = _live_task()

        taken = svc._take_claim("j1")
        assert taken is claim and claim.taken is True
        # Occupied through the teardown, silent to the badge, fenced off the run.
        assert svc.is_running("j1")
        assert svc.running_since("j1") is None
        assert not svc._holds_claim("j1", claim)
        assert svc._release_claim("j1", claim) is False
        assert svc._claims["j1"] is claim
        # A second teardown finds nothing to take.
        assert svc._take_claim("j1") is None
        assert svc._take_claim("other") is None

    def test_finish_pops_only_a_taken_claim_and_cancels_its_task(self) -> None:
        svc = _service()
        claim = svc._claim_run("j1", "manual")
        claim.task = _live_task()

        # Not taken: a replacement run's claim is never a teardown's to finish.
        svc._finish_taken_claim("j1")
        assert svc._claims["j1"] is claim
        claim.task.cancel.assert_not_called()

        svc._take_claim("j1")
        svc._finish_taken_claim("j1")
        assert "j1" not in svc._claims
        claim.task.cancel.assert_called_once()
        # Idempotent: a concurrent teardown that reached this step first left nothing.
        svc._finish_taken_claim("j1")

    def test_finish_does_not_cancel_a_task_that_already_ended(self) -> None:
        svc = _service()
        claim = svc._claim_run("j1", "scheduled")
        claim.task = MagicMock(done=MagicMock(return_value=True))
        svc._take_claim("j1")
        svc._finish_taken_claim("j1")
        assert "j1" not in svc._claims
        claim.task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_reaper_sweep_leaves_a_taken_claim_to_its_teardown(self) -> None:
        """A claim cancel() or an earlier reap took is theirs to finish.

        The teardown that took it is inside its kill awaits; a sweep that
        measured the run against its deadline anyway would reap it a second
        time and write a second terminal row. The same run, not yet taken, is
        reaped.
        """
        svc = _service()
        svc._sessions = MagicMock()
        overdue = time.time() - _JOB_TIMEOUT_SECS - 60
        claim = svc._claims["j1"] = _RunClaim(
            trigger="scheduled", claimed_at=overdue, task=_live_task()
        )

        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()
        reap.assert_awaited_once()
        assert reap.await_args is not None and reap.await_args.args[0] == "j1"
        assert (
            reap.await_args.kwargs.get("claim") is claim
        ), "the sweep did not hand _force_reap the claim it measured"

        assert svc._take_claim("j1") is claim
        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()
        reap.assert_not_awaited()
        assert svc._claims["j1"] is claim, "the sweep released a claim mid-teardown"


def _cancel_fixture(tmp_path: Any) -> tuple[CronService, CronJob, _RunClaim, MagicMock]:
    """A service with one live agent run, a history store and a mocked session."""
    svc = CronService(base_dir=None, on_job=AsyncMock())
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._sessions = MagicMock()
    svc._sessions.reset = AsyncMock()
    job = CronJob(
        id="j1",
        name="job",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
    )
    svc._jobs = [job]
    task = _live_task()
    claim = svc._claims["j1"] = _RunClaim(trigger="manual", claimed_at=time.time() - 5, task=task)
    return svc, job, claim, task


class TestASecondTeardownLeavesTheFirstAlone:
    """A taken claim is its taker's to finish; a later teardown must not pop it.

    ``cancel()`` and ``_force_reap`` take the claim, then await their kills,
    then pop it (``_finish_taken_claim``). A second ``cancel()`` (a
    double-clicked Cancel, or a Cancel during a live reap) that lands inside
    that await finds the claim already taken. Carrying on anyway would pop
    the FIRST teardown's claim early -- the job reads idle while its kill is
    still in flight -- write a second terminal row, and draw a generation for
    it that can outrank a replacement run's, so that run's own result is
    dropped as an older record.
    """

    @pytest.mark.asyncio
    async def test_a_second_cancel_inside_the_firsts_kill_await_answers_not_running(
        self, tmp_path: Any
    ) -> None:
        svc, job, claim, task = _cancel_fixture(tmp_path)
        parked = asyncio.Event()
        release = asyncio.Event()
        resets = 0

        async def _reset(*_args: Any, **_kwargs: Any) -> None:
            # Park only the FIRST teardown's kill; a later reset returns at once
            # so a cancel that wrongly carries on runs to its terminal write.
            nonlocal resets
            resets += 1
            if resets == 1:
                parked.set()
                await release.wait()

        svc._sessions.reset = AsyncMock(side_effect=_reset)

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            first = asyncio.get_running_loop().create_task(svc.cancel("j1"))
            await asyncio.wait_for(parked.wait(), timeout=5)
            assert claim.taken and svc._claims["j1"] is claim
            assert svc._cancelled_jobs.has("j1", claim)

            second = await svc.cancel("j1")

            assert (
                svc._claims.get("j1") is claim
            ), "the second cancel popped the first teardown's claim"
            assert second is False, "the second cancel answered True for a run another cancel owns"
            task.cancel.assert_not_called()
            _runs, total = await svc._history.get_job_history("j1")
            assert total == 0, "the second cancel wrote a cancelled row for a run it did not cancel"
            # Only the taker's reset is in flight; the second cancel killed nothing.
            assert resets == 1

            release.set()
            assert await asyncio.wait_for(first, timeout=5) is True

        assert "j1" not in svc._claims
        task.cancel.assert_called_once()
        runs, total = await svc._history.get_job_history("j1")
        assert total == 1, "two overlapping cancels of one run wrote two cancelled rows"
        assert runs[0]["status"] == "cancelled" and runs[0]["trigger"] == "manual"
        assert (job.last_error or "").startswith("Cancelled by user after")

    @pytest.mark.asyncio
    async def test_a_reap_landing_on_a_taken_claim_leaves_the_teardown_alone(
        self, tmp_path: Any
    ) -> None:
        """The reaper's own sweep skips a taken claim; ``_force_reap`` must too.

        The sweep re-reads the store and skips a taken claim before it calls
        the reap, so this is the reap's own fence: handed the very claim a
        ``cancel()`` took (the sweep's snapshot of it, measured overdue), it
        must take nothing, kill nothing and write nothing -- the claim is the
        cancel's to finish, and its row the only one owed.
        """
        svc, job, claim, task = _cancel_fixture(tmp_path)
        assert svc._take_claim("j1") is claim  # a cancel() inside its kill await
        svc._cancelled_jobs.mark("j1", claim)
        generation_before = svc._run_generations.get("j1", 0)

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("j1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert svc._claims.get("j1") is claim, "the reap popped the cancel's taken claim"
        task.cancel.assert_not_called()
        svc._sessions.reset.assert_not_awaited()
        assert not svc._reaped_jobs.has(
            "j1", claim
        ), "the reap marked a run cancel() already marked"
        assert job.last_status != "error" and not (job.last_error or "").startswith("Reaped")
        assert (
            svc._run_generations.get("j1", 0) == generation_before
        ), "the reap drew a generation for a terminal row it does not own"
        _runs, total = await svc._history.get_job_history("j1")
        assert total == 0, "the reap wrote a reaped row beside the cancel's cancelled row"

    def test_discard_finished_run_leaves_a_taken_claim_to_its_teardown(self) -> None:
        """A taken claim whose task has ended is not an orphan: its taker pops it.

        The run task ending is the expected effect of the teardown's kill, so
        a Run click or a second Cancel that calls ``discard_finished_run``
        between the task ending and the teardown resuming would otherwise pop
        the claim: the job reads idle, a replacement run is accepted, and the
        teardown's terminal row then outranks that run's generation.
        """
        svc = _service()
        claim = svc._claim_run("j1", "manual")
        claim.task = MagicMock(done=MagicMock(return_value=True))
        assert svc._take_claim("j1") is claim

        assert (
            svc.discard_finished_run("j1") is False
        ), "discard_finished_run popped a claim a teardown had taken"
        assert svc._claims["j1"] is claim
        assert svc.is_running("j1")

        # The teardown's own finish still pops it, task done or not.
        svc._finish_taken_claim("j1")
        assert "j1" not in svc._claims


_WEDGED = (
    "an aborted teardown left the claim taken: the run's fences fail, the sweep skips it "
    "and discard_finished_run refuses it, so the job reads running until restart "
    "(every manual run 409s, every scheduled fire is skipped)"
)


class TestAnAbortedTeardownStillFinishesItsClaim:
    """``taken`` is a lock; the teardown that took it must release it even when its kill fails.

    From the take onwards, by design, the run's own fences fail, the reaper
    sweep skips the claim and ``discard_finished_run`` refuses it: only the
    taker's ``_finish_taken_claim`` pops it. ``cancel()`` and ``_force_reap``
    run kill awaits between the take and that finish -- the executor hop to
    ``kill_running_process``, the session reset, ``_sigkill_session`` -- and a
    raise there (an executor shut down under the hop, the route's handler
    cancelled when the client disconnects) must not abandon the claim taken:
    the finish runs regardless, the terminal record still lands (naming the
    failure it hit), and the failure then propagates to the caller. The reap
    case injects its raise at the ``_sigkill_session`` seam: the real one
    swallows its own failures, so the injection stands in for any failure the
    reset's inner handlers let through.
    """

    @pytest.mark.asyncio
    async def test_a_cancel_whose_kill_raises_still_finishes_the_claim_it_took(
        self, tmp_path: Any
    ) -> None:
        svc, job, claim, task = _cancel_fixture(tmp_path)

        def _kill_raises(_job_id: str) -> bool:
            raise RuntimeError("cannot schedule new futures after shutdown")

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch.object(cron_script, "kill_running_process", _kill_raises),
        ):
            with pytest.raises(RuntimeError):
                await svc.cancel("j1")

        assert "j1" not in svc._claims, _WEDGED
        assert not svc.is_running("j1"), "the manual-run route would still 409 the job"
        task.cancel.assert_called_once()
        runs, total = await svc._history.get_job_history("j1")
        assert total == 1, "the aborted cancel wrote no terminal row for the run it tore down"
        assert runs[0]["status"] == "cancelled" and runs[0]["trigger"] == "manual"
        assert "RuntimeError" in runs[0]["error"], "the row does not name the failure the kill hit"
        assert job.last_status == "error"
        assert (job.last_error or "").startswith("Cancelled by user after")
        assert "RuntimeError" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_cancel_cancelled_inside_its_kill_await_still_finishes_the_claim(
        self, tmp_path: Any
    ) -> None:
        """The route's handler is cancelled when the client disconnects mid-cancel."""
        svc, job, claim, task = _cancel_fixture(tmp_path)
        parked = asyncio.Event()

        async def _reset(*_args: Any, **_kwargs: Any) -> None:
            parked.set()
            await asyncio.Event().wait()  # parked until the handler is cancelled

        svc._sessions.reset = AsyncMock(side_effect=_reset)

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            handler = asyncio.get_running_loop().create_task(svc.cancel("j1"))
            await asyncio.wait_for(parked.wait(), timeout=5)
            assert claim.taken and svc._claims["j1"] is claim
            handler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handler

        assert "j1" not in svc._claims, _WEDGED
        task.cancel.assert_called_once()
        runs, total = await svc._history.get_job_history("j1")
        assert total == 1, "the cancelled cancel wrote no terminal row for the run it tore down"
        assert runs[0]["status"] == "cancelled"
        assert "CancelledError" in runs[0]["error"]
        assert job.last_status == "error"

    @pytest.mark.asyncio
    async def test_a_reap_whose_kill_raises_still_finishes_the_claim_it_took(
        self, tmp_path: Any
    ) -> None:
        svc, job, claim, task = _cancel_fixture(tmp_path)
        svc._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))

        async def _sigkill_raises(
            _session_key: str, _handle: object = None, *, who: str = "Reaper"
        ) -> None:
            raise OSError("killpg refused")

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch.object(svc, "_sigkill_session", _sigkill_raises),
        ):
            with pytest.raises(OSError):
                await svc._force_reap("j1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "j1" not in svc._claims, _WEDGED
        assert not svc.is_running("j1"), "the manual-run route would still 409 the job"
        task.cancel.assert_called_once()
        runs, total = await svc._history.get_job_history("j1")
        assert total == 1, "the aborted reap wrote no terminal row for the run it tore down"
        assert runs[0]["status"] == "timeout"
        assert "OSError" in runs[0]["error"], "the row does not name the failure the kill hit"
        assert job.last_status == "error"
        assert (job.last_error or "").startswith("Reaped after")
        assert "OSError" in (job.last_error or "")


def _overdue_claim(trigger: str = "scheduled") -> _RunClaim:
    """A live run well past the reaper's deadline on both clocks."""
    return _RunClaim(
        trigger=trigger,
        claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
        started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
        jitter=0.0,
        task=_live_task(),
    )


def _two_job_fixture(tmp_path: Any) -> tuple[CronService, list[CronJob]]:
    """A service with two agent jobs, a history store and a mocked session."""
    svc = CronService(base_dir=None, on_job=AsyncMock())
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._sessions = MagicMock()
    svc._sessions.reset = AsyncMock()
    jobs = [
        CronJob(
            id=job_id,
            name=job_id,
            message="m",
            schedule=CronSchedule(kind="every", every_secs=300),
            created_ts=time.time(),
        )
        for job_id in ("j1", "j2")
    ]
    svc._jobs = jobs
    return svc, jobs


class TestTheSweepReapsOnlyTheClaimItMeasured:
    """The sweep measures a snapshot of the claims; a kill must follow the store.

    ``_reaper_loop`` iterates ``list(self._claims.items())`` and awaits inside
    ``_force_reap`` for each overdue job it meets (the session reset, the
    locked persist, the history append). While it awaits the first job's
    reap, a later job's run can end and a replacement claim the job. The
    snapshot's claim for that job then belongs to a run that is gone, and its
    stamps put the job far past its deadline -- so a sweep that measured the
    snapshot and took whatever claim the store held would force-kill a
    seconds-old run: session reset, task cancelled, ``last_status="error"``
    and a ``timeout`` row. The shape the claim replaced read the monotonic
    and jitter stamps live per iteration, so the replacement's fresh stamp
    made the sweep continue; the claim must give the same guarantee.
    """

    @pytest.mark.asyncio
    async def test_a_replacement_claimed_while_the_sweep_awaited_is_not_reaped(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc, (job_a, job_b) = _two_job_fixture(tmp_path)
        claim_a = svc._claims["j1"] = _overdue_claim()
        stale_b = svc._claims["j2"] = _overdue_claim()
        parked = asyncio.Event()
        release = asyncio.Event()
        reset_keys: list[str] = []

        async def _reset(session_key: str, **_kwargs: Any) -> None:
            # Park only the FIRST reap's kill (job A); a later reset returns at
            # once so a sweep that wrongly reaps job B runs to its terminal write.
            reset_keys.append(session_key)
            if len(reset_keys) == 1:
                parked.set()
                await release.wait()

        svc._sessions.reset = AsyncMock(side_effect=_reset)

        caplog.set_level(logging.WARNING, logger="kiro_crew.cron")
        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), _one_sweep():
            sweep = asyncio.get_running_loop().create_task(svc._reaper_loop())
            await asyncio.wait_for(parked.wait(), timeout=5)
            assert claim_a.taken and svc._claims["j1"] is claim_a
            # While job A's reap awaits its reset, job B's run ends on its own
            # and the due-scan claims the job again: a fresh run, seconds old.
            assert svc._release_claim("j2", stale_b) is True
            replacement = svc._claim_run("j2", "scheduled")
            replacement.started_monotonic = time.monotonic()
            replacement.jitter = 0.0
            replacement.task = _live_task()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(sweep, timeout=5)

        assert (
            svc._claims.get("j2") is replacement
        ), "the sweep reaped a replacement run it never measured"
        assert replacement.taken is False
        assert replacement.task is not None
        replacement.task.cancel.assert_not_called()
        assert reset_keys == ["cron:j1"], "the sweep reset a replacement run's session"
        assert not svc._reaped_jobs.has("j2", replacement)
        assert not svc._reaped_jobs.has("j2", stale_b)
        assert job_b.last_status != "error" and not (job_b.last_error or "").startswith("Reaped")
        _runs, total_b = await svc._history.get_job_history("j2")
        assert total_b == 0, "the sweep wrote a timeout row for a run seconds old"
        # The sweep itself never measured job B on the snapshot's stale stamps:
        # the identity re-read skips the entry before the deadline math, so
        # the "exceeded" warning is logged for job A alone.
        overdue = [r.getMessage() for r in caplog.records if "exceeded" in r.getMessage()]
        assert [m for m in overdue if "j2" in m] == [], "the sweep measured a stale snapshot"
        assert any("j1" in m for m in overdue)
        # Job A, the run the sweep did measure, was reaped exactly as before.
        assert "j1" not in svc._claims
        assert claim_a.task is not None
        claim_a.task.cancel.assert_called_once()
        assert (job_a.last_error or "").startswith("Reaped after")
        runs_a, total_a = await svc._history.get_job_history("j1")
        assert total_a == 1 and runs_a[0]["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_force_reap_takes_the_claim_it_was_handed_or_nothing(self, tmp_path: Any) -> None:
        """A reap named for a run that is gone leaves the job's run alone."""
        svc, (_job_a, job_b) = _two_job_fixture(tmp_path)
        stale = _overdue_claim()
        replacement = svc._claims["j2"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - 2,
            started_monotonic=time.monotonic() - 2,
            task=_live_task(),
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("j2", _JOB_TIMEOUT_SECS + 60, claim=stale)

        assert (
            svc._claims.get("j2") is replacement and replacement.taken is False
        ), "the reap took a claim it was not handed"
        assert replacement.task is not None
        replacement.task.cancel.assert_not_called()
        svc._sessions.reset.assert_not_awaited()
        assert not svc._reaped_jobs.has("j2", replacement)
        assert job_b.last_status != "error"
        _runs, total = await svc._history.get_job_history("j2")
        assert total == 0

        # The measured run released its claim and nothing replaced it: there
        # is no run to kill, and the job's session is not that run's any more.
        del svc._claims["j2"]
        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("j2", _JOB_TIMEOUT_SECS + 60, claim=stale)
        svc._sessions.reset.assert_not_awaited()
        _runs, total = await svc._history.get_job_history("j2")
        assert total == 0

        # Handed the stored claim itself, the reap proceeds as before.
        live = svc._claims["j2"] = _overdue_claim()
        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("j2", _JOB_TIMEOUT_SECS + 60, claim=live)
        assert "j2" not in svc._claims
        assert live.task is not None
        live.task.cancel.assert_called_once()
        svc._sessions.reset.assert_awaited_once()
        assert (job_b.last_error or "").startswith("Reaped after")
        runs, total = await svc._history.get_job_history("j2")
        assert total == 1 and runs[0]["status"] == "timeout"

    def test_take_claim_with_an_expected_claim_takes_only_that_object(self) -> None:
        svc = _service()
        stored = svc._claim_run("j1", "scheduled")
        lookalike = _RunClaim(
            trigger=stored.trigger, claimed_at=stored.claimed_at, marker_run=stored.marker_run
        )

        assert svc._take_claim("j1", expected=lookalike) is None
        assert stored.taken is False, "a take for another run took the stored claim"
        assert svc._take_claim("other", expected=stored) is None

        assert svc._take_claim("j1", expected=stored) is stored
        assert stored.taken is True
        # Taken once: the same expected claim is not taken a second time.
        assert svc._take_claim("j1", expected=stored) is None


class TestTaskTracking:
    def test_attach_run_task_fills_only_an_empty_slot(self) -> None:
        svc = _service()
        claim = svc._claim_run("j1", "manual")
        wrapper = _live_task()
        svc.attach_run_task("j1", wrapper)
        assert claim.task is wrapper

        other = _live_task()
        svc.attach_run_task("j1", other)
        assert claim.task is wrapper, "a live run's task handle was overwritten"

        svc.attach_run_task("unclaimed", other)  # no claim: nothing to track
        assert "unclaimed" not in svc._claims

    @pytest.mark.asyncio
    async def test_discard_finished_run_releases_only_a_done_task(self) -> None:
        svc = _service()
        claim = svc._claim_run("j1", "scheduled")
        assert svc.discard_finished_run("j1") is False  # no task tracked yet

        gate = asyncio.Event()

        async def _run() -> None:
            await gate.wait()

        claim.task = asyncio.get_running_loop().create_task(_run())
        assert svc.discard_finished_run("j1") is False
        assert svc._claims["j1"] is claim

        gate.set()
        await claim.task
        assert svc.discard_finished_run("j1") is True
        assert "j1" not in svc._claims
        assert svc.discard_finished_run("j1") is False

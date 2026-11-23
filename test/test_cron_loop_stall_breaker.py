"""The cron loop-stall breaker and the in-flight markers behind it.

A gateway the loop-stall watchdog hard-exits runs no ``finally``: the run in
flight writes no ``last_run_ts``, so on the next boot the job is due again,
fires again and stalls again -- an hourly crash loop in the field. These tests
pin the two halves that end it: the run task leaves a marker on disk while it
executes and clears it on every ``finally`` path, and ``CronService.start()``
pauses the one job a cron-surface dump plus a single abandoned marker name,
BEFORE the timer is armed, once per dump.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import time
from pathlib import Path

import pytest
from stall_dump_helpers import CHAT_STACK, CRON_STACK, write_dump

from kiro_crew import cron as cron_module
from kiro_crew import cron_inflight
from kiro_crew.cron import CronJob, CronService, CronStoreUnreadable
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
from kiro_crew.cron_service.execution import interruption_note
from kiro_crew.dashboard import crash_dump_store
from kiro_crew.stall_attribution import attribute_dump, describe
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.asyncio


@pytest.fixture
def dead_pids(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    alive: set[int] = set()
    monkeypatch.setattr(crash_dump_store, "pid_exists", lambda pid: pid in alive)
    return alive


def _abandoned_marker(
    base: Path,
    job_id: str,
    name: str,
    pid: int,
    started_at: float,
    run: str | None = None,
    phase: str | None = None,
) -> None:
    d = cron_inflight.running_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "job_id": job_id,
        "name": name,
        "started_at": started_at,
        "pid": pid,
        "pid_domain": crash_dump_store._pid_domain(),
    }
    if phase is not None:
        payload["phase"] = phase
    (d / (f"{job_id}.json" if run is None else f"{job_id}.{run}.json")).write_text(
        json.dumps(payload), encoding="utf-8"
    )


async def _started_service(base: Path, dumps: Path) -> CronService:
    svc = await CronService.create(base_dir=base)
    svc._dumps_dir = dumps
    await svc.start()
    return svc


class TestBreaker:
    async def test_pauses_the_attributed_job_before_the_timer_arms(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(
            name="twb-refresh", message="refresh", every_secs=3600, strict_schedule=True
        )
        now = time.time()
        # The run the watchdog killed never wrote its last_run_ts, so the store
        # still says the job last ran two periods ago: OVERDUE, and strict, so
        # the timer would dispatch it at zero delay on the first tick.
        job.last_run_ts = now - 7200
        seed._save()
        write_dump(tmp_path / "dumps", 4_100_001, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_001, now - 60)

        fired: list[str] = []
        svc = await CronService.create(base_dir=tmp_path, on_job=lambda j: _record(fired, j))
        svc._dumps_dir = tmp_path / "dumps"
        await svc.start()
        try:
            # Let a timer tick happen: an unpaused overdue `every` job would fire.
            await asyncio.sleep(0.2)
            assert (
                fired == []
            ), "the overdue job fired: the breaker ran after the timer, or not at all"
            paused = svc.get_job(job.id)
            assert paused is not None
            assert paused.auto_paused is True and paused.enabled is False
            assert paused.last_status == "error"
            assert paused.last_error is not None
            assert "loop-stall watchdog" in paused.last_error
            assert f"kirocrew cron resume {job.id}" in paused.last_error
            # Persisted, so a reload sees the pause.
            raw = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))
            (rec,) = [r for r in raw["jobs"] if r["id"] == job.id]
            assert rec["auto_paused"] is True
            # The marker was consumed -- the breaker acted on it, and the verdict
            # now lives in last_error -- and the dump is claimed.
            assert cron_inflight.read_markers(tmp_path) == []
            assert cron_inflight.read_claim(tmp_path).startswith("loopstall-")
        finally:
            await svc.stop()

    async def test_second_boot_does_not_re_pause_a_resumed_job(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="hourly", message="m", every_secs=3600)
        now = time.time()
        write_dump(tmp_path / "dumps", 4_100_002, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_002, now - 60)
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        await svc.stop()
        assert svc.get_job(job.id).auto_paused is True  # type: ignore[union-attr]
        # Operator resumes; the dump is still on disk for a week.
        resumed = CronService(base_dir=tmp_path)
        assert resumed.enable_job(job.id, True) is True
        again = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            j = again.get_job(job.id)
            assert j is not None and j.auto_paused is False and j.enabled is True
        finally:
            await again.stop()

    async def test_non_cron_dump_pauses_nothing(self, tmp_path: Path, dead_pids: set[int]) -> None:
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="bystander", message="m", every_secs=3600)
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_100_003, CHAT_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_003, now - 60)
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            j = svc.get_job(job.id)
            assert j is not None and j.auto_paused is False and j.enabled is True
            # Swept, but the record keeps the bystander for the doctor -- and the
            # chat surface still implicates no job.
            assert cron_inflight.read_markers(tmp_path) == []
            later = attribute_dump(dump, tmp_path)
            assert later.is_cron is False
            assert [m.job_id for m in later.candidates] == [job.id]
        finally:
            await svc.stop()

    async def test_a_host_wide_pause_pauses_nothing(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """The alarm fired 65 s after it was due: the host stopped, not the job."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="hourly", message="m", every_secs=3600)
        fired = 1_762_540_000  # any instant: the breaker does not age dumps
        write_dump(tmp_path / "dumps", 4_100_009, CRON_STACK, mtime=fired, alarm_due=fired - 65)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_009, fired - 60)
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            j = svc.get_job(job.id)
            assert j is not None and j.auto_paused is False and j.enabled is True
        finally:
            await svc.stop()

    async def test_two_candidates_pause_nothing(self, tmp_path: Path, dead_pids: set[int]) -> None:
        seed = CronService(base_dir=tmp_path)
        a = seed.add_job(name="a", message="m", every_secs=3600)
        b = seed.add_job(name="b", message="m", every_secs=3600)
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_100_004, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, a.id, a.name, 4_100_004, now - 60)
        _abandoned_marker(tmp_path, b.id, b.name, 4_100_004, now - 50)
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            for jid in (a.id, b.id):
                j = svc.get_job(jid)
                assert j is not None and j.auto_paused is False
            # The markers are swept, but the verdict the breaker declined to act
            # on is what the operator needs: the doctor, reading the same dump
            # later, must still name both candidates.
            assert cron_inflight.read_markers(tmp_path) == []
            later = attribute_dump(dump, tmp_path)
            assert later.job is None
            assert sorted(m.job_id for m in later.candidates) == sorted([a.id, b.id])
            assert "2 jobs were in flight" in "\n".join(describe(later))
        finally:
            await svc.stop()

    async def test_a_run_in_its_jitter_wait_is_not_a_second_candidate(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A run still sleeping out its jitter has run nothing, so it cannot
        have stalled the loop: the stalled job is still the one paused, and the
        waiting run is filed as one that never started."""
        seed = CronService(base_dir=tmp_path)
        stalled = seed.add_job(name="stalled", message="m", every_secs=3600)
        waiting = seed.add_job(name="waiting", message="m", cron_expr="0 9 * * *", timezone="UTC")
        now = time.time()
        write_dump(tmp_path / "dumps", 4_100_015, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, stalled.id, stalled.name, 4_100_015, now - 60)
        _abandoned_marker(
            tmp_path, waiting.id, waiting.name, 4_100_015, now - 50, run="waitrun", phase="jitter"
        )
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            assert svc.get_job(stalled.id).auto_paused is True  # type: ignore[union-attr]
            assert svc.get_job(waiting.id).auto_paused is False  # type: ignore[union-attr]
            rows, total = await svc._history.get_job_history(waiting.id)
            assert total == 1, "the run cut in its jitter wait left no history row"
            assert (rows[0]["status"], rows[0]["error"]) == (
                "cancelled",
                interruption_note(dispatched=False),
            )
            assert cron_inflight.abandoned_markers(tmp_path) == []
        finally:
            await svc.stop()

    async def test_a_marker_no_dump_explains_is_swept(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """The general cleanup: with no dump left there is nothing to attribute a
        marker to, so keeping it would only make `doctor` report evidence that
        names no crash."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="stale", message="m", every_secs=3600)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_009, time.time() - 600)
        svc = await _started_service(tmp_path, tmp_path / "no-dumps")
        try:
            assert cron_inflight.read_markers(tmp_path) == []
        finally:
            await svc.stop()

    async def test_a_run_no_dump_explains_is_filed_before_its_marker_goes(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A crash, a kill or a power loss leaves no dump, and the cut run ran
        no ``finally``: its marker is all that says it ran. The boot files it as
        ``stop()`` files a run it cut -- the job waits for its next slot, a
        cancelled row, the interrupt notice -- and only then removes the marker."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="hard-cut", message="m", every_secs=3600)
        started_at = time.time() - 600
        # The previous run's stamp: overdue now, so left as it is the timer
        # would run the job again at once, against what the notice says.
        job.last_run_ts = started_at - 3600
        seed._save()
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_011, started_at)
        notices: list[tuple[str, str]] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append((j.id, detail))

        svc = await CronService.create(base_dir=tmp_path)
        svc._dumps_dir = tmp_path / "no-dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        try:
            rows, total = await svc._history.get_job_history(job.id)
            assert total == 1, "the cut run left no history row"
            note = interruption_note(dispatched=True)
            assert (rows[0]["status"], rows[0]["started_at"], rows[0]["error"]) == (
                "cancelled",
                started_at,
                note,
            )
            assert cron_inflight.read_markers(tmp_path) == []
            filed = CronService(base_dir=tmp_path).get_job(job.id)
            assert filed is not None and filed.last_run_ts == started_at
        finally:
            await svc.stop()
        assert notices == [(job.id, note)]

    async def test_a_run_cut_in_its_jitter_wait_is_filed_as_never_started(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A hard exit in the jitter wait leaves the marker written before the
        wait. The run executed nothing, so its row and notice say it never
        started, as a graceful stop at the same point says."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="daily", message="m", cron_expr="0 9 * * *", timezone="UTC")
        claimed_at = time.time() - 600
        _abandoned_marker(
            tmp_path, job.id, job.name, 4_100_014, claimed_at, run="waitrun", phase="jitter"
        )
        notices: list[tuple[str, str]] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append((j.id, detail))

        svc = await CronService.create(base_dir=tmp_path)
        svc._dumps_dir = tmp_path / "no-dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        try:
            rows, total = await svc._history.get_job_history(job.id)
            assert total == 1, "the run cut in its jitter wait left no history row"
            note = interruption_note(dispatched=False)
            assert (rows[0]["status"], rows[0]["started_at"], rows[0]["error"]) == (
                "cancelled",
                claimed_at,
                note,
            )
            assert cron_inflight.abandoned_markers(tmp_path) == []
        finally:
            await svc.stop()
        assert notices == [(job.id, note)]

    async def test_a_run_cancel_already_filed_is_not_filed_again(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """``cancel()`` and the reaper file a run under its marker token before
        its task clears the marker, so a process that ends in between leaves a
        marker for a run that already has its row. The row, not a timestamp,
        says so: the job's ``last_run_ts`` is still the claim's, before the
        marker's start."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="cancelled", message="m", every_secs=3600)
        started_at = time.time() - 600
        job.last_run_ts = started_at - 5
        seed._save()
        filed = CronRunRecord(
            run_id="cancelledrun",
            job_id=job.id,
            started_at=started_at - 5,
            finished_at=started_at + 30,
            status="cancelled",
        )
        assert seed._history.append_sync(filed)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_013, started_at, run="cancelledrun")
        notices: list[str] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append(j.id)

        svc = await CronService.create(base_dir=tmp_path)
        svc._dumps_dir = tmp_path / "no-dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        try:
            rows, total = await svc._history.get_job_history(job.id)
            assert total == 1 and rows[0]["run_id"] == "cancelledrun"
            assert cron_inflight.read_markers(tmp_path) == []
        finally:
            await svc.stop()
        assert notices == []

    async def test_a_cut_run_is_filed_though_a_later_run_stamped_the_job(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A marker kept by a failed row outlives the job's later runs, so the
        later ``last_run_ts`` says nothing about the cut run: it is filed under
        its own token, and the later run's stamp stays."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="later-run", message="m", every_secs=3600)
        started_at = time.time() - 600
        job.last_run_ts = started_at + 300
        seed._save()
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_014, started_at, run="cutrun")
        notices: list[str] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append(j.id)

        svc = await CronService.create(base_dir=tmp_path)
        svc._dumps_dir = tmp_path / "no-dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        try:
            rows, total = await svc._history.get_job_history(job.id)
            assert total == 1
            assert (rows[0]["run_id"], rows[0]["status"], rows[0]["started_at"]) == (
                "cutrun",
                "cancelled",
                started_at,
            )
            assert cron_inflight.read_markers(tmp_path) == []
            reloaded = CronService(base_dir=tmp_path).get_job(job.id)
            assert reloaded is not None and reloaded.last_run_ts == started_at + 300
        finally:
            await svc.stop()
        assert notices == [job.id]

    async def test_a_cut_run_whose_history_cannot_be_read_keeps_its_marker(
        self, tmp_path: Path, dead_pids: set[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unread history file is not one without the run's row: the marker
        stays for the next boot, and nothing is filed or sent."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="unreadable", message="m", every_secs=3600)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_015, time.time() - 600, run="unreadrun")
        monkeypatch.setattr(
            CronHistoryStore,
            "_get_run_detail_sync",
            lambda self, job_id, run_id: (_ for _ in ()).throw(OSError(errno.EIO, "io")),
        )
        notices: list[str] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append(j.id)

        svc = await CronService.create(base_dir=tmp_path)
        svc._dumps_dir = tmp_path / "no-dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        try:
            assert (await svc._history.get_job_history(job.id))[1] == 0
        finally:
            await svc.stop()
        assert [m.run_token for m in cron_inflight.read_markers(tmp_path)] == ["unreadrun"]
        assert notices == []

    @pytest.mark.parametrize("cause", ["disk-full", "history-denied", "store-quarantined"])
    async def test_a_cut_run_whose_row_is_not_written_keeps_its_marker(
        self, tmp_path: Path, dead_pids: set[int], monkeypatch: pytest.MonkeyPatch, cause: str
    ) -> None:
        """The marker is the only evidence of the cut run, so it stays for the
        next boot until a row has been written, and no notice goes out first:
        also when a denial turned history off, or the store was moved aside."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="full-disk", message="m", every_secs=3600)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_012, time.time() - 600)
        if cause == "disk-full":
            monkeypatch.setattr(
                CronHistoryStore,
                "_append_sync",
                lambda self, record: (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")),
            )
        elif cause == "history-denied":
            # ``start()``'s ``rotate_all()`` degrades the store before the breaker runs.
            monkeypatch.setattr(
                CronHistoryStore,
                "_rotate_all_sync",
                lambda self: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")),
            )
        else:
            seed._path.write_bytes(b"{")
        notices: list[str] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append(j.id)

        quarantine = cause == "store-quarantined"
        svc = await CronService.create(base_dir=tmp_path, quarantine_unreadable=quarantine)
        svc._dumps_dir = tmp_path / "no-dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        await svc.stop()
        assert (svc.quarantined_store is not None) is quarantine
        assert svc._history.enabled is (cause != "history-denied")
        assert [m.job_id for m in cron_inflight.read_markers(tmp_path)] == [job.id]
        assert notices == []

    @pytest.mark.parametrize("settled", [True, False], ids=["claimed-dump", "fresh-dump"])
    async def test_a_run_cut_after_an_older_dump_is_filed(
        self, tmp_path: Path, dead_pids: set[int], settled: bool
    ) -> None:
        """A dump stays on disk long after its stall, so a later hard exit that
        left no dump of its own still finds one; the run that exit cut is filed."""
        seed = CronService(base_dir=tmp_path)
        cut = seed.add_job(name="cut", message="m", every_secs=3600)
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_100_016, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, cut.id, cut.name, 4_100_017, now - 10, run="laterexit")
        if settled:
            assert cron_inflight.write_claim(tmp_path, dump.name)
        else:
            stalled = seed.add_job(name="stalled", message="m", every_secs=3600)
            _abandoned_marker(tmp_path, stalled.id, stalled.name, 4_100_016, now - 60)
        notices: list[str] = []

        async def notice(j: CronJob, detail: str) -> None:
            notices.append(j.id)

        svc = await CronService.create(base_dir=tmp_path)
        svc._dumps_dir = tmp_path / "dumps"
        svc.set_interrupt_alert(notice)
        await svc.start()
        try:
            rows, total = await svc._history.get_job_history(cut.id)
            assert total == 1, "the run the later exit cut left no history row"
            assert (rows[0]["run_id"], rows[0]["status"], rows[0]["started_at"]) == (
                "laterexit",
                "cancelled",
                now - 10,
            )
            if not settled:
                j = svc.get_job(stalled.id)
                assert j is not None and j.auto_paused is True
                assert (await svc._history.get_job_history(stalled.id))[1] == 0
            assert cron_inflight.read_markers(tmp_path) == []
        finally:
            await svc.stop()
        assert notices == [cut.id]

    async def test_an_unpersisted_pause_is_retried_on_the_next_boot(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A store the breaker could not write leaves the job enabled and still
        due, so neither the claim nor the marker may be consumed -- otherwise the
        boot that CAN write the store skips the job and re-runs the crash."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="unwritable", message="m", every_secs=3600)
        now = time.time()
        write_dump(tmp_path / "dumps", 4_100_010, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_010, now - 60)

        def refuse(self: CronService) -> None:
            raise CronStoreUnreadable("store held by another writer")

        # A scoped patch, not ``monkeypatch.setattr`` + ``monkeypatch.undo()``:
        # ``undo()`` reverts EVERY patch on the fixture, including ``dead_pids``'
        # fake of ``pid_exists`` -- after which the second boot below probed the
        # marker's synthetic pid against the real host (``os.kill(4_100_010, 0)``)
        # and the verdict depended on that number being free on this machine.
        with pytest.MonkeyPatch.context() as store_patch:
            store_patch.setattr(CronService, "_save", refuse)
            svc = await _started_service(tmp_path, tmp_path / "dumps")
            await svc.stop()
        assert cron_inflight.read_claim(tmp_path) == ""
        assert [m.job_id for m in cron_inflight.read_markers(tmp_path)] == [job.id]
        # A repaired boot reaches the same verdict and pauses.
        again = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            j = again.get_job(job.id)
            assert j is not None and j.auto_paused is True and j.enabled is False
        finally:
            await again.stop()

    async def test_a_breaker_failure_does_not_stop_the_scheduler(
        self, tmp_path: Path, dead_pids: set[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The breaker is a safety net that runs before the timer arms, so a
        fault in it must cost the net and not the scheduler."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="fine", message="m", every_secs=3600)
        monkeypatch.setattr(
            CronService,
            "_loop_stall_breaker_verdict",
            lambda self: (_ for _ in ()).throw(RuntimeError("breaker exploded")),
        )
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            assert svc._running is True
            j = svc.get_job(job.id)
            assert j is not None and j.enabled is True
        finally:
            await svc.stop()

    async def test_no_dump_is_a_no_op(self, tmp_path: Path, dead_pids: set[int]) -> None:
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="fine", message="m", every_secs=3600)
        svc = await _started_service(tmp_path, tmp_path / "no-dumps")
        try:
            j = svc.get_job(job.id)
            assert j is not None and j.enabled is True
        finally:
            await svc.stop()

    async def test_breaker_audits_the_pause(
        self, tmp_path: Path, dead_pids: set[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from kiro_crew import sel as sel_mod

        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="audited", message="m", every_secs=3600)
        now = time.time()
        write_dump(tmp_path / "dumps", 4_100_005, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_005, now - 60)
        stub = MagicMock()
        monkeypatch.setattr(sel_mod, "sel", lambda: stub)
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        await svc.stop()
        outcomes = [
            c.kwargs.get("outcome")
            for c in stub.log_tool_invocation.call_args_list
            if c.kwargs.get("tool_kind") == "cron_auto_pause"
        ]
        assert outcomes == ["auto_paused_loop_stall"]

    async def test_a_lost_claim_cannot_re_pause_a_resumed_job(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """The pause persisted but the claim file did not: the markers must stay
        (nothing else says the dump was handled), and once the operator resumes
        the job the next boot must NOT pause it again -- the store's own
        ``last_error`` names the dump and settles it."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="claimless", message="m", every_secs=3600)
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_100_007, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_007, now - 60)
        # Scoped for the same reason as the unwritable-store test: an ``undo()``
        # here would also drop ``dead_pids``' fake of ``pid_exists`` for the
        # second boot below.
        with pytest.MonkeyPatch.context() as claim_patch:
            claim_patch.setattr(cron_inflight, "write_claim", lambda base, name: False)
            svc = await _started_service(tmp_path, tmp_path / "dumps")
            await svc.stop()
        j = svc.get_job(job.id)
        assert j is not None and j.auto_paused is True and dump.name in (j.last_error or "")
        # No claim, so the evidence is retained for the next boot.
        assert [m.job_id for m in cron_inflight.read_markers(tmp_path)] == [job.id]
        assert cron_inflight.read_claim(tmp_path) == ""

        assert CronService(base_dir=tmp_path).enable_job(job.id, True) is True
        again = await _started_service(tmp_path, tmp_path / "dumps")
        try:
            j2 = again.get_job(job.id)
            assert j2 is not None and j2.enabled is True and j2.auto_paused is False
            # This boot could claim, so the dump is settled and the markers go.
            assert cron_inflight.read_claim(tmp_path) == dump.name
            assert cron_inflight.read_markers(tmp_path) == []
        finally:
            await again.stop()

    async def test_an_unrecorded_attribution_is_not_claimed(
        self, tmp_path: Path, dead_pids: set[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record could not be written: no claim may land either. A claim
        without a record would make the next boot treat the dump as settled
        while the markers -- the only remaining copy of the evidence -- are
        swept; both must stay for the boot that can record."""
        seed = CronService(base_dir=tmp_path)
        job = seed.add_job(name="unrecorded", message="m", every_secs=3600)
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_100_009, CRON_STACK, mtime=now - 30)
        _abandoned_marker(tmp_path, job.id, job.name, 4_100_009, now - 60)
        monkeypatch.setattr(cron_inflight, "record_attribution", lambda *a, **k: False)
        svc = await _started_service(tmp_path, tmp_path / "dumps")
        await svc.stop()
        j = svc.get_job(job.id)
        assert j is not None and j.auto_paused is True and dump.name in (j.last_error or "")
        assert cron_inflight.read_claim(tmp_path) == ""
        assert [m.job_id for m in cron_inflight.read_markers(tmp_path)] == [job.id]


async def _record(sink: list[str], job: CronJob) -> str | None:
    sink.append(job.id)
    return None


class TestRunMarker:
    async def test_marker_exists_during_the_run_and_is_gone_after(self, tmp_path: Path) -> None:
        seen: list[list[cron_inflight.RunningMarker]] = []

        async def on_job(job: CronJob) -> str | None:
            seen.append(cron_inflight.read_markers(tmp_path))
            return None

        svc = await CronService.create(base_dir=tmp_path, on_job=on_job)
        job = svc.add_job(name="probe", message="m", every_secs=3600, strict_schedule=True)
        svc._running = True
        await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
        assert len(seen) == 1
        (marker,) = seen[0]
        assert marker.job_id == job.id and marker.name == "probe" and marker.pid == os.getpid()
        assert cron_inflight.read_markers(tmp_path) == []

    async def test_a_run_in_its_jitter_wait_is_on_disk_before_the_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hard exit in the jitter wait runs no ``finally``, so the claimed run
        is on disk before the wait begins, under its claim time, as a run that
        has not started; it reads as running only once it executes."""
        on_disk: list[list[dict[str, object]]] = []
        executing: list[list[cron_inflight.RunningMarker]] = []

        async def wait_out(_seconds: float) -> None:
            on_disk.append(
                [
                    json.loads(p.read_text(encoding="utf-8"))
                    for p in cron_inflight.running_dir(tmp_path).glob("*.json")
                ]
            )
            executing.append(cron_inflight.read_markers(tmp_path))

        async def on_job(job: CronJob) -> str | None:
            executing.append(cron_inflight.read_markers(tmp_path))
            return None

        monkeypatch.setattr(cron_module, "_sleep_out_jitter", wait_out)
        svc = await CronService.create(base_dir=tmp_path, on_job=on_job)
        job = svc.add_job(name="daily", message="m", cron_expr="0 9 * * *", timezone="UTC")
        monkeypatch.setattr(svc, "_compute_jitter", lambda _job: 30.0)
        svc._running = True
        claim = svc._claim_run(job.id, "scheduled")
        await svc._run_job_isolated(job, claim)
        assert [len(markers) for markers in on_disk] == [1], "no marker before the jitter wait"
        ((waiting,),) = on_disk
        assert (waiting["job_id"], waiting["phase"], waiting["started_at"]) == (
            job.id,
            "jitter",
            claim.claimed_at,
        )
        in_wait, in_run = executing
        assert in_wait == []
        assert [(m.job_id, m.phase) for m in in_run] == [(job.id, "running")]
        assert list(cron_inflight.running_dir(tmp_path).glob("*.json")) == []

    async def test_marker_is_cleared_when_the_callback_raises(self, tmp_path: Path) -> None:
        async def on_job(job: CronJob) -> str | None:
            raise RuntimeError("boom")

        svc = await CronService.create(base_dir=tmp_path, on_job=on_job)
        job = svc.add_job(name="raiser", message="m", every_secs=3600, strict_schedule=True)
        svc._running = True
        await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
        assert cron_inflight.read_markers(tmp_path) == []
        assert svc.get_job(job.id).last_status == "error"  # type: ignore[union-attr]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/FIFO shapes")
class TestMarkerIoRefusesWhatItDidNotWrite:
    """The markers are evidence an automatic pause rests on, so this module reads
    only plain files it could have written and never follows a link out.

    The fence on ``cron-running`` is the control that keeps an agent out of the
    directory; these are the second half, for a leaf planted before the fence
    existed or by something outside the gate. Async only so the module's
    ``asyncio_mode`` applies uniformly.
    """

    async def test_a_symlinked_marker_is_not_followed(self, tmp_path: Path) -> None:
        secret = tmp_path / "elsewhere.json"
        secret.write_text(
            json.dumps({"job_id": "deadbeef", "name": "forged", "started_at": 1.0, "pid": 4242}),
            encoding="utf-8",
        )
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / "deadbeef.json").symlink_to(secret)
        assert cron_inflight.read_markers(tmp_path) == []

    async def test_a_fifo_marker_is_refused_rather_than_blocking(self, tmp_path: Path) -> None:
        # A `read_text` here blocks until a writer arrives -- on the worker the
        # cron service awaits inside start(), so it would hang the scheduler
        # instead of arming it. The test would time out rather than fail.
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        os.mkfifo(d / "cafebabe.json")
        assert (
            await finished(
                asyncio.to_thread(cron_inflight.read_markers, tmp_path),
                "read_markers returning past a FIFO marker",
            )
            == []
        )

    async def test_a_symlinked_claim_is_not_followed(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "claimed.txt"
        elsewhere.write_text("loopstall-20260101T000000Z.txt\n", encoding="utf-8")
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        cron_inflight.claim_path(tmp_path).symlink_to(elsewhere)
        assert cron_inflight.read_claim(tmp_path) == ""

    async def test_a_redirecting_parent_link_refuses_the_write(self, tmp_path: Path) -> None:
        # The write must not land the payload wherever the link points: a
        # pre-planted parent link is how a marker write becomes a keystone write.
        target = tmp_path / "attacker"
        target.mkdir()
        cron_inflight.running_dir(tmp_path).symlink_to(target, target_is_directory=True)
        cron_inflight.write_marker(tmp_path, "0badc0de", "victim", run="aaaa")
        cron_inflight.write_claim(tmp_path, "loopstall-20260101T000000Z.txt")
        assert list(target.iterdir()) == []

    async def test_an_oversized_marker_is_refused(self, tmp_path: Path) -> None:
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": "0f0f0f0f",
            "name": "x" * (cron_inflight._MARKER_MAX_BYTES + 64),
            "started_at": 1.0,
            "pid": 7,
        }
        (d / "0f0f0f0f.json").write_text(json.dumps(payload), encoding="utf-8")
        assert cron_inflight.read_markers(tmp_path) == []

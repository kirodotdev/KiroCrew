"""Retention: an opt-in per-kind cap bounds how many finished records a kind keeps.

A kind registered with ``retention=N`` keeps the run that just finished plus the
newest ``N - 1`` other terminal records, and removes the rest after each terminal
write. A kind that never opts in keeps every record, as before the cap existed.

Runs execute on real daemon threads, so state is awaited with a bounded poll, never
a sleep. The waits count record files by name instead of reading them: on Windows a
reader holding a record open makes the sweep's delete of that record fail. The clock
is never patched: where order matters, a test plants the older records with stamps of
its own, which sort apart from the real one a finished run carries.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    _wait_until,
    sdk_fixture,
)

from conftest import requires_symlinks
from kiro_crew.apps import job_routes, job_sdk
from kiro_crew.apps.builtins.aws_control import hooks
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod
from kiro_crew.apps.job_sdk import DONE, RUNNING, JobRun, JobSDK, forget_sdk
from kiro_crew.apps.manager import app_data_dir

ACCOUNT = "111122223333"


#: Stamps for planted records: one before any real run, one after.
PAST = "2000-01-01T00:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"


def _file_count(sdk: JobSDK) -> int:
    """Record files in the store, counted by name without opening any."""
    if not sdk.store.dir.is_dir():
        return 0
    return len(list(sdk.store.dir.glob("*.json")))


def _ids_on_disk(sdk: JobSDK) -> set[str]:
    return {p.stem for p in sdk.store.dir.glob("*.json")}


def _swept(sdk: JobSDK, run_id: str) -> bool:
    """Whether ``run_id`` has left the live table and its own sweep has ended."""
    return run_id not in sdk.cancelling_and_live_ids()[1] and run_id not in sdk._unswept


def _finish(sdk: JobSDK, kind: str) -> str:
    """Start a run of a registered kind and wait until its own sweep has ended.

    Waiting for the sweep, not only the terminal record, keeps the sweeps in the
    order the runs finished, which the tests below read their survivors from.
    """
    run_id = sdk.start(kind)
    _wait_terminal(sdk, run_id)
    _wait_until(lambda: _swept(sdk, run_id))
    return run_id


def _settle(sdk: JobSDK, count: int) -> None:
    """Wait for the last sweep: it runs on the worker after the terminal write."""
    _wait_until(
        lambda: _file_count(sdk) == count
        and not sdk.cancelling_and_live_ids()[1]
        and not sdk._unswept  # noqa: SLF001 - every sweep has ended
    )


def _plant(
    sdk: JobSDK, run_id: str, kind: str, status: str = DONE, stamp: str = PAST, **fields: str
) -> None:
    """Write a record an earlier run left, created and (if terminal) finished at ``stamp``."""
    run = JobRun(run_id=run_id, app=sdk.app_name, kind=kind, status=status, created_at=stamp)
    if run.is_terminal:
        run.finished_at = stamp
    for name, value in fields.items():
        setattr(run, name, value)
    sdk.store.write(run)


class TestTheCap:
    def test_a_kind_without_retention_keeps_every_record(self, sdk: JobSDK) -> None:
        sdk.register("plain", lambda h: None)
        finished = {_finish(sdk, "plain") for _ in range(5)}
        _settle(sdk, 5)
        assert _ids_on_disk(sdk) == finished

    def test_the_store_settles_at_the_cap_and_keeps_the_newest(self, sdk: JobSDK) -> None:
        sdk.register("capped", lambda h: None, retention=3)
        for n, run_id in enumerate(("1" * 32, "2" * 32, "3" * 32, "4" * 32)):
            _plant(sdk, run_id, "capped", stamp=f"2000-01-0{n + 1}T00:00:00Z")
        newest = _finish(sdk, "capped")
        _settle(sdk, 3)
        assert _ids_on_disk(sdk) == {newest, "3" * 32, "4" * 32}

    def test_a_cap_on_one_kind_leaves_another_kind_alone(self, sdk: JobSDK) -> None:
        sdk.register("capped", lambda h: None, retention=1)
        sdk.register("plain", lambda h: None)
        capped, plain = [], []
        for _ in range(3):
            capped.append(_finish(sdk, "capped"))
            plain.append(_finish(sdk, "plain"))
        _settle(sdk, 1 + 3)
        assert _ids_on_disk(sdk) == {capped[-1], *plain}

    def test_the_run_that_just_finished_is_kept_whatever_its_stamp(self, sdk: JobSDK) -> None:
        # The stamps are whole seconds, so an older run can tie with the one that
        # just finished. The worst tie is an older record that sorts newer:
        # these planted ones do, and the run that just finished is still kept.
        sdk.register("k", lambda h: None, retention=1)
        _plant(sdk, "0" * 32, "k", stamp=FUTURE)
        _plant(sdk, "f" * 32, "k", stamp=FUTURE)
        newest = _finish(sdk, "k")
        _settle(sdk, 1)
        assert _ids_on_disk(sdk) == {newest}

    def test_an_executing_run_is_never_removed(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()

        def blocker(h) -> None:  # noqa: ANN001 - the SDK's handle
            started.set()
            assert release.wait(timeout=5.0)

        sdk.register("k", blocker, retention=1)
        blocked = sdk.start("k")
        assert started.wait(timeout=5.0)
        _plant(sdk, "f" * 32, "k", status=RUNNING)  # another process's unresolved run
        sdk.register("k", lambda h: None, retention=1)
        quick = [_finish(sdk, "k") for _ in range(2)]
        _wait_until(
            lambda: _file_count(sdk) == 3 and quick[-1] not in sdk.cancelling_and_live_ids()[1]
        )
        assert _ids_on_disk(sdk) == {blocked, "f" * 32, quick[-1]}
        assert sdk.get(blocked).status == RUNNING
        release.set()
        _wait_terminal(sdk, blocked)
        _settle(sdk, 2)
        assert _ids_on_disk(sdk) == {blocked, "f" * 32}

    def test_each_removal_is_audited(self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[dict] = []
        monkeypatch.setattr(
            job_sdk, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
        )
        sdk.register("k", lambda h: None, retention=1)
        first = _finish(sdk, "k")
        _finish(sdk, "k")

        def evicted() -> list[str]:
            return [e["resources"] for e in events if e["operation"] == "jobs.job_evicted"]

        _wait_until(lambda: evicted() == [first])
        _settle(sdk, 1)
        assert evicted() == [first]

    def test_two_runs_finishing_together_do_not_remove_each_other(self, sdk: JobSDK) -> None:
        # Hold the sweep lock so both runs pass the live-table pop before either
        # sweep scans: the shape where each could see the other as old.
        sdk.register("k", lambda h: None, retention=1)
        with sdk._sweep_lock:  # noqa: SLF001 - the gate the sweeps queue on
            ids = [sdk.start("k") for _ in range(2)]
            _wait_until(
                lambda: not sdk.cancelling_and_live_ids()[1]
                and all(sdk.get(i) is not None and sdk.get(i).is_terminal for i in ids)
            )
        _wait_until(lambda: all(_swept(sdk, i) for i in ids))
        assert len(_ids_on_disk(sdk) & set(ids)) == 1

    def test_a_sweep_lets_its_run_go_before_the_next_sweep_scans(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The first run's sweep starts a second run once it has pruned, before
        # its run leaves ``_unswept``. If the sweep lock were free by then, the
        # second sweep could scan while the first run still reads as held, remove
        # nothing, and leave two records under a cap of one.
        sdk.register("k", lambda h: None, retention=1)
        sweep = job_sdk._sweep
        late: list[str] = []

        def sweep_then_finish_another(*args) -> None:  # noqa: ANN002 - _sweep's own
            sweep(*args)
            if not late:
                gated = sdk._sweep_lock.locked()  # noqa: SLF001 - read before the next run exists
                late.append(sdk.start("k"))
                if not gated:
                    _wait_until(lambda: _swept(sdk, late[0]))

        monkeypatch.setattr("kiro_crew.apps.job_sdk_retention._sweep", sweep_then_finish_another)
        first = sdk.start("k")
        _wait_until(lambda: bool(late) and _swept(sdk, first) and _swept(sdk, late[0]))
        assert _ids_on_disk(sdk) == {late[0]}

    def test_a_run_finishing_during_a_scan_is_not_removed(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The first sweep's scan starts a second run and lets it finish before
        # reading the store, so the second run is terminal but not yet swept when
        # the first sweep decides what to remove.
        sdk.register("k", lambda h: None, retention=1)
        scan = job_sdk.JobStore._iter_runs_nolink
        late: list[str] = []

        def scan_with_a_late_finish(store):  # noqa: ANN001 - a JobStore
            if not late:
                late.append(sdk.start("k"))
                _wait_until(lambda: late[0] in sdk._unswept)  # noqa: SLF001
            yield from scan(store)

        monkeypatch.setattr(job_sdk.JobStore, "_iter_runs_nolink", scan_with_a_late_finish)
        first = sdk.start("k")
        _wait_until(lambda: bool(late) and _swept(sdk, first) and _swept(sdk, late[0]))
        assert _ids_on_disk(sdk) == {late[0]}

    def test_a_negative_cap_is_refused(self, sdk: JobSDK) -> None:
        with pytest.raises(ValueError, match="retention"):
            sdk.register("k", lambda h: None, retention=-1)

    def test_a_capped_kind_refuses_a_key_its_scan_could_not_read(self, sdk: JobSDK) -> None:
        # A record over the scan's size limit is never removed, so a capped kind
        # refuses the one unbounded caller field before any record is written.
        sdk.register("capped", lambda h: None, retention=1)
        with pytest.raises(ValueError, match="dedupe_key"):
            sdk.start("capped", dedupe_key="k" * (job_sdk._MAX_DEDUPE_KEY + 1))
        assert _file_count(sdk) == 0
        at_bound = sdk.start("capped", dedupe_key="k" * job_sdk._MAX_DEDUPE_KEY)
        _wait_terminal(sdk, at_bound)
        _wait_until(lambda: _swept(sdk, at_bound))
        _finish(sdk, "capped")
        _settle(sdk, 1)
        assert at_bound not in _ids_on_disk(sdk)

    def test_a_kind_without_a_cap_still_takes_a_long_key(self, sdk: JobSDK) -> None:
        sdk.register("plain", lambda h: None)
        long_key = "k" * (job_sdk._MAX_DEDUPE_KEY + 1)
        run_id = sdk.start("plain", dedupe_key=long_key)
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.dedupe_key == long_key

    def test_the_largest_capped_record_fits_the_scan(self) -> None:
        # Every caller and runner field at its bound, in characters JSON escapes
        # to twelve bytes each: the record stays far under the scan's limit.
        wide = "\U0001f600"
        params = {
            f"{n:02d}" + "p" * (job_sdk._MAX_PARAM_KEY - 2): wide * job_sdk._MAX_PARAM_VALUE
            for n in range(job_sdk._MAX_PARAMS)
        }
        run = JobRun(
            run_id="e" * 32,
            app="a",
            kind="k",
            status=DONE,
            dedupe_key=wide * job_sdk._MAX_DEDUPE_KEY,
            params=params,
            error=wide * 2000,
        )
        assert len(json.dumps(run.to_dict(), indent=1)) < job_sdk._MAX_RECORD_BYTES // 4


class TestTheSweepNeverHarmsTheRun:
    def test_a_failing_sweep_still_leaves_the_run_terminal_and_released(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(self):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(job_sdk.JobStore, "_iter_runs_nolink", _boom)
        sdk.register("k", lambda h: None, retention=1)
        run_id = _finish(sdk, "k")
        _settle(sdk, 1)
        assert sdk.get(run_id).status == DONE

    def test_a_disabled_sdk_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Disable leaves a worker that ignores cancel running. A re-enable builds a
        # new SDK over the same directory, with no cap. When the old worker returns,
        # its sweep still carries the old cap and an empty live table.
        monkeypatch.setattr(
            job_sdk,
            "_CLEANUP_JOIN_SECS",
            0.2,  # flake-ok: disable's join must expire so the worker outlives it
        )
        started = threading.Event()
        stop = threading.Event()
        workers: list[threading.Thread] = []

        def stubborn(h) -> None:  # noqa: ANN001 - the SDK's handle
            workers.append(threading.current_thread())
            started.set()
            stop.wait(10.0)

        old = JobSDK("reenable-app", tmp_path)
        try:
            old.register("k", stubborn, cancellable=True, retention=1)
            old.start("k")
            assert started.wait(5.0)
            assert asyncio.run(old.remove_all_async()).still_running == 1
        finally:
            forget_sdk(old.app_name)

        new = JobSDK("reenable-app", tmp_path)
        try:
            new.register("k", lambda h: None)
            kept = {_finish(new, "k") for _ in range(2)}
            stop.set()
            for t in workers:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the stubborn worker outlived its test"
            assert _ids_on_disk(new) == kept
        finally:
            stop.set()
            forget_sdk(new.app_name)

    def test_reconcile_does_not_bring_back_a_removed_record(self, sdk: JobSDK) -> None:
        # reconcile decides from a snapshot. A run it read as running may have
        # finished and been removed by the cap before its write.
        stale = JobRun(
            run_id="c" * 32,
            app=sdk.app_name,
            kind="k",
            status=RUNNING,
            origin=job_sdk._ORIGIN,  # noqa: SLF001 - this process's record, as reconcile sees it
        )
        assert sdk._persist(stale, only_if_not_terminal=True) is False  # noqa: SLF001
        assert sdk.get(stale.run_id) is None


class TestTheScanSkipsWhatItCannotTrust:
    def test_a_body_naming_another_run_is_skipped(self, sdk: JobSDK) -> None:
        sdk.register("k", lambda h: None, retention=1)
        victim = _finish(sdk, "k")
        forged = JobRun(run_id=victim, app=sdk.app_name, kind="k", status=DONE)
        (sdk.store.dir / f"{'b' * 32}.json").write_text(
            json.dumps(forged.to_dict()), encoding="utf-8"
        )
        assert {r.run_id for r in sdk.store._iter_runs_nolink()} == {victim}  # noqa: SLF001

    @requires_symlinks
    def test_a_planted_link_is_neither_counted_nor_removed(
        self, sdk: JobSDK, tmp_path: Path
    ) -> None:
        sdk.register("k", lambda h: None, retention=1)
        first = _finish(sdk, "k")
        outside = tmp_path / "outside-record.json"
        outside.write_text(
            json.dumps(JobRun(run_id="d" * 32, app=sdk.app_name, kind="k", status=DONE).to_dict()),
            encoding="utf-8",
        )
        link = sdk.store.dir / f"{'d' * 32}.json"
        link.symlink_to(outside)
        second = _finish(sdk, "k")
        _wait_until(lambda: not (sdk.store.dir / f"{first}.json").exists())
        assert link.is_symlink() and outside.exists()
        assert sdk.get(second) is not None

    def test_an_oversized_or_unparseable_record_is_skipped(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sdk.register("k", lambda h: None)
        kept = _finish(sdk, "k")
        big = JobRun(run_id="e" * 32, app=sdk.app_name, kind="k", status=DONE, error="x" * 4096)
        (sdk.store.dir / f"{big.run_id}.json").write_text(
            json.dumps(big.to_dict()), encoding="utf-8"
        )
        (sdk.store.dir / f"{'a' * 32}.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(
            job_sdk, "_MAX_RECORD_BYTES", (sdk.store.dir / f"{kept}.json").stat().st_size
        )
        assert {r.run_id for r in sdk.store._iter_runs_nolink()} == {kept}  # noqa: SLF001


class TestTheBackupJobsOptIn:
    """aws_control is the cap's consumer; its readers still see what they read."""

    def test_the_cap_applies_in_the_backup_apps_own_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The security lists protect this app's data dir from agent reads, so a
        # scan through the agent-facing read would see no record there at all.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        sdk = JobSDK(backup_mod.APP_NAME, app_data_dir(backup_mod.APP_NAME))
        try:
            sdk.register("k", lambda h: None, retention=1)
            first = _finish(sdk, "k")
            second = _finish(sdk, "k")
            _settle(sdk, 1)
            assert _ids_on_disk(sdk) == {second} != {first}
        finally:
            forget_sdk(sdk.app_name)

    def test_both_backup_kinds_register_with_the_cap(self) -> None:
        sdk = mock.Mock()
        sdk.reconcile.return_value = 0
        asyncio.run(hooks._register_job_runners(SimpleNamespace(job=sdk)))
        kinds = {c.args[0]: c.kwargs for c in sdk.register.call_args_list}
        assert set(kinds) == set(backup_mod.JOB_KINDS)
        assert all(kw == {"retention": hooks._JOB_RECORDS_KEPT} for kw in kinds.values())

    def test_the_cap_is_at_least_every_readers_window(self) -> None:
        # The Backup row reads the newest 20 of a kind; ``_jobs/recent`` at most
        # ``_RECENT_MAX``. A smaller cap would remove runs a page can show.
        assert hooks._JOB_RECORDS_KEPT >= max(20, job_routes._RECENT_MAX)

    def test_the_backup_row_still_reports_the_newest_failure_after_the_cap(
        self, tmp_path: Path
    ) -> None:
        sdk = JobSDK(backup_mod.APP_NAME, tmp_path)

        def runner(h) -> None:  # noqa: ANN001 - the SDK's handle
            raise RuntimeError("drive refused the upload")

        kind = backup_mod.KIND_SNAPSHOT
        try:
            sdk.register(kind, runner, retention=2)
            for n, run_id in enumerate(("1" * 32, "2" * 32)):
                _plant(sdk, run_id, kind, stamp=f"2000-01-0{n + 1}T00:00:00Z", dedupe_key=ACCOUNT)
            _wait_terminal(sdk, sdk.start(kind, dedupe_key=ACCOUNT))
            _settle(sdk, 2)
            assert "2" * 32 in _ids_on_disk(sdk)
            with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
                got = routes_mod._account_jobs(ACCOUNT)[backup_mod.KIND_SNAPSHOT]
            assert got["active"] is None
            assert "drive refused the upload" in got["lastFailed"]["error"]
        finally:
            forget_sdk(sdk.app_name)

"""Regression tests for atomic runs-registry persistence (Track A, bug 1).

Covers taskrunner._persist_runs / _load_runs:
- writes are atomic (temp + fsync + os.replace) so a crash can't truncate,
- a corrupt/truncated registry is NOT silently discarded — it is surfaced
  loudly (logged at error) and preserved as a ``.corrupt`` sidecar,
- a missing registry seeds a fresh (empty) run set without error.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner


def _make_runner(work_dir: Path) -> TaskRunner:
    return TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=work_dir)


def _make_run(task_id: str = "t1") -> TaskRun:
    return TaskRun(
        spec_path="/tmp/TASK.md",
        spec_content="# demo",
        task_id=task_id,
        name="demo run",
        status="paused",
        source="text",
        work_dir="/tmp",
        tasks=[
            Step(index=0, title="step one", description="do it", status=StepStatus.PENDING),
        ],
    )


class TestPersistRoundTrip:
    def test_persist_writes_valid_json(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        runner._runs[_make_run().task_id] = _make_run()
        runner._persist_runs()

        runs_file = tmp_path / "runs.json"
        assert runs_file.exists()
        data = json.loads(runs_file.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert data[0]["task_id"] == "t1"

    def test_persist_leaves_no_temp_files(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        runner._runs["t1"] = _make_run()
        runner._persist_runs()
        # Atomic write must clean up its temp file after os.replace.
        assert list(tmp_path.glob("*.tmp")) == []

    def test_round_trip_reloads_run(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        runner._runs["t1"] = _make_run()
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        assert "t1" in reloaded._runs
        assert reloaded._runs["t1"].status == "paused"
        assert reloaded._runs["t1"].tasks[0].title == "step one"


class TestLoadRunsResilience:
    def test_missing_file_seeds_fresh(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        # No runs.json on disk.
        runner._load_runs()
        assert runner._runs == {}

    def test_corrupt_file_not_silently_discarded(self, tmp_path: Path, caplog) -> None:
        # A truncated/partial write left behind by a crash mid-persist.
        runs_file = tmp_path / "runs.json"
        runs_file.write_text('[{"task_id": "t1", "spec_pa', encoding="utf-8")

        runner = _make_runner(tmp_path)
        with caplog.at_level("ERROR"):
            runner._load_runs()

        # State is NOT silently dropped: the corruption is surfaced loudly and
        # the bad file is preserved for recovery rather than overwritten/empty.
        assert runner._runs == {}
        assert any("corrupt" in r.message.lower() for r in caplog.records)
        assert (tmp_path / "runs.json.corrupt").exists()
        # Original path was moved aside (so the next persist starts clean).
        assert not runs_file.exists()

    def test_unreadable_file_does_not_prevent_startup(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        # An existing but unreadable registry (permission error / transient
        # Windows sharing violation) must NOT raise out of _load_runs — that
        # is called from TaskRunner.__init__ and would otherwise block startup.
        runs_file = tmp_path / "runs.json"
        runs_file.write_text("[]", encoding="utf-8")

        orig_read_text = Path.read_text

        def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self.name == "runs.json":
                raise PermissionError("locked")
            return orig_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", boom)
        with caplog.at_level("ERROR"):
            runner = _make_runner(tmp_path)  # __init__ -> _load_runs; must not raise

        assert runner._runs == {}
        assert any("failed to read" in r.message.lower() for r in caplog.records)
        # File is left untouched (NOT moved to .corrupt) so a later successful
        # read can still recover it.
        assert runs_file.exists()
        assert not (tmp_path / "runs.json.corrupt").exists()


class TestConcurrentPersist:
    def test_persist_lock_serializes_writes(self, tmp_path: Path) -> None:
        """Overlapping persistence workers (dispatched via _apersist_runs ->
        asyncio.to_thread) must never interleave their snapshot+write. The
        registry file must always stay valid JSON with the full run set — an
        older, slow os.replace can't clobber a newer one under the lock."""
        runner = _make_runner(tmp_path)
        for i in range(20):
            runner._runs[f"t{i}"] = _make_run(f"t{i}")

        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(30):
                    runner._persist_runs()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        data = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
        assert len(data) == 20
        assert list(tmp_path.glob("*.tmp")) == []

    def test_stale_snapshot_does_not_overwrite_newer(self, tmp_path: Path) -> None:
        """A snapshot with an older sequence whose (offloaded) write is
        scheduled late must never clobber a newer snapshot that already
        landed — _commit_snapshot enforces monotonic ordering."""
        runner = _make_runner(tmp_path)
        newer = json.dumps([{"task_id": "newer"}])
        older = json.dumps([{"task_id": "older"}])

        runner._commit_snapshot(5, newer)
        assert json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))[0]["task_id"] == "newer"

        # Older sequence arriving late is ignored.
        runner._commit_snapshot(3, older)
        assert json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))[0]["task_id"] == "newer"

    def test_apersist_snapshots_on_caller_thread(self, tmp_path: Path) -> None:
        """_apersist_runs must build the snapshot before offloading, so the
        live _runs registry is never iterated in the worker thread."""
        import asyncio

        runner = _make_runner(tmp_path)
        runner._runs["t1"] = _make_run("t1")
        asyncio.run(runner._apersist_runs())

        data = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
        assert [d["task_id"] for d in data] == ["t1"]


class TestAsyncMutationPersistence:
    def test_update_plan_offloads_atomic_write(self, tmp_path: Path, monkeypatch) -> None:
        """Public mutation APIs must await durability without running fsync on
        the gateway event-loop thread."""
        import asyncio

        runner = _make_runner(tmp_path)
        run = _make_run("t1")
        run.status = "planned"
        runner._runs[run.task_id] = run
        loop_thread = threading.get_ident()
        write_threads: list[int] = []

        def record_write(path, content, *, fsync=False):  # type: ignore[no-untyped-def]
            write_threads.append(threading.get_ident())
            path.write_text(content, encoding="utf-8")

        monkeypatch.setattr("kiro_crew.taskrunner.atomic_write", record_write)
        asyncio.run(runner.update_plan("t1", [{"title": "updated"}]))

        assert write_threads
        assert all(thread_id != loop_thread for thread_id in write_threads)
        assert runner._runs["t1"].tasks[0].title == "updated"


class TestBackgroundStartAdmission:
    def test_concurrent_starts_are_serialized_and_get_unique_ids(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Persistence yields must not let starts overwrite task/run tracking."""
        spec = tmp_path / "same-spec.md"
        spec.write_text("# Task\n", encoding="utf-8")
        runner = _make_runner(tmp_path)
        first_persist_entered = asyncio.Event()
        allow_first_persist = asyncio.Event()
        release_runs = asyncio.Event()
        persist_calls = 0

        async def delayed_persist() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                first_persist_entered.set()
                await allow_first_persist.wait()

        async def held_run(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            await release_runs.wait()

        runner._apersist_runs = delayed_persist  # type: ignore[method-assign]
        runner.run = held_run  # type: ignore[method-assign]
        monkeypatch.setattr(time, "time_ns", lambda: 123456789)

        async def exercise() -> None:
            first = asyncio.create_task(runner.start_background(spec))
            await asyncio.wait_for(first_persist_entered.wait(), timeout=2)
            second = asyncio.create_task(runner.start_background(spec))
            await asyncio.sleep(0)

            # The second start cannot pass admission while the first durable
            # placeholder write is in flight.
            assert not second.done()
            assert len(runner._runs) == 1

            allow_first_persist.set()
            first_id, second_id = await asyncio.gather(first, second)
            assert first_id != second_id
            assert {first_id, second_id} == set(runner._runs)
            assert {first_id, second_id} == set(runner._tasks)

            background_tasks = list(runner._tasks.values())
            release_runs.set()
            await asyncio.gather(*background_tasks)

        asyncio.run(exercise())

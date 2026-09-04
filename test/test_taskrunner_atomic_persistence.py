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


class TestGitWorkspaceIdentitySurvivesRestart:
    """`work_dir` alone does not describe a run's git workspace.

    ``git_coord.init_workspace()`` OVERWRITES ``work_dir`` with the worktree
    path, so restoring only ``work_dir`` leaves a run pointed AT a worktree
    while every field saying which worktree it is came back empty -- and
    ``git_enabled`` came back at its ``True`` default. ``git_coord`` reads all
    six of these after a restart: ``git_enabled`` gates every git op,
    ``branch_name`` gates retry's workspace validation, ``worktree_path`` +
    ``repo_root`` drive cleanup and recovery, ``base_branch`` is the range for
    the step-diff summary, and ``commit_hashes`` is what a revert pops.
    """

    def _git_run(self, task_id: str = "g1") -> TaskRun:
        run = _make_run(task_id)
        run.work_dir = "/repos/proj/../.kirocrew-work/g1"
        run.branch_name = "kirocrew/task/g1"
        run.base_branch = "main"
        run.worktree_path = "/repos/.kirocrew-work/g1"
        run.repo_root = "/repos/proj"
        run.git_enabled = True
        run.commit_hashes = ["abc1234", "def5678"]
        return run

    def test_every_git_field_round_trips(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        runner._runs["g1"] = self._git_run()
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        back = reloaded._runs["g1"]

        assert back.branch_name == "kirocrew/task/g1"
        assert back.base_branch == "main"
        assert back.worktree_path == "/repos/.kirocrew-work/g1"
        assert back.repo_root == "/repos/proj"
        assert back.git_enabled is True
        assert back.commit_hashes == ["abc1234", "def5678"]

    def test_a_restored_run_still_gates_retry_validation(self, tmp_path: Path) -> None:
        """The consequence that matters: `retry_from_task` skips workspace
        validation entirely when `branch_name` is empty, so losing it across a
        restart silently re-opened the very defect the validation was added for
        -- steps dispatching against a worktree nothing had checked."""
        runner = _make_runner(tmp_path)
        runner._runs["g1"] = self._git_run()
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()

        assert reloaded._runs["g1"].branch_name, (
            "branch_name did not survive the restart, so retry's "
            "`if run.branch_name and not await workspace_is_valid(run)` guard "
            "short-circuits and never validates the workspace"
        )

    def test_a_legacy_entry_does_not_enable_git_without_an_identity(
        self, tmp_path: Path
    ) -> None:
        """An entry written before these fields were persisted carries none of
        them. `git_enabled` must NOT come back at its `True` default there: git
        ops enabled while nothing records which worktree they target is the one
        combination this must not reconstruct. Falling back to "no git
        coordination" is the documented behaviour for a workspace git cannot be
        pointed at."""
        runner = _make_runner(tmp_path)
        runner._runs["g1"] = self._git_run()
        runner._persist_runs()

        runs_file = tmp_path / "runs.json"
        data = json.loads(runs_file.read_text(encoding="utf-8"))
        for key in (
            "branch_name",
            "base_branch",
            "worktree_path",
            "repo_root",
            "git_enabled",
            "commit_hashes",
        ):
            data[0].pop(key, None)
        runs_file.write_text(json.dumps(data), encoding="utf-8")

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        back = reloaded._runs["g1"]

        assert back.git_enabled is False
        assert back.worktree_path == ""
        assert back.branch_name == ""
        assert back.commit_hashes == []

    def test_an_explicit_git_enabled_false_is_honoured(self, tmp_path: Path) -> None:
        """A non-git run (`git_enabled=False` from the start, run in place) must
        stay disabled -- the fallback only applies when the value is absent."""
        runner = _make_runner(tmp_path)
        plain = _make_run("g2")
        plain.git_enabled = False
        runner._runs["g2"] = plain
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        assert reloaded._runs["g2"].git_enabled is False


class TestLessonsLearnedSurvivesRestart:
    """`lessons_learned` is produced once and cannot be recomputed.

    ``_extract_lesson`` appends a rule to ``run.lessons_learned`` after an LLM
    call, once per lesson-yielding step, during execution. Nothing rebuilds it
    on load: the lesson TEXT is separately durable in the lesson/vector store,
    but that store is a global corpus keyed by category, so the per-run
    attribution is the part that only ``runs.json`` holds.

    Two consumers read it after a restart -- the dashboard status payload
    (``task_reporter.build_status``, which ``handlers/taskrunner.py`` then
    redacts element-wise) and the to-chat continuation prompt's "Lessons
    Learned" section (``handlers/taskrunner.py:473``). Both go silently empty
    for every run that outlived a gateway restart.
    """

    def _lessons_run(self, task_id: str = "l1") -> TaskRun:
        run = _make_run(task_id)
        run.lessons_learned = [
            "Run the migration before seeding fixtures",
            "The flake was a shared tmp dir, not a race",
        ]
        return run

    def test_lessons_learned_round_trips(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        runner._runs["l1"] = self._lessons_run()
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()

        assert reloaded._runs["l1"].lessons_learned == [
            "Run the migration before seeding fixtures",
            "The flake was a shared tmp dir, not a race",
        ]

    def test_the_status_payload_still_carries_the_lessons_after_a_restart(
        self, tmp_path: Path
    ) -> None:
        """The consequence that matters: `build_status` publishes
        `lessons_learned` for every run, and `api_taskrunner_status` redacts
        each element before it reaches the dashboard. A restart turned that
        into an empty list, so the section the UI renders disappeared with no
        error anywhere."""
        runner = _make_runner(tmp_path)
        runner._runs["l1"] = self._lessons_run()
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        payload = reloaded.status()
        entry = next(r for r in payload["runs"] if r["task_id"] == "l1")

        assert entry["lessons_learned"] == [
            "Run the migration before seeding fixtures",
            "The flake was a shared tmp dir, not a race",
        ]

    def test_a_legacy_entry_without_the_key_loads_as_no_lessons(self, tmp_path: Path) -> None:
        """An entry written before this field was persisted carries no key.
        The default is the empty list -- the same thing the reader saw before,
        so no legacy entry changes meaning."""
        runner = _make_runner(tmp_path)
        runner._runs["l1"] = self._lessons_run()
        runner._persist_runs()

        runs_file = tmp_path / "runs.json"
        data = json.loads(runs_file.read_text(encoding="utf-8"))
        data[0].pop("lessons_learned", None)
        runs_file.write_text(json.dumps(data), encoding="utf-8")

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        assert reloaded._runs["l1"].lessons_learned == []

    def test_a_run_that_learned_nothing_stays_empty(self, tmp_path: Path) -> None:
        """Negative control: persisting the field must not manufacture one."""
        runner = _make_runner(tmp_path)
        runner._runs["l2"] = _make_run("l2")
        runner._persist_runs()

        reloaded = _make_runner(tmp_path)
        reloaded._load_runs()
        assert reloaded._runs["l2"].lessons_learned == []


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

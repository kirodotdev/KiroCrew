"""``kirocrew doctor`` names task rows nothing is picking up, and writes nothing.

A row that is not terminal, holds no lease and fell due long ago is what a lost
terminal write leaves: a stopped sub-agent whose cancel never landed, parked in
``retry_wait`` with no owner. Its parent's pending count stays above zero, so the
parent's chat slot answers ``409 slot_subagents_running`` until someone finds the
row. These tests pin the store read that finds such rows, the doctor lines that
name them, and that the doctor run leaves the store exactly as it found it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from overload_fakes import Clock, open_task_store

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import resources, workload
from kiro_crew.taskq import model
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_SIX_HOURS = 6 * 3600


def _rec(task_id: str, **kw) -> model.TaskRecord:
    kw.setdefault("kind", model.KIND_SUBAGENT)
    kw.setdefault("params", {"task": f"task {task_id}"})
    kw.setdefault("session_key", "dashboard:chat-1")
    return model.TaskRecord(id=task_id, **kw)


def _park_after_start(store: TaskStore, task_id: str, *, due: float) -> None:
    """Claim *task_id*, start it, and release it to ``retry_wait`` due at *due*.

    The path the reported row took: claimed, ``starting``, then released for a
    re-dispatch, which clears the lease.
    """
    claimed = store.claim(task_id)
    assert claimed is not None
    gen = claimed.generation
    assert store.transition(task_id, model.STARTING, generation=gen)
    assert store.transition(task_id, model.RETRY_WAIT, generation=gen, next_run_at=due)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock, name="tasks/tasks.db", window=8)


def test_the_read_returns_only_unleased_non_terminal_rows_long_past_due(
    store: TaskStore, clock: Clock
) -> None:
    store.accept([_rec(i) for i in ("stuck", "recent", "never-deferred", "leased", "ended")])
    t0 = clock.t
    _park_after_start(store, "stuck", due=t0)
    # Claimed and parked on infra with a due time: it keeps its lease.
    leased = store.claim("leased")
    assert leased is not None
    assert store.transition(
        "leased", model.WAITING_INFRA, generation=leased.generation, next_run_at=t0
    )
    # Parked like "stuck", then ended: terminal, whatever its due time says.
    _park_after_start(store, "ended", due=t0)
    ended = store.get("ended")
    assert ended is not None
    assert store.transition("ended", model.FAILED, generation=ended.generation)
    clock.advance(_SIX_HOURS)
    # Due ten minutes ago: a row a busy queue can still be holding.
    _park_after_start(store, "recent", due=clock.t - 600)

    rows = store.overdue_unleased_rows(3600)

    assert [r.id for r in rows] == ["stuck"]
    assert rows[0].state == model.RETRY_WAIT
    assert rows[0].lease_owner is None
    assert store.get("never-deferred").next_run_at is None  # queued, never deferred
    assert store.get("leased").lease_owner  # waiting_infra keeps the lease


def _seed_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, gateway: bool | None = True
) -> Path:
    """A ``KIROCREW_HOME`` under *tmp_path*, with the gateway probe answering *gateway*.

    True is a running gateway, the case the warning is for, False a stopped one and
    None a lock probe that cannot tell. The pair doctor reads is pinned, so the
    verdict never depends on a gateway the test host happens to run.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    pid = UNALLOCATABLE_PID if gateway else None
    monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: pid)
    monkeypatch.setattr(resources, "_gateway_lock_indeterminate", lambda: gateway is None)
    return tmp_path / "tasks" / "tasks.db"


def _snapshot(path: Path) -> dict[str, tuple]:
    reader = TaskStore(path, network_fs=False, diagnostic=True).open()
    try:
        return {
            r.id: (r.state, r.generation, r.lease_owner, r.next_run_at, len(reader.events(r.id)))
            for r in reader.list_rows(limit=1000)
        }
    finally:
        reader.close()


def test_doctor_names_a_stranded_row_and_leaves_the_store_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _seed_home(tmp_path, monkeypatch)
    s = TaskStore(path, network_fs=False).open()
    s.accept([_rec("sa-stuck-1", session_key="dashboard:chat-7"), _rec("sa-waiting")])
    _park_after_start(s, "sa-stuck-1", due=time.time() - _SIX_HOURS)
    s.close()
    before = _snapshot(path)

    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert "stranded rows: ⚠️  1 task row(s) not terminal, with no lease" in out
    assert "sa-stuck-1 (retry_wait, session dashboard:chat-7, overdue 6.0h)" in out
    assert "sa-waiting" not in out
    assert "409 slot_subagents_running" in out
    assert any("1 task row(s) not terminal, with no lease" in i for i in issues)
    # Report only: no state, generation, lease, due time or event changed.
    assert _snapshot(path) == before


def test_doctor_prints_no_stranded_warning_for_a_healthy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _seed_home(tmp_path, monkeypatch)
    s = TaskStore(path, network_fs=False).open()
    s.accept([_rec("queued-now"), _rec("retry-soon"), _rec("done-long-ago")])
    # Due a minute ago: past due, but well inside the bound.
    _park_after_start(s, "retry-soon", due=time.time() - 60)
    _park_after_start(s, "done-long-ago", due=time.time() - _SIX_HOURS)
    rec = s.get("done-long-ago")
    assert rec is not None
    assert s.transition("done-long-ago", model.CANCELLED, generation=rec.generation)
    s.close()

    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert "task store:" in out
    # "stranded rows:", not "stranded": the store path printed above carries the
    # test's own name.
    assert "stranded rows:" not in out
    assert not any("no lease" in i for i in issues)


def test_doctor_caps_the_named_rows_and_counts_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _seed_home(tmp_path, monkeypatch)
    s = TaskStore(path, network_fs=False).open()
    total = workload._STRANDED_REPORT_CAP + 2
    ids = [f"sa-{i}" for i in range(total)]
    s.accept([_rec(i) for i in ids])
    for n, task_id in enumerate(ids):
        # Oldest due first: sa-0 is the most overdue.
        _park_after_start(s, task_id, due=time.time() - _SIX_HOURS - (total - n))
    s.close()

    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert f"{total} task row(s) not terminal" in out
    named = [i for i in ids if f"{i} (retry_wait" in out]
    assert named == ids[: workload._STRANDED_REPORT_CAP]
    assert "+2 more" in out


def test_a_refused_stranded_read_is_reported_unread_and_the_counts_still_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew.taskq import TaskStoreUnavailable

    path = _seed_home(tmp_path, monkeypatch)
    s = TaskStore(path, network_fs=False).open()
    s.accept([_rec("queued-now")])
    s.close()

    def refuse(self: TaskStore, overdue_secs: float) -> list[model.TaskRecord]:
        raise TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(TaskStore, "overdue_unleased_rows", refuse)
    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert "pending=1" in out
    assert "stranded rows: ⚠️  not checked (database is locked)" in out
    assert issues == []


def _one_stranded_row(path: Path) -> None:
    s = TaskStore(path, network_fs=False).open()
    s.accept([_rec("sa-stuck-1")])
    _park_after_start(s, "sa-stuck-1", due=time.time() - _SIX_HOURS)
    s.close()


def test_with_no_gateway_running_the_rows_are_advisory_and_no_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stopped gateway leaves every deferred row past due; the next start claims
    # them, so listing them as a fault would send the user after nothing.
    _one_stranded_row(_seed_home(tmp_path, monkeypatch, gateway=False))

    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert "stranded rows: ℹ️  1 task row(s) not terminal" in out
    assert "sa-stuck-1 (retry_wait" in out
    assert "No gateway is running, so these may only be waiting for it" in out
    assert "409 slot_subagents_running" not in out
    assert issues == []


def test_an_indeterminate_gateway_probe_keeps_the_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # "Cannot tell" is not "stopped": a serving gateway on Windows reads this way.
    _one_stranded_row(_seed_home(tmp_path, monkeypatch, gateway=None))

    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert "stranded rows: ⚠️  1 task row(s) not terminal" in out
    assert "409 slot_subagents_running" in out
    assert len(issues) == 1


def test_a_failed_gateway_probe_keeps_the_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A probe that raises says nothing about the gateway, so it is not "stopped".
    _one_stranded_row(_seed_home(tmp_path, monkeypatch))

    def broken() -> None:
        raise OSError("lock file unreadable")

    monkeypatch.setattr(cli_doctor, "_read_gateway_pid", broken)
    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out

    assert "stranded rows: ⚠️  1 task row(s) not terminal" in out
    assert len(issues) == 1

"""Windows stop risks exercised through boot-contained Jobs and durable receipts.

Tree-snapshot ordering is replaced by kernel membership, not waived: late children,
dead intermediaries and vanished successor records must all remain covered. Native
membership/descendant enforcement lives in test_pod_windows_job.
"""

from __future__ import annotations

import subprocess

import pytest
from test_pod_windows_run import contained
from test_pod_windows_run import model as model

from kiro_crew import platform_compat as pc
from kiro_crew.pod import _windows_run as runs
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import windows as win

__all__ = ["model"]


@pytest.mark.parametrize(
    "evidence",
    [
        "dead_record",
        "malformed_record",
        "unreadable_record",
        "invalid_encoding",
        "missing_token",
        "reused_identity",
        "wrapper_only",
        "result_only",
        "orphaned_handoff",
        "home_only",
        "task_only",
        "settled_handoff",
    ],
)
def test_stop_pod_refuses_missing_root_with_prior_writer_evidence(model, monkeypatch, evidence):
    cfg, state = model
    record = win.pid_record_path(cfg, "demo")
    if evidence == "home_only":
        cfg.home_dir("demo").mkdir(parents=True)
    elif evidence == "task_only":
        pass  # The scheduler model reports a registered task.
    elif evidence in {"wrapper_only", "result_only", "orphaned_handoff"}:
        path = {
            "wrapper_only": win.task_script_path,
            "result_only": win.result_path,
            "orphaned_handoff": win.handoff_marker_path,
        }[evidence](cfg, "demo")
        path.write_text("4242\n100\n", encoding="utf-8")
    elif evidence == "settled_handoff":
        win.write_task_script(cfg, "demo")
        win._begin_handoff(cfg, "demo")
        win._end_handoff(cfg, "demo")
    else:
        record.write_bytes(
            {
                "malformed_record": b"not-a-pid",
                "invalid_encoding": b"\xff",
                "missing_token": b"4242\n",
            }.get(evidence, b"4242\n100\n")
        )
    before = {p: p.read_bytes() for p in cfg.pods_dir.iterdir() if p.is_file()}
    if evidence == "unreadable_record":
        monkeypatch.setattr(
            win, "_read_pid_record", lambda *_a: (_ for _ in ()).throw(PermissionError())
        )
    monkeypatch.setattr(
        rt, "cleanup_home", lambda *_a: pytest.fail("legacy evidence cannot authorize cleanup")
    )
    result = rt.stop_pod(cfg, "demo")
    assert result.returncode == 1 and "preserved" in result.stderr
    assert "/End" not in state.events and "/Delete" not in state.events
    assert "open_job" not in state.events
    assert all(p.read_bytes() == data for p, data in before.items())


def test_stop_accepts_a_never_started_empty_plane(model, monkeypatch):
    cfg, state = model
    monkeypatch.setattr(
        win,
        "schtasks",
        lambda *args: state.events.append(args[0]) or subprocess.CompletedProcess([], 1, "", ""),
    )
    assert win.stop(cfg, "demo", timeout=0).returncode == 0
    assert not {"open_job", "retire", "job_zero"}.intersection(state.events)


def test_public_opener_closes_a_recycled_pid_handle(monkeypatch):
    closed = []
    monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 8001)
    monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _handle: (4242, 200, None))
    monkeypatch.setattr(pc, "close_process_handle", closed.append)
    assert pc.open_process_termination_handle(4242, "100") is None
    assert closed == [8001]


@pytest.mark.parametrize(
    "outcome",
    [
        "ordinary",
        "orphaned_publisher",
        "live_successor",
        "dead_successor",
        "cleared_record",
        "marker_absent",
        "stale_marker",
        "late_child",
        "dead_intermediary",
        "second_restart",
    ],
)
def test_stop_covers_successors_even_when_records_and_intermediaries_disappear(
    model, monkeypatch, outcome
):
    cfg, state = model
    contained(cfg)
    win.write_task_script(cfg, "demo")
    cfg.home_dir("demo").mkdir(parents=True)
    saved = cfg.home_dir("demo") / "owned-state"
    saved.write_text("owned", encoding="utf-8")
    if outcome == "orphaned_publisher":
        monkeypatch.setattr(
            win.jobs, "open_identity", lambda pid, token: None if pid == 777 else 8100
        )

    def task(*args):
        state.events.append(args[0])
        if args[0] == "/End":
            assert state.active == 1
            win._begin_handoff(cfg, "demo")
            win.record_supervised_pid(cfg, "demo", 4300)
            if outcome in {"cleared_record", "dead_intermediary", "second_restart"}:
                win.clear_supervised_pid(cfg, "demo")
            win._end_handoff(cfg, "demo")
            # Only the Job model knows this descendant; no parent snapshot can see it.
            state.active = 2 if outcome in {"late_child", "dead_intermediary"} else 1
        if args[0] == "/Delete":
            assert state.active == 0
            assert runs.read(cfg, "demo")["state"] == "drained"
            assert saved.read_text(encoding="utf-8") == "owned"
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(win, "schtasks", task)
    assert rt.stop_pod(cfg, "demo").returncode == 0
    assert state.events.index("open_job") < state.events.index("/End")
    assert state.events.index("job_zero") < state.events.index("/Delete")
    assert not saved.exists()
    assert state.events.count("close_identity") == (1 if outcome == "orphaned_publisher" else 2)
    assert state.events.count("close_job") == 1


@pytest.mark.parametrize("phase", ["before_end", "after_end"])
@pytest.mark.parametrize(
    "failure", ["unopenable", "unreadable_identity", "query_error", "survivor", "publisher"]
)
def test_incomplete_identity_or_kernel_proof_preserves_home_and_task(
    model, monkeypatch, phase, failure
):
    cfg, state = model
    contained(cfg)
    win.write_task_script(cfg, "demo")
    cfg.home_dir("demo").mkdir(parents=True)
    saved = cfg.home_dir("demo") / "state"
    saved.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(rt, "cleanup_home", lambda *_a: pytest.fail("incomplete proof"))
    if phase == "before_end":
        monkeypatch.setattr(
            win.jobs, "open_identity", lambda *_a: (_ for _ in ()).throw(OSError(failure))
        )
    else:

        def fail(*_a, **_kw):
            raise OSError(failure)

        if failure == "publisher":
            monkeypatch.setattr(win.jobs, "retire_identity", fail)
        else:
            monkeypatch.setattr(win.jobs.PodJob, "terminate_and_wait", fail)
    result = rt.stop_pod(cfg, "demo")
    assert result.returncode == 1 and failure in result.stderr
    assert "/Delete" not in state.events
    assert ("/End" in state.events) is (phase == "after_end")
    assert saved.read_text(encoding="utf-8") == "preserve"
    assert win.task_script_path(cfg, "demo").exists()
    assert runs.read(cfg, "demo")["state"] == "ready"
    if phase == "after_end":
        assert state.events.count("close_identity") == 2
        assert state.events.count("close_job") == 1


@pytest.mark.parametrize("moment", ["end", "retire", "receipt"])
def test_a_changed_generation_or_failed_receipt_never_authorizes_cleanup(
    model, monkeypatch, moment
):
    cfg, state = model
    contained(cfg)
    cfg.home_dir("demo").mkdir(parents=True)
    monkeypatch.setattr(
        rt, "cleanup_home", lambda *_a: pytest.fail("late writer must preserve HOME")
    )

    def replace(*_a, **_kw):
        current = runs.read(cfg, "demo")
        current["generation"] = "b" * 32
        runs.publish(cfg, "demo", current)

    if moment == "retire":
        monkeypatch.setattr(win.jobs, "retire_identity", replace)
    elif moment == "receipt":
        monkeypatch.setattr(
            runs, "drained", lambda *_a: (_ for _ in ()).throw(OSError("receipt refused"))
        )
    else:

        def task(*args):
            state.events.append(args[0])
            if args[0] == "/End":
                replace()
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(win, "schtasks", task)
    assert rt.stop_pod(cfg, "demo").returncode == 1
    assert "/Delete" not in state.events
    assert cfg.home_dir("demo").exists()


def test_recycled_root_is_not_signalled_but_contained_descendants_still_drain(model, monkeypatch):
    cfg, state = model
    contained(cfg)
    opened, closed = [], []

    def pin(pid, token):
        opened.append((pid, token))
        return None if pid == 4242 else 8100  # Positive reuse proof, not access failure.

    monkeypatch.setattr(win.jobs, "open_identity", pin)
    monkeypatch.setattr(win.jobs, "close_identity", closed.append)
    assert win.stop(cfg, "demo").returncode == 0
    assert opened == [(777, "100"), (4242, "100")]
    assert closed == [8100]
    assert state.active == 0


@pytest.mark.parametrize("exception", [OSError("end failed"), RuntimeError("unexpected failure")])
def test_stop_releases_retained_handles_when_end_raises(model, monkeypatch, exception):
    cfg, state = model
    contained(cfg)
    monkeypatch.setattr(win, "schtasks", lambda *_a: (_ for _ in ()).throw(exception))
    if isinstance(exception, OSError):
        assert win.stop(cfg, "demo").returncode == 1
    else:
        with pytest.raises(RuntimeError):
            win.stop(cfg, "demo")
    assert state.events.count("close_identity") == 2
    assert state.events.count("close_job") == 1
    assert runs.read(cfg, "demo")["state"] == "ready"

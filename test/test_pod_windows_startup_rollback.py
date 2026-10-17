"""A cancelled ``pod up`` deletes its Task Scheduler wrapper the way ``pod down`` does.

When a Windows start fails before ``/Run``, ``_rollback_start`` deletes the task and
its ``.cmd`` wrapper. A process this backend does not own -- the Task Scheduler
service finishing with the action file, an indexer, an AV scanner -- can still hold
that file open for a moment, and the delete then fails with ``[WinError 32]``.
``stop`` waits that one error out through ``_unlink_waiting_out_sharing``; the
rollback goes through the same helper, so it waits out the same hold and still fails
closed on anything else.
"""

from __future__ import annotations

import functools
import subprocess

import pytest
from test_pod_windows_stop_race import _FakeClock, _held

from kiro_crew.pod import _windows_run as runs
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import PodConfig


@pytest.fixture
def cancelled_start(tmp_path, monkeypatch):
    """A reserved, never-run start whose task and wrapper the rollback must delete."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
    cfg = PodConfig.load()
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    events: list[str] = []

    def schtasks(*args):
        events.append(args[0])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(win, "schtasks", schtasks)
    record = runs.reserve(cfg, "demo")
    wrapper = win.task_script_path(cfg, "demo")
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("@echo off\n", encoding="utf-8")
    clock = _FakeClock()
    # The helper's default clock and sleep are bound at definition, so the fake
    # ones are handed to it the way a caller would; no test waits for real.
    monkeypatch.setattr(
        win,
        "_unlink_waiting_out_sharing",
        functools.partial(win._unlink_waiting_out_sharing, sleep=clock.sleep, clock=clock),
    )
    return cfg, record, wrapper, events, clock


def test_a_wrapper_held_briefly_is_still_deleted_by_the_rollback(cancelled_start, monkeypatch):
    cfg, record, wrapper, events, clock = cancelled_start
    attempts = _held(monkeypatch, wrapper, holds=2)

    win._rollback_start(cfg, "demo", record)

    assert not wrapper.exists()
    assert attempts[0] == 3
    assert len(clock.sleeps) == 2
    assert events == ["/Query", "/Delete"]
    # The whole rollback finished, so its cancellation receipt is consumed.
    assert runs.read(cfg, "demo") is None


def test_a_hold_past_the_ceiling_still_fails_the_rollback_closed(cancelled_start, monkeypatch):
    cfg, record, wrapper, _events, _clock = cancelled_start
    _held(monkeypatch, wrapper, holds=10_000)

    with pytest.raises(PermissionError):
        win._rollback_start(cfg, "demo", record)

    assert wrapper.exists()
    # Cancellation survives the failed cleanup, so a later start may retry it.
    assert runs.read(cfg, "demo")["state"] == "cancelled"


def test_an_access_denial_fails_the_rollback_at_once(cancelled_start, monkeypatch):
    cfg, record, wrapper, _events, clock = cancelled_start
    attempts = _held(monkeypatch, wrapper, holds=1, error=PermissionError)

    with pytest.raises(PermissionError):
        win._rollback_start(cfg, "demo", record)

    assert attempts[0] == 1
    assert clock.sleeps == []
    assert runs.read(cfg, "demo")["state"] == "cancelled"

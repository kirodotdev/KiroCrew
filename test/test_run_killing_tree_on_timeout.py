"""``platform_compat.run_killing_tree_on_timeout``: a timeout that ends the child's tree.

On Windows the stdlib's ``subprocess.run`` kills only the direct child on a timeout and
then waits, with no bound, for every process holding its pipes. These tests drive the
Windows arm on any host with a fake ``Popen``: no child is spawned and nothing waits,
so every timeout passed is the shared fallback and never elapses.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_NONE, run_limited
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_ARGV = ["git", "-c", "alias.zz=!sleep 30", "zz"]


class _FakePopen:
    """A ``Popen`` whose ``communicate`` answers from a script, one entry per call."""

    instances: list[_FakePopen] = []
    script: list[Any] = []

    def __init__(self, args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = UNALLOCATABLE_PID
        self.returncode: int | None = None
        self.calls: list[tuple[Any, float | None]] = []
        self.events: list[str] = []
        self.script: list[Any] = list(type(self).script)
        type(self).instances.append(self)

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        self.calls.append((input, timeout))
        self.events.append("communicate")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.returncode = step[0]
        return step[1], step[2]

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None, "waited on a child communicate() never finished"
        return self.returncode

    def kill(self) -> None:
        self.events.append("kill")


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> list[_FakePopen]:
    """The Windows arm with a fake ``Popen`` and a recording tree kill."""
    _FakePopen.instances = []
    _FakePopen.script = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    def _kill_tree(proc: _FakePopen) -> None:
        proc.events.append("kill_tree")

    monkeypatch.setattr(platform_compat, "kill_popen_tree", _kill_tree)
    return _FakePopen.instances


def _timeout(seconds: float) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(_ARGV, seconds)


def test_a_timeout_kills_the_tree_then_drains_under_a_bound(windows) -> None:
    _FakePopen.script = [_timeout(FALLBACK_TIMEOUT_SECS), (1, b"partial", b"")]
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        platform_compat.run_killing_tree_on_timeout(
            _ARGV, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS
        )
    proc = windows[0]
    assert proc.kwargs["stdout"] is subprocess.PIPE and proc.kwargs["stderr"] is subprocess.PIPE
    assert "capture_output" not in proc.kwargs and "timeout" not in proc.kwargs
    assert proc.events == ["communicate", "kill_tree", "communicate"]
    assert proc.calls == [
        (None, FALLBACK_TIMEOUT_SECS),
        (None, platform_compat._POST_KILL_DRAIN_SECS),
    ]
    assert caught.value.cmd == _ARGV and caught.value.timeout == FALLBACK_TIMEOUT_SECS
    assert caught.value.stdout == b"partial"


def test_a_drain_that_also_times_out_still_raises_the_caller_s_timeout(windows) -> None:
    """Something outside the tree holds a pipe: the call returns at the drain bound."""
    _FakePopen.script = [
        _timeout(FALLBACK_TIMEOUT_SECS),
        _timeout(platform_compat._POST_KILL_DRAIN_SECS),
    ]
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        platform_compat.run_killing_tree_on_timeout(
            _ARGV, stdout=subprocess.PIPE, timeout=FALLBACK_TIMEOUT_SECS
        )
    assert caught.value.timeout == FALLBACK_TIMEOUT_SECS
    assert windows[0].events == ["communicate", "kill_tree", "communicate"]


def test_a_child_that_finishes_is_a_completed_process(windows) -> None:
    _FakePopen.script = [(0, "out", "err")]
    result = platform_compat.run_killing_tree_on_timeout(
        _ARGV, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS, input="x", text=True
    )
    assert (result.args, result.returncode, result.stdout, result.stderr) == (
        _ARGV,
        0,
        "out",
        "err",
    )
    assert windows[0].kwargs["stdin"] is subprocess.PIPE
    assert windows[0].calls == [("x", FALLBACK_TIMEOUT_SECS)]

    _FakePopen.script = [(3, "o", "e")]
    with pytest.raises(subprocess.CalledProcessError) as caught:
        platform_compat.run_killing_tree_on_timeout(
            _ARGV, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS, check=True
        )
    assert (caught.value.returncode, caught.value.output, caught.value.stderr) == (3, "o", "e")


@pytest.mark.parametrize(
    ("is_windows", "kwargs"),
    [
        (False, {"capture_output": True, "timeout": FALLBACK_TIMEOUT_SECS}),
        (True, {"capture_output": True}),
        (
            True,
            {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": FALLBACK_TIMEOUT_SECS,
            },
        ),
    ],
    ids=["posix", "no-timeout", "no-pipe"],
)
def test_otherwise_it_is_subprocess_run_with_the_caller_s_arguments(
    monkeypatch: pytest.MonkeyPatch, is_windows: bool, kwargs: dict[str, Any]
) -> None:
    seen: list[tuple[Any, dict[str, Any]]] = []
    done = subprocess.CompletedProcess(_ARGV, 0, b"", b"")

    def _run(args: Any, **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append((args, kw))
        return done

    def _no_popen(*_a: Any, **_k: Any) -> None:
        raise AssertionError("this arm must not spawn through Popen")

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(subprocess, "run", _run)
    # Stands in as the stdlib's own run, so only the platform and the arguments decide.
    monkeypatch.setattr(platform_compat, "_STDLIB_RUN", _run)
    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    assert platform_compat.run_killing_tree_on_timeout(_ARGV, **kwargs) is done
    assert seen == [(_ARGV, kwargs)]


def test_a_replaced_subprocess_run_still_intercepts_the_windows_shape(windows) -> None:
    """A stub of ``subprocess.run`` keeps standing in for the child on Windows too."""
    done = subprocess.CompletedProcess(_ARGV, 0, b"", b"")
    seen: list[dict[str, Any]] = []

    def _stub(args: Any, **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(kw)
        return done

    kwargs = {"capture_output": True, "timeout": FALLBACK_TIMEOUT_SECS}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", _stub)
        assert platform_compat.run_killing_tree_on_timeout(_ARGV, **kwargs) is done
    assert seen == [kwargs]
    assert windows == []


def test_run_limited_routes_its_windows_spawn_through_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_limited`` still reports the caller's argv on the Windows timeout."""
    seen: list[tuple[Any, dict[str, Any]]] = []

    def _tree_run(args: Any, **kw: Any) -> None:
        seen.append((args, kw))
        raise subprocess.TimeoutExpired(["spawned", *args], kw["timeout"])

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "run_killing_tree_on_timeout", _tree_run)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_limited(
            _ARGV, profile=RLIMIT_PROFILE_NONE, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS
        )
    assert caught.value.cmd == _ARGV
    assert seen == [
        (_ARGV, {"preexec_fn": None, "capture_output": True, "timeout": FALLBACK_TIMEOUT_SECS})
    ]

"""The sandbox mount-source pin scan in its own process.

On Linux the scan of the host's ``/proc`` runs in a spawned child so its
syscalls stop re-taking the gateway's GIL. Pinned here:

- the real child runs end to end (a real spawn reading the real ``/proc``) and
  is what ``_mount_pinned_source_names`` / ``_bound_source_basenames`` answer
  with on Linux;
- every way the child can fail -- no start, timeout, crash, non-zero exit,
  malformed reply -- answers ``None``, which the sweep treats as an unproven
  scan, so a dead-pid directory is kept.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import sandbox_mount_sweep, sandbox_pin_scan_child
from kiro_crew.sandbox import (
    _MOUNT_SOURCE_MAX_AGE_SECONDS,
    _cleanup_stale_sandbox_mount_sources,
    _PinScanCoverage,
)

requires_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the pin scan child runs on Linux only"
)

_DEAD_PID = 4_194_305  # above Linux PID_MAX_LIMIT (2**22) -- never a live process


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@requires_linux
def test_real_child_scans_the_host_proc(monkeypatch: pytest.MonkeyPatch, sandbox_sweep_original):
    """A real spawned child reads the real /proc and its answer is what the
    gateway-side entry points return."""
    reply = sandbox_pin_scan_child.scan_in_child("keyed")
    assert reply is not None
    pinned, complete, covered = reply
    assert isinstance(pinned, set) and all(n.startswith("kirocrew_sb_") for n in pinned)
    assert isinstance(complete, bool) and isinstance(covered, bool)

    calls: list[str] = []
    real = sandbox_pin_scan_child.scan_in_child

    def _spy(kind: str, **kw):
        calls.append(kind)
        return real(kind, **kw)

    monkeypatch.setattr(sandbox_pin_scan_child, "scan_in_child", _spy)
    pinned, _ = sandbox_sweep_original("_mount_pinned_source_names")(coverage=_PinScanCoverage())
    assert all(n.startswith("kirocrew_sb_") for n in pinned)
    sandbox_sweep_original("_bound_source_basenames")(coverage=_PinScanCoverage())
    assert calls == ["keyed", "legacy"]


_GOOD_REPLY = '{"pinned": [], "complete": true, "covered": true}'


@pytest.mark.parametrize(
    ("argv", "timeout", "reason"),
    [
        pytest.param(["/nonexistent/kirocrew-python"], 30.0, "could not start", id="cannot-start"),
        # The child would reply well-formed after the sleep, so only the timeout
        # can make this answer None.
        pytest.param(
            _python(f"import time; time.sleep(60); print({_GOOD_REPLY!r})"),
            1.0,
            "timed out",
            id="timeout",
        ),
        pytest.param(
            _python("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"),
            30.0,
            "exited with code",
            id="crash",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="no SIGKILL"),
        ),
        pytest.param(
            _python(f"import sys; print({_GOOD_REPLY!r}); sys.exit(3)"),
            30.0,
            "exited with code 3",
            id="non-zero-exit",
        ),
        pytest.param(_python("print('not json')"), 30.0, "malformed", id="malformed-not-json"),
        pytest.param(
            _python(f"print({_GOOD_REPLY.replace('[]', '[1]')!r})"),
            30.0,
            "malformed",
            id="malformed-wrong-shape",
        ),
        pytest.param(_python("pass"), 30.0, "malformed", id="malformed-empty"),
    ],
)
def test_failed_child_answers_none(argv, timeout, reason, caplog):
    assert sandbox_pin_scan_child.scan_in_child("keyed", argv=argv, timeout=timeout) is None
    assert "pin scan child failed" in caplog.text
    assert reason in caplog.text


def test_well_formed_reply_is_returned():
    argv = _python('print(\'{"pinned": ["kirocrew_sb_1_a"], "complete": false, "covered": true}\')')
    assert sandbox_pin_scan_child.scan_in_child("keyed", argv=argv) == (
        {"kirocrew_sb_1_a"},
        False,
        True,
    )


def test_child_entry_refuses_an_unknown_kind():
    assert sandbox_pin_scan_child.main(["bogus"]) == 2


@pytest.mark.parametrize("kind", ["keyed", "legacy"])
def test_child_scan_runs_the_matching_in_process_scan(kind, monkeypatch: pytest.MonkeyPatch):
    """The child's scan is the in-process one over /proc, with the legacy name
    shape for ``legacy``, and reports what that scan found."""
    seen: dict[str, object] = {}

    def _fake(proc_root, *, matcher, coverage):
        seen.update(proc_root=proc_root, matcher=matcher)
        coverage.covered = False
        return {"kirocrew_sb_2_b", "kirocrew_sb_1_a"}, True

    monkeypatch.setattr(sandbox_mount_sweep, "_mount_pinned_source_names_in_process", _fake)
    reply = sandbox_pin_scan_child._scan(kind)

    assert reply == {
        "pinned": ["kirocrew_sb_1_a", "kirocrew_sb_2_b"],
        "complete": True,
        "covered": False,
    }
    assert seen["proc_root"] == "/proc"
    expected = sandbox_mount_sweep._is_legacy_source_name if kind == "legacy" else None
    assert seen["matcher"] is expected


def test_child_entry_writes_one_reply_line(capfd, monkeypatch: pytest.MonkeyPatch):
    """``main`` writes the scan's answer as one JSON line on the original stdout,
    which ``scan_in_child`` parses back into the same answer."""
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(os, "nice", lambda _inc: 0, raising=False)
    monkeypatch.setattr(
        sandbox_pin_scan_child,
        "_scan",
        lambda kind: {"pinned": [f"kirocrew_sb_1_{kind}"], "complete": True, "covered": True},
    )

    assert sandbox_pin_scan_child.main(["keyed"]) == 0

    out = capfd.readouterr().out
    assert out.count("\n") == 1
    assert sandbox_pin_scan_child._parse_reply(out.encode()) == (
        {"kirocrew_sb_1_keyed"},
        True,
        True,
    )


def _failing_child(monkeypatch: pytest.MonkeyPatch) -> None:
    real = sandbox_pin_scan_child.scan_in_child
    monkeypatch.setattr(
        sandbox_pin_scan_child,
        "scan_in_child",
        lambda kind, **kw: real(kind, argv=_python("import os; os._exit(70)")),
    )
    monkeypatch.setattr(sandbox_mount_sweep, "_scan_runs_in_child", lambda proc_root: True)


def test_failed_child_keeps_a_dead_pid_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_sweep_original
):
    """A failed child is an unproven scan: the sweep reclaims no directory."""
    monkeypatch.setattr(
        "kiro_crew.sandbox._mount_pinned_source_names",
        sandbox_sweep_original("_mount_pinned_source_names"),
    )
    _failing_child(monkeypatch)
    held = tmp_path / f"kirocrew_sb_{_DEAD_PID}_home"
    held.mkdir()
    stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
    os.utime(held, (stale, stale))

    assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 0
    assert held.exists()


def test_failed_child_leaves_the_legacy_scan_unproven(
    monkeypatch: pytest.MonkeyPatch, sandbox_sweep_original
):
    _failing_child(monkeypatch)
    coverage = _PinScanCoverage()
    bound, complete = sandbox_sweep_original("_bound_source_basenames")(coverage=coverage)
    assert (bound, complete, coverage.covered) == (set(), False, False)

"""The stranded browser-daemon kill: the group TERM, the grace, the re-verify, the group SIGKILL.

``kill_orphan_mcps`` hands a confirmed stranded playwright-cli daemon to
``_kill_orphan_browser_daemon`` (``session_pid_orphan_kill``). These pin each way out of that
kill, reached through ``kiro_crew.session_pid`` as callers reach it, with every process
primitive replaced so no signal can land on a real process.
"""

from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest
from pid_lifecycle_helpers import _POSIX_ONLY

from kiro_crew import session_pid
from kiro_crew.testing.ids import UNALLOCATABLE_PID

pytestmark = _POSIX_ONLY

_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/srv/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"kc-1a2b3c4d\x00--headed"
)


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """An isolated group leader that is still alive: records every group signal and audit row."""
    seen = SimpleNamespace(signals=[], audits=[])

    def killpg(pgid: int, sig: int) -> None:
        seen.signals.append((pgid, sig))

    monkeypatch.setattr(session_pid.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(session_pid.os, "getpgrp", lambda: 1, raising=False)
    monkeypatch.setattr(session_pid.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(session_pid.platform_compat, "pid_exists", lambda pid: True)
    monkeypatch.setattr(
        session_pid,
        "_sel_orphan_kill",
        lambda pid, pgid, cmdline, method: seen.audits.append((pid, pgid, method)),
    )
    return seen


def _no_grace(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    """The TERM grace is already spent, on *platform*."""
    monkeypatch.setattr(session_pid, "_BROWSER_DAEMON_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(session_pid, "sys", SimpleNamespace(platform=platform))


def test_a_daemon_that_is_gone_is_not_signalled(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(pid: int) -> int:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(session_pid.os, "getpgid", gone, raising=False)
    assert session_pid._kill_orphan_browser_daemon(UNALLOCATABLE_PID, _CMDLINE) == 0
    assert (daemon.signals, daemon.audits) == ([], [])


@pytest.mark.parametrize(
    "pid, pgid_of, our_group",
    [
        pytest.param(UNALLOCATABLE_PID, lambda pid: pid + 1, 1, id="not-its-own-group-leader"),
        pytest.param(UNALLOCATABLE_PID, lambda pid: pid, UNALLOCATABLE_PID, id="our-own-group"),
        # pid 1 leads init's group: ``killpg(1, sig)`` is ``kill(-1, sig)``, every process.
        pytest.param(1, lambda pid: pid, 2, id="init-group"),
    ],
)
def test_a_daemon_that_is_not_an_isolated_leader_is_left_alone(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, pid: int, pgid_of, our_group: int
) -> None:
    """``killpg`` would reach processes nothing identified, so nothing is signalled."""
    monkeypatch.setattr(session_pid.os, "getpgid", pgid_of, raising=False)
    monkeypatch.setattr(session_pid.os, "getpgrp", lambda: our_group, raising=False)
    assert session_pid._kill_orphan_browser_daemon(pid, _CMDLINE) == 0
    assert (daemon.signals, daemon.audits) == ([], [])


def test_a_refused_term_ends_the_kill(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(pgid: int, sig: int) -> None:
        raise PermissionError(pgid)

    monkeypatch.setattr(session_pid.os, "killpg", refused, raising=False)
    assert session_pid._kill_orphan_browser_daemon(UNALLOCATABLE_PID, _CMDLINE) == 0
    assert daemon.audits == []


def test_a_daemon_that_exits_in_its_grace_is_recorded_as_a_term(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first liveness probe after the TERM sees it gone: no SIGKILL, one TERM record."""
    monkeypatch.setattr(session_pid, "_BROWSER_DAEMON_TERM_GRACE_SECONDS", 3600.0)
    monkeypatch.setattr(session_pid.platform_compat, "pid_exists", lambda pid: False)
    assert session_pid._kill_orphan_browser_daemon(UNALLOCATABLE_PID, _CMDLINE) == 1
    assert daemon.signals == [(UNALLOCATABLE_PID, signal.SIGTERM)]
    assert daemon.audits == [(UNALLOCATABLE_PID, UNALLOCATABLE_PID, "browser-daemon-sigterm")]


def test_a_pid_recycled_inside_the_grace_is_never_force_killed(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the grace, a Linux re-read that shows another process's argv ends it."""
    _no_grace(monkeypatch, "linux")

    class _Recycled:
        def __init__(self, path: str) -> None:
            self.path = path

        def read_bytes(self) -> bytes:
            return b"/usr/bin/some-other-process\x00"

    monkeypatch.setattr(session_pid, "Path", _Recycled)
    assert session_pid._kill_orphan_browser_daemon(UNALLOCATABLE_PID, _CMDLINE) == 1
    assert daemon.signals == [(UNALLOCATABLE_PID, signal.SIGTERM)]
    assert daemon.audits == [(UNALLOCATABLE_PID, UNALLOCATABLE_PID, "browser-daemon-sigterm")]


def test_a_daemon_alive_past_its_grace_gets_the_group_sigkill(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Linux there is no argv re-read; the still-isolated group takes the SIGKILL."""
    _no_grace(monkeypatch, "darwin")
    assert session_pid._kill_orphan_browser_daemon(UNALLOCATABLE_PID, _CMDLINE) == 1
    assert daemon.signals == [
        (UNALLOCATABLE_PID, signal.SIGTERM),
        (UNALLOCATABLE_PID, signal.SIGKILL),
    ]
    assert daemon.audits == [
        (UNALLOCATABLE_PID, UNALLOCATABLE_PID, "browser-daemon-sigterm+sigkill")
    ]


def test_a_sigkill_that_fails_is_still_recorded(
    daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group may exit between the grace and the SIGKILL; the kill is recorded either way."""
    _no_grace(monkeypatch, "darwin")

    def term_then_gone(pgid: int, sig: int) -> None:
        daemon.signals.append((pgid, sig))
        if sig == signal.SIGKILL:
            raise ProcessLookupError(pgid)

    monkeypatch.setattr(session_pid.os, "killpg", term_then_gone, raising=False)
    assert session_pid._kill_orphan_browser_daemon(UNALLOCATABLE_PID, _CMDLINE) == 1
    assert daemon.audits == [
        (UNALLOCATABLE_PID, UNALLOCATABLE_PID, "browser-daemon-sigterm+sigkill")
    ]

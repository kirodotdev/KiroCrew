"""Tests for ``kiro_crew.testing.harness`` — gateway-spawning context manager.

Most tests exercise the harness's internal helpers in isolation with a
stand-in for ``subprocess.Popen``. Spawning a real gateway takes 5–15s
and pulls in the full MCP probe / config init / dashboard bind path, so
the end-to-end test is gated behind ``KIROCREW_HARNESS_INTEGRATION``.

This file runs on Windows too. A wholesale collect-ignore of it is what
hid the READY-wait and orchestration tests -- the majority of it, and all of the
platform-neutral part -- from the only shards that could have caught the
selectors-on-a-pipe break. What is genuinely POSIX-only is the ``terminate_pgid``
family plus the SIGKILL return-code assertions, and those carry
``_POSIX_ONLY`` per test. Nothing else is skipped: see
``docs/system-specs/common/testing-conventions.md`` on why a whole-file skip is
the expensive kind of green.
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.testing.harness import (
    DEFAULT_READY_TIMEOUT,
    READY_PREFIX,
    GatewayHandle,
    GatewaySpawnError,
    _drain_stderr,
    _resolve_workspace_src,
    _terminate_process_group,
    _wait_for_ready_line,
    parse_ready_line,
    spawn_feature_gateway,
    terminate_pgid,
)

# Process groups, ``setsid``, ``killpg`` and negative SIGKILL return codes are
# POSIX concepts with no Windows equivalent -- ``_terminate_process_group``
# routes Windows through ``platform_compat.kill_process_tree`` instead, whose
# ``taskkill /F`` exit is not a negative signal number. Per test, never
# per file.
_POSIX_ONLY = pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="POSIX process-group semantics (setsid/killpg, -SIGKILL return codes)",
)


class FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for ``_wait_for_ready_line``.

    Exposes just the surface the harness touches: ``poll``, ``stdout``,
    ``returncode``, ``pid``. ``stdout`` is backed by an OS pipe (not a
    BytesIO) so ``selectors.register`` can poll it the same way it polls
    a real subprocess. Pre-populate via ``stdout_lines``; the bytes are
    written to the pipe at construction and the write end is closed so
    ``select`` reports EOF after the buffered bytes are consumed.
    """

    def __init__(self, stdout_lines: list[bytes]) -> None:
        read_fd, write_fd = os.pipe()
        if stdout_lines:
            os.write(write_fd, b"".join(stdout_lines))
        os.close(write_fd)  # EOF on read side once buffered bytes are drained
        # Default buffering wraps the underlying FileIO in a BufferedReader,
        # which exposes ``read1()`` — same shape ``subprocess.Popen.stdout``
        # has when ``stdout=PIPE``.
        self.stdout = os.fdopen(read_fd, "rb")
        self.stderr: Optional[io.BytesIO] = None
        self.returncode: Optional[int] = None
        self.pid = 99999  # unused — _wait_for_ready_line never signals

    def poll(self) -> Optional[int]:
        return self.returncode


# ── _wait_for_ready_line ────────────────────────────────────────────────


def test_ready_line_parses_valid_payload() -> None:
    payload = '{"port": 52093, "token": "abc", "pid": 1234, "home": "/tmp/x"}'
    fake = FakePopen([f"{READY_PREFIX}{payload}\n".encode()])

    result = _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=[])  # type: ignore[arg-type]

    assert result["port"] == 52093
    assert result["token"] == "abc"
    assert result["pid"] == 1234


def test_ready_line_skips_lines_before_ready() -> None:
    """Pre-READY chatter (e.g. ``Created default config``) must not break parsing."""
    fake = FakePopen(
        [
            b"Some startup log\n",
            b"Another line\n",
            f'{READY_PREFIX}{{"port": 1, "token": "t", "pid": 1, "home": "/h"}}\n'.encode(),
        ]
    )

    result = _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=[])  # type: ignore[arg-type]

    assert result["port"] == 1


def test_ready_line_raises_on_early_exit_with_stderr() -> None:
    """Subprocess exits before READY → exception includes stderr tail."""
    # Prime the proc as already-exited so the harness's poll() check fires
    # on the next iteration after consuming whatever output we provided.
    fake = FakePopen([b"some output\n"])
    fake.returncode = 2  # simulate already-exited subprocess
    stderr = [b"FATAL: missing config\n", b"Traceback (most recent call last):\n"]

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=stderr)  # type: ignore[arg-type]

    assert "exited with code 2" in str(exc.value)
    assert "FATAL: missing config" in str(exc.value)


def test_ready_line_raises_on_timeout_with_stderr() -> None:
    """No READY line within timeout → exception includes stderr tail.

    Pins the contract under test: even with stdout fully drained (EOF at
    once) but proc.poll() reporting alive forever, the deadline must
    still fire because the selector poll is bounded.
    """
    fake = FakePopen([b"line1\n", b"line2\n"])  # never emits READY
    # FakePopen's pipe writer was already closed in __init__ — the read
    # side will see EOF, but proc.poll() returns None (alive). The
    # selector keeps reporting EOF readiness; the harness keeps looping
    # until the deadline fires.
    stderr = [b"WARNING something\n"]

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=0.3, stderr_buffer=stderr)  # type: ignore[arg-type]

    assert "did not emit" in str(exc.value)
    assert "WARNING something" in str(exc.value)
    assert "KIROCREW_HARNESS_READY_TIMEOUT" in str(exc.value)


def test_ready_line_timeout_fires_when_subprocess_silent_but_alive() -> None:
    """Deadline must fire even when stdout never produces data.

    Pins the contract introduced by the selectors-based loop: a subprocess
    that's alive but never writes to stdout must hit the timeout, not
    block forever in a stdout read.
    """
    # Empty pipe + proc.poll() returning None forever simulates a hung
    # subprocess that's silent on stdout. Without the selector-based
    # bounded poll, this would hang the harness indefinitely.
    fake = FakePopen([])  # no output
    fake.returncode = None  # explicitly alive

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=0.4, stderr_buffer=[])  # type: ignore[arg-type]

    assert "did not emit" in str(exc.value)


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not-json",  # invalid JSON
        "[1, 2, 3]",  # valid JSON but not a dict
        '"just-a-string"',  # valid JSON, scalar
        '{"foo": 1}',  # dict, missing both required keys
        '{"port": 1}',  # dict, missing token
        '{"token": "t", "pid": 1}',  # dict, missing port
    ],
)
def test_ready_line_raises_on_malformed_payload(bad_payload: str) -> None:
    fake = FakePopen([f"{READY_PREFIX}{bad_payload}\n".encode()])

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=1.0, stderr_buffer=[])  # type: ignore[arg-type]

    msg = str(exc.value)
    assert "malformed" in msg or "expected dict" in msg or "missing required key" in msg


# ── parse_ready_line ────────────────────────────────────────────────────


def test_parse_ready_line_valid() -> None:
    payload = parse_ready_line(
        f'{READY_PREFIX}{{"port": 42, "token": "tok", "pid": 7, "home": "/h"}}'
    )
    assert payload == {"port": 42, "token": "tok", "pid": 7, "home": "/h"}


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not-json",  # invalid JSON
        "[1, 2, 3]",  # valid JSON, not a dict
        '"scalar"',  # valid JSON, scalar
        '{"foo": 1}',  # dict, missing both required keys
        '{"port": 1}',  # dict, missing token
        '{"token": "t"}',  # dict, missing port
    ],
)
def test_parse_ready_line_rejects_bad_payload(bad_payload: str) -> None:
    with pytest.raises(GatewaySpawnError) as exc:
        parse_ready_line(f"{READY_PREFIX}{bad_payload}")
    msg = str(exc.value)
    assert "malformed" in msg or "expected dict" in msg or "missing required key" in msg


@pytest.mark.parametrize(
    "non_matching_line",
    [
        "Some unrelated log line",
        '{"port": 1, "token": "t"}',  # payload without prefix
        f"  {READY_PREFIX}" + '{"port": 1, "token": "t"}',  # prefix not at col 0
        "",
    ],
)
def test_parse_ready_line_rejects_missing_prefix(non_matching_line: str) -> None:
    """The primitive owns the prefix check — a non-matching line raises
    ``GatewaySpawnError`` instead of json-decoding a blind slice (which would
    misattribute the failure to protocol drift, or in the pathological case
    silently accept a wrong payload)."""
    with pytest.raises(GatewaySpawnError) as exc:
        parse_ready_line(non_matching_line)
    assert "does not start with" in str(exc.value)


# ── _terminate_process_group ────────────────────────────────────────────


def test_terminate_handles_already_exited_proc() -> None:
    """Already-exited proc → no-op, no exception.

    Runs on Windows too. An exited ROOT is not a gone TREE, so both platforms
    reach their kill primitive even for a process that has already exited —
    what this pins is that doing so raises nothing when there is genuinely
    nothing left to sweep.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    # 30s: generous for `python -c pass`, but the package-builder fleet can
    # stall a fresh child process >5s under load (two consecutive Dry Run
    # failures on with TimeoutExpired at timeout=5).
    proc.wait(timeout=30)
    assert proc.poll() is not None  # already exited

    # Should not raise even though the process is gone.
    _terminate_process_group(proc)


def test_created_after_cannot_attribute_a_darwin_style_token() -> None:
    """The creation-identity guard is NUMERIC, and macOS tokens are not numbers.

    ``created_after`` compares ``int(child) > int(parent)``. On Linux the token is
    ``/proc/<pid>/stat``'s start time in clock ticks, so that works; on macOS it is
    ``ps -o lstart=`` -- ``"Thu Sep 10 22:30:00 2026"`` -- where ``int()`` raises and
    the guard answers False for EVERY pair, however far apart the two were born.

    Pinned because a caller that filters candidates through this guard on macOS
    filters out all of them, which is silent: nothing raises, the set is simply
    empty. ``_terminate_process_group`` therefore applies it on Windows only, where
    the stale parent map it compensates for actually exists.

    Runs everywhere: the tokens are literals, so no platform has to produce them.
    """
    earlier = "Thu Sep 10 22:30:00 2026"
    later = "Thu Sep 10 22:30:01 2026"
    assert platform_compat.created_after(later, earlier) is False, (
        "a non-numeric token pair cannot be ordered by this guard, so a caller must "
        "not read False as 'not a child'"
    )
    # The numeric form it IS for, as the contrast that makes the point.
    assert platform_compat.created_after("200", "100") is True
    assert platform_compat.created_after("100", "100") is False


@_POSIX_ONLY
def test_terminate_ends_a_descendant_that_left_the_process_group() -> None:
    """A child the gateway spawned with ``start_new_session=True`` is outside the
    group the harness kills (the MCP tool probes are the standing example). It
    is snapshotted before the kill, ended pinned after the group is gone, and
    the answer is True only once it too has exited -- otherwise the rmtree that
    follows races a live writer into the HOME."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
            " start_new_session=True)\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    escaped_pid = int(proc.stdout.readline().strip())
    assert os.getpgid(escaped_pid) != proc.pid, "the child must be outside the group"
    try:
        assert _terminate_process_group(proc) is True
        assert proc.poll() is not None
        assert not platform_compat.pid_exists(escaped_pid), "the escaped child outlived teardown"
    finally:
        with contextlib.suppress(OSError):
            os.kill(escaped_pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(timeout=10)


@_POSIX_ONLY
def test_a_reaped_leader_still_resolves_to_its_group(monkeypatch) -> None:
    """A dead LEADER is not a dead GROUP, and the GROUP is what gets torn down.

    ``os.getpgid`` cannot name a group whose leader has been reaped. Reading that
    lookup failure as "already gone" signals nothing at all while the children
    left in the group keep running — the POSIX twin of trusting an exited root on
    Windows. A leader's group id IS its pid, so that number still names it.

    POSIX-only like its siblings: ``os.getpgid`` does not exist on Windows, which
    is the deliberate loud failure for a caller using the wrong primitive there.
    """

    def _getpgid(pid: int) -> int:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(platform_compat.os, "getpgid", _getpgid, raising=False)

    assert platform_compat.pgroup_of_leader(4242) == 4242, (
        "a reaped leader must still resolve to its own group; nothing else will "
        "reach the children left in it"
    )


@_POSIX_ONLY
def test_an_unsignalable_group_resolves_to_nothing(monkeypatch) -> None:
    """Denied is not empty: the caller must not go on to signal a group it cannot see."""

    def _getpgid(pid: int) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(platform_compat.os, "getpgid", _getpgid, raising=False)

    assert platform_compat.pgroup_of_leader(4242) is None


@_POSIX_ONLY
def test_an_explicit_group_is_signalled_without_consulting_the_leader(monkeypatch) -> None:
    """A caller that spawned with ``start_new_session=True`` already knows the group.

    Passing it makes teardown independent of the leader's liveness, so the group
    resolver must not be consulted at all — patched here to fail the test if it is.
    """
    signalled: list[tuple[int, int]] = []

    def _boom(pid: int) -> int:
        raise AssertionError("the group must not be re-resolved when it is given")

    monkeypatch.setattr(platform_compat, "pgroup_of_leader", _boom)
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)), raising=False
    )

    terminate_pgid(4242, grace=0.01, pgid=9001)

    assert signalled, "the given group must actually be signalled"
    assert {p for p, _ in signalled} == {9001}, "only the group the caller named"


@_POSIX_ONLY
def test_terminate_falls_back_to_sigkill() -> None:
    """Process that ignores SIGTERM gets SIGKILL after the grace period.

    The child Python process registers ``SIG_IGN`` for SIGTERM, then
    prints ``READY`` to stdout. The parent reads that line before
    triggering the terminate — guaranteeing the handler is in place
    before SIGTERM lands, regardless of how slow the build host is at
    starting Python. Without that handshake the test races on cold
    sandboxes where Python startup exceeds an arbitrary sleep, lets
    SIGTERM reach the default disposition, and the child exits -15
    instead of being killed -9.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('READY', flush=True);"
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # Block until the child confirms the SIG_IGN handler is registered.
        assert proc.stdout is not None
        ready_line = proc.stdout.readline()
        assert ready_line == b"READY\n", f"child did not signal ready: {ready_line!r}"

        with patch("kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS", 0.5):
            _terminate_process_group(proc)
        assert proc.poll() is not None
        # On POSIX, SIGKILL is signal 9; returncode is -9 when killed.
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


# ── Windows teardown routing (asserted from every platform) ─────────────


def test_terminate_routes_through_process_tree_kill_on_windows() -> None:
    """On Windows the teardown must use ``kill_process_tree``, not the pgid path.

    Asserted with ``IS_WINDOWS`` patched rather than only on a Windows runner:
    the POSIX legs are the ones a contributor runs locally, and a reroute that
    quietly went back to ``terminate_pgid`` would otherwise only surface as an
    ``AttributeError`` from ``os.getpgid`` inside a Windows CI job.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    killed: list[int] = []
    try:
        with (
            patch("kiro_crew.testing.harness.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.testing.harness.platform_compat.kill_process_tree",
                side_effect=lambda pid, sig=None: killed.append(pid) or True,
            ),
            patch("kiro_crew.testing.harness.terminate_pgid") as pgid,
            patch("kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS", 0.2),
        ):
            _terminate_process_group(proc)
        assert killed == [proc.pid], "Windows teardown did not kill the process tree"
        assert not pgid.called, "Windows teardown reached the POSIX pgid primitive"
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_terminate_on_windows_survives_a_failed_tree_kill() -> None:
    """A ``taskkill`` failure is logged, not raised.

    Teardown runs in a ``finally``: letting a protected descendant or a
    transient access denial propagate would convert a PASSING test into an error
    whose traceback names the harness rather than the test.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        with (
            patch("kiro_crew.testing.harness.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.testing.harness.platform_compat.kill_process_tree",
                side_effect=PermissionError("taskkill rc=5 access denied"),
            ),
            patch("kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS", 0.2),
        ):
            _terminate_process_group(proc)  # must not raise
    finally:
        proc.kill()
        proc.wait(timeout=30)


# ── READY emitted in the same breath as the exit ─────────────────────────


def test_ready_line_returned_when_child_exits_immediately_after_printing() -> None:
    """A child that prints READY and exits at once still yields its payload.

    The child-exit check runs before the read, so a gateway whose exit lands in
    that window would be reported as "exited before READY" even though the
    line was already in the pipe. That is a wall-clock race (flake class 2), not
    a verdict, so the exit path drains what the reader captured first.
    """
    fake = FakePopen([f'{READY_PREFIX}{{"port": 7, "token": "t"}}\n'.encode()])
    fake.returncode = 0  # already exited, as a fast gateway would be

    result = _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=[])  # type: ignore[arg-type]

    assert result["port"] == 7


# ── terminate_pgid ──────────────────────────────────────────────────────


@_POSIX_ONLY
def test_terminate_pgid_noop_when_pid_gone() -> None:
    """A pid with no live process is a clean no-op (no exception)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # 30s: generous for `python -c pass`, but the package-builder fleet can
    # stall a fresh child process >5s under load (two consecutive Dry Run
    # failures on with TimeoutExpired at timeout=5).
    proc.wait(timeout=30)
    # Reaped — getpgid raises ProcessLookupError, terminate_pgid swallows it.
    terminate_pgid(proc.pid)


@_POSIX_ONLY
def test_terminate_pgid_sigkills_after_grace() -> None:
    """A process ignoring SIGTERM is SIGKILLed after the grace window.

    Mirrors ``test_terminate_falls_back_to_sigkill`` but exercises the
    pid-based primitive directly (the path an out-of-process supervisor
    takes). Handshakes on a READY line so the SIG_IGN handler is provably
    installed before the signal lands, avoiding a cold-host race.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('READY', flush=True);"
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        ready_line = proc.stdout.readline()
        assert ready_line == b"READY\n", f"child did not signal ready: {ready_line!r}"

        terminate_pgid(proc.pid, grace=0.5)
        # terminate_pgid does not reap; the owning Popen does.
        proc.wait(timeout=2)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


@_POSIX_ONLY
def test_terminate_pgid_graceful_exit_returns_fast() -> None:
    """A group that exits promptly on SIGTERM returns well under ``grace``.

    Pins the in-loop group-liveness early-return (the graceful path) and
    guards teardown latency: the poll must detect the exit and return in a
    fraction of the grace window, not burn it fully.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Reap concurrently so the child doesn't linger as a zombie (a zombie's
    # GROUP still reads alive to killpg(pgid, 0) until reaped) — models the
    # out-of-process case where init reaps the reparented child promptly.
    import threading

    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    try:
        start = time.monotonic()
        terminate_pgid(proc.pid, grace=5.0)
        elapsed = time.monotonic() - start
        assert elapsed < 2.5, f"graceful teardown burned the grace window: {elapsed:.2f}s"
        reaper.join(timeout=2)
        assert proc.returncode == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


@_POSIX_ONLY
def test_terminate_pgid_grace_default_resolved_at_call_time() -> None:
    """Patching ``TERMINATE_GRACE_SECONDS`` affects the default grace.

    The default is a ``None`` sentinel resolved inside the call, so tests
    (and out-of-process callers) that patch the module constant are honored —
    matching the behavior documented on ``_terminate_process_group``.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('READY', flush=True);"
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == b"READY\n"
        start = time.monotonic()
        with patch("kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS", 0.5):
            terminate_pgid(proc.pid)  # default grace — must pick up the patch
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"patched grace ignored, took {elapsed:.2f}s"
        proc.wait(timeout=2)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


@_POSIX_ONLY
def test_terminate_pgid_wait_hook_used_for_exit_detection() -> None:
    """A supplied ``wait`` hook replaces the pid poll for exit detection.

    The Popen-owning caller passes ``proc.wait`` so a graceful exit is seen
    immediately (even as an unreaped zombie). The hook returning without
    ``TimeoutExpired`` must mean no SIGKILL is sent to the (gone) group.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    calls: list[float] = []

    def hook(timeout: float) -> None:
        calls.append(timeout)
        proc.wait(timeout=timeout)  # real handle-based wait — reaps too

    try:
        start = time.monotonic()
        terminate_pgid(proc.pid, grace=5.0, wait=hook)
        elapsed = time.monotonic() - start
        assert calls == [5.0], "wait hook not invoked with the grace window"
        assert elapsed < 2.5, f"handle-based teardown burned grace: {elapsed:.2f}s"
        assert proc.returncode == -signal.SIGTERM  # graceful, not SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


# ── _resolve_workspace_src / GatewayHandle / _drain_stderr ──────────────


def test_resolve_workspace_src_finds_package() -> None:
    """When run from inside the package, returns ``<pkg>/src``."""
    src = _resolve_workspace_src()
    assert (src / "kiro_crew" / "__init__.py").exists()


def test_gateway_handle_is_frozen() -> None:
    handle = GatewayHandle(
        url="http://localhost:1/?token=t",
        port=1,
        token="t",
        home=Path("/tmp/x"),
        proc=None,  # type: ignore[arg-type]
    )
    with pytest.raises(FrozenInstanceError):
        handle.port = 2  # type: ignore[misc]


def test_drain_stderr_accumulates() -> None:
    """``_drain_stderr`` reads from a stream into the buffer until EOF."""
    stream = io.BytesIO(b"first chunk\nsecond chunk\n")
    # ``_drain_stderr`` calls ``stream.read1(4096)``; ``BytesIO`` lacks
    # read1, so wire it manually for the test.
    stream.read1 = lambda n: stream.read(n)  # type: ignore[attr-defined,assignment]

    buffer: list[bytes] = []
    _drain_stderr(stream, buffer)

    assert b"".join(buffer) == b"first chunk\nsecond chunk\n"


# ── spawn_feature_gateway: orchestration with mocked Popen ──────────────


def _make_fake_proc_with_ready(payload: str) -> FakePopen:
    """Build a FakePopen that emits a READY line and has a working stderr."""
    proc = FakePopen([f"{READY_PREFIX}{payload}\n".encode()])
    proc.stderr = io.BytesIO(b"")
    proc.stderr.read1 = lambda n: proc.stderr.read(n)  # type: ignore[attr-defined,assignment]
    return proc


def test_spawn_feature_gateway_happy_path() -> None:
    """End-to-end with mocked subprocess: spawn → READY → teardown.

    Doesn't spin a real gateway (covered by the gated integration test);
    instead patches ``subprocess.Popen`` so the orchestration logic in
    ``spawn_feature_gateway`` runs end-to-end with controlled output.
    """
    fake_proc = _make_fake_proc_with_ready(
        '{"port": 51234, "token": "t-abc", "pid": 9876, "home": "/tmp/x"}'
    )

    terminated: dict[str, bool] = {"called": False}

    def fake_terminate(_proc: object, _known: object) -> bool:
        terminated["called"] = True
        return True  # the child exited, so the HOME may go

    captured_cmd: list[list[str]] = []
    captured_env: dict[str, str] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> FakePopen:
        captured_cmd.append(cmd)
        env = kwargs["env"]
        assert isinstance(env, dict)
        captured_env.update(env)
        return fake_proc

    with (
        patch("kiro_crew.testing.harness.subprocess.Popen", side_effect=fake_popen),
        patch(
            "kiro_crew.testing.harness._terminate_process_group",
            side_effect=fake_terminate,
        ),
        patch(
            "kiro_crew.testing.harness.platform_compat.process_descendants",
            return_value=[],
        ),
    ):
        with spawn_feature_gateway(fixture="empty") as handle:
            assert handle.port == 51234
            assert handle.token == "t-abc"
            assert handle.url == "http://localhost:51234/?token=t-abc"
            assert handle.home.exists()  # tmp dir created by harness
            captured_home = handle.home

    assert terminated["called"] is True
    # Tmp home is cleaned up on context exit.
    assert not captured_home.exists()

    # Spawn invokes ``kirocrew gateway --test-mode --seed <fixture>`` —
    # seeding is atomic with gateway startup (no separate seed pass).
    assert captured_cmd, "Popen was not called"
    cmd_str = " ".join(captured_cmd[0])
    assert "gateway" in cmd_str
    assert "--test-mode" in cmd_str
    assert "--seed empty" in cmd_str
    assert "--approval reads" in cmd_str
    assert captured_env["KIROCREW_FAKE_ACP_TEST_MODE"] == "1"
    # ``crons`` defaults to False so the safe ``--no-crons`` flag is
    # included — a stray cron firing during an unrelated test is the
    # exact flake the default guards against.
    assert "--no-crons" in captured_cmd[0]


def test_spawn_feature_gateway_keeps_the_home_when_the_gateway_will_not_die() -> None:
    """A tree kill that did not end the gateway must not be followed by deleting
    the HOME it is still writing into. The harness logs the path and leaves it."""

    fake_proc = _make_fake_proc_with_ready(
        '{"port": 51234, "token": "t-abc", "pid": 9876, "home": "/tmp/x"}'
    )

    with (
        patch("kiro_crew.testing.harness.subprocess.Popen", return_value=fake_proc),
        patch(
            "kiro_crew.testing.harness._terminate_process_group",
            return_value=False,
        ),
        patch(
            "kiro_crew.testing.harness.platform_compat.process_descendants",
            return_value=[],
        ),
    ):
        with spawn_feature_gateway(fixture="empty") as handle:
            captured_home = handle.home

    try:
        assert captured_home.exists(), "HOME was deleted under a gateway that did not exit"
    finally:
        import shutil

        shutil.rmtree(captured_home, ignore_errors=True)


def test_spawn_feature_gateway_isolates_the_agent_spec_home() -> None:
    """The spawned gateway must write its agent specs under its OWN throwaway home.

    The gateway boot runs ``rebuild_agent_config``,
    which writes the managed MCP specs into ``kiro_agents_dir()``. With only
    ``KIROCREW_HOME`` isolated (the data home) and ``KIRO_HOME`` left at the default,
    that resolver names the operator's real machine-wide ``~/.kiro/agents`` -- and the
    spawned gateway is an ordinary (non-worktree) install, so ``agent.py``'s write
    guard takes the "writing its own shared home" branch and does NOT decline. It would
    then poison the real install's specs with this checkout's venv + a per-test data
    home, 403-ing every managed MCP call on the machine.

    The harness pins ``KIRO_HOME`` to ``<home>/kiro`` so ``kiro_agents_dir()`` resolves
    to exactly ``isolated_agents_dir(<home>)`` -- the dedicated dir the write guard's
    private-target exemption already lets an isolated instance own -- and the whole tree
    is removed with ``home`` on teardown.
    """
    from kiro_crew.config.paths import isolated_agents_dir

    fake_proc = _make_fake_proc_with_ready(
        '{"port": 51234, "token": "t-abc", "pid": 9876, "home": "/tmp/x"}'
    )
    captured_env: dict[str, str] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> FakePopen:
        env = kwargs["env"]
        assert isinstance(env, dict)
        captured_env.update(env)
        return fake_proc

    with (
        patch("kiro_crew.testing.harness.subprocess.Popen", side_effect=fake_popen),
        patch("kiro_crew.testing.harness._terminate_process_group"),
        patch(
            "kiro_crew.testing.harness.platform_compat.process_descendants",
            return_value=[],
        ),
    ):
        with spawn_feature_gateway(fixture="empty") as handle:
            data_home = handle.home

            # KIRO_HOME is set, points UNDER the gateway's throwaway data home, and is
            # NOT the operator's real kiro-cli home.
            assert "KIRO_HOME" in captured_env, "spawned gateway did not isolate KIRO_HOME"
            kiro_home = Path(captured_env["KIRO_HOME"])
            assert kiro_home == data_home / "kiro"
            real_home = Path.home() / ".kiro"
            assert kiro_home.resolve() != real_home.resolve()

            # The agents dir the gateway will write to (``<KIRO_HOME>/agents``) is
            # EXACTLY the write guard's private-target exemption for this instance's
            # own data home -- so the boot-time ``rebuild_agent_config`` is allowed to
            # write, but only into a directory this test rig owns and tears down.
            assert kiro_home / "agents" == isolated_agents_dir(data_home)

            # And the data home stays isolated too (unchanged by this fix).
            assert captured_env["KIROCREW_HOME"] == str(data_home)


def test_spawn_feature_gateway_crons_opt_in() -> None:
    """``crons=True`` drops ``--no-crons`` so the scheduler thread runs.

    Pins the API contract for cron-exercising tests (follow-up):
    setting ``crons=True`` MUST allow the gateway to start its scheduler;
    the harness has no other lever for tests that need ``cron_add`` /
    ``cron_remove`` to actually fire.
    """
    fake_proc = _make_fake_proc_with_ready(
        '{"port": 51234, "token": "t-abc", "pid": 9876, "home": "/tmp/x"}'
    )
    captured_cmd: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kw: object) -> FakePopen:
        captured_cmd.append(cmd)
        return fake_proc

    with (
        patch("kiro_crew.testing.harness.subprocess.Popen", side_effect=fake_popen),
        patch("kiro_crew.testing.harness._terminate_process_group"),
        patch(
            "kiro_crew.testing.harness.platform_compat.process_descendants",
            return_value=[],
        ),
    ):
        with spawn_feature_gateway(fixture="empty", crons=True):
            pass

    assert captured_cmd, "Popen was not called"
    assert "--no-crons" not in captured_cmd[0]
    # Sanity: rest of the test-mode shape is unchanged
    assert "--test-mode" in captured_cmd[0]
    assert "--seed" in captured_cmd[0]


def test_parallel_spawns_get_distinct_homes_and_ports() -> None:
    """Two concurrent spawns must each get their own tmp home + port.

    Pins the PRD acceptance "parallel invocations don't share
    KIROCREW_HOME or port". Mocks Popen so each call returns a fake
    process emitting a different READY payload; the harness must
    propagate the distinct ports and create separate tmp dirs.
    """
    procs = iter(
        [
            _make_fake_proc_with_ready(
                '{"port": 50001, "token": "t-A", "pid": 1, "home": "/ignored"}'
            ),
            _make_fake_proc_with_ready(
                '{"port": 50002, "token": "t-B", "pid": 2, "home": "/ignored"}'
            ),
        ]
    )

    with (
        patch(
            "kiro_crew.testing.harness.subprocess.Popen",
            side_effect=lambda *a, **kw: next(procs),
        ),
        patch("kiro_crew.testing.harness._terminate_process_group"),
        patch(
            "kiro_crew.testing.harness.platform_compat.process_descendants",
            return_value=[],
        ),
    ):
        with spawn_feature_gateway(fixture="empty") as outer:
            with spawn_feature_gateway(fixture="empty") as inner:
                # Distinct ports propagated from the (different) READY lines.
                assert outer.port != inner.port
                # Each invocation gets its own tmp home directory.
                assert outer.home != inner.home
                assert outer.home.exists()
                assert inner.home.exists()


def test_default_timeout_constant() -> None:
    """Sanity check: default timeout is the documented 60s."""
    assert DEFAULT_READY_TIMEOUT == 60.0


# ── End-to-end (gated) ──────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("KIROCREW_HARNESS_INTEGRATION"),
    reason=(
        "Real-gateway integration test. Set KIROCREW_HARNESS_INTEGRATION=1 to run. "
        "Requires the composable-gateway CLI flags (--test-mode + --seed) to be "
        "present on the local feature branch."
    ),
)
def test_spawn_real_gateway_round_trip() -> None:
    """End-to-end: spawn, hit URL, exit, assert process is gone."""
    import urllib.error
    import urllib.request

    with spawn_feature_gateway(fixture="empty") as handle:
        assert handle.port > 0
        assert handle.token
        assert handle.home.exists()
        # Hit the dashboard root. ``urllib.request.urlopen`` follows
        # redirects automatically and raises ``HTTPError`` for any 4xx/5xx,
        # so the success path is just a 200 here. Auth-related rejections
        # surface as ``HTTPError`` and are also acceptable — what we're
        # really testing is that the gateway is reachable, not the
        # specific response policy of an unauthenticated request.
        req = urllib.request.Request(handle.url)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403)
        proc = handle.proc

    # Outside the with-block: process is gone, home is removed.
    assert proc.poll() is not None
    assert not handle.home.exists()


# --------------------------------------------------------------------------- #
# The Windows backend shim. Its whole job is to survive cmd.exe's own parsing,
# and every rule below was added because the naive spelling breaks on a real
# path -- so each one is asserted rather than left to the E2E run, which only
# ever sees the happy path on a runner whose paths are plain ASCII.
# --------------------------------------------------------------------------- #
def test_the_backend_shim_is_the_module_path_itself_off_windows(tmp_path: Path) -> None:
    """No shim where none is needed: POSIX spawns the module file directly."""
    from kiro_crew.testing import harness as h

    with patch.object(h.platform_compat, "IS_WINDOWS", False):
        produced = h.fake_acp_backend_launcher(tmp_path)

    assert produced.name == "fake_acp_backend.py"
    assert not (tmp_path / "kiro-backend.cmd").exists()


def test_the_backend_shim_is_written_in_the_console_code_page() -> None:
    """The codec choice, pinned on its own because it cannot be exercised elsewhere.

    ``oem`` exists only on Windows, so a test that WRITES the shim can only assert
    the escaping rules (which are platform-independent) and not this. cmd.exe
    decodes a batch file in the console's OEM code page: ``ascii`` raises outright
    on a non-ASCII interpreter path, and ``utf-8`` hands cmd.exe mojibake for one.
    """
    from kiro_crew.testing import harness as h

    assert h.SHIM_ENCODING == "oem"


def test_the_backend_shim_doubles_percent_so_cmd_cannot_expand_the_path(tmp_path: Path) -> None:
    """``%`` is legal in a Windows path and is cmd.exe's variable sigil.

    An interpreter under a directory containing ``%`` would otherwise be
    substituted away and the shim would boot whatever the empty expansion named,
    or nothing at all. Doubling is what makes cmd.exe emit one literal ``%``.

    The codec is redirected because ``oem`` does not exist off Windows, and the
    rule under test is the ESCAPING, which is the same on every platform. The real
    codec is pinned by the test above, so nothing is lost by not using it here.
    """
    from kiro_crew.testing import harness as h

    interpreter = r"C:\tools\100%python\python.exe"
    with patch.object(h, "SHIM_ENCODING", "utf-8"):
        with patch.object(h.platform_compat, "IS_WINDOWS", True):
            with patch.object(h.sys, "executable", interpreter):
                produced = h.fake_acp_backend_launcher(tmp_path)

    assert produced.name == "kiro-backend.cmd"
    raw = produced.read_bytes()
    assert rb"100%%python" in raw, "each % must be doubled for cmd.exe"
    assert b"100%python" not in raw.replace(rb"100%%python", b""), "no bare % may survive"
    # Asserted on the BYTES: a text read applies universal newlines, so it cannot
    # see what cmd.exe actually reads, and CRLF is what cmd.exe needs to not
    # mis-parse the last line.
    assert raw.endswith(b"\r\n")


def test_the_backend_shim_refuses_a_path_cmd_cannot_quote(tmp_path: Path) -> None:
    """Refuse, rather than write a shim that boots a DIFFERENT path.

    A double quote or a newline in the interpreter path cannot be escaped inside
    a batch file, so the alternatives are a loud harness error or a silent boot of
    the wrong executable. The message has to name the path, because the operator
    cannot see the generated file.
    """
    from kiro_crew.testing import harness as h

    for hostile in ('C:\\to"ols\\python.exe', "C:\\tools\\py\nthon.exe"):
        with patch.object(h.platform_compat, "IS_WINDOWS", True):
            with patch.object(h.sys, "executable", hostile):
                with pytest.raises(GatewaySpawnError) as excinfo:
                    h.fake_acp_backend_launcher(tmp_path)

        assert "cannot quote" in str(excinfo.value)
        assert not (
            tmp_path / "kiro-backend.cmd"
        ).exists(), "a refused shim must leave no file behind for a later run to pick up"


class TestTeardownDoesNotTrustTheRootsExit:
    """An exited root is not a tree that is gone, and teardown must not say it is.

    The caller spends this verdict on whether it may ``rmtree`` the gateway's HOME.
    A gateway that crashes mid-test while a child it spawned keeps running would,
    with a ``proc.poll()`` short-circuit, be reported as fully torn down and the
    directory removed under a live writer.
    """

    class _ExitedProc:
        pid = 4242
        returncode = 1

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

    class _LiveProc:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def test_the_windows_sweep_still_runs_for_an_exited_root(self, monkeypatch) -> None:
        from kiro_crew.testing import harness as h

        swept: list[int] = []
        monkeypatch.setattr(h.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            h,
            "_terminate_process_tree_windows",
            lambda p, known=None: swept.append(p.pid) or True,
        )

        verdict = h._terminate_process_group(self._ExitedProc())

        assert swept == [4242], "the descendant sweep must not be skipped for a dead root"
        assert verdict is False, "an exited root cannot certify the HOME as safe to remove"

    def test_an_exited_root_with_no_snapshot_never_calls_killpg(self, monkeypatch) -> None:
        """A recycled root pid cannot be used as a process-group identity."""
        from kiro_crew.testing import harness as h

        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(h.platform_compat, "IS_WINDOWS", False)
        # raising=False: this pins the POSIX path under a pinned IS_WINDOWS=False,
        # and the test also runs on Windows, where ``os`` has no ``killpg`` to
        # replace -- the recorder is installed there rather than swapped in.
        monkeypatch.setattr(
            h.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)), raising=False
        )

        verdict = h._terminate_process_group(self._ExitedProc(), {})

        assert calls == []
        assert verdict is False, "an exited root cannot certify the HOME as safe to remove"

    def test_the_group_teardowns_own_answer_is_the_verdict(self, monkeypatch) -> None:
        """Reaping the leader says nothing about the group, and the group is the verdict.

        The caller spends this answer on whether it may ``rmtree`` the gateway's
        HOME, so a group teardown that could not carry itself out must read as a
        tree that is NOT gone — the same way the Windows sibling above takes its
        verdict from the sweep rather than from the root's exit. The group
        teardown polls the group itself, so its answer is read rather than
        re-probed here.
        """
        from kiro_crew.testing import harness as h

        monkeypatch.setattr(h.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(h.platform_compat, "process_descendants", lambda pid: [])
        monkeypatch.setattr(
            h, "terminate_pgid", lambda pid, grace=None, pgid=None, wait=None: False
        )

        verdict = h._terminate_process_group(self._LiveProc())

        assert verdict is False, (
            "a group teardown that could not certify itself must not be reported as a "
            "torn-down tree; the caller would rmtree the HOME under a live writer"
        )

    def test_a_live_root_names_its_process_group_explicitly(self, monkeypatch) -> None:
        """The group id is passed rather than derived through another pid lookup."""
        from kiro_crew.testing import harness as h

        seen: list[object] = []
        monkeypatch.setattr(h.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(h.platform_compat, "process_descendants", lambda pid: [])
        monkeypatch.setattr(h, "_await_group_gone", lambda pgid: True)
        monkeypatch.setattr(
            h,
            "terminate_pgid",
            lambda pid, grace=None, pgid=None, wait=None: (seen.append(pgid), True)[1],
        )

        h._terminate_process_group(self._LiveProc())

        assert seen == [4242], (
            "the caller spawned with start_new_session=True, so it knows the group id "
            "and must name it rather than leave it to be looked up"
        )


@_POSIX_ONLY
def test_dead_root_ends_saved_in_group_child() -> None:
    """A saved in-group child is ended without trusting the dead root's pgid."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(c.pid, flush=True)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    child_pid = int(proc.stdout.readline().strip())
    token = platform_compat.process_start_time(child_pid)
    assert token is not None
    proc.wait(timeout=10)
    try:
        assert _terminate_process_group(proc, {child_pid: token}) is False
        assert not platform_compat.pid_exists(child_pid), "the saved child outlived teardown"
    finally:
        with contextlib.suppress(OSError):
            os.kill(child_pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(timeout=10)


@_POSIX_ONLY
def test_terminate_preserves_home_after_root_exits() -> None:
    """A saved detached child ends, but a crashed root cannot certify cleanup.

    Reparenting removes the child from a teardown-time descendant walk. The
    pinned snapshot still lets teardown end that child, while the fail-closed
    verdict keeps the scratch HOME because later descendants are unknowable.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
            " start_new_session=True)\n"
            "print(c.pid, flush=True)\n"
            "sys.stdin.readline()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    escaped_pid = int(proc.stdout.readline().strip())
    token = platform_compat.process_start_time(escaped_pid)
    assert token is not None
    assert escaped_pid in platform_compat.process_descendants(proc.pid)
    try:
        proc.stdin.write(b"exit\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
        assert escaped_pid not in platform_compat.process_descendants(proc.pid)

        assert _terminate_process_group(proc, {escaped_pid: token}) is False
        assert not platform_compat.pid_exists(escaped_pid), "the saved child outlived teardown"
    finally:
        with contextlib.suppress(OSError):
            os.kill(escaped_pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(timeout=10)

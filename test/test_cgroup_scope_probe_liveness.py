"""The cgroup-scope probe must follow the systemd user manager, not outlive it.

``cgroup_scope_argv`` prepends ``systemd-run --user --scope`` whenever
``_probe_cgroup_scope`` says the host can enforce the per-spawn ceiling.

On a host without lingering, logind stops the per-user manager and removes
``/run/user/<uid>`` when the last login session ends, while ``XDG_RUNTIME_DIR``
stays set in the gateway's environment. A probe answer cached from before the
logout would keep wrapping every spawn in ``systemd-run --user``, which cannot
reach any bus and dies with ``Failed to connect to bus: No such file or
directory`` before exec'ing the wrapped command -- every agent respawn, cron
script and app backend would fail until a restart. The probe must instead give
a long-running gateway the same answer a gateway started after the logout gets.

These tests stand up a FAKE user manager: a real listening ``AF_UNIX`` socket in a
runtime directory under ``tmp_path``, plus a fake delegated-controllers file. The
manager "vanishing" is that directory being removed, exactly what logind does. The
real user manager on the host running the suite is never contacted, stopped or
reconfigured: the session floor in the rootdir conftest already strips the real
bus locators, and every path here lives under ``tmp_path``.
"""

from __future__ import annotations

import builtins
import logging
import os
import shutil
import socket
import sys

import pytest

import kiro_crew.sandbox as sb

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="cgroup v2 scopes and the user bus are Linux-only"
)

_REAL_OPEN = builtins.open
_REAL_TRUSTED_SYSTEM_BIN_QUIET = sb.platform_compat.trusted_system_bin_quiet


class _FakeUserManager:
    """A stand-in for the per-user systemd manager's runtime footprint.

    ``start()`` creates the runtime directory with a LISTENING bus socket and the
    user slice's ``cgroup.controllers``; ``stop()`` removes both, which is what
    logind does to ``/run/user/<uid>`` and ``user-<uid>.slice`` at the last logout.
    """

    def __init__(self, root) -> None:
        self.runtime_dir = str(root / "rt")
        self.bus_path = os.path.join(self.runtime_dir, "bus")
        self.slice_dir = str(root / "slice")
        self.controllers = os.path.join(self.slice_dir, "cgroup.controllers")
        self._listener: socket.socket | None = None

    def start(self) -> None:
        os.makedirs(self.runtime_dir, exist_ok=True)
        os.makedirs(self.slice_dir, exist_ok=True)
        with _REAL_OPEN(self.controllers, "w", encoding="utf-8") as fh:
            fh.write("cpu memory pids\n")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.bus_path)
        listener.listen(8)
        self._listener = listener

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        shutil.rmtree(self.slice_dir, ignore_errors=True)

    def leave_stale_socket(self) -> None:
        """The manager died but its socket file stayed behind (nothing listens)."""
        if self._listener is not None:
            self._listener.close()
            self._listener = None


@pytest.fixture
def fake_manager(tmp_path, monkeypatch):
    # AF_UNIX paths are capped at 107 bytes; a deep basetemp would make bind()
    # fail for a reason unrelated to the code under test.
    if len(str(tmp_path / "rt" / "bus")) > 100:
        pytest.skip(f"tmp_path too long for an AF_UNIX socket: {tmp_path}")
    mgr = _FakeUserManager(tmp_path)
    uid = os.getuid()
    real_controllers = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
    cgroup_file = tmp_path / "proc-self-cgroup"
    cgroup_file.write_text("0::/user.slice/user-1.slice/session-1.scope\n", encoding="utf-8")

    def _open(path, *args, **kwargs):
        # Redirect ONLY the two host files the probe reads; everything else is real.
        if path == "/proc/self/cgroup":
            return _REAL_OPEN(cgroup_file, *args, **kwargs)
        if path == real_controllers:
            return _REAL_OPEN(mgr.controllers, *args, **kwargs)
        return _REAL_OPEN(path, *args, **kwargs)

    real_which = shutil.which

    def _which(name, *args, **kwargs):
        if name == "systemd-run":
            return "/usr/bin/systemd-run"
        return real_which(name, *args, **kwargs)

    real_trusted = sb.platform_compat.trusted_system_bin

    def _trusted(name):
        if name == "systemd-run":
            return "/usr/bin/systemd-run"
        return real_trusted(name)

    monkeypatch.setattr(sb, "open", _open, raising=False)
    monkeypatch.setattr(sb.shutil, "which", _which)
    monkeypatch.setattr(sb.platform_compat, "trusted_system_bin", _trusted)
    monkeypatch.setattr(sb.platform_compat, "trusted_system_bin_quiet", _trusted)
    # The slice-level reconcile shells out to systemctl; it is not under test.
    monkeypatch.setattr(sb, "_reconcile_slice_memory_high_off_thread", lambda: None)
    monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda: (8192, 8192, 50, 0))
    # The gateway's environment as a login shell leaves it: the locators stay SET
    # after logind removes the directory they name.
    monkeypatch.setenv("XDG_RUNTIME_DIR", mgr.runtime_dir)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={mgr.bus_path}")
    for name in ("_CGROUP_SCOPE_PROBE", "_CPU_DELEGATED"):
        monkeypatch.setattr(sb, name, None)
    monkeypatch.setattr(sb, "_CGROUP_WARNED", False)
    mgr.start()
    try:
        yield mgr
    finally:
        mgr.stop()
        sb._CGROUP_SCOPE_PROBE = None
        sb._CPU_DELEGATED = None


def _is_scope_wrapped(argv: list[str]) -> bool:
    return bool(argv) and os.path.basename(argv[0]) == "systemd-run"


def test_logout_after_probe_stops_wrapping_spawns_in_systemd_run(fake_manager, caplog):
    """The reported sequence: probe while logged in, then the manager goes away.

    A cached ``(True, "ok")`` that survives the logout wraps this spawn in
    ``systemd-run --user``, which dies with ``Failed to connect to bus``. The
    spawn must instead take the same no-scope path a gateway started after the
    logout takes, and say so loudly, naming the remedy.
    """
    assert sb._probe_cgroup_scope() == (True, "ok")
    assert _is_scope_wrapped(sb.cgroup_scope_argv(["kiro-cli", "acp"]))

    fake_manager.stop()  # logind: last session ended, /run/user/<uid> removed

    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        out = sb.cgroup_scope_argv(["kiro-cli", "acp"])

    assert out == ["kiro-cli", "acp"], out
    available, reason = sb._probe_cgroup_scope()
    assert available is False
    assert "enable-linger" in reason
    # No bus address may be forwarded to a spawn that is not wrapped.
    assert sb.cgroup_scope_bus_env({}) == ({}, ())
    security = [r.getMessage() for r in caplog.records if "SECURITY" in r.getMessage()]
    assert security, "the ceiling was dropped silently"
    assert any("enable-linger" in m for m in security), security


def test_ceiling_returns_when_the_manager_comes_back(fake_manager):
    """Re-login recreates the runtime dir; spawns must be bounded again without a restart."""
    assert _is_scope_wrapped(sb.cgroup_scope_argv(["true"]))
    fake_manager.stop()
    assert sb.cgroup_scope_argv(["true"]) == ["true"]

    fake_manager.start()

    out = sb.cgroup_scope_argv(["true"])
    assert _is_scope_wrapped(out), out
    assert "TasksMax=8192" in out and "MemoryMax=8192M" in out


def test_transition_warns_once_not_per_spawn(fake_manager, caplog):
    sb.cgroup_scope_argv(["true"])
    fake_manager.stop()
    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        for _ in range(5):
            assert sb.cgroup_scope_argv(["true"]) == ["true"]
    security = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security) == 1, [r.getMessage() for r in security]


def test_a_second_outage_is_reported_too(fake_manager, caplog):
    """The one-time startup warning must not swallow a LATER loss of the ceiling.

    Gateway starts with no manager (warned once), the user logs in (ceiling
    back), then logs out again: that second drop is a new event the log must show.
    """
    fake_manager.stop()
    assert sb.cgroup_scope_argv(["true"]) == ["true"]  # startup: the one-time warning
    assert sb._CGROUP_WARNED is True
    fake_manager.start()
    with caplog.at_level(logging.INFO, logger=sb.logger.name):
        assert _is_scope_wrapped(sb.cgroup_scope_argv(["true"]))
        assert any("available again" in r.getMessage() for r in caplog.records)
        caplog.clear()
        fake_manager.stop()
        assert sb.cgroup_scope_argv(["true"]) == ["true"]
    security = [r.getMessage() for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security) == 1 and "enable-linger" in security[0], security


def test_bus_gone_but_slice_still_delegated_is_unavailable(fake_manager):
    """Only the bus disappears (the slice outlives it): still no wrapping.

    Isolates the bus gate from the controllers gate, so neither can stand in
    for the other.
    """
    assert sb._probe_cgroup_scope()[0] is True
    fake_manager.leave_stale_socket()
    os.unlink(fake_manager.bus_path)
    assert os.path.exists(fake_manager.controllers)

    assert sb.cgroup_scope_argv(["true"]) == ["true"]


def test_stale_bus_socket_with_no_listener_is_unavailable(fake_manager):
    """A socket FILE is not a bus: the first probe must connect, not stat."""
    fake_manager.leave_stale_socket()
    assert os.path.exists(fake_manager.bus_path)

    available, reason = sb._probe_cgroup_scope()

    assert available is False, reason
    assert sb.cgroup_scope_argv(["true"]) == ["true"]


def test_bus_connect_refused_by_policy_is_unavailable(fake_manager, monkeypatch):
    """A seccomp/LSM filter that refuses connect() with EPERM."""
    real_connect = socket.socket.connect_ex

    def _deny(self, address):
        if self.family == socket.AF_UNIX:
            return 1  # EPERM
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect_ex", _deny)

    available, reason = sb._probe_cgroup_scope()

    assert available is False, reason


def test_a_listener_with_a_full_backlog_is_reachable_and_never_blocks(fake_manager):
    """A busy bus answers a non-blocking connect() with EAGAIN; that is still a bus.

    Treating it as unreachable would drop the ceiling under load, and a BLOCKING
    connect would stall the event loop the spawn runs on until the bus drained.
    """
    fake_manager._listener.listen(0)
    fillers = []
    try:
        for _ in range(64):
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            filler.setblocking(False)
            fillers.append(filler)
            if filler.connect_ex(fake_manager.bus_path) != 0:
                break
        else:
            pytest.skip("could not saturate the AF_UNIX backlog on this kernel")

        assert sb._probe_cgroup_scope() == (True, "ok")
    finally:
        for filler in fillers:
            filler.close()


def test_a_percent_escaped_bus_address_is_decoded(fake_manager, monkeypatch, tmp_path):
    """D-Bus addresses may escape any byte; the escaped spelling names the same socket.

    ``XDG_RUNTIME_DIR`` points somewhere with no sockets, so the only way to
    find the live bus is to decode the address.
    """
    empty = tmp_path / "empty-rt"
    empty.mkdir()
    escaped = "".join(
        f"%{b:02x}" if chr(b) == "/" else chr(b) for b in fake_manager.bus_path.encode()
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(empty))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={escaped},guid=0123")

    assert sb._probe_cgroup_scope() == (True, "ok")


def test_a_probe_refresh_never_walks_path(fake_manager, monkeypatch):
    """A refresh runs on the event loop; it must not resolve ``systemd-run`` through PATH.

    A PATH entry on a stalled mount would hang ``shutil.which`` and with it the
    gateway's loop. The probe uses the same fixed-directory lookup the wrapper does.
    """

    def _no_path_walk(*_args, **_kwargs):
        raise AssertionError("the probe walked PATH")

    monkeypatch.setattr(sb.shutil, "which", _no_path_walk)
    clock = [1000.0]
    monkeypatch.setattr(sb.time, "monotonic", lambda: clock[0])
    assert sb._probe_cgroup_scope() == (True, "ok")
    clock[0] += sb._CGROUP_SCOPE_PROBE_TTL_SECONDS + 1
    assert sb._probe_cgroup_scope() == (True, "ok")


def test_a_probe_miss_never_walks_path(fake_manager, monkeypatch, tmp_path):
    """No trusted ``systemd-run``: the miss must not fall through to a PATH walk.

    ``trusted_system_bin``'s miss diagnostic calls ``shutil.which``; on the
    event loop that can hang on a stalled mount just like a direct PATH lookup.
    """
    empty_bin = tmp_path / "no-bin"
    empty_bin.mkdir()

    def _no_path_walk(*_args, **_kwargs):
        raise AssertionError("the probe walked PATH")

    monkeypatch.setattr(
        sb.platform_compat, "trusted_system_bin_quiet", _REAL_TRUSTED_SYSTEM_BIN_QUIET
    )
    monkeypatch.setattr(sb.platform_compat, "_TRUSTED_SYSTEM_BIN_DIRS", (str(empty_bin),))
    monkeypatch.setattr(sb.platform_compat, "_UNPINNED_TOOL_PROBED", set())
    monkeypatch.setattr(sb.platform_compat.shutil, "which", _no_path_walk)

    available, reason = sb._probe_cgroup_scope()

    assert available is False
    assert "trusted system directory" in reason


@pytest.mark.parametrize("address", ["unix:path=%00", "unix:path=/run/x%00y", "unix:path="])
def test_a_bus_address_naming_no_socket_never_breaks_a_spawn(fake_manager, monkeypatch, address):
    """A decoded NUL cannot name a socket; it is skipped, not passed to ``os.stat``."""
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)

    assert sb._probe_cgroup_scope() == (True, "ok")  # the XDG_RUNTIME_DIR bus is live
    assert _is_scope_wrapped(sb.cgroup_scope_argv(["true"]))


def test_unchanged_session_reuses_the_cached_probe(fake_manager, monkeypatch):
    """The per-spawn check is a stat, not a re-probe: an unchanged host computes once."""
    calls = []
    real_compute = sb._compute_cgroup_scope_probe

    def _counting():
        calls.append(1)
        return real_compute()

    monkeypatch.setattr(sb, "_compute_cgroup_scope_probe", _counting)
    for _ in range(20):
        assert sb._probe_cgroup_scope() == (True, "ok")
    assert len(calls) == 1

    fake_manager.stop()
    for _ in range(20):
        assert sb._probe_cgroup_scope()[0] is False
    assert len(calls) == 2


def test_cached_probe_is_rechecked_after_its_ttl(fake_manager, monkeypatch):
    """A manager that dies without removing its socket changes no fingerprint.

    The TTL is what catches it: past the TTL the probe recomputes (and its
    connect() finds no listener) even though every stat looks the same.
    """
    clock = [1000.0]
    monkeypatch.setattr(sb.time, "monotonic", lambda: clock[0])
    assert sb._probe_cgroup_scope() == (True, "ok")
    fake_manager.leave_stale_socket()

    assert sb._probe_cgroup_scope() == (True, "ok")  # within the TTL: cached
    clock[0] += sb._CGROUP_SCOPE_PROBE_TTL_SECONDS + 1

    assert sb._probe_cgroup_scope()[0] is False


def test_cpu_delegation_is_reread_with_the_session(fake_manager):
    """``_cpu_controller_delegated`` was cached for life too; it must follow the probe."""
    assert sb._cpu_controller_delegated() is True
    fake_manager.stop()
    sb._probe_cgroup_scope()
    fake_manager.start()
    with _REAL_OPEN(fake_manager.controllers, "w", encoding="utf-8") as fh:
        fh.write("memory pids\n")
    assert sb._probe_cgroup_scope() == (True, "ok")
    assert sb._cpu_controller_delegated() is False
    assert not any(a.startswith("CPUWeight=") for a in sb.cgroup_scope_argv(["true"]))

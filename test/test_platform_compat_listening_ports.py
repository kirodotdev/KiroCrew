"""Port listeners: the ``lsof``, ``/proc`` and Windows table reads, and loopback owners.

``find_listening_pids`` and the listener probes over canned tool output and
their failure answers, including a real IPv6 loopback listener on Windows that
CI's IPv6 lane runs; which addresses cover loopback; and the most-specific-bind
tiers of ``loopback_owner_pids``. A topic of the ``test_platform_compat``
family; see ``test_platform_compat.py``.
"""

from __future__ import annotations

import os
import subprocess
import types

import pytest
from platform_compat_helpers import _fake_windows_bins

from kiro_crew import platform_compat as pc

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestFindListeningPidsErrors:
    def test_returns_empty_when_lsof_missing(self, monkeypatch):
        # Simulate lsof not being installed: check_output raises
        # FileNotFoundError -> the except returns [] (fail-closed).
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")

        def no_lsof(*args, **kwargs):
            raise FileNotFoundError("lsof")

        monkeypatch.setattr(pc.subprocess, "check_output", no_lsof)
        assert pc.find_listening_pids(59998) == []

    def test_dedupes_pids_from_lsof_output(self, monkeypatch):
        # lsof can emit the same (pid, address) socket multiple times (one row
        # per fd) and one PID can hold several addresses on the port; the PID
        # accessor must dedupe while preserving first-seen order.
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        blob = "p111\nn127.0.0.1:7777\nn127.0.0.1:7777\nn*:7777\np222\nn[::1]:7777\n"
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: blob)
        assert pc.find_listening_pids(7777) == [111, 222]

    def test_posix_listeners_carry_their_local_address(self, monkeypatch):
        # The lsof -Fptn field output attributes each LISTEN socket's local
        # address AND family to its owning PID, so callers can scope ownership
        # to the address they actually probed (family is what tells the two
        # wildcard binds apart — lsof prints both as ``*``). v6 brackets are
        # stripped; rows for a different port (defensive — the -i filter
        # already scopes) and malformed p-lines are ignored.
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        blob = (
            "p111\n"
            "tIPv4\n"
            "n127.0.0.1:7777\n"
            "p222\n"
            "tIPv6\n"
            "n[::1]:7777\n"
            "tIPv4\n"
            "n192.168.1.5:7777\n"
            "pbogus\n"
            "n10.0.0.1:7777\n"
            "p333\n"
            "tIPv4\n"
            "n*:7778\n"
        )
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: blob)
        assert pc.find_port_listeners(7777) == [
            pc.PortListener(111, "127.0.0.1", "4"),
            pc.PortListener(222, "::1", "6"),
            pc.PortListener(222, "192.168.1.5", "4"),
        ]

    def test_posix_no_match_is_a_completed_empty_probe(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/usr/bin/lsof")

        def _no_match(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv, output="")

        monkeypatch.setattr(pc.subprocess, "check_output", _no_match)
        listeners, completed = pc.probe_port_listeners(7777)
        assert listeners == []
        assert completed is True

    def test_posix_stderr_only_exit_is_not_completed_nonownership(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/usr/bin/lsof")
        captured: dict = {}

        def _failure(argv, **kwargs):
            captured["stderr"] = kwargs.get("stderr")
            raise subprocess.CalledProcessError(
                1,
                argv,
                output="",
                stderr="permission denied",
            )

        monkeypatch.setattr(pc.subprocess, "check_output", _failure)

        listeners, completed = pc.probe_port_listeners(7777, process_pid=4242)

        assert listeners == []
        assert completed is False
        assert captured["stderr"] is subprocess.PIPE

    def test_posix_lookup_timeout_is_not_a_completed_probe(self, monkeypatch):
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        captured: dict = {}

        def _capture(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        monkeypatch.setattr(pc.subprocess, "check_output", _capture)
        listeners, completed = pc.probe_port_listeners(7777)
        assert listeners == []
        assert completed is False
        assert captured["kwargs"].get("timeout") == pc._LSOF_TIMEOUT_SECS

    def test_linux_process_listener_probe_matches_the_child_socket_inode(
        self, tmp_path, monkeypatch
    ):
        proc_root = tmp_path / "proc"
        (proc_root / "net").mkdir(parents=True)
        (proc_root / "4242" / "fd").mkdir(parents=True)
        (proc_root / "4242" / "fd" / "7").touch()
        (proc_root / "net" / "tcp").write_text(
            "header\n"
            "0: 0100007F:1E61 00000000:0000 0A 00000000:00000000 "
            "00:00000000 00000000 1000 0 12345\n",
            encoding="ascii",
        )
        (proc_root / "net" / "tcp6").write_text("header\n", encoding="ascii")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "readlink", lambda path: "socket:[12345]")

        assert pc.process_owns_loopback_listener(4242, 7777, proc_root=proc_root) is True

    def test_linux_process_listener_probe_rejects_an_inode_the_child_does_not_hold(
        self, tmp_path, monkeypatch
    ):
        proc_root = tmp_path / "proc"
        (proc_root / "net").mkdir(parents=True)
        (proc_root / "4242" / "fd").mkdir(parents=True)
        (proc_root / "4242" / "fd" / "7").touch()
        (proc_root / "net" / "tcp").write_text(
            "header\n"
            "0: 0100007F:1E61 00000000:0000 0A 00000000:00000000 "
            "00:00000000 00000000 1000 0 12345\n",
            encoding="ascii",
        )
        (proc_root / "net" / "tcp6").write_text("header\n", encoding="ascii")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "readlink", lambda path: "socket:[99999]")

        assert pc.process_owns_loopback_listener(4242, 7777, proc_root=proc_root) is False

    def test_posix_process_listener_probe_scopes_lsof_to_the_child_pid(self, monkeypatch):
        captured: dict = {}
        blob = "p4242\ntIPv4\nn127.0.0.1:7777\n"
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/usr/bin/lsof")

        def _capture(argv, **kwargs):
            captured["argv"] = list(argv)
            return blob

        monkeypatch.setattr(pc.subprocess, "check_output", _capture)

        assert pc.process_owns_loopback_listener(4242, 7777) is True
        assert captured["argv"] == [
            "/usr/bin/lsof",
            "-nP",
            "-a",
            "-p",
            "4242",
            "-iTCP:7777",
            "-sTCP:LISTEN",
            "-Fptn",
        ]

    def test_windows_listener_owner_pids_use_in_process_tcp_tables(self, monkeypatch):
        rows = {
            False: [
                types.SimpleNamespace(
                    state=2,
                    local_address=bytes([127, 0, 0, 1]),
                    local_scope_id=0,
                    local_port=7777,
                    pid=99_999_999_991,
                ),
                types.SimpleNamespace(
                    state=2,
                    local_address=bytes([0, 0, 0, 0]),
                    local_scope_id=0,
                    local_port=7777,
                    pid=99_999_999_992,
                ),
            ],
            True: [
                types.SimpleNamespace(
                    state=2,
                    local_address=bytes(16),
                    local_scope_id=0,
                    local_port=7777,
                    pid=99_999_999_993,
                )
            ],
        }
        calls = []

        def _rows(ipv6, table_class):
            calls.append((ipv6, table_class))
            return rows[ipv6]

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_tcp_owner_rows", _rows, raising=False)

        assert pc._windows_loopback_listener_owner_pids(7777) == {99_999_999_991}
        assert calls == [(False, 3), (True, 3)]

        rows[True] = None
        assert pc._windows_loopback_listener_owner_pids(7777) is None

    @pytest.mark.parametrize(
        ("owners", "expected"),
        [
            pytest.param({4242}, True, id="owned"),
            pytest.param({7777}, False, id="foreign"),
            pytest.param(None, None, id="table-failure"),
        ],
    )
    def test_windows_process_listener_probe_uses_owner_pid_table(
        self,
        monkeypatch,
        owners,
        expected,
    ):
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc,
            "_windows_loopback_listener_owner_pids",
            lambda port: owners,
            raising=False,
        )
        monkeypatch.setattr(
            pc,
            "probe_port_listeners",
            lambda *args, **kwargs: pytest.fail("netstat must not be used"),
        )

        assert pc.process_owns_loopback_listener(4242, 7777) is expected

    def _fake_netstat(self, blob: str):
        """Return a fake subprocess.check_output that returns *blob*."""

        def _run(*_a, **_kw):
            return blob

        return _run

    def test_windows_lookup_error_is_not_a_completed_probe(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        def _boom(*args, **kwargs):
            raise OSError("netstat unavailable")

        monkeypatch.setattr(pc.subprocess, "check_output", _boom)
        listeners, completed = pc.probe_port_listeners(7777)
        assert listeners == []
        assert completed is False

    def test_windows_finds_ipv6_listener_via_netstat(self, monkeypatch):
        # Regression:. Windows netstat -ano prints IPv6 LISTEN rows
        # with proto column "TCP" (NOT "TCP6") and address form [::1]:<port>.
        # Before this fix `-p tcp` on the netstat argv dropped these entirely,
        # so `kirocrew stop` / `kirocrew restart` silently no-op'd when the
        # gateway bound v6. This canned blob mirrors what real Windows netstat
        # actually prints (verified on Windows 11 24H2 with an AF_INET6
        # loopback listener) — regression-guards without a Windows CI lane.
        blob = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    [::1]:7777             [::]:0                 LISTENING       12345\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [12345]

    def test_windows_dedupes_dualstack_v4_and_v6_rows(self, monkeypatch):
        # A dual-stack listener shows up as TWO netstat rows sharing a PID
        # (very common for aiohttp / http.server with an empty host). Existing
        # dict.fromkeys() dedup must collapse them and preserve first-seen
        # order.
        blob = (
            "  TCP    0.0.0.0:7777           0.0.0.0:0              LISTENING       99\n"
            "  TCP    [::]:7777              [::]:0                 LISTENING       99\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [99]

    def test_windows_accepts_tcp6_label_defensively(self, monkeypatch):
        # Today Windows netstat prints plain "TCP" for both families, but we
        # relaxed the proto check from `== "TCP"` to `startswith("TCP")` to
        # future-proof against a hypothetical Windows build that switches to
        # "TCP6" (the netstat -p flag already accepts "tcpv6"). Guard the
        # defensive path so a future relabel doesn't silently re-break this.
        blob = "  TCP6   [::1]:7777             [::]:0                 LISTENING       77\n"
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [77]

    def test_windows_ignores_non_listening_rows(self, monkeypatch):
        # ESTABLISHED / TIME_WAIT etc. must never match: their foreign
        # endpoint is a real peer (not the 0.0.0.0:0 / [::]:0 wildcard) and
        # their state is not LISTENING, so both signals reject them.
        blob = (
            "  TCP    127.0.0.1:7777         127.0.0.1:9999         ESTABLISHED     55\n"
            "  TCP    127.0.0.1:7777         0.0.0.0:0              LISTENING       88\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [88]

    def test_windows_finds_listener_on_localized_netstat(self, monkeypatch):
        # netstat localizes state names (German "ABHÖREN", French, Cyrillic…),
        # so matching the English "LISTENING" literal alone returns [] on any
        # non-English Windows and stop/restart silently no-op with the gateway
        # still holding the port. Listener detection therefore keys off the
        # wildcard FOREIGN endpoint (0.0.0.0:0 / [::]:0), which is
        # locale-independent; the English literal remains as a second signal.
        blob = (
            "  Proto  Lokale Adresse         Remoteadresse          Status          PID\n"
            "  TCP    127.0.0.1:7777         0.0.0.0:0              ABHÖREN         44\n"
            "  TCP    [::1]:7777             [::]:0                 ABHÖREN         44\n"
            "  TCP    127.0.0.1:7777         127.0.0.1:9999         HERGESTELLT     66\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [44]

    def test_windows_listeners_carry_their_local_address(self, monkeypatch):
        # The netstat parse attributes each row's local address to its PID so
        # callers can scope ownership to the address they probed; a dual-stack
        # listener keeps one entry per bound address, v6 brackets stripped.
        blob = (
            "  TCP    0.0.0.0:7777           0.0.0.0:0              LISTENING       99\n"
            "  TCP    [::]:7777              [::]:0                 LISTENING       99\n"
            "  TCP    192.168.1.5:7777       0.0.0.0:0              LISTENING       55\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_port_listeners(7777) == [
            pc.PortListener(99, "0.0.0.0", "4"),
            pc.PortListener(99, "::", "6"),
            pc.PortListener(55, "192.168.1.5", "4"),
        ]

    @pytest.mark.ipv6_required
    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows netstat branch")
    def test_windows_finds_real_ipv6_loopback_listener(self):
        # End-to-end guard on a live host: bind AF_INET6 to ::1 at an ephemeral
        # port and confirm find_listening_pids returns THIS process's pid.
        # Loopback-only (::1) so no firewall prompt fires. Complements the
        # canned-blob tests above by exercising the real netstat parse against
        # whatever this Windows build actually prints.
        import socket as _socket

        s = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
        try:
            s.bind(("::1", 0))
            s.listen()
            port = s.getsockname()[1]
            pids = pc.find_listening_pids(port)
            assert os.getpid() in pids, f"expected pid {os.getpid()} in {pids}"
        finally:
            s.close()


class TestAddressCoversLoopback:
    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "0.0.0.0", "*", "::", "[::]", "::ffff:127.0.0.1", " 0.0.0.0 "],
    )
    def test_loopback_covering_addresses(self, address):
        assert pc.address_covers_loopback(address) is True

    @pytest.mark.parametrize(
        "address",
        # ::1 cannot receive a connect addressed to 127.0.0.1, so a
        # v6-loopback-only listener is deliberately NOT loopback-covering.
        ["::1", "[::1]", "192.168.1.5", "10.0.0.1", "fe80::1", "127.0.0.2", ""],
    )
    def test_other_addresses_do_not_cover_loopback(self, address):
        assert pc.address_covers_loopback(address) is False


class TestLoopbackOwnerPids:
    """The most-specific-bind dispatch tiers of :func:`loopback_owner_pids`."""

    def test_an_exact_loopback_bind_beats_wildcards(self):
        # The kernel routes a 127.0.0.1 connect to the exact bind, so wildcard
        # listeners on the same port never saw the probe and are not owners.
        listeners = [
            pc.PortListener(111, "127.0.0.1", "4"),
            pc.PortListener(999, "*", "4"),
            pc.PortListener(888, "::", "6"),
        ]
        assert pc.loopback_owner_pids(listeners) == [111]

    def test_a_v4_wildcard_beats_a_possibly_v6only_wildcard(self):
        # An unrelated IPV6_V6ONLY wildcard next to the real v4 owner must not
        # be claimed: a v4 connect reaches the v4 wildcard socket, never the
        # v6-only one. lsof spells both ``*`` — the family is the separator.
        listeners = [
            pc.PortListener(111, "*", "4"),
            pc.PortListener(999, "*", "6"),
        ]
        assert pc.loopback_owner_pids(listeners) == [111]

    def test_a_lone_v6_wildcard_is_the_responder(self):
        # Callers only ask after a successful 127.0.0.1 probe; with nothing
        # more specific on the port, the v6 wildcard must be dual-stack and is
        # the adopted owner (refusing it would break [::]-bound externally
        # managed backends).
        listeners = [pc.PortListener(77, "::", "6")]
        assert pc.loopback_owner_pids(listeners) == [77]

    def test_multi_worker_backends_share_ownership(self):
        # Pre-fork / multi-worker backends legitimately share one listening
        # socket: every PID in the winning tier is recorded.
        listeners = [
            pc.PortListener(11, "127.0.0.1", "4"),
            pc.PortListener(12, "127.0.0.1", "4"),
            pc.PortListener(999, "*", "4"),
        ]
        assert pc.loopback_owner_pids(listeners) == [11, 12]

    def test_unknown_family_wildcards_fall_to_the_covering_tier(self):
        # A source that reported no family (old lsof output) still resolves:
        # the covering tier keeps adoption working rather than refusing it.
        listeners = [
            pc.PortListener(11, "*"),
            pc.PortListener(22, "192.168.1.5"),
        ]
        assert pc.loopback_owner_pids(listeners) == [11]

    def test_no_covering_listener_yields_no_owner(self):
        listeners = [pc.PortListener(999, "192.168.1.5", "4")]
        assert pc.loopback_owner_pids(listeners) == []

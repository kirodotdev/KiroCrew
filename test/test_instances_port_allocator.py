"""Instances ``PortAllocator``: loopback local-port allocation for tunnels."""

from __future__ import annotations

import errno
import socket

import pytest


class TestPortAllocator:
    def test_rejects_bad_base(self):
        from kiro_crew.instances.port_allocator import PortAllocator

        with pytest.raises(ValueError):
            PortAllocator(base_port=0)

    def test_skips_bound_and_excluded(self, monkeypatch):
        """The allocator skips a bound port and a caller-supplied exclusion window.

        Uses a fixed base (41000) with a monkeypatched probe so this test is
        not sensitive to OS ephemeral port assignments near 65535.  Real socket
        binding is verified separately in test_is_port_free_detects_live_listener*.
        """
        import kiro_crew.instances.port_allocator as pa_mod
        from kiro_crew.instances.port_allocator import PortAllocator

        base = 41000
        # Simulate base as occupied (as if a socket is actively listening there).
        monkeypatch.setattr(pa_mod, "_is_port_free", lambda p: p != base)
        pa = PortAllocator(base_port=base)
        assert pa.allocate() > base  # base is occupied -> skipped
        assert pa.allocate(exclude={base, base + 1, base + 2}) >= base + 3

    def test_preferred_port_is_returned_when_free(self):
        """A free, in-range ``preferred`` is handed back unchanged.

        This is the origin-stability contract for a crew's embedded pane: the
        loopback port is the browser origin of that pane, so returning the same
        recorded port across reconnects keeps its origin-keyed localStorage
        settings alive. ``preferred`` well above ``base`` must win over the
        first-free port.
        """
        from kiro_crew.instances.port_allocator import PortAllocator

        # Two OS-assigned free ports; the higher one is the "recorded" one.
        socks = []
        for _ in range(2):
            # flake-ok: bind-close OS ports feed the preferred-port probe, which must test one fixed port; moved verbatim
            sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sk.bind(("127.0.0.1", 0))
            socks.append(sk)
        low, high = sorted(s.getsockname()[1] for s in socks)
        for s in socks:
            s.close()
        base = min(low, high)
        pa = PortAllocator(base_port=base)
        # Even though a lower free port exists, the preferred one is returned.
        assert pa.allocate(preferred=high) == high

    def test_preferred_port_falls_back_when_excluded_or_out_of_range(self, monkeypatch):
        """A ``preferred`` that is excluded, below base, or occupied yields.

        The preference is honoured, never enforced: a port another instance now
        holds (``exclude``) or one outside the allocator's range must not be
        handed back — allocation falls through to first-free instead.

        Uses a fixed base (41000) with a monkeypatched probe so this test is
        not sensitive to OS ephemeral port assignments near 65535.
        """
        import kiro_crew.instances.port_allocator as pa_mod
        from kiro_crew.instances.port_allocator import PortAllocator

        base = 41000
        # All ports appear free; the test exercises only the exclude-set and
        # out-of-range logic, not real socket occupancy.
        monkeypatch.setattr(pa_mod, "_is_port_free", lambda p: True)
        pa = PortAllocator(base_port=base)
        # Preferred is reserved by another instance -> not returned.
        got = pa.allocate(exclude={base}, preferred=base)
        assert got != base and got > base
        # Preferred below base is ignored, never handed back out of range.
        assert pa.allocate(preferred=base - 1) >= base

    def test_is_port_free_detects_live_listener_even_with_reuseaddr(self):
        """A genuinely LISTENing port is still reported in-use.

        Regression guard for the disconnect->reconnect fix: on POSIX
        `_is_port_free` sets SO_REUSEADDR (so a just-freed port lingering in
        TIME_WAIT is not a false positive, matching ssh's own `-L` listener
        bind). This must NOT relax detection of a real, live listener — a true
        two-instance port collision still has to be caught. SO_REUSEADDR
        exempts TIME_WAIT only, never an active LISTEN, so the probe must still
        fail to bind against a LISTENing socket that itself set SO_REUSEADDR.

        On Windows the same occupier is the port-squat shape: two SO_REUSEADDR
        sockets can both listen there, so the probe uses SO_EXCLUSIVEADDRUSE
        and this test runs unchanged on the Windows matrix as the live proof.
        """
        from kiro_crew.instances.port_allocator import _is_port_free

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # The occupier sets SO_REUSEADDR too (as ssh does); the probe must still
        # be denied while this socket is actively listening.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            assert _is_port_free(port) is False
        finally:
            s.close()

    class _ProbeSocket:
        """Socket double that records the options the probe sets."""

        def __init__(self, calls, bind_error=None):
            self._calls = calls
            self._bind_error = bind_error

        def setsockopt(self, level, name, value):
            self._calls.append(("setsockopt", level, name, value))

        def bind(self, addr):
            self._calls.append(("bind", addr))
            if self._bind_error is not None:
                raise self._bind_error

        def close(self):
            self._calls.append(("close",))

    def _fake_socket_module(self, calls, *, exclusive=True, bind_error=None):
        import types

        ns = types.SimpleNamespace(
            AF_INET=socket.AF_INET,
            AF_INET6=socket.AF_INET6,
            SOCK_STREAM=socket.SOCK_STREAM,
            SOL_SOCKET=socket.SOL_SOCKET,
            SO_REUSEADDR=socket.SO_REUSEADDR,
            socket=lambda family, kind: self._ProbeSocket(calls, bind_error),
        )
        if exclusive:
            # Winsock's value (~SO_REUSEADDR); any int distinct from SO_REUSEADDR works.
            ns.SO_EXCLUSIVEADDRUSE = -5
        return ns

    def test_windows_probe_sets_exclusiveaddruse_and_never_reuseaddr(self, monkeypatch):
        """Windows probe: SO_EXCLUSIVEADDRUSE only.

        With SO_REUSEADDR, a Windows probe binds next to a live listener that set
        it too (OpenSSH's -L listener does), reads the port as free, and the
        forward becomes a second listener that never gets the traffic.
        """
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pa, "socket", self._fake_socket_module(calls))

        assert pa._is_addr_free(7778, "127.0.0.1") is True

        opts = [c for c in calls if c[0] == "setsockopt"]
        assert opts == [("setsockopt", socket.SOL_SOCKET, -5, 1)]
        assert calls[-2:] == [("bind", ("127.0.0.1", 7778)), ("close",)]

    def test_windows_probe_reads_exclusive_bind_refusal_as_in_use(self, monkeypatch):
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pa,
            "socket",
            self._fake_socket_module(calls, bind_error=OSError(errno.EADDRINUSE, "in use")),
        )

        assert pa._is_addr_free(7778, "127.0.0.1") is False
        assert pa._is_port_free(7778) is False

    def test_windows_probe_without_the_constant_sets_no_reuse_option(self, monkeypatch):
        """No SO_EXCLUSIVEADDRUSE in this build: plain bind, never SO_REUSEADDR."""
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pa, "socket", self._fake_socket_module(calls, exclusive=False))

        assert pa._is_addr_free(7778, "::1") is True
        assert [c for c in calls if c[0] == "setsockopt"] == []

    def test_posix_probe_still_sets_reuseaddr_only(self, monkeypatch):
        """POSIX probe unchanged: SO_REUSEADDR, so TIME_WAIT is not a false positive."""
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pa, "socket", self._fake_socket_module(calls))

        assert pa._is_addr_free(7778, "127.0.0.1") is True
        opts = [c for c in calls if c[0] == "setsockopt"]
        assert opts == [("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]

    def test_is_port_free_true_for_unbound_port(self):
        from kiro_crew.instances.port_allocator import _is_port_free

        # Grab an OS-assigned port, then release it — it is now free to bind.
        # flake-ok: bind-close OS port is the fixed port the free-port probe under test must check; moved verbatim
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert _is_port_free(port) is True

    @pytest.mark.skipif(not socket.has_ipv6, reason="host has no IPv6 support")
    def test_is_port_free_rejects_port_held_on_ipv6_loopback_only(self):
        """A port free on 127.0.0.1 but LISTENing on ::1 counts as in use.

        The forward binds one address, so leaving the other loopback family to a
        foreign listener makes `localhost:<port>` resolve to whichever socket the
        client's resolver and the platform's bind precedence pick. The probe must
        therefore clear every loopback address, not just IPv4.
        """
        from kiro_crew.instances.port_allocator import _is_addr_free, _is_port_free

        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind(("::1", 0))
        except OSError:  # ::1 not configured on this host
            s.close()
            pytest.skip("::1 is not assignable here")
        s.listen(1)
        port = s.getsockname()[1]
        try:
            # Quick check: the IPv4 half really is free, so only the ::1 half can be
            # what makes the aggregate probe say "in use".
            # flake-ok: real 127.0.0.1 bind probe on the ::1 listener port number; moved verbatim, tracked for a de-flake pass
            assert _is_addr_free(port, "127.0.0.1") is True
            assert _is_port_free(port) is False
        finally:
            s.close()

    def test_is_port_free_treats_unassignable_address_as_free(self, monkeypatch):
        """EADDRNOTAVAIL means the address does not exist, not that it is taken.

        Without this, a host with IPv6 compiled in but ::1 not configured would
        see every candidate port as occupied and connect would never allocate one.
        """
        import kiro_crew.instances.port_allocator as pa

        real_socket = socket.socket

        def fake_socket(family, type_):
            sock = real_socket(family, type_)
            if family == socket.AF_INET6:
                sock.close()

                class _Unassignable:
                    def setsockopt(self, *a):
                        pass

                    def bind(self, *a):
                        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign address")

                    def close(self):
                        pass

                return _Unassignable()
            return sock

        # flake-ok: pre-existing stdlib socket.socket patch inside one sync test, restored at teardown; moved verbatim
        monkeypatch.setattr(pa.socket, "socket", fake_socket)

        # flake-ok: bind-close OS port is the fixed IPv4 port the probe checks beside a faked IPv6 socket; moved verbatim
        s = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert pa._is_port_free(port) is True

    @pytest.mark.parametrize(
        "creation_errno",
        [errno.EMFILE, errno.ENFILE, errno.ENOBUFS],
        ids=["EMFILE", "ENFILE", "ENOBUFS"],
    )
    def test_is_port_free_propagates_when_the_probe_cannot_run(self, monkeypatch, creation_errno):
        """A probe that could not RUN answers neither "free" nor "in use".

        Reading it as free would hand out a port a listener on the unprobed
        family may hold; reading it as in use would send the allocator through
        every candidate and fail with a port-exhaustion message naming the wrong
        cause. So it propagates, which is also what the pre-dual-stack code did
        (a creation error was never caught).
        """
        import kiro_crew.instances.port_allocator as pa

        real_socket = socket.socket

        def fake_socket(family, type_):
            if family == socket.AF_INET6:
                raise OSError(creation_errno, "probe could not be run")
            return real_socket(family, type_)

        # flake-ok: pre-existing stdlib socket.socket patch inside one sync test, restored at teardown; moved verbatim
        monkeypatch.setattr(pa.socket, "socket", fake_socket)

        # flake-ok: bind-close OS port must still be free on IPv4 for the faked IPv6 creation error to surface; moved verbatim
        s = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with pytest.raises(OSError) as excinfo:
            pa._is_port_free(port)
        assert excinfo.value.errno == creation_errno

    @pytest.mark.parametrize(
        "creation_errno",
        [errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT],
        ids=["EAFNOSUPPORT", "EPROTONOSUPPORT"],
    )
    def test_is_port_free_treats_absent_family_as_free(self, monkeypatch, creation_errno):
        """A family the kernel will not create cannot be holding the port.

        Both errnos are reported by IPv6-less kernels depending on the stack.
        Failing closed here would refuse every candidate port on such a host and
        `connect` would never allocate one.
        """
        import kiro_crew.instances.port_allocator as pa

        real_socket = socket.socket

        def fake_socket(family, type_):
            if family == socket.AF_INET6:
                raise OSError(creation_errno, "no such protocol family")
            return real_socket(family, type_)

        # flake-ok: pre-existing stdlib socket.socket patch inside one sync test, restored at teardown; moved verbatim
        monkeypatch.setattr(pa.socket, "socket", fake_socket)

        # flake-ok: bind-close OS port is the fixed IPv4 port the probe checks beside a faked absent IPv6 family; moved verbatim
        s = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert pa._is_port_free(port) is True

    def test_allocate_skips_port_held_on_one_loopback_family(self, monkeypatch):
        """The allocator climbs past a port the aggregate probe rejects."""
        import kiro_crew.instances.port_allocator as pa

        base = 41000
        monkeypatch.setattr(
            pa, "_is_addr_free", lambda port, host: not (port == base and host == "::1")
        )
        assert pa.PortAllocator(base_port=base).allocate() == base + 1

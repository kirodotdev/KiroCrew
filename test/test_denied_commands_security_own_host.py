"""Tests for the own-host set the ssh-to-self floor compares a destination against.

The interface-address seed and the platform readers behind it (Windows adapters, Darwin
``getifaddrs``, Linux netlink), the background name-resolution worker and its gateway
warm-up, the DNS-alias verdict layer, and dotless names a hosts file maps.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _REAL_RESOLVED_HOST_VERDICT, _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.testing.wait import async_wait_until, wait_until

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestSandboxEscapeSshSelfOwnHost:
    """Which hosts the ssh-to-self floor treats as this machine.

    The other half of ``TestSandboxEscapeSshSelf``: that class pins which command lines
    the floor reads as reaching a host, this one pins the own-host set a destination is
    compared against -- the interface-address seed on each platform, the netlink
    publish, the name-resolution worker and its gateway warm-up, the DNS-alias verdict
    layer, and dotless names a hosts file maps.
    """

    _RULE = "sandbox-escape-ssh-self"

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _pin_own_host_cache(self, monkeypatch):
        # The same pin as ``TestSandboxEscapeSshSelf``'s. None of that class's
        # parametrized deny cases target the machine's own hostname dynamically --
        # they use localhost / 127.0.0.1 / ::1 / the literal ``$(hostname)`` text
        # hint -- so a fixed cache with the resolved-once latch set covers them,
        # and it keeps ``_own_host_names``
        # from seeding the module globals and spawning the real
        # ``kirocrew-own-host-resolve`` getfqdn/getaddrinfo daemon thread (a
        # no-test-side-effects violation, plus leaked global state for the
        # worker). ``_OWN_HOST_RESOLVE_DONE`` short-circuits ``_own_host_names``
        # before the lock, so there is no seed and no thread; monkeypatch
        # restores the globals afterward. The resolver unit tests below re-pin
        # these same globals and call the cache fn directly, so this autouse pin
        # does not interfere with them.
        own = security.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        # The cache slots live on the module that OWNS them: they are
        # deliberately not re-exported by the facade (a slot rebound through
        # ``global`` would leave the facade holding a stale value), so the
        # facade's patch mirroring does not cover them and the owner is
        # patched directly.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        # tests run at steady state — the kernel address table has
        # published, so IP-literal allow rows resolve on their own merits.
        # The pending-window tests set this back to False themselves.
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        # The DNS-alias verdict layer fails closed on hostnames it has not
        # resolved, and tests must not resolve real names -- stub it to
        # "not self" so remote-host allow cases stay allowed (that class's
        # parametrized ones, and the far hosts here).  The alias-layer tests
        # below re-bind the real function (captured at import as
        # _REAL_RESOLVED_HOST_VERDICT) and stub the resolver socket instead.
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    def test_floor_spawns_no_dns_resolver_thread(self):
        # With the own-host cache pinned by the autouse fixture, evaluating a
        # representative allow case through the floor must NOT spawn the real
        # ``kirocrew-own-host-resolve`` daemon (DONE=True short-circuits
        # ``_own_host_names`` before the thread).
        #
        # Scoped to threads that appear DURING the call, because the claim is
        # about THIS evaluation and `threading.enumerate()` is process-wide. The
        # resolver unit tests below call the cache function directly, and on macOS
        # a `getfqdn`/`getaddrinfo` for a `*.local` name takes SECONDS (the same
        # mDNS latency this branch's boot matrix surfaced), so one of their daemons
        # can still be alive here under any test order -- a neighbour's leftover is
        # not this floor spawning one.
        before = {id(t) for t in threading.enumerate() if t.name == "kirocrew-own-host-resolve"}
        assert _denied_by("ssh far-host.example.com uptime") is None
        spawned = [
            t
            for t in threading.enumerate()
            if t.name == "kirocrew-own-host-resolve" and id(t) not in before
        ]
        assert not spawned, "the DNS-enrichment daemon thread was spawned during the floor scan"

    def test_resolver_strips_ipv6_zone_id(self, monkeypatch):
        # Cache side of the zone strip: ``getaddrinfo`` can return a scoped
        # spelling (``fe80::1%eth0``) for a link-local address, so the resolver
        # strips the zone before caching -- otherwise the cached address would
        # never match the bare ``fe80::1`` a command names.
        monkeypatch.setattr(security.socket, "gethostname", lambda: "myhost")
        monkeypatch.setattr(security.socket, "getfqdn", lambda: "myhost.example.com")
        monkeypatch.setattr(
            security.socket,
            "getaddrinfo",
            lambda *a, **k: [(None, None, None, None, ("fe80::1%eth0", 0))],
        )
        monkeypatch.setattr(security, "_own_interface_addresses", set, raising=False)
        resolved, _complete = security._resolve_own_host_names()
        assert "fe80::1" in resolved
        assert "fe80::1%eth0" not in resolved

    def test_first_command_knows_interface_addresses(self, monkeypatch):
        # Interface addresses belong to the SYNCHRONOUS seed: the very first
        # ssh-family command must already see them.  The DNS enrichment worker
        # is suppressed here (NEXT_TRY=inf), so a pass proves the seed alone
        # covers the interface IP -- no worker race can re-open the window.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_own_interface_addresses", lambda: {"203.0.113.7"})
        assert _denied_by("ssh 203.0.113.7 id") == self._RULE
        assert _denied_by("ssh 198.51.100.9 id") is None

    def test_own_host_seed_does_no_dns(self, monkeypatch):
        # The synchronous seed runs on the event-loop ``is_denied`` path, so it
        # must never resolve names: a slow resolver there stalls every session
        # and the heartbeat.  DNS-derived names belong to the async enrichment
        # worker alone; the seed is gethostname forms plus packet-less
        # interface enumeration.
        calls: "list[tuple]" = []

        def _record(*args, **kwargs):
            calls.append(args)
            return ("stub-host", [], ["203.0.113.9"])

        monkeypatch.setattr(security.socket, "gethostbyname_ex", _record)
        seed = _argv_floor._own_host_seed()
        assert isinstance(seed, frozenset)
        assert calls == [], "the synchronous seed must not call gethostbyname_ex"

    def test_first_command_knows_windows_interface_addresses(self, monkeypatch):
        # The Windows per-adapter sweep feeds the SYNCHRONOUS seed exactly like
        # the Linux sweep: the very first ssh-family command must already see a
        # secondary/VPN address that the route-selected UDP probes miss.  The
        # sweep itself is a win32-only iphlpapi call, so it is stubbed here --
        # this test pins the seed WIRING, and the off-platform guard below pins
        # that the helper contributes nothing elsewhere.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_windows_interface_addresses", lambda: {"203.0.113.44"})
        assert _denied_by("ssh 203.0.113.44 id") == self._RULE
        assert _denied_by("ssh 198.51.100.9 id") is None

    def test_windows_sweep_is_inert_off_windows(self):
        if sys.platform == "win32":  # pragma: no cover - exercised on win CI
            pytest.skip("the sweep enumerates real adapters on Windows")
        assert _argv_floor._windows_interface_addresses() == set()

    def test_netlink_dump_parse_covers_secondary_addresses(self):
        # SIOCGIFADDR returns only the PRIMARY IPv4 per interface,
        # so a secondary address (cloud multi-IP, VIP) never reached the
        # own-name seed.  The netlink RTM_GETADDR dump lists every assigned
        # address; this pins the parser on a synthetic two-message dump.
        import ipaddress
        import socket
        import struct

        def _nl(msg_type, payload):
            return struct.pack("=LHHLL", 16 + len(payload), msg_type, 0, 1, 0) + payload

        ifa4 = struct.pack("=BBBBL", socket.AF_INET, 32, 0, 0, 2)
        attr4 = struct.pack("=HH", 8, 2) + socket.inet_aton("10.0.0.7")  # IFA_LOCAL
        ifa6 = struct.pack("=BBBBL", socket.AF_INET6, 64, 0, 0, 2)
        attr6 = struct.pack("=HH", 20, 1) + ipaddress.IPv6Address("2001:db8::7").packed
        done = struct.pack("=LHHLL", 16, 3, 0, 1, 0)  # NLMSG_DONE
        got = _argv_floor._parse_netlink_addr_dump(_nl(20, ifa4 + attr4) + _nl(20, ifa6 + attr6))
        assert "10.0.0.7" in got
        assert "2001:db8::7" in got
        assert _argv_floor._parse_netlink_addr_dump(done) == set()

    def test_secondary_addresses_deny_after_netlink_publish(self, monkeypatch):
        # the netlink dump runs OFF the event loop in the DNS
        # enrichment worker (a blocking recv is barred from the synchronous
        # seed).  Once published, a secondary IPv4 the ioctl sweep cannot
        # see denies from the cache like any own address, and a far IP is
        # admitted again.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"203.0.113.66"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        assert _denied_by("ssh 203.0.113.66 id") == self._RULE
        assert _denied_by("ssh 198.51.100.9 id") is None

    def test_ip_literals_fail_closed_until_netlink_publishes(self, monkeypatch):
        # while the kernel address table is UNREAD, a non-own IP
        # literal in host position could be an unlisted secondary of this
        # very machine -- so the window denies every one (the same
        # fail-closed contract dotted first-contact hostnames carry).
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        assert _denied_by("ssh 198.51.100.9 id") == self._RULE
        assert _denied_by("ssh 203.0.113.66 uptime") == self._RULE
        assert _denied_by("ssh 2001:db8::7 id") == self._RULE
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        assert _denied_by("ssh 198.51.100.9 id") is None

    def test_enrichment_worker_merges_netlink_addresses(self, monkeypatch):
        # the worker pass carries the netlink layer -- its output
        # lands in the resolved set and a non-empty pass publishes the flag
        # that closes the IP-literal window.  DNS is stubbed inert so the
        # test stays packet-less.
        monkeypatch.setattr(_argv_floor, "_linux_netlink_addresses", lambda: {"203.0.113.66"})
        monkeypatch.setattr(_argv_floor.socket, "getfqdn", lambda: "")
        monkeypatch.setattr(_argv_floor.socket, "getaddrinfo", lambda *a, **k: [])
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        resolved, _complete = _argv_floor._resolve_own_host_names()
        assert "203.0.113.66" in resolved
        assert _argv_floor._NETLINK_ADDRS_PUBLISHED is True

    _RESOLVER_THREAD = "kirocrew-own-host-resolve"

    @classmethod
    def _resolvers_now(cls) -> set[int]:
        """The resolver daemons alive right now, by identity."""
        return {id(t) for t in threading.enumerate() if t.name == cls._RESOLVER_THREAD}

    def _open_window(self, monkeypatch, *, netlink, fqdn=lambda: ""):
        """A fresh process: nothing published, the worker free to start now.

        Records the resolver daemons already alive, so :meth:`_join_resolver`
        joins only the one this test starts. ``threading.enumerate()`` is
        process-wide, and the unit tests below call the cache function directly,
        where a macOS ``getfqdn`` for a ``*.local`` name takes seconds, so a
        neighbour's daemon can still be running here under any test order.
        """
        self._foreign_resolvers = self._resolvers_now()
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        monkeypatch.setattr(_argv_floor, "_linux_netlink_addresses", netlink)
        monkeypatch.setattr(_argv_floor.socket, "getfqdn", fqdn)
        monkeypatch.setattr(_argv_floor.socket, "getaddrinfo", lambda *a, **k: [])

    #: Lost-run bounds for the resolver tests below. Once released, the netlink
    #: publish and the worker's exit take milliseconds; these only end a wedged
    #: worker, inside half the suite's 120 s timeout.
    _PUBLISH_DEADLINE_SECS = 30.0
    _RESOLVER_JOIN_SECS = 30.0
    #: How long a stubbed DNS lookup holds the worker. It must outlast the publish
    #: wait, or a slow publish lets DNS return before the test can see it still
    #: in flight. The test's ``finally`` releases it, so no passing run spends it.
    _DNS_HOLD_SECS = 2 * _PUBLISH_DEADLINE_SECS

    def _join_resolver(self):
        """Join the resolver this test started, and fail if it outlives its bound."""
        foreign = getattr(self, "_foreign_resolvers", frozenset())
        bound = self._RESOLVER_JOIN_SECS
        for t in threading.enumerate():
            if t.name == self._RESOLVER_THREAD and id(t) not in foreign:
                t.join(bound)
                assert not t.is_alive(), f"the own-host resolver still ran after {bound:.0f}s"

    def test_startup_warm_publishes_before_the_first_ip_literal(self, monkeypatch):
        # Without a boot-time read, the first IP-literal ssh of a gateway
        # process is what starts the worker and it reads the unpublished flag
        # in the same instant, so it is refused as "this machine".  The warm
        # starts the worker at boot, so the table is published before then.
        self._open_window(monkeypatch, netlink=lambda: {"203.0.113.66"})
        try:
            _argv_floor.warm_own_host_names()
            self._join_resolver()
            assert _argv_floor._NETLINK_ADDRS_PUBLISHED is True
            assert _denied_by("ssh 198.51.100.9 id") is None
            assert _denied_by("ssh 203.0.113.66 id") == self._RULE
        finally:
            self._join_resolver()

    def test_slow_dns_does_not_hold_the_netlink_publish(self, monkeypatch):
        # The netlink dump runs BEFORE DNS: a host whose name is not in DNS
        # must not keep every IP literal refused for the length of the lookups,
        # and the warm returns without waiting for either.
        # The lookup's hold outlasts the publish wait, so the in-flight check
        # below runs while DNS is still held however long the publish took.
        release = threading.Event()

        def _slow_fqdn():
            release.wait(self._DNS_HOLD_SECS)
            return ""

        self._open_window(monkeypatch, netlink=lambda: {"203.0.113.66"}, fqdn=_slow_fqdn)
        try:
            _argv_floor.warm_own_host_names()
            wait_until(
                lambda: _argv_floor._NETLINK_ADDRS_PUBLISHED,
                timeout=self._PUBLISH_DEADLINE_SECS,
                describe=lambda: "netlink never published",
            )
            assert _argv_floor._OWN_HOST_RESOLVE_IN_FLIGHT is True  # DNS still blocked
            assert "203.0.113.66" in _argv_floor._OWN_HOST_NAMES_CACHE
            assert _denied_by("ssh 198.51.100.9 id") is None
        finally:
            release.set()
            self._join_resolver()

    @staticmethod
    def _hook(monkeypatch, warm):
        from kiro_crew.dashboard import server as _server

        monkeypatch.setattr(_server, "warm_own_host_names", warm)

        class _App:
            def __init__(self):
                self.on_startup: "list" = []

        app = _App()
        _server._register_own_host_warm(app)
        assert len(app.on_startup) == 1
        return _server, app

    def test_gateway_startup_starts_the_own_host_read_without_awaiting_it(self, monkeypatch):
        # The startup hook schedules the read in a worker thread and returns at
        # once: nothing is awaited in front of the listener
        # (no-new-work-on-gateway-boot-path).  The read still runs, off the loop.
        release = threading.Event()
        done = threading.Event()

        def _warm():
            release.wait(5)
            done.set()

        _server, app = self._hook(monkeypatch, _warm)

        async def _run():
            start = time.monotonic()
            await app.on_startup[0](app)
            elapsed = time.monotonic() - start
            started_before_release = not done.is_set()
            release.set()
            # Let the scheduled worker finish while the loop is still alive.
            await async_wait_until(
                lambda: done.is_set() and not _server._OWN_HOST_WARM_TASKS,
                describe=lambda: f"done={done.is_set()} tasks={_server._OWN_HOST_WARM_TASKS}",
            )
            return elapsed, started_before_release

        elapsed, started_before_release = asyncio.run(_run())
        # flake-ok: pre-existing 0.5 s ceiling against a read held for up to 5 s, so it only fails when the hook awaits the read; moved verbatim, tracked for a de-flake pass
        assert elapsed < 0.5, "the startup hook waited on the own-address read"
        assert started_before_release
        assert done.is_set(), "the scheduled own-address read never ran"
        assert not _server._OWN_HOST_WARM_TASKS, "the finished task was not released"

    def test_gateway_startup_logs_a_failed_own_host_read(self, monkeypatch, caplog):
        def _boom():
            raise RuntimeError("netlink unavailable")

        _server, app = self._hook(monkeypatch, _boom)

        async def _run():
            await app.on_startup[0](app)
            await async_wait_until(
                lambda: not _server._OWN_HOST_WARM_TASKS,
                describe=lambda: f"tasks={_server._OWN_HOST_WARM_TASKS}",
            )

        with caplog.at_level("WARNING", logger=_server.logger.name):
            asyncio.run(_run())
        assert "own-address read failed at startup" in caplog.text

    def test_repeated_netlink_misses_log_one_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(_argv_floor, "_NETLINK_MISSES", 0)
        with caplog.at_level("WARNING", logger=_argv_floor.logger.name):
            for _ in range(5):
                _argv_floor._note_netlink_result(False)
        assert caplog.text.count("netlink read has not completed in 3 attempts") == 1
        _argv_floor._note_netlink_result(True)
        assert _argv_floor._NETLINK_MISSES == 0

    def test_publish_merges_into_the_cache_before_opening_the_window(self, monkeypatch):
        seen: "list[tuple[bool, bool]]" = []

        class _Probe(frozenset):
            # The merge calls ``cache | addrs``: record the flag and lock
            # state at that instant, so a flag flipped before the merge fails.
            def __or__(self, other):
                seen.append(
                    (
                        _argv_floor._NETLINK_ADDRS_PUBLISHED,
                        _argv_floor._OWN_HOST_RESOLVE_LOCK.locked(),
                    )
                )
                return frozenset(self) | other

        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", _Probe({"10.1.1.1"}))
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        _argv_floor._publish_netlink_addresses({"203.0.113.66"})
        assert seen == [(False, True)]
        assert _argv_floor._NETLINK_ADDRS_PUBLISHED is True
        assert {"10.1.1.1", "203.0.113.66"} <= _argv_floor._OWN_HOST_NAMES_CACHE

    @pytest.mark.parametrize("target", ["203.0.113.66", "2001:db8::66"])
    def test_a_publish_between_the_name_read_and_the_flag_read_still_refuses(
        self, monkeypatch, target
    ):
        # The check reads the names, the publisher lands, then the check
        # reads the flag: it must judge by the flag it saw BEFORE the names.
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)

        def stale_names():
            monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
            return frozenset()

        monkeypatch.setattr(_argv_floor, "_own_host_names", stale_names)
        assert _argv_floor._host_is_self(target) is True

    def test_dns_worker_merges_the_cache_under_the_lock(self, monkeypatch):
        # The netlink publisher and the DNS worker both read-modify-write the
        # own-name cache; the worker's merge must hold the lock, or a publish
        # landing between its read and its write is lost after the window
        # has opened.
        held: "list[bool]" = []
        real_lock = _argv_floor._OWN_HOST_RESOLVE_LOCK
        state = {"inside": False}

        class _Spy:
            def __enter__(self):
                real_lock.__enter__()
                state["inside"] = True

            def __exit__(self, *exc):
                state["inside"] = False
                return real_lock.__exit__(*exc)

        class _Cache(frozenset):
            def __or__(self, other):
                held.append(state["inside"])
                return frozenset(self) | frozenset(other)

        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_LOCK", _Spy())
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", _Cache({"10.1.1.1"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", True)
        monkeypatch.setattr(
            _argv_floor, "_resolve_own_host_names", lambda: (frozenset({"10.2.2.2"}), False)
        )
        _argv_floor._resolve_own_host_names_into_cache()
        assert held == [True]
        assert {"10.1.1.1", "10.2.2.2"} <= _argv_floor._OWN_HOST_NAMES_CACHE

    def test_netlink_sweep_is_inert_off_linux(self):
        if sys.platform.startswith("linux"):  # pragma: no cover - real enumeration
            pytest.skip("the sweep enumerates real addresses on Linux")
        assert _argv_floor._linux_netlink_addresses() == set()

    def test_first_command_knows_darwin_interface_addresses(self, monkeypatch):
        # The macOS per-interface sweep feeds the SYNCHRONOUS seed exactly
        # like the Linux and Windows sweeps: the very first ssh-family
        # command must already see a secondary/VPN address that the
        # route-selected UDP probes miss.  The sweep itself is a
        # darwin-only getifaddrs call, so it is stubbed here -- this test
        # pins the seed WIRING, and the off-platform guard below pins that
        # the helper contributes nothing elsewhere.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_darwin_interface_addresses", lambda: {"203.0.113.55"})
        assert _denied_by("ssh 203.0.113.55 id") == self._RULE
        assert _denied_by("ssh 198.51.100.9 id") is None

    def test_darwin_sweep_is_inert_off_darwin(self):
        if sys.platform == "darwin":  # pragma: no cover - exercised on mac CI
            pytest.skip("the sweep enumerates real interfaces on macOS")
        assert _argv_floor._darwin_interface_addresses() == set()

    def test_dns_alias_fails_closed_until_resolved(self, monkeypatch):
        # A hostname this floor cannot classify textually may still resolve
        # to a loopback or local address (a DNS alias).  The verdict comes
        # from an off-loop resolution; the decision fails closed until the
        # worker publishes, and scheduling is single-flight per host.
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        started: "list[str]" = []

        class _RecordingThread:
            def __init__(self, *args, **kwargs):
                # The threading patch is module-wide, so unrelated thread
                # constructions land here too -- count only this layer's
                # verdict workers.
                if kwargs.get("name") == "kirocrew-host-verdict":
                    started.append(kwargs.get("name", ""))

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(_argv_floor.threading, "Thread", _RecordingThread)
        assert _denied_by("ssh self.attacker.example id") == self._RULE
        assert _denied_by("ssh self.attacker.example id") == self._RULE
        assert len(started) == 1, "resolution scheduling must be single-flight"

    def test_negative_dns_verdict_is_revalidated_after_ttl(self, monkeypatch):
        # a cached ALLOW verdict is not reused unbounded -- a name
        # that later rebinds to loopback is caught at the next revalidation.
        # Deny verdicts stay permanent (over-blocking is the safe direction).
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        now = 1_000_000.0
        # flake-ok: pre-existing frozen clock on the floor module's time binding, restored by monkeypatch at teardown; moved verbatim, tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing clock pin on the floor module's time binding, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(_argv_floor.time, "monotonic", lambda: now)
        stale = now - _argv_floor._HOST_VERDICT_ALLOW_TTL - 1
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {"a.example": False})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_STAMP", {"a.example": stale})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        started: "list[str]" = []

        class _RecordingThread:
            def __init__(self, *args, **kwargs):
                if kwargs.get("name") == "kirocrew-host-verdict":
                    started.append(kwargs.get("args", ("",))[0])

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(_argv_floor.threading, "Thread", _RecordingThread)
        # Stale allow: served (stale-while-revalidate), one worker scheduled.
        assert _argv_floor._resolved_host_verdict("a.example") is False
        assert started == ["a.example"]
        # Fresh allow: served, no revalidation.
        _argv_floor._HOST_VERDICT_STAMP["a.example"] = now
        _argv_floor._HOST_VERDICT_PENDING.clear()
        started.clear()
        assert _argv_floor._resolved_host_verdict("a.example") is False
        assert started == []
        # Deny verdicts are permanent -- no revalidation however old.
        _argv_floor._HOST_VERDICT_CACHE["b.example"] = True
        _argv_floor._HOST_VERDICT_STAMP["b.example"] = stale
        assert _argv_floor._resolved_host_verdict("b.example") is True
        assert started == []

    def test_dns_alias_worker_classifies_addresses(self, monkeypatch):
        # The worker resolves off-loop and records whether any address is
        # loopback/local: an alias to 127.0.0.1 is self, a public address
        # is not, and a name that does not resolve is not (the connection
        # cannot reach this host either).
        def _fake_gai(addr):
            def _gai(host, *args, **kwargs):
                if addr is None:
                    raise OSError("resolution failure")
                return [(2, 1, 6, "", (addr, 0))]

            return _gai

        for addr, expected in (
            ("127.0.0.1", True),
            ("::1", True),
            ("198.51.100.7", False),
        ):
            cache: "dict[str, bool]" = {}
            monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", cache)
            monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", {"alias.example"})
            monkeypatch.setattr(security.socket, "getaddrinfo", _fake_gai(addr))
            _argv_floor._resolve_host_verdict_into_cache("alias.example")
            assert cache.get("alias.example") is expected, (addr, expected)
            assert "alias.example" not in _argv_floor._HOST_VERDICT_PENDING
        # a resolution FAILURE caches nothing -- a transient DNS
        # error latched as a 300s allow would let a recovered loopback alias
        # bypass the floor.  Pending clears so the next decision can retry.
        cache = {}
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", cache)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", {"alias.example"})
        monkeypatch.setattr(security.socket, "getaddrinfo", _fake_gai(None))
        _argv_floor._resolve_host_verdict_into_cache("alias.example")
        assert "alias.example" not in cache
        assert "alias.example" not in _argv_floor._HOST_VERDICT_PENDING

    def test_resolver_thread_start_failure_answers_from_seed(self, monkeypatch):
        # ``Thread.start`` can fail under resource exhaustion; the
        # permission decision must then come from the synchronous seed
        # rather than an exception aborting the gate.  The cleared latch
        # lets a later call retry the worker once threads free up.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)

        class _NoStartThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr(_argv_floor.threading, "Thread", _NoStartThread)
        names = _argv_floor._own_host_names()
        assert isinstance(names, frozenset)
        assert names, "the synchronous seed must answer the decision"
        assert _argv_floor._OWN_HOST_RESOLVE_IN_FLIGHT is False

    def test_own_hostname_is_denied_once_resolved(self, monkeypatch):
        # The enriched set reads the published cache; enrichment happens in a
        # worker thread (see test_own_name_resolution below), so the deny path
        # is tested against a directly-published set.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(
            _argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"myhost.example.com", "myhost"})
        )
        assert _denied_by("ssh myhost.example.com sudo id") == self._RULE
        assert _denied_by("ssh user@myhost id") == self._RULE
        assert _denied_by("ssh otherhost.example.com id") is None

    def test_first_own_host_command_is_denied_without_waiting_for_dns(self, monkeypatch):
        # The gethostname seed is published SYNCHRONOUSLY on first use, so the
        # very first `ssh <own-hostname>` is denied even while DNS enrichment
        # has not run (no resolution race).
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        # Backoff pushed to the future so no enrichment thread spawns in-test.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(security.socket, "gethostname", lambda: "MyHost.Example.Com")
        assert _denied_by("ssh myhost.example.com sudo id") == self._RULE
        assert _denied_by("ssh myhost id") == self._RULE
        assert _denied_by("ssh otherhost.example.com id") is None

    def test_unresolved_own_names_still_block_loopback(self, monkeypatch):
        # The loopback half never depends on the seed or on DNS enrichment.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))

        def _boom():
            raise OSError("no hostname")

        monkeypatch.setattr(security.socket, "gethostname", _boom)
        # The seed also enumerates interface addresses; stub that too so this
        # scenario is a machine where NOTHING about the own identity resolves.
        monkeypatch.setattr(_argv_floor, "_own_interface_addresses", set)
        assert _denied_by("ssh -p 22 localhost id") == self._RULE
        assert security._own_host_names() == frozenset()

    def test_own_name_resolution_is_best_effort(self, monkeypatch):
        # The resolver itself (thread body) tolerates hostname/DNS failures, and
        # reports the pass INCOMPLETE so the caller does not latch a partial set.
        def _boom():
            raise OSError("no hostname")

        monkeypatch.setattr(security.socket, "gethostname", _boom)
        monkeypatch.setattr(security.socket, "getfqdn", _boom)
        # Interface enumeration is a separate, DNS-independent source (Fix 3);
        # stub it empty here so this test isolates the DNS-failure path it
        # targets.  raising=False keeps it valid on a tree without the helper.
        monkeypatch.setattr(security, "_own_interface_addresses", set, raising=False)
        # The netlink layer lives on the owning module; the facade's patch
        # mirroring does not create absent attributes, so stub it directly.
        monkeypatch.setattr(_argv_floor, "_linux_netlink_addresses", set)
        resolved, complete = security._resolve_own_host_names()
        assert resolved == frozenset()
        assert complete is False
        monkeypatch.setattr(security.socket, "gethostname", lambda: "MyHost.Example.Com")
        monkeypatch.setattr(security.socket, "getfqdn", lambda: "myhost.example.com")
        monkeypatch.setattr(
            security.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError())
        )
        resolved, complete = security._resolve_own_host_names()
        assert {"myhost.example.com", "myhost"} <= resolved
        assert complete is False

    def test_stale_complete_own_set_rekicks_the_worker_while_serving(self, monkeypatch):
        # a COMPLETE resolve must go stale after the refresh window,
        # or a long-lived gateway that gains an interface address later (VPN
        # attach, DHCP renewal) admits ``ssh <new-address>`` forever.  The
        # stale set keeps being served (never blocks, never shrinks) while a
        # single-flight worker re-enumerates and merges.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"oldname"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        # A stamp of 0.0 only reads as stale once ``time.monotonic()`` has
        # passed the refresh window; on Linux that clock counts from boot, so a
        # CI runner in its first five minutes served the set as fresh and never
        # kicked the worker. Place the stamp one window behind the clock instead.
        monkeypatch.setattr(
            _argv_floor,
            "_OWN_HOST_RESOLVE_STAMP",
            _argv_floor.time.monotonic() - _argv_floor._OWN_HOST_REFRESH_SECS - 1.0,
        )
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        spawned: "list[dict]" = []

        class _FakeThread:
            def __init__(self, **kw):
                spawned.append(kw)

            def start(self):
                return None

        monkeypatch.setattr(_argv_floor.threading, "Thread", _FakeThread)
        assert "oldname" in _argv_floor._own_host_names()
        assert spawned, "stale complete set must re-kick the enrichment worker"

    def test_fresh_complete_own_set_short_circuits(self, monkeypatch):
        # Within the refresh window the DONE short-circuit serves the cache
        # with no lock taken and no worker spawned.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"oldname"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_STAMP", _argv_floor.time.monotonic())
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)

        def _no_thread(**kw):
            raise AssertionError("no worker may spawn within the refresh window")

        monkeypatch.setattr(_argv_floor.threading, "Thread", _no_thread)
        assert "oldname" in _argv_floor._own_host_names()

    def test_complete_resolve_stamps_the_refresh_clock(self, monkeypatch):
        # A complete worker pass records WHEN it finished, which is what the
        # refresh window above is measured from.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"seed"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_STAMP", 0.0)
        monkeypatch.setattr(
            _argv_floor, "_resolve_own_host_names", lambda: (frozenset({"10.9.9.9"}), True)
        )
        security._resolve_own_host_names_into_cache()
        assert _argv_floor._OWN_HOST_RESOLVE_DONE is True
        assert _argv_floor._OWN_HOST_RESOLVE_STAMP > 0.0
        assert {"seed", "10.9.9.9"} <= (_argv_floor._OWN_HOST_NAMES_CACHE or frozenset())

    def test_partial_resolve_publishes_but_leaves_retryable(self, monkeypatch):
        # A pass where one name enriches and another fails must PUBLISH the
        # successes yet leave DONE False -- otherwise the FQDN that failed to
        # resolve latches a partial set and bypasses the floor forever.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(security.socket, "gethostname", lambda: "MyHost.Example.Com")
        monkeypatch.setattr(security.socket, "getfqdn", lambda: "myhost.example.com")

        def _gai(name, *a, **k):
            if name == "myhost":
                return [(None, None, None, None, ("10.0.0.9", 0))]
            raise OSError("no addr")

        monkeypatch.setattr(security.socket, "getaddrinfo", _gai)
        security._resolve_own_host_names_into_cache()
        assert _argv_floor._OWN_HOST_NAMES_CACHE is not None
        assert {"myhost", "10.0.0.9"} <= _argv_floor._OWN_HOST_NAMES_CACHE
        assert _argv_floor._OWN_HOST_RESOLVE_DONE is False

    def test_hung_resolver_spawns_no_overlapping_workers(self, monkeypatch):
        # A DNS resolve that hangs past the 60s backoff must NOT stack a new
        # daemon per call: the single-flight latch gates the spawn while a
        # worker is alive, and the worker clears it on exit so the retry can
        # spawn again (single-flight, not single-shot).
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        # raising=False so this test also runs against pre-fix code (which lacks
        # the attribute) and fails on the behavioral overlap assert, not on a
        # missing attribute -- the parent's proof pass reverts the hunk and
        # expects THIS test to fail there.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False, raising=False)

        spawned: list[threading.Thread] = []
        hang = threading.Event()

        def _hang():
            spawned.append(threading.current_thread())
            hang.wait(timeout=10)
            return frozenset(), False

        monkeypatch.setattr(security, "_resolve_own_host_names", _hang)

        def _poll_until(pred, timeout=1.0, step=0.01):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if pred():
                    return True
                # flake-ok: pre-existing 10 ms step of a deadline-bounded poll helper, moved verbatim; tracked for a de-flake pass
                time.sleep(step)
            return pred()

        try:
            security._own_host_names()
            assert _poll_until(lambda: len(spawned) == 1), "first worker did not start"
            # Backoff expired again, worker still hung: no new thread may spawn.
            for _ in range(2):
                monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
                security._own_host_names()
            assert len(spawned) == 1, "overlapping resolver workers were spawned"
        finally:
            hang.set()
            for t in spawned:
                t.join(timeout=10)

        # The latch clears when the worker exits, so the retry can spawn again.
        cleared = _poll_until(lambda: _argv_floor._OWN_HOST_RESOLVE_IN_FLIGHT is False)
        assert cleared, "single-flight latch was not cleared on worker exit"
        # A fresh call after the worker exited spawns the retry (hang is set, so
        # this worker returns at once).
        spawned.clear()
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        security._own_host_names()
        try:
            assert _poll_until(lambda: len(spawned) == 1), "retry did not spawn after exit"
        finally:
            for t in spawned:
                t.join(timeout=10)

    def test_complete_resolve_latches_done(self, monkeypatch):
        # A fully successful pass (fqdn + every address) latches DONE so the
        # backoff retry stops.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(security.socket, "gethostname", lambda: "MyHost.Example.Com")
        monkeypatch.setattr(security.socket, "getfqdn", lambda: "myhost.example.com")
        monkeypatch.setattr(
            security.socket,
            "getaddrinfo",
            lambda *a, **k: [(None, None, None, None, ("10.0.0.9", 0))],
        )
        security._resolve_own_host_names_into_cache()
        assert _argv_floor._OWN_HOST_NAMES_CACHE is not None
        assert {"myhost.example.com", "myhost", "10.0.0.9"} <= _argv_floor._OWN_HOST_NAMES_CACHE
        assert _argv_floor._OWN_HOST_RESOLVE_DONE is True

    def test_partial_resolve_merges_without_shrinking(self, monkeypatch):
        # A later partial pass UNIONS into the cache rather than replacing it, so
        # a name learned by an earlier pass is never dropped by one that missed
        # it -- and it still does not latch DONE while incomplete.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"a"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", 0.0)
        monkeypatch.setattr(security, "_resolve_own_host_names", lambda: (frozenset({"b"}), False))
        security._resolve_own_host_names_into_cache()
        assert _argv_floor._OWN_HOST_NAMES_CACHE == frozenset({"a", "b"})
        assert _argv_floor._OWN_HOST_RESOLVE_DONE is False

    def test_resolver_includes_interface_addresses(self, monkeypatch):
        # An interface IP with no DNS record (a DHCP lease, a secondary NIC)
        # still names this machine, so the resolver must union in
        # ``_own_interface_addresses``.  The DNS halves are stubbed to fixed
        # values so the test is network-free; raising=False so it also runs
        # against the pre-fix tree (where the helper is absent) and fails on the
        # missing address rather than on a patch error.
        monkeypatch.setattr(security.socket, "gethostname", lambda: "myhost")
        monkeypatch.setattr(security.socket, "getfqdn", lambda: "myhost.example.com")
        monkeypatch.setattr(security.socket, "getaddrinfo", lambda *a, **k: [])
        monkeypatch.setattr(
            security, "_own_interface_addresses", lambda: {"203.0.113.7"}, raising=False
        )
        resolved, _complete = security._resolve_own_host_names()
        assert "203.0.113.7" in resolved

    def test_own_interface_addresses_returns_parseable_addresses(self):
        # The real helper is best-effort but must only ever return strings that
        # parse as IP addresses.  On Linux the per-interface sweep sees loopback,
        # so the set is non-empty; off-Linux the sweep is skipped, so only the
        # parseability contract is asserted there.
        import ipaddress
        import sys as _sys

        addrs = security._own_interface_addresses()
        for addr in addrs:
            ipaddress.ip_address(addr)  # raises ValueError if unparseable
        if _sys.platform.startswith("linux"):
            assert addrs, "Linux loopback should always enumerate at least one address"

    def test_dotless_hostname_first_contact_is_not_refused(self, monkeypatch, tmp_path):
        # ``ssh dev-dsk '<cmd>'`` -- the allow pin in
        # test_security_denied_commands.py -- must not be refused at first contact
        # by the fail-closed DNS layer.  A dotless name ABSENT
        # from the hosts file answers OPEN while one async worker revalidates
        # through DNS; only DOTTED names keep the fail-closed first contact.
        hosts = tmp_path / "hosts"
        hosts.write_text("127.0.0.1 localhost\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (str(hosts),))
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        started: "list[str]" = []

        class _RecordingThread:
            def __init__(self, *args, **kwargs):
                if kwargs.get("name") == "kirocrew-host-verdict":
                    started.append(kwargs.get("name", ""))

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(_argv_floor.threading, "Thread", _RecordingThread)
        assert _denied_by("ssh dev-dsk 'cd /workplace && git status'") is None
        assert started, "the open answer must still schedule a revalidation worker"

    def test_dotless_hosts_file_alias_denies_same_call(self, monkeypatch, tmp_path):
        # The dotless attack vector -- an /etc/hosts loopback alias -- gets
        # a SAME-CALL verdict from the hosts file (a local read, no DNS, no
        # first-contact refusal), in both the plain and the ``--``-terminated
        # spellings; a hosts entry naming a remote address answers allowed.
        hosts = tmp_path / "hosts"
        hosts.write_text("# test hosts\n127.0.0.1  localhost localalias\n10.4.4.4 farbox\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (str(hosts),))
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        monkeypatch.setattr(
            _argv_floor.threading,
            "Thread",
            lambda *a, **kw: type("_T", (), {"start": lambda s: None})(),
        )
        assert _denied_by("ssh localalias uptime") == self._RULE
        assert _denied_by("ssh -- localalias uptime") == self._RULE
        assert _denied_by("ssh farbox uptime") is None

    def test_dotless_alias_revalidates_through_dns(self, monkeypatch, tmp_path):
        # A dotless loopback alias that exists only in DNS (search domains)
        # is caught at the NEXT decision: the open first answer schedules the
        # worker, and its published verdict flips the cache to deny.
        hosts = tmp_path / "hosts"
        hosts.write_text("")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (str(hosts),))
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        monkeypatch.setattr(
            _argv_floor.threading,
            "Thread",
            lambda *a, **kw: type("_T", (), {"start": lambda s: None})(),
        )
        assert _denied_by("ssh dnsalias uptime") is None
        # What the scheduled worker would have done: resolve to loopback.
        monkeypatch.setattr(
            _argv_floor.socket,
            "getaddrinfo",
            lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        _argv_floor._HOST_VERDICT_PENDING.clear()
        _argv_floor._resolve_host_verdict_into_cache("dnsalias")
        assert _denied_by("ssh dnsalias uptime") == self._RULE


class TestHostAddressesPlatformReaders:
    """The split platform address readers walk real tables end to end.

    ``host_addresses.py`` holds the ctypes/struct wire plumbing behind the
    own-host name set.  The Windows and macOS bodies read this machine's
    adapter tables, so off-platform they are exercised against synthetic
    in-memory chains built at the documented struct offsets; the Linux
    netlink dump is a kernel-local read and runs for real on Linux.
    """

    @staticmethod
    def _win_chain(keepalive):
        """Synthetic IP_ADAPTER_ADDRESSES chain; returns a buf-writer."""
        import ctypes
        import socket
        import struct as _struct

        ptr8 = ctypes.sizeof(ctypes.c_void_p) == 8
        pack_ptr = "=Q" if ptr8 else "=L"
        first_unicast_off = 24 if ptr8 else 16
        sockaddr_off = 16 if ptr8 else 12

        def sockaddr(family, addr_bytes, at):
            blob = bytearray(28)
            _struct.pack_into("=H", blob, 0, family)
            blob[at : at + len(addr_bytes)] = addr_bytes
            buf = ctypes.create_string_buffer(bytes(blob), 28)
            keepalive.append(buf)
            return ctypes.addressof(buf)

        def unicast(sa_addr, next_addr):
            blob = bytearray(32)
            _struct.pack_into(pack_ptr, blob, 8, next_addr)
            _struct.pack_into(pack_ptr, blob, sockaddr_off, sa_addr)
            buf = ctypes.create_string_buffer(bytes(blob), 32)
            keepalive.append(buf)
            return ctypes.addressof(buf)

        def adapter(unicast_addr, next_addr):
            blob = bytearray(40)
            _struct.pack_into(pack_ptr, blob, 8, next_addr)
            _struct.pack_into(pack_ptr, blob, first_unicast_off, unicast_addr)
            buf = ctypes.create_string_buffer(bytes(blob), 40)
            keepalive.append(buf)
            return ctypes.addressof(buf)

        # sockaddr_in: family + port(2..4) + v4 addr at 4..8; sockaddr_in6:
        # family + port + flowinfo, v6 addr at 8..24.  An unknown family and
        # a NULL lpSockaddr entry cover the walker's skip branches.
        sa4 = sockaddr(socket.AF_INET, bytes([10, 11, 12, 13]), 4)
        sa6 = sockaddr(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, "2001:db8::7"), 8)
        sa_odd = sockaddr(99, bytes(4), 4)
        u_odd = unicast(sa_odd, 0)
        u_null = unicast(0, u_odd)
        u6 = unicast(sa6, u_null)
        u4 = unicast(sa4, u6)
        adapter2 = adapter(0, 0)

        def write_into(buf):
            import struct as _s

            psz = 8 if ptr8 else 4
            ctypes.memmove(ctypes.byref(buf, 8), _s.pack(pack_ptr, adapter2), psz)
            ctypes.memmove(ctypes.byref(buf, first_unicast_off), _s.pack(pack_ptr, u4), psz)

        return write_into

    @staticmethod
    def _fake_iphlpapi(rets, writer=None):
        class _Fake:
            def GetAdaptersAddresses(self, family, flags, reserved, buf, size_ref):
                ret = rets.pop(0)
                if ret == 0 and writer is not None:
                    writer(buf)
                return ret

        return _Fake()

    def test_windows_reader_walks_synthetic_adapter_chain(self, monkeypatch):
        import ctypes

        from kiro_crew.security import host_addresses as ha

        keepalive: list = []
        writer = self._win_chain(keepalive)
        # First call reports ERROR_BUFFER_OVERFLOW to cover the resize retry.
        fake = self._fake_iphlpapi([111, 0], writer)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "WinDLL", lambda name: fake, raising=False)
        assert ha._windows_interface_addresses() == {"10.11.12.13", "2001:db8::7"}

    def test_windows_reader_error_paths_are_empty(self, monkeypatch):
        import ctypes

        from kiro_crew.security import host_addresses as ha

        monkeypatch.setattr(sys, "platform", "win32")
        # Hard error: any code other than buffer-overflow aborts the sweep.
        fake_hard = self._fake_iphlpapi([5])
        monkeypatch.setattr(ctypes, "WinDLL", lambda name: fake_hard, raising=False)
        assert ha._windows_interface_addresses() == set()
        # Overflow on every attempt exhausts the retry loop.
        fake_spin = self._fake_iphlpapi([111, 111, 111])
        monkeypatch.setattr(ctypes, "WinDLL", lambda name: fake_spin, raising=False)
        assert ha._windows_interface_addresses() == set()
        # A DLL load failure is swallowed, not raised.
        monkeypatch.setattr(
            ctypes,
            "WinDLL",
            lambda name: (_ for _ in ()).throw(OSError("no iphlpapi")),
            raising=False,
        )
        assert ha._windows_interface_addresses() == set()
        # Off Windows the guard returns before any ctypes work.
        monkeypatch.setattr(sys, "platform", "linux")
        assert ha._windows_interface_addresses() == set()

    @staticmethod
    def _darwin_chain(keepalive):
        """Synthetic BSD ifaddrs chain; returns the head node's address."""
        import ctypes
        import socket

        class _Ifaddrs(ctypes.Structure):
            pass

        _Ifaddrs._fields_ = [
            ("ifa_next", ctypes.POINTER(_Ifaddrs)),
            ("ifa_name", ctypes.c_char_p),
            ("ifa_flags", ctypes.c_uint),
            ("ifa_addr", ctypes.c_void_p),
            ("ifa_netmask", ctypes.c_void_p),
            ("ifa_dstaddr", ctypes.c_void_p),
            ("ifa_data", ctypes.c_void_p),
        ]

        def sockaddr(sa_len, family, addr_bytes, at):
            blob = bytearray(sa_len)
            blob[0] = sa_len
            blob[1] = family
            blob[at : at + len(addr_bytes)] = addr_bytes
            buf = ctypes.create_string_buffer(bytes(blob), sa_len)
            keepalive.append(buf)
            return ctypes.addressof(buf)

        sa4 = sockaddr(16, socket.AF_INET, bytes([172, 16, 5, 9]), 4)
        sa6 = sockaddr(28, socket.AF_INET6, socket.inet_pton(socket.AF_INET6, "2001:db8::9"), 8)
        sa_odd = sockaddr(8, 99, b"", 4)

        nodes = [_Ifaddrs(), _Ifaddrs(), _Ifaddrs(), _Ifaddrs()]
        keepalive.extend(nodes)
        nodes[0].ifa_addr = sa4
        nodes[0].ifa_next = ctypes.pointer(nodes[1])
        nodes[1].ifa_addr = sa6
        nodes[1].ifa_next = ctypes.pointer(nodes[2])
        nodes[2].ifa_addr = None  # NULL sockaddr entry is skipped
        nodes[2].ifa_next = ctypes.pointer(nodes[3])
        nodes[3].ifa_addr = sa_odd  # unknown family contributes nothing
        return ctypes.addressof(nodes[0])

    @staticmethod
    def _fake_libc(head_addr, rc=0):
        import ctypes

        class _Fake:
            def __init__(self):
                self.freed: list = []

            def getifaddrs(self, head_ref):
                if rc == 0:
                    ctypes.cast(head_ref, ctypes.POINTER(ctypes.c_void_p))[0] = head_addr
                return rc

            def freeifaddrs(self, head):
                self.freed.append(True)

        return _Fake()

    def test_darwin_reader_walks_synthetic_ifaddrs_chain(self, monkeypatch):
        import ctypes

        from kiro_crew.security import host_addresses as ha

        keepalive: list = []
        head = self._darwin_chain(keepalive)
        fake = self._fake_libc(head)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ctypes, "CDLL", lambda name, use_errno=False: fake)
        assert ha._darwin_interface_addresses() == {"172.16.5.9", "2001:db8::9"}
        assert fake.freed  # the chain is released in the finally block

    def test_darwin_reader_error_paths_are_empty(self, monkeypatch):
        import ctypes

        from kiro_crew.security import host_addresses as ha

        monkeypatch.setattr(sys, "platform", "darwin")
        fake = self._fake_libc(0, rc=-1)
        monkeypatch.setattr(ctypes, "CDLL", lambda name, use_errno=False: fake)
        assert ha._darwin_interface_addresses() == set()
        assert fake.freed == []  # NULL head is never freed
        monkeypatch.setattr(
            ctypes,
            "CDLL",
            lambda name, use_errno=False: (_ for _ in ()).throw(OSError("no libc")),
        )
        assert ha._darwin_interface_addresses() == set()
        # Off macOS the guard returns before any ctypes work.
        monkeypatch.setattr(sys, "platform", "linux")
        assert ha._darwin_interface_addresses() == set()

    def test_linux_netlink_dump_reads_own_addresses(self, monkeypatch):
        import socket

        from kiro_crew.security import host_addresses as ha

        if sys.platform.startswith("linux") and hasattr(socket, "AF_NETLINK"):
            # Kernel-local RTM_GETADDR dump: loopback is always assigned.
            assert "127.0.0.1" in ha._linux_netlink_addresses()
        # Off Linux the guard returns before any socket is opened.
        monkeypatch.setattr(sys, "platform", "win32")
        assert ha._linux_netlink_addresses() == set()

    @pytest.mark.parametrize(
        "tail", ["done", "error", "timeout", "cap", "intr", "intr-done", "done-errno", "done-short"]
    )
    def test_netlink_dump_counts_only_when_it_completes(self, monkeypatch, tail):
        import socket
        import struct as _struct

        from kiro_crew.security import host_addresses as ha

        def msg(msg_type, payload=b"", flags=0):
            return _struct.pack("=LHHLL", 16 + len(payload), msg_type, flags, 0, 0) + payload

        addr = bytes([socket.AF_INET]) + bytes(7) + _struct.pack("=HH", 8, 2) + bytes([10, 0, 0, 9])
        replies = {
            "done": [msg(20, addr), msg(3, bytes(4))],
            "error": [msg(20, addr), msg(2, bytes(20))],
            "timeout": [msg(20, addr), socket.timeout()],
            "cap": [msg(20, addr)] * 64,
            # NLM_F_DUMP_INTR (0x10): the table changed mid-dump.
            "intr": [msg(20, addr, flags=0x12), msg(3, bytes(4))],
            "intr-done": [msg(20, addr), msg(3, bytes(4), flags=0x12)],
            # NLMSG_DONE carries an int32 errno; nonzero means the dump failed.
            "done-errno": [msg(20, addr), msg(3, _struct.pack("=i", -4))],
            "done-short": [msg(20, addr), msg(3)],
        }[tail]

        class _Sock:
            def __init__(self, *a):
                self.left = list(replies)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def bind(self, *a):
                pass

            def settimeout(self, *a):
                pass

            def send(self, *a):
                pass

            def recv(self, *a):
                item = self.left.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(socket, "AF_NETLINK", 16, raising=False)
        monkeypatch.setattr(ha.socket, "socket", _Sock)
        # A cut-short dump may miss an own secondary: it must count as unread.
        expected = {"10.0.0.9"} if tail == "done" else set()
        assert ha._linux_netlink_addresses() == expected

    def test_netlink_parser_mixed_and_malformed_records(self):
        import socket
        import struct as _struct

        from kiro_crew.security import host_addresses as ha

        def rec(msg_type, family, attrs):
            body = bytes([family]) + bytes(7)  # ifaddrmsg
            attr_blob = b""
            for a_type, payload in attrs:
                a_len = 4 + len(payload)
                attr_blob += _struct.pack("=HH", a_len, a_type) + payload
                attr_blob += b"\x00" * ((-a_len) % 4)
            msg = _struct.pack("=LHHLL", 16 + len(body) + len(attr_blob), msg_type, 0, 0, 0)
            msg += body + attr_blob
            return msg + b"\x00" * ((-len(msg)) % 4)

        v6 = socket.inet_pton(socket.AF_INET6, "2001:db8::42")
        data = (
            # IFA_LOCAL v4 add; unknown attr type skipped; wrong-length no-add.
            rec(20, socket.AF_INET, [(2, bytes([10, 0, 0, 9])), (3, b"xxxx"), (1, b"abcdef")])
            # IFA_ADDRESS v6 add.
            + rec(20, socket.AF_INET6, [(1, v6)])
            # Non-RTM_NEWADDR message is skipped entirely.
            + rec(16, socket.AF_INET, [(1, bytes(4))])
            # Unknown address family contributes nothing.
            + rec(20, 99, [(1, bytes(4))])
        )
        assert ha._parse_netlink_addr_dump(data) == {"10.0.0.9", "2001:db8::42"}
        # A truncated attribute stops the attr walk without raising.
        broken_attr = _struct.pack("=LHHLL", 28, 20, 0, 0, 0) + bytes([2] + [0] * 7)
        broken_attr += _struct.pack("=HH", 2, 1)
        assert ha._parse_netlink_addr_dump(broken_attr) == set()
        # A record announcing msg_len < 16 stops the message walk.
        assert ha._parse_netlink_addr_dump(_struct.pack("=LHHLL", 12, 20, 0, 0, 0)) == set()
        # Offsets helper: same malformed shapes are bounded, not raised.
        assert ha._nlmsg_offsets(_struct.pack("=LHHLL", 12, 20, 0, 0, 0)) == []
        assert ha._nlmsg_offsets(rec(20, socket.AF_INET, [])) == [0]

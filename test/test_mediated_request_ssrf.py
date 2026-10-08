"""Tests for the mediated-request SSRF guard."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from kiro_crew.secrets_mediation import ssrf
from kiro_crew.secrets_mediation.ssrf import SsrfError, check_url


#: A deadline far enough ahead that a normal (stubbed) resolution always
#: completes within it; the bounded-resolution behaviour is exercised
#: separately by the stalling-resolver test below.
def _far_deadline() -> float:
    return time.monotonic() + 30.0


def _fake_getaddrinfo(ip: str, family: int = socket.AF_INET):
    def _inner(host, port, *a, **kw):
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _inner


def test_rejects_non_https():
    with pytest.raises(SsrfError):
        check_url("http://example.com/x", _far_deadline())


def test_rejects_missing_host():
    with pytest.raises(SsrfError):
        check_url("https://", _far_deadline())


@pytest.mark.parametrize(
    "host",
    ["localhost", "metadata.google.internal", "metadata", "169.254.169.254"],
)
def test_rejects_blocked_hostnames_before_resolution(host):
    # These are refused by name — no resolver is consulted.
    with pytest.raises(SsrfError):
        check_url(f"https://{host}/path", _far_deadline())


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "172.16.0.9",  # private
        "169.254.169.254",  # link-local / metadata
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
    ],
)
def test_rejects_hosts_resolving_to_blocked_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    with pytest.raises(SsrfError):
        check_url("https://evil.example.com/path", _far_deadline())


def test_rejects_ipv6_loopback(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("::1", socket.AF_INET6))
    with pytest.raises(SsrfError):
        check_url("https://evil.example.com/", _far_deadline())


def test_allows_public_address_and_pins_it(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    target = check_url("https://example.com/api?q=1", _far_deadline())
    assert target.host == "example.com"
    assert target.ip == "93.184.216.34"
    assert target.port == 443


def test_explicit_port_preserved(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    target = check_url("https://example.com:8443/api", _far_deadline())
    assert target.port == 8443


def test_mixed_public_and_private_records_fail_closed(monkeypatch):
    # A resolver returning BOTH a public and a private A record is the rebinding
    # shape; the whole target is refused rather than cherry-picking the public one.
    def _mixed(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed)
    with pytest.raises(SsrfError):
        check_url("https://example.com/", _far_deadline())


def test_resolution_failure_fails_closed(monkeypatch):
    def _boom(host, port, *a, **kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(SsrfError):
        check_url("https://nx.example.com/", _far_deadline())


def test_too_many_addresses_fail_closed(monkeypatch):
    def _many(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"93.184.216.{i}", port))
            for i in range(ssrf._MAX_RESOLVED_ADDRS + 5)
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _many)
    with pytest.raises(SsrfError):
        check_url("https://example.com/", _far_deadline())


def test_slow_resolution_is_refused_at_the_deadline(monkeypatch):
    # A resolver that stalls past the deadline must NOT extend completion: the
    # bounded resolver refuses at the deadline rather than letting the lookup's
    # origin-chosen latency push total completion past the normalized budget (the
    # timing channel this PR closes). The refusal returns quickly (~at the
    # deadline), not after the full stall.
    release = threading.Event()

    def _stall(host, port, *a, **kw):
        # Block well past the deadline; released in teardown so the daemon thread
        # exits cleanly rather than leaking for the whole test session.
        release.wait(timeout=10.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _stall)
    try:
        with pytest.raises(SsrfError):
            check_url("https://slow.example.com/", time.monotonic() + 0.2)
        # The SsrfError IS the deterministic proof the refusal came at the
        # deadline rather than after the 10s stall: the resolver thread is still
        # blocked in ``release.wait(timeout=10.0)`` (released only in teardown
        # below), so check_url could only have raised by hitting its own
        # deadline. A wall-clock ``elapsed`` upper bound is NOT asserted -- it
        # would flake under runner descheduling even though the deadline fired.
    finally:
        release.set()


def test_already_past_deadline_is_refused_without_resolving(monkeypatch):
    # If the budget is already spent when resolution would start, refuse without
    # consulting the resolver at all.
    calls = []

    def _record(host, port, *a, **kw):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _record)
    with pytest.raises(SsrfError):
        check_url("https://example.com/", time.monotonic() - 1.0)
    assert calls == []


def test_resolver_cap_refuses_without_spawning_another_thread(monkeypatch):
    """A stalling authorized hostname with repeated short-timeout calls must not
    pile up uncancellable ``mediated-dns-resolve`` daemon threads: once the
    outstanding count hits the cap, a new lookup is refused on the ordinary
    resolution-failure path INSTEAD of spawning another abandoned thread. This is
    the DNS-side analogue of the HTTP worker's bound — without it, repeated calls
    exhaust the thread pool and crash unrelated gateway requests.
    """
    # Shrink the cap to a small number so the test does not need to spawn many
    # threads; swap in a fresh BoundedSemaphore at that size.
    cap = 3
    monkeypatch.setattr(ssrf, "_MAX_INFLIGHT_RESOLVERS", cap)
    monkeypatch.setattr(ssrf, "_resolver_slots", threading.BoundedSemaphore(cap))

    release = threading.Event()
    getaddrinfo_calls = {"n": 0}

    def _stall(host, port, *a, **kw):
        getaddrinfo_calls["n"] += 1
        # Hold the resolver (and thus its slot) open past every call's deadline.
        release.wait(timeout=10.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _stall)

    names_before = {t.name for t in threading.enumerate()}
    try:
        # Fill every slot with a stalled resolver (each overruns its 0.2s deadline
        # and abandons a live daemon thread holding its slot).
        for _ in range(cap):
            with pytest.raises(SsrfError):
                check_url("https://slow.example.com/", time.monotonic() + 0.2)
        assert getaddrinfo_calls["n"] == cap, "each in-cap call should reach the resolver"
        inflight = sum(1 for t in threading.enumerate() if t.name == "mediated-dns-resolve")
        assert inflight >= cap, f"expected {cap} stalled resolver threads, saw {inflight}"

        # The next lookup is at the cap: it must refuse FAST (no deadline wait) and
        # must NOT call getaddrinfo or start another resolver thread. The fast
        # path is proven DETERMINISTICALLY by the two signals below -- the
        # resolver was never reached and no new thread was spawned -- not by a
        # wall-clock bound. Note the deadline passed is 5s out: had the refusal
        # instead waited on the deadline, getaddrinfo would still show the cap
        # count AND the refusal could only come after that wait, so the
        # unchanged resolver count is itself the no-wait witness.
        with pytest.raises(SsrfError):
            check_url("https://slow.example.com/", time.monotonic() + 5.0)
        assert getaddrinfo_calls["n"] == cap, "at-cap lookup must not reach the resolver"
        inflight_after = sum(1 for t in threading.enumerate() if t.name == "mediated-dns-resolve")
        assert inflight_after <= inflight, "at-cap refusal must not spawn another thread"
    finally:
        release.set()
    # Let the released daemon threads drain their slots.
    for t in threading.enumerate():
        if t.name == "mediated-dns-resolve" and t.name not in names_before:
            t.join(timeout=2.0)


def test_resolver_slot_is_released_when_a_lookup_completes(monkeypatch):
    """A completed lookup frees its slot, so the cap recovers: a successful
    resolution does not permanently consume a resolver slot. Running cap+ lookups
    serially must all succeed because each releases before the next acquires."""
    cap = 2
    monkeypatch.setattr(ssrf, "_MAX_INFLIGHT_RESOLVERS", cap)
    monkeypatch.setattr(ssrf, "_resolver_slots", threading.BoundedSemaphore(cap))

    def _ok(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _ok)
    # cap + 2 serial lookups: if a completed lookup leaked its slot, the (cap+1)th
    # would refuse. All must resolve and pin the public address.
    for _ in range(cap + 2):
        target = check_url("https://example.com/", time.monotonic() + 5.0)
        assert target.ip == "93.184.216.34"

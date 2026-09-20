"""Unit tests for :mod:`kiro_crew.acp.runtime_registry`.

Covers the registry's contract: register/discard/idempotence, automatic drop-out
of garbage-collected runtimes, deterministic root-pid ordering (pid-less last),
and a thread-safety smoke test that mutates and iterates concurrently.
"""

from __future__ import annotations

import gc
import threading

import pytest

from kiro_crew.acp import runtime_registry


class _FakeRuntime:
    """Minimal stand-in for an AcpRuntime: only a readable ``pid`` is needed."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate each test from the module-level WeakSet in both directions."""
    for rt in runtime_registry.live_runtimes():
        runtime_registry.discard(rt)
    yield
    for rt in runtime_registry.live_runtimes():
        runtime_registry.discard(rt)


def test_register_then_live_returns_the_runtime():
    rt = _FakeRuntime(pid=100)
    runtime_registry.register(rt)
    assert runtime_registry.live_runtimes() == [rt]


def test_register_is_idempotent():
    rt = _FakeRuntime(pid=100)
    runtime_registry.register(rt)
    runtime_registry.register(rt)
    assert runtime_registry.live_runtimes() == [rt]


def test_discard_removes_the_runtime():
    rt = _FakeRuntime(pid=100)
    runtime_registry.register(rt)
    runtime_registry.discard(rt)
    assert runtime_registry.live_runtimes() == []


def test_discard_absent_runtime_is_a_noop():
    rt = _FakeRuntime(pid=100)
    # Never registered — discard must not raise, matching set.discard semantics.
    runtime_registry.discard(rt)
    assert runtime_registry.live_runtimes() == []


def test_gc_collected_runtime_drops_out_automatically():
    survivor = _FakeRuntime(pid=1)
    runtime_registry.register(survivor)

    transient = _FakeRuntime(pid=2)
    runtime_registry.register(transient)
    assert len(runtime_registry.live_runtimes()) == 2

    # Drop the only strong reference; the WeakSet must let it go on collection.
    del transient
    gc.collect()

    assert runtime_registry.live_runtimes() == [survivor]


def test_live_runtimes_sorted_by_root_pid_ascending():
    high = _FakeRuntime(pid=900)
    low = _FakeRuntime(pid=10)
    mid = _FakeRuntime(pid=100)
    # Register out of order to prove ordering is by pid, not insertion.
    for rt in (high, low, mid):
        runtime_registry.register(rt)
    assert runtime_registry.live_runtimes() == [low, mid, high]


def test_runtimes_without_a_pid_sort_last():
    with_pid = _FakeRuntime(pid=500)
    pidless_none = _FakeRuntime(pid=None)
    runtime_registry.register(pidless_none)
    runtime_registry.register(with_pid)

    ordered = runtime_registry.live_runtimes()
    assert ordered[0] is with_pid
    assert ordered[-1] is pidless_none


def test_ordering_is_stable_across_repeated_calls():
    # Two pid-less runtimes fall back to id() as the tiebreaker; whatever order
    # results, it must be identical on every call for the same set.
    runtimes = [_FakeRuntime(pid=None) for _ in range(5)]
    for rt in runtimes:
        runtime_registry.register(rt)
    first = runtime_registry.live_runtimes()
    second = runtime_registry.live_runtimes()
    assert first == second


def test_live_runtimes_returns_a_fresh_snapshot():
    rt = _FakeRuntime(pid=100)
    runtime_registry.register(rt)
    snap = runtime_registry.live_runtimes()
    snap.clear()
    # Mutating the returned list must not affect the registry.
    assert runtime_registry.live_runtimes() == [rt]


def test_thread_safety_smoke_concurrent_register_discard_iterate():
    # Keep strong references so GC does not race the assertion; the point of this
    # test is that concurrent mutation + iteration never raises (e.g. "Set changed
    # size during iteration").
    runtimes = [_FakeRuntime(pid=i) for i in range(200)]
    errors: list[BaseException] = []
    stop = threading.Event()

    def mutate():
        try:
            while not stop.is_set():
                for rt in runtimes:
                    runtime_registry.register(rt)
                for rt in runtimes:
                    runtime_registry.discard(rt)
        except BaseException as exc:  # noqa: BLE001 - surface any race to the test
            errors.append(exc)

    def iterate():
        try:
            while not stop.is_set():
                for rt in runtime_registry.live_runtimes():
                    _ = rt.pid
        except BaseException as exc:  # noqa: BLE001 - surface any race to the test
            errors.append(exc)

    threads = [threading.Thread(target=mutate) for _ in range(3)]
    threads += [threading.Thread(target=iterate) for _ in range(3)]
    for t in threads:
        t.start()
    threading.Event().wait(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not any(t.is_alive() for t in threads)
    assert errors == []


# ── direct AcpClient (one process per session, no AcpRuntime) ────────────────


def test_direct_client_presents_the_sampler_identity_surface(tmp_path):
    """A claude-style direct client is its own process and must present the same
    read-only identity a shared runtime does -- ``pid``, ``agent``,
    ``spawn_monotonic`` and a ``session_owners`` map naming the chat -- or the
    monitor cannot attribute it (it would fold into the gateway's remainder)."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path, agent="claude-dev", session_key="dashboard:chat-9")
    # Before spawn: no pid, no session, so nothing to attribute yet.
    assert client.pid is None
    assert client.spawn_monotonic is None
    assert client.session_owners == {}
    assert client.agent == "claude-dev"

    client._pid = 7777
    client._spawn_monotonic = 123.5
    client._session_id = "acp-direct-1"
    assert client.pid == 7777
    assert client.spawn_monotonic == 123.5
    assert client.session_owners == {"acp-direct-1": "dashboard:chat-9"}


def test_direct_client_leaves_the_registry_on_reset(tmp_path):
    """``_reset_state`` runs on every teardown path; it must discard the client so
    the sampler never walks a /proc tree for a process that is gone."""
    from unittest.mock import patch

    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path)
    client._pid = 8888
    runtime_registry.register(client)
    assert client in runtime_registry.live_runtimes()

    with (
        patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
        patch("kiro_crew.session._untrack_pid"),
        patch("kiro_crew.session._untrack_session_pid"),
    ):
        client._reset_state()
    assert client not in runtime_registry.live_runtimes()
    assert client.pid is None
    assert client.spawn_monotonic is None

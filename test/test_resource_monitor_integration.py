"""End-to-end integration test for the chat resource sampler against live /proc.

Every other resource_monitor test fakes the ``/proc`` layer to isolate one pass
(measurement, CPU delta, attribution). This test does the opposite: it spawns
REAL child processes, registers REAL runtime objects around their pids in the
REAL ``runtime_registry``, and runs the REAL ``ResourceSampler`` against the
host's live ``/proc`` — so the enumeration seam, the ``/proc`` tree walk, the
CPU-delta pass, the dedupe/partition and the gateway-self subtraction are all
exercised together the way the gateway runs them.

Linux-only: the assertions below (a multi-process tree via
``/proc/<pid>/task/<tid>/children``, RSS summed from ``/proc/<pid>/statm``, CPU
ticks from ``/proc/<pid>/stat``) describe Linux ``/proc`` semantics. Off Linux
the sampler uses the single-pid ``ps`` fallback and these tree/partition facts
do not hold, so the module is skipped rather than asserted against.

The runtime objects are deliberately minimal: the sampler reads a runtime purely
through the identity accessors ``pid`` / ``agent`` / ``spawn_monotonic`` /
``session_keys`` (and, when a subagent lookup is injected, the manager record),
so a small object exposing exactly those attributes is a faithful stand-in for a
live ``AcpRuntime`` without spawning a real kiro-cli. The PROCESSES are real; the
runtime WRAPPERS around them are the only fakes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from kiro_crew.acp import resource_monitor as rm
from kiro_crew.acp import runtime_registry

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="integration test asserts Linux /proc tree/RSS/CPU semantics",
)


class _LiveRuntime:
    """A stand-in runtime wrapping a REAL child pid, read via the sampler's
    identity accessors only (``pid`` / ``agent`` / ``spawn_monotonic`` /
    ``session_keys``)."""

    def __init__(
        self,
        pid: int,
        *,
        agent: str,
        session_keys: list[str] | None = None,
        spawn_monotonic: float | None = None,
    ) -> None:
        self.pid = pid
        self.agent = agent
        self.session_keys = list(session_keys or [])
        self.spawn_monotonic = time.monotonic() if spawn_monotonic is None else spawn_monotonic


@pytest.fixture
def clean_registry():
    """Isolate the module-global runtime registry around each test.

    The registry is process-wide state, so a test that registers fake runtimes
    must not leak them into any other test (or inherit one). Snapshot the current
    members, clear, yield, then restore the originals — leaving the registry
    exactly as it was found.
    """
    original = runtime_registry.live_runtimes()
    for rt in original:
        runtime_registry.discard(rt)
    try:
        yield runtime_registry
    finally:
        for rt in runtime_registry.live_runtimes():
            runtime_registry.discard(rt)
        for rt in original:
            runtime_registry.register(rt)


def _spawn_sleep() -> subprocess.Popen:
    """A leaf child process: a bare ``sleep`` with no children of its own.

    Every test child is started in its OWN process group (``start_new_session``)
    so cleanup can signal the whole group — a plain ``terminate()`` on a shell
    root would kill the shell and orphan its backgrounded ``sleep``.
    """
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def _spawn_sleep_with_child() -> subprocess.Popen:
    """A root whose /proc children interface shows a real descendant.

    ``sh -c "sleep 30 & wait"`` keeps the shell alive as the parent (it does not
    ``exec`` the single command away) with a ``sleep`` running as its child, so
    ``_iter_descendant_pids(shell_pid)`` returns two pids — a genuine tree, not a
    lone process. Own process group, see :func:`_spawn_sleep`.
    """
    return subprocess.Popen(["sh", "-c", "sleep 30 & wait"], start_new_session=True)


def _reap_group(child: subprocess.Popen) -> None:
    """Terminate ``child``'s whole process group and reap the root.

    The group id equals the root pid (each child is its own session leader). A
    group already gone raises ``ProcessLookupError``, which is the desired end
    state. Escalates to ``SIGKILL`` if the group ignores ``SIGTERM``.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:  # pragma: no cover — belt and braces
            continue


def _wait_for_tree(root_pid: int, expected: int, timeout_s: float = 2.0) -> list[int]:
    """Poll live /proc until the root's tree has ``expected`` pids (or time out).

    The child of the ``sh`` root is forked asynchronously after Popen returns, so
    a snapshot taken too eagerly could see the shell alone. Reuse the sampler's
    own enumeration primitive so this waits on exactly what the sampler will read.
    """
    deadline = time.monotonic() + timeout_s
    pids = rm._iter_descendant_pids(root_pid)
    while len(pids) < expected and time.monotonic() < deadline:
        time.sleep(0.02)
        pids = rm._iter_descendant_pids(root_pid)
    return pids


@pytest.mark.asyncio
async def test_end_to_end_attribution_against_live_proc(clean_registry) -> None:
    """Spawn a real forest, register runtimes around it, and assert the live
    snapshot attributes, sums, dedupes and computes CPU end-to-end.

    Covers: one entry per runtime (Req 1.1), RSS summed across the real tree
    (Req 1.2), proc_count matching the real tree (Req 2.3), the attributed pids
    partitioning with no overlap and a gateway entry excluding every attributed
    pid (Req 8.4/2.4), and — on the second pass — a real CPU figure within bounds
    (Req 1.3).
    """
    children: list[subprocess.Popen] = []
    try:
        leaf_a = _spawn_sleep()
        leaf_b = _spawn_sleep()
        treed = _spawn_sleep_with_child()
        children = [leaf_a, leaf_b, treed]

        # The forked grandchild appears asynchronously; wait for the two-pid tree
        # so the proc_count assertion is not racing the shell's fork.
        tree_pids = _wait_for_tree(treed.pid, expected=2)
        assert len(tree_pids) == 2, f"expected a 2-pid tree, saw {tree_pids}"

        runtimes = [
            _LiveRuntime(leaf_a.pid, agent="kirocrew", session_keys=["sess-A"]),
            _LiveRuntime(leaf_b.pid, agent="reviewer", session_keys=["sess-B"]),
            _LiveRuntime(treed.pid, agent="kirocrew", session_keys=["sess-T"]),
        ]
        for rt in runtimes:
            clean_registry.register(rt)

        # sess-A resolves to a dashboard slot (a chat); the others do not (workers).
        sampler = rm.ResourceSampler(
            slot_resolver=lambda key: "slot-A" if key == "sess-A" else None,
            interval_s=0.0,
        )

        first = await sampler.snapshot()

        runtime_pids = {leaf_a.pid, leaf_b.pid, treed.pid}
        runtime_entries = [e for e in first.entries if e.pid in runtime_pids]
        gateway_entries = [e for e in first.entries if e.kind == "gateway"]

        # One entry per registered runtime (Req 1.1).
        assert {e.pid for e in runtime_entries} == runtime_pids
        assert len(runtime_entries) == 3

        by_pid = {e.pid: e for e in runtime_entries}

        # The slot-resolving runtime is a chat; the others are workers.
        assert by_pid[leaf_a.pid].kind == "chat"
        assert by_pid[leaf_a.pid].slot == "slot-A"
        assert by_pid[leaf_b.pid].kind == "worker"
        assert by_pid[treed.pid].kind == "worker"

        # RSS summed across each real tree is a positive number of MB (Req 1.2).
        for entry in runtime_entries:
            assert entry.rss_mb is not None and entry.rss_mb > 0.0

        # proc_count matches the real tree: leaves are 1, the shell tree is 2
        # (Req 2.3 — a descendant is part of exactly its root's tree).
        assert by_pid[leaf_a.pid].proc_count == 1
        assert by_pid[leaf_b.pid].proc_count == 1
        assert by_pid[treed.pid].proc_count == 2
        assert by_pid[treed.pid].pids == frozenset(tree_pids)

        # uptime is derived from the recorded spawn instant — a small positive.
        for entry in runtime_entries:
            assert entry.uptime_s is not None and entry.uptime_s >= 0.0

        # First sight of every root → CPU unavailable, never a fabricated 0.0
        # (Req 1.4).
        for entry in runtime_entries:
            assert entry.cpu_pct is None

        # The attributed pids form a true partition: no pid in two entries
        # (Req 8.4), and a gateway entry (this test process's own tree) excludes
        # every attributed pid (Req 2.4).
        all_attributed = [pid for e in first.entries for pid in e.pids]
        assert len(all_attributed) == len(set(all_attributed))
        assert len(gateway_entries) == 1
        gateway = gateway_entries[0]
        assert gateway.pids.isdisjoint(runtime_pids)
        assert set(gateway.pids).isdisjoint(pid for e in runtime_entries for pid in e.pids)

        # Do a little CPU work so the second pass has a non-zero-but-bounded delta.
        end = time.monotonic() + 0.05
        while time.monotonic() < end:
            pass

        # Second pass (interval_s=0.0 defeats the cache): the CPU-delta baseline
        # now exists, so CPU is a real figure within [0, 100 * cpu_count]
        # (Req 1.3).
        second = await sampler.snapshot()
        second_runtime_entries = [e for e in second.entries if e.pid in runtime_pids]
        assert len(second_runtime_entries) == 3
        import os as _os

        ceiling = 100.0 * (_os.cpu_count() or 1)
        for entry in second_runtime_entries:
            assert entry.cpu_pct is not None
            assert 0.0 <= entry.cpu_pct <= ceiling

        # Killing one runtime's process drops its entry on the next snapshot,
        # with no error (Req 1.6). In the gateway this happens because
        # ``AcpRuntime._mark_dead`` discards the dead runtime from the registry,
        # which removes it from enumeration — mirror that here by killing the
        # real child and discarding its wrapper the way the death path does.
        leaf_b.terminate()
        leaf_b.wait(timeout=5)
        clean_registry.discard(runtimes[1])
        third = await sampler.snapshot()
        third_pids = {e.pid for e in third.entries if e.pid in runtime_pids}
        assert leaf_b.pid not in third_pids
        assert {leaf_a.pid, treed.pid} <= third_pids
    finally:
        for child in children:
            _reap_group(child)

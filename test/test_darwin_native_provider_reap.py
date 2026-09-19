"""Native Darwin teardown contracts, including idle and warm-pool expiry.

Only the provider protocol and clock are faked. Process identities, zombie
reads, descendant discovery and signals use the real macOS kernel. Tiny Python
children stand in for the runtime/MCP tree; this is not a live gateway test.
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import session, session_pid, session_pool
from kiro_crew.acp.client import _capture_child_records
from kiro_crew.config import KiroCrewConfig

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="requires the real Darwin kernel"),
    pytest.mark.timeout(60),
    pytest.mark.xdist_group(name="subprocess_spawn"),
]

# Children cannot leave the group until pytest has recorded EVERY child identity.
# Thus a failed handshake can always clean up the still-owned root group. No child
# imports the product, invokes an agent, or reads the operator's configuration.
_CHILD = """
import os, signal, sys, time
if sys.stdin.buffer.read(1) != b'g':
    raise SystemExit(1)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[1] == 'escape':
    os.setsid()
print('ready', flush=True)
time.sleep(30)
"""
_ROOT = """
import subprocess, sys
kids = []
try:
    for mode in ('group', 'group', 'escape'):
        p = subprocess.Popen([sys.executable, '-c', sys.argv[1], mode],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        kids.append(p)
        print(p.pid, flush=True)
    if sys.stdin.buffer.read(1) != b'g':
        raise SystemExit(1)
    for p in kids:
        p.stdin.write(b'g')
        p.stdin.flush()
        if p.stdout.readline() != b'ready\\n':
            raise SystemExit(1)
    print('ready', flush=True)
    sys.stdin.buffer.read(1)
finally:
    for p in kids:
        p.kill()
        p.wait(timeout=5)
"""


def _await(predicate, message):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)


def _line(pipe):
    """Bound each handshake without a reader thread or buffered read-ahead."""
    deadline = time.monotonic() + 5
    data = bytearray()
    while not data.endswith(b"\n"):
        ready, _, _ = select.select([pipe], [], [], max(0, deadline - time.monotonic()))
        assert ready, "native process handshake timed out"
        byte = os.read(pipe.fileno(), 1)
        assert byte, "native process exited before handshake"
        data.extend(byte)
        assert len(data) < 128, "invalid native process handshake"
    return bytes(data).strip()


def _stopped(pid):
    """Independent exit oracle: do not use the zombie helper being tested."""
    result = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode in (0, 1), result.stderr
    states = result.stdout.split()
    if not states:
        assert result.returncode == 1, "ps returned no process state without reporting absence"
        return True
    return all(state.startswith("Z") for state in states)


@contextmanager
def _tree(tmp_path):
    """Own the tree from spawn through teardown, including failed setup/mutants."""
    root = subprocess.Popen(
        [sys.executable, "-c", _ROOT, _CHILD],
        start_new_session=True,
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    identities = {}
    # Keep the cleanup identity oracle independent of a scoped test mutation.
    identity = pc.get_process_start_id
    try:
        start = identity(root.pid)
        assert start is not None
        identities[root.pid] = start
        for _ in range(3):
            child = int(_line(root.stdout))
            token = identity(child)
            assert token is not None
            identities[child] = token
        root.stdin.write(b"g")
        root.stdin.flush()
        assert _line(root.stdout) == b"ready"
        group_a, group_b, escapee = list(identities)[1:]
        assert os.getpgid(group_a) == os.getpgid(group_b) == root.pid
        assert os.getpgid(escapee) == escapee
        records = _capture_child_records([group_a, group_b, escapee])
        assert set(records) == {group_a, group_b, escapee}
        assert all(token == identities[pid] and name for pid, (token, name) in records.items())
        client = SimpleNamespace(
            _pid=root.pid,
            _start_time=start,
            _child_pids=records,
        )
        provider = SimpleNamespace(
            _client=client,
            client=client,
            _proc=None,
            _active_proc=None,
            shutdown=AsyncMock(),
            is_process_alive=lambda: not _stopped(root.pid),
            cwd=str(tmp_path),
            session_id="",
            exit_code=None,
        )
        yield SimpleNamespace(
            root=root,
            identities=identities,
            provider=provider,
            group_a=group_a,
            group_b=group_b,
            escapee=escapee,
        )
    finally:
        # A native group kill covers children not yet announced during setup.
        # An unreaped Popen child still owns its pid; after production reaps it,
        # only an identity-verified member can authorize this group's cleanup.
        witness = any(
            identity(pid) == token and pc.pgroup_of(pid) == root.pid
            for pid, token in identities.items()
        )
        if witness or (not identities and root.poll() is None):
            try:
                os.killpg(root.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid, token in reversed(list(identities.items())):
            if identity(pid) == token:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        root.wait(timeout=5)
        root.stdin.close()
        root.stdout.close()
        for pid in identities:
            _await(lambda: _stopped(pid), f"fixture cleanup left pid {pid} running")


@pytest.fixture
def native(monkeypatch, tmp_path):
    # Shorten only this test process's escalation wait, never live configuration.
    monkeypatch.setattr(session_pid, "_PROVIDER_TERM_GRACE_SECONDS", 0.1)
    # The sibling is not in the provider records/group. Every scenario must spare it.
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        cwd=tmp_path,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(session, "subprocess_executor", lambda: pool)
            yield tmp_path
            assert sibling.poll() is None, "cleanup killed an unrelated runtime"
    finally:
        sibling.kill()
        sibling.wait(timeout=5)


def _assert_stopped(tree, *, root_reaped):
    for pid in tree.identities:
        _await(lambda: _stopped(pid), f"production left pid {pid} running")
    if root_reaped:
        # Read without waitpid/poll: a test-side reap would conceal a broken reaper.
        _await(lambda: not pc.pid_exists(tree.root.pid), "production did not reap its root")


def test_native_zombie_retains_identity_and_is_reaped(native):
    with _tree(native) as tree:
        token = tree.identities[tree.root.pid]
        tree.root.send_signal(signal.SIGTERM)
        _await(lambda: _stopped(tree.root.pid), "root did not exit")
        assert pc.pid_exists(tree.root.pid), "precondition: root must still be a zombie"
        assert pc._darwin_libproc_start_id(tree.root.pid) is None
        assert pc.get_process_start_id(tree.root.pid) == token
        assert pc.darwin_pid_is_zombie(tree.root.pid) is True
        members = pc.darwin_pgroup_members(tree.root.pid)
        assert members is not None and members
        assert any(m.pid == tree.root.pid and m.zombie for m in members)
        assert any(m.pid == tree.group_a and not m.zombie for m in members)

        session_pid._sync_kill_provider(tree.provider)

        _assert_stopped(tree, root_reaped=True)


def test_native_reaped_root_recovers_group_and_escaped_descendant(native):
    with _tree(native) as tree:
        # An unrecorded member models a fork after the runtime's last snapshot.
        del tree.provider._client._child_pids[tree.group_b]
        tree.root.send_signal(signal.SIGTERM)
        tree.root.wait(timeout=5)
        assert not pc.pid_exists(tree.root.pid)
        assert all(not _stopped(pid) for pid in (tree.group_a, tree.group_b, tree.escapee))

        session_pid._sync_kill_provider(tree.provider)

        _assert_stopped(tree, root_reaped=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["idle", "pool_health", "pool_claim"])
async def test_native_expiry_repeatedly_reclaims_whole_trees(native, monkeypatch, path):
    cfg = KiroCrewConfig()
    cfg.session.pool_size = 2
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 1800
    manager = session.SessionManager(cfg, provider_factory=None)
    # Only prevent creating replacement agents; expiry, reset, discard and signals stay real.
    monkeypatch.setattr(manager, "_schedule_replenish", lambda: None)
    clock = [100_000.0]
    cleanup = manager._cleanup_boundary()
    monkeypatch.setattr(cleanup, "_deps", replace(cleanup._deps, monotonic=lambda: clock[0]))
    monkeypatch.setattr(session_pool, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    for cycle in range(3):
        with _tree(native) as tree:
            provider = tree.provider
            key = f"test:native-expiry-{cycle}"
            if path == "idle":
                entry = session._Session(provider=provider, last_used=clock[0])
                manager._sessions[key] = entry
                await asyncio.wait_for(manager._expire_idle(14400), 10)
                assert manager._sessions[key] is entry
                assert all(not _stopped(pid) for pid in tree.identities)
                clock[0] += 14401
                await asyncio.wait_for(manager._expire_idle(14400), 10)
                assert key not in manager._sessions
            else:
                manager._warm_pool.put_nowait((provider, clock[0]))
                await asyncio.wait_for(manager._sweep_warm_pool_once(), 10)
                assert manager._warm_pool.qsize() == 1
                assert all(not _stopped(pid) for pid in tree.identities)
                clock[0] += 1801
                if path == "pool_health":
                    await asyncio.wait_for(manager._sweep_warm_pool_once(), 10)
                else:
                    assert await asyncio.wait_for(manager._drain_and_claim(None), 10) is None
                assert manager._warm_pool.empty()
                assert manager._pool_sweep_pids == set()
            provider.shutdown.assert_awaited_once()
            # reset lets the transport reap its root; pool fallback itself owns the reap.
            _assert_stopped(tree, root_reaped=path != "idle")
            seen.extend(tree.identities)
            assert all(_stopped(pid) for pid in seen), "live descendants accumulated across cycles"

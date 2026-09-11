"""Request-driven caller-repair tests for the MCP stub (recaller path).

Drive real startup and bridge code with an in-memory transport. Identity may
appear after initialization, but repair must precede the first request that can
resolve it. Slow probes survive reconnects without accumulating executor jobs.
The first-call test also replays its captured wire through the real broker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import kiro_crew.mcp_caller
from kiro_crew import member_memory_auth, session_pid_sig
from kiro_crew.mcp_gateway import stub as stub_mod

# Pin to one xdist worker (requires --dist loadgroup) alongside the other
# mcp_gateway suites.
pytestmark = pytest.mark.xdist_group("mcp_gateway")


# Required argv fields matching the rewriter's generated args.
_REQUIRED_STUB_ARGV_FIELDS = (
    "--server", "fake-mcp",
    "--agent", "kirocrew",
    "--target-command", "/usr/bin/true",
    "--target-args=--foo|bar",
    "--sandbox-mode", "standard",
    "--work-dir", "/tmp",
    "--env", "FOO=bar,BAZ=qux",
    "--auto-approve", "ToolA,ToolB",
    "--approval-mode", "interactive",
    "--channel-id", "C0AUNEY55NV",
    "--socket", "/tmp/gw.sock",
)


def _parse(argv: list[str]) -> argparse.Namespace:
    return stub_mod._parse_args(argv)


# --- session_key resolves from warm-pool PID file when env absent -----------


def test_stub_session_key_from_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-pool: ``KIROCREW_SESSION_KEY`` is absent at register time, but a
    ``session_pid_<pid>.txt`` exists in the config dir (written once the
    session is claimed). ``build_register_payload`` must resolve the key via
    the ancestor walk (``CallerContext.from_env``) so the Register carries a
    real caller instead of an empty one — otherwise gatewayd stamps
    ``caller=None`` and state-mutating tools break."""
    # Reset the process-lifetime from_env cache so an identity resolved by
    # another test cannot leak in (and monkeypatch teardown reverts whatever
    # this test caches).
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

    from kiro_crew.config.loader import config_dir

    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    # from_env walks from os.getppid() upward; the immediate ancestor's file
    # is found on the first iteration.
    (cfg / f"session_pid_{os.getppid()}.txt").write_text(
        "dashboard:chat-9-42", encoding="utf-8"
    )

    argv = list(_REQUIRED_STUB_ARGV_FIELDS)
    for i, tok in enumerate(argv):
        if tok == "--channel-id":
            del argv[i : i + 2]
            break
    payload = stub_mod.build_register_payload(_parse(argv))
    assert payload["session_key"] == "dashboard:chat-9-42"
    assert payload["caller"]["session_key"] == "dashboard:chat-9-42"
    assert payload["caller"]["session_type"] == "dashboard"


# --- Caller repair shares the bridge's frame writer ------------------------


class _CapWriter:
    """Minimal StreamWriter double capturing whole JSON frames."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line:
                self.frames.append(json.loads(line.decode("utf-8")))

    async def drain(self) -> None:
        return None


class _CriticalSectionWriter:
    """StreamWriter double that detects a broken write+drain critical section:
    its drain() yields the loop, and it flags if any OTHER write() lands between
    a given writer's write() and its drain() completing. Serializing write+drain
    under the shared lock must prevent that."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._in_flight = False
        self.overlap = False

    def write(self, data: bytes) -> None:
        if self._in_flight:
            # Another writer's write+drain was still in progress.
            self.overlap = True
        for line in data.split(b"\n"):
            if line:
                self.frames.append(json.loads(line.decode("utf-8")))

    async def drain(self) -> None:
        self._in_flight = True
        try:
            await asyncio.sleep(0)  # yield: a concurrent write() would overlap
        finally:
            self._in_flight = False


@pytest.mark.asyncio
async def test_write_frame_serializes_concurrent_writers() -> None:
    """stdin_pump and _recaller_loop share one writer and both await drain().
    Without serialization, one coroutine's write() lands inside another's
    write+drain critical section (the interleave that corrupts the socket
    stream). _write_frame must hold the writer's _mc_write_lock across write+
    drain so the sections never overlap."""
    w = _CriticalSectionWriter()
    setattr(w, "_mc_write_lock", asyncio.Lock())

    async def spam(tag: str) -> None:
        for i in range(20):
            await stub_mod._write_frame(w, {"type": tag, "n": i})

    await asyncio.gather(spam("a"), spam("b"))

    assert not w.overlap, "write+drain critical sections overlapped (not serialized)"
    assert len(w.frames) == 40


@pytest.mark.asyncio
async def test_run_bridge_installs_write_lock() -> None:
    """run_bridge must install a _mc_write_lock on the shared writer so its two
    writer coroutines serialize. Verified by driving a minimal bridge to EOF."""
    reader = asyncio.StreamReader()
    reader.feed_eof()  # immediate EOF -> stdin_pump sends Unregister and exits
    w = _CapWriter()
    stop = asyncio.Event()
    stdin = asyncio.StreamReader()
    stdin.feed_eof()
    await asyncio.wait_for(
        stub_mod.run_bridge(
            reader, w, stop, stdin=stdin, stdout_writer=w,
            session=stub_mod.StubSession(),
        ),
        timeout=2.0,
    )
    assert getattr(w, "_mc_write_lock", None) is not None


class _FrameStream(_CapWriter):
    """In-memory wire with observable frames, without OS pipes or threads."""

    def __init__(self) -> None:
        super().__init__()
        self.received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.reader = asyncio.StreamReader()

    def write(self, data: bytes) -> None:
        start = len(self.frames)
        super().write(data)
        for frame in self.frames[start:]:
            self.received.put_nowait(frame)

    def feed(self, frame: dict[str, Any]) -> None:
        self.reader.feed_data(json.dumps(frame).encode() + b"\n")

    async def next_frame(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.received.get(), timeout=5)

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _StubPeer(_FrameStream):
    """Reply to the real handshake and MCP requests; retain the exact wire."""

    def write(self, data: bytes) -> None:
        start = len(self.frames)
        super().write(data)
        for frame in self.frames[start:]:
            if frame.get("type") == "register":
                self.feed(
                    {
                        "type": "registered",
                        "capabilities": ["poolable_ack", "session_bound_ack"],
                    }
                )
            elif "method" in frame and "id" in frame:
                result: dict[str, Any] = {"content": []}
                if frame["method"] == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "race-peer", "version": "1"},
                    }
                self.feed({"jsonrpc": "2.0", "id": frame["id"], "result": result})


@asynccontextmanager
async def _initialized_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    initial_key: str = "",
    explicit: bool = True,
) -> AsyncIterator[tuple[_StubPeer, _FrameStream, asyncio.StreamReader]]:
    """Run real startup/bridge; inject only transport, stdio and host probes."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in ("KIROCREW_SESSION_KEY", "KIROCREW_HOST_PID", "KIROCREW_CHANNEL_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    # Exercise the V1 ancestor-file path, with no dependency on host ancestry
    # beyond the immediate parent whose mapping this test publishes.
    monkeypatch.setattr(member_memory_auth, "protected_member_session_for_pid", lambda _pid: None)
    monkeypatch.setattr(kiro_crew.mcp_caller, "_parent_pid", lambda _pid: 0)
    monkeypatch.setattr(stub_mod, "_ancestor_pids", lambda: [])
    monkeypatch.setattr(stub_mod, "pool_binary_version", lambda *_args: "race-test")
    monkeypatch.setattr(stub_mod, "configure_default_executor", lambda: None)
    monkeypatch.setattr(stub_mod, "_install_signal_handlers", lambda *_args: None)
    monkeypatch.setattr(stub_mod, "alog_fallback", AsyncMock())
    monkeypatch.setattr(
        stub_mod, "fallback_exec", lambda _args: pytest.fail("unexpected direct fallback")
    )
    monkeypatch.setattr(session_pid_sig, "_load_hmac_key", lambda: b"r" * 32)
    if initial_key:
        if explicit:
            monkeypatch.setenv("KIROCREW_SESSION_KEY", initial_key)
        else:
            await asyncio.to_thread(session_pid_sig.publish_session_pid, os.getppid(), initial_key)

    async def parked_poll(_writer, _channel_id, stop_event):
        # Force first-tool-before-poll by synchronization, never a short sleep.
        await stop_event.wait()

    # Also works once production deletes the timer: no new API is imported or
    # called by the test, so the original fails on identity/order alone.
    monkeypatch.setattr(stub_mod, "_recaller_loop", parked_poll, raising=False)
    peer, output = _StubPeer(), _FrameStream()
    stdin = asyncio.StreamReader()
    monkeypatch.setattr(stub_mod.transport, "connect", AsyncMock(return_value=(peer.reader, peer)))
    real_bridge = stub_mod.run_bridge

    async def injected_bridge(*args, **kwargs):
        await real_bridge(*args, stdin=stdin, stdout_writer=output, **kwargs)

    monkeypatch.setattr(stub_mod, "run_bridge", injected_bridge)
    argv = [
        "--server",
        "kirocrew-core",
        "--agent",
        "race-test",
        "--target-command",
        sys.executable,
        "--work-dir",
        str(tmp_path),
        "--socket",
        str(tmp_path / "in-memory"),
        "--poolable",
    ]
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(stub_mod, "subprocess_executor", lambda: executor)
        task = asyncio.create_task(stub_mod._amain(argv))
        try:
            register = await peer.next_frame()
            assert register["type"] == "register"
            assert register["session_key"] == initial_key
            assert register["session_bound"] is bool(initial_key and explicit)
            stdin.feed_data(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "race-client", "version": "1"},
                        },
                    }
                ).encode()
                + b"\n"
            )
            assert (await output.next_frame())["id"] == 0
            stdin.feed_data(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            yield peer, output, stdin
        finally:
            stdin.feed_eof()
            try:
                await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def _ledger_request(
    stdin: asyncio.StreamReader, output: _FrameStream, request_id: int
) -> None:
    stdin.feed_data(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "session_ledger_read", "arguments": {}},
            }
        ).encode()
        + b"\n"
    )
    assert (await output.next_frame())["id"] == request_id


@pytest.mark.asyncio
async def test_first_ledger_request_repairs_caller_before_forwarding(monkeypatch, tmp_path) -> None:
    """A PID mapping published after initialization must identify the FIRST call."""
    from test_mcp_gateway_recaller import _FakePool, _run

    from kiro_crew.mcp_gateway import socketsec

    session_key = "dashboard:chat-first-ledger"
    async with _initialized_stub(monkeypatch, tmp_path) as (peer, output, stdin):
        # Same publication entry point as publish_turn_identity, after both
        # registration and MCP initialization, before any ledger request.
        await asyncio.to_thread(session_pid_sig.publish_session_pid, os.getppid(), session_key)
        assert (
            await asyncio.to_thread(session_pid_sig.read_session_pid_txt, os.getppid(), tmp_path)
            == session_key
        )
        await _ledger_request(stdin, output, 1)
        wire = list(peer.frames)

    # Replay the captured wire through the REAL broker handler. No kernel-peer
    # fallback or parent claim may repair the registration on this second pass.
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: None)
    monkeypatch.setattr(_FakePool, "release_exclusive", AsyncMock(return_value=None), raising=False)
    backend, _audit = await _run(wire, monkeypatch)
    caller = backend.callers[-1]
    order = [
        frame.get("type", frame.get("method"))
        for frame in wire
        if frame.get("type") == "recaller" or frame.get("method") == "tools/call"
    ]
    assert (
        caller is not None and caller.session_key == session_key
    ), f"first ledger request reached broker without its caller: {caller!r}; wire order={order}"
    assert order == ["recaller", "tools/call"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_lookup", ["missing", "error", "resolver-timeout"])
async def test_request_retries_unresolved_caller_then_stops_after_repair(
    monkeypatch,
    tmp_path,
    first_lookup,
) -> None:
    """Missing identity or a failed probe must not strand this connection."""
    session_key = "dashboard:chat-later-ledger"
    async with _initialized_stub(monkeypatch, tmp_path) as (peer, output, stdin):
        resolve = stub_mod._build_caller_block
        lookups = []
        loop_thread = threading.get_ident()

        def probe(channel_id):
            assert threading.get_ident() != loop_thread, "caller lookup blocked the event loop"
            lookups.append(channel_id)
            if first_lookup == "error" and len(lookups) == 1:
                raise OSError("transient mapping read failure")
            if first_lookup == "resolver-timeout" and len(lookups) == 1:
                raise TimeoutError("resolver failed before returning identity")
            return resolve(channel_id)

        monkeypatch.setattr(stub_mod, "_build_caller_block", probe)
        await _ledger_request(stdin, output, 1)
        assert not any(frame.get("type") == "recaller" for frame in peer.frames)
        await asyncio.to_thread(session_pid_sig.publish_session_pid, os.getppid(), session_key)
        await _ledger_request(stdin, output, 2)
        await _ledger_request(stdin, output, 3)
        order = [
            frame.get("type", frame.get("method"))
            for frame in peer.frames
            if frame.get("type") == "recaller" or frame.get("method") == "tools/call"
        ]
        assert order == ["tools/call", "recaller", "tools/call", "tools/call"]
        assert len(lookups) == 2, "a successful repair must stop further identity probes"
        recaller = next(frame for frame in peer.frames if frame.get("type") == "recaller")
        assert recaller["caller"]["session_key"] == session_key


@pytest.mark.asyncio
async def test_notifications_and_responses_do_not_trigger_caller_probe(
    monkeypatch, tmp_path
) -> None:
    """Only outbound requests need repair; notifications/replies stay transparent."""
    async with _initialized_stub(monkeypatch, tmp_path) as (peer, _output, stdin):
        probes = []
        resolve = stub_mod._build_caller_block

        def probe(channel_id):
            probes.append(channel_id)
            return resolve(channel_id)

        monkeypatch.setattr(stub_mod, "_build_caller_block", probe)
        messages = [
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
            {"jsonrpc": "2.0", "id": "server-request", "result": {}},
        ]
        for message in messages:
            stdin.feed_data(json.dumps(message).encode() + b"\n")
            while await peer.next_frame() != message:
                pass
        assert probes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-child", "existing-pid-mapping"])
async def test_identified_registration_bypasses_caller_repair(
    monkeypatch, tmp_path, explicit
) -> None:
    """Neither an explicit child nor an already resolved V1 stub needs repair."""
    session_key = "subagent:child" if explicit else "dashboard:chat-already-identified"
    async with _initialized_stub(
        monkeypatch,
        tmp_path,
        initial_key=session_key,
        explicit=explicit,
    ) as (peer, output, stdin):
        lookups = []
        resolve = stub_mod._build_caller_block

        def probe(channel_id):
            lookups.append(channel_id)
            return resolve(channel_id)

        monkeypatch.setattr(stub_mod, "_build_caller_block", probe)
        await _ledger_request(stdin, output, 1)
        await _ledger_request(stdin, output, 2)
        assert lookups == []
        assert not any(frame.get("type") == "recaller" for frame in peer.frames)


def _hold_caller_lookups(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Future]:
    """Control executor completion without a sleeping or wedged worker thread."""
    loop = asyncio.get_running_loop()
    real_submit = loop.run_in_executor
    pending = []

    def submit(executor, function, *args):
        if function is stub_mod._build_caller_block:
            future = loop.create_future()
            pending.append(future)
            return future
        return real_submit(executor, function, *args)

    monkeypatch.setattr(loop, "run_in_executor", submit)
    # An immediate deadline exercises timeout deterministically. The shield
    # must preserve the underlying pending future instead of cancelling it.
    monkeypatch.setattr(stub_mod, "_CALLER_REPAIR_TIMEOUT_SECS", 0, raising=False)
    return pending


async def _reconnect_stub(monkeypatch: pytest.MonkeyPatch, old_peer: _StubPeer) -> _StubPeer:
    peer = _StubPeer()
    monkeypatch.setattr(stub_mod.transport, "connect", AsyncMock(return_value=(peer.reader, peer)))
    old_peer.reader.feed_eof()
    assert (await peer.next_frame())["type"] == "register"
    assert (await peer.next_frame())["method"] == "initialize"
    return peer


@pytest.mark.asyncio
async def test_timed_out_lookup_survives_reconnect_without_new_jobs(monkeypatch, tmp_path) -> None:
    """A late result belongs to the current socket, never the socket it outlived."""
    async with _initialized_stub(monkeypatch, tmp_path) as (peer, output, stdin):
        pending = _hold_caller_lookups(monkeypatch)
        peers = [peer]
        try:
            await _ledger_request(stdin, output, 1)
            assert len(pending) == 1
            assert not pending[0].done(), "timeout cancelled the underlying caller lookup"
            await _ledger_request(stdin, output, 2)
            assert len(pending) == 1, "a second request queued another lookup"
            for request_id in (3, 4):
                peer = await _reconnect_stub(monkeypatch, peer)
                peers.append(peer)
                await _ledger_request(stdin, output, request_id)
                assert len(pending) == 1, "reconnect discarded the pending lookup"
                assert not pending[0].done()

            session_key = "dashboard:chat-after-reconnect"
            completed = asyncio.Event()
            pending[0].add_done_callback(lambda _future: completed.set())
            pending[0].set_result(
                {
                    "session_key": session_key,
                    "session_type": "dashboard",
                    "principal_id": "",
                    "channel_id": "",
                }
            )
            # Completion alone must never write a frame to any captured socket.
            await asyncio.wait_for(completed.wait(), timeout=5)
            assert not any(
                frame.get("type") == "recaller" for item in peers for frame in item.frames
            )
            await _ledger_request(stdin, output, 5)
            await _ledger_request(stdin, output, 6)
            assert len(pending) == 1
            for old_peer in peers[:-1]:
                assert not any(frame.get("type") == "recaller" for frame in old_peer.frames)
            repaired = [frame for frame in peer.frames if frame.get("type") == "recaller"]
            assert len(repaired) == 1
            assert repaired[0]["caller"]["session_key"] == session_key
            order = [
                frame.get("type", frame.get("id"))
                for frame in peer.frames
                if frame.get("type") == "recaller" or frame.get("method") == "tools/call"
            ]
            assert order == [4, "recaller", 5, 6]
            # A successful repair belongs to one connection. A later keyless
            # registration must clear the sent flag and perform a fresh lookup.
            peer = await _reconnect_stub(monkeypatch, peer)
            await _ledger_request(stdin, output, 7)
            assert len(pending) == 2
            pending[1].set_result(
                {
                    "session_key": session_key,
                    "session_type": "dashboard",
                    "principal_id": "",
                    "channel_id": "",
                }
            )
            await _ledger_request(stdin, output, 8)
            assert len([frame for frame in peer.frames if frame.get("type") == "recaller"]) == 1
        finally:
            for future in pending:
                future.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["before-request", "during-request"])
async def test_first_ledger_request_refreshes_inherited_empty_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, completion: str
) -> None:
    """A retained miss must not hide identity published before this ledger call."""
    from test_mcp_gateway_recaller import _FakePool, _run

    from kiro_crew.mcp_gateway import socketsec

    session_key = "dashboard:chat-stale-probe"
    async with _initialized_stub(monkeypatch, tmp_path) as (peer, output, stdin):
        stale_caller = await asyncio.to_thread(stub_mod._build_caller_block, None)
        assert stale_caller["session_key"] == ""
        loop = asyncio.get_running_loop()
        real_submit = loop.run_in_executor
        pending = _hold_caller_lookups(monkeypatch)
        request_task = None
        try:
            # A pre-publication tools/list exhausts its zero wait budget while
            # the controlled lookup remains unfinished for a later request.
            stdin.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
            assert (await output.next_frame())["id"] == 1
            assert len(pending) == 1
            inherited = pending[0]
            assert not inherited.done(), "the first request cancelled its lookup"

            # Only the inherited result is controlled. Any fresh lookup uses
            # the real resolver and the PID mapping published below.
            monkeypatch.setattr(loop, "run_in_executor", real_submit)
            monkeypatch.setattr(stub_mod, "_CALLER_REPAIR_TIMEOUT_SECS", 60)
            if completion == "before-request":
                inherited.set_result(stale_caller)

            await asyncio.to_thread(session_pid_sig.publish_session_pid, os.getppid(), session_key)
            assert (
                await asyncio.to_thread(session_pid_sig.read_session_pid_txt, os.getppid(), tmp_path)
                == session_key
            )

            inherited_awaited = asyncio.Event()
            if completion == "during-request":
                real_shield = asyncio.shield

                def observe_inherited_wait(awaitable):
                    shielded = real_shield(awaitable)
                    if awaitable is inherited:
                        inherited_awaited.set()
                    return shielded

                monkeypatch.setattr(asyncio, "shield", observe_inherited_wait)

            request_task = asyncio.create_task(_ledger_request(stdin, output, 2))
            if completion == "during-request":
                # The request has reached its wait on the old lookup, after
                # publication. Release the stale miss at that exact point.
                await asyncio.wait_for(inherited_awaited.wait(), timeout=5)
                assert not inherited.done()
                assert not any(frame.get("method") == "tools/call" for frame in peer.frames)
                inherited.set_result(stale_caller)
            await asyncio.wait_for(request_task, timeout=5)
            wire = list(peer.frames)
        finally:
            for future in pending:
                future.cancel()
            if request_task is not None:
                if not request_task.done():
                    request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)

    # No parent claim or kernel-peer fallback may repair the captured stream.
    # There is exactly one ledger call: success on a later retry cannot pass.
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: None)
    monkeypatch.setattr(_FakePool, "release_exclusive", AsyncMock(return_value=None), raising=False)
    backend, _audit = await _run(wire, monkeypatch)
    caller = backend.callers[-1]
    order = [
        frame.get("type", frame.get("method"))
        for frame in wire
        if frame.get("type") == "recaller" or frame.get("method") == "tools/call"
    ]
    assert (
        caller is not None and caller.session_key == session_key
    ), (
        f"first ledger request after PID publication reached broker with caller={caller!r}; "
        f"inherited empty lookup completed {completion}; wire order={order}"
    )
    assert order == ["recaller", "tools/call"]


@pytest.mark.asyncio
async def test_terminal_bridge_closes_pending_caller_lookup(monkeypatch, tmp_path) -> None:
    """A timed-out lookup is retained while live and cancelled on terminal EOF."""
    async with _initialized_stub(monkeypatch, tmp_path) as (peer, output, stdin):
        pending = _hold_caller_lookups(monkeypatch)
        await _ledger_request(stdin, output, 1)
        assert len(pending) == 1
        assert not pending[0].done()
    assert pending[0].cancelled()
    assert not any(frame.get("type") == "recaller" for frame in peer.frames)

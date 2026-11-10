"""A failed Register must release its socket and its daemon-side registrations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.mcp_gateway import gatewayd, stub, stub_link, transport
from kiro_crew.mcp_gateway.daemon import connection, identity, wire
from kiro_crew.mcp_gateway.pool import BackendPool
from kiro_crew.testing.ids import UNALLOCATABLE_PID

pytestmark = pytest.mark.xdist_group("mcp_gateway")


class _Writer:
    def __init__(
        self, *, fail_write: bool = False, block_drain: bool = False, abort_raises: bool = False
    ) -> None:
        self.transport = self
        self.fail_write = fail_write
        self.block_drain = block_drain
        self.abort_raises = abort_raises
        self.draining = asyncio.Event()
        self.aborts = 0
        self.closes = 0
        self.waits = 0

    def write(self, data: bytes) -> None:
        if self.fail_write:
            raise BrokenPipeError("injected registered write failure")

    async def drain(self) -> None:
        if self.block_drain:
            self.draining.set()
            await asyncio.Event().wait()

    def abort(self) -> None:
        self.aborts += 1
        if self.abort_raises:
            raise OSError("injected abort failure")

    def close(self) -> None:
        self.closes += 1

    async def wait_closed(self) -> None:
        self.waits += 1
        raise AssertionError("handshake cleanup must not wait for a stuck pipe")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "read", "closed", "rejected", "stale"])
async def test_stub_handshake_releases_socket_on_failed_registration(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    writer = _Writer()

    async def connect(*args: Any, **kwargs: Any) -> tuple[object, _Writer]:
        return object(), writer

    async def send(*args: Any) -> None:
        if failure == "write":
            raise BrokenPipeError("injected write failure")

    async def receive(*args: Any) -> Any:
        if failure == "read":
            raise ConnectionResetError("injected read failure")
        if failure == "closed":
            return None
        if failure == "rejected":
            return {"type": "rejected", "reason": "busy"}
        return {"type": "registered", "fingerprint": "old"}

    monkeypatch.setattr(stub_link.transport, "connect", connect)
    monkeypatch.setattr(stub, "_write_frame", send)
    monkeypatch.setattr(stub, "_read_frame", receive)
    payload = {"stub_uuid": "stub-test", "stub_code_fingerprint": "new"}
    with pytest.raises(stub_link.FallbackRequestedError):
        await stub_link.handshake("unused", payload)
    assert writer.aborts + writer.closes == 1
    assert writer.waits == 0


@pytest.mark.asyncio
async def test_stub_uses_close_when_transport_has_no_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer()
    writer.transport = object()

    async def connect(*args: Any, **kwargs: Any) -> tuple[object, _Writer]:
        return object(), writer

    async def send(*args: Any) -> None:
        raise BrokenPipeError("injected write failure")

    monkeypatch.setattr(stub_link.transport, "connect", connect)
    monkeypatch.setattr(stub, "_write_frame", send)
    with pytest.raises(stub_link.FallbackRequestedError):
        await stub_link.handshake("unused", {"stub_uuid": "stub-test"})
    assert writer.aborts == writer.waits == 0
    assert writer.closes == 1


@pytest.mark.asyncio
async def test_stub_falls_back_to_close_if_abort_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer(abort_raises=True)

    async def connect(*args: Any, **kwargs: Any) -> tuple[object, _Writer]:
        return object(), writer

    async def send(*args: Any) -> None:
        raise BrokenPipeError("injected write failure")

    monkeypatch.setattr(stub_link.transport, "connect", connect)
    monkeypatch.setattr(stub, "_write_frame", send)
    with pytest.raises(stub_link.FallbackRequestedError):
        await stub_link.handshake("unused", {"stub_uuid": "stub-test"})
    assert writer.aborts == writer.closes == 1
    assert writer.waits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["send", "receive"])
async def test_stub_handshake_cancellation_releases_socket_without_waiting(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    writer = _Writer()
    entered = asyncio.Event()

    async def connect(*args: Any, **kwargs: Any) -> tuple[object, _Writer]:
        return object(), writer

    async def send(*args: Any) -> None:
        if phase == "send":
            entered.set()
            await asyncio.Event().wait()

    async def receive(*args: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stub_link.transport, "connect", connect)
    monkeypatch.setattr(stub, "_write_frame", send)
    monkeypatch.setattr(stub, "_read_frame", receive)
    task = asyncio.create_task(stub_link.handshake("unused", {"stub_uuid": "stub-test"}))
    await asyncio.wait_for(entered.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)
    assert writer.aborts + writer.closes == 1
    assert writer.waits == 0


@pytest.mark.asyncio
async def test_stub_handshake_hands_off_successful_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer()

    async def connect(*args: Any, **kwargs: Any) -> tuple[object, _Writer]:
        return object(), writer

    async def send(*args: Any) -> None:
        pass

    async def receive(*args: Any) -> dict[str, str]:
        return {"type": "registered", "fingerprint": "new"}

    monkeypatch.setattr(stub_link.transport, "connect", connect)
    monkeypatch.setattr(stub, "_write_frame", send)
    monkeypatch.setattr(stub, "_read_frame", receive)
    result = await stub_link.handshake(
        "unused", {"stub_uuid": "stub-test", "stub_code_fingerprint": "new"}
    )
    assert result[1] is writer
    assert writer.aborts == writer.closes == writer.waits == 0


@pytest.mark.asyncio
async def test_real_stream_writer_abort_releases_server_side_pipe(short_sock_dir) -> None:
    """The concrete transport abort also closes a real Windows named pipe."""
    accepted = asyncio.Event()
    closed = asyncio.Event()

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        try:
            await reader.read()
        except ConnectionError:
            pass  # Windows may report reset rather than EOF after abort().
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            closed.set()

    sock = short_sock_dir / "register-abort.sock"
    transport.prepare_dir(sock)
    server = await transport.serve(sock, on_connect, limit=65536)
    writer: asyncio.StreamWriter | None = None
    try:
        _reader, writer = await asyncio.wait_for(transport.connect(str(sock)), timeout=30)
        await asyncio.wait_for(accepted.wait(), timeout=30)
        writer.transport.abort()
        await asyncio.wait_for(closed.wait(), timeout=30)
    finally:
        if writer is not None:
            writer.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "audit", "hot_key", "pool_get", "cancel", "normal"])
async def test_daemon_unregisters_after_post_identity_exit(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(identity, "_CONN_INDEX", {})
    monkeypatch.setattr(wire, "_STUB_PROBES", set())
    writer = _Writer(fail_write=failure == "write", block_drain=failure == "cancel")
    register = {"stub_uuid": "stub-test"}
    pool_key = SimpleNamespace(stable_hash=lambda: "a" * 64, human_readable=lambda: "test")
    conn = identity._StubConn("stub-test", [UNALLOCATABLE_PID], "test", None)
    teardown_calls: list[object] = []
    original_teardown = connection._teardown_connection

    class Pool(BackendPool):
        def __init__(self) -> None:
            super().__init__(max_backends=1)
            self.released: list[str] = []
            self.reaped: list[object] = []

        async def release_exclusive(self, stub_uuid: str) -> Any:
            self.released.append(stub_uuid)
            return await super().release_exclusive(stub_uuid)

        def spawn_shutdown(self, backend: Any) -> None:
            self.reaped.append(backend)

        async def get(self, key: Any) -> Any:
            if failure == "pool_get":
                raise RuntimeError("injected pool lookup failure")
            return await super().get(key)

    class Reader:
        async def readuntil(self, sep: bytes) -> bytes:
            raise asyncio.IncompleteReadError(b"", None)

    async def first_frame(reader: Any) -> dict[str, str]:
        return register

    async def resolve(*args: Any) -> identity._StubConn:
        identity._conn_index_add(conn)
        return conn

    async def teardown(*args: Any) -> None:
        teardown_calls.append(args[0])
        await original_teardown(*args)

    def audit(*args: Any) -> None:
        if failure == "audit":
            raise RuntimeError("injected audit failure")

    class HotKeys:
        def record(self, frame: dict[str, str]) -> None:
            if failure == "hot_key":
                raise RuntimeError("injected hot-key failure")

        def record_outcome(self, *, hit: bool) -> None:
            pass

    monkeypatch.setattr(connection, "_peer_admitted", lambda *args: True)
    monkeypatch.setattr(connection, "_read_first_frame", first_frame)
    monkeypatch.setattr(connection, "_record_session_mount", lambda *args: None)
    monkeypatch.setattr(connection.PoolKey, "from_register", lambda *args: pool_key)
    monkeypatch.setattr(connection, "_resolve_register_identity", resolve)
    monkeypatch.setattr(connection, "code_fingerprint", lambda: "fingerprint")
    monkeypatch.setattr(gatewayd, "_audit_peer_allowed", audit)
    monkeypatch.setattr(connection, "_teardown_connection", teardown)
    pool = Pool()
    predecessor = SimpleNamespace(exclusive_token="")

    async def spawn_predecessor() -> Any:
        return predecessor

    # Another connection with the same UUID may own a private backend.
    if failure != "normal":
        await pool.acquire_exclusive(pool_key, "stub-test", spawn_predecessor)
    coro = connection._handle_connection(
        Reader(),
        writer,
        pool,
        object(),
        Path("unused"),
        hot_keys=HotKeys() if failure in ("hot_key", "pool_get") else None,
    )
    if failure == "cancel":
        task = asyncio.create_task(coro)
        await asyncio.wait_for(writer.draining.wait(), timeout=30)
        # Prove the probe exists before cancellation exercises its removal.
        assert len(wire._STUB_PROBES) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
    elif failure == "normal":
        await asyncio.wait_for(coro, timeout=30)
        assert writer.aborts == writer.closes == 0
    else:
        with pytest.raises((BrokenPipeError, RuntimeError)):
            await asyncio.wait_for(coro, timeout=30)
    assert not identity._CONN_INDEX
    assert not wire._STUB_PROBES
    if failure == "normal":
        assert teardown_calls == [conn]
        assert pool.released == ["stub-test"]
    else:
        # Registration has not acquired a backend and must not release one.
        assert pool.released == []
        assert pool.reaped == []
        assert await pool.release_exclusive("stub-test") is predecessor
        assert teardown_calls == []

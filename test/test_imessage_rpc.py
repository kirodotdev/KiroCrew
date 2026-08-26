"""Tests for kiro_crew.imessage.rpc (newline-framed JSON-RPC 2.0 over stdio).

No real child process is spawned: ``asyncio.create_subprocess_exec`` is replaced
with a fake whose stdout is a real ``StreamReader`` the test feeds, so the framing
is exercised end to end while staying deterministic and Mac-free.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kiro_crew.imessage import rpc
from kiro_crew.imessage.rpc import (
    STDOUT_LINE_LIMIT,
    JsonRpcPeer,
    RpcError,
    RpcTransportError,
)


class FakeStdin:
    """Records the lines the peer writes; never blocks."""

    def __init__(self, proc: "FakeProc") -> None:
        self._proc = proc
        self.lines: list[str] = []
        self.closed = False
        self.fail = False
        #: When True the child ignores the stdin-EOF exit contract, which is
        #: what forces ``close`` down its kill-escalation path.
        self.ignore_eof = False

    def write(self, data: bytes) -> None:
        if self.fail:
            raise ConnectionResetError("pipe gone")
        self.lines.append(data.decode())

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True
        # A real ``imsg rpc`` exits cleanly when stdin closes; model that, so
        # the tests do not each pay the exit grace period.
        if not self.ignore_eof:
            self._proc.exit(0)


class FakeProc:
    """Minimal asyncio.subprocess.Process stand-in with feedable streams.

    The readers are created on FIRST USE rather than in ``__init__``, and that is
    load-bearing. ``asyncio.StreamReader()`` binds to whatever
    ``get_event_loop()`` answers at CONSTRUCTION time, and this class is built by
    a SYNC fixture -- which runs before the loop pytest-asyncio gives the test.
    Bound to that earlier loop, ``feed_data`` sets its waiters there and the
    peer's reader task, awaiting on the test's loop, is never woken: every
    ``call`` then fails its 30s timeout while the request itself is written
    correctly, so the symptom points at the peer rather than at the fixture.
    Whether the two loops coincide depends on the platform's default policy and
    on fixture ordering, which is why this was green in CI and red on macOS.
    First use is always inside the running loop -- ``peer.start()`` reads
    ``stdout``, and ``feed`` is only ever called from an async test.
    """

    def __init__(self) -> None:
        self.stdin = FakeStdin(self)
        self._stdout: asyncio.StreamReader | None = None
        self._stderr: asyncio.StreamReader | None = None
        self.returncode: int | None = None
        self.killed = False

    @property
    def stdout(self) -> asyncio.StreamReader:
        if self._stdout is None:
            self._stdout = asyncio.StreamReader(limit=STDOUT_LINE_LIMIT)
        return self._stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        if self._stderr is None:
            self._stderr = asyncio.StreamReader()
        return self._stderr

    def feed(self, frame: dict[str, Any]) -> None:
        self.stdout.feed_data((json.dumps(frame) + "\n").encode())

    def feed_raw(self, text: str) -> None:
        self.stdout.feed_data(text.encode())

    def exit(self, code: int = 0) -> None:
        if self.returncode is not None:
            return
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.stdin.ignore_eof = False
        self.exit(-9)


@pytest.fixture
def proc(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    fake = FakeProc()

    async def _spawn(*_args: object, **_kwargs: object) -> FakeProc:
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    return fake


async def _started(proc: FakeProc) -> JsonRpcPeer:
    peer = JsonRpcPeer(["imsg", "rpc"])
    await peer.start()
    return peer


class TestFraming:
    @pytest.mark.asyncio
    async def test_a_request_is_one_compact_json_line(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        task = asyncio.create_task(peer.call("status"))
        await _until(lambda: bool(proc.stdin.lines))
        line = proc.stdin.lines[0]
        assert line.endswith("\n")
        assert "\n" not in line[:-1]
        sent = json.loads(line)
        assert sent["jsonrpc"] == "2.0"
        assert sent["method"] == "status"
        # The bridge rejects a null/array params, so an empty params is OMITTED.
        assert "params" not in sent
        proc.feed({"jsonrpc": "2.0", "id": sent["id"], "result": {"ok": True}})
        assert await task == {"ok": True}
        await peer.close()

    @pytest.mark.asyncio
    async def test_params_are_sent_when_present(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        task = asyncio.create_task(peer.call("send", {"to": "+1", "text": "hi"}))
        await _until(lambda: bool(proc.stdin.lines))
        sent = json.loads(proc.stdin.lines[0])
        assert sent["params"] == {"to": "+1", "text": "hi"}
        proc.feed({"jsonrpc": "2.0", "id": sent["id"], "result": {"ok": True}})
        await task
        await peer.close()

    @pytest.mark.asyncio
    async def test_responses_are_matched_by_id_out_of_order(self, proc: FakeProc) -> None:
        # The bridge runs up to four concurrent reads and documents that they
        # may complete out of order, so id correlation is load-bearing.
        peer = await _started(proc)
        first = asyncio.create_task(peer.call("a"))
        second = asyncio.create_task(peer.call("b"))
        await _until(lambda: len(proc.stdin.lines) == 2)
        ids = [json.loads(line)["id"] for line in proc.stdin.lines]
        proc.feed({"jsonrpc": "2.0", "id": ids[1], "result": {"which": "b"}})
        proc.feed({"jsonrpc": "2.0", "id": ids[0], "result": {"which": "a"}})
        assert await first == {"which": "a"}
        assert await second == {"which": "b"}
        await peer.close()

    @pytest.mark.asyncio
    async def test_a_string_id_response_still_resolves(self, proc: FakeProc) -> None:
        # The spec allows a string id; the peer sends ints but must not strand a
        # pending call if the peer echoes it back as a string.
        peer = await _started(proc)
        task = asyncio.create_task(peer.call("status"))
        await _until(lambda: bool(proc.stdin.lines))
        sent_id = json.loads(proc.stdin.lines[0])["id"]
        proc.feed({"jsonrpc": "2.0", "id": str(sent_id), "result": {"ok": True}})
        assert await task == {"ok": True}
        await peer.close()


class TestNotifications:
    @pytest.mark.asyncio
    async def test_a_notification_reaches_the_handler(self, proc: FakeProc) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            seen.append((method, params))

        peer = JsonRpcPeer(["imsg", "rpc"], on_notification=handler)
        await peer.start()
        proc.feed({"jsonrpc": "2.0", "method": "message", "params": {"subscription": 1}})
        await _until(lambda: bool(seen))
        assert seen == [("message", {"subscription": 1})]
        await peer.close()

    @pytest.mark.asyncio
    async def test_a_handler_may_call_back_through_the_same_peer(self, proc: FakeProc) -> None:
        """The inbound path always does this, so it must not deadlock.

        Answering a message sends one, so the notification handler issues a call
        on the very peer that delivered the notification. While notifications
        were awaited inline on the reader, the reader sat inside the handler and
        could never read the response it was itself waiting for: the call died on
        timeout and the reply was silently lost. Dispatching notifications on a
        separate task is what makes this terminate.
        """
        got: dict[str, Any] = {}

        async def handler(_method: str, _params: dict[str, Any]) -> None:
            reply = await peer.call("send", {"to": "+1", "text": "hi"}, timeout=5.0)
            got["guid"] = reply.get("guid")

        peer = JsonRpcPeer(["imsg", "rpc"], on_notification=handler)
        await peer.start()
        proc.feed({"jsonrpc": "2.0", "method": "message", "params": {"subscription": 1}})

        # Play the child: answer the call the handler makes from inside dispatch.
        await _until(lambda: any('"send"' in line for line in proc.stdin.lines))
        request = json.loads(next(line for line in proc.stdin.lines if '"send"' in line))
        proc.feed({"jsonrpc": "2.0", "id": request["id"], "result": {"guid": "G-OK"}})

        await _until(lambda: "guid" in got)
        assert got["guid"] == "G-OK"
        await peer.close()

    @pytest.mark.asyncio
    async def test_notifications_do_not_block_each_other(self, proc: FakeProc) -> None:
        # Ordering is deliberately NOT a property of this layer. A task per
        # notification with a tracked set is what every other channel does
        # (webex/client.py's `_handler_tasks`); per-conversation serialization is
        # the per-session semaphore in `sessions.get_or_create`, and messages for
        # different conversations have no ordering relationship worth keeping.
        #
        # What IS required is that a slow handler cannot stall the others -- the
        # property a single serialized worker would have broken.
        release = asyncio.Event()
        started: list[int] = []
        finished: list[int] = []

        async def handler(_method: str, params: dict[str, Any]) -> None:
            rowid = int(params["rowid"])
            started.append(rowid)
            if rowid == 1:
                await release.wait()
            finished.append(rowid)

        peer = JsonRpcPeer(["imsg", "rpc"], on_notification=handler)
        await peer.start()
        for rowid in (1, 2, 3):
            proc.feed({"jsonrpc": "2.0", "method": "message", "params": {"rowid": rowid}})
        # 2 and 3 finish while 1 is still parked, which a serialized worker could
        # not do.
        await _until(lambda: finished == [2, 3])
        assert started[0] == 1
        release.set()
        await _until(lambda: 1 in finished)
        await peer.close()

    @pytest.mark.asyncio
    async def test_a_raising_handler_does_not_kill_inbound_delivery(self, proc: FakeProc) -> None:
        seen: list[str] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            seen.append(method)
            if len(seen) == 1:
                raise RuntimeError("boom")

        peer = JsonRpcPeer(["imsg", "rpc"], on_notification=handler)
        await peer.start()
        proc.feed({"jsonrpc": "2.0", "method": "one"})
        proc.feed({"jsonrpc": "2.0", "method": "two"})
        await _until(lambda: len(seen) == 2)
        assert seen == ["one", "two"]
        await peer.close()


class TestReaderResilience:
    @pytest.mark.asyncio
    async def test_an_unparseable_line_is_dropped_not_fatal(self, proc: FakeProc) -> None:
        seen: list[str] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            seen.append(method)

        peer = JsonRpcPeer(["imsg", "rpc"], on_notification=handler)
        await peer.start()
        proc.feed_raw("not json at all\n")
        proc.feed({"jsonrpc": "2.0", "method": "after"})
        await _until(lambda: seen == ["after"])
        await peer.close()

    @pytest.mark.asyncio
    async def test_a_non_object_frame_is_ignored(self, proc: FakeProc) -> None:
        seen: list[str] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            seen.append(method)

        peer = JsonRpcPeer(["imsg", "rpc"], on_notification=handler)
        await peer.start()
        proc.feed_raw("[1,2,3]\n")
        proc.feed({"jsonrpc": "2.0", "method": "after"})
        await _until(lambda: seen == ["after"])
        await peer.close()

    @pytest.mark.asyncio
    async def test_the_stdout_limit_is_far_above_the_asyncio_default(self) -> None:
        # A single inbound line can carry a whole message payload; asyncio's
        # 64 KiB default would raise inside the reader and silence the channel.
        assert STDOUT_LINE_LIMIT >= 1024 * 1024


class TestErrors:
    @pytest.mark.asyncio
    async def test_an_error_response_raises_rpcerror_with_its_code(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        task = asyncio.create_task(peer.call("watch.subscribe"))
        await _until(lambda: bool(proc.stdin.lines))
        sent_id = json.loads(proc.stdin.lines[0])["id"]
        proc.feed(
            {
                "jsonrpc": "2.0",
                "id": sent_id,
                "error": {"code": -32002, "message": "database unavailable"},
            }
        )
        with pytest.raises(RpcError) as excinfo:
            await task
        assert excinfo.value.code == -32002
        assert "database unavailable" in excinfo.value.message
        await peer.close()

    @pytest.mark.asyncio
    async def test_calling_before_start_is_a_transport_error(self) -> None:
        peer = JsonRpcPeer(["imsg", "rpc"])
        with pytest.raises(RpcTransportError):
            await peer.call("status")

    @pytest.mark.asyncio
    async def test_a_write_failure_does_not_leak_the_pending_call(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        proc.stdin.fail = True
        with pytest.raises(RpcTransportError):
            await peer.call("status")
        # A leaked future would strand the id forever and leak memory per call.
        assert peer._pending == {}
        await peer.close()

    @pytest.mark.asyncio
    async def test_a_timeout_fails_only_that_call(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        with pytest.raises(RpcTransportError):
            await peer.call("status", timeout=0.01)
        assert peer._pending == {}
        assert peer.alive
        await peer.close()

    @pytest.mark.asyncio
    async def test_child_exit_fails_every_in_flight_call(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        task = asyncio.create_task(peer.call("status", timeout=5))
        await _until(lambda: bool(proc.stdin.lines))
        proc.exit(1)
        with pytest.raises(RpcTransportError):
            await task
        await peer.close()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_shuts_stdin_which_is_the_documented_clean_exit(
        self, proc: FakeProc
    ) -> None:
        peer = await _started(proc)
        await peer.close()
        assert proc.stdin.closed
        assert not proc.killed
        assert not peer.alive

    @pytest.mark.asyncio
    async def test_a_child_that_ignores_eof_is_killed_rather_than_leaked(
        self, proc: FakeProc, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rpc, "EXIT_GRACE_S", 0)
        proc.stdin.ignore_eof = True
        peer = await _started(proc)
        await peer.close()
        assert proc.killed
        assert not peer.alive

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, proc: FakeProc) -> None:
        peer = await _started(proc)
        await peer.start()
        assert peer.alive
        await peer.close()

    @pytest.mark.asyncio
    async def test_close_without_start_is_a_no_op(self) -> None:
        await JsonRpcPeer(["imsg", "rpc"]).close()


async def _until(predicate: object, timeout: float = 2.0) -> None:
    """Poll for a condition instead of sleeping a guessed interval."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met within the deadline")

"""L1 turn-lock regression tests (Mesh _bg readuntil race).

The shared `_bg` ACP session is streamed by ~8 callers through ONE process's
single stdout StreamReader. asyncio's StreamReader permits exactly one waiting
reader; when a turn dies mid-readline and the next caller starts its own read on
the same reader, asyncio raises:

    RuntimeError: readuntil() called while another coroutine is already
    waiting for incoming data

L1 serializes whole turns with an asyncio.Lock inside AcpClient so two
coroutines can never read the same stdout concurrently.

`test_concurrent_turns_serialize` is the RED-FIRST test: it FAILS on current
(unmodified) client.py because the second turn's readline collides with the
first's parked read, and PASSES once L1 lands.

These use a REAL asyncio.StreamReader (not an AsyncMock) — only the real
StreamReader enforces the single-waiter rule that produces the race.

`test_aclosing_releases_lock_deterministically` covers the finalization edge:
the lock is held across _prompt_loop and released in its finally, but a consumer
that early-returns leaves the async-gen suspended (finally runs at deferred
finalization, not at the return). send_message_stream wraps the loop in
aclosing() for deterministic release; that test asserts it.
"""

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.client import AcpClient


def _client_with_real_reader(tmp_path):
    """An AcpClient whose _process.stdout is a real, never-fed StreamReader.

    With no data and no EOF, readline() parks — so turn A holds the reader's
    single waiter slot and turn B's readline collides (pre-L1) or blocks on the
    turn lock (post-L1).
    """
    client = AcpClient(work_dir=tmp_path)
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None  # _is_process_alive() -> True, so the loop reads
    client._process = proc
    return client, reader


@pytest.mark.asyncio
async def test_concurrent_turns_serialize(tmp_path):
    """Two concurrent _prompt_loop turns on one client must not raise the
    readuntil race. RED on current code, GREEN after L1."""
    client, _reader = _client_with_real_reader(tmp_path)

    async def drive(req_id):
        # Short per-turn deadline so an un-fed reader unblocks quickly.
        async for _action, _msg in client._prompt_loop(req_id, timeout=1.0):
            pass

    a = asyncio.create_task(drive(1))
    await asyncio.sleep(0.1)  # let turn A reach + park on readline
    b = asyncio.create_task(drive(2))

    results = await asyncio.gather(a, b, return_exceptions=True)

    readuntil = [
        r for r in results
        if isinstance(r, RuntimeError) and "readuntil" in str(r)
    ]
    assert not readuntil, (
        "concurrent _bg reads raced — L1 turn-lock missing or ineffective: "
        f"{results}"
    )


@pytest.mark.asyncio
async def test_turn_releases_on_normal_completion(tmp_path):
    """A turn that ends at its deadline must let a SUBSEQUENT turn run — i.e.
    the lock (post-L1) is released, no deadlock. Sequential, not concurrent."""
    client, _reader = _client_with_real_reader(tmp_path)

    async def drive(req_id):
        async for _action, _msg in client._prompt_loop(req_id, timeout=0.3):
            pass

    await drive(1)
    # If L1 held the lock past turn 1, this second turn would hang; bound it.
    await asyncio.wait_for(drive(2), timeout=5.0)


@pytest.mark.asyncio
async def test_aclosing_releases_lock_deterministically(tmp_path):
    """Document the _turn_lock finalization behavior and verify the deterministic
    hot path.

    A consumer that `return`s on "complete" without exhausting _prompt_loop
    leaves the async-gen SUSPENDED at the yield, holding _turn_lock. CPython
    runs the generator's finally via a DEFERRED scheduled athrow (next loop
    tick), not at the consumer's return — so on a bare early-return the lock is
    released promptly on a live loop, but NOT synchronously at the return point.

    send_message_stream avoids relying on that by wrapping the loop in
    `aclosing(...)`, which runs aclose() (and thus the finally) synchronously on
    block exit. This test asserts that deterministic release: iterate one event,
    leave the `async with aclosing(...)` block, and the lock is free with no
    pending finalization.
    """
    client, reader = _client_with_real_reader(tmp_path)

    # Feed one frame so the loop yields once before we exit the block.
    reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n')

    from contextlib import aclosing

    async with aclosing(client._prompt_loop(1, timeout=5.0)) as loop:
        async for _action, _msg in loop:
            break  # early exit, as a consumer does on "complete"
    # aclosing ran the finally synchronously on block exit — lock is free NOW.
    assert not client._turn_lock.locked(), (
        "_turn_lock still held after aclosing() exit — aclose() did not run the "
        "_prompt_loop finally (deterministic release broken)"
    )

    # A subsequent turn must acquire promptly (bounded).
    async def drive(req_id):
        async for _a, _m in client._prompt_loop(req_id, timeout=0.3):
            pass

    await asyncio.wait_for(drive(2), timeout=5.0)


@pytest.mark.asyncio
async def test_effort_rpc_waits_for_turn_admitted_during_overlay_write(tmp_path):
    """A turn admitted during the effort overlay write keeps sole stdout ownership."""
    import json
    import threading
    from unittest.mock import AsyncMock

    from kiro_crew.providers.acp import AcpProvider

    client, reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client._model = "gpt-5.6-sol"
    client.ensure_ready = AsyncMock()

    prompt_sent = asyncio.Event()
    command_sent = asyncio.Event()
    requests: list[dict] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            request = json.loads(raw)
            requests.append(request)
            if request["method"] == "session/prompt":
                prompt_sent.set()
            elif request["method"] == "_kiro.dev/commands/execute":
                command_sent.set()
                reader.feed_data(
                    (
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "result": {"text": "effort changed"},
                            }
                        )
                        + "\n"
                    ).encode()
                )

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client
    provider._effort_per_model = {}
    provider._effort_defaults = None
    provider.supports_effort = lambda: True

    overlay_started = threading.Event()
    release_overlay = threading.Event()

    def _blocked_overlay(*, timeout: float) -> bool:
        overlay_started.set()
        return release_overlay.wait(timeout)

    provider._apply_effort_overlay = _blocked_overlay
    effort = asyncio.create_task(provider.change_effort("high"))
    assert await asyncio.to_thread(overlay_started.wait, 1.0)

    async def _drive_turn():
        return [event async for event in client.stream_events("hello", timeout=2.0)]

    turn = asyncio.create_task(_drive_turn())
    await asyncio.wait_for(prompt_sent.wait(), timeout=1.0)
    for _ in range(100):
        if client._turn_lock.locked():
            break
        await asyncio.sleep(0)
    assert client._turn_lock.locked()

    release_overlay.set()
    await asyncio.sleep(0.05)
    assert not command_sent.is_set(), "effort command bypassed the active turn's stdout lock"

    prompt_request = next(r for r in requests if r["method"] == "session/prompt")
    reader.feed_data(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_request["id"],
                    "result": {"stopReason": "end_turn"},
                }
            )
            + "\n"
        ).encode()
    )

    turn_events, effort_changed = await asyncio.wait_for(
        asyncio.gather(turn, effort), timeout=2.0
    )
    assert turn_events[-1].kind == "complete"
    assert turn_events[-1].stop_reason == "end_turn"
    assert effort_changed is True
    assert command_sent.is_set()


@pytest.mark.asyncio
async def test_prompt_owns_stdout_before_write_against_effort_config(tmp_path):
    """An effort response reader cannot divert a prompt's streamed update."""
    import json
    from unittest.mock import AsyncMock

    client, reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()

    prompt_written = asyncio.Event()
    release_prompt_send = asyncio.Event()
    config_written = asyncio.Event()
    requests: list[dict] = []

    original_send_prompt = client._send_prompt

    async def _paused_send_prompt(message: str) -> int:
        req_id = await original_send_prompt(message)
        prompt_written.set()
        await release_prompt_send.wait()
        return req_id

    client._send_prompt = _paused_send_prompt

    class _Stdin:
        def write(self, raw: bytes) -> None:
            request = json.loads(raw)
            requests.append(request)
            if request["method"] == "session/set_config_option":
                config_written.set()

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()

    async def _drive_prompt():
        return [event async for event in client.stream_events("hello", timeout=2.0)]

    prompt = asyncio.create_task(_drive_prompt())
    await asyncio.wait_for(prompt_written.wait(), timeout=1.0)
    effort = asyncio.create_task(client.set_config_option("effort", "high"))

    try:
        await asyncio.wait_for(config_written.wait(), timeout=0.05)
    except asyncio.TimeoutError:
        # Fixed path: the prompt owns the lock before its write, so finish it
        # before the effort request can be written.
        release_prompt_send.set()
        prompt_request = next(r for r in requests if r["method"] == "session/prompt")
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "session-1",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "kept"},
                            },
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": prompt_request["id"],
                        "result": {"stopReason": "end_turn"},
                    }
                )
                + "\n"
            ).encode()
        )
        events = await asyncio.wait_for(prompt, timeout=1.0)
        await asyncio.wait_for(config_written.wait(), timeout=1.0)
        config_request = next(
            r for r in requests if r["method"] == "session/set_config_option"
        )
        reader.feed_data(
            (
                json.dumps(
                    {"jsonrpc": "2.0", "id": config_request["id"], "result": {}}
                )
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(effort, timeout=1.0)
    else:
        # Old path: the effort request enters after the prompt write but before
        # the prompt reader owns the lock, so it consumes the prompt update.
        prompt_request = next(r for r in requests if r["method"] == "session/prompt")
        config_request = next(
            r for r in requests if r["method"] == "session/set_config_option"
        )
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "session-1",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "kept"},
                            },
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {"jsonrpc": "2.0", "id": config_request["id"], "result": {}}
                )
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(effort, timeout=1.0)
        release_prompt_send.set()
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": prompt_request["id"],
                        "result": {"stopReason": "end_turn"},
                    }
                )
                + "\n"
            ).encode()
        )
        events = await asyncio.wait_for(prompt, timeout=1.0)

    assert [event.text for event in events if event.kind == "text_chunk"] == ["kept"]


@pytest.mark.asyncio
async def test_command_result_waits_for_active_prompt_reader(tmp_path):
    """Structured commands cannot write while a prompt owns stdout."""
    from unittest.mock import AsyncMock

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()
    writes: list[bytes] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            writes.append(raw)

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()

    async def _drive_prompt():
        async for _action, _message in client._prompt_loop(1, timeout=2.0):
            pass

    prompt = asyncio.create_task(_drive_prompt())
    for _ in range(100):
        if client._turn_lock.locked():
            break
        await asyncio.sleep(0)
    assert client._turn_lock.locked()

    command = asyncio.create_task(client.command_result("/tools"))
    await asyncio.sleep(0.05)
    assert writes == []

    command.cancel()
    prompt.cancel()
    await asyncio.gather(command, prompt, return_exceptions=True)


@pytest.mark.asyncio
async def test_effort_turn_lock_timeout_writes_nothing(tmp_path, monkeypatch):
    """A bounded effort acquire writes nothing and keeps the cold-start default."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp.client import TurnLockBusy
    from kiro_crew.providers import acp as provider_module
    from kiro_crew.providers.acp import AcpProvider

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client._model = "gpt-5.6-sol"
    client._acp_backend = ""
    client.ensure_ready = AsyncMock()
    writes: list[bytes] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            writes.append(raw)

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()
    await client._turn_lock.acquire()
    try:
        with pytest.raises(TurnLockBusy):
            await asyncio.wait_for(
                client.set_config_option("effort", "high", lock_timeout=0.02),
                timeout=0.5,
            )
    finally:
        client._turn_lock.release()

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client
    provider._effort_per_model = {client._model: "high"}
    provider._effort_defaults = None
    provider.supports_effort = lambda: True
    provider._resolve_effort = lambda: "medium"
    persisted: list[str] = []

    def _record_overlay(*, timeout: float) -> bool:
        persisted.append(provider._effort_per_model.get(client._model, "medium"))
        return True

    provider._apply_effort_overlay = _record_overlay
    monkeypatch.setattr(provider_module, "EFFORT_PUSH_TURN_LOCK_TIMEOUT_SECS", 0.02)

    await client._turn_lock.acquire()
    try:
        with pytest.raises(TurnLockBusy):
            await asyncio.wait_for(provider.clear_effort(), timeout=0.5)
    finally:
        client._turn_lock.release()

    assert writes == []
    assert provider._effort_per_model == {}
    assert persisted == ["medium"]


@pytest.mark.asyncio
async def test_prompt_write_failure_releases_turn_ownership(tmp_path):
    """A failed prompt write releases the lock and completes turn state."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp.client import AcpProcessDied

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()

    class _BrokenStdin:
        def write(self, _raw: bytes) -> None:
            raise BrokenPipeError("closed")

        async def drain(self) -> None:
            return None

    client._process.stdin = _BrokenStdin()

    with pytest.raises(AcpProcessDied):
        await asyncio.wait_for(
            _collect_events(client.stream_events("hello", timeout=1.0)),
            timeout=1.0,
        )

    assert not client._turn_lock.locked()
    assert client._turn_done.is_set()


@pytest.mark.asyncio
async def test_prompt_cancelled_while_waiting_does_not_write(tmp_path):
    """Task cancellation before lock ownership preserves the idle turn state."""
    from unittest.mock import AsyncMock

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()
    writes: list[bytes] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            writes.append(raw)

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()
    await client._turn_lock.acquire()
    prompt = asyncio.create_task(
        _collect_events(client.stream_events("hello", timeout=1.0))
    )
    await asyncio.sleep(0.05)
    assert client.has_active_turn(), inspect.getfile(type(client))
    assert not client.has_unfinished_turn()
    prompt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prompt
    client._turn_lock.release()

    assert writes == []
    assert not client.has_active_turn()
    assert client._turn_done.is_set()


@pytest.mark.asyncio
async def test_cancelled_prompt_waiter_preserves_active_turn(tmp_path):
    """A cancelled waiter cannot publish completion over the active prompt."""
    import json
    from unittest.mock import AsyncMock

    client, reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()
    requests: list[dict] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            requests.append(json.loads(raw))

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()
    active = asyncio.create_task(
        _collect_events(client.stream_events("first", timeout=2.0))
    )
    for _ in range(100):
        if requests and client._turn_lock.locked():
            break
        await asyncio.sleep(0)
    assert requests
    assert client.has_active_turn(), inspect.getfile(type(client))

    waiter = asyncio.create_task(
        _collect_events(client.stream_events("second", timeout=2.0))
    )
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert client.has_active_turn(), inspect.getfile(type(client))
    assert len(requests) == 1

    request = requests[0]
    reader.feed_data(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"stopReason": "end_turn"},
                }
            )
            + "\n"
        ).encode()
    )
    events = await asyncio.wait_for(active, timeout=1.0)

    assert events[-1].kind == "complete"
    assert events[-1].stop_reason == "end_turn"
    assert not client.has_active_turn()
    assert client._turn_done.is_set()


@pytest.mark.asyncio
async def test_stop_cancels_prompt_waiting_behind_non_turn_owner(tmp_path):
    """Stop owns an admitted prompt before its deferred write reaches stdin."""
    import json
    from unittest.mock import AsyncMock

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()
    requests: list[dict] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            requests.append(json.loads(raw))

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()
    await client._turn_lock.acquire()
    prompt = asyncio.create_task(
        _collect_events(client.stream_events("hello", timeout=1.0))
    )
    for _ in range(100):
        if client.has_active_turn():
            break
        await asyncio.sleep(0)

    assert client.has_active_turn(), inspect.getfile(type(client))
    assert not client.has_unfinished_turn()
    done = asyncio.create_task(client.wait_turn_done(timeout=1.0))
    await asyncio.sleep(0)
    assert not done.done()

    await client.cancel_session()
    await asyncio.sleep(0)
    assert not done.done()
    assert [request["method"] for request in requests] == ["session/cancel"]

    client._turn_lock.release()
    events, reason = await asyncio.wait_for(
        asyncio.gather(prompt, done), timeout=1.0
    )

    assert events[-1].kind == "complete"
    assert events[-1].stop_reason == "cancelled"
    assert reason == "cancelled"
    assert [request["method"] for request in requests] == ["session/cancel"]
    assert not client.has_active_turn()
    assert not client.has_unfinished_turn()


@pytest.mark.asyncio
async def test_provider_cancel_acks_prompt_waiting_behind_non_turn_owner(tmp_path):
    """The provider reports an admitted waiting prompt as softly cancelled."""
    import json
    from unittest.mock import AsyncMock

    from kiro_crew.providers.acp import AcpProvider

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()
    requests: list[dict] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            requests.append(json.loads(raw))

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client
    provider.essential_delivery = MagicMock()

    await client._turn_lock.acquire()
    prompt = asyncio.create_task(
        _collect_events(client.stream_events("hello", timeout=1.0))
    )
    for _ in range(100):
        if client.has_active_turn():
            break
        await asyncio.sleep(0)

    cancel = asyncio.create_task(provider.cancel(wait_ack_timeout=1.0))
    await asyncio.sleep(0)
    assert not cancel.done(), inspect.getfile(type(client))
    client._turn_lock.release()

    events, outcome = await asyncio.wait_for(
        asyncio.gather(prompt, cancel), timeout=1.0
    )
    assert events[-1].stop_reason == "cancelled"
    assert outcome == "acked"
    assert [request["method"] for request in requests] == ["session/cancel"]


@pytest.mark.parametrize(
    "api",
    ["send_message", "send_message_stream", "stream_events", "stream_command"],
)
@pytest.mark.asyncio
async def test_cancelled_waiting_turn_finishes_each_prompt_api(tmp_path, api):
    """Every client prompt surface consumes the synthetic cancelled terminal."""
    import json
    from unittest.mock import AsyncMock

    client, _reader = _client_with_real_reader(tmp_path)
    client._session_id = "session-1"
    client.ensure_ready = AsyncMock()
    requests: list[dict] = []

    class _Stdin:
        def write(self, raw: bytes) -> None:
            requests.append(json.loads(raw))

        async def drain(self) -> None:
            return None

    client._process.stdin = _Stdin()
    await client._turn_lock.acquire()
    if api == "send_message":
        turn = asyncio.create_task(client.send_message("hello", timeout=1.0))
    elif api == "send_message_stream":
        turn = asyncio.create_task(
            _collect_events(client.send_message_stream("hello", timeout=1.0))
        )
    elif api == "stream_events":
        turn = asyncio.create_task(
            _collect_events(client.stream_events("hello", timeout=1.0))
        )
    else:
        turn = asyncio.create_task(
            _collect_events(client.stream_command("/help", timeout=1.0))
        )

    for _ in range(100):
        if client.has_active_turn():
            break
        await asyncio.sleep(0)
    assert client.has_active_turn(), inspect.getfile(type(client))

    await client.cancel_session()
    client._turn_lock.release()
    result = await asyncio.wait_for(turn, timeout=1.0)

    assert client._last_stop_reason == "cancelled"
    if api in ("stream_events", "stream_command"):
        assert result[-1].kind == "complete"
        assert result[-1].stop_reason == "cancelled"
    else:
        assert result in ("", [])
    assert [request["method"] for request in requests] == ["session/cancel"]


async def _collect_events(events):
    return [event async for event in events]

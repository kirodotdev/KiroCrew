"""Characterization of the ACP stdio framing: bounded writes and oversize-line drains.

Pins the paths no other test reaches: an awaitable that completes exactly as a
no-progress window closes, a best-effort notification cancelled while it waits for
the write lock, the oversize-line drain's return value and both of its
``OversizeLineUnrecoverable`` messages, the per-loop write window, the framing
constants, and that ``kiro_crew.acp.runtime`` re-exports the very same framing
objects the client defines.

The code is reached only through the ``kiro_crew.acp.client`` /
``kiro_crew.acp.runtime`` facades, and only names defined there (or attributes of
the shared ``asyncio`` module) are patched, so these tests hold unchanged before
and after the definitions move to their owner module.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime

# Upper bound for an await the test itself must unblock. It exists so a hang fails
# here by name rather than as pytest's --timeout; no passing run measures it.
_BACKSTOP = 10.0


@pytest.fixture(autouse=True)
def _selector_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the selector-loop reading, so each test means the same thing on the Windows
    shards, whose real test loop is the proactor (whose level never counts as
    progress). A test about the proactor path overrides this with its own patch."""
    monkeypatch.setattr(acp_client, "_is_proactor_loop", lambda _loop: False)


class _Transport:
    def __init__(self, level: object = 0) -> None:
        self.level = level
        self.probed = asyncio.Event()

    def get_write_buffer_size(self) -> object:
        self.probed.set()
        return self.level


class _Stdin:
    """A StreamWriter stand-in that records writes; its level never moves."""

    def __init__(self, transport: object | None = None) -> None:
        if transport is not None:
            self.transport = transport
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


def test_framing_constants():
    assert acp_client._RESPONSE_WRITE_BOUND_SECS == 5.0
    assert acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES == 4096
    assert acp_client._RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS == 900.0
    assert acp_client._STDOUT_BUFFER_LIMIT == 10 * 1024 * 1024
    assert acp_client._OVERSIZE_DRAIN_MAX_BYTES == 160 * 1024 * 1024
    assert issubclass(acp_client.OversizeLineUnrecoverable, Exception)


@pytest.mark.parametrize(
    "name",
    [
        "write_response_frame_bounded",
        "write_notification_best_effort",
        "response_write_window_secs",
        "_drain_oversize_line",
        "OversizeLineUnrecoverable",
    ],
)
def test_runtime_reexports_the_client_framing_objects(name):
    assert getattr(acp_runtime, name) is getattr(acp_client, name)


def test_runtime_reexports_the_client_write_bounds():
    assert acp_runtime._RESPONSE_WRITE_BOUND_SECS == acp_client._RESPONSE_WRITE_BOUND_SECS
    assert (
        acp_runtime._RESPONSE_WRITE_MIN_PROGRESS_BYTES
        == acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport, proactor, expected",
    [
        pytest.param(_Transport, False, 7.5, id="selector-loop-uses-the-bound"),
        pytest.param(_Transport, True, 900.0, id="proactor-loop-uses-the-elapsed-window"),
        pytest.param(None, False, 900.0, id="no-transport-is-unobservable"),
        pytest.param(lambda: _Transport(level=None), False, 900.0, id="non-int-level"),
    ],
)
async def test_response_write_window_secs(monkeypatch, transport, proactor, expected):
    seen: list[asyncio.AbstractEventLoop] = []

    def _fake_is_proactor(loop: asyncio.AbstractEventLoop) -> bool:
        seen.append(loop)
        return proactor

    monkeypatch.setattr(acp_client, "_is_proactor_loop", _fake_is_proactor)
    stdin = _Stdin(transport() if transport is not None else None)
    assert acp_client.response_write_window_secs(stdin, 7.5) == expected
    if transport is not None:
        assert seen == [asyncio.get_running_loop()]


@pytest.mark.asyncio
async def test_awaitable_completing_as_the_window_closes_counts_as_completed():
    real_wait_for = asyncio.wait_for
    timeouts: list[float] = []

    async def _window_closes_after_completion(aw, timeout):
        # The awaited work finishes, and only then does the window report a timeout.
        timeouts.append(timeout)
        try:
            await aw
        except Exception:
            pass
        raise asyncio.TimeoutError()

    async def _ok() -> int:
        return 1

    async def _boom() -> None:
        raise KeyError("k")

    stdin = _Stdin(_Transport())
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(asyncio, "wait_for", _window_closes_after_completion)
        completed = await real_wait_for(
            acp_client.await_under_no_progress_bound(_ok(), stdin, bound_secs=1),
            timeout=_BACKSTOP,
        )
        with pytest.raises(KeyError):
            await real_wait_for(
                acp_client.await_under_no_progress_bound(_boom(), stdin, bound_secs=1),
                timeout=_BACKSTOP,
            )
    assert completed is True
    assert timeouts == [1, 1]


@pytest.mark.asyncio
async def test_notification_cancelled_waiting_for_the_lock_writes_nothing():
    lock = asyncio.Lock()
    await lock.acquire()
    transport = _Transport()
    stdin = _Stdin(transport)
    task = asyncio.ensure_future(
        acp_client.write_notification_best_effort(stdin, lock, b"X", bound_secs=30)
    )
    try:
        # The level is probed once the lock acquire is in flight, right before the
        # bounded wait on it begins: that is the moment to cancel.
        await asyncio.wait_for(transport.probed.wait(), timeout=_BACKSTOP)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_BACKSTOP)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        lock.release()

    async def _until_unlocked() -> None:
        while lock.locked():
            await asyncio.sleep(0)

    await asyncio.wait_for(_until_unlocked(), timeout=_BACKSTOP)
    assert stdin.writes == []


async def _overrun(reader: asyncio.StreamReader) -> asyncio.LimitOverrunError:
    with pytest.raises(asyncio.LimitOverrunError) as caught:
        await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_BACKSTOP)
    return caught.value


@pytest.mark.asyncio
async def test_drain_discards_one_line_and_leaves_the_reader_on_the_next_frame():
    reader = asyncio.StreamReader(limit=8)
    reader.feed_data(b"0123456789ABCDEF\nNEXT\n")
    reader.feed_eof()
    exc = await _overrun(reader)
    drained = await asyncio.wait_for(
        acp_client._drain_oversize_line(reader, exc), timeout=_BACKSTOP
    )
    assert drained == 17
    assert await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_BACKSTOP) == b"NEXT\n"


@pytest.mark.asyncio
async def test_drain_refuses_a_zero_byte_prefix():
    reader = asyncio.StreamReader(limit=8)
    reader.feed_eof()
    with pytest.raises(acp_client.OversizeLineUnrecoverable) as caught:
        await asyncio.wait_for(
            acp_client._drain_oversize_line(reader, asyncio.LimitOverrunError("x", 0)),
            timeout=_BACKSTOP,
        )
    assert str(caught.value) == "stream reported a 0-byte oversize prefix"


@pytest.mark.asyncio
async def test_drain_budget_is_read_through_the_facade(monkeypatch):
    monkeypatch.setattr(acp_client, "_OVERSIZE_DRAIN_MAX_BYTES", 10)
    reader = asyncio.StreamReader(limit=4)
    reader.feed_data(b"x" * 64)
    reader.feed_eof()
    exc = await _overrun(reader)
    with pytest.raises(acp_client.OversizeLineUnrecoverable) as caught:
        await asyncio.wait_for(acp_client._drain_oversize_line(reader, exc), timeout=_BACKSTOP)
    assert str(caught.value) == "discarded 64 bytes with no frame boundary (limit 10)"

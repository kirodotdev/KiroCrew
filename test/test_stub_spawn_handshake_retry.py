from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import stub as stub_mod

_SERVER_RESULT = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "fake", "version": "1"},
}


class _CaptureWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._mc_write_lock = asyncio.Lock()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _reader_with(*frames: dict) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
    reader.feed_eof()
    return reader


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.delays: list[float] = []

    def time(self) -> float:
        return self.now

    async def wait(self, stop_event: asyncio.Event, delay: float) -> bool:
        if stop_event.is_set():
            return False
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)
        return not stop_event.is_set()


@pytest.fixture()
def clock(monkeypatch) -> _FakeClock:  # noqa: ANN001
    fake = _FakeClock()
    monkeypatch.setattr(stub_mod, "_reconnect_now", fake.time)
    monkeypatch.setattr(stub_mod, "_reconnect_wait", fake.wait)
    return fake


def _registered() -> tuple:
    return (
        _reader_with({"jsonrpc": "2.0", "id": 7, "result": dict(_SERVER_RESULT)}),
        _CaptureWriter(),
        "stub-uuid",
        {"type": "registered", "capabilities": ["poolable_ack"]},
    )


def _gateway_back_at(
    clock: _FakeClock, back_after: float | None, *, error: str = ""
):  # noqa: ANN202
    seen: list[str] = []
    start = clock.now

    async def _hs(_socket_path: str, payload: dict):
        seen.append(payload["stub_uuid"])
        if error:
            raise stub_mod.FallbackRequestedError(error)
        if back_after is None or clock.now - start < back_after:
            raise stub_mod.FallbackRequestedError("connect failed: [Errno 2] No such file")
        return _registered()

    _hs.seen = seen  # type: ignore[attr-defined]
    return _hs


@pytest.mark.asyncio
async def test_spawn_handshake_retries_a_gateway_that_is_still_starting(
    clock: _FakeClock, monkeypatch
) -> None:
    hs = _gateway_back_at(clock, back_after=4.0)
    monkeypatch.setattr(stub_mod, "handshake", hs)

    result, payload, reason, attempts = await stub_mod._spawn_handshake(
        "unused", {"stub_uuid": "first"}, asyncio.Event(), "probe:fake"
    )

    assert result is not None
    assert reason == ""
    assert attempts == len(hs.seen) > 1
    assert len(set(hs.seen)) == attempts
    assert payload["stub_uuid"] == hs.seen[-1]
    assert clock.delays[:3] == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_spawn_handshake_gives_up_once_its_budget_is_spent(
    clock: _FakeClock, monkeypatch
) -> None:
    hs = _gateway_back_at(clock, back_after=None)
    monkeypatch.setattr(stub_mod, "handshake", hs)
    started = clock.now

    result, payload, reason, attempts = await stub_mod._spawn_handshake(
        "unused", {"stub_uuid": "first"}, asyncio.Event(), "probe:fake"
    )

    assert result is None
    assert reason.startswith("connect failed")
    assert attempts == len(hs.seen) > 1
    assert payload["stub_uuid"] == hs.seen[-1]
    assert clock.now - started <= stub_mod._SPAWN_HANDSHAKE_BUDGET_SECS


@pytest.mark.asyncio
async def test_spawn_handshake_retries_a_timed_out_attempt(clock: _FakeClock, monkeypatch) -> None:
    calls: list[int] = []

    async def _hs(_socket_path: str, _payload: dict):
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.TimeoutError()
        return _registered()

    monkeypatch.setattr(stub_mod, "handshake", _hs)

    result, _payload, reason, attempts = await stub_mod._spawn_handshake(
        "unused", {"stub_uuid": "first"}, asyncio.Event(), "probe:fake"
    )

    assert result is not None
    assert (reason, attempts) == ("", 2)


@pytest.mark.parametrize(
    "refusal",
    [
        "gateway rejected: unknown target",
        "unexpected handshake reply: type='nope'",
        "unexpected handshake reply (not an object): list",
    ],
)
@pytest.mark.asyncio
async def test_spawn_handshake_does_not_retry_a_refusal(
    clock: _FakeClock, monkeypatch, refusal: str
) -> None:
    hs = _gateway_back_at(clock, back_after=0.0, error=refusal)
    monkeypatch.setattr(stub_mod, "handshake", hs)

    result, _payload, reason, attempts = await stub_mod._spawn_handshake(
        "unused", {"stub_uuid": "first"}, asyncio.Event(), "probe:fake"
    )

    assert result is None
    assert (reason, attempts) == (refusal, 1)
    assert clock.delays == []


@pytest.mark.asyncio
async def test_spawn_handshake_stops_when_the_stub_is_told_to(
    clock: _FakeClock, monkeypatch
) -> None:
    stop = asyncio.Event()
    hs = _gateway_back_at(clock, back_after=None)

    async def _hs(socket_path: str, payload: dict):
        stop.set()
        return await hs(socket_path, payload)

    monkeypatch.setattr(stub_mod, "handshake", _hs)

    result, _payload, _reason, attempts = await stub_mod._spawn_handshake(
        "unused", {"stub_uuid": "first"}, stop, "probe:fake"
    )

    assert result is None
    assert attempts == 1


def test_spawn_worst_case_stays_inside_the_existing_preflight_window() -> None:
    worst_case = stub_mod._SPAWN_HANDSHAKE_BUDGET_SECS + stub_mod._HANDSHAKE_TIMEOUT_SECS
    assert worst_case < stub_mod._SPAWN_QUEUE_SILENCE_SECS
    assert stub_mod._SPAWN_HANDSHAKE_BUDGET_SECS < stub_mod._RECONNECT_TOTAL_BUDGET_SECS


def test_fallback_record_carries_the_attempt_count(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "stub_fallback.jsonl"
    monkeypatch.setattr(stub_mod, "_fallback_log_path", lambda: log)
    args = argparse.Namespace(
        server="fake", agent="probe", channel_id="", target_command="fake-bin"
    )

    stub_mod.log_fallback("handshake_timeout", "u1", "probe:fake", args, attempts=5)
    stub_mod.log_fallback("connect failed: x", "u2", "probe:fake", args)

    first, second = (json.loads(line) for line in log.read_text().splitlines())
    assert first["attempts"] == 5
    assert "attempts" not in second

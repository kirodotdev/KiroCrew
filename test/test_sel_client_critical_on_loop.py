"""A critical audit write never runs on a thread that runs an event loop.

A critical write is synchronous network I/O: the caller refuses its action when the row is
not taken, so the thread that makes the call waits for the answer. On an event loop that
wait stalls every task, so the write is refused there and nothing is sent; the caller
denies the action. An async caller runs the write with ``asyncio.to_thread``, where it
lands. Off a loop nothing changes. The token and the sender are shared by every thread
that writes, so one mint serves concurrent writers and none sends an empty token.

The route is a fake, so every check is an ordering of calls, not a measured duration.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from kiro_crew.apps import sel_client
from kiro_crew.apps.sel_client import SelClient, SelClientError
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_APP = "loop-app"


class _Reply:
    """A route answer: a context manager with a status and a body."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status, self._body = status, body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _Route:
    """A fake gateway: one token per mint, 204 for a row posted with a token it issued."""

    def __init__(self, *, refuse_rows: int = 0) -> None:
        self._lock = threading.Lock()
        self.mints = 0
        self.rows: list[tuple[str, dict]] = []
        self._refuse_rows = refuse_rows

    def __call__(self, req: object, timeout: float) -> _Reply:
        url = req.full_url  # type: ignore[attr-defined]
        with self._lock:
            if url.endswith("/token"):
                self.mints += 1
                return _Reply(200, json.dumps({"token": f"tok-{self.mints}"}).encode())
            token = url.rsplit("token=", 1)[-1]
            if self._refuse_rows:
                self._refuse_rows -= 1
                return _Reply(401)
            if not token:
                return _Reply(401)
            self.rows.append((token, json.loads(req.data)))  # type: ignore[attr-defined]
            return _Reply(204)


def _client() -> SelClient:
    return SelClient(_APP, "http://127.0.0.1:1", "per-app-secret")


def _write(client: SelClient, operation: str = "x.y") -> None:
    client.log_api_access(operation=operation, outcome="ok", critical=True)


@pytest.fixture
def route(monkeypatch) -> _Route:
    fake = _Route()
    monkeypatch.setattr(sel_client, "loopback_urlopen", fake)
    return fake


@pytest.mark.asyncio
async def test_a_critical_write_on_a_loop_is_refused_before_any_network_or_queue_wait(
    route, monkeypatch
) -> None:
    client = _client()

    def _no_polling(_budget: float) -> None:
        raise AssertionError("the queue was polled on an event loop")

    monkeypatch.setattr(client, "_wait_for_queue", _no_polling)

    with pytest.raises(SelClientError, match="event loop"):
        _write(client)

    assert route.mints == 0 and route.rows == []


@pytest.mark.asyncio
async def test_a_critical_write_offloaded_from_a_running_loop_lands(route) -> None:
    client = _client()

    await asyncio.to_thread(_write, client, "x.offloaded")

    assert [row["kwargs"]["operation"] for _token, row in route.rows] == ["x.offloaded"]
    assert route.rows[0][0] == "tok-1"


def test_a_critical_write_off_a_loop_waits_for_the_queue_then_lands(route, monkeypatch) -> None:
    client = _client()
    waited: list[float] = []
    monkeypatch.setattr(client, "_wait_for_queue", waited.append)

    _write(client)

    assert waited == [sel_client._EXIT_DRAIN_SECS]
    assert len(route.rows) == 1


def test_concurrent_writers_share_one_token_and_none_sends_an_empty_one(route) -> None:
    client = _client()
    count = 8
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def _worker(i: int) -> None:
        try:
            barrier.wait(FALLBACK_TIMEOUT_SECS)
            _write(client, f"x.n{i}")
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(count)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(FALLBACK_TIMEOUT_SECS)
            assert not thread.is_alive(), "a writer did not finish"
    finally:
        barrier.abort()  # a worker that could not start must not leave the rest waiting

    assert errors == []
    assert route.mints == 1
    assert len(route.rows) == count
    assert {token for token, _row in route.rows} == {"tok-1"}


def test_a_refused_token_fails_the_write_instead_of_skipping_it(monkeypatch) -> None:
    """Every row is refused (401): the write raises, so the caller denies; it is never dropped."""
    fake = _Route(refuse_rows=2)
    monkeypatch.setattr(sel_client, "loopback_urlopen", fake)
    client = _client()

    with pytest.raises(SelClientError, match="401"):
        _write(client)

    assert fake.rows == [] and fake.mints == 2  # one retry with a fresh token, then the raise


def test_a_token_refused_once_is_renewed_and_the_write_lands(monkeypatch) -> None:
    fake = _Route(refuse_rows=1)
    monkeypatch.setattr(sel_client, "loopback_urlopen", fake)
    client = _client()

    _write(client)

    assert [token for token, _row in fake.rows] == ["tok-2"]

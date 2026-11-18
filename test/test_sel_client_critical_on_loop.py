"""A critical audit write from an event loop is bounded and polls nothing.

A critical write is synchronous: the caller refuses its action when the row is not taken,
so the thread that makes the call waits for the answer. When that thread runs an event
loop, the wait stalls every task on it. The write therefore skips the queue polling and
has a budget for the token exchange and the row together; a route that uses up the budget
fails the write (the caller refuses the action). Off a loop nothing changes.

The budget is read from an injected clock and the route is a fake that moves that clock,
so every check is an ordering of calls, not a measured duration.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps import sel_client
from kiro_crew.apps.sel_client import SelClient, SelClientError

_APP = "loop-app"
_BUDGET = sel_client._LOOP_CRITICAL_SECS


class _Reply:
    """A route answer: a context manager with a body."""

    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _Route:
    """A fake route. Each request records its timeout and moves the clock by its cost."""

    def __init__(self, clock, costs: list[float], *, fail_last: bool = False) -> None:
        self.timeouts: list[float] = []
        self._clock, self._costs, self._fail_last = clock, costs, fail_last

    def __call__(self, _req: object, timeout: float) -> _Reply:
        self.timeouts.append(timeout)
        self._clock.advance(self._costs[len(self.timeouts) - 1])
        if self._fail_last and len(self.timeouts) == len(self._costs):
            raise TimeoutError("the route did not answer")
        return _Reply(b'{"token": "tok"}')


def _client() -> SelClient:
    return SelClient(_APP, "http://127.0.0.1:1", "per-app-secret")


@pytest.mark.asyncio
async def test_a_critical_write_on_a_loop_does_not_poll_the_queue(
    manual_clock, monkeypatch
) -> None:
    manual_clock.install(monkeypatch, sel_client)
    client = _client()
    sent: list[float | None] = []
    monkeypatch.setattr(client, "_send", lambda _body, deadline=None: sent.append(deadline))

    def _no_polling(_budget: float) -> None:
        raise AssertionError("the queue was polled on an event loop")

    monkeypatch.setattr(client, "_wait_for_queue", _no_polling)

    client.log_api_access(operation="x.y", outcome="ok", critical=True)

    assert sent == [manual_clock.monotonic() + _BUDGET]


def test_a_critical_write_off_a_loop_waits_for_the_queue_and_has_no_deadline(monkeypatch) -> None:
    client = _client()
    sent: list[float | None] = []
    waited: list[float] = []
    monkeypatch.setattr(client, "_send", lambda _body, deadline=None: sent.append(deadline))
    monkeypatch.setattr(client, "_wait_for_queue", waited.append)

    client.log_api_access(operation="x.y", outcome="ok", critical=True)

    assert sent == [None]
    assert waited == [sel_client._EXIT_DRAIN_SECS]


@pytest.mark.asyncio
async def test_each_request_gets_what_is_left_of_the_budget(manual_clock, monkeypatch) -> None:
    manual_clock.install(monkeypatch, sel_client)
    route = _Route(manual_clock, [1.5, 0.25], fail_last=True)
    monkeypatch.setattr(sel_client, "loopback_urlopen", route)

    with pytest.raises(SelClientError):
        _client().log_api_access(operation="x.y", outcome="ok", critical=True)

    assert route.timeouts == pytest.approx([_BUDGET, _BUDGET - 1.5])


@pytest.mark.asyncio
async def test_a_spent_budget_fails_the_write_before_another_request(
    manual_clock, monkeypatch
) -> None:
    manual_clock.install(monkeypatch, sel_client)
    route = _Route(manual_clock, [_BUDGET])
    monkeypatch.setattr(sel_client, "loopback_urlopen", route)

    with pytest.raises(SelClientError, match="timed out"):
        _client().log_api_access(operation="x.y", outcome="ok", critical=True)

    assert route.timeouts == [_BUDGET]

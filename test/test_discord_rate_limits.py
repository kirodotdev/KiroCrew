"""Discord REST rate-limit accounting and failure classification.

Covers what ``discord/client.py``'s request ladder does with the accounting
Discord hands back on every response: bucket pre-emption, a global hold that
stops every route, the invalid-request breaker that keeps the app clear of
Discord's 10,000-per-10-minutes IP block, and the permanent/transient split a
caller needs so a turn whose whole output failed to send is not recorded as a
delivered turn.

Everything runs against stubbed transports with an injected clock: no socket,
no network, no real sleeping, and no writes anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict

from kiro_crew.discord import client as dc
from kiro_crew.discord.client import (
    _BREAKER_COOLOFF_SECS,
    _DEFAULT_RETRY_AFTER_SECS,
    _INVALID_LIMIT,
    _INVALID_WINDOW_SECS,
    _MAX_GLOBAL_HOLD_SECS,
    _MAX_PREEMPT_SECS,
    _MAX_RETRY_AFTER_SECS,
    _MAX_TRACKED_ROUTES,
    _TRANSIENT_BACKOFF_SECS,
    _TRANSIENT_RETRIES,
    DISCORD_BLOCKED,
    DISCORD_OK,
    DISCORD_PERMANENT,
    DISCORD_TRANSIENT,
    DiscordApiResult,
    DiscordClient,
    _is_global_limit_exempt,
    _route_key,
)
from kiro_crew.messaging.outbound_files import OutboundFile

_TOKEN = "bot-secret"
#: Longer than the client's literal-segment ceiling, so it collapses like a
#: real interaction token rather than growing the route map per interaction.
_INTERACTION_TOKEN = "t" * 40
_CALLBACK_PATH = f"/interactions/1234567890/{_INTERACTION_TOKEN}/callback"


# ── Stub transport, injected clock ─────────────────────────────────────────


class _Clock:
    """Deterministic stand-in for ``time``.

    Only ``monotonic`` is used by the request ladder, and the fake sleep
    advances it, so a recorded back-off and the deadline it satisfies can never
    disagree the way a frozen clock plus a fake sleep would.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Asyncio:
    """Stands in for the ``asyncio`` name inside the client module.

    Records every sleep, advances the clock by it, and yields once so other
    tasks run; everything else delegates to the real module.
    """

    def __init__(self, clock: _Clock, events: list[tuple[str, Any]]) -> None:
        self._clock = clock
        self._events = events

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *args: Any, **kwargs: Any) -> None:
        self._events.append(("sleep", delay))
        self._clock.now += delay
        await asyncio.sleep(0)


class _Resp:
    """Minimal aiohttp ClientResponse stand-in."""

    def __init__(
        self,
        status: int,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        json_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._json_error = json_error
        # Case-insensitive like the real thing, so a header lookup that only
        # works against exact casing cannot pass here and fail in production.
        self.headers = CIMultiDict(headers or {})

    async def json(self, content_type: Any = None) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._body


class _CM:
    def __init__(self, value: Any, *, enter_error: BaseException | None = None) -> None:
        self._value = value
        self._enter_error = enter_error

    async def __aenter__(self) -> Any:
        if self._enter_error is not None:
            raise self._enter_error
        return self._value

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Session:
    """Serves queued responses (or exceptions) and records every call."""

    def __init__(self, responses: list[Any], events: list[tuple[str, Any]]) -> None:
        self._responses = list(responses)
        self._events = events
        self.kwargs: list[dict[str, Any]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> _CM:
        self._events.append(("request", f"{method} {url}"))
        self.kwargs.append(kwargs)
        # A dry queue answers 204 rather than raising, so an unexpected extra
        # attempt shows up as an extra recorded call instead of an IndexError.
        nxt = self._responses.pop(0) if self._responses else _Resp(204)
        if isinstance(nxt, BaseException):
            return _CM(None, enter_error=nxt)
        return _CM(nxt)

    async def close(self) -> None:
        self.closed = True


@dataclass
class _Harness:
    client: DiscordClient
    session: _Session
    clock: _Clock
    events: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def sleeps(self) -> list[float]:
        return [value for kind, value in self.events if kind == "sleep"]

    @property
    def requests(self) -> list[str]:
        return [value for kind, value in self.events if kind == "request"]


def _harness(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> _Harness:
    events: list[tuple[str, Any]] = []
    clock = _Clock()
    client = DiscordClient(token=_TOKEN)
    session = _Session(responses, events)

    async def _ensure() -> Any:
        return session

    monkeypatch.setattr(client, "_ensure_session", _ensure)
    monkeypatch.setattr(dc, "time", clock)
    monkeypatch.setattr(dc, "asyncio", _Asyncio(clock, events))
    return _Harness(client=client, session=session, clock=clock, events=events)


def _bucket_headers(bucket: str, remaining: int, reset_after: str) -> dict[str, str]:
    return {
        "X-RateLimit-Bucket": bucket,
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset-After": reset_after,
    }


# ── Bucket pre-emption ─────────────────────────────────────────────────────


class TestBucketPreemption:
    @pytest.mark.asyncio
    async def test_a_spent_bucket_delays_the_next_call_on_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [
                _Resp(200, {"id": "1"}, headers=_bucket_headers("b1", 0, "1.75")),
                _Resp(200, {"id": "2"}, headers=_bucket_headers("b1", 4, "3")),
            ],
        )
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == []
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        # The wait lands BEFORE the second request: earning the 429 the previous
        # response predicted is what the accounting exists to avoid.
        assert harness.events[1] == ("sleep", pytest.approx(1.75))
        assert len(harness.requests) == 2

    @pytest.mark.asyncio
    async def test_headroom_never_delays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(
            monkeypatch,
            [
                _Resp(200, {}, headers=_bucket_headers("b1", 1, "5")),
                _Resp(200, {}, headers=_bucket_headers("b1", 1, "5")),
            ],
        )
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == []

    @pytest.mark.asyncio
    async def test_a_spent_bucket_holds_every_route_sharing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discord charges several routes to one bucket, so state is keyed by
        the bucket: an edit must wait out a send that spent their shared limit."""
        harness = _harness(
            monkeypatch,
            [
                _Resp(200, {}, headers=_bucket_headers("shared-b", 3, "5")),
                _Resp(200, {}, headers=_bucket_headers("shared-b", 0, "2.5")),
                _Resp(200, {}, headers=_bucket_headers("shared-b", 3, "5")),
            ],
        )
        edit = ("PATCH", "/channels/9111/messages/7222333444")
        await harness.client.api_json(*edit, {})
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == []
        await harness.client.api_json(*edit, {})
        assert harness.sleeps == [pytest.approx(2.5)]

    @pytest.mark.asyncio
    async def test_a_bucketless_response_still_pre_empts_by_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.25"}
        harness = _harness(monkeypatch, [_Resp(200, {}, headers=headers), _Resp(200, {})])
        await harness.client.api_json("POST", "/channels/9111/typing", {})
        await harness.client.api_json("POST", "/channels/9111/typing", {})
        assert harness.sleeps == [pytest.approx(0.25)]

    @pytest.mark.asyncio
    async def test_a_spent_bucket_does_not_hold_a_different_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [_Resp(200, {}, headers=_bucket_headers("b1", 0, "4")), _Resp(200, {})],
        )
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        await harness.client.api_json("POST", "/channels/9222/messages", {})
        assert harness.sleeps == []

    @pytest.mark.asyncio
    async def test_the_pre_emptive_wait_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(
            monkeypatch,
            [_Resp(200, {}, headers=_bucket_headers("b1", 0, "900")), _Resp(200, {})],
        )
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [_MAX_PREEMPT_SECS]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            {"X-RateLimit-Remaining": "nope", "X-RateLimit-Reset-After": "1"},
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "soon"},
            {"X-RateLimit-Remaining": "0"},
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0"},
            {},
        ],
    )
    async def test_unusable_headers_carry_no_accounting(
        self, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(200, {}, headers=headers), _Resp(200, {})])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == []

    @pytest.mark.asyncio
    async def test_a_response_without_headers_at_all_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A proxy error page can answer without Discord's headers; that is
        missing information, not a failed request."""

        class _Bare:
            status = 200

            async def json(self, content_type: Any = None) -> Any:
                return {"id": "5"}

        harness = _harness(monkeypatch, [_Bare()])
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result and result.message_id == "5"


# ── Global hold ────────────────────────────────────────────────────────────


def _global_429(retry_after: float) -> _Resp:
    return _Resp(
        429,
        {"message": "You are being rate limited.", "retry_after": retry_after, "global": True},
        headers={"X-RateLimit-Scope": "global"},
    )


class TestGlobalHold:
    @pytest.mark.asyncio
    async def test_a_global_429_holds_an_unrelated_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app's 50 requests/second allowance is shared by every route, so
        the route that happened to hit it is not the only one that must stop."""
        harness = _harness(monkeypatch, [_global_429(3.0), _global_429(5.0), _Resp(204)])
        first = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert first.outcome == DISCORD_TRANSIENT and first.retryable
        second = await harness.client.api_json("POST", "/users/@me/channels", {})
        assert second.outcome == DISCORD_OK
        assert harness.events == [
            ("request", f"POST {dc._API_BASE}/channels/9111/messages"),
            ("sleep", pytest.approx(3.0)),
            ("request", f"POST {dc._API_BASE}/channels/9111/messages"),
            ("sleep", pytest.approx(5.0)),
            ("request", f"POST {dc._API_BASE}/users/@me/channels"),
        ]

    @pytest.mark.asyncio
    async def test_an_interaction_callback_ignores_the_global_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discord exempts interaction callbacks from the global limit, and
        they answer a ~3s deadline a hold would blow."""
        harness = _harness(monkeypatch, [_global_429(3.0), _global_429(5.0), _Resp(204)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert await harness.client.api_json("POST", _CALLBACK_PATH, {"type": 6})
        assert harness.sleeps == [pytest.approx(3.0)]

    @pytest.mark.asyncio
    async def test_a_route_scoped_429_leaves_other_routes_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rate_limited = _Resp(429, {"retry_after": 2.0}, headers={"X-RateLimit-Scope": "user"})
        harness = _harness(monkeypatch, [rate_limited, rate_limited, _Resp(204)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        await harness.client.api_json("POST", "/users/@me/channels", {})
        assert harness.sleeps == [pytest.approx(2.0)]

    @pytest.mark.asyncio
    async def test_the_global_hold_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(monkeypatch, [_global_429(9999.0), _Resp(204)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [_MAX_GLOBAL_HOLD_SECS]

    @pytest.mark.asyncio
    async def test_the_global_scope_header_alone_is_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 429 whose body omits the ``global`` flag but whose scope names it
        must still hold every route."""
        scoped = _Resp(429, {"retry_after": 1.0}, headers={"X-RateLimit-Scope": "global"})
        harness = _harness(monkeypatch, [scoped, scoped, _Resp(204)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.client._global_ready_at > harness.clock.now
        await harness.client.api_json("POST", "/users/@me/channels", {})
        assert len(harness.sleeps) == 2

    @pytest.mark.asyncio
    async def test_a_later_hold_never_shortens_an_earlier_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_global_429(20.0), _global_429(1.0)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        # 20s hold, 20s served, then a 1s hold: the deadline stays the later of
        # the two rather than being walked backwards.
        assert harness.client._global_ready_at == pytest.approx(harness.clock.now + 1.0)


# ── 429 back-off arithmetic ────────────────────────────────────────────────


class TestRetryAfter:
    @pytest.mark.asyncio
    async def test_the_fractional_body_beats_the_whole_second_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [
                _Resp(429, {"retry_after": 0.75}, headers={"Retry-After": "2"}),
                _Resp(204),
            ],
        )
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [pytest.approx(0.75)]

    @pytest.mark.asyncio
    async def test_a_short_back_off_never_shortens_a_longer_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One response can say two things about the same bucket: its headers
        reset in 8s while its body asks for 0.5s. The later deadline is the one
        that keeps the next caller out of a 429, so the shorter must not win.
        """
        harness = _harness(
            monkeypatch,
            [
                _Resp(
                    429,
                    {"retry_after": 0.5},
                    headers=_bucket_headers("b1", 0, "8"),
                ),
                _Resp(204),
                _Resp(204),
            ],
        )
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [pytest.approx(0.5)]
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps[1] == pytest.approx(7.5)

    @pytest.mark.asyncio
    async def test_the_header_is_used_when_the_body_has_no_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [_Resp(429, "not a mapping", headers={"Retry-After": "2"}), _Resp(204)],
        )
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [pytest.approx(2.0)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"retry_after": 2.5}, 2.5),
            ({"retry_after": "soon"}, _DEFAULT_RETRY_AFTER_SECS),
            ({"retry_after": None}, _DEFAULT_RETRY_AFTER_SECS),
            ({}, _DEFAULT_RETRY_AFTER_SECS),
            (None, _DEFAULT_RETRY_AFTER_SECS),
            ({"retry_after": 0.0}, 0.5),
            ({"retry_after": 900}, _MAX_RETRY_AFTER_SECS),
        ],
    )
    async def test_the_route_back_off_is_clamped_and_defaulted(
        self, monkeypatch: pytest.MonkeyPatch, body: Any, expected: float
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(429, body), _Resp(204)])
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [pytest.approx(expected)]

    @pytest.mark.asyncio
    async def test_a_second_429_gives_up_and_leaves_the_route_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [_Resp(429, {"retry_after": 1.0}), _Resp(429, {"retry_after": 4.0}), _Resp(204)],
        )
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result.outcome == DISCORD_TRANSIENT and not result
        assert len(harness.requests) == 2
        # Giving up does not discard what the 429 said: the next caller on the
        # route waits it out instead of earning a third 429.
        assert await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert harness.sleeps == [pytest.approx(1.0), pytest.approx(4.0)]


# ── Invalid-request breaker ────────────────────────────────────────────────


_BREAKER_LOG = "pausing all outbound requests"


class TestInvalidRequestBreaker:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,headers",
        [
            (401, {}),
            (403, {}),
            (429, {}),
            (429, {"X-RateLimit-Scope": "user"}),
        ],
    )
    async def test_it_opens_and_then_refuses_to_send(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        status: int,
        headers: dict[str, str],
    ) -> None:
        harness = _harness(monkeypatch, [])
        with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.client"):
            for _ in range(_INVALID_LIMIT):
                harness.client._note_invalid(status, CIMultiDict(headers))
            assert harness.client._breaker_until > harness.clock.now
            for _ in range(3):
                result = await harness.client.api_json("POST", "/channels/9/typing", {})
                assert result.outcome == DISCORD_BLOCKED
                assert result.retryable and not result and result.data is None
            # Nothing reached the wire, and the breaker states its case exactly
            # once however many callers it turns away.
            assert harness.requests == []
        assert sum(_BREAKER_LOG in r.message for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_a_shared_scope_429_never_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Discord excludes a shared-scope 429 from the ban count: it is
        another app's traffic on a limit we merely share, so counting it would
        stop this channel sending over something it did not cause."""
        harness = _harness(monkeypatch, [_Resp(204)])
        shared = CIMultiDict({"X-RateLimit-Scope": "shared"})
        for _ in range(_INVALID_LIMIT * 2):
            harness.client._note_invalid(429, shared)
        assert list(harness.client._invalid_hits) == []
        assert harness.client._breaker_until == 0.0
        assert await harness.client.api_json("POST", "/channels/9111/typing", {})

    @pytest.mark.asyncio
    async def test_a_2xx_or_5xx_never_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(monkeypatch, [])
        for status in (200, 204, 400, 404, 500, 503):
            for _ in range(_INVALID_LIMIT):
                harness.client._note_invalid(status, CIMultiDict())
        assert harness.client._breaker_until == 0.0

    @pytest.mark.asyncio
    async def test_it_closes_after_the_cool_off_and_starts_clean(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(204)])
        for _ in range(_INVALID_LIMIT):
            harness.client._note_invalid(403, CIMultiDict())
        assert not await harness.client.api_json("POST", "/channels/9/typing", {})
        harness.clock.now += _BREAKER_COOLOFF_SECS
        with caplog.at_level(logging.INFO, logger="kiro_crew.discord.client"):
            assert await harness.client.api_json("POST", "/channels/9/typing", {})
        assert "breaker closed" in caplog.text
        assert list(harness.client._invalid_hits) == []
        assert harness.requests == [f"POST {dc._API_BASE}/channels/9/typing"]

    def test_a_slow_drip_ages_out_and_never_opens_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The window is rolling, so a channel that fails occasionally for days
        must not accumulate its way into a cool-off."""
        harness = _harness(monkeypatch, [])
        step = _INVALID_WINDOW_SECS / (_INVALID_LIMIT / 2)
        for _ in range(_INVALID_LIMIT * 4):
            harness.clock.now += step
            harness.client._note_invalid(403, CIMultiDict())
        assert harness.client._breaker_until == 0.0
        assert len(harness.client._invalid_hits) < _INVALID_LIMIT

    @pytest.mark.asyncio
    async def test_a_429_answered_end_to_end_counts_once_per_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(429, {"retry_after": 1.0}), _Resp(204)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert len(harness.client._invalid_hits) == 1


# ── Failure classification ─────────────────────────────────────────────────


class TestClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 413])
    async def test_a_4xx_is_permanent_and_never_retried(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        harness = _harness(
            monkeypatch,
            [_Resp(status, {"code": 10008, "message": "Unknown Message"})],
        )
        result = await harness.client.api_json("PATCH", "/channels/9111/messages/1", {})
        assert result.outcome == DISCORD_PERMANENT
        assert not result and not result.retryable and result.data is None
        assert (result.status, result.code, result.detail) == (
            status,
            10008,
            "Unknown Message",
        )
        assert len(harness.requests) == 1
        assert harness.sleeps == []

    @pytest.mark.asyncio
    async def test_a_5xx_is_retried_with_a_bounded_linear_back_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(500), _Resp(502), _Resp(503)])
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result.outcome == DISCORD_TRANSIENT and result.retryable
        assert result.status == 503
        assert len(harness.requests) == _TRANSIENT_RETRIES + 1
        assert harness.sleeps == [
            pytest.approx(_TRANSIENT_BACKOFF_SECS),
            pytest.approx(_TRANSIENT_BACKOFF_SECS * 2),
        ]

    @pytest.mark.asyncio
    async def test_a_5xx_that_clears_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(monkeypatch, [_Resp(500), _Resp(200, {"id": "77"})])
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result.outcome == DISCORD_OK and result.message_id == "77"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            aiohttp.ClientError("reset"),
            aiohttp.ClientConnectorError(None, OSError("dns")),  # type: ignore[arg-type]
            asyncio.TimeoutError(),
        ],
    )
    async def test_a_transport_failure_is_transient_and_leaks_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exc: BaseException,
    ) -> None:
        harness = _harness(monkeypatch, [exc, exc, exc])
        with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.client"):
            result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result.outcome == DISCORD_TRANSIENT and result.retryable
        assert result.status == 0 and result.detail == type(exc).__name__
        assert len(harness.requests) == _TRANSIENT_RETRIES + 1
        assert _TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_a_transport_failure_that_clears_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [asyncio.TimeoutError(), _Resp(204)])
        result = await harness.client.api_json("POST", "/channels/9111/typing", {})
        assert result.outcome == DISCORD_OK and result.data == {}
        assert harness.sleeps == [pytest.approx(_TRANSIENT_BACKOFF_SECS)]

    @pytest.mark.asyncio
    async def test_a_transport_failure_does_not_spend_ban_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [asyncio.TimeoutError(), _Resp(204)])
        await harness.client.api_json("POST", "/channels/9111/typing", {})
        assert list(harness.client._invalid_hits) == []

    def test_the_result_contract(self) -> None:
        landed = DiscordApiResult(DISCORD_OK, data={"id": 42}, status=200)
        assert landed and not landed.retryable and landed.message_id == "42"
        for outcome in (DISCORD_PERMANENT, DISCORD_TRANSIENT, DISCORD_BLOCKED):
            failed = DiscordApiResult(outcome)
            assert not failed and failed.data is None and failed.message_id == ""
        assert DiscordApiResult(DISCORD_OK, data=[{"id": 1}]).message_id == ""


# ── Malformed bodies ───────────────────────────────────────────────────────


class TestMalformedBodies:
    @pytest.mark.asyncio
    async def test_a_non_json_2xx_degrades_to_an_empty_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(200, json_error=ValueError("html error page"))])
        result = await harness.client.api_json("GET", "/channels/9111", None)
        assert result.outcome == DISCORD_OK and result.data == {}

    @pytest.mark.asyncio
    async def test_a_non_json_4xx_still_classifies_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(404, json_error=ValueError("html error page"))])
        result = await harness.client.api_json("GET", "/channels/9111", None)
        assert result.outcome == DISCORD_PERMANENT
        assert (result.status, result.code, result.detail) == (404, 0, "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [[1, 2, 3], "text", 7, None])
    async def test_a_non_object_error_body_still_classifies(
        self, monkeypatch: pytest.MonkeyPatch, body: Any
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(400, body)])
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result.outcome == DISCORD_PERMANENT and result.code == 0

    @pytest.mark.asyncio
    async def test_a_non_numeric_error_code_is_dropped_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(400, {"code": "nope", "message": 5})])
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert result.code == 0 and result.detail == "5"

    @pytest.mark.asyncio
    async def test_a_2xx_json_array_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The application-command bulk overwrite answers a top-level array."""
        harness = _harness(monkeypatch, [_Resp(200, [{"id": "1"}, {"id": "2"}])])
        result = await harness.client.api_json("PUT", "/applications/1/commands", [])
        assert result.outcome == DISCORD_OK and result.data == [{"id": "1"}, {"id": "2"}]

    @pytest.mark.asyncio
    async def test_an_over_long_error_message_is_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(400, {"message": "x" * 5000})])
        result = await harness.client.api_json("POST", "/channels/9111/messages", {})
        assert len(result.detail) == 200


# ── Route keys and exemptions ──────────────────────────────────────────────


class TestRouteKeys:
    def test_message_ids_collapse_but_the_channel_stays(self) -> None:
        first = _route_key("PATCH", "/channels/9111/messages/7222333444")
        second = _route_key("PATCH", "/channels/9111/messages/8222333444")
        assert first == second
        assert "9111" in first
        assert first != _route_key("PATCH", "/channels/9222/messages/7222333444")

    def test_the_verb_is_part_of_the_route(self) -> None:
        assert _route_key("POST", "/channels/9111/messages") != _route_key(
            "GET", "/channels/9111/messages"
        )

    def test_an_interaction_token_collapses(self) -> None:
        assert _route_key("POST", _CALLBACK_PATH) == _route_key(
            "POST", f"/interactions/9999999999/{'z' * 60}/callback"
        )

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/interactions/1/tok/callback", True),
            ("/webhooks/1234567890/tok/messages/@original", True),
            ("/channels/9111/messages", False),
            ("/users/@me/channels", False),
        ],
    )
    def test_only_interaction_routes_are_exempt(self, path: str, expected: bool) -> None:
        assert _is_global_limit_exempt(path) is expected


# ── State bounds and isolation ─────────────────────────────────────────────


class TestStateBounds:
    def test_the_tracked_maps_are_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(monkeypatch, [])
        overflow = _MAX_TRACKED_ROUTES + 50
        for index in range(overflow):
            route = _route_key("POST", f"/channels/{900000 + index}/messages")
            harness.client._note_headers(route, CIMultiDict(_bucket_headers(f"b{index}", 0, "1")))
        assert len(harness.client._route_buckets) == _MAX_TRACKED_ROUTES
        assert len(harness.client._holds) == _MAX_TRACKED_ROUTES
        assert f"b{overflow - 1}" in harness.client._holds
        assert "b0" not in harness.client._holds

    def test_re_touching_a_route_keeps_it_from_being_evicted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [])
        keeper = _route_key("POST", "/channels/9111/messages")
        headers = CIMultiDict(_bucket_headers("keeper", 5, "1"))
        harness.client._note_headers(keeper, headers)
        for index in range(_MAX_TRACKED_ROUTES - 1):
            harness.client._note_headers(
                _route_key("POST", f"/channels/{900000 + index}/messages"),
                CIMultiDict(_bucket_headers(f"b{index}", 5, "1")),
            )
            harness.client._note_headers(keeper, headers)
        harness.client._note_headers(
            _route_key("POST", "/channels/999111/messages"),
            CIMultiDict(_bucket_headers("newest", 5, "1")),
        )
        assert harness.client._route_buckets[keeper] == "keeper"

    @pytest.mark.asyncio
    async def test_accounting_is_per_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two clients are two bot identities: one's global hold must not stall
        the other, and one's breaker must not silence the other."""
        harness = _harness(monkeypatch, [_global_429(9.0), _global_429(9.0)])
        await harness.client.api_json("POST", "/channels/9111/messages", {})
        other = DiscordClient(token="second-bot")
        session = _Session([_Resp(204)], harness.events)

        async def _ensure() -> Any:
            return session

        monkeypatch.setattr(other, "_ensure_session", _ensure)
        before = len(harness.sleeps)
        assert await other.api_json("POST", "/channels/9111/messages", {})
        assert len(harness.sleeps) == before
        assert other._global_ready_at == 0.0
        assert list(other._invalid_hits) == []


# ── The wait never blocks the loop ─────────────────────────────────────────


class TestLoopIsNotBlocked:
    @pytest.mark.asyncio
    async def test_other_tasks_run_while_a_bucket_refills(self) -> None:
        """A hold is an ``await``, not a stall: a bucket refilling on one
        channel must not freeze every other turn in the process. Asserts the
        SHAPE (other tasks were scheduled during the wait), not a duration.
        """
        client = DiscordClient(token=_TOKEN)
        events: list[tuple[str, Any]] = []
        session = _Session([_Resp(204)], events)

        async def _ensure() -> Any:
            return session

        client._ensure_session = _ensure  # type: ignore[method-assign]
        hold = 0.1
        client._set_hold(_route_key("POST", "/channels/9111/typing"), hold)
        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(hold / 20)
                ticks += 1

        ticker = asyncio.create_task(_ticker())
        try:
            assert await client.api_json("POST", "/channels/9111/typing", {})
        finally:
            ticker.cancel()
        assert ticks >= 2


# ── Classified send verbs ──────────────────────────────────────────────────


class TestClassifiedSendVerbs:
    @pytest.mark.asyncio
    async def test_send_message_result_carries_the_id_and_the_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(200, {"id": 42})])
        result = await harness.client.send_message_result(
            "9111", "hello", components=[{"type": 1}], reply_to_message_id="7222333444"
        )
        assert result.outcome == DISCORD_OK and result.message_id == "42"
        payload = harness.session.kwargs[0]["json"]
        assert payload["content"] == "hello"
        assert payload["allowed_mentions"] == {"parse": []}
        assert payload["components"] == [{"type": 1}]
        assert payload["message_reference"] == {
            "message_id": "7222333444",
            "fail_if_not_exists": False,
        }

    @pytest.mark.asyncio
    async def test_send_message_result_reports_a_permanent_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch, [_Resp(403, {"code": 50013, "message": "Missing Permissions"})]
        )
        result = await harness.client.send_message_result("9111", "hello")
        assert result.outcome == DISCORD_PERMANENT and not result.retryable
        assert result.message_id == "" and result.code == 50013
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_send_message_result_switches_to_multipart_for_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(200, {"id": "5"})])
        files = [OutboundFile(path="/tmp/a.png", data=b"png", alt="", mime="image/png")]
        assert await harness.client.send_message_result("9111", "hi", files=files)
        kwargs = harness.session.kwargs[0]
        assert "json" not in kwargs
        assert isinstance(kwargs["data"], aiohttp.FormData)

    @pytest.mark.asyncio
    async def test_edit_message_result_keeps_an_empty_component_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(200, {"id": "5"})])
        result = await harness.client.edit_message_result(
            "9111", "7222333444", "body", components=[]
        )
        assert result.outcome == DISCORD_OK
        assert harness.session.kwargs[0]["json"]["components"] == []
        assert harness.requests == [f"PATCH {dc._API_BASE}/channels/9111/messages/7222333444"]

    @pytest.mark.asyncio
    async def test_edit_message_result_reports_a_transient_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(503), _Resp(503), _Resp(503)])
        result = await harness.client.edit_message_result("9111", "7222333444", "body")
        assert result.outcome == DISCORD_TRANSIENT and result.retryable


# ── The truthiness contract the old callers rely on ───────────────────────


class TestLegacyContract:
    @pytest.mark.asyncio
    async def test_the_body_shape_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _harness(
            monkeypatch,
            [
                _Resp(204),
                _Resp(200, {"id": "9"}),
                _Resp(404, {"code": 10003}),
                _Resp(200, json_error=ValueError("html")),
            ],
        )
        assert await harness.client._api("POST", "/channels/9111/typing", {}) == {}
        assert await harness.client._api("POST", "/channels/9111/messages", {}) == {"id": "9"}
        assert await harness.client._api("GET", "/channels/9111", None) is None
        assert await harness.client._api("GET", "/channels/9111", None) == {}

    @pytest.mark.asyncio
    async def test_the_send_and_edit_verbs_keep_their_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [_Resp(200, {"id": 991}), _Resp(403, {}), _Resp(200, {}), _Resp(403, {})],
        )
        client = harness.client
        assert await client.send_message("9111", "hi") == "991"
        assert await client.send_message("9111", "hi") is None
        assert await client.edit_message("9111", "7222333444", "hi") is True
        assert await client.edit_message("9111", "7222333444", "hi") is False

    @pytest.mark.asyncio
    async def test_the_multipart_verbs_keep_their_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(200, {"id": "5"}), _Resp(403, {})])
        files = [OutboundFile(path="/tmp/a.png", data=b"png", alt="", mime="image/png")]
        client = harness.client
        assert await client.send_message_with_files("9111", "hi", files) == "5"
        assert await client.edit_message_with_files("9111", "7222333444", "hi", files) is False


class TestNonFiniteRateLimitNumbersCannotWedgeTheClient:
    """A non-finite rate-limit number must read as absent, not become a sleep.

    ``float("nan")`` parses, and NaN then survives every downstream guard: ``nan
    <= 0`` is False so the "no hold needed" branch does not take it, and
    ``min(max(nan, floor), ceiling)`` is still NaN. One malformed or hostile
    header would otherwise become ``asyncio.sleep(nan)`` and wedge the client for
    the life of the process.
    """

    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
    def test_a_non_finite_value_reads_as_absent(self, raw: str) -> None:
        assert dc._coerce_float(raw) is None

    @pytest.mark.parametrize("raw", ["0", "0.5", "12", "1.75", 3, 2.5])
    def test_a_finite_value_still_parses(self, raw: object) -> None:
        value = dc._coerce_float(raw)
        assert value is not None and math.isfinite(value)

    def test_a_nan_reset_after_header_arms_no_hold(self) -> None:
        """The bucket-hold path is where a NaN would have become an unbounded
        pre-emptive wait on every later call to that route."""
        client = DiscordClient(token=_TOKEN)
        client._note_headers(
            "/channels/1/messages",
            {
                "X-RateLimit-Bucket": "b1",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset-After": "nan",
            },
        )
        assert client._holds == {}


class TestRouteKeyCarriesNoCredential:
    """The route key is the string every rate-limit log line prints.

    Two Discord paths carry a CREDENTIAL as a path segment -- an interaction token
    and a webhook token, each of which authorizes the call it rides in. Collapsing
    them by LENGTH is the wrong footing for that: the length ceiling exists to keep
    the bucket table from growing one entry per button press, and a token shorter
    than it (or a Discord change to the format) would print verbatim into the log
    ring, where the operator's own ``kirocrew logs`` would then hand it out.
    """

    #: A short token is the case a length rule misses; a long one is the case it
    #: catches anyway; the realistic length is what actually ships.
    @pytest.mark.parametrize("token", ["ab", "x" * 24, "aW50ZXJhY3Rpb24" * 20])
    def test_an_interaction_token_is_collapsed_at_any_length(self, token: str) -> None:
        route = dc._route_key("POST", f"/interactions/1234567890123456789/{token}/callback")
        assert token not in route
        assert route == "POST /interactions/{id}/{token}/callback"

    @pytest.mark.parametrize("token", ["ab", "y" * 24])
    def test_a_webhook_token_is_collapsed_at_any_length(self, token: str) -> None:
        """The webhook id stays verbatim -- it is the rate-limit major param."""
        route = dc._route_key("PATCH", f"/webhooks/1234567890123456789/{token}/messages/@original")
        assert token not in route
        assert route == "PATCH /webhooks/1234567890123456789/{token}/messages/@original"

    def test_an_ordinary_route_is_still_readable(self) -> None:
        """The collapse must not eat the segments the log line exists to show."""
        assert dc._route_key("POST", "/channels/999/messages") == "POST /channels/999/messages"
        assert (
            dc._route_key("PUT", "/applications/1234567890123456789/commands")
            == "PUT /applications/{id}/commands"
        )

"""Authorization is re-read across the Discord REST ladder's own waits.

``discord/client.py``'s request ladder suspends itself in four places -- a
pre-emptive bucket hold, a 429 back-off, a global hold, and the 5xx/connector
back-off -- for up to tens of seconds. An operator who withdraws a destination
during one of those waits expects the attempt that follows not to land, which
means the ladder has to ask again rather than trusting the caller's pre-send
reading from before the wait.

Two authorities are re-read, and these tests exercise both: the operator's
``channels`` governance ceiling, which the client asks the messaging seam for
directly, and the transport's live rosters, which arrive as the injected
``still_permitted`` predicate. Both fail closed.

Everything runs against a stubbed transport with an injected clock: no socket,
no network, no real sleeping, and no writes anywhere.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict

from kiro_crew.discord import client as dc
from kiro_crew.discord.client import (
    _REVOKED_DETAIL,
    DISCORD_BLOCKED,
    DISCORD_OK,
    DISCORD_TRANSIENT,
    DiscordClient,
    _guarded_destination,
)
from kiro_crew.discord.transport import DiscordTransport

_TOKEN = "bot-secret"
_CHANNEL = "9111222333"
_SEND_PATH = f"/channels/{_CHANNEL}/messages"
#: Longer than the client's literal-segment ceiling, like a real one.
_INTERACTION_TOKEN = "t" * 40
_CALLBACK_PATH = f"/interactions/1234567890/{_INTERACTION_TOKEN}/callback"


# -- Stub transport, injected clock ------------------------------------------


class _Clock:
    """Deterministic stand-in for ``time``; the fake sleep advances it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Asyncio:
    """Stands in for the ``asyncio`` name inside the client module.

    Records every sleep and advances the clock by it, so a back-off and the
    deadline it satisfies cannot disagree. A hook fires DURING the sleep, which
    is what lets a test revoke a destination mid-wait.
    """

    def __init__(self, clock: _Clock, events: list[tuple[str, Any]]) -> None:
        self._clock = clock
        self._events = events
        self.on_sleep: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *args: Any, **kwargs: Any) -> None:
        self._events.append(("sleep", delay))
        self._clock.now += delay
        if self.on_sleep is not None:
            self.on_sleep()
        await asyncio.sleep(0)


class _Resp:
    """Minimal aiohttp ClientResponse stand-in."""

    def __init__(
        self, status: int, body: Any = None, *, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self._body = body
        self.headers = CIMultiDict(headers or {})

    async def json(self, content_type: Any = None) -> Any:
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

    def request(self, method: str, url: str, **kwargs: Any) -> _CM:
        self._events.append(("request", f"{method} {url}"))
        # A dry queue answers 204 rather than raising, so an unexpected extra
        # attempt shows up as an extra recorded call instead of an IndexError.
        nxt = self._responses.pop(0) if self._responses else _Resp(204)
        if isinstance(nxt, BaseException):
            return _CM(None, enter_error=nxt)
        return _CM(nxt)

    async def close(self) -> None:
        return None


@dataclass
class _Harness:
    client: DiscordClient
    clock: _Clock
    fake_asyncio: _Asyncio
    #: Flipped by a test to withdraw the operator's ``channels`` ceiling.
    ceiling: dict[str, bool] = field(default_factory=lambda: {"open": True})
    #: Channel ids the injected roster predicate still admits.
    roster: set[str] = field(default_factory=set)
    events: list[tuple[str, Any]] = field(default_factory=list)
    #: Every destination the roster predicate was asked about.
    asked: list[str] = field(default_factory=list)

    @property
    def sleeps(self) -> list[float]:
        return [value for kind, value in self.events if kind == "sleep"]

    @property
    def requests(self) -> list[str]:
        return [value for kind, value in self.events if kind == "request"]

    def revoke_during_next_wait(self, *, ceiling: bool = False) -> None:
        """Withdraw authorization from inside the ladder's next sleep."""

        def _revoke() -> None:
            if ceiling:
                self.ceiling["open"] = False
            else:
                self.roster.clear()

        self.fake_asyncio.on_sleep = _revoke


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    *,
    wire_predicate: bool = True,
) -> _Harness:
    events: list[tuple[str, Any]] = []
    clock = _Clock()
    fake_asyncio = _Asyncio(clock, events)
    client = DiscordClient(token=_TOKEN)
    session = _Session(responses, events)
    harness = _Harness(client=client, clock=clock, fake_asyncio=fake_asyncio, events=events)
    harness.roster.add(_CHANNEL)

    async def _ensure() -> Any:
        return session

    async def _ceiling(channel_type: str) -> bool:
        events.append(("ceiling", channel_type))
        return harness.ceiling["open"]

    def _still_permitted(channel_id: str) -> bool:
        harness.asked.append(channel_id)
        return channel_id in harness.roster

    monkeypatch.setattr(client, "_ensure_session", _ensure)
    monkeypatch.setattr(dc, "time", clock)
    monkeypatch.setattr(dc, "asyncio", fake_asyncio)
    monkeypatch.setattr(dc, "channel_inbound_permitted", _ceiling)
    if wire_predicate:
        client.still_permitted = _still_permitted
    return harness


def _bucket_headers(bucket: str, remaining: int, reset_after: str) -> dict[str, str]:
    return {
        "X-RateLimit-Bucket": bucket,
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset-After": reset_after,
    }


def _rate_limited(retry_after: float = 2.0, *, is_global: bool = False) -> _Resp:
    return _Resp(429, {"retry_after": retry_after, "global": is_global})


# -- Which routes carry a guarded destination --------------------------------


class TestGuardedDestination:
    @pytest.mark.parametrize(
        "path,expected",
        [
            (_SEND_PATH, _CHANNEL),
            (f"/channels/{_CHANNEL}/messages/7222333444", _CHANNEL),
            (f"/channels/{_CHANNEL}/messages/7222333444/threads", _CHANNEL),
            (f"/channels/{_CHANNEL}", _CHANNEL),
            # A press's own reply names no channel and is re-judged elsewhere.
            (_CALLBACK_PATH, ""),
            ("/users/@me/channels", ""),
            ("/gateway/bot", ""),
        ],
    )
    def test_only_channel_routes_name_a_destination(self, path: str, expected: str) -> None:
        assert _guarded_destination(path) == expected


# -- The happy path pays nothing ---------------------------------------------


class TestNoWaitNoRecheck:
    @pytest.mark.asyncio
    async def test_a_send_that_never_waits_is_not_re_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller's own pre-send reading is the most recent one when no time
        has passed, so the guard must stay off the common path entirely."""
        harness = _harness(monkeypatch, [_Resp(200, {"id": "1"})])
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_OK
        assert harness.sleeps == []
        assert harness.asked == []
        assert [kind for kind, _ in harness.events if kind == "ceiling"] == []


# -- Each of the ladder's four waits ----------------------------------------


class TestRecheckAcrossEveryWait:
    @pytest.mark.asyncio
    async def test_a_preemptive_hold_that_loses_the_roster_abandons_the_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [
                _Resp(200, {"id": "1"}, headers=_bucket_headers("b1", 0, "4.0")),
                _Resp(200, {"id": "2"}),
            ],
        )
        assert await harness.client.api_json("POST", _SEND_PATH, {})
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        # The second attempt was never issued: the whole point is that the
        # message does not reach the withdrawn destination.
        assert len(harness.requests) == 1
        assert harness.asked == [_CHANNEL]

    @pytest.mark.asyncio
    async def test_a_429_backoff_that_loses_the_roster_abandons_the_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_a_global_hold_that_loses_the_roster_abandons_the_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GLOBAL 429 holds every non-exempt route, the longest of the ladder's
        waits, so it is the widest window a revocation can land in."""
        harness = _harness(
            monkeypatch,
            [_rate_limited(20.0, is_global=True), _Resp(200, {"id": "2"})],
        )
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_a_5xx_backoff_that_loses_the_roster_abandons_the_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_Resp(503, {}), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_a_connector_backoff_that_loses_the_roster_abandons_the_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(
            monkeypatch,
            [aiohttp.ClientConnectionError("boom"), _Resp(200, {"id": "2"})],
        )
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        assert len(harness.requests) == 1


# -- The operator's channels ceiling ----------------------------------------


class TestChannelsCeiling:
    @pytest.mark.asyncio
    async def test_a_ceiling_flipped_mid_wait_abandons_the_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The governance flip is the other half of the operator's expectation,
        and it is read by the client itself rather than by the predicate."""
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait(ceiling=True)
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        assert len(harness.requests) == 1
        # Refused on the ceiling alone: the roster is never reached.
        assert harness.asked == []

    @pytest.mark.asyncio
    async def test_the_ceiling_is_read_with_no_predicate_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare client (a unit harness, or a transport not yet built) still
        honours the governance ceiling rather than skipping the re-check."""
        harness = _harness(
            monkeypatch,
            [_rate_limited(2.0), _Resp(200, {"id": "2"})],
            wire_predicate=False,
        )
        assert harness.client.still_permitted is None
        harness.revoke_during_next_wait(ceiling=True)
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL

    @pytest.mark.asyncio
    async def test_an_unguarded_route_is_still_retried_after_its_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A press's own reply names no channel, is re-judged against the live
        rosters before it is answered, and has a ~3 second deadline, so the
        roster is not consulted for it."""
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {})
        assert result.outcome == DISCORD_OK
        assert len(harness.requests) == 2
        assert harness.asked == []


# -- Fail-closed -------------------------------------------------------------


class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_a_predicate_that_raises_is_read_as_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is a network egress boundary: a predicate that cannot answer has
        not said yes."""
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])

        def _boom(channel_id: str) -> bool:
            raise RuntimeError("roster unavailable")

        harness.client.still_permitted = _boom
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_a_still_authorized_destination_keeps_retrying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard refuses a withdrawal, not a rate limit: an unchanged roster
        must leave the ladder's own retry budget intact."""
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_OK
        assert result.message_id == "2"
        assert len(harness.requests) == 2
        assert harness.asked == [_CHANNEL]

    @pytest.mark.asyncio
    async def test_a_revoked_destination_is_not_reported_as_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A withdrawal must not read as "try again later": re-driving it is
        exactly what the operator asked to stop."""
        harness = _harness(monkeypatch, [_Resp(503, {}), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome != DISCORD_TRANSIENT
        assert result.data is None
        assert not result


# -- The transport's roster predicate ---------------------------------------


def _transport(**kwargs: Any) -> tuple[DiscordTransport, DiscordClient]:
    client = DiscordClient(token=_TOKEN)
    transport = DiscordTransport(client, **kwargs)
    return transport, client


class TestTransportPredicate:
    def test_the_transport_installs_the_predicate_on_its_client(self) -> None:
        """Wired in the constructor, so every transport carries the contract --
        not only the one the gateway builds."""
        _, client = _transport(allowed_user_ids=["u1"])
        assert client.still_permitted is not None

    def test_a_thread_on_the_roster_is_still_permitted(self) -> None:
        transport, _ = _transport(allowed_thread_ids=["555666777"])
        assert transport._still_may_send_to("555666777") is True

    def test_a_thread_withdrawn_from_the_roster_is_refused(self) -> None:
        """Discord channel types are immutable, so a cached type is never stale:
        a known thread missing from the roster has been withdrawn.

        The DM roster is deliberately non-empty, so the fall-through below would
        answer True: the refusal can only come from the thread check itself.
        """
        transport, client = _transport(allowed_user_ids=["u1"], allowed_thread_ids=["555666777"])
        client._channel_types["888999000"] = 11
        assert transport._still_may_send_to("888999000") is False

    def test_a_dm_channel_passes_while_the_roster_admits_anybody(self) -> None:
        """A DM link persists the channel id, and the roster holds user ids, so
        the pairing is not derivable here -- the same gap ``may_send_to``
        documents. What is answerable is whether anyone is authorized at all."""
        transport, _ = _transport(allowed_user_ids=["u1"])
        assert transport._still_may_send_to("444555666") is True

    def test_an_empty_roster_refuses_every_destination(self) -> None:
        """Deny-by-default: an empty allow-list authorizes nobody, so a send in
        flight to a DM is refused on it."""
        transport, _ = _transport(allowed_user_ids=[])
        assert transport._still_may_send_to("444555666") is False

    def test_a_missing_channel_id_is_refused(self) -> None:
        transport, _ = _transport(allowed_user_ids=["u1"])
        assert transport._still_may_send_to("") is False

    def test_an_unseen_channel_type_does_not_refuse_on_a_guess(self) -> None:
        """``None`` from the cache means "not known here", and guessing either
        way is worse than falling to the roster test."""
        transport, client = _transport(allowed_user_ids=["u1"])
        assert client.cached_channel_is_thread("123456789") is None
        assert transport._still_may_send_to("123456789") is True

    def test_the_cached_reader_issues_no_request(self) -> None:
        """It runs INSIDE the REST ladder, so resolving a type there would
        re-enter the ladder it is guarding."""
        _, client = _transport(allowed_user_ids=["u1"])
        client._channel_types["555666777"] = 11
        client._channel_types["444555666"] = 0
        assert client.cached_channel_is_thread("555666777") is True
        assert client.cached_channel_is_thread("444555666") is False

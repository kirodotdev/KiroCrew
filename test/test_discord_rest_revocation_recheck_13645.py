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

import ast
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict

from kiro_crew.discord import client as dc
from kiro_crew.discord import transport as dt
from kiro_crew.discord.client import (
    _REVOKED_DETAIL,
    _UNANSWERABLE_DETAIL,
    _UNATTRIBUTABLE_DETAIL,
    DISCORD_BLOCKED,
    DISCORD_OK,
    DISCORD_TRANSIENT,
    DiscordClient,
    DiscordInbound,
    SendPermission,
    _guarded_destination,
)
from kiro_crew.discord.transport import DiscordTransport

#: Structural assertions read their sources from here, not from the process
#: working directory, so a run started anywhere reads this checkout.
_REPO_ROOT = Path(__file__).resolve().parents[1]

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
    #: What the injected roster predicate answers for a destination it does not
    #: admit. A withdrawal by default, since that is what an id the roster can
    #: place and refuses means; a test that wants the other ground sets it.
    refusal: SendPermission = field(default_factory=SendPermission.revoked)

    @property
    def sleeps(self) -> list[float]:
        return [value for kind, value in self.events if kind == "sleep"]

    @property
    def requests(self) -> list[str]:
        return [value for kind, value in self.events if kind == "request"]

    #: Set by a test to withdraw the roster from inside the governance read.
    revoke_in_ceiling_read: bool = False

    def revoke_during_next_wait(self, *, ceiling: bool = False) -> None:
        """Withdraw authorization from inside the ladder's next sleep."""

        def _revoke() -> None:
            if ceiling:
                self.ceiling["open"] = False
            else:
                self.roster.clear()

        self.fake_asyncio.on_sleep = _revoke

    def revoke_during_the_governance_read(self) -> None:
        """Withdraw the roster while the governance read is suspended.

        The governance read is an ``await``, so it is a wait like any other and an
        operator's edit can land inside it.
        """
        self.revoke_in_ceiling_read = True


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
        if harness.revoke_in_ceiling_read:
            harness.roster.clear()
        return harness.ceiling["open"]

    def _still_permitted(channel_id: str) -> SendPermission:
        harness.asked.append(channel_id)
        if channel_id in harness.roster:
            return SendPermission.allow()
        return harness.refusal

    monkeypatch.setattr(client, "_ensure_session", _ensure)
    monkeypatch.setattr(dc, "time", clock)
    monkeypatch.setattr(dc, "asyncio", fake_asyncio)
    monkeypatch.setattr(dc, "channel_outbound_permitted", _ceiling)
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
        rosters before it is answered, and has a ~3 second deadline, so NEITHER
        authority is consulted for it: the route is classified first."""
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {})
        assert result.outcome == DISCORD_OK
        assert len(harness.requests) == 2
        assert harness.asked == []
        # The ceiling read is a blocking ProfileStore read on a ~3s deadline, and
        # an unguarded route cannot be refused on a destination it does not name.
        assert [e for e in harness.events if e[0] == "ceiling"] == []

    @pytest.mark.asyncio
    async def test_a_guarded_route_does_read_the_ceiling_after_a_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control for the assertion above: the ceiling IS read once the
        route names a channel, so its absence there is the classification
        working rather than the ceiling never being read at all."""
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"id": "2"})])
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_OK
        assert [e for e in harness.events if e[0] == "ceiling"] == [("ceiling", "discord")]


# -- A caller supplies the destination its route cannot name -------------------


class TestCallerSuppliedDestination:
    """An interaction reply's destination rides inside an opaque token.

    The path cannot yield it, but the dispatcher holds it, so the caller passes it
    and the ladder re-checks BOTH authorities across its waits. The ceiling is read
    first and the roster LAST, the same order every other route uses: the ceiling
    read is an ``await``, so a roster reading taken before it could describe a
    destination already withdrawn. A ceiling refusal therefore never reaches the
    roster at all, which is what these tests assert by watching ``asked``.
    """

    @pytest.mark.asyncio
    async def test_a_withdrawn_destination_stops_an_interaction_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {}, destination=_CHANNEL)
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        # The second attempt was never issued.
        assert len(harness.requests) == 1
        assert harness.asked == [_CHANNEL]

    @pytest.mark.asyncio
    async def test_a_still_permitted_destination_lets_the_retry_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {}, destination=_CHANNEL)
        assert result.outcome == DISCORD_OK
        assert len(harness.requests) == 2
        assert harness.asked == [_CHANNEL]

    @pytest.mark.asyncio
    async def test_a_ceiling_withdrawn_during_the_wait_stops_the_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The governance ceiling is the other authority over this route, and a
        withdrawal landing inside the wait has to stop the attempt after it."""
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        harness.revoke_during_next_wait(ceiling=True)
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {}, destination=_CHANNEL)
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        # The second attempt was never issued, and the ceiling alone decided it, so
        # the roster is never reached -- the same shape a route naming its channel has.
        assert len(harness.requests) == 1
        assert harness.asked == []
        assert [e for e in harness.events if e[0] == "ceiling"] == [("ceiling", "discord")]

    @pytest.mark.asyncio
    async def test_the_ceiling_is_read_through_the_outbound_entry_point(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A send is outbound traffic, so the row it leaves has to say so. Pinned
        by which name the client calls, because the direction is carried by the
        entry point rather than by an argument."""
        import kiro_crew.discord.client as dc

        assert hasattr(dc, "channel_outbound_permitted")
        assert not hasattr(dc, "channel_inbound_permitted")

    @pytest.mark.asyncio
    async def test_a_withdrawal_inside_the_governance_read_still_stops_the_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The governance read is an await, so it is a wait of its own.

        A roster reading taken before it is stale by the time the caller writes, so
        answering from one would rebuild the very defect this predicate exists to
        close, one layer up. The roster is therefore read AFTER it.
        """
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        harness.revoke_during_the_governance_read()
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {}, destination=_CHANNEL)
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL
        # The second attempt was never issued, and the ceiling itself said yes.
        assert len(harness.requests) == 1
        assert harness.ceiling["open"] is True
        assert [e for e in harness.events if e[0] == "ceiling"] == [("ceiling", "discord")]
        assert harness.asked == [_CHANNEL]

    @pytest.mark.asyncio
    async def test_the_path_outranks_a_caller_that_names_a_different_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both name a destination, the PATH decides, because that is where
        the bytes go. Re-checking the caller's id instead would authorize one
        channel and write to another, so the precedence is pinned rather than left
        to whichever expression happens to come first.
        """
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"id": "2"})])
        # The roster admits only the id the CALLER named, not the path's own.
        harness.roster.clear()
        harness.roster.add("8000000001")
        result = await harness.client.api_json("POST", _SEND_PATH, {}, destination="8000000001")
        assert result.outcome == DISCORD_BLOCKED
        assert harness.asked == [_CHANNEL]

    @pytest.mark.asyncio
    async def test_the_same_holds_for_a_route_that_names_its_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both classes of route run the same body, so neither can regress alone."""
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"id": "2"})])
        harness.revoke_during_the_governance_read()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_an_unguarded_route_with_no_destination_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller that names no destination gets the prior behaviour: neither
        authority is read, so this does not become a silent new refusal path."""
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"ok": True})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _CALLBACK_PATH, {})
        assert result.outcome == DISCORD_OK
        assert harness.asked == []
        assert [e for e in harness.events if e[0] == "ceiling"] == []

    @pytest.mark.asyncio
    async def test_the_interaction_verbs_pass_their_destination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both verbs that issue an interaction route carry the destination down."""
        seen: list[tuple[str, str]] = []

        async def _api(
            method: str, path: str, payload: Any, timeout: int = 30, *, destination: str = ""
        ) -> Any:
            seen.append((path, destination))
            return {}

        client = DiscordClient(token=_TOKEN)
        monkeypatch.setattr(client, "_api", _api)
        await client.respond_interaction("i1", "t1", "hi", destination="chan-a")
        await client.ack_component_interaction("i2", "t2", destination="chan-b")
        assert [d for _, d in seen] == ["chan-a", "chan-b"]
        assert all(p.startswith("/interactions/") for p, _ in seen)

    def test_every_dispatcher_call_site_names_its_destination(self) -> None:
        """Enumerate-once gate over the dispatcher.

        The predicate can only answer for a destination it is given, so a call site
        that omits one silently restores the unguarded retry. Asserting this
        structurally covers a call site added later, which a behavioural test of
        today's seven would not. The value must come from the interaction record
        itself, which carries the channel the opaque token hides.

        Two sites are exempt, and each is identified by its own text rather than by a
        line number so the exemption cannot drift onto a different call: a refusal
        notice must not be gated by the authority it is announcing, or it can never
        be delivered in the one case it exists for. Both are ephemeral, so they
        disclose nothing into the channel.
        """
        from kiro_crew.discord import transport_dispatch as td

        source = Path(td.__file__).read_text(encoding="utf-8")
        verbs = {"respond_interaction", "ack_component_interaction"}
        exempt_markers = ("Commands run in a direct message", "disabled by policy")
        sites: list[tuple[int, str, bool]] = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) not in verbs:
                continue
            passed = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            rendered = " ".join(ast.unparse(a) for a in node.args) + " " + " ".join(passed.values())
            announces_a_refusal = any(m in rendered for m in exempt_markers)
            sites.append((node.lineno, passed.get("destination", ""), announces_a_refusal))
        assert len(sites) == 7, f"call-site count changed: {sites}"
        exempt = [ln for ln, _dest, is_refusal in sites if is_refusal]
        assert len(exempt) == 2, f"expected exactly 2 refusal notices, found {exempt}"
        # Each exempt site must also be ephemeral: that is what makes ungating it safe.
        assert source.count("ephemeral=True") >= 2
        guarded = [(ln, dest) for ln, dest, is_refusal in sites if not is_refusal]
        missing = [ln for ln, dest in guarded if not dest]
        assert missing == [], f"interaction call sites with no destination: {missing}"
        wrong = [(ln, d) for ln, d in guarded if d != "itx.channel_id"]
        assert wrong == [], f"destination not taken from the interaction record: {wrong}"
        # And the exempt ones must NOT carry a destination, or the exemption is a lie.
        leaked = [ln for ln, dest, is_refusal in sites if is_refusal and dest]
        assert leaked == [], f"a refusal notice is gated by the authority it announces: {leaked}"


# -- The refusal's own ground -----------------------------------------------


class TestRefusalNamesItsOwnGround:
    """A refusal reports WHY, because two different events end a send the same way.

    A destination an authority can place and refuses is a withdrawal. A
    destination nothing can place is one this process cannot attribute -- a
    proactive DM whose channel id came from a link written before the process
    started has no pairing, and no operator touched anything. Both stop the send,
    which is correct at an egress boundary, but reporting the second as the first
    sends whoever is debugging it after a policy change that never happened.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "wait",
        ["preemptive-hold", "429", "global-429", "5xx", "connector"],
    )
    async def test_an_unattributable_destination_is_not_reported_as_a_withdrawal(
        self, monkeypatch: pytest.MonkeyPatch, wait: str
    ) -> None:
        """Every wait site, because each one reports the refusal itself.

        A site that names the withdrawal detail directly instead of the ground the
        predicate reported looks correct from any single other site, so pinning one
        wait would leave the other four free to hardcode it.
        """
        sequences: dict[str, list[Any]] = {
            "preemptive-hold": [
                _Resp(200, {"id": "1"}, headers=_bucket_headers("b1", 0, "4.0")),
                _Resp(200, {"id": "2"}),
            ],
            "429": [_rate_limited(2.0), _Resp(200, {"id": "2"})],
            "global-429": [_rate_limited(20.0, is_global=True), _Resp(200, {"id": "2"})],
            "5xx": [_Resp(503, {}), _Resp(200, {"id": "2"})],
            "connector": [aiohttp.ClientConnectionError("boom"), _Resp(200, {"id": "2"})],
        }
        harness = _harness(monkeypatch, sequences[wait])
        if wait == "preemptive-hold":
            # This wait is only reached on the SECOND call: the first one is what
            # exhausts the bucket it then holds against.
            assert await harness.client.api_json("POST", _SEND_PATH, {})
        harness.refusal = SendPermission.unattributable()
        harness.roster.clear()

        result = await harness.client.api_json("POST", _SEND_PATH, {})

        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _UNATTRIBUTABLE_DETAIL
        assert result.detail != _REVOKED_DETAIL

    @pytest.mark.asyncio
    async def test_an_unattributable_destination_still_stops_the_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Naming the ground must not soften the answer: at an egress boundary
        "cannot tell" still reads as no."""
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.refusal = SendPermission.unattributable()
        harness.roster.clear()

        result = await harness.client.api_json("POST", _SEND_PATH, {})

        assert result.outcome == DISCORD_BLOCKED
        assert len(harness.requests) == 1, "the retry must never be issued"

    @pytest.mark.asyncio
    async def test_a_withdrawn_destination_is_still_reported_as_a_withdrawal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control. Naming the new ground must not rename the old one."""
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.refusal = SendPermission.revoked()
        harness.roster.clear()

        result = await harness.client.api_json("POST", _SEND_PATH, {})

        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL

    def test_the_transport_arms_answer_their_own_grounds(self) -> None:
        """The transport is the only place the two can be told apart, so each arm
        names its own. A paired peer the roster dropped is a withdrawal; an id no
        roster places and no pairing names is unattributable."""
        transport, client = _transport(allowed_user_ids=["u9"], allowed_channel_ids=["555666777"])

        on_roster = transport._still_may_send_to("555666777")
        assert on_roster.permitted is True
        assert on_roster.detail == ""

        client.remember_dm_recipient("dm-chan-9", "u-not-allowed")
        dropped_peer = transport._still_may_send_to("dm-chan-9")
        assert dropped_peer.permitted is False
        assert dropped_peer.detail == _REVOKED_DETAIL

        for unplaceable in ("", "999000111"):
            verdict = transport._still_may_send_to(unplaceable)
            assert verdict.permitted is False
            assert verdict.detail == _UNATTRIBUTABLE_DETAIL


# -- Fail-closed -------------------------------------------------------------


class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_a_predicate_that_raises_is_read_as_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is a network egress boundary: a predicate that cannot answer has
        not said yes."""
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])

        def _boom(channel_id: str) -> SendPermission:
            raise RuntimeError("roster unavailable")

        harness.client.still_permitted = _boom
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _UNANSWERABLE_DETAIL
        assert len(harness.requests) == 1

    @pytest.mark.asyncio
    async def test_a_check_that_could_not_run_is_not_reported_as_a_withdrawal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising predicate means NO authority answered, which is a third event.

        A roster that refused, a destination nothing could place, and a check that
        could not run send an operator to three different places. Collapsing the
        third into the first says someone withdrew a destination when the only thing
        that happened is that the check broke.
        """
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])

        def _boom(channel_id: str) -> SendPermission:
            raise RuntimeError("roster unavailable")

        harness.client.still_permitted = _boom
        result = await harness.client.api_json("POST", _SEND_PATH, {})

        assert result.outcome == DISCORD_BLOCKED
        assert result.detail != _REVOKED_DETAIL
        assert result.detail != _UNATTRIBUTABLE_DETAIL

    def test_every_refusal_ground_is_its_own_wording(self) -> None:
        """Four constructors, three distinct refusal wordings, one empty allow.

        Two grounds sharing a string would make the reported reason useless while
        every assertion about it still passed.
        """
        grounds = [
            SendPermission.revoked(),
            SendPermission.unattributable(),
            SendPermission.unanswerable(),
        ]
        assert all(not g.permitted for g in grounds)
        details = [g.detail for g in grounds]
        assert all(details)
        assert len(set(details)) == len(details), f"two grounds share a wording: {details}"
        assert SendPermission.allow().permitted is True
        assert SendPermission.allow().detail == ""

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
        assert transport._still_may_send_to("555666777").permitted is True

    def test_an_unattributable_dm_is_refused(self) -> None:
        """The roster admitting SOMEBODY is not the same as admitting this peer.

        A DM link persists the channel id while the roster holds user ids, so a
        channel this process never opened names nobody. Answering on "does the
        roster admit anybody" would let one remaining peer authorize a different,
        revoked one, so the arm refuses instead.
        """
        transport, _ = _transport(allowed_user_ids=["u1"])
        assert transport._still_may_send_to("444555666").permitted is False

    def test_an_empty_roster_refuses_every_destination(self) -> None:
        """Deny-by-default: an empty allow-list authorizes nobody, so a send in
        flight to a DM is refused on it."""
        transport, _ = _transport(allowed_user_ids=[])
        assert transport._still_may_send_to("444555666").permitted is False

    def test_a_missing_channel_id_is_refused(self) -> None:
        transport, _ = _transport(allowed_user_ids=["u1"])
        assert transport._still_may_send_to("").permitted is False

    def test_an_unseen_channel_type_is_refused_by_the_final_arm_not_a_guess(self) -> None:
        """``None`` from the cache means "not known here", so the thread arm does
        not fire; the refusal comes from the final arm having no peer to ask about.

        The pairing case below shows this is not a blanket refusal: the same
        transport permits a channel whose peer it can actually name.
        """
        transport, client = _transport(allowed_user_ids=["u1"])
        assert transport._still_may_send_to("123456789").permitted is False
        client._dm_recipients["123456789"] = "u1"
        assert transport._still_may_send_to("123456789").permitted is True

    def test_a_dm_this_process_opened_is_decided_on_its_own_peer(self) -> None:
        """The client kept the pairing when it created the channel, so the roster
        is asked about the one user the message would actually reach instead of
        whether it admits anybody.

        The roster keeps another user, so a decision made on "does the roster admit
        anybody" would answer True: the refusal can only come from the pairing.
        """
        transport, client = _transport(allowed_user_ids=["u-keeps"])
        client._dm_recipients["444555666"] = "u-revoked"
        assert transport._still_may_send_to("444555666").permitted is False

    def test_a_dm_this_process_opened_for_an_allowed_peer_passes(self) -> None:
        transport, client = _transport(allowed_user_ids=["u-keeps"])
        client._dm_recipients["444555666"] = "u-keeps"
        assert transport._still_may_send_to("444555666").permitted is True

    @pytest.mark.asyncio
    async def test_an_authorized_inbound_dm_can_be_replied_to(self) -> None:
        """An inbound DM names its peer, and the reply goes to that SAME channel
        without ever opening it.

        Driven through ``receive``, which is the wiring: recording the pairing in a
        helper the dispatcher never calls would leave the reply refused. Without the
        inbound half the fail-closed arm refuses a reply to a user who is on the
        roster and just spoke, which is a dropped reply rather than a withheld one.
        """
        transport, client = _transport(allowed_user_ids=["u1"])
        assert transport._still_may_send_to("dm-chan-1").permitted is False
        await transport.receive(
            DiscordInbound(
                channel_id="dm-chan-1",
                user_id="u1",
                username="u",
                text="hello",
                message_id="m1",
                guild_id="",
            )
        )
        assert client.cached_dm_recipient("dm-chan-1") == "u1"
        assert transport._still_may_send_to("dm-chan-1").permitted is True

    @pytest.mark.asyncio
    async def test_a_guild_message_records_no_pairing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A guild id must never acquire a DM pairing.

        This is what lets the predicate read a pairing's PRESENCE as proof that the
        id is a DM channel, and so what lets the final arm refuse everything else
        without a separate record of what was admitted before. Driven through
        ``receive`` because the gate is at the call site, not in the recorder.

        The whole guild arm runs, thread promotion included, against a stubbed
        ``_api``: leaving it unstubbed sends a live request to Discord carrying the
        bot token, and turning the promotion off instead would make the assertion
        vacuous, because the arm returns before it reaches the pairing gate at all.
        """
        transport, client = _transport(allowed_user_ids=["u1"], allowed_channel_ids=["g-chan-1"])
        calls: list[tuple[str, str]] = []

        async def _api(method: str, path: str, payload: Any = None, *_a: Any, **_k: Any) -> Any:
            calls.append((method, path))
            if method == "POST" and path.endswith("/threads"):
                return {"id": "g-thread-1"}
            if method == "GET" and path.startswith("/channels/"):
                # A public thread, so the thread-type guard admits it.
                return {"type": 11}
            return {}

        monkeypatch.setattr(client, "_api", _api)
        await transport.receive(
            DiscordInbound(
                channel_id="g-chan-1",
                user_id="u1",
                username="u",
                text="hello",
                message_id="m1",
                guild_id="guild-9",
            )
        )
        # The arm really ran: it promoted the message to a thread.
        assert ("POST", "/channels/g-chan-1/messages/m1/threads") in calls
        assert client.cached_dm_recipient("g-chan-1") is None
        assert client.cached_dm_recipient("g-thread-1") is None
        # It passes on the roster it is actually on, not on a pairing.
        assert transport._still_may_send_to("g-chan-1").permitted is True

    def test_this_file_never_hand_rolls_an_event_loop(self) -> None:
        """Structural, because the damage is invisible to the test that causes it.

        Driving a coroutine with a policy loop leaks that loop's selector fd, and
        the transport it drives is a real client: without a stubbed ``_api`` the
        guild arm opens a live session to Discord carrying the bot token and then
        burns real back-off sleeps, while the test still passes. Every coroutine here
        is awaited inside an async test instead.

        Matched on CALLS rather than on text, because the names appear in this very
        docstring and in the assertion below, so a text scan would fail against
        itself and prove nothing.
        """
        banned = {"get_event_loop_policy", "new_event_loop", "run_until_complete"}
        found: list[str] = []
        for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in banned:
                found.append(f"{name} at line {node.lineno}")
        assert found == [], f"a hand-rolled event loop is back: {found}"

    def test_every_pairing_write_is_reachable_only_for_a_direct_message(self) -> None:
        """Structural, because the guarantee is about code that does not exist yet.

        The predicate's short form is sound only while a pairing implies a DM
        channel, so a writer added outside a ``guild_id`` gate would quietly break
        the refusing final arm rather than fail a behavioural test. The write
        surface has three levels and all three are pinned: every write into the store
        funnels through ONE method, only two methods call that funnel, and every
        caller of the public one of those sits under a gate that excludes a guild.
        """
        import ast as _ast

        client_src = (_REPO_ROOT / "src/kiro_crew/discord/client.py").read_text(encoding="utf-8")
        tree = _ast.parse(client_src)
        holders: list[str] = []
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            body = _ast.dump(node)
            if (
                "_dm_recipients" in body
                and "Subscript" not in body.split("_dm_recipients")[0][-80:]
            ):
                if any(
                    isinstance(n, _ast.Call)
                    and isinstance(n.func, _ast.Name)
                    and n.func.id == "_remember"
                    for n in _ast.walk(node)
                ):
                    holders.append(node.name)
        # One funnel, so the eviction accounting cannot be bypassed by a new writer
        # either: anything that records a pairing goes through the method that also
        # notices what the cap dropped.
        assert sorted(holders) == ["_remember_dm_pairing"], holders

        funnel_callers = sorted(
            {
                fn.name
                for fn in _ast.walk(tree)
                if isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and any(
                    isinstance(n, _ast.Call)
                    and isinstance(n.func, _ast.Attribute)
                    and n.func.attr == "_remember_dm_pairing"
                    for n in _ast.walk(fn)
                )
            }
        )
        # A write can only happen where Discord itself has just named the peer, or
        # through the one public method whose callers are checked below.
        assert funnel_callers == ["create_dm_channel", "remember_dm_recipient"], funnel_callers

        for path in (
            _REPO_ROOT / "src/kiro_crew/discord/transport.py",
            _REPO_ROOT / "src/kiro_crew/discord/transport_dispatch.py",
        ):
            src = path.read_text(encoding="utf-8")
            t = _ast.parse(src)
            parents: dict[_ast.AST, _ast.AST] = {}
            for node in _ast.walk(t):
                for child in _ast.iter_child_nodes(node):
                    parents[child] = node
            sites = 0
            for node in _ast.walk(t):
                if not (
                    isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Attribute)
                    and node.func.attr == "remember_dm_recipient"
                ):
                    continue
                sites += 1
                guarded = False
                cur: _ast.AST | None = node
                while cur is not None and not isinstance(cur, _ast.Module):
                    parent = parents.get(cur)
                    if isinstance(parent, _ast.If) and cur in parent.body:
                        test = _ast.dump(parent.test)
                        if "guild_id" in test and "Not()" in test:
                            guarded = True
                    cur = parent
                assert guarded, f"{path.name}:{node.lineno} writes a pairing with no DM gate"
            assert sites == 1, f"{path.name}: expected 1 pairing writer, found {sites}"

    @pytest.mark.asyncio
    async def test_an_unauthorized_inbound_dm_records_no_pairing(self) -> None:
        """The record is made on the AUTHORIZED path only, so a denied sender
        cannot plant a pairing that would answer for their channel later."""
        transport, client = _transport(allowed_user_ids=["u1"])
        await transport.receive(
            DiscordInbound(
                channel_id="dm-chan-2",
                user_id="intruder",
                username="x",
                text="hello",
                message_id="m2",
                guild_id="",
            )
        )
        assert client.cached_dm_recipient("dm-chan-2") is None

    def test_an_inbound_pairing_still_answers_for_the_peer_not_the_channel(self) -> None:
        """Recording the pairing grants nothing on its own: the roster still
        decides, so a peer removed after speaking is refused."""
        transport, client = _transport(allowed_user_ids=["u1"])
        client.remember_dm_recipient("dm-chan-1", "u-revoked")
        assert transport._still_may_send_to("dm-chan-1").permitted is False

    def test_a_shared_channel_on_the_roster_is_still_permitted(self) -> None:
        transport, _ = _transport(allowed_user_ids=["u1"], allowed_channel_ids=["777888999"])
        assert transport._still_may_send_to("777888999").permitted is True

    def test_a_reclassified_id_is_not_read_as_a_withdrawal(self) -> None:
        """An operator who mislabels a shared channel in ``allowed_thread_ids`` and
        then corrects it into ``allowed_channel_ids`` has reclassified it, not
        withdrawn it.

        The admitted-thread set grows only, so consulting it before the current
        channel roster would refuse that id on every waited send -- including the
        thread promotion, whose failure drops the user's message with no reply.
        Both current rosters are therefore read before any history.
        """
        transport, _ = _transport(allowed_user_ids=["u1"], allowed_thread_ids=["777888999"])
        transport.reconfigure(
            SimpleNamespace(allowed_thread_ids=[], allowed_channel_ids=["777888999"])
        )
        assert transport._still_may_send_to("777888999").permitted is True

    def test_a_reloaded_channel_roster_keeps_admitting_what_it_has_seen(self) -> None:
        """The admitted set grows across a reload, so a channel added by config and
        later withdrawn is still distinguishable from an id never seen."""
        transport, _ = _transport(allowed_user_ids=["u1"], allowed_channel_ids=["777888999"])
        transport.reconfigure(SimpleNamespace(allowed_channel_ids=["101112131"]))
        assert transport._still_may_send_to("101112131").permitted is True
        assert transport._still_may_send_to("777888999").permitted is False


# -- The refusal is audited --------------------------------------------------


class _Sel:
    """Captures ``log_api_access`` calls, standing in for the SEL singleton."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class TestRefusalsAreAudited:
    """A mid-send refusal suppresses an already-composed, user-visible message.

    The ladder's result carries the decision no further than its caller, so the
    SEL row is the record it leaves -- matching ``may_send_to``, ``authorize``
    and the inbound ceiling, which all record theirs.
    """

    @pytest.mark.asyncio
    async def test_a_roster_refusal_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert [c["outcome"] for c in recorder.calls] == ["denied"]
        assert recorder.calls[0]["operation"].endswith(".roster")
        assert recorder.calls[0]["caller"] == _CHANNEL
        assert recorder.calls[0]["source"] == "discord"

    @pytest.mark.asyncio
    async def test_a_ceiling_refusal_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait(ceiling=True)
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert [c["outcome"] for c in recorder.calls] == ["denied"]
        assert recorder.calls[0]["operation"].endswith(".channels_ceiling")

    @pytest.mark.asyncio
    async def test_a_predicate_that_raises_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])

        def _boom(channel_id: str) -> SendPermission:
            raise RuntimeError("roster unreadable")

        harness.client.still_permitted = _boom
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert recorder.calls[0]["operation"].endswith(".predicate_raised")

    @pytest.mark.asyncio
    async def test_an_unwritable_audit_store_does_not_break_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal already stands, so letting the audit raise here would turn
        every throttled send on a governed install into an exception."""

        class _Broken:
            def log_api_access(self, **kwargs: Any) -> None:
                raise OSError("audit store unwritable")

        monkeypatch.setattr(dc, "sel", lambda: _Broken())
        harness = _harness(monkeypatch, [_rate_limited(2.0), _Resp(200, {"id": "2"})])
        harness.revoke_during_next_wait()
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_BLOCKED
        assert result.detail == _REVOKED_DETAIL

    @pytest.mark.asyncio
    async def test_a_send_that_is_never_refused_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A send that never waits pays nothing: the rows are paced by the ladder's
        own waits, not by traffic."""
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        harness = _harness(monkeypatch, [_Resp(200, {"id": "2"})])
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_OK
        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_a_post_wait_allow_is_recorded_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The allow is a permission decision as much as the refusal is, and the
        ladder's result carries neither to anyone who could record it."""
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        harness = _harness(monkeypatch, [_rate_limited(1.0), _Resp(200, {"id": "2"})])
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_OK
        assert [c["outcome"] for c in recorder.calls] == ["allowed"]
        assert recorder.calls[0]["operation"].endswith(".roster")

    @pytest.mark.asyncio
    async def test_the_ceiling_is_the_recorded_authority_with_no_predicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        harness = _harness(
            monkeypatch, [_rate_limited(1.0), _Resp(200, {"id": "2"})], wire_predicate=False
        )
        result = await harness.client.api_json("POST", _SEND_PATH, {})
        assert result.outcome == DISCORD_OK
        assert [c["outcome"] for c in recorder.calls] == ["allowed"]
        assert recorder.calls[0]["operation"].endswith(".channels_ceiling")


# -- Every retained field is bounded -----------------------------------------


class TestRetentionIsBounded:
    """Both stores the predicate reads grow with traffic, so both are capped.

    Dropping history may only ever cost a send, never authorize one, so the
    overflow makes the final fall-through refuse instead of trimming silently.
    """

    @pytest.mark.asyncio
    async def test_the_dm_pairing_store_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Driven through ``create_dm_channel``, which is the retention site: a cap
        on the helper alone would not prove this one uses it."""
        _, client = _transport(allowed_user_ids=["u1"])

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": f"chan-{payload['recipient_id']}"}

        monkeypatch.setattr(client, "_api", _api)
        last = dc._MAX_TRACKED_DM_PEERS + 49
        for i in range(dc._MAX_TRACKED_DM_PEERS + 50):
            assert await client.create_dm_channel(f"u{i}") == f"chan-u{i}"
        assert len(client._dm_recipients) == dc._MAX_TRACKED_DM_PEERS
        # Least-recently-used eviction: the oldest pairings went, the newest stayed.
        assert client.cached_dm_recipient("chan-u0") is None
        assert client.cached_dm_recipient(f"chan-u{last}") == f"u{last}"

    @pytest.mark.asyncio
    async def test_an_over_long_channel_id_is_never_paired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = _transport(allowed_user_ids=["u1"])
        huge = "9" * 4096

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": huge}

        monkeypatch.setattr(client, "_api", _api)
        assert await client.create_dm_channel("u1") == huge
        assert client.cached_dm_recipient(huge) is None

    @pytest.mark.asyncio
    async def test_reading_a_pairing_keeps_it_from_ageing_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store is least-recently-used, so a pairing that is only ever READ
        would age out while it is still in active use, and the next waited send to
        that channel would be refused for want of a peer it had."""
        _, client = _transport(allowed_user_ids=["u1"])

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": f"chan-{payload['recipient_id']}"}

        monkeypatch.setattr(client, "_api", _api)
        await client.create_dm_channel("keepme")
        # Fill the store past its cap, reading the first pairing on every step.
        for i in range(dc._MAX_TRACKED_DM_PEERS + 5):
            assert client.cached_dm_recipient("chan-keepme") == "keepme"
            await client.create_dm_channel(f"filler{i}")
        assert client.cached_dm_recipient("chan-keepme") == "keepme"

    def test_a_refusal_with_intact_history_does_not_claim_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: without an overflow the refusal carries no truncation row,
        so the reason means something when it does appear."""
        transport, _ = _transport(allowed_user_ids=["u1"])
        recorder = _Sel()
        monkeypatch.setattr(dt, "sel", lambda: recorder)
        assert transport._still_may_send_to("444555666").permitted is False
        reasons = [c["operation"] for c in recorder.calls]
        assert not any("truncation" in r for r in reasons), reasons

    @pytest.mark.asyncio
    async def test_reaching_the_pairing_cap_is_counted_and_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cap reached in silence looks exactly like a roster the operator shrank.

        The count and the row are what let whoever is reading the log see that this
        process forgot a peer, rather than inferring a policy change that never
        happened.
        """
        _, client = _transport(allowed_user_ids=["u1"])
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": f"chan-{payload['recipient_id']}"}

        monkeypatch.setattr(client, "_api", _api)
        for i in range(dc._MAX_TRACKED_DM_PEERS + 3):
            await client.create_dm_channel(f"u{i}")
        assert client._dm_pairings_evicted == 3
        evicted_rows = [c for c in recorder.calls if "pairing_evicted" in c["operation"]]
        assert len(evicted_rows) == 3
        # The row names WHICH channel was forgotten, which is what makes a later
        # refusal of that channel attributable at all.
        assert [r["caller"] for r in evicted_rows] == ["chan-u0", "chan-u1", "chan-u2"]

    @pytest.mark.asyncio
    async def test_a_refusal_after_the_cap_dropped_the_pairing_names_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Paired with the control above. The send is refused either way -- the point
        is that the audit says which event refused it."""
        _, client = _transport(allowed_user_ids=["u0"])

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": f"chan-{payload['recipient_id']}"}

        monkeypatch.setattr(client, "_api", _api)
        for i in range(dc._MAX_TRACKED_DM_PEERS + 1):
            await client.create_dm_channel(f"u{i}")
        assert "chan-u0" in client._evicted_dm_channels
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        assert client._roster_still_permits("chan-u0").permitted is False
        reasons = [c["operation"] for c in recorder.calls]
        assert any("pairing_truncated" in r for r in reasons), reasons
        assert not any(r.endswith(".roster") for r in reasons), reasons

    @pytest.mark.asyncio
    async def test_re_learning_a_pairing_clears_its_truncation_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise a genuine withdrawal after a re-learn is reported as a cap
        symptom forever, which is the same mislabel in the other direction."""
        _, client = _transport(allowed_user_ids=["u0"])

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": f"chan-{payload['recipient_id']}"}

        monkeypatch.setattr(client, "_api", _api)
        for i in range(dc._MAX_TRACKED_DM_PEERS + 1):
            await client.create_dm_channel(f"u{i}")
        assert "chan-u0" in client._evicted_dm_channels
        client.remember_dm_recipient("chan-u0", "u0")
        assert "chan-u0" not in client._evicted_dm_channels
        recorder = _Sel()
        monkeypatch.setattr(dc, "sel", lambda: recorder)
        # Still authorized, so this one is an allow -- and it is not a truncation.
        assert client._roster_still_permits("chan-u0").permitted is True
        reasons = [c["operation"] for c in recorder.calls]
        assert not any("pairing_truncated" in r for r in reasons), reasons

    @pytest.mark.asyncio
    async def test_the_evicted_record_is_itself_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It retains external ids, so it needs its own named cap -- and it must be
        far smaller than the store it accounts for, since it decides no send.

        Driven through ``create_dm_channel`` rather than the bounded-map helper: a cap
        passed at the helper is the thing under test, so calling the helper directly
        would prove only that the helper honours whatever it is handed.
        """
        assert dc._MAX_EVICTED_DM_CHANNELS < dc._MAX_TRACKED_DM_PEERS
        _, client = _transport(allowed_user_ids=["u1"])

        async def _api(method: str, path: str, payload: Any) -> Any:
            return {"id": f"chan-{payload['recipient_id']}"}

        monkeypatch.setattr(client, "_api", _api)
        evictions = dc._MAX_EVICTED_DM_CHANNELS + 20
        for i in range(dc._MAX_TRACKED_DM_PEERS + evictions):
            await client.create_dm_channel(f"u{i}")
        assert client._dm_pairings_evicted == evictions
        assert len(client._evicted_dm_channels) == dc._MAX_EVICTED_DM_CHANNELS
        # The count keeps rising after the record stops growing, so the total is not
        # silently capped along with it.
        assert client._dm_pairings_evicted > dc._MAX_EVICTED_DM_CHANNELS

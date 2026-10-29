"""How a client's cancel is tracked, and whom a final unsubscribe's identity release is left to.

These tests pin the cancel branch of ``Backend.forward_from_stub`` and the identity release hold of
the lease broker. A cancelled tool call stays pending and marked, so the hard wedge ceiling skips
it while the two-condition rule still counts it. A cancelled ``resources/subscribe`` or
``resources/unsubscribe`` is not forwarded: the server answers it, and the verdict settles the lease
under the stub that sent it. On an identity-capable server a final unsubscribe leaves its release to
a same-caller subscribe still in flight, or to a routed stub of that caller
(``_hold_identity_release``): the subscribe inherits the release (``release_owed``), a grant that
routes a stub discharges it, and any other verdict sends it as the caller unless the rule still
holds, whichever of several subscribes answers first and also when the subscribe was retracted or
its stub detached. The shared fixture and helpers live in
``mcp_gateway_backend_coverage_helpers.py``, and ``test_mcp_gateway_backend_coverage.py`` keeps the
rest of ``forward_from_stub``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import (
    _drain,
    _frames,
    _line,
    _make_backend,
    _settle_lease,
)

from kiro_crew.mcp_caller import CALLER_META_KEY, CallerContext
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import Backend


def _releases(backend: Backend) -> list[dict]:
    """The gateway's own ``resources/unsubscribe`` frames, in the order written."""
    return [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]


def _fid_of(backend: Backend, stub: str, *, origin: str = "") -> str:
    """The forward id of the lease request ``stub`` owns (for the release
    sentinel, the one it took over from ``origin``)."""
    return next(
        f
        for f, p in backend._pending_requests.items()
        if p.resource_uri and p.stub_uuid == stub and (not origin or p.origin_stub == origin)
    )


async def _answer(backend: Backend, fid: str, *, refuse: bool = False) -> None:
    """Answer one forwarded request with the server's verdict."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": fid}
    if refuse:
        msg["error"] = {"code": -32603, "message": "denied"}
    else:
        msg["result"] = {}
    await backend._route_backend_line(_line(msg))


class TestForwardFromStubCancel:
    """A stub's ``notifications/cancelled`` for a request it still has in flight.

    A tool call is marked and stays pending. A subscribe or unsubscribe is not forwarded, so the
    server's verdict settles its lease under the stub that sent it. ``TestForwardFromStub`` in
    ``test_mcp_gateway_backend_coverage.py`` pins the rest of ``forward_from_stub``.
    """

    @pytest.mark.asyncio
    async def test_a_hung_backend_whose_only_call_was_cancelled_is_recycled(self) -> None:
        """s1 cancels its only call, then the backend stops answering pings. The
        cancelled call still says a request is outstanding, so the two-condition
        rule recycles the backend as it would for any other old request."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {"method": "tools/call", "id": 5})
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 5},
            },
        )
        now = time.monotonic() + backend_mod.HEARTBEAT_TIMEOUT_SECS + 1
        backend._last_ping_response_mono = now - backend_mod.PING_STALE_SECS - 1
        assert await backend._heartbeat_once(now) == "wedged"
        assert "ping stale" in backend._dead_reason

    @pytest.mark.asyncio
    async def test_a_cancelled_subscribe_does_not_age_a_healthy_backend_into_the_ceiling(
        self,
    ) -> None:
        """s1 takes a URI's lease with a subscribe, s2 parks behind it, and s1
        cancels. The cancel is not forwarded, so the server answers the subscribe
        (one honouring MCP cancellation answers only what it was not asked to
        cancel). The verdict settles the lease, so a backend still answering
        pings is not recycled past the hard ceiling."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id in (("s1", 1), ("s2", 9)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///shared.txt"},
                },
            )
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            },
        )
        cancelled = {
            f["params"]["requestId"]
            for f in _frames(backend)
            if f.get("method") == "notifications/cancelled"
        }
        assert cancelled == set()
        for frame in _frames(backend):
            if frame.get("method") == "resources/subscribe" and frame["id"] not in cancelled:
                await backend._route_backend_line(
                    _line({"jsonrpc": "2.0", "id": frame["id"], "result": {}})
                )
        later = time.monotonic() + backend_mod.HARD_WEDGE_CEILING_SECS + 1
        backend._last_ping_response_mono = later
        assert await backend._heartbeat_once(later) == "alive"
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 9, "result": {}}
        assert backend._pending_requests == {}
        assert backend._lease_awaiting_grant == set()

    @pytest.mark.asyncio
    async def test_a_cancelled_subscribe_the_server_never_answers_still_ages_out(
        self,
    ) -> None:
        """A server that never answers the lease-taking subscribe still trips the
        wedge ceiling after its client cancels: the entry stays under its stub,
        so the recycle answers the rider parked behind it."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id in (("s1", 1), ("s2", 9)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///shared.txt"},
                },
            )
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            },
        )
        assert [p.stub_uuid for p in backend._pending_requests.values()] == ["s1"]
        later = time.monotonic() + backend_mod.HARD_WEDGE_CEILING_SECS + 1
        backend._last_ping_response_mono = later
        assert await backend._heartbeat_once(later) == "wedged"
        verdict = await _drain(inbox2)
        assert verdict["id"] == 9 and "error" in verdict

    @pytest.mark.asyncio
    async def test_a_cancelled_final_unsubscribe_settles_the_release_under_its_stub(
        self,
    ) -> None:
        """A cancelled final unsubscribe is not dropped and its cancel is not
        forwarded: the server's answer settles the release, and the stub is no
        longer routed for the URI."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await _settle_lease(backend)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
        )
        assert [f for f in _frames(backend) if f.get("method") == "notifications/cancelled"] == []
        await _settle_lease(backend)
        assert backend._pending_requests == {}
        assert backend._resource_subscriptions == {}
        assert backend._lease_awaiting_release == set()

    @pytest.mark.asyncio
    async def test_an_identity_cancelled_subscribe_is_settled_by_its_own_verdict(
        self,
    ) -> None:
        """On an identity server a cancelled subscribe is answered by the server,
        not cancelled upstream; its grant routes the stub as the caller that took
        it, and no release is sent."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
            caller=caller_a,
        )
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            },
            caller=caller_a,
        )
        assert [f for f in _frames(backend) if f.get("method") == "notifications/cancelled"] == []
        await _settle_lease(backend)
        assert backend._pending_requests == {}
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        assert backend._grant_callers == {("file:///watched.txt", "s1"): caller_a}
        assert [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"] == []


class TestIdentityReleaseHold:
    """``_hold_identity_release``: whom a final unsubscribe's upstream release is left to.

    On an identity-capable server the release is held while another stub of the caller is routed
    for the URI or a same-caller subscribe is in flight. That subscribe inherits it, a grant that
    routes a stub discharges it, and any other verdict sends it unless the rule still holds.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("granted", [True, False], ids=["granted", "refused"])
    async def test_a_last_holder_unsubscribe_waits_for_a_same_caller_subscribe(
        self, granted: bool
    ) -> None:
        """s1 holds a URI as caller A, s2's subscribe as A is in flight, and s1
        unsubscribes and cancels. Forwarding s1's release would let the server
        grant s2 and then drop A's one subscription, leaving s2 routed to
        nothing. The release is left to s2's verdict: a grant needs none (A's
        subscription serves s2), a refusal sends exactly one, as A."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        uri = "file:///watched.txt"
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "resources/subscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
            caller=caller_a,
        )

        def _releases() -> list[dict]:
            return [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]

        assert _releases() == []
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        assert backend._resource_subscriptions.get(uri, set()) == set()
        await _settle_lease(
            backend, error=None if granted else {"code": -32603, "message": "denied"}
        )
        assert (await _drain(inbox2))["id"] == 9
        if granted:
            assert _releases() == []
            assert backend._resource_subscriptions == {uri: {"s2"}}
            assert backend._grant_callers == {(uri, "s2"): caller_a}
        else:
            assert len(_releases()) == 1
            assert CALLER_META_KEY in _releases()[0]["params"]["_meta"]
            assert backend._resource_subscriptions.get(uri, set()) == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answers",
        [
            (("s3", "granted"), ("s2", "refused")),
            (("s2", "refused"), ("s3", "granted")),
            (("s2", "refused"), ("s3", "refused")),
        ],
        ids=["s3-granted-then-s2-refused", "s2-refused-then-s3-granted", "both-refused"],
    )
    async def test_a_release_left_to_one_of_two_same_caller_subscribes_spares_the_other(
        self, answers: tuple[tuple[str, str], tuple[str, str]]
    ) -> None:
        """s1 holds a URI as caller A while s2 and s3 both have a subscribe as A
        in flight, and s1 unsubscribes. Whichever subscribe answers first, a
        release is sent only when neither ends up granted: a granted sibling
        holds A's one subscription, and a refused one hands the release on to
        the subscribe still in flight."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inboxes = {stub: await backend.attach_stub(stub) for stub in ("s2", "s3")}
        caller_a = CallerContext(session_key="dashboard:aaa")
        uri = "file:///watched.txt"
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await _settle_lease(backend)
        await _drain(inbox1)
        for stub, req_id in (("s2", 9), ("s3", 7)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": uri},
                },
                caller=caller_a,
            )
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        assert _releases(backend) == []
        granted: set[str] = set()
        for stub, verdict in answers:
            await _answer(backend, _fid_of(backend, stub), refuse=verdict == "refused")
            reply = await _drain(inboxes[stub])
            assert ("error" in reply) is (verdict == "refused")
            if verdict == "granted":
                granted.add(stub)
        if granted:
            assert _releases(backend) == []
            assert backend._resource_subscriptions == {uri: granted}
            assert backend._grant_callers == {(uri, stub): caller_a for stub in granted}
        else:
            assert len(_releases(backend)) == 1
            assert _releases(backend)[0]["params"]["_meta"][CALLER_META_KEY]["sessionKey"] == (
                "dashboard:aaa"
            )
            assert backend._resource_subscriptions.get(uri, set()) == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refused", [False, True], ids=["granted", "refused"])
    async def test_a_release_left_to_a_detached_stubs_subscribe_spares_a_routed_sibling(
        self, refused: bool
    ) -> None:
        """The subscribe that inherited s1's release belongs to s2, which detaches
        before the server answers; s3, also as caller A, is granted meanwhile.
        s2's verdict then settles under the release sentinel and must not
        release A's subscription, which s3 holds."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        inbox3 = await backend.attach_stub("s3")
        caller_a = CallerContext(session_key="dashboard:aaa")
        uri = "file:///watched.txt"
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await _settle_lease(backend)
        await _drain(inbox1)
        for stub, req_id in (("s2", 9), ("s3", 7)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": uri},
                },
                caller=caller_a,
            )
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await _drain(inbox1)
        await backend.detach_stub("s2")
        await _answer(backend, _fid_of(backend, "s3"))
        assert await _drain(inbox3) == {"jsonrpc": "2.0", "id": 7, "result": {}}
        await _answer(
            backend,
            _fid_of(backend, backend_mod._RELEASE_STUB_SENTINEL, origin="s2"),
            refuse=refused,
        )
        assert _releases(backend) == []
        assert backend._resource_subscriptions == {uri: {"s3"}}
        assert backend._grant_callers == {(uri, "s3"): caller_a}
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_a_retracted_subscribe_carrying_the_release_settles_it_beside_another_caller(
        self,
    ) -> None:
        """s1 holds a URI as caller A and re-subscribes it; sb holds the same URI
        as caller B. s1 unsubscribes while its re-subscribe is in flight, so the
        retracted subscribe inherits the release. Its grant must release A's
        subscription even though sb keeps the URI routed: sb's subscription is
        B's, not A's."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inboxb = await backend.attach_stub("sb")
        caller_a = CallerContext(session_key="dashboard:aaa")
        caller_b = CallerContext(session_key="dashboard:bbb")
        uri = "file:///watched.txt"
        for stub, req_id, caller in (("s1", 1, caller_a), ("sb", 5, caller_b)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": uri},
                },
                caller=caller,
            )
            await _settle_lease(backend)
        await _drain(inbox1)
        await _drain(inboxb)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": uri},
            },
            caller=caller_a,
        )
        assert "error" in await _drain(inbox1)  # the re-subscribe, retracted
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        assert _releases(backend) == []
        await _answer(backend, _fid_of(backend, backend_mod._RELEASE_STUB_SENTINEL, origin="s1"))
        assert len(_releases(backend)) == 1
        assert _releases(backend)[0]["params"]["_meta"][CALLER_META_KEY]["sessionKey"] == (
            "dashboard:aaa"
        )
        assert backend._resource_subscriptions == {uri: {"sb"}}
        assert backend._grant_callers == {(uri, "sb"): caller_b}
        assert inboxb.empty()

    @pytest.mark.asyncio
    async def test_an_update_while_the_held_release_waits_is_not_a_hazard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """s1 holds a URI as caller A, s2's subscribe as A is in flight, and s1
        unsubscribes, so routing for the URI empties while A's subscription is
        still live upstream. An update the server sends before s2's verdict
        routes nowhere, and is dropped without charging the server a hazard.
        A refusal of s2's subscribe sends the held release, whose success
        clears the orphan mark; an update after that is a hazard again."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        uri = "file:///watched.txt"
        await backend.forward_from_stub(
            "s1",
            {"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe", "params": {"uri": uri}},
            caller=caller_a,
        )
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s2",
            {"jsonrpc": "2.0", "id": 9, "method": "resources/subscribe", "params": {"uri": uri}},
            caller=caller_a,
        )
        await backend.forward_from_stub(
            "s1",
            {"jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe", "params": {"uri": uri}},
            caller=caller_a,
        )
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        assert uri not in backend._resource_subscriptions
        assert uri in backend._orphaned_leases
        update = {"method": "notifications/resources/updated", "params": {"uri": uri}}
        await backend._route_backend_line(_line(update))
        assert inbox1.empty() and inbox2.empty()
        assert recorded == []
        await _answer(backend, _fid_of(backend, "s2"), refuse=True)
        assert (await _drain(inbox2))["id"] == 9
        assert len(_releases(backend)) == 1
        await _answer(backend, _releases(backend)[0]["id"])
        assert uri not in backend._orphaned_leases
        await backend._route_backend_line(_line(update))
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

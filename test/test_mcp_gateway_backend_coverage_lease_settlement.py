"""How the lease broker settles each subscribe, unsubscribe and replay a response answers.

These tests pin the lease broker without caller identity, mostly on its response path: a malformed
subscribe or replay verdict is never a grant and releases the lease it may have taken, while a
settled refusal releases nothing, and a malformed release keeps routing; only one final release is
in flight per URI, with repeat unsubscribes parked as waiters that settle on its verdict (a detached
waiter pruned) and co-tenant subscribes parked as replacements, which a refused release joins
locally and a confirmed one drains into a fresh grant; a parking survives its releaser's detach, an
unsubscribe retracts it, and a release the gateway originates when the last subscriber detaches
parks a replacement the same way; every routing and pending-owner change commits before a reply is
awaited, so a reply that detaches its stub (``_fill_inbox`` fills the inbox that trips the detach)
neither leaves that stub routed nor strands a surviving rider, and a departing stub's own rider is
never promoted; respawn replays coalesce on one lease, join a held one or park behind a release, and
a replay write failure raises and scopes the URIs already sent to release on grant; the orphan-drop,
replay-failure and release-failure log lines omit the URI; gateway-originated release and replay
pendings carry a start time; id-less subscribe and unsubscribe frames are swallowed; release waiters
count toward the per-stub cap, the orphaned-lease set is bounded and an overlong URI is refused
before any table stores it; and a backend death answers every parked rider, waiter and replacement.
A topic of the ``test_mcp_gateway_backend_coverage`` family:
``test_mcp_gateway_backend_coverage.py`` keeps ``forward_from_stub`` and inbox delivery, the other
siblings are ``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py``,
``test_mcp_gateway_backend_coverage_mcp_apps.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``, and the shared fixture and helpers live
in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import (
    _drain,
    _frames,
    _line,
    _make_backend,
    _settle_lease,
)

from kiro_crew.mcp_gateway import backend as backend_mod

# --- subscription response hardening ----------------------------------------


def _fill_inbox(inbox: "asyncio.Queue[bytes]") -> None:
    """Fill a stub's inbox to its cap so the NEXT delivery into it trips
    the slow-stub guard and detaches the stub mid-reply."""
    while True:
        try:
            inbox.put_nowait(b"x")
        except asyncio.QueueFull:
            return


class TestSubscriptionResponseHardeningLeaseSettlement:
    """Response-path invariants for the lease broker: a malformed response
    is never a grant, only one final release is in flight per URI, and every
    routing / pending-owner transition is committed BEFORE a reply is
    awaited (a reply can detach its stub, which prunes the very tables a
    late commit would then re-decide).

    This class drives the broker without caller identity; its sibling topic class
    ``TestSubscriptionResponseHardeningCallerGrantsAndEviction``, in
    ``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``, holds the per-caller grants of
    an identity-capable server, the rekey eviction of a claimed stub and the respawn capture of
    in-flight replay URIs.
    """

    @pytest.mark.asyncio
    async def test_malformed_subscribe_response_grants_nothing(self) -> None:
        """A response with neither ``result`` nor ``error`` is not a grant:
        routing on it would start delivering updates on a verdict the server
        never settled. The forwarder still receives the raw frame; the
        parked rider is refused, not granted."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {}
        # An UNSETTLED verdict proves nothing: the subscribe may have taken,
        # so the possibly-live lease is released rather than stranded
        # upstream firing updates that get charged to the server as hazards.
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert (await _drain(inbox1))["id"] == 1  # raw frame, no grant
        refusal = await _drain(inbox2)
        assert refusal["id"] == 2 and "error" in refusal
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert inbox1.empty() and inbox2.empty()

    @pytest.mark.asyncio
    async def test_malformed_replay_response_grants_nothing(self) -> None:
        """The sentinel-owned respawn replay applies the same success gate:
        a frame with neither ``result`` nor ``error`` grants no routing —
        and the possibly-live lease is released, not stranded."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_refused_subscribe_response_does_not_release(self) -> None:
        """A settled REFUSAL proves the server holds no lease, so no
        gateway-originated release is issued for it."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        await _drain(inbox1)
        assert backend._resource_subscriptions == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert releases == []

    @pytest.mark.asyncio
    async def test_malformed_unsubscribe_response_keeps_routing(self) -> None:
        """A malformed release response is treated as a refusal: the entry
        is kept (fail closed) instead of assuming the server released."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
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
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        assert "file:///watched.txt" not in backend._lease_awaiting_release

    @pytest.mark.asyncio
    async def test_repeat_final_unsubscribe_parks_behind_inflight_release(
        self,
    ) -> None:
        """A second final unsubscribe while the first's release is in flight
        is PARKED, not forwarded: forwarding it would race two responses
        against one shared awaiting-release flag, and a refusal+success pair
        would leave local routing granting a lease the server has released.
        Every waiter settles on the one in-flight release's verdict."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
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
        await _drain(inbox1)
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
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        assert inbox1.empty()  # parked, not answered early
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(upstream) == 1
        # The one in-flight release settles as a refusal: the server kept
        # the lease, routing keeps the stub, the waiter is told the truth,
        # and the flag is down so a later retry can release cleanly.
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        # The waiter's local reply lands first; the forwarder's raw server
        # frame is delivered afterwards by the routing path.
        waiter_reply = await _drain(inbox1)
        assert waiter_reply["id"] == 3 and "error" in waiter_reply
        raw = await _drain(inbox1)
        assert raw["id"] == 2 and "error" in raw
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        assert "file:///watched.txt" not in backend._lease_awaiting_release
        assert backend._lease_release_waiters == {}

    @pytest.mark.asyncio
    async def test_release_waiter_settles_on_success(self) -> None:
        """A parked final unsubscribe is answered with success — and its
        stub dropped from routing — when the in-flight release confirms."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
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
        await _drain(inbox1)
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
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await _settle_lease(backend)  # release confirmed
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        raw = await _drain(inbox1)
        assert raw["id"] == 2 and "result" in raw
        assert backend._resource_subscriptions == {}
        assert backend._lease_release_waiters == {}

    @pytest.mark.asyncio
    async def test_cotenant_final_unsubscribe_survives_releasers_detach(
        self,
    ) -> None:
        """A co-tenant subscribing while another stub's final release is in
        flight parks as a replacement; the releaser detaching (its release
        becomes sentinel-owned) must not strand that parking. The settled
        release drains it into a fresh grant, and the co-tenant's own later
        final unsubscribe releases cleanly."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
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
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # final release forwarded, in flight
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # parks as a replacement behind the release
        assert backend._lease_replacement_subscribes == {"file:///watched.txt": [("s2", 3)]}
        await backend.detach_stub("s1")  # release becomes sentinel-owned
        assert backend._lease_replacement_subscribes == {
            "file:///watched.txt": [("s2", 3)]
        }  # parking survives
        await _settle_lease(backend)  # the release confirms; drain forwards
        await _settle_lease(backend)  # the fresh subscribe is granted
        assert (await _drain(inbox2))["id"] == 3
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # s2's own final release forwards normally
        await _settle_lease(backend)
        assert (await _drain(inbox2))["id"] == 4
        assert backend._resource_subscriptions == {}
        assert backend._lease_release_waiters == {}
        assert backend._lease_replacement_subscribes == {}

    @pytest.mark.asyncio
    async def test_detached_waiter_is_pruned_not_replied(self) -> None:
        """A waiter that detaches while parked expects no further reply and
        must not linger in the waiter table."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
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
        await _drain(inbox1)
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
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # parked
        await backend.detach_stub("s1")
        assert backend._lease_release_waiters == {}
        await _settle_lease(backend)  # sentinel-owned release settles cleanly
        assert backend._resource_subscriptions == {}

    @pytest.mark.asyncio
    async def test_sentinel_grant_commits_routing_before_rider_replies(
        self,
    ) -> None:
        """A rider whose grant reply detaches it (full inbox) must not be
        reinserted into the routing table by a commit made after the
        replies — updates would then route at a stub that is gone."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        full_inbox = await backend.attach_stub("full")
        ok_inbox = await backend.attach_stub("ok")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # retract: the in-flight grant is now sentinel-owned
        cancelled = await _drain(inbox1)
        assert cancelled["id"] == 1 and "error" in cancelled
        assert (await _drain(inbox1))["id"] == 2  # local retract success
        await backend.forward_from_stub(
            "full",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.forward_from_stub(
            "ok",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        _fill_inbox(full_inbox)
        await _settle_lease(backend)  # grant arrives under the sentinel
        assert "full" not in backend._stub_inboxes  # detached mid-reply
        assert backend._resource_subscriptions == {"file:///watched.txt": {"ok"}}
        assert (await _drain(ok_inbox))["id"] == 4
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_grant_commits_every_rider_before_replies(self) -> None:
        """The coalesced grant commits the forwarder AND every rider before
        any reply: a mid-loop detach that empties the entry would otherwise
        delete it from the table, stranding later riders in a stale set alias
        that routes nowhere."""
        backend = _make_backend()
        s1_inbox = await backend.attach_stub("s1")
        s2_inbox = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # s1 rides behind its own grant
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        _fill_inbox(s1_inbox)
        await _settle_lease(backend)  # s1's rider reply detaches s1
        assert "s1" not in backend._stub_inboxes
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        assert await _drain(s2_inbox) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert (await _drain(s2_inbox))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_retract_commits_promotion_before_replies(self) -> None:
        """The forwarder's retract reply can detach the retracting stub
        (full inbox). The surviving rider's promotion must already be
        committed, or detach's own promotion is overwritten with the
        sentinel and that rider's subscribe hangs unanswered forever."""
        backend = _make_backend()
        s1_inbox = await backend.attach_stub("s1")
        s2_inbox = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # rider behind s1's in-flight grant
        _fill_inbox(s1_inbox)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # retract; the error reply into the full inbox detaches s1
        assert "s1" not in backend._stub_inboxes
        p = next(p for p in backend._pending_requests.values() if p.resource_uri)
        assert p.stub_uuid == "s2" and p.original_id == 2
        await _settle_lease(backend)
        granted_reply = await _drain(s2_inbox)
        assert granted_reply["id"] == 2 and "result" in granted_reply
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_orphan_drop_log_omits_the_uri(self, caplog: pytest.LogCaptureFixture) -> None:
        """Resource URIs can carry tokens or presigned query parameters, so
        the orphaned-lease drop line must not persist them in the log."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        secret_uri = "https://bucket/object?sig=TOPSECRET"
        backend._orphaned_leases.add(secret_uri)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(
                _line(
                    {
                        "method": "notifications/resources/updated",
                        "params": {"uri": secret_uri},
                    }
                )
            )
        assert "orphaned lease" in caplog.text
        assert "TOPSECRET" not in caplog.text

    @pytest.mark.asyncio
    async def test_replay_and_release_failure_logs_omit_the_uri(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The replay-failure and release-failure lines must not persist
        the URI either."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        secret_uri = "https://bucket/object?sig=TOPSECRET"
        monkeypatch.setattr(
            backend_mod,
            "_write_json_line",
            AsyncMock(side_effect=RuntimeError("pipe gone")),
        )
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_gateway.backend"):
            with pytest.raises(backend_mod.BackendGone):
                await backend.replay_resource_subscriptions("s1", [secret_uri])
            await backend._release_upstream_subscriptions([secret_uri])
        assert "could not replay" in caplog.text
        assert "could not release" in caplog.text
        assert "TOPSECRET" not in caplog.text

    @pytest.mark.asyncio
    async def test_refused_release_joins_replacement_locally(self) -> None:
        """When the in-flight release is REFUSED the server retained the
        lease, so a parked replacement joins the still-live entry locally —
        no second upstream subscribe."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
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
        await _drain(inbox1)
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
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # parks as replacement
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1", "s2"}}
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        assert backend._lease_replacement_subscribes == {}

    @pytest.mark.asyncio
    async def test_detach_never_promotes_the_departing_stubs_own_rider(
        self,
    ) -> None:
        """A departing stub's own parked duplicate must not be promoted into
        its in-flight subscribe: the grant would record a phantom subscriber
        that blocks the release and swallows updates. The rider prune runs
        before the pending scan, so the pending converts to the sentinel and
        the granted-but-unwanted lease is released."""
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
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # s1's own duplicate parks as a rider
        await backend.detach_stub("s1")
        p = next(p for p in backend._pending_requests.values() if p.resource_uri)
        assert p.stub_uuid == backend_mod._RELEASE_STUB_SENTINEL
        assert backend._lease_pending_riders == {}
        await _settle_lease(backend)  # grant lands under the sentinel
        # Nobody wants the lease: routing stays empty and it is released.
        assert backend._resource_subscriptions == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_replacement_parks_behind_a_gateway_originated_release(
        self,
    ) -> None:
        """A detach-orphaned release is a release like any other: it must
        mark the URI in-flight so a replacement subscribe parks behind it
        instead of forwarding beside it and racing its answer order (the
        round-11 guard covered only stub-forwarded releases)."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
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
        await _drain(inbox1)
        # Last subscriber departs WITHOUT unsubscribing: the gateway
        # originates the release itself.
        await backend.detach_stub("s1")
        assert "file:///watched.txt" in backend._lease_awaiting_release
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # parks — not forwarded beside the in-flight release
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        await _settle_lease(backend)  # the gateway release confirms
        assert "file:///watched.txt" not in backend._lease_awaiting_release
        await _settle_lease(backend)  # the drained fresh subscribe is granted
        assert (await _drain(inbox2))["id"] == 2
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_concurrent_replays_coalesce_on_one_lease(self) -> None:
        """Two identity-less replays of one URI must not put two grants in
        flight: the server holds ONE subscription, so a later final
        unsubscribe would release it while the other stub stayed routed.
        The second replay rides the first's sentinel grant, and a final
        unsubscribe afterwards only leaves the table (lease intact)."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        await backend.replay_resource_subscriptions("s2", ["file:///watched.txt"])
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1  # second replay rides, never forwards
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1", "s2"}}
        # s1 leaves: with a co-tenant still routed this must NOT release
        # the server's single subscription.
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        assert (await _drain(inbox1))["id"] == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert releases == []

    @pytest.mark.asyncio
    async def test_replay_joins_a_held_lease_locally(self) -> None:
        """A replay for a URI another stub already holds joins the routing
        table locally — no duplicate upstream subscribe."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
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
        await _drain(inbox1)
        await backend.replay_resource_subscriptions("s2", ["file:///watched.txt"])
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1", "s2"}}

    @pytest.mark.asyncio
    async def test_replay_parks_behind_an_inflight_release(self) -> None:
        """A replay racing an in-flight release parks as a replacement and
        is granted on the fresh sentinel subscribe after the release
        settles — never forwarded beside it."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
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
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # final release in flight
        await backend.replay_resource_subscriptions("s2", ["file:///watched.txt"])
        assert backend._lease_replacement_subscribes == {"file:///watched.txt": [("s2", None)]}
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        await _settle_lease(backend)  # release confirms; drain forwards
        await _settle_lease(backend)  # fresh sentinel subscribe granted
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_maintenance_pendings_carry_a_start_time(self) -> None:
        """Gateway-originated release and replay pendings must carry
        ``t_start_ms``: the heartbeat wedge scan computes each pending's age
        from it, and a zero start time reads as host-uptime old — past the
        hard ceiling it recycles a healthy backend as wedged."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend._release_upstream_subscriptions(["file:///a.txt"])
        await backend.replay_resource_subscriptions("s1", ["file:///b.txt"])
        maintenance = [p for p in backend._pending_requests.values() if p.resource_uri]
        assert len(maintenance) == 2
        assert all(p.t_start_ms > 0 for p in maintenance)

    @pytest.mark.asyncio
    async def test_idless_subscription_frames_are_swallowed(self) -> None:
        """An id-less subscribe has no response that could ever settle the
        lease transition it would start: it must neither mutate lease state
        (later subscribers would park forever) nor be forwarded (a
        server-side subscription the broker never routes). A later valid
        subscriber proceeds normally."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # id-less
        assert backend._lease_awaiting_grant == set()
        assert _frames(backend) == []
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # id-less unsubscribe swallowed too
        assert _frames(backend) == []
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox2))["id"] == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_release_waiters_count_toward_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated final unsubscribes under a slow server park as waiters;
        the per-stub cap bounds that list exactly as it bounds riders."""
        monkeypatch.setattr(backend_mod, "_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB", 2)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
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
        await _drain(inbox1)
        for req_id in (2, 3, 4, 5):
            await backend.forward_from_stub(
                "s1",
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/unsubscribe",
                    "params": {"uri": "file:///watched.txt"},
                },
            )
        # id 2 forwarded (release in flight), 3 and 4 parked, 5 refused.
        refusal = await _drain(inbox1)
        assert refusal["id"] == 5 and "error" in refusal
        assert len(backend._lease_release_waiters["file:///watched.txt"]) == 2
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(upstream) == 1

    @pytest.mark.asyncio
    async def test_orphaned_leases_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The orphaned-lease set is bounded: past the cap an arbitrary
        entry is evicted rather than growing the set for the backend's
        lifetime (suppression is telemetry hygiene, not correctness)."""
        monkeypatch.setattr(backend_mod, "_ORPHANED_LEASES_MAX", 2)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        backend._orphaned_leases.update({"file:///old1", "file:///old2"})
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
        await _drain(inbox1)
        await backend.detach_stub("s1")  # sentinel release forwarded
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        assert "file:///watched.txt" in backend._orphaned_leases
        assert len(backend._orphaned_leases) == 2

    @pytest.mark.asyncio
    async def test_backend_death_settles_every_parked_lease_request(
        self,
    ) -> None:
        """A rider, a release waiter, and a parked replacement each hold a
        client id only a backend response would settle. When the backend
        dies terminally, each must receive the synthetic backend-gone error
        instead of hanging in the client's id table forever, and the lease
        coordination state must clear (the routing table is kept for the
        respawn replay)."""
        backend = _make_backend()
        rider_inbox = await backend.attach_stub("rider")
        waiter_inbox = await backend.attach_stub("waiter")
        repl_inbox = await backend.attach_stub("repl")
        # A rider: parks behind rider's own in-flight grant on uri A.
        await backend.forward_from_stub(
            "rider",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///a.txt"},
            },
        )
        await backend.forward_from_stub(
            "rider",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///a.txt"},
            },
        )  # rider parking (rider, 2)
        # A waiter + a replacement: waiter holds uri B, releases it, then
        # repeats the unsubscribe (waiter parking) while repl subscribes
        # (replacement parking).
        await backend.forward_from_stub(
            "waiter",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///b.txt"},
            },
        )
        await _settle_lease(backend)
        await _drain(waiter_inbox)
        await backend.forward_from_stub(
            "waiter",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///b.txt"},
            },
        )  # final release in flight
        await backend.forward_from_stub(
            "waiter",
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///b.txt"},
            },
        )  # waiter parking (waiter, 5)
        await backend.forward_from_stub(
            "repl",
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/subscribe",
                "params": {"uri": "file:///b.txt"},
            },
        )  # replacement parking (repl, 6)
        await backend._broadcast_backend_gone("test teardown")
        # Every parked id answered with the synthetic error.
        rider_err = await _drain(rider_inbox)
        assert rider_err["id"] in (1, 2) and "error" in rider_err
        rider_err2 = await _drain(rider_inbox)
        assert {rider_err["id"], rider_err2["id"]} == {1, 2}
        assert "error" in rider_err2
        waiter_errs = [await _drain(waiter_inbox), await _drain(waiter_inbox)]
        assert {e["id"] for e in waiter_errs} == {4, 5}
        assert all("error" in e for e in waiter_errs)
        repl_err = await _drain(repl_inbox)
        assert repl_err["id"] == 6 and "error" in repl_err
        # Coordination state cleared; routing kept for the respawn replay.
        assert backend._lease_pending_riders == {}
        assert backend._lease_release_waiters == {}
        assert backend._lease_replacement_subscribes == {}
        assert backend._lease_awaiting_grant == set()
        assert backend._lease_awaiting_release == set()
        assert backend._resource_subscriptions == {"file:///b.txt": {"waiter"}}

    @pytest.mark.asyncio
    async def test_replay_write_failure_raises_not_swallows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A replay WRITE failure must surface as BackendGone: the
        replacement's pipe is broken, and swallowing it would report a
        successful respawn whose subscriptions are silently dark forever."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        monkeypatch.setattr(
            backend_mod,
            "_write_json_line",
            AsyncMock(side_effect=RuntimeError("pipe gone")),
        )
        with pytest.raises(backend_mod.BackendGone):
            await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        assert backend._lease_awaiting_grant == set()
        assert all(not p.resource_uri for p in backend._pending_requests.values())

    @pytest.mark.asyncio
    async def test_partial_replay_failure_scopes_earlier_pendings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the second replay write fails, the FIRST URI's already-sent
        pending is scoped to release-on-grant: the respawn is reported
        failed, so a late success must release its lease rather than pin it
        to a stub the caller is tearing down."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        real_write = backend_mod._write_json_line
        calls = {"n": 0}

        async def write_second_fails(writer: Any, obj: Any) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("pipe gone")
            await real_write(writer, obj)

        monkeypatch.setattr(backend_mod, "_write_json_line", write_second_fails)
        with pytest.raises(backend_mod.BackendGone):
            await backend.replay_resource_subscriptions("s1", ["file:///a.txt", "file:///b.txt"])
        # The first URI's pending survives but is scoped: no replay stub.
        survivors = [p for p in backend._pending_requests.values() if p.resource_uri]
        assert len(survivors) == 1
        assert not survivors[0].replay_stub
        # Its late grant releases the lease instead of granting s1.
        monkeypatch.setattr(backend_mod, "_write_json_line", real_write)
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_retracts_a_parked_replacement(self) -> None:
        """A stub that parked a replacement subscribe and then unsubscribes
        must not be resurrected by the release drain: the parking is
        retracted (its subscribe id answered with a cancellation error) and
        the settled release re-subscribes nobody."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
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
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # final release in flight
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # parks as replacement
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # retracts the parking
        retracted = await _drain(inbox2)
        assert retracted["id"] == 3 and "error" in retracted
        unsub_reply = await _drain(inbox2)
        assert unsub_reply["id"] == 4 and "result" in unsub_reply
        assert backend._lease_replacement_subscribes == {}
        await _settle_lease(backend)  # the release confirms
        # Nobody is re-subscribed: exactly one upstream subscribe ever.
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        assert backend._resource_subscriptions == {}

    @pytest.mark.asyncio
    async def test_overlong_uri_refused_before_any_table_stores_it(
        self,
    ) -> None:
        """The per-stub cap bounds subscription COUNT, not bytes: an
        overlong URI is refused locally before any table retains the key,
        and the refusal does not echo the URI."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        huge = "file:///" + "x" * (backend_mod._RESOURCE_URI_MAX_LEN + 1)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": huge},
            },
        )
        refusal = await _drain(inbox1)
        assert refusal["id"] == 1 and "error" in refusal
        assert huge not in json.dumps(refusal)
        assert backend._resource_subscriptions == {}
        assert backend._lease_awaiting_grant == set()
        assert backend._lease_pending_riders == {}
        assert all(not p.resource_uri for p in backend._pending_requests.values())
        assert _frames(backend) == []  # never forwarded either

"""How per-caller grants and rekey eviction keep one owner's leases away from another.

These tests pin the lease broker on an identity-capable server, where each grant is recorded with
the caller that took it and a replayed one with the replay's caller: a detach releases it as that
caller, but not while another stub of the same caller still holds it; an unsubscribe settles locally
from a stub holding no grant, or from one sharing the caller while another sharer remains; a grant
with no recorded caller is orphaned rather than released bare; an unsettled grant, and the late
grant of a subscribe retracted by an unsubscribe or a detach, is released as its caller; and a
retracted pending still counts against the stub's cap. ``evict_stub_subscriptions``, which a
warm-pool claim runs to rekey an attached stub to a new owner without detaching it, releases each of
the stub's grants as its grant-time caller (a callerless one orphaned, and without caller identity a
shared lease released only by its last subscriber), sentinelizes an in-flight unsubscribe, retracts
an in-flight subscribe or replay and commits every removal before its first await, and a multi-URI
replay stops writing once a rekey lands mid-loop; ``resource_subscription_uris``, which the next
respawn's capture reads, keeps in-flight replay URIs, across a backend death too, but not a
rekey-evicted one. A topic of the ``test_mcp_gateway_backend_coverage`` family:
``test_mcp_gateway_backend_coverage.py`` keeps ``forward_from_stub`` and stub attach and detach, the
other siblings are ``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py``,
``test_mcp_gateway_backend_coverage_mcp_apps.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``, and the shared fixture and helpers live
in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

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


class TestSubscriptionResponseHardeningCallerGrantsAndEviction:
    """Per-caller grants and rekey eviction keep one owner's leases away from another.

    These tests pin the lease broker on an identity-capable server, where a grant is released as the
    caller that took it; ``evict_stub_subscriptions``, which a warm-pool claim runs to rekey an
    attached stub to a new owner, in either regime; and the respawn capture of in-flight replays.
    The broker without caller identity is pinned by
    ``TestSubscriptionResponseHardeningLeaseSettlement`` in
    ``test_mcp_gateway_backend_coverage_lease_settlement.py``.
    """

    @pytest.mark.asyncio
    async def test_identity_detach_releases_as_the_grant_caller(self) -> None:
        """On an identity-capable server a departing stub's subscription is
        released AS the caller recorded at grant time — a bare unsubscribe
        would be adjudicated as the connection, leaving the departed
        caller's upstream subscription firing forever."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
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
        await _settle_lease(backend)
        await _drain(inbox1)
        assert backend._grant_callers == {("file:///watched.txt", "s1"): caller_a}
        await backend.detach_stub("s1")
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]
        assert backend._grant_callers == {}
        assert backend._resource_subscriptions == {}

    @pytest.mark.asyncio
    async def test_identity_detach_skips_release_for_a_shared_caller(
        self,
    ) -> None:
        """Two stubs claimed to one session hold ONE per-caller grant
        upstream: the first stub's detach must NOT release it (the survivor
        stays routed); the last sharer's detach releases it."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        for stub, req_id, inbox in (("s1", 1, inbox1), ("s2", 2, inbox2)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///watched.txt"},
                },
                caller=caller_a,
            )
            await _settle_lease(backend)
            await _drain(inbox)
        await backend.detach_stub("s1")
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert releases == []  # survivor still consumes the shared grant
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend.detach_stub("s2")
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1  # last sharer departs: released once
        # ... and released AS the grant caller, not as a bare unsubscribe.
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_identity_unknown_grant_caller_orphans_not_bare_release(
        self,
    ) -> None:
        """A routed identity grant with no recorded caller cannot be
        released safely (a bare unsubscribe is the wrong principal): the
        lease is knowingly retained and orphan-marked so its updates never
        charge a hazard to a blameless server."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )  # no caller: grant recorded with no principal
        await _settle_lease(backend)
        await _drain(inbox1)
        assert backend._grant_callers == {}
        await backend.detach_stub("s1")
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert releases == []
        assert "file:///watched.txt" in backend._orphaned_leases

    @pytest.mark.asyncio
    async def test_replayed_identity_grant_records_the_replay_caller(
        self,
    ) -> None:
        """A respawn-replayed identity grant is held by the replay's
        principal: the grant caller is recorded so the stub's later detach
        releases it as that caller instead of orphaning the lease."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"], caller=caller_a)
        await _settle_lease(backend)
        assert backend._grant_callers == {("file:///watched.txt", "s1"): caller_a}
        await backend.detach_stub("s1")
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]
        assert "file:///watched.txt" not in backend._orphaned_leases

    @pytest.mark.asyncio
    async def test_malformed_identity_grant_releases_as_the_caller(
        self,
    ) -> None:
        """An identity subscribe answered with neither result nor error may
        have taken upstream: recording nothing would strand a live
        per-caller lease. The unsettled verdict releases it as the caller
        that took it."""
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
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_shared_caller_unsubscribe_settles_locally(self) -> None:
        """Two stubs sharing one per-caller grant: either stub's explicit
        unsubscribe must settle locally (drop only that stub) — forwarding
        it would destroy the survivor's upstream subscription. The last
        sharer's unsubscribe forwards and releases."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        for stub, req_id, inbox in (("s1", 1, inbox1), ("s2", 2, inbox2)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///watched.txt"},
                },
                caller=caller_a,
            )
            await _settle_lease(backend)
            await _drain(inbox)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
            caller=caller_a,
        )
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert upstream == []  # settled locally, survivor keeps the lease
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        # Last sharer's unsubscribe forwards normally.
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
            caller=caller_a,
        )
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(upstream) == 1
        await _settle_lease(backend)
        assert (await _drain(inbox2))["id"] == 4
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}

    @pytest.mark.asyncio
    async def test_evict_stub_subscriptions_releases_as_grant_caller(
        self,
    ) -> None:
        """A warm-pool claim rekeys a stub to a new session: the old
        caller's grants are evicted and released AS the grant-time caller,
        so the new owner never receives the old owner's resource-update
        URIs. The stub stays attached (rekey, not detach)."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
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
        await _settle_lease(backend)
        await _drain(inbox1)
        evicted = await backend.evict_stub_subscriptions("s1")
        assert evicted == 1
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        assert "s1" in backend._stub_inboxes  # attached: rekey, not detach
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]
        # Post-eviction: an update for the old URI routes to nobody.
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_evict_stub_subscriptions_identityless_last_subscriber(
        self,
    ) -> None:
        """Identity-less regime: eviction drops the stub from routing and
        releases the shared lease only when it was the last subscriber."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id, inbox in (("s1", 1, inbox1), ("s2", 2, inbox2)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///watched.txt"},
                },
            )
            if stub == "s1":
                await _settle_lease(backend)
            await _drain(inbox)
        assert await backend.evict_stub_subscriptions("s1") == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert releases == []  # co-tenant still subscribed: no release
        assert await backend.evict_stub_subscriptions("s2") == 1
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1  # last subscriber evicted: released

    @pytest.mark.asyncio
    async def test_eviction_sentinelizes_inflight_unsubscribe(self) -> None:
        """An unsubscribe sent under the OLD owner whose response arrives
        AFTER the rekey must not deliver under the old request id into the
        rekeyed stub's stream — the new owner may have already reused that
        id for its own request. Eviction sentinelizes the pending (mirror
        of ``detach_stub``): the response settles silently."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
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
            caller=caller_a,
        )  # release in flight at the rekey moment
        pend = [
            (fid, p)
            for fid, p in backend._pending_requests.items()
            if p.method == "resources/unsubscribe" and p.stub_uuid == "s1"
        ]
        assert len(pend) == 1
        fid, p = pend[0]
        assert p.original_id == 2
        await backend.evict_stub_subscriptions("s1")
        assert p.stub_uuid == backend_mod._RELEASE_STUB_SENTINEL
        assert p.original_id is None
        # The unsubscribe response settles after the rekey: nothing may
        # reach the rekeyed stub's stream under the old id.
        await backend._route_backend_line(
            _line(
                {
                    "jsonrpc": "2.0",
                    "id": fid,
                    "result": {},
                }
            )
        )
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_eviction_retracts_inflight_subscribe(self) -> None:
        """A subscribe sent under the old owner whose grant arrives AFTER
        the rekey must not route to the new owner: eviction retracts the
        pending (id answered with a cancellation error) and the late grant
        releases the lease as the old principal."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
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
        )  # grant in flight
        assert await backend.evict_stub_subscriptions("s1") == 0
        retracted = await _drain(inbox1)
        assert retracted["id"] == 1 and "error" in retracted
        await _settle_lease(backend)  # the late grant arrives
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_eviction_commits_routing_before_first_await(self) -> None:
        """The claim path reassigns the owner then awaits the eviction: any
        frame routed during those awaits must already see the stub gone
        from the tables, so every removal commits before the first reply
        or release is awaited."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///granted.txt"},
            },
            caller=caller_a,
        )
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///inflight.txt"},
            },
            caller=caller_a,
        )  # in flight: forces a retract-reply await
        seen_at_first_await: list[dict] = []
        real_reply = backend._reply_locally

        async def spying_reply(*args: Any, **kwargs: Any) -> None:
            if not seen_at_first_await:
                seen_at_first_await.append(
                    {u: set(s) for u, s in backend._resource_subscriptions.items()}
                )
            await real_reply(*args, **kwargs)

        backend._reply_locally = spying_reply  # type: ignore[method-assign]
        try:
            await backend.evict_stub_subscriptions("s1")
        finally:
            backend._reply_locally = real_reply  # type: ignore[method-assign]
        assert seen_at_first_await == [{}]  # routing already empty at await
        assert backend._grant_callers == {}

    @pytest.mark.asyncio
    async def test_eviction_retracts_inflight_replay(self) -> None:
        """A REPLAY in flight when the stub is rekeyed targets the stub by
        its ``replay_stub`` back-reference, which the subscribe retraction
        cannot see: eviction must scope it to release-on-grant, or the old
        owner's replayed URI routes to (and its grant-caller record binds
        to) the new owner."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///old.txt"])
        assert any(
            p.replay_stub == "s1" for p in backend._pending_requests.values()
        )  # replay grant in flight
        await backend.evict_stub_subscriptions("s1")
        assert not any(p.replay_stub == "s1" for p in backend._pending_requests.values())
        await _settle_lease(backend)  # the late replay grant arrives
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1  # released, not routed to the new owner
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///old.txt"},
                }
            )
        )
        assert inbox1.empty()  # nothing delivered to the rekeyed stub

    @pytest.mark.asyncio
    async def test_respawn_capture_includes_inflight_replay_uris(self) -> None:
        """Routing commits only on the server's grant, so a replacement that
        dies before its replay responses arrive holds those URIs in neither
        the routing table nor anywhere the NEXT respawn's capture looked:
        ``resource_subscription_uris`` must include in-flight replay URIs or
        a second respawn goes permanently dark."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///pending.txt"])
        # No grant yet: the routing table is empty, the replay is in flight.
        assert backend._resource_subscriptions == {}
        assert backend.resource_subscription_uris("s1") == ["file:///pending.txt"]

    @pytest.mark.asyncio
    async def test_respawn_capture_survives_backend_death(self) -> None:
        """The backend-gone cleanup clears the pending table, which is an
        in-flight replay's only record: erase it and a replacement dying before
        its replay responses arrive leaves the NEXT respawn's capture empty and
        the subscription permanently dark. Death preserves the replay-target URIs
        for the capture. A rekey-evicted replay
        (``replay_stub`` scoped to ``""``) is correctly NOT preserved."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.replay_resource_subscriptions("s1", ["file:///live.txt"])
        await backend.replay_resource_subscriptions("s2", ["file:///evicted.txt"])
        await backend.evict_stub_subscriptions("s2")  # scopes s2's replay
        await backend._broadcast_backend_gone("stdout EOF")
        assert backend._pending_requests == {}
        assert backend.resource_subscription_uris("s1") == ["file:///live.txt"]
        assert backend.resource_subscription_uris("s2") == []

    @pytest.mark.asyncio
    async def test_eviction_clears_grants_without_caller_records(self) -> None:
        """On an identity server the grant arm commits routing even when the
        accepted subscribe carried no caller metadata (no ``_grant_callers``
        record). An eviction keyed on the records alone would leave that
        entry routed — the new owner would keep receiving the old owner's
        URI. Eviction must walk the routing table: the callerless grant is
        removed and orphan-marked (retained upstream, updates dropped), not
        released as the wrong principal."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        backend.supports_caller_identity = True
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///keyless.txt"},
            },
        )  # no caller kwarg: grant will carry no _grant_callers record
        await _settle_lease(backend)
        await _drain(inbox1)
        assert backend._resource_subscriptions == {"file:///keyless.txt": {"s1"}}
        assert backend._grant_callers == {}
        evicted = await backend.evict_stub_subscriptions("s1")
        assert evicted == 1
        assert backend._resource_subscriptions == {}
        assert "file:///keyless.txt" in backend._orphaned_leases
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert releases == []  # never released as the wrong principal
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///keyless.txt"},
                }
            )
        )
        assert inbox1.empty()  # nothing routed to the rekeyed stub

    @pytest.mark.asyncio
    async def test_replay_stops_when_stub_rekeyed_mid_loop(self) -> None:
        """A multi-URI replay awaits between writes; a claim landing
        mid-loop evicts the pendings written so far, but the loop would
        keep writing the REMAINING URIs under the old owner. The replay
        must notice the rekey between writes and stop."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        real_write = backend_mod._write_json_line
        calls: list[str] = []

        async def rekeying_write(stdin: Any, msg: dict) -> None:
            calls.append(msg["params"]["uri"])
            await real_write(stdin, msg)
            if len(calls) == 1:
                # The claim lands while the first write's await is live.
                await backend.evict_stub_subscriptions("s1")

        backend_mod._write_json_line = rekeying_write
        try:
            await backend.replay_resource_subscriptions(
                "s1", ["file:///a.txt", "file:///b.txt", "file:///c.txt"]
            )
        finally:
            backend_mod._write_json_line = real_write
        assert calls == ["file:///a.txt"]  # loop stopped after the rekey

    @pytest.mark.asyncio
    async def test_nonholder_unsubscribe_settles_locally(self) -> None:
        """On an identity server a stub holding NO grant for the URI must
        have its unsubscribe answered locally: the server adjudicates by
        CALLER, so forwarding a non-holder's frame would remove a
        same-caller HOLDER's live lease while the holder stayed locally
        routed — updates silently stop."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///held.txt"},
            },
            caller=caller_a,
        )
        await _settle_lease(backend)
        await _drain(inbox1)
        # s2 (same caller) never subscribed, nothing outstanding — its
        # unsubscribe must settle locally, not forward.
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///held.txt"},
            },
            caller=caller_a,
        )
        reply = await _drain(inbox2)
        assert reply["id"] == 2 and "error" not in reply
        forwarded = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert forwarded == []  # holder's lease untouched upstream
        assert backend._resource_subscriptions == {"file:///held.txt": {"s1"}}

    @pytest.mark.asyncio
    async def test_retracted_pendings_still_count_against_the_cap(self) -> None:
        """Sentinelizing a retracted subscribe removes it from stub-keyed
        accounting; without origin accounting a stub cycling
        subscribe/unsubscribe against an unresponsive server slips every
        cycle's pending out of its count and grows the table without
        bound. Retained sentinels must count against the originator."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        before = backend._stub_subscription_count("s1")
        for i in range(3):
            await backend.forward_from_stub(
                "s1",
                {
                    "jsonrpc": "2.0",
                    "id": 100 + i,
                    "method": "resources/subscribe",
                    "params": {"uri": f"file:///cycle-{i}.txt"},
                },
                caller=caller_a,
            )  # server never answers
            await backend.forward_from_stub(
                "s1",
                {
                    "jsonrpc": "2.0",
                    "id": 200 + i,
                    "method": "resources/unsubscribe",
                    "params": {"uri": f"file:///cycle-{i}.txt"},
                },
                caller=caller_a,
            )  # retract sentinelizes the pending
        assert backend._stub_subscription_count("s1") == before + 3

    @pytest.mark.asyncio
    async def test_identity_detach_sentinelizes_inflight_subscribe(
        self,
    ) -> None:
        """An identity stub detaching with a subscribe in flight must not
        drop the pending: the late grant would strand the upstream lease
        with nobody to release it. The pending converts to the sentinel and
        the grant is released as the pending's caller."""
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
        )  # grant in flight
        await backend.detach_stub("s1")
        p = next(p for p in backend._pending_requests.values() if p.resource_uri)
        assert p.stub_uuid == backend_mod._RELEASE_STUB_SENTINEL
        await _settle_lease(backend)  # the late grant arrives
        assert backend._resource_subscriptions == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_identity_unsubscribe_retracts_inflight_subscribe(
        self,
    ) -> None:
        """An identity unsubscribe racing the stub's own in-flight subscribe
        retracts it: the subscribe id is answered with a cancellation error,
        the unsubscribe settles locally, and the out-of-order late grant
        releases instead of recording routing for a stub that left."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
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
        )  # grant in flight
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
            caller=caller_a,
        )
        cancelled = await _drain(inbox1)
        assert cancelled["id"] == 1 and "error" in cancelled
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        unsubs = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert unsubs == []  # nothing granted: settled locally
        await _settle_lease(backend)  # the out-of-order grant arrives late
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

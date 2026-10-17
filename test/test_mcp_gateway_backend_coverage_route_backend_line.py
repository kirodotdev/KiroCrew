"""How ``Backend._route_backend_line`` attributes each frame a pooled backend writes.

These tests pin the drop of a non-JSON or non-object line and of a frame with neither id nor method,
the swallowed heartbeat pong, an unknown response id dropped, a response restored to its stub's
original id with its latency metric, the initialize and MCP Apps sentinel responses, a progress
notification routed to its one owner, a global notification broadcast and an unattributable
request-scoped one dropped. Most of the file is resource-update routing: an update reaches only the
subscribers of its URI and is never routed by ``relatedRequestId``; one subscribe per URI is
forwarded while later subscribers join the granted lease locally or park until the server's verdict;
a refusal grants nobody, an update racing the grant is dropped and a refused final unsubscribe keeps
routing; a co-tenant's replacement subscribe parks behind an in-flight release, while the releasing
stub's own resubscribe is refused until that release settles; unsubscribe and detach release the
upstream lease only when the last subscriber leaves, and an unsubscribe from a stub that never
subscribed is answered locally; a detached in-flight forwarder hands its pending to the first parked
rider, and an orphaned grant settles a late rider or is released at once, as is a replay grant for a
detached stub; an update for a URI nobody holds is dropped with the unattributable-notification
hazard recorded, except on a lease the broker itself retained, and fan-out survives a full inbox
detaching a subscriber; an identity-capable server is sent every subscribe and grants per stub; a
respawn replay restores routing on its grant, carries the caller on an identity-capable server and
is skipped there without one; retracted subscribe ids are answered; a subscribe past the per-stub
cap is refused locally, a malformed subscribe records nothing and a malformed update is dropped. A
server-to-client request routes by ``relatedRequestId`` or to the single attached stub, and one that
cannot be attributed recycles the backend. A topic of the ``test_mcp_gateway_backend_coverage``
family: ``test_mcp_gateway_backend_coverage.py`` keeps ``forward_from_stub``, the initialize state
machine and notification ownership, the other siblings are
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py``,
``test_mcp_gateway_backend_coverage_mcp_apps.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``, and the shared fixture and helpers live
in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import (
    _drain,
    _frames,
    _line,
    _make_backend,
    _settle,
    _settle_lease,
)

from kiro_crew.mcp_caller import CALLER_META_KEY, CallerContext
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import HEARTBEAT_PING_ID, _PendingRequest

# --- _route_backend_line ----------------------------------------------------


class TestRouteBackendLine:
    @pytest.mark.asyncio
    async def test_non_json_and_non_object_lines_dropped(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(b"not json at all\n")
        await backend._route_backend_line(b"\xff\xfe binary\n")
        await backend._route_backend_line(b"[1,2,3]\n")
        assert inbox.empty()

    @pytest.mark.asyncio
    async def test_heartbeat_pong_is_swallowed(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._last_ping_response_mono = 0.0
        await backend._route_backend_line(_line({"id": HEARTBEAT_PING_ID, "result": {}}))
        assert inbox.empty()
        assert backend._last_ping_response_mono > 0.0
        # Stringified form too.
        backend._last_ping_response_mono = 0.0
        await backend._route_backend_line(
            _line({"id": str(HEARTBEAT_PING_ID), "error": {"code": -1}})
        )
        assert backend._last_ping_response_mono > 0.0

    @pytest.mark.asyncio
    async def test_unknown_response_id_dropped(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(_line({"id": "gw-nope", "result": {}}))
        assert inbox.empty()

    @pytest.mark.asyncio
    async def test_response_restores_original_id(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-4242-1"] = _PendingRequest("s1", 42, "tools/list")
        await backend._route_backend_line(
            _line({"jsonrpc": "2.0", "id": "gw-4242-1", "result": {"tools": []}})
        )
        assert await _drain(inbox) == {"jsonrpc": "2.0", "id": 42, "result": {"tools": []}}
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_completed_request_emits_latency_metric(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(path))
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", t_start_ms=time.monotonic() * 1000.0
        )
        await backend._route_backend_line(_line({"id": "gw-1", "result": {}}))
        await _settle(backend)
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert record["method"] == "tools/call"
        assert record["ok"] is True
        assert record["stub"] == "s1"
        assert record["pid"] == 4242

    @pytest.mark.asyncio
    async def test_init_sentinel_response_goes_to_handshake(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("__init__", None, "initialize")
        backend._init_pending = [("s1", 1)]
        await backend._route_backend_line(_line({"id": "gw-1", "result": {"capabilities": {}}}))
        assert backend._init_state == "ready"
        assert (await _drain(inbox))["id"] == 1

    @pytest.mark.asyncio
    async def test_apps_sentinel_response_resolves_parked_future(self) -> None:
        backend = _make_backend()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        backend._pending_requests["gw-1"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut
        )
        await backend._route_backend_line(_line({"id": "gw-1", "result": {"contents": []}}))
        # flake-ok: pre-existing await of a future the route call above resolved, moved verbatim
        assert (await fut)["result"] == {"contents": []}

    @pytest.mark.asyncio
    async def test_resolved_apps_future_is_not_re_set(self) -> None:
        backend = _make_backend()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        fut.set_result({"first": True})
        backend._pending_requests["gw-1"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut
        )
        await backend._route_backend_line(_line({"id": "gw-1", "result": {"second": True}}))
        # flake-ok: pre-existing await of a future the test resolved first, moved verbatim
        assert await fut == {"first": True}

    @pytest.mark.asyncio
    async def test_attributable_notification_goes_to_one_stub(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", progress_token="pt"
        )
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/progress",
                    "params": {"progressToken": "pt", "progress": 1},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/progress"
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_global_notification_is_broadcast(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend._route_backend_line(_line({"method": "notifications/tools/list_changed"}))
        assert not inbox1.empty() and not inbox2.empty()

    @pytest.mark.asyncio
    async def test_unattributable_request_scoped_notification_dropped(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "tenant secret"},
                }
            )
        )
        assert inbox1.empty() and inbox2.empty()

    @pytest.mark.asyncio
    async def test_resource_update_reaches_only_the_subscriber(self) -> None:
        """A ``notifications/resources/updated`` is attributed by URI: the stub
        whose ``resources/subscribe`` the server accepted receives it, and a
        co-pooled tenant that never subscribed receives nothing."""
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
        assert (await _drain(inbox1))["id"] == 1  # server's own reply
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        delivered = await _drain(inbox1)
        assert delivered["method"] == "notifications/resources/updated"
        assert delivered["params"]["uri"] == "file:///watched.txt"
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_resource_update_reaches_every_subscriber_of_the_uri(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        # s2 joined a confirmed lease, so its subscribe is answered locally.
        assert (await _drain(inbox2))["result"] == {}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///shared.txt"},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_duplicate_subscribe_is_answered_locally_not_forwarded(self) -> None:
        """The server holds ONE subscription per URI, so only the FIRST
        subscriber's subscribe reaches it; a stub joining a confirmed lease
        receives the MCP empty result from the gateway."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 7
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 7, "result": {}}

    @pytest.mark.asyncio
    async def test_rider_parked_during_grant_gets_the_server_verdict(self) -> None:
        """A stub subscribing while the first subscribe is still in flight is
        PARKED, not told an early success: it is answered only when the
        server's verdict arrives, with that verdict."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub in ("s1", "s2"):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///shared.txt"},
                },
            )
        # Grant still in flight: the rider has NO reply yet.
        assert inbox2.empty()
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 9
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 9, "result": {}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///shared.txt"},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_refused_subscribe_grants_nobody(self) -> None:
        """A server refusal of the forwarded subscribe settles the forwarder
        AND every parked rider on that refusal: nobody routes, and nobody was
        told "subscribed" on the strength of a lease that never materialized
        (the refusal may be made on per-caller authorization grounds)."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub in ("s1", "s2"):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///denied.txt"},
                },
            )
        await _settle_lease(backend, error={"code": -32002, "message": "access denied"})
        refusal1 = await _drain(inbox1)
        assert refusal1["id"] == 5 and "error" in refusal1
        refusal2 = await _drain(inbox2)
        assert refusal2["id"] == 5 and refusal2["error"]["code"] == -32002
        assert backend._resource_subscriptions == {}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///denied.txt"},
                }
            )
        )
        assert inbox1.empty() and inbox2.empty()

    @pytest.mark.asyncio
    async def test_update_racing_the_grant_is_dropped_not_guessed(self) -> None:
        """Routing grants land on the RESPONSE, so an update arriving while
        the grant is still in flight fails closed (dropped) rather than being
        delivered on a subscription the server has not yet accepted."""
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
    async def test_resource_update_for_unsubscribed_uri_keeps_deny_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URI nobody subscribed to must not be guessed at: the update is
        dropped and the unattributable-notification hazard is recorded."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
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
        assert (await _drain(inbox1))["id"] == 1
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///other.txt"},
                }
            )
        )
        assert inbox1.empty() and inbox2.empty()
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

    @pytest.mark.asyncio
    async def test_resource_update_is_never_routed_by_related_request_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URI arm is TERMINAL: a server-supplied ``_meta.relatedRequestId``
        on a resources/updated must not hand a co-tenant a URI only another
        tenant ever named. With no subscriber for the URI the update is
        dropped and the hazard recorded, even though the relatedRequestId
        resolves to a live pending request."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
        backend = _make_backend()
        inbox_v = await backend.attach_stub("victim")
        inbox_a = await backend.attach_stub("attacker-adjacent")
        # The victim subscribes (granted), then unsubscribes (confirmed) —
        # the table is empty again.
        await backend.forward_from_stub(
            "victim",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///victim-private.txt"},
            },
        )
        await _settle_lease(backend)
        await backend.forward_from_stub(
            "victim",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///victim-private.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox_v))["id"] == 1
        assert (await _drain(inbox_v))["id"] == 2
        assert backend._resource_subscriptions == {}
        # A co-tenant holds an ordinary in-flight tools/call.
        await backend.forward_from_stub(
            "attacker-adjacent",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "t"},
            },
        )
        fid = next(
            f for f, p in backend._pending_requests.items() if p.stub_uuid == "attacker-adjacent"
        )
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {
                        "uri": "file:///victim-private.txt",
                        "_meta": {"relatedRequestId": fid},
                    },
                }
            )
        )
        assert inbox_v.empty() and inbox_a.empty()
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_resource_update_delivery(self) -> None:
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
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        # The last subscriber's unsubscribe releases the upstream lease …
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(upstream) == 1
        # … and routing drops the stub once the server CONFIRMS the release.
        assert backend._resource_subscriptions != {}
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        assert (await _drain(inbox1))["id"] == 1
        assert (await _drain(inbox1))["id"] == 2
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
    async def test_out_of_order_release_response_keeps_replacement_grant(self) -> None:
        """A server may answer concurrent requests out of order, so a
        replacement subscribe is never forwarded BESIDE an in-flight
        release — the reverse answer order would leave routing keeping a
        subscriber whose lease the release then destroyed upstream. The
        replacement PARKS; the confirmed release drains it into a fresh
        forwarded subscribe, serialized strictly after the release, and its
        grant routes the replacement."""
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
        assert (await _drain(inbox1))["id"] == 1
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
        )
        # Parked, not forwarded: exactly one subscribe has gone upstream.
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        assert inbox2.empty()
        # The release confirms; the drain forwards the fresh subscribe.
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 2
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 2
        assert inbox2.empty()  # still fail-closed until the grant
        await _settle_lease(backend)  # the fresh subscribe is granted
        assert (await _drain(inbox2))["id"] == 3
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_failed_final_unsubscribe_keeps_routing(self) -> None:
        """A refused unsubscribe means the server RETAINED the subscription:
        routing must keep the stub, or live updates are silently discarded
        while the client believes (from the error it received) that it is
        still subscribed."""
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
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await _settle_lease(backend, error={"code": -32000, "message": "busy"})
        assert (await _drain(inbox1))["id"] == 1
        refusal = await _drain(inbox1)
        assert refusal["id"] == 2 and "error" in refusal
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_unsubscribe_prunes_only_the_unsubscribing_stub(self) -> None:
        """One tenant's unsubscribe must not silence a co-tenant: the routing
        entry survives AND the upstream lease is kept — the non-final
        unsubscribe is answered locally instead of being forwarded to the one
        shared server-side subscription."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        assert (await _drain(inbox2))["result"] == {}
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        # Answered locally; no unsubscribe reached the backend.
        assert (await _drain(inbox1)) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        assert not [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///shared.txt"},
                }
            )
        )
        assert inbox1.empty()
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_stray_unsubscribe_never_tears_down_a_co_tenants_lease(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        # s2 never subscribed; its unsubscribe must not reach the server.
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        assert (await _drain(inbox2))["result"] == {}
        assert not [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///shared.txt"},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_identity_server_gets_every_subscribe_and_grants_per_stub(self) -> None:
        """An identity-capable server authorizes per caller, so EVERY
        subscribe is forwarded (no local coalescing) and each stub routes only
        on its OWN grant — one stub's refusal neither grants it nor disturbs a
        co-tenant's accepted subscription."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id in (("s1", 1), ("s2", 2)):
            await backend.forward_from_stub(
                stub,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///acl.txt"},
                },
            )
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 2
        fids = {p.stub_uuid: f for f, p in backend._pending_requests.items()}
        await backend._route_backend_line(_line({"id": fids["s1"], "result": {}}))
        await backend._route_backend_line(
            _line(
                {
                    "id": fids["s2"],
                    "error": {"code": -32002, "message": "denied"},
                }
            )
        )
        assert (await _drain(inbox1))["id"] == 1
        refusal = await _drain(inbox2)
        assert refusal["id"] == 2 and "error" in refusal
        assert backend._resource_subscriptions == {"file:///acl.txt": {"s1"}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///acl.txt"},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_stub_detach_drops_its_subscriptions(self) -> None:
        """A departed stub's routing entries go with it, and — as the URI's
        last subscriber — the upstream subscription is released so the server
        does not keep firing updates nobody will receive."""
        backend = _make_backend()
        await backend.attach_stub("s1")
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
        await backend.detach_stub("s1")
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1
        assert release[0]["params"] == {"uri": "file:///watched.txt"}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_detach_keeps_upstream_lease_while_a_subscriber_remains(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        assert (await _drain(inbox2))["result"] == {}
        await backend.detach_stub("s1")
        assert not [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///shared.txt"},
                }
            )
        )
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_detach_of_inflight_forwarder_promotes_the_parked_rider(self) -> None:
        """When the stub whose subscribe is awaiting the server detaches, the
        first parked rider inherits the pending response: the grant settles
        the rider (with the server's verdict under the rider's id) instead of
        being dropped as unroutable and wedging the lease."""
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
        await backend.detach_stub("s1")
        await _settle_lease(backend)
        verdict = await _drain(inbox2)
        assert verdict["id"] == 9 and verdict["result"] == {}
        assert backend._resource_subscriptions == {"file:///shared.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_orphaned_grant_is_released_not_leaked(self) -> None:
        """A grant whose forwarder detached with no rider is a lease nobody
        wants: on the server's success it is released on the spot rather than
        left firing updates into the deny-by-default drop."""
        backend = _make_backend()
        await backend.attach_stub("s1")
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
        await backend.detach_stub("s1")
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1

    @pytest.mark.asyncio
    async def test_respawn_replay_restores_routing_on_grant(self) -> None:
        """Transparent respawn replays a stub's subscriptions onto the fresh
        backend: routing returns once the server grants the replayed
        subscribe, so a live subscription survives the backend swap."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        replays = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(replays) == 1
        # No grant yet: fail-closed until the server accepts.
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert inbox1.empty()
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_rider_after_retract_settles_on_the_orphan_grant(self) -> None:
        """Subscribe, unsubscribe before the grant, then a co-tenant
        subscribes before the response: the orphaned grant's verdict settles
        the parked rider (granted and answered) instead of dropping its
        parking and leaving the subscribe hung forever."""
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
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        # The retract answers BOTH of s1's ids: the outstanding subscribe
        # with a cancellation error, the unsubscribe with success.
        cancelled = await _drain(inbox1)
        assert cancelled["id"] == 1 and "error" in cancelled
        assert (await _drain(inbox1))["id"] == 2  # local retract reply
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        assert inbox2.empty()  # parked, not answered early
        await _settle_lease(backend)
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_replay_carries_caller_identity_on_identity_server(self) -> None:
        """On an identity-capable server the respawn replay is adjudicated as
        the same caller that held the original subscription: the replayed
        subscribe carries the connection's caller block."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions(
            "s1",
            ["file:///acl.txt"],
            caller=CallerContext(session_key="dashboard:abc"),
        )
        replays = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(replays) == 1
        assert CALLER_META_KEY in replays[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_replay_skipped_on_identity_server_without_caller(self) -> None:
        """Without a caller to inject, a bare replay would be adjudicated as
        the connection rather than the original caller — so it is skipped
        entirely (fail-closed) instead of risking a wrong-principal grant or
        refusal."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///acl.txt"])
        assert not [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_replay_grant_for_a_detached_stub_releases_the_lease(self) -> None:
        """Detach cannot see a sentinel-owned replay pending, so the response
        arm must catch the mid-replay disconnect itself: a grant for a stub
        that is already detached is released, never recorded against the dead
        UUID (which would pin the lease to a stub that cannot drain it)."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        await backend.detach_stub("s1")
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1

    @pytest.mark.asyncio
    async def test_refused_orphan_release_suppresses_the_false_hazard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Final unsubscribe in flight, the stub disconnects, and the server
        REFUSES the release: the retained subscription's updates are a
        consequence of the broker's own lease handling, so they are dropped
        without recording a hazard — while an update for a genuinely unknown
        URI still records one."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        # A third tenant keeps refcount above the single-client threshold
        # after s1 departs, so the control hazard below is recordable.
        await backend.attach_stub("s3")
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
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await backend.detach_stub("s1")
        # The server refuses the (now sentinel-owned) release.
        await _settle_lease(backend, error={"code": -32000, "message": "busy"})
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///watched.txt"},
                }
            )
        )
        assert recorded == []
        # A genuinely unknown URI still records the hazard.
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///never-named.txt"},
                }
            )
        )
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

    @pytest.mark.asyncio
    async def test_repeated_same_uri_subscribes_count_toward_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rider parkings are counted individually, duplicates included:
        repeating one URI's subscribe while the grant is in flight must not
        grow the rider list unboundedly under a slow server."""
        monkeypatch.setattr(backend_mod, "_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB", 3)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        for req_id in (1, 2, 3, 4):
            await backend.forward_from_stub(
                "s1",
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
                    "params": {"uri": "file:///same.txt"},
                },
            )
        # 1st forwarded (in flight), 2nd and 3rd parked, 4th refused at cap.
        refusal = await _drain(inbox1)
        assert refusal["id"] == 4 and "error" in refusal
        assert len(backend._lease_pending_riders["file:///same.txt"]) == 2
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1

    @pytest.mark.asyncio
    async def test_retracted_inflight_subscribe_ids_are_answered_not_hung(self) -> None:
        """Subscribe (id 1), subscribe again (id 2, parked), then unsubscribe
        (id 3) before the grant arrives: BOTH outstanding subscribe ids
        receive a cancellation error and the unsubscribe receives success —
        no id is silently dropped from the tables to hang in the client's id
        table forever."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        for req_id in (1, 2):
            await backend.forward_from_stub(
                "s1",
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "resources/subscribe",
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
        replies = {}
        while not inbox1.empty():
            reply = await _drain(inbox1)
            replies[reply["id"]] = reply
        assert set(replies) == {1, 2, 3}
        assert "error" in replies[1] and "error" in replies[2]
        assert replies[3]["result"] == {}
        # The orphaned grant settles via the sentinel: on success it is
        # released rather than granted to the departed interest.
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1

    @pytest.mark.asyncio
    async def test_same_stub_resubscribe_during_pending_unsubscribe_is_refused(self) -> None:
        """The releasing stub and the re-subscriber share one uuid, so a late
        out-of-order unsubscribe confirmation would erase the fresh grant.
        The resubscribe is refused locally while the unsubscribe is in
        flight; once it settles, a fresh subscribe succeeds normally."""
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
        assert (await _drain(inbox1))["id"] == 1
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
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        refusal = await _drain(inbox1)
        assert refusal["id"] == 3 and "error" in refusal
        await _settle_lease(backend)  # release confirms
        assert (await _drain(inbox1))["id"] == 2
        assert backend._resource_subscriptions == {}
        # A fresh subscribe after settlement succeeds normally.
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            },
        )
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 4
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}

    @pytest.mark.asyncio
    async def test_subscription_cap_is_refused_locally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap precedes EVERY subscribe branch — including joining a
        co-tenant's confirmed lease — and counts in-flight grants, so a stub
        can neither queue unbounded subscribes nor keep joining URIs."""
        monkeypatch.setattr(backend_mod, "_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB", 1)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///one.txt"},
            },
        )
        # In-flight grant already counts: a second subscribe is refused.
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "file:///two.txt"},
            },
        )
        refusal = await _drain(inbox1)
        assert refusal["id"] == 2 and "error" in refusal
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        # A capped stub cannot join a co-tenant's confirmed lease either.
        await _settle_lease(backend)
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "file:///one.txt"},
            },
        )
        assert (await _drain(inbox2))["result"] == {}
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/subscribe",
                "params": {"uri": "file:///one.txt"},
            },
        )
        capped = await _drain(inbox2)
        assert capped["id"] == 4 and "error" in capped

    @pytest.mark.asyncio
    async def test_full_inbox_detach_during_fanout_does_not_break_delivery(self) -> None:
        """Fan-out iterates a COPY of the target set: a full inbox detaches
        its stub mid-fan-out (mutating the table) and the surviving subscriber
        still receives the update."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub(
            "s1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        await _settle_lease(backend)
        await backend.forward_from_stub(
            "s2",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            },
        )
        assert (await _drain(inbox2))["result"] == {}
        while not inbox1.full():
            inbox1.put_nowait(b"{}")
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": "file:///shared.txt"},
                }
            )
        )
        assert "s1" not in backend._stub_inboxes
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params", [None, "not-a-dict", {}, {"uri": 7}, {"uri": ""}])
    async def test_malformed_subscribe_params_record_nothing(self, params: Any) -> None:
        """A subscribe without a usable ``params.uri`` string leaves the table
        untouched: the backend will reject the request anyway, and a garbage
        key could never match an incoming update's URI."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe"}
        if params is not None:
            msg["params"] = params
        await backend.forward_from_stub("s1", msg)
        assert backend._resource_subscriptions == {}
        assert backend._lease_awaiting_grant == set()

    @pytest.mark.asyncio
    async def test_malformed_resource_update_is_dropped_not_broadcast(self) -> None:
        """An update without a usable URI cannot be attributed and must fall to
        the deny-by-default drop even while subscriptions exist."""
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
        assert (await _drain(inbox1))["id"] == 1
        await backend._route_backend_line(
            _line(
                {
                    "method": "notifications/resources/updated",
                    "params": {"uri": 7},
                }
            )
        )
        await backend._route_backend_line(_line({"method": "notifications/resources/updated"}))
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_server_request_routed_via_related_request_id(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._pending_requests["gw-7"] = _PendingRequest("s2", 1, "tools/call")
        await backend._route_backend_line(
            _line(
                {
                    "id": "srv-1",
                    "method": "sampling/createMessage",
                    "params": {"_meta": {"relatedRequestId": "gw-7"}},
                }
            )
        )
        assert (await _drain(inbox2))["id"] == "srv-1"
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_server_request_routed_to_single_stub(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(_line({"id": "srv-1", "method": "roots/list"}))
        assert (await _drain(inbox))["method"] == "roots/list"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params",
        [
            "not-a-dict",
            {"_meta": "not-a-dict"},
            {"_meta": {}},
            {"_meta": {"relatedRequestId": "gw-unknown"}},
        ],
    )
    async def test_malformed_related_request_id_falls_back_to_single_stub(
        self, params: Any
    ) -> None:
        """Every rung of the relatedRequestId lookup that cannot resolve must
        fall through to the single-stub rule rather than mis-attribute."""
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(
            _line({"id": "srv-1", "method": "roots/list", "params": params})
        )
        assert (await _drain(inbox))["id"] == "srv-1"

    @pytest.mark.asyncio
    async def test_unattributable_server_request_recycles_backend(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend._route_backend_line(_line({"id": "srv-1", "method": "elicitation/create"}))
        assert "cannot route without a cross-tenant leak" in (backend.dead_reason or "")
        assert backend._gone_broadcast is True

    @pytest.mark.asyncio
    async def test_frame_with_neither_id_nor_method_dropped(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(_line({"jsonrpc": "2.0"}))
        assert inbox.empty()

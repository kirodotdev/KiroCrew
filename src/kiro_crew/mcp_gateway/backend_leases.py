"""The ``resources/subscribe`` lease broker of a shared ``Backend``.

Which stubs hold a subscription to which URI; how a stub's subscribe or unsubscribe becomes
at most one lease transition upstream; how the server's verdict settles the riders and
waiters parked on it; the release of a lease nobody holds; the eviction of a rekeyed stub's
grants; and the replay of a stub's subscriptions onto a respawned backend.

Each function here whose first parameter is ``self`` is a ``Backend`` method: the class
binds it by name, so it reads and writes the backend's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_grant_callers``, ``_lease_awaiting_grant``,
``_lease_awaiting_release``, ``_lease_pending_riders``, ``_lease_release_waiters``,
``_lease_replacement_subscribes``, ``_orphaned_leases``, ``_pending_requests``,
``_rekey_generation``, ``_resource_subscriptions``.

Instance fields read: ``_gone_replay_uris``, ``stdin``, ``supports_caller_identity``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway.backend_wire import (
    _JSONRPC_SERVER_ERROR,
    BackendGone,
    _inject_caller_meta,
    _PendingRequest,
    _write_json_line,
)

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

#: The facade's logger: log capture and filters select this code's records by that name.
logger = logging.getLogger("kiro_crew.mcp_gateway.backend")


# Upper bound on the number of distinct URIs one stub may hold resource
# subscriptions for. Same guard class as ``_STUB_INBOX_MAXSIZE``: table
# entries are created by stub input and freed only on unsubscribe, a server
# refusal, or detach, so without a bound one misbehaving tenant could grow
# the shared broker's table (and gateway RSS) without limit. Generous — a
# real client subscribes to a handful of URIs. Past the cap a subscribe is
# answered locally with a JSON-RPC error instead of being forwarded.
_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB = 1024

# Upper bound on the orphaned-lease set (URIs whose gateway-originated
# release the server refused with nobody left to route to). Same guard
# class: entries accumulate for the backend's lifetime across detached
# stubs, so a server that keeps refusing releases would otherwise grow the
# set without bound. The set is telemetry hygiene (hazard suppression),
# not correctness — evicting an arbitrary entry risks at most one unfair
# hazard row, while an unbounded set is unbounded gateway memory.
_ORPHANED_LEASES_MAX = 1024

# Upper bound on a subscribable resource URI's LENGTH. The per-stub cap
# bounds how MANY subscriptions a stub holds, not their bytes: without a
# length bound, repeated accepted subscriptions to distinct near-frame-limit
# URIs retain gigabytes of dictionary keys. Real resource URIs are short;
# 8 KiB is far beyond any legitimate identifier. Overlong URIs are refused
# locally before any table stores them.
_RESOURCE_URI_MAX_LEN = 8192


# MCP resource-subscription wire methods and the notification they scope.
# ``notifications/resources/updated`` carries only a URI — no request id — so
# request-scoped attribution can never route it. The broker instead records
# which stub sent ``resources/subscribe`` for which URI and delivers each
# update to exactly those stubs (see ``Backend._resource_subscriptions``).
_RESOURCES_SUBSCRIBE_METHOD = "resources/subscribe"
_RESOURCES_UNSUBSCRIBE_METHOD = "resources/unsubscribe"
_RESOURCES_UPDATED_NOTIFICATION = "notifications/resources/updated"


# Sentinel ``stub_uuid`` for gateway-originated lease maintenance on the
# resource-subscription table: the ``resources/unsubscribe`` issued when the
# last subscriber of a URI detaches without unsubscribing (so the server does
# not keep firing updates nobody will receive), and the ``resources/subscribe``
# replayed after a transparent respawn (so a live subscription survives the
# backend swap). Responses are consumed in :meth:`Backend._route_backend_line`.
_RELEASE_STUB_SENTINEL = "__release__"


def _is_success_response(msg: dict[str, Any]) -> bool:
    """Whether a JSON-RPC response frame is a settled SUCCESS: it must carry
    a ``result`` and no ``error``. A malformed frame with neither is not a
    grant — mutating the resource-subscription routing table on its strength
    would let a co-tenant start (or stop) receiving updates on a verdict the
    server never actually delivered, so lease transitions treat it as a
    refusal (fail closed)."""
    return "error" not in msg and "result" in msg


async def evict_stub_subscriptions(self: Backend, stub_uuid: str) -> int:
    """Evict ``stub_uuid``'s resource subscriptions because its stub is
    being REKEYED to a different caller (warm-pool ``claim``) — the stub
    stays attached, but grants belong to the OLD principal: without
    eviction the new session would keep receiving the old session's
    resource-update URIs (which can carry tokens or presigned params).

    Ownership of caller-binding lives at the gatewayd connection layer;
    this is the backend-side clearance it invokes at the moment of the
    rekey. Identity-capable grants are released AS the grant-time caller
    (shared-caller guard applies: the last sharer's eviction releases);
    identity-less routing drops the stub with a last-subscriber release.
    Returns the number of URIs evicted. In-flight and parked subscribe
    transitions are RETRACTED (each id answered with a cancellation
    error, pendings scoped so a late grant releases instead of routing):
    a subscribe sent under the old owner must not land its grant on the
    rekeyed stub. In-flight unsubscribes are SENTINELIZED (mirror of
    ``detach_stub``): their table effects are already applied, but the
    response would otherwise deliver under the OLD owner's request id
    into the rekeyed stub's stream — an id the new owner may have
    already reused for its own request.
    """
    # Bump FIRST, before any table work and any await: an in-flight
    # replay loop checks this between its writes, and the bump must be
    # visible from the moment the rekey is decided.
    self._rekey_generation[stub_uuid] = self._rekey_generation.get(stub_uuid, 0) + 1
    # --- retract in-flight/parked subscribe transitions (commit all
    # table changes first, then reply; replies can re-enter via a
    # full-inbox detach) ---
    rekey_error = {
        "code": _JSONRPC_SERVER_ERROR,
        "message": "resources/subscribe retracted: stub ownership changed",
    }
    retract_replies: list[Any] = []
    for table in (
        self._lease_pending_riders,
        self._lease_replacement_subscribes,
        self._lease_release_waiters,
    ):
        for uri in list(table):
            mine = [r for r in table[uri] if r[0] == stub_uuid]
            if not mine:
                continue
            remaining = [r for r in table[uri] if r[0] != stub_uuid]
            if remaining:
                table[uri] = remaining
            else:
                del table[uri]
            retract_replies.extend(rid for _u, rid in mine if rid is not None)
    for p in self._pending_requests.values():
        if (
            p.stub_uuid == stub_uuid
            and p.method == _RESOURCES_UNSUBSCRIBE_METHOD
            and p.resource_uri
        ):
            # The release was sent under the OLD owner: its response
            # would otherwise deliver under the old request id into the
            # rekeyed stub's stream — an id the new owner may have
            # already reused. Sentinelize (mirror of ``detach_stub``):
            # the response settles silently, table effects stay applied.
            p.origin_stub = stub_uuid
            p.stub_uuid = _RELEASE_STUB_SENTINEL
            p.original_id = None
            continue
        if p.stub_uuid == stub_uuid and p.method == _RESOURCES_SUBSCRIBE_METHOD and p.resource_uri:
            if p.original_id is not None:
                retract_replies.append(p.original_id)
            riders = (
                []
                if self.supports_caller_identity
                else self._lease_pending_riders.get(p.resource_uri, [])
            )
            if riders:
                p.stub_uuid, p.original_id = riders.pop(0)
            else:
                # Sentinelize keeping ``caller``: the late grant then
                # finds no replay stub and releases the lease AS the
                # old principal instead of routing it to the new owner.
                p.origin_stub = stub_uuid
                p.stub_uuid = _RELEASE_STUB_SENTINEL
                p.original_id = None
            continue
        if (
            p.stub_uuid == _RELEASE_STUB_SENTINEL
            and p.method == _RESOURCES_SUBSCRIBE_METHOD
            and p.replay_stub == stub_uuid
        ):
            # An in-flight REPLAY targets the rekeyed stub by its
            # ``replay_stub`` back-reference, not by ``stub_uuid`` — the
            # subscribe retraction above cannot see it.  Left attached,
            # its late grant would commit routing (and, on an
            # identity-capable server, a grant-caller record) for a stub
            # the OLD owner subscribed but the NEW owner now holds.
            # Scope it to release-on-grant, exactly as the replay
            # write-failure path does: nobody consumes the grant, so it
            # releases instead of routing.
            p.replay_stub = ""
    # --- commit ALL routing-table removals before the FIRST await:
    # the claim path reassigns the owner then awaits this eviction, so
    # any frame routed during an await must already see the stub gone
    # from the tables ---
    evicted = 0
    releases: list[tuple[str, Optional[CallerContext]]] = []
    if self.supports_caller_identity:
        # Walk the ROUTING TABLE, not ``_grant_callers``: a routed grant
        # can lack a caller record (the grant arm records one only when
        # the accepted subscribe carried caller metadata), and keying
        # the eviction on the records alone would leave that entry
        # routed — the new owner would keep receiving the old owner's
        # URIs. Mirror of ``detach_stub``: known-caller grants release
        # as their principal; unknown-caller grants are knowingly
        # RETAINED and orphan-marked (a bare release would be
        # adjudicated as the wrong principal).
        id_emptied: list[str] = []
        for uri, subscribers in self._resource_subscriptions.items():
            if stub_uuid in subscribers:
                subscribers.discard(stub_uuid)
                evicted += 1
                if not subscribers:
                    id_emptied.append(uri)
        departed_keys = [key for key in self._grant_callers if key[1] == stub_uuid]
        for uri, _stub in departed_keys:
            grant_caller = self._grant_callers.pop((uri, stub_uuid))
            shared = any(
                other != stub_uuid and self._grant_callers.get((uri, other)) == grant_caller
                for other in self._resource_subscriptions.get(uri, set())
            )
            if not shared:
                releases.append((uri, grant_caller))
        for uri in id_emptied:
            del self._resource_subscriptions[uri]
            if not any(u == uri for u, _c in releases):
                while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                    self._orphaned_leases.pop()
                self._orphaned_leases.add(uri)
    else:
        emptied: list[str] = []
        for uri, subscribers in list(self._resource_subscriptions.items()):
            if stub_uuid in subscribers:
                subscribers.discard(stub_uuid)
                evicted += 1
                if not subscribers:
                    emptied.append(uri)
        for uri in emptied:
            del self._resource_subscriptions[uri]
            if uri not in self._lease_awaiting_release:
                releases.append((uri, None))
    # --- state committed; now the awaits ---
    for reply_id in retract_replies:
        await self._reply_locally(stub_uuid, reply_id, error=rekey_error)
    for uri, release_caller in releases:
        await self._release_upstream_subscriptions([uri], caller=release_caller)
    return evicted


async def _handle_resource_subscription(
    self: Backend, stub_uuid: str, method: str, msg: dict[str, Any]
) -> bool:
    """Apply subscription-lease bookkeeping for a stub-forwarded
    ``resources/subscribe`` / ``resources/unsubscribe``; return ``True``
    when the request was answered locally and must not be forwarded.

    Routing grants are recorded on the server's RESPONSE, never at
    forward time, so the table only ever names stubs whose subscription
    the server actually accepted (fail-closed: an update racing the
    response is dropped, not mis-routed).

    Two regimes, split on ``supports_caller_identity``:

    - An identity-capable server authorizes per caller, so EVERY
      subscribe is forwarded with its own caller block and every stub
      gets its own upstream authorization decision — no local coalescing
      on the subscribe side (``_on_resource_subscription_response``
      records the per-stub grant).
    - An identity-less server sees every co-tenant as one principal, so
      duplicate traffic tells it nothing; only lease TRANSITIONS reach
      it. The first subscriber's subscribe takes the lease; a stub
      arriving while that grant is in flight is PARKED and answered with
      the server's actual verdict, never a premature success; a stub
      arriving after the grant joins locally with the MCP empty result
      (exactly as the initialize cache answers later stubs). A non-final
      unsubscribe is answered locally; the final one is forwarded and the
      routing entry is kept until the server confirms, because a failed
      unsubscribe means the server RETAINED the subscription and dropping
      routing early would silently discard live updates.

    A request without a well-formed ``params.uri`` string is forwarded
    verbatim with no table change: the backend rejects it with its own
    error, and an entry keyed on garbage could never match an update.
    """
    params = msg.get("params")
    uri = params.get("uri") if isinstance(params, dict) else None
    if not isinstance(uri, str) or not uri:
        return False
    original_id = msg.get("id")
    if len(uri) > _RESOURCE_URI_MAX_LEN:
        # The per-stub cap bounds subscription COUNT, not bytes: 1024
        # accepted near-frame-limit URIs would retain gigabytes of
        # dictionary keys. Refuse locally before any table stores the
        # key; the URI itself is never logged.
        await self._reply_locally(
            stub_uuid,
            original_id,
            error={
                "code": _JSONRPC_SERVER_ERROR,
                "message": "resource URI exceeds the maximum supported " "length",
            },
        )
        return True
    if original_id is None:
        # An id-less subscribe/unsubscribe is a notification-shaped frame
        # the MCP spec does not define. No response can ever settle the
        # lease transition it would start, so mutating lease state on it
        # would wedge the URI permanently (later subscribers park behind
        # a grant whose response never comes), and forwarding it would
        # open a server-side subscription the broker never routes.
        # Swallow it: a request without an id expects no reply.
        return True
    if method == _RESOURCES_SUBSCRIBE_METHOD:
        # Cap check precedes EVERY subscribe branch, so a capped stub can
        # neither open new leases nor keep joining co-tenants' URIs.
        if self._stub_subscription_count(stub_uuid) >= _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB:
            # The URI is deliberately NOT logged: resource URIs can carry
            # credentials (tokens, presigned query params) and this line
            # lands in the operator's plain-text log.
            logger.warning(
                "backend pid=%s stub=%s at resource-subscription cap (%d); " "refusing subscribe",
                self.pid,
                stub_uuid,
                _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB,
            )
            await self._reply_locally(
                stub_uuid,
                original_id,
                error={
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resource subscription limit reached",
                },
            )
            return True
        if any(
            p.stub_uuid == stub_uuid
            and p.method == _RESOURCES_UNSUBSCRIBE_METHOD
            and p.resource_uri == uri
            for p in self._pending_requests.values()
        ):
            # This stub's own unsubscribe for the URI is still in flight.
            # Its response may arrive AFTER a new grant (out-of-order
            # server) and would then erase it — the releasing stub and the
            # re-subscriber are the same uuid, so the per-stub discard
            # cannot tell them apart. Refuse the resubscribe; the client
            # retries once the unsubscribe settles.
            await self._reply_locally(
                stub_uuid,
                original_id,
                error={
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resources/unsubscribe for this URI is "
                    "still in flight; retry after it completes",
                },
            )
            return True
        if self.supports_caller_identity:
            # Per-caller authorization: the server must see this stub's
            # own subscribe (the caller block is injected downstream).
            return False
        if uri in self._lease_awaiting_grant:
            self._lease_pending_riders.setdefault(uri, []).append((stub_uuid, original_id))
            return True
        subscribers = self._resource_subscriptions.get(uri)
        if subscribers is not None and uri not in self._lease_awaiting_release:
            subscribers.add(stub_uuid)
            await self._reply_locally(stub_uuid, original_id, result={})
            return True
        if uri in self._lease_awaiting_release:
            # A release for this URI is in flight. The one wire orders
            # the WRITES, but the server may ANSWER out of order — a
            # replacement grant landing before the release would leave
            # local routing keeping this subscriber while the release
            # then destroys the lease upstream: silent update loss.
            # Park until the release settles; the drain either joins a
            # retained lease locally or takes a fresh one with a
            # subscribe forwarded strictly AFTER the release settled.
            self._lease_replacement_subscribes.setdefault(uri, []).append((stub_uuid, original_id))
            return True
        # First subscriber: forward to take the lease.
        self._lease_awaiting_grant.add(uri)
        return False
    # --- resources/unsubscribe ---
    if self.supports_caller_identity:
        # Retract this stub's own in-flight subscribe for the URI first
        # (mirror of the coalesced retract arm): its grant may be
        # answered AFTER this unsubscribe on an out-of-order server,
        # which would record routing for a stub that already left —
        # stranding the upstream lease. Sentinelize keeping ``caller``
        # so the late grant releases as the right principal; commit the
        # transitions, then reply.
        retracted_ids: list[Any] = []
        for p in self._pending_requests.values():
            if (
                p.stub_uuid == stub_uuid
                and p.method == _RESOURCES_SUBSCRIBE_METHOD
                and p.resource_uri == uri
            ):
                if p.original_id is not None:
                    retracted_ids.append(p.original_id)
                p.origin_stub = stub_uuid
                p.stub_uuid = _RELEASE_STUB_SENTINEL
                p.original_id = None
        if retracted_ids:
            retract_error = {
                "code": _JSONRPC_SERVER_ERROR,
                "message": "resources/subscribe retracted by a later " "unsubscribe",
            }
            for retracted_id in retracted_ids:
                await self._reply_locally(stub_uuid, retracted_id, error=retract_error)
            if stub_uuid not in self._resource_subscriptions.get(uri, set()):
                # Nothing granted yet: the retract settles everything —
                # the sentinelized pending releases any late grant, so
                # the unsubscribe is truthfully answered locally.
                await self._reply_locally(stub_uuid, original_id, result={})
                return True
        # A stub that holds NO grant for this URI must be answered
        # locally, never forwarded: the server adjudicates an
        # unsubscribe by CALLER, so a non-holder's forwarded frame
        # would remove a same-caller HOLDER's live lease while that
        # holder stayed locally routed — updates silently stop. (The
        # retract arm above already handled the in-flight-subscribe
        # case; this covers a stub with nothing outstanding at all.)
        if stub_uuid not in self._resource_subscriptions.get(uri, set()):
            await self._reply_locally(stub_uuid, original_id, result={})
            return True
        # Two stubs claimed to one session hold ONE per-caller grant
        # upstream: forwarding either stub's unsubscribe would destroy
        # the survivor's subscription while it stays locally routed.
        # When another routed stub shares this stub's grant caller,
        # settle locally — drop only this stub — and leave the upstream
        # lease to the last sharer (same guard the detach path applies).
        grant_caller = self._grant_callers.get((uri, stub_uuid))
        if grant_caller is not None and any(
            other != stub_uuid and self._grant_callers.get((uri, other)) == grant_caller
            for other in self._resource_subscriptions.get(uri, set())
        ):
            self._grant_callers.pop((uri, stub_uuid), None)
            subscribers = self._resource_subscriptions.get(uri)
            if subscribers is not None:
                subscribers.discard(stub_uuid)
                if not subscribers:
                    del self._resource_subscriptions[uri]
            await self._reply_locally(stub_uuid, original_id, result={})
            return True
        # Sole holder (or unknown grant): forward, and mutate routing on
        # the response.
        return False
    if uri in self._lease_awaiting_grant:
        # Retract while the grant is in flight. Every subscribe id this
        # stub has outstanding for the URI is ANSWERED (with a
        # cancellation error) before its parking or pending is removed or
        # reassigned — a request silently dropped from the tables would
        # hang in the client's id table forever. A parked rider is then
        # unparked; the FORWARDER's pending is promoted to the first
        # remaining rider, or converted to the lease sentinel so a
        # granted-but-unwanted lease is released.
        retract_error = {
            "code": _JSONRPC_SERVER_ERROR,
            "message": "resources/subscribe retracted by a later unsubscribe",
        }
        riders = self._lease_pending_riders.get(uri, [])
        retracted = [r for r in riders if r[0] == stub_uuid]
        self._lease_pending_riders[uri] = [r for r in riders if r[0] != stub_uuid]
        # EVERY table transition is committed before the first reply is
        # awaited: a reply into a full inbox detaches the stub, and
        # detach both mutates the pending table mid-iteration and
        # re-decides the very promotion this arm is making — the late
        # reassignment would overwrite detach's promoted rider with the
        # sentinel, leaving that rider's subscribe unanswered forever.
        forwarder_reply_id: Any = None
        forwarder_retracted = False
        for p in self._pending_requests.values():
            if (
                p.stub_uuid == stub_uuid
                and p.method == _RESOURCES_SUBSCRIBE_METHOD
                and p.resource_uri == uri
            ):
                forwarder_retracted = True
                forwarder_reply_id = p.original_id
                remaining = self._lease_pending_riders.get(uri, [])
                if remaining:
                    p.stub_uuid, p.original_id = remaining.pop(0)
                else:
                    p.origin_stub = stub_uuid
                    p.stub_uuid = _RELEASE_STUB_SENTINEL
                    p.original_id = None
                break
        for _retracted_uuid, retracted_id in retracted:
            await self._reply_locally(stub_uuid, retracted_id, error=retract_error)
        if forwarder_retracted:
            await self._reply_locally(stub_uuid, forwarder_reply_id, error=retract_error)
        await self._reply_locally(stub_uuid, original_id, result={})
        return True
    parked_replacements = [
        r for r in self._lease_replacement_subscribes.get(uri, []) if r[0] == stub_uuid
    ]
    if parked_replacements:
        # This stub parked a replacement subscribe behind an in-flight
        # release and now unsubscribes: leaving the parking in place
        # would let the release drain re-subscribe a stub that has
        # since asked to leave. Retract the parking (committed before
        # any reply is awaited), answer each parked subscribe id with
        # the cancellation error, then fall through — the routing
        # checks below answer the unsubscribe itself truthfully.
        remaining_repl = [r for r in self._lease_replacement_subscribes[uri] if r[0] != stub_uuid]
        if remaining_repl:
            self._lease_replacement_subscribes[uri] = remaining_repl
        else:
            del self._lease_replacement_subscribes[uri]
        repl_retract_error = {
            "code": _JSONRPC_SERVER_ERROR,
            "message": "resources/subscribe retracted by a later " "unsubscribe",
        }
        for _parked_uuid, parked_id in parked_replacements:
            await self._reply_locally(stub_uuid, parked_id, error=repl_retract_error)
    subscribers = self._resource_subscriptions.get(uri)
    if subscribers is not None and stub_uuid not in subscribers:
        # A stub unsubscribing a URI it never held must not tear down a
        # co-tenant's live lease.
        await self._reply_locally(stub_uuid, original_id, result={})
        return True
    if subscribers is not None and len(subscribers) > 1:
        subscribers.discard(stub_uuid)
        await self._reply_locally(stub_uuid, original_id, result={})
        return True
    if uri in self._lease_awaiting_release:
        # A final release for this URI is already in flight — and it may
        # belong to a stub that has since detached, so a refusal would
        # dead-end this stub: still routed, told to retry by a message
        # nothing acts on. Park behind the one in-flight release instead
        # (mirroring how the grant side parks riders) and settle on its
        # verdict. Forwarding a second release would race two responses
        # against one shared awaiting-release flag. Parkings are counted
        # against the same per-stub cap as subscriptions — a repeated
        # unsubscribe under a slow server must not grow the waiter list
        # without bound.
        parked = sum(
            1
            for waiters in self._lease_release_waiters.values()
            for waiter_uuid, _waiter_id in waiters
            if waiter_uuid == stub_uuid
        )
        if parked >= _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB:
            # The URI is deliberately NOT logged: resource URIs can
            # carry credentials.
            logger.warning(
                "backend pid=%s stub=%s at release-waiter cap (%d); " "refusing unsubscribe",
                self.pid,
                stub_uuid,
                _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB,
            )
            await self._reply_locally(
                stub_uuid,
                original_id,
                error={
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resource subscription limit reached",
                },
            )
            return True
        self._lease_release_waiters.setdefault(uri, []).append((stub_uuid, original_id))
        return True
    if subscribers is None:
        # Nothing tracked for this URI and no release in flight: there
        # is no lease to protect, so the server answers the stray
        # unsubscribe truthfully itself (forwarded unflagged — its
        # response mutates no lease state).
        return False
    # Last subscriber: release the lease upstream. Routing keeps the stub
    # until the server confirms (see _on_resource_subscription_response).
    self._lease_awaiting_release.add(uri)
    return False


async def _on_resource_subscription_response(
    self: Backend,
    pending: "_PendingRequest",
    msg: dict[str, Any],
    *,
    verdict_unknown: bool = False,
) -> None:
    """Apply the lease transition a subscribe/unsubscribe RESPONSE
    confirms or refuses. The wire is one ordered stream, so transitions
    arrive in the order their requests were forwarded.

    ``verdict_unknown`` (see :meth:`_settle`): *msg* is the gateway's
    error for a reply it could not use, so a subscribe is released as
    an unsettled verdict, never read as a refusal.
    """
    uri = pending.resource_uri
    ok = _is_success_response(msg)
    if pending.method == _RESOURCES_SUBSCRIBE_METHOD:
        if self.supports_caller_identity:
            # Per-stub grant: only the caller the server accepted routes.
            if ok:
                self._resource_subscriptions.setdefault(uri, set()).add(pending.stub_uuid)
                self._orphaned_leases.discard(uri)
                if pending.caller is not None:
                    # Grant-time is the only moment the grant principal
                    # is certain; detach releases with this caller.
                    self._grant_callers[(uri, pending.stub_uuid)] = pending.caller
            elif verdict_unknown or "error" not in msg:
                # UNSETTLED verdict (neither result nor error, or the
                # gateway's error for a reply it could not use): the
                # subscribe may well have taken upstream, and recording
                # nothing would strand a live per-caller lease firing
                # updates that route nowhere. Release it as the caller
                # that took it — the same fail-closed release the
                # coalesced arms apply to malformed verdicts.
                await self._release_upstream_subscriptions([uri], caller=pending.caller)
            return
        # Coalesced grant: the forwarder and every parked rider settle on
        # the server's one verdict — success grants all of them, refusal
        # grants none, and no rider was ever told "subscribed" early.
        self._lease_awaiting_grant.discard(uri)
        riders = self._lease_pending_riders.pop(uri, [])
        if ok:
            # Commit the whole grant BEFORE any reply is awaited: a reply
            # into a full inbox detaches its stub, and when that empties
            # the entry detach deletes it from the table — later adds
            # through the stale set alias would then grant riders into a
            # set the routing table does not hold.
            granted = self._resource_subscriptions.setdefault(uri, set())
            granted.add(pending.stub_uuid)
            granted.update(rider_uuid for rider_uuid, _ in riders)
            self._orphaned_leases.discard(uri)
            for rider_uuid, rider_id in riders:
                await self._reply_locally(rider_uuid, rider_id, result={})
            return
        error_obj = msg.get("error")
        # A refusal proves the server holds no lease; a MALFORMED frame
        # (no error either) proves nothing — the subscribe may have
        # taken, and denying routing while keeping it would strand a
        # live lease whose every update is charged to the server as a
        # hazard. An unsettled verdict with nobody routed releases it.
        if (error_obj is None or verdict_unknown) and uri not in self._resource_subscriptions:
            await self._release_upstream_subscriptions([uri])
        for rider_uuid, rider_id in riders:
            await self._reply_locally(
                rider_uuid,
                rider_id,
                error=(
                    error_obj
                    if isinstance(error_obj, dict)
                    else {
                        "code": _JSONRPC_SERVER_ERROR,
                        "message": "resources/subscribe refused by server",
                    }
                ),
            )
        return
    # --- unsubscribe response ---
    if self.supports_caller_identity:
        if ok:
            self._grant_callers.pop((uri, pending.stub_uuid), None)
            subscribers = self._resource_subscriptions.get(uri)
            if subscribers is not None:
                subscribers.discard(pending.stub_uuid)
                if not subscribers:
                    del self._resource_subscriptions[uri]
        return
    if uri not in self._lease_awaiting_release:
        return
    self._lease_awaiting_release.discard(uri)
    waiters = self._lease_release_waiters.pop(uri, [])
    if ok:
        # Release confirmed: drop the releasing stub and every parked
        # waiter — all committed BEFORE any reply is awaited. A server
        # may answer concurrent requests out of order, so a replacement
        # subscribe forwarded behind this release can have been granted
        # already — deleting the whole entry would silently discard that
        # fresh grant.
        subscribers = self._resource_subscriptions.get(uri)
        if subscribers is not None:
            subscribers.discard(pending.stub_uuid)
            for waiter_uuid, _waiter_id in waiters:
                subscribers.discard(waiter_uuid)
            if not subscribers:
                del self._resource_subscriptions[uri]
        for waiter_uuid, waiter_id in waiters:
            await self._reply_locally(waiter_uuid, waiter_id, result={})
        await self._drain_replacement_subscribes(uri, released=True)
        return
    # On a refusal the server RETAINED the subscription, so routing keeps
    # the entry; the unsubscribing stub receives the server's error and
    # knows its unsubscribe did not take effect — and so does every
    # parked waiter, which stays routed and may retry (the flag is down,
    # so a retry forwards a fresh release).
    error_obj = msg.get("error")
    for waiter_uuid, waiter_id in waiters:
        await self._reply_locally(
            waiter_uuid,
            waiter_id,
            error=(
                error_obj
                if isinstance(error_obj, dict)
                else {
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resources/unsubscribe refused by server",
                }
            ),
        )
    await self._drain_replacement_subscribes(uri, released=False)


def resource_subscription_uris(self: Backend, stub_uuid: str) -> list[str]:
    """URIs whose routing table names ``stub_uuid`` — the set a
    transparent respawn must replay onto the replacement backend."""
    routed = {
        uri for uri, subscribers in self._resource_subscriptions.items() if stub_uuid in subscribers
    }
    # An in-flight replay has not committed routing yet (routing is
    # recorded only on the server's grant): if the replacement dies
    # before its replay responses arrive, those URIs are in neither the
    # routing table nor any pending the next respawn can see — captured
    # from pendings here, or the subscription goes permanently dark.
    routed.update(
        p.resource_uri
        for p in self._pending_requests.values()
        if p.replay_stub == stub_uuid and p.method == _RESOURCES_SUBSCRIBE_METHOD and p.resource_uri
    )
    # URIs whose replay was in flight when THIS backend died: the
    # backend-gone cleanup preserved them here because clearing the
    # pending table erased their only record.
    routed.update(self._gone_replay_uris.get(stub_uuid, ()))
    return sorted(routed)


async def replay_resource_subscriptions(
    self: Backend,
    stub_uuid: str,
    uris: list[str],
    *,
    caller: Optional[CallerContext] = None,
) -> None:
    """Re-establish ``stub_uuid``'s subscriptions on a freshly respawned
    backend, so a live subscription survives the transparent backend swap
    instead of silently going dark (kiro-cli never learns the old backend
    died, so it will never re-subscribe on its own).

    Gateway-originated: each URI is re-subscribed under the lease
    sentinel and the stub's routing grant is recorded only on the
    server's success (a refusal leaves the update undelivered —
    fail-closed — rather than mis-attributed). On an identity-capable
    server the replay carries the connection's authoritative caller
    block, so the server adjudicates it as the same caller that held the
    original subscription; without a caller to inject there the replay is
    skipped entirely, because a bare subscribe would be adjudicated as
    the connection and could be refused — again failing closed.

    A WRITE failure raises :class:`BackendGone` (the replacement's pipe
    is already broken, so the respawn cannot succeed): swallowing it
    would report a successful respawn whose subscriptions are silently
    dark forever. Pendings for URIs written before the failure are
    scoped to release-on-grant — with the respawn failed nobody will
    consume those grants, so each late success releases its lease
    instead of pinning it to a stub the caller is tearing down.
    """
    if self.supports_caller_identity and caller is None:
        logger.debug(
            "backend pid=%s skipping subscription replay for stub=%s: "
            "identity-capable server and no caller context to inject",
            self.pid,
            stub_uuid,
        )
        return
    replay_generation = self._rekey_generation.get(stub_uuid, 0)
    for uri in uris:
        if self._rekey_generation.get(stub_uuid, 0) != replay_generation:
            # A claim rekeyed this stub mid-replay. The eviction
            # retracted the pendings written so far, but continuing
            # would write the REMAINING URIs under the old owner and
            # route their grants to the new one. Stop here — fail
            # closed, the new owner subscribes on its own.
            logger.debug(
                "backend pid=%s stopping subscription replay for "
                "stub=%s: stub was rekeyed mid-replay",
                self.pid,
                stub_uuid,
            )
            return
        if stub_uuid in self._resource_subscriptions.get(uri, set()):
            continue
        if not self.supports_caller_identity:
            # Identity-less: the server holds ONE subscription per URI,
            # so a replay flows through the same per-URI lease
            # coordination as a stub subscribe — two uncoordinated
            # replays would put two grants in flight, and a later final
            # unsubscribe would then release the server's single
            # subscription while the other stub stayed locally routed.
            # A replay parking carries no reply id (_reply_locally
            # no-ops on None), so it settles silently on the verdict.
            subscribers = self._resource_subscriptions.get(uri)
            if uri in self._lease_awaiting_grant:
                self._lease_pending_riders.setdefault(uri, []).append((stub_uuid, None))
                continue
            if subscribers is not None and uri not in self._lease_awaiting_release:
                subscribers.add(stub_uuid)
                continue
            if uri in self._lease_awaiting_release:
                self._lease_replacement_subscribes.setdefault(uri, []).append((stub_uuid, None))
                continue
            self._lease_awaiting_grant.add(uri)
        fid = self._next_forward_id()
        self._pending_requests[fid] = _PendingRequest(
            stub_uuid=_RELEASE_STUB_SENTINEL,
            original_id=None,
            method=_RESOURCES_SUBSCRIBE_METHOD,
            resource_uri=uri,
            replay_stub=stub_uuid,
            caller=caller,
            t_start_ms=time.monotonic() * 1000.0,
        )
        replay_msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": fid,
            "method": _RESOURCES_SUBSCRIBE_METHOD,
            "params": {"uri": uri},
        }
        if self.supports_caller_identity and caller is not None:
            replay_msg = _inject_caller_meta(replay_msg, caller)
        try:
            await _write_json_line(self.stdin, replay_msg)
        except Exception as exc:
            self._pending_requests.pop(fid, None)
            if not self.supports_caller_identity:
                # Unwind the lease claim so the URI is not wedged
                # awaiting a grant that was never written.
                self._lease_awaiting_grant.discard(uri)
            # Scope any EARLIER replay pendings written in this pass to
            # release-on-grant: their responses may still arrive, and
            # with the respawn reported failed nobody consumes the
            # grants — an attached replay_stub would pin each lease to
            # a stub the caller is about to tear down.
            for p in self._pending_requests.values():
                if (
                    p.stub_uuid == _RELEASE_STUB_SENTINEL
                    and p.method == _RESOURCES_SUBSCRIBE_METHOD
                    and p.replay_stub == stub_uuid
                ):
                    p.replay_stub = ""
            # No URI in the line: resource URIs can carry credentials.
            logger.debug(
                "backend pid=%s could not replay a resource " "subscription (write failed)",
                self.pid,
            )
            raise BackendGone(
                "subscription replay write failed on the replacement " "backend"
            ) from exc


def _stub_subscription_count(self: Backend, stub_uuid: str) -> int:
    """Subscription load ``stub_uuid`` holds or is acquiring: confirmed
    table grants, its own forwarded subscribes still awaiting the server,
    and rider parkings — each counted INDIVIDUALLY, duplicates included.
    Grants land on the response, so distinct-URI counting would let a
    stub repeat one URI's subscribe unboundedly while the grant is in
    flight, growing the rider list without limit under a slow server."""
    confirmed = sum(
        1 for subscribers in self._resource_subscriptions.values() if stub_uuid in subscribers
    )
    in_flight = sum(
        1
        for p in self._pending_requests.values()
        if p.stub_uuid == stub_uuid and p.method == _RESOURCES_SUBSCRIBE_METHOD and p.resource_uri
    )
    # Subscribes this stub originated that were since converted to the
    # lease sentinel (retracted by an unsubscribe, an eviction, or a
    # detach) but whose server response is still outstanding. Without
    # these a stub cycling subscribe/unsubscribe against an
    # unresponsive server would slip every cycle's pending out of its
    # count and grow the table without bound.
    sentinel_retained = sum(
        1
        for p in self._pending_requests.values()
        if p.stub_uuid == _RELEASE_STUB_SENTINEL
        and p.origin_stub == stub_uuid
        and p.method == _RESOURCES_SUBSCRIBE_METHOD
        and p.resource_uri
    )
    parked = sum(
        1
        for riders in self._lease_pending_riders.values()
        for rider_uuid, _rider_id in riders
        if rider_uuid == stub_uuid
    )
    replacement = sum(
        1
        for parkers in self._lease_replacement_subscribes.values()
        for parker_uuid, _parker_id in parkers
        if parker_uuid == stub_uuid
    )
    return confirmed + in_flight + sentinel_retained + parked + replacement


async def _drain_replacement_subscribes(self: Backend, uri: str, *, released: bool) -> None:
    """Settle subscribes that parked while ``uri``'s release was in
    flight. On a CONFIRMED release the lease is gone, so the parkers
    take it afresh with ONE forwarded subscribe — first parker as the
    forwarder, the rest parked as riders settling on the same verdict —
    serialized strictly AFTER the release on the wire. On a refusal the
    server RETAINED the lease, so parkers join the still-live entry
    locally (state committed before replies); when detach has pruned the
    entry out from under a retained lease, a fresh subscribe is
    forwarded instead (idempotent on the server's live subscription, and
    its grant re-establishes routing).
    """
    parked = self._lease_replacement_subscribes.pop(uri, [])
    if not parked:
        return
    subscribers = self._resource_subscriptions.get(uri)
    if not released and subscribers is not None:
        subscribers.update(parker_uuid for parker_uuid, _ in parked)
        for parker_uuid, parker_id in parked:
            await self._reply_locally(parker_uuid, parker_id, result={})
        return
    # Take the lease afresh with ONE subscribe forwarded under the
    # sentinel, every parker riding on its verdict: success grants and
    # answers each parker ({} for a stub subscribe, silently for an
    # id-less replay parking), refusal answers each with the server's
    # error. The sentinel arm owns exactly this settle already.
    fid = self._next_forward_id()
    self._pending_requests[fid] = _PendingRequest(
        stub_uuid=_RELEASE_STUB_SENTINEL,
        original_id=None,
        method=_RESOURCES_SUBSCRIBE_METHOD,
        resource_uri=uri,
        t_start_ms=time.monotonic() * 1000.0,
    )
    self._lease_awaiting_grant.add(uri)
    self._lease_pending_riders.setdefault(uri, []).extend(parked)
    try:
        await _write_json_line(
            self.stdin,
            {
                "jsonrpc": "2.0",
                "id": fid,
                "method": _RESOURCES_SUBSCRIBE_METHOD,
                "params": {"uri": uri},
            },
        )
    except Exception:
        # Backend going away: unwind and answer every parker — an id
        # silently dropped from the tables hangs in the client forever.
        self._pending_requests.pop(fid, None)
        self._lease_awaiting_grant.discard(uri)
        remaining = [r for r in self._lease_pending_riders.get(uri, []) if r not in parked]
        if remaining:
            self._lease_pending_riders[uri] = remaining
        else:
            self._lease_pending_riders.pop(uri, None)
        err = {
            "code": _JSONRPC_SERVER_ERROR,
            "message": "resources/subscribe could not be forwarded " "(backend going away)",
        }
        for parker_uuid, parker_id in parked:
            await self._reply_locally(parker_uuid, parker_id, error=err)


async def _release_upstream_subscriptions(
    self: Backend,
    uris: list[str],
    *,
    caller: Optional[CallerContext] = None,
) -> None:
    """Send a gateway-originated ``resources/unsubscribe`` for each URI
    whose last subscriber departed without unsubscribing. Best-effort: a
    backend that is already dying takes its subscriptions with it.

    On an identity-capable server pass the caller that HOLDS the grant:
    the release is adjudicated per principal, so a bare unsubscribe
    (adjudicated as the connection) would be refused — or worse, revoke
    a different principal's live subscription.
    """
    for uri in uris:
        fid = self._next_forward_id()
        self._pending_requests[fid] = _PendingRequest(
            stub_uuid=_RELEASE_STUB_SENTINEL,
            original_id=None,
            method=_RESOURCES_UNSUBSCRIBE_METHOD,
            resource_uri=uri,
            t_start_ms=time.monotonic() * 1000.0,
        )
        if not self.supports_caller_identity:
            # Mark the release in flight BEFORE writing: without the
            # flag a replacement subscribe takes the first-subscriber
            # path and forwards BESIDE this release, racing its answer
            # order — the same silent update loss the stub-forwarded
            # release path parks against. The sentinel response arm
            # clears the flag and drains any parked replacements on the
            # server's verdict. Identity-capable servers never coalesce
            # (every subscribe forwards per caller), so the flag is not
            # consulted there and setting it would only go stale.
            self._lease_awaiting_release.add(uri)
        release_msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": fid,
            "method": _RESOURCES_UNSUBSCRIBE_METHOD,
            "params": {"uri": uri},
        }
        if self.supports_caller_identity and caller is not None:
            release_msg = _inject_caller_meta(release_msg, caller)
        try:
            await _write_json_line(self.stdin, release_msg)
        except Exception:
            # Per-URI, not terminal: one failed write must not leak every
            # remaining server-side lease (the pipe may also be gone, in
            # which case the dying backend takes them all anyway).
            self._pending_requests.pop(fid, None)
            self._lease_awaiting_release.discard(uri)
            # The lease is now RETAINED upstream through the broker's
            # own failure: mark it orphaned so its later updates are
            # dropped without charging a hazard to a blameless server.
            while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                self._orphaned_leases.pop()
            self._orphaned_leases.add(uri)
            # No URI in the line: resource URIs can carry credentials.
            logger.debug(
                "backend pid=%s could not release a resource " "subscription (backend going away)",
                self.pid,
            )
            continue


def _resource_update_targets(self: Backend, msg: dict[str, Any]) -> set[str]:
    """Return the stubs subscribed to an incoming
    ``notifications/resources/updated``'s URI — empty when the URI is
    missing, malformed, or has no subscribers (the caller then applies
    the deny-by-default drop).

    Returns a COPY: delivery awaits per stub, and a full inbox detaches
    its stub mid-fan-out, which mutates the underlying table.
    """
    params = msg.get("params")
    if not isinstance(params, dict):
        return set()
    uri = params.get("uri")
    if not isinstance(uri, str) or not uri:
        return set()
    return set(self._resource_subscriptions.get(uri, ()))

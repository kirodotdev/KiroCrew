"""The checks every Instances handler runs before it acts, and the registry it acts on.

The owner-only gate (:func:`_guard`), the SEL audit record, registry access and the
status view, and the chain policy every add and removal of a chained crew goes through
(the chain-mutation lock and :func:`_chain_refusal`).

Owner of :mod:`kiro_crew.dashboard.handlers_instances`, which is its only import path:
callers and tests reach every name here as ``handlers_instances.<name>``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.instances.registry import (
    MAX_CHAINED_PER_PARENT,
    MAX_VIA_HOPS,
    InstancesRegistry,
    ancestor_ids,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers_instances")


# Per-string ceiling for fields in a PEER's federated-search reply. A peer is
# untrusted input (see api_instances_search_sessions), and without a clamp a
# hostile or broken remote could ship megabyte titles/snippets straight to the
# browser — and feed the redaction regexes unbounded input on the way. Sized
# well above any honest value (local snippets are a short match window and
# titles are one line) so it only ever bites on garbage.
_PEER_FIELD_MAX_CHARS = 2048


# Serializes the registry mutations that read the chain and then write it. A
# chained add validates its parent and inserts the child in two separate awaits;
# a removal snapshots the subtree and deletes it in several more. Interleaved,
# the add's row lands after the removal's snapshot was taken and outlives the
# parent it names -- a crew whose only route is a hop that is gone.
# Every add and every removal takes this, so the pair is atomic rather than
# merely quick. Operator-paced actions, so the serialization costs nothing a user
# can perceive, and nothing under it calls back into these handlers.
# `LoopBoundLock`, not a bare `asyncio.Lock`: a module global binds to the first
# loop that acquires it and then refuses every other one.
_CHAIN_MUTATION_LOCK = LoopBoundLock()


def _audit(operation: str, outcome: str, *, request_id: str = "", error: str = "") -> None:
    """Emit a SEL audit event for an instances control-plane action."""
    try:
        sel().log_tool_invocation(
            session_key="dashboard:instances",
            tool_name=f"instances_{operation}",
            outcome=outcome,
            request_id=request_id,
            source="dashboard",
            error=error,
        )
    except Exception:  # audit must never break the request path
        logger.debug("SEL audit failed for instances_%s", operation, exc_info=True)


# The addressing fields Stop/Start/Delete resolve the real EC2 stack through
# (see coordsOf() in RemoteCrewPanel.tsx). Locked from PATCH for a correlated
# cloud instance — see _is_correlated_cloud_instance().
_ADDRESSING_FIELDS = {"connection_method", "ssm_target", "aws_profile", "aws_region"}


def _is_correlated_cloud_instance(ssm_target: str) -> bool:
    """True if *ssm_target* was provisioned by a Kiro Crew cloud launch.

    Deferred import: this is the one place the instances feature reaches into
    the cloud module, kept lazy so instances stays usable with the cloud
    module unavailable/import-broken (mirrors register_instance()'s own
    best-effort posture in cloud/connect.py). Only the import itself is
    best-effort (``ImportError`` -> not correlated, the "cloud feature
    absent" case) — a launch-job STORE read failure inside
    ``is_launched_instance()`` is a different failure mode and is NOT caught
    here, so it propagates to ``api_instances_update``, which fails the PATCH
    CLOSED rather than silently treating a possibly-launched instance as
    uncorrelated.
    """
    try:
        from kiro_crew.cloud.connect import is_launched_instance
    except ImportError:  # pragma: no cover - cloud feature absent
        return False
    return is_launched_instance(ssm_target)


def _is_slack_origin(request: web.Request) -> bool:
    """True if the request arrived via the Slack path (X-Session-Key 'slack:*')."""
    sk = request.headers.get("X-Session-Key", "")
    return sk.startswith("slack:")


def _guard(request: web.Request, operation: str) -> web.Response | None:
    """Shared gate: enabled-check + owner-only (non-Slack). Returns a denial
    Response to short-circuit, or None to proceed. Audits every denial."""
    if _is_slack_origin(request):
        _audit(operation, "denied", error="slack-origin rejected")
        return web.json_response(
            {"error": "instances control plane is owner-only (not reachable via Slack)"},
            status=403,
        )
    # Deny-by-default in two steps, because ``request["user"]`` proves
    # AUTHENTICATED and nothing more: ``token_auth`` publishes it for any valid
    # dashboard token, and the messaging transports mint such a token per
    # allow-listed user, so its presence alone admits a non-owner subject to this
    # SSH-pivoting control plane. Step one refuses an unauthenticated caller;
    # step two demands the positive owner identity, which is the same predicate
    # the capabilities, federated-search, chat-slot and proxy routes of
    # ``handlers_instances`` apply for the same reason.
    if not request.get("user"):
        _audit(operation, "denied", error="unauthenticated (no owner identity)")
        return web.json_response(
            {"error": "authentication required (owner-only control plane)"},
            status=401,
        )
    if not is_owner_dashboard_request(request):
        _audit(operation, "denied", error="non-owner identity rejected")
        return _owner_denial_response(request)
    cfg = KiroCrewConfig.load()
    if not cfg.instances.enabled:
        _audit(operation, "denied", error="feature disabled")
        # Carries a ``code`` because this is the ONE denial on this route the
        # dashboard treats as routine: it is expected on every install that has
        # not turned the feature on, so the SPA opts it out of the error journal
        # rather than reporting it.  The opt-out is keyed on this code, not on the
        # 403 -- the owner-only and Slack-origin denials above share that status
        # and are real authorization failures a reader must still see.
        return web.json_response(
            {
                "error": "instances feature is disabled (set instances.enabled=true)",
                "code": "instances_disabled",
            },
            status=403,
        )
    return None


#: The one answer every route gives when the feature is enabled but no tunnel
#: manager exists. ``instances.enabled`` is a restart-marked field and the
#: manager is built only by the gateway's startup hook, so turning the flag on in
#: a running gateway passes ``_guard`` yet leaves ``instances_manager`` unset
#: until a restart. The message names that fix; the code lets the dashboard say
#: it without parsing prose.
MANAGER_UNAVAILABLE_CODE = "instances_manager_unavailable"
MANAGER_UNAVAILABLE_MESSAGE = (
    "instances manager not running: restart the gateway (kirocrew restart) to start remote crews"
)


def _manager_unavailable_response() -> web.Response:
    """The 503 for a request that reached a route with no instances manager."""
    return web.json_response(
        {"error": MANAGER_UNAVAILABLE_MESSAGE, "code": MANAGER_UNAVAILABLE_CODE},
        status=503,
    )


def _registry(state: "DashboardState"):
    """Return the live InstancesRegistry, creating a standalone one if needed."""
    reg = getattr(state, "instances_registry", None)
    if reg is None:
        reg = InstancesRegistry()
        state.instances_registry = reg
    return reg


def _apply_update(reg, instance_id: str, changes: dict) -> object:
    """Write *changes* to *instance_id*. Blocking — callers offload it.

    Module level on purpose: the registry write must never run on the event loop,
    and a closure defined inside the async handler reads (to a human and to the
    AST ratchet in ``test_apps_instances_loop_offload``) as a call on the loop
    even when every caller hands it to a thread.
    """
    return reg.update(instance_id, **changes)


def _status_for(state: "DashboardState", instance_id: str) -> dict:
    """Live tunnel status dict for an instance, or a disconnected default.

    When there is no live tunnel but the manager retained a failure reason from
    a failed connect/reconnect (e.g. a startup auto-revive that couldn't reach
    the host), report an ``error`` state carrying that reason so a sticky tab
    can show *why* it is down rather than a bare "disconnected".
    """
    mgr = getattr(state, "instances_manager", None)
    if mgr is not None:
        st = mgr.status(instance_id)
        if st is not None:
            d = st.to_dict()
            # Surface token TTL remaining (lives on the manager, not the status).
            ttl = mgr.token_ttl_remaining(instance_id)
            if ttl is not None:
                d["token_ttl_remaining"] = ttl
            # And the total it was issued for. The remaining is a countdown from
            # THIS number, so a reader measuring how far a token has run must
            # divide by it: a chained crew's token is issued by its parent, and
            # the row's own TTL is a different figure entirely.
            total = mgr.token_ttl_total(instance_id)
            if total is not None:
                d["token_ttl_total"] = total
            return d
        last_err = mgr.last_error(instance_id)
        if last_err:
            return {"instance_id": instance_id, "state": "error", "error": last_err}
    return {"instance_id": instance_id, "state": "disconnected"}


def _instance_view(state: "DashboardState", inst) -> dict:
    """Registry record + live status, merged for the Manage panel. No token."""
    view = inst.to_dict()
    view["status"] = _status_for(state, inst.id)
    return view


async def _chain_refusal(
    reg, via_instance_id: str, via_remote_id: str = "", name: str = ""
) -> dict | None:
    """Why this gateway will not chain behind *via_instance_id*, or ``None``.

    Server-side, and the only authority on it: the frontend shows this reason but
    never decides it, because the request can come from inside an embedded pane
    whose code the hub does not control.

    Five refusals, each naming what the user has to change:

    * the named crew is not configured here — nothing to ride;
    * that crew ALREADY has a row for this remote crew. One row per remote crew
      is an invariant about stored state, so it is settled here rather than by
      the announcing pane: the pane decides from a list it refreshes only after
      the add and the connect it triggers, so a second announcement arriving
      inside that multi-second window reads a list without the first row and asks
      for a duplicate -- which the registry would accept under a suffixed id,
      giving one remote crew two rows, two forwards and two tabs;
    * riding it would make the chain deeper than :data:`MAX_VIA_HOPS`. This is
      the depth cap, and it is checked BEFORE any tunnel is opened;
    * that parent already carries :data:`MAX_CHAINED_PER_PARENT` crews. Depth
      and width are separate bounds: a chain of legal depth can still be added
      without end, and these rows come from the parent's own pane rather than
      from anyone at this dashboard;
    * the named crew is reached over SSM, whose forwarder takes no second local
      forward from this gateway.

    The hop PORT is left to the registry's own validator: it is a shape check on
    one field, and a copy here would be a second place to widen.

    Every refusal is a whole sentence naming the crew and the action. The panel
    renders this text raw and it can land after the connect already looked like it
    succeeded, so a subjectless clause left the reader unable to tell what was
    refused. ``name`` is the announced crew's name; the caller holds it, this
    function only sees ids.
    """
    subject = f"crew {name}" if name else "that crew"
    instances = await asyncio.to_thread(reg.list)
    parent = next((i for i in instances if i.id == via_instance_id), None)
    if parent is None:
        return {
            "error": (
                f"Connecting {subject} needs a crew with id {via_instance_id!r} to "
                f"reach it through, and none is configured here."
            ),
            "code": "chain_parent_unknown",
        }
    # Checked before the caps and the depth: if this crew is already here, none of
    # those questions is the true answer, and "already added" is both the least
    # alarming thing to say and the only one that is correct.
    if via_remote_id:
        already = next(
            (
                i
                for i in instances
                if i.via_instance_id == via_instance_id and i.via_remote_id == via_remote_id
            ),
            None,
        )
        if already is not None:
            return {
                "error": (
                    f"Connecting {subject} is unnecessary: it is already configured "
                    f"here as {already.name!r}, reached through {parent.name}."
                ),
                "code": "chain_duplicate",
            }
    # Hops counted from THIS gateway: one to reach the parent, one more for every
    # crew the parent itself rides through, and one for the new record. So a crew
    # chained behind a top-level crew is 2 — the cap — and one chained behind THAT
    # is 3, which is refused. The count is HOPS and not machines in between: it
    # includes the link to the new crew itself, so a refused 3 is two intermediary
    # machines, and the message says hops for that reason.
    hops = 2 + len(ancestor_ids(instances, parent.id))
    if hops > MAX_VIA_HOPS:
        return {
            "error": (
                f"Connecting {subject} through {parent.name} would put it {hops} hops "
                f"from this dashboard, and {MAX_VIA_HOPS} is the limit. Connect it "
                f"from a dashboard closer to it."
            ),
            "code": "chain_too_deep",
        }
    # Counted over this parent's DIRECT children, which is the population the
    # parent's pane can grow: one announcement adds one row naming that parent.
    # Crews further down belong to their own parent's count.
    riding = sum(1 for i in instances if i.via_instance_id == parent.id)
    if riding >= MAX_CHAINED_PER_PARENT:
        return {
            "error": (
                f"Connecting {subject} is refused because crew {parent.name} already "
                f"carries {riding} chained crews, and {MAX_CHAINED_PER_PARENT} is the limit. "
                f"Remove one, or connect it from a dashboard closer to it."
            ),
            "code": "chain_parent_full",
        }
    parent_method = (parent.connection_method or "ssh").strip().lower()
    if parent_method != "ssh":
        return {
            "error": (
                f"Connecting {subject} is refused because crew {parent.name} is reached "
                f"over {parent_method}, and a further crew can only be chained through an "
                f"ssh hop."
            ),
            "code": "chain_parent_not_ssh",
        }
    return None

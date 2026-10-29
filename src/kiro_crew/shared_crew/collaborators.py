"""Who counts as a Shared Crew collaborator, and the question-card audience.

A collaborator is the dashboard owner, or a dashboard-user request whose
verified actor is a person the operator admitted through an identity boundary
that enforces membership: a tailnet login on the serve path (the middleware
allowlists it and pins the token to it).
A Slack-allowlist dashboard token, an app token, an internal caller, a direct
tailnet peer (attribution only, never allowlisted) and an Unknown actor are not
collaborators, so they keep every refusal they had.

Agent questions go to every collaborator: the card frames, the decision frame
and the pending read. The owner's sockets keep receiving them through
``broadcast_ws_owners``; a collaborator socket that is not an owner socket gets
the same frame here, and nothing else the owner set receives.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.shared_crew.contracts import ActorContext, PrincipalKind, Provenance

logger = logging.getLogger(__name__)

__all__ = [
    "is_collaborator_actor",
    "is_collaborator_request",
    "broadcast_to_collaborators",
    "deliver_to_collaborators",
]

#: Provenances whose principal was admitted by an enforced membership boundary.
_MEMBER_PROVENANCES = frozenset({Provenance.TAILSCALE_SERVE, Provenance.TAILSCALE_LISTENER})


def is_collaborator_actor(actor: object) -> bool:
    """Whether *actor* is a person admitted through a membership boundary."""
    if not isinstance(actor, ActorContext):
        return False
    principal = actor.principal
    return (
        not principal.is_unknown
        and principal.kind is PrincipalKind.HUMAN
        and principal.provenance in _MEMBER_PROVENANCES
    )


def is_collaborator_request(request: Any) -> bool:
    """Whether *request* is the owner or a collaborator's own dashboard session.

    The dashboard-user claims are the ones the auth middleware sets: a PRESENT,
    empty ``app`` and ``is_dashboard_user``. An internal-secret caller is never
    a person looking at a card.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.shared_crew.identity import actor_of

    if is_owner_dashboard_request(request):
        return True
    if request.get("internal_auth") is True or request.get("app", None) != "":
        return False
    if request.get("is_dashboard_user") is not True:
        return False
    return is_collaborator_actor(actor_of(request))


def _collaborator_sockets(state: Any) -> list[Any]:
    clients = getattr(state, "_ws_clients", None)
    if not isinstance(clients, list):
        return []
    owners = getattr(state, "_owner_ws_clients", None) or set()
    return [
        ws
        for ws in list(clients)
        if not ws.closed
        and ws not in owners
        and ws.get("_is_dashboard_user", False)
        and not ws.get("_app")
        and is_collaborator_actor(ws.get("_shared_crew_actor"))
    ]


def _send_collaborators(state: Any, msg_type: str, data: dict[str, Any]) -> int:
    """Hand the frame to every non-owner collaborator socket; return how many."""
    targets = _collaborator_sockets(state)
    spawn = getattr(state, "_spawn_ws_send", None)
    if not targets or not callable(spawn):
        return 0
    msg = json.dumps({"type": msg_type, "data": data})
    sent = 0
    for ws in targets:
        try:
            spawn(ws, msg)
            sent += 1
        except Exception:
            logger.debug("collaborator WS send failed for %s", msg_type, exc_info=True)
    return sent


def broadcast_to_collaborators(state: Any, msg_type: str, data: dict[str, Any]) -> None:
    """Send a question frame to the owner sockets and every collaborator socket."""
    state.broadcast_ws_owners(msg_type, data)
    _send_collaborators(state, msg_type, data)


async def deliver_to_collaborators(state: Any, msg_type: str, data: dict[str, Any]) -> int:
    """:func:`broadcast_to_collaborators` that counts the sockets reached.

    Owner sockets are awaited (``deliver_ws_owners``); a collaborator socket counts
    once its send is handed off.
    """
    delivered = int(await state.deliver_ws_owners(msg_type, data))
    return delivered + _send_collaborators(state, msg_type, data)

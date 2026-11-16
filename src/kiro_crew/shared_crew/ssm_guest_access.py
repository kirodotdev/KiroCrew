"""An SSM invite guest acts as the Crew owner everywhere except the Terminal.

A person who redeemed the operator's SSM invite on the Session Manager listener
passes every owner gate (``source_providers.is_owner_dashboard_request``): the
Bots and their Dashboards, approval modes including Trust and YOLO, apps,
updates, settings and the rest of the owner's dashboard. The integrated
Terminal and the external-terminal launcher stay the owner's alone, because a
shell on the host is the one thing the invite does not hand over.

The guest keeps their own identity: ``request["user"]`` stays ``ssm:<invite>``,
the published actor stays their ``ssm_invite`` principal, and every audit row
names them, so admitting them to the owner's surfaces never relabels their
work as the owner's. Nobody is a guest while the Shared Crew preview is off.
"""

from __future__ import annotations

from typing import Any

__all__ = ["TERMINAL_PATH_PREFIXES", "is_ssm_guest_request", "is_terminal_request"]

#: Every route that opens, drives, lists or launches a host shell.
TERMINAL_PATH_PREFIXES = ("/api/terminal", "/api/ws/terminal")


def is_terminal_request(request: Any) -> bool:
    """Whether *request* addresses a Terminal route."""
    path = str(getattr(request, "path", "") or "")
    return any(path == p or path.startswith(p + "/") for p in TERMINAL_PATH_PREFIXES)


def is_ssm_guest_request(request: Any) -> bool:
    """Whether *request* is a person who signed in through an SSM invite.

    Read from the claims the SSM gate and the auth middleware publish, never a
    header: the gate's ``SsmGuest`` mark, an empty ``app`` claim on a
    dashboard-user session, and an actor whose principal is the ``ssm_invite``
    person for that same invite. App tokens and internal callers never are.
    """
    from kiro_crew.shared_crew.contracts import (
        ACTOR_REQUEST_KEY,
        SSM_GUEST_REQUEST_KEY,
        ActorContext,
        PrincipalKind,
        Provenance,
        SsmGuest,
    )
    from kiro_crew.shared_crew.identity import PROVIDER_SSM
    from kiro_crew.shared_crew.preview import preview_enabled

    getter = getattr(request, "get", None)
    if not callable(getter):
        return False
    if getter("internal_auth") is True or getter("app", None) != "":
        return False
    if getter("is_dashboard_user") is not True:
        return False
    guest = getter(SSM_GUEST_REQUEST_KEY)
    if not isinstance(guest, SsmGuest):
        return False
    actor = getter(ACTOR_REQUEST_KEY)
    if not isinstance(actor, ActorContext):
        return False
    principal = actor.principal
    if (
        principal.is_unknown
        or principal.kind is not PrincipalKind.HUMAN
        or principal.provider != PROVIDER_SSM
        or principal.provenance is not Provenance.SSM_INVITE
        or principal.subject != guest.invite_id.strip()
    ):
        return False
    return preview_enabled()


def ssm_guest_passes_owner_gate(request: Any) -> bool:
    """Whether the owner gate admits *request* as an SSM guest: any route but the Terminal."""
    return is_ssm_guest_request(request) and not is_terminal_request(request)

"""Claims that make a test request an admitted Shared Crew teammate's own session.

A dashboard session whose subject is neither the owner nor a teammate (a messaging
allow-list link) is refused the owner's routes. A test that means "a teammate"
publishes these claims: an SSM-invite guest, the kind the SSM gate admits, which
``shared_crew.teammate_access.is_shared_crew_teammate`` counts while the Shared
Crew preview is on. Call :func:`enable_teammates` (it sets the preview switch on
``monkeypatch``) and :func:`publish_teammate` on the request.
"""

from __future__ import annotations

from typing import Any

TEAMMATE_INVITE = "b0ae764d1587"
TEAMMATE_ALIAS = "Sam"
TEAMMATE_SUBJECT = f"ssm:{TEAMMATE_INVITE}"
RELAY_TEAMMATE = "kiro-user.teammate-ana-0002"


def enable_teammates(monkeypatch: Any) -> None:
    """Turn the Shared Crew preview on, which SSM-invite teammates need."""
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


def publish_teammate(request: Any) -> None:
    """Publish an SSM-invite teammate's dashboard session claims on *request*."""
    from kiro_crew.shared_crew.contracts import (
        ACTOR_REQUEST_KEY,
        SSM_GUEST_REQUEST_KEY,
        SSM_REQUEST_KEY,
        ActorContext,
        SsmGuest,
        Transport,
    )
    from kiro_crew.shared_crew.identity import ssm_principal

    request["app"] = ""
    request["is_dashboard_user"] = True
    request["user"] = TEAMMATE_SUBJECT
    request[SSM_REQUEST_KEY] = True
    request[SSM_GUEST_REQUEST_KEY] = SsmGuest(invite_id=TEAMMATE_INVITE, alias=TEAMMATE_ALIAS)
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=ssm_principal(TEAMMATE_INVITE, TEAMMATE_ALIAS),
        request_id="req",
        transport=Transport.HTTP,
    )


def publish_relay_teammate(request: Any) -> None:
    """Publish a Kiro relay teammate's dashboard session claims on *request*.

    The relay ingress publishes the verified caller under ``RELAY_CALLER_KEY`` and
    the actor as that teammate's ``kiro_relay`` principal
    (``shared_crew.routes_identity._relay_teammate``).
    """
    from kiro_crew.dashboard.kiro_relay.caller import (
        RELAY_CALLER_KEY,
        RelayCaller,
        RelayCallerKind,
    )
    from kiro_crew.shared_crew.contracts import (
        ACTOR_REQUEST_KEY,
        ActorContext,
        DisplaySnapshot,
        Principal,
        PrincipalKind,
        Provenance,
        Transport,
        principal_id_for,
    )
    from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO

    request["app"] = ""
    request["is_dashboard_user"] = True
    request["user"] = f"relay:{RELAY_TEAMMATE}"
    request[RELAY_CALLER_KEY] = RelayCaller(
        kiro_user_id=RELAY_TEAMMATE, kind=RelayCallerKind.TEAMMATE
    )
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=Principal(
            id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, RELAY_TEAMMATE),
            kind=PrincipalKind.HUMAN,
            provider=PROVIDER_KIRO,
            issuer=KIRO_ISSUER,
            subject=RELAY_TEAMMATE,
            provenance=Provenance.KIRO_RELAY,
            display=DisplaySnapshot(name="Ana", login=""),
        ),
        request_id="req",
        transport=Transport.HTTP,
    )

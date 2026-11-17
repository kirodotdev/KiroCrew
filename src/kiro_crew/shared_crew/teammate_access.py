"""A Shared Crew teammate acts as the Crew owner everywhere except the Terminal.

A teammate is a person a sharing path admitted by its own check:

* an SSM invite guest on the Session Manager listener (:func:`is_ssm_guest_request`);
* a team Crew teammate the Kiro relay verified and this Crew's relay client handed
  over (``routes_identity._relay_teammate``);
* a collaborator a Tailscale membership boundary admitted: the direct tailnet
  listener's ``whois`` or the allowlisted ``tailscale serve`` path
  (``collaborators.is_collaborator_actor``).

Every admitted person has the owner's rights while roles are off (the recorded
decision that a Shared Crew gives each person one role), so a
teammate passes every owner gate (``source_providers.is_owner_dashboard_request``):
Bots and their Dashboards, approval modes including Trust and YOLO, apps, updates,
settings and the rest of the owner's dashboard. Three things stay the machine
owner's (:func:`is_owner_only_for_teammates`):

* the integrated Terminal and the external-terminal launcher
  (:data:`TERMINAL_PATH_PREFIXES`);
* every route the relay refuses a remote caller by its path alone
  (``kiro_relay.limits.remote_route_limit``): remote instances, whose routes mint a
  peer's owner token; Crew's local tokens and secrets, the vault, an app secret, a
  webhook bearer; shutdown; and a path not in canonical form. Each is a credential
  that would outlive the teammate's access, or the gateway process. Plus a channel
  connection's writes (:data:`CHANNEL_CONNECTION_WRITES`), which hold the channel's
  token, and a cron job's secret grants (:data:`CRON_SECRET_GRANTS`);
* managing who may reach the Crew: invites, the access card and the preview switch,
  which ``routes_invites._owner_refusal`` holds to the local operator or this
  machine's own tailnet user; the mobile setup (``tailnet_mobile``); and the config
  keys that open a listener or name who is let in (:func:`is_admission_config_key`),
  which the config PATCH refuses a teammate.

A teammate is not the owner for redaction either: what they read stays redacted
while the owner's switch is off (the recorded credential-redaction decision: the
owner's OFF covers only what the owner alone sees), so the redaction audience asks ``is_host_owner_dashboard_request`` and a
teammate's owner socket carries :data:`TEAMMATE_WS_FLAG`.

A teammate keeps their own identity: ``request["user"]`` stays their subject
(``ssm:<invite>``, ``relay:<id>``, ``tailnet:uid:<id>``) and the published actor
stays their principal, so every audit row and attribution names them, never the
owner. A Slack, Telegram, Teams or Webex allowlist link, an app token, an internal
caller, a widened-bind tailnet peer (attribution only) and an Unknown actor are not
teammates. The SSM and Tailscale paths exist only while the Shared Crew preview is
on; a relay teammate is the team Crew's own access path, granted by the control
plane, and is admitted as its other routes admit it.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CHANNEL_CONNECTION_WRITES",
    "CRON_SECRET_GRANTS",
    "TEAMMATE_WS_FLAG",
    "TERMINAL_PATH_PREFIXES",
    "is_admission_config_key",
    "is_owner_only_for_teammates",
    "is_shared_crew_teammate",
    "is_ssm_guest_request",
    "is_terminal_request",
    "teammate_passes_owner_gate",
]

#: Every route that opens, drives, lists or launches a host shell.
TERMINAL_PATH_PREFIXES = ("/api/terminal", "/api/ws/terminal")


#: Writes that create, change, link or remove a channel connection. Each holds the
#: channel's token and its allow-list of who may talk to the Crew; reading one, and
#: sending through a connected channel, stay open.
CHANNEL_CONNECTION_WRITES = (
    re.compile(
        r"\A/api/(?:slack|telegram|discord|feishu|imessage|teams|webex|wecom|weixin|whatsapp)"
        r"/config/?\Z"
    ),
    re.compile(r"\A/api/channels(?:/[^/]+)?/?\Z"),
    re.compile(r"\A/api/channels/(?:weixin|whatsapp)/"),
    re.compile(r"\A/api/messaging/teams/?\Z"),
)

#: A cron job's vault-secret grants: granting one hands the secret's value to a job
#: the teammate can read the output of.
CRON_SECRET_GRANTS = re.compile(r"\A/api/crons/[^/]+/secrets/?\Z")

#: Set on an owner socket a teammate opened, so the WebSocket redaction treats it as
#: a non-owner reader while every owner frame still reaches it.
TEAMMATE_WS_FLAG = "_shared_crew_teammate"

#: Config subtrees that open a listener or name who may reach the Crew.
_ADMISSION_CONFIG_ROOTS = ("dashboard.shared_crew", "dashboard.tailscale", "tunnel")


def is_admission_config_key(path: str) -> bool:
    """Whether config key *path* opens a listener or is an allow-list.

    The Shared Crew, Tailscale and tunnel subtrees, and any allow-list or DM policy by
    its name (a segment starting ``allow``, or ``dm_policy``), so a channel added later
    is covered without a table.
    """
    if not isinstance(path, str) or not path:
        return False
    if any(path == root or path.startswith(root + ".") for root in _ADMISSION_CONFIG_ROOTS):
        return True
    return any(seg.startswith("allow") or seg == "dm_policy" for seg in path.split("."))


def is_owner_only_for_teammates(request: Any) -> bool:
    """Whether *request* addresses a route a teammate does not get as the owner does."""
    from kiro_crew.dashboard.kiro_relay.limits import remote_route_limit

    path = str(getattr(request, "path", "") or "")
    if is_terminal_request(request) or remote_route_limit(path) is not None:
        return True
    if CRON_SECRET_GRANTS.match(path):
        return True
    method = str(getattr(request, "method", "GET") or "GET").upper()
    return method not in ("GET", "HEAD") and any(
        rule.match(path) for rule in CHANNEL_CONNECTION_WRITES
    )


def is_terminal_request(request: Any) -> bool:
    """Whether *request* addresses a Terminal route."""
    path = str(getattr(request, "path", "") or "")
    return any(path == p or path.startswith(p + "/") for p in TERMINAL_PATH_PREFIXES)


def _dashboard_user_session(getter: Any) -> bool:
    """The dashboard-user claims token auth publishes: never an app or internal caller."""
    if getter("internal_auth") is True or getter("app", None) != "":
        return False
    return getter("is_dashboard_user") is True


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
    if not callable(getter) or not _dashboard_user_session(getter):
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


def _tailnet_collaborator(getter: Any) -> bool:
    """A person a Tailscale membership boundary admitted, on their own session."""
    from kiro_crew.shared_crew.collaborators import is_collaborator_actor
    from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, Provenance

    actor = getter(ACTOR_REQUEST_KEY)
    if not is_collaborator_actor(actor):
        return False
    provenance = actor.principal.provenance
    if provenance is Provenance.TAILSCALE_LISTENER:
        return getter("auth_tailnet_direct") is True
    return provenance is Provenance.TAILSCALE_SERVE


def is_shared_crew_teammate(request: Any) -> bool:
    """Whether *request* is a Shared Crew teammate's own dashboard session.

    Never the machine owner, whichever path they came in on: the owner reaching
    the dashboard over a tailnet path carries a ``tailscale_*`` actor too.
    """
    getter = getattr(request, "get", None)
    if not callable(getter) or not _dashboard_user_session(getter):
        return False
    from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request

    if is_host_owner_dashboard_request(request):
        return False
    if is_ssm_guest_request(request):
        return True
    from kiro_crew.shared_crew.routes_identity import _relay_teammate

    if _relay_teammate(request):
        return True
    return _tailnet_collaborator(getter)


def teammate_passes_owner_gate(request: Any) -> bool:
    """Whether the owner gate admits *request* as a teammate: any route but the owner-only ones."""
    return not is_owner_only_for_teammates(request) and is_shared_crew_teammate(request)

"""The Crew owner on the direct tailnet listener: full owner rights, proved by ``whois``.

When the daemon's ``whois`` of a direct-listener connection names this
machine's own Tailscale user (stable user id and login both match the node's
owner, :func:`tailnet_direct.is_owner`), the request is the owner's: it carries
the owner's dashboard subject, so settings, the Tailscale API key, invites and
approvals answer it with no dashboard token.

Only an irreversible action asks for one more step: the request must carry
:data:`CONFIRM_HEADER` with :data:`CONFIRM_VALUE`, which the dashboard sends
only after its own confirmation dialog. Without it the answer is 428
``confirm_required`` and nothing runs. The local token path is unchanged.

Everyone else the listener admits keeps a ``tailnet:`` subject that never
equals the owner's, so owner-gated routes refuse them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.tailnet_direct import DirectGate, DirectPeer

#: Request key set on a direct-listener request ``whois`` proved is the owner.
OWNER_WHOIS_REQUEST_KEY = "auth_owner_whois"
CONFIRM_HEADER = "X-Kirocrew-Confirm"
CONFIRM_VALUE = "irreversible"
CODE_CONFIRM_REQUIRED = "confirm_required"

#: ``(method, path)`` of actions that cannot be undone: removing the Tailscale
#: API key and deleting every stored session.
IRREVERSIBLE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/api/shared-crew/tailscale/credential"),
        ("DELETE", "/api/sessions"),
    }
)

#: The bootstrap subject a no-owner install treats as its owner.
_BOOTSTRAP_OWNER_SUBJECT = "local-app"


def irreversible(request: web.Request) -> bool:
    return (request.method.upper(), request.path.rstrip("/") or "/") in IRREVERSIBLE_ROUTES


def confirmed(request: web.Request) -> bool:
    return (request.headers.get(CONFIRM_HEADER) or "").strip().lower() == CONFIRM_VALUE


def owner_subject(request: web.Request) -> str:
    """The dashboard subject owner-gated routes accept: the configured owner, else the bootstrap one."""
    state = request.app.get("state") if hasattr(request, "app") else None
    owner_id = str(getattr(state, "owner_id", "") or "")
    return owner_id or _BOOTSTRAP_OWNER_SUBJECT


def confirm_required_response() -> web.Response:
    from aiohttp import web

    return web.json_response(
        {
            "error": "This can't be undone. Confirm it in the dashboard to continue.",
            "code": CODE_CONFIRM_REQUIRED,
        },
        status=428,
    )


def direct_subject(
    request: web.Request, gate: DirectGate, peer: DirectPeer
) -> tuple[str, web.Response | None]:
    """``(subject, None)`` for an admitted direct request, or ``("", 428)`` to refuse it.

    The owner gets :func:`owner_subject` and :data:`OWNER_WHOIS_REQUEST_KEY`;
    an unconfirmed irreversible action by the owner is refused. Everyone else
    gets ``tailnet:uid:<id>`` (a tagged node ``tailnet:node:<name>``).
    """
    if gate.is_owner(peer):
        if irreversible(request) and not confirmed(request):
            return "", confirm_required_response()
        request[OWNER_WHOIS_REQUEST_KEY] = True
        return owner_subject(request), None
    if peer.user_id:
        return f"tailnet:uid:{peer.user_id}", None
    return f"tailnet:node:{peer.node}", None


__all__ = [
    "CODE_CONFIRM_REQUIRED",
    "CONFIRM_HEADER",
    "CONFIRM_VALUE",
    "IRREVERSIBLE_ROUTES",
    "OWNER_WHOIS_REQUEST_KEY",
    "confirm_required_response",
    "confirmed",
    "direct_subject",
    "irreversible",
    "owner_subject",
]

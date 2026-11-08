"""How token auth admits a relay-delivered request.

The relay is the authentication for a relayed request: it verified the person's
Kiro access token and removed it, and the Crew never sees one. So token auth admits
the caller the ingress middleware published, as that caller, and publishes the same
request keys its own token branches do:

* the owner of a personal Crew gets the owner's own dashboard subject, so every
  owner gate already in the dashboard (and the app proxy's ``owner-session`` claim)
  decides for them exactly as it does for the owner at the machine;
* a teammate gets a ``relay:`` subject no owner gate matches.

Admission runs before every token branch and token auth returns right after it, so
no Crew credential is read, honored or issued over the relay: a ``?token=`` or a
session cookie a relayed request carries is never validated, pinned, exchanged or
turned into an app scope, and no Crew cookie is set on the response.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.kiro_relay.caller import RelayCaller
from kiro_crew.dashboard.token_auth_paths import _log_auth

#: The subject the local owner-token bootstrap mints when no owner id is configured
#: (``handlers/core_local_token.py``: ``owner_id or "local-app"``), which the owner
#: predicate accepts as the owner.
_LOCAL_OWNER_SUBJECT = "local-app"


def relay_owner_subject(request: web.Request) -> str:
    """The dashboard subject the owner at the machine is authenticated as."""
    state = request.app.get("state")
    owner_id = str(getattr(state, "owner_id", "") or "")
    return owner_id or _LOCAL_OWNER_SUBJECT


def admit_relay_caller(request: web.Request, caller: RelayCaller) -> web.Response | None:
    """Admit *request* as *caller*: ``None`` once admitted, or the refusal to return."""
    owner_subject = relay_owner_subject(request)
    subject = caller.dashboard_subject(owner_subject)
    if not caller.is_owner and subject == owner_subject:
        # A teammate whose subject collides with the owner's can never be admitted
        # under it: refusing is the only answer that keeps a teammate off the owner.
        _log_auth(request, caller.audit_label, "denied", "relay teammate subject collision")
        return web.json_response(
            {"error": "This person can't be told apart from the owner.", "code": "relay_subject"},
            status=403,
        )
    request["user"] = subject
    # PRESENT and empty: the positive "not an app" claim the owner predicate and
    # the app-scope gate read, as every token branch publishes it.
    request["app"] = ""
    # No Crew credential authenticated this request, so none is published; a
    # handler reading claims from it gets the fail-closed "no credential" answer.
    request["auth_token"] = ""
    request["is_dashboard_user"] = True
    request["auth_from_query_token"] = False
    _log_auth(request, caller.audit_label, "granted", f"relay {caller.kind.value}")
    return None

"""``GET /v1/crew/info`` and ``GET /v1/crew/capabilities``: this Kiro Crew, and what
its Crew API can do.

``info`` answers the instance id, its form and the API version. ``display_name`` and
``timezone`` are optional in the contract and left out: the gateway holds no value
for either.

A client calls ``capabilities`` first and decides from ``features`` and ``agents``
(``docs/request-for-change/rfc-crew-api.md``, "Versioning"). The gateway never
reports a capability it cannot confirm, so a feature is ``true`` only when it is
named in :data:`SERVED_FEATURES`, and an agent's row carries only the abilities
``POST /v1/responses`` serves today.

Both routes serve the machine owner and an app credential, the way
``GET /v1/models`` does: each passes its own key as ``app_route``, so a verified
app credential is judged by that route's row in the scope table and every other
caller passes the owner gate. A caller that is not a verified app credential is
then asked for the machine owner, as ``POST /v1/responses`` does, so a person the
owner let into a Shared Crew is refused.

An app reads the same ``info`` the owner reads. Its ``capabilities`` lists under
``agents`` only what its grant allows (:func:`crew_api_models.models_for_grant`, the
list ``GET /v1/models`` gives it), and the owner's catalog is never read for it.
No caller gets a ``quota``: quota is not built.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from aiohttp import web

from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_errors import CREW_API_VERSION
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_models import models_for_grant, usable_models_off_loop
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_scopes import CREDENTIAL_KEY
from kiro_crew.dashboard.openai_compat import _MAX_PROMPT_BYTES

CAPABILITIES_PATH: Final = "/v1/crew/capabilities"
INFO_PATH: Final = "/v1/crew/info"
CAPABILITIES_KEY: Final = ("GET", CAPABILITIES_PATH)
INFO_KEY: Final = ("GET", INFO_PATH)

#: ``InstanceInfo.form``. shortcut: every gateway answers ``local``; name another
#: form when the gateway can tell which one it runs as.
FORM: Final = "local"

#: Every feature the contract's ``Capabilities.features`` names.
FEATURES: Final = (
    "background",
    "client_tools",
    "mcp_tools",
    "approvals",
    "fork",
    "mid_turn_question",
    "instance_events",
    "webhooks",
    "file_uploads",
)
#: The features a Crew API route on this gateway serves. A name is added here by the
#: change that makes the route serve it.
SERVED_FEATURES: Final[frozenset[str]] = frozenset()


def _agent() -> dict[str, Any]:
    """One agent's abilities: text in, plain text out, no reasoning summary."""
    return {"input_modalities": ["text"], "structured_output": False, "reasoning_summary": []}


async def _refusal(request: web.Request, path: str) -> web.Response | None:
    """The 403 for a caller that is neither a verified app credential nor the machine owner.

    Token auth sets :data:`CREDENTIAL_KEY` only for an app credential it verified
    (signature, expiry, an active grant, sent from this machine), and ``owner_route``
    has judged that credential by this route's row before the handler runs. Every
    other caller is asked for the machine owner.
    """
    if request.get(CREDENTIAL_KEY) is not None:
        return None
    return await _host_owner_refusal(request, OWNER_ONLY_PUBLIC_ROUTES[path])


async def _capabilities(request: web.Request) -> web.Response:
    denied = await _refusal(request, CAPABILITIES_PATH)
    if denied is not None:
        return denied
    credential = request.get(CREDENTIAL_KEY)
    # An app's agents come from its grant alone: the owner's catalog is not read.
    models = (
        models_for_grant(credential) if credential is not None else await usable_models_off_loop()
    )
    return web.json_response(
        {
            "api_version": {"current": CREW_API_VERSION},
            "features": {name: name in SERVED_FEATURES for name in FEATURES},
            "agents": {row["id"]: _agent() for row in models},
            "limits": {"max_input_bytes": _MAX_PROMPT_BYTES},
        }
    )


def _instance_id() -> str:
    from kiro_crew.relay_client.lease import crew_instance_id

    return crew_instance_id(data_home())


async def _info(request: web.Request) -> web.Response:
    # The id this Kiro Crew gives the relay, so a caller sees one identity on
    # either path. The first read on a data home makes and keeps it.
    denied = await _refusal(request, INFO_PATH)
    if denied is not None:
        return denied
    instance_id = await asyncio.to_thread(_instance_id)
    return web.json_response({"id": instance_id, "form": FORM, "api_version": CREW_API_VERSION})


#: GET /v1/crew/info serves a verified app credential with no required scope. Every
#: other caller passes the owner gate, and the handler then asks for the machine owner.
api_info = owner_route(_info, OWNER_ONLY_PUBLIC_ROUTES[INFO_PATH], app_route=INFO_KEY)

#: GET /v1/crew/capabilities serves a verified app credential with no required scope,
#: from its grant alone. Every other caller passes the owner gate, and the handler then
#: asks for the machine owner before the catalog is read.
api_capabilities = owner_route(
    _capabilities, OWNER_ONLY_PUBLIC_ROUTES[CAPABILITIES_PATH], app_route=CAPABILITIES_KEY
)

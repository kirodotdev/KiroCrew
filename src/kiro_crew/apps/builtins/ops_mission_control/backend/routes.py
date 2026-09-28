"""Ops Mission Control — backend routes.

Builtin-app contract: ``register_routes(app: web.Application) -> None`` registering
FULL paths directly on the gateway router (confirmed against the call site in
``dashboard/routes/system.py``: ``_mod.register_routes(app)``, single argument). This is
NOT the external-app ``AppRoute``-list contract — mixing them up produces routes
that silently never dispatch.

Every handler is wrapped in ``_require_enabled``: builtin routes exist from gateway
startup even while the app is disabled, so a default-disabled opt-in app would
otherwise stay callable.

Secrets are **write-only** over this surface. ``PUT /providers/<id>/secret`` accepts
a token; nothing ever returns one. The read endpoints report only whether a field
is set.

This module is the surface's composition root; the handler bodies live in
``http_routes`` (its package docstring maps them). What stays HERE is what has to stay
addressable as ``routes``: the registration table, the enable gate, the SEL audit writer,
the redaction floor (this module is the registered redaction sink for the ledger write),
the ledger indexer, a handler for each route whose projection calls a seam — it hands the
projection this module's CURRENT binding of each one — and three adapters that keep
``_schedule_verification``, ``_execute_stored_proposal`` and ``_settings_write_or_refuse``
callable with their historic signatures. The six routes whose projection calls no seam
register that projection directly.

The patchable seams are exactly ``is_app_enabled``, ``get_registry``, ``_audit``,
``_safe_outbound``, ``put_secret``, ``delete_secret``, ``merge_provider_config`` and
``_index_ledger_safely``: ``mock.patch.object(routes, <seam>)`` reaches every call site that
reads one. Every other name it offers (``_authorize``, ``_sink_refuses``, ``_slot_state``, the
constants, the domain module objects, ...) is a re-export of the object its owner defines, or
one of the three adapters. Patching it here changes what ``routes.<name>`` returns, not what a
projection calls, so patch it on its owner — or, for a module object, THROUGH it
(``routes.rotation.is_primary`` patches the rotation module itself).
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

# Every domain module `routes` held stays an attribute of it, as the same module object: the
# suite patches THROUGH several (`routes.rotation.is_primary`, `routes.store.claim`, ...).
from kiro_crew.apps.builtins.ops_mission_control.backend import (  # noqa: F401 -- historical module export
    companion,
    dispatch,
    handover,
    ledger,
    notify_out,
    policy_store,
    rotation,
    slack_out,
    slot_watch,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import (
    actions as actions_routes,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import board as board_routes
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import (
    configuration as configuration_routes,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import ledger as ledger_routes
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import (
    lifecycle as lifecycle_routes,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import (
    webhook as webhook_routes,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (  # noqa: F401 -- historical module export
    APP_NAME,
    _json_body,
    _NotABool,
    _require_bool,
    _slack_client,
    _store_read_refusal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes.actions import (  # noqa: F401 -- historical module export
    _MAX_NOTE_LEN,
    _authorize,
    _Authorized,
    _execute_authorized,
    _handle_proposals,
    _sink_refuses,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes.board import (  # noqa: F401 -- historical module export
    MAX_INCIDENTS_RESPONSE,
    _handle_incident,
    _handle_incidents,
    _ledger_sync_status,
    _provider_dict,
    _slot_state,
    canonical_slot_key,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes.configuration import (  # noqa: F401 -- historical module export
    _MAX_PROVIDER_ID_LEN,
    _MAX_REMOTE_LEN,
    _MAX_SECRET_LEN,
    _SAFE_BRANCH_RE,
    _SAFE_LOGIN_RE,
    _url_has_userinfo,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes.ledger import (
    _handle_get_ledger,
    _handle_ledger_contradictions,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes.lifecycle import (
    _handle_dispatch,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes.webhook import (  # noqa: F401 -- historical module export
    _SPOOL_RETRY_AFTER_SECS,
    _WEBHOOK_AUTH_REJECTIONS,
    _WEBHOOK_PAYLOAD_REJECTIONS,
    _read_capped,
    _webhook_reject_status,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    merge_provider_config,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (  # noqa: F401 -- historical module export
    webhook as webhook_mod,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import get_registry
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    delete_secret,
    put_secret,
    redact_tokens,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_BASE = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ---------------------------------------------------------------------------
# The gate, the audit writer and the redaction floor
# ---------------------------------------------------------------------------


def _require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off the
    event loop — same treatment as the other builtin apps' gates.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    sel().log_api_access(
        caller=f"core:{APP_NAME}",
        operation=op,
        outcome=outcome,
        resources=target,
        error=error,
    )


def _safe_outbound(text: str) -> str:
    """The redaction floor for text this app publishes where it cannot take it back.

    An action note is agent- or operator-authored free text that becomes an
    acknowledgement comment, a resolve reason or a mute note **on someone else's system**,
    where we cannot unpublish it. It reached the sink verbatim: an agent that pasted a
    provider token into its diagnosis published that token into the provider's own comment
    thread. Found in review — the same shape as the Slack sink and the ledger write path,
    which were already covered while this one was not.

    The same floor covers the agent-authored ``diagnosis``/``resolution`` an incident
    persists and renders, and a learned pattern/fix before its content-addressed id is
    computed — ``ledger.jsonl`` is what ``ledger_sync`` pushes to the team's shared remote.
    This module is the registered redaction sink for that ledger write, which is why the
    floor is defined here and handed to the projections that call it.

    Both passes, and `redact_via_context` rather than `security.redact` directly, for the
    same reasons the ledger write path documents: the two redactors cover different token
    families, and the context shim makes a loaded companion's declared patterns apply while
    an enterprise host that fails to compose its companion fails CLOSED.
    """
    return redact_tokens(redact_via_context(text))


def _index_ledger_safely() -> dict[str, int]:
    """Project new ledger entries into the vector store. Never raises.

    Resolves the store here rather than holding one open: an install with no vector
    store (model still downloading, or a deliberately minimal setup) must complete
    hygiene exactly as before. Mirrors ``dispatch._attach_similar_safely``.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_index

    store_obj = None
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.vector_memory import VectorMemoryStore

        cfg = KiroCrewConfig.load()
        store_obj = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim, config=cfg)
        store_obj.init()
        return ledger_index.import_pending(store_obj)
    except Exception:  # noqa: BLE001 — no store, or a broken one, is a supported state
        logger.debug(
            "ops-mission-control: ledger indexing unavailable; hygiene still ran",
            exc_info=True,
        )
        return {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0}
    finally:
        if store_obj is not None:
            try:
                store_obj.close()
            except Exception:  # noqa: BLE001
                logger.debug("ops-mission-control: vector store close failed", exc_info=True)


# ---------------------------------------------------------------------------
# Handlers. A projection that calls a seam receives this module's binding of it on every
# call, which is what keeps `mock.patch.object(routes, <seam>)` reaching that call site.
# The projections that call no seam are re-exported above and registered as they are.
# ---------------------------------------------------------------------------


async def _handle_state(request: web.Request) -> web.StreamResponse:
    return await board_routes._handle_state(request, get_registry=get_registry)


async def _handle_handover(request: web.Request) -> web.StreamResponse:
    return await board_routes._handle_handover(request, get_registry=get_registry)


async def _handle_signals(request: web.Request) -> web.StreamResponse:
    return await board_routes._handle_signals(request, get_registry=get_registry)


async def _handle_providers(request: web.Request) -> web.StreamResponse:
    return await board_routes._handle_providers(request, get_registry=get_registry)


async def _handle_rotation(request: web.Request) -> web.StreamResponse:
    return await board_routes._handle_rotation(request, get_registry=get_registry)


async def _handle_transition(request: web.Request) -> web.StreamResponse:
    return await lifecycle_routes._handle_transition(
        request, _audit=_audit, _safe_outbound=_safe_outbound
    )


async def _handle_claim(request: web.Request) -> web.StreamResponse:
    return await lifecycle_routes._handle_claim(request, get_registry=get_registry, _audit=_audit)


async def _handle_action(request: web.Request) -> web.StreamResponse:
    return await actions_routes._handle_action(
        request, get_registry=get_registry, _audit=_audit, _safe_outbound=_safe_outbound
    )


async def _handle_propose(request: web.Request) -> web.StreamResponse:
    return await actions_routes._handle_propose(
        request, _audit=_audit, _safe_outbound=_safe_outbound
    )


async def _handle_decide_proposal(request: web.Request) -> web.StreamResponse:
    return await actions_routes._handle_decide_proposal(
        request, get_registry=get_registry, _audit=_audit, _safe_outbound=_safe_outbound
    )


async def _handle_put_provider_config(request: web.Request) -> web.StreamResponse:
    return await configuration_routes._handle_put_provider_config(
        request,
        get_registry=get_registry,
        merge_provider_config=merge_provider_config,
        _audit=_audit,
    )


async def _handle_put_settings(request: web.Request) -> web.StreamResponse:
    return await configuration_routes._handle_put_settings(request, _audit=_audit)


async def _handle_put_secret(request: web.Request) -> web.StreamResponse:
    return await configuration_routes._handle_put_secret(
        request, get_registry=get_registry, put_secret=put_secret
    )


async def _handle_delete_secret(request: web.Request) -> web.StreamResponse:
    return await configuration_routes._handle_delete_secret(request, delete_secret=delete_secret)


async def _handle_rotation_arm(request: web.Request) -> web.StreamResponse:
    return await configuration_routes._handle_rotation_arm(request, get_registry=get_registry)


async def _handle_post_ledger(request: web.Request) -> web.StreamResponse:
    return await ledger_routes._handle_post_ledger(
        request, _audit=_audit, _safe_outbound=_safe_outbound
    )


async def _handle_ledger_hygiene(request: web.Request) -> web.StreamResponse:
    return await ledger_routes._handle_ledger_hygiene(
        request, _audit=_audit, _index_ledger_safely=_index_ledger_safely
    )


async def _handle_delete_ledger(request: web.Request) -> web.StreamResponse:
    return await ledger_routes._handle_delete_ledger(request, _audit=_audit)


async def _handle_webhook(request: web.Request) -> web.StreamResponse:
    return await webhook_routes._handle_webhook(request, _audit=_audit)


# ---------------------------------------------------------------------------
# Helpers that take seams, callable here with their historic signatures
# ---------------------------------------------------------------------------


def _schedule_verification(incident_id: str, action: str, duration_secs: Any) -> tuple[str, str]:
    return actions_routes._schedule_verification(incident_id, action, duration_secs, _audit=_audit)


async def _execute_stored_proposal(
    incident: Any, proposal: dict[str, Any], permit: _Authorized
) -> dict[str, Any]:
    return await actions_routes._execute_stored_proposal(
        incident,
        proposal,
        permit,
        get_registry=get_registry,
        _audit=_audit,
        _safe_outbound=_safe_outbound,
    )


async def _settings_write_or_refuse(
    fn: Any,
    *args: Any,
    code: str,
    applied: dict[str, Any],
    **kwargs: Any,
) -> web.Response | None:
    return await configuration_routes._settings_write_or_refuse(
        fn, *args, code=code, applied=applied, _audit=_audit, **kwargs
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(app: web.Application) -> None:
    """Register Ops Mission Control's routes on the gateway application."""
    add = app.router
    add.add_get(f"{_BASE}/state", _require_enabled(_handle_state))
    add.add_get(f"{_BASE}/incidents", _require_enabled(_handle_incidents))
    add.add_get(f"{_BASE}/incident", _require_enabled(_handle_incident))
    add.add_post(f"{_BASE}/incident/transition", _require_enabled(_handle_transition))
    add.add_post(f"{_BASE}/incident/claim", _require_enabled(_handle_claim))
    add.add_post(f"{_BASE}/incident/action", _require_enabled(_handle_action))
    # The propose loop: draft -> queue -> decide. Separate routes because they have
    # different authority: proposing changes nothing, deciding may write.
    add.add_post(f"{_BASE}/incident/propose", _require_enabled(_handle_propose))
    add.add_post(f"{_BASE}/incident/proposal/decide", _require_enabled(_handle_decide_proposal))
    add.add_get(f"{_BASE}/proposals", _require_enabled(_handle_proposals))
    add.add_post(f"{_BASE}/dispatch", _require_enabled(_handle_dispatch))
    add.add_get(f"{_BASE}/signals", _require_enabled(_handle_signals))
    add.add_get(f"{_BASE}/handover", _require_enabled(_handle_handover))
    add.add_get(f"{_BASE}/providers", _require_enabled(_handle_providers))
    add.add_put(
        f"{_BASE}/providers/{{provider_id}}/config",
        _require_enabled(_handle_put_provider_config),
    )
    add.add_put(f"{_BASE}/providers/{{provider_id}}/secret", _require_enabled(_handle_put_secret))
    add.add_delete(
        f"{_BASE}/providers/{{provider_id}}/secret", _require_enabled(_handle_delete_secret)
    )
    add.add_put(f"{_BASE}/settings", _require_enabled(_handle_put_settings))
    add.add_get(f"{_BASE}/rotation", _require_enabled(_handle_rotation))
    add.add_post(f"{_BASE}/rotation/arm", _require_enabled(_handle_rotation_arm))
    add.add_get(f"{_BASE}/ledger", _require_enabled(_handle_get_ledger))
    add.add_get(f"{_BASE}/ledger/contradictions", _require_enabled(_handle_ledger_contradictions))
    add.add_post(f"{_BASE}/ledger", _require_enabled(_handle_post_ledger))
    add.add_post(f"{_BASE}/ledger/hygiene", _require_enabled(_handle_ledger_hygiene))
    add.add_delete(f"{_BASE}/ledger", _require_enabled(_handle_delete_ledger))
    add.add_post(f"{_BASE}/webhook", _require_enabled(_handle_webhook))

    # Warm the provider registry HERE, at gateway startup, not on the first request.
    # `get_registry()` populates lazily: entry-point enumeration, signed-plugin admission
    # I/O and companion import all run on the first call. Every producer of that first call
    # is a request handler (`_handle_signals`, `_handle_claim`, …), so the discovery cost
    # landed on the event loop — the gateway's first `/signals` poll stalled the heartbeat
    # and every other task for the length of a filesystem plugin scan. `register_routes`
    # runs synchronously before the loop serves anything, so paying it here is free.
    # Found in review.
    #
    # Fail-open: this app is default-disabled and an install that never enables it must not
    # crash gateway startup on a discovery fault (`get_registry` already swallows companion
    # errors; this guards the enumeration around it).
    try:
        get_registry()
    except Exception:  # noqa: BLE001 — a discovery fault must not break gateway startup
        logger.exception("ops-mission-control: registry warm-up failed; will retry lazily")

    logger.info("ops-mission-control: routes registered")

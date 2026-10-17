"""HTTP plumbing every Ops Mission Control projection shares.

Request parsing (``_json_body``, ``_require_bool``), the coded response a strict store
reader's refusal maps to (``_store_read_refusal``), the gateway-state accessor for the Slack
client, the surface's logger, and the types of the facade seams a projection receives. No
handler lives here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Protocol

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out, store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import UnknownFieldError
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import OpsProviderRegistry

#: The whole HTTP surface logs under the facade's name, whichever projection emits the line:
#: the gateway's log format prints the logger name, so a filter or a grep keyed on
#: ``backend.routes`` covers every handler.
logger = logging.getLogger("kiro_crew.apps.builtins.ops_mission_control.backend.routes")

APP_NAME = store.APP_NAME

#: ``routes.get_registry``, handed to a projection so a patched binding is the one it calls.
RegistryLookup = Callable[[], OpsProviderRegistry]


class AuditWriter(Protocol):
    """``routes._audit``: one SEL ``log_api_access`` row attributed to this app."""

    def __call__(self, op: str, target: str, outcome: str, *, error: str = "") -> None: ...


class _NotABool(ValueError):
    """A field that must be a JSON boolean was not one."""


def _require_bool(body: dict[str, Any], field: str, *, default: bool | None = None) -> bool | None:
    """A JSON boolean, or raise. NEVER ``bool(value)``.

    `bool()` on a string is true for ANY non-empty text, so every spelling of "no" a client
    might send — `"false"`, `"False"`, `"no"`, `"0"` — coerced to True. On `/incident/proposal/
    decide` that inverted the operator's answer: a request saying "reject this" reached
    `decide_proposal(approve=True)` and executed the authorized production action instead of
    refusing it. The same coercion sat on `schedule_strict_gating` (setting it "false" would
    have READ as enabling strict gating, which is at least the safe direction, but by luck
    rather than design) and on `primary_instance`, which decides who may prune the shared
    ledger. Found in review.

    Refusing is the only correct behavior: there is no safe guess about which way an operator
    meant an ambiguous answer to a question about executing a production write. Accepts the
    JSON booleans and nothing else — not `1`/`0`, because a caller sending those is a caller
    whose serializer this endpoint should not be quietly accommodating.
    """
    if field not in body:
        return default
    value = body[field]
    if isinstance(value, bool):
        return value
    raise _NotABool(field)


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    """Parse a JSON object body; ``None`` when it is malformed or not an object.

    Same Q1/Q2 posture as ``dashboard/handlers/_shared.read_bounded_json`` (a
    non-object is refused, and the catch spans the client-input failure set of
    ``LookupError``/``RecursionError``/``ValueError`` so an unknown ``charset=``
    codec is a 400, not a 500). The deliberate divergence is the return shape:
    this yields ``dict | None`` and lets each caller answer the 400 itself,
    rather than the ``(body, response)`` tuple the shared helper returns. The
    catch is narrowed from the previous ``except Exception`` so a mid-read
    transport error propagates instead of being reported as a client mistake.
    """
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _store_read_refusal(exc: Exception, *, code: str) -> web.Response:
    """Map a strict reader's refusal to a coded response.

    The store and config readers refuse rather than publishing a mutation over a read
    they could not make, so every handler that mutates them can see two distinct
    failures. They get DIFFERENT statuses because the operator's
    next move differs: an unreadable file is transient, so 503 correctly says "retry",
    while a corrupt one needs a person to repair it, so 503 would send them at something
    that cannot succeed and 500 is the honest answer.

    Centralized because four handlers need the identical pair, and translating it at
    some of them and not others is worse than not translating it anywhere -- an
    operator cannot tell a route that reports the condition from one that swallows it.

    ``JSONDecodeError`` MUST be matched before any ``ValueError`` arm at the call site.
    It is a `ValueError` subclass, so an existing arm for a domain error will otherwise
    claim it and report corruption as that domain error -- which is the same accident
    this change fixes in `verify_pending_actions`, `reconcile` and
    `_schedule_verification`.
    """
    if isinstance(exc, UnknownFieldError):
        # Refused for the same reason as corruption -- writing would strip a newer instance's
        # field -- but the file is FINE and this reader is behind, so the remedy is an upgrade
        # rather than a repair. Reporting it as corruption sends the operator to fix something
        # that is not broken. Found in review (Design Review). Ordered before the
        # `CorruptDocumentError` arm below, which it subclasses.
        return web.json_response(
            {
                "ok": False,
                "error": (
                    "the stored document holds a field this build does not serialize; "
                    "writing would drop it, so check whether this instance needs upgrading"
                ),
                "code": f"{code}_version_skew",
            },
            status=409,
        )
    if isinstance(exc, json.JSONDecodeError):
        return web.json_response(
            {
                "ok": False,
                "error": "the stored document is unreadable and must be repaired",
                "code": f"{code}_corrupt",
            },
            status=500,
        )
    return web.json_response(
        {
            "ok": False,
            "error": "the store is not readable right now; try again",
            "code": f"{code}_unreadable",
        },
        status=503,
    )


def _slack_client(request: web.Request) -> Any | None:
    """The gateway's live Slack client, or None when Slack is not configured.

    Passed explicitly into slack_out rather than fetched from a global: Kiro Crew
    has no global state accessor, and an explicit dependency is testable.
    """
    return slack_out.client_from_state(request.app.get("state"))

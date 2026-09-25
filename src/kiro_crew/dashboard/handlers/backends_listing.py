"""``GET /api/backends`` -- the SELECTION listing for the per-chat backend picker.

Distinct from ``GET /api/acp-backends`` (``acp_backend_status.py``), which is an
owner-gated machine-readiness probe (installed / missing / restart-owed / auth).
This endpoint answers the composer's question instead: "which backends may I
start a new chat on, what do I call them, and which one do I get if I pick
nothing" -- plus the two operator-descriptor diagnostics D3 asks Settings to
show, so an operator can see WHY an entry they wrote is not offered.

Three arrays, from two owners that must not be conflated:

* ``backends`` -- every currently SELECTABLE id, from the one selectability
  owner (``selectable_backend_values`` -- the same set the PATCH allowlist and
  ``/api/config/schema`` read, so this cannot offer a value session creation
  refuses, harness-parity H4). Each row carries a display ``label`` and
  ``is_global_default`` (the id ``resolve_selected_backend`` resolves the
  configured ``agent.acp_backend`` to -- exactly what an unselected new chat
  runs on).
* ``invalid`` -- operator descriptors that failed to parse/validate, id ->
  reasons. Never spellable, so they can never be a session backend; shown in
  Settings with what is wrong.
* ``unroutable`` -- operator descriptors that parsed cleanly and ARE spellable
  but declared no verified routing, so ``register_selectable_backend`` refused
  them (D3's visible-but-unselectable state). Shown with the reason.

Not owner-gated: the new-chat picker is a per-user surface, and the answer names
no host secret -- only which harnesses this build offers and what they are
called. Config load resolves the governance ceiling, so the snapshot is
offloaded off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from aiohttp import web

from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

#: Display names for the baseline (non-descriptor) backends. ``provider_label_for``
#: answers only for REGISTERED (config-authored) ids -- a builtin returns ``""`` --
#: so the human name for kiro-cli / KAS / Claude / Codex lives here, the one place
#: the selection surface reads it. Keyed by the wire id (``""`` is kiro-cli). A
#: builtin absent from this map falls back to its policy id, then its raw id, so a
#: newly-baselined backend still renders a name rather than an empty label.
_BUILTIN_LABELS: Dict[str, str] = {
    "": "Kiro CLI",
    "kas": "Kiro Agent (KAS)",
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "pi": "Pi",
    "goose": "Goose",
    "deepseek": "DeepSeek",
}


def _label_for(backend_id: str) -> str:
    """The display name for a selectable/known id.

    A REGISTERED descriptor recorded its ``display_name`` as the backend label at
    ``register_known_backend``, so ``provider_label_for`` answers for it; a builtin
    reads the static map above. Falls through to the policy id and then the raw id
    so every row has a non-empty, human-ish name even for an id neither source
    knows -- the picker must render SOMETHING selectable, never a blank chip.
    """
    from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND, provider_label_for

    registered = provider_label_for(backend_id)
    if registered:
        return registered
    if backend_id in _BUILTIN_LABELS:
        return _BUILTIN_LABELS[backend_id]
    return (
        POLICY_ID_BY_BACKEND.get(backend_id, "") or backend_id or POLICY_ID_BY_BACKEND.get("", "")
    )


def _snapshot() -> Dict[str, Any]:
    """Build the payload. BLOCKING -- run under ``asyncio.to_thread``.

    ``KiroCrewConfig.load`` resolves the governance ceiling (config read), and the
    registry reads are cheap but sit behind the same import boundary, so the whole
    thing is assembled off the event loop in one hop rather than reaching back for
    config a second time.

    Imports are function-local: this module is pulled in by the route registrar at
    startup, and a module-scope import from ``config`` / the SDK boundary here is
    how an import cycle gets introduced later (the same reason the sibling
    ``acp_backend_status._snapshot`` defers its imports).
    """
    from kiro_crew.acp_backends import resolve_selected_backend, selectable_backend_values
    from kiro_crew.agent_sdk.operator_harnesses import (
        invalid_operator_harnesses,
        unselectable_operator_harnesses,
        unverified_operator_harnesses,
    )
    from kiro_crew.config import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    # The id an unselected new chat actually lands on: the configured
    # ``agent.acp_backend`` put through the SINGLE selectability gate, so a
    # persisted-but-now-unselectable default resolves to the same fallback session
    # creation would use, rather than a value the picker would then mark unpickable.
    global_default = resolve_selected_backend(getattr(cfg.agent, "acp_backend", ""))

    backends: List[Dict[str, Any]] = [
        {
            "id": backend_id,
            "label": _label_for(backend_id),
            "is_global_default": backend_id == global_default,
        }
        for backend_id in selectable_backend_values()
    ]

    invalid: List[Dict[str, Any]] = [
        {"id": backend_id, "label": _label_for(backend_id), "reasons": list(reasons)}
        for backend_id, reasons in sorted(invalid_operator_harnesses().items())
    ]
    # ``unverified`` is the subset of unroutable rows whose only missing piece is
    # the end-to-end routing attestation: a recognized routing is declared and the
    # harness is registered, so Settings can offer a Verify action that runs the
    # probe and, on success, makes the row selectable without a restart. An
    # unroutable row that declared no routing has nothing to verify and stays a
    # read-only diagnostic.
    unverified_ids = unverified_operator_harnesses()
    unroutable: List[Dict[str, Any]] = [
        {
            "id": backend_id,
            "label": _label_for(backend_id),
            "reason": reason,
            "verifiable": backend_id in unverified_ids,
        }
        for backend_id, reason in sorted(unselectable_operator_harnesses().items())
    ]

    return {"backends": backends, "invalid": invalid, "unroutable": unroutable}


async def api_backends(request: web.Request) -> web.Response:
    """GET /api/backends -- selectable backends + operator-descriptor diagnostics."""
    payload = await asyncio.to_thread(_snapshot)
    return web.json_response(payload)


#: Audit/refusal vocabulary for the verify action. Same codes and prose as the
#: sibling owner-gated ``acp_backend_status`` handlers, so the client has one
#: branch for "sign in as the owner" across the backend surfaces.
_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_OWNER_REQUIRED_MESSAGE = "dashboard owner required"
_CODE_UNKNOWN_OPERATOR_BACKEND = "unknown_operator_backend"
_UNKNOWN_OPERATOR_BACKEND_MESSAGE = "not a registered operator backend"
_AUDIT_VERIFY_OPERATION = "backend_routing_verify"

#: One probe at a time per gateway: each run spawns a harness process and drives
#: a real turn, and two concurrent probes of the same descriptor would race on the
#: attestation write. A second request while one runs answers 409. ``LoopBoundLock``
#: rather than a bare ``asyncio.Lock`` so the primitive binds to the loop that
#: first uses it, not the import-time loop (the loop-bound-locks gate).
_verify_lock = LoopBoundLock()


async def api_backend_verify(request: web.Request) -> web.Response:
    """POST /api/backends/{id}/verify -- run the end-to-end routing probe.

    Owner-gated and audited (success and denial), because the outcome grants
    selectability: a verified attestation is what lets a chat start on this
    harness, so who verified what is exactly the record the audit log exists to
    hold. Only a REGISTERED operator descriptor can be verified -- a builtin has
    no routing claim to test, and an invalid/unroutable descriptor has nothing
    registered to spawn -- so anything else is a 404 with its own code.

    The probe is :func:`kiro_crew.acp.harness.routing_verification.verify_routing`
    behind the SDK facade: it spawns the harness through the production provider
    factory, asks for one file write in a scratch directory, denies every
    permission request it receives, and reports ``verified`` only when the host
    asked and nothing was written. See that module for the verdict table. The
    verdict is bound to the agent the probe ran under (the optional ``agent``
    body field; default: the configured default agent), and a descriptor spawn
    for an agent the attestation does not name is refused.
    """
    from kiro_crew.dashboard.handlers.kiro_prerequisite import _is_dashboard_owner
    from kiro_crew.sel import sel

    backend_id = request.match_info.get("id", "")
    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _audit(outcome: str, error: str = "") -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation=_AUDIT_VERIFY_OPERATION,
            outcome=outcome,
            source="dashboard",
            resources=backend_id,
            error=error,
        )

    if not _is_dashboard_owner(request):
        try:
            await asyncio.to_thread(_audit, "denied", _OWNER_REQUIRED_MESSAGE)
        except Exception:
            logger.debug("Could not audit denied backend verify", exc_info=True)
        return web.json_response(
            {"error": _OWNER_REQUIRED_MESSAGE, "code": _CODE_OWNER_REQUIRED}, status=403
        )

    from kiro_crew.agent_sdk.operator_harnesses import (
        is_registered_operator_backend,
        unverified_operator_harnesses,
        verify_operator_backend_routing,
    )
    from kiro_crew.dashboard.handlers._shared import read_bounded_json
    from kiro_crew.validation import _AGENT_NAME_RE

    # Optional body: ``{"agent": "<name>"}`` selects the agent the probe runs
    # under; absent (the panel's Verify button) means the configured default. The
    # attestation is bound to that agent, so a backend meant to serve several
    # agents is verified once per agent -- which is why an already-selectable
    # backend is still verifiable here when an agent is named.
    body, err = await read_bounded_json(request, allow_absent=True)
    if err is not None:
        return err
    agent_raw = (body or {}).get("agent")
    agent: str | None
    if agent_raw is None or agent_raw == "":
        agent = None
    elif isinstance(agent_raw, str) and _AGENT_NAME_RE.match(agent_raw):
        agent = agent_raw
    else:
        return web.json_response(
            {"error": "agent must be a valid agent name", "code": "invalid_agent"}, status=400
        )

    pending = backend_id in unverified_operator_harnesses()
    if not pending and not (agent is not None and is_registered_operator_backend(backend_id)):
        # Not registered, or unroutable, or already selectable with no further
        # agent named: none of these has a routing claim this action can settle.
        return web.json_response(
            {"error": _UNKNOWN_OPERATOR_BACKEND_MESSAGE, "code": _CODE_UNKNOWN_OPERATOR_BACKEND},
            status=404,
        )

    if _verify_lock.locked():
        return web.json_response(
            {"error": "a routing verification is already running", "code": "verify_in_progress"},
            status=409,
        )

    from kiro_crew.config import KiroCrewConfig

    async with _verify_lock:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        try:
            result = await verify_operator_backend_routing(backend_id, cfg, agent=agent)
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "code": _CODE_UNKNOWN_OPERATOR_BACKEND}, status=404
            )

    # After the work, like the sibling re-check: the record must describe what
    # actually happened, and the verdict is part of it.
    try:
        await asyncio.to_thread(
            _audit, "success" if result.get("verified") else result.get("verdict", "unknown")
        )
    except Exception:
        logger.debug("Could not audit backend verify", exc_info=True)

    state = request.app.get("state")
    if state is not None and result.get("selectable"):
        # The picker and the composer read the listing through their own fetches;
        # a slots push is the existing nudge that makes open dashboards re-read.
        try:
            state.push_slots_update()
        except Exception:  # noqa: BLE001 - a missed nudge is a stale picker, not an error
            logger.debug("backend verify: slots push failed", exc_info=True)
    result["verified"] = bool(result.get("verdict") == "verified")
    return web.json_response(result)

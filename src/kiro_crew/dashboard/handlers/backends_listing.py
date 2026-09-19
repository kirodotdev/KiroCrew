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
    from kiro_crew.acp.harness.operator_registry import (
        invalid_operator_harnesses,
        unselectable_operator_harnesses,
    )
    from kiro_crew.acp_backends import resolve_selected_backend, selectable_backend_values
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
    unroutable: List[Dict[str, Any]] = [
        {"id": backend_id, "label": _label_for(backend_id), "reason": reason}
        for backend_id, reason in sorted(unselectable_operator_harnesses().items())
    ]

    return {"backends": backends, "invalid": invalid, "unroutable": unroutable}


async def api_backends(request: web.Request) -> web.Response:
    """GET /api/backends -- selectable backends + operator-descriptor diagnostics."""
    payload = await asyncio.to_thread(_snapshot)
    return web.json_response(payload)

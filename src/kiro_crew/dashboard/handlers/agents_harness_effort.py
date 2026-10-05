"""What a session's harness offers before the session reports: its model list and effort answer.

:mod:`kiro_crew.dashboard.handlers.agents` binds these names and is the import path
callers use; this plain module sits outside the owner composition, as
``agents_cc_model_rows`` does.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp_backends import (
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    backend_to_stored,
    model_registry_namespace,
    selectable_backend_values,
)
from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.dashboard.chat_persistence import (
    cap_effort_capability_levels,
    get_reasoning_effort_values,
)
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.effort import EFFORT_LEVELS, model_supports_effort


def own_models_backend(backend: str, configured: str) -> str | None:
    """*backend*'s config spelling when its pickers need its own ``GET /api/models?backend=`` list.

    The config spelling (``kiro``, not ``""``) is what ``?backend=`` takes. ``None`` when
    it shares the configured backend's model-id namespace (kiro-cli and kas read one
    catalog) or is not selectable, which ``?backend=`` refuses.
    """
    if model_registry_namespace(backend) == model_registry_namespace(configured):
        return None
    return backend_to_stored(backend) if backend in selectable_backend_values() else None


def _live_on(state: Any, backend: str, crew: str) -> Any:
    """A live provider on *backend*, *crew*'s own session's first, then the newest.

    The levels these harnesses advertise can follow the model a session runs (goose's do,
    per model), and the crew's own session is the likeliest to run the crew's. Newest first, as
    ``core._advertised_evidence_provider`` reads: an older session still holds what its
    build said at its own ``session/new``.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return None
    on = [
        p
        for p in reversed(list(sessions.active_providers()))
        if capabilities_of(p).backend == backend
    ]
    mine = {
        id(sessions.get_provider(effective_session_key(s)))
        for s in getattr(state, "_slots", {}).values()
        if crew and s.agent == crew
    }
    return next((p for p in on if id(p) in mine), next(iter(on), None))


def harness_effort(
    state: Any, backend: str, model: str | None, crew: str = "", *, model_answers: bool = True
) -> dict:
    """``effort_supported`` and ``effort_levels`` for a session on *backend* before it reports its own.

    Where the level rides the model (kiro-cli, claude) *model* answers over the shared
    vocabulary, as the live provider's own gate does. ``model_answers=False`` returns
    nothing there instead, for a caller that judges the model itself: the crew editor
    follows the pick the user is making, which a saved model would contradict.

    Where the harness carries it in a session option (goose, pi) or splits a model pair
    onto one (codex), only that harness's build can say, and with no live session on it
    ``effort_supported`` is ``None``: unknown, not unsupported.

    A model-scoped advertised option can answer only for the model that live session
    actually serves. A different or unknown model leaves both support and levels unknown,
    even for the crew's own session: its slot can override the crew's pin. A pair-id
    harness (codex) instead supplies build-level option vocabulary and keeps support
    unknown when levels exist; the target model still decides whether it takes them.
    """
    if backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION | ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS:
        live = _live_on(state, backend, crew)
        if live is None or (
            backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
            and (not model or model == "auto" or getattr(live, "served_model", "") != model)
        ):
            return {"effort_supported": None, "effort_levels": []}
        advertised = [lvl for lvl in live.get_valid_effort_levels() if isinstance(lvl, str)]
        # A real session registers its own levels (goose's ``off``) into the shared
        # vocabulary, so the vocabulary filter alone cannot tell an off-only option apart.
        levels = [lvl for lvl in advertised if lvl in get_reasoning_effort_values()]
        supported: bool | None
        pair_ids = backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
        if pair_ids:
            supported = None if levels else False
        elif not live.supports_effort():
            supported = None if advertised else False
        elif any(lvl != "off" for lvl in levels):
            supported = True
        else:
            supported = None
        if supported is False or (supported is None and not pair_ids):
            # Levels an unknown answer would carry are that other session's model's.
            levels = []
    elif not model_answers or model is None:
        return {}
    else:
        supported = model_supports_effort(model)
        levels = list(EFFORT_LEVELS) if supported else []
    return {
        "effort_supported": supported,
        "effort_levels": cap_effort_capability_levels(levels, source="harness"),
    }

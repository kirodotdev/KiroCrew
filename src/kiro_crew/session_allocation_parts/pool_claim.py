"""The model checks a warm-pool claim makes before it switches the claimed process.

``get_or_create`` imports these and stays the path callers and tests import them
from. None is a ``SessionAllocationService`` member.
"""

from __future__ import annotations

from typing import Any, cast


async def _revalidated_pin(provider: Any, model: str, deps: Any) -> str:
    """*model* in the spelling a fresh backend answer serves it under, else ``""``.

    A probe that fails or answers nothing is no evidence of entitlement, so it
    keeps the snapshot's verdict (``""``), as the cold-start withhold does.
    """
    try:
        rows = await cast(Any, provider).client.refresh_available_models()
        fresh = deps.advertised_model_ids(rows)
    except Exception:  # noqa: BLE001 - a failed probe keeps the snapshot verdict
        return ""
    if not fresh:
        return ""
    if not deps.model_is_unusable(model, fresh):
        return model
    return deps.resolve_pin_spelling(model, fresh, provider)

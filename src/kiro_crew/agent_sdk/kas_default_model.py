"""kiro-cli's ``chat.defaultModel`` for a session created on a running KAS runtime.

kiro-cli starts every unpinned session on its ``cli.json`` ``chat.defaultModel``;
KAS reads no ``cli.json``. The chat start applies the setting itself. A session
created straight on an already-running runtime (a sub-agent sharing its parent's
process, a workflow run's planner, step or review session) takes it through
:func:`serve_kiro_cli_default_model`, so that code reaches the ACP layer through
this boundary. The ACP-layer reads happen when it is called, so importing this
module costs only the stdlib.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def serve_kiro_cli_default_model(
    runtime: Any,
    handle: Any,
    work_dir: Path | str | None,
    agent_model: Callable[[], str],
) -> None:
    """Start an unpinned session on a KAS *runtime* on kiro-cli's ``chat.defaultModel``.

    *work_dir* is the folder the session runs in (the runtime's own when empty),
    whose ``cli.json`` wins over the global one. *agent_model* answers the model
    Crew itself pins for the session's starting agent (``auto`` or ``""``: none),
    the chat start's ``configured_model`` test; a pinned one skips the setting, and
    so does one that cannot be resolved. Both reads touch files and run off the
    loop. The model is applied as INHERITED, so the session stays unpinned; a
    refusal is logged and leaves KAS's own default. Any other backend sends nothing.
    """
    from kiro_crew.acp.kas_cli_settings import kiro_cli_default_model
    from kiro_crew.acp.runtime_models import DEFAULT_MODEL
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    is_kas = getattr(runtime, "acp_backend", "") == ACP_BACKEND_KAS

    def _read() -> str:
        try:
            if (agent_model() or "") not in ("", DEFAULT_MODEL):
                return ""
        except Exception:  # Crew's own pick is unknown: it may name one
            logger.debug("KAS default model: agent model unresolved", exc_info=True)
            return ""
        return kiro_cli_default_model(work_dir or getattr(runtime, "_work_dir", None))

    model = await asyncio.to_thread(_read) if is_kas else ""
    if not model:
        return
    try:
        await handle.set_model(model, inherit=True)
    except Exception:
        logger.warning(
            "Failed to apply kiro-cli's chat.defaultModel %s to the KAS session",
            model,
            exc_info=True,
        )

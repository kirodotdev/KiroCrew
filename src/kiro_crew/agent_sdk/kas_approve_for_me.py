"""The application's seam to KAS "approve for me" (:mod:`kiro_crew.acp.kas_approve_for_me`).

The dashboard and the session allocator reach the ACP layer only through
:mod:`kiro_crew.agent_sdk`; these are the few names they use, each a thin call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def register_owner_check(check: Callable[[str], bool]) -> None:
    """Install the dashboard's answer to "may this session keep approve-for-me on?"."""
    from kiro_crew.acp import kas_approve_for_me

    kas_approve_for_me.register_owner_check(check)


def engine_supported() -> bool:
    """Whether the engine is known to send its approve-for-me verdict to Crew. Memory only."""
    from kiro_crew.acp import kas_approve_for_me

    return kas_approve_for_me.engine_supported()


def refresh_engine_support() -> bool:
    """Re-read whether the engine sends its verdict to Crew. Blocking."""
    from kiro_crew.acp import kas_approve_for_me

    return kas_approve_for_me.refresh_engine_support()


def refresh_engine_support_soon() -> Any:
    """:func:`refresh_engine_support` on a worker thread, from a running loop; else now."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return refresh_engine_support()
    return loop.create_task(asyncio.to_thread(refresh_engine_support))


def session_env(session_key: str, extra_env: dict[str, str] | None) -> dict[str, str] | None:
    """*extra_env* for a session about to start, with the engine switch set for it."""
    from kiro_crew.acp import kas_approve_for_me

    return kas_approve_for_me.session_env(session_key, extra_env)

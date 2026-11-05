"""The ``capabilities.browse`` refusal every gated ``/api/browser/*`` route answers.

One shape for all of them: ``403 {"error": DENIED_TEXT, "code": "policy_denied"}``,
which the ``browser`` tool and the dashboard both read by its code. A deny also stops
a browser that is still running (``browser_harness.governance.stop_running_harness``).
The decision itself, its fail-closed degrade and which surface it reads are
``browser_harness/governance.py``'s.

Routes left ungated on purpose, because each only ends something: ``live/close``
(fold the card), ``input/release`` (hand back), ``browser/disconnect`` (stop the
engine).
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.browser_harness.governance import (
    DASHBOARD_SURFACE_KEY,
    DENIED_CODE,
    DENIED_TEXT,
    browse_denied_async,
    stop_running_harness,
)


async def browse_refusal(
    *, session_key: str = DASHBOARD_SURFACE_KEY, audit: bool = True
) -> web.Response | None:
    """The 403 to answer when the ceiling denies browsing, else ``None``."""
    if not await browse_denied_async(session_key, audit=audit):
        return None
    stop_running_harness()
    return web.json_response({"error": DENIED_TEXT, "code": DENIED_CODE}, status=403)


__all__ = ["browse_refusal"]

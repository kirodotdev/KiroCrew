"""Refuse the gateway's file, diff and Git routes for a chat that runs remotely.

These routes read and write paths on THIS machine. A chat bound to a session
environment works in its own workspace, so showing it this machine's copy of a
file, diff or repository would show it the wrong files. The dashboard already
withholds those tabs for such a chat; this guard is the server-side backstop for
any request that names one with ``?slot=<chat>``.

Scope, deliberately narrow:

* A request that names no chat, or a chat the gateway does not hold, runs as
  before: the routes also serve callers that are not a chat (library previews,
  artifacts, image thumbnails).
* With the developer flag off every chat runs here, so nothing is refused and
  the placement store is never read.
* Only the dashboard owner is checked against placement. Any other caller goes
  on to the route's own owner check and gets the refusal it gets today, so the
  guard never tells a non-owner where a chat runs.
* An unreadable placement counts as remote (fail closed), as
  :func:`kiro_crew.session_environment.session_runs_remotely` answers.

The refusal names no path: it is the same fixed message for every route.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

#: The ``code`` of the refusal; the client matches on it.
CHAT_RUNS_REMOTELY = "chat_runs_remotely"
_MESSAGE = (
    "This chat runs on a remote environment, so files and Git on this machine"
    " are not shown for it. Use the chat's Files panel instead."
)


def chat_runs_remotely_refusal() -> web.Response:
    """The one answer a guarded route gives for a remote chat."""
    return web.json_response({"error": _MESSAGE, "code": CHAT_RUNS_REMOTELY}, status=409)


def _audit(request: web.Request, slot_name: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=str(request.get("user") or "dashboard"),
            operation="file_api.remote_chat",
            outcome="denied",
            source="dashboard",
            resources=f"route={request.path},chat_remote=1",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for remote chat %s failed", slot_name, exc_info=True)


async def remote_chat_refusal(request: web.Request) -> web.Response | None:
    """A route guard: the refusal when ``?slot=`` names a chat that runs remotely."""
    slot_name = request.query.get("slot", "").strip()
    if not slot_name:
        return None
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return None
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        return None
    state = request.app.get("state")
    get_slot = getattr(state, "get_slot", None)
    slot = get_slot(slot_name) if callable(get_slot) else None
    if slot is None:
        return None
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.session_environment import session_runs_remotely

    if not await asyncio.to_thread(session_runs_remotely, effective_session_key(slot)):
        return None
    _audit(request, slot_name)
    return chat_runs_remotely_refusal()

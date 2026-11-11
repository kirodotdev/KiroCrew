"""``GET /api/telemetry/prompt-as-sent?slot=&turn=``: the text one turn sent the agent.

The Context tab's "Prompt as sent", under the selected turn. ``context-trace`` says
how many characters each block of the turn had; this returns the text itself, read
from :mod:`kiro_crew.prompt_trace`'s in-memory record and joined to the tab's turn
through the ``usage`` fold, which knows the composer's turn number behind each
ordinal it draws.

Owner-only, as the Crew log is: the text holds the conversation and a crew member's
own memory, which only the dashboard owner can read today. An app caller is refused
first, with the 404 an unknown route gives and a SEL line, as ``context-trace``
refuses one; any other caller that is not the owner gets ``owner_only``. Secrets are
masked on the way out with the chain other prompt egresses use.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from aiohttp import web

from kiro_crew import prompt_trace
from kiro_crew import sel as _sel_mod
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import require_owner_dashboard_request
from .usage_context import _usage_read


def prompt_as_sent(slot: str, ordinal: int) -> dict[str, Any] | Literal["not_kept", "too_long"]:
    """Turn *ordinal* of *slot* as sent, secrets masked; else why there is no text:
    ``too_long`` when it was over ``prompt_trace.MAX_PROMPT_CHARS``, else ``not_kept``."""
    from kiro_crew.crew_log.projection import context_turn_number, newest_unit_in_succession

    _context, ctx, _revision = _usage_read(slot)
    number = context_turn_number(ctx, ordinal) if ctx is not None else None
    if number is None:
        return "not_kept"
    text = prompt_trace.read(newest_unit_in_succession(slot), number)
    if text is prompt_trace.TOO_LONG:
        return "too_long"
    if text is None:
        return "not_kept"
    return {
        "slot": slot,
        "turn": ordinal,
        "text": redact_credentials(redact_exfiltration_urls(text)[0])[0],
    }


async def api_prompt_as_sent(request: web.Request) -> web.Response:
    """The selected turn's prompt text; ``404 not_kept`` once it is not held, ``404
    too_long`` when it was too long to keep."""
    request_app = str(request.get("app", "") or "")
    slot = (request.query.get("slot") or "").strip()
    if request_app:

        def _audit_denied() -> None:
            _sel_mod.sel().log_api_access(
                caller=request_app,
                operation="prompt_as_sent",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot or '(missing)'}",
                error="dashboard-only endpoint",
            )

        await asyncio.to_thread(_audit_denied)
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    denied = await require_owner_dashboard_request(request, "prompt_as_sent.read")
    if denied is not None:
        return denied
    raw = (request.query.get("turn") or "").strip()
    if not slot:
        return web.json_response({"error": "slot is required", "code": "slot_required"}, status=400)
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9 or int(raw) <= 0:
        return web.json_response(
            {"error": "turn must be a positive integer", "code": "turn_invalid"}, status=400
        )
    body = await asyncio.to_thread(prompt_as_sent, slot, int(raw))
    if body == "too_long":
        return web.json_response(
            {"error": "prompt was too long to keep", "code": "too_long"}, status=404
        )
    if body == "not_kept":
        return web.json_response({"error": "prompt is not kept", "code": "not_kept"}, status=404)
    return web.json_response(body)

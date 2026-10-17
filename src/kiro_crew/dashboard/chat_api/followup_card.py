"""The follow-up card: ``POST /api/chat/slots/{slot}/followup``, which backs the
``suggest_followup`` MCP tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    import time

    from kiro_crew.dashboard.chat_handlers import (
        _FOLLOWUP_TEXT_FIELDS,
        SUGGEST_FOLLOWUP_SCHEMA,
        DashboardState,
        ValidationError,
        deny_non_dashboard_caller,
        logger,
        read_bounded_json,
        redact_credentials,
        redact_exfiltration_urls,
        slot_not_found,
        validate_tool_args,
    )


def _redact_followup_item(item: dict) -> dict:
    """Return a display-safe copy of one follow-up item.

    Every string is LLM-authored and renders in the dashboard DOM, so it goes
    through the same credential + exfiltration-URL redaction as chat content
    (mirrors the AskUserQuestion path in chat_runner). ``branch`` is omitted
    when absent so the frontend can fall back to deriving one from the title.
    """
    out: dict[str, str] = {}
    for key in _FOLLOWUP_TEXT_FIELDS:
        text = str(item.get(key) or "")
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        out[key] = text
    branch = item.get("branch")
    if isinstance(branch, str) and branch:
        # `branch` is LLM-authored too, and it travels further than the text
        # fields: into a git ref, a directory name, SEL records and logs. Run the
        # same redactors, and if either one CHANGES it, drop the field rather than
        # ship a mangled ref — the frontend then derives a branch from the title.
        scrubbed, _ = redact_exfiltration_urls(branch)
        scrubbed, _ = redact_credentials(scrubbed)
        if scrubbed == branch:
            out["branch"] = branch
    return out


async def api_chat_slot_followup(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/followup — show an agent-authored follow-up card.

    Backs the ``suggest_followup`` MCP tool. Reachable over loopback HTTP from
    inside the kiro-cli process group, so the payload is re-validated here
    against the same schema the MCP layer used: this endpoint is a trust
    boundary in its own right, not merely a relay.

    The card is ephemeral (broadcast-only, held in frontend state) and one card
    per slot: a second call replaces an unacted-on card rather than stacking.
    """
    state: DashboardState = request.app["state"]
    denied = deny_non_dashboard_caller(request, "chat_slot_followup")
    if denied is not None:
        return denied
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    try:
        cleaned = validate_tool_args(body, SUGGEST_FOLLOWUP_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    items = [_redact_followup_item(item) for item in cleaned.get("items") or []]
    if not items:
        return web.json_response({"error": "items must not be empty"}, status=400)
    # The card is delivered by broadcast only — nothing is stored server-side —
    # so with no WS client attached the suggestions are dropped on the floor.
    # Report the number of sends that COMPLETED instead of an unconditional
    # success, so the MCP tool can tell the model to restate the follow-ups in
    # its reply text rather than being assured they were shown and steered into
    # silence.
    #
    # This send is AWAITED: a socket count is taken before any send runs, so an
    # owner window that disconnects in that window produced a failed send already
    # reported as delivered.
    #
    # OWNER clients only: an app token can open /api/ws, and an all-clients
    # broadcast would hand it another user's complete handoff prompts.
    try:
        clients = int(
            await state.deliver_ws_owners(
                "followup_card",
                {"slot": slot.key, "items": items, "ts": time.time()},
            )
        )
    except Exception:  # pragma: no cover - defensive: delivery must not 500
        logger.debug("Follow-up card delivery failed", exc_info=True)
        clients = 0
    logger.info(
        "Slot %s follow-up card broadcast with %d item(s) to %d client(s)",
        name,
        len(items),
        clients,
    )
    resp: dict[str, Any] = {"ok": True, "count": len(items), "delivered": clients}
    if not getattr(slot, "project", ""):
        # Parity with session_directive_apply._suggest_followup: an unscoped
        # slot's card offers a plain new chat instead of the worktree route,
        # and the caller (the MCP relay, and through it the model) must hear
        # that from the delivery path — the tool description alone cannot know
        # this slot.
        resp["warning"] = (
            "this session has no project directory, so the card offers 'Start "
            "in new chat' (a new chat with no worktree) instead of 'Start in "
            "new worktree'; for isolated git work, suggest scoping a git "
            "repository first"
        )
    return web.json_response(resp)

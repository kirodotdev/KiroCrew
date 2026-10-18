"""The session summary routes: the cached read and the on-demand generate."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        KiroCrewConfig,
        count_user_turns_in_records,
        deny_app_slot_access,
        generate_session_summary,
        is_incognito_transcript,
        read_cached_intent_summary,
        slot_not_found,
    )


async def api_chat_slot_summary(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/summary — intent summary for the panel.

    Read-only: it never triggers generation. Summaries are produced at turn end
    by the background pass, deliberately, so that opening the panel cannot spend
    tokens and repeated opening cannot turn into a refresh loop.

    Responses:
      - 200 with ``{enabled, generated_at, stale, intents, constraints, ...}``
      - 200 with ``intents: []`` and ``enabled: false`` when the feature is off,
        so the panel can render an explanatory empty state rather than an error
      - 404 ``slot_not_found`` for an unknown slot, or for a slot an app caller
        does not own (App Kit §5.2 isolation; 404 not 403 for anti-enumeration)
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    # App ownership check (App Kit §5.2), mirroring api_chat_slot_delete: a
    # summary is derived conversation content, so a slot merely existing must
    # not make it readable. Dashboard users carry an explicit empty request_app
    # and are unaffected; an app token may only read summaries for slots it
    # created, never for unscoped slots.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_summary_read")
    if denied is not None:
        return denied

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    enabled = bool(cfg.session_summary.enabled)

    payload: dict | None = None
    stale = False
    log = state.conversation_log
    # Gate the cache read on the flag as well: turning the feature off has to
    # stop serving summaries, not just stop producing them, or a sidecar written
    # during an earlier opt-in keeps being returned after opt-out.
    if enabled and log is not None:
        payload, stale = await read_cached_intent_summary(log, slot)

    body: dict = {
        "enabled": enabled,
        "stale": stale,
        "intents": (payload or {}).get("intents", []),
        "constraints": (payload or {}).get("constraints", []),
        "generated_at": (payload or {}).get("generated_at"),
        "user_turns": (payload or {}).get("user_turns"),
        "last_activity": (payload or {}).get("last_activity"),
        "generate_state": _generate_state(cfg, slot),
    }
    return web.json_response(body)


def _generate_state(cfg: KiroCrewConfig, slot: Any) -> str:
    """Which on-demand affordance the panel should offer for *slot*.

    Three values, because the panel has three honest things to say and a bool
    could only carry two: ``ready`` (offer the button), ``too_few_turns`` (say so
    plainly and offer nothing -- a click could only fail), and ``unavailable``
    (the feature is off, a pass is already running, or the session is incognito
    and must never leave a durable artifact). Collapsing the last two into
    "not enough messages" would print a reason that is simply untrue for an
    incognito session.

    The turn count is an ESTIMATE from the slot's IN-MEMORY messages, not a
    transcript read: this runs on every panel mount and tab switch, and reading a
    thousand-message session from disk to answer a yes/no question is waste. A
    restored slot keeps only a window of its transcript, and the window is NOT a
    safe proxy for the whole session -- a tail made mostly of assistant replies
    and injected automation messages can hold fewer than the minimum genuine user
    turns while the file holds dozens. So `too_few_turns` is only claimed when the
    window IS the whole session (`_disk_older_count == 0`); a truncated window
    reports `ready` and lets the POST's disk-backed count decide.

    The authoritative gate lives in the generator and reads disk; if this estimate
    is wrong the POST refuses and says why, so the cost is a refused click, never
    a wasted call.

    A turn in flight is deliberately NOT one of these values, even though the
    generator refuses one. This field is only refreshed when a summary is written,
    so a state that begins and ends mid-turn would arrive stale and stay stale: a
    turn that ends without producing a summary (stopped, or gated by cadence)
    pushes no event, and the panel would sit on a dead verdict until it remounted.
    The panel already holds a live per-slot turn signal, so it owns that
    presentation and this field stays limited to what only the server knows.
    """
    if not cfg.session_summary.enabled:
        return "unavailable"
    if getattr(slot, "_summary_in_flight", False):
        return "unavailable"
    if is_incognito_transcript(getattr(slot, "memory_mode", "")):
        return "unavailable"
    turns = count_user_turns_in_records(getattr(slot, "messages", []) or [])
    if turns < cfg.session_summary.min_user_turns and not getattr(slot, "_disk_older_count", 0):
        return "too_few_turns"
    return "ready"


async def api_chat_slot_summary_generate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/summary — summarize this session on request.

    The companion to the read-only GET. Generation stays off the read path so
    that opening the panel can never spend tokens; this route exists because the
    turn-end trigger alone leaves every session that predates the feature -- or
    that simply has not been touched since it was switched on -- permanently
    empty, with nothing a person can do about it from the panel.

    Explicit consent is the whole justification for the spend, so there is no
    batch form: one request summarizes one session.

    Responses:
      - 200 with the same body as the GET, once a summary exists
      - 409 ``summary_disabled`` / ``summary_in_flight`` / ``summary_unavailable``
        when no summary could be produced, so the panel can say which
      - 404 ``slot_not_found`` for an unknown slot, or one an app does not own
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    # Same App Kit §5.2 isolation as the GET: generating is strictly more
    # privileged than reading, so it can never be the laxer of the two.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_summary_generate")
    if denied is not None:
        return denied

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()
    if not cfg.session_summary.enabled:
        return web.json_response(
            {"error": "session summaries are switched off", "code": "summary_disabled"},
            status=409,
        )
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "no conversation log", "code": "summary_unavailable"},
            status=409,
        )
    # Reported separately from the generic failure because it is the one the
    # panel can explain as "already working" rather than "could not".
    if getattr(slot, "_summary_in_flight", False):
        return web.json_response(
            {"error": "a summary is already being written", "code": "summary_in_flight"},
            status=409,
        )
    # Likewise distinct: a turn in flight is a wait-and-retry, not a refusal. The
    # generator would decline anyway; saying so here keeps the panel from
    # reporting a transient state as a failure.
    if getattr(slot, "running", False):
        return web.json_response(
            {"error": "this session has a turn in progress", "code": "summary_turn_running"},
            status=409,
        )

    if request.get("app", ""):
        await generate_session_summary(
            state,
            slot,
            cfg=cfg,
            force=True,
            still_current=lambda: state._slots.get(name) is slot,
        )
        if state._slots.get(name) is not slot:
            return slot_not_found()
    else:
        await generate_session_summary(state, slot, cfg=cfg, force=True)

    # Read back rather than trusting the return value: a forced pass returns
    # False both when it produced nothing AND when the cached summary was
    # already current, and those are opposite outcomes for the panel. Through
    # the same gated read as the GET: a pass the generator skipped for
    # ``memory_mode`` must not be answered with a sidecar left over from the
    # key's earlier persistent life.
    payload, stale = await read_cached_intent_summary(log, slot)
    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()
    if payload is None:
        return web.json_response(
            {"error": "could not summarize this session", "code": "summary_unavailable"},
            status=409,
        )
    return web.json_response(
        {
            "enabled": True,
            "stale": stale,
            "intents": payload.get("intents", []),
            "constraints": payload.get("constraints", []),
            "generated_at": payload.get("generated_at"),
            "user_turns": payload.get("user_turns"),
            "last_activity": payload.get("last_activity"),
            "generate_state": _generate_state(cfg, slot),
        }
    )

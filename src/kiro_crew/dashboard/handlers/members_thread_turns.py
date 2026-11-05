"""The turns a crewmate's pinned thread starts or stops at the page's request.

The once-only first welcome (``POST /api/members/{slug}/greet``) and fresh start
(``POST /api/members/{slug}/fresh-start``): stopping a running turn, clearing the
conversation through the typed ``/clear`` path, and recording the fold boundary in the
member's log.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members_access import _deny_app_caller
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import MemberSlugError

logger = logging.getLogger("kiro_crew.dashboard.handlers.members")


async def api_member_greet(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/greet — a crewmate's once-only first welcome.

    The dashboard calls this after opening a member thread. It starts a turn
    only for a crewmate's own pinned thread whose creation recorded a welcome
    (Mate's, or the goal question of a crewmate made on the New crewmate card),
    only while that thread is empty, and at most once ever (see
    :mod:`kiro_crew.dashboard.mate_welcome`). Every other case answers 200 with
    the outcome that declined it, so the caller never has to branch on errors
    for an ordinary "nothing to do".
    """
    from kiro_crew.dashboard.mate_welcome import maybe_start_first_greeting

    denied = await _deny_app_caller(request, "members.greet")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, "members.greet")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return web.json_response(
            {"error": "dashboard state unavailable", "code": "state_unavailable"}, status=503
        )
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    try:
        # The tab's rendered language: the welcome's only language signal on
        # an install that follows the browser (mate_welcome.hidden_turn_language).
        outcome = await maybe_start_first_greeting(
            state, slug, request.headers.get("X-UI-Lang", "")
        )
    except Exception:
        logger.warning("first welcome failed to start for %r", slug, exc_info=True)
        return web.json_response(
            {"error": "could not start the greeting", "code": "member_greet_failed"}, status=500
        )
    return web.json_response({"outcome": outcome})


#: How long each stop phase waits for the turn to end before moving on: the
#: cooperative cancel first, then the hard-kill escalation.
_FRESH_START_STOP_WAIT_SECS = 10.0
_FRESH_START_POLL_SECS = 0.05


def _turn_busy(state: DashboardState, slot: Any, key: str) -> bool:
    """The reset route's two turn probes: the slot's own task, and the session's."""
    provider = state.sessions.get_provider(key)
    return bool(slot.running) or (provider is not None and provider.has_active_turn())


def _work_pending(slot: Any) -> bool:
    """Queued messages or unconsumed steers: work the runner starts on its own."""
    return bool(slot.queue_depth) or bool(getattr(slot, "_pending_steers", None))


def _queue_pending(name: str) -> web.Response:
    return web.json_response(
        {"error": "queued messages pending", "code": "slot_queue_pending", "slot": name},
        status=409,
    )


async def _turn_ends_within(state: DashboardState, slot: Any, key: str) -> bool:
    deadline = asyncio.get_running_loop().time() + _FRESH_START_STOP_WAIT_SECS
    while _turn_busy(state, slot, key):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_FRESH_START_POLL_SECS)
    return True


async def api_member_fresh_start(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/fresh-start — a clean context on the SAME thread.

    Runs existing mechanisms in order and adds none:

    1. A running turn is stopped with ``stop_slot_turn``, the Stop button's own
       path. A second call while the first is still pending is that function's
       hard-kill escalation, which is how a stuck process is freed.
    2. The conversation is cleared through the typed ``/clear`` path
       (``_answer_slash_without_channel``): discard with replay off, plan
       dropped, eager respawn, and its transcript line. The discard shuts the
       agent process down, so the next turn runs in a fresh process.

    The slot key is never changed and no slot is created: monitor loops, the
    work ledger and the crew log are bound to it. The transcript stays on disk.
    Same gates as every member route: app tokens get 404, and only the owner's
    dashboard passes.
    """
    from kiro_crew.dashboard import chat_handlers as ch
    from kiro_crew.dashboard.chat_runner import _answer_slash_without_channel

    denied = await _deny_app_caller(request, "members.fresh_start")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, "members.fresh_start")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return web.json_response(
            {"error": "dashboard state unavailable", "code": "state_unavailable"}, status=503
        )
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    slot = state._slots.get(binding.get("slot_key", "")) if binding else None
    if (
        binding is None
        or slot is None
        or slot.mode != members_mod.DM_SLOT_MODE
        or slot.agent != binding.get("member")
        or getattr(slot, "executor", "") == "remote"
    ):
        return ch.slot_not_found()
    name = slot.key
    key = effective_session_key(slot)
    from kiro_crew.dashboard.warm_task_brief import cancel_warm_task_brief

    cancel_warm_task_brief(state, name)

    # A soft stop keeps the queue and the unconsumed steers, and the runner
    # drains them the moment the turn ends: they would start on the conversation
    # being dropped. Refused up front, and again after every stop wait, since a
    # message can be queued while the stop waits.
    if _work_pending(slot):
        return _queue_pending(name)

    if _turn_busy(state, slot, key):
        for _attempt in range(2):
            await ch.stop_slot_turn(
                state, slot, cancel_key=ch._cancel_target(slot), source="members.fresh_start"
            )
            settled = await _turn_ends_within(state, slot, key)
            if _work_pending(slot):
                return _queue_pending(name)
            if settled:
                break
        else:
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight", "slot": name},
                status=409,
            )
        # The stop awaited; a rebind or replacement may have landed meanwhile.
        if state._slots.get(name) is not slot or effective_session_key(slot) != key:
            return web.json_response(
                {"error": "slot changed during the stop", "code": "session_rebound"},
                status=409,
            )

    # The same path a typed /clear takes: the discard with replay off, the plan
    # dropped, the eager respawn, and the transcript line that says the agent no
    # longer remembers what is above it. A session that still refuses the discard
    # keeps it queued for its next turn boundary, as /clear does.
    reset_at = datetime.now(timezone.utc).isoformat()
    outcome = await _answer_slash_without_channel(state, slot, key, "/clear")
    cleared = outcome == "discarded"
    # Whether the fold boundary landed in the member's own log, so the client
    # can tell "durable, survives a reload" from "this session's own fold
    # only" instead of assuming the write always lands. The CLEAR itself is
    # never retried on a write failure here: it already happened (the agent
    # process is gone, the transcript already carries its line), and asking
    # the client to redo it would discard a second turn's history for a
    # boundary-recording fault that has nothing to do with the conversation.
    reset_persisted = False
    if cleared:
        # Durable fold so the "earlier conversation" boundary survives a reload:
        # the roster projection carries `reset_at`, which `GET /api/members`
        # serves back to the page that otherwise only holds it in React state.
        # Off-loop; a logging fault never turns a cleared conversation into an
        # error response, since the clear itself already landed -- but the
        # outcome IS inspected, so a failed append is reported rather than
        # silently claimed as durable (the client still folds for this
        # session from its own optimistic state either way).
        member_name = binding.get("member") if binding else None

        def _emit_reset() -> bool:
            from kiro_crew import eventlog_hooks
            from kiro_crew.eventlog.types import MEMBER_RESET

            return eventlog_hooks.emit(slug, member_name, MEMBER_RESET, {"reset_at": reset_at})

        reset_persisted = await asyncio.to_thread(_emit_reset)
        if not reset_persisted:
            logger.warning(
                "fresh-start reset_at for %r did not persist to the member log; "
                "the fold holds only for this client's current session",
                slug,
            )
    return web.json_response(
        {
            "slot": name,
            "reset_at": reset_at,
            "outcome": "cleared" if cleared else "queued",
            "reset_persisted": reset_persisted,
        }
    )

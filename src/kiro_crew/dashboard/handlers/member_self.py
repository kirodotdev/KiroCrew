"""HTTP routes behind the ``kirocrew-mate`` tools: a crewmate changes itself.

Two halves, split the way the guide and the change cards are:

* The **agent half** is MCP-only, under the strict ``/api/member/self`` prefix
  (``dashboard.server`` internal-secret list). The crewmate it acts on is the
  one whose pinned thread the verified caller's slot is -- never a name from the
  body -- and every route admits only a turn running in that dashboard slot
  that the user sent (:func:`handlers.guide._resolve_agent_caller`, with the
  operation in its ``_USER_TURN_OPERATIONS``). A channel message, a schedule,
  a subagent or an app cannot change anyone. ``propose`` also admits a turn the
  user did not send when it runs in this crewmate's own thread (a check-in, a
  sub-agent's result, the goal-setup nudge), and only for
  :data:`OWN_GOAL_PROPOSALS`: each is still a card the user decides.

  - ``POST /api/member/self/update`` writes the presentation fields a crewmate
    owns (:data:`kiro_crew.members.SELF_EDITABLE_FIELDS`) at once, records the
    change for undo (:mod:`member_self_changes`) and draws one row with an Undo
    in the crewmate's chat (:mod:`chat_cards`, surface ``self``).
  - ``POST /api/member/self/propose`` turns a named request (a model, an
    approval mode, a tools draft) into the existing change card for the
    caller's own member: the crewmate names what it wants, the card engine
    derives what changes, and nothing changes until the user presses the card.

* The **browser half**, ``POST /api/members/{slug}/self-changes/{id}/undo``,
  is owner-only cookie auth, deliberately off the strict prefix: taking a
  change back is the user's click, and the value put back is the gateway's own
  record, never the row's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.dashboard import chat_cards, member_self_changes
from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.guide import (
    REQ_OWN_GOAL_TURN,
    _audit,
    _deny,
    _json_body,
    _refusal,
    _resolve_agent_caller,
)

logger = logging.getLogger(__name__)

#: ``update_self`` operation name, in :data:`handlers.guide._USER_TURN_OPERATIONS`.
OP_UPDATE_SELF = "member.update_self"
#: ``propose`` operation name, likewise.
OP_PROPOSE_SELF = "member.propose_self"

#: What a crewmate may ask the user for through a card, by request name, and
#: the change-card kind each becomes -- kinds the dashboard already applies.
#: The crewmate never names a kind itself.
SELF_PROPOSALS: dict[str, str] = {
    "model": "crewmate.update",
    "approval_mode": "crewmate.update",
    "capabilities": "crewmate.capabilities",
    # A goal added or changed (``set_goal``): the user asked the crewmate to work
    # toward its goals, so only the user's button changes one.
    "goal": "crewmate.goal",
    # The key results the crewmate worked out for that goal (``set_key_results``),
    # replacing the whole set once the user confirms.
    "key_results": "crewmate.key_results",
    # Which check-ins run and when (``set_check_ins``), once the user confirms.
    "check_ins": "crewmate.check_ins",
}

#: The requests a crewmate may make in its own thread from a turn the user did
#: not send (``handlers.guide.REQ_OWN_GOAL_TURN``): its own goal, milestones
#: and check-ins, each still a card the user decides.
OWN_GOAL_PROPOSALS = frozenset({"goal", "key_results", "check_ins"})


def _caller_member(request: web.Request, slot_key: str) -> tuple[str, str] | None:
    """``(member, memory_store)`` of the crewmate whose own thread *slot_key* is."""
    slot = request.app["state"].get_slot(slot_key)
    member = getattr(slot, "agent", "") if slot is not None else ""
    if (
        slot is None
        or getattr(slot, "mode", "") != members_mod.DM_SLOT_MODE
        or not isinstance(member, str)
        or not member
    ):
        return None
    return member, str(getattr(slot, "memory_store", "") or "default")


def _not_writable(sk: str, operation: str) -> web.Response:
    # The crewmate's row cannot be published from here, e.g. one declared only
    # in ``config.local.json``: a refusal the model can say, never a 500.
    _audit(sk, operation, "denied", "member_not_writable")
    return _deny(
        409, "member_not_writable", "this crewmate's settings cannot be changed from the chat"
    )


async def api_member_self_update(request: web.Request) -> web.Response:
    """POST /api/member/self/update — ``{"fields": {...}}`` → the applied change.

    Body fields are a subset of :data:`members.SELF_EDITABLE_FIELDS`: the new
    display name, description text, or ``{"traits": {...}}`` for the face. The
    answer carries ``changed`` (the fields whose stored value moved; a value
    equal to the current one changes nothing and draws nothing) and the
    ``change_id`` of the undo record when something did.
    """
    from kiro_crew.memory_stores import (
        MemberAlreadyExists,
        MemberApprovalConflict,
        UnknownMemoryStore,
    )

    try:
        slot_key, sk = await _resolve_agent_caller(request, OP_UPDATE_SELF)
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    who = _caller_member(request, slot_key)
    if who is None:
        _audit(sk, OP_UPDATE_SELF, "denied", "not a crewmate thread")
        return _deny(403, "not_a_crewmate", "only a crewmate, in its own chat, can change itself")
    member, thread_store = who
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        _audit(sk, OP_UPDATE_SELF, "denied", "no_fields")
        return _deny(400, "no_fields", "'fields' must be a non-empty object")
    try:
        # The store the caller's thread runs on: a crewmate deleted and created
        # again under the same key has another, and its row is not this turn's.
        result = await asyncio.to_thread(
            members_mod.update_member_self, member, fields, expected_store=thread_store
        )
    except members_mod.SelfUpdateError as exc:
        _audit(sk, OP_UPDATE_SELF, "denied", exc.code)
        if exc.code == "not_a_crewmate":
            status = 403
        elif exc.code == "changed_meanwhile":
            status = 409
        else:
            status = 400
        return _deny(status, exc.code, str(exc))
    except (UnknownMemoryStore, MemberAlreadyExists, MemberApprovalConflict):
        return _not_writable(sk, OP_UPDATE_SELF)
    answer: dict[str, Any] = {
        "ok": True,
        "member": member,
        "name": result["name"],
        "changed": result["changed"],
        "after": result["after"],
    }
    if result["changed"]:
        slug = await asyncio.to_thread(members_mod.member_slug, member)
        rec = member_self_changes.record(
            member=member,
            slug=slug,
            slot_key=slot_key,
            memory_store=thread_store,
            changed=result["changed"],
            before=result["before"],
            after={k: result["after"][k] for k in result["changed"]},
            name=str(result.get("name") or ""),
        )
        state = request.app["state"]
        # The row lands at this point of the transcript, where the tool call was.
        await chat_cards.load_conversation_window(state, rec)
        chat_cards.record_self_change(state, rec)
        answer["change_id"] = rec["id"]
        answer["undoable"] = rec["undoable"]
    _audit(sk, OP_UPDATE_SELF, "ok", ",".join(result["changed"]))
    return web.json_response(answer)


async def api_member_self_propose(request: web.Request) -> web.Response:
    """POST /api/member/self/propose — ``{"request": <name>, "params": {...}, "reason"?}``.

    ``request`` is a key of :data:`SELF_PROPOSALS`; ``params`` are that
    request's own fields (``model``: ``{"model"?, "reasoning_effort"?}``;
    ``approval_mode``: ``{"approval_mode": mode}``; ``capabilities``:
    ``{"draft": {...}}``; ``goal``: ``{"long_term": text, "goal_id"?: id}``). The member is always the caller's
    own. The card is proposed exactly as ``propose_change`` would propose it and
    the answer is the card's public record.
    """
    from kiro_crew.dashboard.handlers.change_cards import propose_for_caller

    try:
        slot_key, sk = await _resolve_agent_caller(request, OP_PROPOSE_SELF, own_goal=True)
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    who = _caller_member(request, slot_key)
    if who is None:
        _audit(sk, OP_PROPOSE_SELF, "denied", "not a crewmate thread")
        return _deny(403, "not_a_crewmate", "only a crewmate, in its own chat, can ask this")
    member, _store = who
    name = body.get("request")
    if not isinstance(name, str) or name not in SELF_PROPOSALS:
        _audit(sk, OP_PROPOSE_SELF, "denied", "unknown_request")
        return _deny(
            400, "unknown_request", "'request' must be one of " + ", ".join(SELF_PROPOSALS)
        )
    raw = body.get("params")
    if not isinstance(raw, dict):
        _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
        return _deny(400, "invalid_params", "'params' must be an object")
    if request.get(REQ_OWN_GOAL_TURN) is True and name not in OWN_GOAL_PROPOSALS:
        _audit(sk, OP_PROPOSE_SELF, "denied", "not a user turn")
        return _deny(
            403, "not_user_turn", "only a message the user sent in the dashboard can start this"
        )
    kind = SELF_PROPOSALS[name]
    params: dict[str, Any]
    if name == "capabilities":
        params = {"member": member, "draft": raw.get("draft")}
    elif name == "goal":
        if "long_term" not in raw or set(raw) - {"long_term", "goal_id"}:
            _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
            return _deny(400, "invalid_params", "'goal' takes long_term and an optional goal_id")
        params = {"member": member, **raw}
    elif name == "key_results":
        if set(raw) != {"items"}:
            _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
            return _deny(400, "invalid_params", "'key_results' takes only items")
        params = {"member": member, "items": raw.get("items")}
    elif name == "check_ins":
        allowed_ci = {
            "daily",
            "weekly",
            "monthly",
            "heartbeat",
            "weekday",
            "month_day",
            "time",
            "interval",
        }
        if not raw or set(raw) - allowed_ci:
            _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
            return _deny(
                400, "invalid_params", "'check_ins' takes only " + ", ".join(sorted(allowed_ci))
            )
        params = {"member": member, **raw}
    else:
        allowed = {"model", "reasoning_effort"} if name == "model" else {"approval_mode"}
        fields = {k: v for k, v in raw.items() if k in allowed}
        if not fields or set(raw) - allowed:
            _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
            return _deny(
                400, "invalid_params", f"'{name}' takes only " + ", ".join(sorted(allowed))
            )
        params = {"name": member, "fields": fields}
    return await propose_for_caller(
        request, slot_key, sk, kind=kind, params=params, reason=body.get("reason")
    )


async def api_member_self_undo(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/self-changes/{change_id}/undo — the user takes it back.

    Owner-only. The value put back is the gateway's own record of the change
    (:func:`member_self_changes.take_for_undo`), scoped to the member the route
    names; the row in the chat is then marked ``undone``. A record the gateway
    does not hold (after a restart, or past the newest :data:`MAX_CHANGES`)
    answers 410 ``undo_unavailable``: the crew editor is where any value can be
    set. A field edited again since the change answers 409 ``stale_undo`` and
    nothing is written: an old row's button never overwrites a newer edit.
    """
    from kiro_crew.memory_stores import (
        MemberAlreadyExists,
        MemberApprovalConflict,
        UnknownMemoryStore,
    )

    owner_denied = await require_owner_dashboard_request(request, "members.self_change.undo")
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info["slug"]
    change_id = request.match_info["change_id"]
    try:
        members_mod.validate_slug(slug)
    except members_mod.MemberSlugError:
        return _deny(400, "invalid_member_slug", "invalid member slug")
    # The conversation the user pressed Undo in, from the page's own state: the
    # record must have been made there (a row is transcript data the agent can
    # copy between chats; see ``take_for_undo``).
    try:
        body = await request.json()
    except Exception:
        body = None
    slot_key = body.get("slot") if isinstance(body, dict) else None
    if not isinstance(slot_key, str) or not slot_key:
        return _deny(400, "slot_required", "the conversation the row is in must be named")
    try:
        rec = member_self_changes.take_for_undo(change_id, slug=slug, slot_key=slot_key)
    except member_self_changes.SelfChangeUnavailable as exc:
        status = 409 if exc.code == "already_undone" else 410
        return _deny(status, exc.code, str(exc))
    try:
        result = await asyncio.to_thread(
            members_mod.undo_member_self,
            rec["member"],
            rec["before"],
            after=rec["after"],
            expected_store=rec["memory_store"],
        )
    except members_mod.SelfUpdateError as exc:
        status = 403 if exc.code == "not_a_crewmate" else 409
        return _deny(status, exc.code, str(exc))
    except (UnknownMemoryStore, MemberAlreadyExists, MemberApprovalConflict):
        return _deny(409, "member_not_writable", "this crewmate's settings cannot be changed here")
    undone = member_self_changes.mark_undone(change_id) or {**rec, "status": "undone"}
    state = request.app["state"]
    await chat_cards.load_conversation_window(state, undone)
    chat_cards.record_self_change_status(state, undone)
    return web.json_response(
        {"ok": True, "member": rec["member"], "changed": result["changed"], "status": "undone"}
    )

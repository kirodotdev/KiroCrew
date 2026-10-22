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
from typing import Any, Awaitable, Callable

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
    # The crewmate's one offer to write its own Dashboard page
    # (``offer_dashboard_page``): the yes wakes it to write the page.
    "dashboard_page": "crewmate.dashboard_page",
}

#: The requests a crewmate may make in its own thread from a turn the user did
#: not send (``handlers.guide.REQ_OWN_GOAL_TURN``): its own goal, milestones
#: and check-ins, each still a card the user decides. A capabilities card that
#: only adds servers this gateway already runs is admitted too
#: (:func:`_gateway_servers_only`).
OWN_GOAL_PROPOSALS = frozenset({"goal", "key_results", "check_ins", "dashboard_page"})


def _gateway_servers_only(draft: Any) -> bool:
    """Whether a capabilities *draft* only adds servers this gateway already runs.

    The one capabilities card a crewmate may raise from a turn the user did not
    send (a worker report naming a missing capability, a check-in): it brings in
    no outside code, since each addition copies an entry the gateway already
    runs, and it still changes nothing until the user presses it. A registry
    install, a skill, or a raw operations draft stays user-turn only.
    """
    if not isinstance(draft, dict) or set(draft) != {"additions"}:
        return False
    additions = draft.get("additions")
    return (
        isinstance(additions, list)
        and bool(additions)
        and all(
            isinstance(a, dict)
            and a.get("source") == "gateway"
            and isinstance(a.get("server"), str)
            and set(a) <= {"server", "source", "auto_approve"}
            for a in additions
        )
    )


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
    return _deny(409, "member_not_writable", "this mate's settings cannot be changed from the chat")


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
    from kiro_crew import crewmate_dynamic_capabilities

    # ``request_capabilities`` stays mounted while this crewmate's Dynamic
    # capabilities preview is off; its first call then raises the Preview
    # opt-in card instead (:mod:`kiro_crew.mate_tool_access`), and a call
    # after the user said Not now is refused.
    opt_in = name == "capabilities" and await asyncio.to_thread(
        crewmate_dynamic_capabilities.restricted_for, member
    )
    if opt_in:
        refused = await _opt_in_refusal(request, sk, member)
        if refused is not None:
            return refused
    if (
        request.get(REQ_OWN_GOAL_TURN) is True
        and name not in OWN_GOAL_PROPOSALS
        and not (name == "capabilities" and _gateway_servers_only(raw.get("draft")))
    ):
        _audit(sk, OP_PROPOSE_SELF, "denied", "not a user turn")
        return _deny(
            403, "not_user_turn", "only a message the user sent in the dashboard can start this"
        )
    kind = SELF_PROPOSALS[name]
    params: dict[str, Any]
    if name == "capabilities":
        from kiro_crew.mate_capability_catalog import AdditionError

        try:
            # Resolved even for the opt-in, so a request the card could not
            # carry is refused now rather than after the user turns it on.
            params = await capability_params(request.app, member, raw)
        except AdditionError as exc:
            _audit(sk, OP_PROPOSE_SELF, "denied", exc.code)
            return _deny(409 if exc.code == "no_crewmate_file" else 400, exc.code, exc.message)
        if opt_in:
            return await _raise_opt_in(request, slot_key, sk, member, raw, body.get("reason"))
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
    elif name == "dashboard_page":
        if raw:
            _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
            return _deny(400, "invalid_params", "'dashboard_page' takes no params")
        params = {"member": member}
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
    extra = {"claim": _page_offer_claim(member)} if name == "dashboard_page" else {}
    return await propose_for_caller(
        request, slot_key, sk, kind=kind, params=params, reason=body.get("reason"), **extra
    )


async def capability_params(app: Any, member: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The ``crewmate.capabilities`` params for *member*'s ``request_capabilities`` *raw* params.

    Named additions (find_capabilities' catalog) are resolved by the gateway into
    operations with where each comes from; the caller never supplies a transport
    or an origin for one. Raises :class:`~kiro_crew.mate_capability_catalog.AdditionError`.
    """
    from kiro_crew.dashboard.handlers.member_capability_catalog import additions_to_draft

    draft = raw.get("draft")
    if isinstance(draft, dict) and "additions" in draft:
        resolved, origins = await additions_to_draft(app, member, draft)
        return {"member": member, "draft": resolved, "origins": origins}
    return {"member": member, "draft": draft}


#: What the crewmate is told when its request meets a Not now (or an expired opt-in).
_OPT_IN_DECLINED = (
    "the user chose not to turn on Dynamic capabilities (a Preview) for you, so "
    "nothing was raised. Tell the user in one plain line what your worker needs and "
    "that they can add it in your advanced settings, or turn the Preview on there."
)


_OPT_IN_PENDING = (
    "the Preview opt-in card is still waiting for the user; once they turn it "
    "on, your first request becomes its card on its own"
)


async def _opt_in_refusal(request: web.Request, sk: str, member: str) -> web.Response | None:
    """Why *member*'s request with its preview off raises no opt-in card, or ``None`` to raise it.

    Asked once per crewmate (:mod:`kiro_crew.mate_tool_access`): an opt-in card
    still waiting is not raised twice, and one declined, expired, failed or
    applied-then-switched-off is never raised again. The card's outcome is
    recorded here, from the card store, the first time the crewmate asks after it
    finished.
    """
    from kiro_crew import mate_tool_access

    found = await asyncio.to_thread(mate_tool_access.entry, member)
    if found is None:
        return None
    if mate_tool_access.awaiting_card(found):
        status: str | None = None
    else:
        status = await _card_status(request, str(found.get("card_id") or ""))
    if status == _CARD_GONE:
        # The store does not hold the card (pruned once it finished): nothing
        # is waiting for the user, so it counts as expired rather than pending
        # for ever. An unreadable store (``None``) leaves the record as it is.
        status = "expired"
    found = await asyncio.to_thread(mate_tool_access.settle, member, status) or found
    if found.get("state") == mate_tool_access.STATE_OFFERED:
        _audit(sk, OP_PROPOSE_SELF, "denied", "opt_in_pending")
        return _deny(409, "opt_in_pending", _OPT_IN_PENDING)
    _audit(sk, OP_PROPOSE_SELF, "denied", "dynamic_capabilities_off")
    return _deny(403, "dynamic_capabilities_off", _OPT_IN_DECLINED)


async def _raise_opt_in(
    request: web.Request,
    slot_key: str,
    sk: str,
    member: str,
    raw: dict[str, Any],
    reason: Any,
) -> web.Response:
    """Raise *member*'s Preview opt-in card, holding the request that asked for it.

    The offer and the held request are recorded first: a card the user could
    answer with no record behind it would lose the request and be asked again.
    A record that cannot be written raises no card; a card that is then not
    raised takes the record back.
    """
    import json

    from kiro_crew import mate_tool_access
    from kiro_crew.change_card_catalog import KIND_CREWMATE_UPDATE
    from kiro_crew.dashboard.handlers.change_cards import propose_for_caller

    held = {"params": raw, "reason": reason if isinstance(reason, str) else None}
    try:
        claimed = await asyncio.to_thread(mate_tool_access.reserve_offer, member, held)
    except Exception:  # noqa: BLE001 - no record, so no card
        logger.warning("tool access: offer for %s not recorded", member, exc_info=True)
        _audit(sk, OP_PROPOSE_SELF, "denied", "opt_in_unrecorded")
        return _deny(
            503,
            "opt_in_unrecorded",
            "the Preview opt-in could not be recorded, so no card was raised; tell the "
            "user, and ask again later",
        )
    if not claimed:
        # Another call raised it first: asked once.
        _audit(sk, OP_PROPOSE_SELF, "denied", "opt_in_pending")
        return _deny(409, "opt_in_pending", _OPT_IN_PENDING)
    try:
        response = await propose_for_caller(
            request,
            slot_key,
            sk,
            kind=KIND_CREWMATE_UPDATE,
            params={"name": member, "fields": dict(mate_tool_access.CARD_FIELDS)},
            reason=reason,
        )
    except BaseException:
        await asyncio.to_thread(mate_tool_access.release_offer, member)
        raise
    if response.status != 200:
        await asyncio.to_thread(mate_tool_access.release_offer, member)
        return response
    try:
        card = json.loads(response.text or "{}")
    except (TypeError, ValueError):
        card = {}
    await asyncio.to_thread(mate_tool_access.bind_card, member, str(card.get("id") or ""))
    return web.json_response({**card, "opt_in": True}, status=200)


async def continue_after_opt_in(app: Any, slot_key: str, sk: str, member: str) -> str:
    """Raise the request *member*'s applied opt-in card held; ``""`` or why it was not.

    The user turned the preview on, so the request that raised the card becomes
    its own ``crewmate.capabilities`` card in the same thread, resolved again
    against the gateway as it is now. Taken once (``mate_tool_access.take_held``).
    """
    from kiro_crew import crewmate_dynamic_capabilities, mate_tool_access
    from kiro_crew.change_card_catalog import KIND_CREWMATE_CAPABILITIES
    from kiro_crew.dashboard.handlers.change_cards import propose_for_caller
    from kiro_crew.mate_capability_catalog import AdditionError

    if not await asyncio.to_thread(crewmate_dynamic_capabilities.enabled_for, member):
        # The raise below skips the caller's flag check, so it is made here:
        # the held request waits for *member*'s own Turn on.
        return "dynamic_capabilities_off"
    held = await asyncio.to_thread(mate_tool_access.take_held, member)
    if not held or not isinstance(held.get("params"), dict):
        return "no_request_held"
    try:
        params = await capability_params(app, member, held["params"])
    except AdditionError as exc:
        return exc.code
    response = await propose_for_caller(
        _AppCaller(app),  # type: ignore[arg-type]
        slot_key,
        sk,
        kind=KIND_CREWMATE_CAPABILITIES,
        params=params,
        reason=held.get("reason"),
    )
    if response.status == 200:
        return ""
    try:
        import json

        return str(json.loads(response.text or "{}").get("code") or "refused")
    except (TypeError, ValueError):
        return "refused"


class _AppCaller:
    """What :func:`propose_for_caller` reads off a request: its app, nothing else.

    The held request is raised from the card's apply, not from a crewmate call,
    so the caller it is proposed for is the opt-in card's own thread.
    """

    def __init__(self, app: Any) -> None:
        self.app = app


#: What :func:`_card_status` answers for a card id the store does not hold.
_CARD_GONE = "gone"


async def _card_status(request: web.Request, card_id: str) -> str | None:
    """The status of card *card_id* in this gateway's store.

    :data:`_CARD_GONE` when the store does not hold it (or the record names no
    card), ``None`` when the store cannot be read.
    """
    from kiro_crew.dashboard.change_cards import CardError, card_store_for

    if not card_id:
        return _CARD_GONE
    try:
        store = card_store_for(request.app["state"])
        await store.warm()
        return str(store.get(card_id).get("status") or "") or None
    except CardError as exc:
        if exc.status in (400, 404):
            return _CARD_GONE
        logger.debug("tool access: card %s unreadable", card_id, exc_info=True)
        return None
    except Exception:  # noqa: BLE001 - an unreadable store leaves the record as it is
        logger.debug("tool access: card %s unreadable", card_id, exc_info=True)
        return None


def _page_offer_claim(member: str) -> Callable[[], Awaitable[Callable[[], Awaitable[None]]]]:
    """The asked-once write for *member*'s page offer, run before its card is shown.

    Asked-once counts from here: a yes, a no and an expiry all leave this record, so no
    later wake asks again (``dashboard.page_offer``). A crewmate asked before is
    refused here (``already_asked``), on the raise only, so a re-preview of the card
    that wrote the record still works. A record that cannot be written refuses the
    card, and a card that is then not published takes the record back.
    """
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import page_offer
    from kiro_crew.dashboard.change_cards import CardError

    async def claim() -> Callable[[], Awaitable[None]]:
        try:
            slug = await asyncio.to_thread(members_mod.member_slug, member)
            asked = await asyncio.to_thread(page_offer.was_asked, slug)
        except Exception:
            logger.warning("page offer for %s could not be read", member, exc_info=True)
            raise CardError(
                503, "page_offer_write_failed", "the page offer could not be recorded"
            ) from None
        if asked:
            raise CardError(
                409,
                "already_asked",
                "you already offered your Dashboard page once; it is not asked again",
            )
        try:
            target = await asyncio.to_thread(dashboard_agentic.page_offer_target, slug) or {}
            await asyncio.to_thread(
                page_offer.record_asked,
                slug,
                str(target.get("replaces") or ""),
                str(target.get("template_id") or ""),
            )
        except Exception:
            logger.warning("page offer for %s could not be recorded", member, exc_info=True)
            raise CardError(
                503, "page_offer_write_failed", "the page offer could not be recorded"
            ) from None

        async def undo() -> None:
            await asyncio.to_thread(page_offer.forget_asked, slug)

        return undo

    return claim


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
        return _deny(409, "member_not_writable", "this mate's settings cannot be changed here")
    undone = member_self_changes.mark_undone(change_id) or {**rec, "status": "undone"}
    state = request.app["state"]
    await chat_cards.load_conversation_window(state, undone)
    chat_cards.record_self_change_status(state, undone)
    return web.json_response(
        {"ok": True, "member": rec["member"], "changed": result["changed"], "status": "undone"}
    )

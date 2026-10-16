"""HTTP routes for a crewmate's goals (:mod:`kiro_crew.member_goals`).

Two halves, split the way :mod:`handlers.member_self` is:

* The **owner half** is the Goals tab, cookie-authed and owner-only like every
  other ``/api/members/{slug}/*`` write, with the exact crew name in the request
  (``member``) because slugs are lossy:

  - ``GET /api/members/{slug}/goals?member=`` -- the record.
  - ``PUT /api/members/{slug}/goals/long-term`` -- ``{member, long_term,
    goal_id?, expected?}``: add a goal, or replace (an empty text: remove) the
    goal ``goal_id``. Also the route a ``crewmate.goal`` change card applies and
    undoes through (``change_card_catalog.HOOKED_ROUTES``); ``expected`` makes it
    a compare-and-set so a card never overwrites a newer edit.
  - ``DELETE /api/members/{slug}/goals/items/{goal_id}`` and
    ``POST .../{goal_id}/restore`` -- remove one goal, and its one Undo.
  - ``PUT /api/members/{slug}/goals/key-results`` -- ``{member, items, expected?}``:
    replaces the whole set (the user's edit or removal, and the route a
    ``crewmate.key_results`` card applies and undoes through); ``expected`` is the
    ``{text, target}`` list of the set it replaces. An item may name the goals it
    serves (``goal_ids``).
  - ``DELETE /api/members/{slug}/goals/key-results/{kr_id}`` and
    ``POST .../{kr_id}/restore`` -- remove one key result, and its one Undo.
  - ``PUT /api/members/{slug}/goals/schedules`` -- ``{member, daily?, weekly?,
    monthly?, time?, timezone?, weekday?, month_day?}``: the three check-in
    switches, each keeping exactly one schedule of its own in step
    (:mod:`kiro_crew.dashboard.goal_reminder`).

* The **agent half** is MCP-only under the strict ``/api/member/self`` prefix,
  behind the ``kirocrew-mate`` goal tools. The crewmate acted on is the one whose
  pinned thread the verified caller's slot is, never a name from the body.
  Unlike ``update_self`` these operations are NOT in
  ``handlers.guide._USER_TURN_OPERATIONS``: the Daily Goal Reminder's hidden turn
  is not one the user sent, and taking fresh key-result readings is what that
  turn is for. Every other refusal still applies (a channel, a schedule's own slot, a
  subagent, an app, an unattested session). A new goal is not here at
  all: it goes through ``/api/member/self/propose`` as a card, which only a turn
  the user sent can raise.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

from aiohttp import web

from kiro_crew import member_goals
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

logger = logging.getLogger(__name__)

#: Agent-half operation names, as audited. Deliberately absent from
#: ``handlers.guide._USER_TURN_OPERATIONS`` (see the module docstring).
OP_GOAL_READ = "member.goal_read"
OP_GOAL_WRITE = "member.goal_write"

#: Cap on one saved report's Markdown, as the record module bounds it.
REPORT_MAX_CHARS = member_goals.REPORT_MAX_CHARS


#: One lock per crewmate, held across a goal write AND the check-in schedules it
#: reconciles: two saves interleaving between the record and the cron store would
#: otherwise let the older one remove the schedule the newer one just installed.
#: Weak values, so an idle crewmate's lock goes with its last waiter.
_SCHEDULE_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _schedule_lock(slug: str) -> asyncio.Lock:
    lock = _SCHEDULE_LOCKS.get(slug)
    if lock is None:
        lock = asyncio.Lock()
        _SCHEDULE_LOCKS[slug] = lock
    return lock


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


_GOAL_ERROR_STATUS = {
    "changed_meanwhile": 409,
    "stale_undo": 409,
    "undo_unavailable": 410,
    "slug_collision": 409,
    "no_goal": 409,
    "key_result_not_found": 404,
    "goal_not_found": 404,
    "duplicate_goal": 409,
    "unknown_goal": 400,
    "record_too_large": 413,
}


def _goal_refusal(exc: member_goals.GoalError) -> web.Response:
    return _deny(_GOAL_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc))


def _unreadable() -> web.Response:
    return _deny(500, "goals_unreadable", "this mate's goal record cannot be read")


async def _scheduled_cadences(request: web.Request, slug: str) -> set[str]:
    from kiro_crew.dashboard import goal_reminder

    crons = getattr(request.app.get("state"), "crons", None)
    if crons is None:
        return set()
    jobs = await goal_reminder.reminder_jobs(crons, slug)
    return {c for c in (goal_reminder.cadence_of(j) for j in jobs) if c}


async def _view(request: web.Request, slug: str, rec: dict[str, Any]) -> dict[str, Any]:
    view = member_goals.public_view(rec)
    # Each switch shows what will actually run: a schedule removed from the
    # Schedule page reads as off, not as a promise nothing keeps.
    live = await _scheduled_cadences(request, slug)
    for cadence in member_goals.CADENCES:
        view["schedules"][cadence] = bool(view["schedules"][cadence] and cadence in live)
    return view


# ── owner half ──


async def _owner_member(
    request: web.Request, operation: str, member: Any, *, write: bool
) -> tuple[str, str, KiroCrewConfig] | web.Response:
    """``(slug, member, config)`` for an owner request, or the refusal to send."""
    from kiro_crew.dashboard.handlers.members import _deny_app_caller, _member_names_for_slug

    denied = await _deny_app_caller(request, "members.goals")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, operation)
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except members_mod.MemberSlugError:
        return _deny(400, "invalid_member_slug", "invalid member slug")
    if not members_mod.is_dispatchable_member_name(member):
        return _deny(400, "missing_member", "member is required")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    try:
        if member not in cfg.agents or members_mod.member_slug(member, cfg) != slug:
            raise members_mod.MemberSlugError(str(member))
    except members_mod.MemberSlugError:
        return _deny(404, "member_not_found", "no crew member for this slug")
    if write and _member_names_for_slug(cfg, slug) != [member]:
        return _deny(409, "goals_slug_ambiguous", "several crewmates share this name")
    return slug, str(member), cfg


async def _body(request: web.Request) -> dict[str, Any] | web.Response:
    try:
        body = await request.json()
    except Exception:
        return _deny(400, "invalid_json", "invalid JSON body")
    if not isinstance(body, dict):
        return _deny(400, "invalid_json", "invalid JSON body")
    return body


async def api_member_goals_get(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/goals?member=<name> — the crewmate's goal record."""
    who = await _owner_member(
        request, "members.goals.read", request.query.get("member", ""), write=False
    )
    if isinstance(who, web.Response):
        return who
    slug, member, _cfg = who
    try:
        rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
    except member_goals.GoalsUnreadable:
        return _unreadable()
    return web.json_response({"slug": slug, "member": member, **(await _view(request, slug, rec))})


async def _owner_write(
    request: web.Request,
    operation: str,
    member: Any,
    fn: Any,
    *,
    extra: Any = None,
) -> web.Response:
    who = await _owner_member(request, operation, member, write=True)
    if isinstance(who, web.Response):
        return who
    slug, member, cfg = who
    async with _schedule_lock(slug):
        try:
            rec = await asyncio.to_thread(fn, slug, member)
        except member_goals.GoalError as exc:
            return _goal_refusal(exc)
        except member_goals.GoalsUnreadable:
            return _unreadable()
        except OSError:
            logger.warning("goal write failed for %r", slug, exc_info=True)
            return _deny(500, "goals_write_failed", "could not save the goal")
        if not member_goals.has_goal(rec):
            # Removing the last goal turns every check-in off; keep the schedules
            # in step with the record as stored, under the same lock as a save.
            await _sync_reminder(request, slug, member, cfg)
    more = extra() if callable(extra) else {}
    return web.json_response(
        {"ok": True, "slug": slug, "member": member, **more, **(await _view(request, slug, rec))}
    )


async def api_member_goals_long_term(request: web.Request) -> web.Response:
    """PUT /api/members/{slug}/goals/long-term — ``{member, long_term, goal_id?, expected?}``.

    ``long_term`` is one goal's text. Without ``goal_id`` it is added as a new
    goal (``expected`` may then only be ``""``: "no goal yet"); with one it
    replaces that goal's text, an empty text removing it. The answer names the
    goal written as ``goal_id``.
    """
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    if "long_term" not in body:
        return _deny(400, "missing_long_term", "long_term is required")
    expected = body.get("expected")
    if expected is not None and not isinstance(expected, str):
        return _deny(400, "invalid_expected", "expected must be the goal text shown")
    goal_id = body.get("goal_id")
    if goal_id is not None and not member_goals.is_goal_id(goal_id):
        return _deny(400, "invalid_goal_id", "goal_id must be one of the mate's goal ids")
    if goal_id is None and expected not in (None, ""):
        return _deny(400, "invalid_expected", "a new goal replaces nothing; name a goal_id")

    from kiro_crew.dashboard.handlers.change_cards import REQ_CARD_RAW_RESTORE

    raw = request.get(REQ_CARD_RAW_RESTORE)
    restore_from = _raw_goal_text(raw, goal_id) if isinstance(raw, dict) else None
    saved: list[dict[str, Any]] = []

    def fn(slug: str, member: str) -> dict[str, Any]:
        result = member_goals.set_goal(
            slug,
            member,
            body["long_term"],
            by=member_goals.BY_USER,
            goal_id=goal_id,
            expected=expected,
            restore_from=restore_from,
        )
        saved.append({"slug": slug, "member": member, **result})
        return result["record"]

    resp = await _owner_write(
        request,
        "members.goals.write",
        body.get("member"),
        fn,
        extra=lambda: {"goal_id": saved[0]["goal_id"]} if saved else {},
    )
    # A new goal or a real change to one (not an Undo putting an old one back,
    # not a small edit) sends the crewmate its goal-setup nudge: understand it,
    # propose milestones, offer the check-ins (:mod:`kiro_crew.dashboard.goal_setup`).
    if saved and resp.status == 200 and not isinstance(raw, dict):
        from kiro_crew.dashboard import goal_setup

        s = saved[0]
        if goal_setup.changed_enough(s["before"], s["after"]):
            goal_setup.schedule_nudge(
                request.app.get("state"), s["slug"], s["member"], s["goal_id"]
            )
    return resp


def _raw_goal_text(raw: dict[str, Any], goal_id: Any) -> str | None:
    """A card Undo's raw pre-change text for *goal_id*, from its private snapshot."""
    for g in raw.get("goals") or []:
        if isinstance(g, dict) and g.get("id") == goal_id and isinstance(g.get("text"), str):
            return g["text"]
    return None


async def api_member_goals_goal_delete(request: web.Request) -> web.Response:
    """DELETE /api/members/{slug}/goals/items/{goal_id}?member=<name>.

    The Goals tab's remove: kept for one Undo (:func:`api_member_goals_goal_restore`).
    """
    goal_id = request.match_info["goal_id"]
    removed: list[tuple[str, str]] = []

    def fn(slug: str, member: str) -> dict[str, Any]:
        rec = member_goals.remove_goal(slug, member, goal_id)["record"]
        removed.append((slug, member))
        return rec

    resp = await _owner_write(request, "members.goals.write", request.query.get("member", ""), fn)
    # The crewmate hears about the removal, so it can stop that work and tidy
    # milestones that served only that goal (:mod:`kiro_crew.dashboard.goal_setup`).
    if removed and resp.status == 200:
        from kiro_crew.dashboard import goal_setup

        goal_setup.schedule_removed_nudge(request.app.get("state"), *removed[0], goal_id)
    return resp


async def api_member_goals_goal_restore(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/goals/items/{goal_id}/restore — ``{member}``."""
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    goal_id = request.match_info["goal_id"]

    def fn(slug: str, member: str) -> dict[str, Any]:
        return member_goals.restore_goal(slug, member, goal_id)["record"]

    return await _owner_write(request, "members.goals.write", body.get("member"), fn)


async def api_member_goals_key_results(request: web.Request) -> web.Response:
    """PUT /api/members/{slug}/goals/key-results — ``{member, items, expected?}``."""
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    if "items" not in body:
        return _deny(400, "missing_items", "items is required")
    expected = body.get("expected")
    if expected is not None and not isinstance(expected, list):
        return _deny(400, "invalid_expected", "expected must be the key results shown")

    restore = body.get("restore")
    if restore is not None and not isinstance(restore, list):
        return _deny(400, "invalid_restore", "restore must be the key results to put back")
    expected_ids = body.get("expected_ids")
    if expected_ids is not None and not (
        isinstance(expected_ids, list) and all(isinstance(i, str) for i in expected_ids)
    ):
        return _deny(400, "invalid_expected", "expected_ids must be the key result ids shown")
    expected_links = body.get("expected_links")
    if expected_links is not None and not (
        isinstance(expected_links, dict)
        and all(
            isinstance(v, list) and all(isinstance(g, str) for g in v)
            for v in expected_links.values()
        )
    ):
        return _deny(400, "invalid_expected", "expected_links must map key result ids to goals")

    from kiro_crew.dashboard.handlers.change_cards import REQ_CARD_RAW_RESTORE

    raw = request.get(REQ_CARD_RAW_RESTORE)
    raw_records = raw.get("key_results_records") if isinstance(raw, dict) else None
    if not isinstance(raw_records, list):
        raw_records = None

    # The records this write replaced, read inside the same locked write: a
    # card's Apply keeps them for its Undo (REQ_CARD_PRE_WRITE).
    replaced: list[list[dict[str, Any]]] = []
    written: list[tuple[str, str]] = []

    def fn(slug: str, member: str) -> dict[str, Any]:
        if restore is not None:
            # A key-results card's Undo: what the card removed comes back from
            # the pre-change records; what it kept stays as it is now. An empty
            # list (the card added milestones to none) still goes this way, so
            # the locked id check refuses a set recreated since.
            return member_goals.restore_key_results(
                slug,
                member,
                restore,
                expected=expected,
                expected_ids=expected_ids,
                expected_links=expected_links,
                raw_records=raw_records,
            )["record"]
        result = member_goals.set_key_results(
            slug,
            member,
            body["items"],
            expected=expected,
            expected_ids=expected_ids,
            expected_links=expected_links,
        )
        replaced.append(result["before"])
        written.append((slug, member))
        return result["record"]

    resp = await _owner_write(request, "members.goals.write", body.get("member"), fn)
    if replaced and 200 <= resp.status < 300:
        from kiro_crew.dashboard.handlers.change_cards import (
            REQ_CARD_KEY_RESULTS_APPLY,
            REQ_CARD_PRE_WRITE,
        )

        request[REQ_CARD_PRE_WRITE] = replaced[0]
        # The user confirmed the crewmate's milestones on its card: always tell
        # the crewmate, so it carries on (offers the check-ins, or says what it
        # does next) instead of waiting for the user's next message.
        if request.get(REQ_CARD_KEY_RESULTS_APPLY) and written:
            from kiro_crew.dashboard import goal_setup

            goal_setup.schedule_milestones_nudge(request.app.get("state"), *written[0])
    return resp


async def api_member_goals_key_result_delete(request: web.Request) -> web.Response:
    """DELETE /api/members/{slug}/goals/key-results/{kr_id}?member=<name>.

    The Goals tab's remove: kept for one Undo (:func:`api_member_goals_key_result_restore`).
    """
    kr_id = request.match_info["kr_id"]

    def fn(slug: str, member: str) -> dict[str, Any]:
        return member_goals.remove_key_result(slug, member, kr_id)["record"]

    return await _owner_write(request, "members.goals.write", request.query.get("member", ""), fn)


async def api_member_goals_key_result_restore(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/goals/key-results/{kr_id}/restore — ``{member}``."""
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    kr_id = request.match_info["kr_id"]

    def fn(slug: str, member: str) -> dict[str, Any]:
        return member_goals.restore_key_result(slug, member, kr_id)["record"]

    return await _owner_write(request, "members.goals.write", body.get("member"), fn)


def _label(cfg: KiroCrewConfig, member: str) -> str:
    entry = cfg.agents.get(member)
    name = getattr(entry, "display_name", "") if entry is not None else ""
    return name.strip() if isinstance(name, str) and name.strip() else member


async def _sync_reminder(request: web.Request, slug: str, member: str, cfg: KiroCrewConfig) -> None:
    """Make the check-in schedules match the record as it is stored NOW.

    Called with :func:`_schedule_lock` held, and reads the record again rather
    than trusting the caller's copy, so the schedules always end up as the
    newest write left them.
    """
    from kiro_crew.dashboard import goal_reminder

    crons = getattr(request.app.get("state"), "crons", None)
    if crons is None:
        return
    rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
    await goal_reminder.sync_schedule(crons, slug, _label(cfg, member), rec["schedules"])


async def api_member_goals_schedules(request: web.Request) -> web.Response:
    """PUT /api/members/{slug}/goals/schedules — the check-in switches and their timing.

    Body: ``{member, daily?, weekly?, monthly?, time?, timezone?, weekday?,
    month_day?}``. Turning a switch on needs a goal (409 ``no_goal``).
    The record is written first, then the schedules are made to match it; when
    they cannot be written the switches turned on by this request are turned back
    off and the answer is 503, so no switch shows a check-in nothing will send.
    """
    from kiro_crew.cron_service.schedule import is_valid_timezone

    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    changes = {k: v for k, v in body.items() if k not in ("member", "expected")}
    expected = body.get("expected")
    tz = changes.get("timezone")
    if tz is not None and (not isinstance(tz, str) or (tz and not is_valid_timezone(tz))):
        return _deny(400, "invalid_timezone", "unknown timezone")
    who = await _owner_member(request, "members.goals.schedules", body.get("member"), write=True)
    if isinstance(who, web.Response):
        return who
    slug, member, cfg = who
    async with _schedule_lock(slug):
        try:
            rec = await asyncio.to_thread(
                member_goals.set_schedules, slug, member, changes, expected=expected
            )
        except member_goals.GoalError as exc:
            return _goal_refusal(exc)
        except member_goals.GoalsUnreadable:
            return _unreadable()
        try:
            await _sync_reminder(request, slug, member, cfg)
        except Exception:
            logger.warning("goal check-in schedule failed for %r", slug, exc_info=True)
            turned_on = {c: False for c in member_goals.CADENCES if changes.get(c) is True}
            if turned_on:
                try:
                    await asyncio.to_thread(
                        member_goals.set_schedules,
                        slug,
                        member,
                        turned_on,
                        expected=rec["schedules"],
                    )
                except member_goals.GoalError as exc:
                    if exc.code != "changed_meanwhile":
                        raise
                    # A later schedule write owns these switches now.
            return _deny(503, "schedule_failed", "the check-in could not be scheduled")
    return web.json_response(
        {"ok": True, "slug": slug, "member": member, **(await _view(request, slug, rec))}
    )


# ── agent half ──


async def _agent_member(
    request: web.Request, operation: str
) -> tuple[str, str, str] | web.Response:
    """``(session_key, slug, member)`` of the calling crewmate, or the refusal."""
    from kiro_crew.dashboard.handlers.guide import _audit, _refusal, _resolve_agent_caller
    from kiro_crew.dashboard.handlers.member_self import _caller_member

    try:
        slot_key, sk = await _resolve_agent_caller(request, operation)
    except GuideError as exc:
        return _refusal(exc)
    who = _caller_member(request, slot_key)
    if who is None:
        _audit(sk, operation, "denied", "not a crewmate thread")
        return _deny(403, "not_a_crewmate", "only a crewmate, in its own chat, has goals")
    member = who[0]

    def _resolve() -> str | None:
        slug = members_mod.member_slug(member)
        binding = members_mod.read_dm_binding(slug)
        # The thread this turn runs in must be the one the member's binding
        # names: a member deleted and created again under the same name has a
        # new binding, and this turn's goals are not its.
        if not binding or binding.get("member") != member or binding.get("slot_key") != slot_key:
            return None
        return slug

    try:
        slug = await asyncio.to_thread(_resolve)
    except (members_mod.MemberSlugError, OSError):
        slug = None
    if slug is None:
        _audit(sk, operation, "denied", "thread binding mismatch")
        return _deny(403, "not_a_crewmate", "this chat is not the crewmate's own thread")
    return sk, slug, member


async def _agent_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def api_member_self_goals_read(request: web.Request) -> web.Response:
    """POST /api/member/self/goals/read — the calling crewmate's own goal record."""
    from kiro_crew.dashboard.handlers.guide import _audit

    who = await _agent_member(request, OP_GOAL_READ)
    if isinstance(who, web.Response):
        return who
    sk, slug, member = who
    try:
        rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
    except member_goals.GoalsUnreadable:
        return _unreadable()
    _audit(sk, OP_GOAL_READ, "ok", "")
    return web.json_response({"ok": True, **member_goals.public_view(rec)})


async def api_member_self_key_result(request: web.Request) -> web.Response:
    """POST /api/member/self/goals/key-result — ``{id, current}``: a new reading, no card."""
    from kiro_crew.dashboard.handlers.guide import _audit

    who = await _agent_member(request, OP_GOAL_WRITE)
    if isinstance(who, web.Response):
        return who
    sk, slug, member = who
    body = await _agent_body(request)
    try:
        result = await asyncio.to_thread(
            member_goals.record_key_result, slug, member, body.get("id"), body.get("current")
        )
    except member_goals.GoalError as exc:
        _audit(sk, OP_GOAL_WRITE, "denied", exc.code)
        return _goal_refusal(exc)
    except member_goals.GoalsUnreadable:
        return _unreadable()
    _audit(sk, OP_GOAL_WRITE, "ok", "key_result")
    return web.json_response({"ok": True, "item": result["item"]})


async def _notify_report(request: web.Request, member: str, label: str, saved: dict) -> None:
    """One bell notification linking the saved report. Best effort."""
    from kiro_crew.notifications.bus import NotificationPayload

    state = request.app.get("state")
    bus = getattr(state, "notification_bus", None)
    if bus is None:
        return
    try:
        bus.push(
            NotificationPayload(
                source="system",
                channel="system.agent",
                kind="agent",
                title=f"{label}: {saved['title']}"[:200],
                body=f"New report filed under {saved['folder']}",
                url=saved["url"],
                group_key=f"goal-report:{member}"[:200],
            )
        )
    except Exception:
        logger.warning("goal report notification failed for %r", member, exc_info=True)


async def api_member_self_goal_report(request: web.Request) -> web.Response:
    """POST /api/member/self/goals/report — ``{kind, title, markdown}``.

    Saves a weekly report or monthly review as an artifact under
    ``Crewmates/<name>/Reports``, remembers it on the goal record, and sends one
    notification linking it. The answer carries the link for the crewmate's one
    line in the thread; the report's text never goes into the chat.
    """
    from kiro_crew.dashboard import goal_reminder
    from kiro_crew.dashboard.handlers.guide import _audit

    who = await _agent_member(request, OP_GOAL_WRITE)
    if isinstance(who, web.Response):
        return who
    sk, slug, member = who
    body = await _agent_body(request)
    kind, title, markdown = str(body.get("kind") or ""), body.get("title"), body.get("markdown")
    title_max = member_goals.REPORT_TITLE_MAX_CHARS
    if not isinstance(title, str) or not title.strip() or len(title) > title_max:
        return _deny(
            400, "invalid_title", f"the report needs a title of up to {title_max} characters"
        )
    if not isinstance(markdown, str) or len(markdown) > REPORT_MAX_CHARS:
        return _deny(400, "invalid_report", f"the report is text of up to {REPORT_MAX_CHARS} chars")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    label = _label(cfg, member)
    try:
        saved = await asyncio.to_thread(
            goal_reminder.save_report,
            slug,
            member,
            label,
            kind=kind,
            title=title.strip(),
            markdown=markdown,
            session_key=sk,
        )
    except member_goals.GoalError as exc:
        _audit(sk, OP_GOAL_WRITE, "denied", exc.code)
        return _goal_refusal(exc)
    except Exception:
        logger.warning("goal report save failed for %r", slug, exc_info=True)
        return _deny(500, "report_save_failed", "the report could not be saved")
    await _notify_report(request, member, label, saved)
    _audit(sk, OP_GOAL_WRITE, "ok", f"report:{kind}")
    return web.json_response({"ok": True, **saved})

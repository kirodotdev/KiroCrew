"""Change cards and guide offers as rows of the conversation.

A card or a guide offer is part of the conversation at the point the agent proposed
it, so it is recorded there: the propose route appends ONE ``card`` row to the
proposing slot's transcript and writes ``card/proposed`` (or ``guide/offered``)
into that session's crew log. The dashboard draws the live card at that row.

Ownership is split on purpose:

* The card and guide STORES own live state. Applying, undoing, editing and every
  guarantee that goes with them (owner-only, a human click, the plan hook) are
  theirs and are not touched here. The dashboard reads that state through its
  existing reads and frames, so an inline card updates in place.
* The ROW owns the conversation's record. Its ``meta.card`` carries the card's
  id, kind, title and LAST status, and nothing else -- never a parameter, never a
  typed value. When a card or guide reaches a finished status the row is patched
  in place and ``card/finished`` / ``guide/finished`` is written, so a reload long
  after the store pruned the record still shows where the card was and how it
  ended.

The row is display-only (``history_projection.DISPLAY_ONLY_ROLES``): no
model-bound reader carries it. The transcript is agent-writable, so nothing here
READS authority back from a row -- the crew-log session id a status is written
under is held in process, never taken from row meta, and a status is written only
when the store itself broadcast it.
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

ROLE_CARD = "card"
SURFACE_CHANGE = "change"
SURFACE_GUIDE = "guide"
#: A crewmate's own applied change (``update_self``), with its undo.
SURFACE_SELF = "self"
#: A lesson the agent just saved (``learn_add``), with a link to review it.
SURFACE_LESSON = "lesson"

#: The status a proposal is born with, per surface.
_PROPOSED = {SURFACE_CHANGE: "pending", SURFACE_GUIDE: "offered"}
#: Guide end reasons a conversation row keeps (the result line words them).
_GUIDE_RECORDED_REASONS = frozenset({"saved_without_guide"})

#: Statuses after which a row records an outcome. Mirrors the stores' own sets.
_CHANGE_FINISHED = frozenset({"applied", "partial", "failed", "cancelled", "expired", "undone"})
_GUIDE_FINISHED = frozenset({"completed", "cancelled", "expired"})
#: Live statuses a row may hold; ``pending`` after ``failed`` is a retry.
_CHANGE_LIVE = frozenset({"pending", "applying"})
_GUIDE_LIVE = frozenset({"offered", "active", "target_missing"})

_TITLE_MAX = 300
_SUMMARY_MAX = 300

#: Which crew-log unit each proposal was written into, keyed ``(surface, id)``.
#: Held in process ONLY: a row's meta lives in an agent-writable file, and a
#: session id read back from it would aim a gateway-authored entry at another
#: conversation's log. After a restart the slot's live session is used instead.
_SIDS: OrderedDict[tuple[str, str], str] = OrderedDict()
_SIDS_MAX = 1024

#: The status this process last logged per ``(surface, id)``, and whether a guide
#: has been started. Bounded like ``_SIDS``; what it forgets falls back to the row.
_LOGGED: OrderedDict[tuple[str, str], tuple[str, bool]] = OrderedDict()


def _note_logged(key: tuple[str, str], status: str, started: bool) -> None:
    _LOGGED[key] = (status, started)
    _LOGGED.move_to_end(key)
    while len(_LOGGED) > _SIDS_MAX:
        _LOGGED.popitem(last=False)


def reset() -> None:
    """Forget the in-process maps (tests)."""
    _SIDS.clear()
    _LOGGED.clear()


def _remember_sid(surface: str, item_id: str, sid: str) -> None:
    if not sid:
        return
    _SIDS[(surface, item_id)] = sid
    _SIDS.move_to_end((surface, item_id))
    while len(_SIDS) > _SIDS_MAX:
        _SIDS.popitem(last=False)


def _slot_sid(slot: Any) -> str:
    from kiro_crew.crew_log import emit as crew_log_emit

    return crew_log_emit.session_id_of(getattr(slot, "_acp_client", None))


def _sid_for(surface: str, item_id: str, slot: Any) -> str:
    return _SIDS.get((surface, item_id)) or (_slot_sid(slot) if slot is not None else "")


def _clean(text: Any, limit: int) -> str:
    """Display text for a row: redacted, single-line, bounded."""
    if not isinstance(text, str) or not text:
        return ""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    try:
        out, _ = redact_exfiltration_urls(text)
        out, _ = redact_credentials(out)
    except Exception:
        return ""
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 1] + "\u2026"


def _get_slot(state: Any, slot_key: str) -> Any:
    getter = getattr(state, "get_slot", None)
    if getter is None or not slot_key:
        return None
    try:
        return getter(slot_key)
    except Exception:
        return None


async def load_conversation_window(state: Any, public: dict[str, Any]) -> None:
    """Load the window of a card's or guide's conversation, with the read off the loop.

    Every ``record_*`` below reads that conversation's rows, so the async route
    that calls one awaits this first: a tab restored as a sidebar row loads on a
    worker thread instead of inline on the loop. The record resolves the slot
    again afterwards, so a tab closed or replaced during the await is never the
    one written to. Never raises.
    """
    from kiro_crew.dashboard.state import load_window_off_loop

    slot = _get_slot(state, str(public.get("slot_key") or ""))
    if slot is None:
        return
    try:
        await load_window_off_loop(slot)
    except Exception:
        # The record's own read retries the load and fails closed.
        logger.warning("could not load a card's conversation off the loop", exc_info=True)


def find_row(slot: Any, surface: str, item_id: str) -> dict[str, Any] | None:
    """The slot's ``card`` row for *item_id*, newest first, or ``None``."""
    for msg in reversed(getattr(slot, "messages", None) or []):
        if msg.get("role") != ROLE_CARD:
            continue
        card = (msg.get("meta") or {}).get("card")
        if isinstance(card, dict) and card.get("surface") == surface and card.get("id") == item_id:
            return msg
    return None


def _row_meta(surface: str, item_id: str, slot_key: str, **fields: Any) -> dict[str, Any]:
    card: dict[str, Any] = {"surface": surface, "id": item_id, "slot": slot_key}
    card.update({k: v for k, v in fields.items() if v not in (None, "", [])})
    return {"card": card}


def _append_row(slot: Any, content: str, meta: dict[str, Any]) -> str:
    try:
        row = slot.append(ROLE_CARD, content, "msg msg-card", meta=meta)
    except Exception:
        logger.warning("could not record a card row in the conversation", exc_info=True)
        return ""
    mid = (row.get("meta") or {}).get("mid") if isinstance(row, dict) else ""
    return mid if isinstance(mid, str) else ""


def _patch_row(state: Any, slot: Any, row: dict[str, Any], updates: dict[str, Any]) -> None:
    meta = dict(row.get("meta") or {})
    card = dict(meta.get("card") or {})
    card.update({k: v for k, v in updates.items() if v is not None})
    meta["card"] = card
    mid = meta.get("mid") if isinstance(meta.get("mid"), str) else None
    try:
        updated = slot.update_message(row.get("ts", ""), meta=meta, mid=mid)
    except Exception:
        logger.debug("card row update failed", exc_info=True)
        return
    if updated is None:
        return
    broadcast = getattr(state, "broadcast_ws", None)
    if broadcast is None:
        return
    try:
        broadcast(
            "chat_message_update",
            {"slot": slot.key, "ts": row.get("ts", ""), "mid": mid or "", "meta": meta},
        )
    except Exception:
        logger.debug("card row broadcast failed", exc_info=True)


# ── change cards ──


def record_change_proposed(state: Any, card: dict[str, Any]) -> None:
    """Put a just-proposed change card into its conversation. Never raises."""
    try:
        _record_proposed(
            state,
            SURFACE_CHANGE,
            card,
            item_id=str(card.get("id") or ""),
            title=_clean(card.get("title"), _TITLE_MAX),
            fields={"kind": card.get("kind"), "risk": card.get("risk")},
        )
    except Exception:
        logger.warning("could not record a change card in its conversation", exc_info=True)


def record_change_status(state: Any, card: dict[str, Any]) -> None:
    """Fold a card the store just published into its row. Never raises."""
    try:
        status = card.get("status")
        if status in _CHANGE_FINISHED:
            summary = _clean((card.get("result") or {}).get("summary"), _SUMMARY_MAX)
            _record_status(state, SURFACE_CHANGE, card, str(card.get("id") or ""), status, summary)
        elif status in _CHANGE_LIVE:
            _record_status(state, SURFACE_CHANGE, card, str(card.get("id") or ""), status, None)
    except Exception:
        logger.warning("could not record a change card outcome", exc_info=True)


# ── guides ──


def _guide_actions(guide: dict[str, Any]) -> list[str]:
    out = []
    for action in guide.get("actions") or []:
        if isinstance(action, dict) and isinstance(action.get("id"), str):
            out.append(action["id"][:64])
    return out[:16]


def record_guide_offered(state: Any, guide: dict[str, Any]) -> None:
    """Put a just-offered guide into its conversation. Never raises."""
    try:
        actions = _guide_actions(guide)
        _record_proposed(
            state,
            SURFACE_GUIDE,
            guide,
            item_id=str(guide.get("guide_id") or ""),
            title="",
            fields={"kind": actions[0] if actions else "", "actions": actions},
        )
    except Exception:
        logger.warning("could not record a guide offer in its conversation", exc_info=True)


def record_guide_status(state: Any, guide: dict[str, Any]) -> None:
    """Fold a guide the store just published into its row. Never raises."""
    try:
        status = guide.get("status")
        if status in _GUIDE_FINISHED or status in _GUIDE_LIVE:
            # The end's reason rides on the row only when it changes what the
            # result line says, so a reload past the store's window still reads
            # "saved" rather than a plain "cancelled".
            reason = guide.get("reason")
            _record_status(
                state,
                SURFACE_GUIDE,
                guide,
                str(guide.get("guide_id") or ""),
                status,
                None,
                reason=reason if reason in _GUIDE_RECORDED_REASONS else None,
            )
    except Exception:
        logger.warning("could not record a guide outcome", exc_info=True)


# ── shared ──


def _record_proposed(
    state: Any,
    surface: str,
    public: dict[str, Any],
    *,
    item_id: str,
    title: str,
    fields: dict[str, Any],
) -> None:
    from kiro_crew.crew_log import emit as crew_log_emit

    slot_key = str(public.get("slot_key") or "")
    slot = _get_slot(state, slot_key)
    if not item_id or slot is None or find_row(slot, surface, item_id) is not None:
        return
    status = _PROPOSED[surface]
    meta = _row_meta(surface, item_id, slot_key, title=title, status=status, **fields)
    mid = _append_row(slot, title or str(fields.get("kind") or "") or item_id, meta)
    sid = _slot_sid(slot)
    _remember_sid(surface, item_id, sid)
    _note_logged((surface, item_id), status, False)
    if surface == SURFACE_CHANGE:
        crew_log_emit.on_card_proposed(
            sid,
            slot=slot_key,
            card_id=item_id,
            kind=str(fields.get("kind") or ""),
            title=title,
            risk=str(fields.get("risk") or ""),
            revision=revision if isinstance(revision := public.get("revision"), int) else 1,
            mid=mid,
        )
    else:
        crew_log_emit.on_guide_offered(
            sid, slot=slot_key, guide_id=item_id, actions=fields.get("actions") or [], mid=mid
        )


def _record_status(
    state: Any,
    surface: str,
    public: dict[str, Any],
    item_id: str,
    status: str,
    summary: str | None,
    *,
    reason: str | None = None,
) -> None:
    from kiro_crew.crew_log import emit as crew_log_emit

    slot = _get_slot(state, str(public.get("slot_key") or ""))
    row = find_row(slot, surface, item_id) if slot is not None and item_id else None
    row_status = (((row or {}).get("meta") or {}).get("card") or {}).get("status")
    if row is not None and row_status != status:
        _patch_row(state, slot, row, {"status": status, "summary": summary, "reason": reason})
    # The log records what the STORE reported, row or no row: a conversation whose
    # live window does not hold the row still has a log the outcome belongs in.
    # Deduplicated against what this process last logged (or, after a restart, what
    # the row last recorded), so a re-broadcast of a finished card -- a dismiss, a
    # re-read -- is not a second outcome.
    key = (surface, item_id)
    logged = _LOGGED.get(key)
    last = logged[0] if logged else row_status
    started = logged[1] if logged else row_status not in (None, _PROPOSED[surface])
    if last == status:
        return
    _note_logged(key, status, started or status == "active")
    sid = _sid_for(surface, item_id, slot)
    if surface == SURFACE_CHANGE:
        if status in _CHANGE_FINISHED:
            revision = public.get("revision")
            crew_log_emit.on_card_finished(
                sid,
                card_id=item_id,
                status=status,
                revision=revision if isinstance(revision, int) else 1,
            )
    elif status in _GUIDE_FINISHED:
        crew_log_emit.on_guide_finished(sid, guide_id=item_id, status=status, reason=reason)
    elif status == "active" and not started:
        # Once per guide: a tab re-claiming after its lease lapsed is not a start.
        crew_log_emit.on_guide_started(sid, guide_id=item_id)


# ── a crewmate's own changes ──

#: The fields a ``self`` row draws, per changed field: the stored value before
#: and after. Drawing only -- the gateway's own record
#: (:mod:`kiro_crew.dashboard.member_self_changes`) is what an undo reads.
_SELF_FIELDS = ("display_name", "description", "avatar")


def record_self_change(state: Any, change: dict[str, Any]) -> None:
    """Put a crewmate's just-applied self-change into its conversation. Never raises.

    *change* is the record :func:`member_self_changes.record` returned. The row
    is drawn where the crewmate's tool call landed, like a card: its
    ``meta.card`` carries ``surface: "self"``, the change id, the fields that
    changed and their before/after values for the faces and labels the row
    shows, and the status (``applied``, then ``undone``). No crew-log entry: the
    member's own event log already recorded the change (``member/config``).
    """
    try:
        slot_key = str(change.get("slot_key") or "")
        slot = _get_slot(state, slot_key)
        item_id = str(change.get("id") or "")
        if not item_id or slot is None or find_row(slot, SURFACE_SELF, item_id) is not None:
            return
        changed = [f for f in _SELF_FIELDS if f in (change.get("changed") or [])]
        if not changed:
            return
        # The same egress chain every member projection runs before the browser
        # (``eventlog.service._redact_projection_value``): a credential- or
        # URL-shaped string planted in a trait or a description is masked in
        # the row's drawing payload. The gateway's own undo record keeps the
        # value as stored; only what the browser draws is masked.
        from kiro_crew.eventlog.service import _redact_projection_value

        before = _redact_projection_value(change.get("before") or {})
        after = _redact_projection_value(change.get("after") or {})
        assert isinstance(before, dict) and isinstance(after, dict)
        meta = _row_meta(
            SURFACE_SELF,
            item_id,
            slot_key,
            member=str(change.get("member") or ""),
            name=str(_redact_projection_value(change.get("name") or "")),
            slug=str(change.get("slug") or ""),
            kind=changed[0] if len(changed) == 1 else "profile",
            fields=changed,
            before={k: before.get(k) for k in changed},
            after={k: after.get(k) for k in changed},
            status=str(change.get("status") or "applied"),
            undoable=bool(change.get("undoable", True)),
        )
        _append_row(slot, f"{change.get('member') or ''} changed its {changed[0]}", meta)
    except Exception:
        logger.warning(
            "could not record a crewmate's self-change in its conversation", exc_info=True
        )


def record_self_change_status(state: Any, change: dict[str, Any]) -> None:
    """Fold an undone (or re-read) self-change into its row. Never raises."""
    try:
        slot = _get_slot(state, str(change.get("slot_key") or ""))
        item_id = str(change.get("id") or "")
        row = find_row(slot, SURFACE_SELF, item_id) if slot is not None and item_id else None
        if row is None:
            return
        status = str(change.get("status") or "")
        row_status = ((row.get("meta") or {}).get("card") or {}).get("status")
        if status and row_status != status:
            _patch_row(state, slot, row, {"status": status})
    except Exception:
        logger.warning("could not record a crewmate's self-change outcome", exc_info=True)


# ── lessons ──

_LESSON_RULE_MAX = 300


def record_lesson_saved(state: Any, slot_key: str, rule: Any, outcome: Any) -> None:
    """Put one just-saved lesson into the conversation that saved it. Never raises.

    A lesson is re-injected into every later session, so a write the agent made
    is told to the person where it happened: one ``card`` row per write that
    changed the store (``inserted`` or ``enriched``), never one per session or per
    rule, so a second write of the same rule is its own row like any other. The
    row is display-only and carries the redacted rule text and the outcome, which
    the dashboard words as "saved" or "updated". It authorizes nothing, so there
    is nothing to read back from it.
    """
    try:
        # The store's own "this write changed me" set. ``LessonWriteOutcome`` is a
        # ``str`` enum, so the JSONL tier's plain outcome strings match it too.
        from kiro_crew.vector_memory import _LESSON_WROTE_OUTCOMES

        status = str(outcome or "")
        if status not in _LESSON_WROTE_OUTCOMES:
            return
        slot = _get_slot(state, slot_key)
        text = _clean(rule, _LESSON_RULE_MAX)
        if slot is None or not text:
            return
        item_id = uuid.uuid4().hex[:16]
        meta = _row_meta(SURFACE_LESSON, item_id, slot_key, rule=text, status=status)
        _append_row(slot, f"Lesson {'updated' if status == 'enriched' else 'saved'}: {text}", meta)
    except Exception:
        logger.warning("could not record a saved lesson in its conversation", exc_info=True)

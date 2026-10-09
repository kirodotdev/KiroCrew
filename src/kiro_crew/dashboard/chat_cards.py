"""Guide offers as rows of the conversation.

A guide offer is part of the conversation at the point the agent offered it, so
it is recorded there: the offer route appends ONE ``card`` row to the offering
slot's transcript and writes ``guide/offered`` into that session's crew log. The
dashboard draws the live offer at that row.

Ownership is split on purpose:

* The guide STORE owns live state. Claiming, walking, cancelling and every
  guarantee that goes with them (owner-only, a human click) are its own and are
  not touched here. The dashboard reads that state through its existing reads
  and frames, so an inline offer updates in place.
* The ROW owns the conversation's record. Its ``meta.card`` carries the offer's
  id, kind, title and LAST status, and nothing else -- never a parameter. When a
  guide reaches a finished status the row is patched in place and
  ``guide/finished`` is written, so a reload long after the store pruned the
  record still shows where the offer was and how it ended.

The row is display-only (``history_projection.DISPLAY_ONLY_ROLES``): no
model-bound reader carries it. The transcript is agent-writable, so nothing here
READS authority back from a row -- the crew-log session id a status is written
under is held in process, never taken from row meta, and a status is written only
when the store itself broadcast it.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

ROLE_CARD = "card"
SURFACE_GUIDE = "guide"

#: The status a proposal is born with, per surface.
_PROPOSED = {SURFACE_GUIDE: "offered"}
#: Guide end reasons a conversation row keeps (the result line words them).
_GUIDE_RECORDED_REASONS = frozenset({"saved_without_guide"})

#: Statuses after which a row records an outcome. Mirrors the stores' own sets.
_GUIDE_FINISHED = frozenset({"completed", "cancelled", "expired"})
#: Live statuses a row may hold.
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
    if status in _GUIDE_FINISHED:
        crew_log_emit.on_guide_finished(sid, guide_id=item_id, status=status, reason=reason)
    elif status == "active" and not started:
        # Once per guide: a tab re-claiming after its lease lapsed is not a start.
        crew_log_emit.on_guide_started(sid, guide_id=item_id)

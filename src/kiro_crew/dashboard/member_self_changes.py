"""Gateway-held record of a crewmate's own changes, so the user can take one back.

A crewmate changing its own name, description or face (``update_self``, the
``kirocrew-mate`` server) writes the record at once; what the user gets is one
row in the crewmate's chat with an **Undo**. The value Undo puts back is held
HERE, in the gateway process, never read from the row: the transcript is
agent-writable, so a ``before`` taken from it could put any value on the record
under the user's click. The row carries the same ``before``/``after`` for
drawing only (:mod:`kiro_crew.dashboard.chat_cards`, surface ``self``).

Bounded and in-process on purpose. An undo is a short-horizon affordance -- the
user saw the change land and does not want it -- so the record lives as long as
this gateway does and the newest :data:`MAX_CHANGES` are kept. Past that, the
crew editor is where the user sets any value they like.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from kiro_crew.members import MEMBER_DESCRIPTION_MAX_CHARS

#: Newest self-changes the gateway remembers for undo.
MAX_CHANGES = 256

#: Longest value one retained field may hold: the description cap, which is the
#: largest of the three fields' own caps (a ghost avatar record is far smaller),
#: and the same constant the write path enforces, so a description the crewmate
#: may save is always one the record may keep. A string is measured as the write
#: path measures it (its length); a mapping by its serialized length. A value that
#: was ALREADY on the record when the crewmate changed it could be larger than
#: the write paths allow today, so the bound is checked on what is retained, not
#: only on what is written: such a change is applied and drawn but keeps no undo
#: record, rather than keeping an unbounded one or a truncated one that would
#: restore the wrong value.
SNAPSHOT_FIELD_MAX_CHARS = MEMBER_DESCRIPTION_MAX_CHARS

STATUS_APPLIED = "applied"
STATUS_UNDONE = "undone"


class SelfChangeUnavailable(LookupError):
    """No undoable record for that id: unknown, another member's, or already undone."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_lock = threading.Lock()
_changes: OrderedDict[str, dict[str, Any]] = OrderedDict()


def reset() -> None:
    """Forget every record (tests)."""
    with _lock:
        _changes.clear()


def record(
    *,
    member: str,
    slug: str,
    slot_key: str,
    memory_store: str,
    changed: list[str],
    before: dict[str, Any],
    after: dict[str, Any],
    name: str = "",
) -> dict[str, Any]:
    """Remember one applied self-change and return its public record.

    *name* is the display name the crewmate goes by once the change is applied
    (its key when it has none): what the row calls it. The record is kept, and
    the change undoable, only while every retained value fits
    :data:`SNAPSHOT_FIELD_MAX_CHARS`; otherwise ``undoable`` is false and the
    record is returned for the row but not stored.
    """
    change_id = secrets.token_urlsafe(12)
    # Every retained string is bounded, the label included: a display name the
    # config loader preserved as written can exceed the write paths' cap.
    undoable = fits(name or member) and all(
        fits(v) for values in (before, after) for v in values.values()
    )
    rec: dict[str, Any] = {
        "id": change_id,
        "member": member,
        "name": name or member,
        "slug": slug,
        "slot_key": slot_key,
        "memory_store": memory_store,
        "changed": list(changed),
        "before": dict(before),
        "after": dict(after),
        "status": STATUS_APPLIED,
        "ts": time.time(),
        "undoable": undoable,
        # An undo the crewmate has not been told about yet (``take_unreported_undos``).
        "undo_reported": False,
    }
    if undoable:
        with _lock:
            _changes[change_id] = rec
            while len(_changes) > MAX_CHANGES:
                _changes.popitem(last=False)
    return dict(rec)


def fits(value: Any) -> bool:
    """Whether one retained field value is within :data:`SNAPSHOT_FIELD_MAX_CHARS`.

    A string is measured by its own length, as the write path measures a
    description, so quotes and line breaks do not count twice; anything else (an
    avatar mapping) by its serialized length.
    """
    if isinstance(value, str):
        return len(value) <= SNAPSHOT_FIELD_MAX_CHARS
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str)) <= SNAPSHOT_FIELD_MAX_CHARS
    except (TypeError, ValueError):
        return False


def get(change_id: str) -> dict[str, Any] | None:
    with _lock:
        rec = _changes.get(change_id)
        return dict(rec) if rec else None


def take_for_undo(change_id: str, *, slug: str, slot_key: str) -> dict[str, Any]:
    """The record to undo, or raise :class:`SelfChangeUnavailable`.

    *slug* must be the member the route was addressed to, and *slot_key* the
    conversation the user pressed Undo in. The row in the chat is transcript
    data, which the agent can write, so a row copied into another crewmate's
    transcript (its id and slug intact, its name re-labelled) must not make the
    button act on the original's record: the conversation the row is pressed in
    is taken from the page, not the row, and has to be the one the change was
    made in.
    """
    with _lock:
        rec = _changes.get(change_id)
        if rec is None:
            raise SelfChangeUnavailable(
                "undo_unavailable", "this change can no longer be undone here"
            )
        if rec["slug"] != slug or rec["slot_key"] != slot_key:
            raise SelfChangeUnavailable(
                "undo_unavailable", "that change was made in another mate's chat"
            )
        if rec["status"] != STATUS_APPLIED:
            raise SelfChangeUnavailable("already_undone", "this change was already undone")
        return dict(rec)


def mark_undone(change_id: str) -> dict[str, Any] | None:
    with _lock:
        rec = _changes.get(change_id)
        if rec is None:
            return None
        rec["status"] = STATUS_UNDONE
        rec["undone_ts"] = time.time()
        return dict(rec)


def take_unreported_undos(slot_key: str) -> list[dict[str, Any]]:
    """The undos in *slot_key*'s thread the crewmate has not heard yet; marks them heard.

    Read at the start of the crewmate's next turn and rendered as reference data
    (:func:`kiro_crew.context_blocks.render_self_change_undos`), the way change
    card outcomes are: an undo is the user's click, it starts no turn, and
    without this the crewmate's next reply would still speak of the face or name
    the user took back.
    """
    with _lock:
        out = []
        for rec in _changes.values():
            if rec["slot_key"] != slot_key or rec["status"] != STATUS_UNDONE:
                continue
            if rec.get("undo_reported"):
                continue
            rec["undo_reported"] = True
            out.append(dict(rec))
        return out

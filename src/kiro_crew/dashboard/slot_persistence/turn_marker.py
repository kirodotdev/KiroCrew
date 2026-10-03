"""The turn-in-flight marker a restart reads back.

``_save_slot_to_history`` writes ``turn_in_flight_generation`` (and a copy of the
turn's opening row, ``turn_in_flight_prompt``) while a local turn sits between
admission and teardown, and clears both otherwise -- by omitting them from a full
save, and as ``0`` / ``None`` in an empty-window merge. This module owns what a restore
makes of a marker that outlived its process: the bounded, field-by-field
validation of the stored copy, and the reconcile that re-appends a lost opener and
lands one interruption row at the tail. Every restore path calls the reconcile
after its window is loaded.

New rules about how a restart reads a turn it did not see finish belong here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_delivery import ATTACHMENT_LIST_MAX_ITEMS, ATTACHMENT_PATH_MAX_LEN
from kiro_crew.dashboard.slot_queue_repository import ALL_ATTACHMENT_META_KEYS
from kiro_crew.dashboard.state import is_stop_event_row, is_turn_interrupted

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot


_RESTART_INTERRUPTION_KIND = "gateway_restart_interruption"
# "app", not "gateway": the sibling relay-interruption row in
# ``_rehydrate_slot_from_history`` names the same event the same way, and the
# process name means nothing to the person reading the transcript.
_RESTART_INTERRUPTION_MSG = (
    "This turn was interrupted when the app restarted. "
    "Review the partial output above, then resume to continue."
)


def _local_turn_generation(meta: Mapping[str, object]) -> int:
    """The persisted local-turn generation, or zero when none is recorded.

    ``bool`` is an ``int`` subclass, so it is refused explicitly: the metadata
    line is an ordinary writable file, and a malformed or hand-edited value
    must never manufacture an interruption row.
    """
    stored = meta.get("turn_in_flight_generation")
    return stored if type(stored) is int and stored > 0 else 0


_LOCAL_TURN_PROMPT_ROLES = frozenset({"user", "nudge", "subagent", "inject"})
#: The row's identity, its attachment lists (every list of
#: ``ALL_ATTACHMENT_META_KEYS`` -- ``files``, ``dirs`` AND ``images``, since the
#: picture a send attached rides ``meta.images`` and nothing else, so a
#: restored opener without it is a row whose later regenerate or edit-resend
#: cannot replay the image) and the inject kind. Mirrored by
#: ``chat_runner._LOCAL_TURN_PROMPT_META_KEYS``.
_LOCAL_TURN_PROMPT_META_KEYS = ("mid", *ALL_ATTACHMENT_META_KEYS, "injectKind")
#: Bounds on the opening-row copy the marker retains. The attachment bounds are
#: the send path's own (``ATTACHMENT_LIST_MAX_ITEMS`` entries of
#: ``ATTACHMENT_PATH_MAX_LEN`` chars, applied to each list), so every row the
#: send path accepts fits the copy and a narrower local bound cannot drop an
#: accepted opener. The total is sized for ``MAX_PROMPT_BYTES`` (100 KB) plus
#: one full list per attachment key plus the identity fields, so it only
#: rejects a line that no writer of this gateway produced. An out-of-bounds
#: copy is dropped whole, never truncated: the restore re-appends it as the
#: user's own transcript row, and a shortened row would misstate what they
#: sent. A dropped copy leaves the generation alone to flag the turn.
_LOCAL_TURN_PROMPT_MAX_ATTACHMENTS = ATTACHMENT_LIST_MAX_ITEMS
_LOCAL_TURN_PROMPT_MAX_FIELD_CHARS = ATTACHMENT_PATH_MAX_LEN
_LOCAL_TURN_PROMPT_MAX_BYTES = 128 * 1024 + len(
    ALL_ATTACHMENT_META_KEYS
) * ATTACHMENT_LIST_MAX_ITEMS * (ATTACHMENT_PATH_MAX_LEN + 4)


def local_turn_prompt_within_bounds(prompt: Mapping[str, object]) -> bool:
    """Whether an opening-row copy fits the bounds the marker retains.

    Checked at retention (``_local_turn_opening_row``) so an oversize copy is
    never written, and again at restore (:func:`_local_turn_prompt`) because
    the metadata line is a writable file. ``content`` is bounded only through
    the serialized total; ``ts``, ``mid``, ``injectKind`` and each
    attachment path are bounded per field, and the attachment lists per count.
    """
    ts = prompt.get("ts")
    if isinstance(ts, str) and len(ts) > _LOCAL_TURN_PROMPT_MAX_FIELD_CHARS:
        return False
    meta = prompt.get("meta")
    if isinstance(meta, Mapping):
        for key in ("mid", "injectKind"):
            value = meta.get(key)
            if isinstance(value, str) and len(value) > _LOCAL_TURN_PROMPT_MAX_FIELD_CHARS:
                return False
        for key in ALL_ATTACHMENT_META_KEYS:
            value = meta.get(key)
            if isinstance(value, list):
                if len(value) > _LOCAL_TURN_PROMPT_MAX_ATTACHMENTS:
                    return False
                if any(
                    isinstance(item, str) and len(item) > _LOCAL_TURN_PROMPT_MAX_FIELD_CHARS
                    for item in value
                ):
                    return False
    try:
        size = len(json.dumps(prompt, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return False
    return size <= _LOCAL_TURN_PROMPT_MAX_BYTES


def _local_turn_prompt(meta: Mapping[str, object]) -> dict | None:
    """The persisted copy of the in-flight turn's opening row, validated.

    Same trust boundary as :func:`_local_turn_generation`: the line is an
    ordinary writable file, so every field is re-checked rather than trusted.
    A copy that is not a dict, names a role that never opens a turn, or has
    no content is dropped. ``meta`` is reduced to the row's identity, its
    attachment lists and the inject kind; a restored row must not arrive
    wearing a flag that changes what a later reader does with it.
    """
    stored = meta.get("turn_in_flight_prompt")
    if not isinstance(stored, dict) or not local_turn_prompt_within_bounds(stored):
        return None
    role = stored.get("role")
    content = stored.get("content")
    if role not in _LOCAL_TURN_PROMPT_ROLES or not isinstance(content, str) or not content:
        return None
    raw_meta = stored.get("meta")
    kept_meta: dict = {}
    if isinstance(raw_meta, dict):
        mid = raw_meta.get("mid")
        if isinstance(mid, str) and mid:
            kept_meta["mid"] = mid
        for key in ALL_ATTACHMENT_META_KEYS:
            value = raw_meta.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                kept_meta[key] = list(value)
        kind = raw_meta.get("injectKind")
        if isinstance(kind, str) and kind:
            kept_meta["injectKind"] = kind
    ts = stored.get("ts")
    # No ``cls`` is read: the transcript never persists one for these roles,
    # and a JSON ``cls`` on the re-appended row would be parsed into ``meta``
    # on emit, over the identity this copy exists to carry.
    return {
        "role": role,
        "content": content,
        "ts": ts if isinstance(ts, str) else "",
        "meta": kept_meta,
    }


def _window_holds_row(
    messages: Iterable[Mapping[str, object]], prompt: Mapping[str, object]
) -> bool:
    """Whether *messages* (a window or the whole on-disk list) holds the marker's opening row.

    Matched by ``mid`` when the copy has one, which is the identity every
    other dual-writer uses; a copy without an id falls back to the row's
    ordering ``ts`` plus role, the pair ``_ChatSlot.append`` makes unique
    within one transcript.
    """
    meta = prompt.get("meta")
    mid = meta.get("mid") if isinstance(meta, dict) else None
    ts = prompt.get("ts")
    for row in messages:
        row_meta = row.get("meta")
        if mid and isinstance(row_meta, dict) and row_meta.get("mid") == mid:
            return True
        if not mid and ts and row.get("ts") == ts and row.get("role") == prompt.get("role"):
            return True
    return False


def _latest_turn_was_deliberately_stopped(messages: list[dict]) -> bool:
    """Whether the newest turn ends in the user's own Stop card.

    Same tail walk as :func:`state.is_turn_interrupted`: tool, notice and
    status rows are looked through, and the newest stop or conversational row
    decides. Read only when a local-turn marker outlived its process. The stop
    handler cancels the runner, and a shutdown landing in that same instant can
    leave the marker on disk, so the Stop card -- the user's recorded intent --
    must win over a restart-interruption row.
    """
    for message in reversed(messages):
        if is_stop_event_row(message):
            return True
        if message.get("role") in ("user", "assistant") and message.get("content"):
            return False
    return False


def _reconcile_local_turn_marker(
    slot: _ChatSlot,
    generation: int,
    prompt: Mapping[str, object] | None = None,
    *,
    persisted: Iterable[Mapping[str, object]] | None = None,
) -> bool:
    """Turn a local-turn marker that outlived its process into the interruption row.

    Returns whether a marker was present, so the startup restore can avoid
    layering the remote-relay notice on top of metadata that claims both kinds
    of execution. Call it only after the whole window is loaded and
    ``_disk_window_len`` is set: rows are appended past that boundary so the
    next save writes them rather than counting them as already on disk.

    The marker says a turn was admitted and never reached teardown, which the
    transcript alone cannot show -- partial assistant prose followed by
    finished tool rows is shape-identical to a clean answer. Rows ride the
    periodic flush, so the opening row itself may be missing: when the marker
    carries a copy (*prompt*) and no row with its identity is on disk, the
    copy is appended first. *persisted* is EVERY on-disk row of the session,
    not the loaded window: a long turn can push its own opener past the
    window bound into the frozen prefix, and a check against the window alone
    would append a duplicate that the next save then writes for good. Callers
    that hold only the window pass nothing and the window is searched. Only
    then is the tail judged: when the transcript already proves the
    interruption (an unanswered user row, a trailing error) no second row is
    needed; when the newest turn ends in the user's own Stop card, that intent
    wins. Otherwise one ``error`` row lands at the tail so the classifier,
    composer and sidebar agree.

    Every window save rewrites the window from its first row, so a lost
    opener implies every row after it is lost too and the tail is where it
    belongs. The runtime marker starts clear and the slot is marked dirty even
    when no row was appended, so the next save omits the slot-owned keys. A
    second restart before that save re-runs this decision from the same bytes
    and cannot accumulate rows.
    """
    if generation <= 0:
        return False
    if prompt is not None and not _window_holds_row(
        slot.messages if persisted is None else persisted, prompt
    ):
        prompt_meta = prompt.get("meta")
        role = str(prompt["role"])
        slot.append(
            role,
            str(prompt["content"]),
            # The loader's own default for a row whose ``cls`` is not on disk.
            "msg msg-u" if role == "user" else "msg msg-a",
            str(prompt.get("ts") or ""),
            broadcast=False,
            meta=dict(prompt_meta) if isinstance(prompt_meta, dict) and prompt_meta else None,
        )
    if not is_turn_interrupted(slot.messages) and not _latest_turn_was_deliberately_stopped(
        slot.messages
    ):
        slot.append(
            "error",
            _RESTART_INTERRUPTION_MSG,
            "msg msg-err",
            broadcast=False,
            meta={"kind": _RESTART_INTERRUPTION_KIND},
        )
    slot._turn_in_flight_generation = 0
    slot._turn_in_flight_prompt = None
    slot._dirty = True
    return True

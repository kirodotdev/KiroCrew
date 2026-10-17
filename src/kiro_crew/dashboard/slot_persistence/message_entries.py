"""How an in-memory slot row becomes a persisted transcript line, and back.

``_build_message_entry_uncached`` is the one projection a dashboard save applies to
every window row: transient roles drop, non-user content is redacted, inline images
are copied into the session's attachment store and the row is pointed at the copy,
provenance is carried, and blocked-link records ride only with the text they
describe. ``_attach_variants`` is the load-side counterpart for a row's alternate
replies, and ``_approx_window_payload_bytes`` the cheap size bound a save checks
before it routes a window through the memo. The memo itself
(``_build_message_entry``) and its process-wide cache state stay in
``chat_persistence``.

New fields a persisted row carries, or new redaction a row needs, belong here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.chat_attachments import ImageBudget
from kiro_crew.dashboard.chat_utils import (
    UNKNOWN_ROW_FIELDS_KEY,
    _redact_meta_for_role,
    drop_records_without_placeholders,
    redact_display_content,
    with_bounded_redaction_records,
)
from kiro_crew.history import PROVENANCE_FIELDS, carry_provenance

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


#: Every top-level key this build writes on a persisted transcript row: the keys
#: ``_build_message_entry_uncached`` writes, and ``tools``, which
#: ``ConversationLog.append`` writes. A row key outside this set came from a newer
#: build. ``test_slot_save_newer_row_fields`` pins that the builder writes nothing
#: outside it.
_KNOWN_ROW_KEYS = frozenset(
    {"role", "content", "ts", "cls", "meta", "variants", "variant_idx", "tools", *PROVENANCE_FIELDS}
)

#: The most one restored row keeps of the fields a newer build wrote on it, measured
#: as the JSON the save writes them back as (``json.dumps``: ASCII, so a character is a
#: byte). One bound on the whole set bounds every kept key, string and nested
#: container, and it applies where restore keeps them. A row over it keeps none of them
#: (logged), so its next save writes the row as this build knows it.
MAX_UNKNOWN_ROW_FIELDS_BYTES = 64 * 1024

#: The most one slot keeps in total, measured the same way. The row count does not
#: bound it: the refresh and reconcile paths append past the 500-row restore window,
#: up to ``_MAX_SLOT_MESSAGES`` (10,000) rows. When the bytes the slot holds pass it,
#: counted afresh over ``slot.messages`` (never the running count alone), the oldest
#: rows' kept fields are dropped first, down to half of it so the next drop is not one
#: row away, and those rows are written without them by the next save.
MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES = 1024 * 1024


class _KeptRowFields(dict):  # type: ignore[type-arg]
    """One row's kept fields, with the size measured when they were kept."""

    __slots__ = ("nbytes",)
    nbytes: int


def remember_unknown_row_fields(slot: _ChatSlot, row: dict) -> None:
    """Keep the fields a newer build wrote on *row* on the slot's newest message.

    That message is the one restore just built from the persisted *row*. Only keys
    outside :data:`_KNOWN_ROW_KEYS` are kept, as read, so the save can write them back
    (:func:`_build_message_entry_uncached`), up to :data:`MAX_UNKNOWN_ROW_FIELDS_BYTES`
    for the row (over it, none are kept) and :data:`MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES`
    for the slot (:func:`_drop_oldest_unknown_row_fields`). The key never leaves the
    server: every path that sends a message out removes it
    (``chat_utils.without_unknown_row_fields``).
    """
    unknown = {key: value for key, value in row.items() if key not in _KNOWN_ROW_KEYS}
    if not unknown:
        return
    size = len(json.dumps(unknown))
    if size > MAX_UNKNOWN_ROW_FIELDS_BYTES:
        logger.warning(
            "Restored transcript row has %d field(s) from a newer build totalling %d bytes, "
            "over the %d-byte cap; they are not kept, so the next save writes the row without them",
            len(unknown),
            size,
            MAX_UNKNOWN_ROW_FIELDS_BYTES,
        )
        return
    kept = _KeptRowFields(unknown)
    kept.nbytes = size
    slot.messages[-1][UNKNOWN_ROW_FIELDS_KEY] = kept
    # The running count is an upper bound: rows a window rebuild, a trim or a rewind
    # took out of ``slot.messages`` are still in it. So it only decides WHEN to
    # recount; whether anything is dropped is decided on the bytes the slot holds now.
    total = getattr(slot, "_unknown_row_fields_bytes", 0) + size
    if total > MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES:
        total = _kept_row_fields_bytes(slot)
    slot._unknown_row_fields_bytes = total
    if total > MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES:
        _drop_oldest_unknown_row_fields(slot)


def _kept_size(kept: dict) -> int:
    """One row's kept fields' size: the one measured when they were kept."""
    size = getattr(kept, "nbytes", None)
    return len(json.dumps(kept)) if size is None else size


def _kept_row_fields_bytes(slot: _ChatSlot) -> int:
    """The bytes of newer-build fields *slot*'s messages hold now."""
    total = 0
    for m in slot.messages:
        kept = m.get(UNKNOWN_ROW_FIELDS_KEY)
        if kept is not None:
            total += _kept_size(kept)
    return total


def _drop_oldest_unknown_row_fields(slot: _ChatSlot) -> None:
    """Keep the newest rows' fields within half the slot budget; drop every older row's.

    A row that loses them stays the same dict: the key's value is set to None in place.
    Rewind and edit-resend find the rows that arrived during their awaits by identity
    (``id(row)``), so a row replaced by a copy would read as arrived. Setting an existing
    key also never resizes the dict, which a slot-detail render may be iterating on a
    worker thread. The save skips a None value and every egress path strips the key.
    """
    keep_up_to = MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES // 2
    kept_total = 0
    dropped = 0
    full = False
    messages = slot.messages
    for index in range(len(messages) - 1, -1, -1):
        m = messages[index]
        kept = m.get(UNKNOWN_ROW_FIELDS_KEY)
        if kept is None:
            continue
        size = _kept_size(kept)
        if not full and kept_total + size <= keep_up_to:
            kept_total += size
            continue
        full = True
        m[UNKNOWN_ROW_FIELDS_KEY] = None
        dropped += 1
    slot._unknown_row_fields_bytes = kept_total
    if dropped:
        logger.warning(
            "Slot %s: fields from a newer build on %d older row(s) are not kept, so the "
            "slot stays within %d bytes of them; the next save writes those rows without them",
            getattr(slot, "key", "?"),
            dropped,
            MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES,
        )


def _attach_variants(slot: _ChatSlot, m: dict) -> None:
    """Copy variant history from a persisted message onto the slot's last message, with redaction."""
    if m.get("variants"):
        slot.messages[-1]["variants"] = [  # type: ignore[assignment]
            with_bounded_redaction_records(
                {
                    **v,
                    "content": redact_display_content(v.get("content", "")),
                }
            )
            for v in m["variants"]
            if isinstance(v, dict)
        ]
        slot.messages[-1]["variant_idx"] = m.get("variant_idx", 0)


def _approx_window_payload_bytes(window: list[dict]) -> int:
    """Cheap LOWER BOUND on what a window would serialize to, in bytes.

    Sums only string ``content`` on each message and on its variants, ignoring
    keys, meta and JSON escaping, and never serializes anything -- serializing to
    measure would pay the very cost the caller is deciding whether to avoid.

    Being a lower bound is what makes it safe to gate on: an estimate above the
    ceiling proves the real payload is above it too, so the bypass it triggers is
    always justified, while an underestimate merely forgoes the bypass and pays
    the hashing cost. Either way correctness is unaffected -- only throughput.
    """
    total = 0
    for m in window:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        variants = m.get("variants")
        if isinstance(variants, list):
            for v in variants:
                if isinstance(v, dict):
                    vc = v.get("content")
                    if isinstance(vc, str):
                        total += len(vc)
    return total


def _build_message_entry_uncached(
    m: dict, *, attachments: tuple[Path, str] | None = None
) -> dict | None:
    """Build one persisted JSONL message dict from an in-memory slot message.

    Returns None for transient roles that are never persisted. Applies the
    same redaction the overwrite path used so append and rewrite produce
    byte-identical lines for the same message.

    *attachments* is ``(sessions directory, transcript stem)`` when the caller
    knows which session this row belongs to, which turns on inline-image
    preservation: the image a ``![alt](/abs/path.png)`` names is copied into that
    session's attachment directory and the PERSISTED destination is rewritten to
    point there (see :mod:`kiro_crew.chat_attachments`), and the in-memory row is
    updated to the same destination so every later flush of the window is a
    no-op for it. ``None`` skips the step, which is what a caller with no session
    context (a test, a preview) gets.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    role = m.get("role", "assistant")
    if role in ("chunk", "done", "streaming", "queued", "permission"):
        return None
    content = m.get("content", "")
    # Gate is `!= "user"`, NOT `not in ("user", "system")`. _save_slot_to_history
    # re-serializes the WHOLE in-memory window on every flush, so this is the
    # write-back boundary. `system` must be included: the load path does not
    # redact `system` on the way in, so excluding it here would let unredacted
    # bytes from a legacy or foreign writer survive the rewrite indefinitely.
    if role != "user":
        if attachments is not None:
            # One budget for the whole row: the variants below draw on it too.
            image_budget = ImageBudget()
            # COMMITTED back into the live row, not computed on the side. This
            # function re-serializes the whole window on every flush from the
            # in-memory rows, so a row that kept naming the scratch file would
            # be re-resolved from scratch each time -- and once the agent's
            # scratch is reclaimed, that resolution fails open to the dead path
            # and the flush OVERWRITES the good persisted row with it. Writing
            # the durable path into the row makes every later flush idempotent
            # by construction (the destination is already inside the store),
            # and the live UI reads the image from disk at view time either
            # way, so nothing it shows changes.
            #
            # Before redaction, so the file read is of the path as written.
            # Redaction still runs on the result, so what lands on disk is
            # exactly as redacted as before.
            #
            # Compare-and-set, not a blind assignment: this runs in the save's
            # worker thread while the event loop owns the same row dict, and a
            # variant switch can replace the row's content between the read
            # above and this line. Writing the rewrite of the OLD text over the
            # user's newly chosen reply would lose that choice; when the row has
            # moved on, the next flush rewrites whatever it holds then.
            rewritten = cp.persist_inline_images(
                content, sessions_dir=attachments[0], stem=attachments[1], budget=image_budget
            )
            if rewritten != content:
                if m.get("content") == content:
                    m["content"] = rewritten
                content = rewritten
        content, _ = cp.redact_exfiltration_urls(content)
        content, _ = cp.redact_credentials(content)
    entry: dict = {
        "role": role,
        "content": content,
        "ts": m.get("ts", ""),
        # "dashboard" is the fallback, not the answer. A channel tab shares the
        # channel's transcript, so the window this re-serializes can hold turns
        # that arrived FROM Slack or Discord with their own recorded origin; the
        # load paths carry that origin onto the in-memory message so it survives
        # the round trip. Hardcoding "dashboard" flattened it on the next flush,
        # making the audit trail claim inbound channel traffic was typed into
        # the dashboard. A message with no recorded origin genuinely IS a
        # dashboard-authored turn, so it keeps these defaults.
        "source_thread": "dashboard",
        "source_user": "dashboard",
    }
    carry_provenance(entry, m)
    if m.get("variants"):
        redacted_variants: list[dict] = []
        for v in m["variants"]:
            if not isinstance(v, dict):
                continue
            vc = v.get("content", "")
            # A variant is an alternate reply the user can switch BACK to, so its
            # images break in exactly the way this rewrite exists to stop. It is
            # persisted and redacted here, so it is rewritten here too -- from
            # the SAME budget as the primary content, so a row with many
            # variants cannot copy many times the per-message ceiling -- and
            # committed into the live variant for the reason the primary is.
            if attachments is not None and role != "user":
                rewritten = cp.persist_inline_images(
                    vc, sessions_dir=attachments[0], stem=attachments[1], budget=image_budget
                )
                if rewritten != vc:
                    if v.get("content") == vc:  # compare-and-set, as for the primary
                        v["content"] = rewritten
                    vc = rewritten
            vc, _ = cp.redact_exfiltration_urls(vc)
            vc, _ = cp.redact_credentials(vc)
            v_entry = {**v, "content": vc}
            # A variant's records are ITS OWN, under the same rule the row obeys:
            # they describe this variant's text, so they ride with it, they go
            # through their bounded constructors, and they are dropped when that
            # text holds no placeholder to explain.
            drop_records_without_placeholders(v_entry, vc)
            v_entry = with_bounded_redaction_records(v_entry)
            redacted_variants.append(v_entry)
        entry["variants"] = redacted_variants
        entry["variant_idx"] = m.get("variant_idx", 0)
    cls_val = m.get("cls", "")
    if role == "system" and cls_val:
        entry["cls"] = cls_val
    meta_src = m.get("meta") if isinstance(m.get("meta"), dict) else None
    if meta_src is not None:
        meta_in = dict(meta_src)
        # Records are CARRIED, never re-derived here. They are born at the one
        # moment the URL exists -- the redaction that produces this row's text --
        # so by the time this function sees the content it holds the placeholder
        # and a scan of it would describe nothing. The records still have to
        # DESCRIBE this text, so a row whose content shows no placeholder does not
        # keep them; `_redact_meta_for_role` re-validates whatever survives.
        drop_records_without_placeholders(meta_in, content)
        entry["meta"] = _redact_meta_for_role(role, meta_in)
    # Fields a newer build wrote on this row, kept by restore
    # (``remember_unknown_row_fields``): written back exactly as read, fill-only, so a
    # key this build writes is never overwritten -- the rule ``crons.json`` and the
    # config file apply to unknown keys. They were on disk already; writing them back
    # unchanged adds no exposure, and redaction keeps applying to the fields above.
    unknown = m.get(UNKNOWN_ROW_FIELDS_KEY)
    if isinstance(unknown, dict):
        for key, value in unknown.items():
            if key not in _KNOWN_ROW_KEYS:
                entry.setdefault(key, value)
    return entry

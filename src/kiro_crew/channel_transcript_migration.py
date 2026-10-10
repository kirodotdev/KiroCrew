"""One-time merge of orphaned dashboard copies back into channel transcripts.

A conversation started on a messaging channel persists under a channel session
key (``slack:<thread_ts>``), whose transcript file is ``slack_<thread_ts>.jsonl``.
Its dashboard tab now shares that one file. Earlier the tab owned a SEPARATE
transcript at ``dashboard_slack_<thread_ts>.jsonl``, seeded as a copy — so an
existing install can hold a leftover file of that shape, and any turn the user
typed in the tab lives ONLY there. Those are real user messages, so the leftover
cannot simply be deleted.

This module merges such a file into the channel transcript and then removes it.
Two states exist on an upgraded install:

* **One file** (``slack_<ts>.jsonl`` alone) -- the common case: the tab was
  surfaced but never written to, so there is nothing to migrate.
* **Two files** -- the user typed in the tab, or closed it. The extra file holds
  turns that exist nowhere else.

Design:

* **Loss-safe ordering.** The merged transcript is written first (atomically, via
  temp file + ``os.replace``); the leftover is deleted only after that write
  returns. Any failure leaves BOTH files exactly as they were, so the worst
  outcome is "the leftover is still there", never "messages are gone".
* **Idempotent.** A successful pass leaves one file, so the next pass finds
  nothing. A pass that merged but failed to delete re-merges to a byte-identical
  result (every message is already present, so de-duplication drops all of them)
  and retries the delete. Safe to run on every startup.
* **Merged by timestamp.** Both files' messages are ordered on their ``ts``
  field, with the channel side winning ties, so a reader sees one chronological
  conversation rather than two concatenated halves.
* **Precise identification.** A candidate must have a ``dashboard_`` prefix whose
  remainder is a channel session key stem (per
  :func:`~kiro_crew.messaging.link.is_channel_session_key`) AND that channel
  transcript must already exist. A dashboard-born session is therefore never
  touched: its name is ``chat-<N>-<ts>``, which is not a channel key, and it has
  no channel transcript to merge into.
* **The channel file's metadata line survives.** It carries the binding that
  makes the tab the conversation; letting the leftover's metadata overwrite it
  would break that. Only genuinely user-owned presentation fields are carried
  over, and only where the channel side has none (see :data:`_CARRIED_META_FIELDS`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.history import (
    ConversationLog,
    HistoryLockTimeout,
    _restore_mtime,
    _safe_mtime,
    transcript_sort_key,
)
from kiro_crew.history_projection import DISPLAY_ONLY_ROLES
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: Filename prefix the dashboard's own transcripts carry (``history._safe_key``
#: folds the ``dashboard:`` key namespace to this).
_DASHBOARD_STEM_PREFIX = "dashboard_"

#: Metadata fields carried from the leftover onto the channel transcript, and
#: only when the channel side has no value of its own.
#:
#: These three are the user's own presentation choices about the conversation --
#: a name they typed, a folder they filed it under, a pin they set. They were
#: expressed on the tab, so the tab's file is the only place they exist; dropping
#: them would silently undo work the user did.
#:
#: Everything else stays with the channel transcript on purpose:
#:
#: * ``linked_session_key`` binds the tab to the channel session -- the channel
#:   file owns it and a stale copy would repoint the tab at the wrong session.
#: * ``memory_mode`` is a privacy control scoped to the surface it was set on.
#:   Carrying ``incognito`` onto a live channel conversation would drop it out of
#:   search, listing and slot surfacing -- a large, hard-to-undo change the user
#:   never asked for on that conversation.
#: * ``agent`` is resolved from the channel's own configuration on every turn, so
#:   a value copied off a dead surface would fight the live one.
#: * ``tab_id`` chains transcripts for the dashboard's multi-file reads; adopting
#:   the leftover's chain would splice unrelated files into this conversation.
#: * ``closed`` / ``closed_at`` describe a surface that no longer exists.
#: * ``created_at`` / ``last_consolidated`` / ``rotation_generation`` are the
#:   channel file's own bookkeeping and must keep counting from its history.
_CARRIED_META_FIELDS = ("title", "folder_id", "pinned")


def _orphan_target_stem(stem: str) -> str:
    """Channel transcript stem *stem* is an orphaned copy of, or ``""``.

    Repeated ``dashboard_`` prefixes are stripped the same way
    :func:`~kiro_crew.dashboard.chat_utils._history_key_for` strips them, so a
    stacked name resolves to the same conversation.
    """
    if not stem.startswith(_DASHBOARD_STEM_PREFIX):
        return ""
    while stem.startswith(_DASHBOARD_STEM_PREFIX):
        stem = stem[len(_DASHBOARD_STEM_PREFIX) :]
    return stem if is_channel_session_key(stem) else ""


#: Shared with the dashboard save path, which must interleave a channel's
#: foreign appends into its own window in the right order. Same problem, same
#: mixed-format timestamps, so it must be the same comparison — see
#: ``transcript_sort_key``.
_epoch_of = transcript_sort_key


def _sort_keyed(
    messages: list[dict], source_rank: int
) -> list[tuple[tuple[int, float], int, dict]]:
    """Pair each message with the key it sorts on.

    A message with no ``ts`` inherits the previous message's timestamp so it
    stays adjacent to the neighbour it was written next to instead of sorting to
    the front of the conversation. ``source_rank`` breaks a tie between the two
    files deterministically; ties WITHIN a file keep their on-disk order because
    :func:`sorted` is stable.
    """
    keyed: list[tuple[tuple[int, float], int, dict]] = []
    last_key = (1, 0.0)
    for m in messages:
        ts = m.get("ts")
        if isinstance(ts, str) and ts:
            last_key = _epoch_of(ts)
        keyed.append((last_key, source_rank, m))
    return keyed


def _identity(message: dict) -> tuple:
    """De-duplication identity of a message: same role, content and instant.

    Content is compared through both redactors so the two files' copies of one
    message match. They can legitimately differ byte-for-byte: the dashboard
    write path redacts model-authored text, while text stored by the channel path
    may be verbatim — so a credential-bearing turn exists as
    redacted text in the orphan and raw text in the channel file. Comparing raw
    content would call those two different messages and keep both.
    """
    content = message.get("content")
    if not isinstance(content, (str, int, float, bool, type(None))):
        content = json.dumps(content, sort_keys=True)
    if isinstance(content, str):
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
    return (message.get("role"), content, message.get("ts"))


def _merge_messages(channel: list[dict], orphan: list[dict]) -> list[dict]:
    """Interleave both files' messages chronologically, dropping duplicates.

    Deduplication is COUNT-WISE, not set-wise. The two files overlap because the
    orphan was seeded from the channel transcript, so a message present in both
    must survive once. But a message repeated WITHIN one file is two real events
    — the same short reply twice in a second, or two rows stamped in one clock
    tick on a coarse-granularity clock — and a set-based ``seen`` would delete
    one of them permanently, since this merge removes the source file afterwards.

    So each identity is emitted as many times as the file that carried the MOST
    copies of it: cross-file duplicates collapse, within-file repeats are kept.
    The channel side is emitted first for any duplicate pair, so the surviving
    copy is the one the live conversation already agrees on.
    """
    allowed: dict[tuple, int] = {}
    for source in (channel, orphan):
        per_source: dict[tuple, int] = {}
        for m in source:
            ident = _identity(m)
            per_source[ident] = per_source.get(ident, 0) + 1
        for ident, n in per_source.items():
            if n > allowed.get(ident, 0):
                allowed[ident] = n

    keyed = _sort_keyed(channel, 0) + _sort_keyed(orphan, 1)
    keyed.sort(key=lambda item: (item[0], item[1]))
    merged: list[dict] = []
    emitted: dict[tuple, int] = {}
    for _ts, _rank, message in keyed:
        ident = _identity(message)
        if emitted.get(ident, 0) >= allowed.get(ident, 0):
            continue
        emitted[ident] = emitted.get(ident, 0) + 1
        merged.append(message)
    return merged


def _merged_metadata(channel_meta: dict, orphan_meta: dict) -> dict:
    """Channel metadata, backfilled with user-owned fields only it lacks."""
    meta = dict(channel_meta)
    for field in _CARRIED_META_FIELDS:
        if not meta.get(field) and orphan_meta.get(field):
            meta[field] = orphan_meta[field]
    return meta


def _rendered_head(messages: list[dict], offset: int) -> dict | None:
    """The first message at or after *offset* that a consolidation prompt renders.

    Display-only rows (``DISPLAY_ONLY_ROLES`` -- a ``notice`` such as an
    AutoNudge) are carried inside a chunk but never sliced, so the character
    sub-offset describes the first NON-display-only row at or after the marker,
    not the raw ``messages[offset]``. Returns ``None`` when no rendered row
    remains, which compares unequal to any dict and so is treated as a move.
    """
    for message in messages[offset:]:
        if message.get("role") not in DISPLAY_ONLY_ROLES:
            return message
    return None


def _invalidate_consolidation_offset(
    meta: dict, channel_messages: list[dict], merged: list[dict]
) -> None:
    """Reset ``last_consolidated`` in *meta* if this merge shifted its indices.

    ``last_consolidated`` is an absolute message INDEX — consolidation reads
    ``messages[offset:]``. Interleaving the orphan's messages into the channel
    transcript inserts lines mid-file, so every index at or after the first
    insertion shifts and the retained offset now points PAST messages that were
    never consolidated. Those messages are then skipped forever: nothing
    re-examines a region the offset claims is done, so their content never
    reaches memory extraction. ``history.py`` documents the identical hazard for
    rotation, which is why a rotation resets the offset and bumps
    ``rotation_generation``; a merge is the same class of structural rewrite and
    has to say so.

    Reset only when the insertion actually landed inside the consolidated
    region: when every orphan message is newer than the whole channel file the
    merge is a pure append, the first *offset* messages are untouched, and
    zeroing would re-consolidate an entire conversation for nothing.

    The generation bump is for a consolidator in ANOTHER process that snapshotted
    its offset before this merge — its own generation check then fires and it
    discards the stale offset instead of writing it back.

    An in-flight sub-offset is the SAME hazard one message deeper, and it bites
    even when ``last_consolidated`` is 0. ``consolidation_sub_offset`` is a
    CHARACTER position inside the first unconsolidated message —
    ``messages[offset]``, the oversized head being extracted a slice at a time
    (see :func:`history_consolidation._slice_head_for_budget`). The merge sorts
    chronologically, so an older orphan message lands at or before that index and
    the message now at ``messages[offset]`` is a DIFFERENT one. Resuming the
    slice at the retained character position then marks the new head's unread
    prefix consolidated — the data loss this whole path exists to prevent — and
    the index-shift test above never catches it at ``offset == 0`` because both
    ``[:0]`` slices are empty and compare equal. So whenever the message at the
    marker moved, drop any sub-offset by bumping the generation (which is exactly
    how ``_sub_offset_for_generation`` and ``advance_consolidation_sub_offset``
    discard a stale sub-offset).

    The head-moved bump fires on the rendered head moving ALONE — it does not
    require a sub-offset to be persisted yet. A consolidator in another process
    can have written its final snapshot (setting ``last_consolidated``) but not
    yet its first ``consolidation_sub_offset``: gating the bump on an already-live
    sub-offset would leave the generation it captured unchanged, and its first
    ``advance_consolidation_sub_offset`` — whose generation guard still matches —
    would write a character offset into the replacement head it never read. The
    unconditional bump makes that write's generation guard reject the stale
    sub-offset, closing the window.
    """
    generation = meta.get("rotation_generation")
    gen = generation if isinstance(generation, int) else 0

    def _bump() -> None:
        meta["rotation_generation"] = gen + 1

    offset = meta.get("last_consolidated") or 0
    if not isinstance(offset, int):
        offset = 0

    # Whether the first ``offset`` messages are unchanged by the merge. A pure
    # append leaves them untouched, and zeroing then would re-consolidate a
    # whole conversation for nothing. Computed once because both the sub-offset
    # branch and the plain index-shift branch below need it.
    prefix_changed = offset > 0 and merged[:offset] != channel_messages[:offset]

    # The sub-offset is a character position inside the first message the
    # extraction prompt actually renders -- the first NON-display-only row at or
    # after the marker, because ``_consolidation_chunk`` carries display-only
    # rows (a ``notice`` such as an AutoNudge) inside the prefix but never slices
    # one. Comparing the raw ``messages[offset]`` would miss a replacement of the
    # rendered head whenever a display-only notice sits at the marker: the notice
    # is unchanged, so the raw compare says "same" while the oversized head
    # behind it was swapped for an older orphan. So locate the first rendered row
    # at or after the marker in each transcript and compare THOSE.
    #
    # The bump fires on ANY head move, NOT only when a sub-offset is already
    # persisted. A consolidator in another process can be mid-way through the
    # first slice of the over-budget head -- it has written its final snapshot
    # (so ``last_consolidated`` is set) but not yet its first
    # ``consolidation_sub_offset``. If this merge replaced that head with an
    # older orphan and only bumped the generation when a sub-offset was already
    # live, nothing would invalidate the generation the racing consolidator
    # captured, and its first ``advance_consolidation_sub_offset`` would write a
    # character offset into content it never read -- the new head's prefix lost.
    # Bumping whenever the rendered head moves makes that write's generation
    # guard reject the stale sub-offset. The reset-to-0 for a rewritten prefix is
    # kept exactly as before.
    #
    # The move only counts when the ORIGINAL transcript had a rendered head at
    # the marker (``original_head is not None``): that is the head a racing
    # consolidator could be slicing. When the original had no rendered row at or
    # after the marker -- every message is already consolidated -- an orphan
    # appended there is a pure append, not a displacement, so it must not bump
    # (re-slicing a head that never existed would be wasted work).
    original_head = _rendered_head(channel_messages, offset)
    if offset < len(merged) and original_head is not None:
        if _rendered_head(merged, offset) != original_head:
            # The head at the marker now holds different content, so any live or
            # about-to-be-written sub-offset describes the wrong message: drop it
            # by bumping the generation. If the merge also rewrote the
            # consolidated prefix (an older orphan landed strictly before the
            # marker), the marker itself is stale too and must reset to 0 so the
            # unread orphan is extracted: a bump alone leaves the marker pointing
            # past a displaced message, which would re-consolidate a row and
            # silently drop the orphan.
            if prefix_changed:
                meta["last_consolidated"] = 0
            _bump()
            return

    if offset <= 0:
        return
    if not prefix_changed:
        return
    meta["last_consolidated"] = 0
    _bump()


def _write_merged(path: Path, meta: dict, messages: list[dict]) -> None:
    """Replace *path* with *meta* followed by *messages*, atomically."""
    lines = [json.dumps(meta) + "\n"]
    lines.extend(json.dumps(m) + "\n" for m in messages)
    atomic_write(path, "".join(lines))


def _migrate_one(
    log: ConversationLog, channel_stem: str, orphan_stem: str, *, remove: bool = True
) -> bool:
    """Merge one orphan into its channel transcript. True if the orphan is gone.

    The read, merge, write and delete all happen inside the channel session's
    lock -- the same cross-process lock every other transcript mutation takes --
    so a concurrent append cannot be lost to the rewrite. The orphan's lock nests
    inside it; the order is fixed (channel then orphan) so two migrators cannot
    deadlock against each other.

    With ``remove`` false the merge is written and the orphan is LEFT IN PLACE
    (the answer is then always false): the file is a record other startup work
    may still be reading, and a later pass -- this one re-merges to a
    byte-identical transcript -- removes it. See
    :func:`migrate_channel_transcripts`.
    """
    channel_path = log._path(channel_stem)
    orphan_path = log._path(orphan_stem)
    with log._locked(channel_stem), log._locked(orphan_stem):
        # Re-check under the lock: a concurrent pass in another process may have
        # already finished this migration between the scan and the acquire.
        if not orphan_path.exists() or not channel_path.exists():
            return not orphan_path.exists()

        orphan_messages = list(log.read_messages(orphan_stem))
        channel_messages = list(log.read_messages(channel_stem))
        channel_meta = log.get_metadata(channel_stem)
        merged = _merge_messages(channel_messages, orphan_messages)
        meta = _merged_metadata(channel_meta, log.get_metadata(orphan_stem))
        _invalidate_consolidation_offset(meta, channel_messages, merged)

        if merged != channel_messages or meta != channel_meta:
            # Last activity is last activity whichever surface wrote it, so the
            # merged file keeps the newer of the two mtimes. Advancing it to
            # "now" instead would float every migrated conversation to the top
            # of the session list on the upgrade restart.
            prev_mtime = max(
                (t for t in (_safe_mtime(channel_path), _safe_mtime(orphan_path)) if t is not None),
                default=None,
            )
            _write_merged(channel_path, meta, merged)
            _restore_mtime(channel_path, prev_mtime)
            log._invalidate_cache(channel_stem)
            log.invalidate_tab_id_cache()

        # Only now, with every message durable in the channel transcript, is the
        # orphan expendable -- and only when the caller says it may go.
        if not remove:
            logger.info(
                "channel transcript migration: merged %s into %s; the copy stays for a "
                "later pass to remove",
                orphan_stem,
                channel_stem,
            )
            return False
        return log.delete_session(orphan_stem)


def migrate_channel_transcripts(
    log: ConversationLog | None = None,
    *,
    dashboard_slots: frozenset[str] | None = None,
    remove: bool = True,
) -> int:
    """Merge every orphaned dashboard copy into its channel transcript.

    Returns the number of orphans merged and removed. Blocking (file I/O plus a
    cross-process ``flock``): callers on the event loop must offload it, e.g.
    ``await asyncio.to_thread(migrate_channel_transcripts)``.

    *dashboard_slots* names the slots the session map claims as real dashboard
    sessions; any orphan whose target is in that set is left alone. Omitting it
    disables that protection, so the gateway startup call always supplies it.

    *remove* false merges every orphan but deletes none: the copies stay on
    disk, to be removed by a later pass. The gateway passes false while its
    one-time crewmate prune has not settled (and runs a removing pass once it
    has). That prune's only evidence is each crewmate's own DM-thread
    transcript (``dashboard_<member slot key>``, live or archived); it never
    reads these channel orphans, so the deferral does not protect its evidence
    and only delays the deletes. The merge itself still lands before the
    session restores read the channel transcript.

    Best-effort per orphan: a lock timeout or I/O error is logged and the next
    orphan is still attempted, because one wedged session must not stop the rest
    of the install from converging.
    """
    log = log or ConversationLog()
    # Slot names the session map says belong to REAL dashboard sessions. A
    # filename shape is not proof of provenance: a dashboard-born session can
    # be named ``slack_<ts>`` (slot names fold arbitrary display text through
    # the same charset), and its own transcript is then
    # ``dashboard_slack_<ts>.jsonl`` — indistinguishable by name from an
    # orphan. Merging that would splice two unrelated conversations together
    # and delete one. Passed IN rather than read from global config so this
    # stays a function of its arguments; the gateway startup call supplies it.
    claimed = dashboard_slots or frozenset()
    if not log._dir.exists():
        return 0
    migrated = 0
    for path in sorted(log._dir.glob("*.jsonl")):
        # Symlinked transcripts are handoff aliases pointing at a real session
        # (matching ``list_sessions``); merging through one would duplicate the
        # target's own messages and removing it would break the alias.
        if path.is_symlink():
            continue
        channel_stem = _orphan_target_stem(path.stem)
        if not channel_stem:
            continue
        if not log._path(channel_stem).exists():
            # No channel transcript to merge into: this file is the only copy of
            # its messages, and inventing the channel side could resurrect a
            # conversation the user deleted. Leave it alone.
            continue
        if channel_stem in claimed:
            logger.info(
                "channel transcript migration: %s is a live dashboard session, not an "
                "orphan of %s; leaving it alone",
                path.stem,
                channel_stem,
            )
            continue
        try:
            if _migrate_one(log, channel_stem, path.stem, remove=remove):
                migrated += 1
                logger.info(
                    "channel transcript migration: merged %s into %s",
                    path.stem,
                    channel_stem,
                )
        except HistoryLockTimeout:
            logger.warning(
                "channel transcript migration: %s is locked; leaving both files "
                "in place and retrying on the next start",
                path.stem,
            )
        except Exception:  # noqa: BLE001 - one bad session must not stop the sweep
            logger.warning(
                "channel transcript migration: failed to merge %s into %s; both "
                "files left in place",
                path.stem,
                channel_stem,
                exc_info=True,
            )
    return migrated

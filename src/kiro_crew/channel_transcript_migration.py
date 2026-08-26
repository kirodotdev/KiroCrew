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
        stem = stem[len(_DASHBOARD_STEM_PREFIX):]
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
    write path has always redacted model-authored text, while the channel path
    historically stored it verbatim — so a credential-bearing turn exists as
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
    """
    offset = meta.get("last_consolidated") or 0
    if not isinstance(offset, int) or offset <= 0:
        return
    if merged[:offset] == channel_messages[:offset]:
        return
    meta["last_consolidated"] = 0
    generation = meta.get("rotation_generation")
    meta["rotation_generation"] = (generation if isinstance(generation, int) else 0) + 1


def _write_merged(path: Path, meta: dict, messages: list[dict]) -> None:
    """Replace *path* with *meta* followed by *messages*, atomically."""
    lines = [json.dumps(meta) + "\n"]
    lines.extend(json.dumps(m) + "\n" for m in messages)
    atomic_write(path, "".join(lines))


def _migrate_one(log: ConversationLog, channel_stem: str, orphan_stem: str) -> bool:
    """Merge one orphan into its channel transcript. True if the orphan is gone.

    The read, merge, write and delete all happen inside the channel session's
    lock -- the same cross-process lock every other transcript mutation takes --
    so a concurrent append cannot be lost to the rewrite. The orphan's lock nests
    inside it; the order is fixed (channel then orphan) so two migrators cannot
    deadlock against each other.
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
        # orphan expendable.
        return log.delete_session(orphan_stem)


def migrate_channel_transcripts(
    log: ConversationLog | None = None,
    *,
    dashboard_slots: frozenset[str] | None = None,
) -> int:
    """Merge every orphaned dashboard copy into its channel transcript.

    Returns the number of orphans merged and removed. Blocking (file I/O plus a
    cross-process ``flock``): callers on the event loop must offload it, e.g.
    ``await asyncio.to_thread(migrate_channel_transcripts)``.

    *dashboard_slots* names the slots the session map claims as real dashboard
    sessions; any orphan whose target is in that set is left alone. Omitting it
    disables that protection, so the gateway startup call always supplies it.

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
            if _migrate_one(log, channel_stem, path.stem):
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

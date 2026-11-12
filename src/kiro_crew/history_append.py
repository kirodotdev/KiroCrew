"""Appending a message to a transcript.

How a message becomes a durable row: the write-boundary redaction, the copy of the
inline images it references, and the append that skips a row already on disk.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_dir``, ``_read_projection``.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import append_line, atomic_write
from kiro_crew.chat_attachments import (
    ImageBudget,
    persist_inline_images,
    same_text_modulo_images,
    store_image_file,
)
from kiro_crew.history_schema import (
    _parse_transcript_ts,
    metadata_now_iso,
    monotonic_transcript_ts,
)
from kiro_crew.owner_only_files import (
    OWNER_ONLY_FILE_MODE,
    mkdirs_owner_only,
    owner_only_opener,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.transcript_count import bump_after_append, stamp_before_append

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

_LOGGER = logging.getLogger("kiro_crew.history")


def _redact_at_write_boundary(role: str, content: str) -> str:
    """Redact model-authored *content* on its way into a transcript.

    One transcript, one redaction rule. A conversation's dashboard tab and its
    channel thread persist to the same file through different code paths, so the
    rule has to live where the bytes are written rather than in either caller.

    The gate is ``role != "user"``, matching the dashboard's own write-back
    boundary: text the user typed is stored verbatim, and everything the model or
    the system produced is scrubbed of credentials and exfiltration URLs.
    Idempotent, so a caller that already redacted loses nothing by passing
    through here.
    """
    if role == "user":
        return content
    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    return content


def _persist_inline_attachments(self: ConversationLog, key: str, role: str, content: str) -> str:
    """*content* with the images it references copied into session storage.

    The write boundary for inline images, mirroring
    :func:`_redact_at_write_boundary`: this is where a message's text becomes
    a durable row, so it is where a referenced image has to stop being a path
    into someone else's temp directory. The agent scratch dir the picture
    usually lives in is reclaimed when the agent process dies, so without
    this the transcript keeps the reference long after the bytes are gone.
    See :mod:`kiro_crew.chat_attachments` for the copy contract.

    Gated on ``role != "user"``, the same gate the redaction boundary uses:
    an inline image is something the agent produced, and a path the user
    typed names a file of their own that this must not duplicate.

    MUST be called under ``_locked(key)``. ``delete_session`` reclaims the
    attachments directory under that same lock, so a copy made outside it can
    be deleted between the copy and the append -- persisting a row that names
    a file already gone, which is the exact defect this exists to remove. The
    lock therefore costs one bounded file copy inside the critical section;
    the dashboard slot save already holds it across a whole-transcript
    read-modify-write, so this is in family. What one call can do is bounded
    per message by :mod:`kiro_crew.chat_attachments`, so the section cannot be
    held for an unbounded time.
    """
    if role == "user" or "![" not in content:
        return content
    path = self._path(key)
    return persist_inline_images(content, sessions_dir=path.parent, stem=path.stem)


def _store_ingested_images(path: Path, content: str, images: Sequence[str]) -> str:
    """*content* naming a stored copy of each picture a channel downloaded for it.

    The one user row whose pictures are stored, because *images* are files the
    gateway itself fetched (``IngestResult.image_paths``), never a path the user
    typed. ``append_attachment_context`` put each on a line of its own and
    followed it with a note that the file lasts until the turn ends; a stored
    picture's line becomes markup naming the copy and its note is dropped, while
    one that cannot be kept leaves both as written. Same lock rule as
    :func:`_persist_inline_attachments`.
    """
    # Deferred: this module pulls transcription in, which history does not need.
    from kiro_crew.messaging.attachments import image_location_note

    budget = ImageBudget()
    for image in images:
        markup = store_image_file(image, sessions_dir=path.parent, stem=path.stem, budget=budget)
        if markup is None:
            continue
        content = "\n".join(markup if line == image else line for line in content.split("\n"))
        content = content.replace(f"\n\n{image_location_note(image)}", "", 1)
    return content


def append(
    self: ConversationLog,
    key: str,
    role: str,
    content: str,
    tools: list[str] | None = None,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
    tab_id: str | None = None,
    cls: str = "",
    mid: str | None = None,
    extra_meta: dict[str, Any] | None = None,
    entry: dict | None = None,
    ingested_images: Sequence[str] = (),
) -> None:
    """Append a message with optional provenance to the session log.

    *extra_meta* adds display fields (e.g. ``turn_stats``) to the row's
    ``meta``; it never overrides ``mid``.

    *ingested_images* are the pictures a channel downloaded for this message;
    each is copied into session storage and the row names the copy
    (:func:`_store_ingested_images`).

    *cls* persists the message's presentation class, so it survives a replay
    from disk after a restart.

    *mid* persists the row's delivery identity as ``meta.mid``, the shape the
    dashboard slot save writes (``chat_persistence._build_message_entry``). A
    writer that also puts the message in the in-memory slot
    (``_ChatSlot.append``, which mints the id) must pass that id, so
    ``_append_unflushed_tail``'s ``meta.mid`` walk recognises the durable copy.
    Without one the row carries no ``meta``, like pre-id transcripts.

    *entry* is a fully built persisted dict in the slot save's line shape
    (``_build_message_entry``: redacted content, own ``ts``, provenance,
    ``variants``, ``meta``), written verbatim so a fallback persists the same
    bytes the save would have. *role*/*content*/*cls*/*mid* must match it
    (they drive locking, dedup and rotation); redaction, attachment rewriting
    and ``ts`` minting are skipped.

    A missing session file is created with a metadata line, recording *agent*
    when given. *agent* has no effect on an existing file; use
    :meth:`update_metadata`.
    """
    # Serialize the create-if-missing + append + rotate against concurrent
    # rewrites (compaction / consolidation) so no write is lost and readers
    # never observe a torn file. ``_locked`` also takes every stable
    # cross-process alias lock so a subagent / cron / CLI writing the same
    # logical session in another process cannot interleave or split its
    # canonical and pre-migration files.
    with self._locked(key):
        # Inside the lock, so a concurrent ``delete_session`` cannot reclaim
        # the attachment between the copy and this row naming it. Idempotent,
        # so the re-entrant call from ``append_if_absent`` is a no-op.
        # Skipped for a prebuilt *entry*, whose builder already rewrote its
        # attachments against the owning session.
        if entry is None:
            content = self._persist_inline_attachments(key, role, content)
            if ingested_images:
                content = _store_ingested_images(self._path(key), content, ingested_images)
        path = self._path(key)
        created_with_tab_id = False
        created_now = False
        if not path.exists():
            created_now = True
            mkdirs_owner_only(self._dir)
            meta: dict = {
                "_type": "metadata",
                "created_at": metadata_now_iso(),
                "last_consolidated": 0,
            }
            if agent:
                meta["agent"] = agent
            if tab_id:
                meta["tab_id"] = tab_id
                created_with_tab_id = True
            # Created 0600 and staged-then-renamed: a crash between create and
            # write would leave an unparseable first line under later rows.
            atomic_write(path, json.dumps(meta) + "\n", mode=OWNER_ONLY_FILE_MODE)

        if entry is not None:
            # Rebuilding from (role, content) would drop the entry's other fields.
            msg: dict = dict(entry)
            # The entry's ``ts`` was taken when it entered the window and can
            # predate rows since landed; it is moved to just past the last row
            # so ``transcript_sort_key`` keeps append order. No readable stamp
            # means the clock is used.
            own = msg.get("ts")
            own_ts = _parse_transcript_ts(own) if isinstance(own, str) else None
            msg["ts"] = monotonic_transcript_ts(
                None if created_now else self._last_row_ts(key),
                own_ts if own_ts is not None else datetime.now().astimezone(),
            )
        else:
            msg = {
                "role": role,
                "content": _redact_at_write_boundary(role, content),
                **({"cls": cls} if cls else {}),
                # Strictly after the last row on disk (see monotonic_transcript_ts);
                # safe because ``_locked`` holds the cross-process flock.
                # ``astimezone()`` stores an absolute instant: local wall time
                # repeats when daylight saving ends.
                "ts": monotonic_transcript_ts(
                    None if created_now else self._last_row_ts(key),
                    datetime.now().astimezone(),
                ),
            }
            if tools:
                msg["tools"] = tools
            if source_thread:
                msg["source_thread"] = source_thread
            if source_user:
                msg["source_user"] = source_user
            if isinstance(mid, str) and mid:
                # ``meta`` holding ``mid`` is the identity shape every reader of
                # this file already matches on (the slot save writes it, the
                # bounded-read walk consumes it); a second spelling would be
                # invisible to both. Only a non-empty ``str`` counts, matching
                # the read side — persisting any other shape would store an id
                # the reader is structurally unable to honour.
                msg["meta"] = {"mid": mid}
            if extra_meta:
                msg["meta"] = {**extra_meta, **msg.get("meta", {})}

        # Session transcripts are intentionally local plaintext JSONL (the
        # documented storage format), not a credential/secret store.
        stored = stamp_before_append(path)
        line = json.dumps(msg)
        append_line(  # lgtm[py/clear-text-storage-sensitive-data]
            path, line, opener=owner_only_opener
        )
        # Same lock hold as the append: keeps the stored message count current.
        bump_after_append(path, stored, line)

        # Invalidate cache since file changed
        self._invalidate_cache(key)

        # A newly-created file carrying a tab_id adds a fresh key to that
        # tab_id's chain — drop the cached tab_id→[keys] index so the next
        # chained read rebuilds it and actually sees this session (a stale
        # index would silently omit it from the chain).
        if created_with_tab_id:
            self.invalidate_tab_id_cache()

        # Rotate if file exceeds size limit. Pass the logical key so the
        # rotation's invalidation reaches every cache-key spelling; the
        # file stem alone cannot recover it (sanitization is lossy).
        # The row above is already durable, so a rotation that fails is
        # logged rather than raised: a raise would report this append as
        # failed and stop a caller writing a turn's rows from writing the rest.
        try:
            self._maybe_rotate(path, key)
        except Exception:
            _LOGGER.warning(
                "Rotating %s failed; it is retried on the next append", path.name, exc_info=True
            )


def append_if_absent(
    self: ConversationLog,
    key: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
    tab_id: str | None = None,
    cls: str = "",
    mid: str | None = None,
    extra_meta: dict[str, Any] | None = None,
    entry: dict | None = None,
    include_rotated: bool = False,
) -> bool:
    """Append a message only if an identical one is not already persisted.

    Returns ``True`` if the message was written, ``False`` if it is already
    on disk — judged by ``(role, content)`` when the caller supplies no
    *mid*, and by ``(role, content)`` plus the SAME ``meta.mid`` when it
    does (see below).

    *entry* is written on the absent branch in place of the minimal row (see
    :meth:`append`); the dedup scan still runs on *role*/*content*/*mid*, which
    must be the entry's own fields.

    The check and the append share one ``_locked`` hold, the same
    cross-process lock the dashboard slot save (``_save_slot_to_history``)
    takes. A separate check would let an :func:`append_off_loop` scheduled
    after the slot save write the message a second time, replayed twice after
    a restart.

    Without *mid*, any row with the same ``(role, content)`` counts. With
    *mid*, a same-role row needs the SAME ``meta.mid`` and a body that is equal
    or equal modulo preserved images (:func:`same_text_modulo_images`): the slot
    save may have rewritten an image path that fails open here once the scratch
    file is gone. The body check stays because ``meta.mid`` is
    caller-suppliable. A body-equal row under another id is a different
    occurrence; skipping on it would lose this one's only durable copy.

    *include_rotated* also searches rows size rotation archived
    (``reason="rotate"`` segments). A writer replaying a whole in-memory window
    (the dashboard's append-safe fallbacks) must set it, since older window rows
    live only in archives. A single-row writer (cron or workflow result) leaves
    it off. An archive that cannot be enumerated raises rather than answering
    "absent".
    """
    supplied_mid = mid if isinstance(mid, str) and mid else None
    with self._locked(key):
        # Rewritten HERE, not left to ``append``: the id-less comparison
        # below is against what is already on disk, which carries
        # rewritten paths. Comparing the original text would never match
        # a persisted row and would append this message a second time.
        content = self._persist_inline_attachments(key, role, content)
        if self._path(key).exists():
            # Compare against the form ``append`` actually stores: the
            # write boundary redacts non-user content, so matching on the
            # raw text would never recognise an already-persisted message
            # that contained a credential and would append it twice.
            persisted = _redact_at_write_boundary(role, content)
            durable: Iterable[dict] = self._read_messages(key)
            if include_rotated:
                # By the PHYSICAL file stem, which rotation names segments
                # after: a pre-migration ``slack:<ts>`` key lives in ``<ts>``
                # with archives ``<ts>__*``. ``_safe_key`` is idempotent on a stem.
                durable = itertools.chain(
                    self._read_projection.read_rotated_messages(self._path(key).stem),
                    durable,
                )
            if self._holds_identical_row(key, durable, role, persisted, supplied_mid):
                return False
        # Reentrant: ``append`` re-enters ``_locked`` for the same key on
        # this thread (RLock + refcounted flock), so the write stays inside
        # the critical section we already hold. The skip paths above leave
        # the persisted rows untouched — an id is never retrofitted onto a
        # row already on disk.
        self.append(
            key,
            role,
            content,
            agent=agent,
            tab_id=tab_id,
            cls=cls,
            mid=mid,
            extra_meta=extra_meta,
            entry=entry,
        )
        return True


def _holds_identical_row(
    self: ConversationLog,
    key: str,
    rows: Iterable[dict],
    role: str,
    persisted: str,
    supplied_mid: str | None,
) -> bool:
    """The ``append_if_absent`` identity rule over *rows*: same role, and same
    body (no id) or same ``meta.mid`` with a corroborating body (with one)."""
    for m in rows:
        if m.get("role") != role:
            continue
        on_disk = m.get("content")
        if supplied_mid is None:
            if on_disk == persisted:
                return True
            continue
        m_meta = m.get("meta")
        if not (isinstance(m_meta, dict) and m_meta.get("mid") == supplied_mid):
            continue
        # Same id: corroborate by body, allowing for the other writer
        # having preserved an image this one could not.
        if on_disk == persisted or (
            isinstance(on_disk, str)
            and same_text_modulo_images(
                on_disk,
                persisted,
                sessions_dir=self._path(key).parent,
                stem=self._path(key).stem,
            )
        ):
            return True
    return False

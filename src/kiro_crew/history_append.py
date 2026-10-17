"""Appending a message to a transcript.

How a message becomes a durable row: the write-boundary redaction, the copy of the
inline images it references, and the append that skips a row already on disk.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import append_line
from kiro_crew.chat_attachments import persist_inline_images, same_text_modulo_images
from kiro_crew.history_schema import metadata_now_iso, monotonic_transcript_ts
from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


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
) -> None:
    """Append a message with optional provenance to the session log.

    *extra_meta* adds display fields (e.g. ``turn_stats``) to the row's
    ``meta``; it never overrides ``mid``.

    *cls* persists the message's presentation class. The in-memory slot
    carries one (``_ChatSlot.append``) but this durable copy had nowhere to
    put it, so any class-borne distinction silently vanished the moment a
    session's rows had to be replayed from disk after a restart.

    *mid* persists the row's delivery identity as ``meta.mid`` — the SAME
    field shape the dashboard slot save writes
    (``chat_persistence._build_message_entry`` copies the window row's
    ``meta`` dict to disk). A dual-writer that reflects a message in the
    in-memory slot (``_ChatSlot.append``, which mints the id) AND persists
    it here must pass that minted id, so both copies carry one identity and
    the bounded-read reconciliation (``_append_unflushed_tail``'s
    ``meta.mid`` walk) recognises the durable copy instead of treating the
    window copy as still owed. Optional: a row appended without one carries
    no ``meta`` at all, which is what pre-id transcripts hold — readers keep
    their id-less fallback for exactly those rows.

    If the session file does not yet exist, it will be created with an
    initial metadata line.  When *agent* is supplied, the agent name is
    recorded in that metadata so the session can be resumed under the
    correct agent later.  (Has no effect if the file already exists;
    use :meth:`update_metadata` to change the agent after creation.)
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
        content = self._persist_inline_attachments(key, role, content)
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
            # Created 0600: a transcript is the conversation itself.
            with open(path, "w", encoding="utf-8", opener=owner_only_opener) as f:
                f.write(json.dumps(meta) + "\n")

        msg: dict = {
            "role": role,
            "content": _redact_at_write_boundary(role, content),
            **({"cls": cls} if cls else {}),
            # Strictly after the row already on disk, so the pair written by
            # one turn stays ordered on a host whose clock cannot separate
            # them (see monotonic_transcript_ts). Consulting the file here is
            # authoritative because ``_locked`` also holds the cross-process
            # flock: no writer, in this process or another, can append
            # between this look and the write below. A file this call just
            # created provably holds no rows yet, so it is not consulted.
            #
            # ``astimezone()`` resolves the clock to an absolute instant
            # before it is stored. A bare local wall
            # clock, which repeats for an hour when daylight saving ends and
            # cannot be ordered against the offset-aware rows the dashboard
            # writes into this same file.
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
        append_line(  # lgtm[py/clear-text-storage-sensitive-data]
            path, json.dumps(msg), opener=owner_only_opener
        )

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
        self._maybe_rotate(path, key)


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
) -> bool:
    """Append a message only if an identical one is not already persisted.

    Returns ``True`` if the message was written, ``False`` if it is already
    on disk — judged by ``(role, content)`` when the caller supplies no
    *mid*, and by ``(role, content)`` plus the SAME ``meta.mid`` when it
    does (see below).

    The disk check and the append run together under ``_locked`` so they
    are ATOMIC against a concurrent writer of the same session file — in
    particular the periodic dashboard slot save
    (``_save_slot_to_history``), which serializes the in-memory slot window
    (already carrying this message via ``slot.append``) and takes the SAME
    per-session cross-process lock. Without this, a fire-and-forget
    :func:`append_off_loop` scheduled after the slot save has already
    written the identical message would append it a SECOND time; the
    duplicate then survives a restart and is replayed twice to subsequent
    agent turns. This is the workflow-result / cron-result double-append
    race: the read-modify-write must be one locked critical section, not a
    separate unlocked existence check followed by a later append.

    What counts as "already persisted" depends on whether the caller holds
    an identity. Without *mid*, any row with the same ``(role, content)``
    does — body equality is all an id-less writer can check. WITH *mid*,
    a same-role row carrying the SAME ``meta.mid`` AND a corroborating
    body does: equal, or equal modulo preserved images
    (:func:`same_text_modulo_images`). That row is this very message,
    landed by the slot save or an earlier attempt of this write. The
    image allowance is there because the slot save rewrites an inline
    image to its stored copy, and if the agent's scratch file is gone by
    the time this append runs, the rewrite here fails open to the
    original path — strict body equality would miss the row and append a
    duplicate under the same id naming a dead file. The corroboration
    itself stays required because ``meta.mid`` is caller-suppliable, so a
    bare id equality could pair two genuinely distinct messages. A
    body-equal row under another id (or none) is a DIFFERENT occurrence
    that happens to repeat the text — an id-carrying twin of an earlier
    injection, or a pre-id legacy row — and skipping on it would drop
    THIS occurrence's only durable copy: the in-memory window is lost on
    restart, so nothing would replay the newer message.
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
            for m in self._read_messages(key):
                if m.get("role") != role:
                    continue
                on_disk = m.get("content")
                if supplied_mid is None:
                    if on_disk == persisted:
                        return False
                    continue
                m_meta = m.get("meta")
                if not (isinstance(m_meta, dict) and m_meta.get("mid") == supplied_mid):
                    continue
                # Same id: corroborate by body, allowing for the other
                # writer having preserved an image this one could not.
                if on_disk == persisted or (
                    isinstance(on_disk, str)
                    and same_text_modulo_images(
                        on_disk,
                        persisted,
                        sessions_dir=self._path(key).parent,
                        stem=self._path(key).stem,
                    )
                ):
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
        )
        return True

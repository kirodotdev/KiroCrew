"""Copy inline chat images into per-session storage as a message is persisted.

An agent shows a picture in chat by writing ``![alt](/abs/path.png)``, and the
dashboard resolves that path off disk AT VIEW TIME (``/api/file-raw``). Nothing
owns the file the markup names: agent screenshots land in the per-process
scratch dir (:mod:`kiro_crew.agent_scratch`), which is reclaimed as soon as the
agent process dies, so the image routinely outlives its own message by hours and
the transcript then renders a missing-file chip in its place, forever. The
renderer names this as the dominant failure it sees and can only decorate it --
by the time it looks, the bytes are gone.

An image a message references is SESSION-scoped content, like an artifact or an
outbox file: it must live and die with the session's history. So at the write
boundary -- where a message's text becomes a durable transcript row -- each
referenced local image is COPIED into a directory beside the transcript and the
persisted text is rewritten to point there.

Contract, stated once because two writers share it
(:meth:`kiro_crew.history.ConversationLog.append` for agent / channel / cron
rows, ``chat_persistence._build_message_entry`` for the dashboard slot save):

* **Copy, never move.** The original stays exactly where the agent put it; the
  live in-memory message may keep naming it. Only the PERSISTED text changes.
* **Content-addressed.** The stored name carries a sha256 prefix of the bytes, so
  one image referenced by ten messages is stored once and a second reference
  costs a stat.
* **Idempotent.** A destination already inside the attachments directory is left
  alone, which is what lets a re-persist of the same row (the slot save
  re-serializes its whole window on every flush) run without re-copying and lets
  the two writers compose in either order.
* **Fail-open, per image.** Anything unreadable, oversized, symlinked, or of an
  unexpected type keeps its original markup and logs at debug. A picture that
  cannot be preserved must never cost the message its text.
* **Bounded per message.** Both writers copy while holding the per-session lock,
  so one row's work has a ceiling on count and on total bytes -- see
  :data:`MAX_IMAGES_PER_MESSAGE`.
* **Local only.** ``http(s)``, ``data:`` and protocol-relative destinations are
  never touched -- they are already durable and are not ours to copy.

The SECURITY boundary on the read is one call to
:func:`kiro_crew.hooks.safe_read_file_bytes_nolink`, the house chokepoint for
"read a file an untrusted string named". It opens the final component AS ITSELF on
every platform (``FILE_FLAG_OPEN_REPARSE_POINT`` where there is no
``O_NOFOLLOW``), then validates the descriptor it actually opened -- regular file,
not hardlinked, not sensitive -- so no check-to-use window remains. Nothing here
re-decides any of that, and the copy is written from the bytes that call returned.

The ``os.lstat`` ahead of it is CLASSIFICATION, not a second gate, and the
distinction is worth stating because the two look alike. The chokepoint canonicalizes
with ``realpath`` before it opens, so a destination that IS a symlink is read as its
target; refusing one is therefore a policy choice -- an attachment records a file, and
a link is a reference to someone else's, whose target the markup could have named
outright anyway. Being a lexical pre-check it is inherently racy, and that costs
nothing: losing the race yields a read the chokepoint still fully validates.

The attachments directory sits under the crew data home's ``sessions/``
directory, which is WRITE-protected but deliberately not read-sensitive (see
``security.paths._WRITE_PROTECTED_HOME_PATHS``) -- so ``/api/file-raw`` serves an
attachment with no change to its sensitive-path policy, and the gateway's own
persistence writes there through direct calls that never route through the agent
file-edit gate.

Reclamation is DELETE-ONLY, deliberately. Transcript rotation moves old rows to
``archive/`` (:func:`kiro_crew.history._archive_lines`) and those rows still name
their attachments, so rotation orphans nothing and must not sweep -- a sweep
against the live transcript alone would break exactly the references the archive
keeps. An attachment becomes genuinely unreferenced only when archive retention
expires its last row, and what survives until session delete is bounded by
content-addressing plus the per-image ceiling. A sweep keyed on the whole
transcript-plus-archive chain is a reachability pass over a session's entire
history; that is not this change's job, and it buys back disk that is already
bounded.

Everything here is blocking file I/O; callers on the event loop must offload it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.messaging.outbound_files import LocalRef, iter_local_refs, local_destination

logger = logging.getLogger(__name__)

#: Directory suffix appended to a transcript's stem. A sibling of the ``.jsonl``
#: rather than a child of it, because history is a FLAT file per session: there is
#: no session directory to put this inside. Paired with the transcript by stem,
#: which is what lets the delete path reclaim both.
ATTACHMENTS_DIR_SUFFIX = ".attachments"

#: Per-image ceiling. Session history is a conversation log, not a media store;
#: a reference to something larger keeps its original path (and its original
#: fragility) rather than growing the session by an unbounded amount.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: Per-MESSAGE copy budgets. The per-image ceiling alone bounds nothing that
#: matters here: one row may reference a hundred distinct images, and this work
#: happens inside the per-session lock, so an unbounded row holds that lock for as
#: long as its copies take -- and ``ConversationLog._locked`` makes a single
#: non-blocking acquire on the event loop, so a slow holder turns the dashboard's
#: own slot save into a ``HistoryLockTimeout`` rather than a wait. Whichever limit
#: trips first stops preserving further images in that row; the rest keep their
#: original markup, the ordinary fail-open outcome.
#:
#: Same policy and same numbers as ``image_artifacts.MAX_IMAGES_PER_MESSAGE`` /
#: ``MAX_IMAGE_BYTES_PER_MESSAGE``, which bound the sibling copy of the same bytes
#: for the same reason. Restated rather than imported: that module reaches the
#: artifact store, and this one is imported by ``history``, so importing it would
#: put the store on the transcript writer's graph.
MAX_IMAGES_PER_MESSAGE = 12
MAX_BYTES_PER_MESSAGE = 64 * 1024 * 1024

#: Extensions we preserve, matching the set ``/api/file-raw`` will actually serve
#: (``files._ALLOWED_IMAGE_EXT``). Copying bytes the viewer would then refuse
#: would spend the disk and still show a broken image.
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})

#: Bytes of the digest kept in the filename. 16 hex chars is 64 bits -- ample for
#: naming files inside one session, and short enough that the stored name still
#: shows the original basename to a human reading the directory.
_DIGEST_CHARS = 16

#: Original basenames are LLM-authored text joined onto a path, so only this
#: alphabet survives into a filename.
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Longest basename kept after the digest prefix.
_MAX_NAME_CHARS = 64

#: A markdown destination cannot carry these bare, so a rewritten path holding
#: one is wrapped in angle brackets (a home directory with a space in it is the
#: realistic case).
_DEST_NEEDS_ANGLES = frozenset(" \t()<>")


def attachments_dir(sessions_dir: Path, stem: str) -> Path:
    """The attachments directory belonging to the transcript named *stem*."""
    return sessions_dir / f"{stem}{ATTACHMENTS_DIR_SUFFIX}"


def persist_inline_images(content: str, *, sessions_dir: Path, stem: str) -> str:
    """*content* with every preservable local image reference pointing at storage.

    Returns the text unchanged when there is nothing to do -- no references, or
    none that qualify -- so a caller can assign the result unconditionally.
    Never raises: the message must persist even when no image could be copied.

    Bounded by :data:`MAX_IMAGES_PER_MESSAGE` and :data:`MAX_BYTES_PER_MESSAGE`,
    because the caller holds the session lock while this runs.
    """
    # The overwhelming majority of rows carry no image at all, and this runs on
    # every persisted row (the slot save re-serializes its whole window per
    # flush). A substring test keeps that path free of the reference scan.
    if not content or "![" not in content:
        return content
    try:
        refs = iter_local_refs(content)
    except Exception:  # pragma: no cover - defensive: scan must never break a write
        logger.debug("chat-attachments: reference scan failed", exc_info=True)
        return content
    if not refs:
        return content

    target_dir = attachments_dir(sessions_dir, stem)

    # Two passes. The budget is spent in READING order, so which images a row over
    # its limits keeps is the order a person would expect; the rewrite then runs
    # right-to-left so an earlier reference's span stays valid after a later one
    # has been replaced.
    planned: list[tuple[LocalRef, str]] = []
    copies = 0
    budget = MAX_BYTES_PER_MESSAGE
    for ref in refs:
        if copies >= MAX_IMAGES_PER_MESSAGE:
            break
        try:
            result = _store_one(ref.dest, target_dir, budget)
        except Exception:
            logger.debug("chat-attachments: could not preserve an image reference", exc_info=True)
            continue
        if result is None:
            continue
        stored, consumed = result
        # A repeat of an image already stored consumes nothing, so a row showing
        # one picture ten times is not charged ten times for it.
        if consumed:
            budget -= consumed
            copies += 1
        planned.append((ref, stored))

    out = content
    for ref, stored in reversed(planned):
        markup = out[ref.start : ref.end]
        rewritten = _rewrite_destination(markup, ref.dest, stored)
        if rewritten is None:
            continue
        out = out[: ref.start] + rewritten + out[ref.end :]
    return out


def remove_attachments(sessions_dir: Path, stem: str) -> bool:
    """Whether *stem* has no attachments left, removing them if it does.

    The return answers the question a deleter needs -- "is this session's image
    content gone?" -- not "did I unlink something". ``True`` for a session that
    never had any, ``False`` while anything remains, so the caller can fail closed
    on a residue instead of reporting a delete it did not complete. Logged at
    WARNING for the same reason: a leftover is served content, not a stale cache.

    Only direct children are unlinked and the directory must be a real directory,
    never a link, so a planted link cannot redirect the removal. A subdirectory is
    deliberately not recursed into and therefore reports ``False``: something this
    code did not write put it there, and a human should look.
    """
    target = attachments_dir(sessions_dir, stem)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return True  # nothing to reclaim
    except OSError:
        logger.warning("chat-attachments: cannot stat %r", str(target), exc_info=True)
        return False
    if not stat.S_ISDIR(info.st_mode):
        logger.warning("chat-attachments: %r is not a directory, leaving it", str(target))
        return False
    try:
        entries = list(os.scandir(target))
    except OSError:
        logger.warning("chat-attachments: could not list %r", str(target), exc_info=True)
        return False
    for entry in entries:
        try:
            os.unlink(entry.path)
        except OSError:
            logger.warning(
                "chat-attachments: could not remove %r from %r",
                entry.name,
                str(target),
                exc_info=True,
            )
            return False
    try:
        os.rmdir(target)
    except OSError:
        logger.warning("chat-attachments: could not remove %r", str(target), exc_info=True)
        return False
    return True


def _store_one(raw_dest: str, target_dir: Path, budget_bytes: int) -> tuple[str, int] | None:
    """Copy the image *raw_dest* names into *target_dir*.

    Returns its new path and the bytes that copy consumed of the message budget --
    zero when the bytes were already stored, since content-addressing means a
    repeat costs no disk.

    ``None`` means "leave the reference alone", for every reason: not a local
    absolute path, already stored, not an image extension, not a regular file,
    larger than the remaining message budget, or refused by the read chokepoint
    (unreadable, hardlinked, non-regular, sensitive, or over the per-image
    ceiling). An over-budget image is SKIPPED rather than ending the row, so one
    large picture does not cost the smaller ones after it their durability.
    """
    source = local_destination(raw_dest)
    if source is None:
        return None
    if source.suffix.lower() not in _IMAGE_EXTENSIONS:
        return None
    # Already ours: the row is being re-persisted (a slot re-flush, or the second
    # of the two writers). Re-copying would content-address the same bytes to the
    # same name, so this is an optimisation AND the property that makes the
    # rewrite idempotent.
    if _same_dir(source.parent, target_dir):
        return None
    # Classification only (see the module docstring): an attachment records a
    # file, so a destination that is a link, directory or device is left as
    # written rather than resolved. Every safety decision is the chokepoint's.
    try:
        if not stat.S_ISREG(os.lstat(source).st_mode):
            return None
    except OSError:
        return None  # missing, or a path the OS refuses

    # One call rather than a size/sensitive/nofollow triage of our own: this
    # chokepoint validates the descriptor it opened, so it holds on Windows too
    # (where ``O_NOFOLLOW`` does not exist) and adds the hardlink refusal a
    # path-based check cannot make. Oversize RAISES rather than returning None.
    try:
        data = safe_read_file_bytes_nolink(str(source), max_bytes=MAX_ATTACHMENT_BYTES)
    except FileTooLargeError:
        return None
    if data is None:
        return None

    digest = hashlib.sha256(data).hexdigest()[:_DIGEST_CHARS]
    dest = target_dir / f"{digest}-{_safe_name(source.name)}"
    if dest.exists():
        # Content-addressed: a file already under this name holds these bytes.
        return str(dest), 0
    if len(data) > budget_bytes:
        return None
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(dest, data)
    return str(dest), len(data)


def _same_dir(left: Path, right: Path) -> bool:
    """Whether two directory paths name the same place, case-insensitively."""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _safe_name(name: str) -> str:
    """A filename-safe, length-bounded form of an LLM-authored basename."""
    safe = _UNSAFE_NAME_RE.sub("_", name).lstrip(".")[:_MAX_NAME_CHARS].strip("._")
    return safe or "image"


def _rewrite_destination(markup: str, raw_dest: str, new_dest: str) -> str | None:
    """*markup* with its destination replaced by *new_dest*, or ``None``.

    Replaces the destination IN PLACE rather than rebuilding ``![alt](dest)``,
    because the alt text carries its own escaping and reconstructing it would
    have to re-derive rules the scanner already applied. ``rfind`` is deliberate:
    when the alt text happens to repeat the destination, the LAST occurrence
    inside the markup is the destination.
    """
    at = markup.rfind(raw_dest)
    if at < 0:  # pragma: no cover - the scanner read dest out of this very span
        return None
    angle_wrapped = at > 0 and markup[at - 1] == "<"
    encoded = _encode_destination(new_dest, angle_wrapped=angle_wrapped)
    return markup[:at] + encoded + markup[at + len(raw_dest) :]


def _encode_destination(new_dest: str, *, angle_wrapped: bool) -> str:
    """*new_dest* in a form a markdown destination can hold.

    Angle brackets are escaped either way; the wrapping is added only when the
    original had none and the path holds something a bare destination cannot
    carry. The path's own separators are left alone -- a Windows path's
    backslashes precede path characters, which markdown treats literally.
    """
    escaped = new_dest.replace("<", "\\<").replace(">", "\\>")
    if angle_wrapped:
        return escaped
    if any(char in _DEST_NEEDS_ANGLES for char in new_dest):
        return f"<{escaped}>"
    return escaped

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

* **Copy, never move.** The original stays exactly where the agent put it. The
  caller decides what its own live row names afterwards (the dashboard slot save
  commits the rewritten destination into the row, see there); this function only
  returns the rewritten text.
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
expires its last row. The ceilings here are PER MESSAGE; across messages a
session grows with every distinct image until it is deleted (a per-session
ceiling or a retention-coupled sweep is a tracked follow-up). Session delete
takes the directory in three all-or-nothing steps -- rename it aside, unlink the
transcript, purge the staged copy -- so a retained transcript never points at
pictures already gone (see :func:`stage_attachments_removal`).

Everything here is blocking file I/O; callers on the event loop must offload it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass, field
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
#: Most ``![`` openers a row may hold and still be scanned. The reference scanner
#: restarts at every opener, so a run of unclosed ones costs it time quadratic in
#: their count (20 000 of them: tens of seconds) -- and this runs under the
#: session lock, on LLM-authored text. A real message never approaches this; one
#: that does keeps its markup untouched, the ordinary fail-open outcome.
MAX_IMAGE_OPENERS_PER_MESSAGE = 256
#: Infix of a staged-for-removal attachments directory; never a served name.
_STAGED_SUFFIX = "trash"
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


@dataclass
class ImageBudget:
    """What one MESSAGE may still spend on images, across every text it carries.

    A message is persisted as its content plus any number of variants (the
    alternate replies a regeneration keeps). They are one row under one session
    lock, so they share ONE cap on distinct destinations and ONE byte budget --
    a fresh budget per text would let a message with N variants copy N times the
    ceiling while the lock is held. ``seen`` is shared for the same reason: a
    variant repeating the primary reply's picture is a repeat, not a new read.
    """

    copies_left: int = field(default_factory=lambda: MAX_IMAGES_PER_MESSAGE)
    bytes_left: int = field(default_factory=lambda: MAX_BYTES_PER_MESSAGE)
    seen: dict[str, tuple[str, int] | None] = field(default_factory=dict)


def persist_inline_images(
    content: str, *, sessions_dir: Path, stem: str, budget: ImageBudget | None = None
) -> str:
    """*content* with every preservable local image reference pointing at storage.

    Returns the text unchanged when there is nothing to do -- no references, or
    none that qualify -- so a caller can assign the result unconditionally.
    Never raises: the message must persist even when no image could be copied.

    Bounded by *budget* (:class:`ImageBudget`; a fresh per-message one when
    omitted), because the caller holds the session lock while this runs. A
    caller persisting several texts of ONE message passes the same budget to
    each.
    """
    # The overwhelming majority of rows carry no image at all, and this runs on
    # every persisted row (the slot save re-serializes its whole window per
    # flush). A substring test keeps that path free of the reference scan.
    if not content or "![" not in content:
        return content
    if content.count("![") > MAX_IMAGE_OPENERS_PER_MESSAGE:
        logger.debug("chat-attachments: too many image openers in one row; left as written")
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
    if budget is None:
        budget = ImageBudget()
    # One read per distinct destination per message: ``_store_one`` hashes the
    # whole file before it can know the bytes are already stored, so a message
    # repeating one reference must not pay that read once per repeat.
    seen = budget.seen
    for ref in refs:
        # A repeat costs no read, so it is answered even once the cap is reached:
        # the cap bounds distinct destinations, and a repeat left unrewritten
        # would keep naming the scratch file while its earlier twin names storage.
        if ref.dest in seen:
            result = seen[ref.dest]
            if result is not None:
                planned.append((ref, result[0]))
            continue
        if budget.copies_left <= 0:
            continue  # over the cap: new destinations skipped, repeats still answered
        # The image cap bounds the WORK this row does under the session lock, so
        # every distinct destination is charged before it is looked at -- one
        # that turns out oversize, missing or already stored included. The byte
        # budget bounds disk, so only a copy that wrote bytes is charged there.
        budget.copies_left -= 1
        try:
            result = _store_one(ref.dest, target_dir, budget.bytes_left)
        except Exception:
            logger.debug("chat-attachments: could not preserve an image reference", exc_info=True)
            seen[ref.dest] = None
            continue
        if result is None:
            seen[ref.dest] = None
            continue
        stored, consumed = result
        seen[ref.dest] = (stored, 0)
        budget.bytes_left -= consumed
        planned.append((ref, stored))

    out = content
    for ref, stored in reversed(planned):
        markup = out[ref.start : ref.end]
        rewritten = _rewrite_destination(markup, stored)
        if rewritten is None:
            continue
        out = out[: ref.start] + rewritten + out[ref.end :]
    return out


#: The digest prefix a stored attachment's basename carries.
_STORED_NAME_RE = re.compile(rf"^[0-9a-f]{{{_DIGEST_CHARS}}}-(.+)$")

#: Stands in for a destination when two texts are compared modulo images.
_DEST_PLACEHOLDER = "\x00"


def same_text_modulo_images(left: str, right: str, *, sessions_dir: Path, stem: str) -> bool:
    """Whether *left* and *right* are one text whose images were preserved.

    True when the two are equal, or differ ONLY in local image destinations and at
    least one of those already points into this transcript's attachments
    directory. This is the corroboration an id-matched pair of rows needs when
    the two write boundaries met the same message at different times: one landed
    it with an image rewritten to its stored copy, the other holds the text as
    the agent wrote it -- and if the agent's scratch file is gone by then, its
    own rewrite fails open, so the bodies disagree although the message is one.
    Comparing the destinations by the stored copy's own naming (the digest
    prefix dropped, the basename made filename-safe the same way) lets the pair
    agree without the vanished bytes.

    Never raises; an unscannable text simply does not corroborate.
    """
    if left == right:
        return True
    if "![" not in left and "![" not in right:
        return False
    if max(left.count("!["), right.count("![")) > MAX_IMAGE_OPENERS_PER_MESSAGE:
        return False  # neither side would have been rewritten (see the bound)
    target_dir = attachments_dir(sessions_dir, stem)
    try:
        left_norm, left_stored = _normalise_image_destinations(left, target_dir)
        right_norm, right_stored = _normalise_image_destinations(right, target_dir)
    except Exception:  # pragma: no cover - defensive: a comparison must never break a write
        logger.debug("chat-attachments: destination normalisation failed", exc_info=True)
        return False
    return (left_stored or right_stored) and left_norm == right_norm


def _normalise_image_destinations(text: str, target_dir: Path) -> tuple[str, bool]:
    """*text* with each local image destination replaced by its storage name.

    Returns the normalised text and whether any destination already lay inside
    *target_dir*. A destination outside it is named as ``_store_one`` WOULD name
    it (``_safe_name`` of the basename); one inside it has its digest prefix
    dropped, which yields the same token. Angle wrapping is dropped with the
    destination, so the two encodings of one path compare equal.
    """
    refs = iter_local_refs(text)
    stored_seen = False
    out = text
    for ref in reversed(refs):
        source = local_destination(ref.dest)
        if source is None:
            continue
        markup = out[ref.start : ref.end]
        span = _destination_span(markup)
        if span is None:
            continue
        at, until, angle_wrapped = span
        if angle_wrapped:
            at, until = at - 1, until + 1
        if _same_dir(source.parent, target_dir):
            stored_seen = True
            matched = _STORED_NAME_RE.match(source.name)
            token = matched.group(1) if matched else source.name
        else:
            token = _safe_name(source.name)
        out = (
            out[: ref.start + at]
            + _DEST_PLACEHOLDER
            + token
            + _DEST_PLACEHOLDER
            + out[ref.start + until :]
        )
    return out, stored_seen


def stage_attachments_removal(sessions_dir: Path, stem: str) -> Path | None:
    """Move *stem*'s attachments directory aside in ONE rename, so a delete is
    all-or-nothing from the transcript's point of view.

    Returns the staged path, or ``None`` when the session has no attachments.
    Raises :class:`OSError` when the directory cannot be moved -- and then
    NOTHING has changed, so the caller can abort with the transcript and its
    images both intact. Deleting image files one by one before the transcript is
    what this replaces: any per-file failure part-way (a foreign entry, a locked
    file on Windows) left a retained transcript pointing at pictures already
    gone, which is the exact breakage this module exists to prevent.

    The directory must be a real directory, never a link, so a planted link
    cannot redirect what gets moved. The staged name lives beside the original
    under ``sessions/`` (same filesystem, so the rename is atomic) and is never a
    name this module resolves references to, so nothing serves it.
    """
    target = attachments_dir(sessions_dir, stem)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{target} is not a directory")
    staged = target.with_name(f"{target.name}.{_STAGED_SUFFIX}-{secrets.token_hex(4)}")
    os.rename(target, staged)
    return staged


def restore_staged_attachments(staged: Path, sessions_dir: Path, stem: str) -> None:
    """Undo :func:`stage_attachments_removal` after the transcript could not be
    deleted, so the retained transcript's references resolve again. Best effort:
    a failure is logged, and the staged directory is then the residue a human
    should look at."""
    try:
        os.rename(staged, attachments_dir(sessions_dir, stem))
    except OSError:
        logger.warning("chat-attachments: could not restore %r", str(staged), exc_info=True)


def purge_staged_attachments(staged: Path) -> bool:
    """Delete a staged attachments directory; ``True`` when nothing is left.

    Runs AFTER the transcript is gone, so nothing references these bytes any
    more and a leftover is an orphan rather than served content. ``False`` is
    logged at WARNING with the path, so an operator can finish by hand (the
    Windows case: a file still open in a viewer cannot be unlinked).
    """
    shutil.rmtree(staged, ignore_errors=True)
    try:
        os.lstat(staged)
    except FileNotFoundError:
        return True
    except OSError:
        pass
    logger.warning("chat-attachments: orphaned images remain at %r", str(staged))
    return False


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


def _rewrite_destination(markup: str, new_dest: str) -> str | None:
    """*markup* with its destination replaced by *new_dest*, or ``None``.

    Replaces the destination IN PLACE rather than rebuilding ``![alt](dest)``,
    because the alt text carries its own escaping and reconstructing it would
    have to re-derive rules the scanner already applied. The destination is
    located as a SPAN of the raw markup -- never by searching for the parsed
    destination text: the scanner unescapes (``shot\\(1\\).png`` parses to
    ``shot(1).png``), and the alt text before or a ``"title"`` after may repeat
    the path, so a text search finds the wrong thing or nothing and the persisted
    reference keeps naming the scratch file.
    """
    span = _destination_span(markup)
    if span is None:  # pragma: no cover - the scanner matched this very span
        return None
    at, until, angle_wrapped = span
    encoded = _encode_destination(new_dest, angle_wrapped=angle_wrapped)
    return markup[:at] + encoded + markup[until:]


def _destination_span(markup: str) -> tuple[int, int, bool] | None:
    """``(start, end, angle_wrapped)`` of the raw destination inside *markup*.

    Mirrors the scanner's ``_finish_destination`` on the RAW text: after the
    ``](`` and any leading whitespace, an ``<`` opens an angle-wrapped destination
    that ends at the first ``>``; a bare one ends at the first whitespace (where
    an optional title begins) or at the closing paren. Neither terminator is
    produced or removed by markdown unescaping, so the raw and parsed views agree
    on where the destination ends.
    """
    at = _after_alt_text(markup)
    if at is None or not markup.endswith(")"):
        return None
    limit = len(markup) - 1  # the closing paren the scanner walked to
    while at < limit and markup[at] in " \t":
        at += 1
    if at < limit and markup[at] == "<":
        close = markup.find(">", at + 1, limit)
        if close < 0:
            return None
        return at + 1, close, True
    until = at
    while until < limit and markup[until] not in " \t":
        until += 1
    if until == at:
        return None
    return at, until, False


def _after_alt_text(markup: str) -> int | None:
    """Index just past the ``](`` that closes an image's alt text, or ``None``.

    One left-to-right pass mirroring the scanner's alt-text rule (a backslash
    escapes the next character; the first unescaped ``]`` ends the alt). A plain
    walk rather than the scanner's regex: *markup* is LLM-authored text, and a
    regex with a repeated group is what a scanner flags for polynomial
    backtracking on a long run of ``![[`` -- the walk is linear by construction.
    """
    if not markup.startswith("!["):
        return None
    i = 2
    n = len(markup)
    while i < n:
        c = markup[i]
        if c == "\\":
            i += 2
            continue
        if c == "]":
            return i + 2 if markup.startswith("](", i) else None
        i += 1
    return None


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

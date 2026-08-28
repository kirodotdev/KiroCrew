"""Repair kiro-cli session transcripts that an oversized image has wedged.

A provider rejects the ENTIRE request when a many-image conversation carries any
image wider or taller than :data:`kiro_crew.imaging.MAX_IMAGE_EDGE_PX`::

    messages.60.content.0.image.source.base64.data: At least one of the image
    dimensions exceed max allowed size for many-image requests: 2000 pixels

kiro-cli replays the whole message history every turn, so the offending block
sits at a fixed history index that nothing evicts. The session does not fail at
the turn that stored the image; it fails on every turn from the moment enough
images accumulate for the request to count as many-image. Capping new captures
therefore cannot heal a transcript that already carries one -- the stored bytes
have to be rewritten, or the conversation is over.

Why this is a user-invoked command and not a sweeper
----------------------------------------------------
The files belong to kiro-cli, not to Kiro Crew, and automatically rewriting
another tool's data directory was ruled out. Healing existing damage is an
explicit action with a visible dry run and a backup sidecar, so the operator
decides when a transcript is safe to touch. Run it only while the session is
idle: kiro-cli appends to these files, and an append that lands during the
rewrite is lost.

What a repair does to a block it cannot fix
-------------------------------------------
:func:`kiro_crew.imaging.downscale_image_block` fails closed -- it returns
``None`` when no rendition fits the budget. Leaving such a block in place keeps
the session dead, so it is replaced by a text block naming the drop. Losing one
image is strictly better than losing every later turn.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.imaging import (
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
    image_dimensions,
)

logger = logging.getLogger(__name__)

#: Marker text substituted for an image block that has no compliant rendition.
DROPPED_PLACEHOLDER = (
    "[image dropped by session repair: no rendition fits the " "{max_edge}px inline-image cap]"
)


@dataclass
class BlockFinding:
    """One stored image block and what the repair decided to do with it."""

    line_no: int
    width: int
    height: int
    raw_bytes: int
    action: str  # "keep" | "resize" | "drop"
    new_size: tuple[int, int] | None = None


class BackupExistsError(RuntimeError):
    """A ``.pre-image-repair.bak`` sidecar is already there.

    The sidecar's value is that it holds the transcript as it was BEFORE any
    repair, which is the only place a dropped image's bytes survive. Writing it
    unconditionally destroys exactly that: a second ``--apply`` would replace the
    true pre-repair copy with content that has already been rewritten, and if the
    first run dropped an uncappable block those bytes then exist nowhere at all.

    The refusal also covers the uglier ordering -- the backup is written before
    the final movement check, so an append arriving mid-write would clobber the
    old sidecar and THEN refuse the repair, losing the backup for a write that
    never happened.

    Deliberately not a check-then-clobber with a nicer message: the first backup
    is the one worth keeping, so the operator is told to move it aside and decide.
    """


class TranscriptLiveError(RuntimeError):
    """A live kiro-cli process is still writing this transcript.

    Refusing here is the difference between a theoretical race and the one that
    actually loses data: an operator running ``--apply`` on a session that never
    stopped. The residual stat-to-rename window cannot be closed, but this
    removes the condition under which it is likely to be hit at all.
    """


class TranscriptMovedError(RuntimeError):
    """The transcript changed on disk between the scan and the write.

    kiro-cli appends to these files. A repair rewrites the whole file from the
    lines the scan produced, so an append that lands in between would be
    silently discarded -- the operator would see a successful repair and a lost
    turn. Refusing is the only honest outcome; the scan is cheap to redo.
    """


@dataclass
class FileReport:
    """Per-transcript outcome, printable in both dry-run and apply mode."""

    path: Path
    images: int = 0
    findings: list[BlockFinding] = field(default_factory=list)
    rewritten_lines: int = 0
    error: str | None = None
    #: ``(size, mtime_ns)`` as of the scan, or ``None`` when it could not be
    #: read. Passed back into :func:`apply_repair` so the write can refuse a
    #: transcript that moved underneath it.
    source_stat: tuple[int, int] | None = None
    #: Image-shaped blocks the generic walk saw and the anchored traversal did
    #: not. Non-zero means the traversal's shape set may have drifted behind
    #: kiro-cli, so a clean report cannot be trusted. Never repaired -- only
    #: reported, loudly, because the alternative failure is a silent no-op.
    unanchored_images: int = 0
    #: Line numbers where that divergence was seen, so it can be inspected.
    unanchored_lines: list[int] = field(default_factory=list)
    #: True when the tripwire walk hit its depth ceiling and could not finish.
    #: Reported, never swallowed: an unverified subtree must not read as clean.
    walk_truncated: bool = False

    @property
    def oversized(self) -> list[BlockFinding]:
        return [f for f in self.findings if f.action != "keep"]

    @property
    def changed(self) -> bool:
        return bool(self.oversized)


def _block_list(node: dict) -> list:
    """The block array of a record, a snapshot message, or a container block.

    Two spellings, both observed: a record and a container block put their
    blocks at ``data.content``, while a message inside a compaction snapshot
    puts them at a top-level ``content``. Exactly one is present on any given
    node in every transcript surveyed, so the fallback cannot double-count.
    """
    data = node.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, list):
            return content
    content = node.get("content")
    return content if isinstance(content, list) else []


def _snapshot_messages(node: dict) -> list:
    """The message list a ``Compaction`` record carries, if any.

    Compaction is not a footnote: across the surveyed transcripts its snapshot
    holds 42% of all stored image blocks (5314 of 12615), more than
    ``ToolResults`` and nearly as many as ``Prompt``. A traversal that skips it
    reports a clean transcript while leaving a live oversized image in place --
    the tool would return success and change nothing, and the session would stay
    dead.
    """
    data = node.get("data")
    if not isinstance(data, dict):
        return []
    snapshot = data.get("messages_snapshot")
    return snapshot if isinstance(snapshot, list) else []


def _iter_image_blocks(node: Any) -> Iterator[dict]:
    """Yield the transcript image blocks reachable from a record.

    Traversal is ANCHORED to the block arrays rather than walking every dict in
    the record. That restriction is the point: a generic walk also descends into
    the *payload* of a text or json block, and a tool whose output happens to
    contain a dict shaped like an image block would then be rewritten as if it
    were transcript media. This module writes another tool's data file, so a
    false positive is corruption of application data.

    The four shapes below are the complete set found across 16253 transcripts,
    and the traversal is built from that survey rather than from the two shapes
    that happened to appear in the transcripts under repair::

        Prompt       .data.content[i]
        ToolResults  .data.content[i].data.content[i]
        Compaction   .data.messages_snapshot[i].content[i]
        Compaction   .data.messages_snapshot[i].content[i].data.content[i]

    Anchoring buys safety in one direction and costs coverage in the other, so
    it is only sound while the shape set is known to be complete: a future
    record kind that nests its blocks somewhere new would be MISSED, and a miss
    here is not a benign no-op -- the report would say nothing was over cap
    while the transcript stayed wedged. That is why the survey above is part of
    the contract and why the tests pin all four shapes.
    """
    if not isinstance(node, dict):
        return
    for message in _snapshot_messages(node):
        if isinstance(message, dict):
            yield from _iter_blocks_of(message)
    yield from _iter_blocks_of(node)


def _iter_blocks_of(node: dict) -> Iterator[dict]:
    """Yield image blocks from one node's block array, descending containers."""
    for block in _block_list(node):
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "image":
            yield block
        elif kind == "toolResult":
            # The one container kind real transcripts show: its own block array
            # sits a level deeper. Spelled inline rather than held in a set --
            # there has only ever been one.
            yield from _iter_blocks_of(block)


#: The only payload spelling kiro-cli has been observed to write, verified
#: against real transcripts::
#:
#:     {"kind": "image",
#:      "data": {"format": "png",
#:               "source": {"kind": "bytes", "data": [137, 80, 78, 71, ...]}}}
#:
#: The inner ``kind`` names the payload encoding, so it is checked rather than
#: assumed: a future ``{"kind": "path"}`` source would carry a filename in
#: ``data``, and coercing that through ``bytes()`` would corrupt the record.
_SOURCE_BYTES_KIND = "bytes"


def _block_bytes(block: dict) -> bytes | None:
    """Decode a stored image block's payload, or ``None`` if it has none."""
    source = _block_source(block)
    if source is None:
        return None
    raw = source.get("data")
    if not isinstance(raw, list):
        return None
    try:
        return bytes(raw)
    except (TypeError, ValueError):
        return None


def _block_source(block: dict) -> dict | None:
    """Return the byte-payload source dict of *block*, or ``None``."""
    data = block.get("data")
    if not isinstance(data, dict):
        return None
    source = data.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("kind") != _SOURCE_BYTES_KIND:
        return None
    return source


def _mime_of(block: dict) -> str:
    """Resolve the block's mime type from its bare ``format`` tag ("png")."""
    data = block.get("data")
    if isinstance(data, dict):
        fmt = data.get("format")
        if isinstance(fmt, str) and fmt:
            return fmt if "/" in fmt else f"image/{fmt}"
    return "image/png"


def _rewrite_block(block: dict, payload: bytes, max_edge: int) -> BlockFinding | None:
    """Cap one block in place. Returns the finding, or ``None`` if not an image."""
    dims = image_dimensions(payload)
    if dims is None:
        return None
    width, height = dims
    if max(width, height) <= max_edge:
        return BlockFinding(0, width, height, len(payload), "keep")

    fitted = downscale_image_block(payload, _mime_of(block), max_edge=max_edge)
    if fitted is None:
        # Fail-closed from the imaging layer. Keeping the block keeps the
        # session dead, so drop the payload and leave a readable trace.
        block.clear()
        block["kind"] = "text"
        block["data"] = DROPPED_PLACEHOLDER.format(max_edge=max_edge)
        return BlockFinding(0, width, height, len(payload), "drop")

    new_bytes, new_mime = fitted
    source = _block_source(block)
    if source is None:  # pragma: no cover - payload was read through it
        return BlockFinding(0, width, height, len(payload), "keep")
    source["data"] = list(new_bytes)
    data = block["data"]
    if isinstance(data.get("format"), str):
        data["format"] = new_mime.split("/", 1)[-1]
    new_dims = image_dimensions(new_bytes) or (0, 0)
    return BlockFinding(0, width, height, len(payload), "resize", new_dims)


#: Depth ceiling for the tripwire walk. Real transcripts nest about four levels
#: (record -> block array -> container block -> its block array), so this is
#: orders of magnitude of headroom -- it exists only to keep an arbitrarily
#: nested tool payload from exhausting the interpreter stack, which would abort
#: the repair of a transcript the operator needs repaired.
_MAX_WALK_DEPTH = 200


def _generic_image_blocks(node: Any, depth: int = 0) -> tuple[list[dict], bool]:
    """Every image-shaped dict anywhere under *node*, for DETECTION ONLY.

    Returns ``(blocks, truncated)`` where *truncated* means the walk hit
    :data:`_MAX_WALK_DEPTH` and could not finish -- which is reported as a
    divergence rather than swallowed, because the tripwire's whole purpose is
    that it never lets a report read clean when verification did not happen.

    The anchored traversal is deliberately narrow, and its failure direction is
    the dangerous one: a kiro-cli format change it does not know about makes the
    repair report a clean transcript while the session stays wedged -- success
    indistinguishable from failure, on the one command a wedged user is told to
    run. This PR nearly shipped exactly that, having missed the ``Compaction``
    shape that holds 42% of real image blocks.

    So the generic walk runs alongside as a tripwire. Its result is NEVER
    rewritten -- descending into a text or json payload is precisely why the
    anchored form exists -- it is only counted, so a divergence can be reported.
    A false alarm from a tool payload that happens to look like an image block is
    harmless noise; a silent miss is not, which is the whole reason the check is
    allowed to be imprecise in this direction and no other.

    Iteration is depth-bounded rather than trusting the recursion limit: this
    walk descends into arbitrary TOOL PAYLOADS, whose nesting is not this
    module's data and has no natural bound.
    """
    if depth > _MAX_WALK_DEPTH:
        return [], True
    found: list[dict] = []
    truncated = False
    if isinstance(node, dict):
        if node.get("kind") == "image":
            return [node], False
        children: Any = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return [], False
    for value in children:
        sub, sub_truncated = _generic_image_blocks(value, depth + 1)
        found.extend(sub)
        truncated = truncated or sub_truncated
    return found, truncated


def scan_file(path: Path) -> tuple[FileReport, list[bytes] | None]:
    """Inspect one transcript; return its report and the repaired lines.

    The repaired-lines list is ``None`` when nothing needs rewriting, so a
    caller can skip the write entirely rather than rewriting a file to the same
    content -- a no-op rewrite still risks losing a concurrent append.
    """
    report = FileReport(path=path)
    try:
        st = path.stat()
        report.source_stat = (st.st_size, st.st_mtime_ns)
    except OSError:
        report.source_stat = None
    out_lines: list[bytes] = []
    dirty = False

    try:
        with path.open("rb") as handle:
            for line_no, raw_line in enumerate(handle):
                stripped = raw_line.rstrip(b"\n")
                # A CRLF transcript keeps its \r here, and passthrough lines are
                # therefore byte-exact on either convention. Captured so a
                # REWRITTEN line can be given the same terminator back -- writing
                # a bare \n into an otherwise CRLF file would leave the repair as
                # the one line with a different ending.
                eol = b"\r" if stripped.endswith(b"\r") else b""
                # Read as BYTES and pass untouched lines through verbatim. A
                # text-mode read with errors="replace" would substitute U+FFFD
                # for any byte sequence that is not valid UTF-8, and because
                # every line is rewritten from what was read, one malformed
                # sequence anywhere in the file would be irreversibly destroyed
                # on apply -- in a line this tool has no business touching, in
                # another tool's data file. Strict decoding would instead refuse
                # the whole transcript and leave the session dead, so neither
                # text mode is right: only the lines actually being parsed need
                # to be text.
                #
                # Prefilter on the LOOSE token so the drift cross-check below can
                # see lines the strict spelling would skip. Decoding every record
                # of a 29MB transcript is the slow way round, but gating on
                # b'"image"' rather than b'"kind":"image"' costs little and means
                # a future kiro-cli that serializes with a space after the colon
                # shows up as a divergence warning instead of a silent miss.
                if b'"image"' not in stripped:
                    out_lines.append(stripped)
                    continue
                try:
                    record = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    # RecursionError belongs here alongside the decode errors:
                    # json.loads raises it on deeply nested input, and this is
                    # ANOTHER tool's file, so the nesting is not ours to bound.
                    # Uncaught it would abort the whole repair on one odd line.
                    # Not decodable/parseable, so not the JSON kiro-cli writes,
                    # so not a record carrying media. Passed through byte-for-byte.
                    out_lines.append(stripped)
                    continue

                line_dirty = False
                anchored = [b for b in _iter_image_blocks(record) if _block_bytes(b) is not None]
                # Tripwire, before the rewrite mutates anything: count blocks the
                # generic walk can see that the anchored traversal could not.
                # Identity, not count, so a payload-embedded lookalike and a
                # genuinely missed shape are told apart by object.
                anchored_ids = {id(b) for b in anchored}
                generic, walk_truncated = _generic_image_blocks(record)
                unanchored = [
                    b for b in generic if id(b) not in anchored_ids and _block_bytes(b) is not None
                ]
                if unanchored or walk_truncated:
                    # A truncated walk counts as a divergence even with nothing
                    # found: verification did not complete, so the report must not
                    # read clean. That is the tripwire's only guarantee.
                    report.unanchored_images += len(unanchored)
                    report.unanchored_lines.append(line_no)
                    if walk_truncated:
                        report.walk_truncated = True

                for block in anchored:
                    payload = _block_bytes(block)
                    if payload is None:  # pragma: no cover - filtered above
                        continue
                    report.images += 1
                    finding = _rewrite_block(block, payload, MAX_IMAGE_EDGE_PX)
                    if finding is None:
                        continue
                    finding.line_no = line_no
                    report.findings.append(finding)
                    if finding.action != "keep":
                        line_dirty = True

                if line_dirty:
                    dirty = True
                    report.rewritten_lines += 1
                    out_lines.append(
                        json.dumps(record, separators=(",", ":")).encode("utf-8") + eol
                    )
                else:
                    out_lines.append(stripped)
    except OSError as exc:
        report.error = str(exc)
        return report, None

    return report, (out_lines if dirty else None)


def _live_writer_pid(path: Path) -> int | None:
    """The pid of a live kiro-cli holding *path*, from its sibling lock file.

    kiro-cli writes ``<session-id>.lock`` next to the transcript containing
    ``{"pid": N, "started_at": "..."}``. That is the only coordination signal
    the writer exposes -- there is no advisory lock and no protocol to join --
    and it is enough to refuse the case that actually loses data in practice:
    an operator running ``--apply`` against a session that is still running.

    Existence alone means nothing and must not be treated as a signal: stale
    locks accumulate without bound (6471 were present on one machine, the oldest
    months old), so the pid has to be probed. Routed through
    ``platform_compat.pid_exists`` because a raw ``os.kill(pid, 0)`` does not
    probe liveness on Windows.

    A recycled pid yields a false positive and refuses a repair that would have
    been safe. That is the right direction to fail: the operator can re-run,
    whereas a lost turn is gone.
    """
    lock_path = path.with_suffix(".lock")
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        pid = json.loads(raw).get("pid")
    except (ValueError, AttributeError):
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    return pid if platform_compat.pid_exists(pid) else None


def _refuse_if_moved(path: Path, expect: tuple[int, int] | None) -> None:
    """Raise :class:`TranscriptMovedError` if *path* no longer matches *expect*."""
    if expect is None:
        return
    st = path.stat()
    if (st.st_size, st.st_mtime_ns) != expect:
        raise TranscriptMovedError(
            f"{path.name} changed on disk since the scan "
            f"(size/mtime {expect} -> {(st.st_size, st.st_mtime_ns)}); "
            "re-run the repair"
        )


def _write_backup_exclusively(path: Path, backup_path: Path) -> None:
    """Create the sidecar, refusing if anything is already at that name.

    ``O_CREAT | O_EXCL`` rather than a check followed by a write, because the
    check-then-write form has a window: two concurrent ``--apply`` runs can both
    find no sidecar, and the later one's read then captures ALREADY-REPAIRED
    content and replaces the genuine pre-repair copy. Exclusive create is the
    filesystem primitive for this -- exactly one caller wins, the other gets
    ``EEXIST`` -- so the guarantee is atomic rather than merely narrow.

    Three properties fall out of the same call rather than needing separate
    defences. It refuses an existing regular file, which is the re-run case. It
    refuses a symlink, dangling or not, because ``O_CREAT | O_EXCL`` will not
    follow one -- so the predictable name cannot be used to redirect the write
    into a file the planter could not write directly. And it never truncates,
    so the previous sidecar's bytes are not at risk even momentarily.

    This gives up ``atomic_write``'s temp-file-and-rename crash safety, which is
    why the partial file is removed on any failure: at this point in the repair
    NOTHING has been modified yet, so a discarded partial backup costs a re-run
    and nothing else. The failure that mattered -- a backup destroyed after a
    repair already dropped an image -- is the one exclusive create prevents.
    """
    # Second leg, for Windows only in practice: a CI Windows shard showed
    # O_CREAT|O_EXCL NOT refusing a DANGLING symlink at this path, so the
    # symlink protection cannot rest on O_EXCL alone. This is a check-then-act
    # and is deliberately NOT the concurrency guarantee -- O_EXCL below remains
    # the atomic one for the re-run and concurrent-run cases. It only closes the
    # platform gap where a planted link would otherwise be followed.
    if backup_path.is_symlink():
        raise BackupExistsError(
            f"{backup_path.name} already exists (symlink); it would be followed "
            "or overwritten. Move it aside first."
        )
    try:
        fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise BackupExistsError(
            f"{backup_path.name} already exists; it holds the pre-repair "
            "transcript and would be overwritten. Move it aside first."
        ) from exc
    fd_owned = True  # the raw descriptor is ours until fdopen takes it
    try:
        # Lock down BEFORE any content and before the write-mode open, which is
        # both the real invariant and what the lockdown gate checks: the 0o600
        # mode above already means POSIX never sees a readable moment, but that
        # argument is ignored on Windows, so the DACL has to land while the file
        # is still empty (issue #5307).
        platform_compat.restrict_to_owner(backup_path)
        handle = os.fdopen(fd, "wb")
        fd_owned = False  # the file object owns it now and will close it
        with handle:
            handle.write(path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # The lockdown is fail-loud (in-process on Windows, but any failure
        # raises), so it can raise while we still hold the raw fd. Close it before
        # unlinking:
        # Windows refuses to remove a file with an open handle, and a stranded
        # empty sidecar would make every later --apply raise BackupExistsError --
        # a permanent block produced by a failure that changed nothing.
        if fd_owned:
            os.close(fd)
        backup_path.unlink(missing_ok=True)
        raise


def apply_repair(
    path: Path,
    lines: list[bytes],
    *,
    backup: bool = True,
    expect: tuple[int, int] | None = None,
    allow_live: bool = False,
) -> Path | None:
    """Atomically replace *path* with *lines*; return the backup path if made.

    Written to a sibling temp file and renamed so a crash mid-write cannot
    leave a truncated transcript, which would cost the whole session rather
    than one image.

    *expect* is the ``(size, mtime_ns)`` the scan saw. When given it is checked
    TWICE: once up front, and again immediately before :func:`os.replace`. The
    rewrite is built from lines read earlier, so an append landing in between
    would be dropped silently.

    Two checks rather than one because the work between them is not quick. The
    backup is a full copy of the whole transcript and the temp file is a
    full rewrite plus an ``fsync`` -- on a 26MB transcript that is seconds, and
    an append arriving inside it would pass the early check and still be lost.
    The early check is kept because failing before copying tens of megabytes is
    cheaper than failing after.

    The residual window cannot be closed, only narrowed: between the final stat
    and the rename there is no lock to hold, and kiro-cli does not participate
    in one. What changes is the width -- two syscalls instead of the duration of
    a multi-megabyte write. Passing ``None`` skips both checks, which is only
    right when the caller already knows the file is quiescent.

    What DOES address the realistic trigger is the liveness refusal: unless
    *allow_live*, a transcript whose sibling lock names a running kiro-cli is
    refused outright with :class:`TranscriptLiveError`. The narrow window is only
    dangerous while something is actively appending, so declining to touch a live
    session removes the condition rather than chasing the symptom.
    """
    if not allow_live:
        live_pid = _live_writer_pid(path)
        if live_pid is not None:
            raise TranscriptLiveError(
                f"{path.name} is held by a running kiro-cli (pid {live_pid}); "
                "end that session first, or pass --allow-live to override"
            )

    _refuse_if_moved(path, expect)

    backup_path: Path | None = None
    if backup:
        backup_path = path.with_suffix(path.suffix + ".pre-image-repair.bak")
        _write_backup_exclusively(path, backup_path)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    fd_owned = True
    try:
        # Lock the temp down BEFORE any content and before the write-mode open.
        # mkstemp is already 0o600 on POSIX, but on Windows the temp inherits the
        # session directory's DACL -- and os.replace carries the TEMP's
        # permissions onto the transcript, so a repair would silently WIDEN
        # access to the whole conversation for other local accounts. Same
        # invariant and same ordering as the backup above (issue #5307).
        platform_compat.restrict_to_owner(tmp_name)
        handle = os.fdopen(fd, "wb")
        fd_owned = False  # the file object owns it now
        with handle:
            for line in lines:
                handle.write(line)
                handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Last possible moment: everything expensive is already done, so this
        # leaves only the stat-to-rename gap instead of the whole write.
        _refuse_if_moved(path, expect)
        os.replace(tmp_name, path)
    except BaseException:
        if fd_owned:
            os.close(fd)
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return backup_path


def _format_report(report: FileReport, *, applied: bool) -> str:
    if report.error:
        return f"{report.path.name}: ERROR {report.error}"
    parts: list[str]
    if not report.changed:
        parts = [f"{report.path.name}: {report.images} image(s), all within cap"]
    else:
        verb = "repaired" if applied else "would repair"
        parts = [
            f"{report.path.name}: {report.images} image(s), "
            f"{len(report.oversized)} over cap, {verb} {report.rewritten_lines} record(s)"
        ]
        for finding in report.oversized:
            if finding.action == "drop":
                parts.append(f"    L{finding.line_no} {finding.width}x{finding.height} -> DROPPED")
            else:
                new = finding.new_size or (0, 0)
                parts.append(
                    f"    L{finding.line_no} {finding.width}x{finding.height}"
                    f" -> {new[0]}x{new[1]}"
                )
    if report.unanchored_images or report.walk_truncated:
        # Printed for BOTH outcomes, and especially the clean one: "all within
        # cap" plus a drift warning is the state an operator must not read as
        # done.
        lines = ", ".join(f"L{n}" for n in report.unanchored_lines[:8])
        if report.walk_truncated:
            parts.append(
                f"    WARNING: a record at {lines} nests deeper than this tool "
                "will walk, so it could NOT be fully checked for image blocks. "
                "Treat this report as incomplete."
            )
        if report.unanchored_images:
            parts.append(
                f"    WARNING: {report.unanchored_images} image block(s) at {lines} were not "
                "reachable by the known transcript shapes and were NOT repaired. Either a "
                "tool payload merely looks like an image block, or kiro-cli's format has "
                "changed and this report cannot be trusted."
            )
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session-image-repair",
        description=(
            "Cap oversized images stored in kiro-cli session transcripts. "
            "Dry run by default; pass --apply to rewrite."
        ),
    )
    parser.add_argument("paths", nargs="+", type=Path, help="session .jsonl file(s)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite the transcripts (default is a dry run that changes nothing)",
    )
    # No --no-backup flag: the sidecar is the only recovery path for an
    # operation that rewrites another tool's data file, and nothing named a harm
    # it removes. An operator who does not want the copy can delete one file.
    # apply_repair keeps a backup= keyword because the tests genuinely drive it.
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help=(
            "repair even when a running kiro-cli holds the transcript "
            "(refused by default; an append during the write would be lost)"
        ),
    )
    # No --max-edge flag and no max_edge parameter: the cap is the provider's,
    # not the operator's. A higher value produces a file the provider still
    # rejects, and a lower one has no stated use, so there is nothing for a
    # caller to choose. MAX_IMAGE_EDGE_PX is read directly.
    args = parser.parse_args(argv)

    exit_code = 0
    for path in args.paths:
        report, lines = scan_file(path)
        if report.error:
            exit_code = 1
        applied = False
        if args.apply and lines is not None:
            try:
                backup = apply_repair(
                    path,
                    lines,
                    backup=True,
                    expect=report.source_stat,
                    allow_live=args.allow_live,
                )
            except TranscriptLiveError as exc:
                print(f"{path.name}: REFUSED {exc}", file=sys.stderr)
                exit_code = 1
                continue
            except BackupExistsError as exc:
                print(f"{path.name}: REFUSED {exc}", file=sys.stderr)
                exit_code = 1
                continue
            except TranscriptMovedError as exc:
                print(f"{path.name}: REFUSED {exc}", file=sys.stderr)
                exit_code = 1
                continue
            except OSError as exc:
                print(f"{path.name}: ERROR writing repair: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            applied = True
            if backup is not None:
                print(f"{path.name}: backup at {backup.name}")
        print(_format_report(report, applied=applied))

    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())

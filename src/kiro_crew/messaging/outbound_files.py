"""Channel-neutral extraction of local file references from an outbound reply.

An agent that produces an image writes it into the reply as markdown --
``![chart](/tmp/chart.png)``. The dashboard renders that inline, but a chat
channel does not: Discord and Telegram deliver the raw text, so the user reads a
filesystem path where the picture should be. Sending the file natively means
pulling those references OUT of the text and handing the transport a list of
files to upload alongside the remaining prose.

This module owns the part that is identical for every channel: find the local
references, decide which are safe to send, rewrite the text without them, and
report the rest. What it deliberately does NOT own is the upload -- every
transport has its own multipart shape, its own per-file size ceiling and its own
count limit, so the channel keeps that half.

**Extract before splitting.** A reply is chunked to a channel's message-length
limit downstream (:mod:`kiro_crew.messaging.split`). Extraction has to run
first: after splitting, one ``![alt](path)`` can straddle two chunks, and a cut
inside the markup leaves half a link in each -- unrecognisable to any later pass
and visible to the user as broken markdown. Running first also shrinks the text
that has to be split, which can remove a cut entirely.

**A reference inside a code fence is documentation, not a picture to send**, and
which offsets are fenced comes from :func:`kiro_crew.messaging.split.iter_fence_spans`
-- the same state machine the splitter runs, exposed as a whole-text view. Nothing
about the fence grammar is re-derived here, because a second spelling of "which
run length closes which fence character" diverges on the next CommonMark fix.

**Rejections are returned, never swallowed.** Mirrors
:mod:`kiro_crew.messaging.attachments` on the inbound side: silently dropping a
file is the defect, because the user is left reading a reply that references a
picture with no picture and no explanation. Callers surface
:attr:`ExtractResult.rejections`; text whose file was rejected keeps its original
markup, so the path stays visible in the message. Each rejection carries a
machine-readable reason code alongside its default prose, so a channel can
re-word or branch without parsing English.

**The bytes travel, not the path.** :attr:`OutboundFile.data` holds the content
every gate below was applied to, and a transport uploads exactly that. A path
handed onward would be resolved a second time at upload, and anything able to
write that directory in between -- another turn, a subagent, a cron -- could swap
what gets sent for something no gate here ever saw.

**Security floor.** Every reference clears the same gates before it can become an
upload, and each one exists because the reply text is not trustworthy input -- a
prompt-injected agent chooses what it writes:

* a denylist check (:func:`kiro_crew.security.is_sensitive_path`), so
  ``![x](~/.aws/credentials)`` cannot be turned into an upload
* a refusal to follow a symlink, so the bytes come from the inode the written
  path names rather than from wherever a link points
* a descriptor-pinned read (:func:`safe_read_file_bytes_nolink`), so a hardlinked
  inode, a non-regular file, or a final-component swap after the check is refused
  against the inode actually opened -- and the bytes it returns are the ones that
  travel
* a magic-byte allowlist, so only a real raster is sent -- an extension proves
  nothing, and ``.svg`` is scriptable markup rather than a raster
* per-message caps on file count and total bytes, so one reply cannot hand a
  transport a thousand references to the same 25 MiB file. The byte budget is
  handed to the read itself, so an oversize file is refused rather than allocated,
  and an optional per-file ceiling narrows that same read for a channel whose own
  limit sits below the aggregate

No caller yet, by design: the per-channel wiring lands separately, so the core
can be reviewed and tested on its own.

All filesystem work here is blocking, so async callers MUST use
:func:`extract_local_refs_off_loop`, never :func:`extract_local_refs` directly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime
from kiro_crew.messaging.split import iter_fence_spans
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

#: Markdown inline-image OPENING: ``![alt](``. Stops at the opening paren and
#: captures no destination -- the destination is walked from the text after the
#: match by :func:`md_destination`, because a lazy ``[^)\s]+`` capture truncates
#: an ordinary ``screenshot(1).png`` at its inner ``)``, and a greedy one makes
#: ``finditer`` swallow every later image on the SAME line into one match.
#:
#: The alt capture accepts backslash escapes (``(?:[^\]\\]|\\.)*``) because a
#: caption may legally contain an escaped bracket -- ``![Revenue \[Q1\]](p.png)``.
#: A plain ``[^\]]*`` stops at that escaped ``]``, the whole pattern then fails to
#: match, and the image is never seen at all.
IMAGE_MD_RE = re.compile(r"!\[((?:[^\]\\]|\\.)*)\]\(")

#: Unwraps a markdown backslash escape to the character it escaped.
_MD_ESCAPE_RE = re.compile(r"\\(.)")

#: Characters a backslash may legally escape inside a markdown destination. A
#: backslash before anything else is a literal -- most importantly a Windows path
#: separator (``C:\Users\me\shot.png``).
_MD_ESCAPABLE = frozenset("()[]\\<>\"'")

#: Destinations that are not a local file at all, so there is nothing to upload
#: and nothing to report. Public because both directions test against it: a
#: remote reference is skipped by extraction and by artifact registration alike,
#: and two copies of the list is how one direction starts treating a scheme the
#: other rejects as a local path.
REMOTE_PREFIXES = ("http://", "https://", "data:", "//")

#: Machine-readable reason a reference was not turned into an upload. A caller
#: that needs different wording, a different language, or a different channel
#: affordance branches on these rather than parsing the prose.
REASON_NOT_ABSOLUTE = "not_absolute"
REASON_MISSING = "missing"
REASON_SYMLINK = "symlink"
REASON_SENSITIVE = "sensitive"
REASON_UNREADABLE = "unreadable"
REASON_NOT_RASTER = "not_raster"
REASON_OVER_BYTE_BUDGET = "over_byte_budget"
#: One file larger than the per-file ceiling, vs the whole message referencing
#: more files than the count cap allows.
REASON_OVER_FILE_BYTES = "over_file_bytes"
REASON_OVER_FILE_CAP = "over_file_cap"


@dataclass(frozen=True)
class Rejection:
    """One reference that could not be sent, and why.

    Structured rather than a pre-formatted sentence: the wording belongs to
    whatever surface displays it, and a channel-neutral module that bakes English
    into its return value cannot be localized or branched on. ``str()`` renders
    the default prose, so a caller that just wants a line to append does not have
    to know the shape.
    """

    #: The markdown destination as written, or empty for a message-level refusal
    #: that names no single reference.
    dest: str
    #: One of the ``REASON_*`` codes above.
    reason: str
    #: Human-readable explanation, without the destination or brackets.
    detail: str

    def __str__(self) -> str:
        if not self.dest:
            return f"[{self.detail}]"
        return f"[{self.dest} — not sent: {self.detail}]"


@dataclass(frozen=True)
class OutboundFile:
    """One local file a transport should upload alongside the message text.

    **The bytes are the payload; the path is provenance.** A transport uploads
    :attr:`data` and MUST NOT re-open :attr:`path`. Everything this module
    validated -- the denylist, the symlink refusal, the descriptor-pinned read,
    the byte-signature check -- was validated against the inode these bytes came
    from. Re-opening the path later resolves the name a second time, and anything
    that can write to the directory (another agent turn, a subagent, a cron)
    could have replaced the file in between, so a path-based upload would send
    something no gate here ever saw.

    :attr:`path` is still worth carrying: it supplies the filename a transport
    puts on the upload, and it is what a log line or a rejection needs to name.
    """

    #: Absolute path the bytes were read from. Provenance and filename only --
    #: never re-open it.
    path: str
    #: The exact bytes to upload, as validated.
    data: bytes
    #: The markdown alt text, unescaped. Empty when the reference had none. A
    #: transport that supports per-file descriptions should send it; the file
    #: keeps it either way, so the caption is not lost when the markup is cut.
    alt: str
    #: The type the file's leading bytes say it is -- never its extension.
    mime: str

    @property
    def size_bytes(self) -> int:
        """Bytes to upload. Derived from :attr:`data`, so it cannot disagree."""
        return len(self.data)


@dataclass(frozen=True)
class ExtractLimits:
    """Per-message budgets.

    Deliberately separate constants from the artifact-registration budgets in
    :mod:`kiro_crew.image_artifacts`, which happen to carry the same numbers:
    that budget bounds bytes copied into the artifact store, this one bounds
    bytes handed to a transport. Tying them to one symbol would make a change
    for one resource silently retune the other.
    """

    #: Local references considered per message. Counts every reference that
    #: reaches validation, including ones then rejected -- so a reply full of
    #: unreadable paths cannot make this loop do unbounded filesystem work, and
    #: the rejection list it produces stays bounded too.
    max_files: int = 12
    #: Total bytes this message may hand a transport. A per-FILE ceiling is the
    #: channel's own, and much lower; this only bounds the aggregate. It is also a
    #: MEMORY bound: extraction holds the validated bytes until the transport has
    #: sent them, and each read is capped at what remains, so one message's peak
    #: is this value plus one byte rather than the size of whatever it referenced.
    max_total_bytes: int = 64 * 1024 * 1024
    #: Optional per-file ceiling, for a channel whose own limit sits below the
    #: aggregate (Discord's 10 MiB, say). A file over it is REJECTED here, with
    #: its markup left in the text, so the channel never has to drop an
    #: already-validated file after extraction has cut the reference out -- which
    #: would be the silent drop this module exists to prevent. ``None`` leaves the
    #: aggregate as the only byte bound.
    max_file_bytes: int | None = None


@dataclass
class ExtractResult:
    """What a transport needs in order to send the reply.

    ``rewritten_text`` is the message minus the markup of every extracted file.
    Everything else -- a rejected file, a reference inside a code fence, a remote
    URL -- is left exactly as written.
    """

    rewritten_text: str
    files: list[OutboundFile] = field(default_factory=list)
    #: Why each reference was not turned into an upload. Surface these; the whole
    #: point of this module is that they are not swallowed. ``str()`` on one gives
    #: a ready-to-append line, and :attr:`Rejection.reason` is there for a caller
    #: that wants to branch or re-word.
    rejections: list[Rejection] = field(default_factory=list)


def unescape_md(text: str) -> str:
    """Undo markdown backslash escaping, so a caption reads as written."""
    return _MD_ESCAPE_RE.sub(r"\1", text)


def _walk_destination(rest: str) -> tuple[str | None, int]:
    """Walk a markdown destination, returning it and how much text it consumed.

    *rest* is the text immediately after ``![alt](``. The return is the
    destination (``None`` when it never closes) and the offset just past the
    closing paren, so a caller that needs to CUT the markup uses the same walk
    that produced the destination. Deriving that closing paren a second time from
    the same text is how two passes end up disagreeing on where markup ends and
    prose resumes.

    Markdown allows unescaped parentheses in a destination as long as they
    balance, which is exactly the common ``screenshot(1).png`` case, so this
    tracks depth rather than stopping at the first ``)``. Backslash escapes are
    honoured only before markdown-significant characters
    (:data:`_MD_ESCAPABLE`): a native Windows path is ``C:\\Users\\me\\shot.png``,
    and treating every backslash as an escape strips the separators and leaves a
    path that cannot resolve.
    """
    depth = 1
    out: list[str] = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest) and rest[i + 1] in _MD_ESCAPABLE:
            # A real markdown escape: keep the escapee, and never let it move the
            # paren depth.
            out.append(rest[i + 1])
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return _finish_destination("".join(out)), i + 1
        out.append(ch)
        i += 1
    return None, 0


def _finish_destination(raw: str) -> str | None:
    """Strip the optional ``<...>`` wrapper or trailing ``"title"`` from *raw*."""
    dest = raw.strip()
    # `<...>` is markdown's explicit way to write a destination containing spaces
    # (`![c](</tmp/generated images/c.png>)`). Unwrap it and DON'T split on
    # whitespace: inside the brackets a space is part of the path, not the
    # separator before a `"title"`.
    if dest.startswith("<"):
        end = dest.find(">")
        if end == -1:
            return None  # unterminated -- don't guess at the path
        return dest[1:end].strip() or None
    # Bare destination: a `"title"` suffix is separated by whitespace, so the path
    # ends at the first space.
    if " " in dest or "\t" in dest:
        dest = re.split(r"[ \t]", dest, maxsplit=1)[0]
    return dest or None


def md_destination(rest: str) -> str | None:
    """The markdown link destination in the text after ``![alt](``.

    ``None`` when the destination never closes (malformed, or a ``(`` that
    belongs to prose).
    """
    return _walk_destination(rest)[0]


def _inside(offset: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in spans)


def strip_url_syntax(raw_dest: str) -> str:
    """A markdown destination with URL syntax removed, ready for the filesystem.

    Drops anything after ``?`` or ``#`` -- a local path has no query or fragment,
    so those would be taken for part of the filename -- and unwraps ``file://``.
    """
    clean = raw_dest.split("?", 1)[0].split("#", 1)[0]
    if clean.startswith("file://"):
        clean = clean[len("file://") :]
    return clean


def local_destination(raw_dest: str) -> Path | None:
    """Resolve a markdown destination to an absolute local path, or ``None``.

    ``None`` means not a plain absolute path: a relative one (no stable meaning
    off the agent's working directory) or a string the OS refuses. Existence and
    the denylist belong to the caller, which is where the two directions differ --
    registration skips silently, extraction reports a reason -- so only the
    normalization is shared.
    """
    try:
        path = Path(strip_url_syntax(raw_dest)).expanduser()
        if not path.is_absolute():
            return None
    except (OSError, RuntimeError, ValueError):
        # expanduser() raises RuntimeError for a `~user` with no home entry.
        return None
    return path


def _inspect(
    dest: str, path: Path, alt: str, budget: int, max_file_bytes: int | None
) -> OutboundFile | Rejection:
    """Decide whether *path* can be uploaded; return the file OR a rejection.

    ``budget`` is the bytes still available under the message's total cap and
    ``max_file_bytes`` the optional per-file ceiling. The tighter of the two is
    handed to the read itself, so an oversize file is refused by the read rather
    than after allocating it, and the rejection names which bound it hit.

    The read is what makes the result authoritative: a returned
    :class:`OutboundFile` carries the bytes that were checked, so nothing between
    here and the upload can substitute different content.
    """
    read_cap = budget
    over_reason = REASON_OVER_BYTE_BUDGET
    over_detail = "the message's upload budget is full"
    if max_file_bytes is not None and max_file_bytes <= read_cap:
        # Ties go to the per-file bound: it is the more actionable reason, and the
        # one a caller can act on by sending something smaller.
        read_cap = max_file_bytes
        over_reason = REASON_OVER_FILE_BYTES
        over_detail = f"larger than this channel's {max_file_bytes}-byte per-file limit"
    try:
        if path.is_symlink():
            # Refused rather than resolved: the bytes below must come from the
            # inode this path names, not from wherever a link points now.
            return Rejection(dest, REASON_SYMLINK, "symlinks are not uploaded")
        if not path.is_file():
            return Rejection(dest, REASON_MISSING, "no such file")
        if is_sensitive_path(str(path)):
            return Rejection(dest, REASON_SENSITIVE, "reading this location is blocked")
        try:
            data = safe_read_file_bytes_nolink(str(path), max_bytes=read_cap)
        except FileTooLargeError:
            return Rejection(dest, over_reason, over_detail)
        if data is None:
            # Refused by the read gate: a hardlinked inode, a non-regular file, a
            # final-component swap since the checks above, or simply unreadable.
            return Rejection(dest, REASON_UNREADABLE, "the file could not be read safely")
        mime = sniff_raster_mime(data[:SNIFF_BYTES])
        if mime is None:
            return Rejection(dest, REASON_NOT_RASTER, "not a PNG, JPEG, GIF, WebP or BMP image")
        return OutboundFile(path=str(path), data=data, alt=alt, mime=mime)
    except (OSError, ValueError) as exc:
        # ValueError covers a path the OS refuses outright, e.g. an embedded NUL.
        logger.warning("outbound file %s could not be inspected: %s", path, exc)
        return Rejection(dest, REASON_UNREADABLE, "the file could not be read")


def extract_local_refs(text: str, *, limits: ExtractLimits | None = None) -> ExtractResult:
    """Pull local raster references out of *text* for a transport to upload.

    Returns the text to send, the files to send with it, and a reason for every
    reference that could not be sent. Never raises: the reply must go out even if
    every reference in it turns out to be unusable.
    """
    lim = limits or ExtractLimits()
    if not text:
        return ExtractResult(rewritten_text=text or "")
    matches = list(IMAGE_MD_RE.finditer(text))
    if not matches:
        return ExtractResult(rewritten_text=text)

    fenced = list(iter_fence_spans(text))
    files: list[OutboundFile] = []
    rejections: list[Rejection] = []
    cuts: list[tuple[int, int]] = []
    considered = 0
    over_cap = 0
    total_bytes = 0

    for match in matches:
        if _inside(match.start(), fenced):
            continue  # inside a code fence: literal text, not markup
        dest, consumed = _walk_destination(text[match.end() :])
        if not dest:
            continue  # malformed markup, or a `(` that belongs to prose
        if dest.lower().startswith(REMOTE_PREFIXES):
            continue  # remote or data URI: nothing local to upload
        if considered >= lim.max_files:
            over_cap += 1
            continue
        considered += 1
        path = local_destination(dest)
        if path is None:
            rejections.append(
                Rejection(dest, REASON_NOT_ABSOLUTE, "only absolute paths can be uploaded")
            )
            continue
        outcome = _inspect(
            dest,
            path,
            unescape_md(match.group(1) or "").strip(),
            lim.max_total_bytes - total_bytes,
            lim.max_file_bytes,
        )
        if isinstance(outcome, Rejection):
            # A rejected reference keeps its markup, so the path stays visible.
            rejections.append(outcome)
            continue
        files.append(outcome)
        total_bytes += outcome.size_bytes
        cuts.append((match.start(), match.end() + consumed))

    if over_cap:
        rejections.append(
            Rejection(
                "",
                REASON_OVER_FILE_CAP,
                f"{over_cap} more file reference(s) not sent — "
                f"limit {lim.max_files} per message",
            )
        )
    return ExtractResult(rewritten_text=_apply_cuts(text, cuts), files=files, rejections=rejections)


def _apply_cuts(text: str, cuts: list[tuple[int, int]]) -> str:
    """Remove *cuts* from *text*, dropping any line they leave empty.

    Line-by-line rather than a straight slice-and-join so a line that held
    nothing but an extracted image disappears instead of leaving a blank one
    behind. A line whose remainder still has content keeps that remainder
    verbatim -- trailing whitespace included, because a markdown line's trailing
    spaces can be an authored hard break.

    Dropping a whole line also consumes ONE adjacent blank line when the line sat
    between blanks (the usual shape: prose, blank, image, blank, prose). Without
    that, removing the image leaves the two surrounding blanks adjacent and the
    reply gains a blank line the author never wrote. At most one blank goes per
    dropped line, so a deliberately wide gap keeps its width.
    """
    if not cuts:
        return text
    kept: list[str] = []
    squeeze = False
    pos = 0
    for line in text.split("\n"):
        start = pos
        end = start + len(line)
        pos = end + 1
        pieces: list[str] = []
        cursor = start
        touched = False
        for cut_start, cut_end in cuts:
            if cut_end <= start or cut_start >= end:
                continue
            touched = True
            if cut_start > cursor:
                pieces.append(text[cursor:cut_start])
            cursor = max(cursor, min(cut_end, end))
        pieces.append(text[cursor:end])
        remainder = "".join(pieces)
        if touched and not remainder.strip() and line.strip():
            squeeze = True  # the markup WAS the whole line -- drop the blank it left
            continue
        if squeeze and not remainder.strip() and (not kept or not kept[-1].strip()):
            squeeze = False
            continue
        squeeze = False
        kept.append(remainder)
    if squeeze and kept and not kept[-1].strip():
        # The dropped line was last, so its leading blank is now a trailing one.
        kept.pop()
    return "\n".join(kept)


async def extract_local_refs_off_loop(
    text: str, *, limits: ExtractLimits | None = None
) -> ExtractResult:
    """Async form of :func:`extract_local_refs`, run off the event loop.

    The channel send paths that will call this are async, and extraction stats
    and opens up to :attr:`ExtractLimits.max_files` files. On the gateway's one
    loop that freezes every other session for the duration, so an async caller
    MUST use this rather than calling :func:`extract_local_refs` directly.

    ``asyncio.to_thread`` rather than the shared subprocess executor, matching
    :func:`kiro_crew.image_artifacts.register_images_off_loop`: that pool is
    reserved for subprocess and PTY teardown, whose workers have to stay free to
    recover a wedged kernel resource, and this work is short and
    filesystem-bound with no reason to queue behind it.
    """
    return await asyncio.to_thread(extract_local_refs, text, limits=limits)

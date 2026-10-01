"""The structured attachment list a channel hands to the provider with a prompt.

An image reaches the model as an ACP image block ONLY when the receiving
channel says the user attached one: the dashboard's upload list, a Slack or
Discord file, a Telegram photo. The prompt TEXT is never scanned for image
paths -- see :func:`kiro_crew.acp.prompt_blocks.build_prompt_blocks`, which
builds its image blocks from this list alone.

Why the list is structured rather than inferred from the text: a path in text is
a mention, not an upload. The session ledger's ``artifact <name>: <path>.png``
snapshot line rides in every nudge cycle; a nudge body, an injected envelope, an
agent's own ``![shot](...png)`` reply quoted back and a consolidation prompt all
name files the user never attached to THIS message. Inlining a mention re-sends
the file, and because the backend replays every stored image block, a screenshot
named in a per-cycle snapshot grows the request by its full encoded size on every
turn until the backend rejects the body. Only the channel that received a file
knows the user attached it, so only the channel may say so.

A near-leaf module: it is imported by the ACP prompt builder, by
``kiro_crew.messaging`` (which the builder must not import back) and by the
dashboard runner, so it reaches only the file, image and platform helpers, and
the raster sniff is imported inside the predicate because the messaging package
imports this module while initialising.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.hooks import (
    is_unc_shape,
    safe_read_file_bytes,
    safe_read_file_bytes_nolink,
    unc_probe_allowed,
)
from kiro_crew.imaging import MAX_IMAGE_B64_BYTES, MAX_IMAGE_EDGE_PX, downscale_image_block
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

logger = logging.getLogger(__name__)

#: Longest name the ``[image: <name>]`` marker carries. The name is the
#: sender's own filename -- text the model reads inline -- so it is bounded
#: and flattened to one line rather than trusted to be either.
NAME_MAX_CHARS = 120


def bounded_name(name: str) -> str:
    """*name* flattened to one line and cut to :data:`NAME_MAX_CHARS`.

    The one rule for every name a record carries or shows: a channel's
    filename is sender-supplied, so it is bounded where it is STORED
    (ingestion builds the record with it) as well as where it is read.
    """
    return " ".join((name or "").split())[:NAME_MAX_CHARS]


#: A Windows drive-letter or UNC path, the shape the dashboard composer
#: rewrites to forward slashes inside a markdown destination (a destination
#: cannot carry raw backslashes: CommonMark eats ``\`` before punctuation).
_WINDOWS_SHAPE_RE = re.compile(r"^(?:[A-Za-z]:|\\\\[^\\/]+)[\\/]")

#: A destination the composer emits VERBATIM. Anything outside this set is
#: emitted percent-escaped and ``<...>``-wrapped (``mdImageDest`` in the
#: frontend's ``utils/fileTokens.ts``; this is its mirror). ``re.ASCII``
#: because JavaScript's ``\w`` is ``[A-Za-z0-9_]`` while Python's is
#: Unicode-aware: without it a Cyrillic or accented filename reads as plain
#: here and wrapped there, and the two spellings never meet.
_PLAIN_DEST_RE = re.compile(r"^[\w/.@:~-]*$", re.ASCII)


@dataclass(frozen=True)
class PromptAttachment:
    """One file the user attached to THIS message, as the receiving channel saw it.

    ``path`` is the absolute local path the bytes are read from; ``name`` is what
    the model is told it was given (``[image: <name>]``), defaulting to the
    path's basename. The record carries no type: the wire type is always
    derived from the file's leading bytes by the prompt builder, so a declared
    one would be a claim nothing reads.
    """

    path: str
    name: str = ""

    @property
    def display_name(self) -> str:
        """The name the model sees in the ``[image: <name>]`` marker.

        One line, at most :data:`NAME_MAX_CHARS` characters, whichever of the
        two sources supplies it: a channel's filename is sender-supplied text,
        and a basename is whatever the path carries.
        """
        name = bounded_name(self.name)
        if name:
            return name
        fallback = bounded_name(os.path.basename(self.path.rstrip("/\\")))
        return fallback or bounded_name(self.path) or self.path[:NAME_MAX_CHARS]


def markdown_image_dest(path: str) -> str:
    """The destination the dashboard composer writes for *path* in ``![image](...)``.

    Mirrors the frontend's ``mdImageDest`` exactly: a Windows drive-letter or
    UNC path is spelled with forward slashes; a destination made only of
    word characters, ``/``, ``.``, ``@``, ``:``, ``~`` and ``-`` is emitted as
    is; anything else has ``%`` escaped to ``%25`` and ``\\``, ``<``, ``>``
    backslash-escaped, and is wrapped in ``<...>``. Two consumers need the
    same answer: the prompt builder, to rewrite the line it inlined to the
    marker, and the dashboard's queued-edit prune, to see that a user removed
    a picture whose line the composer had escaped.
    """
    normalized = path.replace("\\", "/") if _WINDOWS_SHAPE_RE.match(path) else path
    if _PLAIN_DEST_RE.match(normalized) and "%" not in normalized:
        return normalized
    escaped = normalized.replace("%", "%25")
    escaped = re.sub(r"([\\<>])", r"\\\1", escaped)
    return f"<{escaped}>"


def path_spellings(path: str) -> tuple[str, ...]:
    """Every spelling a channel may have written *path* into the text in.

    The path itself; its forward-slash form when the path is Windows-shaped;
    and the exact markdown destination the dashboard composer emits. Order is
    longest-first so a substitution over the text replaces the wrapped
    destination whole rather than the bare path inside it. Deduplicated.
    """
    out: list[str] = [path]
    # Only a Windows-shaped path is ever spelled with forward slashes by a
    # channel (the same rule markdown_image_dest applies). A POSIX name that
    # merely holds a backslash keeps its one spelling: its slash-translated
    # sibling is an unrelated path, and marking it would rewrite the user's
    # own prose wherever that sibling appeared.
    if _WINDOWS_SHAPE_RE.match(path):
        slashed = path.replace("\\", "/")
        if slashed != path:
            out.append(slashed)
    dest = markdown_image_dest(path)
    if dest not in out:
        out.append(dest)
    return tuple(sorted(set(out), key=len, reverse=True))


#: What may stand right before or right after a spelling for it to count as
#: naming the path: the text's edges, whitespace (Slack writes the path as a
#: bare line), the parentheses of a markdown destination, or a quote. Any other
#: neighbour makes the match a piece of a LONGER token -- ``/tmp/a.png`` inside
#: ``/tmp/a.png.bak`` or ``x/tmp/a.png`` -- which names a different file.
_SPAN_DELIMITERS = frozenset("()\"'`")


def _delimited(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return (not before or before.isspace() or before in _SPAN_DELIMITERS) and (
        not after or after.isspace() or after in _SPAN_DELIMITERS
    )


def _boundary_char(char: str) -> bool:
    return not char or char.isspace() or char in _SPAN_DELIMITERS


def _periodic_run_end(text: str, at: int, period: int, known: int) -> int:
    """End of the longest region ``[at, R)`` in which every character repeats the
    one *period* before it, given that ``[at, known)`` already does.

    Galloping then bisecting over whole-slice comparisons keeps the work in C and
    proportional to the run, never to the text: a long run costs a few compares
    of its own length, a short one a few compares of a spelling's length.
    """
    n = len(text)
    lo, step = known, known - at
    while lo < n:
        hi = min(lo + step, n)
        if text[at + period : hi] == text[at : hi - period]:
            lo, step = hi, step * 2
            continue
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if text[at + period : mid] == text[at : mid - period]:
                lo = mid
            else:
                hi = mid
        return lo
    return n


def _spelling_spans(spelling: str, text: str) -> list[tuple[int, int]]:
    """Delimited occurrences of one *spelling*, left to right, non-overlapping.

    Linear in the text. ``str.find`` verifies the whole spelling at every hit,
    so the cost to keep down is a run of hits that each fail the delimiter
    test: a spelling without a delimiter inside it cannot start a delimited
    occurrence anywhere inside a rejected hit (the character before such a
    start would be one of its own), so the scan skips the hit's whole width.
    A spelling that does contain one can, but only if it recurs at a short
    stride, and then the text around the hits is periodic: the run is measured
    once, and inside it every occurrence after the first sees the same
    character before it and the same one after it, so the whole run is judged
    at once instead of occurrence by occurrence.
    """
    width = len(spelling)
    out: list[tuple[int, int]] = []
    plain = not any(_boundary_char(c) for c in spelling)
    at = text.find(spelling)
    while at >= 0:
        end = at + width
        if _delimited(text, at, end):
            out.append((at, end))
            at = text.find(spelling, end)
            continue
        if plain:
            at = text.find(spelling, end)
            continue
        nxt = text.find(spelling, at + 1)
        gap = nxt - at
        if nxt < 0 or gap > width // 2:
            at = nxt
            continue
        run_end = _periodic_run_end(text, at, gap, nxt + width)
        # Occurrences inside the run sit at ``at + k * gap``; a shorter stride
        # would have been found first. Those ending before the run's end all
        # share one verdict; the one ending exactly at it sees the breaking
        # character and is judged alone.
        last = at + ((run_end - width - at) // gap) * gap
        pos = nxt
        if _boundary_char(spelling[gap - 1]) and _boundary_char(text[at + width]):
            while pos + width < run_end:
                out.append((pos, pos + width))
                pos = at + -(-(pos + width - at) // gap) * gap
        if last >= pos and last + width == run_end and _delimited(text, last, run_end):
            out.append((last, run_end))
        at = text.find(spelling, last + gap)
    return out


def path_spans(path: str, text: str) -> list[tuple[int, int]]:
    """Every span of *text* that names *path*, as ``(start, end)`` pairs.

    A spelling (:func:`path_spellings`) counts only where it stands delimited
    (:data:`_SPAN_DELIMITERS`): a bare substring test would read
    ``/tmp/a.png`` in ``/tmp/a.png.bak`` and keep a removed picture alive
    through a different file, or let a rewrite corrupt that longer path.
    Each spelling is scanned once, left to right (:func:`_spelling_spans`);
    the spans of all spellings are then merged in one sorted pass that keeps
    the earliest (and, at a tie, the longest) span and drops any that overlaps
    a kept one -- so a wrapped destination is one span rather than the bare
    path inside it. Linear in the text plus ``k log k`` in the number of
    spans: this runs on the gateway loop from the queued-edit prune over
    caller-typed text and caller-supplied paths, so nothing here may grow with
    the product of the two or with the square of the matches.
    Sorted by position, non-overlapping.
    """
    found: list[tuple[int, int]] = []
    for spelling in path_spellings(path):
        found.extend(_spelling_spans(spelling, text))
    found.sort(key=lambda span: (span[0], span[0] - span[1]))
    spans: list[tuple[int, int]] = []
    claimed_end = -1
    for at, end in found:
        if at < claimed_end:
            continue
        spans.append((at, end))
        claimed_end = end
    return spans


def named_in_text(path: str, text: str) -> bool:
    """Whether *text* names *path* -- delimited, in any spelling a channel writes."""
    return bool(path_spans(path, text))


def image_attachments(paths: Iterable[str]) -> tuple[PromptAttachment, ...]:
    """Structured attachments for the image files a channel received.

    Order is kept and an empty or non-string entry is dropped; a path that
    appears twice is kept once, first position wins. The builder validates
    every entry against the filesystem and the bytes, so nothing here reads a
    file.
    """
    out: list[PromptAttachment] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            continue
        path = raw.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(PromptAttachment(path=path))
    return tuple(out)


#: Raster formats kiro-cli accepts as inline vision input, by file suffix. SVG
#: is deliberately absent: it is scriptable XML rather than a raster image, and
#: a vision model gains nothing from it. The channel's attachment list selects
#: the candidates and the leading bytes decide the wire type, so the suffix
#: gates nothing here; the table is the declared set of inlineable types that
#: ``messaging.attachments`` keeps in step with.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: Raw bytes per image, checked BEFORE base64. Encoding inflates by 4/3 and the
#: whole request is serialized as a single newline-delimited JSON frame, so an
#: unbounded image becomes an unbounded write. Matches the Slack producer cap so
#: a file that passed ingestion is not silently dropped here.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: Written after a readable picture's name or path when a cap kept it out of
#: the prompt, so neither the user nor the model takes the picture as seen.
PROMPT_LIMIT_NOTE = "[image not attached: prompt image limit]"
IMAGE_SIZE_NOTE = "[image not attached: image size limit]"


@dataclass(frozen=True)
class ImageRead:
    """What the gate and the leading bytes say about one listed picture.

    ``raw`` and ``mime`` are set when the file is a readable raster under the
    size cap. Otherwise both are ``None`` and the picture stays text; ``note``
    then names the note the text earns, which only an oversize file whose head
    sniffs as a raster does. A refused, missing, non-raster or host-named
    entry earns none: the notes speak about images, and a wrong claim is worse
    than a missing one.
    """

    raw: bytes | None = None
    mime: str | None = None
    note: str | None = None


def inline_image_payload(
    attachment: PromptAttachment,
    *,
    max_image_bytes: int | None = None,
    max_image_edge: int | None = None,
    max_image_b64_bytes: int | None = None,
) -> tuple[bytes, str] | None:
    """The ``(bytes, mime)`` *attachment* inlines as, or ``None`` when it stays text.

    One predicate for everything that decides whether a listed path becomes an
    image block on its own: the Windows host-name gates, a readable regular
    file under the size cap, raster bytes by content, and a rendition within
    the edge and encoded-size caps (:func:`read_image_attachment` plus the
    downscale). A caller that only needs to know whether a list would inline
    anything asks this and never re-derives the rules; the prompt builder runs
    the two halves itself, because it compares the raw bytes against the
    pictures it already holds before it decodes anything.
    """
    if max_image_edge is None:
        max_image_edge = MAX_IMAGE_EDGE_PX
    if max_image_b64_bytes is None:
        max_image_b64_bytes = MAX_IMAGE_B64_BYTES
    read = read_image_attachment(attachment, max_image_bytes=max_image_bytes)
    if read.raw is None or read.mime is None:
        return None
    downscaled = downscale_image_block(
        read.raw, read.mime, max_edge=max_image_edge, max_b64_bytes=max_image_b64_bytes
    )
    if downscaled is None:
        # No compliant rendition (decompression-bomb / undecodable /
        # truncated / over the decode-pixel ceiling / still over the
        # encoded ceiling at the minimum edge): leave the text as it is
        # rather than inline a payload the backend rejects on this and
        # every later turn. A tool-capable agent can still open it.
        logger.warning(
            "acp prompt: image %s could not be rendered within the "
            "dimension and encoded-size caps - sending path, not inline",
            Path(attachment.path).name,
        )
        return None
    return downscaled


def probe_safe_path(raw: str) -> Path | None:
    """*raw* as a path the process may probe on disk, or None.

    The path-level screen every filesystem probe of a listed path runs first:
    the reader's probes, the identity recorded beside the list, the recovery
    scan of a spelling's directory. On Windows a UNC-shaped name names a HOST,
    so probing it opens an SMB connection to a caller-controlled name (the
    dashboard's list is client JSON), and a linked ANCESTOR defeats that lexical
    screen because every probe resolves the ancestors. POSIX has neither
    semantics (a doubled leading slash is a local path and stat-ing through a
    symlink is harmless). Everywhere, a component over 255 characters (past
    every common name limit) is refused rather than let one ``ENAMETOOLONG``
    fail the whole turn. A probe that FOLLOWS the leaf adds its own leaf-link
    check; one that reads the leaf by ``lstat`` needs none.
    """
    raw = raw.strip()
    if not raw:
        return None
    if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
        return None
    path = Path(raw)
    if os.name == "nt" and first_linked_ancestor(path) is not None:
        return None
    if any(len(part) > 255 for part in path.parts):
        return None
    return path


def read_image_attachment(
    attachment: PromptAttachment, *, max_image_bytes: int | None = None
) -> ImageRead:
    """*attachment*'s raw bytes and content type, read through the gate.

    Everything that decides whether a listed path is a picture at all: the
    Windows host-name gates before any filesystem call, a readable regular
    file under ``max_image_bytes``, and raster bytes by content. What the
    bytes then become on the wire is the caller's business.
    """
    # Lazy: the messaging package initialises its transport, which imports this
    # module, so this one cannot be module-level here.
    from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime

    if max_image_bytes is None:
        max_image_bytes = MAX_IMAGE_BYTES
    path = probe_safe_path(attachment.path or "")
    if path is None:
        return ImageRead()
    # The LEAF gets the junction-aware check the path screen leaves to the
    # probe: is_file() below FOLLOWS a final-component link, so a leaf
    # symlink/junction targeting a UNC share is the same host probe.
    # lstat-based, so the link itself is never followed.
    if os.name == "nt" and is_link_or_junction(path):
        return ImageRead()
    raw = str(path)
    try:
        if not path.is_file():
            logger.debug("acp prompt: attachment %s is not a file - skipped", raw)
            return ImageRead()
    except OSError:
        logger.debug("acp prompt: could not probe attachment %s - skipped", raw, exc_info=True)
        return ImageRead()
    try:
        size = path.stat().st_size
    except OSError:
        logger.debug("acp prompt: could not stat image %s", raw, exc_info=True)
        return ImageRead()
    if size > max_image_bytes:
        # The text keeps its reference, and the note says the picture was
        # not attached, so nobody takes it as seen. The note speaks about an
        # image, so only bytes that sniff as a raster earn it: a bounded read
        # through the same gate, never the whole file.
        logger.warning(
            "acp prompt: image %s is %d bytes (cap %d) - sending path, not inline",
            path.name,
            size,
            max_image_bytes,
        )
        return ImageRead(note=IMAGE_SIZE_NOTE if _sniffs_as_raster(path) else None)
    try:
        raw_bytes = safe_read_file_bytes(str(path))
    except Exception:
        logger.debug("acp prompt: could not read image %s", raw, exc_info=True)
        return ImageRead()
    if raw_bytes is None:
        # Refused by the sensitive-path gate (or unreadable). The text
        # stays as the channel wrote it; nothing is inlined.
        logger.warning("acp prompt: image read refused for %s", path.name)
        return ImageRead()
    # The channel's list selects the CANDIDATES; the bytes decide what
    # reaches the wire (a suffix is a claim; the record carries no type).
    # Require a complete sniff window so a truncated header cannot
    # become a pass-through image when Pillow is unavailable.
    mime = sniff_raster_mime(raw_bytes[:SNIFF_BYTES]) if len(raw_bytes) >= SNIFF_BYTES else None
    if mime is None or mime not in IMAGE_MEDIA_TYPES.values():
        logger.warning(
            "acp prompt: %s is not a supported raster by content - sending path, not inline",
            path.name,
        )
        return ImageRead()
    return ImageRead(raw=raw_bytes, mime=mime)


def _sniffs_as_raster(path: Path) -> bool:
    """Whether the first bytes of *path* are a supported raster's, read through
    the gate and bounded to the sniff window, for a file too large to read whole.

    The bounded reader refuses a hardlinked file, so such a picture earns no
    note: a missing note is the fail-safe direction, a wrong claim is not.
    """
    from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime

    try:
        head = safe_read_file_bytes_nolink(str(path), max_bytes=SNIFF_BYTES, allow_truncate=True)
    except Exception:
        return False
    if head is None or len(head) < SNIFF_BYTES:
        return False
    return sniff_raster_mime(head) in IMAGE_MEDIA_TYPES.values()


def any_inline_image(attachments: Sequence[PromptAttachment] | None) -> bool:
    """Whether the builder would inline at least one of *attachments*."""
    return any(inline_image_payload(a) is not None for a in attachments or ())

"""The image-path screen ``build_prompt_blocks`` runs before it reads a file.

Split out of :mod:`kiro_crew.acp.prompt_blocks` so a reader outside the ACP layer
(the dashboard's banner echo check) asks the builder's own question -- which
paths in this prompt could become an image block -- without a copy that drifts
from it. :func:`image_path_candidates` is lexical and cheap; only
:func:`names_an_image_file` touches the filesystem.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from kiro_crew.hooks import (
    is_unc_shape,
    screen_windows_linked_ancestors,
    unc_probe_allowed,
)
from kiro_crew.image_refs import _MASKED, _PATH_RE, mask_user_literal_text
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.prompt_segments import SOURCE_USER, segments_of

logger = logging.getLogger(__name__)

#: Raster formats kiro-cli accepts as inline vision input. SVG is deliberately
#: absent: it is scriptable XML rather than a raster image, and a vision model
#: gains nothing from it.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _image_path_candidates(
    message: str, user_spans: list[tuple[int, int]]
) -> Iterator[tuple[int, int, str]]:
    """Every image-path candidate ``build_prompt_blocks`` reads, as ``(start, end, raw)``.

    Lexical only: no filesystem call. Literal text (code, indented code, escaped
    ``\\![x](...)`` markup) is masked first, so a path written as text is not a
    candidate; offsets index straight into *message*, and only spans inside the
    user's own segments count. A fence the user left open stays unmasked: a
    channel's own attachment lines follow the user's text, and a dangling opener
    must not swallow them. Each user segment is judged on its own
    (mask_user_literal_text), so injected text ending inside an open fence
    cannot pair with the user's next fence line.
    """
    try:
        scan = mask_user_literal_text(message)
    except Exception:  # pragma: no cover - defensive: a scan must never break a turn
        logger.debug("image path scan: literal-span mask failed", exc_info=True)
        scan = message
    for match in _PATH_RE.finditer(scan):
        if _MASKED in match.group(1):
            # Only reachable through a Windows UNC host segment, whose
            # negated class admits the sentinel: a span touching literal
            # text is not a reference.
            continue
        group = match.group(1)
        raw = group.strip()
        start = match.start(1) + (len(group) - len(group.lstrip()))
        end = start + len(raw)
        if _inside(user_spans, start, end):
            yield start, end, raw


def _screened_image_file(raw: str) -> tuple[Path, str] | None:
    """The regular image file *raw* names and its suffix's MIME type, or None.

    The screen ``build_prompt_blocks`` runs before it reads a candidate: on
    Windows a UNC shape the hook policy refuses, a linked ancestor that does not
    screen clean and a link or junction leaf are never probed; a name component
    over 255 characters and any probe error read as "not a file".
    """
    # UNC-shaped candidates name a HOST on Windows: gate them before
    # any filesystem call, or is_file() below opens an SMB connection
    # to attacker-controlled text. POSIX has no such semantics (a
    # doubled leading slash is an ordinary local path), and _PATH_RE
    # is platform-gated anyway. See kiro_crew.hooks.unc_probe_allowed.
    if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
        return None
    path = Path(raw)
    suffix_mime = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if suffix_mime is None:
        # Unreachable for regex-produced candidates today (_PATH_RE's
        # suffix group and IMAGE_MEDIA_TYPES share one key set), kept
        # as the lexical backstop should the two ever drift.
        return None
    # A linked ANCESTOR defeats the lexical UNC screen above: the
    # candidate is not itself UNC-shaped -- only the link's target is
    # -- and is_file()/stat() resolve every ancestor, so the probe
    # itself would traverse the link and open the SMB connection. The
    # shared screen rewrites a local linked prefix to its vetted target
    # and refuses UNC, device, drive-relative and `..`-climbing targets;
    # every probe runs on the REWRITTEN path. Windows-only for the same
    # reason as the UNC gate: on POSIX stat-ing through a symlink is
    # harmless.
    if os.name == "nt":
        screened = screen_windows_linked_ancestors(path)
        if screened is None:
            return None
        path = screened
    # The LEAF gets the junction-aware check the walk deliberately
    # excludes: is_file() below FOLLOWS a final-component link, so a
    # leaf symlink/junction targeting a UNC share is the same probe.
    # lstat-based, so the link itself is never followed.
    if os.name == "nt" and is_link_or_junction(path):
        return None
    # A long run of prose ending in an image suffix is not a path: a
    # component over 255 characters (past every common name limit) raises
    # ENAMETOOLONG (pathlib on 3.12 does not swallow it), and one raise
    # here fails the whole turn. Skip it, and treat any probe error as
    # "not a file" so the text still goes out.
    if any(len(part) > 255 for part in path.parts):
        return None
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    return path, suffix_mime


def image_path_candidates(message: str) -> list[str]:
    """The distinct paths in *message* ``build_prompt_blocks`` would consider inlining.

    Lexical only, so it is cheap enough for every turn: whether any of them
    names a file is :func:`names_an_image_file`'s question. A plain string is
    one user segment, so every part of it is read.
    """
    return list(
        dict.fromkeys(raw for _s, _e, raw in _image_path_candidates(message, _user_spans(message)))
    )


def names_an_image_file(candidates: Iterable[str]) -> bool:
    """Whether any of *candidates* passes the builder's screen and names a regular file.

    Filesystem work: callers on the event loop run it in a worker thread. A
    candidate the builder would still keep out (an oversize file, a cap, an
    agent with no image support) counts.
    """
    return any(_screened_image_file(raw) is not None for raw in candidates)


def _user_spans(message: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every user segment of *message*, in order."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for source, segment in segments_of(message):
        if source == SOURCE_USER:
            spans.append((pos, pos + len(segment)))
        pos += len(segment)
    return spans


def _inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in spans)

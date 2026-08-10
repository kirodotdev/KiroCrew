"""Auto-register chat-emitted local images as first-class image artifacts.

The sibling of :mod:`kiro_crew.widget_artifacts`, for the other thing an agent
drops into a finalized message: a local markdown image, ``![alt](/abs/path.png)``.
When a segment is finalized we copy each referenced local raster file into an
``kind="image"`` artifact (bytes and all) so it survives the file being moved or
deleted later and shows up in the session's Artifacts tab — the same "record,
not a library entry" contract widgets get (unpinned, sweepable).

Why copy the bytes immediately rather than storing the path: the markdown points
at a file on disk that the agent (or a later cleanup) may remove, and an
artifact whose bytes vanished is worse than no artifact. So this reads the file
at finalize time and hands the bytes to :meth:`ArtifactStore.create_image`,
which owns them from then on.

Identity mirrors the widget scheme: a deterministic slug from
``(message_ts, image_index)`` via :func:`kiro_crew.widget_slug.derive_widget_slug`,
so re-finalizing / rehydrating the same message re-derives the same slug and the
existing artifact is left untouched instead of duplicated. The seed is
namespaced (``"<ts>#image"``) so an image and a widget at the same ordinal in one
message can never collapse to the same slug.

Scope guards (all deliberate, all mirror the widget path):

* http(s)/data/protocol-relative URLs are skipped — only LOCAL files are copied.
* only absolute paths to existing, readable, non-sensitive raster files
  (png/jpeg/webp/gif by extension) are registered.
* restricted (incognito/temporary) sessions register nothing — the caller gates
  on ``slot.is_restricted`` before scheduling, same as widgets.

All filesystem work here is blocking, so async callers MUST use
:func:`register_images_off_loop`, never :func:`register_images` directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from kiro_crew.artifacts import (
    MAX_AUTO_WIDGET_ARTIFACTS,
    MAX_CONTENT_BYTES,
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactValidationError,
    get_default_store,
)
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import is_sensitive_path
from kiro_crew.widget_slug import derive_widget_slug

logger = logging.getLogger(__name__)

#: Fallback display name when the markdown image had no alt text.
_DEFAULT_IMAGE_NAME = "Image"

#: Markdown inline-image OPENING: ``![alt](``. Deliberately stops at the opening
#: paren and captures no destination — the destination is parsed from the text
#: after the match by :func:`_md_destination`, which walks it with a paren
#: counter. Two reasons it is split this way: a filename like
#: ``screenshot(1).png`` is ordinary and a lazy ``[^)\s]+`` capture truncates it
#: at the inner ``)``; and a pattern that swallows to end-of-line makes
#: ``finditer`` consume every later image on the SAME line into one match, so a
#: second same-line image is never seen.
#:
#: The alt capture accepts backslash escapes (``(?:[^\]\\]|\\.)*``) because a
#: caption may legally contain an escaped bracket — ``![Revenue \[Q1\]](p.png)``.
#: A plain ``[^\]]*`` stops at that escaped ``]``, the whole pattern then fails
#: to match, and the image is never registered at all.
_IMAGE_MD_RE = re.compile(r"!\[((?:[^\]\\]|\\.)*)\]\(")

#: Local raster extensions we register, mapped to the mime create_image expects.
#: SVG is intentionally absent — it is markup (``kind="svg"``), not a raster.
_IMAGE_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


#: Unwraps a markdown backslash escape to the character it escaped, for alt text.
_MD_ESCAPE_RE = re.compile(r"\\(.)")

#: Characters a backslash may legally escape inside a markdown destination. A
#: backslash before anything else is a literal — most importantly a Windows path
#: separator (``C:\Users\me\shot.png``).
_MD_ESCAPABLE = frozenset("()[]\\<>\"'")

#: Per-message registration budgets. Auto-registration copies bytes, and one
#: finalized message can legitimately reference many images — but it can also
#: reference the same 25 MiB file a thousand times, and pruning only runs AFTER
#: the loop, so without a ceiling a single message can fill the disk. Both limits
#: are per message: whichever trips first stops registration for that message.
MAX_IMAGES_PER_MESSAGE = 12
MAX_IMAGE_BYTES_PER_MESSAGE = 64 * 1024 * 1024  # 64 MiB


def _md_destination(rest: str) -> str | None:
    """Extract a markdown link destination from the text after ``![alt](``.

    Markdown allows unescaped parentheses in a destination as long as they
    balance, which is exactly the common ``screenshot(1).png`` case. Walks to the
    closing paren tracking depth, honours backslash escapes *only* before
    markdown-significant characters, and stops at the optional ``"title"``
    suffix. Returns ``None`` when the destination never closes (malformed, or a
    ``(`` that belongs to prose).

    The narrow escape set is load-bearing on Windows: a native path is
    ``C:\\Users\\me\\shot.png``, and treating every backslash as an escape strips
    the separators, leaving a path that cannot resolve — which silently disabled
    image registration on Windows entirely.
    """
    depth = 1
    out: list[str] = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest) and rest[i + 1] in _MD_ESCAPABLE:
            # A real markdown escape: keep the escapee, and never let it move
            # the paren depth.
            out.append(rest[i + 1])
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                dest = "".join(out).strip()
                # `<...>` is markdown's explicit way to write a destination that
                # contains spaces (`![c](</tmp/generated images/c.png>)`). Unwrap
                # it and DON'T split on whitespace: inside the brackets a space
                # is part of the path, not the separator before a `"title"`.
                if dest.startswith("<"):
                    end = dest.find(">")
                    if end == -1:
                        return None  # unterminated — don't guess at the path
                    return dest[1:end].strip() or None
                # Bare destination: a `"title"` suffix is separated by
                # whitespace, so the path ends at the first space.
                if " " in dest or "\t" in dest:
                    dest = re.split(r"[ \t]", dest, maxsplit=1)[0]
                return dest or None
        out.append(ch)
        i += 1
    return None


def _derive_image_slug(message_ts: str, image_index: int) -> str:
    """Deterministic slug for an image impression, namespaced off widgets.

    Reuses :func:`derive_widget_slug` (so the hashing contract lives in one
    place) but seeds it with a ``"<ts>#image"`` message id. Without the
    namespace an image at ordinal 0 and a widget at ordinal 0 in the SAME
    message would both hash to ``derive_widget_slug(ts, 0)`` and collide; the
    namespace keeps the two id spaces disjoint while preserving the property
    that actually matters — same inputs → same slug → idempotent re-finalize.
    """
    return derive_widget_slug(f"{message_ts}#image", image_index)


def _mime_for_path(raw_path: str) -> str | None:
    """Return the raster mime for a path's extension, or ``None`` if unsupported."""
    clean = raw_path.split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(clean)[1].lower()
    return _IMAGE_EXT_MIME.get(ext)


def _local_file(raw_path: str) -> Path | None:
    """Resolve a markdown image target to a local, readable, safe file path.

    Returns ``None`` (skip) for anything that isn't a plain absolute path to an
    existing regular file we're allowed to read: relative paths (no stable
    meaning off the agent's cwd), missing files, and sensitive paths
    (``~/.aws`` etc., via the store's own denylist) are all rejected.
    """
    clean = raw_path.split("?", 1)[0].split("#", 1)[0]
    if clean.startswith("file://"):
        clean = clean[len("file://") :]
    try:
        p = Path(clean).expanduser()
        if not p.is_absolute() or not p.is_file():
            return None
        if is_sensitive_path(str(p)):
            return None
    except OSError:
        return None
    return p


def register_images(text: str, message_ts: str, session_key: str) -> list[str]:
    """Register every local markdown image in ``text``; return slugs created.

    Blocking (reads each referenced file, writes the artifact store). Idempotent:
    a slug that already exists is left untouched, so a replayed message never
    duplicates or clobbers an artifact the user has since edited.

    Never raises — a failure to register a chat image is a lost convenience, not
    a reason to fail the turn that produced it. Per-image failures (unreadable
    file, oversize, validation) are logged and skipped individually.
    """
    if not message_ts:
        # No stable identity to derive a slug from — a random slug would strand
        # the artifact (the frontend probe would never find it).
        return []
    try:
        matches = list(_IMAGE_MD_RE.finditer(text or ""))
    except Exception:  # pragma: no cover — regex scan must never break a turn
        logger.warning("image scan failed for message %s", message_ts, exc_info=True)
        return []
    if not matches:
        return []

    store = get_default_store()
    registered: list[str] = []
    copied_bytes = 0
    # Counts every image this message was ELIGIBLE to store, not just the ones
    # that succeeded. A replayed message whose slugs already exist would
    # otherwise never advance the counter, so each replay would sail past the
    # limit and store the next batch.
    considered = 0
    # Index by position among ALL image matches (including skipped remote ones)
    # so an image's ordinal is stable regardless of which siblings were skipped.
    for index, m in enumerate(matches):
        if considered >= MAX_IMAGES_PER_MESSAGE:
            logger.warning(
                "message %s references more than %d local images; registering the first %d",
                message_ts,
                MAX_IMAGES_PER_MESSAGE,
                MAX_IMAGES_PER_MESSAGE,
            )
            break
        # Undo markdown escaping so the caption reads as written: the alt capture
        # now accepts `\]`, and leaving the backslashes in would surface them in
        # the artifact name and the image's accessible description.
        alt = _MD_ESCAPE_RE.sub(r"\1", (m.group(1) or "")).strip()
        # The destination starts right after the opening paren this match ended on.
        raw_path = _md_destination(text[m.end():])
        if not raw_path:
            continue
        low = raw_path.lower()
        if low.startswith(("http://", "https://", "data:", "//")):
            # Remote / data / protocol-relative — nothing local to copy.
            continue
        mime = _mime_for_path(raw_path)
        if mime is None:
            continue  # not a supported raster extension
        path = _local_file(raw_path)
        if path is None:
            continue  # relative / missing / sensitive
        # Eligible: a local, supported, resolvable raster. Counted here — before
        # the store is consulted — so an already-registered duplicate consumes
        # budget exactly like a fresh copy.
        considered += 1
        slug = _derive_image_slug(message_ts, index)
        # Bounded, O_NOFOLLOW read validated against the inode actually opened:
        # reading the whole file first would allocate an unbounded amount before
        # the store's size check, and an lstat-then-open split leaves a window
        # where the file is swapped for a link to a sensitive one.
        try:
            data = safe_read_file_bytes_nolink(str(path), max_bytes=MAX_CONTENT_BYTES)
        except FileTooLargeError:
            logger.warning("auto-register image %s exceeds the size cap; skipped", path)
            continue
        except OSError as exc:
            logger.warning("auto-register image read failed for %s: %s", path, exc)
            continue
        if data is None:
            # Rejected: unreadable, hardlinked, non-regular, or sensitive.
            continue
        if copied_bytes + len(data) > MAX_IMAGE_BYTES_PER_MESSAGE:
            # Checked BEFORE the copy, so the budget bounds bytes written rather
            # than reporting after the fact.
            logger.warning(
                "message %s exceeds the %d-byte image budget; stopping after %d image(s)",
                message_ts,
                MAX_IMAGE_BYTES_PER_MESSAGE,
                len(registered),
            )
            break
        try:
            store.create_image(
                name=alt or _DEFAULT_IMAGE_NAME,
                image_bytes=data,
                mime=mime,
                slug=slug,
                source="chat",
                session_key=session_key,
                auto_registered=True,
                alt=alt,
                original_filename=path.name,
            )
        except ArtifactAlreadyExistsError:
            # Already registered (message re-finalized, or the user starred it
            # before this ran). Do NOT overwrite.
            continue
        except (ArtifactValidationError, ArtifactError, OSError) as exc:
            logger.warning("auto-register failed for image %s: %s", slug, exc)
            continue
        registered.append(slug)
        # Count only bytes actually written: a skipped duplicate (slug already
        # exists) copies nothing and must not consume the budget.
        copied_bytes += len(data)

    if registered:
        # Image artifacts are auto_registered=True, so they ride the SAME
        # unpinned-widget sweep — its predicate is kind-agnostic.
        try:
            pruned = store.prune_auto_widgets(keep=MAX_AUTO_WIDGET_ARTIFACTS)
            if pruned:
                logger.info("pruned %d unpinned auto-registered artifacts", pruned)
        except (ArtifactError, OSError) as exc:
            logger.warning("auto-artifact prune failed: %s", exc)
    return registered


async def register_images_off_loop(text: str, message_ts: str, session_key: str) -> list[str]:
    """Async wrapper: run :func:`register_images` off the event loop.

    Registration reads files and writes the artifact store, so it must never run
    on the gateway's event loop (see the module docstring).

    Uses ``asyncio.to_thread`` rather than the shared subprocess executor on
    purpose. That executor is sized for subprocess work, and a wedged worker
    there would leave this registration queued behind it — by which time the
    source file (typically a temp file) can already be gone, so the image is lost
    permanently rather than late. This work is short, filesystem-bound, and has
    no reason to share a queue with subprocess teardown.
    """
    return await asyncio.to_thread(register_images, text, message_ts, session_key)

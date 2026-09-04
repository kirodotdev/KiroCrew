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
from kiro_crew.messaging.outbound_files import (
    IMAGE_MD_RE,
    REMOTE_PREFIXES,
    local_destination,
    md_destination,
    strip_url_syntax,
    unescape_md,
)
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction
from kiro_crew.security import is_sensitive_path
from kiro_crew.widget_slug import derive_widget_slug

logger = logging.getLogger(__name__)

#: Fallback display name when the markdown image had no alt text.
_DEFAULT_IMAGE_NAME = "Image"

#: Local raster extensions we register, mapped to the mime create_image expects.
#: SVG is intentionally absent -- it is markup (``kind="svg"``), not a raster.
_IMAGE_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

#: Per-message registration budgets. Auto-registration copies bytes, and one
#: finalized message can legitimately reference many images — but it can also
#: reference the same 25 MiB file a thousand times, and pruning only runs AFTER
#: the loop, so without a ceiling a single message can fill the disk. Both limits
#: are per message: whichever trips first stops registration for that message.
MAX_IMAGES_PER_MESSAGE = 12
MAX_IMAGE_BYTES_PER_MESSAGE = 64 * 1024 * 1024  # 64 MiB


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
    ext = os.path.splitext(strip_url_syntax(raw_path))[1].lower()
    return _IMAGE_EXT_MIME.get(ext)


def _local_file(raw_path: str) -> Path | None:
    """Resolve a markdown image target to a local, readable, safe file path.

    Returns ``None`` (skip) for anything that isn't a plain absolute path to an
    existing regular file we're allowed to read: relative paths (no stable
    meaning off the agent's cwd), missing files, and sensitive paths
    (``~/.aws`` etc., via the store's own denylist) are all rejected. The
    normalize-and-absolute half is :func:`local_destination`.
    """
    p = local_destination(raw_path)
    if p is None:
        return None
    # A linked ANCESTOR defeats local_destination's lexical UNC screen: the
    # destination is not itself UNC-shaped -- only the link's target is --
    # and is_file()/is_sensitive_path below both resolve every ancestor, so
    # the probe itself would traverse the link and open the SMB connection.
    # Same guard as the upload-side consumer (_inspect in outbound_files);
    # local_destination stays lexical by contract, so each consumer screens
    # its own probes. Windows-only: on POSIX stat-ing through a symlink is
    # harmless. Reference wiring:
    # dashboard/handlers/themes.py::_resolve_local_source.
    if os.name == "nt" and first_linked_ancestor(p) is not None:
        return None
    # The LEAF gets the junction-aware check the walk deliberately excludes:
    # is_file() below FOLLOWS a final-component link, so a leaf
    # symlink/junction targeting a UNC share is the same probe. lstat-based,
    # never follows.
    if os.name == "nt" and is_link_or_junction(p):
        return None
    try:
        if not p.is_file():
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
        matches = list(IMAGE_MD_RE.finditer(text or ""))
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
        alt = unescape_md(m.group(1) or "").strip()
        # The destination starts right after the opening paren this match ended on.
        raw_path = md_destination(text[m.end():])
        if not raw_path:
            continue
        low = raw_path.lower()
        if low.startswith(REMOTE_PREFIXES):
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

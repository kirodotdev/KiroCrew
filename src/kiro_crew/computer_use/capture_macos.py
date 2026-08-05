"""Window capture: pixels to a persisted, size-bounded JPEG. Fully in-process.

**No subprocess and no image library.** Not ``/usr/sbin/screencapture``, not
Pillow. ``CGWindowListCreateImage`` grabs one window's pixels and ImageIO's
``CGImageDestination`` encodes them, with ImageIO performing the downscale itself
via ``kCGImageDestinationImageMaxPixelSize``. Three liabilities disappear with
that choice: there is no spawn node for the spawn audit to account for, no
optional dependency to degrade around (Pillow is declared in neither
``setup.cfg`` nor ``pyproject.toml``), and no 204ms process launch per capture.

Why the image is a PATH and never bytes. The MCP transport
(``validation.build_tool_response``) emits ``{"content":[{"type":"text",...}]}``
and cannot express an image block, so a relayed screenshot would have to be
base64 in text — measured at ~41,000 tokens for a single raw window PNG. The
compressed file goes to disk and only its path is relayed, which the model reads
with ``fs_read`` if and only if the accessibility tree was insufficient. At
1280px/q0.55 a real window encodes to ~24KB (~8,300 tokens if read at all), and
the image is corroboration rather than the primary channel.

Size reporting comes from :func:`macos_ffi.jpeg_dimensions` — the ENCODED
dimensions, parsed back out of the JPEG. ``CGImageGetWidth`` on the *input* image
would over-report by the downscale factor (verified: a 1676x1320 window encodes to
1280x1008), and a wrong size in the result would make a model reason about a
resolution it is not going to get.

The encoded frame has exactly one other consumer: :mod:`screencast` relays these
same bytes to the dashboard's floating live view. It is a relay, not a second
capture — no extra ``CGWindowListCreateImage`` call, no timer, no full-screen
grab — and it re-checks ``has_secure`` plus the permitted screenshot channel
before anything leaves the process.

Accepted residual risk, stated here because a reviewer will find it: the persisted
JPEGs live in a ``0o700`` temp dir the agent can reach with ``fs_read``. That is
the posture the browse module already ships. Computer use widens WHAT can be in
frame (any window, not one browser tab), which is why capture is per-window and
never full-screen, why a window holding any secure field is not captured at all,
and why the directory is ring-trimmed. It is not claimed to be closed.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import replace

from kiro_crew import platform_compat
from kiro_crew.computer_use import macos_ffi, screencast
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY,
    DEFAULT_SCREENSHOT_MAX_PX,
    MAX_SCREENSHOT_MAX_PX,
    MIN_SCREENSHOT_MAX_PX,
    SCREENSHOT_FILE_PREFIX,
    SCREENSHOT_FILE_SUFFIX,
    SCREENSHOT_KEEP,
    AppRef,
    Snapshot,
)

logger = logging.getLogger(__name__)

# Owner-only. The directory holds pixels of the operator's own windows, so no
# other local account may list or read it.
_SHOT_DIR_MODE = 0o700
# ImageIO takes quality as 0.0-1.0; the config field is the 0-100 integer users
# and the dashboard understand.
_QUALITY_SCALE = 100.0
# Millisecond timestamp in the filename: two captures inside the same second are
# routine (a get_state followed by a mutating action's re-snapshot) and a
# second-resolution name would collide and silently overwrite.
_TIMESTAMP_SCALE = 1000


def shots_dir() -> str:
    """Path to the screenshot directory (no side effects).

    Resolved through :func:`macos_ffi.shots_dir_default`, which builds it from
    ``tempfile.gettempdir()`` rather than a hardcoded ``/tmp`` — the same idiom
    ``mcp_playwright_proxy`` uses, and the reason a Windows port would not need an
    edit here.
    """
    return macos_ffi.shots_dir_default()


def ensure_shots_dir() -> str:
    """Create the screenshot directory ``0o700`` and return its path.

    ``mode=`` on ``makedirs`` is applied by the OS through the umask, so the mode
    is re-asserted with :func:`platform_compat.chmod_safe` afterwards. Without
    that a permissive umask would leave the directory group- or world-readable and
    the whole point of the mode would be lost.
    """
    path = shots_dir()
    os.makedirs(path, mode=_SHOT_DIR_MODE, exist_ok=True)
    try:
        platform_compat.chmod_safe(path, _SHOT_DIR_MODE)
    except OSError:
        # Warn and continue: a directory whose mode cannot be tightened is a real
        # concern the operator should see, but refusing to capture would disable a
        # feature over a filesystem that may not support modes at all.
        logger.warning("could not restrict computer-use screenshot dir %s to owner-only", path)
    return path


def capture_snapshot_image(
    snap: Snapshot,
    *,
    max_px: int = DEFAULT_SCREENSHOT_MAX_PX,
    quality: int = DEFAULT_SCREENSHOT_JPEG_QUALITY,
) -> Snapshot:
    """Capture *snap*'s window, persist the JPEG, and return an updated snapshot.

    Returns the snapshot UNCHANGED (no image) when:

    * the snapshot contains any secure element — the always-on floor. A password
      field's rendered glyphs are a credential even though the tree redacted its
      value, and there is no reliable way to blank a sub-rectangle of an
      already-encoded JPEG, so suppression is whole-window. The renderer says so
      explicitly rather than silently omitting the line, because a model that
      asked for pixels and got none retries in a loop unless it is told the
      omission was deliberate;
    * the window id is unknown;
    * the capture or encode produced no bytes (a closed window yields a NULL image
      — verified, not a crash);
    * persisting failed.

    Never raises. The accessibility tree is the primary channel, so a capture
    failure must degrade the result rather than fail the observation.
    """
    if snap.has_secure:
        return snap
    # FAIL CLOSED on an incomplete scan. ``has_secure`` is set for every node the
    # walk CLASSIFIED — which now includes nodes past the reporting budget — but the
    # walk still has hard cutoffs of its own (``MAX_TREE_NODES_LIMIT`` nodes and the
    # ``MAX_WALK_SECS`` deadline). If either fired, the walk never reached the end of
    # the window, so ``has_secure=False`` means "none seen", NOT "none present": a
    # password field beyond the cutoff would leave rendered credentials capturable.
    #
    # "Unknown" therefore has to behave like "present" here. This is the one gate
    # that decides whether pixels leave the process, and the cost of being wrong in
    # each direction is not symmetric — a suppressed screenshot costs the model one
    # announced omission (the renderer says so, so it does not retry blindly), while
    # a wrong capture photographs somebody's password box.
    if snap.truncated or snap.depth_truncated:
        return snap
    if snap.app.window_id <= 0:
        return snap

    raw, width, height = _encode_window(snap.app, max_px=max_px, quality=quality)
    if not raw:
        return snap

    path = persist_jpeg(raw)
    if not path:
        return snap
    # ``dataclasses.replace``, NOT a field-by-field rebuild. Enumerating the fields
    # here made this function silently lossy: every field added to ``Snapshot``
    # afterwards was dropped whenever a screenshot was attached, and the shape of the
    # loss is what made it nasty — the SAME snapshot without a screenshot carried
    # them fine, so it would only misbehave on responses that also carry an image.
    # (Caught with ``window_bounds``/``selected_text``, whose absence would delete the
    # element-frame origin line from exactly those responses.) ``replace`` cannot go
    # stale; ``test_attaching_a_screenshot_preserves_EVERY_other_snapshot_field``
    # pins it field-by-field over the whole dataclass.
    captured = replace(
        snap,
        image_jpeg=raw,
        image_path=path,
        image_width=width,
        image_height=height,
    )
    # Mirror the frame we JUST encoded to the dashboard's live view. Nothing is
    # captured for the mirror — it relays these exact already-downscaled bytes —
    # and ``emit_snapshot_frame`` owns all three suppressions (no published
    # surface scope, a secure window, a withheld screenshot channel). It does not
    # block: the POST runs on a daemon thread, so a dead
    # gateway cannot slow the observation the model asked for.
    #
    # Wrapped anyway, even though the relay is itself contracted never to raise:
    # this function's OWN contract is "never raises", and a decorative mirror must
    # not be able to turn a successful observation into a failed tool call if that
    # inner contract is ever broken.
    try:
        screencast.emit_snapshot_frame(captured)
    except Exception:
        logger.debug("computer-use live-view relay failed", exc_info=True)
    return captured


def _encode_window(app: AppRef, *, max_px: int, quality: int) -> tuple[bytes, int, int]:
    """Capture + encode one window. Returns ``(bytes, width, height)``.

    Clamps *max_px* and *quality* here rather than trusting the caller: the MCP
    schemas validate agent input, but this function is also reachable from config,
    and a zero or negative ``max_px`` handed to ImageIO would produce either a
    degenerate image or an unbounded one.
    """
    clamped_px = max(MIN_SCREENSHOT_MAX_PX, min(int(max_px), MAX_SCREENSHOT_MAX_PX))
    clamped_quality = max(1, min(int(quality), 100)) / _QUALITY_SCALE
    try:
        return macos_ffi.capture_window_jpeg(
            app.window_id, max_px=clamped_px, quality=clamped_quality
        )
    except Exception:
        logger.debug("window capture failed for %s", app.label, exc_info=True)
        return b"", 0, 0


def persist_jpeg(raw: bytes) -> str:
    """Write *raw* into the screenshot dir and return its path, or ``""``.

    Owner-only on the file as well as the directory
    (:func:`platform_compat.restrict_to_owner`, which is fail-loud): defence in
    depth for the case where the directory mode could not be applied.

    Ring-trims after writing so the directory is bounded whatever happens next —
    trimming first would leave the cap violated by exactly one file for the
    lifetime of a session that then crashed.
    """
    if not raw:
        return ""
    try:
        directory = ensure_shots_dir()
        # ATOMIC unique allocation, not a millisecond timestamp. The gateway
        # offloads snapshots to a thread pool, so two captures of DIFFERENT apps can
        # land in the same millisecond; a timestamp-only name then resolves to one
        # path and both writers open it — the second overwrites the first and one
        # caller is handed a screenshot of an application it never asked about
        # (a cross-app pixel leak, not merely a lost file).
        #
        # ``mkstemp`` also creates the file 0o600 from the outset, so there is no
        # window in which it exists world-readable before ``restrict_to_owner``
        # runs. The timestamp stays in the prefix because the ring trim orders by
        # mtime and a human reading the spool wants it.
        prefix = f"{SCREENSHOT_FILE_PREFIX}{int(time.time() * _TIMESTAMP_SCALE)}-"
        handle_fd, path = tempfile.mkstemp(
            prefix=prefix, suffix=SCREENSHOT_FILE_SUFFIX, dir=directory
        )
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(raw)
    except OSError:
        logger.warning("could not persist computer-use screenshot", exc_info=True)
        return ""
    try:
        platform_compat.restrict_to_owner(path)
    except OSError:
        # Warn and continue — the same posture every other secret-bearing writer
        # in this repo takes. The file is already inside a 0o700 directory.
        logger.warning("could not restrict computer-use screenshot %s to owner-only", path)
    trim_shots_dir()
    return path


def trim_shots_dir(keep: int = SCREENSHOT_KEEP) -> int:
    """Delete all but the newest *keep* screenshots. Returns the number removed.

    The directory is a cache, not an archive: a long session must not be able to
    fill the temp volume. Ordered by filename rather than by mtime — the names
    carry a millisecond timestamp, so a lexical sort IS chronological and needs no
    ``stat`` per file.

    Never raises: a file another process removed concurrently, or one we cannot
    delete, is skipped.
    """
    if keep <= 0:
        return 0
    directory = shots_dir()
    try:
        names = sorted(
            name
            for name in os.listdir(directory)
            if name.startswith(SCREENSHOT_FILE_PREFIX) and name.endswith(SCREENSHOT_FILE_SUFFIX)
        )
    except OSError:
        return 0
    removed = 0
    for name in names[: max(0, len(names) - keep)]:
        try:
            os.unlink(os.path.join(directory, name))
            removed += 1
        except OSError:
            logger.debug("could not trim screenshot %s", name, exc_info=True)
    return removed


__all__ = [
    "capture_snapshot_image",
    "ensure_shots_dir",
    "persist_jpeg",
    "shots_dir",
    "trim_shots_dir",
]

"""Atomic file write using unique temp filenames to avoid race conditions.

All atomic-write sites in KiroCrew should use this helper instead of
deterministic ``.tmp`` filenames, which cause ENOENT when concurrent
writers target the same file.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: What to do when the owner-only lockdown cannot be applied.
#:
#: ``"raise"`` refuses to write a secret it cannot protect. ``"warn"`` logs and
#: writes anyway. Both are deliberate, established conventions in this codebase,
#: which is why this is a parameter and not a fixed policy: ``webhooks.py`` and
#: ``dashboard/token_auth.py`` let the OSError propagate, while ``sel.py`` and
#: ``dashboard/refresh_tokens.py`` catch it and continue, because a read-only
#: filesystem must not brick SecurityEventLog init or stop refresh-token state
#: from being persisted. Losing reuse-detection state is a worse outcome there
#: than a file whose permissions could not be tightened.
RestrictErrorPolicy = Literal["raise", "warn"]

_umask_lock = threading.Lock()
_default_mode: int | None = None

# Bounded retry budget for the Windows rename window. ``os.replace`` on Windows
# raises ``PermissionError`` when ANY other handle is open on either path, and a
# just-created temp file is exactly what a Search-indexer or AV scanner reaches
# for, so the rename can fail with WinError 5 / 32 / 33 while nothing is wrong.
# POSIX imposes no such restriction, so the retry is Windows-only: a
# PermissionError there is a genuine permission fault and must surface at once
# rather than after a second of sleeping.
#
# Shape mirrors the create-race retry in ``dashboard/token_secret.py``. A
# scanner hold is short but not instantaneous, so this trades ~0.45s of
# worst-case added latency on a doomed write against surviving the common
# transient. The numbers are a heuristic, not a measured hold-time distribution.
_REPLACE_MAX_ATTEMPTS = 10
_REPLACE_BACKOFF_SECONDS = 0.05


def _get_default_mode() -> int:
    """Return umask-based default file mode, cached after first call (thread-safe)."""
    global _default_mode
    if _default_mode is None:
        with _umask_lock:
            if _default_mode is None:
                u = os.umask(0)
                os.umask(u)
                _default_mode = 0o666 & ~u
    return _default_mode


def _encode(content: str | bytes, *, newline: str | None) -> bytes:
    """Return the exact bytes the replaced ``open()`` would have put on disk.

    Encoding goes through :class:`io.TextIOWrapper` rather than
    ``str.encode("utf-8")`` so the ``newline=`` contract stays byte-for-byte
    identical: ``None`` means translate ``\\n`` to ``os.linesep``, so a plain
    encode would silently stop emitting CRLF on Windows for every caller that
    does not pass ``newline`` explicitly. Delegating keeps that translation
    table in the stdlib instead of reimplementing it here.
    """
    if isinstance(content, bytes):
        return content
    buffer = io.BytesIO()
    encoder = io.TextIOWrapper(buffer, encoding="utf-8", newline=newline, write_through=True)
    encoder.write(content)
    encoder.flush()
    encoder.detach()  # unhook the wrapper only; the BytesIO stays open
    return buffer.getvalue()


def _write_all(fd: int, data: bytes, path: Path) -> None:
    """Write every byte of *data* to *fd*, or raise.

    ``write(2)`` may transfer fewer bytes than requested and report the count
    with no error, so one unchecked call can publish a truncated file. Looping
    alone is not sufficient either: :class:`io.BufferedWriter` loops, but it
    retries a raw write reporting 0 bytes *forever*, so a raw layer making no
    progress hangs the caller instead of failing it (measured: a buffered write
    over such a raw never returns). Treat a persistent 0 as the error it is.
    This is the shape ``sel.py`` already used by hand for the SEL HMAC key, for
    exactly this reason.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError(
                f"short write persisting {path}: os.write reported 0 bytes with "
                f"{len(view)} of {len(data)} still pending"
            )
        view = view[written:]


def _on_event_loop() -> bool:
    """Whether this thread is currently running an asyncio event loop.

    Mirrors the probe guarding ``CronService``'s store lock: a worker started by
    ``asyncio.to_thread`` or ``run_in_executor`` has no running loop of its own,
    so a caller that offloads its write keeps the retry while the loop thread
    itself never sleeps.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def replace_with_retry(src: Path | str, dst: Path | str) -> None:
    """``os.replace(src, dst)``, retrying the Windows sharing-violation window.

    Atomic replacement is the last step of every tmp-file-plus-rename writer. On
    Windows that rename fails with ``PermissionError`` if any other handle is
    open on either path. An indexer or an AV scanner touching the freshly
    written temp file is enough, so a correct atomic write can still lose its
    payload for reasons unrelated to the caller. Retry a bounded number of times
    so the transient resolves instead of propagating.

    On POSIX this is a plain ``os.replace``: the OS permits replacing an open
    file, so a ``PermissionError`` means the caller genuinely cannot write there
    and is re-raised immediately rather than slept over.

    The retry sleeps, so it is gated on there being no running event loop in
    this thread. A caller reached from the gateway loop gets the plain
    ``os.replace`` semantics it had before this retry existed: the
    ``PermissionError`` propagates on the first attempt rather than pausing the
    single loop for the whole budget (``no-blocking-call-on-event-loop``). This
    is a property of this function, so it holds no matter how many sync helpers
    sit between a coroutine and this call. Callers wanting the retry on a
    loop-driven path offload the write (``asyncio.to_thread`` /
    ``run_in_executor``), as ``AutoNudgeService`` already does; the worker has no
    loop of its own, so the retry applies there.

    The final attempt sits OUTSIDE the retry loop on purpose. With it inside,
    a budget of 0 would skip the body entirely and return having renamed
    nothing, which every caller reads as success: a silently lost write. Out
    here, any budget of 1 or less simply degrades to a plain ``os.replace``.
    """
    for attempt in range(_REPLACE_MAX_ATTEMPTS - 1):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if not platform_compat.IS_WINDOWS:
                raise
            if _on_event_loop():
                logger.debug(
                    "atomic rename contended at %s on the event loop; "
                    "re-raising instead of sleeping (offload the write to retry)",
                    dst,
                )
                raise
            logger.debug(
                "atomic rename contended at %s; retrying (attempt %d/%d)",
                dst,
                attempt + 1,
                _REPLACE_MAX_ATTEMPTS,
            )
            time.sleep(_REPLACE_BACKOFF_SECONDS)
    os.replace(str(src), str(dst))


def atomic_write(
    path: Path | str,
    content: str | bytes,
    *,
    fsync: bool = False,
    mode: int | None = None,
    newline: str | None = None,
    restrict_to_owner: bool = False,
    restrict_on_error: RestrictErrorPolicy = "raise",
) -> None:
    """Write *content* to *path* atomically via unique temp file + rename.

    Uses ``tempfile.mkstemp`` so concurrent writers never collide on the
    same temp filename.  On error the temp file is cleaned up.

    *content* may be ``str`` (written UTF-8 encoded in text mode) or ``bytes``
    (written verbatim in binary mode). Binary mode exists for callers whose
    payload is not text at all — a compiled helper binary, an archive — which
    previously had to hand-roll the temp-write-and-rename and so silently
    missed the Windows rename retry above.

    *mode* sets explicit permissions (e.g. ``0o600`` for secrets).
    ``None`` (default) applies umask-based permissions (matching ``open()``).

    *newline* is passed straight to ``open()``. The default (``None``) applies
    universal-newline translation, which rewrites ``\\n`` to ``\\r\\n`` on
    Windows. Pass ``""`` when the content must land on disk byte-for-byte —
    e.g. a document that is read back, edited and saved again, where
    translation on every save would accumulate carriage returns. It is
    meaningless for ``bytes`` content, which is never translated, so passing
    both raises rather than silently ignoring the argument.

    *restrict_to_owner* locks the file down to its owner for secret-bearing
    payloads (credentials, HMAC keys, tokens). It is NOT the same as
    ``mode=0o600``: ``fchmod_safe`` is a documented no-op on Windows, so
    ``mode`` alone leaves a Windows temp readable at its inherited DACL for the
    whole write. This applies
    :func:`platform_compat.restrict_to_owner` to the temp file BEFORE any
    content reaches it — the ordering the hand-rolled sites already use — so
    the secret never exists in a world-readable file. It also implies
    ``0o600`` on POSIX, hence the conflict check below: passing a wider
    explicit *mode* alongside it is a caller bug, and narrowing it silently
    would hide that.

    *restrict_on_error* selects what happens when that lockdown fails, and only
    means anything alongside ``restrict_to_owner=True``. The default ``"raise"``
    refuses to write a secret it cannot protect. ``"warn"`` logs and writes
    anyway, for the callers whose own comments say the write matters more than
    the permissions: ``sel.py`` must not brick SecurityEventLog init on a
    read-only filesystem, and ``dashboard/refresh_tokens.py`` must not drop
    refresh-token reuse-detection state. Note the asymmetry the two platforms
    give ``"warn"``: on POSIX ``restrict_to_owner`` is ``chmod(0o600)``, which
    the ``fchmod_safe`` below repeats, so the file still lands at ``0o600``
    after a warn; on Windows ``fchmod_safe`` is a no-op, so a warn genuinely
    publishes the file under its inherited ACL. That is the exposure those
    callers accept today, stated rather than implied.
    """
    binary = isinstance(content, bytes)
    if binary and newline is not None:
        raise TypeError("newline is a text-mode concept and cannot apply to bytes content")
    if restrict_to_owner and mode is not None and mode != 0o600:
        raise ValueError(
            f"restrict_to_owner implies 0o600; refusing to also honour mode={mode:#o}"
        )
    if restrict_on_error != "raise" and not restrict_to_owner:
        # Reject rather than ignore: a caller passing this without asking for the
        # lockdown believes they configured a failure policy for something that
        # never runs, which reads as "permissions are handled" at the call site.
        raise ValueError(
            f"restrict_on_error={restrict_on_error!r} is meaningless without "
            "restrict_to_owner=True"
        )
    # restrict_to_owner wins: fchmod must not widen the file back to the umask
    # default after the lockdown has been applied.
    effective_mode = 0o600 if restrict_to_owner else mode
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        if restrict_to_owner:
            # Before fdopen, matching the shipping order in webhooks.py and
            # mcp_gateway/rewriter.py: the DACL lands while the file is still
            # empty, so a secret never exists in a readable file.
            try:
                platform_compat.restrict_to_owner(tmp)
            except OSError:
                if restrict_on_error == "raise":
                    raise
                # Logs the DESTINATION path, never the temp name and never
                # *content*. The temp name is an internal detail an operator
                # cannot act on; the destination is the file whose permissions
                # they need to check.
                logger.warning(
                    "atomic_write: could not apply owner-only permissions to %s; "
                    "writing it anyway per restrict_on_error='warn' — the file "
                    "may be readable by other users",
                    path,
                    exc_info=True,
                )
        # No-op on Windows (no POSIX permission bits / os.fchmod).
        platform_compat.fchmod_safe(
            fd, effective_mode if effective_mode is not None else _get_default_mode()
        )
        _write_all(fd, _encode(content, newline=newline), path)
        if fsync:
            os.fsync(fd)
        # Close BEFORE the rename: on Windows os.replace cannot swap a file that
        # still has an open handle. Clear fd first so the except branch below
        # cannot double-close if this close is itself what fails.
        fd, open_fd = -1, fd
        os.close(open_fd)
        replace_with_retry(tmp, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

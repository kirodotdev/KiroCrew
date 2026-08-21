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


def read_bytes_with_retry(path: Path | str) -> bytes:
    """``Path.read_bytes()``, retrying the Windows sharing-violation window.

    The read-side twin of :func:`replace_with_retry`, and the same OS fact seen
    from the other end: on Windows a read fails with ``PermissionError``
    (``WinError 32``) while another handle holds the file open for write, so a
    reader can lose to a concurrent tmp-file-plus-rename writer that is
    perfectly correct. POSIX permits the read, which is why this class of bug
    only ever surfaces on the ``Backend Tests (Windows)`` matrix and on Windows
    hosts.

    Only ``PermissionError`` is retried. ``FileNotFoundError`` and a decode or
    parse failure propagate untouched: they mean the file is absent or damaged,
    and sleeping cannot change either.

    Shares :data:`_REPLACE_MAX_ATTEMPTS` / :data:`_REPLACE_BACKOFF_SECONDS` with
    the rename retry deliberately. Both bound the same transient — one Windows
    sharing-violation window — so a second knob would only let the two halves of
    one behaviour drift apart.

    On POSIX a ``PermissionError`` is a genuine access fault and is re-raised
    immediately rather than slept over, and the retry is gated on there being no
    running event loop in this thread: a caller reached from the gateway loop
    gets the plain single-attempt semantics instead of pausing the one loop for
    the whole budget. Callers wanting the retry from a loop-driven path offload
    the read (``asyncio.to_thread`` / ``run_in_executor``), which is what
    ``CrewStore``'s builder already does.

    The final attempt sits OUTSIDE the loop for the same reason it does in
    :func:`replace_with_retry`: with it inside, a budget of 0 would fall out
    having read nothing and return ``None`` to a caller expecting bytes.
    """
    target = Path(path)
    for attempt in range(_REPLACE_MAX_ATTEMPTS - 1):
        try:
            return target.read_bytes()
        except PermissionError:
            if not platform_compat.IS_WINDOWS:
                raise
            if _on_event_loop():
                logger.debug(
                    "read contended at %s on the event loop; re-raising instead "
                    "of sleeping (offload the read to retry)",
                    target,
                )
                raise
            logger.debug(
                "read contended at %s; retrying (attempt %d/%d)",
                target,
                attempt + 1,
                _REPLACE_MAX_ATTEMPTS,
            )
            time.sleep(_REPLACE_BACKOFF_SECONDS)
    return target.read_bytes()


def _resolved_or_none(path: Path) -> Path | None:
    """``path.resolve()``, or ``None`` when the platform cannot resolve it.

    A symlink loop or a vanished component ends here. Both exception types are
    caught on purpose: ``Path.resolve()`` raises ``OSError`` for most failures,
    but on Python 3.10 a symlink LOOP raises ``RuntimeError`` instead (pathlib
    only moved that path onto ``os.path.realpath`` in 3.11), and this repo still
    supports 3.10. Letting that escape would crash the caller -- a Discord
    resume write, a token store -- where the whole point of this helper is to
    turn "cannot prove where the write lands" into a refusal.

    Callers treat ``None`` as "cannot prove", which is a refusal on the write
    path rather than a pass.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _owned_roots() -> tuple[Path, ...]:
    """The directory roots Kiro Crew itself creates and owns.

    Resolution goes through ``config.paths`` lazily: importing it at module scope
    would tie this leaf helper to the config package, and calling it at import
    time would resolve the data home as a side effect of importing a writer.
    Every entry is best-effort -- a host where one cannot be resolved simply
    contributes no anchor rather than failing the write. ``kiro_home()`` resolves
    its override, so it can raise the same ``RuntimeError`` a looped link gives
    :func:`_resolved_or_none`.

    ``data_home()`` performs start-of-process maintenance (a ``mkdir``, and a
    recovery-breadcrumb refresh) on the FIRST resolution in a process, so a
    secret write before ``ensure_data_home()`` can trigger it from here. The
    ``mkdir`` is subsumed by this write's own ``path.parent.mkdir`` a few lines
    later; the breadcrumb is a stat plus one small write, once per process.
    """
    from kiro_crew.config import paths as config_paths

    roots: list[Path] = []
    for resolver in (config_paths.data_home, config_paths.legacy_home, config_paths.kiro_home):
        try:
            roots.append(Path(resolver()))
        except (OSError, RuntimeError, ValueError):  # pragma: no cover - defensive
            continue
    return tuple(roots)


def _link_trust_anchor(parent: Path) -> tuple[Path, tuple[str, ...]] | None:
    """Split *parent* into (anchor, the names below it), or ``None``.

    The anchor is where "a link here is not ours" starts being true. At or ABOVE
    it a link is the operator's own deployment choice and must keep working: a
    symlinked ``$HOME`` (``/home/u -> /local/home/u``) or a data home relocated
    onto another disk are both supported, and ``config/loader.py`` documents a
    symlinked ``config.json`` as a normal setup. BELOW the anchor every
    directory is created by Kiro Crew's own ``mkdir`` calls, so a link there was
    planted by something else.

    The anchor comes back in *parent*'s own LEXICAL namespace together with the
    names below it, so ``anchor.joinpath(*names) == parent``. An anchor handed
    back in the resolved namespace instead would let a link BELOW it satisfy an
    anchor comparison merely by pointing at the anchor, which is the shape that
    slipped through the first version of this guard.

    The SHALLOWEST matching depth wins, so a component that maps onto an owned
    root while a real owned root sits above it in the same chain cannot claim to
    be the anchor and hide itself from the walk.

    Containment is tried lexically FIRST and only then against the resolved
    parent. Order matters: for a path lexically inside an owned tree the lexical
    reading is the truthful one, while its resolved form may have been bent
    elsewhere by exactly the link this guard looks for. The resolved attempt
    exists for the other case, a caller naming the tree through an outside alias
    such as a data home reached by its symlink.
    """
    roots = _owned_roots()
    if not roots:
        return None
    lexical = parent if parent.is_absolute() else Path(os.path.abspath(parent))
    resolved = _resolved_or_none(lexical)
    for candidate in (lexical, resolved):
        if candidate is None:
            continue
        best: int | None = None
        for root in roots:
            if candidate == root or root in candidate.parents:
                depth = len(candidate.parts) - len(root.parts)
                if best is None or depth < best:
                    best = depth
        if best is None:
            continue
        names = lexical.parts[len(lexical.parts) - best :] if best else ()
        anchor = lexical.parents[best - 1] if best else lexical
        return anchor, names
    return None


def _refuse_linked_parent(path: Path) -> None:
    """Refuse to write a secret whose parent chain passes through a link.

    ``mkdir(parents=True)``, ``mkstemp(dir=...)`` and ``os.replace`` all follow
    every component except the final one, so a symlink (or Windows junction)
    pre-planted at the destination's parent — or at any ancestor below the
    trust anchor — silently redirects the whole write: the secret lands under
    whatever the link points at, outside the sensitive-path fence that is the
    only real boundary against a same-UID reader, and the caller sees success.
    Issue #4381 is the class report; a per-caller check was rejected there as
    whack-a-mole, so the refusal lives in the one helper every secret write
    already goes through.

    Two checks, because neither alone is sufficient:

    * an ``lstat`` walk over the components BELOW the anchor, which is the only
      thing that sees a Windows junction (``is_link_or_junction``, since
      ``islink`` reports False for one);
    * and the resolved parent must EQUAL the path rebuilt from the resolved
      anchor and those same names. Containment would not do: a link pointing at
      another directory INSIDE the owned tree resolves to a contained path and
      would pass while still landing the secret somewhere the caller never
      named. Equality also covers a redirect the walk cannot see, such as a
      reparse point a platform's ``realpath`` follows but ``islink`` misses.

    Only the parent CHAIN is checked. A link at the leaf is not a redirect:
    ``os.replace`` does not follow the final component, so it replaces the link
    itself with the new file (verified — the link's target keeps its old
    contents), which is the same outcome as writing over a regular file.

    lstat-based, so it is not race-free: a link planted between this check and
    the ``mkstemp`` below still wins. Closing that would need an ``O_NOFOLLOW``
    descent with a directory handle per component, which ``tempfile`` cannot be
    driven through. ``memory.py``'s lock-path check states the same limitation
    for the same reason; refusing a link that is ALREADY there removes the
    pre-planting shape the report is about, which is the shape an attacker can
    set up at leisure.
    """
    parent = path.parent
    split = _link_trust_anchor(parent)
    if split is None:
        # Outside every directory Kiro Crew creates, a link is indistinguishable
        # from the operator's own layout, so the walk stops at the first
        # ancestor that ALREADY exists: everything below that is a directory
        # this write would create itself, so a link there cannot be ours, while
        # everything above it is pre-existing layout we do not get to judge (a
        # symlinked ``/tmp`` on macOS is exactly that).
        for component in (parent, *parent.parents):
            _refuse_if_link(path, component)
            if component.exists():
                return
        return
    anchor, names = split
    current = anchor
    for name in names:
        current = current / name
        _refuse_if_link(path, current)
    anchor_resolved = _resolved_or_none(anchor)
    resolved_parent = _resolved_or_none(parent)
    if anchor_resolved is None or resolved_parent is None:
        raise OSError(
            f"refusing to write {path}: its parent chain cannot be resolved, so "
            "the write cannot be shown to land where it was named."
        )
    expected = anchor_resolved.joinpath(*names)
    if resolved_parent != expected:
        raise OSError(
            f"refusing to write {path}: its parent resolves to {resolved_parent} "
            f"rather than {expected}, so the write would land somewhere it was "
            "not named. Replace the redirecting link with a real directory."
        )


def _refuse_if_link(path: Path, component: Path) -> None:
    """Raise when *component* of *path*'s parent chain is a link or junction."""
    if platform_compat.is_link_or_junction(component):
        raise OSError(
            f"refusing to write {path}: its parent {component} is a symlink or "
            "junction, so the write would land at the link's target instead. "
            "Replace the link with a real directory."
        )


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
    would hide that. It further implies :func:`_refuse_linked_parent`: a secret
    writer must never follow a link, because a pre-planted parent symlink or
    junction redirects the whole write to a location the caller never named
    (issue #4381).

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
    if restrict_to_owner:
        # Before the mkdir: mkdir(parents=True) walks THROUGH a planted link and
        # would create the missing directories under its target, so checking
        # after it would find a tree the write itself had already built.
        _refuse_linked_parent(path)
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
    except BaseException:
        # BaseException, not Exception. Three of the hand-rolled writers this
        # helper replaces already cleaned up under ``except BaseException``:
        # ``webhooks.write_json_atomic`` and both md_notebook temp writers. So
        # catching only ``Exception`` would leave a temp file behind on Ctrl-C
        # where the original removed it. The webhooks clause has this exact
        # shape, fd close included, which is where this one came from.
        #
        # Propagation is unchanged: the exception is re-raised untouched, so
        # KeyboardInterrupt and SystemExit still reach the caller. Only the
        # orphaned descriptor and the temp file are reclaimed on the way out.
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

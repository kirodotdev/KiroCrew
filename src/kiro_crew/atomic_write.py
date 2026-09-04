"""Atomic file write using unique temp filenames to avoid race conditions.

All atomic-write sites in KiroCrew should use this helper instead of
deterministic ``.tmp`` filenames, which cause ENOENT when concurrent
writers target the same file.
"""

from __future__ import annotations

import asyncio
import base64
import errno
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

# errnos meaning "this filesystem has no extended attributes", as opposed to "the
# lookup failed". Only the former is safe to treat as "nothing to carry": a
# failed lookup is not proof that there is nothing to lose.
#
# These xattr helpers live here rather than in hooks.py because hooks.py already
# imports this leaf module transitively (via platform_compat), while this module
# must not import hooks.py (heavy, aiohttp-adjacent dependency chain). hooks.py
# re-imports them from here so the two ACL-carry sites -- safe_write_file_nolink
# and this module's atomic_write -- share one spelling of the policy.
_XATTR_UNSUPPORTED_ERRNOS = frozenset(
    e
    for e in (getattr(errno, n, None) for n in ("ENOTSUP", "EOPNOTSUPP", "ENOSYS"))
    if e is not None
)

#: Attributes an inode-replacing write reproduces on the replacement, and the
#: ONLY ones -- losing any of these leaves the new file protected less than the
#: one it replaced, which is what the carry exists to prevent.
#:
#: This is an ALLOWLIST, and that direction is the security property. Both carry
#: sites install a FRESH inode holding content the CALLER supplied, so every
#: attribute replayed onto it is applied to NEW bytes. Replaying a
#: privilege-bearing attribute therefore grants the new content whatever the old
#: file was trusted with:
#:
#: * ``security.capability`` is file capabilities. An authenticated
#:   ``/api/file-write`` or steering save that rewrites a capability-bearing file
#:   would leave e.g. ``CAP_NET_RAW`` attached to attacker-chosen content -- a
#:   privilege grant the replacement never earned.
#: * ``security.ima`` / ``security.evm`` are integrity signatures OVER THE OLD
#:   BYTES. Carrying them forges an appraisal for content that was never
#:   measured, which is worse than losing one: the file is not merely unprotected
#:   but affirmatively vouched for.
#:
#: A denylist of those three would close exactly today's cases and silently
#: re-open on the next privileged namespace the kernel grows, so the carry names
#: what it needs and drops everything else. Dropping is safe by construction: an
#: attribute the replacement never receives leaves it with the defaults a plain
#: editor save would have produced, which is the floor, not a regression.
#:
#: ``security.selinux`` is deliberately NOT here, on both halves of the argument.
#: A freshly created inode already gets its type from the parent directory's
#: transition rule -- the same label any ordinary save yields -- so carrying buys
#: nothing; and writing that attribute needs ``relabelfrom``/``relabelto`` in the
#: writing domain, which a dashboard process on an enforcing host typically lacks
#: (``EACCES``). Under the fail-closed half of :func:`_carry_xattrs` that would
#: refuse every save on exactly the hosts that are most locked down.
_CARRIED_ACCESS_CONTROL_XATTRS = frozenset(
    (
        "system.posix_acl_access",  # the file's own named-user/group ACL entries
        "system.posix_acl_default",  # the ACL children inherit (directories)
    )
)

#: Informational namespaces carried BEST EFFORT -- see :func:`_carry_xattrs`.
#: Application metadata (tags, provenance notes) is worth reproducing but never
#: worth failing a save over, and ``user.*`` is unprivileged by kernel rule: it
#: is writable by anyone who can write the file, so it can carry no authority the
#: writer did not already hold.
_CARRIED_INFORMATIONAL_XATTR_PREFIXES = ("user.",)

#: Whether this platform exposes the xattr syscalls an ACL carry needs at all.
#:
#: Windows has none of them, and typeshed guards all three behind
#: ``sys.platform == "linux"``, so every use is a ``hasattr`` probe rather than a
#: direct call.
ACCESS_CONTROL_XATTRS_SUPPORTED = all(
    hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")
)


def pinned_parent_replace_supported() -> bool:
    """Whether an inode-replacing write can be staged and renamed through a dir fd.

    The staged temp file is created with ``os.open(name, ..., dir_fd=)`` and the
    rename that publishes it is ``renameat`` -- ``os.rename`` with both
    ``src_dir_fd`` and ``dst_dir_fd``. Both syscalls must accept a directory
    descriptor, or a caller that passes ``parent_dir_fd`` would fall through to
    the by-name floor.

    ``os.rename`` is probed rather than ``os.replace``: on this interpreter family
    ``os.rename`` is in ``os.supports_dir_fd`` while ``os.replace`` is not, and on
    POSIX ``os.rename`` already overwrites an existing destination, so the replace
    semantics hold. ``O_NOFOLLOW`` is part of the requirement because the staged
    file must refuse a link planted at the temp name.
    """
    return (
        hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
    )


def open_access_control_source(path: Path | str, *, dir_fd: int | None = None) -> int | None:
    """Open *path* for a :func:`atomic_write` ``preserve_access_control_from``.

    *dir_fd* is a descriptor for the destination's ALREADY-PINNED parent — the
    same one the caller hands to ``parent_dir_fd``. With it the leaf is opened as
    a bare component RELATIVE to that descriptor, so the inode whose mode and
    ACL are read is the one inside the directory the caller walked. Opening by
    name after pinning is a hole rather than a redundancy: a directory replaced
    at that name between the pin and this open makes the metadata come from the
    replacement while the write still publishes into the pinned original, so the
    original's file is handed back carrying a mode and ACL chosen by whoever did
    the replacing. Every caller that pins MUST pass it; the pinned parent is only
    worth having if nothing downstream of it is addressed by name again.

    With *dir_fd* the descriptor comes back even where the xattr syscalls are
    absent, and the Windows caveat below does not apply there: pinning needs
    ``O_DIRECTORY``, which Windows does not have, so a pinned caller is on POSIX
    and publishes with ``renameat`` rather than ``os.replace``. That case is not
    hypothetical — macOS has ``openat`` and no ``listxattr`` — and the MODE carry
    needs a descriptor even when the ACL carry has nothing to read, or a by-name
    ``stat`` would reintroduce exactly the mismatch above on that platform.

    Without *dir_fd*, returns ``None`` — meaning "pass no descriptor" — on a
    platform without the xattr syscalls. That is not merely an optimisation:
    there is nothing to carry there, AND holding a read handle open across the
    write is not free on Windows, where ``os.replace`` fails with
    ``PermissionError`` while ANY other handle is open on either path. A
    descriptor kept for a carry that cannot happen would therefore fail every
    write on that platform, which is what this helper exists to prevent — and why
    all three call sites go through it rather than spelling the ``os.open``
    themselves.

    ``O_NOFOLLOW`` carries real weight for one caller and is defense-in-depth for
    the rest. The file-write and steering updates hand in a path already
    canonicalized (``hooks.validate_file_path``) or ``lstat``-checked, so the final
    component is symlink-free by construction there and this open rejects nothing
    legitimate; it closes the window where that component is swapped for a link
    after the check. ``skills._write_skill_md`` has no such check — its guard is a
    plain ``exists()``, which FOLLOWS a link — so for that caller this open is what
    refuses a symlinked ``SKILL.md`` in the first place. An ``OSError`` propagates
    so the caller can treat it as a rejected target rather than a server fault, and
    all three do.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if dir_fd is not None:
        return os.open(os.path.basename(os.fspath(path)), flags, dir_fd=dir_fd)
    if not ACCESS_CONTROL_XATTRS_SUPPORTED:
        return None
    return os.open(path, flags)


def _should_carry_xattr(attr: str) -> bool:
    """True when *attr* is one an inode-replacing write reproduces at all.

    The gate for BOTH carry sites, applied when the source is READ so a
    privilege-bearing value is never even held in memory to be replayed by a
    later edit. See :data:`_CARRIED_ACCESS_CONTROL_XATTRS` for why this is an
    allowlist rather than a denylist of the privileged namespaces.
    """
    return attr in _CARRIED_ACCESS_CONTROL_XATTRS or attr.startswith(
        _CARRIED_INFORMATIONAL_XATTR_PREFIXES
    )


def _is_access_control_xattr(attr: str) -> bool:
    """True when losing *attr* would leave the file less protected.

    Only these justify refusing a write. `user.*` is application metadata: worth
    carrying, not worth failing a save over on a filesystem that cannot store it.
    """
    return attr in _CARRIED_ACCESS_CONTROL_XATTRS


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


#: Bytes of randomness in a pinned-parent temp name. tempfile.mkstemp uses eight
#: random characters; matching that entropy keeps the collision odds equivalent to
#: the by-name floor while the O_EXCL create below is what actually makes the name
#: unique -- a collision simply retries.
_PINNED_TMP_RANDOM_BYTES = 6
_PINNED_TMP_MAX_ATTEMPTS = 100


def _mkstemp_at(dir_fd: int) -> tuple[int, str]:
    """Create a unique temp file relative to *dir_fd*; return ``(fd, name)``.

    ``tempfile.mkstemp`` cannot be driven through a directory descriptor -- it
    only takes a ``dir=`` PATH, which re-resolves every component and so reopens
    exactly the ancestor-swap window the pinned parent exists to close. This is
    the descriptor-relative equivalent: ``O_CREAT|O_EXCL|O_NOFOLLOW`` under the
    pinned parent, so the create is atomic, refuses a link planted at the temp
    name, and never leaves the directory the caller walked.

    The name is returned as a bare component (no directory part); the caller
    addresses it only through *dir_fd*, never by joining it to a path.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0)
    for _ in range(_PINNED_TMP_MAX_ATTEMPTS):
        token = base64.urlsafe_b64encode(os.urandom(_PINNED_TMP_RANDOM_BYTES)).decode("ascii")
        token = token.rstrip("=").replace("-", "_")
        name = f".{token}.tmp"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            continue
        return fd, name
    raise OSError(  # pragma: no cover - 100 consecutive collisions is not reachable
        errno.EEXIST, "could not create a unique temp file under the pinned parent"
    )


def on_event_loop() -> bool:
    """Whether this thread is currently running an asyncio event loop.

    Mirrors the probe guarding ``CronService``'s store lock: a worker started by
    ``asyncio.to_thread`` or ``run_in_executor`` has no running loop of its own,
    so a caller that offloads its write keeps the retry while the loop thread
    itself never sleeps.

    Public because it decides more than this module's own retries.
    ``config/loader.py``'s ``write_config_atomically`` asks it before applying the
    Windows owner-only DACL, whose cost on a network-homed data home is bounded
    only by SMB. Both uses turn on the same property: the answer is about the
    CALLING THREAD, so it holds no matter how many synchronous helpers sit between
    a coroutine and the call, and a caller earns the stronger behavior by
    offloading rather than by declaring anything.
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
            if on_event_loop():
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


#: ``fsync`` on a directory that the platform or filesystem simply cannot express.
#: Every other errno is a real failure and is raised, because a caller whose next
#: step destroys the only other copy must not read "could not sync" as "synced".
_DIR_SYNC_UNSUPPORTED = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "EPERM", "EACCES", "EBADF", "ENOSYS")
    )
    if code is not None
)


def _close_quietly(fd: int, path: Path | str) -> None:
    """Close a directory descriptor, logging rather than raising.

    POSIX releases the descriptor even when ``close`` reports an error, so there is no
    leak to recover from — only a diagnostic, and one that is never the most useful
    thing the caller could be told.
    """
    try:
        os.close(fd)
    except OSError:
        logger.warning("could not close the directory descriptor for %s", path, exc_info=True)


def fsync_dir(path: Path | str, *, best_effort: bool = False) -> None:
    """Force a directory's own entries out, so a create or rename survives a crash.

    The half that :func:`atomic_write`'s ``fsync=True`` does not cover. Syncing the
    file descriptor forces the DATA; the name that reaches it lives in the parent
    directory, and until that directory is synced a power-off can return from
    ``os.replace`` and still come back to the old entry, with the new file's name
    recorded nowhere. Any writer whose next step destroys the only other copy —
    unlinking the source of a move, emptying a staging area — has to sync the
    directory too, or its "the replacement is safely in place" is not yet true.

    Deliberately NOT a ``sync_dir=`` option on :func:`atomic_write`: that would
    change the durability cost of every existing caller. This is opt-in, so the
    callers that need the guarantee pay for it and the rest are untouched.

    **Quiet where a directory sync cannot be expressed, and only there.** Windows has
    no directory descriptor to open, and some filesystems (network mounts in
    particular) reject ``fsync`` on a directory; there the atomic rename plus the
    file ``fsync`` are the guarantee available, and raising would turn a completed
    write into a reported failure. But an ``EIO`` is not that case — it says the
    device did not take the write — so it is raised. Swallowing it would hand the
    caller a false "durable" just before it unlinks the only other copy, which is the
    data loss this helper exists to prevent.

    ``best_effort=True`` downgrades even that to a warning, and exists for one shape
    of caller: one whose operation is ALREADY COMMITTED, where the sync only firms up
    a step that has happened. Raising at such a point does not protect anything — it
    reports completed work as failed, and a caller that then treats the work as
    un-done is the worse outcome. It is a keyword rather than a bare
    ``except OSError`` at the call site so the decision is visible, single-pathed, and
    still logged.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        if platform_compat.IS_WINDOWS:
            # The platform case: no directory descriptors at all.
            return
        if best_effort:
            logger.warning("could not open %s to sync it; its entries may not be durable", path)
            return
        raise
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        # The fsync error is the informative one, so the close is quiet on every
        # failing path here: raising a close error on top would mask the reason.
        _close_quietly(dir_fd, path)
        if exc.errno in _DIR_SYNC_UNSUPPORTED:
            logger.debug(
                "this filesystem does not support syncing the directory %s (%s)",
                path,
                errno.errorcode.get(exc.errno or 0, exc.errno),
            )
            return
        if best_effort:
            logger.warning(
                "could not sync the directory %s; its entries may not be durable",
                path,
                exc_info=True,
            )
            return
        raise
    # The sync reported success — but ``close`` can report a write error the kernel
    # deferred, which for a caller whose next step is to unlink the only other copy
    # is the same signal as a failed fsync. So it is checked, and it honours
    # best_effort for the same reason the fsync above does: a caller past its point of
    # no return cannot act on it. Not in a ``finally``: that would let a close error
    # replace an in-flight fsync error with a less informative one.
    try:
        os.close(dir_fd)
    except OSError:
        if not best_effort:
            raise
        logger.warning(
            "could not close the descriptor for %s; its entries may not be durable",
            path,
            exc_info=True,
        )


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
            if on_event_loop():
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


def _read_source_xattrs(source_fd: int, path: Path) -> list[tuple[str, bytes]]:
    """Read the CARRIABLE xattrs off *source_fd*, or refuse.

    Only attributes :func:`_should_carry_xattr` admits are read at all, so a
    privilege-bearing value (``security.capability``) or an integrity signature
    over the OLD bytes (``security.ima``/``security.evm``) is never captured, and
    so cannot be replayed onto the fresh inode by :func:`_carry_xattrs`. Filtering
    HERE rather than at the write is deliberate: it makes "we do not carry this"
    a property of what was collected instead of a branch a later edit can miss.

    Read from the DESCRIPTOR, never by name: a by-name ``listxattr(path)``
    re-resolves the whole path, so an ancestor swapped mid-save makes the lookup
    fail (or read a different file's attributes) while the rename still lands on
    the original. This mirrors ``hooks.safe_write_file_nolink``, which reads from
    its open fd for the same reason.

    A filesystem that does not support xattrs at all is NOT an error: there is
    nothing on the source to lose. Any OTHER failure means we cannot know what we
    would be dropping, so it refuses (raises) rather than installing a
    replacement that might silently drop the owner's ACL -- a lookup failure is
    not "there are none".

    On platforms without ``os.listxattr``/``getxattr`` (Windows, and macOS
    typeshed under ``mypy --platform linux``) there is nothing to carry, so this
    returns an empty list.
    """
    if not ACCESS_CONTROL_XATTRS_SUPPORTED:
        return []
    collected: list[tuple[str, bytes]] = []
    try:
        for attr in os.listxattr(source_fd):
            if not _should_carry_xattr(attr):
                continue
            collected.append((attr, os.getxattr(source_fd, attr)))
    except OSError as exc:
        if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
            return []
        raise OSError(
            exc.errno,
            f"refusing to write {path}: could not read the source file's extended "
            f"attributes ({exc}), so a replacement could silently drop access controls",
        ) from exc
    return collected


def _carry_xattrs(dest_fd: int, xattrs: list[tuple[str, bytes]], path: Path) -> None:
    """Reproduce *xattrs* onto *dest_fd*, refusing on a lost access control.

    *xattrs* has already been narrowed to the carriable set by
    :func:`_read_source_xattrs`, so nothing privilege- or integrity-bearing
    reaches this loop. What is left splits by what the attribute DOES, matching
    ``safe_write_file_nolink``:

    * a POSIX ACL (``system.posix_acl_access``/``_default``) that fails to copy is
      a security regression -- the rename would install an inode the owner has
      protected LESS than the one it replaced -- so the write is REFUSED (the
      caller's ``except BaseException`` cleans up the temp file and leaves the
      original untouched);
    * an informational ``user.*`` attribute is best effort, because failing
      closed there would break every save on a filesystem that simply cannot
      store xattrs, which is worse than losing a tag.
    """
    if not ACCESS_CONTROL_XATTRS_SUPPORTED:
        return
    for attr, value in xattrs:
        try:
            os.setxattr(dest_fd, attr, value)
        except OSError as exc:
            if _is_access_control_xattr(attr):
                raise OSError(
                    exc.errno,
                    f"refusing to write {path}: could not carry access-control "
                    f"attribute {attr!r} onto the replacement",
                ) from exc
            continue  # informational attribute -- keep going


def atomic_write(
    path: Path | str,
    content: str | bytes,
    *,
    fsync: bool = False,
    mode: int | None = None,
    newline: str | None = None,
    restrict_to_owner: bool = False,
    restrict_on_error: RestrictErrorPolicy = "raise",
    preserve_access_control_from: int | None = None,
    parent_dir_fd: int | None = None,
) -> None:
    """Write *content* to *path* atomically via unique temp file + rename.

    Uses ``tempfile.mkstemp`` so concurrent writers never collide on the
    same temp filename — or, with a pinned parent (*parent_dir_fd* below),
    ``_mkstemp_at``, which gives the same collision-free guarantee through an
    ``O_EXCL`` retry loop relative to the descriptor.  On error the temp file is
    cleaned up.

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

    *preserve_access_control_from* is an OPEN file descriptor for the file being
    replaced. When given, the source's extended attributes are read from that
    descriptor BEFORE staging and reproduced on the replacement inode before the
    rename. ``mode=`` alone carries permission BITS only, so a named POSIX ACL
    (stored in ``system.posix_acl_access``/``_default``) the owner set is
    otherwise dropped the moment a fresh inode is installed, handing back
    a file protected more narrowly than the one it replaced. What is carried is an
    ALLOWLIST -- those two names plus informational ``user.*`` -- and privileged
    namespaces are deliberately excluded, because the replacement holds content
    the CALLER supplied: see :data:`_CARRIED_ACCESS_CONTROL_XATTRS`. The rest of
    the policy mirrors
    ``hooks.safe_write_file_nolink``: an access-control attribute that cannot be
    carried REFUSES the write (the original is left untouched); an informational
    ``user.*`` attribute is best effort; a filesystem with no xattrs at all
    (``ENOTSUP``/``EOPNOTSUPP``/``ENOSYS``) is nothing to carry, not an error; any
    OTHER read failure is a refusal, because a failed lookup is not proof that
    there are none. Reading from the descriptor rather than by name keeps the
    read pinned to the inode the caller validated. The carry is ADDITIVE to
    ``mode=``, not a replacement.

    *parent_dir_fd* is an OPEN descriptor for the destination's directory,
    already pinned component-by-component by the caller (``pinned_fs`` supplies
    the walk). When given on a platform that can stage and rename through a
    descriptor (:func:`pinned_parent_replace_supported`), the temp file is
    created with ``os.open(name, O_CREAT|O_EXCL|O_NOFOLLOW, dir_fd=)`` and the
    publishing rename is ``renameat`` -- both ends relative to that descriptor --
    so neither the temp creation nor the rename re-resolves the parent by name and
    an ancestor swapped after the caller's validation cannot redirect the write.
    ``None`` (default) keeps the by-name ``mkstemp`` + rename floor exactly as
    before, and that is what a platform lacking the descriptor-relative syscalls
    must pass: it is the same platform that cannot pin a directory at all, so the
    floor adds no exposure the declared by-name traversal does not already carry.
    A descriptor handed in where :func:`pinned_parent_replace_supported` is False is
    REFUSED rather than ignored — a caller that walked a parent and passed the
    result believes the write is pinned, so quietly staging and renaming by name
    instead would leave that belief wrong and nothing would say so. Callers ask
    both probes (``pinned_fs.supports_pinned_walk()`` for the walk that produces
    the descriptor, this module's for the write that consumes it) and pass ``None``
    when either is False, which is why the refusal is unreachable in normal
    operation and fires only on probe drift. The destination's own name still comes
    from *path*; only the directory it is resolved through is pinned. It is also
    REFUSED alongside *restrict_to_owner*, whose lockdown is applied to the staged
    file by name and so cannot address a descriptor-relative temp.
    """
    binary = isinstance(content, bytes)
    if binary and newline is not None:
        raise TypeError("newline is a text-mode concept and cannot apply to bytes content")
    if restrict_to_owner and mode is not None and mode != 0o600:
        raise ValueError(f"restrict_to_owner implies 0o600; refusing to also honour mode={mode:#o}")
    if restrict_on_error != "raise" and not restrict_to_owner:
        # Reject rather than ignore: a caller passing this without asking for the
        # lockdown believes they configured a failure policy for something that
        # never runs, which reads as "permissions are handled" at the call site.
        raise ValueError(
            f"restrict_on_error={restrict_on_error!r} is meaningless without "
            "restrict_to_owner=True"
        )
    if restrict_to_owner and parent_dir_fd is not None:
        # Rejected rather than silently reconciled. The lockdown below is applied
        # to the staged file BY NAME (platform_compat.restrict_to_owner takes a
        # path, and its Windows half has no descriptor form), while a pinned
        # parent's temp name is a bare component addressed only through the
        # descriptor. Handing that bare name to a path-based chmod resolves it
        # against the process CWD, so it would tighten some unrelated file -- or
        # nothing -- and then publish a secret at the umask default.
        raise ValueError(
            "restrict_to_owner cannot be combined with parent_dir_fd: the "
            "owner-only lockdown is applied to the staged file by name"
        )
    if parent_dir_fd is not None and not pinned_parent_replace_supported():
        # Refused rather than degraded. The caller pinned a parent chain
        # component-by-component and handed the descriptor over; staging and
        # renaming by name anyway would answer that with an unpinned write while
        # every caller-side comment, spec line and test claims the opposite. A
        # capability the caller can ask about before it walks anything is a
        # caller-side gate, so this is the drift alarm for it, not the fallback.
        raise ValueError(
            "parent_dir_fd requires descriptor-relative open and rename "
            "(pinned_parent_replace_supported() is False on this platform); pass "
            "None to take the by-name floor instead of an unpinned write"
        )
    # restrict_to_owner wins: fchmod must not widen the file back to the umask
    # default after the lockdown has been applied.
    effective_mode = 0o600 if restrict_to_owner else mode
    path = Path(path)
    # Read the source's access-control xattrs BEFORE staging. A refusal here
    # (a lookup failure that is not "this filesystem has none") must abort before
    # any temp file exists, so there is nothing to clean up and the original is
    # untouched.
    src_xattrs: list[tuple[str, bytes]] = []
    if preserve_access_control_from is not None:
        src_xattrs = _read_source_xattrs(preserve_access_control_from, path)
    if restrict_to_owner:
        # Before the mkdir: mkdir(parents=True) walks THROUGH a planted link and
        # would create the missing directories under its target, so checking
        # after it would find a tree the write itself had already built.
        _refuse_linked_parent(path)
    # A pinned parent descriptor stages and renames through the fd the caller
    # already walked; without one the by-name mkstemp + rename is the floor. There
    # is no third state: a descriptor this platform cannot use was refused above.
    pin = parent_dir_fd
    if pin is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    else:
        fd, tmp = _mkstemp_at(pin)
    try:
        if restrict_to_owner:
            # Before fdopen, matching the shipping order in webhooks.py and
            # mcp_gateway/rewriter.py: the DACL lands while the file is still
            # empty, so a secret never exists in a readable file. tmp is a full
            # path here because parent_dir_fd is refused with this flag above.
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
        # Carry the source's access-control xattrs onto the replacement inode
        # before the rename, so the file is never briefly visible without them.
        # A refusal raises out to the except below, which reclaims the temp file
        # and leaves the original in place.
        if src_xattrs:
            _carry_xattrs(fd, src_xattrs, path)
        if fsync:
            os.fsync(fd)
        # Close BEFORE the rename: on Windows os.replace cannot swap a file that
        # still has an open handle. Clear fd first so the except branch below
        # cannot double-close if this close is itself what fails.
        fd, open_fd = -1, fd
        os.close(open_fd)
        if pin is None:
            replace_with_retry(tmp, path)
        else:
            # renameat, both ends relative to the pinned parent: neither the temp
            # name nor the destination name is re-resolved from the root, so an
            # ancestor swapped after the caller's walk cannot redirect the
            # publish. os.rename overwrites an existing destination on POSIX, so
            # the replace semantics hold; os.replace is not in supports_dir_fd on
            # every interpreter, os.rename is (pinned_parent_replace_supported
            # probes rename for exactly this reason).
            os.rename(
                os.path.basename(tmp),
                path.name,
                src_dir_fd=pin,
                dst_dir_fd=pin,
            )
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
            if pin is None:
                os.unlink(tmp)
            else:
                # Removed relative to the same pinned descriptor the temp was
                # created under, so cleanup cannot reach a different file even if
                # the parent name has since been swapped.
                os.unlink(os.path.basename(tmp), dir_fd=pin)
        except OSError:
            pass
        raise


def atomic_write_at(
    dir_fd: int,
    name: str,
    content: str,
    *,
    fsync: bool = False,
    mode: int | None = None,
) -> None:
    """Atomically replace one leaf under an already-pinned directory descriptor.

    The caller owns parent traversal and keeps *dir_fd* open for the whole
    transaction. A private ``O_EXCL|O_NOFOLLOW`` temporary is written and renamed
    to *name* relative to that SAME descriptor, so neither an ancestor swap nor a
    planted final symlink can redirect content to another inode. POSIX-only by
    design: the callers need descriptor-relative traversal, which Windows does
    not expose and Kiro Crew's pod backend does not use there.
    """
    if not platform_compat.IS_POSIX:
        raise NotImplementedError("descriptor-relative atomic writes require POSIX dir_fd support")
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError(f"atomic_write_at needs one leaf name, got {name!r}")

    tmp_name = f".{name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
    try:
        platform_compat.fchmod_safe(fd, mode if mode is not None else _get_default_mode())
        _write_all(fd, _encode(content, newline=None), Path(name))
        if fsync:
            os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise

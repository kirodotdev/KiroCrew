"""Reads, stats and walks that never follow a redirect.

The platform predicate the whole builder is gated on, redirect detection by ``lstat``, the
reparse-safe walk, per-component no-follow directory pinning and the leaf readers. Nothing
here writes or deletes: a write to a path derived from ``--out`` belongs to ``destination``
and a recursive delete to ``staging``, each beside the check that authorises it.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .contract import ExportRefused

#: Extra flags for a DESCRIPTOR-RELATIVE ``os.open`` of a file that must not be a symlink,
#: guarded because NEITHER constant exists on every platform. ``O_NOFOLLOW`` is the security
#: half (refuse a final-component link at open time) and ``O_NONBLOCK`` is the
#: liveness half (a FIFO would otherwise block the open forever, before any check
#: runs). Windows has neither, and getattr'ing only one of them is precisely the bug
#: that reddened five tests on the Windows shard: two platform-specific constants on
#: one line, one of them guarded. A read taken by PATH does not use these: it borrows
#: ``platform_compat.open_file_no_reparse``, which carries the same refusal on both platforms.
_NOFOLLOW_READ_FLAGS: int = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _read_text(path: Path) -> str | None:
    # newline="" on the READ for the same reason _write_guarded pins it on the write, and
    # the two only work as a pair. The default (newline=None) is universal-newlines
    # DECODING: it turns a CRLF file into a string holding "\n". Pinning only the write
    # therefore moved the corruption rather than removing it -- a CRLF-authored skill was
    # read as LF and staged as LF while ``_tree_hash`` had pinned the CRLF source, so the
    # build refused with "changed while the bundle was being written" exactly as it did
    # before, in the opposite direction.
    #
    # With both ends pinned the round trip is byte-preserving whatever the file holds,
    # which is the property the content pin actually needs: what ships is what was
    # hashed. It is not "normalise to LF" -- normalising would require re-hashing the
    # source through the same transform, and a builder that rewrites an operator's bytes
    # is a worse thing than one that carries them.
    # ``open`` rather than ``read_text(newline="")``: pathlib's reader only grew that
    # keyword in 3.13, while ``write_text`` has had it since 3.10, so the pair has to be
    # spelled asymmetrically to work on the versions this package supports.
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def _dir_fd_closed(fd: int) -> bool:
    """Answer whether *fd* names a directory -- and CLOSE it when it does.

    Every leaf open in the builder can hand back a directory descriptor: ``O_NOFOLLOW``
    refuses a SYMLINK at the final component, not a directory, and the shared opener's POSIX
    branch is an ``O_RDONLY`` open that a directory satisfies. The failure then lands on the
    reader's ``os.fdopen``, which raises ``IsADirectoryError`` BEFORE the file object it would
    return owns the descriptor -- so a reader written as ``with os.fdopen(fd, ...)`` reaches
    no close for it, and every read against a directory strands one while still answering
    correctly. A build walking a tree of them exhausts the descriptor table with no wrong
    answer anywhere to show why.

    One authority for every leaf open in the builder (the shared-opener borrow and the anchored
    walk here, the marker read's own ``os.open`` and the descriptor-relative read of a captured
    tree in ``staging``), for the same reason the no-follow refusal is one: a per-site copy is a
    place for one site to drift. A descriptor that cannot be ``fstat``-ed is closed and reported
    unusable, because every refusal in the builder fails closed.
    """
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            return False
    except OSError:
        os.close(fd)
        return True
    os.close(fd)
    return True


def _open_leaf_no_reparse(path: Path) -> "int | None":
    """Open *path* for reading, refusing a reparse point at the final name in that same open.

    One borrow of ``platform_compat.open_file_no_reparse`` for the whole builder, so its three
    leaf readers -- the two below and the marker read in ``staging`` -- cannot drift apart on
    which authority refuses a redirect. That opener
    settles the last component in the operation that opens it: an ``O_NOFOLLOW`` open on
    POSIX, and on Windows a ``FILE_FLAG_OPEN_REPARSE_POINT`` handle whose reparse and
    directory attributes are read off the descriptor that was opened. An ``lstat`` taken
    before a separate ``os.open`` answers about the name instead, and the swap an adversary
    plants lands between the two -- on Windows a junction naming a UNC share turns the read
    into an outbound SMB/NTLM exchange, so the window is a credential surface and not merely
    a wrong read.

    ``None`` is every refusal: a redirect, a directory, a missing path, an unreadable one.
    An environment where the shared module is not importable is among them, for the reason
    every other borrowed authority in the builder fails closed -- approximating the check
    locally is the check-then-open window itself, not a smaller version of it.

    A directory is refused through ``_dir_fd_closed``, the one authority every leaf open in the
    builder shares, because the two platforms disagree about where a directory
    surfaces: the shared opener's Windows branch raises on the handle's directory attribute,
    while its POSIX ``O_RDONLY`` open succeeds and yields a usable descriptor.
    """
    try:
        from kiro_crew.platform_compat import open_file_no_reparse
    except ImportError:
        return None
    try:
        fd = open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    if _dir_fd_closed(fd):
        return None
    return fd


def _read_text_nofollow(path: Path) -> str | None:
    """Read text through one descriptor, refusing a final-component redirect at the open.

    Returns ``None`` for everything it cannot read -- a link, a special file, a missing file,
    a non-UTF-8 body. Size is not among them: the read here is unbounded, and the one caller
    that needs a ceiling applies it itself. That is the contract its five callers are written
    against: each words its own refusal, which is why the agent-spec path says "agent spec"
    where the plan path says "curation plan".

    The refusal comes from ``_open_leaf_no_reparse`` on every platform, so there is no path
    inspected ahead of the open and no window between a verdict and the read it authorises.

    There is no anchored-walk variant here. The prompt read, the only caller that wanted one,
    goes through ``hooks.safe_read_file_bytes_nolink``, which verifies the OPENED descriptor's
    real path against a containment root -- a stronger check than re-walking a name, and one
    authority instead of two. A local per-component opener stack existed for that caller and
    was deleted with it: 191 lines reachable only from tests once the prompt read moved.
    """
    fd = _open_leaf_no_reparse(path)
    if fd is None:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        # Only where O_NONBLOCK exists, which is where the shared opener applies it. On
        # Windows neither that flag nor set_blocking() works on a regular-file descriptor --
        # it raises WinError 87.
        if getattr(os, "O_NONBLOCK", 0):
            os.set_blocking(fd, True)
        # No byte ceiling here. This reader serves the skill scan, the plan read and the
        # agent-spec read as well as nothing else, and a limit named for PROMPTS has no
        # business refusing an oversized agent spec -- a path this change is not about. The
        # prompt read carries its own bound, passed to the shared guard as ``max_bytes``.
        #
        # Reading BYTES rather than text is kept: it is what makes newline translation
        # impossible, which the CRLF round-trip depends on.
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read()
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_text_openat(root: Path, rel: Path, *, refuse_hard_link: bool = False) -> str | None:
    """Read ``root/rel`` as UTF-8, refusing a redirect at EVERY component, not only the last.

    ``_read_text_nofollow`` collapses check and read into one ``O_NOFOLLOW`` open, but
    ``O_NOFOLLOW`` guards only the FINAL component. An intermediate directory on the path
    (``agents/`` on the way to ``agents/frontdesk.json``) swapped for a junction or symlink
    AFTER a separate chain check and BEFORE the open is a check/open TOCTOU a concurrent
    writer can win. This walks ``rel`` one component at a time from ``root``, opening each
    directory with ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous one's descriptor
    (``openat`` semantics), so a component swapped for a redirect fails its OWN open -- there
    is no path string re-resolved after a check. The final component is opened ``O_RDONLY |
    O_NOFOLLOW`` relative to the last directory fd.

    Falls back to ``_read_text_nofollow`` where ``dir_fd`` is unsupported (Windows), the same
    trade the rest of this module makes; there the final component is still settled by the
    shared no-reparse opener and only the intermediate anchoring is lost, on the platform
    whose links differ anyway.
    Returns ``None`` on any redirect, missing component, special file, or non-UTF-8 body.
    """
    parts = rel.parts
    if not parts:
        return None
    if not _dir_fd_supported():
        # Windows has no ``dir_fd``, so the openat walk below is unavailable and the leaf
        # reader anchors only the last component -- an intermediate junction swapped under a
        # component would be followed into an untrusted file. Fail closed instead of
        # best-effort: ``lstat`` every component from ``root`` down and refuse if ANY is a
        # reparse point (a junction is not a symlink, so ``_is_redirecting_entry`` is the
        # check, not ``is_symlink``). The leaf itself is settled by the open the reader takes,
        # so what remains on this platform is a check-then-read window on the INTERMEDIATE
        # components alone: a redirect planted there before the walk is refused rather than
        # traversed, which is the fail-closed posture the openat path gives elsewhere, and one
        # swapped in after the walk is the window a descriptor-relative open would close.
        if _redirect_between(root, root / rel) is not None:
            return None
        # This is the only return on the no-``dir_fd`` path, and like every other one it
        # answers ``None`` rather than naming what was being read. Each caller words its own
        # refusal from that, which is why the agent-spec path says "agent spec" where the
        # plan path says "curation plan": the distinction lives at the call site, not here.
        return _read_text_nofollow(root / rel)
    file_fd = _open_leaf_nofollow_at(root, rel)
    if file_fd is None:
        return None
    if refuse_hard_link:
        # Refuse a HARD LINK on the OPENED leaf: a second name for the same inode that the
        # no-follow component walk cannot see. An operator-supplied file (the curation plan)
        # hard-linked to a credential passes every path and shape check while its bytes are
        # the credential's. Opt-in, so only the operator-file readers that want it pay it;
        # the staging/skill readers keep their own authority (``safe_read_file_bytes_nolink``)
        # and this does not change their semantics. On the descriptor already opened, so there
        # is no re-open TOCTOU.
        try:
            if os.fstat(file_fd).st_nlink > 1:
                os.close(file_fd)
                return None
        except OSError:
            os.close(file_fd)
            return None
    try:
        # BINARY, then decoded. ``read(n)`` on a TEXT stream bounds CHARACTERS while the
        # prompt ceiling is named in BYTES -- measured, 1048576 three-byte characters is a
        # 3145728 byte file that a length check against the ceiling reports as within it, so
        # a CJK persona reached three times the bound in memory. The byte count is the thing
        # bounded, so the read has to be the thing counted. ``newline=""`` on a text read
        # translated nothing and decoding translates nothing either, so the bytes reaching
        # the bundle are the bytes on disk and the CRLF round-trip still holds.
        with os.fdopen(file_fd, "rb") as fh:
            data = fh.read()
        return data.decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _open_leaf_nofollow_at(root: Path, rel: Path) -> "int | None":
    """Open ``root/rel`` for reading, pinning EVERY component no-follow; return the leaf fd.

    Walks ``rel`` one component at a time from ``root``, opening each directory with
    ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous descriptor and the final component
    ``O_RDONLY | O_NOFOLLOW`` relative to the last -- so a component swapped for a redirect
    fails its own open with no path string re-resolved after a check. The caller owns the
    returned fd and must close it (the text/bytes readers below wrap it in ``fdopen``).
    Returns ``None`` on any redirect, missing component, non-directory intermediate, or a
    DIRECTORY at the leaf -- the last because ``O_NOFOLLOW`` refuses a symlink and not a
    directory, and a directory descriptor is one no reader here can wrap.
    """
    parts = rel.parts
    if not parts:
        return None
    if not _dir_fd_supported():
        # Unreachable via the readers (they take the Windows fallback before calling here), but
        # stated locally so the rule that every ``O_DIRECTORY`` user consults ``_dir_fd_supported``
        # holds by reading -- without it this would raise ``AttributeError`` on ``O_DIRECTORY``.
        return None
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        # The root gets the SAME dir_flags as every component below it. Opening it
        # without O_NOFOLLOW made the anchor itself the hole: a link swapped in at
        # ``root`` was followed, and the walk then correctly refused redirects
        # *inside* a tree that was already the wrong tree.
        cur_fd = os.open(str(root), dir_flags)
    except OSError:
        return None
    open_dirs = [cur_fd]
    try:
        for part in parts[:-1]:
            cur_fd = os.open(part, dir_flags, dir_fd=cur_fd)
            open_dirs.append(cur_fd)
        try:
            leaf_fd = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=cur_fd)
        except OSError:
            return None
        if _dir_fd_closed(leaf_fd):
            return None
        return leaf_fd
    except OSError:
        # A redirect (ELOOP), a missing or non-directory component: none is a file to read.
        return None
    finally:
        for d in open_dirs:
            os.close(d)


def _read_bytes_openat(root: Path, rel: Path) -> "bytes | None":
    """Read ``root/rel`` as RAW BYTES, refusing a redirect at EVERY component.

    The bytes counterpart of :func:`_read_text_openat`, for a caller that needs the exact
    bytes (a signed plan carried verbatim, the report drift baseline) rather than decoded
    text. Same whole-window no-follow walk; falls back to a leaf-only no-follow read where
    ``dir_fd`` is unsupported (Windows), after refusing a reparse point anywhere on the chain.
    Returns ``None`` on any redirect, missing component, or read error.
    """
    if not rel.parts:
        return None
    if not _dir_fd_supported():
        if _redirect_between(root, root / rel) is not None:
            return None
        fd = _open_leaf_no_reparse(root / rel)
        if fd is None:
            return None
        try:
            with os.fdopen(fd, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    file_fd = _open_leaf_nofollow_at(root, rel)
    if file_fd is None:
        return None
    try:
        with os.fdopen(file_fd, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _dir_fd_supported() -> bool:
    """Whether a path can be pinned by opening its parent as a descriptor.

    One predicate for the three places that need it -- ``_read_text_openat``,
    ``_write_nofollow`` and ``_marker_is_ours`` -- because the answer must be the same in all
    of them. A site that reaches for ``os.O_DIRECTORY`` without asking raises
    ``AttributeError`` on Windows, where the attribute does not exist, before it does any
    work.

    False is Windows. It is a real narrowing of what those functions promise, spelled as a
    branch at each call site rather than hidden here, so a reader sees which guarantee is
    lost where.
    """
    return os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")


def _nofollow_primitive_available() -> bool:
    """Whether this platform gives the builder an atomic no-follow filesystem primitive.

    Every path this builder reads, stats, enumerates or mutates has to be judged without
    following a reparse point, because following one that names a UNC share is an outbound
    SMB probe carrying an NTLM exchange. The LEAF READ is settled everywhere: it borrows
    ``platform_compat.open_file_no_reparse``, which refuses a reparse point at the final name
    in the operation that opens it on both platforms. What this predicate asks about is the
    DESCRIPTOR-RELATIVE half, which a walk, a stat, an enumeration and a mutation each need:
    on POSIX ``_dir_fd_supported`` plus a working ``os.O_NOFOLLOW`` gives an open taken
    relative to a directory descriptor, so a component swapped for a link fails its own open.
    Windows offers no such open at all, so those fallbacks follow -- the guarantee is absent
    there, not merely narrower.

    Feature-detected, NOT ``os.name == "nt"``, so the guard lifts by itself the day the
    platform answers yes rather than waiting for someone to remember this function exists.
    Adopting a leaf opener does not answer this question, which is why the two halves are
    named apart: one is a property of the final component, the other of every component above
    it.
    """
    return _dir_fd_supported() and bool(getattr(os, "O_NOFOLLOW", 0))


def _refuse_without_nofollow_primitive() -> None:
    """Refuse at the entry point on a platform with no descriptor-relative no-follow open.

    One entry-point guard, because the alternative -- hardening each of the builder's ~15
    filesystem entry points against reparse-following on the Windows fallback branch -- is a
    site list, and a site list is complete only until the next one is found. The guarantee
    this builder needs (no read/stat/enumerate/mutate ever follows a reparse point to a share)
    is a property of the platform's primitives, so it is checked once where the primitive is
    absent rather than re-argued at every call. A deliberate hold whose exit is the predicate
    above, not a bug: the builder is POSIX-only for as long as that predicate answers no.
    """
    if not _nofollow_primitive_available():
        raise ExportRefused(
            "the crew bundle builder is POSIX-only for now: this platform has no "
            "descriptor-relative no-follow open, so the walks, stats, enumerations and writes "
            "behind packaging would follow a reparse point (a Windows junction to a UNC "
            "share) and leak an SMB/NTLM exchange during ordinary packaging. Refusing rather "
            "than ship that surface. The guard lifts automatically once the platform offers "
            "that open."
        )


def _is_redirecting_entry(probe: Path) -> bool:
    """Whether *probe* redirects to somewhere else: a symlink, or any reparse point.

    ``is_symlink()`` alone is the wrong question on Windows. A JUNCTION is a reparse point
    that is NOT reported as a symlink, and a junction is precisely what gets planted over a
    directory to redirect it, so a symlink-only check would pass the attack through. The
    attribute is read from the ``lstat`` result so the entry itself is inspected rather than
    its target.

    A missing entry is not redirecting: the caller's own open reports it, with the error
    message that fits where it happened.
    """
    try:
        st = os.lstat(probe)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _redirect_between(root: Path, path: Path) -> Path | None:
    """The first redirecting component on ``root -> path``, or ``None`` if the walk is clean.

    ``rglob`` and ``is_symlink()`` are not enough to keep a tree walk inside its root.
    ``rglob("*")`` DESCENDS into a directory junction (a non-symlink reparse point), and a
    file under that junction reports ``is_symlink()`` False, so it copies or hashes as an
    ordinary in-tree file even though its bytes live at the junction's target -- outside the
    crew source. Every ``rglob`` walk that trusts ``is_symlink()`` therefore needs this: it
    ``lstat``s each component below ``root`` with ``_is_redirecting_entry`` (which sees a
    junction, not only a symlink) and returns the first that redirects, so the caller can
    skip or refuse the file rather than ship someone else's bytes under a harmless name.

    ``path`` is assumed to be at or below ``root`` (it comes from ``root.rglob``). The
    components strictly between ``root`` and ``path`` are checked, then ``path`` itself.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        # Not under root -- treat the whole path as suspect rather than vouching for it.
        return path
    cur = root
    for part in rel.parts:
        cur = cur / part
        if _is_redirecting_entry(cur):
            return cur
    return None


def _walk_no_reparse(root: Path, *, match: str | None = None) -> "list[Path]":
    """Every descendant of ``root``, like ``root.rglob(match or '*')``, but NEVER descending
    a reparse point.

    ``pathlib.rglob`` walks a directory junction (a non-symlink reparse point) by
    construction, and on Windows walking a junction that names a UNC share is an outbound
    SMB/NTLM probe -- so the leak happens during ENUMERATION, before any post-hoc
    ``is_symlink`` / ``_redirect_between`` guard on the yielded path can refuse it. No amount
    of checking after the fact makes a traversal that already entered a junction safe. This
    walks with ``os.scandir`` and, at each directory, refuses to RECURSE into an entry that
    is a reparse point: the entry itself is still yielded (so a caller that wants to block or
    report it sees it), but its subtree is never entered, so the probe never fires. On a
    platform where ``scandir``/reparse detection is unavailable the result is identical to
    ``rglob`` for an ordinary tree; the reparse refusal is what Windows needs and POSIX
    ``scandir`` provides via ``is_symlink``.

    Returns a sorted list (callers relied on ``sorted(rglob(...))`` for a stable hash order).
    A missing directory yields nothing (a crew with no skills dir is the ordinary case); a
    directory that EXISTS but cannot be listed -- or whose entry cannot be stat'd to decide
    whether to descend -- fails closed with ``ExportRefused`` rather than reading as empty or
    as a leaf, so an unreadable selected directory cannot ship a silently incomplete bundle.
    """
    found: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            # A missing directory is absence, not an unreadable selection: the ROOT being
            # absent is the ordinary "this crew has no skills dir" case and yields empty, like
            # ``rglob``; a subdirectory pushed while it existed and gone now lost a race with a
            # concurrent remove -- nothing there to ship, so skip it.
            continue
        except OSError as exc:
            # A directory that EXISTS but cannot be listed (a permission change, an I/O error)
            # must NOT read as "empty" -- that is how an unreadable selected-skill directory
            # shipped a silently incomplete signed bundle: enumeration, copy, and the pin
            # recheck all skipped it. Fail closed and name the directory. Absent / unreadable /
            # unscannable never counts as "not selected".
            raise ExportRefused(
                f"the directory {current} exists but could not be listed ({exc}); refusing "
                f"rather than ship a bundle that silently omits what is under it. Fix its "
                f"permissions or remove it."
            ) from exc
        for entry in entries:
            p = Path(entry.path)
            if match is None or entry.name == match:
                found.append(p)
            # Recurse only into a REAL directory, never a reparse point. ``follow_symlinks``
            # is False so ``is_dir`` answers about the link itself; ``_is_redirecting_entry``
            # additionally catches a Windows junction, which ``is_symlink`` does not.
            try:
                is_real_dir = entry.is_dir(follow_symlinks=False)
            except FileNotFoundError:
                # Lost a race with a concurrent remove between the scandir and this stat,
                # the same case the scandir arm above skips: there is nothing left to
                # descend into.
                is_real_dir = False
            except OSError as exc:
                # An entry that EXISTS but cannot be inspected must not read as "not a
                # directory". That is the silent omission this function refuses one level
                # up, arriving one level down: an unstattable directory is never pushed, so
                # its whole subtree leaves the walk, and the candidate list, the copy and
                # the hash are all computed over what remains. The bundle is then signed
                # while missing files nothing reported. Same verdict as an unlistable
                # directory, for the same reason.
                raise ExportRefused(
                    f"{p} exists but could not be inspected ({exc}), so whether it is a "
                    f"directory to descend into is unknown; refusing rather than ship a "
                    f"bundle that silently omits what is under it. Fix its permissions or "
                    f"remove it."
                ) from exc
            if is_real_dir and not _is_redirecting_entry(p):
                stack.append(p)
    found.sort()
    return found


def _refuse_redirects_in_chain(root: Path, target: str, *, what: str = "prompt file") -> None:
    """Refuse a redirect at any component of ``root/target``, without resolving it.

    Walked one component at a time and judged by ``lstat``, so nothing here follows a link.
    That is the requirement: this runs BEFORE ``resolve()`` precisely because resolve is the
    traversal, and on Windows traversing a reparse point that names a share is an outbound
    SMB probe carrying an NTLM exchange.

    ``..`` is refused rather than normalised. Normalising it here would mean deciding what
    the path means without touching the filesystem, and ``a/../b`` is not ``b`` when ``a`` is
    a link -- which is the whole class of bug this function exists inside. The containment
    check after ``resolve()`` still runs and still has the final word on where the path
    landed; this only removes the redirects that made the resolve itself dangerous.
    """
    parts = Path(target).parts
    if not parts:
        return
    cur = root
    if _is_redirecting_entry(cur):
        raise ExportRefused(
            f"the anchor directory {root} is a link or junction. The walk below it is what "
            f"keeps a redirect from being traversed, and a redirect at the anchor itself makes "
            f"every check below examine someone else's directory. Refusing to read the {what} "
            f"through it."
        )
    for part in parts:
        if part == "..":
            raise ExportRefused(
                f"the {what} path names a parent directory ({target!r}). Resolving that is only "
                f"meaningful once every component above it is known not to be a link, so it "
                f"is refused rather than normalised. Reference the persona by a path that "
                f"does not climb."
            )
        if part in (".", ""):
            continue
        cur = cur / part
        if _is_redirecting_entry(cur):
            raise ExportRefused(
                f"{cur} is a link or junction on the path to the {what}. Following it "
                f"is what resolving this path would do, and on Windows a redirect naming a "
                f"share is an outbound SMB probe before any check runs. Refusing."
            )


def _open_dir_nofollow_pinned(dir_path: Path, *, already_resolved: bool = False) -> int:
    """Open *dir_path* as a directory fd, pinning EVERY component against a redirect swap.

    ``os.open(str(dir_path), O_RDONLY | O_DIRECTORY)`` opens by re-resolving the whole path
    string, so a symlink at a PARENT or intermediate component is followed -- and a leaf write
    or read taken ``dir_fd``-relative to that descriptor then lands wherever the link named,
    outside ``--out``. The leaf ``O_NOFOLLOW`` guards only the last component; the parent open
    is the hole. This walks the path one component at a time from its anchor, opening each with
    ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW`` relative to the previous descriptor, so a component
    swapped for a link fails its OWN open -- there is no path string re-resolved after a check.
    The caller owns the returned fd and must close it.

    RESOLVED FIRST, deliberately. A per-component ``O_NOFOLLOW`` walk over an UNRESOLVED path
    refuses at the first ordinary symlink -- and a normal home directory is often itself a
    symlink (measured: ``/home/<user>`` resolves elsewhere), so walking an unresolved path
    under ``$HOME`` would refuse every build. ``resolve()`` collapses those legitimate links
    once, up front; walking the resolved components no-follow then makes a refusal mean
    "a component changed AFTER resolution" -- the swap this defends against -- rather than "this
    machine has a normal home". A residual resolve-to-walk window remains (``resolve`` follows
    links at its own call), which is the same narrowing the openat readers accept.

    Falls back to the plain parent open where ``dir_fd`` is unsupported (Windows), the same
    trade the rest of the module makes; the whole builder refuses on that platform up front.
    """
    if not _dir_fd_supported():
        return os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)
    # A caller that has ALREADY resolved says so, and this does not read the tree again.
    # Resolving here as well gives the operation two readings, and two readings can be
    # separately self-consistent about DIFFERENT trees: a replacement landing between them
    # is pinned by the second one, and every check taken through the resulting descriptor
    # then agrees with itself about the attacker's tree. The prompt path resolves once
    # before its validation and hands that value in.
    # CPython 3.12 reports a symlink loop from non-strict ``resolve()`` as
    # ``RuntimeError``; 3.13 can leave the unresolved suffix for the component
    # walk, whose ``O_NOFOLLOW`` open reports ``OSError(ELOOP)`` instead. Both
    # shapes are normalized to ``OSError(ELOOP)`` at this one boundary so every
    # caller's existing ``except OSError`` guard fails closed consistently.
    if already_resolved:
        resolved = dir_path
    else:
        try:
            resolved = dir_path.resolve()
        except RuntimeError as exc:
            raise OSError(errno.ELOOP, f"symlink loop resolving {dir_path}") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OSError(errno.ELOOP, f"symlink loop resolving {dir_path}") from exc
            raise
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    cur_fd = os.open(resolved.anchor or "/", dir_flags)
    open_dirs = [cur_fd]
    try:
        for part in resolved.relative_to(resolved.anchor).parts:
            cur_fd = os.open(part, dir_flags, dir_fd=open_dirs[-1])
            open_dirs.append(cur_fd)
    except BaseException as exc:
        loop_error = isinstance(exc, OSError) and exc.errno == errno.ELOOP
        if isinstance(exc, OSError) and exc.errno == errno.ENOTDIR:
            try:
                component = os.stat(part, dir_fd=open_dirs[-1], follow_symlinks=False)
            except OSError:
                pass
            else:
                loop_error = stat.S_ISLNK(component.st_mode)
        for d in open_dirs:
            os.close(d)
        if loop_error:
            # A no-follow open cannot tell a loop from a component swapped for a link after
            # resolution: both are simply a link where a directory was measured. Name the
            # component and say what was measured, keeping ELOOP so callers fail closed.
            raise OSError(
                errno.ELOOP,
                f"component {part!r} of {dir_path} is a symbolic link where a directory "
                f"was resolved (a symlink loop, or it changed to a link since resolution): "
                f"{exc}",
            ) from exc
        raise
    # Close every intermediate but keep the final descriptor for the caller.
    for d in open_dirs[:-1]:
        os.close(d)
    return open_dirs[-1]

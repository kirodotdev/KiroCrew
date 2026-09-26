"""Local-disk file access for the markdown memory layer.

This module holds :class:`LocalMemoryFiles`, the public edition's implementation
of the ``MemoryFiles`` extension point: the admission gates, the hardened reader
and the atomic writer behind the ``MemoryStore`` markdown surface. Each gate
runs the same syscalls in the same order, returns the same refusal messages and
writes the same SEL audit records the direct ``MemoryStore`` reads and writes
rely on, so the public edition's behaviour, including its failure modes, is
fixed by this one implementation.

It lives in its own module rather than in ``platform/defaults.py`` because these
gates need ``hooks``, ``pinned_fs``, ``platform_compat`` and ``memory_startup``;
importing that set into the platform contract would make the cheap-to-import
defaults module drag in most of the memory stack, and the code reads better next
to the caller it was extracted from than inside a file of one-line no-op
adapters.

## What an alternative implementation does and does not owe

The guarantees here are LOCAL-INODE guarantees: ``O_NOFOLLOW`` opens, rejection
of symlinks, hardlinks, FIFOs and devices, a ``realpath`` containment check, and
a double-stat retry so reported metadata always describes the bytes returned.
They exist because the memory directory is agent-writable, and an agent that can
plant a symlink there could otherwise make a memory read republish an arbitrary
file the user can read.

An implementation backed by something that has no inodes has no such attack to
defend against, and must not pretend to run these checks. What it DOES owe is
every guarantee stated in the protocol and relied on by callers: an unreadable
or oversized source is an empty entry rather than partial text, content is valid
UTF-8 or refused, reported metadata matches the bytes returned, and a write
refused for drift returns ``False`` without retrying against a newer base.
"""

from __future__ import annotations

import logging
import os
import stat as _stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.hooks import (
    FileTooLargeError,
    is_unc_shape,
    safe_read_file_bytes_nolink,
    unc_probe_allowed,
)
from kiro_crew.memory_startup import require_memory_ready
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.platform.interfaces import MemoryEntry, MemoryRoots
from kiro_crew.platform_compat import file_lock, first_linked_ancestor, is_link_or_junction

logger = logging.getLogger(__name__)


def memory_files_for(roots: MemoryRoots) -> "Any":
    """The :class:`MemoryFiles` for *roots*, from the composed context.

    The single place the "which implementation serves this store" question is
    answered, so ``MemoryStore`` and the vector store's legacy markdown import
    cannot disagree about it.

    A provider that raises is NOT caught: per the ``MemoryFilesProvider``
    contract, a provider that cannot supply what it meant to supply must fail
    loudly rather than have this fall back to local disk. A silent fallback would
    serve whatever stale copy happens to be on this disk and then let the next
    write publish it over the live document. The ONLY tolerated failure is the
    absence of a composed context at all -- a bare unit test, or a worker that
    never booted the platform -- which is a standalone-shaped situation and
    correctly yields the standalone implementation. The ``try`` is therefore drawn
    tightly around the LOOKUP and excludes the ``files_for`` call itself.
    """
    try:
        from kiro_crew.platform.context import current_context

        provider = current_context().memory_files
    except Exception:
        logger.debug("no platform context; using local memory files", exc_info=True)
        return LocalMemoryFiles(roots)
    return provider.files_for(roots)


class LocalMemoryFiles:
    """``MemoryFiles`` over the local filesystem -- the public default.

    Constructed per store (see :class:`~kiro_crew.platform.interfaces.MemoryRoots`)
    and held by the ``MemoryStore`` that asked for it, so the roots it gates
    against cannot drift from the roots the caller is using.
    """

    # Both constants keep their values and their reasons from MemoryStore: the
    # size cap bounds a single read so a planted huge file cannot exhaust memory,
    # and the retry count is one extra attempt, which lands after a concurrent
    # writer's atomic rename in every non-adversarial case.
    _HISTORY_SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024  # cumulative content bytes
    _GUARDED_READ_ATTEMPTS = 2

    def __init__(self, roots: MemoryRoots) -> None:
        self._roots = roots

    # ── The MemoryFiles protocol surface ──

    def read_entry(
        self, path: Path, *, require_readable: bool = False, missing_ok: bool = True
    ) -> MemoryEntry:
        """See ``MemoryFiles.read_entry``.

        The dict :meth:`_guarded_entry` returns is the pre-seam shape; it is kept
        as-is and adapted here so the hardened reader itself carries no change.
        """
        entry = self._guarded_entry(path, require_readable=require_readable, missing_ok=missing_ok)
        return MemoryEntry(
            path=entry["path"], updated_at=entry["updated_at"], content=entry["content"]
        )

    def read_text(self, path: Path) -> str:
        """See ``MemoryFiles.read_text`` -- absent reads as ``""``.

        Deliberately NOT :meth:`read_entry`: the decode here is STRICT, and an
        undecodable file raises instead of degrading to an empty string. This is
        the value that feeds read-modify-write callers -- the consolidator's CAS
        baseline, ``add_preference``, the dashboard Save -- so answering ``""`` for
        a file that merely failed to decode would let the next whole-file write
        persist over the original bytes with no backup. Raising leaves the file
        intact and recoverable, which is the pre-seam behaviour and the reason the
        two readers stay separate rather than one calling the other.
        """
        self._require_link_free_roots()
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def read_text_for_rewrite(self, path: Path) -> str:
        """See ``MemoryFiles.read_text_for_rewrite``.

        The leaf checks belong to reading a file the caller is about to
        rewrite: reject a planted link at the dated name BEFORE the read, then
        reject a leaf that is not a
        LONE regular inode, because a hardlink passes the link check yet reading
        it still republishes the shared inode's contents.
        """
        self._require_link_free_roots()
        if is_link_or_junction(path):
            self._audit_read_refusal("leaf_link", path, "memory rewrite target is a link/junction")
            raise OSError(f"memory write refused (rewrite leaf is a link): {path}")
        try:
            st = os.lstat(path)
        except OSError:
            return ""  # missing: a fresh day, normal state
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            self._audit_read_refusal(
                "leaf_not_lone_regular",
                path,
                "memory rewrite target is not a lone regular inode (hardlink/special)",
            )
            raise OSError(f"memory write refused (rewrite leaf is not a lone regular file): {path}")
        # Strict decode on this read-modify-write path: the caller rewrites the
        # whole file, so a lossy read would persist U+FFFD over the original bytes.
        return path.read_text(encoding="utf-8")

    def write(self, path: Path, content: str, *, newline: Optional[str] = None) -> None:
        """See ``MemoryFiles.write`` -- an UNCONDITIONAL publish."""
        self._atomic_write_text(path, content, newline=newline)

    def replace_if(
        self,
        path: Path,
        content: str,
        *,
        base: Optional[str],
        newline: Optional[str] = None,
    ) -> bool:
        """See ``MemoryFiles.replace_if``.

        The comparison runs under the caller's lock tenure when there is one and
        takes its own otherwise, because the compare and the write have to be one
        step: between a bare comparison and a later write, the file can change
        again and the write would publish over bytes nobody compared against.
        """
        if base is not None and self.read_text(path) != base:
            logger.info(
                "Skipping stale memory write: %s changed since the baseline this "
                "update was computed from",
                path,
            )
            return False
        self._atomic_write_text(path, content, newline=newline)
        return True

    def exists(self, path: Path) -> bool:
        """See ``MemoryFiles.exists``."""
        return path.exists()

    def is_dir(self, path: Path) -> bool:
        """See ``MemoryFiles.is_dir``."""
        return path.is_dir()

    def glob(self, directory: Path, pattern: str) -> List[Path]:
        """See ``MemoryFiles.glob`` -- a missing or refused directory lists as empty.

        The read admission gate runs FIRST, which is the invariant
        :meth:`_read_root_guard` states: no syscall in this surface may touch a
        path that has not passed the gate, and a directory listing is such a
        syscall. Before the seam the gate was applied by each caller, and the
        history-pruning path did not apply it -- so a memory root swapped for a
        symlink was enumerated and pruned THROUGH the link. Gating here closes
        that by construction; the visible consequence is that pruning a linked
        root now deletes nothing instead of deleting the link target's files,
        which is the safe answer and only reachable in an attack shape.

        Missing-is-empty rather than an error because every caller is enumerating
        an optional tree (history before the first day is written, a store created
        but never used) and each one guarded the call with its own ``exists``
        check beforehand.
        """
        if not self._read_root_guard():
            return []
        if not directory.is_dir():
            return []
        return list(directory.glob(pattern))

    def mkdir(self, path: Path) -> None:
        """See ``MemoryFiles.mkdir``.

        Gated: ``mkdir`` is a write syscall against the memory tree, so a linked
        or untrusted root must refuse it BEFORE the directory is created. Without
        the gate here, seeding a fresh store created ``history/`` inside a
        symlinked target and only then failed -- the refusal has to come first or
        it is not a refusal.
        """
        self._require_link_free_roots()
        path.mkdir(parents=True, exist_ok=True)

    def remove(self, path: Path) -> None:
        """See ``MemoryFiles.remove``.

        Local disk has no trash, so this is an ``unlink``: the protocol asks an
        implementation that HAS one to use it, and this is the pre-seam behaviour
        for the one caller (history pruning past its retention window). Gated for
        the same reason as :meth:`mkdir` -- deletion through a linked root would
        delete the link target's files.
        """
        self._require_link_free_roots()
        path.unlink(missing_ok=True)

    @contextmanager
    def lock(self, path: Path) -> Iterator[None]:
        """See ``MemoryFiles.lock`` -- an advisory OS lock beside *path*.

        The lock file is a sibling named after the directory being serialised,
        matching the pre-seam ``.write.lock`` / ``.append.lock`` placement, so a
        gateway running this build takes the same locks as one running the last
        and the two cannot interleave writes against each other.
        """
        self._require_link_free_roots()
        directory = path if path.is_dir() else path.parent
        directory.mkdir(parents=True, exist_ok=True)
        name = ".append.lock" if directory == self._roots.history_dir else ".write.lock"
        lock_fd = self._open_lock_nofollow(directory / name)
        try:
            with file_lock(lock_fd, exclusive=True):
                yield
        finally:
            os.close(lock_fd)

    # ── Moved verbatim from MemoryStore (see the module docstring) ──

    def _require_link_free_roots(self) -> None:
        """WRITE-path admission gate — the same single invariant the read
        surface enforces in :meth:`_read_root_guard`: no filesystem syscall
        may touch a path whose workspace, memory-root, or history-dir
        component is a link/junction (or an untrusted UNC workspace on
        Windows; on Windows the workspace's ancestor chain is walked too,
        while POSIX ancestors are deliberately excluded — see the read gate).

        Point defenses alone (hardened temp files, symlink-safe lock opens)
        leave each writer trusting the directories themselves, so a linked
        ``memory/`` or ``history/`` directory routes staging and replacement
        outside the workspace. One
        gate at every writer entry makes that whole class unreachable
        instead of patching instances. Writers must fail LOUD, not silently
        no-op, so this raises where the read gate returns ``False`` (a
        refused read degrades to an empty entry; a refused write must not
        look like success). The refusal is SEL-audited by the shared guard.
        """
        if not self._read_root_guard():
            raise OSError(
                f"memory write refused (linked root or untrusted workspace): {self._roots.memory_dir}"
            )

    def _open_lock_nofollow(self, lock_path: Path) -> int:
        """Open (creating if absent) a lock file without following links.

        A bare ``open(path, "w")`` truncates before locking and follows a
        symlink, so an agent-planted ``.write.lock`` link would get its
        same-user TARGET truncated by the next memory write. This opens with
        ``O_NOFOLLOW`` (a symlink leaf fails with ELOOP instead of being
        traversed), never truncates (no ``O_TRUNC`` — a lock file carries no
        content), and requires a lone regular inode via ``fstat`` (rejects
        special files and hardlinked inodes). A planted link therefore makes
        the write fail closed rather than damage the link's target. Caller
        owns the returned fd and must ``os.close`` it.

        Windows has no ``O_NOFOLLOW`` (and ``O_NOFOLLOW`` would not cover a
        directory junction anyway), so the leaf is additionally rejected with
        an lstat-based link/junction check before the open — not race-free
        like the POSIX flag, but it matches the platform's best available
        primitive and the rest of this surface's Windows posture.
        """
        if is_link_or_junction(lock_path):
            raise OSError(f"refusing lock file (link or junction): {lock_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise OSError(f"refusing lock file (not a lone regular inode): {lock_path}")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _atomic_write_text(self, path: Path, content: str, *, newline: str | None = None) -> None:
        """Publish *content* to *path* via unique temp file + ``os.replace``.

        ``write_text`` truncates then writes, so a concurrent reader can
        observe an empty or partial file between those two steps. Delegates
        to :func:`kiro_crew.atomic_write.atomic_write`, which stages the
        bytes in a ``tempfile.mkstemp`` sibling (``O_CREAT | O_EXCL`` with an
        unpredictable name, so an agent-planted symlink at a guessable temp
        path is never followed) and atomically renames it over the target —
        a reader only ever observes COMMITTED versions. The structured read
        surface additionally double-stats around its read, so a replace
        landing mid-read is retried rather than pairing one version's bytes
        with another version's mtime. The temp carries a ``.tmp`` suffix so
        history ``*.md`` globbing never picks it up.

        An existing destination's permission bits are preserved: ``write_text``
        truncated in place and never touched the mode, but a rename-based
        replace installs the temp file's mode, so without carrying the old
        mode over a user's ``0o600`` memory file would silently widen to the
        umask default on the next write.
        """
        # Admission gate FIRST (see _require_link_free_roots): even the mode
        # stat below traverses the directory chain, and on Windows a stat of
        # a UNC path is itself the outbound SMB probe.
        self._require_link_free_roots()
        # The LEAF must not be a link either: replacing a link with a regular
        # file is safe, but callers doing read-modify-write would have read
        # the link's TARGET, and the mode stat would report the target's
        # mode. Reject before any following syscall; metadata via lstat.
        if is_link_or_junction(path):
            self._audit_read_refusal("leaf_link", path, "memory write target is a link/junction")
            raise OSError(f"memory write refused (target is a link): {path}")
        mode: int | None = None
        try:
            mode = _stat.S_IMODE(os.lstat(path).st_mode)
        except OSError:
            pass  # new file: let atomic_write apply the umask default
        atomic_write(path, content, mode=mode, newline=newline)

    def _audit_read_refusal(self, rule: str, path: Path | str, reason: str) -> None:
        """Best-effort SEL denial record for a refused markdown read.

        The refusal branches below are security controls (link/UNC/special-file
        admission gates over an agent-writable tree), so each denial must leave
        a tamper-evident record in the security event log, not only a process
        log line a same-host actor could suppress. Mirrors
        ``hooks._audit_governance``: lazy import, never lets an audit failure
        break the read path (the refusal itself already fails closed).
        """
        try:
            from kiro_crew.sel import sel

            sel().log_governance_decision(
                session_key="_host",
                tool_name="memory_markdown_read",
                item=str(path),
                outcome="denied",
                rule=rule,
                layer="memory_read_guard",
                reason=reason,
            )
        except Exception:
            logger.debug("memory read refusal audit emit failed", exc_info=True)

    def _read_root_guard(self) -> bool:
        """Single admission gate for the structured read surface.

        INVARIANT: no filesystem syscall in this surface may touch a path
        that has not passed this gate, and no component of a touched path
        may be a link -- on Windows including ancestors; on POSIX ancestors
        are deliberately excluded (see the gate comment below). That one
        property makes the whole REPARSE-POINT finding class
        (symlink/junction escapes, UNC credential probes, special-file reads)
        unreachable instead of patching instances. A mapped network drive or
        ``subst`` target (a ``Z:`` drive letter bound to a network share) is
        a residual outside this class: not UNC-shaped, no reparse point
        anywhere, resolved only at ``realpath`` time. The gates here do not
        screen it.

        Two gates enforce the invariant:

        1. Windows UNC gate — purely LEXICAL, evaluated before any syscall
           (``stat``/``glob``/``exists`` on a UNC path is itself the outbound
           SMB credential probe). Mirrors ``hooks.validate_file_path``.
        2. Reparse-point gate — the memory root and history dir must not be
           symlinks or Windows junctions (``lstat``-based check that never
           traverses the link). On Windows the workspace's ANCESTOR chain is
           walked root-first before any leaf lstat runs, because an lstat
           resolves every ancestor even when it does not follow the final
           component. Leaf files get the same check in
           :meth:`_guarded_entry`, so every component of every touched path
           is verified link-free.
        """
        if self._roots.memory_version != 2:
            require_memory_ready(self._roots.store_name)
        root = str(self._roots.memory_dir)
        if os.name == "nt" and is_unc_shape(root) and not unc_probe_allowed(root):
            logger.warning("memory read refused (untrusted UNC workspace): %s", root)
            self._audit_read_refusal("unc_workspace", root, "untrusted UNC workspace")
            return False
        # On Windows a linked ANCESTOR of the workspace defeats the lexical
        # UNC gate above: the workspace path is not itself UNC-shaped -- only
        # the link's target is -- and the lstat-based leaf checks below
        # resolve every ancestor, so the probe itself would traverse the link
        # and open the SMB connection. The walk is root-first and runs before
        # any leaf lstat. On POSIX linked ancestors remain deliberately
        # unrejected: resolving the whole chain would refuse legitimate
        # setups like a symlinked /home, those components are not
        # agent-writable, and stat-ing through a symlink is harmless there --
        # the same Windows-only rationale as the themes wiring
        # (dashboard/handlers/themes.py::_resolve_local_source). The walk
        # assumes the operator-configured workspace is absolute (every
        # in-tree constructor passes one); a relative workspace would walk
        # only the components the path itself names.
        if os.name == "nt" and first_linked_ancestor(self._roots.workspace) is not None:
            logger.warning("memory read refused (workspace ancestor is a link): %s", root)
            self._audit_read_refusal(
                "workspace_linked_ancestor", root, "a workspace ancestor is a link"
            )
            return False
        # The workspace leaf is checked FIRST among the lstat probes: a
        # workspace swapped for a link/junction would make the two descendant
        # checks below traverse it and validate paths inside the link's
        # target instead of the admitted tree. lstat-based, so the link
        # itself is never followed.
        if (
            is_link_or_junction(self._roots.workspace)
            or is_link_or_junction(self._roots.memory_dir)
            or is_link_or_junction(self._roots.history_dir)
        ):
            logger.warning("memory read refused (memory root is a reparse point): %s", root)
            self._audit_read_refusal(
                "root_reparse_point", root, "workspace, memory root or history dir is a link"
            )
            return False
        return True

    def _read_entry_bytes(self, path: Path) -> bytes | None:
        """Read one bound memory file without consulting learned-memory state."""
        if self._roots.memory_version != 2 and not self._roots.store_name:
            return safe_read_file_bytes_nolink(str(path), within_root=str(self._roots.memory_dir))

        # Named V1 stores share the member-store sensitive parent, so their
        # admitted memory files use the same descriptor checks as V2 anchors.
        # The exact opened path remains pinned to this store's memory root.
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        try:
            info = os.fstat(descriptor)
            opened = fd_real_path(descriptor)
            if not _stat.S_ISREG(info.st_mode):
                raise OSError("Manual profile path is not a regular file")
            if info.st_nlink != 1:
                raise OSError("Manual profile file has multiple hard links")
            if opened is None:
                raise OSError("Cannot verify the opened manual profile file's location")
            root = os.path.normcase(os.path.realpath(self._roots.memory_dir))
            actual = os.path.normcase(opened)
            expected = os.path.normcase(os.path.abspath(path))
            if actual != expected or os.path.commonpath([actual, root]) != root:
                raise OSError("Opened manual profile file is outside its expected bound path")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(self._HISTORY_SNAPSHOT_MAX_BYTES + 1)
            if len(data) > self._HISTORY_SNAPSHOT_MAX_BYTES:
                raise FileTooLargeError("Manual profile file exceeds the 8 MiB read limit")
            return data
        finally:
            os.close(descriptor)

    def _guarded_entry(
        self,
        path: Path,
        *,
        require_readable: bool = False,
        missing_ok: bool = True,
    ) -> dict:
        """Shape one markdown file as ``{"path", "updated_at", "content"}``.

        The memory directory is agent-writable, so a planted dated ``.md``
        name could be a symlink, a hardlink, or a special file. Reads go
        through :func:`kiro_crew.hooks.safe_read_file_bytes_nolink` confined
        to the memory root: it opens with ``O_NOFOLLOW``, rejects non-regular
        files (so a ``/dev/zero`` target cannot wedge the read), rejects
        hardlinked inodes and sensitive resolved targets, and caps the size.
        A refused, unreadable, oversized, or undecodable file surfaces as an
        empty entry — same shape as a missing file — never as leaked content
        or a traceback.

        Member anchors and V1 index rebuilds set ``require_readable`` so a refused
        source raises instead of appearing empty. Missing anchors may initialize
        normally; an already enumerated index source also sets ``missing_ok=False``.

        ``updated_at`` is snapshotted before the read and re-checked after,
        so the reported metadata always describes the bytes returned: when a
        concurrent consolidation rewrites or prunes the file mid-read, the
        read is retried once and then degrades to an empty entry rather than
        pairing one version's content with another version's mtime.
        """
        empty = {"path": str(path), "updated_at": None, "content": ""}

        def refused(reason: str) -> dict:
            if require_readable:
                raise OSError(f"Memory read refused ({reason}): {path}")
            return dict(empty)

        # Admission gate BEFORE the stat below — see _read_root_guard for the
        # invariant. The leaf gets its own lstat-based reparse check so every
        # component of the touched path (root, history dir, file) is verified
        # link-free before any following syscall.
        if not self._read_root_guard():
            return refused("unsafe memory root")
        if is_link_or_junction(path):
            logger.warning("memory read refused (file is a link): %s", path)
            self._audit_read_refusal("leaf_link", path, "memory file is a link/junction")
            return refused("file is a link or junction")
        for _ in range(self._GUARDED_READ_ATTEMPTS):
            try:
                st_before = path.stat()
            except FileNotFoundError:
                return dict(empty) if missing_ok else refused("source file disappeared")
            except OSError as exc:
                logger.warning("memory read refused (cannot inspect file): %s", path)
                return refused(f"cannot inspect file: {exc}")
            # Reject non-regular files BEFORE any open: opening a planted FIFO
            # read-only blocks forever waiting for a writer, so the reader's
            # own fstat check would never be reached. stat() follows symlinks,
            # so a link to a device/FIFO is also rejected here. (A racing swap
            # to a FIFO after this check is the reader's O_NOFOLLOW + fstat
            # problem for symlinks; an active same-host attacker racing the
            # window is outside this surface's threat model.)
            if not _stat.S_ISREG(st_before.st_mode):
                logger.warning("memory read refused (not a regular file): %s", path)
                self._audit_read_refusal(
                    "not_regular_file", path, "memory path is not a regular file"
                )
                return refused("path is not a regular file")
            try:
                data = self._read_entry_bytes(path)
            except FileTooLargeError:
                logger.warning("memory read refused (size cap) for %s", path)
                self._audit_read_refusal("size_cap", path, "memory file exceeds read size cap")
                return refused("file exceeds the read size cap")
            except OSError as exc:
                logger.warning("memory read refused (%s): %s", exc, path)
                return refused(str(exc))
            if data is None:
                logger.warning("memory read refused or failed for %s", path)
                self._audit_read_refusal(
                    "read_refused", path, "hardened read refused the file (link/hardlink/target)"
                )
                return refused("linked, escaped or unreadable file")
            try:
                st_after = path.stat()
            except OSError as exc:
                return refused(f"source changed during read: {exc}")
            if (st_before.st_mtime_ns, st_before.st_size) != (
                st_after.st_mtime_ns,
                st_after.st_size,
            ):
                continue  # rewritten mid-read: retry for a stable version
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("memory file is not valid UTF-8: %s", path)
                return refused("file is not valid UTF-8")
            if content == "":
                # Documented empty-state contract: empty content carries null
                # metadata, same shape as a missing file — consumers key
                # incremental sync on updated_at, and an "updated" empty file
                # has nothing to sync.
                return dict(empty)
            updated_at = datetime.fromtimestamp(st_after.st_mtime, tz=timezone.utc).isoformat()
            return {"path": str(path), "updated_at": updated_at, "content": content}
        logger.warning("memory file kept changing during read: %s", path)
        return refused("file kept changing during read")

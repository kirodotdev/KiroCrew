"""The Python dependency provisioning transaction for an app's ``requirements.txt``.

``provision_app_deps`` installs into the app's deps dir with ``pip --target`` under an
exclusive per-app file lock, from a validated, bounded, no-follow read of the
requirements, into a uniquely named staging tree that is swapped live only on success.
Every path step runs through a pin of the app-writable ``data/`` directory
(:class:`_PinnedDir`), because a running app can swap that directory for a link at any
moment. The stamp digest folds the interpreter ABI and the full marker environment, and
``_deps_tree_stamp_current`` is the activation gate the spawn and the stdio MCP
transports read before they inject the tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.apps.interpreter import app_deps_dir
from kiro_crew.apps.manager import _DEPS_STAGING_SWEEP_RE
from kiro_crew.apps.manifest import REQUIREMENTS_TXT_MAX_BYTES, requirements_in_tree
from kiro_crew.apps.registry import minimal_env
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sandbox import cgroup_scope_argv, run_limited, wrap_argv
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

try:  # optional dependency: the digest has a platform-module fallback
    from packaging.markers import default_environment as _default_marker_environment
except Exception:  # pragma: no cover - packaging ships with pip but is not guaranteed
    _default_marker_environment = None  # type: ignore[assignment]

logger = logging.getLogger(_FACADE)

# requirements.txt provisioning (pip --target into apps/interpreter.app_deps_dir).
# The stamp records the digest a successful install came from (requirements
# bytes + the installing interpreter's ABI tag - see _deps_digest), so a start
# where neither changed skips pip entirely. Staging/prior are transient swap
# directories: pip fills staging, success renames it live, and prior briefly
# holds the outgoing install so a failure at any point leaves either the old
# tree or the new one - never a half-replaced mix.
_DEPS_STAMP_NAME = ".requirements-sha256"
_DEPS_STAGING_NAME = ".kirocrew-deps-staging"


#: Read caps for app-controlled provisioning inputs: the gateway buffers
#: these in ITS OWN memory, so an oversized requirements.txt or stamp file
#: (or a build hook flooding stderr) must exhaust a bounded buffer, not the
#: gateway. The requirements cap is `manifest.REQUIREMENTS_TXT_MAX_BYTES`, the
#: value the shared acceptance rule (`requirements_in_tree`) applies as its
#: fast refusal; the bounded reads below enforce it on the opened descriptor.
_DEPS_REQ_MAX_BYTES = REQUIREMENTS_TXT_MAX_BYTES
_DEPS_STAMP_MAX_BYTES = 4096
_DEPS_PIP_STDERR_TAIL = 16 * 1024
_DEPS_PRIOR_NAME = ".kirocrew-deps-prior"


def _requirements_volatile(requirements: bytes) -> bool:
    """True when the stamp digest cannot prove the resolved set unchanged.

    The digest covers the top-level requirements.txt bytes only, so any line
    whose RESOLUTION can change while the line itself does not defeats the
    stamp: file references (``-r``/``-c``, attached or spaced), editables,
    local paths, VCS and URL requirements, and ``name @ url`` direct
    references. For these the caller disables stamp reuse entirely
    (reprovision every start) rather than re-implementing pip's requirements
    grammar here - over-matching a rare exotic line costs one redundant pip
    run, under-matching serves stale dependencies.
    """
    for raw in requirements.splitlines():
        line = raw.strip()
        if not line or line.startswith(b"#"):
            continue
        if line.startswith(
            (
                b"-r",
                b"-c",
                b"-e",
                b"-f",
                b"--requirement",
                b"--constraint",
                b"--editable",
                b"--find-links",
                b"--no-index",
                b"--index-url",
                b"--extra-index-url",
            )
        ):
            # File/constraint references, editables, and RESOLUTION-LOCATION
            # options: an unchanged `--find-links wheelhouse` line resolves
            # against local wheels whose CONTENT can change - the stamp
            # cannot prove the installed set unchanged for any of these.
            return True
        if b"://" in line or re.search(rb"\s@\s", line):
            return True
        if line.startswith((b".", b"/", b"~")) or re.match(rb"[A-Za-z]:[\\/]", line):
            return True
        # A BARE relative path (wheels/pkg.whl) is a local artifact whose
        # content can change under an unchanged line - any non-option line
        # carrying a path separator is volatile. Over-matching an exotic
        # marker expression costs one redundant pip run; under-matching
        # serves a stale local wheel.
        if b"/" in line or b"\\" in line:
            return True
        # A bare ARCHIVE filename (vendor.whl - no separator at all) is
        # still a local artifact: pip resolves it against the cwd (the app
        # root), and its content can change under an unchanged line.
        if line.lower().endswith(
            (b".whl", b".zip", b".tar.gz", b".tgz", b".tar.bz2", b".tar.xz", b".tar")
        ):
            return True
    return False


def _deps_tree_stamp_current(root: Path, req_file: Path) -> bool:
    """True when the provisioned tree's stamp names the digest for the
    CURRENT interpreter and the CURRENT requirements bytes.

    The activation gate, not the provisioning gate: after a Python upgrade a
    reprovision is attempted, but if it FAILS the stale tree (wheels built
    for the old ABI) is still on disk - injecting it via PYTHONPATH crashes
    the backend at import. The stamp digest folds the interpreter's cache
    tag, platform and full version, so an old-ABI tree can never present a
    matching stamp. Reads are bounded and no-follow, mirroring the
    provisioning path; every failure reads as "not current" (no activation -
    safe direction: the backend runs without the deps and surfaces the
    provisioning error, instead of crashing on foreign wheels).
    """
    try:
        # Same reader shape as provisioning: the shared containment rule
        # (`requirements_in_tree`, the fast refusal), then a component-pinned
        # no-follow open of the resolved target. A SUPPORTED in-tree symlink
        # (which provisioning accepts) must also activate - a direct O_NOFOLLOW
        # open on the link name would refuse it and strand a successfully
        # provisioned app without its deps.
        resolved = requirements_in_tree(root, req_file)
        if resolved is None:
            return False
        root_resolved, open_target = resolved
        rfd = _open_contained_nofollow(root_resolved, open_target)
        with os.fdopen(rfd, "rb") as rfh:
            if not stat.S_ISREG(os.fstat(rfh.fileno()).st_mode):
                return False
            req_bytes = rfh.read(_DEPS_REQ_MAX_BYTES + 1)
        if len(req_bytes) > _DEPS_REQ_MAX_BYTES:
            return False
        digest = _deps_digest(req_bytes)

        def _read_marker(name: str) -> str | None:
            marker = app_deps_dir(root) / name
            if platform_compat.is_link_or_junction(marker):
                return None
            try:
                mfd = os.open(str(marker), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                return None
            with os.fdopen(mfd, "rb") as mfh:
                if not stat.S_ISREG(os.fstat(mfh.fileno()).st_mode):
                    return None
                return mfh.read(_DEPS_STAMP_MAX_BYTES).decode("utf-8").strip()

        if digest:
            stamp_val = _read_marker(_DEPS_STAMP_NAME)
            if stamp_val is not None and stamp_val == digest:
                return True
        # Fall back to the ABI tag: a stamp that mismatches only because the
        # REQUIREMENTS (or their marker environment) changed still names a
        # tree of importable wheels - the last good install keeps serving
        # when a refresh fails (offline pip), exactly as it did before the
        # stamp gate. A missing or foreign-ABI tag never activates.
        return _read_marker(_DEPS_ABI_NAME) == _deps_abi_tag()
    except (OSError, UnicodeDecodeError):
        return False


_DEPS_ABI_NAME = ".abi-sha256"


def _deps_abi_tag() -> str:
    """ABI identity of the CURRENT interpreter, independent of requirements.

    What makes ``pip --target`` wheels importable or not: the implementation
    cache tag (``cpython-312``) and the build platform. Recorded beside the
    full stamp so activation can tell "stale REQUIREMENTS on the right ABI"
    (the prior tree still serves - a failed refresh must not strand the
    backend without its last good install) from "wrong ABI" (never inject).
    """
    tag = sys.implementation.cache_tag or ""
    plat = sysconfig.get_platform()
    return hashlib.sha256(f"{tag}\n{plat}\n".encode()).hexdigest()


def _deps_digest(requirements: bytes) -> str:
    """Stamp digest for a provisioned deps dir.

    Folds the installing interpreter's cache tag (e.g. ``cpython-312``), the
    platform tag (e.g. ``macosx-11.0-arm64``), AND the full interpreter
    version in with the requirements bytes: wheels installed by
    ``pip --target`` are ABI- and architecture-specific, and a
    requirements.txt can carry ``python_full_version`` environment markers
    that flip on a PATCH upgrade - so after a gateway Python upgrade of any
    granularity, or a cross-architecture home migration, an UNCHANGED
    requirements.txt must still reprovision. A requirements-only stamp would
    skip pip and leave a stale or incompatible install live.

    Scope: the digest covers the top-level requirements.txt bytes only.
    The stamp-skip caller compensates: a requirements.txt that references
    other files (``-r``/``-c``) disables the skip entirely, so a change
    confined to an included file can never be masked by a matching stamp.
    """
    tag = sys.implementation.cache_tag or ""
    plat = sysconfig.get_platform()
    pyver = platform.python_version()
    # The FULL PEP 508 marker environment, not just the interpreter tuple:
    # a requirement conditioned on platform_release / platform_version /
    # implementation details flips on an OS update while the requirements
    # bytes (and interpreter) stay identical - the stamp must not prove
    # such a set unchanged. Sorted key=value lines make the digest stable.
    if _default_marker_environment is not None:
        marker_env = "\n".join(f"{k}={v}" for k, v in sorted(_default_marker_environment().items()))
    else:  # packaging unavailable: fall back to the platform module
        marker_env = "\n".join(
            (
                f"platform_release={platform.release()}",
                f"platform_version={platform.version()}",
                f"platform_machine={platform.machine()}",
                f"platform_system={platform.system()}",
                f"implementation_name={sys.implementation.name}",
            )
        )
    return hashlib.sha256(
        f"{tag}\n{plat}\n{pyver}\n{marker_env}\n".encode() + requirements
    ).hexdigest()


def _open_contained_nofollow(base: Path, target: Path) -> int:
    """Open ``target`` under ``base`` with every component no-follow.

    A thin consumer of :mod:`kiro_crew.pinned_fs` (see its module docstring
    for why per-site pinning is banned): the parent chain is pinned one
    openat per component and the final name is opened O_NOFOLLOW through
    it, so neither an ancestor swap nor a final-component link can escape
    the app root. Where the platform cannot pin
    (``supports_pinned_walk()`` is False - Windows), the fallback is a
    single O_NOFOLLOW-less open behind the caller's is_symlink pre-check,
    backed by symlink creation being privileged there; junction swaps of
    the DATA dir are separately caught by _PinnedDir.verify.
    """
    rel_parts = target.relative_to(base).parts
    if not rel_parts:
        raise OSError("requirements path resolves to the app root itself")
    if not pinned_fs.supports_pinned_walk():
        return os.open(str(target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return pinned_fs.open_in_pinned_parent(
        str(target.parent),
        rel_parts[-1],
        flags=os.O_RDONLY | os.O_NOFOLLOW,
        mode=0o644,
        what="app requirements file",
        refusal=OSError,
    )


def _pinned_ancestors(path: Path) -> Path:
    """Return *path* with its ancestors canonical and its own name literal.

    The form :func:`kiro_crew.pinned_fs.pin_parent` asks its callers for: its
    O_NOFOLLOW walk refuses an ancestor that has always been a link - a home
    reached through one - exactly like one swapped mid-transaction. The final
    name is left alone, or the walk follows a link planted AT the directory it
    is pinning. ``realpath``, not ``Path.resolve``, which raises RuntimeError
    on a cycle and escapes the OSError callers refuse with.
    """
    return Path(os.path.realpath(path.parent)) / path.name


class _PinnedDir:
    """Pin the app data dir against link swaps for one provision transaction.

    The path is pinned exactly as handed in, so the CALLER owns
    :func:`_pinned_ancestors`: splitting it here would guess this one's depth.

    A path-based check-then-use is a TOCTOU window: a RUNNING app can swap
    ``data/`` for a symlink after the validation and have every later rename
    or delete land in another app's tree. On POSIX the directory is opened
    O_NOFOLLOW|O_DIRECTORY and HELD: renames go through ``dir_fd`` (they are
    the operations with delete/replace power over a victim's live tree), and
    the path-based steps that cannot take a dir_fd (rmtree, mkdir, pip's
    ``--target``, the stamp write) are each preceded by :meth:`verify`, which
    re-checks that the path still names the pinned inode. On Windows there
    is no O_NOFOLLOW or dir_fd; the caller's is_link_or_junction pre-check
    stands, backed by symlink creation being privileged there.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self._win_id: tuple[int, int] | None = None
        if pinned_fs.supports_pinned_walk():
            # pin_parent walks every component openat+O_NOFOLLOW (see
            # kiro_crew.pinned_fs for the invariants); a link anywhere on
            # the way - or at the target - is refused, not followed.
            self.fd = pinned_fs.pin_parent(str(path), what="app data directory", refusal=OSError)
        else:
            # Windows: capture the directory identity (volume serial + file
            # index via st_dev/st_ino) so verify() can detect a junction
            # swapped in mid-transaction - junction creation needs no
            # privilege, so the pre-check alone is a TOCTOU window there.
            if platform_compat.is_link_or_junction(path):
                raise OSError("app data directory is a symlink/junction; refusing")
            st = os.stat(str(path))
            self._win_id = (st.st_dev, st.st_ino)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def verify(self) -> None:
        """Refuse to proceed when the path names something other than the pinned dir.

        POSIX compares the held fd's identity against a fresh lstat. On
        Windows there is no held fd, but JUNCTION creation needs no
        privilege (unlike symlinks), so the swap threat is real there too:
        re-check that the path is still not a link/junction and still names
        the directory identity captured at pin time (st_dev/st_ino -
        Python's stat on Windows fills these from the volume serial and
        file index).
        """
        if self.fd is not None:
            st_fd = os.fstat(self.fd)
            st_path = os.lstat(str(self.path))
            if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
                raise OSError("app data directory was replaced mid-provisioning; refusing")
            return
        if platform_compat.is_link_or_junction(self.path):
            raise OSError("app data directory was replaced mid-provisioning; refusing")
        st_now = os.stat(str(self.path))
        if self._win_id is not None and (st_now.st_dev, st_now.st_ino) != self._win_id:
            raise OSError("app data directory was replaced mid-provisioning; refusing")

    def rename(self, src_name: str, dst_name: str) -> None:
        """Rename WITHIN the pinned dir, immune to a swapped path.

        POSIX renames are dir_fd-relative (cannot be redirected at all);
        on Windows the identity is revalidated immediately before the
        path-based rename, shrinking the swap window to the single rename
        syscall.
        """
        if self.fd is not None:
            os.rename(src_name, dst_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            self.verify()
            os.rename(str(self.path / src_name), str(self.path / dst_name))

    def rename_out(self, src_name: str, dst: Path) -> None:
        """Move an entry OUT of the pinned dir to a path destination.

        The SOURCE side is the security boundary (it names an entry inside
        the app-writable pinned dir); it goes through the held fd on POSIX
        and an identity re-check on Windows. os.rename never follows the
        final component of the destination, so a link planted at the
        destination name is replaced, not traversed.
        """
        if self.fd is not None:
            os.rename(src_name, str(dst), src_dir_fd=self.fd)
        else:
            self.verify()
            os.rename(str(self.path / src_name), str(dst))


def _pinned_remove_entry(pin: "_PinnedDir", parent: Path, name: str) -> None:
    """Best-effort delete of ``parent/name`` without a path-follow window.

    A thin consumer of :func:`kiro_crew.pinned_fs.remove_tree_pinned`: the
    parent chain is re-pinned, the target opened through it, and the whole
    tree removed by descriptor - approval binds the opened directory to the
    inode this transaction just observed through its own pin, so a swap
    between observation and removal is refused, not followed. Links are
    unlinked via the held descriptor and never traversed. Best-effort by
    contract: every refusal outcome leaves the entry (or its staged rename)
    in place and the transaction continues.
    """
    if pin.fd is not None:
        st = pinned_fs.stat_at(pin.fd, name)
        if st is None:
            return
        if not stat.S_ISDIR(st.st_mode):
            try:
                os.unlink(name, dir_fd=pin.fd)
            except OSError:
                pass
            return
        expect = (st.st_dev, st.st_ino)

        def _approve(root_fd: int, _tree: pinned_fs.PinnedTree) -> str | None:
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != expect:
                return "identity changed since this transaction observed it"
            return None

        try:
            pinned_fs.remove_tree_pinned(
                str(parent / name),
                what="app generated dependency tree",
                approve=_approve,
                refusal=OSError,
            )
        except OSError:
            pass
        return
    # No pinned walk on this platform: identity re-check plus path delete,
    # behind the privileged-symlink argument (junction swaps of data/ are
    # caught by pin.verify's identity check).
    try:
        pin.verify()
    except OSError:
        return
    target = parent / name
    try:
        if platform_compat.is_link_or_junction(target):
            platform_compat.unlink_link_or_junction(target)
        elif target.exists():
            shutil.rmtree(str(target), ignore_errors=True)
    except OSError:
        pass


# Hard on-disk ceiling for a child's captured output spill.
_DEPS_SPILL_HARD_CAP = 8 * 1024 * 1024


@contextlib.contextmanager
def _capped_spill(spill, hard_cap_bytes: int, poll_secs: float = 0.5):
    """Bound a subprocess output SPILL file's on-disk growth.

    The child owns the write end of ``spill`` after exec, so the parent
    cannot bound it per write; without a ceiling a noisy build hook or
    probe can fill the host disk before the run's own timeout fires. A
    watchdog thread polls the spill size and, on breach, truncates it back
    to empty - the child's subsequent writes re-extend from zero, so total
    on-disk residency never exceeds the cap plus one poll interval's worth
    of writes, and the run's timeout still bounds wall-clock. The bounded
    TAIL the caller reads afterwards is unaffected (a flooded run loses old
    output to the truncation, which is the correct trade: the tail is a
    diagnostic, not a transcript). No child pid needed, so this composes
    with a mocked run_limited that spawns nothing.
    """
    stop = threading.Event()
    tripped = threading.Event()

    def _watch() -> None:
        while not stop.wait(poll_secs):
            try:
                if os.fstat(spill.fileno()).st_size > hard_cap_bytes:
                    tripped.set()
                    spill.seek(0)
                    spill.truncate(0)
            except OSError:
                return

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    try:
        yield tripped
    finally:
        stop.set()
        t.join(timeout=2)


def _audit_provision_failure(app_name: str, provision_error: str) -> str:
    """The common failure epilogue for EVERY provisioning refusal arm.

    One ERROR log plus one SEL event per failed provisioning, whatever the
    arm (pip failure, requirements-read refusal, lock failure) - a refusal
    that skips this is invisible to operators and to the audit trail.
    Returns the error so callers can ``return _audit_provision_failure(...)``.
    """
    logger.error("%s", provision_error)
    try:
        sel().log_api_access(
            caller="gateway",
            operation="app_backend_spawn",
            outcome="deps_provision_failed",
            resources=app_name,
        )
    except Exception as sel_exc:
        logger.debug("SEL audit failed for app %s deps failure: %s", app_name, sel_exc)
    return provision_error


def provision_app_deps(app_name: str, root: Path) -> str:
    """Provision ``root/requirements.txt`` into the app's deps dir.

    The entire provision transaction (requirements read, interrupted-swap
    recovery, stamp check, pip into staging, live swap) runs under an
    exclusive per-app file lock: the backend spawn and a backend-less
    registration - or two concurrent registrations - would otherwise delete
    each other's staging tree mid-install and both fail. flock excludes
    across processes AND across threads (each caller opens its own
    descriptor), and the stamp check runs inside the lock, so a waiter that
    blocked behind a successful install skips pip on the stamp it left.
    """
    # Every pinned call below derives its path from root, so one canonical
    # base reaches all of them: requirements, staging snapshot, tree removals.
    root = _pinned_ancestors(root)
    _req = root / "requirements.txt"
    if not _req.is_file():
        # is_file() follows a symlink, so it answers False for a DANGLING
        # requirements.txt link (target missing) as well as for genuine
        # absence. Only true ABSENCE is "nothing to provision": a present
        # entry that is not a readable regular file (a broken link, a
        # non-file) is a provisioning FAILURE, surfaced so the backend does
        # not spawn importing dependencies that were never installed.
        if os.path.lexists(_req):
            return _audit_provision_failure(
                app_name,
                f"Refusing requirements.txt for app {app_name}: it is present "
                f"but not a readable regular file (a dangling symlink or a "
                f"non-file entry); refusing to skip provisioning silently.",
            )
        # Genuine absence: skip the pin and the lock entirely - the
        # transaction machinery must not create lock files (or take
        # platform-specific lock paths) for every app without declared
        # dependencies. The locked body re-checks under the lock, so this
        # is only a fast path, never the security boundary.
        return ""
    deps_parent = app_deps_dir(root).parent
    lock_path = deps_parent / ".kirocrew-deps.lock"
    provision_error = ""
    try:
        # The deps dir lives under app-writable data/, and every operation
        # below (lock file, staging, swap) would FOLLOW a link planted
        # there - an app pointing data/ at another app's tree would have
        # this provisioning swap attacker-chosen dependencies into the
        # victim's dir (the same shape the uninstall purge refuses in
        # manager.py). The gateway creates data/ as a real directory, so a
        # link is never legitimate: refuse before touching anything through
        # it.
        if platform_compat.is_link_or_junction(deps_parent):
            raise OSError("app data directory is a symlink/junction; refusing to provision")
        deps_parent.mkdir(parents=True, exist_ok=True)
        pin = _PinnedDir(deps_parent)
        try:
            # The lock file is opened through the PIN (dir_fd on POSIX), so
            # a link swapped in at data/ cannot redirect its creation; the
            # O_NOFOLLOW arm refuses a link planted at the lock name itself.
            # O_RDWR (not read-only): Windows msvcrt.locking requires write
            # access on the fd (same reason as bridges' _mcp_lock).
            lflags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            lock_name = lock_path.name if pin.fd is not None else str(lock_path)
            # Concurrent openat(O_CREAT) of an absent file can return ENOENT on
            # macOS; the shared helper elects one creator, then lets contenders
            # open its existing inode, and never recreates a lock that
            # disappears before the reopen.
            lfd = platform_compat.open_create_or_existing(lock_name, lflags, 0o644, dir_fd=pin.fd)
            with os.fdopen(lfd, "r+") as lf:
                with platform_compat.file_lock(lf.fileno(), exclusive=True):
                    provision_error = _provision_app_deps_locked(app_name, root, pin)
        finally:
            pin.close()
    except OSError as exc:
        # file_lock fails CLOSED; an unserialized install could corrupt the
        # live deps tree, so surface the failure instead of proceeding.
        provision_error = f"Failed to serialize dependency provisioning for app {app_name}: {exc}"
    if provision_error:
        return _audit_provision_failure(app_name, provision_error)
    return provision_error


def _write_staging_marker(
    staging_pin: "_PinnedDir", staging: Path, name: str, content: str
) -> None:
    """Write a provisioning marker into staging through its HELD descriptor.

    The markers are written AFTER pip - after arbitrary build-hook code has
    run with write access to data/ - so a by-name write here is the classic
    swap window: replace staging with a symlink and the gateway's own write
    lands outside the app root. Through the descriptor pinned at staging
    creation the write cannot be redirected (the fd is the directory,
    whatever the NAME points at now), O_NOFOLLOW refuses a planted link at
    the marker name, and O_EXCL refuses a planted regular file (a build
    hook pre-creating a marker is an attack signal - provisioning fails
    loud rather than trusting it). Windows has no dir_fd: the existing
    verify()+atomic_write floor stands (junction identity revalidation,
    same as every other Windows arm of this transaction).
    """
    if staging_pin.fd is not None:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=staging_pin.fd,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    else:
        staging_pin.verify()
        atomic_write(staging / name, content)


def _provision_app_deps_locked(app_name: str, root: Path, pin: _PinnedDir) -> str:
    """The provision transaction body - caller holds the per-app deps lock.

    Shared by the backend spawn and by backend-less stdio registration (an
    app can ship only MCP servers - with no backend start, nothing else ever
    runs pip, and the shim/PYTHONPATH transports would reference a forever-
    empty tree). Stamp-gated, so repeat calls with unchanged requirements do
    no network work. Returns an error message ('' when provisioning
    succeeded or was skipped). CALLERS gate trust: a module-style builtin
    executes trusted code from inside the kiro_crew package, and provisioning
    an app-dir requirements.txt for it would let agent-authored wheels load
    ahead of the trusted module - so this is only ever called for apps whose
    code runs from the writable app dir itself.
    """
    # Install Python dependencies into a per-app deps dir (isolated from the
    # Kiro Crew runtime). `pip install --target` rather than a venv: packaged
    # installs bundle an interpreter that ships pip but no ensurepip, so
    # `-m venv` dies after creating the directory skeleton - and the venv-first
    # interpreter policy would then prefer that skeleton while it holds none of
    # the app's dependencies. A --target install needs no bootstrap and works
    # identically under packaged and source installs; the deps dir reaches the
    # child via PYTHONPATH (set where the spawn body in ``backend.py`` builds the env).
    # sys.executable, never a bare "python3": the bare name relies on PATH
    # (absent on some hosts, a Store stub on Windows) - the same policy every
    # app spawn path applies via apps/interpreter.
    #
    # The install is stamp-gated and staged:
    # - A hash of requirements.txt is stamped into the deps dir on success, and
    #   a matching stamp skips pip entirely - so a restart with unchanged
    #   requirements does no network work and an OFFLINE restart of a healthy
    #   backend raises no alarm (pip --target cannot answer "already
    #   satisfied" the way a venv install could).
    # - pip installs into a staging dir that is swapped in only on success, so
    #   an interrupted or failed (re)install can never corrupt the live deps
    #   dir in place - the prior good install keeps serving the spawn.
    req_file = root / "requirements.txt"
    provision_error = ""
    req_bytes: bytes | None = None
    if req_file.is_file():
        # The app dir is app-writable, so requirements.txt can be a planted
        # symlink - and a resolve-then-read pair would be a TOCTOU window a
        # concurrent writer could race (validate a real file, swap in a
        # symlink, gateway reads protected bytes and stamps their digest).
        # The open is O_NOFOLLOW-bound: for a regular file the kernel refuses
        # any link swapped in before the open, and the fstat regular-file
        # check runs on the very handle the bytes come from. A LINK at
        # requirements.txt is legitimate app layout when it stays in-tree
        # (requirements.txt -> requirements/prod.txt), so a link is accepted
        # ONLY when its strict resolution stays inside the app root - then
        # the RESOLVED path is opened, itself O_NOFOLLOW-bound. Every race
        # collapses to a refusal or to reading a different in-root file
        # (app-controlled either way: no out-of-root bytes can ever be read
        # or digested). On Windows os.O_NOFOLLOW is absent; the is_symlink
        # pre-check substitutes (symlink creation is privileged there).
        try:
            resolved = requirements_in_tree(root, req_file)
            if resolved is None:
                raise OSError("requirements.txt resolves outside the app root or is not a file")
            root_resolved, open_target = resolved
            # Descriptor-relative, every-component-no-follow open: the
            # containment check above (`requirements_in_tree`, the rule the
            # install-time gate predicts from) is only a fast refusal - an
            # ancestor of the resolved path could be swapped for a link between
            # the check and the open, so the traversal itself is pinned
            # component by component (see _open_contained_nofollow).
            fd = _open_contained_nofollow(root_resolved, open_target)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise OSError("requirements.txt is not a regular file")
                # Bounded read: this buffer lives in the GATEWAY's memory
                # and the file is app-controlled - cap it instead of letting
                # a giant file take the gateway down.
                req_bytes = fh.read(_DEPS_REQ_MAX_BYTES + 1)
                if len(req_bytes) > _DEPS_REQ_MAX_BYTES:
                    raise OSError("requirements.txt exceeds the size cap")
        except OSError:
            req_bytes = None
        if req_bytes is None:
            provision_error = (
                f"Refusing requirements.txt for app {app_name}: it is a "
                f"symlink escaping the app directory, not a regular file, or "
                f"unreadable (out-of-root symlinked requirements are not "
                f"installed)"
            )
    if req_bytes is not None:
        deps_dir = app_deps_dir(root)
        prior = deps_dir.parent / _DEPS_PRIOR_NAME
        # Recover from an interrupted swap: a crash between the two renames
        # below leaves only the outgoing tree under the prior name. Put it
        # back before the stamp check, so an offline restart still has its
        # last good install (and a matching stamp skips pip entirely).
        if not deps_dir.exists() and prior.exists():
            try:
                pin.rename(prior.name, deps_dir.name)
            except OSError as exc:
                logger.warning("App %s: could not recover interrupted deps swap: %s", app_name, exc)
        stamp = deps_dir / _DEPS_STAMP_NAME
        digest = _deps_digest(req_bytes)
        # The digest covers the top-level file's bytes only: any requirement
        # whose RESOLUTION can change while its line does not (file
        # references, local paths, VCS/URL and direct references) defeats the
        # stamp, so those disable the skip - reprovision on every start
        # (correct, just slower) instead of silently serving a stale install.
        volatile = _requirements_volatile(req_bytes)
        # The stamp lives in the app-writable tree too, so its read is
        # no-follow-bound exactly like the requirements read above: a
        # planted symlink at the stamp name must not make the gateway read
        # an arbitrary path. Any open/read/decode failure reads as
        # "unprovisioned" (pip runs - safe direction).
        provisioned = False
        if bool(digest) and not volatile:
            try:
                # On Windows os.O_NOFOLLOW is absent; the is_link pre-check
                # substitutes (same pattern as the requirements read) - a
                # planted stamp link must read as "unprovisioned", not
                # through to an arbitrary file.
                if platform_compat.is_link_or_junction(stamp):
                    raise OSError("stamp is a symlink/junction")
                sfd = os.open(str(stamp), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(sfd, "rb") as sfh:
                    if stat.S_ISREG(os.fstat(sfh.fileno()).st_mode):
                        # Bounded read: a real stamp is one digest line; an
                        # oversized file reads its head, fails the compare,
                        # and safely reprovisions.
                        provisioned = (
                            sfh.read(_DEPS_STAMP_MAX_BYTES).decode("utf-8").strip() == digest
                        )
            except (OSError, UnicodeDecodeError):
                provisioned = False
        if not provisioned:
            # UNIQUE staging name per transaction, created through the PIN:
            # a fixed name plus path-based mkdir was the last re-pointable
            # step - a data/ swap after verification would have pip fill (or
            # a cleanup delete) another app's staging. The dir_fd mkdir
            # cannot be redirected; the fresh name means no pre-existing
            # tree to delete through a path; and pip receives the path only
            # after a final verify, with the flock guaranteeing no sibling
            # transaction races the window.
            staging = deps_dir.parent / f"{_DEPS_STAGING_NAME}-{os.getpid()}-{os.urandom(4).hex()}"
            _env = minimal_env()  # don't leak secrets to pip subprocesses
            try:
                # Stale staging trees from crashed transactions (the old
                # fixed name or unique names another pid left) are swept
                # best-effort AFTER a verify; pip --target does not replace
                # a distribution already present, so installs never reuse a
                # stale tree - the fresh unique name guarantees that
                # structurally instead of by strict pre-delete.
                pin.verify()  # path-based steps below cannot take a dir_fd
                # Stale-staging sweep, DESCRIPTOR-relative: a path glob plus
                # path rmtree could follow a data/ swapped in after the
                # verify. Enumerate through the held fd, quarantine each
                # match to a fresh random name via dir_fd rename (cannot be
                # redirected), then delete by path - the random name cannot
                # pre-exist in a victim tree the attacker cannot write, so a
                # post-swap delete is a harmless ENOENT.
                if pin.fd is not None:
                    _stale_names = [
                        e
                        for e in os.listdir(pin.fd)
                        if _DEPS_STAGING_SWEEP_RE.fullmatch(e) is not None and e != staging.name
                    ]
                else:
                    _stale_names = [
                        p.name
                        for p in deps_dir.parent.glob(f"{_DEPS_STAGING_NAME}*")
                        if _DEPS_STAGING_SWEEP_RE.fullmatch(p.name) is not None
                        if p.name != staging.name
                    ]
                for _stale in _stale_names:
                    _pinned_remove_entry(pin, deps_dir.parent, _stale)
                if pin.fd is not None:
                    os.mkdir(staging.name, 0o755, dir_fd=pin.fd)
                else:
                    staging.mkdir(parents=True, exist_ok=True)
                # Pin staging ITSELF for the rest of the transaction: pip
                # runs arbitrary build-hook code with write access to data/,
                # so every gateway step after it that addresses staging by
                # NAME (the marker writes, the publish rename) needs an
                # identity the hook cannot re-point. Same tool as the parent
                # pin; closed on every exit of the transaction.
                staging_pin = _PinnedDir(staging)
                pin.verify()  # pip receives a PATH; last re-check before it runs
                # Stamp-vs-install atomicity: pip RE-OPENS the requirements
                # path, and a concurrent rewrite after the hash above would
                # install the replacement while stamping the ORIGINAL digest
                # - later starts then skip repair and serve the wrong deps.
                # When the stamp will be trusted (non-volatile), pip installs
                # from an immutable SNAPSHOT of the very bytes the digest
                # covers. Volatile requirements never take the stamp
                # shortcut, and only they can carry file references whose
                # resolution is relative to the requirements file - so they
                # keep reading the validated live path, where includes
                # resolve correctly, with no stamp to skew.
                req_src = open_target
                if not volatile:
                    req_src = staging / "._kirocrew-requirements.snapshot"
                    # The write is the GATEWAY's own and staging lives in
                    # app-writable data/: a staging dir swapped for a link
                    # after its dir_fd mkdir would have a path write land in
                    # an arbitrary same-user file. Open through the pinned
                    # parent chain (every component O_NOFOLLOW) with O_EXCL,
                    # so neither a swapped ancestor nor a planted entry at
                    # the snapshot name can redirect it. Windows keeps the
                    # verify+path write behind the junction identity check.
                    if pinned_fs.supports_pinned_walk():
                        _sfd = pinned_fs.open_in_pinned_parent(
                            str(staging),
                            req_src.name,
                            flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            mode=0o644,
                            what="requirements snapshot",
                            refusal=OSError,
                        )
                        with os.fdopen(_sfd, "wb") as _sfh:
                            _sfh.write(req_bytes)
                    else:
                        pin.verify()
                        req_src.write_bytes(req_bytes)
                # pip reads the VALIDATED open_target, not the manifest
                # name: for an in-tree symlinked requirements.txt a nested
                # include (`-r base.txt`) resolves relative to the
                # requirements FILE, so handing pip the symlink path would
                # resolve includes beside the LINK instead of its target.
                # The no-follow handle above already refused an out-of-root
                # requirements.txt before any bytes were hashed; pip's own
                # re-open is a follow-open, but by then provisioning is
                # committed to THIS app's tree and the digest was taken from
                # the validated handle. Include-bearing requirements never
                # take the stamp shortcut (_requirements_volatile), so they
                # reprovision on every start - a change confined to an
                # included file cannot be masked.
                pip_cmd, _ = wrap_argv(
                    platform_compat.isolated_python_argv(
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        "--disable-pip-version-check",
                        "--target",
                        str(staging),
                        "-r",
                        str(req_src),
                    ),
                    mode="standard",
                )
                pip_cmd = cgroup_scope_argv(pip_cmd)  # cgroup DoS ceiling
                # check=True: a non-zero pip exit IS a provisioning failure. It
                # must not be discarded - the backend would spawn without its
                # dependencies and die on an import error pointing at the app.
                # cwd=root: relative references (`-e ./lib`) resolve against
                # the app root, not whatever directory the gateway happens to
                # be running from.
                # Bounded capture: capture_output buffers the child's whole
                # stdout/stderr in the GATEWAY's memory, and a noisy build
                # hook can flood it. stderr goes to a temp FILE and only a
                # bounded TAIL is ever read back (attached as exc.stderr for
                # the redaction pipeline below); stdout is discarded.
                with (
                    tempfile.TemporaryFile() as _pipbuf,
                    _capped_spill(_pipbuf, _DEPS_SPILL_HARD_CAP),
                ):
                    try:
                        run_limited(
                            pip_cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=_pipbuf,
                            timeout=60,
                            env=_env,
                            cwd=str(root),
                        )
                    except subprocess.CalledProcessError as _pip_exc:
                        # stderr went to the file, so the exception carries
                        # none - attach the bounded tail (never clobber a
                        # stderr some other spawn shape already set).
                        if not getattr(_pip_exc, "stderr", None):
                            _pipbuf.seek(0, os.SEEK_END)
                            _sz = _pipbuf.tell()
                            _start = max(0, _sz - _DEPS_PIP_STDERR_TAIL)
                            _pipbuf.seek(_start)
                            _tail = _pipbuf.read()
                            if _start > 0:
                                # The first line is PARTIAL: the seek can
                                # sever a URL's scheme, and the downstream
                                # exfil/credential redaction anchors on
                                # https?:// - a scheme-less remainder would
                                # carry its query token straight into the
                                # logs. Drop through the first newline; a
                                # tail that is one giant line is dropped
                                # whole (never worth a credential).
                                _nl = _tail.find(b"\n")
                                _tail = (
                                    _tail[_nl + 1 :]
                                    if _nl != -1
                                    else b"[pip stderr tail elided: unterminated first line]\n"
                                )
                            _pip_exc.stderr = _tail
                        raise
                # Editable installs (`-e ./lib`) materialise as
                # __editable__*.pth hooks. They are RETAINED: python children
                # launch through the deps_boot shim, whose site.addsitedir
                # processes .pth files, so editable installs work through
                # the shim. (Deps-provided python console scripts route
                # through the same shim via the shebang sniff in bridges, so
                # editables work there too.)
                if digest:
                    _write_staging_marker(staging_pin, staging, _DEPS_STAMP_NAME, digest)
                # ABI tag is written even for volatile requirements (digest
                # empty): activation uses it to keep serving the last good
                # tree when only the requirements resolution went stale, and
                # to refuse a wrong-ABI tree always.
                _write_staging_marker(staging_pin, staging, _DEPS_ABI_NAME, _deps_abi_tag())
                # Swap the fresh install live. Two renames, not an in-place
                # upgrade, so no state mixes old and new trees; the recovery
                # above (and the restore in the except arm) covers the window
                # in which only the prior name exists.
                pin.verify()
                # The publish rename moves whatever ENTRY sits at
                # staging.name - verify it is still the directory this
                # transaction created (held fd vs fresh lstat), or a build
                # hook that re-pointed the name would have its tree
                # published as the live install.
                staging_pin.verify()
                _pinned_remove_entry(pin, deps_dir.parent, prior.name)
                if deps_dir.exists():
                    pin.rename(deps_dir.name, prior.name)
                pin.rename(staging.name, deps_dir.name)
                staging_pin.close()
                _pinned_remove_entry(pin, deps_dir.parent, prior.name)
            except Exception as exc:
                if "staging_pin" in locals():
                    staging_pin.close()
                _pinned_remove_entry(pin, deps_dir.parent, staging.name)
                # If the failure hit between the swap renames (e.g. a locked
                # directory on Windows), the live name is empty and the good
                # tree sits under the prior name - put it back.
                if not deps_dir.exists() and prior.exists():
                    try:
                        pin.rename(prior.name, deps_dir.name)
                    except OSError as restore_exc:
                        logger.warning(
                            "App %s: could not restore prior deps after failed swap: %s",
                            app_name,
                            restore_exc,
                        )
                detail = str(exc)
                stderr = getattr(exc, "stderr", None)
                if stderr:
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode("utf-8", "replace")
                    # Redact BEFORE truncating: a suffix cut can split a
                    # credential from the marker the redactor matches on,
                    # leaving the secret's tail to survive the pass below -
                    # the same split-across-a-length-cap shape the MCP report
                    # capture guards against. pip errors can echo an index
                    # URL carrying credentials
                    # (`--index-url https://user:token@host/`); this detail
                    # reaches the gateway log and the user-visible backend log
                    # (and /api/logs). Exfiltration-URL redaction runs FIRST:
                    # an agent-authored requirements path can embed a
                    # suspicious URL that pip echoes verbatim, and the
                    # credential/query passes below do not catch a bare
                    # exfil host.
                    stderr, _ = redact_exfiltration_urls(stderr.strip())
                    stderr, _ = redact_credentials(stderr)
                    # Same order rule for the query-strip below: applied to
                    # the FULL stderr before the tail cut, or the cut could
                    # split a URL from its query and leave the token's tail.
                    stderr = re.sub(r"(https?://[^\s?#]+)\?\S+", r"\1?<redacted-query>", stderr)
                    detail = f"{detail}: {stderr[-400:]}"
                detail, _ = redact_exfiltration_urls(detail)
                detail, _ = redact_credentials(detail)
                # redact_credentials catches user:pass@ URL forms; a failed
                # SIGNED or tokenized URL carries its secret in the QUERY
                # STRING (?X-Amz-Signature=..., ?token=...), which pip echoes
                # verbatim. Strip query strings from every URL in the detail
                # (covers URLs arriving via str(exc), not just stderr).
                detail = re.sub(r"(https?://[^\s?#]+)\?\S+", r"\1?<redacted-query>", detail)
                provision_error = (
                    f"Failed to install requirements.txt dependencies for app "
                    f"{app_name}: {detail}"
                )
                # The spawn is still attempted: the deps dir may hold a
                # previous successful install, and some requirements are
                # optional. The failure is surfaced instead of swallowed: the
                # provision_app_deps wrapper's failure epilogue emits the
                # ERROR log and the deps_provision_failed SEL event for EVERY
                # nonempty error - this arm, the requirements-read refusal,
                # and a lock failure - so neither is duplicated here; a
                # header line in the backend's own log points the import
                # errors missing deps produce back at provisioning.
    return provision_error

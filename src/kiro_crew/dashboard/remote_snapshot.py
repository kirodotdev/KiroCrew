"""Project snapshot a hub uploads to a remote crew for a placed subagent.

Builds the archive :mod:`kiro_crew.dashboard.remote_subagents` sends to a peer's
``POST /api/remote-workspaces``: the parent project's tracked files at HEAD,
read by the gateway through a pinned directory, filtered by the parent's own
read profile and the shared snapshot exclusions, and bounded to what the peer
accepts on install (:mod:`kiro_crew.dashboard.remote_workspaces`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from kiro_crew.platform_compat import PinnedDirectory, pin_directory

logger = logging.getLogger(__name__)

#: Largest archive the hub sends; the peer refuses anything larger on install.
_MAX_WORKSPACE_ARCHIVE_BYTES = 64 * 1024 * 1024


class _SnapshotRefused(Exception):
    """A project snapshot that must not be built, with the refusal to report."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


#: Bounds the peer enforces on install (``remote_workspaces``); refusing here
#: avoids packaging and sending what it would reject anyway.
_MAX_SNAPSHOT_EXPANDED_BYTES = 512 * 1024 * 1024
_MAX_SNAPSHOT_MEMBERS = 100_000

#: Settings a checkout's own ``.git/config`` can carry that make git run a
#: program. ``-c`` outranks repo config, and global and system config are not
#: read at all (:func:`hardened_git_env`). The two commands run here only list the
#: index and resolve HEAD, so these cover everything either can reach.
_GIT_NEUTRALISED_CONFIG = (
    "core.fsmonitor=",
    f"core.hooksPath={os.devnull}",
    "core.untrackedCache=false",
    "core.pager=cat",
    "diff.external=",
)


def _project_git(git: str, project: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    from kiro_crew.code_fingerprint import hardened_git_env

    argv = [git, "-C", str(project)]
    for setting in _GIT_NEUTRALISED_CONFIG:
        argv += ["-c", setting]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [*argv, *args],
        check=False,
        capture_output=True,
        timeout=60,
        env=hardened_git_env(GIT_OPTIONAL_LOCKS="0"),
    )


def _open_tracked_file(root: PinnedDirectory, rel: str) -> int | None:
    """An fd on *rel* under *root*, opened without following ANY symlink.

    Each directory component is opened refusing a link at that name relative to
    the one before, so a tracked path beneath a symlinked parent
    (``cache -> ~/.aws``) cannot resolve outside the project. ``None`` when the
    entry is not a regular file. *rel* is a git index path, which is
    ``/``-separated on every OS.

    Portable by construction: intermediate components descend through
    :class:`platform_compat.PinnedDirectory`, whose ``child`` refuses a link or
    non-directory at the name on POSIX (``O_DIRECTORY | O_NOFOLLOW``) and on
    Windows (a reparse-point-refusing handle open) alike; the leaf is opened
    with the same pin's own ``_open_file``, which is ``dir_fd``-relative on
    POSIX (so a rename of the parent between the last ``child`` and this open
    cannot redirect it) and :func:`platform_compat.open_file_no_reparse` on
    Windows. Neither POSIX ``dir_fd`` opens nor the
    ``O_DIRECTORY``/``O_NOFOLLOW`` constants they need exist on Windows.
    """
    parts = rel.split(posixpath.sep)
    pins: list[PinnedDirectory] = []
    try:
        current = root
        for part in parts[:-1]:
            current = current.child(part)
            pins.append(current)
        fd = current._open_file(parts[-1])
    except OSError:
        # A symlinked/non-directory component, ENOTDIR, or a tracked file
        # deleted in the worktree: none of them is packaged.
        return None
    finally:
        for opened in pins:
            opened.close()
    import stat as _stat

    info = os.fstat(fd)
    if not _stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        # A second link can alias an excluded file (a tracked name hardlinked to
        # the project's .env): the exclusions judge names, not inodes, and this
        # reader runs outside the agent's sandbox. Such a file is not packaged.
        if info.st_nlink > 1:
            logger.warning("Tracked file %s has more than one link; not packaged", rel)
        os.close(fd)
        return None
    return fd


def _project_read_check(
    parent_session: str, app: str = "", *, agent: str = ""
) -> Callable[[str], bool] | None:
    """The parent's ``filesystem.read`` decision for one tracked file, or ``None``.

    The snapshot is read by the gateway, so the PreToolUse gate that would stop
    the parent's own agent from reading a denied path never sees it. This
    resolves the same ceiling and profile ``governance_permits`` resolves for
    the parent surface, with the parent's *agent* (the one its own tool gate
    passes, so a profile bound to that agent narrows here too) and the calling
    app's own profile when *app* is set, once, and returns a check on the file's
    absolute path, the spelling the
    gate's ``Reading <path>`` item has once ``_match_path`` normalizes it. ``None``
    means nothing governs reads, the standalone default. Any evaluation error
    refuses the snapshot: a wrong permit here uploads the file to another host.
    """
    from kiro_crew.platform.context import PlatformCompositionError, current_context
    from kiro_crew.platform.governance import resolve
    from kiro_crew.platform.governance_profiles import resolve_active_scope

    unavailable = _SnapshotRefused(
        "The parent's file-read policy could not be evaluated; nothing was uploaded.",
        code="remote_project_policy_unavailable",
        status=409,
    )
    try:
        ceiling = getattr(current_context(), "governance", None)
        profile = resolve_active_scope(parent_session, agent=agent, app=app)
    except PlatformCompositionError:
        raise
    except Exception:
        logger.warning("filesystem.read policy unavailable for a remote snapshot", exc_info=True)
        raise unavailable from None
    if ceiling is None and profile is None:
        return None

    def may_read(absolute: str) -> bool:
        try:
            decision = resolve(ceiling, profile, "filesystem.read", absolute)
            permitted = bool(getattr(decision, "permitted", False))
        except PlatformCompositionError:
            raise
        except Exception:
            logger.warning("filesystem.read evaluation failed for a remote snapshot", exc_info=True)
            raise unavailable from None
        if not permitted:
            _audit_snapshot_denial(parent_session, absolute, decision)
        return permitted

    return may_read


def _audit_snapshot_denial(parent_session: str, absolute: str, decision: object) -> None:
    """Record in the SEL that the snapshot left *absolute* out under ``filesystem.read``.

    The PreToolUse gate audits the denials it makes, and this check stands in
    for that gate on a read the gateway does itself, so each denied file gets
    its own event with the parent's attribution, the path, and the deciding rule
    and layer. Permits are not written one per file, as the gate does not write
    them either; the spawn audit records the upload. Best effort, like the
    gate's: the file is already left out, so a failed write cannot widen what
    reaches the peer.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=parent_session,
            tool_name="remote_project_snapshot",
            scope="filesystem.read",
            item=absolute,
            outcome="denied",
            rule=str(getattr(decision, "rule", "") or ""),
            layer=str(getattr(decision, "layer", "") or ""),
            reason=str(getattr(decision, "reason", "") or ""),
        )
    except Exception:
        logger.debug("remote snapshot read-denial audit failed", exc_info=True)


def _snapshot_project(
    project: Path, *, may_read: Callable[[str], bool] | None = None
) -> tuple[bytes, str, str]:
    """The parent project's tracked files as a gzip tarball, its digest and HEAD.

    This runs in the gateway, outside the agent sandbox, on a tree the agent can
    write. So git runs from a trusted binary with no global or system config and
    with every program-running repo setting pinned off, and the files are read by
    this process, never by git: each one is opened without following a symlink at
    any component, must be a regular file, and is checked against the
    sensitive-path fence, the source builder's credential-name rules and, when
    *may_read* is given, the parent's ``filesystem.read`` policy. A file any of
    them refuses is left out unread. Tracked symlinks and submodules are left out
    (the peer would skip them anyway). The project is pinned from the filesystem
    anchor down before git runs (:func:`_pin_project_root`), and every file is
    read through that pin.
    """
    from kiro_crew.platform_compat import trusted_git_bin
    from kiro_crew.security.paths import is_sensitive_resolved_path

    def refused(message: str, code: str, status: int) -> _SnapshotRefused:
        return _SnapshotRefused(message, code=code, status=status)

    git = trusted_git_bin()
    if git is None:
        raise refused(
            "No trusted git executable is available to snapshot the parent project.",
            "remote_project_snapshot_failed",
            400,
        )
    project = Path(os.path.realpath(project))
    if is_sensitive_resolved_path(str(project)):
        raise refused(
            "The parent project is inside a protected directory.",
            "remote_project_protected",
            403,
        )
    moved = refused(
        "The parent project directory changed while it was being packaged.",
        "remote_project_moved",
        409,
    )
    try:
        root = _pin_project_root(project)
    except OSError:
        raise moved from None
    try:
        return _snapshot_pinned(project, root, git, may_read)
    finally:
        root.close()


def _pin_project_root(project: Path) -> PinnedDirectory:
    """*project*, a resolved absolute path, pinned from the filesystem anchor down.

    Every component is opened through the one above it, refusing a link at the
    name, so an ancestor swapped for a symlink after the path was resolved and
    screened (``checkout -> ~/.aws/sso``) makes the open fail instead of
    redirecting every later read. Only the leaf stays open: on POSIX the file
    reads are relative to its descriptor, and on Windows its handle keeps it and
    every ancestor from being renamed.
    """
    chain = [PinnedDirectory(pin_directory(project.anchor), project.anchor)]
    try:
        for part in project.parts[1:]:
            chain.append(chain[-1].child(part))
    except BaseException:
        for pin in chain:
            pin.close()
        raise
    for pin in chain[:-1]:
        pin.close()
    return chain[-1]


def _same_directory(path: Path, root: PinnedDirectory) -> bool:
    """Whether *path* still names the directory *root* holds open."""
    try:
        named = os.stat(path)
        held = os.fstat(root._fd)
    except OSError:
        return False
    return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


def _snapshot_pinned(
    project: Path,
    root: PinnedDirectory,
    git: str,
    may_read: Callable[[str], bool] | None,
) -> tuple[bytes, str, str]:
    """Package the tracked files of the pinned *root*; see :func:`_snapshot_project`."""
    import gzip
    import io
    import stat as _stat
    import tarfile

    from kiro_crew.cloud.source import custom_home_rel_parts, excluded_tracked_path
    from kiro_crew.security.paths import is_sensitive_resolved_path

    def refused(message: str, code: str, status: int) -> _SnapshotRefused:
        return _SnapshotRefused(message, code=code, status=status)

    head = _project_git(git, project, "rev-parse", "HEAD")
    commit = head.stdout.decode("utf-8", "replace").strip() if head.returncode == 0 else ""
    if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise refused(
            "The parent project does not have a verifiable Git HEAD.",
            "remote_project_commit_unknown",
            409,
        )
    listing = _project_git(git, project, "ls-files", "-z", "--stage")
    if listing.returncode != 0:
        raise refused(
            "The parent project's tracked files could not be listed.",
            "remote_project_snapshot_failed",
            400,
        )
    if not _same_directory(project, root):
        # git ran by path: a directory swapped in meanwhile gave the listing.
        raise refused(
            "The parent project directory changed while it was being packaged.",
            "remote_project_moved",
            409,
        )
    home_parts = custom_home_rel_parts(project)
    buffer = io.BytesIO()
    expanded = 0
    members = 0
    seen: set[str] = set()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for entry in sorted(listing.stdout.split(b"\0")):
                if not entry:
                    continue
                meta, _, raw_name = entry.partition(b"\t")
                rel = raw_name.decode("utf-8", "surrogateescape")
                mode = meta.split(b" ", 1)[0]
                if mode not in (b"100644", b"100755"):
                    continue  # symlink (120000) or submodule (160000)
                if rel in seen:
                    continue  # a conflicted path lists one entry per stage
                seen.add(rel)
                if (
                    not rel
                    or rel.startswith("/")
                    or any(part in ("", ".", "..") for part in rel.split(posixpath.sep))
                    or excluded_tracked_path(rel, home_parts)
                    # No component is a link (checked as it is opened), so
                    # the lexical path is the canonical one.
                    or is_sensitive_resolved_path(str(project / rel))
                    or (may_read is not None and not may_read(str(project / rel)))
                ):
                    continue
                fd = _open_tracked_file(root, rel)
                if fd is None:
                    continue
                with os.fdopen(fd, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    expanded += size
                    members += 1
                    if expanded > _MAX_SNAPSHOT_EXPANDED_BYTES or members > _MAX_SNAPSHOT_MEMBERS:
                        raise refused(
                            "The tracked project snapshot is too large for remote execution.",
                            "remote_project_too_large",
                            413,
                        )
                    info = tarfile.TarInfo(rel)
                    info.size = size
                    executable = _stat.S_IMODE(os.fstat(handle.fileno()).st_mode) & 0o111
                    info.mode = 0o755 if executable else 0o644
                    info.mtime = 0
                    tar.addfile(info, handle)
                    if buffer.tell() > _MAX_WORKSPACE_ARCHIVE_BYTES:
                        raise refused(
                            "The tracked project snapshot is too large for remote execution.",
                            "remote_project_too_large",
                            413,
                        )
    payload = buffer.getvalue()
    if len(payload) > _MAX_WORKSPACE_ARCHIVE_BYTES:
        raise refused(
            "The tracked project snapshot is too large for remote execution.",
            "remote_project_too_large",
            413,
        )
    if members == 0:
        raise refused("The tracked project snapshot is empty.", "remote_project_empty", 400)
    return payload, hashlib.sha256(payload).hexdigest(), commit

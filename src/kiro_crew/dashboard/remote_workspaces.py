"""Transfer a tracked project snapshot to a connected remote crew.

The hub builds the archive; this peer endpoint only verifies and installs it.
Archives are content-addressed, bounded before extraction, and may contain only
regular files and directories with relative POSIX names.  Symlink members (which
the hub's tracked-source tarball emits for tracked symlinks) are skipped, never
created; hardlinks, devices, FIFOs and traversal spellings are refused.
Extraction goes through held directory descriptors with exclusive no-follow
creates, so an entry planted in the staging tree is never written through.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tarfile
import threading
import time
from collections.abc import Collection
from pathlib import Path, PurePosixPath

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.pinned_fs import dir_flags, supports_pinned_walk
from kiro_crew.platform_compat import rmtree_force

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 100_000
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}\Z")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40,64}\Z")
_MARKER = ".kirocrew-remote-workspace.json"
_WORKSPACE_TTL_SECS = 7 * 24 * 60 * 60
# ``<digest[:24]>-<nonce>``: one directory per install, never shared by two runs.
_WORKSPACE_NAME_RE = re.compile(r"^[a-f0-9]{24}-[a-f0-9]{8}\Z")
_UPLOAD_READ_CHUNK_BYTES = 256 * 1024
# install_workspace runs in worker threads; one lock keeps a prune from deleting
# a snapshot another upload is installing or handing out for reuse.
_INSTALL_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


class WorkspaceArchiveRejected(ValueError):
    """The supplied project archive is unsafe or inconsistent."""


def _reject_name(name: str) -> None:
    if not name or "\0" in name or "\\" in name:
        raise WorkspaceArchiveRejected("archive contains an unsafe member name")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise WorkspaceArchiveRejected("archive contains an absolute member path")
    path = PurePosixPath(name)
    if not path.parts or path.as_posix() != name or any(part == ".." for part in path.parts):
        raise WorkspaceArchiveRejected("archive contains path traversal")


def _member_filter(member: tarfile.TarInfo, _destination: str) -> tarfile.TarInfo | None:
    _reject_name(member.name)
    if member.issym():
        # Skipped, not created: a link target could point outside the snapshot.
        return None
    if not (member.isfile() or member.isdir()):
        raise WorkspaceArchiveRejected("archive contains a non-file member")
    scrubbed = member.replace(deep=True) if hasattr(member, "replace") else member
    scrubbed.uid = 0
    scrubbed.gid = 0
    scrubbed.uname = ""
    scrubbed.gname = ""
    if scrubbed.isdir():
        scrubbed.mode = 0o755
    else:
        scrubbed.mode = 0o755 if member.mode & 0o111 else 0o644
    return scrubbed


def _validate_archive(payload: bytes) -> None:
    count = 0
    expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            while (member := archive.next()) is not None:
                count += 1
                if count > _MAX_MEMBERS:
                    raise WorkspaceArchiveRejected("archive contains too many members")
                _member_filter(member, "")
                if member.isfile():
                    expanded += max(0, int(member.size))
                    if expanded > _MAX_EXPANDED_BYTES:
                        raise WorkspaceArchiveRejected("archive expands beyond the workspace limit")
    except WorkspaceArchiveRejected:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise WorkspaceArchiveRejected("archive is not a valid gzip tarball") from exc
    if count == 0:
        raise WorkspaceArchiveRejected("archive is empty")


def _can_extract_pinned() -> bool:
    """Whether every create below can go through a held directory descriptor."""
    return (
        supports_pinned_walk()
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
    )


def _create_flags() -> int:
    """Exclusive, no-follow create: a planted link or existing entry fails the open."""
    return (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _open_child_dir(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(name, dir_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise WorkspaceArchiveRejected(
                "a staging directory turned into a link or file during extraction"
            ) from exc
        raise


def _extract_pinned(archive: tarfile.TarFile, staging_fd: int) -> None:
    """Write the archive's files under *staging_fd* without resolving a name twice.

    The staging directory is visible to every process of this user, including
    agents, while the gateway (outside the sandbox) fills it. ``extractall``
    opens each destination by path, so a link planted at a file or parent name
    would redirect the write anywhere this user can write. Here each directory
    is opened ``O_NOFOLLOW`` relative to its parent's descriptor and each file
    is created ``O_CREAT | O_EXCL | O_NOFOLLOW``, so a planted entry fails the
    create instead of being written through. Only the current directory chain
    is held open, which bounds descriptors by depth.
    """
    chain: list[tuple[str, int]] = []

    def dir_fd_for(parts: tuple[str, ...]) -> int:
        common = 0
        while common < min(len(chain), len(parts)) and chain[common][0] == parts[common]:
            common += 1
        while len(chain) > common:
            os.close(chain.pop()[1])
        for name in parts[common:]:
            parent = chain[-1][1] if chain else staging_fd
            chain.append((name, _open_child_dir(parent, name)))
        return chain[-1][1] if chain else staging_fd

    try:
        for member in archive:
            kept = _member_filter(member, "")
            if kept is None:
                continue
            parts = PurePosixPath(kept.name).parts
            if kept.isdir():
                dir_fd_for(parts)
                continue
            parent_fd = dir_fd_for(parts[:-1])
            source = archive.extractfile(member)
            if source is None:
                raise WorkspaceArchiveRejected("archive file member has no data")
            try:
                fd = os.open(parts[-1], _create_flags(), kept.mode, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ELOOP):
                    raise WorkspaceArchiveRejected(
                        "archive member collides with an existing staging entry"
                    ) from exc
                raise
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(source, out)
                if hasattr(os, "fchmod"):
                    os.fchmod(out.fileno(), kept.mode)
    finally:
        for _name, fd in chain:
            os.close(fd)


def _write_marker(staging_fd: int, digest: str, commit: str) -> None:
    try:
        fd = os.open(_MARKER, _create_flags(), 0o600, dir_fd=staging_fd)
    except FileExistsError as exc:
        raise WorkspaceArchiveRejected("archive contains the reserved marker name") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps({"sha256": digest, "commit": commit}, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def _is_held_dir(base_fd: int, name: str, held_fd: int) -> bool:
    """Whether *name* under *base_fd* is still the directory *held_fd* points at."""
    try:
        current = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
    except OSError:
        return False
    held = os.fstat(held_fd)
    return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
        held.st_dev,
        held.st_ino,
    )


def _workspace_root() -> Path:
    return Path.home() / "workplace" / "kirocrew-remote-workspaces"


def _prune_workspaces(
    base: Path,
    *,
    keep: str,
    now: float | None = None,
    in_use: Collection[str] = (),
) -> int:
    """Remove expired, valid workspace snapshots except *keep* and *in_use*.

    *in_use* names the snapshots an active or queued run works in. File edits
    inside a snapshot do not refresh the directory's age, so age alone would
    delete a long-running worker's tree.
    """
    current = time.time() if now is None else now
    pruned = 0
    for child in base.iterdir():
        if (
            child.name == keep
            or child.name in in_use
            or not _WORKSPACE_NAME_RE.fullmatch(child.name)
            or child.is_symlink()
            or not child.is_dir()
        ):
            continue
        marker = child / _MARKER
        if marker.is_symlink() or not marker.is_file():
            continue
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            modified = child.stat().st_mtime
        except (OSError, ValueError, RecursionError):
            logger.warning("Remote workspace %s has an unreadable marker", child.name)
            continue
        marker_digest = metadata.get("sha256") if isinstance(metadata, dict) else None
        if (
            not isinstance(marker_digest, str)
            or not _DIGEST_RE.fullmatch(marker_digest)
            or marker_digest[:24] != child.name[:24]
            or current - modified < _WORKSPACE_TTL_SECS
        ):
            continue
        if rmtree_force(child):
            pruned += 1
        else:
            logger.warning("Could not prune expired remote workspace %s", child)
    return pruned


def install_workspace(
    payload: bytes,
    digest: str,
    commit: str,
    *,
    root: Path | None = None,
    in_use: Collection[str] | None = (),
) -> Path:
    """Verify and atomically install *payload* into a fresh directory; return it.

    Every install gets its own ``<digest[:24]>-<nonce>`` tree. Two runs from the
    same commit (always the case for a remote batch) produce the same digest, and
    a run edits and deletes inside its tree, so handing a later run the earlier
    run's directory would start it from a tree the hub never checked.
    """
    with _INSTALL_LOCK:
        return _install_workspace_locked(payload, digest, commit, root=root, in_use=in_use)


def _install_workspace_locked(
    payload: bytes,
    digest: str,
    commit: str,
    *,
    root: Path | None,
    in_use: Collection[str] | None,
) -> Path:
    if not _DIGEST_RE.fullmatch(digest):
        raise WorkspaceArchiveRejected("invalid archive digest")
    if commit and not _COMMIT_RE.fullmatch(commit):
        raise WorkspaceArchiveRejected("invalid source commit")
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise WorkspaceArchiveRejected("archive exceeds the compressed workspace limit")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise WorkspaceArchiveRejected("archive digest mismatch")
    _validate_archive(payload)
    if not _can_extract_pinned():
        # No by-name fallback: it is exactly the write-through-a-link hole.
        raise WorkspaceArchiveRejected(
            "this platform cannot install a workspace through pinned directory descriptors"
        )

    base = (root or _workspace_root()).resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = base / f"{digest[:24]}-{secrets.token_hex(4)}"
    # ``None``: the caller could not read which trees runs own, so none is
    # provably free and nothing is pruned this time.
    pruned = 0 if in_use is None else _prune_workspaces(base, keep=target.name, in_use=in_use)
    if pruned:
        logger.info("Pruned %d expired remote workspace snapshot(s)", pruned)
    if target.exists() or target.is_symlink():
        raise WorkspaceArchiveRejected("workspace target already exists in an unsafe form")

    base_fd = os.open(base, dir_flags())
    try:
        staging_name = f".{digest[:12]}-{secrets.token_hex(8)}"
        os.mkdir(staging_name, 0o700, dir_fd=base_fd)
        staging_fd = os.open(staging_name, dir_flags(), dir_fd=base_fd)
        try:
            _fill_and_publish(
                payload, digest, commit, base, base_fd, staging_name, staging_fd, target
            )
        finally:
            os.close(staging_fd)
    finally:
        os.close(base_fd)
    return target


def _fill_and_publish(
    payload: bytes,
    digest: str,
    commit: str,
    base: Path,
    base_fd: int,
    staging_name: str,
    staging_fd: int,
    target: Path,
) -> None:
    """Extract into the held staging directory, then rename it to *target*.

    The rename is by name, so it is followed by an identity check: *target*
    must be the very directory the descriptor holds, not a link or another
    directory swapped in for the staging name.
    """
    current = staging_name
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            _extract_pinned(archive, staging_fd)
        _write_marker(staging_fd, digest, commit)
        if not _is_held_dir(base_fd, staging_name, staging_fd):
            raise WorkspaceArchiveRejected("workspace staging was replaced during installation")
        # renameat on the held base: the new name is a fresh nonce no run uses.
        os.rename(staging_name, target.name, src_dir_fd=base_fd, dst_dir_fd=base_fd)
        current = target.name
        if not _is_held_dir(base_fd, current, staging_fd):
            raise WorkspaceArchiveRejected("workspace staging was replaced during installation")
    except BaseException:
        _discard(base, base_fd, current, staging_fd)
        raise


def _discard(base: Path, base_fd: int, name: str, staging_fd: int) -> None:
    """Remove a failed install, but only the tree this call created."""
    if _is_held_dir(base_fd, name, staging_fd):
        if not rmtree_force(base / name):
            logger.error("Remote workspace staging cleanup failed: %s", base / name)
        return
    try:
        if stat.S_ISLNK(os.stat(name, dir_fd=base_fd, follow_symlinks=False).st_mode):
            os.unlink(name, dir_fd=base_fd)
    except OSError:
        pass
    logger.error("Remote workspace staging %s was replaced; left the replacement alone", name)


async def _read_upload(request: web.Request) -> bytes:
    """Read the upload body to EOF, stopping once it exceeds the archive cap.

    A single ``content.read(n)`` returns whatever is buffered, so an archive
    that arrives in several chunks would be cut short and fail its digest.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.content.iter_chunked(_UPLOAD_READ_CHUNK_BYTES):
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_ARCHIVE_BYTES:
            break
    return b"".join(chunks)[: _MAX_ARCHIVE_BYTES + 1]


def _workspace_name(cwd: object, bases: Collection[Path]) -> str | None:
    """The snapshot directory *cwd* lies in, or ``None`` when it is not one."""
    if not isinstance(cwd, str) or not cwd:
        return None
    for base in bases:
        try:
            relative = Path(cwd).relative_to(base)
        except ValueError:
            continue
        if relative.parts and _WORKSPACE_NAME_RE.fullmatch(relative.parts[0]):
            return relative.parts[0]
    return None


async def _workspaces_in_use(request: web.Request) -> frozenset[str] | None:
    """Snapshot names that a live or queued run on this gateway works in.

    Queued runs are not in ``all_agents``: a run waiting for memory keeps its
    ``cwd`` in the dispatch window or a task-store row, so the queued listing is
    read too. ``None`` when ownership cannot be read completely (no manager, a
    partial or failed listing): pruning is then skipped, since an age-expired
    tree may still belong to a run that has not started.
    """
    state = request.app.get("state") if hasattr(request.app, "get") else None
    manager = getattr(state, "subagents", None)
    queued_runs = getattr(manager, "queued_runs_async", None)
    if not callable(queued_runs):
        return None
    try:
        listing = await queued_runs(None)
    except Exception:
        logger.warning("Could not list queued runs; skipping workspace pruning", exc_info=True)
        return None
    if getattr(listing, "partial", True):
        return None
    # Read the live runs AFTER the queued listing: a run admitted while the
    # listing was being read has left the queue, and is registered by now.
    agents = getattr(manager, "all_agents", None)
    if not isinstance(agents, list):
        return None
    bases = (_workspace_root(), _workspace_root().resolve())
    names: set[str] = set()
    cwds = [getattr(info, "cwd", "") for info in agents if not getattr(info, "done", True)]
    cwds += [getattr(run, "cwd", "") for run in getattr(listing, "runs", ())]
    for cwd in cwds:
        name = _workspace_name(cwd, bases)
        if name:
            names.add(name)
    return frozenset(names)


async def api_remote_workspace_upload(request: web.Request) -> web.Response:
    """Install one tracked source snapshot sent through an instance tunnel."""
    if request.get("internal_auth") or request.get("app", ""):
        return _owner_denial_response(request, "dashboard owner required", "owner_only")
    if not is_owner_dashboard_request(request):
        return _owner_denial_response(request, "dashboard owner required", "owner_only")
    digest = str(request.query.get("sha256") or "")
    commit = str(request.query.get("commit") or "")
    payload = await _read_upload(request)
    if len(payload) > _MAX_ARCHIVE_BYTES:
        return web.json_response(
            {
                "error": "archive exceeds the compressed workspace limit",
                "code": "archive_too_large",
            },
            status=413,
        )
    in_use = await _workspaces_in_use(request)
    try:
        path = await asyncio.to_thread(install_workspace, payload, digest, commit, in_use=in_use)
    except WorkspaceArchiveRejected as exc:
        return web.json_response(
            {"error": str(exc), "code": "workspace_archive_rejected"}, status=400
        )
    except OSError:
        return web.json_response(
            {"error": "workspace installation failed", "code": "workspace_install_failed"},
            status=500,
        )
    return web.json_response({"path": str(path), "sha256": digest, "commit": commit})

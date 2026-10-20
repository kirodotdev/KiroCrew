"""Spec Builder's duplicate transaction: stage a copy, publish it, recover an abandoned one.

A duplicate writes its documents into a hidden sibling stage that carries a provenance
marker, then renames the stage into place without replacing anything. Recovery resolves,
once and on the app's first enabled use, a duplicate reservation that a gateway process
left behind when it stopped. The index, its reservation fields and the descriptor-pinned
directory helpers come from :mod:`repository`; from the rest of the backend this module
imports only :mod:`parsers`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path

from aiohttp import web

from kiro_crew.platform_compat import rename_noreplace

from .parsers import _EDITABLE_DOCS, _SHA256_RE, _owns_slot_key
from .repository import (
    _CAN_PIN_DIR,
    _CAN_PUBLISH_DIR_NOREPLACE,
    _DUPLICATE_MARKER,
    _DUPLICATE_RECOVERY_STATE,
    _DUPLICATE_TOKEN_RE,
    _DUPLICATING,
    _INDEX_LOCK,
    _MAX_SPEC_BYTES,
    _create_open_verified_dir,
    _load_index,
    _open_verified_dir,
    _refresh_slot_keys,
    _reservation_is_ours,
    _safe_dir,
    _save_index,
    _verified_spec_dir,
    logger,
)


def _create_spec_doc(
    spec_dir: Path,
    fname: str,
    text: str,
    expected_dir_identity: tuple[int, int] | None = None,
) -> tuple[str, tuple[int, int, int, int] | None]:
    """Create one absent spec document and return a rollback identity.

    Duplication owns an empty destination, so ``O_EXCL`` gives it a real atomic
    boundary: an IDE or agent that creates the same file first wins and is never
    overwritten. The returned stat tuple lets failure cleanup remove only the
    exact file this call created; a file replaced or modified by another writer
    is left alone.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    if fname not in _EDITABLE_DOCS:
        return "not_editable", None
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_SPEC_BYTES:
        return "too_large", None
    if not _CAN_PIN_DIR:
        return "unsupported_platform", None
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return "unsafe_dir", None
    _real_dir, dir_fd = opened_dir
    if expected_dir_identity is not None:
        try:
            dir_info = os.fstat(dir_fd)
            if (dir_info.st_dev, dir_info.st_ino) != expected_dir_identity:
                os.close(dir_fd)
                return "identity_mismatch", None
        except OSError:
            os.close(dir_fd)
            return "identity_mismatch", None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(fname, flags, 0o600, dir_fd=dir_fd)
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("document write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
            stat = os.fstat(fd)
            return "", (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            # Return the exact partial inode to the duplicate transaction. Its
            # rollback removes it only if no other writer replaced or modified it.
            try:
                stat = os.fstat(fd)
                identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            except OSError:
                identity = None
            return "write_failed", identity
        finally:
            os.close(fd)
            fd = -1
    except FileExistsError:
        return "conflict", None
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        return "write_failed", None
    finally:
        os.close(dir_fd)


def _rollback_staged_docs(spec_dir: Path, created: dict[str, tuple[int, int, int, int]]) -> bool:
    """Remove unchanged files created by a failed duplicate.

    Cleanup deliberately leaves the empty hidden stage directory. POSIX has no
    portable inode-bound rmdir, so removing it by name would reopen a race where
    an attacker swaps in a different directory after descriptor validation.

    Returns true only when no editable document remains. The provenance marker
    stays in place until the caller durably releases the index reservation, so a
    crash during rollback still leaves recovery authority for any residue.
    """
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return False
    _, dir_fd = opened_dir
    try:
        for fname in _EDITABLE_DOCS:
            try:
                stat = os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
                identity = created.get(fname)
                current = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if identity is not None and current == identity:
                    os.unlink(fname, dir_fd=dir_fd)
            except OSError:
                continue
        for fname in _EDITABLE_DOCS:
            try:
                os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
                return False
            except FileNotFoundError:
                continue
            except OSError:
                return False
        return True
    finally:
        os.close(dir_fd)


def _write_duplicate_marker_at(dir_fd: int, token: str) -> bool:
    """Create the provenance marker relative to an already verified directory."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(_DUPLICATE_MARKER, flags, 0o600, dir_fd=dir_fd)
        remaining = memoryview(token.encode("ascii"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                return False
            remaining = remaining[written:]
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _duplicate_marker_matches_at(dir_fd: int, token: str) -> bool:
    """Read a duplicate marker relative to an already verified directory."""
    fd = -1
    try:
        fd = os.open(
            _DUPLICATE_MARKER,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=dir_fd,
        )
        return os.read(fd, 256).decode("ascii", errors="strict") == token
    except (OSError, UnicodeError):
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _duplicate_marker_matches(spec_dir: Path, token: str) -> bool:
    """Read a duplicate provenance marker without following directory links."""
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return False
    _real_dir, dir_fd = opened_dir
    try:
        return _duplicate_marker_matches_at(dir_fd, token)
    finally:
        os.close(dir_fd)


def _duplicate_stage_identity(stage_dir: Path, token: str) -> tuple[int, int] | None:
    """Return the inode identity of a descriptor-pinned, matching stage."""
    opened_dir = _open_verified_dir(stage_dir)
    if opened_dir is None:
        return None
    _real_dir, dir_fd = opened_dir
    try:
        if not _duplicate_marker_matches_at(dir_fd, token):
            return None
        info = os.fstat(dir_fd)
        return info.st_dev, info.st_ino
    except OSError:
        return None
    finally:
        os.close(dir_fd)


def _create_duplicate_stage(stage_dir: Path, token: str) -> str:
    """Create and durably mark a hidden stage before its index reservation."""
    if not _CAN_PUBLISH_DIR_NOREPLACE:
        return "unsupported_platform"
    opened_stage = _create_open_verified_dir(stage_dir)
    if opened_stage is None:
        return "write_failed"
    _, stage_fd, parent_fd = opened_stage
    try:
        if _write_duplicate_marker_at(stage_fd, token):
            try:
                # The marker must survive before the index can name this stage.
                # Persist both the marker entry and the stage's parent entry.
                os.fsync(stage_fd)
                os.fsync(parent_fd)
                return ""
            except OSError:
                pass
        try:
            os.unlink(_DUPLICATE_MARKER, dir_fd=stage_fd)
            os.fsync(stage_fd)
        except OSError:
            pass
        return "unsupported_platform" if not _CAN_PIN_DIR else "write_failed"
    finally:
        os.close(stage_fd)
        os.close(parent_fd)


def _remove_duplicate_marker(
    spec_dir: Path, token: str, expected_identity: tuple[int, int] | None = None
) -> None:
    """Remove only the matching marker from a descriptor-pinned directory."""
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return
    _real_dir, dir_fd = opened_dir
    try:
        if expected_identity is not None:
            info = os.fstat(dir_fd)
            if (info.st_dev, info.st_ino) != expected_identity:
                return
        if _duplicate_marker_matches_at(dir_fd, token):
            os.unlink(_DUPLICATE_MARKER, dir_fd=dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _duplicate_manifest_is_valid(documents: object) -> bool:
    """True when recovery metadata names only complete document digests."""
    if not isinstance(documents, dict) or not documents:
        return False
    for fname, digest in documents.items():
        if fname not in _EDITABLE_DOCS or not isinstance(digest, str) or len(digest) != 64:
            return False
        if _SHA256_RE.fullmatch(digest) is None:
            return False
    return True


def _duplicate_documents_match_at(dir_fd: int, documents: object) -> bool:
    """Validate the complete reserved payload through one pinned directory."""
    if not _duplicate_manifest_is_valid(documents):
        return False
    assert isinstance(documents, dict)
    for fname, digest in documents.items():
        fd = -1
        try:
            fd = os.open(
                fname,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=dir_fd,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > _MAX_SPEC_BYTES
            ):
                return False
            remaining = info.st_size + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
                return False
        except OSError:
            return False
        finally:
            if fd >= 0:
                os.close(fd)
    return True


def _clear_duplicate_stage_documents_at(dir_fd: int, token: str, documents: object) -> bool:
    """Remove marker-provenanced documents through their proven stage inode.

    The marker proves ownership of the directory, while the manifest digest
    proves ownership of each document. Recovery must preserve the reservation
    if a present document does not match; a project writer may have moved an
    unrelated file into an abandoned stage before recovery runs.

    The marker remains until the matching index transition is durably saved.
    The empty stage remains because directory removal cannot be bound to its
    already-open inode across the final pathname-based rmdir syscall.
    """
    if not _duplicate_manifest_is_valid(documents) or not _duplicate_marker_matches_at(
        dir_fd, token
    ):
        return False
    assert isinstance(documents, dict)
    opened: dict[str, tuple[int, int, int]] = {}
    owned_fds: list[int] = []
    try:
        for fname, digest in documents.items():
            fd = -1
            try:
                fd = os.open(
                    fname,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=dir_fd,
                )
            except FileNotFoundError:
                continue
            except OSError:
                return False
            owned_fds.append(fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > _MAX_SPEC_BYTES
            ):
                return False
            opened[fname] = (fd, info.st_dev, info.st_ino)
            remaining = info.st_size + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
                return False

        if not _duplicate_marker_matches_at(dir_fd, token):
            return False
        for fname, (_fd, expected_dev, expected_ino) in opened.items():
            current = os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (expected_dev, expected_ino):
                return False
        for fname in opened:
            os.unlink(fname, dir_fd=dir_fd)
        os.fsync(dir_fd)
        for fname in opened:
            try:
                os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
                return False
            except FileNotFoundError:
                continue
            except OSError:
                return False
        return _duplicate_marker_matches_at(dir_fd, token)
    except OSError:
        return False
    finally:
        for fd in owned_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _publish_staged_copy(stage_dir: Path, target_dir: Path) -> str:
    """Atomically publish a sibling staging directory without replacement."""
    if not _CAN_PUBLISH_DIR_NOREPLACE or stage_dir.parent != target_dir.parent:
        return "unsupported_platform"
    real_stage = _verified_spec_dir(stage_dir)
    real_parent = _safe_dir(str(stage_dir.parent))
    if real_stage is None or real_parent is None or real_stage.parent != real_parent:
        return "unsafe_dir"
    opened_parent = _open_verified_dir(real_parent)
    if opened_parent is None:
        return "unsafe_dir"
    _real_parent, parent_fd = opened_parent
    try:
        try:
            rename_noreplace(
                stage_dir.name,
                target_dir.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except FileExistsError:
            return "conflict"
        except NotImplementedError:
            return "unsupported_platform"
        except OSError:
            return "write_failed"
        try:
            os.fsync(parent_fd)
        except OSError:
            # The atomic rename already completed. Treating a filesystem that
            # rejects directory fsync as failure would orphan the published copy.
            logger.debug("duplicate parent directory fsync unavailable", exc_info=True)
        return ""
    finally:
        os.close(parent_fd)


def _publish_pinned_staged_copy(
    stage_dir: Path, target_dir: Path, stage_fd: int, token: str
) -> str:
    """Publish and prove the renamed name still identifies the pinned stage."""
    try:
        expected = os.fstat(stage_fd)
    except OSError:
        return "identity_mismatch"
    if not _duplicate_marker_matches_at(stage_fd, token):
        return "identity_mismatch"
    result = _publish_staged_copy(stage_dir, target_dir)
    if result:
        return result
    opened_target = _open_verified_dir(target_dir)
    if opened_target is None:
        return "identity_mismatch"
    _, target_fd = opened_target
    try:
        published = os.fstat(target_fd)
        if (published.st_dev, published.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ) or not _duplicate_marker_matches_at(target_fd, token):
            return "identity_mismatch"
        return ""
    except OSError:
        return "identity_mismatch"
    finally:
        os.close(target_fd)


_DUPLICATE_RECOVERY_ADOPT = "adopt"
_DUPLICATE_RECOVERY_DISCARD = "discard"
_DUPLICATE_RECOVERY_RELEASE = "release"
_DUPLICATE_RECOVERY_RETRY = "retry"


def _recover_abandoned_copy(name: str, meta: dict) -> tuple[str, Path | None]:
    """Resolve a duplicate and identify any marker removable after index save."""
    held = meta.get(_DUPLICATING)
    if not isinstance(held, dict):
        return _DUPLICATE_RECOVERY_RELEASE, None
    owner = held.get("owner")
    reserved_at = held.get("at")
    token = held.get("token")
    stage_raw = held.get("stage_dir")
    stage_dev = held.get("stage_dev")
    stage_ino = held.get("stage_ino")
    documents = held.get("documents")
    target_raw = meta.get("spec_dir")
    slot_key = meta.get("slot_key")
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(reserved_at, (int, float))
        or not isinstance(token, str)
        or not token
        or not isinstance(stage_raw, str)
        or not stage_raw
        or type(stage_dev) is not int
        or stage_dev < 0
        or type(stage_ino) is not int
        or stage_ino < 0
        or not isinstance(target_raw, str)
        or not target_raw
        or not isinstance(slot_key, str)
        or not _owns_slot_key(name, slot_key)
        or not _duplicate_manifest_is_valid(documents)
    ):
        return _DUPLICATE_RECOVERY_RELEASE, None
    if _DUPLICATE_TOKEN_RE.fullmatch(token) is None:
        return _DUPLICATE_RECOVERY_RELEASE, None
    target_dir = Path(target_raw)
    stage_dir = Path(stage_raw)
    expected_stage = target_dir.parent / f".{name}.duplicate-{token}"
    if (
        not target_dir.is_absolute()
        or target_dir.name != name
        or stage_dir != expected_stage
        or stage_dir.parent != target_dir.parent
    ):
        return _DUPLICATE_RECOVERY_RELEASE, None
    # Before publication a genuine duplicate has no target directory. Any target
    # without our marker is user data (or a concurrent writer's winning create),
    # never transaction residue that recovery may delete from the index.
    if target_dir.exists():
        opened_target = _open_verified_dir(target_dir)
        if opened_target is not None:
            _, target_fd = opened_target
            try:
                target_info = os.fstat(target_fd)
                if (target_info.st_dev, target_info.st_ino) == (
                    stage_dev,
                    stage_ino,
                ) and _duplicate_marker_matches_at(target_fd, token):
                    return _DUPLICATE_RECOVERY_ADOPT, target_dir
            except OSError:
                pass
            finally:
                os.close(target_fd)
        opened_stage = _open_verified_dir(stage_dir)
        if opened_stage is not None:
            _, stage_fd = opened_stage
            try:
                stage_info = os.fstat(stage_fd)
                if (stage_info.st_dev, stage_info.st_ino) == (
                    stage_dev,
                    stage_ino,
                ) and _duplicate_marker_matches_at(stage_fd, token):
                    if not _clear_duplicate_stage_documents_at(stage_fd, token, documents):
                        return _DUPLICATE_RECOVERY_RETRY, None
                    return _DUPLICATE_RECOVERY_DISCARD, stage_dir
            except OSError:
                pass
            finally:
                os.close(stage_fd)
        # The recorded transaction inode is unreachable at either name.
        # Keep the reservation hidden: clearing it would adopt the unrelated
        # target after a crash in the post-rename identity-check window.
        return _DUPLICATE_RECOVERY_RETRY, None
    opened_stage = _open_verified_dir(stage_dir)
    if opened_stage is None:
        return _DUPLICATE_RECOVERY_RETRY, None
    _, stage_fd = opened_stage
    try:
        stage_info = os.fstat(stage_fd)
        if (stage_info.st_dev, stage_info.st_ino) != (
            stage_dev,
            stage_ino,
        ) or not _duplicate_marker_matches_at(stage_fd, token):
            return _DUPLICATE_RECOVERY_RETRY, None
        if not _duplicate_documents_match_at(stage_fd, documents):
            if not _clear_duplicate_stage_documents_at(stage_fd, token, documents):
                return _DUPLICATE_RECOVERY_RETRY, None
            return _DUPLICATE_RECOVERY_DISCARD, stage_dir
        publish_result = _publish_pinned_staged_copy(stage_dir, target_dir, stage_fd, token)
        if publish_result == "":
            return _DUPLICATE_RECOVERY_ADOPT, target_dir
        if publish_result == "identity_mismatch":
            # The still-open descriptor proves the renamed directory was not the
            # validated transaction. Never finalize the index around its files.
            return _DUPLICATE_RECOVERY_DISCARD, None
        if not _clear_duplicate_stage_documents_at(stage_fd, token, documents):
            return _DUPLICATE_RECOVERY_RETRY, None
        return _DUPLICATE_RECOVERY_DISCARD, stage_dir
    except OSError:
        return _DUPLICATE_RECOVERY_RETRY, None
    finally:
        os.close(stage_fd)


def _recover_abandoned_reservations() -> None:
    """Recover duplicate transactions once and persist their terminal state.

    BLOCKING -- first enabled use runs this through ``asyncio.to_thread``.
    The index lock serializes the filesystem recovery with every index mutation,
    and the cleaned index is saved in the same critical section so a later poll
    never repeats a rename/unlink transaction that startup already resolved.
    """
    with _INDEX_LOCK:
        index = _load_index()
        abandoned = [
            name
            for name, meta in index.items()
            if _DUPLICATING in meta and not _reservation_is_ours(meta, _DUPLICATING)
        ]
        if not abandoned:
            return
        recovered = 0
        released = 0
        markers_to_remove: list[tuple[Path, str, tuple[int, int]]] = []
        for name in abandoned:
            meta = index[name]
            held = meta.get(_DUPLICATING)
            marker_token = held.get("token", "") if isinstance(held, dict) else ""
            marker_identity = (
                (held.get("stage_dev"), held.get("stage_ino"))
                if isinstance(held, dict)
                else (None, None)
            )
            outcome, marker_dir = _recover_abandoned_copy(name, meta)
            if outcome == _DUPLICATE_RECOVERY_ADOPT:
                index[name].pop(_DUPLICATING, None)
                recovered += 1
            elif outcome == _DUPLICATE_RECOVERY_DISCARD:
                index.pop(name, None)
                released += 1
            elif outcome == _DUPLICATE_RECOVERY_RELEASE:
                # Malformed/unproven metadata is not authority to delete a real
                # spec record, its approvals, or its conversation linkage.
                index[name].pop(_DUPLICATING, None)
                released += 1
            else:
                # Keep both reservation and marker when cleanup cannot prove a
                # safe terminal state. A later process retries the transaction.
                continue
            if (
                marker_dir is not None
                and type(marker_identity[0]) is int
                and type(marker_identity[1]) is int
            ):
                markers_to_remove.append(
                    (
                        marker_dir,
                        str(marker_token),
                        (marker_identity[0], marker_identity[1]),
                    )
                )
        _save_index(index)
        _refresh_slot_keys(index)
        # Marker removal is deliberately after the durable index transition.
        # A crash from here can strand only a harmless marker in an empty stage
        # or committed target; it cannot create markerless transaction metadata.
        for marker_dir, marker_token, marker_identity in markers_to_remove:
            _remove_duplicate_marker(marker_dir, marker_token, marker_identity)
    logger.info(
        "spec index: recovered %d and released %d duplicate reservation(s) "
        "abandoned by an earlier process",
        recovered,
        released,
    )


async def _recover_abandoned_reservations_on_first_use() -> None:
    """Run recovery off-loop without making an abandoned copy disable the app."""
    try:
        await asyncio.to_thread(_recover_abandoned_reservations)
    except Exception:
        # A reservation remains hidden and keeps its name reserved when recovery
        # cannot prove a safe terminal state. Retry on the next gateway process,
        # rather than failing every poll or taking unrelated app routes down.
        logger.exception("spec index: abandoned duplicate recovery failed")


async def _ensure_duplicate_recovery(app: web.Application) -> None:
    """Recover once on first enabled use, after the gateway is already ready."""
    recovery = app[_DUPLICATE_RECOVERY_STATE]
    task = recovery["task"]
    if task is None:
        # Request handlers for one Application share an event loop. There is no
        # await between checking and publishing the task, so concurrent first
        # requests cannot start two filesystem transactions.
        task = asyncio.create_task(_recover_abandoned_reservations_on_first_use())
        recovery["task"] = task
    await task


def _write_and_publish_duplicate(
    stage_dir: Path,
    target_dir: Path,
    docs: dict[str, str | None],
    token: str,
    expected_stage_identity: tuple[int, int] | None = None,
) -> tuple[str, dict[str, tuple[int, int, int, int]]]:
    """Populate a hidden sibling directory, then atomically publish it. BLOCKING."""

    created: dict[str, tuple[int, int, int, int]] = {}
    if not _CAN_PUBLISH_DIR_NOREPLACE:
        return "unsupported_platform", created
    opened_stage = _open_verified_dir(stage_dir)
    if opened_stage is None:
        return "write_failed", created
    _, stage_fd = opened_stage
    try:
        stage_info = os.fstat(stage_fd)
        opened_identity = (stage_info.st_dev, stage_info.st_ino)
        if expected_stage_identity is None:
            expected_stage_identity = opened_identity
        elif opened_identity != expected_stage_identity:
            return "identity_mismatch", created
        if not _duplicate_marker_matches_at(stage_fd, token):
            return "identity_mismatch", created
    except OSError:
        return "identity_mismatch", created
    finally:
        os.close(stage_fd)
    for fname, text in docs.items():
        if text is None:
            continue
        result, identity = _create_spec_doc(stage_dir, fname, text, expected_stage_identity)
        if identity is not None:
            created[fname] = identity
        if result:
            return result, created
    opened_stage = _open_verified_dir(stage_dir)
    if opened_stage is None:
        return "write_failed", created
    _, stage_fd = opened_stage
    try:
        stage_info = os.fstat(stage_fd)
        if (
            stage_info.st_dev,
            stage_info.st_ino,
        ) != expected_stage_identity or not _duplicate_marker_matches_at(stage_fd, token):
            return "identity_mismatch", created
        # Each document inode is already fsynced. Persist their directory entries
        # before the atomic rename makes this directory visible at the target.
        os.fsync(stage_fd)
        return _publish_pinned_staged_copy(stage_dir, target_dir, stage_fd, token), created
    except OSError:
        return "write_failed", created
    finally:
        os.close(stage_fd)

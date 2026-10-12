"""Descriptor-pinned access to auto-skill state: the public and private roots a claim
moves between, the native identity captured from an opened handle, the pinned parent
chain every claim operation walks, and the no-follow child operations (stat, rename
without replacing, unlink, private-tree removal, exclusive create) that act on one
entry of a pinned parent. ``_DIR_FD_SUPPORTED`` lives here because every one of those
operations branches on it.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Iterator, cast

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import fsync_open_dir
from kiro_crew.config.loader import config_dir
from kiro_crew.constants import AUTO_SKILL_PRIVATE_STATE_DIRNAME
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.skills_auto_model import (
    _AUTHORITY_PROVENANCE_NAME,
    _AUTHORITY_PROVENANCE_PARENT,
    AUTO_CLAIMS_DIRNAME,
    AUTO_EVIDENCE_DIRNAME,
    AUTO_LIVE_QUARANTINE_DIRNAME,
    AUTO_LOCKS_DIRNAME,
    AUTO_QUARANTINE_DIRNAME,
    AUTO_SKILL_NAMESPACE,
    _PinnedSkillParent,
    _TaggedFileIdentity,
)

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


# Whether skill CRUD can address the skill directory and its SKILL.md relative to
# a pinned parent descriptor. supports_pinned_walk covers the openat capability
# itself; the extras are exactly the OTHER descriptor-relative syscalls the CRUD
# pinned branches issue (create, update and create's rollback in
# ``skill_runtime/authoring.py``, delete in ``kiro_crew.skills``), named one per call
# site so the probe stays derived from the code rather than copied from a neighbour:
#   os.mkdir  -- create, the leaf skill directory under the pinned parent
#   os.unlink -- create's rollback (the partial SKILL.md), and update, via
#                atomic_write's staging cleanup under the pinned parent
#   os.stat   -- delete, via pinned_fs.stat_at, and create's rollback, via
#                pinned_fs.remove_dir_verified (os.lstat is not a supports_dir_fd
#                member even on Linux; the capability belongs to os.stat)
#   os.rename -- create's rollback, via remove_dir_verified's stage-aside
#   os.rmdir  -- create's rollback, both the staged-aside directory and the
#                reclaim when the leaf open loses a race to the mkdir
# delete's own removal is still a by-name shutil.rmtree, the residual documented
# there -- os.rmdir is here for the ROLLBACK, not for that. update additionally
# needs a descriptor-relative rename for atomic_write's publish, which is that
# module's own probe and is asked at the call site. Where this is False (Windows)
# the by-name create/write/rmtree are the floor, unchanged.
_DIR_FD_SUPPORTED = pinned_fs.supports_pinned_walk() and {
    os.mkdir,
    os.unlink,
    os.stat,
    os.rename,
    os.rmdir,
}.issubset(os.supports_dir_fd)


def _quarantine_root(self) -> Path:
    return self._dir / AUTO_SKILL_NAMESPACE / AUTO_QUARANTINE_DIRNAME


def _live_quarantine_root(self) -> Path:
    return self._dir / AUTO_SKILL_NAMESPACE / AUTO_LIVE_QUARANTINE_DIRNAME


def _private_root(self) -> Path:
    return (
        config_dir().resolve(strict=True)
        / _AUTHORITY_PROVENANCE_PARENT
        / AUTO_SKILL_PRIVATE_STATE_DIRNAME
    )


def _authority_provenance_path(self) -> Path:
    return (
        config_dir().resolve(strict=True)
        / _AUTHORITY_PROVENANCE_PARENT
        / _AUTHORITY_PROVENANCE_NAME
    )


def _tag_opened_identity(fd: int) -> _TaggedFileIdentity | None:
    raw = platform_compat.opened_file_identity(fd)
    if raw is None:
        return None
    volume, object_id = raw
    if platform_compat.IS_WINDOWS:
        if (
            not isinstance(object_id, bytes)
            or not object_id.startswith(b"F128")
            or len(object_id) != 20
        ):
            return None
        return _TaggedFileIdentity("windows-file-id-128", int(volume), object_id[4:])
    if not isinstance(object_id, int):
        return None
    return _TaggedFileIdentity("posix-dev-ino", int(volume), int(object_id))


def _identity_payload(identity: _TaggedFileIdentity) -> dict[str, object]:
    if identity.kind == "windows-file-id-128" and isinstance(identity.object_id, bytes):
        return {
            "kind": identity.kind,
            "volume": identity.volume,
            "file_id": base64.b64encode(identity.object_id).decode("ascii"),
        }
    if identity.kind == "posix-dev-ino" and isinstance(identity.object_id, int):
        return {
            "kind": identity.kind,
            "device": identity.volume,
            "inode": identity.object_id,
        }
    raise ValueError("unsupported file identity")


def _identity_from_payload(value: object) -> _TaggedFileIdentity | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "posix-dev-ino":
        device = value.get("device")
        inode = value.get("inode")
        if isinstance(device, int) and device >= 0 and isinstance(inode, int) and inode >= 0:
            return _TaggedFileIdentity(kind, device, inode)
        return None
    if kind == "windows-file-id-128":
        volume = value.get("volume")
        encoded = value.get("file_id")
        if not isinstance(volume, int) or volume < 0 or not isinstance(encoded, str):
            return None
        try:
            file_id = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        if len(file_id) != 16:
            return None
        return _TaggedFileIdentity(kind, volume, file_id)
    return None


def _claims_root(self) -> Path:
    return self._private_root() / AUTO_CLAIMS_DIRNAME


def _evidence_root(self) -> Path:
    return self._private_root() / AUTO_EVIDENCE_DIRNAME


def _locks_root(self) -> Path:
    return self._private_root() / AUTO_LOCKS_DIRNAME


def _pinned_parent_matches(pin: _PinnedSkillParent) -> bool:
    """Whether *pin.path* still names the directory held by *pin.fd*."""
    try:
        opened = os.fstat(pin.fd)
        opened_identity = _tag_opened_identity(pin.fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened_identity is None
            or opened_identity != pin.native_identity
        ):
            return False
        if platform_compat.IS_WINDOWS:
            return platform_compat.opened_path_identity_matches(pin.fd, pin.path)
        if (opened.st_dev, opened.st_ino) != pin.identity:
            return False
        named = os.stat(pin.path, follow_symlinks=False)
        return stat.S_ISDIR(named.st_mode) and os.path.samestat(opened, named)
    except (OSError, ValueError):
        return False


def _sync_pinned_parent(self, parent: _PinnedSkillParent) -> None:
    """Durably sync one authenticated parent without reopening its name."""
    if not self._pinned_parent_matches(parent):
        raise OSError("skill-state parent changed before directory sync")
    fsync_open_dir(parent.fd, parent.path)
    if not self._pinned_parent_matches(parent):
        raise OSError("skill-state parent changed during directory sync")


def _sync_pinned_rename_parents(
    self,
    source: _PinnedSkillParent,
    destination: _PinnedSkillParent,
) -> None:
    """Sync both parents after a cross-directory authority rename.

    Destination first: once its name is durable, a source-side failure can
    at worst resurrect a duplicate. Sync the source next so recovery never
    has to distinguish that resurrection from an interrupted transition.
    """
    self._sync_pinned_parent(destination)
    if source.native_identity != destination.native_identity:
        self._sync_pinned_parent(source)


def _opened_path_matches(fd: int, path: Path, expected: os.stat_result) -> bool:
    """Authenticate an opened entry against a pre-open lstat result.

    Windows CRT inode fields are not a stable comparison between a by-name
    stat and a handle. Once the native no-reparse identity check authenticates
    the held descriptor, it is the authority there. POSIX retains ``samestat``
    because its descriptor-relative paths depend on that inode comparison.
    """
    if platform_compat.IS_WINDOWS:
        return platform_compat.opened_path_identity_matches(fd, path)
    return os.path.samestat(expected, os.fstat(fd))


@contextlib.contextmanager
def _pin_skill_parent(self, path: Path) -> Iterator[_PinnedSkillParent]:
    """Open one real directory and keep its name-to-identity binding live."""
    if _DIR_FD_SUPPORTED:
        fd = pinned_fs.open_dir_pinned(path, what="skill-state parent", refusal=OSError)
    else:
        fd = platform_compat.pin_directory(path)
    try:
        opened = os.fstat(fd)
        native_identity = self._tag_opened_identity(fd)
        if native_identity is None:
            raise OSError(f"skill-state parent identity is unavailable: {path}")
        pin = _PinnedSkillParent(
            path,
            fd,
            (opened.st_dev, opened.st_ino),
            native_identity,
        )
        if not self._pinned_parent_matches(pin):
            raise OSError(f"skill-state parent changed while opening: {path}")
        yield pin
    finally:
        os.close(fd)


@contextlib.contextmanager
def _pin_skill_child_parent(
    self,
    parent: _PinnedSkillParent,
    name: str,
    *,
    create: bool,
    created_out: list[bool] | None = None,
) -> Iterator[_PinnedSkillParent]:
    """Create or open one directory while retaining its pinned parent."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe skill-state child directory name")
    if not self._pinned_parent_matches(parent):
        raise OSError("skill-state parent changed before child open")
    child_path = parent.path / name
    created = False
    try:
        child_info = self._stat_pinned_child(parent, name)
    except FileNotFoundError:
        if not create:
            raise
        if not self._pinned_parent_matches(parent):
            raise OSError("skill-state parent changed before child create")
        if _DIR_FD_SUPPORTED:
            os.mkdir(name, 0o700, dir_fd=parent.fd)
        else:
            os.mkdir(child_path, 0o700)
        created = True
        child_info = self._stat_pinned_child(parent, name)
    if not stat.S_ISDIR(child_info.st_mode) or is_link_or_junction(child_path):
        raise OSError(f"unsafe skill-state child directory: {child_path}")
    if not self._pinned_parent_matches(parent):
        raise OSError("skill-state parent changed before child pin")
    if _DIR_FD_SUPPORTED:
        fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent.fd)
    else:
        fd = platform_compat.pin_directory(child_path)
    try:
        opened = os.fstat(fd)
        native_identity = self._tag_opened_identity(fd)
        if native_identity is None:
            raise OSError(f"skill-state child identity is unavailable: {child_path}")
        child = _PinnedSkillParent(
            child_path,
            fd,
            (opened.st_dev, opened.st_ino),
            native_identity,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not self._pinned_parent_matches(parent)
            or not self._pinned_parent_matches(child)
            or not self._opened_path_matches(fd, child_path, child_info)
        ):
            raise OSError(f"skill-state child changed while opening: {child_path}")
        if create:
            # The child descriptor authenticates what the name currently
            # reaches; syncing that child cannot make the name durable in
            # its parent. Flush the already-held parent before any caller
            # can use this authority directory. Repeat for an existing
            # child on a create-capable path so a prior interrupted create
            # that returned before this boundary is repaired on retry.
            self._sync_pinned_parent(parent)
        if created_out is not None:
            created_out[:] = [created]
        yield child
    finally:
        os.close(fd)


def _restrict_private_parent(self, parent: _PinnedSkillParent) -> None:
    """Make one authenticated private directory owner-only on this platform."""
    if platform_compat.IS_POSIX:
        platform_compat.fchmod_safe(parent.fd, 0o700)
    else:
        platform_compat.restrict_dir_to_owner(parent.path)
    if not self._pinned_parent_matches(parent):
        raise OSError("private skill-state root changed during permission lockdown")


def _read_pinned_regular_file(
    self,
    parent: _PinnedSkillParent,
    name: str,
    *,
    max_bytes: int,
) -> bytes | None:
    fd: int | None = None
    try:
        before = self._stat_pinned_child(parent, name)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        path = parent.path / name
        if platform_compat.IS_WINDOWS:
            fd = platform_compat.open_file_no_reparse(path)
        else:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if _DIR_FD_SUPPORTED:
                fd = os.open(name, flags, dir_fd=parent.fd)
            else:
                fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not self._opened_path_matches(fd, path, before)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            return None
        captured = self._stable_file_payload(fd, opened, max_bytes=max_bytes)
        if captured is None or not self._pinned_parent_matches(parent):
            return None
        return captured[0]
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _stat_pinned_child(pin: _PinnedSkillParent, name: str) -> os.stat_result:
    """lstat one child under *pin* without following the child itself."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe skill-state child name")
    if os.name == "nt":
        return os.stat(pin.path / name, follow_symlinks=False)
    return os.stat(name, dir_fd=pin.fd, follow_symlinks=False)


def _pinned_child_identity(
    self,
    parent: _PinnedSkillParent,
    name: str,
) -> _TaggedFileIdentity | None:
    """Open one current child without following it and return native identity."""
    fd: int | None = None
    try:
        current = self._stat_pinned_child(parent, name)
        path = parent.path / name
        if platform_compat.IS_WINDOWS:
            fd = platform_compat.open_path_no_reparse(path)
            opened = os.fstat(fd)
            opened_attrs = getattr(opened, "st_file_attributes", None)
            current_is_reparse = bool(
                getattr(current, "st_reparse_tag", 0) or is_link_or_junction(path)
            )
            if opened_attrs is not None:
                opened_is_reparse = bool(opened_attrs & 0x00000400)
                if current_is_reparse != opened_is_reparse:
                    return None
            identity = self._tag_opened_identity(fd)
            if identity is None or not self._pinned_parent_matches(parent):
                return None
            return identity
        if stat.S_ISDIR(current.st_mode):
            if _DIR_FD_SUPPORTED:
                fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent.fd)
            else:
                fd = platform_compat.pin_directory(path)
        elif stat.S_ISREG(current.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if _DIR_FD_SUPPORTED:
                fd = os.open(name, flags, dir_fd=parent.fd)
            else:
                fd = os.open(path, flags)
        elif stat.S_ISLNK(current.st_mode):
            return _TaggedFileIdentity(
                "posix-dev-ino",
                int(current.st_dev),
                int(current.st_ino),
            )
        else:
            return None
        if not self._opened_path_matches(fd, path, current):
            return None
        return self._tag_opened_identity(fd)
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _rename_skill_child_no_replace(
    self,
    source: _PinnedSkillParent,
    source_name: str,
    destination: _PinnedSkillParent,
    destination_name: str,
    *,
    expected_identity: _TaggedFileIdentity | None = None,
    compensate_mismatch_to_source: bool = True,
) -> os.stat_result:
    """Move one exact child between captured parents without replacing."""
    if not self._pinned_parent_matches(source) or not self._pinned_parent_matches(destination):
        raise OSError("skill-state parent changed before rename")
    before_identity = self._pinned_child_identity(source, source_name)
    if before_identity is None or (
        expected_identity is not None and before_identity != expected_identity
    ):
        raise OSError("skill-state child identity changed before rename")
    if platform_compat.IS_WINDOWS:
        # MoveFileW refuses an existing destination. The source and final
        # names are authenticated with native 128-bit IDs; CRT inode fields
        # are never mutation authority.
        os.rename(source.path / source_name, destination.path / destination_name)
    else:
        platform_compat.rename_noreplace(
            source_name,
            destination_name,
            src_dir_fd=source.fd,
            dst_dir_fd=destination.fd,
        )
    if not self._pinned_parent_matches(source) or not self._pinned_parent_matches(destination):
        raise OSError("skill-state parent changed during rename")
    after = self._stat_pinned_child(destination, destination_name)
    after_identity = self._pinned_child_identity(destination, destination_name)
    if after_identity is None or after_identity != before_identity:
        if not compensate_mismatch_to_source:
            raise OSError(
                "skill-state child changed after rename; public destination left untouched"
            )
        restored_name = source_name
        if after_identity is None or self._pinned_child_exists(source, restored_name):
            restored_name = f".mismatch-{source_name[:32]}-{secrets.token_hex(8)}"
        try:
            if platform_compat.IS_WINDOWS:
                os.rename(
                    destination.path / destination_name,
                    source.path / restored_name,
                )
            else:
                platform_compat.rename_noreplace(
                    destination_name,
                    restored_name,
                    src_dir_fd=destination.fd,
                    dst_dir_fd=source.fd,
                )
            self._sync_pinned_rename_parents(destination, source)
            if (
                after_identity is not None
                and self._pinned_child_identity(source, restored_name) != after_identity
            ):
                raise OSError("mismatched child changed during compensation")
        except OSError:
            logger.error(
                "Could not evacuate mismatched rename destination %s",
                destination.path / destination_name,
                exc_info=True,
            )
        raise OSError("skill-state child changed during rename")
    return after


def _rename_untrusted_link_no_replace(
    self,
    source: _PinnedSkillParent,
    source_name: str,
    destination: _PinnedSkillParent,
    destination_name: str,
    *,
    expected_identity: _TaggedFileIdentity,
) -> None:
    """Move one exact public link that can never become publication authority."""
    before = self._stat_pinned_child(source, source_name)
    if not (
        stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_reparse_tag", 0))
        or is_link_or_junction(source.path / source_name)
    ):
        raise OSError("public entry is not an untrusted link")
    self._rename_skill_child_no_replace(
        source,
        source_name,
        destination,
        destination_name,
        expected_identity=expected_identity,
    )
    after = self._stat_pinned_child(destination, destination_name)
    if not (
        stat.S_ISLNK(after.st_mode)
        or bool(getattr(after, "st_reparse_tag", 0))
        or is_link_or_junction(destination.path / destination_name)
    ):
        raise OSError("public link changed during rename")


def _unlink_skill_child(
    self,
    parent: _PinnedSkillParent,
    name: str,
    *,
    expected: os.stat_result | None = None,
    expected_identity: _TaggedFileIdentity | None = None,
    directory: bool = False,
) -> bool:
    """Remove only the captured child under one revalidated parent."""
    if not self._pinned_parent_matches(parent):
        return False
    try:
        current = self._stat_pinned_child(parent, name)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    current_identity = (
        self._pinned_child_identity(parent, name) if platform_compat.IS_WINDOWS else None
    )
    if platform_compat.IS_WINDOWS:
        if expected_identity is None or current_identity != expected_identity:
            return False
    elif expected is not None and not os.path.samestat(current, expected):
        return False
    try:
        if platform_compat.IS_WINDOWS:
            if (
                expected_identity is None
                or expected_identity.kind != "windows-file-id-128"
                or not isinstance(expected_identity.object_id, bytes)
            ):
                return False
            removed = platform_compat.unlink_path_if_identity(
                parent.path / name,
                (
                    expected_identity.volume,
                    b"F128" + expected_identity.object_id,
                ),
                directory=directory,
            )
            return removed and self._pinned_parent_matches(parent)
        if directory:
            os.rmdir(name, dir_fd=parent.fd)
        else:
            os.unlink(name, dir_fd=parent.fd)
    except OSError:
        return False
    return self._pinned_parent_matches(parent)


def _remove_private_tree(
    self,
    path: Path,
    *,
    what: str,
    expected_identity: _TaggedFileIdentity | None = None,
) -> bool:
    """Remove one opened private tree without traversing a mutable name.

    POSIX delegates the recursive walk to ``pinned_fs`` and requires that
    its independently opened root is the same inode captured here. Windows
    keeps a no-reparse, non-delete-sharing handle on the root while deleting
    children bottom-up; it closes that handle only for the final empty
    ``rmdir``, with the parent handle still pinning every ancestor.
    """
    if not os.path.lexists(path):
        return True
    try:
        with self._pin_skill_parent(path.parent) as parent:
            root_fd = platform_compat.pin_directory(path)
            try:
                root_identity = self._tag_opened_identity(root_fd)
                if root_identity is None or (
                    expected_identity is not None and root_identity != expected_identity
                ):
                    return False
                if platform_compat.IS_WINDOWS:
                    if not platform_compat.opened_path_identity_matches(root_fd, path):
                        return False
                    # Validate the complete tree before the first mutation.
                    if self._skill_tree_snapshot(path) is None:
                        return False
                    for current_root, dirs, files in os.walk(
                        path, topdown=False, followlinks=False
                    ):
                        current_path = Path(current_root)
                        with self._pin_skill_parent(current_path) as current_parent:
                            for filename in files:
                                child = self._stat_pinned_child(current_parent, filename)
                                child_identity = self._pinned_child_identity(
                                    current_parent,
                                    filename,
                                )
                                if (
                                    not stat.S_ISREG(child.st_mode)
                                    or child.st_nlink != 1
                                    or child_identity is None
                                    or not self._unlink_skill_child(
                                        current_parent,
                                        filename,
                                        expected=child,
                                        expected_identity=child_identity,
                                    )
                                ):
                                    return False
                            for dirname in dirs:
                                child_path = current_path / dirname
                                if is_link_or_junction(child_path):
                                    return False
                                child = self._stat_pinned_child(current_parent, dirname)
                                child_identity = self._pinned_child_identity(
                                    current_parent,
                                    dirname,
                                )
                                if (
                                    not stat.S_ISDIR(child.st_mode)
                                    or child_identity is None
                                    or not self._unlink_skill_child(
                                        current_parent,
                                        dirname,
                                        expected=child,
                                        expected_identity=child_identity,
                                        directory=True,
                                    )
                                ):
                                    return False
                else:
                    resolved = str(path.resolve(strict=True))

                    def approve_root(fd: int, tree: pinned_fs.PinnedTree) -> str | None:
                        opened_identity = self._tag_opened_identity(fd)
                        if opened_identity != root_identity:
                            return "private tree changed identity"
                        if tree.links:
                            return "private tree contains links"
                        return None

                    removed = pinned_fs.remove_tree_pinned(
                        resolved,
                        what=what,
                        approve=approve_root,
                        refusal=OSError,
                    )
                    return removed.removed
            finally:
                os.close(root_fd)
            # The target is empty. The held parent still prevents an
            # ancestor swap; refuse a replacement/reparse point and remove
            # only the captured empty directory.
            if not self._pinned_parent_matches(parent):
                return False
            root_now = self._stat_pinned_child(parent, path.name)
            current_identity = self._pinned_child_identity(parent, path.name)
            if (
                not stat.S_ISDIR(root_now.st_mode)
                or current_identity != root_identity
                or is_link_or_junction(path)
            ):
                return False
            return self._unlink_skill_child(
                parent,
                path.name,
                expected=root_now,
                expected_identity=root_identity,
                directory=True,
            )
    except (OSError, ValueError):
        logger.warning("Could not safely remove %s %s", what, path)
        return False


def _open_skill_lock(
    self,
    parent: _PinnedSkillParent,
    name: str,
    *,
    created_out: list[bool] | None = None,
) -> int:
    """Open one lone regular lock relative to its retained parent pin."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe skill lock name")
    if not self._pinned_parent_matches(parent):
        raise OSError("skill lock parent changed before open")
    path = parent.path / name
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | nofollow
    fd: int | None = None
    pre: os.stat_result | None = None
    created = False
    try:
        try:
            pre = cast(os.stat_result, self._stat_pinned_child(parent, name))
        except FileNotFoundError:
            if _DIR_FD_SUPPORTED:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent.fd)
            else:
                fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        else:
            if is_link_or_junction(path) or not stat.S_ISREG(pre.st_mode) or pre.st_nlink != 1:
                raise OSError(f"refusing unsafe skill lock {path}")
            if _DIR_FD_SUPPORTED:
                fd = os.open(name, flags, dir_fd=parent.fd)
            else:
                fd = os.open(str(path), flags)
        opened = os.fstat(fd)
        if (
            not self._pinned_parent_matches(parent)
            or (pre is not None and not self._opened_path_matches(fd, path, pre))
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise OSError(f"refusing unsafe skill lock {path}")
        if created_out is not None:
            created_out[:] = [created]
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        raise


@contextlib.contextmanager
def _create_pinned_child_exclusive(
    self,
    parent: _PinnedSkillParent,
    name: str,
) -> Iterator[_PinnedSkillParent]:
    """Create and pin one new child directory beneath *parent*.

    POSIX creation/open is descriptor-relative. Windows keeps the parent
    no-reparse/non-delete-sharing handle live across the unavoidable by-name
    mkdir and then authenticates the child through its own retained handle.
    A setup failure removes only the empty directory this call created.
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe staged skill directory name")
    if not self._pinned_parent_matches(parent):
        raise OSError("pending parent changed before staged directory create")
    try:
        self._stat_pinned_child(parent, name)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "staged skill directory exists", name)
    if platform_compat.IS_POSIX:
        if not _DIR_FD_SUPPORTED:
            raise OSError(errno.ENOTSUP, "descriptor-relative staging is unavailable")
        os.mkdir(name, 0o777, dir_fd=parent.fd)
    else:
        os.mkdir(parent.path / name, 0o777)
    created = self._stat_pinned_child(parent, name)
    created_identity: _TaggedFileIdentity | None = None
    try:
        with self._pin_skill_child_parent(parent, name, create=False) as child:
            created_identity = child.native_identity
            if (
                not platform_compat.IS_WINDOWS
                and (created.st_dev, created.st_ino) != child.identity
            ):
                raise OSError("staged skill directory changed during create")
            yield child
    except BaseException:
        self._unlink_skill_child(
            parent,
            name,
            expected=created,
            expected_identity=created_identity,
            directory=True,
        )
        raise


def _write_pinned_new_file(
    self,
    parent: _PinnedSkillParent,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o666,
) -> os.stat_result:
    """Create, flush, and authenticate one new file beneath *parent*."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe staged skill filename")
    if not self._pinned_parent_matches(parent):
        raise OSError("staged skill parent changed before file create")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    path = parent.path / name
    if platform_compat.IS_POSIX and _DIR_FD_SUPPORTED:
        fd = os.open(name, flags, mode, dir_fd=parent.fd)
    else:
        fd = os.open(path, flags, mode)
    owned: os.stat_result | None = None
    owned_identity: _TaggedFileIdentity | None = None
    try:
        opened = os.fstat(fd)
        opened_identity = self._tag_opened_identity(fd)
        current = self._stat_pinned_child(parent, name)
        if (
            opened_identity is None
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not self._opened_path_matches(fd, path, current)
        ):
            raise OSError("staged skill file changed during create")
        owned = current
        owned_identity = opened_identity
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short staged skill file write")
            remaining = remaining[written:]
        os.fsync(fd)
        after = os.fstat(fd)
        current = self._stat_pinned_child(parent, name)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_size != len(payload)
            or self._tag_opened_identity(fd) != opened_identity
            or not self._opened_path_matches(fd, path, current)
            or not self._pinned_parent_matches(parent)
        ):
            raise OSError("staged skill file changed during write")
        owned = current
        owned_identity = opened_identity
        return current
    except BaseException:
        if owned is not None:
            self._unlink_skill_child(
                parent,
                name,
                expected=owned,
                expected_identity=owned_identity,
            )
        raise
    finally:
        os.close(fd)


def _pinned_child_exists(self, parent: _PinnedSkillParent, name: str) -> bool:
    try:
        self._stat_pinned_child(parent, name)
        return True
    except FileNotFoundError:
        return False

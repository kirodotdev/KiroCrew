"""Access-control-xattr carry in ``atomic_write``.

``atomic_write`` installs a FRESH inode (mkstemp + rename), so a named POSIX ACL
(``system.posix_acl_access``, ``system.posix_acl_default``) the owner set on the
file
being replaced is dropped unless it is explicitly reproduced. The
``preserve_access_control_from`` parameter carries it across, refusing the write
when an access-control attribute cannot be reproduced -- otherwise an edit would
hand back a file protected more narrowly than the one it replaced.

These tests monkeypatch ``os.listxattr``/``getxattr``/``setxattr`` exactly as
test/test_artifacts_source_root.py does for the ``safe_write_file_nolink`` twin, so they run
offline without any real xattr support. Each asserts a distinct branch of the
policy and fails if the ACL-carry code is reverted, so none can pass by accident.
The macOS extended ACL is not an xattr; its tests set a real entry with
``chmod +a`` and run only on macOS, except the refusal, which fakes the copy.
The macOS temp-cleanup tests guard what a refused save leaves behind once the
carry has run, so they do not fail on a reverted carry.

Run offline via:

    PYTHONPATH=src python -m pytest test/test_atomic_write_acl_preservation.py \
        --noconftest -o addopts=""
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, open_access_control_source
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_MACOS_ONLY = pytest.mark.skipif(
    not platform_compat.IS_MACOS, reason="the extended ACL is the macOS store"
)


def _needs_xattr() -> None:
    if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("platform without xattr syscalls")  # pragma: no cover


def _open_source(path) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def test_refuses_when_an_access_control_attribute_cannot_be_carried(tmp_path, monkeypatch):
    """A lost POSIX ACL is a security regression, so the write is refused.

    The rename would install an inode the owner protected less than the one it
    replaced, so the original must be left untouched instead.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
        mp.setattr(os, "getxattr", lambda *a, **k: b"acl-bytes", raising=False)

        def refuse_setxattr(*a, **k):
            raise OSError(errno.EPERM, "Operation not permitted")

        mp.setattr(os, "setxattr", refuse_setxattr, raising=False)

        src_fd = _open_source(target)
        try:
            with pytest.raises(OSError):
                atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    # Original untouched, and no orphaned temp file left behind.
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.tmp")) == []


def test_proceeds_when_only_an_informational_attribute_fails(tmp_path, monkeypatch):
    """A ``user.*`` attribute is metadata, not protection.

    Failing the save over it would break every edit on a filesystem that cannot
    store xattrs, so the write proceeds and the informational attribute is
    dropped best-effort.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", lambda *a, **k: ["user.note"], raising=False)
        mp.setattr(os, "getxattr", lambda *a, **k: b"x", raising=False)

        def refuse_setxattr(*a, **k):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        mp.setattr(os, "setxattr", refuse_setxattr, raising=False)

        src_fd = _open_source(target)
        try:
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert target.read_text(encoding="utf-8") == "new body"


def test_refuses_when_the_attribute_list_cannot_be_read(tmp_path, monkeypatch):
    """A failed ``listxattr`` is not the same as "there are none".

    Treating a lookup failure as an empty list would install a replacement
    stripped of the owner's ACL. Only "this filesystem has no xattrs" is safe to
    read as nothing-to-carry; ``EACCES`` is a refusal.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    def failing_list(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", failing_list, raising=False)

        src_fd = _open_source(target)
        try:
            with pytest.raises(OSError):
                atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    # The refusal happens BEFORE staging, so there is nothing to clean up.
    assert list(tmp_path.glob("*.tmp")) == []


def test_proceeds_when_the_filesystem_has_no_xattrs(tmp_path, monkeypatch):
    """``ENOTSUP`` means there is nothing on the source to lose.

    The write goes ahead -- failing here would break edits on tmpfs and several
    network mounts.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    def unsupported(*a, **k):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", unsupported, raising=False)

        src_fd = _open_source(target)
        try:
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert target.read_text(encoding="utf-8") == "new body"


def test_happy_path_reproduces_the_source_attribute_on_the_replacement(tmp_path, monkeypatch):
    """A fake xattr read from the source fd is reproduced on the destination.

    Asserts the captured value is passed through to ``setxattr`` unchanged, so a
    revert of the carry loop fails this test.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
        mp.setattr(os, "getxattr", lambda *a, **k: b"captured-acl", raising=False)

        recorded: list[tuple[str, bytes]] = []

        def recording_setxattr(fd, attr, value, *a, **k):
            recorded.append((attr, value))

        mp.setattr(os, "setxattr", recording_setxattr, raising=False)

        src_fd = _open_source(target)
        try:
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert target.read_text(encoding="utf-8") == "new body"
    assert ("system.posix_acl_access", b"captured-acl") in recorded


def test_no_source_descriptor_is_opened_where_xattrs_do_not_exist(tmp_path, monkeypatch):
    """On a platform without the xattr syscalls, no source handle is opened.

    This is the Windows contract, and it is load-bearing rather than cosmetic:
    ``os.replace`` there fails with ``PermissionError`` while ANY other handle is
    open on either path, so a descriptor held for a carry that cannot happen
    would fail every dashboard and steering save. Returning ``None`` keeps the
    write on its plain path.
    """
    import kiro_crew.atomic_write as aw

    target = tmp_path / "file.txt"
    target.write_text("before", encoding="utf-8")

    with monkeypatch.context() as mp:
        mp.setattr(aw, "ACCESS_CONTROL_XATTRS_SUPPORTED", False)

        opened: list[object] = []
        real_open = aw.os.open

        def recording_open(*args, **kwargs):
            opened.append(args[:1])
            return real_open(*args, **kwargs)

        mp.setattr(aw.os, "open", recording_open)
        assert aw.open_access_control_source(target) is None
    assert opened == [], "no descriptor may be opened where xattrs do not exist"


def test_pinned_caller_gets_a_descriptor_even_without_xattrs(tmp_path, monkeypatch):
    """With ``dir_fd`` the helper returns a descriptor REGARDLESS of the flag.

    This is the macOS composition (``openat`` and no ``listxattr``), simulated
    deterministically: the flag is forced off while the platform can still pin.
    The pinned branch must win — the MODE carry reads the bits off this
    descriptor so they come from the inode inside the pinned directory, and a
    ``None`` here would send the caller back to a by-name ``stat``, exactly the
    re-resolution the pin exists to prevent. The Windows open-handle caveat
    does not apply: pinning needs ``O_DIRECTORY``, which Windows lacks, so a
    pinned caller is on POSIX where ``renameat`` publishes past open handles.
    The handler tests must not conflate "no xattr
    syscalls" with "no descriptor" and report this contract as a failure.
    """
    if not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd:
        pytest.skip("platform cannot pin a parent directory")

    import kiro_crew.atomic_write as aw

    target = tmp_path / "file.txt"
    target.write_text("before", encoding="utf-8")

    monkeypatch.setattr(aw, "ACCESS_CONTROL_XATTRS_SUPPORTED", False)
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = aw.open_access_control_source(target, dir_fd=dir_fd)
        assert isinstance(fd, int), "pinned branch must not be gated on the xattr flag"
        try:
            # The descriptor references the leaf inside the pinned directory.
            assert os.read(fd, 16) == b"before"
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def test_source_descriptor_is_opened_where_xattrs_exist(tmp_path):
    """Where the syscalls exist, the helper hands back a usable descriptor."""
    _needs_xattr()
    target = tmp_path / "file.txt"
    target.write_text("before", encoding="utf-8")

    from kiro_crew.atomic_write import open_access_control_source

    fd = open_access_control_source(target)
    assert isinstance(fd, int)
    try:
        assert os.fstat(fd).st_size == len(b"before")
    finally:
        os.close(fd)


def test_source_open_refuses_a_leaf_symlink(tmp_path):
    """The helper's ``O_NOFOLLOW`` refuses a final component that is a link."""
    _needs_xattr()
    if not hasattr(os, "O_NOFOLLOW"):  # pragma: no cover - POSIX-only assertion
        pytest.skip("O_NOFOLLOW is required to refuse a link")
    real = tmp_path / "real.txt"
    real.write_text("protected", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    from kiro_crew.atomic_write import open_access_control_source

    with pytest.raises(OSError) as caught:
        open_access_control_source(link)
    assert caught.value.errno in (errno.ELOOP, errno.EMLINK)


def test_a_privilege_bearing_attribute_is_never_carried(tmp_path, monkeypatch):
    """``security.capability`` and the integrity attrs must NOT reach the copy.

    The replacement holds content the CALLER supplied, so replaying file
    capabilities onto it hands attacker-chosen bytes whatever the old file was
    trusted with, and replaying ``security.ima``/``security.evm`` forges an
    integrity appraisal over bytes that were never measured. The ACL beside them
    still has to be carried, which is what makes this a filter rather than a
    blanket "stop carrying xattrs".

    Also pins that the drop is SILENT, not a refusal: the write completes.
    """
    _needs_xattr()
    target = tmp_path / "helper.bin"
    target.write_text("ORIGINAL", encoding="utf-8")

    present = [
        "security.capability",
        "security.ima",
        "security.evm",
        "security.selinux",
        "system.posix_acl_access",
        "user.note",
    ]
    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", lambda *a, **k: list(present), raising=False)
        mp.setattr(os, "getxattr", lambda fd, attr, *a, **k: attr.encode(), raising=False)

        read: list[str] = []
        real_getxattr = os.getxattr

        def recording_getxattr(fd, attr, *a, **k):
            read.append(attr)
            return real_getxattr(fd, attr, *a, **k)

        mp.setattr(os, "getxattr", recording_getxattr, raising=False)

        written: list[str] = []
        mp.setattr(
            os,
            "setxattr",
            lambda fd, attr, value, *a, **k: written.append(attr),
            raising=False,
        )

        src_fd = _open_source(target)
        try:
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert target.read_text(encoding="utf-8") == "new body"
    assert written == [
        "system.posix_acl_access",
        "user.note",
    ], "only the allowlisted access-control and informational attributes may be carried"
    # Not merely unwritten -- never even READ, so no later edit can replay a
    # value the collection step was not supposed to hold.
    assert read == ["system.posix_acl_access", "user.note"]


def test_a_failed_privileged_carry_cannot_refuse_the_write(tmp_path, monkeypatch):
    """A ``security.*`` attribute is outside the fail-closed set entirely.

    Before the allowlist, every ``security.*`` name counted as access control, so
    a ``setxattr`` denied for ``security.capability`` (or for ``security.selinux``
    on an enforcing host, where the writing domain usually lacks ``relabelto``)
    REFUSED the save. Now such an attribute is never collected, so no ``setxattr``
    happens for it and there is nothing to fail on: the save proceeds.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    with monkeypatch.context() as mp:
        mp.setattr(
            os,
            "listxattr",
            lambda *a, **k: ["security.capability", "security.selinux"],
            raising=False,
        )
        mp.setattr(os, "getxattr", lambda *a, **k: b"privileged", raising=False)

        def refuse_setxattr(*a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("a privileged attribute must never be offered to setxattr")

        mp.setattr(os, "setxattr", refuse_setxattr, raising=False)

        src_fd = _open_source(target)
        try:
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert target.read_text(encoding="utf-8") == "new body"


def test_the_default_acl_is_carried_too(tmp_path, monkeypatch):
    """Both POSIX ACL names are allowlisted, not just the access one.

    ``system.posix_acl_default`` is the ACL children inherit; dropping it would
    silently widen everything created under the path afterwards.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_default"], raising=False)
        mp.setattr(os, "getxattr", lambda *a, **k: b"default-acl", raising=False)

        written: list[tuple[str, bytes]] = []
        mp.setattr(
            os,
            "setxattr",
            lambda fd, attr, value, *a, **k: written.append((attr, value)),
            raising=False,
        )

        src_fd = _open_source(target)
        try:
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
        finally:
            os.close(src_fd)

    assert written == [("system.posix_acl_default", b"default-acl")]


def test_no_source_fd_leaves_the_write_untouched(tmp_path, monkeypatch):
    """Without the parameter, no xattr syscall is consulted at all.

    Guards against the carry running unconditionally and imposing the refusal on
    every caller that never opted in.
    """
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    def boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("xattr syscall consulted without preserve_access_control_from")

    with monkeypatch.context() as mp:
        mp.setattr(os, "listxattr", boom, raising=False)
        mp.setattr(os, "setxattr", boom, raising=False)

        atomic_write(target, "new body")
    assert target.read_text(encoding="utf-8") == "new body"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows DACL exists only on Windows")
def test_a_narrowed_windows_dacl_survives_the_replace(tmp_path):
    """A file narrowed below its folder keeps that narrowing after an overwrite."""
    from kiro_crew import windows_acl

    # The real lockdown under restrict_to_owner, which conftest stubs on Windows.
    from kiro_crew.platform_owner_compat import (
        _OWNER_RIGHTS_SID,
        _apply_owner_only_dacl,
        current_user_sid,
    )

    target = tmp_path / "narrow.txt"
    target.write_text("old", encoding="utf-8")
    _apply_owner_only_dacl(target, inherit=False)
    user_sid = current_user_sid()
    assert user_sid is not None
    sids = (_OWNER_RIGHTS_SID,) if user_sid == _OWNER_RIGHTS_SID else (_OWNER_RIGHTS_SID, user_sid)
    assert windows_acl.owner_only_dacl_matches(target, inherit=False, sids=sids)

    atomic_write(target, "new", preserve_windows_dacl=True)

    assert target.read_text(encoding="utf-8") == "new"
    assert windows_acl.owner_only_dacl_matches(target, inherit=False, sids=sids)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows DACL exists only on Windows")
def test_a_file_moved_from_a_narrower_folder_keeps_its_inherited_dacl(tmp_path):
    """A move keeps the old folder's inherited entries; a save must not swap in the new folder's."""
    from kiro_crew import windows_acl
    from kiro_crew.platform_owner_compat import _apply_owner_only_dacl

    narrow, wide = tmp_path / "narrow", tmp_path / "wide"
    narrow.mkdir()
    wide.mkdir()
    _apply_owner_only_dacl(narrow, inherit=True)
    (narrow / "doc.txt").write_text("old", encoding="utf-8")
    target = wide / "doc.txt"
    os.rename(narrow / "doc.txt", target)
    before = windows_acl.read_carried_dacl(target)
    assert before is not None
    (wide / "fresh.txt").write_text("x", encoding="utf-8")
    assert before != windows_acl.read_carried_dacl(wide / "fresh.txt")

    atomic_write(target, "new", preserve_windows_dacl=True)

    assert target.read_text(encoding="utf-8") == "new"
    assert windows_acl.read_carried_dacl(target) == before


def _run(*argv: str) -> str:
    return subprocess.run(
        list(argv),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    ).stdout


#: ``nobody``'s fixed UUID. ``ls -len`` prints a qualifier as its UUID, so a check
#: does not depend on a name lookup, which a busy host can fail.
_NOBODY = "FFFFEEEE-DDDD-CCCC-BBBB-AAAAFFFFFFFE"


def _acl_entries(path: Path) -> list[str]:
    return [line.strip() for line in _run("/bin/ls", "-len", str(path)).splitlines()[1:]]


def _pinned_write(target: Path, content: str, *, mode: int | None = None) -> None:
    dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    src_fd = None
    try:
        src_fd = open_access_control_source(target, dir_fd=dir_fd)
        atomic_write(
            target,
            content,
            mode=mode,
            preserve_access_control_from=src_fd,
            parent_dir_fd=dir_fd,
        )
    finally:
        for fd in (src_fd, dir_fd):
            if fd is not None:
                os.close(fd)


@_MACOS_ONLY
def test_macos_extended_acl_survives_a_pinned_replace(tmp_path):
    """The replacement keeps the file's own ACL entry, not only the folder's.

    ``user:nobody deny read`` narrows the file below its folder, so losing it
    would hand the saved file back readable by that account.
    """
    target = Path(os.path.realpath(tmp_path)) / "narrow.txt"
    target.write_text("old", encoding="utf-8")
    _run("/bin/chmod", "+a", "user:nobody deny read", str(target))
    assert _acl_entries(target) == [f"0: {_NOBODY} deny read"]

    _pinned_write(target, "new")

    assert _acl_entries(target) == [f"0: {_NOBODY} deny read"]
    assert target.read_text(encoding="utf-8") == "new"


@_MACOS_ONLY
def test_a_refused_macos_save_removes_a_temp_that_carries_a_deny_delete_entry(tmp_path):
    """A deny-delete entry refuses the rename, and the carried copy refuses a plain unlink.

    The save fails as it always has; what must not change is that the temp is
    gone afterwards, rather than left in the user's folder with an entry that
    stops them deleting it.
    """
    folder = Path(os.path.realpath(tmp_path))
    target = folder / "locked.txt"
    target.write_text("old", encoding="utf-8")
    _run("/bin/chmod", "+a", "everyone deny delete", str(target))
    try:
        with pytest.raises(PermissionError):
            _pinned_write(target, "new")
        assert os.listdir(folder) == ["locked.txt"]
        assert target.read_text(encoding="utf-8") == "old"
    finally:
        # Lets tmp_path's cleanup remove whatever is left.
        _run("/bin/chmod", "-N", *(str(p) for p in folder.iterdir()))


@_MACOS_ONLY
def test_a_temp_is_owner_only_before_its_acl_is_emptied_for_removal(tmp_path, monkeypatch):
    """The temp holds the new content, so emptying its ACL must not open it to a denied account.

    On a 0644 file, ``user:nobody deny read`` is the only thing keeping that
    account out, so the temp loses its group and other bits before the ACL goes.
    """
    folder = Path(os.path.realpath(tmp_path))
    target = folder / "locked.txt"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    _run("/bin/chmod", "+a", "user:nobody deny read", str(target))
    _run("/bin/chmod", "+a", "everyone deny delete", str(target))
    seen: list[tuple[int, list[str]]] = []
    real_unlink = os.unlink

    def unlink(name, *, dir_fd=None):
        if os.path.basename(name) != target.name:
            leaf = folder / os.path.basename(name)
            seen.append((stat.S_IMODE(leaf.lstat().st_mode), _acl_entries(leaf)))
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    try:
        with pytest.raises(PermissionError):
            _pinned_write(target, "new", mode=0o644)
        assert os.listdir(folder) == ["locked.txt"]
    finally:
        # Lets tmp_path's cleanup remove whatever is left.
        _run("/bin/chmod", "-N", *(str(p) for p in folder.iterdir()))
    assert [mode for mode, entries in seen if not entries] == [0o600]


@_MACOS_ONLY
def test_a_temp_whose_removal_stays_refused_keeps_the_acl_it_carried(tmp_path):
    """Emptying the temp's ACL to remove it is undone when the folder refuses the removal.

    The temp holds the new content, so one left behind must stay as restricted
    as the file it was meant to replace.
    """
    folder = Path(os.path.realpath(tmp_path)) / "shut"
    folder.mkdir()
    _run("/bin/chmod", "+a", "user:nobody deny read,file_inherit", str(folder))
    target = folder / "narrow.txt"
    target.write_text("old", encoding="utf-8")
    _run("/bin/chmod", "+a", "everyone deny delete_child", str(folder))
    try:
        with pytest.raises(PermissionError):
            _pinned_write(target, "new")
        (leftover,) = [p for p in folder.iterdir() if p.name != target.name]
        assert _acl_entries(leftover) == [f"0: {_NOBODY} inherited deny read"]
        assert target.read_text(encoding="utf-8") == "old"
    finally:
        # Lets tmp_path's cleanup remove whatever is left.
        _run("/bin/chmod", "-N", str(folder))
        _run("/bin/chmod", "-N", *(str(p) for p in folder.iterdir()))


def test_a_macos_acl_that_cannot_be_carried_refuses_the_write(tmp_path, monkeypatch):
    """Like a lost POSIX ACL: the write raises, the original stays, no temp is left."""
    import kiro_crew.atomic_write as aw

    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")
    sources: list[int] = []

    def refuse(src_fd: int, dst_fd: int) -> None:
        sources.append(src_fd)
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(aw.macos_acl, "copy_extended_acl", refuse)
    src_fd = _open_source(target)
    try:
        with pytest.raises(PermissionError):
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    assert sources == [src_fd]
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.tmp")) == []

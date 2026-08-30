"""Access-control-xattr carry in ``atomic_write``.

``atomic_write`` installs a FRESH inode (mkstemp + rename), so a named POSIX ACL
(``system.posix_acl_access``, ``system.posix_acl_default``) the owner set on the
file
being replaced is dropped unless it is explicitly reproduced. The
``preserve_access_control_from`` parameter carries it across, refusing the write
when an access-control attribute cannot be reproduced -- otherwise an edit would
hand back a file protected more narrowly than the one it replaced.

These tests monkeypatch ``os.listxattr``/``getxattr``/``setxattr`` exactly as
test/test_artifacts.py does for the ``safe_write_file_nolink`` twin, so they run
offline without any real xattr support. Each asserts a distinct branch of the
policy and fails if the ACL-carry code is reverted, so none can pass by accident.

Run offline via:

    PYTHONPATH=src python -m pytest test/test_atomic_write_acl_preservation.py \
        --noconftest -o addopts=""
"""

from __future__ import annotations

import errno
import os

import pytest

from kiro_crew.atomic_write import atomic_write


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

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"acl-bytes", raising=False)

    def refuse_setxattr(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "setxattr", refuse_setxattr, raising=False)

    src_fd = _open_source(target)
    try:
        with pytest.raises(OSError):
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
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

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["user.note"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"x", raising=False)

    def refuse_setxattr(*a, **k):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(os, "setxattr", refuse_setxattr, raising=False)

    src_fd = _open_source(target)
    try:
        atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
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

    monkeypatch.setattr(os, "listxattr", failing_list, raising=False)

    src_fd = _open_source(target)
    try:
        with pytest.raises(OSError):
            atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
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

    monkeypatch.setattr(os, "listxattr", unsupported, raising=False)

    src_fd = _open_source(target)
    try:
        atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "new body"


def test_happy_path_reproduces_the_source_attribute_on_the_replacement(tmp_path, monkeypatch):
    """A fake xattr read from the source fd is reproduced on the destination.

    Asserts the captured value is passed through to ``setxattr`` unchanged, so a
    revert of the carry loop fails this test.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"captured-acl", raising=False)

    recorded: list[tuple[str, bytes]] = []

    def recording_setxattr(fd, attr, value, *a, **k):
        recorded.append((attr, value))

    monkeypatch.setattr(os, "setxattr", recording_setxattr, raising=False)

    src_fd = _open_source(target)
    try:
        atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
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

    monkeypatch.setattr(aw, "ACCESS_CONTROL_XATTRS_SUPPORTED", False)

    opened: list[object] = []
    real_open = aw.os.open

    def recording_open(*args, **kwargs):
        opened.append(args[:1])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(aw.os, "open", recording_open)
    assert aw.open_access_control_source(target) is None
    monkeypatch.undo()
    assert opened == [], "no descriptor may be opened where xattrs do not exist"


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
    monkeypatch.setattr(os, "listxattr", lambda *a, **k: list(present), raising=False)
    monkeypatch.setattr(os, "getxattr", lambda fd, attr, *a, **k: attr.encode(), raising=False)

    read: list[str] = []
    real_getxattr = os.getxattr

    def recording_getxattr(fd, attr, *a, **k):
        read.append(attr)
        return real_getxattr(fd, attr, *a, **k)

    monkeypatch.setattr(os, "getxattr", recording_getxattr, raising=False)

    written: list[str] = []
    monkeypatch.setattr(
        os, "setxattr", lambda fd, attr, value, *a, **k: written.append(attr), raising=False
    )

    src_fd = _open_source(target)
    try:
        atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
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

    monkeypatch.setattr(
        os, "listxattr", lambda *a, **k: ["security.capability", "security.selinux"], raising=False
    )
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"privileged", raising=False)

    def refuse_setxattr(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("a privileged attribute must never be offered to setxattr")

    monkeypatch.setattr(os, "setxattr", refuse_setxattr, raising=False)

    src_fd = _open_source(target)
    try:
        atomic_write(target, "new body", preserve_access_control_from=src_fd)
    finally:
        os.close(src_fd)

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "new body"


def test_the_default_acl_is_carried_too(tmp_path, monkeypatch):
    """Both POSIX ACL names are allowlisted, not just the access one.

    ``system.posix_acl_default`` is the ACL children inherit; dropping it would
    silently widen everything created under the path afterwards.
    """
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    monkeypatch.setattr(
        os, "listxattr", lambda *a, **k: ["system.posix_acl_default"], raising=False
    )
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"default-acl", raising=False)

    written: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
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

    monkeypatch.undo()
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

    monkeypatch.setattr(os, "listxattr", boom, raising=False)
    monkeypatch.setattr(os, "setxattr", boom, raising=False)

    atomic_write(target, "new body")
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "new body"

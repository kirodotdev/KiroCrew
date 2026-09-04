"""Pinned-parent atomic-write mode in ``atomic_write``.

``atomic_write(parent_dir_fd=fd)`` stages its temp file and runs the publishing
rename relative to a directory descriptor the caller has already pinned
component-by-component, rather than through ``tempfile.mkstemp(dir=...)`` and a
by-name ``os.replace`` -- both of which re-resolve the parent and so reopen the
ancestor-swap window the pinned parent exists to close. These tests exercise that
mode against a real descriptor from ``pinned_fs.open_dir_pinned``.

They run offline: the parameter, the temp create, the ``renameat`` publish, the
byte-exact write, and the failure cleanup are all pure filesystem behaviour on
``kiro_crew.atomic_write`` + ``kiro_crew.pinned_fs``, both leaf modules. The ACL
carry is asserted by monkeypatching ``os.listxattr``/``getxattr``/``setxattr``
exactly as ``test/test_atomic_write_acl_preservation.py`` does. Each test fails if
the pinning is reverted.

Run offline via:

    PYTHONPATH=src python -m pytest test/test_atomic_write_pinned_parent.py \
        --noconftest -o addopts=""
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import pinned_fs
from kiro_crew.atomic_write import atomic_write, pinned_parent_replace_supported


def _needs_pinned_parent() -> None:
    if not pinned_parent_replace_supported():  # pragma: no cover - POSIX CI runs this
        pytest.skip("platform without descriptor-relative rename/open")


def _needs_xattr() -> None:
    if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("platform without xattr syscalls")  # pragma: no cover


def _pin(directory) -> int:
    """Pin *directory*, skipping the test where the platform cannot pin at all.

    The guard lives HERE rather than as a line each test remembers, because
    forgetting it does not degrade -- it CRASHES. ``pinned_fs.pin_parent`` reaches
    for ``os.O_DIRECTORY`` directly, which does not exist on Windows, so a test
    asserting something platform-independent (that ``atomic_write`` refuses a
    parameter combination) still dies on the descriptor it has to construct to
    make the call. Derived from the capability probe rather than
    ``sys.platform``, so the Windows-simulation tests that delete
    ``os.O_NOFOLLOW`` at runtime are covered by the same guard.
    """
    if not pinned_fs.supports_pinned_walk():  # pragma: no cover - POSIX CI runs this
        pytest.skip("platform without a descriptor-relative directory walk")
    return pinned_fs.open_dir_pinned(str(directory), what="test directory")


def test_content_lands_byte_exact_through_a_pinned_parent(tmp_path):
    """A write through a valid parent_dir_fd publishes the exact bytes."""
    _needs_pinned_parent()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    dir_fd = _pin(tmp_path)
    try:
        atomic_write(target, "replacement body", parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    assert target.read_text(encoding="utf-8") == "replacement body"
    # No temp file may survive a successful publish.
    assert list(tmp_path.glob("*.tmp")) == []


def test_acl_carry_still_fires_on_the_pinned_path(tmp_path, monkeypatch):
    """The ACL carry is reproduced on the replacement even through a pinned parent.

    The captured source value must reach ``setxattr`` unchanged, so a revert of
    the carry OR of the pinned staging fails this.
    """
    _needs_pinned_parent()
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"captured-acl", raising=False)
    recorded: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda fd, attr, value, *a, **k: recorded.append((attr, value)),
        raising=False,
    )

    src_fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    dir_fd = _pin(tmp_path)
    try:
        atomic_write(target, "new body", preserve_access_control_from=src_fd, parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
        os.close(src_fd)

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "new body"
    assert ("system.posix_acl_access", b"captured-acl") in recorded


def test_newline_empty_round_trips_with_no_crlf_expansion(tmp_path):
    """newline='' keeps the bytes exact -- no LF-to-CRLF translation on the pinned path."""
    _needs_pinned_parent()
    target = tmp_path / "doc.md"
    target.write_text("seed", encoding="utf-8")

    body = "line one\nline two\n"
    dir_fd = _pin(tmp_path)
    try:
        atomic_write(target, body, newline="", parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    assert target.read_bytes() == body.encode("utf-8")
    assert b"\r\n" not in target.read_bytes()


def test_temp_is_created_under_the_pinned_parent_and_cleaned_up_on_failure(tmp_path, monkeypatch):
    """A staging failure removes the temp relative to the pinned parent -- no orphan.

    The write is forced to fail after the temp exists (a refused ACL carry), and
    the assertion is that no ``.tmp`` residue is left in the pinned directory and
    the original is untouched.
    """
    _needs_pinned_parent()
    _needs_xattr()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"acl", raising=False)

    def refuse_setxattr(*a, **k):
        raise OSError("cannot carry")

    monkeypatch.setattr(os, "setxattr", refuse_setxattr, raising=False)

    src_fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    dir_fd = _pin(tmp_path)
    try:
        with pytest.raises(OSError):
            atomic_write(
                target, "new body", preserve_access_control_from=src_fd, parent_dir_fd=dir_fd
            )
    finally:
        os.close(dir_fd)
        os.close(src_fd)

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    # The temp names begin with a dot; assert nothing was left behind at all.
    leftover = [p.name for p in tmp_path.iterdir() if p.name != "doc.md"]
    assert leftover == [], f"orphaned staging residue: {leftover}"


def test_a_symlinked_parent_directory_is_refused(tmp_path):
    """Pinning a parent that is a symlink refuses rather than following it.

    ``open_dir_pinned`` -- the walk every caller uses to obtain the descriptor it
    hands to ``parent_dir_fd`` -- refuses a directory whose own name is a link,
    with ``O_NOFOLLOW``. That refusal is what stops the write from ever landing
    through a swapped parent, so the pinned mode cannot be reached with a link in
    the chain.
    """
    _needs_pinned_parent()
    if not hasattr(os, "O_NOFOLLOW"):  # pragma: no cover - POSIX-only assertion
        pytest.skip("O_NOFOLLOW is required to refuse a link")
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "doc.md").write_text("protected", encoding="utf-8")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(pinned_fs.PinnedPathRefusal):
        _pin(link_dir)


def test_parent_dir_fd_none_preserves_the_by_name_behaviour(tmp_path):
    """Without a descriptor the write is the unchanged by-name mkstemp + rename."""
    _needs_pinned_parent()
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    atomic_write(target, "by-name body", parent_dir_fd=None)

    assert target.read_text(encoding="utf-8") == "by-name body"
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_pinned_write_stages_through_the_descriptor_not_mkstemp(tmp_path, monkeypatch):
    """With a parent_dir_fd the temp is created via _mkstemp_at, never tempfile.mkstemp.

    This is the revert canary: if the pinned branch is removed and the write falls
    back to the by-name ``tempfile.mkstemp(dir=...)``, ``_mkstemp_at`` is never
    called and ``mkstemp`` is, so this test goes red.
    """
    _needs_pinned_parent()
    import kiro_crew.atomic_write as aw

    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    pinned_calls: list[int] = []
    real_mkstemp_at = aw._mkstemp_at

    def spy_mkstemp_at(dir_fd: int):
        pinned_calls.append(dir_fd)
        return real_mkstemp_at(dir_fd)

    def forbid_mkstemp(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("by-name mkstemp used despite a pinned parent_dir_fd")

    monkeypatch.setattr(aw, "_mkstemp_at", spy_mkstemp_at)
    monkeypatch.setattr(aw.tempfile, "mkstemp", forbid_mkstemp)

    dir_fd = _pin(tmp_path)
    try:
        atomic_write(target, "new body", parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    monkeypatch.undo()
    assert len(pinned_calls) == 1
    assert target.read_text(encoding="utf-8") == "new body"


def test_pinned_publish_replaces_an_existing_regular_file(tmp_path):
    """renameat overwrites the destination, so an existing file is replaced, not doubled."""
    _needs_pinned_parent()
    target = tmp_path / "doc.md"
    target.write_text("v1", encoding="utf-8")

    dir_fd = _pin(tmp_path)
    try:
        atomic_write(target, "v2", parent_dir_fd=dir_fd)
        atomic_write(target, "v3", parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    assert target.read_text(encoding="utf-8") == "v3"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]


def test_restrict_to_owner_with_a_pinned_parent_is_refused(tmp_path):
    """The owner-only lockdown and a pinned parent are mutually exclusive.

    ``platform_compat.restrict_to_owner`` takes a PATH (its Windows half has no
    descriptor form) while a pinned parent's temp name is a bare component
    addressed only through the descriptor. Combining them would chmod that bare
    name against the process CWD and then publish a secret at the umask default,
    so the combination is rejected before any temp file exists.
    """
    target = tmp_path / "secret.json"

    dir_fd = _pin(tmp_path)
    try:
        with pytest.raises(ValueError, match="restrict_to_owner cannot be combined"):
            atomic_write(target, "{}", restrict_to_owner=True, parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    # Refused before staging: nothing was created, so there is nothing to clean up.
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_descriptor_the_platform_cannot_publish_through_is_refused(tmp_path, monkeypatch):
    """A parent_dir_fd is refused, not ignored, where the publish cannot be pinned.

    The alternative -- staging with ``mkstemp`` and renaming by name anyway --
    answers a caller that walked the parent chain component by component with an
    UNPINNED write, and says nothing. So if this probe ever drifts apart from
    ``pinned_fs.supports_pinned_walk()`` (the one callers use to decide whether to
    walk at all), the pin lapses at every site that passes a descriptor and no test
    or log reports it. Callers ask both probes; this is the alarm for the case they
    disagree.
    """
    import kiro_crew.atomic_write as aw

    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    dir_fd = _pin(tmp_path)
    try:
        monkeypatch.setattr(aw, "pinned_parent_replace_supported", lambda: False)
        with pytest.raises(ValueError, match="parent_dir_fd requires"):
            atomic_write(target, "never lands", parent_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]

"""The staging helper's own contract, and the copy primitive's lower-level promises.

``_copytree_safe`` rejects a keyword it does not honour, and the tree-walk
capability probe names the function that really carries ``dir_fd``.
``fd_real_path`` is defined in ``pinned_fs`` and every consumer binds that object,
and ``copy_file_pinned`` reports the identity it opened before it writes a byte.
The staging suite itself lives in ``test_pinned_staging.py`` and
``test_pinned_staging_skips_before_delete.py``.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, snapshot

# ── The helper's own contract ────────────────────────────────────────────────


def test_an_unknown_copytree_keyword_is_rejected_rather_than_dropped(tmp_path: Path) -> None:
    """``dirs_exist_ok`` is absorbed on purpose; anything else is a caller bug.

    Silently swallowing ``**kwargs`` is how a staging call would keep compiling after
    the flag it depends on stopped being honoured.
    """
    src = tmp_path / "source"
    src.mkdir()
    with pytest.raises(TypeError, match="unexpected keyword"):
        snapshot._copytree_safe(src, tmp_path / "dst", symlinks=True)


def test_the_tree_walk_capability_probe_names_a_function_that_supports_dir_fd() -> None:
    """Pins the bug that made every POSIX snapshot refuse before it was caught.

    The capability belongs to ``os.stat``. Whether ``os.lstat`` is also listed in
    ``os.supports_dir_fd`` depends on the CPython build (3.12.13 omits it, 3.12.16
    lists it), so a probe on ``os.lstat`` can report False on a fully capable
    platform, which turns the deliberate refusal into a blanket outage. The test pins
    that ``os.stat`` carries the capability and that the probe agrees, not what the
    stdlib says about ``os.lstat``.

    The membership assertions are guarded because `os.supports_dir_fd` is EMPTY on Windows,
    where "which function carries the capability" is not a meaningful question -- nothing
    does. Asserting it unguarded made this test fail on the Windows shard, which is the
    platform whose refusal path the rest of this change is careful about. Caught by CI, not
    locally: this was the first run in which every Windows shard concluded before I pushed
    a new head over it.
    """
    if os.name != "posix":
        assert os.supports_dir_fd == set() or os.stat not in os.supports_dir_fd
        assert pinned_fs.supports_pinned_tree_walk() is False
        return

    assert os.stat in os.supports_dir_fd
    if pinned_fs.supports_pinned_walk():
        assert pinned_fs.supports_pinned_tree_walk() is True


class TestFdRealPathHome:
    """``fd_real_path`` is defined in pinned_fs and every consumer binds THAT object.

    The primitive has cross-module consumers (hooks, sandbox, the app UI route,
    spec_builder), and its containment guarantees — including the Windows
    fail-closed branch — belong with the other descriptor-pinned mechanisms.
    Pinning the location keeps a refactor from quietly forking the
    implementation into a consumer module.
    """

    def test_defined_in_pinned_fs_and_exported(self):
        assert pinned_fs.fd_real_path.__module__ == "kiro_crew.pinned_fs"
        assert "fd_real_path" in pinned_fs.__all__

    def test_every_consumer_binds_the_pinned_fs_object(self):
        from kiro_crew import hooks, sandbox
        from kiro_crew.apps import routes as app_routes
        from kiro_crew.apps.builtins.spec_builder.backend import repository as spec_repository

        assert hooks._fd_real_path is pinned_fs.fd_real_path
        assert sandbox.fd_real_path is pinned_fs.fd_real_path
        assert app_routes.fd_real_path is pinned_fs.fd_real_path
        # spec_builder's descriptor-pinned reads live in repository.py (the
        # handlers refactor moved them out of routes.py).
        assert spec_repository.fd_real_path is pinned_fs.fd_real_path

    def test_resolves_an_open_descriptor_to_its_real_path(self, tmp_path: Path):
        target = tmp_path / "witness.txt"
        target.write_text("x")
        fd = os.open(target, os.O_RDONLY)
        try:
            got = pinned_fs.fd_real_path(fd)
        finally:
            os.close(fd)
        assert got is not None
        assert os.path.normcase(os.path.normpath(got)) == os.path.normcase(os.path.realpath(target))

    def test_fails_closed_on_a_dead_descriptor(self):
        # A descriptor number that cannot be open (far above any real fd
        # table): every platform route must answer None, never a guess. A
        # freshly-closed real fd would race reuse by another thread in the
        # test process, so the impossible number is the deterministic probe.
        assert pinned_fs.fd_real_path(2**30) is None


@pytest.mark.parametrize("fail", [False, True], ids=["success", "mid-write-failure"])
def test_copy_reports_opened_identity_before_writing(tmp_path, monkeypatch, fail):
    src = tmp_path / "source.txt"
    src.write_bytes(b"complete source")
    dst = tmp_path / "destination.txt"
    opened = []
    published = []
    copy_bytes = pinned_fs.shutil.copyfileobj

    def checked_copy(fsrc, fdst):
        assert len(opened) == 1
        actual = os.fstat(fdst.fileno())
        assert (opened[0].st_dev, opened[0].st_ino) == (actual.st_dev, actual.st_ino)
        assert opened[0].st_size == 0
        assert not published, "the existing callback stays success-only"
        if fail:
            fdst.write(fsrc.read(4))
            fdst.flush()
            raise OSError(errno.ENOSPC, "injected disk full")
        copy_bytes(fsrc, fdst)

    monkeypatch.setattr(pinned_fs.shutil, "copyfileobj", checked_copy)
    if fail:
        with pytest.raises(OSError, match="injected disk full"):
            pinned_fs.copy_file_pinned(
                str(src), str(dst), on_opened=opened.append, on_created=published.append
            )
        assert dst.read_bytes() == b""
        assert published == []
    else:
        assert pinned_fs.copy_file_pinned(
            str(src), str(dst), on_opened=opened.append, on_created=published.append
        )
        assert dst.read_bytes() == src.read_bytes()
        assert len(published) == 1
        assert published[0].st_size == len(b"complete source")
    actual = dst.stat()
    assert len(opened) == 1
    assert (opened[0].st_dev, opened[0].st_ino) == (actual.st_dev, actual.st_ino)

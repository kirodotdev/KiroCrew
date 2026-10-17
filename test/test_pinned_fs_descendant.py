"""``open_pinned_descendant_dir``: walk (and optionally create) a relative directory
chain below a trusted root, refusing a link at every component -- the root included.

The store-art read and publish paths both reach a leaf directory through this one
primitive rather than re-implementing a no-follow chain walk each. These tests pin
what both callers depend on:

- the pinned (POSIX) arm yields the leaf directory's descriptor and refuses a link at
  the root, an intermediate, or (via the empty-chain case) nothing at all;
- ``create=True`` makes the missing chain, ``create=False`` refuses a missing one;
- the by-name (Windows) arm -- forced by monkeypatching ``supports_pinned_walk`` --
  yields ``None`` after an ``lstat`` chain that refuses a linked root/intermediate and
  creates the missing components, matching the residual by-name posture the module
  documents for a platform that cannot pin a directory;
- negative controls prove each refusal observes the link and not the fixture.
"""

from __future__ import annotations

import os

import pytest

from conftest import requires_o_nofollow, requires_symlinks
from kiro_crew import pinned_fs
from kiro_crew.pinned_fs import PinnedPathRefusal, open_pinned_descendant_dir


def _fstat_ino(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


# ---------------------------------------------------------------------------
# The pinned (POSIX) arm
# ---------------------------------------------------------------------------


@requires_o_nofollow
class TestPinnedArm:
    def test_a_clean_chain_yields_the_leaf_dir_fd(self, tmp_path):
        leaf_dir = tmp_path / "a" / "b" / "c"
        leaf_dir.mkdir(parents=True)
        (leaf_dir / "file.txt").write_bytes(b"hi")

        with open_pinned_descendant_dir(tmp_path, ("a", "b", "c"), what="chain") as leaf:
            assert isinstance(leaf, int)
            # The yielded fd is the leaf directory: it can open the file under it.
            fd = os.open("file.txt", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=leaf)
            try:
                assert os.read(fd, 8) == b"hi"
            finally:
                os.close(fd)
            # And it IS the directory we expect.
            assert _fstat_ino(leaf) == (leaf_dir.stat().st_dev, leaf_dir.stat().st_ino)

    def test_an_empty_chain_yields_the_root_itself(self, tmp_path):
        with open_pinned_descendant_dir(tmp_path, (), what="root") as leaf:
            assert isinstance(leaf, int)
            assert _fstat_ino(leaf) == (tmp_path.stat().st_dev, tmp_path.stat().st_ino)

    @requires_symlinks
    def test_a_symlinked_intermediate_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        (outside / "c").mkdir(parents=True)
        (tmp_path / "a").mkdir()
        # a/b is a symlink to the outside directory; the walk must refuse it.
        os.symlink(outside, tmp_path / "a" / "b", target_is_directory=True)

        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(tmp_path, ("a", "b", "c"), what="chain"):
                pass

    @requires_symlinks
    def test_a_symlinked_root_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        (outside / "a").mkdir(parents=True)
        linked_root = tmp_path / "root"
        os.symlink(outside, linked_root, target_is_directory=True)

        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(linked_root, ("a",), what="chain"):
                pass

    def test_a_missing_component_without_create_is_refused(self, tmp_path):
        (tmp_path / "a").mkdir()
        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(tmp_path, ("a", "b"), what="chain"):
                pass

    def test_create_makes_the_missing_chain_and_yields_its_leaf(self, tmp_path):
        with open_pinned_descendant_dir(
            tmp_path, ("x", "y", "z"), what="chain", create=True
        ) as leaf:
            assert isinstance(leaf, int)
            # Write through the pinned leaf; the file lands in the created chain.
            fd = os.open("made.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=leaf)
            try:
                os.write(fd, b"ok")
            finally:
                os.close(fd)
        made = tmp_path / "x" / "y" / "z" / "made.txt"
        assert made.is_file() and made.read_bytes() == b"ok"

    def test_create_tolerates_existing_components(self, tmp_path):
        (tmp_path / "x" / "y").mkdir(parents=True)
        with open_pinned_descendant_dir(
            tmp_path, ("x", "y", "z"), what="chain", create=True
        ) as leaf:
            assert isinstance(leaf, int)
        assert (tmp_path / "x" / "y" / "z").is_dir()

    @requires_symlinks
    def test_create_still_refuses_a_link_at_an_existing_component(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "x").mkdir()
        os.symlink(outside, tmp_path / "x" / "y", target_is_directory=True)
        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(tmp_path, ("x", "y", "z"), what="chain", create=True):
                pass
        assert not (outside / "z").exists()

    def test_the_refusal_type_is_the_callers(self, tmp_path):
        # A caller that treats a refusal as an OSError (the publish path) gets one.
        (tmp_path / "a").mkdir()
        with pytest.raises(OSError):
            with open_pinned_descendant_dir(
                tmp_path, ("a", "missing"), what="chain", refusal=OSError
            ):
                pass

    def test_every_open_carries_o_nofollow(self, tmp_path, monkeypatch):
        (tmp_path / "a" / "b").mkdir(parents=True)
        seen_flags: list[int] = []
        real_open = os.open

        def _spy_open(path, flags, *args, **kwargs):
            seen_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {_spy_open})
        monkeypatch.setattr(os, "open", _spy_open)
        with open_pinned_descendant_dir(tmp_path, ("a", "b"), what="chain"):
            pass
        assert seen_flags
        assert all(flags & os.O_NOFOLLOW for flags in seen_flags), seen_flags


# ---------------------------------------------------------------------------
# The by-name (Windows) arm, forced by monkeypatching supports_pinned_walk
# ---------------------------------------------------------------------------


class TestByNameArm:
    def test_it_yields_none_and_validates_a_clean_chain(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        (tmp_path / "a" / "b").mkdir(parents=True)
        with open_pinned_descendant_dir(tmp_path, ("a", "b"), what="chain") as leaf:
            assert leaf is None  # the caller addresses the leaf by name

    def test_create_makes_the_missing_chain_by_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        with open_pinned_descendant_dir(
            tmp_path, ("x", "y", "z"), what="chain", create=True
        ) as leaf:
            assert leaf is None
        assert (tmp_path / "x" / "y" / "z").is_dir()

    def test_a_missing_component_without_create_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        (tmp_path / "a").mkdir()
        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(tmp_path, ("a", "b"), what="chain"):
                pass

    @requires_symlinks
    def test_a_symlinked_root_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        outside = tmp_path / "outside"
        (outside / "a").mkdir(parents=True)
        linked_root = tmp_path / "root"
        os.symlink(outside, linked_root, target_is_directory=True)
        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(linked_root, ("a",), what="chain", create=True):
                pass
        # Nothing was created inside the link target.
        assert not (outside / "a" / "made").exists()

    @requires_symlinks
    def test_a_symlinked_intermediate_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "a").mkdir()
        os.symlink(outside, tmp_path / "a" / "b", target_is_directory=True)
        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(tmp_path, ("a", "b", "c"), what="chain", create=True):
                pass

    def test_a_non_directory_component_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").write_bytes(b"not a dir")
        with pytest.raises(PinnedPathRefusal):
            with open_pinned_descendant_dir(tmp_path, ("a", "b"), what="chain", create=True):
                pass

    def test_a_clean_chain_is_not_refused_negative_control(self, tmp_path, monkeypatch):
        # The SAME by-name arm on a clean real chain does NOT raise, proving the
        # refusals above observe the link/type and not the arm being inert.
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        (tmp_path / "a" / "b").mkdir(parents=True)
        with open_pinned_descendant_dir(tmp_path, ("a", "b"), what="chain") as leaf:
            assert leaf is None

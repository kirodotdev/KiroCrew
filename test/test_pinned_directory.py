"""``platform_compat.PinnedDirectory``: act on the directory you inspected.

The class exists because the two platforms reach that property by OPPOSITE routes,
and a caller written in terms of one is broken on the other:

* POSIX has ``dir_fd``-relative operations and the pin does NOT block a rename, so
  the descriptor must be used for everything.
* Windows has no ``dir_fd`` operations at all, so everything goes by path -- sound
  only because the pin makes the path stable (no ``FILE_SHARE_DELETE``, so the
  directory and every ancestor refuse a rename and a delete while it lives).

So these tests assert the BEHAVIOUR both routes are for, plus one test per
platform for the mechanism that route depends on. Links are staged through
``conftest.make_dir_link`` -- a symlink on POSIX, a real junction on Windows.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import platform_compat, skills


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    # Nested one level below tmp_path on purpose: the Windows pin test renames
    # this directory's PARENT, and the target of that rename has to stay inside
    # tmp_path. Rooted directly at tmp_path it would land in pytest's basetemp,
    # so a regressed pin would strand the fixture outside its own cleanup root.
    root = tmp_path / "box" / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
    return root


class TestItDoesNotDivergeFromPinnedFs:
    """`pinned_fs` owns this discipline; this class is its cross-platform arm.

    `pinned_fs` imports THIS module for its Windows no-reparse open, so the
    dependency runs one way only and `platform_compat` cannot import it back to
    share a helper. That leaves one deliberate copy of the POSIX open flags, and
    these assertions are what keep the copy honest: a flag added on either side
    reddens here instead of leaving the two modules quietly walking trees by
    different rules.
    """

    def test_the_posix_open_flags_match_pinned_fs(self) -> None:
        from kiro_crew import pinned_fs

        assert platform_compat.pinned_dir_flags() == pinned_fs.dir_flags()

    def test_the_read_is_not_looser_than_pinned_fs_read(self, tmp_path: Path) -> None:
        """This read refuses a hardlinked file; ``pinned_fs.read_file_pinned`` does not.

        Stated as a test rather than a comment because the asymmetry is easy to read
        as an oversight in either direction. ``refuse_hardlink_alias`` guards that
        module's WRITE and COPY paths, not its read, and this class applies the same
        refusal to a read because what it serves is an agent-written tree read out
        through an API. If `pinned_fs` ever adopts it on the read side, this test is
        where the two meet again -- it may then be tightened, never silently dropped.
        """
        from kiro_crew import pinned_fs

        target = tmp_path / "box" / "aliased.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", encoding="utf-8")
        try:
            os.link(target, target.parent / "alias.txt")
        except (OSError, NotImplementedError, AttributeError) as exc:
            pytest.skip(f"hardlinks unavailable here: {exc}")

        with platform_compat.pinned_directory(target.parent) as pinned:
            with pytest.raises(OSError):
                pinned.read_text("aliased.txt")

        assert (
            pinned_fs.read_file_pinned(target, what="parity probe") == "secret"
        ), "pinned_fs adopted a hardlink refusal on read; make this class's guard the shared one"


class TestWhatItReadsAndRemoves:
    def test_it_lists_and_classifies_its_own_entries(self, tree: Path) -> None:
        with platform_compat.pinned_directory(tree) as pinned:
            assert sorted(pinned.names()) == ["a.txt", "sub"]
            assert pinned.is_dir("sub") is True
            assert pinned.is_dir("a.txt") is False
            assert pinned.is_link("sub") is False

    def test_an_unreadable_name_lstats_as_none_rather_than_raising(self, tree: Path) -> None:
        # Reaches the private `_lstat` deliberately: it is the seam both public
        # predicates below are built on, and it is private because no caller outside
        # the class needs a raw stat -- they ask is_link/is_dir. What matters is that
        # a missing name answers None here instead of raising, so neither predicate
        # has to catch anything.
        with platform_compat.pinned_directory(tree) as pinned:
            assert pinned._lstat("a.txt") is not None
            assert pinned._lstat("not-there") is None
            assert pinned.is_dir("not-there") is False
            assert pinned.is_link("not-there") is False

    def test_it_removes_a_file_and_an_empty_directory(self, tree: Path) -> None:
        with platform_compat.pinned_directory(tree) as pinned:
            with pinned.child("sub") as sub:
                sub.unlink("b.txt")
            pinned.rmdir("sub")
            pinned.unlink("a.txt")
            assert pinned.names() == []
        assert tree.is_dir() and not any(tree.iterdir())


class TestALinkIsSeenAndNeverTraversed:
    def test_a_directory_link_is_classified_as_a_link_not_a_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (target / "precious.txt").write_text("outside", encoding="utf-8")
        holder = tmp_path / "holder"
        holder.mkdir()
        make_dir_link(holder / "link", target)

        with platform_compat.pinned_directory(holder) as pinned:
            assert pinned.is_link("link") is True
            assert pinned.is_dir("link") is False, "a link must not answer as its target's shape"

    def test_child_refuses_a_link_in_the_open_itself(self, tmp_path: Path) -> None:
        """The refusal is the open, not a check before it -- that is the point."""
        target = tmp_path / "target"
        target.mkdir()
        holder = tmp_path / "holder"
        holder.mkdir()
        make_dir_link(holder / "link", target)

        with platform_compat.pinned_directory(holder) as pinned:
            with pytest.raises(OSError):
                pinned.child("link").close()

    def test_pinned_directory_refuses_a_link_at_the_top(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        make_dir_link(link, target)

        with pytest.raises(OSError):
            platform_compat.pinned_directory(link).close()

    def test_removing_a_link_leaves_its_target_alone(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (target / "precious.txt").write_text("outside", encoding="utf-8")
        holder = tmp_path / "holder"
        holder.mkdir()
        make_dir_link(holder / "link", target)

        with platform_compat.pinned_directory(holder) as pinned:
            pinned.unlink("link")
            assert pinned.names() == []
        assert (target / "precious.txt").read_text(encoding="utf-8") == "outside"
        assert target.is_dir()


class TestTheMechanismEachPlatformDependsOn:
    """One test per platform for the property that route rests on.

    Guards against the class silently degrading to by-name operations with no
    protection at all: on POSIX that would mean the descriptor is not being used,
    and on Windows that the share-mode lock is not being taken.
    """

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="dir_fd operations are POSIX-only")
    def test_on_posix_the_descriptor_survives_a_rename_of_the_name(self, tree: Path) -> None:
        """POSIX does not block the rename, so the descriptor is what must carry."""
        with platform_compat.pinned_directory(tree) as pinned:
            moved = tree.parent / "moved"
            os.rename(tree, moved)
            try:
                # The name is gone, so anything by-name would fail or hit another
                # object. Reading through the pin still describes the same directory.
                assert sorted(pinned.names()) == ["a.txt", "sub"]
                pinned.unlink("a.txt")
                assert sorted(pinned.names()) == ["sub"]
            finally:
                os.rename(moved, tree)

    @pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="share-mode locking is Windows-only")
    def test_on_windows_the_pin_blocks_renaming_the_directory_and_its_parent(
        self, tree: Path, tmp_path: Path
    ) -> None:
        """Windows has no dir_fd, so by-path is sound only while this holds.

        Both renames must stay inside ``tmp_path`` even when they SUCCEED, which is
        what a regressed pin would do: a test whose failure path moves its fixture out
        of the cleanup root leaves litter behind on an already-red run. The assert
        below pins that, so un-nesting the fixture fails here instead of quietly
        restoring the escape.
        """
        assert tree.parent.parent == tmp_path, "the parent-rename target must stay in tmp_path"
        with platform_compat.pinned_directory(tree) as pinned:
            assert pinned.names(), "fixture is empty, so the pin proves nothing"
            with pytest.raises(OSError):
                os.rename(tree, tree.parent / "moved")
            with pytest.raises(OSError):
                os.rename(tree.parent, tree.parent.parent / "parent-moved")

    def test_a_parent_stays_usable_while_a_child_is_pinned(self, tree: Path) -> None:
        """A chain of pins is what anchors a whole path, so both must be live."""
        with platform_compat.pinned_directory(tree) as parent:
            with parent.child("sub") as child:
                assert child.names() == ["b.txt"]
                assert sorted(parent.names()) == ["a.txt", "sub"]


class TestChildIfRealDir:
    """The screen-then-descend fallback three callers share.

    They share the QUESTION and not the action -- two remove the entry, one leaves
    it alone -- so what this must get right is when it answers None and when it
    declines to answer at all.
    """

    def test_a_real_directory_is_pinned(self, tree: Path) -> None:
        with platform_compat.pinned_directory(tree) as pinned:
            sub = pinned.child_if_real_dir("sub")
            assert sub is not None
            with sub:
                assert sub.names() == ["b.txt"]

    def test_a_plain_file_and_a_link_both_answer_none(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        holder = tmp_path / "holder"
        holder.mkdir()
        (holder / "a.txt").write_text("a", encoding="utf-8")
        make_dir_link(holder / "linked", target)

        with platform_compat.pinned_directory(holder) as pinned:
            assert pinned.child_if_real_dir("a.txt") is None
            assert pinned.child_if_real_dir("linked") is None
        assert target.exists(), "asking about the name disturbed the link target"

    def test_the_depth_refusal_re_raises_instead_of_answering_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tree too deep must FAIL the operation, never look like a link to delete.

        The one contract a later simplification would break: two of the callers
        UNLINK on None, so collapsing the re-raise into None turns "this chain is
        too deep to sweep" into "remove that entry", which is a silent deletion
        where a classified refusal belongs.
        """
        monkeypatch.setattr(platform_compat, "PINNED_TREE_MAX_DEPTH", 1)
        deep = tmp_path / "box" / "a" / "b"
        deep.mkdir(parents=True)

        with platform_compat.pinned_directory(tmp_path / "box") as pinned:
            first = pinned.child_if_real_dir("a")
            assert first is not None
            with first:
                with pytest.raises(OSError) as caught:
                    first.child_if_real_dir("b")
        assert caught.value.errno == errno.ENAMETOOLONG
        assert deep.is_dir(), "the over-deep directory must be left in place"

    def test_the_descriptor_is_closed_on_exit(self, tree: Path) -> None:
        # Reaches the private slot deliberately: the class exposes NO public
        # accessor for the descriptor because no consumer needs one, and the
        # Windows route operates by path, so ``names()`` keeps working after the
        # handle is gone and cannot stand in for this.
        pinned = platform_compat.pinned_directory(tree)
        with pinned:
            assert os.fstat(pinned._fd).st_mode
        with pytest.raises(OSError):
            os.fstat(pinned._fd)

    def test_no_public_accessor_hands_out_the_descriptor(self, tree: Path) -> None:
        with platform_compat.pinned_directory(tree) as pinned:
            assert not hasattr(pinned, "fd")

    def test_the_chain_refuses_to_descend_past_the_depth_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The BOUND is what this pins, with the cap lowered so the test does not
        # depend on building a 64-deep tree (which no platform needs to allow for
        # the mechanism to hold). The constant's own value is pinned separately.
        monkeypatch.setattr(platform_compat, "PINNED_TREE_MAX_DEPTH", 2)
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        with platform_compat.pinned_directory(tmp_path) as root:
            with root.child("a") as one:
                with one.child("b") as two:
                    with pytest.raises(OSError) as exc:
                        two.child("c")
        assert exc.value.errno == errno.ENAMETOOLONG
        # Not a link refusal: a caller dispatching on what is at the name must
        # re-raise this rather than unlink a real directory.
        assert not isinstance(exc.value, NotADirectoryError)

    def test_the_depth_bound_is_the_repos_tree_depth_number(self) -> None:
        # Same number and same reasoning as skills._PROJECT_SKILL_MAX_DEPTH: past any
        # legitimate tree, far short of the recursion limit and of any fd soft limit.
        assert platform_compat.PINNED_TREE_MAX_DEPTH == 64
        assert platform_compat.PINNED_TREE_MAX_DEPTH == skills._PROJECT_SKILL_MAX_DEPTH

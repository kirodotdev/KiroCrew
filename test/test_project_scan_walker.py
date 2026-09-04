"""Tests for the project scanner's traversal and classification.

The walker is the part of the feature a user cannot inspect before trusting it:
it is pointed at an unfamiliar tree and its answer decides which folders get
created. So the tests here pin the four things that make that trustworthy —

1. **Bounded and read-only.** Links are never followed (a POSIX symlink or a
   Windows junction alike), dependency and
   build directories are never entered, the depth cap holds, and the tree on
   disk is byte-for-byte unchanged afterwards.
2. **Prune beats every signal.** A manifest under a pruned directory, or on a
   pruned directory itself, yields no candidate.
3. **Tier rules.** A repository or top-level manifest is auto-selected; a
   manifest nested inside a package is only offered; a directory the user has
   already used with Kiro is auto-selected wherever it sits.
4. **Determinism.** Two scans of an unchanged tree return equal trees, and a
   directory that cannot be read costs a warning rather than the whole scan.

Fixtures are built as literal directory layouts because the bugs this code can
have are layout-shaped; a mocked filesystem would hide exactly the cases
(symlink, permission, iteration order) worth testing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew.project_scan import (
    Candidate,
    CandidateTree,
    Tier,
    manifest_signal,
    scan,
)

_IS_WINDOWS = sys.platform == "win32"
# A root user reads a mode-000 directory regardless, so the unreadable-subtree
# case cannot be staged there.
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _make(root: Path, *layout: str) -> None:
    """Create the given layout under ``root``.

    An entry ending in ``/`` is a directory; anything else is an empty file with
    its parent directories created for it. Keeps a fixture readable as the tree
    it represents.
    """

    for entry in layout:
        target = root / entry
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")


def _by_path(tree: CandidateTree) -> dict[str, Candidate]:
    return {candidate.path: candidate for candidate in tree.candidates}


def _relative_paths(tree: CandidateTree, root: Path) -> list[str]:
    """Candidate paths as POSIX-style paths relative to ``root``, in tree order."""

    return [Path(candidate.path).relative_to(root).as_posix() for candidate in tree.candidates]


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Size and modification time of every entry under ``root``."""

    snapshot: dict[str, tuple[int, int]] = {}
    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = os.path.join(current, name)
            info = os.stat(path, follow_symlinks=False)
            snapshot[path] = (info.st_size, info.st_mtime_ns)
    return snapshot


class TestPackageBoundaries:
    def test_sibling_repositories_are_auto_selected(self, tmp_path: Path) -> None:
        _make(tmp_path, "api/.git/", "web/.git/", "notes/")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["api", "web"]
        assert {candidate.tier for candidate in tree.candidates} == {Tier.AUTO}

    def test_top_level_manifest_is_a_boundary(self, tmp_path: Path) -> None:
        _make(tmp_path, "service/pyproject.toml", "docs/")

        tree = scan(tmp_path)

        candidate = _by_path(tree)[str(tmp_path / "service")]
        assert candidate.tier is Tier.AUTO
        assert candidate.signals == (manifest_signal("pyproject.toml"),)

    def test_candidate_records_its_basename_and_absolute_path(self, tmp_path: Path) -> None:
        _make(tmp_path, "api/.git/")

        candidate = tree_single(scan(tmp_path))

        assert candidate.name == "api"
        assert candidate.path == str(tmp_path / "api")
        assert os.path.isabs(candidate.path)

    def test_signal_free_directory_is_omitted_but_still_traversed(self, tmp_path: Path) -> None:
        # "packages" carries nothing, so it is not offered — but the scan has to
        # walk through it or the packages a monorepo keeps there are all missed.
        _make(tmp_path, "packages/app/package.json", "packages/lib/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/app", "packages/lib"]

    def test_a_manifest_below_an_unmarked_directory_is_still_a_boundary(
        self, tmp_path: Path
    ) -> None:
        # Nothing above it was detected, so it is the package the user pointed
        # at, not a detail nested inside one — depth alone does not demote it.
        _make(tmp_path, "packages/app/package.json")

        assert tree_single(scan(tmp_path)).tier is Tier.AUTO

    def test_multiple_signals_are_all_recorded_in_a_fixed_order(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/", "app/.kiro/", "app/package.json", "app/pyproject.toml")

        candidate = tree_single(scan(tmp_path))

        assert candidate.signals == (
            "git",
            ".kiro",
            manifest_signal("package.json"),
            manifest_signal("pyproject.toml"),
        )


class TestNestedDetection:
    def test_nested_manifest_inside_a_package_is_offered(self, tmp_path: Path) -> None:
        _make(tmp_path, "repo/.git/", "repo/package.json", "repo/packages/ui/package.json")

        tiers = {path: candidate.tier for path, candidate in _by_path(scan(tmp_path)).items()}

        assert tiers == {
            str(tmp_path / "repo"): Tier.AUTO,
            str(tmp_path / "repo" / "packages" / "ui"): Tier.OFFERED,
        }

    def test_nested_kiro_directory_is_auto_selected(self, tmp_path: Path) -> None:
        # A directory already used with Kiro is one the user has themselves
        # treated as a project root, so it is ticked even when nested.
        _make(tmp_path, "repo/.git/", "repo/crates/engine/Cargo.toml", "repo/crates/engine/.kiro/")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "repo" / "crates" / "engine")]

        assert candidate.tier is Tier.AUTO
        assert ".kiro" in candidate.signals

    def test_nested_repository_is_auto_selected(self, tmp_path: Path) -> None:
        _make(tmp_path, "repo/.git/", "repo/vendor/forked/.git/")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "repo" / "vendor" / "forked")]

        assert candidate.tier is Tier.AUTO

    def test_nesting_is_recorded_against_the_nearest_candidate_ancestor(
        self, tmp_path: Path
    ) -> None:
        _make(
            tmp_path,
            "repo/.git/",
            "repo/packages/ui/package.json",
            "repo/packages/ui/tools/gen/package.json",
        )

        parents = {candidate.name: candidate.parent_path for candidate in scan(tmp_path).candidates}

        # "packages" and "tools" carry no signal, so they are not anyone's parent:
        # the folder tree mirrors detected packages, not the directory tree.
        assert parents == {
            "repo": None,
            "ui": str(tmp_path / "repo"),
            "gen": str(tmp_path / "repo" / "packages" / "ui"),
        }

    def test_a_deeper_manifest_under_an_offered_package_is_also_offered(
        self, tmp_path: Path
    ) -> None:
        _make(
            tmp_path, "repo/.git/", "repo/packages/ui/package.json", "repo/packages/ui/e2e/go.mod"
        )

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "repo" / "packages" / "ui" / "e2e")]

        assert candidate.tier is Tier.OFFERED

    def test_a_monorepo_root_makes_its_packages_nested(self, tmp_path: Path) -> None:
        # The scan root is never a candidate itself (the scaffold step creates its
        # folder from the root path), but its signals still decide the shape: a
        # root holding a manifest is a package, so what is below it is nested in
        # it and offered unticked rather than ticked by default.
        _make(tmp_path, "package.json", "packages/app/package.json", "packages/lib/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/app", "packages/lib"]
        assert {candidate.tier for candidate in tree.candidates} == {Tier.OFFERED}

    def test_a_repository_inside_a_monorepo_root_is_still_auto_selected(
        self, tmp_path: Path
    ) -> None:
        # ``.git`` and ``.kiro`` are unambiguous wherever they sit, so nesting
        # demotes a bare manifest and nothing else.
        _make(tmp_path, "Cargo.toml", "vendor/forked/.git/", "vendor/patched/Cargo.toml")

        tiers = {candidate.name: candidate.tier for candidate in scan(tmp_path).candidates}

        assert tiers == {"forked": Tier.AUTO, "patched": Tier.OFFERED}

    def test_a_directory_used_with_kiro_makes_its_packages_nested(self, tmp_path: Path) -> None:
        # A ``.kiro`` directory is the user having already treated that directory
        # as a project root, so a manifest below it is nested in a package.
        _make(tmp_path, "project/.kiro/", "project/service/pyproject.toml")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "project" / "service")]

        assert candidate.tier is Tier.OFFERED


class TestPruning:
    def test_dependency_directory_contents_never_become_candidates(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "app/package.json",
            "app/node_modules/left-pad/package.json",
            "app/node_modules/.package-lock.json",
        )

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    @pytest.mark.parametrize(
        "pruned",
        [
            "node_modules",
            "dist",
            "build",
            "target",
            "env",
            "venv",
            ".venv",
            "__pycache__",
            "DerivedData",
        ],
    )
    def test_a_pruned_directory_carrying_a_manifest_is_still_pruned(
        self, tmp_path: Path, pruned: str
    ) -> None:
        # Prune wins over detection: classifying first and filtering afterwards
        # leaks a candidate the moment a new signal is added without a matching
        # filter, so a pruned name never reaches the classifier at all.
        _make(tmp_path, f"app/{pruned}/package.json", f"app/{pruned}/.kiro/", "app/.git/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    def test_hidden_directories_are_pruned(self, tmp_path: Path) -> None:
        _make(tmp_path, ".cache/pkg/package.json", ".idea/workspace/package.json", "app/.git/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    def test_the_kiro_directory_is_a_signal_and_not_a_container(self, tmp_path: Path) -> None:
        # ``.kiro`` holds steering and specs; descending into it could only
        # manufacture candidates out of the tool's own files.
        _make(tmp_path, "app/.kiro/specs/demo/package.json")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    def test_the_git_directory_is_a_signal_and_not_a_container(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/modules/sub/package.json")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]


class TestFirebaseAppRoots:
    def test_a_firebase_json_marks_an_app_root_and_children_nest_under_it(
        self, tmp_path: Path
    ) -> None:
        # A Firebase app commonly has no manifest at its own level — the
        # package.json files live in its functions/ and web/ children — so
        # firebase.json is the marker that makes the app itself a candidate
        # the children can hang off, instead of the children floating free.
        _make(
            tmp_path,
            "package.json",
            "apps/planner/firebase.json",
            "apps/planner/functions/package.json",
            "apps/planner/web/package.json",
        )

        tree = scan(tmp_path)
        by_path = _by_path(tree)
        planner = by_path[str(tmp_path / "apps" / "planner")]
        assert planner.signals == (manifest_signal("firebase.json"),)
        # A deploy root is unambiguous even nested inside the workspace root
        # package: a deploy config never names a build fixture.
        assert planner.tier is Tier.AUTO
        for child in ("functions", "web"):
            assert by_path[str(tmp_path / "apps" / "planner" / child)].parent_path == planner.path


class TestGitignorePruning:
    """The project's own ``.gitignore`` prunes with the same precedence as names.

    The shape that motivated this is Xcode/SwiftPM: ``tmp/derived_data/
    SourcePackages/checkouts/`` holds every dependency as a full git clone, so
    each carries the strongest (repository) signal while being exactly what the
    project's ``.gitignore`` already disowns.
    """

    def test_a_gitignored_dependency_store_of_git_clones_is_pruned(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "tmp/derived_data/SourcePackages/checkouts/Alamofire/.git/",
            "tmp/derived_data/SourcePackages/checkouts/BigInt/.git/",
            "Sources/App/.git/",
        )
        (tmp_path / ".gitignore").write_text("tmp/\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["Sources/App"]

    def test_without_the_gitignore_the_same_clones_are_offered(self, tmp_path: Path) -> None:
        # The control that keeps the test above honest: the clones DO carry the
        # strongest signal, and only the .gitignore removes them.
        _make(
            tmp_path,
            "tmp/derived_data/SourcePackages/checkouts/Alamofire/.git/",
            "Sources/App/.git/",
        )

        assert "tmp/derived_data/SourcePackages/checkouts/Alamofire" in _relative_paths(
            scan(tmp_path), tmp_path
        )

    def test_negation_re_includes_a_sibling(self, tmp_path: Path) -> None:
        _make(tmp_path, "vendor/keep/.git/", "vendor/other/.git/")
        (tmp_path / ".gitignore").write_text("vendor/*\n!vendor/keep\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["vendor/keep"]

    def test_a_nested_gitignore_prunes_only_its_own_subtree(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/sub/.git/", "b/sub/.git/")
        (tmp_path / "a" / ".gitignore").write_text("sub/\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["b/sub"]

    def test_a_gitignore_above_the_scan_root_has_no_effect(self, tmp_path: Path) -> None:
        # The scanner never reads outside the tree the user pointed at, so a
        # parent directory's exclusions are invisible by construction.
        _make(tmp_path, "repo/pkg/.git/")
        (tmp_path / ".gitignore").write_text("pkg/\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path / "repo"), tmp_path / "repo") == ["pkg"]

    def test_an_ignored_manifest_is_no_signal_and_no_declaration(self, tmp_path: Path) -> None:
        # `generated/` holds a package.json that both signals a package and
        # declares members; ignoring the FILE (not the directory) must silence
        # both roles while the directory itself stays walkable.
        _make(tmp_path, "generated/lib/.git/", "app/.git/")
        (tmp_path / "generated" / "package.json").write_text(
            '{"workspaces": ["member"]}', encoding="utf-8"
        )
        (tmp_path / "generated" / "member").mkdir()
        (tmp_path / ".gitignore").write_text("generated/package.json\n", encoding="utf-8")

        paths = _relative_paths(scan(tmp_path), tmp_path)
        assert "generated" not in paths  # no manifest signal
        assert "generated/member" not in paths  # no declaration read
        assert set(paths) == {"app", "generated/lib"}  # the subtree still walks

    def test_a_declared_member_inside_an_ignored_directory_is_dropped(self, tmp_path: Path) -> None:
        # Members arrive by name rather than by walking, so they need the same
        # judgement: a root declaration naming a directory the project has
        # disowned must not resurrect it.
        _make(tmp_path, "app/.git/", "out/pkg/")
        (tmp_path / "package.json").write_text('{"workspaces": ["out/pkg"]}', encoding="utf-8")
        (tmp_path / ".gitignore").write_text("out/\n", encoding="utf-8")

        assert "out/pkg" not in _relative_paths(scan(tmp_path), tmp_path)

    def test_an_unreadable_gitignore_costs_the_layer_not_the_scan(self, tmp_path: Path) -> None:
        # Same recovery rule as declarations: the file is refused with a warning
        # and the scan continues as if it were absent (oversized = unreadable).
        _make(tmp_path, "tmp/pkg/.git/", "app/.git/")
        (tmp_path / ".gitignore").write_text("tmp/\n" + "#" * (512 * 1024), encoding="utf-8")

        tree = scan(tmp_path)
        assert set(_relative_paths(tree, tmp_path)) == {"app", "tmp/pkg"}
        assert any(".gitignore" in warning for warning in tree.warnings)

    def test_a_malformed_gitignore_costs_the_layer_not_the_scan(self, tmp_path: Path) -> None:
        # A lone ``!`` is refused by the gitignore grammar with a ValueError
        # subclass. Uncaught, that is an HTTP 500 from a tree the user merely
        # pointed at; the recovery rule is the same as unreadable/oversized —
        # the file costs a warning and its layer, so the ``tmp/`` pattern in
        # the same file no longer prunes.
        _make(tmp_path, "tmp/pkg/.git/", "app/.git/")
        (tmp_path / ".gitignore").write_text("tmp/\n!\n", encoding="utf-8")

        tree = scan(tmp_path)
        assert set(_relative_paths(tree, tmp_path)) == {"app", "tmp/pkg"}
        assert any(".gitignore" in warning for warning in tree.warnings)


class TestSymlinks:
    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_symlinked_directory_is_not_traversed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        _make(outside, "secret/package.json")
        root = tmp_path / "root"
        _make(root, "app/.git/")
        (root / "link").symlink_to(outside, target_is_directory=True)

        tree = scan(root)

        assert _relative_paths(tree, root) == ["app"]

    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_symlink_loop_back_to_the_root_terminates(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/")
        (tmp_path / "app" / "self").symlink_to(tmp_path, target_is_directory=True)

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    @pytest.mark.skipif(_IS_WINDOWS, reason="file symlinks need elevation on Windows")
    def test_symlinked_manifest_still_signals(self, tmp_path: Path) -> None:
        # Only the filename is read, never the target, so honouring a symlinked
        # manifest cannot reach outside the root.
        _make(tmp_path, "shared/package.json", "app/src/")
        (tmp_path / "app" / "package.json").symlink_to(tmp_path / "shared" / "package.json")

        assert _by_path(scan(tmp_path))[str(tmp_path / "app")].signals == (
            manifest_signal("package.json"),
        )


class TestDirectorySwapRace:
    """A directory verified as real must not be readable through a later symlink.

    ``entry.is_dir(follow_symlinks=False)`` settles what a child is when its
    parent is listed, but the descent re-resolves the child by NAME. A directory
    swapped for a symlink in between is therefore read through that link, and the
    candidates it yields carry a path string that still looks inside the root — so
    the containment invariant, which compares path strings, cannot catch it. These
    tests stage that swap deterministically rather than racing it.
    """

    @staticmethod
    def _swap_after_listing(
        monkeypatch: pytest.MonkeyPatch,
        *,
        when_listing: Path,
        replace: Path,
        with_link_to: Path,
        park: Path,
    ) -> None:
        """Turn ``replace`` into a symlink to ``with_link_to`` once ``when_listing`` is read.

        Wrapping the module's own directory read is what makes the swap land in
        the real window: after the parent has listed the child and recorded it,
        and before the walk opens that child.

        The swapped-out directory is moved to ``park`` rather than removed, which
        is what makes the identity check's premise hold on every filesystem. An
        inode number freed by ``rmdir`` is commonly handed straight back to the
        next allocation, so a removed directory and the symlink replacing it can
        share an inode number and be indistinguishable by identity; keeping the
        directory alive guarantees the link is a different inode. ``park`` must
        sit outside the scan root so the moved directory is not itself scanned.
        """

        from kiro_crew import project_scan

        original = project_scan._read_dir

        def swapping_read_dir(
            directory: str, manifests: frozenset[str], identity: tuple[int, int] | None = None
        ) -> object:
            contents = original(directory, manifests, identity)
            if os.path.samefile(directory, when_listing) and not replace.is_symlink():
                replace.rename(park)
                replace.symlink_to(with_link_to, target_is_directory=True)
            return contents

        monkeypatch.setattr(project_scan, "_read_dir", swapping_read_dir)

    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_a_directory_swapped_for_a_link_after_listing_is_not_read_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        _make(outside, "leaked/package.json")
        root = tmp_path / "root"
        _make(root, "kept/.git/", "swapped/")
        self._swap_after_listing(
            monkeypatch,
            when_listing=root,
            replace=root / "swapped",
            with_link_to=outside,
            park=tmp_path / "swapped-parked",
        )

        tree = scan(root)

        # Without the descriptor-pinned open, "swapped/leaked" is a candidate:
        # a real package discovered outside the tree the user pointed at, wearing
        # a path that claims to be inside it.
        assert _relative_paths(tree, root) == ["kept"]
        assert any(str(root / "swapped") in warning for warning in tree.warnings)

    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_the_identity_check_alone_refuses_a_swap_that_changed_the_inode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pins the branch Windows takes, where neither O_NOFOLLOW nor a
        # descriptor-based scandir exists and the recorded identity is the first
        # of the two checks left (the leaf-resolution check is pinned separately
        # below). Exercised here because the platform that runs it cannot stage
        # a symlink without elevation.
        #
        # The property is narrow on purpose: the identity refuses a swap it can
        # SEE, meaning one whose inode differs from the recorded one — which is
        # why the swap is staged so the inode is guaranteed to differ.
        from kiro_crew import project_scan

        monkeypatch.setattr(project_scan, "_PINNED_SCANDIR", False)
        outside = tmp_path / "outside"
        _make(outside, "leaked/package.json")
        root = tmp_path / "root"
        _make(root, "kept/.git/", "swapped/")
        self._swap_after_listing(
            monkeypatch,
            when_listing=root,
            replace=root / "swapped",
            with_link_to=outside,
            park=tmp_path / "swapped-parked",
        )

        tree = scan(root)

        assert _relative_paths(tree, root) == ["kept"]
        assert any(str(root / "swapped") in warning for warning in tree.warnings)

    def test_the_resolution_check_refuses_a_swap_the_identity_cannot_see(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact Windows shape: no descriptor pinning AND no per-entry
        # identity (a directory listing there reports zero inodes, recorded as
        # no identity at all). The leaf-resolution check is then the only guard
        # left — and ``realpath`` treats the POSIX symlink staged here exactly
        # as it treats the junction this platform cannot stage, so the property
        # proved is the one Windows relies on.
        from kiro_crew import project_scan

        monkeypatch.setattr(project_scan, "_PINNED_SCANDIR", False)
        monkeypatch.setattr(project_scan, "_entry_identity", lambda entry: None)
        outside = tmp_path / "outside"
        _make(outside, "leaked/package.json")
        root = tmp_path / "root"
        _make(root, "kept/.git/", "swapped/")
        self._swap_after_listing(
            monkeypatch,
            when_listing=root,
            replace=root / "swapped",
            with_link_to=outside,
            park=tmp_path / "swapped-parked",
        )

        tree = scan(root)

        assert _relative_paths(tree, root) == ["kept"]
        assert any(str(root / "swapped") in warning for warning in tree.warnings)

    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_an_ancestor_of_the_root_swapped_after_validation_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The root is the one frame with no parent to record its identity, so
        # it is pinned at scan entry instead. O_NOFOLLOW cannot catch this
        # shape — the swapped component is an ANCESTOR, so the final component
        # of the root path is still a real directory; only the recorded inode
        # differing from the one actually opened can refuse it.
        from kiro_crew import project_scan

        outside = tmp_path / "outside"
        _make(outside, "parent/root/leaked/package.json")
        parent = tmp_path / "parent"
        _make(parent, "root/kept/.git/")
        root = parent / "root"

        original = project_scan._read_dir
        swapped = {"done": False}

        def swapping_read_dir(
            directory: str, manifests: frozenset[str], identity: tuple[int, int] | None = None
        ) -> object:
            # Fires before the ROOT is first opened — after scan() captured its
            # identity, which is exactly the window the pin exists for.
            if not swapped["done"]:
                swapped["done"] = True
                parent.rename(tmp_path / "parent-parked")
                (tmp_path / "parent").symlink_to(outside / "parent", target_is_directory=True)
            return original(directory, manifests, identity)

        monkeypatch.setattr(project_scan, "_read_dir", swapping_read_dir)

        tree = scan(root)

        assert tree.candidates == ()
        assert any(str(root) in warning for warning in tree.warnings)

    def test_an_unswapped_scan_still_reports_every_candidate(self, tmp_path: Path) -> None:
        # The guard must not cost a normal scan its tree: same layout, no swap.
        _make(tmp_path, "kept/.git/", "also/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["also", "kept"]
        assert tree.warnings == ()


class TestIdentityUnavailable:
    """A platform that cannot identify a directory must still scan it.

    Windows fills ``st_ino``/``st_dev`` with zeros for every
    ``DirEntry.stat()`` — the real values need a full ``os.stat`` — while the
    descent's own ``os.lstat`` reports a REAL inode. Comparing the two would
    therefore mismatch on every child and skip the entire tree, so a zero inode
    has to mean "no identity" rather than "an identity that happens to be zero".
    """

    def test_a_zero_inode_is_recorded_as_no_identity(self) -> None:
        from kiro_crew import project_scan

        class _ZeroStat:
            st_dev = 0
            st_ino = 0

        class _ZeroEntry:
            def stat(self, *, follow_symlinks: bool = True) -> object:
                return _ZeroStat()

        assert project_scan._entry_identity(_ZeroEntry()) is None  # type: ignore[arg-type]

    def test_a_nonzero_inode_is_still_recorded(self) -> None:
        # The zero rule must not swallow genuine identities.
        from kiro_crew import project_scan

        class _RealStat:
            st_dev = 7
            st_ino = 42

        class _RealEntry:
            def stat(self, *, follow_symlinks: bool = True) -> object:
                return _RealStat()

        assert project_scan._entry_identity(_RealEntry()) == (7, 42)  # type: ignore[arg-type]

    def test_a_real_inode_is_still_recorded(self, tmp_path: Path) -> None:
        # Same rule against a genuine DirEntry, which only carries an inode on
        # platforms whose DirEntry.stat() populates one. The guard asks the
        # production helper itself rather than reading stat() separately, so the
        # skip condition cannot drift from what the code under test decides.
        from kiro_crew import project_scan

        _make(tmp_path, "pkg/")
        with os.scandir(tmp_path) as entries:
            identity = project_scan._entry_identity(next(iter(entries)))

        if identity is None:
            pytest.skip("this platform's DirEntry.stat() carries no inode")
        assert identity[1] != 0

    def test_zeroed_identities_do_not_cost_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end in the Windows shape: no descriptor pinning available AND
        # every recorded identity zeroed. Every candidate must still be reported.
        from kiro_crew import project_scan

        monkeypatch.setattr(project_scan, "_PINNED_SCANDIR", False)
        monkeypatch.setattr(project_scan, "_entry_identity", lambda entry: (0, 0))
        _make(tmp_path, "kept/.git/", "nested/inner/package.json", "also/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["also", "kept", "nested/inner"]
        assert tree.warnings == ()


class TestRedirectingEntries:
    """The descent gate reads redirection from the entry's own listing data.

    A Windows directory junction lstats as a plain directory, so
    ``is_dir(follow_symlinks=False)`` alone would descend it — into whatever
    tree it targets, with no elevation needed to create one (``mklink /J``).
    The gate tests the name-surrogate bit of the listing-time reparse tag
    instead of enumerating link kinds; these pin that logic on every platform,
    junctions available or not.
    """

    class _Entry:
        def __init__(self, attrs: int = 0, tag: int = 0, fail: bool = False) -> None:
            self._attrs = attrs
            self._tag = tag
            self._fail = fail

        def stat(self, *, follow_symlinks: bool = True) -> object:
            if self._fail:
                raise OSError("stat failed")
            entry = self

            class _Stat:
                st_file_attributes = entry._attrs
                st_reparse_tag = entry._tag

            return _Stat()

    _REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
    # The stat module defines IO_REPARSE_TAG_* on Windows only, so the tests
    # carry the Windows SDK values themselves — both tags have the
    # name-surrogate bit (0x20000000) set.
    _TAG_MOUNT_POINT = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT (a junction)
    _TAG_SYMLINK = 0xA000000C  # IO_REPARSE_TAG_SYMLINK

    def test_a_junction_tagged_entry_is_refused(self) -> None:
        from kiro_crew import project_scan

        entry = self._Entry(attrs=self._REPARSE, tag=self._TAG_MOUNT_POINT)
        assert project_scan._entry_redirects(entry) is True  # type: ignore[arg-type]

    def test_a_symlink_tagged_entry_is_refused(self) -> None:
        from kiro_crew import project_scan

        entry = self._Entry(attrs=self._REPARSE, tag=self._TAG_SYMLINK)
        assert project_scan._entry_redirects(entry) is True  # type: ignore[arg-type]

    def test_an_in_place_reparse_decoration_still_scans(self) -> None:
        # A cloud placeholder (or WCI/dedup) directory carries a reparse point
        # whose tag has NO name-surrogate bit: it is a real local directory
        # decorated in place, and refusing it would silently skip a user's
        # OneDrive-backed tree.
        from kiro_crew import project_scan

        entry = self._Entry(attrs=self._REPARSE, tag=0x9000_001A)
        assert project_scan._entry_redirects(entry) is False  # type: ignore[arg-type]

    def test_a_plain_directory_still_scans(self) -> None:
        # The POSIX shape: no st_file_attributes at all resolves to 0.
        from kiro_crew import project_scan

        assert project_scan._entry_redirects(self._Entry()) is False  # type: ignore[arg-type]

    def test_an_unstattable_entry_is_left_to_the_descent_guards(self) -> None:
        # Classification settles nothing on a failed stat; the descent-time
        # guards (O_NOFOLLOW, identity, leaf resolution) own that case, the
        # same split ``_entry_identity`` applies.
        from kiro_crew import project_scan

        assert project_scan._entry_redirects(self._Entry(fail=True)) is False  # type: ignore[arg-type]


class TestWindowsJunctions:
    """Real directory junctions, on the platform that has them.

    These are the symlink containment tests' Windows counterparts: creating a
    junction needs NO elevation, so the skip reason shielding the symlink tests
    does not apply — this is coverage on exactly the platform where a
    symlink-only gate breaks.
    """

    @staticmethod
    def _junction(target: Path, link: Path) -> None:
        _winapi = pytest.importorskip("_winapi")
        _winapi.CreateJunction(str(target), str(link))

    @pytest.mark.skipif(not _IS_WINDOWS, reason="directory junctions exist only on Windows")
    def test_junction_directory_is_not_traversed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        _make(outside, "secret/package.json")
        root = tmp_path / "root"
        _make(root, "app/.git/")
        self._junction(outside, root / "junction")

        tree = scan(root)

        assert _relative_paths(tree, root) == ["app"]

    @pytest.mark.skipif(not _IS_WINDOWS, reason="directory junctions exist only on Windows")
    def test_junction_loop_back_to_the_root_terminates(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/")
        self._junction(tmp_path, tmp_path / "app" / "self")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    @pytest.mark.skipif(not _IS_WINDOWS, reason="directory junctions exist only on Windows")
    def test_a_junction_named_like_a_repo_marker_is_no_signal(self, tmp_path: Path) -> None:
        # A junction named ``.git`` must not manufacture a repository signal —
        # the same treatment a POSIX symlink named ``.git`` gets.
        elsewhere = tmp_path / "elsewhere"
        _make(elsewhere, "unrelated/")
        root = tmp_path / "root"
        _make(root, "app/package.json")
        self._junction(elsewhere, root / "app" / ".git")

        tree = scan(root)

        assert _by_path(tree)[str(root / "app")].signals == (manifest_signal("package.json"),)


class TestDepthCap:
    def test_directories_beyond_the_cap_are_not_reached(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/b/c/d/package.json")

        assert scan(tmp_path, depth_cap=3).candidates == ()

    def test_a_directory_at_exactly_the_cap_is_classified(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/b/c/package.json")

        assert _relative_paths(scan(tmp_path, depth_cap=3), tmp_path) == ["a/b/c"]

    def test_default_cap_reaches_five_levels_down(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/b/c/d/e/package.json", "a/b/c/d/e/f/package.json")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["a/b/c/d/e"]

    def test_a_zero_cap_yields_nothing(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/")

        assert scan(tmp_path, depth_cap=0).candidates == ()

    def test_the_cap_bounds_depth_not_candidate_count(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/.git/", "b/.git/", "c/.git/")

        assert _relative_paths(scan(tmp_path, depth_cap=1), tmp_path) == ["a", "b", "c"]


class TestDeterminismAndSafety:
    def test_two_scans_of_an_unchanged_tree_are_equal(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "zeta/.git/",
            "alpha/package.json",
            "alpha/packages/one/package.json",
            "alpha/packages/two/pyproject.toml",
            "middle/crates/engine/Cargo.toml",
            "middle/node_modules/dep/package.json",
        )

        first = scan(tmp_path)
        second = scan(tmp_path)

        assert first == second
        assert [candidate.path for candidate in first.candidates] == sorted(
            candidate.path for candidate in first.candidates
        )

    def test_scanning_writes_nothing(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "app/.git/",
            "app/package.json",
            "app/packages/ui/package.json",
            "app/node_modules/dep/package.json",
        )
        before = _snapshot(tmp_path)

        scan(tmp_path)

        assert _snapshot(tmp_path) == before

    def test_empty_root_is_an_empty_answer_not_an_error(self, tmp_path: Path) -> None:
        tree = scan(tmp_path)

        assert tree == CandidateTree(root=str(tmp_path))

    def test_missing_root_is_reported_as_a_warning(self, tmp_path: Path) -> None:
        tree = scan(tmp_path / "absent")

        assert tree.candidates == ()
        assert len(tree.warnings) == 1
        assert "absent" in tree.warnings[0]

    def test_relative_root_is_recorded_as_an_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make(tmp_path, "app/.git/")
        monkeypatch.chdir(tmp_path)

        tree = scan(Path("."))

        assert tree.root == str(tmp_path)
        assert tree_single(tree).path == str(tmp_path / "app")

    @pytest.mark.skipif(_IS_WINDOWS, reason="chmod does not remove directory read access")
    @pytest.mark.skipif(_IS_ROOT, reason="root reads an unreadable directory anyway")
    def test_unreadable_subtree_costs_a_warning_not_the_scan(self, tmp_path: Path) -> None:
        _make(tmp_path, "readable/.git/", "locked/inner/")
        locked = tmp_path / "locked"
        os.chmod(locked, 0o000)
        try:
            tree = scan(tmp_path)
        finally:
            # Owner-only: the walker ran as this process, so restoring traverse
            # for the owner is all tmp_path teardown needs. Granting the group
            # or world a bit here would be gratuitously permissive.
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- a directory needs its execute bit to be traversable at all, so the rule's suggested 0o644 cannot apply; 0o700 is the tightest mode that works, and this line is teardown restoring owner-only access so tmp_path cleanup can remove the tree, not the behaviour under test.  # noqa: E501
            os.chmod(locked, 0o700)

        assert _relative_paths(tree, tmp_path) == ["readable"]
        assert len(tree.warnings) == 1
        assert str(locked) in tree.warnings[0]


def tree_single(tree: CandidateTree) -> Candidate:
    """Return the tree's only candidate, asserting there is exactly one."""

    assert len(tree.candidates) == 1, [candidate.path for candidate in tree.candidates]
    return tree.candidates[0]

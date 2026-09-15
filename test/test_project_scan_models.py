"""Tests for the project scanner's data models and signal constants.

The models carry the invariants the rest of the feature is allowed to assume, so
they are pinned here rather than left to the walker's tests:

1. **Immutability.** A candidate is handed to preview, reconcile, and scaffold in
   turn; if any stage could mutate it, "two scans of an unchanged tree are equal"
   stops being checkable.
2. **Path-sorted order.** Directory iteration order is not guaranteed by the OS,
   so ordering has to come from the tree builder, not from the walk.
3. **Absolute paths.** A relative ``project_dir`` would resolve against the
   process CWD, so it is refused at construction.
4. **Prune precedence rule.** ``.kiro`` is the single hidden directory that
   survives the dot-directory rule; everything else hidden is pruned.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from kiro_crew.project_scan import (
    DEFAULT_DEPTH_CAP,
    GIT_DIR,
    KIRO_DIR,
    MANIFESTS,
    PRUNE_DIRS,
    SIGNAL_GIT,
    SIGNAL_KIRO,
    SIGNAL_MEMBER,
    Candidate,
    CandidateTree,
    Tier,
    is_pruned,
    manifest_signal,
    recognized_manifests,
    scan,
)

# Absolute paths in tests are built rather than written literally: on Windows a
# bare "/root/app" is not absolute, and the models reject a non-absolute path.
_ROOT = os.path.abspath(os.sep + "workspace")


def _abs(*parts: str) -> str:
    return os.path.join(_ROOT, *parts)


def _candidate(*parts: str, tier: Tier = Tier.AUTO) -> Candidate:
    path = _abs(*parts)
    return Candidate(path=path, name=parts[-1], parent_path=None, tier=tier)


class TestTier:
    def test_only_two_tiers_are_representable(self) -> None:
        # A directory with no signal is absent from the tree entirely; if
        # "ignored" were a member here a surface could render it by forgetting a
        # filter.
        assert [tier.value for tier in Tier] == ["auto", "offered"]


class TestCandidate:
    def test_is_frozen(self) -> None:
        candidate = _candidate("app")

        with pytest.raises(dataclasses.FrozenInstanceError):
            candidate.tier = Tier.OFFERED  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            candidate.path = _abs("other")  # type: ignore[misc]

    def test_signals_default_to_empty_tuple(self) -> None:
        assert _candidate("app").signals == ()

    def test_equal_candidates_compare_equal_and_hash_equal(self) -> None:
        # Equality and hashing are what let a test assert on a whole scan result
        # in one comparison, and what let reconcile use candidates in a set.
        first = Candidate(
            path=_abs("app"),
            name="app",
            parent_path=_ROOT,
            tier=Tier.OFFERED,
            signals=(SIGNAL_MEMBER,),
        )
        second = Candidate(
            path=_abs("app"),
            name="app",
            parent_path=_ROOT,
            tier=Tier.OFFERED,
            signals=(SIGNAL_MEMBER,),
        )

        assert first == second
        assert len({first, second}) == 1

    def test_replace_produces_a_new_candidate(self) -> None:
        original = _candidate("app")

        promoted = dataclasses.replace(original, tier=Tier.OFFERED)

        assert promoted.tier is Tier.OFFERED
        assert original.tier is Tier.AUTO

    def test_relative_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            Candidate(path="app", name="app", parent_path=None, tier=Tier.AUTO)

    def test_relative_parent_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="parent_path must be absolute"):
            Candidate(path=_abs("app"), name="app", parent_path="..", tier=Tier.AUTO)

    def test_parent_path_none_means_hanging_off_the_scan_root(self) -> None:
        assert _candidate("app").parent_path is None


class TestCandidateTree:
    def test_is_frozen(self) -> None:
        tree = CandidateTree.build(_ROOT, [_candidate("app")])

        with pytest.raises(dataclasses.FrozenInstanceError):
            tree.root = _abs("elsewhere")  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            tree.candidates = ()  # type: ignore[misc]

    def test_build_sorts_candidates_by_path(self) -> None:
        unsorted = [
            _candidate("zeta"),
            _candidate("alpha"),
            _candidate("mid", "nested"),
            _candidate("mid"),
        ]

        tree = CandidateTree.build(_ROOT, unsorted)

        assert [candidate.path for candidate in tree.candidates] == [
            _abs("alpha"),
            _abs("mid"),
            _abs("mid", "nested"),
            _abs("zeta"),
        ]

    def test_build_ordering_does_not_depend_on_input_order(self) -> None:
        candidates = [_candidate("b"), _candidate("a"), _candidate("c")]

        forward = CandidateTree.build(_ROOT, candidates)
        reversed_ = CandidateTree.build(_ROOT, list(reversed(candidates)))

        assert forward == reversed_

    def test_build_accepts_any_iterable_and_stores_tuples(self) -> None:
        tree = CandidateTree.build(
            _ROOT,
            (candidate for candidate in [_candidate("app")]),
            (warning for warning in ["skipped one declaration"]),
        )

        assert isinstance(tree.candidates, tuple)
        assert isinstance(tree.warnings, tuple)

    def test_build_preserves_warning_order(self) -> None:
        # Warnings are emitted by an already-deterministic walk, and their
        # sequence is the order a user reads them in — so they are not re-sorted.
        tree = CandidateTree.build(_ROOT, [], ["second is not first", "and first was first"])

        assert tree.warnings == ("second is not first", "and first was first")

    def test_empty_tree_is_a_valid_result(self) -> None:
        tree = CandidateTree.build(_ROOT, [])

        assert tree.root == _ROOT
        assert tree.candidates == ()
        assert tree.warnings == ()


class TestSignalConstants:
    def test_manifests_cover_the_documented_ecosystems(self) -> None:
        assert MANIFESTS == (
            "package.json",
            "pyproject.toml",
            "Cargo.toml",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "build.sbt",
            "pubspec.yaml",
            "composer.json",
            "Gemfile",
            "mix.exs",
            "Package.swift",
            "deno.json",
            "deno.jsonc",
            "firebase.json",
            "vercel.json",
            "netlify.toml",
            "amplify.yml",
            "serverless.yml",
            "serverless.yaml",
            "cdk.json",
            "wrangler.toml",
            "wrangler.jsonc",
            "fly.toml",
            "render.yaml",
            "Procfile",
        )

    def test_the_manifest_set_is_the_builtin_tuple(self) -> None:
        assert recognized_manifests() == frozenset(MANIFESTS)

    def test_manifest_signal_names_the_file_found(self) -> None:
        assert manifest_signal("package.json") == "manifest:package.json"

    def test_signal_names_are_distinct(self) -> None:
        names = {SIGNAL_GIT, SIGNAL_KIRO, SIGNAL_MEMBER, manifest_signal("go.mod")}

        assert len(names) == 4

    def test_boundary_directory_names(self) -> None:
        assert (GIT_DIR, KIRO_DIR) == (".git", ".kiro")

    def test_default_depth_cap(self) -> None:
        assert DEFAULT_DEPTH_CAP == 5


class TestIsPruned:
    @pytest.mark.parametrize("name", PRUNE_DIRS)
    def test_listed_directories_are_pruned(self, name: str) -> None:
        assert is_pruned(name)

    def test_dependency_build_and_virtualenv_names_are_all_covered(self) -> None:
        # Pinned independently of PRUNE_DIRS: this is the minimum set a scan must
        # never traverse, so shrinking the constant has to fail a test that does
        # not read the constant.
        for name in (
            "node_modules",
            "dist",
            "build",
            "target",
            "env",
            "venv",
            ".venv",
            "__pycache__",
        ):
            assert is_pruned(name), name

    @pytest.mark.parametrize("name", [".git", ".tox", ".mypy_cache", ".idea", ".hidden"])
    def test_dot_directories_are_pruned(self, name: str) -> None:
        assert is_pruned(name)

    def test_kiro_directory_survives_the_dot_rule(self) -> None:
        # ``.kiro`` is the one hidden directory carrying a detection signal, so
        # the dot rule has to make an exception for it.
        assert not is_pruned(KIRO_DIR)

    @pytest.mark.parametrize("name", ["packages", "app", "crates", "kiro", "buildsrc"])
    def test_ordinary_directories_are_not_pruned(self, name: str) -> None:
        assert not is_pruned(name)


class TestScanSignature:
    def test_scan_returns_an_empty_tree_for_an_empty_root(self, tmp_path: Path) -> None:
        # Signature smoke test only — traversal and classification behavior lives
        # in the walker's own suite.
        tree = scan(tmp_path, depth_cap=2)

        assert tree == CandidateTree(root=str(tmp_path))

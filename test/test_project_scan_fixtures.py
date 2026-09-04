"""Whole-tree scans of layouts shaped like the projects this feature exists for.

The sibling modules pin one rule at a time. This one points :func:`scan` at
complete trees — a directory of unrelated checkouts, a JS monorepo, a polyglot
monorepo declaring members in all four supported formats — and asserts the entire
answer: which directories are offered, in which tier, for which reason, and under
which parent.

The two kinds of test catch different bugs. A rule test says a nested manifest is
offered unticked. A whole-tree test catches the interactions between rules: an
exclusion that hides a package carrying a signal of its own, a member list that
re-offers a pruned dependency tree, a declaration read from a directory that is
not itself a package, a candidate whose parent only exists because a member list
named it. None of those are visible until several rules apply to one tree.

Layouts are written out as the trees they represent, junk directories and all,
because the mistakes this code can make are layout-shaped: a mocked filesystem
would hide the symlink, the pruned name, and the iteration order that are the
point.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.project_scan import (
    MAX_DECLARATION_BYTES,
    SIGNAL_GIT,
    SIGNAL_KIRO,
    SIGNAL_MEMBER,
    CandidateTree,
    Tier,
    manifest_signal,
    scan,
)

_IS_WINDOWS = sys.platform == "win32"

_PKG_JSON = manifest_signal("package.json")
_PYPROJECT = manifest_signal("pyproject.toml")
_CARGO = manifest_signal("Cargo.toml")
_GO_MOD = manifest_signal("go.mod")
_GRADLE = manifest_signal("build.gradle")


def _make(root: Path, *layout: str) -> None:
    """Create the given layout under ``root``.

    An entry ending in ``/`` is a directory; anything else is an empty file whose
    parent directories are created for it. Keeps a fixture readable as the tree it
    represents.
    """

    for entry in layout:
        target = root / entry
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")


def _write(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, creating parents. Returns the path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pkg(path: Path, name: str, workspaces: object | None = None) -> Path:
    """Write a realistic ``package.json``, optionally declaring workspaces."""

    payload: dict[str, object] = {"name": name, "version": "1.0.0"}
    if workspaces is not None:
        payload["private"] = True
        payload["workspaces"] = workspaces
    return _write(path, json.dumps(payload, indent=2) + "\n")


def _rel(path: str, root: Path) -> str:
    return Path(path).relative_to(root).as_posix()


def _paths(tree: CandidateTree, root: Path) -> list[str]:
    """Candidate paths relative to ``root``, in the order the tree reports them."""

    return [_rel(candidate.path, root) for candidate in tree.candidates]


def _tiers(tree: CandidateTree, root: Path) -> dict[str, Tier]:
    return {_rel(candidate.path, root): candidate.tier for candidate in tree.candidates}


def _signals(tree: CandidateTree, root: Path) -> dict[str, tuple[str, ...]]:
    return {_rel(candidate.path, root): candidate.signals for candidate in tree.candidates}


def _parents(tree: CandidateTree, root: Path) -> dict[str, str | None]:
    """Each candidate's parent, relative to ``root``; ``None`` means the root."""

    return {
        _rel(candidate.path, root): (
            None if candidate.parent_path is None else _rel(candidate.parent_path, root)
        )
        for candidate in tree.candidates
    }


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


@pytest.fixture
def sibling_repos(tmp_path: Path) -> Path:
    """A plain directory of unrelated checkouts, with the junk each one carries.

    The scan root has no signal of its own, so every checkout under it is the
    package the user pointed at rather than a detail nested inside one.
    """

    root = tmp_path / "projects"
    _make(
        root,
        "api/.git/HEAD",
        "api/src/service/__pycache__/handler.cpython-312.pyc",
        "api/.venv/lib/python3.12/site-packages/attrs/pyproject.toml",
        # The undotted spelling of the same thing, and the one a real scan turned
        # up dozens of false candidates from: an environment installs one
        # third-party manifest per package, so a single tree offers more junk
        # than the checkout around it holds real packages.
        "api/env/lib/python3.12/site-packages/click/pyproject.toml",
        "api/env/src/vendored-tool/.git/HEAD",
        "web/.git/HEAD",
        "web/dist/package.json",
        "web/node_modules/left-pad/package.json",
        "web/node_modules/.bin/tsc",
        "notes/todo.md",
        "archive/2019/receipts/summary.txt",
    )
    _write(root / "api" / "pyproject.toml", '[project]\nname = "api"\n')
    _pkg(root / "web" / "package.json", "web")
    return root


@pytest.fixture
def npm_monorepo(tmp_path: Path) -> Path:
    """A JS monorepo: declared members, a nested ``.kiro``, junk, an escape attempt.

    The out-of-root member and the exclusion are both things the project's own
    ``package.json`` asks for, which is what makes them worth combining with the
    rest: one must be refused, the other honoured.
    """

    root = tmp_path / "shop"
    _make(tmp_path, "escape/leak/package.json")
    _pkg(
        root / "package.json",
        "shop",
        workspaces=["packages/*", "tools/cli", "../escape/*", "!packages/legacy"],
    )
    _make(
        root,
        ".git/HEAD",
        "docs/architecture.md",
        "node_modules/.bin/tsc",
        "node_modules/typescript/package.json",
        # A build directory carrying both a manifest and a .kiro: prune has to win
        # over each of them separately.
        "packages/api/build/.kiro/steering/leak.md",
        "packages/api/build/package.json",
        "packages/api/.kiro/steering/api.md",
        "packages/shared/src/index.ts",
        "packages/web/node_modules/react/package.json",
    )
    _pkg(root / "packages" / "api" / "package.json", "@shop/api")
    _pkg(root / "packages" / "web" / "package.json", "@shop/web")
    _pkg(root / "packages" / "legacy" / "package.json", "@shop/legacy")
    _pkg(root / "tools" / "cli" / "package.json", "@shop/cli")
    _write(root / "packages" / "web" / "e2e" / "go.mod", "module shop/e2e\n\ngo 1.22\n")
    return root


@pytest.fixture
def polyglot_monorepo(tmp_path: Path) -> Path:
    """One tree declaring members in all four supported formats.

    Also covers the two shapes a declaring directory can take: ``rust`` and
    ``tools`` declare members in their own manifest, while the root's
    ``pnpm-workspace.yaml`` and ``services/go.work`` name members from directories
    that are not packages themselves.
    """

    root = tmp_path / "platform"
    _write(root / "pnpm-workspace.yaml", "packages:\n  - 'frontend/*'\n")
    _write(root / "rust" / "Cargo.toml", '[workspace]\nmembers = [\n  "crates/*",\n]\n')
    _write(root / "services" / "go.work", "go 1.22\n\nuse (\n\t./api\n\t./worker\n)\n")
    _pkg(root / "tools" / "package.json", "platform-tools", workspaces={"packages": ["cli"]})
    _make(
        root,
        "frontend/web/src/main.ts",
        "rust/crates/engine/.kiro/steering/engine.md",
        "rust/target/debug/build/probe/Cargo.toml",
        "services/worker/main.go",
        "tools/cli/bin/run.js",
    )
    _pkg(root / "frontend" / "admin" / "package.json", "@platform/admin")
    _write(root / "rust" / "crates" / "engine" / "Cargo.toml", '[package]\nname = "engine"\n')
    _write(root / "rust" / "crates" / "cli" / "Cargo.toml", '[package]\nname = "cli"\n')
    _write(root / "services" / "api" / "go.mod", "module platform/api\n\ngo 1.22\n")
    return root


@pytest.fixture
def broken_declarations(tmp_path: Path) -> Path:
    """A tree where three declarations cannot be understood and one can."""

    root = tmp_path / "tangle"
    _write(root / "package.json", "{ not json\n")
    _write(root / "apps" / "web" / "pnpm-workspace.yaml", "packages: [unclosed\n")
    # Too large to be a hand-written member list, so it is refused rather than
    # parsed from a truncated prefix.
    _write(root / "vendor" / "sdk" / "Cargo.toml", " " * (MAX_DECLARATION_BYTES + 1))
    _pkg(root / "apps" / "api" / "package.json", "api", workspaces=["libs/*"])
    _pkg(root / "apps" / "web" / "package.json", "web")
    _make(root, "apps/api/libs/core/src/index.ts")
    return root


@pytest.fixture
def deep_tree(tmp_path: Path) -> Path:
    """A service tree deep enough for the cap to decide what is reachable.

    The ``go.work`` names a member six levels down, so the cap has to bound member
    expansion as well as the walk — otherwise a member list is a way past it.
    """

    root = tmp_path / "deep"
    _write(
        root / "services" / "go.work",
        "go 1.22\n\nuse ./api/internal/handlers/v2/pkg\n",
    )
    _write(root / "services" / "api" / "go.mod", "module deep/api\n\ngo 1.22\n")
    _write(
        root / "services" / "api" / "internal" / "handlers" / "v2" / "pkg" / "go.mod",
        "module deep/api/pkg\n\ngo 1.22\n",
    )
    _write(
        root / "services" / "api" / "internal" / "tools" / "gen" / "build.gradle",
        "plugins { id 'java' }\n",
    )
    return root


class TestDirectoryOfCheckouts:
    def test_every_checkout_is_offered_and_nothing_else_is(self, sibling_repos: Path) -> None:
        tree = scan(sibling_repos)

        assert _tiers(tree, sibling_repos) == {"api": Tier.AUTO, "web": Tier.AUTO}
        assert _signals(tree, sibling_repos) == {
            "api": (SIGNAL_GIT, _PYPROJECT),
            "web": (SIGNAL_GIT, _PKG_JSON),
        }
        # No checkout is inside another, so each hangs off the scan root.
        assert _parents(tree, sibling_repos) == {"api": None, "web": None}
        assert tree.warnings == ()

    def test_dependency_and_environment_directories_contribute_nothing(
        self, sibling_repos: Path
    ) -> None:
        # Each of these holds a manifest that would otherwise match: pruning is
        # what keeps a scan of a working checkout from offering its dependencies.
        # ``env`` additionally holds a vendored checkout with its own ``.git``,
        # which is the AUTO tier's strongest signal — so pruning has to win over
        # the tier that is never otherwise downgraded.
        pruned = ("node_modules", "dist", "env", ".venv", "__pycache__")

        for candidate in scan(sibling_repos).candidates:
            segments = Path(candidate.path).parts
            assert not [segment for segment in segments if segment in pruned]

    def test_scanning_the_tree_changes_nothing_on_disk(self, sibling_repos: Path) -> None:
        before = _snapshot(sibling_repos)

        scan(sibling_repos)

        assert _snapshot(sibling_repos) == before


class TestNpmMonorepo:
    def test_the_whole_tree_resolves_to_one_expected_answer(self, npm_monorepo: Path) -> None:
        tree = scan(npm_monorepo)

        assert _paths(tree, npm_monorepo) == [
            "packages/api",
            "packages/legacy",
            "packages/shared",
            "packages/web",
            "packages/web/e2e",
            "tools/cli",
        ]
        assert _tiers(tree, npm_monorepo) == {
            # Its own .kiro outranks the nesting that demoted its siblings.
            "packages/api": Tier.AUTO,
            "packages/legacy": Tier.OFFERED,
            "packages/shared": Tier.OFFERED,
            "packages/web": Tier.OFFERED,
            "packages/web/e2e": Tier.OFFERED,
            "tools/cli": Tier.OFFERED,
        }
        assert tree.warnings == ()

    def test_each_candidate_records_why_it_was_detected(self, npm_monorepo: Path) -> None:
        # A member that also carries a manifest keeps both reasons, so a preview
        # can explain an unticked candidate that the project itself declared.
        assert _signals(scan(npm_monorepo), npm_monorepo) == {
            "packages/api": (SIGNAL_KIRO, _PKG_JSON, SIGNAL_MEMBER),
            "packages/legacy": (_PKG_JSON,),
            "packages/shared": (SIGNAL_MEMBER,),
            "packages/web": (_PKG_JSON, SIGNAL_MEMBER),
            "packages/web/e2e": (_GO_MOD,),
            "tools/cli": (_PKG_JSON, SIGNAL_MEMBER),
        }

    def test_nesting_follows_detected_packages_not_directories(self, npm_monorepo: Path) -> None:
        # "packages" and "tools" carry no signal, so they are nobody's parent: the
        # folder tree mirrors the packages found, not the directories walked.
        assert _parents(scan(npm_monorepo), npm_monorepo) == {
            "packages/api": None,
            "packages/legacy": None,
            "packages/shared": None,
            "packages/web": None,
            "packages/web/e2e": "packages/web",
            "tools/cli": None,
        }

    def test_an_excluded_member_with_its_own_manifest_is_still_offered(
        self, npm_monorepo: Path
    ) -> None:
        # The exclusion only withdraws the member list's claim. The directory is
        # still a package by its own manifest, so it stays offered — without the
        # member signal, which is the part the project retracted.
        legacy = _signals(scan(npm_monorepo), npm_monorepo)["packages/legacy"]

        assert SIGNAL_MEMBER not in legacy

    def test_a_member_pattern_cannot_reach_outside_the_scan_root(self, npm_monorepo: Path) -> None:
        # "../escape/*" matches a real package that exists on disk; containment is
        # the reason it is absent rather than the directory being empty.
        escape = npm_monorepo.parent / "escape" / "leak"
        assert escape.is_dir()

        for candidate in scan(npm_monorepo).candidates:
            assert candidate.path.startswith(str(npm_monorepo) + os.sep)

    def test_two_scans_of_the_monorepo_are_equal(self, npm_monorepo: Path) -> None:
        assert scan(npm_monorepo) == scan(npm_monorepo)


class TestPolyglotMonorepo:
    def test_members_from_all_four_declaration_formats_are_offered(
        self, polyglot_monorepo: Path
    ) -> None:
        tree = scan(polyglot_monorepo)

        assert _paths(tree, polyglot_monorepo) == [
            "frontend/admin",
            "frontend/web",
            "rust",
            "rust/crates/cli",
            "rust/crates/engine",
            "services/api",
            "services/worker",
            "tools",
            "tools/cli",
        ]
        # Every candidate below carries a member signal from a different format:
        # pnpm-workspace.yaml (frontend), Cargo (crates), go.work (services), and
        # yarn's object-shaped workspaces (tools).
        assert _signals(tree, polyglot_monorepo) == {
            "frontend/admin": (_PKG_JSON, SIGNAL_MEMBER),
            "frontend/web": (SIGNAL_MEMBER,),
            "rust": (_CARGO,),
            "rust/crates/cli": (_CARGO, SIGNAL_MEMBER),
            "rust/crates/engine": (SIGNAL_KIRO, _CARGO, SIGNAL_MEMBER),
            "services/api": (_GO_MOD, SIGNAL_MEMBER),
            "services/worker": (SIGNAL_MEMBER,),
            "tools": (_PKG_JSON,),
            "tools/cli": (SIGNAL_MEMBER,),
        }
        assert tree.warnings == ()

    def test_position_decides_the_tier_of_a_manifest(self, polyglot_monorepo: Path) -> None:
        # "frontend" and "services" carry no signal, so a manifest below them names
        # the package itself and is ticked. Inside "rust", the same kind of manifest
        # may be an implementation detail, so it is only offered — unless the
        # directory carries a .kiro, which the user created by treating it as a
        # project root.
        assert _tiers(scan(polyglot_monorepo), polyglot_monorepo) == {
            "frontend/admin": Tier.AUTO,
            "frontend/web": Tier.OFFERED,
            "rust": Tier.AUTO,
            "rust/crates/cli": Tier.OFFERED,
            "rust/crates/engine": Tier.AUTO,
            "services/api": Tier.AUTO,
            "services/worker": Tier.OFFERED,
            "tools": Tier.AUTO,
            "tools/cli": Tier.OFFERED,
        }

    def test_members_nest_under_the_package_that_declared_them(
        self, polyglot_monorepo: Path
    ) -> None:
        assert _parents(scan(polyglot_monorepo), polyglot_monorepo) == {
            "frontend/admin": None,
            "frontend/web": None,
            "rust": None,
            "rust/crates/cli": "rust",
            "rust/crates/engine": "rust",
            # "services" declares members in a go.work but is not a package, so its
            # members hang off the scan root.
            "services/api": None,
            "services/worker": None,
            "tools": None,
            "tools/cli": "tools",
        }

    def test_build_output_is_pruned_even_inside_a_declared_workspace(
        self, polyglot_monorepo: Path
    ) -> None:
        assert (polyglot_monorepo / "rust" / "target" / "debug" / "build" / "probe").is_dir()

        assert "rust/target" not in _paths(scan(polyglot_monorepo), polyglot_monorepo)

    def test_reading_four_declaration_formats_writes_nothing(self, polyglot_monorepo: Path) -> None:
        before = _snapshot(polyglot_monorepo)

        scan(polyglot_monorepo)

        assert _snapshot(polyglot_monorepo) == before

    def test_two_scans_of_the_polyglot_tree_are_equal(self, polyglot_monorepo: Path) -> None:
        assert scan(polyglot_monorepo) == scan(polyglot_monorepo)


class TestMalformedDeclarations:
    def test_packages_are_still_found_around_the_broken_declarations(
        self, broken_declarations: Path
    ) -> None:
        tree = scan(broken_declarations)

        # The root's package.json cannot be parsed, yet its name still signals a
        # manifest — so the root is a package and everything below it is nested.
        assert _tiers(tree, broken_declarations) == {
            "apps/api": Tier.OFFERED,
            "apps/api/libs/core": Tier.OFFERED,
            "apps/web": Tier.OFFERED,
            "vendor/sdk": Tier.OFFERED,
        }
        # The one readable declaration still produced its member.
        assert _signals(tree, broken_declarations)["apps/api/libs/core"] == (SIGNAL_MEMBER,)
        assert _parents(tree, broken_declarations)["apps/api/libs/core"] == "apps/api"

    def test_each_unusable_declaration_costs_exactly_one_warning(
        self, broken_declarations: Path
    ) -> None:
        warnings = scan(broken_declarations).warnings

        # Warnings are reported in the order the walk produced them, which is the
        # order a user reads the tree in.
        assert [warning.split(": ", 1)[0] for warning in warnings] == [
            f"skipped workspace declaration {broken_declarations / 'package.json'}",
            f"skipped workspace declaration "
            f"{broken_declarations / 'apps' / 'web' / 'pnpm-workspace.yaml'}",
            f"skipped workspace declaration "
            f"{broken_declarations / 'vendor' / 'sdk' / 'Cargo.toml'}",
        ]
        # A reason is rendered in a preview, so it stays short and on one line.
        for warning in warnings:
            assert "\n" not in warning
            assert len(warning) < 400

    def test_a_tree_with_broken_declarations_still_scans_identically_twice(
        self, broken_declarations: Path
    ) -> None:
        assert scan(broken_declarations) == scan(broken_declarations)


class TestDepthCap:
    def test_the_default_cap_reaches_five_levels_and_no_further(self, deep_tree: Path) -> None:
        tree = scan(deep_tree)

        # "gen" sits at exactly five levels and is classified; the package six
        # levels down is never reached.
        assert _paths(tree, deep_tree) == [
            "services/api",
            "services/api/internal/tools/gen",
        ]
        assert _tiers(tree, deep_tree)["services/api/internal/tools/gen"] is Tier.OFFERED
        assert _signals(tree, deep_tree)["services/api/internal/tools/gen"] == (_GRADLE,)

    def test_a_declared_member_past_the_cap_is_not_offered(self, deep_tree: Path) -> None:
        # The go.work names it and it exists on disk, so the cap is the only reason
        # it is absent — a member list must not be a way past the bound.
        declared = deep_tree / "services" / "api" / "internal" / "handlers" / "v2" / "pkg"
        assert declared.is_dir()

        assert _rel(str(declared), deep_tree) not in _paths(scan(deep_tree), deep_tree)

    def test_raising_the_cap_reveals_the_member_the_default_hid(self, deep_tree: Path) -> None:
        tree = scan(deep_tree, depth_cap=6)

        assert _paths(tree, deep_tree) == [
            "services/api",
            "services/api/internal/handlers/v2/pkg",
            "services/api/internal/tools/gen",
        ]
        deep = _signals(tree, deep_tree)["services/api/internal/handlers/v2/pkg"]
        assert deep == (_GO_MOD, SIGNAL_MEMBER)

    def test_a_tight_cap_leaves_only_what_is_shallow_enough(self, deep_tree: Path) -> None:
        assert _paths(scan(deep_tree, depth_cap=2), deep_tree) == ["services/api"]


class TestNothingToOffer:
    def test_a_tree_of_documents_yields_an_empty_answer(self, tmp_path: Path) -> None:
        root = tmp_path / "handbook"
        _make(
            root,
            "docs/onboarding.md",
            "docs/images/diagram.png",
            "archive/2019/notes.txt",
        )

        # An empty result is an answer, not an error, and not a warning either:
        # nothing failed, there is simply no package here.
        assert scan(root) == CandidateTree(root=str(root))

    def test_an_empty_root_and_a_signal_free_root_agree(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        filled = tmp_path / "filled"
        _make(filled, "one/two/three/four/notes.txt")

        assert scan(empty).candidates == scan(filled).candidates == ()


@pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
class TestSymlinkedTree:
    @pytest.fixture
    def linked_root(self, tmp_path: Path) -> Path:
        """A workspace whose member patterns match two symlinks out of the root."""

        outside = tmp_path / "outside"
        _make(outside, "pkg/.kiro/steering/pkg.md")
        _pkg(outside / "pkg" / "package.json", "outsider")

        root = tmp_path / "linked"
        _pkg(root / "package.json", "linked", workspaces=["packages/*", "vendor"])
        _pkg(root / "packages" / "real" / "package.json", "@linked/real")
        (root / "packages" / "mirror").symlink_to(outside / "pkg", target_is_directory=True)
        (root / "vendor").symlink_to(outside, target_is_directory=True)
        return root

    def test_only_the_real_package_is_offered(self, linked_root: Path) -> None:
        tree = scan(linked_root)

        # Both links resolve to a directory that would otherwise be detected — the
        # linked one even carries a .kiro. Neither is traversed, so neither is
        # offered, and no warning is needed because nothing failed.
        assert _paths(tree, linked_root) == ["packages/real"]
        assert tree.warnings == ()

    def test_no_candidate_path_leaves_the_scan_root(self, linked_root: Path) -> None:
        for candidate in scan(linked_root).candidates:
            assert candidate.path.startswith(str(linked_root) + os.sep)

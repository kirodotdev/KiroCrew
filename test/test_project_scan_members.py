"""Tests for workspace declarations and the member candidates they produce.

A member list is the one input to the scan that comes from a file rather than
from the shape of the tree, which makes it the one place a project can ask for
something the scan must refuse. So these tests pin both halves:

1. **Parsing.** Each of the four formats is read in the spellings real projects
   use, a file that merely lacks a member list is not an error, and a file that
   cannot be understood costs a warning rather than the scan.
2. **Resolution.** Patterns expand relative to the package that declared them,
   a member is offered unticked unless it already earned a stronger tier, and a
   member that would escape the root, sit under a pruned directory, cross a
   symlink, or fall past the depth cap never becomes a candidate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.project_scan import (
    MAX_DECLARATION_BYTES,
    Candidate,
    CandidateTree,
    DeclarationError,
    Tier,
    _scan_cargo_members,
    _warning_reason,
    declared_patterns,
    manifest_signal,
    scan,
)

_IS_WINDOWS = sys.platform == "win32"


def _write(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, creating parents. Returns the path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make(root: Path, *layout: str) -> None:
    """Create the given layout under ``root``.

    An entry ending in ``/`` is a directory; anything else is an empty file.
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
    return [Path(candidate.path).relative_to(root).as_posix() for candidate in tree.candidates]


def _npm_root(root: Path, *patterns: str) -> None:
    """Write a workspace-declaring ``package.json`` at ``root``."""

    _write(root / "package.json", json.dumps({"name": "root", "workspaces": list(patterns)}))


class TestDeclarationParsing:
    def test_npm_array_form(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "package.json", '{"workspaces": ["packages/*", "tools/cli"]}')

        assert declared_patterns(str(path), str(tmp_path)) == ["packages/*", "tools/cli"]

    def test_yarn_object_form(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "package.json",
            '{"workspaces": {"packages": ["packages/*"], "nohoist": ["**/react"]}}',
        )

        assert declared_patterns(str(path), str(tmp_path)) == ["packages/*"]

    def test_package_json_without_workspaces_declares_nothing(self, tmp_path: Path) -> None:
        # The common case by far: a manifest is a manifest first, so its absence
        # of a member list must not read as a broken declaration.
        path = _write(tmp_path / "package.json", '{"name": "app", "version": "1.0.0"}')

        assert declared_patterns(str(path), str(tmp_path)) == []

    def test_pnpm_block_and_flow_yaml(self, tmp_path: Path) -> None:
        block = _write(
            tmp_path / "block" / "pnpm-workspace.yaml",
            "packages:\n  - 'packages/*'\n  - apps/web\n",
        )
        flow = _write(
            tmp_path / "flow" / "pnpm-workspace.yaml", 'packages: ["packages/*", "apps/web"]\n'
        )

        assert declared_patterns(str(block), str(tmp_path)) == ["packages/*", "apps/web"]
        assert declared_patterns(str(flow), str(tmp_path)) == ["packages/*", "apps/web"]

    def test_pnpm_yaml_aliases_are_refused(self, tmp_path: Path) -> None:
        # An alias lets a small file compose a much larger structure, and no real
        # member list needs one.
        path = _write(
            tmp_path / "pnpm-workspace.yaml",
            "base: &base\n  - packages/*\npackages: *base\n",
        )

        with pytest.raises(DeclarationError):
            declared_patterns(str(path), str(tmp_path))

    def test_cargo_members_across_lines_with_comments(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "Cargo.toml",
            "[package]\nname = 'app'\n\n"
            "[workspace]\nresolver = '2'\n"
            "members = [\n  'crates/*',  # the libraries\n  'tools/cli',\n]\n"
            "exclude = ['crates/legacy']\n",
        )

        assert declared_patterns(str(path), str(tmp_path)) == ["crates/*", "tools/cli"]

    def test_cargo_without_a_workspace_table_declares_nothing(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "Cargo.toml", "[package]\nname = 'app'\n")

        assert declared_patterns(str(path), str(tmp_path)) == []

    def test_go_work_use_line_and_block(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "go.work",
            'go 1.22\n\nuse ./api\n\nuse (\n\t./web\n\t"./cmd/tool"\n)\n\n'
            "replace example.com/x => ./vendored\n",
        )

        assert declared_patterns(str(path), str(tmp_path)) == ["./api", "./web", "./cmd/tool"]

    def test_go_work_comments_are_ignored(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "go.work", "// used to be ./old\nuse ./api // keep\n")

        assert declared_patterns(str(path), str(tmp_path)) == ["./api"]

    def test_a_non_string_member_costs_only_that_entry(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "package.json", '{"workspaces": ["packages/*", 7, "", "apps/*"]}')

        assert declared_patterns(str(path), str(tmp_path)) == ["packages/*", "apps/*"]

    def test_a_member_list_of_the_wrong_type_is_a_parse_failure(self, tmp_path: Path) -> None:
        # A bare string would otherwise be iterated one character at a time.
        path = _write(tmp_path / "package.json", '{"workspaces": "packages/*"}')

        with pytest.raises(DeclarationError):
            declared_patterns(str(path), str(tmp_path))

    def test_invalid_json_is_a_parse_failure(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "package.json", "{not json")

        with pytest.raises(DeclarationError):
            declared_patterns(str(path), str(tmp_path))

    def test_an_oversized_declaration_is_refused_rather_than_truncated(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "package.json", " " * (MAX_DECLARATION_BYTES + 1))

        with pytest.raises(DeclarationError):
            declared_patterns(str(path), str(tmp_path))


class TestCargoFallbackParser:
    """The parser used where the interpreter has no TOML module.

    Exercised directly rather than through :func:`declared_patterns`, because on
    an interpreter that ships ``tomllib`` the fallback branch is unreachable —
    and it is the branch that runs for every Cargo workspace on the oldest
    interpreter the project supports.
    """

    def test_it_agrees_with_a_real_parser_on_what_cargo_writes(self, tmp_path: Path) -> None:
        text = (
            "[package]\nname = 'app'\nmembers = ['not-a-workspace-member']\n\n"
            "[workspace]\nresolver = '2'\n"
            'members = [\n  "crates/*",  # libraries\n  "tools/cli",\n]\n'
            "exclude = ['crates/legacy']\n\n"
            "[workspace.dependencies]\nserde = '1'\n"
        )
        path = _write(tmp_path / "Cargo.toml", text)

        assert _scan_cargo_members(text) == ["crates/*", "tools/cli"]
        assert _scan_cargo_members(text) == declared_patterns(str(path), str(tmp_path))

    def test_a_single_line_array_is_read(self) -> None:
        assert _scan_cargo_members("[workspace]\nmembers = ['a', 'b']\n") == ["a", "b"]

    def test_a_manifest_without_a_workspace_table_yields_nothing(self) -> None:
        assert _scan_cargo_members("[package]\nname = 'app'\n") == []


class TestMemberCandidates:
    def test_npm_glob_members_are_offered_unticked(self, tmp_path: Path) -> None:
        # "packages/api" carries nothing of its own; the root's member list is the
        # only reason it is known at all.
        _npm_root(tmp_path, "packages/*")
        _make(tmp_path, "packages/api/src/", "packages/web/src/", "docs/")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/api", "packages/web"]
        assert {candidate.tier for candidate in tree.candidates} == {Tier.OFFERED}
        assert {candidate.signals for candidate in tree.candidates} == {("member",)}

    def test_a_literal_member_is_offered(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "tools/cli")
        _make(tmp_path, "tools/cli/src/", "tools/other/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["tools/cli"]

    def test_pnpm_declaration_without_its_own_manifest_still_names_members(
        self, tmp_path: Path
    ) -> None:
        # A pnpm workspace file carries no manifest meaning, so the root is not a
        # package — but what it lists is still what the user wants offered.
        _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
        _make(tmp_path, "packages/ui/", "packages/api/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["packages/api", "packages/ui"]

    def test_cargo_members_are_offered(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "Cargo.toml",
            "[workspace]\nmembers = ['crates/*']\n",
        )
        _make(tmp_path, "crates/engine/src/", "crates/cli/src/")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["crates/cli", "crates/engine"]
        assert {candidate.tier for candidate in tree.candidates} == {Tier.OFFERED}

    def test_go_work_uses_are_offered(self, tmp_path: Path) -> None:
        _write(tmp_path / "go.work", "go 1.22\n\nuse (\n\t./api\n\t./web\n)\n")
        _make(tmp_path, "api/", "web/", "scripts/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["api", "web"]

    def test_a_double_star_pattern_reaches_every_level(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/**")
        _make(tmp_path, "packages/group/inner/", "packages/solo/")

        # ``**`` matches zero levels too, so the container itself is offered — one
        # extra unticked candidate rather than a second rule for a trailing ``**``.
        assert _relative_paths(scan(tmp_path), tmp_path) == [
            "packages",
            "packages/group",
            "packages/group/inner",
            "packages/solo",
        ]

    def test_a_middle_double_star_matches_zero_levels(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/**/tests")
        _make(tmp_path, "packages/tests/", "packages/ui/tests/", "packages/ui/src/")

        assert _relative_paths(scan(tmp_path), tmp_path) == [
            "packages/tests",
            "packages/ui/tests",
        ]

    def test_an_empty_manifest_declares_nothing_without_warning(self, tmp_path: Path) -> None:
        # A placeholder manifest is common; calling it a malformed member list
        # would fill the preview with warnings about files that never were one.
        _make(tmp_path, "packages/api/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/api"]
        assert tree.warnings == ()

    def test_an_excluded_member_is_not_offered(self, tmp_path: Path) -> None:
        # The project has already said this directory is not a member; offering it
        # anyway would contradict the file just read.
        _npm_root(tmp_path, "packages/*", "!packages/legacy")
        _make(tmp_path, "packages/api/", "packages/legacy/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["packages/api"]

    def test_patterns_expand_relative_to_the_declaring_package(self, tmp_path: Path) -> None:
        # The inner declaration says "packages/*" about ITSELF, not about the root.
        _make(tmp_path, "repo/.git/", "packages/decoy/")
        _npm_root(tmp_path / "repo", "packages/*")
        _make(tmp_path, "repo/packages/ui/", "repo/packages/api/")

        assert _relative_paths(scan(tmp_path), tmp_path) == [
            "repo",
            "repo/packages/api",
            "repo/packages/ui",
        ]

    def test_a_missing_member_directory_is_not_offered(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/api", "packages/ghost")
        _make(tmp_path, "packages/api/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["packages/api"]

    def test_a_member_file_is_not_offered(self, tmp_path: Path) -> None:
        # Only a directory can become a folder's project directory.
        _npm_root(tmp_path, "packages/notes.md")
        _make(tmp_path, "packages/notes.md")

        assert scan(tmp_path).candidates == ()


class TestMemberTiers:
    def test_a_member_that_is_already_auto_selected_keeps_that_tier(self, tmp_path: Path) -> None:
        # Being named in a member list is weaker evidence than the directory's own
        # ``.kiro``, so it must not demote what the walk already ticked.
        _npm_root(tmp_path, "packages/*")
        _make(tmp_path, "packages/ui/.kiro/", "packages/ui/package.json")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "packages" / "ui")]

        assert candidate.tier is Tier.AUTO
        assert candidate.signals == (".kiro", manifest_signal("package.json"), "member")

    def test_a_member_with_its_own_manifest_records_both_reasons(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/*")
        _make(tmp_path, "packages/ui/package.json")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "packages" / "ui")]

        assert candidate.tier is Tier.OFFERED
        assert candidate.signals == (manifest_signal("package.json"), "member")

    def test_a_member_named_twice_is_one_candidate(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/*", "packages/ui")
        _make(tmp_path, "packages/ui/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["packages/ui"]

    def test_a_member_becomes_the_parent_of_packages_below_it(self, tmp_path: Path) -> None:
        # "group" has no signal of its own, so the walk gave "inner" the root as
        # its parent; once the member list makes "group" a candidate, the folder
        # tree has to nest "inner" under it.
        _npm_root(tmp_path, "packages/group")
        _make(tmp_path, "packages/group/inner/pyproject.toml")

        parents = {candidate.name: candidate.parent_path for candidate in scan(tmp_path).candidates}

        assert parents == {
            "group": None,
            "inner": str(tmp_path / "packages" / "group"),
        }


class TestMemberLimits:
    def test_a_member_outside_the_scan_root_is_dropped(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        _make(tmp_path, "outside/secret/")
        _npm_root(root, "../outside/*", "packages/api")
        _make(root, "packages/api/")

        tree = scan(root)

        assert _relative_paths(tree, root) == ["packages/api"]

    def test_an_absolute_member_is_dropped(self, tmp_path: Path) -> None:
        other = tmp_path / "elsewhere"
        _make(other, "pkg/")
        root = tmp_path / "root"
        _npm_root(root, str(other / "pkg"), "packages/api")
        _make(root, "packages/api/")

        assert _relative_paths(scan(root), root) == ["packages/api"]

    def test_a_member_under_a_pruned_directory_is_dropped(self, tmp_path: Path) -> None:
        # Prune precedence has to hold for members too, or a member list becomes a
        # way to reintroduce a vendored dependency tree.
        _npm_root(tmp_path, "node_modules/*", "target/gen", "packages/api")
        _make(tmp_path, "node_modules/dep/", "target/gen/", "packages/api/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["packages/api"]

    def test_a_hidden_member_directory_is_dropped(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "*", ".hidden/pkg")
        _make(tmp_path, ".hidden/pkg/", "packages/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["packages"]

    def test_a_member_past_the_depth_cap_is_dropped(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "a/b/c/deep", "a/b")
        _make(tmp_path, "a/b/c/deep/")

        assert _relative_paths(scan(tmp_path, depth_cap=3), tmp_path) == ["a/b"]

    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_a_member_reached_through_a_symlink_is_dropped(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        _make(outside, "pkg/")
        root = tmp_path / "root"
        _npm_root(root, "link/*", "link", "packages/api")
        _make(root, "packages/api/")
        (root / "link").symlink_to(outside, target_is_directory=True)

        assert _relative_paths(scan(root), root) == ["packages/api"]

    @pytest.mark.skipif(_IS_WINDOWS, reason="file symlinks need elevation on Windows")
    def test_a_symlinked_declaration_is_not_read(self, tmp_path: Path) -> None:
        # The scan opens declarations, so following a link would read a file from
        # outside the tree the user pointed at. The name still signals a manifest,
        # which is name-only and cannot reach anywhere.
        outside = _write(tmp_path / "outside" / "package.json", '{"workspaces": ["packages/*"]}')
        root = tmp_path / "root"
        _make(root, "packages/api/", "src/")
        (root / "package.json").symlink_to(outside)

        tree = scan(root)

        assert tree.candidates == ()
        assert tree.warnings == ()


class TestDeclarationFailures:
    def test_a_malformed_declaration_warns_and_the_scan_continues(self, tmp_path: Path) -> None:
        _write(tmp_path / "package.json", "{ broken")
        _make(tmp_path, "packages/api/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/api"]
        assert len(tree.warnings) == 1
        assert str(tmp_path / "package.json") in tree.warnings[0]

    def test_one_broken_declaration_does_not_hide_another_package_members(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "broken" / "pnpm-workspace.yaml", "packages: [unclosed\n")
        _npm_root(tmp_path / "good", "packages/*")
        _make(tmp_path, "good/packages/api/")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["good", "good/packages/api"]
        assert len(tree.warnings) == 1

    def test_a_warning_reason_stays_short_and_single_line(self, tmp_path: Path) -> None:
        _write(tmp_path / "pnpm-workspace.yaml", "packages:\n\t- broken tab indent\n")

        tree = scan(tmp_path)

        assert len(tree.warnings) == 1
        assert "\n" not in tree.warnings[0]
        assert len(tree.warnings[0]) < 400

    def test_scanning_a_declaring_tree_still_writes_nothing(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/*")
        _make(tmp_path, "packages/api/", "packages/web/package.json")
        before = {
            os.path.join(current, name): os.stat(
                os.path.join(current, name), follow_symlinks=False
            ).st_mtime_ns
            for current, dirnames, filenames in os.walk(tmp_path)
            for name in dirnames + filenames
        }

        scan(tmp_path)

        after = {
            os.path.join(current, name): os.stat(
                os.path.join(current, name), follow_symlinks=False
            ).st_mtime_ns
            for current, dirnames, filenames in os.walk(tmp_path)
            for name in dirnames + filenames
        }
        assert after == before

    def test_two_scans_of_a_declaring_tree_are_equal(self, tmp_path: Path) -> None:
        _npm_root(tmp_path, "packages/**", "!packages/legacy")
        _make(tmp_path, "packages/api/", "packages/legacy/", "packages/group/inner/")
        _write(tmp_path / "broken.d" / "package.json", "{ broken")

        assert scan(tmp_path) == scan(tmp_path)


# AWS's documented example access-key id, split so no contiguous key-shaped
# literal exists in this source file for a secrets scanner to flag; the joined
# runtime value is what the redactor must catch.
_FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


class TestWarningReasonRedaction:
    """A parse error quotes the offending source line, and that line is tree
    content the user merely pointed at — so a credential in a malformed
    declaration would otherwise ride the warning into the scan response."""

    def test_a_credential_in_an_exception_message_is_redacted(self) -> None:
        exc = ValueError(f"could not parse near '{_FAKE_AWS_KEY}'")
        reason = _warning_reason(exc)
        assert _FAKE_AWS_KEY not in reason
        assert "REDACTED" in reason

    def test_a_credential_in_a_malformed_declaration_never_reaches_warnings(
        self, tmp_path: Path
    ) -> None:
        # End to end through the scanner: YAML quotes the bad line verbatim in
        # its error ("found character '\t' ..."), which is exactly the
        # exfiltration path — a warning is API response content.
        (tmp_path / ".git").mkdir()
        _write(tmp_path / "pnpm-workspace.yaml", f"packages: [\n\t{_FAKE_AWS_KEY}\n")

        tree = scan(tmp_path)

        assert any("pnpm-workspace.yaml" in w for w in tree.warnings)
        assert not any(_FAKE_AWS_KEY in w for w in tree.warnings)


class TestParserStackExhaustion:
    """Deep nesting exhausts the parser stack while sitting well under the size
    cap — tree content the user merely pointed at must cost a warning, never
    the scan (the pnpm parser has guarded this from the start)."""

    def test_a_deeply_nested_package_json_costs_a_warning_not_the_scan(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        depth = 100_000
        _write(tmp_path / "package.json", "[" * depth + "]" * depth)

        tree = scan(tmp_path)

        assert any("package.json" in w for w in tree.warnings)

    def test_a_deeply_nested_cargo_toml_costs_a_warning_not_the_scan(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        depth = 100_000
        _write(tmp_path / "Cargo.toml", "members = " + "[" * depth + "]" * depth)

        tree = scan(tmp_path)

        assert any("Cargo.toml" in w for w in tree.warnings)


class TestSensitiveReadGate:
    """The scan's two file reads never open a file inside a protected location,
    whatever the root was — defense in depth beneath the route's own
    root-containment refusal."""

    def test_a_declaration_inside_a_protected_location_is_never_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()
        target = _write(tmp_path / "package.json", '{"workspaces": ["app"]}')
        opened: list[str] = []
        real_open = open

        def _spy_open(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        monkeypatch.setattr(
            "kiro_crew.project_scan.is_sensitive_path", lambda p: str(p) == str(target)
        )
        monkeypatch.setattr("builtins.open", _spy_open)

        tree = scan(tmp_path)

        assert str(target) not in opened
        assert any("package.json" in w for w in tree.warnings)

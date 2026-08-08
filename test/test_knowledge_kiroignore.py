"""Tests for ``.kiroignore`` support in knowledge folder source scans."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

from kiro_crew import hooks
from kiro_crew.knowledge import folder_watcher as fw_mod
from kiro_crew.knowledge import kiroignore
from kiro_crew.knowledge.folder_watcher import FolderWatcher


def _rules(*lines: str) -> kiroignore.KiroIgnore:
    """Compile rule lines directly, without going through a file."""
    compiled = [r for r in (kiroignore._compile(ln) for ln in lines) if r is not None]
    return kiroignore.KiroIgnore(compiled)


def _watcher() -> FolderWatcher:
    """A watcher for ``_walk`` only -- store/pipeline are never touched by it."""
    return FolderWatcher(MagicMock(), MagicMock())


class TestKiroIgnoreSyntax:
    def test_comments_and_blank_lines_carry_no_rules(self):
        m = _rules("", "   ", "# a comment", "  # indented comment")
        assert not m
        assert m.is_ignored("anything.md", is_dir=False) is False

    def test_escaped_hash_is_a_literal_pattern(self):
        m = _rules(r"\#notes.md")
        assert m.is_ignored("#notes.md", is_dir=False) is True
        assert m.is_ignored("notes.md", is_dir=False) is False

    def test_bare_basename_matches_at_any_depth(self):
        m = _rules("cdk.out")
        assert m.is_ignored("cdk.out", is_dir=True) is True
        assert m.is_ignored("services/api/cdk.out", is_dir=True) is True

    def test_leading_slash_anchors_at_the_source_root(self):
        m = _rules("/build.md")
        assert m.is_ignored("build.md", is_dir=False) is True
        assert m.is_ignored("sub/build.md", is_dir=False) is False

    def test_embedded_separator_also_anchors(self):
        m = _rules("docs/generated")
        assert m.is_ignored("docs/generated", is_dir=True) is True
        assert m.is_ignored("pkg/docs/generated", is_dir=True) is False

    def test_trailing_slash_matches_directories_only(self):
        m = _rules("cache/")
        assert m.is_ignored("cache", is_dir=True) is True
        assert m.is_ignored("cache", is_dir=False) is False

    def test_star_does_not_cross_a_separator(self):
        m = _rules("*.snap.md")
        assert m.is_ignored("a.snap.md", is_dir=False) is True
        assert m.is_ignored("deep/a.snap.md", is_dir=False) is True
        m2 = _rules("/gen/*.md")
        assert m2.is_ignored("gen/a.md", is_dir=False) is True
        assert m2.is_ignored("gen/sub/a.md", is_dir=False) is False

    def test_question_mark_matches_one_non_separator_char(self):
        m = _rules("/v?.md")
        assert m.is_ignored("v1.md", is_dir=False) is True
        assert m.is_ignored("v10.md", is_dir=False) is False

    def test_double_star_prefix_crosses_separators(self):
        m = _rules("**/cdk.out/")
        assert m.is_ignored("cdk.out", is_dir=True) is True
        assert m.is_ignored("a/b/cdk.out", is_dir=True) is True

    def test_double_star_suffix_matches_directory_contents(self):
        m = _rules("logs/**")
        assert m.is_ignored("logs/today.md", is_dir=False) is True
        assert m.is_ignored("logs/a/b.md", is_dir=False) is True
        assert m.is_ignored("other/today.md", is_dir=False) is False

    def test_double_star_in_the_middle(self):
        m = _rules("pkg/**/snap.md")
        assert m.is_ignored("pkg/snap.md", is_dir=False) is True
        assert m.is_ignored("pkg/a/b/snap.md", is_dir=False) is True

    def test_negation_the_last_matching_rule_wins(self):
        m = _rules("*.md", "!keep.md")
        assert m.is_ignored("note.md", is_dir=False) is True
        assert m.is_ignored("keep.md", is_dir=False) is False
        # Order matters: a later broad rule re-excludes.
        m2 = _rules("!keep.md", "*.md")
        assert m2.is_ignored("keep.md", is_dir=False) is True

    def test_an_excluded_directory_covers_its_whole_subtree(self):
        m = _rules("cdk.out/")
        assert m.is_ignored("cdk.out/asset/manifest.md", is_dir=False) is True

    def test_negation_cannot_re_include_inside_an_excluded_directory(self):
        """Documented subset boundary, matching git's own behaviour."""
        m = _rules("cdk.out/", "!cdk.out/keep.md")
        assert m.is_ignored("cdk.out/keep.md", is_dir=False) is True

    def test_character_classes_are_matched_literally(self):
        """Documented subset boundary: ``[...]`` is not a class here."""
        m = _rules("[ab].md")
        assert m.is_ignored("[ab].md", is_dir=False) is True
        assert m.is_ignored("a.md", is_dir=False) is False

    def test_empty_relative_path_is_never_ignored(self):
        m = _rules("*")
        assert m.is_ignored("", is_dir=True) is False
        assert m.is_ignored(".", is_dir=True) is False


class TestKiroIgnoreLoad:
    def test_absent_file_yields_no_matcher(self, tmp_path):
        assert kiroignore.load(tmp_path) is None

    def test_rule_only_file_yields_no_matcher(self, tmp_path):
        (tmp_path / kiroignore.KIROIGNORE_FILENAME).write_text("# nothing here\n\n")
        assert kiroignore.load(tmp_path) is None

    def test_loads_rules_from_the_source_root(self, tmp_path):
        (tmp_path / kiroignore.KIROIGNORE_FILENAME).write_text("cdk.out/\n*.snap.md\n")
        m = kiroignore.load(tmp_path)
        assert m is not None
        assert m.is_ignored("cdk.out", is_dir=True) is True
        assert m.is_ignored("a.snap.md", is_dir=False) is True

    def test_unreadable_file_degrades_to_no_exclusions(self, tmp_path, monkeypatch):
        (tmp_path / kiroignore.KIROIGNORE_FILENAME).write_text("cdk.out/\n")

        def boom(*a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", boom)
        assert kiroignore.load(tmp_path) is None

    def test_rule_cap_is_enforced(self, tmp_path):
        lines = "\n".join(f"gen{i}/" for i in range(kiroignore.MAX_RULES + 50))
        (tmp_path / kiroignore.KIROIGNORE_FILENAME).write_text(lines)
        m = kiroignore.load(tmp_path)
        assert m is not None
        assert len(m._rules) == kiroignore.MAX_RULES


class TestKiroIgnoreReadIsGuarded:
    """A source root is registered by a user, so the rule file is an
    attacker-influenceable path: it is read through the centralized guarded read,
    not a bare ``open``, and a refused read degrades to "no exclusions"."""

    def test_read_is_routed_through_the_centralized_helper(self, tmp_path, monkeypatch):
        """Pins the guard's arguments so the containment root and the size
        ceiling cannot silently stop being passed."""
        (tmp_path / kiroignore.KIROIGNORE_FILENAME).write_text("cdk.out/\n")
        seen: dict[str, object] = {}

        def record(raw, within_root=None, *, max_bytes=None, **kw):
            seen.update(raw=raw, within_root=within_root, max_bytes=max_bytes)
            return b"cdk.out/\n"

        monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", record)

        assert kiroignore.load(tmp_path) is not None
        assert seen["raw"] == str(tmp_path / kiroignore.KIROIGNORE_FILENAME)
        assert seen["within_root"] == str(tmp_path)
        assert seen["max_bytes"] == kiroignore.MAX_FILE_BYTES

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    def test_symlink_out_of_the_source_root_is_never_read(self, tmp_path):
        """A rule file is project config: resolving outside the tree it describes
        is refused even when the target is not itself classified sensitive."""
        root = tmp_path / "src"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "rules.txt"
        target.write_text("cdk.out/\n*.snap.md\n")
        (root / kiroignore.KIROIGNORE_FILENAME).symlink_to(target)

        # Not merely "no matcher" -- the outside file's rules must have no effect.
        assert kiroignore.load(root) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    def test_symlink_to_a_protected_file_is_never_read(self, tmp_path, monkeypatch):
        """The reported vector: a ``.kiroignore`` symlinked at a credential store
        would otherwise have the secret's lines parsed -- and logged -- as rules."""
        # is_sensitive_path is HOME-relative and resolved per call, so pointing
        # HOME at the fixture makes this credential store genuinely protected.
        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        creds = home / ".aws" / "credentials"
        creds.write_text("[default]\naws_secret_access_key = SECRET\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        # The source root is HOME itself, so the containment check alone would
        # admit the target: the sensitive-path floor is what has to refuse it.
        (home / kiroignore.KIROIGNORE_FILENAME).symlink_to(creds)

        assert kiroignore.load(home) is None

    def test_oversize_is_judged_on_the_bytes_actually_read(self, tmp_path):
        """The ceiling is enforced by the guarded read, not by a ``stat`` of a
        path that may no longer be the file that gets opened."""
        body = "cdk.out/\n" + ("# pad\n" * 20000)
        path = tmp_path / kiroignore.KIROIGNORE_FILENAME
        path.write_text(body)
        assert path.stat().st_size > kiroignore.MAX_FILE_BYTES

        assert kiroignore.load(tmp_path) is None


class TestWalkHonoursKiroIgnore:
    @pytest.fixture()
    def project(self, tmp_path):
        root = tmp_path / "monorepo"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "design.md").write_text("# Design")
        (root / "README.md").write_text("# Readme")
        # DELIBERATELY not ``cdk.out``: that directory joined HARD_SKIP_DIRS
        # (never walked, regardless of .kiroignore) in the same-day change
        # that skips CDK output by default — a canary inside it is invisible
        # even with no rule file, which inverts what these tests assert.
        # ``synth.out`` is generated-output-shaped but NOT default-skipped,
        # so discovery vs .kiroignore-driven exclusion stays observable.
        (root / "synth.out" / "asset.abc123").mkdir(parents=True)
        (root / "synth.out" / "manifest.json").write_text("{}")
        (root / "synth.out" / "asset.abc123" / "bundle.md").write_text("generated")
        (root / "coverage").mkdir()
        (root / "coverage" / "report.md").write_text("generated")
        return root

    def _walk_paths(self, root):
        return [p for p, _ in _watcher()._walk(str(root), [], set())]

    def test_no_kiroignore_leaves_discovery_unchanged(self, project):
        paths = self._walk_paths(project)
        assert any("design.md" in p for p in paths)
        assert any("bundle.md" in p for p in paths)
        assert any("report.md" in p for p in paths)

    def test_matching_paths_are_excluded(self, project):
        (project / ".kiroignore").write_text("synth.out/\n/coverage/\n")
        paths = self._walk_paths(project)
        assert any("design.md" in p for p in paths)
        assert any("README.md" in p for p in paths)
        assert not any("bundle.md" in p for p in paths)
        assert not any("manifest.json" in p for p in paths)
        assert not any("report.md" in p for p in paths)

    def test_ignored_directories_are_pruned_from_the_walk(self, project, monkeypatch):
        """The tree must never be DESCENDED, not merely filtered after the fact."""
        (project / ".kiroignore").write_text("synth.out/\n")
        visited: list[str] = []
        real_walk = os.walk

        def spy(top, *args, **kwargs):
            # Yields the real generator's own dirnames list, so the in-place prune
            # under test still controls what os.walk descends into.
            for entry in real_walk(top, *args, **kwargs):
                visited.append(entry[0])
                yield entry

        monkeypatch.setattr(fw_mod.os, "walk", spy)
        self._walk_paths(project)
        assert not any("synth.out" in v for v in visited)
        assert any(v.endswith("docs") for v in visited)

    def test_the_rule_file_itself_is_not_indexed(self, project):
        (project / ".kiroignore").write_text("coverage/\n")
        paths = self._walk_paths(project)
        assert not any(p.endswith(".kiroignore") for p in paths)

    def test_negation_re_includes_a_file(self, project):
        (project / ".kiroignore").write_text("*.md\n!README.md\n")
        paths = self._walk_paths(project)
        assert any(p.endswith("README.md") for p in paths)
        assert not any(p.endswith("design.md") for p in paths)

    def test_malformed_file_degrades_without_losing_valid_rules(self, project):
        """A junk line costs that line only; the scan never raises."""
        path = project / ".kiroignore"
        path.write_bytes(b"synth.out/\n\xff\xfe not utf-8 \xff\n[unclosed\n")
        paths = self._walk_paths(project)
        assert not any("bundle.md" in p for p in paths)
        # Everything the valid rule did not name is still discovered.
        assert any("design.md" in p for p in paths)
        assert any("report.md" in p for p in paths)

    def test_unreadable_rule_file_never_fails_the_scan(self, project, monkeypatch):
        (project / ".kiroignore").write_text("synth.out/\n")

        def boom(*a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", boom)
        paths = self._walk_paths(project)
        # Degrades to no extra exclusions rather than raising mid-scan.
        assert any("bundle.md" in p for p in paths)
        assert any("design.md" in p for p in paths)

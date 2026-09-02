"""Tests for the auto-ingest document filter (knowledge/doc_filter.py)."""

from __future__ import annotations

from pathlib import Path

from test_folder_watcher import LOCK_FILES

from kiro_crew.knowledge.doc_filter import (
    DOC_EXTENSIONS,
    MIN_DOC_BYTES,
    project_doc_properties,
    should_ingest_doc,
)
from kiro_crew.knowledge.folder_watcher import FolderWatcher

BIG = MIN_DOC_BYTES + 10


class TestRootAnchoring:
    """The single most likely way to ship a silently-wrong filter."""

    def test_nested_security_docs_survive(self):
        # Measured: an unanchored basename rule deleted both of these.
        assert should_ingest_doc("docs/kiro-cli/mcp/security.md", BIG)
        assert should_ingest_doc("docs/system-specs/modules/security.md", BIG)

    def test_root_security_policy_is_dropped(self):
        assert not should_ingest_doc("SECURITY.md", BIG)

    def test_nested_boilerplate_names_survive(self):
        for rel in ("docs/CONTRIBUTING.md", "guide/CHANGELOG.md", "notes/AGENTS.md"):
            assert should_ingest_doc(rel, BIG), rel

    def test_root_boilerplate_is_dropped(self):
        for rel in ("AGENTS.md", "CLAUDE.md", "KIRO.md", "GEMINI.md", "CHANGELOG.md",
                    "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "AUTHORS.md", "NOTICE.md",
                    "LICENSE.md"):
            assert not should_ingest_doc(rel, BIG), rel


class TestExtensions:
    def test_document_extensions_taken(self):
        for ext in DOC_EXTENSIONS:
            assert should_ingest_doc(f"docs/note{ext}", BIG), ext

    def test_txt_is_excluded(self):
        # In a code repo a .txt is nearly always a list, not prose.
        assert not should_ingest_doc("docs/SOURCES.txt", BIG)

    def test_code_and_data_never_taken(self):
        for rel in ("src/app.py", "src/app.ts", "data.json", "conf.yaml",
                    "rows.csv", "run.log", "script.sh", "Makefile"):
            assert not should_ingest_doc(rel, BIG), rel

    def test_doc_extensions_are_all_reader_supported(self):
        from kiro_crew.knowledge.readers import FileReader
        assert DOC_EXTENSIONS <= FileReader.SUPPORTED


class TestSkips:
    def test_skill_files_dropped_at_any_depth(self):
        assert not should_ingest_doc("SKILL.md", BIG)
        assert not should_ingest_doc("skills/a/b/SKILL.md", BIG)
        assert not should_ingest_doc("packages/x/MY-SKILL.md", BIG)

    def test_egg_info_pruned(self):
        # The dot is mid-name, so the walk's dot-prefix pruning never sees it.
        assert not should_ingest_doc("src/kirocrew.egg-info/PKG-INFO.md", BIG)

    def test_skip_dirs_pruned(self):
        for rel in ("tmp/notes.md", "vendor/lib/readme.md", "fixtures/case.md",
                    "__snapshots__/a.md", "locales/en.md", "migrations/001.md",
                    "examples/demo.md", "coverage/report.md", "site-packages/x.md"):
            assert not should_ingest_doc(rel, BIG), rel

    def test_dot_dirs_pruned(self):
        assert not should_ingest_doc(".github/PULL_REQUEST_TEMPLATE.md", BIG)

    def test_hard_skip_dirs_pruned(self):
        assert not should_ingest_doc("node_modules/pkg/readme.md", BIG)

    def test_cdk_out_pruned(self):
        # Inherited from HARD_SKIP_DIRS. The prose case is the one that matters:
        # synth output is JSON, which the extension allowlist already drops.
        assert not should_ingest_doc("cdk.out/tree.json", BIG)
        assert not should_ingest_doc("infra/cdk.out/docs/readme.md", BIG)

    def test_dependency_locks_need_no_project_doc_patterns(self):
        # The extension allowlist alone excludes every lock file, at any depth,
        # so this filter carries no lock-file entries of its own.
        for name in LOCK_FILES:
            assert Path(name).suffix.lower() not in DOC_EXTENSIONS, name
            assert not should_ingest_doc(name, BIG), name
            assert not should_ingest_doc(f"tools/{name}", BIG), name

    def test_os_junk_dropped(self):
        assert not should_ingest_doc("docs/._notes.md", BIG)

    def test_size_floor(self):
        assert not should_ingest_doc("docs/stub.md", MIN_DOC_BYTES - 1)
        assert should_ingest_doc("docs/stub.md", MIN_DOC_BYTES)

    def test_unjudgeable_paths_refused(self):
        # Root-anchoring is meaningless without a known root.
        assert not should_ingest_doc("/abs/docs/a.md", BIG)
        assert not should_ingest_doc("../outside/a.md", BIG)
        assert not should_ingest_doc("", BIG)


class TestSeparatorNormalization:
    """Patterns are written with "/", so paths must be normalized before matching."""

    def test_nested_pattern_matches_regardless_of_os_separator(self, tmp_path):
        # On Windows a walked relative path uses "\\", so a pattern containing "/"
        # would silently never match and packaging metadata would be ingested.
        egg = tmp_path / "src" / "pkg.egg-info"
        egg.mkdir(parents=True)
        (egg / "PKG-INFO.md").write_text("x" * BIG)
        (tmp_path / "keep.md").write_text("x" * BIG)
        props = project_doc_properties()
        fw = FolderWatcher(store=None, pipeline=None)
        got = {
            Path(fp).relative_to(tmp_path).as_posix()
            for fp, _ in fw._walk(
                str(tmp_path), props["ignore_patterns"],
                set(props["extra_skip_dirs"]), set(props["include_extensions"]),
                props["min_file_bytes"])
        }
        assert got == {"keep.md"}


class TestRootConfinement:
    """An auto-registered scan must not be carried outside its own root."""

    def _tree(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "real.md").write_text("x" * BIG)
        outside = tmp_path / "private"
        outside.mkdir()
        (outside / "runbook.md").write_text("y" * BIG)
        # os.walk does not descend a DIRECTORY symlink, but a FILE symlink is
        # followed on open -- that is the hole.
        (repo / "docs" / "runbook.md").symlink_to(outside / "runbook.md")
        return repo

    def _walk(self, repo, confine):
        props = project_doc_properties()
        fw = FolderWatcher(store=None, pipeline=None)
        return sorted(
            Path(fp).name
            for fp, _ in fw._walk(
                str(repo), props["ignore_patterns"], set(props["extra_skip_dirs"]),
                set(props["include_extensions"]), props["min_file_bytes"], confine)
        )

    def test_file_symlink_out_of_the_root_is_skipped(self, tmp_path):
        assert self._walk(self._tree(tmp_path), True) == ["real.md"]

    def test_a_hand_added_folder_still_follows_its_links(self, tmp_path):
        # Off by default: for a folder the user registered themselves, following a
        # link they put there is their choice.
        assert self._walk(self._tree(tmp_path), False) == ["real.md", "runbook.md"]

    def test_project_sources_turn_confinement_on(self):
        from kiro_crew.knowledge.project_docs import project_source_properties
        assert project_source_properties()["confine_to_root"] is True

    def test_a_sibling_prefix_is_not_treated_as_inside(self, tmp_path):
        # Both separator forms must work: commonpath answers in the platform's own
        # separator, so a "/"-style input compared against its own source string
        # never matched on Windows.
        from kiro_crew.knowledge.folder_watcher import _within
        base = str(tmp_path / "repo")
        assert _within(str(tmp_path / "repo" / "docs" / "a.md"), base)
        assert not _within(str(tmp_path / "repo-evil" / "a.md"), base)
        assert _within("/repo/docs/a.md", "/repo")
        assert not _within("/repo-evil/a.md", "/repo")
        assert _within("/anything", "")


class TestWalkEquivalence:
    """The predicate and what a scan actually takes must not drift apart."""

    def test_walk_with_project_properties_matches_the_predicate(self, tmp_path):
        files = {
            "README.md": BIG,               # taken
            "SECURITY.md": BIG,             # root boilerplate
            "AGENTS.md": BIG,               # root boilerplate
            "notes.txt": BIG,               # excluded extension
            "src/app.py": BIG,              # code
            "docs/design.md": BIG,          # taken
            "docs/modules/security.md": BIG,  # taken (nested)
            "docs/stub.md": 10,             # below floor
            "docs/SKILL.md": BIG,           # agent instructions
            "tmp/scratch.md": BIG,          # skip dir
            "vendor/lib/guide.md": BIG,     # skip dir
            "x.egg-info/PKG-INFO.md": BIG,  # packaging metadata
        }
        for rel, size in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x" * size)

        props = project_doc_properties()
        fw = FolderWatcher(store=None, pipeline=None)
        walked = {
            Path(fp).relative_to(tmp_path).as_posix()
            for fp, _ in fw._walk(
                str(tmp_path),
                props["ignore_patterns"],
                set(props["extra_skip_dirs"]),
                set(props["include_extensions"]),
                props["min_file_bytes"],
            )
        }
        predicted = {rel for rel, size in files.items() if should_ingest_doc(rel, size)}
        assert walked == predicted
        assert walked == {"README.md", "docs/design.md", "docs/modules/security.md"}

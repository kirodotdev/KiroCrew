"""Tests for Papyrus's path-containment gate and on-disk layout (``store.py``).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Ported and extended from the upstream app's ``backend/tests/test_server.py``. The
coverage target is the security- and correctness-sensitive half of the module:

  * ``safe_child`` — traversal, absolute-path, backslash, NUL and symlink-escape
    defenses, plus the legitimate cases (nested source folders) that must keep
    working;
  * ``safe_project_dir`` — a project name may only be ONE slug segment, so it can
    never contribute a separator or a leading dash;
  * ``get_main_file`` — a ``.papyrus.json`` arriving inside a cloned repository is
    untrusted and must not be able to name a document outside the project;
  * ``resolve_main_file`` — discovery order and the persistence of a non-default
    discovery;
  * ``list_files`` — hidden entries and symlinks skipped, walk bounded;
  * the read/write/create/delete surface, including the refusal to delete the main
    document.

No subprocess is spawned by anything in this file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.papyrus.backend import store


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """An isolated data dir, so no test touches the real app data home."""
    root = tmp_path / "papyrus-data"
    root.mkdir()
    return root


@pytest.fixture()
def project(data_root: Path) -> Path:
    """A project directory with a main document and one nested source file."""
    proj = store.projects_dir(data_root) / "my-paper"
    (proj / "sections").mkdir(parents=True)
    (proj / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    (proj / "references.bib").write_text("", encoding="utf-8")
    (proj / "sections" / "intro.tex").write_text("intro", encoding="utf-8")
    return proj


class TestSafeChild:
    @pytest.mark.parametrize(
        "relative",
        [
            "main.tex",
            "references.bib",
            "chapter.tex.bak",          # a legitimate name with .tex inside it
            "sections/intro.tex",       # subfolders are how real papers are built
            "a/b/c/deep.tex",           # arbitrary depth
        ],
    )
    def test_accepts_legitimate_relative_paths(self, project: Path, relative: str) -> None:
        assert store.safe_child(project, relative).is_relative_to(project.resolve())

    @pytest.mark.parametrize(
        "relative",
        [
            "",                         # empty
            "../etc/passwd",            # parent escape
            "foo/../bar.tex",           # `..` in the middle
            "sections/../../etc",       # `..` after a legitimate-looking prefix
            "..",                       # bare parent
            "/etc/passwd",              # absolute POSIX
            "C:/Windows/system.ini",    # absolute Windows
            "..\\evil",                 # backslash — a separator on Windows
            "sections\\intro.tex",      # backslash anywhere
            "\\\\host\\share\\x.tex",   # UNC
            "main.tex\0.bib",           # NUL truncates at the syscall boundary
            "./main.tex",               # a `.` segment is not a path we accept
            "sections//intro.tex",      # empty segment
        ],
    )
    def test_rejects_unsafe_relative_paths(self, project: Path, relative: str) -> None:
        with pytest.raises(store.PathRejected):
            store.safe_child(project, relative)

    def test_rejects_an_over_long_path(self, project: Path) -> None:
        with pytest.raises(store.PathRejected):
            store.safe_child(project, "a" * 2000)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    def test_rejects_a_symlink_escaping_the_project(self, project: Path) -> None:
        """A cloned repo can ship a symlink whose target is outside the project.

        Every path SEGMENT looks innocent, so only the post-``resolve()``
        containment check catches it — which is why that check must stay.
        """
        secret = project.parent / "outside.tex"
        secret.write_text("secret", encoding="utf-8")
        os.symlink(secret, project / "evil-link.tex")
        with pytest.raises(store.PathRejected):
            store.safe_child(project, "evil-link.tex")

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    def test_rejects_a_path_through_a_symlinked_directory(self, project: Path) -> None:
        """The escape can also be a mid-path DIRECTORY link, not just a file one."""
        outside = project.parent / "outside-dir"
        outside.mkdir()
        (outside / "secret.tex").write_text("secret", encoding="utf-8")
        os.symlink(outside, project / "linked")
        with pytest.raises(store.PathRejected):
            store.safe_child(project, "linked/secret.tex")


class TestSafeProjectDir:
    @pytest.mark.parametrize("name", ["paper", "my-paper", "paper2", "a.b_c-d"])
    def test_accepts_a_slug(self, data_root: Path, name: str) -> None:
        resolved = store.safe_project_dir(name, data_root)
        assert resolved.name == name

    @pytest.mark.parametrize(
        "name",
        [
            "",                     # empty
            "..",                   # parent
            "../escape",            # traversal
            "a/b",                  # a separator would address a nested dir
            "a\\b",                 # Windows separator
            "/abs",                 # absolute
            "-rf",                  # a leading dash could be read as an option
            "Paper",                # uppercase: names are normalized before this
            "x" * 200,              # over the length budget
            "a b",                  # a space is normalized away before this
        ],
    )
    def test_rejects_anything_that_is_not_one_slug_segment(self, data_root: Path, name: str) -> None:
        with pytest.raises(store.PathRejected):
            store.safe_project_dir(name, data_root)

    def test_normalize_slugifies_a_typed_name(self) -> None:
        assert store.normalize_project_name("  My Great Paper ") == "my-great-paper"
        assert store.normalize_project_name("Two   Spaces") == "two-spaces"

    def test_normalize_does_not_make_a_traversal_safe(self, data_root: Path) -> None:
        """Slugifying must never launder an attack into an accepted name."""
        with pytest.raises(store.PathRejected):
            store.safe_project_dir(store.normalize_project_name("../escape"), data_root)


class TestMainFile:
    def test_defaults_when_no_config(self, project: Path) -> None:
        assert store.get_main_file(project) == "main.tex"

    def test_uses_a_valid_configured_value(self, project: Path) -> None:
        (project / "thesis.tex").write_text("", encoding="utf-8")
        store.write_project_config(project, {"main_file": "thesis.tex"})
        assert store.get_main_file(project) == "thesis.tex"

    def test_rejects_traversal_in_the_config(self, project: Path) -> None:
        """A hostile cloned repo's .papyrus.json must not name a file outside.

        This is the pivot the upstream app closed: without the re-validation, the
        PDF-serving route would happily read the configured path.
        """
        (project / store.PROJECT_CONFIG_FILENAME).write_text(
            json.dumps({"main_file": "../../etc/passwd.tex"}), encoding="utf-8"
        )
        assert store.get_main_file(project) == "main.tex"

    def test_rejects_an_absolute_path_in_the_config(self, project: Path) -> None:
        (project / store.PROJECT_CONFIG_FILENAME).write_text(
            json.dumps({"main_file": "/etc/passwd"}), encoding="utf-8"
        )
        assert store.get_main_file(project) == "main.tex"

    def test_handles_a_corrupt_config(self, project: Path) -> None:
        (project / store.PROJECT_CONFIG_FILENAME).write_text("{not valid json", encoding="utf-8")
        assert store.get_main_file(project) == "main.tex"

    def test_handles_a_non_object_config(self, project: Path) -> None:
        (project / store.PROJECT_CONFIG_FILENAME).write_text('["a list"]', encoding="utf-8")
        assert store.get_main_file(project) == "main.tex"

    def test_resolve_prefers_the_existing_main(self, project: Path) -> None:
        assert store.resolve_main_file(project) == "main.tex"

    def test_resolve_falls_back_to_a_known_candidate_and_persists_it(self, data_root: Path) -> None:
        proj = store.projects_dir(data_root) / "cloned"
        proj.mkdir(parents=True)
        (proj / "paper.tex").write_text("", encoding="utf-8")
        assert store.resolve_main_file(proj) == "paper.tex"
        # Persisted, so the next call is a config read rather than a re-search.
        assert store.read_project_config(proj)["main_file"] == "paper.tex"

    def test_resolve_falls_back_to_the_first_tex_in_sorted_order(self, data_root: Path) -> None:
        proj = store.projects_dir(data_root) / "cloned"
        proj.mkdir(parents=True)
        (proj / "zzz.tex").write_text("", encoding="utf-8")
        (proj / "amlc.tex").write_text("", encoding="utf-8")
        assert store.resolve_main_file(proj) == "amlc.tex"

    def test_resolve_returns_none_without_any_tex(self, data_root: Path) -> None:
        proj = store.projects_dir(data_root) / "not-a-paper"
        proj.mkdir(parents=True)
        (proj / "README.md").write_text("", encoding="utf-8")
        assert store.resolve_main_file(proj) is None

    def test_set_main_file_validates_and_preserves_other_keys(self, project: Path) -> None:
        store.write_project_config(project, {"unrelated": "kept"})
        store.set_main_file(project, "sections/intro.tex")
        config = store.read_project_config(project)
        assert config["main_file"] == "sections/intro.tex"
        assert config["unrelated"] == "kept"

    def test_set_main_file_refuses_a_traversal(self, project: Path) -> None:
        with pytest.raises(store.PathRejected):
            store.set_main_file(project, "../evil.tex")

    def test_pdf_path_follows_the_main_stem(self, project: Path) -> None:
        assert store.pdf_path(project, "amlc.tex").name == "amlc.pdf"


class TestListFiles:
    def test_lists_nested_sources_as_posix_paths(self, project: Path) -> None:
        assert store.list_files(project) == [
            "main.tex",
            "references.bib",
            "sections/intro.tex",
        ]

    def test_skips_hidden_entries(self, project: Path) -> None:
        (project / ".git").mkdir()
        (project / ".git" / "config").write_text("", encoding="utf-8")
        (project / store.PROJECT_CONFIG_FILENAME).write_text("{}", encoding="utf-8")
        listed = store.list_files(project)
        assert not any(f.startswith(".") for f in listed)
        assert "main.tex" in listed

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    def test_skips_symlinks_entirely(self, project: Path) -> None:
        """A tree walk that follows links is how containment leaks."""
        outside = project.parent / "outside.tex"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, project / "link.tex")
        assert "link.tex" not in store.list_files(project)

    def test_is_bounded(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(store, "MAX_PROJECT_FILES", 2)
        for i in range(10):
            (project / f"f{i}.tex").write_text("", encoding="utf-8")
        assert len(store.list_files(project)) == 2


class TestListProjects:
    def test_lists_only_projects_with_a_resolvable_main(self, data_root: Path) -> None:
        good = store.projects_dir(data_root) / "good"
        good.mkdir(parents=True)
        (good / "main.tex").write_text("", encoding="utf-8")
        bad = store.projects_dir(data_root) / "no-tex"
        bad.mkdir(parents=True)
        (bad / "README.md").write_text("", encoding="utf-8")

        names = [p.name for p in store.list_projects(data_root)]
        assert names == ["good"]

    def test_reports_pdf_presence(self, data_root: Path) -> None:
        proj = store.projects_dir(data_root) / "paper"
        proj.mkdir(parents=True)
        (proj / "main.tex").write_text("", encoding="utf-8")
        assert store.list_projects(data_root)[0].has_pdf is False
        (proj / "main.pdf").write_bytes(b"%PDF-1.4")
        assert store.list_projects(data_root)[0].has_pdf is True

    def test_summary_serializes_the_wire_shape(self, data_root: Path) -> None:
        proj = store.projects_dir(data_root) / "paper"
        proj.mkdir(parents=True)
        (proj / "main.tex").write_text("", encoding="utf-8")
        payload = store.list_projects(data_root)[0].to_dict()
        assert set(payload) == {"name", "modified", "has_pdf"}


class TestFileIO:
    def test_read_write_round_trip(self, project: Path) -> None:
        store.write_file(project, "main.tex", "hello")
        assert store.read_text_file(project, "main.tex") == "hello"

    def test_write_creates_parent_directories(self, project: Path) -> None:
        store.write_file(project, "figures/plots/a.tex", "x")
        assert (project / "figures" / "plots" / "a.tex").is_file()

    def test_write_refuses_a_traversal(self, project: Path) -> None:
        with pytest.raises(store.PathRejected):
            store.write_file(project, "../escape.tex", "x")

    def test_write_is_size_capped(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(store, "MAX_FILE_BYTES", 8)
        with pytest.raises(ValueError):
            store.write_file(project, "main.tex", "far too much content")

    def test_write_preserves_bytes_exactly(self, project: Path) -> None:
        """A document read, edited and saved repeatedly must not accumulate \\r."""
        store.write_file(project, "main.tex", "a\nb\nc\n")
        assert (project / "main.tex").read_bytes() == b"a\nb\nc\n"

    def test_read_rejects_a_binary_file(self, project: Path) -> None:
        (project / "logo.bin").write_bytes(b"\xff\xfe\x00\x01")
        with pytest.raises(ValueError):
            store.read_text_file(project, "logo.bin")

    def test_read_rejects_an_over_large_file(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (project / "big.tex").write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(store, "MAX_FILE_BYTES", 10)
        with pytest.raises(ValueError):
            store.read_text_file(project, "big.tex")

    def test_read_missing_file_raises(self, project: Path) -> None:
        with pytest.raises(FileNotFoundError):
            store.read_text_file(project, "absent.tex")

    def test_create_refuses_to_clobber(self, project: Path) -> None:
        with pytest.raises(FileExistsError):
            store.create_file(project, "main.tex")

    def test_create_makes_an_empty_file(self, project: Path) -> None:
        store.create_file(project, "methods.tex")
        assert store.read_text_file(project, "methods.tex") == ""

    def test_delete_removes_a_file(self, project: Path) -> None:
        store.delete_file(project, "references.bib")
        assert not (project / "references.bib").exists()

    def test_delete_refuses_the_main_document(self, project: Path) -> None:
        with pytest.raises(ValueError):
            store.delete_file(project, "main.tex")

    def test_delete_missing_file_raises(self, project: Path) -> None:
        with pytest.raises(FileNotFoundError):
            store.delete_file(project, "absent.tex")

    def test_delete_refuses_a_traversal(self, project: Path) -> None:
        with pytest.raises(store.PathRejected):
            store.delete_file(project, "../outside.tex")


class TestProjectConfig:
    def test_write_then_read_round_trip(self, project: Path) -> None:
        store.write_project_config(project, {"main_file": "foo.tex"})
        assert store.read_project_config(project)["main_file"] == "foo.tex"

    def test_write_replaces_an_existing_config(self, project: Path) -> None:
        store.write_project_config(project, {"main_file": "old.tex"})
        store.write_project_config(project, {"main_file": "new.tex"})
        assert store.read_project_config(project)["main_file"] == "new.tex"

    def test_write_leaves_no_temp_files_behind(self, project: Path) -> None:
        before = {p.name for p in project.iterdir()}
        store.write_project_config(project, {"main_file": "foo.tex"})
        after = {p.name for p in project.iterdir()}
        assert after - before == {store.PROJECT_CONFIG_FILENAME}

    def test_absent_config_reads_as_empty(self, project: Path) -> None:
        assert store.read_project_config(project) == {}


class TestArtifactClassification:
    @pytest.mark.parametrize("name", ["main.aux", "main.log", "main.bbl", "main.TOC"])
    def test_recognizes_build_artifacts(self, name: str) -> None:
        assert store.is_artifact(name)

    @pytest.mark.parametrize("name", ["main.tex", "references.bib", "acl.sty", "fig.png"])
    def test_leaves_source_alone(self, name: str) -> None:
        assert not store.is_artifact(name)

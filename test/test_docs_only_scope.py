"""Unit tests for .github/scripts/docs_only.py.

CI skips its heavy lanes on a docs-only pull request and PR Readiness excuses
them, and both ask this one script. These tests pin which paths count as prose
docs, that every unreadable or untrusted input answers "not docs-only" (run
everything), and that the reader selector finds tests that read a doc by name,
by Markdown glob, or through a helper module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "docs_only.py"


@pytest.fixture(scope="module")
def module():
    return load_skill_script("docs_only", SCRIPT)


@pytest.mark.parametrize(
    "path",
    [
        "docs/README.md",
        "docs/guides/install.md",
        "src/kiro_crew/docs/agent-spec-fields.md",
        "website/docs/page-layout.md",
        "README.md",
        "CONTRIBUTING.md",
    ],
)
def test_prose_doc_paths(module, path):
    assert module.is_prose_doc(path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/guides/assets/setup.sh",
        "docs/guides/assets/shot.png",
        "skills/foo/SKILL.md",
        "AGENTS.md",
        "CHANGELOG.md",
        ".github/workflows/ci.yml",
        ".kiro/steering/x.md",
        "website/README.md",
        "src/kiro_crew/README.md",
        "docsx/a.md",
        "src/kiro_crew/docs/ui-index.generated.json",
    ],
)
def test_other_paths_are_not_prose_docs(module, path):
    assert not module.is_prose_doc(path)


def test_classify(module):
    assert module.classify(["docs/a.md", "README.md"])[0] is True
    assert module.classify(["docs/a.md", "src/kiro_crew/x.py"])[0] is False
    assert module.classify([])[0] is False
    many = ["docs/f%d.md" % i for i in range(2 * module.MAX_FILES + 1)]
    assert module.classify(many)[0] is False


def _compare(*entries):
    return json.dumps({"files": list(entries)})


def test_compare_counts_both_sides_of_a_rename(module):
    body = _compare({"filename": "docs/moved.md", "previous_filename": "src/kiro_crew/x.py"})
    paths, reason = module.paths_from_compare(body, 1)
    assert reason == ""
    assert module.classify(paths)[0] is False


@pytest.mark.parametrize(
    "body,expect",
    [
        (_compare({"filename": "docs/a.md"}), 2),  # truncated or moved head
        (_compare({"filename": "docs/a.md"}), 0),
        (_compare({"filename": "docs/a.md"}), 10_000),
        ("not json", 1),
        (json.dumps({"files": None}), 1),
        (_compare({"name": "docs/a.md"}), 1),
    ],
)
def test_untrusted_compare_is_not_docs_only(module, body, expect):
    paths, reason = module.paths_from_compare(body, expect)
    assert paths == [] and reason


def test_cli_prints_false_on_bad_input(module, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert module.main(["docs_only.py", "classify", "--expect-files", "1"]) == 0
    assert capsys.readouterr().out.strip() == "docs_only=false"
    monkeypatch.setattr("sys.stdin", io.StringIO("docs/a.md\n"))
    assert module.main(["docs_only.py", "classify"]) == 0
    assert capsys.readouterr().out.strip() == "docs_only=true"
    assert module.main(["docs_only.py", "bogus"]) == 2


def test_readers_by_name_glob_and_helper(module, tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "website" / "src").mkdir(parents=True)
    (tmp_path / "test" / "test_names.py").write_text('DOC = ROOT / "docs" / "install.md"\n')
    (tmp_path / "test" / "test_globs.py").write_text('paths = ROOT.rglob("*.md")\n')
    (tmp_path / "test" / "doc_helpers.py").write_text('NAME = "install.md"\n')
    (tmp_path / "test" / "test_via_helper.py").write_text("from doc_helpers import NAME\n")
    (tmp_path / "test" / "test_unrelated.py").write_text("x = 1\n")
    (tmp_path / "website" / "src" / "a.test.ts").write_text("join('docs', 'install.md')\n")
    (tmp_path / "website" / "src" / "b.test.ts").write_text("x\n")
    got = module.readers(["docs/guides/install.md"], root=str(tmp_path))
    assert got == [
        "test/test_globs.py",
        "test/test_names.py",
        "test/test_via_helper.py",
        "website/src/a.test.ts",
    ]
    assert module.readers([], root=str(tmp_path)) == []


def test_readers_by_md_suffix_literal_and_source_corpus(module, tmp_path):
    (tmp_path / "test").mkdir()
    tests = tmp_path / "test"
    (tests / "test_suffix.py").write_text('docs = [p for p in files if p.suffix == ".md"]\n')
    (tests / "test_endswith.py").write_text("ok = name.endswith('.md')\n")
    (tests / "test_corpus.py").write_text("import source_corpus\n")
    (tests / "corpus_helper.py").write_text("from source_corpus import repo_files\n")
    (tests / "test_via_corpus_helper.py").write_text("import corpus_helper\n")
    (tests / "test_markdownish.py").write_text('x = "notes.mdx"\n')
    got = module.readers(["docs/guides/brand-new-page.md"], root=str(tmp_path))
    assert got == [
        "test/test_corpus.py",
        "test/test_endswith.py",
        "test/test_suffix.py",
        "test/test_via_corpus_helper.py",
    ]


def test_readers_walk_every_setup_cfg_testpaths_root(module, tmp_path):
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = test src/pkg/apps\n")
    (tmp_path / "test").mkdir()
    app_tests = tmp_path / "src" / "pkg" / "apps" / "demo" / "tests"
    app_tests.mkdir(parents=True)
    (tmp_path / "test" / "test_first.py").write_text('DOC = "install.md"\n')
    (app_tests / "test_second.py").write_text('DOC = "install.md"\n')
    (app_tests / "doc_paths.py").write_text('SPEC = "install.md"\n')
    (app_tests / "test_via_app_helper.py").write_text("from .doc_paths import SPEC\n")
    (app_tests / "test_unrelated.py").write_text("x = 1\n")
    # App code beside the tests is not a helper, though it names the doc.
    (app_tests.parent / "store.py").write_text('SPEC = "install.md"\n')
    (app_tests / "test_store.py").write_text("from ..store import SPEC\n")
    got = module.readers(["docs/guides/install.md"], root=str(tmp_path))
    assert "src/pkg/apps/demo/tests/test_store.py" not in got
    assert got == [
        "src/pkg/apps/demo/tests/test_second.py",
        "src/pkg/apps/demo/tests/test_via_app_helper.py",
        "test/test_first.py",
    ]


@pytest.mark.parametrize("setup_cfg", [None, "[tool:pytest\ntestpaths = src\n", "[x]\na = 1\n"])
def test_unreadable_setup_cfg_falls_back_to_test(module, tmp_path, setup_cfg):
    if setup_cfg is not None:
        (tmp_path / "setup.cfg").write_text(setup_cfg)
    (tmp_path / "test").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "test" / "test_first.py").write_text('DOC = "install.md"\n')
    (tmp_path / "src" / "test_elsewhere.py").write_text('DOC = "install.md"\n')
    assert module._backend_roots(str(tmp_path)) == ["test"]
    got = module.readers(["docs/guides/install.md"], root=str(tmp_path))
    assert got == ["test/test_first.py"]

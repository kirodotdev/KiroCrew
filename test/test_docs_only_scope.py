"""Unit tests for .github/scripts/docs_only.py.

CI skips its heavy lanes on a docs-only pull request and PR Readiness excuses
them, and both ask this one script. These tests pin which paths count as prose
docs, that nothing the package ships is one, that every unreadable or untrusted
input answers "not docs-only" (run everything), and that the reader selector
finds tests that read a doc by name, by Markdown glob, or through a helper
module.
"""

from __future__ import annotations

import configparser
import fnmatch
import json
import subprocess
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

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


def _setup_cfg() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "setup.cfg", encoding="utf-8")
    return parser


def _source_root() -> str:
    """The directory ``setup.cfg`` finds the packages in, with a trailing slash."""
    return _setup_cfg().get("options.packages.find", "where").strip().strip("/") + "/"


def _tracked() -> set[str]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached"],
        check=True,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    return {p for p in listed.stdout.decode("utf-8", errors="replace").split("\0") if p}


def _wheel_data(tracked: set[str]) -> set[str]:
    """Tracked files matched by a ``[options.package_data]`` glob of ``setup.cfg``."""
    source = _source_root()
    shipped: set[str] = set()
    for package, value in _setup_cfg().items("options.package_data"):
        base = ROOT / source / package.replace(".", "/")
        for line in value.splitlines():
            pattern = line.strip()
            if not pattern or pattern.startswith("#"):
                continue
            for found in base.glob(pattern):
                shipped.add(found.relative_to(ROOT).as_posix())
    return shipped & tracked


def _sdist_data(tracked: set[str]) -> set[str]:
    """Tracked files under the source root a ``recursive-include`` of ``MANIFEST.in`` names."""
    source = _source_root()
    shipped: set[str] = set()
    for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        words = line.split()
        if len(words) < 3 or words[0] != "recursive-include":
            continue
        tree = words[1].strip("/") + "/"
        if not tree.startswith(source):
            continue
        for path in tracked:
            if path.startswith(tree) and any(
                fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern) for pattern in words[2:]
            ):
                shipped.add(path)
    return shipped


def test_nothing_the_package_ships_is_a_prose_doc(module):
    tracked = _tracked()
    wheel = {p for p in _wheel_data(tracked) if p.endswith(".md")}
    sdist = {p for p in _sdist_data(tracked) if p.endswith(".md")}
    # Both files name Markdown today, so neither half of the statement is empty.
    assert wheel and sdist
    prose = sorted(p for p in wheel | sdist if module.is_prose_doc(p))
    assert not prose, f"package data counted as prose docs: {prose[:5]}"
    for path in sorted(wheel | sdist):
        assert module.classify([path]) == (False, f"1 non-doc path(s): {path}")
        assert not module.classify(["docs/README.md", path])[0]


def test_no_doc_tree_lies_under_the_package_source_root(module):
    source = _source_root()
    assert source != "/"
    for tree in module._DOC_TREES:
        assert not tree.startswith(source) and not source.startswith(tree), tree
    # Whatever Markdown is added beside the code later is not prose either.
    for tree in sorted({p.rsplit("/", 1)[0] for p in _tracked() if p.startswith(source)}):
        assert not module.is_prose_doc(f"{tree}/added-page.md"), tree


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


def test_the_import_search_and_the_pattern_agree_on_every_shape_of_an_import(module):
    helpers = ["doc_helpers", "read_docs", "x"]
    imports = module._helper_imports(helpers)
    heads = [
        "import ",
        "from ",
        "import\t",
        "from\n",
        "importx ",
        "fromage ",
        "reimport ",
        " import  ",
    ]
    prefixes = [
        "",
        ".",
        "..",
        "test.",
        "tests.",
        "pkg.tests.",
        "a.b.test.",
        "other.",
        "tests",
        "1.",
    ]
    tails = ["", " import y", "s", "_more", ".sub", "\n", ", z"]
    yes = no = 0
    for head in heads:
        for prefix in prefixes:
            for name in [*helpers, "doc_helper", "nothing"]:
                for tail in tails:
                    for text in (
                        f"{head}{prefix}{name}{tail}",
                        f"import os\n{head}{prefix}{name}{tail}\nx = 1\n",
                        f"# {head}{prefix}{name}{tail}",
                    ):
                        want = bool(imports.search(text))
                        yes, no = yes + want, no + (not want)
                        assert module._imports_one(imports, text) == want, text
    # Both answers are exercised.
    assert yes > 100 and no > 100


def test_the_import_search_and_the_pattern_agree_on_every_test_file_of_the_tree(module):
    root = str(ROOT)
    tests = [
        rel
        for top in module._backend_roots(root)
        for rel in module._walk(root, top, lambda n: n.startswith("test_") and n.endswith(".py"))
    ]
    assert len(tests) > 1000
    # Helper modules of this tree's own tests, so some files import one and most do not.
    helpers = ["skill_script_helpers", "ci_test_selection_ledger_fake", "conftest_resets"]
    imports = module._helper_imports(helpers)
    hits = 0
    for rel in tests:
        text = module._read(root, rel)
        want = bool(imports.search(text))
        hits += want
        assert module._imports_one(imports, text) == want, rel
    assert 0 < hits < len(tests)


def test_readers_lists_what_the_whole_text_search_lists_for_docs_of_the_tree(module, monkeypatch):
    root = str(ROOT)
    docs = sorted(
        rel
        for top in ("docs", "website/docs")
        for rel in module._walk(root, top, lambda n: n.endswith(".md"))
        if module.is_prose_doc(rel)
    )
    assert len(docs) > 100
    # A few docs spread over the tree, one at a time and together, as a pull request names them.
    picked = docs[:: len(docs) // 3][:3]
    fast = [module.readers([doc], root) for doc in picked] + [module.readers(picked, root)]
    # The reference: the same function, with the pattern searched over each whole text.
    monkeypatch.setattr(module, "_imports_one", lambda imports, text: bool(imports.search(text)))
    whole = [module.readers([doc], root) for doc in picked] + [module.readers(picked, root)]
    assert fast == whole
    assert all(len(listed) > 100 for listed in fast)

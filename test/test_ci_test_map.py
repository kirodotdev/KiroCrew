"""Contract tests for the SHADOW test map: scripts/ci_test_file_map.py,
scripts/ci_test_map.py, scripts/ci_test_map_shadow.py and
.github/workflows/test-map.yml.

Four properties are pinned:

* RECORD -- the plugin, in a real pytest session, links each test file to the
  repository files its tests executed, and to nothing another test ran; for a
  listed file, to the functions they ran.
* BUILD -- plugin parts merge into the file -> test-file map, and the listed
  files' function -> test-file table; `bundle` adds the co-change and guard
  tables to it, leaving out a table it cannot build.
* SELECT -- every fall-back rule in the module docstring yields the full suite,
  a mapped edit yields its tests plus the guard tier, and an edit inside one
  function of a listed file yields only the tests that ran it.
* SHADOW -- the workflow is scheduled/dispatch only, never cancelled by a push,
  read-only, and is the only workflow that loads the recorder.
"""

from __future__ import annotations

import gzip
import io
import itertools
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_test_map as ctm
from scripts import ci_test_map_shadow as shadow

# One module that starts one child pytest: keep it on one worker, apart from the others.
pytestmark = pytest.mark.xdist_group(name="ci_test_map")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test-map.yml"

MAP = ctm.merge_parts(
    [
        {
            "test/test_a.py": {
                "nodes": 1,
                "files": ["src/kiro_crew/a.py", "test/test_a.py"],
                "functions": [
                    ["src/kiro_crew/a.py", "f", 6, 9],
                    ["src/kiro_crew/a.py", "g", 13, 16],
                ],
            }
        },
        {
            "test/test_b.py": {
                "nodes": 2,
                "files": ["src/kiro_crew/a.py", "src/kiro_crew/b.py", "test/helpers_x.py"],
                "functions": [
                    ["src/kiro_crew/a.py", "g", 13, 16],
                    ["src/kiro_crew/a.py", "g.<locals>.inner", 14, 15],
                    ["src/kiro_crew/a.py", "cached", 23, 25],
                ],
            }
        },
    ],
    commit="abc",
)
TIER = ["test/test_gate.py"]


def test_parts_merge_into_test_files_and_node_counts() -> None:
    assert MAP["tests"] == {"test/test_a.py": 1, "test/test_b.py": 2}
    assert MAP["files"] == {
        "src/kiro_crew/a.py": [0, 1],
        "src/kiro_crew/b.py": [1],
        "test/helpers_x.py": [1],
        "test/test_a.py": [0],
    }
    assert MAP["functions"] == {
        "src/kiro_crew/a.py": {
            "cached": [23, 25, [1]],
            "f": [6, 9, [0]],
            "g": [13, 16, [0, 1]],
            "g.<locals>.inner": [14, 15, [1]],
        }
    }
    # Two parts naming one test file (xdist splits a file's tests) add up.
    again = ctm.merge_parts([{"t.py": {"nodes": 1, "files": ["x"]}}] * 2, commit="")
    assert again["tests"] == {"t.py": 2}
    # A property's getter and setter share one qualname: its entry spans both.
    getter, setter = (
        {"t.py": {"nodes": 1, "files": ["x"], "functions": [["x", "C.p", *span]]}}
        for span in ([3, 4], [6, 7])
    )
    assert ctm.merge_parts([getter, setter], commit="")["functions"] == {"x": {"C.p": [3, 7, [0]]}}
    # One part saw C.p run during an import: no part's runs of it are trusted.
    imported = {ctm.IMPORT_TIME: [["x", "C.p"]]}
    assert ctm.merge_parts([getter, imported], commit="")["functions"] == {}


def test_build_cli_reads_parts_and_refuses_an_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "s1").mkdir()
    (tmp_path / "s1" / "part-gw0.json").write_text(
        json.dumps({"test/test_a.py": {"nodes": 1, "files": ["src/kiro_crew/a.py"]}}),
        encoding="utf-8",
    )
    out = tmp_path / "map.json"
    assert ctm.main(["build", "--parts", str(tmp_path), "--out", str(out), "--commit", "c"]) == 0
    assert ctm.load_map(out)["files"] == {"src/kiro_crew/a.py": [0]}
    (tmp_path / "empty").mkdir()
    assert ctm.main(["build", "--parts", str(tmp_path / "empty"), "--out", str(out)]) == 2


def test_load_map_refuses_another_schema(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"schema": 99, "tests": {}, "files": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        ctm.load_map(path)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        + ["-c", "commit.gpgsign=false", *argv],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")


def _fixture_repo(tmp_path: Path) -> Path:
    """Four commits: a root, two that change `a.py` with `test_a.py`, one with non-ASCII paths."""
    repo = tmp_path / "repo"
    _write(repo, {"src/kiro_crew/a.py": "", "test/test_a.py": "", "docs/x.md": ""})
    # One src file and one test file the OS tables name.
    _write(repo, {"src/kiro_crew/lock.py": "import fcntl\n", "test/test_windows_x.py": ""})
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    for n in ("two", "three"):
        _write(repo, {"src/kiro_crew/a.py": f"# {n}\n", "test/test_a.py": f"# {n}\n"})
        _git(repo, "commit", "-q", "-am", n)
    # Non-ASCII paths, which git quotes in its default (non `-z`) output.
    _write(repo, {"src/kiro_crew/café.py": "", "test/test_café.py": ""})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "four")
    return repo


def _shallow_clone(repo: Path, depth: int) -> Path:
    clone = repo.parent / f"shallow-{depth}"
    _git(repo.parent, "clone", "-q", "--depth", str(depth), f"file://{repo}", clone.name)
    return clone


def test_co_change_links_what_a_first_parent_commit_changed_together(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    commits = ctm.git_first_parent_files(repo, "HEAD", 3, FALLBACK_TIMEOUT_SECS)
    assert commits == [
        ["src/kiro_crew/café.py", "test/test_café.py"],
        *[["src/kiro_crew/a.py", "test/test_a.py"]] * 2,
    ]
    whole = {
        "src/kiro_crew/a.py": ["test/test_a.py"],
        "src/kiro_crew/café.py": ["test/test_café.py"],
    }
    # The root commit lists no paths, so it pairs nothing; nor does a commit with no test.
    assert ctm.co_change_window(repo, "HEAD", 10, FALLBACK_TIMEOUT_SECS) == whole
    assert ctm.co_change([["src/kiro_crew/a.py", "docs/x.md"]]) == {}
    # A shallow clone answers as the full one does for a window whose every commit
    # has its parent, and for no wider one: its boundary commit lists no paths, so
    # that commit's pairs would read as no links.
    for depth in (1, 2, 3):
        clone = _shallow_clone(repo, depth)
        for commits in range(depth):
            full = ctm.co_change_window(repo, "HEAD", commits, FALLBACK_TIMEOUT_SECS)
            assert ctm.co_change_window(clone, "HEAD", commits, FALLBACK_TIMEOUT_SECS) == full
        with pytest.raises(ValueError, match=f"holds {depth} of the {depth + 1} commits"):
            ctm.co_change_window(clone, "HEAD", depth, FALLBACK_TIMEOUT_SECS)


def test_bundle_cli_writes_one_gzip_file_select_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(ctm, "guard_tier", lambda root: TIER)
    # The CLI's git log deadline stays inside the test's own timeout.
    monkeypatch.setattr(ctm, "GIT_LOG_TIMEOUT_SECS", FALLBACK_TIMEOUT_SECS)
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(MAP), encoding="utf-8")
    out = tmp_path / "bundle.json.gz"
    argv = ["--root", str(repo), "bundle", "--map", str(map_path), "--out", str(out)]
    before = datetime.now(timezone.utc).replace(microsecond=0)
    assert ctm.main(argv) == 0
    doc = json.loads(gzip.decompress(out.read_bytes()))
    # The command stamps the file with the time it wrote it, in UTC.
    assert before <= datetime.fromisoformat(doc.pop("built_at")) <= datetime.now(timezone.utc)
    assert "built_at" not in ctm.bundle(MAP, repo, timeout=FALLBACK_TIMEOUT_SECS)[0]
    # The map as it was, with the two tables beside it.
    assert {k: v for k, v in doc.items() if k not in ctm.BUNDLE_TABLES} == MAP
    assert doc["guard"] == TIER
    assert doc["cochange"]["src/kiro_crew/a.py"] == ["test/test_a.py"]
    assert doc["os_sensitive"] == {"src/kiro_crew/lock.py": "W2"}
    assert doc["windows_targeting"] == ["test/test_windows_x.py"]
    assert {k: v for k, v in ctm.load_map(out).items() if k != "built_at"} == doc
    # select reads the bundle as it reads a schema-1 map.
    diff = tmp_path / "diff.txt"
    diff.write_text("M\tsrc/kiro_crew/b.py\n", encoding="utf-8")
    capsys.readouterr()
    for path in (out, map_path):
        assert ctm.main(["select", "--map", str(path), "--name-status", str(diff)]) == 0
        assert capsys.readouterr().out.split() == ["test/test_b.py", "test/test_gate.py"]
    # A file cut short anywhere is a usage error, not a traceback.
    whole = out.read_bytes()
    for size in range(2, len(whole)):
        out.write_bytes(whole[:size])
        assert ctm.main(["select", "--map", str(out), "--name-status", str(diff)]) == 2


def test_a_table_that_cannot_be_built_is_absent_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path)

    def scan_fails(root: Path) -> list[str]:
        raise OSError("unreadable test tree")

    def build(root: Path, guard: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        monkeypatch.setattr(ctm, "guard_tier", guard)
        return ctm.bundle(MAP, root, timeout=FALLBACK_TIMEOUT_SECS, **kwargs)

    cases: list[tuple[tuple[dict[str, Any], dict[str, str]], dict[str, str]]] = [
        (build(repo, lambda root: TIER), {}),
        (build(repo, lambda root: []), {"guard": ctm.NO_GUARD}),
        (build(repo, scan_fails), {"guard": "corpus_gates failed: unreadable test tree"}),
        (build(repo, lambda root: TIER, ref="no-such-ref"), {"cochange": "history read failed"}),
        # A window with no pair, and a clone holding a part of the window: no data, not no links.
        (build(repo, lambda root: TIER, commits=0), {"cochange": "no commit in the window"}),
        (
            build(_shallow_clone(repo, 1), scan_fails),
            {"cochange": "1 of the 2001", "guard": "failed"},
        ),
    ]
    for (doc, absent), reasons in cases:
        assert sorted(absent) == sorted(reasons)
        assert all(why in absent[table] for table, why in reasons.items())
        # Every table is in the file with rows, or named as left out: never both, never empty.
        assert {t for t in ctm.BUNDLE_TABLES if t in doc} == set(ctm.BUNDLE_TABLES) - set(absent)
        assert all(doc[t] for t in ctm.BUNDLE_TABLES if t in doc)
        assert {k: v for k, v in doc.items() if k not in ctm.BUNDLE_TABLES} == MAP
    # A table the map already carries is rebuilt or left out, never carried over.
    stale = {**MAP, "cochange": {"src/kiro_crew/a.py": ["test/test_stale.py"]}, "guard": ["x"]}
    monkeypatch.setattr(ctm, "guard_tier", scan_fails)
    doc, absent = ctm.bundle(stale, _shallow_clone(repo, 3), timeout=FALLBACK_TIMEOUT_SECS)
    assert sorted(absent) == ["cochange", "guard"] and not {"cochange", "guard"} & set(doc)
    # The same in a tree where no table can be built: the map alone is left.
    doc, absent = ctm.bundle(stale, tmp_path / "no-tree", timeout=FALLBACK_TIMEOUT_SECS)
    assert doc == MAP and sorted(absent) == sorted(ctm.BUNDLE_TABLES)
    # The CLI names each table it left out, and still writes the map.
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(MAP), encoding="utf-8")
    out = tmp_path / "bundle.json.gz"
    monkeypatch.setattr(ctm, "GIT_LOG_TIMEOUT_SECS", FALLBACK_TIMEOUT_SECS)
    argv = ["--root", str(_shallow_clone(repo, 2)), "bundle", "--map", str(map_path)]
    assert ctm.main([*argv, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "bundle: tables os_sensitive, windows_targeting," in printed
    assert all(f"::warning::{table} left out" in printed for table in ("cochange", "guard"))
    left = {k: v for k, v in ctm.load_map(out).items() if k not in ctm.BUNDLE_TABLES}
    assert {k: v for k, v in left.items() if k != "built_at"} == MAP


def _run_pytest_with_recorder(project: Path, out: Path) -> subprocess.CompletedProcess[str]:
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(project), *inherited])}
    # The child is a hermetic session: the parent run's options and plugins stay out.
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.ci_test_file_map",
            f"--test-map-dir={out}",
            "--rootdir",
            str(project),
            "-c",
            os.devnull,
            str(project),
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=FALLBACK_TIMEOUT_SECS,
    )


@pytest.fixture(scope="module")
def recorded_parts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One recorded child pytest run that every recorder test below reads."""
    tmp_path = tmp_path_factory.mktemp("recorder")
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pkg" / "one.py").write_text(
        "def f():\n    return 1\n\n\ndef unused():\n    return 0\n\n\n"
        "def limit():\n    return 3\n\n\nLIMIT = limit()\n",
        encoding="utf-8",
    )
    # A directory's conftest is imported while pytest collects the directory, outside any
    # test module: rates.py's body runs rate() there, and a test calls rate() again.
    (project / "rates.py").write_text(
        "def rate():\n    return 2\n\n\nRATE = rate()\n", encoding="utf-8"
    )
    (project / "sub").mkdir()
    (project / "sub" / "conftest.py").write_text("import rates\n", encoding="utf-8")
    (project / "sub" / "test_rate.py").write_text(
        "import rates\n\ndef test_rate():\n    assert rates.rate() == rates.RATE\n",
        encoding="utf-8",
    )
    (project / "pkg" / "two.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    (project / "pkg" / "consts.py").write_text("LIMIT = 3\n", encoding="utf-8")
    (project / "test_one.py").write_text(
        "import threading\nfrom pkg import one, two\n\n"
        "def test_f():\n    t = threading.Thread(target=one.f)\n    t.start()\n    t.join()\n",
        encoding="utf-8",
    )
    (project / "test_two.py").write_text(
        "from pkg import two\n\ndef test_g():\n    assert two.g() == 2\n", encoding="utf-8"
    )
    (project / "test_x.py").write_text(
        "from pkg.consts import LIMIT\n\nCASES = list(range(LIMIT))\n\n"
        "def test_cases():\n    assert CASES == [0, 1, 2]\n",
        encoding="utf-8",
    )
    (project / "scripts").mkdir()
    (project / ctm.CORE_LIST).write_text("# listed\npkg/one.py\nrates.py\n", encoding="utf-8")
    out = tmp_path / "parts"
    done = _run_pytest_with_recorder(project, out)
    assert done.returncode == 0, done.stdout + done.stderr
    return out


def test_the_plugin_records_what_each_test_file_ran(recorded_parts: Path) -> None:
    part = json.loads((recorded_parts / "part-main.json").read_text(encoding="utf-8"))
    # test_one.py imported pkg first: the module bodies are linked to it alone.
    # Of the listed pkg/one.py, only the functions it ran are named: f in its test,
    # limit while its import ran pkg/one.py's body, which marks limit import time.
    assert part["test_one.py"] == {
        "nodes": 1,
        "files": ["pkg/__init__.py", "pkg/one.py", "pkg/two.py", "test_one.py"],
        "functions": [["pkg/one.py", "f", 1, 2], ["pkg/one.py", "limit", 9, 10]],
    }
    assert part["sub/test_rate.py"]["functions"] == [["rates.py", "rate", 1, 2]]
    assert part[ctm.IMPORT_TIME] == [["pkg/one.py", "limit"], ["rates.py", "rate"]]
    assert part["test_two.py"] == {
        "nodes": 1,
        "files": ["pkg/two.py", "test_two.py"],
        "functions": [],
    }


def test_a_module_body_run_at_collection_is_linked_to_its_test_file(
    recorded_parts: Path,
) -> None:
    built = ctm.merge_parts(ctm.read_parts(recorded_parts), commit="c")
    # pkg/consts.py is read only while test_x.py is imported, never during a test.
    assert built["files"]["pkg/consts.py"] == [list(built["tests"]).index("test_x.py")]
    # limit and rate built LIMIT and RATE at import, which later readers use without
    # running them: neither is trusted, though a test ran rate.
    assert built["functions"] == {
        "pkg/one.py": {"f": [1, 2, [list(built["tests"]).index("test_one.py")]]}
    }


def test_a_worker_crash_leaves_a_marker_instead_of_a_silent_gap(tmp_path: Path) -> None:
    """The xdist controller hook, called as xdist calls it, without a child interpreter."""
    from types import SimpleNamespace

    from scripts import ci_test_file_map

    out = tmp_path / "parts"

    def node(worker: str) -> SimpleNamespace:
        config = SimpleNamespace(getoption=lambda name: str(out))
        return SimpleNamespace(config=config, gateway=SimpleNamespace(id=worker))

    ci_test_file_map.pytest_testnodedown(node("gw0"), None)  # a clean shutdown
    ci_test_file_map.pytest_testnodedown(node("gw1"), "worker 'gw1' crashed")
    assert sorted(p.name for p in out.iterdir()) == ["crashed-gw1"]


def _select(text: str, tier: list[str] = TIER) -> ctm.Selection:
    return ctm.select(ctm.parse_name_status(text), MAP, tier)


@pytest.mark.parametrize(
    "diff",
    [
        "A\tsrc/kiro_crew/new.py",
        "D\tsrc/kiro_crew/a.py",
        "R100\tsrc/kiro_crew/a.py\tsrc/kiro_crew/c.py",
        "C90\tsrc/kiro_crew/a.py\tsrc/kiro_crew/c.py",
        "M\ttest/conftest.py",
        "M\tsrc/kiro_crew/apps/builtins/x/conftest.py",
        "M\tsrc/kiro_crew/static/app.js",
        "M\tscripts/ci_file_shards.py",
        "M\tsetup.cfg",
        "M\t.github/workflows/ci.yml",
        "M\tsrc/kiro_crew/never_ran.py",
        "M\ttest/helpers_unmapped.py",
    ],
)
def test_every_unmappable_change_falls_back_to_the_full_suite(diff: str) -> None:
    result = _select("M\tsrc/kiro_crew/a.py\n" + diff)
    assert result.full and result.tests == [] and result.reasons
    assert ctm.share(result, MAP) == 1.0


def test_an_empty_guard_tier_forfeits_the_reduction() -> None:
    assert _select("M\tsrc/kiro_crew/a.py", tier=[]).full


def test_a_mapped_edit_selects_its_tests_and_the_guard_tier() -> None:
    assert _select("M\tsrc/kiro_crew/b.py") == ctm.Selection(
        False, ["test/test_b.py", "test/test_gate.py"], []
    )
    assert _select("M\ttest/helpers_x.py").tests == ["test/test_b.py", "test/test_gate.py"]
    # A changed test file runs itself even when the map never saw it.
    assert _select("M\ttest/test_new_but_modified.py").tests == [
        "test/test_gate.py",
        "test/test_new_but_modified.py",
    ]
    assert ctm.share(_select("M\tsrc/kiro_crew/b.py"), MAP) == pytest.approx(2 / 3)


def test_select_cli_prints_files_or_exits_full(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(MAP), encoding="utf-8")
    diff = tmp_path / "diff.txt"
    diff.write_text("A\tsrc/kiro_crew/x.py\n", encoding="utf-8")
    argv = ["--root", str(ROOT), "select", "--map", str(map_path), "--name-status", str(diff)]
    assert ctm.main(argv) == ctm.FULL
    assert "file set changed" in capsys.readouterr().err
    assert ctm.main([*argv[:-2], "--base", "x"]) == 2


# The base of src/kiro_crew/a.py. MAP records f run by test_a, g by both, and g's
# inner and cached by test_b.
A_BASE = '''"""Module a."""

LIMIT = 3


def f():
    """Doc of f."""
    spare = 1
    return LIMIT
REGISTRY = [f]


def g():
    def inner():
        return 2
    return inner()


def unused():
    return 0


@functools.cache
def cached():
    return 4
'''
FILE_LEVEL = ["test/test_a.py", "test/test_b.py", "test/test_gate.py"]


def _commit(repo: Path, text: str) -> str:
    (repo / "src" / "kiro_crew").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "kiro_crew" / "a.py").write_text(text, encoding="utf-8")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for argv in (["add", "."], [*identity, "commit", "-qm", "c"], ["rev-parse", "HEAD"]):
        done = subprocess.run(
            ["git", *argv],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
        )
    return done.stdout.strip()


@pytest.mark.parametrize(
    "old, new, selected",
    [
        ("return LIMIT", "return LIMIT + 1", ["test/test_a.py", "test/test_gate.py"]),
        # A removed line has no head position: it is placed in the base file.
        ("    spare = 1\n", "", ["test/test_a.py", "test/test_gate.py"]),
        # The innermost function: only test_b ran inner, though test_a ran g.
        ("return 2", "return 3", ["test/test_b.py", "test/test_gate.py"]),
        ("LIMIT = 3", "LIMIT = 4", FILE_LEVEL),  # module level
        ("def g():", "def h():", FILE_LEVEL),  # renamed
        ("Doc of f.", "Doc.", FILE_LEVEL),  # a docstring is read without calling f
        ("return 0", "return 1", FILE_LEVEL),  # no test ran it
        ("return 4", "return 5", FILE_LEVEL),  # later callers read the cached result
        # One hunk that also removes module-level code.
        ("    return LIMIT\nREGISTRY = [f]\n", "    return LIMIT + 1\n", FILE_LEVEL),
    ],
)
def test_an_edit_inside_one_function_selects_only_the_tests_that_ran_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    old: str,
    new: str,
    selected: list[str],
) -> None:
    # No host git config reaches the temp repo, and no repository above it is found.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(ctm, "guard_tier", lambda root: TIER)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    base = _commit(repo, A_BASE)
    assert old in A_BASE
    head = _commit(repo, A_BASE.replace(old, new))
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(MAP), encoding="utf-8")
    argv = ["--root", str(repo), "select", "--map", str(map_path), "--base", base, "--head", head]
    assert ctm.main(argv) == 0
    assert capsys.readouterr().out.splitlines() == selected


def test_core_lists_the_src_files_linked_to_over_half_the_test_files(tmp_path: Path) -> None:
    from scripts import ci_test_file_map

    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(MAP), encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    assert ctm.main(["--root", str(tmp_path), "core", "--map", str(map_path)]) == 0
    # a.py reaches both test files; b.py, one of two, is not over half.
    assert ctm.core_files(MAP) == ["src/kiro_crew/a.py"]
    # The recorder reads the list from the path the command writes, and the
    # builder reads the recorder's import-time key.
    assert ci_test_file_map.CORE_LIST == ctm.CORE_LIST
    assert ci_test_file_map.IMPORT_TIME == ctm.IMPORT_TIME
    assert ci_test_file_map._core_files(tmp_path / ctm.CORE_LIST) == {"src/kiro_crew/a.py"}


def test_guard_tier_is_the_leaf_scope_corpus_gates() -> None:
    from scripts.leaf_test_scope import corpus_gates

    tier = ctm.guard_tier(ROOT)
    assert tier and tier == corpus_gates(ROOT)
    assert ctm.tier_lines(tier, None) == sorted(tier)


def _guard_tier_tree(root: Path, *gates: str) -> list[str]:
    """A tree whose *gates* read the test tree and whose one leaf does not: its argv."""
    test_dir = root / "test"
    test_dir.mkdir()
    for name in gates:
        # Formatted, so this module's own text does not read as a test-tree join.
        text = "FILES = ROOT / {!r}\n".format("test")
        (test_dir / name).write_text(text, encoding="utf-8")
    (test_dir / "test_leaf.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    return ["--root", str(root), "guard-tier"]


def test_guard_tier_prints_posix_paths_in_code_point_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `B` sorts before `a` by code point and after it when case is folded.
    argv = _guard_tier_tree(tmp_path, "test_a.py", "test_B.py", "test_gate.py")
    assert ctm.tracked_tests(tmp_path) is None
    assert ctm.main(argv) == 0
    assert capsys.readouterr().out.splitlines() == [
        "test/test_B.py",
        "test/test_a.py",
        "test/test_gate.py",
    ]


def test_guard_tier_leaves_out_a_test_file_git_does_not_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _guard_tier_tree(tmp_path, "test_gate.py", "test_scratch.py")
    index = ctm.parse_ls_files(b"test/test_gate.py\0test/test_leaf.py\0")
    assert index == {"test/test_gate.py", "test/test_leaf.py"}
    monkeypatch.setattr(ctm, "tracked_tests", lambda root: index)
    assert ctm.main(argv) == 0
    assert capsys.readouterr().out.splitlines() == ["test/test_gate.py"]


def test_guard_tier_never_prints_an_empty_tier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ctm.main(_guard_tier_tree(tmp_path)) == 2
    printed = capsys.readouterr()
    assert printed.out == "" and "guard tier is empty" in printed.err


def test_shadow_judge_counts_a_missed_failing_file() -> None:
    row = shadow.judge(
        "M\tsrc/kiro_crew/b.py\n", MAP, TIER, failing=["test/test_a.py", "test/test_b.py"]
    )
    assert row["missed"] == ["test/test_a.py"] and not row["full"]
    gate = shadow.judge("M\tsrc/kiro_crew/b.py\n", MAP, TIER, failing=["test/test_gate.py"])
    assert gate["missed"] == [] and gate["missed_by_map"] == ["test/test_gate.py"]
    # A changed test file that is also in the tier was picked on its own.
    edited = shadow.judge("M\ttest/test_gate.py\n", MAP, TIER, failing=["test/test_gate.py"])
    assert edited["missed_by_map"] == [] and edited["map_share"] == 0.0
    full = shadow.judge("A\tsrc/kiro_crew/x.py\n", MAP, TIER, failing=["test/test_a.py"])
    assert full["missed"] == [] and full["share"] == 1.0
    # A function-level selection is judged as it selects: inner ran in test_b only.
    narrowed = shadow.judge(
        "M\tsrc/kiro_crew/a.py\n",
        MAP,
        TIER,
        failing=["test/test_a.py"],
        functions={"src/kiro_crew/a.py": {"g.<locals>.inner"}},
    )
    assert narrowed["missed"] == ["test/test_a.py"]


def test_shadow_red_row_reads_only_failed_linux_backend_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        {"id": 1, "name": "Backend Tests (3.12, 2)", "conclusion": "failure"},
        {"id": 2, "name": "Backend Tests (Windows) (2)", "conclusion": "failure"},
        {"id": 3, "name": "Backend Tests (3.12, 3)", "conclusion": "success"},
    ]
    fetched: list[list[str]] = []
    listed: list[list[str]] = []
    page = {"total_count": len(jobs), "jobs": jobs}
    monkeypatch.setattr(shadow, "_gh_json", lambda args, root: listed.append(args) or page)

    def fake_run(argv: list[str], root: Path, most: int) -> bytes:
        fetched.append(argv)
        assert most == shadow.MAX_LOG_BYTES
        return b"2026-11-18T00:00:00.0Z FAILED test/test_a.py::test_one - assert 1 == 2\n"

    monkeypatch.setattr(shadow, "_gh_bytes", fake_run)
    run = {"id": 9, "run_attempt": 2, "head_sha": "f" * 40, "pull_requests": [{"number": 7}]}
    row = shadow._red_row(run, ROOT, shadow._nodeid_parser())
    assert row == {"run": 9, "pr": 7, "head": "f" * 40, "failing": ["test/test_a.py"]}
    assert [argv[-1] for argv in fetched] == [f"repos/{shadow.REPO}/actions/jobs/1/logs"]
    # The jobs of THIS attempt: a rerun keeps the run id.
    assert listed[0][1] == f"repos/{shadow.REPO}/actions/runs/9/attempts/2/jobs?per_page=100&page=1"

    def unreadable(argv: list[str], root: Path, most: int) -> bytes:
        raise subprocess.CalledProcessError(1, argv)

    # One unreadable shard log leaves the sample incomplete: none, not a partial row.
    monkeypatch.setattr(shadow, "_gh_bytes", unreadable)
    assert shadow._red_row(run, ROOT, shadow._nodeid_parser()) is None
    # So does one larger than is ever held: it is cut off at the limit, not read whole.
    monkeypatch.setattr(shadow, "_gh_bytes", lambda argv, root, most: None)
    assert shadow._red_row(run, ROOT, shadow._nodeid_parser()) is None


@pytest.mark.parametrize("size", [0, 1, 7, 8, 9, 64])
def test_a_stream_is_held_to_its_limit_and_no_further(size: int) -> None:
    most = 8

    class Counting(io.BytesIO):
        asked = 0

        def read(self, wanted: int | None = -1) -> bytes:
            assert wanted is not None and wanted >= 0
            Counting.asked += wanted
            return super().read(wanted)

    held = shadow.capped(Counting(b"x" * size), most)
    assert held == (b"x" * size if size <= most else None)
    # One byte past the limit says it is over; nothing beyond that is ever asked for.
    assert Counting.asked == most + 1


def test_shadow_summary_states_both_invariants() -> None:
    merged = [
        {"full": False, "share": 0.25, "map_share": 0.05, "reasons": []},
        {"full": True, "share": 1.0, "map_share": 1.0, "reasons": ["x: file set changed"]},
    ]
    red = [
        {"full": False, "missed": [], "missed_by_map": ["t"], "shared": False},
        {"full": False, "missed": ["t"], "missed_by_map": ["t"], "shared": True},
    ]
    text = shadow.summarize(merged, red, MAP)
    assert "Merged PRs: 2. Reduced (not full suite): 1. Median selected share: 62.5%." in text
    assert "median share 25.0% with the guard tier, 5.0% from the map alone" in text
    assert "Red PR pushes: 2. Every failing test file selected: 1 (50%)" in text
    assert "1 caught every failing file (0 from the map alone)" in text


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_is_shadow_only_and_cannot_be_cancelled_by_main() -> None:
    doc = _workflow()
    triggers = doc.get("on") or doc.get(True)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False
    for job in doc["jobs"].values():
        assert "permissions" not in job
        for step in job["steps"]:
            if "actions/checkout@" in str(step.get("uses", "")):
                assert step["with"]["persist-credentials"] is False


def test_only_the_shadow_workflow_loads_the_recorder() -> None:
    assert "-p scripts.ci_test_file_map" in WORKFLOW.read_text(encoding="utf-8")
    for path in [
        ROOT / "setup.cfg",
        ROOT / "pyproject.toml",
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
    ]:
        if path != WORKFLOW:
            assert "ci_test_file_map" not in path.read_text(encoding="utf-8"), path


SCRIPT = "scripts/ci_test_map.py"
# The retention the publishing issue asks for at least: a reader must still find
# a bundle after a run of red nights.
MIN_RETENTION_DAYS = 14


def _map_job_calls() -> dict[str, tuple[int, dict[str, Any], list[str]]]:
    """Each subcommand of the script the `map` job runs: its step index, step and argv."""
    calls: dict[str, tuple[int, dict[str, Any], list[str]]] = {}
    for index, step in enumerate(_workflow()["jobs"]["map"]["steps"]):
        for line in str(step.get("run", "")).splitlines():
            words = list(shlex.shlex(line, posix=True, punctuation_chars=True))
            if SCRIPT not in words:
                continue
            argv = words[words.index(SCRIPT) + 1 :]
            piped = "|" in argv
            argv = argv[: argv.index("|")] if piped else argv
            assert argv[0] not in calls, f"{argv[0]} runs twice"
            # Without pipefail a pipe ends green on its last command's status.
            assert not piped or step.get("shell") == "bash", line
            calls[argv[0]] = (index, step, argv)
    return calls


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _tables_reported_missing(printed: str) -> set[str]:
    """The bundle tables `check` printed an error for."""
    return {t for t in ctm.BUNDLE_TABLES if f"::error::the bundle has no {t} table" in printed}


def test_map_job_publishes_the_checked_bundle_as_its_one_artifact() -> None:
    steps = _workflow()["jobs"]["map"]["steps"]
    calls = _map_job_calls()
    uploads = [
        (i, s) for i, s in enumerate(steps) if "actions/upload-artifact@" in str(s.get("uses", ""))
    ]
    assert len(uploads) == 1
    at, upload = uploads[0]
    # What is built is what is bundled; what is bundled is what is checked and uploaded.
    assert _option(calls["bundle"][2], "--map") == _option(calls["build"][2], "--out")
    bundle_out = _option(calls["bundle"][2], "--out")
    assert _option(calls["check"][2], "--bundle") == bundle_out == upload["with"]["path"]
    order = [calls[name][0] for name in ("build", "bundle", "check")]
    assert order == sorted(order) and order[-1] < at
    # Nothing lets the upload run after a failed check, or a failed check pass.
    for step in (upload, *(calls[name][1] for name in calls)):
        assert "if" not in step and "continue-on-error" not in step
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] >= MIN_RETENTION_DAYS
    # The checkout holds the window `bundle` reads by default, and the commit before it.
    assert "--commits" not in calls["bundle"][2] and "--ref" not in calls["bundle"][2]
    depths = [
        s["with"].get("fetch-depth", 1)
        for s in steps[: calls["bundle"][0]]
        if "actions/checkout@" in str(s.get("uses", ""))
    ]
    assert len(depths) == 1
    assert depths[0] == 0 or depths[0] > ctm.CO_CHANGE_COMMITS


def test_map_job_commands_pass_only_on_a_clone_that_holds_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path)
    calls = _map_job_calls()
    window = 2
    monkeypatch.setattr(ctm, "CO_CHANGE_COMMITS", window)
    monkeypatch.setattr(ctm, "GIT_LOG_TIMEOUT_SECS", FALLBACK_TIMEOUT_SECS)
    map_name = _option(calls["bundle"][2], "--map")
    out_name = _option(calls["bundle"][2], "--out")
    works = iter(tmp_path / f"work-{n}" for n in itertools.count())

    def run(root: Path, tier: list[str]) -> tuple[int, str, Path]:
        """The job's own `bundle` and `check` lines against `root`, in a new directory."""
        work = next(works)
        work.mkdir()
        (work / map_name).write_text(json.dumps(MAP), encoding="utf-8")
        monkeypatch.chdir(work)
        monkeypatch.setattr(ctm, "guard_tier", lambda root: tier)
        assert ctm.main(["--root", str(root), *calls["bundle"][2]]) == 0
        capsys.readouterr()
        status = ctm.main(["--root", str(root), *calls["check"][2]])
        if status == 0:
            doc = ctm.load_map(work / out_name)
            assert all(doc[table] for table in ctm.BUNDLE_TABLES)
        return status, capsys.readouterr().out, work / out_name

    # The fixture has four commits: a shallow clone passes from `window + 1` commits up.
    for depth in range(1, 5):
        status, printed, _ = run(_shallow_clone(repo, depth), TIER)
        assert status == (0 if depth > window else 1), depth
        assert _tables_reported_missing(printed) == ({"cochange"} if depth <= window else set())
    status, printed, written = run(repo, TIER)
    assert status == 0 and "::error::" not in printed
    # The summary line: the commit, the build time, the file's size and each table's rows.
    assert f"commit `{MAP['commit']}`, built_at {ctm.load_map(written)['built_at']}," in printed
    assert f"{written.stat().st_size} bytes" in printed
    rows = ctm.bundle_rows(ctm.load_map(written))
    assert all(f"{name} {count}" in printed for name, count in rows.items())
    assert set(rows) > set(ctm.BUNDLE_TABLES) and all(rows.values())
    # A dead guard scan fails the job as a short history does.
    status, printed, _ = run(repo, [])
    assert status == 1 and _tables_reported_missing(printed) == {"guard"}


def test_check_fails_on_every_bundle_that_lacks_a_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    whole = {**MAP, **{table: ["row"] for table in ctm.BUNDLE_TABLES}}
    path = tmp_path / "bundle.json.gz"
    for count in range(len(ctm.BUNDLE_TABLES) + 1):
        for gone in itertools.combinations(ctm.BUNDLE_TABLES, count):
            # Absent and empty read the same: neither holds a row.
            for doc in (
                {k: v for k, v in whole.items() if k not in gone},
                {k: (type(v)() if k in gone else v) for k, v in whole.items()},
            ):
                path.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8")))
                assert ctm.main(["check", "--bundle", str(path)]) == (1 if gone else 0)
                printed = capsys.readouterr().out
                assert _tables_reported_missing(printed) == set(gone)
    # The build time is printed when the file has one, and decides nothing.
    for extra in ({}, {"built_at": "not a time"}):
        path.write_bytes(gzip.compress(json.dumps({**whole, **extra}).encode("utf-8")))
        assert ctm.main(["check", "--bundle", str(path)]) == 0
        assert ("built_at not a time" in capsys.readouterr().out) == bool(extra)
    # A file that is not a readable schema-1 map is a usage error, never a pass.
    data = path.read_bytes()
    for damaged in (data[: len(data) // 2], b"{}", gzip.compress(b"[]")):
        path.write_bytes(damaged)
        assert ctm.main(["check", "--bundle", str(path)]) == 2
    assert ctm.main(["check", "--bundle", str(tmp_path / "absent.json.gz")]) == 2

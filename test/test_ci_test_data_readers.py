"""Contract tests for the readers of a changed doc or frontend path in the SHADOW selection.

`scripts/ci_test_data_readers.py` names the test files that read a changed prose
doc or frontend file; `ci_test_map.select`, the scorer and its Windows block add
them to the selection. Pinned: the rule is `docs_only`'s and ci.yml's, held by
reference; a covered path, added, deleted, modified or renamed, changes nothing
but the readers it adds; every other tracked path keeps the verdict it has with
no rule; the readers are never skipped, on Linux or on Windows; and the verdict
says which statuses it covered, which the miss ledger keeps.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from macos_lane_helpers import load_workflow

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_test_selection as cts
from scripts import ci_test_selection_ledger as ledger

ctm = cts.ctm
win = cts.windows
readers = cts.data_readers
ROOT = readers.REPO_ROOT
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

TESTS = {
    "test/test_a.py": 1,
    "test/test_b.py": 2,
    "test/test_gate.py": 1,
    "test/test_idle.py": 8,
    "test/test_reads.py": 4,
}
MAP: dict[str, Any] = {
    "tests": TESTS,
    "files": {"src/kiro_crew/a.py": [0], "src/kiro_crew/b.py": [1]},
}
GUARD = ["test/test_gate.py"]
FOUND = {"test/test_reads.py", "test/test_unmapped_reader.py"}
# Diffs `select` narrows with no rule, and diffs it sends to the full suite.
NARROWED = ["", "M\tsrc/kiro_crew/a.py\n", "M\tsrc/kiro_crew/a.py\nM\ttest/test_b.py\n"]
FULL = [
    "A\tsrc/kiro_crew/new.py\n",
    "M\tsetup.cfg\n",
    "M\ttest/conftest.py\n",
    "M\tsrc/kiro_crew/never_ran.py\n",
    "M\tsrc/kiro_crew/a.py\nD\tsrc/kiro_crew/b.py\n",
]
# Covered rows of each status that leaves the fall-back rules, and what the verdict says of them.
DATA = {
    "M\tdata/guide.md\n": {"M": 1},
    "A\tdata/new.md\nD\tdata/old.md\nM\tdata/deep/mod.py\nA\tdata/newer.md\n": {
        "A": 2,
        "D": 1,
        "M": 1,
    },
    "R100\tdata/guide.md\tdata/moved.md\nA\tdata/other.md\n": {"A": 1, "R": 1},
}


class _Rule:
    """Covers `data/**`; records every scan it is asked for."""

    def __init__(self, found: Iterable[str] = FOUND) -> None:
        self.found = set(found)
        self.asked: list[list[str]] = []

    def covers(self, path: str) -> bool:
        return path.startswith("data/")

    def tests(self, paths: Iterable[str]) -> set[str]:
        self.asked.append(sorted(paths))
        return set(self.found)


def _paths(diff: str) -> list[str]:
    return sorted(path for _status, paths in ctm.parse_name_status(diff) for path in paths)


def _select(diff: str, rule: _Rule | None = None, guard: list[str] = GUARD) -> Any:
    return ctm.select(ctm.parse_name_status(diff), MAP, guard, rule=rule)


@pytest.mark.parametrize("data", DATA)
@pytest.mark.parametrize("rest", NARROWED)
def test_a_covered_path_adds_its_readers_and_changes_nothing_else(rest: str, data: str) -> None:
    without = _select(rest)
    assert not without.full and without.readers == ()
    for diff in (rest + data, data + rest):
        rule = _Rule()
        result = _select(diff, rule)
        assert not result.full and result.reasons == []
        assert result.tests == sorted(set(without.tests) | FOUND)
        assert result.readers == tuple(sorted(FOUND))
        assert rule.asked == [_paths(data)]
    # With no rule the same rows send the diff to the full suite.
    assert _select(rest + data).full


@pytest.mark.parametrize("data", DATA)
@pytest.mark.parametrize("rest", FULL)
def test_a_covered_path_never_narrows_a_diff_that_falls_back(rest: str, data: str) -> None:
    rule = _Rule()
    result = _select(rest + data, rule)
    assert result == _select(rest) and result.full and result.reasons
    # The scans read the whole test tree, so a diff that is full anyway runs none.
    assert rule.asked == []
    empty_guard = _select(data, rule, guard=[])
    assert empty_guard.full and rule.asked == []


def test_the_covered_statuses_are_the_four_the_verdict_names() -> None:
    assert ctm.COVERED_STATUSES == {status for counts in DATA.values() for status in counts}


@pytest.mark.parametrize(
    "row",
    [
        # A status outside the four.
        "T\tdata/guide.md\n",
        "C90\tdata/guide.md\tdata/copy.md\n",
        # A rename with one name outside the covered set, either side.
        "R100\tdata/guide.md\tsrc/kiro_crew/guide.md\n",
        "R90\tsetup.cfg\tdata/setup.md\n",
    ],
)
def test_a_row_the_rule_does_not_cover_whole_keeps_the_fall_back_rules(row: str) -> None:
    rule = _Rule()
    for rest in NARROWED:
        result = _select(rest + row, rule)
        assert result.full and result == _select(rest + row)
    assert rule.asked == []


def test_a_scan_that_names_no_reader_forfeits_the_reduction() -> None:
    result = _select("M\tsrc/kiro_crew/a.py\nM\tdata/guide.md\n", _Rule(found=()))
    assert result.full and result.tests == []
    assert result.reasons == [
        "data/guide.md: its reader scan found no test file: refusing to reduce"
    ]


def test_a_diff_of_covered_paths_alone_selects_the_guard_tier_and_the_readers() -> None:
    result = _select(next(iter(DATA)), _Rule())
    assert result == ctm.Selection(False, sorted(set(GUARD) | FOUND), [], tuple(sorted(FOUND)))
    total = sum(TESTS.values())
    assert ctm.share(result, MAP) == pytest.approx((TESTS["test/test_gate.py"] + 4) / total)


# ── The scorer and its Windows block ──

BUNDLE: dict[str, Any] = {
    "schema": 1,
    "commit": "abc",
    "built_at": NOW.isoformat(),
    **MAP,
    "cochange": {},
    "os_sensitive": {"src/kiro_crew/a.py": "W2"},
    "windows_targeting": ["test/test_b.py"],
}
UNIVERSE = sorted(TESTS)


def _tiered(
    changes: list[tuple[str, list[str]]],
    rule: _Rule | None = None,
    expect: list[str] | None = None,
) -> dict[str, Any]:
    def grep(modules: list[str]) -> set[str]:
        assert expect is None or modules == expect
        return set()

    return cts.score(
        changes,
        BUNDLE,
        UNIVERSE,
        GUARD,
        skippable={"G"},
        narrow_reach_max=20,
        grep=grep,
        rule=rule,
    )


def _score(diff: str, rule: _Rule | None = None, expect: list[str] | None = None) -> dict[str, Any]:
    return _tiered(ctm.parse_name_status(diff), rule, expect)


@pytest.mark.parametrize("data", DATA)
def test_the_scorer_runs_every_reader_and_reads_no_other_signal_from_a_covered_path(
    data: str,
) -> None:
    rest = "M\tsrc/kiro_crew/a.py\n"
    before = _score(rest)
    assert "test/test_reads.py" in before["would_skip"]
    # The modules searched for are the rest's: a covered `.py` path names none.
    after = _score(rest + data, _Rule(), expect=["kiro_crew.a"])
    assert after["verdict"] == "select" and after["reason"] is None
    assert set(after["tiers"]["A"]) == set(before["tiers"]["A"]) | FOUND
    assert not FOUND & set(after["would_skip"])
    for tier in cts.TIERS[1:]:
        assert set(after["tiers"][tier]) == set(before["tiers"][tier]) - FOUND
    assert set(after["would_skip"]) == set(before["would_skip"]) - FOUND
    assert after["would_run_share"] > before["would_run_share"]
    # The statuses that let the diff be narrowed, as the miss ledger reads them.
    assert before["covered"] == {} and after["covered"] == DATA[data]
    parsed = ledger.parse_verdict(json.dumps({"schema": 1, **after}))
    assert parsed is not None and parsed["covered"] == DATA[data]
    key = {"run": 1, "attempt": 1, "source": "1:b", "fork": False, "pr": 7}
    key.update(head="a" * 40, date="2026-11-20T12:00:00Z")
    held = ledger.run_rows(key, parsed, {}, set())
    assert held[0]["covered"] == DATA[data] and ledger.valid_row(held[0])


def test_the_scorer_falls_back_as_select_does_and_runs_no_scan_then() -> None:
    rule = _Rule()
    data = next(iter(DATA))
    for rest in FULL:
        fallen = _score(rest + data, rule)
        assert fallen == _score(rest) and fallen["covered"] == {}
    unread = _score(data, _Rule(found=()))
    assert unread["verdict"] == "full" and unread["covered"] == {}
    assert rule.asked == []


def _block(diff: str, rule: _Rule | None, **over: Any) -> dict[str, Any]:
    changes = ctm.parse_name_status(diff)
    args: dict[str, Any] = {"added": {}, "bucket": ["**/*windows*"], "ignored": set(), "at": NOW}
    args.update(over)
    data = [path for _status, paths in ctm.data_rows(changes, rule)[1] for path in paths]
    return win.block(
        changes,
        _score(diff, rule),
        BUNDLE,
        UNIVERSE,
        GUARD,
        score=lambda part: _tiered(part, rule),
        readers=rule.tests(data) if rule is not None and data else (),
        **args,
    )


def test_the_windows_block_runs_the_readers_less_what_windows_never_collects() -> None:
    rest = "M\tsrc/kiro_crew/a.py\n"
    before = _block(rest, None)
    data = next(iter(DATA))
    after = _block(rest + data, _Rule())
    assert before["verdict"] == after["verdict"] == "select"
    assert set(after["run"]) == set(before["run"]) | FOUND
    ignored = {"test/test_reads.py"}
    assert set(_block(rest + data, _Rule(), ignored=ignored)["run"]) == set(after["run"]) - ignored
    # A covered path is still a path: one in the Windows bucket runs everything there.
    named = _block(rest + "M\tdata/windows_notes.md\n", _Rule())
    assert named["verdict"] == "full" and named["reason"].startswith("windows_path: ")


# ── The rule itself, against the repository ──


@pytest.fixture(scope="module")
def rule() -> Any:
    return readers.DataRule(ROOT)


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached"],
        check=True,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    return sorted(ctm.parse_ls_files(listed.stdout))


def _ci_frontend() -> list[str]:
    steps = load_workflow("ci.yml")["jobs"]["changes"]["steps"]
    filters = next(step["with"]["filters"] for step in steps if step.get("id") == "filter")
    return __import__("yaml").safe_load(filters)[readers.FRONTEND_BUCKET]


def _mapped(path: str) -> bool:
    """A path the map can record: `select` judges it by its links, not by the rule."""
    return path.endswith(".py") and path.startswith(("src/", "test/"))


def test_a_covered_path_is_a_prose_doc_outside_the_package_or_a_frontend_path(
    rule: Any, tracked: list[str]
) -> None:
    globs = [win.glob_pattern(glob) for glob in _ci_frontend()]
    assert globs

    def frontend(path: str) -> bool:
        return any(glob.fullmatch(path) for glob in globs)

    docs = {p for p in tracked if rule.docs.is_prose_doc(p)}
    shipped = {p for p in docs if p.startswith(readers.PACKAGE_TREE)}
    covered = {p for p in tracked if rule.covers(p)}
    assert covered == (docs - shipped) | {p for p in tracked if frontend(p)}
    # Each kind is in the tree, so none of the three statements above is empty.
    assert shipped and docs - shipped and covered - docs and set(tracked) - covered - docs


def test_every_uncovered_path_keeps_the_verdict_it_has_with_no_rule(
    rule: Any, tracked: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_scan(paths: Iterable[str]) -> set[str]:
        raise AssertionError(f"scanned for {sorted(paths)}")

    monkeypatch.setattr(rule, "tests", no_scan)
    uncovered = [p for p in tracked if not rule.covers(p) and not _mapped(p)]
    # One path of each folder and suffix, every packaged doc, and every file
    # under a doc tree that is not prose.
    kinds = {(p.split("/")[0], Path(p).suffix): p for p in uncovered}
    packaged = [p for p in uncovered if rule.docs.is_prose_doc(p)]
    trees = tuple(tree for tree in rule.docs._DOC_TREES if not tree.startswith("website/"))
    not_prose = [p for p in uncovered if p.startswith(trees) and not rule.docs.is_prose_doc(p)]
    skills = [p for p in uncovered if p.startswith("src/") and p.endswith("/SKILL.md")]
    assert packaged and not_prose and skills and len(kinds) > len(trees)
    for path in sorted({*kinds.values(), *packaged, *not_prose, *skills}):
        for status in sorted(ctm.COVERED_STATUSES - {"R"}):
            changes = [("M", ["src/kiro_crew/a.py"]), (status, [path])]
            result = ctm.select(changes, MAP, GUARD, rule=rule)
            assert result.full and result == ctm.select(changes, MAP, GUARD), path
            assert any(path in reason for reason in result.reasons), path


def test_the_rule_is_docs_onlys_and_ci_ymls_not_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = readers.DataRule(ROOT)
    assert rule.covers("docs/guide.md") and not rule.covers("odd.bin")
    monkeypatch.setattr(rule.docs, "is_prose_doc", lambda path: path == "odd.bin")
    assert rule.covers("odd.bin") and not rule.covers("docs/guide.md")
    # The frontend globs are the checkout's ci.yml's, whatever they say.
    workflow = tmp_path / win.CI_WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text("  frontend:\n    - 'web/**'\n  meta:\n", encoding="utf-8")
    moved = readers.DataRule(tmp_path)
    assert moved.covers("web/src/a.ts") and not moved.covers("website/src/a.ts")
    assert win.path_bucket(ROOT, readers.FRONTEND_BUCKET) == _ci_frontend()
    # A filter that cannot be read covers nothing; a doc is still a doc.
    workflow.unlink()
    unread = readers.DataRule(tmp_path)
    assert not unread.covers("web/src/a.ts") and not unread.covers("website/src/a.ts")
    assert unread.covers("docs/guide.md")


def test_the_readers_are_the_lists_ci_runs_for_such_a_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = readers.DataRule(ROOT)
    calls: list[tuple[Any, ...]] = []

    def doc_readers(changed: list[str], root: str) -> list[str]:
        calls.append(("docs", changed, root))
        return ["test/test_reads_docs.py", "website/src/reads.test.ts"]

    def collect(surface: str) -> list[str]:
        calls.append(("surface", surface))
        return ["test/test_reads_ui.py"]

    monkeypatch.setattr(rule.docs, "readers", doc_readers)
    monkeypatch.setattr(rule.surface, "collect", collect)
    doc, page, both = "docs/guide.md", "website/src/a.ts", "website/docs/theme.md"
    # A frontend spec among a doc's readers is no backend test file.
    assert rule.tests([doc]) == {"test/test_reads_docs.py"}
    assert calls == [("docs", [doc], str(ROOT))]
    assert rule.tests([page]) == {"test/test_reads_ui.py"}
    assert calls[1:] == [("surface", readers.BACKEND_SURFACE)]
    assert rule.covers(both) and rule.tests([both]) == rule.tests([doc]) | rule.tests([page])
    # One scan per question: asked again, an instance answers from what it holds.
    assert rule.tests([doc, page]) == rule.tests([both])
    assert [call[0] for call in calls] == ["docs", "surface", "docs"]


# ── End to end, on a small repository ──

CI_YML = "  frontend:\n    - 'website/**'\n  windows:\n    - '**/*windows*'\n  meta:\n"
TREE = {
    win.CI_WORKFLOW: CI_YML,
    "src/kiro_crew/a.py": "",
    "docs/guide.md": "one\n",
    "website/src/a.ts": "one\n",
    "test/test_a.py": "",
    "test/test_gate.py": "",
    "test/test_idle.py": "",
    "test/test_reads_docs.py": 'GUIDE = "docs/guide.md"\n',
    "test/test_reads_ui.py": 'PAGE = "website/src/a.ts"\n',
}


def _git(root: Path, *argv: str) -> str:
    stamp = NOW.isoformat()
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    for rel, text in TREE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    # No repository above `tmp_path` is found, wherever the host's tmp dir sits.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


def _verdict(repo: Path, tmp_path: Path, edits: dict[str, str]) -> dict[str, Any]:
    for rel, text in edits.items():
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    bundle = {
        "schema": 1,
        "commit": "c0ffee",
        "built_at": (NOW - timedelta(days=1)).isoformat(),
        "tests": {"test/test_a.py": 1, "test/test_idle.py": 8},
        "files": {"src/kiro_crew/a.py": [0]},
        "cochange": {"src/kiro_crew/a.py": ["test/test_a.py"]},
        "guard": ["test/test_gate.py"],
        "os_sensitive": {"src/kiro_crew/other.py": "W1"},
        "windows_targeting": ["test/test_idle.py"],
    }
    (tmp_path / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    out = tmp_path / "verdict.json"
    argv = ["--root", str(repo), "--bundle", str(tmp_path / "bundle.json")]
    assert cts.main([*argv, "--base", "HEAD~1", "--head", "HEAD", "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_doc_edit_beside_a_code_edit_runs_the_docs_readers_on_both_systems(
    repo: Path, tmp_path: Path
) -> None:
    edits = {"src/kiro_crew/a.py": "x = 1\n", "docs/guide.md": "two\n", "docs/new.md": "one\n"}
    verdict = _verdict(repo, tmp_path, edits)
    assert verdict["verdict"] == "select" and verdict["reason"] is None
    assert verdict["covered"] == {"A": 1, "M": 1}
    # The reader the real scan finds in this tree; with no reader rule it has no signal.
    assert verdict["tiers"]["A"] == ["test/test_gate.py", "test/test_reads_docs.py"]
    assert verdict["would_skip"] == ["test/test_idle.py", "test/test_reads_ui.py"]
    assert verdict["windows"]["verdict"] == "select"
    assert verdict["windows"]["run"] == ["test/test_gate.py", "test/test_reads_docs.py"]


def test_a_frontend_edit_runs_the_cross_surface_list_of_its_own_tree_only(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edits = {"src/kiro_crew/a.py": "x = 1\n", "website/src/a.ts": "two\n"}
    # `ci-surface-tests.py` lists the tree it sits in: for another one, everything runs.
    refused = _verdict(repo, tmp_path, edits)
    assert refused["verdict"] == refused["windows"]["verdict"] == "full"
    assert "reads its own tree" in refused["reason"]

    class Rooted(readers.DataRule):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.surface._repo_root = lambda: root

    monkeypatch.setattr(readers, "DataRule", Rooted)
    out = tmp_path / "verdict.json"
    argv = ["--root", str(repo), "--bundle", str(tmp_path / "bundle.json")]
    assert cts.main([*argv, "--base", "HEAD~1", "--head", "HEAD", "--out", str(out)]) == 0
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["verdict"] == "select" and verdict["covered"] == {"M": 1}
    assert verdict["tiers"]["A"] == ["test/test_gate.py", "test/test_reads_ui.py"]
    assert verdict["would_skip"] == ["test/test_idle.py", "test/test_reads_docs.py"]
    assert verdict["windows"]["run"] == ["test/test_gate.py", "test/test_reads_ui.py"]


def test_the_select_command_prints_the_readers_of_a_changed_doc(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ctm, "guard_tier", lambda root: ["test/test_gate.py"])
    test_map = {"schema": 1, "tests": {"test/test_a.py": 1}, "files": {"src/kiro_crew/a.py": [0]}}
    (tmp_path / "map.json").write_text(json.dumps(test_map), encoding="utf-8")
    diff = tmp_path / "diff.txt"
    argv = ["--root", str(repo), "select", "--map", str(tmp_path / "map.json")]
    diff.write_text("M\tsrc/kiro_crew/a.py\nM\tdocs/guide.md\n", encoding="utf-8")
    assert ctm.main([*argv, "--name-status", str(diff)]) == 0
    assert capsys.readouterr().out.split() == [
        "test/test_a.py",
        "test/test_gate.py",
        "test/test_reads_docs.py",
    ]
    # A doc the diff adds has the same readers; one added to the package falls back.
    diff.write_text("A\tdocs/new.md\n", encoding="utf-8")
    assert ctm.main([*argv, "--name-status", str(diff)]) == 0
    assert capsys.readouterr().out.split() == ["test/test_gate.py", "test/test_reads_docs.py"]
    diff.write_text("A\tsrc/kiro_crew/docs/new.md\n", encoding="utf-8")
    assert ctm.main([*argv, "--name-status", str(diff)]) == ctm.FULL
    assert "full suite: A src/kiro_crew/docs/new.md: file set changed" in capsys.readouterr().err

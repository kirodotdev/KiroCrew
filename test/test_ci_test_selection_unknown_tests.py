"""Contract tests for the test files the test map has no entry for, in the SHADOW selection.

The map cannot say what a test file it never recorded runs, so no verdict may
skip one. Pinned: for every bundle and every diff with verdict `select`, no test
file absent from the bundle's `tests` table is in `would_skip`, and the Windows
block runs each one it collects; the verdict and `ci_test_map.py check` count
such files, new since the map apart from those the map's run recorded no node
from; a share weighs a new file the map's mean; and the nightly bundle writes
the table that tells the two kinds apart.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_test_selection as cts
from scripts import ci_test_selection_ledger as ledger

ctm = cts.ctm
win = cts.windows
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
KNOWN = {"test/test_a.py": 4, "test/test_gate.py": 2, "test/test_idle.py": 6}
MEAN = sum(KNOWN.values()) / len(KNOWN)
# Test files the map has no entry for, one of each way a file can sit beside a diff.
NEW = [
    "test/test_added_later.py",  # no signal at all
    "test/acp/test_beside.py",  # a folder the diff changes
    "test/test_acp_named.py",  # the changed file's package
    "src/kiro_crew/apps/builtins/x/tests/test_app.py",  # the other test path
]
NEVER_RUNS = ["test/test_skips_at_import.py", "test/container/test_other_lane.py"]
UNIVERSE = sorted([*KNOWN, *NEW, *NEVER_RUNS])
GUARD = ["test/test_gate.py"]
SELECT = [
    "M\tsrc/kiro_crew/a.py\n",
    "M\tsrc/kiro_crew/acp/mod.py\n",
    "M\ttest/test_idle.py\n",
    "M\tsrc/kiro_crew/a.py\nA\ttest/test_brand_new.py\n",
    "",
]


def _bundles() -> list[dict[str, Any]]:
    """Every bundle shape the scorer reads: with and without each table that is not the map."""
    base: dict[str, Any] = {
        "schema": 1,
        "commit": "abc",
        "built_at": NOW.isoformat(),
        "tests": KNOWN,
        "files": {"src/kiro_crew/a.py": [0], "src/kiro_crew/acp/mod.py": []},
        "os_sensitive": {"src/kiro_crew/other.py": "W1"},
        "windows_targeting": ["test/test_idle.py"],
    }
    extra = {"cochange": {}, ctm.UNRECORDED: NEVER_RUNS}
    return [
        {**base, **{key: extra[key] for key in keys}}
        for size in range(len(extra) + 1)
        for keys in itertools.combinations(sorted(extra), size)
    ]


BUNDLES = _bundles()


def _score(
    diff: str, bundle: dict[str, Any], skippable: frozenset[str] = frozenset("G")
) -> dict[str, Any]:
    return cts.score(
        ctm.parse_name_status(diff),
        bundle,
        UNIVERSE,
        GUARD,
        skippable=set(skippable),
        narrow_reach_max=20,
        grep=lambda modules: set(),
    )


def _absent(bundle: dict[str, Any], tests: list[str]) -> set[str]:
    return {test for test in tests if test not in bundle["tests"]}


@pytest.mark.parametrize("skippable", [frozenset("G"), frozenset(cts.TIERS[1:])])
@pytest.mark.parametrize("bundle", BUNDLES)
@pytest.mark.parametrize("diff", SELECT)
def test_no_verdict_skips_a_test_file_the_map_has_no_entry_for(
    diff: str, bundle: dict[str, Any], skippable: frozenset[str]
) -> None:
    verdict = _score(diff, bundle, skippable)
    assert verdict["verdict"] == "select"
    everything = {test for tier in cts.TIERS for test in verdict["tiers"][tier]}
    unknown = _absent(bundle, sorted(everything))
    assert {*NEW, *NEVER_RUNS} <= unknown
    # Whatever tier a signal gave it, and however many tiers are skippable.
    assert not unknown & set(verdict["would_skip"])
    assert not unknown & set(verdict["tiers"]["G"])
    # A file the map does know is still skipped when nothing ties it to the diff.
    if "test/test_idle.py" not in diff and "cochange" in bundle and "a.py" in diff:
        assert "test/test_idle.py" in verdict["would_skip"]


@pytest.mark.parametrize("bundle", BUNDLES)
@pytest.mark.parametrize("diff", SELECT)
def test_the_windows_block_runs_every_unknown_test_file_it_collects(
    diff: str, bundle: dict[str, Any]
) -> None:
    changes = ctm.parse_name_status(diff)
    ignored = {NEW[0], NEVER_RUNS[0]}
    unknown = sorted(_absent(bundle, UNIVERSE))
    block = win.block(
        changes,
        _score(diff, bundle),
        bundle,
        UNIVERSE,
        GUARD,
        added={},
        bucket=["**/*windows*"],
        ignored=ignored,
        at=NOW,
        score=lambda part: cts.score(
            part,
            bundle,
            UNIVERSE,
            GUARD,
            skippable={"G"},
            narrow_reach_max=20,
            grep=lambda modules: set(),
        ),
        unknown=unknown,
    )
    assert block["verdict"] == "select"
    assert set(unknown) - ignored <= set(block["run"]) and not ignored & set(block["run"])
    # The miss ledger reads the block as it is written.
    assert ledger.parse_windows(json.loads(json.dumps(block))) is not None
    # Its share weighs each file as the Linux share does.
    weigh = ctm.node_weights(bundle["tests"], bundle.get(ctm.UNRECORDED, ()))
    collected = (set(UNIVERSE) | set(block["run"])) - ignored
    expected = sum(map(weigh, block["run"])) / sum(map(weigh, collected))
    assert block["would_run_share"] == pytest.approx(expected)


@pytest.mark.parametrize("bundle", BUNDLES)
def test_the_verdict_counts_the_unknown_files_by_kind_and_how_many_it_held(
    bundle: dict[str, Any],
) -> None:
    verdict = _score("M\tsrc/kiro_crew/acp/mod.py\n", bundle)
    told = ctm.UNRECORDED in bundle
    # Without the table every unknown file reads as newer than the map.
    assert verdict["unknown"]["new"] == len(NEW) + (0 if told else len(NEVER_RUNS))
    assert verdict["unknown"]["never_runs"] == (len(NEVER_RUNS) if told else 0)
    # Two of the new files have a signal of their own (the folder, the package).
    signalled = {"test/acp/test_beside.py", "test/test_acp_named.py"}
    assert verdict["unknown"]["held"] == len(NEW) + len(NEVER_RUNS) - len(signalled)
    # With their tiers skippable too, those two are held as well.
    every_tier = _score("M\tsrc/kiro_crew/acp/mod.py\n", bundle, frozenset(cts.TIERS[1:]))
    assert every_tier["unknown"]["held"] == len(NEW) + len(NEVER_RUNS)
    # A verdict that falls back counts the files and holds none; an unscored one says nothing.
    full = _score("M\tsetup.cfg\n", bundle)
    assert full["verdict"] == "full"
    assert full["unknown"] == {**verdict["unknown"], "held": 0}
    assert cts.full_verdict("no bundle")["unknown"] == {}
    # The miss ledger reads a verdict that carries the counts.
    assert ledger.parse_verdict(json.dumps({"schema": 1, **verdict})) is not None
    assert ledger.parse_verdict(json.dumps({"schema": 1, **full})) is not None


def test_a_share_weighs_a_new_file_the_maps_mean_and_an_unrecorded_one_nothing() -> None:
    weigh = ctm.node_weights(KNOWN, NEVER_RUNS)
    assert [weigh(test) for test in KNOWN] == list(KNOWN.values())
    assert {weigh(test) for test in NEW} == {MEAN} and {weigh(t) for t in NEVER_RUNS} == {0.0}
    # With no map there is no mean: every file weighs 1, an unrecorded one included.
    assert {ctm.node_weights({}, NEVER_RUNS)(test) for test in UNIVERSE} == {1.0}
    told = next(b for b in BUNDLES if ctm.UNRECORDED in b and "cochange" in b)
    verdict = _score("M\tsrc/kiro_crew/a.py\n", told)
    assert verdict["would_skip"] == ["test/test_idle.py"]
    total = sum(KNOWN.values()) + len(NEW) * MEAN
    assert verdict["would_run_share"] == pytest.approx((total - KNOWN["test/test_idle.py"]) / total)
    # The same diff with every unknown file read as new: they weigh more, the share is higher.
    untold = {k: v for k, v in told.items() if k != ctm.UNRECORDED}
    assert _score("M\tsrc/kiro_crew/a.py\n", untold)["would_run_share"] > verdict["would_run_share"]


def test_the_scorer_refuses_an_unrecorded_table_of_another_shape() -> None:
    cts.check_tables({**BUNDLES[-1]})
    for bad in ({}, "x", [1], [None]):
        with pytest.raises(ValueError, match=ctm.UNRECORDED):
            cts.check_tables({**BUNDLES[-1], ctm.UNRECORDED: bad})


# ── The nightly bundle and its check, on a small repository ──


def _git(root: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    tree = [*KNOWN, *NEVER_RUNS, "test/helpers.py", "src/kiro_crew/a.py", "docs/test_notes.py"]
    for rel in tree:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    # No repository above `tmp_path` is found, wherever the host's tmp dir sits.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _bundle(repo: Path, monkeypatch: pytest.MonkeyPatch, tests: dict[str, int]) -> dict[str, Any]:
    monkeypatch.setattr(ctm, "guard_tier", lambda root: GUARD)
    test_map = {"schema": 1, "commit": "abc", "tests": tests, "files": {"src/kiro_crew/a.py": [0]}}
    return ctm.bundle(test_map, repo, timeout=FALLBACK_TIMEOUT_SECS)[0]


def test_the_bundle_lists_the_tracked_test_files_the_map_recorded_no_node_from(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _bundle(repo, monkeypatch, KNOWN)
    # Test files only, under the paths pytest collects, in code point order.
    assert doc[ctm.UNRECORDED] == sorted(NEVER_RUNS)
    # A map with an entry for each leaves the table out; it is never written empty.
    whole = _bundle(repo, monkeypatch, {**KNOWN, **dict.fromkeys(NEVER_RUNS, 1)})
    assert ctm.UNRECORDED not in whole
    # A table the map already carries is rebuilt, never carried over.
    stale = {"schema": 1, "tests": KNOWN, "files": {}, ctm.UNRECORDED: ["test/test_stale.py"]}
    assert ctm.bundle(stale, repo, timeout=FALLBACK_TIMEOUT_SECS)[0][ctm.UNRECORDED] == sorted(
        NEVER_RUNS
    )
    stale["tests"] = {**KNOWN, **dict.fromkeys(NEVER_RUNS, 1)}
    assert ctm.UNRECORDED not in ctm.bundle(stale, repo, timeout=FALLBACK_TIMEOUT_SECS)[0]


def test_check_prints_both_counts_and_passes_a_bundle_without_the_table(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = {**_bundle(repo, monkeypatch, KNOWN), "os_sensitive": {"x": "W1"}}
    doc.update(windows_targeting=["test/test_a.py"], cochange={"src/kiro_crew/a.py": ["x"]})
    path = tmp_path / "bundle.json"
    argv = ["--root", str(repo), "check", "--bundle", str(path)]

    def check(bundle: dict[str, Any]) -> tuple[int, str]:
        path.write_text(json.dumps(bundle), encoding="utf-8")
        code = ctm.main(argv)
        return code, capsys.readouterr().out

    code, printed = check(doc)
    total = len(KNOWN) + len(NEVER_RUNS)
    assert code == 0 and f"unknown test files: 2 of {total} tracked" in printed
    assert "0 new since the map, 2 the map's run recorded no node from" in printed
    # A test file merged after the map was built is new since the map.
    (repo / "test/test_merged_later.py").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "later")
    code, printed = check(doc)
    assert code == 0 and f"unknown test files: 3 of {total + 1} tracked" in printed
    assert "1 new since the map, 2 the map's run recorded no node from" in printed
    # A bundle built before the table existed is still whole: all three read as new.
    code, printed = check({k: v for k, v in doc.items() if k != ctm.UNRECORDED})
    assert code == 0 and "3 new since the map, 0 the map's run recorded no node from" in printed
    # Outside a tracked tree the count is not made, and the check's answer does not change.
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert ctm.main(["--root", str(tmp_path / "no-tree"), "check", "--bundle", str(path)]) == 0
    assert "unknown test files: not counted" in capsys.readouterr().out

"""Contract tests for an added test file in the SHADOW selection.

`ci_test_map.select` takes a test file a diff adds as it takes one the diff
edits: the file runs, and nothing falls back for it. Pinned: that holds under
every test path and beside any other row; every other added path keeps the
fall-back; a test file that is deleted, renamed or copied keeps it too; and the
verdict counts the added test files under `covered`, which the miss ledger keeps.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import ci_test_selection as cts
from scripts import ci_test_selection_ledger as ledger

ctm = cts.ctm
win = cts.windows
TESTS = {"test/test_a.py": 1, "test/test_gate.py": 1, "test/test_idle.py": 8}
MAP: dict[str, Any] = {"tests": TESTS, "files": {"src/kiro_crew/a.py": [0]}}
GUARD = ["test/test_gate.py"]
# One new test file under each path pytest collects.
NEW = [f"{root}new/test_added.py" for root in ctm.TESTPATHS]
NARROWED = ["", "M\tsrc/kiro_crew/a.py\n", "M\ttest/test_idle.py\n"]
FULL = ["M\tsetup.cfg\n", "M\ttest/conftest.py\n", "D\tsrc/kiro_crew/a.py\n"]


def _select(diff: str) -> Any:
    return ctm.select(ctm.parse_name_status(diff), MAP, GUARD)


@pytest.mark.parametrize("new", NEW)
@pytest.mark.parametrize("rest", NARROWED)
def test_an_added_test_file_runs_and_changes_nothing_else(rest: str, new: str) -> None:
    without = _select(rest)
    assert not without.full
    for diff in (rest + f"A\t{new}\n", f"A\t{new}\n" + rest):
        result = _select(diff)
        assert not result.full and result.reasons == []
        assert result.tests == sorted({*without.tests, new})
    # The same file edited is selected the same way.
    assert _select(rest + f"A\t{new}\n") == _select(rest + f"M\t{new}\n")


@pytest.mark.parametrize("rest", FULL)
def test_an_added_test_file_never_narrows_a_diff_that_falls_back(rest: str) -> None:
    assert _select(rest + f"A\t{NEW[0]}\n") == _select(rest)
    assert _select(rest).full


@pytest.mark.parametrize(
    "row",
    [
        # Added, and not a test file pytest collects.
        "A\tsrc/kiro_crew/new.py",
        "A\ttest/helpers_new.py",
        "A\ttest/conftest_new.py",
        "A\ttest/sub/conftest.py",
        "A\tscripts/test_added.py",
        "A\ttest/test_added.txt",
        # A test file whose status is not `added`.
        "D\ttest/test_idle.py",
        "R100\ttest/test_idle.py\ttest/test_moved.py",
        "C90\ttest/test_idle.py\ttest/test_copy.py",
        "T\ttest/test_idle.py",
    ],
)
def test_every_other_change_to_the_file_set_still_falls_back(row: str) -> None:
    for rest in NARROWED:
        result = _select(rest + row + "\n")
        assert result.full and result.tests == []
        assert any(reason.endswith(": file set changed") for reason in result.reasons)
    assert ctm.added_tests(ctm.parse_name_status(row)) == {}


BUNDLE: dict[str, Any] = {"schema": 1, "commit": "abc", **MAP, "cochange": {}}
# The head's test tree, which holds the files the diff adds.
UNIVERSE = sorted([*TESTS, *NEW])


def _score(diff: str) -> dict[str, Any]:
    return cts.score(
        ctm.parse_name_status(diff),
        BUNDLE,
        UNIVERSE,
        GUARD,
        skippable={"G"},
        narrow_reach_max=20,
        grep=lambda modules: set(),
    )


@pytest.mark.parametrize("count", [1, 2])
def test_the_verdict_counts_the_added_test_files_and_the_ledger_keeps_the_count(count: int) -> None:
    rest = "M\tsrc/kiro_crew/a.py\n"
    added = NEW[:count]
    before = _score(rest)
    after = _score(rest + "".join(f"A\t{path}\n" for path in added))
    assert after["verdict"] == "select" and before["covered"] == {}
    assert after["covered"] == {ctm.ADDED_TEST: count}
    # An added test file is tier A, is never skipped, and moves no other file's tier.
    assert set(after["tiers"]["A"]) == {*before["tiers"]["A"], *added}
    assert not set(added) & set(after["would_skip"])
    for tier in cts.TIERS[1:]:
        assert set(after["tiers"][tier]) == set(before["tiers"][tier]) - set(added)
    # With no row for it, the same file has no signal.
    assert set(added) <= set(before["would_skip"])
    parsed = ledger.parse_verdict(json.dumps({"schema": 1, **after}))
    assert parsed is not None and parsed["covered"] == {ctm.ADDED_TEST: count}
    key = {"run": 1, "attempt": 1, "source": "1:b", "fork": False, "pr": 7}
    key.update(head="a" * 40, date="2026-11-20T12:00:00Z")
    held = ledger.run_rows(key, parsed, {}, set())[0]
    assert held["covered"] == {ctm.ADDED_TEST: count} and ledger.valid_row(held)


def test_a_full_verdict_and_an_edited_test_file_count_no_added_test() -> None:
    assert _score(f"M\tsetup.cfg\nA\t{NEW[0]}\n")["covered"] == {}
    assert _score("M\ttest/test_idle.py\n")["covered"] == {}


def test_the_windows_block_runs_an_added_test_file() -> None:
    diff = f"M\tsrc/kiro_crew/a.py\nA\t{NEW[0]}\n"
    bundle = {
        **BUNDLE,
        "built_at": "2026-11-20T12:00:00+00:00",
        "os_sensitive": {"src/kiro_crew/other.py": "W1"},
        "windows_targeting": ["test/test_idle.py"],
    }
    block = win.block(
        ctm.parse_name_status(diff),
        _score(diff),
        bundle,
        UNIVERSE,
        GUARD,
        added={},
        bucket=["**/*windows*"],
        ignored=set(),
        at=ledger.datetime.fromisoformat("2026-11-20T12:00:00+00:00"),
        score=lambda part: _score("".join("\t".join([s, *ps]) + "\n" for s, ps in part)),
    )
    assert block["verdict"] == "select" and set(block["run"]) == {NEW[0], *GUARD}

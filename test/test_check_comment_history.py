"""The comment-history gate must be real, wired into CI, and ratchet-only.

``docs/system-specs/common/code-style.md`` forbids change history in comments and
docstrings. ``scripts/check_comment_history.py`` is what makes that rule
enforceable. These tests pin the halves that must stay true together: CI actually
runs the gate (a gate that exists only on disk is not a gate), the detector reads
comments and docstrings but NOT ordinary string literals, and the baseline can
only shrink -- no operation may add a path or raise a count.
"""

from __future__ import annotations

import importlib.util
import json
import tokenize
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_comment_history.py"
BASELINE = ROOT / "comment-history-baseline.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CODE_STYLE = ROOT / "docs" / "system-specs" / "common" / "code-style.md"

SPEC = importlib.util.spec_from_file_location("check_comment_history", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _lint_steps() -> list[dict]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        steps = job.get("steps") or []
        if any("isort --check-only" in str(step.get("run", "")) for step in steps):
            return steps
    raise AssertionError("ci.yml has no job running isort --check-only")


class TestCiWiring:
    def test_ci_actually_runs_the_gate(self) -> None:
        runs = [str(step.get("run", "")) for step in _lint_steps()]
        assert any(
            "scripts/check_comment_history.py" in run for run in runs
        ), "ci.yml's lint job no longer runs the comment-history gate"

    def test_ci_runs_the_self_test_first(self) -> None:
        # The self-test plants one probe per rule family, so a typo that
        # silently disables a rule fails in CI instead of shipping green.
        for run in (str(step.get("run", "")) for step in _lint_steps()):
            if "check_comment_history.py" not in run:
                continue
            assert "--test" in run, "the gate step must run the --test self-test"
            return
        raise AssertionError("gate step not found")

    def test_scope_resolver_coupling_is_alive(self) -> None:
        # The gate loads scripts/ratchet_scope.py, which OWNS both answers. A
        # rename there must fail HERE, not as an AttributeError inside a CI run.
        scope = gate._load_scope()
        assert callable(scope.changed_paths)
        assert callable(scope.added_lines)

    def test_code_style_doc_names_every_enforced_phrase(self) -> None:
        # The doc's DO-NOT list IS the rule set. A pattern the doc does not name
        # is a rule a contributor cannot discover, and the gate is then the
        # authority instead of the spec.
        text = CODE_STYLE.read_text(encoding="utf-8").lower()
        for phrase in (
            "previously",
            "used to",
            "we now",
            "no longer",
            "historically",
            "status: implemented",
            "hotfix",
            "follow-up to",
            "regression for",
            "round n",
            "gpt round",
            "review round",
            "commit sha",
            "incident date",
            "ticket id",
        ):
            assert phrase in text, f"code-style.md does not name {phrase!r}"

    def test_code_style_doc_names_the_gate_and_the_baseline(self) -> None:
        # The rule and its enforcement must be discoverable from one another:
        # a contributor who reads the rule needs the command that checks it.
        text = CODE_STYLE.read_text(encoding="utf-8")
        assert "scripts/check_comment_history.py" in text
        assert "comment-history-baseline.json" in text


class TestRuleFamilies:
    """One probe per rule family, through the real detector."""

    def _found(self, source: str) -> list[tuple[int, str]]:
        return gate.violations_in_source(source)

    def test_flags_a_violating_comment(self) -> None:
        assert self._found("x = 1  # widen the timeout (#4211)\n") == [(1, "(#4211)")]

    def test_flags_a_violating_docstring(self) -> None:
        source = "def f():\n" '    """Return the path. Previously it read the cache."""\n'
        assert self._found(source) == [(2, "Previously")]

    def test_flags_a_module_docstring(self) -> None:
        assert self._found('"""Parse the manifest. Hotfix for the launch."""\n') == [(1, "Hotfix")]

    def test_flags_a_class_docstring(self) -> None:
        source = "class C:\n" '    """Holds state. We now resolve symlinks."""\n' "    x = 1\n"
        assert self._found(source) == [(2, "We now")]

    def test_non_docstring_string_literal_is_not_scanned(self) -> None:
        # A user-facing message that happens to use one of these phrases is
        # BEHAVIOR, not narration. Scanning every string would make the gate
        # wrong in the one place the words are legitimate.
        assert self._found('MESSAGE = "this token is no longer valid"\n') == []

    def test_string_literal_after_a_docstring_is_not_scanned(self) -> None:
        # The first statement is the docstring; the assignment below it is not,
        # even though both are string literals at module level.
        source = '"""Module."""\n' 'HINT = "the flag was previously named --slow"\n'
        assert self._found(source) == []

    def test_docstring_position_is_what_makes_it_a_docstring(self) -> None:
        # Same literal, second statement: not a docstring, so not scanned.
        source = "x = 1\n" '"""Previously this parsed lazily."""\n'
        assert self._found(source) == []

    @pytest.mark.parametrize(
        "pragma",
        [
            "x: int = 1  # type: ignore[assignment]",
            "import os  # noqa: F401",
            "x = [1]  # fmt: off",
        ],
    )
    def test_pragma_comments_are_exempt(self, pragma: str) -> None:
        assert self._found(pragma + "\n") == []

    def test_pragma_no_cover_is_exempt(self) -> None:
        assert self._found("if False:  # pragma: no cover\n    pass\n") == []

    def test_a_marker_reports_the_line_it_sits_on_inside_a_docstring(self) -> None:
        # The added-line rule compares against the lines a diff touched. A hit
        # reported at the docstring's FIRST line is invisible to it, so swapping
        # a fresh marker into an old multi-line docstring would pass.
        source = '"""Head.\n\nTail: previously it blocked.\n"""\n'
        assert self._found(source) == [(3, "previously")]

    def test_present_tense_purpose_is_not_narration(self) -> None:
        # Naming the incident a change answers is narration; naming what a test
        # pins is purpose. code-style.md forbids the first, not the second.
        assert self._found("x = 1  # regression test pins this shape\n") == []
        assert self._found("x = 1  # regression for the truncated parse\n")

    @pytest.mark.parametrize("broken", ["def broken(:\n", "x = (\n"])
    def test_unparseable_source_raises_instead_of_reading_clean(self, broken: str) -> None:
        # A parse failure reading as "zero violations" would invite a baseline
        # prune that deletes the file's real entry. Which of the two exceptions
        # comes out depends on whether tokenize or ast gives up first, so the
        # scanner catches both and this pins both.
        with pytest.raises((SyntaxError, tokenize.TokenError)):
            self._found(broken)

    def test_self_test_passes(self) -> None:
        assert gate._self_test() == 0

    def test_prefilter_cannot_fall_behind_the_pattern_tuple(self) -> None:
        # The pre-filter skips tokenize/ast for a file it cannot match, so a
        # pattern missing from it would be silently unenforced.
        for pattern in gate.PATTERNS:
            assert pattern.pattern in gate._ANY_MARKER.pattern


class TestScopeExclusions:
    def test_vendor_directory_is_excluded(self) -> None:
        # Vendored third-party code is not ours to rewrite, and code-style.md
        # exempts it.
        assert gate._excluded("src/kiro_crew/_vendor/anyio/_core.py")

    def test_first_party_paths_are_not_excluded(self) -> None:
        assert not gate._excluded("src/kiro_crew/agent.py")
        assert not gate._excluded("test/test_agent.py")

    def test_a_path_merely_containing_vendor_is_not_excluded(self) -> None:
        # The exclusion is a directory prefix, not a substring: a first-party
        # test ABOUT vendored code must still be judged.
        assert not gate._excluded("test/test_vendored_llama_payload.py")

    def test_targets_are_the_documented_two_trees(self) -> None:
        assert gate.DEFAULT_TARGETS == ("src/kiro_crew", "test")


class TestVerdicts:
    """The ratchet's verdict logic, on synthetic inputs."""

    def test_unbaselined_file_in_scope_is_a_new_offender(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(10, "previously")]}, {}, {"src/x.py"}, None
        )
        assert new == ["src/x.py"]
        assert not grown and not on_added and not shrunk

    def test_out_of_scope_files_are_not_judged(self) -> None:
        # CI evaluates a merge ref: someone else's file must not colour this PR.
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(10, "previously")], "src/y.py": [(5, "hotfix"), (6, "we now")]},
            {"src/y.py": 1},
            {"src/other.py"},
            None,
        )
        assert not new and not grown and not on_added and not shrunk

    def test_baseline_exceeded_fails(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(1, "a"), (2, "b"), (3, "c")]}, {"src/x.py": 2}, {"src/x.py"}, None
        )
        assert grown == ["src/x.py"]

    def test_level_count_within_the_baseline_passes(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(1, "a"), (2, "b")]}, {"src/x.py": 2}, {"src/x.py"}, None
        )
        assert not new and not grown and not on_added and not shrunk

    def test_swapping_one_marker_for_another_is_caught_by_added_lines(self) -> None:
        # Delete one old marker, write one new one: the count is level, but the
        # new one sits on an added line and must still fail.
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(10, "previously"), (30, "we now")]},
            {"src/x.py": 2},
            {"src/x.py"},
            {"src/x.py": {30}},
        )
        assert on_added == {"src/x.py": [(30, "we now")]}
        assert not new and not grown

    def test_stale_baseline_demands_a_lower(self) -> None:
        # The count dropped and the entry was not lowered in the same change.
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(10, "previously")]}, {"src/x.py": 3}, {"src/x.py"}, None
        )
        assert shrunk == ["src/x.py"]

    def test_a_file_cleaned_to_zero_must_be_removed(self) -> None:
        new, grown, on_added, shrunk = gate._verdicts({}, {"src/x.py": 3}, {"src/x.py"}, None)
        assert shrunk == ["src/x.py"]

    def test_undeterminable_scope_judges_the_whole_tree(self) -> None:
        # A scoping mechanism that fails open would disable the gate exactly
        # when its inputs are unusual.
        new, grown, on_added, shrunk = gate._verdicts(
            {"src/x.py": [(10, "previously")]}, {}, None, None
        )
        assert new == ["src/x.py"]


class TestBaselineIsShrinkOnly:
    def test_refresh_lowers_counts(self) -> None:
        assert gate._shrunken_baseline({"src/x.py": 5}, {"src/x.py": 2}) == {"src/x.py": 2}

    def test_refresh_never_raises_a_count(self) -> None:
        # The one rule that keeps the gate from being a formality: a grown file
        # is a red gate to fix, not an entry to raise.
        assert gate._shrunken_baseline({"src/x.py": 2}, {"src/x.py": 9}) == {"src/x.py": 2}

    def test_refresh_drops_a_clean_file(self) -> None:
        assert gate._shrunken_baseline({"src/x.py": 2}, {}) == {}

    def test_refresh_never_adds_a_path(self) -> None:
        assert gate._shrunken_baseline({}, {"src/new.py": 4}) == {}

    def test_write_baseline_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        entries = {"src/b.py": 2, "src/a.py": 7}
        gate._write_baseline(path, entries)
        assert gate._read_baseline(path) == entries

    def test_written_baseline_is_sorted_and_carries_a_total(self, tmp_path: Path) -> None:
        # Sorted so an entry lowered by one PR is a one-line diff, not a
        # reordering that hides the change.
        path = tmp_path / "baseline.json"
        gate._write_baseline(path, {"src/b.py": 2, "src/a.py": 7})
        document = json.loads(path.read_text(encoding="utf-8"))
        assert list(document["files"]) == ["src/a.py", "src/b.py"]
        assert document["_total"] == 9

    def test_write_baseline_refuses_when_the_baseline_is_absent(self, tmp_path: Path) -> None:
        # Otherwise `rm baseline && --write-baseline` records the whole tree as
        # pre-existing, which amnesties every marker in one command.
        with pytest.raises(SystemExit):
            gate.run_gate(tmp_path / "absent.json", write=True)

    def test_missing_baseline_refuses_to_regenerate_itself(self, tmp_path: Path) -> None:
        # Regenerating on absence would silently absorb every offender added
        # since the file was recorded.
        with pytest.raises(SystemExit):
            gate._read_baseline(tmp_path / "absent.json")

    def test_malformed_count_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"files": {"src/x.py": "many"}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            gate._read_baseline(path)

    def test_zero_count_is_an_error(self, tmp_path: Path) -> None:
        # A zero entry is an exemption dressed as a count: the file is clean, so
        # the entry must be gone.
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"files": {"src/x.py": 0}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            gate._read_baseline(path)


class TestCommittedBaseline:
    def test_committed_baseline_parses(self) -> None:
        entries = gate._read_baseline(BASELINE)
        assert entries, "the committed baseline records no files"

    def test_committed_baseline_lists_no_vendor_or_excluded_path(self) -> None:
        for rel in gate._read_baseline(BASELINE):
            assert not gate._excluded(rel), f"{rel} is excluded but recorded"

    def test_committed_baseline_only_lists_scanned_trees(self) -> None:
        for rel in gate._read_baseline(BASELINE):
            assert rel.startswith(gate.DEFAULT_TARGETS), f"{rel} is outside the scanned trees"

"""Unit tests for .github/scripts/issue_gate_refs.py.

The Issue Gate workflow decides "which issues does this PR declare" through
this adapter, and the adapter must give the SAME answer as the prepare-pr
grammar it wraps (`pr_status.py`'s explicit-trailer grammar). These tests pin
that equivalence on the body shapes that have already fooled a hand-rolled
grep: the PR template's HTML-comment hint, an unclosed fence, inline code,
non-closing keywords, and references to another repository -- and pin the
declaration grammar's own shape (a declaration starts a line, the rest of the
line is free; `Refs` / `Part of` declare without closing; only github.com URLs).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script, no_bytecode

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "issue_gate_refs.py"
REPO = "kirodotdev/KiroCrew"


@pytest.fixture(scope="module")
def module():
    # Loaded without bytecode: a plain import drops `__pycache__` beside the
    # checked-in script, a working-copy mutation the no-side-effects rule forbids.
    return load_skill_script("issue_gate_refs", SCRIPT)


@pytest.fixture(scope="module")
def grammar(module):
    with no_bytecode():
        return module.load_grammar()


def _declared(module, grammar, body: str) -> list[str]:
    numbers, well_formed = module.declared_numbers(body, REPO, grammar)
    assert well_formed
    return numbers


class TestWhatCounts:
    def test_whole_line_closing_trailer_counts(self, module, grammar):
        assert _declared(module, grammar, "Summary.\n\nCloses #100\n") == ["100"]

    @pytest.mark.parametrize("verb", ["Fixes", "fixed", "Resolve", "CLOSED", "resolves"])
    def test_every_github_closing_verb_counts(self, module, grammar, verb):
        assert _declared(module, grammar, f"{verb} #7") == ["7"]

    def test_qualified_and_url_targets_for_this_repo_count(self, module, grammar):
        body = (
            "Fixes kirodotdev/KiroCrew#100\n"
            "Resolves https://github.com/kirodotdev/KiroCrew/issues/200\n"
        )
        assert _declared(module, grammar, body) == ["100", "200"]

    @pytest.mark.parametrize("line", ["Refs #5", "Ref #5", "Part of #5", "- part of: #5"])
    def test_non_closing_declarations_count_without_closing(self, module, grammar, line):
        assert _declared(module, grammar, line) == ["5"]

    def test_bulleted_line_with_several_references_counts_each(self, module, grammar):
        assert _declared(module, grammar, "- Fixes #1, closes #2 and resolves #3.") == [
            "1",
            "2",
            "3",
        ]

    def test_numbers_are_sorted_numerically_and_deduplicated(self, module, grammar):
        assert _declared(module, grammar, "Closes #30\nFixes #4\nCloses #30") == ["4", "30"]


class TestWhatDoesNotCount:
    def test_pr_template_html_comment_hint_is_not_a_declaration(self, module, grammar):
        body = "## Related Issues\n\n<!-- Link to relevant issues, e.g. Fixes #123 -->\n"
        assert _declared(module, grammar, body) == []

    def test_unclosed_fence_is_masked_through_end_of_body(self, module, grammar):
        assert _declared(module, grammar, "```\nCloses #999\n") == []

    def test_fence_closed_by_a_longer_run_still_closes(self, module, grammar):
        body = "```\nCloses #999\n`````\nCloses #100\n"
        assert _declared(module, grammar, body) == ["100"]

    def test_inline_code_is_not_a_declaration(self, module, grammar):
        assert _declared(module, grammar, "The harness checks `Closes #999` too.") == []

    @pytest.mark.parametrize("line", ["Related to #5", "see #5", "Addresses #5"])
    def test_unlisted_keywords_do_not_count(self, module, grammar, line):
        assert _declared(module, grammar, line) == []

    @pytest.mark.parametrize("line", ["Discloses #5", "Unfixed #5", "prefixes #5"])
    def test_keyword_must_start_the_trailer(self, module, grammar, line):
        assert _declared(module, grammar, line) == []

    def test_other_repository_is_not_this_repository(self, module, grammar):
        body = "Closes other/repo#5\nFixes https://github.com/other/repo/issues/6\n"
        assert _declared(module, grammar, body) == []

    def test_more_than_the_ceiling_is_reported_by_the_cli_not_read(self, module, grammar):
        body = "\n".join(f"Closes #{n}" for n in range(1, module.MAX_DECLARED + 2))
        numbers, well_formed = module.declared_numbers(body, REPO, grammar)
        assert well_formed and len(numbers) == module.MAX_DECLARED + 1

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/kirodotdev/KiroCrew/issues/100",
            "https://github.com.evil.example/kirodotdev/KiroCrew/issues/100",
            "http://gitlab.com/kirodotdev/KiroCrew/issues/100",
        ],
    )
    def test_url_on_another_host_is_not_a_github_declaration(self, module, grammar, url):
        assert _declared(module, grammar, f"Fixes {url}") == []

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/kirodotdev/KiroCrew/issues/100",
            "http://github.com/kirodotdev/KiroCrew/issues/100",
            "https://www.github.com/kirodotdev/KiroCrew/issues/100",
        ],
    )
    def test_github_host_url_still_counts(self, module, grammar, url):
        assert _declared(module, grammar, f"Fixes {url}") == ["100"]


class TestLineStartReading:
    """pr_status.py's NOTICE path wants the trailer to be the WHOLE line; the
    gate wants it to START the line and leaves the rest free. Quotation and
    code shapes are refused by both."""

    def test_trailer_with_a_parenthetical_tail_counts(self, module, grammar):
        assert _declared(module, grammar, "Fixes #123 (the Windows half)") == ["123"]

    def test_every_reference_on_a_declaring_line_counts(self, module, grammar):
        body = "- Fixes #1, closes #2 (note) and later closes #7 too"
        assert _declared(module, grammar, body) == ["1", "2", "7"]

    def test_tail_of_a_declaring_line_still_needs_a_word_start(self, module, grammar):
        body = "Fixes #1; the crash is unresolved: #2 and prefixes #3"
        assert _declared(module, grammar, body) == ["1"]

    def test_blockquoted_reference_is_not_a_declaration(self, module, grammar):
        assert _declared(module, grammar, "> Closes #100\n") == []

    def test_four_column_nested_list_item_is_not_credited(self, module, grammar):
        body = "- Scope:\n    - Closes #5\n"
        assert _declared(module, grammar, body) == []

    def test_reference_buried_mid_sentence_is_not_a_declaration(self, module, grammar):
        assert _declared(module, grammar, "This PR Fixes #123 partially.") == []

    def test_sentence_that_opens_with_the_keyword_counts(self, module, grammar):
        body = "Fixed #123 in an earlier release; this PR only adds tests."
        assert _declared(module, grammar, body) == ["123"]


class TestMalformed:
    def test_impossible_number_is_reported_not_dropped(self, module, grammar):
        numbers, well_formed = module.declared_numbers("Closes #99999999999", REPO, grammar)
        assert numbers == []
        assert well_formed is False


class TestLabelContract:
    """The verdict labels are written by the maintainer-operated triage pipeline
    outside this repository; this is the one place in-repo that pins the names
    the gate reads, so a rename on either side shows up as a red test rather
    than as every PR going red on "no triage verdict label"."""

    def test_verdict_and_pending_labels_are_the_documented_set(self):
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "issue-gate.yml").read_text(encoding="utf-8")
        )
        (job,) = workflow["jobs"].values()
        env = next(step for step in job["steps"] if "run" in step)["env"]
        assert set(env["TRIAGE_VERDICT_LABELS"].split()) == {
            "auto-fixable",
            "needs-investigation",
            "needs-human",
        }
        assert env["PENDING_LABEL"] == "needs-triage"


class TestCommandLine:
    def _run(self, body: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_prints_one_number_per_line_and_exits_zero(self):
        proc = self._run("Closes #100\nFixes #7\n", REPO)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["7", "100"]

    def test_no_declaration_is_an_empty_stdout_and_exit_zero(self):
        proc = self._run("Nothing to see here.\n", REPO)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_malformed_trailer_exits_three(self):
        proc = self._run("Closes #99999999999\n", REPO)
        assert proc.returncode == 3
        assert "could never have issued" in proc.stderr

    def test_above_the_ceiling_exits_four_and_prints_nothing(self):
        body = "\n".join(f"Closes #{n}" for n in range(1, 22))
        proc = self._run(body, REPO)
        assert proc.returncode == 4
        assert proc.stdout == ""
        assert "above the ceiling" in proc.stderr

    def test_missing_repository_argument_exits_two(self):
        proc = self._run("Closes #1\n")
        assert proc.returncode == 2
        assert "usage" in proc.stderr

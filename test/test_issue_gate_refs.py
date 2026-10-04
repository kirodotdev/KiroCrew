"""Unit tests for .github/scripts/issue_gate_refs.py.

The Issue Gate workflow decides "which issues does this PR declare" through
this adapter, and the adapter must give the SAME answer as the kirocrew-prepare-pr
grammar it wraps (`pr_status.py`'s explicit-trailer grammar). These tests pin
that equivalence on the body shapes that have already fooled a hand-rolled
grep: the PR template's HTML-comment hint, an unclosed fence, inline code,
non-closing keywords, and references to another repository -- and pin the
declaration grammar's own shape (a declaration starts a line, the rest of the
line is free; `Refs` / `Part of` declare without closing; only github.com URLs).
"""

from __future__ import annotations

import json
import os
import shutil
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


def _gate_step() -> dict:
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "issue-gate.yml").read_text(encoding="utf-8")
    )
    (job,) = workflow["jobs"].values()
    return next(step for step in job["steps"] if "run" in step)


class TestLabelContract:
    """The tier and triage labels are written by the maintainer-operated Captain
    outside this repository; this is the one place in-repo that pins the names
    the gate reads, so a rename on either side shows up as a red test rather
    than as every PR going red on "no tier label"."""

    def test_tier_and_triage_labels_are_the_documented_set(self):
        env = _gate_step()["env"]
        assert env["TIER_LABELS"].split() == ["tier:T1", "tier:T2", "tier:T3", "tier:T4"]
        assert env["PENDING_LABEL"] == "pending-triage"
        assert env["TRIAGED_LABEL"] == "triaged"
        # The retired verdict labels decide dispatch, not merge; the gate must
        # not read them again.
        assert "TRIAGE_VERDICT_LABELS" not in env
        assert "needs-triage" not in env.values()

    def test_pass_and_review_tiers_partition_the_tiers(self):
        env = _gate_step()["env"]
        passing = env["TIER_PASS_LABELS"].split()
        review = env["TIER_REVIEW_LABELS"].split()
        assert passing == ["tier:T1", "tier:T2"]
        assert review == ["tier:T3", "tier:T4"]
        assert sorted(passing + review) == sorted(env["TIER_LABELS"].split())

    def test_the_pause_switch_is_an_explicit_boolean(self):
        # Flipping the gate on or off is a one-line change; it must stay a
        # literal "true" / "false" the step's `case` understands.
        assert _gate_step()["env"]["GATE_ENFORCED"] in {"true", "false"}


_NEEDS_SHELL = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or os.name == "nt",
    reason="the gate step runs under bash with jq, as on the ubuntu runner",
)


@_NEEDS_SHELL
class TestGateStepDecisions:
    """Run the workflow's own step script against a stubbed `gh`, so the rule is
    proven on the shipped shell and not on a re-statement of it."""

    def _run(
        self,
        tmp_path: Path,
        *,
        labels: list[str] | None,
        body: str = "Closes #7\n",
        enforced: str = "true",
        waived: str = "false",
        state: str = "open",
        reason: str = "",
    ) -> tuple[int, str, str]:
        step = _gate_step()
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "body.txt").write_text(body, encoding="utf-8")
        if labels is not None:
            (stub / "issue-7.json").write_text(
                json.dumps(
                    {"is_pr": False, "state": state, "reason": reason, "labels": "\n".join(labels)}
                ),
                encoding="utf-8",
            )
        gh = stub / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'case "$2" in\n'
            '  */pulls/*) cat "$STUB_DIR/body.txt" ;;\n'
            '  */issues/*) f="$STUB_DIR/issue-${2##*/}.json"\n'
            '    if [ -f "$f" ]; then cat "$f"; else echo "Not Found (HTTP 404)" >&2; exit 1; fi ;;\n'
            '  *) echo "unexpected $2" >&2; exit 2 ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        py = stub / "python3"
        py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        gh.chmod(0o755)
        py.chmod(0o755)
        work = tmp_path / "work"
        work.mkdir()
        (work / "base").symlink_to(ROOT, target_is_directory=True)
        summary = tmp_path / "summary.md"
        env = {
            **os.environ,
            **{k: str(v) for k, v in step["env"].items()},
            "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(stub),
            "GH_TOKEN": "unused",
            "REPO": REPO,
            "PR": "1",
            "AUTHOR": "someone",
            "WAIVED": waived,
            "GATE_ENFORCED": enforced,
            "GITHUB_STEP_SUMMARY": str(summary),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        script = tmp_path / "step.sh"
        script.write_bytes(step["run"].encode("utf-8"))
        proc = subprocess.run(
            ["bash", "-e", str(script)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        text = summary.read_text(encoding="utf-8") if summary.exists() else ""
        return proc.returncode, proc.stdout + proc.stderr, text

    @pytest.mark.parametrize(
        "labels",
        [
            ["tier:T1"],
            ["tier:T2"],
            ["tier:T2", "pending-triage"],
            ["tier:T1", "needs-triage"],
            ["tier:T3", "triaged"],
            ["tier:T4", "triaged", "bug"],
        ],
    )
    def test_cleared_issues_are_green(self, tmp_path, labels):
        rc, out, _ = self._run(tmp_path, labels=labels)
        assert rc == 0, out

    @pytest.mark.parametrize(
        ("labels", "needle"),
        [
            ([], "no tier label"),
            (["auto-fixable", "needs-human"], "no tier label"),
            (["tier:T1", "tier:T3"], "more than one tier label"),
            (["tier:T3", "pending-triage"], "waiting for a person"),
            (["tier:T4"], "waiting for a person"),
            (["tier:T3", "pending-triage", "triaged"], "both"),
        ],
    )
    def test_uncleared_issues_are_red_and_say_why(self, tmp_path, labels, needle):
        rc, out, summary = self._run(tmp_path, labels=labels)
        assert rc == 1, out
        assert needle in summary

    def test_closed_as_not_planned_is_red_even_for_a_small_tier(self, tmp_path):
        rc, out, summary = self._run(
            tmp_path, labels=["tier:T1"], state="closed", reason="not_planned"
        )
        assert rc == 1, out
        assert "closed as not planned" in summary

    def test_closed_as_completed_is_not_refused_on_that_alone(self, tmp_path):
        rc, out, _ = self._run(tmp_path, labels=["tier:T1"], state="closed", reason="completed")
        assert rc == 0, out

    def test_a_body_with_no_issue_is_red(self, tmp_path):
        rc, out, summary = self._run(tmp_path, labels=["tier:T1"], body="No link here.\n")
        assert rc == 1, out
        assert "Issue reference required" in summary

    def test_a_missing_issue_is_red(self, tmp_path):
        rc, out, summary = self._run(tmp_path, labels=None)
        assert rc == 1, out
        assert "does not exist" in summary

    def test_the_waiver_label_lets_an_unsized_issue_through(self, tmp_path):
        rc, out, _ = self._run(tmp_path, labels=[], waived="true")
        assert rc == 0, out

    @pytest.mark.parametrize(
        "labels",
        [["tier:T4", "pending-triage"], ["tier:T3"], ["tier:T1", "tier:T2"]],
    )
    def test_the_waiver_label_overrides_a_red_rule_and_warns(self, tmp_path, labels):
        rc, out, _ = self._run(tmp_path, labels=labels, waived="true")
        assert rc == 0, out
        assert "::warning::" in out and "overridden" in out

    def test_the_waiver_label_lets_a_pr_with_no_issue_through(self, tmp_path):
        rc, out, _ = self._run(tmp_path, labels=None, body="No link.\n", waived="true")
        assert rc == 0, out

    def test_paused_passes_without_reading_anything(self, tmp_path):
        # No issue stub exists and the body has no reference: a paused gate must
        # not look at either.
        rc, out, summary = self._run(tmp_path, labels=None, body="", enforced="false")
        assert rc == 0, out
        assert "paused" in summary

    def test_an_unknown_switch_value_fails_closed(self, tmp_path):
        rc, out, _ = self._run(tmp_path, labels=["tier:T1"], enforced="maybe")
        assert rc == 1, out
        assert "GATE_ENFORCED" in out


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

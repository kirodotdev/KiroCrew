"""The GPT lane's push-budget section: shape, placement, and the real count.

After a PR has had three blocked pushes, ``codex-review.yml`` appends a short
section from ``.github/review-prompts/gpt-push-budget.md`` that asks the
reviewer to list sibling sites. A first-time review must stay exactly as long as
it was, so the section is conditional, staged outside the shared prompt loop, and
never part of ``gpt-review-core.md``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    REVIEW_PROMPTS,
    _bash,
    _child_env,
    _flat,
    _proc_log,
    _prompt,
    _script_file,
    _step_env,
    _step_script,
    _stub_path,
    _workflow,
)
from workflow_token_scopes import WORKFLOWS, load_workflow, missing_scopes

PROMPT_NAME = "gpt-push-budget.md"
STEP = "Write review prompt"
HEAD = "a" * 40
LANES = (
    "codex-review",
    "claude-review",
    "design-review",
    "ux-review",
    "first-principles-review",
    "security-scope-review",
)


def _budget_block() -> str:
    script = _step_script(_workflow("codex-review.yml"), STEP)
    return script[script.index("PUSH_BUDGET_MIN=") :]


class TestThePromptText:
    def test_it_carries_the_designed_instruction(self) -> None:
        text = _flat(_prompt(PROMPT_NAME))

        assert "This PR has had %BLOCKED_PUSHES% blocked pushes." in text
        assert (
            "For each BLOCKING item add one `Sites:` line naming every other spelling, "
            "path or reader of the same root cause you can ground in THIS head" in text
        )
        assert (
            "If a recorded ruling already fixed this root cause twice, add one "
            "`Remedy: narrow or remove the mechanism` line to that item." in text
        )

    def test_it_never_reassigns_an_anchor_verdict_or_marker(self) -> None:
        """The security fence reads the item's `Anchor:` line, and the gate reads
        the verdict markers, so a section that hands the reviewer a new value for
        either can un-fence or flip a finding. It may only add its own two lines."""
        text = _flat(_prompt(PROMPT_NAME))

        assert "Keep each item's own `Anchor:` line exactly as the rules above give it" in text
        assert "never changes an Anchor, a verdict or a marker" in text
        # No Anchor value is spelled out, so none can fall outside the contract.
        assert not re.search(r"Anchor\s+is\b|Anchor:\s*`?\w", text), text
        assert "`design`" not in text
        assert "[GPT-REVIEWED]" not in text and "[BLOCK-MERGE]" not in text
        # The fields it names: its own two, plus the Anchor it keeps and the Fix
        # line it places them after.
        fields = set(re.findall(r"`([A-Za-z]+):", text))
        assert fields == {"Sites", "Remedy", "Anchor", "Fix"}, fields

    def test_it_stays_short(self) -> None:
        assert len(_prompt(PROMPT_NAME).encode("utf-8")) < 1300

    def test_the_first_time_prompt_files_do_not_mention_it(self) -> None:
        for name in ("gpt-review-core", "gpt-round-convergence", "gpt-output-contract"):
            body = _prompt(f"{name}.md")
            assert "PUSH BUDGET" not in body, name
            assert "%BLOCKED_PUSHES%" not in body, name
            assert "Sites:" not in body, name


class TestThePlacementInTheWorkflow:
    def test_it_is_staged_from_the_base_outside_the_shared_loop(self) -> None:
        workflow = _workflow("codex-review.yml")
        script = _step_script(workflow, STEP)
        loop = next(line for line in script.splitlines() if line.startswith("for p in gpt-"))

        # Not in the loop: the loop stages the unconditional prompt, and its
        # `cp` bootstrap would let a PR supply its own copy of this section.
        assert "gpt-push-budget" not in loop
        block = _budget_block()
        assert 'git show "$BASE_SHA:.github/review-prompts/gpt-push-budget.md"' in block
        assert "cp " not in block

    def test_it_is_appended_after_the_convergence_rules_and_the_ledger(self) -> None:
        script = _step_script(_workflow("codex-review.yml"), STEP)

        convergence = script.index("cat .review-prompts-gpt/gpt-round-convergence.md")
        ledger_end = script.index('echo "ADJUDICATION_END::${nonce}"')
        budget = script.index("PUSH_BUDGET_MIN=")
        assert convergence < ledger_end < budget

    def test_it_is_a_conditional_sed_never_an_unconditional_cat_splice(self) -> None:
        block = _budget_block()

        assert not re.search(r"^\s*cat\s+\S*push-budget", block, flags=re.M)
        assert "PUSH_BUDGET_MIN=3" in block
        assert '-ge "$PUSH_BUDGET_MIN"' in block
        assert "sed " in block and ">> /tmp/codex-prompt.md" in block

    def test_every_call_is_a_read(self) -> None:
        block = _budget_block()

        calls = re.findall(r"gh api[^\n]*", block)
        assert calls, "the count makes no gh api call"
        assert not re.search(r"--method\s+(POST|PUT|PATCH|DELETE)", block)
        assert "gh pr " not in block and "gh issue " not in block
        assert "--input" not in block
        # The lane list the count reads: all review lanes, never a free choice.
        for lane in LANES:
            assert lane in block, lane

    def test_the_run_block_stays_free_of_template_expressions(self) -> None:
        # A run block with an expression is capped at 21,000 characters.
        assert "${{" not in _step_script(_workflow("codex-review.yml"), STEP)

    def test_the_token_can_read_what_the_count_reads(self) -> None:
        workflow = load_workflow(WORKFLOWS / "codex-review.yml")

        assert workflow["permissions"]["actions"] == "read"
        assert not missing_scopes("codex-review.yml", workflow)

    def test_no_write_scope_was_added(self) -> None:
        permissions = load_workflow(WORKFLOWS / "codex-review.yml")["permissions"]

        assert permissions == {
            "contents": "read",
            "pull-requests": "write",
            "id-token": "write",
            "actions": "read",
        }


# The stubs are shell FUNCTIONS prepended to the extracted block, not files on
# PATH: a function wins over any executable of the same name on every platform,
# whereas a PATH stub can lose to the real ``git`` (Windows Git Bash keeps its
# own directories on PATH), which silently turns the block into a no-op.
_STUBS = r"""
gh() {
  local path="" jqx="" fixture
  echo "$@" >> "$CALLS"
  while [ $# -gt 0 ]; do
    case "$1" in
      api|GET|--paginate) shift ;;
      --method) shift 2 ;;
      -f) echo "$2" >> "$CALLS"; shift 2 ;;
      --jq) jqx="$2"; shift 2 ;;
      -*) shift ;;
      *) path="$1"; shift ;;
    esac
  done
  fixture="$FIXTURES/$(printf '%s' "$path" | tr '/' '_')"
  if [ ! -f "$fixture" ]; then
    echo "gh: HTTP 403: Resource not accessible by integration" >&2
    return 1
  fi
  if [ -n "$jqx" ]; then
    jq -r "$jqx" "$fixture"
  else
    cat "$fixture"
  fi
}
git() {
  if [ -n "${BUDGET_TEXT:-}" ] && [ -f "$BUDGET_TEXT" ]; then
    cat "$BUDGET_TEXT"
    return 0
  fi
  echo "fatal: path does not exist in the base commit" >&2
  return 128
}
sleep() { return 0; }
"""


def _sha(ch: str) -> str:
    return ch * 40


class TestTheCountRunsAgainstStubbedReads:
    """Run the ACTUAL block with ``gh``, ``git`` and ``sleep`` stubbed as functions."""

    def _fixtures(
        self,
        tmp_path: Path,
        *,
        reviews: list | None = None,
        failures: dict[str, list] | None = None,
        branch: str = "feature/x",
    ) -> Path:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()

        def put(path: str, payload: object) -> None:
            name = path.replace("/", "_")
            (fixtures / name).write_text(json.dumps(payload), encoding="utf-8")

        put("repos/example/repo/pulls/7", {"head": {"ref": branch}})
        put("repos/example/repo/pulls/7/reviews", reviews or [])
        failures = failures or {}
        for lane in LANES:
            runs = [
                {
                    "head_sha": sha,
                    "conclusion": conclusion,
                    "pull_requests": [{"number": number}],
                }
                for sha, conclusion, number in failures.get(lane, [])
            ]
            put(
                f"repos/example/repo/actions/workflows/{lane}.yml/runs",
                {"workflow_runs": runs},
            )
        return fixtures

    def _run(
        self,
        tmp_path: Path,
        fixtures: Path,
        *,
        base_has_text: bool = True,
        head: str = HEAD,
        pr: str = "7",
    ):
        bash = _bash()
        if bash is None:
            pytest.skip("the block is Bash; skip where Bash is absent")
        prompt = tmp_path / "codex-prompt.md"
        prompt.write_text("EXISTING PROMPT\n", encoding="utf-8", newline="\n")
        calls = tmp_path / "calls.txt"
        calls.write_text("", encoding="utf-8")
        script = _STUBS + _budget_block().replace("/tmp/codex-prompt.md", '"$PROMPT_OUT"')
        (tmp_path / ".review-prompts-gpt").mkdir()
        env = {
            "PATH": _stub_path(tmp_path),
            "GH_CONFIG_DIR": str(tmp_path),
            "LC_ALL": "C",
            "REPO": "example/repo",
            "PR": pr,
            "HEAD": head,
            "BASE_SHA": "b" * 40,
            "CALLS": str(calls),
            "PROMPT_OUT": str(prompt),
            "FIXTURES": str(fixtures),
            "TMPDIR": str(tmp_path),
        }
        if base_has_text:
            env["BUDGET_TEXT"] = str(REVIEW_PROMPTS / PROMPT_NAME)
        result = subprocess.run(
            [bash, "-e", _script_file(tmp_path, script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(env),
            cwd=tmp_path,
        )
        return result, prompt.read_text(encoding="utf-8"), calls.read_text(encoding="utf-8")

    def test_three_blocked_pushes_append_the_section_with_the_count(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            reviews=[
                {"state": "CHANGES_REQUESTED", "commit_id": _sha("1")},
                {"state": "APPROVED", "commit_id": _sha("9")},
            ],
            failures={
                "codex-review": [(_sha("2"), "failure", 7), (_sha("3"), "success", 7)],
                "ux-review": [(_sha("4"), "failure", 7)],
            },
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt.startswith("EXISTING PROMPT\n")
        assert "This PR has had 3 blocked pushes." in _flat(prompt)
        assert "%BLOCKED_PUSHES%" not in prompt
        assert "`Sites:` line" in prompt

    def test_two_blocked_pushes_leave_the_prompt_byte_identical(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            reviews=[{"state": "CHANGES_REQUESTED", "commit_id": _sha("1")}],
            failures={"codex-review": [(_sha("2"), "failure", 7)]},
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"

    def test_a_head_counts_once_however_many_lanes_or_reviews_blocked_it(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            reviews=[
                {"state": "CHANGES_REQUESTED", "commit_id": _sha("1")},
                {"state": "CHANGES_REQUESTED", "commit_id": _sha("1")},
            ],
            failures={
                "codex-review": [(_sha("1"), "failure", 7), (_sha("2"), "failure", 7)],
                "claude-review": [(_sha("1"), "failure", 7), (_sha("2"), "failure", 7)],
            },
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n", "two heads were counted as more"

    def test_the_current_head_is_never_counted(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            reviews=[{"state": "CHANGES_REQUESTED", "commit_id": HEAD}],
            failures={
                "codex-review": [(HEAD, "failure", 7), (_sha("1"), "failure", 7)],
                "design-review": [(_sha("2"), "failure", 7)],
            },
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n", "nothing was pushed past the current head"

    def test_a_run_of_another_pull_request_is_not_counted(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            failures={
                "codex-review": [
                    (_sha("1"), "failure", 7),
                    (_sha("2"), "failure", 8),
                    (_sha("3"), "failure", 8),
                ],
            },
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"

    def test_a_cancelled_or_dismissed_signal_is_not_a_block(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            reviews=[{"state": "DISMISSED", "commit_id": _sha("1")}],
            failures={
                "codex-review": [(_sha("2"), "cancelled", 7), (_sha("3"), "skipped", 7)],
                "ux-review": [(_sha("4"), "success", 7)],
            },
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"

    def test_an_unreadable_count_skips_the_section_and_never_fails_the_step(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            failures={"codex-review": [(_sha(c), "failure", 7) for c in "1234"]},
        )
        (fixtures / "repos_example_repo_actions_workflows_ux-review.yml_runs").unlink()

        result, prompt, calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n", "a count that was never read produced a section"
        assert "::notice::" in result.stdout
        # Three bounded attempts, each reading the PR first.
        assert calls.count("repos/example/repo/pulls/7 ") == 3

    def test_without_the_text_on_the_base_nothing_is_read_or_appended(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            failures={"codex-review": [(_sha(c), "failure", 7) for c in "1234"]},
        )

        result, prompt, calls = self._run(tmp_path, fixtures, base_has_text=False)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"
        assert calls.strip() == "", "the API was read for a section that cannot be appended"

    def test_every_recorded_call_is_a_get_on_the_pull_request_or_its_runs(self, tmp_path: Path):
        fixtures = self._fixtures(
            tmp_path,
            failures={"codex-review": [(_sha(c), "failure", 7) for c in "123"]},
        )

        result, _prompt_text, calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        for line in calls.splitlines():
            if line.startswith(("event=", "branch=", "per_page=")):
                continue
            assert line.startswith("api "), line
            assert not re.search(r"--method (POST|PUT|PATCH|DELETE)", line), line
            assert "repos/example/repo/pulls/7" in line or "actions/workflows/" in line, line
        assert "branch=feature/x" in calls

    def test_a_branch_name_is_data_never_shell_or_jq_text(self, tmp_path: Path):
        marker = tmp_path / "pwned"
        branch = f'x"; touch {marker}; echo "'
        fixtures = self._fixtures(
            tmp_path,
            branch=branch,
            failures={"codex-review": [(_sha(c), "failure", 7) for c in "123"]},
        )

        result, prompt, _calls = self._run(tmp_path, fixtures)

        assert result.returncode == 0, _proc_log(result)
        assert not marker.exists()
        assert "This PR has had 3 blocked pushes." in _flat(prompt)

    def test_a_non_numeric_pr_is_refused_before_it_reaches_jq(self, tmp_path: Path):
        fixtures = self._fixtures(tmp_path)
        result, prompt, calls = self._run(tmp_path, fixtures, pr="7 or true")
        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"
        assert calls.strip() == ""


ADJ_EXTRACT = "Extract blocking findings for adjudication"


class TestTheSecurityFenceSurvivesThePushBudgetText:
    """Run the ACTUAL fence condition on items shaped the way the section asks.

    The fence reads the item's own `Anchor:` line. A recurring security finding
    that the section re-anchored would leave the fence and become eligible for
    downgrade, so the section keeps the Anchor and adds only its own lines.
    """

    SITES = "Sites: src/a.py:10, src/b.py:44 (same call, other spelling)\n"
    REMEDY = "Remedy: narrow or remove the mechanism\n"

    def _fenced(self, tmp_path: Path, body: str) -> bool:
        bash = _bash()
        if bash is None:
            pytest.skip("the fence executes only under Bash")
        script = _step_script(_workflow("codex-review.yml"), ADJ_EXTRACT)
        start = script.index('if grep -qE -- "$CHAIN_RE"') + len("if ")
        cond = script[start : script.index("; then", start)]
        env = _step_env("codex-review.yml", ADJ_EXTRACT)
        item = tmp_path / "item.md"
        item.write_text(body, encoding="utf-8", newline="\n")
        out = subprocess.run(
            [bash, "-c", f'set -uo pipefail; f="$1"; {cond}', "fence", str(item)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                **_child_env({}),
                "PATH": _stub_path(tmp_path),
                "SECURITY_RE": env["SECURITY_RE"],
                "CHAIN_RE": env["CHAIN_RE"],
            },
        )
        return out.returncode == 0

    # Wording that avoids every SECURITY_RE token on purpose: only the Anchor
    # line classifies this item as a dangerous class.
    HEAD_LINES = (
        "BLOCKING -- src/run.py:9 -- task name reaches the command builder\n"
        "Trigger: a crafted task name is queued.\n"
        "Outcome: arbitrary command execution on the host.\n"
    )

    @pytest.mark.parametrize(
        "anchor",
        [
            "residual/security",
            "residual/guard-removal",
            "residual/crash-data-loss-corruption",
            "harness-parity",
            "no-test-side-effects",
        ],
    )
    def test_a_recurring_dangerous_class_item_stays_fenced(self, tmp_path: Path, anchor: str):
        plain = f"{self.HEAD_LINES}Anchor: {anchor}\nFix: quote it.\n"
        assert self._fenced(tmp_path, plain), "the baseline item must be fenced"

        with_budget = plain + self.SITES + self.REMEDY
        assert self._fenced(tmp_path, with_budget), anchor

    def test_a_remedy_or_sites_line_alone_never_fences_an_ordinary_item(self, tmp_path: Path):
        ordinary = (
            "BLOCKING -- src/ui.py:7 -- the banner renders twice\n"
            "Trigger: a stale flag reaches the retry loop.\n"
            "Outcome: the banner renders twice.\n"
            "Anchor: defect\n"
            "Fix: reset the flag.\n"
        )
        assert not self._fenced(tmp_path, ordinary)
        assert not self._fenced(tmp_path, ordinary + self.SITES + self.REMEDY)

    def test_reanchoring_would_have_unfenced_a_security_item(self, tmp_path: Path):
        """Why the section must not reassign the Anchor: this is the loss."""
        reanchored = f"{self.HEAD_LINES}Anchor: design\nFix: quote it.\n" + self.REMEDY

        assert not self._fenced(tmp_path, reanchored)

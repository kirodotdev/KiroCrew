"""The GPT lane's push-budget section: shape, placement, and the real count.

After a PR has had three blocked pushes, ``codex-review.yml`` appends a short
section from ``.github/review-prompts/gpt-push-budget.md`` that asks the
reviewer to list sibling sites. A first-time review must stay exactly as long as
it was, so the section is conditional, staged outside the shared prompt loop, and
never part of ``gpt-review-core.md``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
from prepare_pr_rounds_helpers import _contract
from workflow_token_scopes import WORKFLOWS, load_workflow, missing_scopes

PROMPT_NAME = "gpt-push-budget.md"
STEP = "Write review prompt"
HEAD = "a" * 40
COUNTER_TREE = "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts"
ROOT = REVIEW_PROMPTS.parents[1]


def _budget_block() -> str:
    script = _step_script(_workflow("codex-review.yml"), STEP)
    return script[script.index("PUSH_BUDGET_MIN=") :]


class TestThePromptText:
    def test_it_carries_the_designed_instruction(self) -> None:
        text = _flat(_prompt(PROMPT_NAME))

        assert "This PR has had at least %BLOCKED_PUSHES% blocked pushes." in text
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

    def test_the_count_is_the_base_commits_blocked_push_count(self) -> None:
        """One definition: the block runs `_review_pushes.py`, the module `--rounds`
        and the sweep check call, staged from the BASE commit outside the workspace."""
        block = _budget_block()

        assert "gh api" not in block, "the lane must not count blocked pushes on its own"
        assert f'counter_tree="{COUNTER_TREE}"' in block
        assert 'counter_dir="${RUNNER_TEMP:-/tmp}/push-budget-count"' in block
        assert 'git archive --format=tar "$BASE_SHA" -- "$counter_tree"' in block
        assert (
            'REVIEW_PUSHES_STOP_AT="$PUSH_BUDGET_MIN" \\\n'
            '    python3 "$counter_dir/$counter_tree/_review_pushes.py"'
            ' --repo "$REPO" --pr "$PR"' in block
        ), "the count stops at the threshold, the only number the section needs"
        assert (ROOT / COUNTER_TREE / "_review_pushes.py").is_file()
        assert "gh pr " not in block and "gh issue " not in block
        assert "--input" not in block

    def test_the_run_block_stays_free_of_template_expressions(self) -> None:
        # A run block with an expression is capped at 21,000 characters.
        assert "${{" not in _step_script(_workflow("codex-review.yml"), STEP)

    def test_the_token_can_read_what_the_count_reads(self) -> None:
        workflow = load_workflow(WORKFLOWS / "codex-review.yml")

        # The PR timeline (issues/<n>/timeline) and each head's check runs.
        assert workflow["permissions"]["issues"] == "read"
        assert workflow["permissions"]["checks"] == "read"
        assert not missing_scopes("codex-review.yml", workflow)

    def test_no_write_scope_was_added(self) -> None:
        permissions = load_workflow(WORKFLOWS / "codex-review.yml")["permissions"]

        assert permissions == {
            "contents": "read",
            "pull-requests": "write",
            "id-token": "write",
            "issues": "read",
            "checks": "read",
        }


# ``git`` and ``sleep`` are shell FUNCTIONS prepended to the extracted block, not
# files on PATH: a function wins over any executable of the same name, whereas a
# PATH stub can lose to the real ``git``. ``git archive`` hands back this checkout's
# counting scripts as a tar, standing in for the base commit's. ``gh`` and
# ``python3`` are PATH shims instead, because the count runs in Python and calls
# ``gh`` as a child process, where a shell function is not visible.
_STUBS = r"""
git() {
  echo "$@" >> "$GIT_CALLS"
  case "$1" in
    show)
      if [ -n "${BUDGET_TEXT:-}" ] && [ -f "$BUDGET_TEXT" ]; then
        cat "$BUDGET_TEXT"
        return 0
      fi
      ;;
    archive)
      if [ -n "${COUNTER_SRC:-}" ]; then
        local last
        for last; do :; done
        tar -cf - -C "$COUNTER_SRC" "$last"
        return $?
      fi
      ;;
  esac
  echo "fatal: path does not exist in the base commit" >&2
  return 128
}
sleep() { return 0; }
"""

# The PR's GitHub history, served to the count from a JSON fixture. Check-run ids
# rise in list order, so a later row is the newer run of its lane.
_GH_STUB = r"""
import json, os, re, sys
with open(os.environ["CALLS"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
with open(os.environ["GH_FIXTURE"], encoding="utf-8") as f:
    fx = json.load(f)
args = sys.argv[1:]
if len(args) != 2 or args[0] != "api":
    sys.stderr.write("unexpected gh call\n")
    sys.exit(2)
url = args[1]
page = int(re.search(r"[?&]page=(\d+)", url).group(1))
if "/timeline" in url:
    if fx.get("broken_timeline"):
        sys.stderr.write("gh: HTTP 502\n")
        sys.exit(1)
    print(json.dumps(fx["events"][(page - 1) * 100 : page * 100]))
    sys.exit(0)
sha = re.search(r"/commits/([0-9a-f]+)/check-runs", url).group(1)
rows = [
    {"id": i, "name": name, "conclusion": conclusion, "status": "completed"}
    for i, (name, conclusion) in enumerate(fx["checks"].get(sha, []), 1)
]
print(json.dumps({"total_count": len(rows), "check_runs": rows[(page - 1) * 100 : page * 100]}))
"""


def _sha(ch: str) -> str:
    return ch * 40


def _force(sha: str) -> dict:
    return {"event": "head_ref_force_pushed", "commit_id": sha}


def _review(sha: str, state: str = "changes_requested", rid: int = 1) -> dict:
    return {
        "event": "reviewed",
        "id": rid,
        "state": state,
        "commit_id": sha,
        "user": {"login": "alice"},
    }


SCOPE = "Security Scope Review"

# Five heads; the last is the head under review. A Security-Scope-only block, a
# GPT block, an Opus block and a Design block replaced: four blocked pushes.
FOUR_BLOCKED = {
    "events": [_force(_sha(c)) for c in "12345"],
    "checks": {
        _sha("1"): [(SCOPE, "failure"), ("GPT 6.1 Review", "success")],
        _sha("2"): [("GPT 6.1 Review", "failure")],
        _sha("3"): [("Opus 5.5 Review", "failure")],
        _sha("4"): [("Design Review", "failure")],
    },
}

# Three blocked pushes, the first by Security Scope Review alone.
SCOPE_TIPS_IT = {
    "events": [_force(_sha(c)) for c in "1234"],
    "checks": {
        _sha("1"): [(SCOPE, "failure")],
        _sha("2"): [("UX Review", "failure")],
        _sha("3"): [("First Principles Review", "failure")],
    },
}

# Every earlier head was blocked once, but an approval cleared the first and a
# green re-run cleared the second: one blocked push is left. A count of every
# head that ever failed or drew a request would say three.
CLEARED = {
    "events": [
        _force(_sha("1")),
        _review(_sha("1")),
        _review(_sha("1"), "approved", rid=2),
        _force(_sha("2")),
        _force(_sha("3")),
        _force(_sha("4")),
    ],
    "checks": {
        _sha("2"): [("GPT 6.1 Review", "failure"), ("GPT 6.1 Review", "success")],
        _sha("3"): [(SCOPE, "failure")],
    },
}


@pytest.mark.skipif(
    os.name == "nt",
    reason="the gh and python3 shims are shell scripts, which a Windows child process cannot run",
)
class TestTheCountRunsAgainstStubbedReads:
    """Run the ACTUAL block and the staged ``_review_pushes.py`` against one history."""

    def _shims(self, tmp_path: Path) -> None:
        stub = tmp_path / "gh_stub.py"
        stub.write_text(_GH_STUB, encoding="utf-8")
        for name, body in (
            ("gh", f'exec "{sys.executable}" "{stub}" "$@"'),
            ("python3", f'exec "{sys.executable}" "$@"'),
        ):
            shim = tmp_path / name
            shim.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            shim.chmod(0o755)

    def _run(
        self,
        tmp_path: Path,
        history: dict,
        *,
        base_has_text: bool = True,
        base_has_counter: bool = True,
        pr: str = "7",
    ):
        bash = _bash()
        if bash is None:
            pytest.skip("the block is Bash; skip where Bash is absent")
        self._shims(tmp_path)
        fixture = tmp_path / "history.json"
        fixture.write_text(json.dumps(history), encoding="utf-8")
        prompt = tmp_path / "codex-prompt.md"
        prompt.write_text("EXISTING PROMPT\n", encoding="utf-8", newline="\n")
        calls = tmp_path / "calls.txt"
        calls.write_text("", encoding="utf-8")
        git_calls = tmp_path / "git-calls.txt"
        git_calls.write_text("", encoding="utf-8")
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        script = _STUBS + _budget_block().replace("/tmp/codex-prompt.md", '"$PROMPT_OUT"')
        (tmp_path / ".review-prompts-gpt").mkdir()
        env = {
            "PATH": _stub_path(tmp_path),
            "GH_CONFIG_DIR": str(tmp_path),
            "LC_ALL": "C",
            "REPO": "example/repo",
            "PR": pr,
            "HEAD": HEAD,
            "BASE_SHA": "b" * 40,
            "CALLS": str(calls),
            "GIT_CALLS": str(git_calls),
            "GH_FIXTURE": str(fixture),
            "PROMPT_OUT": str(prompt),
            "RUNNER_TEMP": str(runner_temp),
            "TMPDIR": str(tmp_path),
        }
        if base_has_text:
            env["BUDGET_TEXT"] = str(REVIEW_PROMPTS / PROMPT_NAME)
        if base_has_counter:
            env["COUNTER_SRC"] = str(ROOT)
        result = subprocess.run(
            [bash, "-e", _script_file(tmp_path, script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(env),
            cwd=tmp_path,
        )
        return (
            result,
            prompt.read_text(encoding="utf-8"),
            calls.read_text(encoding="utf-8"),
            git_calls.read_text(encoding="utf-8"),
        )

    def _rounds_count(self, tmp_path: Path, history: dict) -> int | None:
        """What `--rounds` counts on the same history, through the same gh stub."""
        self._shims(tmp_path)
        fixture = tmp_path / "rounds-history.json"
        fixture.write_text(json.dumps(history), encoding="utf-8")
        env = {
            **os.environ,
            "CALLS": str(tmp_path / "rounds-calls.txt"),
            "GH_FIXTURE": str(fixture),
        }

        def run(args: list[str]):
            proc = subprocess.run(
                [sys.executable, str(tmp_path / "gh_stub.py"), *args[1:]],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr

        return _contract().blocked_push_count("example/repo", 7, run)

    @pytest.mark.parametrize("history", [FOUR_BLOCKED, SCOPE_TIPS_IT], ids=["four", "scope"])
    def test_the_gpt_lane_reports_the_threshold_the_rounds_count_reaches(
        self, tmp_path: Path, history
    ):
        """Same definition as `--rounds`, stopped at the threshold the section needs."""
        expected = self._rounds_count(tmp_path, history)
        assert expected is not None and expected >= 3

        result, prompt, _calls, _git = self._run(tmp_path, history)

        assert result.returncode == 0, _proc_log(result)
        assert prompt.startswith("EXISTING PROMPT\n")
        assert "This PR has had at least 3 blocked pushes." in _flat(prompt)
        assert "%BLOCKED_PUSHES%" not in prompt
        assert "`Sites:` line" in prompt

    def test_the_count_stops_reading_heads_at_the_threshold(self, tmp_path: Path):
        """A full count reads every replaced head; the lane reads only until three."""
        self._rounds_count(tmp_path, FOUR_BLOCKED)
        full = (tmp_path / "rounds-calls.txt").read_text(encoding="utf-8")

        result, _prompt, calls, _git = self._run(tmp_path, FOUR_BLOCKED)

        assert result.returncode == 0, _proc_log(result)
        assert full.count("/check-runs") == 4
        assert calls.count("/check-runs") == 3

    def test_a_security_scope_block_tips_the_count_to_three(self, tmp_path: Path):
        result, prompt, _calls, _git = self._run(tmp_path, SCOPE_TIPS_IT)

        assert result.returncode == 0, _proc_log(result)
        assert "This PR has had at least 3 blocked pushes." in _flat(prompt)

    def test_an_approval_and_a_green_rerun_clear_their_heads(self, tmp_path: Path):
        assert self._rounds_count(tmp_path, CLEARED) == 1

        result, prompt, _calls, _git = self._run(tmp_path, CLEARED)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n", "cleared heads were counted as blocked"

    def test_the_current_head_is_never_counted(self, tmp_path: Path):
        history = {
            "events": [_force(_sha(c)) for c in "123"] + [_review(_sha("3"))],
            "checks": {h: [("GPT 6.1 Review", "failure")] for h in map(_sha, "123")},
        }

        result, prompt, _calls, _git = self._run(tmp_path, history)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n", "nothing was pushed past the current head"

    def test_an_unreadable_count_skips_the_section_and_never_fails_the_step(self, tmp_path: Path):
        history = {**FOUR_BLOCKED, "broken_timeline": True}

        result, prompt, calls, _git = self._run(tmp_path, history)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n", "a count that was never read produced a section"
        assert "::notice::" in result.stdout
        # Three bounded attempts, each reading the timeline first.
        assert calls.count("/timeline?") == 3

    def test_without_the_text_on_the_base_nothing_is_staged_read_or_appended(self, tmp_path: Path):
        result, prompt, calls, git_calls = self._run(tmp_path, FOUR_BLOCKED, base_has_text=False)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"
        assert calls.strip() == "", "the API was read for a section that cannot be appended"
        assert "archive" not in git_calls

    def test_without_the_counting_scripts_on_the_base_the_section_is_off(self, tmp_path: Path):
        result, prompt, calls, _git = self._run(tmp_path, FOUR_BLOCKED, base_has_counter=False)

        assert result.returncode == 0, _proc_log(result)
        assert prompt == "EXISTING PROMPT\n"
        assert "::notice::" in result.stdout
        assert calls.strip() == "", "no checkout copy of the counter may stand in"

    def test_the_scripts_come_from_the_base_commit(self, tmp_path: Path):
        result, _prompt_text, _calls, git_calls = self._run(tmp_path, FOUR_BLOCKED)

        assert result.returncode == 0, _proc_log(result)
        assert f"archive --format=tar {'b' * 40} -- {COUNTER_TREE}" in git_calls
        staged = tmp_path / "runner-temp" / "push-budget-count" / COUNTER_TREE
        assert (staged / "_review_pushes.py").is_file()

    def test_every_recorded_call_is_a_get_on_the_pull_request_or_a_head(self, tmp_path: Path):
        result, _prompt_text, calls, _git = self._run(tmp_path, FOUR_BLOCKED)

        assert result.returncode == 0, _proc_log(result)
        lines = calls.splitlines()
        assert lines
        for line in lines:
            words = line.split()
            assert words[0] == "api" and len(words) == 2, line
            assert re.match(
                r"repos/example/repo/(issues/7/timeline|commits/[0-9a-f]{40}/check-runs)\?",
                words[1],
            ), line

    def test_a_non_numeric_pr_is_refused_before_any_read(self, tmp_path: Path):
        result, prompt, calls, _git = self._run(tmp_path, FOUR_BLOCKED, pr="7 or true")

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

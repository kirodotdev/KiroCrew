"""``diff_signals.py --check-body`` and the Pattern harvest rule (exit 26).

CI's PR Hygiene step fails a ``fix``/``revert`` pull request whose body has no
``## Pattern harvest`` heading, or whose section has no ``Rule candidate:`` or
``Not generalizable:`` line. The rule is inline shell in ``code-review.yml``, so
``--check-body`` carries a copy of its three patterns; this file pins the copy to
the workflow and checks the local verdicts match CI's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "diff_signals.py"
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"

ds = load_skill_script("prepare_pr_diff_signals_harvest_under_test", SCRIPT)

MISSING_SECTION = "Fix/revert PRs must carry a '## Pattern harvest' section."
MISSING_LINE = (
    "The Pattern harvest section must contain a 'Rule candidate:' or 'Not generalizable:' line."
)


def test_the_copied_patterns_are_the_workflows() -> None:
    # The step greps each pattern with -qiE; a change there must change the copy too.
    text = WORKFLOW.read_text(encoding="utf-8")
    assert f"grep -qiE '{ds.HARVEST_TITLE_RE}'" in text
    assert f"grep -qiE '{ds.HARVEST_HEADING_RE}'" in text
    assert f"grep -qiE '{ds.HARVEST_LINE_RE}'" in text
    assert f'err "{MISSING_SECTION}"' in text
    assert f'err "{MISSING_LINE}"' in text


@pytest.mark.parametrize(
    "body",
    [
        "## Pattern harvest\n\nNot generalizable: one typo.\n",
        "### Pattern harvest\nRule candidate: semgrep\nPattern: x\n",
        "## pattern harvest\n  not generalizable : one-off\n",
    ],
)
def test_a_fix_body_ci_accepts_passes(body: str) -> None:
    assert ds.harvest_problem("fix(cli): x", body) is None


def test_a_fix_body_without_the_heading_is_refused() -> None:
    assert ds.harvest_problem("fix: x", "## Tests\n\nx\n") == MISSING_SECTION


@pytest.mark.parametrize(
    "line",
    [
        "Not generalizable beyond this one file.",  # the words, but no colon after them
        "This is not generalizable: it is one typo.",  # the words are not at line start
    ],
)
def test_a_heading_without_a_matching_line_is_refused(line: str) -> None:
    body = f"## Pattern harvest\n\n{line}\n"
    assert ds.harvest_problem("revert: x", body) == MISSING_LINE


@pytest.mark.parametrize("title", ["docs(install): x", "feat: x", "test: fix flake", "fixup: x"])
def test_a_non_fix_title_needs_no_section(title: str) -> None:
    assert ds.harvest_problem(title, "## Tests\n\nx\n") is None


def test_check_body_reports_exit_26(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fake_run(cmd):
        if cmd[:2] == ["git", "log"]:
            return 0, "fix(cli): name the cap\nchore: second\n", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(ds, "run", fake_run)
    assert ds.harvest_check("## Pattern harvest\n\nNot generalizable beyond x\n", "main") == 26
    assert "PATTERN HARVEST: " + MISSING_LINE in capsys.readouterr().out
    assert ds.harvest_check("## Pattern harvest\n\nNot generalizable: x\n", "main") == 0


# End to end through the real script: a throwaway repo whose branch is a fix, a body
# PR Hygiene would fail, and the exit code ``--check-body`` returns for it.
_BODY = "## What changed\n\nmod.py reads the last char. test_mod.py pins it.\n\n## Tests\n\nx\n\n"


def test_the_script_exits_26_on_a_fix_body_ci_would_fail(tmp_path: Path) -> None:
    from test_prepare_pr_diff_signals_proof import _make_repo, _run

    repo = _make_repo(tmp_path, enforce_from=None, script=False)
    rc, out = _run(repo, _BODY + "## Pattern harvest\n\nNot generalizable beyond mod.py\n")
    assert rc == 26, out
    assert "PATTERN HARVEST: " + MISSING_LINE in out
    rc, out = _run(repo, _BODY)
    assert rc == 26, out
    assert "PATTERN HARVEST: " + MISSING_SECTION in out


def test_the_script_accepts_the_same_body_with_the_line(tmp_path: Path) -> None:
    from test_prepare_pr_diff_signals_proof import _make_repo, _run

    repo = _make_repo(tmp_path, enforce_from=None, script=False)
    rc, out = _run(repo, _BODY + "## Pattern harvest\n\nNot generalizable: one index\n")
    assert rc == 0, out
    assert "PATTERN HARVEST" not in out

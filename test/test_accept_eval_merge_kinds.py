"""The ``pr_merged`` and ``crux_cr`` acceptance kinds of the bundled evaluator.

Both kinds exist so a later work item can start on MERGED code: ``pr_checks`` only
says a pull request's checks are green. Three properties are pinned here, each the
way the rest of the evaluator's tests pin them:

* **Spec validation.** A malformed field is an ``error`` verdict and costs no
  subprocess.
* **No spec text in argv.** Every argv is the fixed template with only the
  validated field substituted, whatever else the spec carries.
* **Exit-code mapping.** Each command outcome becomes exactly one verdict, and an
  outcome the evaluator cannot vouch for is an ``error``, never ``fail`` or
  ``pass``.

No network and no real ``gh`` or ``kirocrew``: ``subprocess.run`` is replaced by a
script that records every argv and answers from a fixed list.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import work_ledger as wl
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SCRIPT = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "scripts" / "accept_eval.py"

CR = "CR-311204923"
REPO = "owner/name"

CRUX_ARGV = ["kirocrew", "crux-status", "--json", CR]
MERGED_ARGV = ["gh", "pr", "view", "42", "--repo", REPO, "--json", "state", "-q", ".state"]


class _Proc:
    """The three ``CompletedProcess`` fields the evaluator reads."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load():
    return load_skill_script("_accept_eval_merge_kinds_under_test", SCRIPT)


def _wire(mod, monkeypatch, answers):
    """Answer ``subprocess.run`` from ``answers`` in order; return the argvs seen.

    An answer that is an exception instance is raised instead of returned.
    """
    seen: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        answer = answers[len(seen) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return seen


def _report(verdict: str, cr: str = CR, summary: str = "the summary line") -> str:
    return json.dumps({"cr": cr, "verdict": verdict, "summary": summary})


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        None,
        311204923,
        True,
        "",
        "CR-12345",  # five digits
        "CR-1234567890123",  # thirteen digits
        "cr-311204923",
        "CR311204923",
        "CR-311204923\n",
        " CR-311204923",
        "CR-311204923 --verbose",
        "--help",
        "CR-\u0663\u0661\u0661\u0662\u0660\u0664",  # non-ASCII digits match \d, not [0-9]
        "https://code.example/reviews/CR-311204923",
    ],
)
def test_crux_cr_rejects_a_malformed_cr_before_any_subprocess(monkeypatch, bad):
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, evidence = mod._evaluate({"accept": {"kind": "crux_cr", "cr": bad}})
    assert verdict == "error", bad
    assert "CR-123456" in evidence
    assert seen == []


def test_crux_cr_without_a_cr_is_an_error(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, _ = mod._evaluate({"accept": {"kind": "crux_cr"}})
    assert verdict == "error"
    assert seen == []


@pytest.mark.parametrize("bad", [None, "42", True, False, 0, -3, 4.2, [42]])
def test_pr_merged_rejects_a_non_positive_or_non_integer_pr(monkeypatch, bad):
    """``-3`` matters most: as argv it would reach ``gh`` as a flag."""
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_merged", "pr": bad, "repo": REPO}})
    assert verdict == "error", bad
    assert "positive integer pr" in evidence
    assert seen == []


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "owner",
        "-owner/name",
        "owner/name/extra",
        "owner/name --hostname evil.example",
        "owner/name\n",
        "https://github.com/owner/name",
        "github.com/owner/name",
        17,
    ],
)
def test_pr_merged_rejects_a_repo_that_is_not_owner_slash_name(monkeypatch, bad):
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": bad}})
    assert verdict == "error", bad
    assert "owner/name" in evidence
    assert seen == []


def test_pr_merged_requires_a_repo(monkeypatch):
    """Unlike ``pr_checks``: without ``--repo`` the number resolves against
    whatever checkout the evaluator happens to run in."""
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, _ = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42}})
    assert verdict == "error"
    assert seen == []


# --------------------------------------------------------------------------- #
# No spec text reaches argv
# --------------------------------------------------------------------------- #

_HOSTILE_EXTRAS = {
    "argv": ["sh", "-c", "id"],
    "command": "id",
    "cmd": "id",
    "shell": "id",
    "args": ["--hostname", "evil.example"],
    "revision": "--verbose",
    "flags": ["--jq", ".body"],
}


def test_crux_cr_runs_only_its_fixed_template(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, _report("pass"))])
    verdict, _ = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR, **_HOSTILE_EXTRAS}})
    assert verdict == "pass"
    assert seen == [CRUX_ARGV]


def test_pr_merged_runs_only_its_fixed_template(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, "MERGED\n")])
    verdict, _ = mod._evaluate(
        {"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO, **_HOSTILE_EXTRAS}}
    )
    assert verdict == "pass"
    assert seen == [MERGED_ARGV]


def test_every_argv_element_is_a_template_literal_or_a_validated_field(monkeypatch):
    """The general form of the two tests above: across both kinds, the only
    non-literal elements are the validated values themselves."""
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, _report("pass")), _Proc(0, "MERGED")])
    mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR, **_HOSTILE_EXTRAS}})
    mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO, **_HOSTILE_EXTRAS}})
    literals = {"kirocrew", "crux-status", "--json", "gh", "pr", "view", "--repo", "state", "-q"}
    literals |= {".state"}
    validated = {CR, "42", REPO}
    for argv in seen:
        assert argv[0] in mod._SELF_BUILT_COMMANDS
        for element in argv:
            assert element in literals | validated, (argv, element)


def test_the_self_built_commands_are_exactly_gh_and_kirocrew():
    mod = _load()
    assert mod._SELF_BUILT_COMMANDS == {"gh", "kirocrew"}


def test_the_ledger_mirror_uses_the_evaluators_own_patterns():
    """``is_acceptance_concrete`` duplicates these across a process boundary, so
    they are pinned equal rather than trusted to stay in step."""
    mod = _load()
    assert wl.ACCEPTANCE_CR_ID.pattern == mod._CR_ID.pattern
    assert wl.ACCEPTANCE_GH_REPO.pattern == mod._GH_REPO.pattern


# --------------------------------------------------------------------------- #
# Exit-code mapping: crux_cr
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("returncode", "report_verdict", "verdict"),
    [
        (0, "pass", "pass"),
        (8, "pending", "pending"),
        (1, "fail", "fail"),
        (3, "needs_person", "refused"),
    ],
)
def test_crux_cr_maps_each_agreeing_outcome_to_one_verdict(
    monkeypatch, returncode, report_verdict, verdict
):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(returncode, _report(report_verdict, summary="why it reads so"))])
    got, evidence = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR}})
    assert got == verdict
    assert "why it reads so" in evidence


def test_a_needs_person_refusal_says_a_person_decides(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(3, _report("needs_person", summary="merged without X"))])
    verdict, evidence = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR}})
    assert verdict == "refused"
    assert "a person decides" in evidence


def test_a_kirocrew_without_the_command_is_an_error_that_names_the_need(monkeypatch):
    """The public ``kirocrew`` has no ``crux-status``: argparse exits 2."""
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(2, "", "kirocrew: error: argument <command>: invalid choice")])
    verdict, evidence = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR}})
    assert verdict == "error"
    assert "needs an edition that ships that command" in evidence


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        # A crash also exits 1; with no report it must not read as a failed review.
        (1, "Traceback (most recent call last):\n  ...\nKeyError: 'x'\n"),
        # The exit code and the report disagree, in both directions.
        (0, _report("fail")),
        (1, _report("pass")),
        (8, _report("pass")),
        # A report about some other review.
        (0, _report("pass", cr="CR-999999999")),
        # Not one JSON object.
        (0, "pass"),
        (0, json.dumps(["pass"])),
        (0, _report("pass") + "\nextra line"),
        # An exit code with no meaning in the contract.
        (4, _report("pass")),
        (137, ""),
    ],
)
def test_an_outcome_the_evaluator_cannot_vouch_for_is_an_error(monkeypatch, returncode, stdout):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(returncode, stdout)])
    verdict, _ = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR}})
    assert verdict == "error", (returncode, stdout)


def test_crux_cr_with_no_kirocrew_on_path_is_an_error(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [FileNotFoundError("kirocrew")])
    verdict, evidence = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR}})
    assert verdict == "error"
    assert "not found on PATH" in evidence


def test_a_hung_crux_status_is_an_error(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [subprocess.TimeoutExpired(CRUX_ARGV, mod.TIMEOUT_SECS)])
    verdict, evidence = mod._evaluate({"accept": {"kind": "crux_cr", "cr": CR}})
    assert verdict == "error"
    assert "timed out" in evidence


# --------------------------------------------------------------------------- #
# Exit-code mapping: pr_merged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stdout", "verdict"),
    [
        ("MERGED\n", "pass"),
        ("merged", "pass"),
        ("OPEN\n", "pending"),
        ("CLOSED\n", "fail"),
        ("", "error"),
        ("SOMETHING_NEW\n", "error"),
    ],
)
def test_pr_merged_maps_each_state_to_one_verdict(monkeypatch, stdout, verdict):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(0, stdout)])
    got, _ = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO}})
    assert got == verdict, stdout


@pytest.mark.parametrize("returncode", [1, 4, 8])
def test_a_gh_that_cannot_read_the_pr_is_an_error_not_a_fail(monkeypatch, returncode):
    """No such PR, no auth, a rate limit: none of them says anything about the work."""
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(returncode, "", "GraphQL: Could not resolve to a PullRequest")])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO}})
    assert verdict == "error"
    assert "could not read PR #42" in evidence


# --------------------------------------------------------------------------- #
# The whole script, over stdin
# --------------------------------------------------------------------------- #


def test_the_new_kinds_reject_bad_specs_end_to_end(tmp_path):
    """Through the real script: a malformed spec for each new kind comes back
    ``error`` beside a sibling that still evaluates."""
    items = [
        {"id": "cr", "accept": {"kind": "crux_cr", "cr": "CR-1"}},
        {"id": "pr", "accept": {"kind": "pr_merged", "pr": 7, "repo": "nope"}},
        {"id": "human", "accept": {"kind": "human_approval"}},
    ]
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"items": items}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    out = {row["id"]: row["verdict"] for row in json.loads(proc.stdout)["results"]}
    assert out == {"cr": "error", "pr": "error", "human": "pending"}

"""The ``pr_merged`` acceptance kind of the bundled evaluator.

The kind exists so a later work item can start on MERGED code: ``pr_checks`` only
says a pull request's checks are green. Three properties are pinned here, each the
way the rest of the evaluator's tests pin them:

* **Spec validation.** A malformed field is an ``error`` verdict and costs no
  subprocess.
* **No spec text in argv.** The argv is the fixed template with only the validated
  fields substituted, whatever else the spec carries.
* **State mapping.** Each state ``gh`` reports becomes exactly one verdict, and an
  answer the evaluator cannot vouch for is an ``error``, never ``fail`` or
  ``pass``.

No network and no real ``gh``: ``subprocess.run`` is replaced by a script that
records every argv and answers from a fixed list.
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

REPO = "owner/name"
MERGED_ARGV = ["gh", "pr", "view", "42", "--repo", REPO, "--json", "state", "-q", ".state"]


class _Proc:
    """The three ``CompletedProcess`` fields the evaluator reads."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load():
    return load_skill_script("_accept_eval_pr_merged_under_test", SCRIPT)


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


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #


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
        " owner/name",
        "own\u0435r/name",  # a Cyrillic letter that looks like a Latin "e"
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
    "flags": ["--jq", ".body"],
}


def test_pr_merged_runs_only_its_fixed_template(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, "MERGED\n")])
    verdict, _ = mod._evaluate(
        {"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO, **_HOSTILE_EXTRAS}}
    )
    assert verdict == "pass"
    assert seen == [MERGED_ARGV]


def test_every_argv_element_is_a_template_literal_or_a_validated_field(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, "MERGED")])
    mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO, **_HOSTILE_EXTRAS}})
    literals = {"gh", "pr", "view", "--repo", "--json", "state", "-q", ".state"}
    validated = {"42", REPO}
    [argv] = seen
    assert argv[0] in mod._SELF_BUILT_COMMANDS
    for element in argv:
        assert element in literals | validated, (argv, element)


def test_the_self_built_commands_are_still_only_gh():
    mod = _load()
    assert mod._SELF_BUILT_COMMANDS == {"gh"}


def test_the_ledger_mirror_uses_the_evaluators_own_pattern():
    """``is_acceptance_concrete`` duplicates it across a process boundary, so the
    two are pinned equal rather than trusted to stay in step."""
    mod = _load()
    assert wl.ACCEPTANCE_GH_REPO.pattern == mod._GH_REPO.pattern


# --------------------------------------------------------------------------- #
# State mapping
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


def test_no_gh_on_path_is_an_error(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [FileNotFoundError("gh")])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO}})
    assert verdict == "error"
    assert "not found on PATH" in evidence


def test_a_hung_gh_is_an_error(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [subprocess.TimeoutExpired(MERGED_ARGV, mod.TIMEOUT_SECS)])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 42, "repo": REPO}})
    assert verdict == "error"
    assert "timed out" in evidence


# --------------------------------------------------------------------------- #
# The whole script, over stdin
# --------------------------------------------------------------------------- #


def test_a_bad_pr_merged_spec_is_an_error_end_to_end(tmp_path):
    """Through the real script: a malformed spec comes back ``error`` beside a
    sibling that still evaluates."""
    items = [
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
    assert out == {"pr": "error", "human": "pending"}

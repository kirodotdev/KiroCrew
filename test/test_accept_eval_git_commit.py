"""The ``git_commit`` acceptance kind of the bundled evaluator.

The kind exists so work that ends in a local commit can be accepted on the commit
itself, instead of on a hand-over file written only so a ``file`` bar can pass.
Pinned here:

* **Verdicts on a real repository.** A branch with a commit past its base passes;
  no such branch, or nothing past the base, is ``pending``; a base the repository
  does not hold is ``error``.
* **Spec validation.** A relative repo is ``refused``; a malformed field is an
  ``error`` and costs no subprocess.
* **No spec text in argv.** Every argv is the fixed template with only the
  validated fields substituted.
* **The ledger mirror.** ``is_acceptance_concrete`` uses the evaluator's patterns.

Only local ``git`` runs: no network, no remote.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import mcp_core, mcp_work
from kiro_crew import work_ledger as wl
from kiro_crew import work_ledger_records as records
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SCRIPT = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "scripts" / "accept_eval.py"


def _load():
    return load_skill_script("_accept_eval_git_commit_under_test", SCRIPT)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repository on ``main`` with one commit; returns ``(path, base sha)``."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("a", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _spec(path, base, branch="feat/x"):
    return {"kind": "git_commit", "repo": str(path), "branch": branch, "base": base}


def test_a_branch_with_a_commit_past_its_base_passes(repo):
    path, base = repo
    _git(path, "switch", "-q", "-c", "feat/x")
    (path / "b.txt").write_text("b", encoding="utf-8")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-q", "-m", "work")
    head = _git(path, "rev-parse", "HEAD")
    verdict, evidence = _load()._evaluate({"accept": _spec(path, base)})
    assert verdict == "pass", evidence
    assert "1 commit(s)" in evidence and head in evidence


def test_an_abbreviated_base_is_accepted(repo):
    path, base = repo
    _git(path, "switch", "-q", "-c", "feat/x")
    _git(path, "commit", "-q", "--allow-empty", "-m", "work")
    assert _load()._evaluate({"accept": _spec(path, base[:10])})[0] == "pass"


def test_a_branch_with_nothing_past_its_base_is_pending(repo):
    path, base = repo
    _git(path, "branch", "feat/x")
    verdict, evidence = _load()._evaluate({"accept": _spec(path, base)})
    assert verdict == "pending", evidence
    assert "no commit past" in evidence


def test_a_branch_that_does_not_exist_yet_is_pending(repo):
    path, base = repo
    verdict, evidence = _load()._evaluate({"accept": _spec(path, base)})
    assert verdict == "pending", evidence
    assert "does not exist" in evidence


def test_only_a_local_branch_counts(repo):
    """``branch`` is read as ``refs/heads/<branch>``, so a tag of that name does not."""
    path, base = repo
    _git(path, "switch", "-q", "-c", "other")
    _git(path, "commit", "-q", "--allow-empty", "-m", "work")
    _git(path, "tag", "feat/x")
    assert _load()._evaluate({"accept": _spec(path, base)})[0] == "pending"


def test_a_base_the_repository_does_not_hold_is_an_error(repo):
    path, _base = repo
    _git(path, "branch", "feat/x")
    verdict, evidence = _load()._evaluate({"accept": _spec(path, "0" * 40)})
    assert verdict == "error"
    assert "is not a commit" in evidence


def test_a_directory_that_is_not_a_repository_is_an_error(tmp_path):
    verdict, _ = _load()._evaluate({"accept": _spec(tmp_path, "a" * 40)})
    assert verdict == "error"


@pytest.fixture
def no_exec(monkeypatch):
    """Fail the test if any subprocess runs."""
    mod = _load()

    def _boom(*args, **kwargs):
        raise AssertionError("no subprocess may run for a malformed spec")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    return mod


def test_a_relative_repo_is_refused_without_running_git(no_exec):
    verdict, evidence = no_exec._evaluate({"accept": _spec("repo", "a" * 40)})
    assert verdict == "refused"
    assert "absolute path" in evidence


@pytest.mark.parametrize(
    "spec",
    [
        {"branch": "feat/x", "base": "a" * 40},
        {"repo": "", "branch": "feat/x", "base": "a" * 40},
        {"repo": 7, "branch": "feat/x", "base": "a" * 40},
        {"repo": "/r\x00", "branch": "feat/x", "base": "a" * 40},
        {"repo": "/r", "base": "a" * 40},
        {"repo": "/r", "branch": "-x", "base": "a" * 40},
        {"repo": "/r", "branch": "a..b", "base": "a" * 40},
        {"repo": "/r", "branch": "a b", "base": "a" * 40},
        {"repo": "/r", "branch": "x\n", "base": "a" * 40},
        {"repo": "/r", "branch": "feat/x"},
        {"repo": "/r", "branch": "feat/x", "base": "HEAD"},
        {"repo": "/r", "branch": "feat/x", "base": "abc"},
        {"repo": "/r", "branch": "feat/x", "base": "A" * 40},
        {"repo": "/r", "branch": "feat/x", "base": "a" * 40 + "\n"},
    ],
)
def test_a_malformed_field_is_an_error_without_running_git(no_exec, spec):
    verdict, _ = no_exec._evaluate({"accept": {"kind": "git_commit", **spec}})
    assert verdict == "error"


def test_every_argv_is_the_fixed_template(monkeypatch):
    mod = _load()
    seen: list[list[str]] = []

    class _Proc:
        def __init__(self, stdout):
            self.returncode, self.stdout, self.stderr = 0, stdout, ""

    def _fake_run(argv, **kwargs):
        assert kwargs.get("shell", False) is False
        seen.append(list(argv))
        return _Proc("3" if "rev-list" in argv else "f" * 40)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    base = "b" * 40
    spec = {**_spec("/r", base), "argv": ["rm", "-rf", "/"], "cmd": "touch /tmp/x"}
    assert mod._evaluate({"accept": spec})[0] == "pass"
    assert seen == [
        ["git", "-C", "/r", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        ["git", "-C", "/r", "rev-parse", "--verify", "--quiet", "refs/heads/feat/x"],
        ["git", "-C", "/r", "rev-list", "--count", "refs/heads/feat/x", "--not", base],
    ]


def test_git_is_the_one_binary_this_kind_adds():
    assert _load()._SELF_BUILT_COMMANDS == {"gh", "kirocrew", "git"}


def test_the_ledger_mirror_uses_the_evaluators_own_patterns():
    mod = _load()
    assert records.ACCEPTANCE_GIT_BRANCH.pattern == mod._GIT_BRANCH.pattern
    assert records.ACCEPTANCE_GIT_BASE.pattern == mod._GIT_BASE.pattern


def test_the_ledger_calls_the_bar_concrete_exactly_when_the_evaluator_reads_it(no_exec):
    """Every spec the ledger calls non-concrete is ``error`` in the evaluator; a
    relative repo is concrete, so its refusal reaches the conductor."""
    base = "a" * 40
    for spec in (
        _spec("/r", "TBD"),
        _spec("/r", base, branch="a..b"),
        _spec("/r", base, branch="-x"),
        {"kind": "git_commit", "repo": "/r", "branch": "feat/x"},
        {"kind": "git_commit", "repo": 7, "branch": "feat/x", "base": base},
    ):
        assert wl.is_acceptance_concrete(spec) is False, spec
        assert no_exec._evaluate({"accept": spec})[0] == "error", spec
    # A placeholder that fits the pattern would reach git, so it is held to the
    # placeholder rule here rather than run.
    assert wl.is_acceptance_concrete(_spec("/r", base, branch="TBD")) is False
    assert wl.is_acceptance_concrete(_spec("TBD", base)) is False
    relative = _spec("repo", base)
    assert wl.is_acceptance_concrete(relative) is True
    assert no_exec._evaluate({"accept": relative})[0] == "refused"
    assert wl.is_acceptance_concrete(_spec("/r", base)) is True


def test_the_tool_screens_the_repo_path_before_git_runs(monkeypatch):
    """The ``accept_eval`` tool screens a ``git_commit`` repo as it screens a
    ``file`` path: a refused path never reaches ``git``, and an allowed one runs at
    its canonical form."""
    from kiro_crew import hooks

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_get", lambda path, **kw: {"conductor": {}, "items": []})
    monkeypatch.setattr(
        hooks, "validate_file_path", lambda raw: None if "secret" in raw else "/canon" + raw
    )
    seen: list[list[str]] = []
    mod = mcp_work._skill_script("accept_eval")

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        raise FileNotFoundError

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    base = "a" * 40
    args = {
        "items": [
            {"id": "s", "accept": _spec("/home/u/secret", base)},
            {"id": "ok", "accept": _spec("/r", base)},
            {"id": "rel", "accept": _spec("repo", base)},
        ]
    }
    out = mcp_work._call_tool_inner("accept_eval", mcp_work._validate_args("accept_eval", args))
    verdicts = {r["id"]: r["verdict"] for r in json.loads(out)["results"]}
    assert verdicts == {"s": "refused", "ok": "error", "rel": "refused"}
    assert [argv[2] for argv in seen] == ["/canon/r"]


def test_an_unhashable_kind_is_one_error_verdict_not_a_lost_batch(monkeypatch):
    """The path screen looks a kind up by value, so a list kind must reach the
    evaluator as its own ``error`` rather than raise and lose every sibling."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_get", lambda path, **kw: {"conductor": {}, "items": []})
    args = {
        "items": [
            {"id": "bad", "accept": {"kind": []}},
            {"id": "ok", "accept": {"kind": "human_approval"}},
        ]
    }
    out = mcp_work._call_tool_inner("accept_eval", mcp_work._validate_args("accept_eval", args))
    verdicts = {r["id"]: r["verdict"] for r in json.loads(out)["results"]}
    assert verdicts == {"bad": "error", "ok": "pending"}


@pytest.mark.skipif(sys.platform == "win32", reason="the planted program is a POSIX shell script")
def test_a_partial_clone_cannot_make_a_read_run_the_repos_own_program(repo, tmp_path):
    """The repo is agent-written. A promisor remote with a planted ``uploadpack``
    would run that program when ``rev-parse`` meets a missing base object; the
    evaluator's git environment refuses every transport, so the read fails closed
    instead."""
    path, base = repo
    marker = tmp_path / "planted-ran"
    planted = tmp_path / "planted.sh"
    planted.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    planted.chmod(0o755)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.url", remote.as_uri()),
        ("remote.origin.promisor", "true"),
        ("remote.origin.uploadpack", str(planted)),
    ):
        _git(path, "config", key, value)
    _git(path, "branch", "feat/x")
    verdict, _ = _load()._evaluate({"accept": _spec(path, "1" * 40)})
    assert not marker.exists(), "the repository's own uploadpack ran"
    assert verdict == "error"


def test_every_git_call_runs_in_the_offline_environment(monkeypatch):
    mod = _load()
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    envs: list[dict] = []

    class _Proc:
        returncode, stdout, stderr = 1, "", ""

    def _fake_run(argv, **kwargs):
        envs.append(kwargs.get("env"))
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    mod._evaluate({"accept": _spec("/r", "a" * 40)})
    assert envs, "git never ran"
    for env in envs:
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        assert env["GIT_ALLOW_PROTOCOL"] == ""
        assert "GIT_DIR" not in env

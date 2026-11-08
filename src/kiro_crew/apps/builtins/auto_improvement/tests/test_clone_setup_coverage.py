"""``clone_setup``: host and URL screening, the ``gh`` transport probe, remote slugs, branch
listing and the fetch-then-checkout path, which the clone-setup suites leave unpinned.

Everything that would reach the network, the resolver, ``gh`` or ``git`` is replaced on the
``clone_setup`` module itself, so no test here resolves a name or starts a process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import DISABLED_NO_PUSH, CloneSpec


def _git(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


@pytest.mark.parametrize(
    "host",
    ["", "localhost", "LocalHost", "metadata.google.internal", "169.254.169.254", "fd00:ec2::254"],
)
def test_a_host_blocked_by_name_is_refused_without_resolving_it(monkeypatch, host):
    resolve = mock.Mock(side_effect=AssertionError("resolved a host that is blocked by name"))
    monkeypatch.setattr(clone_setup, "socket", SimpleNamespace(getaddrinfo=resolve))
    assert clone_setup._host_is_blocked(host) is True
    resolve.assert_not_called()


_TOO_LONG = "https://github.com/o/" + "r" * clone_setup._MAX_URL_LEN


@pytest.mark.parametrize(
    ("url", "blocked", "reason"),
    [
        (None, False, "Enter a GitHub repository URL."),
        ("   ", False, "Enter a GitHub repository URL."),
        (_TOO_LONG, False, "URL is too long."),
        ("https://github.com/o/r", True, "URL host is not allowed (blocked address)."),
        ("https://github.com/lonely", False, "URL did not match a github.com/<owner>/<repo> URL."),
    ],
)
def test_validate_target_url_refuses_with_a_reason(monkeypatch, url, blocked, reason):
    monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda host: blocked)
    assert clone_setup.validate_target_url(url) == (None, reason)


@pytest.mark.parametrize(
    ("ssh", "clone_url"),
    [(True, "git@github.com:o/r.git"), (False, "https://github.com/o/r.git")],
)
def test_validate_target_url_clones_over_the_transport_gh_is_set_up_for(
    monkeypatch, ssh, clone_url
):
    monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda host: False)
    monkeypatch.setattr(clone_setup, "_gh_prefers_ssh", lambda: ssh)
    spec, err = clone_setup.validate_target_url("https://www.github.com/o/r.git/tree/main")
    assert (spec, err) == (CloneSpec("o/r", clone_url, "o--r"), "")


def _gh(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, "")


_GH_HOST = ["gh", "config", "get", "git_protocol", "-h", "github.com"]
_GH_GLOBAL = ["gh", "config", "get", "git_protocol"]


@pytest.mark.parametrize(
    ("which", "outcomes", "prefers_ssh", "asked"),
    [
        (None, [], False, []),  # no gh on PATH: nothing is run
        ("/bin/gh", [_gh(0, "SSH\n")], True, [_GH_HOST]),  # the setting for github.com, any case
        ("/bin/gh", [_gh(0, "https\n")], False, [_GH_HOST]),
        ("/bin/gh", [_gh(1, ""), _gh(0, "ssh\n")], True, [_GH_HOST, _GH_GLOBAL]),
        ("/bin/gh", [_gh(0, ""), _gh(1, "ssh")], False, [_GH_HOST, _GH_GLOBAL]),  # says nothing
        ("/bin/gh", [OSError("exec format error")], False, [_GH_HOST]),
        ("/bin/gh", [subprocess.TimeoutExpired("gh", 15)], False, [_GH_HOST]),
    ],
)
def test_gh_prefers_ssh_reads_the_host_setting_then_the_global_one(
    monkeypatch, which, outcomes, prefers_ssh, asked
):
    run = mock.Mock(side_effect=outcomes)
    monkeypatch.setattr(clone_setup, "shutil", SimpleNamespace(which=lambda name: which))
    fake_subprocess = SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)
    monkeypatch.setattr(clone_setup, "subprocess", fake_subprocess)
    assert clone_setup._gh_prefers_ssh() is prefers_ssh
    assert [c.args[0] for c in run.call_args_list] == asked


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("", ""),
        (DISABLED_NO_PUSH, ""),
        ("git@github.com", ""),  # scp-like with no path separator
        ("git@github.com:Owner/Repo.git", "owner/repo"),
        ("https://github.com/Owner/Repo", "owner/repo"),
        ("https://github.com/owner", ""),  # no repo part
        ("/srv/git/repo.git", ""),  # a local path has no identity to pin
        ("file:///srv/git/repo.git", ""),
    ],
)
def test_remote_slug_is_owner_slash_repo_for_a_network_remote_and_empty_otherwise(url, slug):
    assert clone_setup._remote_slug(url) == slug


def _safe_clone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *outcomes: Any) -> tuple:
    """A clone dir that passes the safety and push checks, with ``git`` scripted to ``outcomes``."""
    run = mock.Mock(side_effect=list(outcomes))
    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda repo: True)
    monkeypatch.setattr(clone_setup, "_push_disabled", lambda repo: True)
    monkeypatch.setattr(clone_setup, "subprocess", SimpleNamespace(run=run))
    (tmp_path / ".git").mkdir()
    return tmp_path, run


def test_list_clone_branches_refuses_a_directory_that_is_not_a_clone(tmp_path):
    missing = tmp_path / "nowhere"
    assert clone_setup.list_clone_branches(missing) == ([], f"Not a git clone: {missing}")


def test_list_clone_branches_puts_the_default_first_and_drops_refs_it_must_not_offer(
    monkeypatch, tmp_path
):
    # git prints the bare `origin` for origin/HEAD; `cand/` branches are the spine's throwaways.
    refs = (
        "main\norigin\norigin/HEAD\norigin/main\ncand/x\norigin/cand/y\nmain\nbad name\n-evil\n\n"
    )
    listing, head = _git(0, refs + "feature/z\n"), _git(0, "origin/main\n")
    clone, run = _safe_clone(monkeypatch, tmp_path, listing, head)
    assert clone_setup.list_clone_branches(clone) == (["origin/main", "feature/z", "main"], "")
    where = str(clone.absolute())
    list_argv, head_argv = (c.args[0] for c in run.call_args_list)
    assert list_argv[:5] == ["git", "-C", where, "for-each-ref", "--format=%(refname:short)"]
    assert list_argv[5:] == ["refs/remotes/origin", "refs/heads"]
    assert head_argv[3:] == ["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"]


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        (
            [_git(128, "", "fatal: not a git repository\n")],
            ([], "could not list branches: fatal: not a git repository"),
        ),
        ([_git(0, "origin\norigin/HEAD\ncand/a\n")], ([], "no branches found in the clone")),
        ([_git(0, "main\ndev\n"), _git(1)], (["dev", "main"], "")),  # no origin/HEAD: alphabetical
    ],
)
def test_list_clone_branches_reports_a_failed_or_empty_listing(
    monkeypatch, tmp_path, outcomes, expected
):
    clone, run = _safe_clone(monkeypatch, tmp_path, *outcomes)
    assert clone_setup.list_clone_branches(clone) == expected
    assert run.call_count == len(outcomes)


@pytest.mark.parametrize("branch", ["", "origin/", "-flag", "a..b", "origin/-x"])
def test_checkout_branch_refuses_an_invalid_name_before_it_inspects_the_clone(
    monkeypatch, tmp_path, branch
):
    safe = mock.Mock(side_effect=AssertionError("inspected the clone"))
    monkeypatch.setattr(clone_setup, "_repository_is_safe", safe)
    assert clone_setup.checkout_branch(tmp_path, branch) == (
        False,
        f"invalid branch name: {branch!r}",
    )
    safe.assert_not_called()


@pytest.mark.parametrize(
    ("checkout", "expected"),
    [
        (_git(0), (True, "checked out dev @ origin/dev")),
        (
            _git(1, "", "error: pathspec 'origin/dev' did not match\n"),
            (False, "could not check out dev: error: pathspec 'origin/dev' did not match"),
        ),
    ],
)
def test_checkout_branch_checks_out_what_it_fetched_or_reports_why_not(
    monkeypatch, tmp_path, checkout, expected
):
    _, run = _safe_clone(monkeypatch, tmp_path, _git(0, "main\n"), _git(0), checkout)
    monkeypatch.setattr(clone_setup, "require_pinned", lambda repo: None)
    monkeypatch.setattr(clone_setup, "hook_off_args", lambda where, env: [])
    monkeypatch.setattr(clone_setup, "reap_stale_tmp_packs", mock.Mock())
    assert clone_setup.checkout_branch(tmp_path, "origin/dev") == expected
    skip = 4 + len(clone_setup._GIT_SAFE_CONFIG)  # git -C <dir> --work-tree=<dir> <safe config>
    assert [c.args[0][skip:] for c in run.call_args_list] == [
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["fetch", "--quiet", "origin", "dev"],
        ["checkout", "-B", "dev", "origin/dev"],
    ]

"""Registry sources in private GitHub repositories, read through the host's ``gh``.

An owner-designated ``https://github.com/<owner>/<repo>`` fetch that fails the way
a private repository fails is checked against the host's ``gh`` and, when ``gh``
can read the repository, run once more with ``gh`` as git's credential helper on
the network step alone. ``gh`` and git are faked here; no process starts.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from conftest import host_abs
from kiro_crew import github_runner, sandbox, sandbox_plan
from kiro_crew.apps import registry
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_URL = "https://github.com/acme/private-apps.git"
_GH = host_abs("usr", "bin", "gh")
_GIT = host_abs("usr", "bin", "git")
_GH_HELPER = f"!{shlex.quote(_GH)} auth git-credential"
_PRIVATE_FAILURE = (
    b"remote: Repository not found.\n"
    b"fatal: repository 'https://github.com/acme/private-apps.git/' not found\n"
)
_PINNED = "a" * 40


@pytest.fixture(autouse=True)
def _provider_bins_not_strict(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts outside strict provider-binary mode; strict cases opt in."""
    _floor_monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)


class _Proc:
    def __init__(self, returncode: int, out: bytes = b"") -> None:
        self.returncode = returncode
        self._out = out
        self.pid = UNALLOCATABLE_PID

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._out


def _helper_of(env: dict[str, str]) -> list[str]:
    """The ``credential.helper`` values the command-scope git config carries."""
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return [
        env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(count)
        if env.get(f"GIT_CONFIG_KEY_{i}") == "credential.helper"
    ]


class _FakeGit:
    """Every git spawn the registry makes, answered without a process.

    Without a gh helper in its env, a network step fails like a private repository.
    With one, it succeeds; the local steps always succeed. ``landed`` is the commit
    a fresh checkout reports.
    """

    def __init__(self, *, landed: str = _PINNED) -> None:
        self.calls: list[dict[str, Any]] = []
        self.modes: list[str] = []
        self.visible: list[tuple[str, ...]] = []
        self.landed = landed
        self.network_failure = _PRIVATE_FAILURE
        #: Files a checkout writes into its tree, by relative path.
        self.files: dict[str, str] = {}

    async def wrap(self, argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        self.modes.append(kwargs.get("mode", ""))
        self.visible.append(tuple(kwargs.get("extra_visible_dirs", ())))
        return list(argv), None

    async def spawn(self, *argv: str, **kwargs: Any) -> _Proc:
        env = dict(kwargs.get("env") or {})
        cwd = kwargs.get("cwd")
        self.calls.append(
            {
                "argv": list(argv),
                "env": env,
                "mode": self.modes[-1],
                "visible": self.visible[-1],
            }
        )
        verb = next((a for a in argv[1:] if not a.startswith("-") and "=" not in a), "")
        if verb in {"clone", "fetch", "pull"}:
            if _GH_HELPER not in _helper_of(env):
                return _Proc(128, self.network_failure)
            return _Proc(0)
        if verb == "init":
            dest = Path(argv[-1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)
        if verb == "checkout" and cwd:
            dest = Path(cwd)
            (dest / ".git" / "HEAD").write_text(self.landed + "\n", encoding="utf-8")
            for rel, text in self.files.items():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                (dest / rel).write_text(text, encoding="utf-8")
        return _Proc(0)

    def network_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if {"clone", "fetch", "pull"} & set(c["argv"])]


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> _FakeGit:
    git = _FakeGit()
    monkeypatch.setattr(registry, "wrap_argv_async", git.wrap)
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(registry, "create_subprocess_limited", git.spawn)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "_sel_fn", None)
    monkeypatch.setattr(registry, "_resolve_gh_git", lambda: _GIT)
    monkeypatch.setattr(
        github_runner,
        "gh_env",
        lambda pin_host="": {"GH_CONFIG_DIR": "/cfg/gh", "GH_HOST": pin_host},
    )
    yield git
    # The index fetch records its cause in a module-level map; leave none behind.
    registry._FETCH_FAILURE_CAUSES.pop((_URL, "main"), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["clone", "pull", "index", "manifest"])
async def test_first_network_attempt_pins_the_classifier_locale(
    fake_git: _FakeGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setattr(registry, "_gh_access", AsyncMock(return_value=("", "gh_missing")))
    dest = tmp_path / "private-app"
    if operation == "pull":
        (dest / ".git").mkdir(parents=True)
        monkeypatch.setattr(registry, "_clone_origin_url", AsyncMock(return_value=_URL))
        monkeypatch.setattr(registry, "_read_clone_branch", lambda path: "main")
    if operation in {"clone", "pull"}:
        await registry._git_clone_or_pull(_URL, "main", dest, [])
    elif operation == "index":
        await registry._fetch_external_registry_index(_URL, "main")
    else:
        await registry._fetch_app_manifest(_URL, "main", owner_designated=True)
    first = fake_git.network_calls()[0]
    expected_verb = "pull" if operation == "pull" else "clone"
    assert expected_verb in first["argv"]
    assert first["env"]["LC_ALL"] == registry._GIT_CLONE_LOCALE


# ---------------------------------------------------------------------------
# Repository name validation: the only text that reaches a gh argv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("https://github.com/acme/private-apps.git", "acme/private-apps"),
        ("https://github.com/acme/private-apps", "acme/private-apps"),
        ("https://github.com/acme/private-apps/", "acme/private-apps"),
        ("https://github.com/a-b/x.y_z", "a-b/x.y_z"),
    ],
)
def test_a_plain_github_https_url_names_its_repository(url: str, repo: str) -> None:
    assert registry._github_https_repo(url) == repo


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/apps;rm -rf ~",
        "https://github.com/acme/$(id)",
        "https://github.com/ac`me`/apps",
        "https://github.com/acme/apps|sh",
        "https://github.com/acme/apps\nx",
        "https://github.com/-acme/apps",
        "https://github.com/acme/..",
        "https://github.com/acme/apps/tree/main",
        "https://user:pw@github.com/acme/apps",
        "https://github.com:8443/acme/apps",
        "https://github.com/acme/apps?x=1",
        "https://github.com/acme/apps#frag",
        "http://github.com/acme/apps",
        "git@github.com:acme/apps.git",
        "https://gitlab.com/acme/apps",
        "https://github.com.evil.example/acme/apps",
    ],
)
def test_any_other_url_is_refused_before_gh_runs(url: str) -> None:
    assert registry._github_https_repo(url) == ""


@pytest.mark.asyncio
async def test_gh_access_refuses_a_repository_name_with_shell_characters() -> None:
    with (
        patch.object(github_runner, "resolve_gh") as resolve,
        patch.object(github_runner, "run_gh") as run,
    ):
        _path, cause = await registry._gh_access("acme/apps;id")
    assert cause == "gh_no_access"
    resolve.assert_not_called()
    run.assert_not_called()


# ---------------------------------------------------------------------------
# The gh access check
# ---------------------------------------------------------------------------


def _completed(code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr="")


@pytest.mark.asyncio
async def test_strict_provider_bins_refuses_gh_before_resolution_or_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH) as resolve,
        patch.object(github_runner, "run_gh", return_value=_completed(0)) as run,
    ):
        assert await registry._gh_access("acme/private-apps") == ("", "gh_strict_mode")
    resolve.assert_not_called()
    run.assert_not_called()


@pytest.mark.asyncio
async def test_strict_provider_bins_refuses_a_private_app_without_retrying_git(
    fake_git: _FakeGit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH) as resolve,
        patch.object(github_runner, "run_gh", return_value=_completed(0)) as run,
    ):
        err = await registry._git_clone_or_pull(_URL, "main", tmp_path / "private-app", [])
    assert err is not None and err["ok"] is False
    assert err["code"] == "gh_strict_mode"
    assert "acme/private-apps" in err["error"]
    assert "strict" in err["error"]
    assert len(fake_git.network_calls()) == 1
    resolve.assert_not_called()
    run.assert_not_called()


@pytest.mark.asyncio
async def test_strict_provider_bins_records_a_private_index_refusal(
    fake_git: _FakeGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH) as resolve,
        patch.object(github_runner, "run_gh", return_value=_completed(0)) as run,
    ):
        assert await registry._fetch_external_registry_index(_URL, "main") is None
    assert registry._registry_fetch_failure(_URL, "main") == "gh_strict_mode"
    assert len(fake_git.network_calls()) == 1
    resolve.assert_not_called()
    run.assert_not_called()


@pytest.mark.asyncio
async def test_gh_missing_is_reported_and_nothing_runs() -> None:
    with (
        patch.object(github_runner, "resolve_gh", side_effect=github_runner.SetupError("no gh")),
        patch.object(github_runner, "run_gh") as run,
    ):
        assert await registry._gh_access("acme/private-apps") == ("", "gh_missing")
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_repository_read_and_auth_status_report_signed_out() -> None:
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH),
        patch.object(github_runner, "run_gh", return_value=_completed(1)) as run,
    ):
        assert await registry._gh_access("acme/private-apps") == ("", "gh_signed_out")
    assert [call.args[0] for call in run.call_args_list] == [
        [_GH, "api", "repos/acme/private-apps", "--silent"],
        [_GH, "auth", "status", "--hostname", "github.com"],
    ]
    assert all("--active" not in call.args[0] for call in run.call_args_list)


@pytest.mark.asyncio
async def test_a_repository_gh_cannot_read_is_no_access() -> None:
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH),
        patch.object(github_runner, "run_gh", side_effect=[_completed(1), _completed(0)]) as run,
    ):
        assert await registry._gh_access("acme/private-apps") == ("", "gh_no_access")
    assert [call.args[0] for call in run.call_args_list] == [
        [_GH, "api", "repos/acme/private-apps", "--silent"],
        [_GH, "auth", "status", "--hostname", "github.com"],
    ]
    assert all("--active" not in call.args[0] for call in run.call_args_list)
    assert all(call.kwargs["pin_host"] == "github.com" for call in run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_failed", [False, True])
async def test_a_gh_timeout_fails_closed(api_failed: bool) -> None:
    error = subprocess.TimeoutExpired(cmd="gh", timeout=1)  # flake-ok: a raised value
    results = [_completed(1), error] if api_failed else [error]
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH),
        patch.object(github_runner, "run_gh", side_effect=results),
    ):
        assert await registry._gh_access("acme/private-apps") == ("", "timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("api_failed", [False, True])
@pytest.mark.parametrize("error_type", [github_runner.SetupError, OSError])
async def test_a_gh_probe_that_cannot_start_is_missing(
    api_failed: bool, error_type: type[Exception]
) -> None:
    error = error_type("cannot start gh")
    results = [_completed(1), error] if api_failed else [error]
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH),
        patch.object(github_runner, "run_gh", side_effect=results),
    ):
        assert await registry._gh_access("acme/private-apps") == ("", "gh_missing")


@pytest.mark.asyncio
async def test_a_readable_repository_never_checks_an_expired_inactive_account() -> None:
    with (
        patch.object(github_runner, "resolve_gh", return_value=_GH),
        patch.object(github_runner, "run_gh", side_effect=[_completed(0), _completed(1)]) as run,
    ):
        assert await registry._gh_access("acme/private-apps") == (_GH, "")
    run.assert_called_once_with(
        [_GH, "api", "repos/acme/private-apps", "--silent"],
        timeout=20,  # flake-ok: asserting the probe's argument; no process or wait runs
        audit_caller="core:app-registry",
        pin_host="github.com",
    )
    assert all("--active" not in call.args[0] for call in run.call_args_list)


# ---------------------------------------------------------------------------
# The credential env: gh does the sign-in, no token in Kiro Crew's hands
# ---------------------------------------------------------------------------


def test_the_credential_env_clears_other_helpers_and_names_gh_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_runner, "gh_env", lambda pin_host="": {"GH_CONFIG_DIR": "/c"})
    env = registry._gh_credential_env({}, _GH)
    assert _helper_of(env) == ["", _GH_HELPER]
    # Entries already in the env are kept and the new ones go after them.
    twice = registry._gh_credential_env(env, _GH)
    assert _helper_of(twice) == ["", _GH_HELPER] * 2
    assert env["GH_CONFIG_DIR"] == "/c"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any("token" in key.lower() for key in env if key.startswith("GIT_"))
    # No system or global git config reaches the fetch that can read the gh sign-in,
    # so no URL rewrite there can send it to a remote helper; and git may use only
    # its built-in https transport.
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_ALLOW_PROTOCOL"] == "https"


def test_the_credential_env_refuses_a_relative_gh_path() -> None:
    with pytest.raises(ValueError):
        registry._gh_credential_env({}, "gh")


# ---------------------------------------------------------------------------
# Trusted git for the gh-credentialed network step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["", "untrusted", "unreadable", "discovery"])
def test_gh_git_resolution_uses_the_shared_trust_policy_and_fails_closed(
    failure: str,
) -> None:
    with (
        patch.object(
            github_runner,
            "provider_executable_candidates",
            return_value=(_GIT,),
            side_effect=OSError("discovery failed") if failure == "discovery" else None,
        ) as candidates,
        patch.object(
            github_runner,
            "validate_provider_executable",
            return_value=_GIT,
            side_effect=(
                ValueError("untrusted")
                if failure == "untrusted"
                else OSError("unreadable") if failure == "unreadable" else None
            ),
        ) as validate,
    ):
        assert registry._resolve_gh_git() == ("" if failure else _GIT)
    candidates.assert_called_once_with("git")
    if failure == "discovery":
        validate.assert_not_called()
    else:
        validate.assert_called_once_with(_GIT)


# ---------------------------------------------------------------------------
# Install and update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_private_app_installs_through_gh_on_the_network_step_only(
    fake_git: _FakeGit, tmp_path: Path
) -> None:
    dest = tmp_path / "sources" / "private-app"
    log: list[str] = []
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        err = await registry._git_clone_or_pull(_URL, "main", dest, log)
    assert err is None
    access.assert_awaited_once_with("acme/private-apps")
    first, second = fake_git.network_calls()
    assert not _helper_of(first["env"])
    assert _GH_HELPER in _helper_of(second["env"])
    # Same tier as the first attempt, with only the gh sign-in folder lifted, and
    # only on the network step.
    assert second["mode"] == first["mode"]
    assert second["visible"] == (str(Path.home() / ".config" / "gh"),)
    assert all(not c["visible"] for c in fake_git.calls if c is not second)
    # Every step of the retry, init and checkout too, runs the trusted git, so a
    # git found on PATH cannot write the config the credentialed fetch reads.
    retry = fake_git.calls[fake_git.calls.index(first) + 1 :]
    assert second in retry
    for call in retry:
        assert call["argv"][0] == _GIT, call["argv"]
    # Init and checkout never carry the helper.
    for call in fake_git.calls:
        if call not in (first, second):
            assert not _helper_of(call["env"]), call["argv"]
    assert any("looks private" in line for line in log)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")
@pytest.mark.asyncio
async def test_the_gh_fetch_reads_the_gh_sign_in_and_no_other_credential_folder(
    fake_git: _FakeGit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sandbox the gh-credentialed fetch asked for, replayed into the real Linux
    # plan and the real macOS profile: only `~/.config/gh` comes back into view.
    monkeypatch.setattr(registry, "_context_clone_sandbox_mode", lambda url: "strict")
    dest = tmp_path / "sources" / "private-app"
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))):
        assert await registry._git_clone_or_pull(_URL, "main", dest, []) is None
    _first, second = fake_git.network_calls()
    home = str(Path.home())
    gh_dir = str(Path(home) / ".config" / "gh")
    plan = sandbox_plan.namespace_payload(
        sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, second["mode"], extra_visible_dirs=second["visible"]
        )
    )
    profile = sandbox._build_seatbelt_profile(second["mode"], extra_visible_dirs=second["visible"])
    denied = [line for line in profile.splitlines() if "deny file-read" in line]
    assert gh_dir not in plan["sensitive_dirs"]
    assert not any(gh_dir in line for line in denied)
    for leaf in (".aws", ".kube"):
        path = str(Path(home) / leaf)
        assert path in plan["sensitive_dirs"], leaf
        assert any(f'"{path}"' in line for line in denied), leaf
    assert plan["hide_ssh"] == 1
    assert any(f'"{home}/.ssh"' in line for line in denied)


@pytest.mark.asyncio
async def test_a_private_app_without_a_trusted_git_never_runs_the_gh_fetch(
    fake_git: _FakeGit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "_resolve_gh_git", lambda: "")
    dest = tmp_path / "sources" / "private-app"
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))):
        err = await registry._git_clone_or_pull(_URL, "main", dest, [])
    assert len(fake_git.network_calls()) == 1
    assert err is not None and err["ok"] is False
    # The retry stops at its first step: without a trusted git nothing runs.
    assert err["error"] == "git init failed"
    assert not dest.exists()


@pytest.mark.asyncio
async def test_an_existing_private_app_updates_through_an_isolated_gh_fetch(
    fake_git: _FakeGit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "sources" / "private-app"
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (dest / "local-changes.txt").write_text("preserved", encoding="utf-8")
    monkeypatch.setattr(registry, "_proxy_env_has_credentials", lambda url: False)
    monkeypatch.setattr(registry, "_context_clone_sandbox_mode", lambda url: "strict")

    async def spawn(*argv: str, **kwargs: Any) -> _Proc:
        proc = await fake_git.spawn(*argv, **kwargs)
        if list(argv) == ["git", "remote", "get-url", "origin"]:
            return _Proc(0, (_URL + "\n").encode())
        return proc

    monkeypatch.setattr(registry, "create_subprocess_limited", spawn)
    pending_cleanup: list[Path] = []
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        err = await registry._git_clone_or_pull(
            _URL, "main", dest, [], pending_cleanup=pending_cleanup
        )
    assert err is None
    access.assert_awaited_once_with("acme/private-apps")
    first, second = fake_git.network_calls()
    assert "pull" in first["argv"] and "--ff-only" in first["argv"]
    assert first["mode"] == "strict"
    assert not _helper_of(first["env"])
    assert "fetch" in second["argv"] and "pull" not in second["argv"]
    assert second["mode"] == "strict"
    assert second["visible"] == (str(Path.home() / ".config" / "gh"),)
    assert _helper_of(second["env"]) == ["", _GH_HELPER]
    for call in fake_git.calls:
        if call not in (first, second):
            assert not _helper_of(call["env"]), call["argv"]
    assert pending_cleanup
    stale = pending_cleanup[0]
    assert ".stale-" in stale.name and stale.parent == dest.parent
    assert (stale / "local-changes.txt").read_text(encoding="utf-8") == "preserved"
    assert (dest / ".git").is_dir()
    assert not (dest / "local-changes.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["gh_missing", "gh_signed_out", "gh_no_access"])
async def test_a_private_app_gh_cannot_read_fails_with_its_cause(
    fake_git: _FakeGit, tmp_path: Path, cause: str
) -> None:
    dest = tmp_path / "sources" / "private-app"
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=("", cause))):
        err = await registry._git_clone_or_pull(_URL, "main", dest, [])
    assert err is not None and err["ok"] is False
    assert err["code"] == cause
    assert "acme/private-apps" in err["error"]
    assert "computer that runs Kiro Crew" in err["error"]
    assert "token" not in err["error"].lower()
    # No second git fetch ran.
    assert len(fake_git.network_calls()) == 1


@pytest.mark.asyncio
async def test_a_pinned_private_app_whose_commit_differs_is_refused(
    fake_git: _FakeGit, tmp_path: Path
) -> None:
    fake_git.landed = "b" * 40
    dest = tmp_path / "sources" / "private-app"
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))):
        err = await registry._git_clone_or_pull(_URL, "main", dest, [], commit=_PINNED)
    assert err is not None
    assert err["error"] == "pinned commit verification failed"


@pytest.mark.asyncio
async def test_a_pinned_private_app_whose_commit_matches_installs(
    fake_git: _FakeGit, tmp_path: Path
) -> None:
    dest = tmp_path / "sources" / "private-app"
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))):
        err = await registry._git_clone_or_pull(_URL, "main", dest, [], commit=_PINNED)
    assert err is None


@pytest.mark.asyncio
async def test_an_index_originated_install_never_asks_gh(
    fake_git: _FakeGit, tmp_path: Path
) -> None:
    dest = tmp_path / "sources" / "sibling"
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        err = await registry._git_clone_or_pull(_URL, "main", dest, [], index_originated=True)
    assert err is not None
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_branch_that_does_not_exist_is_not_retried_through_gh(
    fake_git: _FakeGit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def spawn(*argv: str, **kwargs: Any) -> _Proc:
        fake_git.calls.append(
            {"argv": list(argv), "env": kwargs.get("env") or {}, "mode": "", "visible": ()}
        )
        return _Proc(128, b"warning: Could not find remote branch nope to clone.\n")

    monkeypatch.setattr(registry, "create_subprocess_limited", spawn)
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        err = await registry._git_clone_or_pull(_URL, "nope", tmp_path / "x", [])
    assert err is not None
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_gh_sign_in_never_combines_with_an_index_originated_fetch(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await registry._git_clone_or_pull(
            _URL, "main", tmp_path / "x", [], index_originated=True, gh_path=_GH
        )


# ---------------------------------------------------------------------------
# The registry index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_private_index_is_read_through_gh(fake_git: _FakeGit) -> None:
    fake_git.files["app-registry.json"] = json.dumps([{"name": "private-app", "repo": _URL}])
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))):
        entries = await registry._fetch_external_registry_index(_URL, "main")
    assert entries == [{"name": "private-app", "repo": _URL}]
    assert registry._registry_fetch_failure(_URL, "main") == ""
    network = fake_git.network_calls()
    assert _GH_HELPER in _helper_of(network[-1]["env"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["gh_missing", "gh_signed_out", "gh_no_access"])
async def test_a_private_index_gh_cannot_read_records_its_cause(
    fake_git: _FakeGit, cause: str
) -> None:
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=("", cause))):
        assert await registry._fetch_external_registry_index(_URL, "main") is None
    assert registry._registry_fetch_failure(_URL, "main") == cause
    assert len(fake_git.network_calls()) == 1


@pytest.mark.parametrize("cause", ["gh_signed_out", "gh_strict_mode"])
def test_a_gh_cause_status_names_the_repository(
    monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    reg = type("Reg", (), {"repo": _URL, "branch": "main", "name": "acme"})()
    monkeypatch.setattr(registry, "_external_registry_cache_identity", lambda r: "acme")
    monkeypatch.setattr(registry, "_read_external_registry_failure_cause", lambda n: cause)
    monkeypatch.setattr(registry, "_read_external_registry_cache", lambda *a, **k: None)
    status = registry._external_registry_fetch_status(reg)
    assert status == {
        "ok": False,
        "cause": cause,
        "cached": False,
        "host": "github.com",
        "repo": "acme/private-apps",
    }


# ---------------------------------------------------------------------------
# The store card's app.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_same_repo_private_manifest_is_read_through_gh(fake_git: _FakeGit) -> None:
    fake_git.files["apps/private-app/app.json"] = json.dumps({"name": "private-app"})
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        manifest = await registry._fetch_app_manifest(
            _URL, "main", "apps/private-app", git_url=_URL, owner_designated=True
        )
    assert manifest == {"name": "private-app"}
    access.assert_awaited_once_with("acme/private-apps")


@pytest.mark.asyncio
async def test_a_manifest_the_owner_did_not_designate_never_asks_gh(fake_git: _FakeGit) -> None:
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        manifest = await registry._fetch_app_manifest(
            _URL, "main", "apps/private-app", git_url=_URL, owner_designated=False
        )
    assert manifest is None
    access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["index", "manifest"])
@pytest.mark.parametrize("failure", [b"fatal: Authentication failed\n", _PRIVATE_FAILURE])
async def test_a_private_source_behind_a_credentialed_proxy_retries_through_gh(
    fake_git: _FakeGit, monkeypatch: pytest.MonkeyPatch, operation: str, failure: bytes
) -> None:
    monkeypatch.setattr(registry, "_proxy_env_has_credentials", lambda url: True)
    fake_git.network_failure = failure
    fake_git.files["app-registry.json"] = json.dumps([{"name": "private-app", "repo": _URL}])
    fake_git.files["app.json"] = json.dumps({"name": "private-app"})
    with patch.object(registry, "_gh_access", new=AsyncMock(return_value=(_GH, ""))) as access:
        if operation == "index":
            result = await registry._fetch_external_registry_index(_URL, "main")
            assert result == [{"name": "private-app", "repo": _URL}]
            assert registry._registry_fetch_failure(_URL, "main") == ""
        else:
            result = await registry._fetch_app_manifest(_URL, "main", owner_designated=True)
            assert result == {"name": "private-app"}
    access.assert_awaited_once_with("acme/private-apps")
    first, second = fake_git.network_calls()
    assert "fetch" in first["argv"] and "fetch" in second["argv"]
    assert not _helper_of(first["env"])
    assert _GH_HELPER in _helper_of(second["env"])
    for call in fake_git.calls:
        if call not in (first, second):
            assert not _helper_of(call["env"]), call["argv"]


def test_the_redacted_authentication_label_still_looks_private() -> None:
    label = registry._loggable_git_transport_output(
        "fatal: Authentication failed", credentialed=True
    )
    assert label == "git authentication failed (credentialed transport details redacted)"
    assert registry._private_github_shaped(label)


def test_the_redacted_repository_not_found_label_still_looks_private() -> None:
    label = registry._loggable_git_transport_output(
        _PRIVATE_FAILURE.decode("utf-8"), credentialed=True
    )
    assert label == "git transport failed: the repository was not found (details redacted)"
    assert registry._private_github_shaped(label)
    assert registry._private_github_shaped("the repository was not found")
    assert registry._classify_registry_fetch_failure(label) == "other"


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: unable to access 'https://github.com/acme/not-found.git/': HTTP 500",
        "fatal: pathspec 'src/missing' not found",
    ],
)
def test_unrelated_not_found_output_does_not_look_like_a_private_repository(stderr: str) -> None:
    assert registry._redacted_git_failure_class(stderr) == ""
    label = registry._loggable_git_transport_output(stderr, credentialed=True)
    assert not registry._private_github_shaped(label)

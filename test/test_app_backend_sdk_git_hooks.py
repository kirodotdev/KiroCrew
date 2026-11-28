"""The backend SDK's git hook overrides (``kiro_crew.apps.backend_sdk.git_hooks``).

What is pinned is what an app cannot check for itself: the listing git is always
prepared by the gateway's own sandbox chokepoint, read at call time, at a tier the
SDK accepts; no parameter lets the caller pick another spawner, runner or git; the
refusal is the gateway's own class; and the names are published at seam level 17.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew import git_config_hooks
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_GIT_HOOK_NAMES = ("GitHookScanError", "git_hook_disable_args", "git_hook_disable_args_async")


@pytest.fixture(autouse=True)
def _no_host_git_config(tmp_path, _floor_monkeypatch):
    # Only the hooks a test plants exist: no global or system config of the host. Set
    # through the isolation floor's own MonkeyPatch, so a test's monkeypatch.undo()
    # does not lift these pins.
    _floor_monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _floor_monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _floor_monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def _repo_with_a_config_hook(tmp_path: Path, name: str = "repo", hook: str = "planted") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", f"hook.{hook}.command", "true")
    _git(repo, "config", f"hook.{hook}.event", "pre-commit")
    return repo


class _Chokepoint:
    """Stands in for ``sandbox.sandboxed_spawn_argv``: records, then runs git unwrapped."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    def __call__(self, argv, mode="standard", *, env=None, **_kw):
        self.calls.append((list(argv), mode, env))
        return list(argv), dict(env) if env is not None else dict(os.environ), None


def test_the_git_hook_names_are_listed_at_seam_level_seventeen() -> None:
    for name in _GIT_HOOK_NAMES:
        assert name in sdk.__all__, name
        assert sdk._LAZY_EXPORTS[name] == "git_hooks", name
        assert sdk._EXPORT_LEVELS[name] == 17, name
    assert sdk.SDK_LEVEL >= 17


def test_the_refusal_is_the_gateway_class() -> None:
    assert sdk.GitHookScanError is git_config_hooks.ConfigHookScanError


@pytest.mark.parametrize("fn", ["git_hook_disable_args", "git_hook_disable_args_async"])
@pytest.mark.parametrize("knob", ["runner", "spawn_argv", "git", "extra_visible_dirs"])
def test_no_parameter_picks_what_runs_the_listing(fn, knob) -> None:
    assert knob not in inspect.signature(getattr(sdk, fn)).parameters


def test_every_listing_git_goes_through_the_sandbox_chokepoint(tmp_path, monkeypatch) -> None:
    repo = _repo_with_a_config_hook(tmp_path)
    chokepoint = _Chokepoint()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", chokepoint)
    env = {**os.environ, "PROBE": "1"}
    args = sdk.git_hook_disable_args(repo, mode="strict", env=env)
    assert "hook.planted.enabled=false" in args
    assert "submodule.recurse=false" in args
    assert chokepoint.calls, "the listing ran no git through the chokepoint"
    for argv, mode, seen_env in chokepoint.calls:
        assert argv[0] == "git"
        assert mode == "strict"
        assert seen_env is not None and seen_env.get("PROBE") == "1"


def test_the_async_form_goes_through_the_same_chokepoint(tmp_path, monkeypatch) -> None:
    repo = _repo_with_a_config_hook(tmp_path)
    chokepoint = _Chokepoint()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", chokepoint)
    args = asyncio.run(
        asyncio.wait_for(sdk.git_hook_disable_args_async(repo), timeout=FALLBACK_TIMEOUT_SECS)
    )
    assert "hook.planted.enabled=false" in args
    assert chokepoint.calls and all(mode == "standard" for _, mode, _ in chokepoint.calls)


def test_a_relative_cwd_scans_the_repository_it_names(tmp_path, monkeypatch) -> None:
    # `website` from its parent: the listing git runs in it and is pointed at it with
    # `-C`, and a path resolved by both lands in website/website. That nested repository
    # carries a different hook, so a scan of the wrong repository is seen, not only a
    # failed one.
    outer = _repo_with_a_config_hook(tmp_path, "website", "right")
    _repo_with_a_config_hook(outer, "website", "wrong")
    monkeypatch.chdir(tmp_path)
    chokepoint = _Chokepoint()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", chokepoint)
    for args in (
        sdk.git_hook_disable_args("website"),
        asyncio.run(
            asyncio.wait_for(
                sdk.git_hook_disable_args_async(Path("website")), timeout=FALLBACK_TIMEOUT_SECS
            )
        ),
    ):
        assert "hook.right.enabled=false" in args
        assert "hook.wrong.enabled=false" not in args


@pytest.mark.parametrize("mode", ["off", "auto", "cc", ""])
def test_another_tier_is_refused_before_any_git_runs(tmp_path, monkeypatch, mode) -> None:
    chokepoint = _Chokepoint()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", chokepoint)
    with pytest.raises(ValueError):
        sdk.git_hook_disable_args(tmp_path, mode=mode)
    with pytest.raises(ValueError):
        asyncio.run(
            asyncio.wait_for(
                sdk.git_hook_disable_args_async(tmp_path, mode=mode),
                timeout=FALLBACK_TIMEOUT_SECS,
            )
        )
    assert chokepoint.calls == []


def test_a_host_that_cannot_sandbox_the_listing_refuses(tmp_path, monkeypatch) -> None:
    repo = _repo_with_a_config_hook(tmp_path)

    def unavailable(*_a, **_k):
        raise sdk.SandboxUnavailableError(
            "no sandbox backend", kind="no_backend", detail="userns denied"
        )

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", unavailable)
    with pytest.raises(sdk.SandboxUnavailableError):
        sdk.git_hook_disable_args(repo)


def test_an_unreadable_config_raises_the_scan_error(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".git" / "config").write_text("[broken\n", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", _Chokepoint())
    with pytest.raises(sdk.GitHookScanError):
        sdk.git_hook_disable_args(repo)

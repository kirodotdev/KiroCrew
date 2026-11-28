"""The update fetch signs in with the helper git picks for the checkout.

The fetch runs from a throwaway git dir outside the checkout, so an ``includeIf``
in the operator's own config that selects a credential helper for the checkout
never matches there. Under an env that reads the operator's config, the
``credential.*`` entries are resolved against the checkout and carried into the
fetch; under the isolated default, nothing is.

POSIX only: the helper is a shell function.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.platform import update_fetch
from kiro_crew.platform.update_governance import git_command_env
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="the helper is a POSIX shell function"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
]

_HELPER = "!f() { echo username=u; echo password=from-work; }; f"


def _g(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env.update(git_maintenance_off_entries(env))
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout under ``work/`` whose helper only an ``includeIf "gitdir:"`` loads."""
    from kiro_crew import agent_scratch

    repo = tmp_path / "work" / "kiro-crew"
    repo.mkdir(parents=True)
    _g(repo, "init", "-q")
    _g(repo, "remote", "add", "origin", "https://example.invalid/kiro-crew.git")
    work = tmp_path / "gitconfig-work"
    work.write_text(f'[credential]\n\thelper = "{_HELPER}"\n')
    user = tmp_path / "gitconfig"
    user.write_text(f'[includeIf "gitdir:{tmp_path / "work"}/"]\n\tpath = {work}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(agent_scratch, "scratch_root", lambda: tmp_path / "scratch-root")
    return repo


def _fill(repo: Path, env: dict[str, str]) -> str:
    """The password ``git credential fill`` returns under *env*, or ``""``."""
    env = {**env, "GIT_TERMINAL_PROMPT": "0"}
    done = subprocess.run(
        ["git", "credential", "fill"],
        cwd=repo,
        env=env,
        input=b"protocol=https\nhost=example.invalid\n\n",
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    for line in done.stdout.decode().splitlines():
        if line.startswith("password="):
            return line.partition("=")[2]
    return ""


def test_an_include_if_scoped_helper_reaches_the_scratch_fetch(checkout: Path) -> None:
    env = git_command_env(human_transport=True)
    prepared = update_fetch._prepare_isolated_fetch(str(checkout), "origin", "main", env=env)
    assert prepared is not None
    _url, objects_dir, scratch, auth_pairs = prepared
    try:
        assert auth_pairs == [("credential.helper", ""), ("credential.helper", _HELPER)]
        # Without the forwarded entries the scratch dir misses the include.
        bare = update_fetch._scratch_fetch_env(env, scratch, objects_dir, [])
        assert _fill(checkout, bare) == ""
        fetch_env = update_fetch._scratch_fetch_env(env, scratch, objects_dir, auth_pairs)
        assert _fill(checkout, fetch_env) == "from-work"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_isolated_default_env_forwards_no_credential_config(checkout: Path) -> None:
    assert update_fetch._scratch_credential_pairs(str(checkout), git_command_env()) == []


def test_the_checkouts_own_credential_config_is_not_forwarded(checkout: Path) -> None:
    _g(checkout, "config", "credential.helper", "repo-planted")
    _g(checkout, "config", "credential.username", "repo-user")
    pairs = update_fetch._scratch_credential_pairs(
        str(checkout), git_command_env(human_transport=True)
    )
    assert pairs == [("credential.helper", ""), ("credential.helper", _HELPER)]


def test_a_multi_line_helper_is_forwarded_whole(checkout: Path, tmp_path: Path) -> None:
    helper = "!f() {\n echo username=u; echo password=multi-line; }; f"
    (tmp_path / "gitconfig").write_text(
        '[credential]\n\thelper = "!f() {\\n echo username=u; echo password=multi-line; }; f"\n'
        "\tusername = u\n"
    )
    env = git_command_env(human_transport=True)
    prepared = update_fetch._prepare_isolated_fetch(str(checkout), "origin", "main", env=env)
    assert prepared is not None
    _url, objects_dir, scratch, auth_pairs = prepared
    try:
        assert auth_pairs == [
            ("credential.helper", ""),
            ("credential.helper", helper),
            ("credential.username", "u"),
        ]
        fetch_env = update_fetch._scratch_fetch_env(env, scratch, objects_dir, auth_pairs)
        assert _fill(checkout, fetch_env) == "multi-line"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

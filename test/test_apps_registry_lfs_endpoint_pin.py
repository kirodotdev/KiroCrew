"""A registry checkout's Git LFS endpoint comes from the vetted clone URL.

A repository's committed ``.lfsconfig`` may name ``lfs.url`` (or
``remote.<name>.lfsurl``), and Git LFS reads it during the checkout's smudge step.
Without a pin, a repository on a trusted forge could point that step at a loopback
or internal address the clone-host gate never saw. The module-wide autouse
fixtures come from ``apps_registry_helpers.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew import platform_compat
from kiro_crew.apps import registry
from kiro_crew.apps.registry_pipeline.git_targets import _lfs_endpoint_pin_argv
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_PUBLIC_URL = "https://github.com/acme/demo.git"
_ATTACKER_LFS_URL = "http://127.0.0.1:9/attacker-lfs"


@pytest.mark.parametrize(
    ("git_url", "pinned"),
    [
        ("https://github.com/acme/demo.git", "https://github.com/acme/demo.git/info/lfs"),
        ("https://github.com/acme/demo", "https://github.com/acme/demo.git/info/lfs"),
        ("https://github.com/acme/demo/", "https://github.com/acme/demo.git/info/lfs"),
        ("http://git.internal:8080/a/b.git", "http://git.internal:8080/a/b.git/info/lfs"),
        ("git://github.com/acme/demo.git", "https://github.com/acme/demo.git/info/lfs"),
        ("git@github.com:acme/demo.git", "git@github.com:acme/demo.git"),
        ("ssh://git@github.com/acme/demo.git", "ssh://git@github.com/acme/demo.git"),
    ],
)
def test_the_pin_is_the_endpoint_git_lfs_derives_from_the_clone_url(git_url, pinned):
    assert _lfs_endpoint_pin_argv(git_url) == ("-c", f"lfs.url={pinned}")


@pytest.mark.parametrize("git_url", ["/srv/repo.git", "file:///srv/repo.git", ""])
def test_a_hostless_target_gets_no_pin(git_url):
    # The clone-host gate refuses a hostless target before any network clone.
    assert _lfs_endpoint_pin_argv(git_url) == ()


def _git(*args, cwd, env):
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("git is unavailable")
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


async def _fetch_repo_with_hostile_lfsconfig(tmp_path, monkeypatch, filter_body: str) -> str:
    """Fetch a repo whose ``.lfsconfig`` names an attacker URL; return what the smudge saw."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    marker = tmp_path / "smudge-saw"
    script = tmp_path / "smudge.py"
    home.mkdir()
    work.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Registry Test",
        "GIT_AUTHOR_EMAIL": "registry@example.invalid",
        "GIT_COMMITTER_NAME": "Registry Test",
        "GIT_COMMITTER_EMAIL": "registry@example.invalid",
    }
    env.update(git_maintenance_off_entries(env))
    _git("init", "--quiet", "--template=", "-b", "main", cwd=work, env=env)
    (work / ".lfsconfig").write_text(
        f'[lfs]\n\turl = {_ATTACKER_LFS_URL}\n[remote "origin"]\n\tlfsurl = {_ATTACKER_LFS_URL}\n',
        encoding="utf-8",
    )
    (work / ".gitattributes").write_text("payload.bin filter=lfs\n", encoding="utf-8")
    (work / "payload.bin").write_text("payload\n", encoding="utf-8")
    _git("add", ".", cwd=work, env=env)
    _git("commit", "--quiet", "--no-verify", "-m", "base", cwd=work, env=env)
    _git("clone", "--quiet", "--bare", str(work), str(remote), cwd=tmp_path, env=env)

    script.write_text(
        "import pathlib, subprocess, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        f"{filter_body}\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    # The registry URL is a trusted forge; a command-scope rewrite serves it from the
    # local bare repository so nothing leaves the machine.
    clone_env = {
        **env,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"url.{remote.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": _PUBLIC_URL,
        "GIT_CONFIG_KEY_1": "filter.lfs.smudge",
        "GIT_CONFIG_VALUE_1": f'"{sys.executable}" "{script}"',
    }
    clone_env.update(git_maintenance_off_entries(clone_env))
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    result = await registry._git_fetch_branch(
        _PUBLIC_URL,
        "main",
        tmp_path / "checkout",
        [],
        clone_env=clone_env,
        sandbox_mode="strict",
    )
    assert result is None
    return marker.read_text(encoding="utf-8")


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="real Git smudge filter run")
@pytest.mark.asyncio
async def test_the_checkout_smudge_step_sees_the_pinned_lfs_url(tmp_path, monkeypatch):
    seen = await _fetch_repo_with_hostile_lfsconfig(
        tmp_path,
        monkeypatch,
        "marker.write_text(subprocess.run(['git', 'config', '--get', 'lfs.url'],"
        " capture_output=True, text=True).stdout.strip())",
    )
    assert seen == f"{_PUBLIC_URL}/info/lfs"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="real Git smudge filter run")
@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not installed")
@pytest.mark.asyncio
async def test_git_lfs_in_the_smudge_step_ignores_the_repository_lfs_url(tmp_path, monkeypatch):
    # The operator's HOME only for this one call, so a version-manager shim in front
    # of git-lfs still resolves; GIT_CONFIG_GLOBAL stays os.devnull.
    real_home = os.path.expanduser("~")
    seen = await _fetch_repo_with_hostile_lfsconfig(
        tmp_path,
        monkeypatch,
        "import os\n"
        "out = subprocess.run(['git', 'lfs', 'env'], capture_output=True, text=True,"
        f" env={{**os.environ, 'HOME': {real_home!r}}}).stdout\n"
        "marker.write_text(next(l for l in out.splitlines() if l.startswith('Endpoint=')))",
    )
    assert "127.0.0.1" not in seen
    assert seen.startswith("Endpoint=")

"""``git_command_env`` keeps the user's global and system git files out of host git.

A sandboxed agent runs as the gateway's user, so it can write ``~/.gitconfig``,
``$XDG_CONFIG_HOME/git/config``, ``~/.config/git/attributes`` and whatever an
inherited ``GIT_CONFIG_GLOBAL`` names. Host-side git reads those for every
repository. Each test plants a program-running setting in one of them, proves
the plant FIRES under a plain environment (so the test is not vacuous), and
then proves it does not fire under :func:`git_command_env`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.platform import update_governance
from kiro_crew.platform.update_governance import git_command_env

pytestmark = pytest.mark.skipif(os.name != "posix", reason="planted programs are sh scripts")

_ID = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _marker_script(tmp_path: Path, name: str, tail: str = "cat") -> tuple[Path, Path]:
    """A script that records it ran, and the marker it writes.

    *tail* runs after the record: ``cat`` passes a filter's content through, and
    ``exit 1`` ends a transport at once instead of waiting on git's stdin.
    """
    marker = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\n{tail}\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, marker


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``$HOME`` the planted files live in, with git reading it as global config."""
    fake = tmp_path / "home"
    (fake / ".config" / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake / ".config"))
    # The suite points GIT_CONFIG_GLOBAL at its own file; drop it so git reads $HOME.
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    # An exported ssh command outranks core.sshCommand and would mask the plant.
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    return fake


@pytest.fixture(autouse=True)
def _fresh_safe_directory_cache():
    """Each test edits ``~/.gitconfig`` mid-test; a reused read would hide that."""
    cache = getattr(update_governance, "_safe_directory_cache", {})
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A one-commit checkout holding ``a.txt``, built without any user config."""
    root = tmp_path / "repo"
    root.mkdir()
    env = {
        **os.environ,
        **_ID,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TEMPLATE_DIR": "",
    }
    # Last, so the entries land after any GIT_CONFIG_COUNT already in the env.
    env.update(git_maintenance_off_entries(env))
    for args in (
        ["init", "-q"],
        ["config", "core.hooksPath", os.devnull],
    ):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    return root


def _plain_env() -> dict[str, str]:
    """The unprotected baseline: the process env as a careless caller passes it."""
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("GIT_CONFIG")]:
        if key != "GIT_CONFIG_GLOBAL":
            env.pop(key)
    return env


def _run(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, timeout=60)


def _reset_hard(repo: Path, env: dict[str, str]) -> None:
    """The update path's destructive step, forced to re-check-out ``a.txt``."""
    (repo / "a.txt").unlink()
    _run(repo, env, "reset", "-q", "--hard", "HEAD")


class TestPlantedGlobalConfigDoesNotRun:
    def test_global_fsmonitor(self, home: Path, repo: Path, tmp_path: Path):
        script, marker = _marker_script(tmp_path, "fsmonitor")
        (home / ".gitconfig").write_text(f"[core]\n\tfsmonitor = {script}\n", encoding="utf-8")

        _run(repo, _plain_env(), "status", "--porcelain")
        assert marker.exists(), "control: a global core.fsmonitor runs under a plain env"
        marker.unlink()

        _run(repo, git_command_env(), "status", "--porcelain")
        assert not marker.exists()

    def test_global_ssh_command(self, home: Path, repo: Path, tmp_path: Path):
        script, marker = _marker_script(tmp_path, "ssh", tail="exit 1")
        (home / ".gitconfig").write_text(f"[core]\n\tsshCommand = {script}\n", encoding="utf-8")
        # Port 9 on loopback refuses at once, so the trusted ssh fails fast.
        url = "ssh://git@127.0.0.1:9/x.git"

        _run(repo, _plain_env(), "ls-remote", url)
        assert marker.exists(), "control: a global core.sshCommand runs under a plain env"
        marker.unlink()

        _run(repo, git_command_env(), "ls-remote", url)
        assert not marker.exists()

    def test_global_filter_driver_with_global_attributes(
        self, home: Path, repo: Path, tmp_path: Path
    ):
        """The repo-NAMED pair no fixed-key pin can reach: the gap this closes."""
        script, marker = _marker_script(tmp_path, "smudge")
        (home / ".gitconfig").write_text(
            f'[filter "evil"]\n\tsmudge = {script}\n', encoding="utf-8"
        )
        (home / ".config" / "git" / "attributes").write_text("* filter=evil\n", encoding="utf-8")

        _reset_hard(repo, _plain_env())
        assert marker.exists(), "control: a global smudge filter runs on reset --hard"
        marker.unlink()

        _reset_hard(repo, git_command_env())
        assert not marker.exists()
        assert (repo / "a.txt").read_text(encoding="utf-8") == "hello\n"

    def test_xdg_config_and_attributes(self, home: Path, repo: Path, tmp_path: Path):
        """``$XDG_CONFIG_HOME/git/config`` is a second global file with no ``~/.gitconfig``."""
        script, marker = _marker_script(tmp_path, "xdg")
        (home / ".config" / "git" / "config").write_text(
            f'[filter "evil"]\n\tsmudge = {script}\n', encoding="utf-8"
        )
        (home / ".config" / "git" / "attributes").write_text("* filter=evil\n", encoding="utf-8")

        _reset_hard(repo, _plain_env())
        assert marker.exists(), "control: the XDG config file is read as global config"
        marker.unlink()

        _reset_hard(repo, git_command_env())
        assert not marker.exists()

    def test_include_path_chain(self, home: Path, repo: Path, tmp_path: Path):
        """A clean-looking ``~/.gitconfig`` whose include points at a planted file."""
        script, marker = _marker_script(tmp_path, "include")
        planted = tmp_path / "dotfiles-local.gitconfig"
        planted.write_text(
            f'[filter "evil"]\n\tsmudge = {script}\n[core]\n\tattributesFile = {tmp_path}/attrs\n',
            encoding="utf-8",
        )
        (tmp_path / "attrs").write_text("* filter=evil\n", encoding="utf-8")
        (home / ".gitconfig").write_text(f"[include]\n\tpath = {planted}\n", encoding="utf-8")

        _reset_hard(repo, _plain_env())
        assert marker.exists(), "control: an included file is read as global config"
        marker.unlink()

        _reset_hard(repo, git_command_env())
        assert not marker.exists()

    def test_symlinked_gitconfig(self, home: Path, repo: Path, tmp_path: Path):
        """A dotfile manager's ``~/.gitconfig -> ~/dotfiles/gitconfig`` is still global."""
        script, marker = _marker_script(tmp_path, "symlink")
        target = tmp_path / "dotfiles" / "gitconfig"
        target.parent.mkdir()
        target.write_text(f"[core]\n\tfsmonitor = {script}\n", encoding="utf-8")
        (home / ".gitconfig").symlink_to(target)

        _run(repo, _plain_env(), "status", "--porcelain")
        assert marker.exists(), "control: a symlinked ~/.gitconfig is read"
        marker.unlink()

        _run(repo, git_command_env(), "status", "--porcelain")
        assert not marker.exists()

    def test_inherited_git_config_global(
        self, home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An exported ``GIT_CONFIG_GLOBAL`` naming an agent-writable file is replaced."""
        script, marker = _marker_script(tmp_path, "envglobal")
        planted = tmp_path / "planted.gitconfig"
        planted.write_text(f'[filter "evil"]\n\tsmudge = {script}\n', encoding="utf-8")
        (home / ".config" / "git" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(planted))

        _reset_hard(repo, _plain_env())
        assert marker.exists(), "control: git reads the file GIT_CONFIG_GLOBAL names"
        marker.unlink()

        _reset_hard(repo, git_command_env())
        assert not marker.exists()

    def test_inherited_system_config(
        self, home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A user-writable system file (a Homebrew prefix), simulated by ``GIT_CONFIG_SYSTEM``."""
        script, marker = _marker_script(tmp_path, "system")
        planted = tmp_path / "etc-gitconfig"
        planted.write_text(f'[filter "evil"]\n\tsmudge = {script}\n', encoding="utf-8")
        (home / ".config" / "git" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(planted))
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

        plain = _plain_env()
        plain["GIT_CONFIG_SYSTEM"] = str(planted)
        _reset_hard(repo, plain)
        assert marker.exists(), "control: git reads the system file"
        marker.unlink()

        _reset_hard(repo, git_command_env())
        assert not marker.exists()

    def test_global_attributes_cannot_arm_a_repo_driver(
        self, home: Path, repo: Path, tmp_path: Path
    ):
        """The default attributes file is read with global config gone, hence its own pin.

        Only the attributes half sits in the agent-writable home here; the driver
        is the repository's. Without ``core.attributesFile`` pinned, that pair runs.
        """
        script, marker = _marker_script(tmp_path, "attrs")
        subprocess.run(
            ["git", "config", "filter.evil.smudge", str(script)],
            cwd=repo,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
            check=True,
            capture_output=True,
        )
        (home / ".config" / "git" / "attributes").write_text("* filter=evil\n", encoding="utf-8")

        no_attr_pin = {**git_command_env()}
        no_attr_pin["GIT_CONFIG_COUNT"] = str(int(no_attr_pin["GIT_CONFIG_COUNT"]) - 1)
        _reset_hard(repo, no_attr_pin)
        assert marker.exists(), "control: the XDG attributes file arms the repo's driver"
        marker.unlink()

        _reset_hard(repo, git_command_env())
        assert not marker.exists()


class TestTheIsolationEntries:
    def test_entries_are_present(self):
        env = git_command_env()
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_ATTR_NOSYSTEM"] == "1"

    def test_attributes_pin_is_appended_after_the_neutralizers(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Appended, so no existing pin index moves and the count covers it."""
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")  # no host safe.directory
        neutral = update_governance.git_neutralizer_env()
        env = git_command_env()
        base = int(neutral["GIT_CONFIG_COUNT"])
        assert env["GIT_CONFIG_COUNT"] == str(base + 1)
        assert env[f"GIT_CONFIG_KEY_{base}"] == "core.attributesFile"
        assert env[f"GIT_CONFIG_VALUE_{base}"] == os.devnull
        for i in range(base):
            assert env[f"GIT_CONFIG_KEY_{i}"] == neutral[f"GIT_CONFIG_KEY_{i}"]

    def test_process_env_is_untouched(self):
        """Only the returned dict changes; sandboxed children inherit ``os.environ``."""
        before = dict(os.environ)
        git_command_env()
        assert dict(os.environ) == before

    def test_git_still_runs_and_commits_with_explicit_identity(self, home: Path, repo: Path):
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        env = {**git_command_env(), **_ID}
        subprocess.run(["git", "add", "b.txt"], cwd=repo, env=env, check=True, capture_output=True)
        done = subprocess.run(
            ["git", "commit", "-q", "-m", "b"], cwd=repo, env=env, capture_output=True
        )
        assert done.returncode == 0, done.stderr


class TestSafeDirectoryIsCarriedOver:
    """``safe.directory`` lives only in system/global config, which is now dropped."""

    def test_global_values_are_forwarded_in_order(self, home: Path, repo: Path):
        (home / ".gitconfig").write_text(
            f"[safe]\n\tdirectory = /elsewhere\n\tdirectory = {repo}\n", encoding="utf-8"
        )
        env = git_command_env()
        count = int(env["GIT_CONFIG_COUNT"])
        forwarded = [
            env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(count)
            if env[f"GIT_CONFIG_KEY_{i}"] == "safe.directory"
        ]
        assert forwarded[-2:] == ["/elsewhere", str(repo)]

    def test_a_differently_owned_checkout_still_opens(
        self, home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """git's own test knob makes the checkout read as another user's."""
        monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
        probe = ["git", "rev-parse", "--git-dir"]

        bare = subprocess.run(probe, cwd=repo, env=git_command_env(), capture_output=True)
        assert bare.returncode != 0, "control: git refuses a dubious-ownership checkout"

        (home / ".gitconfig").write_text(f"[safe]\n\tdirectory = {repo}\n", encoding="utf-8")
        update_governance._safe_directory_cache.clear()  # an operator edit, after the TTL
        allowed = subprocess.run(probe, cwd=repo, env=git_command_env(), capture_output=True)
        assert allowed.returncode == 0, allowed.stderr

    def test_a_value_behind_include_path_is_forwarded(
        self, home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Git follows ``include.path`` for ``safe.directory``, so the read must too."""
        monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
        local = tmp_path / "gitconfig.local"
        local.write_text(f"[safe]\n\tdirectory = {repo}\n", encoding="utf-8")
        (home / ".gitconfig").write_text(f"[include]\n\tpath = {local}\n", encoding="utf-8")
        done = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=repo, env=git_command_env(), capture_output=True
        )
        assert done.returncode == 0, done.stderr

    def test_only_safe_directory_is_carried(self, home: Path, repo: Path, tmp_path: Path):
        """Forwarding one key must not reopen the rest of the file."""
        script, marker = _marker_script(tmp_path, "safe")
        (home / ".gitconfig").write_text(
            f'[safe]\n\tdirectory = *\n[filter "evil"]\n\tsmudge = {script}\n',
            encoding="utf-8",
        )
        (home / ".config" / "git" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
        _reset_hard(repo, git_command_env())
        assert not marker.exists()

    def test_the_read_is_reused_within_the_window(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Every env build lands here, some on the event loop: read once per window."""
        calls: list[list[str]] = []
        real_run = subprocess.run

        def counting_run(argv, **kwargs):
            calls.append(list(argv))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting_run)
        git_command_env()
        first = len(calls)
        git_command_env()
        assert first > 0
        assert len(calls) == first


class TestThePinJudgesTheUrlEachFetchUses:
    """Each update path checks the pin against the URL its own fetch connects to.

    ``fetch_env=None`` is ``kirocrew update`` and the dashboard update, whose
    fetch inherits the process env and so reads ``~/.gitconfig``.
    ``fetch_env=git_command_env()`` is the gateway's isolated auto-update.
    """

    def _pin(self, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
        from kiro_crew.platform.governance import UpdatePins

        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(source=source),
        )

    def _origin(self, repo: Path, url: str) -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", url],
            cwd=repo,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
            check=True,
            capture_output=True,
        )

    def _rewrite(self, home: Path, to: str, frm: str) -> None:
        (home / ".gitconfig").write_text(f'[url "{to}"]\n\tinsteadOf = {frm}\n', encoding="utf-8")

    def _blocked(self, repo: Path, fetch_env: dict[str, str] | None, **kwargs: str) -> str:
        url = update_governance.resolve_remote_url(str(repo), fetch_env=fetch_env, **kwargs)
        return update_governance.update_blocked_reason(url)

    def _fetch_sees(self, repo: Path, fetch_env: dict[str, str] | None) -> str:
        """What a fetch run with *fetch_env* resolves ``origin`` to."""
        return subprocess.run(
            ["git", "ls-remote", "--get-url", "origin"],
            cwd=repo,
            env=fetch_env,
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout.strip()

    def test_the_judged_url_is_the_url_each_fetch_resolves(self, home: Path, repo: Path):
        self._origin(repo, "https://good.example/r.git")
        self._rewrite(home, "git@good.example:", "https://good.example/")
        for fetch_env in (None, git_command_env()):
            judged = update_governance.resolve_remote_url(
                str(repo), fetch_env=fetch_env, remote="origin"
            )
            assert judged == self._fetch_sees(repo, fetch_env)

    def test_a_rewrite_only_pin_passes_on_the_cli_and_dashboard(
        self, home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The operator's ordinary https -> ssh rewrite, pinned in its ssh spelling."""
        self._pin(monkeypatch, "git@good.example:*")
        self._origin(repo, "https://good.example/r.git")
        self._rewrite(home, "git@good.example:", "https://good.example/")
        assert self._blocked(repo, None, remote="origin") == ""
        assert self._blocked(repo, None) == ""

    def test_a_rewrite_to_an_unpinned_host_is_refused_on_the_cli_and_dashboard(
        self, home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._pin(monkeypatch, "*good.example*")
        self._origin(repo, "https://good.example/r.git")
        self._rewrite(home, "https://evil.example/", "https://good.example/")
        assert "does not match" in self._blocked(repo, None, remote="origin")
        assert "does not match" in self._blocked(repo, None)

    def test_a_rewrite_to_an_unpinned_host_cannot_redirect_the_gateway(
        self, home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An agent-planted global rewrite: the isolated fetch never applies it."""
        self._pin(monkeypatch, "*good.example*")
        self._origin(repo, "https://good.example/r.git")
        self._rewrite(home, "https://evil.example/", "https://good.example/")
        env = git_command_env()
        assert self._fetch_sees(repo, env) == "https://good.example/r.git"
        assert self._blocked(repo, env, remote="origin") == ""

    def test_a_planted_rewrite_cannot_dress_up_an_evil_origin_for_the_gateway(
        self, home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The reverse plant: the gateway judges the evil URL its fetch would use."""
        self._pin(monkeypatch, "*good.example*")
        self._origin(repo, "https://evil.example/r.git")
        self._rewrite(home, "https://good.example/", "https://evil.example/")
        assert "does not match" in self._blocked(repo, git_command_env(), remote="origin")

    def test_a_home_tracked_remote_is_judged_on_the_dashboard(
        self, home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A global ``branch.<b>.remote`` sends the dashboard's bare pull elsewhere."""
        self._pin(monkeypatch, "*good.example*")
        self._origin(repo, "https://good.example/r.git")
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout.strip()
        (home / ".gitconfig").write_text(
            f'[remote "evil"]\n\turl = https://evil.example/r.git\n'
            f'[branch "{branch}"]\n\tremote = evil\n',
            encoding="utf-8",
        )
        assert "does not match" in self._blocked(repo, None)
        assert self._blocked(repo, git_command_env()) == ""


class TestPlatformArms:
    def test_posix_drops_the_system_files(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(update_governance.platform_compat, "IS_WINDOWS", False)
        env = git_command_env()
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_ATTR_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull

    def test_windows_keeps_the_admin_owned_system_files(self, monkeypatch: pytest.MonkeyPatch):
        """Git for Windows needs its system gitconfig (TLS backend, autocrlf)."""
        monkeypatch.setattr(update_governance.platform_compat, "IS_WINDOWS", True)
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
        monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
        env = git_command_env()
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_ATTR_NOSYSTEM" not in env
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull

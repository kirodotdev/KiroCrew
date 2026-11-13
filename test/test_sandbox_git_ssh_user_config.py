"""The launcher's default ``GIT_SSH_COMMAND`` reads the user's ``~/.ssh/config`` when ssh would.

``-F /dev/null`` keeps ssh off the system config, whose root-owned includes it refuses
inside the user namespace, but it also drops the user's own config, so a git host
reachable only through a proxy or a host alias there fails to resolve. The default
names ``~/.ssh/config`` instead when the tier leaves ``~/.ssh`` readable and ssh would
accept the file and everything it includes, and keeps ``/dev/null`` otherwise: ssh
exits on a config it refuses rather than ignoring it.

These tests run the shipped ``scrub_env`` stage on a plain dict, as
``test_sandbox_git_ssh_identities.py`` does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, payload

from kiro_crew import sandbox_launcher_program as program

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the launcher payload uses POSIX-only os.getuid",
)

_PROXY_CONFIG = (
    "Host git.example.com\n    ProxyCommand nc -X connect -x proxy.example.com:3128 %h %p\n"
)


def _git_ssh_command(ssh_dir: Path, environ: dict[str, str] | None = None, **plan: object) -> str:
    launch_plan = payload(
        ssh_dir=str(ssh_dir), ssh_known_hosts=str(ssh_dir / "known_hosts"), **plan
    )
    run = program.Launch(launch_plan, RecordingLibc(), environ=dict(environ or {}))
    program.scrub_env(run)
    return run.environ["GIT_SSH_COMMAND"]


def _config_arg(command: str) -> str:
    words = command.split()
    return words[words.index("-F") + 1]


def _ssh_dir_with_config(tmp_path: Path, text: str = _PROXY_CONFIG) -> Path:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    config = ssh_dir / "config"
    config.write_text(text, encoding="utf-8")
    config.chmod(0o600)
    return ssh_dir


def test_a_visible_config_ssh_accepts_is_named(tmp_path: Path) -> None:
    ssh_dir = _ssh_dir_with_config(tmp_path)
    command = _git_ssh_command(ssh_dir, hide_ssh=0, sandbox_level="standard")
    assert _config_arg(command) == str(ssh_dir / "config")


def test_every_other_option_is_kept(tmp_path: Path) -> None:
    ssh_dir = _ssh_dir_with_config(tmp_path)
    (ssh_dir / "id_ed25519").write_text("key", encoding="utf-8")
    command = _git_ssh_command(ssh_dir, strict_host_key_opt=" -o StrictHostKeyChecking=accept-new")
    assert command == (
        "ssh -F %s -o IdentityFile=~/.ssh/id_ed25519 "
        "-o UserKnownHostsFile=~/.ssh/known_hosts -o StrictHostKeyChecking=accept-new"
        % (ssh_dir / "config")
    )


def test_no_config_keeps_dev_null(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


def test_the_strict_tier_keeps_dev_null(tmp_path: Path) -> None:
    """The strict tier hides ``~/.ssh``; a config left at the name is not the user's."""
    ssh_dir = _ssh_dir_with_config(tmp_path)
    command = _git_ssh_command(ssh_dir, hide_ssh=1, sandbox_level="strict")
    assert _config_arg(command) == "/dev/null"


def test_a_symlinked_config_keeps_dev_null(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    real = tmp_path / "real-config"
    real.write_text(_PROXY_CONFIG, encoding="utf-8")
    real.chmod(0o600)
    (ssh_dir / "config").symlink_to(real)
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


def test_a_directory_at_the_name_keeps_dev_null(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    (ssh_dir / "config").mkdir(parents=True)
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


def test_a_config_owned_by_another_uid_keeps_dev_null(tmp_path: Path) -> None:
    ssh_dir = _ssh_dir_with_config(tmp_path)
    command = _git_ssh_command(ssh_dir, real_uid=os.getuid() + 1)
    assert _config_arg(command) == "/dev/null"


def test_a_group_writable_config_keeps_dev_null(tmp_path: Path) -> None:
    """ssh refuses a config writable by others, and that refusal is fatal."""
    ssh_dir = _ssh_dir_with_config(tmp_path)
    (ssh_dir / "config").chmod(0o620)
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


@pytest.mark.parametrize(
    "include",
    [
        "Include /etc/ssh/ssh_config.d/*.conf",
        "include=/etc/ssh/ssh_config",
        "Include =/etc/ssh/ssh_config",
        "Include = /etc/ssh/ssh_config.d/*.conf",
        "Include ~/other/config",
        "Include config.d/* /opt/ssh/extra",
        "Include ../outside",
        'Include "${HOME}/x"',
        "Include work.con\\f",
        "Include config.d/\\*",
    ],
)
def test_an_include_outside_the_ssh_dir_keeps_dev_null(tmp_path: Path, include: str) -> None:
    """An included file gets ssh's ownership check, and system paths are root-owned."""
    ssh_dir = _ssh_dir_with_config(tmp_path, include + "\n" + _PROXY_CONFIG)
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


@pytest.mark.parametrize(
    "include",
    [
        "Include config.d/*",
        "  include ~/.ssh/work.conf  # per-host keys",
        "Include={ssh_dir}/hosts",
    ],
)
def test_an_include_under_the_ssh_dir_keeps_the_config(tmp_path: Path, include: str) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir = _ssh_dir_with_config(tmp_path, include.format(ssh_dir=ssh_dir) + "\n")
    assert _config_arg(_git_ssh_command(ssh_dir)) == str(ssh_dir / "config")


def _include_file(ssh_dir: Path, rel: str, text: str, mode: int = 0o600) -> None:
    path = ssh_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def test_a_quoted_include_path_with_a_space_keeps_the_config(tmp_path: Path) -> None:
    home = tmp_path / "my home"
    home.mkdir()
    ssh_dir = home / ".ssh"
    ssh_dir = _ssh_dir_with_config(home, 'Include "%s/hosts"\n' % ssh_dir)
    _include_file(ssh_dir, "hosts", _PROXY_CONFIG)
    assert _git_ssh_command(ssh_dir).startswith("ssh -F '%s' -o " % (ssh_dir / "config"))


@pytest.mark.parametrize("include", ["Include work.conf", "Include config.d/*"])
def test_an_included_file_ssh_would_refuse_keeps_dev_null(tmp_path: Path, include: str) -> None:
    """ssh checks the owner and mode of every included file, and a failure there is fatal."""
    ssh_dir = _ssh_dir_with_config(tmp_path, include + "\n")
    rel = "work.conf" if "work" in include else "config.d/a.conf"
    _include_file(ssh_dir, rel, _PROXY_CONFIG, mode=0o620)
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


def test_a_glob_include_skips_hidden_entries_as_ssh_does(tmp_path: Path) -> None:
    """ssh's glob does not match a leading dot, so a ``.git`` beside the files is not read."""
    ssh_dir = _ssh_dir_with_config(tmp_path, "Include config.d/*\n")
    _include_file(ssh_dir, "config.d/a.conf", _PROXY_CONFIG)
    (ssh_dir / "config.d" / ".git").mkdir()
    _include_file(ssh_dir, "config.d/.swp", "", mode=0o666)
    assert _config_arg(_git_ssh_command(ssh_dir)) == str(ssh_dir / "config")


def test_an_included_file_that_includes_a_system_path_keeps_dev_null(tmp_path: Path) -> None:
    ssh_dir = _ssh_dir_with_config(tmp_path, "Include config.d/*\n")
    _include_file(ssh_dir, "config.d/a.conf", "Include /etc/ssh/ssh_config.d/*\n")
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


def test_an_include_cycle_keeps_dev_null(tmp_path: Path) -> None:
    ssh_dir = _ssh_dir_with_config(tmp_path, "Include config\n")
    assert _config_arg(_git_ssh_command(ssh_dir)) == "/dev/null"


def test_a_path_sh_would_split_is_quoted(tmp_path: Path) -> None:
    home = tmp_path / "my home"
    home.mkdir()
    ssh_dir = _ssh_dir_with_config(home)
    command = _git_ssh_command(ssh_dir)
    assert command.startswith("ssh -F '%s' -o " % (ssh_dir / "config"))


def test_a_caller_command_is_left_alone(tmp_path: Path) -> None:
    ssh_dir = _ssh_dir_with_config(tmp_path)
    command = _git_ssh_command(ssh_dir, {"GIT_SSH_COMMAND": "ssh -i /custom/key"})
    assert command == "ssh -i /custom/key"

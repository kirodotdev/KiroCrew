"""The launcher's default ``GIT_SSH_COMMAND`` names only identity files that exist.

A user-supplied ``IdentityFile`` that is missing is an error-level ``no such identity``
line on every git-over-ssh call, which reads as a failed fetch although the command
exits 0. The default therefore names, of the three keys it has always named, only the
ones present in the sandbox's ``~/.ssh`` -- and never fewer than one, because an
explicit ``IdentityFile`` is what keeps ssh from trying its wider default list
(``id_dsa``, the ``_sk`` keys, ``id_xmss``).

These tests run the shipped ``scrub_env`` stage on a plain dict, as
``test_sandbox_gradle_daemon.py`` does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, payload

from kiro_crew import sandbox_launcher_program as program

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the launcher payload uses POSIX-only os.getuid",
)

_ALL_THREE = ("id_rsa", "id_ecdsa", "id_ed25519")


def _git_ssh_command(ssh_dir: Path, environ: dict[str, str] | None = None) -> str:
    plan = payload(ssh_dir=str(ssh_dir), ssh_known_hosts=str(ssh_dir / "known_hosts"))
    run = program.Launch(plan, RecordingLibc(), environ=dict(environ or {}))
    program.scrub_env(run)
    return run.environ["GIT_SSH_COMMAND"]


def _named_identities(command: str) -> list[str]:
    return [
        token.split("=", 1)[1].rsplit("/", 1)[1]
        for token in command.split()
        if token.startswith("IdentityFile=")
    ]


def test_only_the_present_key_is_named(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ecdsa").write_text("key", encoding="utf-8")
    assert _named_identities(_git_ssh_command(ssh_dir)) == ["id_ecdsa"]


def test_every_present_key_is_named_in_the_usual_order(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    for name in ("id_ed25519", "id_rsa"):
        (ssh_dir / name).write_text("key", encoding="utf-8")
    assert _named_identities(_git_ssh_command(ssh_dir)) == ["id_rsa", "id_ed25519"]


def test_with_none_of_the_three_all_three_stay_named(tmp_path: Path) -> None:
    """No key named would let ssh fall back to its own, wider default list."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_dsa").write_text("key", encoding="utf-8")
    assert _named_identities(_git_ssh_command(ssh_dir)) == list(_ALL_THREE)


def test_a_key_type_outside_the_three_is_never_named(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    for name in ("id_ecdsa", "id_dsa", "id_ed25519_sk"):
        (ssh_dir / name).write_text("key", encoding="utf-8")
    assert _named_identities(_git_ssh_command(ssh_dir)) == ["id_ecdsa"]


def test_the_rest_of_the_command_is_unchanged(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("key", encoding="utf-8")
    command = _git_ssh_command(ssh_dir)
    assert command.startswith("ssh -F /dev/null ")
    assert "-o UserKnownHostsFile=~/.ssh/known_hosts" in command


def test_a_caller_command_is_left_alone(tmp_path: Path) -> None:
    command = _git_ssh_command(tmp_path, {"GIT_SSH_COMMAND": "ssh -i /custom/key"})
    assert command == "ssh -i /custom/key"

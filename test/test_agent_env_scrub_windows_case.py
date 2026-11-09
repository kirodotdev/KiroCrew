"""The agent env scrub folds name case on Windows only.

Windows environment names are case-insensitive, so a lower-case spelling of a
scrubbed name IS the scrubbed variable there and the child reads it under its
upper-case name. POSIX keeps the raw spelling: the two spellings are different
variables and the scrub keeps the lower-case one. Each Windows refusal/removal
is paired with the POSIX control taken the same way.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import sandbox_spawn
from kiro_crew.acp.harness import deepseek as deepseek_mod

_DECOY = "decoy-not-a-credential"


@pytest.mark.parametrize("name", ["aws_secret_access_key", "Aws_Session_Token"])
def test_on_windows_a_case_variant_of_a_scrubbed_name_is_refused(monkeypatch, name: str) -> None:
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ValueError, match="agent environment scrub removes") as refused:
        deepseek_mod._validate_deepseek_env_mapping({name: "secret://dsh-proof"})
    assert repr(name) in str(refused.value)


def test_on_posix_the_case_variant_is_not_judged_a_scrubbed_name(monkeypatch) -> None:
    """Unchanged from main: on POSIX it is a different variable the scrub keeps."""
    monkeypatch.setattr(os, "name", "posix")
    deepseek_mod._validate_deepseek_env_mapping({"aws_secret_access_key": "secret://x"})


def test_on_windows_the_subprocess_scrub_removes_a_case_variant(monkeypatch) -> None:
    env = {
        "aws_secret_access_key": _DECOY,
        "slack_bot_token": _DECOY,
        "PythonPath": _DECOY,
        "Path": "C:\\bin",
    }
    monkeypatch.setattr(os, "name", "nt")
    assert sandbox_spawn.scrub_agent_subprocess_env(env) == {"Path": "C:\\bin"}
    assert sandbox_spawn.scrub_agent_denied_env(env) == {
        "aws_secret_access_key": _DECOY,
        "PythonPath": _DECOY,
        "Path": "C:\\bin",
    }


def test_on_posix_the_subprocess_scrub_keeps_a_case_variant(monkeypatch) -> None:
    env = {"aws_secret_access_key": _DECOY, "AWS_SECRET_ACCESS_KEY": _DECOY}
    monkeypatch.setattr(os, "name", "posix")
    assert sandbox_spawn.scrub_agent_subprocess_env(env) == {"aws_secret_access_key": _DECOY}
    assert sandbox_spawn.scrub_agent_denied_env({"slack_bot_token": _DECOY}) == {
        "slack_bot_token": _DECOY
    }


def test_on_windows_the_ssh_socket_forward_still_reaches_a_case_variant(monkeypatch) -> None:
    """The opted-in forward reaches every spelling the fold scrubs, and nothing else."""
    env = {"ssh_auth_sock": "\\\\.\\pipe\\agent", "SSH_AUTH_SOCK_EXTRA": _DECOY}
    monkeypatch.setattr(os, "name", "nt")
    assert sandbox_spawn.scrub_agent_subprocess_env(env, forward_ssh_auth_sock=True) == {
        "ssh_auth_sock": "\\\\.\\pipe\\agent"
    }
    assert sandbox_spawn.scrub_agent_subprocess_env(env) == {}


def test_every_scrub_prefix_is_upper_case() -> None:
    """The Windows fold upper-cases the NAME only, so a lower-case prefix would never match."""
    prefixes = sandbox_spawn.agent_env_scrub_prefixes()
    assert prefixes
    assert [p for p in prefixes if p != p.upper()] == []

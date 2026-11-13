"""The ssh-agent consent notice: a spawn left without the socket for lack of consent says so.

A harness whose sandbox hides ``~/.ssh`` (``strict``, or an enforced harness's
credential mask) keeps ``SSH_AUTH_SOCK`` only with the ``ssh_auth_sock_consent.json``
consent. Without it the child starts with no agent socket and can only report that
no ssh keys are available. These tests pin that the operator is told which setting
decides it and how to grant it -- and that the notice changes nothing about what is
forwarded: the socket is still scrubbed without consent.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from unittest.mock import patch

import acp_launch_capture as capture_mod
import pytest

import kiro_crew.sandbox as sb
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import launch as launch_mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

_SSH_MASK = (os.path.expanduser("~/.ssh"),)


@pytest.fixture
def _no_consent(monkeypatch, tmp_path):
    """Consent absent, non-Windows, ungoverned, consent leaf under *tmp_path*."""
    from kiro_crew import ssh_auth_sock_consent as consent
    from kiro_crew.config import loader

    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(consent, "is_granted", lambda: False)
    monkeypatch.setattr(sb, "_governance_sandbox_floor", lambda: None)
    leaf = tmp_path / "ssh_auth_sock_consent.json"
    monkeypatch.setattr(loader, "ssh_auth_sock_consent_path", lambda: leaf)
    return consent, leaf


# --- the notice itself ---


def test_masked_harness_without_consent_names_the_setting_and_how_to_grant_it(_no_consent):
    _consent, leaf = _no_consent
    notice = sb.ssh_auth_sock_consent_notice("codex", "standard", _SSH_MASK)
    assert "codex" in notice
    assert "ssh-agent forward consent" in notice
    assert '{"enabled": true}' in notice
    assert str(leaf) in notice
    assert "outside Kiro Crew" in notice


def test_strict_without_consent_gets_the_notice(_no_consent):
    assert sb.ssh_auth_sock_consent_notice("kiro", "strict", ())


def test_no_notice_where_the_socket_is_forwarded_by_default(_no_consent):
    """Standard tier, no mask over ~/.ssh: the forward needs no consent, so nothing to say."""
    assert sb._forward_ssh_auth_sock("standard", ()) is True
    assert sb.ssh_auth_sock_consent_notice("claude", "standard", ()) == ""


def test_no_notice_once_consent_is_granted(_no_consent, monkeypatch):
    consent, _leaf = _no_consent
    monkeypatch.setattr(consent, "is_granted", lambda: True)
    assert sb.ssh_auth_sock_consent_notice("codex", "standard", _SSH_MASK) == ""


def test_no_notice_on_windows(_no_consent, monkeypatch):
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", True)
    assert sb.ssh_auth_sock_consent_notice("codex", "strict", _SSH_MASK) == ""


def test_a_failure_to_resolve_omits_the_notice(_no_consent, monkeypatch):
    """Fail-closed verdict, no notice: the socket is scrubbed, but nothing is guessed."""

    def _boom():
        raise RuntimeError("composition failed")

    monkeypatch.setattr(sb, "_governance_sandbox_floor", _boom)
    assert sb._forward_ssh_auth_sock("standard", _SSH_MASK) is False
    assert sb.ssh_auth_sock_consent_notice("codex", "standard", _SSH_MASK) == ""


@pytest.mark.parametrize(
    ("mode", "hidden"),
    [
        ("standard", ()),
        ("standard", _SSH_MASK),
        ("strict", ()),
        ("cc", ()),
        ("off", ()),
        ("paranoid-new-tier", ()),
    ],
)
def test_the_notice_appears_exactly_when_the_forward_waits_on_consent(_no_consent, mode, hidden):
    """Notice and verdict read the same inputs: a notice iff the socket is scrubbed."""
    forwarded = sb._forward_ssh_auth_sock(mode, hidden)
    noticed = bool(sb.ssh_auth_sock_consent_notice("codex", mode, hidden))
    assert noticed is (not forwarded)


def test_the_socket_is_still_scrubbed_without_consent(_no_consent):
    """The notice does not change the verdict or the scrub."""
    assert sb._forward_ssh_auth_sock("standard", _SSH_MASK) is False
    env = {"SSH_AUTH_SOCK": "/tmp/agent.sock", "PATH": "/bin"}
    scrubbed = sb.scrub_agent_subprocess_env(dict(env), forward_ssh_auth_sock=False)
    assert "SSH_AUTH_SOCK" not in scrubbed
    # Control (same scrub): the forward keeps it.
    kept = sb.scrub_agent_subprocess_env(dict(env), forward_ssh_auth_sock=True)
    assert kept.get("SSH_AUTH_SOCK") == "/tmp/agent.sock"


# --- the reported case, with the real harness masks ---


def test_codex_mask_gets_the_notice_and_claude_does_not(_no_consent):
    """Under the default tier, Codex's own mask hides ~/.ssh; Claude's leaves it visible."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
    from kiro_crew.agent_sdk.tool_gate import adapter_hidden_credential_dirs

    codex_mask = tuple(adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
    claude_mask = tuple(adapter_hidden_credential_dirs(ACP_BACKEND_CLAUDE))
    assert sb._forward_ssh_auth_sock("auto", codex_mask) is False
    assert sb.ssh_auth_sock_consent_notice(ACP_BACKEND_CODEX, "auto", codex_mask)
    assert sb._forward_ssh_auth_sock("auto", claude_mask) is True
    assert sb.ssh_auth_sock_consent_notice(ACP_BACKEND_CLAUDE, "auto", claude_mask) == ""


# --- kirocrew doctor ---


def test_doctor_names_the_harnesses_and_the_remedy(_no_consent, monkeypatch, capsys):
    from kiro_crew import cli_doctor
    from kiro_crew.doctor_checks import confinement

    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/a.sock")
    monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(cli_doctor.sandbox, "configured_sandbox_mode", lambda: "auto")
    confinement._doctor_ssh_agent_forward()
    out = capsys.readouterr().out
    assert "ssh-agent:" in out
    assert "codex" in out
    assert "claude" not in out.split("ssh-agent:", 1)[1].splitlines()[0]
    assert "enabled" in out and "ssh_auth_sock_consent.json" in out


def test_doctor_is_silent_once_consent_is_granted(_no_consent, monkeypatch, capsys):
    from kiro_crew import cli_doctor
    from kiro_crew.doctor_checks import confinement

    consent, _leaf = _no_consent
    monkeypatch.setattr(consent, "is_granted", lambda: True)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/a.sock")
    monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(cli_doctor.sandbox, "configured_sandbox_mode", lambda: "auto")
    confinement._doctor_ssh_agent_forward()
    assert capsys.readouterr().out == ""


def test_doctor_is_silent_without_an_ssh_agent(_no_consent, monkeypatch, capsys):
    from kiro_crew.doctor_checks import confinement

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    confinement._doctor_ssh_agent_forward()
    assert capsys.readouterr().out == ""


# --- through the real launch tail ---


@pytest.fixture
def _fresh_notice_memory(monkeypatch):
    monkeypatch.setattr(launch_mod, "_SSH_AUTH_SOCK_NOTICE_LOGGED", set())


def _spawn(tmp_path, *, forward: bool, notice: str, calls: list, sock: str | None):
    recorders: list = []

    class _Spy(capture_mod._Recorder):
        def __init__(self) -> None:
            super().__init__()
            recorders.append(self)

    def _notice(backend, mode, hidden):
        calls.append((backend, mode, hidden))
        return notice

    real_tools = client_mod._launch_tools

    def _tools():
        return dataclasses.replace(real_tools(), ssh_auth_sock_consent_notice=_notice)

    env = {"SSH_AUTH_SOCK": sock} if sock else {}
    with patch.object(capture_mod, "_Recorder", _Spy):
        capture_mod.capture(
            ACP_BACKEND_KIRO,
            tmp_path,
            extra_patches=[
                patch.dict(os.environ, env),
                patch.object(client_mod, "_forward_ssh_auth_sock", return_value=forward),
                patch.object(client_mod, "_launch_tools", _tools),
                # The capture stubs the scrub to a pass-through; run the real one
                # so the child env shows what the spawn actually removes.
                patch.object(
                    client_mod, "scrub_agent_subprocess_env", sb.scrub_agent_subprocess_env
                ),
            ],
        )
    assert recorders, "the capture did not reach the spawn"
    return recorders[-1].env


def test_the_tools_default_is_the_sandbox_notice():
    """Production launches compute the notice from the sandbox's own reader."""
    fields = {f.name: f for f in dataclasses.fields(launch_mod.LaunchTools)}
    assert fields["ssh_auth_sock_consent_notice"].default is sb.ssh_auth_sock_consent_notice


def test_launch_logs_the_notice_once_when_the_socket_is_withheld(
    tmp_path, caplog, _fresh_notice_memory
):
    calls: list = []
    text = "ssh-agent not forwarded to the kiro agent: NOTICE"
    with caplog.at_level(logging.WARNING):
        child = _spawn(tmp_path, forward=False, notice=text, calls=calls, sock="/tmp/a.sock")
        _spawn(tmp_path, forward=False, notice=text, calls=calls, sock="/tmp/a.sock")
    assert "SSH_AUTH_SOCK" not in child
    assert calls and calls[0][0] == ACP_BACKEND_KIRO
    assert len(calls) == 1
    assert [r.getMessage() for r in caplog.records].count(text) == 1


def test_a_notice_that_raises_leaves_the_spawn_unchanged(tmp_path, caplog, _fresh_notice_memory):
    recorders: list = []

    class _Spy(capture_mod._Recorder):
        def __init__(self) -> None:
            super().__init__()
            recorders.append(self)

    def _boom(*_a):
        raise RuntimeError("unreadable")

    real_tools = client_mod._launch_tools
    with patch.object(capture_mod, "_Recorder", _Spy):
        result = capture_mod.capture(
            ACP_BACKEND_KIRO,
            tmp_path,
            extra_patches=[
                patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/a.sock"}),
                patch.object(client_mod, "_forward_ssh_auth_sock", return_value=False),
                patch.object(
                    client_mod, "scrub_agent_subprocess_env", sb.scrub_agent_subprocess_env
                ),
                patch.object(
                    client_mod,
                    "_launch_tools",
                    lambda: dataclasses.replace(real_tools(), ssh_auth_sock_consent_notice=_boom),
                ),
            ],
        )
    assert recorders and result["argv"], "the spawn did not proceed"
    # Same child as a spawn whose notice computes to nothing (minus the per-spawn
    # random session credential, which differs between any two spawns).
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    baseline = _spawn(tmp_path, forward=False, notice="", calls=[], sock="/tmp/a.sock")
    got = {k: v for k, v in recorders[0].env.items() if k != STUB_SESSION_TOKEN_ENV}
    assert got == {k: v for k, v in baseline.items() if k != STUB_SESSION_TOKEN_ENV}
    assert "SSH_AUTH_SOCK" not in got
    assert "ssh-agent not forwarded" not in caplog.text
    # One attempt per harness: a notice that could not be computed is not retried.
    assert ACP_BACKEND_KIRO in launch_mod._SSH_AUTH_SOCK_NOTICE_LOGGED


def test_launch_says_nothing_when_the_socket_is_forwarded(tmp_path, caplog, _fresh_notice_memory):
    calls: list = []
    with caplog.at_level(logging.WARNING):
        child = _spawn(
            tmp_path, forward=True, notice="SHOULD NOT APPEAR", calls=calls, sock="/tmp/a.sock"
        )
    assert child.get("SSH_AUTH_SOCK") == "/tmp/a.sock"
    assert calls == []
    assert "SHOULD NOT APPEAR" not in caplog.text


def test_launch_says_nothing_without_a_parent_socket(tmp_path, caplog, _fresh_notice_memory):
    calls: list = []
    with patch.object(client_mod, "_resolve_ssh_auth_sock", lambda env: None):
        with caplog.at_level(logging.WARNING):
            _spawn(tmp_path, forward=False, notice="SHOULD NOT APPEAR", calls=calls, sock=None)
    assert calls == []
    assert "SHOULD NOT APPEAR" not in caplog.text


def test_concurrent_spawns_of_one_harness_log_once(_fresh_notice_memory, caplog):
    """A second spawn while the first is still computing its notice logs nothing."""
    import asyncio
    import threading
    from types import SimpleNamespace

    from kiro_crew.testing.wait import await_until, wait_until

    entered = threading.Event()
    gate = threading.Event()
    calls: list = []

    def _notice(backend, mode, hidden):
        calls.append(backend)
        entered.set()
        gate.wait()  # released by the test in ``finally``; never a timing bet
        return "ssh-agent not forwarded to the codex agent: NOTICE"

    class _Host:
        async def _to_thread_guarding_sandbox(self, fn, /, *args, **kwargs):
            return await asyncio.to_thread(fn, *args, **kwargs)

    tools = SimpleNamespace(
        ssh_auth_sock_consent_notice=_notice, logger=logging.getLogger("test-notice")
    )
    request = SimpleNamespace(backend="codex", sandbox_mode="auto", extra_hidden_dirs=())

    async def _both():
        first = asyncio.create_task(
            launch_mod._note_ssh_auth_sock_withheld(_Host(), tools, request)
        )
        try:
            # The first spawn is parked inside its notice read...
            await asyncio.to_thread(wait_until, entered.is_set)
            # ...when the second one runs to completion.
            await await_until(
                asyncio.ensure_future(
                    launch_mod._note_ssh_auth_sock_withheld(_Host(), tools, request)
                ),
                what="second spawn's notice step",
            )
            assert calls == ["codex"]
        finally:
            gate.set()
        await await_until(first, what="first spawn's notice step")

    with caplog.at_level(logging.WARNING):
        asyncio.run(_both())
    assert calls == ["codex"]
    assert caplog.text.count("NOTICE") == 1

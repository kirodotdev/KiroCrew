"""The DeepSeek Harness's session start: the restore verb it is sent, and its spawn and handshake."""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from unittest.mock import AsyncMock

import pytest
from acp_deepseek_backend_helpers import (  # noqa: F401 -- fixtures are used by name
    _client,
    _DeepseekLaunchRig,
    _pinned_kiro_cli_version,
)

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_DEEPSEEK

# ── The restore verb ─────────────────────────────────────────────────────────


def _arm_restore(client: AcpClient, capabilities: dict, restore_result: dict) -> list[str]:
    """Drive one handshake with a resume id pending; return the methods sent."""
    sent: list[str] = []
    responses = [{"protocolVersion": 1, "agentCapabilities": capabilities}, restore_result]

    async def fake_send(method: str, params: dict) -> int:
        sent.append(method)
        return len(sent)

    async def fake_wait(req_id: int, timeout: float = 50.0, *, method="", expected_mcp=None):
        # A handshake that does not restore falls through to session/new, so every
        # id past the scripted ones answers as a fresh session rather than as {}.
        if req_id <= len(responses):
            scripted = responses[req_id - 1]
            if sent and sent[req_id - 1] == "session/new":
                return {"sessionId": "fresh"}
            return scripted
        return {"sessionId": "fresh"}

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._resume_session_id = "prior-session"
    return sent


@pytest.mark.asyncio
async def test_a_member_is_sent_session_resume(tmp_path: pathlib.Path) -> None:
    """The verb follows the membership set, and the restore is adopted."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _arm_restore(
        client,
        {"sessionCapabilities": {"resume": {}, "list": {}, "close": {}}},
        {"configOptions": []},
    )

    await client._initialize_session()

    assert "session/resume" in sent
    assert "session/load" not in sent
    assert client._session_id == "prior-session"
    assert client._resumed is True


@pytest.mark.asyncio
async def test_a_non_member_is_still_sent_session_load(tmp_path: pathlib.Path) -> None:
    """The other arm, so the set is what decides rather than the new code path.

    Without this the verb swap could be unconditional and every kiro-family resume
    would be sent a method kiro-cli does not serve.
    """
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _arm_restore(client, {"loadSession": True}, {"modes": ["chat"]})

    await client._initialize_session()

    assert "session/load" in sent
    assert "session/resume" not in sent


@pytest.mark.asyncio
async def test_the_capability_is_read_where_this_harness_advertises_it(
    tmp_path: pathlib.Path,
) -> None:
    """A harness advertising no ``resume`` must not be sent the verb at all.

    ``loadSession`` is absent from this harness's ``initialize`` result, so reading
    that flag would answer False and silently start every reopened session fresh.
    Reading the wrong key is the failure this pins.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _arm_restore(client, {"sessionCapabilities": {"list": {}}}, {"configOptions": []})

    await client._initialize_session()

    assert "session/resume" not in sent
    assert client._resumed is False


@pytest.mark.asyncio
async def test_a_restore_with_no_modes_block_is_adopted(tmp_path: pathlib.Path) -> None:
    """This harness can never return ``modes``; gating on one would discard the restore."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    _arm_restore(client, {"sessionCapabilities": {"resume": {}}}, {"configOptions": []})

    await client._initialize_session()

    assert client._resumed is True


def test_both_varying_reads_are_keyed_on_the_one_set() -> None:
    """The capability AND the verb, from the same membership, in the shared path.

    Two sets would invite an entry in one and not the other, which reads as "cannot
    restore" and starts every reopened session fresh. A source scan because the
    hazard is a second decision site appearing, which no behavioural test sees.
    """
    body = inspect.getsource(AcpClient._initialize_session)
    assert body.count("ACP_BACKENDS_RESUME_WITHOUT_LOAD") == 2
    assert 'session_capabilities.get("resume")' in body
    assert "METHOD_SESSION_RESUME" in body
    assert "restore_method" in body


def test_the_restore_params_are_built_once_for_either_verb() -> None:
    """One params dict, because the two calls share a contract.

    ``ResumeSessionRequest`` carries the same fields as ``LoadSessionRequest``, so a
    second dict would be a copy that can drift rather than an abstraction.
    """
    body = inspect.getsource(AcpClient._initialize_session)
    assert body.count("load_params: dict = {") == 1


def test_the_restore_method_is_resolved_before_the_try() -> None:
    """The failure log names the verb, so the name must be bound on every path."""
    body = inspect.getsource(AcpClient._initialize_session)
    resolved = body.index("restore_method = (")
    guarded = body.index("try:", resolved - 400)
    assert resolved < guarded, "restore_method must be bound before the guarded block"


# ── Spawn and handshake ──────────────────────────────────────────────────────


def test_a_stalled_restore_keeps_its_mcp_detail() -> None:
    """The timeout message enricher has to know BOTH restore verbs.

    A stalled restore is one of the few failures an operator reads verbatim, and the
    MCP progress detail the caller supplies is what makes it actionable. The enricher
    gates on a set of methods, so a harness sent the other verb loses that detail
    while the message still claims to describe the restore.
    """
    body = inspect.getsource(AcpClient._wait_for_response)
    assert (
        "{METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SESSION_RESUME}" in body
    ), "the enricher must name every restore verb this client can send"


def test_the_handshake_is_the_spec_dialect() -> None:
    """Integer ``protocolVersion`` 1, captured off its own wire."""
    from kiro_crew.acp.client import _PROTOCOL_VERSION_BY_BACKEND

    assert _PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_DEEPSEEK] == 1


def test_the_argv_is_the_host_binary_plus_the_shipped_profile() -> None:
    """The ACP package is a plugin with no executable; the host binary boots it."""
    from kiro_crew.agent_sdk.backends import launch_for

    # The RECORD is what is pinned, not a line of source: the adapter reads
    # ``acp.launch.resolve_self_served_launch``, which is shared with the sibling harnesses, so
    # a source-text assertion there would pin their spelling as well as this one's.
    record = launch_for(ACP_BACKEND_DEEPSEEK)
    assert record.binary == "dsh"
    assert record.acp_args == ("--profile", "acp")
    assert record.spawn_label == "dsh --profile acp"
    # The installer names the HOST binary. The ACP package is a plugin with no
    # executable of its own, so advice naming it would not produce a runnable
    # harness -- which is the whole reason this fact is data rather than prose.
    assert record.install_command == "npm i -g @deepseek-ai/dsh"
    assert "dsh" in record.missing_hint or "plugin" in record.missing_hint


def test_the_resolution_ladder_prefers_the_explicit_override(monkeypatch, tmp_path) -> None:
    """Override, then mise, then PATH -- the plain-binary ladder.

    What is pinned is the ORDER, so executability is STUBBED rather than staged on
    disk. A file written and chmod-ed here answers ``is_executable_file`` on POSIX
    and not on Windows, where an executable needs a recognised extension -- so a
    disk-staged override fell through to the mise rung there, and this test was
    asserting the platform's notion of an executable instead of the precedence it
    exists to pin.
    """
    from kiro_crew.acp import client as client_module

    binary = tmp_path / "dsh"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("DSH_BIN", str(binary))
    monkeypatch.setattr(
        client_module.platform_compat,
        "is_executable_file",
        lambda candidate: str(candidate) == str(binary),
    )
    monkeypatch.setattr(client_module, "_mise_which", lambda _name: "/never/reached")

    resolved, _searched = client_module._resolve_self_served_bin(ACP_BACKEND_DEEPSEEK)
    assert resolved == str(binary)


def test_an_absent_binary_reports_what_was_searched(monkeypatch) -> None:
    """A caller must be able to say where it looked rather than raising from inside."""
    from kiro_crew.acp import client as client_module

    monkeypatch.delenv("DSH_BIN", raising=False)
    monkeypatch.setattr(client_module, "_mise_which", lambda _name: None)
    monkeypatch.setattr(client_module.shutil, "which", lambda *_a, **_kw: None)

    resolved, searched = client_module._resolve_self_served_bin(ACP_BACKEND_DEEPSEEK)
    assert resolved is None
    assert searched


def test_the_sandbox_posture_is_pinned_in_the_child_environment() -> None:
    """Defence in depth, and not a routing claim.

    One variable selects both a sandbox mode and an approval policy on this
    harness, and its permissive end runs sensitive actions without asking anyone.
    Pinning the confined value keeps an inherited shell variable from selecting
    that end; it does not make a tool call reach Crew's gate.
    """
    from kiro_crew.acp.client import _ENV_DEEPSEEK_PERMISSION_MODE, DEEPSEEK_PERMISSION_MODE

    assert _ENV_DEEPSEEK_PERMISSION_MODE == "DSH_PERMISSION_MODE"
    assert DEEPSEEK_PERMISSION_MODE == "workspace-write"

    env = {_ENV_DEEPSEEK_PERMISSION_MODE: "danger-full-access"}
    deepseek_mod.DeepseekLaunch().apply_spawn_env(env)
    assert env[_ENV_DEEPSEEK_PERMISSION_MODE] == DEEPSEEK_PERMISSION_MODE

    async def _no_vault_key(fn):
        return {}, ()

    env = {_ENV_DEEPSEEK_PERMISSION_MODE: "danger-full-access"}
    asyncio.run(deepseek_mod.DeepseekLaunch().prepare_spawn_env(env, to_thread=_no_vault_key))
    assert env[_ENV_DEEPSEEK_PERMISSION_MODE] == DEEPSEEK_PERMISSION_MODE


def test_the_arm_runs_the_credential_mask_preflight_before_its_read_back(
    monkeypatch, tmp_path
) -> None:
    """An ENFORCED harness takes a preflight call site, and takes it FIRST.

    ORDER is the part worth pinning: the read-back boots a child of this harness, and
    a child that ran before the mask was resolved would read the operator's
    credentials with the gate's own blessing. And the gate is composed through the
    harness's own per-launch patch flag, naming the sealed copy, never by editing a
    profile Crew does not own.
    """
    rig = _DeepseekLaunchRig(monkeypatch, tmp_path)
    plan = rig.run()
    names = rig.names()
    assert names.index("preflight") < names.index("wrap") < names.index("verify")
    preflight, backend, mode = rig.args_of("preflight")
    assert preflight is launch_mod._sandbox_preflight
    assert (backend, mode) == (ACP_BACKEND_DEEPSEEK, rig.MODE)
    assert names.index("seal") < names.index("patch")
    assert rig.args_of("patch") == (rig.SEALED,)
    assert plan.argv == [*rig.ARGV, deepseek_mod._DSH_PATCH_FLAG, rig.PATCH]
    wrapped, kwargs = rig.args_of("wrap")
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_DEEPSEEK, rig.HIDDEN)
    assert wrapped == plan.argv, "the probe did not boot the composition the session runs"
    assert kwargs["extra_hidden_dirs"] == rig.HIDDEN
    assert kwargs["extra_expose_files"] == expose
    assert kwargs["extra_private_dirs"] == (str(rig.probe_dir),)
    assert rig.args_of("verify")[:2] == ([*rig.SANDBOX, *wrapped], rig.SEALED)
    assert (plan.extra_hidden_dirs, plan.extra_expose_files) == (rig.HIDDEN, expose)


def test_a_deepseek_readback_issue_becomes_an_acp_routing_refusal(monkeypatch, tmp_path) -> None:
    """The DeepSeek-only read-back converts every issue through the enforced gate.

    And the refusal is reached only after the probe's launcher and its window have
    been reclaimed, so a refused session leaves nothing behind.
    """
    rig = _DeepseekLaunchRig(monkeypatch, tmp_path, routing=("not loaded", "reinstall it"))
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        rig.run()
    assert type(excinfo.value) is AcpToolGateUnroutable
    assert excinfo.value.__suppress_context__, "the gate module's exception leaks as context"
    assert "reinstall it" in str(excinfo.value)
    assert rig.args_of("unlink") == (rig.CLEANUP,)
    assert not rig.probe_dir.exists(), "the probe's window survived the refusal"
    assert rig.plan is None

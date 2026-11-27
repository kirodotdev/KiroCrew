"""A host that refuses an enforced harness says so before it looks for the binary.

On a host where the harness's credential mask cannot be applied (native Windows,
or ``agent.sandbox`` off) every session start of an enforced harness is refused by
the sandbox floor. That refusal must be the first and only error: a missing binary
looked up first raised an install hint, the start loop retried it as a transient
failure, and the floor sentence was never shown. With the binary present the
refusal is the same single error. The mask verdict is faked through
``sandbox.credential_mask_applies``, so the test runs the same on every platform.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import sandbox
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import antigravity as antigravity_mod
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable
from kiro_crew.agent_sdk import tool_gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_ANTIGRAVITY,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_MIMOCODE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)

_ENFORCED_ON_THE_CLIENT = [
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_PI,
    ACP_BACKEND_MIMOCODE,
    ACP_BACKEND_ANTIGRAVITY,
]


def _binary_lookups(monkeypatch, lookups: list[str], *, present: bool) -> None:
    """Every binary lookup these harnesses make records itself, then finds or misses."""
    binary = str(Path(sys.executable))

    async def _self_served(backend):
        lookups.append(backend)
        if present:
            return binary, [binary, "acp"], backend, backend
        raise AcpError(f"{backend} not found (searched nothing). Install it.")

    def _pi_acp():
        lookups.append("pi-acp")
        return ([binary] if present else None, "")

    def _pi():
        lookups.append("pi")
        return (binary if present else None, "")

    def _antigravity():
        lookups.append("antigravity")
        if present:
            return antigravity_mod.AntigravityResolution(binary, binary, "")
        raise AcpError("Google Antigravity ACP is not fully installed")

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _self_served)
    monkeypatch.setattr(pi_mod, "_pi_acp_argv_cache", launch_mod._UNRESOLVED)
    monkeypatch.setattr(pi_mod, "_pi_bin_cache", launch_mod._UNRESOLVED)
    monkeypatch.setattr(pi_mod, "_resolve_pi_acp_bin", _pi_acp)
    monkeypatch.setattr(pi_mod, "_resolve_pi_bin", _pi)
    monkeypatch.setattr(antigravity_mod, "resolve_antigravity_runtime", _antigravity)


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True], ids=["binary-absent", "binary-present"])
@pytest.mark.parametrize("backend", _ENFORCED_ON_THE_CLIENT)
async def test_the_floor_is_the_first_and_only_error(
    backend, present, monkeypatch, tmp_path, caplog
):
    # The mask cannot be applied on this host: the floor refuses every start.
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    expected = tool_gate.sandbox_floor_refusal(backend, "auto")
    assert expected, f"{backend} is not refused by the floor; the premise is wrong"
    lookups: list[str] = []
    _binary_lookups(monkeypatch, lookups, present=present)
    # ``tmp_path`` is its own repo, so the Copilot config walk stops there.
    (tmp_path / ".git").mkdir()

    adapter = process_adapter_for(backend)
    real_resolve = adapter.resolve_spawn
    resolve_calls: list[str] = []

    async def _counted(ctx):
        resolve_calls.append(backend)
        return await real_resolve(ctx)

    monkeypatch.setattr(adapter, "resolve_spawn", _counted)
    client = AcpClient(work_dir=tmp_path, acp_backend=backend, sandbox_mode="auto")
    client._prepare_spawn_workspace = lambda: None
    client._cleanup_failed_live_spawn = AsyncMock()
    monkeypatch.setattr(client, "_prepare_session_mcp", AsyncMock())
    caplog.set_level(logging.WARNING)

    with (
        patch("kiro_crew.acp.client.process_adapter_for", return_value=adapter),
        patch("kiro_crew.acp.client._ACP_RESPAWN_BACKOFF_S", 0),
        patch("kiro_crew.acp.client.assert_voice_runtime_outside_agent_workspace"),
    ):
        with pytest.raises(AcpToolGateUnroutable) as raised:
            await client.ensure_ready()

    assert str(raised.value) == expected
    assert resolve_calls == [backend], "the refusal was retried with a fresh process"
    assert lookups == [], f"{backend} looked up its binary before the floor refused"
    assert not [r for r in caplog.records if "retrying with fresh process" in r.getMessage()]

"""Copilot CLI's long-context tier is a launch argument, offered as ``<id>[1m]`` picks.

Copilot CLI serves a larger context window for eligible models only when the process
starts with ``--context long_context``; ``session/new`` advertises bare ids and no ACP
request moves the tier. The picker offers a ``[1m]`` sibling for each advertised
model, the spawn reads the suffix into the flag, the wire gets the bare id, and a
switch across tiers relaunches and reloads the session.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp import launch_tuning
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import copilot as copilot_mod
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_COPILOT,
    ACP_BACKENDS_CONTEXT_TIER_AT_LAUNCH,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
    LONG_CONTEXT_MODEL_SUFFIX,
    long_context_pick,
    with_long_context_variants,
)

COPILOT = ACP_BACKEND_COPILOT


def _row(model_id: str, name: str = "") -> dict:
    return {"modelId": model_id, "name": name or model_id, "description": "d"}


def test_copilot_takes_its_tier_at_launch_but_its_model_on_the_wire() -> None:
    assert COPILOT in ACP_BACKENDS_CONTEXT_TIER_AT_LAUNCH
    assert COPILOT not in ACP_BACKENDS_MODEL_AT_LAUNCH
    assert acp_backends.ACP_BACKENDS_CONTEXT_TIER_AT_LAUNCH is ACP_BACKENDS_CONTEXT_TIER_AT_LAUNCH


def test_each_advertised_model_gains_a_long_context_sibling() -> None:
    rows = with_long_context_variants(
        COPILOT, [_row("auto", "Auto"), _row("model-a", "Model A"), _row("model-b")]
    )
    assert [r["modelId"] for r in rows] == [
        "auto",
        "model-a",
        "model-a" + LONG_CONTEXT_MODEL_SUFFIX,
        "model-b",
        "model-b[1m]",
    ]
    assert rows[2]["name"] == "Model A (long context)"
    assert rows[2]["description"] == "d"


def test_synthesis_is_idempotent() -> None:
    once = with_long_context_variants(COPILOT, [_row("model-a")])
    assert with_long_context_variants(COPILOT, once) == once


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_no_other_backend_gains_variants_or_strips_a_suffix(backend: str) -> None:
    rows = [_row("model-a")]
    assert with_long_context_variants(backend, rows) == rows
    assert long_context_pick(backend, "model-a[1m]") == ("model-a[1m]", False)


def test_the_pick_splits_into_wire_id_and_tier() -> None:
    assert long_context_pick(COPILOT, "model-a[1m]") == ("model-a", True)
    assert long_context_pick(COPILOT, "model-a") == ("model-a", False)
    assert long_context_pick(COPILOT, "") == ("", False)


async def _spawn_argv(tmp_path, monkeypatch, model: str) -> tuple[list[str], AcpClient]:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT, model=model)

    async def _resolve(_backend):
        return ("copilot", ["copilot", "--acp"], "copilot", "copilot")

    async def _prepare() -> None:
        return None

    async def _preflight(*_a, **_k):
        return ()

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _resolve)
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
    client._prepare_session_mcp = _prepare  # type: ignore[method-assign]
    client._session_mcp_servers = lambda: []  # type: ignore[method-assign]
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "work_dir", tmp_path)
    object.__setattr__(ctx, "sandbox_mode", "auto")
    object.__setattr__(ctx, "model", None)
    plan = await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    return list(plan.argv), client


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the spawn checks POSIX config paths")
async def test_a_long_context_pick_launches_on_the_long_tier(tmp_path, monkeypatch) -> None:
    argv, client = await _spawn_argv(tmp_path, monkeypatch, "model-a[1m]")
    assert argv[-2:] == ["--context", "long_context"]
    assert client._launched_long_context is True


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the spawn checks POSIX config paths")
async def test_a_plain_pick_launches_on_the_default_tier(tmp_path, monkeypatch) -> None:
    argv, client = await _spawn_argv(tmp_path, monkeypatch, "model-a")
    assert "--context" not in argv
    assert client._launched_long_context is False


def _live_client(tmp_path, *, launched_long: bool) -> tuple[AcpClient, list]:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._session_id = "sess-1"
    client._launched_long_context = launched_long
    sent: list = []

    async def _send(method: str, params: dict) -> int:
        sent.append((method, params))
        return 1

    client._send_request = _send  # type: ignore[method-assign]
    return client, sent


@pytest.mark.asyncio
async def test_the_wire_gets_the_bare_id_and_the_pick_keeps_its_tier(tmp_path) -> None:
    client, sent = _live_client(tmp_path, launched_long=True)
    await client.set_model("model-a[1m]")
    assert sent == [("session/set_model", {"sessionId": "sess-1", "modelId": "model-a"})]
    assert client._model == "model-a[1m]"


@pytest.mark.asyncio
async def test_the_startup_apply_sends_the_bare_id(tmp_path) -> None:
    client, sent = _live_client(tmp_path, launched_long=True)
    client._model = "model-a[1m]"
    client._available_models = with_long_context_variants(COPILOT, [_row("model-a")])
    await client._apply_startup_model()
    assert ("session/set_model", {"sessionId": "sess-1", "modelId": "model-a"}) in sent
    assert client._model == "model-a[1m]"


@pytest.mark.asyncio
@pytest.mark.parametrize(("launched_long", "pick"), [(False, "model-a[1m]"), (True, "model-a")])
async def test_a_switch_across_tiers_relaunches(tmp_path, monkeypatch, launched_long, pick) -> None:
    client, sent = _live_client(tmp_path, launched_long=launched_long)
    relaunch = AsyncMock()
    monkeypatch.setattr(launch_tuning, "switch_launch_model", relaunch)
    await client.set_model(pick)
    relaunch.assert_awaited_once_with(client, pick)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("launched_long", "pick"), [(False, "model-b"), (True, "model-b[1m]")])
async def test_a_switch_within_a_tier_stays_on_the_wire(
    tmp_path, monkeypatch, launched_long, pick
) -> None:
    client, sent = _live_client(tmp_path, launched_long=launched_long)
    relaunch = AsyncMock()
    monkeypatch.setattr(launch_tuning, "switch_launch_model", relaunch)
    await client.set_model(pick)
    relaunch.assert_not_awaited()
    assert [params["modelId"] for _method, params in sent] == ["model-b"]

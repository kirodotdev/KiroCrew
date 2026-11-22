"""Copilot CLI's long-context tier is a launch argument, offered as ``<id>[1m]`` picks.

Copilot CLI serves a larger context window for eligible models only when the process
starts with ``--context long_context``; ``session/new`` advertises bare ids and no ACP
request moves the tier. The picker offers a ``[1m]`` sibling for each advertised
model, the spawn reads the suffix into the flag, the wire gets the bare id, and a
switch across tiers relaunches and reloads the session.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import acp_backends, model_registry, model_scope
from kiro_crew.acp import launch_tuning
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import copilot as copilot_mod
from kiro_crew.acp.transport_errors import AcpError, AcpModelUnavailable
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


async def _spawn_argv(
    tmp_path, monkeypatch, model: str, client: AcpClient | None = None
) -> tuple[list[str], AcpClient]:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = client or AcpClient(work_dir=tmp_path, acp_backend=COPILOT, model=model)

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
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    return client, sent


@pytest.mark.asyncio
async def test_the_wire_gets_the_bare_id_and_the_pick_keeps_its_tier(tmp_path) -> None:
    client, sent = _live_client(tmp_path, launched_long=True)
    await client.set_model("model-a[1m]")
    assert sent == [
        (
            "session/set_config_option",
            {"sessionId": "sess-1", "configId": "model", "value": "model-a"},
        )
    ]
    assert client._model == "model-a[1m]"


@pytest.mark.asyncio
async def test_the_startup_apply_sends_the_bare_id(tmp_path) -> None:
    client, sent = _live_client(tmp_path, launched_long=True)
    client._model = "model-a[1m]"
    client._available_models = with_long_context_variants(COPILOT, [_row("model-a")])
    await client._apply_startup_model()
    assert (
        "session/set_config_option",
        {"sessionId": "sess-1", "configId": "model", "value": "model-a"},
    ) in sent
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
    assert sent == [
        (
            "session/set_config_option",
            {"sessionId": "sess-1", "configId": "model", "value": "model-b"},
        )
    ]
    assert client._model == pick


def _capture(client: AcpClient, model: str = "claude-sonnet-4.5") -> None:
    client._capture_available_models(
        {"models": {"currentModelId": model, "availableModels": [_row(model)]}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised", "pick"),
    [
        ("claude-sonnet-4.5", "CLAUDE-SONNET-4.5"),
        ("claude-sonnet-4.5", "claude-sonnet-4-5"),
        ("claude-sonnet-4-5", "CLAUDE-SONNET-4-5"),
        ("claude-sonnet-4-5", "claude-sonnet-4.5"),
    ],
)
@pytest.mark.parametrize("suffix", ["", "[1m]", "[1M]"])
@pytest.mark.parametrize("startup", [False, True])
async def test_spelling_fold_preserves_requested_tier(
    tmp_path, advertised, pick, suffix, startup
) -> None:
    client, sent = _live_client(tmp_path, launched_long=bool(suffix))
    _capture(client, advertised)
    expected = advertised + ("[1m]" if suffix else "")
    if startup:
        client._model = pick + suffix
        await client._apply_startup_model()
    else:
        await client.set_model(pick + suffix)
    assert client._model == expected
    assert client._resolved_model_id == expected
    assert sent[-1][1]["value"] == advertised
    assert long_context_pick(COPILOT, client._model)[1] is bool(suffix)


@pytest.mark.parametrize("long", [False, True])
def test_capture_attributes_the_served_model_to_the_launched_tier(tmp_path, long) -> None:
    client, _ = _live_client(tmp_path, launched_long=long)
    _capture(client)
    assert client._resolved_model_id == "claude-sonnet-4.5" + ("[1m]" if long else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("pick", ["claude-sonnet-4.5", "CLAUDE-SONNET-4-5"])
async def test_real_cross_tier_switch_accepts_shared_catalog_ids(
    tmp_path, monkeypatch, long, pick
) -> None:
    client, sent = _live_client(tmp_path, launched_long=not long)
    client._model = "claude-sonnet-4.5" + ("" if long else "[1m]")
    client._turn_done.set()
    _capture(client)
    model_registry.refresh_advertised_models("acp", ["claude-sonnet-4.5"])
    assert model_scope.foreign_namespaces(pick, "copilot")
    launches = []

    async def ready():
        assert client._resume_session_id == "sess-1"
        argv, _ = await _spawn_argv(tmp_path, monkeypatch, client._model, client)
        launches.append(argv)
        client._stdin_stalled = False
        client._resumed = True
        _capture(client)
        await client._apply_startup_model()

    monkeypatch.setattr(client, "ensure_ready", ready)
    monkeypatch.setattr(client, "reclaim", Mock())
    await client.set_model(pick + ("[1m]" if long else ""))
    assert len(launches) == 1
    assert ("--context" in launches[0]) is long
    assert client._session_id == "sess-1"
    expected = "claude-sonnet-4.5" + ("[1m]" if long else "")
    assert client._model == client._resolved_model_id == expected
    assert client._launched_long_context is long
    assert client._launch_model_explicit is False
    assert sent[-1][1]["value"] == "claude-sonnet-4.5"
    expected_window = (
        model_registry.model_window(expected) if model_registry.has_known_window(expected) else 0
    )
    assert client.last_prompt_stats.context_window_tokens == expected_window


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_tier", [False, True])
async def test_explicit_refusal_preserves_the_previous_pick(tmp_path, monkeypatch, cross_tier):
    client, _ = _live_client(tmp_path, launched_long=False)
    client._model = "model-a"
    client._turn_done.set()
    _capture(client)
    client._wait_for_response = AsyncMock(side_effect=AcpError("Invalid params", code=-32602))

    async def ready():
        client._launched_long_context = True
        client._resumed = True
        _capture(client)
        await client._apply_startup_model()

    monkeypatch.setattr(client, "ensure_ready", ready)
    monkeypatch.setattr(client, "reclaim", Mock())
    with pytest.raises(AcpModelUnavailable):
        await client.set_model("unlisted-model" + ("[1m]" if cross_tier else ""))
    assert client._model == "model-a"
    assert client._launch_model_explicit is False
    if cross_tier:
        assert client._relaunch_owed_session == "sess-1"
        assert client._stdin_stalled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("long", [False, True])
async def test_cross_tier_model_refusal_is_not_retried(tmp_path, monkeypatch, long):
    client, sent = _live_client(tmp_path, launched_long=not long)
    prior = "model-a" + ("" if long else "[1m]")
    client._model = prior
    client._turn_done.set()
    client._work_dir_ready = True
    client._wait_for_response = AsyncMock(side_effect=AcpError("Invalid params", code=-32602))

    async def spawn():
        client._process = Mock(returncode=None)
        client._launched_long_context = long

    async def initialize():
        client._session_id = client._resume_session_id or "throwaway-session"
        client._resume_session_id = None
        client._resumed = True
        _capture(client)
        await client._apply_startup_model()

    async def kill(*, force):
        if client._process is not None:
            client._process.returncode = -1

    spawn_call = AsyncMock(side_effect=spawn)
    initialize_call = AsyncMock(side_effect=initialize)
    cleanup = AsyncMock(wraps=client._cleanup_failed_live_spawn)
    recorder = Mock()
    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
    monkeypatch.setattr(client, "_spawn", spawn_call)
    monkeypatch.setattr(client, "_initialize_session", initialize_call)
    monkeypatch.setattr(client, "_kill_process", kill)
    monkeypatch.setattr(client, "_discard_bound_workspace", AsyncMock())
    monkeypatch.setattr(client, "_discard_settings_seed", AsyncMock())
    monkeypatch.setattr(client, "_cleanup_failed_live_spawn", cleanup)

    with pytest.raises(AcpModelUnavailable):
        await client.set_model("unlisted-model" + ("[1m]" if long else ""))

    spawn_call.assert_awaited_once()
    initialize_call.assert_awaited_once()
    cleanup.assert_awaited_once()
    assert recorder.histogram.call_args.kwargs["attrs"] == {
        "outcome": "model_unavailable",
        "spawned": True,
    }
    assert len(sent) == 1
    assert client._process is None
    assert client._model == prior
    assert client._launched_long_context is not long
    assert client._launch_model_explicit is False
    assert (
        client._session_id == client._resume_session_id == client._relaunch_owed_session == "sess-1"
    )
    assert client._stdin_stalled is True


@pytest.mark.parametrize("suffix", ["", "[1m]"])
def test_fallback_spelling_preserves_requested_tier(tmp_path, suffix):
    from kiro_crew.acp.runtime_models import resolve_pin_spelling_on

    client, _ = _live_client(tmp_path, launched_long=False)
    _capture(client)
    assert (
        resolve_pin_spelling_on(
            "claude-sonnet-4-5" + suffix, client._advertised_model_ids(), backend=COPILOT
        )
        == "claude-sonnet-4.5" + suffix
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "[1m]"])
async def test_an_inherited_refusal_keeps_the_served_default(tmp_path, suffix):
    client, _ = _live_client(tmp_path, launched_long=bool(suffix))
    _capture(client)
    client._model = "unlisted-model" + suffix
    client._wait_for_response = AsyncMock(side_effect=AcpError("Invalid params", code=-32602))
    await client._apply_startup_model()
    assert client._model == "auto"
    assert client.model_pin_refused == "unlisted-model" + suffix
    assert client._resolved_model_id == "claude-sonnet-4.5" + suffix


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "cancel", "different-session"])
async def test_failed_tier_relaunch_restores_the_pick_and_owes_the_chat(
    tmp_path, monkeypatch, failure
):
    client, _ = _live_client(tmp_path, launched_long=False)
    client._model = "model-a"
    client._turn_done.set()

    async def ready():
        assert client._launch_model_explicit is True
        # resolve_spawn records the target tier before the start can fail.
        client._launched_long_context = True
        if failure == "transport":
            raise AcpError("transport failed")
        if failure == "cancel":
            raise asyncio.CancelledError()
        client._session_id = "different-session"
        client._resumed = False

    monkeypatch.setattr(client, "ensure_ready", ready)
    error = asyncio.CancelledError if failure == "cancel" else AcpError
    with pytest.raises(error):
        await client.set_model("model-a[1m]")
    assert client._model == "model-a"
    # The tier goes back with the pick, so retrying the same pick relaunches again.
    assert client._launched_long_context is False
    assert client._launch_model_explicit is False
    assert client._session_id == client._relaunch_owed_session == "sess-1"
    assert client._stdin_stalled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("pick", ["", "auto"])
async def test_default_startup_does_not_choose_a_long_row(tmp_path, pick):
    client, sent = _live_client(tmp_path, launched_long=False)
    _capture(client)
    client._model = pick
    await client._apply_startup_model()
    assert client._model == pick
    assert client._resolved_model_id == "claude-sonnet-4.5"
    assert sent == []


@pytest.mark.parametrize("suffix", ["", "[1m]"])
def test_fallback_does_not_substitute_an_opposite_tier(tmp_path, suffix):
    from kiro_crew.acp.runtime_models import resolve_pin_spelling_on

    opposite = "" if suffix else "[1m]"
    assert (
        resolve_pin_spelling_on(
            "claude-sonnet-4-5" + suffix, ["claude-sonnet-4.5" + opposite], backend=COPILOT
        )
        == ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize(
    "failure",
    ["transport", "different-session", "owed-session", "none", "same-tier", "same-tier-error"],
)
async def test_dashboard_tier_switch_preserves_the_chat(tmp_path, monkeypatch, long, failure):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.providers.acp import AcpProvider

    prior = "model-a" + ("[1m]" if long else "")
    same_tier = failure.startswith("same-tier")
    target = "model-b" + ("[1m]" if (long if same_tier else not long) else "")
    acp, sent = _live_client(tmp_path, launched_long=long)
    acp._model = acp._resolved_model_id = prior
    acp._turn_done.set()
    if failure == "owed-session":
        acp._relaunch_owed_session = "owed-chat"
    launches = []

    async def ready():
        launches.append(acp._model)
        if failure == "transport":
            raise AcpError("transport failed")
        acp._launched_long_context = not long
        acp._resumed = failure != "different-session"
        if not acp._resumed:
            acp._session_id = "different-chat"
        else:
            acp._stdin_stalled = False
            await acp._apply_startup_model()

    monkeypatch.setattr(acp, "ensure_ready", ready)
    monkeypatch.setattr(acp, "reclaim", Mock())
    if failure == "same-tier-error":
        acp._wait_for_response = AsyncMock(side_effect=AcpError("wire failed"))
    provider = AcpProvider(work_dir=tmp_path, model=prior, acp_backend=COPILOT)
    provider._client = acp
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = COPILOT
    monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", lambda: cfg)
    state = _make_state(tmp_path)
    state.sessions.get_provider = Mock(return_value=provider)
    state.sessions.reset = AsyncMock()
    slot = state.get_or_create_slot("tier", model=prior)
    generation = slot._model_pick_gen
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/model", chat_handlers.api_chat_slot_model)
    async with TestClient(TestServer(app, host="127.0.0.1")) as http:
        response = await http.post("/api/chat/slots/tier/model", json={"model": target})
        data = await response.json()

    if failure in {"transport", "different-session", "owed-session"}:
        assert response.status == 400, data
        assert data["code"] == "model_switch_refused"
        assert slot.model == acp._model == prior
        assert slot._model_pick_gen == generation
        assert acp._session_id == "sess-1"
        assert acp._relaunch_owed_session == (
            "owed-chat" if failure == "owed-session" else "sess-1"
        )
        if failure != "owed-session":
            assert acp._stdin_stalled is True
        state.sessions.reset.assert_not_awaited()
    else:
        assert response.status == 200, data
        assert slot.model == target
        assert slot._model_pick_gen == generation + 1
        if failure == "same-tier-error":
            state.sessions.reset.assert_awaited_once()
        else:
            assert acp._model == target
            assert acp._session_id == "sess-1"
            state.sessions.reset.assert_not_awaited()
        assert sent[-1][1]["value"] == "model-b"
    assert launches == ([] if same_tier or failure == "owed-session" else [target])

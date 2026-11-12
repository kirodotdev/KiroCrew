"""``AcpProvider.set_model``: switch the model, then re-apply the slot's effort.

The fallback walk and the sticky-state restore in ``llm_helpers`` reach this
method through ``resolve_substitute_set_model``, which prefers it over the
wrapped client. Before it existed they called the client directly and wrote no
effort, while the dashboard's interactive switch re-applied it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    effort_config_option_id,
    effort_config_option_value,
)
from kiro_crew.dashboard.chat_turn.jev_route_effort import (
    _JEV_ROUTE_EFFORT_ATTR,
    _apply_jev_route_effort,
    _restore_jev_route_effort,
)
from kiro_crew.decisions.points import model_route
from kiro_crew.llm_helpers import pick_epoch_host, resolve_substitute_set_model
from kiro_crew.providers.acp import AcpProvider

#: Per config-option backend: (model left, model switched to, whether the new
#: model takes an effort write). deepseek, goose, opencode and pi answer from the option
#: their session advertised rather than the registry, so their fallback writes it too.
BACKEND_CASES = {
    ACP_BACKEND_CLAUDE: ("claude-opus-4.7", "claude-sonnet-4.6", True),
    ACP_BACKEND_CODEX: ("openai.gpt-6-astra", "openai.gpt-5.5-codex", True),
    ACP_BACKEND_DEEPSEEK: ("deepseek-3.2", "deepseek-v4", True),
    ACP_BACKEND_GOOSE: ("goose-model-a", "goose-model-b", True),
    ACP_BACKEND_KAS: ("claude-opus-5", "claude-opus-5.5", True),
    ACP_BACKEND_OPENCODE: ("stub/think-1", "stub/think-2", True),
    ACP_BACKEND_PI: ("pi-model-a", "pi-model-b", True),
}


def _provider(backend: str, model: str, applied: list[tuple[str, str]]) -> AcpProvider:
    """A provider whose client records model and config-option writes in order."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    client = MagicMock()
    client.backend = backend
    client._model = model
    client._work_dir = MagicMock()
    client.send_command = AsyncMock()
    client.supports_config_option = MagicMock(return_value=True)
    client.get_valid_effort_levels = MagicMock(return_value=["low", "medium", "high"])

    async def _set_model(wire: str) -> None:
        applied.append(("model", wire))
        client._model = wire

    async def _set_option(config_id: str, value: str) -> None:
        applied.append((config_id, value))

    client.set_model = _set_model
    client.set_config_option = _set_option
    provider._client = client
    return provider


def test_every_config_option_backend_has_a_case() -> None:
    assert set(BACKEND_CASES) == set(ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION)


def test_the_fallback_walk_resolves_the_provider_method() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", [])
    assert resolve_substitute_set_model(provider) == provider.set_model


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(BACKEND_CASES))
async def test_set_model_delegates_then_reapplies_the_slot_level(backend: str) -> None:
    before, after, takes_effort = BACKEND_CASES[backend]
    applied: list[tuple[str, str]] = []
    provider = _provider(backend, before, applied)
    provider._effort_per_model[before] = "high"

    await provider.set_model(after)

    effort_write = (effort_config_option_id(backend), effort_config_option_value(backend, "high"))
    if takes_effort:
        # Slot-last: the model push lands first, the effort write after it.
        assert applied == [("model", after), effort_write]
    else:
        assert applied == [("model", after)]
    # Pushed live only: the target model's stored state is untouched.
    assert after not in provider._effort_per_model


@pytest.mark.asyncio
@pytest.mark.parametrize("left", ["", "low"])
async def test_the_target_models_own_override_wins_and_survives(left: str) -> None:
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied)
    if left:
        provider._effort_per_model["claude-opus-4.7"] = left
    provider._effort_per_model["claude-sonnet-4.6"] = "high"

    with patch.object(provider, "clear_effort", wraps=provider.clear_effort) as clear:
        await provider.set_model("claude-sonnet-4.6")

    clear.assert_not_awaited()
    assert applied == [
        ("model", "claude-sonnet-4.6"),
        (effort_config_option_id(ACP_BACKEND_CLAUDE), "high"),
    ]
    assert provider._effort_per_model["claude-sonnet-4.6"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(BACKEND_CASES))
async def test_set_model_clears_effort_when_no_slot_level_is_set(backend: str) -> None:
    before, after, _ = BACKEND_CASES[backend]
    applied: list[tuple[str, str]] = []
    provider = _provider(backend, before, applied)

    with patch.object(provider, "clear_effort", wraps=provider.clear_effort) as clear:
        await provider.set_model(after)

    assert applied == [("model", after)]
    if BACKEND_CASES[backend][2]:
        clear.assert_awaited_once()
    else:
        clear.assert_not_awaited()
    assert after not in provider._effort_per_model


@pytest.mark.asyncio
async def test_a_hop_through_an_effortless_model_keeps_the_override_on_restore() -> None:
    """Fallback onto an effort-less model, then the restore back to the primary."""
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied)
    provider._effort_per_model["claude-opus-4.7"] = "high"

    await provider.set_model("deepseek-v4")
    await provider.set_model("claude-opus-4.7")

    assert applied == [
        ("model", "deepseek-v4"),
        ("model", "claude-opus-4.7"),
        (effort_config_option_id(ACP_BACKEND_CLAUDE), "high"),
    ]
    assert provider._effort_per_model["claude-opus-4.7"] == "high"


@pytest.mark.asyncio
async def test_a_failed_effort_reapply_does_not_undo_the_switch() -> None:
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied)
    provider._effort_per_model["claude-opus-4.7"] = "high"
    provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("process died"))

    await provider.set_model("claude-sonnet-4.6")

    assert applied == [("model", "claude-sonnet-4.6")]
    assert provider._client._model == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_set_model_pushes_nothing_when_the_targets_vocabulary_hides_the_carried_level() -> (
    None
):
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_GOOSE, "goose-model-a", applied)
    provider._effort_per_model["goose-model-a"] = "xhigh"
    assert provider.accepts_effort_level("xhigh") is False, "the vocabulary must hide this level"

    await provider.set_model("goose-model-b")

    assert applied == [("model", "goose-model-b")]
    assert "goose-model-b" not in provider._effort_per_model


def test_owner_effort_answers_for_the_model_asked_about_not_the_live_one() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-sonnet-4.6", [])
    provider._effort_per_model["claude-opus-4.7"] = "high"

    assert provider.owner_effort("claude-opus-4.7") == "high"
    assert not provider.owner_effort("claude-sonnet-4.6"), "the live model has no level stored"


@pytest.mark.asyncio
async def test_an_effort_the_owner_sets_by_hand_drops_the_routed_efforts_restore_mark() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-sonnet-4.6", [])
    _persistence_spy(provider)
    host = pick_epoch_host(provider)
    setattr(host, _JEV_ROUTE_EFFORT_ATTR, "medium")

    assert await provider.change_effort("high") is True

    assert not getattr(host, _JEV_ROUTE_EFFORT_ATTR, None), "the turn end must not undo the pick"


def _kiro_provider(*, default: str, applied: list[tuple[str, str]]) -> AcpProvider:
    """A kiro provider on a model that takes effort, with or without a workspace default."""
    provider = _provider(ACP_BACKEND_KIRO, "claude-opus-4.7", applied)
    provider._effort_defaults = {"claude-opus-4.7": default} if default else {}
    provider._effort_per_model["claude-opus-4.7"] = "low"
    _persistence_spy(provider)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "default", "mark_kept"),
    [
        (ACP_BACKEND_CLAUDE, "", True),
        (ACP_BACKEND_KIRO, "", True),
        (ACP_BACKEND_KIRO, "high", False),
    ],
    ids=["claude", "kiro-no-default", "kiro-default-pushed"],
)
async def test_clearing_the_effort_drops_the_restore_mark_only_when_a_default_lands_live(
    backend: str, default: str, mark_kept: bool
) -> None:
    if backend == ACP_BACKEND_KIRO:
        provider = _kiro_provider(default=default, applied=[])
    else:
        provider = _provider(backend, "claude-sonnet-4.6", [])
        provider._effort_per_model["claude-sonnet-4.6"] = "low"
        _persistence_spy(provider)
    host = pick_epoch_host(provider)
    setattr(host, _JEV_ROUTE_EFFORT_ATTR, "medium")

    with patch("kiro_crew.providers.acp_model_effort._clear_cli_overlay_effort", return_value=True):
        assert await provider.clear_effort() is bool(default)

    assert bool(getattr(host, _JEV_ROUTE_EFFORT_ATTR, None)) is mark_kept


@pytest.mark.asyncio
async def test_an_automated_switch_onto_a_model_with_no_stored_level_keeps_the_restore_mark() -> (
    None
):
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", [])
    _persistence_spy(provider)
    host = pick_epoch_host(provider)
    setattr(host, _JEV_ROUTE_EFFORT_ATTR, "medium")

    await provider.set_model("claude-sonnet-4.6")

    assert getattr(host, _JEV_ROUTE_EFFORT_ATTR, None) == "medium"


@pytest.mark.asyncio
async def test_an_effort_change_whose_push_fails_keeps_the_restore_mark() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-sonnet-4.6", [])
    _persistence_spy(provider)
    provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("process died"))
    host = pick_epoch_host(provider)
    setattr(host, _JEV_ROUTE_EFFORT_ATTR, "medium")

    with pytest.raises(RuntimeError):
        await provider.change_effort("high")

    assert getattr(host, _JEV_ROUTE_EFFORT_ATTR, None) == "medium"


def test_owner_effort_defaults_to_the_live_model() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-sonnet-4.6", [])
    provider._effort_per_model["claude-sonnet-4.6"] = "low"
    provider._effort_per_model["claude-opus-4.7"] = "high"

    assert provider.owner_effort() == "low"


def test_owner_effort_of_no_model_named_is_not_the_live_models() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-sonnet-4.6", [])
    provider._effort_per_model["claude-sonnet-4.6"] = "low"

    assert provider.owner_effort("") is None
    assert provider.owner_effort() == "low"


@pytest.mark.asyncio
async def test_an_unknown_baseline_model_pushes_no_routed_effort_and_marks_nothing() -> None:
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-sonnet-4.6", [])
    provider._effort_per_model["claude-sonnet-4.6"] = "low"
    provider.push_live_effort = AsyncMock(return_value=True)
    host = pick_epoch_host(provider)
    slot = SimpleNamespace(
        key="k", jev_route=True, jev_range={"models": [], "effort_min": "", "effort_max": "high"}
    )
    routed = {"tier": "complex"}

    await _apply_jev_route_effort(slot, provider, "k", routed, model_route, "")

    assert routed == {"tier": "complex", "effort_skipped": "baseline-unknown"}
    provider.push_live_effort.assert_not_awaited()
    assert not getattr(host, _JEV_ROUTE_EFFORT_ATTR, None)


@pytest.mark.asyncio
async def test_the_restore_after_an_automated_switch_pushes_the_captured_level_and_clears_it() -> (
    None
):
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied)
    _persistence_spy(provider)
    host = pick_epoch_host(provider)
    setattr(host, _JEV_ROUTE_EFFORT_ATTR, "medium")
    await provider.set_model("claude-sonnet-4.6")
    applied.clear()

    await _restore_jev_route_effort(SimpleNamespace(key="k"), provider)

    assert applied == [
        (
            effort_config_option_id(ACP_BACKEND_CLAUDE),
            effort_config_option_value(ACP_BACKEND_CLAUDE, "medium"),
        )
    ]
    assert not getattr(host, _JEV_ROUTE_EFFORT_ATTR, None)


# ── push_live_effort: a level sent to the running session and stored nowhere ──


def _persistence_spy(provider: AcpProvider) -> MagicMock:
    provider._apply_effort_overlay = MagicMock(return_value=True)
    return provider._apply_effort_overlay


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(BACKEND_CASES))
async def test_push_live_effort_sends_the_level_without_persisting_it(backend: str) -> None:
    before, _, _ = BACKEND_CASES[backend]
    applied: list[tuple[str, str]] = []
    provider = _provider(backend, before, applied)
    overlay = _persistence_spy(provider)
    stored = dict(provider._effort_per_model)

    assert await provider.push_live_effort("high") is True

    assert applied == [
        (effort_config_option_id(backend), effort_config_option_value(backend, "high"))
    ]
    assert provider._effort_per_model == stored
    overlay.assert_not_called()


@pytest.mark.asyncio
async def test_push_live_effort_uses_the_slash_command_on_kiro(monkeypatch) -> None:
    provider = _provider(ACP_BACKEND_KIRO, "kiro-model", [])
    monkeypatch.setattr("kiro_crew.providers.acp.model_supports_effort", lambda _m: True)
    overlay = _persistence_spy(provider)

    assert await provider.push_live_effort("high") is True

    provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "high"})
    assert provider._effort_per_model == {}
    overlay.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "option_offered"),
    [("deepseek-v4", True), ("claude-opus-4.7", False)],
    ids=["model-takes-no-effort", "adapter-exposes-no-option"],
)
async def test_push_live_effort_sends_nothing_without_an_effort_channel(
    model: str, option_offered: bool
) -> None:
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_CLAUDE, model, applied)
    provider._client.supports_config_option = MagicMock(return_value=option_offered)

    assert await provider.push_live_effort("high") is False

    assert applied == []
    provider._client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_live_effort_refuses_a_level_the_sessions_vocabulary_hides() -> None:
    applied: list[tuple[str, str]] = []
    provider = _provider(ACP_BACKEND_GOOSE, "goose-model-a", applied)
    overlay = _persistence_spy(provider)
    assert provider.accepts_effort_level("xhigh") is False, "the vocabulary must hide this level"

    assert await provider.push_live_effort("xhigh") is False

    assert applied == []
    provider._client.send_command.assert_not_awaited()
    overlay.assert_not_called()


# ── codex: a fallback onto a ``<model>[<effort>]`` candidate ──

CODEX_EFFORT = effort_config_option_id(ACP_BACKEND_CODEX)
CODEX_SESSION_NEW = {
    "sessionId": "codex-sess",
    "models": {
        "currentModelId": "openai.gpt-6-astra[high]",
        "availableModels": [
            {"modelId": "openai.gpt-6-astra[high]", "name": "high"},
            {"modelId": "openai.gpt-6-astra[max]", "name": "max"},
        ],
    },
    "configOptions": [
        {
            "id": "model",
            "type": "select",
            "currentValue": "openai.gpt-6-astra",
            "options": [{"value": "openai.gpt-6-astra", "name": "GPT-6 Astra"}],
        },
        {
            "id": CODEX_EFFORT,
            "type": "select",
            "currentValue": "high",
            "options": [{"value": v, "name": v} for v in ("high", "xhigh", "max")],
        },
    ],
}


@pytest.mark.asyncio
async def test_codex_fallback_onto_a_suffixed_pair_ends_on_the_slot_level(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client._session_id = "codex-sess"
    client._model = "openai.gpt-6-astra[high]"
    client._capture_available_models(CODEX_SESSION_NEW)
    client._acp_config_options = CODEX_SESSION_NEW["configOptions"]
    applied: list[tuple[str, str]] = []

    async def _set_option(config_id: str, value: str) -> None:
        applied.append((config_id, value))
        if config_id == "model" and "[" in value:
            raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    client.set_config_option = _set_option  # type: ignore[method-assign]
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
    provider._client = client
    provider._effort_per_model["openai.gpt-6-astra[high]"] = "xhigh"

    await provider.set_model("openai.gpt-6-astra[max]")

    # The pair's own [max] is written first; the slot's xhigh lands last.
    assert (CODEX_EFFORT, "max") in applied
    assert applied[-1] == (CODEX_EFFORT, "xhigh")

"""``AcpProvider.set_model``: switch the model, then re-apply the slot's effort.

The fallback walk and the sticky-state restore in ``llm_helpers`` reach this
method through ``resolve_substitute_set_model``, which prefers it over the
wrapped client. Before it existed they called the client directly and wrote no
effort, while the dashboard's interactive switch re-applied it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_AGY,
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_PI,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    effort_config_option_id,
    effort_config_option_value,
)
from kiro_crew.llm_helpers import resolve_substitute_set_model
from kiro_crew.providers.acp import AcpProvider

#: Per config-option backend: (model left, model switched to, whether the new
#: model takes an effort write). deepseek's models carry no effort selector in
#: the registry, so its fallback stays write-free exactly as before.
BACKEND_CASES = {
    ACP_BACKEND_AGY: ("agy-model-1", "agy-model-2", False),
    ACP_BACKEND_CLAUDE: ("claude-opus-4.7", "claude-sonnet-4.6", True),
    ACP_BACKEND_CODEX: ("openai.gpt-6-astra", "openai.gpt-5.5-codex", True),
    ACP_BACKEND_DEEPSEEK: ("deepseek-3.2", "deepseek-v4", False),
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

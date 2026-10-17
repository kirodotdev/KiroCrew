"""KAS takes reasoning effort as its ``effortLevel`` session config option.

KAS (``kiro-cli acp --agent-engine v3``) reads none of the cli.json
``chat.modelDefaults`` overlay the kiro family spawns with, and answers the kiro
``/effort`` slash command (``_kiro.dev/commands/execute``) with -32603. Its only
effort channel is ``session/set_config_option`` on ``effortLevel``, a select it
serves once the session has a concrete model -- the ``model`` write's own result
carries it. Without that channel every KAS session ran at the model's built-in
default (``medium`` for claude-opus-5.5) whatever Crew had configured.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    JsonRpcMessage,
    effort_config_option_id,
)
from kiro_crew.providers.acp import AcpProvider

MODEL = "claude-opus-5.5"
LEVELS = ["low", "medium", "high", "xhigh", "max"]

#: The options KAS serves after ``session/set_config_option model=claude-opus-5.5``
#: (kiro-cli 2.28.0), trimmed to the two this path reads.
KAS_OPTIONS_WITH_MODEL = [
    {"type": "select", "id": "model", "currentValue": MODEL, "options": []},
    {
        "type": "select",
        "id": "effortLevel",
        "category": "thought_level",
        "currentValue": "medium",
        "options": [{"value": v, "name": v} for v in LEVELS],
    },
]


def _kas_provider(*, override: str = "", defaults: dict[str, str] | None = None) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
    client = MagicMock()
    client.backend = ACP_BACKEND_KAS
    client._model = MODEL
    client._work_dir = MagicMock()
    client.set_config_option = AsyncMock()
    client.send_command = AsyncMock()
    client.supports_config_option = MagicMock(side_effect=lambda cid: cid == "effortLevel")
    client.get_valid_effort_levels = MagicMock(return_value=list(LEVELS))
    provider._client = client
    provider._effort_per_model = {MODEL: override} if override else {}
    provider._effort_defaults = defaults or {}
    # KAS ignores the cli.json overlay; keep the shared write off the filesystem.
    provider._apply_effort_overlay = MagicMock(return_value=True)  # type: ignore[method-assign]
    return provider


def test_kas_is_a_config_option_member_under_its_own_id() -> None:
    assert ACP_BACKEND_KAS in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert effort_config_option_id(ACP_BACKEND_KAS) == "effortLevel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "defaults", "expected"),
    [
        ("xhigh", {}, "xhigh"),  # the slot's own pick
        ("", {MODEL: "high"}, "high"),  # the workspace per-model default
    ],
)
async def test_session_start_pushes_the_configured_level(
    override: str, defaults: dict[str, str], expected: str
) -> None:
    provider = _kas_provider(override=override, defaults=defaults)
    await provider._apply_initial_effort()
    provider._client.set_config_option.assert_awaited_once_with("effortLevel", expected)
    provider._client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_is_sent_when_no_effort_is_configured() -> None:
    provider = _kas_provider()
    await provider._apply_initial_effort()
    provider._client.set_config_option.assert_not_awaited()
    provider._client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_live_change_uses_the_config_option_not_the_slash_command() -> None:
    provider = _kas_provider()
    with patch(
        "kiro_crew.dashboard.chat_persistence.get_reasoning_effort_values",
        return_value=frozenset(LEVELS),
    ):
        assert await provider.change_effort("high") is True
    provider._client.set_config_option.assert_awaited_once_with("effortLevel", "high")
    provider._client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_clearing_to_a_workspace_default_uses_the_config_option() -> None:
    provider = _kas_provider(override="low", defaults={MODEL: "high"})
    assert await provider.clear_effort() is True
    provider._client.set_config_option.assert_awaited_once_with("effortLevel", "high")
    provider._client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_handle_adopts_the_options_a_config_write_returns() -> None:
    """``effortLevel`` appears only in the ``model`` write's result.

    The matching ``config_option_update`` notification is buffered behind that
    response until the next turn drains the queue, so the startup effort push --
    gated on ``supports_config_option`` -- would otherwise read the session/new
    list, find no ``effortLevel``, and skip.
    """
    runtime = MagicMock()
    runtime.acp_backend = ACP_BACKEND_KAS
    handle = AcpSessionHandle("s1", asyncio.Queue(), runtime)
    handle._config_options = [{"type": "select", "id": "model", "currentValue": "auto"}]
    handle._send_awaited = AsyncMock(return_value=7)  # type: ignore[method-assign]
    handle._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value=JsonRpcMessage(id=7, result={"configOptions": KAS_OPTIONS_WITH_MODEL})
    )
    handle._sync_effort_levels = MagicMock()  # type: ignore[method-assign]
    assert handle.supports_config_option("effortLevel") is False

    await handle.set_config_option("model", MODEL)

    assert handle.supports_config_option("effortLevel") is True
    assert handle.get_valid_effort_levels() == LEVELS
    handle._sync_effort_levels.assert_called_once()


@pytest.mark.asyncio
async def test_a_result_without_options_keeps_the_cached_list() -> None:
    runtime = MagicMock()
    runtime.acp_backend = ACP_BACKEND_KAS
    handle = AcpSessionHandle("s1", asyncio.Queue(), runtime)
    handle._config_options = list(KAS_OPTIONS_WITH_MODEL)
    handle._send_awaited = AsyncMock(return_value=7)  # type: ignore[method-assign]
    handle._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value=JsonRpcMessage(id=7, result={})
    )

    await handle.set_config_option("effortLevel", "high")

    assert handle._config_options == KAS_OPTIONS_WITH_MODEL

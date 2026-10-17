"""A reasoning effort requested while no model is pinned reaches the session.

With ``agent.model`` set to the empty string the factory resolves no model id,
so the per-model effort map has no key to hold the level under. The factory
carries the level unkeyed and the provider binds it to the model the backend
reports serving once the session is ready, before the initial effort push.

Covered for a harness that advertises its effort option per session (pi), a
harness whose effort support comes from the model registry (claude) and the
kiro family, whose level otherwise rides the spawn-time overlay. An unpinned
kiro-cli serves its ``chat.defaultModel`` when one is set and ``auto`` when
none is, so both are covered there.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO, ACP_BACKEND_PI
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.providers.acp import AcpProvider, _read_cli_overlay, _write_cli_overlay

_PI_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"]


def _provider(
    backend: str, *, unbound: str, served: str, model: str = "", work_dir=None
) -> AcpProvider:
    """An unstarted provider whose client reports *served* once it is ready.

    With *work_dir* the client is configured before construction, so the kiro
    family seeds its effort map from a real overlay there, as on a reused
    workspace.
    """
    with patch("kiro_crew.providers.acp.AcpClient") as client_cls:
        if work_dir is not None:
            client_cls.return_value.backend = backend
            client_cls.return_value._model = model or "auto"
            client_cls.return_value._resolved_model_id = None
            client_cls.return_value._work_dir = work_dir
        provider = AcpProvider(acp_backend=backend, model=model, unbound_effort=unbound)
    client = provider._client
    client.backend = backend
    # An unpinned client records the ``auto`` sentinel until the backend answers.
    client._model = model or "auto"
    client._resolved_model_id = None
    client._work_dir = MagicMock() if work_dir is None else work_dir
    client.supports_config_option = MagicMock(return_value=True)
    client.get_valid_effort_levels = MagicMock(return_value=list(_PI_LEVELS))
    client.set_config_option = AsyncMock()
    client.send_command = AsyncMock()

    async def _ensure_ready() -> None:
        client._resolved_model_id = served

    client.ensure_ready = AsyncMock(side_effect=_ensure_ready)
    return provider


class TestFactoryCarriesTheLevel:
    @pytest.mark.parametrize("backend", [ACP_BACKEND_PI, ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO])
    def test_explicitly_empty_model_carries_the_level_unkeyed(self, backend, tmp_path):
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.acp_backend = backend
        cfg.agent.model = ""
        with (
            patch("kiro_crew.providers.acp.AcpProvider") as mock_provider,
            patch("kiro_crew.members.select_provider_backend", return_value=backend),
        ):
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(
                cwd=str(tmp_path),
                session_key="dashboard:1",
                reasoning_effort_override="low",
            )
        kwargs = mock_provider.call_args.kwargs
        assert kwargs["model"] == ""
        assert kwargs["effort_per_model"] == {}
        assert kwargs["unbound_effort"] == "low"

    def test_pinned_model_keys_the_level_and_carries_nothing(self, tmp_path):
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(
                cwd=str(tmp_path),
                session_key="dashboard:1",
                model_override="claude-opus-4.7",
                reasoning_effort_override="high",
            )
        kwargs = mock_provider.call_args.kwargs
        assert kwargs["effort_per_model"] == {"claude-opus-4.7": "high"}
        assert kwargs["unbound_effort"] == ""


class TestProviderBindsTheLevel:
    @pytest.mark.asyncio
    async def test_advertised_option_harness_pushes_the_level_at_start(self):
        provider = _provider(ACP_BACKEND_PI, unbound="minimal", served="claude-opus-5")
        await provider.start()
        provider._client.set_config_option.assert_awaited_once_with("thought_level", "minimal")
        assert provider._effort_per_model == {"claude-opus-5": "minimal"}

    @pytest.mark.asyncio
    async def test_registry_gated_harness_pushes_the_level_at_start(self):
        provider = _provider(ACP_BACKEND_CLAUDE, unbound="high", served="claude-opus-4.7")
        await provider.start()
        provider._client.set_config_option.assert_awaited_once_with("effort", "high")
        assert provider.supports_effort() is True

    @pytest.mark.asyncio
    async def test_the_requested_level_replaces_one_already_stored_for_the_served_model(self):
        # A live change moves the unbound level with it, so a stored level that
        # differs is a leftover; the session runs the level the caller asked for.
        provider = _provider(ACP_BACKEND_CLAUDE, unbound="high", served="claude-opus-4.7")
        provider._effort_per_model["claude-opus-4.7"] = "low"
        await provider.start()
        provider._client.set_config_option.assert_awaited_once_with("effort", "high")
        assert provider._effort_per_model == {"claude-opus-4.7": "high"}

    @pytest.mark.asyncio
    async def test_served_model_without_effort_logs_the_drop_and_pushes_nothing(self, caplog):
        provider = _provider(ACP_BACKEND_CLAUDE, unbound="high", served="deepseek-3.2")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.acp"):
            await provider.start()
            await provider.start()
        provider._client.set_config_option.assert_not_awaited()
        assert provider._effort_per_model == {}
        drops = [r.getMessage() for r in caplog.records if "will not be applied" in r.getMessage()]
        assert len(drops) == 1
        assert "'high'" in drops[0] and "'deepseek-3.2'" in drops[0]

    @pytest.mark.asyncio
    async def test_no_served_model_logs_the_drop_naming_auto(self, caplog):
        provider = _provider(ACP_BACKEND_CLAUDE, unbound="high", served="")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.acp"):
            await provider.start()
        provider._client.set_config_option.assert_not_awaited()
        assert any(
            "will not be applied" in r.getMessage() and "'auto'" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_kiro_family_pushes_the_level_live_through_change_effort(self):
        provider = _provider(ACP_BACKEND_KIRO, unbound="high", served="claude-opus-4.7")
        provider._client._resolved_model_id = "claude-opus-4.7"
        provider.change_effort = AsyncMock(return_value=True)
        await provider._bind_unbound_effort()
        provider.change_effort.assert_awaited_once_with("high")

    @pytest.mark.asyncio
    async def test_kiro_default_model_takes_the_level_through_the_real_push(self):
        # kiro-cli serves its chat.defaultModel when nothing is pinned (2.29.1
        # reports it as currentModelId and answers "/effort low" with "Effort set
        # to low"), so the bind writes the overlay and pushes /effort live.
        provider = _provider(ACP_BACKEND_KIRO, unbound="high", served="claude-sonnet-5.5")
        provider._client._resolved_model_id = "claude-sonnet-5.5"
        provider._apply_effort_overlay = MagicMock(return_value=True)
        await provider._bind_unbound_effort()
        provider._apply_effort_overlay.assert_called_once()
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "high"})
        assert provider._effort_per_model == {"claude-sonnet-5.5": "high"}

    @pytest.mark.asyncio
    async def test_kiro_push_refused_leaves_no_level_stored(self):
        # A push kiro-cli refuses must not leave the level in the map or the
        # overlay: the next spawn would adopt a level this session never ran.
        provider = _provider(ACP_BACKEND_KIRO, unbound="high", served="claude-sonnet-5.5")
        provider._client._resolved_model_id = "claude-sonnet-5.5"
        provider._apply_effort_overlay = MagicMock(return_value=True)
        provider._client.send_command = AsyncMock(side_effect=RuntimeError("refused"))
        with patch(
            "kiro_crew.providers.acp._clear_cli_overlay_effort", return_value=True
        ) as clear_overlay:
            await provider._bind_unbound_effort()
        clear_overlay.assert_called_once_with(provider._client._work_dir, "claude-sonnet-5.5")
        assert provider._effort_per_model == {}

    @pytest.mark.asyncio
    async def test_kiro_family_pushes_over_a_different_level_the_overlay_left(self, tmp_path):
        # A reused workspace whose overlay holds another level for the served
        # model seeds the map at construction; the requested level still wins,
        # live and in the overlay the next spawn reads.
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "high")
        provider = _provider(
            ACP_BACKEND_KIRO, unbound="low", served="claude-opus-4.7", work_dir=tmp_path
        )
        assert provider._effort_per_model == {"claude-opus-4.7": "high"}
        provider._client._resolved_model_id = "claude-opus-4.7"
        await provider._bind_unbound_effort()
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "low"})
        assert provider._effort_per_model == {"claude-opus-4.7": "low"}
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "low"}

    @pytest.mark.asyncio
    async def test_kiro_family_skips_the_push_when_the_overlay_already_holds_the_level(
        self,
    ):
        # kiro-cli read that level from the overlay at spawn, so the session
        # already runs it.
        provider = _provider(ACP_BACKEND_KIRO, unbound="high", served="claude-opus-4.7")
        provider._client._resolved_model_id = "claude-opus-4.7"
        provider._effort_per_model["claude-opus-4.7"] = "high"
        provider.change_effort = AsyncMock(return_value=True)
        await provider._bind_unbound_effort()
        provider.change_effort.assert_not_awaited()
        assert provider._effort_per_model == {"claude-opus-4.7": "high"}

    @pytest.mark.asyncio
    async def test_kiro_push_refused_over_an_overlay_level_keeps_the_spawned_level(self):
        # The session was spawned at the overlay's level, so a refused push must
        # leave the map and the overlay naming that level, not the requested one.
        provider = _provider(ACP_BACKEND_KIRO, unbound="low", served="claude-opus-4.7")
        provider._client._resolved_model_id = "claude-opus-4.7"
        provider._effort_per_model["claude-opus-4.7"] = "high"
        provider._apply_effort_overlay = MagicMock(return_value=True)
        provider._client.send_command = AsyncMock(side_effect=RuntimeError("refused"))
        await provider._bind_unbound_effort()
        assert provider._effort_per_model == {"claude-opus-4.7": "high"}
        # Written once for the attempt and once more for the rollback.
        assert provider._apply_effort_overlay.call_count == 2

    @pytest.mark.asyncio
    async def test_kiro_session_on_auto_still_offers_no_effort(self):
        # kiro-cli reports currentModelId "auto" for an auto session and refuses
        # any effort level there, so neither the binding nor a live change may
        # push one.
        provider = _provider(ACP_BACKEND_KIRO, unbound="high", served="auto")
        provider._client._model = ""
        provider._client._resolved_model_id = "auto"
        await provider._bind_unbound_effort()
        assert provider.supports_effort() is False
        assert await provider.change_effort("high") is False
        provider._client.send_command.assert_not_awaited()
        assert provider._effort_per_model == {}

    @pytest.mark.asyncio
    async def test_a_pinned_model_is_left_to_the_keyed_path(self):
        provider = _provider(
            ACP_BACKEND_CLAUDE,
            unbound="high",
            served="claude-opus-4.7",
            model="claude-opus-4.7",
        )
        await provider._bind_unbound_effort()
        assert provider._effort_per_model == {}

    @pytest.mark.asyncio
    async def test_clearing_the_effort_stops_the_next_restart_rebinding_it(self):
        provider = _provider(ACP_BACKEND_CLAUDE, unbound="high", served="claude-opus-4.7")
        await provider.start()
        assert await provider.clear_effort() is False
        provider._client.set_config_option.reset_mock()
        await provider.start()
        provider._client.set_config_option.assert_not_awaited()
        assert provider._effort_per_model == {}

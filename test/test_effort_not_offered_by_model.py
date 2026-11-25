"""A stored effort level the serving model does not offer stays picked, and the
dashboard is told which level runs instead.

The frames are pi's own capture (``test/fixtures/acp_frames/pi/effort-live.jsonl``):
``claude-opus-4-8`` offers ``max``; after the model write to ``claude-sonnet-4-5``
the ``thought_level`` select offers only ``off`` through ``high`` and pi clamps
the level to ``high`` itself.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import acp_pi_backend_helpers
import pytest
from acp_pi_backend_helpers import ROOT
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, ACP_BACKEND_PI
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_selection_capabilities
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider

_no_ambient_pi_env = acp_pi_backend_helpers._no_ambient_pi_env

FRAMES = ROOT / "test" / "fixtures" / "acp_frames" / "pi" / "effort-live.jsonl"
OPUS = "anthropic/claude-opus-4-8"
SONNET = "anthropic/claude-sonnet-4-5"


def _thought_level(frame: dict) -> dict:
    """The last ``thought_level`` select carried anywhere in *frame*."""
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("id") == "thought_level" and "options" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(frame)
    return found[-1]


def _frames() -> list[dict]:
    return [json.loads(line) for line in FRAMES.read_text().splitlines() if line.strip()]


def _opus_option() -> dict:
    """The ``session/new`` select on opus."""
    return _thought_level(_frames()[2])


def _sonnet_option() -> dict:
    """The select the model write to sonnet answers with."""
    return _thought_level(_frames()[11])


def _pi_provider(tmp_path, model: str, option: dict) -> AcpProvider:
    provider = AcpProvider(acp_backend=ACP_BACKEND_PI, work_dir=tmp_path, model=model)
    provider._client._acp_config_options = [option]
    provider._client._model = model
    provider._client.set_config_option = AsyncMock()
    return provider


def test_the_capture_is_the_premise() -> None:
    """Stated so the tests below cannot pass for the wrong reason."""
    opus = _opus_option()
    sonnet = _sonnet_option()
    assert "max" in [o["value"] for o in opus["options"]]
    assert [o["value"] for o in sonnet["options"]] == ["off", "minimal", "low", "medium", "high"]
    assert sonnet["currentValue"] == "high"


class TestTheSlotKeepsThePick:
    """The level stays on the slot for a model that offers it, as before."""

    @pytest.mark.asyncio
    async def test_a_live_switch_writes_nothing_and_keeps_the_level(self, tmp_path) -> None:
        from kiro_crew.dashboard import chat_handlers as ch

        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "max"

        assert await ch._reapply_effort_after_live_switch("s1", slot, provider) is True

        provider._client.set_config_option.assert_not_awaited()
        assert slot.reasoning_effort == "max"


class TestTheSessionNamesTheLevelThatRuns:
    def test_the_option_names_the_level_after_a_switch(self, tmp_path) -> None:
        """pi clamped ``max`` to ``high``; the option says so."""
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())

        assert provider.effort_left_unapplied("max") == ("max", "high")

    def test_the_option_outranks_a_level_this_session_never_pushed(self, tmp_path) -> None:
        """A live switch pushes no default for a hidden level, so the option answers."""
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())
        provider._effort_defaults = {SONNET: "low"}

        assert provider.effort_left_unapplied("max") == ("max", "high")

    def test_without_a_named_current_value_the_resolved_level_answers(self, tmp_path) -> None:
        option = {k: v for k, v in _sonnet_option().items() if k != "currentValue"}
        provider = _pi_provider(tmp_path, SONNET, option)
        provider._effort_per_model = {SONNET: "max"}
        provider._effort_defaults = {SONNET: "low"}

        assert provider.effort_left_unapplied("max") == ("max", "low")

    def test_a_current_value_the_option_does_not_list_names_nothing(self, tmp_path) -> None:
        option = dict(_sonnet_option(), currentValue="turbo")
        provider = _pi_provider(tmp_path, SONNET, option)

        assert provider.effort_left_unapplied("max") == ("max", "")

    def test_an_offered_level_is_not_reported(self, tmp_path) -> None:
        provider = _pi_provider(tmp_path, OPUS, _opus_option())

        assert provider.effort_left_unapplied("max") is None
        assert provider.effort_left_unapplied("") is None

    def test_a_level_that_folds_onto_an_offered_one_is_not_reported(self, tmp_path) -> None:
        """pi's ``max`` runs at ``xhigh`` where ``xhigh`` is offered: applied, not dropped."""
        option = dict(
            _sonnet_option(),
            options=[
                {"value": v, "name": v}
                for v in ("off", "minimal", "low", "medium", "high", "xhigh")
            ],
        )
        provider = _pi_provider(tmp_path, SONNET, option)

        assert provider.effort_left_unapplied("max") is None

    @pytest.mark.parametrize(
        "backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE], ids=["kiro", "claude"]
    )
    def test_a_harness_whose_level_rides_the_model_reports_nothing(self, tmp_path, backend) -> None:
        """Only an advertised vocabulary hides a level; kiro-cli refusals stay as they are."""
        provider = AcpProvider(acp_backend=backend, work_dir=tmp_path, model=SONNET)
        provider._client._acp_config_options = [_sonnet_option()]
        provider._client._model = SONNET

        assert provider.effort_left_unapplied("max") is None


class TestASessionStartSaysItDroppedTheLevel:
    @pytest.mark.asyncio
    async def test_a_stored_level_the_model_does_not_offer_is_logged(self, tmp_path, caplog):
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())
        provider._effort_per_model = {SONNET: "max"}

        with caplog.at_level(logging.INFO, logger="kiro_crew.providers.acp"):
            await provider._apply_initial_effort()

        provider._client.set_config_option.assert_not_awaited()
        dropped = [
            r
            for r in caplog.records
            if r.name == "kiro_crew.providers.acp" and "stored effort not applied" in r.getMessage()
        ]
        assert len(dropped) == 1
        assert "effort=max" in dropped[0].getMessage()
        assert provider._effort_per_model == {SONNET: "max"}

    @pytest.mark.asyncio
    async def test_a_default_that_replaces_it_is_logged_too(self, tmp_path, caplog):
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())
        provider._effort_per_model = {SONNET: "max"}
        provider._effort_defaults = {SONNET: "low"}

        with caplog.at_level(logging.INFO, logger="kiro_crew.providers.acp"):
            await provider._apply_initial_effort()

        provider._client.set_config_option.assert_awaited_once_with("thought_level", "low")
        messages = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.providers.acp"]
        assert any("effort=max runs=low" in m for m in messages)

    @pytest.mark.asyncio
    async def test_an_offered_level_is_applied_without_that_line(self, tmp_path, caplog):
        provider = _pi_provider(tmp_path, OPUS, _opus_option())
        provider._effort_per_model = {OPUS: "max"}

        with caplog.at_level(logging.INFO, logger="kiro_crew.providers.acp"):
            await provider._apply_initial_effort()

        provider._client.set_config_option.assert_awaited_once_with("thought_level", "max")
        messages = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.providers.acp"]
        assert not any("stored effort not applied" in m for m in messages)


def _app(slot: _ChatSlot, provider: object) -> web.Application:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    return app


async def _capabilities(slot: _ChatSlot, provider: object) -> dict:
    async with TestClient(TestServer(_app(slot, provider))) as client:
        resp = await client.get(f"/api/chat/slots/{slot.key}/selection-capabilities")
        assert resp.status == 200
        return await resp.json()


class TestTheSelectionCapabilitiesReport:
    @pytest.mark.asyncio
    async def test_the_level_that_runs_is_reported_beside_the_pick(self, tmp_path) -> None:
        chat_handlers.register_reasoning_effort_values(
            [o["value"] for o in _opus_option()["options"]]
        )
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "max"
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())

        data = await _capabilities(slot, provider)

        assert data["effort_unapplied"] == "max"
        assert data["effort_running"] == "high"
        # Checked for the slot's own pick, so a chip with no pick ignores it.
        assert "effort_inherited" not in data
        assert "max" not in data["effort_levels"]
        assert slot.reasoning_effort == "max"

    @pytest.mark.asyncio
    async def test_nothing_is_reported_for_an_offered_level(self, tmp_path) -> None:
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "max"
        provider = _pi_provider(tmp_path, OPUS, _opus_option())

        data = await _capabilities(slot, provider)

        assert "effort_unapplied" not in data
        assert "effort_running" not in data

    @pytest.mark.asyncio
    async def test_an_inherited_level_is_checked_like_a_pick(self, tmp_path, monkeypatch) -> None:
        """No pick: the chip names the inherited level, so that level is the one checked."""
        from kiro_crew.config import loader

        cfg = loader.KiroCrewConfig()
        cfg.agent.reasoning_effort = "max"
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        slot = _ChatSlot("s1")
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())

        data = await _capabilities(slot, provider)

        assert data["effort_unapplied"] == "max"
        assert data["effort_running"] == "high"
        assert data["effort_inherited"] is True
        assert slot.reasoning_effort == ""

    @staticmethod
    def _reviewer_pins_max(monkeypatch) -> None:
        """A crewmate ``reviewer`` pins ``max``; nothing else sets a level."""
        from kiro_crew.config import loader

        cfg = loader.KiroCrewConfig()
        cfg.agents["reviewer"] = loader.KiroCrewAgentConfig(
            kiro_agent="kirocrew", reasoning_effort="max"
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    @pytest.mark.asyncio
    async def test_a_template_slot_never_takes_a_same_named_crewmates_pin(
        self, tmp_path, monkeypatch
    ) -> None:
        """A template that shares a crewmate's name runs under no crew, so no pin is checked."""
        self._reviewer_pins_max(monkeypatch)
        slot = _ChatSlot("s1")
        slot.agent = "reviewer"
        slot.agent_kind = "template"
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())

        data = await _capabilities(slot, provider)

        assert "effort_unapplied" not in data
        assert "effort_running" not in data

    @pytest.mark.asyncio
    async def test_a_crewmate_slot_still_checks_its_crewmates_pin(
        self, tmp_path, monkeypatch
    ) -> None:
        self._reviewer_pins_max(monkeypatch)
        slot = _ChatSlot("s1")
        slot.agent = "reviewer"
        slot.agent_kind = "member"
        provider = _pi_provider(tmp_path, SONNET, _sonnet_option())

        data = await _capabilities(slot, provider)

        assert data["effort_unapplied"] == "max"
        assert data["effort_running"] == "high"
        assert data["effort_inherited"] is True
        assert slot.reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_nothing_is_asked_when_no_level_is_picked_or_inherited(self, monkeypatch) -> None:
        from kiro_crew.config import loader

        cfg = loader.KiroCrewConfig()
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        slot = _ChatSlot("s1")
        provider = MagicMock(spec=AcpProvider)
        provider.capabilities = SimpleNamespace(backend=ACP_BACKEND_PI)
        provider.supports_effort.return_value = True
        provider.get_valid_effort_levels.return_value = ["low", "high"]
        provider.ultracode_enabled.return_value = None
        provider.fast_mode_enabled.return_value = None

        data = await _capabilities(slot, provider)

        provider.effort_left_unapplied.assert_not_called()
        assert "effort_unapplied" not in data

    @pytest.mark.asyncio
    async def test_a_model_that_takes_no_effort_reports_nothing(self, tmp_path) -> None:
        """An off-only option: the chip shows no level at all, so there is nothing to correct."""
        option = dict(
            _sonnet_option(), currentValue="off", options=[{"value": "off", "name": "off"}]
        )
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "max"
        provider = _pi_provider(tmp_path, SONNET, option)

        data = await _capabilities(slot, provider)

        assert data["effort_supported"] is False
        assert "effort_unapplied" not in data

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            (("max", "<b>"), {"effort_unapplied": "max", "effort_running": ""}),
            (("not a level", "high"), {}),
            (None, {}),
        ],
    )
    async def test_only_known_levels_reach_the_chip(self, answer, expected) -> None:
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "max"
        provider = MagicMock(spec=AcpProvider)
        provider.capabilities = SimpleNamespace(backend=ACP_BACKEND_PI)
        provider.supports_effort.return_value = True
        provider.get_valid_effort_levels.return_value = ["low", "high"]
        provider.ultracode_enabled.return_value = None
        provider.fast_mode_enabled.return_value = None
        provider.effort_left_unapplied.return_value = answer

        data = await _capabilities(slot, provider)

        reported = {k: v for k, v in data.items() if k in ("effort_unapplied", "effort_running")}
        assert reported == expected

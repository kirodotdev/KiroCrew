"""The per-template model setting, ``templates.<name>.model``.

A model the user chose for an agent template outranks the model the template's
own spec pins, for every session that launches that template. Unset or ``""``
leaves the spec pin in force. It is a model setting only: a template session
still takes no workspace, memory store or other field from a crew row that
shares the template's name.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.config.loader import (
    KiroCrewConfig,
    resolve_app_spawn_model,
    resolve_effective_model,
)
from kiro_crew.session import SessionManager, _session_model

GLOBAL = "claude-opus-5"
SPEC_PIN = "claude-opus-4.8"
USER_PICK = "claude-sonnet-5"
CREW_PIN = "claude-haiku-5"
APP_PIN = "claude-haiku-4.5"
SLOT_PIN = "claude-opus-4.6"
TEMPLATE = "oncall-template"


@pytest.fixture
def specs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kiro agent dir with one template that pins a model and one that does not."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / f"{TEMPLATE}.json").write_text(
        json.dumps({"name": TEMPLATE, "model": SPEC_PIN}), encoding="utf-8"
    )
    (d / "unpinned.json").write_text(json.dumps({"name": "unpinned"}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: d)
    return d


def _load(tmp_path: Path, data: dict) -> KiroCrewConfig:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


def _data(template_model: str | None = USER_PICK, **extra) -> dict:
    data: dict = {
        "agent": {"model": GLOBAL, "provider": "acp"},
        "agents": {"default": {"kiro_agent": "kirocrew", "memory_store": "default"}},
        "default_agent": "default",
    }
    if template_model is not None:
        data["templates"] = {TEMPLATE: {"model": template_model}}
    data.update(extra)
    return data


def _factory(captured: dict):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        captured.update(kwargs)
        m = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.is_process_alive = lambda: True
        m.is_alive.return_value = True
        return m

    return factory


async def _started_on(cfg: KiroCrewConfig, **kwargs) -> dict:
    captured: dict = {}
    mgr = SessionManager(cfg, provider_factory=_factory(captured))
    try:
        await mgr.get_or_create("tpl", **kwargs)
    finally:
        await mgr.close_all()
    return captured


class TestTheTable:
    def test_loads_and_round_trips(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data())
        assert cfg.to_dict().get("templates") == {TEMPLATE: {"model": USER_PICK}}

    def test_absent_table_is_empty(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data(template_model=None))
        assert cfg.to_dict().get("templates") == {}

    def test_a_malformed_table_reads_empty(self, tmp_path, specs_dir):
        data = _data(template_model=None)
        data["templates"] = ["not", "an", "object"]
        cfg = _load(tmp_path, data)
        assert cfg.to_dict().get("templates") == {}
        assert _session_model(cfg, TEMPLATE, crew_agent="") == SPEC_PIN


class TestTheTemplateLaunch:
    @pytest.mark.asyncio
    async def test_the_users_pick_outranks_the_spec_pin(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data())
        got = await _started_on(cfg, agent=TEMPLATE, crew_agent="")
        assert got["model_override"] == USER_PICK
        assert _session_model(cfg, TEMPLATE, crew_agent="") == USER_PICK
        assert resolve_effective_model(cfg, None, template=TEMPLATE) == USER_PICK

    @pytest.mark.parametrize("pick", [None, "", "auto"])
    def test_no_pick_keeps_the_spec_pin(self, tmp_path, specs_dir, pick):
        cfg = _load(tmp_path, _data(template_model=pick))
        assert _session_model(cfg, TEMPLATE, crew_agent="") == SPEC_PIN

    def test_a_pick_for_an_unpinned_template_outranks_the_global(self, tmp_path, specs_dir):
        data = _data(template_model=None)
        data["templates"] = {"unpinned": {"model": USER_PICK}}
        cfg = _load(tmp_path, data)
        assert _session_model(cfg, "unpinned", crew_agent="") == USER_PICK

    def test_another_templates_pick_does_not_apply(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data())
        assert _session_model(cfg, "unpinned", crew_agent="") == GLOBAL

    def test_a_pick_the_harness_cannot_claim_falls_to_the_spec_pin(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data(template_model="gpt-5.6-sol"))
        # kiro-cli's namespace cannot claim a codex-style id, so the tier defers.
        with unittest.mock.patch(
            "kiro_crew.config.loader.model_scope.scoped_pin",
            side_effect=lambda pin, ns, **kw: "" if pin == "gpt-5.6-sol" else pin,
        ):
            assert _session_model(cfg, TEMPLATE, crew_agent="") == SPEC_PIN

    def test_the_spec_pin_the_factory_falls_back_to_is_the_users_pick(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data())
        assert cfg.acp_effective_model(TEMPLATE, None) == USER_PICK

    @pytest.mark.asyncio
    async def test_a_callers_own_model_still_wins(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data())
        got = await _started_on(cfg, agent=TEMPLATE, crew_agent="", model=SLOT_PIN)
        assert got["model_override"] == SLOT_PIN


class TestIsolation:
    def test_a_same_named_crew_row_still_does_not_reach_a_template_launch(
        self, tmp_path, specs_dir
    ):
        """Only ``templates.<name>`` reaches a template launch, never a crew row."""
        data = _data(template_model=None)
        data["agents"][TEMPLATE] = {"kiro_agent": TEMPLATE, "model": CREW_PIN}
        cfg = _load(tmp_path, data)
        assert _session_model(cfg, TEMPLATE, crew_agent="") == SPEC_PIN

    def test_the_default_rows_pin_does_not_reach_a_template_launch(self, tmp_path, specs_dir):
        data = _data()
        data["agents"]["default"]["model"] = CREW_PIN
        cfg = _load(tmp_path, data)
        assert _session_model(cfg, TEMPLATE, crew_agent="") == USER_PICK

    def test_a_crews_own_pin_outranks_its_templates_pick(self, tmp_path, specs_dir):
        data = _data()
        data["agents"]["oncall"] = {"kiro_agent": TEMPLATE, "model": CREW_PIN}
        cfg = _load(tmp_path, data)
        assert _session_model(cfg, "oncall") == CREW_PIN

    def test_a_crew_that_pins_nothing_runs_its_templates_pick(self, tmp_path, specs_dir):
        data = _data()
        data["agents"]["oncall"] = {"kiro_agent": TEMPLATE}
        cfg = _load(tmp_path, data)
        assert _session_model(cfg, "oncall") == USER_PICK


class TestAppSpawn:
    def test_a_templates_pick_outranks_the_apps_setting(self, tmp_path, specs_dir):
        data = _data(template_model=None, apps={"demo": {"model": APP_PIN}})
        data["templates"] = {"unpinned": {"model": USER_PICK}}
        cfg = _load(tmp_path, data)
        # The spawn defers to the session, which resolves the template's pick.
        assert resolve_app_spawn_model(cfg, "demo", "unpinned") == ""
        assert _session_model(cfg, "unpinned", crew_agent="", app_name="demo") == USER_PICK

    def test_without_a_pick_the_apps_setting_still_applies(self, tmp_path, specs_dir):
        cfg = _load(tmp_path, _data(template_model=None, apps={"demo": {"model": APP_PIN}}))
        assert resolve_app_spawn_model(cfg, "demo", "unpinned") == APP_PIN


class TestALiveEdit:
    @pytest.mark.asyncio
    async def test_an_edit_reaches_new_sessions_and_the_factory_without_a_restart(
        self, tmp_path, specs_dir
    ):
        from _hot_reload_helpers import change

        edited = _load(tmp_path, _data())
        mgr = SessionManager(_load(tmp_path, _data(template_model=None)))
        try:
            assert "templates" in mgr._config_sub.prefixes
            with unittest.mock.patch.object(mgr, "refresh_defaults", AsyncMock()) as refresh:
                await mgr._on_config_change(change(edited, f"templates.{TEMPLATE}.model"))
            # The provider factory's fallback reads the table, so it is rebuilt.
            refresh.assert_awaited_once_with(cfg=edited)
        finally:
            await mgr.close_all()


class TestAWarmPoolClaim:
    """A warm process for the pool's template launched on the template's pick."""

    @staticmethod
    async def _claim(tmp_path: Path, requested: str) -> AsyncMock:
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider

        data = _data(session={"pool_size": 1, "pool_agent": TEMPLATE})
        mgr = SessionManager(_load(tmp_path, data), provider_factory=_factory({}))
        pooled = MagicMock()
        pooled.__class__ = AcpProvider
        pooled.cwd = ""
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.shutdown = AsyncMock()
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = unittest.mock.MagicMock()
        try:
            # The spec read the post-claim check makes; the pool helpers pin it the same way.
            with unittest.mock.patch.object(
                SessionManager, "_resolve_agent_model", return_value=SPEC_PIN
            ):
                await mgr.get_or_create("tpl", agent=TEMPLATE, crew_agent="", model=requested)
        finally:
            await mgr.close_all()
        mgr._drain_and_claim.assert_awaited_once()
        return pooled.client.set_model

    @pytest.mark.asyncio
    async def test_asking_for_the_spec_pin_switches_off_the_pick(self, tmp_path, specs_dir):
        set_model = await self._claim(tmp_path, SPEC_PIN)
        set_model.assert_awaited_once_with(SPEC_PIN)

    @pytest.mark.asyncio
    async def test_asking_for_the_pick_needs_no_switch(self, tmp_path, specs_dir):
        set_model = await self._claim(tmp_path, USER_PICK)
        set_model.assert_not_awaited()


class TestAnEditDuringAClaim:
    @pytest.mark.asyncio
    async def test_the_claim_compares_against_the_pick_the_process_launched_on(
        self, tmp_path, specs_dir
    ):
        """An edit applied while a claim is in flight does not change which model
        the claimed process launched on, so the requested model is still sent."""
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider

        data = _data(session={"pool_size": 1, "pool_agent": TEMPLATE})
        mgr = SessionManager(_load(tmp_path, data), provider_factory=_factory({}))
        edited = _load(tmp_path, {**data, "templates": {TEMPLATE: {"model": CREW_PIN}}})
        pooled = MagicMock()
        pooled.__class__ = AcpProvider
        pooled.cwd = ""
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.shutdown = AsyncMock()
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = unittest.mock.MagicMock()

        def _edit_lands(cfg, agent, crew):
            # The live applier swaps the manager's config mid-claim.
            mgr._cfg = edited
            return ""

        try:
            with (
                unittest.mock.patch.object(
                    SessionManager, "_resolve_agent_model", return_value=SPEC_PIN
                ),
                unittest.mock.patch(
                    "kiro_crew.session._resolve_allocation_crew_identity", side_effect=_edit_lands
                ),
            ):
                await mgr.get_or_create("tpl", agent=TEMPLATE, crew_agent="", model=CREW_PIN)
        finally:
            await mgr.close_all()
        pooled.client.set_model.assert_awaited_once_with(CREW_PIN)

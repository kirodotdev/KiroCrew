"""The default-model ladder a new session starts on, owned by the session layer.

Every surface that asks ``SessionManager.get_or_create`` for no model (Slack, cron,
sub-agents, the dashboard's inheriting slots) gets the answer of ONE resolver,
:func:`resolve_effective_model`, the chain the dashboard's model chip reads. The
precedence, highest first:

1. the caller's own model (a slot pin), which applies to that call only;
2. a deliberate agent pin: the crew's ``model``, else its template's;
3. an app's ``apps.<name>.model`` for a session that app owns;
4. the global ``agent.model``; the built-in ``default`` crew's unset or ``auto``
   model inherits it;
5. the installed agent file.

Each test drives a real ``SessionManager`` with a factory that records the model
it is handed, so a rung the session layer stops resolving shows up as the factory
being handed something else.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from _hot_reload_helpers import write_config

from kiro_crew.config import live
from kiro_crew.config.live import ConfigWatch
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    app_model_pin,
    resolve_effective_model,
)
from kiro_crew.session import SessionManager, _session_model

GLOBAL = "claude-opus-5"
CREW_PIN = "claude-sonnet-5"
TEMPLATE_PIN = "claude-sonnet-4.6"
APP_PIN = "claude-haiku-5"
SLOT_PIN = "claude-haiku-4.5"
INSTALLED = "claude-opus-4.6"
APP = "mochi"


@pytest.fixture
def specs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kiro agent dir with one template that pins a model and one that does not."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / "pinned.json").write_text(
        json.dumps({"name": "pinned", "model": TEMPLATE_PIN}), encoding="utf-8"
    )
    (d / "unpinned.json").write_text(json.dumps({"name": "unpinned"}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: d)
    return d


@pytest.fixture
def cfg(specs_dir: Path) -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.agent.provider = "acp"
    c.agent.model = GLOBAL
    c.session.timeout_secs = 2
    c.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "oncall": KiroCrewAgentConfig(kiro_agent="pinned", model=CREW_PIN),
        "writer": KiroCrewAgentConfig(kiro_agent="pinned"),
    }
    return c


def _capturing_factory(captured: dict):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        captured.update(kwargs)
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.is_process_alive = lambda: True
        m.is_alive.return_value = True
        return m

    return factory


async def _started_on(cfg: KiroCrewConfig, key: str = "ladder", **kwargs) -> dict:
    """Cold-start *key* and return what the provider factory was handed."""
    captured: dict = {}
    mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
    try:
        await mgr.get_or_create(key, **kwargs)
        captured["stamp"] = mgr.allocation_requested_model(key)
    finally:
        await mgr.close_all()
    return captured


class TestTheLadder:
    @pytest.mark.asyncio
    async def test_a_callers_own_model_outranks_every_tier(self, cfg):
        """Rung 1: the slot pin, for this call only."""
        got = await _started_on(cfg, agent="oncall", model=SLOT_PIN, app_name=APP)
        assert got["model_override"] == SLOT_PIN

    @pytest.mark.asyncio
    async def test_a_crew_pin_outranks_its_template_pin_and_the_global(self, cfg):
        """Rung 2, the crew half."""
        got = await _started_on(cfg, agent="oncall")
        assert got["model_override"] == CREW_PIN

    @pytest.mark.asyncio
    async def test_a_template_pin_outranks_the_global(self, cfg):
        """Rung 2, the template half: the session layer hands the pin itself."""
        got = await _started_on(cfg, agent="pinned")
        assert got["model_override"] == TEMPLATE_PIN
        assert got["stamp"] == TEMPLATE_PIN

    def test_a_template_the_snapshot_has_not_listed_still_answers_its_pin(self, cfg, monkeypatch):
        """The session launches the template it is handed, verbatim.

        The materialized-agent snapshot that maps a selection name to a template is
        refreshed at boot and around app registration, so it can lag a spec written
        since. Read through that lookup (``selection_kind="template"``), such a
        template answers the default template's tier instead of its own pin, while
        the factory still launches it and would have run its pin.
        """
        monkeypatch.setattr("kiro_crew.config.loader._materialized_kiro_agent", lambda *a, **k: "")
        assert _session_model(cfg, "pinned") == TEMPLATE_PIN
        assert resolve_effective_model(cfg, "pinned", selection_kind="template") == GLOBAL

    @pytest.mark.asyncio
    async def test_a_crew_that_pins_nothing_runs_its_templates_pin(self, cfg):
        got = await _started_on(cfg, agent="writer")
        assert got["model_override"] == TEMPLATE_PIN

    @pytest.mark.asyncio
    async def test_an_apps_setting_applies_below_the_agent_pins(self, cfg):
        """Rung 3: an app's own setting, and never a key the factory is handed."""
        cfg.apps = {APP: unittest.mock.MagicMock(model=APP_PIN)}
        got = await _started_on(cfg, agent="unpinned", app_name=APP)
        assert got["model_override"] == APP_PIN
        assert "app_name" not in got

    @pytest.mark.asyncio
    async def test_an_agent_pin_outranks_an_apps_setting(self, cfg):
        cfg.apps = {APP: unittest.mock.MagicMock(model=APP_PIN)}
        got = await _started_on(cfg, agent="pinned", app_name=APP)
        assert got["model_override"] == TEMPLATE_PIN

    @pytest.mark.asyncio
    @pytest.mark.parametrize("default_model", ["", "auto"])
    async def test_the_default_crews_unset_model_inherits_the_global(self, cfg, default_model):
        """Rung 4: ``default`` with no deliberate pin lets ``agent.model`` through."""
        cfg.agents["default"].model = default_model
        got = await _started_on(cfg, agent="default")
        assert got["model_override"] == GLOBAL

    @pytest.mark.asyncio
    async def test_a_deliberate_pin_on_the_default_crew_stays_a_pin(self, cfg):
        cfg.agents["default"].model = CREW_PIN
        got = await _started_on(cfg, agent="default")
        assert got["model_override"] == CREW_PIN

    @pytest.mark.asyncio
    async def test_an_auto_global_falls_to_the_installed_agent_file(self, cfg):
        """Rung 5: what the factory itself would read, now resolved up front."""
        cfg.agent.model = "auto"
        with unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: INSTALLED)
        ):
            got = await _started_on(cfg, agent="unpinned")
        assert got["model_override"] == INSTALLED
        assert got["stamp"] == INSTALLED

    @pytest.mark.asyncio
    async def test_every_tier_deferring_leaves_the_backend_to_choose(self, cfg):
        cfg.agent.model = "auto"
        with unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")
        ):
            got = await _started_on(cfg, agent="unpinned")
        assert got["model_override"] is None
        assert got["stamp"] == ""


class TestTheConfigSource:
    @pytest.mark.asyncio
    async def test_a_refreshed_config_reaches_the_next_cold_start(self, cfg):
        """The watcher-refreshed config is the source: no gateway restart."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        try:
            await mgr.get_or_create("before", agent="unpinned")
            assert captured["model_override"] == GLOBAL
            changed = KiroCrewConfig()
            changed.agent.provider = "acp"
            changed.agent.model = CREW_PIN
            changed.agents = dict(cfg.agents)
            # The refresh rebuilds the provider factory from the new config; keep
            # the recording one so the next cold start is still observable.
            with unittest.mock.patch(
                "kiro_crew.session.build_provider_factory",
                lambda _cfg: _capturing_factory(captured),
            ):
                await mgr.refresh_defaults(cfg=changed)
            await mgr.get_or_create("after", agent="unpinned")
            assert captured["model_override"] == CREW_PIN
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_apps_only_edit_reaches_the_manager_through_the_watcher(self, tmp_path):
        """``apps.<name>.model`` is a ladder tier, so the manager subscribes to ``apps``."""
        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        write_config(cfg_file, {"session": {"pool_size": 0}, "apps": {APP: {"model": ""}}})
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            unittest.mock.patch("kiro_crew.config.loader.config_local_path", return_value=local),
            unittest.mock.patch(
                "kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)
            ),
        ):
            watch = live.watch()
            boot = KiroCrewConfig.load()
            mgr = SessionManager(boot, provider_factory=unittest.mock.MagicMock())
            watch.prime(boot)
            write_config(cfg_file, {"session": {"pool_size": 0}, "apps": {APP: {"model": APP_PIN}}})
            change = await watch.refresh_now()
        try:
            assert change is not None
            assert mgr._cfg is change.new
            assert app_model_pin(mgr._cfg, APP) == APP_PIN
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_apps", ["not-an-object", {APP: "not-an-entry", "": {}}])
    async def test_a_malformed_apps_section_never_holds_back_an_agent_model_edit(
        self, tmp_path, bad_apps
    ):
        """A bad ``apps`` table reads as "every app inherits", never as degraded.

        So watching ``apps`` cannot defer the manager's apply: an ``agent.model``
        edit written beside it still reaches the next cold start.
        """
        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        write_config(cfg_file, {"session": {"pool_size": 0}, "agent": {"model": GLOBAL}})
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            unittest.mock.patch("kiro_crew.config.loader.config_local_path", return_value=local),
            unittest.mock.patch(
                "kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)
            ),
            unittest.mock.patch(
                "kiro_crew.session.build_provider_factory",
                lambda _cfg: unittest.mock.MagicMock(),
            ),
        ):
            watch = live.watch()
            boot = KiroCrewConfig.load()
            mgr = SessionManager(boot, provider_factory=unittest.mock.MagicMock())
            watch.prime(boot)
            write_config(
                cfg_file,
                {"session": {"pool_size": 0}, "agent": {"model": CREW_PIN}, "apps": bad_apps},
            )
            change = await watch.refresh_now()
        try:
            assert change is not None
            assert "apps" not in change.new.degraded_sections
            assert mgr._cfg is change.new
            assert mgr._cfg.agent.model == CREW_PIN
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [RuntimeError("boom"), StopIteration("malformed")])
    async def test_a_resolver_error_never_fails_the_cold_start(self, cfg, error):
        """The factory's own fallback takes over; the session still starts."""
        with unittest.mock.patch(
            "kiro_crew.config.loader.resolve_effective_model", side_effect=error
        ):
            got = await _started_on(cfg, agent="unpinned")
        assert got["model_override"] is None


class TestTheBackendScope:
    def test_a_member_dm_is_scoped_to_the_member_backend(self, cfg):
        """Each tier is judged for the harness that will run the session."""
        cfg.agent.member_acp_backend = "codex"
        seen: list[object] = []
        real = __import__("kiro_crew.config.loader", fromlist=["x"]).resolve_effective_model

        def spy(*args, **kwargs):
            seen.append(kwargs.get("backend"))
            return real(*args, **kwargs)

        with (
            unittest.mock.patch("kiro_crew.config.loader.resolve_effective_model", spy),
            unittest.mock.patch(
                "kiro_crew.acp_backends.resolve_selected_backend", side_effect=lambda b: b
            ),
        ):
            _session_model(cfg, "oncall", session_key="dashboard:member-oncall")
            _session_model(cfg, "oncall", session_key="dashboard:chat-1")
            _session_model(cfg, "oncall", backend_override="claude")
        assert seen == ["codex", None, "claude"]

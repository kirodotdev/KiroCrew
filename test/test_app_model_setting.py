"""The per-app model setting, ``apps.<name>.model``.

A user can choose the model an app's sessions and background spawns start on, from
the app's row in Settings. It is strictly opt-in, so these tests pin both halves of
that contract:

* unset or ``""``, every app session and spawn resolves EXACTLY as it does without
  the setting -- no default moves, so nothing costs more;
* set, it sits below everything more specific (the session's own pick, the app's
  own pick such as a Research campaign or the Spec Builder default, a model the
  agent or crew pins) and above the global default, and only for a slot or spawn
  the app owns.

A pin the account cannot run takes the existing path: the Settings write refuses
it, and the turn hands it to the session as the same ``model`` request every other
default rides, which the provider withholds rather than sends.
"""

from __future__ import annotations

import asyncio
import json
import types
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_chat_runner_coverage import _complete, _drive, _runner_state, _set_stream, _slot
from test_chat_send_agent_model_default import _allocation_resolves

from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.config.loader import (
    KiroCrewConfig,
    app_model_pin,
    resolve_app_spawn_model,
    resolve_effective_model,
)
from kiro_crew.config.sections import AppSettingsConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.providers.base import LLMEvent

pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

APP = "demo-app"
GLOBAL_DEFAULT = "claude-opus-5"
APP_PIN = "claude-haiku-5"
CREW_PIN = "claude-sonnet-5"
TEMPLATE_PIN = "claude-sonnet-4.6"
SLOT_PIN = "claude-opus-4.8"


def _load_config(tmp_path: Path, data: dict) -> KiroCrewConfig:
    """A real ``KiroCrewConfig`` loaded from *data* (same shape as config.json)."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


def _config(
    tmp_path: Path, *, app_model: str | None = APP_PIN, default_crew_model: str = ""
) -> KiroCrewConfig:
    """Global ``agent.model`` set; ``apps.demo-app.model`` set unless *app_model* is None."""
    data: dict = {
        "agent": {"model": GLOBAL_DEFAULT, "provider": "acp"},
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "memory_store": "default",
                "model": default_crew_model,
            },
        },
        "default_agent": "default",
    }
    if app_model is not None:
        data["apps"] = {APP: {"model": app_model}}
    return _load_config(tmp_path, data)


# ── The config table ────────────────────────────────────────────────────────


class TestAppsTable:
    def test_absent_table_loads_empty_and_emits_empty(self, tmp_path):
        cfg = _config(tmp_path, app_model=None)
        assert cfg.apps == {}
        assert cfg.to_dict()["apps"] == {}

    def test_a_pick_round_trips(self, tmp_path):
        cfg = _config(tmp_path)
        assert cfg.apps == {APP: AppSettingsConfig(model=APP_PIN)}
        assert cfg.to_dict()["apps"] == {APP: {"model": APP_PIN}}

    def test_an_unmodelled_key_inside_an_entry_survives_a_save(self, tmp_path):
        cfg = _load_config(tmp_path, {"apps": {APP: {"model": APP_PIN, "later": 1}}})
        assert cfg.to_dict()["apps"][APP] == {"model": APP_PIN, "later": 1}

    @pytest.mark.parametrize(
        "raw",
        [
            [],
            "x",
            {APP: "claude-haiku-5"},
            {APP: {"model": 5}},
            {APP: {"model": None}},
        ],
    )
    def test_malformed_input_reads_as_inherit(self, tmp_path, raw):
        cfg = _load_config(tmp_path, {"apps": raw})
        assert app_model_pin(cfg, APP) == ""

    @pytest.mark.parametrize("value", ["", "auto", "  "])
    def test_inherit_spellings_read_empty(self, tmp_path, value):
        cfg = _config(tmp_path, app_model=value)
        assert app_model_pin(cfg, APP) == ""

    def test_an_unknown_app_reads_empty(self, tmp_path):
        cfg = _config(tmp_path)
        assert app_model_pin(cfg, "other-app") == ""
        assert app_model_pin(cfg, "") == ""


# ── The resolver ────────────────────────────────────────────────────────────


@pytest.fixture
def _no_template_pin(monkeypatch):
    """No template pins a model unless a test says so."""
    pins: dict[str, str] = {}
    monkeypatch.setattr(
        KiroCrewConfig,
        "_resolve_named_agent_model",
        staticmethod(lambda name, agents_dir=None: pins.get(name, "")),
    )
    return pins


class TestResolveEffectiveModel:
    @pytest.mark.parametrize("app_model", [None, "", "auto"])
    def test_unset_resolves_exactly_as_without_the_app(self, tmp_path, _no_template_pin, app_model):
        cfg = _config(tmp_path, app_model=app_model)
        assert resolve_effective_model(cfg, None, app_name=APP) == resolve_effective_model(
            cfg, None
        )
        assert resolve_effective_model(cfg, None, app_name=APP) == GLOBAL_DEFAULT

    def test_unset_with_global_auto_still_defers(self, tmp_path, _no_template_pin):
        cfg = _load_config(tmp_path, {"agent": {"model": "auto"}, "apps": {APP: {"model": ""}}})
        with unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")
        ):
            assert resolve_effective_model(cfg, None, app_name=APP) == ""

    def test_the_setting_wins_over_the_global_default(self, tmp_path, _no_template_pin):
        cfg = _config(tmp_path)
        assert resolve_effective_model(cfg, None, app_name=APP) == APP_PIN

    def test_a_crew_pin_wins_over_the_setting(self, tmp_path, _no_template_pin):
        cfg = _config(tmp_path, default_crew_model=CREW_PIN)
        assert resolve_effective_model(cfg, "default", app_name=APP) == CREW_PIN
        # A session on no named agent is a template session, not that crewmate.
        assert resolve_effective_model(cfg, None, app_name=APP) == APP_PIN

    def test_a_template_pin_wins_over_the_setting(self, tmp_path, _no_template_pin, monkeypatch):
        cfg = _config(tmp_path)
        _no_template_pin["demo-app--worker"] = TEMPLATE_PIN
        monkeypatch.setattr(
            "kiro_crew.config.loader._materialized_kiro_agent",
            lambda name, project_dir=None: name or "",
        )
        assert resolve_effective_model(cfg, "demo-app--worker", app_name=APP) == TEMPLATE_PIN

    def test_a_session_that_is_not_the_apps_never_sees_it(self, tmp_path, _no_template_pin):
        cfg = _config(tmp_path)
        assert resolve_effective_model(cfg, None) == GLOBAL_DEFAULT
        assert resolve_effective_model(cfg, None, app_name="other-app") == GLOBAL_DEFAULT


class TestResolveAppSpawnModel:
    @pytest.mark.parametrize("app_model", [None, "", "auto"])
    def test_unset_leaves_the_spawn_alone(self, tmp_path, _no_template_pin, app_model):
        cfg = _config(tmp_path, app_model=app_model)
        assert resolve_app_spawn_model(cfg, APP, "worker") == ""

    def test_the_setting_applies_to_an_unpinned_agent(self, tmp_path, _no_template_pin):
        cfg = _config(tmp_path)
        assert resolve_app_spawn_model(cfg, APP, "worker") == APP_PIN

    def test_a_template_pin_wins(self, tmp_path, _no_template_pin):
        cfg = _config(tmp_path)
        _no_template_pin["worker"] = TEMPLATE_PIN
        assert resolve_app_spawn_model(cfg, APP, "worker") == ""

    def test_a_same_named_crews_pin_does_not_win(self, tmp_path, _no_template_pin):
        """A crew that shares the spawned template's name does not hide the setting.

        An app spawn is a template spawn (``crew_agent=""``), so its session never
        reads that crew's pin. The setting is the model the user chose for the app.
        """
        cfg = _config(tmp_path, default_crew_model=CREW_PIN)
        assert resolve_app_spawn_model(cfg, APP, "default") == APP_PIN

    def test_the_spawn_runs_on_the_apps_model(self, tmp_path, _no_template_pin, monkeypatch):
        """Drive the spawn's own model verdict, as the spawn path allocates it.

        A crew named like the template pins a model. The spawn allocates the
        template with ``crew_agent=""``, so without a model it runs on the global
        default, not on that crew's pin, and with the setting it runs on the app's
        model.
        """
        from kiro_crew.subagent import _spawn_effective_model

        cfg = _config(tmp_path, default_crew_model=CREW_PIN)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
        on_app_pin = cfg.acp_effective_model(None, APP_PIN)
        on_global = cfg.acp_effective_model(None, GLOBAL_DEFAULT)
        assert on_app_pin and on_app_pin != on_global

        assert _spawn_effective_model("", "default", crew_agent="") == on_global
        model = resolve_app_spawn_model(cfg, APP, "default")
        assert _spawn_effective_model(model, "default", crew_agent="") == on_app_pin

    def test_another_apps_setting_does_not_apply(self, tmp_path, _no_template_pin):
        cfg = _config(tmp_path)
        assert resolve_app_spawn_model(cfg, "other-app", "worker") == ""

    def test_the_default_crews_pin_does_not_block_a_spawn(self, tmp_path, _no_template_pin):
        """The spawn's agent tier is the spawn session's, and a pinned crew is not it.

        The ``default`` crewmate pins a model, which is its agent tier when a
        session names it. A spawn of the app's own template is not that crew: its
        session ignores the default crew's pin and starts on the global default.
        Judging the spawn by the crew's tier would therefore drop the per-app
        setting for a spawn that never runs on the crew's model.
        """
        from kiro_crew.config.loader import _agent_tier_model, _model_scope_filter
        from kiro_crew.session import _session_model

        cfg = _config(tmp_path, default_crew_model=CREW_PIN)
        assert _agent_tier_model(cfg, "default", _model_scope_filter(cfg)) == CREW_PIN
        assert _agent_tier_model(cfg, "worker", _model_scope_filter(cfg)) == ""
        assert _session_model(cfg, "worker") == GLOBAL_DEFAULT
        assert resolve_app_spawn_model(cfg, APP, "worker") == APP_PIN


# ── A real turn on an app-owned slot ────────────────────────────────────────


def _pin_sync_accessors(client) -> None:
    client.mcp_session_report = unittest.mock.MagicMock(return_value=None)
    client.available_models = unittest.mock.MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = unittest.mock.MagicMock(return_value=[])


def _turn_state(tmp_path: Path):
    builder = unittest.mock.MagicMock()
    builder.ensure_store = unittest.mock.AsyncMock(return_value=object())
    builder.build_message.return_value = ("fixture context", None)
    state, client = _runner_state(tmp_path, context_builder=builder)
    _pin_sync_accessors(client)
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    _allocation_resolves(state, chat_runner.KiroCrewConfig.load())
    return state, client


def _session_model(state) -> str | None:
    """The model the session this turn allocated starts on, resolved at allocation."""
    state.sessions.get_or_create.assert_awaited_once()
    return (
        state.sessions.allocated_models.get(state.sessions.get_or_create.await_args.args[0]) or None
    )


@pytest.fixture
def _runner_config():
    patchers: list = []

    def _use(cfg: KiroCrewConfig) -> None:
        p = unittest.mock.patch.object(
            chat_runner.KiroCrewConfig, "load", unittest.mock.MagicMock(return_value=cfg)
        )
        p.start()
        patchers.append(p)

    yield _use
    for p in patchers:
        p.stop()


def _app_slot(app: str = APP):
    slot = _slot()
    slot._app = app
    return slot


class TestAppSlotTurn:
    @pytest.mark.asyncio
    async def test_unset_app_slot_runs_on_the_global_default(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path, app_model=""))
        state, _client = _turn_state(tmp_path)
        slot = _app_slot()

        await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_the_setting_is_the_model_the_session_requests(self, tmp_path, _runner_config):
        """The per-app id rides the same ``model`` request as any default.

        That request is what the provider judges against the account's advertised
        models and withholds when it cannot run it, so an unservable per-app id
        takes the existing withhold. It is a session input, never a slot pin.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _app_slot()

        await _drive(state, slot)

        assert _session_model(state) == APP_PIN
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_an_explicit_slot_pick_wins(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _app_slot()
        slot.model = SLOT_PIN

        await _drive(state, slot)

        assert _session_model(state) == SLOT_PIN

    @pytest.mark.asyncio
    @pytest.mark.parametrize("app", ["spec-builder", "auto-research"])
    async def test_the_apps_own_pick_wins(self, tmp_path, _runner_config, app):
        """Spec Builder and Research stamp their pick on the worker slot's model."""
        cfg = _load_config(
            tmp_path,
            {"agent": {"model": GLOBAL_DEFAULT}, "apps": {app: {"model": APP_PIN}}},
        )
        _runner_config(cfg)
        state, _client = _turn_state(tmp_path)
        slot = _app_slot(app)
        slot.model = SLOT_PIN

        await _drive(state, slot)

        assert _session_model(state) == SLOT_PIN

    @pytest.mark.asyncio
    async def test_a_crew_pin_wins(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path, default_crew_model=CREW_PIN))
        state, _client = _turn_state(tmp_path)
        slot = _app_slot()
        slot.agent = "default"

        await _drive(state, slot)

        assert _session_model(state) == CREW_PIN

    @pytest.mark.asyncio
    async def test_a_slot_no_app_owns_is_never_affected(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()

        await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_the_prewarmed_session_requests_the_same_model(self, tmp_path, _runner_config):
        _runner_config(_config(tmp_path))
        state, client = _runner_state(tmp_path)
        _pin_sync_accessors(client)
        _allocation_resolves(state, chat_runner.KiroCrewConfig.load())
        slot = _app_slot()
        state._slots[slot.key] = slot
        state.sessions.release = unittest.mock.MagicMock()
        state.sessions.remove_if_unclaimed = unittest.mock.AsyncMock(return_value=True)

        with unittest.mock.patch.object(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0):
            await _eager_spawn(state, slot)

        assert _session_model(state) == APP_PIN
        assert slot.model == ""


# ── An app's background spawn ──────────────────────────────────────────────


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn(self, task: str, **kwargs: object) -> object:
        self.calls.append({"task": task, **kwargs})
        return types.SimpleNamespace(id="sub-1", error="")


class TestAppSpawn:
    @pytest.fixture(autouse=True)
    def _known_agent(self, _floor_monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        _floor_monkeypatch.setattr(
            spawn_sdk,
            "list_agents",
            lambda: [types.SimpleNamespace(name="worker", filename=f"{APP}--worker.json")],
        )

    def _spawn(self, cfg: KiroCrewConfig, model: str = "") -> dict:
        from kiro_crew.apps.spawn_sdk import SpawnSDK, build_spawn_impl

        mgr = _FakeManager()
        with unittest.mock.patch.object(KiroCrewConfig, "load", return_value=cfg):
            asyncio.run(SpawnSDK(APP, build_spawn_impl(mgr)).run("t", "worker", model=model))
        return mgr.calls[0]

    @pytest.mark.parametrize("app_model", [None, "", "auto"])
    def test_unset_spawns_exactly_as_today(self, tmp_path, _no_template_pin, app_model):
        assert self._spawn(_config(tmp_path, app_model=app_model))["model"] is None

    def test_the_setting_fills_a_spawn_with_no_pick(self, tmp_path, _no_template_pin):
        assert self._spawn(_config(tmp_path))["model"] == APP_PIN

    def test_an_explicit_spawn_model_wins(self, tmp_path, _no_template_pin):
        assert self._spawn(_config(tmp_path), model=SLOT_PIN)["model"] == SLOT_PIN

    def test_the_agents_own_pin_wins(self, tmp_path, _no_template_pin):
        _no_template_pin["worker"] = TEMPLATE_PIN
        assert self._spawn(_config(tmp_path))["model"] is None

    def test_an_unreadable_config_leaves_the_spawn_alone(self, tmp_path):
        from kiro_crew.apps.spawn_sdk import SpawnSDK, build_spawn_impl

        mgr = _FakeManager()
        with unittest.mock.patch.object(KiroCrewConfig, "load", side_effect=OSError("boom")):
            asyncio.run(SpawnSDK(APP, build_spawn_impl(mgr)).run("t", "worker"))
        assert mgr.calls[0]["model"] is None


# ── The Settings write ──────────────────────────────────────────────────────


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _patch_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = unittest.mock.MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def _cfg_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agent": {"approval_mode": "auto"}}), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


@pytest.fixture
def _advertised(monkeypatch):
    """The account serves only ``claude-haiku-5``."""
    from kiro_crew.dashboard.handlers import core

    monkeypatch.setattr(
        core, "_active_advertised_ids", lambda request, backend=None: ["claude-haiku-5"]
    )
    monkeypatch.setattr(
        core, "_revalidate_role_pin_evidence", unittest.mock.AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
        lambda m, provider=None: None,
    )


class TestSettingsWrite:
    @pytest.mark.parametrize(
        "path",
        ["apps.demo-app.model", "apps.a.model", "apps.app-2.model"],
    )
    def test_a_per_app_model_path_is_editable(self, path):
        from kiro_crew.dashboard.handlers import core

        spec = core._editable_config_spec(path)
        assert spec is not None
        assert spec["validate_fn"] is core._validate_role_model

    @pytest.mark.parametrize(
        "path",
        [
            "apps.Demo.model",
            "apps.demo_app.model",
            "apps..model",
            "apps.demo-app.other",
            "apps.demo-app",
            "apps.demo-app.model.x",
            "apps.-demo.model",
            "apps.demo\n.model",
            "apps.demo-app.model\n",
            ["apps"],
            None,
        ],
    )
    def test_any_other_apps_path_is_not(self, path):
        from kiro_crew.dashboard.handlers import core

        assert core._editable_config_spec(path) is None

    def test_the_app_name_grammar_is_the_manifests(self):
        # One spelling of the app-name grammar: the PATCH allowlist reads the
        # manifest's ``KEBAB_RE`` rather than restating it, so the two cannot drift.
        from kiro_crew.apps import manifest
        from kiro_crew.dashboard.handlers import core

        assert core.KEBAB_RE is manifest.KEBAB_RE
        assert not hasattr(core, "_APP_MODEL_PATH_RE")

    @pytest.mark.asyncio
    async def test_a_pick_is_written_under_the_app(self, _cfg_file, _advertised):
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": f"apps.{APP}.model", "value": APP_PIN}
            )
            assert resp.status == 200
        data = json.loads(_cfg_file.read_text(encoding="utf-8"))
        assert data["apps"][APP]["model"] == APP_PIN
        assert data["agent"]["approval_mode"] == "auto"

    @pytest.mark.asyncio
    async def test_inherit_is_always_accepted(self, _cfg_file, _advertised):
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": f"apps.{APP}.model", "value": ""}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_model_the_account_cannot_run_is_refused(self, _cfg_file, _advertised):
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew",
                json={"path": f"apps.{APP}.model", "value": "claude-opus-5"},
            )
            assert resp.status == 400
            assert "not available" in (await resp.json())["error"]
        assert "apps" not in json.loads(_cfg_file.read_text(encoding="utf-8"))

    @pytest.mark.asyncio
    async def test_a_malformed_id_is_refused(self, _cfg_file, _advertised):
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew",
                json={"path": f"apps.{APP}.model", "value": "bad; rm -rf /"},
            )
            assert resp.status == 400


# ── The model chip's resolver ───────────────────────────────────────────────


class TestResolvedModelEndpoint:
    """The composer chip reads this endpoint, so it must name the app's model too."""

    async def _get(self, cfg: KiroCrewConfig, params: dict) -> dict:
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

        app = web.Application()
        app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
        with unittest.mock.patch.object(KiroCrewConfig, "load", return_value=cfg):
            async with TestClient(TestServer(app)) as client:
                return await (await client.get("/api/agents/resolved-model", params=params)).json()

    @pytest.mark.asyncio
    async def test_the_owning_app_is_part_of_the_answer(self, tmp_path, _no_template_pin):
        body = await self._get(_config(tmp_path), {"agent": "", "app": APP})
        assert body["model"] == APP_PIN

    @pytest.mark.asyncio
    async def test_without_an_app_the_answer_is_unchanged(self, tmp_path, _no_template_pin):
        body = await self._get(_config(tmp_path), {"agent": ""})
        assert body["model"] == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("app", ["Demo", "../x", "demo_app", ""])
    async def test_a_value_that_is_not_an_app_name_is_ignored(
        self, tmp_path, _no_template_pin, app
    ):
        cfg = _load_config(
            tmp_path, {"agent": {"model": GLOBAL_DEFAULT}, "apps": {app or "x": {"model": APP_PIN}}}
        )
        body = await self._get(cfg, {"agent": "", "app": app})
        assert body["model"] == GLOBAL_DEFAULT

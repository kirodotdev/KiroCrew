"""A Bot's ``worker_environment``: the session environment its WORKERS run on.

The Bot's profile names one session environment profile; a worker it opens
through ``session_create`` is bound to it before anything starts. The Bot's own
chat is never moved by it. Only a Coder, Sprites or boxd profile counts, and only
for a worker on KAS: Kiro Cloud sends no MCP servers, so a worker there could not
report back. A profile that is gone leaves the worker here, with the dropped id in
the create's audit record. No remote workspace is created: ``_profiles`` is faked
and the choice store is the real one under the test home.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import _make_state
from test_mate_worker_dispatch import crewmate  # noqa: F401 -- the Bot conductor fixture
from test_session_control_create_inherits_model import _MEMBER, _crewmate_caller, _crewmate_home

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers import session_environments as envs
from kiro_crew.mate_worker import MATE_WORKER_AGENT

_CODER = {"id": "cdr", "name": "Coder box", "provider": "coder", "workdir": "/workspace"}
_SPRITE = {"id": "spr", "name": "Sprite", "provider": "sprites", "workdir": "/work"}
_BOXD = {"id": "bxd", "name": "boxd VM", "provider": "boxd", "workdir": "/work"}
_CLOUD = {"id": "cld", "name": "Cloud", "provider": "kiro_cloud", "workdir": ""}


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture()
def profiles(monkeypatch):
    """Session environments on, with one profile of each provider saved."""
    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    saved = [_CODER, _SPRITE, _BOXD, _CLOUD]
    monkeypatch.setattr(envs, "_profiles", lambda: [dict(p) for p in saved])
    return saved


@pytest.fixture()
def spawns(monkeypatch):
    """The prewarms the create arms, by slot key; none starts."""
    armed: list[str] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.schedule_eager_spawn",
        lambda _state, slot, **_kw: armed.append(slot.key),
    )
    return armed


@pytest.fixture()
def bot(crewmate, tmp_path, monkeypatch):  # noqa: F811 -- the imported fixture
    """``pixel``'s conductor chat, whose ``session_create`` opens its own worker.

    ``make(backend, **record)`` writes *record* onto pixel's profile and runs its
    workers on *backend*, without that harness installed."""

    def make(backend: str = "kas", **record):
        state, caller, _execution = crewmate
        path = tmp_path / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["agents"]["pixel"].update(record)
        path.write_text(json.dumps(config), encoding="utf-8")
        loader._invalidate_config_cache()
        monkeypatch.setattr("kiro_crew.mate_worker.crewmate_worker_backend", lambda *_a: backend)
        monkeypatch.setattr(
            "kiro_crew.mate_worker.worker_kit_backend_refusal", lambda *_a, **_k: None
        )
        # A newborn worker has no session registered yet (the helper's is a mock).
        state.sessions.has_session = lambda _key: False
        return state, caller

    return make


def _create_audited(state, caller, monkeypatch, **kwargs):
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    created = asyncio.run(
        sc.create_session(state, caller_session_key=slot_history_key(caller), **kwargs)
    )
    detail = next(a for a in audits if a["operation"] == "create")["detail"]
    return created, detail


def _choice(slot) -> str | None:
    return envs._choices().get(effective_session_key(slot))


class TestBotWorkerIsBound:
    @pytest.mark.parametrize("profile", [_CODER, _SPRITE, _BOXD], ids=lambda p: p["provider"])
    def test_the_worker_starts_on_the_bots_choice(
        self, bot, monkeypatch, profiles, spawns, profile
    ):
        state, caller = bot(worker_environment=profile["id"])

        created, detail = _create_audited(state, caller, monkeypatch)

        child = state.get_slot(created["target"])
        assert child.agent == MATE_WORKER_AGENT
        assert detail["worker_environment"] == profile["id"]
        assert detail["worker_environment_dropped"] == ""
        assert _choice(child) == profile["id"]
        # Not prewarmed: a local spawn would run here, a remote one starts billed compute.
        assert child.key not in spawns

    def test_the_bots_own_chat_is_never_bound(self, bot, monkeypatch, profiles, spawns):
        state, caller = bot(worker_environment="cdr")

        _create_audited(state, caller, monkeypatch)

        assert _choice(caller) is None


class TestWorkerStaysOnThisMachine:
    @staticmethod
    def _assert_local(state, created, spawns, detail, dropped: str) -> None:
        child = state.get_slot(created["target"])
        assert _choice(child) is None
        assert child.key in spawns
        assert detail["worker_environment"] == ""
        assert detail["worker_environment_dropped"] == dropped

    def test_a_bot_with_the_setting_empty(self, bot, monkeypatch, profiles, spawns):
        state, caller = bot()

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="")

    def test_a_removed_profile_is_dropped_and_audited(self, bot, monkeypatch, profiles, spawns):
        state, caller = bot(worker_environment="gone")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="gone")

    def test_a_kiro_cloud_profile_is_never_used(self, bot, monkeypatch, profiles, spawns):
        """Kiro Cloud sends no MCP servers: the worker could not report to its Bot."""
        state, caller = bot(worker_environment="cld")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="cld")

    def test_a_worker_off_kas_is_not_bound(self, bot, monkeypatch, profiles, spawns):
        """Every harness but KAS refuses a remote workspace at its first start."""
        state, caller = bot(backend="claude", worker_environment="cdr")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="cdr")

    def test_session_environments_off(self, bot, monkeypatch, spawns):
        """The real `_profiles`, with the developer flag unset: nothing is offered."""
        monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
        state, caller = bot(worker_environment="cdr")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="cdr")

    def test_no_eligible_profile_saved(self, bot, monkeypatch, spawns):
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
        monkeypatch.setattr(envs, "_profiles", lambda: [dict(_CLOUD)])
        state, caller = bot(worker_environment="cdr")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="cdr")

    def test_a_bind_that_fails_is_audited_as_dropped(self, bot, monkeypatch, profiles, spawns):
        async def refuse(*_a, **_k):
            return False

        monkeypatch.setattr(envs, "apply_default_environment", refuse)
        state, caller = bot(worker_environment="cdr")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="cdr")

    def test_a_kas_child_of_a_bot_whose_workers_run_elsewhere(
        self, bot, monkeypatch, profiles, spawns
    ):
        """The bot names KAS on the call, but its workers run on Claude Code, which
        the Settings row was computed for (and hid): the choice does not apply."""
        state, caller = bot(backend="claude", worker_environment="cdr")

        created, detail = _create_audited(state, caller, monkeypatch, backend="kas")

        self._assert_local(state, created, spawns, detail, dropped="cdr")

    @pytest.mark.parametrize(("workers_on", "bound"), [("claude", False), ("kas", True)])
    def test_a_non_conductor_bots_child_on_the_default_harness(
        self, tmp_path, monkeypatch, profiles, spawns, workers_on, bound
    ):
        """The child runs on `agent.acp_backend` (KAS here). It takes the choice
        only when the bot's workers run there too, as the Settings row says."""
        monkeypatch.setattr("kiro_crew.mate_worker.crewmate_worker_backend", lambda *_a: workers_on)
        state, caller = _crewmate_caller(tmp_path, monkeypatch, worker_environment="cdr")
        state.sessions.has_session = lambda _key: False
        path = tmp_path / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config.setdefault("agent", {})["acp_backend"] = "kas"
        path.write_text(json.dumps(config), encoding="utf-8")
        loader._invalidate_config_cache()

        created, detail = _create_audited(state, caller, monkeypatch)

        if bound:
            assert _choice(state.get_slot(created["target"])) == "cdr"
            assert detail["worker_environment"] == "cdr"
        else:
            self._assert_local(state, created, spawns, detail, dropped="cdr")

    def test_an_id_less_bot_is_judged_by_its_own_record(
        self, tmp_path, monkeypatch, profiles, spawns
    ):
        """A legacy bot with no `member_id` is identified by its selection name; its
        own harness pin (KAS) judges the bind, not the install-wide default."""
        from kiro_crew.config.loader import member_template_id
        from kiro_crew.execution_context import (
            ExecutionContext,
            MemoryStoreRef,
            bind_session_execution,
        )

        monkeypatch.setattr(
            "kiro_crew.mate_worker.crewmate_worker_backend",
            lambda _cfg, crew=None: "kas" if crew == _MEMBER else "claude",
        )
        monkeypatch.setattr(
            "kiro_crew.mate_worker.worker_kit_backend_refusal", lambda *_a, **_k: None
        )
        state, caller = _crewmate_caller(tmp_path, monkeypatch, worker_environment="cdr")
        state.sessions.has_session = lambda _key: False
        record = KiroCrewConfig.load().agents[_MEMBER]
        id_less = ExecutionContext(
            None,
            MemoryStoreRef("default"),
            "member",
            member_template_id(record),
            "persistent",
            selection_name=_MEMBER,
        )
        legacy = state.get_or_create_slot("chat-legacy-bot", workspace=caller.workspace)
        bind_session_execution(slot_history_key(legacy), id_less, vouch=True)

        created, detail = _create_audited(state, legacy, monkeypatch, backend="kas")

        assert detail["worker_environment"] == "cdr"
        assert _choice(state.get_slot(created["target"])) == "cdr"

    def test_a_bot_starting_another_bot(self, tmp_path, monkeypatch, profiles, spawns):
        """The child runs its own profile, so the dispatcher's choice is not copied."""
        state, caller = _crewmate_caller(tmp_path, monkeypatch, worker_environment="cdr")

        created, detail = _create_audited(
            state, caller, monkeypatch, agent="peer", caller_fenced=False
        )

        self._assert_local(state, created, spawns, detail, dropped="")

    def test_a_caller_that_is_no_bot(self, tmp_path, monkeypatch, profiles, spawns):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        created, detail = _create_audited(state, caller, monkeypatch)

        self._assert_local(state, created, spawns, detail, dropped="")


class TestApplyWithAProfile:
    """``apply_default_environment(profile=...)``: the private-chat rule still holds."""

    @staticmethod
    def _slot(tmp_path, memory_mode: str):
        slot = _make_state(tmp_path).get_or_create_slot("chat-w")
        slot.memory_mode = memory_mode
        return slot

    def test_a_private_chat_still_refuses_kiro_cloud(self, tmp_path, profiles):
        slot = self._slot(tmp_path, "ephemeral")

        assert asyncio.run(envs.apply_default_environment(slot, profile="cld")) is False
        assert _choice(slot) is None

    def test_a_private_chat_takes_a_vm_profile(self, tmp_path, profiles):
        """Only Kiro Cloud needs a persistent chat, as on the chat's own picker."""
        slot = self._slot(tmp_path, "ephemeral")

        assert asyncio.run(envs.apply_default_environment(slot, profile="cdr")) is True
        assert _choice(slot) == "cdr"

    def test_the_profile_outranks_the_default(self, tmp_path, profiles):
        cfg = KiroCrewConfig.load()
        cfg.session_environments.default_profile = "spr"
        cfg.save()
        slot = self._slot(tmp_path, "persistent")

        assert asyncio.run(envs.apply_default_environment(slot, profile="bxd")) is True
        assert _choice(slot) == "bxd"


class TestEligibleProfiles:
    def test_the_provider_is_judged_on_the_read_the_choice_records(self, tmp_path, monkeypatch):
        """An id re-saved as Kiro Cloud before the bind is refused, not recorded as cloud."""
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
        monkeypatch.setattr(envs, "_profiles", lambda: [dict(_CLOUD, id="cdr")])
        slot = _make_state(tmp_path).get_or_create_slot("chat-w")

        bound = asyncio.run(
            envs.apply_default_environment(
                slot, profile="cdr", allowed_providers=envs.WORKER_ENVIRONMENT_PROVIDERS
            )
        )

        assert bound is False
        assert _choice(slot) is None

    def test_a_worker_bind_checks_the_provider_at_bind_time(self, bot, monkeypatch, spawns):
        """`bind_worker_environment` reads the profiles once, inside the bind."""
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
        monkeypatch.setattr(envs, "_profiles", lambda: [dict(_CLOUD, id="cdr")])
        state, caller = bot(worker_environment="cdr")

        created, detail = _create_audited(state, caller, monkeypatch)

        assert _choice(state.get_slot(created["target"])) is None
        assert detail["worker_environment_dropped"] == "cdr"

    def test_kas_gets_the_vm_profiles_only(self, profiles):
        assert [p["id"] for p in envs.worker_environment_profiles("kas")] == ["cdr", "spr", "bxd"]

    @pytest.mark.parametrize("backend", ["claude", "codex", "kiro", ""])
    def test_any_other_harness_gets_none(self, profiles, backend):
        assert envs.worker_environment_profiles(backend) == []


class TestProfileWritesTheChoice:
    """``PUT /api/agents/{name}`` stores ``worker_environment``, shape-checked."""

    @pytest.fixture(autouse=True)
    def _owner_caller(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.fixture()
    def seeded_agent(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["existing"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="default", memory_store="default"
        )
        cfg.save()
        return "existing"

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

        app = web.Application()
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        return app

    @pytest.mark.asyncio
    async def test_stores_and_clears(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"worker_environment": " cdr "}
            )
            assert resp.status == 200, await resp.text()
            assert KiroCrewConfig.load().agents[seeded_agent].worker_environment == "cdr"
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"worker_environment": ""})
            assert resp.status == 200

        assert KiroCrewConfig.load().agents[seeded_agent].worker_environment == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [3, None, "not an id", "-x", "x" * 65])
    async def test_a_bad_value_writes_nothing(self, seeded_agent, bad):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "worker_environment": bad},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_worker_environment"

        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.worker_environment == ""
        assert stored.workspace == "default"


class TestTheCardReadsIt:
    @pytest.mark.asyncio
    async def test_only_the_owner_readout_names_the_profiles(self, monkeypatch, profiles):
        """`worker_environments` rides the owner-only harness half of the readout."""
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="default", memory_store="default"
        )
        cfg.save()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.session_environments.bot_worker_backend",
            lambda *_a: "kas",
        )
        app = web.Application()
        app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
        bodies = {}
        for owner in (True, False):
            monkeypatch.setattr(
                "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
                lambda request, owner=owner: owner,
            )
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/agents/resolved-model", params={"agent": "atlas"})
                bodies[owner] = await resp.json()

        assert [p["id"] for p in bodies[True]["worker_environments"]] == ["cdr", "spr", "bxd"]
        assert "worker_environments" not in bodies[False]

    @pytest.mark.asyncio
    async def test_the_roster_row_carries_the_choice(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers.members import api_members

        _crewmate_home(tmp_path, monkeypatch, worker_environment="cdr")
        app = web.Application()
        app["state"] = SimpleNamespace(owner_id="", _slots={}, conversation_log=None)
        request = make_mocked_request("GET", "/api/members", app=app)
        request["app"] = ""
        request["user"] = "local-app"

        rows = {r["name"]: r for r in json.loads((await api_members(request)).body)["members"]}

        assert rows[_MEMBER]["worker_environment"] == "cdr"
        assert rows["peer"]["worker_environment"] == ""

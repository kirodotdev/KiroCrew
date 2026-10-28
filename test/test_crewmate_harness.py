"""A crewmate's own harness pin (``agents.<name>.acp_backend``).

The properties held here:

1. The pin is one tier of the ONE selection gate (``members.select_provider_backend``):
   below an explicit ``spawn_run(backend=...)``, above the member route and the
   configured default, and every caller of the gate passes it.
2. It reaches the crewmate's own DM thread and the worker sessions it opens, and no
   other session.
3. It fails CLOSED: a pin this gateway cannot start refuses, it never runs the
   crewmate on another harness. The refusal is the pre-gate check
   (``crewmate_backend_pin``, harness-parity H17); the gate itself never raises.
4. The model pins are judged against the pinned harness at save time.
5. An unset pin leaves every answer exactly as it was.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

MEMBER_ID = "atlas-0123456789ab"
DM_KEY = f"dashboard:member-{MEMBER_ID}"
SELECTABLE = {"", "kas", "claude"}


@pytest.fixture(autouse=True)
def _selectable(_floor_monkeypatch):
    """A fixed selectable set: kiro-cli, KAS and Claude Code."""
    from kiro_crew.agent_sdk import backends

    _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset(SELECTABLE))


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Exercise the agent handlers past their independent owner-auth boundary."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _cfg(pin: str = "", member_backend: str = "kas", kas_bundle: bool = False) -> SimpleNamespace:
    """A config shaped as the gate's readers see it."""
    return SimpleNamespace(
        agent=SimpleNamespace(
            member_acp_backend=member_backend, acp_backend="kas", kas_bundle=kas_bundle
        ),
        agents={
            "atlas": KiroCrewAgentConfig(member_id=MEMBER_ID, kiro_agent="atlas", acp_backend=pin),
            "other": KiroCrewAgentConfig(member_id="other-0123456789ab", kiro_agent="other"),
        },
    )


def _startable(stored: str):
    """A pin for crewmate ``atlas`` that the pre-gate check has confirmed."""
    from kiro_crew.crewmate_harness import CrewBackendPin

    return CrewBackendPin("atlas", stored).checked()


class TestSelectionGate:
    def test_the_pin_outranks_the_member_route(self):
        from kiro_crew.members import select_provider_backend

        pin = _startable("claude")
        assert select_provider_backend(DM_KEY, "kas", "kas", crew_backend=pin) == "claude"

    def test_kiro_is_a_real_pin(self):
        from kiro_crew.members import select_provider_backend

        pin = _startable("kiro")
        assert select_provider_backend(DM_KEY, "kas", "kas", crew_backend=pin) == ""

    def test_an_explicit_override_still_wins(self):
        from kiro_crew.members import select_provider_backend

        pin = _startable("claude")
        assert (
            select_provider_backend(DM_KEY, "kas", "kas", backend_override="kas", crew_backend=pin)
            == "kas"
        )

    def test_no_pin_leaves_the_member_route_as_it_was(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend(DM_KEY, "kas", "claude", crew_backend=None) == "kas"
        assert select_provider_backend("dashboard_x", "kas", "claude", crew_backend=None) == (
            "claude"
        )

    def test_the_gate_never_raises_for_a_crew_pin(self, _floor_monkeypatch):
        """H17: the gate takes a confirmed pin and has no refusal of its own.

        Selectability is not consulted on the pin's arm at all, so even a harness
        that drops out between the check and the call cannot make the gate raise.
        """
        from kiro_crew.agent_sdk import backends
        from kiro_crew.members import select_provider_backend

        pin = _startable("claude")

        def _not_consulted():
            raise AssertionError("the gate re-judged a pin the pre-gate check confirmed")

        _floor_monkeypatch.setattr(backends, "selectable_backends", _not_consulted)
        assert select_provider_backend(DM_KEY, "kas", "kas", crew_backend=pin) == "claude"


class TestThePreGateCheck:
    """H17: an unstartable pin refuses BEFORE the gate, and never falls back."""

    @pytest.mark.parametrize("stored", ["codex", "no-such-harness"])
    def test_an_unstartable_pin_refuses_instead_of_falling_back(self, stored):
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin

        with pytest.raises(CrewmateHarnessUnavailable) as raised:
            crewmate_backend_pin(_cfg(stored), DM_KEY)
        message = str(raised.value)
        assert raised.value.code == "crewmate_harness_unavailable"
        assert "atlas" in message and raised.value.stored == stored

    def test_a_harness_that_stopped_being_selectable_refuses(self, _floor_monkeypatch):
        """Saved while selectable, then denied or marked unservable: refused next turn."""
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin

        cfg = _cfg("claude")
        assert crewmate_backend_pin(cfg, DM_KEY).backend == "claude"
        _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas"}))
        with pytest.raises(CrewmateHarnessUnavailable):
            crewmate_backend_pin(cfg, DM_KEY)

    def test_the_stored_read_names_an_unstartable_pin_without_raising(self):
        from kiro_crew.crewmate_harness import crewmate_stored_pin

        pin = crewmate_stored_pin(_cfg("codex"), DM_KEY)
        assert pin is not None and pin.stored == "codex"

    @pytest.mark.parametrize("stored", ["codex", "claude"])
    def test_the_provider_factory_refuses_before_the_gate(
        self, stored, _floor_monkeypatch, monkeypatch
    ):
        """DM chat and worker sessions are built here: the refusal lands first."""
        from kiro_crew import members
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

        cfg = KiroCrewConfig()
        cfg.agents["atlas"] = KiroCrewAgentConfig(
            member_id=MEMBER_ID, kiro_agent="atlas", acp_backend=stored
        )
        # "claude" was selectable when it was saved and has since dropped out.
        _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas"}))
        gate_calls: list[object] = []
        monkeypatch.setattr(
            members, "select_provider_backend", lambda *a, **k: gate_calls.append(a)
        )
        factory = cfg.create_provider_factory()
        with pytest.raises(CrewmateHarnessUnavailable):
            factory(session_key=DM_KEY, agent="atlas")
        assert gate_calls == [], "the gate ran for a pin the pre-gate check refuses"


class TestExplicitBackendBeforePinCheck:
    @pytest.mark.parametrize("override", ["kas", ""])
    @pytest.mark.parametrize("worker", [False, True])
    def test_factory_override_skips_an_unstartable_pin(
        self, override, worker, monkeypatch, _floor_monkeypatch
    ):
        from kiro_crew import members
        from kiro_crew.agent_sdk import backends
        from kiro_crew.agent_sdk.drivers import acp
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

        cfg = KiroCrewConfig()
        cfg.agents["atlas"] = KiroCrewAgentConfig(
            member_id=MEMBER_ID, kiro_agent="atlas", acp_backend="claude"
        )
        key = "dashboard:override-worker" if worker else DM_KEY
        if worker:
            monkeypatch.setattr(members, "_CREWMATE_WORKER_SLOTS", set())
            monkeypatch.setattr(members, "_CREWMATE_WORKER_MEMBERS", {})
            members.note_crewmate_worker_session(key, MEMBER_ID)
        _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas"}))
        monkeypatch.setattr(acp, "build_acp_provider", lambda **kwargs: kwargs)
        factory = cfg.create_provider_factory()
        result = factory(session_key=key, agent="atlas", backend_override=override)
        assert result["acp_backend"] == override
        with pytest.raises(CrewmateHarnessUnavailable):
            factory(session_key=key, agent="atlas")

    @pytest.mark.parametrize("override", ["kas", ""])
    def test_model_resolution_uses_override_despite_unstartable_pin(
        self, override, monkeypatch, _floor_monkeypatch
    ):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.config import loader
        from kiro_crew.session_parts.policy import _session_model

        cfg = KiroCrewConfig()
        cfg.agents["atlas"] = KiroCrewAgentConfig(
            member_id=MEMBER_ID, kiro_agent="atlas", acp_backend="claude"
        )
        _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas"}))
        seen: list[str | None] = []

        def resolve(config, crew, *, app_name, backend):
            seen.append(backend)
            return "saved-model"

        monkeypatch.setattr(loader, "resolve_effective_model", resolve)
        assert _session_model(cfg, "atlas", session_key=DM_KEY, backend_override=override) == (
            "saved-model"
        )
        assert seen == [override]
        # The default-model resolver fails soft, but never resolves a model on
        # another harness when the selecting pin cannot start. The factory refuses.
        seen.clear()
        assert _session_model(cfg, "atlas", session_key=DM_KEY) is None
        assert seen == []


class TestWhichSessionsTakeThePin:
    @pytest.mark.parametrize(
        "key",
        [
            DM_KEY,
            f"dashboard_member-{MEMBER_ID}",
            f"member-{MEMBER_ID}.memory-member-atlas-store",
        ],
    )
    def test_the_crewmates_own_thread_in_every_spelling(self, key):
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        pin = crewmate_backend_pin(_cfg("claude"), key)
        assert pin is not None and (pin.crew, pin.stored) == ("atlas", "claude")

    def test_the_crewmates_worker(self):
        from kiro_crew import members
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        members.note_crewmate_worker_session("dashboard:w-1", MEMBER_ID)
        pin = crewmate_backend_pin(_cfg("claude"), "dashboard:w-1")
        assert pin is not None and pin.crew == "atlas"

    def test_a_forgotten_worker_takes_no_pin(self):
        from kiro_crew import members
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        members.note_crewmate_worker_session("dashboard:w-1", MEMBER_ID)
        members.forget_crewmate_worker_session("dashboard:w-1")
        assert crewmate_backend_pin(_cfg("claude"), "dashboard:w-1") is None

    @pytest.mark.parametrize("key", ["dashboard_chat-1", "subagent:abc", None, ""])
    def test_any_other_session_takes_no_pin(self, key):
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        assert crewmate_backend_pin(_cfg("claude"), key) is None

    def test_another_crewmates_thread_takes_its_own_pin(self):
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        assert crewmate_backend_pin(_cfg("claude"), "dashboard:member-other-0123456789ab") is None

    def test_an_unset_pin_is_none(self):
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        assert crewmate_backend_pin(_cfg(""), DM_KEY) is None


class TestWorkerBackend:
    def test_the_worker_runs_on_its_crewmates_pin(self):
        from kiro_crew.mate_worker import crewmate_worker_backend

        assert crewmate_worker_backend(_cfg("claude"), "atlas") == "claude"

    def test_without_a_pin_the_member_route_decides(self):
        from kiro_crew.mate_worker import crewmate_worker_backend

        assert crewmate_worker_backend(_cfg(""), "atlas") == "kas"
        assert crewmate_worker_backend(_cfg("claude")) == "kas"

    def test_an_unstartable_pin_refuses(self):
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable
        from kiro_crew.mate_worker import crewmate_worker_backend

        with pytest.raises(CrewmateHarnessUnavailable):
            crewmate_worker_backend(_cfg("codex"), "atlas")


class TestAKiroPinOnAV3KiroCli:
    """A stored ``kiro`` pin reads as a stored ``agent.acp_backend`` does: KAS once
    the host's kiro-cli is confirmed to offer v3, off the KAS bundle route
    (the decision record ``docs/decisions/*-a-kiro-cli-choice-runs-on-kas.md``)."""

    def test_confirmed_v3_runs_the_dm_and_the_worker_on_kas(self):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_backend_pin
        from kiro_crew.mate_worker import crewmate_worker_backend
        from kiro_crew.members import select_provider_backend

        backends.confirm_kiro_cli_offers_kas()
        pin = crewmate_backend_pin(_cfg("kiro"), DM_KEY)
        assert pin is not None and pin.backend == "kas"
        assert pin.stored == "kiro"
        assert select_provider_backend(DM_KEY, "kas", "kas", crew_backend=pin) == "kas"
        assert crewmate_worker_backend(_cfg("kiro"), "atlas") == "kas"

    def test_an_unconfirmed_kiro_cli_keeps_the_kiro_cli_launch(self):
        from kiro_crew.crewmate_harness import crewmate_backend_pin
        from kiro_crew.mate_worker import crewmate_worker_backend

        pin = crewmate_backend_pin(_cfg("kiro"), DM_KEY)
        assert pin is not None and pin.backend == ""
        assert crewmate_worker_backend(_cfg("kiro"), "atlas") == ""

    def test_the_kas_bundle_route_keeps_the_kiro_cli_launch(self):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_backend_pin
        from kiro_crew.mate_worker import crewmate_worker_backend

        backends.confirm_kiro_cli_offers_kas()
        pin = crewmate_backend_pin(_cfg("kiro", kas_bundle=True), DM_KEY)
        assert pin is not None and pin.backend == ""
        assert crewmate_worker_backend(_cfg("kiro", kas_bundle=True), "atlas") == ""

    def test_kas_not_selectable_keeps_the_kiro_cli_launch(self, _floor_monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        _floor_monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({""}))
        backends.confirm_kiro_cli_offers_kas()
        pin = crewmate_backend_pin(_cfg("kiro"), DM_KEY)
        assert pin is not None and pin.backend == ""

    def test_the_catalog_names_the_harness_that_runs(self):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.dashboard.handlers import agent_catalog

        assert agent_catalog._session_backend(_cfg("kiro"), DM_KEY) == "kiro"
        backends.confirm_kiro_cli_offers_kas()
        assert agent_catalog._session_backend(_cfg("kiro"), DM_KEY) == "kas"


class TestDispatchBlock:
    def test_the_operating_block_follows_the_pin(self, monkeypatch):
        from kiro_crew import context

        # kiro-cli cannot mount the dispatch tools; KAS can.
        assert context._member_backend_can_dispatch(_cfg("", "kas"), "atlas") is True
        assert context._member_backend_can_dispatch(_cfg("kiro", "kas"), "atlas") is False

    def test_a_kiro_pin_on_v3_dispatches_like_an_inherited_kiro(self):
        """On a confirmed v3 kiro-cli a pinned ``kiro`` runs on KAS, as an unpinned
        crewmate inheriting a stored ``kiro`` member route does, so both get the block."""
        from kiro_crew import context
        from kiro_crew.agent_sdk import backends

        backends.confirm_kiro_cli_offers_kas()
        # The loader reads a stored ``kiro`` member route as KAS.
        inherited = context._member_backend_can_dispatch(_cfg("", "kas"), "atlas")
        assert context._member_backend_can_dispatch(_cfg("kiro", "kas"), "atlas") is inherited
        assert inherited is True

    def test_an_unstartable_pin_withholds_the_block(self):
        from kiro_crew import context

        assert context._member_backend_can_dispatch(_cfg("codex", "kas"), "atlas") is False


def _is_pre_gate_checked(value: ast.expr) -> bool:
    """Whether *value* is ``None``, a ``crewmate_backend_pin(...)`` call, or a
    conditional choosing between those."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if isinstance(value, ast.IfExp):
        return _is_pre_gate_checked(value.body) and _is_pre_gate_checked(value.orelse)
    if isinstance(value, ast.Call):
        func = value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name == "crewmate_backend_pin"
    return False


class TestEveryCallerPassesThePin:
    """A reader of the selection gate that skips the pin would judge or start a
    pinned crewmate on another harness."""

    def test_every_select_provider_backend_call_names_crew_backend(self):
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        missing: list[str] = []
        unchecked: list[str] = []
        calls = 0
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "select_provider_backend(" not in text:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "select_provider_backend":
                    continue
                calls += 1
                crew = [kw.value for kw in node.keywords if kw.arg == "crew_backend"]
                if not crew:
                    missing.append(f"{path.relative_to(root)}:{node.lineno}")
                elif not _is_pre_gate_checked(crew[0]):
                    unchecked.append(f"{path.relative_to(root)}:{node.lineno}")
                override = next(
                    (kw.value for kw in node.keywords if kw.arg == "backend_override"), None
                )
                if override is not None and crew:
                    # A lower-priority pin must not be checked before an explicit
                    # override reaches the selection gate, Kiro's "" override
                    # included: the argument must be exactly
                    # ``crewmate_backend_pin(...) if <override> is None else None``,
                    # checked by shape, not run.
                    where = f"{path.relative_to(root)}:{node.lineno}"
                    value = crew[0]
                    assert isinstance(value, ast.IfExp), where
                    assert (
                        isinstance(value.orelse, ast.Constant) and value.orelse.value is None
                    ), where
                    test = value.test
                    assert (
                        isinstance(test, ast.Compare)
                        and len(test.ops) == 1
                        and isinstance(test.ops[0], ast.Is)
                        and ast.dump(test.left) == ast.dump(override)
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value is None
                    ), where
        assert calls >= 9, "the scan found too few callers to be looking at the source"
        assert missing == []
        # H17: the pin reaches the gate only as the pre-gate check's own result,
        # evaluated in the call's arguments, so an unstartable pin has already
        # refused and nothing stale or unchecked can arrive.
        assert unchecked == []


class TestConfigRoundTrip:
    def test_a_pin_survives_save_and_load(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend="claude")
        cfg.save()
        assert KiroCrewConfig.load().agents["atlas"].acp_backend == "claude"

    def test_an_unknown_value_is_kept_so_the_gate_can_refuse_it(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend="nope")
        cfg.save()
        assert KiroCrewConfig.load().agents["atlas"].acp_backend == "nope"

    def test_absent_reads_as_inherit(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        assert KiroCrewConfig.load().agents["atlas"].acp_backend == ""


def _app(refreshed: list[str] | None = None) -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_resolved_model,
        api_kirocrew_agent_update,
        api_kirocrew_agents,
    )

    class _Sessions:
        async def refresh_defaults(self) -> None:
            if refreshed is not None:
                refreshed.append("yes")

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app["state"] = SimpleNamespace(sessions=_Sessions(), conversation_log=None)
    return app


@pytest.fixture()
def seeded():
    cfg = KiroCrewConfig.load()
    cfg.agents["atlas"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "atlas"


class TestSave:
    @pytest.mark.asyncio
    async def test_a_selectable_harness_is_stored_and_refreshes_the_factory(self, seeded):
        refreshed: list[str] = []
        async with TestClient(TestServer(_app(refreshed))) as client:
            resp = await client.put(f"/api/agents/{seeded}", json={"acp_backend": "claude"})
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents[seeded].acp_backend == "claude"
        assert refreshed == ["yes"]

    @pytest.mark.asyncio
    async def test_clearing_the_pin_inherits_again(self, seeded):
        async with TestClient(TestServer(_app())) as client:
            assert (
                await client.put(f"/api/agents/{seeded}", json={"acp_backend": "claude"})
            ).status == 200
            assert (await client.put(f"/api/agents/{seeded}", json={"acp_backend": ""})).status == (
                200
            )
        assert KiroCrewConfig.load().agents[seeded].acp_backend == ""

    @pytest.mark.parametrize("bad", ["codex", "nope", 3, None])
    @pytest.mark.asyncio
    async def test_a_harness_that_cannot_start_is_refused_and_nothing_is_written(self, seeded, bad):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded}", json={"acp_backend": bad, "description": "changed"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_acp_backend"
        stored = KiroCrewConfig.load().agents[seeded]
        assert (stored.acp_backend, stored.description) == ("", "")

    @pytest.mark.asyncio
    async def test_the_model_is_judged_against_the_pinned_harness(self, seeded, monkeypatch):
        seen: list[str | None] = []

        def judge(model, request, provider, *, backend=None, harness_pinned=False):
            seen.append(backend)
            return "not on this harness" if backend == "claude" and model == "kiro-only" else None

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._model_pin_rejected", judge)
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded}", json={"acp_backend": "claude", "model": "kiro-only"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_model"
        assert "claude" in seen
        assert KiroCrewConfig.load().agents[seeded].acp_backend == ""

    @pytest.mark.asyncio
    async def test_a_harness_change_re_judges_the_stored_model(self, seeded, monkeypatch):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].model = "kiro-only"
        cfg.save()

        def judge(model, request, provider, *, backend=None, harness_pinned=False):
            return "not on this harness" if backend == "claude" and model == "kiro-only" else None

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._model_pin_rejected", judge)
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(f"/api/agents/{seeded}", json={"acp_backend": "claude"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_model"
            # Picking the harness and clearing the model together is accepted.
            resp = await client.put(
                f"/api/agents/{seeded}", json={"acp_backend": "claude", "model": ""}
            )
            assert resp.status == 200
        stored = KiroCrewConfig.load().agents[seeded]
        assert (stored.acp_backend, stored.model) == ("claude", "")


class TestAHarnessChangeReloadsTheLiveChat:
    """A harness change reloads the crewmate's live DM chat, or refuses with nothing written."""

    KEY = "member-atlas-0123456789ab"

    @pytest.fixture()
    def chat(self, tmp_path, seeded, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_handlers

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].display_name = " Atlas Prime "
        cfg.save()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(
            self.KEY, agent=seeded, mode="member", model="claude-opus-4.8"
        )
        slot.reasoning_effort = "high"
        slot._deferred_model_switch_key = f"dashboard:{self.KEY}"
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi there", "msg msg-a")
        provider = MagicMock()
        provider.turns = [False]
        provider.has_active_turn = MagicMock(
            side_effect=lambda: (
                provider.turns.pop(0) if len(provider.turns) > 1 else provider.turns[0]
            )
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        order: list[str] = []

        async def reset(key, *, skip_if_busy=False):
            order.append("reset")
            return not provider.turns[0]

        async def refresh():
            # The record is written before the factory is rebuilt.
            order.append("refresh:" + KiroCrewConfig.load().agents[seeded].acp_backend)

        state.sessions.reset = AsyncMock(side_effect=reset)
        state.sessions.refresh_defaults = AsyncMock(side_effect=refresh)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", eager)
        app = _app()
        app["state"] = state
        return SimpleNamespace(
            state=state, slot=slot, provider=provider, order=order, eager=eager, app=app
        )

    @pytest.fixture(params=["", "member-atlas-store"])
    def dormant(self, chat, request):
        from kiro_crew import members

        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"].member_id = MEMBER_ID
        cfg.save()
        generation = request.param
        key = members.member_slot_key(MEMBER_ID, generation)
        members.write_dm_binding(MEMBER_ID, member="atlas", slot_key=key, memory_store=generation)
        log = chat.state.conversation_log
        history_key = members.member_thread_session_alias(MEMBER_ID, generation)
        log.append(history_key, "user", "keep this conversation")
        log.update_metadata(
            history_key,
            {
                "agent": "atlas",
                "model": chat.slot.model,
                "reasoning_effort": chat.slot.reasoning_effort,
                "closed": True,
                "closed_at": 1.0,
                "tab_id": "kept-tab",
            },
        )
        chat.state._slots.clear()
        chat.state.sessions.resumable_sid.return_value = "kept-session"
        return SimpleNamespace(
            chat=chat,
            log=log,
            key=key,
            history_key=history_key,
            metadata=log.get_metadata(history_key),
            messages=log.read_messages(history_key),
        )

    @staticmethod
    async def _put(app, body):
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/agents/atlas", json=body)
            return resp.status, await resp.json()

    def _untouched(self, chat, before):
        stored = KiroCrewConfig.load().agents["atlas"]
        assert (stored.acp_backend, stored.description) == ("", "")
        assert (chat.slot.model, chat.slot.reasoning_effort) == ("claude-opus-4.8", "high")
        assert chat.slot.messages == before
        assert all(not step.startswith("refresh") for step in chat.order)
        chat.eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_idle_chat_is_reloaded_onto_the_new_harness(self, chat):
        before = [dict(m) for m in chat.slot.messages]
        status, data = await self._put(chat.app, {"acp_backend": "claude", "model": ""})

        assert status == 200, data
        assert KiroCrewConfig.load().agents["atlas"].acp_backend == "claude"
        # Torn down first, then the write, then the factory rebuild, all before
        # the respawn that starts the new harness.
        assert chat.order == ["reset", "refresh:claude"]
        chat.state.sessions.reset.assert_awaited_once_with(
            f"dashboard:{self.KEY}", skip_if_busy=True
        )
        chat.eager.assert_called_once()
        assert chat.eager.call_args.kwargs.get("allow_resume") is True
        # The old harness's picks go; the next session inherits the record.
        assert (chat.slot.model, chat.slot.reasoning_effort) == ("", "")
        assert chat.slot._deferred_model_switch_key == ""
        assert chat.slot.jev_route is False
        # The transcript stays, plus one notice.
        assert chat.slot.messages[: len(before)] == before
        assert len(chat.slot.messages) == len(before) + 1
        assert chat.slot.messages[-1]["meta"]["kind"] == "session_reload"
        assert "new agent harness" in chat.slot.messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_an_unchanged_harness_reloads_nothing(self, chat):
        before = [dict(m) for m in chat.slot.messages]
        status, data = await self._put(chat.app, {"acp_backend": "", "description": "changed"})

        assert status == 200, data
        assert KiroCrewConfig.load().agents["atlas"].description == "changed"
        chat.state.sessions.reset.assert_not_awaited()
        chat.eager.assert_not_called()
        assert (chat.slot.model, chat.slot.reasoning_effort) == ("claude-opus-4.8", "high")
        assert chat.slot.messages == before

    @pytest.mark.parametrize("live_dm", [False, True])
    @pytest.mark.asyncio
    async def test_a_dormant_dm_reopens_without_old_harness_picks(self, dormant, live_dm):
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        chat = dormant.chat
        if live_dm:
            chat.state.get_or_create_slot(
                "member-atlas-live", agent="atlas", mode="member", model=chat.slot.model
            )
        status, data = await self._put(chat.app, {"acp_backend": "kas", "model": ""})

        assert status == 200, data
        assert KiroCrewConfig.load().agents["atlas"].acp_backend == "kas"
        assert dormant.key not in chat.state._slots
        assert await asyncio.to_thread(dormant.log.get_metadata, dormant.history_key) == {
            **dormant.metadata,
            "model": "",
            "reasoning_effort": "",
        }
        assert (
            await asyncio.to_thread(dormant.log.read_messages, dormant.history_key)
            == dormant.messages
        )
        assert chat.state.sessions.resumable_sid(dormant.history_key) == "kept-session"
        chat.state.sessions.discard_conversation.assert_not_awaited()
        chat.state.sessions.remove.assert_not_awaited()
        if not live_dm:
            chat.state.sessions.reset.assert_not_awaited()
            chat.eager.assert_not_called()
        restored = await rehydrate_slot_from_history_async(
            chat.state, dormant.key, adopt_closed=True
        )
        assert restored is not None
        assert (restored.model, restored.reasoning_effort) == ("", "")
        assert [m["content"] for m in restored.messages] == ["keep this conversation"]

    @pytest.mark.asyncio
    async def test_an_unchanged_harness_preserves_dormant_picks(self, dormant):
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        chat = dormant.chat
        status, data = await self._put(chat.app, {"acp_backend": "", "description": "changed"})

        assert status == 200, data
        assert dormant.key not in chat.state._slots
        assert (
            await asyncio.to_thread(dormant.log.get_metadata, dormant.history_key)
            == dormant.metadata
        )
        chat.state.sessions.reset.assert_not_awaited()
        chat.eager.assert_not_called()
        restored = await rehydrate_slot_from_history_async(
            chat.state, dormant.key, adopt_closed=True
        )
        assert restored is not None
        assert (restored.model, restored.reasoning_effort) == ("claude-opus-4.8", "high")

    @pytest.mark.asyncio
    async def test_a_running_reply_refuses_and_nothing_is_written(self, chat):
        before = [dict(m) for m in chat.slot.messages]
        chat.provider.turns = [True]
        status, data = await self._put(chat.app, {"acp_backend": "claude", "description": "x"})

        assert status == 409
        assert data == {
            "error": "Atlas Prime is in the middle of a reply. Wait for it to finish, "
            "or stop it, then change the agent harness.",
            "code": "crewmate_harness_busy",
        }
        chat.state.sessions.reset.assert_not_awaited()
        self._untouched(chat, before)

    @pytest.mark.asyncio
    async def test_a_reply_that_starts_after_the_guards_refuses_and_nothing_is_written(self, chat):
        """The reset's own busy decline is the authority: the write waits for it."""
        before = [dict(m) for m in chat.slot.messages]
        chat.provider.turns = [False, True]
        status, data = await self._put(chat.app, {"acp_backend": "claude", "description": "x"})

        assert status == 409
        assert data["code"] == "crewmate_harness_busy"
        assert chat.order == ["reset"]
        self._untouched(chat, before)

    @pytest.mark.asyncio
    async def test_running_sub_agents_refuse_and_nothing_is_written(self, chat, monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        async def _children(*_a, **_kw):
            return chat_handlers.web.json_response(
                {"error": "sub-agents are running", "code": "slot_subagents_running"}, status=409
            )

        monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _children)
        before = [dict(m) for m in chat.slot.messages]
        status, data = await self._put(chat.app, {"acp_backend": "claude", "description": "x"})

        assert status == 409
        assert data == {
            "error": "Atlas Prime has sub-agents running. Wait for them to finish, "
            "or stop them, then change the agent harness.",
            "code": "crewmate_harness_busy",
        }
        chat.state.sessions.reset.assert_not_awaited()
        self._untouched(chat, before)

    @pytest.mark.asyncio
    async def test_a_label_the_roster_would_mask_is_never_echoed(self, chat):
        secret = "Atlas AKIAIOSFODNN7EXAMPLE"
        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"].display_name = secret
        cfg.save()
        chat.provider.turns = [True]
        status, data = await self._put(chat.app, {"acp_backend": "claude"})

        assert status == 409
        assert secret not in data["error"]
        assert data["error"].startswith("atlas is in the middle of a reply.")

    @pytest.mark.asyncio
    async def test_a_second_live_chat_refuses_before_any_teardown(self, chat):
        other = chat.state.get_or_create_slot(
            "member-atlas-second", agent="atlas", mode="member", model="claude-opus-4.8"
        )
        other.append("user", "second chat", "msg msg-s")
        before = [dict(m) for m in chat.slot.messages]
        other_before = [dict(m) for m in other.messages]
        status, data = await self._put(chat.app, {"acp_backend": "claude", "description": "x"})

        assert status == 409
        assert data == {
            "error": "Atlas Prime has more than one chat open. Close the mate's other chat, "
            "then change the agent harness.",
            "code": "crewmate_harness_busy",
        }
        chat.state.sessions.reset.assert_not_awaited()
        chat.provider.has_active_turn.assert_not_called()
        assert chat.order == []
        assert other.messages == other_before
        assert other.model == "claude-opus-4.8"
        self._untouched(chat, before)

    @pytest.mark.asyncio
    async def test_an_approval_conflict_refuses_before_the_chat_is_torn_down(
        self, chat, monkeypatch
    ):
        """Another process sets Normal after the handler read; the save is refused
        with the permission conflict before the live chat is reloaded."""
        from kiro_crew.dashboard import chat_handlers

        real_key = chat_handlers.effective_session_key
        wrote: list[bool] = []

        def cli_write_then_key(slot):
            if not wrote:
                wrote.append(True)
                other = KiroCrewConfig.load()
                other.agents["atlas"].approval_mode = "normal"
                other.save()
            return real_key(slot)

        monkeypatch.setattr(chat_handlers, "effective_session_key", cli_write_then_key)
        before = [dict(m) for m in chat.slot.messages]
        status, data = await self._put(
            chat.app,
            {"acp_backend": "claude", "approval_mode": "trust", "expected_approval_mode": ""},
        )

        assert status == 409
        assert data == {
            "error": "the permission changed since it was read",
            "code": "approval_mode_conflict",
            "approval_mode": "normal",
        }
        assert wrote == [True]
        chat.state.sessions.reset.assert_not_awaited()
        assert chat.order == []
        assert KiroCrewConfig.load().agents["atlas"].approval_mode == "normal"
        self._untouched(chat, before)

    @pytest.mark.parametrize("bound", [False, True])
    @pytest.mark.asyncio
    async def test_with_no_live_chat_it_is_a_plain_save(self, chat, bound):
        from kiro_crew import members

        slug = members.member_slug("atlas", KiroCrewConfig.load())
        if bound:
            await asyncio.to_thread(
                members.write_dm_binding,
                slug,
                member="atlas",
                slot_key=members.member_slot_key(slug),
            )
        history_key = members.member_thread_session_alias(slug)
        assert not await asyncio.to_thread(chat.state.conversation_log.has_log, history_key)
        chat.state._slots.clear()
        status, data = await self._put(chat.app, {"acp_backend": "claude"})

        assert status == 200, data
        assert KiroCrewConfig.load().agents["atlas"].acp_backend == "claude"
        chat.state.sessions.reset.assert_not_awaited()
        chat.eager.assert_not_called()
        assert not await asyncio.to_thread(chat.state.conversation_log.has_log, history_key)
        assert chat.state._slots == {}
        assert chat.order == ["refresh:claude"]


#: The real model pins ``stranded`` stores beside its unstartable harness.
STRANDED_PINS = {"model": "stored-model", "worker_model": "stored-worker"}


@pytest.fixture()
def stranded(seeded):
    """``atlas`` pinned to a harness that has since stopped being selectable."""
    cfg = KiroCrewConfig.load()
    cfg.agents[seeded].acp_backend = "codex"
    cfg.agents[seeded].model = STRANDED_PINS["model"]
    cfg.agents[seeded].worker_model = STRANDED_PINS["worker_model"]
    cfg.save()
    return seeded


class TestSaveWithAnUnstartableStoredPin:
    @pytest.mark.parametrize(
        "body",
        [
            {"description": "changed", "model": ""},
            {"description": "changed", "model": "auto"},
            {"description": "changed", "model": "", "worker_model": ""},
            {"description": "changed", "worker_model": "auto"},
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unrelated_edit_with_an_inherited_model_is_saved(self, stranded, body):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(f"/api/agents/{stranded}", json=body)
            assert resp.status == 200, await resp.text()
        stored = KiroCrewConfig.load().agents[stranded]
        assert (stored.acp_backend, stored.description) == ("codex", "changed")

    @pytest.mark.parametrize("field", ["model", "worker_model"])
    @pytest.mark.asyncio
    async def test_a_real_model_pin_is_refused(self, stranded, field):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{stranded}", json={"description": "changed", field: "some-model"}
            )
            assert resp.status in (400, 409)
            assert "crewmate_harness_unavailable" in (await resp.json())["error"]
        stored = KiroCrewConfig.load().agents[stranded]
        assert (stored.description, getattr(stored, field)) == ("", STRANDED_PINS[field])

    @pytest.mark.parametrize(
        "pins",
        [
            {"model": STRANDED_PINS["model"]},
            {"worker_model": STRANDED_PINS["worker_model"]},
            STRANDED_PINS,
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unrelated_edit_resending_the_stored_models_is_saved(self, stranded, pins):
        """The editor re-sends the stored pins with every save; unchanged, they judge nothing."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{stranded}", json={"description": "changed", **pins}
            )
            assert resp.status == 200, await resp.text()
        stored = KiroCrewConfig.load().agents[stranded]
        assert (stored.acp_backend, stored.description) == ("codex", "changed")
        assert (stored.model, stored.worker_model) == tuple(STRANDED_PINS.values())

    @pytest.mark.asyncio
    async def test_a_harness_move_still_judges_unchanged_models(self, stranded, monkeypatch):
        probed: list[str] = []
        judged: list[str] = []

        async def probe(model, *args, **kwargs):
            probed.append(model)
            return None

        def judge(model, *args, **kwargs):
            judged.append(model)
            return "not served here"

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._revalidate_crew_pin", probe)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._model_pin_rejected", judge)
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{stranded}", json={"acp_backend": "claude", **STRANDED_PINS}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_model"
        assert probed == list(STRANDED_PINS.values())
        assert judged == [STRANDED_PINS["model"]]
        assert KiroCrewConfig.load().agents[stranded].acp_backend == "codex"

    @pytest.mark.parametrize("field", ["model", "worker_model"])
    @pytest.mark.asyncio
    async def test_the_locked_check_refuses_a_real_pin(self, stranded, field, monkeypatch):
        """With the pre-lock probe out of the way (it skips some values), the locked
        check still refuses: the pin has no harness to be judged against."""

        async def no_probe(*args, **kwargs):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._revalidate_crew_pin", no_probe)
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(f"/api/agents/{stranded}", json={field: "some-model"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "crewmate_harness_unavailable"
        assert getattr(KiroCrewConfig.load().agents[stranded], field) == STRANDED_PINS[field]

    @pytest.mark.asyncio
    async def test_moving_to_an_unstartable_harness_is_refused(self, stranded):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{stranded}", json={"acp_backend": "nope", "model": ""}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_acp_backend"
        assert KiroCrewConfig.load().agents[stranded].acp_backend == "codex"

    @pytest.mark.asyncio
    async def test_clearing_the_pin_is_saved(self, stranded):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.put(
                f"/api/agents/{stranded}", json={"acp_backend": "", "model": ""}
            )
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents[stranded].acp_backend == ""

    @pytest.mark.parametrize("raw", ["", "auto"])
    def test_the_cli_stores_an_inherited_model(self, raw):
        from kiro_crew.cli_commands_agent import _agent_model_or_exit

        cfg = _cfg("codex")
        cfg.agent.provider = "acp"
        assert _agent_model_or_exit(raw, cfg, "atlas") == ""

    def test_the_cli_refuses_a_real_model_pin(self, capsys):
        from kiro_crew.cli_commands_agent import _agent_model_or_exit

        cfg = _cfg("codex")
        cfg.agent.provider = "acp"
        with pytest.raises(SystemExit):
            _agent_model_or_exit("some-model", cfg, "atlas")
        assert "crewmate_harness_unavailable" in capsys.readouterr().err


class TestReadPath:
    @pytest.mark.asyncio
    async def test_the_owner_readout_names_the_pin_and_the_harness(self, seeded):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].acp_backend = "claude"
        cfg.save()
        async with TestClient(TestServer(_app())) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded})
            ).json()
        assert body["acp_backend"] == "claude"
        assert body["harness"] == "claude"
        # What Inherited would give stays named while a harness is pinned.
        assert body["inherited_harness"] in {"kas", "kiro"}
        assert set(body["harnesses"]) == {"kiro", "kas", "claude"}
        assert set(body["worker_harnesses"]) == {"kas", "claude"}

    @pytest.mark.asyncio
    async def test_a_kiro_pin_on_v3_reads_as_stored_and_runs_on_kas(self, seeded):
        from kiro_crew.agent_sdk import backends

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].acp_backend = "kiro"
        cfg.save()
        backends.confirm_kiro_cli_offers_kas()
        async with TestClient(TestServer(_app())) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded})
            ).json()
        assert body["acp_backend"] == "kiro"
        assert body["harness"] == "kas"

    @pytest.mark.asyncio
    async def test_an_unstartable_pin_is_reported_not_hidden(self, seeded):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].acp_backend = "codex"
        cfg.save()
        async with TestClient(TestServer(_app())) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded})
            ).json()
        assert body["acp_backend"] == "codex"
        assert body["harness"] == ""
        assert "crewmate_harness_unavailable" in body["harness_error"]

    @pytest.mark.asyncio
    async def test_a_non_owner_readout_carries_no_harness_fields(self, seeded, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_app())) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded})
            ).json()
        assert "acp_backend" not in body and "harness" not in body

    @pytest.mark.asyncio
    async def test_the_roster_leaves_the_pin_out(self, seeded):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].acp_backend = "claude"
        cfg.save()
        async with TestClient(TestServer(_app())) as client:
            rows = await (await client.get("/api/agents")).json()
        rows = rows if isinstance(rows, list) else rows.get("agents", [])
        assert rows and all("acp_backend" not in row for row in rows)


class TestHotReload:
    @pytest.mark.asyncio
    async def test_a_pin_edited_outside_the_editor_rebuilds_the_factory(self):
        from kiro_crew.session_parts import config_apply

        refreshed: list[object] = []

        class _Manager:
            _CONFIG_SECTIONS = config_apply._CONFIG_SECTIONS
            _FACTORY_CONFIG_PATHS = config_apply._FACTORY_CONFIG_PATHS

            async def refresh_defaults(self, cfg=None):
                refreshed.append(cfg)

        new = SimpleNamespace(degraded_sections=frozenset())
        change = SimpleNamespace(
            new=new,
            changed=frozenset({"agents.atlas.acp_backend"}),
            touched=lambda *prefixes: False,
            under=lambda prefix: frozenset({"agents.atlas.acp_backend"}),
        )
        await config_apply._on_config_change(_Manager(), change)
        assert refreshed == [new]


class TestModelsRoute:
    """``GET /api/models?backend=``: the asked harness names its own catalog route."""

    def _config(self, acp_backend: str, kas_bundle: bool) -> SimpleNamespace:
        return SimpleNamespace(
            agent=SimpleNamespace(acp_backend=acp_backend, kas_bundle=kas_bundle)
        )

    @pytest.mark.parametrize(
        "installed, bundle, asked, expected",
        [
            ("claude", True, "kas", ("kas", True)),
            ("kas", True, "kiro", ("", False)),
            ("kas", False, "kas", ("kas", False)),
            ("kas", True, "claude", ("claude", False)),
        ],
    )
    def test_the_asked_harness_decides_the_bundle_route(self, installed, bundle, asked, expected):
        from kiro_crew.crewmate_harness import models_route

        assert models_route(self._config(installed, bundle), {"backend": asked}) == expected

    @pytest.mark.parametrize("asked", ["codex", ""])
    def test_an_unselectable_harness_is_refused(self, asked):
        from kiro_crew.crewmate_harness import models_route

        assert models_route(self._config("kas", False), {"backend": asked}) is None

    def test_no_query_keeps_the_installation_route(self, monkeypatch):
        from kiro_crew.agent_sdk import kas_bundle
        from kiro_crew.crewmate_harness import models_route

        monkeypatch.setattr(kas_bundle, "kas_bundle_route", lambda: True)
        assert models_route(self._config("kas", True), {}) == ("kas", True)


class TestWorkerInheritsThePinnedModel:
    """A worker on its crewmate's pinned harness keeps the model chosen for it."""

    def test_a_model_chosen_for_the_pin_follows_the_worker(self):
        from kiro_crew.dashboard.session_control_parts.session_create import (
            _inherited_pin_foreign_to_child,
        )

        reason = _inherited_pin_foreign_to_child(
            "claude-opus-5", _cfg("claude"), "claude", validated_for_worker=False, crew="atlas"
        )
        assert reason is None

    def test_without_a_pin_the_member_route_still_decides(self):
        from kiro_crew.dashboard.session_control_parts.session_create import (
            _inherited_pin_foreign_to_child,
        )

        reason = _inherited_pin_foreign_to_child(
            "claude-opus-5", _cfg(""), "claude", validated_for_worker=False, crew="atlas"
        )
        assert reason is not None and "(kas)" in reason


class TestHarnessNames:
    """No harness reaches a person as its raw id."""

    def test_every_known_harness_has_a_readable_name(self):
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN, backend_to_stored
        from kiro_crew.crewmate_harness import harness_label

        # goose spells its own name in lower case; every other name differs from its id.
        for backend in ACP_BACKENDS_KNOWN:
            stored = backend_to_stored(backend)
            label = harness_label(stored)
            assert "'" not in label and (label != stored or stored == "goose"), stored

    def test_harness_names_cover_every_known_backend(self):
        """The dashboard's lookup (``website/src/api/acpBackend.ts``) names each one."""
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN, backend_to_stored

        source = (
            Path(__file__).resolve().parents[1] / "website" / "src" / "api" / "acpBackend.ts"
        ).read_text(encoding="utf-8")
        body = source[source.index("export function acpBackendName") :]
        body = body[: body.index("\n}\n")]
        named = {"kiro", "kas"} | {
            line.split("'")[1] for line in body.splitlines() if line.strip().startswith("case '")
        }
        assert {backend_to_stored(b) for b in ACP_BACKENDS_KNOWN} <= named

    def test_the_refusal_names_the_harness(self):
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

        assert "OpenAI Codex" in str(CrewmateHarnessUnavailable("atlas", "codex"))
        assert "an unknown harness" in str(CrewmateHarnessUnavailable("atlas", "nope"))

    def test_a_pin_that_stopped_being_startable_drops_the_model_without_raising(self):
        from kiro_crew.dashboard.session_control_parts.session_create import (
            _inherited_pin_foreign_to_child,
        )

        reason = _inherited_pin_foreign_to_child(
            "claude-opus-5", _cfg("codex"), "kas", validated_for_worker=False, crew="atlas"
        )
        assert reason is not None and "cannot start" in reason


class TestStoredPinDisplay:
    SECRET = "sk-test-not-a-real-secret-0000"

    def test_known_spellings_and_inherit_are_preserved(self):
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN, backend_to_stored
        from kiro_crew.crewmate_harness import CrewBackendPin, display_stored_harness

        for stored in ["", *(backend_to_stored(b) for b in ACP_BACKENDS_KNOWN)]:
            assert display_stored_harness(stored) == stored
            assert CrewBackendPin("atlas", stored).display_stored == stored

    @pytest.mark.parametrize("stored", [SECRET, "no-such-harness", "CLAUDE", "claude\nsecret"])
    def test_unknown_text_is_never_displayed_or_logged(self, stored, caplog):
        import logging

        from kiro_crew.crewmate_harness import (
            CrewmateHarnessUnavailable,
            crewmate_stored_pin,
            harness_label,
        )

        pin = crewmate_stored_pin(_cfg(stored), DM_KEY)
        assert pin is not None and pin.stored == stored
        assert pin.display_stored == "unknown"
        assert harness_label(stored) == "an unknown harness"
        with caplog.at_level(logging.WARNING):
            try:
                pin.resolve()
            except CrewmateHarnessUnavailable as exc:
                assert exc.stored == stored
                assert stored not in str(exc) and stored not in repr(exc)
                assert "an unknown harness" in str(exc)
                logging.getLogger(__name__).exception("Refused pin %r: %s", pin, exc)
            else:
                pytest.fail("an unknown stored pin must still refuse")
        assert "crewmate_harness_unavailable" in caplog.text
        assert stored not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner", [False, True])
    async def test_resolved_model_never_echoes_unknown_text(
        self, seeded, owner, monkeypatch, caplog
    ):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].acp_backend = self.SECRET
        cfg.save()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: owner,
        )
        async with TestClient(TestServer(_app(), host="127.0.0.1")) as client:
            response = await client.get("/api/agents/resolved-model", params={"agent": seeded})
            assert response.status == 200
            text = await response.text()
            body = await response.json()
        assert self.SECRET not in text + caplog.text
        if owner:
            assert body["acp_backend"] == "unknown"
            assert body["harness"] == ""
            assert "an unknown harness" in body["harness_error"]
        else:
            assert "acp_backend" not in body
        assert KiroCrewConfig.load().agents[seeded].acp_backend == self.SECRET

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["model", "worker_model"])
    @pytest.mark.parametrize("locked", [False, True])
    async def test_crew_update_refusal_never_echoes_unknown_text(
        self, seeded, field, locked, monkeypatch, caplog
    ):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded].acp_backend = self.SECRET
        cfg.save()
        if locked:

            async def no_probe(*args, **kwargs):
                return None

            monkeypatch.setattr(
                "kiro_crew.dashboard.handlers.agents._revalidate_crew_pin", no_probe
            )
        async with TestClient(TestServer(_app(), host="127.0.0.1")) as client:
            response = await client.put(f"/api/agents/{seeded}", json={field: "some-model"})
            assert response.status == (409 if locked else 400)
            text = await response.text()
            body = await response.json()
        assert "crewmate_harness_unavailable" in body["error"]
        assert "an unknown harness" in body["error"]
        assert self.SECRET not in text + caplog.text
        record = KiroCrewConfig.load().agents[seeded]
        assert record.acp_backend == self.SECRET and getattr(record, field) == ""

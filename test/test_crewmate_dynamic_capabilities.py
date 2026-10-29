# SPDX-License-Identifier: Apache-2.0
"""Dynamic capabilities: a per-crewmate preview switch, off by default.

Stored on the crewmate's own record (``KiroCrewAgentConfig.dynamic_capabilities``)
and set on its Profile card; there is no global key. With a
crewmate's flag off, that crewmate's crewmate-driven capability path is
withheld: find_capabilities is refused at its route (403, read on every call so a
Turn on takes effect in a running session), request_capabilities raises the
Preview opt-in card once, no capability summary on hand-off, a worker's gap
reaches its conductor as one plain "ask the user" line and never a card, no
``[Capabilities added]`` nudge, and its worker is asked for no
``MISSING CAPABILITY:`` line. Another crewmate's flag changes nothing for it. The
user adding worker capabilities by hand keeps working. The rest of the suite runs with every flag on
(``conftest._crewmate_dynamic_capabilities_on``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew import crewmate_dynamic_capabilities as dyn
from kiro_crew import crewmate_prompts, mcp_mate
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


@pytest.fixture
def off():
    dyn._override = False


@pytest.fixture
def roster(monkeypatch):
    """Two crewmates on one roster, the per-crewmate read live (no override)."""
    dyn._override = None
    cfg = KiroCrewConfig()
    cfg.agents = {
        "Atlas": KiroCrewAgentConfig(member_id="atlas", kiro_agent="Atlas"),
        "Briar": KiroCrewAgentConfig(member_id="briar", kiro_agent="Briar"),
    }
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    return cfg


# ── the flag itself: per crewmate, off by default ──


def test_a_new_crewmate_starts_off_and_there_is_no_global_key():
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import core

    assert KiroCrewAgentConfig().dynamic_capabilities is False
    assert not hasattr(KiroCrewConfig().dashboard, "crewmate_dynamic_capabilities")
    assert "dashboard.crewmate_dynamic_capabilities" not in core._EDITABLE_CONFIG
    # A config that still carries the old key loads, and the key is not read.
    built = loader._build_dashboard_config(set(), {"crewmate_dynamic_capabilities": True})
    assert not hasattr(built, "crewmate_dynamic_capabilities")


def test_the_flag_is_read_from_the_record(tmp_path, monkeypatch):
    from kiro_crew.config import loader

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "dashboard": {"crewmate_dynamic_capabilities": True},
                "agents": {
                    "Atlas": {
                        "member_id": "atlas",
                        "kiro_agent": "Atlas",
                        "dynamic_capabilities": True,
                    },
                    "Briar": {"member_id": "briar", "kiro_agent": "Briar"},
                    "Cove": {
                        "member_id": "cove",
                        "kiro_agent": "Cove",
                        "dynamic_capabilities": "yes",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "config_path", lambda: path)
    loader._invalidate_config_cache()
    dyn._override = None
    try:
        agents = loader.KiroCrewConfig.load().agents
        assert [agents[n].dynamic_capabilities for n in ("Atlas", "Briar", "Cove")] == [
            True,
            False,
            False,
        ]
        assert (dyn.enabled_for("Atlas"), dyn.enabled_for("Briar")) == (True, False)
    finally:
        loader._invalidate_config_cache()


def test_on_for_one_crewmate_only(roster):
    roster.agents["Atlas"].dynamic_capabilities = True
    assert (dyn.enabled_for("Atlas"), dyn.restricted_for("Atlas")) == (True, False)
    assert (dyn.enabled_for("Briar"), dyn.restricted_for("Briar")) == (False, True)


def test_no_crewmate_in_scope_reads_as_off(roster):
    roster.agents["Atlas"].dynamic_capabilities = True
    for nobody in ("", None, "not-on-the-roster"):
        assert dyn.enabled_for(nobody) is False
        assert dyn.restricted_for(nobody) is True


def test_a_worker_names_its_crewmate_by_member_id(roster):
    assert dyn.member_name("atlas") == "Atlas"
    assert dyn.member_name("nobody") == ""


# ── off: the conductor's tools and their routes ──


def _thread(agent: str):
    from kiro_crew import members as members_mod

    return SimpleNamespace(mode=members_mod.DM_SLOT_MODE, agent=agent, executor="")


def test_off_the_conductor_still_lists_both_capability_tools():
    """The flag does not ride the session tool policy.

    The pooled ``kirocrew-mate`` server caches a session's tool policy with no
    way to drop it, so a tool cut there would stay cut after the user turned the
    preview on. The flag is read at each tool's route instead.
    """
    import inspect

    from kiro_crew.dashboard.handlers.sessions_parts import tool_policy

    assert "crewmate_dynamic_capabilities" not in inspect.getsource(tool_policy)
    names = {t["name"] for t in mcp_mate._list_tools()}
    assert {"find_capabilities", "request_capabilities"} <= names
    assert "ask_tool_access" not in names
    assert {"update_self", "set_model", "set_goal", "set_check_ins"} <= names


@pytest.fixture
def routes(monkeypatch):
    from guide_route_helpers import FakeState, agent, run_guide_app

    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.handlers import guide as guide_routes
    from kiro_crew.dashboard.handlers import member_capability_catalog as catalog_routes
    from kiro_crew.dashboard.handlers import member_self as self_routes

    class _Null:
        def log_api_access(self, **_kw):
            return None

    monkeypatch.setattr(guide_routes, "sel", lambda: _Null())
    state = FakeState()
    slot = state.open_slot("member-pixel")
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = "pixel"
    table = {
        "find": (
            "POST",
            "/api/member/self/capabilities",
            catalog_routes.api_member_self_capabilities,
        ),
        "propose": ("POST", "/api/member/self/propose", self_routes.api_member_self_propose),
    }

    def post(which, body):
        method, path, handler = table[which]

        async def go(c):
            r = await c.post(path, json=body, headers=agent("dashboard:member-pixel"))
            return r.status, await r.json()

        return run_guide_app(go, state, extra_routes=[(method, path, handler)])

    return post


def test_off_the_catalog_route_refuses_with_dynamic_capabilities_off(off, routes):
    status, body = routes("find", {})
    assert (status, body["code"]) == (403, "dynamic_capabilities_off")
    assert "request_capabilities" in body["error"]


def test_off_the_other_crewmate_cards_are_still_proposed(off, routes, monkeypatch):
    from kiro_crew.dashboard.handlers import change_cards

    seen: list[str] = []

    async def propose(request, slot_key, sk, *, kind, params, reason):
        from aiohttp import web

        seen.append(kind)
        return web.json_response({"kind": kind})

    monkeypatch.setattr(change_cards, "propose_for_caller", propose)
    from kiro_crew.dashboard.handlers.member_self import SELF_PROPOSALS

    status, _ = routes("propose", {"request": "model", "params": {"model": "m"}})
    assert (status, seen) == (200, [SELF_PROPOSALS["model"]])


# ── off: no gap card, a plain line instead ──


def _report(text):
    return [{"turns": 1, "title": "t", "key": "k", "reply": text}]


@pytest.mark.parametrize(
    "reply",
    [
        "MISSING CAPABILITY: the jira server -- the task reads tickets\nDid the rest.",
        "Step 2: blocked on jira (no jira tool)",
    ],
)
def test_off_a_worker_gap_asks_with_request_capabilities_and_no_catalog(off, reply):
    from kiro_crew.dashboard import mate_thread_wakes

    prompt = mate_thread_wakes.report_prompt(_report(reply))
    assert "request_capabilities" in prompt and "find_capabilities" not in prompt
    dyn._override = True
    prompt = mate_thread_wakes.report_prompt(_report(reply))
    assert "request_capabilities" in prompt and "find_capabilities" in prompt


def test_off_a_crewmate_cannot_raise_the_card_through_propose_change(off, monkeypatch):
    from kiro_crew.dashboard.handlers import change_cards

    crew_slot = SimpleNamespace(mode="member-dm", agent="pixel", executor="")
    other_slot = SimpleNamespace(mode="", agent="", executor="")
    monkeypatch.setattr(
        "kiro_crew.dashboard.mate_thread_wakes.is_crewmate_thread",
        lambda slot: slot is crew_slot,
    )
    slots = {"crew": crew_slot, "other": other_slot}
    req = SimpleNamespace(get_slot=lambda key: slots.get(key))
    refuse = change_cards._crewmate_drives_capabilities_off
    assert refuse(req, "crew", "dashboard:crew") is True
    # The main assistant asked to add a tool to a crewmate still proposes it.
    assert refuse(req, "other", "dashboard:other") is False
    # A crewmate's own worker is refused too.
    from kiro_crew import members

    members.note_crewmate_worker_session("dashboard:w-dyn")
    assert refuse(req, "other", "dashboard:w-dyn") is True
    dyn._override = True
    assert refuse(req, "crew", "dashboard:crew") is False


# ── off: no summary on hand-off, no nudge ──


def _complete_capabilities_card(monkeypatch, member="pixel"):
    from kiro_crew import change_card_catalog as catalog
    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import change_cards

    nudged: list[str] = []
    store = SimpleNamespace(complete_apply=lambda rec, **kw: None)
    monkeypatch.setattr(change_cards, "card_store_for", lambda state: store)

    async def read_state(*a, **k):
        return {}

    monkeypatch.setattr(change_cards.cards, "read_state", read_state)
    monkeypatch.setattr(change_cards.cards, "after_shows_the_write", lambda *a: True)
    monkeypatch.setattr(catalog, "applied_write_count", lambda *a: 1)
    monkeypatch.setattr(catalog, "build_undo", lambda *a: (None, "x"))
    monkeypatch.setattr(
        goal_setup, "schedule_capabilities_nudge", lambda s, k, t, *, card_member: nudged.append(t)
    )
    rec = {
        "id": "c1",
        "kind": catalog.KIND_CREWMATE_CAPABILITIES,
        "params": {"member": member},
        "before": {},
        "evidence": [],
        "plan": {"apply": []},
        "slot_key": "member-pixel",
        "title": "Add jira to Pixel's worker",
    }
    request = SimpleNamespace(app={"state": SimpleNamespace()})
    asyncio.run(change_cards._complete(request, rec, partial=False))
    return nudged


def test_off_no_capabilities_added_nudge(off, monkeypatch):
    assert _complete_capabilities_card(monkeypatch) == []
    dyn._override = True
    assert _complete_capabilities_card(monkeypatch) == ["Add jira to Pixel's worker"]


# ── off: the prompts ──


def test_the_worker_is_asked_for_the_missing_capability_line_either_way(off, monkeypatch):
    """Off, the conductor still asks with request_capabilities, so a gap stays named."""
    from kiro_crew import agent as agent_mod
    from kiro_crew import mate_worker
    from kiro_crew.agent_materialization import service_agents

    written: dict = {}
    monkeypatch.setattr(agent_mod, "build_agent_config", lambda *a, **k: {"prompt": "Base."})
    monkeypatch.setattr(
        service_agents, "_write_mate_service_spec", lambda f, c, w: written.update(c)
    )
    service_agents._install_mate_worker_agent()
    assert written["prompt"] == f"Base.\n\n{mate_worker.MATE_WORKER_PROMPT}"
    assert mate_worker.MISSING_CAPABILITY_PREFIX in written["prompt"]


def test_off_the_conductor_prompt_names_no_catalog(off, monkeypatch, tmp_path):
    from kiro_crew import context as ctx
    from kiro_crew import member_mate_tools
    from kiro_crew.context_assembly import member

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = ctx.KiroCrewConfig()
    cfg.agents["Ada"] = SimpleNamespace(
        description="", triggers="", kiro_agent="", workspace="", member_id=""
    )
    monkeypatch.setattr(ctx.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(member_mate_tools, "binding_has_mate_tools", lambda *a, **k: True)
    monkeypatch.setattr(member, "binding_has_mate_tools", lambda *a, **k: True, raising=False)
    text = member.build_member_section("Ada")
    assert "[YOUR CREW LOG, DASHBOARD AND TOOLS]" in text
    assert "find_capabilities" not in text and "request_capabilities" in text
    dyn._override = True
    text = member.build_member_section("Ada")
    assert "find_capabilities" in text


def test_the_off_pieces_are_the_pieces_from_before_the_preview():
    """Off, a crewmate asks as it did before Dynamic capabilities: no catalog, no new tool."""
    card = crewmate_prompts.text("conductor.crew_log_section").splitlines()
    manual = crewmate_prompts.text("conductor.crew_log_section_manual").splitlines()
    assert card[:-1] == manual[:-1]
    for key in (
        "conductor.crew_log_section_manual",
        "conductor.mate_tools_identity_manual",
        "worker_report.missing_capability_manual",
    ):
        piece = crewmate_prompts.text(key)
        assert "find_capabilities" not in piece and "request_capabilities" in piece, key
        assert "ask_tool_access" not in piece, key


# ── off: the user still adds capabilities by hand, and a fresh worker has them ──


@pytest.fixture
def mate_home(tmp_path, monkeypatch):
    from kiro_crew import mate_capabilities
    from kiro_crew.config import paths

    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    (user / "release-bot.json").write_text(
        json.dumps(
            {
                "name": "release-bot",
                "prompt": "Release SOP.",
                "tools": ["@jira"],
                "mcpServers": {"jira": {"command": "jira-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "mate_agents_dir", lambda: mates, raising=False)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    return mates


def _fresh_worker(mates):
    from kiro_crew import mate_worker

    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    own = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))
    return mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, own))


def test_off_a_skill_added_in_advanced_settings_reaches_a_fresh_worker(off, mate_home):
    from kiro_crew import mate_capabilities as mc

    catalog = {"kiro-user/release": "skill://release/SKILL.md"}
    revision = mc._revision(json.loads((mate_home / "pixel.json").read_text()))
    op = {"section": "skills", "id": "kiro-user/release", "action": "set", "value": True}
    body = {
        "revision": revision,
        "operations": [op],
        "preview_token": revision,
        "accept_parent": [],
        "accept_members": [],
    }
    assert mc.put("pixel", body, catalog)["mode"] == "mate"
    assert "skill://release/SKILL.md" in _fresh_worker(mate_home).get("resources", [])


def test_off_a_custom_template_still_configures_the_worker(off, mate_home):
    from kiro_crew import mate_worker_kit

    ask = {"template": "release-bot", "keep_additions": True}
    preview = mate_worker_kit.preview("pixel", ask)
    mate_worker_kit.apply("pixel", {**ask, "revision": preview["revision"]})
    worker = _fresh_worker(mate_home)
    assert "jira" in worker["mcpServers"]
    assert "Release SOP." in worker["prompt"]


# ── per crewmate: on for one, the others unaffected ──


def test_per_crewmate_routes(roster, routes_for):
    roster.agents["Atlas"].dynamic_capabilities = True
    status, body = routes_for("Briar", "find", {})
    assert (status, body.get("code")) == (403, "dynamic_capabilities_off")
    # Atlas gets past the flag: the next refusal is its missing crewmate file.
    status, body = routes_for("Atlas", "find", {})
    assert (status, body.get("code")) == (409, "no_crewmate_file")
    # Turned on for Briar: its very next call gets past the flag, no restart.
    roster.agents["Briar"].dynamic_capabilities = True
    status, body = routes_for("Briar", "find", {})
    assert (status, body.get("code")) == (409, "no_crewmate_file")


def test_per_crewmate_gap_line(roster):
    from kiro_crew.dashboard import mate_thread_wakes

    roster.agents["Atlas"].dynamic_capabilities = True
    gap = _report("MISSING CAPABILITY: jira -- tickets")
    assert "find_capabilities" in mate_thread_wakes.report_prompt(gap, "Atlas")
    assert "find_capabilities" not in mate_thread_wakes.report_prompt(gap, "Briar")
    assert "find_capabilities" not in mate_thread_wakes.report_prompt(gap)  # nobody named
    assert "request_capabilities" in mate_thread_wakes.report_prompt(gap, "Briar")


def test_per_crewmate_propose_change(roster, monkeypatch):
    from kiro_crew import members
    from kiro_crew.dashboard.handlers import change_cards

    roster.agents["Atlas"].dynamic_capabilities = True
    slots = {"a": _thread("Atlas"), "b": _thread("Briar"), "w": SimpleNamespace(key="w")}
    state = SimpleNamespace(get_slot=lambda key: slots.get(key))
    refuse = change_cards._crewmate_drives_capabilities_off
    assert refuse(state, "a", "dashboard:a") is False
    assert refuse(state, "b", "dashboard:b") is True
    # A worker is judged by its own crewmate's flag; an unnamed one is off.
    members.note_crewmate_worker_session("dashboard:w")
    monkeypatch.setattr(change_cards, "_worker_crewmate", lambda slot: "Atlas")
    assert refuse(state, "w", "dashboard:w") is False
    monkeypatch.setattr(change_cards, "_worker_crewmate", lambda slot: "")
    assert refuse(state, "w", "dashboard:w") is True


def test_per_crewmate_nudge(roster, monkeypatch):
    roster.agents["Atlas"].dynamic_capabilities = True
    assert _complete_capabilities_card(monkeypatch, "Atlas") == ["Add jira to Pixel's worker"]
    assert _complete_capabilities_card(monkeypatch, "Briar") == []


def test_per_crewmate_hand_off_summary(roster, monkeypatch):
    from kiro_crew import execution_context, mate_capabilities
    from kiro_crew import mate_capability_catalog as catalog_mod
    from kiro_crew import mate_worker
    from kiro_crew.dashboard import change_cards as cards_mod
    from kiro_crew.dashboard import crewmate_approval, crewmate_dispatch_capabilities

    roster.agents["Atlas"].dynamic_capabilities = True
    rec = SimpleNamespace(
        selection_kind="template", template_id=mate_worker.MATE_WORKER_AGENT, member_id="m"
    )
    monkeypatch.setattr(execution_context, "read_session_execution", lambda key: rec)
    monkeypatch.setattr(mate_worker, "is_conductor_execution", lambda e, config=None: True)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda m: True)
    monkeypatch.setattr(
        cards_mod, "_capability_service", lambda app: SimpleNamespace(_mate_catalog=lambda m: {})
    )
    monkeypatch.setattr(catalog_mod, "worker_spec", lambda m: {})
    monkeypatch.setattr(catalog_mod, "dispatch_summary", lambda spec, cat: "worker has: jira")
    for name, want in (("Atlas", "worker has: jira"), ("Briar", "")):
        monkeypatch.setattr(crewmate_approval, "crewmate_name_for", lambda cfg, e, n=name: n)
        assert crewmate_dispatch_capabilities._note(None, "c", "t") == want, name


def test_per_crewmate_conductor_prompt(roster, monkeypatch, tmp_path):
    from kiro_crew import context as ctx
    from kiro_crew import member_mate_tools
    from kiro_crew.context_assembly import member

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = ctx.KiroCrewConfig()
    for name, flag in (("Atlas", True), ("Briar", False)):
        cfg.agents[name] = SimpleNamespace(
            description="",
            triggers="",
            kiro_agent="",
            workspace="",
            member_id="",
            dynamic_capabilities=flag,
        )
    monkeypatch.setattr(ctx.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(member_mate_tools, "binding_has_mate_tools", lambda *a, **k: True)
    monkeypatch.setattr(member, "binding_has_mate_tools", lambda *a, **k: True, raising=False)
    assert "find_capabilities" in member.build_member_section("Atlas")
    assert "find_capabilities" not in member.build_member_section("Briar")
    assert "request_capabilities" in member.build_member_section("Briar")


@pytest.fixture
def routes_for(monkeypatch):
    """POST a crewmate route from crewmate *name*'s own thread."""
    from guide_route_helpers import FakeState, agent, run_guide_app

    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.handlers import guide as guide_routes
    from kiro_crew.dashboard.handlers import member_capability_catalog as catalog_routes
    from kiro_crew.dashboard.handlers import member_self as self_routes

    class _Null:
        def log_api_access(self, **_kw):
            return None

    monkeypatch.setattr(guide_routes, "sel", lambda: _Null())
    monkeypatch.setattr(catalog_routes, "_is_mate", lambda _m: False)
    table = {
        "find": (
            "POST",
            "/api/member/self/capabilities",
            catalog_routes.api_member_self_capabilities,
        ),
        "propose": ("POST", "/api/member/self/propose", self_routes.api_member_self_propose),
    }

    def post(name, which, body):
        state = FakeState()
        key = f"member-{name.lower()}"
        slot = state.open_slot(key)
        slot.mode = members_mod.DM_SLOT_MODE
        slot.agent = name
        method, path, handler = table[which]

        async def go(c):
            r = await c.post(path, json=body, headers=agent(f"dashboard:{key}"))
            return r.status, await r.json()

        return run_guide_app(go, state, extra_routes=[(method, path, handler)])

    return post


class TestTheProfileSwitchWritesOneCrewmate:
    """``PUT /api/agents/{name}`` stores the flag on that record alone, as a strict bool."""

    @pytest.fixture(autouse=True)
    def _owner_caller(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.fixture()
    def two(self):
        cfg = KiroCrewConfig.load()
        for name in ("Atlas", "Briar"):
            cfg.agents[name] = KiroCrewAgentConfig(
                kiro_agent="kirocrew", workspace="default", memory_store="default"
            )
        cfg.save()

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

        app = web.Application()
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        return app

    @pytest.mark.asyncio
    async def test_on_for_one_crewmate_leaves_the_other_off(self, two):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put("/api/agents/Atlas", json={"dynamic_capabilities": True})
            assert resp.status == 200, await resp.text()
            bad = await client.put("/api/agents/Briar", json={"dynamic_capabilities": "true"})
            assert bad.status == 400
            assert (await bad.json())["code"] == "invalid_dynamic_capabilities"

        agents = KiroCrewConfig.load().agents
        assert agents["Atlas"].dynamic_capabilities is True
        assert agents["Briar"].dynamic_capabilities is False

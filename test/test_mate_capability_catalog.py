# SPDX-License-Identifier: Apache-2.0
"""A crewmate's capability catalog and its additions.

The conductor reads what its worker has and what it could be given
(``find_capabilities``), then asks for an addition on a ``crewmate.capabilities``
card (``request_capabilities`` with ``additions``). The gateway resolves each
addition -- a gateway-loaded server's own entry, an official-registry install
plan, a loaded skill -- into the card's operations; pressing the card writes the
crewmate's own file in place, never the user's MCP config.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from guide_route_helpers import FakeState, agent, run_guide_app

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mate_capability_catalog as cat
from kiro_crew import members as members_mod
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.handlers import member_capability_catalog as routes
from kiro_crew.dashboard.handlers import member_self as self_routes

SLOT = "member-pixel"
MEMBER = "pixel"
SKILLS = {"release": "skill://release/SKILL.md", "triage": "skill://triage/SKILL.md"}
WORKER = {
    "name": "kirocrew-mate-worker",
    "tools": ["@kirocrew-core", "@jira"],
    "allowedTools": ["@kirocrew-core", "@jira/search"],
    "excludedTools": ["@jira/delete_issue", "execute_bash"],
    "resources": ["skill://release/SKILL.md"],
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew"},
        "jira": {"command": "jira-mcp", "autoApprove": ["get_issue"]},
    },
}
GATEWAY_ROWS = [
    SimpleNamespace(name="jira", status="ok", tools=["search"]),
    SimpleNamespace(name="github", status="ok", tools=["list_prs", "merge_pr"]),
    SimpleNamespace(name="slack", status="disabled", tools=[]),
]
GITHUB_ENTRY = {
    "command": "gh-mcp",
    "args": ["--stdio"],
    "autoApprove": ["list_prs"],
    "disabledTools": ["merge_pr"],
}


# ── what the worker has, and what it lacks ──


def test_the_worker_is_read_as_it_runs():
    has = cat.worker_has(WORKER, SKILLS)
    assert has["servers"] == [
        {
            "name": "jira",
            "runs_without_asking": ["get_issue", "search"],
            "switched_off": ["delete_issue"],
        },
        {"name": "kirocrew-core", "runs_without_asking": "all"},
    ]
    assert has["skills"] == ["release"]
    assert has["builtin_tools_left_out"] == ["execute_bash"]
    # Data only: no transport, which can carry credentials.
    assert "jira-mcp" not in json.dumps(has)


def test_the_gateway_lists_only_what_the_worker_lacks(monkeypatch):
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: GATEWAY_ROWS)
    lacks = cat.gateway_lacks(WORKER, SKILLS)
    assert [s["name"] for s in lacks["servers"]] == ["github", "slack"]
    assert lacks["servers"][0] == {
        "name": "github",
        "switched_on": True,
        "tools": ["list_prs", "merge_pr"],
    }
    assert lacks["servers"][1]["switched_on"] is False
    assert lacks["skills"] == ["triage"]


# ── the route: find_capabilities ──


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    class _Null:
        def log_api_access(self, **_kw):
            return None

    _floor_monkeypatch.setattr(guide_routes, "sel", lambda: _Null())


@pytest.fixture
def crewmate(monkeypatch):
    """A crewmate that owns its file, with a fixed worker and gateway."""
    monkeypatch.setattr(routes, "_is_mate", lambda _m: True)
    monkeypatch.setattr(cat, "worker_spec", lambda _m: json.loads(json.dumps(WORKER)))
    monkeypatch.setattr(
        routes, "_service", lambda _app: SimpleNamespace(_mate_catalog=lambda _m: SKILLS)
    )
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: GATEWAY_ROWS)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._find_server_spec_anywhere",
        lambda name: dict(GITHUB_ENTRY) if name == "github" else None,
    )


def _thread(state: FakeState):
    slot = state.open_slot(SLOT)
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = MEMBER
    return slot


FIND = ("POST", "/api/member/self/capabilities", routes.api_member_self_capabilities)
PROPOSE = ("POST", "/api/member/self/propose", self_routes.api_member_self_propose)


def _post(state, route, path, body):
    async def go(c):
        r = await c.post(path, json=body, headers=agent(f"dashboard:{SLOT}"))
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[route])


def test_find_capabilities_answers_worker_then_gateway_then_registry(crewmate, monkeypatch):
    seen: list[str] = []

    async def search(query, *, provider=None, limit=20):
        seen.append(query)
        return {
            "results": [
                {
                    "id": "io.github/acme-mcp",
                    "title": "Acme",
                    "description": "d",
                    "provider": "official",
                    "version": "1",
                    "installed": False,
                    "repo_url": "x",
                }
            ],
            "providers": ["official"],
            "provider_outcomes": [],
        }

    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp_discover.search_registry", search)
    state = FakeState()
    _thread(state)
    status, body = _post(state, FIND, "/api/member/self/capabilities", {"query": "acme"})
    assert status == 200, body
    assert list(body) == ["worker", "gateway", "registry", "next"]
    assert [s["name"] for s in body["worker"]["servers"]] == ["jira", "kirocrew-core"]
    assert [s["name"] for s in body["gateway"]["servers"]] == ["github", "slack"]
    assert body["registry"][0]["id"] == "io.github/acme-mcp"
    assert "repo_url" not in body["registry"][0]
    assert seen == ["acme"]
    # No query, no registry search.
    status, body = _post(state, FIND, "/api/member/self/capabilities", {})
    assert status == 200 and "registry" not in body and seen == ["acme"]


def test_find_capabilities_is_only_for_a_crewmate_in_its_own_chat(crewmate):
    state = FakeState()
    state.open_slot(SLOT)  # an ordinary chat: not a crewmate thread
    status, body = _post(state, FIND, "/api/member/self/capabilities", {})
    assert (status, body["code"]) == (403, "not_a_crewmate")


def test_find_capabilities_needs_the_crewmates_own_file(crewmate, monkeypatch):
    monkeypatch.setattr(routes, "_is_mate", lambda _m: False)
    state = FakeState()
    _thread(state)
    status, body = _post(state, FIND, "/api/member/self/capabilities", {})
    assert (status, body["code"]) == (409, "no_crewmate_file")


# ── the route: request_capabilities with additions ──


@pytest.fixture
def proposed(monkeypatch) -> list[dict[str, Any]]:
    from aiohttp import web

    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict[str, Any]] = []

    async def fake_propose(request, slot_key, sk, *, kind, params, reason):
        seen.append({"kind": kind, "params": params})
        return web.json_response({"id": "card-1", "kind": kind, "status": "pending"})

    monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    return seen


def _ask(state, draft):
    return _post(
        state,
        PROPOSE,
        "/api/member/self/propose",
        {"request": "capabilities", "params": {"draft": draft}},
    )


def test_a_gateway_server_is_copied_from_the_gateways_own_entry(crewmate, proposed):
    state = FakeState()
    _thread(state)
    status, body = _ask(
        state,
        {"additions": [{"server": "github", "source": "gateway", "auto_approve": ["list_prs"]}]},
    )
    assert status == 200, body
    (card,) = proposed
    assert card["kind"] == "crewmate.capabilities"
    params = card["params"]
    assert params["member"] == MEMBER
    assert params["origins"] == {"github": {"source": "gateway"}}
    assert params["draft"]["operations"] == [
        {
            "section": "mcpServers",
            "id": "github",
            "action": "set",
            # The per-scope switches stay behind: approvals travel as grants, and
            # the user's own per-tool switch reaches the worker on its own.
            "value": {"command": "gh-mcp", "args": ["--stdio"]},
        },
        {"section": "tools", "id": "@github", "action": "set", "value": True},
        {"section": "allowedTools", "id": "@github/list_prs", "action": "set", "value": True},
    ]
    # The params are what the card engine accepts.
    assert catalog.validate_params("crewmate.capabilities", params)["origins"] == params["origins"]


def test_a_registry_server_takes_the_official_install_plan(crewmate, proposed, monkeypatch):
    calls: list[str] = []

    async def plan(server_id):
        calls.append(server_id)
        return "acme-mcp", {"command": "npx", "args": ["-y", "acme-mcp"]}, "npm", []

    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp_discover.official_install_plan", plan)
    state = FakeState()
    _thread(state)
    status, body = _ask(
        state,
        {"additions": [{"server": "ignored", "source": "registry", "registry_id": "io/acme"}]},
    )
    assert status == 200, body
    params = proposed[0]["params"]
    assert calls == ["io/acme"]
    assert params["origins"] == {"acme-mcp": {"source": "registry", "registry_id": "io/acme"}}
    assert params["draft"]["operations"][0]["value"] == {
        "command": "npx",
        "args": ["-y", "acme-mcp"],
    }
    # Nothing in this path writes the user's MCP config: the card's plan is the
    # crewmate's own capability route.
    preview = catalog.build_preview(
        "crewmate.capabilities",
        params,
        {"revision": "r1"},
        {"impact": [{"section": "mcpServers", "id": "acme-mcp", "change": "added"}]},
    )
    assert {s["path"] for s in preview["apply"]} == {
        f"/api/agents/{MEMBER}/capabilities/preview",
        f"/api/agents/{MEMBER}/capabilities",
    }


@pytest.mark.parametrize(
    ("draft", "code"),
    [
        ({"additions": [{"server": "jira", "source": "gateway"}]}, "already_has"),
        ({"additions": [{"server": "nope", "source": "gateway"}]}, "not_on_gateway"),
        ({"additions": [{"skill": "release"}]}, "already_has"),
        ({"additions": [{"skill": "unknown"}]}, "unknown_skill"),
        ({"additions": [{"server": "github", "source": "capability"}]}, "invalid_additions"),
        (
            {"additions": [{"server": "github", "source": "gateway", "value": {"command": "x"}}]},
            "invalid_additions",
        ),
        (
            {"additions": [{"server": "github", "source": "gateway", "auto_approve": ["a b"]}]},
            "invalid_additions",
        ),
        ({"additions": []}, "invalid_additions"),
        ({"additions": [{"skill": "triage"}], "origins": {}}, "invalid_additions"),
        # A raw operation riding a card of additions would fold, unseen, under Details.
        (
            {
                "additions": [{"skill": "triage"}],
                "operations": [{"section": "tools", "id": "@g", "action": "set", "value": True}],
            },
            "invalid_additions",
        ),
    ],
)
def test_an_addition_the_gateway_cannot_vouch_for_is_refused(crewmate, proposed, draft, code):
    state = FakeState()
    _thread(state)
    status, body = _ask(state, draft)
    assert (status, body["code"]) == (400, code)
    assert proposed == []


def test_a_gateway_server_the_user_switched_off_is_not_added(crewmate, proposed, monkeypatch):
    rows = [SimpleNamespace(name="github", status="disabled", tools=[])]
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: rows)
    state = FakeState()
    _thread(state)
    status, body = _ask(state, {"additions": [{"server": "github", "source": "gateway"}]})
    assert (status, body["code"]) == (400, "not_on_gateway")
    assert proposed == []


def test_a_gateway_server_with_env_or_headers_is_not_copied_onto_a_card(
    crewmate, proposed, monkeypatch
):
    """A card's params reach the crewmate, so a credential must never ride in them."""
    for extra in ({"env": {"GITHUB_TOKEN": "ghp_x"}}, {"headers": {"Authorization": "Basic eA=="}}):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.mcp._find_server_spec_anywhere",
            lambda name, extra=extra: {**GITHUB_ENTRY, **extra} if name == "github" else None,
        )
        state = FakeState()
        _thread(state)
        status, body = _ask(state, {"additions": [{"server": "github", "source": "gateway"}]})
        assert (status, body["code"]) == (400, "carries_settings"), extra
        assert "ghp_x" not in json.dumps(body) and "eA==" not in json.dumps(body)
    assert proposed == []


@pytest.mark.parametrize(
    "args",
    [
        ["-y", "srv", "--api-key", "sk-live-1"],
        ["srv", "--api-key=sk-live-1"],
        ["run", "-e", "API_KEY=sk-live-1", "img"],
    ],
    ids=["option_value", "inline_option", "env_assignment"],
)
def test_a_gateway_server_with_a_credential_argument_is_not_copied(
    crewmate, proposed, monkeypatch, args
):
    """A credential passed as an argument would ride on the card as surely as env."""
    entry = {"command": "npx", "args": args}
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._find_server_spec_anywhere",
        lambda name: entry if name == "github" else None,
    )
    state = FakeState()
    _thread(state)
    status, body = _ask(state, {"additions": [{"server": "github", "source": "gateway"}]})
    assert (status, body["code"]) == (400, "carries_settings")
    assert "sk-live-1" not in json.dumps(body)
    assert proposed == []


def test_a_gateway_server_with_plain_arguments_is_still_copied(crewmate, proposed, monkeypatch):
    entry = {"command": "uvx", "args": ["mcp-server-git", "--repository", "/r"]}
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._find_server_spec_anywhere",
        lambda name: entry if name == "github" else None,
    )
    state = FakeState()
    _thread(state)
    status, body = _ask(state, {"additions": [{"server": "github", "source": "gateway"}]})
    assert status == 200, body
    assert len(proposed) == 1


def test_propose_change_cannot_supply_where_a_server_comes_from(monkeypatch):
    """Origins are the gateway's record; a caller of the generic path never sets one."""
    import asyncio

    from kiro_crew.dashboard.handlers import change_cards as card_routes

    async def caller(_request, _op):
        return "chat-x", "dashboard:chat-x"

    async def body(_request):
        return {
            "kind": "crewmate.capabilities",
            "params": {
                "member": MEMBER,
                "draft": {"operations": []},
                "origins": {"evil": {"source": "registry", "registry_id": "io/gh"}},
            },
        }

    async def never(*_a, **_k):
        raise AssertionError("a card was proposed")

    monkeypatch.setattr(card_routes, "_resolve_agent_caller", caller)
    monkeypatch.setattr(card_routes, "_json_body", body)
    monkeypatch.setattr(card_routes, "propose_for_caller", never)
    resp = asyncio.run(card_routes.api_cards_agent_propose(SimpleNamespace(app={"state": None})))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "invalid_param"


def _server_op(value):
    return {"draft": {"operations": [{"section": "mcpServers", "id": "github", "value": value}]}}


def test_only_the_gateways_own_entry_as_written_counts_as_the_gateways(crewmate, monkeypatch):
    same = cat.transport_for_file(dict(GITHUB_ENTRY))
    assert routes.gateway_matches(_server_op(same)) == {"github"}
    for changed in (
        {**same, "env": {"NODE_OPTIONS": "--require /tmp/x.js"}},
        {**same, "cwd": "/tmp"},
        {**same, "args": ["--stdio", "--extra"]},
    ):
        assert routes.gateway_matches(_server_op(changed)) == set(), changed
    off = [SimpleNamespace(name="github", status="disabled", tools=[])]
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: off)
    assert routes.gateway_matches(_server_op(same)) == set()


def test_a_registry_name_this_gateway_already_runs_is_refused(crewmate, proposed, monkeypatch):
    async def plan(_server_id):
        return "github", {"command": "other"}, "npm", []

    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp_discover.official_install_plan", plan)
    state = FakeState()
    _thread(state)
    status, body = _ask(
        state, {"additions": [{"server": "x", "source": "registry", "registry_id": "io/gh"}]}
    )
    assert (status, body["code"]) == (400, "name_in_use")
    assert proposed == []


def test_a_skill_addition_becomes_a_skill_operation(crewmate, proposed):
    state = FakeState()
    _thread(state)
    status, _ = _ask(state, {"additions": [{"skill": "triage"}]})
    assert status == 200
    assert proposed[0]["params"]["draft"]["operations"] == [
        {"section": "skills", "id": "triage", "action": "set", "value": True}
    ]


def test_a_crewmate_without_its_own_file_cannot_add(crewmate, proposed, monkeypatch):
    monkeypatch.setattr(routes, "_is_mate", lambda _m: False)
    state = FakeState()
    _thread(state)
    status, body = _ask(state, {"additions": [{"skill": "triage"}]})
    assert (status, body["code"]) == (409, "no_crewmate_file")


# ── the card ──


def _card(origins, gateway, impact):
    params = {
        "member": MEMBER,
        "draft": {
            "operations": cat.addition_operations(
                "github", {"command": "gh-mcp"}, ["list_prs", "merge_pr"]
            )
        },
        "origins": origins,
    }
    return catalog.build_preview(
        "crewmate.capabilities", params, {"revision": "r1"}, {"impact": impact, "gateway": gateway}
    )


IMPACT = [
    {"section": "mcpServers", "id": "github", "change": "added"},
    {"section": "tools", "id": "@github", "change": "added"},
    # The ceiling withheld @github/merge_pr: the preview's impact has no row for it.
    {
        "section": "allowedTools",
        "id": "@github/list_prs",
        "change": "added",
        "approval_expanded": True,
    },
]


def _row(preview, label):
    return next(r for r in preview["changes"] if r["label"] == label)


def test_the_card_names_where_each_server_comes_from_and_what_runs_unasked():
    preview = _card({"github": {"source": "gateway"}}, ["github"], IMPACT)
    assert "this gateway" in _row(preview, "github: comes from")["after"]
    # Exactly the approvals the preview kept after the ceiling.
    assert _row(preview, "github: runs without asking")["add"] == ["list_prs"]


def test_the_main_view_is_one_plain_line_per_addition_and_the_raw_rows_are_details():
    preview = _card({"github": {"source": "gateway"}}, ["github"], IMPACT)
    main = [r for r in preview["changes"] if not r.get("detail")]
    assert main == [
        {
            "label": "github",
            "addition": {
                "kind": "server",
                "name": "github",
                "origin": "gateway",
                "auto_approve": ["list_prs"],
            },
        },
        # A grant that widens what runs without asking is never only behind Details.
        {"label": "allowedTools", "add": ["@github/list_prs"], "remove": []},
    ]
    details = [r["label"] for r in preview["changes"] if r.get("detail")]
    assert "mcpServers" in details and "github" in details and "github: comes from" in details
    assert "allowedTools" not in details


#: The same gateway addition with no approval the preview counts as new.
IMPACT_NO_GRANT = [
    {"section": "mcpServers", "id": "github", "change": "added"},
    {"section": "tools", "id": "@github", "change": "added"},
]

#: The gateway addition plus a whole-server grant (``@github``).
IMPACT_WHOLE = [
    *IMPACT_NO_GRANT,
    {"section": "allowedTools", "id": "@github", "change": "added", "approval_expanded": True},
]


def test_a_gateway_server_with_no_new_approval_carries_no_risk_and_no_ack():
    preview = _card({"github": {"source": "gateway"}}, ["github"], IMPACT_NO_GRANT)
    assert preview["risk"] not in ("code_exec", "widen")
    assert preview["widen"] is False
    line = next(r for r in preview["changes"] if "addition" in r)
    assert line["addition"]["auto_approve"] == [] and "auto_approve_all" not in line["addition"]
    # No per-call promise: the crewmate's own approval mode (Trust, say) still decides.
    after = _row(preview, "github: runs without asking")["after"]
    assert after == "no new per-tool grant; current approval settings apply"
    assert "every call asks" not in after


def test_a_gateway_server_that_auto_approves_a_tool_keeps_the_acknowledgement():
    """The RFC's rule: an approval it would widen keeps its acknowledgement."""
    preview = _card({"github": {"source": "gateway"}}, ["github"], IMPACT)
    # No third-party warning: the gateway already runs this code.
    assert preview["risk"] == "widen"
    assert preview["widen"] is True
    # The grant is on the card's main view, not only behind Details.
    main = [r for r in preview["changes"] if not r.get("detail")]
    assert {"label": "allowedTools", "add": ["@github/list_prs"], "remove": []} in main


def test_a_gateway_server_granted_whole_keeps_the_ack_and_shows_every_tool():
    preview = _card({"github": {"source": "gateway"}}, ["github"], IMPACT_WHOLE)
    assert preview["risk"] == "widen"
    assert preview["widen"] is True
    line = next(r for r in preview["changes"] if "addition" in r)
    assert line["addition"]["auto_approve_all"] is True
    main = [r for r in preview["changes"] if not r.get("detail")]
    assert {"label": "allowedTools", "add": ["@github"], "remove": []} in main
    assert _row(preview, "github: runs without asking")["after"] == "every tool"


def test_a_registry_install_keeps_the_warning_and_its_checkbox():
    preview = _card({"github": {"source": "registry", "registry_id": "io/gh"}}, [], IMPACT)
    assert preview["risk"] == "code_exec"
    assert preview["widen"] is True
    line = next(r for r in preview["changes"] if "addition" in r)
    assert line["addition"]["origin"] == "registry"
    assert line["addition"]["registry_id"] == "io/gh"
    assert any(r["label"] == "github" and r.get("after") for r in preview["changes"])


def test_a_gateway_claim_the_gateway_no_longer_backs_is_not_shown_as_the_gateways():
    preview = _card({"github": {"source": "gateway"}}, [], IMPACT)
    assert _row(preview, "github: comes from")["after"].startswith("defined on this card")


def test_a_registry_origin_is_named_and_scoped_to_this_crewmate():
    preview = _card({"github": {"source": "registry", "registry_id": "io/gh"}}, [], IMPACT)
    where = _row(preview, "github: comes from")["after"]
    assert "MCP registry (io/gh)" in where and "this crewmate only" in where


def test_a_card_with_no_origins_keeps_its_own_rows():
    preview = _card({}, ["github"], IMPACT)
    assert not any("comes from" in r["label"] for r in preview["changes"])


@pytest.mark.parametrize(
    "origins",
    [
        {"github": {"source": "elsewhere"}},
        {"github": {"source": "registry"}},
        {"github": {"source": "gateway", "registry_id": "x"}},
        {"": {"source": "gateway"}},
        [],
    ],
)
def test_the_card_engine_refuses_a_malformed_origin(origins):
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            "crewmate.capabilities",
            {
                "member": MEMBER,
                "draft": {
                    "operations": [{"section": "tools", "id": "@g", "action": "set", "value": True}]
                },
                "origins": origins,
            },
        )


# ── the worker never asks ──


def test_the_worker_does_not_mount_the_conductors_request_tools(tmp_path, monkeypatch):
    from kiro_crew import agent
    from kiro_crew.agent_files import MATE_WORKER_AGENT_FILENAME
    from kiro_crew.agent_materialization import service_agents

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_worker_agent()
    from kiro_crew.config import paths as paths_mod

    spec = json.loads(
        (paths_mod.mate_agents_dir() / MATE_WORKER_AGENT_FILENAME).read_text(encoding="utf-8")
    )
    assert {
        "@kirocrew-mate/request_capabilities",
        "@kirocrew-mate/find_capabilities",
    } <= set(spec["excludedTools"])


# ── pressing the card: the crewmate's own file, picked up at the next spawn ──


def test_the_press_writes_the_crewmates_file_and_the_next_worker_carries_it(tmp_path, monkeypatch):
    import kiro_crew.mate_capabilities as mc
    from kiro_crew import mate_worker

    mates = tmp_path / "mates"
    mates.mkdir()
    path = mates / f"{MEMBER}.json"
    path.write_text(json.dumps({"name": MEMBER, "tools": [], "allowedTools": []}), "utf-8")
    monkeypatch.setattr(mc, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mc, "mate_config_path", lambda _m: path)
    global_mcp = tmp_path / "global-mcp.json"
    global_mcp.write_text("{}", "utf-8")

    ops = cat.addition_operations("acme", {"command": "npx", "args": ["acme"]}, ["read"])
    revision = mc._revision(json.loads(path.read_text("utf-8")))
    body = {"revision": revision, "operations": ops, "preview_token": revision}
    preview = mc.preview(MEMBER, body, {})
    assert {"section": "allowedTools", "id": "@acme/read"} in [
        {k: i[k] for k in ("section", "id")} for i in preview["impact"]
    ]
    mc.put(MEMBER, {**body, "accept_parent": [], "accept_members": []}, {})

    written = json.loads(path.read_text("utf-8"))
    assert written["mcpServers"]["acme"] == {"command": "npx", "args": ["acme"]}
    assert "@acme" in written["tools"] and "@acme/read" in written["allowedTools"]
    assert global_mcp.read_text("utf-8") == "{}"
    # The worker's next spawn merges the file: the server, mounted and approved.
    merged = mate_worker.merge_worker_spec(
        {"name": "w", "tools": [], "allowedTools": [], "mcpServers": {}},
        mate_worker.worker_additions({"mcpServers": {}}, written),
    )
    assert "@acme" in merged["tools"] and "@acme/read" in merged["allowedTools"]

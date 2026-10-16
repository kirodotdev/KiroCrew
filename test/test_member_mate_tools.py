"""Every new crewmate gets its own-record tools, and the text it hears matches what it has.

``member_mate_tools.grant_mate_tools_to_new_member`` enrolls a just-created
crewmate whose template lacks ``kirocrew-mate`` through the capability editor;
``member_mate_tools.platform_offers`` offers an existing one the same rows on its
Capabilities pane; the identity sentence, the first welcome and the goal nudges
name those tools only when its spec mounts them.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import editor_fixture  # noqa: F401
from agent_capabilities_helpers import save, spec_for

from kiro_crew import agent, member_mate_tools
from kiro_crew.config import loader
from kiro_crew.context_assembly import member as member_ctx
from kiro_crew.dashboard import goal_setup, mate_welcome

MATE_TOOLS = ("set_goal", "set_key_results", "set_check_ins", "update_self")

TRANSPORT = {"command": "/opt/kirocrew", "args": ["mcp", "mate"]}


@pytest.fixture(name="granting")
def granting_fixture(editor, monkeypatch):
    """The capability editor, with the mate transport and the policy pinned."""
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: dict(TRANSPORT)
    )
    monkeypatch.setattr(member_mate_tools, "_policy_permits", lambda member: True)
    return editor


def _grant(member="A"):
    loader._invalidate_config_cache()
    return member_mate_tools.grant_mate_tools_to_new_member(member, catalog=lambda project: {})


def _binding(home, member="A"):
    return json.loads((home / "config.json").read_text())["agents"][member]


def test_a_template_without_the_server_is_enrolled_with_it(granting) -> None:
    service, home, specs, parent = granting
    assert _grant() == member_mate_tools.GRANTED
    assert _binding(home)["kiro_agent"] != "parent"
    spec = spec_for(home, specs)
    # Exactly the default template's entry: no ``disabled`` key, which a
    # per-session element cannot express and would cost the server its identity.
    assert spec["mcpServers"]["kirocrew-mate"] == TRANSPORT
    from kiro_crew.acp.session_mcp import native_mount_withholding

    assert (
        native_mount_withholding("kirocrew-mate", spec["mcpServers"]["kirocrew-mate"], ()) is None
    )
    assert "@kirocrew-mate" in spec["tools"]
    assert set(agent._MATE_AUTO_GRANTS) <= set(spec["allowedTools"])
    # Every other row keeps the template's value and still inherits.
    assert spec["mcpServers"]["search"] == parent["mcpServers"]["search"]
    assert "read" in spec["tools"] and spec["prompt"] == "original"
    assert spec["resources"] == parent["resources"]
    assert member_mate_tools.spec_has_mate_tools(spec)
    # B shares the same template and was not touched.
    assert _binding(home, "B")["kiro_agent"] == "parent"
    # A second call finds the member enrolled and writes nothing.
    before = (specs / f"{_binding(home)['kiro_agent']}.json").read_bytes()
    assert _grant() == member_mate_tools.ALREADY_ENROLLED
    assert (specs / f"{_binding(home)['kiro_agent']}.json").read_bytes() == before


def test_a_template_that_already_mounts_it_is_left_alone(granting) -> None:
    _, home, specs, parent = granting
    parent = {
        **parent,
        "tools": ["read", "@kirocrew-mate"],
        "mcpServers": {**parent["mcpServers"], "kirocrew-mate": dict(TRANSPORT)},
    }
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    assert _grant() == member_mate_tools.TEMPLATE_HAS_IT
    assert _binding(home)["kiro_agent"] == "parent"
    assert json.loads((specs / "parent.json").read_text()) == parent


def test_a_template_that_mutes_it_is_left_alone(granting) -> None:
    _, home, specs, parent = granting
    parent = {
        **parent,
        "mcpServers": {**parent["mcpServers"], "kirocrew-mate": {**TRANSPORT, "disabled": True}},
    }
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    assert _grant() == member_mate_tools.TEMPLATE_MUTES_IT
    assert _binding(home)["kiro_agent"] == "parent"


def test_a_backend_that_cannot_load_the_copy_gets_no_enrollment(granting, monkeypatch) -> None:
    _, home, _, _ = granting
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities.member_backend_warning", lambda spec: {"backend": "x"}
    )
    assert _grant() == member_mate_tools.BACKEND_UNSUPPORTED
    assert _binding(home)["kiro_agent"] == "parent"


def test_a_policy_that_withholds_the_server_gets_no_enrollment(granting, monkeypatch) -> None:
    _, home, _, _ = granting
    monkeypatch.setattr(member_mate_tools, "_policy_permits", lambda member: False)
    assert _grant() == member_mate_tools.POLICY_WITHHOLDS
    assert _binding(home)["kiro_agent"] == "parent"


def test_an_unavailable_server_gets_no_enrollment(granting, monkeypatch) -> None:
    _, home, _, _ = granting
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: None
    )
    assert _grant() == member_mate_tools.SERVER_UNAVAILABLE
    assert _binding(home)["kiro_agent"] == "parent"


def test_the_default_template_and_the_default_member_are_skipped(granting) -> None:
    _, home, _, _ = granting
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "kirocrew"
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    assert _grant() == member_mate_tools.DEFAULT_TEMPLATE
    assert _grant("default") == member_mate_tools.NOT_A_MEMBER
    assert _grant("nobody") == member_mate_tools.NOT_A_MEMBER


def test_a_later_save_keeps_the_platform_server_current(granting, monkeypatch) -> None:
    service, home, specs, _ = granting
    assert _grant() == member_mate_tools.GRANTED
    moved = {"command": "/new/kirocrew", "args": ["mcp", "mate"]}
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: dict(moved)
    )
    save(service, [])
    assert spec_for(home, specs)["mcpServers"]["kirocrew-mate"] == moved
    # A build that stops emitting the server retracts it from the copy.
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: None
    )
    save(service, [])
    assert "kirocrew-mate" not in spec_for(home, specs).get("mcpServers", {})


def test_a_refresh_keeps_a_per_tool_restriction_the_copy_holds(monkeypatch) -> None:
    from kiro_crew import agent_capabilities

    moved = {"command": "/new/kirocrew", "args": ["mcp", "mate"]}
    monkeypatch.setattr(agent_capabilities, "_member_platform_transport", lambda name: dict(moved))
    monkeypatch.setattr(agent_capabilities, "_parent_is_owned", lambda snap: False)
    held = {**TRANSPORT, "disabledTools": ["update_self"]}
    spec = {"mcpServers": {"kirocrew-mate": held}}
    agent_capabilities._maintain_owned({"parent_spec": {"mcpServers": {}}}, spec)
    assert spec["mcpServers"]["kirocrew-mate"] == {**moved, "disabledTools": ["update_self"]}


def _offers(service, member="A"):
    loader._invalidate_config_cache()
    return service.get(member)["platform_offers"]


def _mate_row(view):
    return next(
        (r for r in view["rows"] if r["section"] == "mcpServers" and r["id"] == "kirocrew-mate"),
        None,
    )


def test_an_existing_member_is_offered_what_creation_grants(granting) -> None:
    service, home, specs, parent = granting
    view = service.get("A")
    # The member's spec has no row for the server, so the offer is the way to add it.
    assert view["mode"] == "shared" and _mate_row(view) is None
    assert view["platform_offers"] == [
        {
            "id": "kirocrew-mate",
            "value": TRANSPORT,
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "kirocrew-mate",
                    "action": "set",
                    "value": {"disabled": False},
                },
                {"section": "tools", "id": "@kirocrew-mate", "action": "set", "value": True},
                *(
                    {"section": "allowedTools", "id": grant, "action": "set", "value": True}
                    for grant in agent._MATE_AUTO_GRANTS
                ),
            ],
        }
    ]
    # Offering writes nothing.
    assert _binding(home)["kiro_agent"] == "parent"
    assert json.loads((specs / "parent.json").read_text()) == parent


def test_saving_the_offer_gives_the_next_session_the_mate_tools(granting) -> None:
    from kiro_crew.acp.session_mcp import native_mount_withholding
    from kiro_crew.agent_capabilities import prepare_member_capabilities

    service, home, specs, parent = granting
    offer = _offers(service)[0]
    save(service, offer["operations"], enroll=True)
    loader._invalidate_config_cache()
    spec = spec_for(home, specs)
    # The entry creation writes: the default template's, with no ``disabled`` key.
    assert spec["mcpServers"]["kirocrew-mate"] == TRANSPORT
    assert (
        native_mount_withholding("kirocrew-mate", spec["mcpServers"]["kirocrew-mate"], ()) is None
    )
    assert "@kirocrew-mate" in spec["tools"]
    assert set(agent._MATE_AUTO_GRANTS) <= set(spec["allowedTools"])
    assert spec["mcpServers"]["search"] == parent["mcpServers"]["search"]
    # The chat-start seam accepts the saved copy and launches the server.
    assert "kirocrew-mate" in prepare_member_capabilities("A")["mcp_servers"]
    assert member_mate_tools.member_has_mate_tools("A") is True
    view = service.get("A")
    assert _mate_row(view)["present"] is True and view["platform_offers"] == []
    # B shares the template and was not touched.
    assert _binding(home, "B")["kiro_agent"] == "parent"


def test_a_member_of_an_owned_conductor_template_gets_the_refreshed_entry(
    granting, monkeypatch
) -> None:
    """A crewmate on the owned ``kirocrew-conductor`` template, with no private copy."""
    import sys

    from kiro_crew.acp.session_mcp import native_mount_withholding
    from kiro_crew.agent_capabilities import prepare_member_capabilities
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    service, home, specs, parent = granting
    managed = {name: {"command": sys.executable, "args": [name]} for name in ("kirocrew-core",)}
    managed["kirocrew-mate"] = {"command": sys.executable, "args": ["mcp-mate"]}
    monkeypatch.setattr(agent, "_MANAGED_MCP_SERVERS", managed)
    allowed = ["@kirocrew-core/send_message"]
    conductor = {
        **parent,
        "name": "kirocrew-conductor",
        "tools": ["fs_read", "@kirocrew-core"],
        "allowedTools": allowed,
        "permissions": derived_agent_permissions(allowed, "kirocrew-conductor"),
        "mcpServers": {"kirocrew-core": {"command": sys.executable, "args": ["kirocrew-core"]}},
    }
    (specs / "kirocrew-conductor.json").write_text(json.dumps(conductor), encoding="utf-8")
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "kirocrew-conductor"
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    offer = _offers(service)[0]
    save(service, offer["operations"], enroll=True)
    loader._invalidate_config_cache()
    spec = spec_for(home, specs)
    entry = spec["mcpServers"]["kirocrew-mate"]
    assert (entry["command"], entry["args"]) == (sys.executable, ["mcp-mate"])
    assert "disabled" not in entry and native_mount_withholding("kirocrew-mate", entry, ()) is None
    assert {"@kirocrew-core", "@kirocrew-mate"} <= set(spec["tools"])
    assert set(agent._MATE_AUTO_GRANTS) | set(allowed) <= set(spec["allowedTools"])
    assert "kirocrew-mate" in prepare_member_capabilities("A")["mcp_servers"]
    assert member_mate_tools.member_has_mate_tools("A") is True


def test_an_enrolled_member_is_offered_it_too(granting) -> None:
    service, home, specs, _ = granting
    save(service, [], enroll=True)
    offer = _offers(service)[0]
    assert service.get("A")["mode"] == "inherited"
    save(service, offer["operations"])
    loader._invalidate_config_cache()
    assert member_mate_tools.member_has_mate_tools("A") is True
    assert _offers(service) == []


def _write_parent(specs, parent, **changes):
    (specs / "parent.json").write_text(json.dumps({**parent, **changes}), encoding="utf-8")


@pytest.mark.parametrize(
    "case",
    ["mounted", "muted", "removed", "default_template", "policy", "unavailable", "backend"],
)
def test_no_offer_where_creation_would_skip(granting, monkeypatch, case) -> None:
    service, home, specs, parent = granting
    servers = parent["mcpServers"]
    if case == "mounted":
        mate = {"kirocrew-mate": dict(TRANSPORT)}
        _write_parent(specs, parent, tools=["@kirocrew-mate"], mcpServers={**servers, **mate})
    elif case == "muted":
        mate = {"kirocrew-mate": {**TRANSPORT, "disabled": True}}
        _write_parent(specs, parent, mcpServers={**servers, **mate})
    elif case == "removed":
        save(
            service,
            [{"section": "mcpServers", "id": "kirocrew-mate", "action": "remove"}],
            enroll=True,
        )
        assert _mate_row(service.get("A"))["state"] == "removed"
    elif case == "default_template":
        (specs / "kirocrew.json").write_text(
            json.dumps({**parent, "name": "kirocrew"}), encoding="utf-8"
        )
        config = json.loads((home / "config.json").read_text())
        config["agents"]["A"]["kiro_agent"] = "kirocrew"
        (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    elif case == "policy":
        monkeypatch.setattr(member_mate_tools, "_policy_permits", lambda member: False)
    elif case == "unavailable":
        monkeypatch.setattr(
            "kiro_crew.agent_capabilities._member_platform_transport", lambda name: None
        )
    else:
        monkeypatch.setattr(
            "kiro_crew.agent_capabilities.member_backend_warning", lambda spec: {"backend": "x"}
        )
    assert _offers(service) == []
    if case in ("removed", "default_template"):
        # A choice about A alone: B, on the same template, is still offered.
        assert [offer["id"] for offer in _offers(service, "B")] == ["kirocrew-mate"]


def test_a_parent_that_gains_the_server_is_a_parent_change_not_an_offer(granting) -> None:
    service, _, specs, parent = granting
    save(service, [], enroll=True)
    mate = {"kirocrew-mate": dict(TRANSPORT)}
    _write_parent(
        specs, parent, tools=["read", "@kirocrew-mate"], mcpServers={**parent["mcpServers"], **mate}
    )
    view = service.get("A")
    assert any(c["id"] == "kirocrew-mate" for c in view["parent_changes"])
    assert view["platform_offers"] == []


def test_an_offer_that_cannot_be_worked_out_is_not_made(granting, monkeypatch) -> None:
    service, _, _, _ = granting

    def broken(member):
        raise RuntimeError("governance unavailable")

    monkeypatch.setattr(member_mate_tools, "_policy_permits", broken)
    view = service.get("A")
    assert view["platform_offers"] == [] and view["mode"] == "shared"


@pytest.mark.asyncio
@pytest.mark.parametrize("user,app_id", [("intruder", ""), ("owner", "some-app")])
async def test_only_the_owner_sees_or_saves_the_offer(granting, user, app_id) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, home, specs, _ = granting
    view = await asyncio.to_thread(service.get, "A")
    offer = view["platform_offers"][0]
    body = {
        "revision": view["revision"],
        "enroll": True,
        "operations": offer["operations"],
        "accept_parent": [],
        "accept_members": [],
    }

    def build(caller, scoped_app):
        @web.middleware
        async def identity(request, handler):
            request["user"], request["app"] = caller, scoped_app
            return await handler(request)

        app = web.Application(middlewares=[identity])
        app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda *_: None)
        app[_SERVICE] = service
        register(app)
        return app

    async with TestClient(TestServer(build(user, app_id))) as client:
        for method, suffix in (("get", ""), ("post", "/preview"), ("put", "")):
            kwargs = {} if method == "get" else {"json": {**body, "preview_token": "x"}}
            response = await getattr(client, method)(
                "/api/agents/A/capabilities" + suffix, **kwargs
            )
            assert response.status == 403
            assert "platform_offers" not in await response.text()
    assert (await asyncio.to_thread(_binding, home))["kiro_agent"] == "parent"
    async with TestClient(TestServer(build("owner", ""))) as client:
        response = await client.get("/api/agents/A/capabilities")
        assert response.status == 200
        assert (await response.json())["platform_offers"] == [offer]
        preview = await client.post("/api/agents/A/capabilities/preview", json=body)
        token = (await preview.json())["preview_token"]
        saved = await client.put(
            "/api/agents/A/capabilities", json={**body, "preview_token": token}
        )
        assert saved.status == 200
        assert (await saved.json())["platform_offers"] == []
    assert "@kirocrew-mate" in (await asyncio.to_thread(spec_for, home, specs))["tools"]


def test_has_mate_tools_reads_the_spec_the_member_runs(granting) -> None:
    _, home, _, _ = granting
    loader._invalidate_config_cache()
    assert member_mate_tools.member_has_mate_tools("A") is False
    assert _grant() == member_mate_tools.GRANTED
    loader._invalidate_config_cache()
    assert member_mate_tools.member_has_mate_tools("A") is True
    # Nothing to read keeps the wording every crewmate had before.
    assert member_mate_tools.member_has_mate_tools("nobody") is True
    assert member_mate_tools.binding_has_mate_tools(SimpleNamespace()) is True


def test_a_project_template_shadows_the_global_one(granting, tmp_path, monkeypatch) -> None:
    _, _, specs, parent = granting
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.config.loader.default_project_dir", lambda _: str(project))
    with_mate = {
        **parent,
        "tools": ["@kirocrew-mate"],
        "mcpServers": {"kirocrew-mate": dict(TRANSPORT)},
    }
    (specs / "parent.json").write_text(json.dumps(with_mate), encoding="utf-8")
    shadow = project / ".kiro" / "agents" / "parent.json"
    shadow.write_text(json.dumps(parent), encoding="utf-8")
    binding = SimpleNamespace(kiro_agent="parent", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(binding) is False
    # An unreadable project copy keeps the default wording and never reads the
    # global template in its place.
    shadow.write_bytes(b"\xff")
    monkeypatch.setattr(
        agent, "agent_spec_path", lambda *a, **k: pytest.fail("fell back to a scan")
    )
    assert member_mate_tools.binding_has_mate_tools(binding) is True
    traversal = SimpleNamespace(kiro_agent="../parent", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(traversal) is True


def test_spec_has_mate_tools_needs_the_server_and_a_tools_ref() -> None:
    entry = {"kirocrew-mate": dict(TRANSPORT)}
    assert member_mate_tools.spec_has_mate_tools({"mcpServers": entry, "tools": ["*"]})
    assert member_mate_tools.spec_has_mate_tools(
        {"mcpServers": entry, "tools": ["@kirocrew-mate/set_goal"]}
    )
    assert not member_mate_tools.spec_has_mate_tools({"mcpServers": entry, "tools": ["read"]})
    assert not member_mate_tools.spec_has_mate_tools({"tools": ["@kirocrew-mate"]})
    muted = {"kirocrew-mate": {**TRANSPORT, "disabled": True}}
    assert not member_mate_tools.spec_has_mate_tools({"mcpServers": muted, "tools": ["*"]})


def test_the_identity_sentence_names_the_tools_only_when_they_are_there() -> None:
    assert all(t in member_ctx._MATE_TOOLS_IDENTITY for t in MATE_TOOLS)
    assert not any(t in member_ctx._NO_MATE_TOOLS_IDENTITY for t in MATE_TOOLS)
    assert "Goals tab" in member_ctx._NO_MATE_TOOLS_IDENTITY


def test_the_first_welcomes_follow_the_tools() -> None:
    entry = SimpleNamespace(display_name="Scout", description="")
    with_tools = mate_welcome.goal_kickoff(entry, "scout")
    without = mate_welcome.goal_kickoff(entry, "scout", has_mate_tools=False)
    assert "set_goal" in with_tools
    assert "set_goal" not in without and "Goals tab" in without
    described = SimpleNamespace(display_name="Scout", description="ship notes")
    known = mate_welcome.goal_kickoff(described, "scout", has_mate_tools=False)
    assert "ship notes" in known and "set_goal" not in known
    for others in (False, True):
        mate = mate_welcome.welcome_kickoff(entry, has_other_crewmates=others)
        plain = mate_welcome.welcome_kickoff(
            entry, has_other_crewmates=others, has_mate_tools=False
        )
        assert "update_self" in mate
        assert "update_self" not in plain and "crewmate page" in plain


GOAL = {"id": "g1", "text": "Keep the release notes current"}


def test_the_goal_nudges_follow_the_tools() -> None:
    rec = {"goals": [GOAL], "key_results": [], "schedules": {}}
    assert "set_key_results" in goal_setup.kickoff_prompt(rec, "g1")
    bare = goal_setup.kickoff_prompt(rec, "g1", has_mate_tools=False)
    assert bare is not None and "set_key_results" not in bare and "Goals tab" in bare
    krs = {**rec, "key_results": [{"id": "k1", "text": "notes out", "goal_ids": ["g1"]}]}
    assert "set_check_ins" in goal_setup.milestones_prompt(krs)
    off = goal_setup.milestones_prompt(krs, has_mate_tools=False)
    assert off is not None and "set_check_ins" not in off and "Goals tab" in off
    removed = {
        "goals": [],
        "key_results": [{"id": "k1", "text": "notes out", "goal_ids": []}],
        "removed_goal": {"goal": GOAL, "index": 0, "at": 1, "key_result_ids": ["k1"]},
    }
    assert "set_key_results" in goal_setup.removed_prompt(removed, "g1")
    gone = goal_setup.removed_prompt(removed, "g1", has_mate_tools=False)
    assert gone is not None and "set_key_results" not in gone and "Goals tab" in gone


def test_the_shipped_defaults_auto_approve_every_mate_grant() -> None:
    from importlib import resources

    defaults = json.loads(
        resources.files("kiro_crew.config").joinpath("defaults.json").read_text(encoding="utf-8")
    )
    shipped = {t for t in defaults["allowedTools"] if t.startswith("@kirocrew-mate/")}
    assert shipped == set(agent._MATE_AUTO_GRANTS)

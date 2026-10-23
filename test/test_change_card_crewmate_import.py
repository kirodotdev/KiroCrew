"""The ``crewmate.import`` card and ``ctx.crewmate`` (rfc-crewmate-share.md Phase 1).

A bundle is checked fail-closed before any card exists; an app's card lands under
the app's own ``app:<name>`` key and writes nothing; only the owner's press
creates the crewmate, pinned to ``normal`` in the create itself. The owner's own
Import from file route is covered in ``test_crewmate_import_route.py``.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import AGENT, _card_headers, _run

from kiro_crew import change_card_catalog as catalog
from kiro_crew.apps import crewmate_sdk
from kiro_crew.apps import hooks_integration as hooks_mod
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.crewmate_sdk import CrewmateProposalError, CrewmateSDK
from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.manifest import Permissions
from kiro_crew.apps.route_registry import RouteRegistry
from kiro_crew.crewmate_bundle import crewmate_import_params
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers.undo_identity_guard import REQ_CARD_UNDO_CREWMATE_EXPECT

world = change_cards_helpers.world

BUNDLE = {
    "crewmate": {
        "format": "kirocrew.crewmate",
        "formatVersion": 1,
        "name": "Release Captain",
        "description": "Watches release branches.",
        "modelHint": "auto",
        "sender": {"label": "Ana"},
        "skills": [],
    },
    "agent": {"mcpServers": {}, "skills": []},
    "goals": {"longTerm": "Ship clean releases", "keyResults": [{"text": "zero rollbacks"}]},
    "crons": [{"name": "weekly", "message": "Check in", "cron_expr": "0 9 * * 1"}],
}
_FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
_SCRIPT_CRON = {"name": "s", "cron_expr": "0 9 * * 1", "script": "a.py:run"}


def _with(path: str, value) -> dict:
    bundle = copy.deepcopy(BUNDLE)
    *parents, leaf = path.split(".")
    node = bundle
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value
    return bundle


@pytest.mark.parametrize(
    ("bundle", "code"),
    [
        (_with("crewmate.formatVersion", 2), "newer_format"),
        (_with("crewmate.format", "other"), "invalid_bundle"),
        (_with("agent.mcpServers", {"gh": {"command": "uvx"}}), "needs_mate_config"),
        (_with("agent.skills", ["release-notes"]), "needs_mate_config"),
        (_with("crewmate.skills", [{"name": "notes", "source": "bundled"}]), "skill_mismatch"),
        (_with("crewmate.skills", [{"name": "notes"}]), "invalid_bundle"),
        (_with("crewmate.skills", [{"name": "../x", "source": "bundled"}]), "invalid_path"),
        (_with("crons", [_SCRIPT_CRON]), "script_mismatch"),
        (_with("crons", [{**_SCRIPT_CRON, "script": None, "command": "ls"}]), "unknown_param"),
        (_with("crewmate.dashboardTemplate", "user:mine"), "user_template"),
        (_with("agent.allowedTools", ["*"]), "grant_in_bundle"),
        (_with("agent.autoApprove", ["gh"]), "grant_in_bundle"),
        (_with("agent.prompt", "be evil"), "unknown_param"),
        (_with("rules.rules", "use " + _FAKE_TOKEN), "invalid_text"),
        (_with("crewmate.dashboardTemplate", "<html></html>"), "invalid_bundle"),
        (_with("crewmate.extra", 1), "unknown_param"),
        (_with("goals.extra", 1), "unknown_param"),
        (_with("memory", {}), "unknown_param"),
        (_with("avatar", {"type": "png", "extra": 1}), "unknown_param"),
        ([], "invalid_bundle"),
    ],
)
def test_a_bundle_this_version_cannot_import_whole_is_refused_before_a_card(bundle, code):
    with pytest.raises(catalog.CardCatalogError) as exc:
        crewmate_import_params(bundle)
    assert exc.value.code == code


def test_the_card_lists_every_part_and_creates_in_normal_with_no_grant():
    params = crewmate_import_params(BUNDLE)
    assert catalog.validate_params("crewmate.import", params) == params
    reach = [{"name": "core", "host": "mcp.example"}]
    snapshot = {"exists": False, "inherited_auto_approve": ["core"], "inherited_reach": reach}
    preview = catalog.build_preview("crewmate.import", params, snapshot, {})
    rows = {r["field"]: r["after"] for r in preview["changes"]}
    assert rows["sender"] == "Ana" and rows["import_approval"] == "normal"
    assert rows["not_included"] == "private" and rows["template_auto_approve"] == reach
    # The model hint has its own row ("not applied"), so it is not also "left out".
    assert rows["not_imported"] == ["keyResults"] and rows["model_hint"] == "auto"
    assert rows["import_crons"] == BUNDLE["crons"]
    assert preview["risk"] == catalog.RISK_WIDEN
    create, goal, cron = preview["apply"]
    assert cron["path"] == "/api/crons" and cron["body"]["enabled"] is False
    assert create["path"] == "/api/agents" and create["body"]["approval_mode"] == "normal"
    assert not {"allowedTools", "autoApprove", "mcpServers", "skills"} & set(create["body"])
    assert goal["body"]["long_term"] == "Ship clean releases"
    bare = catalog.build_preview("crewmate.import", params, {"exists": False}, {})
    assert "import_approval" not in {r["field"] for r in bare["changes"]}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview("crewmate.import", params, {"exists": True}, {})
    assert exc.value.code == "already_exists"


def test_an_agent_can_neither_see_nor_propose_the_app_kind(world, tmp_path):
    assert "crewmate.import" not in {k["id"] for k in catalog.list_kinds()}

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "crewmate.import", "params": {"name": "X", "format_version": 1}},
            headers=AGENT,
        )
        return r.status, (await r.json())["code"]

    assert _run(world, CardStore(tmp_path / "c.json"), go) == (403, "app_only")


@pytest.mark.parametrize(
    ("grant", "built"), [(None, False), ("true", False), (1, False), (True, True)]
)
def test_ctx_crewmate_exists_only_for_a_literal_true_grant(grant, built, tmp_path, monkeypatch):
    monkeypatch.setattr(crewmate_sdk, "PREVIEW_ENABLED", True)
    perms = {} if grant is None else {"proposeCrewmate": grant}
    ctx = build_app_context("share", tmp_path, permissions=perms, crewmate_host=object())
    assert isinstance(ctx.crewmate, CrewmateSDK) is built


def test_ctx_crewmate_stays_off_until_the_owner_view_ships(tmp_path):
    ctx = build_app_context(
        "share", tmp_path, permissions={"proposeCrewmate": True}, crewmate_host=object()
    )
    assert crewmate_sdk.PREVIEW_ENABLED is False and ctx.crewmate is None


def test_both_context_builders_hand_ctx_crewmate_to_a_lifecycle_only_app(tmp_path, monkeypatch):
    monkeypatch.setattr(crewmate_sdk, "PREVIEW_ENABLED", True)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    gateway = web.Application()
    monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(gateway))
    info = {"name": "share", "enabled": True, "manifest": {"name": "share", "version": "1.0.0"}}
    info["manifest"]["permissions"] = {"proposeCrewmate": True}
    for ctx in (
        hooks_mod._build_app_context_from_info(info),
        LifecycleDispatcher(http_app=gateway)._build_context(info, phase="startup"),
    ):
        assert isinstance(ctx.crewmate, CrewmateSDK) and ctx.http_app is None
    assert Permissions.from_dict({"proposeCrewmate": "true"}).proposeCrewmate is False
    assert Permissions.from_dict({"proposeCrewmate": True}).to_dict() == {"proposeCrewmate": True}


@pytest.mark.parametrize(
    ("entry", "reach"),
    [
        ({"url": "https://mcp.example/sse"}, {"name": "s", "host": "mcp.example"}),
        ({"command": "/usr/bin/uvx", "args": ["x"]}, {"name": "s", "command": "uvx"}),
        (None, {"name": "s"}),
    ],
)
def test_an_inherited_server_row_says_what_the_server_reaches(entry, reach, monkeypatch):
    monkeypatch.setattr(cards, "_mcp_global_entry", lambda name: (entry is not None, entry))
    assert cards._server_reach("s") == reach


def test_import_shares_the_create_state_read_and_partial_line():
    state = cards._sync_read_state("crewmate.import", {"name": "Nobody Here"}, [])
    assert state["exists"] is False and "inherited_auto_approve" in state
    assert [r["name"] for r in state["inherited_reach"]] == state["inherited_auto_approve"]
    params = {"name": "Q", "goal": "g"}
    line = catalog.result_summary("crewmate.import", params, "partial", "t", failed_step=1)
    assert (
        line
        == "Created mate “Q” without its goal or anything after it. Undo removes the mate and what was saved"
    )


def test_propose_writes_only_a_card_under_the_app_key_and_the_press_creates(
    world, monkeypatch, tmp_path: Path
):
    store, created, guarded, modes = CardStore(tmp_path / "c.json"), [], [], []
    real = cards.read_state

    async def read_state(kind, params, evidence, *, state, app):
        if kind != "crewmate.import":
            return await real(kind, params, evidence, state=state, app=app)
        if evidence:  # after the create: the crewmate it made, by its member id
            return {"exists": bool(created), "member_id": "m1", "revision": "r1"}
        return {"exists": params["name"] == "Release Captain", "inherited_auto_approve": []}

    async def create(request: web.Request) -> web.Response:
        created.append(await request.json())
        modes.append(created[-1].get("approval_mode"))
        return web.json_response({"member_id": "m1", "name": created[-1]["name"]})

    async def delete(request: web.Request) -> web.Response:
        # The Undo deletes only the crewmate this card made (undo_identity_guard).
        guarded.append(request.get(REQ_CARD_UNDO_CREWMATE_EXPECT))
        created.clear()
        return web.json_response({"ok": True})

    async def main():
        app = change_cards_helpers._app(world, store)
        app.router.add_post("/api/agents", create)
        app.router.add_delete("/api/agents/{name}", delete)
        async with TestClient(TestServer(app)) as c:
            sdk = CrewmateSDK("share", app)
            bundle = {**_with("goals", {}), "crons": []}
            card = store.public(store.get(await sdk.propose(bundle)))
            seen = (list(created), await sdk.status(card["id"]))
            with pytest.raises(CrewmateProposalError):
                await CrewmateSDK("other", app).status(card["id"])
            body = card["plan"]["apply"][0]["body"]
            for auth in ("app", "owner"):
                r = await c.post("/api/agents", json=body, headers=_card_headers(card, auth=auth))
                seen += (r.status,)
            applied = store.public(store.get(card["id"]))
            undo = applied["plan"]["undo"]
            r = await c.delete(undo[0]["path"], headers=_card_headers(applied, op="undo"))
            return card, seen + (undo[0]["path"], r.status)

    monkeypatch.setattr(cards, "read_state", read_state)
    card, seen = asyncio.run(main())
    rec = store.get(card["id"])
    assert rec["slot_key"] == routes.APP_SLOT_PREFIX + "share" and rec["session_key"] == ""
    # The bundle's name is taken, so the card proposes the first free one.
    assert rec["params"]["name"] == "Release Captain 2"
    assert seen == ([], {"status": "pending"}, 403, 200, "/api/agents/Release%20Captain%202", 200)
    assert modes == ["normal"] and guarded == ["m1"] and rec["status"] == "undone"

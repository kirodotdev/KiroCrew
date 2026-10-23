"""Change cards: the routes, the route hook and the tool wiring.

Runs the card routes in an in-process aiohttp app behind a stand-in auth middleware:
propose, apply and undo through the real handlers, the hook that records manual saves
and forwards the ones it cannot capture, the shim's routing of the card tools, the
member-capabilities view, the preview shapes and the card read routes.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import (
    AGENT,
    OWNER,
    REPO,
    SECRET_VALUE,
    SK,
    SLOT,
    FakeState,
    _app,
    _card_headers,
    _fake_auth,
    _propose_setting,
    _run,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_guide
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes

# The shared fixtures, bound here by name so pytest registers them for this module.
world = change_cards_helpers.world


# ── routes and the hook ──


def test_real_setting_reader_and_validation_use_the_settings_registry():
    async def go():
        before = await cards.read_state(
            "setting.change",
            {"path": "agent.approval_mode", "value": "auto"},
            [],
            state=None,
            app=None,
        )
        ok = await cards.read_context(
            "setting.change",
            {"path": "agent.approval_mode", "value": "auto"},
            before,
            state=None,
            app=None,
        )
        return before, ok

    before, ok = asyncio.run(go())
    assert before["value"] in ("auto", "interactive") and ok == {}
    with pytest.raises(catalog.CardCatalogError) as exc:
        asyncio.run(
            cards.read_context(
                "setting.change",
                {"path": "agent.approval_mode", "value": "yolo"},
                {},
                state=None,
                app=None,
            )
        )
    assert exc.value.code == "invalid_value"
    with pytest.raises(catalog.CardCatalogError) as exc:
        asyncio.run(
            cards.read_context(
                "setting.change",
                {"path": "agent.not_a_setting", "value": 1},
                {},
                state=None,
                app=None,
            )
        )
    assert exc.value.code == "setting_not_editable"


def test_propose_lands_in_the_callers_slot_and_only_an_agent_may_propose(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        owner = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "setting.change", "params": {"path": "chat.verbosity", "value": "x"}},
            headers=OWNER,
        )
        sub = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "setting.change", "params": {"path": "chat.verbosity", "value": "x"}},
            headers={"X-Test-Auth": "internal", "X-Session-Key": "subagent:abc"},
        )
        pending = await (await c.get(f"/api/cards/pending?slot={SLOT}", headers=OWNER)).json()
        agent_pending = await c.get("/api/cards/pending", headers=AGENT)
        return card, owner.status, sub.status, pending, agent_pending.status

    card, owner_status, sub_status, pending, agent_pending = _run(world, store, go)
    assert card["slot_key"] == SLOT and card["status"] == "pending"
    assert card["changes"] == [
        {"label": "chat.verbosity", "before": "standard", "after": "ultra-brief"}
    ]
    assert card["delivered_clients"] == 1
    assert "session_key" not in card and "before" not in card
    assert owner_status == 403 and sub_status == 403
    assert [c["id"] for c in pending["cards"]] == [card["id"]]
    assert agent_pending == 403


def test_a_repeated_proposal_tells_the_model_a_card_is_already_waiting(
    world, tmp_path, monkeypatch
):
    """The route answers a repeat with 409 duplicate_card and writes no second row, and
    the propose_change tool turns that answer into one line naming the waiting card,
    so the model says the card is already there instead of failing the turn."""
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        first = await _propose_setting(c)
        again = await c.post(
            "/api/cards/agent/propose",
            json={
                "kind": "setting.change",
                "params": {"path": "chat.verbosity", "value": "ultra-brief"},
            },
            headers=AGENT,
        )
        return first, again.status, await again.json()

    first, status, body = _run(world, store, go)
    assert status == 409 and body["code"] == "duplicate_card"
    assert first["id"] in body["error"]
    assert [c["id"] for c in store.pending(SLOT)] == [first["id"]]
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: (SK, ""))
    monkeypatch.setattr(mcp_guide, "_post", lambda *a, **k: dict(body))
    out = mcp_guide._call_tool_inner(
        "propose_change",
        {"kind": "setting.change", "params": {"path": "chat.verbosity", "value": "ultra-brief"}},
    )
    assert out.startswith(f"Error: the same change is already on card {first['id']}")


def test_an_internal_or_app_caller_can_never_apply_a_card(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        body = card["plan"]["apply"][0]["body"]
        statuses = []
        for auth in ("internal", "app", ""):
            r = await c.patch(
                "/api/config/kirocrew", json=body, headers=_card_headers(card, auth=auth)
            )
            statuses.append(r.status)
        return statuses

    assert _run(world, store, go) == [403, 403, 403]
    assert world.calls == [] and world.config["chat.verbosity"] == "standard"


def test_a_card_step_sent_chunked_applies(world, tmp_path):
    # A tunnel or proxy can re-frame the body without Content-Length; the step
    # must still be read, not refused as "not JSON".
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        raw = json.dumps(card["plan"]["apply"][0]["body"]).encode()

        async def chunks():
            yield raw[:5]
            yield raw[5:]

        r = await c.patch(
            "/api/config/kirocrew",
            data=chunks(),
            headers={**_card_headers(card), "Content-Type": "application/json"},
        )
        return r.status, await r.text()

    status, text = _run(world, store, go)
    assert status == 200, text
    assert world.config["chat.verbosity"] == "ultra-brief"


def test_apply_must_equal_the_plan_and_the_previewed_state(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        wrong = await c.patch(
            "/api/config/kirocrew",
            json={"path": "agent.approval_mode", "value": "auto"},
            headers=_card_headers(card),
        )
        world.config["chat.verbosity"] = "detailed"  # someone changed it meanwhile
        changed = await c.patch(
            "/api/config/kirocrew",
            json=card["plan"]["apply"][0]["body"],
            headers=_card_headers(card),
        )
        return (wrong.status, (await wrong.json())["code"]), (
            changed.status,
            (await changed.json())["code"],
        )

    wrong, changed = _run(world, store, go)
    assert wrong == (409, "plan_mismatch")
    assert changed == (409, "changed_since_preview")
    assert world.calls == []


def test_apply_records_from_the_real_handler_once_then_undo_restores(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        step = card["plan"]["apply"][0]
        first = await c.patch(
            "/api/config/kirocrew", json=step["body"], headers=_card_headers(card)
        )
        again = await c.patch(
            "/api/config/kirocrew", json=step["body"], headers=_card_headers(card)
        )
        applied = store.public(store.get(card["id"]))
        undo_step = applied["plan"]["undo"][0]
        undone = await c.patch(
            "/api/config/kirocrew",
            json=undo_step["body"],
            headers=_card_headers(applied, op="undo"),
        )
        return first.status, await again.json(), applied, undone.status, store.get(card["id"])

    first, again, applied, undone, final = _run(world, store, go)
    assert first == 200 and again["card_replay"] is True
    assert world.calls == [("PATCH", "/api/config/kirocrew")] * 2  # apply once, undo once
    assert applied["status"] == "applied"
    assert applied["plan"]["undo"][0]["body"] == {"path": "chat.verbosity", "value": "standard"}
    assert undone == 200 and final["status"] == "undone"
    assert world.config["chat.verbosity"] == "standard"
    assert any("via change card" in m and "standard→ultra-brief" in m for m in world.memory)


def test_undo_is_refused_when_the_value_changed_after_apply(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        await c.patch(
            "/api/config/kirocrew",
            json=card["plan"]["apply"][0]["body"],
            headers=_card_headers(card),
        )
        world.config["chat.verbosity"] = "detailed"
        applied = store.public(store.get(card["id"]))
        r = await c.patch(
            "/api/config/kirocrew",
            json=applied["plan"]["undo"][0]["body"],
            headers=_card_headers(applied, op="undo"),
        )
        return r.status, (await r.json())["code"]

    assert _run(world, store, go) == (409, "changed_since_apply")
    assert world.config["chat.verbosity"] == "detailed"


def test_a_secret_value_reaches_only_the_vault_route(world, tmp_path):
    path = tmp_path / "c.json"
    store = CardStore(path)
    world.secrets["EXISTING"] = "old"

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "secret.save", "params": {"name": "GH_TOKEN"}},
            headers=AGENT,
        )
        card = await r.json()
        body = dict(card["plan"]["apply"][0]["body"], value=SECRET_VALUE)
        ok = await c.post("/api/secrets", json=body, headers=_card_headers(card))
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "secret.save", "params": {"name": "EXISTING"}},
            headers=AGENT,
        )
        over = await r.json()
        await c.post(
            "/api/secrets", json={"name": "EXISTING", "value": "new"}, headers=_card_headers(over)
        )
        pending = await (await c.get("/api/cards/pending", headers=OWNER)).text()
        return ok.status, pending, store.get(over["id"])

    status, pending, overwrote = _run(world, store, go)
    assert status == 200 and world.secrets["GH_TOKEN"] == SECRET_VALUE
    assert SECRET_VALUE not in path.read_text(encoding="utf-8")
    assert SECRET_VALUE not in pending
    assert all(SECRET_VALUE not in m for m in world.memory)
    assert any("saved secret GH_TOKEN (via change card)" in m for m in world.memory)
    assert overwrote["status"] == "applied" and overwrote["plan"]["undo"] is None
    assert overwrote["undo_unavailable_reason"] == "overwrites_existing"


def test_a_manual_change_leaves_no_memory_line_while_persistence_is_off(
    world, tmp_path, monkeypatch
):
    from kiro_crew.config.loader import KiroCrewConfig

    real_load = KiroCrewConfig.load.__func__

    def _load(cls, *args, **kwargs):
        cfg = real_load(cls, *args, **kwargs)
        cfg.memory.persistence_enabled = False
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_load))
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        r = await c.patch(
            "/api/config/kirocrew", json={"path": "chat.verbosity", "value": "brief"}, headers=OWNER
        )
        return r.status

    assert _run(world, store, go) == 200
    assert not any(m.startswith("Dashboard: ") for m in world.memory)


def test_a_manual_settings_change_becomes_a_memory_event(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        r = await c.patch(
            "/api/config/kirocrew", json={"path": "chat.verbosity", "value": "brief"}, headers=OWNER
        )
        s = await c.post("/api/secrets", json={"name": "K", "value": SECRET_VALUE}, headers=OWNER)
        return r.status, s.status

    assert _run(world, store, go) == (200, 200)
    assert (
        "Dashboard: changed config chat.verbosity standard→brief (via settings page)"
        in world.memory
    )
    assert "Dashboard: saved secret K (via settings page)" in world.memory
    assert all(SECRET_VALUE not in m for m in world.memory)


def test_preview_edits_bump_the_revision_and_cancel_closes(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        bad = await c.post(
            f"/api/cards/{card['id']}/preview",
            json={"revision": 1, "params": {"path": "agent.sandbox", "value": "off"}},
            headers=OWNER,
        )
        ok = await c.post(
            f"/api/cards/{card['id']}/preview",
            json={"revision": 1, "params": {"path": "chat.verbosity", "value": "brief"}},
            headers=OWNER,
        )
        revised = await ok.json()
        stale = await c.patch(
            "/api/config/kirocrew",
            json=revised["plan"]["apply"][0]["body"],
            headers=_card_headers(card),
        )
        cancel = await c.post(
            f"/api/cards/{card['id']}/cancel", json={"revision": 2}, headers=OWNER
        )
        return (await bad.json())["code"], revised, stale.status, await cancel.json()

    bad, revised, stale, cancelled = _run(world, store, go)
    assert bad == "field_not_editable"
    assert revised["revision"] == 2 and revised["params"]["value"] == "brief"
    assert stale == 409
    assert cancelled["status"] == "cancelled"
    assert world.calls == []


# ── wiring ──


def test_server_route_table_matches_the_card_module():
    app = web.Application()
    routes.register_change_card_routes(app)
    mine = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    text = (REPO / "src/kiro_crew/dashboard/server_runtime/mcp_routes.py").read_text(
        encoding="utf-8"
    )
    server = set(
        re.findall(
            r'\("(GET|POST)", "(/api/(?:cards|members)/[a-z0-9_{}/]+)", "api_(?:cards|members)_[a-z_]+"\)',
            text,
        )
    )
    assert server == mine


def test_only_the_agent_half_is_strict_internal():
    from kiro_crew.dashboard import server

    strict = server._STRICT_INTERNAL_API_PATHS
    assert "/api/cards/agent" in strict
    for p in ("/api/cards/pending", "/api/cards/cc_x/preview", "/api/cards"):
        assert not any(p == s or p.startswith(s + "/") for s in strict)


def test_shim_routes_card_tools_with_the_verified_key(monkeypatch):
    sent: list[tuple[str, str, Any]] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: (SK, ""))
    monkeypatch.setattr(
        mcp_guide,
        "_post",
        lambda path, body, session_key: sent.append((path, session_key, body)) or {},
    )
    monkeypatch.setattr(
        mcp_guide, "_get", lambda path, session_key: sent.append((path, session_key, None)) or {}
    )
    mcp_guide._call_tool_inner("list_change_kinds", {})
    mcp_guide._call_tool_inner("propose_change", {"kind": "secret.save", "params": {"name": "A"}})
    mcp_guide._call_tool_inner("get_change_status", {"change_id": "cc_abcdefgh"})
    assert sent == [
        ("/api/cards/agent/kinds", SK, None),
        ("/api/cards/agent/propose", SK, {"kind": "secret.save", "params": {"name": "A"}}),
        ("/api/cards/agent/status?card_id=cc_abcdefgh", SK, None),
    ]
    blob = json.dumps([t["inputSchema"] for t in mcp_guide._list_tools()]).lower()
    assert "slot" not in blob and "session" not in blob


def test_every_agent_pre_approves_the_card_tools_through_the_ceiling():
    from kiro_crew import agent

    cards_grants = {
        "@kirocrew-guide/list_change_kinds",
        "@kirocrew-guide/find_setting",
        "@kirocrew-guide/get_member_capabilities",
        "@kirocrew-guide/diagnose_settings",
        "@kirocrew-guide/propose_change",
        "@kirocrew-guide/get_change_status",
    }
    assert cards_grants <= set(agent._GUIDE_AUTO_GRANTS)
    assert cards_grants <= set(agent.build_agent_config()["allowedTools"])


def test_setting_id_resolves_to_the_dashboard_store_for_verbosity():
    target = cards.resolve_setting({"setting_id": "chat.response-verbosity"})
    assert target == {
        "store": "dashboard",
        "key": "verbosity",
        "label": "Response Verbosity",
        "allowed": ["default", "concise", "ultra", "answer_only"],
    }
    p = catalog.validate_params(
        "setting.change", {"setting_id": "chat.response-verbosity", "value": "ultra"}
    )
    before = {"store": "dashboard", "key": "verbosity", "label": "Response Verbosity"}
    before["value"] = "default"
    preview = catalog.build_preview("setting.change", p, before, {})
    assert preview["apply"] == [
        {"method": "PUT", "path": "/api/dashboard/config", "body": {"verbosity": "ultra"}}
    ]
    assert preview["changes"] == [
        {"label": "Response Verbosity", "before": "default", "after": "ultra"}
    ]
    assert preview["risk"] == "normal"
    undo, _ = catalog.build_undo("setting.change", p, before, [{}], 1)
    assert undo == [
        {"method": "PUT", "path": "/api/dashboard/config", "body": {"verbosity": "default"}}
    ]


def test_setting_id_with_an_editable_config_key_uses_the_config_route():
    target = cards.resolve_setting({"setting_id": "chat.auto-compact-threshold"})
    assert target["store"] == "kirocrew" and target["key"] == "session.autocompact_pct"
    target = cards.resolve_setting({"setting_id": "security.how-long-auto-approve-stays-on"})
    assert target["key"] == "agent.yolo_duration" and "24h" in target["allowed"]
    p = {"setting_id": "security.how-long-auto-approve-stays-on", "value": "24h"}
    before = {"store": "kirocrew", "key": "agent.yolo_duration", "label": "x", "value": "1h"}
    preview = catalog.build_preview("setting.change", p, before, {})
    assert preview["apply"][0]["body"] == {"path": "agent.yolo_duration", "value": "24h"}
    assert preview["risk"] == "widen"
    # The raw path form keeps working.
    assert cards.resolve_setting({"path": "agent.approval_mode"})["store"] == "kirocrew"
    assert cards.resolve_setting({"path": "dashboard.verbosity"})["key"] == "verbosity"


def test_a_setting_with_no_provable_write_path_is_refused():
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.resolve_setting({"setting_id": "chat.message-font-size"})
    assert exc.value.code == "no_write_path" and "settings.show" in exc.value.message
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.resolve_setting({"setting_id": "chat.no-such-thing"})
    assert exc.value.code == "unknown_setting"
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params("setting.change", {"setting_id": "a", "path": "a.b", "value": 1})


def test_value_is_validated_against_the_controls_options():
    target = cards.resolve_setting({"setting_id": "chat.response-verbosity"})
    cards.check_target_value(target, "answer_only")
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.check_target_value(target, "shorter")
    assert exc.value.code == "invalid_value"
    flag = cards.resolve_setting({"setting_id": "chat.quick-send"})
    cards.check_target_value(flag, True)
    with pytest.raises(catalog.CardCatalogError):
        cards.check_target_value(flag, 1)
    window = cards.resolve_setting({"setting_id": "chat.restore-window"})
    with pytest.raises(catalog.CardCatalogError):
        cards.check_target_value(window, True)


def test_every_dashboard_setting_is_a_registry_id_the_route_accepts():
    ids = {e["id"] for e in cards.settings_registry()}
    route = (REPO / "src/kiro_crew/dashboard/file_api/dashboard_config.py").read_text(
        encoding="utf-8"
    )
    accepted = re.search(r"_allowed = \{([^}]*)\}", route).group(1)
    for sid, (key, _values) in catalog.DASHBOARD_SETTINGS.items():
        assert sid in ids, sid
        assert f'"{key}"' in accepted, key


def test_find_setting_returns_writable_matches_and_withholds_credential_values(monkeypatch):
    monkeypatch.setattr(cards, "read_setting_value", lambda target: f"<{target['key']}>")
    rows = cards.find_settings("shorter replies verbosity")
    assert rows and rows[0]["setting_id"] == "chat.response-verbosity"
    top = rows[0]
    assert top["writable"] is True and top["current_value"] == "<verbosity>"
    assert top["allowed_values"] == ["default", "concise", "ultra", "answer_only"]
    assert set(top) >= {"setting_id", "label", "description", "tab", "writable", "current_value"}
    assert len(cards.find_settings("chat")) <= 10
    teams = cards.find_settings("tenant id teams")
    assert all(r["current_value"] is None for r in teams if "tenant" in r["setting_id"])
    assert cards.find_settings("  ") == []


def test_find_setting_keeps_tip_text_apart_from_row_text(monkeypatch):
    """A row's tip prose comes back as `hint`, never inside `description`, so
    the agent quotes only what the row shows as on-row text."""
    entry = {
        "id": "chat.example",
        "label": "Example",
        "tab": "chat",
        "hint": "Tip words zebrafinch.",
        "description": "Row words.",
    }
    tip_only = {
        "id": "chat.tiponly",
        "label": "Tip only",
        "tab": "chat",
        "hint": "Only zebrafinch.",
    }
    monkeypatch.setattr(cards, "settings_registry", lambda: (entry, tip_only))
    monkeypatch.setattr(cards, "resolve_setting", lambda p: None)
    rows = {r["setting_id"]: r for r in cards.find_settings("zebrafinch")}
    assert rows["chat.example"]["description"] == "Row words."
    assert rows["chat.example"]["hint"] == "Tip words zebrafinch."
    assert rows["chat.tiponly"]["description"] == ""
    assert rows["chat.tiponly"]["hint"] == "Only zebrafinch."


def test_find_setting_flags_only_settings_that_wait_for_a_restart(monkeypatch):
    # Most settings apply live; the schema's restart mark is what lets Mate
    # say "restart" for the few that need it and stay quiet for the rest.
    monkeypatch.setattr(cards, "read_setting_value", lambda target: None)
    remote = next(r for r in cards.find_settings("remote crew management") if r["writable"])
    assert remote["setting_id"] == "instances.enable-remote-crew-management"
    assert remote["restart_required"] is True
    verbosity = cards.find_settings("shorter replies verbosity")[0]
    assert "restart_required" not in verbosity


def test_dashboard_apply_and_undo_through_the_hook(world, tmp_path, monkeypatch):
    world.dash = {"verbosity": "default"}
    real_read_state = cards.read_state

    async def read_state(kind, params, evidence, *, state, app):
        if kind == "setting.change" and "setting_id" in params:
            return {
                "store": "dashboard",
                "key": "verbosity",
                "label": "Response Verbosity",
                "value": world.dash["verbosity"],
            }
        return await real_read_state(kind, params, evidence, state=state, app=app)

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(
        routes,
        "_config_before",
        lambda route, body: (
            {k: world.dash.get(k) for k in body or {}}
            if route == ("PUT", "/api/dashboard/config")
            else None
        ),
    )
    store = CardStore(tmp_path / "c.json")

    async def put_dash(request):
        body = await request.json()
        world.calls.append(("PUT", "/api/dashboard/config"))
        world.dash.update(body)
        return web.json_response({"ok": True})

    async def main():
        app = _app(world, store)
        app.router.add_put("/api/dashboard/config", put_dash)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post(
                "/api/cards/agent/propose",
                json={
                    "kind": "setting.change",
                    "params": {"setting_id": "chat.response-verbosity", "value": "ultra"},
                },
                headers=AGENT,
            )
            card = await r.json()
            step = card["plan"]["apply"][0]
            r1 = await client.put(step["path"], json=step["body"], headers=_card_headers(card))
            applied = store.public(store.get(card["id"]))
            undo = applied["plan"]["undo"][0]
            r2 = await client.put(
                undo["path"], json=undo["body"], headers=_card_headers(applied, op="undo")
            )
            await asyncio.gather(*list(routes._BACKGROUND))
            return card, r1.status, applied, r2.status
        finally:
            await client.close()

    card, s1, applied, s2 = asyncio.run(main())
    assert card["plan"]["apply"][0]["body"] == {"verbosity": "ultra"}
    assert s1 == 200 and applied["status"] == "applied" and s2 == 200
    assert world.dash["verbosity"] == "default"
    assert store.get(card["id"])["status"] == "undone"
    assert any("verbosity default→ultra (via change card)" in m for m in world.memory)


# ── the hook leaves the body readable for every reader the real routes use ──


def test_hook_replays_the_body_to_the_capped_reader_and_request_json(world, tmp_path):
    """A REAL aiohttp app: the handlers read through ``read_bounded_json`` and ``json()``."""
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    store = CardStore(tmp_path / "c.json")
    seen: list[Any] = []

    async def capped_patch(request):
        body, err = await read_bounded_json(request, max_bytes=4096)
        if err is not None:
            return err
        seen.append(("capped", body))
        world.config[body["path"]] = body["value"]
        return web.json_response({"ok": True})

    async def json_put(request):
        seen.append(("json", await request.json()))
        return web.json_response({"ok": True})

    async def main():
        app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
        app["state"] = FakeState(store)
        routes.register_change_card_routes(app)
        app.router.add_patch("/api/config/kirocrew", capped_patch)
        app.router.add_put("/api/dashboard/config", json_put)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            manual = await client.patch(
                "/api/config/kirocrew",
                json={"path": "chat.verbosity", "value": "brief"},
                headers=OWNER,
            )
            card = await _propose_setting(client, value="ultra-brief")
            carded = await client.patch(
                "/api/config/kirocrew",
                json=card["plan"]["apply"][0]["body"],
                headers=_card_headers(card),
            )
            plain = await client.put(
                "/api/dashboard/config", json={"verbosity": "ultra"}, headers=OWNER
            )
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return manual.status, carded.status, plain.status, store.get(card["id"])["status"]
        finally:
            await client.close()

    world.config["chat.verbosity"] = "standard"
    manual, carded, plain, status = asyncio.run(main())
    assert (manual, carded, plain) == (200, 200, 200)
    assert seen == [
        ("capped", {"path": "chat.verbosity", "value": "brief"}),
        ("capped", {"path": "chat.verbosity", "value": "ultra-brief"}),
        ("json", {"verbosity": "ultra"}),
    ]
    assert status == "applied" and world.config["chat.verbosity"] == "ultra-brief"


def test_names_follow_the_routes_own_rules_not_ascii():
    p = catalog.validate_params(
        "crewmate.create", {"name": "PR跟进", "goal": "看等待 review 的 PR"}
    )
    assert p["name"] == "PR跟进"
    assert catalog.validate_params(
        "crewmate.update", {"name": "PR跟进", "fields": {"description": "x"}}
    )
    assert (
        catalog.validate_params("secret.save", {"name": "github token"})["name"] == "github token"
    )
    for bad in ("..", "a\tb"):
        with pytest.raises(catalog.CardCatalogError):
            catalog.validate_params("crewmate.create", {"name": bad, "goal": "g"})
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params("template.update", {"template": "../x", "fields": {"model": "a"}})


# ── member capabilities ──


@pytest.fixture
def assistant_editor(tmp_path, monkeypatch):
    """A real CapabilityService over a temp home with one ordinary crewmate."""
    from kiro_crew import agent, agent_state
    from kiro_crew.config import loader
    from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

    home = tmp_path / "home"
    specs = home / "agents"
    specs.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / "kiro"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr(agent_state, "_state_path", lambda: home / "agent_model_state.json")
    monkeypatch.setattr("kiro_crew.agent_capabilities.default_project_dir", lambda _: "")
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    loader._invalidate_config_cache()
    row = {"kiro_agent": "kirocrew", "workspace": "default", "memory_store": "default"}
    (home / "config.json").write_text(json.dumps({"agents": {"mate": row}}), encoding="utf-8")
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: specs)
    # A default that grants nothing, so the crewmate starts without fs_read approved.
    (specs / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "tools": ["fs_read"], "allowedTools": []}),
        encoding="utf-8",
    )
    template = json.loads((specs / "kirocrew.json").read_text(encoding="utf-8"))
    from kiro_crew.agent_capabilities import CapabilityService

    app: dict[str, Any] = {}
    from kiro_crew.dashboard.handlers import agent_capabilities as handler

    app[handler._SERVICE] = CapabilityService()
    return app, home, specs, template


def test_capabilities_view_explains_both_approval_grammars(assistant_editor):
    app, *_ = assistant_editor
    view = cards.member_capabilities_view(app, "mate")
    assert view["template"] == "kirocrew"
    assert {"section": "tools", "id": "fs_read"}.items() <= next(
        r for r in view["rows"] if r["id"] == "fs_read"
    ).items()
    assert view["how_to"]["builtin_tool_approval"]["section"] == "allowedTools"
    params = {
        "member": "mate",
        "draft": {
            "operations": [
                {"section": "autoApprove", "id": "fs_read", "action": "set", "value": True}
            ]
        },
    }
    hint = cards.capability_ref_hint(app, params)
    assert "use section allowedTools id fs_read" in hint


def test_an_approval_the_ceiling_withholds_is_refused_plainly(assistant_editor):
    app, *_ = assistant_editor
    params = catalog.validate_params(
        "crewmate.capabilities",
        {
            "member": "mate",
            "draft": {
                "enroll": True,
                "operations": [
                    {"section": "allowedTools", "id": "fs_read", "action": "set", "value": True}
                ],
            },
        },
    )
    with pytest.raises(catalog.CardCatalogError) as exc:
        asyncio.run(cards.build("crewmate.capabilities", params, state=None, app=app))
    assert exc.value.code == "approval_withheld_by_policy"


def test_crewmate_delete_memory_line_uses_the_display_name():
    text = catalog.describe_manual_change(
        ("DELETE", "/api/agents/{name}"), {"name": "pr"}, None, "PR跟进"
    )
    assert text == "deleted mate “PR跟进”"
    assert (
        catalog.describe_manual_change(("DELETE", "/api/agents/{name}"), {"name": "pr"}, None, None)
        == "deleted mate “pr”"
    )


def test_shim_routes_get_member_capabilities(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: (SK, ""))
    monkeypatch.setattr(
        mcp_guide, "_get", lambda path, session_key: sent.append(path) or {"rows": []}
    )
    json.loads(mcp_guide._call_tool_inner("get_member_capabilities", {"member": "mate"}))
    assert sent == ["/api/cards/agent/capabilities?member=mate"]


# ── manual saves the hook forwards, preview shapes and the card read routes ──


@pytest.mark.parametrize("chunked", [False, True])
def test_a_manual_save_the_hook_cannot_capture_reaches_the_route_unrecorded(
    world, tmp_path, chunked
):
    # The hook never refuses an ordinary save: the route answers it, and no
    # memory line is written for a body the hook did not read.
    store = CardStore(tmp_path / "c.json")

    async def go(client):
        async def oversized():
            for _ in range(routes._BODY_MAX // (2**16) + 1):
                yield b"x" * (2**16)
            yield b'{"path":"chat.verbosity","value":"ultra-brief"}'

        response = await client.patch(
            "/api/config/kirocrew",
            data=oversized() if chunked else b"not-json",
            headers=OWNER,
        )
        assert "invalid_body" not in await response.text()

    _run(world, store, go)
    assert world.memory == []
    assert world.config["chat.verbosity"] == "standard"


@pytest.mark.parametrize(
    "kind, params, before, context, method, path",
    [
        (
            "schedule.update",
            {
                "id": "job-1",
                "fields": {
                    "name": "Morning",
                    "message": "Review",
                    "cron_expr": "0 9 * * *",
                    "timezone": "UTC",
                },
            },
            {"exists": True, "fields": {"name": "Evening"}},
            {},
            "PATCH",
            "/api/crons/job-1",
        ),
        (
            "crewmate.update",
            {"name": "Radar", "fields": {"description": "Watch builds"}},
            {"exists": True, "fields": {}},
            {},
            "PUT",
            "/api/agents/Radar",
        ),
        (
            "template.update",
            {
                "template": "helper",
                "fields": {
                    "tools": ["read"],
                    "allowedTools": ["read"],
                    "skills": ["review"],
                    "model": "auto",
                },
            },
            {"exists": True, "fields": {}},
            {"members": ["Radar"]},
            "PATCH",
            "/api/agents/detail/helper",
        ),
        (
            "mcp.toggle",
            {"name": "files", "enabled": False},
            {"exists": True, "enabled": True},
            {},
            "POST",
            "/api/mcp/toggle",
        ),
        (
            "mcp.toggle",
            {"server": "files", "tool": "read", "enabled": True},
            {"exists": True, "enabled": False},
            {},
            "POST",
            "/api/mcp/toggle-tool",
        ),
        (
            "trust.app",
            {"name": "clock", "repository": "example/clock"},
            {"trusted": False},
            {},
            "POST",
            "/api/security/trusted-apps/clock",
        ),
        (
            "denied_command",
            {"action": "add", "pattern": "danger", "note": "avoid"},
            {"exists": False},
            {},
            "POST",
            "/api/security/denied-commands/user",
        ),
        (
            "denied_command",
            {"action": "toggle", "id": "rule-1", "enabled": False},
            {"exists": True, "enabled": True, "pattern": "danger"},
            {},
            "PATCH",
            "/api/security/denied-commands/user/rule-1",
        ),
    ],
    ids=["schedule", "member", "template", "server", "tool", "trust", "rule-add", "rule-toggle"],
)
def test_preview_shapes_preserve_exact_native_route(kind, params, before, context, method, path):
    normalized = catalog.validate_params(kind, params)
    preview = catalog.build_preview(kind, normalized, before, context)
    assert preview["apply"][0]["method"] == method
    assert preview["apply"][0]["path"] == path
    assert preview["changes"]
    assert preview["title"]


@pytest.mark.parametrize("route", sorted(catalog.MEMORY_ROUTES))
def test_each_manual_route_records_names_not_secret_values(route):
    body = {
        "name": "Example",
        "slug": "provider",
        "id": "item-1",
        "provider": "registry",
        "server": "files",
        "tool": "read",
        "enabled": True,
        "pattern": "danger",
        "value": SECRET_VALUE,
        "servers": {"files": {"env": {"KEY": SECRET_VALUE}}},
        "changes": [{"name": "files", "uninstall": True}],
        "operations": [{"action": "remove", "section": "tools", "id": "read"}],
    }
    if route == ("PATCH", "/api/config/kirocrew"):
        body = {"path": "chat.verbosity", "value": "standard"}
    elif route == ("PUT", "/api/dashboard/config"):
        body = {"show_tool_calls": True}
    text = catalog.describe_manual_change(
        route, {"name": "Example", "id": "item-1", "job_id": "job-1"}, body
    )
    assert text
    assert SECRET_VALUE not in text


def test_card_read_routes_validate_queries_and_keep_service_failures_visible(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        cards, "member_capabilities_view", lambda app, member: {"member": member, "rows": []}
    )
    monkeypatch.setattr(cards, "diagnose_settings", lambda *args, **kwargs: {"findings": []})
    monkeypatch.setattr(cards, "find_settings", lambda query: [{"id": "chat.verbosity"}])
    monkeypatch.setattr(cards, "localize_setting_rows", lambda rows, lang: rows)

    async def go(client):
        for path, status in [
            ("/api/cards/agent/kinds", 200),
            ("/api/cards/agent/settings", 400),
            ("/api/cards/agent/settings?q=verbosity", 200),
            ("/api/cards/agent/capabilities", 400),
            ("/api/cards/agent/capabilities?member=Radar", 200),
            ("/api/cards/agent/diagnose?topic=" + "x" * 201, 400),
            ("/api/cards/agent/diagnose?topic=models", 200),
        ]:
            response = await client.get(path, headers=AGENT)
            assert response.status == status, await response.text()
        for path in ("kinds", "settings?q=x", "capabilities?member=x", "diagnose"):
            response = await client.get("/api/cards/agent/" + path, headers=OWNER)
            assert response.status == 403
        response = await client.get("/api/cards/pending?slot=" + "x" * 600, headers=OWNER)
        assert response.status == 400

    _run(world, CardStore(tmp_path / "c.json"), go)


def test_card_read_routes_report_failed_services(world, tmp_path, monkeypatch):
    def fail_capabilities(*args):
        raise catalog.CardCatalogError("not_found", "missing member")

    def fail_diagnosis(*args, **kwargs):
        raise OSError("not readable")

    monkeypatch.setattr(cards, "member_capabilities_view", fail_capabilities)
    monkeypatch.setattr(cards, "diagnose_settings", fail_diagnosis)

    async def go(client):
        response = await client.get("/api/cards/agent/capabilities?member=Radar", headers=AGENT)
        assert (await response.json())["code"] == "not_found"
        response = await client.get("/api/cards/agent/diagnose", headers=AGENT)
        assert response.status == 500
        assert (await response.json())["code"] == "diagnose_failed"

    _run(world, CardStore(tmp_path / "c.json"), go)


def test_an_unknown_length_manual_save_is_forwarded_whole(world, monkeypatch):
    class Payload:
        def __init__(self):
            self.chunks = iter([b"x" * (2**16)] * 17 + [b'{"name":"files"}'])

        async def iter_chunked(self, size):
            for chunk in self.chunks:
                yield chunk

    class Request(dict):
        headers = {}
        can_read_body = True
        content_length = None
        match_info = {}
        _payload = Payload()

    monkeypatch.setattr(routes, "route_key", lambda request: ("POST", "/api/mcp/custom"))
    monkeypatch.setattr(routes, "_is_owner_browser", lambda request: True)
    reached = []

    async def handler(request):
        raw = b"".join([part async for part in request._payload.iter_chunked(2**16)])
        reached.append(len(raw))
        return web.json_response({"ok": True})

    response = asyncio.run(routes.change_card_middleware(Request(), handler))
    # Unknown length: the hook reads nothing and the route gets every byte.
    assert response.status == 200
    assert reached == [17 * 2**16 + len(b'{"name":"files"}')]

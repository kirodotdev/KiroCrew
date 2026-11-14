"""Per-tool "Always allow": the approval card's decision and the MCP tool list's routes.

Both write the entry ``@server/tool`` to ``hooks.auto_approve_tool_identities``
(``dashboard.always_allow_tool``), never to ``hooks.auto_approve_tools``. The card grants only the ref its own pending
row carries; the routes refuse a Kiro Crew server, a wildcard and a tool an
operator ``confirm_tools`` pattern covers.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import always_allow_tool
from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.handlers.tool_grants import (
    api_tool_grant_add,
    api_tool_grant_remove,
    api_tool_grants_list,
)
from kiro_crew.dashboard.state import SlotOrigin

REF = "@notes/lookup"


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def no_refusal(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: False
    )
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _dashboard_user(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _app_caller(request: web.Request, handler):
    request["app"] = "some-app"
    return await handler(request)


def _app(state, middleware=_dashboard_user) -> web.Application:
    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_get("/api/security/tool-grants", api_tool_grants_list)
    app.router.add_post("/api/security/tool-grants", api_tool_grant_add)
    app.router.add_post("/api/security/tool-grants/remove", api_tool_grant_remove)
    return app


def _pending(state, request_id: str, meta: dict):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", REF, json.dumps({"request_id": request_id, **meta}))
    slot.register_approval(request_id, fut, row)
    return slot, fut


def _decide(slot, request_id: str, **extra) -> dict:
    return {
        "action": "always_allow_tool",
        "request_id": request_id,
        "origin": "native",
        "request_mid": slot.approval_instance(request_id),
        **extra,
    }


def _stored(tmp_path, key: str = "auto_approve_tool_identities") -> list:
    path = tmp_path / "config.json"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("hooks", {}).get(key, [])


def _legacy(tmp_path) -> list:
    """The hand-written ``auto_approve_tools`` list, which these surfaces never write."""
    return _stored(tmp_path, "auto_approve_tools")


GRANTABLE = {
    always_allow_tool.FIELD_GRANTABLE: "1",
    always_allow_tool.FIELD_REF: REF,
}


# -- the card's decision ----------------------------------------------------


@pytest.mark.asyncio
async def test_card_grant_writes_the_cards_ref_and_approves(state, tmp_path, no_refusal):
    slot, fut = _pending(state, "r1", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json=_decide(slot, "r1", pattern=REF)
        )
    assert resp.status == 200
    assert fut.result() == "approved"
    assert _stored(tmp_path) == [REF]
    assert _legacy(tmp_path) == []


@pytest.mark.asyncio
async def test_card_grant_refuses_a_client_pattern_naming_another_tool(state, tmp_path, no_refusal):
    slot, fut = _pending(state, "r2", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json=_decide(slot, "r2", pattern="@notes/delete_all"),
        )
        body = await resp.json()
    assert resp.status == 409 and body["code"] == "approval_superseded"
    assert not fut.done()
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_card_without_the_servers_proof_grants_nothing(state, tmp_path, no_refusal):
    slot, fut = _pending(state, "r3", {always_allow_tool.FIELD_REF: REF})
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json=_decide(slot, "r3", pattern=REF)
        )
    assert resp.status == 400
    assert not fut.done()
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_card_grant_needs_the_native_target(state, tmp_path, no_refusal):
    _slot, fut = _pending(state, "r4", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": "always_allow_tool", "request_id": "r4", "pattern": REF},
        )
    assert resp.status == 403
    assert not fut.done()
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_card_grant_refused_when_confirm_tools_now_covers_it(state, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: True
    )
    slot, fut = _pending(state, "r5", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json=_decide(slot, "r5", pattern=REF)
        )
        body = await resp.json()
    assert resp.status == 409 and body["code"] == "asks_every_time"
    assert not fut.done()
    assert _stored(tmp_path) == []


# -- what the card offers ---------------------------------------------------


def _event(**kw):
    base = dict(
        mcp_identity_trusted=True,
        mcp_server_name="notes",
        tool_name="lookup",
        is_shell=False,
        mcp_identity_unreadable=False,
        connection_mcp_identity=None,
        child_low_fidelity=False,
        child_mcp_identity_trusted=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_card_offers_a_verified_identity(no_refusal):
    assert always_allow_tool.card_fields(_event()) == GRANTABLE


@pytest.mark.parametrize(
    "kw",
    [
        {"mcp_identity_trusted": False},
        {"is_shell": True},
        {"mcp_identity_unreadable": True},
        {"connection_mcp_identity": ("notes", "lookup")},
        {"child_low_fidelity": True},
        {"mcp_server_name": "kirocrew-core"},
        {"tool_name": "look*"},
    ],
)
def test_card_offers_nothing_without_a_grantable_identity(no_refusal, kw):
    assert always_allow_tool.card_fields(_event(**kw)) == {}


def test_card_names_the_confirm_tools_refusal(monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: True
    )
    assert always_allow_tool.card_fields(_event()) == {
        always_allow_tool.FIELD_REFUSAL: always_allow_tool.REFUSAL_ASKS_EVERY_TIME
    }


def test_card_names_the_local_overlay_refusal(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: False
    )
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: True)
    assert always_allow_tool.card_fields(_event()) == {
        always_allow_tool.FIELD_REFUSAL: always_allow_tool.REFUSAL_SET_ELSEWHERE
    }


# -- the tool list's routes ------------------------------------------------


@pytest.mark.asyncio
async def test_tool_list_adds_lists_and_removes_one_grant(state, tmp_path, no_refusal):
    async with TestClient(TestServer(_app(state))) as client:
        added = await client.post("/api/security/tool-grants", json={"ref": REF})
        assert added.status == 200
        listed = await (await client.get("/api/security/tool-grants?server=notes")).json()
        assert listed == {"grants": [{"ref": REF}], "server": "notes", "refusal": ""}
        assert _stored(tmp_path) == [REF] and _legacy(tmp_path) == []
        removed = await client.post("/api/security/tool-grants/remove", json={"ref": REF})
        assert (await removed.json())["grants"] == []
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_tool_list_leaves_hand_written_auto_approve_tools_alone(state, tmp_path, no_refusal):
    """A hand-written ``auto_approve_tools`` entry is not a toggle grant: not listed, not revoked."""
    (tmp_path / "config.json").write_text(
        json.dumps({"hooks": {"auto_approve_tools": [REF]}}), encoding="utf-8"
    )
    async with TestClient(TestServer(_app(state))) as client:
        listed = await (await client.get("/api/security/tool-grants?server=notes")).json()
        assert listed["grants"] == []
        await client.post("/api/security/tool-grants", json={"ref": REF})
        removed = await client.post("/api/security/tool-grants/remove", json={"ref": REF})
        assert (await removed.json())["grants"] == []
    assert _legacy(tmp_path) == [REF]
    assert _stored(tmp_path) == []


@pytest.mark.parametrize("ref", ["@notes/*", "@notes", "notes/lookup", "@a/b/c"])
@pytest.mark.asyncio
async def test_tool_list_refuses_a_ref_that_is_not_one_tool(state, tmp_path, no_refusal, ref):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/security/tool-grants", json={"ref": ref})
    assert resp.status == 400
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_tool_list_refuses_a_kiro_crew_server(state, tmp_path, no_refusal):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/security/tool-grants", json={"ref": "@kirocrew-core/spawn_run"}
        )
        code = (await resp.json())["code"]
        listed = await (await client.get("/api/security/tool-grants?server=kirocrew-core")).json()
    assert resp.status == 409 and code == "crew_owned"
    assert listed["refusal"] == "crew_owned"
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_tool_list_writes_are_owner_only(state, tmp_path, no_refusal):
    async with TestClient(TestServer(_app(state, _app_caller))) as client:
        resp = await client.post("/api/security/tool-grants", json={"ref": REF})
    assert resp.status == 403
    assert _stored(tmp_path) == []


@pytest.mark.parametrize("requires_confirmation", [False, True])
@pytest.mark.asyncio
async def test_tool_list_cannot_revoke_an_overlay_grant(
    state, tmp_path, monkeypatch, requires_confirmation
):
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation",
        lambda r: requires_confirmation,
    )
    base = tmp_path / "config.json"
    overlay = tmp_path / "config.local.json"
    payload = json.dumps({"hooks": {"auto_approve_tool_identities": [REF]}})
    base.write_text(payload, encoding="utf-8")
    overlay.write_text(payload, encoding="utf-8")
    save = AsyncMock(return_value=None)
    monkeypatch.setattr(always_allow_tool, "save_grant", save)
    async with TestClient(TestServer(_app(state))) as client:
        listed = await (await client.get("/api/security/tool-grants?server=notes")).json()
        assert listed["refusal"] == "set_elsewhere"
        assert listed["grants"] == [{"ref": REF}]
        # Loading config may migrate the base; only the removal must leave it unchanged.
        before_remove = base.read_text(encoding="utf-8")
        resp = await client.post("/api/security/tool-grants/remove", json={"ref": REF})
        assert resp.status == 409
        assert (await resp.json())["code"] == "set_elsewhere"
    save.assert_not_called()
    assert base.read_text(encoding="utf-8") == before_remove
    assert overlay.read_text(encoding="utf-8") == payload


@pytest.mark.asyncio
async def test_tool_list_can_revoke_a_base_grant_when_confirmation_is_required(
    state, tmp_path, monkeypatch
):
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: True
    )
    always_allow_tool.write_grant(REF, add=True)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/security/tool-grants/remove", json={"ref": REF})
        assert resp.status == 200
        assert (await resp.json())["grants"] == []
    assert _stored(tmp_path) == []


@pytest.mark.parametrize("stored", ["@notes/LOOKUP", "@NOTES/Lookup"])
def test_revoke_removes_every_case_variant_but_preserves_patterns(tmp_path, stored):
    from kiro_crew.trust_patterns import _tool_matches

    wider = ["@notes/*", "@notes/look?p", "@notes/[l]ookup", "@other/lookup"]
    entries = [stored, REF, *wider, 42]
    (tmp_path / "config.json").write_text(
        json.dumps({"hooks": {"auto_approve_tool_identities": entries}}), encoding="utf-8"
    )
    assert _tool_matches(stored, REF)
    always_allow_tool.write_grant(REF, add=True)
    assert _stored(tmp_path) == entries
    always_allow_tool.write_grant(REF, add=False)
    assert _stored(tmp_path) == [*wider, 42]
    always_allow_tool.write_grant(REF, add=False)
    assert _stored(tmp_path) == [*wider, 42]


def test_grant_comparison_uses_lower_not_unicode_casefold(tmp_path):
    from kiro_crew.trust_patterns import _tool_matches

    stored, distinct = "@notes/straße", "@notes/STRASSE"
    always_allow_tool.write_grant(stored, add=True)
    assert not _tool_matches(stored, distinct)
    always_allow_tool.write_grant(distinct, add=True)
    assert _stored(tmp_path) == [stored, distinct]
    always_allow_tool.write_grant(distinct, add=False)
    assert _stored(tmp_path) == [stored]


# -- every refusal is audited ------------------------------------------------


@pytest.fixture
def sel_records(monkeypatch):
    records: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            records.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    return records


def _denied(records: list[dict]) -> list[tuple[str, str]]:
    return [(r["operation"], r["error"]) for r in records if r["outcome"] == "denied"]


CARD_OP = "tool_approval:" + always_allow_tool.ACTION


@pytest.mark.parametrize(
    ("meta", "extra", "confirm", "code"),
    [
        (GRANTABLE, {"origin": None, "request_mid": None}, False, "always_allow_not_here"),
        ({always_allow_tool.FIELD_REF: REF}, {"pattern": REF}, False, "pattern_underivable"),
        (GRANTABLE, {"pattern": "@notes/delete_all"}, False, "approval_superseded"),
        (GRANTABLE, {"pattern": REF}, True, "asks_every_time"),
    ],
)
@pytest.mark.asyncio
async def test_each_card_refusal_writes_one_denied_record(
    state, tmp_path, monkeypatch, sel_records, meta, extra, confirm, code
):
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: confirm
    )
    slot, fut = _pending(state, "a1", meta)
    payload = {k: v for k, v in _decide(slot, "a1", **extra).items() if v is not None}
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots/selected/approve", json=payload)
        body = await resp.json()
    assert body["code"] == code
    assert not fut.done()
    assert _denied(sel_records) == [(CARD_OP, code)]


@pytest.mark.asyncio
async def test_card_refusal_for_a_settled_approval_is_audited(monkeypatch, sel_records):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
        AsyncMock(return_value=None),
    )
    fut = asyncio.get_running_loop().create_future()
    fut.set_result("approved")
    resp = await always_allow_tool.grant_from_card(
        {"user": "local-app", "app": ""},
        fut=fut,
        card=lambda field: "",
        body={},
        from_app=False,
        delegated=False,
        strict_native=True,
    )
    assert resp.status == 404
    assert _denied(sel_records) == [(CARD_OP, "approval_not_pending")]


@pytest.mark.parametrize(
    ("path", "ref", "overlay", "op", "code"),
    [
        ("/api/security/tool-grants", "@notes/*", False, "add", "invalid_ref"),
        ("/api/security/tool-grants", "@kirocrew-core/spawn_run", False, "add", "crew_owned"),
        ("/api/security/tool-grants", REF, True, "add", "set_elsewhere"),
        ("/api/security/tool-grants/remove", "@notes", False, "remove", "invalid_ref"),
        ("/api/security/tool-grants/remove", REF, True, "remove", "set_elsewhere"),
    ],
)
@pytest.mark.asyncio
async def test_each_tool_list_refusal_writes_one_denied_record(
    state, tmp_path, monkeypatch, sel_records, path, ref, overlay, op, code
):
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: False
    )
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: overlay)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(path, json={"ref": ref})
        body = await resp.json()
    assert body["code"] == code
    assert _denied(sel_records) == [(f"security.tool_grants.{op}", code)]
    assert _stored(tmp_path) == []

"""App-drawn sessions: the ``{app, kind, object}`` slot binding a session kind creates.

``POST /api/chat/slots`` with ``session_kind: {app, kind}`` creates an ordinary chat
slot bound to an installed app's declared kind (``dashboard/session_kinds.py``). These
cover the create gate (who may ask, which app and kind, and which creates may not carry
one, every refusal building nothing), the server-minted ``object``, the birth line
that brings an idle session back as its kind after a restart, the re-validation every
read of that line applies, and the projection every client receives.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner

from kiro_crew.apps.manager_records import APP_MANIFEST_FILENAME, INSTALLED_META_FILENAME, app_dir
from kiro_crew.dashboard import session_kinds
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, slot_history_key

HOST = "project-host"
KIND = "project"
OBJECT = "0123456789abcdef0123456789abcdef"


def _install(name: str = HOST, *, enabled: bool = True, kinds: object = None) -> None:
    """Write *name* as an installed app whose manifest declares *kinds*."""
    root = app_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    declared = (
        [{"id": KIND, "title": "Project app", "menuLabel": "New project app", "entry": "pane.mjs"}]
        if kinds is None
        else kinds
    )
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Project host",
        "description": "Draws project sessions",
        "author": "tester",
        "contributes": {"sessionKinds": declared},
    }
    (root / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    installed = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Project host",
        "enabled": enabled,
    }
    (root / INSTALLED_META_FILENAME).write_text(json.dumps(installed), encoding="utf-8")


async def _state(tmp_path, monkeypatch):
    from test_chat_agent_selection import _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    return state


async def _create(state, body: dict, *, app_token: str = "") -> tuple[int, dict]:
    headers = {"X-Test-App": app_token} if app_token else {}
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json=body, headers=headers)
        return response.status, await response.json()


# ── The binding's grammar ─────────────────────────────────────────────────────


def test_a_minted_binding_is_the_shape_every_read_accepts():
    minted = session_kinds.mint_session_kind(HOST, KIND)
    assert session_kinds.SESSION_KIND_OBJECT_RE.fullmatch(minted["object"])
    assert session_kinds.session_kind_binding(minted) == minted
    assert minted["object"] != session_kinds.mint_session_kind(HOST, KIND)["object"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "project-host/project",
        {"app": HOST, "kind": KIND},
        {"app": HOST, "kind": KIND, "object": OBJECT.upper()},
        {"app": HOST, "kind": KIND, "object": OBJECT + "0"},
        {"app": HOST, "kind": KIND, "object": "../../" + OBJECT[6:]},
        {"app": HOST, "kind": KIND, "object": 7},
        {"app": "Not An App", "kind": KIND, "object": OBJECT},
        {"app": "system", "kind": KIND, "object": OBJECT},
        {"app": HOST, "kind": "Project", "object": OBJECT},
        {"app": HOST, "kind": "project\n", "object": OBJECT},
    ],
)
def test_anything_but_a_well_formed_binding_reads_as_none(value):
    assert session_kinds.session_kind_binding(value) is None


def test_a_read_binding_is_a_fresh_dict_of_the_three_keys_only():
    raw = {"app": HOST, "kind": KIND, "object": OBJECT, "entry": "../evil.mjs"}
    binding = session_kinds.session_kind_binding(raw)
    assert binding == {"app": HOST, "kind": KIND, "object": OBJECT}
    assert binding is not raw


# ── What the create menu lists ────────────────────────────────────────────────


def test_the_app_listing_serves_each_apps_declared_kinds_and_its_switch():
    """The create menu reads the kinds from ``GET /api/apps`` (``list_apps``): each
    row's manifest carries them, beside the ``enabled`` flag that decides whether the
    row is offered."""
    from kiro_crew.apps.manager import list_apps

    _install()
    _install("other-app", enabled=False)
    rows = {row["name"]: row for row in list_apps()}
    for name, enabled in ((HOST, True), ("other-app", False)):
        assert rows[name]["enabled"] is enabled
        (kind,) = rows[name]["manifest"]["contributes"]["sessionKinds"]
        assert (kind["id"], kind["entry"]) == (KIND, "pane.mjs")


# ── The create ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_owner_creates_a_bound_session_that_survives_a_restart(tmp_path, monkeypatch):
    state = await _state(tmp_path, monkeypatch)
    _install()
    status, created = await _create(
        state, {"session_kind": {"app": HOST, "kind": KIND}, "title": "Tracker"}
    )
    assert status == 200, created
    binding = created["session_kind"]
    assert set(binding) == {"app", "kind", "object"}
    assert (binding["app"], binding["kind"]) == (HOST, KIND)
    assert session_kinds.SESSION_KIND_OBJECT_RE.fullmatch(binding["object"])
    slot = state._slots[created["key"]]
    assert slot._session_kind == binding
    # Not the ownership field: the owner's own create stays the owner's.
    assert slot._app == ""
    # Durable at birth, before any message: the birth line carries the binding.
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("session_kind") == binding
    state._slots.pop(created["key"])
    restored = _rehydrate_slot_from_history(state, created["key"])
    assert restored is not None
    assert restored._session_kind == binding
    assert state.serialize_slot(restored)["session_kind"] == binding


@pytest.mark.asyncio
async def test_the_server_mints_the_object_and_ignores_one_the_caller_sends(tmp_path, monkeypatch):
    state = await _state(tmp_path, monkeypatch)
    _install()
    body = {"session_kind": {"app": HOST, "kind": KIND, "object": OBJECT}}
    first_status, first = await _create(state, body)
    second_status, second = await _create(state, body)
    assert first_status == second_status == 200, (first, second)
    objects = {first["session_kind"]["object"], second["session_kind"]["object"]}
    assert len(objects) == 2 and OBJECT not in objects


@pytest.mark.asyncio
async def test_the_apps_own_token_may_create_its_kind(tmp_path, monkeypatch):
    state = await _state(tmp_path, monkeypatch)
    _install()
    status, created = await _create(
        state, {"session_kind": {"app": HOST, "kind": KIND}}, app_token=HOST
    )
    assert status == 200, created
    assert created["session_kind"]["app"] == HOST
    assert state._slots[created["key"]]._app == HOST


@pytest.mark.asyncio
async def test_a_plain_create_and_a_null_kind_are_unbound(tmp_path, monkeypatch):
    state = await _state(tmp_path, monkeypatch)
    for body in ({}, {"session_kind": None}):
        status, created = await _create(state, body)
        assert status == 200, created
        assert created["session_kind"] is None
        assert state._slots[created["key"]]._session_kind is None


def _refusal_cases():
    good = {"app": HOST, "kind": KIND}
    return {
        "unknown app": ({"session_kind": {"app": "no-such-app", "kind": KIND}}, {}),
        "disabled app": ({"session_kind": good}, {"enabled": False}),
        "undeclared kind": ({"session_kind": {"app": HOST, "kind": "other"}}, {}),
        "kinds block invalid": (
            {"session_kind": good},
            {"kinds": [{"id": KIND, "title": "P", "menuLabel": "P", "entry": "../x.mjs"}] * 2},
        ),
        "bad kind id": ({"session_kind": {"app": HOST, "kind": "Project"}}, {}),
        "bad app name": ({"session_kind": {"app": "Not An App", "kind": KIND}}, {}),
        "not an object": ({"session_kind": f"{HOST}/{KIND}"}, {}),
        "ephemeral": ({"session_kind": good, "ephemeral": True}, {}),
        "artifact": ({"session_kind": good, "artifact": "some-artifact"}, {}),
        "named": ({"session_kind": good, "name": "named-session"}, {}),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_refusal_cases()))
async def test_a_refused_kind_builds_nothing(tmp_path, monkeypatch, case):
    body, install = _refusal_cases()[case]
    state = await _state(tmp_path, monkeypatch)
    _install(**install)
    before = set(state._slots)
    status, data = await _create(state, body)
    assert status == 400, data
    assert data["code"] == "invalid_session_kind"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_another_apps_token_is_refused(tmp_path, monkeypatch):
    state = await _state(tmp_path, monkeypatch)
    _install()
    _install("other-app", kinds=[])
    before = set(state._slots)
    status, data = await _create(
        state, {"session_kind": {"app": HOST, "kind": KIND}}, app_token="other-app"
    )
    assert status == 400, data
    assert data["code"] == "invalid_session_kind"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_the_create_fails_and_retracts_when_the_birth_line_cannot_be_written(
    tmp_path, monkeypatch
):
    from kiro_crew.dashboard import chat_handlers

    state = await _state(tmp_path, monkeypatch)
    _install()
    before = set(state._slots)

    async def failing_save(*_a, **kw):
        assert kw.get("best_effort") is False
        return False

    monkeypatch.setattr(state.conversation_log, "update_metadata_if", lambda *a, **kw: False)
    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", failing_save)
    status, data = await _create(state, {"session_kind": {"app": HOST, "kind": KIND}})
    assert status == 503, data
    assert data["code"] == "session_kind_unpersisted"
    assert set(state._slots) == before


# ── The metadata line ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered",
    [
        {"app": HOST, "kind": KIND, "object": "not-a-minted-object"},
        {"app": "../escape", "kind": KIND, "object": OBJECT},
        "project-host/project",
    ],
)
async def test_a_tampered_line_restores_the_slot_unbound(tmp_path, monkeypatch, tampered):
    state = await _state(tmp_path, monkeypatch)
    _install()
    status, created = await _create(state, {"session_kind": {"app": HOST, "kind": KIND}})
    assert status == 200, created
    key = created["key"]
    history_key = slot_history_key(state._slots[key])
    state.conversation_log.update_metadata(history_key, {"session_kind": tampered})
    assert state.conversation_log.get_metadata(history_key)["session_kind"] == tampered
    state._slots.pop(key)
    restored = _rehydrate_slot_from_history(state, key)
    assert restored is not None
    assert restored._session_kind is None
    assert state.serialize_slot(restored)["session_kind"] is None

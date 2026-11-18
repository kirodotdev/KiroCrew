"""An installed app may keep its own chats out of the session lists.

``permissions.hiddenSessions: true`` lets the app create a chat whose mode is its
own app id (``apps/hidden_sessions.py``). The chat sidebar already lists only plain
(``""``) chats; the Older-sessions listing leaves these out while the app may hide
them. The mode must be the creating app's own id, so no app hides another app's
chat or the person's, and an app that declares nothing behaves as before.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import hidden_sessions
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.permissions import format_permissions_summary
from kiro_crew.history import ConversationLog

APP = "acme-app"
OTHER = "other-app"


# ── the declaration ───────────────────────────────────────────────────────────


def test_only_the_literal_true_declares_it() -> None:
    assert Permissions.from_dict({"hiddenSessions": True}).hiddenSessions is True
    for value in ("true", 1, None, False):
        assert Permissions.from_dict({"hiddenSessions": value}).hiddenSessions is False
    assert Permissions.from_dict({}).to_dict() == {}
    assert Permissions(hiddenSessions=True).to_dict() == {"hiddenSessions": True}


def test_the_store_row_discloses_it_to_the_trust_prompt() -> None:
    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest

    entry = {"permissions": {"chatSlots": True, "hiddenSessions": True}}
    assert _disclosed_permissions_manifest(entry) == {
        "permissions": {"chatSlots": True, "hiddenSessions": True}
    }
    assert _disclosed_permissions_manifest({"permissions": {"hiddenSessions": "true"}}) is None


def test_the_install_summary_names_it() -> None:
    manifest = SimpleNamespace(permissions=Permissions(hiddenSessions=True))
    assert "out of your session list" in format_permissions_summary(manifest)  # type: ignore[arg-type]


# ── which apps may hide ───────────────────────────────────────────────────────


def _installed(monkeypatch, *, enabled=True, declares=True, trusted=True, installed=True):
    from kiro_crew.apps import execution, manager

    manifest = SimpleNamespace(permissions=Permissions(hiddenSessions=declares))
    monkeypatch.setattr(manager, "is_app_enabled", lambda name: installed and enabled)
    monkeypatch.setattr(manager, "get_app_manifest", lambda name: manifest if installed else None)
    monkeypatch.setattr(
        execution, "third_party_ceiling_closed", lambda name: None if trusted else "untrusted"
    )


def test_an_enabled_trusted_declaring_app_may_hide(monkeypatch) -> None:
    _installed(monkeypatch)
    assert hidden_sessions.may_hide_sessions(APP) is True


@pytest.mark.parametrize(
    "state",
    [
        {"installed": False},
        {"enabled": False},
        {"declares": False},
        {"trusted": False},
    ],
    ids=["uninstalled", "disabled", "not-declared", "untrusted"],
)
def test_any_other_app_may_not(monkeypatch, state) -> None:
    _installed(monkeypatch, **state)
    assert hidden_sessions.may_hide_sessions(APP) is False


@pytest.mark.parametrize("mode", ["member", "crew", "orchestrator", "", "Bad Name", 7, None])
def test_a_core_mode_or_a_non_app_name_is_never_an_app_s(monkeypatch, mode) -> None:
    """An app that shares a core mode's name ("member" is a Bot's DM thread) gets none."""
    _installed(monkeypatch)
    assert hidden_sessions.may_hide_sessions(mode) is False


# ── creating a hidden chat ────────────────────────────────────────────────────


def _state(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    state.broadcast_ws = MagicMock()
    return state


def _client(state, caller_app: str | None = None, user: str = "") -> TestClient:
    from chat_test_helpers import _make_app

    from kiro_crew.dashboard.chat import api_chat_slot_create

    app = _make_app(state)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    if caller_app is not None:

        @web.middleware
        async def as_app(request, handler):
            request["app"] = caller_app
            request["user"] = user
            return await handler(request)

        app.middlewares.insert(0, as_app)
    return TestClient(TestServer(app))


def _may_hide(monkeypatch, allowed=(APP,)) -> list[str]:
    asked: list[str] = []

    def fake(app_id):
        asked.append(app_id)
        return app_id in allowed

    monkeypatch.setattr(hidden_sessions, "may_hide_sessions", fake)
    return asked


@pytest.mark.asyncio
async def test_an_app_page_creates_a_chat_under_its_own_mode(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    _may_hide(monkeypatch)
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots", json={"name": "dj-1", "embed_app": APP, "mode": APP}
        )
        assert resp.status == 200, await resp.text()
    assert state.get_slot("dj-1").mode == APP


@pytest.mark.asyncio
async def test_an_app_token_creates_a_chat_under_its_own_mode(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    _may_hide(monkeypatch)
    async with _client(state, caller_app=APP) as client:
        resp = await client.post("/api/chat/slots", json={"name": f"{APP}-dj", "mode": APP})
        assert resp.status == 200, await resp.text()
    assert state.get_slot(f"{APP}-dj").mode == APP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller_app", "body"),
    [
        # A page naming one app cannot pick another app's mode.
        (None, {"name": "spoof-1", "embed_app": APP, "mode": OTHER}),
        # A plain create names no app, so it has no app mode to take.
        (None, {"name": "spoof-2", "mode": APP}),
        # An app token cannot take another app's mode, whatever it claims.
        (APP, {"name": f"{APP}-x", "mode": OTHER, "embed_app": OTHER}),
        # An app that may not hide (disabled, uninstalled, untrusted, undeclared).
        (None, {"name": "spoof-3", "embed_app": "idle-app", "mode": "idle-app"}),
    ],
    ids=["page-other-app", "no-app", "token-other-app", "app-may-not-hide"],
)
async def test_any_other_mode_is_refused(tmp_path, monkeypatch, caller_app, body) -> None:
    state = _state(tmp_path, monkeypatch)
    # Both apps may hide their own chats: the refusal is about whose mode it is.
    _may_hide(monkeypatch, allowed=(APP, OTHER))
    async with _client(state, caller_app=caller_app) as client:
        resp = await client.post("/api/chat/slots", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_mode"
    assert state.get_slot(body["name"]) is None


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_caller_cannot_name_an_app_page(tmp_path, monkeypatch) -> None:
    """Only the owner's own request may speak for an app page through ``embed_app``."""
    state = _state(tmp_path, monkeypatch)
    _may_hide(monkeypatch)
    async with _client(state, caller_app="", user="someone-else") as client:
        resp = await client.post(
            "/api/chat/slots", json={"name": "guest-1", "embed_app": APP, "mode": APP}
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "invalid_mode"
    assert state.get_slot("guest-1") is None


# ── an open hidden chat while its app may not hide it ────────────────────────


@pytest.fixture(autouse=True)
def _unpublished_listing(_floor_monkeypatch):
    _floor_monkeypatch.setattr(hidden_sessions, "_hiding_apps", None)
    _floor_monkeypatch.setattr(hidden_sessions, "_app_ids", frozenset())
    _floor_monkeypatch.setattr(hidden_sessions, "_refresh_queued", False)
    yield
    hidden_sessions.bind_listing(None, None)


async def _listed_surface(client: TestClient, key: str) -> str:
    resp = await client.get("/api/chat/slots")
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    rows = data if isinstance(data, list) else data["slots"]
    (row,) = [r for r in rows if r["key"] == key]
    return row["surface"]


@pytest.mark.asyncio
async def test_disabling_the_app_lists_its_open_hidden_chat_and_enabling_hides_it(
    tmp_path, monkeypatch
) -> None:
    """The owner's disable asks for the refresh that flips the list, and resends it."""
    from kiro_crew.apps import protected_app_state

    state = _state(tmp_path, monkeypatch)
    allowed = {APP}
    monkeypatch.setattr(hidden_sessions, "may_hide_sessions", lambda app_id: app_id in allowed)
    monkeypatch.setattr(hidden_sessions, "_installed_app_names", lambda: [APP, OTHER])
    refreshes: list[int] = []
    async with _client(state) as client:
        # The dashboard runs the refresh in a worker thread; here it runs at once.
        hidden_sessions.bind_listing(
            hidden_sessions.refresh_hiding_apps, lambda: refreshes.append(1)
        )
        resp = await client.post(
            "/api/chat/slots", json={"name": "dj-open", "embed_app": APP, "mode": APP}
        )
        assert resp.status == 200, await resp.text()
        hidden_sessions.request_hiding_refresh()
        assert await _listed_surface(client, "dj-open") == APP
        refreshes.clear()

        allowed.clear()
        protected_app_state.set_owner_disabled(APP, True)
        # Every open browser is sent the slots frame again, not only the next reader.
        assert refreshes == [1]
        assert await _listed_surface(client, "dj-open") == ""

        allowed.add(APP)
        protected_app_state.set_owner_disabled(APP, False)
        assert await _listed_surface(client, "dj-open") == APP
    # The chat itself keeps its mode; only the list it shows in changes.
    assert state.get_slot("dj-open").mode == APP


def test_the_listing_reads_only_the_published_set(monkeypatch) -> None:
    """The slots frame never reads an app record: no disk I/O on the serving loop."""

    def on_disk(app_id):
        raise AssertionError("the listing read an app record")

    monkeypatch.setattr(hidden_sessions, "may_hide_sessions", on_disk)
    monkeypatch.setattr(hidden_sessions, "_app_ids", frozenset({APP, OTHER}))
    monkeypatch.setattr(hidden_sessions, "_hiding_apps", frozenset({APP}))
    assert hidden_sessions.listed_surface(APP) == APP
    assert hidden_sessions.listed_surface(OTHER) == ""


def test_a_live_trust_setting_edit_refreshes_the_set(monkeypatch) -> None:
    """A trust change from the CLI or a hand edit reaches the listing, not only the endpoint."""
    from kiro_crew.config import live

    subscribed: dict = {}

    def fake_subscribe(*prefixes, callback, name=None):
        subscribed.update(prefixes=prefixes, callback=callback)
        return object()

    monkeypatch.setattr(live, "subscribe", fake_subscribe)
    monkeypatch.setattr(hidden_sessions, "_trust_config_sub", None)
    asked: list[int] = []
    hidden_sessions.bind_listing(lambda: asked.append(1), None)
    hidden_sessions.watch_trust_config()
    assert set(subscribed["prefixes"]) == {
        "agent.apps_allow_third_party",
        "agent.apps_trusted",
        "agent.apps_trusted_local",
        "agent.apps_trusted_repositories",
    }
    subscribed["callback"](SimpleNamespace(path="agent.apps_trusted"))
    assert asked == [1]


def test_an_unpublished_or_failed_set_shows_the_chat(monkeypatch) -> None:
    monkeypatch.setattr(hidden_sessions, "_app_ids", frozenset({APP}))
    assert hidden_sessions.listed_surface(APP) == ""

    def broken():
        raise OSError("unreadable apps tree")

    monkeypatch.setattr(hidden_sessions, "_installed_app_names", broken)
    hidden_sessions.refresh_hiding_apps()
    assert hidden_sessions.listed_surface(APP) == ""


@pytest.mark.parametrize("mode", ["", "member", "orchestrator", "Bad Name", "custom-mode"])
def test_a_mode_that_names_no_app_keeps_its_surface(monkeypatch, mode) -> None:
    """A core, worker or any other mode mirrors as before; only an app's mode is listed."""
    monkeypatch.setattr(hidden_sessions, "_app_ids", frozenset({APP}))
    monkeypatch.setattr(hidden_sessions, "_hiding_apps", frozenset())
    assert hidden_sessions.listed_surface(mode) == mode


def test_an_uninstalled_app_s_open_chat_is_listed(monkeypatch) -> None:
    installed = [APP]
    monkeypatch.setattr(hidden_sessions, "_installed_app_names", lambda: list(installed))
    monkeypatch.setattr(hidden_sessions, "may_hide_sessions", lambda app_id: app_id in installed)
    hidden_sessions.refresh_hiding_apps()
    assert hidden_sessions.listed_surface(APP) == APP
    installed.clear()
    hidden_sessions.refresh_hiding_apps()
    assert hidden_sessions.listed_surface(APP) == ""


@pytest.mark.asyncio
async def test_an_existing_chat_keeps_its_mode(tmp_path, monkeypatch) -> None:
    """Naming a person's chat cannot move it out of their list."""
    state = _state(tmp_path, monkeypatch)
    _may_hide(monkeypatch)
    state.get_or_create_slot("mine")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots", json={"name": "mine", "embed_app": APP, "mode": APP}
        )
        assert resp.status == 200, await resp.text()
    assert state.get_slot("mine").mode == ""


@pytest.mark.asyncio
async def test_a_built_in_worker_mode_needs_no_declaration(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    asked = _may_hide(monkeypatch, allowed=())
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._CREATABLE_MODES", ("", "builtin-worker")
    )
    # The registry `_CREATABLE_MODES` is built from, which the listing reads too.
    monkeypatch.setattr("kiro_crew.apps.seams.worker_slot_modes", lambda: ("builtin-worker",))
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots", json={"name": "w-1", "mode": "builtin-worker"})
        assert resp.status == 200, await resp.text()
    assert state.get_slot("w-1").mode == "builtin-worker"
    assert asked == []


# ── a hidden chat an app's backend opens (``ctx.chat_slots``) ────────────────


@pytest.mark.asyncio
async def test_chat_slots_opens_a_hidden_chat_stamped_with_its_app(tmp_path, monkeypatch) -> None:
    """Hidden from the lists, and stamped ``_app`` so app-scoped rules know whose it is.

    While the app may not hide it (disabled, untrusted), the open chat lists again.
    """
    from kiro_crew.apps.chat_slot_sdk import ChatSlotSDK

    state = _state(tmp_path, monkeypatch)
    allowed = {APP}
    asked: list[str] = []

    def may_hide(app_id):
        asked.append(app_id)
        return app_id in allowed

    monkeypatch.setattr(hidden_sessions, "may_hide_sessions", may_hide)
    monkeypatch.setattr(hidden_sessions, "_installed_app_names", lambda: [APP])
    key = f"{APP}-dj"
    slot, created = await ChatSlotSDK(APP).open(state, key, hidden=True)
    assert created is True
    assert (slot._app, slot.mode) == (APP, APP)
    assert asked == [APP]
    async with _client(state) as client:
        hidden_sessions.bind_listing(hidden_sessions.refresh_hiding_apps, lambda: None)
        hidden_sessions.request_hiding_refresh()
        assert await _listed_surface(client, key) == APP
        allowed.clear()
        hidden_sessions.request_hiding_refresh()
        assert await _listed_surface(client, key) == ""
    assert slot.mode == APP


@pytest.mark.asyncio
async def test_chat_slots_refuses_a_hidden_chat_to_an_app_that_may_not_hide(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.apps.chat_slot_sdk import ChatSlotRefused, ChatSlotSDK

    state = _state(tmp_path, monkeypatch)
    _may_hide(monkeypatch, allowed=())
    with pytest.raises(ChatSlotRefused):
        await ChatSlotSDK(APP).open(state, f"{APP}-dj", hidden=True)
    assert state.get_slot(f"{APP}-dj") is None


@pytest.mark.asyncio
async def test_chat_slots_opens_a_listed_chat_by_default(tmp_path, monkeypatch) -> None:
    from kiro_crew.apps.chat_slot_sdk import ChatSlotSDK

    state = _state(tmp_path, monkeypatch)
    asked = _may_hide(monkeypatch)
    plain, _ = await ChatSlotSDK(APP).open(state, f"{APP}-plain")
    assert (plain._app, plain.mode) == (APP, "")
    # An existing chat keeps its mode, whatever a later open asks.
    again, created = await ChatSlotSDK(APP).open(state, f"{APP}-plain", hidden=True)
    assert again is plain and created is False and plain.mode == ""
    assert asked == []


# ── the Older-sessions listing ────────────────────────────────────────────────


def test_a_listing_row_carries_the_chat_s_mode(tmp_path) -> None:
    log = ConversationLog(tmp_path)
    for key, mode in (("dashboard_hidden", APP), ("dashboard_plain", "")):
        meta = {"_type": "metadata", "title": key, **({"mode": mode} if mode else {})}
        (tmp_path / f"{key}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    rows = {row["key"]: row for row in log.list_sessions()}
    assert rows["dashboard_hidden"]["mode"] == APP
    assert "mode" not in rows["dashboard_plain"]


def _listing_request(sessions: list[dict]) -> web.Request:
    conv_log = MagicMock()
    conv_log.list_sessions_indexed.return_value = sessions
    conv_log._canonical_key = ConversationLog._canonical_key
    state = MagicMock()
    state.conversation_log = conv_log
    state._slots = {}
    request = MagicMock(spec=web.Request)
    request.get = {}.get
    request.app = {"state": state}
    request.query = {"user_only": "1"}
    return request


_ROWS = [
    {"key": "dashboard_chat-1", "title": "mine"},
    {"key": "dashboard_dj-1", "title": "songs", "mode": APP},
    {"key": "dashboard_dj-2", "title": "other app", "mode": OTHER},
]


@pytest.mark.asyncio
async def test_older_sessions_leaves_out_a_chat_its_app_hides(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import api_sessions

    _may_hide(monkeypatch)
    resp = await api_sessions(_listing_request([dict(r) for r in _ROWS]))
    body = json.loads(resp.body.decode("utf-8"))
    assert [s["key"] for s in body["sessions"]] == ["dashboard_chat-1", "dashboard_dj-2"]
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_a_chat_its_app_can_no_longer_hide_is_listed_again(monkeypatch) -> None:
    """Disabled, uninstalled or untrusted: the person can find the chat again."""
    from kiro_crew.dashboard.handlers import api_sessions

    _may_hide(monkeypatch, allowed=())
    resp = await api_sessions(_listing_request([dict(r) for r in _ROWS]))
    body = json.loads(resp.body.decode("utf-8"))
    assert [s["key"] for s in body["sessions"]] == [r["key"] for r in _ROWS]


def test_delete_all_in_older_sessions_skips_what_the_list_hides(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers.sessions_parts import delete_routes

    _may_hide(monkeypatch)
    log = MagicMock()
    log.list_sessions_indexed.return_value = [dict(r) for r in _ROWS]
    log.get_metadata_status.return_value = ({}, True)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions._open_row_test", lambda state: lambda key: False
    )
    clearable, _skipped, _unreadable = delete_routes._clearable_history_keys(
        MagicMock(), log, user_only=True
    )
    assert "dashboard_dj-1" not in clearable
    assert "dashboard_chat-1" in clearable


@pytest.mark.parametrize(
    ("mode", "may_hide", "builtin", "keeps"),
    [
        ("", False, False, True),
        (None, False, False, True),
        (APP, True, False, True),
        (APP, False, False, False),
        ("builtin-worker", False, True, True),
    ],
    ids=["plain", "absent", "app-may-hide", "app-may-not-hide", "built-in-worker"],
)
def test_a_resumed_chat_keeps_only_a_mode_the_chat_page_can_still_reach(
    monkeypatch, mode, may_hide, builtin, keeps
) -> None:
    """A chat Older sessions lists again (its app disabled, uninstalled, untrusted)
    resumes plain, so the chat page can open it."""
    from kiro_crew.apps import seams

    monkeypatch.setattr(hidden_sessions, "may_hide_sessions", lambda m: may_hide)
    monkeypatch.setattr(seams, "worker_slot_modes", lambda: ("builtin-worker",) if builtin else ())
    assert hidden_sessions.resume_keeps_mode(mode) is keeps

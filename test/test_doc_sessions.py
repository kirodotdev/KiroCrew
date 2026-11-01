"""Doc sessions: a sidebar slot whose pane is a markdown document instead of a chat.

Covers the file store behind a doc (``dashboard/doc_files.py``), its owner-only
routes, the ``doc: true`` slot create (bound at birth, durable across a restart,
refused anywhere but a person's own fresh create) and the eager-spawn gate that
keeps a doc from ever starting an agent process.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from eager_spawn_helpers import _mock_state, _prime

from kiro_crew.dashboard import doc_files
from kiro_crew.dashboard.chat_runner import schedule_eager_spawn
from kiro_crew.dashboard.handlers.doc_files import register_doc_routes
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture
def docs_home(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_files, "data_home", lambda: tmp_path)
    return tmp_path / doc_files.DOCS_DIRNAME


# ── The file store ────────────────────────────────────────────────────────────


def test_a_minted_id_is_the_only_shape_accepted():
    assert doc_files.is_doc_id(doc_files.mint_doc_id())


def test_a_board_id_is_a_doc_id_with_the_board_prefix_and_a_doc_never_has_it():
    board = doc_files.mint_doc_id("board")
    assert doc_files.is_doc_id(board)
    assert board.startswith(doc_files.BOARD_ID_PREFIX)
    assert not any(
        doc_files.mint_doc_id().startswith(doc_files.BOARD_ID_PREFIX) for _ in range(200)
    )


def test_a_codebase_id_has_its_own_prefix_and_no_other_kind_carries_it():
    codebase = doc_files.mint_doc_id("codebase")
    assert doc_files.is_doc_id(codebase)
    assert codebase.startswith(doc_files.CODEBASE_ID_PREFIX)
    others = [doc_files.mint_doc_id(k) for k in ("doc", "board") for _ in range(200)]
    assert not any(i.startswith(doc_files.CODEBASE_ID_PREFIX) for i in others)
    # An unknown kind is an ordinary doc, never a prefixed one.
    assert not doc_files.mint_doc_id("bogus").startswith(
        (doc_files.CODEBASE_ID_PREFIX, doc_files.BOARD_ID_PREFIX)
    )


def test_a_dashboard_id_has_its_own_prefix_and_no_other_kind_carries_it():
    dashboard = doc_files.mint_doc_id("dashboard")
    assert doc_files.is_doc_id(dashboard)
    assert dashboard.startswith(doc_files.DASHBOARD_ID_PREFIX)
    others = [doc_files.mint_doc_id(k) for k in ("doc", "board", "codebase") for _ in range(200)]
    assert not any(i.startswith(doc_files.DASHBOARD_ID_PREFIX) for i in others)


@pytest.mark.asyncio
async def test_a_dashboard_create_binds_a_dashboard_id(tmp_path, monkeypatch):
    from test_chat_agent_selection import _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"doc": True, "doc_kind": "dashboard", "title": "Ops"}
        )
        assert response.status == 200, await response.text()
        created = await response.json()
    assert created["doc"].startswith(doc_files.DASHBOARD_ID_PREFIX)


@pytest.mark.asyncio
async def test_a_board_create_binds_a_board_id(tmp_path, monkeypatch):
    from test_chat_agent_selection import _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"doc": True, "doc_kind": "board", "title": "Ideas"}
        )
        assert response.status == 200, await response.text()
        created = await response.json()
    assert created["doc"].startswith(doc_files.BOARD_ID_PREFIX)
    for bad in ["", "../etc/passwd", "ABCDEF" * 6, "a" * 31, "a" * 33, None, 7]:
        assert not doc_files.is_doc_id(bad)
    with pytest.raises(ValueError):
        doc_files.doc_path("../escape")


def test_a_doc_with_no_file_reads_empty(docs_home):
    doc = doc_files.read_doc(doc_files.mint_doc_id())
    assert doc == doc_files.DocFile(content="", version="")


def test_a_save_lands_byte_for_byte_and_reads_back(docs_home):
    doc_id = doc_files.mint_doc_id()
    text = "# Title\r\n\nbody\n"
    saved = doc_files.write_doc(doc_id, text, expected_version=None)
    assert (docs_home / f"{doc_id}.md").read_bytes() == text.encode("utf-8")
    assert saved.content == text
    assert saved.version not in ("", None)


def test_the_version_is_a_string_so_it_survives_json_to_the_browser(docs_home):
    """A float-sized integer stamp (``st_mtime_ns``) is rounded by the browser's JSON
    parse, so every save after the first came back as a false conflict."""
    doc_id = doc_files.mint_doc_id()
    version = doc_files.read_doc(doc_id).version
    for text in ["one", "two", "three"]:
        saved = doc_files.write_doc(doc_id, text, expected_version=version)
        assert isinstance(saved.version, str)
        version = saved.version
    assert doc_files.read_doc(doc_id).content == "three"


def test_a_save_over_a_newer_file_is_refused_with_the_file_as_it_is(docs_home):
    doc_id = doc_files.mint_doc_id()
    first = doc_files.write_doc(doc_id, "one", expected_version=None)
    (docs_home / f"{doc_id}.md").write_text("edited elsewhere", encoding="utf-8")
    with pytest.raises(doc_files.DocConflictError) as caught:
        doc_files.write_doc(doc_id, "two", expected_version=first.version)
    assert caught.value.current.content == "edited elsewhere"
    assert (docs_home / f"{doc_id}.md").read_text(encoding="utf-8") == "edited elsewhere"


def test_an_oversized_doc_is_refused(docs_home):
    with pytest.raises(ValueError):
        doc_files.write_doc(
            doc_files.mint_doc_id(), "x" * (doc_files.MAX_DOC_BYTES + 1), expected_version=None
        )


# ── The routes ────────────────────────────────────────────────────────────────


def _doc_app(*, user: str = "local-app", app_name: str = "") -> web.Application:
    @web.middleware
    async def caller(request, handler):
        request["user"] = user
        request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[caller])
    app["state"] = type("S", (), {"owner_id": ""})()
    register_doc_routes(app)
    return app


@pytest.mark.asyncio
async def test_the_owner_saves_and_reads_a_doc(docs_home):
    doc_id = doc_files.mint_doc_id()
    async with TestClient(TestServer(_doc_app())) as client:
        put = await client.put(f"/api/docs/{doc_id}", json={"content": "hello", "version": ""})
        assert put.status == 200, await put.text()
        version = (await put.json())["version"]
        got = await client.get(f"/api/docs/{doc_id}")
        assert await got.json() == {"content": "hello", "version": version}
        stale = await client.put(f"/api/docs/{doc_id}", json={"content": "x", "version": "stale"})
        assert stale.status == 409
        assert (await stale.json())["content"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [{"app_name": "notes"}, {"user": ""}, {"user": "someone"}])
async def test_anyone_but_the_owner_is_refused(docs_home, caller):
    doc_id = doc_files.mint_doc_id()
    async with TestClient(TestServer(_doc_app(**caller))) as client:
        assert (await client.get(f"/api/docs/{doc_id}")).status == 403
        put = await client.put(f"/api/docs/{doc_id}", json={"content": "x"})
        assert put.status == 403
    assert not (docs_home / f"{doc_id}.md").exists()


@pytest.mark.asyncio
async def test_a_malformed_id_or_body_is_refused(docs_home):
    async with TestClient(TestServer(_doc_app())) as client:
        assert (await client.get("/api/docs/not-an-id")).status == 400
        doc_id = doc_files.mint_doc_id()
        for body in [
            {"content": 7},
            {"content": "x", "version": 1},
            {"content": "x", "version": True},
        ]:
            assert (await client.put(f"/api/docs/{doc_id}", json=body)).status == 400


# ── The slot create ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_doc_create_binds_a_fresh_doc_and_survives_a_restart(tmp_path, monkeypatch):
    from test_chat_agent_selection import _template_chat

    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, slot_history_key

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"doc": True, "title": "Plan"})
        assert response.status == 200, await response.text()
        created = await response.json()
    key = created["key"]
    assert doc_files.is_doc_id(created["doc"])
    slot = state._slots[key]
    assert slot._doc == created["doc"]
    # Durable at birth: a doc never gets a message, so nothing later writes it.
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("doc") == created["doc"]
    state._slots.pop(key)
    restored = _rehydrate_slot_from_history(state, key)
    assert restored is not None
    assert restored._doc == created["doc"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"name": "named-doc"},
        {"memory_mode": "incognito"},
        {"ephemeral": True},
        {"artifact": "some-artifact"},
    ],
)
async def test_a_doc_create_anywhere_but_a_fresh_ordinary_create_is_refused(
    tmp_path, monkeypatch, extra
):
    from test_chat_agent_selection import _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    before = set(state._slots)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"doc": True, **extra})
        assert response.status == 400, await response.text()
        assert (await response.json())["code"] == "invalid_doc_create"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_plain_create_is_not_a_doc(tmp_path, monkeypatch):
    from test_chat_agent_selection import _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"doc": "yes"})
        assert response.status == 200, await response.text()
        created = await response.json()
    assert created["doc"] == ""
    assert state._slots[created["key"]]._doc == ""


# ── No agent process ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_doc_slot_never_eagerly_spawns_an_agent():
    _prime(True)
    chat = _ChatSlot("chat")
    schedule_eager_spawn(_mock_state(chat), chat)
    # The control: the same signal on an ordinary slot does arm a spawn.
    assert chat._eager_spawn_task is not None
    chat._eager_spawn_task.cancel()

    doc = _ChatSlot("doc")
    doc._doc = doc_files.mint_doc_id()
    assert schedule_eager_spawn(_mock_state(doc), doc) is None
    assert doc._eager_spawn_task is None

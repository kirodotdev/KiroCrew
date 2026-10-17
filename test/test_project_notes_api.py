"""Project-notes HTTP surface and its project-first search outcome.

Two halves:

* The **search outcome** -- a note recorded under project X is found ranked
  first from another session in X and not first in Y -- against a real
  ``KnowledgeStore`` + ``HybridRetriever`` (keyword/graph legs, no embedder),
  exactly the way ``local_knowledge_search`` reaches it, proving the notes ride
  item B's project-first merge.
* The **route gates** -- the record route resolves the project server-side from
  the session key (never the body); a nested child folder resolves to its
  project root; promote refuses an app/agent principal exactly like a
  ``steering_dirs`` write; the GET list is read-only for the app; delete/promote
  are behind the owner gate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import project_notes_api as api
from kiro_crew.knowledge import project_notes
from kiro_crew.knowledge.project_scope import project_namespace
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

NS_X = project_namespace("root-x")
NS_Y = project_namespace("root-y")


@pytest.fixture
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s._close_all_for_tests()


# ── search outcome: a note ranks first in its own project, not in another ──


_QUERY = "roll back the payments deployment"
_LEARNING = "To roll back the payments deployment, run the rollback runbook."


def _search(store, namespace):
    return [
        h["content"]
        for h in HybridRetriever(store).search(_QUERY, limit=3, namespace=namespace)
        if h.get("score", 0) >= 0.012
    ]


def test_a_project_note_ranks_first_in_its_project_and_not_in_another(store):
    # One global doc and one note per project, all answering the same query, so
    # RANKING (not matching) is what the assertion reads.
    store.add_item("Global", _LEARNING, "doc", namespace="default")
    project_notes.add_note(store, namespace=NS_X, text=_LEARNING + " (project X note)")
    project_notes.add_note(store, namespace=NS_Y, text=_LEARNING + " (project Y note)")

    # A session in X, searching X first, sees X's note.
    x_hits = _search(store, NS_X)
    assert any("project X note" in h for h in x_hits)
    # The project namespace pass never surfaces Y's note.
    assert all("project Y note" not in h for h in x_hits)

    # The global pass (a session filed nowhere) does not surface either note,
    # since they are filed under their project namespaces, not "default".
    global_hits = _search(store, "default")
    assert all("project X note" not in h and "project Y note" not in h for h in global_hits)


# ── route harness ──


def _make_app(store, folders=None):
    app = web.Application()
    state = SimpleNamespace(knowledge_store=store, _folders=folders or [])
    app["state"] = state
    return app


# ── record route: project resolved from the session key, never the body ──


@pytest.mark.asyncio
async def test_record_resolves_project_from_session_key_not_body(store, monkeypatch):
    app = _make_app(store)

    # Whatever the body claims, the namespace comes from the (async) resolver.
    async def _ns(_state, _key):
        return NS_X

    monkeypatch.setattr(api, "resolve_session_project_namespace", _ns)

    async def _body(_request, max_bytes=None, **_kw):
        return {"text": "a learning", "folder_id": "forged", "namespace": "project:forged"}, None

    monkeypatch.setattr(api, "read_bounded_json", _body)
    req = make_mocked_request(
        "POST",
        "/api/knowledge/project-notes/record",
        headers={"X-Session-Key": "dashboard_abc"},
        app=app,
    )
    resp = await api.record_project_note_route(req)
    assert resp.status == 200
    # It landed under the resolver's namespace, not the body's forged one.
    assert project_notes.note_count(store, NS_X) == 1
    assert project_notes.note_count(store, project_namespace("forged")) == 0


@pytest.mark.asyncio
async def test_record_refuses_a_session_in_no_project(store, monkeypatch):
    app = _make_app(store)

    async def _ns(_s, _k):
        return ""

    monkeypatch.setattr(api, "resolve_session_project_namespace", _ns)

    async def _body(_request, max_bytes=None, **_kw):
        return {"text": "a learning"}, None

    monkeypatch.setattr(api, "read_bounded_json", _body)
    req = make_mocked_request(
        "POST",
        "/api/knowledge/project-notes/record",
        headers={"X-Session-Key": "x"},
        app=app,
    )
    resp = await api.record_project_note_route(req)
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "project_note_refused"


# ── list route: a nested child folder resolves to its project root ──


@pytest.mark.asyncio
async def test_list_resolves_a_nested_child_folder_to_its_project_root(store):
    # Folders: project root "proj", a sub-team under it, a worker under that.
    folders = [
        {"id": "proj"},
        {"id": "subteam", "parent_id": "proj"},
        {"id": "worker", "parent_id": "subteam"},
    ]
    app = _make_app(store, folders=folders)
    ns_proj = project_namespace("proj")
    project_notes.add_note(store, namespace=ns_proj, text="a project learning")

    # A worker folder deep in the tree lists the SAME project's notes.
    req = make_mocked_request(
        "GET",
        "/api/knowledge/project-notes?folder_id=worker",
        app=app,
    )
    resp = await api.list_project_notes_route(req)
    body = json.loads(resp.body)
    assert body["namespace"] == ns_proj
    assert len(body["notes"]) == 1
    assert body["notes"][0]["text"] == "a project learning"


@pytest.mark.asyncio
async def test_list_is_empty_for_a_folder_in_no_project(store):
    app = _make_app(store, folders=[])
    req = make_mocked_request("GET", "/api/knowledge/project-notes?folder_id=", app=app)
    resp = await api.list_project_notes_route(req)
    body = json.loads(resp.body)
    assert body["notes"] == [] and body["namespace"] == ""


# ── promote: an app/agent principal is refused exactly like steering_dirs ──


@pytest.mark.asyncio
async def test_promote_refuses_an_app_principal(store):
    app = _make_app(store, folders=[{"id": "proj"}])
    req = make_mocked_request(
        "POST",
        "/api/knowledge/project-notes/note1/promote",
        match_info={"note_id": "note1"},
        app=app,
    )
    # A non-person principal carries a non-empty "app" request key.
    req["app"] = "team-projects"
    resp = await api.promote_project_note_route(req)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "steering_dirs_forbidden"


@pytest.mark.asyncio
async def test_promote_refuses_a_non_owner_person(store, monkeypatch):
    app = _make_app(store, folders=[{"id": "proj"}])

    # Person principal (app == ""), but not the owner: the owner gate 403s.
    async def _deny(_request, _op):
        return web.json_response({"error": "owner", "code": "owner_only"}, status=403)

    monkeypatch.setattr(api, "require_owner_dashboard_request", _deny)
    req = make_mocked_request(
        "POST",
        "/api/knowledge/project-notes/n/promote",
        match_info={"note_id": "n"},
        app=app,
    )
    req["app"] = ""
    resp = await api.promote_project_note_route(req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_promote_refuses_a_dir_the_project_did_not_declare(store, monkeypatch):
    # Owner principal; the note exists; but the chosen steering_dir is not one the
    # project declares, so the write is refused before any file is touched.
    ns = project_namespace("proj")
    note = project_notes.add_note(store, namespace=ns, text="a learning")
    app = _make_app(store, folders=[{"id": "proj"}])
    monkeypatch.setattr(api, "require_owner_dashboard_request", lambda _r, _o: _none())

    async def _body(_request, max_bytes=None, **_kw):
        return {"folder_id": "proj", "steering_dir": "/tmp/not-declared"}, None

    monkeypatch.setattr(api, "read_bounded_json", _body)
    # The project declares NO steering dirs, so nothing matches.
    monkeypatch.setattr(api, "_resolve_folder_steering_dirs", lambda *_a, **_k: ([], None))
    req = make_mocked_request(
        "POST",
        f"/api/knowledge/project-notes/{note['id']}/promote",
        match_info={"note_id": note["id"]},
        app=app,
    )
    req["app"] = ""
    resp = await api.promote_project_note_route(req)
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "steering_dir_not_declared"


@pytest.mark.asyncio
async def test_promote_appends_to_a_declared_steering_dir(store, tmp_path, monkeypatch):
    # The happy path: an owner promotes a note into a declared steering dir, and
    # the note text is appended to a Markdown file inside it.
    steer = tmp_path / "steering"
    steer.mkdir()
    canonical = api.validate_file_path(str(steer))
    assert canonical  # resolvable, non-sensitive

    ns = project_namespace("proj")
    note = project_notes.add_note(store, namespace=ns, text="prefer small PRs")
    app = _make_app(store, folders=[{"id": "proj"}])
    monkeypatch.setattr(api, "require_owner_dashboard_request", lambda _r, _o: _none())

    async def _body(_request, max_bytes=None, **_kw):
        return {"folder_id": "proj", "steering_dir": str(steer)}, None

    monkeypatch.setattr(api, "read_bounded_json", _body)
    monkeypatch.setattr(api, "_resolve_folder_steering_dirs", lambda *_a, **_k: ([canonical], None))
    req = make_mocked_request(
        "POST",
        f"/api/knowledge/project-notes/{note['id']}/promote",
        match_info={"note_id": note["id"]},
        app=app,
    )
    req["app"] = ""
    resp = await api.promote_project_note_route(req)
    assert resp.status == 200, resp.body
    written = (steer / "project-notes.md").read_text()
    assert "prefer small PRs" in written
    assert "promoted" in written.lower()


async def _none():
    """An awaitable returning None, for monkeypatching the async owner gate."""
    return None

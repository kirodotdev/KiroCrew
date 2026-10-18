"""Session history, the knowledge library and task actions answer the owner only.

A Telegram, Teams or Webex allowlist link mints ``generate_token(<channel user>)``:
an empty app claim and a subject that is not ``owner_id``. That is an
authenticated dashboard session that is not the owner's. The app-isolation
checks on these routes (``_app_transcript_refusal``, ``_app_bulk_history_refusal``)
return ``None`` for every caller with an empty app claim, so on their own they
admit it. ``require_owner_for_dashboard_caller`` refuses it, layered on top:

* ``GET`` / ``DELETE /api/sessions/{key}`` and ``DELETE /api/sessions``;
* every ``GET /api/knowledge/*`` read, the whole-library export included;
* ``POST /api/tasks/{task_id}`` (``answer_input`` / ``cancel_wait``) and
  ``POST /api/tasks/{task_id}/cancel``, which ends a parked row through the
  same ``cancel_wait``.

The owner is still served, and an app token keeps exactly the control it had:
the uniform 404 for a transcript it does not own, its own transcript, and its
manifest scope on the library.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state
from off_loop_helpers import off_loop
from overload_fakes import Clock

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers import tasks as tasks_mod
from kiro_crew.dashboard.handlers._shared import require_owner_for_dashboard_caller
from kiro_crew.dashboard.handlers.sessions import _app_transcript_refusal
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.routes import register_all
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.taskq import model
from kiro_crew.taskq.adapters import runner as runner_mod
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord

OWNER = "U0OWNER"
#: The subject a channel allowlist mint puts in the token.
NON_OWNER = "U0NONOWNER"
#: An app whose manifest declares every route under test.
APP = "plain-app"
OTHER_APP = "other-app"

OWNER_ID = {"app": "", "user": OWNER}
NON_OWNER_ID = {"app": "", "user": NON_OWNER}
APP_ID = {"app": APP, "user": APP}

_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}


def _owner_only(body: dict) -> bool:
    return body.get("code") == "owner_only"


# ── the real helpers, on the three principals ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "is_owner", "app_check_refuses", "owner_gate_refuses"),
    [
        pytest.param(OWNER_ID, True, False, False, id="owner"),
        # The gap: the app check returns None for an empty app claim, so on its
        # own it admits a non-owner dashboard session. The owner gate refuses it.
        pytest.param(NON_OWNER_ID, False, False, True, id="non-owner-dashboard"),
        pytest.param(APP_ID, False, True, False, id="app-token"),
    ],
)
async def test_the_real_helpers_on_the_three_principals(
    tmp_path, identity, is_owner, app_check_refuses, owner_gate_refuses
):
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    off_loop(_write_transcript, state, "dashboard:owner-chat", "owner secret", "")
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", "/api/sessions/dashboard:owner-chat", app=app)
    req["app"] = identity["app"]
    req["user"] = identity["user"]

    assert is_owner_dashboard_request(req) is is_owner
    app_refusal = await _app_transcript_refusal(req, state, "dashboard:owner-chat", "probe")
    assert (app_refusal is not None) is app_check_refuses
    gate = await require_owner_for_dashboard_caller(req, "probe")
    assert (gate is not None) is owner_gate_refuses
    if gate is not None:
        assert gate.status == 403 and _owner_only(json.loads(gate.body))


# ── session history: the real token middleware and the real route table ─────


def _manifest(name: str) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "description": "owner gate fixture",
            "permissions": {"api": ["/api/sessions", "/api/sessions/*"]},
        }
    )


_MANIFESTS = {APP: _manifest(APP), OTHER_APP: _manifest(OTHER_APP)}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"owner-gate-history-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", _MANIFESTS.__contains__)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = OWNER
    # No cron store: the delete path's owner sweep reads it, and cron
    # ownership is not what these tests decide.
    st.crons = None
    return st


@contextlib.asynccontextmanager
async def _serve(state):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _token(identity: dict) -> dict[str, str]:
    """A signed token for *identity*, minted the way its caller class mints it."""
    if identity["app"]:
        return {"token": token_auth.generate_token("local-app", app=identity["app"])}
    return {"token": token_auth.generate_token(identity["user"])}


def _write_transcript(state, key: str, text: str, app_name: str) -> None:
    log = state.conversation_log
    log.append(key, "user", text)
    log.append(key, "assistant", "noted")
    if app_name:
        log.update_metadata(key, {"app": app_name})


def _seed(state) -> None:
    off_loop(_write_transcript, state, "dashboard:owner-chat", "owner secret", "")
    off_loop(_write_transcript, state, "dashboard:app-chat", "app note", APP)


@pytest.mark.asyncio
async def test_the_owner_reads_a_transcript(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/dashboard:owner-chat", params=_token(OWNER_ID))
        assert resp.status == 200
        assert [m["content"] for m in await resp.json()] == ["owner secret", "noted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dashboard:owner-chat", "dashboard:app-chat"])
async def test_a_non_owner_dashboard_session_is_refused_a_transcript(state, key):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get(f"/api/sessions/{key}", params=_token(NON_OWNER_ID))
        text = await resp.text()
    assert resp.status == 403, text
    assert _owner_only(json.loads(text))
    assert "secret" not in text and "app note" not in text


@pytest.mark.asyncio
async def test_an_app_token_is_refused_a_foreign_transcript_as_before(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/dashboard:owner-chat", params=_token(APP_ID))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_an_app_token_still_reads_its_own_transcript(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/dashboard:app-chat", params=_token(APP_ID))
        assert resp.status == 200
        assert [m["content"] for m in await resp.json()] == ["app note", "noted"]


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_session_cannot_delete_a_transcript(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.delete(
            "/api/sessions/dashboard:owner-chat", params=_token(NON_OWNER_ID)
        )
        assert resp.status == 403
        assert _owner_only(await resp.json())
    assert state.conversation_log.has_log("dashboard:owner-chat")


@pytest.mark.asyncio
async def test_an_app_token_delete_of_a_foreign_transcript_is_refused_as_before(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions/dashboard:owner-chat", params=_token(APP_ID))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert state.conversation_log.has_log("dashboard:owner-chat")


@pytest.mark.asyncio
async def test_the_owner_deletes_a_transcript(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions/dashboard:owner-chat", params=_token(OWNER_ID))
        assert (resp.status, await resp.json()) == (200, {"ok": True})
    assert not state.conversation_log.has_log("dashboard:owner-chat")


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_session_cannot_clear_history(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions", params=_token(NON_OWNER_ID))
        assert resp.status == 403
        assert _owner_only(await resp.json())
    assert state.conversation_log.has_log("dashboard:owner-chat")
    assert state.conversation_log.has_log("dashboard:app-chat")


@pytest.mark.asyncio
async def test_an_app_token_clear_is_refused_as_before(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions", params=_token(APP_ID))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert state.conversation_log.has_log("dashboard:owner-chat")


@pytest.mark.asyncio
async def test_the_owner_clears_history(state):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions", params=_token(OWNER_ID))
        assert resp.status == 200, await resp.text()
    assert not state.conversation_log.has_log("dashboard:owner-chat")


#: The other history reads behind the same module-level guard: the list, the
#: search, a transcript's meta, the archive list and an archive read, and the
#: clearable count.
OTHER_HISTORY_READS = [
    "/api/sessions",
    "/api/sessions/search?q=secret",
    "/api/sessions/dashboard:owner-chat/meta",
    "/api/session/archive",
    "/api/session/archive/dashboard_owner-chat.jsonl",
    "/api/sessions/clearable/count",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", OTHER_HISTORY_READS)
async def test_a_non_owner_dashboard_session_is_refused_every_history_read(state, path):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get(path, params=_token(NON_OWNER_ID))
        text = await resp.text()
    assert resp.status == 403, text
    assert _owner_only(json.loads(text))
    assert "secret" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", OTHER_HISTORY_READS)
async def test_the_owner_passes_the_gate_on_every_history_read(state, path):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get(path, params=_token(OWNER_ID))
        text = await resp.text()
    assert resp.status != 403, text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", OTHER_HISTORY_READS)
async def test_an_app_token_is_not_answered_by_the_owner_gate(state, path):
    _seed(state)
    async with _serve(state) as client:
        resp = await client.get(path, params=_token(APP_ID))
        text = await resp.text()
    body = json.loads(text) if resp.content_type == "application/json" else {}
    assert not (isinstance(body, dict) and _owner_only(body)), text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "refused"),
    [
        pytest.param({"app": "", "user": OWNER}, False, id="owner"),
        pytest.param({"app": "", "user": NON_OWNER}, True, id="non-owner-dashboard"),
        pytest.param({"app": APP, "user": APP}, False, id="app-token"),
        # token_auth leaves ``app`` unset for an internal caller acting as the person.
        pytest.param({"user": OWNER}, False, id="internal-caller"),
    ],
)
async def test_the_dashboard_owner_surface_guard_on_each_caller_class(tmp_path, identity, refused):
    from kiro_crew.dashboard.handlers._shared import dashboard_owner_surface_guard

    state = _make_state(tmp_path)
    state.owner_id = OWNER
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", "/api/sessions", app=app)
    for k, v in identity.items():
        req[k] = v
    refusal = await dashboard_owner_surface_guard("probe")(req)
    assert (refusal is not None) is refused
    if refusal is not None:
        assert refusal.status == 403 and _owner_only(json.loads(refusal.body))


# ── knowledge reads: the write gate's harness ────────────────────────────────

#: Every GET route ``setup_knowledge_routes`` registers.
READ_ROUTES = [
    ("/api/knowledge/config", kh.get_config),
    ("/api/knowledge/items", kh.list_items),
    ("/api/knowledge/namespaces", kh.list_namespaces),
    ("/api/knowledge/stats", kh.get_stats),
    ("/api/knowledge/sources", kh.list_sources),
    ("/api/knowledge/source-counts", kh.source_counts),
    ("/api/knowledge/sources/{id}/files", kh.list_source_files),
    ("/api/knowledge/entities", kh.list_entities),
    ("/api/knowledge/graph", kh.get_full_graph),
    ("/api/knowledge/export", kh.export_all),
    ("/api/knowledge/items/{id}", kh.get_item),
    ("/api/knowledge/items/{id}/content", kh.get_item_content),
    ("/api/knowledge/items/{id}/related", kh.get_related_items),
    ("/api/knowledge/items/{id}/export", kh.export_item),
    ("/api/knowledge/entities/by-name/{name}/items", kh.get_entity_items),
    ("/api/knowledge/entities/{id}/graph", kh.get_entity_graph),
    ("/api/knowledge/jobs/{id}", kh.get_job),
    ("/api/knowledge/embedding/status", kh.get_embedding_status),
    ("/api/knowledge/search-for-context", kh.search_for_context),
]


def _planted(identity: dict):
    @web.middleware
    async def _mw(request, handler):
        for key, value in identity.items():
            request[key] = value
        return await handler(request)

    return _mw


def _kb_app(store, identity: dict) -> web.Application:
    app = web.Application(middlewares=[_planted(identity)])
    state = MagicMock()
    state.knowledge_store = store
    state.owner_id = OWNER
    app["state"] = state
    app["knowledge_pipeline"] = MagicMock()
    app["knowledge_embedder"] = None
    for path, handler in READ_ROUTES:
        app.router.add_get(path, handler)
    return app


def _concrete(path: str) -> str:
    return path.replace("{id}", "abc123").replace("{name}", "alpha")


@pytest.fixture()
def kb(tmp_path):
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    store.add_item("Owner plan", "owner private plan", "note")
    yield store
    store._close_all_for_tests()


def test_read_route_list_covers_every_get_registration():
    """The table above is the full set of reads setup_knowledge_routes registers."""
    app = web.Application()
    app["state"] = MagicMock()
    app["knowledge_pipeline"] = MagicMock()
    kh.setup_knowledge_routes(app)
    registered = {r.resource.canonical for r in app.router.routes() if r.method == "GET"}
    assert registered == {path for path, _ in READ_ROUTES}


@pytest.mark.asyncio
@pytest.mark.parametrize("path,handler", READ_ROUTES, ids=[p for p, _ in READ_ROUTES])
async def test_a_non_owner_dashboard_session_is_refused_every_knowledge_read(path, handler):
    store = MagicMock()
    async with TestClient(TestServer(_kb_app(store, NON_OWNER_ID))) as client:
        resp = await client.get(_concrete(path), params={"q": "plan"})
        body = await resp.json()
    assert resp.status == 403
    assert _owner_only(body)
    assert store.method_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,handler", READ_ROUTES, ids=[p for p, _ in READ_ROUTES])
async def test_the_owner_passes_the_gate_on_every_knowledge_read(path, handler, kb, monkeypatch):
    verdicts: list[object] = []
    real = kh.require_owner_for_dashboard_caller

    async def _recording(request, operation):
        verdicts.append(await real(request, operation))
        return verdicts[-1]

    monkeypatch.setattr(kh, "require_owner_for_dashboard_caller", _recording)
    async with TestClient(TestServer(_kb_app(kb, OWNER_ID))) as client:
        resp = await client.get(_concrete(path), params={"q": "plan"})
    assert verdicts == [None], "the route must consult the owner gate, and admit the owner"
    assert resp.status != 403


@pytest.mark.asyncio
async def test_the_owner_exports_the_library(kb):
    async with TestClient(TestServer(_kb_app(kb, OWNER_ID))) as client:
        resp = await client.get("/api/knowledge/export")
        text = await resp.text()
    assert resp.status == 200
    assert "owner private plan" in text


@pytest.mark.asyncio
async def test_an_app_token_keeps_its_scope_control_on_the_export(kb):
    """The Notes app declares /api/knowledge; its manifest scope governs it, not this gate."""
    async with TestClient(TestServer(_kb_app(kb, {"app": "md-notebook", "user": "x"}))) as client:
        resp = await client.get("/api/knowledge/export")
    assert resp.status == 200


# ── task actions: a real task store and the real runner admission ────────────


class _RecordingAdmission:
    """Records what the route asked for, then answers through the REAL admission."""

    def __init__(self, store: TaskStore) -> None:
        self.answers: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self._real = runner_mod.RunnerAdmission(store)

    def answer_input(self, task_id: str, answer: str, *, generation: int | None = None) -> bool:
        self.answers.append((task_id, answer))
        return self._real.answer_input(task_id, answer, generation=generation)

    def cancel_wait(self, task_id: str, *, reason: str = "", generation: int | None = None) -> bool:
        self.cancelled.append(task_id)
        return self._real.cancel_wait(task_id, reason=reason, generation=generation)


WAITING = "taskrunner:r1:task2"


@pytest.fixture
def tasks(tmp_path: Path):
    clock = Clock()
    store = TaskStore(tmp_path / "tasks.db", clock=clock).open()
    store.accept([model.TaskRecord(id=WAITING, kind=model.KIND_TASKRUNNER_STEP, params={})])
    assert store.claim(WAITING) is not None
    assert store.transition(WAITING, model.STARTING)
    assert store.transition(WAITING, model.RUNNING)
    asked = WaitRecord.input("call-1", since=clock.t, reason="the owner's step asks for input")
    assert store.enter_wait(WAITING, asked.to_dict())
    admission = _RecordingAdmission(store)

    async def _no_subagent(_task_id: str) -> bool:
        return False

    state = SimpleNamespace(
        owner_id=OWNER,
        subagents=SimpleNamespace(_taskq=store, cancel=_no_subagent),
        task_runner=SimpleNamespace(task_admission=admission),
        workflow_service=None,
        _slots={},
    )
    yield SimpleNamespace(store=store, admission=admission, state=state)
    store.close()


def _task_req(state: Any, identity: dict, path: str, body: Any = None) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", path, match_info={"task_id": WAITING}, app=app)
    for key, value in identity.items():
        req[key] = value
    if body is not None:

        async def _json() -> Any:
            return body

        req.json = _json  # type: ignore[method-assign]
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"action": "answer_input", "answer": "attacker text"}, id="answer_input"),
        pytest.param({"action": "cancel_wait"}, id="cancel_wait"),
    ],
)
async def test_a_non_owner_dashboard_session_cannot_act_on_the_owners_waiting_run(tasks, body):
    req = _task_req(tasks.state, NON_OWNER_ID, f"/api/tasks/{WAITING}", body)
    resp = await tasks_mod.api_task_action(req)
    assert resp.status == 403
    assert _owner_only(json.loads(resp.body))
    assert tasks.admission.answers == [] and tasks.admission.cancelled == []
    assert tasks.store.state_of(WAITING) == model.WAITING_INPUT


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_session_cannot_end_the_wait_through_cancel(tasks):
    req = _task_req(tasks.state, NON_OWNER_ID, f"/api/tasks/{WAITING}/cancel")
    resp = await tasks_mod.api_task_cancel(req)
    assert resp.status == 403
    assert _owner_only(json.loads(resp.body))
    assert tasks.admission.cancelled == []
    assert tasks.store.state_of(WAITING) == model.WAITING_INPUT


@pytest.mark.asyncio
async def test_the_owner_answers_the_waiting_run(tasks):
    body = {"action": "answer_input", "answer": "yes"}
    resp = await tasks_mod.api_task_action(
        _task_req(tasks.state, OWNER_ID, f"/api/tasks/{WAITING}", body)
    )
    assert resp.status == 200, resp.text
    assert tasks.admission.answers == [(WAITING, "yes")]


@pytest.mark.asyncio
async def test_the_owner_ends_the_wait_through_cancel(tasks):
    resp = await tasks_mod.api_task_cancel(
        _task_req(tasks.state, OWNER_ID, f"/api/tasks/{WAITING}/cancel")
    )
    assert resp.status == 200, resp.text
    assert tasks.admission.cancelled == [WAITING]
    await asyncio.sleep(0)
    assert tasks.store.state_of(WAITING) == model.CANCELLED


@pytest.mark.asyncio
async def test_a_refused_task_action_reads_no_body(tasks):
    """Gated before the body read, so a refusal costs nothing."""
    req = _task_req(tasks.state, NON_OWNER_ID, f"/api/tasks/{WAITING}")
    req.json = AsyncMock(side_effect=AssertionError("a refused caller's body was read"))  # type: ignore[method-assign]
    resp = await tasks_mod.api_task_action(req)
    assert resp.status == 403

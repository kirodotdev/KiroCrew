"""Apps learn that the global lesson store changed through ``lessons.changed``.

``kiro_crew.lesson_events`` owns the signal and its closed vocabulary. The
``/api/lessons`` routes and consolidation raise it only after the default store
actually changed, never for a write into a named store, and the payload carries no
lesson text. The gateway observer marshals it onto the serving loop and broadcasts the
frame, which the WebSocket gate delivers to an app token only under the ``lessons``
declaration. The last class pins the read side an app relies on: an app token whose
manifest declares ``/api/lessons`` and ``/api/skills`` passes the real auth middleware
on both, and the lessons route resolves it to the default store.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from body_stream_helpers import attach_body

from kiro_crew import lesson_events
from kiro_crew.history import ConversationLog
from kiro_crew.history_consolidation import HistoryConsolidator
from kiro_crew.learn import LessonStore
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult


@pytest.fixture
def frames():
    """Capture every signal raised while the test runs; restore no hook after."""
    seen: list[dict] = []
    lesson_events.set_lessons_changed_hook(seen.append)
    yield seen
    lesson_events.set_lessons_changed_hook(None)


class TestSignal:
    def test_payload_carries_only_the_closed_vocabulary(self):
        assert lesson_events.lessons_changed_payload("added", "consolidation", 3) == {
            "action": "added",
            "source": "consolidation",
            "count": 3,
        }
        with pytest.raises(ValueError):
            lesson_events.lessons_changed_payload("rewritten", "user_explicit")
        with pytest.raises(ValueError):
            lesson_events.lessons_changed_payload("added", "import")

    @pytest.mark.parametrize(
        ("outcome", "superseded", "want"),
        [
            ("inserted", False, "added"),
            ("inserted", True, "added"),
            ("enriched", False, "updated"),
            ("unchanged", False, None),
            ("deduped", False, None),
            ("refused", False, None),
            # A declining write that still deleted the rows it superseded.
            ("deduped", True, "removed"),
        ],
    )
    def test_write_outcome_maps_to_a_change(self, outcome, superseded, want):
        assert lesson_events.lesson_write_action(outcome, superseded) == want

    def test_no_observer_and_a_failing_observer_are_both_silent(self):
        lesson_events.set_lessons_changed_hook(None)
        lesson_events.emit_lessons_changed("added", "user_explicit")

        def boom(_payload: dict) -> None:
            raise RuntimeError("observer down")

        lesson_events.set_lessons_changed_hook(boom)
        try:
            lesson_events.emit_lessons_changed("removed", "user_explicit")
        finally:
            lesson_events.set_lessons_changed_hook(None)


@pytest.mark.asyncio
class TestRoutes:
    def _request(self, body: dict):
        request = MagicMock()
        state = MagicMock()
        state.conversation_log = ConversationLog()
        state._background_tasks = set()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        attach_body(request, body)
        return request, state

    async def _create(self, result, silo: str = ""):
        from kiro_crew.dashboard.handlers import cron

        request, state = self._request({"rule": "pin every dependency", "category": "tool"})
        vs = MagicMock()
        vs.algorithm_version = "v1"
        vs.embed_lesson.return_value = [0.1] * 4
        vs.find_contradiction_candidates.return_value = []
        vs.write_lesson.return_value = result
        memory = MagicMock(vector_store=vs)
        with (
            patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(silo, None))),
            patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
            patch.object(cron, "_get_memory", return_value=memory),
            patch.object(cron.ContextBuilder, "get_memory_for", return_value=memory),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
            patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
        ):
            resp = await cron.api_lessons_create(request)
        assert resp.status == 200
        return json.loads(resp.text)

    async def test_an_insert_into_the_global_store_is_announced(self, frames):
        body = await self._create(LessonWriteResult(LessonWriteOutcome.INSERTED))
        assert body["outcome"] == "inserted"
        assert frames == [{"action": "added", "source": "user_explicit", "count": 1}]

    async def test_an_enrichment_is_an_update(self, frames):
        await self._create(LessonWriteResult(LessonWriteOutcome.ENRICHED))
        assert frames == [{"action": "updated", "source": "user_explicit", "count": 1}]

    async def test_a_write_that_changed_nothing_is_silent(self, frames):
        await self._create(LessonWriteResult(LessonWriteOutcome.UNCHANGED))
        await self._create(LessonWriteResult(LessonWriteOutcome.REFUSED, "injection_blocked"))
        assert frames == []

    async def test_a_write_into_a_named_store_is_silent(self, frames):
        await self._create(LessonWriteResult(LessonWriteOutcome.INSERTED), silo="crew-a")
        assert frames == []

    async def test_the_payload_never_carries_the_rule(self, frames):
        await self._create(LessonWriteResult(LessonWriteOutcome.INSERTED))
        assert "pin every dependency" not in json.dumps(frames)

    async def test_a_refused_session_announces_nothing(self, frames):
        from kiro_crew.dashboard.handlers import cron

        request, _ = self._request({"rule": "pin every dependency"})
        with (
            patch.object(cron, "_is_restricted_session", return_value=True),
            patch.object(cron, "_sel"),
        ):
            resp = await cron.api_lessons_create(request)
        assert resp.status == 403
        assert frames == []

    async def _delete(self, tmp_path, rule: str, silo: str = ""):
        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.learn import Lesson

        store = LessonStore(base_dir=tmp_path)
        store.save(Lesson(ts="2026-01-01T00:00:00+00:00", rule="pin every dependency", category="tool"))
        request, state = self._request({"rule": rule})
        state.lessons = store
        with (
            patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(silo, None))),
            patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
            patch.object(
                cron.ContextBuilder, "get_memory_for", return_value=MagicMock(vector_store=None)
            ),
            patch.object(cron.ContextBuilder, "get_lessons_for", return_value=store),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
        ):
            resp = await cron.api_lessons_delete(request)
        return json.loads(resp.text)

    async def test_a_delete_that_removed_a_row_is_announced(self, frames, tmp_path):
        assert (await self._delete(tmp_path, "pin every"))["ok"] is True
        assert frames == [{"action": "removed", "source": "user_explicit", "count": 1}]

    async def test_a_delete_that_matched_nothing_is_silent(self, frames, tmp_path):
        assert (await self._delete(tmp_path, "no such rule"))["ok"] is False
        assert frames == []

    async def test_a_delete_in_a_named_store_is_silent(self, frames, tmp_path):
        assert (await self._delete(tmp_path, "pin every", silo="crew-a"))["ok"] is True
        assert frames == []


class TestConsolidation:
    def _consolidator(self, vector_store=None, lesson_store=None) -> HistoryConsolidator:
        return HistoryConsolidator(
            log=MagicMock(),
            memory=MagicMock(),
            sessions=None,
            vector_store=vector_store,
            lesson_store=lesson_store,
            migrated=True,
        )

    def test_global_vector_writes_are_announced_once_per_kind(self, frames):
        vs = MagicMock()
        vs.write_lesson.side_effect = [
            LessonWriteResult(LessonWriteOutcome.INSERTED),
            LessonWriteResult(LessonWriteOutcome.INSERTED),
            LessonWriteResult(LessonWriteOutcome.ENRICHED),
            LessonWriteResult(LessonWriteOutcome.DEDUPED, "substring"),
        ]
        c = self._consolidator(vector_store=vs)
        c._save_lessons([{"rule": f"rule {i}"} for i in range(4)])
        assert sorted(frames, key=lambda f: f["action"]) == [
            {"action": "added", "source": "consolidation", "count": 2},
            {"action": "updated", "source": "consolidation", "count": 1},
        ]

    def test_a_named_stores_vector_writes_are_silent(self, frames):
        own, silo = MagicMock(), MagicMock()
        silo.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.INSERTED)
        self._consolidator(vector_store=own)._save_lessons([{"rule": "a rule"}], silo, None)
        assert silo.write_lesson.called
        assert frames == []

    def test_global_jsonl_inserts_are_announced_and_no_ops_are_not(self, frames, tmp_path):
        store = LessonStore(base_dir=tmp_path)
        c = self._consolidator(lesson_store=store)
        c._save_lessons([{"rule": "check return codes"}], None, store)
        c._save_lessons([{"rule": "check return codes"}], None, store)  # unchanged
        assert frames == [{"action": "added", "source": "consolidation", "count": 1}]

    def test_a_named_stores_jsonl_inserts_are_silent(self, frames, tmp_path):
        (tmp_path / "own").mkdir()
        (tmp_path / "silo").mkdir()
        own = LessonStore(base_dir=tmp_path / "own")
        silo = LessonStore(base_dir=tmp_path / "silo")
        self._consolidator(lesson_store=own)._save_lessons([{"rule": "a rule"}], None, silo)
        assert len(silo.load_all()) == 1
        assert frames == []


@pytest.mark.asyncio
async def test_the_gateway_observer_broadcasts_on_the_serving_loop() -> None:
    from kiro_crew.dashboard import server

    state = MagicMock()
    state.serving_loop = asyncio.get_running_loop()
    try:
        server._register_lesson_change_hook(state)
        # Raised from a worker thread, as a consolidation pass raises it.
        await asyncio.to_thread(lesson_events.emit_lessons_changed, "added", "consolidation", 2)
        for _ in range(3):
            # flake-ok: bounded yield ticks for the call_soon_threadsafe callback the observer schedules
            await asyncio.sleep(0)
    finally:
        lesson_events.set_lessons_changed_hook(None)
    state.broadcast_ws.assert_called_once_with(
        "lessons.changed", {"action": "added", "source": "consolidation", "count": 2}
    )


class TestWebSocketScope:
    APP = "commons-under-test"

    @pytest.fixture(autouse=True)
    def _caches(self):
        from kiro_crew.dashboard import ws_event_scope

        for cache in (ws_event_scope._declared_cache, ws_event_scope._sel_last_audit):
            cache.clear()
        yield
        for cache in (ws_event_scope._declared_cache, ws_event_scope._sel_last_audit):
            cache.clear()

    def _allowed(self, declared: list[str]) -> bool:
        from kiro_crew.dashboard import ws_event_scope

        allowed = ws_event_scope.build_allowed_event_set(declared)
        # An installed, enabled app, so the decision is the scope and not cache timing.
        ws_event_scope._declared_cache[self.APP] = (time.monotonic(), True, allowed)
        return ws_event_scope.ws_event_allowed(
            lesson_events.LESSONS_CHANGED_EVENT,
            {"action": "added", "source": "user_explicit", "count": 1},
            app=self.APP,
            allowed_events=allowed,
            state=SimpleNamespace(_slots={}),
        )

    def test_an_app_that_declared_nothing_never_receives_it(self):
        assert self._allowed([]) is False

    def test_the_lessons_declaration_receives_it(self):
        assert self._allowed(["lessons"]) is True

    def test_an_unrelated_declaration_does_not(self):
        assert self._allowed(["skills"]) is False
        assert self._allowed(["notification:all"]) is False


@pytest.mark.asyncio
class TestAppTokenReachesTheLessonAndSkillReads:
    """An app token with ``permissions.api: ["/api/lessons", "/api/skills"]`` gets in.

    Both routes run behind the real middleware with the gateway's own mixed-internal
    path table, so ``/api/lessons`` takes the mixed-internal branch it takes in
    production. The handlers here record who reached them; the lessons route's store
    resolution is checked against the real resolver below.
    """

    APP = "commons-under-test"

    async def _get(self, monkeypatch, declared: tuple[str, ...], path: str) -> tuple[int, dict]:
        from kiro_crew.dashboard import token_auth
        from kiro_crew.dashboard.server_internal_paths import _MIXED_INTERNAL_API_PATHS

        monkeypatch.setattr(
            token_auth, "_app_api_allowlist", lambda name: declared if name == self.APP else ()
        )

        async def reached(request: web.Request) -> web.Response:
            return web.json_response(
                {"app": request.get("app"), "dashboard": request.get("is_dashboard_user")}
            )

        app = web.Application(
            middlewares=[
                token_auth.token_auth_middleware(mixed_internal_paths=_MIXED_INTERNAL_API_PATHS)
            ]
        )
        app.router.add_get("/api/lessons", reached)
        app.router.add_get("/api/skills", reached)
        credential = token_auth.generate_token(self.APP, ttl_seconds=300, app=self.APP)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path, params={"token": credential})
            text = await resp.text()
        return resp.status, (json.loads(text) if resp.status == 200 else {})

    @pytest.mark.parametrize("path", ["/api/lessons", "/api/skills"])
    async def test_a_declared_app_token_passes_the_middleware(self, monkeypatch, path):
        status, body = await self._get(monkeypatch, ("/api/lessons", "/api/skills"), path)
        assert status == 200
        assert body == {"app": self.APP, "dashboard": False}

    @pytest.mark.parametrize("path", ["/api/lessons", "/api/skills"])
    async def test_an_undeclared_app_token_is_refused(self, monkeypatch, path):
        status, _ = await self._get(monkeypatch, (), path)
        assert status == 403

    async def test_the_lessons_route_resolves_an_app_token_to_the_global_store(self):
        from kiro_crew.dashboard.handlers._shared import resolve_lesson_memory_store

        request = MagicMock()
        request.headers = {}
        request.get = {"app": self.APP, "is_dashboard_user": False}.get
        state = SimpleNamespace(conversation_log=ConversationLog())
        with patch(
            "kiro_crew.dashboard.handlers._shared_memory_stores.memory_startup_refusal",
            return_value=None,
        ):
            store, refusal = await resolve_lesson_memory_store(request, state, "lessons.list")
        assert (store, refusal) == ("", None)

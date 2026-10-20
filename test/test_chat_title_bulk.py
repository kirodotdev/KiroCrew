from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.dashboard import chat_title, chat_title_bulk, create_rate_limit
from kiro_crew.dashboard.chat_title_bulk import (
    api_chat_slots_generate_titles,
    api_chat_slots_generate_titles_status,
    plan_bulk_titles,
    run_bulk_titles,
)
from kiro_crew.dashboard.create_rate_limit import GENERATE_TITLE, allow_create, has_create_budget
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.members import DM_SLOT_MODE
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _fresh_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _slot(key: str, *, said: bool = True, origin: str = "auto", running: bool = False) -> _ChatSlot:
    slot = _ChatSlot(key)
    if said:
        slot.messages.append({"role": "user", "content": f"about {key}"})
    slot._title_origin = origin
    slot._turn_admission_reserved = running
    return slot


def _state(*slots: _ChatSlot) -> SimpleNamespace:
    return SimpleNamespace(_slots={s.key: s for s in slots}, _background_tasks=set())


class _Recorder:
    def __init__(self) -> None:
        self.applied: dict[str, str] = {}
        self.active = 0
        self.peak = 0
        self.on_generate = None

    async def generate(self, state, slot):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if self.on_generate is not None:
                self.on_generate(state, slot)
            return f"New {slot.key}"
        finally:
            self.active -= 1

    async def apply(self, state, slot, title, *, still_current=None):
        if still_current is not None and not still_current():
            return False
        self.applied[slot.key] = title
        return True


@pytest.fixture
def audit(monkeypatch) -> list[dict]:
    rows: list[dict] = []

    class _Sel:
        def log_api_access(self, **kwargs):
            rows.append({k: kwargs[k] for k in ("caller", "operation", "outcome", "source")})

    monkeypatch.setattr(chat_title_bulk, "sel", lambda: _Sel())
    return rows


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(chat_title_bulk, "_model_manual_title", rec.generate)
    monkeypatch.setattr(chat_title_bulk, "apply_regenerated_title", rec.apply)
    return rec


def _app(state, *, caller: str | None = "") -> web.Application:
    @web.middleware
    async def as_caller(request, handler):
        if caller is not None:
            request["app"] = caller
        return await handler(request)

    app = web.Application(middlewares=[as_caller])
    app["state"] = state
    app.router.add_post("/api/chat/generate-titles", api_chat_slots_generate_titles)
    app.router.add_get("/api/chat/generate-titles", api_chat_slots_generate_titles_status)
    return app


async def _drain(state) -> None:
    while state._background_tasks:
        await await_until(
            asyncio.gather(*list(state._background_tasks)), what="the bulk title run to finish"
        )


class TestPlan:
    def test_each_session_lands_in_one_bucket(self):
        state = _state(
            _slot("ready"),
            _slot("busy", running=True),
            _slot("named", origin="user"),
            _slot("blank", said=False),
            _slot("recent"),
        )
        assert allow_create(GENERATE_TITLE, "recent")

        plan = plan_bulk_titles(state, include_renamed=False)

        assert [s.key for s in plan.queued] == ["ready"]
        assert plan.skipped_running == ["busy"]
        assert plan.skipped_renamed == ["named"]
        assert plan.skipped_empty == ["blank"]
        assert plan.skipped_recent == ["recent"]
        assert plan.renamed == 1

    def test_include_renamed_queues_a_hand_set_title(self):
        state = _state(_slot("named", origin="user"))

        plan = plan_bulk_titles(state, include_renamed=True)

        assert [s.key for s in plan.queued] == ["named"]
        assert plan.skipped_renamed == []
        assert plan.renamed == 1

    def test_a_running_renamed_session_counts_as_running_only(self):
        state = _state(_slot("busy-named", origin="user", running=True))

        plan = plan_bulk_titles(state, include_renamed=False)

        assert plan.skipped_running == ["busy-named"]
        assert plan.skipped_renamed == []
        assert plan.renamed == 0

    def test_a_channel_session_answering_on_the_channel_path_counts_as_running(self):
        replying = _slot("replying")
        replying.linked_session_key = "slack:123.456"
        replying._linked_session_busy = lambda key: key == "slack:123.456"
        state = _state(replying)

        plan = plan_bulk_titles(state, include_renamed=False)

        assert plan.skipped_running == ["replying"]
        assert plan.queued == []

    def test_a_closing_session_is_left_out(self, monkeypatch):
        closing = _slot("closing")
        monkeypatch.setattr(type(closing), "is_closing", property(lambda s: s.key == "closing"))
        state = _state(closing, _slot("open"))

        plan = plan_bulk_titles(state, include_renamed=False)

        assert [s.key for s in plan.queued] == ["open"]

    @pytest.mark.parametrize("include_renamed", [False, True])
    def test_a_crewmate_thread_is_left_out(self, include_renamed):
        thread = _slot("thread", origin="member")
        thread.mode = DM_SLOT_MODE
        state = _state(thread, _slot("open"))

        plan = plan_bulk_titles(state, include_renamed=include_renamed)

        assert [s.key for s in plan.queued] == ["open"]
        assert plan.to_json(dry_run=True)["skipped_empty"] == []
        assert plan.renamed == 0


class TestRun:
    @pytest.mark.asyncio
    async def test_at_most_two_titles_generate_at_once(self, recorder):
        slots = [_slot(f"s{i}") for i in range(6)]
        state = _state(*slots)

        await run_bulk_titles(state, slots, include_renamed=False)

        assert recorder.peak == chat_title_bulk.BULK_TITLE_CONCURRENCY == 2
        assert set(recorder.applied) == {s.key for s in slots}

    @pytest.mark.asyncio
    async def test_a_rename_during_generation_keeps_the_rename(self, recorder):
        slot = _slot("s")
        state = _state(slot)

        def renamed(_state, target):
            target._title_epoch += 1

        recorder.on_generate = renamed

        await run_bulk_titles(state, [slot], include_renamed=False)

        assert recorder.applied == {}

    @pytest.mark.asyncio
    async def test_a_session_replaced_during_generation_keeps_its_title(self, recorder):
        slot = _slot("s")
        state = _state(slot)

        def replaced(state_, target):
            state_._slots[target.key] = _slot(target.key)

        recorder.on_generate = replaced

        await run_bulk_titles(state, [slot], include_renamed=False)

        assert recorder.applied == {}

    @pytest.mark.asyncio
    async def test_a_session_renamed_while_queued_is_skipped(self, recorder):
        slot = _slot("s")
        state = _state(slot)
        slot._title_origin = "user"

        await run_bulk_titles(state, [slot], include_renamed=False)

        assert recorder.applied == {}

    @pytest.mark.asyncio
    async def test_one_failure_does_not_stop_the_others(self, recorder, caplog):
        good, bad = _slot("good"), _slot("bad")
        state = _state(good, bad)

        def boom(_state, target):
            if target.key == "bad":
                raise RuntimeError("model down")

        recorder.on_generate = boom

        await run_bulk_titles(state, [bad, good], include_renamed=False)

        assert recorder.applied == {"good": "New good"}
        assert "Bulk title regeneration failed for slot bad" in caplog.text

    @pytest.mark.asyncio
    async def test_a_crewmate_thread_is_never_retitled(self, recorder):
        thread = _slot("thread", origin="member")
        thread.mode = DM_SLOT_MODE

        await run_bulk_titles(_state(thread), [thread], include_renamed=True)

        assert recorder.applied == {}

    @pytest.mark.asyncio
    async def test_a_model_failure_keeps_the_title_and_counts_as_failed(self, monkeypatch):
        async def model_down(*_args, **_kwargs):
            raise TimeoutError("titling model timed out")

        async def no_key(_key):
            return ""

        applied: list[str] = []

        async def apply(state, slot, title, *, still_current=None):
            applied.append(title)
            return True

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", model_down)
        monkeypatch.setattr(chat_title, "_ticket_key_if_enabled", no_key)
        monkeypatch.setattr(chat_title_bulk, "apply_regenerated_title", apply)
        slot = _slot("s")
        slot.title = "Old name"
        state = _state(slot)
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            await _drain(state)
            status = await (await client.get("/api/chat/generate-titles")).json()

        assert applied == []
        assert slot.title == "Old name"
        assert status["run"]["failed"] == 1
        assert status["run"]["failed_sessions"] == [{"key": "s", "title": "Old name"}]
        assert status["run"]["retitled"] == 0

    @pytest.mark.asyncio
    async def test_a_failed_sessions_title_is_redacted_like_the_sidebar(self, monkeypatch):
        async def model_down(*_args, **_kwargs):
            raise TimeoutError("titling model timed out")

        async def no_key(_key):
            return ""

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", model_down)
        monkeypatch.setattr(chat_title, "_ticket_key_if_enabled", no_key)
        slot = _slot("s")
        slot.title = "Deploy with AKIAIOSFODNN7EXAMPLE"
        state = _state(slot)
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            await _drain(state)
            status = await (await client.get("/api/chat/generate-titles")).json()

        [failed] = status["run"]["failed_sessions"]
        assert "AKIAIOSFODNN7EXAMPLE" not in failed["title"]
        assert slot.title == "Deploy with AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_an_empty_title_changes_nothing(self, recorder, monkeypatch):
        async def nothing(state, slot):
            return ""

        monkeypatch.setattr(chat_title_bulk, "_model_manual_title", nothing)
        slot = _slot("s")

        await run_bulk_titles(_state(slot), [slot], include_renamed=False)

        assert recorder.applied == {}


class TestRoute:
    @pytest.mark.asyncio
    async def test_dry_run_reports_the_plan_and_spends_nothing(self, recorder):
        state = _state(_slot("a"), _slot("named", origin="user"))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/generate-titles", json={"dry_run": True})
            body = await resp.json()

        assert resp.status == 200
        assert body["dry_run"] is True
        assert body["queued"] == ["a"]
        assert body["skipped_renamed"] == ["named"]
        assert body["renamed"] == 1
        assert has_create_budget(GENERATE_TITLE, "a")
        assert state._background_tasks == set()
        assert recorder.applied == {}

    @pytest.mark.asyncio
    async def test_apply_retitles_and_spends_each_sessions_budget(self, recorder):
        state = _state(_slot("a"), _slot("b"))
        async with TestClient(TestServer(_app(state))) as client:
            first = await (await client.post("/api/chat/generate-titles", json={})).json()
            await _drain(state)
            second = await (await client.post("/api/chat/generate-titles", json={})).json()

        assert sorted(first["queued"]) == ["a", "b"]
        assert recorder.applied == {"a": "New a", "b": "New b"}
        assert second["queued"] == []
        assert sorted(second["skipped_recent"]) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_budget_spent_between_plan_and_apply_is_skipped(self, recorder, monkeypatch):
        state = _state(_slot("a"))
        monkeypatch.setattr(chat_title_bulk, "allow_create", lambda verb, key: False)
        async with TestClient(TestServer(_app(state))) as client:
            body = await (await client.post("/api/chat/generate-titles", json={})).json()

        assert body["queued"] == []
        assert body["skipped_recent"] == ["a"]
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_rename_before_the_run_starts_stands(self, recorder, monkeypatch):
        slot = _slot("a")
        state = _state(slot)
        real = chat_title_bulk.run_bulk_titles

        async def renamed_first(state_, slots, **kwargs):
            slot._title_epoch += 1
            slot._title_origin = "user"
            await real(state_, slots, **kwargs)

        monkeypatch.setattr(chat_title_bulk, "run_bulk_titles", renamed_first)
        async with TestClient(TestServer(_app(state))) as client:
            body = await (
                await client.post("/api/chat/generate-titles", json={"include_renamed": True})
            ).json()
            await _drain(state)

        assert body["queued"] == ["a"]
        assert recorder.applied == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["some-app", None])
    async def test_only_a_dashboard_user_may_call_it(self, recorder, audit, caller):
        state = _state(_slot("a"))
        async with TestClient(TestServer(_app(state, caller=caller))) as client:
            resp = await client.post("/api/chat/generate-titles", json={})

        assert resp.status == 403
        assert has_create_budget(GENERATE_TITLE, "a")
        assert audit == [
            {
                "caller": caller or "unknown",
                "operation": "chat.slots_generate_titles",
                "outcome": "denied",
                "source": "app_isolation",
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["include_renamed", "dry_run"])
    async def test_a_non_boolean_flag_is_refused(self, recorder, field):
        state = _state(_slot("a"))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/generate-titles", json={field: "yes"})

        assert resp.status == 400
        assert has_create_budget(GENERATE_TITLE, "a")


class _PendingLoader:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.read_threads: list[int] = []

    def read(self, slot: _ChatSlot) -> tuple[list[dict], list[set[str]]]:
        self.read_threads.append(threading.get_ident())
        return list(self.rows), []

    def __call__(self, slot: _ChatSlot, messages: list[dict], claims: list[set[str]]) -> None:
        slot._messages = messages


def _pending_slot(key: str) -> tuple[_ChatSlot, _PendingLoader]:
    slot = _slot(key, said=False)
    rows = [{"role": "user", "content": f"about {key}"}]
    loader = _PendingLoader(rows)
    slot._lazy_window = loader
    slot._lazy_tail = list(rows)
    return slot, loader


class TestPendingWindow:
    def test_plan_reads_the_restore_tail_without_loading(self):
        slot, loader = _pending_slot("a")
        plan = plan_bulk_titles(_state(slot), include_renamed=False)
        assert [s.key for s in plan.queued] == ["a"]
        assert slot.window_pending
        assert loader.read_threads == []

    def test_plan_counts_a_pending_slot_with_no_conversation_as_empty(self):
        slot, loader = _pending_slot("a")
        slot._lazy_tail = []
        plan = plan_bulk_titles(_state(slot), include_renamed=False)
        assert plan.skipped_empty == ["a"]
        assert loader.read_threads == []

    @pytest.mark.asyncio
    async def test_run_loads_the_window_off_the_loop(self, recorder):
        slot, loader = _pending_slot("a")
        loop_thread = threading.get_ident()
        await run_bulk_titles(_state(slot), [slot], include_renamed=False)
        assert loader.read_threads and loop_thread not in loader.read_threads
        assert not slot.window_pending
        assert recorder.applied == {"a": "New a"}


class TestQueued:
    async def _hold_first_two(self, monkeypatch, recorder, change):
        release = asyncio.Event()
        started = asyncio.Event()
        seen: list[str] = []

        async def held(state, slot):
            seen.append(slot.key)
            if len(seen) == 2:
                started.set()
            if slot.key in {"a", "b"}:
                await release.wait()
            return f"New {slot.key}"

        monkeypatch.setattr(chat_title_bulk, "_model_manual_title", held)
        slots = [_slot("a"), _slot("b"), _slot("c")]
        state = _state(*slots)
        run = asyncio.ensure_future(run_bulk_titles(state, slots, include_renamed=True))
        try:
            await await_until(started, what="two titles generating")
            change(state, slots[2])
        finally:
            release.set()
            await await_until(run, what="the bulk title run to finish")
        return seen

    @pytest.mark.asyncio
    async def test_a_session_that_starts_running_while_queued_is_skipped(
        self, monkeypatch, recorder
    ):
        def start(_state, slot):
            slot._turn_admission_reserved = True

        seen = await self._hold_first_two(monkeypatch, recorder, start)

        assert seen == ["a", "b"]
        assert set(recorder.applied) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_a_session_whose_channel_turn_starts_while_queued_is_skipped(
        self, monkeypatch, recorder
    ):
        def reply(_state, slot):
            slot.linked_session_key = "slack:123.456"
            slot._linked_session_busy = lambda key: True

        seen = await self._hold_first_two(monkeypatch, recorder, reply)

        assert seen == ["a", "b"]
        assert set(recorder.applied) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_a_rename_while_queued_stands_even_with_renamed_included(
        self, monkeypatch, recorder
    ):
        def rename(_state, slot):
            slot._title_origin = "user"
            slot._title_epoch += 1

        seen = await self._hold_first_two(monkeypatch, recorder, rename)

        assert seen == ["a", "b"]
        assert set(recorder.applied) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_a_session_still_under_construction_is_left_alone(self, recorder):
        slot = _slot("s")
        state = _state(slot)
        state._slots_under_construction = {"s"}

        assert plan_bulk_titles(state, include_renamed=False).queued == []
        await run_bulk_titles(state, [slot], include_renamed=False)
        assert recorder.applied == {}


class TestProgress:
    @pytest.mark.asyncio
    async def test_status_counts_each_outcome_once_the_run_ends(self, recorder):
        good, bad, blank = _slot("good"), _slot("bad"), _slot("blank")
        bad.title = "Deploy triage"
        state = _state(good, bad, blank)

        def outcome(_state, target):
            if target.key == "bad":
                raise RuntimeError("model down")
            if target.key == "blank":
                target._title_epoch += 1

        recorder.on_generate = outcome
        async with TestClient(TestServer(_app(state))) as client:
            started = await (await client.post("/api/chat/generate-titles", json={})).json()
            await _drain(state)
            status = await (await client.get("/api/chat/generate-titles")).json()

        assert status["run"] == {
            "id": started["run_id"],
            "total": 3,
            "done": 3,
            "retitled": 1,
            "unchanged": 1,
            "failed": 1,
            "failed_sessions": [{"key": "bad", "title": "Deploy triage"}],
            "finished": True,
        }

    @pytest.mark.asyncio
    async def test_status_reports_a_run_in_flight(self, monkeypatch, recorder):
        release = asyncio.Event()

        async def held(state, slot):
            await release.wait()
            return f"New {slot.key}"

        monkeypatch.setattr(chat_title_bulk, "_model_manual_title", held)
        state = _state(_slot("a"), _slot("b"), _slot("c"))
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            mid = await (await client.get("/api/chat/generate-titles")).json()
            release.set()
            await _drain(state)

        assert mid["run"]["total"] == 3
        assert mid["run"]["done"] == 0
        assert mid["run"]["finished"] is False

    @pytest.mark.asyncio
    async def test_a_second_run_waits_for_the_first(self, monkeypatch, recorder):
        release = asyncio.Event()

        async def held(state, slot):
            await release.wait()
            return f"New {slot.key}"

        monkeypatch.setattr(chat_title_bulk, "_model_manual_title", held)
        state = _state(_slot("a"))
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            second = await client.post("/api/chat/generate-titles", json={})
            second_body = await second.json()
            preview = await client.post("/api/chat/generate-titles", json={"dry_run": True})
            release.set()
            await _drain(state)

        assert second.status == 409
        assert second_body["code"] == "bulk_titles_in_progress"
        assert preview.status == 200

    @pytest.mark.asyncio
    async def test_status_is_empty_before_any_run(self, recorder):
        async with TestClient(TestServer(_app(_state()))) as client:
            status = await (await client.get("/api/chat/generate-titles")).json()

        assert status == {"ok": True, "run": None}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", [None, "some-app"])
    async def test_only_a_dashboard_user_may_read_status(self, recorder, audit, caller):
        async with TestClient(TestServer(_app(_state(), caller=caller))) as client:
            resp = await client.get("/api/chat/generate-titles")

        assert resp.status == 403
        assert [(a["operation"], a["outcome"], a["source"]) for a in audit] == [
            ("chat.slots_generate_titles_status", "denied", "app_isolation")
        ]

    @pytest.mark.asyncio
    async def test_a_title_that_does_not_save_counts_as_failed(self, recorder, monkeypatch):
        async def unsaved(state, slot, title, *, still_current=None):
            return False

        monkeypatch.setattr(chat_title_bulk, "apply_regenerated_title", unsaved)
        state = _state(_slot("s"))
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            await _drain(state)
            status = await (await client.get("/api/chat/generate-titles")).json()

        assert status["run"]["failed"] == 1
        assert status["run"]["retitled"] == 0

    @pytest.mark.asyncio
    async def test_a_title_that_does_not_save_is_rolled_back_and_re_shown(self, monkeypatch):
        async def generate(state, slot):
            return "Unsaved title"

        async def unsaved(state, slot, title, *, still_current=None):
            slot.title = title
            slot._title_origin = "auto"
            slot._title_epoch += 1
            state.push_slot_title(slot.key, title)
            return False

        monkeypatch.setattr(chat_title_bulk, "_model_manual_title", generate)
        monkeypatch.setattr(chat_title_bulk, "apply_regenerated_title", unsaved)
        slot = _slot("s")
        slot.title = "Old title"
        state = _state(slot)
        pushed: list[tuple[str, str]] = []
        state.push_slot_title = lambda key, title, **kw: pushed.append((key, title))
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            await _drain(state)

        assert slot.title == "Old title"
        assert slot._title_origin == "auto"
        assert pushed == [("s", "Unsaved title"), ("s", "Old title")]

    @pytest.mark.asyncio
    async def test_an_unsaved_title_overtaken_by_a_rename_is_left_alone(self, monkeypatch):
        async def generate(state, slot):
            return "Unsaved title"

        async def unsaved(state, slot, title, *, still_current=None):
            slot.title = title
            slot._title_epoch += 1
            slot.title = "Renamed"
            slot._title_origin = "user"
            slot._title_epoch += 1
            return False

        monkeypatch.setattr(chat_title_bulk, "_model_manual_title", generate)
        monkeypatch.setattr(chat_title_bulk, "apply_regenerated_title", unsaved)
        slot = _slot("s")
        slot.title = "Old title"
        state = _state(slot)
        pushed: list[tuple[str, str]] = []
        state.push_slot_title = lambda key, title, **kw: pushed.append((key, title))
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={})
            await _drain(state)

        assert slot.title == "Renamed"
        assert slot._title_origin == "user"
        assert pushed == []

    @pytest.mark.asyncio
    async def test_a_started_run_writes_one_audit_row(self, recorder, audit):
        state = _state(_slot("a"), _slot("b"))
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/generate-titles", json={"dry_run": True})
            await client.post("/api/chat/generate-titles", json={})
            await _drain(state)

        assert [(a["operation"], a["outcome"], a["source"]) for a in audit] == [
            ("chat.slots_generate_titles", "success", "dashboard")
        ]


class TestRouteTable:
    @pytest.mark.asyncio
    async def test_the_bulk_routes_sit_outside_the_slot_namespace(self):
        from kiro_crew.dashboard import chat, routes

        app = web.Application()
        routes.register_all(app)

        async def handler(method: str, path: str):
            return (await app.router.resolve(make_mocked_request(method, path, app=app))).handler

        status = api_chat_slots_generate_titles_status
        assert await handler("GET", "/api/chat/generate-titles") is status
        assert await handler("POST", "/api/chat/generate-titles") is api_chat_slots_generate_titles
        assert await handler("GET", "/api/chat/slots/generate-titles") is chat.api_chat_slot_detail

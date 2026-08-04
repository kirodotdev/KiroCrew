"""Tests for PATCH /api/chat/slots/{slot}/plan-mode."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew import plan_mode
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat import api_chat_plan_approve, api_chat_slot_plan_mode
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _kc_path(rel: str) -> str:
    """Absolute path to *rel* inside the repo, independent of cwd."""
    import pathlib as _p

    return str(_p.Path(__file__).resolve().parents[1] / rel)


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_patch("/api/chat/slots/{slot}/plan-mode", api_chat_slot_plan_mode)
    app.router.add_post("/api/chat/slots/{slot}/plan-approve", api_chat_plan_approve)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    # No live children by default; the endpoint refuses while any are running.
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    # A real set: the approve endpoint adds its turn task here, and a MagicMock
    # attribute would swallow the add instead of holding a strong reference.
    state._background_tasks = set()
    return state


@pytest.fixture(autouse=True)
def _clean_registry():
    plan_mode.reset()
    yield
    plan_mode.reset()


@pytest.fixture(autouse=True)
def _as_owner():
    """Present every request as the dashboard owner unless a test says otherwise.

    Both endpoints are owner-only: they take a slot name from the caller and act
    on it, so an app or a non-owner dashboard session could otherwise disarm
    another slot's gate or start its implementation turn. The test client carries
    no auth identity, so without this the happy-path cases would all 403. The
    denial paths are asserted explicitly in TestPlanModeAuthorization.
    """
    with patch(
        "kiro_crew.dashboard.chat_folders.is_owner_dashboard_request",
        return_value=True,
    ):
        yield


class TestPlanModeEndpoint:
    @pytest.mark.asyncio
    async def test_enable(self):
        slot = _ChatSlot("test")
        assert slot.plan_mode is False
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": True}
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True, "plan_mode": True}
                assert slot.plan_mode is True
                state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_enable_arms_the_gate_immediately(self):
        # The registry must not wait for the next turn: the user expects the
        # gate to be live the moment the toggle flips.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.patch("/api/chat/slots/test/plan-mode", json={"plan_mode": True})
        assert plan_mode.is_active("dashboard:test") is True

    @pytest.mark.asyncio
    async def test_disable_disarms_the_gate(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        plan_mode.activate("dashboard:test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": False}
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True, "plan_mode": False}
        assert slot.plan_mode is False
        assert plan_mode.is_active("dashboard:test") is False

    @pytest.mark.asyncio
    async def test_missing_field_defaults_to_off(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch("/api/chat/slots/test/plan-mode", json={})
                assert resp.status == 200
                assert slot.plan_mode is False

    @pytest.mark.asyncio
    async def test_non_boolean_rejected(self):
        # A truthy string must not silently arm or disarm a security gate.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": "yes"}
                )
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_plan_mode"
                assert slot.plan_mode is False

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nope/plan-mode", json={"plan_mode": True})
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/test/plan-mode",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_refused_while_running(self):
        slot = _ChatSlot("test")
        # A MagicMock task reads as done() truthy; slot.running needs a real
        # pending future.
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        assert slot.running
        state = _mock_state(slot)
        try:
            with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
                async with TestClient(TestServer(_make_app(state))) as client:
                    resp = await client.patch(
                        "/api/chat/slots/test/plan-mode", json={"plan_mode": True}
                    )
                    assert resp.status == 409
                    assert (await resp.json())["code"] == "session_running"
                    assert slot.plan_mode is False
        finally:
            slot.task.cancel()
            try:
                await slot.task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_save_forced_so_resumed_sessions_persist(self):
        # Metadata-only mutations do not mark the slot dirty, so without
        # force=True the resumed-session guard silently drops the write.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop") as saver:
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.patch("/api/chat/slots/test/plan-mode", json={"plan_mode": True})
        assert saver.call_args.kwargs.get("force") is True


class TestPlanModeSerialization:
    def test_exposed_on_the_slot_payload(self):
        slot = _ChatSlot("test")
        assert slot.to_dict()["plan_mode"] is False
        slot.plan_mode = True
        assert slot.to_dict()["plan_mode"] is True


class TestPlanModePersistence:
    """Real round trip: endpoint → metadata line on disk → restored slot."""

    @pytest.mark.asyncio
    async def test_persists_on_a_resumed_session(self, tmp_path, monkeypatch):
        # Metadata-only mutations do not mark the slot dirty, so the
        # resumed-count guard in _save_slot_to_history drops the write unless
        # the handler passes force=True. Same regression as pin and folder.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("planslot")
        slot.append("user", "old message")
        slot.drain()
        slot._resumed_count = len(slot.messages)

        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/planslot/plan-mode", json={"plan_mode": True}
            )
            assert resp.status == 200

        path = tmp_path / "dashboard_planslot.jsonl"
        assert path.exists(), "plan_mode save must reach disk on resumed session"
        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta.get("plan_mode") is True, (
            "plan_mode was silently dropped on a resumed session — "
            "force=True must bypass the _resumed_count guard"
        )

    @pytest.mark.asyncio
    async def test_persists_on_an_empty_session(self, tmp_path, monkeypatch):
        """A slot the user has not typed into yet must still persist the gate.

        `_save_slot_to_history` returned on an empty message window BEFORE
        consulting `force`, so arming plan mode on a fresh slot never reached
        disk. `_run_chat` re-syncs the gate from this metadata, so after a restart
        the session came back with plan mode OFF while the user believed it was
        on -- a security flag failing OPEN. Sibling of the resumed-session case
        above, and it affects pin / folder / tag on an empty slot too.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("emptyslot")
        assert not slot.messages, "precondition: the slot has no messages"

        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/emptyslot/plan-mode", json={"plan_mode": True}
            )
            assert resp.status == 200

        path = tmp_path / "dashboard_emptyslot.jsonl"
        assert path.exists(), (
            "plan_mode save must reach disk even with no messages -- otherwise a "
            "restart silently disarms the gate"
        )
        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta.get("plan_mode") is True, meta

    def test_the_empty_window_guard_still_exists_for_unforced_saves(self):
        # An untouched slot must not create a history file just because something
        # called the saver; only a FORCED save proceeds.
        import inspect

        src = inspect.getsource(chat_persistence._save_slot_to_history)
        assert "if not window and not force:" in src

    @pytest.mark.asyncio
    async def test_absent_from_metadata_when_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("planslot")
        slot.append("user", "old message")
        slot.drain()
        slot._resumed_count = len(slot.messages)

        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/planslot/plan-mode", json={"plan_mode": False}
            )
            assert resp.status == 200

        meta = json.loads(
            (tmp_path / "dashboard_planslot.jsonl").read_text(encoding="utf-8").split("\n")[0]
        )
        assert "plan_mode" not in meta

    def test_restored_from_metadata_on_resume(self, tmp_path, monkeypatch):
        # The gate must survive a gateway restart: a planning session that came
        # back with writes re-armed would be the worst possible failure mode.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        (tmp_path / "dashboard_restored.jsonl").write_text(
            json.dumps({"_type": "metadata", "plan_mode": True}) + "\n"
            + json.dumps({"_type": "message", "role": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )
        slot = chat_persistence._rehydrate_slot_from_history(state, "restored")
        assert slot is not None
        assert slot.plan_mode is True

    def test_absent_metadata_leaves_the_gate_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        (tmp_path / "dashboard_plain.jsonl").write_text(
            json.dumps({"_type": "metadata"}) + "\n"
            + json.dumps({"_type": "message", "role": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )
        slot = chat_persistence._rehydrate_slot_from_history(state, "plain")
        assert slot is not None
        assert slot.plan_mode is False


class TestPlanModeInFlightChildren:
    """A fire-and-forget sub-agent outlives the parent turn.

    A child inherits plan mode only at ITS start, so arming the gate while
    children are already running would leave them ungated for the rest of their
    run — and slot.running is False in that window.
    """

    @pytest.mark.asyncio
    async def test_refused_while_subagents_running(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.subagents.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": True}
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "subagents_running"
                assert slot.plan_mode is False

    @pytest.mark.asyncio
    async def test_allowed_when_no_children(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": True}
                )
                assert resp.status == 200


class TestPlanModeSessionKey:
    """The endpoint must arm the key the runner actually enforces on."""

    @pytest.mark.asyncio
    async def test_linked_slot_arms_its_linked_key(self):
        # A cron- or workflow-driven slot runs under the linked key, not
        # dashboard:<slot>. Arming the wrong string would be a silent no-op.
        slot = _ChatSlot("test")
        slot.linked_session_key = "cron:job-7"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": True}
                )
                assert resp.status == 200
        assert plan_mode.is_active("cron:job-7") is True
        assert plan_mode.is_active("dashboard:test") is False

    @pytest.mark.asyncio
    async def test_plain_slot_arms_the_dashboard_key(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.patch("/api/chat/slots/test/plan-mode", json={"plan_mode": True})
        assert plan_mode.is_active("dashboard:test") is True


class TestPlanApprove:
    """The one-click handoff: clear the gate AND start the implementation turn.

    Two steps is where users got stuck — saying "go ahead" while the gate is
    still armed just earns another refusal.
    """

    @pytest.mark.asyncio
    async def test_clears_the_gate_and_starts_a_turn(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        plan_mode.activate("dashboard:test")
        state = _mock_state(slot)
        state._background_tasks = set()
        state.broadcast_ws = MagicMock()
        started: list[str] = []

        async def _fake_run(_state, _slot, message, **_kw):
            started.append(message)

        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), \
                patch("kiro_crew.dashboard.chat_folders._run_chat", _fake_run):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/plan-approve")
                assert resp.status == 200
                body = await resp.json()
                assert body["plan_mode"] is False and body["started"] is True
                if slot.task:
                    await slot.task

        assert slot.plan_mode is False
        assert plan_mode.is_active("dashboard:test") is False
        assert started and "implement" in started[0].lower()

    @pytest.mark.asyncio
    async def test_disarms_before_the_turn_starts(self):
        # If the gate were still armed when the turn began, the first tool call
        # of the implementation would be denied by the gate being lifted.
        slot = _ChatSlot("test")
        slot.plan_mode = True
        plan_mode.activate("dashboard:test")
        state = _mock_state(slot)
        state._background_tasks = set()
        state.broadcast_ws = MagicMock()
        seen: list[bool] = []

        async def _fake_run(_state, _slot, _message, **_kw):
            seen.append(plan_mode.is_active("dashboard:test"))

        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), \
                patch("kiro_crew.dashboard.chat_folders._run_chat", _fake_run):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post("/api/chat/slots/test/plan-approve")
                if slot.task:
                    await slot.task
        assert seen == [False], "the gate must be lifted before the turn runs"

    @pytest.mark.asyncio
    async def test_refused_when_not_planning(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/plan-approve")
            assert resp.status == 400
            assert (await resp.json())["code"] == "not_in_plan_mode"

    @pytest.mark.asyncio
    async def test_refused_while_running(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        state = _mock_state(slot)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/plan-approve")
                assert resp.status == 409
                assert (await resp.json())["code"] == "session_running"
                assert slot.plan_mode is True
        finally:
            slot.task.cancel()
            try:
                await slot.task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/plan-approve")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"


class TestPlanModeAuthorization:
    """Both endpoints are owner-only.

    Route scope is not authorization here: the middleware's app-scope check only
    confirms the ROUTE is in the calling app's manifest allowlist, not that the
    caller owns the slot it names. plan-approve is the sharper end -- it starts
    an implementation turn, which by design runs the write tools plan mode was
    holding back.
    """

    @staticmethod
    def _with_app_identity(app):
        @web.middleware
        async def _mw(request, handler):
            request["app"] = "some-third-party-app"
            return await handler(request)

        app.middlewares.append(_mw)
        return app

    @pytest.mark.asyncio
    async def test_app_identity_refused_on_toggle(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        app = self._with_app_identity(_make_app(state))
        async with TestClient(TestServer(app)) as client:
            r = await client.patch(
                "/api/chat/slots/test/plan-mode", json={"plan_mode": True}
            )
            assert r.status == 403
            assert (await r.json())["code"] == "app_not_permitted"
        # The gate state was not touched.
        assert slot.plan_mode is False

    @pytest.mark.asyncio
    async def test_app_identity_refused_on_approve(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        state = _mock_state(slot)
        app = self._with_app_identity(_make_app(state))
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/chat/slots/test/plan-approve", json={})
            assert r.status == 403
        # Still armed: an app cannot start another slot's implementation turn.
        assert slot.plan_mode is True

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_session_refused(self):
        # A dashboard session credential is also minted for every allowed Slack
        # user and carries an EMPTY app identity, so an app-only check would let
        # it straight past.
        slot = _ChatSlot("test")
        slot.plan_mode = True
        state = _mock_state(slot)
        app = _make_app(state)
        with patch(
            "kiro_crew.dashboard.chat_folders.is_owner_dashboard_request",
            return_value=False,
        ):
            async with TestClient(TestServer(app)) as client:
                r = await client.patch(
                    "/api/chat/slots/test/plan-mode", json={"plan_mode": False}
                )
                assert r.status == 403
                assert (await r.json())["code"] == "not_owner"
                r2 = await client.post("/api/chat/slots/test/plan-approve", json={})
                assert r2.status == 403
        assert slot.plan_mode is True


class TestPlanApproveRefusesLiveSubagents:
    """Approval starts the implementation turn, so live children must be done.

    ``slot.running`` alone is False while a fire-and-forget child is still
    executing. Worse, approval DISARMS the gate first, so those children would
    keep the plan gate they inherited while the parent had already left plan
    mode -- the two halves of one session disagreeing about whether writes are
    allowed. The toggle endpoint already refuses on this; approval is the
    sharper case.
    """

    @pytest.mark.asyncio
    async def test_refused_while_a_child_is_running(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        state = _mock_state(slot)
        state.subagents.running_agents_for = MagicMock(return_value=["agent-1"])
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/chat/slots/test/plan-approve", json={})
            assert r.status == 409
            assert (await r.json())["code"] == "subagents_running"
        # Gate untouched: no half-disarmed session.
        assert slot.plan_mode is True

    @pytest.mark.asyncio
    async def test_allowed_once_children_are_done(self):
        slot = _ChatSlot("test")
        slot.plan_mode = True
        state = _mock_state(slot)
        state.subagents.running_agents_for = MagicMock(return_value=[])
        app = _make_app(state)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", new=AsyncMock()), \
                patch("kiro_crew.dashboard.chat_folders._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/api/chat/slots/test/plan-approve", json={})
                assert r.status == 200
        assert slot.plan_mode is False

    @pytest.mark.asyncio
    async def test_the_child_check_uses_the_linked_session_key(self):
        """A cron/workflow-driven slot runs children under its LINKED key."""
        slot = _ChatSlot("test")
        slot.plan_mode = True
        slot.linked_session_key = "cron:nightly"
        state = _mock_state(slot)
        seen = []

        def _running(key):
            seen.append(key)
            return []

        state.subagents.running_agents_for = MagicMock(side_effect=_running)
        app = _make_app(state)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", new=AsyncMock()), \
                patch("kiro_crew.dashboard.chat_folders._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(app)) as client:
                await client.post("/api/chat/slots/test/plan-approve", json={})
        assert "cron:nightly" in seen, seen


class TestPlanApproveKeepsTheSlotGuardAtomic:
    """No ``await`` between the ``slot.running`` guard and ``slot.task = ...``.

    ``ChatSlot.enqueue_or_run_prompt`` documents the invariant this rests on: the
    ``running`` check and the ``slot.task`` assignment run synchronously on the
    loop, so two concurrent callers cannot both observe an idle slot. An ``await``
    inside that window reopens it -- a message submitted while the approval was
    persisting would start a second turn on the same slot, and whichever assigned
    ``slot.task`` last would orphan the other turn.

    Persisting the disarm is therefore deferred until after the task is
    registered. Structural rather than behavioural because the race needs a
    precisely timed second request; the invariant is "no await in this window",
    which the AST states directly.
    """

    def _approve_fn(self):
        import ast
        import pathlib as _pl

        src = _pl.Path(
            _kc_path("src/kiro_crew/dashboard/chat_folders.py")
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
                "plan_approve" in node.name
            ):
                return node
        raise AssertionError("plan-approve handler not found")

    def test_no_await_between_the_running_guard_and_task_registration(self):
        import ast

        fn = self._approve_fn()

        guard_line = None
        task_assign_line = None
        for node in ast.walk(fn):
            if (
                guard_line is None
                and isinstance(node, ast.Attribute)
                and node.attr == "running"
                and isinstance(node.value, ast.Name)
                and node.value.id == "slot"
            ):
                guard_line = node.lineno
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "task"
                and isinstance(node.value, ast.Name)
                and node.value.id == "slot"
                and isinstance(getattr(node, "ctx", None), ast.Store)
            ):
                task_assign_line = node.lineno

        assert guard_line is not None, "no slot.running guard in the handler"
        assert task_assign_line is not None, "no slot.task assignment in the handler"
        assert guard_line < task_assign_line

        offenders = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Await) and guard_line < n.lineno < task_assign_line
        ]
        assert not offenders, (
            "await(s) at lines "
            f"{offenders} sit between the slot.running guard (line {guard_line}) "
            f"and slot.task assignment (line {task_assign_line}); a concurrent "
            "submit can start a second turn on this slot"
        )

    def test_the_disarm_itself_still_precedes_the_turn(self):
        # The other half of the ordering: the gate must be lifted (in memory)
        # BEFORE the turn starts, or the implementation's first tool call is
        # denied. Guards against "fixing" the race by moving the disarm instead.
        import ast

        fn = self._approve_fn()
        disarm_line = None
        task_assign_line = None
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "plan_mode"
                and isinstance(node.value, ast.Name)
                and node.value.id == "slot"
                and isinstance(getattr(node, "ctx", None), ast.Store)
            ):
                disarm_line = node.lineno
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "task"
                and isinstance(node.value, ast.Name)
                and node.value.id == "slot"
                and isinstance(getattr(node, "ctx", None), ast.Store)
            ):
                task_assign_line = node.lineno
        assert disarm_line is not None and task_assign_line is not None
        assert disarm_line < task_assign_line

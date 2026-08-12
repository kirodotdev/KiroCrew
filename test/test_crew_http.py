"""HTTP-level integration tests for Crew Mode.

Drives the REAL api_chat handler with a crew-mode slot and a CrewOrchestrator
whose decision LLM is stubbed (deterministic actions) and whose subagent
manager is mocked at the spawn/continue boundary. Proves the full pipeline:
create crew slot via HTTP → interleaved messages → instant acks in the
transcript → topics spawned/routed → completion → forwarded result with
attribution → held message auto-dispatch. The pieces NOT covered here (real
LLM routing quality, real sub-session execution) are exercised by the live
manual protocol in the PR description.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

import kiro_crew.crew_chat as crew_mod
from kiro_crew.crew_chat import CrewOrchestrator


@pytest.fixture(autouse=True)
def _isolate_crew_dir(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(crew_mod, "data_home", lambda: tmp_path / "crewdata")


def _spawn_info(run_id: str, done: bool = False, error: str = "") -> MagicMock:
    info = MagicMock()
    info.id = run_id
    info.done = done
    info.error = error
    return info


def _crew_state(tmp_path):  # type: ignore[no-untyped-def]
    state = _make_state(tmp_path)
    subagents = MagicMock()
    subagents.spawn = MagicMock(side_effect=[_spawn_info("rA"), _spawn_info("rB")])
    subagents.continue_conversation = MagicMock(return_value=_spawn_info("rC"))
    state.subagents = subagents
    state.crew = CrewOrchestrator(state=state, sessions=state.sessions, subagents=subagents)
    state.broadcast_ws = MagicMock()
    return state


async def _until(cond, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """Yield to the loop until *cond* holds, or fail with a real deadline.

    Crew ingest schedules its decision pass as a background task, so a caller
    that wants to observe the result has to wait for it. A fixed `sleep(0.05)`
    encodes a guess about how fast the host is: it passed on Linux for months and
    failed on a Windows runner the moment the store gained one extra file read.
    Waiting on the CONDITION removes the guess and still fails loudly if the work
    genuinely never happens.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition still false after {timeout}s")
        await asyncio.sleep(0.01)


def _mode_app(state):  # type: ignore[no-untyped-def]
    """`_make_app` is a trimmed test app that omits the mode route (the real one
    is registered at server.py:2538). Add just that route."""
    from kiro_crew.dashboard.chat_folders import api_chat_slot_mode
    app = _make_app(state)
    app.router.add_patch("/api/chat/slots/{slot}/mode", api_chat_slot_mode)
    return app


class TestCrewHttpFlow:
    @pytest.mark.asyncio
    async def test_interleaved_messages_full_flow(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _crew_state(tmp_path)
        slot = state.get_or_create_slot("crew1", mode="crew")
        assert slot.mode == "crew"

        # Deterministic decision LLM: msg1 -> new topic; msg2 -> new topic;
        # msg3 -> route to topic rA (idle at that point).
        decisions = [
            '{"actions": [{"do": "spawn", "msg_id": "%s", "title": "task A"}]}',
            '{"actions": [{"do": "spawn", "msg_id": "%s", "title": "task B"}]}',
            '{"actions": [{"do": "route", "msg_id": "%s", "topic_id": "rA"}]}',
        ]
        seen: list[str] = []

        async def fake_oneliner(sessions, prompt, **kw):  # type: ignore[no-untyped-def]
            import json as _j
            state_part = prompt.split("STATE:", 1)[1]
            snap = _j.loads(state_part[state_part.index("{"):state_part.rindex("}") + 1])
            pending = [e["msg_id"] for e in snap.get("queue", [])]
            tmpl = decisions[len(seen)]
            seen.append(pending[0])
            return tmpl % pending[0]

        async with TestClient(TestServer(_make_app(state))) as client:
            with patch.object(crew_mod, "run_bg_oneliner", side_effect=fake_oneliner):
                r1 = await client.post("/api/chat", json={"slot": "crew1", "message": "do task A"})
                assert (await r1.json()).get("crew") is True
                await _until(lambda: state.crew.owns("rA"))
                r2 = await client.post("/api/chat", json={"slot": "crew1", "message": "do task B"})
                assert r2.status == 200
                await _until(lambda: state.crew.owns("rB"))

        st = state.crew._store("crew1")
        # Two topics spawned, owned, running
        assert state.crew.owns("rA") and state.crew.owns("rB")
        assert {t["title"] for t in st.topics} == {"task A", "task B"}
        # Transcript got: 2 user messages + 2 acks (assistant)
        roles = [m.get("role") for m in slot.messages]
        assert roles.count("user") == 2
        assert roles.count("assistant") >= 2
        # Both messages were spawned with keep=True and the summary contract
        for call in state.subagents.spawn.call_args_list:
            assert call.kwargs["keep"] is True
            assert "<<<SUMMARY" in call.args[0]

    @pytest.mark.asyncio
    async def test_completion_forwards_and_dispatches_held(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _crew_state(tmp_path)
        slot = state.get_or_create_slot("crew1", mode="crew")
        crew = state.crew
        st = crew._store("crew1")
        e = st.add_msg("original ask")
        e["state"] = "accepted"
        e["run_id"] = "rA"
        t = st.add_topic("rA", "rA", "task A", e["msg_id"])
        held = st.add_msg("follow up")
        held["state"] = "held"
        t["held"] = [held["msg_id"]]
        crew._owned["rA"] = "crew1"

        info = _spawn_info("rA", done=True)
        info.result = "work work <<<SUMMARY task A finished: everything green >>>"
        await crew.on_subagent_done(info)   # delivered synchronously, one message

        # Forward landed in the transcript with attribution to the origin msg
        bodies = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        fwd = next(b for b in bodies if "task A finished" in b)
        assert "↩ re:" in fwd and "original ask" in fwd
        # Held follow-up auto-dispatched via continue on the same conversation
        state.subagents.continue_conversation.assert_called_once()
        assert t["active_run_id"] == "rC"
        assert crew.owns("rC")

    @pytest.mark.asyncio
    async def test_non_crew_slot_unaffected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A default slot must not route through the crew pipeline."""
        state = _crew_state(tmp_path)
        state.get_or_create_slot("plain", mode="")
        ingest = AsyncMock()
        with patch.object(state.crew, "ingest", ingest):
            async with TestClient(TestServer(_make_app(state))) as client:
                with patch("kiro_crew.dashboard.chat_runner._run_chat", new=AsyncMock()):
                    await client.post("/api/chat", json={"slot": "plain", "message": "hi"})
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_endpoint_rejects_bad_mode(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_create

        state = _crew_state(tmp_path)
        app = _make_app(state)
        app.router.add_post("/api/chat/slots", api_chat_slot_create)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/chat/slots", json={"mode": "bogus"})
            assert r.status == 400
            r2 = await client.post("/api/chat/slots", json={"mode": "crew"})
            assert r2.status in (200, 201)

    @pytest.mark.asyncio
    async def test_crew_ingest_refusal_is_not_a_200(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """`ingest` declines an app-owned session. Reporting 200 anyway told the
        caller its message was accepted for work that will never run — and an API
        caller never sees the transcript note the refusal posts."""
        state = _crew_state(tmp_path)
        slot = state.get_or_create_slot("appslot")
        slot.mode = "crew"
        slot._app = "owner-app"
        async with TestClient(TestServer(_make_app(state))) as client:
            r = await client.post(
                "/api/chat", json={"slot": "appslot", "message": "do a thing"})
            body = await r.text()
        assert r.status == 409, f"a refused crew ingress reported {r.status}"
        assert "crew_app_session_unsupported" in body

    @pytest.mark.asyncio
    async def test_mode_switch_refuses_a_foreign_apps_slot(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The mode decides which execution model a session runs under, so this
        endpoint needs the same app-ownership rule `api_chat_send` and
        `api_chat_slot_create` apply. Without it an app holding `/api/chat` could
        list a foreign slot and switch it into crew mode.

        Calls the handler directly with a stub request: the app identity arrives
        as a request KEY set by upstream middleware, and faking that through a
        TestServer says more about middleware wiring than about this branch.
        """
        from kiro_crew.dashboard.chat_folders import api_chat_slot_mode

        state = _crew_state(tmp_path)
        slot = state.get_or_create_slot("victim")
        slot._app = ""            # a dashboard-owned session

        class _Req:
            def __init__(self, caller_app: str) -> None:
                self.app = {"state": state}
                self.match_info = {"slot": "victim"}
                self._caller = caller_app

            def get(self, key, default=None):  # type: ignore[no-untyped-def]
                return self._caller if key == "app" else default

            async def json(self):  # type: ignore[no-untyped-def]
                return {"mode": "crew"}

        resp = await api_chat_slot_mode(_Req("intruder-app"))  # type: ignore[arg-type]
        assert resp.status == 404, "an app switched a session it does not own"
        assert json.loads(resp.text or "{}")["code"] == "slot_not_found"
        assert state._slots["victim"].mode == ""

        # The dashboard itself (no app identity) still switches it. The busy
        # guard has to answer honestly here: `_crew_state` hands us a MagicMock
        # manager whose `has_pending_work_for` is truthy, which reads as "work in
        # flight" and would 409 for the wrong reason.
        state.subagents.has_pending_work_for = MagicMock(return_value=False)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop",
                   new=AsyncMock()):
            ok = await api_chat_slot_mode(_Req(""))  # type: ignore[arg-type]
        assert ok.status == 200
        assert state._slots["victim"].mode == "crew"

    @pytest.mark.asyncio
    async def test_crew_entry_points_refuse_an_unstorable_name(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A slot whose key folds to nothing but dots has no crew store, so
        `CrewStore` raises — on the first MESSAGE, as an unhandled 500, and again
        on every message after it. Both doors into crew mode answer instead."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_create

        state = _crew_state(tmp_path)
        app = _mode_app(state)
        app.router.add_post("/api/chat/slots", api_chat_slot_create)
        async with TestClient(TestServer(app)) as client:
            created = await client.post(
                "/api/chat/slots", json={"name": "..", "mode": "crew"})
            assert created.status == 400
            assert (await created.json())["code"] == "crew_unsupported_slot"
            # The same shape of name is a perfectly legal PLAIN slot, so the
            # switch is the other door and has to hold the line too. Uses "..."
            # rather than "..": both are unstorable, but only "..." survives URL
            # path normalization on the way to the handler.
            plain = await client.post("/api/chat/slots", json={"name": "..."})
            assert plain.status in (200, 201)
            switched = await client.patch("/api/chat/slots/.../mode",
                                          json={"mode": "crew"})
            assert switched.status == 400
            assert (await switched.json())["code"] == "crew_unsupported_slot"
            assert state._slots["..."].mode == ""


class TestModeSwitchGuard:
    """Mode must not flip while crew work is in flight.

    `slot.running` stays false for the whole life of a crew session, because the
    work executes in SUBAGENTS — so the pre-existing guard alone let the mode
    change mid-flight and interleave two execution models in one session.
    """

    @pytest.mark.asyncio
    async def test_refuses_while_crew_has_live_work(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _crew_state(tmp_path)
        state.subagents.has_pending_work_for = MagicMock(return_value=False)
        slot = state.get_or_create_slot("crew1", mode="crew")
        # The premise: crew work lives in subagents, so the slot itself never
        # looks busy — which is exactly why slot.running cannot be the guard.
        assert not slot.running
        state.crew.has_live_work = AsyncMock(return_value=True)
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/crew1/mode", json={"mode": ""})
            assert r.status == 409
        assert slot.mode == "crew"                # unchanged

    @pytest.mark.asyncio
    async def test_allows_when_crew_is_idle(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _crew_state(tmp_path)
        state.subagents.has_pending_work_for = MagicMock(return_value=False)
        slot = state.get_or_create_slot("crew1", mode="crew")
        assert not slot.running
        state.crew.has_live_work = AsyncMock(return_value=False)
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/crew1/mode", json={"mode": ""})
            assert r.status == 200
        assert slot.mode == ""

    @pytest.mark.asyncio
    async def test_a_non_crew_slot_is_not_gated_on_crew_work(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Regression: the guard first asked the orchestrator on EVERY switch, so
        # sessions that cannot have crew work (default / orchestrator mode) were
        # refused too. Only a slot already IN crew mode can have such work — one
        # entering it has none yet by construction.
        state = _crew_state(tmp_path)
        state.subagents.has_pending_work_for = MagicMock(return_value=False)
        slot = state.get_or_create_slot("plain1", mode="")
        state.crew.has_live_work = AsyncMock(return_value=True)
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/plain1/mode", json={"mode": "crew"})
            assert r.status == 200
        assert slot.mode == "crew"
        state.crew.has_live_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stand_in_crew_attribute_does_not_refuse(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # `state.crew` is gated by isinstance, matching gateway.py's own check on
        # this attribute. An identity check (`is not None`) would accept any
        # stand-in object, whose `has_live_work` then answers with something
        # truthy and refuses a switch that is perfectly fine — the same
        # truthiness-on-a-double mistake an earlier round of this PR already hit.
        state = _crew_state(tmp_path)
        state.subagents.has_pending_work_for = MagicMock(return_value=False)
        slot = state.get_or_create_slot("crew1", mode="crew")
        state.crew = MagicMock()          # not a CrewOrchestrator
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/crew1/mode", json={"mode": ""})
            assert r.status == 200
        assert slot.mode == ""

    @pytest.mark.asyncio
    async def test_entering_crew_is_refused_while_a_plain_subagent_runs(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # The asymmetry the previous guard missed: it gated the whole check on
        # `slot.mode == "crew"`, so ENTERING crew mode skipped it — while a
        # plain-chat subagent was running on that very slot. Its completion
        # follows the default `_run_chat` path, so the session would end up
        # mixing main-agent and crew execution.
        state = _crew_state(tmp_path)
        slot = state.get_or_create_slot("plain2", mode="")
        state.subagents.has_pending_work_for = MagicMock(return_value=True)
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/plain2/mode", json={"mode": "crew"})
            assert r.status == 409
        assert slot.mode == ""
        state.subagents.has_pending_work_for.assert_called_with("dashboard:plain2")

    @pytest.mark.asyncio
    async def test_background_work_check_fails_closed(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _crew_state(tmp_path)
        state.get_or_create_slot("plain3", mode="")
        state.subagents.has_pending_work_for = MagicMock(side_effect=RuntimeError("down"))
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/plain3/mode", json={"mode": "crew"})
            assert r.status == 409

    @pytest.mark.asyncio
    async def test_fails_closed_when_the_orchestrator_raises(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # An orchestrator that cannot answer is not permission to flip the mode.
        state = _crew_state(tmp_path)
        state.subagents.has_pending_work_for = MagicMock(return_value=False)
        slot = state.get_or_create_slot("crew1", mode="crew")
        assert not slot.running
        state.crew.has_live_work = AsyncMock(side_effect=RuntimeError("store unreadable"))
        async with TestClient(TestServer(_mode_app(state))) as client:
            r = await client.patch("/api/chat/slots/crew1/mode", json={"mode": ""})
            assert r.status == 409
        assert slot.mode == "crew"

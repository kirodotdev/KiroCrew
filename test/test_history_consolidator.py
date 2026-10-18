"""Tests for the background history consolidator's memory and lesson writes.

Covers ``HistoryConsolidator``: its tool-free LLM turn, the offset that advances
only on success, the writes kept off the event loop, ``consolidate_session`` and
``consolidate_now``, the value guard, the source a consolidated memory is written
under, the embedder breaker, and the lesson scope and applies tier. Skill
detection and auto-skills are in ``test_history_auto_skills.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log

from kiro_crew.history import _CONSOLIDATION_THRESHOLD, ConversationLog, HistoryConsolidator

# ---------------------------------------------------------------------------
# Tests for consolidation offset only advances on success
# ---------------------------------------------------------------------------


class TestConsolidationToolPolicy:
    """The background consolidation LLM turn must run tool-free on ALL providers.

    kiro scopes the kirocrew-lite session to tools:[] via set_mode, but the
    Claude Code backend skips set_mode and injects the full kirocrew-core/cron
    toolset (auto-approved). To keep parity — and prevent a background turn from
    firing side-effecting tools like send_message/learn_add — _call_llm must
    reject all tools regardless of provider.
    """

    @pytest.mark.asyncio
    async def test_call_llm_rejects_tools(self):
        from kiro_crew.llm_helpers import ToolApprovalPolicy

        provider = MagicMock()
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(provider, False, False))
        sessions.release = MagicMock()
        sessions.recycle_background = AsyncMock()

        consolidator = HistoryConsolidator(log=MagicMock(), memory=MagicMock(), sessions=sessions)

        captured = {}

        async def _fake_scj(prov, prompt, *, approval_policy=None, **kw):
            captured["approval_policy"] = approval_policy
            return {"ok": True}

        # Patch where it is USED — history.py imports the symbol at module top.
        with patch("kiro_crew.history.stream_and_collect_json", side_effect=_fake_scj):
            result = await consolidator._call_llm("some prompt")

        assert result == {"ok": True}
        assert captured["approval_policy"] == ToolApprovalPolicy.REJECT_ALL, (
            "background consolidation turn must reject tools (parity with kiro's "
            "tool-free lite agent; CC injects the full toolset otherwise)"
        )


class TestConsolidationOffset:
    """Verify _prefs_offset only advances when _consolidate succeeds."""

    def _make_consolidator(self, msg_count=_CONSOLIDATION_THRESHOLD):
        log = MagicMock()
        log._read_messages = MagicMock(return_value=[{}] * msg_count)
        # The off-loop row count maybe_consolidate runs in a worker thread.
        log._count_messages.return_value = msg_count
        # A fresh span is eligible; maybe_consolidate's pre-check reads this.
        log.consolidation_retry_state.return_value = (0, 0.0)
        return HistoryConsolidator(log=log, memory=MagicMock(), sessions=None)

    def test_offset_advances_on_success(self):
        """When _consolidate succeeds, _prefs_offset should advance."""
        c = self._make_consolidator()

        async def run():
            with patch.object(c, "_consolidate", new_callable=AsyncMock):
                c.maybe_consolidate("k")
                # flake-ok: pre-existing unbounded await of the consolidation tasks, moved verbatim; tracked for a de-flake pass
                await asyncio.gather(*c._tasks, return_exceptions=True)

        asyncio.run(run())
        assert c._prefs_offset.get("k") == _CONSOLIDATION_THRESHOLD

    def test_offset_does_not_advance_on_failure(self):
        """When _consolidate raises, _prefs_offset must NOT advance."""
        c = self._make_consolidator()

        async def run():
            with patch.object(c, "_consolidate", new_callable=AsyncMock) as m:
                m.side_effect = RuntimeError("LLM failed")
                c.maybe_consolidate("k")
                # flake-ok: pre-existing unbounded await of the consolidation tasks, moved verbatim; tracked for a de-flake pass
                await asyncio.gather(*c._tasks, return_exceptions=True)

        asyncio.run(run())
        assert c._prefs_offset.get("k", 0) == 0

    def test_retry_after_failure(self):
        """After failure, next call retries (offset still 0)."""
        c = self._make_consolidator()

        async def run():
            with patch.object(c, "_consolidate", new_callable=AsyncMock) as m:
                m.side_effect = RuntimeError("timeout")
                c.maybe_consolidate("k")
                # flake-ok: pre-existing unbounded await of the consolidation tasks, moved verbatim; tracked for a de-flake pass
                await asyncio.gather(*c._tasks, return_exceptions=True)
                c._running.discard("k")

                m.side_effect = None
                c.maybe_consolidate("k")
                # flake-ok: pre-existing unbounded await of the consolidation tasks, moved verbatim; tracked for a de-flake pass
                await asyncio.gather(*c._tasks, return_exceptions=True)

        asyncio.run(run())
        assert c._prefs_offset["k"] == _CONSOLIDATION_THRESHOLD


class TestConsolidationDoesNotBlockLoop:
    """Structured-memory writes embed synchronously (blocking urllib to Ollama).

    _consolidate runs as an asyncio.create_task on the event loop thread, so it
    MUST offload _write_structured_memory to a worker thread — otherwise a slow
    or stalled embedding endpoint freezes the whole gateway loop (heartbeats,
    Slack, dashboard) and can trip the faulthandler hard-kill. Regression guard
    for the loop-stall crash traced to embeddings.py urlopen on the loop thread.
    """

    @pytest.mark.asyncio
    async def test_structured_memory_write_runs_off_loop_thread(self):
        import threading

        loop_thread_id = threading.get_ident()
        write_thread_id: dict[str, int] = {}

        log = lease_aware_log()
        log.snapshot_for_consolidation.return_value = ([{"role": "user", "content": "hi"}], 1, 0, 0)
        log.consolidation_retry_state.return_value = (0, 0.0)

        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""

        vector_store = MagicMock()
        vector_store.get_all_semantic.return_value = []

        c = HistoryConsolidator(
            log=log,
            memory=memory,
            sessions=None,
            vector_store=vector_store,
            migrated=True,
        )

        def _fake_write(result, key, vector_store=None, **_):
            # Simulate the blocking embed call; record the executing thread.
            write_thread_id["id"] = threading.get_ident()

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock) as llm,
            patch.object(c, "_write_structured_memory", side_effect=_fake_write),
        ):
            llm.return_value = {"episodic": [{"text": "x" * 20}]}
            await c._consolidate("k", include_history=False)

        assert write_thread_id.get("id") is not None, "_write_structured_memory was not called"
        assert write_thread_id["id"] != loop_thread_id, (
            "_write_structured_memory ran on the event loop thread — a blocking "
            "embed here freezes the gateway loop. It must be offloaded via "
            "asyncio.to_thread()."
        )

    @pytest.mark.asyncio
    async def test_save_lessons_runs_off_loop_thread(self):
        """_save_lessons calls write_lesson which embeds via blocking urllib.

        Regression guard: 22475ceb offloaded _write_structured_memory but missed
        _save_lessons 15 lines below — the observed ~26s loop stall that causes
        learn_add MCP timeouts (collateral damage from the blocked event loop).
        """
        import threading

        loop_thread_id = threading.get_ident()
        save_thread_id: dict[str, int] = {}

        log = lease_aware_log()
        log.snapshot_for_consolidation.return_value = ([{"role": "user", "content": "hi"}], 1, 0, 0)
        log.consolidation_retry_state.return_value = (0, 0.0)

        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""

        vector_store = MagicMock()
        vector_store.get_all_semantic.return_value = []
        vector_store.write_lesson.return_value = True

        c = HistoryConsolidator(
            log=log,
            memory=memory,
            sessions=None,
            vector_store=vector_store,
            migrated=True,
        )

        original_save = c._save_lessons

        def _instrumented_save(raw, vector_store=None, lesson_store=None, **_):
            save_thread_id["id"] = threading.get_ident()
            original_save(raw)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock) as llm,
            patch.object(c, "_write_structured_memory"),
            patch.object(c, "_save_lessons", side_effect=_instrumented_save),
        ):
            llm.return_value = {
                "lessons": [{"rule": "always check return codes", "category": "tool"}],
            }
            await c._consolidate("k", include_history=True)

        assert save_thread_id.get("id") is not None, "_save_lessons was not called"
        assert save_thread_id["id"] != loop_thread_id, (
            "_save_lessons ran on the event loop thread — write_lesson embeds via "
            "blocking urllib here, freezing the gateway loop. It must be offloaded "
            "via asyncio.to_thread()."
        )

    def test_save_lessons_caps_oversized_list(self):
        """The LLM lessons array is capped like semantic/episodic: each
        write_lesson can perform up to 6 blocking embeds, so an uncapped list
        would occupy a worker thread for minutes."""
        from kiro_crew.vector_memory import _MAX_LESSONS_PER_CONSOLIDATION

        vector_store = MagicMock()
        vector_store.write_lesson.return_value = True

        c = HistoryConsolidator(
            log=MagicMock(),
            memory=MagicMock(),
            sessions=None,
            vector_store=vector_store,
            migrated=True,
        )
        oversized = [
            {"rule": f"lesson number {i}", "category": "tool"}
            for i in range(_MAX_LESSONS_PER_CONSOLIDATION * 3)
        ]
        c._save_lessons(oversized)

        assert vector_store.write_lesson.call_count == _MAX_LESSONS_PER_CONSOLIDATION


class TestConsolidateSession:
    """Tests for the public consolidate_session() method (session-end trigger)."""

    @pytest.mark.asyncio
    async def test_cold_transcript_is_never_parsed_on_the_event_loop(self, tmp_path):
        """Idle expiry calls this on the loop after the parsed-transcript cache
        has expired, so every transcript read it needs must run in a worker
        thread: a full parse there stalls every other gateway task."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        key = "dashboard:chat-cold-expiry"
        for i in range(5):
            conv_log.append(key, "user", f"m{i}")
        conv_log._msg_cache.clear()  # what the idle TTL leaves at session expiry

        loop_thread = threading.get_ident()
        reads: list[tuple[str, int]] = []
        projection = conv_log._read_projection
        real_read, real_count = projection._read_messages, projection._count_messages

        def spy_read(k, *a, **kw):
            reads.append(("read", threading.get_ident()))
            return real_read(k, *a, **kw)

        def spy_count(k, *a, **kw):
            reads.append(("count", threading.get_ident()))
            return real_count(k, *a, **kw)

        with (
            patch.object(projection, "_read_messages", side_effect=spy_read),
            patch.object(projection, "_count_messages", side_effect=spy_count),
            patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as run,
        ):
            consolidator.consolidate_session(key)
            on_loop = [kind for kind, tid in reads if tid == loop_thread]
            assert on_loop == [], f"transcript read on the event loop: {on_loop}"
            # flake-ok: pre-existing unbounded await of the consolidation tasks, moved verbatim; tracked for a de-flake pass
            await asyncio.gather(*list(consolidator._tasks))

        assert reads, "premise: the eligibility check read the transcript"
        assert all(tid != loop_thread for _kind, tid in reads), reads
        run.assert_awaited_once_with(key, include_history=True)

    @pytest.mark.asyncio
    async def test_repeated_expiry_starts_one_read_per_transcript(self, tmp_path):
        """A second call while the first read is in flight starts no second
        full parse of the same file, so a large transcript is never resident
        twice because of this path."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        key = "dashboard:chat-twice"
        conv_log.append(key, "user", "hello")

        entered, release = threading.Event(), threading.Event()
        calls: list[str] = []
        real = consolidator._session_end_eligibility

        def gated(k):
            calls.append(k)
            entered.set()
            assert release.wait(timeout=60), "eligibility worker was not released"
            return real(k)

        with (
            patch.object(consolidator, "_session_end_eligibility", side_effect=gated),
            patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as run,
        ):
            try:
                consolidator.consolidate_session(key)
                assert await asyncio.to_thread(
                    entered.wait, timeout=60
                ), "eligibility worker did not enter"
                consolidator.consolidate_session(key)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*list(consolidator._tasks)), timeout=60)

        assert calls == [key]
        run.assert_awaited_once_with(key, include_history=True)
        assert not consolidator._counting

    @pytest.mark.asyncio
    async def test_consolidate_session_fires_for_eligible(self, tmp_path):
        """consolidate_session triggers consolidation for sessions with messages."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=2,
        )

        # Seed a session with tool calls
        for i in range(5):
            conv_log.append("dashboard:chat-expire", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "did stuff",
                "new_skill": {
                    "slug": "expire-triggered-skill",
                    "description": "Skill from session expire",
                    "triggers": "expire, test",
                    "procedure_md": "## Steps\n1. Do thing\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            consolidator.consolidate_session("dashboard:chat-expire")
            # Let the task run
            # flake-ok: pre-existing 100 ms wait for the scheduled consolidation task to run, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 100 ms wait for the scheduled consolidation task to run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.1)
            # Drain pending tasks
            for t in list(consolidator._tasks):
                # flake-ok: pre-existing unbounded await of the consolidation task, moved verbatim; tracked for a de-flake pass
                await t

        auto = skills.list_auto_skills()
        assert len(auto) == 1
        assert auto[0]["key"] == "auto/expire-triggered-skill"

    @pytest.mark.asyncio
    async def test_consolidate_session_skips_empty(self, tmp_path):
        """consolidate_session does nothing for sessions with no messages."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)

        # No messages for this key
        consolidator.consolidate_session("dashboard:chat-empty")
        # No tasks should be created
        assert len(consolidator._tasks) == 0

    @pytest.mark.asyncio
    async def test_consolidate_session_skips_already_running(self, tmp_path):
        """consolidate_session doesn't double-trigger for the same session."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-dup", "user", "hello")

        # Simulate already running
        consolidator._running.add("dashboard:chat-dup")
        consolidator.consolidate_session("dashboard:chat-dup")
        # No new tasks created
        assert len(consolidator._tasks) == 0

    @pytest.mark.asyncio
    async def test_consolidate_session_skips_sensitive(self, tmp_path):
        """consolidate_session skips sessions that touched sensitive paths."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        # Seed a session with a sensitive tool call
        conv_log.append(
            "dashboard:chat-sensitive",
            "assistant",
            "reading secrets",
            tools=["cat .aws/credentials"],
        )

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as run:
            consolidator.consolidate_session("dashboard:chat-sensitive")
            # flake-ok: pre-existing unbounded await of the consolidation tasks, moved verbatim; tracked for a de-flake pass
            await asyncio.gather(*list(consolidator._tasks))
        # Sensitive session skipped: the off-loop check ran no pass
        run.assert_not_awaited()
        assert len(consolidator._tasks) == 0
        assert "dashboard:chat-sensitive" not in consolidator._running

    @pytest.mark.asyncio
    async def test_consolidate_session_on_done_logs_exception(self, tmp_path):
        """_on_done callback logs warning when consolidation task raises."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-fail", "user", "hello")

        with patch.object(
            consolidator, "_consolidate", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            consolidator.consolidate_session("dashboard:chat-fail")
            # flake-ok: pre-existing 100 ms wait for the scheduled consolidation task to run, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 100 ms wait for the scheduled consolidation task to run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.1)
            for t in list(consolidator._tasks):
                try:
                    # flake-ok: pre-existing unbounded await of the consolidation task, moved verbatim; tracked for a de-flake pass
                    await t
                except RuntimeError:
                    pass

        # Key should be removed from _running after _on_done fires
        assert "dashboard:chat-fail" not in consolidator._running

    @pytest.mark.asyncio
    async def test_consolidate_now_skips_sensitive(self, tmp_path):
        """consolidate_now skips sessions that touched sensitive paths."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append(
            "dashboard:chat-sens2", "assistant", "read .ssh/id_rsa", tools=["cat .ssh/id_rsa"]
        )

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as mock_consolidate:
            await consolidator.consolidate_now("dashboard:chat-sens2")
            mock_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidate_now_happy_path(self, tmp_path):
        """consolidate_now calls _consolidate for eligible sessions."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-ok", "user", "hello world")

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as mock_consolidate:
            await consolidator.consolidate_now("dashboard:chat-ok")
            mock_consolidate.assert_called_once_with("dashboard:chat-ok", include_history=True)

    @pytest.mark.asyncio
    async def test_consolidate_now_skips_empty(self, tmp_path):
        """consolidate_now does nothing for sessions with no unconsolidated messages."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as mock_consolidate:
            await consolidator.consolidate_now("dashboard:chat-nonexist")
            mock_consolidate.assert_not_called()


class TestConsolidationValueGuard:
    """A consolidation item whose 'value' the LLM omitted must not reach the store."""

    @staticmethod
    def _consolidator(store):
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        return HistoryConsolidator(
            log=MagicMock(),
            memory=memory,
            sessions=None,
            vector_store=store,
            migrated=True,
        )

    def _store(self, tmp_path):
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    def test_value_absent_item_does_not_clobber_existing_row(self, tmp_path) -> None:
        store = self._store(tmp_path)
        assert (
            store.set_semantic("project.alpha.status", "curated text", 1.0, "user_explicit") is None
        )
        c = self._consolidator(store)

        c._write_structured_memory(
            {"semantic": [{"key": "project.alpha.status", "confidence": 1.0}]}, "sess-1"
        )

        row = store.get_semantic("project.alpha.status")
        assert row["value_json"] == json.dumps("curated text")

    def test_explicit_none_value_item_does_not_clobber_existing_row(self, tmp_path) -> None:
        store = self._store(tmp_path)
        assert (
            store.set_semantic("project.alpha.status", "curated text", 1.0, "user_explicit") is None
        )
        c = self._consolidator(store)

        c._write_structured_memory(
            {"semantic": [{"key": "project.alpha.status", "value": None, "confidence": 1.0}]},
            "sess-1",
        )

        row = store.get_semantic("project.alpha.status")
        assert row["value_json"] == json.dumps("curated text")

    def test_value_absent_item_is_logged_and_counted(self, tmp_path, caplog) -> None:
        """The skip must be observable: layer 1 returns before set_semantic, so no event fires."""
        store = self._store(tmp_path)
        c = self._consolidator(store)

        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory(
                {"semantic": [{"key": "project.alpha.status", "confidence": 1.0}]}, "sess-1"
            )

        assert any(
            "skipped 'project.alpha.status'" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        ), "the omitted-value item was dropped without a per-item warning"
        assert any(
            "0 written, 0 deleted, 1 skipped" in r.getMessage() for r in caplog.records
        ), "the summary line did not report the skip"

    def test_value_absent_item_creates_no_row(self, tmp_path) -> None:
        store = self._store(tmp_path)
        c = self._consolidator(store)

        c._write_structured_memory(
            {"semantic": [{"key": "project.beta.status", "confidence": 1.0}]}, "sess-1"
        )

        assert store.get_semantic("project.beta.status") is None

    def test_well_formed_item_still_overwrites(self, tmp_path) -> None:
        """Negative control: the harness above can detect a clobber when one happens.

        The seeded row is a lower-confidence *consolidation* row rather than a
        ``user_explicit`` one, because consolidation is never allowed to overwrite
        ``user_explicit`` (see ``TestConsolidationDoesNotImpersonateUser``) — seeding one
        here would make this control pass for the wrong reason and stop detecting clobbers.
        """
        store = self._store(tmp_path)
        assert (
            store.set_semantic("project.alpha.status", "curated text", 0.85, "consolidation:sess-0")
            is None
        )
        c = self._consolidator(store)

        c._write_structured_memory(
            {"semantic": [{"key": "project.alpha.status", "value": "replaced", "confidence": 1.0}]},
            "sess-1",
        )

        row = store.get_semantic("project.alpha.status")
        assert row["value_json"] == json.dumps("replaced")

    def test_layer2_refusal_counted_apart_from_no_value_skip(self, tmp_path, caplog) -> None:
        """An empty-string value clears layer 1 and is refused at layer 2, not 'no value'."""
        store = self._store(tmp_path)
        c = self._consolidator(store)

        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory(
                {"semantic": [{"key": "project.alpha.status", "value": "", "confidence": 1.0}]},
                "sess-1",
            )

        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "0 written, 0 deleted, 0 skipped (no value), 1 refused" in m for m in msgs
        ), "the layer-2 refusal was still folded into the no-value skip count"
        assert any(
            "refused 'project.alpha.status'" in m and "value_empty" in m for m in msgs
        ), "the refusal did not name its reject cause"

    def test_refusal_names_the_actual_cause_not_a_constant(self, tmp_path, caplog) -> None:
        """A low-confidence refusal is not a missing value, so the label must follow the code."""
        store = self._store(tmp_path)
        c = self._consolidator(store)

        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory(
                {"semantic": [{"key": "project.alpha.status", "value": "v", "confidence": 0.1}]},
                "sess-1",
            )

        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "refused 'project.alpha.status'" in m and "low_confidence" in m for m in msgs
        ), "the cause was not read from the reject code"
        assert not any("value_empty" in m for m in msgs), "a non-empty value reported value_empty"
        assert any("0 skipped (no value), 1 refused" in m for m in msgs)


class TestConsolidationDoesNotImpersonateUser:
    """Consolidation writes under its own source, never under ``user_explicit``."""

    @staticmethod
    def _consolidator(store):
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        return HistoryConsolidator(
            log=MagicMock(),
            memory=memory,
            sessions=None,
            vector_store=store,
            migrated=True,
        )

    def _store(self, tmp_path):
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    def test_confident_item_does_not_overwrite_a_user_explicit_row(self, tmp_path) -> None:
        """A re-summarization at confidence 1.0 must lose the ``user_explicit`` conflict path."""
        store = self._store(tmp_path)
        assert (
            store.set_semantic("project.alpha.status", "curated text", 1.0, "user_explicit") is None
        )
        c = self._consolidator(store)

        c._write_structured_memory(
            {"semantic": [{"key": "project.alpha.status", "value": "shrunk", "confidence": 1.0}]},
            "sess-1",
        )

        row = store.get_semantic("project.alpha.status")
        assert row["value_json"] == json.dumps("curated text")
        assert row["source"] == "user_explicit"

    def test_confident_item_still_creates_a_new_key_under_consolidation_source(
        self, tmp_path
    ) -> None:
        """Demoting the source must not stop consolidation from writing its own keys."""
        store = self._store(tmp_path)
        c = self._consolidator(store)

        c._write_structured_memory(
            {"semantic": [{"key": "project.beta.status", "value": "fresh", "confidence": 1.0}]},
            "sess-1",
        )

        row = store.get_semantic("project.beta.status")
        assert row["value_json"] == json.dumps("fresh")
        assert row["source"] == "consolidation:sess-1"

    def test_v1_extracted_lesson_cannot_displace_user_lesson(self, tmp_path) -> None:
        taught = "Zebra crossings need beacons"
        inferred = "Submarine hatches demand orange lanterns for visibility"
        store = self._store(tmp_path)
        try:
            assert store.algorithm_version == "v1"
            store.embed_fn = lambda _text: [1.0, 0.0]
            assert store.write_lesson(taught, source="user_explicit")
            before = store.get_lessons()
            events = store.get_events()

            self._consolidator(store)._save_lessons([{"rule": inferred}])

            assert store.get_lessons() == before
            assert store.get_events() == events
            [lesson] = store.get_lessons()
            assert json.loads(lesson["value_json"])["rule"] == taught
            assert lesson["source"] == "user_explicit"
        finally:
            store.close()


class TestConsolidationEmbedBreaker:
    """One consolidation pass must not pay a slow embedder once per item.

    Every memory a pass writes embeds its text inline, so an embedder that is slow
    for the first row is slow for all of them — and each failed embed stores the
    same NULL-vector row the repair sweep would have filled anyway. The pass
    therefore measures its own write time and, past the budget, defers the
    remaining embeddings instead of re-paying the latency per item.
    """

    @staticmethod
    def _consolidator(store):
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        return HistoryConsolidator(
            log=MagicMock(),
            memory=memory,
            sessions=None,
            vector_store=store,
            migrated=True,
        )

    @staticmethod
    def _store(tmp_path):
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    @staticmethod
    def _episodes(count):
        return [
            {"text": f"episode {i}: the operator asked for a fresh status sweep", "importance": 0.5}
            for i in range(count)
        ]

    @staticmethod
    def _rows(store):
        return store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE is_deleted = 0"
        ).fetchall()

    def test_slow_embedder_is_attempted_once_and_every_row_is_still_written(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        """N items cost one embed attempt, not N."""
        from kiro_crew import history_consolidation

        store = self._store(tmp_path)
        calls = []

        def slow_embed(text):
            calls.append(text)
            # flake-ok: pre-existing 50 ms stall in the slow-embedder stub, past the patched budget, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms stall in the slow-embedder stub, past the patched budget, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
            raise TimeoutError("timed out")

        store.embed_fn = slow_embed
        monkeypatch.setattr(history_consolidation, "_EMBED_BUDGET_SECS_PER_PASS", 0.01)
        try:
            with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
                self._consolidator(store)._write_structured_memory(
                    {"episodic": self._episodes(5)}, "sess-1"
                )

            assert len(calls) == 1, (
                f"the pass kept embedding after the first overrun ({len(calls)} attempts); "
                "a degraded embedder is paid once per item again"
            )
            rows = self._rows(store)
            assert len(rows) == 5, "deferring the embedding must not drop the memory itself"
            assert all(row["embedding"] is None for row in rows)
            deferrals = [
                r
                for r in caplog.records
                if "embedding is deferred to the repair sweep" in r.getMessage()
            ]
            assert len(deferrals) == 1, (
                "the deferral must be logged once per pass, not once per row; got "
                f"{[r.getMessage() for r in deferrals]}"
            )
            assert deferrals[0].levelno == logging.WARNING
        finally:
            store.close()

    def test_healthy_embedder_embeds_every_row(self, tmp_path) -> None:
        """Control: an embedder that answers promptly never arms the latch."""
        store = self._store(tmp_path)
        calls = []

        def fast_embed(text):
            index = len(calls)
            calls.append(text)
            # Orthogonal per row: identical vectors would hit the similarity dedup
            # and reject rows this control needs written.
            vec = [0.0] * 8
            vec[index % 8] = 1.0
            return vec

        store.embed_fn = fast_embed
        try:
            self._consolidator(store)._write_structured_memory(
                {"episodic": self._episodes(5)}, "sess-1"
            )

            assert len(calls) == 5, "a healthy pass must still embed every row inline"
            rows = self._rows(store)
            assert len(rows) == 5
            assert all(row["embedding"] is not None for row in rows)
        finally:
            store.close()

    def test_both_tiers_share_one_budget_and_stop_after_the_first_overrun(
        self, tmp_path, monkeypatch
    ) -> None:
        """Every row a pass writes embeds inline, so both tiers latch together."""
        from kiro_crew import history_consolidation

        store = self._store(tmp_path)
        calls = []

        def slow_embed(text):
            calls.append(text)
            # flake-ok: pre-existing 50 ms stall in the slow-embedder stub, past the patched budget, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms stall in the slow-embedder stub, past the patched budget, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
            raise TimeoutError("timed out")

        store.embed_fn = slow_embed
        monkeypatch.setattr(history_consolidation, "_EMBED_BUDGET_SECS_PER_PASS", 0.01)
        keys = [f"project.p{i}.status" for i in range(4)]
        try:
            self._consolidator(store)._write_structured_memory(
                {
                    "semantic": [
                        {"key": k, "value": f"green {i}", "confidence": 0.9}
                        for i, k in enumerate(keys)
                    ],
                    "episodic": self._episodes(3),
                },
                "sess-1",
            )

            assert len(calls) == 1, (
                "a tier kept embedding after the pass had already overrun "
                f"({len(calls)} attempts across 4 semantic + 3 episodic rows)"
            )
            assert len(self._rows(store)) == 3, "deferral must not drop an episode"
            rows = [store.get_semantic(k) for k in keys]
            assert all(row is not None for row in rows), "deferral must not drop a fact"
            assert all(row["embedding"] is None for row in rows)
        finally:
            store.close()

    def test_a_deferred_episode_never_evicts_an_existing_memory(
        self, tmp_path, monkeypatch
    ) -> None:
        """A row admitted without dedup must not displace one it never compared.

        Deferring leaves the vector NULL, which skips the similarity dedup. On a
        legacy V1 store at its episodic cap the insert would then tombstone the
        lowest-importance row to make room for a possible paraphrase. The refusal
        is the cheaper loss: the transcript is still on disk, the evicted row is
        not.
        """
        from kiro_crew import history_consolidation
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db", episodic_max=2)
        store.init()
        assert store.algorithm_version == "v1"
        kept = ["the canary stage runs before production", "the release owner signs the ledger"]
        for text in kept:
            assert store.write_episodic(text=text, importance=0.9, source="user_explicit")

        calls = []

        def slow_embed(text):
            calls.append(text)
            # flake-ok: pre-existing 50 ms stall in the slow-embedder stub, past the patched budget, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms stall in the slow-embedder stub, past the patched budget, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
            raise TimeoutError("timed out")

        store.embed_fn = slow_embed
        monkeypatch.setattr(history_consolidation, "_EMBED_BUDGET_SECS_PER_PASS", 0.01)
        try:
            self._consolidator(store)._write_structured_memory(
                {
                    # One semantic write spends the budget, so every episode below
                    # is written deferred rather than only the ones after the first.
                    "semantic": [
                        {"key": "project.alpha.status", "value": "green", "confidence": 0.9}
                    ],
                    "episodic": self._episodes(3),
                },
                "sess-1",
            )

            surviving = {
                row["text"]
                for row in store.db.execute(
                    "SELECT text FROM episodic_memories WHERE is_deleted = 0"
                ).fetchall()
            }
            assert surviving == set(kept), (
                "a deferred episode displaced a memory it could not be compared "
                f"against; store now holds {surviving}"
            )
            assert len(calls) == 1
        finally:
            store.close()

    def test_a_deferred_semantic_row_is_left_for_the_repair_sweep(self, tmp_path) -> None:
        """The deferred vector is work the standing sweep can still see and finish."""
        store = self._store(tmp_path)
        store.embed_fn = lambda _text: [1.0, 0.0, 0.0, 0.0]
        try:
            assert (
                store.set_semantic(
                    "project.alpha.status",
                    "green",
                    0.9,
                    "consolidation:sess-1",
                    defer_embedding=True,
                )
                is None
            )

            row = store.get_semantic("project.alpha.status")
            assert row is not None and row["embedding"] is None
            assert (
                store.has_pending_embeddings()
            ), "a deferred row the repair sweep cannot see is a vector lost forever"
            # The returned count is episodic-only, so the repaired row itself is
            # the evidence the semantic sub-sweep ran.
            store.backfill_missing_embeddings(pace=False)
            assert store.get_semantic("project.alpha.status")["embedding"] is not None
        finally:
            store.close()


class TestConsolidationLessonScope:
    """Consolidation forwards a model-supplied ``repo_scope`` into the lesson write.

    ``write_lesson`` has accepted ``repo_scope`` all along, but ``_save_lessons``
    never passed it, so consolidation-written lessons could not be
    project-scoped -- and since the context gate drops out-of-scope lessons
    before ranking, the scope lever was unreachable for every consolidation
    write. These tests pin the forwarding on both write paths.
    """

    @staticmethod
    def _consolidator(store):
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        return HistoryConsolidator(
            log=MagicMock(),
            memory=memory,
            sessions=None,
            vector_store=store,
            migrated=True,
        )

    def _store(self, tmp_path):
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = lambda _text: [1.0, 0.0]
        return store

    @staticmethod
    def _jsonl_consolidator(lesson_store):
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        return HistoryConsolidator(
            log=MagicMock(),
            memory=memory,
            sessions=None,
            lesson_store=lesson_store,
            vector_store=None,
            migrated=True,
        )

    def test_model_supplied_scope_is_written_with_repo_scope(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [
                    {
                        "rule": "Run the scoped gate before pushing here",
                        "category": "tool",
                        "repo_scope": "src/kiro_crew",
                    }
                ]
            )

            [lesson] = store.get_lessons()
            value = json.loads(lesson["value_json"])
            assert value["repo_scope"] == "src/kiro_crew"
            assert lesson["source"] == "consolidation"
        finally:
            store.close()

    def test_absent_scope_still_writes_a_global_lesson(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [{"rule": "Prefer explicit timezones in timestamps"}]
            )

            [lesson] = store.get_lessons()
            value = json.loads(lesson["value_json"])
            # A global lesson stores NO scope key at all -- that absence is what
            # keeps it applying everywhere (see write_lesson's lesson_value).
            assert "repo_scope" not in value
            assert lesson["source"] == "consolidation"
        finally:
            store.close()

    def test_jsonl_fallback_forwards_scope_canonicalised(self, tmp_path) -> None:
        """The no-vector-store path stores the scope too, canonicalised by save()."""
        from kiro_crew.learn import LessonStore

        lesson_store = LessonStore(base_dir=tmp_path)
        c = self._jsonl_consolidator(lesson_store)

        c._save_lessons(
            [
                # Trailing slash: save() canonicalises before storing.
                {"rule": "Pin the vitest worker count here", "repo_scope": "src/kiro_crew/"},
                {"rule": "A rule with no scope stays global"},
            ]
        )

        by_rule = {le.rule: le for le in lesson_store.load_all()}
        assert by_rule["Pin the vitest worker count here"].repo_scope == "src/kiro_crew"
        assert by_rule["A rule with no scope stays global"].repo_scope is None

    def test_whitespace_only_scope_is_global_not_refused(self, tmp_path) -> None:
        """A whitespace-only scope is NO scope -- the lesson still lands, globally."""
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [{"rule": "Trailing spaces mean nothing here", "repo_scope": "   "}]
            )

            [lesson] = store.get_lessons()
            assert "repo_scope" not in json.loads(lesson["value_json"])
        finally:
            store.close()

    def test_non_string_scope_refuses_the_lesson_not_widens_it(self, tmp_path) -> None:
        """A present non-string scope must NOT slip past write_lesson's string-only
        guard and store the lesson globally -- that is the fail-open a scoped
        lesson must never take. The whole lesson is refused instead."""
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [{"rule": "Scoped to a list, somehow", "repo_scope": ["src/kiro_crew"]}]
            )

            assert store.get_lessons() == []
        finally:
            store.close()

    def test_inadmissible_scope_refused_on_jsonl_fallback_too(self, tmp_path) -> None:
        """LessonStore.save never checks admissibility, so the consolidation seam
        must -- an absolute path stored as a scope would render nowhere while
        suppressing the store (see project_scope.scope_is_admissible)."""
        from kiro_crew.learn import LessonStore

        lesson_store = LessonStore(base_dir=tmp_path)
        c = self._jsonl_consolidator(lesson_store)

        c._save_lessons([{"rule": "Scoped to an absolute path", "repo_scope": "/etc/passwd"}])

        assert lesson_store.load_all() == []

    def test_non_string_scope_refused_on_jsonl_fallback_too(self, tmp_path) -> None:
        """The silent-widening cell on the fallback path: a non-string scope must
        refuse the lesson there too, not canonicalise to a global write."""
        from kiro_crew.learn import LessonStore

        lesson_store = LessonStore(base_dir=tmp_path)
        c = self._jsonl_consolidator(lesson_store)

        c._save_lessons([{"rule": "Scoped to a list, somehow", "repo_scope": ["src/kiro_crew"]}])

        assert lesson_store.load_all() == []

    def test_inadmissible_scope_refuses_the_lesson_on_vector_path(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [{"rule": "Scoped to an absolute path", "repo_scope": "/etc/passwd"}]
            )

            assert store.get_lessons() == []
        finally:
            store.close()

    def test_refused_lesson_keeps_its_well_formed_siblings(self, tmp_path) -> None:
        """The per-item drop is a ``continue``, not an abort: one malformed scope
        must not take the rest of the batch with it."""
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [
                    {"rule": "Scoped to a list, somehow", "repo_scope": ["src/kiro_crew"]},
                    {"rule": "A clean global lesson survives the batch"},
                ]
            )

            [lesson] = store.get_lessons()
            assert (
                json.loads(lesson["value_json"])["rule"]
                == "A clean global lesson survives the batch"
            )
        finally:
            store.close()


class TestConsolidationLessonApplies:
    """Consolidation states the authored ``applies`` tier on both lesson write paths.

    Every other write surface (``learn_add``, ``POST /api/lessons``) states the
    tier; consolidation is the highest-volume writer, so without this its rows
    all land unstated and are served as standing rules. The tier is untrusted
    model output: the two literals round-trip, an omitted key lands unstated,
    and a misspelling is logged and lands unstated rather than dropping the
    correction.
    """

    _consolidator = staticmethod(TestConsolidationLessonScope._consolidator)
    _jsonl_consolidator = staticmethod(TestConsolidationLessonScope._jsonl_consolidator)

    def _store(self, tmp_path):
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = lambda _text: [1.0, 0.0]
        return store

    @staticmethod
    def _by_rule(store) -> dict[str, dict]:
        return {
            json.loads(row["value_json"])["rule"]: json.loads(row["value_json"])
            for row in store.get_lessons()
        }

    @pytest.mark.parametrize("tier", ["always", "on_topic"])
    def test_tier_round_trips_on_vector_path(self, tmp_path, tier) -> None:
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [{"rule": "Read the job log before classifying a red", "applies": tier}]
            )

            [lesson] = store.get_lessons()
            assert json.loads(lesson["value_json"])["applies"] == tier
            assert lesson["source"] == "consolidation"
        finally:
            store.close()

    def test_omitted_tier_lands_unstated_on_vector_path(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            self._consolidator(store)._save_lessons(
                [{"rule": "Prefer explicit timezones in timestamps"}]
            )

            [lesson] = store.get_lessons()
            # Unstated is ABSENT, not null: the row is byte-identical to one
            # written before the field existed (see write_lesson's lesson_value).
            assert "applies" not in json.loads(lesson["value_json"])
        finally:
            store.close()

    def test_misspelled_tier_logs_and_lands_unstated_on_vector_path(self, tmp_path, caplog) -> None:
        """A misspelling is a bug in the writer and must be audible, but the
        correction itself is not lost: it lands unstated, never dropped."""
        store = self._store(tmp_path)
        # Orthogonal embeddings: the two rows must not be judged duplicates of
        # each other, or the dedup pass (not the tier seam) decides which lands.
        store.embed_fn = lambda text: [1.0, 0.0] if "Misspelled" in text else [0.0, 1.0]
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
                self._consolidator(store)._save_lessons(
                    [
                        {"rule": "Misspelled tier still lands", "applies": "Directive"},
                        {"rule": "Clean sibling keeps its tier", "applies": "on_topic"},
                    ]
                )

            by_rule = self._by_rule(store)
            assert "applies" not in by_rule["Misspelled tier still lands"]
            assert by_rule["Clean sibling keeps its tier"]["applies"] == "on_topic"
            assert any("unrecognized applies tier" in rec.getMessage() for rec in caplog.records)
            # Only the closed-set reason is logged, never the untrusted value.
            assert all("Directive" not in rec.getMessage() for rec in caplog.records)
        finally:
            store.close()

    def test_tier_round_trips_on_jsonl_fallback(self, tmp_path) -> None:
        from kiro_crew.learn import LessonStore

        lesson_store = LessonStore(base_dir=tmp_path)
        c = self._jsonl_consolidator(lesson_store)

        c._save_lessons(
            [
                {"rule": "Never force-push a shared branch here", "applies": "always"},
                {"rule": "The flaky shard was the arm64 runner", "applies": "on_topic"},
                {"rule": "A rule with no tier stays unstated"},
            ]
        )

        by_rule = {le.rule: le for le in lesson_store.load_all()}
        assert by_rule["Never force-push a shared branch here"].applies == "always"
        assert by_rule["The flaky shard was the arm64 runner"].applies == "on_topic"
        assert by_rule["A rule with no tier stays unstated"].applies is None
        # The unstated row carries NO applies key on disk (not ``null``), so the
        # two stores agree on what absence means.
        rows = [
            json.loads(line)
            for line in (tmp_path / "lessons.jsonl").read_text().splitlines()
            if line.strip()
        ]
        unstated = [r for r in rows if r["rule"] == "A rule with no tier stays unstated"]
        assert unstated and "applies" not in unstated[0]

    def test_misspelled_tier_logs_and_lands_unstated_on_jsonl_fallback(
        self, tmp_path, caplog
    ) -> None:
        from kiro_crew.learn import LessonStore

        lesson_store = LessonStore(base_dir=tmp_path)
        c = self._jsonl_consolidator(lesson_store)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            c._save_lessons([{"rule": "Misspelled on the fallback path", "applies": "ALWAYS "}])
        # Case and surrounding whitespace are canonicalised, not refused.
        [lesson] = lesson_store.load_all()
        assert lesson.applies == "always"
        assert not any("unrecognized applies tier" in r.getMessage() for r in caplog.records)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            c._save_lessons([{"rule": "Truly misspelled on the fallback path", "applies": 7}])
        by_rule = {le.rule: le for le in lesson_store.load_all()}
        assert by_rule["Truly misspelled on the fallback path"].applies is None
        assert any("unrecognized applies tier" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_extraction_prompt_asks_for_the_tier(self, tmp_path) -> None:
        """The prompt the code actually builds names the field and the two
        literals, tells the model to decide from what the user said, and to omit
        the field when it cannot tell -- the same instruction ``learn_add`` carries."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-tier", "user", "no, always run the gate first")
        conv_log.append("dashboard:chat-tier", "assistant", "Understood, running it.")

        captured: dict[str, str] = {}

        async def fake_llm(prompt, *, memory_store: str = "", session_key: str = ""):
            captured["prompt"] = prompt
            return {"history_entry": "did stuff", "lessons": []}

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            consolidator.consolidate_session("dashboard:chat-tier")
            # flake-ok: pre-existing 50 ms wait for the scheduled consolidation task to run, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms wait for the scheduled consolidation task to run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            for t in list(consolidator._tasks):
                # flake-ok: pre-existing unbounded await of the consolidation task, moved verbatim; tracked for a de-flake pass
                await t

        prompt = " ".join(captured["prompt"].split())
        assert '"applies": "always|on_topic"' in prompt
        assert "YOU decide it from what the user actually said" in prompt
        assert "Omit the field when you genuinely cannot tell" in prompt

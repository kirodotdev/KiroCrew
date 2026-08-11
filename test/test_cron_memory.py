"""Tests for cron-session memory consolidation (kiro_crew.cron_memory).

Agent-mode cron runs used to write only to CronHistoryStore, which has no
connection to HistoryConsolidator / the memory stores — work a cron did (e.g.
root-causing a defect and opening a PR) left no trace in memory, and a later
interactive session would re-derive it from scratch.

Covers:
- record_cron_run_to_memory: exchange appended under cron-mem:{job_id} +
  consolidation triggered, for silent / hidden / persistent_session=False jobs
- nothing recorded for empty results or the "_No response._" placeholder
- recording failures (append or consolidator) never propagate
- credential redaction on the stored transcript rows
- gateway executor wiring: single-agent AND sequential paths record + trigger;
  failed runs record nothing; a consolidation error doesn't break the run or
  the session reset
- salience contract: the consolidation prompt authorizes an empty
  history_entry, and an empty history_entry writes no history
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_memory import cron_memory_key, record_cron_run_to_memory
from kiro_crew.history import ConversationLog, HistoryConsolidator


def _make_job(**overrides):
    defaults = dict(
        id="j1",
        name="test-job",
        message="check the logs",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


class TestRecordCronRunToMemory:
    """Unit tests for the bridge helper against a real ConversationLog."""

    def test_records_exchange_and_triggers_consolidation(self, tmp_path):
        """Silent + hidden + stateless job still lands in memory (default-on)."""
        log = ConversationLog(base_dir=tmp_path)
        job = _make_job(silent=True, hide_in_chat=True, persistent_session=False)

        ok = asyncio.run(
            record_cron_run_to_memory(log, job, "found a defect in X")
        )

        assert ok is True
        key = cron_memory_key(job.id)
        assert key == "cron-mem:j1"
        msgs = log.read_messages(key)
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "check the logs"),
            ("assistant", "found a defect in X"),
        ]

    def test_does_not_overload_cron_id_key(self, tmp_path):
        """cron:{id} emptiness is a documented invariant — must stay untouched."""
        log = ConversationLog(base_dir=tmp_path)
        job = _make_job()

        asyncio.run(record_cron_run_to_memory(log, job, "result"))

        assert log.read_messages(f"cron:{job.id}") == []

    @pytest.mark.parametrize("result", [None, "", "   ", "_No response._"])
    def test_empty_or_placeholder_result_records_nothing(self, tmp_path, result):
        log = ConversationLog(base_dir=tmp_path)
        job = _make_job()

        ok = asyncio.run(record_cron_run_to_memory(log, job, result))

        assert ok is False
        assert log.read_messages(cron_memory_key(job.id)) == []

    def test_missing_log_is_a_noop(self, tmp_path):
        job = _make_job()
        assert asyncio.run(record_cron_run_to_memory(None, job, "r")) is False

    def test_append_failure_is_swallowed(self):
        log = MagicMock()
        log.append.side_effect = OSError("disk full")
        job = _make_job()

        ok = asyncio.run(record_cron_run_to_memory(log, job, "result"))

        assert ok is False

    def test_credentials_redacted_in_transcript(self, tmp_path):
        """Stored rows follow the cron result sink redaction precedent."""
        log = ConversationLog(base_dir=tmp_path)
        # Built by concatenation so the fixture itself never matches secret
        # scanners (repo precedent: test_telemetry_titles.py).
        fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        job = _make_job(message=f"rotate key {fake_key} now")

        asyncio.run(
            record_cron_run_to_memory(log, job, f"old key was {fake_key}")
        )

        msgs = log.read_messages(cron_memory_key(job.id))
        blob = "\n".join(m["content"] for m in msgs)
        assert fake_key not in blob


# ── Gateway executor wiring ──────────────────────────────────────────────


def _make_gateway(tmp_path):
    """Minimal GatewayOrchestrator mirroring test_cron_dedup's harness,
    with a REAL ConversationLog and a recording consolidator stub."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = ConversationLog(base_dir=tmp_path)
    gw.dashboard_state = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._running_script_ids = set()
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _run_callback(gw, job, stream_result="done", stream_exc=None):
    """Init cron on the gateway, capture the callback, and invoke it."""
    captured_cb = None

    async def fake_stream(client, msg, **kwargs):
        if stream_exc is not None:
            raise stream_exc
        return stream_result

    with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
        "kiro_crew.slack.gateway.CronService"
    ) as mock_cron_cls, patch("kiro_crew.slack.gateway.sel"):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            result = await captured_cb(job)
            # Recording is DETACHED from the job's timeout window (a task,
            # not an await) — drain it so assertions see the completed write.
            from kiro_crew import cron_memory as _cm

            if _cm._DETACHED_TASKS:
                await asyncio.gather(
                    *_cm._DETACHED_TASKS, return_exceptions=True
                )
            return result

        return asyncio.run(_init_and_run())


class TestGatewaySingleAgentPath:
    def test_run_records_memory_and_triggers_consolidation(self, tmp_path):
        gw = _make_gateway(tmp_path)
        # Silent + hidden + stateless: the run never reaches a delivery site,
        # yet must still reach memory (the default-on requirement).
        job = _make_job(silent=True, hide_in_chat=True, persistent_session=False)

        result = _run_callback(gw, job, stream_result="root-caused defect Y")

        assert result == "root-caused defect Y"
        key = cron_memory_key(job.id)
        msgs = gw.conv_log.read_messages(key)
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "check the logs"),
            ("assistant", "root-caused defect Y"),
        ]

    def test_failed_run_records_nothing(self, tmp_path):
        gw = _make_gateway(tmp_path)
        job = _make_job(silent=True)

        with pytest.raises(RuntimeError):
            _run_callback(gw, job, stream_exc=RuntimeError("turn failed"))

        assert gw.conv_log.read_messages(cron_memory_key(job.id)) == []

    def test_empty_result_records_nothing(self, tmp_path):
        gw = _make_gateway(tmp_path)
        job = _make_job(silent=True)

        result = _run_callback(gw, job, stream_result="")

        assert result == "_No response._"
        assert gw.conv_log.read_messages(cron_memory_key(job.id)) == []

    def test_recording_error_breaks_neither_run_nor_reset(self, tmp_path):
        gw = _make_gateway(tmp_path)
        gw.conv_log = MagicMock()
        gw.conv_log.atomic_appends.side_effect = RuntimeError("boom")
        job = _make_job(silent=True)

        result = _run_callback(gw, job, stream_result="fine")

        assert result == "fine"
        gw.sessions.reset.assert_awaited()


class TestGatewaySequentialPath:
    def test_sequential_run_records_final_result(self, tmp_path):
        gw = _make_gateway(tmp_path)
        job = _make_job(agent_sequence=["planner", "executor"], silent=True)

        result = _run_callback(gw, job, stream_result="sequence outcome")

        assert result == "sequence outcome"
        key = cron_memory_key(job.id)
        msgs = gw.conv_log.read_messages(key)
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "check the logs"),
            ("assistant", "sequence outcome"),
        ]


# ── Salience: nothing-notable spans must write no history ────────────────


class TestConsolidationSalience:
    def _consolidator_with_real_log(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("cron-mem:j1", "user", "check the logs")
        log.append("cron-mem:j1", "assistant", "nothing new")
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        c = HistoryConsolidator(log=log, memory=memory, sessions=None, migrated=True)
        return c, memory

    def test_prompt_authorizes_empty_history_entry(self, tmp_path):
        """The consolidation prompt must offer the nothing-notable skip."""
        c, _memory = self._consolidator_with_real_log(tmp_path)
        prompts: list[str] = []

        async def fake_llm(prompt):
            prompts.append(prompt)
            return {"history_entry": ""}

        with patch.object(c, "_call_llm", side_effect=fake_llm):
            asyncio.run(c._consolidate("cron-mem:j1", include_history=True))

        assert prompts, "_call_llm was not invoked"
        assert 'return an empty string ""' in prompts[0]

    def test_empty_history_entry_writes_no_history(self, tmp_path):
        c, memory = self._consolidator_with_real_log(tmp_path)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {"history_entry": "", "lessons": []}
            asyncio.run(c._consolidate("cron-mem:j1", include_history=True))

        memory.append_history.assert_not_called()

    def test_notable_history_entry_is_written(self, tmp_path):
        """Control: the skip must not swallow genuinely notable spans."""
        c, memory = self._consolidator_with_real_log(tmp_path)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {"history_entry": "Root-caused defect Y.", "lessons": []}
            asyncio.run(c._consolidate("cron-mem:j1", include_history=True))

        memory.append_history.assert_called_once_with("Root-caused defect Y.")


class TestDiskBackedSweep:
    """The transcript FILE is the enrollment record — no volatile state."""

    def _fresh_consolidator(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        c = HistoryConsolidator(log=log, memory=memory, sessions=None, migrated=True)
        return c, log

    async def _drain(self, c):
        for t in list(c._tasks):
            await t
        for t in list(c._tasks):  # nested spawns from the scan task
            await t

    @pytest.mark.asyncio
    async def test_restart_discovers_preexisting_transcript(self, tmp_path):
        """A transcript written before a restart is swept with ZERO in-process
        registration — the core durability property (survives restarts,
        one-shot cron deletion, and shutdown between write and trigger)."""
        writer_log = ConversationLog(base_dir=tmp_path)
        with writer_log.atomic_appends("cron-mem:oneshot"):
            writer_log.append("cron-mem:oneshot", "user", "run once")
            writer_log.append("cron-mem:oneshot", "assistant", "opened PR #1")
        # Age the file past the settle grace.
        f = next(tmp_path.glob("cron-mem_oneshot*.jsonl"))
        old = time.time() - 120
        os.utime(f, (old, old))

        # "Restart": a brand-new consolidator with empty in-memory dicts.
        c, _log = self._fresh_consolidator(tmp_path)
        with patch.object(c, "_consolidate", AsyncMock()) as consolidated:
            c.sweep_cron_memory_keys()
            await self._drain(c)
            consolidated.assert_awaited_once()
            assert consolidated.await_args.args[0] == "cron-mem_oneshot"

    @pytest.mark.asyncio
    async def test_just_written_transcript_is_swept_immediately(self, tmp_path):
        """No freshness gate: a minutely cron's file never 'settles', so an
        mtime filter would starve it forever (GPT round-6 finding). Locks
        make concurrent appends safe; discovery must include hot files."""
        writer_log = ConversationLog(base_dir=tmp_path)
        writer_log.append("cron-mem:hot", "user", "m")
        writer_log.append("cron-mem:hot", "assistant", "r")

        c, _log = self._fresh_consolidator(tmp_path)
        with patch.object(c, "_consolidate", AsyncMock()) as consolidated:
            c.sweep_cron_memory_keys()
            await self._drain(c)
            consolidated.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_is_single_flight(self, tmp_path):
        c, _log = self._fresh_consolidator(tmp_path)
        c._cron_sweep_inflight = True
        before = len(c._tasks)
        c.sweep_cron_memory_keys()
        assert len(c._tasks) == before  # latched: no second scan spawned

    @pytest.mark.asyncio
    async def test_non_cron_transcripts_are_not_swept(self, tmp_path):
        writer_log = ConversationLog(base_dir=tmp_path)
        writer_log.append("dashboard_chat-1", "user", "hi")
        f = next(tmp_path.glob("dashboard_chat-1*.jsonl"))
        old = time.time() - 120
        os.utime(f, (old, old))

        c, _log = self._fresh_consolidator(tmp_path)
        with patch.object(c, "_consolidate", AsyncMock()) as consolidated:
            c.sweep_cron_memory_keys()
            await self._drain(c)
            consolidated.assert_not_awaited()

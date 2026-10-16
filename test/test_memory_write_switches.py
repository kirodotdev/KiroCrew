"""The writes-only and lesson-extraction memory switches.

``memory.writes_enabled`` stops the same automatic writers as
``memory.persistence_enabled`` while stored memory and lessons are still
injected. ``memory.lesson_extraction_enabled`` stops only the extracted-lesson
saves: consolidation still runs and writes everything else. Both default on, so
a config that sets neither behaves exactly as one that predates them.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log
from test_memory_context_toggles import KEY, _builder, _make_consolidator, _seed_log, _write_config

from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.config.memory_sections import (
    MemoryConfig,
    lesson_extraction_enabled,
    memory_writes_off_key,
)
from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
from kiro_crew.learn import LessonStore


class TestDefaults:
    def test_both_switches_default_on(self):
        mem = MemoryConfig()
        assert mem.writes_enabled is True
        assert mem.lesson_extraction_enabled is True

    def test_an_unset_config_loads_both_on(self, tmp_path):
        _write_config()
        mem = KiroCrewConfig.load().memory
        assert mem.writes_enabled is True
        assert mem.lesson_extraction_enabled is True
        assert memory_writes_off_key(mem) is None
        assert lesson_extraction_enabled(mem) is True

    def test_the_config_file_turns_each_off(self, tmp_path):
        _write_config({"writes_enabled": False, "lesson_extraction_enabled": False})
        mem = KiroCrewConfig.load().memory
        assert mem.writes_enabled is False
        assert mem.lesson_extraction_enabled is False


class TestHelpers:
    def test_persistence_wins_the_reported_key(self):
        both_off = SimpleNamespace(persistence_enabled=False, writes_enabled=False)
        assert memory_writes_off_key(both_off) == "memory.persistence_enabled"

    def test_writes_off_alone_is_reported(self):
        writes_off = SimpleNamespace(persistence_enabled=True, writes_enabled=False)
        assert memory_writes_off_key(writes_off) == "memory.writes_enabled"

    def test_a_stand_in_without_the_new_keys_counts_them_on(self):
        legacy = SimpleNamespace(persistence_enabled=True)
        assert memory_writes_off_key(legacy) is None
        assert lesson_extraction_enabled(legacy) is True

    def test_writes_off_also_stops_extraction(self):
        writes_off = SimpleNamespace(
            persistence_enabled=True, writes_enabled=False, lesson_extraction_enabled=True
        )
        assert lesson_extraction_enabled(writes_off) is False


def _lesson_consolidator() -> tuple[HistoryConsolidator, MagicMock]:
    """A consolidator whose one pass extracts a lesson, with stores mocked."""
    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = ([{"role": "user", "content": "hi"}], 1, 0, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    vector_store = MagicMock()
    vector_store.get_all_semantic.return_value = []
    vector_store.write_lesson.return_value = True
    cons = HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=vector_store, migrated=True
    )
    return cons, vector_store


async def _run_lesson_pass(cons: HistoryConsolidator) -> tuple[bool, bool]:
    """Run one pass; report whether the lesson stage and the structured stage ran."""
    with (
        patch.object(cons, "_call_llm", new_callable=AsyncMock) as llm,
        patch.object(cons, "_write_structured_memory") as structured,
        patch.object(cons, "_save_lessons") as save,
    ):
        llm.return_value = {
            "lessons": [{"rule": "always check return codes", "category": "tool"}],
            "semantic": [{"key": "editor", "value": "vim"}],
        }
        await cons._consolidate("k", include_history=True)
    return save.called, structured.called


class TestLessonExtractionSwitch:
    @pytest.mark.asyncio
    async def test_consolidation_saves_lessons_by_default(self, tmp_path):
        _write_config()
        cons, _ = _lesson_consolidator()
        saved, structured = await _run_lesson_pass(cons)
        assert saved is True
        assert structured is True

    @pytest.mark.asyncio
    async def test_off_skips_lessons_but_keeps_the_rest_of_the_pass(self, tmp_path):
        _write_config({"lesson_extraction_enabled": False})
        cons, _ = _lesson_consolidator()
        saved, structured = await _run_lesson_pass(cons)
        assert saved is False, "an extracted lesson was saved with extraction off"
        assert structured is True, "extraction off must not stop the other memory writes"

    @pytest.mark.asyncio
    async def test_off_skips_the_task_runner_extractor(self, tmp_path):
        _write_config({"lesson_extraction_enabled": False})
        assert await _task_extractor_reached() is False

    @pytest.mark.asyncio
    async def test_on_runs_the_task_runner_extractor(self, tmp_path):
        _write_config()
        assert await _task_extractor_reached() is True

    def test_learn_add_route_is_not_gated_by_extraction(self, tmp_path):
        _write_config({"lesson_extraction_enabled": False})
        assert memory_writes_off_key(KiroCrewConfig.load().memory) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_member_store_payload_follows_the_switch(self, tmp_path, monkeypatch, enabled):
        """The V2 member path hands the store its updates with lessons only when on."""
        from kiro_crew.context import ContextBuilder
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        _write_config({} if enabled else {"lesson_extraction_enabled": False})
        cons, vector_store = _lesson_consolidator()
        vector_store.algorithm_version = "v2"
        vector_store.consolidation_receipt.return_value = None
        vector_store.with_record_metadata.side_effect = lambda entries: entries
        vector_store.apply_consolidation.return_value = {"semantic": 0, "episodic": 0, "lessons": 0}
        execution = ExecutionContext(
            "member-id",
            MemoryStoreRef("member-store", "member-id"),
            "member",
            "kirocrew",
            "persistent",
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda _key: execution
        )
        monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vector_store))
        monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: cons._memory)
        with patch.object(cons, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {
                "lessons": [{"rule": "always check return codes", "category": "tool"}],
                "semantic": [{"key": "project.editor", "value": "vim"}],
            }
            await cons._consolidate("k", include_history=False)
        vector_store.apply_consolidation.assert_called_once()
        handed = vector_store.apply_consolidation.call_args.kwargs["result"]
        assert ("lessons" in handed) is enabled
        assert handed["semantic"] == [{"key": "project.editor", "value": "vim"}]


async def _task_extractor_reached() -> bool:
    """Drive the task runner's lesson extractor on a stub; True if it passed the gate."""
    from kiro_crew.taskrunner import TaskRunner

    reached = False

    def _capture() -> SimpleNamespace:
        nonlocal reached
        reached = True
        return SimpleNamespace(memory_mode="ephemeral", member_id=None)

    stub = SimpleNamespace(_capture_execution=_capture, _lesson_store=None, _ctx=None)
    await TaskRunner._extract_lesson(stub, SimpleNamespace(title="t", error="boom"))
    return reached


class TestWritesSwitch:
    @pytest.mark.asyncio
    async def test_consolidation_refuses(self, tmp_path):
        _write_config({"writes_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        assert await cons._consolidate(KEY) is _CONSOLIDATION_REFUSED

    def test_automatic_consolidation_schedules_nothing(self, tmp_path):
        _write_config({"writes_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        with patch.object(HistoryConsolidator, "_consolidate", new=AsyncMock()) as run:
            cons.maybe_consolidate(KEY)
            assert not cons._tasks, "a consolidation was scheduled with writes off"
            run.assert_not_called()

    @pytest.mark.asyncio
    async def test_lessons_route_refuses_and_names_the_switch(self, tmp_path):
        _write_config({"writes_enabled": False})
        from kiro_crew.dashboard.handlers import cron as cron_mod

        req = MagicMock()
        req.app = {"state": MagicMock()}
        req.headers = {"X-Session-Key": "dashboard:ui"}

        async def _body(request, **_kw):
            return {"rule": "r", "category": "tool"}, None

        sel = MagicMock()
        with (
            patch.object(cron_mod, "read_bounded_json", side_effect=_body),
            patch.object(cron_mod, "_recognize_session", new=AsyncMock(return_value=None)),
            patch.object(cron_mod, "_is_restricted_session", return_value=False),
            patch.object(cron_mod, "_sel", return_value=sel),
        ):
            resp = await cron_mod.api_lessons_create(req)
        assert resp.status == 403
        assert "memory.writes_enabled" in resp.text
        assert "writes_disabled" in resp.text
        assert sel.log_api_access.call_args.kwargs["resources"] == "writes_disabled"

    def test_learn_cli_refuses(self, tmp_path, capsys):
        _write_config({"writes_enabled": False})
        from kiro_crew import cli_commands

        args = argparse.Namespace(
            learn_action="add", rule="Always frobnicate", category="tool", negative=None
        )
        cli_commands._learn(args)
        out = capsys.readouterr().out
        assert "NOT saved" in out
        assert "memory.writes_enabled" in out
        assert LessonStore().load_all() == []
        assert not (config_dir() / "memory.db").exists()

    @pytest.mark.asyncio
    async def test_task_runner_extractor_skips(self, tmp_path):
        _write_config({"writes_enabled": False})
        assert await _task_extractor_reached() is False

    def test_stored_memory_and_lessons_are_still_injected(self, tmp_path):
        _write_config({"writes_enabled": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "Prefers tabs over spaces" in ctx
        assert "Always pass encoding=utf-8" in ctx

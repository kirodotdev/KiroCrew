"""Config-driven memory/lessons injection toggles and the persistence master switch.

Two features share one mechanism (#9909 / #9959):

* ``memory.inject_memory`` / ``memory.inject_lessons`` withhold the stored-memory
  and lessons blocks on the MAIN path — ``context_groups=None``, which every
  non-subagent surface passes — where the spawn-time ``include_*`` flags never
  reach. The intersection happens inside ``build_session_context``, so every
  surface obeys the config without passing anything.
* ``memory.persistence_enabled`` is the master switch: off withholds BOTH groups
  regardless of the ``inject_*`` values, and stops the automatic writers. The
  writer gates tested here are consolidation (all three automatic entry points
  plus ``_consolidate`` itself, which the manual REST/CLI triggers call), the
  ``POST /api/lessons`` route (the enforcement point behind the ``learn_add``
  MCP tool), and the ``kirocrew learn add`` CLI's direct store write.

The builder fixture mirrors ``test_subagent_context_groups._builder``: every
group's content is populated, because an "absent" assertion against an empty
store passes with the gate deleted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew.config.loader import config_path
from kiro_crew.context import (
    CONTEXT_GROUP_MEMORY,
    SWITCHABLE_CONTEXT_GROUPS,
    ContextBuilder,
)
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

ALL_GROUPS = frozenset(SWITCHABLE_CONTEXT_GROUPS)

KEY = "dashboard:chat-toggles"


def _write_config(memory_overrides: dict[str, Any] | None = None) -> None:
    """Write the isolated home's config.json.

    Always carries the onboarding answer that puts a [USER PROFILE] block in
    the lessons group, so the lessons-absent assertions have real content to
    be absent.
    """
    data: dict[str, Any] = {"dashboard": {"user_role": "developer"}}
    if memory_overrides:
        data["memory"] = memory_overrides
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def _builder(tmp_path) -> ContextBuilder:
    """A builder with the memory and lessons groups' content populated."""
    memory = MemoryStore(workspace=tmp_path / "ws")
    memory.write_preferences("# User Preferences\n\n- Prefers tabs over spaces\n")
    memory.write_projects("# Active Projects\n\n- Ship the widget rewrite\n")
    lessons = LessonStore(base_dir=tmp_path)
    lessons.save(
        Lesson(ts="2026-01-01T00:00:00Z", rule="Always pass encoding=utf-8", category="tool")
    )
    return ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=lessons,
    )


class TestInjectMemoryToggle:
    def test_present_by_default(self, tmp_path):
        _write_config()
        ctx = _builder(tmp_path).build_session_context()
        assert "Prefers tabs over spaces" in ctx
        assert "## Active Projects" in ctx

    def test_absent_when_disabled(self, tmp_path):
        _write_config({"inject_memory": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "Prefers tabs over spaces" not in ctx
        assert "## Active Projects" not in ctx

    def test_lessons_unaffected(self, tmp_path):
        _write_config({"inject_memory": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "Always pass encoding=utf-8" in ctx

    def test_config_withholding_is_silent(self, tmp_path):
        """No [CONTEXT SCOPE] marker: "your parent withheld" describes subagent
        narrowing, and a config withholding is the operator's standing choice."""
        _write_config({"inject_memory": False, "inject_lessons": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "[CONTEXT SCOPE]" not in ctx


class TestInjectLessonsToggle:
    def test_absent_when_disabled(self, tmp_path):
        _write_config({"inject_lessons": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "Always pass encoding=utf-8" not in ctx
        assert "[USER PROFILE]" not in ctx

    def test_memory_unaffected(self, tmp_path):
        _write_config({"inject_lessons": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "Prefers tabs over spaces" in ctx


class TestPersistenceMasterSwitchInjection:
    def test_withholds_both_groups(self, tmp_path):
        _write_config(
            {"persistence_enabled": False, "inject_memory": True, "inject_lessons": True}
        )
        ctx = _builder(tmp_path).build_session_context()
        assert "Prefers tabs over spaces" not in ctx
        assert "Always pass encoding=utf-8" not in ctx
        assert "[USER PROFILE]" not in ctx

    def test_conduct_and_workspace_survive(self, tmp_path):
        """Within-conversation context is out of scope; only stored blocks go."""
        _write_config({"persistence_enabled": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "[CRITICAL RULES" in ctx
        assert "[WORKSPACE IDENTITY]" in ctx


class TestConfigIntersectsSubagentScope:
    def test_config_wins_over_an_explicit_full_scope(self, tmp_path):
        """A parent granting every group cannot re-enable a config-disabled one."""
        _write_config({"inject_memory": False})
        ctx = _builder(tmp_path).build_session_context(context_groups=ALL_GROUPS)
        assert "Prefers tabs over spaces" not in ctx

    def test_subagent_narrowing_survives_config_all_on(self, tmp_path):
        _write_config()
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "Prefers tabs over spaces" not in ctx
        # The parent's withholding is still announced.
        assert "[CONTEXT SCOPE]" in ctx


# ---------------------------------------------------------------------------
# Writer gates
# ---------------------------------------------------------------------------


def _seed_log(tmp_path, key: str = KEY, count: int = 40) -> ConversationLog:
    """A real transcript with enough messages to clear the 30-message threshold."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


class TestConsolidationGate:
    @pytest.mark.asyncio
    async def test_maybe_consolidate_schedules_nothing_when_disabled(self, tmp_path):
        _write_config({"persistence_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        with patch.object(HistoryConsolidator, "_consolidate", new=AsyncMock()) as run:
            cons.maybe_consolidate(KEY)
            await asyncio.sleep(0)
            run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_maybe_consolidate_fires_when_enabled(self, tmp_path):
        """The threshold fixture really clears the gate — the disabled assertion
        above is about the switch, not about an under-seeded transcript."""
        _write_config()
        cons = _make_consolidator(_seed_log(tmp_path))
        with patch.object(
            HistoryConsolidator, "_consolidate", new=AsyncMock(return_value=None)
        ) as run:
            cons.maybe_consolidate(KEY)
            await asyncio.gather(*cons._tasks)
            run.assert_awaited()

    @pytest.mark.asyncio
    async def test_idle_sweep_schedules_nothing_when_disabled(self, tmp_path):
        _write_config({"persistence_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        cons._last_activity[KEY] = 0.0  # long idle
        with patch.object(HistoryConsolidator, "_consolidate", new=AsyncMock()) as run:
            cons.check_idle_sessions()
            await asyncio.sleep(0)
            run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_end_schedules_nothing_when_disabled(self, tmp_path):
        _write_config({"persistence_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        with patch.object(HistoryConsolidator, "_consolidate", new=AsyncMock()) as run:
            cons.consolidate_session(KEY)
            await asyncio.sleep(0)
            run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consolidate_itself_refuses_covering_manual_triggers(self, tmp_path):
        """POST /api/memory/consolidate and the CLI call _consolidate directly;
        the REFUSED sentinel keeps offsets unadvanced and throttles unset."""
        _write_config({"persistence_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        assert await cons._consolidate(KEY) is _CONSOLIDATION_REFUSED


class TestLessonsRouteGate:
    @pytest.mark.asyncio
    async def test_post_api_lessons_refuses_when_disabled(self, tmp_path):
        _write_config({"persistence_enabled": False})
        from kiro_crew.dashboard.handlers import cron as cron_mod

        req = MagicMock()
        req.app = {"state": MagicMock()}
        req.headers = {"X-Session-Key": "dashboard:ui"}

        async def _body(request, **_kw):
            return {"rule": "r", "category": "tool"}, None

        with (
            patch.object(cron_mod, "read_bounded_json", side_effect=_body),
            patch.object(cron_mod, "_recognize_session", new=AsyncMock(return_value=None)),
            patch.object(cron_mod, "_is_restricted_session", return_value=False),
            patch.object(cron_mod, "_sel", return_value=MagicMock()),
        ):
            resp = await cron_mod.api_lessons_create(req)
        assert resp.status == 403
        assert "persistence_enabled" in resp.text


class TestLearnCliGate:
    def test_learn_add_refuses_when_disabled(self, tmp_path, capsys):
        _write_config({"persistence_enabled": False})
        from kiro_crew import cli_commands

        args = argparse.Namespace(
            learn_action="add", rule="Always frobnicate", category="tool", negative=None
        )
        cli_commands._learn(args)
        out = capsys.readouterr().out
        assert "NOT saved" in out
        assert "persistence_enabled" in out
        # Nothing reached either store.
        assert LessonStore().load_all() == []

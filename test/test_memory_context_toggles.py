"""Config-driven memory/lessons injection toggles and the global persistence switch.

Two features share one mechanism:

* ``memory.inject_memory`` / ``memory.inject_lessons`` withhold the stored-memory
  and lessons blocks on the MAIN path — ``context_groups=None``, which every
  non-subagent surface passes — where the spawn-time ``include_*`` flags never
  reach. The intersection happens inside ``build_session_context``, so every
  surface obeys the config without passing anything.
* ``memory.persistence_enabled`` is the global switch: off withholds BOTH groups
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
import contextlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew.config.loader import config_dir, config_path
from kiro_crew.context import (
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    SWITCHABLE_CONTEXT_GROUPS,
    ContextBuilder,
    _config_scoped_groups,
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
        assert "[Memory tools]" in ctx

    def test_absent_when_disabled(self, tmp_path):
        _write_config({"inject_memory": False})
        ctx = _builder(tmp_path).build_session_context()
        assert "Prefers tabs over spaces" not in ctx
        assert "[Memory tools]" not in ctx

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


class TestPostCompactionReinjection:
    """The re-injection after a compaction restores what session start withheld.

    ``build_message`` re-injects the stored-memory activity index once a
    compaction drops the session-start context. That is the same block
    ``build_session_context`` gates, so it routes through the same config
    intersection — reading the caller scope alone would hand back the memory an
    operator's ``inject_memory: false`` excluded, for the rest of the session.
    """

    MARKER = "Call memory_recall with specific keywords"

    def _reinjected(self, tmp_path) -> str:
        message, _ = _builder(tmp_path).build_message(
            "hi", is_new_session=False, needs_reinjection=True
        )
        return message

    def test_present_by_default(self, tmp_path):
        _write_config()
        assert self.MARKER in self._reinjected(tmp_path)

    def test_absent_when_memory_injection_is_disabled(self, tmp_path):
        _write_config({"inject_memory": False})
        assert self.MARKER not in self._reinjected(tmp_path)

    def test_absent_when_the_global_switch_is_off(self, tmp_path):
        _write_config({"persistence_enabled": False})
        assert self.MARKER not in self._reinjected(tmp_path)


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


class TestPersistenceGlobalSwitchInjection:
    def test_withholds_both_groups(self, tmp_path):
        _write_config({"persistence_enabled": False, "inject_memory": True, "inject_lessons": True})
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


class TestConfigScopedGroupsHelper:
    """The shared intersection every context entry point routes through.

    Asserted directly because it now has two callers (``build_session_context``
    and the v2 essentials builder): a regression in one caller is caught by the
    section tests above, but a regression in the helper itself would surface as
    two unrelated failures without naming the cause.
    """

    def test_all_on_passes_the_caller_scope_through_unchanged(self, tmp_path):
        _write_config()
        assert _config_scoped_groups(None) is None
        assert _config_scoped_groups(ALL_GROUPS) == ALL_GROUPS

    def test_a_disabled_group_is_removed_from_an_implicit_full_scope(self, tmp_path):
        _write_config({"inject_memory": False})
        assert _config_scoped_groups(None) == ALL_GROUPS - {CONTEXT_GROUP_MEMORY}

    def test_the_global_switch_removes_both_memory_and_lessons(self, tmp_path):
        _write_config({"persistence_enabled": False})
        assert _config_scoped_groups(None) == frozenset({CONTEXT_GROUP_PROJECT})

    def test_config_can_only_subtract_never_restore(self, tmp_path):
        """A group the parent withheld stays withheld however config reads."""
        _write_config()
        parent = ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        assert CONTEXT_GROUP_MEMORY not in (_config_scoped_groups(parent) or ALL_GROUPS)


class TestValidationPathIgnoresInjectionConfig:
    """A profile-validation pass must see the complete candidate set.

    ``_build_v2_essentials`` doubles as the validator behind a private-profile
    update (``profile_overrides`` supplied): the candidate is appended only
    under the memory-group gate, and ``render_essentials`` raising on the
    combined budget is what rejects a profile that fits per-file but overflows
    combined. If config scoping applied there, disabling injection would skip
    that refusal, save an oversized profile, and break every later member
    context build for that member.

    The builder is driven past ``member_context_identity`` with a stubbed owner,
    since an unresolvable store returns before the scoping decision is ever made
    — which is what makes the assertion about that decision meaningful.
    """

    def _scoping_calls(self, tmp_path, monkeypatch, **kwargs) -> list[object]:
        """Drive the builder and report what the scoping helper was asked."""
        builder = _builder(tmp_path)
        calls: list[object] = []
        monkeypatch.setattr(
            "kiro_crew.member_essential_context.member_context_identity",
            lambda member, *, member_is_id=True: ("alice", "template"),
        )
        monkeypatch.setattr(
            "kiro_crew.context._config_scoped_groups",
            lambda groups, cfg=None: calls.append(groups) or groups,
        )
        # Downstream rendering needs a fully provisioned V2 member; this test is
        # about the scoping decision, which is made before any of it.
        with contextlib.suppress(Exception):
            builder._build_v2_essentials("store-alice", **kwargs)
        return calls

    def test_scoping_is_skipped_when_profile_overrides_is_supplied(self, tmp_path, monkeypatch):
        _write_config({"persistence_enabled": False})
        calls = self._scoping_calls(
            tmp_path, monkeypatch, profile_overrides={"USER.md": "candidate"}
        )
        assert calls == [], "config scoping must not run on a validation pass"

    def test_a_context_build_still_consults_config(self, tmp_path, monkeypatch):
        """The control: without profile_overrides the scoping still applies."""
        _write_config({"persistence_enabled": False})
        calls = self._scoping_calls(tmp_path, monkeypatch)
        assert calls == [None]


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

    @pytest.mark.asyncio
    async def test_a_refusal_releases_the_running_claim(self, tmp_path):
        """The refusal must not strand the key in ``_running``.

        The automatic entry points claim the key BEFORE scheduling the task and
        their done-callbacks never discard it — ``_consolidate``'s own ``finally``
        is the only clearer. A refusal that returned ahead of that ``try`` would
        leave the key claimed forever, so every later consolidation for the
        session is refused by the ``key in self._running`` guard even after
        persistence is switched back on. Reachable whenever the switch is
        flipped off between ``create_task`` and the task's first line.
        """
        _write_config({"persistence_enabled": False})
        cons = _make_consolidator(_seed_log(tmp_path))
        cons._running.add(KEY)  # what the entry points do before scheduling
        assert await cons._consolidate(KEY) is _CONSOLIDATION_REFUSED
        assert KEY not in cons._running, "a refused pass left the session claimed"


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


class TestConsolidateRouteGate:
    @pytest.mark.asyncio
    async def test_consolidate_route_refuses_and_audits_when_disabled(self, tmp_path):
        """The refusal is SEL-recorded.

        The request has already passed identity and the write gate by this
        point, so the refusal is a config-state decision about an authorized
        caller — exactly what an audit trail has to show. An unrecorded denial
        leaves no trace that the switch turned work away.
        """
        _write_config({"persistence_enabled": False})
        from kiro_crew.dashboard.handlers import memory as memory_mod

        req = MagicMock()
        req.app = {"state": MagicMock()}
        req.headers = {"X-Session-Key": "dashboard:ui"}
        sel = MagicMock()
        with (
            patch.object(
                memory_mod,
                "resolve_lesson_memory_store",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch.object(memory_mod, "_memory_write_gate", new=AsyncMock(return_value=None)),
            patch.object(memory_mod, "_sel", return_value=sel),
        ):
            resp = await memory_mod.api_memory_consolidate(req)
        assert resp.status == 403
        assert "persistence_enabled" in resp.text
        assert sel.log_api_access.called, "a persistence denial must be SEL-recorded"
        kwargs = sel.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["resources"] == "persistence_disabled"


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
        # And no store was opened on the way to the refusal: constructing the
        # vector store creates or migrates memory.db, which a write the switch
        # refuses must not leave behind.
        assert not (config_dir() / "memory.db").exists()


class TestTaskrunnerLessonGate:
    """The task runner's lesson extractor stops before it spends an LLM turn.

    Asserted behaviourally, because the inventory ratchet cannot carry this one:
    its check is that the module mentions ``persistence_enabled`` at all, and the
    guard's own comment names the key, so a deleted guard leaves that token in
    place and the cheap check stays green. Reaching ``_capture_execution`` — the
    first thing the extractor touches past the switch — is the witness.
    """

    async def _capture_reached(self) -> bool:
        """Drive the extractor on a stub and report whether it ran past the gate."""
        from kiro_crew.taskrunner import TaskRunner

        reached = False

        def _capture() -> SimpleNamespace:
            nonlocal reached
            reached = True
            # Ephemeral, so the enabled path returns at its own mode check
            # instead of resolving a store or calling the model.
            return SimpleNamespace(memory_mode="ephemeral", member_id=None)

        stub = SimpleNamespace(_capture_execution=_capture, _lesson_store=None, _ctx=None)
        await TaskRunner._extract_lesson(stub, SimpleNamespace(title="t", error="boom"))
        return reached

    @pytest.mark.asyncio
    async def test_extraction_is_skipped_when_disabled(self, tmp_path):
        _write_config({"persistence_enabled": False})
        assert await self._capture_reached() is False

    @pytest.mark.asyncio
    async def test_extraction_runs_when_enabled(self, tmp_path):
        """The control: the gate is what stopped it above, not the stub."""
        _write_config()
        assert await self._capture_reached() is True

"""Tests for the plumbing that carries context-group flags to the sub-agent.

The flags are decided once, by the parent, at spawn. Every path that
re-materializes a run from stored fields must carry them, or a run silently
executes with a different context scope than the one its caller asked for:

- the **stagger queue**, which is where most members of a large fan-out sit
  before starting, and
- the **retry** endpoint, which re-spawns from the failed record.

``spawn_continue`` is deliberately NOT in that list: a resumed session skips
context rebuilding entirely, so the flags cannot apply to it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import (
    SubagentInfo,
    SubagentManager,
    _context_groups_field,
    _context_groups_of,
)
from kiro_crew.subagent_persistence import create_agent_folder, read_state


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _mgr() -> SubagentManager:
    return SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())


class TestDefaults:
    def test_all_groups_on_by_default(self):
        """A caller that says nothing gets the context a normal session gets."""
        info = SubagentInfo(id="a1", task="t")
        assert info.include_memory is True
        assert info.include_lessons is True
        assert info.include_project is True

    @pytest.mark.asyncio
    async def test_spawn_defaults_to_all_on(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing")
        assert info is not None
        assert (info.include_memory, info.include_lessons, info.include_project) == (
            True,
            True,
            True,
        )


class TestFlagsReachTheRecord:
    @pytest.mark.asyncio
    async def test_spawn_threads_flags_onto_info(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("read these files", include_memory=False, include_project=False)
        assert info is not None
        assert info.include_memory is False
        assert info.include_lessons is True
        assert info.include_project is False


class TestQueueRoundTrip:
    """A queued spawn must start with the scope its caller chose."""

    def test_queue_entry_carries_flags(self):
        mgr = _mgr()
        # Force the stagger gate so the spawn queues instead of starting.
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        info = mgr.spawn("summarize this log", include_memory=False)
        assert info is not None and info.queued is True
        assert len(mgr._queue) == 1
        entry = mgr._queue[0]
        assert entry["include_memory"] is False
        assert entry["include_lessons"] is True
        assert entry["include_project"] is True

    def test_queued_placeholder_reports_the_scope(self):
        """spawn_list shows queued members too, so the record must carry it."""
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        info = mgr.spawn("summarize this log", include_lessons=False)
        assert info is not None
        assert info.include_lessons is False

    def test_drained_spawn_receives_the_flags(self):
        """The drain forwards the FULL kwarg set, flags included."""
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        mgr.spawn("validate this finding", include_memory=False, include_project=False)
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> None:
            captured.update(kwargs)

        mgr.spawn = _capture  # type: ignore[method-assign]
        mgr._max_concurrent = 4
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._drain_queue()
        assert captured["include_memory"] is False
        assert captured["include_lessons"] is True
        assert captured["include_project"] is False


class TestContinuationInheritsScope:
    """A continuation REBUILDS session context, so it must inherit the scope.

    ``get_or_create`` returns ``is_new=True`` even when it restores the session
    via ``session/load`` (``resumed`` is separate and gates only thread
    history), so ``build_message`` runs the full session-context path on a
    follow-up turn. Un-inherited, a run spawned without memory would silently
    regain it.
    """

    def test_inherits_from_the_live_record(self):
        mgr = _mgr()
        original = SubagentInfo(
            id="conv1", task="t", include_memory=False, include_project=False
        )
        mgr._agents["conv1"] = original
        assert mgr._inherited_context_groups("conv1") == (False, True, False)

    def test_falls_back_to_persisted_scope(self, monkeypatch):
        mgr = _mgr()
        monkeypatch.setattr(
            "kiro_crew.subagent.read_state",
            lambda _id: {"context_groups": "lessons,project"},
        )
        assert mgr._inherited_context_groups("gone") == (False, True, True)

    def test_all_groups_withheld_is_not_confused_with_a_legacy_run(self, monkeypatch):
        """An empty recorded scope means "all withheld", not "unknown"."""
        mgr = _mgr()
        monkeypatch.setattr(
            "kiro_crew.subagent.read_state", lambda _id: {"context_groups": ""}
        )
        assert mgr._inherited_context_groups("stripped") == (False, False, False)

    def test_run_predating_the_field_defaults_to_all_on(self, monkeypatch):
        mgr = _mgr()
        monkeypatch.setattr("kiro_crew.subagent.read_state", lambda _id: {"id": "old"})
        assert mgr._inherited_context_groups("old") == (True, True, True)

    def test_continue_conversation_forwards_the_inherited_scope(self, monkeypatch):
        """End-to-end: the flags reach spawn(), not just the helper."""
        mgr = _mgr()
        mgr._agents["conv2"] = SubagentInfo(id="conv2", task="t", include_memory=False)
        monkeypatch.setattr(mgr, "_conversation_busy", lambda _k: None)
        monkeypatch.setattr(mgr._sessions, "resumable_sid", lambda _k: "sid-1")
        monkeypatch.setattr(mgr, "_promote_conversation", lambda *_a: None)
        captured: dict[str, object] = {}
        monkeypatch.setattr(mgr, "spawn", lambda *_a, **kw: captured.update(kw))
        mgr.continue_conversation("conv2", "follow up")
        assert captured["include_memory"] is False
        assert captured["include_lessons"] is True
        assert captured["include_project"] is True


class TestScopePersistence:
    """The scope must be on disk from folder creation, not a later merge.

    A continuation resolves an evicted run's scope from ``state.json``; if the
    write were deferred to a read-modify-write after the run started, a failed
    update would silently widen the follow-up turn's scope.
    """

    def test_encoding_round_trips_through_the_helper(self):
        info = SubagentInfo(id="r1", task="t", include_memory=False)
        assert _context_groups_field(info) == "lessons,project"
        assert _context_groups_of(info) == frozenset({"lessons", "project"})

    def test_all_withheld_encodes_as_empty_string(self):
        """Distinct from a legacy run, where the key is absent entirely."""
        info = SubagentInfo(
            id="r2",
            task="t",
            include_memory=False,
            include_lessons=False,
            include_project=False,
        )
        assert _context_groups_field(info) == ""

    def test_folder_creation_records_the_scope(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.subagent_persistence._subagents_dir", lambda: tmp_path)
        create_agent_folder("r3", task="t", context_groups="lessons")
        assert (read_state("r3") or {}).get("context_groups") == "lessons"

    def test_scope_survives_to_inheritance_without_the_live_record(
        self, tmp_path, monkeypatch
    ):
        """The real write -> real read path, not a patched read_state."""
        monkeypatch.setattr("kiro_crew.subagent_persistence._subagents_dir", lambda: tmp_path)
        mgr = _mgr()
        info = SubagentInfo(id="r4", task="t", include_memory=False, include_project=False)
        mgr._log_spawned(info)
        mgr._agents.pop("r4", None)  # simulate eviction of the completed run
        assert mgr._inherited_context_groups("r4") == (False, True, False)

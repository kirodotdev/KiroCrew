"""``spawn_run``'s optional ``label``: a short display name for each task.

The task box names a run by the start of its task text, which for an
orchestrator's assignment is a long prompt. A caller-given label replaces that
name on every surface that names the run (the tool's receipt line, the
``subagent_spawn``/``subagent_done`` frames the task box folds) while the task
the sub-agent receives stays unchanged. No label keeps the task text.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_handlers_messaging_coverage import _info, _mgr, _Req, _run, _state

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.execution_context import execution_for_store
from kiro_crew.spawn_label import SPAWN_LABEL_MAX_CHARS, normalize_spawn_label
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_LONG_TASK = ("Review the retry ladder in the uploader and report every path that " * 3).strip()


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


def _run_tool(args: dict[str, Any]) -> tuple[list[dict], str]:
    """Run spawn_run against a fake gateway; return (POSTed bodies, result text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict, **_kw: Any) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return {"id": f"a{len(bodies):04x}"}
        return {}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool("spawn_run", args)
    return bodies, result


class TestNormalize:
    def test_newlines_and_runs_of_whitespace_become_one_space(self) -> None:
        assert normalize_spawn_label("  Retry\n\tladder   audit ") == "Retry ladder audit"

    def test_a_long_label_is_cut_to_the_cap(self) -> None:
        out = normalize_spawn_label("x" * 500)
        assert out == "x" * SPAWN_LABEL_MAX_CHARS

    @pytest.mark.parametrize("value", [None, 7, ["a"], {"a": 1}, "", "   \n"])
    def test_anything_unusable_is_empty(self, value: object) -> None:
        assert normalize_spawn_label(value) == ""

    def test_a_key_straddling_the_cut_is_redacted_before_the_cut(self) -> None:
        """Cut first, the label would keep the key's first characters, which no
        later redactor recognizes as a key any more."""
        key = "AKIAIOSFODNN7EXAMPLE"
        out = normalize_spawn_label("x" * 68 + key)
        assert "AKIA" not in out
        assert key[:12] not in out

    def test_a_key_anywhere_in_the_label_is_redacted(self) -> None:
        out = normalize_spawn_label("audit AKIAIOSFODNN7EXAMPLE now")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert out.startswith("audit ")


class TestSchema:
    def test_label_is_accepted_at_the_top_level_and_per_task(self) -> None:
        cleaned = validate_tool_args(
            {"label": "audit", "tasks": [{"task": "p", "label": "one"}, "q"]}, SPAWN_RUN_SCHEMA
        )
        assert cleaned["label"] == "audit"
        assert cleaned["tasks"][0] == {"task": "p", "label": "one"}

    def test_a_non_string_label_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "p", "label": 3}, SPAWN_RUN_SCHEMA)


class TestSpawnRun:
    def test_the_label_is_forwarded_and_names_the_receipt_line(self) -> None:
        bodies, result = _run_tool({"task": _LONG_TASK, "label": "Retry\nladder audit"})
        assert bodies[0]["label"] == "Retry ladder audit"
        assert bodies[0]["task"] == _LONG_TASK
        assert "  a0001: Retry ladder audit" in result
        assert _LONG_TASK[:40] not in result

    def test_without_a_label_the_receipt_keeps_the_task_text(self) -> None:
        bodies, result = _run_tool({"task": _LONG_TASK})
        assert "label" not in bodies[0]
        assert f"  a0001: {_LONG_TASK[:80]}" in result

    def test_a_per_task_label_wins_over_the_batch_wide_one(self) -> None:
        bodies, result = _run_tool(
            {"tasks": [{"task": "first", "label": "own"}, "second"], "label": "shared"}
        )
        assert [b["label"] for b in bodies] == ["own", "shared"]
        assert "  a0001: own" in result and "  a0002: shared" in result


class TestApiSpawn:
    def test_the_label_reaches_the_manager(self) -> None:
        mgr = _mgr()
        mgr.spawn.return_value = _info()
        body = {"task": "x", "label": "Retry ladder"}
        resp = _run(messaging.api_spawn, _Req(_state(subagents=mgr), body))
        assert resp.status == 200
        assert mgr.spawn.call_args.kwargs["label"] == "Retry ladder"

    def test_no_label_reaches_the_manager_as_empty(self) -> None:
        mgr = _mgr()
        mgr.spawn.return_value = _info()
        resp = _run(messaging.api_spawn, _Req(_state(subagents=mgr), {"task": "x"}))
        assert resp.status == 200
        assert mgr.spawn.call_args.kwargs["label"] == ""


def _manager() -> SubagentManager:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return SubagentManager(sessions=sessions, ctx_builder=ctx)


class TestManager:
    def test_the_record_holds_the_normalized_label(self) -> None:
        info = SubagentInfo(id="a1", task="t", label="  Retry\nladder ")
        assert info.label == "Retry ladder"

    @pytest.mark.asyncio
    async def test_spawn_records_the_label_and_keeps_the_task(self) -> None:
        manager = _manager()
        manager._run = AsyncMock()  # type: ignore[method-assign]
        info = manager.spawn(_LONG_TASK, label="Retry ladder audit")
        assert info is not None and not info.error
        assert info.label == "Retry ladder audit"
        assert info.task == _LONG_TASK

    def test_a_queued_spawn_keeps_its_label_for_the_drain(self) -> None:
        """The drain re-enters ``spawn`` from the queued params alone."""
        manager = _manager()
        manager._should_stagger_queue = MagicMock(  # type: ignore[method-assign]
            return_value=(True, False)
        )
        info = manager.spawn("waits", label="queued one")
        assert info is not None and info.queued
        assert info.label == "queued one"
        assert manager._queue[0]["label"] == "queued one"

    @pytest.mark.asyncio
    async def test_the_spawn_and_done_frames_carry_the_label(self) -> None:
        from test_subagent_provenance_write import _mock_ctx_builder, _mock_sessions

        manager = SubagentManager(
            sessions=_mock_sessions(served_model="m"),
            ctx_builder=_mock_ctx_builder(),
            is_yolo=lambda: True,
        )
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="lbl001",
            task=_LONG_TASK,
            label="Retry ladder audit",
        )
        manager._log_spawned(info)
        manager._agents[info.id] = info
        frames: dict[str, dict] = {}
        orig_fire = manager._fire_event

        async def _spy_fire(kind: str, agent: Any, extra: dict | None = None, **kw: Any) -> None:
            frames[kind] = dict(extra or {})
            await orig_fire(kind, agent, extra, **kw)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state", return_value=True),
            patch.object(manager, "_fire_event", _spy_fire),
        ):
            await manager._run_inner(info, f"subagent:{info.id}")
        assert frames["subagent_spawn"]["label"] == "Retry ladder audit"
        assert frames["subagent_spawn"]["task"] == _LONG_TASK

    def test_the_reconnect_snapshot_carries_the_label(self) -> None:
        from kiro_crew.dashboard.ws import build_subagent_snapshot

        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="lbl002",
            task=_LONG_TASK,
            label="Retry ladder audit",
        )
        snap = build_subagent_snapshot(info)
        assert snap["label"] == "Retry ladder audit"
        assert snap["task"] == _LONG_TASK

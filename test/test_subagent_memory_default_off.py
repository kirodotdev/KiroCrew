"""A child the model starts gets the memory group only when its caller asks for it.

Three doors start a child for an agent: the ``spawn_run`` / ``spawn_sub_agents``
tools, a workflow ``ctx.agent()`` node and the ``task_run`` tool. Each withholds
the memory group (preferences, facts, history, episodes, memory index) unless
the call opts in, and keeps the lessons and project groups as before. A spawn a
person starts from the dashboard (``POST /api/spawn`` with no flag) keeps every
group; ``test_subagent_context_group_boundary.py`` pins that door. A task run a
person starts (``POST /api/taskrunner`` with no flag) keeps memory too.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _request,
    _runner,
    _state,
    _stub_sel,
)
from test_background_loops_compaction_reinjection import (
    _task_ctx,
    _task_provider,
    _task_sessions,
)
from test_spawn_light_lean_context import _tool
from test_subagent_context_group_boundary import _posted
from test_workflows_agent_exec import FakeSessions
from test_workflows_agent_pool import _fake_stream, _FakeSessions

from kiro_crew.context_assembly.inclusion import (
    CHILD_GROUPS_WITHOUT_MEMORY,
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    _build_context_scope_section,
)
from kiro_crew.workflow_memory import WorkflowScope

_NO_MEMORY = frozenset({CONTEXT_GROUP_LESSONS, CONTEXT_GROUP_PROJECT})


class TestSpawnTools:
    def test_spawn_run_withholds_memory_when_the_call_names_nothing(self):
        (body,) = _posted({"task": "x"})
        assert body["include_memory"] is False
        assert "include_lessons" not in body and "include_project" not in body

    def test_spawn_run_keeps_memory_when_the_call_asks_for_it(self):
        (body,) = _posted({"task": "x", "include_memory": True})
        assert "include_memory" not in body

    def test_spawn_run_explicit_null_withholds_like_absent(self):
        (body,) = _posted({"task": "x", "include_memory": None})
        assert body["include_memory"] is False

    def test_every_member_of_a_batch_withholds_memory(self):
        bodies = _posted({"tasks": ["t1", "t2"]})
        assert [b["include_memory"] for b in bodies] == [False, False]

    def test_the_dispatcher_path_withholds_memory_too(self):
        """``_call_tool`` validates (and fills defaults) before the handler runs."""
        (body,) = _tool({"task": "x"})
        assert body["include_memory"] is False
        (body,) = _tool({"task": "x", "include_memory": True})
        assert "include_memory" not in body

    @pytest.mark.parametrize("field", ["crew", "target_member"])
    def test_a_crew_delegation_keeps_the_member_memory(self, field):
        """The member's store is not the parent's, so the parent cannot brief it in."""
        for run in (_tool, _posted):
            (body,) = run({"task": "x", field: "coder"})
            assert "include_memory" not in body
            (body,) = run({"task": "x", field: "coder", "include_memory": False})
            assert body["include_memory"] is False

    def test_spawn_sub_agents_withholds_memory_by_default(self):
        (body,) = _posted({"agents": [{"prompt": "a"}]}, tool="spawn_sub_agents")
        assert body["include_memory"] is False
        assert "include_lessons" not in body

    def test_spawn_sub_agents_keeps_memory_when_asked(self):
        (body,) = _posted(
            {"agents": [{"prompt": "a"}], "include_memory": True}, tool="spawn_sub_agents"
        )
        assert "include_memory" not in body


class TestScopeNotice:
    def test_withheld_memory_points_at_memory_recall(self):
        text = _build_context_scope_section(_NO_MEMORY)
        assert "memory_recall" in text

    def test_a_scope_that_keeps_memory_does_not_mention_it(self):
        text = _build_context_scope_section(
            frozenset({CONTEXT_GROUP_MEMORY, CONTEXT_GROUP_LESSONS})
        )
        assert "memory_recall" not in text


def _scope() -> WorkflowScope:
    return WorkflowScope("run-1", "global", "origin", "temporary", MagicMock(member_id=None))


async def _workflow_groups(**kwargs):
    scope = _scope()
    context = MagicMock()
    context.build_message = MagicMock(return_value=("full", None))
    with patch("kiro_crew.workflow_memory.bind_session_execution"):
        await scope.prompt(
            context,
            scope.worker_key("w"),
            "do it",
            is_new=True,
            agent=None,
            cwd=None,
            provider=MagicMock(),
            **kwargs,
        )
    return context.build_message.call_args.kwargs.get("context_groups")


class TestWorkflowScopePrompt:
    @pytest.mark.asyncio
    async def test_a_node_withholds_memory_by_default(self):
        assert await _workflow_groups() == _NO_MEMORY

    @pytest.mark.asyncio
    async def test_a_node_that_asks_keeps_every_group(self):
        assert await _workflow_groups(include_memory=True) is None


class _RecordingScope:
    """A memory scope that records what each turn asked for."""

    def __init__(self) -> None:
        self.asked: list[bool] = []

    def worker_key(self, label: str) -> str:
        return f"wk:{label}"

    async def prepare(self, context, key):
        return "global"

    async def validate(self) -> None:
        return None

    async def prompt(self, context, key, text, **kwargs):
        self.asked.append(kwargs["include_memory"])
        return text


class TestWorkflowExecutors:
    @pytest.mark.asyncio
    async def test_cold_step_passes_the_call_choice(self, monkeypatch):
        import kiro_crew.workflows.agent_exec as agent_exec
        from kiro_crew.workflows.agent_exec import build_agent_fn

        async def _stream(provider, message, **kw):
            return "ok"

        monkeypatch.setattr(agent_exec, "stream_and_collect", _stream)
        scope = _RecordingScope()
        fn = build_agent_fn(FakeSessions(), run_id="wf_m", memory_scope=scope)
        await fn("a", {})
        await fn("b", {"memory": True})
        assert scope.asked == [False, True]

    @pytest.mark.asyncio
    async def test_a_named_memory_step_gets_its_own_session(self, monkeypatch):
        """Memory is injected at session start, so it cannot join a session started without it."""
        import kiro_crew.workflows.agent_exec as agent_exec
        from kiro_crew.workflows.agent_exec import build_agent_fn

        async def _stream(provider, message, **kw):
            return "ok"

        monkeypatch.setattr(agent_exec, "stream_and_collect", _stream)
        sessions = FakeSessions()
        fn = build_agent_fn(sessions, run_id="wf_n")
        await fn("a", {"session": "s"})
        await fn("b", {"session": "s", "memory": True})
        await fn("c", {"session": "s"})
        assert [key for key, _ in sessions.created] == ["s", "s#memory", "s"]

    @pytest.mark.asyncio
    async def test_memory_step_skips_the_warm_pool(self, monkeypatch):
        from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

        monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _fake_stream)
        monkeypatch.setattr("kiro_crew.workflows.agent_pool.redact", lambda t: t)
        scope = _RecordingScope()
        sessions = _FakeSessions()
        agent_fn, pool = build_pooled_agent_fn(sessions, run_id="mp", memory_scope=scope)
        try:
            pooled = await agent_fn("x", {})
            unpooled = await agent_fn("y", {"memory": True})
        finally:
            await pool.shutdown()
        assert pooled.startswith("[wf-pool:mp:")
        assert unpooled.startswith("[wf-unpooled:mp:")
        assert scope.asked == [False, True]

    @pytest.mark.asyncio
    async def test_ctx_agent_names_memory_only_when_asked(self):
        """A call that does not ask keeps the opts, and so the replay fingerprint, it had."""
        from kiro_crew.workflows.runner import WorkflowRunner

        seen: list[dict] = []

        async def agent(prompt: str, opts: dict):
            seen.append(opts)
            return "ok"

        script = (
            'META = {"name": "m"}\n'
            "async def workflow(ctx):\n"
            "    await ctx.agent('a')\n"
            "    await ctx.agent('b', memory=True)\n"
        )
        res = await WorkflowRunner(agent_fn=agent).run(
            script, run_id="wf_mem", now="2026-11-27T00:00:00Z"
        )
        assert res.ok, res.error
        assert "memory" not in seen[0]
        assert seen[1]["memory"] is True


async def _task_groups(tmp_path, *, include_memory: bool | None = None):
    from kiro_crew.task_models import Project, Task
    from kiro_crew.taskrunner import TaskRunner

    sessions = _task_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_task_provider(), False, False))
    ctx = _task_ctx()
    runner = TaskRunner(sessions=sessions, context_builder=ctx, auto_test=False, work_dir=tmp_path)
    run = Project(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    if include_memory is not None:
        run.include_memory = include_memory
    step = Task(index=1, title="Write", description="d")
    run.tasks = [step]
    with patch("kiro_crew.task_executor.self_review", return_value=True):
        await runner._execute_single_task(run, step)
    return ctx.build_message.call_args.kwargs.get("context_groups")


class TestTaskStep:
    @pytest.mark.asyncio
    async def test_a_default_run_keeps_every_group(self, tmp_path):
        assert await _task_groups(tmp_path) is None

    @pytest.mark.asyncio
    async def test_a_run_that_withholds_memory_gets_lessons_and_project(self, tmp_path):
        assert await _task_groups(tmp_path, include_memory=False) == CHILD_GROUPS_WITHOUT_MEMORY

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra, expected",
        [({}, True), ({"include_memory": None}, True), ({"include_memory": False}, False)],
    )
    async def test_post_taskrunner_keeps_memory_unless_explicitly_false(
        self, tmp_path, extra, expected
    ):
        from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_start

        runner = _runner(tmp_path)
        resp = await api_taskrunner_start(
            _request(_state(runner), json_body={"spec": "__inline__:# t", **extra})
        )
        assert resp.status == 200
        assert runner.start_background.call_args.kwargs["include_memory"] is expected

    def test_a_run_persisted_without_the_key_keeps_memory(self, tmp_path):
        from kiro_crew.task_models import Project, Task
        from kiro_crew.taskrunner import TaskRunner

        runner = TaskRunner(sessions=_task_sessions(), auto_test=False, work_dir=tmp_path)
        for tid, value in (("old", True), ("off", False)):
            run = Project(
                spec_path=str(tmp_path / "s.md"), spec_content="s", status="paused", task_id=tid
            )
            run.work_dir = str(tmp_path)
            run.include_memory = value
            run.tasks = [Task(index=1, title="A", description="d")]
            runner._runs[tid] = run
        serialize = runner._serialize_runs

        def _without_old_key() -> str:
            # A row written before the flag existed carries no include_memory key.
            rows = json.loads(serialize())
            for row in rows:
                if row["task_id"] == "old":
                    del row["include_memory"]
            return json.dumps(rows)

        runner._serialize_runs = _without_old_key
        runner._persist_runs()
        again = TaskRunner(sessions=_task_sessions(), auto_test=False, work_dir=tmp_path)
        assert again._runs["old"].include_memory is True
        assert again._runs["off"].include_memory is False

    def test_the_choice_survives_a_restart(self, tmp_path):
        from kiro_crew.task_models import Project, Task
        from kiro_crew.taskrunner import TaskRunner

        runner = TaskRunner(sessions=_task_sessions(), auto_test=False, work_dir=tmp_path)
        run = Project(
            spec_path=str(tmp_path / "s.md"), spec_content="s", status="paused", task_id="t1"
        )
        run.work_dir = str(tmp_path)
        run.include_memory = False
        run.tasks = [Task(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        runner._persist_runs()
        # A fresh runner loads the same runs.json on __init__.
        again = TaskRunner(sessions=_task_sessions(), auto_test=False, work_dir=tmp_path)
        assert again._runs["t1"].include_memory is False

    def test_task_run_tool_always_sends_the_flag(self):
        from kiro_crew import mcp_core

        posted: list[dict] = []

        def _fake_post(path, body):
            posted.append(body)
            return {"task_id": "t1"}

        with (
            patch.object(mcp_core, "_post", side_effect=_fake_post),
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            mcp_core._call_tool_inner("task_run", {"spec": "__inline__:x"})
            mcp_core._call_tool_inner("task_run", {"spec": "__inline__:x", "include_memory": True})
        assert posted[0]["include_memory"] is False
        assert posted[1]["include_memory"] is True

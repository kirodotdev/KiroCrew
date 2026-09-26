"""Agent spec hooks on KAS: auto-approved calls, ``confirm: true`` hooks, and
subagent / task-runner turns.

* A PreToolUse hook runs on Crew's permission path, which an auto-approved call
  never takes, so the KAS projection withholds auto-approval for what such a hook
  covers.
* A ``confirm: true`` spec hook cannot run from Crew, and the user is told once
  per session.
* A subagent or task-runner turn gates each permission request on its
  PreToolUse hooks (the Hooks page's and ITS OWN agent spec's), only when ITS
  OWN backend never receives the spec's hooks. kiro-cli runs the field itself, so a
  kiro-cli turn gets none from Crew and nothing fires twice.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.acp import kas_agents, kas_permissions
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
)

_ALL = set(kas_permissions.AUTO_APPROVABLE_CAPABILITIES)
_PRE = {"preToolUse": [{"matcher": "web_fetch", "command": "guard.sh"}]}
_CONFIRM_DOCS = [
    {"name": "on", "trigger": "PreToolUse", "action": {"type": "command", "command": "a"}},
    {
        "name": "ask",
        "trigger": "PreToolUse",
        "confirm": True,
        "action": {"type": "command", "command": "b"},
    },
    {
        "name": "ask too",
        "trigger": "Stop",
        "confirm": True,
        "action": {"type": "command", "command": "c"},
    },
]


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _write_spec(agents_dir: Path, name: str, **fields) -> None:
    spec = {"name": name, "prompt": "p", **fields}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _provider(backend: str, cwd: str = "") -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend), cwd=cwd)


def _rules(policy) -> list[tuple[str, str]]:
    return [(r["capability"], r["effect"]) for r in (policy or {}).get("rules", [])]


# ── item 1: an auto-approved call is withheld for a PreToolUse hook ──────────


@pytest.mark.parametrize(
    ("matchers", "covered"),
    [
        ((), set()),
        (("",), _ALL),
        (("*",), _ALL),
        (("web_fetch",), {"web_fetch"}),
        (("WEB_FETCH",), {"web_fetch"}),
        (("execute_bash",), set()),
        (("fs_write", "grep"), set()),
        (("mcp__github__create_issue",), {"mcp"}),
        (("@github/create_issue",), {"mcp"}),
        (("WebFetch",), _ALL),
        (("web_*",), {"web_fetch", "web_search", "mcp"}),
        (("execute_*",), {"mcp"}),
        (("invoke_sub_agent", "disclose_context"), {"subagent", "skill"}),
        (("use_subagent",), {"subagent"}),
        (("invoke_*",), {"subagent", "mcp"}),
    ],
)
def test_the_capabilities_a_pre_tool_matcher_covers(matchers, covered):
    assert kas_permissions.hook_gated_capabilities(matchers) == covered


def test_an_ask_outranks_the_allow_a_hook_covers():
    policy = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(policy, ("web_fetch",))
    assert _rules(out) == [("web_fetch", "allow"), ("web_fetch", "ask")]
    # The input is left as it was.
    assert _rules(policy) == [("web_fetch", "allow")]


def test_every_withheld_capability_is_audited():
    audited: list = []
    policy = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
    kas_permissions.withhold_hook_gated_auto_approval(
        policy,
        ("web_*",),
        audit_decision=lambda refs, outcome, reason: audited.append((refs, outcome)),
    )
    assert audited == [("mcp", "withheld"), ("web_fetch", "withheld"), ("web_search", "withheld")]


def test_the_projection_audits_a_hook_gated_withhold(monkeypatch):
    audited: list = []
    monkeypatch.setattr(
        kas_agents,
        "_audit_permission_decision",
        lambda refs, outcome, reason, agent_id: audited.append((refs, outcome, agent_id)),
    )
    kas_agents.to_client_custom_agent(
        "a1", {"allowedTools": ["web_fetch"]}, "p", pre_tool_hook_matchers=("web_fetch",)
    )
    assert ("web_fetch", "withheld", "a1") in audited


def test_no_hook_or_no_policy_changes_nothing():
    policy = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
    assert kas_permissions.withhold_hook_gated_auto_approval(policy, ()) is policy
    assert kas_permissions.withhold_hook_gated_auto_approval(policy, ("execute_bash",)) is policy
    assert kas_permissions.withhold_hook_gated_auto_approval(None, ("*",)) is None


def test_the_projection_withholds_what_a_pre_tool_hook_covers():
    spec = {"allowedTools": ["web_fetch", "web_search", "@srv"]}
    plain = kas_agents.to_client_custom_agent("a1", spec, "p")
    gated = kas_agents.to_client_custom_agent(
        "a1", spec, "p", pre_tool_hook_matchers=("web_fetch",)
    )
    assert ("web_fetch", "ask") not in _rules(plain["permissions"])
    assert ("web_fetch", "ask") in _rules(gated["permissions"])
    assert ("web_search", "ask") not in _rules(gated["permissions"])
    assert ("mcp", "ask") not in _rules(gated["permissions"])


def test_the_built_batch_reads_the_specs_own_pre_tool_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: None)
    spec = {"prompt": "p", "allowedTools": ["web_fetch"], "hooks": _PRE}
    (entry,) = kas_agents.build_kas_custom_agents(tmp_path, "a1", spec)
    assert ("web_fetch", "ask") in _rules(entry["permissions"])


def test_the_projection_lists_spec_and_hooks_page_pre_tool_matchers(tmp_path, monkeypatch):
    store = ScriptHookStore(config_dir=tmp_path)
    store._hooks = {
        "p1": ScriptHook(id="p1", event=HOOK_EVENT_PRE_TOOL_USE, matcher="web_*", command="x"),
        "p2": ScriptHook(id="p2", event=HOOK_EVENT_PRE_TOOL_USE, command="x", enabled=False),
        "p3": ScriptHook(id="p3", event=HOOK_EVENT_PRE_TOOL_USE, skills=["s"]),
        "p4": ScriptHook(id="p4", event=HOOK_EVENT_POST_TOOL_USE, command="x"),
    }
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: store)
    matchers = kas_agents.pre_tool_hook_matchers("a1", {"hooks": _PRE})
    assert sorted(matchers) == ["web_*", "web_fetch"]


def test_the_projection_gates_everything_when_it_cannot_list_hooks(monkeypatch):
    def boom():
        raise RuntimeError("store broken")

    monkeypatch.setattr(kas_agents, "get_global_hook_store", boom)
    assert kas_agents.pre_tool_hook_matchers("a1", {}) == ("*",)


# ── item 2: a confirm: true spec hook is named to the user ───────────────────


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda state, slot, role, text, cls: seen.append(text)
    )
    return seen


def _prepare(provider, agent, *, is_new):
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), provider, agent, is_new=is_new
        )
    )


def test_confirm_hooks_get_one_notice_per_session(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_CONFIRM_DOCS)
    hooks, unreadable, _cwd = _prepare(_provider(ACP_BACKEND_KAS), "a1", is_new=True)
    assert [h.command for h in hooks] == ["a"]
    assert unreadable is False
    assert len(notices) == 1
    assert "2 hooks ask to be confirmed" in notices[0]
    notices.clear()
    _prepare(_provider(ACP_BACKEND_KAS), "a1", is_new=False)
    assert notices == []


def test_no_confirm_notice_without_confirm_hooks_or_on_kiro_cli(agents_dir, notices):
    _write_spec(agents_dir, "plain", hooks=_PRE)
    _write_spec(agents_dir, "a1", hooks=_CONFIRM_DOCS)
    _prepare(_provider(ACP_BACKEND_KAS), "plain", is_new=True)
    _prepare(_provider(ACP_BACKEND_KIRO), "a1", is_new=True)
    assert notices == []


def test_one_confirm_hook_reads_in_the_singular():
    assert "1 hook asks to be" in chat_runner._spec_confirm_hooks_notice("a1", 1)


# ── item 5: subagent and task-runner turns gate on PreToolUse hooks ──────────


def test_a_kiro_cli_turn_is_not_gated_and_reads_no_spec(monkeypatch):
    def must_not_read(_agent):
        raise AssertionError("a kiro-cli turn must not read the spec")

    monkeypatch.setattr(spec_hooks, "crew_fired_spec_hooks", must_not_read)
    out = asyncio.run(spec_hooks.turn_spec_hooks(_provider(ACP_BACKEND_KIRO, "/w"), "a1"))
    assert out == spec_hooks.TurnSpecHooks([], None, False, False)


def test_a_kas_turn_gets_its_own_agents_spec_hooks(agents_dir):
    _write_spec(agents_dir, "worker", hooks=_PRE)
    out = asyncio.run(spec_hooks.turn_spec_hooks(_provider(ACP_BACKEND_KAS, "/w"), "worker"))
    assert [(h.event, h.matcher, h.command) for h in out.hooks] == [
        (HOOK_EVENT_PRE_TOOL_USE, "web_fetch", "guard.sh")
    ]
    assert (out.cwd, out.unreadable, out.gated) == ("/w", False, True)


def test_a_kas_turn_with_no_agent_at_all_blocks_its_permission_requests():
    out = asyncio.run(spec_hooks.turn_spec_hooks(_provider(ACP_BACKEND_KAS, "/w"), ""))
    assert out == spec_hooks.TurnSpecHooks([], "/w", True, True)


def test_a_turn_that_names_no_agent_meets_the_sessions_agent(agents_dir):
    _write_spec(agents_dir, "worker", hooks=_PRE)
    provider = _provider(ACP_BACKEND_KAS, "/w")
    provider.kas_projected_agent = "worker"
    assert spec_hooks.session_agent(provider, "") == "worker"
    # An agent the turn names (an in-turn switch) wins over the recorded one.
    assert spec_hooks.session_agent(provider, "other") == "other"
    out = asyncio.run(spec_hooks.turn_spec_hooks(provider, ""))
    assert [h.command for h in out.hooks] == ["guard.sh"]
    assert (out.unreadable, out.gated) == (False, True)


def test_a_kas_turn_with_an_unreadable_spec_says_so(agents_dir):
    (agents_dir / "worker.json").write_text("{not json", encoding="utf-8")
    out = asyncio.run(spec_hooks.turn_spec_hooks(_provider(ACP_BACKEND_KAS, "/w"), "worker"))
    assert out == spec_hooks.TurnSpecHooks([], "/w", True, True)


def _spec_hook(matcher: str = "Read*") -> ScriptHook:
    return ScriptHook(
        id="spec:a1:PreToolUse:0", event=HOOK_EVENT_PRE_TOOL_USE, matcher=matcher, command="g"
    )


@pytest.mark.parametrize(("exit_code", "blocked"), [(0, False), (2, True), (1, True), (-1, True)])
def test_the_gate_runs_stored_and_spec_hooks_and_blocks_on_no_verdict(
    tmp_path, monkeypatch, exit_code, blocked
):
    ran: list = []

    async def fake_run(hook, context="", hook_event=None, cwd=None):
        ran.append((hook.command, hook_event.get("tool_name"), cwd))
        return ScriptHookResult(
            hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=exit_code
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    store = ScriptHookStore(config_dir=tmp_path)
    store._hooks = {"page": ScriptHook(id="page", event=HOOK_EVENT_PRE_TOOL_USE, command="p")}
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(store, [_spec_hook()], "/w", "Running: ReadFile")
    )
    assert ran == [("p", "ReadFile", None), ("g", "ReadFile", "/w")]
    assert (reason is not None) is blocked


def _gate_runs(tmp_path, monkeypatch, matcher, title, **identity) -> list:
    ran: list = []

    async def fake_run(hook, context="", hook_event=None, cwd=None):
        ran.append(hook_event.get("tool_name"))
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=2)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    store = ScriptHookStore(config_dir=tmp_path)
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(store, [_spec_hook(matcher)], None, title, **identity)
    )
    assert (reason is not None) is bool(ran)
    return ran


def test_the_gate_matches_the_canonical_tool_name_as_well_as_the_title(tmp_path, monkeypatch):
    ran = _gate_runs(
        tmp_path, monkeypatch, "use_subagent", "Spawn a helper", tool_identity="use_subagent"
    )
    # The payload names the call as the chat loop does; the matcher met its tool name.
    assert ran == ["Spawn a helper"]
    assert _gate_runs(tmp_path, monkeypatch, "Spawn*", "Spawn a helper", tool_identity="x")


@pytest.mark.parametrize(
    ("matcher", "runs"),
    [
        ("@github/create_issue", True),
        ("mcp__github__create_issue", True),
        ("create_issue", True),
        ("@github/*", True),
        ("@other/create_issue", False),
    ],
)
def test_the_gate_matches_an_mcp_call_by_its_server_identity(tmp_path, monkeypatch, matcher, runs):
    ran = _gate_runs(
        tmp_path,
        monkeypatch,
        matcher,
        "Running: Create issue",
        tool_identity="create_issue",
        mcp_server="github",
    )
    assert bool(ran) is runs


def test_the_gate_with_no_store_blocks_only_when_spec_hooks_were_loaded():
    assert asyncio.run(hooks_mod.permission_pre_tool_block(None, [], None, "x")) is None
    assert (
        asyncio.run(hooks_mod.permission_pre_tool_block(None, [_spec_hook()], None, "x"))
        is not None
    )


def _deny_result(*_a, **kwargs):
    return [
        ScriptHookResult(hook_id="h", hook_name="h", event=HOOK_EVENT_PRE_TOOL_USE, exit_code=2)
    ]


def _subagent_run(backend: str, monkeypatch, *, unreadable: bool = False, spec: bool = True):
    """One subagent run on *backend*; every PreToolUse fire it makes denies."""
    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.providers.base import (
        EVENT_PERMISSION_REQUEST,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        LLMEvent,
    )
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    def read(agent):
        if unreadable:
            raise OSError("unreadable")
        return ([_spec_hook("*")] if spec else []), [], 0

    monkeypatch.setattr(spec_hooks, "crew_fired_spec_hooks", read)

    async def _stream(*_a, **_kw):
        # KAS sends the tool-call frame BEFORE the permission request.
        yield LLMEvent(kind=EVENT_TOOL_CALL, title="Running: ReadFile", tool_call_id="t-1")
        yield LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: ReadFile",
            request_id="r-1",
            tool_call_id="t-1",
        )
        yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t-1", tool_output="ok")

    provider = AsyncMock()
    provider.capabilities = capabilities_for(backend)
    provider.cwd = "/w"
    provider.kas_auto_approved_capabilities = None
    provider.kas_projected_agent = ""
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.reset = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.auto_approve_subagent_spawn = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    manager.hook_store = MagicMock()
    manager.hook_store.fire = AsyncMock(side_effect=_deny_result)
    info = SubagentInfo(
        execution_context=execution_for_store("", template_id="worker"),
        id="t01",
        task="test",
        parent_session_key="dashboard:parent",
    )
    manager._log_spawned(info)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        asyncio.run(manager._run_inner(info, "subagent:t01"))
    return manager.hook_store.fire, provider


def _pre_calls(fire) -> tuple[list, list]:
    """(permission-gate fires, informational tool-call fires) of PreToolUse."""
    pre = [c for c in fire.await_args_list if c.args[0] == HOOK_EVENT_PRE_TOOL_USE]
    return [c for c in pre if "extra_hooks" in c.kwargs], [
        c for c in pre if "extra_hooks" not in c.kwargs
    ]


@pytest.mark.usefixtures("healthy_host_memory")
def test_a_kas_subagent_denies_on_its_permission_request_and_fires_once(monkeypatch):
    fire, provider = _subagent_run(ACP_BACKEND_KAS, monkeypatch)
    gate, informational = _pre_calls(fire)
    assert len(gate) == 1 and informational == []
    assert [h.id for h in gate[0].kwargs["extra_hooks"]] == ["spec:a1:PreToolUse:0"]
    assert gate[0].kwargs["extra_hooks_cwd"] == "/w"
    provider.reject_tool.assert_awaited_once_with("r-1")
    provider.approve_tool.assert_not_awaited()
    (post,) = [c for c in fire.await_args_list if c.args[0] == HOOK_EVENT_POST_TOOL_USE]
    assert [h.id for h in post.kwargs["extra_hooks"]] == ["spec:a1:PreToolUse:0"]


@pytest.mark.usefixtures("healthy_host_memory")
def test_a_kas_subagent_honours_a_hooks_page_deny_without_spec_hooks(monkeypatch):
    fire, provider = _subagent_run(ACP_BACKEND_KAS, monkeypatch, spec=False)
    gate, informational = _pre_calls(fire)
    assert len(gate) == 1 and list(gate[0].kwargs["extra_hooks"]) == []
    assert informational == []
    provider.reject_tool.assert_awaited_once_with("r-1")


@pytest.mark.usefixtures("healthy_host_memory")
def test_a_kas_subagent_with_an_unreadable_spec_denies_the_request(monkeypatch):
    fire, provider = _subagent_run(ACP_BACKEND_KAS, monkeypatch, unreadable=True)
    gate, _informational = _pre_calls(fire)
    assert gate == []
    provider.reject_tool.assert_awaited_once_with("r-1")


@pytest.mark.usefixtures("healthy_host_memory")
def test_a_kiro_cli_subagent_is_not_gated_and_gets_no_spec_hooks(monkeypatch):
    fire, _provider_ = _subagent_run(ACP_BACKEND_KIRO, monkeypatch)
    gate, informational = _pre_calls(fire)
    assert gate == []
    assert len(informational) == 1
    for call in fire.await_args_list:
        assert list(call.kwargs.get("extra_hooks", ())) == []


def _task_step(backend: str, monkeypatch, *, agent: str = "worker") -> tuple[list, list, list]:
    """One task-runner step on *backend*; every PreToolUse fire it makes denies."""
    from kiro_crew import task_executor
    from kiro_crew.acp.types import STOP_REASON_END_TURN, TurnUsage
    from kiro_crew.providers.base import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TOOL_CALL,
        LLMEvent,
    )
    from kiro_crew.task_models import Project, Task

    monkeypatch.setattr(
        spec_hooks, "crew_fired_spec_hooks", lambda agent: ([_spec_hook("*")], [], 0)
    )
    store = MagicMock()
    store.fire = AsyncMock(side_effect=_deny_result)
    monkeypatch.setattr(task_executor, "get_global_hook_store", lambda: store)
    informational: list = []

    async def spy(*args, **kwargs):
        informational.append(kwargs)

    monkeypatch.setattr(task_executor, "fire_tool_hooks", spy)
    monkeypatch.setattr(task_executor, "check_context", AsyncMock())
    monkeypatch.setattr(
        task_executor, "build_task_prompt", AsyncMock(side_effect=lambda run, t, *a, **k: "go")
    )
    fake_config = MagicMock()
    fake_config.load.return_value = SimpleNamespace(agent=SimpleNamespace(provider="acp"))
    monkeypatch.setattr(task_executor, "KiroCrewConfig", fake_config)
    rejected: list = []

    class _Client:
        capabilities = capabilities_for(backend)
        cwd = "/w"

        def context_usage_pct(self):
            return 0.0

        async def reject_tool(self, request_id):
            rejected.append(request_id)

        async def approve_tool(self, request_id):
            raise AssertionError("a denied call must not be approved")

        async def stream(self, prompt):
            yield LLMEvent(kind=EVENT_TOOL_CALL, title="Running: ReadFile", tool_call_id="t-1")
            yield LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="Running: ReadFile",
                request_id="r-1",
                tool_call_id="t-1",
            )
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                usage=TurnUsage(duration_ms=0),
            )

    run = Project(spec_path="spec.md", spec_content="body")
    run.task_id = "task-spec-hooks"
    task = Task(index=1, title="t", description="d")
    run.tasks = [task]
    run.branch_name = ""
    run.work_dir = ""
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(_Client(), True, False))
    sessions.reset = AsyncMock()
    sessions.record_failure = AsyncMock()
    asyncio.run(
        task_executor.execute_task(
            run, task, sessions, None, agent, None, False, None, "", AsyncMock(), "tk"
        )
    )
    gate = [c for c in store.fire.await_args_list if "extra_hooks" in c.kwargs]
    return rejected, gate, informational


def test_a_kas_task_step_with_no_agent_reads_the_default_agents_spec(monkeypatch):
    from kiro_crew import task_executor
    from kiro_crew.acp.client import CLIENT_NAME

    asked: list = []
    real = spec_hooks.turn_spec_hooks

    async def spy(provider, agent_id):
        asked.append(agent_id)
        return await real(provider, agent_id)

    monkeypatch.setattr(task_executor, "turn_spec_hooks", spy)
    _task_step(ACP_BACKEND_KAS, monkeypatch, agent="")
    assert asked and set(asked) == {CLIENT_NAME}


def test_a_kas_task_step_denies_on_its_permission_request_and_fires_once(monkeypatch):
    rejected, gate, informational = _task_step(ACP_BACKEND_KAS, monkeypatch)
    assert rejected == ["r-1"]
    assert gate and [h.id for h in gate[0].kwargs["extra_hooks"]] == ["spec:a1:PreToolUse:0"]
    assert informational == []


@pytest.mark.parametrize(
    ("backend", "outcome"), [(ACP_BACKEND_KAS, "invoked"), (ACP_BACKEND_KIRO, "auto_approved")]
)
def test_a_task_step_audits_a_gated_tool_call_frame_as_not_approved(monkeypatch, backend, outcome):
    from kiro_crew import task_executor

    logged: list = []
    audit = MagicMock()
    audit.log_tool_invocation = lambda **kw: logged.append(kw)
    monkeypatch.setattr(task_executor, "sel", lambda: audit)
    _task_step(backend, monkeypatch)
    frames = [kw for kw in logged if kw.get("outcome") in ("invoked", "auto_approved")]
    assert frames and {kw["outcome"] for kw in frames} == {outcome}


def test_a_kiro_cli_task_step_is_not_gated_and_gets_no_spec_hooks(monkeypatch):
    rejected, gate, informational = _task_step(ACP_BACKEND_KIRO, monkeypatch)
    assert gate == []
    assert informational and all("extra_hooks" not in kwargs for kwargs in informational)


# ── a hook added mid-session re-projects the live KAS session ────────────────


@pytest.mark.parametrize(
    ("rules", "approved"),
    [
        ([{"capability": "web_fetch", "effect": "allow"}], {"web_fetch"}),
        (
            [
                {"capability": "web_fetch", "effect": "allow"},
                {"capability": "web_fetch", "effect": "ask"},
            ],
            set(),
        ),
        (
            [
                {"capability": "web_fetch", "effect": "allow"},
                {"capability": "web_fetch", "match": ["x.test"], "effect": "ask"},
            ],
            {"web_fetch"},
        ),
        ([{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}], {"mcp"}),
        ([{"capability": "web_search", "effect": "deny"}], set()),
    ],
)
def test_what_a_projected_policy_auto_approves(rules, approved):
    assert kas_permissions.auto_approved_capabilities({"rules": rules}) == approved
    assert kas_permissions.auto_approved_capabilities(None) == frozenset()


def test_the_projection_answer_is_the_active_agents():
    batch = [
        {"id": "other", "permissions": {"rules": [{"capability": "mcp", "effect": "allow"}]}},
        {"id": "a1", "permissions": {"rules": [{"capability": "web_fetch", "effect": "allow"}]}},
    ]
    assert kas_agents.projected_auto_approved(batch, "a1") == {"web_fetch"}
    assert kas_agents.projected_auto_approved(None, "a1") is None


def test_the_provider_reads_the_sessions_projection_through():
    from kiro_crew.providers.acp import AcpProvider

    provider = object.__new__(AcpProvider)
    provider._client = SimpleNamespace(kas_auto_approved_capabilities=frozenset({"web_fetch"}))
    assert provider.kas_auto_approved_capabilities == {"web_fetch"}
    provider._client = SimpleNamespace()
    assert provider.kas_auto_approved_capabilities is None


def _live(backend: str, approved) -> SimpleNamespace:
    return SimpleNamespace(
        capabilities=capabilities_for(backend), kas_auto_approved_capabilities=approved
    )


@pytest.fixture
def hooks_page(tmp_path, monkeypatch) -> ScriptHookStore:
    store = ScriptHookStore(config_dir=tmp_path / "hooks")
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: store)
    return store


def _add_hooks_page_hook(store: ScriptHookStore, matcher: str = "web_fetch") -> None:
    store._hooks["page"] = ScriptHook(
        id="page", event=HOOK_EVENT_PRE_TOOL_USE, matcher=matcher, command="guard.sh"
    )


def test_a_new_hook_on_an_auto_approved_capability_makes_the_session_stale(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    live = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "a1")) is False
    _add_hooks_page_hook(hooks_page, "web_search")
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "a1")) is False
    _add_hooks_page_hook(hooks_page, "web_fetch")
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "a1")) is True


def test_a_new_spec_hook_makes_the_session_stale(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"], hooks=_PRE)
    live = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "a1")) is True


def test_staleness_is_false_off_kas_or_without_a_projection(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    _add_hooks_page_hook(hooks_page)
    assert (
        asyncio.run(
            spec_hooks.hook_projection_stale(
                _live(ACP_BACKEND_KIRO, frozenset({"web_fetch"})), "a1"
            )
        )
        is False
    )
    assert (
        asyncio.run(spec_hooks.hook_projection_stale(_live(ACP_BACKEND_KAS, None), "a1")) is False
    )


def test_an_unreadable_spec_re_projects(agents_dir, hooks_page):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    live = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "a1")) is True


class _Sessions:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.reset = AsyncMock(return_value=True)

    def get_provider(self, key):
        return self.provider


def test_a_hook_added_mid_session_gates_the_next_turns_auto_approved_call(agents_dir, hooks_page):
    """The session registers a batch that auto-approves web_fetch; a Hooks-page hook
    on web_fetch is added; the next turn resets the session, and the batch the claim
    registers in its place asks for web_fetch, so the call reaches the gate."""
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    spec = kas_agents.load_agent_spec(agents_dir, "a1")
    first = kas_agents.build_kas_custom_agents(agents_dir, "a1", spec)
    live = _live(ACP_BACKEND_KAS, kas_agents.projected_auto_approved(first, "a1"))
    sessions = _Sessions(live)
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(sessions, "k", "a1")) == (
        spec_hooks.PROJECTION_FRESH
    )
    sessions.reset.assert_not_awaited()

    _add_hooks_page_hook(hooks_page)
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(sessions, "k", "a1")) == (
        spec_hooks.PROJECTION_RESET
    )
    sessions.reset.assert_awaited_once_with("k", skip_if_busy=True)

    second = kas_agents.build_kas_custom_agents(agents_dir, "a1", spec)
    assert ("web_fetch", "ask") in _rules(second[0]["permissions"])
    live.kas_auto_approved_capabilities = kas_agents.projected_auto_approved(second, "a1")
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(sessions, "k", "a1")) == (
        spec_hooks.PROJECTION_FRESH
    )
    assert sessions.reset.await_count == 1


def test_no_live_session_means_nothing_to_invalidate():
    sessions = _Sessions(None)
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(sessions, "k", "a1")) == (
        spec_hooks.PROJECTION_FRESH
    )
    sessions.reset.assert_not_awaited()


# ── the busy-session race: the decision is made again under the lease ───────


def test_invalidation_tells_busy_apart_from_fresh(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    stale = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    fresh = _Sessions(stale)
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(fresh, "k", "a1")) == (
        spec_hooks.PROJECTION_FRESH
    )
    _add_hooks_page_hook(hooks_page)
    idle = _Sessions(stale)
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(idle, "k", "a1")) == (
        spec_hooks.PROJECTION_RESET
    )
    busy = _Sessions(stale)
    busy.reset = AsyncMock(return_value=False)
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(busy, "k", "a1")) == (
        spec_hooks.PROJECTION_BUSY
    )


def test_a_turn_that_waited_behind_a_busy_stale_session_re_projects_before_running(
    agents_dir, hooks_page
):
    """Hook added while turn A holds the session: turn B's pre-claim reset is
    declined (busy), B's claim then gets the SAME stale session once A is done, and
    the claim-time re-check resets it and claims a freshly projected one."""
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    spec = kas_agents.load_agent_spec(agents_dir, "a1")
    stale = _live(
        ACP_BACKEND_KAS,
        kas_agents.projected_auto_approved(
            kas_agents.build_kas_custom_agents(agents_dir, "a1", spec), "a1"
        ),
    )
    _add_hooks_page_hook(hooks_page)
    fresh = _live(
        ACP_BACKEND_KAS,
        kas_agents.projected_auto_approved(
            kas_agents.build_kas_custom_agents(agents_dir, "a1", spec), "a1"
        ),
    )
    sessions = _Sessions(stale)
    sessions.reset = AsyncMock(side_effect=lambda key, **kw: not kw.get("skip_if_busy"))
    assert asyncio.run(spec_hooks.invalidate_stale_kas_session(sessions, "k", "a1")) == (
        spec_hooks.PROJECTION_BUSY
    )
    claims: list = []

    async def claim():
        claims.append("claim")
        return fresh, True, False

    held = asyncio.run(
        spec_hooks.reproject_claimed_session(sessions, "k", "a1", (stale, False, True), claim)
    )
    assert held == (fresh, True, False)
    assert claims == ["claim"]
    assert sessions.reset.await_args_list[-1].args == ("k",)
    assert sessions.reset.await_args_list[-1].kwargs == {}


def test_a_fresh_claim_is_kept_as_is(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    live = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    sessions = _Sessions(live)
    claim = AsyncMock()
    held = asyncio.run(
        spec_hooks.reproject_claimed_session(sessions, "k", "a1", (live, False, False), claim)
    )
    assert held == (live, False, False)
    sessions.reset.assert_not_awaited()
    claim.assert_not_awaited()


def test_a_session_that_stays_stale_refuses_the_turn(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    _add_hooks_page_hook(hooks_page)
    stale = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    sessions = _Sessions(stale)

    async def claim():
        return stale, True, False

    with pytest.raises(spec_hooks.StaleProjectionError):
        asyncio.run(
            spec_hooks.reproject_claimed_session(sessions, "k", "a1", (stale, False, False), claim)
        )
    assert sessions.reset.await_count == spec_hooks._MAX_REPROJECTIONS


def test_every_provider_declares_what_its_batch_auto_approves():
    from kiro_crew.providers.base import LLMProvider

    assert "kas_auto_approved_capabilities" in vars(LLMProvider)
    assert LLMProvider.kas_auto_approved_capabilities.fget(object()) is None


def _chat_turn(tmp_path, monkeypatch, reproject):
    """Drive one chat turn with a spied claim; returns (order, sessions)."""
    from chat_test_helpers import _make_state

    from kiro_crew.agent_discovery import clear_list_agents_cache
    from kiro_crew.config.loader import refresh_materialized_agents
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    d = kiro_agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "helper.json").write_text(json.dumps({"name": "helper"}), encoding="utf-8")
    clear_list_agents_cache()
    refresh_materialized_agents()
    order: list = []

    async def pre(sessions, session_key, agent_id):
        order.append(("check", session_key, agent_id))
        return spec_hooks.PROJECTION_FRESH

    monkeypatch.setattr(chat_runner, "invalidate_stale_kas_session", pre)

    async def post(sessions, session_key, agent_id, claimed, claim):
        order.append(("recheck", session_key, agent_id))
        return await reproject(claimed, claim)

    monkeypatch.setattr(chat_runner, "reproject_claimed_session", post)
    state = _make_state(tmp_path)
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=50.0)
    client.shutdown = AsyncMock()

    async def claim(key, **kwargs):
        order.append(("claim", key))
        return client, False, False

    state.sessions.get_or_create = AsyncMock(side_effect=claim)
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("spec-hooks-reproject-slot")
    slot.agent = "helper"

    async def _stream(msg):
        order.append(("stream",))
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = _stream
    client.stream_command = _stream

    async def run():
        try:
            await chat_runner._run_chat(state, slot, "hello")
        finally:
            tasks = list(state._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    return order, state.sessions


def test_a_chat_turn_re_checks_the_claimed_session_before_its_tools_run(tmp_path, monkeypatch):
    async def keep(claimed, claim):
        return claimed

    order, _sessions = _chat_turn(tmp_path, monkeypatch, keep)
    kinds = [o[0] for o in order]
    assert kinds.index("check") < kinds.index("claim") < kinds.index("recheck")
    assert kinds.index("recheck") < kinds.index("stream")


def test_a_chat_turn_refused_as_stale_hands_its_lease_back(tmp_path, monkeypatch):
    async def refuse(claimed, claim):
        raise spec_hooks.StaleProjectionError("still stale")

    order, sessions = _chat_turn(tmp_path, monkeypatch, refuse)
    assert ("stream",) not in order
    sessions.release.assert_called()


def test_the_turn_loop_meets_the_sessions_agent_when_the_turn_names_none(agents_dir, notices):
    _write_spec(agents_dir, "worker", hooks=_PRE)
    provider = _provider(ACP_BACKEND_KAS)
    provider.kas_projected_agent = "worker"
    hooks, unreadable, _cwd = _prepare(provider, "", is_new=False)
    assert [h.command for h in hooks] == ["guard.sh"] and unreadable is False


def test_the_turn_loop_fails_closed_when_no_agent_is_known(agents_dir, notices):
    hooks, unreadable, _cwd = _prepare(_provider(ACP_BACKEND_KAS), "", is_new=False)
    assert hooks == [] and unreadable is True


def test_staleness_reads_the_sessions_agent_when_the_turn_names_none(agents_dir, hooks_page):
    _write_spec(agents_dir, "worker", allowedTools=["web_fetch"])
    live = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    live.kas_projected_agent = "worker"
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "")) is False
    _add_hooks_page_hook(hooks_page)
    assert asyncio.run(spec_hooks.hook_projection_stale(live, "")) is True


def test_the_re_claim_adopts_the_new_permit(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    _add_hooks_page_hook(hooks_page)
    stale = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    fresh = _live(ACP_BACKEND_KAS, frozenset())
    sessions = _Sessions(stale)
    events: list = []
    sessions.reset = AsyncMock(side_effect=lambda key, **kw: events.append("reset") or True)
    sessions.adopt_turn = lambda key: events.append(("adopt", key))

    async def claim():
        events.append("claim")
        return fresh, True, False

    asyncio.run(
        spec_hooks.reproject_claimed_session(sessions, "k", "a1", (stale, False, False), claim)
    )
    assert events == ["reset", "claim", ("adopt", "k")]


def test_every_provider_declares_the_agent_its_batch_was_built_for():
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.providers.base import LLMProvider

    assert LLMProvider.kas_projected_agent.fget(object()) == ""
    provider = object.__new__(AcpProvider)
    provider._client = SimpleNamespace(kas_projected_agent="worker")
    assert provider.kas_projected_agent == "worker"
    provider._client = SimpleNamespace()
    assert provider.kas_projected_agent == ""


def test_a_kas_mode_switch_moves_the_sessions_agent_and_its_hooks(agents_dir, notices):
    """KAS session on agent A; a current_mode_update names B: the next call is gated
    by B's spec hooks, not A's, whether or not the turn names B."""
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.types import EVENT_AGENT_SWITCHED, UPDATE_CURRENT_MODE

    _write_spec(agents_dir, "agent-a", hooks={"preToolUse": [{"command": "a-guard.sh"}]})
    _write_spec(agents_dir, "agent-b", hooks={"preToolUse": [{"command": "b-guard.sh"}]})
    handle = object.__new__(AcpSessionHandle)
    handle._last_kas_mode_id = "agent-a"
    handle.kas_projected_agent = "agent-a"
    (event,) = handle._handle_kas_update(UPDATE_CURRENT_MODE, {"currentModeId": "agent-b"})
    assert event.kind == EVENT_AGENT_SWITCHED and event.text == "agent-b"
    assert handle.kas_projected_agent == "agent-b"

    provider = _provider(ACP_BACKEND_KAS)
    provider.kas_projected_agent = handle.kas_projected_agent
    named, _, _ = _prepare(provider, "agent-b", is_new=False)
    unnamed, _, _ = _prepare(provider, "", is_new=False)
    assert [h.command for h in named] == ["b-guard.sh"]
    assert [h.command for h in unnamed] == ["b-guard.sh"]


def test_an_explicit_agent_wins_over_a_stale_recorded_one(agents_dir, notices):
    _write_spec(agents_dir, "agent-a", hooks={"preToolUse": [{"command": "a-guard.sh"}]})
    _write_spec(agents_dir, "agent-b", hooks={"preToolUse": [{"command": "b-guard.sh"}]})
    provider = _provider(ACP_BACKEND_KAS)
    provider.kas_projected_agent = "agent-a"
    hooks, _, _ = _prepare(provider, "agent-b", is_new=False)
    assert [h.command for h in hooks] == ["b-guard.sh"]


def test_a_mode_frame_on_a_session_with_no_batch_records_nothing():
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.types import UPDATE_CURRENT_MODE

    handle = object.__new__(AcpSessionHandle)
    handle._last_kas_mode_id = ""
    handle.kas_projected_agent = ""
    handle._handle_kas_update(UPDATE_CURRENT_MODE, {"currentModeId": "agent-b"})
    assert handle.kas_projected_agent == ""


def test_a_stale_new_shared_session_is_replaced(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    _add_hooks_page_hook(hooks_page)
    stale = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    stale.shutdown = AsyncMock()
    fresh = _live(ACP_BACKEND_KAS, frozenset())
    recreate = AsyncMock(return_value=fresh)
    out = asyncio.run(spec_hooks.replace_stale_shared_session(stale, "a1", recreate))
    assert out is fresh
    stale.shutdown.assert_awaited_once()
    recreate.assert_awaited_once()


def test_a_fresh_shared_session_is_kept(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    live = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    recreate = AsyncMock()
    assert asyncio.run(spec_hooks.replace_stale_shared_session(live, "a1", recreate)) is live
    recreate.assert_not_awaited()


def test_a_shared_session_that_stays_stale_refuses_the_run(agents_dir, hooks_page):
    _write_spec(agents_dir, "a1", allowedTools=["web_fetch"])
    _add_hooks_page_hook(hooks_page)
    stale = _live(ACP_BACKEND_KAS, frozenset({"web_fetch"}))
    stale.shutdown = AsyncMock()
    with pytest.raises(spec_hooks.StaleProjectionError):
        asyncio.run(
            spec_hooks.replace_stale_shared_session(stale, "a1", AsyncMock(return_value=stale))
        )
    assert stale.shutdown.await_count == spec_hooks._MAX_REPROJECTIONS + 1


@pytest.mark.usefixtures("healthy_host_memory")
def test_a_shared_subagent_session_is_checked_after_it_is_created(monkeypatch):
    """The shared arm hands its new session to replace_stale_shared_session before
    the run streams on it, so a stale one is swapped for a fresh one."""
    from kiro_crew import subagent as subagent_mod
    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    streamed: list = []

    def provider(name):
        p = AsyncMock()
        p.capabilities = capabilities_for(ACP_BACKEND_KAS)
        p.cwd = "/w"
        p.kas_auto_approved_capabilities = None
        p.kas_projected_agent = ""
        p.context_usage_pct = lambda: 0.0

        async def _stream(*_a, **_kw):
            streamed.append(name)
            yield LLMEvent(kind=EVENT_COMPLETE)

        p.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        return p

    first, second = provider("stale"), provider("fresh")
    checked: list = []

    async def replace(client, agent_id, recreate):
        checked.append((client, agent_id))
        return await recreate()

    monkeypatch.setattr(subagent_mod, "replace_stale_shared_session", replace)
    monkeypatch.setattr(spec_hooks, "crew_fired_spec_hooks", lambda agent: ([], [], 0))
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.reset = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.auto_approve_subagent_spawn = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    manager.hook_store = MagicMock()
    manager.hook_store.fire = AsyncMock(return_value=[])
    created = iter([first, second])
    monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
    monkeypatch.setattr(
        manager, "_create_shared_session", AsyncMock(side_effect=lambda *a: next(created))
    )
    info = SubagentInfo(
        execution_context=execution_for_store("", template_id="worker"),
        id="t02",
        task="test",
        parent_session_key="dashboard:parent",
    )
    manager._log_spawned(info)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        asyncio.run(manager._run_inner(info, "subagent:t02"))
    assert checked and checked[0] == (first, "worker")
    assert streamed and set(streamed) == {"fresh"}

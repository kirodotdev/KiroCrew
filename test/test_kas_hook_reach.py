"""A spec's PreToolUse hooks reach every KAS call they cover.

Rows P0-8, P0-10, P0-12, P1-5 and P1-33 of the kiro-cli -> KAS migration
plan: the matcher kinds kiro-cli reads, the shell and
filesystem asks, a session with no ``permissions``, a refused hook, a server-wide
MCP ask, the spec-hook tags and the hook cap. Also an MCP grant a hook's ``ask``
holds: on a surface that fires the hook Crew answers it at the gate, so the granted
tool still runs unprompted, and every other surface keeps asking.
"""

from __future__ import annotations

import ast
import logging
from types import SimpleNamespace

import pytest

from kiro_crew.acp import kas_agents, kas_hook_coverage, kas_permissions
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_BUILTINS_KEY,
    HOOK_GATEABLE_CAPABILITIES,
    UNPARSEABLE_SHELL_GUARD,
    hook_gated_auto_approved,
    hook_gated_capabilities,
    unprompted_capabilities,
    withhold_hook_gated_auto_approval,
)
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.agent_materialization.kiro_hooks import _hook_matcher_ok
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.hook_runtime.hook_dispatch import pre_tool_match_names
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    HookManager,
    HooksConfig,
    ScriptHookResult,
    _tool_matches,
    hook_gate_kwargs,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


def _spec_meets(matcher: str, **call) -> bool:
    _every, spec = pre_tool_match_names("title", **call)
    return any(_tool_matches(matcher, name) for name in spec or ())


# ── P0-8: the matcher kinds kiro-cli reads ──────────────────────────────────


@pytest.mark.parametrize("matcher", ["@builtin", "@github", "fs_rea?"])
def test_the_materialized_kiro_cli_spec_keeps_its_own_rule(matcher):
    # kiro_hooks.py:48 also writes kiro-cli's materialized spec; it is not widened.
    assert _hook_matcher_ok(matcher) is False


@pytest.mark.parametrize(
    ("matcher", "call", "meets"),
    [
        ("@builtin", {"harness_tool_id": "run_command"}, True),
        ("@builtin", {"harness_tool_id": "x", "tool_identity": "t", "mcp_server": "gh"}, False),
        ("@builtin/execute_*", {"harness_tool_id": "run_command"}, True),
        ("@builtin/fs_*", {"harness_tool_id": "run_command"}, False),
        ("@gh", {"harness_tool_id": "x", "tool_identity": "t", "mcp_server": "gh"}, True),
        ("@GH", {"harness_tool_id": "x", "tool_identity": "t", "mcp_server": "gh"}, True),
        ("@gh", {"harness_tool_id": "x", "tool_identity": "t", "mcp_server": "gl"}, False),
        ("@gh", {"harness_tool_id": "run_command"}, False),
        ("@mcp", {"harness_tool_id": "x", "tool_identity": "t", "mcp_server": "gh"}, True),
        ("@mcp", {"harness_tool_id": "run_command"}, False),
        ("fs_rea?", {"harness_tool_id": "read_file"}, True),
        # P1-33: kiro-cli's tags, for spec hooks only.
        ("read", {"harness_tool_id": "grep_search"}, True),
        ("read", {"harness_tool_id": "run_command"}, False),
        ("web", {"harness_tool_id": "remote_web_search"}, True),
        ("subagent", {"harness_tool_id": "invoke_sub_agent"}, True),
    ],
)
def test_a_spec_hook_meets_each_kind_on_the_call_it_names(matcher, call, meets):
    assert _spec_meets(matcher, **call) is meets


def test_the_kind_names_never_reach_a_hooks_page_hook_or_the_reported_tool_name():
    every, spec = pre_tool_match_names("title", harness_tool_id="grep_search")
    assert "@builtin" not in every and "read" not in every
    # The first name is the one a spec hook's stdin reports.
    assert spec is not None and spec[0] == "grep"


@pytest.mark.parametrize("matcher", ["@builtin", "@builtin/fs_*", "@github", "fs_rea?"])
def test_an_array_form_document_keeps_the_wider_matcher(matcher):
    # The document normalizer reads Crew's spec-hook rule, not kiro-cli's.
    docs = [
        {
            "name": "g",
            "trigger": "PreToolUse",
            "matcher": matcher,
            "action": {"type": "command", "command": "guard.sh"},
        }
    ]
    assert [h.matcher for h in spec_hooks.spec_script_hooks("a1", {"hooks": docs})] == [matcher]


def _opencode(title: str, servers: tuple[str, ...]) -> str:
    from kiro_crew.acp.harness_tool_names import qualified_harness_tool_id

    return qualified_harness_tool_id("opencode", title, frozenset(servers))


@pytest.mark.parametrize(
    ("title", "servers", "matcher", "meets"),
    [
        # A fused title that is also opencode's own tool keeps both readings.
        ("apply_patch", ("apply",), "@builtin", True),
        ("apply_patch", ("apply",), "@builtin/fs_write", True),
        ("apply_patch", ("apply",), "@apply", True),
        ("apply_patch", (), "@builtin", True),
        # An MCP split names its server, and is not a built-in.
        ("docs_lookup", ("docs",), "@docs", True),
        ("docs_lookup", ("docs",), "@mcp", True),
        ("docs_lookup", ("docs",), "@builtin", False),
    ],
)
def test_an_opencode_fused_title_meets_each_reading(title, servers, matcher, meets):
    assert _spec_meets(matcher, harness_tool_id=_opencode(title, servers)) is meets


def test_a_trusted_mcp_server_gives_no_builtin_reading():
    call = {"harness_tool_id": "web_fetch", "tool_identity": "web_fetch", "mcp_server": "docs"}
    assert _spec_meets("@builtin", **call) is False
    assert _spec_meets("@docs", **call) is True


@pytest.mark.parametrize(
    ("matcher", "covered"),
    [
        ("@builtin", set(HOOK_GATEABLE_CAPABILITIES) - {"mcp"}),
        ("@builtin/execute_bash", {"shell"}),
        ("@mcp", {"mcp"}),
    ],
)
def test_the_projection_covers_what_each_kind_meets(matcher, covered):
    assert hook_gated_capabilities((matcher,)) == covered


# ── P1-5: an @server or wildcard MCP hook asks on that server only ──────────


@pytest.mark.parametrize("matcher", ["@gh", "@gh/get_*", "@GH/x?"])
def test_a_server_wide_mcp_hook_asks_on_that_server(matcher):
    policy = {"rules": [{"capability": "mcp", "match": ["gh/*", "docs/*"], "effect": "allow"}]}
    out = withhold_hook_gated_auto_approval(policy, (matcher,), mcp_tool_names=lambda s: None)
    assert out["rules"][1:] == [{"capability": "mcp", "match": ["gh/*"], "effect": "ask"}]


def test_an_mcp_hook_on_a_server_the_policy_does_not_name_asks_on_all_of_mcp():
    policy = {"rules": [{"capability": "mcp", "match": ["docs/*"], "effect": "allow"}]}
    out = withhold_hook_gated_auto_approval(policy, ("@gh",), mcp_tool_names=lambda s: None)
    assert {"capability": "mcp", "effect": "ask"} in out["rules"]


# ── P0-10: shell, filesystem and a session with no policy ───────────────────


def test_a_spec_with_no_permissions_still_gets_the_hooks_asks():
    out = kas_agents.to_client_custom_agent("a1", {}, "p", pre_tool_hook_matchers=("execute_bash",))
    assert out["permissions"]["rules"] == [
        {"capability": "shell", "effect": "ask"},
        {"capability": "shell", "match": [UNPARSEABLE_SHELL_GUARD], "effect": "deny"},
    ]


def test_a_hook_ask_is_not_read_as_the_author_restricting_the_shell():
    """Crew still answers the shell the spec granted, after the hook ran."""
    spec = {"allowedTools": ["execute_bash"]}
    plain = kas_agents.to_client_custom_agent("a1", spec, "p")
    hooked = kas_agents.to_client_custom_agent(
        "a1", spec, "p", pre_tool_hook_matchers=("execute_bash",)
    )
    assert plain.get(CREW_ANSWERED_BUILTINS_KEY) == hooked.get(CREW_ANSWERED_BUILTINS_KEY)
    assert ("shell", "ask") in [
        (r["capability"], r["effect"]) for r in hooked["permissions"]["rules"]
    ]


def test_a_live_session_with_no_shell_ask_is_stale_once_a_shell_hook_exists():
    # A batch projected before the hook: no rule, so a saved answer can decide it.
    covered = hook_gated_capabilities(("execute_bash",))
    assert hook_gated_auto_approved(covered, unprompted_capabilities(None)) == {"shell"}
    # The batch projected with the hook holds it.
    fresh = withhold_hook_gated_auto_approval(None, ("execute_bash",))
    assert hook_gated_auto_approved(covered, unprompted_capabilities(fresh)) == set()


def test_a_shell_hook_does_not_make_a_remote_workspace_refuse_the_agent():
    """The guard deny is Crew's own rule, not the author restricting the shell."""
    from kiro_crew.acp.runtime_remote_workspace import _restricts_replaced_builtins

    hooked = kas_agents.to_client_custom_agent("a1", {}, "p", pre_tool_hook_matchers=("*",))
    assert _restricts_replaced_builtins(hooked["permissions"]) is False
    authored = {"rules": [{"capability": "shell", "match": ["rm *"], "effect": "deny"}]}
    assert _restricts_replaced_builtins(authored) is True


def test_unprompted_keeps_auto_approved_for_every_other_reader():
    policy = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
    assert kas_permissions.auto_approved_capabilities(policy) == {"web_fetch"}
    assert unprompted_capabilities(policy) == set(HOOK_GATEABLE_CAPABILITIES)


# ── P1-33: the cap is the cap, and what it drops is named ───────────────────


@pytest.mark.parametrize("form", ["object", "array"])
def test_hooks_past_the_cap_are_not_read_and_are_named(form, caplog):
    cap = spec_hooks._MAX_SPEC_HOOKS
    if form == "object":
        value: object = {
            "preToolUse": [{"command": f"c{i}"} for i in range(cap)],
            "stop": [{"command": "late"}, {"command": "later"}],
        }
    else:
        value = [
            {
                "name": f"n{i}",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": f"c{i}"},
            }
            for i in range(cap + 2)
        ]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_sdk.spec_hooks"):
        hooks = spec_hooks.spec_script_hooks("a1", {"hooks": value})
    assert len(hooks) == cap
    assert any("2 more are not read" in r.getMessage() for r in caplog.records)


# ── an MCP grant a hook holds is Crew's to answer ───────────────────────────


@pytest.fixture
def mcp_tools(monkeypatch):
    """The MCP probe cache, as a dict the test fills: no real cache is read."""
    tools: dict[str, list[str]] = {}
    monkeypatch.setattr(kas_permissions, "cached_mcp_tool_names", tools.get)
    return tools


def _hooked(matcher: str = "*", **spec) -> dict:
    spec = {"mcpServers": {"docs": {"command": "x"}}, "tools": ["*"], **spec}
    return kas_agents.to_client_custom_agent("a1", spec, "p", pre_tool_hook_matchers=(matcher,))


def _answered(entry: dict) -> frozenset[str]:
    return kas_hook_coverage.projected_hook_held([entry], "a1")


def _mcp_request(held: frozenset[str], tool: str = "lookup") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="req-1",
        tool_call_id="tc-1",
        title=f"Running: @docs/{tool}",
        mcp_server_name="docs",
        tool_name=tool,
        mcp_identity_trusted=True,
        kas_builtin_ids=True,
        spec_hook_held=held,
    )


def _judge(event: AcpEvent, *, fires: bool = True):
    return HookManager(HooksConfig()).on_tool_call(
        event.title, pre_tool_hooks_fire=fires, **hook_gate_kwargs(event)
    )


def test_a_hook_held_mcp_grant_is_recorded_for_crew_to_answer(mcp_tools):
    entry = _hooked(allowedTools=["@docs/lookup"])
    # The hook's ask still takes the call off KAS's own approval ...
    assert {"capability": "mcp", "effect": "ask"} in entry["permissions"]["rules"]
    # ... and the grant it held is on its own record, never on the wire, and
    # never among the grants every surface answers.
    assert "@docs/lookup" in _answered(entry)
    assert "@docs/lookup" not in kas_agents.projected_crew_answered([entry], "a1")
    assert {k for k in entry if k.startswith("_")} <= kas_permissions.CREW_ONLY_ENTRY_KEYS


def test_the_gate_answers_a_hook_held_mcp_grant_without_a_prompt(mcp_tools):
    verdict = _judge(_mcp_request(_answered(_hooked(allowedTools=["@docs/lookup"]))))
    assert verdict.action == TOOL_AUTO_APPROVE
    assert verdict.tier == "batch-guard-grant"


def test_a_surface_that_fires_no_hook_keeps_asking(mcp_tools):
    # Answering the grant on a surface that does not say it runs the PreToolUse
    # hooks on the request (a cron or webhook turn runs none) could run the call
    # with neither the hook nor a person.
    event = _mcp_request(_answered(_hooked(allowedTools=["@docs/lookup"])))
    assert _judge(event, fires=False).action == TOOL_ALLOW


def test_only_the_turn_loops_that_fire_the_hooks_say_so():
    # Saying so lets the gate answer a grant a hook held, so a surface that fires
    # no PreToolUse hook on the request (a cron or webhook turn) must not.
    import source_corpus

    root = source_corpus.src_root()
    sites = {
        path.relative_to(root).as_posix()
        for path, _text, tree in source_corpus.parsed_candidates(
            require_all=("pre_tool_hooks_fire",)
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(k.arg == "pre_tool_hooks_fire" and _is_true(k.value) for k in node.keywords)
    }
    assert sites == {
        "dashboard/chat_turn/tool_approval.py",
        "subagent_manager/run_turn.py",
        "task_executor.py",
    }


def _is_true(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is True


def test_the_task_runner_gate_answers_a_held_grant(mcp_tools):
    # The task runner's ladder runs its SpecHooks floor before the gate, so its
    # consult says the hooks fire, and the held grant runs unprompted there.
    from kiro_crew import task_executor, tool_permission
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks

    ctx = SimpleNamespace(hooks=HookManager(HooksConfig()))
    policy = task_executor._step_permission_policy(
        client=SimpleNamespace(context_usage_pct=lambda: 0.0),
        ctx=ctx,
        spec=TurnSpecHooks([], None, False, False),
        run=SimpleNamespace(),
        task=SimpleNamespace(),
        agent="",
        session_key="taskrunner-1",
        on_tool_approval=None,
    )
    event = _mcp_request(_answered(_hooked(allowedTools=["@docs/lookup"])))
    hit = policy.gate.judge(tool_permission.Ask(event, None, "taskrunner-1"))
    assert isinstance(hit, tool_permission.Hit)


def test_the_session_stamps_the_held_grants_on_its_own_request(mcp_tools):
    from test_kas_spec_builtin_grant import _handle, _permission

    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    entry = _hooked(allowedTools=["@docs/lookup"])
    handle = _handle(ACP_BACKEND_KAS)
    KasHarness.record_session_projection(object.__new__(KasHarness), handle, [entry], "a1")
    event = handle._build_permission_event(_permission("own-sid"))
    assert event is not None
    assert event.spec_hook_held == frozenset({"@docs/lookup"})
    assert "@docs/lookup" not in event.spec_granted_builtins
    # A mode switch to an entry the batch does not carry holds nothing.
    assert kas_hook_coverage.switched_hook_held([entry], "kas-builtin") == frozenset()


@pytest.mark.parametrize(
    "spec",
    [
        {
            "allowedTools": ["@docs/lookup"],
            "permissions": {
                "rules": [{"capability": "mcp", "match": ["docs/lookup"], "effect": "ask"}]
            },
        },
        {"allowedTools": ["@docs/search"]},
    ],
    ids=["authored-ask", "never-granted"],
)
def test_a_tool_the_policy_did_not_auto_approve_is_not_answered(mcp_tools, spec):
    mcp_tools["docs"] = ["lookup", "search"]
    granted = _answered(_hooked(**spec))
    assert "@docs/lookup" not in granted
    assert _judge(_mcp_request(granted)).action == TOOL_ALLOW


def test_with_no_hook_nothing_is_recorded(mcp_tools):
    # KAS approves the grant itself; no request reaches Crew to answer.
    entry = kas_agents.to_client_custom_agent(
        "a1", {"mcpServers": {"docs": {"command": "x"}}, "allowedTools": ["@docs/lookup"]}, "p"
    )
    assert "@docs/lookup" not in _answered(entry)


def test_a_server_grant_records_each_cached_tool_the_hook_holds(mcp_tools):
    mcp_tools["docs"] = ["lookup", "search"]
    assert {"@docs/lookup", "@docs/search"} <= _answered(_hooked(allowedTools=["@docs"]))
    # A hook on one tool holds that tool alone; the other keeps KAS's own allow.
    one = _answered(_hooked("@docs/lookup", allowedTools=["@docs"]))
    assert "@docs/lookup" in one and "@docs/search" not in one


def test_a_server_grant_with_no_cached_tool_list_still_prompts(mcp_tools):
    granted = _answered(_hooked(allowedTools=["@docs"]))
    assert not {g for g in granted if g.startswith("@docs")}
    assert _judge(_mcp_request(granted)).action == TOOL_ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 2])
async def test_a_dashboard_turn_runs_the_hook_on_the_answered_grant(tmp_path, mcp_tools, exit_code):
    from dashboard_approval_helpers import (
        _complete_event,
        _context_builder,
        _make_hook_store,
        _make_slot,
        _make_state,
        _patch_stats,
        _set_stream,
    )

    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.testing.wait import finished

    builder = _context_builder()
    builder.hooks = HookManager(HooksConfig())
    store = _make_hook_store()
    store.fire.return_value = [
        ScriptHookResult(
            hook_id="h1", hook_name="guard", event=HOOK_EVENT_PRE_TOOL_USE, exit_code=exit_code
        )
    ]
    state, client = _make_state(tmp_path, context_builder=builder, hook_store=store)
    slot = _make_slot()
    granted = _answered(_hooked(allowedTools=["@docs/lookup"]))
    _set_stream(client, [_mcp_request(granted), _complete_event()])

    with _patch_stats():
        # A card would wait for an answer nobody gives: bounded, it fails by name.
        await finished(_run_chat(state, slot, "hello"), "the turn ending with no card")

    # Answered by the gate, so no card; the hook still decides.
    assert not any(m.get("role") == "permission" for m in slot.messages)
    if exit_code == 2:
        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()
    else:
        client.approve_tool.assert_called_once()
        client.reject_tool.assert_not_called()

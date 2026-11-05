"""A spec's PreToolUse hooks reach every KAS call they cover.

Rows P0-8, P0-10, P0-12, P1-5 and P1-33 of the kiro-cli -> KAS migration
plan: the matcher kinds kiro-cli reads, the shell and
filesystem asks, a session with no ``permissions``, a refused hook, a server-wide
MCP ask, the spec-hook tags and the hook cap.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.acp import kas_agents, kas_permissions
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_BUILTINS_KEY,
    HOOK_GATEABLE_CAPABILITIES,
    UNPARSEABLE_SHELL_GUARD,
    hook_gated_auto_approved,
    hook_gated_capabilities,
    unprompted_capabilities,
    withhold_hook_gated_auto_approval,
)
from kiro_crew.agent_materialization.kiro_hooks import _hook_matcher_ok
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.hook_runtime.hook_dispatch import pre_tool_match_names
from kiro_crew.hooks import _tool_matches


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


def _pre_tool_hooks(matcher: str) -> list:
    return spec_hooks.spec_script_hooks(
        "a1", {"hooks": {"preToolUse": [{"matcher": matcher, "command": "guard.sh"}]}}
    )


def _spec_meets(matcher: str, **call) -> bool:
    _every, spec = pre_tool_match_names("title", **call)
    return any(_tool_matches(matcher, name) for name in spec or ())


# ── P0-8: the matcher kinds kiro-cli reads ──────────────────────────────────


@pytest.mark.parametrize(
    "matcher", ["@builtin", "@builtin/fs_*", "@github", "@github/get_*", "@mcp", "fs_rea?"]
)
def test_kiro_cli_matcher_kinds_are_kept_for_spec_hooks(matcher):
    assert [h.matcher for h in _pre_tool_hooks(matcher)] == [matcher]


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

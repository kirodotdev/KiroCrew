"""KAS projection: a PreToolUse hook on MCP tools withholds auto-approve for the
exact ``<server>/<tool>`` resources it meets, resolved against the servers' real
tool names, and for all of MCP when those cannot be resolved."""

from __future__ import annotations

import pytest

from kiro_crew.acp import kas_permissions

#: Real tool names per server, as the MCP probe cache would hold them.
_TOOLS = {"srv": ["get_creds", "search"], "srv_": ["x", "search"], "Docs": ["Get_Creds", "find"]}


def _tool_names(server):
    return _TOOLS.get(server)


def _asks(policy) -> list[str]:
    return [
        p
        for r in policy["rules"]
        if r["effect"] == "ask" and r["capability"] == "mcp"
        for p in r.get("match") or ["**"]
    ]


def test_a_hook_on_one_mcp_tool_withholds_only_that_tool():
    # A hook guarding one MCP tool must not make every other MCP tool prompt.
    policy = {"rules": [{"capability": "mcp", "match": ["srv/*", "Docs/*"], "effect": "allow"}]}
    audited: list = []
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy,
        ("@srv/get_creds",),
        audit_decision=lambda refs, outcome, reason: audited.append((refs, outcome)),
        mcp_tool_names=_tool_names,
    )
    assert out["rules"][1:] == [{"capability": "mcp", "match": ["srv/get_creds"], "effect": "ask"}]
    assert audited == [("@srv/get_creds", "withheld")]
    # The rest of MCP stays auto-approved, and the hooked tool is held back.
    approved = kas_permissions.auto_approved_capabilities(out)
    assert approved == {"mcp", "mcp-held:srv/get_creds", "mcp-server:srv", "mcp-server:Docs"}


@pytest.mark.parametrize(
    ("matcher", "resources"),
    [
        # Written in another case than the server's real names.
        ("@docs/get_creds", ["Docs/Get_Creds"]),
        ("mcp__DOCS__GET_CREDS", ["Docs/Get_Creds"]),
        # A server whose name ends in `_`: no split can put the ask on `srv/_x`.
        ("mcp__srv___x", ["srv_/x"]),
        ("@SRV_/X", ["srv_/x"]),
        # A wildcard can meet a tool the cache does not list yet: that whole
        # server, even when it meets exactly one cached tool today (P1-5).
        ("@srv_/x*", ["srv_/*"]),
        ("@srv_/*", ["srv_/*"]),
        ("@SRV_/x*", ["srv_/*"]),
        # A server the policy does not allow is not known: all of MCP.
        ("@srv/get_*", ["**"]),
        # A title can be split at more than one `__`: all of MCP.
        ("mcp__srv___*", ["**"]),
        # kiro-cli's `@server`: that server's tools.
        ("@srv_", ["srv_/*"]),
        # A glob in the server part names no one server: all of MCP.
        ("@srv*/x", ["**"]),
        # Meeting no real tool is not one exact resource: all of MCP.
        ("@nope/x", ["**"]),
        ("mcp__a__b__c", ["**"]),
    ],
)
def test_an_mcp_hook_asks_for_the_exact_real_resources_it_meets(matcher, resources):
    policy = {"rules": [{"capability": "mcp", "match": ["srv_/*", "Docs/*"], "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, (matcher,), mcp_tool_names=_tool_names
    )
    # Exact names, no glob: the real call is held whether or not KAS compares case.
    assert _asks(out) == resources


def test_an_mcp_hook_still_fires_on_a_server_written_differently_in_config():
    # The hook is written `mcp__srv___x` / `@SRV_/X`; the server is `srv_` and its
    # tool `x`. The real call must stay a permission request so the gate, which
    # names it from the trusted server identity, runs the hook on it.
    from kiro_crew.hook_runtime.hook_dispatch import pre_tool_match_names
    from kiro_crew.hooks import _tool_matches

    policy = {"rules": [{"capability": "mcp", "match": ["srv_/*"], "effect": "allow"}]}
    for matcher in ("mcp__srv___x", "@SRV_/X"):
        out = kas_permissions.withhold_hook_gated_auto_approval(
            policy, (matcher,), mcp_tool_names=_tool_names
        )
        assert "srv_/x" in _asks(out)
        names, _ = pre_tool_match_names("call", tool_identity="x", mcp_server="srv_")
        assert any(_tool_matches(matcher, n) for n in names)
        assert "srv_/search" not in _asks(out)


@pytest.mark.parametrize(
    ("allows", "tool_names"),
    [
        # An allow that names no server: the servers it grants cannot be listed.
        ([{"capability": "mcp", "effect": "allow"}], _tool_names),
        ([{"capability": "mcp", "match": ["s*/x"], "effect": "allow"}], _tool_names),
    ],
)
def test_an_mcp_hook_that_cannot_be_resolved_withholds_all_of_mcp(allows, tool_names):
    out = kas_permissions.withhold_hook_gated_auto_approval(
        {"rules": allows}, ("@srv/get_creds",), mcp_tool_names=tool_names
    )
    assert {"capability": "mcp", "effect": "ask"} in out["rules"]
    assert "mcp" not in kas_permissions.auto_approved_capabilities(out)


@pytest.mark.parametrize(
    "tool_names",
    [
        # The matcher's server is allowed but its tools are not cached.
        {"Docs": ["x"]}.get,
        # A reader that knows no tools.
        lambda server: None,
    ],
)
def test_an_uncached_tool_on_an_allowed_server_withholds_that_server(tool_names):
    """P1-5: the server is known, so only its tools ask; the rest of MCP keeps its allow."""
    allows = [{"capability": "mcp", "match": ["Docs/*", "srv/*"], "effect": "allow"}]
    out = kas_permissions.withhold_hook_gated_auto_approval(
        {"rules": allows}, ("@srv/get_creds",), mcp_tool_names=tool_names
    )
    assert _asks(out) == ["srv/*"]
    approved = kas_permissions.auto_approved_capabilities(out)
    assert {"mcp", "mcp-held:srv/*"} <= approved
    # The staleness check reads the same answer: nothing is left to withhold.
    covered = kas_permissions.hook_gated_capabilities(
        ("@srv/get_creds",), mcp_servers={"Docs", "srv"}, mcp_tool_names=tool_names
    )
    assert kas_permissions.hook_gated_auto_approved(covered, approved) == set()


def test_a_hook_that_covers_all_of_mcp_still_asks_for_all_of_it():
    policy = {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, ("@srv/get_creds", "*_x"), mcp_tool_names=_tool_names
    )
    assert {"capability": "mcp", "effect": "ask"} in out["rules"]
    assert not any(r.get("match") == ["srv/get_creds"] for r in out["rules"])
    assert "mcp" not in kas_permissions.auto_approved_capabilities(out)


@pytest.mark.parametrize(
    ("covered", "approved", "still"),
    [
        ({"mcp:srv/t"}, {"mcp"}, {"mcp:srv/t"}),
        ({"mcp:srv/t"}, {"mcp", "mcp-held:srv/t"}, set()),
        ({"mcp:srv/t"}, {"mcp", "mcp-held:srv/other"}, {"mcp:srv/t"}),
        ({"mcp:srv/t"}, {"mcp-held:srv/t"}, set()),
        ({"mcp:srv/t"}, set(), set()),
        ({"web_fetch", "mcp"}, {"mcp"}, {"mcp"}),
    ],
)
def test_which_hook_coverage_a_live_batch_still_auto_approves(covered, approved, still):
    assert kas_permissions.hook_gated_auto_approved(covered, approved) == still


def test_a_matcher_two_allowed_servers_could_name_withholds_all_of_mcp():
    # `mcp__a__b__c` is tool `b__c` on `a` and tool `c` on `a__b` alike. Either
    # server's cached list may lack the tool the hook means (a server can expose a
    # tool later), so no tool list can make the match unique: all of MCP stays
    # withheld, whichever server holds the tool today.
    tools = {"a": ["b__c"], "a__b": ["c"]}.get
    policy = {"rules": [{"capability": "mcp", "match": ["a/*", "a__b/*"], "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, ("mcp__a__b__c",), mcp_tool_names=tools
    )
    assert _asks(out) == ["**"]
    # `@server/tool` names its server unambiguously, so it still resolves.
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, ("@A__B/c",), mcp_tool_names=tools
    )
    assert _asks(out) == ["a__b/c"]
    # With one allowed server the `mcp__` spelling resolves to its one real tool.
    single = {"rules": [{"capability": "mcp", "match": ["a__b/*"], "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(
        single, ("mcp__a__b__c",), mcp_tool_names=tools
    )
    assert _asks(out) == ["a__b/c"]


# ── a bare tool name (``get_creds``), which the gate also names a KAS MCP call by ──


def test_a_bare_hook_on_one_mcp_tool_withholds_only_that_tool():
    """An unattended agent guards one MCP tool with a hook written as its bare
    name. Every other MCP tool, and every built-in, keeps its auto-approval."""
    policy = {
        "rules": [
            {"capability": "mcp", "match": ["srv/*", "Docs/*"], "effect": "allow"},
            {"capability": "web_fetch", "effect": "allow"},
        ]
    }
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, ("get_creds",), mcp_tool_names=_tool_names
    )
    # Held on every server, listed or not, in each spelling it meets.
    assert sorted(_asks(out)) == ["*/Get_Creds", "*/get_creds"]
    assert [r for r in out["rules"] if r["effect"] == "ask" and r["capability"] != "mcp"] == []
    approved = kas_permissions.auto_approved_capabilities(out)
    assert {"mcp", "web_fetch"} <= approved


def test_a_bare_hook_holds_the_tool_on_a_server_the_policy_does_not_list():
    """The gate runs the hook on the same-named tool of any server, and a saved
    answer can decide an unlisted server's call without a request: only an
    explicit ask on that resource keeps it on Crew's path."""
    policy = {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, ("get_creds",), mcp_tool_names=_tool_names
    )
    assert "*/get_creds" in _asks(out)
    approved = kas_permissions.auto_approved_capabilities(out)
    covered = kas_permissions.hook_gated_capabilities(
        ("get_creds",), mcp_servers={"srv"}, mcp_tool_names=_tool_names
    )
    assert kas_permissions.hook_gated_auto_approved(covered, approved) == set()


def test_the_bare_hook_holds_the_tool_the_gate_runs_it_on():
    from kiro_crew.hook_runtime.hook_dispatch import pre_tool_match_names
    from kiro_crew.hooks import _tool_matches

    names, _ = pre_tool_match_names("call", tool_identity="get_creds", mcp_server="srv")
    assert any(_tool_matches("get_creds", n) for n in names)
    names, _ = pre_tool_match_names("call", tool_identity="search", mcp_server="srv")
    assert not any(_tool_matches("get_creds", n) for n in names)


@pytest.mark.parametrize(
    ("tool_names", "asks"),
    [
        # An allowed server with nothing cached is held whole.
        ({"srv": ["get_creds"]}.get, ["*/get_creds", "Docs/*"]),
        ({"srv": ["get_creds"], "Docs": ["find"]}.get, ["*/get_creds"]),
    ],
)
def test_a_bare_hook_holds_every_allowed_server_that_could_serve_it(tool_names, asks):
    policy = {"rules": [{"capability": "mcp", "match": ["srv/*", "Docs/*"], "effect": "allow"}]}
    out = kas_permissions.withhold_hook_gated_auto_approval(
        policy, ("get_creds",), mcp_tool_names=tool_names
    )
    assert sorted(_asks(out)) == asks


@pytest.mark.parametrize(
    ("matcher", "allows", "tool_names"),
    [
        # Meets no cached tool: it may be a harness's name for a built-in.
        ("nope", [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}], _tool_names),
        # Nothing cached anywhere: nothing proves it names an MCP tool.
        ("get_creds", [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}], None),
        # The allowed servers cannot be listed.
        ("get_creds", [{"capability": "mcp", "effect": "allow"}], _tool_names),
        # A glob is not a bare name.
        ("get_cre?s", [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}], _tool_names),
    ],
    ids=["no-cached-match", "no-cache", "unbounded-allow", "glob"],
)
def test_a_bare_hook_that_proves_nothing_still_covers_everything(matcher, allows, tool_names):
    out = kas_permissions.withhold_hook_gated_auto_approval(
        {"rules": allows},
        (matcher,),
        mcp_tool_names=tool_names if tool_names is not None else (lambda server: None),
    )
    assert {"capability": "mcp", "effect": "ask"} in out["rules"]
    assert {"capability": "fs_write", "effect": "ask"} in out["rules"]


def test_the_projection_holds_only_the_hooked_mcp_tool(monkeypatch):
    """Through the real KAS projection of a spec like the reported one: a hook on
    one MCP tool by its bare name, both servers auto-approved in ``allowedTools``."""
    from kiro_crew.acp import kas_agents

    monkeypatch.setattr(
        kas_permissions,
        "cached_mcp_tool_names",
        {"creds": ["get_creds", "list_roles"], "search": ["InternalSearch"]}.get,
    )
    spec = {
        "name": "a",
        "prompt": "p",
        "tools": ["*"],
        "allowedTools": ["@creds", "@search"],
        "hooks": {"preToolUse": [{"matcher": "get_creds", "command": "guard.sh"}]},
    }
    matchers = kas_agents.pre_tool_hook_matchers("a", spec)
    assert matchers == ("get_creds",)
    out = kas_agents.to_client_custom_agent("a", spec, "p", pre_tool_hook_matchers=matchers)
    rules = out["permissions"]["rules"]
    assert _asks(out["permissions"]) == ["*/get_creds"]
    assert [r for r in rules if r["effect"] == "ask" and r["capability"] != "mcp"] == []
    assert any(r["capability"] == "mcp" and r["effect"] == "allow" for r in rules)

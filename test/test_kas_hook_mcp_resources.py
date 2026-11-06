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

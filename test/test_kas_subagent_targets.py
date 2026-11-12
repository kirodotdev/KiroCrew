"""A KAS agent's sub-agent tool reaches the agents its ``availableAgents`` names.

KAS resolves an ``invoke_sub_agent`` name only in the session's agent registry, and
its on-disk loader drops a spec written for kiro-cli, so Kiro Crew registers each
named agent in the session's ``customAgents`` batch, translated like the session's
own agent, and names every one it does not register in the session-start notice.
A remote workspace leaves out a target whose own rules it cannot bind, names it,
and still starts the session.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import KasHarness

#: Shaped like a kiro-cli or AIM spec: ``allowedTools`` and ``toolsSettings``, no
#: ``permissions`` block, which KAS's own loader skips as a CLI-only profile.
_SLACK = {
    "name": "slack-agent",
    "description": "Reads and posts Slack messages",
    "prompt": "You are the Slack agent.",
    "tools": ["fs_read"],
    "allowedTools": ["fs_read"],
    "toolsSettings": {"fs_read": {"allowedPaths": ["~/slack"]}},
}


def _delegator(*available: str, tools: tuple[str, ...] = ("fs_read", "use_subagent")) -> dict:
    return {
        "name": "delegator",
        "prompt": "Delegate every Slack task to the slack-agent sub-agent.",
        "tools": list(tools),
        "allowedTools": ["fs_read"],
        "toolsSettings": {"subagent": {"availableAgents": list(available)}},
    }


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _extras():
    return await KasHarness().session_extras("delegator", work_dir=None)


def _ids(extras) -> list[str]:
    return [a["id"] for a in extras.custom_agents]


@pytest.mark.asyncio
async def test_a_delegating_agent_registers_the_agent_its_available_agents_names(agents_dir):
    _write(agents_dir, _delegator("slack-agent"))
    denied = {"fs_read": {"allowedPaths": ["~/slack"], "deniedPaths": ["~/slack/private"]}}
    _write(agents_dir, {**_SLACK, "toolsSettings": denied})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "slack-agent"]
    parent, target = extras.custom_agents
    assert "invoke_sub_agent" in parent["tools"]
    assert {
        "capability": "subagent",
        "match": ["*"],
        "exclude": ["slack-agent"],
        "effect": "deny",
    } in parent["permissions"]["rules"]
    # Through Crew's own translation: KAS tool ids, the description the engine's
    # tool lists, and its own toolsSettings as KAS permissions, never relayed raw.
    assert "read_file" in target["tools"]
    assert target["description"] == _SLACK["description"]
    assert target["prompt"] == _SLACK["prompt"]
    assert "allowedTools" not in target and "toolsSettings" not in target
    assert any(r.get("effect") == "deny" for r in target["permissions"]["rules"])
    assert "slack-agent" not in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_name_no_spec_declares_is_named_in_the_notice(agents_dir):
    _write(agents_dir, _delegator("ghost"))

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "ghost (no agent of that name was found)" in extras.withheld_credentials_notice
    assert "agent delegator cannot hand work to" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_pattern_registers_every_agent_it_matches(agents_dir):
    _write(agents_dir, _delegator("slack-*", "mail-*"))
    for name in ("slack-agent", "slack-bot", "other"):
        _write(agents_dir, {**_SLACK, "name": name})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "slack-agent", "slack-bot"]
    assert "mail-* (it matches no agent)" in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [
        pytest.param(
            {
                "tools": ["execute_bash"],
                "toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}},
            },
            id="shell-deny-by-default",
        ),
        pytest.param(
            {
                "tools": ["fs_write"],
                "toolsSettings": {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}},
            },
            id="write-fallback-deny",
        ),
        pytest.param(
            {"hooks": {"preToolUse": [{"matcher": "*", "command": "exit 2"}]}},
            id="pre-tool-use-hook",
        ),
        pytest.param(
            # A regex KAS has no deny rule for: only the gate's deny record holds it.
            {
                "tools": ["execute_bash"],
                "allowedTools": ["execute_bash"],
                "toolsSettings": {"shell": {"deniedCommands": ["rm [a-z]+\\.txt"]}},
            },
            id="shell-denied-commands",
        ),
        pytest.param(
            {
                "tools": ["fs_write"],
                "allowedTools": ["fs_write"],
                "toolsSettings": {"fs_write": {"deniedPaths": ["secret/**"]}},
            },
            id="write-denied-paths",
        ),
    ],
)
async def test_a_target_whose_limits_only_the_gate_applies_is_not_registered(agents_dir, limits):
    # Crew's gate applies these to the agent the session runs, and a sub-agent's
    # calls reach it under that agent, so registered they would not bind.
    _write(agents_dir, _delegator("guarded"))
    _write(agents_dir, {**_SLACK, "name": "guarded", **limits})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "guarded (Kiro Crew applies its shell" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_target_whose_deny_record_only_grants_a_fetch_is_registered(agents_dir):
    # The gate never refuses a fetch by the record's web_fetch lists, it only grants
    # a trusted one, so the sub-agent's fetch without the record asks instead.
    fetch = {
        "web_fetch": {
            "trusted": ["https://example\\.com/docs.*"],
            "blocked": ["https://example\\.com/private.*"],
        }
    }
    _write(agents_dir, _delegator("fetcher"))
    _write(
        agents_dir, {**_SLACK, "name": "fetcher", "tools": ["web_fetch"], "toolsSettings": fetch}
    )

    extras = await _extras()

    assert _ids(extras) == ["delegator", "fetcher"]
    record = extras.custom_agents[1]["_crewSpecDenies"]
    assert record["webFetchBlocked"] == fetch["web_fetch"]["blocked"]


@pytest.mark.asyncio
@pytest.mark.parametrize("derived", ["kirocrew-worker", "kirocrew-heartbeat"])
async def test_a_derived_agent_is_not_registered_as_a_target(agents_dir, monkeypatch, derived):
    # Its verified snapshot would be re-checked after the host loads the payload
    # only for the agent the session runs, so a change to the default spec while
    # the session starts would leave the target's stale copy registered.
    from kiro_crew.agent import DerivedSpecSnapshot

    spec = {**_SLACK, "name": derived}
    real = agent_mod.require_fresh_derived_spec

    def _gate(name, work_dir):
        if name == derived:
            return DerivedSpecSnapshot("identity", "fingerprint", spec, name)
        return real(name, work_dir)

    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", _gate)
    _write(agents_dir, _delegator(derived))
    _write(agents_dir, spec)

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert extras.derived_spec_snapshot is None
    assert f"{derived} (it is one of Kiro Crew's own agents)" in extras.withheld_credentials_notice


_CORE = {"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}}


@pytest.mark.asyncio
async def test_a_target_using_a_crew_server_the_session_agent_lacks_is_not_registered(
    agents_dir,
):
    # Only the session's own agent's Crew servers are launched with this session's
    # identity, so the target's own copy would refuse every call unattested.
    switched_off = {"kirocrew-core": {**_CORE["kirocrew-core"], "disabled": True}}
    _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": switched_off})
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": _CORE})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    notice = extras.withheld_credentials_notice
    assert "helper (it uses kirocrew-core, which this chat's own agent does not load)" in notice


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [_CORE, None], ids=["declared", "crew-defaults"])
async def test_a_crew_server_both_agents_load_is_only_the_sessions_attested_copy(
    agents_dir, declared
):
    # The session-level array carries the session's own copy, with its identity,
    # to every agent of the batch; a copy left in the target's entry would be a
    # second launch of the name without it.
    from kiro_crew.acp.kas_agents import MANAGED_MCP_SERVER_NAMES, hoist_servers_for_session
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    servers = {} if declared is None else {"mcpServers": declared}
    _write(agents_dir, {**_delegator("helper", tools=("*",)), **servers})
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], **servers})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    parent, target = extras.custom_agents
    assert "kirocrew-core" in parent["mcpServers"]
    assert not MANAGED_MCP_SERVER_NAMES.intersection(target.get("mcpServers", {}))
    agents, array = hoist_servers_for_session(
        extras.custom_agents,
        "delegator",
        [],
        session_token="session-token",
        managed_entries=extras.managed_entries,
        app_launches=extras.app_launches,
    )
    assert not any(MANAGED_MCP_SERVER_NAMES.intersection(a.get("mcpServers", {})) for a in agents)
    core = next(e for e in array if e.get("name") == "kirocrew-core")
    assert STUB_SESSION_TOKEN_ENV in [v.get("name") for v in core["env"]]


@pytest.mark.asyncio
async def test_an_agent_without_the_sub_agent_tool_registers_nothing(agents_dir):
    _write(agents_dir, _delegator("slack-agent", tools=("fs_read",)))
    _write(agents_dir, _SLACK)

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "slack-agent" not in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(("fs_read", "use_subagent"), id="kiro-cli-name"),
        pytest.param(("*",), id="every-tool"),
        pytest.param(("@builtin",), id="builtin-tag"),
        pytest.param(("fs_read", "@subagent"), id="subagent-tag"),
        pytest.param(("fs_read", "@sub*"), id="pattern-over-a-tag"),
    ],
)
async def test_every_spelling_the_engine_mounts_the_tool_for_registers_the_target(
    agents_dir, tools
):
    # KAS declares invoke_sub_agent under the tags @builtin and @subagent, and its
    # tool filter mounts it for the id, either tag or a pattern over one.
    _write(agents_dir, _delegator("slack-agent", tools=tools))
    _write(agents_dir, _SLACK)

    extras = await _extras()

    assert _ids(extras) == ["delegator", "slack-agent"]


@pytest.mark.asyncio
async def test_an_exclusion_by_tag_takes_the_tool_and_its_targets_away(agents_dir):
    parent = {**_delegator("slack-agent", tools=("@builtin",)), "excludedTools": ["@subagent"]}
    _write(agents_dir, parent)
    _write(agents_dir, _SLACK)

    extras = await _extras()

    assert _ids(extras) == ["delegator"]


@pytest.mark.asyncio
async def test_a_name_the_engine_keeps_for_its_own_mode_is_named(agents_dir):
    # Sent under another wire id, which the parent's subagent rule denies.
    _write(agents_dir, _delegator("plan"))
    _write(agents_dir, {**_SLACK, "name": "plan"})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "plan (the backend keeps that name" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_registration_stops_at_the_cap_and_counts_the_rest(agents_dir):
    from kiro_crew.acp.kas_subagent_targets import MAX_SUBAGENT_TARGETS

    _write(agents_dir, _delegator("helper-*"))
    names = [f"helper-{i:02d}" for i in range(MAX_SUBAGENT_TARGETS + 2)]
    for name in names:
        _write(agents_dir, {**_SLACK, "name": name})

    extras = await _extras()

    assert _ids(extras) == ["delegator", *names[:MAX_SUBAGENT_TARGETS]]
    assert "2 more agents" in extras.withheld_credentials_notice


@pytest.fixture
def mcp_json(monkeypatch, tmp_path) -> Path:
    """The global ``mcp.json``, and a legacy list recorded before any agent file.

    An agent file written after the first KAS session follows kiro-cli's rule: it
    loads the file's servers only when it opts in.
    """
    from kiro_crew.acp import kas_mcp_json_legacy

    settings = tmp_path / "home" / ".kiro" / "settings"
    settings.mkdir(parents=True)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings / "mcp.json")
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    listed = kas_mcp_json_legacy.list_path()
    listed.parent.mkdir(parents=True, exist_ok=True)
    listed.write_text(json.dumps({"agents": []}), encoding="utf-8")
    return settings / "mcp.json"


def _wire(extras) -> dict[str, dict]:
    from kiro_crew.acp._dispatch import attach_kas_custom_agents

    params: dict = {"mcpServers": []}
    attach_kas_custom_agents(params, extras.custom_agents)
    return {a["id"]: a for a in params["_meta"]["kiro"]["customAgents"]}


@pytest.mark.asyncio
async def test_a_target_gets_the_mcp_json_settings_the_session_agent_gets(agents_dir, mcp_json):
    mcp_json.write_text(
        json.dumps({"mcpServers": {"extra": {"command": "e", "disabledTools": ["write_*"]}}}),
        encoding="utf-8",
    )
    _write(agents_dir, _delegator("helper", "reader"))
    own = {"own": {"command": "own-server"}}
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": own})
    _write(agents_dir, {**_SLACK, "name": "reader", "tools": ["*"], "includeMcpJson": True})

    wire = _wire(await _extras())

    # Not opted in: the file's server it does not declare is not started for it.
    assert "@extra" in wire["helper"]["excludedTools"]
    # Its own server gets kiro-cli's start budget, not KAS's 60 s.
    assert wire["helper"]["mcpServers"]["own"]["timeout"] == 120_000
    # Opted in: it reaches the server without the tools the file switches off.
    assert "@extra/write_*" in wire["reader"]["excludedTools"]
    assert "@extra" not in wire["reader"]["excludedTools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_opts_in", [True, False])
async def test_a_pooled_stub_stays_while_an_agent_in_the_batch_loads_its_server(
    agents_dir, mcp_json, parent_opts_in
):
    from kiro_crew.acp.kas_settings_mcp import without_excluded_stubs

    mcp_json.write_text(json.dumps({"mcpServers": {"pooled": {"command": "p"}}}), encoding="utf-8")
    parent = _delegator("helper", tools=("*",))
    _write(agents_dir, {**parent, "includeMcpJson": parent_opts_in})
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"]})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    kept = without_excluded_stubs([{"name": "pooled"}], extras.custom_agents)
    # The session-level array is the whole batch's: a sub-agent that does not load
    # the server never takes it from the session's own agent.
    assert kept == ([{"name": "pooled"}] if parent_opts_in else [])
    assert "@pooled" in _wire(extras)["helper"]["excludedTools"]


def _files(root: str) -> dict:
    return {"files": {"command": "files-server", "args": ["--root", root]}}


#: A Crew server a member's session injects at session level (``member_dispatch``).
_INJECTED_CREW = "kirocrew-dashboard"


@pytest.fixture
def files_pooled(monkeypatch) -> None:
    """The delegator's session-level array holds a stub for ``files``, as the gateway
    writes it, and a Crew server the session injects."""
    from kiro_crew.acp.harness import kas as kas_harness

    def _names(_overlay, agent, **_kwargs):
        return frozenset({"files", _INJECTED_CREW}) if agent == "delegator" else frozenset()

    monkeypatch.setattr(kas_harness.session_servers_mod, "injection_server_names", _names)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent, target",
    [
        pytest.param(_files("/a"), _files("/b"), id="other-arguments"),
        pytest.param(
            _files("/a"),
            {"files": {**_files("/a")["files"], "env": {"FILES_MODE": "write"}}},
            id="other-environment",
        ),
        # The stub then stands for a declaration the session's spec does not hold.
        pytest.param({}, _files("/a"), id="session-agent-declares-none"),
        pytest.param(
            _files("/a"),
            {"files": {**_files("/b")["files"], "type": "stdio", "poolable": True}},
            id="other-arguments-beside-the-ignored-keys",
        ),
        pytest.param(
            _files("/a"),
            {"files": {**_files("/a")["files"], "type": "http"}},
            id="other-transport",
        ),
    ],
)
async def test_a_target_configuring_a_pooled_server_differently_is_not_registered(
    agents_dir, files_pooled, parent, target
):
    # The session-level stub launches the delegator's own ``files``; the target's
    # differently rooted copy dropped by name would resolve to the delegator's root.
    _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": parent})
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": target})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert (
        "helper (it sets up its own files, and every agent in this chat gets the copy "
        "this chat's own agent loads)"
    ) in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_server_the_session_agent_leaves_out_of_tools_refuses_a_target_declaring_it(
    agents_dir, files_pooled
):
    # The session's stub for a server its ``tools`` leaves out starts nothing
    # (``rewriter_spec._refusing_stub_entry``), so it never launches the target's copy.
    _write(agents_dir, {**_delegator("helper"), "mcpServers": _files("/a")})
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": _files("/a")})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert (
        "helper (it sets up its own files, and every agent in this chat gets the copy "
        "this chat's own agent loads)"
    ) in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target, excluded",
    [
        pytest.param(_files("/a"), [], id="same"),
        pytest.param(
            {
                "files": {
                    **_files("/a")["files"],
                    "env": {},
                    "timeout": 5_000,
                    "autoApprove": ["read"],
                    "disabledTools": ["write"],
                }
            },
            ["@files/write"],
            id="same-launch",
        ),
        pytest.param(
            {"files": {**_files("/a")["files"], "type": "stdio"}}, [], id="explicit-stdio"
        ),
        pytest.param(
            {"files": {**_files("/a")["files"], "poolable": True}}, [], id="poolable-hint"
        ),
    ],
)
async def test_a_target_configuring_a_pooled_server_alike_shares_the_sessions_stub(
    agents_dir, files_pooled, target, excluded
):
    _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": _files("/a")})
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": target})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    helper = extras.custom_agents[1]
    assert "files" not in helper.get("mcpServers", {})
    # Its own tool switches still bind on the session's copy.
    assert [t for t in helper.get("excludedTools", []) if t.startswith("@files")] == excluded
    assert "helper" not in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "switch_off",
    [
        pytest.param({"disabled": True}, id="disabled"),
        # The shared launch predicate: anything but ``false`` mutes.
        pytest.param({"disabled": None}, id="null"),
        pytest.param({"disabled": "yes"}, id="non-boolean"),
        pytest.param({"disabled": True, "timeoutMs": 5_000}, id="beside-a-start-budget"),
    ],
)
async def test_a_target_that_mutes_a_pooled_server_never_reaches_the_sessions_stub(
    agents_dir, files_pooled, switch_off
):
    # The session-level stub serves every agent of the batch, so the target's own
    # mute has to bind as an exclusion: dropping its declaration is not enough.
    _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": _files("/a")})
    muted = {"files": {**_files("/a")["files"], **switch_off}}
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": muted})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    assert "files" not in extras.custom_agents[1].get("mcpServers", {})
    wire = _wire(extras)
    assert "@files" in wire["helper"].get("excludedTools", [])
    # The session's own agent still loads the stub it declares.
    assert "@files" not in wire["delegator"].get("excludedTools", [])


@pytest.mark.asyncio
async def test_a_target_muting_a_differently_rooted_pooled_server_still_registers(
    agents_dir, files_pooled
):
    # A muted declaration never launches, so its other root cannot reach the stub.
    _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": _files("/a")})
    muted = {"files": {**_files("/b")["files"], "disabled": True}}
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": muted})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    wire = _wire(extras)
    assert "@files" in wire["helper"].get("excludedTools", [])
    assert "@files" not in wire["delegator"].get("excludedTools", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", ["session-crew-server", "mcp-json-server"])
async def test_a_target_that_mutes_a_server_the_batch_shares_keeps_it_off(
    agents_dir, mcp_json, shared
):
    # Neither is a pooled stub the target's own spec could be compared with: one is
    # hoisted from the session's own entry, the other read from the settings file.
    from kiro_crew.acp.kas_settings_mcp import without_excluded_stubs

    if shared == "session-crew-server":
        name, entry = "kirocrew-core", _CORE["kirocrew-core"]
        _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": _CORE})
    else:
        name, entry = "pooled", {"command": "p"}
        mcp_json.write_text(json.dumps({"mcpServers": {name: entry}}), encoding="utf-8")
        _write(agents_dir, _delegator("helper", tools=("*",)))
    muted = {name: {**entry, "disabled": True}}
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": muted})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    wire = _wire(extras)
    assert f"@{name}" in wire["helper"]["excludedTools"]
    if shared == "session-crew-server":
        assert f"@{name}" not in wire["delegator"].get("excludedTools", [])
    else:
        # No agent of the batch loads it, so its stub is not sent at all.
        assert without_excluded_stubs([{"name": name}], extras.custom_agents) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_budget, target_budget",
    [
        pytest.param({"timeout": 90_000}, {"timeoutMs": 90_000}, id="alias-in-target"),
        pytest.param({"timeoutMs": 90_000}, {}, id="alias-in-session-agent"),
    ],
)
async def test_a_target_spelling_the_start_budget_timeout_ms_shares_the_sessions_stub(
    agents_dir, files_pooled, parent_budget, target_budget
):
    # ``timeoutMs`` is the alias of ``timeout`` (``kas_settings_mcp._with_timeout``):
    # a start budget, never a different launch.
    parent = {"files": {**_files("/a")["files"], **parent_budget}}
    _write(agents_dir, {**_delegator("helper", tools=("*",)), "mcpServers": parent})
    target = {"files": {**_files("/a")["files"], **target_budget}}
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": target})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    assert "files" not in extras.custom_agents[1].get("mcpServers", {})
    assert "helper" not in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_crew_server_the_session_injects_reaches_the_target_as_the_sessions_copy(
    agents_dir, files_pooled
):
    # The session's own copy carries its identity; the target's would launch without.
    crew = {_INJECTED_CREW: {"command": "kirocrew", "args": ["mcp-dashboard"]}}
    _write(agents_dir, _delegator("helper", tools=("*",)))
    _write(agents_dir, {**_SLACK, "name": "helper", "tools": ["*"], "mcpServers": crew})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "helper"]
    assert _INJECTED_CREW not in extras.custom_agents[1].get("mcpServers", {})
    assert "helper" not in extras.withheld_credentials_notice


_WORKSPACE = {
    "server": {"name": "workspace", "command": "bridge", "args": [], "env": []},
    "workdir": "/w",
}


def _restricted_target() -> dict:
    # A file-path limit KAS would judge workspace tools by as ``mcp``, never as
    # ``fs_read``, so a remote workspace cannot hold the agent to it.
    denied = {"fs_read": {"allowedPaths": ["~/slack"], "deniedPaths": ["~/slack/private"]}}
    return {**_SLACK, "toolsSettings": denied}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["session/new", "session/load"])
async def test_a_remote_workspace_starts_without_a_target_it_cannot_bind(
    agents_dir, monkeypatch, tmp_path, path
):
    """The session's own agent has no such rules, so the session starts on both
    paths and only the hand-off to the target is lost, named in the notice."""
    from acp_runtime_helpers import _make_runtime

    from kiro_crew.acp import runtime_session_load, session_handle
    from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD, METHOD_SESSION_NEW

    _write(agents_dir, _delegator("slack-agent"))
    _write(agents_dir, _restricted_target())
    projected = await _extras()
    assert _ids(projected) == ["delegator", "slack-agent"]

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    # No engine here reports an MCP server, so neither start-up wait has anything
    # to hold for.
    monkeypatch.setattr(session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0.0)
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 0.0)
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._can_load_session = True
    rt._remote_workspace = dict(_WORKSPACE)
    sent: dict[str, dict] = {}

    async def _fake_send(method, params, timeout=None):
        sent.setdefault(method, params)
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-remote", "modes": {"currentModeId": "delegator"}}
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "delegator"}, "models": []}
        return {}

    # Without the projected servers: their readiness is not what this pins, and
    # no engine here reports them.
    batch = [{k: v for k, v in a.items() if k != "mcpServers"} for a in projected.custom_agents]
    extras = dataclasses.replace(
        projected, custom_agents=batch, managed_entries={}, app_launches={}
    )

    async def _fake_agents(agent, **_kw):
        return extras

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)

    if path == "session/new":
        handle = await rt.create_session(
            cwd=str(tmp_path), agent="delegator", mcp_servers=[], session_key="k"
        )
    else:
        handle = await rt.load_session(
            "", "sid-remote", cwd=str(tmp_path), agent="delegator", session_key="k"
        )

    assert [a["id"] for a in handle.kas_registered_agents] == ["delegator"]
    wire = sent[path]["_meta"]["kiro"]["customAgents"]
    assert [a["id"] for a in wire] == ["delegator"]
    notice = handle.kas_withheld_credentials_notice
    assert "agent delegator cannot hand work to slack-agent" in notice
    assert "remote workspace cannot enforce them" in notice


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["session/new", "session/load"])
@pytest.mark.parametrize("server", ["pooled", "workspace"])
async def test_a_remote_workspace_sends_no_pooled_stub_only_a_left_out_target_loads(
    agents_dir, mcp_json, monkeypatch, tmp_path, path, server
):
    """The session's own agent does not load ``mcp.json``; the one target that
    does is left out by the remote workspace, so no agent the session still
    carries loads the file's server, and its pooled stub is neither sent nor
    recorded as sent. A file server named like the workspace server does not
    take the workspace's own entry with it."""
    from acp_runtime_helpers import _make_runtime

    from kiro_crew.acp import runtime_session_load, session_handle
    from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD, METHOD_SESSION_NEW

    mcp_json.write_text(json.dumps({"mcpServers": {server: {"command": "p"}}}), encoding="utf-8")
    _write(agents_dir, _delegator("slack-agent"))
    _write(agents_dir, {**_restricted_target(), "includeMcpJson": True})
    projected = await _extras()
    assert _ids(projected) == ["delegator", "slack-agent"]

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0.0)
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 0.0)
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._can_load_session = True
    rt._remote_workspace = dict(_WORKSPACE)
    sent: dict[str, dict] = {}

    async def _fake_send(method, params, timeout=None):
        sent.setdefault(method, params)
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-remote", "modes": {"currentModeId": "delegator"}}
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "delegator"}, "models": []}
        return {}

    batch = [{k: v for k, v in a.items() if k != "mcpServers"} for a in projected.custom_agents]
    extras = dataclasses.replace(
        projected, custom_agents=batch, managed_entries={}, app_launches={}
    )

    async def _fake_agents(agent, **_kw):
        return extras

    stub = {"name": server, "command": "kirocrew", "args": ["mcp-stub", server], "env": []}

    async def _pooled(servers, *_a, **_kw):
        return [*servers, dict(stub)]

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
    monkeypatch.setattr(rt, "_unpooled_control_planes", _pooled)

    if path == "session/new":
        handle = await rt.create_session(
            cwd=str(tmp_path), agent="delegator", mcp_servers=[dict(stub)], session_key="k"
        )
    else:
        handle = await rt.load_session(
            "", "sid-remote", cwd=str(tmp_path), agent="delegator", session_key="k"
        )

    assert [a["id"] for a in handle.kas_registered_agents] == ["delegator"]
    assert sent[path]["mcpServers"] == [_WORKSPACE["server"]]
    assert handle.mcp_session_report().configured == ("workspace",)


def test_a_remote_workspace_still_refuses_the_sessions_own_restricted_agent():
    from types import SimpleNamespace

    from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError

    denied = {"rules": [{"capability": "fs_read", "match": ["/x"], "effect": "deny"}]}
    agents = [
        {"id": "delegator", "tools": ["fs_read"], "permissions": denied},
        {"id": "slack-agent", "tools": ["fs_read"]},
    ]
    with pytest.raises(AcpRuntimeError, match="this agent's own rules"):
        AcpRuntime._remote_workspace_agents(
            SimpleNamespace(_remote_workspace=dict(_WORKSPACE)),
            agents,
            [{"name": "workspace"}],
            active_agent="delegator",
            notices_out=[],
        )


# What a registered target auto-approves meets the hooks that fire on its calls.


#: A target granting what the delegator does not: KAS decides its fetches itself.
_FETCHER = {
    "name": "fetcher",
    "prompt": "Fetch pages.",
    "tools": ["web_fetch"],
    "allowedTools": ["web_fetch"],
}


@pytest.fixture
def hooks_page(tmp_path, monkeypatch):
    """The Hooks page's store, empty, and no spec-hook read left over from another test."""
    from kiro_crew.acp import kas_agents
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.hooks import ScriptHookStore

    store = ScriptHookStore(config_dir=tmp_path / "hooks")
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: store)
    spec_hooks._cache.clear()
    yield store
    spec_hooks._cache.clear()


def _fetch_guard(store) -> None:
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook

    store._hooks["page"] = ScriptHook(
        id="page", event=HOOK_EVENT_PRE_TOOL_USE, matcher="web_fetch", command="guard.sh"
    )


def _provider(handle):
    """A KAS provider over *handle*, exposing what both real providers expose."""
    from types import SimpleNamespace

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    return SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_KAS),
        kas_auto_approved_capabilities=handle.kas_auto_approved,
        kas_registered_agents=handle.kas_registered_agents,
        kas_projected_agent=handle.kas_projected_agent,
    )


def _live(extras):
    """A live KAS session that registered *extras*' batch for the delegator."""
    from types import SimpleNamespace

    handle = SimpleNamespace()
    KasHarness().record_session_projection(handle, extras.custom_agents, "delegator")
    return _provider(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "held",
    [
        # The delegator denies fetches outright.
        {"capability": "web_fetch", "effect": "deny"},
        # The delegator asks for everything, so it records nothing auto-approved.
        {"capability": "all", "effect": "ask"},
    ],
)
async def test_a_hook_added_on_what_a_target_auto_approves_makes_the_session_stale(
    agents_dir, hooks_page, held
):
    """A Hooks-page PreToolUse hook added mid-session on a capability only a
    registered target auto-approves re-projects the session: KAS decides that
    target's calls itself, so the hook would never see them."""
    from kiro_crew.acp.kas_permissions import unprompted_capabilities
    from kiro_crew.agent_sdk import spec_hooks

    _write(agents_dir, {**_delegator("fetcher"), "permissions": {"rules": [held]}})
    _write(agents_dir, _FETCHER)
    extras = await _extras()
    assert _ids(extras) == ["delegator", "fetcher"]
    parent, target = extras.custom_agents
    assert "web_fetch" not in unprompted_capabilities(parent.get("permissions"))
    assert "web_fetch" in unprompted_capabilities(target.get("permissions"))
    live = _live(extras)
    assert await spec_hooks.hook_projection_stale(live, "delegator") is False

    _fetch_guard(hooks_page)

    assert await spec_hooks.hook_projection_stale(live, "delegator") is True


@pytest.mark.asyncio
async def test_a_switch_back_to_the_delegator_keeps_its_targets_in_the_stale_check(
    agents_dir, hooks_page
):
    """KAS keeps the whole batch across a mode switch, so once the session is back
    on the delegator its stale check still counts what its registered target
    auto-approves."""
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.types import UPDATE_CURRENT_MODE
    from kiro_crew.agent_sdk import spec_hooks

    held = {"capability": "web_fetch", "effect": "deny"}
    _write(agents_dir, {**_delegator("fetcher"), "permissions": {"rules": [held]}})
    _write(agents_dir, _FETCHER)
    extras = await _extras()
    assert _ids(extras) == ["delegator", "fetcher"]
    handle = object.__new__(AcpSessionHandle)
    KasHarness().record_session_projection(handle, extras.custom_agents, "delegator")
    handle._last_kas_mode_id = "delegator"
    handle._handle_kas_update(UPDATE_CURRENT_MODE, {"currentModeId": "fetcher"})
    handle._handle_kas_update(UPDATE_CURRENT_MODE, {"currentModeId": "delegator"})
    assert handle.kas_projected_agent == "delegator"
    live = _provider(handle)
    assert await spec_hooks.hook_projection_stale(live, "delegator") is False

    _fetch_guard(hooks_page)

    assert await spec_hooks.hook_projection_stale(live, "delegator") is True


@pytest.mark.asyncio
async def test_a_fresh_batch_is_not_stale_when_the_delegators_own_hook_covers_a_target(
    agents_dir, hooks_page
):
    """Right after projection, with no hook changed, the batch reads fresh. The
    delegator's own spec PreToolUse hook fires on every permission request of its
    turn, a native sub-agent's included, so the target does not auto-approve what
    that hook covers either; otherwise the session would reset on every claim
    until ``StaleProjectionError``."""
    from kiro_crew.acp.kas_permissions import unprompted_capabilities
    from kiro_crew.agent_sdk import spec_hooks

    guard = {"preToolUse": [{"matcher": "web_fetch", "command": "guard.sh"}]}
    _write(agents_dir, {**_delegator("fetcher"), "hooks": guard})
    _write(agents_dir, _FETCHER)
    extras = await _extras()
    assert _ids(extras) == ["delegator", "fetcher"]
    _parent, target = extras.custom_agents

    assert "web_fetch" not in unprompted_capabilities(target.get("permissions"))
    assert await spec_hooks.hook_projection_stale(_live(extras), "delegator") is False


@pytest.mark.asyncio
async def test_one_entrys_held_mcp_tool_never_stands_for_another_entrys_grant(
    agents_dir, hooks_page, monkeypatch
):
    """The stale check reads each registered entry on its own. The delegator holds
    one MCP tool back with a scoped ``ask``; the target auto-approves that server
    whole. A hook added on that tool leaves the delegator fresh and the target
    stale, so the session is stale: a union of the two answers would carry the
    delegator's hold and read the target's grant as held too."""
    from types import SimpleNamespace

    from kiro_crew.acp import kas_permissions
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook

    monkeypatch.setattr(
        kas_permissions, "cached_mcp_tool_names", lambda s: ["get_creds"] if s == "vault" else None
    )
    grant = {"capability": "mcp", "match": ["vault/*"], "effect": "allow"}
    hold = {"capability": "mcp", "match": ["vault/get_creds"], "effect": "ask"}
    batch = [
        {"id": "delegator", "permissions": {"rules": [grant, hold]}},
        {"id": "fetcher", "permissions": {"rules": [grant]}},
    ]
    handle = SimpleNamespace()
    KasHarness().record_session_projection(handle, batch, "delegator")
    live = _provider(handle)
    assert await spec_hooks.hook_projection_stale(live, "delegator") is False

    hooks_page._hooks["page"] = ScriptHook(
        id="page", event=HOOK_EVENT_PRE_TOOL_USE, matcher="@vault/get_creds", command="guard.sh"
    )

    assert await spec_hooks.hook_projection_stale(live, "delegator") is True
    alone = _provider(SimpleNamespace(**{**vars(handle), "kas_registered_agents": batch[:1]}))
    assert await spec_hooks.hook_projection_stale(alone, "delegator") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [True, False])
async def test_a_target_server_gets_the_ssh_agent_the_session_may_forward(
    agents_dir, monkeypatch, forwarded
):
    """With forwarding permitted by the engine's launch verdict and the sandbox's
    current one, a target's unpooled stdio server is launched with the ssh-agent
    pointer, as the session's own agent's server is; without it, neither is."""
    import sys

    from kiro_crew import kas_env_shim, sandbox
    from kiro_crew.acp import kas_env_launch

    monkeypatch.setattr(sandbox, "_forward_ssh_auth_sock", lambda _mode, _hidden: True)
    monkeypatch.setattr(
        kas_env_launch,
        "inherited_auth_env",
        lambda forward: {"SSH_AUTH_SOCK": "/run/agent.sock"} if forward else {},
    )
    server = {"command": sys.executable, "args": ["-c", "pass"]}
    _write(agents_dir, {**_delegator("git-agent"), "mcpServers": {"own-git": server}})
    _write(agents_dir, {**_SLACK, "name": "git-agent", "mcpServers": {"git": server}})
    harness = KasHarness()
    harness.spawn_sandbox_mode = "strict"

    extras = await harness.session_extras(
        "delegator",
        work_dir=None,
        session_key="dashboard_chat-target-ssh-1",
        ssh_auth_sock_forwarded=forwarded,
    )

    assert _ids(extras) == ["delegator", "git-agent"]
    parent, target = extras.custom_agents
    assert (kas_env_shim.SHIM_MODULE in parent["mcpServers"]["own-git"]["args"]) is forwarded
    assert (kas_env_shim.SHIM_MODULE in target["mcpServers"]["git"]["args"]) is forwarded

"""A user's custom agent gets Crew's default servers on claude and codex too.

``acp/default_crew_servers.py`` adds ``kirocrew-core`` and ``kirocrew-cron`` to a
user-level spec Crew did not write. kiro-cli and KAS apply it on their own paths
(``test_default_crew_servers.py``); the mirrors in
``ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY`` get it from the shared translation
their ``session/new`` array is built from. These tests pin that array for a custom
agent, its parity with Crew's default agent, the servers left out because those
mirrors mount a server whole and apply no ``excludedTools`` (every app server, and
a default one an exclusion may reach), the residual they keep, the absent audit
record, and the cases that stay as written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import codex_session_mcp_helpers
import pytest
from codex_session_mcp_helpers import _CORE, _CRON, _by_name

from kiro_crew import agent as agent_mod
from kiro_crew.acp import default_crew_servers as defaults
from kiro_crew.acp import session_mcp
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror
from kiro_crew.providers.mirrors.codex import codex_projection

# The shared fixture, bound here by name so pytest registers it for this module.
agents_dir = codex_session_mcp_helpers.agents_dir

_CREW = {"kirocrew-core", "kirocrew-cron"}


@pytest.fixture
def custom(agents_dir, monkeypatch):
    """A hand-written user-level agent, and the seams the default-server helper reads."""
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        agent_mod,
        "managed_mcp_spec_entry",
        lambda name, **kwargs: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)

    def write(spec: dict, name: str = "mine") -> str:
        text = json.dumps({"name": name, **spec})
        (agents_dir / f"{name}.json").write_text(text, encoding="utf-8")
        return text

    return write


def _claude_names(agent: str, **kwargs) -> set[str]:
    params = ClaudeCodeMirror().session_params(agent, permission_surface_owned=True, **kwargs)
    return {e["name"] for e in params["mcpServers"]}


def test_the_claim_is_the_two_measured_mirrors():
    """Only the harnesses a live custom-agent turn was measured on carry the claim."""
    assert ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY == frozenset(
        {ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX}
    )
    assert ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY <= ACP_BACKENDS_SESSION_MCP_ARRAY


def test_claude_mounts_both_servers_for_a_custom_agent_and_writes_nothing(agents_dir, custom):
    text = custom({"tools": ["fs_read", "execute_bash"]})
    assert _claude_names("mine") == _CREW
    assert (agents_dir / "mine.json").read_text(encoding="utf-8") == text


def test_codex_mounts_both_as_crew_s_default_agent_does(custom):
    """Same elements, identity env included, as the default agent's in the same session."""
    custom({"tools": ["fs_read"]})
    custom({"tools": ["fs_read", "@kirocrew-core", "@kirocrew-cron"]}, name="kirocrew")
    key = "dashboard:chat-1"
    mine = _by_name(codex_projection("mine", session_key=key).params["mcpServers"])
    default = _by_name(codex_projection("kirocrew", session_key=key).params["mcpServers"])
    assert set(mine) == _CREW
    assert mine == {name: default[name] for name in _CREW}
    assert all(element["env"] for element in mine.values()), "identity rides the element"


def test_the_shared_translation_adds_them_only_when_the_mirror_asks(custom):
    custom({"tools": ["fs_read"]})
    asked = session_mcp.session_mcp_projection("mine", default_crew_servers=True)
    unasked = session_mcp.session_mcp_projection("mine")
    assert {e["name"] for e in asked.servers} == _CREW
    assert unasked.servers == []
    assert asked.allowlist.grants("kirocrew-core") and not unasked.allowlist.grants("kirocrew-core")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"tools": ["fs_read"], "excludedTools": ["@kirocrew-cron"]}, {"kirocrew-core"}),
        (
            {"tools": ["fs_read"], "managedToolPolicy": {"excludeServers": ["kirocrew-core"]}},
            {"kirocrew-cron"},
        ),
        ({"tools": []}, set()),
        ({}, set()),
    ],
    ids=["excluded-tools-opt-out", "exclude-servers-opt-out", "empty-tools", "no-tools"],
)
def test_the_helper_s_opt_outs_hold_on_the_array(custom, spec, expected):
    custom(spec)
    assert _claude_names("mine") == expected


@pytest.mark.parametrize(
    ("excluded", "expected"),
    [
        ("@kirocrew-core/spawn_run", {"kirocrew-cron"}),
        ("@kirocrew-core/spawn_*", {"kirocrew-cron"}),
        ("@KIROCREW-CORE/spawn_run", {"kirocrew-cron"}),
        # Which server's tools a server glob names is unknowable without connecting.
        ("@kirocrew-*/spawn_*", set()),
    ],
    ids=["one-tool", "tool-glob", "case-folded", "server-glob"],
)
def test_a_server_the_spec_narrows_per_tool_is_not_added(custom, excluded, expected):
    """Neither mirror applies ``excludedTools``, so a narrowed server mounted whole would
    run the excluded tool; it is left out instead, and an untouched server still mounts."""
    custom({"tools": ["fs_read"], "excludedTools": ["fs_write", excluded]})
    assert _claude_names("mine") == expected
    codex = codex_projection("mine", session_key="dashboard:chat-1")
    assert set(_by_name(codex.params["mcpServers"])) == expected


@pytest.mark.parametrize(
    "excluded",
    [["*"], ["@*"], ["@*/spawn_run"], ["*spawn_run"], ["?*"], ["[@]*"]],
    ids=["star", "any-server", "any-server-tool", "leading-glob", "leading-any", "leading-class"],
)
def test_an_exclusion_reaching_every_server_adds_none(custom, excluded):
    """An entry starting with a glob may match the ``@`` of any server's tool ids."""
    custom({"tools": ["fs_read"], "excludedTools": excluded})
    assert _claude_names("mine") == set()
    codex = codex_projection("mine", session_key="dashboard:chat-1")
    assert _by_name(codex.params["mcpServers"]) == {}


@pytest.mark.parametrize(
    "rule, expected",
    [
        (
            {"effect": "deny", "capability": "mcp", "match": ["kirocrew-core/spawn_run"]},
            {"kirocrew-cron"},
        ),
        ({"effect": "deny", "capability": "all", "match": ["kirocrew-*/spawn_*"]}, set()),
        (
            {"effect": "deny", "capability": "mcp", "match": ["kirocrew-core/*"], "exclude": ["x"]},
            {"kirocrew-cron"},
        ),
        ({"effect": "deny", "capability": "mcp"}, set()),
        ({"effect": "allow", "capability": "mcp", "match": ["kirocrew-core/spawn_run"]}, _CREW),
        ({"effect": "deny", "capability": "fs", "match": ["kirocrew-core/spawn_run"]}, _CREW),
    ],
    ids=["one-tool", "server-glob", "carve-out", "every-resource", "allow", "other-capability"],
)
def test_a_permissions_deny_reaching_a_server_leaves_it_out(custom, rule, expected):
    """Neither mirror applies KAS ``permissions`` either, so a deny that may reach one of a
    server's tools leaves that server out, the same as an ``excludedTools`` entry."""
    custom({"tools": ["fs_read"], "permissions": {"rules": [rule]}})
    assert _claude_names("mine") == expected
    codex = codex_projection("mine", session_key="dashboard:chat-1")
    assert set(_by_name(codex.params["mcpServers"])) == expected


def test_kas_keeps_a_per_tool_narrowed_server(custom):
    """The host that applies ``excludedTools`` itself still gets the server."""
    spec = {"name": "mine", "tools": ["fs_read"], "excludedTools": ["@kirocrew-core/spawn_run"]}
    assert set(defaults.with_default_crew_servers("mine", spec)["mcpServers"]) == _CREW


@pytest.mark.parametrize(
    "granted",
    [["tool_search"], ["tool_search", "@notes:notes/read"], ["*"]],
    ids=["loader", "loader-and-one-app-tool", "star"],
)
def test_no_app_server_is_added_on_these_mirrors(custom, monkeypatch, granted):
    """Both mount a server with every tool and apply no ``excludedTools``, and codex
    runs an app tool marked ``readOnlyHint`` without asking, so an app's server,
    third-party code, is never added there. KAS still adds it, and narrows it."""
    launch = {"command": "/opt/app", "args": []}
    monkeypatch.setattr(
        defaults,
        "_app_servers",
        lambda: {"notes:notes": dict(launch), "mail:mail": {**launch, "disabledTools": ["send"]}},
    )
    custom({"tools": ["fs_read", *granted]})
    assert _claude_names("mine") == _CREW
    codex = codex_projection("mine", session_key="dashboard:chat-1")
    assert set(_by_name(codex.params["mcpServers"])) == _CREW
    kas = defaults.with_default_crew_servers("mine", {"name": "mine", "tools": granted})
    assert set(kas["mcpServers"]) == _CREW | {"notes:notes", "mail:mail"}


@pytest.mark.parametrize(
    "spec",
    [
        {"tools": ["*"], "excludedTools": ["@kirocrew-core"]},
        {"tools": ["*"], "excludedTools": ["@kirocrew-core/spawn_run"]},
        {"tools": ["fs_read", "@kirocrew-core"], "excludedTools": ["@kirocrew-core/spawn_run"]},
        {"tools": ["fs_read", "@kirocrew-core/skill_search"]},
    ],
    ids=["star-opt-out", "star-one-tool", "named-one-tool", "per-tool-grant"],
)
def test_a_control_plane_server_the_spec_names_still_mounts_whole(custom, spec):
    """Pins a residual these mirrors had before Crew added any server: when the spec's
    own ``tools`` names ``kirocrew-core``, the array re-adds it whole
    (``session_mcp_servers``) and nothing reads ``excludedTools``. Deferred, and
    stated in agent-spec-fields.md and the agent-host-contract row; a change that
    narrows it updates both and this test."""
    custom(spec)
    assert "kirocrew-core" in _claude_names("mine")
    codex = codex_projection("mine", session_key="dashboard:chat-1")
    assert "kirocrew-core" in _by_name(codex.params["mcpServers"])


def test_the_server_s_own_disabled_tools_still_turn_one_tool_off(custom):
    """The way the docs give to keep one of its tools off on these mirrors."""
    custom({"tools": ["*"], "mcpServers": {"kirocrew-core": {"disabledTools": ["spawn_run"]}}})
    assert "mcp__kirocrew-core__spawn_run" in session_mcp.session_mcp_deny_rules("mine")
    codex = codex_projection("mine", session_key="dashboard:chat-1")
    assert ("kirocrew-core", "spawn_run") in codex.denied_tools


def test_an_approved_addition_merges_before_the_defaults_as_on_kas(custom):
    """KAS merges a template's approved servers, then adds Crew's: a ``tools: []`` agent
    an approval gave one server has a tool, so it gets them on either host."""
    from kiro_crew import template_mcp_additions as tma

    entry = {"command": "/opt/fetch/bin/mcp-fetch", "args": ["--stdio"]}
    tma.add_addition("mine", "fetch", entry, approved_by="owner", card_id="c1")
    custom({"tools": []})
    assert _claude_names("mine") == _CREW | {"fetch"}
    kas = defaults.with_default_crew_servers("mine", tma.merge_at_spawn("mine", {"tools": []}))
    assert set(kas["mcpServers"]) == _CREW | {"fetch"}


@pytest.fixture
def per_tool_ceiling(monkeypatch):
    """A governance ceiling that denies one tool of ``kirocrew-cron``."""
    from kiro_crew.platform import context, governance, governance_profiles

    ceiling = governance.parse_policy(
        {
            "version": 1,
            "boot": {},
            "mcp": {"mode": "deny", "deny": ["@kirocrew-cron/cron_remove_all"]},
        }
    )
    monkeypatch.setattr(context, "current_context", lambda: SimpleNamespace(governance=ceiling))
    monkeypatch.setattr(
        governance_profiles,
        "_STORE",
        SimpleNamespace(resolved=lambda: True, all_profiles=lambda: ()),
    )
    events: list[dict] = []
    monkeypatch.setattr(
        "kiro_crew.acp.kas_agents.sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    return events


def test_the_array_records_no_withheld_approval(custom, per_tool_ceiling):
    """These mirrors grant no ``allowedTools`` approval, so neither a spawn's array nor
    ``kirocrew doctor``'s read of it writes the record a KAS projection writes."""
    custom({"tools": ["fs_read"], "allowedTools": ["*", "@kirocrew-*"]})
    assert _claude_names("mine") == _CREW
    codex_projection("mine", session_key="dashboard:chat-1")
    from kiro_crew.agent_sdk.drivers.acp import agent_spec_mcp_refs

    assert agent_spec_mcp_refs("mine", backends=[ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])[0]
    assert per_tool_ceiling == []
    spec = {"name": "mine", "tools": ["fs_read"], "allowedTools": ["*"]}
    defaults.with_default_crew_servers("mine", spec)
    assert len(per_tool_ceiling) == 1, "the KAS path still records it"


@pytest.fixture
def checkout(tmp_path):
    """A cloned folder whose ``.kiro/agents`` declares *name*, as Crew never trusts it."""

    def write(spec: dict, name: str = "mine"):
        project = tmp_path / "checkout"
        (project / ".kiro" / "agents").mkdir(parents=True, exist_ok=True)
        text = json.dumps({"name": name, **spec})
        (project / ".kiro" / "agents" / f"{name}.json").write_text(text, encoding="utf-8")
        return project

    return write


def test_a_user_agent_a_checkout_shadows_gets_them_under_its_switch_offs(
    custom, checkout, monkeypatch
):
    """As on KAS: the user-level spec runs, its default servers added BEFORE the
    checkout's switch-offs, so those still narrow them (one read of the checkout)."""
    custom({"tools": ["fs_read"]})
    project = checkout(
        {"tools": ["fs_read"], "mcpServers": {"kirocrew-core": {"disabledTools": ["spawn_run"]}}}
    )
    scans: list[str] = []
    real_scan = session_mcp._project_spec_path_for

    def counting_scan(agent, work_dir):
        scans.append(agent)
        return real_scan(agent, work_dir)

    monkeypatch.setattr(session_mcp, "_project_spec_path_for", counting_scan)
    projection = session_mcp.session_mcp_projection(
        "mine", work_dir=project, default_crew_servers=True
    )
    assert scans == ["mine"], "the checkout is scanned once"
    assert {e["name"] for e in projection.servers} == _CREW
    assert ("kirocrew-core", "spawn_run") in projection.disabled_tools
    codex = codex_projection("mine", work_dir=project, session_key="dashboard:chat-1")
    assert ("kirocrew-core", "spawn_run") in codex.denied_tools
    rules = session_mcp.session_mcp_deny_rules("mine", work_dir=project)
    assert "mcp__kirocrew-core__spawn_run" in rules


def test_an_agent_only_a_checkout_declares_runs_as_written(custom, checkout):
    project = checkout({"tools": ["fs_read"]}, name="theirs")
    assert _claude_names("theirs", work_dir=project) == set()


def test_crew_s_own_agent_is_not_widened(custom):
    """A spec Crew writes keeps exactly the servers its own ``tools`` names."""
    custom({"tools": ["fs_read", "@kirocrew-core"]}, name="kirocrew")
    assert _claude_names("kirocrew") == {"kirocrew-core"}

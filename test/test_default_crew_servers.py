"""A user's custom agent gets Crew's default servers on kiro-cli and KAS alike.

One helper (``acp/default_crew_servers.py``) adds ``kirocrew-core`` and
``kirocrew-cron`` to a user-level spec Crew did not write. kiro-cli reads it
through the agent's skill view, KAS through the projected spec. These tests pin
the helper, both backends' use of it, and the opt-outs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import native_skill_projection_helpers
import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew import agent_state
from kiro_crew.acp import default_crew_servers as defaults
from kiro_crew.acp.harness.kas import KasHarness, resolve_projected_spec
from kiro_crew.dashboard.side_readonly_spec import OWNER_MARKER

# The skill view suites' shared fixtures, bound here by name so pytest registers them.
native_tree = native_skill_projection_helpers.native_tree
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)

_CORE = {"command": "test-core", "args": ["mcp"]}
_CRON = {"command": "test-core", "args": ["mcp-cron"]}


def _managed(name, *, include_opt_in=False):
    del include_opt_in
    return {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}.get(name)


@pytest.fixture
def unopenable_state(tmp_path_factory):
    """A present ``agent_model_state.json`` the real reader cannot open (EACCES).

    The suite pins the sidecar's directory per test; the guard keeps the chmod off
    any file outside this run's temp tree. Whether mode 0 refuses a read is probed,
    not assumed: Windows keeps the file readable, and so does root.
    """
    state_path = agent_state._state_path()
    assert state_path.is_relative_to(tmp_path_factory.getbasetemp())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    state_path.chmod(0)
    try:
        try:
            with open(state_path, "rb"):
                pass
        except OSError:
            yield state_path
        else:
            pytest.skip("mode 0 does not refuse a read on this host")
    finally:
        state_path.chmod(0o600)


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", _managed)
    monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)


class TestHelper:
    def test_a_custom_agent_gets_both_servers_and_grants(self, managed):
        assert agent_state.get_capabilities("mine") is None
        spec = {"name": "mine", "tools": ["fs_read"]}
        out = defaults.with_default_crew_servers("mine", spec)
        assert out["tools"] == ["fs_read", "@kirocrew-core", "@kirocrew-cron"]
        assert out["mcpServers"] == {"kirocrew-core": _CORE, "kirocrew-cron": _CRON}
        assert spec == {"name": "mine", "tools": ["fs_read"]}, "the input is not mutated"

    @pytest.mark.parametrize("agent", ["crew-0123456789abcdef01234567", "mine"])
    @pytest.mark.parametrize("status", ["saved", "pending"])
    def test_saved_capability_record_is_left_alone(
        self, managed, monkeypatch, tmp_path, agent, status
    ):
        state_path = tmp_path / "agent_state.json"
        monkeypatch.setattr(agent_state, "_state_path", lambda: state_path)
        record = {
            "schema_version": 1,
            "parent": {
                "name": "parent",
                "source": "user",
                "path": str(tmp_path / "parent.json"),
                "scope": "global",
                "project": "",
            },
            "accepted": {section: {} for section in agent_state.CAPABILITY_SECTIONS},
            "overrides": {section: {} for section in agent_state.CAPABILITY_SECTIONS},
            "status": status,
        }
        record["overrides"]["mcpServers"]["kirocrew-core"] = {"action": "remove"}
        state_path.write_text(json.dumps({agent: {"capabilities": record}}), encoding="utf-8")
        assert agent_state.get_capabilities(agent) == record
        spec = {"name": agent, "tools": ["fs_read"], "mcpServers": {}}
        assert defaults.with_default_crew_servers(agent, spec) is spec
        assert spec == {"name": agent, "tools": ["fs_read"], "mcpServers": {}}

    @pytest.mark.parametrize("state", ["{", '{"mine": {"capabilities": null}}'])
    def test_unreadable_capability_record_is_left_alone(
        self, managed, monkeypatch, tmp_path, state
    ):
        state_path = tmp_path / "agent_state.json"
        monkeypatch.setattr(agent_state, "_state_path", lambda: state_path)
        state_path.write_text(state, encoding="utf-8")
        with pytest.raises(ValueError):
            agent_state.get_capabilities("mine")
        spec = {"name": "mine", "tools": ["fs_read"], "mcpServers": {}}
        assert defaults.with_default_crew_servers("mine", spec) is spec
        assert spec == {"name": "mine", "tools": ["fs_read"], "mcpServers": {}}

    def test_capability_read_os_error_is_left_alone(self, managed, monkeypatch):
        def refuse(name):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(agent_state, "get_capabilities", refuse)
        spec = {"name": "mine", "tools": ["fs_read"], "mcpServers": {}}
        assert defaults.with_default_crew_servers("mine", spec) is spec
        assert spec == {"name": "mine", "tools": ["fs_read"], "mcpServers": {}}

    def test_unopenable_capability_state_is_left_alone(self, managed, unopenable_state):
        with pytest.raises(OSError):
            agent_state.get_capabilities("mine")
        spec = {"name": "mine", "tools": ["fs_read"], "mcpServers": {}}
        assert defaults.with_default_crew_servers("mine", spec) is spec
        assert spec == {"name": "mine", "tools": ["fs_read"], "mcpServers": {}}

    def test_kirocrew_work_is_not_a_default(self, managed):
        out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
        assert "kirocrew-work" not in out["mcpServers"]
        assert "@kirocrew-work" not in out["tools"]

    def test_the_entry_carries_no_session_identity_or_approval(self, managed):
        out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
        for entry in out["mcpServers"].values():
            assert "autoApprove" not in entry
            assert "KIROCREW_SESSION_KEY" not in json.dumps(entry)

    def test_a_star_grant_gets_the_entries_only(self, managed):
        out = defaults.with_default_crew_servers("mine", {"tools": "*"})
        assert out["tools"] == "*"
        assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
        out = defaults.with_default_crew_servers("mine", {"tools": ["*", "fs_read"]})
        assert out["tools"] == ["*", "fs_read"]

    @pytest.mark.parametrize("tools", [None, [], "fs_read", {"a": 1}])
    def test_an_agent_granting_no_tools_stays_tool_less(self, managed, tools):
        spec = {"name": "mine"} if tools is None else {"name": "mine", "tools": tools}
        assert defaults.with_default_crew_servers("mine", spec) is spec

    def test_an_authored_declaration_is_kept_as_written(self, managed):
        muted = {"command": "x", "disabled": True}
        spec = {"tools": ["fs_read"], "mcpServers": {"kirocrew-cron": muted, "other": {}}}
        out = defaults.with_default_crew_servers("mine", spec)
        assert out["mcpServers"]["kirocrew-cron"] == muted
        assert out["mcpServers"]["other"] == {}
        assert out["mcpServers"]["kirocrew-core"] == _CORE
        assert out["tools"] == ["fs_read", "@kirocrew-core"]

    def test_a_per_tool_grant_is_not_widened(self, managed):
        out = defaults.with_default_crew_servers("mine", {"tools": ["@kirocrew-core/skill_search"]})
        assert out["tools"] == ["@kirocrew-core/skill_search", "@kirocrew-cron"]
        assert "kirocrew-core" in out["mcpServers"]

    @pytest.mark.parametrize(
        "opt_out",
        [
            {"managedToolPolicy": {"excludeServers": ["kirocrew-core", "kirocrew-cron"]}},
            {"excludedTools": ["@kirocrew-core", "@kirocrew-cron"]},
            {"excludedTools": ["@kirocrew-core/*", "@kirocrew-cron/*"]},
            # Every MCP server, and every tool.
            {"excludedTools": ["@*"]},
            {"excludedTools": ["*"]},
            # KAS's own name for every MCP tool, which a v3 agent file may use.
            {"excludedTools": ["@mcp"]},
            # A permissions deny of every MCP tool, or of each server whole.
            {"permissions": {"rules": [{"capability": "mcp", "effect": "deny"}]}},
            {"permissions": {"rules": [{"capability": "all", "effect": "deny", "match": []}]}},
            {
                "permissions": {
                    "rules": [
                        {
                            "capability": "mcp",
                            "effect": "deny",
                            "match": ["kirocrew-core/*", "kirocrew-cron"],
                        }
                    ]
                }
            },
            {"permissions": {"rules": [{"capability": "mcp", "effect": "deny", "match": ["*/*"]}]}},
            # Read as written: a block KAS's projection refuses whole (unknown
            # capability "read") still says the author withholds MCP.
            {
                "permissions": {
                    "rules": [
                        {"capability": "read", "effect": "allow"},
                        {"capability": "mcp", "effect": "deny"},
                    ]
                }
            },
        ],
    )
    def test_an_agent_that_excludes_the_servers_gets_neither(self, managed, opt_out):
        spec = {"tools": ["fs_read"], **opt_out}
        assert defaults.with_default_crew_servers("mine", spec) is spec

    @pytest.mark.parametrize(
        "rule",
        [
            {"capability": "mcp", "effect": "deny", "match": ["kirocrew-core/spawn_run"]},
            {"capability": "mcp", "effect": "deny", "match": ["kirocrew-core/?"]},
            {"capability": "mcp", "effect": "deny", "exclude": ["kirocrew-core/*"]},
            {"capability": "mcp", "effect": "ask"},
            {"capability": "shell", "effect": "deny"},
        ],
    )
    def test_a_narrower_permissions_rule_is_not_an_opt_out(self, managed, rule):
        spec = {"tools": ["fs_read"], "permissions": {"rules": [rule]}}
        out = defaults.with_default_crew_servers("mine", spec)
        assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}

    def test_opting_out_of_one_server_keeps_the_other(self, managed):
        spec = {"tools": ["fs_read"], "excludedTools": ["@kirocrew-cron"]}
        out = defaults.with_default_crew_servers("mine", spec)
        assert set(out["mcpServers"]) == {"kirocrew-core"}
        assert out["tools"] == ["fs_read", "@kirocrew-core"]

    def test_a_per_tool_exclusion_is_not_an_opt_out(self, managed):
        spec = {"tools": ["fs_read"], "excludedTools": ["@kirocrew-core/spawn_run"]}
        out = defaults.with_default_crew_servers("mine", spec)
        assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
        assert out["excludedTools"] == ["@kirocrew-core/spawn_run"]

    @pytest.mark.parametrize(
        "agent",
        [
            "kirocrew",
            "kirocrew-guest",
            "kirocrew-lite",
            "kirocrew-worker",
            "kirocrew-mate-worker",
            "kirocrew-skill-view-0123456789abcdef01234567",
            "kirocrew-app-launch-deploy",
        ],
    )
    def test_crew_s_own_and_derived_specs_are_left_alone(self, managed, agent):
        spec = {"tools": ["fs_read"]}
        assert defaults.with_default_crew_servers(agent, spec) is spec

    def test_the_side_chat_read_only_spec_is_left_alone(self, managed):
        spec = {"tools": ["fs_read"], "description": f"{OWNER_MARKER} mine"}
        assert defaults.with_default_crew_servers("mine--readonly", spec) is spec

    def test_an_app_agent_is_left_alone(self, managed, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: object())
        spec = {"tools": ["fs_read"]}
        assert defaults.with_default_crew_servers("deploy", spec) is spec

    def test_a_registry_governed_install_adds_nothing(self, managed, monkeypatch):
        monkeypatch.setattr("kiro_crew.acp.session_mcp._registry_mode", lambda: True)
        spec = {"tools": ["fs_read"]}
        assert defaults.with_default_crew_servers("mine", spec) is spec

    def test_an_unresolvable_managed_entry_adds_nothing(self, managed, monkeypatch):
        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: None)
        spec = {"tools": ["fs_read"]}
        assert defaults.with_default_crew_servers("mine", spec) is spec


_APP_KEY = "pinger:ping"
_APP_ENTRY = {"command": "python3", "args": ["server.py"]}


def _install_app(monkeypatch, *, enabled=True):
    """``kirocrew.json`` mounting one installed app's server, as its registration writes it."""
    installed = {"mcpServers": {_APP_KEY: {**_APP_ENTRY, "autoApprove": ["*"]}}}
    monkeypatch.setattr(
        "kiro_crew.agent_materialization.worker_agent._installed_default_spec", lambda: installed
    )
    monkeypatch.setattr(
        "kiro_crew.agent_materialization.mcp_sources._app_owned_mcp_keys",
        lambda: {_APP_KEY: enabled},
    )


def test_a_tool_search_agent_gets_the_enabled_app_s_servers(managed, monkeypatch):
    _install_app(monkeypatch)
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read", "tool_search"]})
    assert out["mcpServers"] == {
        "kirocrew-core": _CORE,
        "kirocrew-cron": _CRON,
        _APP_KEY: _APP_ENTRY,
    }
    assert out["tools"] == [
        "fs_read",
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-cron",
        f"@{_APP_KEY}",
    ]


@pytest.mark.parametrize(
    ("spec", "enabled"),
    [
        ({"tools": ["fs_read"]}, True),
        ({"tools": ["fs_read", "tool_search"], "excludedTools": [f"@{_APP_KEY}"]}, True),
        ({"tools": ["fs_read", "tool_search"]}, False),
        ({"tools": ["fs_read", "tool_search"], "allowedTools": ["*"]}, True),
        ({"tools": ["fs_read", "tool_search"], "allowedTools": ["@pinger:*"]}, True),
        ({"tools": ["fs_read", "tool_search"], "allowedTools": ["*", f"@{_APP_KEY}"]}, True),
    ],
    ids=[
        "no-tool-search",
        "excluded",
        "app-disabled",
        "wildcard-approval",
        "app-glob-approval",
        "wildcard-beside-name",
    ],
)
def test_the_app_s_servers_are_not_added(managed, monkeypatch, spec, enabled):
    _install_app(monkeypatch, enabled=enabled)
    out = defaults.with_default_crew_servers("mine", spec)
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
    assert f"@{_APP_KEY}" not in out["tools"]


def test_an_app_server_the_agent_approves_by_name_is_added(managed, monkeypatch):
    _install_app(monkeypatch)
    spec = {"tools": ["fs_read", "tool_search"], "allowedTools": [f"@{_APP_KEY}"]}
    out = defaults.with_default_crew_servers("mine", spec)
    assert out["mcpServers"][_APP_KEY] == _APP_ENTRY


def test_the_read_only_copy_keeps_the_wildcard_rule(managed, monkeypatch):
    """Doctor's copy judges ``allowedTools`` as a spawn does: no server a wildcard keeps out."""
    from kiro_crew.agent_sdk import spec_with_default_crew_servers

    _install_app(monkeypatch)
    spec = {"tools": ["fs_read", "tool_search"], "allowedTools": ["*"]}
    out = spec_with_default_crew_servers("mine", spec)
    assert _APP_KEY not in out["mcpServers"]


def test_an_aliased_key_is_not_copied_as_the_app_s(managed, monkeypatch):
    """A slashed server name reaches ``kirocrew.json`` as an alias another server can hold."""
    installed = {"mcpServers": {"playwright-mcp": _APP_ENTRY, "kirocrew-core": _APP_ENTRY}}
    monkeypatch.setattr(
        "kiro_crew.agent_materialization.worker_agent._installed_default_spec", lambda: installed
    )
    monkeypatch.setattr(
        "kiro_crew.agent_materialization.mcp_sources._app_owned_mcp_keys",
        lambda: {"playwright-mcp": True, "kirocrew-core": True},
    )
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read", "tool_search"]})
    assert out["mcpServers"] == {"kirocrew-core": _CORE, "kirocrew-cron": _CRON}
    assert "@playwright-mcp" not in out["tools"]


def test_a_crew_mode_conductor_gets_no_app_server(managed, monkeypatch):
    """A custom agent ``agent.crew_mode_agent`` names is a conductor: Crew's own servers only."""
    from types import SimpleNamespace

    from kiro_crew import crew_mode

    _install_app(monkeypatch)
    cfg = SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="mine"))
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read", "tool_search"]})
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
    assert f"@{_APP_KEY}" not in out["tools"]


# --- the edition's servers -------------------------------------------------------

_EDITION_ENTRY = {"command": "/opt/edition/vend", "args": ["mcp"]}


class _EditionTooling:
    """An edition naming one of its two servers for custom agents, and one it lacks."""

    def __init__(self, names=("edition-vend", "edition-absent")):
        self._names = names
        self.server_reads = 0

    def extra_mcp_servers(self):
        self.server_reads += 1
        return {
            "edition-vend": {**_EDITION_ENTRY, "autoApprove": ["vend"]},
            "edition-other": {"command": "/opt/edition/other", "args": []},
        }

    def extra_skills(self):
        return []

    def custom_agent_servers(self):
        return self._names


class _OlderEditionTooling(_EditionTooling):
    custom_agent_servers = None


@pytest.fixture
def edition():
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context
    from kiro_crew.platform.context import reset_context

    def compose(tooling):
        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, mcp_tooling=tooling))

    compose(_EditionTooling())
    yield compose
    reset_context()


def test_a_custom_agent_gets_the_server_the_edition_names(managed, edition):
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
    assert out["mcpServers"]["edition-vend"] == _EDITION_ENTRY, "no autoApprove is copied"
    assert out["tools"] == ["fs_read", "@kirocrew-core", "@kirocrew-cron", "@edition-vend"]
    assert "edition-other" not in out["mcpServers"], "only the named servers ride"
    assert "edition-absent" not in out["mcpServers"], "a name the host does not supply adds nothing"
    assert "allowedTools" not in out


def test_the_user_s_env_for_the_edition_server_reaches_the_custom_agent(managed, edition):
    """Built as every spec Crew writes builds it, so the store's env applies too."""
    store = agent_mod._user_dir() / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    entry = {"command": "/opt/edition/vend", "env": {"USER_KEY": "mine"}}
    store.write_text(json.dumps({"mcpServers": {"edition-vend": entry}}), encoding="utf-8")
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
    assert out["mcpServers"]["edition-vend"] == {**_EDITION_ENTRY, "env": {"USER_KEY": "mine"}}


def test_the_edition_server_needs_no_loader(managed, edition):
    """Unlike an app's servers, the edition chose these names, so ``tool_search`` is not asked."""
    out = defaults.with_default_crew_servers("mine", {"tools": "*"})
    assert "edition-vend" in out["mcpServers"]
    assert out["tools"] == "*"


@pytest.mark.parametrize(
    "spec",
    [
        {"tools": ["fs_read"], "excludedTools": ["@edition-vend"]},
        {"tools": ["fs_read"], "managedToolPolicy": {"excludeServers": ["edition-vend"]}},
        {"tools": ["fs_read"], "mcpServers": {"edition-vend": {"command": "mine"}}},
        {"tools": ["fs_read"], "allowedTools": ["*"]},
        {"tools": ["fs_read"], "allowedTools": ["@*"]},
        {"tools": ["fs_read"], "allowedTools": ["@edition-*", "@edition-vend"]},
    ],
    ids=["excluded", "server-excluded", "author-declared", "wildcard", "mcp-wildcard", "glob"],
)
def test_the_edition_server_is_not_added(managed, edition, spec):
    out = defaults.with_default_crew_servers("mine", spec)
    if "edition-vend" in (spec.get("mcpServers") or {}):
        assert out["mcpServers"]["edition-vend"] == {"command": "mine"}, "the author's entry stands"
    else:
        assert "edition-vend" not in out["mcpServers"]
    assert "@edition-vend" not in out["tools"]


def test_an_edition_server_approved_by_name_is_added(managed, edition):
    spec = {"tools": ["fs_read"], "allowedTools": ["@edition-vend"]}
    out = defaults.with_default_crew_servers("mine", spec)
    assert out["mcpServers"]["edition-vend"] == _EDITION_ENTRY
    assert out["allowedTools"] == ["@edition-vend"]


@pytest.mark.parametrize("tools", [[], None])
def test_a_tool_less_agent_gets_no_edition_server(managed, edition, tools):
    spec = {} if tools is None else {"tools": tools}
    assert defaults.with_default_crew_servers("mine", spec) is spec


def test_a_crew_mode_conductor_gets_no_edition_server(managed, edition, monkeypatch):
    from kiro_crew import crew_mode

    cfg = SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="mine"))
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}


def test_crew_s_own_specs_get_no_edition_server(managed, edition):
    spec = {"tools": ["fs_read"]}
    assert defaults.with_default_crew_servers("kirocrew", spec) is spec


@pytest.mark.parametrize(
    "tooling",
    [_EditionTooling(names=()), _OlderEditionTooling()],
    ids=["names-none", "predates-the-method"],
)
def test_an_edition_naming_nothing_adds_only_crew_s_servers(managed, edition, tooling):
    edition(tooling)
    out = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}


@pytest.mark.parametrize(
    "spec",
    [
        {"tools": ["fs_read"], "excludedTools": ["@edition-vend", "@edition-absent"]},
        {"tools": ["fs_read"], "allowedTools": ["*"]},
    ],
    ids=["opted-out", "wildcard"],
)
def test_a_spec_that_wants_no_edition_server_reads_no_edition_entry(managed, edition, spec):
    """The names are filtered before ``extra_mcp_servers()`` and ``mcp.json`` are read."""
    tooling = _EditionTooling()
    edition(tooling)
    defaults.with_default_crew_servers("mine", spec)
    assert tooling.server_reads == 0


def test_a_conductor_reads_no_edition_entry(managed, edition, monkeypatch):
    from kiro_crew import crew_mode

    tooling = _EditionTooling()
    edition(tooling)
    cfg = SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="mine"))
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
    assert tooling.server_reads == 0


def test_the_public_default_names_no_server():
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    assert DefaultMcpToolingProvider().custom_agent_servers() == ()


def test_the_edition_server_is_not_counted_as_crew_s(managed, edition):
    """Its tools are not deferred as Crew's own are: the author's servers' rule applies."""
    out = defaults.with_default_crew_servers("mine", {"name": "mine", "tools": ["fs_read"]})
    assert defaults.crew_added_servers("mine", out) == ("kirocrew-core", "kirocrew-cron")


# --- the spec a session on each backend runs -------------------------------------


@pytest.fixture
def projects(monkeypatch):
    """Set what kiro-cli's skill-view admission answers, and count the asks."""
    from kiro_crew.acp import skill_projection

    asked: list[bool] = []

    def answer(value: bool) -> list[bool]:
        monkeypatch.setattr(
            skill_projection,
            "next_spawn_projects_skill_view",
            lambda: asked.append(value) or value,
        )
        return asked

    return answer


@pytest.mark.parametrize("view", [True, False])
def test_kiro_cli_runs_them_only_while_a_spawn_projects_the_skill_view(
    managed, edition, projects, view
):
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO

    projects(view)
    spec = {"tools": ["fs_read"]}
    out = defaults.session_spec_on(ACP_BACKEND_KIRO, "mine", spec)
    assert ("edition-vend" in (out.get("mcpServers") or {})) is view
    assert (out is spec) is not view, "a spawn that falls back runs the spec as written"


def test_kas_runs_them_after_the_template_s_approved_additions(managed, edition, projects):
    """As ``resolve_projected_spec``: a ``tools: []`` agent an approval gave a server has
    a tool, so its session also gets Crew's and the edition's servers."""
    from kiro_crew import template_mcp_additions as tma
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    projects(False)
    entry = {"command": "/opt/fetch/bin/mcp-fetch", "args": ["--stdio"]}
    tma.add_addition("mine", "fetch", entry, approved_by="owner", card_id="c1")
    out = defaults.session_spec_on(ACP_BACKEND_KAS, "mine", {"tools": []})
    assert set(out["mcpServers"]) == {"fetch", "kirocrew-core", "kirocrew-cron", "edition-vend"}


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_claude_and_codex_run_their_array_s_copy(managed, edition, projects, backend):
    """Crew's own two servers only, and not one the spec narrows per tool: those
    mirrors mount a server whole, so they get no edition server, as no app server."""
    from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY

    assert backend in ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY
    projects(False)
    out = defaults.session_spec_on(backend, "mine", {"tools": ["fs_read"]})
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
    narrowed = {"tools": ["fs_read"], "excludedTools": ["@kirocrew-cron/cron_remove"]}
    assert set(defaults.session_spec_on(backend, "mine", narrowed)["mcpServers"]) == {
        "kirocrew-core"
    }
    kas = defaults.session_spec_on(ACP_BACKEND_KAS, "mine", narrowed)
    assert set(kas["mcpServers"]) == {"kirocrew-core", "kirocrew-cron", "edition-vend"}


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_claude_and_codex_run_the_template_s_approved_additions_too(
    managed, edition, projects, backend
):
    """Their spawns merge an approved addition as KAS does, so a check counts it there."""
    from kiro_crew import template_mcp_additions as tma
    from kiro_crew.acp_backends import ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN

    assert backend in ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN
    projects(False)
    entry = {"command": "/opt/fetch/bin/mcp-fetch", "args": ["--stdio"]}
    tma.add_addition("mine", "fetch", entry, approved_by="owner", card_id="c1")
    out = defaults.session_spec_on(backend, "mine", {"tools": ["fs_read"]})
    assert set(out["mcpServers"]) == {"fetch", "kirocrew-core", "kirocrew-cron"}


def test_every_other_backend_runs_the_spec_as_written(managed, edition, projects):
    """A check that counted the added servers there would report a tool its chat lacks."""
    from kiro_crew.acp_backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY,
        ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN,
        selectable_backend_values,
    )

    projects(True)
    spec = {"tools": ["fs_read"]}
    others = set(selectable_backend_values()) - {ACP_BACKEND_KIRO, ACP_BACKEND_KAS}
    others -= ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY | ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN
    assert others, "the selectable set lists more harnesses than the two kiro ones"
    assert [
        b for b in sorted(others) if defaults.session_spec_on(b, "mine", spec) is not spec
    ] == []


def test_a_session_spec_that_cannot_be_resolved_is_judged_as_written(managed, monkeypatch):
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    def unreadable(*args, **kwargs):
        raise OSError("capability state unreadable")

    monkeypatch.setattr(defaults, "with_default_crew_servers", unreadable)
    spec = {"tools": ["fs_read"]}
    assert defaults.session_spec_on(ACP_BACKEND_KAS, "mine", spec) is spec


@pytest.fixture
def mirror_agents(tmp_path, monkeypatch, managed):
    """A hand-written user-level agent the mirrors' shared translation reads."""
    from kiro_crew.acp import session_mcp

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", _managed)
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def test_each_mirror_s_array_carries_an_added_server_only_where_session_spec_on_does(
    mirror_agents, edition, projects
):
    """The array a mirrored session gets and the spec a check judges agree per backend.

    So a mirror whose array starts adding a custom agent's servers fails here until
    :func:`session_spec_on` says so, instead of leaving the edition's doctor to tell a
    user to hand-edit an agent whose chats already have the tool.
    """
    from kiro_crew.acp_backends import selectable_backend_values
    from kiro_crew.providers.mirrors import has_mirror, mirror_for

    projects(True)
    spec = {"name": "mine", "tools": ["fs_read"]}
    (mirror_agents / "mine.json").write_text(json.dumps(spec), encoding="utf-8")
    added = {"kirocrew-core", "kirocrew-cron", "edition-vend"}
    mirrored = [b for b in selectable_backend_values() if has_mirror(b)]
    assert mirrored, "the selectable set lists mirrored harnesses"
    disagree = {}
    for backend in mirrored:
        params = mirror_for(backend).session_params(
            "mine", permission_surface_owned=True, session_key="dashboard:chat-1"
        )
        wire = {e.get("name") for e in params.get("mcpServers") or []} & added
        judged = set(defaults.session_spec_on(backend, "mine", spec).get("mcpServers") or {})
        if wire != judged & added:
            disagree[backend] = (sorted(wire), sorted(judged & added))
    assert disagree == {}


# --- one pass over many agents -----------------------------------------------------


def test_one_pass_reads_the_edition_s_servers_once_for_every_agent(managed, edition):
    tooling = _EditionTooling()
    edition(tooling)
    with defaults.one_pass():
        for name in ("mine", "yours", "theirs"):
            out = defaults.with_default_crew_servers(name, {"tools": ["fs_read"]})
            assert out["mcpServers"]["edition-vend"] == _EDITION_ENTRY
    assert tooling.server_reads == 1
    defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
    assert tooling.server_reads == 2, "outside a pass every call reads afresh"


def test_one_pass_hands_each_agent_its_own_entry(managed, edition):
    with defaults.one_pass():
        first = defaults.with_default_crew_servers("mine", {"tools": ["fs_read"]})
        first["mcpServers"]["edition-vend"]["args"].append("edited")
        second = defaults.with_default_crew_servers("yours", {"tools": ["fs_read"]})
    assert second["mcpServers"]["edition-vend"] == _EDITION_ENTRY


def test_one_pass_asks_the_skill_view_admission_once(managed, edition, projects):
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO

    asked = projects(True)
    with defaults.one_pass():
        with defaults.one_pass():
            defaults.session_spec_on(ACP_BACKEND_KIRO, "mine", {"tools": ["fs_read"]})
        defaults.session_spec_on(ACP_BACKEND_KIRO, "yours", {"tools": ["fs_read"]})
    assert asked == [True], "a nested pass shares the outer one's reads"


# --- KAS: the projected custom agent --------------------------------------------


@pytest.fixture
def kas_layout(monkeypatch, tmp_path, managed):
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user, checkout


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _kas_entry(agent: str, checkout: Path, session_key: str = "") -> dict:
    extras = await KasHarness().session_extras(
        agent, work_dir=str(checkout), session_key=session_key
    )
    [entry] = extras.custom_agents
    return entry


@pytest.mark.asyncio
async def test_kas_projects_the_default_servers_with_this_session_s_key(kas_layout):
    user, checkout = kas_layout
    _write(user, "mine", {"name": "mine", "prompt": "p", "tools": ["@kept"]})

    entry = await _kas_entry("mine", checkout, session_key="chat-abc")

    assert entry["tools"] == ["@kept", "@kirocrew-core", "@kirocrew-cron"]
    for server in ("kirocrew-core", "kirocrew-cron"):
        projected = entry["mcpServers"][server]
        assert projected["args"] == _managed(server)["args"]
        # The session's own key: the server resolves the caller, and its memory
        # store, from this session and nothing the spec chose.
        assert projected["env"]["KIROCREW_SESSION_KEY"] == "chat-abc"
    assert not (user / "mine.json").read_text(encoding="utf-8").count("kirocrew-core")


@pytest.mark.asyncio
async def test_kas_projects_the_edition_server_without_writing_the_file(kas_layout, edition):
    user, checkout = kas_layout
    _write(user, "mine", {"name": "mine", "prompt": "p", "tools": ["@kept"]})

    entry = await _kas_entry("mine", checkout, session_key="chat-abc")

    assert "@edition-vend" in entry["tools"]
    assert entry["mcpServers"]["edition-vend"]["command"] == _EDITION_ENTRY["command"]
    assert "edition-vend" not in (user / "mine.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_kas_honours_the_opt_out(kas_layout):
    user, checkout = kas_layout
    _write(
        user,
        "mine",
        {
            "name": "mine",
            "prompt": "p",
            "tools": ["@kept"],
            "managedToolPolicy": {"excludeServers": ["kirocrew-core", "kirocrew-cron"]},
        },
    )

    entry = await _kas_entry("mine", checkout)

    assert entry["tools"] == ["@kept"]
    assert "kirocrew-core" not in (entry.get("mcpServers") or {})


#: The built-ins a ``tools: ["fs_read", "execute_bash"]`` spec mounts on KAS.
_READ_AND_SHELL = [
    "fs_read",
    "read_file",
    "list_directory",
    "execute_bash",
    "control_bash_process",
    "control_pwsh_process",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "narrowing, excluded",
    [
        pytest.param({"excludedTools": ["@*"]}, ["@mcp", "@powers"], id="every-mcp-server"),
        pytest.param(
            {"excludedTools": ["@*"], "resources": ["skill://skills/*/SKILL.md"]},
            ["@mcp", "@powers"],
            id="every-mcp-server-with-skills",
        ),
        pytest.param(
            {
                "permissions": {
                    "rules": [
                        {"capability": "fs_read", "effect": "allow"},
                        {"capability": "mcp", "effect": "deny"},
                    ]
                }
            },
            None,
            id="permissions-deny-mcp",
        ),
        pytest.param(
            {"allowedTools": ["fs_read"], "excludedTools": ["@*", "execute_bash"]},
            ["@mcp", "@powers", "execute_bash", "control_bash_process", "control_pwsh_process"],
            id="mixed-allowed-and-excluded",
        ),
    ],
)
async def test_kas_keeps_a_narrow_agent_narrow(kas_layout, narrowing, excluded):
    """A hand-written read+shell agent that withholds MCP registers on KAS with
    exactly the built-ins its ``tools`` names, none of Crew's servers, and its
    ``@*`` as KAS's MCP tags rather than KAS's every-tool wildcard."""
    user, checkout = kas_layout
    spec = {"name": "narrow", "prompt": "p", "tools": ["fs_read", "execute_bash"], **narrowing}
    _write(user, "narrow", spec)

    entry = await _kas_entry("narrow", checkout, session_key="chat-abc")

    assert entry["id"] == "narrow"
    assert entry["tools"] == _READ_AND_SHELL
    assert not entry.get("mcpServers")
    assert entry.get("excludedTools") == excluded
    if "permissions" in narrowing:
        assert {"capability": "mcp", "effect": "deny"} in entry["permissions"]["rules"]


@pytest.mark.asyncio
async def test_kas_leaves_a_project_only_agent_alone(kas_layout):
    _user, checkout = kas_layout
    _write(checkout / ".kiro" / "agents", "proj", {"name": "proj", "tools": ["fs_read"]})

    entry = await _kas_entry("proj", checkout)

    assert "@kirocrew-core" not in entry["tools"]
    assert "kirocrew-core" not in (entry.get("mcpServers") or {})


def test_kas_layers_the_checkout_s_switch_offs_on_an_added_app_server(kas_layout, monkeypatch):
    user, checkout = kas_layout
    _install_app(monkeypatch)
    _write(user, "mine", {"name": "mine", "prompt": "p", "tools": ["fs_read", "tool_search"]})
    switch_off = {"disabled": True, "disabledTools": ["x"]}
    _write(
        checkout / ".kiro" / "agents",
        "mine",
        {"name": "mine", "mcpServers": {_APP_KEY: switch_off}},
    )

    spec, _anchor = resolve_projected_spec(user, "mine", checkout)

    assert spec["mcpServers"][_APP_KEY] == {**_APP_ENTRY, **switch_off}


# --- kiro-cli: the skill view and its per-session control plane --------------------


def test_kiro_cli_view_and_control_plane_mount_both_servers(native_tree, managed):
    from kiro_crew.acp import skill_projection as projection
    from kiro_crew.acp.session_mcp import kiro_control_plane_servers

    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps({"name": "custom", "tools": ["read"]}), encoding="utf-8"
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.specs["custom"]
    assert view["tools"] == ["read", "@kirocrew-core", "@kirocrew-cron"]
    assert set(view["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
    assert json.loads((agents / "custom.json").read_text(encoding="utf-8")) == {
        "name": "custom",
        "tools": ["read"],
    }, "the authored spec is never rewritten"

    mount = kiro_control_plane_servers(
        prepared.agent("custom"), work_dir=project, spec_override=view
    )
    assert {element["name"] for element in mount.elements} >= {"kirocrew-core", "kirocrew-cron"}


def test_kiro_cli_projection_survives_unopenable_capability_state(
    native_tree, managed, unopenable_state, monkeypatch
):
    from kiro_crew.acp import skill_projection as projection

    _home, agents, project = native_tree
    for name in ("custom", "kirocrew"):
        (agents / f"{name}.json").write_text(
            json.dumps({"name": name, "tools": ["read"]}), encoding="utf-8"
        )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("custom", "kirocrew")
        ],
    )
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert prepared.specs["custom"]["tools"] == ["read"]
    assert "mcpServers" not in prepared.specs["custom"]


def test_kiro_cli_views_read_the_edition_s_servers_once_per_preparation(
    native_tree, managed, edition, monkeypatch
):
    """Every custom agent's view gets the edition server from one read of the host."""
    from kiro_crew.acp import skill_projection as projection

    tooling = _EditionTooling()
    edition(tooling)
    _home, agents, project = native_tree
    names = ("custom", "helper")
    for name in names:
        (agents / f"{name}.json").write_text(
            json.dumps({"name": name, "tools": ["read"]}), encoding="utf-8"
        )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global") for name in names
        ],
    )
    prepared = projection.prepare_native_skill_projection(project)
    assert all("edition-vend" in prepared.specs[name]["mcpServers"] for name in names)
    assert tooling.server_reads == 1


def test_kiro_cli_view_honours_the_opt_out(native_tree, managed):
    from kiro_crew.acp import skill_projection as projection

    _home, agents, project = native_tree
    spec = {"name": "custom", "tools": ["read"], "excludedTools": ["@kirocrew-cron"]}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.specs["custom"]
    assert view["tools"] == ["read", "@kirocrew-core"]
    assert set(view["mcpServers"]) == {"kirocrew-core"}


# --- Approval grants on the derived copy -----------------------------------------

_APPROVALS = [
    "*",
    "@other",
    "@*",
    "@kirocrew-*",
    "@kirocrew-cron",
    "@kirocrew-cron/*",
    "@kirocrew-cron/cron_*",
    "@kirocrew-*/cron_*",
    "web_search",
    "@kirocrew-core",
    "@other/read",
]


@pytest.fixture
def per_tool_ceiling(monkeypatch):
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
    return ceiling


@pytest.fixture(params=["helper", "kiro-cli"])
def derive(request, native_tree, managed):
    from kiro_crew.acp import skill_projection as projection

    _home, agents, project = native_tree

    def project_spec(spec):
        original = json.dumps(spec)
        if request.param == "helper":
            out = defaults.with_default_crew_servers("custom", spec)
        else:
            authored = agents / "custom.json"
            authored.write_text(original, encoding="utf-8")
            prepared = projection.prepare_native_skill_projection(project)
            out = prepared.specs["custom"]
            emitted = agents / f"{prepared.agent('custom')}.json"
            assert json.loads(emitted.read_text(encoding="utf-8")) == out
            assert authored.read_text(encoding="utf-8") == original
        assert json.dumps(spec) == original
        return out

    return project_spec


def test_single_tool_deny_drops_covering_approvals(derive, per_tool_ceiling):
    from kiro_crew.platform import governance

    assert not governance.gate_decision(
        per_tool_ceiling, None, "mcp__kirocrew-cron__cron_remove_all"
    ).permitted
    assert governance.gate_decision(
        per_tool_ceiling, None, "mcp__kirocrew-cron__cron_add"
    ).permitted
    assert governance.may_skip_gate_now("@kirocrew-cron") is False
    assert governance.may_skip_gate_now("*") is True
    assert governance.may_skip_gate_now("@kirocrew-*") is True

    out = derive({"name": "custom", "tools": ["read"], "allowedTools": _APPROVALS})

    assert out["allowedTools"] == ["@other", "web_search", "@kirocrew-core", "@other/read"]
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}


@pytest.fixture
def audit_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        "kiro_crew.acp.kas_agents.sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    return events


def test_single_tool_deny_audits_withheld_approvals(derive, per_tool_ceiling, audit_events):
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": _APPROVALS})
    assert out["allowedTools"] == ["@other", "web_search", "@kirocrew-core", "@other/read"]
    withheld = ", ".join(
        sorted(
            [
                "*",
                "@*",
                "@kirocrew-*",
                "@kirocrew-cron",
                "@kirocrew-cron/*",
                "@kirocrew-cron/cron_*",
                "@kirocrew-*/cron_*",
            ]
        )
    )
    assert audit_events == [
        {
            "caller": "system",
            "operation": "mcp_auto_approve_withheld",
            "outcome": "ok",
            "source": "kas_agent_projection",
            "resources": (
                f"{withheld} projected without auto-approve (governance ceiling) "
                "for agent custom; calls go through the approval gate"
            ),
        }
    ]


def test_ungoverned_keeps_approvals(derive, per_tool_ceiling, monkeypatch, audit_events):
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: None)
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": _APPROVALS})
    assert out["allowedTools"] == _APPROVALS
    assert audit_events == []


def test_audit_failure_keeps_approvals_withheld(derive, per_tool_ceiling, monkeypatch):
    def unavailable(**kwargs):
        raise OSError("audit sink unavailable")

    monkeypatch.setattr(
        "kiro_crew.acp.kas_agents.sel",
        lambda: SimpleNamespace(log_api_access=unavailable),
    )
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": _APPROVALS})
    assert out["allowedTools"] == ["@other", "web_search", "@kirocrew-core", "@other/read"]


def test_unreadable_ceiling_withholds_approvals(derive, per_tool_ceiling, monkeypatch):
    def unreadable():
        raise RuntimeError("ceiling unavailable")

    monkeypatch.setattr("kiro_crew.platform.context.current_context", unreadable)
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": _APPROVALS})
    assert out["allowedTools"] == ["@other", "web_search", "@other/read"]
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}


def test_only_added_servers_narrow_covering_approvals(derive, per_tool_ceiling):
    spec = {
        "name": "custom",
        "tools": ["read"],
        "allowedTools": ["*", "@kirocrew-*", "@other"],
        "excludedTools": ["@kirocrew-cron"],
    }
    out = derive(spec)
    assert out["allowedTools"] == spec["allowedTools"]
    assert set(out["mcpServers"]) == {"kirocrew-core"}


def test_entries_not_reaching_an_added_server_are_kept_as_written(derive, per_tool_ceiling):
    allowed = ["@other", "fs_read", None, 42, "execute_bash", "web_search"]
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": allowed})
    assert out["allowedTools"] == allowed


_AUTHOR_BUILTINS = ["fs_read", "execute_bash", "@mine"]


def test_ungoverned_host_keeps_author_builtin_approvals(
    derive, per_tool_ceiling, monkeypatch, audit_events
):
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: None)
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": _AUTHOR_BUILTINS})
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
    assert out["allowedTools"] == _AUTHOR_BUILTINS
    assert audit_events == []


def test_single_tool_deny_keeps_author_builtin_approvals(derive, per_tool_ceiling, audit_events):
    allowed = ["*", "fs_read", "@kirocrew-*", "execute_bash", "@mine"]
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": allowed})
    assert set(out["mcpServers"]) == {"kirocrew-core", "kirocrew-cron"}
    assert out["allowedTools"] == _AUTHOR_BUILTINS
    assert len(audit_events) == 1
    assert audit_events[0]["resources"].startswith("*, @kirocrew-* projected without")


@pytest.mark.parametrize("allowed", [None, "*", {}, 42])
def test_non_list_approvals_are_untouched(derive, per_tool_ceiling, allowed):
    out = derive({"name": "custom", "tools": ["read"], "allowedTools": allowed})
    assert out["allowedTools"] == allowed


@pytest.mark.parametrize(
    "changes",
    [
        {"tools": []},
        {"mcpServers": {"kirocrew-core": _CORE, "kirocrew-cron": _CRON}},
        {"excludedTools": ["@kirocrew-core", "@kirocrew-cron"]},
    ],
)
def test_no_added_servers_preserves_approvals(managed, per_tool_ceiling, changes):
    spec = {"tools": ["read"], "allowedTools": list(_APPROVALS), **changes}
    assert defaults.with_default_crew_servers("custom", spec) is spec
    assert spec["allowedTools"] == _APPROVALS


class TestCrewAddedServers:
    """Which of Crew's default servers Crew itself put on the spec a session runs."""

    def test_the_servers_the_helper_added_are_named(self, managed):
        out = defaults.with_default_crew_servers("mine", {"name": "mine", "tools": ["fs_read"]})
        assert defaults.crew_added_servers("mine", out) == ("kirocrew-core", "kirocrew-cron")

    def test_an_entry_the_author_wrote_is_theirs(self, managed):
        """A declared server is skipped by the helper and is not counted here."""
        spec = {
            "name": "mine",
            "tools": ["fs_read", "@kirocrew-core"],
            "mcpServers": {"kirocrew-core": {**_CORE, "disabledTools": ["spawn_run"]}},
        }
        out = defaults.with_default_crew_servers("mine", spec)
        assert defaults.crew_added_servers("mine", out) == ("kirocrew-cron",)

    def test_an_agent_crew_wrote_has_none(self, managed):
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": dict(_CORE)}}
        assert defaults.crew_added_servers("kirocrew", spec) == ()

    @pytest.mark.parametrize("spec", [None, {"tools": ["fs_read"]}, {"mcpServers": []}])
    def test_no_servers_means_none(self, managed, spec):
        assert defaults.crew_added_servers("mine", spec) == ()

    def test_a_tool_less_agent_gets_nothing_to_defer(self, managed):
        """The zero-tool rule holds: the helper adds no server, so nothing is counted."""
        out = defaults.with_default_crew_servers("mine", {"name": "mine", "tools": []})
        assert "mcpServers" not in out
        assert defaults.crew_added_servers("mine", out) == ()

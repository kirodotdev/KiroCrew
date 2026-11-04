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
from kiro_crew.acp.harness.kas import KasHarness
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
        ],
    )
    def test_an_agent_that_excludes_the_servers_gets_neither(self, managed, opt_out):
        spec = {"tools": ["fs_read"], **opt_out}
        assert defaults.with_default_crew_servers("mine", spec) is spec

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


@pytest.mark.asyncio
async def test_kas_leaves_a_project_only_agent_alone(kas_layout):
    _user, checkout = kas_layout
    _write(checkout / ".kiro" / "agents", "proj", {"name": "proj", "tools": ["fs_read"]})

    entry = await _kas_entry("proj", checkout)

    assert "@kirocrew-core" not in entry["tools"]
    assert "kirocrew-core" not in (entry.get("mcpServers") or {})


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

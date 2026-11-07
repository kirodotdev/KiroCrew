"""A KAS chat that picks kiro-cli's built-in ``kiro_default`` switches to Crew's default agent.

KAS has no definition of ``kiro_default``, and KAS's own closest mode decides some
tool calls without asking Crew. So the pick itself
(``session_agent_selection.kas_pick_stand_in``, called by the agent switch) selects
Crew's default template instead, on KAS only, before the choice is recorded. The
record's selection and template then both name that template, so every reader of the
record sees it: the spawn allowlist, its spec hooks, its KAS projection, and the
governance ceiling the permission floor applies to the agent the session runs as.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.session_handle import AcpAgentUnavailable
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.manager import InstalledApp, _write_installed
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod
from kiro_crew.session_agent_selection import kas_pick_stand_in

_HOOKS = {"preToolUse": [{"matcher": "web_fetch", "command": "guard.sh"}]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """An isolated data home whose agents directory holds Crew's default agent only."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent, **_: frozenset()
    )
    _write_spec(agents, "kirocrew", hooks=_HOOKS)
    spec_hooks._cache.clear()
    yield agents
    spec_hooks._cache.clear()


def _write_spec(agents, name: str, **fields) -> None:
    spec = {"name": name, "prompt": "p", "tools": ["fs_read", "web_fetch"], **fields}
    (agents / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _pick(agent: str, backend: str = ACP_BACKEND_KAS, default_agent: str = "", project=None):
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = backend
    cfg.agent.member_acp_backend = backend
    cfg.agent.default_agent = default_agent
    return kas_pick_stand_in(cfg, "chat-1", agent, project)


def test_kiro_default_on_kas_runs_crews_default_agent(agents_dir):
    assert _pick("kiro_default") == "kirocrew"


def test_kiro_default_on_kas_runs_the_configured_default_agent(agents_dir):
    _write_spec(agents_dir, "mine")

    assert _pick("kiro_default", default_agent="mine") == "mine"
    # A default that is itself a built-in is no stand-in: the managed one is.
    assert _pick("kiro_default", default_agent="kiro_default") == "kirocrew"


def test_kiro_default_on_kiro_cli_stays_kiro_clis_own(agents_dir):
    """kiro-cli runs its built-in from its own binary, so nothing is mapped there."""
    assert _pick("kiro_default", backend="") == "kiro_default"


@pytest.mark.parametrize("builtin", ["kiro_planner", "kiro_guide"])
def test_built_ins_with_no_crew_match_are_not_mapped(agents_dir, builtin):
    """No Crew agent does their job, so KAS keeps refusing them with a pick."""
    assert _pick(builtin) == builtin


def test_a_crewmate_named_kiro_default_keeps_its_name(agents_dir):
    """The resolver's member-first lookup owns a crewmate of the name."""
    from kiro_crew.config.loader import KiroCrewAgentConfig

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = cfg.agent.member_acp_backend = ACP_BACKEND_KAS
    cfg.agents["kiro_default"] = KiroCrewAgentConfig(kiro_agent="mine")

    assert kas_pick_stand_in(cfg, "chat-1", "kiro_default", None) == "kiro_default"


def test_a_users_own_kiro_default_spec_runs_as_itself(agents_dir):
    _write_spec(agents_dir, "kiro_default")

    assert _pick("kiro_default") == "kiro_default"


def test_a_project_kiro_default_spec_runs_as_itself(agents_dir, tmp_path):
    project = tmp_path / "work"
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(project / ".kiro" / "agents", "kiro_default")

    assert _pick("kiro_default", project=str(project)) == "kiro_default"


@pytest.mark.parametrize("enabled", [True, False])
def test_an_app_that_declares_kiro_default_keeps_its_own_refusal(agents_dir, enabled):
    root = manager_mod.app_dir("demo-app")
    (root / "agents").mkdir(parents=True)
    (root / manager_mod.APP_MANIFEST_FILENAME).write_text(
        json.dumps({"name": "demo-app", "version": "1.0.0", "agents": ["agents/w.json"]}),
        encoding="utf-8",
    )
    (root / "agents" / "w.json").write_text(json.dumps({"name": "kiro_default"}), encoding="utf-8")
    _write_installed("demo-app", InstalledApp(name="demo-app", version="1.0.0", enabled=enabled))

    assert _pick("kiro_default") == "kiro_default"


def test_the_mapped_session_fires_the_default_agents_spec_hooks(agents_dir):
    """Hooks fire by the turn's agent name, which is now the default agent's."""
    agent = _pick("kiro_default")

    hooks, _lost, _unconfirmable = spec_hooks.crew_fired_spec_hooks(agent)

    assert [(h.matcher, h.command) for h in hooks] == [("web_fetch", "guard.sh")]
    # The unmapped name has no spec, so it would have fired no hooks at all.
    assert spec_hooks.crew_fired_spec_hooks("kiro_default") == ([], [], 0)


@pytest.mark.asyncio
async def test_the_mapped_session_projects_exactly_the_default_agent(agents_dir, tmp_path):
    """KAS gets the default agent's own spec, never a KAS mode, and nothing wider."""
    kas = harness_for(ACP_BACKEND_KAS)
    work = str(tmp_path / "work")

    mapped = await kas.session_extras(_pick("kiro_default"), work_dir=work)
    default = await kas.session_extras("kirocrew", work_dir=work)

    assert mapped.custom_agents == default.custom_agents
    with pytest.raises(AcpAgentUnavailable):
        await kas.session_extras("kiro_default", work_dir=work)


@pytest.fixture
def secret_commands_denied(tmp_path, monkeypatch):
    """An admin ceiling that denies ``*secret*`` commands; records governance audit rows."""
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "profiles")
    gp.reset_store()
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "commands": {"mode": "deny", "deny": ["*secret*"]},
        }
    )
    base = build_default_context(KiroCrewConfig.load())
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))
    rows: list[dict] = []
    sel_mock = MagicMock()
    sel_mock.log_governance_decision.side_effect = lambda **kw: rows.append(kw)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    yield rows
    ctx_mod.reset_context()
    gp.reset_store()


def test_admin_policy_applies_to_the_mapped_session(agents_dir, secret_commands_denied):
    """The permission floor judges the session as the agent it runs, under the ceiling."""
    from kiro_crew import permission_floor
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    agent = _pick("kiro_default")
    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=1,
        title="Running: echo my-secret-value",
        is_shell=True,
        tool_input=json.dumps({"command": "echo my-secret-value"}),
    )

    reason = permission_floor.refusal_for(
        event, session_key="chat-1", agent=agent, security_only=False
    )

    assert reason is not None and "governance" in reason.lower()
    assert [(r["outcome"], r["agent"]) for r in secret_commands_denied] == [("denied", "kirocrew")]


@pytest.mark.asyncio
@pytest.mark.parametrize("announce", [False, True])
async def test_the_pick_records_one_consistent_template(
    agents_dir, tmp_path, monkeypatch, announce
):
    """``/agent kiro_default`` on KAS records Crew's default as selection AND template.

    The spawn allowlist reads the record's template (``_parent_template_for_spawn``),
    so it reads the default agent's spec, never a name with no spec and no allowlist.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from chat_agent_selection_helpers import _turn_state
    from chat_test_helpers import _make_app_with_agent_routes
    from dashboard_owner_helpers import as_owner

    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.subagent_parts.agent_policy import _parent_template_for_spawn

    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = ACP_BACKEND_KAS
    cfg.agent.member_acp_backend = ACP_BACKEND_KAS
    cfg.save()
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("picker")
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        response = await client.post(
            "/api/chat/slots/picker/agent", json={"agent": "kiro_default", "announce": announce}
        )
        body = await response.json()
    assert response.status == 200, body
    assert body["agent"] == "kirocrew"
    assert "kiro_default runs as Kiro Crew's default agent (kirocrew)" in body["warning"]
    assert slot.agent == "kirocrew"
    record = read_session_execution("dashboard:picker")
    assert (record.selection_name, record.template_id) == ("kirocrew", "kirocrew")
    assert _parent_template_for_spawn("dashboard:picker") == "kirocrew"
    if announce:
        assert "kiro_default runs as Kiro Crew's default agent" in slot.messages[-1]["content"]

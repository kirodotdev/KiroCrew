# SPDX-License-Identifier: Apache-2.0
"""``kirocrew-mate-conductor`` is the default template, renamed, minus sub-agents.

A crewmate's own chat runs on a source-defined spec whose prompt, tools and
``mcpServers`` are ``build_agent_config()``'s output, exactly as ``kirocrew.json``
carries them. It differs in ``name``, ``description`` and ``excludedTools``: the
conductor hands its work to a worker session, so the sub-agent tools are not in
its tool list, and the spawn gate refuses a spawn from it.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import service_agents
from kiro_crew.config import paths as paths_mod


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    """The spec this installer actually writes, read back off disk."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    return json.loads(
        (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
    )


class TestTheInstallerWritesTheDefaultTemplateUnderItsOwnName:
    def test_the_file_carries_this_agents_name_and_description(self, installed):
        assert installed["name"] == "kirocrew-mate-conductor"
        assert installed["description"] == "The agent a Bot's own chat runs on."

    def test_the_filename_is_owned_so_the_boot_sweep_covers_it(self):
        assert MATE_CONDUCTOR_AGENT_FILENAME == "kirocrew-mate-conductor.json"
        assert MATE_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_beyond_name_and_description_it_is_the_default_templates_shape(
        self, installed, monkeypatch, tmp_path
    ):
        """Every other field matches ``build_agent_config()`` exactly, except the
        work-ledger mount (:class:`TestItMountsTheWorkLedger`), which only adds."""
        from kiro_crew.agent_materialization.skill_resources import (
            WIDGET_POINTER_SKILLS,
            prompt_skill_uris,
        )

        template = agent.build_agent_config()
        mounted = ("tools", "allowedTools", "mcpServers", "resources")
        for key, value in template.items():
            if key in ("name", "description", "excludedTools", *mounted):
                continue
            assert installed.get(key) == value, key
        # The mounted ledger maps the skills its prompt names onto the template's own.
        mapped = {*prompt_skill_uris("goal-conductor", "dashboard", *WIDGET_POINTER_SKILLS)}
        assert [r for r in installed["resources"] if r not in mapped] == template.get("resources")
        assert {k: v for k, v in installed["mcpServers"].items() if k != "kirocrew-work"} == (
            template["mcpServers"]
        )
        assert [t for t in installed["tools"] if t != "@kirocrew-work"] == template["tools"]
        assert [
            t for t in installed["allowedTools"] if not t.startswith("@kirocrew-work/")
        ] == template["allowedTools"]

    def test_it_keeps_the_default_templates_built_in_tools(self, installed):
        """Only sub-agent spawning is taken away: the shell, the file writer and
        ``code`` are the default template's and stay mounted."""
        for tool in ("execute_bash", "fs_write", "code"):
            assert tool in installed["tools"]
            assert tool not in installed.get("excludedTools", [])
        assert "fs_read" in installed["tools"]
        assert "@kirocrew-mate" in installed["tools"]

    def test_it_still_lacks_the_spawn_tools(self, installed):
        assert "@kirocrew-core/spawn_run" in installed["excludedTools"]
        assert "@kirocrew-core/spawn_run" not in installed.get("allowedTools", [])

    def test_task_run_and_workflow_run_are_not_in_its_tool_list(self, installed):
        excluded = installed["excludedTools"]
        assert "@kirocrew-core/task_run" in excluded
        assert "@kirocrew-core/workflow_run" in excluded
        # Computer use and the browser tool stay.
        assert "@kirocrew-computer" in installed["tools"]
        assert "@kirocrew-core/browser" not in excluded


def test_the_user_servers_never_reach_the_conductor(tmp_path, monkeypatch):
    """Whatever servers the default template carries for the user, the conductor
    mounts only the gateway's own platform servers."""
    real = agent.build_agent_config

    def with_user_servers():
        config = real()
        config["mcpServers"] = {**config.get("mcpServers", {}), "builder-mcp": {"command": "b"}}
        config["tools"] = [*config["tools"], "@builder-mcp"]
        config["allowedTools"] = [*config.get("allowedTools", []), "@builder-mcp/search"]
        config["includeMcpJson"] = True
        return config

    monkeypatch.setattr(agent, "build_agent_config", with_user_servers)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    spec = json.loads(
        (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
    )

    from kiro_crew.mate_worker import conductor_mcp_servers

    assert spec["includeMcpJson"] is False
    assert "builder-mcp" not in spec["mcpServers"]
    assert set(spec["mcpServers"]) <= conductor_mcp_servers()
    assert "kirocrew-mate" in spec["mcpServers"] and "kirocrew-core" in spec["mcpServers"]
    assert not any("builder-mcp" in ref for ref in [*spec["tools"], *spec["allowedTools"]])

    def test_the_sub_agent_tools_are_not_in_its_tool_list(self, installed):
        from kiro_crew.mate_worker import MATE_CONDUCTOR_EXCLUDED_TOOLS

        excluded = installed.get("excludedTools") or []
        for tool in ("spawn_run", "spawn_sub_agents", "spawn_continue", "spawn_steer"):
            assert f"@kirocrew-core/{tool}" in excluded
        assert set(MATE_CONDUCTOR_EXCLUDED_TOOLS) <= set(excluded)
        # The hand-off it does have stays mounted.
        assert not any("session_" in ref for ref in excluded)

    def test_the_prompt_is_the_same_native_stub_every_crewmate_runs_today(self, installed):
        assert installed["prompt"] == agent._NATIVE_PROMPT_STUB


def test_rebuild_agent_config_installs_it_alongside_the_default(tmp_path, monkeypatch):
    """A full ``rebuild_agent_config()`` writes the mate conductor too, not just a
    direct call to the installer -- this is the self-healing boot path a crewmate's
    turn actually depends on."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent.rebuild_agent_config(clean=True)
    assert (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).exists()


class TestIsKirocrewDefaultAgent:
    """``context_assembly``'s ``is_custom`` checks all defer to this predicate so
    the mate conductor is treated as the kirocrew default, not a user's custom
    agent -- see test_context.py's ``test_docs_still_injected_for_the_mate_conductor``
    for the behavioral proof."""

    def test_kirocrew_and_the_mate_conductor_are_both_the_default(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert is_kirocrew_default_agent("kirocrew")
        assert is_kirocrew_default_agent("kirocrew-mate-conductor")

    def test_a_real_custom_agent_is_not(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("code-reviewer")
        assert not is_kirocrew_default_agent("")
        assert not is_kirocrew_default_agent(None)

    def test_other_derived_specs_stay_out_of_scope(self):
        """Research/worker/team-lead are backend loops this fix does not touch;
        widening their classification is a separate decision."""
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("kirocrew-research")
        assert not is_kirocrew_default_agent("kirocrew-worker")
        assert not is_kirocrew_default_agent("kirocrew-team-lead")


def test_the_first_crewmates_implicit_fallback_names_the_mate_conductor():
    """``first_crewmate._ORDINARY_TEMPLATE`` is internal-only -- nothing displays
    its value, unlike ``change_card_catalog.DEFAULT_CREWMATE_TEMPLATE`` (the New
    crewmate dialog's "Built from" label and API payload), which deliberately
    stays "kirocrew": a UI label change is its own design decision, out of scope
    for step 1's pure sourcing move."""
    from kiro_crew import change_card_catalog
    from kiro_crew.agent_materialization import first_crewmate

    assert first_crewmate._ORDINARY_TEMPLATE == "kirocrew-mate-conductor"
    assert first_crewmate._default_template() == "kirocrew-mate-conductor"
    # The ``default`` binding a plain chat runs on is not a crewmate's.
    assert first_crewmate._PLAIN_TEMPLATE == "kirocrew"
    assert change_card_catalog.DEFAULT_CREWMATE_TEMPLATE == "kirocrew"


def test_the_spawn_gate_refuses_a_parent_on_the_conductor():
    from kiro_crew.subagent_parts.agent_policy import (
        _vet_parent_available_agents,
        conductor_spawn_refusal,
    )

    refusal = _vet_parent_available_agents(("kirocrew-mate-conductor", ()), "kirocrew")
    assert refusal is not None and "session_create" in refusal
    # Even with no child template resolved yet, and whatever the spec declares.
    assert _vet_parent_available_agents(("kirocrew-mate-conductor", ()), "") is not None
    assert conductor_spawn_refusal("kirocrew-mate-conductor") is not None
    # Every other parent keeps today's answer, the worker included.
    assert _vet_parent_available_agents(("kirocrew", ()), "kirocrew") is None
    assert _vet_parent_available_agents(("kirocrew-mate-worker", ()), "kirocrew") is None
    assert conductor_spawn_refusal("") is None


def test_the_member_working_protocol_hands_long_work_to_worker_sessions():
    from kiro_crew.context_assembly import member

    text = member._MEMBER_HOW_YOU_WORK_COMMON
    assert "session_create" in text and "end your turn" in text
    assert "[Worker report]" in text and "session_read_message" not in text
    assert "spawn_run" not in text


def test_the_operating_mode_block_dispatches_and_ends_instead_of_patrolling():
    from kiro_crew.context_assembly import member

    text = member.operating_mode_block("Atlas")
    assert "end your turn" in text and "[Worker report]" in text
    assert "monitor_start" not in text


# -- the work ledger: goal-conductor skill and the ledger wording follow the mount -- #

_RECORD = "@kirocrew-work/work_ledger_record"
_READ = "@kirocrew-work/work_ledger_read"


def _install_with_work(
    tmp_path, monkeypatch, *, withhold_record: bool = False, select: bool = True
) -> dict:
    """The installed conductor spec when only the template carries ``kirocrew-work``.

    The installer's own mount is stubbed out, so *select* and *withhold_record* decide
    what the spec holds and the mapping is read against exactly that."""
    from kiro_crew.agent_materialization import auto_approve, managed_mcp

    real = agent.build_agent_config

    def with_work():
        config = real()
        config["mcpServers"] = {
            **config.get("mcpServers", {}),
            "kirocrew-work": managed_mcp._managed_opt_in_entry("mcp-work"),
        }
        if select:
            config["tools"] = [*config["tools"], "@kirocrew-work"]
        config["allowedTools"] = [*config.get("allowedTools", []), _READ, _RECORD]
        return config

    monkeypatch.setattr(agent, "build_agent_config", with_work)
    monkeypatch.setattr(service_agents, "_mount_work_ledger", lambda config, grants: None)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    if withhold_record:
        real_may = auto_approve._may_auto_approve
        monkeypatch.setattr(
            auto_approve,
            "_may_auto_approve",
            lambda ref: ref != _RECORD and real_may(ref),
        )
    service_agents._install_mate_conductor_agent()
    return json.loads(
        (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
    )


def _install_without_ledger(tmp_path, monkeypatch) -> dict:
    """The installed conductor spec with the installer's ledger mount stubbed out."""
    monkeypatch.setattr(service_agents, "_mount_work_ledger", lambda config, grants: None)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    return json.loads(
        (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
    )


def _goal_conductor_uri() -> str:
    from kiro_crew.agent_materialization.skill_resources import prompt_skill_uris

    return prompt_skill_uris("goal-conductor")[0]


def test_without_the_ledger_the_conductor_maps_no_goal_conductor_skill(tmp_path, monkeypatch):
    from kiro_crew.member_mate_tools import spec_has_ledger_tools

    spec = _install_without_ledger(tmp_path, monkeypatch)
    assert "kirocrew-work" not in spec["mcpServers"]
    assert _goal_conductor_uri() not in (spec.get("resources") or [])
    assert not spec_has_ledger_tools(spec)


def test_the_installed_conductor_mounts_the_ledger_and_maps_goal_conductor(installed):
    from kiro_crew.member_mate_tools import spec_has_ledger_tools

    assert "kirocrew-work" in installed["mcpServers"]
    assert _goal_conductor_uri() in installed["resources"]
    assert spec_has_ledger_tools(installed)


def test_with_the_ledger_the_conductor_maps_goal_conductor_for_patrol_budget(tmp_path, monkeypatch):
    from kiro_crew.member_mate_tools import spec_has_ledger_tools

    spec = _install_with_work(tmp_path, monkeypatch)
    assert _goal_conductor_uri() in spec["resources"]
    # The dashboard skill the crewmate prompt names, and the skills its widget pointer
    # names, stay in reach once mapping scopes.
    from kiro_crew.agent_materialization.skill_resources import (
        WIDGET_POINTER_SKILLS,
        prompt_skill_uris,
    )

    for uri in prompt_skill_uris("dashboard", *WIDGET_POINTER_SKILLS):
        assert uri in spec["resources"]
    # The template's own resources keep their places.
    template = agent.build_agent_config()
    for entry in template.get("resources") or []:
        assert entry in spec["resources"]
    assert spec_has_ledger_tools(spec)


def test_a_ceiling_that_withholds_the_record_verb_reads_as_no_ledger(tmp_path, monkeypatch):
    from kiro_crew.member_mate_tools import spec_has_ledger_tools

    spec = _install_with_work(tmp_path, monkeypatch, withhold_record=True)
    assert "kirocrew-work" in spec["mcpServers"] and "@kirocrew-work" in spec["tools"]
    assert _RECORD not in spec["allowedTools"]
    assert not spec_has_ledger_tools(spec)
    # No ledger text, so no mapping: the spec keeps the skills it inherits.
    assert _goal_conductor_uri() not in (spec.get("resources") or [])


def test_a_server_entry_without_its_tools_selected_maps_no_skill(tmp_path, monkeypatch):
    spec = _install_with_work(tmp_path, monkeypatch, select=False)
    assert "kirocrew-work" in spec["mcpServers"]
    assert _goal_conductor_uri() not in (spec.get("resources") or [])


def test_a_crewmate_on_the_conductor_reads_the_installed_conductor_spec(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew import mate_worker, member_mate_tools

    binding = SimpleNamespace(kiro_agent="cove", workspace="")
    monkeypatch.setattr(
        mate_worker, "member_chat_agent", lambda _k: mate_worker.MATE_CONDUCTOR_AGENT
    )
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    assert member_mate_tools.binding_has_ledger_tools(binding) is True
    _install_without_ledger(tmp_path, monkeypatch)
    assert member_mate_tools.binding_has_ledger_tools(binding) is False
    _install_with_work(tmp_path, monkeypatch)
    assert member_mate_tools.binding_has_ledger_tools(binding) is True
    _install_with_work(tmp_path, monkeypatch, withhold_record=True)
    assert member_mate_tools.binding_has_ledger_tools(binding) is False


# -- this release: the mapping ADDS the user's skills; the flag flips to scoped ---- #


@pytest.fixture
def scoped(monkeypatch):
    """The behavior the next release ships: the mapping is the whole scope."""
    from kiro_crew.agent_discovery_parts import skill_scope

    monkeypatch.setattr(skill_scope, "MATE_CONDUCTOR_INHERITS_USER_SKILLS", False)


def test_by_default_the_mapped_conductor_still_inherits_the_users_skills(user_skills):
    """The one-release deprecation: the default keeps the user's trees beside the mapping."""
    from kiro_crew.agent_discovery_parts.skill_scope import mate_conductor_inherited_skills

    _spec, project, goal, user, workspace = user_skills
    scope = _scope_via("agent_skill_globs_inheriting", project)
    assert {goal, user, workspace} <= set(scope)
    assert mate_conductor_inherited_skills(project) == ["my-own", "team-skill"]


def test_scoped_the_conductor_reports_no_inherited_skill(user_skills, scoped):
    from kiro_crew.agent_discovery_parts.skill_scope import mate_conductor_inherited_skills

    assert mate_conductor_inherited_skills(user_skills[1]) == []


# -- scoped: the mapping is the whole scope, for every caller, as on Ultra Mode ---- #


@pytest.fixture
def user_skills(tmp_path, monkeypatch):
    """A ledger-mounted conductor beside a user ``~/.kiro/skills`` skill and a trusted
    workspace ``.kiro/skills`` skill. Returns ``(spec, project, goal, user, workspace)``:
    ``goal`` is the mapped glob each caller keeps, ``user`` and ``workspace`` the
    inherited trees no caller may add back."""
    from kiro_crew import skill_trust
    from kiro_crew.agent_discovery_parts.skill_scope import expand_skill_uri

    home = tmp_path / "kiro-home"
    (home / "skills" / "my-own").mkdir(parents=True)
    (home / "skills" / "my-own" / "SKILL.md").write_text("---\nname: my-own\n---\n")
    project = tmp_path / "project"
    (project / ".kiro" / "skills" / "team-skill").mkdir(parents=True)
    (project / ".kiro" / "skills" / "team-skill" / "SKILL.md").write_text(
        "---\nname: team-skill\n---\n"
    )
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: home)
    monkeypatch.setattr(skill_trust, "project_skills_enabled", lambda: True)
    monkeypatch.setattr(skill_trust, "is_project_trusted", lambda p: str(p) == str(project))
    if not skill_trust.project_skill_traversal_supported():
        pytest.skip("no-follow project traversal is unsupported on this platform")
    spec = _install_with_work(tmp_path, monkeypatch)
    path = paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME
    goal = expand_skill_uri(_goal_conductor_uri(), path, project_dir=project)
    user = str(home / "skills" / "*" / "SKILL.md")
    workspace = str(project / ".kiro" / "skills" / "*" / "SKILL.md")
    return spec, project, goal, user, workspace


def _scope_via(caller: str, project) -> list[str]:
    """The conductor's scope as each production caller asks for it."""
    from kiro_crew import agent_discovery, context
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT as conductor

    if caller == "turn_message_triggered_filter":
        # context_assembly/turn_message.py: no inherit_defaults.
        return context.agent_skill_globs(conductor, project_dir=project)
    if caller == "agent_skill_globs_inheriting":
        return agent_discovery.agent_skill_globs(
            conductor, project_dir=project, strict=True, inherit_defaults=True
        )
    if caller in ("session_skill_globs_kiro_cli", "session_skill_globs_kas"):
        provider = context.PROVIDER_ACP if caller.endswith("kiro_cli") else None
        scope = agent_discovery.session_skill_globs(
            "", conductor, project_dir=project, provider_type=provider
        )
        assert scope is not None
        return scope
    if caller in ("injection_plan_kiro_cli", "injection_plan_kas"):
        provider = context.PROVIDER_ACP if caller.endswith("kiro_cli") else None
        _ok, globs = context._skills_injection_plan(
            conductor, is_cc=False, project_dir=project, provider_type=provider
        )
        return globs
    raise AssertionError(caller)


@pytest.mark.parametrize(
    "caller",
    [
        "turn_message_triggered_filter",
        "agent_skill_globs_inheriting",
        "session_skill_globs_kiro_cli",
        "session_skill_globs_kas",
        "injection_plan_kiro_cli",
        "injection_plan_kas",
    ],
)
def test_every_caller_scopes_the_conductor_to_its_mapped_skills(user_skills, scoped, caller):
    _spec, project, goal, user, workspace = user_skills
    scope = _scope_via(caller, project)
    assert goal in scope
    assert user not in scope
    assert workspace not in scope


def test_kas_deny_rule_denies_the_users_skills_to_the_mapped_conductor(user_skills, scoped):
    """KAS loads both trees itself; Crew's ``deny skill`` rule must not spare them."""
    from kiro_crew.acp import kas_skill_scope
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    spec, project, goal, user, workspace = user_skills
    globs = kas_skill_scope.scope_globs(
        MATE_CONDUCTOR_AGENT, spec, agents_dir=paths_mod.mate_agents_dir(), work_dir=project
    )
    assert globs is not None and goal in globs
    assert user not in globs and workspace not in globs
    rules = kas_skill_scope.skill_scope_rules(globs, work_dir=project)
    assert rules, "a mapped scope must deny the skills outside it"
    kept = {name for rule in rules for name in rule.get("exclude", [])}
    assert not {"my-own", "team-skill"} & kept


def _notice_rows(monkeypatch, project, agent: str) -> tuple[list, list]:
    """``(notice rows, pending-context entries)`` the helper writes for *agent*."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_runner

    rows: list = []
    queued: list = []
    slot = SimpleNamespace(append_pending_context=lambda e: queued.append(e) or True)
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    asyncio.run(
        chat_runner._notice_inherited_conductor_skills(
            object(), slot, SimpleNamespace(cwd=str(project)), agent
        )
    )
    return rows, queued


def test_a_session_that_loads_an_inherited_skill_is_told_and_one_that_loads_none_is_not(
    user_skills, monkeypatch
):
    """Per new session: the Bot is asked to say it in its reply (its chat draws no
    ``notice`` row; the row is for the Sessions page). A session whose conductor
    loads no inherited skill gets neither."""
    from kiro_crew.agent_discovery_parts import skill_scope
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    rows, queued = _notice_rows(monkeypatch, user_skills[1], MATE_CONDUCTOR_AGENT)
    assert len(rows) == 1 and len(queued) == 1
    for text in (rows[0][3], queued[0]["content"]):
        assert "my-own" in text and "team-skill" in text
        assert "Abilities" in text and "Bot" in text
    assert queued[0]["ephemeral"] is True

    monkeypatch.setattr(skill_scope, "MATE_CONDUCTOR_INHERITS_USER_SKILLS", False)
    assert _notice_rows(monkeypatch, user_skills[1], MATE_CONDUCTOR_AGENT) == ([], [])

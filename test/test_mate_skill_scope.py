"""Crewmate conductor and worker skill scopes resolve from the crewmate folder."""

from __future__ import annotations

import fnmatch
import json

import pytest

from kiro_crew import agent_discovery, mate_worker
from kiro_crew.config import paths
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef


@pytest.fixture
def mates(tmp_path, monkeypatch):
    folder = tmp_path / "mates" / "agents"
    folder.mkdir(parents=True)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: folder)
    skills = tmp_path / "skills"
    for name in ("triage", "release"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return folder, skills


def _spec(folder, name, resources, **extra):
    spec = {"name": name, "prompt": "p", "tools": ["@kirocrew-core"], "resources": resources}
    spec["mcpServers"] = {"kirocrew-core": {"command": "kirocrew"}}
    spec.update(extra)
    (folder / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _found(globs, path) -> bool:
    return any(fnmatch.fnmatch(str(path), g) for g in globs)


def test_a_crewmate_conductor_session_can_search_its_skills(mates):
    folder, skills = mates
    _spec(folder, mate_worker.MATE_CONDUCTOR_AGENT, [f"skill://{skills}/triage/SKILL.md"])

    globs = agent_discovery.session_skill_globs("", mate_worker.MATE_CONDUCTOR_AGENT)

    assert _found(globs, skills / "triage" / "SKILL.md")


def test_a_crewmate_worker_searches_its_base_and_its_crewmates_skills(mates, monkeypatch):
    folder, skills = mates
    _spec(folder, mate_worker.MATE_WORKER_AGENT, [f"skill://{skills}/triage/SKILL.md"])
    _spec(folder, "pixel", [f"skill://{skills}/release/SKILL.md"], mcpServers={})
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_: ExecutionContext(
            "pixel",
            MemoryStoreRef("member-pixel", "pixel"),
            "template",
            mate_worker.MATE_WORKER_AGENT,
            "persistent",
        ),
    )

    globs = agent_discovery.session_skill_globs("chat-1", mate_worker.MATE_WORKER_AGENT)

    assert _found(globs, skills / "triage" / "SKILL.md")
    assert _found(globs, skills / "release" / "SKILL.md")


def test_a_crewmate_workers_context_skill_plan_reaches_its_crewmates_skills(mates, monkeypatch):
    """The session-start and post-compaction plan names the worker's session, so
    the crewmate's own skill is in its context, not only in skill_search."""
    from kiro_crew import context

    folder, skills = mates
    _spec(folder, mate_worker.MATE_WORKER_AGENT, [f"skill://{skills}/triage/SKILL.md"])
    _spec(folder, "pixel", [f"skill://{skills}/release/SKILL.md"], mcpServers={})
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_: (
            ExecutionContext(
                "pixel",
                MemoryStoreRef("member-pixel", "pixel"),
                "template",
                mate_worker.MATE_WORKER_AGENT,
                "persistent",
            )
            if key == "chat-1"
            else None
        ),
    )

    inject, globs = context._skills_injection_plan(
        mate_worker.MATE_WORKER_AGENT, is_cc=False, session_key="chat-1"
    )

    assert inject
    assert _found(globs, skills / "release" / "SKILL.md")
    assert _found(globs, skills / "triage" / "SKILL.md")


def test_a_missing_crewmate_service_spec_still_refuses(mates):
    with pytest.raises(agent_discovery.SkillScopeResolutionError):
        agent_discovery.session_skill_globs("", mate_worker.MATE_CONDUCTOR_AGENT)


def test_every_default_agent_sees_the_whole_catalog_when_unmapped():
    """The crewmate's conductor and worker are default agents, so the set that grants
    the whole catalog to an unmapped template holds them, and any name added to the
    default-agent set later."""
    from kiro_crew.agent_files import KIROCREW_DEFAULT_AGENT_NAMES

    assert KIROCREW_DEFAULT_AGENT_NAMES <= agent_discovery.WHOLE_CATALOG_TEMPLATES
    assert "kirocrew-worker" in agent_discovery.WHOLE_CATALOG_TEMPLATES


@pytest.mark.parametrize("agent", [mate_worker.MATE_CONDUCTOR_AGENT, mate_worker.MATE_WORKER_AGENT])
@pytest.mark.parametrize("provider", ["kas", "claude_code", None])
def test_an_unmapped_crewmate_searches_the_catalog_its_startup_directory_lists(
    mates, monkeypatch, agent, provider
):
    """Off kiro-cli no skill tree is inherited, so an unmapped crewmate conductor or
    worker gets the whole catalog in its startup directory; skill_search answers the
    same whole catalog (``None``), not an empty scope."""
    from kiro_crew import context

    folder, _skills = mates
    _spec(folder, agent, [])
    key = ""
    if agent == mate_worker.MATE_WORKER_AGENT:
        key = "chat-1"
        _unmapped_crewmate_worker_session(folder, monkeypatch, key)

    assert agent_discovery.session_skill_globs(key, agent, provider_type=provider) is None
    assert context._skills_injection_plan(
        agent, is_cc=False, provider_type=provider, session_key=key
    ) == (True, [])


def _unmapped_crewmate_worker_session(folder, monkeypatch, key: str) -> None:
    """*key* is a worker session of crewmate ``pixel``, whose Abilities map no skill."""
    _spec(folder, "pixel", [], mcpServers={})
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda k, **_: (
            ExecutionContext(
                "pixel",
                MemoryStoreRef("member-pixel", "pixel"),
                "template",
                mate_worker.MATE_WORKER_AGENT,
                "persistent",
            )
            if k == key
            else None
        ),
    )


def test_kas_puts_no_skill_rule_on_an_unmapped_crewmate(mates, tmp_path):
    """KAS reads the same set: an unmapped crewmate gets no deny rule (the whole
    catalog), even in a workspace that opts out of inherited skill trees, where an
    unmapped custom agent is held to no skills."""
    from kiro_crew.acp import kas_skill_scope
    from kiro_crew.acp import skill_projection as projection

    folder, _skills = mates
    project = tmp_path / "project"
    settings = project / ".kiro" / "settings" / "cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({projection._INHERIT_SETTING: True}), encoding="utf-8")
    for agent in (mate_worker.MATE_CONDUCTOR_AGENT, mate_worker.MATE_WORKER_AGENT, "custom"):
        _spec(folder, agent, [])
        spec = json.loads((folder / f"{agent}.json").read_text(encoding="utf-8"))
        scope = kas_skill_scope.scope_globs(agent, spec, agents_dir=folder, work_dir=project)
        assert scope == ([] if agent == "custom" else None), agent


def test_by_name_readers_find_the_crewmate_service_specs(mates, tmp_path, monkeypatch):
    """Hooks and spec paths resolve the conductor in the crewmate folder."""
    from kiro_crew.agent_parts.spec_lookup import agent_spec_path
    from kiro_crew.agent_sdk import spec_hooks

    folder, _ = mates
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    deny = {"preToolUse": [{"command": "deny.sh"}]}
    _spec(folder, mate_worker.MATE_CONDUCTOR_AGENT, [], hooks=deny)

    assert spec_hooks._agent_spec(mate_worker.MATE_CONDUCTOR_AGENT)["hooks"] == deny
    path = agent_spec_path(mate_worker.MATE_CONDUCTOR_AGENT)
    assert path == folder / f"{mate_worker.MATE_CONDUCTOR_AGENT}.json"

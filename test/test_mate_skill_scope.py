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


# ── the worker's record is filed under its dashboard: session key ──


def _bound_worker_with_mate_skill(mates, monkeypatch):
    """A crewmate worker on ``dashboard:chat-7`` whose crewmate maps ``release``.

    The execution record is really bound (not a stub), under the key
    ``session_create`` uses, so a lookup by any other key finds no record.
    """
    from kiro_crew.execution_context import bind_session_execution

    folder, skills = mates
    _spec(folder, mate_worker.MATE_WORKER_AGENT, [])
    _spec(folder, "pixel", [f"skill://{skills}/release/SKILL.md"], mcpServers={})
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, **_: "pixel")
    bind_session_execution(
        "dashboard:chat-7",
        ExecutionContext(
            "pixel",
            MemoryStoreRef("member-pixel", "pixel"),
            "template",
            mate_worker.MATE_WORKER_AGENT,
            "persistent",
        ),
    )
    return skills / "release" / "SKILL.md"


@pytest.mark.parametrize("key", ["dashboard:chat-7", "chat-7"])
def test_a_crewmate_skill_reaches_its_worker_by_either_slot_key(mates, monkeypatch, key):
    """The bare slot name and the dashboard: key both find the worker's record."""
    skill = _bound_worker_with_mate_skill(mates, monkeypatch)

    try:
        globs = agent_discovery.session_skill_globs(key, mate_worker.MATE_WORKER_AGENT)
    except agent_discovery.SkillScopeResolutionError as exc:
        pytest.fail(f"the worker's skill scope was refused for {key!r}: {exc}")

    assert _found(globs, skill)


def test_the_workers_tool_limits_read_the_same_record_as_its_skills(mates, monkeypatch):
    """KAS and the MCP projection read ``worker_session_spec`` directly; a bare slot
    name gives them the crewmate's own limits, not a refusal."""
    folder, _skills = mates
    _bound_worker_with_mate_skill(mates, monkeypatch)
    _spec(folder, "pixel", [], mcpServers={}, excludedTools=["execute_bash"])

    try:
        spec = mate_worker.worker_session_spec({"name": mate_worker.MATE_WORKER_AGENT}, "chat-7")
    except mate_worker.WorkerKitUnreadable as exc:
        pytest.fail(f"the worker was refused instead of given its crewmate's limits: {exc}")

    assert "execute_bash" in spec["excludedTools"]


def test_a_bare_slot_name_with_its_own_record_keeps_it(mates, monkeypatch):
    """The bare name is mapped only when it names no record of its own."""
    from kiro_crew.execution_context import bind_session_execution

    folder, skills = mates
    _bound_worker_with_mate_skill(mates, monkeypatch)
    bind_session_execution(
        "chat-7",
        ExecutionContext(
            "scout",
            MemoryStoreRef("member-scout", "scout"),
            "template",
            mate_worker.MATE_WORKER_AGENT,
            "persistent",
        ),
    )
    _spec(folder, "scout", [f"skill://{skills}/triage/SKILL.md"], mcpServers={})
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, **_: member)

    globs = agent_discovery.session_skill_globs("chat-7", mate_worker.MATE_WORKER_AGENT)

    assert _found(globs, skills / "triage" / "SKILL.md")
    assert not _found(globs, skills / "release" / "SKILL.md")


@pytest.mark.parametrize("key", ["", None])
def test_a_worker_with_no_session_key_is_refused_as_before(mates, monkeypatch, key):
    _bound_worker_with_mate_skill(mates, monkeypatch)

    with pytest.raises(mate_worker.WorkerKitUnreadable):
        mate_worker.worker_session_spec({"name": mate_worker.MATE_WORKER_AGENT}, key)


@pytest.mark.parametrize("key", ["chat-8", "slack:chat-7", "chat-7x", "x"])
def test_a_worker_key_naming_no_record_is_still_refused(mates, monkeypatch, key):
    """Only the exact bare-slot shape is mapped, and only onto a record that exists."""
    _bound_worker_with_mate_skill(mates, monkeypatch)

    with pytest.raises(agent_discovery.SkillScopeResolutionError):
        agent_discovery.session_skill_globs(key, mate_worker.MATE_WORKER_AGENT)


@pytest.mark.parametrize(
    ("key", "bare"),
    [
        ("chat-7", True),
        ("chat-7-1785396512", True),
        ("chat-", False),
        ("chat-7-", False),
        ("chat--7", False),
        ("chat-7-1-2", False),
        ("chat-7x", False),
        ("chat-\u0663", False),  # an Arabic-Indic digit: only 0-9 names a slot
        ("dashboard:chat-7", False),
        ("Chat-7", False),
    ],
)
def test_the_bare_slot_shape_is_checked_with_plain_string_tests(key, bare):
    """The bare-slot check takes exactly ``chat-<digits>[-<digits>]``, ASCII digits only."""
    assert mate_worker._is_bare_dashboard_slot(key) is bare
    # Product code takes no new regex: the shape is plain string checks.
    assert not hasattr(mate_worker, "re")

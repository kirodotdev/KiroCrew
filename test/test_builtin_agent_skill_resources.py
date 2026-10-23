# Copyright Kiro Crew contributors
# SPDX-License-Identifier: Apache-2.0
"""A generated agent can reach the skills its own prompt tells it to read.

Every spec Kiro Crew generates for itself is a custom template, so under the scoping
rule it sees exactly its own ``skill://`` resources: with none,
``session_skill_globs`` answers ``[]`` and the catalog answers nothing. The
conductors, the team lead, the dashboard author and the dashboard manager each carry
a prompt naming a skill to load first, so each one needs that skill mapped or it is
an agent told to read a document it cannot find.

These tests are pinned on REACHABILITY, not on names: a test asserting only that the
skill's NAME appears in the prompt holds while the agent can reach nothing. Each
agent is asked the two questions its session asks -- does ``session_skill_globs``
give a non-empty scope, and does an exact read inside that scope return the body --
against the REAL shipped ``SKILL.md`` files, copied into a private skills root.

One known limit is pinned with them rather than hidden: ``pipeline-conductor``'s
``SKILL.md`` is larger than one read's capacity, so its body arrives in PAGES. The
mapping is what makes the pages reachable at all; it does not change the size.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

import pytest

from kiro_crew import agent
from kiro_crew import skills as skills_mod
from kiro_crew.agent_discovery import (
    clear_list_agents_cache,
    session_skill_globs,
    skill_resource_uris,
)
from kiro_crew.agent_materialization import (
    conductor_agents,
    service_agents,
    skill_resources,
    worker_agent,
)
from kiro_crew.context import _skills_injection_plan
from kiro_crew.skills import SKILL_READ_CAPACITY, SkillsLoader

_BUILTIN_SKILLS = Path(skills_mod.__file__).parent / "builtin_skills"

#: The default template's own steering resource, which every derived spec inherits.
#: Named here so the tests can assert the mapping was MERGED beside it.
_STEERING_GLOB = "file://.kiro/steering/**/*.md"

#: ``agent name -> (installer, the skill keys that agent's prompt names)``.
#:
#: The keys are the claim this module makes, so they are read from the installers'
#: behaviour and not from a list the installers also read: each row is what the
#: prompt the installer writes tells the agent to load. ``kirocrew-ledger-conductor``
#: is here because it is a deprecated ALIAS emitting the conductor's body -- an alias
#: that reached a different skill set would silently change what an in-flight session
#: can do.
AGENTS: dict[str, tuple[object, tuple[str, ...]]] = {
    "kirocrew-conductor": (
        conductor_agents._install_conductor_agent,
        ("goal-conductor", "dashboard", "widgets", "living-ui", "artifacts"),
    ),
    "kirocrew-ledger-conductor": (
        conductor_agents._install_ledger_conductor_agent,
        ("goal-conductor", "dashboard", "widgets", "living-ui", "artifacts"),
    ),
    "kirocrew-pipeline-conductor": (
        conductor_agents._install_pipeline_conductor_agent,
        ("pipeline-conductor", "dashboard"),
    ),
    "kirocrew-security-conductor": (
        conductor_agents._install_security_conductor_agent,
        ("security-conductor",),
    ),
    "kirocrew-team-lead": (
        service_agents._install_team_lead_agent,
        ("team-lead", "goal-conductor", "dashboard"),
    ),
    "kirocrew-dashboard-manager": (
        service_agents._install_dashboard_manager_agent,
        ("dashboard",),
    ),
    "kirocrew-dashboard-author": (
        worker_agent._install_dashboard_author_agent,
        ("kirocrew-dev/dashboard-template",),
    ),
}

#: Every key any row above names, once.
ALL_KEYS = tuple(dict.fromkeys(key for _, keys in AGENTS.values() for key in keys))


class _Rig:
    """A private agents directory and a private skills root holding the real bodies."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        self.skills = tmp_path / "skills"
        for key in ALL_KEYS:
            parts = PurePosixPath(key).parts
            source = _BUILTIN_SKILLS.joinpath(*parts) / "SKILL.md"
            target = self.skills.joinpath(*parts) / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            # The real body, so a size limit shows up as itself rather than as a
            # fixture's idea of one. Only SKILL.md: the scripts and references beside
            # it are not what a scope admits or refuses.
            shutil.copyfile(source, target)
        binary = tmp_path / "bin" / "kirocrew"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
        monkeypatch.setattr(skills_mod, "skills_dir", lambda: self.skills)

    def install(self, name: str) -> dict:
        AGENTS[name][0]()  # type: ignore[operator]
        clear_list_agents_cache()
        return json.loads((self.agents / f"{name}.json").read_text(encoding="utf-8"))

    def loader(self) -> SkillsLoader:
        return SkillsLoader(skills_path=self.skills, install_builtins=False)


@pytest.fixture(autouse=True)
def _no_agent_cache():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    return _Rig(tmp_path, monkeypatch)


def _scope(name: str) -> list[str]:
    """The scope the session itself resolves, with no session key bound."""
    scope = session_skill_globs("", name)
    assert scope is not None, f"{name} resolved the whole catalog, which is kirocrew's alone"
    return scope


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_the_installed_spec_maps_every_skill_its_prompt_names(name: str, rig: _Rig) -> None:
    """The mapping is on the file the installer writes, and it is exactly the keys that
    agent's prompt names -- no more, because a mapping is also the agent's search scope."""
    spec = rig.install(name)
    assert skill_resource_uris(spec) == list(
        skill_resources.prompt_skill_uris(*AGENTS[name][1])
    ), f"{name} does not map the skills its prompt names"


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_the_session_scope_is_not_empty(name: str, rig: _Rig) -> None:
    """The one question a session asks before it can find anything: what can it see?"""
    rig.install(name)
    assert len(_scope(name)) == len(AGENTS[name][1])


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_an_exact_read_returns_the_body_of_every_skill_the_prompt_names(
    name: str, rig: _Rig
) -> None:
    """Reachability, which is the whole point: the scope admits the key AND the body
    comes back. ``pipeline-conductor`` is read in pages because its body is over one
    read's capacity -- a limit of the file, not of the scope, so the pages must arrive."""
    rig.install(name)
    scope = _scope(name)
    loader = rig.loader()
    try:
        for key in AGENTS[name][1]:
            body = loader.read_scoped_skill(key, only=scope)
            if body is None:
                # A whole-body read refused for SIZE, and for size only. Paging from the
                # first line must then walk to the end, so the mapping really does put
                # the whole document in reach.
                assert (
                    rig.skills.joinpath(*PurePosixPath(key).parts) / "SKILL.md"
                ).stat().st_size > SKILL_READ_CAPACITY, (
                    f"{name}: {key} refused a whole-body read for a reason other than size"
                )
                offset: int | None = 0
                seen = 0
                while offset is not None:
                    page = loader.read_scoped_skill_page(key, only=scope, offset=offset)
                    assert getattr(page, "content", None), f"{name}: {key} page {offset} is empty"
                    seen += page.line_count
                    offset = page.next_offset
                assert seen == page.total_lines, f"{name}: {key} did not page to the end"
                continue
            assert body.strip(), f"{name}: {key} read back empty"
    finally:
        loader.close()


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_the_startup_skills_directory_turns_on(name: str, rig: _Rig) -> None:
    """The mapped set is also what puts each skill's ABSOLUTE path into the first turn,
    which is what a skill asking the agent to run a script beside it needs. It is off for
    an unmapped custom template, so this is the same mapping observed from the injection
    side."""
    rig.install(name)
    inject, globs = _skills_injection_plan(name, is_cc=False)
    assert inject is True
    assert len(globs) == len(AGENTS[name][1])


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_the_mapping_is_derived_from_the_live_skills_directory(
    name: str, rig: _Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Never a hardcoded ``~/.kiro/crew/skills``. Every generated spec is rewritten on
    each boot, so a relocated ``KIROCREW_HOME`` must come out mapped at the new root --
    a literal would map a path that holds nothing."""
    moved = tmp_path / "relocated-home" / "skills"
    (moved / "goal-conductor").mkdir(parents=True)
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: moved)

    spec = rig.install(name)

    for uri in skill_resource_uris(spec):
        assert moved.as_posix() in uri or uri.startswith("skill://~/"), uri


class TestTheMappingIsMergedAndNotOverwritten:
    """``_install_dashboard_author_agent`` is the one installer that carries a person's
    saved ``resources`` forward, so it is where merge-not-overwrite is observable."""

    def test_the_shipped_steering_resource_is_kept_beside_the_mapping(self, rig: _Rig) -> None:
        """Every derived spec inherits one ``file://`` steering glob. Replacing
        ``resources`` instead of appending to it would drop it, and
        ``.kiro/steering/**/*.md`` would stop loading for that agent."""
        spec = rig.install("kirocrew-conductor")
        resources = spec["resources"]
        assert resources[0] == _STEERING_GLOB
        assert resources[1:] == list(
            skill_resources.prompt_skill_uris(*AGENTS["kirocrew-conductor"][1])
        )

    def test_a_persons_own_mapping_keeps_its_place_and_the_charter_skill_is_appended(
        self, rig: _Rig
    ) -> None:
        config = {"resources": ["skill://~/mine/a/SKILL.md", _STEERING_GLOB]}
        skill_resources.map_prompt_skills(config, "kirocrew-dev/dashboard-template")
        assert config["resources"] == [
            "skill://~/mine/a/SKILL.md",
            _STEERING_GLOB,
            *skill_resources.prompt_skill_uris("kirocrew-dev/dashboard-template"),
        ]

    def test_a_second_boot_does_not_map_the_same_skill_twice(self, rig: _Rig) -> None:
        """The installers run on every boot, so the merge has to be idempotent: a
        duplicate URI would grow the spec by one entry per restart."""
        config: dict = {}
        for _ in range(3):
            skill_resources.map_prompt_skills(config, "goal-conductor", "dashboard")
        assert config["resources"] == list(
            skill_resources.prompt_skill_uris("goal-conductor", "dashboard")
        )

    def test_a_mapping_already_present_is_not_moved(self, rig: _Rig) -> None:
        """A person who reordered their mapping keeps that order: the merge adds what is
        missing and touches no index."""
        wanted = skill_resources.prompt_skill_uris("goal-conductor", "dashboard")
        config = {"resources": [wanted[1], "file://notes/**/*.md"]}
        skill_resources.map_prompt_skills(config, "goal-conductor", "dashboard")
        assert config["resources"] == [wanted[1], "file://notes/**/*.md", wanted[0]]

    def test_a_malformed_resources_value_is_replaced_rather_than_indexed(self, rig: _Rig) -> None:
        """``resources`` is hand-editable, so it can be any JSON. A string would make an
        element-wise merge iterate CHARACTERS; anything that is not a list is dropped and
        the mapping is written on its own."""
        config: dict = {"resources": "skill://not-a-list/SKILL.md"}
        skill_resources.map_prompt_skills(config, "dashboard")
        assert config["resources"] == list(skill_resources.prompt_skill_uris("dashboard"))

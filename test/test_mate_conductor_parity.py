# SPDX-License-Identifier: Apache-2.0
"""A crewmate's conductor keeps what the root conductor has, or says why not.

Two agents conduct work through a work ledger: the agent conductor mode runs
(``crew_mode.crew_mode_agent()``, by default ``kirocrew-conductor``, the plain and
child conductor) and ``kirocrew-mate-conductor`` (every crewmate's own chat). They are
built by different installers (``agent_materialization/conductor_agents.py`` and
``agent_materialization/service_agents.py``) and prompted from different files, so
a change that lands on one is easy to leave off the other. These tests fail when
the root conductor gains a prompt rule, an MCP server, a tool, an auto-approve
grant or a mapped skill that the crewmate's conductor does not carry and that
:data:`ALLOWLIST` does not name with a reason.

The rule they hold (``docs/system-specs/modules/crew-mode.md``, Keeping the two
conductors in step): a conductor change lands on both, or it lands here with a
reason. The ask policy is the one exception to the placeholder route: both get
the same ``output_contract`` constants (crew-mode.md, Ask cases and reroute).

* A prompt rule both need is written ONCE, in ``agent_parts/spec_prompts.py``
  ``_LEDGER_CONDUCTOR_RULES``. The root charter is assembled from it
  (``_CONDUCTOR_PROMPT_SECTIONS``) and the crewmate's ``conductor.patrol`` piece
  names it as a placeholder (``context_assembly.member.ledger_rules_block``).
* Servers, tools, grants and skills are compared on the EFFECTIVE agents: the
  specs the installers write, each put through the per-session projection
  (``acp.kas_agents.to_client_custom_agent``), which for a crewmate's thread adds
  the session-level dashboard and panel servers and their grants. A server added
  on either path is therefore seen (:func:`_effective`).

The direction is one way: the crewmate's conductor carries much the root one does
not (cron, guide and mate servers, the default built-ins, its front-desk wording),
and that is not drift.

Conductor mode runs ``kirocrew-conductor`` by default
(``crew_mode.DEFAULT_CREW_MODE_AGENT``), the root conductor compared above. A person
can still set ``agent.crew_mode_agent`` to a third agent, ``kirocrew-team-lead``.
``goal-conductor/SKILL.md`` points at the shared rules instead of restating them,
so the team lead carries them in its prompt (``_SHARED_CONDUCTOR_RULES_BLOCK``);
the last section holds that, the prompt -> ``team-lead`` -> ``goal-conductor``
chain, and the skill's pointers (:data:`SKILL_POINTERS`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, crew_mode, crewmate_prompts
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME, MATE_CONDUCTOR_AGENT_FILENAME
from kiro_crew.agent_materialization import service_agents
from kiro_crew.config import paths as paths_mod

#: Every intended difference, one line of reason each. Keys are ``<kind>:<name>``,
#: read off the effective agents (:func:`_effective`): ``prompt:`` a section of
#: ``_CONDUCTOR_PROMPT_SECTIONS``, ``server:`` a server the agent mounts, ``tool:`` a
#: built-in ``tools`` entry, ``grant:`` an allowed ``srv/verb`` or
#: ``capability:<name>``, ``skill:`` a mapped skill key. Anything the root conductor
#: has that is not here must be on the crewmate's conductor too.
ALLOWLIST: dict[str, str] = {
    # -- prompt sections of the root charter --
    "prompt:role": "names kirocrew-conductor and its round loop; the crewmate's identity comes from its member section",
    "prompt:reports_as_data": "a crewmate's worker also wakes its thread with [Worker report] turns (page line, capability gaps, Bots without the ledger); conductor.patrol puts every dispatched task in the ledger",
    "prompt:never_do_work": "a crewmate may do a quick command or small edit itself (conductor.operating_mode), so it keeps the default built-ins",
    "prompt:shell_scope": "states why the root conductor's shell exists; a crewmate's conductor has the default shell for any work",
    "prompt:dispatch_in_one_call": "work_dispatch requires `agent`, which a crewmate's session_create overrides to kirocrew-mate-worker; the crewmate runs the four calls",
    "prompt:dispatch_session_create": "names an explicit `agent`; a crewmate's session_create forces kirocrew-mate-worker, so its step 2 is worded in conductor.patrol",
    "prompt:which_agent": "a crewmate's session_create always opens kirocrew-mate-worker (session_create.py), so there is no agent to choose",
    "prompt:patrol_refused": "a crewmate whose arm is refused keeps its [Worker report] turns as the patrol instead of a blocking `wait`, worded in conductor.patrol",
    "prompt:rounds_back_to_back": "a crewmate's thread is driven by the user and its Heartbeat, not by a round loop",
    "prompt:round_close_reset": "a crewmate's DM thread lives for years and is never reset at a round close",
    "prompt:spend_bounded": "a crewmate is not driven by a round loop; a refused create names its own concurrency cap",
    "prompt:patrol_ends": "a crewmate stops a loop with monitor_stop when no item is open, and escalates through its own ladder ([HOW YOU WORK] item 3)",
    "prompt:steer": "steer events come from the Crew panel, which shows Crew Mode trees and not a crewmate's workers",
    "prompt:own_session_ledger": "names session_ledger goal/phase/next, the root conductor's round state; a crewmate keeps its state in its briefing and goal",
    "prompt:talking_to_the_person": "the Crew Mode introduction, board widget and task-dashboard artifact; a crewmate talks and reports through its own member pieces",
    "prompt:dispatched_by_a_conductor": "a crewmate's conductor is never dispatched onto a parent's ledger item",
    "prompt:sealed_crew_paths": "a crewmate's conductor runs on the default contract, config/prompt.md, which states the same sealed-path rule",
    "prompt:tool_inventory": "lists the root conductor's narrowed tool set; a crewmate has the default tools and its member pieces name the crewmate-only ones",
    "prompt:goal_changes": "round-boundary goal changes; a crewmate's goal is set through set_goal (member pieces)",
    "prompt:tracker": "a tracker is for a fleet one compact read cannot hold; a crewmate's ledger is one level on one machine and its [Worker report] turns carry each worker's state",
}

#: Where a new rule or mount goes, quoted by every failure below.
_WHERE = (
    "A conductor change lands on both conductors, or it lands in ALLOWLIST in "
    "test/test_mate_conductor_parity.py with a one-line reason "
    "(docs/system-specs/modules/crew-mode.md, Keeping the two conductors in step)."
)

#: The crewmate piece the shared rules are rendered into.
_MATE_PIECE = "conductor.patrol"


def _mate_ledger_text() -> str:
    from kiro_crew.context_assembly import member

    return member.operating_mode_block("Cove", ledger=True)


# -- the prompt ---------------------------------------------------------------- #


def test_the_root_charter_is_its_sections_and_every_rule_is_one_of_them() -> None:
    sections = agent._CONDUCTOR_PROMPT_SECTIONS
    assert agent._CONDUCTOR_SYSTEM_PROMPT == "".join(text for _n, text in sections)
    names = [name for name, _t in sections]
    assert len(names) == len(set(names)), f"duplicate section names: {names}"
    rules = agent._LEDGER_CONDUCTOR_RULES
    for name, text in sections:
        if name in rules:
            assert text is rules[name], f"section {name!r} restates its rule instead of naming it"
    unused = sorted(set(rules) - set(names))
    assert unused == [], f"rules the root charter never carries: {unused}"


def test_every_root_charter_section_is_shared_or_allowlisted() -> None:
    rules = agent._LEDGER_CONDUCTOR_RULES
    unclassified = []
    for name, text in agent._CONDUCTOR_PROMPT_SECTIONS:
        if name in rules or f"prompt:{name}" in ALLOWLIST:
            continue
        # A bare heading is layout, not a rule: the crewmate piece has its own.
        if name.startswith("heading_") and re.fullmatch(r"#{1,6} [^\n]+\n\n", text):
            continue
        unclassified.append(name)
    assert unclassified == [], (
        f"root conductor prompt sections the crewmate's conductor neither carries nor "
        f"allowlists: {unclassified}. A rule both need goes into _LEDGER_CONDUCTOR_RULES "
        f"in src/kiro_crew/agent_parts/spec_prompts.py and its placeholder into "
        f"{_MATE_PIECE} in src/kiro_crew/crewmate_prompts.json. {_WHERE}"
    )


def test_every_shared_rule_reaches_the_crewmate_conductor() -> None:
    text = _mate_ledger_text()
    missing = sorted(
        name for name, rule in agent._LEDGER_CONDUCTOR_RULES.items() if rule.strip() not in text
    )
    assert missing == [], (
        f"shared conductor rules the crewmate's conductor is not told: {missing}. Add "
        f"each as a {{placeholder}} to {_MATE_PIECE} in src/kiro_crew/crewmate_prompts.json. "
        f"{_WHERE}"
    )


def test_the_crewmate_piece_names_only_shared_rules() -> None:
    unknown = sorted(
        crewmate_prompts.placeholders(_MATE_PIECE) - set(agent._LEDGER_CONDUCTOR_RULES)
    )
    assert unknown == [], f"{_MATE_PIECE} placeholders that name no shared rule: {unknown}"


def _words(text: str) -> str:
    """*text* without markdown emphasis or code marks, whitespace collapsed, lowercased."""
    return " ".join(re.sub(r"[`*]", "", text).split()).lower()


#: A sentence shorter than this is too generic to call a copy.
_MIN_SENTENCE = 40


def _sentences(text: str) -> list[str]:
    out = []
    for part in re.split(r"(?<=[.!?:])\s+", _words(text)):
        part = part.strip(" -")
        if len(part) >= _MIN_SENTENCE:
            out.append(part)
    return out


def _copies(pieces: dict[str, str]) -> list[str]:
    """Root charter sentences found in any of *pieces* (crewmate key -> raw text)."""
    flat = {key: _words(text) for key, text in pieces.items()}
    return sorted(
        f"{key}: {name}: {sentence[:60]!r}"
        for name, text in agent._CONDUCTOR_PROMPT_SECTIONS
        for sentence in _sentences(text)
        for key, piece in flat.items()
        if sentence in piece
    )


def test_no_root_charter_sentence_is_copied_into_a_crewmate_piece() -> None:
    """A copy is the drift this file exists to stop: it reads the same today and
    nothing keeps it so. The shared rules reach the crewmate by placeholder, and a
    root-only sentence that a crewmate needs is a shared rule."""
    copies = _copies({key: crewmate_prompts.text(key) for key in crewmate_prompts.keys()})
    assert copies == [], (
        f"crewmate prompt pieces that copy root conductor text: {copies}. Move the rule "
        f"into _LEDGER_CONDUCTOR_RULES and name it as a placeholder. {_WHERE}"
    )


def test_the_copy_check_sees_a_reworded_copy() -> None:
    """Positive control: the band copied into a piece with its code marks dropped
    and its line breaks moved is still caught."""
    planted = (
        "Pass no interval_secs, no max_cycles and no max_runtime_secs: a work-ledger loop "
        "runs every 300 seconds with no cycle cap, and its one bound is the gateway's "
        "7-day runaway backstop, so it runs until you stop it."
    )
    assert _copies({"planted": planted})


# -- the effective agents ------------------------------------------------------ #


#: The agent conductor mode runs, the root every comparison here starts from.
_ROOT = crew_mode.crew_mode_agent()

#: The installer of each agent conductor mode can run whose charter is
#: ``_CONDUCTOR_PROMPT_SECTIONS``. A root outside it fails
#: :func:`test_conductor_mode_runs_the_charter_this_file_compares`.
_ROOT_INSTALLERS = {CONDUCTOR_AGENT_FILENAME.removesuffix(".json"): "_install_conductor_agent"}


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """Both spec files as their installers write them on this machine."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    getattr(agent, _ROOT_INSTALLERS[_ROOT])()
    service_agents._install_mate_conductor_agent()
    root = json.loads((tmp_path / f"{_ROOT}.json").read_text(encoding="utf-8"))
    mate_path = paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME
    return root, json.loads(mate_path.read_text(encoding="utf-8"))


def _effective(agent_id: str, spec: dict, *, member: bool) -> dict:
    """The agent a session actually runs: *spec* through the per-session projection
    (``acp.kas_agents.to_client_custom_agent``). For a crewmate's DM thread that
    projection adds the session-level ``@kirocrew-dashboard`` with
    ``agent._MEMBER_DASHBOARD_GRANTS`` and ``@kirocrew-panel`` with its grants, which
    the spec file never holds; the root conductor runs its spec as written."""
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    return to_client_custom_agent(agent_id, spec, "", member_dispatch=member, crew_panel=member)


def _rules(projected: dict, effect: str) -> set[str]:
    """Every resource the projected ``permissions`` gives *effect*: ``srv/verb`` (or
    ``srv/*``) for an MCP rule, ``capability:<name>`` for any other."""
    out: set[str] = set()
    for rule in (projected.get("permissions") or {}).get("rules") or []:
        if rule.get("effect") != effect:
            continue
        if rule.get("capability") == "mcp":
            out.update(m for m in rule.get("match") or [] if isinstance(m, str))
        else:
            out.add(f"capability:{rule.get('capability')}")
    return out


def _skill_keys(spec: dict) -> set[str]:
    """Catalog keys of the ``skill://`` entries in *spec*'s ``resources``."""
    from kiro_crew.skills import skills_dir

    base = skills_dir()
    keys = set()
    for entry in spec.get("resources") or []:
        if not isinstance(entry, str) or not entry.startswith("skill://"):
            continue
        # ``skill_uri_for_path`` writes a path under the home directory as ``~/``.
        raw = entry[len("skill://") :]
        path = Path.home() / raw[2:] if raw.startswith("~/") else Path(raw)
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if rel.name == "SKILL.md":
            keys.add(rel.parent.as_posix())
    return keys


def _view(projected: dict) -> dict[str, Any]:
    tools = projected.get("tools")
    entries = ["*"] if tools == "*" else [t for t in tools or [] if isinstance(t, str)]
    return {
        "servers": set(projected.get("mcpServers") or {})
        | {t[1:].split("/", 1)[0] for t in entries if t.startswith("@")},
        "tools": {t for t in entries if not t.startswith("@")},
        "allow": _rules(projected, "allow"),
        "deny": _rules(projected, "deny"),
        "excluded": {t for t in projected.get("excludedTools") or [] if isinstance(t, str)},
        "skills": _skill_keys(projected),
    }


def _differences(root_spec: dict, mate_spec: dict) -> dict[str, list[str]]:
    """What the effective root conductor mounts, grants or maps that the effective
    crewmate conductor does not, per kind."""
    root = _view(_effective(_ROOT, root_spec, member=False))
    mate = _view(_effective("kirocrew-mate-conductor", mate_spec, member=True))

    def withheld(resource: str) -> bool:
        server = resource.split("/", 1)[0]
        return bool(
            {resource, f"{server}/*"} & mate["deny"]
            or {f"@{resource}", f"@{server}", "*"} & mate["excluded"]
        )

    def granted(resource: str) -> bool:
        server = resource.split("/", 1)[0]
        return bool({resource, f"{server}/*"} & mate["allow"]) and not withheld(resource)

    return {
        "server": sorted(root["servers"] - mate["servers"]),
        "tool": sorted(
            t
            for t in root["tools"]
            if ("*" not in mate["tools"] and t not in mate["tools"]) or t in mate["excluded"]
        ),
        "grant": sorted(g for g in root["allow"] if not granted(g)),
        "skill": sorted(root["skills"] - mate["skills"]),
    }


def _unallowed(kind: str, names: list[str]) -> list[str]:
    return sorted(n for n in names if f"{kind}:{n}" not in ALLOWLIST)


#: Where each kind of difference is fixed on the crewmate's side.
_FIX = {
    "server": (
        "mount it in service_agents._install_mate_conductor_agent, or for a "
        "session-level server in the member projection (acp/kas_agents.py)"
    ),
    "tool": "mount it in service_agents._install_mate_conductor_agent",
    "grant": (
        "a work-ledger verb goes into the grants service_agents._mount_work_ledger is "
        "given for the conductor; a session-control verb into agent._MEMBER_DASHBOARD_GRANTS"
    ),
    "skill": "map it in service_agents._install_mate_conductor_agent",
}


@pytest.mark.parametrize("home_form", [True, False], ids=["tilde", "absolute"])
def test_skill_keys_read_both_uri_forms(monkeypatch: pytest.MonkeyPatch, home_form: bool) -> None:
    """A skills tree under the home directory is mapped as ``skill://~/...``."""
    from kiro_crew.agent_materialization import skill_resources
    from kiro_crew.skills import skills_dir

    home = skills_dir().parent if home_form else skills_dir() / "elsewhere"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    uris = skill_resources.prompt_skill_uris("goal-conductor", "kirocrew-dev/babysit")
    assert all(u.startswith("skill://~/") for u in uris) is home_form
    assert _skill_keys({"resources": uris}) == {"goal-conductor", "kirocrew-dev/babysit"}


#: A minimal skill file whose name the KAS scope reads (``kas_skill_scope.skill_names``).
_SKILL_MD = "---\nname: {0}\ndescription: A {0} skill.\n---\n# {0}\n"


def test_a_mapped_crewmate_conductor_loads_only_its_mapped_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crewmate's chat maps ``goal-conductor`` for the ledger, and that mapping is
    its whole skill scope, as on the conductor in conductor mode: the user's
    ``~/.kiro/skills`` and the workspace's ``.kiro/skills`` are not added
    (the decision entry ``conductors-stay-simple-and-new-abilities-go-to-workers``).
    Both trees exist here, so the inherited set is not empty and the check cannot
    pass vacuously."""
    from kiro_crew import skills as skills_mod
    from kiro_crew.agent_discovery_parts import skill_scope
    from kiro_crew.agent_discovery_parts.skill_scope import (
        agent_skill_globs,
        inherited_skill_globs,
    )

    # The scope the next release ships; this release still inherits by default.
    monkeypatch.setattr(skill_scope, "MATE_CONDUCTOR_INHERITS_USER_SKILLS", False)
    kiro_home = tmp_path / "kiro-home"
    (kiro_home / "skills" / "mine").mkdir(parents=True)
    (kiro_home / "skills" / "mine" / "SKILL.md").write_text(
        _SKILL_MD.format("mine"), encoding="utf-8"
    )
    project = tmp_path / "project"
    (project / ".kiro" / "skills" / "ours").mkdir(parents=True)
    (project / ".kiro" / "skills" / "ours" / "SKILL.md").write_text(
        _SKILL_MD.format("ours"), encoding="utf-8"
    )
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    # Workspace trust is a separate gate with its own tests; this pins the scope rule.
    monkeypatch.setattr(skills_mod, "project_skill_tree_present", lambda _d: True)
    _root, mate_spec = _install(tmp_path, monkeypatch)
    assert "goal-conductor" in _skill_keys(mate_spec)

    inherited = inherited_skill_globs(project)
    assert any(str(kiro_home) in g for g in inherited), inherited
    assert any(str(project) in g for g in inherited), inherited
    scope = agent_skill_globs(
        "kirocrew-mate-conductor", project_dir=project, strict=True, inherit_defaults=True
    )
    assert scope, "the mapped conductor resolved no skill at all"
    added = [g for g in inherited if g in scope]
    assert added == [], f"a mapped crewmate conductor took the user's skill trees: {added}"

    # The per-turn triggered-skill filter asks without ``inherit_defaults``
    # (``context_assembly/turn_message.py``); it must hold the mapping alone too.
    from kiro_crew import context as ctx

    per_turn = ctx.agent_skill_globs("kirocrew-mate-conductor", project_dir=project)
    added = [g for g in inherited if g in per_turn]
    assert added == [], (
        f"the per-turn triggered-skill filter gives a mapped crewmate conductor the "
        f"user's skill trees: {added}"
    )

    # KAS loads both trees by itself; the scope must send a rule that denies them.
    from kiro_crew.acp.kas_skill_scope import scope_globs, skill_scope_rules

    globs = scope_globs(
        "kirocrew-mate-conductor",
        mate_spec,
        agents_dir=paths_mod.mate_agents_dir(),
        work_dir=project,
    )
    offered = []
    for name in ("mine", "ours"):
        denied = False
        for rule in skill_scope_rules(globs, work_dir=project):
            if rule.get("effect") != "deny":
                continue
            if "exclude" in rule:
                denied = denied or name not in rule["exclude"]
            else:
                denied = denied or "match" not in rule or name in rule["match"]
        if not denied:
            offered.append(name)
    assert offered == [], f"on KAS a mapped crewmate conductor is offered user skills: {offered}"


@pytest.fixture
def specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    return _install(tmp_path, monkeypatch)


@pytest.mark.parametrize("kind", sorted(_FIX))
def test_the_root_conductors_surface_is_on_the_crewmates(specs, kind: str) -> None:
    missing = _unallowed(kind, _differences(*specs)[kind])
    assert missing == [], (
        f"{kind}s only the effective root conductor has: {missing}. To carry one, "
        f"{_FIX[kind]}. {_WHERE}"
    )


def test_both_effective_agents_run_the_ledger_and_goal_conductor(specs) -> None:
    root_spec, mate_spec = specs
    root = _view(_effective(_ROOT, root_spec, member=False))
    mate = _view(_effective("kirocrew-mate-conductor", mate_spec, member=True))
    for view in (root, mate):
        assert {"kirocrew-work", "kirocrew-dashboard"} <= view["servers"]
        assert {"kirocrew-work/work_ledger_read", "kirocrew-work/work_ledger_record"} <= (
            view["allow"]
        )
        assert "goal-conductor" in view["skills"]


def test_conductor_mode_runs_the_charter_this_file_compares() -> None:
    """The root is what conductor mode runs, and its prompt is the sectioned charter
    the prompt checks above read; a default moved to an agent with another prompt
    must bring its own comparison here first."""
    assert _ROOT in _ROOT_INSTALLERS, (
        f"conductor mode runs {_ROOT!r}, whose prompt is not _CONDUCTOR_PROMPT_SECTIONS; "
        f"compare its rules against kirocrew-mate-conductor before moving the default."
    )


def test_every_shared_rule_renders_on_both_installed_conductors(specs) -> None:
    """Each shared rule is in the prompt the root's spec file carries AND in the text
    the crewmate's chat is given, so neither side can lose one alone."""
    root_spec, _mate = specs
    mate_text = _mate_ledger_text()
    missing = sorted(
        f"{side}: {name}"
        for name, rule in agent._LEDGER_CONDUCTOR_RULES.items()
        for side, text in ((_ROOT, root_spec["prompt"]), ("kirocrew-mate-conductor", mate_text))
        if rule.strip() not in text
    )
    assert missing == [], f"shared rules missing from a conductor: {missing}. {_WHERE}"


#: Rules this change made shared, from the team lead and the long-task conductor.
_NEW_SHARED = (
    "item_budget",
    "own_files",
    "merge_lines",
    "plan_reviewer",
    "bar_pr_merged",
    "wake_check",
    "routine_incidents",
    "report_before_park",
)


def test_the_long_task_and_team_rules_are_shared() -> None:
    assert set(_NEW_SHARED) <= set(agent._LEDGER_CONDUCTOR_RULES)
    assert set(_NEW_SHARED) <= crewmate_prompts.placeholders(_MATE_PIECE)


def test_a_patrol_wake_gets_one_line_on_both_conductors(specs) -> None:
    """The one-line patrol reply is a shared rule both installed conductors carry, and
    no quiet-end wording tells a patrol wake to stay silent, so neither side is told
    two things."""
    root_spec, _mate = specs
    rule = agent._LEDGER_CONDUCTOR_RULES["patrol_reply"].strip()
    assert "patrol_reply" in crewmate_prompts.placeholders(_MATE_PIECE)
    for side, text in (
        (_ROOT, root_spec["prompt"]),
        ("kirocrew-mate-conductor", _mate_ledger_text()),
    ):
        assert rule in text, f"{side} lacks the patrol_reply rule"
    assert "nothing_to_do" not in agent._LEDGER_CONDUCTOR_RULES["wake_check"]
    assert "its wakes are the one exception" in _words(
        crewmate_prompts.text("conductor.how_you_work")
    )
    assert 'never post "nothing changed"' not in _skill_text("goal-conductor")
    # The one line is for a wake with no news only: milestones keep their full
    # report (and the root's in-chat board), and the Bot's Heartbeat and
    # [Worker report] turns keep their own quiet end.
    words = _words(rule)
    assert "a task starting, finishing or getting stuck is reported in full" in words
    assert "a heartbeat or [worker report] turn keeps its own quiet end" in words
    talking = dict(agent._CONDUCTOR_PROMPT_SECTIONS)["talking_to_the_person"]
    assert "patrol wake with no milestone" in _words(talking)


def test_the_render_check_sees_a_rule_lost_on_one_side(
    specs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a shared rule the crewmate piece stops naming is reported."""
    from kiro_crew.context_assembly import member

    real = member.ledger_rules_block

    def dropped(key: str) -> str:
        return real(key).replace(agent._LEDGER_CONDUCTOR_RULES["wake_check"].strip(), "")

    monkeypatch.setattr(member, "ledger_rules_block", dropped)
    with pytest.raises(AssertionError, match="kirocrew-mate-conductor: wake_check"):
        test_every_shared_rule_renders_on_both_installed_conductors(specs)


def test_the_crewmates_dashboard_comes_from_the_projection_not_the_file(specs) -> None:
    """Why the comparison is on the effective agent: the spec file alone lacks it."""
    _root, mate_spec = specs
    assert "kirocrew-dashboard" not in mate_spec["mcpServers"]
    mate = _view(_effective("kirocrew-mate-conductor", mate_spec, member=True))
    assert "kirocrew-dashboard" in mate["servers"]
    assert "kirocrew-dashboard/session_create" in mate["allow"]


def test_the_work_ledger_entry_is_the_same_on_both(specs) -> None:
    root, mate = specs
    assert mate["mcpServers"]["kirocrew-work"] == root["mcpServers"]["kirocrew-work"]


def test_a_new_mount_on_the_root_conductor_alone_is_caught(specs) -> None:
    """Positive control: a future root-only change, made in the root spec and seen
    through the projection, is reported for every kind."""
    from kiro_crew.agent_materialization import skill_resources

    root, mate = specs
    root = json.loads(json.dumps(root))
    root["mcpServers"]["kirocrew-new"] = {"command": "kirocrew", "args": ["mcp-new"]}
    root["tools"].extend(["@kirocrew-new", "fs_new"])
    # Mounted whole on the crewmate's conductor, but withheld there by excludedTools.
    root["allowedTools"].append("@kirocrew-core/spawn_run")
    skill_resources.map_prompt_skills(root, "kirocrew-dev/babysit")
    found = _differences(root, mate)
    assert "kirocrew-new" in found["server"]
    assert "fs_new" in found["tool"]
    assert "kirocrew-core/spawn_run" in found["grant"]
    assert "kirocrew-dev/babysit" in found["skill"]


def test_a_new_root_charter_section_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control for the prompt half."""
    planted = (*agent._CONDUCTOR_PROMPT_SECTIONS, ("new_rule", "Always do the new thing.\n"))
    monkeypatch.setattr(agent, "_CONDUCTOR_PROMPT_SECTIONS", planted)
    with pytest.raises(AssertionError, match="new_rule"):
        test_every_root_charter_section_is_shared_or_allowlisted()


def test_every_allowlist_entry_is_still_a_difference(specs) -> None:
    """A reason for a difference that does not exist is a stale exemption that
    would let the next real one through unremarked."""
    root, mate = specs
    rules = agent._LEDGER_CONDUCTOR_RULES
    live = {f"prompt:{n}" for n, _t in agent._CONDUCTOR_PROMPT_SECTIONS if n not in rules}
    for kind, names in _differences(root, mate).items():
        live |= {f"{kind}:{n}" for n in names}
    stale = sorted(set(ALLOWLIST) - live)
    assert stale == [], f"ALLOWLIST entries that name no difference: {stale}"


# -- Conductor mode's team lead ------------------------------------------------ #

_SKILLS_DIR = Path(agent.__file__).parent / "builtin_skills"


def _skill_text(name: str) -> str:
    return _words((_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8"))


def _team_lead_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    from kiro_crew.agent_files import TEAM_LEAD_AGENT_FILENAME

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_team_lead_agent()
    spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
    return spec["prompt"]


def test_a_team_lead_carries_every_shared_rule_in_its_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``goal-conductor/SKILL.md`` points at "your prompt's" shared rules instead of
    restating them, so every agent that conducts from it carries them in its prompt:
    the root charter and a Bot's ``conductor.patrol`` (above), and a team lead set as
    ``agent.crew_mode_agent``, through ``_SHARED_CONDUCTOR_RULES_BLOCK``."""
    prompt = _team_lead_prompt(tmp_path, monkeypatch)
    missing = sorted(
        name for name, rule in agent._LEDGER_CONDUCTOR_RULES.items() if rule.strip() not in prompt
    )
    assert missing == [], f"shared rules the team lead's prompt lacks: {missing}"


def test_the_team_lead_check_sees_a_lost_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a block missing one rule is reported by key."""
    rule = agent._LEDGER_CONDUCTOR_RULES["close_item"].strip()
    monkeypatch.setattr(
        agent,
        "_SHARED_CONDUCTOR_RULES_BLOCK",
        agent._SHARED_CONDUCTOR_RULES_BLOCK.replace(rule, ""),
    )
    with pytest.raises(AssertionError, match="close_item"):
        test_a_team_lead_carries_every_shared_rule_in_its_prompt(tmp_path, monkeypatch)


#: Shared rules goal-conductor/SKILL.md names by a pointer instead of restating:
#: the phrase that points at each. A pointer the skill drops leaves a reader who
#: only skims the skill with no hint the rule exists.
SKILL_POINTERS: dict[str, str] = {
    "dependent_calls": "your prompt's rule on dependent calls",
    "goal_plan": "the five parts your prompt's goal-plan rule names",
    "resource_wave": "as your prompt says, resource_status decides the wave",
    "own_files": "your prompt's rules on one owner per shared file",
    "item_budget": "your prompt's item-budget rule",
    "bind_before_seed": "bind before you seed, as your prompt's rule says",
    "patrol_watch": "exactly as your prompt's patrol rules say",
    "routine_incidents": "your prompt's routine-incidents rule lists them",
    "report_before_park": "report before you park, as your prompt's rule says",
    "claimed_pr": "your prompt's claimed-pr rule",
    "bar_pr_merged": "as your prompt's rule on landed code says",
    "plan_reviewer": "as your prompt's plan-reviewer rule says",
    "wake_check": "a wake with no news is a check, as your prompt's rule says",
    "patrol_reply": "as your prompt's patrol-reply rule says",
    "reuse_worker": "your prompt's worker-reuse rule",
    "read_message_detail": "as your prompt's read-message rule says",
    "patrol_final_report": "end the patrol as your prompt's patrol rules say",
}


def test_every_reference_the_goal_conductor_skill_names_exists() -> None:
    """Rare paths live in ``references/``, read on demand by the root conductor and a
    Bot's conductor alike (both mount ``fs_read``); a pointer to a file that is not
    shipped leaves that path with no instructions at all."""
    base = _SKILLS_DIR / "goal-conductor"
    pages = [base / "SKILL.md", *sorted((base / "references").glob("*.md"))]
    text = "\n".join(p.read_text(encoding="utf-8") for p in pages)
    named = sorted(set(re.findall(r"references/([a-z0-9-]+\.md)", text)))
    assert named, "the skill names no reference file"
    missing = [n for n in named if not (base / "references" / n).is_file()]
    assert missing == [], f"reference files the skill names but does not ship: {missing}"


def test_goal_conductor_points_at_each_shared_rule_it_no_longer_restates() -> None:
    skill = _skill_text("goal-conductor")
    assert set(SKILL_POINTERS) <= set(agent._LEDGER_CONDUCTOR_RULES)
    missing = sorted(key for key, phrase in SKILL_POINTERS.items() if phrase not in skill)
    assert missing == [], f"goal-conductor/SKILL.md lost its pointer to shared rules: {missing}"


def test_conductor_mode_runs_the_root_conductor_by_default() -> None:
    """The agent conductor mode runs while the key is unset is the root conductor this
    file holds the shared rules against."""
    from kiro_crew import crew_mode
    from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME

    assert crew_mode.DEFAULT_CREW_MODE_AGENT == CONDUCTOR_AGENT_FILENAME.removesuffix(".json")


def test_a_team_lead_set_as_the_conductor_reaches_goal_conductor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A team lead set as ``agent.crew_mode_agent``: its prompt sends it to ``team-lead``,
    that skill sends it to ``goal-conductor``, and its spec maps both."""
    from kiro_crew.agent_files import TEAM_LEAD_AGENT_FILENAME

    prompt = _words(agent._TEAM_LEAD_SYSTEM_PROMPT)
    assert "read the team-lead skill" in prompt
    assert "procedure is goal-conductor" in prompt
    assert "read goal-conductor first" in _skill_text("team-lead")

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_team_lead_agent()
    spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert {"team-lead", "goal-conductor"} <= _skill_keys(spec)


# -- Worker reuse ---------------------------------------------------------------- #


def test_the_worker_reuse_rule_reaches_every_conductor(
    specs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a finished worker gets the next item is one shared rule: the root
    conductor's spec, a Bot's ledger patrol and the team lead all carry it, and
    no Bot piece or skill still tells the conductor to reuse a worker without
    its limits."""
    root_spec, _mate = specs
    rule = agent._LEDGER_CONDUCTOR_RULES["reuse_worker"].strip()
    assert "reuse_worker" in crewmate_prompts.placeholders(_MATE_PIECE)
    for side, text in (
        (_ROOT, root_spec["prompt"]),
        ("kirocrew-mate-conductor", _mate_ledger_text()),
        ("kirocrew-team-lead", _team_lead_prompt(tmp_path, monkeypatch)),
    ):
        assert rule in text, f"{side} lacks the reuse_worker rule"
    # The unconditional reuse lines the rule replaced.
    assert "bind it to the same session" not in _skill_text("goal-conductor")
    assert "give the worker its next step," not in _words(
        crewmate_prompts.text("conductor.operating_mode")
    )
    assert "when a worker needs its next step," not in _words(
        crewmate_prompts.text("worker_report.guidance")
    )
    assert "keep open only a worker you will send to again" not in _words(
        crewmate_prompts.text("conductor.how_you_work")
    )


def test_the_worker_reuse_check_sees_a_bot_without_the_rule(
    specs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a Bot patrol that stops naming the rule is reported."""
    from kiro_crew.context_assembly import member

    real = member.ledger_rules_block
    rule = agent._LEDGER_CONDUCTOR_RULES["reuse_worker"].strip()
    monkeypatch.setattr(member, "ledger_rules_block", lambda key: real(key).replace(rule, ""))
    with pytest.raises(AssertionError, match="kirocrew-mate-conductor lacks the reuse_worker"):
        test_the_worker_reuse_rule_reaches_every_conductor(specs, tmp_path, monkeypatch)


def _shared_rule_copies(text: str) -> list[str]:
    flat = _words(text)
    return sorted(
        f"{name}: {sentence[:60]!r}"
        for name, rule in agent._LEDGER_CONDUCTOR_RULES.items()
        for sentence in _sentences(rule)
        if sentence in flat
    )


@pytest.mark.parametrize("skill", ["goal-conductor", "team-lead"])
def test_no_conductor_skill_restates_a_shared_rule(skill: str) -> None:
    """Every agent that reads these skills carries the shared rules in its prompt,
    so a sentence copied into a skill is a second copy that can drift. The skill
    points at the rule instead."""
    text = (_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    assert _shared_rule_copies(text) == []


def test_the_skill_copy_check_sees_a_copied_rule() -> None:
    """Positive control."""
    assert _shared_rule_copies(agent._LEDGER_CONDUCTOR_RULES["own_files"])

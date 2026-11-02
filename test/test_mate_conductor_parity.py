# SPDX-License-Identifier: Apache-2.0
"""A crewmate's conductor keeps what the root conductor has, or says why not.

Two agents conduct work through a work ledger: ``kirocrew-conductor`` (Crew Mode,
Ultra chats) and ``kirocrew-mate-conductor`` (every crewmate's own chat). They are
built by different installers (``agent_materialization/conductor_agents.py`` and
``agent_materialization/service_agents.py``) and prompted from different files, so
a change that lands on one is easy to leave off the other. These tests fail when
the root conductor gains a prompt rule, an MCP server, a tool, an auto-approve
grant or a mapped skill that the crewmate's conductor does not carry and that
:data:`ALLOWLIST` does not name with a reason.

The rule they hold (``docs/system-specs/modules/crew-mode.md``, Keeping the two
conductors in step): a conductor change lands on both, or it lands here with a
reason.

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
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, crewmate_prompts
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
    "prompt:reports_as_data": "a crewmate's untracked task still reports through [Worker report] turns; conductor.patrol says which work reports through the ledger",
    "prompt:never_do_work": "a crewmate may do a quick command or small edit itself (conductor.operating_mode), so it keeps the default built-ins",
    "prompt:shell_scope": "states why the root conductor's shell exists; a crewmate's conductor has the default shell for any work",
    "prompt:dispatch_in_one_call": "work_dispatch requires `agent`, which a crewmate's session_create overrides to kirocrew-mate-worker; the crewmate runs the four calls",
    "prompt:dispatch_session_create": "names an explicit `agent`; a crewmate's session_create forces kirocrew-mate-worker, so its step 2 is worded in conductor.patrol",
    "prompt:which_agent": "a crewmate's session_create always opens kirocrew-mate-worker (session_create.py), so there is no agent to choose",
    "prompt:patrol_refused": "a crewmate whose arm is refused keeps its [Worker report] turns as the patrol instead of a blocking `wait`, worded in conductor.patrol",
    "prompt:rounds_back_to_back": "a crewmate's thread is driven by the user and its Heartbeat, not by a round loop",
    "prompt:round_close_reset": "a crewmate's DM thread lives for years and is never reset at a round close",
    "prompt:spend_bounded": "the item caps and item_cap_notice ride the round-close session ledger a crewmate does not keep; a refused create names its own cap",
    "prompt:patrol_ends": "a crewmate stops a loop with monitor_stop when no item is open, and escalates through its own ladder ([HOW YOU WORK] item 3)",
    "prompt:steer": "steer events come from the Crew panel, which shows Crew Mode trees and not a crewmate's workers",
    "prompt:own_session_ledger": "names session_ledger goal/phase/next, the root conductor's round state; a crewmate keeps its state in its briefing and goal",
    "prompt:talking_to_the_person": "the Crew Mode introduction, board widget and task-dashboard artifact; a crewmate talks and reports through its own member pieces",
    "prompt:dispatched_by_a_conductor": "a crewmate's conductor is never dispatched onto a parent's ledger item",
    "prompt:tool_inventory": "lists the root conductor's narrowed tool set; a crewmate has the default tools and its member pieces name the crewmate-only ones",
    "prompt:goal_changes": "round-boundary goal changes; a crewmate's goal is set through set_goal (member pieces)",
    # -- skills --
    "skill:widgets": "named by prompt:talking_to_the_person (the introduction and board widgets), a root-conductor-only section",
    "skill:living-ui": "reached from the widgets skill for a moving board, named by prompt:talking_to_the_person only",
    "skill:artifacts": "named by prompt:talking_to_the_person (the task-dashboard artifact), a root-conductor-only section",
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
        "Keep interval_secs within 300..900 seconds, whatever the round waits on. Take "
        "the bounds from the goal-conductor skill's patrol_budget.py check (it never "
        "raises the user's runtime; on exit 21 ask them for a longer one)."
    )
    assert _copies({"planted": planted})


# -- the effective agents ------------------------------------------------------ #


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """Both spec files as their installers write them on this machine."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent._install_conductor_agent()
    service_agents._install_mate_conductor_agent()
    root = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
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
    root = _view(_effective("kirocrew-conductor", root_spec, member=False))
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


def test_a_mapped_crewmate_conductor_keeps_the_skills_it_inherits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping ``goal-conductor`` for the ledger must not cost a crewmate's chat on
    kiro-cli the trees it inherits: the user's ``~/.kiro/skills`` and the
    workspace's ``.kiro/skills``. Both trees exist here, so the inherited set is
    not empty and the check cannot pass vacuously."""
    from kiro_crew import skills as skills_mod
    from kiro_crew.agent_discovery_parts.skill_scope import (
        agent_skill_globs,
        inherited_skill_globs,
    )

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
    missing = [g for g in inherited if g not in scope]
    assert missing == [], f"a mapped crewmate conductor lost its inherited skill trees: {missing}"

    # The per-turn triggered-skill filter asks without ``inherit_defaults``
    # (``context_assembly/turn_message.py``): a non-empty answer there narrows the
    # triggered skills to it, so it must hold the inherited trees too.
    from kiro_crew import context as ctx

    per_turn = ctx.agent_skill_globs("kirocrew-mate-conductor", project_dir=project)
    missing = [g for g in inherited if per_turn and g not in per_turn]
    assert missing == [], (
        f"the per-turn triggered-skill filter drops a mapped crewmate conductor's "
        f"inherited skill trees: {missing}"
    )

    # KAS loads both trees by itself; the scope must not send a rule that denies them.
    from kiro_crew.acp.kas_skill_scope import scope_globs, skill_scope_rules

    globs = scope_globs(
        "kirocrew-mate-conductor",
        mate_spec,
        agents_dir=paths_mod.mate_agents_dir(),
        work_dir=project,
    )
    denied = []
    for rule in skill_scope_rules(globs, work_dir=project):
        if rule.get("effect") != "deny":
            continue
        for name in ("mine", "ours"):
            if "exclude" in rule:
                hit = name not in rule["exclude"]
            else:
                hit = "match" not in rule or name in rule["match"]
            if hit:
                denied.append((name, rule))
    assert (
        denied == []
    ), f"on KAS a mapped crewmate conductor is denied its inherited skills: {denied}"


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
    root = _view(_effective("kirocrew-conductor", root_spec, member=False))
    mate = _view(_effective("kirocrew-mate-conductor", mate_spec, member=True))
    for view in (root, mate):
        assert {"kirocrew-work", "kirocrew-dashboard"} <= view["servers"]
        assert {"kirocrew-work/work_ledger_read", "kirocrew-work/work_ledger_record"} <= (
            view["allow"]
        )
        assert "goal-conductor" in view["skills"]


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

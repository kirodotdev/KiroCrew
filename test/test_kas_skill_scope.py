"""A KAS session loads the skills its kiro-cli twin loads, not every global skill.

KAS reads no ``cli.json``, so ``chat.disableInheritingDefaultResources`` -- the switch
Crew's native kiro-cli launch uses to hold a session to its mapped skills -- has no
effect there: KAS offers every skill under ``~/.kiro/skills`` and the workspace's
``.kiro/skills`` on top of the agent's ``resources``. Measured on kiro-cli 2.35.1 with
a spec mapping 3 skills: 45 on KAS, 3 through Crew's kiro-cli path.

The fix rides the one channel KAS does read, ``permissions``: KAS drops a skill the
``skill`` capability denies (by name) from what its skill tool offers. These tests
model that filter (:func:`_kas_offers`) over the payload ``KasHarness.session_extras``
really builds, and pin it to the scope Crew's ``skill_search`` resolves for a kiro-cli
session (:func:`_v2_skills`).
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_skill_scope
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.agent_discovery import session_skill_globs
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP


def _skill(root: Path, dirname: str, name: str | None = None) -> Path:
    path = root / dirname / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name or dirname}\ndescription: test skill {dirname}\n---\nbody\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def home(monkeypatch, tmp_path):
    # The suite isolates the agents directory; the kiro home the default skill
    # tree lives under is pointed at a scratch one here, so the operator's real
    # ``~/.kiro/skills`` is never read.
    kiro = tmp_path / "kiro"
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *_a, **_k: None)
    for i in range(5):
        _skill(kiro / "skills", f"global-{i}")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    package = tmp_path / "package" / "skills"
    return kiro, checkout, package


def _agent(_kiro: Path, name: str, spec: dict) -> None:
    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "prompt": "p", **spec}), encoding="utf-8"
    )


async def _entry(agent: str, checkout: Path) -> dict:
    extras = await KasHarness().session_extras(agent, work_dir=str(checkout))
    [entry] = extras.custom_agents
    return entry


def _kas_catalog(kiro: Path, entry: dict) -> set[str]:
    """What KAS loads before any policy: the default tree plus the agent's resources."""
    paths = [str(kiro / "skills" / "*" / "SKILL.md")]
    paths += [r[len("skill://") :] for r in entry.get("resources", []) if r.startswith("skill://")]
    return kas_skill_scope.skill_names(paths)


def _denied(rules: list[dict], name: str) -> bool:
    for rule in rules:
        if rule.get("capability") != "skill" or rule.get("effect") != "deny":
            continue
        matched = any(fnmatchcase(name, p) for p in rule.get("match", ["*"]))
        excluded = any(fnmatchcase(name, p) for p in rule.get("exclude", []))
        if matched and not excluded:
            return True
    return False


def _kas_offers(kiro: Path, entry: dict) -> set[str]:
    """The skills KAS's skill tool offers: its catalog less every name ``skill`` denies."""
    rules = (entry.get("permissions") or {}).get("rules", [])
    return {n for n in _kas_catalog(kiro, entry) if not _denied(rules, n)}


def _v2_skills(agent: str, checkout: Path) -> set[str] | None:
    """The skills a kiro-cli session of *agent* loads under Crew (inheritance off)."""
    globs = session_skill_globs("", agent, project_dir=str(checkout), provider_type=PROVIDER_ACP)
    return None if globs is None else kas_skill_scope.skill_names(globs)


@pytest.mark.asyncio
async def test_a_mapped_agent_loads_its_own_skills_and_no_global_one(home):
    kiro, checkout, package = home
    mapped = [_skill(package, n) for n in ("alpha", "beta", "gamma")]
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{p}" for p in mapped]})

    entry = await _entry("mapped", checkout)

    assert _v2_skills("mapped", checkout) == {"alpha", "beta", "gamma"}
    assert _kas_offers(kiro, entry) == _v2_skills("mapped", checkout)
    assert len(_kas_catalog(kiro, entry)) == 8  # the spill the rule takes away


@pytest.mark.asyncio
async def test_the_exclude_list_names_the_frontmatter_name_kas_keys_on(home):
    kiro, checkout, package = home
    path = _skill(package, "dir-name", name="front-name")
    _agent(kiro, "renamed", {"tools": ["*"], "resources": [f"skill://{path}"]})

    entry = await _entry("renamed", checkout)

    assert _kas_offers(kiro, entry) == {"front-name"}


@pytest.mark.asyncio
async def test_an_inheriting_agent_keeps_the_default_trees_and_gets_no_rule(home):
    kiro, checkout, _package = home
    _agent(kiro, "plain", {"tools": ["fs_read"]})

    entry = await _entry("plain", checkout)

    assert "permissions" not in entry
    assert _kas_offers(kiro, entry) == _v2_skills("plain", checkout)
    assert len(_kas_offers(kiro, entry)) == 5


@pytest.mark.asyncio
async def test_an_agent_that_may_not_inherit_loads_no_skill(home):
    kiro, checkout, _package = home
    _agent(kiro, "zero", {"tools": []})

    entry = await _entry("zero", checkout)

    assert _v2_skills("zero", checkout) == set()
    assert _kas_offers(kiro, entry) == set()


@pytest.mark.asyncio
async def test_the_whole_catalog_worker_is_not_narrowed(home):
    kiro, checkout, _package = home
    _agent(kiro, "kirocrew-worker", {"tools": ["*"]})

    entry = await _entry("kirocrew-worker", checkout)

    assert _v2_skills("kirocrew-worker", checkout) is None
    assert "permissions" not in entry


@pytest.mark.asyncio
async def test_a_scope_past_the_exclude_bound_denies_by_name_and_keeps_parity(home):
    kiro, checkout, package = home
    count = kas_skill_scope._MAX_PATTERNS_PER_RULE + 6
    _agent(kiro, "wide", {"tools": ["*"], "resources": [f"skill://{package}/*/SKILL.md"]})
    for i in range(count):
        _skill(package, f"wide-{i:03d}")

    entry = await _entry("wide", checkout)

    rules = entry["permissions"]["rules"]
    assert all("exclude" not in r for r in rules)
    assert all(len(r.get("match", [])) <= kas_skill_scope._MAX_PATTERNS_PER_RULE for r in rules)
    assert _kas_offers(kiro, entry) == _v2_skills("wide", checkout)
    assert len(_kas_offers(kiro, entry)) == count


@pytest.mark.asyncio
async def test_a_failed_scope_costs_the_narrowing_not_the_session(home, monkeypatch):
    kiro, checkout, package = home
    path = _skill(package, "alpha")
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{path}"]})

    def _boom(*_a, **_k):
        raise RuntimeError("scope read failed")

    monkeypatch.setattr(kas_skill_scope, "scope_globs", _boom)

    entry = await _entry("mapped", checkout)

    assert entry["id"] == "mapped"
    assert "permissions" not in entry


@pytest.mark.asyncio
async def test_a_shadowing_checkout_spec_does_not_set_the_user_agents_scope(home):
    kiro, checkout, package = home
    alpha = _skill(package, "alpha")
    beta = _skill(package, "beta")
    _agent(kiro, "shared", {"tools": ["*"], "resources": [f"skill://{alpha}"]})
    project = checkout / ".kiro" / "agents"
    project.mkdir(parents=True)
    (project / "shared.json").write_text(
        json.dumps({"name": "shared", "prompt": "p", "resources": [f"skill://{beta}"]}),
        encoding="utf-8",
    )

    entry = await _entry("shared", checkout)

    # KAS runs the user-level spec, so its own skill is the one it keeps.
    assert entry["resources"] == [f"skill://{alpha}"]
    assert _kas_offers(kiro, entry) == {"alpha"}


@pytest.mark.asyncio
async def test_a_glob_named_skill_outside_a_wide_scope_never_becomes_a_pattern(home):
    kiro, checkout, package = home
    _skill(kiro / "skills", "starred", name="*")
    count = kas_skill_scope._MAX_PATTERNS_PER_RULE + 1
    _agent(kiro, "wide", {"tools": ["*"], "resources": [f"skill://{package}/*/SKILL.md"]})
    for i in range(count):
        _skill(package, f"wide-{i:03d}")

    entry = await _entry("wide", checkout)

    patterns = [p for r in entry["permissions"]["rules"] for p in r.get("match", [])]
    assert "*" not in patterns
    assert {f"wide-{i:03d}" for i in range(count)} <= _kas_offers(kiro, entry)


@pytest.mark.asyncio
async def test_every_skill_file_is_read_through_the_sensitive_path_fence(home, monkeypatch):
    import kiro_crew.hooks as hooks

    kiro, checkout, package = home
    path = _skill(package, "alpha")
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{path}"]})
    seen: list[str] = []
    real = hooks.safe_read_prefix

    def _spy(raw: str, n: int):
        seen.append(raw)
        return real(raw, n)

    monkeypatch.setattr(hooks, "safe_read_prefix", _spy)

    await _entry("mapped", checkout)

    assert str(path) in seen


def test_a_refused_skill_file_yields_no_name(tmp_path, monkeypatch):
    import kiro_crew.hooks as hooks

    path = _skill(tmp_path, "secret")
    monkeypatch.setattr(hooks, "safe_read_prefix", lambda _raw, _n: None)

    assert kas_skill_scope.skill_names([str(path)]) == set()


@pytest.mark.asyncio
async def test_a_skill_scope_pin_left_on_the_key_does_not_set_the_new_sessions_scope(home):
    from kiro_crew.skill_scope_pin import pin_session_skill_spec

    kiro, checkout, package = home
    alpha = _skill(package, "alpha")
    beta = _skill(package, "beta")
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{alpha}"]})
    app_spec = kiro / "app-spec.json"
    app_spec.write_text(
        json.dumps({"name": "app", "prompt": "p", "resources": [f"skill://{beta}"]}),
        encoding="utf-8",
    )
    # The pin an earlier app-agent turn on this key left; the gateway replaces it
    # only once the next turn holds the session, after the session is built.
    pin_session_skill_spec("chat-1", app_spec)
    try:
        extras = await KasHarness().session_extras(
            "mapped", work_dir=str(checkout), session_key="chat-1"
        )
    finally:
        pin_session_skill_spec("chat-1", None)

    [entry] = extras.custom_agents
    assert _kas_offers(kiro, entry) == {"alpha"}


@pytest.mark.asyncio
async def test_the_skill_scope_denies_are_recorded_in_the_security_event_log(home, monkeypatch):
    import types

    from kiro_crew.acp import kas_agents

    kiro, checkout, package = home
    path = _skill(package, "alpha")
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{path}"]})
    events: list[dict] = []
    monkeypatch.setattr(
        kas_agents,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )

    await _entry("mapped", checkout)

    [event] = [e for e in events if e["operation"] == "kas_skill_scope_denied"]
    assert event["source"] == "kas_agent_projection"
    assert event["resources"] == "every skill denied except 1: alpha for agent mapped"


def test_a_skill_scope_record_names_what_it_takes_away():
    from kiro_crew.acp.kas_agents import _skill_scope_summary

    assert _skill_scope_summary(({"capability": "skill", "effect": "deny"},)) == (
        "every skill denied"
    )
    many = [f"s{i:02d}" for i in range(45)]
    line = _skill_scope_summary(({"capability": "skill", "match": many, "effect": "deny"},))
    assert line.startswith("45 default-tree skills denied by name: s00, ")
    assert line.endswith("and 5 more")

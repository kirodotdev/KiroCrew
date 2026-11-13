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
async def test_a_mapped_agent_keeps_a_default_tree_skill_its_own_policy_allows(home):
    # A managed agent whose file lists absolute skill:// paths and also allows, by
    # name, a skill that lives only in ~/.kiro/skills: the deny rule must not
    # override the author's allow. A pattern allow names no particular skill.
    kiro, checkout, package = home
    mapped = [_skill(package, n) for n in ("alpha", "beta")]
    allows = {"capability": "skill", "effect": "allow", "match": ["alpha", "global-1", "glob*"]}
    _agent(
        kiro,
        "managed",
        {
            "tools": ["*"],
            "resources": [f"skill://{p}" for p in mapped],
            "permissions": {"rules": [allows]},
        },
    )

    entry = await _entry("managed", checkout)

    assert _kas_offers(kiro, entry) == {"alpha", "beta", "global-1"}


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


@pytest.mark.parametrize("bom", [b"\xef\xbb\xbf", b""])
def test_a_skill_saved_with_a_bom_is_named_by_its_frontmatter(tmp_path, bom):
    path = tmp_path / "dirname" / "SKILL.md"
    path.parent.mkdir()
    path.write_bytes(bom + b"---\nname: other\ndescription: d\n---\nbody\n")

    assert kas_skill_scope.skill_names([str(path)]) == {"other"}


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


# --- Skills KAS refuses get a fixed copy (P0-26) ---------------------------------------

_KAS_NAME = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"


def _raw_skill(root: Path, dirname: str, text: str) -> Path:
    path = root / dirname / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _copies(entry: dict) -> dict[str, dict]:
    """The copies in *entry*'s resources, by name, each with its parsed front matter."""
    import re

    import yaml

    out = {}
    for uri in entry.get("resources", []):
        path = Path(uri[len("skill://") :])
        if path.parent.parent.name != kas_skill_scope.COPY_DIR_NAME:
            continue
        text = path.read_text(encoding="utf-8")
        _, head, body = text.split("---\n", 2)
        front = yaml.safe_load(head)
        # What KAS's ``validateSkillMd`` holds a skill to.
        assert re.fullmatch(_KAS_NAME, front["name"]) and "--" not in front["name"]
        assert 1 <= len(front["description"]) <= 1024
        out[front["name"]] = {**front, "body": body}
    return out


@pytest.mark.asyncio
async def test_a_default_tree_skill_kas_refuses_loads_as_a_fixed_copy(home):
    kiro, checkout, _package = home
    _raw_skill(kiro / "skills", "No_Front", "# Deploy helper\n\nRun scripts/go.sh\n")
    _raw_skill(kiro / "skills", "upper", "---\nname: My_Skill\ndescription: d\n---\nbody\n")
    _raw_skill(
        kiro / "skills",
        "colon",
        "---\nname: colon\ndescription: Three backends: a, b\n---\nbody\n",
    )
    _agent(kiro, "plain", {"tools": ["fs_read"]})

    entry = await _entry("plain", checkout)

    copies = _copies(entry)
    assert set(copies) == {"no-front", "my-skill", "colon"}
    assert copies["no-front"]["description"] == "Deploy helper"
    assert "Run scripts/go.sh" in copies["no-front"]["body"]
    assert str(kiro / "skills" / "No_Front") in copies["no-front"]["body"]
    assert copies["my-skill"]["description"] == "d"
    assert copies["colon"]["description"] == "body"
    assert "permissions" not in entry


@pytest.mark.asyncio
async def test_a_mapped_skill_kas_refuses_is_copied_and_kept_in_scope(home):
    kiro, checkout, package = home
    path = _raw_skill(package, "nodesc", "---\nname: nodesc\n---\nUse me well\n")
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{path}"]})

    entry = await _entry("mapped", checkout)

    assert set(_copies(entry)) == {"nodesc"}
    assert entry["resources"][0] == f"skill://{path}"
    rules = entry["permissions"]["rules"]
    assert not _denied(rules, "nodesc")
    assert all(_denied(rules, f"global-{i}") for i in range(5))


@pytest.mark.asyncio
async def test_a_copy_takes_a_name_no_loaded_skill_uses(home):
    kiro, checkout, _package = home
    _raw_skill(kiro / "skills", "Global_0", "---\nname: Global-0\ndescription: d\n---\nb\n")
    _agent(kiro, "plain", {"tools": ["fs_read"]})

    entry = await _entry("plain", checkout)

    assert set(_copies(entry)) == {"global-0-2"}


@pytest.mark.asyncio
async def test_a_skill_kas_loads_and_one_out_of_scope_get_no_copy(home):
    kiro, checkout, package = home
    _raw_skill(kiro / "skills", "No_Front", "no front matter\n")
    alpha = _skill(package, "alpha")
    _agent(kiro, "mapped", {"tools": ["*"], "resources": [f"skill://{alpha}"]})

    entry = await _entry("mapped", checkout)

    assert entry["resources"] == [f"skill://{alpha}"]


def test_a_long_description_is_cut_to_kas_bound(tmp_path):
    path = _raw_skill(
        tmp_path, "long", "---\nname: long\ndescription: " + "x" * 2000 + "\n---\nb\n"
    )
    front, body = kas_skill_scope._kas_front_matter(path.read_text(encoding="utf-8"))
    assert kas_skill_scope._kas_loads(front, native=False)
    assert not kas_skill_scope._kas_loads(front, native=True)
    text = kas_skill_scope._copy_text(path, front, body, "long")
    copy_front, _ = kas_skill_scope._kas_front_matter(text)
    assert kas_skill_scope._kas_loads(copy_front, native=True)
    assert len(copy_front["description"]) == 1024


def test_a_skill_opening_with_a_rule_and_no_fence_keeps_its_body():
    front, body = kas_skill_scope._kas_front_matter("---\n# Title\n\nDo the thing\n")
    assert front is None
    assert "Do the thing" in body


@pytest.mark.parametrize(
    "extra", ["license: null", "compatibility: 1.0", "metadata: []", "metadata: x"]
)
def test_a_field_kas_schema_rejects_counts_as_refused(extra):
    front, _ = kas_skill_scope._kas_front_matter(f"---\nname: a\ndescription: d\n{extra}\n---\nb\n")
    assert not kas_skill_scope._kas_loads(front, native=False)


@pytest.mark.asyncio
async def test_two_names_for_one_skill_never_share_a_copy_file(home):
    kiro, checkout, package = home
    refused = _raw_skill(kiro / "skills", "Deploy", "no front matter\n")
    taken = _skill(package, "deploy")
    _agent(kiro, "plain", {"tools": ["fs_read"]})
    _agent(kiro, "both", {"tools": ["*"], "resources": [f"skill://{taken}", f"skill://{refused}"]})

    plain = await _entry("plain", checkout)
    both = await _entry("both", checkout)

    [plain_uri] = [r for r in plain["resources"] if kas_skill_scope.COPY_DIR_NAME in r]
    [both_uri] = [r for r in both["resources"] if kas_skill_scope.COPY_DIR_NAME in r]
    assert set(_copies(plain)) == {"deploy"}
    assert set(_copies(both)) == {"deploy-2"}
    assert plain_uri != both_uri


@pytest.mark.parametrize(
    "head",
    [
        "name: a\ndescription: d\ndescription: e",
        "name: a\ndescription: d\nlicense: 1e3",
    ],
)
def test_front_matter_js_yaml_refuses_counts_as_refused(head):
    front, _ = kas_skill_scope._kas_front_matter(f"---\n{head}\n---\nb\n")
    assert not kas_skill_scope._kas_loads(front, native=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["deny", "ask"])
async def test_a_skill_the_author_restricts_by_its_own_name_gets_no_renamed_copy(home, effect):
    kiro, checkout, _package = home
    _raw_skill(kiro / "skills", "upper", "---\nname: My_Skill\ndescription: d\n---\nbody\n")
    _raw_skill(kiro / "skills", "Other_One", "---\nname: Other_One\ndescription: d\n---\nb\n")
    rule = {"capability": "skill", "match": ["My_Skill"], "effect": effect}
    _agent(kiro, "plain", {"tools": ["fs_read"], "permissions": {"rules": [rule]}})

    entry = await _entry("plain", checkout)

    # The rule names ``My_Skill``; its copy would be ``my-skill``, which it misses.
    assert set(_copies(entry)) == {"other-one"}


@pytest.mark.asyncio
async def test_a_number_like_description_stays_a_string_in_the_copy(home):
    kiro, checkout, _package = home
    _raw_skill(kiro / "skills", "Example", '---\nname: Example\ndescription: "1e3"\n---\nb\n')
    _agent(kiro, "plain", {"tools": ["fs_read"]})

    entry = await _entry("plain", checkout)

    [uri] = [r for r in entry["resources"] if kas_skill_scope.COPY_DIR_NAME in r]
    text = Path(uri[len("skill://") :]).read_text(encoding="utf-8")
    front, _ = kas_skill_scope._kas_front_matter(text)
    assert front == {"name": "example", "description": "1e3"}
    assert kas_skill_scope._kas_loads(front, native=True)


@pytest.mark.parametrize(
    ("name", "valid"),
    [("a", True), ("a-b-9", True), ("A", False), ("a_b", False), ("-a", False), ("a-", False)]
    + [("a--b", False), ("", False), ("a" * 64, True), ("a" * 65, False)],
)
def test_the_name_rule_is_kas_is_valid_skill_name(name, valid):
    assert kas_skill_scope._valid_kas_name(name) is valid


@pytest.mark.parametrize(
    ("value", "number"),
    [("1e3", True), (".5", True), ("5.", True), ("+1.5E-3", True), ("e3", False)]
    + [("1e", False), (".", False), ("1.2.3", False), ("-", False), ("1e3x", False)],
)
def test_the_yaml_1_2_float_form(value, number):
    assert kas_skill_scope._Yaml12Float().match(value) is number


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("front", "rule"),
    [
        # Front matter KAS refuses still names the skill for kiro-cli's rules.
        ("name: My_Skill\ndescription: d\nlicense: null", {"match": ["My_Skill"]}),
        # ``?`` is literal to KAS, so this exclude does not lift the deny.
        ("name: My_Skill\ndescription: d", {"match": ["My_Skill"], "exclude": ["My_?kill"]}),
        # An empty ``match`` is every skill to KAS; only the copy's name is excluded.
        ("name: My_Skill\ndescription: d", {"match": [], "exclude": ["my-skill"]}),
    ],
)
async def test_a_restricted_skill_gets_no_copy_however_its_rule_reads(home, front, rule):
    kiro, checkout, _package = home
    _raw_skill(kiro / "skills", "upper", f"---\n{front}\n---\nbody\n")
    rule = {"capability": "skill", "effect": "deny", **rule}
    _agent(kiro, "plain", {"tools": ["fs_read"], "permissions": {"rules": [rule]}})

    entry = await _entry("plain", checkout)

    assert _copies(entry) == {}


@pytest.mark.parametrize(
    ("text", "pattern", "hit"),
    [
        ("My_Skill", "My_Skill", True),
        ("My_Skill", "My_?kill", False),
        ("My_Skill", "My*", True),
        ("My_Skill", "*Skill", True),
        ("My_Skill", "M*_*l", True),
        ("My_Skill", "[M]y_Skill", False),
        ("ab", "a*b*", True),
        ("a", "a*a", False),
    ],
)
def test_skill_patterns_match_as_cedar_like(text, pattern, hit):
    assert kas_skill_scope._cedar_like(text, pattern) is hit


@pytest.mark.asyncio
async def test_a_link_planted_at_a_copy_folder_is_never_written_through(home, tmp_path):
    import hashlib

    kiro, checkout, _package = home
    path = _raw_skill(kiro / "skills", "No_Front", "no front matter\n")
    _agent(kiro, "plain", {"tools": ["fs_read"]})
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    key = f"{path.resolve()}\0no-front"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    root = paths_mod.data_home() / kas_skill_scope.COPY_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    (root / digest).symlink_to(sealed, target_is_directory=True)

    entry = await _entry("plain", checkout)

    assert list(sealed.iterdir()) == []
    assert _copies(entry) == {}


@pytest.mark.asyncio
async def test_a_skill_linked_to_a_prompt_only_secret_is_never_copied(home, tmp_path, monkeypatch):
    kiro, checkout, _package = home
    fake_home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    secret = fake_home / ".config" / "gh" / "hosts.yml"
    secret.parent.mkdir(parents=True)
    secret.write_text("oauth_token: gho_not_a_real_token\n", encoding="utf-8")
    link = kiro / "skills" / "gh_link" / "SKILL.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)
    _raw_skill(kiro / "skills", "No_Front", "no front matter\n")
    _agent(kiro, "plain", {"tools": ["fs_read"]})

    entry = await _entry("plain", checkout)

    copies = _copies(entry)
    assert "no-front" in copies
    assert "gh-link" not in copies
    root = paths_mod.data_home() / kas_skill_scope.COPY_DIR_NAME
    written = [p.read_text(encoding="utf-8") for p in root.rglob("SKILL.md")]
    assert written and not any("gho_not_a_real_token" in text for text in written)


def test_skill_files_past_the_read_bound_are_counted_once(tmp_path, monkeypatch, caplog):
    import logging

    for i in range(5):
        _skill(tmp_path, f"s{i}")
    monkeypatch.setattr(kas_skill_scope, "_MAX_SKILL_FILES", 3)

    with caplog.at_level(logging.WARNING, logger=kas_skill_scope.logger.name):
        files = kas_skill_scope._matched_files([str(tmp_path / "*" / "SKILL.md")])

    assert len(files) == 3
    [record] = [r for r in caplog.records if "get no copy" in r.getMessage()]
    assert record.getMessage().startswith("KAS skill copies: 2 skill files")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["", "json"])
async def test_one_deeply_nested_skill_does_not_cost_the_other_copies(home, language):
    kiro, checkout, _package = home
    depth = 5000
    if language:
        nested = (
            '{"name": "deep", "description": "d", "metadata": ' + "[" * depth + "]" * depth + "}"
        )
    else:
        nested = "name: deep\ndescription: d\nmetadata: " + "[" * depth + "]" * depth
    _raw_skill(kiro / "skills", "deep", f"---{language}\n{nested}\n---\nb\n")
    _raw_skill(kiro / "skills", "No_Front", "no front matter\n")
    _agent(kiro, "plain", {"tools": ["fs_read"]})

    entry = await _entry("plain", checkout)

    assert "no-front" in _copies(entry)

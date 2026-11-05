# SPDX-License-Identifier: Apache-2.0
"""A crewmate's spec policy: its template's hooks, permissions and tool settings.

Adding an agent's abilities copies its ``hooks``, ``permissions`` and
``toolsSettings`` into the crewmate's file, exactly or not at all, and the
crewmate's worker runs under them on KAS; the crewmate's chat keeps the stock
conductor spec (owner decision, ``docs/decisions``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import mate_capabilities, mate_spec_policy, mate_worker, mate_worker_kit
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.config import paths
from kiro_crew.mate_spec_policy import SpecPolicyUncarried
from kiro_crew.mate_worker_kit import WorkerKitError

HOOK = {"matcher": "execute_bash", "command": "audit-shell.sh"}
BASE_HOOK = {"command": "crew-base.sh"}
DENY = {"capability": "filesystem", "match": ["/etc/**"], "effect": "deny"}
# Scoped with an ``exclude``: an ``allowedTools`` list cannot say this, so KAS keeps it.
ALLOW = {
    "capability": "mcp",
    "match": ["jira/search"],
    "exclude": ["jira/delete_issue"],
    "effect": "allow",
}
SETTINGS = {"execute_bash": {"allowedCommands": ["git status"], "deniedCommands": ["rm .*"]}}

WORKER_BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "base",
    "tools": ["*"],
    "allowedTools": ["fs_read"],
    "mcpServers": {},
    "hooks": {"stop": [BASE_HOOK]},
}
WORKER = "kirocrew-mate-worker"
CONDUCTOR = "kirocrew-mate-conductor"
TEMPLATE = {
    "name": "release-bot",
    "prompt": "Release SOP.",
    "tools": ["*"],
    "mcpServers": {},
    "hooks": {"preToolUse": [HOOK]},
    "permissions": {"rules": [DENY, ALLOW]},
    "toolsSettings": SETTINGS,
}


@pytest.fixture(autouse=True)
def _no_ceiling(_floor_monkeypatch):
    """The governance ceiling is its own module's concern; here it passes grants."""
    from kiro_crew.platform import governance

    _floor_monkeypatch.setattr(
        governance, "sanitize_agent_config_governance", lambda spec, **_k: None
    )


# --- layering, exact or refused -------------------------------------------------


def test_a_template_hook_is_added_beside_the_bases_own():
    spec = json.loads(json.dumps(WORKER_BASE))
    mate_spec_policy.layer_policy(spec, {"hooks": TEMPLATE["hooks"]})
    assert spec["hooks"] == {"stop": [BASE_HOOK], "preToolUse": [HOOK]}


def test_template_permission_rules_join_the_bases_rules_unchanged():
    spec = {"permissions": {"rules": [{"capability": "builtin", "effect": "ask"}]}}
    mate_spec_policy.layer_policy(spec, {"permissions": TEMPLATE["permissions"]})
    assert spec["permissions"]["rules"] == [
        {"capability": "builtin", "effect": "ask"},
        DENY,
        ALLOW,
    ]


def test_a_tool_setting_is_copied_whole_for_a_tool_the_base_leaves_alone():
    spec = {"toolsSettings": {"fs_write": {"deniedPaths": ["/etc"]}}}
    mate_spec_policy.layer_policy(spec, {"toolsSettings": SETTINGS})
    assert spec["toolsSettings"] == {"fs_write": {"deniedPaths": ["/etc"]}, **SETTINGS}


def test_a_tool_setting_the_base_sets_differently_is_refused_by_name():
    spec = {"toolsSettings": {"execute_bash": {"deniedCommands": ["sudo .*"]}}}
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy(spec, {"toolsSettings": SETTINGS})
    assert caught.value.fields == ["toolsSettings.execute_bash"]
    assert isinstance(caught.value, mate_worker.WorkerKitUnreadable)


def test_a_permissions_block_kas_would_refuse_whole_is_refused_by_name():
    """A block with a key KAS cannot read would travel as no rule at all."""
    bad = {"rules": [{"effect": "deny", "tools": ["@jira/delete_issue"]}]}
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy({}, {"permissions": bad})
    assert caught.value.fields == ["permissions"]


# --- the worker ------------------------------------------------------------------


def test_the_worker_carries_the_files_hooks_permissions_and_tool_settings():
    merged = mate_worker.merge_worker_spec(
        WORKER_BASE, mate_worker.worker_additions(WORKER_BASE, TEMPLATE)
    )
    assert merged["hooks"] == {"stop": [BASE_HOOK], "preToolUse": [HOOK]}
    assert merged["permissions"]["rules"] == [DENY, ALLOW]
    assert merged["toolsSettings"] == SETTINGS


def test_the_worker_gains_no_permission_the_template_does_not_declare():
    merged = mate_worker.merge_worker_spec(
        WORKER_BASE, mate_worker.worker_additions(WORKER_BASE, TEMPLATE)
    )
    allows = [r for r in merged["permissions"]["rules"] if r["effect"] == "allow"]
    assert allows == [ALLOW]
    assert merged["allowedTools"] == WORKER_BASE["allowedTools"]


def test_a_host_that_applies_no_policy_refuses_a_worker_whose_file_sets_one(monkeypatch, tmp_path):
    (tmp_path / "pixel.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_worker.resolve_worker_spec(dict(WORKER_BASE), "m-1", refuse_spec_policy=True)
    assert caught.value.fields == ["hooks", "permissions", "toolsSettings"]


# --- the conductor ---------------------------------------------------------------
def test_only_kas_applies_a_crewmates_policy():
    policy = {"hooks": {}}
    assert mate_spec_policy.backend_refusal(policy, ACP_BACKEND_KAS) is None
    assert "claude" in (mate_spec_policy.backend_refusal(policy, "claude") or "")
    assert mate_spec_policy.backend_refusal({}, "claude") is None


def _worker_session(monkeypatch, tmp_path, file_spec: dict = TEMPLATE) -> None:
    """A crewmate ``m-1`` on its own file ``pixel``, whose worker session is ``worker-1``."""
    from kiro_crew import execution_context

    (tmp_path / "pixel.json").write_text(json.dumps(file_spec), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
    record = SimpleNamespace(selection_kind="template", template_id=WORKER, member_id="m-1")
    monkeypatch.setattr(execution_context, "read_session_execution", lambda key: record)


def test_the_session_mcp_read_refuses_off_kas_and_layers_on_kas(monkeypatch, tmp_path):
    from kiro_crew.acp.session_mcp_crewmate import _crewmate_worker_spec

    _worker_session(monkeypatch, tmp_path)
    with pytest.raises(SpecPolicyUncarried) as caught:
        _crewmate_worker_spec(WORKER, dict(WORKER_BASE), "worker-1")
    assert caught.value.fields == ["hooks", "permissions", "toolsSettings"]
    spec = _crewmate_worker_spec(WORKER, dict(WORKER_BASE), "worker-1", spec_policy_honoured=True)
    assert spec is not None
    assert spec["hooks"] == {"stop": [BASE_HOOK], "preToolUse": [HOOK]}
    assert spec["permissions"]["rules"] == [DENY, ALLOW]
    assert spec["toolsSettings"] == SETTINGS


def test_crew_fired_hooks_read_the_worker_sessions_merged_spec(monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks

    merged = {**WORKER_BASE, "hooks": {"preToolUse": [HOOK]}}
    monkeypatch.setattr(
        mate_worker, "mate_service_scope_spec", lambda agent, key: (merged, paths.mate_agents_dir())
    )
    spec, origin = spec_hooks._agent_spec_with_origin(WORKER, None, "worker-1")
    assert spec["hooks"] == {"preToolUse": [HOOK]}
    assert origin == "project"


# --- the crewmate's chat runs the stock conductor --------------------------------


@pytest.fixture
def stock_conductor(tmp_path, monkeypatch):
    """The stock conductor exactly as the installer writes it, beside a crewmate
    file that sets hooks, permissions and tool settings."""
    import kiro_crew.config.paths as paths_mod
    from kiro_crew import agent, execution_context
    from kiro_crew.agent_materialization import service_agents

    kiro = tmp_path / "kiro"
    user = kiro / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: user)
    service_agents._install_mate_conductor_agent()
    mates = kiro / "mates" / "agents"
    stock_text = (mates / f"{CONDUCTOR}.json").read_text(encoding="utf-8")
    (mates / "pixel.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
    chat = SimpleNamespace(selection_kind="member", template_id="", member_id="m-1")
    monkeypatch.setattr(execution_context, "read_session_execution", lambda key: chat)
    return json.loads(stock_text)


def test_a_crewmates_chat_runs_the_stock_conductor_whatever_its_file_sets(stock_conductor):
    """Nothing from the crewmate's file reaches its chat: hooks, permissions and
    tool settings stay on the worker."""
    from kiro_crew.acp.session_mcp_crewmate import _crewmate_worker_spec
    from kiro_crew.agent_sdk import spec_hooks

    assert mate_worker.MATE_CONDUCTOR_AGENT == CONDUCTOR
    scoped = mate_worker.mate_service_scope_spec(CONDUCTOR, "chat-1")
    assert scoped is not None
    assert scoped[0] == stock_conductor
    for honoured in (True, False):
        spec = _crewmate_worker_spec(
            CONDUCTOR, dict(stock_conductor), "chat-1", spec_policy_honoured=honoured
        )
        assert spec == stock_conductor
    spec_hooks._cache.clear()
    try:
        fired = spec_hooks._agent_spec(CONDUCTOR, None, "chat-1")
    finally:
        spec_hooks._cache.clear()
    assert fired == stock_conductor
    for key in mate_spec_policy.POLICY_KEYS:
        assert stock_conductor.get(key) != TEMPLATE[key]


# --- the kit copy ----------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (user / "release-bot.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(WORKER_BASE), encoding="utf-8")
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    monkeypatch.setattr(mate_worker_kit, "_crewmate_backend", lambda member, held: ACP_BACKEND_KAS)
    return user, mates


def _file(mates) -> dict:
    return json.loads((mates / "pixel.json").read_text(encoding="utf-8"))


def _use(user, template: dict) -> None:
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")


def test_adding_a_template_copies_its_policy_and_previews_each_item(home):
    _, mates = home
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    rows = {(r["section"], r["id"]) for r in preview["changes"]}
    labels = {(section, ident.rsplit(" (", 1)[0]) for section, ident in rows}
    assert ("hooks", "preToolUse: execute_bash") in labels
    assert ("permissions", "deny filesystem /etc/**") in labels
    assert ("permissions", "allow mcp jira/search") in labels
    assert ("toolsSettings", "execute_bash") in rows
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    written = _file(mates)
    assert written["hooks"] == TEMPLATE["hooks"]
    assert written["permissions"] == TEMPLATE["permissions"]
    assert written["toolsSettings"] == SETTINGS


def test_a_template_that_denies_a_hand_off_tool_is_still_added(home):
    """The chat takes nothing from the file, so a rule naming a conductor tool
    narrows only the worker and is carried like any other."""
    user, mates = home
    deny = {"capability": "mcp", "match": ["kirocrew-dashboard/session_send"], "effect": "deny"}
    _use(user, {**TEMPLATE, "permissions": {"rules": [deny]}})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    assert _file(mates)["permissions"] == {"rules": [deny]}


def test_a_crewmate_off_kas_is_refused_a_template_with_a_policy(home, monkeypatch):
    _, mates = home
    before = _file(mates)
    monkeypatch.setattr(mate_worker_kit, "_crewmate_backend", lambda member, held: "claude")
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_policy_backend_unsupported"
    assert caught.value.detail == "hooks, permissions, toolsSettings"
    assert _file(mates) == before


def test_a_template_setting_the_worker_base_sets_differently_is_refused_by_name(home):
    user, mates = home
    base = {**WORKER_BASE, "toolsSettings": {"execute_bash": {"deniedCommands": ["sudo .*"]}}}
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_restrictions_uncarried"
    assert caught.value.detail == "toolsSettings.execute_bash"


def test_an_entry_sharing_a_label_with_one_already_held_still_gets_a_row(home):
    """Each hook and rule the add writes is shown, beside one with the same label."""
    user, mates = home
    held_hook = {"matcher": "execute_bash", "command": "already.sh"}
    held_rule = {**DENY, "exclude": ["/etc/hosts"]}
    mate_file = {
        "name": "pixel",
        "prompt": "",
        "tools": [],
        "mcpServers": {},
        "hooks": {"preToolUse": [held_hook]},
        "permissions": {"rules": [held_rule]},
    }
    (mates / "pixel.json").write_text(json.dumps(mate_file), encoding="utf-8")
    _use(user, {**TEMPLATE, "permissions": {"rules": [DENY]}, "toolsSettings": {}})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    rows = [(r["section"], r["id"]) for r in preview["changes"]]
    hook_rows = [ident for section, ident in rows if section == "hooks"]
    rule_rows = [ident for section, ident in rows if section == "permissions"]
    assert len(hook_rows) == 1 and hook_rows[0].startswith("preToolUse: execute_bash (")
    assert len(rule_rows) == 1 and rule_rows[0].startswith("deny filesystem /etc/** (")


# --- where Crew fires a crewmate worker's hooks on KAS ---------------------------


def _kas_client() -> SimpleNamespace:
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    return SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")


def test_a_kas_worker_turn_fires_the_hooks_of_the_session_it_holds(monkeypatch):
    import asyncio

    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.dashboard import chat_runner

    asked: list[str] = []

    def scoped(agent, key):
        asked.append(key)
        return {**WORKER_BASE, "hooks": {"preToolUse": [HOOK]}}, paths.mate_agents_dir()

    monkeypatch.setattr(mate_worker, "mate_service_scope_spec", scoped)
    spec_hooks._cache.clear()
    slot = SimpleNamespace(_active_turn_session_key="worker-1")
    try:
        hooks, unreadable, _cwd = asyncio.run(
            chat_runner._prepare_spec_hooks(
                SimpleNamespace(), slot, _kas_client(), WORKER, is_new=False
            )
        )
    finally:
        spec_hooks._cache.clear()
    assert asked == ["worker-1"] and not unreadable
    assert [h.command for h in hooks] == [HOOK["command"]]


def test_a_stale_check_reads_the_providers_own_session(monkeypatch):
    import asyncio

    from kiro_crew.acp import kas_agents
    from kiro_crew.agent_sdk import spec_hooks

    asked: list[str] = []

    def spec(agent_id, project_dir=None, session_key=""):
        asked.append(session_key)
        return {"hooks": {"preToolUse": [HOOK]}}

    monkeypatch.setattr(spec_hooks, "_agent_spec", spec)
    monkeypatch.setattr(kas_agents, "pre_tool_hook_matchers", lambda agent_id, s: ("execute_bash",))
    provider = SimpleNamespace(
        **vars(_kas_client()),
        _session_key="worker-1",
        kas_auto_approved_capabilities=frozenset({"shell"}),
        kas_projected_agent=WORKER,
    )
    asyncio.run(spec_hooks.hook_projection_stale(provider, WORKER))
    assert asked == ["worker-1"]


def test_a_subagent_worker_turn_fires_the_hooks_of_its_own_session(monkeypatch):
    """``spawn_run(crew=...)`` runs the worker as a sub-agent turn: it reads the
    same merged spec the KAS projection was built from."""
    import asyncio

    from kiro_crew.agent_sdk import spec_hooks

    asked: list[str] = []

    def scoped(agent, key):
        asked.append(key)
        return {**WORKER_BASE, "hooks": {"preToolUse": [HOOK]}}, paths.mate_agents_dir()

    monkeypatch.setattr(mate_worker, "mate_service_scope_spec", scoped)
    provider = SimpleNamespace(**vars(_kas_client()), _session_key="worker-1")
    spec_hooks._cache.clear()
    try:
        gated = asyncio.run(spec_hooks.turn_spec_hooks(provider, WORKER))
    finally:
        spec_hooks._cache.clear()
    assert asked == ["worker-1"]
    assert HOOK["command"] in [h.command for h in gated.hooks]


# --- a restriction KAS cannot enforce ---------------------------------------------


_UNCARRIED_SETTINGS = {
    "a_tool_restriction_kas_cannot_enforce_is_refused_by_name": [
        (
            {"execute_bash": {"deniedCommands": ["git push (origin|upstream) .*"]}},
            "toolsSettings.execute_bash.deniedCommands",
        ),
        (
            {"web_fetch": {"blocked": ["https://example.com/private/.*"]}},
            "toolsSettings.web_fetch.blocked",
        ),
    ],
    "a_tools_setting_outside_the_allowlist_is_refused_by_name": [
        ({"fs_write": {"allowedPaths": ["/srv/**"]}}, "toolsSettings.fs_write.allowedPaths"),
        ({"fs_write": {"fallbackAction": "deny"}}, "toolsSettings.fs_write.fallbackAction"),
        ({"execute_bash": {"denyByDefault": True}}, "toolsSettings.execute_bash.denyByDefault"),
        ({"web_fetch": {"trusted": ["docs\\.example\\.com"]}}, "toolsSettings.web_fetch.trusted"),
        ({"subagent": {"availableAgents": []}}, "toolsSettings.subagent.availableAgents"),
        (
            {"use_subagent": {"availableAgents": ["reviewer"]}},
            "toolsSettings.use_subagent.availableAgents",
        ),
        ({"subagent": {"trustedAgents": ["reviewer"]}}, "toolsSettings.subagent.trustedAgents"),
        ({"use_aws": {"allowedServices": ["s3"]}}, "toolsSettings.use_aws.allowedServices"),
        ({"use_aws": {}}, "toolsSettings.use_aws"),
        ({"execute_bash": "deny"}, "toolsSettings.execute_bash"),
    ],
}


@pytest.mark.parametrize(
    ("settings", "named"),
    [
        pytest.param(settings, named, id=f"{name}-{named}")
        for name, cases in _UNCARRIED_SETTINGS.items()
        for settings, named in cases
    ],
)
def test_a_tools_setting_crew_cannot_carry_is_refused_by_name(settings, named):
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy({}, {"toolsSettings": settings})
    assert named in caught.value.fields


def test_the_kit_copy_refuses_a_template_whose_deny_kas_cannot_enforce(home):
    user, mates = home
    before = _file(mates)
    bad = {"execute_bash": {"deniedCommands": ["git push (origin|upstream) .*"]}}
    _use(user, {**TEMPLATE, "toolsSettings": bad})
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_restrictions_uncarried"
    assert "toolsSettings.execute_bash.deniedCommands" in caught.value.detail
    assert _file(mates) == before


@pytest.mark.parametrize(
    ("held", "added"),
    [
        (
            {"execute_bash": {"denyByDefault": True, "allowedCommands": ["git status"]}},
            {"shell": {"allowedCommands": ["touch report.txt"]}},
        ),
        (
            {"fs_write": {"deniedPaths": ["/srv/secret"]}},
            {"write": {"deniedPaths": ["/etc"]}},
        ),
    ],
)
def test_a_tool_setting_under_another_spelling_is_refused_by_name(held, added):
    """The gate unions ``shell`` with ``execute_bash``: a second spelling would
    widen the first one's limits."""
    spec = {"toolsSettings": held}
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy(spec, {"toolsSettings": added})
    assert caught.value.fields == [f"toolsSettings.{next(iter(added))}"]


def test_a_hook_past_the_runtime_cap_is_refused():
    """Crew fires at most this many spec hooks; one layered past it would never run."""
    from kiro_crew.agent_sdk.spec_hooks import _MAX_SPEC_HOOKS

    held = [{"matcher": "execute_bash", "command": f"held-{i}.sh"} for i in range(_MAX_SPEC_HOOKS)]
    spec = {"hooks": {"preToolUse": held}}
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy(spec, {"hooks": {"preToolUse": [HOOK]}})
    assert caught.value.fields == ["hooks"]
    one_short = {"hooks": {"preToolUse": held[1:]}}
    mate_spec_policy.layer_policy(one_short, {"hooks": {"preToolUse": [HOOK]}})
    assert len(one_short["hooks"]["preToolUse"]) == _MAX_SPEC_HOOKS


# --- the toolsSettings allowlist ---------------------------------------------------


def _deny_rules(settings: dict) -> list[dict]:
    from kiro_crew.acp.kas_permissions import tools_settings_to_permissions

    block, lost = tools_settings_to_permissions(settings)
    assert lost == []
    return [r for r in (block or {}).get("rules", []) if r.get("effect") == "deny"]


@pytest.mark.parametrize(
    ("settings", "capability"),
    [
        ({"execute_bash": {"deniedCommands": ["rm .*"]}}, "shell"),
        ({"fs_read": {"deniedPaths": ["/etc/shadow"]}}, "fs_read"),
        ({"fs_write": {"deniedPaths": ["/etc"]}}, "fs_write"),
        ({"web_fetch": {"blocked": ["evil\\.example\\.com"]}}, "web_fetch"),
    ],
)
def test_each_carried_deny_reaches_the_workers_kas_rules(settings, capability):
    """A listed deny lands in the worker spec and KAS's translation makes it a rule."""
    spec: dict = {}
    mate_spec_policy.layer_policy(spec, {"toolsSettings": settings})
    assert spec["toolsSettings"] == settings
    assert any(r["capability"] == capability for r in _deny_rules(spec["toolsSettings"]))


def test_a_carried_allowed_command_is_answered_by_crews_gate():
    from kiro_crew.acp.kas_permissions import crew_answered_commands

    settings = {"execute_bash": {"allowedCommands": ["git status"]}}
    spec: dict = {}
    mate_spec_policy.layer_policy(spec, {"toolsSettings": settings})
    assert crew_answered_commands(spec["toolsSettings"]) == ["git status"]


def test_an_allowed_command_crews_gate_cannot_answer_is_refused():
    """A listed grant must take effect too: an unusable entry is refused."""
    settings = {"execute_bash": {"allowedCommands": ["make -j\\d+"]}}
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy({}, {"toolsSettings": settings})
    assert "toolsSettings.execute_bash.allowedCommands" in caught.value.fields


def test_a_dedicated_worker_provider_fires_its_sessions_hooks(monkeypatch):
    """``spawn_run(keep=True)`` hands the turn an ``AcpProvider``, whose session
    key sits on its ``_client``."""
    import asyncio

    from kiro_crew.agent_sdk import spec_hooks

    asked: list[str] = []

    def scoped(agent, key):
        asked.append(key)
        return {**WORKER_BASE, "hooks": {"preToolUse": [HOOK]}}, paths.mate_agents_dir()

    monkeypatch.setattr(mate_worker, "mate_service_scope_spec", scoped)
    provider = SimpleNamespace(**vars(_kas_client()), _client=SimpleNamespace(_session_key="w-2"))
    spec_hooks._cache.clear()
    try:
        gated = asyncio.run(spec_hooks.turn_spec_hooks(provider, WORKER))
    finally:
        spec_hooks._cache.clear()
    assert asked == ["w-2"]
    assert HOOK["command"] in [h.command for h in gated.hooks]


# --- only what the template adds is carried ---------------------------------------


def test_hooks_the_worker_base_already_holds_are_not_copied(home):
    """Crew's own hooks and the user's reach the worker through its base; the add
    writes only the template's own."""
    user, mates = home
    _use(user, {**TEMPLATE, "hooks": {"stop": [BASE_HOOK], "preToolUse": [HOOK]}})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    hook_rows = [r["id"] for r in preview["changes"] if r["section"] == "hooks"]
    assert len(hook_rows) == 1 and hook_rows[0].startswith("preToolUse: execute_bash (")
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    assert _file(mates)["hooks"] == {"preToolUse": [HOOK]}


def test_a_base_event_holding_one_bare_hook_still_holds_it():
    """A base event set to one hook, not a list of them, still holds that hook."""
    base = {"hooks": {"stop": BASE_HOOK}}
    policy = {"hooks": {"stop": [BASE_HOOK, HOOK]}}
    assert mate_spec_policy.without_held(policy, base) == {"hooks": {"stop": [HOOK]}}


def test_a_template_holding_only_the_bases_hooks_is_added_off_kas(home, monkeypatch):
    """Nothing of its own to carry, so a non-KAS worker is not refused for it."""
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_crewmate_backend", lambda member, held: "claude")
    only_base = {k: v for k, v in TEMPLATE.items() if k not in ("permissions", "toolsSettings")}
    _use(user, {**only_base, "hooks": {"stop": [BASE_HOOK]}})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    assert "hooks" not in _file(mates)


# --- an allow KAS would drop beside allowedTools ------------------------------------


def test_a_builtin_allow_the_workers_allowed_tools_would_override_is_refused():
    """KAS's projection drops an allow an ``allowedTools`` list could express."""
    rule = {"capability": "web_fetch", "effect": "allow"}
    spec = {"allowedTools": ["fs_read"]}
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy(spec, {"permissions": {"rules": [DENY, rule]}}, worker=True)
    assert caught.value.fields == ["permissions rule 1 (allow web_fetch)"]


@pytest.mark.parametrize(
    ("match", "refs"),
    [
        (["jira/*"], ["@jira"]),
        (["jira/search"], ["@jira/search"]),
        (["jira/search", "notes/*"], ["@jira/search", "@notes"]),
    ],
)
def test_an_mcp_allow_the_workers_list_lacks_is_carried_as_its_refs(match, refs):
    """The allow lands as exactly the refs KAS derives it back from, no wider."""
    from kiro_crew.agent_sdk.kas_tool_view import allow_granted_by_spec

    rule = {"capability": "mcp", "match": match, "effect": "allow"}
    spec = {"allowedTools": ["fs_read"]}
    mate_spec_policy.layer_policy(spec, {"permissions": {"rules": [rule]}}, worker=True)
    assert spec["allowedTools"] == ["fs_read", *refs]
    assert allow_granted_by_spec(rule, spec)


def test_an_mcp_allow_whose_refs_cannot_grant_it_is_still_refused():
    """A bracket glob has no ``allowedTools`` ref, so nothing is written."""
    rule = {"capability": "mcp", "match": ["jira/search", "jira/re[ad]"], "effect": "allow"}
    spec = {"allowedTools": ["fs_read"]}
    with pytest.raises(SpecPolicyUncarried):
        mate_spec_policy.layer_policy(spec, {"permissions": {"rules": [rule]}}, worker=True)
    assert spec["allowedTools"] == ["fs_read"]


def test_a_carried_allow_survives_the_workers_kas_projection():
    from kiro_crew.acp.kas_permissions import merge_user_permissions

    merged = mate_worker.merge_worker_spec(
        WORKER_BASE, mate_worker.worker_additions(WORKER_BASE, TEMPLATE)
    )
    out = merge_user_permissions(
        None,
        merged["permissions"],
        ceiling_permits=lambda ref: True,
        allowlist_present=True,
    )
    assert ALLOW in (out or {}).get("rules", [])


# --- a policy write reloads the crewmate's live workers ------------------------------


def _put(home, monkeypatch, template: dict) -> list[str]:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import member_worker_kit as route

    user, _mates = home
    _use(user, template)
    scheduled: list[str] = []

    async def _owner(request, operation):
        return None

    monkeypatch.setattr(route, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(
        "kiro_crew.dashboard.crewmate_worker_reload.schedule_reload_workers",
        lambda state, member: scheduled.append(member),
    )

    async def go():
        app = web.Application()
        app["state"] = SimpleNamespace(push_refresh=lambda *_a: None)
        for method, path in (("POST", "/preview"), ("PUT", "")):
            app.router.add_route(method, "/k/{name}" + path, route.api_member_worker_kit)
        async with TestClient(TestServer(app)) as client:
            preview = await (await client.post("/k/pixel/preview", json={"template": "x"})).json()
            body = {"template": "x", "revision": preview["revision"]}
            assert (await client.put("/k/pixel", json=body)).status == 200

    import asyncio

    asyncio.run(go())
    return scheduled


def test_adding_a_deny_reloads_the_crewmates_idle_worker(home, monkeypatch):
    """A live KAS worker keeps the batch it registered; the add reloads it onto
    the file, so its next turn runs under the new deny."""
    from kiro_crew.dashboard import crewmate_worker_reload as reload_mod

    user, _mates = home
    (user / "x.json").write_text(json.dumps({}), encoding="utf-8")
    scheduled = _put(
        home,
        monkeypatch,
        {"name": "x", "prompt": "", "tools": [], "permissions": {"rules": [DENY]}},
    )
    assert scheduled == ["pixel"]
    reloaded: list[str] = []

    async def reload_if_idle(state, slot):
        reloaded.append(slot.key)
        return reload_mod.RELOADED

    worker = SimpleNamespace(key="w1")
    monkeypatch.setattr(reload_mod, "_workers_of", lambda state, member: [worker])
    monkeypatch.setattr(reload_mod, "reload_if_idle", reload_if_idle)
    import asyncio

    out = asyncio.run(reload_mod.reload_workers(SimpleNamespace(), "pixel"))
    assert reloaded == ["w1"] and out == {"w1": reload_mod.RELOADED}


def test_an_add_without_a_policy_change_reloads_nothing(home, monkeypatch):
    """A file change that touches no hook, rule or tool setting leaves workers be."""
    _user, mates = home
    template = {
        "name": "x",
        "prompt": "Answer briefly.",
        "tools": ["@notes"],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    scheduled = _put(home, monkeypatch, template)
    assert "notes" in _file(mates)["mcpServers"]
    assert scheduled == []


def test_an_allow_the_target_already_holds_is_not_refused():
    """The worker base's own generated allow, carried again by a template, adds nothing."""
    held = {"capability": "web_fetch", "effect": "allow"}
    spec = {"allowedTools": ["fs_read"], "permissions": {"rules": [held]}}
    mate_spec_policy.layer_policy(spec, {"permissions": {"rules": [held]}}, worker=True)
    assert spec["permissions"]["rules"] == [held]


def test_a_templates_generated_permissions_block_is_added():
    """A governed template's ``permissions`` block mirrors its own ``allowedTools``;
    the kit carries that list, so the worker keeps every grant."""
    generated = [
        {"capability": "mcp", "match": ["jira/search"], "effect": "allow"},
        {"capability": "web_fetch", "effect": "allow"},
    ]
    # The real worker base auto-approves ``web_fetch`` itself.
    base = {**WORKER_BASE, "allowedTools": ["fs_read", "web_fetch"]}
    source = {
        **TEMPLATE,
        "mcpServers": {"jira": {"command": "jira-mcp"}},
        "allowedTools": ["@jira/search", "web_fetch"],
        "permissions": {"rules": [DENY, *generated]},
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, source))
    assert DENY in merged["permissions"]["rules"]
    assert "@jira/search" in merged["allowedTools"]


def test_adding_a_governed_template_with_its_generated_block_succeeds(home):
    user, mates = home
    template = {
        "name": "release-bot",
        "prompt": "",
        "tools": ["*"],
        "mcpServers": {"jira": {"command": "jira-mcp"}},
        "allowedTools": ["@jira/search", "web_fetch"],
        "permissions": {
            "rules": [
                {"capability": "mcp", "match": ["jira/search"], "effect": "allow"},
                {"capability": "web_fetch", "effect": "allow"},
            ]
        },
    }
    _use(user, template)
    base = {**WORKER_BASE, "allowedTools": ["fs_read", "web_fetch"]}
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    assert "jira" in _file(mates)["mcpServers"]


def test_an_allow_a_servers_auto_approve_grants_is_not_refused():
    """KAS derives a server's governed ``autoApprove`` verbs into the same grants."""
    rule = {"capability": "mcp", "match": ["notes/read"], "effect": "allow"}
    spec = {
        "allowedTools": ["fs_read"],
        "mcpServers": {"notes": {"command": "notes-mcp", "autoApprove": ["read"]}},
    }
    mate_spec_policy.layer_policy(spec, {"permissions": {"rules": [rule]}}, worker=True)
    assert spec["permissions"]["rules"] == [rule]


@pytest.mark.parametrize("rules", [1, "deny", {"capability": "mcp"}])
def test_a_permissions_block_whose_rules_is_not_a_list_is_refused_not_crashed(rules, home):
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy({}, {"permissions": {"rules": rules}}, worker=True)
    assert caught.value.fields == ["permissions"]
    assert mate_spec_policy.without_held({"permissions": {"rules": rules}}, WORKER_BASE)
    user, _mates = home
    _use(user, {**TEMPLATE, "permissions": {"rules": rules}})
    with pytest.raises(WorkerKitError) as refused:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert refused.value.code == "template_restrictions_uncarried"


@pytest.mark.parametrize(
    ("granted", "wanted", "covered"),
    [
        ("@notes/read_?", "notes/read_*", False),
        ("@notes/read_*", "notes/read_?", False),
        ("@notes/search", "notes/search", True),
        ("@notes", "notes/read_*", True),
    ],
)
def test_an_mcp_allow_counts_as_granted_only_when_coverage_is_proved(granted, wanted, covered):
    """A grant's pattern text matching another pattern proves nothing about coverage."""
    from kiro_crew.agent_sdk.kas_tool_view import allow_granted_by_spec

    rule = {"capability": "mcp", "match": [wanted], "effect": "allow"}
    assert allow_granted_by_spec(rule, {"allowedTools": [granted]}) is covered


def test_an_allow_for_a_server_the_worker_base_also_declares_does_not_block_the_worker():
    """A crewmate file's allow for a server the worker base declares too (one from
    the user's own MCP config) starts the worker instead of refusing it, and the
    worker auto-approves no more than the file declares."""
    rule = {"capability": "mcp", "match": ["notes/*"], "effect": "allow"}
    base = {**WORKER_BASE, "mcpServers": {"notes": {"command": "notes-mcp"}}}
    mate_file = {
        **TEMPLATE,
        "mcpServers": {"notes": {"command": "notes-mcp"}},
        "allowedTools": ["@notes"],
        "permissions": {"rules": [rule]},
    }
    del mate_file["hooks"], mate_file["toolsSettings"]
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, mate_file))
    from kiro_crew.agent_sdk.kas_tool_view import allow_granted_by_spec

    assert allow_granted_by_spec(rule, merged)
    assert merged["allowedTools"] == [*WORKER_BASE["allowedTools"], "@notes"]


def test_a_tool_scoped_allow_for_a_base_declared_server_grants_only_that_tool():
    rule = {"capability": "mcp", "match": ["notes/search"], "effect": "allow"}
    base = {**WORKER_BASE, "mcpServers": {"notes": {"command": "notes-mcp"}}}
    mate_file = {
        "name": "x",
        "prompt": "",
        "mcpServers": {"notes": {"command": "notes-mcp"}},
        "permissions": {"rules": [rule]},
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, mate_file))
    assert merged["allowedTools"] == [*WORKER_BASE["allowedTools"], "@notes/search"]
    assert merged["permissions"]["rules"] == [rule]


def test_an_allow_for_a_server_the_file_adds_keeps_the_files_own_refs():
    """A server the base does not declare carries the file's list, as before."""
    rule = {"capability": "mcp", "match": ["jira/search"], "effect": "allow"}
    mate_file = {
        "name": "x",
        "prompt": "",
        "mcpServers": {"jira": {"command": "jira-mcp"}},
        "allowedTools": ["@jira/search"],
        "permissions": {"rules": [rule]},
    }
    merged = mate_worker.merge_worker_spec(
        WORKER_BASE, mate_worker.worker_additions(WORKER_BASE, mate_file)
    )
    assert merged["allowedTools"] == [*WORKER_BASE["allowedTools"], "@jira/search"]


def test_the_ceiling_still_withholds_a_carried_allow(_floor_monkeypatch):
    """The carried ref goes through the governance ceiling like any other grant:
    a ceiling that withholds the server leaves the worker no auto-approval for it."""
    from kiro_crew.acp.kas_permissions import allowed_tools_to_permissions, merge_user_permissions
    from kiro_crew.platform import governance

    def ceiling(ref: str) -> bool:
        return not ref.startswith("@notes")

    _floor_monkeypatch.setattr(governance, "may_skip_gate_now", ceiling)
    _floor_monkeypatch.setattr(
        governance,
        "sanitize_agent_config_governance",
        lambda spec, **_k: spec.update(
            allowedTools=[r for r in spec["allowedTools"] if ceiling(r)]
        ),
    )
    rule = {"capability": "mcp", "match": ["notes/*"], "effect": "allow"}
    base = {**WORKER_BASE, "mcpServers": {"notes": {"command": "notes-mcp"}}}
    mate_file = {
        "name": "x",
        "prompt": "",
        "mcpServers": {"notes": {"command": "notes-mcp"}},
        "permissions": {"rules": [rule]},
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, mate_file))
    assert merged["allowedTools"] == WORKER_BASE["allowedTools"]
    projected = merge_user_permissions(
        allowed_tools_to_permissions(merged["allowedTools"]),
        merged["permissions"],
        ceiling_permits=ceiling,
        allowlist_present=True,
    )
    mcp_allows = [
        r
        for r in (projected or {}).get("rules", [])
        if r["capability"] == "mcp" and r["effect"] == "allow"
    ]
    assert mcp_allows == []


@pytest.mark.parametrize(
    ("file_entry", "excluded"),
    [
        ({"command": "notes-mcp"}, ["@notes/search"]),
        ({"command": "notes-mcp", "disabled": True}, []),
    ],
    ids=["excluded-tool", "muted-server"],
)
def test_a_carried_allow_does_not_restore_a_grant_the_merge_took_away(file_entry, excluded):
    """An excluded tool or a muted server keeps no auto-approval, and the worker starts."""
    rule = {"capability": "mcp", "match": ["notes/search"], "effect": "allow"}
    base = {**WORKER_BASE, "mcpServers": {"notes": {"command": "notes-mcp"}}}
    mate_file = {
        "name": "x",
        "prompt": "",
        "mcpServers": {"notes": file_entry},
        "excludedTools": excluded,
        "permissions": {"rules": [rule]},
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, mate_file))
    assert merged["allowedTools"] == WORKER_BASE["allowedTools"]

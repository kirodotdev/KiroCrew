"""The ``kas_subagents`` preview: a KAS dashboard chat on the default agent uses the engine's sub-agents.

On, the default agent's projected entry mounts the engine's sub-agent tool naming
only the shipped helper, ``spawn_sub_agents`` leaves that entry, and the helper
joins the batch. Off or never set (the default), and for every other agent,
session kind and while the chat-runtime-sharing preview is on, the batch is
exactly what the projection built before this switch existed
(``test_kas_engine_subagents_crew_hold.py`` pins the off batch per agent shape).
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_engine_subagents as engine
from kiro_crew.acp import kas_native_batch
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.config.loader import KiroCrewConfig

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_HELPER = "kirocrew-helper"
_IMPORTS_SEAM = re.compile(r"import kas_engine_subagents|kas_engine_subagents import")
_DASHBOARD = "dashboard:t1"


def _default_spec(name: str = "kirocrew") -> dict:
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    return {
        "name": name,
        "description": "the default agent",
        "prompt": "You are the default agent.",
        "tools": shipped["tools"],
        "allowedTools": shipped["allowedTools"],
    }


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


@pytest.fixture
def setting(monkeypatch):
    """Set the ``kas_subagents`` preview (and the sharing one) for what the projection reads."""

    def _set(on: bool, *, sharing: bool = False) -> None:
        cfg = KiroCrewConfig()
        previews = {engine.KAS_SUBAGENTS_PREVIEW: True} if on else {}
        if sharing:
            previews["chat_runtime_sharing"] = True
        cfg.dashboard.feature_previews = previews
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _batch(agent: str = "kirocrew", key: str = _DASHBOARD, **kw) -> list[dict]:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key=key, **kw)
    return extras.custom_agents


async def _mains_batch(monkeypatch, agent: str = "kirocrew", key: str = _DASHBOARD, **kw):
    """The batch as the projection built it before this switch: no mount."""
    with monkeypatch.context() as m:
        m.setattr(engine, "engine_subagents_on", lambda *a, **k: False)
        return await _batch(agent, key, **kw)


def _by_id(batch: list[dict]) -> dict[str, dict]:
    return {a["id"]: a for a in batch}


def _refused(entry: dict, name: str) -> bool:
    rules = (entry.get("permissions") or {}).get("rules") or []
    return any(
        r.get("capability") == "subagent"
        and r.get("effect") == "deny"
        and any(fnmatch.fnmatchcase(name, p) for p in r.get("match") or ["*"])
        and not any(fnmatch.fnmatchcase(name, p) for p in r.get("exclude") or [])
        for r in rules
    )


# ── on: the default agent's dashboard chat ─────────────────────────────────


@pytest.mark.asyncio
async def test_engine_mounts_the_tool_naming_only_the_helper(agents_dir, setting):
    _write(agents_dir, _default_spec())
    setting(True)
    batch = _by_id(await _batch())
    parent = batch["kirocrew"]
    assert "invoke_sub_agent" in parent["tools"]
    assert _HELPER in batch
    assert not _refused(parent, _HELPER)
    for other in ("kirocrew-worker", "kirocrew-lite", "some-user-agent", "kirocrew"):
        assert _refused(parent, other), other


@pytest.mark.asyncio
@pytest.mark.parametrize("star", ["*", ["*"]], ids=["string", "list"])
async def test_engine_keeps_an_all_tools_default_agent_whole(agents_dir, setting, star):
    _write(agents_dir, {**_default_spec(), "tools": star})
    setting(True)
    parent = _by_id(await _batch())["kirocrew"]
    assert parent["tools"] == "*" or "*" in parent["tools"]
    assert "@kirocrew-core/spawn_sub_agents" in parent["excludedTools"]
    assert not _refused(parent, _HELPER) and _refused(parent, "kirocrew-worker")


@pytest.mark.asyncio
async def test_a_targets_own_server_is_excluded_from_the_helper(agents_dir, setting):
    from kiro_crew.acp import kas_engine_subagent_target

    batch = [
        {"id": "kirocrew", "tools": ["fs_read"], "mcpServers": {"a": {}}},
        {"id": "user-target", "tools": ["fs_read"], "mcpServers": {"target-only": {}}},
    ]
    setting(True)
    out = engine.with_engine_subagents(
        batch,
        "kirocrew",
        agents_dir=agents_dir,
        work_dir=None,
        stub_server_names=frozenset(),
        session_key=_DASHBOARD,
        settings_dir=None,
        notices=[],
        parent_spec=_default_spec(),
    )
    helper = _by_id(out)[kas_engine_subagent_target.KAS_HELPER_AGENT]
    assert {"@a", "@target-only"} <= set(helper["excludedTools"])


@pytest.mark.asyncio
async def test_the_helper_withholds_what_the_parents_own_hooks_cover(agents_dir, setting):
    """What the staleness check reads: no entry auto-approves what the hook covers."""
    from kiro_crew.acp.kas_hook_mcp import approved_mcp_servers
    from kiro_crew.acp.kas_permissions import (
        cached_mcp_tool_names,
        hook_gated_auto_approved,
        hook_gated_capabilities,
    )
    from kiro_crew.acp.kas_subagent_targets import registered_unprompted

    hooks = {"preToolUse": [{"matcher": "fs_read", "command": "echo checked"}]}
    _write(agents_dir, {**_default_spec(), "hooks": hooks})
    setting(True)
    batch = await _batch()
    assert _HELPER in _by_id(batch)
    still: set[str] = set()
    for part in registered_unprompted(None, batch):
        covered = hook_gated_capabilities(
            ("fs_read",),
            mcp_servers=approved_mcp_servers(part),
            mcp_tool_names=cached_mcp_tool_names,
        )
        still |= hook_gated_auto_approved(covered, part)
    assert still == set()


#: Every restriction a session agent's spec can carry, and what the helper gets for
#: it: "mirrored" (the mount happens and the helper carries it) or "refused" (no
#: mount: the chat keeps Kiro Crew's own sub-agents, exactly as on main). A new kind
#: of restriction is added here first; ``unmirrored_restriction`` must answer it.
def _perms(*rules: dict) -> dict:
    """A spec ``permissions`` block holding *rules*."""
    return {"permissions": {"rules": list(rules)}}


_RESTRICTIONS = [
    (
        "pre_tool_hook",
        {"hooks": {"preToolUse": [{"matcher": "fs_read", "command": "echo x"}]}},
        "mirrored",
    ),
    (
        "post_tool_hook",
        {"hooks": {"postToolUse": [{"matcher": "execute_bash", "command": "echo x"}]}},
        "mirrored",
    ),
    ("mcp_exclusion", {"excludedTools": ["@kirocrew-core/spawn_run"]}, "mirrored"),
    ("builtin_exclusion", {"excludedTools": ["execute_bash"]}, "refused"),
    ("builtin_ref_exclusion", {"excludedTools": ["@builtin/execute_bash"]}, "refused"),
    ("builtin_short_ref_exclusion", {"excludedTools": ["@builtin/shell"]}, "refused"),
    ("all_builtins_exclusion", {"excludedTools": ["@builtin"]}, "refused"),
    ("tag_exclusion", {"excludedTools": ["@write"]}, "refused"),
    (
        "read_denied_paths",
        {"toolsSettings": {"fs_read": {"deniedPaths": ["private/**"]}}},
        "refused",
    ),
    (
        "shell_allowlist",
        {"toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}}},
        "mirrored",
    ),
    (
        "shell_setting_with_no_mirror",
        {"toolsSettings": {"shell": {"denyByDefault": True, "somethingElse": True}}},
        "refused",
    ),
    (
        "write_allowlist",
        {"toolsSettings": {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}}},
        "refused",
    ),
    (
        "web_blocked",
        {"toolsSettings": {"web_fetch": {"blocked": ["https://x.example/*"]}}},
        "refused",
    ),
    ("narrower_tools", {"tools": ["fs_read", "grep", "@kirocrew-core"]}, "refused"),
    ("aliased_tools", {"tools": ["fsRead", "fsWrite", "@kirocrew-core"]}, "refused"),
    ("narrower_allowed", {"allowedTools": ["grep", "@kirocrew-core"]}, "mirrored"),
    ("no_allowed", {"allowedTools": []}, "mirrored"),
    ("builtin_ref_tools", {"tools": ["@builtin/*", "@kirocrew-core"]}, "refused"),
    ("builtin_slash_tools", {"tools": ["@builtin/", "@kirocrew-core"]}, "refused"),
    ("builtin_ref_allowed", {"allowedTools": ["@builtin/*", "@kirocrew-core"]}, "mirrored"),
    ("all_tools", {"tools": "*"}, "mirrored"),
    ("all_tools_list", {"tools": ["*"]}, "mirrored"),
    # Operator rules on starting a sub-agent: one that would refuse the helper keeps
    # the chat on Kiro Crew's own sub-agents; one that spares it does not.
    ("deny_subagent", _perms({"capability": "subagent", "effect": "deny"}), "refused"),
    (
        "deny_subagent_all_names",
        _perms({"capability": "subagent", "match": ["*"], "effect": "deny"}),
        "refused",
    ),
    (
        "deny_helper_by_glob",
        _perms({"capability": "subagent", "match": ["kirocrew-*"], "effect": "deny"}),
        "refused",
    ),
    (
        "deny_helper_by_name",
        _perms({"capability": "subagent", "match": [_HELPER], "effect": "deny"}),
        "refused",
    ),
    (
        "allow_list_without_helper",
        _perms({"capability": "subagent", "match": ["*"], "exclude": ["mine"], "effect": "deny"}),
        "refused",
    ),
    ("deny_all_capabilities", _perms({"capability": "all", "effect": "deny"}), "refused"),
    ("deny_builtin_capabilities", _perms({"capability": "builtin", "effect": "deny"}), "refused"),
    (
        "deny_empty_match",
        _perms({"capability": "subagent", "match": [], "effect": "deny"}),
        "refused",
    ),
    (
        # KAS reads only ``*`` as a wildcard: ``?`` is a literal, so this spares no one.
        "exclude_with_literal_question_mark",
        _perms(
            {
                "capability": "subagent",
                "match": ["*"],
                "exclude": ["kirocrew-helpe?"],
                "effect": "deny",
            }
        ),
        "refused",
    ),
    (
        "deny_filesystem_only",
        _perms({"capability": "filesystem", "match": ["/secret/**"], "effect": "deny"}),
        "mirrored",
    ),
    (
        "deny_sparing_helper",
        _perms({"capability": "subagent", "match": ["*"], "exclude": [_HELPER], "effect": "deny"}),
        "mirrored",
    ),
    (
        "deny_other_agent",
        _perms({"capability": "subagent", "match": ["someone-else"], "effect": "deny"}),
        "mirrored",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "extra", "outcome"), _RESTRICTIONS, ids=[r[0] for r in _RESTRICTIONS]
)
async def test_every_parent_restriction_is_mirrored_or_refuses_the_mount(
    agents_dir, setting, monkeypatch, kind, extra, outcome
):
    _write(agents_dir, {**_default_spec(), **extra})
    setting(True)
    batch = await _batch()
    if outcome == "refused":
        assert batch == await _mains_batch(monkeypatch), kind
        return
    by_id = _by_id(batch)
    assert _HELPER in by_id, kind
    parent, helper = by_id["kirocrew"], by_id[_HELPER]

    def rules(entry: dict) -> list[dict]:
        return (entry.get("permissions") or {}).get("rules") or []

    # Engine rules: every deny and ask of the parent is on the helper too.
    for rule in rules(parent):
        if rule.get("effect") in ("deny", "ask") and rule.get("capability") != "subagent":
            assert rule in rules(helper), kind
    # Tools: the helper names no MCP server.
    assert not any(isinstance(t, str) and t.startswith("@") for t in helper["tools"]), kind
    # Unasked: the helper runs nothing unasked the parent would have asked for.
    from kiro_crew.acp.kas_permissions import auto_approved_capabilities

    assert auto_approved_capabilities(helper.get("permissions")) <= auto_approved_capabilities(
        parent.get("permissions")
    ), kind
    assert not helper.get("_crewAnsweredBuiltins"), kind


@pytest.mark.asyncio
async def test_the_shipped_default_agent_mounts_the_tool_and_the_helper(agents_dir, setting):
    """The default agent's spec as the product's own writer builds it, on a fresh home."""
    from kiro_crew.agent_parts.spec_build import build_agent_config

    spec = build_agent_config()
    assert engine.unmirrored_restriction(spec) == ""
    _write(agents_dir, {**spec, "name": "kirocrew"})
    setting(True)
    batch = _by_id(await _batch())
    assert "invoke_sub_agent" in batch["kirocrew"]["tools"]
    assert _HELPER in batch


def test_no_parent_spec_means_no_mount(agents_dir):
    assert engine.unmirrored_restriction(None)
    batch = [{"id": "kirocrew", "tools": ["fs_read"]}]
    out = engine.with_engine_subagents(
        batch,
        "kirocrew",
        agents_dir=agents_dir,
        work_dir=None,
        stub_server_names=frozenset(),
        session_key=_DASHBOARD,
        settings_dir=None,
        notices=[],
    )
    assert out == batch


def test_only_restrictions_travel_to_the_helper():
    parent = {
        "permissions": {
            "rules": [
                {"capability": "read", "match": ["a/**"], "effect": "allow"},
                {"capability": "read", "match": ["b/**"], "effect": "deny"},
                {"capability": "shell", "match": ["*"], "effect": "ask"},
                {"capability": "subagent", "match": ["*"], "effect": "deny"},
            ]
        }
    }
    helper = engine._with_parent_restrictions({"id": _HELPER}, parent)
    assert helper["permissions"]["rules"] == [
        {"capability": "read", "match": ["b/**"], "effect": "deny"},
        {"capability": "shell", "match": ["*"], "effect": "ask"},
    ]


@pytest.mark.asyncio
async def test_engine_takes_spawn_sub_agents_off_the_default_agent_only(agents_dir, setting):
    _write(agents_dir, _default_spec())
    setting(True)
    batch = _by_id(await _batch())
    assert "@kirocrew-core/spawn_sub_agents" in batch["kirocrew"]["excludedTools"]
    # spawn_run, its lifecycle tools and every other Kiro Crew tool stay.
    assert not any(
        t.startswith("@kirocrew-core/") and t != "@kirocrew-core/spawn_sub_agents"
        for t in batch["kirocrew"]["excludedTools"]
    )


# ── off, and everywhere "on" does not apply: exactly main's batch ────────────


@pytest.mark.asyncio
async def test_off_projects_what_main_projects(agents_dir, setting, monkeypatch):
    _write(agents_dir, _default_spec())
    setting(False)
    assert await _batch() == await _mains_batch(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["cron:job-1", "subagent:abc", "wf:run-1", "taskrunner:t", "slack:C1:T1", "heartbeat", ""],
)
async def test_engine_leaves_every_other_session_kind_as_main(
    agents_dir, setting, monkeypatch, key
):
    _write(agents_dir, _default_spec())
    setting(True)
    batch = await _batch(key=key)
    assert batch == await _mains_batch(monkeypatch, key=key)
    assert "@kirocrew-core/spawn_sub_agents" not in (
        _by_id(batch)["kirocrew"].get("excludedTools") or []
    )


@pytest.mark.asyncio
async def test_engine_leaves_a_crew_members_chat_as_main(agents_dir, setting, monkeypatch):
    _write(agents_dir, _default_spec())
    setting(True)
    key = "dashboard:member-ada"
    assert await _batch(key=key) == await _mains_batch(monkeypatch, key=key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "kirocrew-conductor",
        "kirocrew-research",
        "kirocrew-mate-conductor",
        "kirocrew-worker",
        "kirocrew-heartbeat",
        "kirocrew-team-lead",
    ],
)
async def test_an_agent_built_from_the_default_template_is_byte_equal_to_main(
    agents_dir, setting, monkeypatch, name
):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _default_spec(name))
    # A mirror's generation check is not what is compared here: both sides skip it.
    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *a, **k: None)
    setting(True)
    assert json.dumps(await _batch(name), sort_keys=True) == json.dumps(
        await _mains_batch(monkeypatch, name), sort_keys=True
    )


@pytest.mark.asyncio
async def test_a_users_own_agent_keeps_what_its_file_says(agents_dir, setting, monkeypatch):
    _write(agents_dir, {"name": "mine", "prompt": "p", "tools": ["fs_read", "@kirocrew-core"]})
    setting(True)
    assert await _batch("mine") == await _mains_batch(monkeypatch, "mine")


@pytest.mark.asyncio
async def test_a_remote_workspace_session_keeps_kiro_crews_own_sub_agents(
    agents_dir, setting, monkeypatch
):
    """A remote workspace replaces every built-in, the engine's tool included."""
    _write(agents_dir, _default_spec())
    setting(True)
    extras = await KasHarness().session_extras(
        "kirocrew", work_dir=None, session_key=_DASHBOARD, remote_workspace=True
    )
    assert extras.custom_agents == await _mains_batch(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("attached", [False, True])
async def test_the_runtime_names_a_remote_workspace_only_when_one_is_set(attached):
    """Every other host's ``session_extras`` call is unchanged: the key goes only when set."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.acp import runtime_session_setup

    harness = SimpleNamespace(
        session_extras=AsyncMock(return_value=SimpleNamespace(custom_agents=None))
    )
    runtime = SimpleNamespace(
        _harness=harness,
        _mcp_gateway_overlay=None,
        _remote_workspace={"server": {}} if attached else None,
        _refuse_if_loader_unreachable=lambda *_a: None,
    )
    await runtime_session_setup._kas_custom_agents(runtime, "kirocrew", session_key=_DASHBOARD)
    kwargs = harness.session_extras.await_args.kwargs
    assert ("remote_workspace" in kwargs) is attached
    assert kwargs.get("remote_workspace", attached) is attached


@pytest.mark.asyncio
async def test_the_sharing_preview_keeps_the_mount_off(agents_dir, setting, monkeypatch):
    from kiro_crew.acp.chat_runtime_sharing import agent_spawns_native_subagents

    _write(agents_dir, _default_spec())
    setting(True, sharing=True)
    assert await _batch() == await _mains_batch(monkeypatch)
    # The process-sharing decision reads the spec on disk, which the mount never
    # touches, so it answers as on main under either value.
    on = agent_spawns_native_subagents(None, "kirocrew", "kas")
    setting(False, sharing=True)
    assert agent_spawns_native_subagents(None, "kirocrew", "kas") == on


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "body"),
    [
        (f"{_HELPER}.json", json.dumps({"name": _HELPER, "prompt": "p", "tools": ["*"]})),
        (f"{_HELPER}.md", f"---\nname: {_HELPER}\ntools: ['*']\n---\np\n"),
        # A package manager's namespaced file that declares the bare name.
        ("pkg-helper.json", json.dumps({"name": _HELPER, "prompt": "p", "tools": ["*"]})),
    ],
    ids=["json", "markdown", "declared-name"],
)
async def test_an_agent_file_named_like_the_helper_refuses_the_mount(
    agents_dir, setting, monkeypatch, filename, body
):
    _write(agents_dir, _default_spec())
    (agents_dir / filename).write_text(body, encoding="utf-8")
    setting(True)
    extras = await KasHarness().session_extras("kirocrew", work_dir=None, session_key=_DASHBOARD)
    assert extras.custom_agents == await _mains_batch(monkeypatch)
    assert f"an agent named {_HELPER} already exists" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_the_switch_never_writes_an_agent_file(agents_dir, setting):
    _write(agents_dir, _default_spec())
    before = {p.name: p.read_bytes() for p in agents_dir.iterdir()}
    setting(True)
    await _batch()
    assert {p.name: p.read_bytes() for p in agents_dir.iterdir()} == before


# ── where the switch is read, and its one default ────────────────────────────


def test_the_default_is_off_and_the_switch_is_a_feature_preview_only():
    from types import SimpleNamespace

    from kiro_crew.config.sections import FEATURE_PREVIEW_FLAGS

    assert engine.KAS_SUBAGENTS_PREVIEW in FEATURE_PREVIEW_FLAGS
    assert not hasattr(KiroCrewConfig().agent, "subagents")
    assert engine.kas_subagents_preview_on(KiroCrewConfig()) is False
    # A config with no dashboard section reads off too.
    assert engine.kas_subagents_preview_on(SimpleNamespace()) is False


@pytest.mark.parametrize("value", ["true", 1, "on", None, False])
def test_a_value_that_is_not_true_reads_off(value):
    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = {engine.KAS_SUBAGENTS_PREVIEW: value}
    assert engine.kas_subagents_preview_on(cfg) is False


def test_an_lts_build_reads_the_preview_off(monkeypatch):
    from kiro_crew import edition_locks

    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = {engine.KAS_SUBAGENTS_PREVIEW: True}
    assert engine.kas_subagents_preview_on(cfg) is True
    monkeypatch.setattr(
        edition_locks, "local_enable_locked", lambda name: name == "feature_previews"
    )
    assert engine.kas_subagents_preview_on(cfg) is False


def test_only_the_kas_projection_and_the_contract_read_the_switch():
    """Every other backend ignores it: nothing outside these modules imports the seam."""
    root = Path(engine.__file__).parents[1]
    readers = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if _IMPORTS_SEAM.search(p.read_text(encoding="utf-8"))
        and p.name != "kas_engine_subagents.py"
    )
    assert readers == [
        # The KAS ``toolsSettings`` translation: three ``deniedCommands`` entries
        # travel as kiro-cli sends them while the preview is on.
        "acp/kas_deny_cli_forms.py",
        "acp/kas_native_batch.py",
        # A spawn_run run approved as its founding chat is, with the preview on.
        "acp/kas_parent_grants.py",
        # The KAS projection's reading of a shell grant beside `denyByDefault`.
        "acp/kas_shell_grant_lift.py",
        "context_assembly/agent_spec.py",
    ]


def test_the_batch_seam_is_called_only_by_the_kas_harness():
    root = Path(kas_native_batch.__file__).parents[1]
    callers = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "native_batch(" in p.read_text(encoding="utf-8") and p.name != "kas_native_batch.py"
    )
    assert callers == ["acp/harness/kas.py"]


# ── the never-defer list and the job-tool budget do not move ─────────────────


@pytest.mark.parametrize("value", [True, False])
def test_the_never_defer_row_is_the_same_for_both_values(value, setting):
    from kiro_crew.agent_sdk.tool_search import JOB_TOOLS, job_tool_ids

    setting(value)
    spec = _default_spec()
    assert "kirocrew-core::spawn_sub_agents" in JOB_TOOLS["kirocrew"]
    assert "kirocrew-core::spawn_sub_agents" in job_tool_ids("kirocrew", spec)


# ── one stored setting: the preview, and no agent.subagents anywhere ──────────


def test_no_settings_row_change_card_or_schema_names_agent_subagents():
    from kiro_crew.change_card_catalog_kinds import _SETTING_ORDER
    from kiro_crew.dashboard.handlers import config_all_settings, core_config_schema

    assert "agent.subagents" not in config_all_settings._OWNING_PAGES
    assert "agent.subagents" not in core_config_schema._EDITABLE_CONFIG
    assert "agent.subagents" not in _SETTING_ORDER
    assert f"dashboard.feature_previews.{engine.KAS_SUBAGENTS_PREVIEW}" in (
        core_config_schema._EDITABLE_CONFIG
    )


# ── the value as config.json stores it ────────────────────────────────────────


@pytest.fixture
def stored(monkeypatch, tmp_path):
    """Write the preview into a real ``config.json`` (``None``: no such key)."""

    def _store(value: bool | None) -> None:
        home = tmp_path / "crew-home"
        home.mkdir(exist_ok=True)
        previews = {} if value is None else {engine.KAS_SUBAGENTS_PREVIEW: value}
        body = {"dashboard": {"feature_previews": previews}}
        (home / "config.json").write_text(json.dumps(body), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)

    return _store


@pytest.mark.parametrize("value,loaded", [(True, True), (False, False), (None, False)], ids=str)
def test_the_stored_value_is_the_loaded_value(stored, value, loaded):
    stored(value)
    assert engine.kas_subagents_preview_on(KiroCrewConfig.load()) is loaded


@pytest.mark.asyncio
async def test_a_stored_off_or_unset_projects_the_batch_and_contract_of_before(
    agents_dir, stored, monkeypatch
):
    from kiro_crew.acp.provider_labels import PROVIDER_LABEL_KAS

    _write(agents_dir, _default_spec())
    for value in (None, False):
        stored(value)
        assert await _batch() == await _mains_batch(monkeypatch)
        assert not engine.contract_names_engine_subagents(
            PROVIDER_LABEL_KAS, "kirocrew", _DASHBOARD
        )
    stored(True)
    assert _HELPER in _by_id(await _batch())
    assert engine.contract_names_engine_subagents(PROVIDER_LABEL_KAS, "kirocrew", _DASHBOARD)


def _sel_events(monkeypatch) -> list[dict]:
    import types

    from kiro_crew.acp import kas_subagent_targets

    events: list[dict] = []
    monkeypatch.setattr(
        kas_subagent_targets,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    return events


@pytest.mark.asyncio
async def test_the_mount_is_recorded_in_the_security_event_log(agents_dir, setting, monkeypatch):
    _write(agents_dir, _default_spec())
    setting(True)
    events = _sel_events(monkeypatch)

    batch = _by_id(await _batch())

    [event] = [e for e in events if e["operation"] == "kas_engine_subagent_mounted"]
    assert event["source"] == "kas_agent_projection"
    copied = sum(
        1
        for r in (batch["kirocrew"].get("permissions") or {}).get("rules") or []
        if r.get("effect") in ("deny", "ask") and r.get("capability") != "subagent"
    )
    assert copied > 0
    assert event["resources"] == (
        f"sub-agent tool limited to {_HELPER}, which carries {copied} deny or ask "
        "rule(s) of its parent and holds 0 of its MCP server(s) for agent kirocrew"
    )


@pytest.mark.asyncio
async def test_off_records_no_mount(agents_dir, setting, monkeypatch):
    _write(agents_dir, _default_spec())
    setting(False)
    events = _sel_events(monkeypatch)

    await _batch()

    assert [e for e in events if e["operation"] == "kas_engine_subagent_mounted"] == []

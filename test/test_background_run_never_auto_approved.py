"""``@kirocrew-cron/background_run`` is never auto-approved, by any grant, on any route.

It runs an arbitrary command, so like ``execute_bash`` every call has to reach the
approval gate. A covering grant (``@kirocrew-cron``, ``@kirocrew-cron/*``, ``*``) is
replaced by the server's other verbs; an exact or verb-glob grant of it is dropped.
Pinned on the three routes a grant reaches a session by: the kiro-cli spec writers
(``auto_approve``), ``kirocrew doctor``'s repair, and the KAS projection.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import patch

import native_skill_projection_helpers
import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _healthy_agent_file,
)
from kas_agents_helpers import _spec

import kiro_crew.mcp_discovery_managed as managed
from kiro_crew import agent
from kiro_crew.acp import kas_agents, kas_auto_approve
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.agent_materialization import auto_approve
from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions
from kiro_crew.agent_sdk.mcp_refs import _glob_hits, entry_reaches
from kiro_crew.platform import governance, governance_profiles

BACKGROUND_RUN = "@kirocrew-cron/background_run"

#: The cron server in another letter case, which ``entry_reaches`` folds onto it.
MIXED_CASE = "@KiroCrew-Cron"  # brand-ok - a server-name spelling, not the product name

#: Every spelling that covers ``background_run`` without naming it alone.
COVERING = ["@kirocrew-cron", "@kirocrew-cron/", "@kirocrew-cron/*", "*", MIXED_CASE]


def _other_cron_verbs() -> list[str]:
    verbs = managed.managed_tool_names_for_policy("kirocrew-cron")
    assert verbs and "background_run" in verbs, verbs
    return [f"@kirocrew-cron/{v}" for v in verbs if v != "background_run"]


def _covers(entries: list[str]) -> list[str]:
    return [e for e in entries if entry_reaches(e, BACKGROUND_RUN)]


def _no_ceiling(ref: str) -> bool:
    """The shared predicate on a host with no ceiling, no profile and no confirm list."""
    return governance.may_skip_gate(ref, None)


@pytest.fixture
def ungoverned(monkeypatch):
    """No ceiling and no profile: the host the finding names."""
    monkeypatch.setattr(agent, "may_skip_gate_now", _no_ceiling)
    monkeypatch.setattr(kas_agents, "may_skip_gate_now", _no_ceiling)
    monkeypatch.setattr(kas_auto_approve, "may_skip_gate_now", _no_ceiling)
    monkeypatch.setattr(governance, "may_skip_gate_now", _no_ceiling)


# ── kiro-cli write route ──────────────────────────────────────────────────


@pytest.mark.parametrize("grant", COVERING)
def test_the_ceiling_narrows_a_covering_grant_to_the_other_cron_verbs(ungoverned, grant):
    config = {"allowedTools": ["fs_read", grant, "@kirocrew-core/wait"]}
    auto_approve._apply_allowed_tools_ceiling(config, source="test")
    allowed = config["allowedTools"]
    assert not _covers(allowed), allowed
    for ref in _other_cron_verbs():
        assert ref in allowed, ref
    assert "@kirocrew-core/wait" in allowed
    assert len(allowed) == len(set(allowed))


@pytest.mark.parametrize("grant", [BACKGROUND_RUN, "@kirocrew-cron/back*", "background_run"])
def test_the_ceiling_drops_a_grant_that_names_only_background_run(ungoverned, grant):
    config = {"allowedTools": ["@kirocrew-cron/cron_list", grant]}
    auto_approve._apply_allowed_tools_ceiling(config, source="test")
    assert config["allowedTools"] == ["@kirocrew-cron/cron_list"]


@pytest.mark.parametrize("grant", [*COVERING, BACKGROUND_RUN])
def test_the_predicate_never_lets_a_covering_grant_on_a_list(ungoverned, grant):
    assert auto_approve._may_auto_approve(grant) is False
    assert auto_approve._may_auto_approve("@kirocrew-cron/cron_list") is True


def test_build_agent_config_carries_no_grant_covering_background_run(ungoverned, tmp_path):
    """The shipped template plus a user override that widened cron to the whole server."""
    override = tmp_path / "agent.json"
    override.write_text(json.dumps({"allowedTools": ["fs_read", "@kirocrew-cron"]}))
    with patch("kiro_crew.agent._user_overrides_path", return_value=override):
        allowed = agent.build_agent_config()["allowedTools"]
    assert not _covers(allowed), allowed
    for ref in _other_cron_verbs():
        assert ref in allowed, ref


def test_the_rebuild_final_pass_narrows_an_on_disk_whole_server_grant(ungoverned):
    """``rebuild_agent_config`` keeps entries from the spec on disk, which may predate the fix."""
    config = {"allowedTools": ["@kirocrew-cron", "@kirocrew-cron/cron_list"]}
    auto_approve.final_ceiling_pass(config)
    assert not _covers(config["allowedTools"])
    assert config["allowedTools"].count("@kirocrew-cron/cron_list") == 1


def test_the_conductor_grant_filter_narrows_too(ungoverned):
    granted = auto_approve._filter_auto_approve(("@kirocrew-cron",), source="test")
    assert not _covers(granted)
    assert "@kirocrew-cron/cron_list" in granted


def test_the_ceiling_still_judges_each_substituted_verb(monkeypatch):
    monkeypatch.setattr(
        agent,
        "may_skip_gate_now",
        lambda ref: ref != "@kirocrew-cron/cron_add" and _no_ceiling(ref),
    )
    config = {"allowedTools": ["@kirocrew-cron"]}
    auto_approve._apply_allowed_tools_ceiling(config, source="test")
    assert "@kirocrew-cron/cron_add" not in config["allowedTools"]
    assert "@kirocrew-cron/cron_list" in config["allowedTools"]


def test_an_unreadable_cron_declaration_fails_closed_without_dropping_named_verbs(
    ungoverned, monkeypatch
):
    """No verb list to substitute: the covering grant goes, the per-verb ones stay."""
    real = managed.managed_tool_names_for_policy
    monkeypatch.setattr(
        managed,
        "managed_tool_names_for_policy",
        lambda name: None if name == "kirocrew-cron" else real(name),
    )
    config = {"allowedTools": ["@kirocrew-cron", "@kirocrew-cron/cron_list"]}
    auto_approve._apply_allowed_tools_ceiling(config, source="test")
    assert config["allowedTools"] == ["@kirocrew-cron/cron_list"]


def test_the_withhold_is_audited(ungoverned):
    with patch.object(agent, "sel") as sel:
        auto_approve._apply_allowed_tools_ceiling(
            {"allowedTools": ["@kirocrew-cron"]}, source="test"
        )
    (call,) = sel.return_value.log_api_access.call_args_list
    assert call.kwargs["operation"] == "mcp_auto_approve_withheld"
    assert "@kirocrew-cron (narrowed to" in call.kwargs["resources"]


def test_the_derived_kas_block_on_disk_carries_no_covering_allow(ungoverned):
    config = {"allowedTools": ["@kirocrew-cron"]}
    auto_approve._apply_allowed_tools_ceiling(config, source="test")
    block = derived_agent_permissions(config["allowedTools"], "kirocrew.json")
    assert not _kas_allows_background_run(block)
    assert _kas_allows("kirocrew-cron/cron_list", block)


# ── KAS projection route ──────────────────────────────────────────────────


def _kas_allows(tool: str, permissions: dict | None) -> bool:
    for rule in (permissions or {}).get("rules", []):
        if rule["capability"] != "mcp" or rule["effect"] != "allow":
            continue
        if any(_glob_hits(tool, p) for p in rule.get("match") or ["*"]):
            return True
    return False


def _kas_allows_background_run(permissions: dict | None) -> bool:
    return _kas_allows("kirocrew-cron/background_run", permissions)


@pytest.mark.parametrize("grant", COVERING)
def test_the_projection_narrows_a_spec_on_disk_that_predates_the_fix(ungoverned, grant):
    spec = _spec(
        tools=["@kirocrew-cron"],
        allowedTools=[grant],
        mcpServers={"kirocrew-cron": {"command": "x"}},
    )
    permissions = to_client_custom_agent("a", spec, "p").get("permissions")
    assert not _kas_allows_background_run(permissions), permissions
    if grant != MIXED_CASE:  # KAS matches servers case-sensitively
        assert _kas_allows("kirocrew-cron/cron_list", permissions), permissions


def test_ceiling_permitted_withholds_every_covering_spelling(ungoverned):
    permitted = kas_agents._ceiling_permitted([*COVERING, BACKGROUND_RUN, "*_run"], "a")
    assert not _covers(permitted), permitted
    assert "@kirocrew-cron/cron_list" in permitted


def test_an_authored_kas_allow_cannot_cover_background_run(ungoverned):
    """A pure-KAS spec (no ``allowedTools``) whose own block allows the whole cron server."""
    spec = _spec(
        tools=["@kirocrew-cron"],
        mcpServers={"kirocrew-cron": {"command": "x"}},
        permissions={
            "rules": [{"capability": "mcp", "match": ["kirocrew-cron/*"], "effect": "allow"}]
        },
    )
    permissions = to_client_custom_agent("a", spec, "p").get("permissions")
    assert not _kas_allows_background_run(permissions), permissions


def test_an_authored_kas_deny_still_travels(ungoverned):
    rule = {"capability": "mcp", "match": ["kirocrew-cron/cron_add"], "effect": "deny"}
    spec = _spec(
        tools=["@kirocrew-cron"],
        mcpServers={"kirocrew-cron": {"command": "x"}},
        permissions={"rules": [rule]},
    )
    assert rule in to_client_custom_agent("a", spec, "p")["permissions"]["rules"]


# ── doctor route ──────────────────────────────────────────────────────────


def _probe_nothing():
    from kiro_crew.mcp_discovery import McpServerInfo

    async def fake(target: McpServerInfo) -> McpServerInfo:
        target.status, target.tools, target.error = "ok", [], ""
        return target

    return patch("kiro_crew.cli_doctor.probe_server", side_effect=fake)


def test_doctor_mints_no_grant_covering_background_run(tmp_path):
    from kiro_crew.cli_doctor import _doctor_mcp_tools

    agent_path = tmp_path / "kirocrew.json"
    _healthy_agent_file(agent_path)
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data["allowedTools"] = []
    agent_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with (
        patch("kiro_crew.cli_doctor.may_skip_gate_now", return_value=True),
        _probe_nothing(),
    ):
        _doctor_mcp_tools(agent_path, [])
    allowed = json.loads(agent_path.read_text(encoding="utf-8"))["allowedTools"]
    assert not _covers(allowed), allowed
    for ref in _other_cron_verbs():
        assert ref in allowed, ref
    assert "@kirocrew-core" in allowed


def test_doctor_does_not_read_the_never_rule_as_a_ceiling(tmp_path):
    """The real predicate refuses ``@kirocrew-cron``; doctor still mints the other verbs."""
    from kiro_crew.cli_doctor import _doctor_mcp_tools

    agent_path = tmp_path / "kirocrew.json"
    _healthy_agent_file(agent_path)
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data["allowedTools"] = []
    agent_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with (
        patch("kiro_crew.cli_doctor.may_skip_gate_now", side_effect=_no_ceiling),
        patch("kiro_crew.cli_doctor.withheld_by_confirm_tools", return_value=False),
        _probe_nothing(),
    ):
        _doctor_mcp_tools(agent_path, [])
    allowed = json.loads(agent_path.read_text(encoding="utf-8"))["allowedTools"]
    assert not _covers(allowed), allowed
    for ref in _other_cron_verbs():
        assert ref in allowed, ref


def test_doctor_is_idempotent_on_its_narrowed_grant(tmp_path):
    from kiro_crew.cli_doctor import _doctor_mcp_tools

    agent_path = tmp_path / "kirocrew.json"
    _healthy_agent_file(agent_path)
    with (
        patch("kiro_crew.cli_doctor.may_skip_gate_now", return_value=True),
        _probe_nothing(),
    ):
        _doctor_mcp_tools(agent_path, [])
        first = agent_path.read_text(encoding="utf-8")
        _doctor_mcp_tools(agent_path, [])
    assert agent_path.read_text(encoding="utf-8") == first


# ── the shared predicate (platform.governance) ───────────────────────────


#: Every spelling that reaches ``background_run``: exact, covering, verb glob, bare verb.
REACHING = [*COVERING, BACKGROUND_RUN, "@kirocrew-cron/back*", "background_run", "@*"]


@pytest.fixture
def no_policy(monkeypatch):
    """The real ``may_skip_gate_now`` with no ceiling, no profile and no confirm list."""
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: None)
    monkeypatch.setattr(governance_profiles, "any_configured_profile_governs", lambda ref: False)
    monkeypatch.setattr(governance, "_operator_requires_confirmation", lambda ref: False)


@pytest.mark.parametrize("ref", REACHING)
def test_the_shared_predicate_refuses_every_ref_reaching_background_run(no_policy, ref):
    assert governance.may_skip_gate(ref, None) is False
    assert governance.may_skip_gate_now(ref) is False


@pytest.mark.parametrize("ref", ["@kirocrew-cron/cron_list", "@kirocrew-core", "web_fetch"])
def test_the_shared_predicate_still_lets_other_refs_skip(no_policy, ref):
    assert governance.may_skip_gate(ref, None) is True
    assert governance.may_skip_gate_now(ref) is True


def test_an_auto_approve_list_is_narrowed_past_background_run(ungoverned, monkeypatch):
    """The verb form: an owner-written ``autoApprove`` on the cron server keeps its other verbs."""
    monkeypatch.setattr(governance, "_auto_approve_is_honoured", lambda: True)
    servers = {"kirocrew-cron": {"command": "x", "autoApprove": ["cron_list", "background_run"]}}
    out: dict[str, Any] = governance.strip_ungoverned_auto_approve(servers, audit=False)
    assert out["kirocrew-cron"]["autoApprove"] == ["cron_list"]
    assert servers["kirocrew-cron"]["autoApprove"] == ["cron_list", "background_run"]


def test_the_whole_config_filter_withholds_a_covering_grant(ungoverned, monkeypatch):
    """The dashboard ``PUT /api/agent/config`` route asks only the shared predicate."""
    monkeypatch.setattr(governance, "_auto_approve_is_honoured", lambda: True)
    config: dict[str, Any] = {
        "allowedTools": ["@kirocrew-cron", "@kirocrew-cron/cron_list", "*", "fs_read"],
        "mcpServers": {"kirocrew-cron": {"command": "x", "autoApprove": ["background_run"]}},
    }
    governance.sanitize_agent_config_governance(config, audit=False)
    assert config["allowedTools"] == ["@kirocrew-cron/cron_list"]
    assert "autoApprove" not in config["mcpServers"]["kirocrew-cron"]


# ── kiro-cli native skill view of a user agent no crew is bound to ───────


native_tree = native_skill_projection_helpers.native_tree
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)


@pytest.fixture
def view_of(native_tree, ungoverned, monkeypatch):
    """Build the view kiro-cli runs for a user's own ``custom`` agent, outside any crew."""
    from kiro_crew.acp import skill_projection as projection

    _home, agents, project = native_tree
    monkeypatch.setattr(agent, "governed_view_names", lambda: frozenset())
    monkeypatch.setattr(governance, "_auto_approve_is_honoured", lambda: True)

    def build(allowed: list[str], cron: dict) -> dict:
        spec = {
            "name": "custom",
            "tools": ["fs_read", "@kirocrew-cron"],
            "allowedTools": allowed,
            "mcpServers": {"kirocrew-cron": {"command": "kirocrew", "args": ["mcp-cron"], **cron}},
        }
        (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
        prepared = projection.prepare_native_skill_projection(project)
        assert prepared is not None
        return prepared.specs["custom"]

    return build


@pytest.mark.parametrize("grant", COVERING)
def test_a_user_agent_view_narrows_a_covering_allowed_tools_grant(view_of, grant):
    allowed = view_of(["fs_read", grant], {})["allowedTools"]
    assert not _covers(allowed), allowed
    assert "fs_read" in allowed
    for ref in _other_cron_verbs():
        assert ref in allowed, ref


def test_a_user_agent_view_never_grants_an_auto_approved_background_run(view_of):
    allowed = view_of(["fs_read"], {"autoApprove": ["background_run", "cron_list"]})["allowedTools"]
    assert not _covers(allowed), allowed
    assert "@kirocrew-cron/cron_list" in allowed


# ── the gate's runtime grant row ─────────────────────────────────────────


def _batch_guard_answer(monkeypatch, tool: str):
    """``_tier_batch_guard_grant`` for a call the session's projection recorded as granted."""
    from types import SimpleNamespace

    from kiro_crew.hook_runtime import gate_tiers

    monkeypatch.setattr(governance_profiles, "any_configured_profile_governs", lambda ref: False)
    monkeypatch.setattr(governance, "confirm_tools_unknown_now", lambda: False)
    call = SimpleNamespace(
        identity_trusted=True,
        mcp_server="kirocrew-cron",
        mcp_tool=tool,
        spec_granted_builtins=frozenset({f"@kirocrew-cron/{tool}"}),
        spec_hook_held=frozenset(),
    )
    facts = SimpleNamespace(
        classifier_only=False,
        call=call,
        pre_tool_hooks_fire=False,
        ctx=SimpleNamespace(governance=None),
        config=SimpleNamespace(confirm_tools_unknown=False, confirm_tools=()),
    )
    return gate_tiers._tier_batch_guard_grant(cast(Any, facts), cast(Any, None))


def test_the_gate_grant_row_leaves_background_run_to_prompt(monkeypatch):
    """Refusing the skip is "no grant here", never a deny: the call falls through and asks."""
    assert _batch_guard_answer(monkeypatch, "background_run") is None
    assert _batch_guard_answer(monkeypatch, "cron_list") is not None

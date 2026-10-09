"""The platform server on every crewmate, and the first crewmate's creation.

``kirocrew-guide`` is a platform capability: mounted on the default template,
on every spec derived from it and so on every crewmate and dashboard session,
with exactly the reviewed :data:`kiro_crew.agent._GUIDE_AUTO_GRANTS` granted
through the shared ceiling.
The first crewmate is an ordinary crewmate on that template, created once.
Every test writes under the isolated data home; nothing touches a live
``~/.kiro``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    ASSISTANT_MEMBER_NAME,
    WORKER_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import first_crewmate, guide_platform
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

GUIDE_SERVER = "kirocrew-guide"
GUIDE_REF = f"@{GUIDE_SERVER}"
GRANTS = set(agent._GUIDE_AUTO_GRANTS)


@pytest.fixture()
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: directory)
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", directory)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    return directory


@pytest.fixture()
def rebuildable(agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An agents dir a full ``rebuild_agent_config`` can write into."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launcher = bindir / "kirocrew"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(agent, "_KIROCREW_BIN", str(launcher))
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
    monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "hooks")
    monkeypatch.setattr(
        "kiro_crew.apps.bridges._mcp_json_path", lambda: agents_dir / AGENT_FILENAME
    )
    return agents_dir


def _spec(agents_dir: Path, filename: str) -> dict[str, Any]:
    return json.loads((agents_dir / filename).read_text(encoding="utf-8"))


def _guide_grants(spec: dict[str, Any]) -> set[str]:
    return {
        ref
        for ref in spec.get("allowedTools") or []
        if isinstance(ref, str) and ref.startswith(f"{GUIDE_REF}/")
    }


def _saved() -> dict:
    from kiro_crew.config.loader import config_path

    return json.loads(config_path().read_text(encoding="utf-8"))


def _seed(doc: dict) -> None:
    from kiro_crew.config.loader import update_config_locked

    update_config_locked(mutate=lambda _: json.loads(json.dumps(doc)))


# ── the platform guide set ──


def test_the_guide_server_is_always_emitted_and_never_auto_approved() -> None:
    spec = agent._MANAGED_MCP_SERVERS[GUIDE_SERVER]
    assert not spec.get("opt_in")
    assert "autoApprove" not in spec
    built = agent.build_agent_config()
    assert GUIDE_REF in built["tools"]
    assert built["mcpServers"][GUIDE_SERVER]["args"][-1] == "mcp-guide"
    assert "autoApprove" not in built["mcpServers"][GUIDE_SERVER]
    assert _guide_grants(built) == GRANTS
    # Per tool only: a whole-server grant would pre-approve unreviewed tools.
    assert GUIDE_REF not in built["allowedTools"]


def test_the_grants_name_exactly_the_servers_tools() -> None:
    from kiro_crew import mcp_guide

    assert {g.rsplit("/", 1)[1] for g in GRANTS} == {t["name"] for t in mcp_guide._list_tools()}


def test_the_server_accepts_every_name_its_schema_advertises() -> None:
    from kiro_crew import mcp_guide
    from kiro_crew.validation import MCP_GUIDE_SCHEMAS, validate_tool_args

    (tool,) = mcp_guide._list_tools()
    longest = tool["inputSchema"]["properties"]["name"]["maxLength"]
    name = "a" * longest
    assert validate_tool_args({"name": name}, MCP_GUIDE_SCHEMAS["rename_self"])["name"] == name


def test_the_governance_ceiling_still_withholds_a_guide_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = {f"{GUIDE_REF}/rename_self"}
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: ref not in denied)
    built = agent.build_agent_config()
    assert GUIDE_SERVER in built["mcpServers"]  # still mounted; the user is asked instead
    assert _guide_grants(built) == GRANTS - denied


def test_every_crewmate_spec_mounts_the_same_guide_set(rebuildable: Path) -> None:
    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
    agent.rebuild_agent_config()
    default = _spec(rebuildable, AGENT_FILENAME)
    worker = _spec(rebuildable, WORKER_AGENT_FILENAME)
    for spec in (default, worker):
        assert GUIDE_REF in spec["tools"]
        assert "autoApprove" not in spec["mcpServers"][GUIDE_SERVER]
        assert _guide_grants(spec) == GRANTS
    # The first crewmate runs the very template a user-created crewmate gets.
    row = _saved()["agents"][ASSISTANT_MEMBER_NAME]
    assert row["kiro_agent"] == "kirocrew"
    assert sorted(p.name for p in rebuildable.glob("*mate*")) == []


def test_conductors_and_background_agents_do_not_mount_the_guide_set(rebuildable: Path) -> None:
    """They are driven by patrols and loops, never by a person at the dashboard."""
    from kiro_crew import agent_files

    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
    agent.rebuild_agent_config()
    narrow = (
        agent_files.CONDUCTOR_AGENT_FILENAME,
        agent_files.LEDGER_CONDUCTOR_AGENT_FILENAME,
        agent_files.PIPELINE_CONDUCTOR_AGENT_FILENAME,
        agent_files.SECURITY_CONDUCTOR_AGENT_FILENAME,
        agent_files.KNOWLEDGE_AGENT_FILENAME,
        agent_files.RESEARCH_AGENT_FILENAME,
        agent_files.HEARTBEAT_AGENT_FILENAME,
    )
    for filename in narrow:
        spec = _spec(rebuildable, filename)
        assert GUIDE_SERVER not in (spec.get("mcpServers") or {}), filename
        assert not [t for t in spec.get("tools") or [] if str(t).startswith(GUIDE_REF)], filename
        assert not _guide_grants(spec), filename


def test_an_existing_spec_gains_the_guide_set_once(rebuildable: Path) -> None:
    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
    agent.rebuild_agent_config()
    path = rebuildable / AGENT_FILENAME
    # An install from before the guide set was a platform capability.
    old = _spec(rebuildable, AGENT_FILENAME)
    old["tools"] = [t for t in old["tools"] if t != GUIDE_REF]
    old["allowedTools"] = [t for t in old["allowedTools"] if not t.startswith(GUIDE_REF)]
    path.write_text(json.dumps(old), encoding="utf-8")
    (agent.config_dir() / guide_platform.GUIDE_GRANT_MARKER).unlink()
    agent.rebuild_agent_config()
    upgraded = _spec(rebuildable, AGENT_FILENAME)
    assert GUIDE_REF in upgraded["tools"] and _guide_grants(upgraded) == GRANTS
    # The user then takes it off: it stays off.
    upgraded["tools"].remove(GUIDE_REF)
    path.write_text(json.dumps(upgraded), encoding="utf-8")
    agent.rebuild_agent_config()
    assert GUIDE_REF not in _spec(rebuildable, AGENT_FILENAME)["tools"]


def test_a_spec_without_the_guide_server_keeps_the_one_time_grant_for_later(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant marker is spent only on a spec that could take the grant."""
    marker = agent.config_dir() / guide_platform.GUIDE_GRANT_MARKER
    marker.unlink(missing_ok=True)
    config: dict[str, Any] = {"mcpServers": {"other": {}}, "tools": [], "allowedTools": []}
    assert guide_platform.grant_guide_platform_once(config, fresh_install=False) is False
    assert config == {"mcpServers": {"other": {}}, "tools": [], "allowedTools": []}
    assert not marker.exists()
    config["mcpServers"][agent._GUIDE_SERVER] = {}
    audited: list[dict] = []

    class _Sel:
        def log_api_access(self, **kwargs) -> None:
            audited.append(kwargs)

    monkeypatch.setattr(agent, "sel", lambda: _Sel())
    assert guide_platform.grant_guide_platform_once(config, fresh_install=False) is True
    assert GUIDE_REF in config["tools"] and _guide_grants(config) == GRANTS
    # The pre-approval an upgrade gains is audited like every other added grant.
    (event,) = audited
    assert event["operation"] == "mcp_tools_added"
    assert all(g in event["resources"] for g in GRANTS)


# ── the first crewmate ──


def _assert_private_first_crewmate(saved: dict, template: str = "kirocrew") -> str:
    row = saved["agents"][ASSISTANT_MEMBER_NAME]
    assert (row["kiro_agent"], row["workspace"], row["source"]) == (template, "default", "builtin")
    assert row["member_id"] and row["memory_store"] != "default"
    assert saved["memory_stores"][row["memory_store"]]["owner_member_id"] == row["member_id"]
    return row["member_id"]


@pytest.mark.parametrize(
    "default_row",
    [
        {"kiro_agent": "kirocrew", "display_name": "Mochi", "workspace": "work"},
        {"kiro_agent": "custom-template"},
    ],
)
def test_creation_never_changes_the_default_member(agents_dir, default_row):
    _seed({"agents": {"default": dict(default_row)}, "dashboard": {"user_role": "designer"}})
    first_crewmate.create_first_crewmate_once()
    saved = _saved()
    assert saved["agents"]["default"] == default_row
    assert saved["dashboard"] == {"user_role": "designer"}
    _assert_private_first_crewmate(saved)


def test_it_runs_the_configured_default_template(agents_dir):
    _seed({"agent": {"default_agent": "my-template"}})
    first_crewmate.create_first_crewmate_once()
    saved = _saved()
    assert saved["agents"]["default"]["kiro_agent"] == "my-template"
    _assert_private_first_crewmate(saved, "my-template")


def test_a_deleted_first_crewmate_is_not_recreated(agents_dir):
    from kiro_crew.config.loader import config_path

    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
    first_crewmate.create_first_crewmate_once()
    saved = _saved()
    del saved["agents"][ASSISTANT_MEMBER_NAME]
    _seed(saved)
    before = config_path().read_bytes()
    first_crewmate.create_first_crewmate_once()
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("where", ["base", "overlay"])
def test_an_existing_key_is_left_alone(agents_dir, where):
    from kiro_crew.config.loader import config_local_path, config_path, update_config_locked

    mine = {"kiro_agent": "my-template"}
    if where == "base":
        _seed({"agents": {"default": {"kiro_agent": "kirocrew"}, ASSISTANT_MEMBER_NAME: mine}})
    else:
        _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
        update_config_locked(
            config_local_path(),
            mutate=lambda _: {"agents": {ASSISTANT_MEMBER_NAME: mine}},
            stamp_meta=False,
        )
    before = config_path().read_bytes()
    first_crewmate.create_first_crewmate_once()
    assert config_path().read_bytes() == before


def test_a_failed_creation_leaves_nothing_and_is_tried_again(
    agents_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mate never runs on Global: no private store means no row and no marker."""
    from kiro_crew import memory_stores

    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
    real = memory_stores.persist_member_config

    def refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(memory_stores, "persist_member_config", refuse)
    first_crewmate.create_first_crewmate_once()
    saved = _saved()
    assert ASSISTANT_MEMBER_NAME not in saved["agents"]
    assert not saved.get("memory_stores")
    assert not (agent.config_dir() / first_crewmate.FIRST_CREWMATE_MARKER).exists()
    monkeypatch.setattr(memory_stores, "persist_member_config", real)
    first_crewmate.create_first_crewmate_once()
    _assert_private_first_crewmate(_saved())


def test_a_deleted_predecessors_identity_is_not_inherited(agents_dir) -> None:
    """A deleted ``mate`` whose DM binding was kept reserves its slug."""
    from kiro_crew.members import member_slot_key, write_dm_binding

    write_dm_binding("mate", member="mate", slot_key=member_slot_key("mate"))
    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}}})
    first_crewmate.create_first_crewmate_once()
    assert _assert_private_first_crewmate(_saved()) != "mate"


def test_a_legacy_member_whose_name_slugs_to_mate_keeps_its_slug(agents_dir) -> None:
    """An upgrade that already has a legacy ``Mate`` row: the created first
    crewmate gets an identity of its own, so the two never resolve to one slug."""
    from kiro_crew.members import slug_for_name

    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}, "Mate": {"kiro_agent": "kirocrew"}}})
    first_crewmate.create_first_crewmate_once()
    saved = _saved()
    assert saved["agents"]["Mate"].get("member_id", "") == ""
    assert _assert_private_first_crewmate(saved) != slug_for_name("Mate")


def test_a_foreign_template_and_its_member_are_left_exactly_as_they_are(
    rebuildable: Path,
) -> None:
    """A template and member under a name the product does not own stay untouched.

    The rebuild neither rewrites the file, nor rebinds, relabels or re-stores the
    member bound to it, nor treats the member as the first crewmate.
    """
    foreign = {"name": "kirocrew-mate", "prompt": "mine", "hooks": {"auto_approve_tools": []}}
    path = rebuildable / "kirocrew-mate.json"
    path.write_text(json.dumps(foreign), encoding="utf-8")
    row = {
        "kiro_agent": "kirocrew-mate",
        "memory_store": "default",
        "display_name": "Skipper",
    }
    _seed({"agents": {"default": {"kiro_agent": "kirocrew"}, "kirocrew-mate": row}})
    agent.repair_agent_configs()
    agent.rebuild_agent_config()
    assert json.loads(path.read_text(encoding="utf-8")) == foreign
    assert _saved()["agents"]["kirocrew-mate"] == row


# ── the tools off the dashboard ──


@pytest.mark.parametrize(
    "code",
    [
        "no_live_slot",
        "no_dashboard_turn",
        "channel_caller",
        "subagent_caller",
        "unattended_caller",
        "app_caller",
        "not_user_turn",
        "unattested_caller",
    ],
)
def test_every_gateway_admission_refusal_is_said_as_off_the_dashboard(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """How the MCP side words each refusal the admission returns.

    Which caller gets which refusal is pinned against the real admission in
    ``test_guide_admission.py``; this pins only the wording of the result.
    """
    from kiro_crew import mcp_guide

    reply = {"error": "this message came from a messaging channel", "code": code}
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:c", ""))
    monkeypatch.setattr(mcp_guide, "_post", lambda *a, **k: dict(reply))
    out = mcp_guide._call_tool_inner("rename_self", {"name": "Pebble"})
    assert out.startswith("Error: rename_self needs the dashboard: ")
    assert "Your name did not change" in out


def test_an_unidentified_caller_gets_the_same_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_guide

    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("", "no identity"))
    out = mcp_guide._call_tool_inner("rename_self", {"name": "Pebble"})
    assert out.startswith("Error: rename_self needs the dashboard: no identity")

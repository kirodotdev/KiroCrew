"""The platform guide set on every crewmate.

``kirocrew-guide`` (find_ui, search_docs, guides, change cards) is a platform
capability: mounted on the default template, on every spec derived from it and
so on every crewmate and dashboard session, with exactly the reviewed
:data:`kiro_crew.agent._GUIDE_AUTO_GRANTS` granted through the shared ceiling.
Every test writes under the isolated data home; nothing touches a live
``~/.kiro``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import AGENT_FILENAME, WORKER_AGENT_FILENAME
from kiro_crew.agent_materialization import guide_platform
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


@pytest.mark.parametrize(
    "denied",
    [{f"{GUIDE_REF}/propose_change"}, {f"{GUIDE_REF}/find_ui", f"{GUIDE_REF}/guide_start"}],
)
def test_the_governance_ceiling_still_withholds_a_guide_grant(
    monkeypatch: pytest.MonkeyPatch, denied: set[str]
) -> None:
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
    rebuildable: Path,
) -> None:
    """The grant marker is spent only on a spec that could take the grant."""
    marker = agent.config_dir() / guide_platform.GUIDE_GRANT_MARKER
    marker.unlink(missing_ok=True)
    config: dict[str, Any] = {"mcpServers": {"other": {}}, "tools": [], "allowedTools": []}
    assert guide_platform.grant_guide_platform_once(config, fresh_install=False) is False
    assert config == {"mcpServers": {"other": {}}, "tools": [], "allowedTools": []}
    assert not marker.exists()
    config["mcpServers"][agent._GUIDE_SERVER] = {}
    assert guide_platform.grant_guide_platform_once(config, fresh_install=False) is True
    assert GUIDE_REF in config["tools"] and _guide_grants(config) == GRANTS


# ── the guide tools on and off the dashboard ──


def test_a_dashboard_session_gets_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_guide

    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:c", ""))
    monkeypatch.setattr(
        mcp_guide, "_post", lambda *a, **k: {"id": "c1", "delivered_clients": 1, "risk": "normal"}
    )
    out = json.loads(
        mcp_guide._call_tool_inner("propose_change", {"kind": "setting.change", "params": {}})
    )
    assert out["id"] == "c1" and "Shown above" in out["next"]


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
    ],
)
@pytest.mark.parametrize("tool", ["propose_change", "guide_start", "guide_status"])
def test_every_gateway_admission_refusal_is_said_as_off_the_dashboard(
    monkeypatch: pytest.MonkeyPatch, code: str, tool: str
) -> None:
    """How the MCP side words each refusal the admission returns.

    Which caller gets which refusal is pinned against the real admission in
    ``test_guide_channel_turns.py``; this pins only the wording of the result.
    """
    from kiro_crew import mcp_guide

    reply = {"error": "this message came from a messaging channel", "code": code}
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:c", ""))
    monkeypatch.setattr(mcp_guide, "_post", lambda *a, **k: dict(reply))
    monkeypatch.setattr(mcp_guide, "_get", lambda *a, **k: dict(reply))
    args: dict[str, Any] = {"kind": "setting.change", "params": {}}
    if tool == "guide_start":
        args = {"actions": [{"id": "settings.show"}]}
    elif tool == "guide_status":
        args = {}
    out = mcp_guide._call_tool_inner(tool, args)
    assert out.startswith(f"Error: {tool} needs the dashboard: ")
    assert "Nothing was shown" in out


def test_an_unidentified_caller_gets_the_same_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_guide

    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("", "no identity"))
    out = mcp_guide._call_tool_inner("list_change_kinds", {})
    assert out.startswith("Error: list_change_kinds needs the dashboard: no identity")


def test_find_ui_still_answers_off_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_guide

    refused = {"error": "this message came from a messaging channel", "code": "channel_caller"}
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("slack:C1:t", ""))
    monkeypatch.setattr(mcp_guide, "_get", lambda *a, **k: dict(refused))
    monkeypatch.setattr(mcp_guide, "_post", lambda *a, **k: dict(refused))
    out = json.loads(mcp_guide._call_tool_inner("find_ui", {"query": "dark mode"}))
    assert out["status"] == "ok" and out["results"]
    assert all(r["live"]["status"] == "not_observed" for r in out["results"])
    docs = json.loads(mcp_guide._call_tool_inner("search_docs", {"query": "Slack"}))
    assert docs["results"]


def test_offer_results_tell_the_model_how_to_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_guide

    reply: dict[str, Any] = {}
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:c", ""))
    monkeypatch.setattr(mcp_guide, "_post", lambda *a, **k: dict(reply))

    def next_for(name: str, args: dict[str, Any], **state: Any) -> str:
        reply.clear()
        reply.update({"id": "c1", **state})
        return json.loads(mcp_guide._call_tool_inner(name, args))["next"]

    change = ("propose_change", {"kind": "setting.change", "params": {}})
    guide = ("guide_start", {"actions": [{"id": "settings.show"}]})
    for name, args in (change, guide):
        assert "Queued" in next_for(name, args, delivered_clients=0)
        assert "Shown above" in next_for(name, args, delivered_clients=1)
    assert "box ticked" in next_for(*change, delivered_clients=1, risk="widen")


def test_a_refused_guide_start_says_no_card_was_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_guide

    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:c", ""))
    monkeypatch.setattr(mcp_guide, "_post", lambda *a, **k: {"error": "no such action"})
    out = mcp_guide._call_tool_inner("guide_start", {"actions": [{"id": "ui.show"}]})
    assert out.startswith("Error: no such action")
    assert mcp_guide.GUIDE_NOT_SHOWN_NOTE in out

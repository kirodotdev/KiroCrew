"""Login-posture rebuild withholds non-managed MCP."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy


class _ForcedOnIdentity(DefaultAgentIdentityProvider):
    def enabled(self) -> bool:
        return True


def _ceiling(*, posture: str) -> Any:
    return parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": True, "posture": posture}},
        }
    )


def _enable(posture: str, *, identity_on: bool = True) -> None:
    base = build_default_context(KiroCrewConfig())
    adapter = _ForcedOnIdentity() if identity_on else DefaultAgentIdentityProvider()
    set_context(
        dataclasses.replace(base, agent_identity=adapter, governance=_ceiling(posture=posture))
    )


def _seed_rebuild_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate kiro-cli home + agent dir; seed dummy servers. Never writes ~/.kiro."""
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings_dir = tmp_path / ".kiro" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"dummy-kiro-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    seam_global = tmp_path / "seam-global.json"
    seam_global.write_text(
        json.dumps({"mcpServers": {"dummy-seam-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    from kiro_crew.config import config_dir

    crew_mcp = config_dir() / "mcp.json"
    crew_mcp.write_text(
        json.dumps({"mcpServers": {"dummy-crew-store": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    leftover = kiro_dir / "kirocrew.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    "dummy-leftover": {"command": "dummy-srv"},
                    "dummy-kiro-global": {"command": "dummy-srv"},
                },
                "tools": ["@dummy-leftover"],
                "allowedTools": ["@dummy-leftover"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", sys.executable)
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [seam_global])
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: sys.executable)
    # Ownership is the manifest's, never the filename prefix. These fixtures
    # have no installed app on disk, so declare it: `notes` owns exactly
    # notes--scribe.json; every other <app>--*.json is somebody's custom agent.
    _own(monkeypatch, {"notes": {"notes--scribe.json"}})
    return kiro_dir


def _own(monkeypatch: pytest.MonkeyPatch, owned: dict[str, set[str]]) -> None:
    """Declare which materialized agent files each app owns (manifest seam)."""
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.owned_app_agent_names",
        lambda app_name: set(owned[app_name]) if app_name in owned else set(),
    )


def test_login_rebuild_withholds_kiro_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "kirocrew-core" in servers
    assert "dummy-kiro-global" not in servers
    assert "dummy-seam-global" not in servers
    assert "dummy-crew-store" not in servers
    # Leftover agent-file servers are omitted so kiro-cli cannot exec them
    # before inbound attach. Source mcp.json is not mutated.
    assert "dummy-leftover" not in servers
    tools = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("tools") or []
    assert "@dummy-leftover" not in tools


def test_login_withhold_audits_capability_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.sel import sel

    _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        events = sel().recent(limit=50)
    finally:
        reset_context()

    audited = [e for e in events if e.get("operation") == "agentcore.login_withhold"]
    assert audited, f"expected SEL agentcore.login_withhold row in {events!r}"
    assert audited[0].get("outcome") == "allowed"


def test_login_rebuild_withholds_without_companion_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login", identity_on=False)
        rebuild_agent_config()
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-kiro-global" not in servers
    assert "kirocrew-core" in servers


def test_login_rebuild_stashes_authored_mcp_and_restores_on_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    source_before = settings.read_text(encoding="utf-8")
    try:
        _enable("login")
        rebuild_agent_config()
        sidecar = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        runtime = (
            json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers")
            or {}
        )
        assert "dummy-leftover" not in runtime
        assert "dummy-leftover" in sidecar.get("mcpServers", {})
        assert "@dummy-leftover" in sidecar.get("tools", [])
        assert "@dummy-leftover" in sidecar.get("allowedTools", [])
        assert settings.read_text(encoding="utf-8") == source_before

        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    restored = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-leftover" in restored
    assert "dummy-kiro-global" in restored
    assert settings.read_text(encoding="utf-8") == source_before
    assert not (config_dir() / AUTHORED_MCP_SIDECAR).exists()


def test_clean_login_rebuild_discards_authored_mcp_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        assert (config_dir() / AUTHORED_MCP_SIDECAR).exists()
        rebuild_agent_config(clean=True)
        assert not (config_dir() / AUTHORED_MCP_SIDECAR).exists()
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    restored = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-leftover" not in restored
    assert "dummy-kiro-global" in restored


def test_login_rebuild_does_not_overwrite_stash_with_empty_retract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        first = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        rebuild_agent_config()
        second = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    finally:
        reset_context()

    assert first.get("mcpServers", {}).get("dummy-leftover")
    assert second == first
    runtime = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-leftover" not in runtime


def test_second_login_rebuild_keeps_qualified_authored_tool_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "kirocrew.json"
    spec = json.loads(leftover.read_text(encoding="utf-8"))
    spec["tools"] = ["@dummy-leftover/search"]
    spec["allowedTools"] = ["@dummy-leftover/search"]
    leftover.write_text(json.dumps(spec), encoding="utf-8")
    try:
        _enable("login")
        rebuild_agent_config()
        first = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        leftover.write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}, "tools": []}),
            encoding="utf-8",
        )
        rebuild_agent_config()
        second = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    finally:
        reset_context()

    assert first.get("mcpServers", {}).get("dummy-leftover")
    assert "@dummy-leftover/search" in first.get("tools", [])
    assert "@dummy-leftover/search" in first.get("allowedTools", [])
    assert "@dummy-leftover/search" in second.get("tools", [])
    assert "@dummy-leftover/search" in second.get("allowedTools", [])
    assert "dummy-leftover" in second.get("mcpServers", {})


def test_merge_keeps_qualified_tool_ref_for_kept_server() -> None:
    from kiro_crew.agent import _merge_authored_mcp_payload

    existing = {
        "mcpServers": {"gateway": {"command": "old"}},
        "tools": ["@gateway/toolA", "@gone/toolB"],
        "allowedTools": ["@gateway/toolA"],
        "sourceServers": ["gateway", "gone"],
    }
    incoming = {
        "mcpServers": {"gateway": {"command": "new"}},
        "tools": ["@gateway/toolA"],
        "allowedTools": ["@gateway/toolA"],
        "sourceServers": ["gateway"],
    }
    merged = _merge_authored_mcp_payload(existing, incoming)
    assert merged["mcpServers"]["gateway"]["command"] == "new"
    assert "@gateway/toolA" in merged["tools"]
    assert "@gateway/toolA" in merged["allowedTools"]
    assert "@gone/toolB" not in merged["tools"]
    assert "gone" not in merged["mcpServers"]


def test_merge_honors_explicit_empty_source_servers() -> None:
    """Empty live-source list must not inherit prior ownership.

    Deleting a source during login and adding a same-name agent override
    would otherwise keep the name in ``sourceServers``; leave-login
    restore would treat the override as a vanished source and drop it.
    """
    from kiro_crew.agent import _merge_authored_mcp_payload

    existing = {
        "mcpServers": {"custom": {"command": "npx"}},
        "sourceServers": ["custom"],
    }
    incoming = {
        "mcpServers": {"custom": {"command": "override-bin", "args": ["--agent"]}},
        "sourceServers": [],
    }
    merged = _merge_authored_mcp_payload(existing, incoming)
    assert merged["sourceServers"] == []
    assert merged["mcpServers"]["custom"] == {
        "command": "override-bin",
        "args": ["--agent"],
    }


def test_merge_keeps_prior_source_servers_when_incoming_omits_key() -> None:
    from kiro_crew.agent import _merge_authored_mcp_payload

    existing = {
        "mcpServers": {"custom": {"command": "npx"}},
        "sourceServers": ["custom"],
    }
    incoming = {"mcpServers": {"custom": {"command": "npx"}}}
    merged = _merge_authored_mcp_payload(existing, incoming)
    assert merged["sourceServers"] == ["custom"]


def test_extract_does_not_stash_qualified_managed_ref() -> None:
    from kiro_crew.agent import _extract_non_managed_mcp

    config: dict[str, Any] = {
        "mcpServers": {"kirocrew-core": {}, "dummy": {}},
        "tools": ["@kirocrew-core/search", "@dummy/x"],
        "allowedTools": ["@kirocrew-core/search", "@dummy/x"],
    }
    extracted = _extract_non_managed_mcp(config, {"kirocrew-core"})
    assert "dummy" in extracted["mcpServers"]
    assert "kirocrew-core" not in extracted["mcpServers"]
    assert extracted["tools"] == ["@dummy/x"]
    assert extracted["allowedTools"] == ["@dummy/x"]


def test_retract_keeps_qualified_ref_for_managed_server() -> None:
    from kiro_crew.agent import _retract_non_managed_mcp

    config: dict[str, Any] = {
        "mcpServers": {"kirocrew-core": {}, "dummy": {}},
        "tools": ["@kirocrew-core/search", "@dummy/x"],
        "allowedTools": ["@kirocrew-core/search", "@dummy/x"],
    }
    _retract_non_managed_mcp(config, {"kirocrew-core"})
    assert config["tools"] == ["@kirocrew-core/search"]
    assert config["allowedTools"] == ["@kirocrew-core/search"]
    assert "dummy" not in config["mcpServers"]


def test_login_rebuild_drops_deleted_source_from_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    try:
        _enable("login")
        rebuild_agent_config()
        first = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        assert "dummy-kiro-global" in first.get("mcpServers", {})
        settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        leftover = kiro_dir / "kirocrew.json"
        leftover.write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}, "tools": []}),
            encoding="utf-8",
        )
        rebuild_agent_config()
        second = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    finally:
        reset_context()

    assert "dummy-kiro-global" not in second.get("mcpServers", {})
    assert "dummy-leftover" in second.get("mcpServers", {})
    assert "@dummy-leftover" in second.get("allowedTools", [])


def test_restore_keeps_sidecar_until_runtime_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp, rebuild_agent_config
    from kiro_crew.config import config_dir

    _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        sidecar = config_dir() / AUTHORED_MCP_SIDECAR
        assert sidecar.exists()
        _restore_authored_mcp({"mcpServers": {}})
        assert sidecar.exists()
    finally:
        reset_context()


def test_workload_rebuild_still_merges_kiro_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-kiro-global" in servers
    assert "kirocrew-core" in servers


def test_login_probe_succeeds_iam_invoke_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.cloud import iam
    from kiro_crew.sel import sel

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(iam, "probe_instance_invoke_gateway", lambda: True)
    try:
        _enable("login")
        rebuild_agent_config()
        events = sel().recent(limit=50)
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    mismatch = [e for e in events if e.get("operation") == "agentcore.posture_mismatch"]
    assert mismatch, f"expected SEL agentcore.posture_mismatch row in {events!r}"
    assert mismatch[0].get("outcome") == "denied"
    assert not any("gateway" in name.lower() for name in servers)


def test_source_mcp_names_include_enabled_app_and_edition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda **_k: {"notes:tools": {}})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {"edition-internal": {}})
    names = agent_mod._source_mcp_server_names()
    assert "notes:tools" in names
    assert "edition-internal" in names


def test_source_mcp_names_include_provider_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent as agent_mod

    seam = tmp_path / "seam-global.json"
    seam.write_text(
        json.dumps({"mcpServers": {"dummy-seam-global": {"command": "x"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {})
    monkeypatch.setattr(agent_mod, "_extra_mcp_scope_globals", lambda: [seam])
    names = agent_mod._source_mcp_server_names()
    assert "dummy-seam-global" in names


def test_restore_drops_deleted_provider_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider-global delete must not restore the leftover command."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    seam = tmp_path / "seam-global.json"
    seam.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [seam])
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"dummy-seam-global": {"command": "dummy-srv"}},
                "sourceServers": ["dummy-seam-global"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}}
    assert _restore_authored_mcp(config) is True
    assert "dummy-seam-global" not in config["mcpServers"]


def test_source_mcp_names_store_normalized_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slash source keys must match the alias emitted into the runtime stash."""
    from kiro_crew import agent as agent_mod

    src = tmp_path / "kiro-mcp.json"
    src.write_text(
        json.dumps({"mcpServers": {"namespace/name": {"command": "x"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", src)
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {})
    names = agent_mod._source_mcp_server_names()
    assert "namespace/name" not in names
    assert "namespace-name" in names


def test_source_mcp_names_preserve_collision_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slash key and its alias are two servers; sourceServers must list both."""
    from kiro_crew import agent as agent_mod

    src = tmp_path / "kiro-mcp.json"
    src.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "namespace-name": {"command": "slash-free"},
                    "namespace/name": {"command": "slashed"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", src)
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {})
    names = agent_mod._source_mcp_server_names()
    assert names == {"namespace-name", "namespace-name-2"}


def test_restore_drops_deleted_collision_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting one colliding source must not restore the other's leftover command."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    src = tmp_path / "kiro-mcp.json"
    src.write_text(
        json.dumps({"mcpServers": {"namespace-name": {"command": "slash-free"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", src)
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "namespace-name": {"command": "slash-free"},
                    "namespace-name-2": {"command": "slashed-deleted"},
                },
                "sourceServers": ["namespace-name", "namespace-name-2"],
                "tools": ["@namespace-name-2/search"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}, "tools": []}
    assert _restore_authored_mcp(config) is True
    assert "namespace-name-2" not in config["mcpServers"]
    assert "@namespace-name-2/search" not in config["tools"]
    assert config["mcpServers"]["namespace-name"]["command"] == "slash-free"


def test_merge_drops_deleted_collision_sibling() -> None:
    from kiro_crew.agent import _merge_authored_mcp_payload

    existing = {
        "mcpServers": {
            "namespace-name": {"command": "slash-free"},
            "namespace-name-2": {"command": "slashed-deleted"},
        },
        "sourceServers": ["namespace-name", "namespace-name-2"],
        "tools": ["@namespace-name-2/search"],
    }
    incoming = {
        "mcpServers": {"namespace-name": {"command": "slash-free"}},
        "sourceServers": ["namespace-name"],
        "tools": [],
    }
    merged = _merge_authored_mcp_payload(
        existing, incoming, {"namespace-name": {"command": "slash-free"}}
    )
    assert "namespace-name-2" not in merged["mcpServers"]
    assert "@namespace-name-2/search" not in merged["tools"]
    assert merged["mcpServers"]["namespace-name"]["command"] == "slash-free"


def test_merge_replaces_shifted_unsuffixed_alias() -> None:
    """Deleting the slash-free sibling must not keep its command under the alias."""
    from kiro_crew.agent import _merge_authored_mcp_payload

    existing = {
        "mcpServers": {
            "namespace-name": {"command": "slash-free"},
            "namespace-name-2": {"command": "slashed"},
        },
        "sourceServers": ["namespace-name", "namespace-name-2"],
        "tools": ["@namespace-name/search", "@namespace-name-2/search"],
        "allowedTools": ["@namespace-name/search"],
    }
    incoming = {
        "mcpServers": {},
        "sourceServers": ["namespace-name"],
        "tools": [],
        "allowedTools": [],
    }
    merged = _merge_authored_mcp_payload(
        existing, incoming, {"namespace-name": {"command": "slashed"}}
    )
    assert merged["mcpServers"]["namespace-name"]["command"] == "slashed"
    assert "slash-free" not in {
        spec.get("command") for spec in merged["mcpServers"].values() if isinstance(spec, dict)
    }
    assert "namespace-name-2" not in merged["mcpServers"]
    assert "@namespace-name/search" not in merged["tools"]
    assert "@namespace-name-2/search" not in merged["tools"]
    assert "@namespace-name/search" not in merged["allowedTools"]


def test_restore_drops_shifted_unsuffixed_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remaining slash key must not restore the deleted slash-free command."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    src = tmp_path / "kiro-mcp.json"
    src.write_text(
        json.dumps({"mcpServers": {"namespace/name": {"command": "slashed"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", src)
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "namespace-name": {"command": "slash-free"},
                    "namespace-name-2": {"command": "slashed"},
                },
                "sourceServers": ["namespace-name", "namespace-name-2"],
                "tools": ["@namespace-name/search", "@namespace-name-2/search"],
                "allowedTools": ["@namespace-name/search"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}, "tools": [], "allowedTools": []}
    assert _restore_authored_mcp(config) is True
    commands = {
        spec.get("command") for spec in config["mcpServers"].values() if isinstance(spec, dict)
    }
    assert "slash-free" not in commands
    assert config["mcpServers"].get("namespace-name", {}).get("command") != "slash-free"
    assert "@namespace-name/search" not in config["tools"]
    assert "@namespace-name-2/search" not in config["tools"]
    assert "@namespace-name/search" not in config["allowedTools"]


def test_empty_reconciliation_unlinks_authored_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _stash_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"gone": {"command": "x"}},
                "sourceServers": ["gone"],
            }
        ),
        encoding="utf-8",
    )
    _stash_authored_mcp({"mcpServers": {}, "tools": [], "allowedTools": []}, set())
    assert sidecar.exists() is False


def test_restore_drops_disabled_app_mcp_from_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "sourceServers": ["notes:tools"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}}
    assert _restore_authored_mcp(config) is True
    assert "notes:tools" not in config["mcpServers"]


def test_restore_applies_stash_over_merged_live_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until source baselines exist, stash wins even when dest equals live."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"custom": {"command": "npx", "args": ["--old"], "env": {"A": "1"}}}}
        ),
        encoding="utf-8",
    )
    crew = config_dir() / "mcp.json"
    crew.parent.mkdir(parents=True, exist_ok=True)
    crew.write_text(
        json.dumps({"mcpServers": {"custom": {"args": ["--new"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"custom": {"command": "npx", "args": ["--old"], "env": {"A": "1"}}},
                "sourceServers": ["custom"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {
        "mcpServers": {"custom": {"command": "npx", "args": ["--new"], "env": {"A": "1"}}}
    }
    assert _restore_authored_mcp(config) is True
    assert config["mcpServers"]["custom"] == {
        "command": "npx",
        "args": ["--old"],
        "env": {"A": "1"},
    }


def test_restore_applies_stash_over_live_source_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dest-equals-live is not a skip; stash is the durable copy until baselines."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"custom": {"command": "npx", "args": ["--new"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"custom": {"command": "npx"}},
                "sourceServers": ["custom"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {"custom": {"command": "npx", "args": ["--new"]}}}
    assert _restore_authored_mcp(config) is True
    assert config["mcpServers"]["custom"] == {"command": "npx"}


def test_restore_applies_agent_local_override_when_dest_equals_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override sharing a live source name must not be discarded on restore."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"custom": {"command": "npx"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "custom": {"command": "npx", "env": {"TOKEN": "keep"}, "args": ["--extra"]}
                },
                "sourceServers": ["custom"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {"custom": {"command": "npx"}}}
    assert _restore_authored_mcp(config) is True
    assert config["mcpServers"]["custom"] == {
        "command": "npx",
        "env": {"TOKEN": "keep"},
        "args": ["--extra"],
    }


def test_restore_keeps_source_edited_during_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live source edited while stashed keeps the edit; the stale stash copy loses."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"custom": {"command": "npx", "args": ["--new"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"custom": {"command": "npx", "args": ["--old"]}},
                "sourceServers": ["custom"],
                # Baseline says the source read ``--old`` when stashed; it now
                # reads ``--new``, so the operator edited it during login.
                "sourceBaselines": {"custom": {"command": "npx", "args": ["--old"]}},
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {"custom": {"command": "npx", "args": ["--new"]}}}
    restored: set[str] = set()
    assert _restore_authored_mcp(config, restored) is True
    assert config["mcpServers"]["custom"] == {"command": "npx", "args": ["--new"]}
    assert "custom" not in restored


def test_restore_applies_override_when_source_matches_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged source since stash: the agent-local override still comes back."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"custom": {"command": "npx"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"custom": {"command": "npx", "env": {"TOKEN": "keep"}}},
                "sourceServers": ["custom"],
                "sourceBaselines": {"custom": {"command": "npx"}},
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {"custom": {"command": "npx"}}}
    assert _restore_authored_mcp(config) is True
    assert config["mcpServers"]["custom"] == {"command": "npx", "env": {"TOKEN": "keep"}}


def test_stash_records_source_baselines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stash writer records each live source's definition for restore to compare."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _stash_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"custom": {"command": "npx", "args": ["--old"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    config: dict[str, Any] = {
        "mcpServers": {"custom": {"command": "npx", "args": ["--old"], "env": {"T": "1"}}}
    }
    _stash_authored_mcp(config, set())
    raw = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    assert raw["sourceBaselines"] == {"custom": {"command": "npx", "args": ["--old"]}}


def test_restore_keeps_stashed_override_when_dest_is_not_live_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stash still fills a dest spec that is not the current live source."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "npx"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "custom": {
                        "command": "custom-bin",
                        "env": {"TOKEN": "keep"},
                        "args": ["--extra"],
                    }
                },
                "sourceServers": [],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {"custom": {"command": "old-bin"}}}
    assert _restore_authored_mcp(config) is True
    assert config["mcpServers"]["custom"] == {
        "command": "custom-bin",
        "env": {"TOKEN": "keep"},
        "args": ["--extra"],
    }


def test_restore_drops_qualified_ref_when_source_server_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "sourceServers": ["notes:tools"],
                "tools": ["@notes:tools/search"],
                "allowedTools": ["@notes:tools/search"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}, "tools": [], "allowedTools": []}
    assert _restore_authored_mcp(config) is True
    assert "notes:tools" not in config["mcpServers"]
    assert "@notes:tools/search" not in config["tools"]
    assert "@notes:tools/search" not in config["allowedTools"]


def test_authored_mcp_directory_fences_atomic_write_temp() -> None:
    """A file-leaf classification would leave mkstemp siblings agent-writable."""
    from kiro_crew.security import is_sensitive_path

    assert is_sensitive_path("~/.kiro/crew/agentcore-authored-mcp/stash.json")
    assert is_sensitive_path("~/.kiro/crew/agentcore-authored-mcp/.stash.json.tmp")
    assert is_sensitive_path("~/.kirocrew/agentcore-authored-mcp/tmpXXXX")


def test_unlink_authored_mcp_sidecar_propagates_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked sidecar must fail the rebuild, not report success and restore later."""
    from kiro_crew import agent as agent_mod

    class _Locked:
        def unlink(self) -> None:
            raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(agent_mod, "_authored_mcp_path", lambda: _Locked())
    with pytest.raises(OSError):
        agent_mod._unlink_authored_mcp_sidecar()


def test_unlink_authored_mcp_sidecar_ignores_missing_file() -> None:
    from kiro_crew.agent import _unlink_authored_mcp_sidecar

    _unlink_authored_mcp_sidecar()


def test_login_withhold_true_when_governance_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import agent as agent_mod

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("governance unavailable")

    monkeypatch.setattr(agent_mod, "vet_and_audit", _boom)
    assert agent_mod._login_mcp_withhold() is True


def test_login_withhold_reads_the_effective_posture_not_the_ceiling_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CloudFormation instance configured through its unit environment has no
    ceiling document: the session attach path reads ``login`` from the env, so
    the rebuild's withhold gate must too -- a ceiling-only read would attach
    login Gateways while leaving non-managed MCP executable."""
    from types import SimpleNamespace

    from kiro_crew import agent as agent_mod
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setattr(
        agent_mod, "vet_and_audit", lambda *_a, **_k: SimpleNamespace(permitted=True)
    )
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: "")
    monkeypatch.setenv(aws_mod.ENV_POSTURE, "login")
    assert agent_mod._login_mcp_withhold() is True
    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    assert agent_mod._login_mcp_withhold() is False
    monkeypatch.delenv(aws_mod.ENV_POSTURE, raising=False)
    assert agent_mod._login_mcp_withhold() is False


def test_login_withhold_fails_closed_when_the_ceiling_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effective read's lenient mode says "off" for an unreadable ceiling;
    the withhold gate uses the strict mode so that case still withholds."""
    from types import SimpleNamespace

    from kiro_crew import agent as agent_mod
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setattr(
        agent_mod, "vet_and_audit", lambda *_a, **_k: SimpleNamespace(permitted=True)
    )

    def _boom() -> object:
        raise RuntimeError("unreadable policy")

    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", _boom)
    monkeypatch.delenv(aws_mod.ENV_POSTURE, raising=False)
    assert aws_mod.resolved_posture() == ""
    assert agent_mod._login_mcp_withhold() is True


def test_register_mcp_servers_skips_and_scrubs_under_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.bridges import _register_mcp_servers
    from kiro_crew.apps.manifest import AppManifest

    mcp_path = tmp_path / "kirocrew.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kirocrew-core": {"command": "core"},
                    "notes:tools": {"command": "stale"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: mcp_path)
    try:
        _enable("login")
        registered = _register_mcp_servers(
            "notes",
            AppManifest(name="notes", mcpServers={"tools": {"command": "notes-mcp"}}),
        )
    finally:
        reset_context()
    assert registered == []
    servers = json.loads(mcp_path.read_text(encoding="utf-8")).get("mcpServers") or {}
    assert "notes:tools" not in servers
    assert "kirocrew-core" in servers


def test_register_mcp_servers_rechecks_withhold_inside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login flipping after a pre-lock peek must still scrub, not write app MCP."""
    import contextlib

    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    mcp_path = tmp_path / "kirocrew.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kirocrew-core": {"command": "core"},
                    "notes:tools": {"command": "stale"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: mcp_path)
    withhold = {"on": False}
    monkeypatch.setattr("kiro_crew.agent._login_mcp_withhold", lambda: withhold["on"])
    real_lock = bridges._mcp_lock

    @contextlib.contextmanager
    def _lock_then_withhold(**_kwargs: object):
        with real_lock(**_kwargs):
            withhold["on"] = True
            yield

    monkeypatch.setattr(bridges, "_mcp_lock", _lock_then_withhold)
    try:
        _enable("workload")
        registered = bridges._register_mcp_servers(
            "notes",
            AppManifest(name="notes", mcpServers={"tools": {"command": "notes-mcp"}}),
        )
    finally:
        reset_context()
    assert registered == []
    servers = json.loads(mcp_path.read_text(encoding="utf-8")).get("mcpServers") or {}
    assert "notes:tools" not in servers
    assert "kirocrew-core" in servers


def test_reregister_app_mcp_servers_reports_login_withhold_unlanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health must not treat a withheld register as landed.

    Enable an HTTP-MCP app during login, then leave login: if reregister
    returns [] with an empty io_failures collector, `_gate_mcp_registration`
    records success and never retries.
    """
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    mcp_path = tmp_path / "kirocrew.json"
    mcp_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: mcp_path)
    monkeypatch.setattr(
        bridges,
        "_registration_source",
        lambda _n: (
            AppManifest(name="notes", mcpServers={"tools": {"command": "notes-mcp"}}),
            tmp_path,
        ),
    )
    monkeypatch.setattr(bridges, "_registration_denied", lambda name, action, app_root: None)
    try:
        _enable("login")
        collected: list[str] = []
        registered = bridges.reregister_app_mcp_servers("notes", io_failures=collected)
    finally:
        reset_context()
    assert registered == []
    assert collected == ["notes: login withhold"]


def test_gate_mcp_registration_unlanded_under_login_withhold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_gate_mcp_registration` returns False so mcp_healthy does not advance."""
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    mcp_path = tmp_path / "kirocrew.json"
    mcp_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: mcp_path)
    monkeypatch.setattr(
        bridges,
        "_registration_source",
        lambda _n: (
            AppManifest(name="notes", mcpServers={"tools": {"command": "notes-mcp"}}),
            tmp_path,
        ),
    )
    monkeypatch.setattr(bridges, "_registration_denied", lambda name, action, app_root: None)
    try:
        _enable("login")
        landed = bmod._gate_mcp_registration("notes", 9100, healthy=True)
    finally:
        reset_context()
    assert landed is False


def _install_notes_app_with_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a notes app whose agent embeds an MCP command. Returns agents dir."""
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app

    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_agents)
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(
        json.dumps({"mcpServers": {"notes:tools": {"command": "notes-mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: mcp_path)
    src = tmp_path / "source" / "notes"
    src.mkdir(parents=True)
    (src / "agents").mkdir()
    (src / "agents" / "scribe.json").write_text(
        json.dumps(
            {
                "name": "scribe",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "tools": ["@notes:tools"],
            }
        ),
        encoding="utf-8",
    )
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "notes",
                "version": "1.0.0",
                "displayName": "Notes",
                "description": "test",
                "author": "tester",
                "agents": ["agents/scribe.json"],
                "mcpServers": {"tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    result = install_app(src)
    assert result.ok, result.error
    return kiro_agents


def test_register_agents_strips_embedded_mcp_under_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kiro-cli loads app-agent mcpServers even after mcp.json is scrubbed."""
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
    from kiro_crew.apps.manifest import AppManifest

    kiro_agents = _install_notes_app_with_agent(tmp_path, monkeypatch)
    app_root = Path(os.environ["KIROCREW_HOME"]) / "apps" / "notes"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    try:
        _enable("login")
        registered = bridges._register_agents("notes", manifest, app_root)
    finally:
        reset_context()
    assert registered
    written = json.loads((kiro_agents / "notes--scribe.json").read_text(encoding="utf-8"))
    commands = {
        spec.get("command")
        for spec in (written.get("mcpServers") or {}).values()
        if isinstance(spec, dict)
    }
    assert "notes-mcp" not in commands
    assert written.get("includeMcpJson") is False


def test_register_agents_strips_policy_grant_under_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Policy merge must not re-copy an ambient command after the login wipe."""
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
    from kiro_crew.apps.manifest import AppManifest

    kiro_agents = _install_notes_app_with_agent(tmp_path, monkeypatch)
    app_root = Path(os.environ["KIROCREW_HOME"]) / "apps" / "notes"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    monkeypatch.setattr(
        bridges,
        "_agent_mcp_policy",
        lambda _name: {"agents": {"scribe": {"servers": {"ambient-grant": {}}}}},
    )
    monkeypatch.setattr(
        bridges,
        "_global_mcp_specs",
        lambda: {"ambient-grant": {"command": "ambient-mcp"}},
    )
    try:
        _enable("login")
        registered = bridges._register_agents("notes", manifest, app_root)
    finally:
        reset_context()
    assert registered
    written = json.loads((kiro_agents / "notes--scribe.json").read_text(encoding="utf-8"))
    commands = {
        spec.get("command")
        for spec in (written.get("mcpServers") or {}).values()
        if isinstance(spec, dict)
    }
    assert "ambient-mcp" not in commands
    assert "notes-mcp" not in commands
    assert written.get("includeMcpJson") is False


def test_login_rebuild_aborts_when_app_agent_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed refresh writes the filtered host spec, then empties leftover MCP."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    original = kiro_dir / "notes--scribe.json"
    original.write_text(
        json.dumps(
            {
                "name": "scribe",
                "model": "auto",
                "description": "hand-tuned scribe",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": True}],
    )

    def _fail(name: str, io_failures: list[str] | None = None, **_kwargs: object) -> list[str]:
        if io_failures is not None:
            io_failures.append(f"{name}: unwritable")
        return []

    monkeypatch.setattr("kiro_crew.apps.bridges.refresh_app_agents", _fail)
    try:
        _enable("login")
        with pytest.raises(RuntimeError, match="app-agent refresh failed"):
            rebuild_agent_config()
    finally:
        reset_context()
    runtime = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = runtime.get("mcpServers") or {}
    assert "dummy-leftover" not in servers
    assert "kirocrew-core" in servers
    leftover = json.loads(original.read_text(encoding="utf-8"))
    assert leftover.get("mcpServers") == {}
    assert leftover.get("includeMcpJson") is False
    assert leftover.get("model") == "auto"
    assert leftover.get("description") == "hand-tuned scribe"


def test_login_rebuild_scrubs_stale_app_agent_when_refresh_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed app agent reports no I/O failure; the leftover must still go."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "scribe",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": True}],
    )
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: [],
    )
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()
    assert leftover.exists() is False


def test_login_rebuild_skips_self_managed_app_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apps with resources=app keep their own agents when refresh is a no-op."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    planted = kiro_dir / "notes--scribe.json"
    body = {
        "name": "scribe",
        "model": "auto",
        "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
    }
    planted.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": True, "resources": "app"}],
    )
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: pytest.fail(
            "must not rematerialize a self-managed app"
        ),
    )
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()
    assert planted.exists() is True
    leftover_body = json.loads(planted.read_text(encoding="utf-8"))
    assert leftover_body == body


def test_login_rebuild_scrubs_disabled_app_agent_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover that survived disable-unlink must still lose its command."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "scribe",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": False}],
    )
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: pytest.fail(
            "must not rematerialize a disabled app"
        ),
    )
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()
    assert leftover.exists() is False


def test_login_rebuild_neutralizes_disabled_app_when_prune_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed disable unlink then failed prune must still empty leftover MCP."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "scribe",
                "model": "auto",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": False}],
    )
    monkeypatch.setattr(
        "kiro_crew.agent._prune_unkept_app_agent_files",
        lambda name, keep: ["notes--scribe.json"],
    )
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: pytest.fail(
            "must not rematerialize a disabled app"
        ),
    )
    try:
        _enable("login")
        with pytest.raises(RuntimeError, match="leftover notes--scribe.json"):
            rebuild_agent_config()
    finally:
        reset_context()
    assert leftover.exists() is True
    leftover_body = json.loads(leftover.read_text(encoding="utf-8"))
    assert leftover_body.get("mcpServers") == {}
    assert leftover_body.get("includeMcpJson") is False
    assert leftover_body.get("model") == "auto"


def test_login_rebuild_leaves_unlisted_app_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unclaimed ``--`` filename is not a leftover; custom agents stay."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover_body = {
        "name": "scribe",
        "model": "auto",
        "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
    }
    leftover.write_text(json.dumps(leftover_body), encoding="utf-8")
    custom = kiro_dir / "research--local.json"
    custom_body = {
        "name": "research-local",
        "mcpServers": {"research:tools": {"command": "research-mcp"}},
    }
    custom.write_text(json.dumps(custom_body), encoding="utf-8")
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", lambda: [])
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: pytest.fail(
            "must not rematerialize an unlisted app"
        ),
    )
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()
    assert leftover.exists() is True
    assert json.loads(leftover.read_text(encoding="utf-8")) == leftover_body
    assert json.loads(custom.read_text(encoding="utf-8")) == custom_body
    host = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    assert "dummy-leftover" not in (host.get("mcpServers") or {})


def test_login_rebuild_reuses_prewrite_catalog_when_later_list_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog loaded before the host write is reused for neutralize."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "scribe",
                "model": "auto",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)

    def _list() -> list[dict[str, object]]:
        host = kiro_dir / "kirocrew.json"
        if host.is_file():
            try:
                runtime = json.loads(host.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                runtime = {}
            servers = runtime.get("mcpServers") or {}
            # Filtered host write is the post-catalog window: a later
            # list_apps failure must not skip neutralize of listed apps.
            if "dummy-leftover" not in servers and "kirocrew-core" in servers:
                raise OSError("apps dir unreadable")
        return [{"name": "notes", "enabled": True}]

    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", _list)

    def _fail(name: str, io_failures: list[str] | None = None, **_kwargs: object) -> list[str]:
        if io_failures is not None:
            io_failures.append(f"{name}: unwritable")
        return []

    monkeypatch.setattr("kiro_crew.apps.bridges.refresh_app_agents", _fail)
    try:
        _enable("login")
        with pytest.raises(RuntimeError, match="app-agent refresh failed"):
            rebuild_agent_config()
    finally:
        reset_context()
    leftover_body = json.loads(leftover.read_text(encoding="utf-8"))
    assert leftover_body.get("mcpServers") == {}
    assert leftover_body.get("includeMcpJson") is False
    assert leftover_body.get("model") == "auto"
    host = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    assert "dummy-leftover" not in (host.get("mcpServers") or {})


def test_login_rebuild_aborts_when_catalog_fails_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pre-write catalog still writes the host spec and leaves custom agents."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover_body = {
        "name": "scribe",
        "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
    }
    leftover.write_text(json.dumps(leftover_body), encoding="utf-8")
    custom = kiro_dir / "research--local.json"
    custom_body = {
        "name": "research-local",
        "mcpServers": {"research:tools": {"command": "research-mcp"}},
    }
    custom.write_text(json.dumps(custom_body), encoding="utf-8")
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)

    def _boom() -> list[dict[str, object]]:
        raise OSError("apps dir unreadable")

    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", _boom)
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: pytest.fail(
            "must not rematerialize when the catalog failed"
        ),
    )
    try:
        _enable("login")
        with pytest.raises(RuntimeError, match="app-agent catalog unavailable"):
            rebuild_agent_config()
    finally:
        reset_context()
    assert json.loads(leftover.read_text(encoding="utf-8")) == leftover_body
    assert json.loads(custom.read_text(encoding="utf-8")) == custom_body
    host = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    assert "dummy-leftover" not in (host.get("mcpServers") or {})
    assert "kirocrew-core" in (host.get("mcpServers") or {})


def test_login_rebuild_aborts_when_an_installed_app_is_missing_from_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_apps`` drops an app whose installed.json will not parse. Under Login
    that app's materialized agents would then never be refreshed and their
    leftover commands would stay loadable -- so the catalog load fails closed
    when an app directory carries an installed.json the listing omitted."""
    from kiro_crew.agent import _load_app_catalog, rebuild_agent_config
    from kiro_crew.apps import bridges, manager

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    apps_root = tmp_path / "apps"
    (apps_root / "notes").mkdir(parents=True)
    (apps_root / "notes" / manager.INSTALLED_META_FILENAME).write_text(
        "{not json", encoding="utf-8"
    )
    # A directory with NO installed.json is not an installed app and is ignored.
    (apps_root / "scratch").mkdir()
    monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(manager, "detect_orphaned_builtins", lambda: set())

    with pytest.raises(RuntimeError, match="installed.json unreadable for: notes"):
        _load_app_catalog()

    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: pytest.fail(
            "must not rematerialize when the catalog is incomplete"
        ),
    )
    try:
        _enable("login")
        with pytest.raises(RuntimeError, match="app-agent catalog unavailable"):
            rebuild_agent_config()
    finally:
        reset_context()

    # Repairing the file makes the same directory a listed app again.
    (apps_root / "notes" / manager.INSTALLED_META_FILENAME).write_text(
        json.dumps({"name": "notes", "version": "1.0.0", "enabled": True}), encoding="utf-8"
    )
    names = [info["name"] for info in _load_app_catalog()]
    assert names == ["notes"]


def test_login_rebuild_aborts_when_stale_agent_prune_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swallowed prune unlink must not leave the withheld command executable."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "notes--scribe.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "scribe",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": True}],
    )
    monkeypatch.setattr(
        "kiro_crew.agent._prune_unkept_app_agent_files",
        lambda name, keep: ["notes--scribe.json"],
    )
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.refresh_app_agents",
        lambda name, io_failures=None, **_kwargs: [],
    )
    try:
        _enable("login")
        with pytest.raises(RuntimeError, match="leftover notes--scribe.json"):
            rebuild_agent_config()
    finally:
        reset_context()
    assert leftover.exists() is True
    leftover_body = json.loads(leftover.read_text(encoding="utf-8"))
    assert leftover_body.get("mcpServers") == {}
    assert leftover_body.get("includeMcpJson") is False


def test_restore_keeps_sidecar_when_validation_drops_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation drop must not delete the sole durable stash copy."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    missing = tmp_path / "no-such-mcp-bin"
    monkeypatch.setattr(
        "shutil.which",
        lambda cmd, path=None: None if cmd == str(missing) else sys.executable,
    )
    sidecar.write_text(
        json.dumps({"mcpServers": {"stash-only": {"command": str(missing)}}}),
        encoding="utf-8",
    )
    try:
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()
    assert sidecar.exists()
    runtime = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "stash-only" not in runtime


def test_register_mcp_servers_rematerializes_app_agents_under_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withhold must strip an already-materialized app-agent command."""
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    kiro_agents = _install_notes_app_with_agent(tmp_path, monkeypatch)
    prior = kiro_agents / "notes--scribe.json"
    prior.write_text(
        json.dumps(
            {
                "name": "scribe",
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "tools": ["@notes:tools"],
            }
        ),
        encoding="utf-8",
    )
    try:
        _enable("login")
        registered = bridges._register_mcp_servers(
            "notes",
            AppManifest(
                name="notes",
                agents=["agents/scribe.json"],
                mcpServers={"tools": {"command": "notes-mcp"}},
            ),
        )
    finally:
        reset_context()
    assert registered == []
    written = json.loads(prior.read_text(encoding="utf-8"))
    commands = {
        spec.get("command")
        for spec in (written.get("mcpServers") or {}).values()
        if isinstance(spec, dict)
    }
    assert "notes-mcp" not in commands


def test_stash_replaces_unreadable_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable stash must be replaced with the live extract."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _stash_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{not-json", encoding="utf-8")
    _stash_authored_mcp(
        {"mcpServers": {"keep": {"command": "x"}}, "tools": [], "allowedTools": []},
        set(),
    )
    body = json.loads(sidecar.read_text(encoding="utf-8"))
    assert body["mcpServers"]["keep"]["command"] == "x"


def test_login_rebuild_retracts_after_replacing_unreadable_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt stash is replaced, then leftover runtime MCP is retracted."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{not-json", encoding="utf-8")
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()
    runtime = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = runtime.get("mcpServers") or {}
    assert "dummy-leftover" not in servers
    assert "kirocrew-core" in servers
    stash = json.loads(sidecar.read_text(encoding="utf-8"))
    assert (stash.get("mcpServers") or {}).get("dummy-leftover", {}).get("command") == "dummy-srv"


def test_login_rebuild_keeps_runtime_when_stash_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed sidecar replace must not retract the only remaining copy."""
    import kiro_crew.agent as agent_mod
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{not-json", encoding="utf-8")
    real = agent_mod.atomic_write

    def _boom(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name == "stash.json":
            raise OSError("stash replace failed")
        real(path, *args, **kwargs)

    monkeypatch.setattr(agent_mod, "atomic_write", _boom)
    try:
        _enable("login")
        with pytest.raises(OSError, match="stash replace failed"):
            rebuild_agent_config()
    finally:
        reset_context()
    runtime = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    assert "dummy-leftover" in (runtime.get("mcpServers") or {})
    assert sidecar.read_text(encoding="utf-8") == "{not-json"


def test_workload_rebuild_restores_app_agent_mcp_after_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave-login must rematerialize app-agent commands, not only host MCP."""
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
    from kiro_crew.apps.manifest import AppManifest

    kiro_agents = _install_notes_app_with_agent(tmp_path, monkeypatch)
    _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(bridges, "_registration_denied", lambda name, action, app_root: None)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": True}],
    )
    app_root = Path(os.environ["KIROCREW_HOME"]) / "apps" / "notes"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    try:
        _enable("workload")
        assert bridges._register_agents("notes", manifest, app_root)
    finally:
        reset_context()
    agent_file = kiro_agents / "notes--scribe.json"
    try:
        _enable("login")
        rebuild_agent_config()
        stripped = json.loads(agent_file.read_text(encoding="utf-8"))
        commands = {
            spec.get("command")
            for spec in (stripped.get("mcpServers") or {}).values()
            if isinstance(spec, dict)
        }
        assert "notes-mcp" not in commands
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()
    restored = json.loads(agent_file.read_text(encoding="utf-8"))
    commands = {
        spec.get("command")
        for spec in (restored.get("mcpServers") or {}).values()
        if isinstance(spec, dict)
    }
    assert "notes-mcp" in commands


def _materialize_scribe_with_user_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install notes, rematerialize scribe, then write user-owned fields."""
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
    from kiro_crew.apps.manifest import AppManifest

    kiro_agents = _install_notes_app_with_agent(tmp_path, monkeypatch)
    _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(bridges, "_registration_denied", lambda name, action, app_root: None)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.list_apps",
        lambda: [{"name": "notes", "enabled": True}],
    )
    app_root = Path(os.environ["KIROCREW_HOME"]) / "apps" / "notes"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    try:
        _enable("workload")
        assert bridges._register_agents("notes", manifest, app_root)
    finally:
        reset_context()
    agent_file = kiro_agents / "notes--scribe.json"
    data = json.loads(agent_file.read_text(encoding="utf-8"))
    data["model"] = "auto"
    data["description"] = "hand-tuned scribe"
    data["toolsSettings"] = {"custom": True}
    agent_file.write_text(json.dumps(data), encoding="utf-8")
    return agent_file


def test_login_rebuild_preserves_user_app_agent_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed unlink must not discard model/description/toolsSettings."""
    from kiro_crew.agent import rebuild_agent_config

    agent_file = _materialize_scribe_with_user_edits(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()
    written = json.loads(agent_file.read_text(encoding="utf-8"))
    assert written.get("model") == "auto"
    assert written.get("description") == "hand-tuned scribe"
    assert written.get("toolsSettings") == {"custom": True}
    commands = {
        spec.get("command")
        for spec in (written.get("mcpServers") or {}).values()
        if isinstance(spec, dict)
    }
    assert "notes-mcp" not in commands


def test_leave_login_rebuild_preserves_user_app_agent_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave-login rematerialize must keep user-owned fields in place."""
    from kiro_crew.agent import rebuild_agent_config

    agent_file = _materialize_scribe_with_user_edits(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()
    written = json.loads(agent_file.read_text(encoding="utf-8"))
    assert written.get("model") == "auto"
    assert written.get("description") == "hand-tuned scribe"
    assert written.get("toolsSettings") == {"custom": True}
    commands = {
        spec.get("command")
        for spec in (written.get("mcpServers") or {}).values()
        if isinstance(spec, dict)
    }
    assert "notes-mcp" in commands


def test_gateway_boot_skips_app_agent_refresh() -> None:
    """Leftover-verify is usage-scaled; boot rebuild must not wait on it."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._init_services)
    assert "app_agent_refresh=False" in src
    assert 'refresh_forks="defer"' in src
    assert "path = rebuild_agent_config()" not in src


def _app_catalog(*names: str) -> list[dict[str, object]]:
    return [{"name": n, "enabled": True, "resources": "agents"} for n in names]


def test_neutralize_leaves_unreadable_agent_file_untouched_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting an unparseable file from ``{}`` would erase the user's model /
    description to neutralize a command kiro-cli could never have run. Leave
    it, report it, and let the neutralize pass raise with the file named."""
    from kiro_crew.agent import _neutralize_app_agent_file, _neutralize_unrefreshed_app_agents
    from kiro_crew.apps import bridges

    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    corrupt = kiro_dir / "notes--scribe.json"
    corrupt.write_text('{"name": "scribe", "model": "sonnet", "mcpServers": {', encoding="utf-8")
    assert _neutralize_app_agent_file("notes--scribe.json") is False
    assert corrupt.read_text(encoding="utf-8").startswith('{"name": "scribe", "model": "sonnet"')
    with pytest.raises(
        RuntimeError, match="leftover MCP could not be neutralized on: notes--scribe.json"
    ):
        _own(monkeypatch, {"notes": {"notes--scribe.json"}})
        _neutralize_unrefreshed_app_agents(apps=_app_catalog("notes"))


def test_login_register_neutralizes_when_rematerialize_fails_and_raises_when_it_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``io_failures=None`` must not turn a failed rematerialize into a silent
    ``[]``: the leftover command is emptied, or the register raises."""
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest

    mcp_path = tmp_path / "kirocrew.json"
    mcp_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: mcp_path)
    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    leftover = kiro_dir / "notes--scribe.json"
    leftover.write_text(
        json.dumps({"name": "scribe", "mcpServers": {"notes:tools": {"command": "notes-mcp"}}}),
        encoding="utf-8",
    )

    def _boom(*_a: object, **_k: object) -> list[str]:
        raise OSError("agents dir read-only")

    monkeypatch.setattr(bridges, "_register_agents", _boom)
    monkeypatch.setattr(bridges, "_app_resource_root", lambda _n: tmp_path)
    _own(monkeypatch, {"notes": {"notes--scribe.json"}})
    manifest = AppManifest(name="notes", mcpServers={"tools": {"command": "notes-mcp"}})
    try:
        _enable("login")
        # Rematerialize failed; the leftover is neutralized instead (no collector passed).
        assert bridges._register_mcp_servers("notes", manifest) == []
        body = json.loads(leftover.read_text(encoding="utf-8"))
        assert body["mcpServers"] == {}
        assert body["includeMcpJson"] is False
        assert body["name"] == "scribe"
        # Now the leftover cannot be neutralized either: the register raises.
        leftover.write_text(
            json.dumps({"name": "scribe", "mcpServers": {"notes:tools": {"command": "notes-mcp"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.agent._atomic_json_write", _boom)
        with pytest.raises(RuntimeError, match="leftover MCP remains on: notes--scribe.json"):
            bridges._register_mcp_servers("notes", manifest)
    finally:
        reset_context()


def test_require_login_withhold_refuses_app_agent_still_carrying_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn is the last gate: an app agent whose spec is not the exact shape a
    login rebuild leaves (includeMcpJson pinned false; only host-managed servers,
    each with the host's own invocation) does not start, nor does one that cannot
    be read. A managed NAME over a foreign command is a spoof, not a pass. Custom
    agents, the host agent, and Off/Workload postures are untouched."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.agent import LoginWithholdUnresolved, require_login_withhold
    from kiro_crew.apps import bridges

    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._load_app_catalog", lambda: _app_catalog("notes"))
    _own(
        monkeypatch,
        {
            "notes": {
                "notes--scribe.json",
                "notes--clean.json",
                "notes--spoof.json",
                "notes--autoapprove.json",
                "notes--env.json",
                "notes--narrowed.json",
                "notes--leaky.json",
                "notes--broken.json",
            }
        },
    )
    monkeypatch.setitem(
        agent_mod._MANAGED_MCP_SERVERS,
        "kirocrew-core",
        {
            **agent_mod._MANAGED_MCP_SERVERS["kirocrew-core"],
            "invocation_fn": lambda: ("core", ["--x"]),
        },
    )

    def _write(name: str, body: dict[str, Any]) -> None:
        (kiro_dir / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")

    _write(
        "notes--scribe",
        {
            "name": "scribe",
            "includeMcpJson": False,
            "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
        },
    )
    _write(
        "notes--clean",
        {
            "name": "clean",
            "includeMcpJson": False,
            "mcpServers": {"kirocrew-core": {"command": "core", "args": ["--x"]}},
        },
    )
    _write(
        "notes--spoof",
        {
            "name": "spoof",
            "includeMcpJson": False,
            "mcpServers": {"kirocrew-core": {"command": "evil", "args": []}},
        },
    )
    # Canonical invocation, plus a grant / knob the login shape never emits.
    _write(
        "notes--autoapprove",
        {
            "name": "autoapprove",
            "includeMcpJson": False,
            "mcpServers": {
                "kirocrew-core": {"command": "core", "args": ["--x"], "autoApprove": ["search"]}
            },
        },
    )
    _write(
        "notes--env",
        {
            "name": "env",
            "includeMcpJson": False,
            "mcpServers": {
                "kirocrew-core": {
                    "command": "core",
                    "args": ["--x"],
                    "env": {"LD_PRELOAD": "/x.so"},
                }
            },
        },
    )
    # A policy NARROWING is fine.
    _write(
        "notes--narrowed",
        {
            "name": "narrowed",
            "includeMcpJson": False,
            "mcpServers": {
                "kirocrew-core": {"command": "core", "args": ["--x"], "disabledTools": ["shell"]}
            },
        },
    )
    _write("notes--leaky", {"name": "leaky", "mcpServers": {}})  # includeMcpJson omitted
    (kiro_dir / "notes--broken.json").write_text("{nope", encoding="utf-8")
    _write(
        "research--local",
        {"name": "research-local", "mcpServers": {"r:tools": {"command": "research-mcp"}}},
    )
    try:
        _enable("workload")
        require_login_withhold("notes--scribe", tmp_path)  # not login: no gate
        reset_context()
        _enable("login")
        with pytest.raises(LoginWithholdUnresolved, match="non-managed server 'notes:tools'"):
            require_login_withhold("notes--scribe", tmp_path)
        with pytest.raises(LoginWithholdUnresolved, match="does not match the host's invocation"):
            require_login_withhold("notes--spoof", tmp_path)
        with pytest.raises(
            LoginWithholdUnresolved, match="fields the login shape does not \\(autoApprove\\)"
        ):
            require_login_withhold("notes--autoapprove", tmp_path)
        with pytest.raises(
            LoginWithholdUnresolved, match="fields the login shape does not \\(env\\)"
        ):
            require_login_withhold("notes--env", tmp_path)
        require_login_withhold("notes--narrowed", tmp_path)  # disabledTools narrows; allowed
        with pytest.raises(LoginWithholdUnresolved, match="includeMcpJson is not pinned false"):
            require_login_withhold("notes--leaky", tmp_path)
        with pytest.raises(LoginWithholdUnresolved, match="unreadable"):
            require_login_withhold("notes--broken", tmp_path)
        require_login_withhold("notes--clean", tmp_path)  # exact login shape
        require_login_withhold("research--local", tmp_path)  # not an app agent
        require_login_withhold("kirocrew", tmp_path)
        require_login_withhold("", tmp_path)
        # A project-local shadow of the app agent's name is what kiro-cli would
        # execute; it is the copy that is read.
        shadow = tmp_path / ".kiro" / "agents"
        shadow.mkdir(parents=True)
        (shadow / "notes--clean.json").write_text(
            json.dumps(
                {
                    "name": "clean",
                    "includeMcpJson": False,
                    "mcpServers": {"x": {"url": "https://x.example/mcp"}},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(LoginWithholdUnresolved, match="non-managed server 'x'"):
            require_login_withhold("notes--clean", tmp_path)
    finally:
        reset_context()


def test_spawn_paths_call_the_login_withhold_gate() -> None:
    import inspect

    from kiro_crew.acp import client
    from kiro_crew.acp.harness import kas, kiro

    for mod in (client, kiro, kas):
        src = inspect.getsource(mod)
        assert "require_login_withhold" in src
        assert "LoginWithholdUnresolved" in src


def test_login_rebuild_never_touches_a_users_prefix_named_custom_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<installed-app>--mine.json`` is the user's, not the app's. Ownership is
    the manifest's: the login rebuild neither prunes nor neutralizes it, and the
    spawn gate lets it start with its own MCP (custom agents keep theirs)."""
    from kiro_crew.agent import (
        _neutralize_unrefreshed_app_agents,
        _prune_unkept_app_agent_files,
        require_login_withhold,
    )
    from kiro_crew.apps import bridges

    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._load_app_catalog", lambda: _app_catalog("notes"))
    _own(monkeypatch, {"notes": {"notes--scribe.json"}})
    owned_body = {"name": "scribe", "mcpServers": {"notes:tools": {"command": "notes-mcp"}}}
    mine_body = {"name": "mine", "mcpServers": {"my:tools": {"command": "my-mcp"}}}
    (kiro_dir / "notes--scribe.json").write_text(json.dumps(owned_body), encoding="utf-8")
    mine = kiro_dir / "notes--mine.json"
    mine.write_text(json.dumps(mine_body), encoding="utf-8")

    # Prune with nothing kept: only the OWNED file goes.
    assert _prune_unkept_app_agent_files("notes", set()) == []
    assert not (kiro_dir / "notes--scribe.json").exists()
    assert json.loads(mine.read_text(encoding="utf-8")) == mine_body
    # Neutralize: nothing owned is left, nothing else is touched.
    (kiro_dir / "notes--scribe.json").write_text(json.dumps(owned_body), encoding="utf-8")
    _neutralize_unrefreshed_app_agents(apps=_app_catalog("notes"))
    assert (
        json.loads((kiro_dir / "notes--scribe.json").read_text(encoding="utf-8"))["mcpServers"]
        == {}
    )
    assert json.loads(mine.read_text(encoding="utf-8")) == mine_body
    try:
        _enable("login")
        require_login_withhold("notes--mine", tmp_path)  # custom: not gated
    finally:
        reset_context()


def test_unknown_app_ownership_fails_closed_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app whose manifest cannot be read owns an UNKNOWN set of files: the
    login paths name the app and fail instead of falling back to the prefix."""
    from kiro_crew.agent import (
        AppAgentOwnershipUnknown,
        LoginWithholdUnresolved,
        _neutralize_unrefreshed_app_agents,
        _remaining_app_agent_names,
        require_login_withhold,
    )
    from kiro_crew.apps import bridges

    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._load_app_catalog", lambda: _app_catalog("notes"))
    monkeypatch.setattr("kiro_crew.apps.bridges.owned_app_agent_names", lambda _n: None)
    (kiro_dir / "notes--scribe.json").write_text(
        json.dumps({"name": "scribe", "mcpServers": {"notes:tools": {"command": "x"}}}),
        encoding="utf-8",
    )
    with pytest.raises(AppAgentOwnershipUnknown, match="notes"):
        _remaining_app_agent_names("notes")
    with pytest.raises(RuntimeError, match="notes: app notes: manifest"):
        _neutralize_unrefreshed_app_agents(apps=_app_catalog("notes"))
    # The file was not guessed at.
    assert json.loads((kiro_dir / "notes--scribe.json").read_text(encoding="utf-8"))["mcpServers"]
    try:
        _enable("login")
        with pytest.raises(LoginWithholdUnresolved, match="manifest unreadable"):
            require_login_withhold("notes--scribe", tmp_path)
    finally:
        reset_context()


def test_gate_still_owns_a_stale_agent_the_manifest_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade removes `scribe` from notes' manifest and the prune fails, so
    ``notes--scribe.json`` (old MCP intact) stays on disk. The manifest omits it,
    but the ownership ledger records it: the gate refuses it under Login,
    while a never-owned ``notes--mine.json`` is still the user's."""
    from kiro_crew.agent import LoginWithholdUnresolved, require_login_withhold
    from kiro_crew.apps import bridges

    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._load_app_catalog", lambda: _app_catalog("notes"))
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    # Manifest side: `notes` now declares nothing. Ledger side: the writer had
    # recorded scribe when it materialized it.
    monkeypatch.setattr(bridges, "manifest_agent_link_names", lambda *_a: set())
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest", lambda _n: object()  # present, readable
    )
    monkeypatch.setattr(bridges, "_app_resource_root", lambda _n: tmp_path)
    bridges._record_app_agent_ownership("notes", {"notes--scribe.json"})
    stale = {"name": "scribe", "mcpServers": {"notes:tools": {"command": "notes-mcp"}}}
    (kiro_dir / "notes--scribe.json").write_text(json.dumps(stale), encoding="utf-8")
    (kiro_dir / "notes--mine.json").write_text(
        json.dumps({"name": "mine", "mcpServers": {"my:tools": {"command": "my-mcp"}}}),
        encoding="utf-8",
    )
    assert bridges.owned_app_agent_names("notes") == {"notes--scribe.json"}
    try:
        _enable("login")
        with pytest.raises(LoginWithholdUnresolved, match="notes--scribe"):
            require_login_withhold("notes--scribe", tmp_path)
        require_login_withhold("notes--mine", tmp_path)  # never owned: the user's
        # App uninstalled altogether: the catalog omits it, but the
        # ledger still names the file the framework wrote -> still gated.
        monkeypatch.setattr("kiro_crew.agent._load_app_catalog", lambda: [])
        with pytest.raises(LoginWithholdUnresolved, match="notes--scribe"):
            require_login_withhold("notes--scribe", tmp_path)
        require_login_withhold("notes--mine", tmp_path)
        # Unreadable ledger: unknown, not empty.
        bridges._app_agent_ledger_path().write_text("{not json", encoding="utf-8")
        with pytest.raises(LoginWithholdUnresolved, match="ledger unreadable"):
            require_login_withhold("notes--mine", tmp_path)
    finally:
        reset_context()


def test_ownership_ledger_records_on_write_and_forgets_on_framework_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges

    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    assert bridges._recorded_app_agent_names("notes") == set()
    bridges._record_app_agent_ownership("notes", {"notes--scribe.json"})
    bridges._record_app_agent_ownership("notes", {"notes--clerk.json"})
    bridges._record_app_agent_ownership("other", {"other--x.json"})
    assert bridges._recorded_app_agent_names("notes") == {"notes--scribe.json", "notes--clerk.json"}
    # Only the framework's own unlink forgets; a name still recorded stays.
    bridges.forget_app_agent_ownership("notes", {"notes--clerk.json"})
    assert bridges._recorded_app_agent_names("notes") == {"notes--scribe.json"}
    bridges.forget_app_agent_ownership("notes", {"notes--scribe.json"})
    assert bridges._recorded_app_agent_names("notes") == set()
    assert bridges._recorded_app_agent_names("other") == {"other--x.json"}
    # The ledger lives at the apps root, not in kiro-cli's agents dir.
    # The ledger is a keystone: it lives in its own crew-home directory that is
    # on the sensitive-path floor (agent file tools) AND bind-masked from every
    # sandboxed process -- not the apps root, not the agents dir, not trust/.
    import os
    import stat

    from kiro_crew import sandbox
    from kiro_crew.security import is_sensitive_path
    from kiro_crew.security.paths import APP_AGENT_TRUST_DIR_NAME

    path = bridges._app_agent_ledger_path()
    assert path.parent == tmp_path / APP_AGENT_TRUST_DIR_NAME
    if os.name == "posix":  # mode bits are POSIX; Windows uses the DACL
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    home = os.path.expanduser("~/.kiro/crew")
    assert is_sensitive_path(f"{home}/{APP_AGENT_TRUST_DIR_NAME}/ownership.json")
    assert APP_AGENT_TRUST_DIR_NAME in sandbox._CREW_HIDDEN_LEAVES
    assert APP_AGENT_TRUST_DIR_NAME not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


def test_unreadable_ownership_ledger_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger that fails to parse still holds every other app's records in its
    bytes. Recording must refuse (the writer treats it as an I/O failure for that
    agent) rather than start a fresh map over it; forgetting is a no-op."""
    from kiro_crew.apps import bridges

    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    path = bridges._app_agent_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = '{"other": ["other--x.json"], "notes": ["notes--scr'  # truncated write
    path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(bridges.AppAgentLedgerUnavailable, match="unreadable"):
        bridges._record_app_agent_ownership("notes", {"notes--scribe.json"})
    assert isinstance(bridges.AppAgentLedgerUnavailable("x"), OSError)  # the writer's path
    assert path.read_text(encoding="utf-8") == corrupt
    bridges.forget_app_agent_ownership("other", {"other--x.json"})
    assert path.read_text(encoding="utf-8") == corrupt
    # Reads report unknown, not empty.
    assert bridges._recorded_app_agent_names("other") is None
    # A record that cannot be written is refused the same way.
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bridges, "atomic_write", _raise_oserror)
    with pytest.raises(bridges.AppAgentLedgerUnavailable, match="could not record"):
        bridges._record_app_agent_ownership("notes", {"notes--scribe.json"})


def _raise_oserror(*_a, **_k):
    raise OSError("disk full")


def test_gate_judges_the_file_kiro_cli_resolves_by_declared_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kiro-cli selects ``--agent scribe`` by the spec's declared ``name``, and
    an app agent materialized as ``notes--scribe.json`` declares ``scribe``. The
    gate must judge THAT file, so a bare name -- no ``--`` in it -- is never an
    exemption. A custom spec selected by its declared name stays the user's; a
    name two specs declare is refused as undefined."""
    from kiro_crew.agent import LoginWithholdUnresolved, require_login_withhold
    from kiro_crew.apps import bridges

    kiro_dir = tmp_path / "agents"
    kiro_dir.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path / "home")
    monkeypatch.setattr("kiro_crew.agent._load_app_catalog", lambda: _app_catalog("notes"))
    _own(monkeypatch, {"notes": {"notes--scribe.json"}})
    (kiro_dir / "notes--scribe.json").write_text(
        json.dumps({"name": "scribe", "mcpServers": {"notes:tools": {"command": "notes-mcp"}}}),
        encoding="utf-8",
    )
    (kiro_dir / "mine.json").write_text(
        json.dumps({"name": "helper", "mcpServers": {"my:tools": {"command": "my-mcp"}}}),
        encoding="utf-8",
    )
    try:
        _enable("login")
        # The app file, selected the way kiro-cli selects it.
        with pytest.raises(LoginWithholdUnresolved, match=r"'scribe' \(notes--scribe.json\)"):
            require_login_withhold("scribe", tmp_path)
        # ...and by its stem, which kiro-cli also accepts.
        with pytest.raises(LoginWithholdUnresolved, match="notes--scribe.json"):
            require_login_withhold("notes--scribe", tmp_path)
        # A custom spec selected by its declared name keeps its own MCP.
        require_login_withhold("helper", tmp_path)
        require_login_withhold("mine", tmp_path)
        # Nothing on disk declares or is named this: nothing to gate.
        require_login_withhold("ghost", tmp_path)
        # Two specs declaring the same name: which runs is undefined -> refuse.
        (kiro_dir / "other.json").write_text(
            json.dumps({"name": "scribe", "mcpServers": {}}), encoding="utf-8"
        )
        with pytest.raises(LoginWithholdUnresolved, match="more than one spec"):
            require_login_withhold("scribe", tmp_path)
    finally:
        reset_context()


def test_restore_never_drops_a_stashed_name_when_a_source_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source file that exists but does not parse makes the live inventory
    UNKNOWN, not empty. The stashed name is restored, its refs stay, and the
    restore reports incomplete (False) so the writer never unlinks the sidecar."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    broken = tmp_path / "kiro-mcp.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", broken)
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [])
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "sourceServers": ["notes:tools"],
                "tools": ["@notes:tools/search"],
                "allowedTools": ["@notes:tools/search"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}, "tools": [], "allowedTools": []}
    assert _restore_authored_mcp(config) is False  # applied, but not fully: keep the sidecar
    assert config["mcpServers"]["notes:tools"] == {"command": "notes-mcp"}
    assert "@notes:tools/search" in config["tools"]
    assert "@notes:tools/search" in config["allowedTools"]
    assert sidecar.exists()
    # Once the source reads again and truly lacks the name, the drop applies.
    broken.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config = {"mcpServers": {}, "tools": [], "allowedTools": []}
    assert _restore_authored_mcp(config) is True
    assert "notes:tools" not in config["mcpServers"]


def test_stash_with_unreadable_source_prunes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stash writer's ``sourceServers`` is what licenses the merge to prune
    vanished names; with a source it could not read it omits the list, so the
    prior set and every prior server survive."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _stash_authored_mcp
    from kiro_crew.config import config_dir

    broken = tmp_path / "kiro-mcp.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", broken)
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda **_k: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [])
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "sourceServers": ["notes:tools"],
                "sourceBaselines": {"notes:tools": {"command": "notes-mcp"}},
                "tools": [],
                "allowedTools": [],
            }
        ),
        encoding="utf-8",
    )
    config = {
        "mcpServers": {"local:tools": {"command": "local-mcp"}},
        "tools": [],
        "allowedTools": [],
    }
    _stash_authored_mcp(config, set())
    stored = json.loads(sidecar.read_text(encoding="utf-8"))
    assert stored["mcpServers"]["notes:tools"] == {"command": "notes-mcp"}
    assert stored["mcpServers"]["local:tools"] == {"command": "local-mcp"}
    assert stored["sourceServers"] == ["notes:tools"]  # prior set kept, not emptied


def test_stash_with_unreadable_source_keeps_source_owned_aliases_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one source unreadable, the runtime spec was assembled from the
    readable sources only, so an alias the unreadable source owns has resolved
    to its sibling's spec there. Merging that extract must not overwrite the
    stash's definition of the alias (or its refs) -- durably -- while a
    runtime-only server still merges."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _stash_authored_mcp
    from kiro_crew.config import config_dir

    broken = tmp_path / "kiro-mcp.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", broken)
    # The readable sibling source: its slash key sanitizes to the bare alias
    # the unreadable source's slash-free server owns, and with that server
    # unknown the readable key takes the alias.
    monkeypatch.setattr(
        "kiro_crew.agent._collect_app_mcp_servers",
        lambda **_k: {"namespace/name": {"command": "slashed"}},
    )
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [])
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "namespace-name": {"command": "slash-free"},
                    "namespace-name-2": {"command": "slashed"},
                },
                "sourceServers": ["namespace-name", "namespace-name-2"],
                "sourceBaselines": {
                    "namespace-name": {"command": "slash-free"},
                    "namespace-name-2": {"command": "slashed"},
                },
                "tools": ["@namespace-name/search", "@namespace-name-2/search"],
                "allowedTools": ["@namespace-name/search"],
            }
        ),
        encoding="utf-8",
    )
    # The partial runtime spec: the bare alias now carries the sibling's spec
    # (the unreadable source's own definition is missing), and the sibling's
    # tool ref rides under the alias.
    config = {
        "mcpServers": {
            "namespace-name": {"command": "slashed"},
            "local:tools": {"command": "local-mcp"},
        },
        "tools": ["@namespace-name/sibling-only", "@local:tools/run"],
        "allowedTools": ["@namespace-name/sibling-only"],
    }
    _stash_authored_mcp(config, set())
    stored = json.loads(sidecar.read_text(encoding="utf-8"))
    # Source-owned entries are untouched; the runtime-only server merged.
    assert stored["mcpServers"]["namespace-name"] == {"command": "slash-free"}
    assert stored["mcpServers"]["namespace-name-2"] == {"command": "slashed"}
    assert stored["mcpServers"]["local:tools"] == {"command": "local-mcp"}
    assert stored["sourceServers"] == ["namespace-name", "namespace-name-2"]
    assert stored["sourceBaselines"]["namespace-name"] == {"command": "slash-free"}
    assert "@namespace-name/search" in stored["tools"]
    assert "@namespace-name/search" in stored["allowedTools"]
    assert "@namespace-name-2/search" in stored["tools"]
    assert "@namespace-name/sibling-only" not in stored["tools"]
    assert "@namespace-name/sibling-only" not in stored["allowedTools"]
    assert "@local:tools/run" in stored["tools"]


def test_restore_with_unreadable_source_never_classifies_an_alias_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Takeover detection compares each alias with its live resolution; over the
    readable sources alone the unreadable source's alias resolves to a sibling,
    which looks exactly like a takeover. Unknown, so the stash spec is kept."""
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    broken = tmp_path / "kiro-mcp.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", broken)
    monkeypatch.setattr(
        "kiro_crew.agent._collect_app_mcp_servers",
        lambda **_k: {"namespace/name": {"command": "slashed"}},
    )
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [])
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "namespace-name": {"command": "slash-free"},
                    "namespace-name-2": {"command": "slashed"},
                },
                "sourceServers": ["namespace-name", "namespace-name-2"],
                "tools": ["@namespace-name/search", "@namespace-name-2/search"],
                "allowedTools": ["@namespace-name/search"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {
        "mcpServers": {"namespace-name": {"command": "slashed"}},
        "tools": [],
        "allowedTools": [],
    }
    assert _restore_authored_mcp(config) is False
    assert config["mcpServers"]["namespace-name"] == {"command": "slash-free"}
    assert config["mcpServers"]["namespace-name-2"] == {"command": "slashed"}
    assert "@namespace-name/search" in config["tools"]
    assert "@namespace-name/search" in config["allowedTools"]
    assert sidecar.exists()

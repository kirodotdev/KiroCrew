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
    return kiro_dir


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
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda: {"notes:tools": {}})
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
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
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
    assert "asyncio.to_thread(rebuild_agent_config, app_agent_refresh=False)" in src
    assert "path = rebuild_agent_config()" not in src

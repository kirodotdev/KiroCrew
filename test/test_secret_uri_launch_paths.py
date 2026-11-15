"""A ``secret://`` env value is resolved in ONE place, and every other launch refuses.

The gateway's backend spawn (``mcp_gateway/secret_uri.resolve_secret_uris``) is the
only code that reads the vault for an MCP server. Every other path that starts a
server -- the session launching an unrouted server from the rebuilt agent spec, a
routed server the rewriter left unwrapped because a shared backend would withhold
part of its env, the stub's direct-exec fallback -- runs where the vault cannot be
read, so a server started there would get the literal reference as its credential:
it starts and every authenticated call fails. These tests pin that each of them
either reaches the gateway's spawn or refuses to start the server.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.acp.kas_settings_mcp import (
    CREW_SETTINGS_MCP_KEY,
    excluded_whole,
    without_excluded_stubs,
)
from kiro_crew.agent_materialization import mcp_sources
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _collect_target_env,
    _injectable_settings_servers,
    _rewrite_single_spec,
    _wrapped_target,
)
from kiro_crew.mcp_gateway.secret_uri import routed_for_secret_reference, secret_reference_keys
from kiro_crew.mcp_gateway.session_servers import injection_server_names, pooled_session_servers
from kiro_crew.testing.wait import default_timeout

REF = "secret://ATLASSIAN_TOKEN"


# --- the shared predicate ---------------------------------------------------


def test_secret_reference_keys_names_keys_only() -> None:
    env = {"B_TOKEN": REF, "A_TOKEN": "secret://other", "URL": "https://x", "N": 3}
    assert secret_reference_keys(env) == ["A_TOKEN", "B_TOKEN"]


@pytest.mark.parametrize("env", [None, [], "secret://x", {"K": "secret:/x"}, {}])
def test_secret_reference_keys_is_empty_for_anything_else(env: object) -> None:
    assert secret_reference_keys(env) == []


# --- rebuild projection: an unrouted server is withheld ----------------------


def _project(
    monkeypatch: pytest.MonkeyPatch, env: dict, routed: frozenset[str]
) -> tuple[dict, mcp_sources.ResolvedServers]:
    spec = {"command": "atlassian-mcp", "env": env}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": spec}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: routed)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    resolved = mcp_sources.resolve_mcp_servers(
        config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", "")
    )
    return config, resolved


def test_an_unrouted_server_with_a_secret_reference_is_not_emitted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config, resolved = _project(monkeypatch, {"API_TOKEN": REF}, frozenset())
    entry = config["mcpServers"]["atlassian"]
    # Kept muted, not dropped, so spec-only fields survive until it is routed.
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True
    assert "atlassian" not in resolved.unresolved
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "'API_TOKEN'" in messages and "MCP Management" in messages
    assert "ATLASSIAN_TOKEN" not in messages  # the secret's name is never echoed


def test_a_withheld_scope_server_keeps_its_spec_only_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field only the agent spec carries (here ``disabledTools``) is not lost
    while the server is withheld, and the mute lifts once it is routed."""
    scope = {"command": "atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {"mcpServers": {"atlassian": {**scope, "disabledTools": ["delete_page"]}}}
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    muted = config["mcpServers"]["atlassian"]
    assert muted["disabled"] is True
    assert muted["disabledTools"] == ["delete_page"]

    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atlassian"}))
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (cmd, ""))
    lifted = config["mcpServers"]["atlassian"]
    assert "disabled" not in lifted and mcp_sources.WITHHELD_KEY not in lifted
    assert lifted["disabledTools"] == ["delete_page"]


def test_the_shared_ref_sync_keeps_a_withheld_server_muted() -> None:
    """The mount pass must not re-enable an entry the rebuild withheld."""
    scope = {"command": "/opt/bin/atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {
        "mcpServers": {"atlassian": {**scope, "disabled": True, mcp_sources.WITHHELD_KEY: True}},
        "tools": ["@atlassian"],
        "allowedTools": ["@atlassian"],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(config, sources, {"atlassian": "atlassian"})
    assert config["mcpServers"]["atlassian"]["disabled"] is True
    assert "@atlassian" not in config["allowedTools"]


def test_a_mount_only_server_is_mounted_without_a_grant() -> None:
    """The default agent's mid-chat addition: the ref mounts the server and the
    rebuild's own whole-server grant is withheld, even one written earlier, so a
    later rebuild cannot silently turn the mount into an approval. A per-tool
    grant the user wrote is theirs: the card lists it as still applying."""
    scope = {"command": "/opt/bin/atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {
        "mcpServers": {"atlassian": dict(scope)},
        "tools": [],
        "allowedTools": ["@atlassian", "@atlassian/search"],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(
        config, sources, {"atlassian": "atlassian"}, mount_only=frozenset({"atlassian"})
    )
    assert config["tools"] == ["@atlassian"]
    assert config["allowedTools"] == ["@atlassian/search"]


def test_a_mount_only_record_leaves_a_suffixed_sibling_s_grant_alone() -> None:
    """``time-2`` is a separately installed server, not the recorded ``time``:
    its page approval is kept."""
    scope = {"command": "/opt/bin/time-mcp"}
    config = {
        "mcpServers": {"time": dict(scope), "time-2": dict(scope)},
        "tools": [],
        "allowedTools": [],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"time": scope, "time-2": scope},
        kiro_global={},
        provider_global={},
        managed_names=set(),
    )
    mcp_sources.sync_shared_server_refs(
        config, sources, {"time": "time", "time-2": "time-2"}, mount_only=frozenset({"time"})
    )
    assert "@time" in config["tools"] and "@time-2" in config["tools"]
    assert config["allowedTools"] == ["@time-2"]


def test_a_mount_only_server_under_the_ceiling_takes_the_ceilings_stricter_arm(
    monkeypatch,
) -> None:
    """When the governance ceiling has an opinion about the server, mount-only
    does not soften it: the ceiling strips per-tool grants too."""
    from kiro_crew.agent_materialization import auto_approve

    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: False)
    scope = {"command": "/opt/bin/atlassian-mcp"}
    config = {
        "mcpServers": {"atlassian": dict(scope)},
        "tools": [],
        "allowedTools": ["@atlassian", "@atlassian/search"],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(
        config, sources, {"atlassian": "atlassian"}, mount_only=frozenset({"atlassian"})
    )
    assert config["tools"] == ["@atlassian"]
    assert config["allowedTools"] == []


def test_a_withheld_mute_keeps_the_users_per_tool_grants() -> None:
    """The withheld mute is temporary, and a hand-written ``@srv/tool`` grant
    cannot be re-derived once stripped; only the bare refs go."""
    scope = {"command": "/opt/bin/atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {
        "mcpServers": {"atlassian": {**scope, "disabled": True, mcp_sources.WITHHELD_KEY: True}},
        "tools": ["@atlassian", "@atlassian/search"],
        "allowedTools": ["@atlassian", "@atlassian/search"],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(config, sources, {"atlassian": "atlassian"})
    assert config["tools"] == ["@atlassian/search"]
    assert config["allowedTools"] == ["@atlassian/search"]


def test_a_user_mute_still_strips_per_tool_grants() -> None:
    scope = {"command": "/opt/bin/atlassian-mcp", "disabled": True}
    config = {
        "mcpServers": {"atlassian": dict(scope)},
        "tools": ["@atlassian/search"],
        "allowedTools": ["@atlassian/search"],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(config, sources, {"atlassian": "atlassian"})
    assert config["allowedTools"] == []


def test_the_next_start_lifts_the_mute_without_the_post_overlay_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rebuild that ran before the gateway wrote its overlay, followed by a
    post-overlay rebuild that failed, still ends served at the next start: the
    overlay persists on disk, so the next start's boot rebuild reads the stub
    the previous start wrote and lifts the mute on its own."""
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    cfg = _routing_config(["atlassian"], tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    spec = {"command": "atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": spec}, kiro_global={}, provider_global={}, managed_names=set()
    )

    def _rebuild() -> dict:
        mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _e: (f"/opt/bin/{cmd}", ""))
        return config["mcpServers"]["atlassian"]

    _write_overlay(tmp_path, [])  # the boot rebuild ran before the overlay
    assert _rebuild()["disabled"] is True
    _write_overlay(tmp_path, ["atlassian"])  # that start wrote it; its lift failed
    entry = _rebuild()  # the NEXT start's boot rebuild
    assert "disabled" not in entry and mcp_sources.WITHHELD_KEY not in entry


def test_the_warning_on_a_host_without_a_gateway_does_not_point_at_routing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: False)
    with caplog.at_level(logging.WARNING):
        withheld = mcp_sources._unrouted_secret_reference(
            "atl", {"command": "x", "env": {"API_TOKEN": REF}}, frozenset, dict
        )
    assert withheld is True
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "does not run on this platform" in messages
    assert "Turn on routing" not in messages
    assert "ATLASSIAN_TOKEN" not in messages


@pytest.mark.parametrize("served", [False, True])
def test_the_rebuild_is_a_function_of_config_and_overlay_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, served: bool
) -> None:
    """Same config and same overlay give a byte-identical spec, whatever the spec
    held before (live, or muted by an earlier pass) and however many passes ran."""
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    cfg = _routing_config(["atlassian"], tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _write_overlay(tmp_path, ["atlassian"] if served else [])
    scope = {"command": "atlassian-mcp", "env": {"API_TOKEN": REF}}
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )

    def _rebuild(config: dict) -> str:
        mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _e: (f"/opt/bin/{cmd}", ""))
        mcp_sources.sync_shared_server_refs(config, sources, {"atlassian": "atlassian"})
        return json.dumps(config, sort_keys=True)

    live = {"mcpServers": {"atlassian": dict(scope)}, "tools": [], "allowedTools": []}
    muted = {
        "mcpServers": {"atlassian": {**scope, "disabled": True, mcp_sources.WITHHELD_KEY: True}},
        "tools": [],
        "allowedTools": [],
    }
    first = {_rebuild(live), _rebuild(muted)}
    assert len(first) == 1
    assert _rebuild(json.loads(first.pop())) == _rebuild(muted)


def _routing_config(stub_servers: list[str], overlay_dir: Path) -> object:
    import types

    return types.SimpleNamespace(
        mcp_gateway=types.SimpleNamespace(stub_servers=stub_servers, overlay_dir=str(overlay_dir))
    )


def _write_overlay(overlay_dir: Path, stubbed: list[str]) -> None:
    """The overlay a running gateway wrote at its start, with a stub per name."""
    overlay_dir.mkdir(parents=True, exist_ok=True)
    servers = {n: {"command": "kirocrew-mcp-stub", _WRAPPER_MARKER: True} for n in stubbed}
    (overlay_dir / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "mcpServers": servers}), encoding="utf-8"
    )


def test_routing_on_a_host_without_a_gateway_routes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    _write_overlay(tmp_path, ["atlassian"])
    routed_cfg = _routing_config(["atlassian"], tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: routed_cfg))
    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    assert mcp_sources._gateway_routed_names() == frozenset({"atlassian"})
    monkeypatch.setattr(gw, "is_gateway_supported", lambda: False)
    assert mcp_sources._gateway_routed_names() == frozenset()


def test_saved_routing_is_not_routed_until_the_gateway_serves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The routing switch records intent for the NEXT gateway start. Until then
    the running gateway's overlay has no stub, so a session launches the agent
    spec's own entry -- the server must stay withheld in that window."""
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    _write_overlay(tmp_path, [])  # the overlay from before routing was saved
    cfg = _routing_config(["atlassian"], tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    assert mcp_sources._gateway_routed_names() == frozenset()

    _write_overlay(tmp_path, ["atlassian"])  # the gateway restarted with it
    assert mcp_sources._gateway_routed_names() == frozenset({"atlassian"})


def test_a_stale_stub_does_not_count_once_routing_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    _write_overlay(tmp_path, ["atlassian"])
    cfg = _routing_config([], tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    assert mcp_sources._gateway_routed_names() == frozenset()


def test_a_missing_overlay_routes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    cfg = _routing_config(["atlassian"], tmp_path / "no-overlay")
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    assert mcp_sources._gateway_routed_names() == frozenset()


def test_a_raw_routed_name_matches_its_alias_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw, "is_gateway_supported", lambda: True)
    _write_overlay(tmp_path, ["acme-mcp"])
    cfg = _routing_config(["npm:@acme/mcp"], tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    assert mcp_sources._gateway_routed_names() == frozenset({"acme-mcp"})


def test_a_routed_server_with_a_secret_reference_is_emitted_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, resolved = _project(monkeypatch, {"API_TOKEN": REF}, frozenset({"atlassian"}))
    entry = config["mcpServers"]["atlassian"]
    assert entry["env"]["API_TOKEN"] == REF  # resolved later, by the gateway
    assert "atlassian" not in resolved.unresolved


def _project_one(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    in_scope: bool,
    routed: frozenset[str] = frozenset(),
    app_owned: dict[str, bool] | None = None,
) -> tuple[dict, mcp_sources.ResolvedServers]:
    spec = {"command": "some-mcp", "env": {"API_TOKEN": REF}}
    config = {"mcpServers": {name: dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={name: spec} if in_scope else {},
        kiro_global={},
        provider_global={},
        managed_names=set(),
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: routed)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", lambda: dict(app_owned or {}))
    resolved = mcp_sources.resolve_mcp_servers(
        config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", "")
    )
    return config, resolved


def test_a_server_routed_under_its_raw_name_is_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing may be recorded under the raw slashed spelling or the alias."""
    config, resolved = _project_one(
        monkeypatch, "npm:@acme/mcp", in_scope=True, routed=frozenset({"npm:@acme/mcp"})
    )
    assert "npm:@acme/mcp" in config["mcpServers"]
    assert not resolved.unresolved


def test_a_rebuilt_alias_key_of_a_raw_routed_name_stays_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first pass the spec key is the alias, while routing still lists
    the raw slashed name: the alias key must count as routed, not be muted."""
    config, _ = _project_one(
        monkeypatch, "acme-mcp", in_scope=False, routed=frozenset({"npm:@acme/mcp"})
    )
    entry = config["mcpServers"]["acme-mcp"]
    assert "disabled" not in entry and mcp_sources.WITHHELD_KEY not in entry


def test_a_server_only_in_the_agent_spec_is_kept_but_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent spec is the only copy of such an entry, so it is muted in place
    rather than dropped, and the mute lifts on the pass after it is routed."""
    config, resolved = _project_one(monkeypatch, "atlassian", in_scope=False)
    entry = config["mcpServers"]["atlassian"]
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True
    assert entry["env"]["API_TOKEN"] == REF  # the declaration survives intact
    assert "atlassian" not in resolved.unresolved

    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atlassian"}))
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (cmd, ""))
    lifted = config["mcpServers"]["atlassian"]
    assert "disabled" not in lifted
    assert mcp_sources.WITHHELD_KEY not in lifted


def test_a_user_written_mute_is_never_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = {"command": "some-mcp", "env": {"API_TOKEN": REF}, "disabled": True}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atlassian"}))
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    assert config["mcpServers"]["atlassian"]["disabled"] is True


@pytest.mark.parametrize("flag", [None, 0, "true"])
def test_a_non_boolean_user_mute_never_gains_the_withheld_record(
    monkeypatch: pytest.MonkeyPatch, flag: object
) -> None:
    """A mute the user wrote in any form stays the user's: the rebuild must not
    claim it, or a later pass would lift it and start the server."""
    spec = {"command": "some-mcp", "env": {"API_TOKEN": REF}, "disabled": flag}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    assert mcp_sources.WITHHELD_KEY not in config["mcpServers"]["atlassian"]


def test_an_app_registered_server_is_left_to_the_app_path(monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = _project_one(
        monkeypatch, "myapp:srv", in_scope=False, app_owned={"myapp:srv": True}
    )
    assert config["mcpServers"]["myapp:srv"]["env"]["API_TOKEN"] == REF


def test_a_server_without_a_reference_does_not_read_the_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> frozenset[str]:
        raise AssertionError("routing read for a server with no secret reference")

    spec = {"command": "plain-mcp", "env": {"MODE": "fast"}}
    config = {"mcpServers": {"plain": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={"plain": spec}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", _boom)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    assert config["mcpServers"]["plain"]["env"] == {"MODE": "fast"}


def test_an_unreadable_routing_withholds_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a config that cannot be read routes nothing."""

    def _broken_load() -> None:
        raise OSError("config unreadable")

    import kiro_crew.config.loader as loader

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(_broken_load))
    assert mcp_sources._gateway_routed_names() == frozenset()


# --- rewriter: a rostered server is never handed out raw ---------------------


def _rewrite(spec: dict, tmp_path: Path, **kw: object) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        **kw,  # type: ignore[arg-type]
    )


def test_a_credential_reference_gets_a_private_backend_instead_of_none(tmp_path: Path) -> None:
    """A credential-named key is withheld from a SHARED backend, and an unwrapped
    server would be launched by the session with the literal reference. A private
    backend receives the whole declared env, which is where the gateway resolves it."""
    spec = {
        "name": "agent-a",
        "tools": ["*"],
        "mcpServers": {"atl": {"command": sys.executable, "env": {"OAUTH_TOKEN": REF}}},
    }
    out, wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"atl"}), pooling_enabled=True, forward_env=True
    )
    entry = out["mcpServers"]["atl"]
    assert wrapped == 1
    assert entry.get(_WRAPPER_MARKER) is True
    assert "--poolable" not in expand_stub_flags(entry["args"])


def test_a_withheld_plain_credential_still_runs_unwrapped(tmp_path: Path) -> None:
    """Unchanged for a literal credential: the session launches it with its env."""
    spec = {
        "name": "agent-a",
        "tools": ["*"],
        "mcpServers": {"atl": {"command": sys.executable, "env": {"OAUTH_TOKEN": "x"}}},
    }
    out, wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"atl"}), pooling_enabled=True, forward_env=True
    )
    assert wrapped == 0
    assert out["mcpServers"]["atl"].get("env") == {"OAUTH_TOKEN": "x"}
    assert "disabled" not in out["mcpServers"]["atl"]


def test_a_settings_server_with_a_credential_reference_gets_a_private_backend(
    tmp_path: Path,
) -> None:
    """The settings-file edition of the same rule: the server is injected as a
    private stub rather than left to kiro-cli's own merge, which would launch it
    from the session with the literal reference."""
    entry = {"command": sys.executable, "env": {"OAUTH_TOKEN": REF}}
    inject = _injectable_settings_servers(
        {"mcpServers": {"atl": entry}},
        frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
    )
    assert set(inject) == {"atl"}
    out, wrapped = _rewrite(
        {"name": "agent-a", "mcpServers": {}, "tools": ["@atl"]},
        tmp_path,
        stub_servers=frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
        inject_servers=inject,
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True
    flags = expand_stub_flags(out["mcpServers"]["atl"]["args"])
    assert "--poolable" not in flags
    assert "--refuse" not in flags
    assert _wrapped_target(out["mcpServers"]["atl"]) is not None


def test_a_settings_server_with_a_plain_credential_is_still_left_to_kiro_cli() -> None:
    entry = {"command": sys.executable, "env": {"OAUTH_TOKEN": "x"}}
    inject = _injectable_settings_servers(
        {"mcpServers": {"atl": entry}},
        frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
    )
    assert inject == {}


class _RefuseAll:
    """A launch-approval store that approves nothing."""

    def __init__(self) -> None:
        # The real store's per-pass refusal reasons, read on each refusal.
        self.refused: dict[str, str] = {}

    def admit(self, *_a: object, **_kw: object) -> bool:
        return False


def _ref_spec(name: str = "atl", command: str = sys.executable, env: dict | None = None) -> dict:
    return {
        "name": "agent-a",
        "tools": ["*"],
        "mcpServers": {name: {"command": command, "env": env or {"API_KEY": REF}}},
    }


def test_an_unapproved_routed_server_with_a_reference_stays_wrapped(tmp_path: Path) -> None:
    """Left unwrapped, the session would launch the spec's entry with the literal
    reference. Wrapped, the gateway has no approved target for it and refuses."""
    out, wrapped = _rewrite(
        _ref_spec(), tmp_path, stub_servers=frozenset({"atl"}), approvals=_RefuseAll()
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True
    assert "--poolable" not in expand_stub_flags(out["mcpServers"]["atl"]["args"])


def test_the_routing_probe_lifts_the_rebuilds_withheld_mute(tmp_path: Path) -> None:
    """The launch probe routes a server before its routing is saved, so the
    agent spec still carries the rebuild's mute. It must be wrapped (and its
    launch captured for approval), not passed through as the user's mute."""
    spec = _ref_spec()
    spec["mcpServers"]["atl"].update({"disabled": True, mcp_sources.WITHHELD_KEY: True})
    spec["tools"] = []  # the rebuild strips a withheld server's refs
    out, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"atl"}))
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True


def test_a_user_mute_is_still_honoured_by_the_rewriter(tmp_path: Path) -> None:
    spec = _ref_spec()
    spec["mcpServers"]["atl"]["disabled"] = True
    out, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"atl"}))
    assert wrapped == 0
    assert out["mcpServers"]["atl"]["disabled"] is True


def test_an_unrouted_withheld_mute_stays_muted_in_the_overlay(tmp_path: Path) -> None:
    spec = _ref_spec()
    spec["mcpServers"]["atl"].update({"disabled": True, mcp_sources.WITHHELD_KEY: True})
    out, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset())
    assert wrapped == 0
    assert out["mcpServers"]["atl"]["disabled"] is True


def test_a_servable_routed_server_with_a_reference_stays_pooled(tmp_path: Path) -> None:
    """Nothing withheld, command resolves, launch approved: the pooled spawn
    resolves the reference itself, so the server keeps sharing one backend."""
    out, wrapped = _rewrite(
        _ref_spec(env={"API_URL": REF}),
        tmp_path,
        stub_servers=frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
    )
    assert wrapped == 1
    assert "--poolable" in expand_stub_flags(out["mcpServers"]["atl"]["args"])


def test_an_unapproved_routed_server_without_a_reference_is_still_left_unwrapped(
    tmp_path: Path,
) -> None:
    out, wrapped = _rewrite(
        _ref_spec(env={"MODE": "x"}),
        tmp_path,
        stub_servers=frozenset({"atl"}),
        approvals=_RefuseAll(),
    )
    assert wrapped == 0
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is None


def test_an_unresolvable_routed_server_with_a_reference_stays_wrapped(tmp_path: Path) -> None:
    out, wrapped = _rewrite(
        _ref_spec(command="no-such-mcp-binary-16189"),
        tmp_path,
        stub_servers=frozenset({"atl"}),
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True


def test_a_server_routed_under_its_raw_name_is_wrapped_under_its_alias(tmp_path: Path) -> None:
    """The spec key is the alias; the routing list holds the raw slashed name."""
    out, wrapped = _rewrite(
        _ref_spec(name="acme-mcp"), tmp_path, stub_servers=frozenset({"npm:@acme/mcp"})
    )
    assert wrapped == 1
    assert out["mcpServers"]["acme-mcp"].get(_WRAPPER_MARKER) is True


def test_a_settings_server_with_a_reference_is_injected_when_unresolvable(
    tmp_path: Path,
) -> None:
    entry = {"command": "no-such-mcp-binary-16189", "env": {"API_KEY": REF}}
    inject = _injectable_settings_servers({"mcpServers": {"atl": entry}}, frozenset({"atl"}))
    assert set(inject) == {"atl"}
    out, wrapped = _rewrite(
        {"name": "agent-a", "mcpServers": {}, "tools": ["@atl"]},
        tmp_path,
        stub_servers=frozenset({"atl"}),
        inject_servers=inject,
        approvals=_RefuseAll(),
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True
    assert "--refuse" not in expand_stub_flags(out["mcpServers"]["atl"]["args"])


_ABSENT = object()


def _refusing_settings_rewrite(tmp_path: Path, command: str, **spec_keys: object) -> dict:
    """The overlay ``agent-a`` gets for a routed settings server ``atl`` carrying a
    reference, with every launch approved (``approvals`` of ``None``)."""
    entry = {"command": command, "env": {"API_KEY": REF}}
    inject = _injectable_settings_servers({"mcpServers": {"atl": entry}}, frozenset({"atl"}))
    assert set(inject) == {"atl"}
    out, _ = _rewrite(
        {"name": "agent-a", "mcpServers": {}, **spec_keys},
        tmp_path,
        stub_servers=frozenset({"atl"}),
        inject_servers=inject,
    )
    return out


@pytest.mark.parametrize("command", [sys.executable, "no-such-mcp-binary-16189"])
@pytest.mark.parametrize("tools", [_ABSENT, [], ["@other"]], ids=["absent", "empty", "other"])
def test_a_settings_server_with_a_reference_gets_a_refusing_stub_when_tools_omits_it(
    tmp_path: Path, command: str, tools: object
) -> None:
    """An agent that may read ``mcp.json`` but whose ``tools`` does not name ``atl``
    still gets an overlay entry for it: with none, the host's own merge of the real
    settings file starts ``atl`` with the literal reference. The entry refuses on its
    own: it names no target, so the gateway is handed none for it, and the launch
    being approved (as here) cannot serve this agent the server."""
    out = _refusing_settings_rewrite(
        tmp_path, command, **({} if tools is _ABSENT else {"tools": tools})
    )
    stub = out["mcpServers"]["atl"]
    assert stub[_WRAPPER_MARKER] is True
    assert "--refuse" in expand_stub_flags(stub["args"])
    assert _wrapped_target(stub) is None
    assert stub["env"] == {}
    target_env: dict[str, str] = {}
    _collect_target_env(out["mcpServers"], target_env)
    assert target_env == {}
    assert REF not in json.dumps(out)


@pytest.mark.parametrize("flag", ["includeMcpJson", "useLegacyMcpJson"])
def test_an_agent_that_excludes_mcp_json_gets_no_refusing_stub(tmp_path: Path, flag: str) -> None:
    """The host merges nothing from ``mcp.json`` into such an agent, so there is no
    entry to outrank, and a stub sent at ``session/new`` would mount the server."""
    out = _refusing_settings_rewrite(tmp_path, sys.executable, **{flag: False})
    assert "atl" not in out.get("mcpServers", {})


def _published(out: dict, tmp_path: Path) -> Path:
    """The overlay directory a gateway pass would leave *out* in, as ``agent-a``."""
    overlay = tmp_path / "agents"
    overlay.mkdir()
    (overlay / "agent-a.json").write_text(json.dumps(out), encoding="utf-8")
    return overlay


def test_a_refusing_stub_is_what_the_session_is_handed(tmp_path: Path) -> None:
    """``session/new`` carries it, on kiro-cli and KAS alike, and the mirrors withhold
    the same name, so the merge's own ``atl`` is outranked rather than started."""
    out = _refusing_settings_rewrite(tmp_path, sys.executable, tools=["@other"])
    overlay = _published(out, tmp_path)
    (element,) = pooled_session_servers(overlay, "agent-a")
    assert element["name"] == "atl"
    assert "--refuse" in expand_stub_flags(element["args"])
    assert injection_server_names(overlay, "agent-a") == frozenset({"atl"})


@pytest.mark.parametrize(
    ("opted_in", "legacy"),
    [(False, False), (True, False), (False, True)],
    ids=["not-opted-in", "opted-in", "legacy"],
)
def test_on_kas_the_files_atl_is_excluded_or_outranked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opted_in: bool, legacy: bool
) -> None:
    """KAS reads ``mcp.json`` itself. For an agent that did not opt in, ``atl`` is
    excluded whole, so KAS starts none and the stub is not sent; for one that opted in,
    or keeps the earlier behaviour, nothing is excluded and the refusing stub is sent,
    outranking the file's ``atl``. Neither leaves KAS a raw ``atl`` to start."""
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    out = _refusing_settings_rewrite(tmp_path, sys.executable, tools=["@other"])
    session = pooled_session_servers(_published(out, tmp_path), "agent-a")
    stamp = {
        "timeout": 120_000,
        "fileServers": {"atl": []},
        "declared": [],
        "legacy": legacy,
        "optedIn": opted_in,
    }
    agent = {"id": "agent-a", "mcpServers": {}, CREW_SETTINGS_MCP_KEY: stamp}
    sent = without_excluded_stubs(session, [agent])
    if opted_in or legacy:
        assert excluded_whole(agent) == set()
        assert [e["name"] for e in sent] == ["atl"]
        assert "--refuse" in expand_stub_flags(sent[0]["args"])
    else:
        assert excluded_whole(agent) == {"atl"}
        assert sent == []


def test_a_refusing_stub_answers_as_a_server_with_no_tools(tmp_path: Path) -> None:
    """Run through the stub's own entry point, as kiro-cli launches the overlay entry:
    it answers ``initialize`` with no capabilities, lists nothing, refuses a call, and
    exits when its stdin closes. The socket it is pointed at has no listener, so a stub
    that tried the gateway (and then its direct-exec fallback) would answer nothing."""
    out = _refusing_settings_rewrite(tmp_path, sys.executable, tools=["@other"])
    stub = out["mcpServers"]["atl"]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "x"}},
    ]
    # The child imports THIS checkout's package by absolute path, as the stub
    # session-token tests pin it, not whatever the interpreter has installed.
    src_root = Path(stub_mod.__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_root), *filter(None, [os.environ.get("PYTHONPATH", "")])]
    )
    env["KIROCREW_MCP_SOCKET"] = str(tmp_path / "no-gateway.sock")
    proc = subprocess.run(
        [stub["command"], *stub["args"]],
        input="".join(json.dumps(r) + "\n" for r in requests),
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4}
    assert replies[1]["result"]["capabilities"] == {}
    assert replies[1]["result"]["serverInfo"]["name"] == "atl"
    assert replies[2]["result"] == {"tools": []}
    assert replies[3]["result"] == {}
    assert replies[4]["error"]["code"] == -32601


# --- one routed decision for the rebuild and the rewriter ---------------------


_ROUTING_CASES = [
    ("atl", set()),
    ("atl", {"atl"}),
    ("atl", {"other"}),
    ("acme-mcp", {"npm:@acme/mcp"}),
    ("acme-mcp", {"acme-mcp"}),
    ("acme-mcp", {"npm:@other/mcp"}),
    ("npm:@acme/mcp", {"npm:@acme/mcp"}),
    ("npm:@acme/mcp", {"acme-mcp"}),
]


@pytest.mark.parametrize(("name", "routed"), _ROUTING_CASES)
def test_the_rebuild_and_the_rewriter_agree_on_routed(
    tmp_path: Path, name: str, routed: set[str]
) -> None:
    """A server the rebuild lets through must be one the rewriter wraps, and a
    server the rebuild withholds must be one it does not: either mismatch either
    launches the literal reference or keeps a routed server off for good."""
    entry = {"command": sys.executable, "env": {"API_KEY": REF}}
    withheld = mcp_sources._unrouted_secret_reference(name, entry, lambda: frozenset(routed), dict)
    _, wrapped = _rewrite(
        {"name": "agent-a", "tools": ["*"], "mcpServers": {name: dict(entry)}},
        tmp_path,
        stub_servers=frozenset(routed),
    )
    assert withheld is (wrapped == 0)
    assert withheld is not routed_for_secret_reference(name, frozenset(routed))


# --- dashboard enable switches keep the withheld mute ------------------------


@pytest.fixture()
def dashboard_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An agent spec holding a withheld server, and a global mcp.json declaring it."""
    from unittest.mock import patch

    declared = {"command": "atl-mcp", "env": {"API_TOKEN": REF}}
    agent_cfg = tmp_path / "kirocrew.json"
    agent_cfg.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    "atl": {**declared, "disabled": True, mcp_sources.WITHHELD_KEY: True}
                },
                "tools": [],
                "allowedTools": [],
            }
        ),
        encoding="utf-8",
    )
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": {"atl": declared, "fresh": declared}}), encoding="utf-8"
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    with (
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
    ):
        yield agent_cfg


def _spec_entry(agent_cfg: Path, name: str) -> dict:
    return json.loads(agent_cfg.read_text(encoding="utf-8"))["mcpServers"][name]


def test_enable_all_keeps_the_rebuilds_withheld_mute(dashboard_spec: Path) -> None:
    """Popping ``disabled`` and leaving the record would let kiro-cli start the
    unrouted server with the literal reference, and the "Enable all" switch runs
    no rebuild that would put the mute back."""
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch_unlocked

    _sync_mcp_to_agent_batch_unlocked(["atl"], True)
    entry = _spec_entry(dashboard_spec, "atl")
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True


def test_the_single_enable_keeps_the_rebuilds_withheld_mute(dashboard_spec: Path) -> None:
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_unlocked

    _sync_mcp_to_agent_unlocked("atl", True)
    entry = _spec_entry(dashboard_spec, "atl")
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True


def test_an_enable_copy_of_an_unrouted_reference_is_withheld(dashboard_spec: Path) -> None:
    """The copy branch brings the server in from mcp.json; it is muted at once."""
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch_unlocked

    _sync_mcp_to_agent_batch_unlocked(["fresh"], True)
    entry = _spec_entry(dashboard_spec, "fresh")
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True


def test_an_enable_lifts_the_mute_once_the_gateway_serves_the_server(
    dashboard_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch_unlocked

    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atl"}))
    _sync_mcp_to_agent_batch_unlocked(["atl"], True)
    entry = _spec_entry(dashboard_spec, "atl")
    assert "disabled" not in entry and mcp_sources.WITHHELD_KEY not in entry


def test_a_dashboard_disable_makes_the_mute_the_users(dashboard_spec: Path) -> None:
    """Otherwise the rebuild would lift the user's switch-off once it is routed."""
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_unlocked

    _sync_mcp_to_agent_unlocked("atl", False)
    entry = _spec_entry(dashboard_spec, "atl")
    assert entry["disabled"] is True
    assert mcp_sources.WITHHELD_KEY not in entry


def test_an_enable_without_a_reference_still_lifts_the_mute(
    dashboard_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_unlocked

    def _boom() -> frozenset[str]:
        raise AssertionError("routing read for a server with no secret reference")

    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", _boom)
    cfg = json.loads(dashboard_spec.read_text(encoding="utf-8"))
    cfg["mcpServers"]["plain"] = {"command": "plain-mcp", "disabled": True}
    dashboard_spec.write_text(json.dumps(cfg), encoding="utf-8")
    _sync_mcp_to_agent_unlocked("plain", True)
    assert "disabled" not in _spec_entry(dashboard_spec, "plain")


# --- the gateway lifts a withhold its fresh overlay now serves ---------------


def _agents_dir_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "mcpServers": servers}), encoding="utf-8"
    )
    monkeypatch.setattr(mcp_sources.agent_mod, "kiro_agents_dir_path", lambda: agents)


def test_a_withheld_server_the_gateway_now_serves_is_due_a_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    withheld = {"command": "atl-mcp", "disabled": True, mcp_sources.WITHHELD_KEY: True}
    _agents_dir_with(tmp_path, monkeypatch, {"atl": withheld})
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atl"}))
    assert mcp_sources.withheld_servers_now_routed() is True
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    assert mcp_sources.withheld_servers_now_routed() is False


def test_no_withheld_server_means_no_routing_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> frozenset[str]:
        raise AssertionError("routing read with nothing withheld")

    _agents_dir_with(tmp_path, monkeypatch, {"atl": {"command": "atl-mcp", "disabled": True}})
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", _boom)
    assert mcp_sources.withheld_servers_now_routed() is False


@pytest.mark.parametrize("due", [True, False])
def test_the_gateway_start_rebuilds_after_readiness_when_a_withheld_server_is_served(
    due: bool,
) -> None:
    """The rebuild is deferred past process readiness (no new work on the boot
    path), and runs only when a withheld server is now served."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["atl"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    rebuilds: list[object] = []

    async def _run() -> None:
        await orch._init_mcp_gateway()
        assert rebuilds == []  # nothing ran on the boot path
        lift = next(t for t in orch._background_tasks if t.get_name() == "mcp-withheld-lift")
        orch._mcp_launch_approval_ready.set()
        await lift

    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        patch.object(mcp_sources, "withheld_servers_now_routed", return_value=due),
        patch(
            "kiro_crew.agent.rebuild_agent_config",
            side_effect=lambda *_a, **kw: rebuilds.append(kw),
        ),
    ):
        asyncio.run(_run())
    assert rebuilds == ([{"refresh_forks": "defer"}] if due else [])


def test_a_source_mute_makes_a_withheld_mute_the_users() -> None:
    """A server switched off in ``mcp.json`` while it is withheld keeps its mute
    and loses the record, so routing it later does not lift the user's mute."""
    scope = {"command": "/opt/bin/atl-mcp", "env": {"API_TOKEN": REF}, "disabled": True}
    config = {
        "mcpServers": {
            "atl": {
                "command": "/opt/bin/atl-mcp",
                "env": {"API_TOKEN": REF},
                "disabled": True,
                mcp_sources.WITHHELD_KEY: True,
            }
        },
        "tools": [],
        "allowedTools": [],
    }
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={"atl": scope}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(config, sources, {"atl": "atl"})
    entry = config["mcpServers"]["atl"]
    assert entry["disabled"] is True
    assert mcp_sources.WITHHELD_KEY not in entry


def test_a_routed_rewrite_honours_a_source_mute_on_a_formerly_withheld_server(
    tmp_path: Path,
) -> None:
    """End to end of the two passes: the sync drops the record, so the rewriter
    reads the remaining ``disabled`` as the user's and never wraps the server."""
    scope = {"command": sys.executable, "env": {"API_KEY": REF}, "disabled": True}
    config = {
        "mcpServers": {"atl": {**scope, mcp_sources.WITHHELD_KEY: True}},
        "tools": [],
        "allowedTools": [],
    }
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={"atl": scope}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(config, sources, {"atl": "atl"})
    out, wrapped = _rewrite(
        {"name": "agent-a", "tools": ["*"], "mcpServers": config["mcpServers"]},
        tmp_path,
        stub_servers=frozenset({"atl"}),
    )
    assert wrapped == 0
    assert out["mcpServers"]["atl"]["disabled"] is True


# --- stub fallback: never exec with an unresolved reference -------------------


def _fallback_args(tmp_path: Path, declared: dict) -> object:
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps(declared), encoding="utf-8")
    return stub_mod._parse_args(
        [
            "--server",
            "atl",
            "--agent",
            "agent-a",
            "--target-command",
            "true",
            "--work-dir",
            str(tmp_path),
            "--env-file",
            str(env_file),
        ]
    )


def test_the_fallback_refuses_to_exec_with_a_secret_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exec(*_a: object) -> None:
        raise AssertionError("exec'd a backend with an unresolved secret reference")

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _exec)
    with pytest.raises(SystemExit) as exc:
        stub_mod.fallback_exec(_fallback_args(tmp_path, {"API_TOKEN": REF}))
    assert exc.value.code == stub_mod._EXIT_UNRESOLVED_SECRET
    err = capsys.readouterr().err
    assert "'API_TOKEN'" in err and "ATLASSIAN_TOKEN" not in err


def test_the_fallback_still_execs_a_plain_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: str, _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit):
        stub_mod.fallback_exec(_fallback_args(tmp_path, {"API_TOKEN": "literal"}))
    assert seen["env"]["API_TOKEN"] == "literal"


def test_the_fallback_ignores_an_inherited_value_it_did_not_declare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: str, _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setenv("UNRELATED_SETTING", "secret://not-ours")
    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit) as exc:
        stub_mod.fallback_exec(_fallback_args(tmp_path, {"API_TOKEN": "literal"}))
    assert exc.value.code == 0
    assert seen["env"]["API_TOKEN"] == "literal"

"""``_refresh_dynamic_fields`` and edition-contributed (PlatformContext) MCP servers.

An edition/plugin contributes MCP servers through
``PlatformContext.extra_mcp_servers()``. A contributed spec may bake in an
absolute ``command``/``args`` path (an interpreter, or a launcher inside a
versioned install tree) that moves between installs. When the refresh merged
those extras with a plain ``setdefault`` the FIRST value written was frozen
forever, so once the old install tree was removed the server silently stopped
spawning.

These tests pin the corrected rule, mirroring the managed-server refresh loop
just above it in ``_refresh_dynamic_fields``:

* an EXISTING entry has its ``command``/``args`` re-pinned from the contributed
  spec on every rebuild,
* while every user-owned field (``autoApprove``, ``env``, ``disabledTools``,
  ``timeout``, anything that is not ``command``/``args``) is preserved,
* a NEW entry is still seeded whole,
* a malformed (non-dict) existing entry is left untouched, and
* managed servers are unaffected by any of this.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew import agent

EXTRA = "vendor-tools"  # not a managed name; managed names never contain a dash-only vendor form


def _refresh(config: dict[str, Any], monkeypatch, extras: dict[str, dict]) -> dict[str, Any]:
    """Run ``_refresh_dynamic_fields`` with a stubbed edition-extras map."""
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {k: (dict(v) if isinstance(v, dict) else v) for k, v in extras.items()},
    )
    agent._refresh_dynamic_fields(config)
    return config["mcpServers"]


def test_existing_extra_entry_has_command_and_args_refreshed(monkeypatch):
    """The core fix: a stale command/args on an entry the user already has is
    re-pinned from the current contributed spec instead of frozen forever."""
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/old/tree/bin/python", "args": ["--stale"]}},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config,
        monkeypatch,
        {EXTRA: {"command": "/new/tree/bin/python", "args": ["-m", "vendor_server"]}},
    )
    assert mcp[EXTRA]["command"] == "/new/tree/bin/python"
    assert mcp[EXTRA]["args"] == ["-m", "vendor_server"]


def test_user_owned_fields_survive_the_refresh(monkeypatch):
    """env / disabledTools / timeout and any other non-invocation field the
    user set are preserved while command/args are refreshed. ``autoApprove`` is
    the one exception when the invocation CHANGES: see the test below."""
    config: dict[str, Any] = {
        "mcpServers": {
            EXTRA: {
                "command": "/old/tree/bin/python",
                "args": ["--stale"],
                "autoApprove": ["safe_tool"],
                "env": {"VENDOR_TOKEN": "keep-me"},
                "disabledTools": ["risky_tool"],
                "timeout": 42,
            }
        },
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config,
        monkeypatch,
        {EXTRA: {"command": "/new/tree/bin/python", "args": ["ok"], "autoApprove": ["all"]}},
    )
    entry = mcp[EXTRA]
    assert entry["command"] == "/new/tree/bin/python"
    assert entry["args"] == ["ok"]
    assert entry["env"] == {"VENDOR_TOKEN": "keep-me"}
    assert entry["disabledTools"] == ["risky_tool"]
    assert entry["timeout"] == 42


def test_an_unchanged_invocation_keeps_the_users_auto_approve(monkeypatch):
    """Same binary, same argv: the user's local pre-approvals are theirs and
    stay, even when the contributed spec declares a different list."""
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/same", "args": ["a"], "autoApprove": ["safe_tool"]}},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config, monkeypatch, {EXTRA: {"command": "/same", "args": ["a"], "autoApprove": ["all"]}}
    )
    assert mcp[EXTRA]["autoApprove"] == ["safe_tool"]


def test_a_changed_invocation_does_not_carry_the_old_grants_to_the_new_server(monkeypatch):
    """``autoApprove`` is a local pre-approval that skips the PreToolUse hook. A
    user entry an edition later claims is a different server behind the same
    name, so grants made against the old binary are reset to what the
    contributed spec declares -- or removed when it declares none -- and the
    reset is SEL-audited like every other autoApprove writer."""
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(agent, "sel", lambda: _Sel())

    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/users/own", "args": [], "autoApprove": ["run_shell"]}},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config,
        monkeypatch,
        {EXTRA: {"command": "/edition/server", "args": [], "autoApprove": ["list_docs"]}},
    )
    assert mcp[EXTRA]["autoApprove"] == ["list_docs"]
    resets = [e for e in events if e.get("operation") == "mcp_auto_approve_reset"]
    assert len(resets) == 1 and f"@{EXTRA}" in resets[0]["resources"]
    # Counts only, never the grant names or the paths.
    assert "run_shell" not in resets[0]["resources"] and "/users/own" not in resets[0]["resources"]

    events.clear()
    config = {
        "mcpServers": {EXTRA: {"command": "/users/own", "args": [], "autoApprove": ["run_shell"]}},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(config, monkeypatch, {EXTRA: {"command": "/edition/server", "args": []}})
    assert "autoApprove" not in mcp[EXTRA]
    assert any(e.get("operation") == "mcp_auto_approve_reset" for e in events)


def test_a_new_extra_is_seeded_whole(monkeypatch):
    """A contributed server the user does not have yet is written verbatim —
    there is no user preference to respect, so the whole spec lands."""
    config: dict[str, Any] = {"mcpServers": {}, "tools": []}
    spec = {"command": "/tree/bin/python", "args": ["-m", "vendor_server"], "autoApprove": ["x"]}
    mcp = _refresh(config, monkeypatch, {EXTRA: spec})
    assert mcp[EXTRA] == spec


def test_a_malformed_existing_extra_is_left_untouched(monkeypatch):
    """A non-object hand-written entry is not rewritten (that would discard what
    the user meant) and does not raise — the same conservative call the managed
    opt-in loop makes for a malformed entry."""
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: "not-a-mapping"},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config, monkeypatch, {EXTRA: {"command": "/new/tree/bin/python", "args": ["ok"]}}
    )
    assert mcp[EXTRA] == "not-a-mapping"


def test_the_refreshed_args_do_not_alias_the_contributed_spec(monkeypatch):
    """The entry's args are a copy, so a later mutation of the source spec's list
    cannot reach back into the written config."""
    source_args = ["-m", "vendor_server"]
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/old", "args": ["--stale"]}},
        "tools": [f"@{EXTRA}"],
    }
    # Patch directly so we keep a handle on the exact list object the loop reads.
    monkeypatch.setattr(
        agent, "_extra_mcp_servers", lambda: {EXTRA: {"command": "/new", "args": source_args}}
    )
    agent._refresh_dynamic_fields(config)
    written = config["mcpServers"][EXTRA]["args"]
    assert written == ["-m", "vendor_server"]
    source_args.append("--mutated")
    assert written == ["-m", "vendor_server"]


def test_managed_servers_are_unaffected_by_the_extras_refresh(monkeypatch):
    """The extras loop touches only edition-contributed names. A managed server's
    entry (here the always-on core server) is governed by the managed loop and
    keeps its own command, unchanged by anything the extras rule does."""
    # Pick any always-on managed server and capture what a fresh build emits.
    managed_name = next(n for n, s in agent._MANAGED_MCP_SERVERS.items() if not s.get("opt_in"))
    fresh = agent.build_agent_config()["mcpServers"][managed_name]

    config: dict[str, Any] = {
        "mcpServers": {managed_name: {"command": "/tampered", "args": ["nope"]}},
        "tools": [f"@{managed_name}"],
    }
    mcp = _refresh(config, monkeypatch, {EXTRA: {"command": "/new", "args": ["ok"]}})
    # The managed loop restored the real command; the tampered value is gone,
    # and the extras rule did not introduce or alter it.
    assert mcp[managed_name]["command"] == fresh["command"]
    assert mcp[managed_name]["command"] != "/tampered"
    # The extra was still seeded, proving both loops ran.
    assert EXTRA in mcp


def test_an_extra_colliding_with_a_managed_name_is_ignored(monkeypatch, caplog):
    """A contributed spec whose name is a managed server's must not repoint that
    alias: managed names are reserved (some are pre-approved wholesale), so the
    extra is neither seeded nor refreshed, and the managed loop's own command
    stands. The collision is logged so an edition author sees it."""
    managed_name = next(n for n, s in agent._MANAGED_MCP_SERVERS.items() if not s.get("opt_in"))
    fresh = agent.build_agent_config()["mcpServers"][managed_name]

    config: dict[str, Any] = {"mcpServers": {}, "tools": [f"@{managed_name}"]}
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        mcp = _refresh(
            config, monkeypatch, {managed_name: {"command": "/hijack", "args": ["evil"]}}
        )
    assert mcp[managed_name]["command"] == fresh["command"]
    assert mcp[managed_name]["command"] != "/hijack"
    assert mcp[managed_name].get("args") != ["evil"]
    assert any(
        "reserved by a managed server" in rec.getMessage() and managed_name in rec.getMessage()
        for rec in caplog.records
    )


def test_args_dropped_by_the_current_spec_are_removed(monkeypatch):
    """When the contributed spec carries no ``args``, a stale argv is removed
    rather than left behind against the new launcher."""
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/old/launcher", "args": ["--legacy-flag"]}},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(config, monkeypatch, {EXTRA: {"command": "/new/launcher"}})
    assert mcp[EXTRA]["command"] == "/new/launcher"
    assert "args" not in mcp[EXTRA]


def test_a_repin_that_changes_the_invocation_is_logged_without_values(monkeypatch, caplog):
    """Drift becomes visible: a refresh that actually changes command/args logs
    WHICH fields changed, never their values (an argv can carry a secret and
    the log is a persistent file); an unchanged one stays quiet."""
    secret = "tok-SECRET-VALUE-123"
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/old", "args": ["--token", secret]}},
        "tools": [f"@{EXTRA}"],
    }
    with caplog.at_level(logging.INFO, logger=agent.logger.name):
        _refresh(config, monkeypatch, {EXTRA: {"command": "/new", "args": ["--token", secret]}})
    hits = [
        rec.getMessage()
        for rec in caplog.records
        if "Re-pinned edition-contributed MCP server" in rec.getMessage()
    ]
    assert len(hits) == 1 and EXTRA in hits[0]
    assert "command" in hits[0] and "args" not in hits[0].split("(")[-1]
    assert secret not in hits[0] and "/old" not in hits[0] and "/new" not in hits[0]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=agent.logger.name):
        _refresh(config, monkeypatch, {EXTRA: {"command": "/new", "args": ["--token", secret]}})
    assert not any("Re-pinned edition-contributed" in rec.getMessage() for rec in caplog.records)


def test_the_fresh_build_also_refuses_an_extra_colliding_with_a_managed_name(monkeypatch, caplog):
    """The build path applies the same reserved-name rule as the refresh path,
    including for a managed server that is absent from a fresh build (opt-in or
    gated off): without the guard the extra would seed under that alias there
    and the refresh guard would then leave the collision standing forever."""
    absent = next(n for n, s in agent._MANAGED_MCP_SERVERS.items() if s.get("opt_in"))
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {
            absent: {"command": "/hijack", "args": ["evil"]},
            EXTRA: {"command": "/vendor", "args": ["ok"]},
        },
    )
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        mcp = agent.build_agent_config()["mcpServers"]
    # The opt-in managed name is not seeded at all (neither by the managed loop
    # nor by the colliding extra); the honest extra still lands.
    assert absent not in mcp
    assert mcp[EXTRA]["command"] == "/vendor"
    assert any(
        "reserved by a managed server" in rec.getMessage() and absent in rec.getMessage()
        for rec in caplog.records
    )


def test_a_non_mapping_contributed_spec_leaves_the_existing_entry_alone(monkeypatch, caplog):
    """A host that contributes a non-mapping spec (declared reachable by
    ``_mcp_server_emission_eligible``) must not touch an entry the user already
    has: a str would make ``"args" in spec`` a substring test that strips the
    argv, and None would raise and force a rebuild from defaults."""
    for bad in ("not-a-mapping", None, 7):
        config: dict[str, Any] = {
            "mcpServers": {EXTRA: {"command": "/keep", "args": ["--keep"], "autoApprove": ["x"]}},
            "tools": [f"@{EXTRA}"],
        }
        with caplog.at_level(logging.WARNING, logger=agent.logger.name):
            mcp = _refresh(config, monkeypatch, {EXTRA: bad})  # type: ignore[dict-item]
        assert mcp[EXTRA] == {"command": "/keep", "args": ["--keep"], "autoApprove": ["x"]}
        assert any(
            "spec is not a mapping" in rec.getMessage() and EXTRA in rec.getMessage()
            for rec in caplog.records
        )
        caplog.clear()

    # And the fresh build neither seeds it nor raises.
    monkeypatch.setattr(agent, "_extra_mcp_servers", lambda: {EXTRA: "not-a-mapping"})
    assert EXTRA not in agent.build_agent_config()["mcpServers"]


def test_a_stale_invocation_in_the_user_store_does_not_defeat_the_repin(monkeypatch, tmp_path):
    """End to end through ``install_agent``: the kirocrew user store
    (``mcp.json``) merges with ``update()`` AFTER the refresh, and the dashboard
    writes a rendered ``command``/``args`` snapshot into that store when a row is
    disabled. For an edition-contributed name the invocation is host-owned, so
    the store's stale ``command``/``args`` must not overwrite the re-pinned one,
    while its other fields (here ``timeout``) still land."""
    import json

    from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

    cfg_dir = bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    # A previous rebuild left the entry frozen on a launcher that has moved.
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": [f"@{EXTRA}"],
                "allowedTools": [],
                "mcpServers": {EXTRA: {"command": "/old/tree/launcher", "args": ["--stale"]}},
            }
        )
    )
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {EXTRA: {"command": "/new/tree/launcher", "args": ["--current"]}},
    )
    config = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        # The store snapshot the dashboard's disable/enable path leaves behind,
        # autoApprove included: it was granted against the OLD binary.
        kirocrew_servers={
            EXTRA: {
                "command": "/old/tree/launcher",
                "args": ["--stale"],
                "timeout": 45,
                "autoApprove": ["dangerous_tool"],
            }
        },
    )
    entry = config["mcpServers"][EXTRA]
    assert entry["command"] == "/new/tree/launcher"
    assert entry["args"] == ["--current"]
    assert entry["timeout"] == 45
    # The refresh revoked the grant on the changed invocation; the store must not
    # hand it back to the new server.
    assert "dangerous_tool" not in entry.get("autoApprove", [])


def test_a_store_snapshot_of_the_current_invocation_keeps_its_auto_approve(monkeypatch, tmp_path):
    """The store's grants stay when its recorded invocation IS the re-pinned
    one: those grants were made against this very binary. A store entry with no
    ``command`` (a field-level user override, not a snapshot) is merged whole."""
    import json

    from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

    cfg_dir = bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": [f"@{EXTRA}", "@other-vendor"],
                "allowedTools": [],
                "mcpServers": {
                    EXTRA: {"command": "/tree/launcher", "args": ["--current"]},
                    "other-vendor": {"command": "/tree/launcher", "args": ["--current"]},
                },
            }
        )
    )
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {
            EXTRA: {"command": "/tree/launcher", "args": ["--current"]},
            "other-vendor": {"command": "/tree/launcher", "args": ["--current"]},
        },
    )
    config = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        kirocrew_servers={
            EXTRA: {
                "command": "/tree/launcher",
                "args": ["--current"],
                "autoApprove": ["safe_tool"],
            },
            "other-vendor": {"autoApprove": ["safe_tool"]},
        },
    )
    assert config["mcpServers"][EXTRA]["autoApprove"] == ["safe_tool"]
    assert config["mcpServers"]["other-vendor"]["autoApprove"] == ["safe_tool"]


def test_a_contributed_stdio_spec_replaces_a_stale_remote_transport_as_a_unit(monkeypatch):
    """At emission an entry carrying ``url`` takes the remote branch before the
    command is looked at, so a leftover ``url`` on the contributed name would
    shadow the freshly pinned command and the spec's ``autoApprove`` would then
    pre-approve that remote server. A stdio contribution therefore drops the
    remote-transport keys, and the switch counts as an invocation change."""
    config: dict[str, Any] = {
        "mcpServers": {
            EXTRA: {
                "url": "https://stale.example/mcp",
                "headers": {"Authorization": "Bearer old"},
                "autoApprove": ["remote_tool"],
                "timeout": 30,
            }
        },
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config,
        monkeypatch,
        {EXTRA: {"command": "/edition/server", "args": ["--stdio"], "autoApprove": ["list_docs"]}},
    )
    entry = mcp[EXTRA]
    assert entry["command"] == "/edition/server"
    assert entry["args"] == ["--stdio"]
    assert "url" not in entry and "headers" not in entry
    assert entry["autoApprove"] == ["list_docs"]
    assert entry["timeout"] == 30


def test_a_url_only_store_snapshot_does_not_restore_the_remote_onto_the_stdio_entry(
    monkeypatch, tmp_path
):
    """The dashboard's disable path snapshots whatever the entry was, so a store
    row can be URL-only (``url``/``headers``/``autoApprove``, no ``command``).
    Merged whole it would put ``url`` back on the re-pinned stdio entry -- and
    at emission ``url`` wins before the command is looked at -- with the old
    remote's grants attached. The whole transport is excluded and the grants
    are dropped because the store's transport differs from the re-pinned one."""
    import json

    from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

    cfg_dir = bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": [f"@{EXTRA}"],
                "allowedTools": [],
                "mcpServers": {EXTRA: {"url": "https://stale.example/mcp"}},
            }
        )
    )
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {EXTRA: {"command": "/edition/server", "args": ["--stdio"]}},
    )
    config = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        kirocrew_servers={
            EXTRA: {
                "url": "https://stale.example/mcp",
                "headers": {"Authorization": "Bearer old"},
                "autoApprove": ["remote_tool"],
                "timeout": 30,
            }
        },
    )
    entry = config["mcpServers"][EXTRA]
    assert entry["command"] == "/edition/server"
    assert entry["args"] == ["--stdio"]
    assert "url" not in entry and "headers" not in entry
    assert "remote_tool" not in entry.get("autoApprove", [])
    assert entry["timeout"] == 30


def test_a_stale_store_launcher_is_not_a_resolution_fallback_for_a_contributed_name(
    monkeypatch, tmp_path
):
    """When the contributed launcher does not resolve but the store's stale one
    does, the store must not win the resolution fallback: that would run the
    old binary under the current entry's grants. The server is dropped with the
    usual warning instead, and the next resolvable rebuild re-pins it."""
    import json

    from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

    cfg_dir = bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": [f"@{EXTRA}"],
                "allowedTools": [],
                "mcpServers": {EXTRA: {"command": "/old/tree/launcher", "args": ["--stale"]}},
            }
        )
    )
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {EXTRA: {"command": "/new/tree/launcher", "args": ["--current"]}},
    )
    config = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        kirocrew_servers={EXTRA: {"command": "/old/tree/launcher", "args": ["--stale"]}},
        # Only the stale launcher resolves.
        which_side_effect=lambda c, **kw: None if c == "/new/tree/launcher" else c,
    )
    entry = config["mcpServers"].get(EXTRA)
    assert entry is None or entry.get("command") != "/old/tree/launcher"


def test_a_non_mapping_contribution_does_not_withhold_the_store_transport(monkeypatch, tmp_path):
    """A non-mapping contribution is refused by the refresh, so the recorded entry
    is not re-pinned. The store's transport for that name must then still merge
    and still serve as a resolution fallback, or a working server would be
    stranded with nothing to re-pin it and nothing to fall back to."""
    import json

    from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

    cfg_dir = bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": [f"@{EXTRA}"],
                "allowedTools": [],
                "mcpServers": {EXTRA: {"command": "/gone/launcher", "args": ["--old"]}},
            }
        )
    )
    monkeypatch.setattr(agent, "_extra_mcp_servers", lambda: {EXTRA: "not-a-mapping"})
    config = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        kirocrew_servers={EXTRA: {"command": "/store/launcher", "args": ["--store"]}},
        which_side_effect=lambda c, **kw: None if c == "/gone/launcher" else c,
    )
    entry = config["mcpServers"][EXTRA]
    assert entry["command"] == "/store/launcher"
    assert entry["args"] == ["--store"]


def test_a_resolved_bare_command_is_not_a_change_on_the_next_rebuild(monkeypatch):
    """The rebuild persists the resolved absolute path of a bare contributed
    command and records ``(source, emitted)`` in the provenance record. The next
    refresh must judge "changed" against that source, or every rebuild would
    read the absolute path as a change, reset ``autoApprove`` and emit a SEL
    event forever."""
    from kiro_crew.mcp_provenance import record_derived

    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(agent, "sel", lambda: _Sel())

    stored = record_derived(
        {"command": "/opt/tools/bin/npx", "args": ["-y", "vendor-mcp"], "autoApprove": ["mine"]},
        ("npx", "/opt/tools/bin/npx"),
    )
    config: dict[str, Any] = {"mcpServers": {EXTRA: stored}, "tools": [f"@{EXTRA}"]}
    mcp = _refresh(config, monkeypatch, {EXTRA: {"command": "npx", "args": ["-y", "vendor-mcp"]}})
    assert mcp[EXTRA]["autoApprove"] == ["mine"]
    assert not any(e.get("operation") == "mcp_auto_approve_reset" for e in events)


def test_a_url_only_contribution_does_not_withhold_the_stores_url(monkeypatch, tmp_path):
    """The refresh re-pins a transport only for a stdio contribution, so a
    URL-only contribution has no other writer for ``url``: the store's updated
    endpoint must still land."""
    import json

    from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

    cfg_dir = bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": [f"@{EXTRA}"],
                "allowedTools": [],
                "mcpServers": {EXTRA: {"url": "https://old.example/mcp"}},
            }
        )
    )
    monkeypatch.setattr(
        agent, "_extra_mcp_servers", lambda: {EXTRA: {"url": "https://old.example/mcp"}}
    )
    config = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        kirocrew_servers={EXTRA: {"url": "https://new.example/mcp"}},
    )
    assert config["mcpServers"][EXTRA]["url"] == "https://new.example/mcp"


def test_a_managed_name_collision_is_not_emission_eligible(monkeypatch):
    """Both spec paths refuse a contributed name that collides with a managed
    server, so the shared eligibility predicate the dashboard's merge-on-write
    preserves by must not vote it eligible from the extras half either -- or a
    refused entry would be unrevocable there."""
    absent = next(n for n, s in agent._MANAGED_MCP_SERVERS.items() if s.get("opt_in"))
    monkeypatch.setattr(
        agent,
        "_extra_mcp_servers",
        lambda: {absent: {"command": "/hijack"}, EXTRA: {"command": "/vendor"}},
    )
    eligible = agent.emission_eligible_mcp_servers()
    assert EXTRA in eligible
    assert absent not in eligible


def test_the_same_program_at_a_moved_path_keeps_the_users_grants(monkeypatch):
    """The fix's own scenario: the contributed launcher moved between install
    trees (same executable name, same argv). That is the same program, so the
    user's local pre-approvals follow it and no reset event is emitted."""
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(agent, "sel", lambda: _Sel())

    config: dict[str, Any] = {
        "mcpServers": {
            EXTRA: {
                "command": "/tools/1.0/bin/python3",
                "args": ["-m", "vendor_mcp"],
                "autoApprove": ["mine"],
            }
        },
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(
        config,
        monkeypatch,
        {EXTRA: {"command": "/tools/2.0/bin/python3", "args": ["-m", "vendor_mcp"]}},
    )
    assert mcp[EXTRA]["command"] == "/tools/2.0/bin/python3"
    assert mcp[EXTRA]["autoApprove"] == ["mine"]
    assert not any(e.get("operation") == "mcp_auto_approve_reset" for e in events)


def test_a_url_only_contribution_leaves_an_existing_stdio_entry_alone(monkeypatch):
    """A URL-only contribution carries no invocation to pin; stripping the
    existing entry's argv without adopting the URL would leave a stdio command
    with the wrong argv. The entry is left exactly as it was."""
    config: dict[str, Any] = {
        "mcpServers": {EXTRA: {"command": "/keep", "args": ["--keep"], "autoApprove": ["x"]}},
        "tools": [f"@{EXTRA}"],
    }
    mcp = _refresh(config, monkeypatch, {EXTRA: {"url": "https://remote.example/mcp"}})
    assert mcp[EXTRA] == {"command": "/keep", "args": ["--keep"], "autoApprove": ["x"]}

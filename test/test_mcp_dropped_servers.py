"""A server the rebuild drops for a missing command is recorded and shown on its row.

Without the record the drop reaches only the gateway log: the dashboard lists the
server while the agent has none of its tools. The rebuild records each
command-not-found drop, and ``GET /api/mcp`` stamps it onto the server's row as
``agentDropped``. The same record carries the user's own entries an edition server
of the same name outranks, stamped as ``ownEntryUnused`` and printed by doctor.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

from kiro_crew import mcp_dropped_servers
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_discovery import McpServerInfo


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch):
    # Every rebuild ends in a spec-permissions gate that would otherwise run the
    # host's real ``kiro-cli --version``.
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _request() -> Any:
    class _App(dict):
        pass

    class _Req(dict):
        def __init__(self, app):
            super().__init__(user="local-app", app="")
            self.app = app

    app = _App()
    app["state"] = SimpleNamespace(
        _background_tasks=set(), sessions=SimpleNamespace(final_drain_started=False)
    )
    return _Req(app)


async def _rows(monkeypatch, tmp_path) -> dict[str, dict]:
    import kiro_crew.mcp_discovery as disc

    servers = [McpServerInfo(name="example-server", command="example-mcp-program")]
    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))
    resp = await mcp_mod.api_mcp_servers(_request())
    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return {r["name"]: r for r in json.loads(body)}


@pytest.mark.asyncio
async def test_a_command_not_found_drop_is_recorded_and_shown_until_it_resolves(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    cfg_dir = bundled_defaults(tmp_path)
    kiro_servers = {"example-server": {"command": "example-mcp-program"}}

    def _missing(c, **kw):
        return None if c == "example-mcp-program" else c

    emitted = run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, which_side_effect=_missing
    )
    assert "example-server" not in emitted["mcpServers"]
    assert mcp_dropped_servers.load_dropped() == {
        "example-server": {"command": "example-mcp-program", "reason": "command_not_found"}
    }
    row = (await _rows(monkeypatch, tmp_path))["example-server"]
    assert row["agentDropped"] == {
        "command": "example-mcp-program",
        "reason": "command_not_found",
    }

    # Once the program is installed, the next rebuild mounts it and the note goes.
    binary = tmp_path / "bin" / "example-mcp-program"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    def _found(c, **kw):
        return str(binary) if c == "example-mcp-program" else c

    emitted = run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, which_side_effect=_found
    )
    assert "example-server" in emitted["mcpServers"]
    assert mcp_dropped_servers.load_dropped() == {}
    assert "agentDropped" not in (await _rows(monkeypatch, tmp_path))["example-server"]


def _drop_warnings(caplog, name: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.getMessage().startswith(f"Dropping MCP server {name!r}")
    ]


def test_a_switched_off_server_with_a_missing_program_is_not_a_drop(tmp_path, caplog) -> None:
    cfg_dir = bundled_defaults(tmp_path)
    kiro_servers: dict[str, dict] = {
        "on-server": {"command": "missing-on"},
        "off-server": {"command": "missing-off", "disabled": True},
    }
    # The store's own copy says ``false`` and is merged last: the switch in the
    # Kiro global still wins, as it does for the listing.
    store = {"off-server": {"command": "missing-off", "disabled": False}}

    def _missing(c, **kw):
        return None if c in ("missing-on", "missing-off") else c

    def _rebuild() -> dict:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent"):
            return run_install_mcp_merge(
                tmp_path,
                cfg_dir,
                cc_servers={},
                kiro_servers=kiro_servers,
                kirocrew_servers=store,
                which_side_effect=_missing,
            )

    _rebuild()
    # Per-tool refs the user wrote for both servers, and a grant on the one that is on.
    spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["tools"] += ["@off-server/echo", "@on-server/echo"]
    spec["allowedTools"] += ["@on-server/echo"]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    emitted = _rebuild()
    assert mcp_dropped_servers.load_dropped() == {
        "on-server": {"command": "missing-on", "reason": "command_not_found"}
    }
    assert _drop_warnings(caplog, "on-server")
    assert _drop_warnings(caplog, "off-server") == []
    # Its refs are kept for when it is switched back on; it holds no grant meanwhile.
    assert {"@off-server/echo", "@on-server/echo"} <= set(emitted["tools"])
    assert "@on-server/echo" in emitted["allowedTools"]
    assert not any(t.startswith("@off-server") for t in emitted["allowedTools"])

    rows = [{"name": "off-server", "enabled": False}, {"name": "on-server", "enabled": True}]
    mcp_mod._annotate_agent_drops(rows)
    assert "agentDropped" not in rows[0]
    assert rows[1]["agentDropped"]["command"] == "missing-on"

    # Switched back on, it is a drop again.
    kiro_servers["off-server"] = {"command": "missing-off", "disabled": False}
    store = {"off-server": {"command": "missing-off"}}
    emitted = _rebuild()
    assert mcp_dropped_servers.load_dropped()["off-server"] == {
        "command": "missing-off",
        "reason": "command_not_found",
    }
    assert _drop_warnings(caplog, "off-server")
    assert "@off-server/echo" in emitted["tools"]


def test_a_row_switched_off_after_the_rebuild_drops_its_note() -> None:
    # A toggle runs no rebuild, so the record still names the server it turned off.
    mcp_dropped_servers.record_rebuild_drops({"example-server": "example-mcp-program"})
    rows: list[dict] = [
        {
            "name": "example-server",
            "enabled": False,
            "agentDropped": {"command": "example-mcp-program", "reason": "command_not_found"},
        }
    ]

    mcp_mod._annotate_agent_drops(rows)

    assert "agentDropped" not in rows[0]


def test_a_raw_slashed_drop_does_not_mark_a_surviving_alias(tmp_path) -> None:
    cfg_dir = bundled_defaults(tmp_path)
    kiro_servers = {
        "playwright-mcp": {"command": "working-playwright"},
        "npm:@playwright/mcp": {"command": "missing-playwright"},
    }
    mcp_dropped_servers.record_rebuild_drops({"npm:@playwright/mcp": "missing-playwright"})

    def _resolve(command, **kw):
        return None if command == "missing-playwright" else command

    emitted = run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, which_side_effect=_resolve
    )

    assert "npm:@playwright/mcp" not in emitted["mcpServers"]
    assert emitted["mcpServers"]["playwright-mcp"]["command"] == "working-playwright"
    assert mcp_dropped_servers.load_dropped() == {}
    rows = [{"name": "playwright-mcp"}]
    mcp_mod._annotate_agent_drops(rows)
    assert "agentDropped" not in rows[0]


def test_a_raw_slashed_drop_is_shown_on_its_canonical_row() -> None:
    mcp_dropped_servers.record_rebuild_drops({"npm:@playwright/mcp": "missing-playwright"})
    rows = [{"name": "playwright-mcp"}]

    mcp_mod._annotate_agent_drops(rows)

    assert rows[0]["agentDropped"] == {
        "command": "missing-playwright",
        "reason": "command_not_found",
    }


def test_an_exact_drop_wins_over_an_aliased_drop() -> None:
    mcp_dropped_servers.record_rebuild_drops(
        {"playwright-mcp": "exact-program", "npm:@playwright/mcp": "raw-program"}
    )
    rows: list[dict] = [{"name": "playwright-mcp"}, {"name": "npm:@playwright/mcp"}]

    mcp_mod._annotate_agent_drops(rows)

    assert [row["agentDropped"]["command"] for row in rows] == ["exact-program", "raw-program"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["api_mcp_probe", "api_mcp_probe_cached"])
async def test_probe_response_carries_current_agent_drops(monkeypatch, tmp_path, endpoint) -> None:
    import kiro_crew.mcp_discovery as disc

    await _rows(monkeypatch, tmp_path)
    servers = [McpServerInfo(name="example-server", command="example-mcp-program")]
    monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=servers))
    monkeypatch.setattr(mcp_mod, "_evaluate_shareability", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [server.to_dict() for server in servers])
    mcp_dropped_servers.record_rebuild_drops({"example-server": "example-mcp-program"})

    resp = await getattr(mcp_mod, endpoint)(_request())

    assert resp.status == 200
    assert json.loads(resp.body)[0]["agentDropped"] == {
        "command": "example-mcp-program",
        "reason": "command_not_found",
    }

    mcp_dropped_servers.record_rebuild_drops({})
    refreshed = await mcp_mod.api_mcp_probe_cached(_request())
    assert refreshed.status == 200
    body = refreshed.body
    assert isinstance(body, (bytes, bytearray))
    assert "agentDropped" not in json.loads(body)[0]


_UNUSED = {"file": "~/.kiro/settings/mcp.json", "command": "/opt/own/example-mcp"}


@pytest.mark.asyncio
async def test_an_unused_own_entry_is_shown_on_its_row_until_the_record_clears(
    monkeypatch, tmp_path
) -> None:
    mcp_dropped_servers.record_rebuild_drops({}, unused_own={"example-server": _UNUSED})
    assert mcp_dropped_servers.load_unused_own() == {"example-server": _UNUSED}
    row = (await _rows(monkeypatch, tmp_path))["example-server"]
    assert row["ownEntryUnused"] == _UNUSED
    assert "agentDropped" not in row

    mcp_dropped_servers.record_rebuild_drops({})
    assert "ownEntryUnused" not in (await _rows(monkeypatch, tmp_path))["example-server"]


def test_an_unused_own_entry_under_a_slash_name_is_shown_on_its_canonical_row() -> None:
    """Recorded under the source key, listed under its alias, exact name first."""
    mcp_dropped_servers.record_rebuild_drops(
        {}, unused_own={"ns/notes": _UNUSED, "plain": {**_UNUSED, "command": "exact"}}
    )
    rows: list[dict] = [{"name": "ns-notes"}, {"name": "plain"}, {"name": "other"}]

    mcp_mod._annotate_agent_drops(rows)

    assert rows[0]["ownEntryUnused"] == _UNUSED
    assert rows[1]["ownEntryUnused"]["command"] == "exact"
    assert "ownEntryUnused" not in rows[2]


def test_a_malformed_unused_record_is_left_out() -> None:
    mcp_dropped_servers.store_path().write_text(
        json.dumps(
            {
                "version": mcp_dropped_servers.STORE_VERSION,
                "servers": {},
                "unused_own": {"ok": _UNUSED, "no-file": {"command": "x"}, "bad": ["x"]},
            }
        ),
        encoding="utf-8",
    )
    assert mcp_dropped_servers.load_unused_own() == {"ok": _UNUSED}


def test_doctor_names_the_unused_own_entry(capsys) -> None:
    from kiro_crew import cli_doctor

    mcp_dropped_servers.record_rebuild_drops({}, unused_own={"example-server": _UNUSED})

    cli_doctor._doctor_unused_own_entries()

    out = capsys.readouterr().out
    assert "@example-server: ⚠️  your own entry in ~/.kiro/settings/mcp.json" in out
    assert "(/opt/own/example-mcp) is not used" in out
    assert "Give yours a different name in that file to run it too." in out
    assert "in its place" not in out


@pytest.mark.asyncio
async def test_an_unused_url_entry_reaches_the_inventory_without_its_credentials(
    monkeypatch, tmp_path
) -> None:
    """``GET /api/mcp`` serves the record as stored, so the URL is cut before it is stored."""
    from kiro_crew.testing.synthetic_credentials import synth

    password, token = synth(12, seed=7), synth(24, seed=8)
    url = f"https://alice:{password}@mcp.example.test/v1/{token}/mcp?token={token}#frag"
    mcp_dropped_servers.record_rebuild_drops(
        {}, unused_own={"example-server": {"file": "~/.kiro/crew/mcp.json", "command": url}}
    )

    row = (await _rows(monkeypatch, tmp_path))["example-server"]
    assert row["ownEntryUnused"]["command"] == "https://mcp.example.test"
    stored = mcp_dropped_servers.store_path().read_text(encoding="utf-8")
    for secret in ("alice", password, token, "#frag"):
        assert secret not in json.dumps(row)
        assert secret not in stored


def test_doctor_prints_an_unused_url_entry_without_its_credentials(capsys) -> None:
    from kiro_crew import cli_doctor
    from kiro_crew.testing.synthetic_credentials import synth

    password, token = synth(12, seed=5), synth(24, seed=6)
    url = f"https://alice:{password}@mcp.example.test/v1/{token}/mcp?token={token}#frag"
    mcp_dropped_servers.record_rebuild_drops(
        {}, unused_own={"example-server": {"file": "~/.kiro/crew/mcp.json", "command": url}}
    )

    cli_doctor._doctor_unused_own_entries()

    out = capsys.readouterr().out
    assert "(https://mcp.example.test) is not used" in out
    for secret in ("alice", password, token, "#frag"):
        assert secret not in out

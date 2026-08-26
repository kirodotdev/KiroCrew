"""Wire-contract tests for the MCP dashboard handlers' un-exercised branches.

Covers the request/response surface of ``mcp.py`` that the existing suites
(``test_mcp_apply``, ``test_mcp_sync_agent``, ``test_mcp_probe_treadmill``,
``test_mcp_gateway_control_plane``) leave untouched: the enable/disable
toggles (server, per-tool, all), removal, the per-agent active list, the
probe endpoints, and the MCP-gateway metrics / server-enumeration /
set-stub endpoints — with their validation matrix, corrupt-config and
write-failure branches, and every documented status code.

Every filesystem seam is redirected into ``tmp_path``; no network, no real
MCP server process, no capability-manager subprocess.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo

# ── harness ─────────────────────────────────────────────────────────────


def _request(
    body: Any = None,
    *,
    state: Any = None,
    query: dict[str, str] | None = None,
    match_info: dict[str, str] | None = None,
    method: str = "POST",
) -> web.Request:
    """Minimal aiohttp request double, matching the suite's existing style."""
    req = MagicMock(spec=web.Request)
    if isinstance(body, Exception):
        req.json = AsyncMock(side_effect=body)
    else:
        req.json = AsyncMock(return_value=body)
    req.app = {"state": state if state is not None else _State()}
    req.query = query or {}
    req.match_info = match_info or {}
    req.method = method
    req.get = lambda key, default=None: default
    return req


class _State:
    """Stand-in for DashboardState's background-task registry."""

    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task] = set()


def _payload(resp: web.Response) -> Any:
    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return json.loads(body)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point every mcp.py filesystem + audit seam at ``tmp_path``.

    The agent-config sync (``kirocrew.json`` read-modify-write) belongs to
    ``handlers.agents`` and has its own suite — record the calls instead of
    re-testing it here.
    """
    global_json = tmp_path / "kiro" / "settings" / "mcp.json"
    global_json.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_json)
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", global_json.with_suffix(".lock"))
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "crew" / "mcp.json")
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)
    sel = MagicMock()
    monkeypatch.setattr(mcp_mod, "sel", lambda: sel)

    synced: list[tuple[str, bool, bool]] = []
    batched: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        mcp_mod,
        "_sync_mcp_to_agent",
        lambda name, enabled, remove=False: synced.append((name, enabled, remove)),
    )
    monkeypatch.setattr(
        mcp_mod,
        "_sync_mcp_to_agent_batch",
        lambda names, enabled: batched.append((list(names), enabled)),
    )
    return SimpleNamespace(
        global_json=global_json,
        synced=synced,
        batched=batched,
        sel=sel,
    )


def _write_global(sandbox: SimpleNamespace, servers: dict[str, Any]) -> None:
    sandbox.global_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _read_global(sandbox: SimpleNamespace) -> dict[str, Any]:
    data = json.loads(sandbox.global_json.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", {})
    assert isinstance(servers, dict)
    return servers


def _known(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make ``list_servers()`` report ``names`` as configured somewhere."""
    import kiro_crew.mcp_discovery as disc

    rows = [McpServerInfo(name=n, command="/bin/true") for n in names]
    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(rows))


# ── POST /api/mcp/toggle ────────────────────────────────────────────────


class TestToggleServer:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_toggle(_request(ValueError("boom")))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_missing_name_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_toggle(_request({"enabled": False}))
        assert resp.status == 400
        assert "name is required" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_corrupt_global_config_is_500(self, sandbox: SimpleNamespace) -> None:
        sandbox.global_json.write_text("{not json", encoding="utf-8")
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv"}))
        assert resp.status == 500
        assert "cannot parse" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_unknown_server_is_404(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _known(monkeypatch)  # nothing configured anywhere
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "ghost"}))
        assert resp.status == 404
        assert "not found" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_disable_writes_flag_and_syncs(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"srv": {"command": "x"}})
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False}))
        assert resp.status == 200
        assert _payload(resp) == {
            "ok": True,
            "name": "srv",
            "enabled": False,
            "applied": True,
        }
        assert _read_global(sandbox)["srv"]["disabled"] is True
        assert sandbox.synced == [("srv", False, False)]

    @pytest.mark.asyncio
    async def test_enable_clears_flag(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"srv": {"command": "x", "disabled": True}})
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": True}))
        assert resp.status == 200
        assert "disabled" not in _read_global(sandbox)["srv"]
        assert sandbox.synced == [("srv", True, False)]

    @pytest.mark.asyncio
    async def test_server_known_elsewhere_gets_a_stub_entry(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server configured only in another scope still records its state here."""
        _known(monkeypatch, "elsewhere")
        resp = await mcp_mod.api_mcp_toggle(
            _request({"name": "elsewhere", "enabled": False})
        )
        assert resp.status == 200
        assert _read_global(sandbox)["elsewhere"] == {"disabled": True}

    @pytest.mark.asyncio
    async def test_string_spec_is_coerced_to_a_command_dict(
        self, sandbox: SimpleNamespace
    ) -> None:
        _write_global(sandbox, {"srv": "run-me"})
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False}))
        assert resp.status == 200
        assert _read_global(sandbox)["srv"] == {"command": "run-me", "disabled": True}

    @pytest.mark.asyncio
    async def test_non_mapping_spec_is_500(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"srv": ["not", "a", "spec"]})
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv"}))
        assert resp.status == 500
        assert "invalid config type: list" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_write_failure_is_500(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_global(sandbox, {"srv": {"command": "x"}})

        def _boom(_data: dict) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mcp_mod, "_write_mcp_json", _boom)
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False}))
        assert resp.status == 500
        assert "disk full" in _payload(resp)["error"]
        assert sandbox.synced == []  # never syncs on a failed write

    @pytest.mark.asyncio
    async def test_missing_global_file_is_treated_as_empty(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No mcp.json yet: the handler starts from an empty document.

        With discovery reporting nothing either, that empty document yields the
        documented 404 rather than an unhandled ``FileNotFoundError``.
        """
        sandbox.global_json.unlink(missing_ok=True)
        _known(monkeypatch)
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False}))
        assert resp.status == 404


# ── POST /api/mcp/toggle-tool ───────────────────────────────────────────


class TestToggleTool:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_toggle_tool(_request(ValueError("boom")))
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{"tool": "ReadFile"}, {"server": "srv"}, {}],
    )
    async def test_missing_fields_are_400(
        self, sandbox: SimpleNamespace, body: dict
    ) -> None:
        resp = await mcp_mod.api_mcp_toggle_tool(_request(body))
        assert resp.status == 400
        assert "server and tool are required" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_corrupt_global_config_is_500(self, sandbox: SimpleNamespace) -> None:
        sandbox.global_json.write_text("nope", encoding="utf-8")
        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "T"})
        )
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_unknown_server_is_404(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _known(monkeypatch)
        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "ghost", "tool": "T"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_disable_then_reenable_round_trips(
        self, sandbox: SimpleNamespace
    ) -> None:
        _write_global(sandbox, {"srv": {"command": "x"}})

        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "ReadFile", "enabled": False})
        )
        assert resp.status == 200
        assert _payload(resp)["tool"] == "ReadFile"
        assert _read_global(sandbox)["srv"]["disabledTools"] == ["ReadFile"]

        # Disabling the same tool twice must not duplicate it.
        await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "ReadFile", "enabled": False})
        )
        assert _read_global(sandbox)["srv"]["disabledTools"] == ["ReadFile"]

        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "ReadFile", "enabled": True})
        )
        assert resp.status == 200
        # Emptying the list drops the key entirely rather than leaving [].
        assert "disabledTools" not in _read_global(sandbox)["srv"]

    @pytest.mark.asyncio
    async def test_server_known_elsewhere_gets_a_stub_entry(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server configured only in another scope still records tool state here."""
        _known(monkeypatch, "elsewhere")
        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "elsewhere", "tool": "T", "enabled": False})
        )
        assert resp.status == 200
        assert _read_global(sandbox)["elsewhere"] == {"disabledTools": ["T"]}

    @pytest.mark.asyncio
    async def test_string_spec_is_coerced(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"srv": "run-me"})
        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "T", "enabled": False})
        )
        assert resp.status == 200
        assert _read_global(sandbox)["srv"] == {
            "command": "run-me",
            "disabledTools": ["T"],
        }

    @pytest.mark.asyncio
    async def test_non_mapping_spec_is_500(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"srv": 7})
        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "T"})
        )
        assert resp.status == 500
        assert "invalid config type: int" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_write_failure_is_500(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_global(sandbox, {"srv": {"command": "x"}})

        def _boom(_data: dict) -> None:
            raise OSError("read-only fs")

        monkeypatch.setattr(mcp_mod, "_write_mcp_json", _boom)
        resp = await mcp_mod.api_mcp_toggle_tool(
            _request({"server": "srv", "tool": "T", "enabled": False})
        )
        assert resp.status == 500
        assert "read-only fs" in _payload(resp)["error"]


# ── POST /api/mcp/toggle-all ────────────────────────────────────────────


class TestToggleAll:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_toggle_all(_request(ValueError("boom")))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_corrupt_global_config_is_500(self, sandbox: SimpleNamespace) -> None:
        sandbox.global_json.write_text("[", encoding="utf-8")
        resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": False}))
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_disables_every_mapping_spec_and_skips_others(
        self, sandbox: SimpleNamespace
    ) -> None:
        _write_global(
            sandbox, {"a": {"command": "x"}, "b": {"command": "y"}, "junk": "str-spec"}
        )
        resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": False}))
        assert resp.status == 200
        assert _payload(resp) == {"ok": True, "enabled": False, "count": 3}
        servers = _read_global(sandbox)
        assert servers["a"]["disabled"] is True
        assert servers["b"]["disabled"] is True
        assert servers["junk"] == "str-spec"  # non-mapping specs are left alone
        # Only the two toggleable names reach the batch sync.
        assert sandbox.batched == [(["a", "b"], False)]

    @pytest.mark.asyncio
    async def test_enable_all_clears_flags(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"a": {"command": "x", "disabled": True}})
        resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": True}))
        assert resp.status == 200
        assert "disabled" not in _read_global(sandbox)["a"]
        assert sandbox.batched == [(["a"], True)]

    @pytest.mark.asyncio
    async def test_missing_global_file_reports_zero_servers(
        self, sandbox: SimpleNamespace
    ) -> None:
        """No mcp.json yet: the handler starts from an empty document, not a 500."""
        sandbox.global_json.unlink(missing_ok=True)
        resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": False}))
        assert resp.status == 200
        assert _payload(resp) == {"ok": True, "enabled": False, "count": 0}

    @pytest.mark.asyncio
    async def test_write_failure_is_500(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_global(sandbox, {"a": {"command": "x"}})
        monkeypatch.setattr(
            mcp_mod,
            "_write_mcp_json",
            lambda _d: (_ for _ in ()).throw(OSError("nope")),
        )
        resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": False}))
        assert resp.status == 500
        assert sandbox.batched == []


# ── POST /api/mcp/remove ────────────────────────────────────────────────


@pytest.fixture
def no_capability_manager(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Pin the capability-manager seam to 'unavailable' (a vanilla machine)."""
    from kiro_crew.dashboard.handlers import _shared

    mgr = MagicMock()
    mgr.available.return_value = False
    monkeypatch.setattr(_shared, "_capability_manager", lambda: mgr)
    return mgr


class TestRemove:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_remove(_request(ValueError("boom")))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_name_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_remove(_request({"name": "   "}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_removes_entry_and_syncs_removal(
        self, sandbox: SimpleNamespace, no_capability_manager: MagicMock
    ) -> None:
        _write_global(sandbox, {"srv": {"command": "x"}, "keep": {"command": "y"}})
        resp = await mcp_mod.api_mcp_remove(_request({"name": "srv"}))
        assert resp.status == 200
        assert _payload(resp) == {"ok": True, "name": "srv", "removed": True}
        assert "srv" not in _read_global(sandbox)
        assert "keep" in _read_global(sandbox)
        assert sandbox.synced == [("srv", False, True)]

    @pytest.mark.asyncio
    async def test_absent_entry_reports_removed_false(
        self, sandbox: SimpleNamespace, no_capability_manager: MagicMock
    ) -> None:
        _write_global(sandbox, {})
        resp = await mcp_mod.api_mcp_remove(_request({"name": "ghost"}))
        assert resp.status == 200
        assert _payload(resp)["removed"] is False
        # The agent-config sync still runs so stale refs cannot survive.
        assert sandbox.synced == [("ghost", False, True)]

    @pytest.mark.asyncio
    async def test_capability_manager_failure_is_best_effort(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An erroring package manager must not block the config removal."""
        from kiro_crew.dashboard.handlers import _shared

        mgr = MagicMock()
        mgr.available.return_value = True
        mgr.uninstall_mcp = AsyncMock(side_effect=RuntimeError("aim exploded"))
        monkeypatch.setattr(_shared, "_capability_manager", lambda: mgr)

        _write_global(sandbox, {"srv": {"command": "x"}})
        resp = await mcp_mod.api_mcp_remove(_request({"name": "srv"}))
        assert resp.status == 200
        assert _payload(resp)["removed"] is True
        assert "srv" not in _read_global(sandbox)

    @pytest.mark.asyncio
    async def test_capability_manager_success_is_reported_ok(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import _shared

        mgr = MagicMock()
        mgr.available.return_value = True
        mgr.uninstall_mcp = AsyncMock(
            return_value=SimpleNamespace(ok=True, message="gone")
        )
        monkeypatch.setattr(_shared, "_capability_manager", lambda: mgr)

        _write_global(sandbox, {"srv": {"command": "x"}})
        resp = await mcp_mod.api_mcp_remove(_request({"name": "srv"}))
        assert resp.status == 200
        mgr.uninstall_mcp.assert_awaited_once_with("srv")

    @pytest.mark.asyncio
    async def test_corrupt_global_config_is_tolerated(
        self, sandbox: SimpleNamespace, no_capability_manager: MagicMock
    ) -> None:
        """Removal treats an unparseable mcp.json as empty, never 500s."""
        sandbox.global_json.write_text("}}}", encoding="utf-8")
        resp = await mcp_mod.api_mcp_remove(_request({"name": "srv"}))
        assert resp.status == 200
        assert _payload(resp)["removed"] is False


# ── PUT/DELETE /api/mcp/servers/{name} ──────────────────────────────────


class TestServerDetail:
    @pytest.mark.asyncio
    async def test_blank_name_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_server_detail(
            _request({}, match_info={"name": "  "}, method="PUT")
        )
        assert resp.status == 400
        assert "server name is required" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_delete_absent_is_404(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {})
        resp = await mcp_mod.api_mcp_server_detail(
            _request(None, match_info={"name": "ghost"}, method="DELETE")
        )
        assert resp.status == 404
        assert _payload(resp)["removed"] is False

    @pytest.mark.asyncio
    async def test_delete_present_is_200(self, sandbox: SimpleNamespace) -> None:
        _write_global(sandbox, {"srv": {"command": "x"}})
        resp = await mcp_mod.api_mcp_server_detail(
            _request(None, match_info={"name": "srv"}, method="DELETE")
        )
        assert resp.status == 200
        assert _payload(resp)["removed"] is True
        assert "srv" not in _read_global(sandbox)

    @pytest.mark.asyncio
    async def test_delete_tolerates_a_corrupt_global_config(
        self, sandbox: SimpleNamespace
    ) -> None:
        """An unparseable mcp.json is treated as empty — a 404, never a 500."""
        sandbox.global_json.write_text("]]]", encoding="utf-8")
        resp = await mcp_mod.api_mcp_server_detail(
            _request(None, match_info={"name": "srv"}, method="DELETE")
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_put_invalid_json_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_server_detail(
            _request(ValueError("boom"), match_info={"name": "srv"}, method="PUT")
        )
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_put_without_command_is_400(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_server_detail(
            _request({"args": ["x"]}, match_info={"name": "srv"}, method="PUT")
        )
        assert resp.status == 400
        assert "command is required" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_put_registers_full_spec(self, sandbox: SimpleNamespace) -> None:
        resp = await mcp_mod.api_mcp_server_detail(
            _request(
                {"command": "node", "args": ["s.js"], "env": {"K": "v"}},
                match_info={"name": "srv"},
                method="PUT",
            )
        )
        assert resp.status == 200
        assert _read_global(sandbox)["srv"] == {
            "command": "node",
            "args": ["s.js"],
            "env": {"K": "v"},
        }
        assert sandbox.synced == [("srv", True, False)]

    @pytest.mark.asyncio
    async def test_put_expands_a_declared_env_path(
        self, sandbox: SimpleNamespace, monkeypatch
    ) -> None:
        """The global file is consumed by the ACP runtime (per-key env), so a
        registered PATH fragment must be emitted complete. See env.emit_env."""
        import os

        monkeypatch.setenv("PATH", "/usr/bin")
        resp = await mcp_mod.api_mcp_server_detail(
            _request(
                {"command": "node", "env": {"PATH": "/opt/shims", "K": "v"}},
                match_info={"name": "srv"},
                method="PUT",
            )
        )
        assert resp.status == 200
        written = _read_global(sandbox)["srv"]["env"]
        entries = written["PATH"].split(os.pathsep)
        assert entries[0] == "/opt/shims", "caller-authored entries stay first"
        assert "/usr/bin" in entries, "inherited PATH must survive the override"
        assert written["K"] == "v"


# ── GET /api/mcp/active ─────────────────────────────────────────────────


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``kiro_agents_dir_path()`` at a tmp agents directory."""
    import kiro_crew.agent as agent_mod

    d = tmp_path / "agents"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    return d


@pytest.fixture
def identity_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind every Kiro Crew agent name to a same-named kiro agent.

    Without this the real resolver maps an unknown name onto the ``kirocrew``
    default, so ``/api/mcp/active`` would always take the global-scope branch
    and the per-agent branch would be unreachable.
    """
    import kiro_crew.config.loader as loader

    monkeypatch.setattr(
        loader,
        "resolve_agent_bindings",
        lambda cfg, name: SimpleNamespace(kiro_agent=name),
    )


class TestActive:
    @pytest.mark.asyncio
    async def test_named_agent_lists_its_own_servers_sorted(
        self, sandbox: SimpleNamespace, agents_dir: Path, identity_bindings: None
    ) -> None:
        (agents_dir / "other.json").write_text(
            json.dumps({"name": "other", "mcpServers": {"zeta": {}, "alpha": {}}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_active(_request(query={"agent": "other"}))
        assert resp.status == 200
        assert _payload(resp) == [
            {"name": "alpha", "enabled": True},
            {"name": "zeta", "enabled": True},
        ]

    @pytest.mark.asyncio
    async def test_unknown_named_agent_is_empty_list(
        self, sandbox: SimpleNamespace, agents_dir: Path, identity_bindings: None
    ) -> None:
        resp = await mcp_mod.api_mcp_active(_request(query={"agent": "nope"}))
        assert resp.status == 200
        assert _payload(resp) == []

    @pytest.mark.asyncio
    async def test_malformed_agent_file_is_skipped(
        self, sandbox: SimpleNamespace, agents_dir: Path, identity_bindings: None
    ) -> None:
        """An unparseable agent file must not abort the scan."""
        (agents_dir / "broken.json").write_text("{oops", encoding="utf-8")
        resp = await mcp_mod.api_mcp_active(_request(query={"agent": "other"}))
        assert resp.status == 200
        assert _payload(resp) == []

    @pytest.mark.asyncio
    async def test_a_failing_binding_lookup_falls_back_to_the_raw_name(
        self, sandbox: SimpleNamespace, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken resolver must not 500 — the query name is used verbatim."""
        import kiro_crew.config.loader as loader

        monkeypatch.setattr(
            loader,
            "resolve_agent_bindings",
            lambda cfg, name: (_ for _ in ()).throw(RuntimeError("no bindings")),
        )
        (agents_dir / "other.json").write_text(
            json.dumps({"name": "other", "mcpServers": {"alpha": {}}}), encoding="utf-8"
        )
        resp = await mcp_mod.api_mcp_active(_request(query={"agent": "other"}))
        assert resp.status == 200
        assert _payload(resp) == [{"name": "alpha", "enabled": True}]

    @pytest.mark.asyncio
    async def test_default_agent_uses_the_global_scope_and_prepends_builtins(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_global(sandbox, {"on": {"command": "x"}, "off": {"disabled": True}})
        _known(monkeypatch, "on", "off")
        resp = await mcp_mod.api_mcp_active(_request(query={}))
        assert resp.status == 200
        rows = _payload(resp)
        by_name = {r["name"]: r["enabled"] for r in rows}
        assert by_name["on"] is True
        assert by_name["off"] is False
        # The managed servers are always present, ahead of the configured ones.
        for builtin in ("kirocrew-cron", "kirocrew-core", "kirocrew-computer"):
            assert by_name[builtin] is True
        assert rows[0]["name"].startswith("kirocrew-")

    @pytest.mark.asyncio
    async def test_agent_alias_resolves_to_the_global_scope(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Kiro Crew agent name bound to ``kirocrew`` reads the global scope."""
        import kiro_crew.config.loader as loader

        monkeypatch.setattr(
            loader,
            "resolve_agent_bindings",
            lambda cfg, name: SimpleNamespace(kiro_agent="kirocrew"),
        )
        _write_global(sandbox, {"on": {"command": "x"}})
        _known(monkeypatch, "on")
        resp = await mcp_mod.api_mcp_active(
            _request(query={"agent": "default"})
        )
        assert {r["name"] for r in _payload(resp)} >= {"on", "kirocrew-core"}

    @pytest.mark.asyncio
    async def test_corrupt_global_scope_falls_back_to_empty(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox.global_json.write_text("{{{", encoding="utf-8")
        _known(monkeypatch, "on")
        resp = await mcp_mod.api_mcp_active(_request(query={}))
        assert resp.status == 200
        # With no readable disabled-state, every configured row reads enabled.
        assert {r["name"]: r["enabled"] for r in _payload(resp)}["on"] is True


# ── /api/mcp/probe (POST live, GET cached) ──────────────────────────────


def _probed(name: str, **extra: Any) -> MagicMock:
    srv = MagicMock()
    srv.name = name
    srv.to_dict.return_value = {"name": name, "status": "ok", **extra}
    return srv


class TestProbe:
    @pytest.mark.asyncio
    async def test_live_probe_overlays_enabled_and_disabled_tools(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        _write_global(
            sandbox,
            {
                "off": {"disabled": True},
                "on": {"command": "x", "disabledTools": ["ReadFile"]},
            },
        )
        monkeypatch.setattr(
            disc, "probe_all", AsyncMock(return_value=[_probed("off"), _probed("on")])
        )
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)

        resp = await mcp_mod.api_mcp_probe(_request({}))
        assert resp.status == 200
        rows = {r["name"]: r for r in _payload(resp)}
        assert rows["off"]["enabled"] is False
        assert rows["on"]["enabled"] is True
        assert rows["on"]["disabledTools"] == ["ReadFile"]
        assert "disabledTools" not in rows["off"]
        # The live result becomes the handler cache.
        assert [r["name"] for r in mcp_mod._mcp_probe_cache] == ["off", "on"]
        assert mcp_mod._mcp_probe_ts > 0.0

    @pytest.mark.asyncio
    async def test_live_probe_tolerates_a_missing_global_config(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        sandbox.global_json.unlink(missing_ok=True)
        monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[_probed("on")]))
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        resp = await mcp_mod.api_mcp_probe(_request({}))
        assert _payload(resp)[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_cached_probe_returns_the_warm_cache_without_reprobing(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        cache = [{"name": "on", "status": "ok"}]
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", list(cache))
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        bg = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", bg)

        state = _State()
        resp = await mcp_mod.api_mcp_probe_cached(_request(state=state))
        assert resp.status == 200
        assert _payload(resp) == cache
        assert not state._background_tasks
        bg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_probe_arms_a_background_reprobe_when_stale(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)  # never probed
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        bg = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", bg)

        state = _State()
        resp = await mcp_mod.api_mcp_probe_cached(_request(state=state))
        assert resp.status == 200
        assert _payload(resp) == []
        assert mcp_mod._mcp_probe_in_progress is True
        assert len(state._background_tasks) == 1
        await asyncio.gather(*state._background_tasks)
        bg.assert_awaited_once()
        # The done-callback deregisters the task from the state registry.
        assert not state._background_tasks

    @pytest.mark.asyncio
    async def test_cached_probe_does_not_stack_reprobes(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An in-flight probe suppresses a second one even on a cold cache."""
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)
        bg = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", bg)

        state = _State()
        await mcp_mod.api_mcp_probe_cached(_request(state=state))
        assert not state._background_tasks
        bg.assert_not_awaited()


# ── GET /api/mcp-gateway/metrics ────────────────────────────────────────


class TestGatewayMetrics:
    @pytest.mark.asyncio
    async def test_reports_not_running_without_a_manager(self) -> None:
        resp = await mcp_mod.api_mcp_gateway_metrics(_request(state=SimpleNamespace()))
        assert resp.status == 200
        assert _payload(resp) == {"running": False, "backends": []}

    @pytest.mark.asyncio
    async def test_reports_not_running_when_the_broker_is_down(self) -> None:
        manager = SimpleNamespace(is_running=False)
        state = SimpleNamespace(_mcp_gateway_manager=manager)
        resp = await mcp_mod.api_mcp_gateway_metrics(_request(state=state))
        assert _payload(resp)["running"] is False

    @pytest.mark.asyncio
    async def test_merges_the_pool_snapshot_and_drops_the_type_tag(self) -> None:
        manager = MagicMock()
        manager.is_running = True
        manager.stats = AsyncMock(
            return_value={
                "type": "pool_stats",
                "size": 1,
                "max_backends": 4,
                "backends": [{"server": "slack-mcp", "pid": 1, "alive": True}],
            }
        )
        state = SimpleNamespace(_mcp_gateway_manager=manager)
        resp = await mcp_mod.api_mcp_gateway_metrics(_request(state=state))
        body = _payload(resp)
        assert body["running"] is True
        assert body["size"] == 1
        assert body["backends"][0]["server"] == "slack-mcp"
        assert "type" not in body  # internal envelope tag never leaks


class TestGatewayStatusPing:
    @pytest.mark.asyncio
    async def test_ping_is_reported_when_the_broker_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        manager = MagicMock()
        manager.is_running = True
        manager.ping = AsyncMock(return_value=True)
        state = SimpleNamespace(_mcp_gateway_manager=manager)
        resp = await mcp_mod.api_mcp_gateway_status(_request(state=state))
        body = _payload(resp)
        assert body["running"] is True
        assert body["ping_ok"] is True


# ── POST /api/mcp-gateway/enable — error branches ───────────────────────


class TestSharingToggleFreezesTheLegacyAlias:
    """The sharing toggle must not change WHICH servers are stubbed.

    ``_resolve_stub_servers`` is conditional on ``enabled``, so on a config still
    riding the deprecated ``poolable_servers`` the resolved set moves when
    ``enabled`` moves. Writing only ``enabled`` would let the global sharing
    switch silently rewrite the per-server stub set in both directions.
    """

    @pytest.mark.asyncio
    async def test_enabling_sharing_does_not_stub_the_alias_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ON from ``enabled:false`` must keep the EMPTY set the page showed.

        The page truthfully reports "0 stubbed" for this config, because a
        gateway that was off ran no stub. One click on *sharing* must not stub
        every alias entry and share it — that is the unrequested topology change
        this design exists to make opt-in.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"enabled": False, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        state = SimpleNamespace(
            _mcp_gateway_apply=AsyncMock(return_value={"running": True, "ping_ok": True})
        )
        resp = await mcp_mod.api_mcp_gateway_enable(_request({"enabled": True}, state=state))
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))["mcp_gateway"]
        assert saved["enabled"] is True
        assert saved["stub_servers"] == []

    @pytest.mark.asyncio
    async def test_disabling_sharing_keeps_the_servers_stubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OFF from ``enabled:true`` must leave the stub set intact.

        Otherwise the alias stops firing, the set empties, and "stubbed but
        private" — the whole middle state this PR adds — becomes unreachable for
        exactly the migrated operator. Turning sharing off narrows sharing only.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"enabled": True, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        state = SimpleNamespace(
            _mcp_gateway_apply=AsyncMock(return_value={"running": True, "ping_ok": True})
        )
        resp = await mcp_mod.api_mcp_gateway_enable(_request({"enabled": False}, state=state))
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))["mcp_gateway"]
        assert saved["enabled"] is False
        assert saved["stub_servers"] == ["x-mcp"]

    @pytest.mark.asyncio
    async def test_an_explicit_empty_set_is_never_repopulated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Key presence, not truthiness: an operator who cleared ``stub_servers``
        chose to stub nothing, and a stale ``poolable_servers`` must not revive
        the servers they just cleared."""
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "mcp_gateway": {
                        "enabled": True,
                        "stub_servers": [],
                        "poolable_servers": ["x-mcp"],
                    }
                }
            ),
            encoding="utf-8",
        )
        state = SimpleNamespace(
            _mcp_gateway_apply=AsyncMock(return_value={"running": True, "ping_ok": True})
        )
        resp = await mcp_mod.api_mcp_gateway_enable(_request({"enabled": False}, state=state))
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))["mcp_gateway"]
        assert saved["stub_servers"] == []


class TestFreezeStubServersOrdering:
    def test_freeze_resolves_against_the_pre_write_enabled_value(self) -> None:
        """Ordering guard: the helper must run BEFORE ``enabled`` is reassigned.

        Freezing afterwards would resolve against the NEW value — the exact
        inversion the two toggle-direction tests above exist to prevent — so this
        pins the helper's own contract independently of its call sites.
        """
        section = {"enabled": True, "poolable_servers": ["x-mcp"]}
        mcp_mod._freeze_stub_servers(section)
        assert section["stub_servers"] == ["x-mcp"]

        # After the freeze, `enabled` no longer speaks for the stub set at all.
        section["enabled"] = False
        mcp_mod._freeze_stub_servers(section)
        assert section["stub_servers"] == ["x-mcp"]

    def test_freeze_is_idempotent_and_sorted(self) -> None:
        section: dict = {"enabled": True, "poolable_servers": ["b-mcp", "a-mcp", "b-mcp"]}
        mcp_mod._freeze_stub_servers(section)
        first = section["stub_servers"]
        assert first == ["a-mcp", "b-mcp"]
        mcp_mod._freeze_stub_servers(section)
        assert section["stub_servers"] == first


class TestLocalOverlayOwnsTheStubKeys:
    """``config.local.json`` is deep-merged OVER ``config.json`` and is
    user-owned, so the base file is NOT the effective config.

    A read-modify-write that resolves from the base section alone gets the stub
    set wrong, and a base write is inert for any key the overlay defines. Both
    directions silently contradict what the operator sees.
    """

    @pytest.mark.asyncio
    async def test_toggling_sharing_keeps_an_overlay_owned_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy allowlist lives in the OVERLAY; the toggle's own key does not.

        Effective set before the click is ``[x-mcp]`` (resolver falls back to
        ``poolable_servers``). Freezing from the base section alone would write
        ``stub_servers: []``, and because key PRESENCE wins in the resolver that
        empty base value then beats the overlay's allowlist — silently unstubbing
        a server the operator never touched.

        ``enabled`` deliberately lives in the BASE here: that is the key this
        endpoint writes, so the write can actually land. (An overlay that owns
        ``enabled`` is the refusal case covered by the next test.)
        """
        from kiro_crew.config.loader import config_local_path, config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        base = config_path()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps({"mcp_gateway": {"enabled": True}}), encoding="utf-8")
        config_local_path().write_text(
            json.dumps({"mcp_gateway": {"poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )

        state = SimpleNamespace(
            _mcp_gateway_apply=AsyncMock(return_value={"running": True, "ping_ok": True})
        )
        resp = await mcp_mod.api_mcp_gateway_enable(_request({"enabled": False}, state=state))
        assert resp.status == 200

        saved = json.loads(base.read_text(encoding="utf-8"))["mcp_gateway"]
        assert saved.get("stub_servers") == ["x-mcp"], (
            "the overlay's allowlist was discarded: freezing must resolve from the "
            "MERGED effective section, not from config.json alone"
        )

    @pytest.mark.asyncio
    async def test_a_write_the_overlay_would_shadow_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the overlay itself defines the key we are about to write, a base
        write cannot take effect — the overlay wins the deep merge. Reporting
        success would be the same lie as a 200 with ``applied: false``."""
        from kiro_crew.config.loader import config_local_path, config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        base = config_path()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps({"mcp_gateway": {"enabled": False}}), encoding="utf-8")
        config_local_path().write_text(
            json.dumps({"mcp_gateway": {"enabled": False}}), encoding="utf-8"
        )

        state = SimpleNamespace(_mcp_gateway_apply=AsyncMock())
        resp = await mcp_mod.api_mcp_gateway_enable(_request({"enabled": True}, state=state))
        assert resp.status == 409, "an inert write must be refused, not reported as applied"
        assert "config.local.json" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_the_per_server_setter_also_freezes_from_the_merged_view(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same invariant on the other writer: stubbing one server must not drop
        an allowlist that lives in the overlay."""
        from kiro_crew.config.loader import config_local_path, config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        base = config_path()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps({"mcp_gateway": {"enabled": True}}), encoding="utf-8")
        config_local_path().write_text(
            json.dumps({"mcp_gateway": {"poolable_servers": ["x-mcp"]}}), encoding="utf-8"
        )

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "y-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(base.read_text(encoding="utf-8"))["mcp_gateway"]
        assert saved["stub_servers"] == ["x-mcp", "y-mcp"]

    @pytest.mark.asyncio
    async def test_the_per_server_setter_refuses_a_shadowed_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overlay owns ``stub_servers`` -> the row toggle cannot land either."""
        from kiro_crew.config.loader import config_local_path, config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        base = config_path()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps({"mcp_gateway": {}}), encoding="utf-8")
        config_local_path().write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["x-mcp"]}}), encoding="utf-8"
        )

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "y-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 409
        assert _payload(resp)["code"] == "overlay_owns_stub_servers"

    @pytest.mark.asyncio
    async def test_a_corrupt_overlay_is_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loader logs and ignores an unparseable overlay, so this path must
        agree with it. Letting the error escape would turn a broken user file into
        a 500 on a settings click."""
        from kiro_crew.config.loader import config_local_path, config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        base = config_path()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            json.dumps({"mcp_gateway": {"enabled": True, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        config_local_path().write_text("{ not json", encoding="utf-8")

        state = SimpleNamespace(
            _mcp_gateway_apply=AsyncMock(return_value={"running": True, "ping_ok": True})
        )
        resp = await mcp_mod.api_mcp_gateway_enable(_request({"enabled": False}, state=state))
        assert resp.status == 200
        saved = json.loads(base.read_text(encoding="utf-8"))["mcp_gateway"]
        # Base's own legacy allowlist still migrates; the corrupt overlay is ignored.
        assert saved["stub_servers"] == ["x-mcp"]


class TestGatewayEnableErrors:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_enable(
            _request(ValueError("boom"), state=SimpleNamespace())
        )
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_corrupt_config_json_is_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ nope", encoding="utf-8")
        apply_cb = AsyncMock()
        state = SimpleNamespace(_mcp_gateway_apply=apply_cb)
        resp = await mcp_mod.api_mcp_gateway_enable(
            _request({"enabled": True}, state=state)
        )
        assert resp.status == 500
        assert "corrupt" in _payload(resp)["error"]
        apply_cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_object_section_is_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcp_gateway": "on"}), encoding="utf-8")
        state = SimpleNamespace(_mcp_gateway_apply=AsyncMock())
        resp = await mcp_mod.api_mcp_gateway_enable(
            _request({"enabled": True}, state=state)
        )
        assert resp.status == 500
        assert "not an object" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_apply_failure_is_500_and_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
        state = SimpleNamespace(
            _mcp_gateway_apply=AsyncMock(side_effect=RuntimeError("broker died"))
        )
        resp = await mcp_mod.api_mcp_gateway_enable(
            _request({"enabled": True}, state=state)
        )
        assert resp.status == 500
        # The raw exception text stays server-side (in the SEL log below); the
        # verbatim-rendered client body gets a generic message + machine code.
        body = _payload(resp)
        assert "broker died" not in body["error"]
        assert body["error"] == "apply failed"
        assert body["code"] == "mcp_apply_failed"
        outcomes = [c.kwargs.get("outcome") for c in sel.log_api_access.call_args_list]
        assert "error" in outcomes


# ── GET /api/mcp-gateway/servers ────────────────────────────────────────


@pytest.fixture
def routed_allowlist(monkeypatch: pytest.MonkeyPatch):
    """Pin ``KiroCrewConfig.load().mcp_gateway.stub_servers``."""
    import kiro_crew.config.loader as loader

    def _set(names: list[str], *, enabled: bool = False, forward_declared_env: bool = False) -> None:
        # ``socket_path`` is part of the real ``McpGatewayConfig`` and the
        # handler reads it to locate the observed-hazard ledger. A double that
        # omitted it would make the row builder raise on a field production
        # always has — empty is the honest stand-in for "no broker configured".
        #
        # ``enabled`` + ``forward_declared_env`` are read for the same reason:
        # together they decide whether stubbing a server could produce a SHARED
        # backend at all, which the row reports as ``pooling_blocked_by_env``.
        # Defaults match a fresh install (sharing off, no forwarding), so the
        # existing cases keep describing the state they were written for.
        cfg = SimpleNamespace(
            mcp_gateway=SimpleNamespace(
                stub_servers=list(names),
                socket_path="",
                enabled=enabled,
                forward_declared_env=forward_declared_env,
            )
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    return _set


def _seed_probe(monkeypatch, *names: str) -> None:
    """Make ``probe_metadata`` report a probed server that declares caller identity.

    Without this the verdict stops at "never probed" for every row, and a test of
    the preflight gate would pass whether or not the gate works.
    """
    meta = SimpleNamespace(
        status="ok",
        capabilities={"experimental": {"kirocrew.caller-identity": {}}},
        protocol_version="2024-11-05",
        tool_annotations=[],
        tools=[SimpleNamespace(name="t")],
    )
    monkeypatch.setattr(mcp_mod, "probe_metadata", lambda n: meta if n in names else None)


class TestGatewayServers:
    @pytest.mark.asyncio
    async def test_the_write_resolves_eligibility_against_the_state_it_writes_under(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The sharing state the rule reads is the one the write itself sees.

        A client filtering its own rows can only answer for the moment it read
        them, and sharing is a separate switch another dashboard or the CLI can
        flip. So the client sends candidates and the decision happens here, inside
        the lock hold that performs the write: a server whose evidence supports a
        stub but not co-tenancy is written while sharing is off and skipped once it
        is on, with no guard, expectation or retry in between.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "a.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": {"a-mcp": {"command": "run"}}}),
            encoding="utf-8",
        )
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
        # Patch the LOADER's name: the handler re-imports ``config_path`` from
        # ``kiro_crew.config.loader`` inside the function body, so patching this
        # module's copy is silently ignored.
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        # Supports a stub, does not support co-tenancy -- the case where the two
        # sharing states must disagree.
        from kiro_crew.mcp_gateway.shareability import ShareVerdict, Strength

        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: ShareVerdict(
                name=name,
                strength=Strength.MEASURED,
                recommend_stub=True,
                recommend_share=False,
                reasons=(),
            ),
        )

        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": False, "stub_servers": []}}))
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": True, "resolve_eligibility": True})
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["stubbed"] == ["a-mcp"]
        assert body["skipped"] == []
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == ["a-mcp"]

        # Same request, same verdict, sharing now ON: the write must decline it and
        # say which name it declined, rather than co-tenanting on the weaker flag.
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": True, "resolve_eligibility": True})
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["stubbed"] == []
        assert body["skipped"] == [{"name": "a-mcp", "reason": "evidence_insufficient"}]
        # Nothing qualified means the file is not rewritten at all.
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == []

    @pytest.mark.asyncio
    async def test_the_overlay_decides_forward_declared_env_for_the_batch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``config.local.json`` wins, because the rewriter reads the merged value.

        Whether a declared key is forwarded decides whether a pooled spawn happens
        at all, so reading the base value while the rewriter reads the overlay would
        let the batch report a stub whose backend is left direct. The sharing switch
        beside it is already read this way.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "mcp_gateway": {
                        "enabled": True,
                        "stub_servers": [],
                        "forward_declared_env": True,
                    }
                }
            )
        )
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(
            mcp_mod, "_local_overlay_section", lambda: {"forward_declared_env": False}
        )

        seen: dict[str, bool] = {}

        def _record(names, *, sharing_on, forward_declared_env):  # type: ignore[no-untyped-def]
            seen["forward"] = forward_declared_env
            return list(names), []

        monkeypatch.setattr(mcp_mod, "_stub_eligibility", _record)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": True, "resolve_eligibility": True})
        )
        assert resp.status == 200
        assert seen["forward"] is False, (
            "the batch must use the effective value the rewriter sees, not the base"
        )

    @pytest.mark.asyncio
    async def test_unstubbing_never_asks_the_evidence_for_permission(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Removing a stub needs no verdict.

        Eligibility gates ADDING co-tenancy. Applying it to a removal would strand
        an operator with a stub they explicitly asked to drop, on the grounds that
        the evidence for keeping it had weakened.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": ["a-mcp"]}})
        )
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": False, "resolve_eligibility": True})
        )
        assert resp.status == 200
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == []

    @pytest.mark.asyncio
    async def test_a_single_toggle_writes_exactly_the_name_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Absent ``resolve_eligibility`` means "write exactly what I named".

        The per-row switch is a direct instruction about one server the operator is
        looking at, so it needs no verdict lookup; and an older dashboard served
        from a previous build sends no such field. Neither may be filtered.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        # Patch the LOADER's name: the handler re-imports ``config_path`` from
        # ``kiro_crew.config.loader`` inside the function body, so patching this
        # module's copy is silently ignored.
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(
            loader.KiroCrewConfig,
            "load",
            staticmethod(
                lambda: SimpleNamespace(
                    mcp_gateway=SimpleNamespace(
                        enabled=True, stub_servers=[], socket_path="", forward_declared_env=False
                    )
                )
            ),
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True})
        )
        assert resp.status == 200
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_the_stub_write_goes_through_the_locked_config_primitive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The stub write is only sound inside a CROSS-PROCESS lock.

        ``_MCP_GATEWAY_APPLY_LOCK`` and ``_get_config_lock`` are asyncio locks:
        they serialize this gateway's own handlers and say nothing about another
        process. The CLI writes ``mcp_gateway.enabled`` through the same file, so
        reading the sharing state outside the lock would let it change before the
        write -- and stub-only servers land in a pool that is now shared. The
        eligibility rule reads that state INSIDE this hold, which is what makes the
        decision and the write see one config.

        ``update_config_locked`` holds an advisory ``flock`` for the whole
        read-modify-write, and its own docstring names it the required path for new
        config.json mutations. This asserts the batch actually uses it rather than
        re-implementing the read-then-write it replaced; a regression to
        ``write_config_atomically`` would reopen the window silently, since every
        behavioural test still passes with the lock removed.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        seen: list[str] = []
        real = loader.update_config_locked

        def _recording_update(path=None, **kwargs):  # type: ignore[no-untyped-def]
            seen.append("locked")
            return real(path, **kwargs)

        monkeypatch.setattr(loader, "update_config_locked", _recording_update)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": True, "resolve_eligibility": False})
        )
        assert resp.status == 200
        # Empty means the handler wrote config.json some other way, which is the
        # regression: a direct ``write_config_atomically`` passes every behavioural
        # test in this file while reopening the cross-process window.
        assert seen == ["locked"]
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_a_cancelled_request_still_waits_for_the_write_to_finish(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cancellation must not unwind the locks while the worker is mid-write.

        A worker thread cannot be cancelled. With a bare ``asyncio.to_thread`` the
        ``CancelledError`` propagates immediately, both locks unwind, and the thread
        keeps mutating config.json -- so an agent-CRUD save can interleave with a
        write the caller already gave up on, which is exactly the interleaving those
        locks exist to prevent.

        The property asserted is ORDERING, not merely that the file was written:
        the write lands either way, since nothing stops the thread. What
        distinguishes the two is whether the write had FINISHED by the time
        cancellation was observable.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        entered = threading.Event()
        finished = threading.Event()
        real = loader.update_config_locked

        def _slow_update(path=None, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            # Long enough that the cancellation below lands while this is running.
            time.sleep(0.4)
            try:
                return real(path, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(loader, "update_config_locked", _slow_update)

        task = asyncio.create_task(
            mcp_mod.api_mcp_gateway_set_stub(_request({"names": ["a-mcp"], "stub": True}))
        )
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert finished.is_set(), (
            "cancellation was observable before the offloaded write completed, so "
            "both locks were released while the worker was still writing"
        )
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_the_stub_write_also_holds_the_lock_agent_crud_writes_under(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The file lock excludes other PROCESSES; it does not exclude this one.

        Agent CRUD saves config through ``cfg.save()`` ->
        ``write_config_atomically``, which takes no advisory file lock and
        serializes only on ``_get_config_lock``. So the two guards cover disjoint
        sets of writers: holding only the file lock leaves an in-process agent
        save free to interleave with this read-modify-write and silently drop one
        of the two changes.

        The offload is what makes this reachable -- ``await asyncio.to_thread``
        yields the event loop mid-write. Asserting the lock is HELD while the
        offloaded write runs is what a behavioural test cannot see: removing
        ``_get_config_lock`` leaves every stub assertion in this file passing.
        """
        import kiro_crew.config.loader as loader
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        held_during_write: list[bool] = []
        real = loader.update_config_locked
        # ``_get_config_lock`` calls ``asyncio.get_running_loop()``, so it can only
        # be resolved here on the event loop -- the recorder below runs on a
        # ``to_thread`` worker. ``Lock.locked()`` needs no loop, so capture the
        # object now and only read its state from the thread.
        lock = _get_config_lock()

        def _checking_update(path=None, **kwargs):  # type: ignore[no-untyped-def]
            held_during_write.append(lock.locked())
            return real(path, **kwargs)

        monkeypatch.setattr(loader, "update_config_locked", _checking_update)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": True, "resolve_eligibility": False})
        )
        assert resp.status == 200
        assert held_during_write == [True], (
            "the config lock agent CRUD writes under must be held across the "
            "offloaded write, not merely taken somewhere in the handler"
        )

    @pytest.mark.asyncio
    async def test_declared_env_blocks_pooling_only_while_sharing_is_on(
        self, agents_dir: Path, routed_allowlist
    ) -> None:
        """The row has to say when stubbing could not produce a SHARED backend.

        The rewriter leaves an env-declaring entry unwrapped rather than spawn a
        pooled backend without a declared key, so a bulk action that stubbed it
        would report work the broker then silently skips. With sharing OFF there
        is no pooled spawn to withhold anything, so the same server is not
        blocked — the flag describes the pooled path, not the server.
        """
        (agents_dir / "a.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "mcpServers": {
                        "declares-env": {"command": "run", "env": {"HOME_DIR": "/x"}},
                        "no-env": {"command": "run"},
                    },
                }
            ),
            encoding="utf-8",
        )

        routed_allowlist([], enabled=True)
        rows = {r["name"]: r for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]}
        assert rows["declares-env"]["pooling_blocked_by_env"] is True
        assert rows["no-env"]["pooling_blocked_by_env"] is False

        # Same config, sharing off: nothing is pooled, so nothing is withheld.
        routed_allowlist([], enabled=False)
        rows = {r["name"]: r for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]}
        assert rows["declares-env"]["pooling_blocked_by_env"] is False

        # Forwarding on: only the rotating-secret and credential classes are
        # withheld, so an ordinary declared key stops blocking.
        routed_allowlist([], enabled=True, forward_declared_env=True)
        rows = {r["name"]: r for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]}
        assert rows["declares-env"]["pooling_blocked_by_env"] is False

    @pytest.mark.asyncio
    async def test_missing_agents_dir_yields_no_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routed_allowlist
    ) -> None:
        import kiro_crew.agent as agent_mod

        routed_allowlist([])
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path / "absent")
        resp = await mcp_mod.api_mcp_gateway_servers(_request())
        assert resp.status == 200
        assert _payload(resp) == {"servers": []}

    @pytest.mark.asyncio
    async def test_dedupes_across_agents_and_computes_effective_stub_state(
        self, agents_dir: Path, routed_allowlist
    ) -> None:
        routed_allowlist(["allowed-mcp"])
        (agents_dir / "a.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "mcpServers": {
                        "allowed-mcp": {"command": "run"},
                        "opted-in": {"command": "run", "poolable": True},
                        "plain": {"command": "run"},
                        "remote": {"url": "https://example.invalid/sse"},
                        "junk": "not-a-mapping",
                    },
                }
            ),
            encoding="utf-8",
        )
        (agents_dir / "b.json").write_text(
            json.dumps({"name": "beta", "mcpServers": {"allowed-mcp": {"command": "run"}}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_servers(_request())
        rows = {r["name"]: r for r in _payload(resp)["servers"]}

        assert "junk" not in rows  # non-mapping entries are ignored
        assert rows["allowed-mcp"]["agents"] == ["alpha", "beta"]  # deduped + sorted
        assert rows["allowed-mcp"]["stub"] is True
        assert rows["allowed-mcp"]["in_allowlist"] is True
        # A spec-level opt-in is NOT sufficient any more, and the row must say so:
        # only ``stub_servers`` produces a stub, so reporting one here would claim
        # a stub the broker never created. ``entry_poolable`` is still surfaced as
        # information about the spec.
        assert rows["opted-in"]["stub"] is False
        assert rows["opted-in"]["in_allowlist"] is False
        assert rows["opted-in"]["entry_poolable"] is True
        # Neither allowlisted nor opted in.
        assert rows["plain"]["stub"] is False
        # HTTP/SSE transports are shared by nature, never pooled.
        assert rows["remote"]["transport"] == "http"
        assert rows["remote"]["stub"] is False
        assert list(rows) == sorted(rows)  # response is name-sorted

    @pytest.mark.asyncio
    async def test_denylisted_server_can_never_be_pooled(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, routed_allowlist
    ) -> None:
        from kiro_crew.mcp_gateway import rewriter

        monkeypatch.setattr(rewriter, "UNPOOLABLE_SERVERS", frozenset({"never-mcp"}))
        routed_allowlist(["never-mcp"])
        (agents_dir / "a.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "mcpServers": {"never-mcp": {"command": "run", "stub": True}},
                }
            ),
            encoding="utf-8",
        )
        rows = {
            r["name"]: r for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }
        assert rows["never-mcp"]["denylisted"] is True
        assert rows["never-mcp"]["stub"] is False

    @pytest.mark.asyncio
    async def test_two_agents_disagreeing_on_the_command_withhold_the_measurement(
        self, agents_dir: Path, routed_allowlist, monkeypatch
    ) -> None:
        """A merged row must not inherit a measurement of one of its definitions.

        Discovery merges by NAME before probing, so only the definition that wins
        the merge is ever measured. If two agents disagree about the command, the
        row covers something nobody ran — and reporting the measured one's verdict
        would tell the operator it is safe to share a backend that was never
        started.
        """
        routed_allowlist([])
        (agents_dir / "alpha.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": {"two-faced": {"command": "/bin/a"}}}),
            encoding="utf-8",
        )
        (agents_dir / "beta.json").write_text(
            json.dumps({"name": "beta", "mcpServers": {"two-faced": {"command": "/bin/b"}}}),
            encoding="utf-8",
        )
        # A clean, sharing-granting measurement exists under that name. The probe
        # metadata declares caller-identity so that consuming the measurement
        # WOULD grant sharing — without that, both branches look the same and the
        # test would pass whether or not the gate works.
        _seed_probe(monkeypatch, "two-faced")
        monkeypatch.setattr(
            mcp_mod, "_load_shareability_state", lambda: ({}, {"two-faced": (True, False)})
        )

        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }

        rec = rows["two-faced"]["recommendation"]
        assert sorted(rows["two-faced"]["agents"]) == ["alpha", "beta"]
        assert rec["recommendShare"] is False, rec
        assert "preflight_not_run" in [r["code"] for r in rec["reasons"]], rec

    @pytest.mark.asyncio
    async def test_one_definition_in_two_agents_still_gets_its_measurement(
        self, agents_dir: Path, routed_allowlist, monkeypatch
    ) -> None:
        """The ordinary case must keep working: same server, several agents.

        Withholding on agent count instead of distinct launches would silently
        drop the recommendation for most real configurations.
        """
        routed_allowlist([])
        spec = {"mcpServers": {"shared-mcp": {"command": "/bin/a", "args": ["--x"]}}}
        (agents_dir / "alpha.json").write_text(
            json.dumps({"name": "alpha", **spec}), encoding="utf-8"
        )
        (agents_dir / "beta.json").write_text(
            json.dumps({"name": "beta", **spec}), encoding="utf-8"
        )
        _seed_probe(monkeypatch, "shared-mcp")
        monkeypatch.setattr(
            mcp_mod, "_load_shareability_state", lambda: ({}, {"shared-mcp": (True, False)})
        )

        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }

        rec = rows["shared-mcp"]["recommendation"]
        assert rec["recommendShare"] is True, rec
        assert "preflight_passed" in [r["code"] for r in rec["reasons"]], rec

    @pytest.mark.asyncio
    async def test_unreadable_and_non_object_agent_files_are_skipped(
        self, agents_dir: Path, routed_allowlist
    ) -> None:
        routed_allowlist([])
        (agents_dir / "broken.json").write_text("{oops", encoding="utf-8")
        (agents_dir / "list.json").write_text("[1, 2]", encoding="utf-8")
        (agents_dir / "nomcp.json").write_text(
            json.dumps({"name": "x", "mcpServers": "wrong-type"}), encoding="utf-8"
        )
        (agents_dir / "ok.json").write_text(
            json.dumps({"mcpServers": {"good": {"command": "run"}}}), encoding="utf-8"
        )
        rows = _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        assert [r["name"] for r in rows] == ["good"]
        # No "name" key in ok.json → the file stem is used as the agent label.
        assert rows[0]["agents"] == ["ok"]


# ── POST /api/mcp-gateway/servers/stub ──────────────────────────────


class TestGatewaySetStub:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(ValueError("boom"), state=SimpleNamespace())
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"stub": True}, "name is required"),
            ({"name": "../evil", "stub": True}, "invalid server name"),
            ({"name": "ok-mcp", "stub": "yes"}, "stub must be a boolean"),
            ({"name": "ok-mcp"}, "stub must be a boolean"),
        ],
    )
    async def test_validation_matrix_is_400(
        self, monkeypatch: pytest.MonkeyPatch, body: dict, expected: str
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(body, state=SimpleNamespace())
        )
        assert resp.status == 400
        assert expected in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_corrupt_config_json_is_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ nope", encoding="utf-8")
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 500
        assert "corrupt" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_object_section_is_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcp_gateway": []}), encoding="utf-8")
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 500
        assert "not an object" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_persists_allowlist_without_an_apply_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the gateway unwired the allowlist is persisted AND reported pending.

        The write happens before the callback is ever reached, so an unwired
        gateway means the change was recorded -- the same state the callback
        reports. Answering ``applied: false`` alone would drop the client onto its
        fault branch and blame the gateway for a change that is safely saved.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body == {
            "ok": True,
            "name": "ok-mcp",
            "stub": True,
            "applied": False,
            "restart_required": True,
        }
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["ok-mcp"]

    @pytest.mark.asyncio
    async def test_an_unwired_batch_that_wrote_nothing_claims_no_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unwired answer must stay tied to something having been WRITTEN.

        A server-resolved batch where no candidate qualified persists nothing, so
        there is no pending change and nothing for a restart to pick up. Claiming
        ``restart_required`` here would send the operator to restart the gateway
        for a write that never happened.
        """
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(
            mcp_mod,
            "_collect_server_rows",
            lambda *a, **k: {},
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(
                {"names": ["nope-mcp"], "stub": True, "resolve_eligibility": True},
                state=SimpleNamespace(),
            )
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body.get("applied") is False
        assert "restart_required" not in body
        assert body.get("stubbed") == []

    @pytest.mark.asyncio
    async def test_the_first_toggle_keeps_the_migrated_legacy_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy config's effective set survives the first write.

        The runtime resolves the stub set through the deprecated
        ``poolable_servers`` when ``stub_servers`` is absent. Seeding this
        read-modify-write from the raw key instead would start from nothing, so
        enabling one server would persist only that server and silently unstub
        everything the migration was preserving.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"enabled": True, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "y-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["x-mcp", "y-mcp"]

    @pytest.mark.asyncio
    async def test_a_disabled_legacy_config_is_not_seeded_by_the_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same guard the resolver applies: a gateway that was off ran no
        stub, so its inert allowlist must not be revived by a toggle either."""
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"enabled": False, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "y-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["y-mcp"]

    @pytest.mark.asyncio
    async def test_removal_dedupes_and_drops_non_string_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"mcp_gateway": {"stub_servers": ["b-mcp", "a-mcp", "a-mcp", 7]}}
            ),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": False}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["b-mcp"]

    @pytest.mark.asyncio
    async def test_apply_result_is_merged_into_the_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        apply_cb = AsyncMock(return_value={"applied": True, "sessions_relinked": 2})
        state = SimpleNamespace(_mcp_gateway_apply_stub=apply_cb)
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=state)
        )
        assert resp.status == 200
        assert _payload(resp)["sessions_relinked"] == 2
        apply_cb.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_apply_failure_is_500_and_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        state = SimpleNamespace(
            _mcp_gateway_apply_stub=AsyncMock(side_effect=RuntimeError("relink"))
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=state)
        )
        assert resp.status == 500
        # Raw exception text stays server-side; client body is generic + coded.
        body = _payload(resp)
        assert "relink" not in body["error"]
        assert body["error"] == "apply failed"
        assert body["code"] == "mcp_apply_failed"
        # The config write happens BEFORE apply, so it survives the failure.
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["ok-mcp"]
        outcomes = [c.kwargs.get("outcome") for c in sel.log_api_access.call_args_list]
        assert "error" in outcomes


# ── the batch ("toggle all") form of the same endpoint ──────────────────


class TestGatewaySetStubBatch:
    """``{"names": [...]}`` writes the allowlist once for the whole set."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"names": "a-mcp", "stub": True}, "names must be a list of strings"),
            (
                {"names": ["a-mcp", 7], "stub": True},
                "names must be a list of strings",
            ),
            ({"names": [], "stub": True}, "names is required"),
            ({"names": ["  "], "stub": True}, "names is required"),
            ({"names": ["../evil"], "stub": True}, "invalid server name"),
            ({"names": ["a-mcp"]}, "stub must be a boolean"),
        ],
    )
    async def test_validation_matrix_is_400(
        self, monkeypatch: pytest.MonkeyPatch, body: dict, expected: str
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(body, state=SimpleNamespace())
        )
        assert resp.status == 400
        assert expected in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_oversized_batch_is_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        names = [f"srv{i}-mcp" for i in range(mcp_mod._MAX_STUB_BATCH + 1)]
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": names, "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 400
        assert "at most" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(["a-mcp"], state=SimpleNamespace())
        )
        assert resp.status == 400
        assert "object" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_adds_every_name_in_one_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["kept-mcp"]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(
                {"names": ["b-mcp", "a-mcp", "a-mcp"], "stub": True},
                state=SimpleNamespace(),
            )
        )
        assert resp.status == 200
        # The batch form answers with `names`, never a single `name`.
        assert _payload(resp) == {
            "ok": True,
            "names": ["b-mcp", "a-mcp", "a-mcp"],
            "stub": True,
            "applied": False,
            "restart_required": True,
        }
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == [
            "a-mcp",
            "b-mcp",
            "kept-mcp",
        ]

    @pytest.mark.asyncio
    async def test_removes_every_name_and_leaves_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"mcp_gateway": {"stub_servers": ["a-mcp", "b-mcp", "kept-mcp", 7]}}
            ),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(
                # A name that is not in the allowlist must not fail the batch.
                {"names": ["a-mcp", "b-mcp", "absent-mcp"], "stub": False},
                state=SimpleNamespace(),
            )
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["kept-mcp"]

    @pytest.mark.asyncio
    async def test_re_applies_the_pool_once_for_the_whole_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N names must not mean N re-links — that is the point of the form."""
        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        apply_cb = AsyncMock(return_value={"applied": True, "sessions_relinked": 3})
        state = SimpleNamespace(_mcp_gateway_apply_stub=apply_cb)
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp", "b-mcp"], "stub": True}, state=state)
        )
        assert resp.status == 200
        assert _payload(resp)["sessions_relinked"] == 3
        apply_cb.assert_awaited_once_with()
        # The audit names every server the request touched, not just the first.
        audited = [c.kwargs.get("resources") for c in sel.log_api_access.call_args_list]
        assert any("names=a-mcp,b-mcp" in (r or "") for r in audited)

    @pytest.mark.asyncio
    async def test_single_name_form_still_answers_with_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`names` absent keeps the pre-existing single-server contract."""
        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert _payload(resp) == {
            "ok": True,
            "name": "ok-mcp",
            "stub": True,
            "applied": False,
            "restart_required": True,
        }
        audited = [c.kwargs.get("resources") for c in sel.log_api_access.call_args_list]
        assert any("name=ok-mcp" in (r or "") for r in audited)


# ── the synchronous cleanup-sweep file lock ─────────────────────────────


class TestSyncFileLock:
    def test_creates_the_lock_sidecar_and_releases_it(
        self, sandbox: SimpleNamespace
    ) -> None:
        """The sweep's lock must be re-entrant across sequential acquires."""
        lock_path = mcp_mod._MCP_LOCK_PATH
        assert not lock_path.exists()
        with mcp_mod._get_mcp_lock_sync():
            assert lock_path.exists()
        # Released: a second acquire in the same process must not block.
        with mcp_mod._get_mcp_lock_sync():
            assert lock_path.exists()

    @pytest.mark.asyncio
    async def test_async_lock_shares_the_same_sidecar(
        self, sandbox: SimpleNamespace
    ) -> None:
        async with mcp_mod._get_mcp_lock():
            assert mcp_mod._MCP_LOCK_PATH.exists()
        with mcp_mod._get_mcp_lock_sync():
            pass


# ── POST /api/mcp-gateway/apps-enable ───────────────────────────────────

# ── GET/POST /api/mcp/measure ───────────────────────────────────────────


class TestMeasureProgressPayload:
    """The measurement readout's wire contract.

    ``done`` and ``measured`` are separate fields because a pass can attempt a
    server and produce no verdict: a pre-flight that could not run leaves the row
    unmeasured on purpose. The dashboard advances its progress line on ``done``
    and builds its closing claim from ``measured``, so collapsing them is what let
    a pass that measured nothing report that it measured everything it tried.
    """

    @pytest.mark.asyncio
    async def test_the_status_payload_carries_both_counts(self) -> None:
        body = _payload(await mcp_mod.api_mcp_measure_progress(_request(method="GET")))
        assert body["ok"] is True
        # Named individually: a reader of this test should see that the two counts
        # are both on the wire, which is the whole change.
        assert "done" in body, body
        assert "measured" in body, body
        assert "total" in body, body

    @pytest.mark.asyncio
    async def test_starting_a_pass_clears_the_previous_pass_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale ``measured`` would render as this pass's result.

        The progress dict outlives the pass that wrote it, and the closing line is
        built from ``measured``, so a new pass that has not measured anything yet
        would inherit the previous pass's number and close on it.
        """
        # Stand in for a finished earlier pass. Restored by monkeypatch, so this
        # cannot leak into another test through the module global.
        monkeypatch.setitem(mcp_mod._measure_progress, "running", False)
        monkeypatch.setitem(mcp_mod._measure_progress, "done", 7)
        monkeypatch.setitem(mcp_mod._measure_progress, "measured", 7)
        monkeypatch.setitem(mcp_mod._measure_progress, "total", 7)
        monkeypatch.setitem(mcp_mod._measure_progress, "error", "RuntimeError")

        # The pass itself spawns processes; this test is about the reset, so the
        # background body is replaced rather than run.
        started = asyncio.Event()

        async def _noop() -> None:
            started.set()

        monkeypatch.setattr(mcp_mod, "_bg_measure_all", _noop)

        state = _State()
        body = _payload(
            await mcp_mod.api_mcp_measure_start(_request(method="POST", state=state))
        )
        assert body["ok"] is True and body["running"] is True
        assert body["measured"] == 0, body
        assert body["done"] == 0, body
        assert body["error"] == "", body

        # Let the stubbed task run so it does not outlive the test.
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.gather(*state._background_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_second_start_while_running_is_reported_not_queued(self) -> None:
        """Refusing is what keeps a second press from doubling the spawn load."""
        import contextlib

        @contextlib.contextmanager
        def _running():
            prior = dict(mcp_mod._measure_progress)
            mcp_mod._measure_progress.update(running=True, done=1, measured=1, total=4)
            try:
                yield
            finally:
                mcp_mod._measure_progress.clear()
                mcp_mod._measure_progress.update(prior)

        with _running():
            body = _payload(
                await mcp_mod.api_mcp_measure_start(_request(method="POST"))
            )
        assert body["ok"] is False and body["running"] is True
        # The in-flight pass's own numbers, not a reset: the operator pressing a
        # second time is still watching the first pass.
        assert (body["measured"], body["done"], body["total"]) == (1, 1, 4), body


class TestStubEligibility:
    """The eligibility rule, which lives on the server rather than in the browser.

    A client can only answer for the moment it read the rows: sharing is a separate
    switch another dashboard or the CLI can flip, and verdicts move as measurements
    land. Resolving it inside the write's own lock hold is what makes the decision
    and the write see one state, so these are the tests of the rule itself.
    """

    @staticmethod
    def _spec(agents_dir: Path, servers: dict[str, dict]) -> None:
        (agents_dir / "a.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": servers}), encoding="utf-8"
        )

    @staticmethod
    def _verdict(monkeypatch: pytest.MonkeyPatch, *, stub: bool, share: bool) -> None:
        """Pin the verdict so these test the RULE, not the engine behind it."""
        from kiro_crew.mcp_gateway.shareability import ShareVerdict, Strength

        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: ShareVerdict(
                name=name,
                strength=Strength.NO_OBJECTION,
                recommend_stub=stub,
                recommend_share=share,
                reasons=(),
            ),
        )

    def test_the_sharing_state_picks_which_flag_the_rule_reads(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same server, same verdict, two answers.

        With sharing OFF a stub keeps the backend 1:1 with the session, so the
        weakest useful verdict is enough. With sharing ON the identical write hands
        that server co-tenants, which is what ``recommend_share`` means.
        """
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=False)

        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]
        assert skipped == []

        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "evidence_insufficient"}]

    def test_a_server_claiming_isolation_is_allowed_to_co_tenant(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, _ = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]

    def test_declared_env_a_shared_backend_would_withhold_skips_the_server(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rewriter leaves that entry unwrapped, so stubbing produces no pool.

        Counting it would report work the broker never does. With sharing OFF there
        is no pooled spawn to withhold anything, so the same server qualifies.
        """
        self._spec(agents_dir, {"a-mcp": {"command": "run", "env": {"HOME_DIR": "/x"}}})
        self._verdict(monkeypatch, stub=True, share=True)

        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "pooling_blocked_by_env"}]

        eligible, _ = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]

    def test_a_server_with_no_stdio_pipe_is_never_eligible(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._spec(agents_dir, {"http-mcp": {"url": "https://example.test"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, skipped = mcp_mod._stub_eligibility(
            ["http-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "http-mcp", "reason": "cannot_stub"}]

    def test_a_denylisted_server_is_never_eligible(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``UNPOOLABLE_SERVERS`` is empty today, so the branch is patched in.

        Testing it against the live set would pass by vacuum: an empty denylist
        makes the assertion unfalsifiable, and the day a name is added is exactly
        when this needs to already work.
        """
        import kiro_crew.mcp_gateway.rewriter as rewriter_mod

        monkeypatch.setattr(rewriter_mod, "UNPOOLABLE_SERVERS", frozenset({"walled-mcp"}))
        self._spec(agents_dir, {"walled-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, skipped = mcp_mod._stub_eligibility(
            ["walled-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "walled-mcp", "reason": "cannot_stub"}]

    def test_a_measurement_of_a_replaced_command_does_not_count(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cache rows are keyed by NAME, so a replaced command keeps the old row.

        That is tolerable for a rendered row, which only shows a stale verdict
        until the next probe. It is not tolerable for an automatic write: stubbing
        on it pools a program nobody measured, and with sharing on that hands one
        session's state to another. The eligibility read therefore goes through
        ``VerdictCache.get`` with the CURRENT identity, where a mismatch reads as
        no measurement.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for
        from kiro_crew.mcp_gateway.verdict_cache import CachedPreflight, load_cache

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)

        # Measure the server as it stands, storing a clean row under its identity.
        self._spec(agents_dir, {"a-mcp": {"command": "orig-server"}})
        measured = SimpleNamespace(command="orig-server", args=[], env={})
        cache = load_cache(runtime)
        cache.put(
            "a-mcp",
            identity_for(measured),
            CachedPreflight(ran=True, caller_sensitive=False, reasons=(), evaluated_at=0.0),
        )
        cache.flush()

        # First leg: with the command unchanged the measurement is honoured, so
        # the assertion below is about identity rather than about an empty cache.
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, _ = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]

        # Same name, different program. The stored row still exists and
        # ``get_by_name`` would return it; the identity-checked read must not.
        self._spec(agents_dir, {"a-mcp": {"command": "replaced-server"}})
        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: SimpleNamespace(
                recommend_stub=kw["preflight"] is not None,
                recommend_share=kw["preflight"] is not None,
            ),
        )
        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "evidence_insufficient"}]

    def test_a_recorded_hazard_survives_an_identity_change(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Hazards are read name-wide, and that asymmetry is deliberate.

        A preflight can promote a server, so it must belong to the program being
        written about. A hazard only ever demotes, so filtering it by the current
        identity would DISCARD a recorded objection -- the permissive direction --
        every time a command changed. The cautious read is the wider one.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})

        seen: dict[str, tuple[str, ...]] = {}

        def _capture(name, **kw):  # type: ignore[no-untyped-def]
            seen[name] = kw["observed_hazards"]
            return SimpleNamespace(recommend_stub=False, recommend_share=False)

        monkeypatch.setattr(
            mcp_mod, "_assess_server", _capture
        )
        monkeypatch.setattr(
            mcp_mod.hazards,
            "load_ledger",
            lambda _rt: SimpleNamespace(as_dict=lambda: {"a-mcp": ("caller_state_leak",)}),
        )

        mcp_mod._stub_eligibility(["a-mcp"], sharing_on=True, forward_declared_env=False)
        assert seen["a-mcp"] == ("caller_state_leak",)

    def test_two_agents_declaring_one_name_differently_have_no_usable_measurement(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Definitions that differ only in env are different programs to the pool.

        ``PoolKey`` includes the effective env, so these two declarations never
        share a backend with each other -- but the write is per NAME and covers
        both. A measurement of one is therefore not evidence for the other, and
        accepting it would pool a program nobody probed.

        The command and args are byte-identical here on purpose: that is the case a
        command+args launch hash cannot see, so this fails if eligibility consults
        that proxy instead of the full identity.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for
        from kiro_crew.mcp_gateway.verdict_cache import CachedPreflight, load_cache

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)

        (agents_dir / "a.json").write_text(
            json.dumps(
                {"name": "alpha", "mcpServers": {"a-mcp": {"command": "run", "env": {"K": "1"}}}}
            ),
            encoding="utf-8",
        )
        (agents_dir / "b.json").write_text(
            json.dumps(
                {"name": "beta", "mcpServers": {"a-mcp": {"command": "run", "env": {"K": "2"}}}}
            ),
            encoding="utf-8",
        )

        # A clean measurement exists for the FIRST declaration.
        cache = load_cache(runtime)
        cache.put(
            "a-mcp",
            identity_for(SimpleNamespace(command="run", args=[], env={"K": "1"})),
            CachedPreflight(ran=True, caller_sensitive=False, reasons=(), evaluated_at=0.0),
        )
        cache.flush()

        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: SimpleNamespace(
                recommend_stub=kw["preflight"] is not None,
                recommend_share=kw["preflight"] is not None,
            ),
        )
        # ``forward_declared_env`` on, so a plainly-named declared key is forwarded
        # rather than withheld. Without it the env-withheld branch answers first
        # and this would pass without exercising identity at all.
        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=True
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "evidence_insufficient"}]

    def test_a_name_no_agent_declares_is_reported_rather_than_written(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale client can name a server that has since been removed.

        Writing it would put a name in ``stub_servers`` that nothing can ever spawn,
        so it is skipped and named instead.
        """
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, skipped = mcp_mod._stub_eligibility(
            ["ghost-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "ghost-mcp", "reason": "unknown"}]

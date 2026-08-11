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
        assert "broker died" in _payload(resp)["error"]
        outcomes = [c.kwargs.get("outcome") for c in sel.log_api_access.call_args_list]
        assert "error" in outcomes


# ── GET /api/mcp-gateway/servers ────────────────────────────────────────


@pytest.fixture
def routed_allowlist(monkeypatch: pytest.MonkeyPatch):
    """Pin ``KiroCrewConfig.load().mcp_gateway.stub_servers``."""
    import kiro_crew.config.loader as loader

    def _set(names: list[str]) -> None:
        cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=list(names)))
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    return _set


class TestGatewayServers:
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
        """With the gateway unwired the allowlist is persisted, applied=False."""
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
        }
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == ["ok-mcp"]

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
        assert "relink" in _payload(resp)["error"]
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

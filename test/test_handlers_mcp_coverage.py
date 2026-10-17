"""Wire-contract tests for the MCP dashboard handlers' un-exercised branches.

Covers the request/response surface of ``mcp.py`` that the existing suites
(``test_mcp_apply``, ``test_mcp_sync_agent``, ``test_mcp_probe_treadmill``,
``test_mcp_gateway_control_plane``) leave untouched: the enable/disable
toggles (server, per-tool, all), removal, server detail, the per-agent active
list, the MCP-gateway metrics / status / enable endpoints, and the
``config.local.json`` overlay both gateway writers resolve through — with
their validation matrix, corrupt-config and write-failure branches, and every
documented status code. The probe and measurement endpoints are in
``test_handlers_mcp_coverage_probe.py``; the server-enumeration endpoint, the
set-stub validation and write matrix, the stub-eligibility rule and the
cleanup-sweep file lock are in ``test_handlers_mcp_coverage_gateway_servers.py``.

Every filesystem seam is redirected into ``tmp_path``; no network, no real
MCP server process, no capability-manager subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_mcp_coverage_helpers import agents_dir_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import (
    _effective_stubs,
    _payload,
    _read_global,
    _request,
    _write_global,
)

from conftest import host_abs
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo


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
    async def test_toggle_mutes_the_raw_slash_named_entry_the_row_stands_for(
        self, sandbox: SimpleNamespace
    ) -> None:
        """The table asks by the canonical alias; the file holds the raw key.
        The flag lands on the raw entry -- the one the rebuild mounts -- and no
        stub is written beside it. Red under ``name not in servers``: a
        ``{"disabled": true}`` stub under ``playwright-mcp`` showed the row
        Disabled while ``npm:@playwright/mcp`` kept mounting untouched."""
        _write_global(sandbox, {"npm:@playwright/mcp": {"command": "npx"}})
        resp = await mcp_mod.api_mcp_toggle(
            _request({"name": "playwright-mcp", "enabled": False})
        )
        assert resp.status == 200
        servers = _read_global(sandbox)
        assert set(servers) == {"npm:@playwright/mcp"}
        assert servers["npm:@playwright/mcp"]["disabled"] is True
        # And back on, through the same resolution.
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "playwright-mcp", "enabled": True}))
        assert resp.status == 200
        assert "disabled" not in _read_global(sandbox)["npm:@playwright/mcp"]

    @pytest.mark.asyncio
    async def test_toggle_refuses_an_ambiguous_alias_without_writing_a_stub(
        self, sandbox: SimpleNamespace
    ) -> None:
        """Two raw keys alias to the row's name and neither is exact: the flag
        has no one entry to land on, and a stub under the alias would show the
        row Disabled while both raw entries kept mounting. 409, file untouched."""
        _write_global(sandbox, {"team/foo": {"command": "a"}, "other:team/foo": {"command": "b"}})
        before = sandbox.global_json.read_bytes()
        resp = await mcp_mod.api_mcp_toggle(_request({"name": "team-foo", "enabled": False}))
        assert resp.status == 409
        assert "alias of 2 entries" in _payload(resp)["error"]
        assert _payload(resp)["code"] == "mcp_server_name_ambiguous"
        assert sandbox.global_json.read_bytes() == before

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
    async def test_a_commented_global_config_is_refused_before_the_uninstall(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removal would rewrite the file as plain JSON, so it is refused
        before the capability manager uninstalls anything."""
        from kiro_crew.dashboard.handlers import _shared

        mgr = MagicMock()
        mgr.available.return_value = True
        mgr.uninstall_mcp = AsyncMock()
        monkeypatch.setattr(_shared, "_capability_manager", lambda: mgr)
        text = '{\n  "mcpServers": {\n    "srv": {"command": "x"},\n    // parked\n  }\n}\n'
        sandbox.global_json.write_text(text, encoding="utf-8")
        with pytest.raises(mcp_mod.McpConfigHasComments):
            await mcp_mod.api_mcp_remove(_request({"name": "srv"}))
        mgr.uninstall_mcp.assert_not_awaited()
        assert sandbox.global_json.read_text(encoding="utf-8") == text

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
    @pytest.mark.parametrize(
        "name",
        ["../../evil", "..", "a/../b", "a" * 129, "-leading-dash"],
        ids=["traversal", "bare-dotdot", "embedded-dotdot", "overlong", "bad-leading-char"],
    )
    async def test_put_rejects_a_name_the_rest_of_the_tree_would_refuse(
        self, sandbox: SimpleNamespace, name: str
    ) -> None:
        """PUT is the writer, so it must not plant a key nothing else can address.

        ``_is_valid_mcp_name`` is the predicate the validation-dependent readers
        in this tree enforce (several call sites in ``mcp.py`` itself, plus
        ``mcp_custom.py``, ``mcp_discover.py`` and ``connections.py``). A name
        carrying ``..`` or exceeding ``_MAX_MCP_NAME_LEN`` written through this
        route is invisible to those, so the entry cannot be managed through them.
        """
        _write_global(sandbox, {})
        resp = await mcp_mod.api_mcp_server_detail(
            _request({"command": "node"}, match_info={"name": name}, method="PUT")
        )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_server_name"
        # The refusal must land before the write, not after it.
        assert _read_global(sandbox) == {}

    @pytest.mark.asyncio
    async def test_delete_still_removes_a_malformed_junk_key(
        self, sandbox: SimpleNamespace
    ) -> None:
        """DELETE stays permissive on purpose, so a junk key remains clearable.

        This mirrors the documented grant/revoke asymmetry in ``security.py``:
        the writer validates, the remover does not, because guarding a remover
        would strand exactly the entries the writer guard prevents. ``DELETE``
        and ``api_mcp_remove`` are both such removers. Guarding this handler's
        DELETE half is the specific regression this test forbids.
        """
        _write_global(sandbox, {"../../evil": {"command": "x"}})
        resp = await mcp_mod.api_mcp_server_detail(
            _request(None, match_info={"name": "../../evil"}, method="DELETE")
        )
        assert resp.status == 200
        assert _payload(resp)["removed"] is True
        assert _read_global(sandbox) == {}

    @pytest.mark.asyncio
    async def test_put_accepts_the_names_the_validator_allows(
        self, sandbox: SimpleNamespace
    ) -> None:
        """The guard must not narrow the accepted set beyond the shared validator.

        ``_VALID_MCP_NAME_RE`` deliberately admits ``@``, ``/``, ``.``, ``:`` and
        ``_`` so scoped package ids (``@scope/pkg``) keep working.
        """
        _write_global(sandbox, {})
        resp = await mcp_mod.api_mcp_server_detail(
            _request(
                {"command": "node"}, match_info={"name": "@scope/pkg.name_v2:1"}, method="PUT"
            )
        )
        assert resp.status == 200
        assert "@scope/pkg.name_v2:1" in _read_global(sandbox)

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
    async def test_put_refuses_a_commented_global_config(self, sandbox: SimpleNamespace) -> None:
        """A PUT would rewrite the file as plain JSON and drop its comments."""
        text = '{\n  "mcpServers": {\n    // "parked": {"command": "p"}\n  }\n}\n'
        sandbox.global_json.write_text(text, encoding="utf-8")
        with pytest.raises(mcp_mod.McpConfigHasComments) as excinfo:
            await mcp_mod.api_mcp_server_detail(
                _request({"command": "node"}, match_info={"name": "srv"}, method="PUT")
            )
        assert json.loads(excinfo.value.text)["code"] == mcp_mod.MCP_CONFIG_HAS_COMMENTS
        assert sandbox.global_json.read_text(encoding="utf-8") == text

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

        # Spelled for the host (conftest.host_abs): declared entries pass through
        # the ``os.path.isabs`` filter in env._spec_path_entries, and from Python
        # 3.13 a bare ``/opt/shims`` is not absolute under ntpath (no drive).
        shims, usr_bin = host_abs("opt", "shims"), host_abs("usr", "bin")
        monkeypatch.setenv("PATH", usr_bin)
        resp = await mcp_mod.api_mcp_server_detail(
            _request(
                {"command": "node", "env": {"PATH": shims, "K": "v"}},
                match_info={"name": "srv"},
                method="PUT",
            )
        )
        assert resp.status == 200
        written = _read_global(sandbox)["srv"]["env"]
        entries = written["PATH"].split(os.pathsep)
        assert entries[0] == shims, "caller-authored entries stay first"
        assert usr_bin in entries, "inherited PATH must survive the override"
        assert written["K"] == "v"


# ── GET /api/mcp/active ─────────────────────────────────────────────────


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
    async def test_default_agent_reads_the_store_and_the_row_flag_not_only_the_shared_file(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registry install awaiting consent is ``disabled: true`` in the Kiro
        Crew store and absent from the shared file; a server switched off in a
        provider global is in neither map. Both are off for the sessions, so
        both answer ``enabled: false`` here. Red under the shared-file-only read,
        which answered ``true`` for each."""
        import kiro_crew.mcp_discovery as disc

        _write_global(sandbox, {"on": {"command": "x"}})
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps({"mcpServers": {"pending": {"command": "y", "disabled": True}}}),
            encoding="utf-8",
        )
        # ``pending`` carries no row flag here, so only the store read can flip
        # it; ``cc-off`` is in neither map, so only the row flag can.
        rows = [
            McpServerInfo(name="on", command="/bin/true"),
            McpServerInfo(name="pending", command="/bin/true"),
            McpServerInfo(name="cc-off", command="/bin/true", disabled=True),
        ]
        monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(rows))
        resp = await mcp_mod.api_mcp_active(_request(query={}))
        assert resp.status == 200
        by_name = {r["name"]: r["enabled"] for r in _payload(resp)}
        assert by_name["on"] is True
        assert by_name["pending"] is False
        assert by_name["cc-off"] is False

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

        # After the freeze, `enabled` does not speak for the stub set at all.
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
        assert _effective_stubs(saved) == ["x-mcp", "y-mcp"]
        # The overlay's migrated roster is frozen into the base, and the click is a
        # decision on top of it -- the allowlist is preserved either way, which is
        # what this test is about.
        assert saved["stub_servers"] == ["x-mcp"]
        assert saved["stub_overrides"] == {"y-mcp": True}

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
    async def test_an_overlay_owning_the_override_map_also_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must cover the key the toggle actually writes.

        The decision lands in ``stub_overrides``, and the overlay wins the deep
        merge per-key. Checking only ``stub_servers`` would let the click write base
        ``config.json`` and answer 200 while the overlay kept the server on its own
        value -- the silent never-takes-effect failure the guard exists to prevent,
        reintroduced by relocating the write.
        """
        from kiro_crew.config.loader import config_local_path, config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        base = config_path()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["a-mcp"]}}), encoding="utf-8"
        )
        config_local_path().write_text(
            json.dumps({"mcp_gateway": {"stub_overrides": {"a-mcp": False}}}),
            encoding="utf-8",
        )

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 409, (
            "the toggle wrote a value the gateway would not read -- the overlay "
            "shadows stub_overrides, so this had to be refused"
        )
        body = _payload(resp)
        assert body["code"] == "overlay_owns_stub_servers"
        assert "stub_overrides" in body["error"]

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

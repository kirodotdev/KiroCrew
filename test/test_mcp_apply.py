
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from body_stream_helpers import attach_body

from kiro_crew.platform.interfaces import McpScope


class _NoSyncLock:
    """No-op synchronous CM standing in for the cleanup sweep's file lock, so
    tests don't touch/lock the real ~/.kiro lock path."""

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return None


def _make_request(body: dict) -> MagicMock:
    """Build a fake aiohttp request for the api_mcp_apply handler.

    Carries real body bytes: the handler reads through ``read_bounded_json``'s
    capped path (``_MCP_APPLY_MAX_BODY_BYTES``), which drains
    ``request.content`` instead of calling ``request.json()``.
    """
    state = MagicMock()
    state._background_tasks = set()
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    attach_body(request, body)
    return request


# ---------------------------------------------------------------------------
# Scope helpers: _set_kirocrew_entry, _set_scope_entry, _remove_kirocrew_entry
# ---------------------------------------------------------------------------


class TestSetKirocrewEntry:
    def test_adds_entry_when_enabling_with_spec(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry("srv", enabled=True, spec={"command": "x"})
        assert action == "added"
        assert json.loads(mc_path.read_text(encoding="utf-8"))["mcpServers"]["srv"] == {"command": "x"}

    def test_disables_existing(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        mc_path.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry("srv", enabled=False)
        assert action == "disabled"
        assert json.loads(mc_path.read_text(encoding="utf-8"))["mcpServers"]["srv"]["disabled"] is True

    def test_enabling_disabled_removes_flag(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        mc_path.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "x", "disabled": True}}})
        )
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry("srv", enabled=True)
        assert action == "enabled"
        assert "disabled" not in json.loads(mc_path.read_text(encoding="utf-8"))["mcpServers"]["srv"]

    def test_disabling_missing_with_spec_seeds_entry(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry(
            "srv", enabled=False, spec={"command": "x"}
        )
        assert action == "disabled"
        entry = json.loads(mc_path.read_text(encoding="utf-8"))["mcpServers"]["srv"]
        assert entry == {"command": "x", "disabled": True}


class TestSetScopeEntry:
    def test_adds_when_enabling_absent(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        action = mcp_mod._set_scope_entry(
            kpath, "srv", enabled=True, spec={"command": "c"}
        )
        assert action == "added"
        assert json.loads(kpath.read_text(encoding="utf-8"))["mcpServers"]["srv"] == {"command": "c"}

    def test_removes_when_disabling_present(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        kpath.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "c"}, "other": {"command": "y"}}})
        )
        action = mcp_mod._set_scope_entry(kpath, "srv", enabled=False)
        assert action == "removed"
        servers = json.loads(kpath.read_text(encoding="utf-8"))["mcpServers"]
        assert "srv" not in servers
        assert "other" in servers  # untouched

    def test_enabling_already_present_noop(self, tmp_path):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        kpath.write_text(json.dumps({"mcpServers": {"srv": {"command": "c"}}}))
        action = mcp_mod._set_scope_entry(kpath, "srv", enabled=True, spec={"command": "c"})
        assert action == "noop"

    def test_disabling_absent_noop(self, tmp_path):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        action = mcp_mod._set_scope_entry(kpath, "srv", enabled=False)
        assert action == "noop"

    def test_enabling_without_spec_missing(self, tmp_path, monkeypatch):
        """When no spec can be found anywhere, the helper returns missing_spec."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        monkeypatch.setattr(
            mcp_mod, "_find_server_spec_anywhere", lambda name: None
        )
        action = mcp_mod._set_scope_entry(kpath, "srv", enabled=True)
        assert action == "missing_spec"
        assert not kpath.exists()


# ---------------------------------------------------------------------------
# api_mcp_apply: preservation rule + batched writes
# ---------------------------------------------------------------------------


class TestApplyEndpoint:
    @pytest.mark.asyncio
    async def test_preservation_kiro_to_kirocrew(self, tmp_path, monkeypatch):
        """Turning Kiro off when server was only in Kiro copies to KiroCrew first."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        # Real files: only kiro global has slack-mcp initially.
        mc_path = tmp_path / "kirocrew.mcp.json"
        kiro_path = tmp_path / "kiro_global.json"
        cc_path = tmp_path / "cc_global.json"
        agent_path = tmp_path / "kirocrew_agent.json"

        kiro_path.write_text(
            json.dumps({"mcpServers": {"slack-mcp": {"command": "slack", "args": []}}})
        )
        agent_path.write_text(json.dumps({"mcpServers": {}}))

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [McpScope("cc", cc_path, None)])
        # Point _find_server_spec_anywhere's lookup list at our tmp paths.
        monkeypatch.setattr(
            mcp_mod,
            "_find_server_spec_anywhere",
            lambda name: (
                {"command": "slack", "args": []}
                if name == "slack-mcp"
                else None
            ),
        )
        # Stub rebuild_agent_config — we only care about file writes here.
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        # No-op the lock to simplify testing.
        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        request = _make_request(
            {
                "changes": [
                    {
                        "name": "slack-mcp",
                        "kirocrew": True,
                        "kiroGlobal": False,
                        "ccGlobal": False,
                    }
                ]
            }
        )
        resp = await mcp_mod.api_mcp_apply(request)
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["applied"] == 1

        # KiroCrew mcp.json should now have slack-mcp (preservation happened)
        mc = json.loads(mc_path.read_text(encoding="utf-8"))
        assert "slack-mcp" in mc["mcpServers"]
        assert mc["mcpServers"]["slack-mcp"].get("disabled") is not True

        # Kiro global should no longer have slack-mcp
        k = json.loads(kiro_path.read_text(encoding="utf-8"))
        assert "slack-mcp" not in k["mcpServers"]

    @pytest.mark.asyncio
    async def test_uninstall_removes_from_all_three(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        kiro_path = tmp_path / "kiro_global.json"
        cc_path = tmp_path / "cc_global.json"
        for p in (mc_path, kiro_path, cc_path):
            p.write_text(json.dumps({"mcpServers": {"foo": {"command": "f"}}}))

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [McpScope("cc", cc_path, None)])
        # Prevent the handler from shelling out to a real `aim` binary if it
        # happens to be on PATH in the test/CI environment.  The handler
        # looks up `aim` via shutil.which; returning None short-circuits
        # the subprocess.run call entirely.
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared._capability_manager", lambda: MagicMock(**{"available.return_value": False}))

        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        request = _make_request({"changes": [{"name": "foo", "uninstall": True}]})
        resp = await mcp_mod.api_mcp_apply(request)
        body = json.loads(resp.body)
        assert body["ok"] is True

        for p in (mc_path, kiro_path, cc_path):
            data = json.loads(p.read_text(encoding="utf-8"))
            assert "foo" not in data["mcpServers"], f"foo still in {p}"

    @pytest.mark.asyncio
    async def test_calls_rebuild_agent_config_once(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "mc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(
            mcp_mod, "_extra_mcp_scopes", lambda: [McpScope("cc", tmp_path / "cc.json", None)]
        )
        # The last change is an uninstall that would try to run `aim mcp
        # uninstall c` as a real subprocess if `aim` is on PATH in CI.
        # Return None from shutil.which to short-circuit that path.
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared._capability_manager", lambda: MagicMock(**{"available.return_value": False}))

        rebuild = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", rebuild)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        monkeypatch.setattr(mcp_mod, "_find_server_spec_anywhere", lambda n: {"command": "x"})

        request = _make_request(
            {
                "changes": [
                    {"name": "a", "kirocrew": True, "kiroGlobal": True, "ccGlobal": False},
                    {"name": "b", "kirocrew": True, "kiroGlobal": False, "ccGlobal": True},
                    {"name": "c", "uninstall": True},
                ]
            }
        )
        resp = await mcp_mod.api_mcp_apply(request)
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["applied"] == 3
        assert rebuild.call_count == 1  # rebuild called ONCE after all edits
        assert body["rebuild"]["ok"] is True


# ---------------------------------------------------------------------------
# Name validation: malicious / malformed server names are rejected before any
# scope mutation or subprocess call.  These lock the _is_valid_mcp_name
# contract in so a future regex change can't silently weaken it.
# ---------------------------------------------------------------------------


class TestHostileNameRejection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../../etc/passwd",       # classic path traversal
            "./local",                # leading . (not alphanumeric)
            "/abs/path",              # leading / (not alphanumeric)
            "-rf",                    # leading dash looks like an argv flag
            "a b",                    # whitespace — shouldn't smuggle into argv
            "a\nb",                   # newline injection
            "a;rm -rf /",             # command-sep chars
            "a|whoami",               # pipe
            "$(echo pwn)",            # command substitution shape
            "`echo pwn`",             # backtick command substitution
            "a\x00b",                 # NUL byte
            "",                       # empty
            "a" * 200,                # too long (> _MAX_MCP_NAME_LEN = 128)
            "foo/../bar",             # embedded .. even with alphanumerics around
            ":bad",                   # leading colon (colon is non-leading only)
        ],
    )
    async def test_rejects_hostile_names(self, tmp_path, monkeypatch, bad_name):
        """Each hostile name should short-circuit with ``error: invalid name``.

        The scope files must NOT be created/touched, and the handler must
        NOT call ``subprocess.run`` or mutate ``rebuild_agent_config``.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "mc.json"
        kiro_path = tmp_path / "kiro.json"
        cc_path = tmp_path / "cc.json"
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [McpScope("cc", cc_path, None)])
        # Trap: if the handler tries to shell out despite the name-gate, fail loudly.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared._capability_manager",
            lambda: pytest.fail("_capability_manager must not be reached for invalid name"),
        )

        rebuild = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", rebuild)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        request = _make_request(
            {"changes": [{"name": bad_name, "kirocrew": True}]}
        )
        resp = await mcp_mod.api_mcp_apply(request)
        body = json.loads(resp.body)

        assert body["ok"] is True
        assert len(body["results"]) == 1
        # Either "invalid name" (regex/len reject) or "empty name" (empty string).
        err = body["results"][0].get("error", "")
        assert err in {"invalid name", "empty name"}, (
            f"expected invalid/empty name error for {bad_name!r}, got {body['results'][0]}"
        )
        # No file was created by the scope helpers.
        assert not mc_path.exists()
        assert not kiro_path.exists()
        assert not cc_path.exists()

    @pytest.mark.asyncio
    async def test_hostile_tool_name_filtered_server_kept(self, tmp_path, monkeypatch):
        """Invalid tool-override names are dropped; the server's scope
        changes still apply, and the handler reports ``tools_rejected``.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "mc.json"
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(
            mcp_mod, "_extra_mcp_scopes", lambda: [McpScope("cc", tmp_path / "cc.json", None)]
        )
        monkeypatch.setattr(
            mcp_mod, "_find_server_spec_anywhere", lambda n: {"command": "x"}
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared._capability_manager", lambda: MagicMock(**{"available.return_value": False}))

        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        request = _make_request(
            {
                "changes": [
                    {
                        "name": "slack-mcp",
                        "kirocrew": True,
                        "toolOverrides": {
                            "../evil": False,         # rejected
                            "legit-tool": False,      # accepted
                        },
                    }
                ]
            }
        )
        resp = await mcp_mod.api_mcp_apply(request)
        body = json.loads(resp.body)

        assert body["ok"] is True
        actions = body["results"][0]["actions"]
        assert actions.get("tools_rejected") == ["../evil"]
        # The good tool went through
        assert "tools" in actions
        assert "legit-tool" in actions["tools"]


# ---------------------------------------------------------------------------
# _is_valid_mcp_name — the allowlist contract, tested directly.  App-provided
# servers are keyed ``<app>:<server>`` (enumerated from ~/.kiro/agents/*.json),
# so the charset must admit a NON-LEADING colon; the leading-char class and the
# separate ``..`` traversal check must stay intact.
# ---------------------------------------------------------------------------


class TestMcpNameValidator:
    @pytest.mark.parametrize(
        "name",
        [
            "auto-improvement:auto-improvement",  # app-provided <app>:<server> key
            "meetings:calendar-sync",             # ditto, differing halves
            "auto-improvement",                   # plain name unchanged
            "playwright-mcp",                     # plain name unchanged
            "@org/server",                        # scoped name unchanged
        ],
    )
    def test_accepts_well_formed_names(self, name):
        from kiro_crew.dashboard.handlers.mcp import _is_valid_mcp_name

        assert _is_valid_mcp_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            ":bad",          # colon may not lead
            "../evil",       # path traversal
            "a:../evil",     # colon does not smuggle traversal past the .. check
            "a;rm -rf /",    # argv-injection chars still rejected
            "",              # empty
        ],
    )
    def test_rejects_malformed_names(self, name):
        from kiro_crew.dashboard.handlers.mcp import _is_valid_mcp_name

        assert _is_valid_mcp_name(name) is False


class TestApplyAcceptsAppProvidedName:
    @pytest.mark.asyncio
    async def test_colon_name_passes_the_gate_and_applies(self, tmp_path, monkeypatch):
        """``<app>:<server>`` keys clear the name gate and the change applies.

        This is the shape ``GET /api/mcp-gateway/servers`` hands the client, so
        the sibling write endpoints must accept it back.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "mc.json"
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr(
            mcp_mod, "_find_server_spec_anywhere", lambda n: {"command": "x"}
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared._capability_manager",
            lambda: MagicMock(**{"available.return_value": False}),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        request = _make_request(
            {"changes": [{"name": "auto-improvement:auto-improvement", "kirocrew": True}]}
        )
        resp = await mcp_mod.api_mcp_apply(request)
        body = json.loads(resp.body)

        assert body["ok"] is True
        result = body["results"][0]
        assert "error" not in result, f"colon name was rejected: {result}"
        assert result["name"] == "auto-improvement:auto-improvement"
        assert "kirocrew" in result["actions"]


# ---------------------------------------------------------------------------
# api_mcp_gateway_set_stub — the endpoint the Pool toggle calls.  App-provided
# names must clear its gate; a leading colon must still 400.
# ---------------------------------------------------------------------------


def _make_stub_request(body: dict) -> MagicMock:
    """Fake aiohttp request for api_mcp_gateway_set_stub.

    ``state`` carries no ``_mcp_gateway_apply_stub`` so the handler persists the
    allowlist without attempting an in-process pool re-apply.
    """
    state = SimpleNamespace(_mcp_gateway_apply_stub=None)
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.get = MagicMock(return_value="dashboard")
    attach_body(request, body)
    return request


class TestApplyBodyCeiling:
    @pytest.mark.asyncio
    async def test_oversized_apply_body_is_413_before_decoding(self):
        """The declared-length precheck refuses an over-ceiling body outright.

        The change-count cap runs only after decoding, so the byte ceiling is
        what stops an arbitrarily large body from being buffered whole.
        """
        from kiro_crew.dashboard.handlers.mcp import (
            _MCP_APPLY_MAX_BODY_BYTES,
            _do_mcp_apply,
        )

        request = _make_request({"changes": []})
        request.content_length = _MCP_APPLY_MAX_BODY_BYTES + 1

        resp = await _do_mcp_apply(request)

        assert resp.status == 413
        assert json.loads(resp.text)["code"] == "payload_too_large"


class TestSetStubNameGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_name", [":bad", "../evil"])
    async def test_rejects_malformed_single_name(self, bad_name):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        request = _make_stub_request({"name": bad_name, "stub": True})
        resp = await mcp_mod.api_mcp_gateway_set_stub(request)
        assert resp.status == 400
        assert json.loads(resp.body)["error"] == "invalid server name"

    @pytest.mark.asyncio
    async def test_rejects_malformed_batch_name(self):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        request = _make_stub_request({"names": ["good-name", ":bad"], "stub": True})
        resp = await mcp_mod.api_mcp_gateway_set_stub(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_server_name"

    @pytest.mark.asyncio
    async def test_accepts_app_provided_colon_name(self, tmp_path, monkeypatch):
        """The stub toggle accepts ``<app>:<server>`` and persists it."""
        import kiro_crew.config.loader as loader_mod
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        cfg = tmp_path / "config.json"
        cfg.write_text("{}")
        monkeypatch.setattr(loader_mod, "config_path", lambda: cfg)
        monkeypatch.setattr(mcp_mod, "_local_overlay_section", lambda: {})

        request = _make_stub_request(
            {"name": "auto-improvement:auto-improvement", "stub": True}
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(request)
        body = json.loads(resp.body)

        assert resp.status == 200, f"colon name was rejected: {body}"
        assert body["ok"] is True
        assert body["name"] == "auto-improvement:auto-improvement"
        written = json.loads(cfg.read_text())
        assert written["mcp_gateway"]["stub_servers"] == [
            "auto-improvement:auto-improvement"
        ]


# ---------------------------------------------------------------------------
# api_mcp_global_scopes — the extra_mcp_scopes() seam surfaced to the UI
# ---------------------------------------------------------------------------


class TestGlobalScopesEndpoint:
    @pytest.mark.asyncio
    async def test_default_returns_no_extra_scopes(self, monkeypatch, tmp_path) -> None:
        """OSS default: no provider scopes → the UI shows only the core Kiro badge."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        resp = await mcp_mod.api_mcp_global_scopes(_make_request({}))
        assert resp.status == 200
        assert json.loads(resp.text) == {"scopes": []}

    @pytest.mark.asyncio
    async def test_companion_scope_is_surfaced(self, monkeypatch, tmp_path) -> None:
        """A companion scope is returned as {id: '<id>Global', label} for the badge."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(
            mcp_mod,
            "_extra_mcp_scopes",
            lambda: [McpScope("cc", tmp_path / "cc.json", None, "Claude")],
        )
        resp = await mcp_mod.api_mcp_global_scopes(_make_request({}))
        assert resp.status == 200
        assert json.loads(resp.text) == {"scopes": [{"id": "ccGlobal", "label": "Claude"}]}

    @pytest.mark.asyncio
    async def test_label_falls_back_to_id(self, monkeypatch, tmp_path) -> None:
        """An empty label falls back to the scope id."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(
            mcp_mod, "_extra_mcp_scopes", lambda: [McpScope("cc", tmp_path / "cc.json")]
        )
        resp = await mcp_mod.api_mcp_global_scopes(_make_request({}))
        assert json.loads(resp.text) == {"scopes": [{"id": "ccGlobal", "label": "cc"}]}


class TestApplyBatchCap:
    @pytest.mark.asyncio
    async def test_rejects_oversized_batch(self) -> None:
        """/api/mcp/apply caps its batch so the process-wide MCP lock is never
        held for timeout×N seconds (LIVENESS — _MCP_APPLY_MAX_CHANGES)."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        oversized = [{"name": f"srv-{i}"} for i in range(mcp_mod._MCP_APPLY_MAX_CHANGES + 1)]
        resp = await mcp_mod.api_mcp_apply(_make_request({"changes": oversized}))
        assert resp.status == 400
        assert "too many changes" in json.loads(resp.text)["error"]

    @pytest.mark.asyncio
    async def test_accepts_batch_at_cap(self, monkeypatch, tmp_path) -> None:
        """A batch exactly at the cap is not rejected by the size guard."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        at_cap = [{"name": f"srv-{i}"} for i in range(mcp_mod._MCP_APPLY_MAX_CHANGES)]
        resp = await mcp_mod.api_mcp_apply(_make_request({"changes": at_cap}))
        assert resp.status == 200


class TestBoundedCapabilityManager:
    @pytest.mark.asyncio
    async def test_mutation_op_is_time_bounded(self, monkeypatch) -> None:
        """BoundedCapabilityManager wraps mutation ops in asyncio.wait_for at the
        seam boundary, so a hanging companion op cannot stall callers or hold
        the MCP lock indefinitely (arbiter: enforce LIVENESS once, not per site)."""
        import asyncio

        from kiro_crew.platform import capability_bound

        class _Hanging:
            def available(self) -> bool:
                return True

            async def uninstall_mcp(self, server_id: str):
                await asyncio.sleep(10)  # never completes within the bound

        monkeypatch.setattr(capability_bound, "CAPABILITY_UNINSTALL_TIMEOUT", 0.05)
        wrapped = capability_bound.BoundedCapabilityManager(_Hanging())
        with pytest.raises(asyncio.TimeoutError):
            await wrapped.uninstall_mcp("x")

    @pytest.mark.asyncio
    async def test_reads_delegate_value(self, monkeypatch) -> None:
        """Read ops delegate their value through (available is sync/unwrapped;
        list ops return the inner result unchanged)."""
        from kiro_crew.platform import capability_bound

        class _Inner:
            def available(self) -> bool:
                return True

            async def list_mcp(self):
                return [{"name": "srv"}]

        wrapped = capability_bound.BoundedCapabilityManager(_Inner())
        assert wrapped.available() is True
        assert await wrapped.list_mcp() == [{"name": "srv"}]

    @pytest.mark.asyncio
    async def test_read_op_is_time_bounded(self, monkeypatch) -> None:
        """Read ops are ALSO bounded (CAPABILITY_READ_TIMEOUT): the dashboard
        polls list endpoints, so a stalled unbounded read would accumulate
        pending gateway tasks — the same wedge class mutations are bounded for
        (arbiter follow-up: symmetric liveness)."""
        import asyncio

        from kiro_crew.platform import capability_bound

        class _Hanging:
            def available(self) -> bool:
                return True

            async def list_mcp(self):
                await asyncio.sleep(10)  # never completes within the bound

        monkeypatch.setattr(capability_bound, "CAPABILITY_READ_TIMEOUT", 0.05)
        wrapped = capability_bound.BoundedCapabilityManager(_Hanging())
        with pytest.raises(asyncio.TimeoutError):
            await wrapped.list_mcp()

    @pytest.mark.asyncio
    async def test_apply_uninstall_calls_manager_off_lock(self, monkeypatch, tmp_path) -> None:
        """The /api/mcp/apply uninstall path invokes the capability manager
        (in the phase BEFORE the MCP file lock is taken) and records it."""
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(return_value=MagicMock(ok=True, message="done"))
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        resp = await mcp_mod.api_mcp_apply(
            _make_request({"changes": [{"name": "srv", "uninstall": True}]})
        )
        assert resp.status == 200
        fake.uninstall_mcp.assert_awaited_once_with("srv")
        srv = next(r for r in json.loads(resp.text)["results"] if r.get("name") == "srv")
        assert srv["actions"]["capability"] == "uninstalled"

    @pytest.mark.asyncio
    async def test_apply_uninstall_phase_timeout_marks_timed_out(
        self, monkeypatch, tmp_path
    ) -> None:
        """When the pre-lock uninstall phase exceeds its budget, unfinished
        uninstalls are stamped 'timed_out' (not silently dropped) so the
        response signals core↔companion drift."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)
        # Tiny budget so the hanging op blows the phase deadline immediately.
        from kiro_crew.platform import capability_bound

        monkeypatch.setattr(capability_bound, "CAPABILITY_UNINSTALL_TIMEOUT", 0.01)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

        async def _hang(_name):
            await _asyncio.sleep(10)

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(side_effect=_hang)
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        resp = await mcp_mod.api_mcp_apply(
            _make_request({"changes": [{"name": "srv", "uninstall": True}]})
        )
        assert resp.status == 200
        srv = next(r for r in json.loads(resp.text)["results"] if r.get("name") == "srv")
        assert srv["actions"]["capability"] == "timed_out"


class TestUninstallCrashWindowCleanup:
    """The package is removed in Phase 1 (off-lock) BEFORE its config is removed
    under the lock. If the locked loop aborts between phases, a guaranteed-cleanup
    finally must still purge the config of every CONFIRMED-removed package so
    persisted mcp.json can never dangle at a removed package (GPT 5.6 HIGH +
    arbiter item 2)."""

    @pytest.mark.asyncio
    async def test_config_purged_even_when_loop_aborts(self, monkeypatch, tmp_path) -> None:
        """A write error on an EARLIER change aborts the loop before the later
        uninstall's config removal runs — the finally sweep still purges it."""
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kiro_path = tmp_path / "kiro.json"
        # Both a normal toggle server 'a' and an uninstall target 'gone' are in
        # the global config; 'gone's package is removed in Phase 1.
        kiro_path.write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}, "gone": {"command": "y"}}})
        )
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", lambda: _NoSyncLock())

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(return_value=MagicMock(ok=True, message="done"))
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        # Make the FIRST change ('a', a scope toggle) blow up mid-loop, before
        # the uninstall change for 'gone' is processed. _set_kirocrew_entry is
        # the first mutation the toggle path calls.
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_mod, "_set_kirocrew_entry", _boom)

        request = _make_request(
            {
                "changes": [
                    {"name": "a", "kirocrew": True},           # aborts the loop
                    {"name": "gone", "uninstall": True},        # never reached by loop
                ]
            }
        )
        with pytest.raises(OSError):
            await mcp_mod.api_mcp_apply(request)

        # Despite the abort, 'gone' (package confirmed removed) must NOT remain
        # in the persisted global config — the finally sweep purged it.
        remaining = json.loads(kiro_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "gone" not in remaining, "dangling config→removed-package reference"

    @pytest.mark.asyncio
    async def test_sweep_purges_by_request_regardless_of_outcome(
        self, monkeypatch, tmp_path
    ) -> None:
        """The sweep purges by REQUEST, matching Phase 2's own unconditional config
        removal: a requested uninstall the loop didn't reach has its config swept
        even when the companion op timed out (or its result was never recorded).
        Removing config for a requested uninstall errs toward the BENIGN direction
        (config gone, package maybe orphaned) and closes the 'package gone, result
        unrecorded, config kept' cancellation window at the root (GPT 5.6 HIGH)."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod
        from kiro_crew.platform import capability_bound

        kiro_path = tmp_path / "kiro.json"
        kiro_path.write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}, "slow": {"command": "y"}}})
        )
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)
        monkeypatch.setattr(capability_bound, "CAPABILITY_UNINSTALL_TIMEOUT", 0.01)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", lambda: _NoSyncLock())

        async def _hang(_name):
            await _asyncio.sleep(10)

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(side_effect=_hang)
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        # Abort the loop before 'slow' is reached, same as above.
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_mod, "_set_kirocrew_entry", _boom)

        request = _make_request(
            {
                "changes": [
                    {"name": "a", "kirocrew": True},
                    {"name": "slow", "uninstall": True},
                ]
            }
        )
        with pytest.raises(OSError):
            await mcp_mod.api_mcp_apply(request)

        # 'slow' was a REQUESTED uninstall the loop never reached → the sweep
        # removes its config even though the companion op only timed out.
        remaining = json.loads(kiro_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "slow" not in remaining, "requested uninstall's config should be swept"

    @pytest.mark.asyncio
    async def test_config_purged_on_phase1_cancellation(self, monkeypatch, tmp_path) -> None:
        """CancelledError raised DURING Phase 1 (before the Phase-2 lock is ever
        taken — gateway shutdown / client disconnect) must still purge the config
        of every REQUESTED uninstall. The finally must wrap BOTH phases, not just
        the locked loop, and sweep by request (GPT 5.6 HIGH follow-up)."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kiro_path = tmp_path / "kiro.json"
        # Two uninstall targets: 'done' completes in Phase 1, 'pending' is still
        # running when the whole apply task is cancelled.
        kiro_path.write_text(
            json.dumps({"mcpServers": {"done": {"command": "x"}, "pending": {"command": "y"}}})
        )
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", lambda: _NoSyncLock())

        # 'done' returns immediately (confirmed uninstalled); 'pending' blocks so
        # the gather is still awaiting Phase 1 when we cancel the outer task.
        async def _uninstall(server_id):
            if server_id == "done":
                return MagicMock(ok=True, message="done")
            await _asyncio.sleep(10)  # still running at cancellation
            return MagicMock(ok=True, message="never")

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(side_effect=_uninstall)
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        request = _make_request(
            {
                "changes": [
                    {"name": "done", "uninstall": True},
                    {"name": "pending", "uninstall": True},
                ]
            }
        )

        task = _asyncio.ensure_future(mcp_mod.api_mcp_apply(request))
        # Let Phase 1 start and 'done' confirm, then cancel mid-Phase-1.
        await _asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

        remaining = json.loads(kiro_path.read_text(encoding="utf-8"))["mcpServers"]
        # Both were REQUESTED uninstalls the loop never reached → the sweep purges
        # both by request (it no longer depends on the companion result being
        # recorded, which cancellation could race). 'done's package was removed;
        # 'pending's may or may not have been — either way, removing config is the
        # user's intent and errs benign.
        assert "done" not in remaining, "requested uninstall's config must be swept"
        assert "pending" not in remaining, "requested uninstall's config must be swept"

    @pytest.mark.asyncio
    async def test_sweep_runs_off_event_loop_thread(self, monkeypatch, tmp_path) -> None:
        """The blocking sweep (its own file-lock acquire) MUST run in a worker
        thread, not on the event-loop thread — otherwise a loop-blocking acquire
        would wedge any other task holding the MCP lock and deadlock the gateway
        (GPT 5.6 HIGH: no-blocking-call-on-event-loop)."""
        import threading
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kiro_path = tmp_path / "kiro.json"
        kiro_path.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}, "gone": {}}}))
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", lambda: _NoSyncLock())

        main_thread = threading.get_ident()
        seen_threads: list[int] = []
        real_purge = mcp_mod._purge_server_config

        def _spy_purge(name):
            seen_threads.append(threading.get_ident())
            return real_purge(name)

        monkeypatch.setattr(mcp_mod, "_purge_server_config", _spy_purge)

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(return_value=MagicMock(ok=True, message="done"))
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        # Abort the loop so the sweep (not the in-loop purge) does the work.
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_mod, "_set_kirocrew_entry", _boom)

        request = _make_request(
            {"changes": [{"name": "a", "kirocrew": True}, {"name": "gone", "uninstall": True}]}
        )
        with pytest.raises(OSError):
            await mcp_mod.api_mcp_apply(request)

        # The sweep purged 'gone', and it did so on a NON-event-loop thread.
        assert seen_threads, "sweep did not run"
        assert all(t != main_thread for t in seen_threads), "sweep ran on the event-loop thread"


class TestApplyMutex:
    """Two concurrent /api/mcp/apply calls must not interleave across the
    Phase-1 (uninstall) / Phase-2 (config write) boundary — the apply mutex
    serializes the whole transaction (GPT 5.6 HIGH: concurrent-apply race)."""

    @pytest.mark.asyncio
    async def test_concurrent_applies_are_serialized(self, monkeypatch, tmp_path) -> None:
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config", lambda: None)
        # Reset the module-global apply lock so a permit a crashed earlier test
        # left held can't leak in (LoopBoundLock rebinds per loop on its own).
        monkeypatch.setattr(mcp_mod, "_apply_lock", mcp_mod.LoopBoundLock())

        class _NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
        monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", lambda: _NoSyncLock())

        # Track apply overlap: each apply's Phase-1 uninstall bumps a counter on
        # entry and asserts it never sees another apply concurrently inside.
        in_flight = 0
        max_concurrent = 0

        async def _uninstall(_name):
            nonlocal in_flight, max_concurrent
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            try:
                await _asyncio.sleep(0.05)  # widen the interleave window
                return MagicMock(ok=True, message="done")
            finally:
                in_flight -= 1

        fake = MagicMock()
        fake.available.return_value = True
        fake.uninstall_mcp = AsyncMock(side_effect=_uninstall)
        monkeypatch.setattr(_shared, "_capability_manager", lambda: fake)

        req1 = _make_request({"changes": [{"name": "s1", "uninstall": True}]})
        req2 = _make_request({"changes": [{"name": "s2", "uninstall": True}]})
        await _asyncio.gather(mcp_mod.api_mcp_apply(req1), mcp_mod.api_mcp_apply(req2))

        # The mutex must have kept the two applies from being in Phase 1 at once.
        assert max_concurrent == 1, f"applies interleaved (max_concurrent={max_concurrent})"


from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web


def _make_request(body: dict) -> MagicMock:
    """Build a fake aiohttp request for the api_mcp_apply handler."""
    state = MagicMock()
    state._background_tasks = set()
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}

    async def _json() -> dict:
        return body

    request.json = _json
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
        assert json.loads(mc_path.read_text())["mcpServers"]["srv"] == {"command": "x"}

    def test_disables_existing(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        mc_path.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry("srv", enabled=False)
        assert action == "disabled"
        assert json.loads(mc_path.read_text())["mcpServers"]["srv"]["disabled"] is True

    def test_enabling_disabled_removes_flag(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        mc_path.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "x", "disabled": True}}})
        )
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry("srv", enabled=True)
        assert action == "enabled"
        assert "disabled" not in json.loads(mc_path.read_text())["mcpServers"]["srv"]

    def test_disabling_missing_with_spec_seeds_entry(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mc_path = tmp_path / "kirocrew.mcp.json"
        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc_path)
        action = mcp_mod._set_kirocrew_entry(
            "srv", enabled=False, spec={"command": "x"}
        )
        assert action == "disabled"
        entry = json.loads(mc_path.read_text())["mcpServers"]["srv"]
        assert entry == {"command": "x", "disabled": True}


class TestSetScopeEntry:
    def test_adds_when_enabling_absent(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        action = mcp_mod._set_scope_entry(
            kpath, "srv", enabled=True, spec={"command": "c"}
        )
        assert action == "added"
        assert json.loads(kpath.read_text())["mcpServers"]["srv"] == {"command": "c"}

    def test_removes_when_disabling_present(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        kpath = tmp_path / "kiro.json"
        kpath.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "c"}, "other": {"command": "y"}}})
        )
        action = mcp_mod._set_scope_entry(kpath, "srv", enabled=False)
        assert action == "removed"
        servers = json.loads(kpath.read_text())["mcpServers"]
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
        monkeypatch.setattr(mcp_mod, "_CC_GLOBAL_JSON", cc_path)
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
        import kiro_crew.agent

        monkeypatch.setattr(kiro_crew.agent, "rebuild_agent_config", lambda: None)

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
        mc = json.loads(mc_path.read_text())
        assert "slack-mcp" in mc["mcpServers"]
        assert mc["mcpServers"]["slack-mcp"].get("disabled") is not True

        # Kiro global should no longer have slack-mcp
        k = json.loads(kiro_path.read_text())
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
        monkeypatch.setattr(mcp_mod, "_CC_GLOBAL_JSON", cc_path)
        # Prevent the handler from shelling out to a real `aim` binary if it
        # happens to be on PATH in the test/CI environment.  The handler
        # looks up `aim` via shutil.which; returning None short-circuits
        # the subprocess.run call entirely.
        monkeypatch.setattr(mcp_mod.shutil, "which", lambda _name: None)

        import kiro_crew.agent

        monkeypatch.setattr(kiro_crew.agent, "rebuild_agent_config", lambda: None)

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
            data = json.loads(p.read_text())
            assert "foo" not in data["mcpServers"], f"foo still in {p}"

    @pytest.mark.asyncio
    async def test_calls_rebuild_agent_config_once(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "mc.json")
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.json")
        monkeypatch.setattr(mcp_mod, "_CC_GLOBAL_JSON", tmp_path / "cc.json")
        # The last change is an uninstall that would try to run `aim mcp
        # uninstall c` as a real subprocess if `aim` is on PATH in CI.
        # Return None from shutil.which to short-circuit that path.
        monkeypatch.setattr(mcp_mod.shutil, "which", lambda _name: None)

        import kiro_crew.agent

        rebuild = MagicMock()
        monkeypatch.setattr(kiro_crew.agent, "rebuild_agent_config", rebuild)

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
        monkeypatch.setattr(mcp_mod, "_CC_GLOBAL_JSON", cc_path)
        # Trap: if the handler tries to shell out despite the name-gate, fail loudly.
        monkeypatch.setattr(
            mcp_mod.shutil,
            "which",
            lambda _name: pytest.fail("shutil.which must not be reached for invalid name"),
        )

        import kiro_crew.agent

        rebuild = MagicMock()
        monkeypatch.setattr(kiro_crew.agent, "rebuild_agent_config", rebuild)

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
        monkeypatch.setattr(mcp_mod, "_CC_GLOBAL_JSON", tmp_path / "cc.json")
        monkeypatch.setattr(
            mcp_mod, "_find_server_spec_anywhere", lambda n: {"command": "x"}
        )
        monkeypatch.setattr(mcp_mod.shutil, "which", lambda _n: None)

        import kiro_crew.agent

        monkeypatch.setattr(kiro_crew.agent, "rebuild_agent_config", lambda: None)

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

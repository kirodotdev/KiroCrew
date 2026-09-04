"""An isolated instance must not rewrite the operator's kiro-cli settings.

``<KIRO_HOME>/settings/mcp.json`` is where kiro-cli keeps its server registry. A
pod boots with an empty data home and runs the stale-entry purge; if that purge
resolved its path from the real ``Path.home()`` instead of honouring the
``KIRO_HOME`` the pod already sets, running a test instance would silently edit
the settings of the machine it runs on. This pins the resolution, so that
regression cannot come back.

The browsing half of this story is gone with the MCP proxy: browsing no longer
registers an MCP server at all, so there is no browse entry left to clobber.
What remains, and is tested here, is the purge's path resolution.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import kiro_crew.mcp_cleanup as cleanup_mod


def _seed_settings(kiro_home: Path) -> Path:
    """Write a settings file carrying one operator-owned server."""
    path = kiro_home / "settings" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcpServers": {"my-own-server": {"command": "somebin"}}}, indent=2),
        encoding="utf-8",
    )
    return path


class TestThePurgeResolvesItsPathFromKiroHome:
    def test_the_settings_path_follows_the_kiro_home_override(self, tmp_path, monkeypatch):
        """Resolved per call, so the override works on an ALREADY-imported module.

        This asserted the module attribute and re-imported to prove an
        import-time binding. That binding was the bug: issue #874's guard forbids
        it precisely because the autouse home-isolation fixture runs *after*
        collection has imported the module, so it could not reach a frozen
        constant -- and an unpatched call would then rewrite the operator's real
        settings file. No reload here on purpose: late resolution is the property
        under test, and needing a reload would mean it was absent.
        """
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "pod-kiro"))
        assert cleanup_mod._kiro_mcp_json() == tmp_path / "pod-kiro" / "settings" / "mcp.json"

        # A second home, same live module object: proves nothing was cached.
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "other-kiro"))
        assert cleanup_mod._kiro_mcp_json() == tmp_path / "other-kiro" / "settings" / "mcp.json"

    def test_an_explicit_override_still_wins_over_the_environment(self, tmp_path, monkeypatch):
        """The hook exists so the six existing monkeypatch call sites keep working."""
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "pod-kiro"))
        monkeypatch.setattr(cleanup_mod, "_KIRO_MCP_JSON", tmp_path / "explicit.json")
        assert cleanup_mod._kiro_mcp_json() == tmp_path / "explicit.json"

    def test_a_pod_purge_leaves_the_operators_file_alone(self, tmp_path, monkeypatch):
        """The pod's own settings file does not exist, so nothing is rewritten."""
        operator_home = tmp_path / "real-home" / ".kiro"
        operator_file = _seed_settings(operator_home)
        # Give the operator's file an entry the purge WOULD remove by name, so a
        # pass would be visible if the purge reached the wrong file.
        data = json.loads(operator_file.read_text(encoding="utf-8"))
        data["mcpServers"]["kirocrew-cron"] = {"command": "kirocrew"}
        operator_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        before = operator_file.read_text(encoding="utf-8")

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "real-home")
        monkeypatch.setattr(cleanup_mod, "_KIRO_MCP_JSON", tmp_path / "pod-kiro" / "mcp.json")

        cleanup_mod.clean_stale_managed_mcp()

        assert operator_file.read_text(encoding="utf-8") == before

    def test_a_normal_install_still_purges(self, tmp_path, monkeypatch):
        """The isolation must not disarm the real path it protects."""
        path = _seed_settings(tmp_path / ".kiro")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["kirocrew-cron"] = {"command": "kirocrew"}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        monkeypatch.setattr(cleanup_mod, "_KIRO_MCP_JSON", path)

        assert cleanup_mod.clean_stale_managed_mcp() == ["kirocrew-cron"]


@pytest.fixture(autouse=True)
def _restore_module():
    """A reload in one test must not leak a rebound module into the next."""
    yield
    importlib.reload(cleanup_mod)


class TestTheDeletedProxyEntryIsSweptOnUpgrade:
    """An upgrade must not leave an entry that spawns a command we deleted.

    Before this sweep, an operator who had Browser Mode ON kept a
    `playwright-mcp` entry whose command was `kirocrew mcp-playwright-proxy`.
    That subcommand is gone, so kiro-cli hit `ModuleNotFoundError` on EVERY
    session while browsing silently vanished -- the exact defect class this
    migration set out to retire.
    """

    def test_an_entry_launching_the_deleted_proxy_is_purged(self, tmp_path, monkeypatch):
        path = tmp_path / "settings" / "mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright-mcp": {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy", "--extension"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(cleanup_mod, "_KIRO_MCP_JSON", path)

        assert cleanup_mod.clean_stale_managed_mcp() == ["playwright-mcp"]
        assert json.loads(path.read_text())["mcpServers"] == {}

    def test_an_operators_own_playwright_server_is_left_alone(self, tmp_path, monkeypatch):
        """Matched on argv, never on the name -- `playwright-mcp` is also what an
        operator's OWN server is called, and purging by name would delete it."""
        path = tmp_path / "settings" / "mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        own = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
        path.write_text(json.dumps({"mcpServers": {"playwright-mcp": own}}), encoding="utf-8")
        monkeypatch.setattr(cleanup_mod, "_KIRO_MCP_JSON", path)

        assert cleanup_mod.clean_stale_managed_mcp() == []
        assert json.loads(path.read_text())["mcpServers"]["playwright-mcp"] == own

"""``${APP_DIR}`` in a stdio ``mcpServers`` entry: the app's own install directory.

An app installed from a registry or a local directory cannot name a script it ships by an
absolute path in its static manifest, and a relative path would resolve against the
session's working directory when kiro-cli spawns the server. Both writers of the agent
config -- registration (``bridges_mcp._register_mcp_servers``) and the rebuild
(``mcp_sources._collect_app_mcp_servers``) -- resolve the placeholder with one rule
(``bridges_mcp.substitute_app_dir``) and refuse an entry that would run anything the app
did not ship.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.apps.bridges import _deregister_mcp_servers, _register_mcp_servers
from kiro_crew.apps.bridges_mcp import APP_DIR_PLACEHOLDER, substitute_app_dir
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

SCRIPT = "server/mcp entry.mjs"  # a space, on purpose: each value stays one argv element


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "apps dir" / "my-app"
    (root / "server").mkdir(parents=True)
    (root / SCRIPT).write_text("// server\n", encoding="utf-8")
    return root


# -- the rule ----------------------------------------------------------------------------


def test_the_placeholder_becomes_the_app_root(tmp_path):
    root = _root(tmp_path)
    out, why = substitute_app_dir(
        {"command": "node", "args": [f"{APP_DIR_PLACEHOLDER}/{SCRIPT}", "--stdio"]}, root
    )
    assert why == ""
    assert out == {"command": "node", "args": [str(root / SCRIPT), "--stdio"]}


def test_the_command_itself_may_be_an_app_file(tmp_path):
    root = _root(tmp_path)
    out, _ = substitute_app_dir({"command": f"{APP_DIR_PLACEHOLDER}/{SCRIPT}"}, root)
    assert out["command"] == str(root / SCRIPT)


@pytest.mark.parametrize(
    "spec",
    [
        {"command": "node"},
        {"command": "node", "args": ["--version"]},
        {"url": f"http://127.0.0.1:9000/{APP_DIR_PLACEHOLDER}"},
    ],
)
def test_an_entry_without_the_placeholder_or_with_a_url_is_unchanged(tmp_path, spec):
    out, why = substitute_app_dir(dict(spec), _root(tmp_path))
    assert out == spec and why == ""


@pytest.mark.parametrize(
    "value, reason",
    [
        (f"{APP_DIR_PLACEHOLDER}/missing.mjs", "not found"),
        (f"{APP_DIR_PLACEHOLDER}/../escape.mjs", "escapes app root"),
        (f"{APP_DIR_PLACEHOLDER}/server", "not found"),  # a directory is not a script
        (f"--script={APP_DIR_PLACEHOLDER}/{SCRIPT}", "must start a value"),
        (f"{APP_DIR_PLACEHOLDER}/{SCRIPT}{APP_DIR_PLACEHOLDER}", "must start a value"),
        (APP_DIR_PLACEHOLDER, "must start a value"),
        (f"{APP_DIR_PLACEHOLDER}/", "not found"),
    ],
)
def test_anything_else_naming_the_placeholder_refuses_the_entry(tmp_path, value, reason):
    root = _root(tmp_path)
    (tmp_path / "apps dir" / "escape.mjs").write_text("// outside\n", encoding="utf-8")
    out, why = substitute_app_dir({"command": "node", "args": [value]}, root)
    assert out is None and reason in why


def test_a_link_out_of_the_app_root_is_refused(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "elsewhere.mjs"
    outside.write_text("// not shipped\n", encoding="utf-8")
    os.symlink(outside, root / "server" / "linked.mjs")
    out, why = substitute_app_dir(
        {"command": "node", "args": [f"{APP_DIR_PLACEHOLDER}/server/linked.mjs"]}, root
    )
    assert out is None and "escapes app root" in why


# -- registration, update, uninstall -----------------------------------------------------


def _install(tmp_path: Path, app_env, args: list[str]) -> AppManifest:
    src = _make_app_source(tmp_path, mcpServers={"files": {"command": "node", "args": args}})
    (src / "server").mkdir()
    (src / "server" / "mcp.mjs").write_text("// v1\n", encoding="utf-8")
    install_app(src)
    return AppManifest.from_json_file(app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME)


def _mcp(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def test_registration_writes_the_resolved_path_and_keeps_foreign_entries(tmp_path, app_env):
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"kiro-space-mcp": {"command": "kiro-space-mcp"}}}),
        encoding="utf-8",
    )
    manifest = _install(tmp_path, app_env, [f"{APP_DIR_PLACEHOLDER}/server/mcp.mjs"])
    assert _register_mcp_servers("test-app", manifest) == ["test-app:files"]
    servers = _mcp(tmp_path)
    app_root = app_env["home"] / "apps" / "test-app"
    assert servers["test-app:files"]["args"] == [str(app_root / "server" / "mcp.mjs")]
    assert servers["kiro-space-mcp"] == {"command": "kiro-space-mcp"}  # foreign: untouched
    # Restart (a second registration) is idempotent.
    assert _register_mcp_servers("test-app", manifest) == ["test-app:files"]
    assert _mcp(tmp_path)["test-app:files"]["args"] == [str(app_root / "server" / "mcp.mjs")]


def test_a_refused_entry_is_not_written_and_a_stale_one_is_scrubbed(tmp_path, app_env):
    manifest = _install(tmp_path, app_env, [f"{APP_DIR_PLACEHOLDER}/server/mcp.mjs"])
    _register_mcp_servers("test-app", manifest)
    assert "test-app:files" in _mcp(tmp_path)
    # An update that ships no script any more: the old entry must not keep pointing at it.
    (app_env["home"] / "apps" / "test-app" / "server" / "mcp.mjs").unlink()
    assert _register_mcp_servers("test-app", manifest) == []
    assert "test-app:files" not in _mcp(tmp_path)


def test_uninstall_removes_only_the_apps_entries(tmp_path, app_env):
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"mine": {"command": "my-tool"}}}), encoding="utf-8"
    )
    manifest = _install(tmp_path, app_env, [f"{APP_DIR_PLACEHOLDER}/server/mcp.mjs"])
    _register_mcp_servers("test-app", manifest)
    assert _deregister_mcp_servers("test-app") == 1
    assert _mcp(tmp_path) == {"mine": {"command": "my-tool"}}


# -- the rebuild (the other writer) ------------------------------------------------------


def test_the_rebuild_resolves_the_placeholder_with_the_same_rule(tmp_path, app_env, monkeypatch):
    import kiro_crew.apps.bridges as bridges
    import kiro_crew.apps.manager as manager
    from kiro_crew.agent_materialization import mcp_sources

    manifest = _install(tmp_path, app_env, [f"{APP_DIR_PLACEHOLDER}/server/mcp.mjs"])
    monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "test-app"}])
    monkeypatch.setattr(manager, "is_app_enabled", lambda _n: True)
    monkeypatch.setattr(manager, "get_app_manifest", lambda _n: manifest)
    monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})  # no live entry
    servers = mcp_sources._collect_app_mcp_servers(audit=False)
    app_root = app_env["home"] / "apps" / "test-app"
    assert servers["test-app:files"]["args"] == [str(app_root / "server" / "mcp.mjs")]

    # And it refuses what registration refuses, rather than writing the raw placeholder.
    bad = SimpleNamespace(
        mcpServers={"files": {"command": "node", "args": [f"{APP_DIR_PLACEHOLDER}/../x.mjs"]}},
        backend=manifest.backend,
    )
    monkeypatch.setattr(manager, "get_app_manifest", lambda _n: bad)
    assert "test-app:files" not in mcp_sources._collect_app_mcp_servers(audit=False)

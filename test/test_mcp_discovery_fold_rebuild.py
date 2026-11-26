"""The folded ``playwright-mcp`` dashboard row matches what the rebuild mounts.

``list_servers`` folds a raw slash key (``npm:@playwright/mcp``) into its alias
row. Each case below runs a real non-clean rebuild twice, then lists the servers
against the files that rebuild read and wrote. The row's ``disabled_tools`` must
equal the ``disabledTools`` on the mounted ``playwright-mcp`` entry, because the
row is what the user toggles to change that mount.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)
from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

from kiro_crew.mcp_discovery import SCOPE_KIRO_GLOBAL, SCOPE_KIROCREW, list_servers

RAW = "npm:@playwright/mcp"
ALIAS = "playwright-mcp"
GUARD = ["browser_install"]
#: A bare launcher no directory but the test's own provides. Windows finds a bare name only
#: through its PATHEXT extensions, so there the file carries one.
LAUNCHER = "pw-fold-npx" + (".exe" if sys.platform == "win32" else "")
_REAL_WHICH = shutil.which


def _which_launcher(cmd: str, **kwargs):
    """A real PATH lookup for :data:`LAUNCHER`; any other command as the helper's default."""
    return _REAL_WHICH(cmd, **kwargs) if cmd == LAUNCHER else cmd


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _listed_disabled_tools(tmp_path: Path, monkeypatch) -> list[str]:
    """The ``playwright-mcp`` row's ``disabled_tools``, listed against the rebuild's files."""
    project = tmp_path / "project"
    (project / "agents").mkdir(parents=True)
    (project / "agents" / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(project))
    store = tmp_path / "kirocrew_home" / "mcp.json"
    kiro_global = tmp_path / "fake_kiro_mcp.json"
    sources = ((store, SCOPE_KIROCREW), (kiro_global, SCOPE_KIRO_GLOBAL))
    monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_SOURCES", sources)
    monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, kiro_global))
    monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_discovery.kiro_agents_dir", lambda: tmp_path / "kiro_agents")

    rows = [s for s in list_servers() if s.name == ALIAS]

    assert len(rows) == 1
    return list(rows[0].disabled_tools)


def _cases(command: str) -> dict[str, tuple[dict, dict | None, list[str]]]:
    """Case id to ``(kiro_servers, kirocrew_servers, mounted disabledTools)``."""
    plain = {"command": command}
    guarded = {"command": command, "disabledTools": GUARD}
    return {
        "global-raw-agent-alias": ({RAW: guarded}, None, GUARD),
        # The store's alias is the canonical mount; the raw key mounts as ``-2``.
        "global-raw-store-alias": ({RAW: guarded}, {ALIAS: plain}, []),
        "store-raw-only": ({}, {RAW: guarded}, GUARD),
        "store-raw-unlisted-global-raw-listed": ({RAW: guarded}, {RAW: plain}, GUARD),
        "store-holds-both-keys": ({}, {RAW: guarded, ALIAS: plain}, []),
        # A Kiro global alias is a source declaration, so it keeps its own list.
        "global-alias-guarded-store-raw-plain": ({ALIAS: guarded}, {RAW: plain}, GUARD),
        "global-alias-differs-store-raw-guarded": (
            {ALIAS: {"command": command, "args": ["--other"]}},
            {RAW: guarded},
            [],
        ),
    }


@pytest.mark.parametrize(
    "case",
    [
        "global-raw-agent-alias",
        "global-raw-store-alias",
        "store-raw-only",
        "store-raw-unlisted-global-raw-listed",
        "store-holds-both-keys",
        "global-alias-guarded-store-raw-plain",
        "global-alias-differs-store-raw-guarded",
    ],
)
def test_folded_row_reports_mounted_disabled_tools(tmp_path: Path, monkeypatch, case) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    command = tmp_path / "bin" / "playwright-mcp"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    kiro_servers, kirocrew_servers, expected = _cases(str(command))[case]
    config_dir = bundled_defaults(tmp_path)
    kwargs = {"cc_servers": {}, "kiro_servers": kiro_servers, "kirocrew_servers": kirocrew_servers}

    run_install_mcp_merge(tmp_path, config_dir, **kwargs)
    rebuilt = run_install_mcp_merge(tmp_path, config_dir, **kwargs)
    mounted = rebuilt["mcpServers"][ALIAS].get("disabledTools", [])
    assert mounted == expected

    assert _listed_disabled_tools(tmp_path, monkeypatch) == mounted


@pytest.mark.parametrize(
    ("field", "declared"),
    [("args", ["${HOME}/pw.js"]), ("env", {"PW_HOME": "${HOME}/pw"})],
    ids=["args", "env"],
)
def test_folded_row_reports_mounted_disabled_tools_when_the_rebuild_expands_the_spec(
    tmp_path: Path, monkeypatch, field, declared
) -> None:
    """The rebuild writes a home reference expanded; the row compares the raw spec as emitted.

    The raw key holds ``${HOME}`` as written and the mounted alias holds the home path. A
    comparison that expands only the command reads them as two servers, and the row would
    report no disabled tools while the mount has the raw key's list.
    """
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    command = _executable(tmp_path / "bin" / "playwright-mcp")
    config_dir = bundled_defaults(tmp_path)
    guarded = {RAW: {"command": str(command), field: declared, "disabledTools": GUARD}}

    run_install_mcp_merge(tmp_path, config_dir, cc_servers={}, kiro_servers=guarded)
    rebuilt = run_install_mcp_merge(tmp_path, config_dir, cc_servers={}, kiro_servers=guarded)
    mounted = rebuilt["mcpServers"][ALIAS]
    assert mounted[field] != declared
    assert mounted["disabledTools"] == GUARD

    assert _listed_disabled_tools(tmp_path, monkeypatch) == GUARD


def test_folded_row_follows_the_rebuild_when_path_moves_the_raw_command(
    tmp_path: Path, monkeypatch
) -> None:
    """A rebuild pinned the alias to ``v18/bin``; PATH now resolves the raw command in ``v20/bin``.

    The rebuild compares the resolved commands, so the alias keeps its own list and the
    raw key mounts as ``playwright-mcp-2``. The pinned file still exists, so a basename
    match alone would read the two as one server and show the raw key's list.
    """
    old = _executable(tmp_path / "v18" / "bin" / LAUNCHER)
    new = _executable(tmp_path / "v20" / "bin" / LAUNCHER)
    config_dir = bundled_defaults(tmp_path)

    monkeypatch.setenv("PATH", str(old.parent))
    plain = {RAW: {"command": LAUNCHER}}
    first = run_install_mcp_merge(
        tmp_path, config_dir, cc_servers={}, kiro_servers=plain, which_side_effect=_which_launcher
    )
    pinned = first["mcpServers"][ALIAS]["command"]
    assert Path(pinned).samefile(old)

    monkeypatch.setenv("PATH", str(new.parent))
    guarded = {RAW: {"command": LAUNCHER, "disabledTools": GUARD}}
    rebuilt = run_install_mcp_merge(
        tmp_path, config_dir, cc_servers={}, kiro_servers=guarded, which_side_effect=_which_launcher
    )
    assert rebuilt["mcpServers"][ALIAS]["command"] == pinned
    sibling = rebuilt["mcpServers"][f"{ALIAS}-2"]
    assert Path(sibling["command"]).samefile(new)
    assert sibling["disabledTools"] == GUARD
    mounted = rebuilt["mcpServers"][ALIAS].get("disabledTools", [])
    assert mounted == []

    assert _listed_disabled_tools(tmp_path, monkeypatch) == mounted

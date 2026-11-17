"""The loader's verdict on the official conformance suite's fixtures.

These are the decided behaviours for the load stage, pinned against the suite
itself rather than against our own reading of the spec. Each expectation names
the suite's case ID. The suite's run-time checks (launching servers, reading
skills through an agent) are out of reach here and belong to later phases.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from agent_plugins_helpers import symlink

from kiro_crew.agent_plugins import discover_plugins

FIXTURES = Path(__file__).parent / "fixtures" / "agent_plugins_conformance"
CORE = "agent-plugins-conformance-core"
RECOVERY = "agent-plugins-conformance-recovery"
INVALID_MCP = "agent-plugins-conformance-invalid-mcp"
_STORED_SKILL = "SKILL.fixture.md"

#: Files the suite bundles that its mcp.json commands point at.
_BUNDLED = {
    CORE: ["bin/posix/probe token.sh", "bin/windows/probe token.cmd", "dist/probe.mjs"],
    RECOVERY: ["dist/probe.mjs"],
}
#: (link, target) pairs exactly as the Recovery fixture ships them.
_RECOVERY_LINKS = [
    ("escape-command-intermediate-posix", "/usr/bin/env"),
    ("escape-command-posix", "escape-command-intermediate-posix"),
    ("escape-command-intermediate-windows.exe", "C:/Windows/System32/cmd.exe"),
    ("escape-command-windows.exe", "escape-command-intermediate-windows.exe"),
]


def _discover(tmp_path, *, command_links):
    home = tmp_path / "powers"
    installed = home / "installed"
    names = [CORE, RECOVERY, INVALID_MCP]
    for name in names:
        shutil.copytree(FIXTURES / name, installed / name)
        # Stored as SKILL.fixture.md so repository-wide SKILL.md scans do not
        # count these as shipped skills; restored to the real name here.
        for stored in (installed / name).rglob(_STORED_SKILL):
            stored.rename(stored.with_name("SKILL.md"))
        for rel in _BUNDLED.get(name, []):
            path = installed / name / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    (installed / CORE / "probe-workdir").mkdir()
    recovery = installed / RECOVERY
    if command_links:
        for link, target in _RECOVERY_LINKS:
            symlink(recovery / link, target)
    symlink(recovery / "escape-link", "..", directory=True)
    (home / "installed.json").write_text(
        json.dumps({"installedPowers": [{"name": n} for n in names]}), encoding="utf-8"
    )
    return {r.name: r for r in discover_plugins(home)}


@pytest.fixture
def records(tmp_path):
    """The three plugins, without the Recovery fixture's four command file links."""
    return _discover(tmp_path, command_links=False)


@pytest.fixture
def recovery_records(tmp_path):
    """The three plugins with every link the Recovery fixture ships.

    Its four command links are file symlinks, so the tests that use this fixture
    are listed in ``test/requires-real-symlinks.txt``.
    """
    return _discover(tmp_path, command_links=True)


def test_core_loads_every_skill_and_server(records):
    core = records[CORE]
    assert core.state == "off" and core.issues == ()
    # skills.discovery: immediate children only, so references/conformance-nested
    # is not a skill.
    assert [s.name for s in core.skills] == ["conformance-alpha", "conformance-beta"]
    assert sorted(s.name for s in core.servers) == sorted(
        [
            "default",
            "relative",
            "root",
            "http",
            "data",
            "command-token-posix",
            "command-token-windows",
            "sse",
            "http-redirect",
            "sse-header-precedence",
            "sse-redirect",
            "sse-endpoint-origin",
        ]
    )


def test_invalid_mcp_keeps_its_skill(records):
    rec = records[INVALID_MCP]
    assert rec.state == "off" and rec.servers == ()
    assert [s.name for s in rec.skills] == ["conformance-invalid-mcp-valid"]
    assert [i.component for i in rec.issues] == ["mcp"]


def test_recovery_keeps_only_the_valid_skill_and_server(recovery_records):
    rec = recovery_records[RECOVERY]
    assert rec.state == "off"
    # skills.recovery.valid-skill-available
    assert [s.name for s in rec.skills] == ["conformance-recovery-valid"]
    # mcp.stdio.recovery.valid-server-available, and every exclusion case
    assert [s.name for s in rec.servers] == ["recovery-valid"]


def test_recovery_reports_every_excluded_entry(recovery_records):
    rec = recovery_records[RECOVERY]
    declared = json.loads((FIXTURES / RECOVERY / "mcp.json").read_text())["mcpServers"]
    skipped = {i.entry for i in rec.issues if i.component == "mcp"}
    assert skipped == set(declared) - {"recovery-valid"}
    assert {i.message for i in rec.issues if i.component == "manifest"} == {
        "unknown field ignored: conformanceUnknown",
        "extensions is not an object, ignored",
    }
    assert {i.entry for i in rec.issues if i.component == "skills"} == {
        "conformance-recovery-invalid"
    }

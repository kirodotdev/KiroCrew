"""Agent Plugins ``mcp.json`` rules (spec 1.0.0 §7.2, §9.2).

Each invalid case mirrors one row of the official conformance suite's Recovery
fixture: the entry is skipped, and its siblings still load.
"""

from __future__ import annotations

import builtins
import errno
import json
import os

import pytest
from agent_plugins_helpers import MCP_SCHEMA, install, mcp, symlink

from kiro_crew.agent_plugins import RemoteServer, StdioServer, discover_plugins
from kiro_crew.agent_plugins import mcp_config as mcp_config_module
from kiro_crew.agent_plugins.model import Issue

VALID = {"type": "stdio", "command": "node", "args": ["${PLUGIN_ROOT}/x.mjs"]}


def _load(tmp_path, servers=None, *, text=None):
    install(tmp_path, "p", **{"mcp.json": text if text is not None else mcp(servers)})
    (rec,) = discover_plugins(tmp_path)
    assert rec.state == "off"
    return rec


def _skipped(rec):
    return {i.entry: i.message for i in rec.issues if i.component == "mcp" and i.entry}


class TestDocument:
    @pytest.mark.parametrize(
        "text",
        [
            '{"$schema": "' + MCP_SCHEMA + '", "mcpServers": }',
            json.dumps({"mcpServers": {"v": VALID}}),
            json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {"v": VALID}, "extra": 1}),
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.1.0/mcp.schema.json",
                    "mcpServers": {"v": VALID},
                }
            ),
            json.dumps({"$schema": MCP_SCHEMA, "mcpServers": []}),
            "[]",
        ],
        ids=["bad-json", "no-schema", "extra-field", "version-mismatch", "servers-list", "array"],
    )
    def test_document_problems_disable_mcp_only(self, tmp_path, text):
        install(tmp_path, "p", **{"skills__s__SKILL.md": "---\nname: s\ndescription: d\n---\n"})
        rec = _load(tmp_path, text=text)
        assert rec.servers == () and [s.name for s in rec.skills] == ["s"]
        assert any(i.component == "mcp" and "MCP is off" in i.message for i in rec.issues)

    def test_missing_file_is_not_an_issue(self, tmp_path):
        install(tmp_path, "p")
        (rec,) = discover_plugins(tmp_path)
        assert rec.servers == () and rec.issues == ()

    def test_empty_servers_is_valid(self, tmp_path):
        rec = _load(tmp_path, {})
        assert rec.servers == () and rec.issues == ()

    def test_an_unreadable_file_is_a_failed_read_and_bad_json_is_not(self, tmp_path, monkeypatch):
        """Patched where ``mcp.json`` is opened, not made with chmod: root and Windows
        read a mode-000 file anyway, so a chmod test would pass there for nothing."""
        install(tmp_path, "bad", **{"mcp.json": "{"})
        install(tmp_path, "locked", **{"mcp.json": mcp({"v": VALID})})

        def refusing(file, *args, **kwargs):
            path = os.fspath(file)
            if os.path.basename(os.path.dirname(path)) == "locked":
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return builtins.open(file, *args, **kwargs)

        monkeypatch.setattr(mcp_config_module, "open", refusing, raising=False)
        bad, locked = discover_plugins(tmp_path)
        assert locked.state == "off" and locked.servers == ()
        assert locked.issues == (
            Issue("mcp", "mcp.json could not be read; MCP is off", failed=True),
        )
        assert bad.issues == (Issue("mcp", "mcp.json is not valid JSON; MCP is off"),)

    def test_a_file_that_is_not_utf8_breaks_a_rule(self, tmp_path):
        root = install(tmp_path, "p", **{"mcp.json": ""})
        (root / "mcp.json").write_bytes(b'{"$schema": "\xff"}')
        (rec,) = discover_plugins(tmp_path)
        assert rec.issues == (Issue("mcp", "mcp.json is not UTF-8 text; MCP is off"),)


class TestStdio:
    def test_valid_entry_is_kept_as_written(self, tmp_path):
        entry = {
            "type": "stdio",
            "command": "./bin/server",
            "args": ["--data", "${PLUGIN_DATA}/x", "$HOME"],
            "env": {"CONFIG": "${PLUGIN_ROOT}/c.json"},
            "cwd": "${PLUGIN_ROOT}",
        }
        install(tmp_path, "p", **{"bin__server": "#!/bin/sh\n"})
        (server,) = _load(tmp_path, {"s": entry}).servers
        assert server == StdioServer(
            name="s",
            command="./bin/server",
            args=("--data", "${PLUGIN_DATA}/x", "$HOME"),
            env={"CONFIG": "${PLUGIN_ROOT}/c.json"},
            cwd="${PLUGIN_ROOT}",
        )

    @pytest.mark.parametrize(
        "entry",
        [
            {"type": "stdio"},
            {"command": "node"},
            {"type": "stdio", "command": "node", "conformanceUnknown": True},
            {"type": "stdio", "command": "node", "cwd": "."},
            {"type": "stdio", "command": "node", "cwd": "data"},
            {"type": "stdio", "command": "node", "cwd": "./.."},
            {"type": "stdio", "command": "node", "cwd": "${PLUGIN_DATA}/.."},
            {"type": "stdio", "command": "node", "cwd": "${PLUGIN_ROOT}/../x"},
            {"type": "stdio", "command": "node", "env": {"PLUGIN_ROOT": "x"}},
            {"type": "stdio", "command": "node", "env": {"PLUGIN_DATA": "x"}},
            {"type": "stdio", "command": "../bin/server"},
            {"type": "stdio", "command": "bin/server"},
            {"type": "stdio", "command": "C:tool.exe"},
            {"type": "stdio", "command": "C:"},
            {"type": "stdio", "command": "node", "args": "x"},
            {"type": "stdio", "command": "node", "args": None},
            {"type": "stdio", "command": "node", "env": {"A": 1}},
            {"type": "stdio", "command": "node", "env": None},
            {"type": "stdio", "command": "node", "cwd": None},
            {"type": "stdio", "command": "node", "url": "https://x.test"},
        ],
    )
    def test_invalid_entry_is_skipped_and_siblings_load(self, tmp_path, entry):
        rec = _load(tmp_path, {"bad": entry, "good": VALID})
        assert [s.name for s in rec.servers] == ["good"]
        assert list(_skipped(rec)) == ["bad"]

    def test_command_with_spaces_is_one_token(self, tmp_path):
        install(tmp_path, "p", **{"bin__probe token.sh": "#!/bin/sh\n"})
        (server,) = _load(
            tmp_path, {"s": {"type": "stdio", "command": "./bin/probe token.sh"}}
        ).servers
        assert server.command == "./bin/probe token.sh"

    @pytest.mark.parametrize("command", ["./bin/missing", "./bin"])
    def test_relative_command_must_be_a_file(self, tmp_path, command):
        install(tmp_path, "p", **{"bin__other": "x"})
        rec = _load(tmp_path, {"bad": {"type": "stdio", "command": command}, "good": VALID})
        assert [s.name for s in rec.servers] == ["good"]

    def test_command_linked_outside_the_root_is_skipped(self, tmp_path):
        root = install(tmp_path, "p")
        outside = tmp_path / "tool"
        outside.write_text("#!/bin/sh\n")
        symlink(root / "escape", outside)
        rec = _load(tmp_path, {"s": {"type": "stdio", "command": "./escape"}})
        assert rec.servers == () and list(_skipped(rec)) == ["s"]

    def test_cwd_linked_outside_the_root_is_skipped(self, tmp_path):
        root = install(tmp_path, "p")
        symlink(root / "escape-link", "..", directory=True)
        rec = _load(tmp_path, {"s": {"type": "stdio", "command": "node", "cwd": "./escape-link"}})
        assert rec.servers == ()


class TestRemote:
    def test_valid_entries(self, tmp_path):
        rec = _load(
            tmp_path,
            {
                "a": {
                    "type": "streamable-http",
                    "url": "https://x.test/mcp",
                    "headers": {"X-T": "1"},
                },
                "b": {"type": "sse", "url": "http://127.0.0.1:9/sse"},
                "c": {"type": "streamable-http", "url": "http://localhost/mcp"},
                "d": {"type": "streamable-http", "url": "http://[::1]:9/mcp"},
                "e": {
                    "type": "streamable-http",
                    "url": "https://" + "a" * 63 + ".x-1.test/mcp",
                    "headers": {"X_T.~!#$%&'*+^`|": "a\tb"},
                },
            },
        )
        assert [s.name for s in rec.servers] == ["a", "b", "c", "d", "e"]
        assert rec.servers[0] == RemoteServer(
            name="a", type="streamable-http", url="https://x.test/mcp", headers={"X-T": "1"}
        )

    @pytest.mark.parametrize("kind", ["streamable-http", "sse"])
    @pytest.mark.parametrize(
        "fields",
        [
            {"url": "http://0.0.0.0:9/x"},
            {"url": "http://example.test/x"},
            {"url": "//127.0.0.1:9/x"},
            {"url": "http://u:p@127.0.0.1:9/x"},
            {"url": "http://127.0.0.1:9/x#frag"},
            {"url": "ftp://127.0.0.1/x"},
            {"url": "https://example.test:invalid/mcp"},
            {"url": "https://example.test:/mcp"},
            {"url": "https://example.test/m cp"},
            {"url": "https://example.test/mcp\n"},
            {"url": "https://bad_host.test/mcp"},
            {"url": "https://-bad.test/mcp"},
            {"url": "https://bad-.test/mcp"},
            {"url": "https://" + "a" * 64 + ".test/mcp"},
            {"url": "http://127.0.0.1:9/x", "headers": None},
            {"url": "http://127.0.0.1:9/x", "headers": {"X Bad": "v"}},
            {"url": "http://127.0.0.1:9/x", "headers": {"x": "a\r\nb"}},
            {"url": "http://127.0.0.1:9/x", "headers": {"": "v"}},
            {"url": "http://127.0.0.1:9/x", "headers": {"X:Y": "v"}},
            {"url": "http://127.0.0.1:9/x", "headers": {"x": "a\x7fb"}},
            {"url": "http://127.0.0.1:9/x", "headers": {"x": "a\x00b"}},
            {"url": "http://127.0.0.1:9/x", "headers": {"X-D": "1", "x-d": "2"}},
            {"url": "http://127.0.0.1:9/x", "command": "node"},
        ],
    )
    def test_invalid_entry_is_skipped(self, tmp_path, kind, fields):
        rec = _load(tmp_path, {"bad": {"type": kind, **fields}, "good": VALID})
        assert [s.name for s in rec.servers] == ["good"]

    def test_legacy_http_type_is_unknown(self, tmp_path):
        rec = _load(tmp_path, {"bad": {"type": "http", "url": "https://x.test"}})
        assert rec.servers == () and "unknown type" in _skipped(rec)["bad"]


class TestConformanceRecoveryCommandLinks:
    """The Recovery fixture's two command-link cases, built as it ships them.

    Each link chain ends outside the plugin root on its own OS. The verdict must
    not depend on which OS reads it: both entries are skipped everywhere, and
    only ``recovery-valid`` survives.
    """

    def test_both_escape_links_are_skipped_on_every_platform(self, tmp_path):
        root = install(tmp_path, "p", **{"dist__probe.mjs": ""})
        symlink(root / "escape-command-intermediate-posix", "/usr/bin/env")
        symlink(root / "escape-command-posix", "escape-command-intermediate-posix")
        symlink(root / "escape-command-intermediate-windows.exe", "C:/Windows/System32/cmd.exe")
        symlink(root / "escape-command-windows.exe", "escape-command-intermediate-windows.exe")
        rec = _load(
            tmp_path,
            {
                "recovery-valid": VALID,
                "recovery-command-symlink-posix": {
                    "type": "stdio",
                    "command": "./escape-command-posix",
                },
                "recovery-command-symlink-windows": {
                    "type": "stdio",
                    "command": "./escape-command-windows.exe",
                },
            },
        )
        assert [s.name for s in rec.servers] == ["recovery-valid"]
        assert sorted(_skipped(rec)) == [
            "recovery-command-symlink-posix",
            "recovery-command-symlink-windows",
        ]

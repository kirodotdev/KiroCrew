"""Converting client-specific plugins into standard Agent Plugins (spec §4)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from agent_plugins_helpers import MCP_SCHEMA, PLUGIN_SCHEMA, skill, symlink

from kiro_crew.agent_plugins import convert as convert_mod
from kiro_crew.agent_plugins import discover_plugins
from kiro_crew.agent_plugins.convert import (
    ConversionError,
    PlaceholderError,
    convert_plugin,
    convert_server,
    find_client_manifest,
    normalize_name,
    placeholder_names,
)


def _write(root: Path, files: dict[str, str | dict]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
    return root


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        h.update(str(path.relative_to(root)).encode())
        if path.is_file() and not path.is_symlink():
            h.update(path.read_bytes())
    return h.hexdigest()


def _install_and_load(tmp_path: Path, out: Path, name: str):
    home = tmp_path / "home"
    (home / "installed").mkdir(parents=True)
    out.rename(home / "installed" / name)
    (home / "installed.json").write_text(json.dumps({"installedPowers": [{"name": name}]}))
    (rec,) = discover_plugins(home)
    return rec


CLAUDE = {
    ".claude-plugin/plugin.json": {
        "name": "Acme Tools",
        "version": "1.2.0",
        "description": "d",
        "author": "Ada",
        "commands": "./commands",
        "hooks": "./hooks/hooks.json",
    },
    ".mcp.json": {
        "mcpServers": {
            "local": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/server", "args": ["--x"]},
            "remote": {"type": "http", "url": "https://acme.test/mcp", "oauth": {"x": 1}},
            "needs-key": {"type": "http", "url": "https://acme.test/mcp?k=${ACME_KEY}"},
        }
    },
    "bin/server": "#!/bin/sh\n",
    "skills/lint/SKILL.md": skill("lint"),
    "commands/run.md": "do it",
}


class TestClaudePackage:
    def test_converts_and_loads(self, tmp_path):
        src = _write(tmp_path / "src", CLAUDE)
        report = convert_plugin(src, tmp_path / "out")
        assert report.name == "acme-tools"
        assert report.source_manifest == ".claude-plugin/plugin.json"
        assert report.not_converted == ["commands", "hooks"]
        assert list(report.skipped_servers) == ["needs-key"]

        manifest = json.loads((tmp_path / "out/plugin.json").read_text())
        assert manifest["$schema"] == PLUGIN_SCHEMA
        assert manifest["author"] == {"name": "Ada"}
        assert "extensions" not in manifest
        mcp = json.loads((tmp_path / "out/mcp.json").read_text())
        assert mcp["$schema"] == MCP_SCHEMA
        assert mcp["mcpServers"] == {
            "local": {"type": "stdio", "command": "./bin/server", "args": ["--x"]},
            "remote": {"type": "streamable-http", "url": "https://acme.test/mcp"},
        }

        rec = _install_and_load(tmp_path, tmp_path / "out", "acme-tools")
        assert rec.state == "off" and rec.issues == ()
        assert [s.name for s in rec.skills] == ["lint"]
        assert sorted(s.name for s in rec.servers) == ["local", "remote"]

    def test_source_is_untouched(self, tmp_path):
        src = _write(tmp_path / "src", CLAUDE)
        before = _tree_digest(src)
        convert_plugin(src, tmp_path / "out")
        assert _tree_digest(src) == before

    def test_client_files_are_kept_beside_the_core(self, tmp_path):
        src = _write(tmp_path / "src", CLAUDE)
        convert_plugin(src, tmp_path / "out")
        assert (tmp_path / "out/.claude-plugin/plugin.json").is_file()
        assert (tmp_path / "out/commands/run.md").is_file()


class TestOtherLayouts:
    def test_codex_skills_path_and_interface(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".codex-plugin/plugin.json": {
                    "name": "notes",
                    "skills": "./skills/",
                    "interface": {"displayName": "Notes"},
                    "apps": "./.app.json",
                },
                "skills/write-note/SKILL.md": skill("write-note"),
            },
        )
        report = convert_plugin(src, tmp_path / "out")
        assert report.not_converted == ["apps", "interface"]
        assert not (tmp_path / "out/mcp.json").exists()
        rec = _install_and_load(tmp_path, tmp_path / "out", "notes")
        assert [s.name for s in rec.skills] == ["write-note"]

    def test_cursor_mcp_path(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".cursor-plugin/plugin.json": {
                    "name": "linear",
                    "mcpServers": "mcp.json",
                    "logo": "x.png",
                },
                "mcp.json": {
                    "mcpServers": {"linear": {"type": "http", "url": "https://l.test/mcp"}}
                },
            },
        )
        convert_plugin(src, tmp_path / "out")
        rec = _install_and_load(tmp_path, tmp_path / "out", "linear")
        assert [s.type for s in rec.servers] == ["streamable-http"]

    def test_skills_from_a_client_folder_move_under_skills(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                ".claude/skills/folder-name/SKILL.md": skill("real-name"),
            },
        )
        convert_plugin(src, tmp_path / "out")
        rec = _install_and_load(tmp_path, tmp_path / "out", "p")
        assert [s.name for s in rec.skills] == ["real-name"]

    def test_root_manifest_without_schema_is_converted(self, tmp_path):
        src = _write(tmp_path / "src", {"plugin.json": {"name": "bare", "hooks": {}}})
        report = convert_plugin(src, tmp_path / "out")
        assert report.source_manifest == "plugin.json" and report.not_converted == ["hooks"]


class TestRefusals:
    def test_already_standard_is_refused(self, tmp_path):
        src = _write(tmp_path / "src", {"plugin.json": {"$schema": PLUGIN_SCHEMA, "name": "p"}})
        with pytest.raises(ConversionError, match="already standard"):
            convert_plugin(src, tmp_path / "out")
        assert not (tmp_path / "out").exists()

    def test_no_manifest_is_refused(self, tmp_path):
        src = _write(tmp_path / "src", {"README.md": "x"})
        with pytest.raises(ConversionError):
            convert_plugin(src, tmp_path / "out")

    def test_unusable_name_writes_nothing(self, tmp_path):
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "---"}})
        with pytest.raises(ConversionError, match="name"):
            convert_plugin(src, tmp_path / "out")
        assert sorted(os.listdir(tmp_path)) == ["src"]

    def test_existing_destination_is_refused(self, tmp_path):
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        (tmp_path / "out").mkdir()
        with pytest.raises(ConversionError, match="exists"):
            convert_plugin(src, tmp_path / "out")

    def test_links_are_not_followed_or_copied(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("do not copy")
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        symlink(src / "leak.txt", secret)
        report = convert_plugin(src, tmp_path / "out")
        assert not (tmp_path / "out/leak.txt").exists()
        assert any("leak.txt" in n for n in report.notes)

    def test_junction_like_directory_is_not_traversed_or_copied(self, tmp_path, monkeypatch):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                "external/secret.txt": "do not copy",
            },
        )
        real_check = convert_mod.is_link_or_junction
        monkeypatch.setattr(
            convert_mod,
            "is_link_or_junction",
            lambda path: Path(path).name == "external" or real_check(path),
        )

        report = convert_plugin(src, tmp_path / "out")

        assert not (tmp_path / "out/external").exists()
        assert any("external" in note for note in report.notes)


class TestServerMapping:
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            (
                {"command": "npx", "args": ["-y", "pkg"]},
                {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]},
            ),
            ({"url": "https://x.test"}, {"type": "streamable-http", "url": "https://x.test"}),
            (
                {"type": "sse", "url": "https://x.test/sse"},
                {"type": "sse", "url": "https://x.test/sse"},
            ),
            (
                {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/s.js"], "cwd": "."},
                {
                    "type": "stdio",
                    "command": "node",
                    "args": ["${PLUGIN_ROOT}/s.js"],
                    "cwd": "${PLUGIN_ROOT}",
                },
            ),
            (
                {"command": "node", "cwd": "data"},
                {"type": "stdio", "command": "node", "cwd": "./data"},
            ),
        ],
    )
    def test_mapping(self, tmp_path, entry, expected):
        assert convert_server(entry, tmp_path) == expected

    def test_relative_command_must_be_bundled(self, tmp_path):
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin/s").write_text("")
        assert convert_server({"command": "bin/s"}, tmp_path)["command"] == "./bin/s"
        with pytest.raises(ConversionError):
            convert_server({"command": "bin/missing"}, tmp_path)

    @pytest.mark.parametrize(
        "entry",
        [
            {"command": "/usr/local/bin/s"},
            {"command": "node", "env": {"TOKEN": "${API_TOKEN}"}},
            {"type": "http", "url": "https://x.test", "headers": {"A": "Bearer ${T}"}},
            {"type": "ws", "url": "wss://x.test"},
            {"note": "nothing"},
            {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT"]},
            {"command": "node", "env": {"${T": "x"}},
            {"url": "https://x.test/${CLAUDE_PLUGIN_ROOT}"},
            {"command": "node", "args": ["${}"]},
        ],
    )
    def test_unportable_entries_are_refused(self, tmp_path, entry):
        with pytest.raises(ConversionError):
            convert_server(entry, tmp_path)

    def test_unclosed_placeholder_is_a_placeholder_error(self, tmp_path):
        with pytest.raises(PlaceholderError):
            convert_server(
                {"command": "node", "args": ["${PLUGIN_ROOT}/a", "${PLUGIN_DATA"]}, tmp_path
            )

    def test_portable_placeholders_are_kept(self, tmp_path):
        entry = {"command": "node", "args": ["${PLUGIN_ROOT}/a"], "cwd": "${PLUGIN_DATA}/w"}
        assert convert_server(entry, tmp_path)["args"] == ["${PLUGIN_ROOT}/a"]


class TestMalformedInput:
    def test_deeply_nested_manifest_is_refused_cleanly(self, tmp_path):
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": "[" * 100_000})
        with pytest.raises(ConversionError, match="JSON"):
            convert_plugin(src, tmp_path / "out")
        assert sorted(os.listdir(tmp_path)) == ["src"]

    def test_deeply_nested_mcp_file_converts_no_servers(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p"}, ".mcp.json": "[" * 100_000},
        )
        with pytest.raises(ConversionError, match="JSON"):
            convert_plugin(src, tmp_path / "out")

    def test_unparsable_skill_frontmatter_keeps_its_folder_name(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                ".claude/skills/dated/SKILL.md": "---\nname: x\ncreated: 2026-13-01\n---\n",
            },
        )
        convert_plugin(src, tmp_path / "out")
        assert (tmp_path / "out/skills/dated/SKILL.md").is_file()


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("Acme Tools", "acme-tools"),
        ("my_plugin", "my-plugin"),
        ("a..b", "a.b"),
        ("--x--", "x"),
        ("!!", None),
        (3, None),
        ("a_-b", "a-b"),
        ("a.-.b", "a.-.b"),
        ("a...b--c", "a.b-c"),
        ("  Café Tools  ", "caf-tools"),
        ("x" * 70, "x" * 64),
    ],
)
def test_normalize_name(raw, name):
    assert normalize_name(raw) == name


@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("plain", []),
        ("${PLUGIN_ROOT}/a", ["PLUGIN_ROOT"]),
        ("${A}-${B}", ["A", "B"]),
        ("${}", [""]),
        ("${a${b}", ["a${b"]),
        ("$PLUGIN_ROOT {x}", []),
    ],
)
def test_placeholder_names(value, names):
    assert placeholder_names(value) == names


@pytest.mark.parametrize("value", ["${", "a ${B", "${A} ${B"])
def test_placeholder_names_refuses_an_unclosed_placeholder(value):
    with pytest.raises(PlaceholderError):
        placeholder_names(value)


def _listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


class TestSkillPlacement:
    def test_rename_chain_keeps_every_skill(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                "skills/a/SKILL.md": skill("b"),
                "skills/b/SKILL.md": skill("c"),
            },
        )
        before = _tree_digest(src)
        convert_plugin(src, tmp_path / "out")
        assert _tree_digest(src) == before
        rec = _install_and_load(tmp_path, tmp_path / "out", "p")
        assert sorted(s.name for s in rec.skills) == ["b", "c"]
        assert rec.issues == ()

    def test_declared_skill_folder_is_placed_as_one_skill(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {
                    "name": "p",
                    "skills": ["./custom/review"],
                },
                "custom/review/SKILL.md": skill("review"),
            },
        )

        convert_plugin(src, tmp_path / "out")

        rec = _install_and_load(tmp_path, tmp_path / "out", "p")
        assert [item.name for item in rec.skills] == ["review"]

    def test_declared_skills_path_yielding_no_skill_is_noted(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": "./custom"},
                "custom/README.md": "no skill here",
            },
        )

        report = convert_plugin(src, tmp_path / "out")

        assert "skills path yielded no skill: ./custom" in report.notes

    def test_declared_skills_root_does_not_descend_into_junction_like_child(
        self, tmp_path, monkeypatch
    ):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": "custom"},
                "custom/hidden/SKILL.md": skill("hidden"),
            },
        )
        real_check = convert_mod.is_link_or_junction

        def reports_staged_child_as_junction(path):
            item = Path(path)
            staged = any(part.startswith(".p.converting-") for part in item.parts)
            return (staged and item.name == "hidden") or real_check(path)

        monkeypatch.setattr(convert_mod, "is_link_or_junction", reports_staged_child_as_junction)

        report = convert_plugin(src, tmp_path / "out")

        assert not (tmp_path / "out/skills").exists()
        assert "skills path yielded no skill: custom" in report.notes

    def test_case_only_rename_keeps_the_skill(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p"}, "skills/Lint/SKILL.md": skill("lint")},
        )
        convert_plugin(src, tmp_path / "out")
        assert sorted(os.listdir(tmp_path / "out/skills")) == ["lint"]
        rec = _install_and_load(tmp_path, tmp_path / "out", "p")
        assert [s.name for s in rec.skills] == ["lint"]

    def test_dropped_duplicate_is_named(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": ["skills", ".claude/skills"]},
                "skills/one/SKILL.md": skill("dup"),
                ".claude/skills/two/SKILL.md": skill("dup"),
                ".claude/skills/DUP/SKILL.md": "no frontmatter",
            },
        )
        report = convert_plugin(src, tmp_path / "out")
        dropped = [n for n in report.notes if "was not placed" in n]
        assert any(".claude/skills/two" in n and "skills/one" in n for n in dropped)
        assert any(".claude/skills/DUP" in n for n in dropped)
        assert (tmp_path / "out/.claude/skills/two/SKILL.md").is_file()

    def test_other_entries_of_skills_are_kept(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                "skills/README.md": "about",
                "skills/folder/SKILL.md": skill("real"),
            },
        )
        convert_plugin(src, tmp_path / "out")
        assert sorted(os.listdir(tmp_path / "out/skills")) == ["README.md", "real"]

    def test_dropped_skills_folder_skill_leaves_no_files_in_skills(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": ["./custom", "./skills"]},
                "custom/a/SKILL.md": skill("a"),
                "skills/SKILL.md": skill("a"),
                "skills/scripts/x.sh": "echo",
            },
        )
        report = convert_plugin(src, tmp_path / "out")
        assert sorted(os.listdir(tmp_path / "out/skills")) == ["a"]
        assert any("was not placed" in n for n in report.notes)

    @pytest.mark.parametrize("declared", ["./", "."])
    def test_package_root_skill_is_placed_once(self, tmp_path, declared):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": declared},
                "SKILL.md": skill("demo"),
                "scripts/run.sh": "echo\n",
                "skills/notes.md": "kept beside the placed skill",
            },
        )

        report = convert_plugin(src, tmp_path / "out")

        out = tmp_path / "out"
        assert sorted(os.listdir(out / "skills")) == ["demo", "notes.md"]
        placed = _listing(out / "skills/demo")
        assert placed == [
            ".claude-plugin",
            ".claude-plugin/plugin.json",
            "SKILL.md",
            "scripts",
            "scripts/run.sh",
        ]
        assert sorted(os.listdir(tmp_path)) == ["out", "src"]
        assert not any("not placed" in n for n in report.notes)
        rec = _install_and_load(tmp_path, out, "p")
        assert [s.name for s in rec.skills] == ["demo"]
        assert rec.issues == ()

    def test_package_root_skill_without_a_name_takes_the_plugin_name(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": "./"},
                "SKILL.md": "---\ndescription: Does a thing.\n---\nBody.\n",
            },
        )

        convert_plugin(src, tmp_path / "out")

        assert sorted(os.listdir(tmp_path / "out/skills")) == ["p"]

    def test_skill_copies_are_bounded(self, tmp_path, monkeypatch):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "skills": ["./", "./sub"]},
                "SKILL.md": skill("root"),
                "sub/SKILL.md": skill("sub"),
            },
        )
        monkeypatch.setattr(convert_mod, "MAX_FILES", 3)

        with pytest.raises(ConversionError, match="too large"):
            convert_plugin(src, tmp_path / "out")

        assert sorted(os.listdir(tmp_path)) == ["src"]

    def test_oversized_skill_md_is_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_mod, "MAX_SKILL_BYTES", 10)
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p"}, "skills/big/SKILL.md": skill("other")},
        )
        report = convert_plugin(src, tmp_path / "out")
        assert (tmp_path / "out/skills/big/SKILL.md").is_file()
        assert any("too large" in n for n in report.notes)


class TestContainment:
    def test_junction_like_manifest_folder_is_not_read(self, tmp_path, monkeypatch):
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        real_check = convert_mod.is_link_or_junction
        monkeypatch.setattr(
            convert_mod,
            "is_link_or_junction",
            lambda path: Path(path).name == ".claude-plugin" or real_check(path),
        )

        assert find_client_manifest(src) is None

    def test_manifest_behind_a_linked_folder_is_not_read(self, tmp_path):
        outside = _write(tmp_path / "outside", {"plugin.json": {"name": "evil"}})
        src = tmp_path / "src"
        src.mkdir()
        symlink(src / ".claude-plugin", outside, directory=True)
        assert find_client_manifest(src) is None
        with pytest.raises(ConversionError, match="no client plugin manifest"):
            convert_plugin(src, tmp_path / "out")
        assert not (tmp_path / "out").exists()

    def test_linked_manifest_folder_falls_back_to_a_contained_one(self, tmp_path):
        outside = _write(tmp_path / "outside", {"plugin.json": {"name": "evil"}})
        src = _write(tmp_path / "src", {".codex-plugin/plugin.json": {"name": "good"}})
        symlink(src / ".claude-plugin", outside, directory=True)
        report = convert_plugin(src, tmp_path / "out")
        assert report.source_manifest == ".codex-plugin/plugin.json"
        assert report.name == "good"

    def test_mcp_file_behind_a_linked_folder_is_not_read(self, tmp_path):
        outside = _write(
            tmp_path / "outside",
            {"mcp.json": {"mcpServers": {"x": {"type": "http", "url": "https://x.test"}}}},
        )
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p", "mcpServers": "./conf/mcp.json"}},
        )
        symlink(src / "conf", outside, directory=True)
        report = convert_plugin(src, tmp_path / "out")
        assert not (tmp_path / "out/mcp.json").exists()
        assert any("conf/mcp.json" in n for n in report.notes)

    def test_skills_root_behind_a_link_is_not_read(self, tmp_path):
        outside = _write(tmp_path / "outside", {"s/SKILL.md": skill("s")})
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        symlink(src / "skills", outside, directory=True)
        convert_plugin(src, tmp_path / "out")
        assert not (tmp_path / "out/skills").exists()

    def test_destination_inside_the_source_is_refused(self, tmp_path):
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        before = _listing(src)
        with pytest.raises(ConversionError, match="inside the source"):
            convert_plugin(src, src / "converted" / "plugin")
        assert _listing(src) == before

    def test_destination_reached_through_a_link_into_the_source_is_refused(self, tmp_path):
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        symlink(tmp_path / "alias", src, directory=True)
        before = _listing(src)
        with pytest.raises(ConversionError, match="inside the source"):
            convert_plugin(src, tmp_path / "alias" / "out")
        assert _listing(src) == before


class TestMcpOutput:
    @pytest.mark.parametrize("declared", [True, False])
    def test_direct_server_map_file_is_converted(self, tmp_path, declared):
        manifest = {"name": "p"}
        filename = "servers.json" if declared else ".mcp.json"
        if declared:
            manifest["mcpServers"] = filename
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": manifest,
                filename: {"weather": {"command": "weather-mcp"}},
            },
        )

        convert_plugin(src, tmp_path / "out")

        doc = json.loads((tmp_path / "out/mcp.json").read_text())
        assert doc["mcpServers"] == {"weather": {"type": "stdio", "command": "weather-mcp"}}

    def test_unrecognised_mcp_file_shape_is_noted(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p", "mcpServers": "servers.json"},
                "servers.json": {"weather": "weather-mcp"},
            },
        )

        report = convert_plugin(src, tmp_path / "out")

        assert any(
            "neither an mcpServers document nor a direct server map" in n for n in report.notes
        )

    def test_client_mcp_json_is_not_kept_when_nothing_converts(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".cursor-plugin/plugin.json": {"name": "p", "mcpServers": "mcp.json"},
                "mcp.json": {"mcpServers": {"abs": {"command": "/usr/bin/s"}}},
            },
        )
        report = convert_plugin(src, tmp_path / "out")
        assert not (tmp_path / "out/mcp.json").exists()
        assert list(report.skipped_servers) == ["abs"]
        assert any("mcp.json was removed" in n for n in report.notes)

    def test_client_mcp_json_is_replaced_by_the_converted_document(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                ".mcp.json": {"mcpServers": {"a": {"type": "http", "url": "https://a.test"}}},
                "mcp.json": {"mcpServers": {"stale": {"command": "npx"}}},
            },
        )
        convert_plugin(src, tmp_path / "out")
        doc = json.loads((tmp_path / "out/mcp.json").read_text())
        assert doc == {
            "$schema": MCP_SCHEMA,
            "mcpServers": {"a": {"type": "streamable-http", "url": "https://a.test"}},
        }

    def test_server_the_loader_rejects_is_reported_and_not_written(self, tmp_path):
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                ".mcp.json": {
                    "mcpServers": {
                        "ok": {"command": "npx"},
                        "reserved": {"command": "npx", "env": {"PLUGIN_ROOT": "x"}},
                        "drive": {"command": "C:tool.exe"},
                    }
                },
            },
        )
        report = convert_plugin(src, tmp_path / "out")
        assert sorted(report.skipped_servers) == ["drive", "reserved"]
        assert "bare name" in report.skipped_servers["drive"]
        doc = json.loads((tmp_path / "out/mcp.json").read_text())
        assert list(doc["mcpServers"]) == ["ok"]
        rec = _install_and_load(tmp_path, tmp_path / "out", "p")
        assert rec.issues == ()

    def test_command_outside_the_package_is_skipped(self, tmp_path):
        (tmp_path / "escape").write_text("#!/bin/sh\n")
        src = _write(
            tmp_path / "src",
            {
                ".claude-plugin/plugin.json": {"name": "p"},
                ".mcp.json": {
                    "mcpServers": {
                        "up": {"command": "./../escape"},
                        "rooted": {"command": "${CLAUDE_PLUGIN_ROOT}/../escape"},
                        "missing": {"command": "./bin/none"},
                    }
                },
            },
        )
        report = convert_plugin(src, tmp_path / "out")
        assert sorted(report.skipped_servers) == ["missing", "rooted", "up"]
        assert "outside" in report.skipped_servers["up"]
        assert not (tmp_path / "out/mcp.json").exists()

    @pytest.mark.parametrize("cwd", ["..", "./../x", "${CLAUDE_PLUGIN_ROOT}/.."])
    def test_cwd_outside_the_package_is_refused(self, tmp_path, cwd):
        with pytest.raises(ConversionError, match="outside"):
            convert_server({"command": "node", "cwd": cwd}, tmp_path)


class TestJsonOutputBounds:
    def test_mcp_uses_compact_json_when_indented_form_is_too_large(self, tmp_path, monkeypatch):
        servers = {"weather": {"command": "weather-mcp", "args": ["x"] * 20}}
        expected = {
            "$schema": MCP_SCHEMA,
            "mcpServers": {
                "weather": {
                    "type": "stdio",
                    "command": "weather-mcp",
                    "args": ["x"] * 20,
                }
            },
        }
        pretty_size = len((json.dumps(expected, indent=2) + "\n").encode())
        compact_size = len((json.dumps(expected, separators=(",", ":")) + "\n").encode())
        limit = (pretty_size + compact_size) // 2
        monkeypatch.setattr(convert_mod, "MAX_MCP_BYTES", limit)
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p", "mcpServers": servers}},
        )

        convert_plugin(src, tmp_path / "out")

        output = (tmp_path / "out/mcp.json").read_bytes()
        assert len(output) <= limit
        assert json.loads(output) == expected

    def test_manifest_uses_compact_json_when_indented_form_is_too_large(
        self, tmp_path, monkeypatch
    ):
        keywords = ["word"] * 20
        expected = {"$schema": PLUGIN_SCHEMA, "name": "p", "keywords": keywords}
        pretty_size = len((json.dumps(expected, indent=2) + "\n").encode())
        compact_size = len((json.dumps(expected, separators=(",", ":")) + "\n").encode())
        limit = (pretty_size + compact_size) // 2
        monkeypatch.setattr(convert_mod, "MAX_MANIFEST_BYTES", limit)
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p", "keywords": keywords}},
        )

        convert_plugin(src, tmp_path / "out")

        output = (tmp_path / "out/plugin.json").read_bytes()
        assert len(output) <= limit
        assert json.loads(output) == expected

    def test_compact_json_over_loader_limit_refuses_conversion(self, tmp_path, monkeypatch):
        servers = {"weather": {"command": "weather-mcp", "args": ["x"] * 20}}
        monkeypatch.setattr(convert_mod, "MAX_MCP_BYTES", 100)
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p", "mcpServers": servers}},
        )

        with pytest.raises(ConversionError, match="converted mcp.json is too large"):
            convert_plugin(src, tmp_path / "out")

        assert sorted(os.listdir(tmp_path)) == ["src"]


class TestFailureCleanup:
    def test_failure_after_staging_leaves_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_mod, "MAX_FILES", 0)
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        before = _tree_digest(src)
        with pytest.raises(ConversionError, match="too large"):
            convert_plugin(src, tmp_path / "a" / "b" / "out")
        assert sorted(os.listdir(tmp_path)) == ["src"]
        assert _tree_digest(src) == before

    def test_failure_during_skill_placement_leaves_nothing(self, tmp_path, monkeypatch):
        real_copy = convert_mod._copy_skill

        def copy_then_fail(*args):
            real_copy(*args)
            raise OSError("disk full")

        monkeypatch.setattr(convert_mod, "_copy_skill", copy_then_fail)
        src = _write(
            tmp_path / "src",
            {".claude-plugin/plugin.json": {"name": "p"}, "skills/a/SKILL.md": skill("a")},
        )

        with pytest.raises(ConversionError, match="could not be copied"):
            convert_plugin(src, tmp_path / "parent" / "out")

        assert sorted(os.listdir(tmp_path)) == ["src"]

    def test_failure_keeps_an_existing_parent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_mod, "MAX_FILES", 0)
        src = _write(tmp_path / "src", {".claude-plugin/plugin.json": {"name": "p"}})
        (tmp_path / "parent").mkdir()
        with pytest.raises(ConversionError):
            convert_plugin(src, tmp_path / "parent" / "out")
        assert os.listdir(tmp_path / "parent") == []

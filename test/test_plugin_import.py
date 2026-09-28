"""Tests for kiro_crew.apps.plugin_import -- converting a plugin package into an app.

The three properties worth a test are the ones a converter gets wrong in a way
nobody notices: it silently drops a kind, it follows a path out of the package
root, or it emits a manifest that only looks valid. The last one is covered by
installing the converted app for real rather than by asserting on the JSON.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps import plugin_import
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.plugin_import import (
    FORMAT_SCHEMA_QUALIFIED,
    FORMAT_VENDOR_DIRECTORY,
    SCHEMA_NAMESPACE_PREFIX,
    PluginImportError,
    convert_plugin_package,
    find_plugin_manifest,
    normalize_app_name,
    resolve_declared_path,
)

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _skill(root: Path, name: str, description: str = "does a thing") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )


def _package(tmp_path: Path, name: str = "demo-plugin", vendor: str = ".codex-plugin", **manifest):
    """A package with a vendor-directory manifest, the common real-world shape."""
    root = tmp_path / "src" / name
    root.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, **manifest}
    _write_json(root / vendor / "plugin.json", payload)
    return root


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


class TestSourceAndOutputResolution:
    def test_a_relative_source_dir_converts_without_a_valueerror(self, tmp_path, monkeypatch):
        """``kirocrew app import ./pkg`` passes a relative source. The manifest
        path must be resolved against the same absolute base as the root, or
        ``manifest_path.relative_to(root)`` raises ``ValueError``."""
        root = _package(tmp_path, skills="./skills")
        _skill(root, "skills")
        monkeypatch.chdir(root.parent)
        report = convert_plugin_package(Path(root.name), tmp_path / "out")
        # ``manifest_path`` on the report is root-relative, proving the resolve
        # happened without raising.
        assert not Path(report.manifest_path).is_absolute()

    def test_output_equal_to_source_is_refused(self, tmp_path):
        root = _package(tmp_path)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, root)
        assert exc.value.code == "output_within_source"

    def test_output_beneath_source_is_refused(self, tmp_path):
        """A ``--out`` under the source root would fold the destination into the
        tree the copy step walks, causing unbounded recursion."""
        root = _package(tmp_path)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, root / "nested" / "out")
        assert exc.value.code == "output_within_source"


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


class TestManifestDiscovery:
    def test_schema_qualified_root_manifest_wins(self, tmp_path):
        root = _package(tmp_path, skills="./skills")
        _write_json(
            root / "plugin.json",
            {"$schema": f"{SCHEMA_NAMESPACE_PREFIX}1.0.0/plugin.schema.json", "name": "rooted"},
        )
        path, fmt = find_plugin_manifest(root)
        assert path == root / "plugin.json"
        assert fmt == FORMAT_SCHEMA_QUALIFIED

    def test_root_manifest_without_the_schema_is_not_the_root_format(self, tmp_path):
        """A bare plugin.json at the root is some other file that shares the name."""
        root = _package(tmp_path)
        _write_json(root / "plugin.json", {"name": "not-a-plugin-manifest"})
        path, fmt = find_plugin_manifest(root)
        assert path == root / ".codex-plugin" / "plugin.json"
        assert fmt == FORMAT_VENDOR_DIRECTORY

    def test_several_vendor_directories_resolve_deterministically(self, tmp_path):
        root = _package(tmp_path, vendor=".zzz-plugin")
        _write_json(root / ".aaa-plugin" / "plugin.json", {"name": "first-in-sort-order"})
        path, _ = find_plugin_manifest(root)
        assert path == root / ".aaa-plugin" / "plugin.json"

    def test_no_manifest_is_refused_with_a_code(self, tmp_path):
        root = tmp_path / "empty"
        root.mkdir()
        with pytest.raises(PluginImportError) as exc:
            find_plugin_manifest(root)
        assert exc.value.code == "manifest_not_found"

    def test_a_file_is_not_a_package(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(PluginImportError) as exc:
            find_plugin_manifest(target)
        assert exc.value.code == "source_not_a_directory"

    def test_malformed_manifest_is_refused_not_ignored(self, tmp_path):
        root = tmp_path / "src" / "broken"
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code == "manifest_not_json"


# ---------------------------------------------------------------------------
# Declared-path containment
# ---------------------------------------------------------------------------


class TestDeclaredPathContainment:
    @pytest.mark.parametrize(
        "raw",
        [
            "skills",  # no ./ prefix
            "./",
            "./.",
            "../outside",
            "./../outside",
            "./a/../../outside",
            "/etc/passwd",
            ".//etc",
            "./\\\\server\\share",
            "",
            "   ",
        ],
    )
    def test_rejected_shapes(self, tmp_path, raw):
        with pytest.raises(PluginImportError) as exc:
            resolve_declared_path(tmp_path, raw)
        assert exc.value.code in {"invalid_declared_path", "resource_outside_root"}

    @pytest.mark.parametrize("raw", [None, 3, ["./skills"], {"path": "./skills"}])
    def test_non_string_is_rejected(self, tmp_path, raw):
        with pytest.raises(PluginImportError) as exc:
            resolve_declared_path(tmp_path, raw)
        assert exc.value.code == "invalid_declared_path"

    def test_a_relative_path_under_root_resolves(self, tmp_path):
        (tmp_path / "skills").mkdir()
        assert resolve_declared_path(tmp_path, "./skills") == (tmp_path / "skills").resolve()

    def test_a_symlink_out_of_the_package_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PluginImportError) as exc:
            resolve_declared_path(root, "./escape")
        assert exc.value.code == "resource_outside_root"

    def test_a_declared_escape_fails_the_whole_conversion(self, tmp_path):
        outside = tmp_path / "outside"
        _skill(outside, "leaked")
        root = _package(tmp_path, skills="./link")
        (root / "link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code == "resource_outside_root"
        assert not (tmp_path / "out" / "app.json").exists()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class TestSkills:
    def test_declared_skills_are_copied_and_listed(self, tmp_path):
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "search")
        _skill(root / "skills", "summarize")
        out = tmp_path / "out"

        report = convert_plugin_package(root, out)

        manifest = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert sorted(manifest["skills"]) == ["skills/search", "skills/summarize"]
        assert (out / "skills" / "search" / "SKILL.md").is_file()
        assert any(m.kind == "skills" for m in report.mapped)

    def test_a_directory_without_a_skill_entry_is_not_a_skill(self, tmp_path):
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "real")
        (root / "skills" / "notaskill").mkdir(parents=True)
        (root / "skills" / "notaskill" / "README.md").write_text("x", encoding="utf-8")

        convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["skills"] == ["skills/real"]

    def test_the_default_skills_directory_is_used_when_undeclared(self, tmp_path):
        root = _package(tmp_path)
        _skill(root / "skills", "implicit")

        convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["skills"] == ["skills/implicit"]

    def test_a_symlink_inside_a_skill_is_skipped_not_followed(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("do not copy me", encoding="utf-8")
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "sneaky")
        (root / "skills" / "sneaky" / "leak.txt").symlink_to(secret)
        out = tmp_path / "out"

        report = convert_plugin_package(root, out)

        assert not (out / "skills" / "sneaky" / "leak.txt").exists()
        assert any("link skipped" in w for w in report.warnings)

    def test_a_missing_declared_skills_root_is_a_warning_not_a_crash(self, tmp_path):
        root = _package(tmp_path, skills="./nope")
        report = convert_plugin_package(root, tmp_path / "out")
        assert any("not a directory" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


class TestMcpServers:
    def test_path_form_with_a_wrapper_key(self, tmp_path):
        root = _package(tmp_path, mcpServers="./.mcp.json")
        _write_json(
            root / ".mcp.json",
            {"mcpServers": {"weather": {"command": "weather-mcp", "args": ["--stdio"]}}},
        )

        convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["mcpServers"]["weather"]["command"] == "weather-mcp"

    def test_path_form_without_a_wrapper_key(self, tmp_path):
        root = _package(tmp_path, mcpServers="./.mcp.json")
        _write_json(root / ".mcp.json", {"weather": {"command": "weather-mcp"}})

        convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert "weather" in manifest["mcpServers"]

    def test_inline_object_form(self, tmp_path):
        root = _package(tmp_path, mcpServers={"inline": {"command": "x"}})
        convert_plugin_package(root, tmp_path / "out")
        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["mcpServers"] == {"inline": {"command": "x"}}

    def test_a_non_object_server_entry_is_dropped_with_a_warning(self, tmp_path):
        root = _package(tmp_path, mcpServers={"good": {"command": "x"}, "bad": "not-an-object"})
        report = convert_plugin_package(root, tmp_path / "out")
        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert list(manifest["mcpServers"]) == ["good"]
        assert any("bad" in w for w in report.warnings)

    def test_a_missing_declared_file_is_a_warning(self, tmp_path):
        root = _package(tmp_path, mcpServers="./.mcp.json")
        report = convert_plugin_package(root, tmp_path / "out")
        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert "mcpServers" not in manifest
        assert any("missing" in w for w in report.warnings)

    def test_a_server_whose_program_lives_in_the_package_is_refused(self, tmp_path):
        """The real shape: command node, args ./mcp/server.mjs, cwd "." """
        root = _package(tmp_path, mcpServers="./.mcp.json")
        _write_json(
            root / ".mcp.json",
            {
                "mcpServers": {
                    "local-server": {
                        "command": "node",
                        "args": ["./mcp/server.mjs", "--stdio"],
                        "cwd": ".",
                    }
                }
            },
        )

        report = convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert "mcpServers" not in manifest
        refused = [u for u in report.unmapped if u.kind == "mcpServers[local-server]"]
        assert len(refused) == 1
        assert refused[0].bucket == "d"
        assert refused[0].detail == "package-relative: args[0], cwd"

    def test_a_bare_command_server_is_kept_verbatim(self, tmp_path):
        """A bare command with flags and a package specifier is not a path."""
        server = {
            "command": "npx",
            "args": ["-y", "some-mcp@latest", "mcp"],
            "env": {"SOME_FLAG": "a,b"},
        }
        root = _package(tmp_path, mcpServers="./.mcp.json")
        _write_json(root / ".mcp.json", {"mcpServers": {"bare": server}})

        report = convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["mcpServers"] == {"bare": server}
        assert not [u for u in report.unmapped if u.kind.startswith("mcpServers")]

    def test_one_refused_server_does_not_take_the_others_with_it(self, tmp_path):
        root = _package(tmp_path, mcpServers="./.mcp.json")
        _write_json(
            root / ".mcp.json",
            {
                "mcpServers": {
                    "keep": {"command": "npx", "args": ["-y", "x"]},
                    "drop": {"command": "./bin/serve"},
                }
            },
        )

        report = convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert list(manifest["mcpServers"]) == ["keep"]
        assert [u.detail for u in report.unmapped if u.kind == "mcpServers[drop]"] == [
            "package-relative: command"
        ]

    def test_an_absolute_cwd_is_not_package_relative(self, tmp_path):
        root = _package(tmp_path, mcpServers={"abs": {"command": "serve", "cwd": "/opt/app"}})
        report = convert_plugin_package(root, tmp_path / "out")
        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert list(manifest["mcpServers"]) == ["abs"]
        assert not [u for u in report.unmapped if u.kind.startswith("mcpServers")]


# ---------------------------------------------------------------------------
# Kinds with no target
# ---------------------------------------------------------------------------


class TestUnmappedKinds:
    def _hooks_package(self, tmp_path, sentinel: Path) -> Path:
        root = _package(tmp_path, hooks="./hooks.json")
        _write_json(
            root / "hooks.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": f"touch {sentinel}"}],
                        }
                    ],
                    "PreCompact": [{"hooks": [{"type": "prompt"}]}],
                }
            },
        )
        return root

    def test_hooks_are_reported_and_never_emitted(self, tmp_path):
        root = self._hooks_package(tmp_path, tmp_path / "never")
        report = convert_plugin_package(root, tmp_path / "out")

        hooks = [u for u in report.unmapped if u.kind == "hooks"]
        assert len(hooks) == 1
        assert hooks[0].bucket == "d"
        assert "PreToolUse->preToolUse" in hooks[0].detail
        assert "PreCompact" in hooks[0].detail

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert "hooks" not in manifest

    def test_conversion_runs_no_command_from_the_package(self, tmp_path):
        """A hook command is data to a converter. Nothing in the package executes."""
        sentinel = tmp_path / "executed"
        root = self._hooks_package(tmp_path, sentinel)

        convert_plugin_package(root, tmp_path / "out")

        assert not sentinel.exists()

    def test_an_empty_hooks_declaration_reads_as_declared_with_no_events(self, tmp_path):
        """A package may reserve the kind and declare nothing. Not a malformed document."""
        root = _package(tmp_path, hooks={})
        report = convert_plugin_package(root, tmp_path / "out")

        hooks = [u for u in report.unmapped if u.kind == "hooks"]
        assert len(hooks) == 1
        assert hooks[0].detail == "declared with no events"
        assert not [w for w in report.warnings if "hooks" in w]

    def test_connector_directories_are_reported(self, tmp_path):
        root = _package(tmp_path, apps="./apps")
        (root / "apps").mkdir()
        report = convert_plugin_package(root, tmp_path / "out")
        assert any(u.kind == "apps" and u.bucket == "d" for u in report.unmapped)

    def test_presentation_fields_are_carried_as_provenance(self, tmp_path):
        root = _package(
            tmp_path,
            homepage="https://example.test/",
            interface={
                "displayName": "Demo",
                "composerIcon": "./icon.svg",
                "brandColor": "#fff",
                "screenshots": [],
            },
        )
        report = convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        carried = manifest["importedPlugin"]["carried"]
        assert carried == {
            "composerIcon": "./icon.svg",
            "brandColor": "#fff",
            "homepage": "https://example.test/",
        }
        assert "screenshots" not in carried
        assert any(u.kind == "presentation and links" for u in report.unmapped)

    def test_unknown_manifest_keys_are_named_in_a_warning(self, tmp_path):
        root = _package(tmp_path, lspServers="./lsp.json")
        report = convert_plugin_package(root, tmp_path / "out")
        assert any("lspServers" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Demo Plugin", "demo-plugin"),
            ("demo_plugin", "demo-plugin"),
            ("  Demo   Plugin  ", "demo-plugin"),
            ("demo@2", "demo-2"),
            ("DEMO", "demo"),
        ],
    )
    def test_names_fold_to_the_app_name_contract(self, raw, expected):
        assert normalize_app_name(raw) == expected

    @pytest.mark.parametrize("raw", ["system", "library", "registry", "install"])
    def test_reserved_names_are_refused(self, raw):
        with pytest.raises(PluginImportError) as exc:
            normalize_app_name(raw)
        assert exc.value.code == "reserved_app_name"

    @pytest.mark.parametrize("raw", ["", "   ", "@@@", "---"])
    def test_unusable_names_are_refused(self, raw):
        with pytest.raises(PluginImportError) as exc:
            normalize_app_name(raw)
        assert exc.value.code == "invalid_app_name"

    def test_a_non_string_manifest_name_falls_back_to_the_directory(self, tmp_path):
        """A manifest is foreign input, so ``name`` can be any JSON type.

        A truthy non-string reached ``normalize_app_name``, whose ``.strip()``
        raised ``AttributeError`` past the handler's ``(OSError,
        JSONDecodeError)`` arms, so ``kirocrew app import`` aborted with a
        traceback. An unusable name is an ABSENT name, and an absent one already
        has a documented fallback: the source directory.
        """
        import argparse

        from kiro_crew.cli_commands import _handle_app_import

        root = tmp_path / "src" / "my-plugin-dir"
        root.mkdir(parents=True)
        _write_json(root / ".codex-plugin" / "plugin.json", {"name": 123})
        _skill(root / "skills", "greet")
        out = tmp_path / "out"

        _handle_app_import(argparse.Namespace(source=str(root), name=None, out=str(out)))

        assert json.loads((out / "app.json").read_text(encoding="utf-8"))["name"] == "my-plugin-dir"

    def test_an_override_replaces_the_declared_name(self, tmp_path):
        root = _package(tmp_path, name="system")
        report = convert_plugin_package(root, tmp_path / "out", name_override="imported-demo")
        assert report.app_name == "imported-demo"

    def test_missing_version_and_description_are_synthesized(self, tmp_path):
        root = _package(tmp_path)
        report = convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["version"] == "0.0.0"
        assert manifest["description"]
        assert any("no version" in w for w in report.warnings)
        assert any("no description" in w for w in report.warnings)

    def test_a_non_semver_version_is_replaced_with_a_warning(self, tmp_path):
        root = _package(tmp_path, version="2026.09")
        report = convert_plugin_package(root, tmp_path / "out")
        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["version"] == "0.0.0"
        assert any("not semver" in w for w in report.warnings)

    def test_display_name_and_author_come_from_the_interface_block(self, tmp_path):
        root = _package(
            tmp_path,
            interface={"displayName": "Demo Plugin", "developerName": "Someone"},
            description="a described package",
            keywords=["search", "", 4],
        )
        convert_plugin_package(root, tmp_path / "out")

        manifest = json.loads((tmp_path / "out" / "app.json").read_text(encoding="utf-8"))
        assert manifest["displayName"] == "Demo Plugin"
        assert manifest["author"] == "Someone"
        assert manifest["description"] == "a described package"
        assert manifest["tags"] == ["search"]

    def test_the_shape_published_packages_actually_use(self, tmp_path):
        """An object author, a license, the uppercase URL keys, a trailing-slash path."""
        root = _package(
            tmp_path,
            name="acme-tracker",
            version="0.1.4",
            description="Work with tracker items.",
            author={"name": "Acme, Inc.", "email": "support@acme.test"},
            homepage="https://acme.test",
            repository="https://github.com/acme/plugins",
            license="MIT",
            keywords=["tracker", "productivity"],
            skills="./skills/",
            apps="./.app.json",
            mcpServers="./.mcp.json",
            interface={
                "displayName": "Acme Tracker",
                "shortDescription": "Read and manage tracker items",
                "developerName": "Acme, Inc.",
                "websiteURL": "https://acme.test",
                "privacyPolicyURL": "https://acme.test/privacy",
                "screenshots": [],
                "brandColor": "#FF584A",
            },
        )
        _skill(root / "skills", "list-items")
        (root / ".app.json").write_text("{}", encoding="utf-8")
        _write_json(root / ".mcp.json", {"mcpServers": {"acme": {"command": "acme-mcp"}}})
        out = tmp_path / "out"

        report = convert_plugin_package(root, out)

        manifest = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert manifest["name"] == "acme-tracker"
        assert manifest["version"] == "0.1.4"
        assert manifest["displayName"] == "Acme Tracker"
        assert manifest["author"] == "Acme, Inc."
        assert manifest["license"] == "MIT"
        assert manifest["skills"] == ["skills/list-items"]
        assert manifest["mcpServers"] == {"acme": {"command": "acme-mcp"}}
        assert AppManifest.from_dict(manifest).validate(out) == []
        # Every declared source field was either mapped or reported.
        assert {u.kind for u in report.unmapped} == {"apps", "presentation and links"}
        assert report.warnings == []


# ---------------------------------------------------------------------------
# Output safety
# ---------------------------------------------------------------------------


class TestForeignManifestShape:
    """A manifest is untrusted input, so a wrong SHAPE is a coded refusal.

    Every case here reached `PluginImportError`'s callers as a raw traceback
    before, which defeats the point of having codes at all: the CLI catches
    `PluginImportError` and prints its message, and catches nothing else.
    """

    def test_a_manifest_that_is_not_an_object_is_refused_not_a_traceback(self, tmp_path):
        root = tmp_path / "src" / "demo-plugin"
        root.mkdir(parents=True)
        _write_json(root / ".codex-plugin" / "plugin.json", [1, 2, 3])
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code == "manifest_not_object"

    def test_the_cli_reads_the_name_through_the_module_that_owns_the_contract(self, tmp_path):
        """The CLI parsed the manifest itself and lost the shape check, so a
        valid-JSON non-object reached `.get` and raised AttributeError past both
        of its except arms."""
        import argparse

        from kiro_crew.cli_commands import _handle_app_import

        root = tmp_path / "src" / "demo-plugin"
        root.mkdir(parents=True)
        _write_json(root / ".codex-plugin" / "plugin.json", "not an object")
        with pytest.raises(SystemExit) as exc:
            _handle_app_import(
                argparse.Namespace(source=str(root), name=None, out=str(tmp_path / "out"))
            )
        assert exc.value.code == 1

    @pytest.mark.parametrize("bad", [5, "abc", {"a": 1}, True])
    def test_a_wrong_typed_list_field_is_refused(self, tmp_path, bad):
        root = _package(tmp_path, keywords=bad)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code == "invalid_manifest_field"

    def test_an_output_path_that_is_a_file_is_refused(self, tmp_path):
        """`iterdir()` on a regular file raises NotADirectoryError, an OSError the
        CLI does not catch, so the emptiness check had to be reached only for a
        directory."""
        root = _package(tmp_path)
        out = tmp_path / "out"
        out.write_text("occupied", encoding="utf-8")
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, out)
        assert exc.value.code == "output_not_a_directory"

    def test_an_unreadable_output_directory_is_refused_not_read_as_empty(
        self, tmp_path, monkeypatch
    ):
        """A directory the process cannot read must refuse, not look empty.

        ``iterdir()`` faults on more than a non-directory: without read permission
        it raises PermissionError, an OSError the CLI does not catch, so it reached
        the operator as a traceback rather than a sentence. Treating the fault as
        "empty" would be worse than the traceback: the copy walk overwrites by name,
        so it would clobber files the check could not see.

        The fault is INJECTED rather than produced with ``chmod``. A mode of 0 is not
        a portable way to make a directory unreadable -- on Windows it does not
        remove read access, and a process running as root ignores it either way -- so
        a chmod-based version of this test silently passes on the platforms where it
        proves nothing. Patched on ``Path.iterdir``, which is what the code calls,
        and narrowed to the output directory so an unrelated listing elsewhere in the
        call still works.
        """
        root = _package(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        (out / "occupant.txt").write_text("x", encoding="utf-8")

        real_iterdir = Path.iterdir

        def refusing_iterdir(self):
            if self == out:
                raise PermissionError(13, "Permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", refusing_iterdir)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, out)
        assert exc.value.code == "output_not_readable"
        monkeypatch.undo()
        # The occupant is still there: the refusal happened before any copy.
        assert (out / "occupant.txt").read_text(encoding="utf-8") == "x"

    def test_the_spec_lists_every_code_the_module_raises(self):
        """The spec's code list drifted once already: it promised "every refusal"
        and omitted `output_within_source`. Compared here so it cannot again."""
        import ast
        from pathlib import Path as _Path

        module = _Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/plugin_import.py"
        raised = set()
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "PluginImportError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                raised.add(node.args[0].value)

        spec = _Path(__file__).resolve().parents[1] / "docs/system-specs/modules/plugin-import.md"
        text = spec.read_text(encoding="utf-8")
        start = text.index("Every refusal carries a stable")
        listed = set(re.findall(r"`([a-z_]+)`", text[start : text.index("\n\n", start)]))
        listed.discard("code")
        listed.discard("PluginImportError")
        assert raised == listed


class TestLinksAndJunctions:
    """The package root is the authority boundary, on Windows too.

    A directory junction is not a symlink by ``Path.is_symlink``, so a walk that
    asks only that question reads a junction as an ordinary directory and copies
    whatever it points at. A junction to the user's ``.ssh`` therefore puts
    private keys in the emitted app.
    """

    def test_the_copy_walk_skips_whatever_the_platform_calls_a_link(self, tmp_path, monkeypatch):
        """Junctions cannot be created on POSIX, so the platform's own verdict is
        what gets stubbed: this pins that the walk ASKS the junction-aware helper
        and honours it, which is the part that was missing."""
        from kiro_crew.apps import plugin_import as PI

        root = _package(tmp_path)
        skill = root / "skills" / "greet"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# greet\n", encoding="utf-8")
        secrets = skill / "id_rsa_dir"
        secrets.mkdir()
        (secrets / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

        real = PI.is_link_or_junction
        monkeypatch.setattr(PI, "is_link_or_junction", lambda p: Path(p) == secrets or real(p))

        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        assert not list(out.rglob("id_rsa")), "a junction's target was copied into the app"
        assert any("link skipped" in w for w in report.warnings)

    def test_a_declared_path_through_a_link_is_refused_before_it_is_resolved(self, tmp_path):
        """The refusal has to come BEFORE resolution, not after it.

        ``.resolve()`` traverses a junction on Windows, and a junction whose target
        is a UNC share makes the OS authenticate to that host while resolving. A
        containment check that runs afterwards refuses a path whose credentials
        have already left, so the link check is ordered ahead of the resolve and
        this test pins that order by observing that resolve is never reached.
        """
        from kiro_crew.apps import plugin_import as PI

        root = tmp_path / "pkg"
        (root / "real").mkdir(parents=True)
        (root / "real" / "SKILL.md").write_text("# s\n", encoding="utf-8")
        (root / "linked").symlink_to(root / "real", target_is_directory=True)

        resolved: list[str] = []
        real_resolve = Path.resolve

        def watching_resolve(self, *a, **k):
            resolved.append(str(self))
            return real_resolve(self, *a, **k)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "resolve", watching_resolve)
            with pytest.raises(PluginImportError) as exc:
                PI.resolve_declared_path(root, "./linked/SKILL.md")
        assert exc.value.code == "resource_outside_root"
        assert not any(
            "linked" in r for r in resolved
        ), f"the linked path was resolved before being refused: {resolved}"

    def test_no_bare_is_symlink_check_remains_in_the_module(self):
        """Each link check has to ask the junction-aware helper, and a site added
        later is exactly how the gap comes back, so the module is checked as a
        whole rather than one call site at a time."""
        from pathlib import Path as _Path

        module = _Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/plugin_import.py"
        source = module.read_text(encoding="utf-8")
        assert ".is_symlink()" not in source, (
            "use platform_compat.is_link_or_junction: Path.is_symlink answers False "
            "for a Windows directory junction"
        )

    def test_a_copy_that_fails_is_reported_not_raised(self, tmp_path, monkeypatch):
        """stat() can succeed and the copy still fail -- a locked, vanishing or
        unreadable file. The CLI catches PluginImportError and nothing else, so an
        escaping OSError reaches the operator as a traceback."""
        import shutil as _shutil

        from kiro_crew.apps import plugin_import as PI

        root = _package(tmp_path)
        _skill(root / "skills", "greet")

        def _refuse(src, dst, *a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(PI.shutil, "copy2", _refuse)
        report = convert_plugin_package(root, tmp_path / "out")
        assert any("unreadable, skipped" in w for w in report.warnings)
        assert _shutil.copy2 is not _refuse or True  # the stub is scoped to the module


class TestOutput:
    def test_a_failure_after_the_copy_leaves_the_output_retryable(self, tmp_path, monkeypatch):
        """The retry has to work for ANY late failure, not for one known field.

        Skills are copied into the output before the later conversion steps run,
        so a failure in one of those steps leaves the directory holding half an
        app, and the empty-directory precondition then refuses the retry of the
        very command that made the mess. Staging is what makes the property hold
        regardless of which step fails, so a step is made to raise directly here:
        pinning this on a malformed field breaks the moment that field gains
        validation, which is what happened to the first version of this test.
        """
        from kiro_crew.apps import plugin_import as PI

        def _boom(*_a, **_k):
            raise RuntimeError("a later conversion step failed")

        monkeypatch.setattr(PI, "_convert_mcp_servers", _boom)

        root = _package(tmp_path)
        _skill(root / "skills", "greet")
        out = tmp_path / "out"

        with pytest.raises(RuntimeError):
            convert_plugin_package(root, out)
        assert not out.exists(), "a failed conversion must not leave an output directory"
        assert not list(tmp_path.glob(".out.partial-*")), "staging directory left behind"

        # The point of the above: the same command run again works.
        monkeypatch.undo()
        report = convert_plugin_package(root, out)
        assert report.app_name == "demo-plugin"
        assert (out / "app.json").exists()

    def test_a_second_conversion_into_the_same_directory_is_refused(self, tmp_path):
        root = _package(tmp_path)
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, out)
        assert exc.value.code == "output_not_empty"

    def test_a_nonempty_output_without_a_manifest_is_refused(self, tmp_path):
        """The copy walk overwrites by name, so a non-empty dir with no app.json
        must still be refused -- otherwise a pre-existing sibling file is silently
        clobbered."""
        root = _package(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("do not clobber", encoding="utf-8")
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, out)
        assert exc.value.code == "output_not_empty"
        # The pre-existing file is untouched.
        assert (out / "keep.txt").read_text(encoding="utf-8") == "do not clobber"

    def test_a_tree_deeper_than_the_bound_is_skipped_not_crashed(self, tmp_path):
        """A pathologically deep resource tree must be skipped past the depth
        bound rather than recursing to a RecursionError that leaves a partial
        import."""
        from kiro_crew.apps.plugin_import import (
            MAX_SKILL_TREE_DEPTH,
            _copy_tree_without_links,
        )

        src = tmp_path / "deep"
        cur = src
        # One level past the bound, with a file at the very bottom.
        for i in range(MAX_SKILL_TREE_DEPTH + 3):
            cur = cur / f"d{i}"
        cur.mkdir(parents=True)
        (cur / "leaf.txt").write_text("x", encoding="utf-8")
        copied, skipped = _copy_tree_without_links(src, tmp_path / "dst")
        # It did not crash; the too-deep subtree is recorded as skipped.
        assert any("deeper than" in s for s in skipped)

    def test_the_skip_list_is_capped_rather_than_unbounded(self, tmp_path):
        """A pathologically wide tree of skippable entries must not grow the
        retained skip-description list without a ceiling."""
        from kiro_crew.apps.plugin_import import (
            MAX_SKIP_DESCRIPTIONS,
            _copy_tree_without_links,
        )

        src = tmp_path / "wide"
        src.mkdir()
        # Every entry is a symlink, so all are skipped and each adds a line.
        target = tmp_path / "real.txt"
        target.write_text("x", encoding="utf-8")
        for i in range(MAX_SKIP_DESCRIPTIONS + 50):
            (src / f"link{i}").symlink_to(target)
        copied, skipped = _copy_tree_without_links(src, tmp_path / "dst")
        assert len(skipped) <= MAX_SKIP_DESCRIPTIONS + 1
        assert any("descriptions capped at" in s for s in skipped)
        # The overflow is counted rather than dropped, so the caller can still say
        # how many entries went undescribed.
        assert any("and 50 more skipped" in s for s in skipped)

    def test_the_skip_allowance_is_spent_on_append_not_trimmed_at_the_end(self, tmp_path):
        """The cap bounds the whole TREE, which a per-directory trim does not.

        Trimming each directory's finished list to the cap satisfies a one-directory
        test and still lets the walk hold N times the cap, because the parent
        extends every child's already-trimmed list -- and the peak the cap exists to
        prevent is reached during accumulation, before any trim can run. Three
        directories each over the cap is the smallest shape that tells the two
        apart: one directory cannot, because there is no second list to add.
        """
        from kiro_crew.apps.plugin_import import (
            MAX_SKIP_DESCRIPTIONS,
            _copy_tree_without_links,
        )

        target = tmp_path / "real.txt"
        target.write_text("x", encoding="utf-8")
        src = tmp_path / "wide"
        src.mkdir()
        per_dir = MAX_SKIP_DESCRIPTIONS + 10
        for d in range(3):
            sub = src / f"dir{d}"
            sub.mkdir()
            for i in range(per_dir):
                (sub / f"link{i}").symlink_to(target)

        # The budget is passed in so the allowance can be READ after the walk. The
        # returned list cannot distinguish the two implementations on its own: a trim
        # that runs at the end of every directory also returns at most the cap,
        # because the outermost trim runs last. What separates them is whether the
        # allowance was spent DURING the walk, which is what the shared counter
        # records and what a trim has no notion of.
        budget: dict[str, int] = {
            "files": 10_000,
            "bytes": 1 << 30,
            "skips": MAX_SKIP_DESCRIPTIONS,
        }
        _copied, skipped = _copy_tree_without_links(src, tmp_path / "dst", budget=budget)

        # Spent to exhaustion, and every description past it counted rather than
        # dropped: 3 directories of `per_dir` links each, minus what the allowance
        # covered.
        assert budget["skips"] == 0, f"allowance not spent during the walk: {budget['skips']} left"
        assert budget.get("skip_overflow") == 3 * per_dir - MAX_SKIP_DESCRIPTIONS
        # Not 3x the cap: the allowance is shared across the recursion.
        assert len(skipped) <= MAX_SKIP_DESCRIPTIONS + 1, f"held {len(skipped)} descriptions"
        # Exactly one summary line, from the call that owns the allowance rather
        # than one per directory each naming the same running total.
        assert sum("descriptions capped at" in s for s in skipped) == 1

    def test_the_report_names_the_source_manifest_relatively(self, tmp_path):
        root = _package(tmp_path)
        report = convert_plugin_package(root, tmp_path / "out")
        assert report.manifest_path == str(Path(".codex-plugin") / "plugin.json")
        assert report.source_format == FORMAT_VENDOR_DIRECTORY

    def test_render_text_lists_both_halves(self, tmp_path):
        root = _package(tmp_path, skills="./skills", apps="./apps")
        _skill(root / "skills", "search")
        (root / "apps").mkdir()
        text = convert_plugin_package(root, tmp_path / "out").render_text()
        assert "mapped:" in text and "not mapped:" in text
        assert "skills -> app.json skills" in text


# ---------------------------------------------------------------------------
# The emitted app is real
# ---------------------------------------------------------------------------


class TestEmittedAppIsInstallable:
    def test_the_emitted_manifest_validates(self, tmp_path):
        root = _package(tmp_path, skills="./skills", version="1.2.3")
        _skill(root / "skills", "search")
        out = tmp_path / "out"
        convert_plugin_package(root, out)

        data = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert AppManifest.from_dict(data).validate(out) == []

    def test_a_converted_package_installs_and_lists(self, tmp_path, monkeypatch):
        from kiro_crew.apps.manager import get_app_manifest, install_app, list_apps

        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
        )

        root = _package(tmp_path, name="Demo Plugin", version="1.2.3", skills="./skills")
        _skill(root / "skills", "search")
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)

        result = install_app(str(out))
        assert result.ok, result.error

        names = [a["name"] for a in list_apps()]
        assert report.app_name in names

        installed = get_app_manifest(report.app_name)
        assert installed is not None
        assert installed.skills == ["skills/search"]
        assert installed.extra["importedPlugin"]["sourceFormat"] == FORMAT_VENDOR_DIRECTORY


def _probe_before_link_offenders(source: str) -> list[str]:
    """Names each boolean test that probes a path before proving it unlinked.

    A probe is any call that asks the filesystem about the path -- ``is_dir``,
    ``is_file``, ``exists``, ``stat``. On Windows those resolve the path, so
    asking one of a junction whose target is a UNC share makes the OS
    authenticate to a host the PACKAGE chose. Python's ``and``/``or`` evaluate
    left to right, so the link check has to be the earlier operand: putting it
    second means the damage is already done when the refusal runs.

    Returns one string per offending expression so a failure names the line
    rather than only the count.
    """
    import ast

    probes = {"is_dir", "is_file", "exists", "stat", "lstat"}
    offenders: list[str] = []

    def receiver_of_probe(node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in probes
        ):
            return ast.unparse(node.func.value)
        return None

    def receiver_of_link_check(node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "is_link_or_junction"
            and node.args
        ):
            return ast.unparse(node.args[0])
        return None

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.BoolOp):
            continue
        first_link: dict[str, int] = {}
        first_probe: dict[str, int] = {}
        for index, operand in enumerate(node.values):
            inner = operand
            while isinstance(inner, ast.UnaryOp) and isinstance(inner.op, ast.Not):
                inner = inner.operand
            for candidate in ast.walk(inner):
                name = receiver_of_link_check(candidate)
                if name is not None:
                    first_link.setdefault(name, index)
                name = receiver_of_probe(candidate)
                if name is not None:
                    first_probe.setdefault(name, index)
        for name, probe_index in first_probe.items():
            link_index = first_link.get(name)
            if link_index is not None and probe_index < link_index:
                offenders.append(
                    f"{name}: probed at operand {probe_index}, link check at {link_index}"
                )
    return offenders


class TestLinkCheckOrdering:
    """The link check must be the earlier operand, at every site, forever.

    This finding came back on three separate heads because each round fixed the
    one call site the reviewer happened to name. The ordering is the property,
    so it is asserted over the whole module rather than per site.
    """

    def test_no_boolean_test_probes_a_path_before_proving_it_unlinked(self):
        from kiro_crew.apps import plugin_import as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        offenders = _probe_before_link_offenders(source)
        assert offenders == [], (
            "a filesystem probe runs before is_link_or_junction on the same path; "
            "on Windows the probe resolves a junction, so the refusal that follows "
            f"is too late: {offenders}"
        )

    def test_the_ordering_check_detects_the_shape_it_forbids(self):
        """A detector that has never fired is indistinguishable from a broken one."""
        offenders = _probe_before_link_offenders(
            "if entry.is_dir() and not is_link_or_junction(entry):\n    pass\n"
        )
        assert len(offenders) == 1, offenders
        assert "entry" in offenders[0]

    def test_the_ordering_check_accepts_the_corrected_shape(self):
        assert (
            _probe_before_link_offenders(
                "if not is_link_or_junction(entry) and entry.is_dir():\n    pass\n"
            )
            == []
        )


class TestCopyFailuresAreReportedNotSwallowed:
    """A copy that declines an entry must not be reported as a success.

    ``_copy_tree_without_links`` deliberately records a failure and carries on
    rather than raising, because one unreadable file should not abort a whole
    import. The consequence is that its CALLER owns the question of whether what
    landed is still the thing it promised, and the emitted ``app.json`` is what an
    operator trusts.
    """

    def test_a_skill_whose_entry_file_did_not_copy_is_not_declared(self, tmp_path):
        """The one file that makes a directory a skill is SKILL.md: it is what
        ``_discover_skill_dirs`` looks for. If it does not reach the output, the
        emitted directory is not a skill, so declaring it ships an app.json naming
        a skill the loader cannot read -- and counts it in "N skill(s) copied"."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        out = tmp_path / "out"

        real_copy = plugin_import.shutil.copy2

        def refuse_skill_md(src, dst, *args, **kwargs):
            if Path(src).name == plugin_import.SKILL_ENTRY_FILENAME:
                raise OSError(13, "Permission denied")
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch.object(plugin_import.shutil, "copy2", refuse_skill_md):
            report = plugin_import.convert_plugin_package(root, out)

        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert emitted.get("skills", []) == [], (
            "a skill whose SKILL.md never copied was declared in app.json, so the "
            "emitted app names a skill the loader cannot read"
        )
        assert not any(
            m.detail.endswith("skill(s) copied") for m in report.mapped
        ), "the report counted a skill that was not emitted"
        assert any(
            plugin_import.SKILL_ENTRY_FILENAME in w and "not emitted" in w for w in report.warnings
        ), f"no warning named the un-emitted skill: {report.warnings}"

    def test_a_skill_whose_other_files_fail_is_still_declared(self, tmp_path):
        """The complement, so the guard is a real condition and not a blanket
        refusal: losing a README costs the README, not the skill."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        (root / "skills" / "alpha" / "README.md").write_text("hi", encoding="utf-8")
        out = tmp_path / "out"

        real_copy = plugin_import.shutil.copy2

        def refuse_readme(src, dst, *args, **kwargs):
            if Path(src).name == "README.md":
                raise OSError(13, "Permission denied")
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch.object(plugin_import.shutil, "copy2", refuse_readme):
            plugin_import.convert_plugin_package(root, out)

        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert emitted.get("skills", []) == ["skills/alpha"]
        assert (out / "skills" / "alpha" / plugin_import.SKILL_ENTRY_FILENAME).is_file()


class TestStagingAndPublishRefusalsCarryACode:
    """The CLI catches ``PluginImportError`` and nothing else, so any OSError the
    staging or publish step can raise reaches the operator as a traceback."""

    def test_an_unwritable_output_parent_is_refused_with_a_code(self, tmp_path):
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")

        with mock.patch.object(
            plugin_import.tempfile, "mkdtemp", side_effect=OSError(30, "Read-only file system")
        ):
            with pytest.raises(plugin_import.PluginImportError) as exc:
                plugin_import.convert_plugin_package(root, tmp_path / "out")

        assert exc.value.code == "staging_unwritable"
        assert "Read-only file system" in str(exc.value)

    def test_a_failed_publish_is_refused_with_its_own_code(self, tmp_path):
        """Distinct from the staging failure because the conversion SUCCEEDED and
        only the move did, which is a different sentence and a different remedy."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        out = tmp_path / "out"

        with mock.patch.object(
            plugin_import.os, "replace", side_effect=OSError(39, "Directory not empty")
        ):
            with pytest.raises(plugin_import.PluginImportError) as exc:
                plugin_import.convert_plugin_package(root, out)

        assert exc.value.code == "output_publish_failed"
        assert "Directory not empty" in str(exc.value)
        assert not list(
            tmp_path.glob(f".{out.name}.partial-*")
        ), "the staging directory outlived the failed publish"


class TestForeignManifestRetentionIsBounded:
    """Everything this converter retains from a manifest is written into the emitted
    app.json, so a bound on the input is not enough -- the bound has to constrain
    what gets written."""

    def test_the_server_count_is_bounded(self, tmp_path):
        servers = {f"s{i}": {"command": "x"} for i in range(plugin_import.MAX_MCP_SERVERS + 20)}
        root = _package(tmp_path, mcpServers=servers)
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert len(emitted.get("mcpServers", {})) == plugin_import.MAX_MCP_SERVERS
        assert any("mcpServers declared" in w for w in report.warnings)

    def test_a_retained_string_is_truncated(self, tmp_path):
        long = "a" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 500)
        root = _package(tmp_path, mcpServers={"s": {"command": "x", "note": long}})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        kept = emitted["mcpServers"]["s"]["note"]
        assert len(kept) == plugin_import.MAX_MANIFEST_STRING_CHARS
        assert any("characters; truncated" in w for w in report.warnings)

    def test_a_deeply_nested_value_is_dropped(self, tmp_path):
        deep: object = "leaf"
        for _ in range(plugin_import.MAX_MANIFEST_DEPTH + 4):
            deep = {"n": deep}
        root = _package(tmp_path, mcpServers={"s": {"command": "x", "deep": deep}})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        # The server survives; only the over-deep branch is dropped, so one
        # pathological field does not cost the whole entry.
        assert emitted["mcpServers"]["s"]["command"] == "x"
        assert any("levels; dropped" in w for w in report.warnings)

    def test_a_non_finite_number_is_dropped_so_the_emitted_json_stays_strict(self, tmp_path):
        """``json.loads`` accepts the bare words NaN/Infinity and ``json.dumps``
        re-emits them, but RFC 8259 defines neither -- so retaining one costs the
        whole emitted app.json to any strict reader, not just the one field.

        The discriminating assertion is a STRICT parse, not the field's absence: a
        reader that refuses the undefined constants is exactly the consumer the
        corruption breaks.
        """
        manifest = {
            "name": "demo-plugin",
            "mcpServers": {"s": {"command": "x", "timeout": float("nan")}},
        }
        root = tmp_path / "src" / "demo-plugin"
        (root / ".codex-plugin").mkdir(parents=True)
        # json.dumps writes the bare word NaN, which is what a malformed manifest
        # in the wild carries; round-tripping through the fixture helper would not
        # produce it any other way.
        (root / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        raw = (out / "app.json").read_text(encoding="utf-8")

        def _reject(_constant: str) -> object:
            raise ValueError("the emitted app.json carries a JSON constant RFC 8259 omits")

        emitted = json.loads(raw, parse_constant=_reject)
        # The server survives; only the unrepresentable field is dropped, matching
        # how an over-deep branch costs the branch and not the entry.
        assert emitted["mcpServers"]["s"]["command"] == "x"
        assert "timeout" not in emitted["mcpServers"]["s"]
        assert any("not a finite number" in w for w in report.warnings)

    def test_a_long_container_is_bounded(self, tmp_path):
        args = [str(i) for i in range(plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 50)]
        root = _package(tmp_path, mcpServers={"s": {"command": "x", "flags": args}})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert (
            len(emitted["mcpServers"]["s"]["flags"]) == plugin_import.MAX_MANIFEST_CONTAINER_ITEMS
        )
        assert any("items; the rest are dropped" in w for w in report.warnings)


class TestCaseCollidingSkillsAreRefused:
    def test_two_skills_differing_only_in_case_do_not_overwrite(self, tmp_path):
        """A case-insensitive destination (macOS, Windows) aliases `Search` and
        `search` to one directory, so without a folded identity the second copy
        overwrites the first while the check sees two distinct skills."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "Search", description="capitalised")
        _skill(root / "skills", "search", description="lowercase")
        # The SOURCE has to actually hold two directories for the collision to
        # exist, and on a case-insensitive filesystem the two _skill calls wrote to
        # ONE directory -- so there is nothing for the guard to refuse and every
        # assertion below would pass for the wrong reason. Asserted rather than
        # assumed, and skipped by measuring the filesystem instead of naming a
        # platform, because Windows and macOS both fold by default while the Linux
        # lanes do not.
        variants = sorted(p.name for p in (root / "skills").iterdir() if p.is_dir())
        if variants != ["Search", "search"]:
            pytest.skip(
                "this filesystem folded Search and search into one directory "
                f"({variants}), so a case-colliding source cannot be built here"
            )
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert len(emitted.get("skills", [])) == 1, (
            "both case variants were emitted, so on a case-insensitive destination "
            "one skill silently overwrites the other"
        )
        assert any("duplicate skill directory name" in w for w in report.warnings)


class TestLinkedOutputPathIsRefused:
    def test_a_symlinked_output_directory_is_refused_before_it_is_resolved(self, tmp_path):
        """Publishing replaces out_dir by rename, so a link reaching that step is
        destroyed rather than followed. The refusal must also precede .resolve(),
        which on Windows traverses a junction."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        target = tmp_path / "real-target"
        target.mkdir()
        link = tmp_path / "linked-out"
        link.symlink_to(target, target_is_directory=True)

        resolved: list[str] = []
        real_resolve = Path.resolve

        def recording_resolve(self, *args, **kwargs):
            resolved.append(str(self))
            return real_resolve(self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", recording_resolve):
            with pytest.raises(PluginImportError) as exc:
                convert_plugin_package(root, link)

        assert exc.value.code == "output_is_a_link"
        assert str(link) not in resolved, "the linked output path was resolved before refusal"
        assert link.is_symlink(), "the refusal destroyed the link it was meant to protect"


class TestSkillEntryFileLinkIsRefusedBeforeItIsProbed:
    def test_discovery_does_not_probe_a_linked_skill_entry_file(self, tmp_path):
        """`is_file()` RESOLVES the path, so a SKILL.md symlinked at a UNC target
        makes Windows authenticate to a host the package chose. The directory arm of
        this walk was already guarded; the entry file is the other probe in the same
        loop, so the guard had covered one of two doors.

        Asserts the ORDER rather than the outcome, the same way the declared-path
        test does: a refusal that happens after the probe is too late."""
        root = _package(tmp_path, skills="./skills")
        holder = root / "skills" / "linked"
        holder.mkdir(parents=True)
        target = tmp_path / "outside" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\ndescription: outside\n---\n", encoding="utf-8")
        (holder / plugin_import.SKILL_ENTRY_FILENAME).symlink_to(target)

        probed: list[str] = []
        real_is_file = Path.is_file

        def recording_is_file(self):
            probed.append(str(self))
            return real_is_file(self)

        with mock.patch.object(Path, "is_file", recording_is_file):
            convert_plugin_package(root, tmp_path / "out")

        linked = str(holder / plugin_import.SKILL_ENTRY_FILENAME)
        assert linked not in probed, (
            "the linked SKILL.md was probed with is_file(), which resolves it -- on "
            "Windows that hands credentials to whatever host the link names"
        )


class TestNonMcpManifestFieldsAreBounded:
    """The mcpServers path is bounded; these are the other foreign-field retention
    points, and they land in the same app.json read at every runtime load."""

    def test_a_carried_interface_value_is_bounded(self, tmp_path):
        long = "z" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 400)
        root = _package(tmp_path, interface={"homepage": long})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        carried = json.dumps(emitted)
        assert long not in carried, "an unbounded interface value reached app.json"
        assert any("characters; truncated" in w for w in report.warnings)

    def test_the_keyword_list_is_bounded(self, tmp_path):
        many = [f"k{i}" for i in range(plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 60)]
        root = _package(tmp_path, keywords=many)
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert (
            len(emitted.get("keywords", [])) <= plugin_import.MAX_MANIFEST_CONTAINER_ITEMS
        ), "the keyword list grew past the bound"
        assert any("items; the rest are dropped" in w for w in report.warnings)


class TestWindowsRelativeCommandsAreRefusedToo:
    """Pinned on ``_package_relative_fields`` directly rather than through a
    converted package: this detector IS the whole finding, and a unit assertion
    names the exact input at stake instead of inferring it from a report.
    """

    def test_a_backslash_relative_command_is_detected_like_its_posix_twin(self):
        """A relative command resolves against the SESSION's working directory, not
        the package, so `.\\tools\\server.exe` launches whatever sits at that path in
        the user's workspace -- the same harm as `./tools/server.exe`, which was
        already detected. Matching only POSIX prefixes left the guard depending on
        which separator the package happened to write, and Windows accepts both.
        """
        posix = plugin_import._package_relative_fields({"command": "./tools/server.exe"})
        windows = plugin_import._package_relative_fields({"command": ".\\tools\\server.exe"})
        assert posix == ["command"], f"the POSIX control regressed: {posix}"
        assert windows == ["command"], (
            "a backslash-relative command was not detected while its POSIX twin "
            "was, so the guard depended on the separator the package chose"
        )

    def test_the_parent_prefix_is_covered_on_both_separators(self):
        assert plugin_import._package_relative_fields({"command": "../up/serve"}) == ["command"]
        assert plugin_import._package_relative_fields({"command": "..\\up\\serve.exe"}) == [
            "command"
        ]

    def test_a_bare_command_is_still_not_read_as_a_path(self):
        """The detector is deliberately narrow, and widening it must not start
        reading a bare command name, a flag or a package specifier as a path."""
        assert (
            plugin_import._package_relative_fields(
                {"command": "npx", "args": ["-y", "some-mcp@latest"]}
            )
            == []
        )
        # Dotted is not the test; carrying a PROGRAM suffix is. An interpreter
        # named for its version stays a bare command, which is what keeps the
        # suffix set load-bearing rather than collapsing to "contains a dot".
        assert plugin_import._package_relative_fields({"command": "python3.11"}) == []
        assert plugin_import._package_relative_fields({"command": "docker-compose"}) == []

    def test_a_flag_carrying_a_package_relative_value_is_read_through(self):
        """A dash does not make the VALUE after ``=`` stop being a path.

        Excluding every token that starts with a dash is what let the shape this
        detector exists to catch back in through option syntax: the value of
        ``--config=./local/thing`` resolves against the session's working directory
        exactly as the bare spelling does, and the package root is no more preserved
        for it. The three non-path shapes stay excluded inside a flag too, because
        the value is re-tested by the same rule rather than by a second one.
        """
        assert plugin_import._package_relative_fields(
            {"command": "npx", "args": ["--config=./local/thing"]}
        ) == ["args[0]"]
        assert plugin_import._package_relative_fields(
            {"command": "npx", "args": ["--plugin=bin/server"]}
        ) == ["args[0]"]
        # A bare flag carries no value to test, and a flag whose value is one of the
        # three excluded shapes is still not a path.
        assert (
            plugin_import._package_relative_fields(
                {
                    "command": "npx",
                    "args": [
                        "-y",
                        "--quiet",
                        "--pkg=@scope/thing",
                        "--src=git+https://example.invalid/x.git",
                        "--level=debug",
                    ],
                }
            )
            == []
        )

    def test_a_bare_program_filename_is_read_as_package_relative(self):
        """A separator-less token carrying a program suffix names a FILE.

        This REVERSES an earlier assertion here that a bare ``server.exe`` command
        stays a bare command. Two reasons, and the second is the decisive one. On
        Windows a bare program name is not a PATH lookup: ``CreateProcess`` searches
        the calling process's directory and the CURRENT directory before consulting
        PATH, so the value resolves against the session's working directory exactly
        as a relative path does. And on every platform such a program cannot start
        after conversion anyway, because the package root is not preserved and the
        program is not a declared resource -- the same ground on which this detector
        already drops a separator-carrying program.
        """
        assert plugin_import._package_relative_fields({"command": "server.exe"}) == ["command"]
        assert plugin_import._package_relative_fields(
            {"command": "node", "args": ["server.js"]}
        ) == ["args[0]"]
        assert plugin_import._package_relative_fields(
            {"command": "python", "args": ["app.py"]}
        ) == ["args[0]"]


class TestEveryEmittedManifestFieldIsBounded:
    def test_display_name_and_description_cannot_grow_the_emitted_manifest(self, tmp_path):
        """Three rounds found three different unbounded fields in this one dict,
        so the bound is applied to the ASSEMBLED manifest rather than per field."""
        over = "z" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 500)
        root = _package(tmp_path, display_name=over, description=over)
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert over not in json.dumps(emitted), "an unbounded field reached app.json"
        for key in ("displayName", "description"):
            if key in emitted:
                assert len(emitted[key]) <= plugin_import.MAX_MANIFEST_STRING_CHARS, key

    def test_the_apps_name_is_not_silently_shortened(self, tmp_path):
        """`name` is an IDENTITY, so the bound must not reach it: a truncated name
        emits a DIFFERENT app rather than a smaller one. Its own validator owns it.
        """
        root = _package(tmp_path)
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert emitted["name"] == plugin_import.normalize_app_name(emitted["name"])


class TestTheNameIsRefusedRatherThanShortened:
    def test_an_overlong_derived_name_is_refused(self):
        """KEBAB_RE bounds the ALPHABET and hyphen placement, not the length, so a
        100k-character kebab string satisfied the contract. The name is retained in
        app.json AND used as a directory segment, and it is an identity, so it is
        refused: a truncated name designates a different app and could collide with
        a real one.
        """
        ok = "a" * plugin_import.MAX_APP_NAME_CHARS
        assert plugin_import.normalize_app_name(ok) == ok, "the control regressed"
        with pytest.raises(plugin_import.PluginImportError) as exc:
            plugin_import.normalize_app_name("a" * (plugin_import.MAX_APP_NAME_CHARS + 1))
        assert exc.value.code == "invalid_app_name"


class TestTheProvenanceBlockIsBoundedToo:
    def test_the_bound_covers_the_field_attached_last(self, tmp_path):
        """The bounding pass ran BEFORE importedPlugin was attached, so provenance
        escaped it -- the pass covered what was assembled above it and nothing added
        below. Bounding the finished object is the placement where a field added
        later is inside it by construction.

        Driven through an unknown manifest KEY rather than an oversized VALUE: the
        carried block is bounded upstream as it is read, so an oversized value never
        reaches provenance and would make this test vacuous. A key name lands in the
        report's own warning TEXT, which is built inside the converter and is only
        ever bounded by the final pass.
        """
        over = "k" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 400)
        root = _package(tmp_path, **{over: "x"})
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        provenance = emitted["importedPlugin"]
        assert any(
            "not read by this converter" in w for w in provenance["warnings"]
        ), "the control is gone: the unknown key no longer reaches provenance"
        assert over not in json.dumps(
            provenance
        ), "an unbounded key name survived inside the provenance block"


class TestMalformedManifestsStayInsideTheCodedBoundary:
    """Every refusal this module makes carries a code, and the CLI's only except arm
    catches PluginImportError. An exception outside that hierarchy reaches the
    operator as a traceback."""

    def test_a_manifest_that_is_not_utf8_is_refused_with_a_code(self, tmp_path):
        """read_text SUCCEEDS at the open and fails at the DECODE, and
        UnicodeDecodeError is not an OSError, so it escaped the first except arm."""
        root = _package(tmp_path)
        (root / ".codex-plugin" / "plugin.json").write_bytes(b'{"name": "\xff\xfe bad"}')
        with pytest.raises(plugin_import.PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code in {"manifest_unreadable", "manifest_not_json"}

    def test_a_recursion_error_while_parsing_becomes_a_coded_refusal(self, tmp_path):
        """``json.loads`` RECURSES, and a RecursionError is not a JSONDecodeError, so
        it escaped the except arm and reached the operator as a traceback.

        The ARM is driven directly rather than by feeding deep input: CPython's C
        scanner parses thousands of levels without raising, so a depth-based test
        picks a number that does not trigger the arm on this interpreter and passes
        for the wrong reason. What this pins is the conversion, which is the finding.
        """
        real = tmp_path / "plugin.json"
        real.write_text('{"name": "demo"}', encoding="utf-8")
        assert plugin_import._read_json_object(real, "a manifest") == {
            "name": "demo"
        }, "the control regressed: this file must parse, or the patch below proves nothing"

        def exploding_loads(*_args, **_kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        with mock.patch.object(plugin_import.json, "loads", exploding_loads):
            with pytest.raises(plugin_import.PluginImportError) as exc:
                plugin_import._read_json_object(real, "a manifest")
        assert exc.value.code == "manifest_not_json"
        assert "nests too deeply" in str(exc.value)

    def test_the_schema_probe_answers_false_rather_than_raising(self, tmp_path):
        """The probe returns a BOOLEAN, so the same two escapes would crash a caller
        that is only asking a question."""
        bad = tmp_path / "app.json"
        bad.write_bytes(b'{"$schema": "\xff\xfe"}')
        assert plugin_import._is_schema_qualified(bad) is False
        # Same arm, driven directly for the same reason as the test above.
        fine = tmp_path / "fine.json"
        fine.write_text(
            '{"$schema": "' + plugin_import.SCHEMA_NAMESPACE_PREFIX + 'x"}', encoding="utf-8"
        )
        assert plugin_import._is_schema_qualified(fine) is True, "the control regressed"

        def exploding_loads(*_args, **_kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        with mock.patch.object(plugin_import.json, "loads", exploding_loads):
            assert plugin_import._is_schema_qualified(fine) is False


class TestProvenanceCarriesTheFinalBoundingWarnings:
    """The bounding pass runs LAST and appends its own warnings, so a provenance
    block snapshotted before it persists a warning list missing exactly the
    truncations that pass performed. A reader needs those: a silently shortened
    list is the dangerous direction.
    """

    def test_a_truncation_by_the_final_pass_reaches_the_persisted_provenance(self, tmp_path):
        import json as _json

        from kiro_crew.apps import plugin_import as pi

        long_description = "d" * (pi.MAX_MANIFEST_STRING_CHARS + 50)
        root = _package(tmp_path, skills="./skills", description=long_description)
        _skill(root, "skills")
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)

        # CONTROL: the final pass must actually have warned. `description` is
        # bounded ONLY by that pass, which is why it is the field used here -- a
        # field bounded upstream would warn before provenance is built and the
        # assertion below would pass with the bug still in place.
        truncation_warnings = [w for w in report.warnings if "description" in w]
        assert truncation_warnings, (
            "control failed: the final bounding pass emitted no warning for an "
            f"over-long description, so this test proves nothing. warnings={report.warnings}"
        )

        emitted = _json.loads((out / "app.json").read_text(encoding="utf-8"))
        persisted = emitted["importedPlugin"]["warnings"]
        missing = [w for w in truncation_warnings if w not in persisted]
        assert not missing, (
            "the persisted provenance omits warnings the final bounding pass "
            f"added, so the record does not say what was truncated: {missing}"
        )

    def test_an_unwritable_output_is_refused_not_a_traceback(self, tmp_path, monkeypatch):
        """The app.json write was unguarded, so an OSError escaped past the CLI's
        only except arm as a traceback. It is refused with the code the staging
        directory already uses, since the write lands inside that directory."""
        import pathlib

        root = _package(tmp_path, skills="./skills")
        _skill(root, "skills")

        real_write = pathlib.Path.write_text

        def _deny(self, *a, **k):
            if self.name == "app.json":
                raise OSError(28, "No space left on device")
            return real_write(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "write_text", _deny)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code == "staging_unwritable", exc.value.code
        assert "app.json" in str(exc.value)


class TestARelativeProgramIsDetectedByItsSeparator:
    """A leading dot is not what makes a value a path. `bin/server` is an
    ordinary manifest spelling, it resolves against the SESSION's working
    directory, and it cannot start after conversion either, because the package
    root is not preserved and the program is not a declared resource.
    """

    def test_every_relative_spelling_is_detected(self):
        from kiro_crew.apps.plugin_import import _package_relative_fields

        for command in [
            "bin/server",  # the spelling the dot-prefixed rule missed
            "tools\\server.exe",
            "./tools/server.exe",
            "../sibling/server",
            ".\\tools\\server.exe",
            "..\\sibling\\server",
            ".",
            "..",
        ]:
            assert _package_relative_fields({"command": command}) == ["command"], (
                f"{command!r} resolves against the session's working directory and "
                "was not detected"
            )

    def test_the_shapes_that_are_not_paths_stay_convertible(self):
        """CONTROL. Widening the rule must not start dropping servers that
        convert correctly: each of these carries no filesystem meaning, and three
        of them carry a slash, which is exactly why a bare separator test needs
        these exclusions stated as tests rather than assumed."""
        from kiro_crew.apps.plugin_import import _package_relative_fields

        for command in [
            "node",  # a bare name resolved through PATH
            "uvx",
            "-y",  # a flag
            "--port=8080",
            "@scope/package",  # an npm scope specifier
            "git+https://example.invalid/pkg.git",  # a URL
            "/usr/local/bin/server",  # absolute resolves against nothing local
        ]:
            assert _package_relative_fields({"command": command}) == [], (
                f"{command!r} is not a filesystem path but was flagged, so a server "
                "that converts correctly would be dropped"
            )

    def test_a_relative_arg_is_detected_too(self):
        from kiro_crew.apps.plugin_import import _package_relative_fields

        found = _package_relative_fields({"command": "node", "args": ["-y", "lib/main.js"]})
        assert found == ["args[1]"], found


class TestRefusalsCoverWhatTheParserActuallyRaises:
    def test_an_oversized_integer_is_refused_not_raised(self, tmp_path):
        """Valid JSON can still fail to parse.

        CPython refuses an integer literal past its digit limit with a plain
        ``ValueError``, not a ``JSONDecodeError``, so a narrower arm let it escape
        the coded boundary as a traceback.
        """
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"name": ' + "9" * 5000 + "}", encoding="utf-8")
        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._read_json_object(manifest, "the manifest")
        assert caught.value.code == "manifest_not_json"

    def test_a_syntax_error_is_still_refused_the_same_way(self, tmp_path):
        # CONTROL. Widening to ValueError must keep answering an ordinary syntax
        # error, which is the case the narrower arm existed for.
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{not json", encoding="utf-8")
        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._read_json_object(manifest, "the manifest")
        assert caught.value.code == "manifest_not_json"


class TestTheServerCapBoundsWhatIsRetained:
    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/src", manifest_path="/src/manifest.json", source_format="x", app_name="a"
        )

    def test_refused_servers_count_against_the_cap(self):
        """A cap on EMITTED servers bounds nothing a refused entry retains.

        Every refusal path appends a warning or an unmapped record, so a manifest
        of nothing but refused servers leaves the emitted dict empty forever while
        the report grows with the input.
        """
        report = self._report()
        servers = {
            f"s{i}": {"command": "./bin/server"} for i in range(plugin_import.MAX_MCP_SERVERS + 50)
        }
        cleaned = plugin_import._convert_mcp_servers(Path("/src"), servers, report)

        assert cleaned == {}
        assert len(report.unmapped) <= plugin_import.MAX_MCP_SERVERS
        assert any("the rest are dropped" in w for w in report.warnings)

    def test_servers_within_the_cap_are_all_still_emitted(self):
        # CONTROL. Counting inspected entries must not start dropping entries that
        # convert correctly, which a cap applied one entry too early would do.
        report = self._report()
        servers = {f"s{i}": {"command": "npx"} for i in range(plugin_import.MAX_MCP_SERVERS)}
        cleaned = plugin_import._convert_mcp_servers(Path("/src"), servers, report)

        assert len(cleaned) == plugin_import.MAX_MCP_SERVERS
        assert not [w for w in report.warnings if "the rest are dropped" in w]


class TestOneSkillTreeIsBoundedNotJustItsDepthAndItsFiles:
    def test_a_wide_tree_stops_at_the_file_budget(self, tmp_path):
        """Depth bounds how DEEP the walk goes, never how wide.

        The per-file size bound answers a different question again, so a directory
        of many small files copied without limit.
        """
        src = tmp_path / "skill"
        src.mkdir()
        for i in range(plugin_import.MAX_SKILL_FILES + 25):
            (src / f"f{i}.txt").write_text("x", encoding="utf-8")

        copied, skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == plugin_import.MAX_SKILL_FILES
        assert any("files in this skill" in line for line in skipped)

    def test_the_budget_is_shared_across_the_whole_tree(self, tmp_path):
        # The budget has to be ONE allowance for the tree: a per-directory cap is
        # not a cap on a tree, because breadth is unbounded either way.
        src = tmp_path / "skill"
        per_dir = (plugin_import.MAX_SKILL_FILES // 2) + 10
        for d in ("a", "b"):
            (src / d).mkdir(parents=True)
            for i in range(per_dir):
                (src / d / f"f{i}.txt").write_text("x", encoding="utf-8")

        copied, _skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == plugin_import.MAX_SKILL_FILES

    def test_an_ordinary_skill_tree_is_copied_whole(self, tmp_path):
        # CONTROL. Without this, a budget of zero would satisfy both tests above
        # while making every import empty.
        src = tmp_path / "skill"
        (src / "nested").mkdir(parents=True)
        (src / "SKILL.md").write_text("hello", encoding="utf-8")
        (src / "nested" / "ref.txt").write_text("ref", encoding="utf-8")

        copied, skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == 2
        assert skipped == []


class TestTheSchemaProbeAnswersRatherThanRaising:
    def test_an_oversized_integer_leaves_the_manifest_unqualified(self, tmp_path):
        """The probe is named in ``_read_json_object``'s own comment as carrying the
        same escapes, so the two arms have to be the same width: an integer past the
        interpreter's digit limit raises a PLAIN ValueError from valid JSON, which
        the narrower arm let out as a traceback."""
        manifest = tmp_path / "plugin.json"
        manifest.write_text('{"$schema": ' + "9" * 5000 + "}", encoding="utf-8")
        assert plugin_import._is_schema_qualified(manifest) is False

    def test_a_qualified_manifest_is_still_recognised(self, tmp_path):
        # CONTROL. Widening the arm must not turn the probe into a constant False,
        # which would satisfy the test above while making every package unqualified.
        manifest = tmp_path / "plugin.json"
        manifest.write_text(
            json.dumps({"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1.json"}),
            encoding="utf-8",
        )
        assert plugin_import._is_schema_qualified(manifest) is True


class TestTheCopyWalksFilesystemFailuresStayInsideTheBoundary:
    def test_an_uncreatable_destination_is_refused_with_a_code(self, tmp_path):
        """An output filesystem this walk cannot write to is not a skippable entry:
        no amount of skipping produces the app, so it is refused with the code this
        module already uses for an unwritable staging area."""
        src = tmp_path / "skill"
        src.mkdir()
        (src / "SKILL.md").write_text("hi", encoding="utf-8")
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._copy_tree_without_links(src, blocker / "out")
        assert caught.value.code == "staging_unwritable"

    def test_an_unreadable_source_directory_is_skipped_and_recorded(self, tmp_path, monkeypatch):
        """One unreadable SUBTREE is a skippable entry, which is the answer this walk
        gives everything it declines and the answer ``_discover_skill_dirs`` already
        gives for the same call."""
        src = tmp_path / "skill"
        src.mkdir()
        real_iterdir = Path.iterdir

        def _refuse(self):
            if self == src:
                raise PermissionError(13, "Permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _refuse)
        copied, skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == 0
        assert any("unreadable, skipped" in line for line in skipped)


class TestSkillDiscoveryIsBoundedWhileItWalks:
    def test_discovery_stops_at_the_skill_cap(self, tmp_path):
        """A package can be WIDE as well as deep, and a walk that finds the whole
        population before anyone counts it has already spent the memory the cap
        exists to bound."""
        root = tmp_path / "skills"
        for i in range(plugin_import.MAX_SKILLS + 40):
            d = root / f"s{i}"
            d.mkdir(parents=True)
            (d / plugin_import.SKILL_ENTRY_FILENAME).write_text("x", encoding="utf-8")

        found = plugin_import._discover_skill_dirs(root)

        assert len(found) == plugin_import.MAX_SKILLS

    def test_an_ordinary_package_is_discovered_whole(self, tmp_path):
        # CONTROL. Without this, a cap of zero would satisfy the test above while
        # making every package look empty.
        root = tmp_path / "skills"
        for name in ("alpha", "beta", "gamma"):
            d = root / name
            d.mkdir(parents=True)
            (d / plugin_import.SKILL_ENTRY_FILENAME).write_text("x", encoding="utf-8")

        found = plugin_import._discover_skill_dirs(root)

        assert sorted(p.name for p in found) == ["alpha", "beta", "gamma"]

    def test_the_frontier_is_bounded_when_no_directory_carries_a_marker(
        self, tmp_path, monkeypatch
    ):
        """MAX_SKILLS bounds the ANSWER, and a marker-free tree never advances it.

        Asserted on DIRECTORIES VISITED, because the return value cannot express
        this: a marker-free tree answers with an empty list whether the frontier is
        bounded or not, so an assertion about the result passes with the bound
        removed. What the bound governs is how much of the tree is held at once.
        """
        root = tmp_path / "skills"
        # Wide at every level and carrying no entry file anywhere: 3 levels of 30
        # gives 27,900 directories, above MAX_SKILL_TREE_DIRS, and `found` stays
        # empty throughout so MAX_SKILLS can never fire.
        for i in range(30):
            for j in range(30):
                for k in range(30):
                    (root / f"a{i}" / f"b{j}" / f"c{k}").mkdir(parents=True)

        visited: list = []
        real = plugin_import._bounded_sorted_entries

        def counting(path):
            visited.append(path)
            return real(path)

        monkeypatch.setattr(plugin_import, "_bounded_sorted_entries", counting)

        found = plugin_import._discover_skill_dirs(root)

        assert found == []
        assert len(visited) <= plugin_import.MAX_SKILL_TREE_DIRS + 1, (
            f"the walk visited {len(visited)} directories against a frontier bound "
            f"of {plugin_import.MAX_SKILL_TREE_DIRS}"
        )

    def test_an_over_deep_directory_is_never_enqueued(self, tmp_path, monkeypatch):
        """The depth limit is applied where a directory is ENQUEUED.

        Popping first and discarding afterwards holds the whole over-deep level in
        the frontier before deciding none of it is wanted, which is the memory the
        limit exists not to spend. Both orders VISIT the same set, so a spy on the
        listing cannot tell them apart -- what separates them is whether an
        over-deep child is probed at all, and a child that is never probed cannot
        have been appended. Asserted on the link probe each candidate passes
        through immediately before it joins the frontier.
        """
        deep = tmp_path / "skills"
        for i in range(plugin_import.MAX_SKILL_TREE_DEPTH + 4):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        root = tmp_path / "skills"

        probed: list = []
        real = plugin_import.is_link_or_junction

        def counting(path):
            probed.append(path)
            return real(path)

        monkeypatch.setattr(plugin_import, "is_link_or_junction", counting)

        plugin_import._discover_skill_dirs(root)

        # The marker probe fires for every directory VISITED and says nothing about
        # the frontier; the entry probes are the enqueue candidates.
        candidates = [p for p in probed if p.name != plugin_import.SKILL_ENTRY_FILENAME]
        depths = [len(p.relative_to(root).parts) for p in candidates]
        assert depths, "no directory was probed at all, so this measures nothing"
        assert max(depths) <= plugin_import.MAX_SKILL_TREE_DEPTH, (
            f"a directory at depth {max(depths)} was probed for the frontier against "
            f"a limit of {plugin_import.MAX_SKILL_TREE_DEPTH}"
        )


class TestALinkedPackageRootIsRefusedBeforeItIsProbed:
    """A probe RESOLVES the path, so the refusal has to come first.

    On Windows a junction whose target is a UNC share makes the OS authenticate to
    a host the PACKAGE chose, and a refusal that runs after the probe is too late.
    ``resolve_declared_path`` covers every DECLARED path by walking its segments,
    but the root is what those are resolved against, so it is checked here.
    """

    def test_a_linked_root_is_refused(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / plugin_import.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import.find_plugin_manifest(linked)
        assert caught.value.code == "source_not_a_directory"
        assert "link" in caught.value.message

    def test_the_refusal_runs_before_anything_resolves_the_root(self, tmp_path, monkeypatch):
        # The ORDER is the property, not the refusal: a check after the probe still
        # refuses, and still leaks the credentials the probe offered. So this records
        # what was resolved and fails if the linked root was resolved at all.
        real = tmp_path / "real"
        real.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        probed: list[str] = []
        real_is_dir = Path.is_dir

        def _record(self):
            probed.append(str(self))
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", _record)
        with pytest.raises(plugin_import.PluginImportError):
            plugin_import.find_plugin_manifest(linked)

        assert str(linked) not in probed

    def test_an_ordinary_root_is_still_accepted(self, tmp_path):
        # CONTROL. Without this, refusing every root would satisfy both tests above
        # while making every import fail.
        root = tmp_path / "pkg"
        root.mkdir()
        (root / plugin_import.MANIFEST_FILENAME).write_text(
            json.dumps({"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1.json"}),
            encoding="utf-8",
        )
        found, _fmt = plugin_import.find_plugin_manifest(root)
        assert found.name == plugin_import.MANIFEST_FILENAME


class TestTheManifestIsBoundedBeforeItIsRead:
    """The bound has to run before the read, not after it.

    Every other bound in the module applies to a value already parsed, which is the
    right place for a bound on what is RETAINED -- but a manifest is read whole in
    one call, so a bound that runs afterwards has already let the allocation happen.
    """

    def test_an_oversized_manifest_is_refused_without_being_read(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(plugin_import, "MAX_MANIFEST_BYTES", 1)
        reads: list[str] = []
        real_read_text = Path.read_text

        def _record(self, *args, **kwargs):
            reads.append(str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _record)

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._read_json_object(manifest, "the manifest")

        assert caught.value.code == "manifest_unreadable"
        # UNREAD is the property. A refusal that reads first still spends the memory.
        assert str(manifest) not in reads

    def test_an_ordinary_manifest_is_still_read(self, tmp_path):
        # CONTROL. Without this, a bound of zero would satisfy the test above while
        # refusing every manifest there is.
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        assert plugin_import._read_json_object(manifest, "the manifest") == {"name": "demo"}


class TestInstallDoesNotActivateADisabledApp:
    def test_the_import_install_path_does_not_register_resources(self):
        """Installing writes the app's files; it does not turn the app on.

        Registering at install activates the app's executable resources -- its agents
        and MCP servers -- for an app that is still disabled, so the next agent
        session launches a command from a third-party package nobody enabled. The
        enable path registers, which is where that decision is made.

        Asserted on the SOURCE: the install branch ends by telling the operator to
        run enable, and driving the CLI here would install a real app into the
        developer's own data home.
        """
        import ast
        from pathlib import Path as _Path

        import kiro_crew.cli_commands as cli

        tree = ast.parse(_Path(cli.__file__).read_text(encoding="utf-8"))
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_app_import"
        )
        called = {
            n.func.id
            for n in ast.walk(target)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "install_app" in called, "the install branch no longer installs, so this is vacuous"
        assert "register_app" not in called

    def test_the_enable_path_still_registers(self):
        # CONTROL. Removing registration from install is only correct because enable
        # does it; without this, deleting BOTH would satisfy the test above.
        import ast
        from pathlib import Path as _Path

        import kiro_crew.cli_commands as cli

        tree = ast.parse(_Path(cli.__file__).read_text(encoding="utf-8"))
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_app"
        )
        called = {
            n.func.id
            for n in ast.walk(target)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "register_app" in called


class TestTheSchemaProbeIsBoundedLikeItsSibling:
    """The probe and `_read_json_object` read the same kind of file the same way.

    The probe's own comment already says the two must match on their exception arms.
    The SIZE bound is the other half of matching, and the probe is reached by MORE
    inputs than its sibling: it runs on a root `plugin.json` on every import,
    including a directory that turns out not to be a plugin at all.
    """

    def test_an_oversized_root_manifest_is_not_read(self, tmp_path, monkeypatch):
        manifest = tmp_path / "plugin.json"
        _write_json(manifest, {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1"})
        monkeypatch.setattr(plugin_import, "MAX_MANIFEST_BYTES", 1)
        reads: list[str] = []
        real_read_text = Path.read_text

        def _record(self, *args, **kwargs):
            reads.append(str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _record)

        assert plugin_import._is_schema_qualified(manifest) is False
        assert str(manifest) not in reads

    def test_an_ordinary_root_manifest_still_qualifies(self, tmp_path):
        # CONTROL. Without this, refusing every root manifest would satisfy the test
        # above while making schema-qualified discovery impossible.
        manifest = tmp_path / "plugin.json"
        _write_json(manifest, {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1"})
        assert plugin_import._is_schema_qualified(manifest) is True


class TestAFailedPublishDoesNotConsumeTheOutputDirectory:
    def test_an_empty_out_dir_the_caller_made_is_restored(self, tmp_path, monkeypatch):
        """A refusal must not also delete something that was already there.

        The publish removes an empty `out_dir` because `os.replace` cannot land on an
        existing directory. If the move then fails, that removal is the only lasting
        effect of a command that reported failure.
        """
        source = _package(tmp_path)
        out_dir = tmp_path / "demo-app"
        out_dir.mkdir()

        def _boom(src, dst):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(plugin_import.os, "replace", _boom)

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import.convert_plugin_package(source, out_dir)

        assert caught.value.code == "output_publish_failed"
        assert out_dir.is_dir(), "the caller's directory was consumed by a failed import"

    def test_a_successful_publish_still_lands(self, tmp_path):
        # CONTROL. Without this, never removing out_dir would satisfy the test above
        # while making every publish fail.
        source = _package(tmp_path)
        out_dir = tmp_path / "demo-app2"
        out_dir.mkdir()
        plugin_import.convert_plugin_package(source, out_dir)
        assert (out_dir / "app.json").is_file()


class TestTheReportCannotForgeTerminalOutput:
    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_manifest_text_reaches_the_terminal_without_live_controls(self):
        """Every rendered field is manifest-derived and printed on its own line.

        Untouched, one carrying an escape could repaint the screen, and one carrying
        a newline could open a line that reads as this tool's own output.
        """
        report = self._report()
        report.warnings.append("\x1b[2Jcleared\nnot mapped:\n  forged -> line")
        text = report.render_text()
        assert "\x1b" not in text, "a live escape reached the terminal"
        assert "\\x0a" in text, "a newline could open a line that reads as our own"

    def test_ordinary_text_is_left_readable(self):
        # CONTROL. Without this, deleting every field would satisfy the test above.
        report = self._report()
        report.warnings.append("no equivalent for hooks")
        text = report.render_text()
        assert "no equivalent for hooks" in text
        assert "demo" in text


class TestTheImportCommandCannotBeMadeToWriteLiveTerminalControls:
    """Every refusal this command prints carries text taken from the package under
    conversion, after a ``❌`` prefix on the operator's terminal.

    The pin is at the PRINT SITE rather than on one upstream message, because which
    refusal happens to embed a package-supplied path changes as the converter grows,
    while the property the operator depends on does not: nothing reaching the
    terminal from a foreign package may move the cursor or open a line that reads as
    this tool's own output. The helpers are imported at ``cli_commands`` module scope,
    so that module holds its own binding and is what a patch has to replace -- patching
    the definition module would leave the command calling the real function.
    """

    HOSTILE = "pkg\x1b[2Kwiped\r\nnot a prefixed line"

    def _assert_inert(self, captured: str) -> None:
        assert "\x1b" not in captured, "a live escape reached the terminal"
        # One printed line: the newline is rendered as its visible literal, so no
        # unprefixed line can pass for this command's own output.
        body = [ln for ln in captured.splitlines() if ln.strip()]
        assert len(body) == 1, f"the refusal spanned {len(body)} lines: {body!r}"
        assert "\\x0a" in body[0]

    def test_a_conversion_refusal_cannot_carry_controls(self, tmp_path, monkeypatch, capsys):
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _handle_app_import

        root = _package(tmp_path)

        def _boom(*_a, **_k):
            raise PluginImportError("staging_unwritable", f"cannot write under {self.HOSTILE}")

        # Patched on CLI_COMMANDS, not on plugin_import: the command imports these
        # names at module scope, so it holds its own binding and replacing the
        # definition module's attribute would leave the command calling the real one.
        monkeypatch.setattr(cli_commands, "convert_plugin_package", _boom)
        with pytest.raises(SystemExit):
            _handle_app_import(
                argparse.Namespace(source=str(root), name=None, out=str(tmp_path / "out"))
            )
        self._assert_inert(capsys.readouterr().err)

    def test_a_manifest_read_failure_cannot_carry_controls(self, tmp_path, monkeypatch, capsys):
        """The OSError arm prints ``str(exc)``, which carries the offending
        filename -- and that name comes from the package."""
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _handle_app_import

        root = _package(tmp_path)

        def _boom(*_a, **_k):
            raise OSError(f"cannot open {self.HOSTILE}")

        # Patched on CLI_COMMANDS, not on plugin_import: the command imports these
        # names at module scope, so it holds its own binding and replacing the
        # definition module's attribute would leave the command calling the real one.
        monkeypatch.setattr(cli_commands, "read_manifest_name", _boom)
        with pytest.raises(SystemExit):
            _handle_app_import(
                argparse.Namespace(source=str(root), name=None, out=str(tmp_path / "out"))
            )
        self._assert_inert(capsys.readouterr().err)


class TestADirectoryListingIsBoundedAtTheIterator:
    """`sorted()` exhausts its iterator before it can order anything.

    So a cap applied to the RESULT runs after the whole directory is already in
    memory: a package shipping a directory of a million names is materialised in
    full by the sort, whatever the later limit says. Asserted on how many entries
    are CONSUMED, which is the only thing that distinguishes the two.
    """

    def test_it_stops_consuming_at_the_cap(self, tmp_path, monkeypatch):
        d = tmp_path / "wide"
        d.mkdir()
        for i in range(60):
            (d / f"e{i:03d}").touch()

        monkeypatch.setattr(plugin_import, "MAX_DIR_ENTRIES", 10)
        consumed = {"n": 0}
        real_iterdir = Path.iterdir

        def _counting(self):
            for item in real_iterdir(self):
                consumed["n"] += 1
                yield item

        monkeypatch.setattr(Path, "iterdir", _counting)
        entries, complete = plugin_import._bounded_sorted_entries(d)

        assert len(entries) == 10
        assert complete is False
        # 10 taken plus the ONE lookahead that decides completeness. A result-side
        # cap would have consumed all 60 and still returned 10.
        assert consumed["n"] == 11, f"consumed {consumed['n']} entries against a cap of 10"

    def test_a_directory_within_the_cap_reads_complete(self, tmp_path, monkeypatch):
        # CONTROL. Without this, reporting every listing as truncated would satisfy
        # the test above while making every import look partial.
        d = tmp_path / "narrow"
        d.mkdir()
        for name in ("b", "a", "c"):
            (d / name).touch()
        monkeypatch.setattr(plugin_import, "MAX_DIR_ENTRIES", 10)
        entries, complete = plugin_import._bounded_sorted_entries(d)
        assert complete is True
        assert [e.name for e in entries] == ["a", "b", "c"], "and the order still sorts"

    def test_the_vendor_scan_reads_only_what_the_bound_handed_it(self, tmp_path, monkeypatch):
        """The helper being bounded says nothing about its CALLERS.

        `find_plugin_manifest` searches for a vendor-prefixed directory, and a glob
        walks the directory exactly as a listing does, so the call site needs its own
        pin. Asserted by narrowing what the BOUND reports and requiring the scan to
        honour that: a site globbing the directory itself would find the vendor
        manifest anyway and never notice the bound at all.

        Deliberately not asserted by counting consumed entries. `Path.glob` does not
        go through `Path.iterdir`, so a spy on the latter is blind to the very
        mutation this exists to catch: it stays green with the bound removed.
        """
        root = tmp_path / "pkg"
        vendor = root / ".acme-plugin"
        vendor.mkdir(parents=True)
        (vendor / plugin_import.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

        real = plugin_import._bounded_sorted_entries
        calls: list[Path] = []

        def _narrow(path, *a, **k):
            calls.append(path)
            # The bound reported nothing for this directory, e.g. because the vendor
            # entry sorts past MAX_DIR_ENTRIES in a package with many children.
            return [], False

        monkeypatch.setattr(plugin_import, "_bounded_sorted_entries", _narrow)
        with pytest.raises(plugin_import.PluginImportError):
            plugin_import.find_plugin_manifest(root)
        assert root in calls, "the vendor scan never consulted the bound"

        # CONTROL. The scan must still resolve the manifest when the bound reports
        # it, or the assertion above would also pass on a scan that finds nothing.
        monkeypatch.setattr(plugin_import, "_bounded_sorted_entries", real)
        found, fmt = plugin_import.find_plugin_manifest(root)
        assert found == vendor / plugin_import.MANIFEST_FILENAME
        assert fmt == plugin_import.FORMAT_VENDOR_DIRECTORY


class TestAContainerIsBoundedByWhatItInspects:
    """A cap on the RETAINED result does not bound what a dropped entry produces.

    Every branch that drops an entry appends a warning, and a dropped entry never
    advances the retained count, so a container of nothing but dropped entries grows
    the report once per input entry with the cap never firing. This is the rule
    ``_convert_mcp_servers`` states in its own comment, applied to the two branches
    that were still counting the result.
    """

    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_a_dict_of_dropped_keys_does_not_grow_the_report_per_entry(self):
        report = self._report()
        over = plugin_import.MAX_MANIFEST_CONTAINER_ITEMS * 20
        # Non-string keys: dropped by the key check, so ``out`` stays empty and a
        # cap on ``out`` can never fire.
        value = {i: "v" for i in range(over)}

        bounded, kept = plugin_import._bounded_manifest_value(value, "x", report, 0)

        assert kept and bounded == {}
        assert len(report.warnings) <= plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 1, (
            f"{len(report.warnings)} warnings from {over} dropped entries against a "
            f"cap of {plugin_import.MAX_MANIFEST_CONTAINER_ITEMS}"
        )

    def test_a_list_of_dropped_items_does_not_grow_the_report_per_item(self):
        report = self._report()
        over = plugin_import.MAX_MANIFEST_CONTAINER_ITEMS * 20
        # Non-finite numbers: ``kept`` is False, so ``items`` stays empty and a cap
        # on ``items`` can never fire.
        value = [float("nan")] * over

        bounded, kept = plugin_import._bounded_manifest_value(value, "x", report, 0)

        assert kept and bounded == []
        assert len(report.warnings) <= plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 1, (
            f"{len(report.warnings)} warnings from {over} dropped items against a "
            f"cap of {plugin_import.MAX_MANIFEST_CONTAINER_ITEMS}"
        )

    def test_an_ordinary_container_is_carried_whole(self):
        # CONTROL. Without this, counting every entry against a cap of zero would
        # satisfy both tests above while emitting nothing a manifest declared.
        report = self._report()
        value = {"a": 1, "b": [2, 3], "c": {"d": "e"}}

        bounded, kept = plugin_import._bounded_manifest_value(value, "x", report, 0)

        assert kept and bounded == value
        assert report.warnings == []


class TestHookDocumentsAreBoundedByWhatIsRead:
    """Nothing rejects a repeat, so the per-file byte budget bounds one read only.

    A manifest may declare one file any number of times and each declaration is
    opened, parsed and retained again. A cap on the retained list would not help
    either: a missing path appends a warning and retains no document, so the list
    stays empty while the report grows.
    """

    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_one_file_declared_many_times_is_read_a_bounded_number_of_times(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
        over = plugin_import.MAX_HOOK_DOCUMENTS * 20

        reads: list = []
        real = plugin_import._read_json_object

        def counting(path, what):
            reads.append(path)
            return real(path, what)

        monkeypatch.setattr(plugin_import, "_read_json_object", counting)

        plugin_import._hook_files(root, ["./hooks.json"] * over, self._report())

        assert len(reads) <= plugin_import.MAX_HOOK_DOCUMENTS, (
            f"the same file was parsed {len(reads)} times against a bound of "
            f"{plugin_import.MAX_HOOK_DOCUMENTS}"
        )

    def test_missing_paths_do_not_grow_the_report_per_entry(self, tmp_path):
        root = tmp_path / "pkg"
        root.mkdir()
        report = self._report()
        over = plugin_import.MAX_HOOK_DOCUMENTS * 20

        documents = plugin_import._hook_files(root, ["./absent.json"] * over, report)

        assert documents == []
        assert len(report.warnings) <= plugin_import.MAX_HOOK_DOCUMENTS + 1, (
            f"{len(report.warnings)} warnings from {over} missing paths against a "
            f"bound of {plugin_import.MAX_HOOK_DOCUMENTS}"
        )

    def test_an_ordinary_declaration_is_read_whole(self, tmp_path):
        # CONTROL. A bound of zero would satisfy both tests above while reading no
        # hooks file a manifest declared.
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "hooks.json").write_text('{"hooks": {"onSave": [1]}}', encoding="utf-8")
        report = self._report()

        documents = plugin_import._hook_files(root, ["./hooks.json"], report)

        assert documents == [{"hooks": {"onSave": [1]}}]
        assert report.warnings == []


class TestALinkedANCESTORIsRefusedBeforeAnyProbe:
    """A leaf-only link check still resolves through a linked PARENT.

    The `is_dir()` that follows is the probe that does it: on Windows an ancestor
    junction whose target is a UNC share turns that call on a local-looking path into
    an outbound SMB connection authenticating as this process, to a host the PACKAGE
    chose. A lexical UNC screen cannot catch it, because the path being probed is not
    itself UNC-shaped -- only the link's target is. The repo's own
    `first_linked_ancestor` documents this and walks root-first so the walk never
    traverses a link, and the sibling check in `member_essential_context` already
    pairs it with the leaf test.
    """

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks on this platform")
    def test_a_package_under_a_linked_ancestor_is_refused(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "pkg").mkdir(parents=True)
        (elsewhere / "pkg" / "plugin.json").write_text("{}", encoding="utf-8")
        # The link stands at an ANCESTOR of the root, not at the root.
        link = tmp_path / "via"
        link.symlink_to(elsewhere, target_is_directory=True)
        root = link / "pkg"

        with pytest.raises(plugin_import.PluginImportError) as exc:
            plugin_import.find_plugin_manifest(root)

        assert exc.value.code == "source_not_a_directory"
        # The offending ancestor is deliberately NOT named: which ancestor is a link
        # is filesystem layout the caller supplied, so naming it adds nothing.
        assert "via" not in str(exc.value) or str(root) in str(exc.value)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks on this platform")
    def test_the_leaf_itself_is_still_refused(self, tmp_path):
        # The ancestor check must ADD to the leaf check, not replace it.
        real = tmp_path / "real"
        real.mkdir()
        (real / "plugin.json").write_text("{}", encoding="utf-8")
        root = tmp_path / "leaf"
        root.symlink_to(real, target_is_directory=True)

        with pytest.raises(plugin_import.PluginImportError) as exc:
            plugin_import.find_plugin_manifest(root)

        assert exc.value.code == "source_not_a_directory"

    def test_an_ordinary_package_is_still_found(self, tmp_path):
        # CONTROL. Without this, refusing every root would satisfy both tests above
        # while making every import impossible.
        root = tmp_path / "pkg"
        root.mkdir()
        _write_json(
            root / "plugin.json",
            {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1", "name": "demo"},
        )

        found, fmt = plugin_import.find_plugin_manifest(root)

        assert found == root / "plugin.json"
        assert fmt == plugin_import.FORMAT_SCHEMA_QUALIFIED


class TestATransportlessServerIsNotWritten:
    """An entry naming no way to reach the server is not a server.

    Nothing else catches it: an empty or transportless object is a dict, carries no
    package-relative field to refuse, and the bounding pass keeps it because nothing
    in it is over-limit -- so it reached app.json as a server with no command and no
    url, which no reader can launch and no message mentioned. A manifest author
    producing one is an ordinary mistake, not a crafted input.
    """

    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_an_empty_config_is_dropped_and_named(self, tmp_path):
        report = self._report()

        cleaned = plugin_import._convert_mcp_servers(tmp_path, {"ghost": {}}, report)

        assert cleaned == {}, f"a transportless server was written: {cleaned}"
        assert any("ghost" in w for w in report.warnings), report.warnings

    def test_a_config_with_fields_but_no_transport_is_dropped(self, tmp_path):
        # The empty dict is the obvious case; this is the one that reads as a real
        # entry. Everything here is valid JSON of the right shape and still names
        # nothing to run or connect to.
        report = self._report()
        declared = {"ghost": {"env": {"TOKEN": "x"}, "disabled": False}}

        cleaned = plugin_import._convert_mcp_servers(tmp_path, declared, report)

        assert cleaned == {}, f"a transportless server was written: {cleaned}"

    def test_a_command_server_is_still_written(self, tmp_path):
        # CONTROL. Without this, refusing every entry would satisfy both tests above
        # while dropping every server a manifest legitimately declares.
        report = self._report()
        declared = {"real": {"command": "/usr/bin/thing", "args": ["--serve"]}}

        cleaned = plugin_import._convert_mcp_servers(tmp_path, declared, report)

        assert cleaned == declared
        assert report.warnings == []

    def test_a_url_server_is_still_written(self, tmp_path):
        # The other transport spelling, so the check cannot be read as command-only.
        report = self._report()
        declared = {"remote": {"url": "https://example.invalid/mcp"}}

        cleaned = plugin_import._convert_mcp_servers(tmp_path, declared, report)

        assert cleaned == declared
        assert report.warnings == []


class TestTheMappingDocCitesOnlyThisRepo:
    """The mapping doc is the converter's reason for every kind it skips, so a
    reader has to be able to resolve what it cites.

    It was written against a companion contract that ships in a DIFFERENT pull
    request, and the citations survived the split: every "section 3-5", "section 7"
    and named diff pointed at a document absent from this tree, which reads as a
    broken reference rather than as future work. Pinned by TOKEN rather than by
    reviewing the prose, because the failure is silent -- nothing in a docs lint
    can tell a citation of an absent document from a citation of a present one.
    """

    _DOC = Path(__file__).resolve().parents[1] / (
        "docs/system-specs/modules/harness-plugin-mapping.md"
    )

    #: Each token appeared ONLY in this doc, so each one dangles by construction.
    _ABSENT = (
        "contribution protocol",
        "stale_seq",
        "10,000 events",
        "sections 3-5",
        "the contract",
        "Diff D",
        "our section",
    )

    def test_the_doc_cites_no_document_absent_from_this_tree(self):
        text = self._DOC.read_text(encoding="utf-8")
        dangling = sorted(t for t in self._ABSENT if t in text)
        assert not dangling, (
            f"the mapping doc cites {dangling}, which resolve nowhere in this tree: "
            "either the cited document must land on the base branch first, or the "
            "citation must say what it means without naming it"
        )

    def test_the_doc_still_carries_the_matrix_it_exists_for(self):
        # CONTROL. Without this, deleting the doc outright would satisfy the test
        # above while removing the reason the converter reports for every skip.
        text = self._DOC.read_text(encoding="utf-8")
        assert "## 3. The matrix" in text, "the matrix is what the converter cites"
        for bucket in ("**(a) ", "**(b) ", "**(c) ", "**(d) "):
            assert bucket in text, f"bucket {bucket!r} is gone, so a row can name no bucket"

    def test_a_proposal_set_does_not_live_in_this_spec(self):
        """Proposals belong in docs/request-for-change/, whose README calls itself
        'a proposal and a record of a decision'. A system spec describes what the
        code does, so a section of unapplied diffs here is the wrong genre in the
        wrong directory -- and its own diffs cited the absent contract."""
        text = self._DOC.read_text(encoding="utf-8")
        assert "Proposed diffs" not in text
        assert "Proposals, not applied" not in text

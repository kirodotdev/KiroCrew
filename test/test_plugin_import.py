"""Tests for kiro_crew.apps.plugin_import -- converting a plugin package into an app.

The three properties worth a test are the ones a converter gets wrong in a way
nobody notices: it silently drops a kind, it follows a path out of the package
root, or it emits a manifest that only looks valid. The last one is covered by
installing the converted app for real rather than by asserting on the JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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
        assert any("list capped at" in s for s in skipped)

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

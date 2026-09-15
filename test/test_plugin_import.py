"""Tests for kiro_crew.apps.plugin_import -- converting a plugin package into an app.

The three properties worth a test are the ones a converter gets wrong in a way
nobody notices: it silently drops a kind, it follows a path out of the package
root, or it emits a manifest that only looks valid. The last one is covered by
installing the converted app for real rather than by asserting on the JSON.
"""

from __future__ import annotations

import json
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
        assert any("symlink skipped" in w for w in report.warnings)

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


class TestOutput:
    def test_a_second_conversion_into_the_same_directory_is_refused(self, tmp_path):
        root = _package(tmp_path)
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, out)
        assert exc.value.code == "output_not_empty"

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

"""Tests for kiro_crew.apps.installed_powers -- the in-place Agent Plugins loader.

The properties worth pinning are the ones a client gets wrong in a way that is
either insecure or silently broken: it loads when the operator never opted in, it
projects a server that leaks a credential or escapes the plugin root, it fails to
expand the two plugin variables (or expands something it must not), or one bad
plugin/entry takes down its siblings.

Every test redirects the install root, the PLUGIN_DATA home and the enablement
gate to the test's own tmp tree, so nothing here reads or writes the real
``~/.kiro``.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from kiro_crew.apps import installed_powers as ip
from kiro_crew.mcp_provenance import POWER_KEY, is_power_entry


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _plugin(
    install_dir: Path,
    name: str,
    *,
    schema: str = "1.0.0",
    manifest_extra: dict | None = None,
    include_schema: bool = True,
    mcp: dict | None = None,
    mcp_schema: str | None = None,
    skills: tuple[str, ...] = (),
    nested_skills: tuple[str, ...] = (),
) -> Path:
    """Build an installed plugin tree and return its root."""
    root = install_dir / name
    root.mkdir(parents=True)
    manifest: dict = {"name": name}
    if include_schema:
        manifest["$schema"] = f"https://agent-plugins.org/schemas/{schema}/plugin.schema.json"
    if manifest_extra:
        manifest.update(manifest_extra)
    _write(root / "plugin.json", manifest)
    if mcp is not None:
        doc = {
            "$schema": (
                "https://agent-plugins.org/schemas/" f"{mcp_schema or schema}/mcp.schema.json"
            ),
            "mcpServers": mcp,
        }
        _write(root / "mcp.json", doc)
    for skill_name in skills:
        (root / "skills" / skill_name).mkdir(parents=True)
        (root / "skills" / skill_name / "SKILL.md").write_text("# s", encoding="utf-8")
    for skill_name in nested_skills:
        deep = root / "skills" / "group" / skill_name
        deep.mkdir(parents=True)
        (deep / "SKILL.md").write_text("# s", encoding="utf-8")
    return root


@pytest.fixture
def powers(tmp_path, monkeypatch):
    """An empty install root with the loader pointed at the tmp tree and gated on."""
    install = tmp_path / "installed"
    install.mkdir()
    monkeypatch.setattr(ip, "INSTALLED_POWERS_DIR", install)
    monkeypatch.setattr(ip, "config_dir", lambda: tmp_path / "crew")
    monkeypatch.setattr(ip, "loading_enabled", lambda: True)
    return install


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_disabled_flag_loads_nothing(self, tmp_path, monkeypatch):
        install = tmp_path / "installed"
        install.mkdir()
        _plugin(
            install, "p", mcp={"s": {"type": "streamable-http", "url": "https://x.example/mcp"}}
        )
        monkeypatch.setattr(ip, "INSTALLED_POWERS_DIR", install)
        monkeypatch.setattr(ip, "config_dir", lambda: tmp_path / "crew")
        monkeypatch.setattr(ip, "loading_enabled", lambda: False)
        report = ip.load_installed_powers()
        assert report.powers == []
        assert report.mcp_servers() == {}

    def test_loading_enabled_reads_the_flag(self, monkeypatch):
        cfg = types.SimpleNamespace(agent=types.SimpleNamespace(load_installed_powers=True))
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: cfg)
        )
        assert ip.loading_enabled() is True

    def test_loading_enabled_fails_closed(self, monkeypatch):
        def _boom(cls):
            raise RuntimeError("unreadable")

        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom))
        assert ip.loading_enabled() is False

    def test_missing_install_dir_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ip, "INSTALLED_POWERS_DIR", tmp_path / "does-not-exist")
        monkeypatch.setattr(ip, "loading_enabled", lambda: True)
        assert ip.load_installed_powers().powers == []


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_missing_schema_skips_the_plugin(self, powers):
        _plugin(powers, "p", include_schema=True, schema="1.0.0", mcp={})
        # Overwrite with a manifest carrying no $schema.
        _write(powers / "p" / "plugin.json", {"name": "p"})
        report = ip.load_installed_powers()
        assert report.powers == []
        assert any("no $schema" in w for w in report.warnings)

    def test_foreign_schema_skips_the_plugin(self, powers):
        root = _plugin(powers, "p")
        _write(root / "plugin.json", {"$schema": "https://example.com/x.json", "name": "p"})
        assert ip.load_installed_powers().powers == []

    def test_missing_name_skips_the_plugin(self, powers):
        root = _plugin(powers, "p")
        _write(
            root / "plugin.json",
            {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"},
        )
        report = ip.load_installed_powers()
        assert report.powers == []
        assert any("no string 'name'" in w for w in report.warnings)

    def test_non_object_extensions_warns_but_loads(self, powers):
        _plugin(
            powers,
            "p",
            manifest_extra={"extensions": 5},
            mcp={"s": {"type": "streamable-http", "url": "https://x.example/mcp"}},
        )
        report = ip.load_installed_powers()
        assert report.mcp_servers()  # the plugin still loaded its server
        assert any("'extensions' is not an object" in w for w in report.warnings)

    def test_a_non_plugin_directory_is_quietly_ignored(self, powers):
        (powers / "notaplugin").mkdir()
        (powers / "notaplugin" / "readme.txt").write_text("hi", encoding="utf-8")
        report = ip.load_installed_powers()
        assert report.powers == []
        assert report.warnings == []


# ---------------------------------------------------------------------------
# MCP projection
# ---------------------------------------------------------------------------


class TestStdioProjection:
    def test_relative_command_resolved_against_root(self, powers, tmp_path):
        root = _plugin(powers, "p", mcp={"local": {"type": "stdio", "command": "./bin/server"}})
        (root / "bin").mkdir()
        (root / "bin" / "server").write_text("#!/bin/sh\n", encoding="utf-8")
        servers = ip.load_installed_powers().mcp_servers()
        assert "power-p--local" in servers
        spec = servers["power-p--local"]
        assert spec["command"] == str((root / "bin" / "server").resolve())
        assert spec["cwd"] == str(root)
        assert spec["env"]["PLUGIN_ROOT"] == str(root)
        assert spec["env"]["PLUGIN_DATA"] == str(tmp_path / "crew" / "powers-data" / "p")

    def test_plugin_variables_expanded_in_args_and_env(self, powers, tmp_path):
        _plugin(
            powers,
            "p",
            mcp={
                "local": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["--data", "${PLUGIN_DATA}"],
                    "env": {"HOME_DIR": "${PLUGIN_ROOT}"},
                }
            },
        )
        spec = ip.load_installed_powers().mcp_servers()["power-p--local"]
        data_dir = str(tmp_path / "crew" / "powers-data" / "p")
        assert spec["args"] == ["--data", data_dir]
        assert spec["env"]["HOME_DIR"] == str(powers / "p")

    def test_plugin_cannot_override_the_injected_vars(self, powers):
        _plugin(
            powers,
            "p",
            mcp={
                "local": {
                    "type": "stdio",
                    "command": "node",
                    "env": {"PLUGIN_ROOT": "/evil", "PLUGIN_DATA": "/evil"},
                }
            },
        )
        spec = ip.load_installed_powers().mcp_servers()["power-p--local"]
        assert spec["env"]["PLUGIN_ROOT"] == str(powers / "p")
        assert Path(spec["env"]["PLUGIN_DATA"]).parts[-2:] == ("powers-data", "p")

    def test_command_with_plugin_root_placeholder_is_dropped(self, powers):
        # ``command`` is never expanded, so a ${PLUGIN_ROOT} there is a runtime
        # env reference and is rejected -- authors use ./ for the root instead.
        _plugin(
            powers,
            "p",
            mcp={"local": {"type": "stdio", "command": "${PLUGIN_ROOT}/bin/x"}},
        )
        report = ip.load_installed_powers()
        assert report.mcp_servers() == {}
        assert any("power-p" in w or "p:" in w or "p " in w for w in report.warnings)

    def test_escaping_relative_command_is_dropped(self, powers):
        _plugin(powers, "p", mcp={"local": {"type": "stdio", "command": "./../evil"}})
        report = ip.load_installed_powers()
        assert report.mcp_servers() == {}


class TestRemoteProjection:
    def test_streamable_http_projected_with_headers(self, powers):
        _plugin(
            powers,
            "p",
            mcp={
                "remote": {
                    "type": "streamable-http",
                    "url": "https://tools.example/mcp",
                    "headers": {"X-Trace": "1"},
                }
            },
        )
        spec = ip.load_installed_powers().mcp_servers()["power-p--remote"]
        assert spec["url"] == "https://tools.example/mcp"
        assert spec["headers"] == {"X-Trace": "1"}

    def test_loopback_http_is_kept_but_remote_cleartext_is_dropped(self, powers):
        _plugin(
            powers,
            "p",
            mcp={
                "loop": {"type": "streamable-http", "url": "http://127.0.0.1:9000/mcp"},
                "bad": {"type": "streamable-http", "url": "http://tools.example/mcp"},
            },
        )
        servers = ip.load_installed_powers().mcp_servers()
        assert "power-p--loop" in servers
        assert "power-p--bad" not in servers


class TestIsolation:
    def test_invalid_entries_dropped_valid_sibling_kept(self, powers):
        _plugin(
            powers,
            "p",
            mcp={
                "ok": {"type": "streamable-http", "url": "https://x.example/mcp"},
                "cleartext": {"type": "streamable-http", "url": "http://x.example/mcp"},
                "envref": {"type": "stdio", "command": "node", "env": {"K": "${HOME}"}},
                "mistyped": {"type": "stdio", "command": "node", "args": 7},
            },
        )
        report = ip.load_installed_powers()
        servers = report.mcp_servers()
        assert set(servers) == {"power-p--ok"}
        assert sum("dropped" in w for w in report.warnings) >= 3

    def test_version_mismatch_disables_mcp(self, powers):
        _plugin(
            powers,
            "p",
            schema="1.0.0",
            mcp_schema="2.0.0",
            mcp={"s": {"type": "streamable-http", "url": "https://x.example/mcp"}},
        )
        report = ip.load_installed_powers()
        assert report.mcp_servers() == {}
        assert any("version" in w and "MCP disabled" in w for w in report.warnings)

    def test_one_broken_plugin_does_not_sink_a_sibling(self, powers):
        _plugin(
            powers, "good", mcp={"s": {"type": "streamable-http", "url": "https://x.example/mcp"}}
        )
        bad = _plugin(powers, "bad")
        (bad / "plugin.json").write_text("{ not json", encoding="utf-8")
        servers = ip.load_installed_powers().mcp_servers()
        assert "power-good--s" in servers
        assert not any(k.startswith("power-bad--") for k in servers)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class TestSkills:
    def test_skills_root_returned_when_a_skill_exists(self, powers):
        root = _plugin(powers, "p", skills=("alpha",))
        roots = ip.load_installed_powers().skill_roots()
        assert roots == [str(root / "skills")]

    def test_empty_skills_dir_is_not_added(self, powers):
        root = _plugin(powers, "p")
        (root / "skills").mkdir()
        assert ip.load_installed_powers().skill_roots() == []

    def test_nested_only_skill_is_not_discovered(self, powers):
        # The spec discovers immediate children of skills/ only, non-recursive.
        _plugin(powers, "p", nested_skills=("deep",))
        assert ip.load_installed_powers().skill_roots() == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_slug_folds_punctuation(self):
        assert ip._slug("My Cool.Plugin") == "my-cool-plugin"
        assert ip._slug("  spaced  ") == "spaced"
        assert ip._slug("!!!") == ""

    def test_schema_version_extracts_the_version(self):
        assert (
            ip._schema_version("https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
            == "1.0.0"
        )
        assert ip._schema_version("https://example.com/x.json") is None
        assert ip._schema_version(5) is None


# ---------------------------------------------------------------------------
# Wiring: the flag, the rebuild reconcile, skill discovery, logged drops
# ---------------------------------------------------------------------------


class TestRealConfigFlag:
    """The flag must reach ``loading_enabled`` through the real config loader."""

    @pytest.mark.parametrize("value", [True, False])
    def test_flag_in_config_file_is_read(self, tmp_path, monkeypatch, value):
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(
            json.dumps({"agent": {"load_installed_powers": value}}), encoding="utf-8"
        )
        assert ip.loading_enabled() is value

    def test_flag_defaults_off_without_the_key(self, tmp_path, monkeypatch):
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
        assert ip.loading_enabled() is False


def _owned(spec: dict) -> dict:
    """A spec as the loader projects it: stamped with the ownership marker."""
    return {**spec, POWER_KEY: True}


class TestRebuildReconcile:
    """Ownership is the entry's marker, never its name."""

    @staticmethod
    def _merge(
        config: dict,
        monkeypatch,
        servers: dict[str, dict],
        *,
        apps: dict[str, dict] | None = None,
        shared: dict[str, dict] | None = None,
    ) -> dict:
        from kiro_crew.agent_materialization import mcp_sources

        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: dict(apps or {}))
        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: servers)
        monkeypatch.setattr(
            mcp_sources.user_json, "load_mcp_servers", lambda _path: dict(shared or {})
        )
        mcp_sources.merge_mcp_sources(config)
        return config

    def test_a_marked_entry_no_longer_derived_is_retired(self, monkeypatch):
        config = {
            "mcpServers": {
                "power-foo--srv": _owned({"command": "/abs/bin/server"}),
                "unrelated": {"command": "/bin/true"},
            },
            "tools": ["@power-foo--srv", "@unrelated", "fs_read"],
        }
        out = self._merge(config, monkeypatch, {})
        assert "power-foo--srv" not in out["mcpServers"]
        assert "@power-foo--srv" not in out["tools"]
        assert "unrelated" in out["mcpServers"]
        assert "@unrelated" in out["tools"]

    def test_a_rederived_entry_is_refreshed_and_stays_marked(self, monkeypatch):
        spec = _owned({"url": "https://x.example/mcp"})
        config = {
            "mcpServers": {
                "power-foo--srv": _owned({"command": "/old"}),
                "power-gone--srv": _owned({"url": "u"}),
            },
            "tools": ["@power-foo--srv", "@power-gone--srv"],
        }
        out = self._merge(config, monkeypatch, {"power-foo--srv": spec})
        assert out["mcpServers"]["power-foo--srv"] == spec
        assert is_power_entry(out["mcpServers"]["power-foo--srv"])
        assert "power-gone--srv" not in out["mcpServers"]
        assert "@power-foo--srv" in out["tools"]
        assert "@power-gone--srv" not in out["tools"]

    def test_an_unmarked_power_named_server_survives_with_the_flag_off(self, monkeypatch):
        config = {
            "mcpServers": {"power-helper": {"command": "/bin/helper"}},
            "tools": ["@power-helper"],
        }
        out = self._merge(config, monkeypatch, {})
        assert "power-helper" in out["mcpServers"]
        assert "@power-helper" in out["tools"]

    def test_an_app_named_power_keeps_its_servers(self, monkeypatch):
        app_spec = {"command": "/bin/app-server"}
        config: dict = {"mcpServers": {}, "tools": []}
        out = self._merge(config, monkeypatch, {}, apps={"power-tools:srv": app_spec})
        assert out["mcpServers"]["power-tools:srv"] == app_spec
        assert "@power-tools:srv" in out["tools"]

    def test_a_failed_load_retires_every_marked_entry(self, monkeypatch):
        from kiro_crew.agent_materialization import mcp_sources

        def _boom():
            raise RuntimeError("broken tree")

        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: {})
        monkeypatch.setattr(ip, "installed_power_mcp_servers", _boom)
        monkeypatch.setattr(mcp_sources.user_json, "load_mcp_servers", lambda _path: {})
        config = {
            "mcpServers": {"power-foo--srv": _owned({"url": "https://x"})},
            "tools": ["@power-foo--srv"],
        }
        mcp_sources.merge_mcp_sources(config)
        assert "power-foo--srv" not in config["mcpServers"]


class TestNeverTakeAName:
    """A plugin cannot mount over a name, or a grant, that belongs to someone else."""

    _NEW = _owned({"url": "https://plugin.example/mcp"})

    def test_an_unmarked_entry_with_the_same_name_is_kept(self, monkeypatch):
        mine = {"command": "/bin/mine"}
        config = {
            "mcpServers": {"power-p--s": mine},
            "tools": ["@power-p--s"],
            "allowedTools": ["@power-p--s"],
        }
        out = TestRebuildReconcile._merge(config, monkeypatch, {"power-p--s": self._NEW})
        assert out["mcpServers"]["power-p--s"] == mine
        assert out["allowedTools"] == ["@power-p--s"]

    def test_a_standing_grant_with_no_entry_blocks_the_mount(self, monkeypatch):
        config: dict = {"mcpServers": {}, "tools": [], "allowedTools": ["@power-p--s/tool"]}
        out = TestRebuildReconcile._merge(config, monkeypatch, {"power-p--s": self._NEW})
        assert "power-p--s" not in out["mcpServers"]
        assert "@power-p--s" not in out["tools"]

    def test_a_shared_file_server_with_the_same_name_blocks_the_mount(self, monkeypatch):
        shared = {"power-p--s": {"command": "/bin/shared"}}
        config: dict = {"mcpServers": {}, "tools": []}
        out = TestRebuildReconcile._merge(
            config, monkeypatch, {"power-p--s": self._NEW}, shared=shared
        )
        assert not is_power_entry(out["mcpServers"].get("power-p--s"))

    def test_a_free_name_is_mounted(self, monkeypatch):
        config: dict = {"mcpServers": {}, "tools": [], "allowedTools": ["@other"]}
        out = TestRebuildReconcile._merge(config, monkeypatch, {"power-p--s": self._NEW})
        assert out["mcpServers"]["power-p--s"] == self._NEW
        assert "@power-p--s" in out["tools"]


class TestFullRebuild:
    """The marker survives a real agent-config rebuild, and drives retirement."""

    def test_marker_survives_and_retires_on_the_next_rebuild(self, tmp_path, monkeypatch):
        from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

        cfg_dir = bundled_defaults(tmp_path)
        remote = _owned({"url": "https://plugin.example/mcp"})
        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: {"power-p--s": remote})
        first = run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers={})
        assert is_power_entry(first["mcpServers"]["power-p--s"])
        assert "@power-p--s" in first["tools"]

        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: {})
        second = run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers={})
        assert "power-p--s" not in second["mcpServers"]
        assert "@power-p--s" not in second["tools"]


class TestKeysAndBounds:
    def test_plugin_server_pairs_do_not_collide(self, powers):
        url = {"type": "streamable-http", "url": "https://x.example/mcp"}
        _plugin(powers, "foo", mcp={"bar-baz": url})
        _plugin(powers, "foo-bar", mcp={"baz": url})
        servers = ip.load_installed_powers().mcp_servers()
        assert set(servers) == {"power-foo--bar-baz", "power-foo-bar--baz"}

    def test_two_server_names_folding_to_one_key_warn(self, powers):
        url = {"type": "streamable-http", "url": "https://x.example/mcp"}
        _plugin(powers, "p", mcp={"a_b": url, "a-b": url})
        report = ip.load_installed_powers()
        assert list(report.mcp_servers()) == ["power-p--a-b"]
        assert any("same key" in w for w in report.warnings)

    def test_two_plugins_folding_to_one_slug_warn(self, powers):
        # Names that differ by more than case, so a case-insensitive file system
        # (Windows, macOS) can hold both directories.
        url = {"type": "streamable-http", "url": "https://x.example/mcp"}
        _plugin(powers, "foo.bar", mcp={"s": url})
        _plugin(powers, "foo-bar", mcp={"s": url})
        report = ip.load_installed_powers()
        assert len(report.powers) == 1
        assert any("same name as another plugin" in w for w in report.warnings)

    def test_the_power_cap_reports_truncation(self, powers, monkeypatch):
        monkeypatch.setattr(ip, "MAX_POWERS", 2)
        for name in ("a", "b", "c"):
            _plugin(powers, name, skills=("s",))
        report = ip.load_installed_powers()
        assert len(report.powers) == 2
        assert any("more than 2 installed powers" in w for w in report.warnings)


class TestCwd:
    @staticmethod
    def _cwd(powers, cwd: str, subdir: str = "") -> tuple[str, list[str]]:
        root = _plugin(powers, "p", mcp={"local": {"command": "node", "cwd": cwd}})
        if subdir:
            (root / subdir).mkdir()
        report = ip.load_installed_powers()
        return report.mcp_servers()["power-p--local"]["cwd"], report.warnings

    def test_plugin_data_cwd_is_honored(self, powers, tmp_path):
        cwd, warnings = self._cwd(powers, "${PLUGIN_DATA}")
        assert Path(cwd) == tmp_path / "crew" / "powers-data" / "p"
        assert not warnings

    def test_plugin_root_subdir_cwd_is_honored(self, powers):
        cwd, warnings = self._cwd(powers, "${PLUGIN_ROOT}/server", subdir="server")
        assert Path(cwd) == powers / "p" / "server"
        assert not warnings

    def test_escaping_cwd_falls_back_with_a_warning(self, powers, tmp_path):
        cwd, warnings = self._cwd(powers, str(tmp_path))
        assert cwd == str(powers / "p")
        assert any("cwd is outside" in w for w in warnings)

    def test_dotdot_out_of_the_root_falls_back(self, powers):
        cwd, warnings = self._cwd(powers, "${PLUGIN_ROOT}/../other")
        assert cwd == str(powers / "p")
        assert any("cwd is outside" in w for w in warnings)

    def test_a_linked_component_below_the_root_is_refused(self, powers, tmp_path):
        from kiro_crew.platform_compat import symlink_or_junction

        root = _plugin(powers, "p")
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            symlink_or_junction(outside, root / "link")
        except OSError:
            pytest.skip("this host cannot create a link or junction")
        assert ip._resolve_cwd(str(root / "link"), root, str(tmp_path / "data")) is None


class TestSkillDiscovery:
    """Plugin skill roots reach the ``SkillsLoader`` the agent actually uses, off-loop."""

    @staticmethod
    def _home(tmp_path, monkeypatch, enabled: bool) -> None:
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(
            json.dumps({"agent": {"load_installed_powers": enabled}}), encoding="utf-8"
        )

    def test_skills_loader_adopts_the_plugin_root_from_a_thread(
        self, powers, tmp_path, monkeypatch
    ):
        from kiro_crew.skills import SkillsLoader

        root = _plugin(powers, "p", skills=("alpha",))
        self._home(tmp_path, monkeypatch, enabled=True)
        loader = SkillsLoader(install_builtins=False)
        try:
            assert loader._power_scan_thread is not None
            loader._power_scan_thread.join(timeout=30)
            expected = (root / "skills").resolve()
            assert expected in loader._extra_paths
            assert expected in loader._power_extra_paths
            # A config reload keeps the plugin root.
            loader._adopt_extra_paths([])
            assert expected in loader._extra_paths
        finally:
            loader.close()

    def test_the_constructor_does_not_scan(self, powers, tmp_path, monkeypatch):
        import threading

        from kiro_crew.skills import SkillsLoader

        _plugin(powers, "p", skills=("alpha",))
        self._home(tmp_path, monkeypatch, enabled=True)
        constructing = threading.current_thread()
        scanned_on: list[threading.Thread] = []
        real = ip.installed_power_skill_roots

        def _record():
            scanned_on.append(threading.current_thread())
            return real()

        monkeypatch.setattr(ip, "installed_power_skill_roots", _record)
        loader = SkillsLoader(install_builtins=False)
        try:
            assert loader._power_scan_thread is not None
            loader._power_scan_thread.join(timeout=30)
            assert scanned_on and constructing not in scanned_on
        finally:
            loader.close()

    def test_skills_loader_adds_nothing_when_off(self, tmp_path, monkeypatch):
        from kiro_crew.skills import SkillsLoader

        install = tmp_path / "installed"
        _plugin(install, "p", skills=("alpha",))
        monkeypatch.setattr(ip, "INSTALLED_POWERS_DIR", install)
        self._home(tmp_path, monkeypatch, enabled=False)
        loader = SkillsLoader(install_builtins=False)
        try:
            assert loader._power_scan_thread is None
            assert loader._power_extra_paths == []
        finally:
            loader.close()


class TestSkillOrigin:
    def test_a_plugin_skill_is_tagged_plugin_not_builtin(self, powers, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers.skill_budget import _origin

        root = _plugin(powers, "p", skills=("alpha",))
        skill_file = root / "skills" / "alpha" / "SKILL.md"
        loader = types.SimpleNamespace(
            _is_user_authored=lambda _row: False,
            _power_extra_paths=[(root / "skills").resolve()],
        )
        assert _origin(loader, "alpha", skill_file, None) == "plugin"

    def test_a_skill_outside_plugin_roots_keeps_its_old_origin(self, tmp_path):
        from kiro_crew.dashboard.handlers.skill_budget import _origin

        other = tmp_path / "elsewhere" / "SKILL.md"
        other.parent.mkdir()
        other.write_text("# s", encoding="utf-8")
        loader = types.SimpleNamespace(
            _is_user_authored=lambda _row: False,
            _power_extra_paths=[(tmp_path / "plugins").resolve()],
        )
        assert _origin(loader, "x", other, None) == "builtin"


class TestDropsAreLogged:
    def test_each_drop_reason_is_logged(self, powers, caplog):
        _plugin(powers, "noschema", include_schema=False)
        _plugin(
            powers,
            "cleartext",
            mcp={"s": {"type": "streamable-http", "url": "http://remote.example/mcp"}},
        )
        with caplog.at_level("WARNING", logger="kiro_crew.apps.installed_powers"):
            report = ip.load_installed_powers()
        assert report.warnings
        logged = "\n".join(r.getMessage() for r in caplog.records)
        for warning in report.warnings:
            assert warning in logged


class TestBrokerNeverRoutesAPlugin:
    """A marked entry passes through the rewriter even when routing names it."""

    @staticmethod
    def _rewrite(tmp_path, entry: dict) -> tuple[dict, int]:
        from kiro_crew.mcp_gateway.rewriter_spec import _rewrite_single_spec

        spec = {"name": "kirocrew", "mcpServers": {"power-p--s": entry}}
        return _rewrite_single_spec(
            spec,
            stubs_dir=tmp_path / "stubs",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "gw",
            sandbox_mode="off",
            approval_mode="normal",
            stub_servers=frozenset({"power-p--s"}),
        )

    def test_a_marked_entry_is_not_stubbed(self, tmp_path):
        import sys

        entry = _owned({"command": sys.executable, "args": ["server.js"], "cwd": str(tmp_path)})
        out, wrapped = self._rewrite(tmp_path, entry)
        assert wrapped == 0
        assert out["mcpServers"]["power-p--s"] == entry

    def test_control_the_same_entry_unmarked_is_stubbed(self, tmp_path):
        import sys

        entry = {"command": sys.executable, "args": ["server.js"], "cwd": str(tmp_path)}
        _out, wrapped = self._rewrite(tmp_path, entry)
        assert wrapped == 1


class TestPluginGetsNoWholeServerGrant:
    """A marked entry copied into a shared source is mounted, never auto-approved."""

    def test_a_store_copy_is_mounted_without_an_allowed_tools_grant(self, monkeypatch):
        from kiro_crew.agent_materialization import mcp_sources

        marked = _owned({"url": "https://plugin.example/mcp", "disabledTools": ["one"]})
        config = {
            "mcpServers": {"power-p--s": dict(marked)},
            "tools": [],
            "allowedTools": [],
        }
        sources = mcp_sources.McpSources(
            kirocrew={"power-p--s": marked},
            kiro_global={},
            provider_global={},
            managed_names=set(),
        )
        monkeypatch.setattr(mcp_sources.auto_approve, "_may_auto_approve", lambda _ref: True)
        mcp_sources.sync_shared_server_refs(config, sources, {"power-p--s": "power-p--s"})
        assert "@power-p--s" in config["tools"]
        assert "@power-p--s" not in config["allowedTools"]

    def test_control_an_unmarked_store_server_does_get_the_grant(self, monkeypatch):
        from kiro_crew.agent_materialization import mcp_sources

        plain = {"url": "https://plain.example/mcp"}
        config = {"mcpServers": {"plain": dict(plain)}, "tools": [], "allowedTools": []}
        sources = mcp_sources.McpSources(
            kirocrew={"plain": plain},
            kiro_global={},
            provider_global={},
            managed_names=set(),
        )
        monkeypatch.setattr(mcp_sources.auto_approve, "_may_auto_approve", lambda _ref: True)
        mcp_sources.sync_shared_server_refs(config, sources, {"plain": "plain"})
        assert "@plain" in config["allowedTools"]


class TestAcpBackendsWithholdLocalPluginServers:
    """A backend whose element has no ``cwd`` cannot run a local plugin server."""

    def test_a_local_plugin_server_is_withheld(self):
        from kiro_crew.acp.session_mcp import acp_server_element

        entry = _owned({"command": "node", "args": ["server.js"], "cwd": "/plug"})
        assert acp_server_element("power-p--s", entry) is None

    def test_a_remote_plugin_server_is_still_projected(self):
        from kiro_crew.acp.session_mcp import acp_server_element

        entry = _owned({"url": "https://plugin.example/mcp"})
        element = acp_server_element("power-p--s", entry)
        assert element is not None and element["url"] == "https://plugin.example/mcp"

    def test_control_an_unmarked_local_server_is_projected(self):
        from kiro_crew.acp.session_mcp import acp_server_element

        element = acp_server_element("srv", {"command": "node", "args": ["server.js"]})
        assert element is not None and element["command"] == "node"


class TestNulCommandIsolation:
    def test_a_nul_command_drops_only_that_server(self, powers):
        _plugin(
            powers,
            "p",
            mcp={
                "bad": {"type": "stdio", "command": "./bad\x00name"},
                "good": {"type": "streamable-http", "url": "https://x.example/mcp"},
            },
            skills=("alpha",),
        )
        report = ip.load_installed_powers()
        assert set(report.mcp_servers()) == {"power-p--good"}
        assert report.skill_roots()


class TestStoreCopyDoesNotResurrect:
    """A marked copy in a source file dies with the loader's derivation."""

    def test_a_marked_store_copy_is_dropped_when_not_derived(self, monkeypatch, tmp_path):
        from kiro_crew.agent_materialization import mcp_sources

        marked = _owned({"url": "https://plugin.example/mcp", "disabledTools": ["one"]})
        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: {})
        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: {})
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {"power-p--s": marked}}), encoding="utf-8")
        monkeypatch.setattr(
            mcp_sources.user_json,
            "load_mcp_servers",
            lambda path: (
                json.loads(store.read_text())["mcpServers"]
                if str(path).endswith("mcp.json")
                else {}
            ),
        )
        config = {"mcpServers": {"power-p--s": dict(marked)}, "tools": ["@power-p--s"]}
        sources = mcp_sources.merge_mcp_sources(config)
        assert "power-p--s" not in config["mcpServers"]
        assert "power-p--s" not in sources.kirocrew
        assert "power-p--s" not in sources.kiro_global

    def test_a_marked_store_copy_is_kept_while_derived(self, monkeypatch):
        from kiro_crew.agent_materialization import mcp_sources

        marked = _owned({"url": "https://plugin.example/mcp", "disabledTools": ["one"]})
        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: {})
        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: {"power-p--s": marked})
        monkeypatch.setattr(
            mcp_sources.user_json, "load_mcp_servers", lambda _path: {"power-p--s": marked}
        )
        config: dict = {"mcpServers": {}, "tools": []}
        sources = mcp_sources.merge_mcp_sources(config)
        assert "power-p--s" in sources.kirocrew

    def test_control_an_unmarked_store_server_is_untouched(self, monkeypatch):
        from kiro_crew.agent_materialization import mcp_sources

        plain = {"url": "https://plain.example/mcp"}
        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: {})
        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: {})
        monkeypatch.setattr(
            mcp_sources.user_json, "load_mcp_servers", lambda _path: {"power-helper": plain}
        )
        config: dict = {"mcpServers": {}, "tools": []}
        sources = mcp_sources.merge_mcp_sources(config)
        assert "power-helper" in sources.kirocrew
        assert "power-helper" in config["mcpServers"]


class TestOverrideStubKeepsThePlugin:
    """A dashboard tool toggle writes a transport-less stub; the plugin stays mounted."""

    def test_a_stub_is_laid_over_the_projection(self, monkeypatch):
        fresh = _owned({"url": "https://plugin.example/mcp"})
        stub = {"disabledTools": ["one"]}
        config: dict = {"mcpServers": {}, "tools": []}
        out = TestRebuildReconcile._merge(
            config, monkeypatch, {"power-p--s": fresh}, shared={"power-p--s": stub}
        )
        entry = out["mcpServers"]["power-p--s"]
        assert is_power_entry(entry)
        assert entry["url"] == "https://plugin.example/mcp"
        assert entry["disabledTools"] == ["one"]
        assert "@power-p--s" in out["tools"]

    def test_a_stub_cannot_change_the_transport(self, monkeypatch):
        fresh = _owned({"url": "https://plugin.example/mcp"})
        stub = {"disabledTools": ["one"], "headers": {"X": "y"}, POWER_KEY: False}
        config: dict = {"mcpServers": {}, "tools": []}
        out = TestRebuildReconcile._merge(
            config, monkeypatch, {"power-p--s": fresh}, shared={"power-p--s": stub}
        )
        entry = out["mcpServers"]["power-p--s"]
        assert "headers" not in entry
        assert entry[POWER_KEY] is True

    def test_control_a_shared_server_with_a_transport_still_blocks(self, monkeypatch):
        fresh = _owned({"url": "https://plugin.example/mcp"})
        other = {"command": "/bin/other"}
        config: dict = {"mcpServers": {}, "tools": []}
        out = TestRebuildReconcile._merge(
            config, monkeypatch, {"power-p--s": fresh}, shared={"power-p--s": other}
        )
        assert not is_power_entry(out["mcpServers"].get("power-p--s"))


class TestKasWithholdsLocalPluginServers:
    def test_local_withheld_remote_kept(self):
        from kiro_crew.mcp_provenance import without_local_power_entries

        servers = {
            "power-p--local": _owned({"command": "node", "args": ["server.js"]}),
            "power-p--remote": _owned({"url": "https://plugin.example/mcp"}),
            "plain": {"command": "node"},
        }
        assert set(without_local_power_entries(servers)) == {"power-p--remote", "plain"}

    def test_the_kas_projection_drops_a_local_plugin_server(self):
        from kiro_crew.acp.kas_agents import _project_mcp_servers

        spec = {
            "mcpServers": {
                "power-p--local": _owned({"command": "node", "args": ["server.js"]}),
                "power-p--remote": _owned({"url": "https://plugin.example/mcp"}),
            }
        }
        out = _project_mcp_servers(spec, "kirocrew", frozenset())
        assert "power-p--local" not in out
        assert "power-p--remote" in out


class TestOneShotSkillReadWaitsForTheScan:
    def test_the_fallback_joins_the_scan_thread(self, monkeypatch):
        from kiro_crew.mcp_tools import skills as skills_tool

        joined: list[float | None] = []

        class _Thread:
            def join(self, timeout=None):
                joined.append(timeout)

        class _Loader:
            _power_scan_thread = _Thread()

            def __init__(self, *a, **k):
                pass

            def read_scoped_skill_page(self, *a, **k):
                raise RuntimeError("stop here")

            def close(self):
                pass

        monkeypatch.setattr(skills_tool.mcp_core, "SkillsLoader", _Loader)
        monkeypatch.setattr(skills_tool.mcp_core, "_signed_session", lambda: "", raising=False)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "")
        try:
            skills_tool.skill_search("skill_search", {"action": "read", "key": "x"})
        except Exception:  # noqa: BLE001 -- only the join matters here
            pass
        assert joined == [skills_tool.PLUGIN_SCAN_WAIT_SECS]


class TestWindowsRelativeCommand:
    def test_a_backslash_relative_command_resolves_inside_the_root(self, powers):
        root = _plugin(powers, "p", mcp={"s": {"type": "stdio", "command": ".\\bin\\server.exe"}})
        spec = ip.load_installed_powers().mcp_servers()["power-p--s"]
        assert Path(spec["command"]) == (root / "bin" / "server.exe").resolve()

    def test_a_backslash_command_that_escapes_is_still_dropped(self, powers):
        _plugin(powers, "p", mcp={"s": {"type": "stdio", "command": ".\\..\\evil.exe"}})
        assert ip.load_installed_powers().mcp_servers() == {}


class TestEverySourceGuardsTheName:
    """A real server under the plugin's key in ANY source keeps the name."""

    @staticmethod
    def _merge_with(monkeypatch, *, store: dict, scope: dict | None = None) -> dict:
        from kiro_crew.agent_materialization import mcp_sources

        fresh = _owned({"url": "https://plugin.example/mcp"})
        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", lambda: {})
        monkeypatch.setattr(ip, "installed_power_mcp_servers", lambda: {"power-p--s": fresh})
        monkeypatch.setattr(mcp_sources, "_extra_mcp_scope_globals", lambda: [Path("/scope")])

        def _load(path):
            path = str(path)
            if path == "/scope":
                return dict(scope or {})
            if path.endswith("crew/mcp.json") or "kirocrew" in path:
                return dict(store)
            return {}

        monkeypatch.setattr(mcp_sources.user_json, "load_mcp_servers", _load)
        monkeypatch.setattr(mcp_sources.agent_mod, "_user_dir", lambda: Path("/home/u/.kiro/crew"))
        config: dict = {"mcpServers": {}, "tools": []}
        mcp_sources.merge_mcp_sources(config)
        return config

    def test_a_store_server_with_its_own_url_keeps_the_name(self, monkeypatch):
        mine = {"url": "https://mine.example/mcp", "oauthScopes": ["read"]}
        out = self._merge_with(monkeypatch, store={"power-p--s": mine})
        entry = out["mcpServers"]["power-p--s"]
        assert not is_power_entry(entry)
        assert entry["url"] == "https://mine.example/mcp"

    def test_a_provider_scope_server_keeps_the_name(self, monkeypatch):
        mine = {"command": "/bin/mine"}
        out = self._merge_with(monkeypatch, store={}, scope={"power-p--s": mine})
        assert not is_power_entry(out["mcpServers"]["power-p--s"])

    def test_control_a_store_stub_does_not_block(self, monkeypatch):
        out = self._merge_with(monkeypatch, store={"power-p--s": {"disabledTools": ["one"]}})
        entry = out["mcpServers"]["power-p--s"]
        assert is_power_entry(entry)
        assert entry["disabledTools"] == ["one"]


class TestMalformedExistingEntry:
    def test_a_non_dict_entry_does_not_abort_the_rebuild(self, monkeypatch):
        fresh = _owned({"url": "https://plugin.example/mcp"})
        config: dict = {"mcpServers": {"power-p--s": 7}, "tools": []}
        out = TestRebuildReconcile._merge(config, monkeypatch, {"power-p--s": fresh})
        assert out["mcpServers"]["power-p--s"] == fresh


class TestDashboardFindsPluginSkills:
    def test_the_finder_serves_a_root_the_loader_adopted(self, tmp_path, monkeypatch):
        import types as _types

        from kiro_crew.dashboard.handlers import _shared_skill_roots as roots_mod

        plugin_skills = tmp_path / "plugin" / "skills"
        (plugin_skills / "alpha").mkdir(parents=True)
        (plugin_skills / "alpha" / "SKILL.md").write_text("# a", encoding="utf-8")
        home = tmp_path / "kirocrew-home"
        (home / "skills").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        loader = _types.SimpleNamespace(_power_extra_paths=[plugin_skills.resolve()])
        # ``_get_skills`` reads the loader off the context builder.
        state = _types.SimpleNamespace(context_builder=_types.SimpleNamespace(skills=loader))
        found = roots_mod._resolve_skill_root("alpha", state)  # type: ignore[arg-type]
        assert found == (plugin_skills / "alpha").resolve()

    def test_control_no_adopted_root_finds_nothing(self, tmp_path, monkeypatch):
        import types as _types

        from kiro_crew.dashboard.handlers import _shared_skill_roots as roots_mod

        home = tmp_path / "kirocrew-home"
        (home / "skills").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        loader = _types.SimpleNamespace(_power_extra_paths=[])
        # ``_get_skills`` reads the loader off the context builder.
        state = _types.SimpleNamespace(context_builder=_types.SimpleNamespace(skills=loader))
        assert roots_mod._resolve_skill_root("alpha", state) is None  # type: ignore[arg-type]

"""Tests for kiro_crew.apps.bridges — resource registration bridges."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew import platform_compat
from kiro_crew.apps.bridges import (
    RegistrationResult,
    _deregister_agents,
    _deregister_skills,
    _namespace,
    _register_agents,
    _register_mcp_servers,
    _register_skills,
    _safe_link_name,
    deregister_app,
    load_app_cron_defs,
    reconcile_app_skills,
    refresh_app_agents,
    register_app,
)
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

# Namespace helpers


class TestNamespace:
    def test_namespace(self):
        assert _namespace("my-app", "agent-1") == "my-app/agent-1"

    def test_safe_link_name(self):
        assert _safe_link_name("my-app/agent-1") == "my-app--agent-1"

    def test_safe_link_name_neutralizes_backslash(self):
        # Windows treats backslash as a separator; it must be flattened too or a
        # resource name could traverse out of the agents dir.
        assert "\\" not in _safe_link_name("my-app/..\\..\\crew\\config")


# Agent registration


class TestAgentRegistration:
    def test_register_agents(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        registered = _register_agents("test-app", manifest, app_root)
        assert len(registered) == 1
        assert "test-app/my-agent" in registered

        # Materialized as a real file, NOT a symlink: the template may live in
        # the read-only Python package (builtins) while the written config
        # carries per-user MCP policy merged in.
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        assert link.is_file()
        assert not link.is_symlink()
        # Content comes from the template
        target = json.loads(link.read_text(encoding="utf-8"))
        assert target["name"] == "my-agent"

    def test_register_agents_leaves_a_foreign_owned_shared_dir_alone(
        self, tmp_path, app_env, monkeypatch
    ):
        """From a data home that does not own the shared agents dir, the app's
        agent is not written there: the owner's specs are left in place."""
        from kiro_crew import agent as agent_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        shared = app_env["kiro_agents"]
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", shared)
        monkeypatch.setattr(agent_mod, "ambient_agents_dir", lambda: shared)
        monkeypatch.setattr(
            agent_mod,
            "_decline_shared_agent_home",
            lambda **_kw: shared / agent_mod.AGENT_FILENAME,
        )

        registered = _register_agents("test-app", manifest, app_root)

        assert registered == []
        assert not (shared / "test-app--my-agent.json").exists()

    def test_deregister_agents(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        _register_agents("test-app", manifest, app_root)

        removed = _deregister_agents("test-app")
        assert removed == 1
        assert not (app_env["kiro_agents"] / "test-app--my-agent.json").exists()

    def test_a_dangling_at_grant_logs_a_warning_and_still_registers(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        """An ``@server`` grant no config declares must be LOUD at registration.

        kiro-cli drops the reference silently at mount time, so this warning is the only
        signal. Warning only: a dangling ref is degradation, and the agent still registers.
        """
        from kiro_crew.apps import bridges as bridges_mod

        # Deterministic ambient: the check falls back to the user's global
        # mcp.json, and the developer's real one must not decide this test.
        monkeypatch.setattr(bridges_mod, "_global_mcp_specs", lambda: {})
        src = _make_app_source(tmp_path)
        (src / "agents" / "my-agent.json").write_text(
            json.dumps({"name": "my-agent", "model": "auto", "tools": ["fs_read", "@ghost/summon"]})
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            registered = _register_agents("test-app", manifest, app_root)

        assert "test-app/my-agent" in registered  # still registers
        warning = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
        assert "silently never mount" in warning
        assert "@ghost/summon" in warning
        assert "my-agent" in warning

    @pytest.mark.parametrize("managed_ref", ["@kirocrew-core", "@kirocrew-core/memory_recall"])
    def test_resolvable_at_grants_log_no_dangling_warning(
        self, tmp_path, app_env, monkeypatch, caplog, managed_ref
    ):
        """The four real resolution sources must not trip the diagnostic.

        Spec-own, host-managed (``_materialize_managed_refs``), the app's own namespaced
        server (``_own_mcp_servers``) and a global-``mcp.json`` server all resolve at mount
        time; warning on them would train operators to ignore the line that matters.
        """
        from kiro_crew.apps import bridges as bridges_mod

        monkeypatch.setattr(bridges_mod, "_global_mcp_specs", lambda: {"ambient-srv": {}})
        src = _make_app_source(tmp_path, mcpServers={"srv": {"command": "echo", "args": []}})
        (src / "agents" / "my-agent.json").write_text(
            json.dumps(
                {
                    "name": "my-agent",
                    "model": "auto",
                    "mcpServers": {"own-srv": {"command": "echo", "args": []}},
                    "tools": [
                        "@own-srv/do_thing",
                        managed_ref,
                        "@ambient-srv",
                        "@test-app:srv",
                    ],
                }
            )
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        # Register the app's own server first, mirroring register_app's order —
        # _own_mcp_servers reads it back from the registered config.
        _register_mcp_servers("test-app", manifest, live_port=None)

        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            _register_agents("test-app", manifest, app_root)

        assert "silently never mount" not in caplog.text

    def test_include_mcp_json_false_does_not_resolve_against_global_config(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        """A spec that opts out of the global mcp.json gets no ambient rescue.

        With ``includeMcpJson: false`` kiro-cli never consults the global
        config at mount time, so a grant that only a global entry could
        satisfy is dropped — treating the ambient entry as resolvable would
        suppress the warning for exactly the specs that set this flag.
        """
        from kiro_crew.apps import bridges as bridges_mod

        monkeypatch.setattr(bridges_mod, "_global_mcp_specs", lambda: {"ambient-srv": {}})
        src = _make_app_source(tmp_path)
        (src / "agents" / "my-agent.json").write_text(
            json.dumps(
                {
                    "name": "my-agent",
                    "model": "auto",
                    "includeMcpJson": False,
                    "tools": ["@ambient-srv"],
                }
            )
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            _register_agents("test-app", manifest, app_root)

        assert "silently never mount" in caplog.text
        assert "@ambient-srv" in caplog.text

    def test_a_failed_rewrite_leaves_the_prior_config_intact(self, tmp_path, app_env, monkeypatch):
        """A rebuild must never destroy the working config before its replacement is durable.

        Unlinking first and then failing a disk-full write makes the agent DISAPPEAR, so the
        file is left for atomic_write's rename to swap.
        """
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        # First registration writes a good config.
        _register_agents("test-app", manifest, app_root)
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        good = link.read_text(encoding="utf-8")
        assert link.is_file()

        # Now make the next write fail, as a full disk would.
        def _boom(*a, **k):
            raise OSError("No space left on device")

        monkeypatch.setattr(bridges_mod, "atomic_write", _boom)
        _register_agents("test-app", manifest, app_root)  # must swallow the OSError

        assert link.is_file(), "the working config must survive a failed rewrite"
        assert link.read_text(encoding="utf-8") == good, "…with its old contents intact"

    @pytest.mark.parametrize("content", ["[1, 2, 3]", "42", "null", "true", '"a string"'])
    def test_a_valid_json_non_object_agent_spec_is_skipped(self, tmp_path, app_env, content):
        """A spec that is valid JSON but not an object parses fine, so the
        JSONDecodeError guard never fires — but ``.get`` on the parsed value
        would raise AttributeError and take down the whole registration pass.
        Same disposition as the unreadable case: skip that agent, register
        nothing for it, and do not crash.
        """
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        (app_root / "agents" / "my-agent.json").write_text(content, encoding="utf-8")

        registered = _register_agents("test-app", manifest, app_root)

        assert registered == []
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        assert not link.exists(), "a spec that was never understood must not be materialized"

    def test_a_legacy_symlink_is_still_replaced(self, tmp_path, app_env):
        """A symlink from an older Kiro Crew is dropped and replaced with a real file."""
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        # Simulate the legacy layout: a symlink where the real file should be.
        legacy_target = tmp_path / "legacy-agent.json"
        legacy_target.write_text("{}", encoding="utf-8")
        try:
            link.symlink_to(legacy_target)
        except OSError:
            import pytest

            pytest.skip("symlinks unavailable")
        _register_agents("test-app", manifest, app_root)
        assert link.is_file() and not link.is_symlink(), "legacy symlink must become a real file"

    def test_register_mcp_strips_a_governed_autoapprove(self, tmp_path, app_env, monkeypatch):
        """The app mcp.json is read by kiro-cli, so a governed `autoApprove` here
        would auto-approve locally and bypass the gate. It must be stripped before
        the write, exactly like the agent-config writers do."""
        from kiro_crew.apps import bridges as bridges_mod
        from kiro_crew.platform import governance as gov

        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: False)  # governed
        src = _make_app_source(
            tmp_path,
            mcpServers={"srv": {"command": "run", "args": [], "autoApprove": ["danger"]}},
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        bridges_mod._register_mcp_servers("test-app", manifest)
        written = json.loads(bridges_mod._mcp_json_path().read_text(encoding="utf-8"))
        entry = written["mcpServers"]["test-app:srv"]
        assert "autoApprove" not in entry, "a governed grant must not reach the file kiro-cli reads"

    def test_a_malformed_agent_spec_does_not_abort_the_apps_other_agents(
        self, tmp_path, app_env
    ):
        """One bad `mcpServers` must not cost the app every agent after it.

        `_register_agents` loops over the manifest's agents, so anything that raises
        on one spec stops the LATER, valid ones from being materialized and the app
        silently loses capability it declared. A value that is not a mapping is
        carried through to the writer, which skips what it cannot read.
        """
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(
            tmp_path, agents=["agents/bad-agent.json", "agents/my-agent.json"]
        )
        # A LIST of mappings: truthy, so `or {}` does not neutralize it, and its
        # elements are unhashable, so building a name set from it raises.
        (src / "agents" / "bad-agent.json").write_text(
            json.dumps({"name": "bad-agent", "model": "auto", "mcpServers": [{"srv": {}}]})
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        registered = bridges_mod._register_agents("test-app", manifest, app_root)
        assert any(
            "my-agent" in name for name in registered
        ), f"the valid agent was not materialized: {registered}"

    def test_register_mcp_never_keeps_an_apps_own_autoapprove(
        self, tmp_path, app_env, monkeypatch
    ):
        """The opt-in honours the OWNER's list, and an app's own is not the owner's.

        A manifest's ``autoApprove`` is chosen by the app and would exempt a third party's
        tools from the approval gate, so it is stripped whatever ``mcp.honour_auto_approve``
        says; that key defaults ON.
        """
        from kiro_crew.apps import bridges as bridges_mod
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import governance as gov

        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: True)  # ungoverned
        cfg = KiroCrewConfig()
        cfg.mcp.honour_auto_approve = True
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        src = _make_app_source(
            tmp_path,
            mcpServers={"srv": {"command": "run", "args": [], "autoApprove": ["ok"]}},
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        bridges_mod._register_mcp_servers("test-app", manifest)
        written = json.loads(bridges_mod._mcp_json_path().read_text(encoding="utf-8"))
        entry = written["mcpServers"]["test-app:srv"]
        assert "autoApprove" not in entry, "an app's own grant must not reach the file kiro-cli reads"
        assert entry["command"] == "run", "the server itself must stay available"

    def test_missing_agent_file_skipped(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, agents=["agents/nonexistent.json"])
        # Don't create the file
        (src / "agents").mkdir(exist_ok=True)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        registered = _register_agents("test-app", manifest, app_root)
        assert registered == []

    def test_agent_name_with_path_separator_is_refused(self, tmp_path, app_env):
        # An app-controlled agent name carrying a path separator (a Windows
        # backslash escape here) must be refused before it becomes a filesystem
        # path — otherwise atomic_write could overwrite an arbitrary JSON file
        # outside the agents dir (e.g. ~/.kiro/crew/config.json).
        src = _make_app_source(tmp_path, agents=["agents/evil.json"])
        (src / "agents").mkdir(exist_ok=True)
        (src / "agents" / "evil.json").write_text(
            json.dumps({"name": "..\\..\\..\\escape\\pwned", "model": "auto"})
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        before = set(app_env["kiro_agents"].rglob("*"))
        registered = _register_agents("test-app", manifest, app_root)

        assert registered == []  # refused, not registered
        # Nothing new written anywhere under (or via traversal out of) the dir.
        assert set(app_env["kiro_agents"].rglob("*")) == before
        assert not (tmp_path / "escape").exists()


# Skill registration


class TestSkillRegistration:
    def test_register_skills(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        registered = _register_skills("test-app", manifest, app_root)
        assert len(registered) == 1
        assert "test-app/my-skill" in registered

        # A link exists under ~/.kiro/crew/skills/test-app/my-skill: a symlink on
        # POSIX, a directory junction on non-admin Windows (both resolve through).
        skill_link = app_env["home"] / "skills" / "test-app" / "my-skill"
        assert platform_compat.is_link_or_junction(skill_link)
        assert (skill_link / "SKILL.md").is_file()

    def test_deregister_skills(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        _register_skills("test-app", manifest, app_root)

        _deregister_skills("test-app")
        assert not (app_env["home"] / "skills" / "test-app").exists()

    def test_missing_skill_dir_skipped(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, skills=["skills/nonexistent"])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        registered = _register_skills("test-app", manifest, app_root)
        assert registered == []

    def test_no_skills_creates_no_directory(self, tmp_path, app_env):
        """An app with no manifest skills must not leave an empty skills dir.

        An empty dir MASKS a PACKAGED builtin skill of the same name: app registration runs
        after `_ensure_builtin_skills`, and the dir has no `SKILL.md`, so the SOPs the app's
        cron prompts reference silently do not exist (ops-mission-control ships this way).
        """
        src = _make_app_source(tmp_path, skills=[])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        registered = _register_skills("test-app", manifest, app_root)
        assert registered == []
        assert not (app_env["home"] / "skills" / "test-app").exists()

    def test_no_skills_does_not_clobber_a_packaged_skill(self, tmp_path, app_env):
        """The regression itself: registration must not empty a same-named skill."""
        packaged = app_env["home"] / "skills" / "test-app"
        packaged.mkdir(parents=True)
        (packaged / "SKILL.md").write_text("---\nname: test-app\n---\nbody\n")
        (packaged / "sops").mkdir()
        (packaged / "sops" / "dispatch.md").write_text("# SOP\n")

        src = _make_app_source(tmp_path, skills=[])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_skills("test-app", manifest, app_env["home"] / "apps" / "test-app")

        assert (packaged / "SKILL.md").is_file()
        assert (packaged / "sops" / "dispatch.md").is_file()

    def test_deregister_preserves_a_same_named_packaged_skill(self, tmp_path, app_env):
        """Deregister must not delete a same-named packaged skill.

        `sync_app_skills` calls `_deregister_skills` for an app declaring no skills; for a
        builtin whose packaged skill shares the name, `skills/<app_name>/` holds real files,
        not links, and an `rmtree` silently deletes every SOP under it.
        """
        packaged = app_env["home"] / "skills" / "test-app"
        packaged.mkdir(parents=True)
        (packaged / "SKILL.md").write_text("---\nname: test-app\n---\nbody\n")
        (packaged / "sops").mkdir()
        (packaged / "sops" / "reconcile.md").write_text("# SOP\n")

        removed = _deregister_skills("test-app")

        assert removed == 0, "no app-owned links existed to remove"
        assert (packaged / "SKILL.md").is_file(), "packaged skill must survive"
        assert (packaged / "sops" / "reconcile.md").is_file()

    def test_deregister_removes_only_symlinks_leaving_real_files(self, tmp_path, app_env):
        """The mixed case: an app link AND a packaged file under the same dir."""
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_skills("test-app", manifest, app_env["home"] / "apps" / "test-app")

        # A real file lands in the same namespaced dir (as a packaged builtin would).
        app_dir_path = app_env["home"] / "skills" / "test-app"
        (app_dir_path / "real.md").write_text("not a link\n")

        _deregister_skills("test-app")

        assert not (app_dir_path / "my-skill").exists(), "the app symlink is gone"
        assert (app_dir_path / "real.md").is_file(), "the real file survives"


# Owning app of each app-skill link, for tools that run without a gateway


def _install_skill_app(tmp_path, name, skills):
    src = _make_app_source(tmp_path, name=name, skills=[f"skills/{s}" for s in skills])
    for skill in skills:
        (src / "skills" / skill).mkdir(parents=True, exist_ok=True)
        (src / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n")
    install_app(src)


def _register(app_env, name):
    app_root = app_env["home"] / "apps" / name
    _register_skills(name, AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME), app_root)


def _skill_links(app_env):
    data = json.loads((app_env["home"] / "skills" / ".app-skill-links.json").read_text())
    assert data["format"] == 1
    return {rel: entry["app"] for rel, entry in data["links"].items()}


class TestAppSkillLinks:
    def test_a_flat_link_names_the_app_that_registered_it_last(self, tmp_path, app_env):
        _install_skill_app(tmp_path, "app-one", ["shared"])
        _register(app_env, "app-one")
        assert _skill_links(app_env) == {"app-one/shared": "app-one", "shared": "app-one"}

        _install_skill_app(tmp_path, "app-two", ["shared"])
        _register(app_env, "app-two")
        assert _skill_links(app_env) == {
            "app-one/shared": "app-one",
            "app-two/shared": "app-two",
            "shared": "app-two",
        }

    def test_a_removed_skill_and_a_deregistered_app_drop_their_entries(self, tmp_path, app_env):
        _install_skill_app(tmp_path, "test-app", ["kept", "dropped"])
        _register(app_env, "test-app")
        manifest_path = app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        manifest["skills"] = ["skills/kept"]
        manifest_path.write_text(json.dumps(manifest))

        reconcile_app_skills("test-app")
        assert _skill_links(app_env) == {"test-app/kept": "test-app", "kept": "test-app"}

        _deregister_skills("test-app")
        assert _skill_links(app_env) == {}

    def test_reconcile_rebuilds_a_corrupt_file_with_only_app_links(self, tmp_path, app_env):
        # The user's own skills: a real directory with the app skill's name (so no flat
        # link is made), and a folder of links named like no app.
        skills_root = app_env["home"] / "skills"
        (skills_root / "my-skill").mkdir(parents=True)
        drafts = tmp_path / "drafts" / "notes"
        drafts.mkdir(parents=True)
        (skills_root / "personal").mkdir()
        platform_compat.symlink_or_junction(str(drafts), str(skills_root / "personal" / "notes"))
        _install_skill_app(tmp_path, "test-app", ["my-skill"])
        _register(app_env, "test-app")
        (skills_root / ".app-skill-links.json").write_text("{ not json")

        reconcile_app_skills("test-app")

        assert _skill_links(app_env) == {"test-app/my-skill": "test-app"}

    @pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
    def test_junction_links_are_recorded(self, tmp_path, app_env, monkeypatch):
        _install_skill_app(tmp_path, "test-app", ["my-skill"])

        def _refused(*_args, **_kwargs):
            # A non-admin Windows account holds no symlink privilege, so links are junctions.
            raise OSError(1314, "A required privilege is not held by the client")

        monkeypatch.setattr(os, "symlink", _refused)
        _register(app_env, "test-app")

        assert _skill_links(app_env) == {"test-app/my-skill": "test-app", "my-skill": "test-app"}


# Top-level register / deregister


class TestTopLevel:
    def test_register_app(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = register_app("test-app")
        assert len(result.agents) == 1
        assert len(result.skills) == 1
        assert len(result.crons) == 1
        assert result.errors == []

    def test_register_app_reports_zero_registered_when_agent_source_missing(
        self, tmp_path, app_env
    ):
        # A manifest that DECLARES agents but whose agent source is absent /
        # unreadable materializes none. That must surface as a visible error
        # (which reconcile counts), not a silent 0-agent success.
        src = _make_app_source(tmp_path)
        install_app(src)
        # Drop the declared agent's source from the installed snapshot.
        (app_env["home"] / "apps" / "test-app" / "agents" / "my-agent.json").unlink()

        result = register_app("test-app")

        assert result.agents == []
        assert any("0 of" in error and "test-app" in error for error in result.errors)

    def test_refresh_app_agents_denied_scrubs_and_registers_nothing(
        self, tmp_path, app_env, monkeypatch
    ):
        # An app whose execution admission was revoked must NOT be re-materialized
        # by the from-source recovery path: refresh_app_agents must honor the same
        # gate register_app does -- scrub any stale agent spec and register nothing
        # -- or a revoked app's agent (and its merged MCP servers) becomes
        # dispatchable again.
        import kiro_crew.apps.execution as execution_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        assert refresh_app_agents("test-app")  # admitted: materializes, returns names
        assert any(app_env["kiro_agents"].iterdir())

        monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: False)
        refreshed = refresh_app_agents("test-app")

        assert refreshed == []
        # The agent-spec lock file spec writers share is not a spec.
        assert not list(app_env["kiro_agents"].glob("*.json"))

    def test_install_while_execution_denied_registers_nothing(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.execution as execution_mod

        src = _make_app_source(
            tmp_path,
            mcpServers={"stdio": {"command": "python", "args": ["server.py"]}},
        )
        install_app(src)
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )

        result = register_app("test-app")

        assert result.agents == []
        assert result.skills == []
        assert result.crons == []
        assert result.mcp_servers == []
        assert any("blocked by execution policy" in error for error in result.errors)
        assert not any(app_env["kiro_agents"].iterdir())
        assert not (app_env["home"] / "skills" / "test-app").exists()
        assert load_app_cron_defs("test-app") == []
        assert not (tmp_path / "mcp.json").exists()

    def test_register_nonexistent_app(self, app_env):
        result = register_app("nonexistent")
        assert len(result.errors) > 0

    def test_register_app_resources_app_skips_all(self, tmp_path, app_env, monkeypatch):
        """Apps with resources='app' manage their own registration.

        register_app must skip all bridge work: a namespaced <app>--<agent>.json with empty
        mcpServers beside the app's real agent file makes kiro-cli load the empty one.
        """
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "backend": {"url": "http://localhost:8080/mcp"},
            },
        )
        install_app(src)

        # Mark as self-managed (like Mochi does via registerExternal)
        from kiro_crew.apps.manager import register_external_app

        register_external_app("test-app", "1.0.0", "Test App", resources="app")

        result = register_app("test-app")

        assert result.agents == []
        assert result.skills == []
        assert result.crons == []
        assert result.mcp_servers == []
        assert result.errors == []

        assert not any(f.name.startswith("test-app--") for f in app_env["kiro_agents"].iterdir())
        assert not (app_env["home"] / "skills" / "test-app").exists()
        assert not mcp_path.exists()

    def test_deregister_app(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        register_app("test-app")
        result = deregister_app("test-app")
        assert result.errors == []
        assert not any(f.name.startswith("test-app--") for f in app_env["kiro_agents"].iterdir())

    def test_register_deregister_cycle(self, tmp_path, app_env):
        """Register, deregister, re-register — no stale state."""
        src = _make_app_source(tmp_path)
        install_app(src)

        r1 = register_app("test-app")
        assert len(r1.agents) == 1

        deregister_app("test-app")
        assert not any(f.name.startswith("test-app--") for f in app_env["kiro_agents"].iterdir())

        r2 = register_app("test-app")
        assert len(r2.agents) == 1


# RegistrationResult


class TestRegistrationResult:
    def test_to_dict(self):
        r = RegistrationResult(agents=["a/b"], skills=["a/s"], crons=["a/c"], errors=[])
        d = r.to_dict()
        assert d["agents"] == ["a/b"]
        assert d["errors"] == []


# Stdio interpreter resolution (one policy shared with the backend launcher)


def _fake_venv_python(app_root: Path) -> Path:
    """Create a runnable venv interpreter at the platform's expected location.

    Non-empty and executable: the resolver rejects zero-byte files and files without the
    execute bit. The usability PROBE is stubbed by ``_stub_venv_probe`` and pinned in
    ``test_apps_backend_coverage_dispatch.TestInterpreterResolution``.
    """
    if platform_compat.IS_WINDOWS:
        py = app_root / ".venv" / "Scripts" / "python.exe"
    else:
        py = app_root / ".venv" / "bin" / "python3"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    return py


class TestStdioInterpreterResolution:
    """The stdio MCP registration path must apply the SAME interpreter policy as the
    app backend launcher: the app's own venv interpreter first (that is where its
    requirements were installed), else the gateway's ``sys.executable`` — never a bare
    PATH-resolved name. The rewrite predicate is deliberately narrow; both sides of
    the boundary are pinned here because getting it wrong breaks working apps."""

    def _register_stdio(self, tmp_path, app_env, monkeypatch, server_cfg, *, setup=None):
        """Install an app with one stdio server and return its written entry.

        ``setup(app_root)`` runs AFTER install (install_app removes a
        pre-existing dest dir, so a venv must be created post-install).
        """
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        src = _make_app_source(tmp_path, mcpServers={"srv": server_cfg})
        install_app(src)
        if setup is not None:
            setup(app_env["home"] / "apps" / "test-app")
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:srv"]
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        return data["mcpServers"]["test-app:srv"]

    def test_bare_python_resolves_to_the_app_venv_interpreter(
        self, tmp_path, app_env, monkeypatch, nonbundled_python_without_user_site
    ):
        import sys

        created: list[Path] = []
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3", "args": ["-m", "myapp.server"]},
            setup=lambda root: created.append(_fake_venv_python(root)),
        )
        assert entry["command"] == str(created[0])
        assert entry["command"] != sys.executable
        # The module path survives after the shared user-site isolation flag.
        assert entry["args"] == ["-s", "-m", "myapp.server"]

    def test_bare_python_falls_back_to_sys_executable_without_a_venv(
        self, tmp_path, app_env, monkeypatch
    ):
        import sys

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3", "args": ["-m", "myapp.server"]},
        )
        assert entry["command"] == sys.executable

    def test_an_absolute_command_is_never_rewritten(self, tmp_path, app_env, monkeypatch):
        # Even with a venv present: an explicit path was a deliberate choice.
        cmd = "C:\\tools\\srv.exe" if platform_compat.IS_WINDOWS else "/usr/local/bin/srv"
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": cmd, "args": []},
            setup=_fake_venv_python,
        )
        assert entry["command"] == cmd

    def test_a_bare_path_dependency_the_venv_lacks_is_left_alone(
        self, tmp_path, app_env, monkeypatch
    ):
        # `node` is a legitimate PATH dependency; the app's venv does not provide
        # it, so rewriting would break a working app.
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "node", "args": ["server.js"]},
            setup=_fake_venv_python,
        )
        assert entry["command"] == "node"

    def test_a_path_carrying_command_never_reaches_the_venv_lookup(
        self, tmp_path, app_env, monkeypatch
    ):
        # The no-path-separator guard is a security boundary, not a nicety: a
        # traversal-shaped command (`../data/evil`) must never be joined under
        # `.venv/bin/` and rewritten to an absolute path outside the venv, even
        # when the joined target happens to exist.
        def plant_traversal_target(app_root: Path) -> None:
            # A real venv layout (bin/ exists), plus a RUNNABLE file OUTSIDE
            # bin/ that a naive `.venv/bin / <command>` join would reach via
            # `..` — executable, so the separator guard is the only defence.
            _fake_venv_python(app_root)
            target = (app_root / ".venv" / "bin" / ".." / "data" / "evil").resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "../data/evil", "args": []},
            setup=plant_traversal_target,
        )
        assert entry["command"] == "../data/evil"

    def test_a_venv_provided_console_script_is_resolved(self, tmp_path, app_env, monkeypatch):
        # A console script pip installed into the app venv is invisible to PATH
        # (the venv is never activated) — resolving it is what makes such a
        # manifest work at all.
        created: list[Path] = []

        def make_script(app_root: Path) -> None:
            _fake_venv_python(app_root)
            if platform_compat.IS_WINDOWS:
                script = app_root / ".venv" / "Scripts" / "my-mcp-server.exe"
            else:
                script = app_root / ".venv" / "bin" / "my-mcp-server"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
            created.append(script)

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "my-mcp-server", "args": []},
            setup=make_script,
        )
        assert entry["command"] == str(created[0])

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="Windows has no execute bit to drop")
    def test_a_non_executable_venv_interpreter_is_not_used(self, tmp_path, app_env, monkeypatch):
        # A venv python that lost its execute bit (truncated copy, permissions
        # dropped in transit) must not displace the always-runnable
        # sys.executable fallback — rewriting to it guarantees EACCES at spawn.
        import sys

        def make_broken_venv(app_root: Path) -> None:
            py = _fake_venv_python(app_root)
            py.chmod(0o644)

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "python3", "args": []},
            setup=make_broken_venv,
        )
        assert entry["command"] == sys.executable

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="Windows has no execute bit to drop")
    def test_a_non_executable_venv_file_never_displaces_a_path_command(
        self, tmp_path, app_env, monkeypatch
    ):
        # A non-runnable venv file with a matching name (a data artifact, a
        # partial pip install) must not hijack a command PATH would satisfy.
        def make_data_artifact(app_root: Path) -> None:
            artifact = app_root / ".venv" / "bin" / "node"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("not a binary")
            artifact.chmod(0o644)

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "node", "args": ["server.js"]},
            setup=make_data_artifact,
        )
        assert entry["command"] == "node"

    def test_a_gateway_module_server_is_pinned_to_the_gateway_interpreter(
        self, tmp_path, app_env, monkeypatch
    ):
        # A stdio server that runs Kiro Crew's OWN code (`-m kiro_crew...`) must
        # run under the gateway's interpreter even when the app has a venv: app
        # venvs are created without --system-site-packages, so kiro_crew is not
        # importable there and the venv interpreter dies on import — silently,
        # since the rewritten venv path exists and no warning fires.
        import sys

        entry = self._register_stdio(
            tmp_path,
            app_env,
            monkeypatch,
            {"command": "python3", "args": ["-s", "-m", "kiro_crew.apps.builtins.x.server"]},
            setup=_fake_venv_python,
        )
        assert entry["command"] == sys.executable

    def test_manifest_pythonpath_keeps_user_site_for_gateway_module(
        self,
        tmp_path,
        app_env,
        monkeypatch,
        nonbundled_python_with_user_site,
    ):
        import sys

        entry = self._register_stdio(
            tmp_path,
            app_env,
            monkeypatch,
            {
                "command": "python3",
                "args": ["-m", "kiro_crew.apps.builtins.x.server"],
                "env": {"PYTHONPATH": "/manifest/own"},
            },
        )
        assert entry["command"] == sys.executable
        assert entry["args"] == ["-m", "kiro_crew.apps.builtins.x.server"]
        assert entry["env"]["PYTHONPATH"] == "/manifest/own"

    def test_a_windows_exe_spelling_of_python_gets_the_interpreter_policy(
        self, tmp_path, app_env, monkeypatch
    ):
        # `python.exe` is an ordinary Windows spelling of the same launcher; it
        # must resolve through the interpreter policy, not miss the venv via a
        # doubled `.exe` probe and fall through to PATH.
        created: list[Path] = []
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python.exe", "args": []},
            setup=lambda root: created.append(_fake_venv_python(root)),
        )
        assert entry["command"] == str(created[0])

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="drive-qualified names are a Windows path form",
    )
    def test_a_drive_qualified_command_is_never_rewritten(self, tmp_path, app_env, monkeypatch):
        # `D:foo` carries no separator but names a different drive; joining it
        # under `.venv\Scripts` would DISCARD the venv anchor (pathlib treats
        # the right operand as a new anchor), so the guard must reject it.
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "D:foo", "args": []},
            setup=_fake_venv_python,
        )
        assert entry["command"] == "D:foo"

    def test_a_zero_byte_venv_interpreter_is_not_used(self, tmp_path, app_env, monkeypatch):
        # The interrupted-copy / Store-stub shape: a zero-byte python.exe
        # passes the Windows extension check (there is no execute bit), so the
        # resolver must reject empty files on every platform.
        import sys

        def make_stub_venv(app_root: Path) -> None:
            py = _fake_venv_python(app_root)
            py.write_text("")  # truncate to zero bytes, still chmod +x

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "python3", "args": []},
            setup=make_stub_venv,
        )
        assert entry["command"] == sys.executable

    def test_a_script_argument_named_dash_m_does_not_trigger_the_gateway_pin(
        self, tmp_path, app_env, monkeypatch
    ):
        # `python3 server.py -m kiro_crew.mode`: the -m belongs to the SCRIPT
        # (CPython stops parsing its own options at the first operand), so the
        # entry must resolve venv-first — pinning it to sys.executable would
        # strand a venv-dependent server without its dependencies.
        created: list[Path] = []
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3", "args": ["server.py", "-m", "kiro_crew.mode"]},
            setup=lambda root: created.append(_fake_venv_python(root)),
        )
        assert entry["command"] == str(created[0])

    def test_interpreter_options_before_dash_m_still_trigger_the_pin(
        self, tmp_path, app_env, monkeypatch
    ):
        import sys

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3", "args": ["-s", "-u", "-m", "kiro_crew.apps.x"]},
            setup=_fake_venv_python,
        )
        assert entry["command"] == sys.executable

    def test_separate_value_options_do_not_break_the_pin_scan(self, tmp_path, app_env, monkeypatch):
        # -X / -W / --check-hash-based-pycs consume the NEXT token as a value;
        # the scanner must skip that value instead of reading it as the script
        # operand and missing the -m that follows.
        import sys

        entry = self._register_stdio(
            tmp_path,
            app_env,
            monkeypatch,
            {"command": "python3", "args": ["-X", "dev", "-W", "ignore", "-m", "kiro_crew.apps.x"]},
            setup=_fake_venv_python,
        )
        assert entry["command"] == sys.executable

    def test_an_attached_module_spelling_triggers_the_pin(self, tmp_path, app_env, monkeypatch):
        # CPython accepts -mMODULE in one token.
        import sys

        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3", "args": ["-mkiro_crew.apps.x"]},
            setup=_fake_venv_python,
        )
        assert entry["command"] == sys.executable

    def test_a_dash_c_program_never_triggers_the_pin(self, tmp_path, app_env, monkeypatch):
        # -c consumes the rest as an inline program; an -m after it belongs to
        # that program's argv, not to CPython. The attached spelling
        # (-cPROGRAM) followed directly by dash tokens is the case where only
        # the -c stop prevents a wrong pin — the following token is not a
        # non-dash operand, so no other branch would halt the scan.
        created: list[Path] = []
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3",
             "args": ["-cimport server", "-m", "kiro_crew.x"]},
            setup=lambda root: created.append(_fake_venv_python(root)),
        )
        assert entry["command"] == str(created[0])

    def test_an_option_value_that_looks_like_a_script_stays_venv_first(
        self, tmp_path, app_env, monkeypatch
    ):
        # `-X importtime server.py`: the scan must not treat `importtime` as
        # the operand, but `server.py` IS one — no pin, venv-first applies.
        created: list[Path] = []
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "python3", "args": ["-X", "importtime", "server.py"]},
            setup=lambda root: created.append(_fake_venv_python(root)),
        )
        assert entry["command"] == str(created[0])

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="drive-qualified names are a Windows path form",
    )
    def test_a_missing_drive_qualified_command_logs_the_warning(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        # `D:missing` carries no separator but names a location; it is never
        # rewritten AND it must not silently skip the unresolvable diagnostic.
        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            entry = self._register_stdio(
                tmp_path, app_env, monkeypatch, {"command": "D:missing", "args": []},
            )
        assert entry["command"] == "D:missing"
        assert "resolves to no existing executable" in caplog.text

    def test_nonbundled_host_cli_pin_forces_isolation_with_pythonpath(
        self, tmp_path, app_env, monkeypatch, nonbundled_python_with_user_site
    ):
        import sys

        # This path supplies the package root through PYTHONPATH, so it keeps
        # its unconditional `-s` guard even when the host interpreter normally
        # allows user-site packages.
        # `kirocrew` is pinned to `sys.executable -m kiro_crew` by
        # _pin_host_cli_command BEFORE stdio resolution; the venv must not
        # override that (the host CLI is gateway code, not app code).
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch,
            {"command": "kirocrew", "args": ["app", "mcp", "test-app"]},
            setup=_fake_venv_python,
        )
        assert entry["command"] == sys.executable
        assert entry["args"][:4] == ["-s", "-P", "-m", "kiro_crew"]

    def test_an_http_entry_is_unaffected(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod

        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"url": "http://localhost:9000/mcp"}
        )
        assert "command" not in entry

    def test_no_cwd_key_is_emitted_for_stdio_entries(self, tmp_path, app_env, monkeypatch):
        # kiro-cli's documented local-server schema has no `cwd` property and
        # (verified empirically against kiro-cli 2.17.0) a `cwd` key is parsed
        # but IGNORED at spawn time — the server starts in the invocation dir
        # regardless. Writing a key that claims to set the working directory but
        # provably does nothing would be a standing lie in the agent config, and
        # risks a stricter future parser rejecting the whole agent file. Keep
        # the entry schema-clean until kiro-cli grows real support.
        entry = self._register_stdio(
            tmp_path, app_env, monkeypatch, {"command": "python3", "args": []},
            setup=_fake_venv_python,
        )
        assert "cwd" not in entry

    def test_an_unresolvable_path_command_logs_a_warning_and_still_registers(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        # kiro-cli reports nothing when a
        # spawn fails, so registration is the one place a bad command can be
        # surfaced. Warn — but never raise, and still write the entry. Only a
        # path-carrying command is probed (one stat): a bare name is not, because
        # PATH at spawn time is not the gateway's PATH, the binary may be
        # installed later (onEnable, node_modules/.bin), and walking PATH with
        # shutil.which from this event-loop-reachable path can block on a
        # network-mounted PATH entry.
        missing = str(tmp_path / "definitely" / "not-a-real-binary-1807")
        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            entry = self._register_stdio(
                tmp_path, app_env, monkeypatch,
                {"command": missing, "args": []},
            )
        assert entry["command"] == missing
        warning = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
        assert "test-app" in warning
        assert "srv" in warning
        # The message renders the command with %r, so Windows backslashes appear
        # escaped — compare against the repr form, not the raw path.
        assert repr(missing) in warning

    def test_a_bare_name_is_not_probed_and_logs_no_warning(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            entry = self._register_stdio(
                tmp_path, app_env, monkeypatch,
                {"command": "definitely-not-a-real-binary-1807", "args": []},
            )
        assert entry["command"] == "definitely-not-a-real-binary-1807"
        assert "resolves to no existing executable" not in caplog.text

    def test_the_probe_is_offloaded_when_an_event_loop_is_running(
        self, tmp_path, app_env, monkeypatch
    ):
        # The probe stats a caller-supplied path, which can block in the
        # kernel on a dead network mount; on the loop thread that freezes
        # every task until the stall watchdog kills the gateway. With a
        # running loop the probe must go to the maintenance pool; with none
        # (this test's own direct call) it runs inline.
        import kiro_crew.apps.bridges as bmod

        dispatched: list[tuple] = []

        class _FakeLoop:
            def run_in_executor(self, executor, fn, *args):
                dispatched.append((executor, fn, args))

        monkeypatch.setattr(
            bmod.asyncio, "get_running_loop", lambda: _FakeLoop()
        )
        cfg = {"command": str(tmp_path / "nope" / "bin"), "args": []}
        bmod._schedule_unresolvable_warning("app", "srv", cfg)
        assert len(dispatched) == 1
        _, fn, args = dispatched[0]
        assert fn is bmod._warn_unresolvable_stdio_command
        # The entry is snapshotted: registration mutates cfg after scheduling.
        assert args[2] is not cfg and args[2] == cfg

        # No loop -> inline (RuntimeError path).
        monkeypatch.setattr(
            bmod.asyncio, "get_running_loop",
            lambda: (_ for _ in ()).throw(RuntimeError("no loop")),
        )
        dispatched.clear()
        bmod._schedule_unresolvable_warning("app", "srv", cfg)
        assert dispatched == []

    def test_a_resolvable_command_logs_no_unresolvable_warning(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            self._register_stdio(
                tmp_path, app_env, monkeypatch, {"command": "python3", "args": []},
                setup=_fake_venv_python,
            )
        assert "resolves to no existing executable" not in caplog.text

    def test_a_secret_uri_env_value_logs_a_warning_and_registers_unchanged(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        # kiro-cli never resolves secret://, so an app stdio server it spawns
        # gets the literal. The warning states that gap without prescribing a
        # route to vault access (that trust model is undecided), names the env
        # KEY only, never the secret name, and leaves the declared value as is.
        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            entry = self._register_stdio(
                tmp_path, app_env, monkeypatch,
                {
                    "command": "python3",
                    "args": [],
                    "env": {"API_TOKEN": "secret://hidden-name-10641", "MODE": "x"},
                },
                setup=_fake_venv_python,
            )
        assert entry["env"]["API_TOKEN"] == "secret://hidden-name-10641"
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        hits = [w for w in warnings if "secret://" in w]
        assert len(hits) == 1
        assert "test-app" in hits[0] and "'srv'" in hits[0]
        assert "'API_TOKEN'" in hits[0]
        assert "'MODE'" not in hits[0]
        assert "hidden-name-10641" not in caplog.text
        assert "kiro-cli spawns the server directly" in hits[0]
        assert "undecided" in hits[0]
        assert "stub" not in hits[0].lower()

    def test_a_plain_env_logs_no_secret_uri_warning(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        with caplog.at_level("WARNING", logger="kiro_crew.apps.bridges"):
            self._register_stdio(
                tmp_path, app_env, monkeypatch,
                {"command": "python3", "args": [], "env": {"MODE": "secret:/not-a-ref"}},
                setup=_fake_venv_python,
            )
        assert "secret:// reference" not in caplog.text

    def test_one_bad_server_does_not_block_its_siblings(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        src = _make_app_source(
            tmp_path,
            mcpServers={
                "bad": {"command": "definitely-not-a-real-binary-1807", "args": []},
                "good": {"command": "python3", "args": ["-m", "x"]},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert sorted(registered) == ["test-app:bad", "test-app:good"]


class TestBackendSharesTheInterpreterPolicy:
    """The backend launcher and the stdio MCP registration must keep answering
    identically — two divergent interpreter policies are a defect.
    These pin the shared helper to the backend's historical behaviour, so a
    change to the helper's answer fails here rather than shipping a silent
    policy fork."""

    def test_venv_present_resolves_to_the_venv_interpreter(self, tmp_path):
        from kiro_crew.apps.interpreter import resolve_app_python

        venv_py = _fake_venv_python(tmp_path)
        assert resolve_app_python(tmp_path) == str(venv_py)

    def test_venv_absent_resolves_to_sys_executable(self, tmp_path):
        import sys

        from kiro_crew.apps.interpreter import resolve_app_python

        assert resolve_app_python(tmp_path) == sys.executable

    def test_no_app_context_resolves_to_sys_executable(self):
        import sys

        from kiro_crew.apps.interpreter import resolve_app_python

        assert resolve_app_python(None) == sys.executable

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS, reason="pins the backend's historical POSIX layout"
    )
    def test_helper_matches_the_backend_historical_posix_policy(self, tmp_path):
        # Literal reconstruction of the resolution backend.py carried inline
        # before the extraction, checked on the two normal shapes (runnable
        # venv interpreter present / no venv). One deliberate divergence is
        # out of scope here and pinned by its own test: a venv python that
        # exists but is NOT executable now falls back to sys.executable
        # instead of being returned as a guaranteed-EACCES spawn target.
        import sys

        from kiro_crew.apps.interpreter import resolve_app_python

        def historical(root: Path) -> str:
            venv_python = str(root / ".venv" / "bin" / "python3")
            return venv_python if (root / ".venv" / "bin" / "python3").is_file() else sys.executable

        with_venv = tmp_path / "with-venv"
        _fake_venv_python(with_venv)
        without_venv = tmp_path / "without-venv"
        without_venv.mkdir()
        for root in (with_venv, without_venv):
            assert resolve_app_python(root) == historical(root)

    def test_the_backend_spawn_uses_the_shared_helper(self):
        # Wiring check: the extraction is only real if backend.py CALLS the
        # helper. Grepping the source keeps this honest without spawning.
        import inspect

        import kiro_crew.apps.backend as backend_mod

        source = inspect.getsource(backend_mod)
        assert source.count("resolve_app_python(") >= 2, (
            "backend.py no longer routes both Python branches through the "
            "shared interpreter helper"
        )
        assert '".venv" / "bin" / "python3").is_file()' not in source, (
            "backend.py grew back an inline copy of the interpreter policy"
        )


class TestAppEventBusIsActuallyWired:
    """An app's EventBus only exists when a real broadcast_fn is supplied.

    With broadcast_fn None, ``build_app_context`` returns ``events=None`` and every app
    event is a SILENT no-op; the method is named ``broadcast_ws``, so passing a
    ``state.broadcast`` lookup disables events with no error. These pin both halves.
    """

    def test_dashboard_state_exposes_the_broadcast_method_the_gateway_passes(self):
        import inspect

        from kiro_crew.dashboard import server as server_mod
        from kiro_crew.dashboard.state import DashboardState

        src = inspect.getsource(server_mod)
        # The app backend waves hand it over from a server_runtime owner.
        owners = sorted((Path(server_mod.__file__).parent / "server_runtime").glob("[!_]*.py"))
        assert owners, "expected the server_runtime owners beside server.py"
        src += "".join(path.read_text(encoding="utf-8") for path in owners)
        # Whatever the gateway hands to the hooks system must exist on the state.
        for attr in re.findall(r"broadcast_fn=state\.([A-Za-z_][A-Za-z0-9_]*)", src):
            assert hasattr(DashboardState, attr), (
                f"gateway passes state.{attr} as broadcast_fn but DashboardState has "
                f"no such attribute — apps would silently get events=None"
            )

    def test_context_has_no_event_bus_without_a_broadcast_fn(self, tmp_path):
        from kiro_crew.apps.context import build_app_context

        ctx = build_app_context(
            "probe", tmp_path, permissions={"events": ["probe:thing"]}, broadcast_fn=None
        )
        assert ctx.events is None

    def test_context_gets_an_event_bus_when_a_broadcast_fn_is_supplied(self, tmp_path):
        from kiro_crew.apps.context import build_app_context

        sent: list[dict] = []
        ctx = build_app_context(
            "probe",
            tmp_path,
            permissions={"events": ["probe:thing"]},
            # ONE dict, not (type, data): that mismatch is why wiring
            # broadcast_ws straight through raised TypeError on every publish.
            broadcast_fn=sent.append,
        )
        assert ctx.events is not None
        ctx.events.publish("probe:thing", {"a": 1})
        assert sent and sent[0]["type"] == "probe:thing"

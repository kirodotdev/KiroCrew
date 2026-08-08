"""Tests for kiro_crew.apps.bridges — resource registration bridges."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from kiro_crew import platform_compat
from kiro_crew.apps.bridges import (
    RegistrationResult,
    _deregister_agents,
    _deregister_crons,
    _deregister_mcp_servers,
    _deregister_skills,
    _namespace,
    _register_agents,
    _register_crons,
    _register_mcp_servers,
    _register_skills,
    _safe_link_name,
    deregister_app,
    load_app_cron_defs,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _run(coro):
    """Drive an async bridge helper from a synchronous test.

    ``register_app_crons_with_service`` / ``deregister_app_crons_from_service``
    are async (they await the async CronSDK mutation API); these unit tests use
    mock services, so a one-shot event loop per call is sufficient.
    """
    return asyncio.run(coro)


def _make_app_source(tmp_path, name="test-app", **extras):
    """Create a minimal app source with agents and skills."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
        "agents": ["agents/my-agent.json"],
        "skills": ["skills/my-skill"],
        "crons": [{"name": "refresh", "every": 3600, "agent": "my-agent", "message": "go"}],
        **extras,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    # Create agent file
    (src / "agents").mkdir()
    (src / "agents" / "my-agent.json").write_text(json.dumps({"name": "my-agent", "model": "auto"}))
    # Create skill directory
    (src / "skills" / "my-skill").mkdir(parents=True)
    (src / "skills" / "my-skill" / "SKILL.md").write_text("# My Skill\nDoes things.")
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Set up isolated KIROCREW_HOME and KIRO agents dir."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    # Patch the KIRO_AGENTS_DIR in bridges module
    import kiro_crew.apps.bridges as bridges_mod
    import kiro_crew.apps.execution as execution_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(
        execution_mod,
        "third_party_execution_allowed",
        lambda: True,
    )

    # Patch _mcp_json_path to avoid file descriptor errors in tests
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: mcp_path)

    return {"home": home, "kiro_agents": kiro_agents}


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------


class TestNamespace:
    def test_namespace(self):
        assert _namespace("my-app", "agent-1") == "my-app/agent-1"

    def test_safe_link_name(self):
        assert _safe_link_name("my-app/agent-1") == "my-app--agent-1"

    def test_safe_link_name_neutralizes_backslash(self):
        # Windows treats backslash as a separator; it must be flattened too or a
        # resource name could traverse out of the agents dir.
        assert "\\" not in _safe_link_name("my-app/..\\..\\crew\\config")


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------


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

    def test_a_failed_rewrite_leaves_the_prior_config_intact(self, tmp_path, app_env, monkeypatch):
        """A rebuild must never destroy the working config before its replacement
        is durable.

        The refresh used to unlink any existing file first, then write. On a
        startup reconciliation that hit a disk-full write, the unlink had already
        removed the config and the write failed — so the agent DISAPPEARED. A
        regular file is now left in place for atomic_write's rename to swap, so a
        failed write leaves the last-good config untouched.
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

    def test_a_legacy_symlink_is_still_replaced(self, tmp_path, app_env):
        """A symlink from an older KiroCrew is dropped and replaced with a real file."""
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

    def test_register_mcp_keeps_autoapprove_when_ungoverned(self, tmp_path, app_env, monkeypatch):
        from kiro_crew.apps import bridges as bridges_mod
        from kiro_crew.platform import governance as gov

        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: True)  # ungoverned
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
        assert written["mcpServers"]["test-app:srv"].get("autoApprove") == ["ok"]

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


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------


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

        When a PACKAGED builtin skill shares the app's name, that empty directory
        MASKS it: `_ensure_builtin_skills` copies the skill at gateway start, app
        registration runs afterwards, and the unconditional mkdir then leaves a
        directory with no `SKILL.md` — so every SOP the app's cron prompts reference
        silently does not exist on disk. Hit for real by ops-mission-control, whose
        skill ships under `builtin_skills/` precisely because a builtin app's own
        directory is never copied into the data home.
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
        """Deregister is the path that actually destroyed the shipped skill.

        `sync_app_skills` calls `_deregister_skills` for any app whose manifest
        declares no skills, to clean up stale symlinks from a prior version. It used
        to `rmtree` the whole `skills/<app_name>/` dir — but for a builtin whose
        packaged skill shares that name, that dir holds real files, not links. This
        deleted the skill and every SOP under it, so the app's cron prompts pointed
        at files that were gone. Silent, because a missing skill file errors nowhere.
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


# ---------------------------------------------------------------------------
# Cron registration
# ---------------------------------------------------------------------------


class TestCronRegistration:
    def test_register_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        registered = _register_crons("test-app", manifest)
        assert len(registered) == 1
        assert "test-app/refresh" in registered

        # Verify cron manifest written
        defs = load_app_cron_defs("test-app")
        assert len(defs) == 1
        assert defs[0]["name"] == "test-app/refresh"
        assert defs[0]["every"] == 3600

    def test_register_crons_persists_enabled_flag(self, tmp_path, app_env):
        """A manifest cron shipped disabled keeps enabled:false in app-crons.json."""
        src = _make_app_source(
            tmp_path,
            crons=[
                {
                    "name": "nightly-run",
                    "cron_expr": "0 22 * * *",
                    "agent": "my-agent",
                    "enabled": False,
                }
            ],
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        _register_crons("test-app", manifest)
        defs = load_app_cron_defs("test-app")
        assert len(defs) == 1
        assert defs[0]["enabled"] is False

    def test_deregister_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)

        _deregister_crons("test-app")
        assert load_app_cron_defs("test-app") == []

    def test_no_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, crons=[])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_crons("test-app", manifest)
        assert registered == []

    @pytest.mark.asyncio
    async def test_register_with_running_service_arms_timer_on_loop(self, tmp_path, app_env):
        # register_app_crons_with_service is async: it awaits the async CronSDK
        # mutation API (add_job -> CronService.add_job_async), which offloads the
        # bounded store-lock spin to a worker thread and then arms the timer
        # IN-SERVICE on the loop. Driving it through a started CronService here
        # exercises that path end-to-end with NO caller-side drain step.
        from kiro_crew.cron import CronService

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)  # persist the app-cron defs

        # Hermetic store under the isolated home (bare CronService() would bind
        # its crons.json at the process-default dir, leaking state across tests).
        svc = CronService(base_dir=app_env["home"] / "crons")
        await svc.start()
        try:
            registered = await register_app_crons_with_service("test-app", svc)
            assert "test-app/refresh" in registered
            # The job is fully added (owned) and the timer armed without error.
            assert any(j.name == "test-app/refresh" for j in svc.list_jobs())
            assert svc._timer_task is not None  # armed in-service, no drain call
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_register_offloads_lock_spin_and_arms_without_drain(self, tmp_path, app_env):
        # The async bridge awaits CronSDK.add_job -> CronService.add_job_async,
        # whose lock+persist runs in a worker thread (asyncio.to_thread) so the
        # bounded _file_lock spin never parks the gateway loop. The timer is
        # (re)armed by CronService ITSELF (in-service, via the bound loop) — so
        # no caller-side drain (the removed rearm_after_offload) is required.
        # This mirrors on_app_enable / on_gateway_startup.
        from kiro_crew.cron import CronService

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)

        svc = CronService(base_dir=app_env["home"] / "crons")
        await svc.start()
        try:
            loop_thread = threading.get_ident()
            persist_threads: list[int] = []
            orig_persist = svc._persist_add_locked

            def _track(job):  # type: ignore[no-untyped-def]
                persist_threads.append(threading.get_ident())
                return orig_persist(job)

            svc._persist_add_locked = _track  # type: ignore[method-assign, assignment]

            registered = await register_app_crons_with_service("test-app", svc)
            assert "test-app/refresh" in registered
            # The store lock+persist ran OFF the event loop (offloaded).
            assert persist_threads and all(
                t != loop_thread for t in persist_threads
            ), "the store lock+persist must run in a worker thread, never on the loop"
            # Timer armed in-service, with no caller-side drain call.
            assert svc._timer_task is not None and not svc._timer_task.done()
            assert any(j.name == "test-app/refresh" for j in svc.list_jobs())
        finally:
            await svc.stop()


# ---------------------------------------------------------------------------
# Top-level register / deregister
# ---------------------------------------------------------------------------


class TestTopLevel:
    def test_register_app(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = register_app("test-app")
        assert len(result.agents) == 1
        assert len(result.skills) == 1
        assert len(result.crons) == 1
        assert result.errors == []

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

        register_app must skip all bridge work (agents, skills, crons, MCP)
        to avoid creating duplicates that confuse kiro-cli.  This is the
        exact scenario that caused Mochi's subagent MCP tools to disappear:
        bridge wrote a namespaced <app>--<agent>.json with empty mcpServers
        alongside the app's real agent file, and kiro-cli loaded the empty one.
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

        # Nothing registered — all skipped
        assert result.agents == []
        assert result.skills == []
        assert result.crons == []
        assert result.mcp_servers == []
        assert result.errors == []

        # No agent symlinks created
        assert not any(f.name.startswith("test-app--") for f in app_env["kiro_agents"].iterdir())
        # No skill symlinks created
        assert not (app_env["home"] / "skills" / "test-app").exists()
        # No MCP entries written
        assert not mcp_path.exists()

    def test_deregister_app(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        register_app("test-app")
        result = deregister_app("test-app")
        assert result.errors == []
        # Verify agents removed
        assert not any(f.name.startswith("test-app--") for f in app_env["kiro_agents"].iterdir())

    def test_register_deregister_cycle(self, tmp_path, app_env):
        """Register, deregister, re-register — no stale state."""
        src = _make_app_source(tmp_path)
        install_app(src)

        r1 = register_app("test-app")
        assert len(r1.agents) == 1

        deregister_app("test-app")
        # Verify clean
        assert not any(f.name.startswith("test-app--") for f in app_env["kiro_agents"].iterdir())

        r2 = register_app("test-app")
        assert len(r2.agents) == 1


# ---------------------------------------------------------------------------
# RegistrationResult
# ---------------------------------------------------------------------------


class TestRegistrationResult:
    def test_to_dict(self):
        r = RegistrationResult(agents=["a/b"], skills=["a/s"], crons=["a/c"], errors=[])
        d = r.to_dict()
        assert d["agents"] == ["a/b"]
        assert d["errors"] == []


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


class TestMCPRegistration:
    def test_register_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live → HTTP url server registers (the dead-port skip only fires when
        # no backend is up; see test_http_mcp_server_skipped_when_backend_not_yet_up).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9000/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-mcp"]

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "test-app:my-mcp" in data["mcpServers"]

    def test_http_mcp_url_port_rewritten_to_live_backend_port(self, tmp_path, app_env, monkeypatch):
        # An app with backend.port:"auto" gets a free port at spawn time (9100, else
        # 9101, …). The manifest's mcpServers url carries an illustrative fixed port.
        # Registration MUST rewrite it to the live allocated port, else agents call the
        # wrong port and every app tool call silently fails.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Pretend the backend actually came up on 9101 (not the manifest's 9100).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        # Port rewritten 9100 -> 9101; scheme/host/path preserved.
        assert data["mcpServers"]["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"

    def test_http_mcp_server_skipped_when_backend_not_yet_up(self, tmp_path, app_env, monkeypatch):
        # REGRESSION (revert): if the backend isn't running
        # (port unknown), an HTTP MCP server must NOT be registered at all — registering
        # the manifest's illustrative dead port (:9100) into global ~/.kiro/settings/mcp.json
        # makes kiro-cli try to connect on EVERY session → "backend hiccup" → 3 retries →
        # hard error, breaking all requests. The enable/boot flow re-registers with the
        # live port once the backend is up.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        # No dead-port entry written — nothing for kiro to fail to connect to.
        assert "test-app:my-mcp" not in data.get("mcpServers", {})

    def test_http_mcp_dead_entry_scrubbed_on_reregister_without_backend(
        self, tmp_path, app_env, monkeypatch
    ):
        # A stale dead-port entry from a prior (now-down) registration must be SCRUBBED
        # when we re-register and the backend still isn't up — so it can't keep poisoning
        # every kiro session across reboots/disable.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        # Backend up → entry written with live port.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        # Backend now DOWN → a re-register must remove the now-dead entry.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get(
            "mcpServers", {}
        )

    def test_stdio_mcp_server_always_registered_no_backend(self, tmp_path, app_env, monkeypatch):
        # A command/stdio MCP server (no url) has no port to be dead — it must always be
        # registered regardless of backend liveness (only HTTP url servers are gated).
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-stdio": {"command": "my-server", "args": ["--stdio"]},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-stdio"]
        assert "test-app:my-stdio" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]

    def test_reregister_app_mcp_servers_overwrites_with_live_port(
        self, tmp_path, app_env, monkeypatch
    ):
        # reregister_app_mcp_servers (called after the backend starts) overwrites the
        # earlier manifest-default entry with the live-port url.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        from kiro_crew.apps.bridges import reregister_app_mcp_servers

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        # First registration BEFORE the backend is up: HTTP server is skipped (no dead
        # entry written — the fail-safe that keeps kiro from dialing a dead port).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get(
            "mcpServers", {}
        )
        # Backend now up on 9101 → re-register writes it with the live port.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)
        reregister_app_mcp_servers("test-app")
        assert (
            json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["test-app:my-mcp"]["url"]
            == "http://localhost:9101/mcp"
        )

    def test_explicit_live_port_rewrites_even_when_backend_unhealthy(
        self, tmp_path, app_env, monkeypatch
    ):
        # The boot/enable path passes the just-allocated port explicitly because the
        # backend isn't marked *healthy* yet (get_app_backend_port would return None at
        # that instant). An explicit live_port must still rewrite the url — this is the
        # exact bug that left the registered url at :9100 while the backend was on :9101.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        from kiro_crew.apps.bridges import reregister_app_mcp_servers

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Health-gated lookup returns None (backend up but not yet confirmed healthy).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path, mcpServers={"my-mcp": {"url": "http://localhost:9100/mcp"}}
        )
        install_app(src)
        # Explicit live_port=9101 (from the spawn result) must win over the None lookup.
        reregister_app_mcp_servers("test-app", live_port=9101)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"

    def test_deregister_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        # Pre-populate with entries from two apps
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "app-a:srv1": {"url": "http://localhost:1"},
                        "app-a:srv2": {"url": "http://localhost:2"},
                        "app-b:srv1": {"url": "http://localhost:3"},
                    }
                }
            )
        )

        removed = _deregister_mcp_servers("app-a")
        assert removed == 2

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "app-a:srv1" not in data["mcpServers"]
        assert "app-a:srv2" not in data["mcpServers"]
        assert "app-b:srv1" in data["mcpServers"]

    def test_deregister_does_not_run_legacy_scrub_on_loop(self, tmp_path, app_env, monkeypatch):
        # The legacy shared-file scrub takes a cross-process flock contended by
        # external processes (Kiro IDE, other agents). deregister_app runs
        # synchronously on the gateway event loop, so the scrub must NOT run
        # here or a held lock would stall all chat/heartbeat. Boot reconcile
        # performs it off-loop instead.
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps({"mcpServers": {"app-a:srv1": {"url": "http://x"}}}))

        called: list[str] = []
        monkeypatch.setattr(bmod, "_scrub_legacy_shared_mcp", lambda name: called.append(name) or 0)
        _deregister_mcp_servers("app-a")
        assert called == [], "deregister must not run the blocking legacy scrub on the event loop"

    def test_deregister_no_servers(self, tmp_path, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        assert _deregister_mcp_servers("nonexistent") == 0

    def test_register_no_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        manifest = AppManifest(name="test", mcpServers={})
        assert _register_mcp_servers("test", manifest) == []

    def test_register_app_includes_mcp(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live so the HTTP url server is registered (not dead-port-skipped).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 8080)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "backend": {"url": "http://localhost:8080/mcp"},
            },
        )
        install_app(src)
        result = register_app("test-app")
        assert len(result.mcp_servers) == 1
        assert "test-app:backend" in result.mcp_servers


# ---------------------------------------------------------------------------
# MCP property tests
# ---------------------------------------------------------------------------

_app_name_st = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)
_server_name_st = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)


class TestMCPProperties:
    # Feature: app-classification-redesign, Property 10: MCP server registration is namespaced per app
    @given(
        app_name=_app_name_st,
        servers=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:9000")}),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_register_namespace(self, app_name, servers, tmp_path, monkeypatch):
        """**Validates: Requirements 8.1, 8.2**"""
        import uuid

        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / f"mcp-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live → HTTP url servers register (dead-port skip only with no backend).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        manifest = AppManifest(name=app_name, mcpServers=servers)
        registered = _register_mcp_servers(app_name, manifest)

        for server_name in servers:
            expected = f"{app_name}:{server_name}"
            assert expected in registered

        data = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
        for name in registered:
            assert name in data.get("mcpServers", {})

    # Feature: app-classification-redesign, Property 11: MCP server deregistration is isolated to one app
    @given(
        app_a=_app_name_st,
        app_b=_app_name_st.filter(lambda s: len(s) > 1),
        servers_a=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:1")}),
            min_size=1,
            max_size=3,
        ),
        servers_b=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:2")}),
            min_size=1,
            max_size=3,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_deregister_isolation(self, app_a, app_b, servers_a, servers_b, tmp_path, monkeypatch):
        """**Validates: Requirements 8.3**"""
        assume(app_a != app_b)
        import uuid

        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / f"mcp-iso-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live → HTTP url servers register (dead-port skip only with no backend).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        # Register both apps
        _register_mcp_servers(app_a, AppManifest(name=app_a, mcpServers=servers_a))
        _register_mcp_servers(app_b, AppManifest(name=app_b, mcpServers=servers_b))

        # Deregister app_a
        _deregister_mcp_servers(app_a)

        data = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
        remaining = data.get("mcpServers", {})

        # app_a entries gone
        for name in servers_a:
            assert f"{app_a}:{name}" not in remaining
        # app_b entries preserved
        for name in servers_b:
            assert f"{app_b}:{name}" in remaining


class TestBootReconcile:
    """Boot-time scrub of stale MCP entries for disabled apps."""

    def test_boot_scrubs_stale_mcp_entry_for_disabled_app(self, tmp_path, monkeypatch):
        # A disabled app that left a (now-dead-port) MCP entry in global mcp.json must
        # have it scrubbed at gateway boot — else kiro-cli dials the dead port on every
        # session. start_enabled_app_backends() reconciles disabled apps before starting
        # any backend.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Seed a stale entry as if a prior enable had registered it.
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ai-app:backend": {"url": "http://localhost:9100/mcp"},
                        "other:keep": {"command": "x"},
                    }
                }
            )
            + "\n"
        )

        # One installed-but-DISABLED app that declares an MCP server. list_apps is imported
        # inside start_enabled_app_backends from the manager module, so patch it there.
        monkeypatch.setattr(
            backend_mod,
            "list_apps",
            lambda: [
                {
                    "name": "ai-app",
                    "enabled": False,
                    "manifest": {
                        "mcpServers": {"backend": {"url": "http://localhost:9100/mcp"}},
                        "backend": {"entryPoint": "x"},
                    },
                },
            ],
        )
        # No backend should be started for a disabled app.
        monkeypatch.setattr(
            backend_mod,
            "start_app_backend",
            lambda *_a, **_k: pytest.fail("must not start disabled app"),
        )

        backend_mod.start_enabled_app_backends()

        remaining = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "ai-app:backend" not in remaining  # stale dead entry scrubbed
        assert "other:keep" in remaining  # unrelated entry untouched

    def test_enabled_app_never_healthy_mcp_entry_scrubbed(self, tmp_path, app_env, monkeypatch):
        # Review scenario: an ENABLED port:"auto" app registered with an optimistic
        # pre-health port whose backend never passes /health must NOT leave a dead HTTP MCP
        # url behind — that's the exact shape that broke every kiro-cli session. The
        # health-gated path calls _gate_mcp_registration(healthy=False) on health failure,
        # which scrubs the entry. (Closes the disabled-only asymmetry the reviewer flagged.)
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Seed an optimistic entry as if the pre-health register had written it.
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "test-app:backend": {"url": "http://localhost:9100/mcp"},
                        "other:keep": {"command": "x"},
                    }
                }
            )
            + "\n"
        )

        backend_mod._gate_mcp_registration("test-app", 9100, healthy=False)

        remaining = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "test-app:backend" not in remaining  # dead enabled-app entry scrubbed
        assert "other:keep" in remaining  # unrelated entry untouched

    def test_enabled_app_healthy_registers_with_live_port(self, tmp_path, app_env, monkeypatch):
        # The complement: once /health passes, _gate_mcp_registration(healthy=True) writes the
        # HTTP MCP url with the confirmed live port (rewriting the manifest's illustrative one).
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Health-gated lookup returns None (port resolved from the explicit live_port instead).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        src = _make_app_source(
            tmp_path, mcpServers={"my-mcp": {"url": "http://localhost:9100/mcp"}}
        )
        install_app(src)

        backend_mod._gate_mcp_registration("test-app", 9101, healthy=True)

        servers = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "test-app:my-mcp" in servers
        assert servers["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"  # live port

    def test_boot_does_not_register_enabled_app_before_health(self, tmp_path, monkeypatch):
        # Review scenario: the boot loop must NOT register MCP servers for a freshly
        # spawned (healthy=False) enabled app — registration is deferred to the health-check
        # loop. Registering here is what could leave a dead url for a never-healthy app.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        mcp_path.write_text(json.dumps({"mcpServers": {}}) + "\n")

        monkeypatch.setattr(
            backend_mod,
            "list_apps",
            lambda: [
                {
                    "name": "ai-app",
                    "enabled": True,
                    "manifest": {
                        "mcpServers": {"backend": {"url": "http://localhost:9100/mcp"}},
                        "backend": {"entryPoint": "x"},
                    },
                },
            ],
        )
        # Spawn returns a not-yet-healthy process (the real pre-health state).
        fake_ap = SimpleNamespace(port=9101, healthy=False)
        monkeypatch.setattr(backend_mod, "start_app_backend", lambda *_a, **_k: fake_ap)
        # If the boot loop tries to register before health, fail loudly.
        monkeypatch.setattr(
            backend_mod,
            "_gate_mcp_registration",
            lambda *_a, **_k: pytest.fail("must not register before health"),
        )

        backend_mod.start_enabled_app_backends()

        # Nothing registered synchronously; the health loop owns it.
        assert json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"] == {}


# ---------------------------------------------------------------------------
# Cron service bridge (register_app_crons_with_service)
# ---------------------------------------------------------------------------


class TestCronServiceBridge:
    """Tests for register_app_crons_with_service — promoting app crons to scheduler."""

    def _write_app_crons(self, tmp_path, app_name, cron_defs):
        """Write a fake app-crons.json for testing."""
        app_dir = tmp_path / "kirocrew-home" / "apps" / app_name
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "app-crons.json").write_text(json.dumps(cron_defs, indent=2))

    def test_boot_default_off_registers_no_third_party_crons(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        import kiro_crew.apps.execution as execution_mod

        self._write_app_crons(
            tmp_path,
            "test-app",
            [{"name": "test-app/refresh", "every": 60, "message": "go"}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )
        mock_sdk = MagicMock()

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", MagicMock()))

        assert result == []
        mock_sdk.add_job_async.assert_not_called()

    def test_boot_explicit_allow_registers_third_party_crons(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        import kiro_crew.apps.execution as execution_mod

        self._write_app_crons(
            tmp_path,
            "test-app",
            [{"name": "test-app/refresh", "every": 60, "message": "go"}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: True,
        )
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_async = AsyncMock(return_value=MagicMock(id="job-id"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", MagicMock()))

        assert result == ["test-app/refresh"]
        mock_sdk.add_job_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boot_disarms_persisted_denied_app_crons_before_timer_start(
        self, tmp_path, app_env, monkeypatch
    ):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        app_root = app_env["home"] / "apps" / "test-app"
        app_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            bridges_mod,
            "list_apps",
            lambda: [{"name": "test-app", "enabled": True}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = []
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=["job-id"])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == ["test-app"]
        mock_service.list_jobs.assert_called_once_with(include_disabled=True)
        mock_service.remove_jobs_by_owner.assert_awaited_once_with("app:test-app")

    @pytest.mark.asyncio
    async def test_boot_disarms_orphaned_app_cron_owner(self, app_env, monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        events = []
        monkeypatch.setattr(bridges_mod, "list_apps", lambda: [])
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: True,
        )
        monkeypatch.setattr(
            bridges_mod,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs)),
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [SimpleNamespace(created_by="app:ghost-app")]
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=["ghost-job"])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == ["ghost-app"]
        mock_service.list_jobs.assert_called_once_with(include_disabled=True)
        mock_service.remove_jobs_by_owner.assert_awaited_once_with("app:ghost-app")
        denial = [event for event in events if event.get("outcome") == "denied"]
        assert denial == [
            {
                "caller": "app_bridge",
                "operation": "app_execution_admission",
                "outcome": "denied",
                "resources": ("app=ghost-app action=cron_boot_restore provenance=unverified"),
                "error": "orphaned app cron owner has no installed app",
            }
        ]

    @pytest.mark.asyncio
    async def test_boot_keeps_shipped_builtin_app_cron_armed(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        shipped = tmp_path / "shipped-builtins"
        shipped_app = shipped / "builtin-app"
        shipped_app.mkdir(parents=True)
        (shipped_app / "app.json").write_text(
            json.dumps(
                {
                    "name": "builtin-app",
                    "version": "1.0.0",
                    "displayName": "Builtin App",
                    "description": "A test builtin app",
                    "author": "kirocrew",
                }
            )
        )
        monkeypatch.setattr(execution_mod, "_BUILTINS_DIR", shipped)
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )
        monkeypatch.setattr(
            bridges_mod,
            "list_apps",
            lambda: [{"name": "builtin-app", "enabled": True}],
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [SimpleNamespace(created_by="app:builtin-app")]
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=[])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == []
        mock_service.remove_jobs_by_owner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_boot_keeps_explicitly_admitted_third_party_cron_armed(
        self, app_env, monkeypatch
    ):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        app_root = app_env["home"] / "apps" / "third-party-app"
        app_root.mkdir(parents=True)
        monkeypatch.setattr(
            bridges_mod,
            "list_apps",
            lambda: [{"name": "third-party-app", "enabled": True}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: True,
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [SimpleNamespace(created_by="app:third-party-app")]
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=[])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == []
        mock_service.remove_jobs_by_owner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execution_disarm_audit_failure_is_best_effort(self, monkeypatch):
        import kiro_crew.apps.bridges as bridges_mod

        def _audit_failure(**kwargs):
            raise OSError("audit unavailable")

        monkeypatch.setattr(
            bridges_mod,
            "sel",
            lambda: SimpleNamespace(log_api_access=_audit_failure),
        )
        mock_service = SimpleNamespace(remove_jobs_by_owner=AsyncMock(return_value=["job-id"]))

        removed = await bridges_mod.disarm_app_crons_for_execution(
            "test-app",
            mock_service,
        )

        assert removed == 1

    def test_registers_cron_with_all_fields(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/refresh",
                "every": 600,
                "cron_expr": "",
                "agent": "my-agent",
                "message": "do stuff",
                "app": "test-app",
                "agent_sequence": ["a1", "a2"],
                "env": {"FOO": "bar"},
                "persistent_session": False,
                "silent": True,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/refresh"]
        mock_sdk.add_job_async.assert_called_once_with(
            name="test-app/refresh",
            message="do stuff",
            every_secs=600,
            cron_expr="",
            agent="my-agent",
            command="",
            script="",
            agent_sequence=["a1", "a2"],
            env={"FOO": "bar"},
            persistent_session=False,
            silent=True,
            enabled=True,
        )

    def test_disabled_cron_registers_paused(self, tmp_path, app_env, monkeypatch):
        """A manifest cron with enabled:false is passed through as enabled=False."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/nightly-run",
                "every": 0,
                "cron_expr": "0 22 * * *",
                "agent": "discovery",
                "message": "",
                "app": "test-app",
                "enabled": False,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/nightly-run"]
        assert mock_sdk.add_job_async.call_args.kwargs["enabled"] is False

    def test_legacy_defs_without_enabled_default_active(self, tmp_path, app_env, monkeypatch):
        """Pre-existing app-crons.json without the enabled key registers active."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/legacy",
                "every": 600,
                "agent": "a",
                "message": "m",
                "app": "test-app",
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert mock_sdk.add_job_async.call_args.kwargs["enabled"] is True

    def test_startup_skips_existing_disabled_job(self, tmp_path, app_env, monkeypatch):
        """Gateway-startup re-registration must not re-add (and thus re-pause)
        a job that already exists in a disabled state.

        CronSDK.list_jobs() includes disabled jobs, so a paused job counts as
        existing — preserving a user's resume/pause state across restarts.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/nightly-run",
                "every": 0,
                "cron_expr": "0 22 * * *",
                "agent": "discovery",
                "message": "",
                "app": "test-app",
                "enabled": False,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        existing = MagicMock()
        existing.name = "test-app/nightly-run"
        existing.enabled = False  # currently paused
        existing.user_paused = True

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing]

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_async.assert_not_called()
        # The existing job's state is untouched — no duplicate, no re-pause.
        assert existing.enabled is False
        assert existing.user_paused is True

    def test_registers_command_type_cron(self, tmp_path, app_env, monkeypatch):
        """Apps declaring command-type crons get them registered as command jobs."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/collect",
                "every": 60,
                "cron_expr": "",
                "agent": "",
                "message": "",
                "command": "python3 ~/.kirocrew/apps/test-app/scripts/collect.py",
                "script": "",
                "app": "test-app",
                "agent_sequence": [],
                "env": {},
                "persistent_session": False,
                "silent": True,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_async = AsyncMock(return_value=MagicMock(id="cmd123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/collect"]
        mock_sdk.add_job_async.assert_called_once_with(
            name="test-app/collect",
            message="",
            every_secs=60,
            cron_expr="",
            agent="",
            command="python3 ~/.kirocrew/apps/test-app/scripts/collect.py",
            script="",
            agent_sequence=None,
            env=None,
            persistent_session=False,
            silent=True,
            enabled=True,
        )

    def test_rejects_malicious_command(self, tmp_path, app_env, monkeypatch):
        """Commands blocked by _vet_shell_command are skipped with SEL audit."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/evil",
                "every": 60,
                "command": "cat ~/.aws/credentials",
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_async.assert_not_called()

    def test_rejects_invalid_script_path(self, tmp_path, app_env, monkeypatch):
        """Scripts outside ~/.kirocrew/crons/ are rejected at registration."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/bad-script",
                "every": 60,
                "script": "/etc/passwd:run",
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_async.assert_not_called()

    def test_idempotent_skips_existing(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{"name": "test-app/refresh", "every": 600, "message": "go"}]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        existing_job = MagicMock()
        existing_job.name = "test-app/refresh"
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing_job]

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_async.assert_not_called()

    def test_returns_empty_when_no_cron_service(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import register_app_crons_with_service

        result = _run(register_app_crons_with_service("test-app", None))
        assert result == []

    def test_returns_empty_when_no_app_crons_file(self, tmp_path, app_env):
        from unittest.mock import MagicMock

        from kiro_crew.apps.bridges import register_app_crons_with_service

        result = _run(register_app_crons_with_service("nonexistent-app", MagicMock()))
        assert result == []

    def test_handles_malformed_entry_gracefully(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {"name": "", "every": 600, "message": "bad"},  # empty name — skipped
            {"name": "test-app/good", "every": 300, "message": "ok"},
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_async = AsyncMock(return_value=MagicMock(id="x"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/good"]

    def test_register_crons_serializes_all_fields(self, tmp_path, app_env):
        """Verify _register_crons writes all CronEntry fields to app-crons.json."""
        from kiro_crew.apps.bridges import _register_crons, load_app_cron_defs

        manifest = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="",
            author="t",
            crons=[],
        )
        # Manually construct a CronEntry with all fields set
        from kiro_crew.apps.manifest import CronEntry

        entry = CronEntry(
            name="refresh",
            every=600,
            agent="my-agent",
            message="go",
            agent_sequence=["a1"],
            env={"K": "V"},
            persistent_session=False,
            silent=True,
        )
        manifest.crons = [entry]

        _register_crons("test-app", manifest)
        defs = load_app_cron_defs("test-app")

        assert len(defs) == 1
        d = defs[0]
        assert d["agent_sequence"] == ["a1"]
        assert d["env"] == {"K": "V"}
        assert d["persistent_session"] is False
        assert d["silent"] is True

    def test_add_job_exception_logged_and_skipped(self, tmp_path, app_env):
        """Exception from CronSDK.add_job is caught, logged, and execution continues."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {"name": "test-app/bad", "every": 600, "message": "x"},
            {"name": "test-app/good", "every": 300, "message": "y"},
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        # First call raises, second succeeds
        mock_sdk.add_job_async = AsyncMock(side_effect=[RuntimeError("boom"), MagicMock(id="ok")])

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        # Failed entry skipped, good entry registered
        assert result == ["test-app/good"]
        assert mock_sdk.add_job_async.call_count == 2


class TestCronServiceDeregister:
    """Tests for deregister_app_crons_from_service — scheduler cleanup helper."""

    def test_returns_zero_when_no_cron_service(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        assert _run(deregister_app_crons_from_service("test-app", None)) == 0

    def test_calls_remove_all_and_returns_count(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all_async = AsyncMock(return_value=3)

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(deregister_app_crons_from_service("test-app", mock_cron_service))

        assert result == 3
        mock_sdk.remove_all_async.assert_called_once()

    def test_returns_zero_on_exception(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all_async = AsyncMock(side_effect=RuntimeError("scheduler unavailable"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(deregister_app_crons_from_service("test-app", mock_cron_service))

        assert result == 0  # exception swallowed, zero returned


class TestBuiltinAgentNamesAreNamespaced:
    """Builtin app agents must not squat a generic global agent name.

    kiro-cli resolves an agent by the ``name`` field INSIDE the JSON, not by the
    namespaced link filename ``_register_agents`` writes into ~/.kiro/agents/
    (``<app>--<agent>.json``).  Agent names are therefore ONE FLAT GLOBAL
    namespace: two installed agents claiming the same name collide, and kiro-cli
    only warns and picks one.  Prefixing with the app id (app ids are unique in
    the registry) is what makes a public install collision-proof.
    """

    def _builtin_dirs(self):
        from pathlib import Path

        import kiro_crew.apps.builtins as builtins_pkg

        root = Path(builtins_pkg.__file__).parent
        return [p for p in root.iterdir() if (p / "app.json").is_file()]

    def test_every_declared_agent_name_is_app_id_prefixed(self):
        checked = 0
        for app_dir in self._builtin_dirs():
            manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
            app_id = manifest.get("name") or app_dir.name
            for rel in manifest.get("agents") or []:
                agent_file = app_dir / rel
                assert agent_file.is_file(), f"{app_id}: declared agent missing: {rel}"
                name = json.loads(agent_file.read_text(encoding="utf-8")).get("name", "")
                assert name == app_id or name.startswith(f"{app_id}-"), (
                    f"{app_id}: agent name {name!r} is not app-id-prefixed — it would "
                    f"collide with any other install claiming that global name"
                )
                checked += 1
        if checked == 0:
            # No shipped builtin declares an agent yet, so there is nothing to
            # check — but a guard that passes on an empty sample is worthless, so
            # say so out loud rather than pass silently. The first agent-declaring
            # builtin turns this on.
            pytest.skip("no builtin declares agents yet — guard is vacuous")


class TestUserAgentEditsSurviveRefresh:
    """App agent JSONs are re-materialized every boot; user edits must survive.

    Registration rewrites these files from the packaged template on each
    registration (that is what lets a template change land without a reinstall),
    but a wholesale write silently reverted anything the user had tuned by hand
    — `model`, extra `toolsSettings` — on every gateway start. Same split as the
    managed-MCP refresh: framework-derived keys are refreshed (a stale one is a
    bug), everything else on disk is the user's and wins.
    """

    def test_user_keys_win_and_owned_keys_are_refreshed(self) -> None:
        from kiro_crew.apps.bridges import _preserve_user_agent_edits

        prior = {
            "name": "app--agent",
            "model": "some-pinned-model",  # user's
            "description": "my tweaks",  # user's
            "allowedTools": ["@stale"],  # framework's
            "prompt": "file:///old/path.md",  # framework's
        }
        fresh = {
            "name": "app--agent",
            "model": "auto",
            "allowedTools": ["@fresh"],
            "prompt": "file:///new/path.md",
        }
        out = _preserve_user_agent_edits("app--agent.json", prior, fresh)

        # Theirs survives...
        assert out["model"] == "some-pinned-model"
        assert out["description"] == "my tweaks"
        # ...ours is refreshed, not resurrected from the old file.
        assert out["allowedTools"] == ["@fresh"]
        assert out["prompt"] == "file:///new/path.md"

    def test_no_prior_file_is_a_no_op(self) -> None:
        from kiro_crew.apps.bridges import _preserve_user_agent_edits

        fresh = {"name": "a", "model": "auto"}
        assert _preserve_user_agent_edits("a.json", None, fresh) == fresh

    def test_containment_keys_are_refreshed_not_preserved(self) -> None:
        """`managedToolPolicy` / `includeMcpJson` are the framework's, not the user's.

        Preserving them is wrong in BOTH directions, which is why they are owned:
        a template that later tightens the exclude list would never reach an
        already-enabled install, and anything that edited the file could drop the
        exclude list — which the old rule then preserved forever. There is no
        provenance here to tell a hand edit from last boot's template copy, so the
        line is drawn by meaning: containment is refreshed, preference is kept.
        """
        from kiro_crew.apps.bridges import _preserve_user_agent_edits

        prior = {
            "name": "app--agent",
            "model": "user-pinned",  # preference — must survive
            "managedToolPolicy": {"exclude": []},  # containment dropped on disk
            "includeMcpJson": True,  # containment widened on disk
        }
        fresh = {
            "name": "app--agent",
            "model": "auto",
            "managedToolPolicy": {"exclude": ["spawn_run", "cron_add"]},
            "includeMcpJson": False,
        }
        out = _preserve_user_agent_edits("app--agent.json", prior, fresh)

        assert out["model"] == "user-pinned", "a real preference must still win"
        assert out["managedToolPolicy"] == {"exclude": ["spawn_run", "cron_add"]}
        assert out["includeMcpJson"] is False

    def test_a_corrupt_prior_file_does_not_fail_the_refresh(self, tmp_path) -> None:
        """An unreadable file must not block registration — it means "nothing to
        preserve", not "abort"."""
        from kiro_crew.apps.bridges import _read_agent_config

        broken = tmp_path / "a.json"
        broken.write_text("{not json", encoding="utf-8")
        assert _read_agent_config(broken) is None
        assert _read_agent_config(tmp_path / "absent.json") is None

    def test_the_owned_key_set_names_every_field_the_framework_derives(self) -> None:
        """A field the framework computes but forgets to list here would be
        frozen at whatever the on-disk file happened to hold.

        The set is spelled out rather than derived because the rule is a JUDGEMENT
        per key, not a property of the data: there is no provenance recording what
        the last template wrote, so a key is owned when it means CONTAINMENT (the
        framework must be able to tighten it, and nothing may loosen it by editing
        the file) and unowned when it means PREFERENCE (the user's choice
        outranks the template's default). Adding a key below is a security
        decision; adding one to the template without deciding is the bug this
        guard exists to surface.
        """
        from kiro_crew.apps.bridges import _FRAMEWORK_OWNED_AGENT_KEYS

        assert _FRAMEWORK_OWNED_AGENT_KEYS == {
            "name",
            "mcpServers",
            "tools",
            "allowedTools",
            "prompt",
            "managedToolPolicy",
            "includeMcpJson",
            # A generated `file://` path list into the app's provisioned tree, rendered
            # from `{ENGINE_ROOT}` placeholders by the gateway — CONTAINMENT-shaped like
            # `prompt`: a user-pinned copy would keep pointing at a previous engine root
            # and silently stop resolving after a re-provision.
            "resources",
        }

    def test_every_template_key_is_a_decided_key(self) -> None:
        """Nothing in a shipped agent template may sit outside the two buckets.

        The failure mode is silent: a new template key that is neither owned nor
        a known preference lands in "preserved forever" by default — so a later
        template tightening it never reaches an existing install, and nothing
        anywhere says so.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.bridges import _FRAMEWORK_OWNED_AGENT_KEYS

        # Keys a user is MEANT to be able to pin by hand.
        # Keys a user is MEANT to be able to pin by hand. `welcomeMessage` is
        # user-facing copy with no containment role, so a reworded greeting must
        # survive a template refresh — the same reasoning as `description`.
        #
        # `skills` belongs here too, not in `_FRAMEWORK_OWNED_AGENT_KEYS`: it is a live field
        # (`agent_discovery.py` reads `row.get("skills")` into `AgentInfo.skills`) naming which
        # skills an agent loads, which is exactly the kind of choice an operator should be able
        # to change and keep across a refresh — same category as `model`. Framework ownership is
        # reserved for identity and CONTAINMENT keys, which this is not. Added when the
        # auto-improvement builtin became the first template to declare it.
        preferences = {
            "description", "model", "toolsSettings", "$schema", "welcomeMessage", "skills",
        }
        root = Path("src/kiro_crew/apps/builtins")
        templates = sorted(root.glob("*/agents/*.json"))
        if not templates:
            # Same reasoning as the namespacing guard above: nothing ships an
            # agent template yet, and a silent pass would hide that.
            pytest.skip("no builtin ships an agent template yet")
        for tpl in templates:
            keys = set(json.loads(tpl.read_text(encoding="utf-8")))
            undecided = keys - _FRAMEWORK_OWNED_AGENT_KEYS - preferences
            assert not undecided, (
                f"{tpl}: {sorted(undecided)} is neither framework-owned nor a known "
                f"preference — decide which, then add it to the matching set"
            )


class TestBuiltinDeclaredResourcesActuallyRegister:
    """A builtin that declares agents/skills must get them registered.

    Two independent framework bugs made this silently fail, and BOTH are
    silent-by-construction (registration only logs a warning), so they need
    executable pins rather than review vigilance:

    1. ``_manifest_to_builtin_dict`` (discovery.py) hand-copied a subset of
       AppManifest fields into the dict that register_builtin_apps() persists as
       the data-home app.json snapshot. ``agents`` and ``skills`` were not in
       that subset, so they never reached the snapshot that register_app() reads.
    2. ``register_app`` resolved manifest-relative paths against the data-home
       app dir. A builtin's code lives in the PACKAGE, so every path missed.
    """

    def test_builtin_dict_carries_every_declarative_manifest_field(self):
        """No AppManifest field may be silently dropped by the conversion.

        Dataclass-field-driven on purpose: adding a field to AppManifest without
        teaching the conversion about it fails here instead of vanishing.
        """
        import dataclasses

        from kiro_crew.apps.discovery import _manifest_to_builtin_dict
        from kiro_crew.apps.manifest import AppManifest

        declared = {f.name for f in dataclasses.fields(AppManifest)}
        # ``extra`` is the catch-all bag, splatted into the dict by key.
        declared.discard("extra")

        manifest = AppManifest.from_dict(
            {
                "name": "probe",
                "version": "1.0.0",
                "displayName": "Probe",
                "description": "d",
                "author": "a",
                "license": "MIT",
                "minKiroCrewVersion": "1.0.0",
                "signer": "s",
                "signature": "sig",
                "agents": ["agents/a.json"],
                "skills": ["skills/s"],
                "sops": ["sops/x.md"],
                "jobFamilies": ["jf"],
                "tags": ["t"],
                "mcpServers": {"m": {"command": "c"}},
                "platform": {"requiresDesktopApp": True},
                "permissions": {"storage": True},
                "ui": {"entry": "ui/dist/index.js"},
                "backend": {"entryPoint": "backend:app"},
                "crons": [{"name": "c", "schedule": "* * * * *", "message": "m"}],
                "dependencies": {"commands": ["git"]},
                "setup": {"onEnable": "echo hi"},
                "publishProvider": {"id": "p", "label": "P"},
                "notifications": {"channels": [{"id": "n", "name": "N"}]},
            }
        )
        # Every declared field must be populated above, otherwise a conditional
        # copy ("if manifest.x") is never exercised and the check goes vacuous.
        for fname in sorted(declared):
            assert getattr(manifest, fname), f"probe manifest leaves {fname!r} empty"
        d = _manifest_to_builtin_dict(manifest)
        missing = sorted(f for f in declared if f not in d)
        assert not missing, (
            f"fields dropped by _manifest_to_builtin_dict: {missing} — they will "
            f"be absent from every builtin's persisted manifest snapshot"
        )

    def test_resource_root_for_builtin_is_the_package_dir(self, app_env):
        """A builtin resolves resource paths against the packaged dir, not $HOME."""
        import json as _json

        from kiro_crew.apps.bridges import _app_resource_root
        from kiro_crew.apps.discovery import _get_builtins_dir
        from kiro_crew.apps.manager import get_app, register_builtin_apps

        register_builtin_apps()
        packaged = _get_builtins_dir()
        # Driven from the packaged DIRS, keyed by each manifest's own name. The
        # registry keys by app name (`auto-research`) while the package dir uses
        # underscores (`auto_research`), so reading names off `iterdir()`
        # conflated the two: the assertion only held for a builtin needing no
        # normalising, and which one came first was filesystem order.
        checked = []
        for d in sorted(packaged.iterdir()):
            manifest = d / "app.json"
            if not manifest.is_file():
                continue
            name = _json.loads(manifest.read_text(encoding="utf-8")).get("name")
            if not name or get_app(name) is None:
                continue  # packaged but not registered in this build
            root = _app_resource_root(name)
            assert root == d, f"{name}: resources must resolve to the packaged dir"
            assert (root / "app.json").is_file(), name
            checked.append(name)

        assert checked, "no registered packaged builtins found — test would be vacuous"
        # The hyphenated case is the one that regressed silently; pin it so a
        # future refactor cannot drop the normalisation while every
        # underscore-free builtin still passes.
        assert any("-" in n for n in checked), f"expected a hyphenated builtin, got {checked}"


class TestAppEventBusIsActuallyWired:
    """An app's EventBus only exists when a real broadcast_fn is supplied.

    ``build_app_context`` returns ``events=None`` when broadcast_fn is None, and
    ``EventBus.publish`` is then never reached — so every app event becomes a
    SILENT no-op. The gateway once passed
    ``state.broadcast if hasattr(state, "broadcast") else None`` while the method
    is actually named ``broadcast_ws``, which disabled app events entirely with no
    error anywhere. These pin both halves.
    """

    def test_dashboard_state_exposes_the_broadcast_method_the_gateway_passes(self):
        import inspect

        from kiro_crew.dashboard import server as server_mod
        from kiro_crew.dashboard.state import DashboardState

        src = inspect.getsource(server_mod)
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


class TestNeutralizeEntryShape:
    """A neutralize entry must be a complete server spec, not a bare deny.

    kiro-cli's agent loader parses strictly: one mcpServers entry without a
    command makes it reject the WHOLE agent file — the agent then vanishes from
    the ACP mode list ("Mode not found" at session time) while `agent list` and
    `agent validate` still show it, because those use a lenient parser. A bare
    {"disabledTools": [...]} therefore does not deny a server; it silently
    unregisters the agent.
    """

    def test_neutralize_copies_the_full_spec_from_the_global_file(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges,
            "_global_mcp_specs",
            lambda: {"some-server": {"command": "srv", "args": ["--x"], "env": {"A": "1"}}},
        )
        agent = {"name": "a", "tools": ["@some-server"], "mcpServers": {}}
        out = bridges._apply_agent_mcp_policy(
            agent, "a", {"agents": {"a": {"neutralize": {"some-server": ["t1", "t2"]}}}}
        )
        entry = out["mcpServers"]["some-server"]
        assert entry["command"] == "srv", "spec must be copied, not a bare deny"
        assert entry["disabledTools"] == ["t1", "t2"]
        assert "@some-server" not in out["tools"]

    def test_server_without_a_global_spec_is_skipped_not_emitted_bare(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {})
        agent = {"name": "a", "tools": [], "mcpServers": {}}
        out = bridges._apply_agent_mcp_policy(
            agent, "a", {"agents": {"a": {"neutralize": {"ghost-server": ["t"]}}}}
        )
        assert "ghost-server" not in out["mcpServers"]

    def test_every_emitted_entry_has_a_command(self, monkeypatch):
        """The invariant itself, over a mixed grant+neutralize merge."""
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {"n1": {"command": "c1"}})
        agent = {
            "name": "a",
            "tools": [],
            "mcpServers": {"own": {"command": "me", "args": []}},
        }
        out = bridges._apply_agent_mcp_policy(
            agent,
            "a",
            {
                "agents": {
                    "a": {
                        "servers": {"own": {"autoApprove": ["x"]}},
                        "neutralize": {"n1": ["t"], "n2": ["t"]},
                    }
                }
            },
        )
        missing = [k for k, v in out["mcpServers"].items() if not v.get("command")]
        assert missing == [], f"entries without command: {missing}"


class TestAppPromptPathIsContained:
    """An app's prompt path is app-controlled and read verbatim as the SYSTEM
    prompt — so it must resolve inside the app's own directories.

    Without the bound, an app writing ``file:///Users/me/.ssh/id_rsa`` into its
    policy hands a credential file to kiro-cli as the persona, and its contents
    reach the model. The path is only ever legitimately shipped inside the app or
    rendered into the app's data dir, so anything else is dropped.
    """

    def _call(self, tmp_path, raw):
        from kiro_crew.apps import bridges

        app_root = tmp_path / "app"
        app_root.mkdir(exist_ok=True)
        policy = {"agents": {"a": {"prompt": raw}}}
        return bridges._apply_agent_prompt({}, "a", policy, "someapp", app_root), app_root

    def test_a_path_outside_the_app_is_dropped(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges, manager

        monkeypatch.setattr(manager, "app_data_dir", lambda n: tmp_path / "data")
        monkeypatch.setattr(bridges, "app_data_dir", lambda n: tmp_path / "data")
        outside = tmp_path / "secret.txt"
        outside.write_text("KEY")
        merged, _ = self._call(tmp_path, f"file://{outside}")
        assert "prompt" not in merged  # escaping path refused

    def test_a_path_inside_the_app_root_is_kept(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges, manager

        monkeypatch.setattr(manager, "app_data_dir", lambda n: tmp_path / "data")
        monkeypatch.setattr(bridges, "app_data_dir", lambda n: tmp_path / "data")
        (tmp_path / "app").mkdir(exist_ok=True)
        prompt = tmp_path / "app" / "persona.md"
        prompt.write_text("you are")
        merged, _ = self._call(tmp_path, f"file://{prompt}")
        assert merged["prompt"] == f"file://{prompt.resolve()}"

    def test_a_symlink_escaping_the_app_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges, manager

        monkeypatch.setattr(manager, "app_data_dir", lambda n: tmp_path / "data")
        monkeypatch.setattr(bridges, "app_data_dir", lambda n: tmp_path / "data")
        (tmp_path / "app").mkdir(exist_ok=True)
        secret = tmp_path / "id_rsa"
        secret.write_text("KEY")
        link = tmp_path / "app" / "prompt.md"
        try:
            link.symlink_to(secret)
        except OSError:
            import pytest

            pytest.skip("symlinks unavailable")
        merged, _ = self._call(tmp_path, f"file://{link}")
        # resolve() follows the link out of the app root, so containment fails.
        assert "prompt" not in merged


class TestRebuildPreservesTheLiveMcpSpec:
    """A rebuild must keep the health-registered live-port spec, not re-copy the
    manifest's illustrative port.

    An auto-port app's manifest carries a fixed illustrative port; the reachable
    one is written to the app mcp.json only after the backend starts. Reading the
    manifest on every rebuild would stamp the dead port back over the live one,
    and kiro-cli dials every configured server — so the app's tools would break
    until the next reregister.
    """

    def test_live_registered_spec_wins_over_the_manifest(self, monkeypatch):
        from kiro_crew import agent

        class _M:
            mcpServers = {"srv": {"url": "http://127.0.0.1:9100/mcp"}}  # illustrative

        monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec: spec)
        import kiro_crew.apps.bridges as bridges
        import kiro_crew.apps.manager as manager

        monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
        monkeypatch.setattr(
            bridges,
            "registered_app_mcp_servers",
            lambda: {"someapp:srv": {"url": "http://127.0.0.1:54321/mcp"}},  # live
        )
        out = agent._collect_app_mcp_servers()
        assert out["someapp:srv"]["url"] == "http://127.0.0.1:54321/mcp"

    def test_http_server_with_no_live_entry_is_skipped(self, monkeypatch):
        from kiro_crew import agent

        class _M:
            mcpServers = {"srv": {"url": "http://127.0.0.1:9100/mcp"}}

        monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec: spec)
        import kiro_crew.apps.bridges as bridges
        import kiro_crew.apps.manager as manager

        monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
        monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
        out = agent._collect_app_mcp_servers()
        assert "someapp:srv" not in out  # dead-port URL never written

    def test_stdio_server_falls_back_to_the_manifest(self, monkeypatch):
        from kiro_crew import agent

        class _M:
            mcpServers = {"srv": {"command": "run", "args": ["x"]}}

        monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec: spec)
        import kiro_crew.apps.bridges as bridges
        import kiro_crew.apps.manager as manager

        monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
        monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
        out = agent._collect_app_mcp_servers()
        assert out["someapp:srv"]["command"] == "run"  # no port to resolve


class TestLegacyScrubIsLocked:
    """The pre-fix shared ~/.kiro/settings/mcp.json can be written by other
    processes (Kiro IDE, another agent). Scrubbing an app's entries there must
    hold that file's own lock across read+remove+write, or a concurrent writer's
    new server is lost to a stale read-modify-write.
    """

    def test_scrub_holds_the_legacy_file_lock(self) -> None:
        import inspect

        from kiro_crew.apps import bridges as bridges_mod

        src = inspect.getsource(bridges_mod._scrub_legacy_shared_mcp)
        assert "with _mcp_lock(target=_LEGACY_SHARED_MCP_PATH):" in src

    def test_scrub_removes_only_the_apps_entries(self, tmp_path, monkeypatch) -> None:
        import json

        from kiro_crew.apps import bridges as bridges_mod

        legacy = tmp_path / "mcp.json"
        legacy.write_text(
            json.dumps(
                {"mcpServers": {"myapp:srv": {"command": "x"}, "other:srv": {"command": "y"}}}
            )
        )
        monkeypatch.setattr(bridges_mod, "_LEGACY_SHARED_MCP_PATH", legacy)
        removed = bridges_mod._scrub_legacy_shared_mcp("myapp")
        assert removed == 1
        data = json.loads(legacy.read_text())
        assert "myapp:srv" not in data["mcpServers"]
        assert "other:srv" in data["mcpServers"], "another app's entry must survive"


class TestReregisterRefreshesAgents:
    """After an auto-port backend becomes healthy, reregister writes the live
    server to the global map — but the app's AGENTS copy that spec into their own
    config, so they must be refreshed too or the app agent can't reach its tools.
    """

    def test_reregister_refreshes_agents_after_registering(self, monkeypatch) -> None:
        from kiro_crew.apps import bridges as bridges_mod

        calls: list[str] = []
        monkeypatch.setattr(bridges_mod, "_registration_source", lambda n: (object(), "/app/root"))
        monkeypatch.setattr(bridges_mod, "_registration_denied", lambda *a, **k: "")
        # manifest with mcpServers truthy
        monkeypatch.setattr(
            bridges_mod, "_register_mcp_servers", lambda n, m, live_port=None: ["srv"]
        )
        monkeypatch.setattr(
            bridges_mod, "_register_agents", lambda n, m, r: calls.append(f"agents:{n}")
        )
        # give _registration_source a manifest with mcpServers

        class _M:
            mcpServers = {"srv": {"command": "x"}}

        monkeypatch.setattr(bridges_mod, "_registration_source", lambda n: (_M(), "/app/root"))
        out = bridges_mod.reregister_app_mcp_servers("myapp", live_port=54321)
        assert out == ["srv"]
        assert calls == ["agents:myapp"], "agents must be refreshed after live registration"


class TestMcpEnableHandlersOffloadTheSync:
    """`_sync_mcp_to_agent*` now walks the profile directory (profile-aware
    may_skip_gate_now), so the async MCP handlers must offload it or the gateway
    loop freezes on slow storage."""

    def test_handlers_offload_sync_to_agent(self) -> None:
        import re
        from pathlib import Path

        src = Path("src/kiro_crew/dashboard/handlers/mcp.py").read_text(encoding="utf-8")
        # No bare synchronous call to a PUBLIC sync-to-agent function inside an
        # async handler: every such invocation is wrapped in asyncio.to_thread.
        # The `_unlocked` variants are the sync locking-wrapper's own delegation
        # (it holds _mcp_lock then calls the body), which is intentional and not a
        # handler call — exclude them.
        bare = [
            m
            for m in re.findall(r"^\s+_sync_mcp_to_agent\w*\(", src, re.M)
            if "_unlocked(" not in m
        ]
        assert bare == [], f"un-offloaded sync-to-agent call(s): {bare}"
        assert "asyncio.to_thread(_sync_mcp_to_agent" in src


class TestRegisterPrunesUpgradedAwayResources:
    """A manifest UPGRADE that drops an agent or MCP server must un-register it.
    Pruning lives in the off-loop boot reconcile (reconcile_enabled_app_resources),
    NOT in register_app: register_app is called on the event loop by the
    enable/update handlers, where a directory walk + lock would stall chat, and
    those callers deregister first. The one path that re-registers without a
    preceding deregister — the boot reconcile — does the selective prune.
    """

    def test_stale_app_agent_and_server_are_pruned(self, tmp_path, app_env) -> None:
        import json as _json

        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)  # declares my-agent, no mcpServers
        install_app(src)
        register_app("test-app")
        assert (app_env["kiro_agents"] / "test-app--my-agent.json").is_file()

        # Simulate resources a PRIOR manifest version registered that the current
        # manifest no longer declares.
        ghost_link = app_env["kiro_agents"] / "test-app--ghost.json"
        ghost_link.write_text("{}", encoding="utf-8")
        with bridges_mod._mcp_lock():
            data = bridges_mod._read_mcp_json_unlocked()
            data.setdefault("mcpServers", {})["test-app:ghost"] = {"command": "x"}
            bridges_mod._write_mcp_json_unlocked(data)

        # register_app must NOT prune: it runs on the event loop, so the walk+lock
        # is kept off it. The ghosts survive a bare re-register.
        register_app("test-app")
        assert ghost_link.exists(), "register_app must not prune on the event loop"

        # The off-loop boot reconcile is the one path that prunes. Ensure the app
        # is enabled so reconcile processes it (it skips disabled apps).
        from kiro_crew.apps.manager import enable_app

        enable_app("test-app")
        bridges_mod.reconcile_enabled_app_resources()

        assert not ghost_link.exists(), "a removed agent must be pruned by reconcile"
        after = _json.loads(bridges_mod._mcp_json_path().read_text(encoding="utf-8"))
        assert "test-app:ghost" not in after.get(
            "mcpServers", {}
        ), "a removed server must be pruned"
        # The still-declared agent survives the prune.
        assert (app_env["kiro_agents"] / "test-app--my-agent.json").is_file()


class TestRegisterNeverDeletesBeforeReplacement:
    """No destination — regular file OR legacy symlink — is unlinked before the
    atomic write. atomic_write's os.replace swaps the NAME atomically for both, so
    a write that fails (disk full at startup) must leave the prior entry intact.
    """

    def test_a_legacy_symlink_survives_a_failed_rewrite(self, tmp_path, app_env, monkeypatch):
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        legacy_target = tmp_path / "legacy.json"
        legacy_target.write_text('{"name": "my-agent"}', encoding="utf-8")
        try:
            link.symlink_to(legacy_target)
        except OSError:
            import pytest

            pytest.skip("symlinks unavailable")

        def _boom(*a, **k):
            raise OSError("No space left on device")

        monkeypatch.setattr(bridges_mod, "atomic_write", _boom)
        _register_agents("test-app", manifest, app_root)  # swallows the OSError
        assert link.is_symlink(), "a failed write must not have unlinked the legacy symlink"


class TestPruneAbortsOnUnreadableAgent:
    """An unreadable declared agent is NOT a removed one: pruning on an incomplete
    current set would delete a still-declared agent's last-good config over a
    transient IO error. The agent prune aborts when any declared agent can't be
    read.
    """

    def test_unreadable_agent_aborts_the_agent_prune(self, tmp_path, app_env, monkeypatch):
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        # A materialized config that MUST survive if the prune aborts.
        keep = app_env["kiro_agents"] / "test-app--my-agent.json"
        keep.write_text('{"name": "my-agent"}', encoding="utf-8")
        # Make the declared agent source unreadable.
        (app_root / "agents" / "my-agent.json").write_text("{ not json", encoding="utf-8")

        bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)
        assert keep.is_file(), "prune must abort — not delete a config over an unreadable source"


class TestMalformedConfigIsNotClobbered:
    """A read-modify-write of an EXISTING-but-unreadable kirocrew.json must
    ABORT, not treat it as empty and overwrite it — that would drop the agent's
    whole configuration."""

    def test_strict_read_raises_on_malformed_existing(self, tmp_path, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "kirocrew.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Missing file -> empty map, both modes.
        assert bmod._read_mcp_json_unlocked() == {}
        assert bmod._read_mcp_json_unlocked(strict=True) == {}
        # Present but malformed: lenient degrades to {}, strict PROPAGATES.
        mcp_path.write_text("{ not valid json", encoding="utf-8")
        assert bmod._read_mcp_json_unlocked() == {}
        with pytest.raises((json.JSONDecodeError, OSError)):
            bmod._read_mcp_json_unlocked(strict=True)

    def test_register_does_not_overwrite_a_malformed_config(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "kirocrew.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        original = "{ this is not json and must survive"
        mcp_path.write_text(original, encoding="utf-8")

        manifest = AppManifest(name="app-a", mcpServers={"srv": {"command": "x"}})
        # register_app wraps _register_mcp_servers in try/except, so the strict
        # read's raise aborts the write; called directly it propagates.
        with pytest.raises((json.JSONDecodeError, OSError)):
            _register_mcp_servers("app-a", manifest)

        # The malformed-but-present config is UNTOUCHED (not clobbered with {}).
        assert mcp_path.read_text(encoding="utf-8") == original


class TestShippedAgentTemplatesAreRenderedByTheGateway:
    """A shipped template is rendered BY THE GATEWAY, from values it computes itself.

    An earlier version of this took the app's own provisioned copy from its install dir
    and verified it was "the template with only placeholders substituted". A reviewer
    pointed out why that is unsound and they were right: the check constrained WHERE a
    substitution could appear but not WHAT it could contain — and `{UV_BIN}` is an
    executable path, so an agent with write access to the engine directory could
    substitute its own binary and kiro-cli would run it.

    Every value is computable in the gateway, so nothing is read back from the mutable
    side at all: the bytes come from the immutable package, the values from here.
    """

    def test_placeholders_resolve_to_gateway_computed_values(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import _placeholder_values

        values = _placeholder_values("pptx-maker")
        assert set(values) == {
            "{UV_BIN}",
            "{ENGINE_ROOT}",
            "{ENGINE_MCP_DIR}",
            "{APP_PROMPTS}",
            "{TOOLS_PATH}",
        }
        # Under the data home this fixture set, i.e. derived here rather than read.
        assert str(app_env["home"]) in values["{ENGINE_ROOT}"]
        # `{TOOLS_PATH}` becomes the MCP server's PATH, and an empty element there
        # means the CWD on POSIX — tool resolution would depend on where kiro-cli
        # happened to start the server.
        assert "" not in values["{TOOLS_PATH}"].split(os.pathsep)

    def test_an_unknown_app_resolves_nothing(self, tmp_path, app_env):
        """Fail-closed: adding a placeholder to a new app's config is inert until its
        values are named, rather than silently registering an unrendered config."""
        from kiro_crew.apps.bridges import _placeholder_values

        assert _placeholder_values("some-other-app") == {}

    def test_a_template_is_rendered_into_the_data_home(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "command": "{UV_BIN}"}))

        out = _render_shipped_agent("pptx-maker", template)
        assert out is not None
        # NOT the app's install dir: the file kiro-cli reads must not be one the app
        # can rewrite after registration.
        assert (app_env["home"] / "apps" / "pptx-maker") not in out.parents
        rendered = json.loads(out.read_text(encoding="utf-8"))
        assert "{UV_BIN}" not in rendered["command"]
        # `Path(...).stem`, not `endswith("uv")`: on Windows `resolve_uv()` returns
        # `uv.exe`, so a suffix check on the bare name passed on POSIX and failed the
        # Windows shard — green on two platforms and red on the third is worse than
        # failing everywhere.
        assert Path(rendered["command"]).stem == "uv"

    def test_the_install_dir_copy_is_never_read(self, tmp_path, app_env):
        """The whole point of the redesign: an attacker-written copy in the install dir
        has no influence, because it is not consulted."""
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "command": "{UV_BIN}"}))

        install = app_env["home"] / "apps" / "pptx-maker" / "agents"
        install.mkdir(parents=True)
        (install / "a.json").write_text(
            json.dumps({"name": "a", "command": "/tmp/attacker-binary"})
        )

        out = _render_shipped_agent("pptx-maker", template)
        assert out is not None
        rendered = json.loads(out.read_text(encoding="utf-8"))
        assert rendered["command"] != "/tmp/attacker-binary"

    def test_a_config_with_no_placeholder_is_returned_untouched(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        concrete = shipped / "a.json"
        concrete.write_text(json.dumps({"name": "a", "command": "/real/uv"}))

        assert _render_shipped_agent("pptx-maker", concrete) == concrete

    def test_an_unresolvable_placeholder_registers_nothing(self, tmp_path, app_env):
        """Better to register no agent than one naming a literal `{ENGINE_ROOT}`."""
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "unknown-app" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "dir": "{ENGINE_ROOT}"}))

        assert _render_shipped_agent("unknown-app", template) is None

    def test_a_windows_path_survives_json_escaping(self, tmp_path, app_env):
        """The placeholders sit INSIDE JSON string literals, so a path's backslashes
        must be escaped or the render is invalid JSON (or a mangled separator)."""
        from unittest import mock

        from kiro_crew.apps import bridges

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "command": "{UV_BIN}"}))

        with mock.patch.object(
            bridges, "_placeholder_values", return_value={"{UV_BIN}": r"C:\Users\me\uv.exe"}
        ):
            out = bridges._render_shipped_agent("pptx-maker", template)
        assert out is not None
        # Parses, and the separator round-trips.
        assert json.loads(out.read_text(encoding="utf-8"))["command"] == r"C:\Users\me\uv.exe"


class TestRegisterAgentsSnapshotUpkeep:
    """`_register_agents` owns keeping the resolver's materialized-agent snapshot
    honest: it publishes what it writes, and it reconciles the directory even when
    it writes nothing, so a name pruned from disk stops being dispatchable."""

    def test_deregister_refreshes_the_snapshot(self, monkeypatch, tmp_path):
        # Removing an app's agent files must drop them from the resolver's
        # snapshot. Otherwise a disabled app's agent stays dispatchable in memory
        # and a slot still bound to it hands kiro-cli a name whose config is gone.
        from kiro_crew.apps import bridges

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "someapp--main.json").write_text(json.dumps({"name": "main"}), encoding="utf-8")

        calls: list[str] = []
        monkeypatch.setattr(bridges, "_kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(
            bridges, "schedule_materialized_agents_refresh", lambda: calls.append("refresh")
        )

        assert bridges._deregister_agents("someapp") == 1
        assert calls == ["refresh"]

    def test_deregister_without_removals_does_not_refresh(self, monkeypatch, tmp_path):
        from kiro_crew.apps import bridges

        agents = tmp_path / "agents"
        agents.mkdir()
        calls: list[str] = []
        monkeypatch.setattr(bridges, "_kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(
            bridges, "schedule_materialized_agents_refresh", lambda: calls.append("refresh")
        )

        assert bridges._deregister_agents("someapp") == 0
        assert calls == []

    def test_refresh_is_scheduled_even_when_nothing_was_registered(self, monkeypatch):
        # A re-registration whose manifest no longer declares an agent (or that
        # follows a prune) writes nothing. Skipping the rescan there would leave
        # the removed name dispatchable in memory, and kiro-cli would silently
        # fall back to its own default for a name it cannot load.
        from kiro_crew.apps import bridges

        calls: list[str] = []
        monkeypatch.setattr(
            bridges, "schedule_materialized_agents_refresh", lambda: calls.append("refresh")
        )
        monkeypatch.setattr(
            bridges,
            "publish_materialized_agents",
            lambda names: calls.append("publish"),
        )

        out = bridges._register_agents("someapp", SimpleNamespace(agents=[]), Path("/nonexistent"))

        assert out == []
        # Nothing to publish, but the directory is still reconciled.
        assert calls == ["refresh"]

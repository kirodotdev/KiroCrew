"""Tests for kiro_crew.apps.bridges — resource registration bridges."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

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
    (src / "agents" / "my-agent.json").write_text(
        json.dumps({"name": "my-agent", "model": "auto"})
    )
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
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)

    # Patch _MCP_JSON_PATH to avoid file descriptor errors in tests
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_MCP_JSON_PATH", mcp_path)

    return {"home": home, "kiro_agents": kiro_agents}


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

class TestNamespace:
    def test_namespace(self):
        assert _namespace("my-app", "agent-1") == "my-app/agent-1"

    def test_safe_link_name(self):
        assert _safe_link_name("my-app/agent-1") == "my-app--agent-1"


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

        # Verify symlink exists
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        assert link.is_symlink()
        # Verify it points to the right file
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

        # Verify symlink exists under ~/.kirocrew/skills/test-app/my-skill
        skill_link = app_env["home"] / "skills" / "test-app" / "my-skill"
        assert skill_link.is_symlink()
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
            crons=[{
                "name": "nightly-run",
                "cron_expr": "0 22 * * *",
                "agent": "my-agent",
                "enabled": False,
            }],
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
        # Regression: register_app_crons_with_service ends in
        # CronService.add_job -> _arm_timer -> asyncio.create_task, which needs
        # a RUNNING event loop. It must therefore run ON the loop, never offloaded
        # to a worker thread (which raised RuntimeError and left a half-persisted,
        # unowned cron behind). Driving it through a started CronService inside
        # this async test exercises the create_task path end-to-end.
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
            registered = register_app_crons_with_service("test-app", svc)
            assert "test-app/refresh" in registered
            # The job is fully added (owned) and the timer armed without error.
            assert any(j.name == "test-app/refresh" for j in svc.list_jobs())
            assert svc._timer_task is not None  # _arm_timer ran on the loop
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

    def test_register_nonexistent_app(self, app_env):
        result = register_app("nonexistent")
        assert len(result.errors) > 0

    def test_register_app_resources_app_skips_all(self, tmp_path, app_env, monkeypatch):
        """Apps with resources='app' manage their own registration.

        register_app must skip all bridge work (agents, skills, crons, MCP)
        to avoid creating duplicates that confuse kiro-cli.  This is the
        exact scenario that caused Mochi's subagent MCP tools to disappear:
        bridge created mochi-pet--mochi-pet-bg.json (empty mcpServers) alongside
        the real mochi-pet-bg.json, and kiro-cli loaded the empty one.
        """
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        src = _make_app_source(tmp_path, mcpServers={
            "backend": {"url": "http://localhost:8080/mcp"},
        })
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
        assert not any(
            f.name.startswith("test-app--")
            for f in app_env["kiro_agents"].iterdir()
        )
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
        assert not any(
            f.name.startswith("test-app--")
            for f in app_env["kiro_agents"].iterdir()
        )

    def test_register_deregister_cycle(self, tmp_path, app_env):
        """Register, deregister, re-register — no stale state."""
        src = _make_app_source(tmp_path)
        install_app(src)

        r1 = register_app("test-app")
        assert len(r1.agents) == 1

        deregister_app("test-app")
        # Verify clean
        assert not any(
            f.name.startswith("test-app--")
            for f in app_env["kiro_agents"].iterdir()
        )

        r2 = register_app("test-app")
        assert len(r2.agents) == 1


# ---------------------------------------------------------------------------
# RegistrationResult
# ---------------------------------------------------------------------------

class TestRegistrationResult:
    def test_to_dict(self):
        r = RegistrationResult(
            agents=["a/b"], skills=["a/s"], crons=["a/c"], errors=[]
        )
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Backend live → HTTP url server registers (the dead-port skip only fires when
        # no backend is up; see test_http_mcp_server_skipped_when_backend_not_yet_up).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        src = _make_app_source(tmp_path, mcpServers={
            "my-mcp": {"url": "http://localhost:9000/mcp"},
        })
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Pretend the backend actually came up on 9101 (not the manifest's 9100).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)

        src = _make_app_source(tmp_path, mcpServers={
            "my-mcp": {"url": "http://localhost:9100/mcp"},
        })
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(tmp_path, mcpServers={
            "my-mcp": {"url": "http://localhost:9100/mcp"},
        })
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        src = _make_app_source(tmp_path, mcpServers={
            "my-mcp": {"url": "http://localhost:9100/mcp"},
        })
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
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get("mcpServers", {})

    def test_stdio_mcp_server_always_registered_no_backend(self, tmp_path, app_env, monkeypatch):
        # A command/stdio MCP server (no url) has no port to be dead — it must always be
        # registered regardless of backend liveness (only HTTP url servers are gated).
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(tmp_path, mcpServers={
            "my-stdio": {"command": "my-server", "args": ["--stdio"]},
        })
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-stdio"]
        assert "test-app:my-stdio" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]

    def test_reregister_app_mcp_servers_overwrites_with_live_port(self, tmp_path, app_env, monkeypatch):
        # reregister_app_mcp_servers (called after the backend starts) overwrites the
        # earlier manifest-default entry with the live-port url.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        from kiro_crew.apps.bridges import reregister_app_mcp_servers
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        src = _make_app_source(tmp_path, mcpServers={
            "my-mcp": {"url": "http://localhost:9100/mcp"},
        })
        install_app(src)
        # First registration BEFORE the backend is up: HTTP server is skipped (no dead
        # entry written — the fail-safe that keeps kiro from dialing a dead port).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get("mcpServers", {})
        # Backend now up on 9101 → re-register writes it with the live port.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)
        reregister_app_mcp_servers("test-app")
        assert json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["test-app:my-mcp"]["url"] \
            == "http://localhost:9101/mcp"

    def test_explicit_live_port_rewrites_even_when_backend_unhealthy(self, tmp_path, app_env, monkeypatch):
        # The boot/enable path passes the just-allocated port explicitly because the
        # backend isn't marked *healthy* yet (get_app_backend_port would return None at
        # that instant). An explicit live_port must still rewrite the url — this is the
        # exact bug that left the registered url at :9100 while the backend was on :9101.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        from kiro_crew.apps.bridges import reregister_app_mcp_servers
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Health-gated lookup returns None (backend up but not yet confirmed healthy).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(tmp_path, mcpServers={"my-mcp": {"url": "http://localhost:9100/mcp"}})
        install_app(src)
        # Explicit live_port=9101 (from the spawn result) must win over the None lookup.
        reregister_app_mcp_servers("test-app", live_port=9101)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"

    def test_deregister_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        # Pre-populate with entries from two apps
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps({
            "mcpServers": {
                "app-a:srv1": {"url": "http://localhost:1"},
                "app-a:srv2": {"url": "http://localhost:2"},
                "app-b:srv1": {"url": "http://localhost:3"},
            }
        }))

        removed = _deregister_mcp_servers("app-a")
        assert removed == 2

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "app-a:srv1" not in data["mcpServers"]
        assert "app-a:srv2" not in data["mcpServers"]
        assert "app-b:srv1" in data["mcpServers"]

    def test_deregister_no_servers(self, tmp_path, monkeypatch):
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        assert _deregister_mcp_servers("nonexistent") == 0

    def test_register_no_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        manifest = AppManifest(name="test", mcpServers={})
        assert _register_mcp_servers("test", manifest) == []

    def test_register_app_includes_mcp(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Backend live so the HTTP url server is registered (not dead-port-skipped).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 8080)

        src = _make_app_source(tmp_path, mcpServers={
            "backend": {"url": "http://localhost:8080/mcp"},
        })
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
    # Feature: app-classification-redesign, Property 10: MCP 服务器注册命名空间
    @given(
        app_name=_app_name_st,
        servers=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:9000")}),
            min_size=1, max_size=5,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_register_namespace(self, app_name, servers, tmp_path, monkeypatch):
        """**Validates: Requirements 8.1, 8.2**"""
        import uuid

        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        mcp_path = tmp_path / f"mcp-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
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

    # Feature: app-classification-redesign, Property 11: MCP 服务器注销隔离性
    @given(
        app_a=_app_name_st,
        app_b=_app_name_st.filter(lambda s: len(s) > 1),
        servers_a=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:1")}),
            min_size=1, max_size=3,
        ),
        servers_b=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:2")}),
            min_size=1, max_size=3,
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Seed a stale entry as if a prior enable had registered it.
        mcp_path.write_text(json.dumps({"mcpServers": {
            "ai-app:backend": {"url": "http://localhost:9100/mcp"},
            "other:keep": {"command": "x"},
        }}) + "\n")

        # One installed-but-DISABLED app that declares an MCP server. list_apps is imported
        # inside start_enabled_app_backends from the manager module, so patch it there.
        monkeypatch.setattr(backend_mod, "list_apps", lambda: [
            {"name": "ai-app", "enabled": False,
             "manifest": {"mcpServers": {"backend": {"url": "http://localhost:9100/mcp"}},
                          "backend": {"entryPoint": "x"}}},
        ])
        # No backend should be started for a disabled app.
        monkeypatch.setattr(backend_mod, "start_app_backend",
                            lambda *_a, **_k: pytest.fail("must not start disabled app"))

        backend_mod.start_enabled_app_backends()

        remaining = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "ai-app:backend" not in remaining   # stale dead entry scrubbed
        assert "other:keep" in remaining            # unrelated entry untouched

    def test_enabled_app_never_healthy_mcp_entry_scrubbed(self, tmp_path, app_env, monkeypatch):
        # Review scenario: an ENABLED port:"auto" app registered with an optimistic
        # pre-health port whose backend never passes /health must NOT leave a dead HTTP MCP
        # url behind — that's the exact shape that broke every kiro-cli session. The
        # health-gated path calls _gate_mcp_registration(healthy=False) on health failure,
        # which scrubs the entry. (Closes the disabled-only asymmetry the reviewer flagged.)
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Seed an optimistic entry as if the pre-health register had written it.
        mcp_path.write_text(json.dumps({"mcpServers": {
            "test-app:backend": {"url": "http://localhost:9100/mcp"},
            "other:keep": {"command": "x"},
        }}) + "\n")

        backend_mod._gate_mcp_registration("test-app", 9100, healthy=False)

        remaining = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "test-app:backend" not in remaining  # dead enabled-app entry scrubbed
        assert "other:keep" in remaining            # unrelated entry untouched

    def test_enabled_app_healthy_registers_with_live_port(self, tmp_path, app_env, monkeypatch):
        # The complement: once /health passes, _gate_mcp_registration(healthy=True) writes the
        # HTTP MCP url with the confirmed live port (rewriting the manifest's illustrative one).
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        # Health-gated lookup returns None (port resolved from the explicit live_port instead).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        src = _make_app_source(tmp_path, mcpServers={"my-mcp": {"url": "http://localhost:9100/mcp"}})
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
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        mcp_path.write_text(json.dumps({"mcpServers": {}}) + "\n")

        monkeypatch.setattr(backend_mod, "list_apps", lambda: [
            {"name": "ai-app", "enabled": True,
             "manifest": {"mcpServers": {"backend": {"url": "http://localhost:9100/mcp"}},
                          "backend": {"entryPoint": "x"}}},
        ])
        # Spawn returns a not-yet-healthy process (the real pre-health state).
        fake_ap = SimpleNamespace(port=9101, healthy=False)
        monkeypatch.setattr(backend_mod, "start_app_backend", lambda *_a, **_k: fake_ap)
        # If the boot loop tries to register before health, fail loudly.
        monkeypatch.setattr(backend_mod, "_gate_mcp_registration",
                            lambda *_a, **_k: pytest.fail("must not register before health"))

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

    def test_registers_cron_with_all_fields(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{
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
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job.return_value = MagicMock(id="abc123")

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == ["test-app/refresh"]
        mock_sdk.add_job.assert_called_once_with(
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

        cron_defs = [{
            "name": "test-app/nightly-run",
            "every": 0,
            "cron_expr": "0 22 * * *",
            "agent": "discovery",
            "message": "",
            "app": "test-app",
            "enabled": False,
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job.return_value = MagicMock(id="abc123")

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == ["test-app/nightly-run"]
        assert mock_sdk.add_job.call_args.kwargs["enabled"] is False

    def test_legacy_defs_without_enabled_default_active(self, tmp_path, app_env, monkeypatch):
        """Pre-existing app-crons.json without the enabled key registers active."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{
            "name": "test-app/legacy",
            "every": 600,
            "agent": "a",
            "message": "m",
            "app": "test-app",
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job.return_value = MagicMock(id="abc123")

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            register_app_crons_with_service("test-app", mock_cron_service)

        assert mock_sdk.add_job.call_args.kwargs["enabled"] is True

    def test_startup_skips_existing_disabled_job(self, tmp_path, app_env, monkeypatch):
        """Gateway-startup re-registration must not re-add (and thus re-pause)
        a job that already exists in a disabled state.

        CronSDK.list_jobs() includes disabled jobs, so a paused job counts as
        existing — preserving a user's resume/pause state across restarts.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{
            "name": "test-app/nightly-run",
            "every": 0,
            "cron_expr": "0 22 * * *",
            "agent": "discovery",
            "message": "",
            "app": "test-app",
            "enabled": False,
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        existing = MagicMock()
        existing.name = "test-app/nightly-run"
        existing.enabled = False  # currently paused
        existing.user_paused = True

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing]

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == []
        mock_sdk.add_job.assert_not_called()
        # The existing job's state is untouched — no duplicate, no re-pause.
        assert existing.enabled is False
        assert existing.user_paused is True

    def test_registers_command_type_cron(self, tmp_path, app_env, monkeypatch):
        """Apps declaring command-type crons get them registered as command jobs."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{
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
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job.return_value = MagicMock(id="cmd123")

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == ["test-app/collect"]
        mock_sdk.add_job.assert_called_once_with(
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

        cron_defs = [{
            "name": "test-app/evil",
            "every": 60,
            "command": "cat ~/.aws/credentials",
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == []
        mock_sdk.add_job.assert_not_called()

    def test_rejects_invalid_script_path(self, tmp_path, app_env, monkeypatch):
        """Scripts outside ~/.kirocrew/crons/ are rejected at registration."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{
            "name": "test-app/bad-script",
            "every": 60,
            "script": "/etc/passwd:run",
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == []
        mock_sdk.add_job.assert_not_called()

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
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == []
        mock_sdk.add_job.assert_not_called()

    def test_returns_empty_when_no_cron_service(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import register_app_crons_with_service

        result = register_app_crons_with_service("test-app", None)
        assert result == []

    def test_returns_empty_when_no_app_crons_file(self, tmp_path, app_env):
        from unittest.mock import MagicMock

        from kiro_crew.apps.bridges import register_app_crons_with_service

        result = register_app_crons_with_service("nonexistent-app", MagicMock())
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
        mock_sdk.add_job.return_value = MagicMock(id="x")

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

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
        mock_sdk.add_job.side_effect = [RuntimeError("boom"), MagicMock(id="ok")]

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        # Failed entry skipped, good entry registered
        assert result == ["test-app/good"]
        assert mock_sdk.add_job.call_count == 2


class TestCronServiceDeregister:
    """Tests for deregister_app_crons_from_service — scheduler cleanup helper."""

    def test_returns_zero_when_no_cron_service(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        assert deregister_app_crons_from_service("test-app", None) == 0

    def test_calls_remove_all_and_returns_count(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all.return_value = 3

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = deregister_app_crons_from_service("test-app", mock_cron_service)

        assert result == 3
        mock_sdk.remove_all.assert_called_once()

    def test_returns_zero_on_exception(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all.side_effect = RuntimeError("scheduler unavailable")

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = deregister_app_crons_from_service("test-app", mock_cron_service)

        assert result == 0  # exception swallowed, zero returned

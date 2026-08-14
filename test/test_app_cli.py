"""Tests for the `kirocrew app` CLI subcommand."""
from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="cli-test-app", crons=None):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "CLI Test App",
        "description": "App for CLI testing",
        "author": "tester",
        "agents": ["agents/test-agent.json"],
        "skills": ["skills/test-skill"],
    }
    if crons is not None:
        manifest["crons"] = crons
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    (src / "agents").mkdir()
    (src / "agents" / "test-agent.json").write_text('{"name": "test-agent"}')
    (src / "skills" / "test-skill").mkdir(parents=True)
    (src / "skills" / "test-skill" / "SKILL.md").write_text("# Test Skill")
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(
        "kiro_crew.apps.execution.third_party_execution_allowed", lambda: True
    )
    return {"home": home, "kiro_agents": kiro_agents}


# ---------------------------------------------------------------------------
# _handle_app unit tests (call the function directly, not subprocess)
# ---------------------------------------------------------------------------

class TestHandleApp:
    """Test _handle_app by simulating argparse Namespace objects."""

    def test_install_and_list(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)

        # Install
        ns = argparse.Namespace(app_action="install", source=str(src))
        _handle_app(ns)  # should not raise

        # List
        from kiro_crew.apps.manager import list_apps
        apps = list_apps()
        assert len(apps) == 1
        assert apps[0]["name"] == "cli-test-app"

    def test_enable_disable(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)

        # Enable
        ns = argparse.Namespace(app_action="enable", name="cli-test-app")
        _handle_app(ns)
        from kiro_crew.apps.manager import _read_installed
        meta = _read_installed("cli-test-app")
        assert meta is not None
        assert meta.enabled is True

        # Disable
        ns = argparse.Namespace(app_action="disable", name="cli-test-app")
        _handle_app(ns)
        meta = _read_installed("cli-test-app")
        assert meta is not None
        assert meta.enabled is False

    def test_info(self, tmp_path, app_env, capsys):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)

        ns = argparse.Namespace(app_action="info", name="cli-test-app")
        _handle_app(ns)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["name"] == "cli-test-app"
        assert "manifest" in data

    def test_uninstall_preserves_data_by_default(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)
        data_file = app_env["home"] / "apps" / "cli-test-app" / "data" / "state.json"
        data_file.write_text('{"saved": true}')

        ns = argparse.Namespace(
            app_action="uninstall", name="cli-test-app", purge_data=False
        )
        _handle_app(ns)

        from kiro_crew.apps.manager import get_app

        assert get_app("cli-test-app") is None
        assert data_file.read_text() == '{"saved": true}'

    def test_uninstall_purge_data_requires_explicit_flag(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)
        app_dir = app_env["home"] / "apps" / "cli-test-app"
        (app_dir / "data" / "state.json").write_text('{"saved": true}')

        ns = argparse.Namespace(
            app_action="uninstall", name="cli-test-app", purge_data=True
        )
        _handle_app(ns)

        assert not app_dir.exists()

    def test_install_invalid_source(self, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        ns = argparse.Namespace(app_action="install", source="/nonexistent")
        with pytest.raises(SystemExit):
            _handle_app(ns)

    def test_no_action_prints_usage(self, app_env, capsys):
        import argparse

        from kiro_crew.cli import _handle_app

        ns = argparse.Namespace(app_action=None)
        _handle_app(ns)
        captured = capsys.readouterr()
        assert "Usage" in captured.out


class TestEnableRegistersCrons:
    """CLI `app enable` must promote manifest crons into the scheduler store.

    The HTTP enable route promotes app crons into the running CronService via
    ``hooks_integration.on_app_enable``; the CLI runs in a separate process, so
    it must write the jobs through the shared on-disk store instead (the
    gateway's timer tick re-syncs the store by content digest and picks up
    externally-added jobs). Without that write, an app enabled from the CLI
    has its crons lie dormant until the next gateway restart.
    """

    CRONS = [{"name": "poller", "every": 900, "message": "poll things", "silent": True}]

    def _store_job_names(self):
        from kiro_crew.config import config_dir
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=config_dir())
        return [j.name for j in svc.list_jobs(include_disabled=True)]

    def _enable(self):
        import argparse

        from kiro_crew.cli import _handle_app

        ns = argparse.Namespace(app_action="enable", name="cli-test-app")
        _handle_app(ns)

    def test_enable_registers_manifest_crons_in_store(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, crons=self.CRONS)
        install_app(src)

        assert self._store_job_names() == []  # nothing before enable
        self._enable()

        names = self._store_job_names()
        assert names == ["cli-test-app/poller"]

        # Ownership tag must match what disable-side cleanup removes by.
        from kiro_crew.config import config_dir
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=config_dir())
        job = svc.list_jobs(include_disabled=True)[0]
        assert job.created_by == "app:cli-test-app"

    def test_enable_twice_is_idempotent(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, crons=self.CRONS)
        install_app(src)

        self._enable()
        self._enable()

        assert self._store_job_names() == ["cli-test-app/poller"]

    def test_concurrent_registration_persists_single_job(self, tmp_path, app_env):
        """Two registrars with stale absent-snapshots must not persist duplicates.

        Reproduces the CLI-enable-vs-gateway-boot race at the primitive the
        bridge now routes through: two store-backed services both see the name
        absent, then add concurrently. ``add_job_if_absent_async`` (name check
        + append under one store lock) must let exactly one win — with the old
        snapshot-then-``add_job_async`` path this persists two jobs.
        """
        import asyncio

        from kiro_crew.apps.cron_sdk import CronSDK
        from kiro_crew.config import config_dir
        from kiro_crew.cron import CronService

        async def _race() -> list:
            # Two independent store-backed services, as in separate processes;
            # both snapshot the store before either has appended.
            svc_a = CronService(base_dir=config_dir())
            svc_a._load()
            svc_b = CronService(base_dir=config_dir())
            svc_b._load()
            return await asyncio.gather(
                CronSDK("cli-test-app", svc_a).add_job_if_absent_async(
                    "cli-test-app/poller", "poll things", every_secs=900
                ),
                CronSDK("cli-test-app", svc_b).add_job_if_absent_async(
                    "cli-test-app/poller", "poll things", every_secs=900
                ),
            )

        results = asyncio.run(_race())

        assert self._store_job_names() == ["cli-test-app/poller"]
        # Exactly one registrar won; the loser observed the existing job.
        assert sorted(r is None for r in results) == [False, True]

    def test_disable_removes_registered_crons(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path, crons=self.CRONS)
        install_app(src)
        self._enable()
        assert self._store_job_names() == ["cli-test-app/poller"]

        ns = argparse.Namespace(app_action="disable", name="cli-test-app")
        _handle_app(ns)

        assert self._store_job_names() == []

    def test_enable_without_crons_registers_nothing(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)  # no crons in manifest
        install_app(src)

        self._enable()

        assert self._store_job_names() == []

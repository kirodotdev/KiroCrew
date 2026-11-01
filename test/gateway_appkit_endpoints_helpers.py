"""Shared fixtures and builders for the ``test_gateway_appkit_endpoints*.py`` family.

``app_env`` installs a test app under a temporary Kiro Crew home, with the app
bridges and backend process tables isolated, and ``_make_state`` builds a
minimal ``DashboardState`` without running its constructor.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.dashboard.state import DashboardState


@pytest.fixture(name="app_env")
def app_env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temp app environment with a test app installed."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    app_dir = home / "apps" / "test-app"
    app_dir.mkdir(parents=True)
    manifest = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "For testing",
        "author": "tester",
    }
    (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    installed = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "enabled": True,
        "managed": "kirocrew",
    }
    (app_dir / "installed.json").write_text(json.dumps(installed))
    # Stub bridges to avoid touching real kiro agents dir
    import kiro_crew.apps.bridges as bridges_mod

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod

    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


def _make_state(tmp_path: Path) -> DashboardState:
    """Create a minimal DashboardState for testing."""
    from unittest.mock import MagicMock

    state = DashboardState.__new__(DashboardState)
    state._sessions = MagicMock()
    state._crons = MagicMock()
    state._lessons = MagicMock()
    # flake-ok: pre-existing real wall-clock read that stamps the stub dashboard state start time the uptime readers compute an age from, moved verbatim; tracked for a de-flake pass
    state._start_time = time.time()
    state._subagents = None
    state._context_builder = None
    state._conversation_log = None
    state._consolidator = None
    state._task_runner = None
    state._slack_client = None
    state._owner_id = ""
    state._notification_log = []
    state._unread_count = 0
    state._slots = {}
    state._slack_to_slot = {}
    state._slot_counter = 0
    state._yolo = False
    state._yolo_expires = 0.0
    state._folders = []
    state._hook_store = MagicMock()
    state.channel_manager = None
    return state

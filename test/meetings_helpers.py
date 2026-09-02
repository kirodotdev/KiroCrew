"""Shared fixtures and helpers for the Meetings app tests.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Three things every test needs:

* ``root`` — a tmp data dir, threaded through ``store``'s ``root=`` parameter so
  no test ever touches the real ``~/.kiro/crew/apps/meetings``.
* ``app`` — an aiohttp Application with only this app's routes registered, the
  enable gate stubbed open, and the data root override stashed on the app.
* ``FakeSessionManager`` — records dispatches instead of running them. No test
  spawns a real kiro-cli process or opens a socket.

Clients are created with ``TestClient(TestServer(app))`` (the pattern the rest of
the suite uses) rather than aiohttp's pytest plugin, so the fixtures do not
depend on plugin load order.

This is a PLAIN module, not a ``conftest.py``, and each fixture is declared with
an explicit ``name=`` so importing it does not collide with the test-function
parameter of the same name (flake8 F811). A module that wants these fixtures
imports the ``*_fixture`` symbols; ``pytest_plugins`` is deliberately NOT used
because it registers globally, which would leak the autouse
``_reset_module_state`` onto all ~22k tests in the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.routes import _common, register_routes


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    """An isolated data root with the app's subtree already created."""
    data = tmp_path / "meetings-data"
    store.ensure_data_dirs(data)
    return data


@pytest.fixture(name="_reset_module_state", autouse=True)
def reset_module_state_fixture():
    """No test may leak the active meeting or the dictionary into the next one."""
    _common.ACTIVE.clear()
    sess.shared_dictionary().load_terms([])
    yield
    _common.ACTIVE.clear()
    sess.shared_dictionary().load_terms([])


@pytest.fixture(name="enabled")
def enabled_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the deny-by-default enable gate open.

    The gate reads ``installed.json``, absent under a tmp home, so a real read
    would 403 every request. ``test_authorization.py`` deliberately does NOT use
    this fixture, so the closed path is covered too.
    """
    monkeypatch.setattr(_common, "is_app_enabled", lambda _name: True)


def make_app(root: Path) -> web.Application:
    """An aiohttp Application carrying only this app's routes."""
    application = web.Application()
    application["_meetings_data_root"] = root
    register_routes(application)
    return application


@pytest.fixture(name="app")
def app_fixture(root: Path, enabled: None) -> web.Application:
    return make_app(root)


def client_for(app: web.Application) -> TestClient:
    """A test client for *app*. Use as ``async with client_for(app) as client:``."""
    return TestClient(TestServer(app))


class FakeSessionManager:
    """Records every dispatch's (key, agent, prompt) instead of running one."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        self.fail = fail

    async def get_or_create(self, key: str, agent: str | None = None, **_kwargs):
        if self.fail:
            raise RuntimeError("session unavailable")
        return _FakeProvider(self, key, agent or ""), True, False

    def release(self, key: str) -> None:
        self.released.append(key)

    def prompts_for(self, agent_id: str) -> list[str]:
        """Every prompt sent to *agent_id*'s slot, in order."""
        return [msg for key, _agent, msg in self.calls if f"-{agent_id}-" in key]


class _FakeProvider:
    """A provider whose ``stream`` yields nothing and records the prompt."""

    def __init__(self, manager: FakeSessionManager, key: str, agent: str) -> None:
        self._manager = manager
        self._key = key
        self._agent = agent

    async def stream(self, message: str):
        self._manager.calls.append((self._key, self._agent, message))
        if False:  # pragma: no cover — makes this an async generator
            yield None

    async def approve_tool(self, request_id):  # pragma: no cover
        return None

    async def reject_tool(self, request_id):  # pragma: no cover
        return None


def install_sessions(app: web.Application, manager: FakeSessionManager) -> FakeSessionManager:
    """Attach a fake session manager to *app* the way the gateway attaches its own."""

    class _State:
        sessions = manager
        context_builder = None

    app["state"] = _State()
    return manager


@pytest.fixture(name="fake_sessions")
def fake_sessions_fixture(app: web.Application) -> FakeSessionManager:
    return install_sessions(app, FakeSessionManager())

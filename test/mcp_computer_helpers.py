"""Fixtures and dispatch helpers shared by the ``test_mcp_computer*.py`` modules.

``home`` points ``KIROCREW_HOME`` at a tmp dir so the keystone lands there,
``keystone`` is its path and ``_enable`` writes it ON, and ``fake_backend`` installs
the shipped fake backend with clean shared singletons. ``_dispatch`` and ``_get_state``
run one tool through the gateway dispatcher as an attended session. The fixtures are
declared ``@pytest.fixture(name=...)`` and imported under their ``*_fixture`` function
names, so a test's parameter does not shadow the import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.computer_use import backend as cu_backend
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import service as cu_service
from kiro_crew.computer_use import tools as cu_tools
from kiro_crew.computer_use.types import STATE_FILE_NAME, TOOL_GET_STATE
from kiro_crew.testing.fake_computer_use import FAKE_FILES_APP, FakeComputerUseBackend

# A session key that looks attended. The gate refuses unattended surfaces
# (``cron:``/``subagent:``/``taskrunner``) even with no profile on disk, so an
# interactive-looking key is what lets these tests reach the dispatcher body.
_SESSION = "dashboard:main"


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``KIROCREW_HOME`` so the keystone lands in a tmp dir.

    Mandatory, not hygiene: without it a developer's real
    ``~/.kiro/crew/computer_use.json`` would decide whether these tests see the
    feature as enabled.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(name="keystone")
def keystone_fixture(home: Path) -> Path:
    return home / STATE_FILE_NAME


def _enable(keystone: Path, **extra: Any) -> None:
    """Write the keystone primary enable ON."""
    keystone.write_text(json.dumps({"enabled": True, **extra}), encoding="utf-8")


@pytest.fixture(name="fake_backend")
def fake_backend_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeComputerUseBackend:
    """Install the shipped fake backend and a clean service + snapshot index.

    The fake is registered process-wide through the same seam a downstream suite
    would use, and the shared service/index singletons are dropped so a snapshot
    from another test cannot resolve an element index here.
    """
    instance = FakeComputerUseBackend()
    cu_backend.register_computer_use_backend(lambda: instance)
    cu_backend.reset_shared_backend()
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()
    yield instance
    cu_backend.register_computer_use_backend(None)
    cu_backend.reset_shared_backend()
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()


def _dispatch(tool: str, **args: Any) -> str:
    """Run one tool through the gateway dispatcher as an attended session."""
    return cu_tools.dispatch_tool(tool, args, session_key=_SESSION)


def _get_state(app: str = FAKE_FILES_APP.name) -> str:
    """Prime the snapshot index (every action tool requires it first)."""
    return _dispatch(TOOL_GET_STATE, app=app)

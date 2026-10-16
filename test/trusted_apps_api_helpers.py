"""Fixtures, app records and the in-test client shared by ``test_trusted_apps_api*.py``.

``home`` and ``mock_sel`` are declared ``@pytest.fixture(name=...)`` and imported
under their function names. ``_install`` writes an app record under the tmp
``KIROCREW_HOME``; ``_client`` builds a ``TestClient`` that each test opens with
``async with``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    install_app,
)
from kiro_crew.dashboard.handlers.security import (
    api_trusted_app_grant,
    api_trusted_app_revoke,
    api_trusted_apps_allow_all,
    api_trusted_apps_list,
)

_APP = "trust-test-app"
_OTHER = "other-test-app"


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME so config.json + installed apps land in tmp."""
    h = tmp_path / "kirocrew-home"
    h.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    return h


@pytest.fixture(name="mock_sel")
def mock_sel_fixture():
    """Patch the late-bound ``_sel()`` so SEL audit calls are observable."""
    with patch("kiro_crew.dashboard.handlers.security._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _install(
    tmp_path: Path,
    name: str,
    *,
    enabled: bool = False,
    builtin: bool = False,
    entry_point: str | None = None,
    port: int | str | None = None,
    body: str = "",
    source_repository: str = "",
) -> None:
    """Install a minimal app record under the tmp KIROCREW_HOME.

    ``builtin=True`` marks the install builtin-OWNED (``source``/``origin`` ==
    ``builtin``), which is what ``manager.builtin_owns_installed`` checks. Combined
    with a *name* a shipped ``app.json`` declares (``_BUILTIN``), that is the only
    way a name enters ``execution.builtin_app_names()``.

    ``entry_point`` declares a gateway-spawned backend so the app has real code to
    stop; *body* is written to that file. ``port`` declares that backend's port —
    a fixed number is observable after a stop attempt, ``"auto"`` is not.
    ``source_repository`` models a registry install when the test pre-seeds a
    repository-bound execution grant.
    """
    source = tmp_path / "source" / name
    source.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "trust grant fixture",
        "author": "tester",
    }
    if entry_point:
        manifest["backend"] = {"entryPoint": entry_point, "runtime": "python"}
        if port is not None:
            manifest["backend"]["port"] = port
        (source / entry_point).write_text(body, encoding="utf-8")
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert install_app(source, source_repository=source_repository).ok
    meta = _read_installed(name)
    assert meta is not None
    meta.enabled = enabled
    meta.origin = "builtin" if builtin else "registry"
    if builtin:
        meta.source = "builtin"
    meta.resources = "gateway"
    _write_installed(name, meta)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/security/trusted-apps", api_trusted_apps_list)
    app.router.add_put("/api/security/trusted-apps/allow-all", api_trusted_apps_allow_all)
    app.router.add_post("/api/security/trusted-apps/{name}", api_trusted_app_grant)
    app.router.add_delete("/api/security/trusted-apps/{name}", api_trusted_app_revoke)
    # The three mutating routes are owner-gated
    # (``handlers._shared.require_owner_dashboard_request``); ``as_owner`` supplies
    # the claims the token-auth middleware normally publishes, so each test keeps
    # exercising its own subject rather than the gate.
    return as_owner(app)


def _client() -> TestClient:
    """Build an aiohttp TestClient — open with ``async with`` inside each test."""
    return TestClient(TestServer(_make_app()))


def _stored(home: Path) -> dict[str, Any]:
    """The persisted ``agent`` section of config.json (``{}`` before first save)."""
    path = home / "config.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    agent = data.get("agent")
    return agent if isinstance(agent, dict) else {}

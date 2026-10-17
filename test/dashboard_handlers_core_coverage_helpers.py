"""Request stubs and fixtures shared by the ``dashboard/handlers/core.py`` coverage tests.

``test_dashboard_handlers_core_coverage.py`` and its ``_stt_runtime``,
``_security_endpoints`` and ``_agent_settings`` siblings import these by name. The two
fixtures are declared with ``name=`` and imported under their function names, so a
test parameter never shadows an import.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.config.loader import config_path


def _req(
    *,
    remote: str = "127.0.0.1",
    headers: dict | None = None,
    query: dict | None = None,
    app: dict | None = None,
    match_info: dict | None = None,
    user: str | None = "dashboard",
    app_token: str | None = "",
) -> web.Request:
    """A stub request carrying only what these handlers read.

    ``app_token`` is the verified app claim the auth middleware publishes, and it
    is a different thing from ``app`` (the aiohttp application): ``""`` means the
    dashboard user, a name means an app token, and ``None`` reproduces a path
    where no auth middleware ran and the claim is absent.

    The claims are exposed through ``in`` and ``[]`` as well as ``get``, because
    an owner-gated handler distinguishes an ABSENT app claim from an empty one
    and reads it with ``"app" in request``.
    """
    req = MagicMock(spec=web.Request)
    req.remote = remote
    req.headers = headers or {}
    req.query = query or {}
    req.app = app if app is not None else {}
    req.match_info = match_info or {}
    claims: dict = {"user": user, "app": app_token}
    # The auth predicates read a claim three ways -- ``.get``, ``in`` and ``[]`` --
    # so all three answer from one dict instead of returning default MagicMocks.
    req.get = lambda key, default=None: claims.get(key, default)
    req.__contains__.side_effect = lambda key: key in claims and claims[key] is not None
    req.__getitem__.side_effect = lambda key: claims[key]
    return req


def _json_req(body: object = None, *, raises: bool = False, **kwargs) -> web.Request:
    """A stub request whose ``await request.json()`` yields *body*.

    ``raises=True`` reproduces an absent or unparseable body, which the prepare
    endpoint has to read as "the configured model" rather than as an error.
    """
    req = _req(**kwargs)
    req.json = AsyncMock(side_effect=ValueError("no body") if raises else None, return_value=body)
    return req


@pytest.fixture(name="fake_sel")
def fake_sel_fixture(monkeypatch) -> MagicMock:
    """Swap the audited SEL seam for a recorder.

    ``core._sel()`` late-binds through the handlers package, so patching the
    package attribute is what the handler observes.
    """
    recorder = MagicMock()
    recorder.dropped_events = 0
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: recorder)
    return recorder


@pytest.fixture(name="seeded_config")
def seeded_config_fixture() -> Path:
    """Write a minimal config into the isolated home and return its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"agent": {"approval_mode": "auto"}}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _seed_stt(path: Path, **fields) -> None:
    """Merge *fields* into the ``stt`` section of the config at *path*.

    Gives a test a configured value that is NOT the default, so "used the
    configured model" is distinguishable from "fell back to the catalog default".
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("stt", {}).update(fields)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8", newline="\n")

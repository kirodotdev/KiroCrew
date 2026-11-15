"""PATCH of a number field with `true` or a fraction still saves as before, and says so.

`int(True)` is 1 and `int(3600.9)` is 3600, and `float(True)` is 1.0 on a float
field with no clamp. Refusing them would newly reject bodies main accepts, so the
value is saved exactly as before and a warning names what was stored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

_LOGGER = "kiro_crew.dashboard.handlers.core"


def _app():
    from aiohttp import web
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard import handlers

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
    return as_owner(app)


async def _patch(path: str, value: object) -> int:
    from aiohttp.test_utils import TestClient, TestServer

    with patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()):
        async with TestClient(TestServer(_app())) as c:
            resp = await c.patch("/api/config/kirocrew", json={"path": path, "value": value})
            return resp.status


def _saved(cfg: Path, dotted: str) -> object:
    node: object = json.loads(cfg.read_text(encoding="utf-8"))
    for part in dotted.split("."):
        assert isinstance(node, dict)
        node = node[part]
    return node


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value", "stored"),
    [
        ("session.pool_size", True, 1),
        ("session.timeout_secs", 3600.9, 3600),
        ("agent.soft_stop_budget_secs", True, 1.0),
    ],
)
async def test_a_non_whole_number_saves_as_before_with_a_warning(
    cfg_file: Path, caplog: pytest.LogCaptureFixture, path: str, value: object, stored: object
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await _patch(path, value) == 200
    assert _saved(cfg_file, path) == stored
    assert any(path in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_whole_number_saves_with_no_warning(
    cfg_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await _patch("session.timeout_secs", 3600) == 200
    assert _saved(cfg_file, "session.timeout_secs") == 3600
    assert not any("session.timeout_secs" in r.getMessage() for r in caplog.records)

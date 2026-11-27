"""PATCH of a float setting with an integer too large for a float answers 400.

A 400-digit JSON integer is a valid body, and ``float()`` of it raises
``OverflowError``. The float branch caught only ``(TypeError, ValueError)``, so the
owner's settings save answered a server error where every sibling branch answers 400.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)


def _app():
    from aiohttp import web
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard import handlers

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
    return as_owner(app)


async def _patch_raw(path: str, value_text: str) -> tuple[int, dict]:
    """Send the value as literal JSON text, so ``1e999`` and a long integer arrive as written."""
    from aiohttp.test_utils import TestClient, TestServer

    body = '{"path": %s, "value": %s}' % (json.dumps(path), value_text)
    with patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()):
        async with TestClient(TestServer(_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", data=body, headers={"Content-Type": "application/json"}
            )
            text = await resp.text()
            return resp.status, (json.loads(text) if resp.status < 500 else {})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["session.autocompact_pct", "agent.soft_stop_budget_secs"])
@pytest.mark.parametrize("sign", ["", "-"])
async def test_an_integer_too_large_for_a_float_is_refused_with_400(
    cfg_file: Path, path: str, sign: str
) -> None:
    status, answer = await _patch_raw(path, sign + "9" * 400)
    assert status == 400
    assert answer["error"] == "must be a number"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value_text", "status", "error"),
    [
        ("1e999", 400, "must be a finite number"),
        ("true", 400, "must be between 5.0 and 90.0"),
        ('"75"', 200, None),
        ("75", 200, None),
    ],
)
async def test_the_other_float_bodies_answer_as_before(
    cfg_file: Path, value_text: str, status: int, error: str | None
) -> None:
    got, answer = await _patch_raw("session.autocompact_pct", value_text)
    assert got == status
    if error is not None:
        assert answer["error"] == error


@pytest.mark.asyncio
async def test_a_float_key_of_an_object_setting_is_refused_with_400_too(cfg_file: Path) -> None:
    """The object branch converts its float keys the same way, so it shares the fix."""
    record = '{"version": "1.0.0", "snoozed_until": %s, "skipped": false}' % ("9" * 400)
    status, answer = await _patch_raw("dashboard.update_nudge", record)
    assert status == 400
    assert answer["error"] == "snoozed_until must be a number"

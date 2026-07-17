"""Test for the /api/health liveness endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import core as core_mod


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    resp = await core_mod.api_health(MagicMock(spec=web.Request))
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True}

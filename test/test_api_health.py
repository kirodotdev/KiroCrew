"""Test for the /api/health liveness endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import core as core_mod


@pytest.mark.asyncio
async def test_health_returns_ok_with_identity() -> None:
    """The payload carries identity fields (app, version) for the desktop
    shell's cross-app instance guard: nightly and production apps share
    ~/.kirocrew and the gateway port, so the shell must be able to tell
    WHICH KiroCrew-family gateway owns the port."""
    from kiro_crew import __version__

    resp = await core_mod.api_health(MagicMock(spec=web.Request))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["app"] == "kirocrew"
    assert body["version"] == __version__

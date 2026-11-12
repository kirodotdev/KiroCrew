"""POST /api/reveal refuses a non-string path with 400, not a 500 TypeError."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import api_reveal_path


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/reveal", api_reveal_path)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [5, 1.5, True, ["/tmp/a"], {"p": "/tmp/a"}])
async def test_reveal_path_non_string_is_400(mock_sel, path):
    """A non-string path is caller error: 400 invalid path, not a 500 TypeError."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/reveal", json={"path": path, "action": "reveal"})
        assert resp.status == 400
        assert await resp.json() == {"error": "invalid path"}
        mock_sel.log_tool_invocation.assert_not_called()

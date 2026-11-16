"""The gateway stub toggle logs a non-string server name instead of saying nothing.

``POST /api/mcp-gateway/servers/stub`` reads ``name`` with ``str()``, so ``null`` or
``123`` is saved as the server ``"None"`` / ``"123"`` under a plain ``ok`` with no
trace. Refusing it would newly reject a body main saves, so it is saved and answered
exactly as before, and the gateway log names it.
"""

from __future__ import annotations

import json
import logging

import pytest
from mcp_launch_approval_helpers import _stub_request  # noqa: F401
from mcp_launch_approval_helpers import toggle_home_fixture  # noqa: F401

_LOGGER = "kiro_crew.dashboard.handlers.mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", [None, 123, False])
async def test_a_non_string_name_saves_as_before_and_is_logged(
    toggle_home, caplog, bad_name
) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _stub_request({"name": bad_name, "stub": False})
        )
    body = json.loads(resp.body)

    assert resp.status == 200, body
    assert body["name"] == str(bad_name)
    assert "warnings" not in body
    logged = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    assert any(
        f"not a string; saved as {str(bad_name)!r}" in m for m in logged
    ), f"no warning logged: {logged}"


@pytest.mark.asyncio
async def test_a_string_name_logs_no_name_warning(toggle_home, caplog) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert resp.status == 200
    assert not any("not a string" in r.getMessage() for r in caplog.records)

"""The gateway stub toggle names a non-string server name instead of saying nothing.

``POST /api/mcp-gateway/servers/stub`` reads ``name`` with ``str()``, so ``null`` or
``123`` is saved as the server ``"None"`` / ``"123"`` under a plain ``ok``. Refusing
it would newly reject a body main saves, so it is saved as before and the reply
carries a ``warnings`` list.
"""

from __future__ import annotations

import json

import pytest
from mcp_launch_approval_helpers import _stub_request  # noqa: F401
from mcp_launch_approval_helpers import toggle_home_fixture  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", [None, 123, False])
async def test_a_non_string_name_saves_as_before_and_is_named(toggle_home, bad_name) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": bad_name, "stub": False}))
    body = json.loads(resp.body)

    assert resp.status == 200, body
    assert body["name"] == str(bad_name)
    assert body.get("warnings") == [f"name is not a string; saved as {str(bad_name)!r}"]


@pytest.mark.asyncio
async def test_a_string_name_carries_no_warnings(toggle_home) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert resp.status == 200
    assert "warnings" not in json.loads(resp.body)

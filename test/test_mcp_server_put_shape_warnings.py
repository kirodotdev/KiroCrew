"""``PUT /api/mcp/servers/{name}`` names a wrongly shaped field instead of saying nothing.

The route writes into the Kiro-global ``mcp.json``, which kiro-cli reads too. A list
``command``, string ``args`` or list ``env`` was stored with a bare 200. Refusing them
would newly reject bodies main saves, so they are saved exactly as before and the
reply carries a ``warnings`` list naming each one.

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _read_global, _request

from kiro_crew.dashboard.handlers import mcp as mcp_mod


@pytest.fixture(autouse=True)
def _owned_home(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent home is ours, so the route reaches its mcp.json step."""
    _floor_monkeypatch.setattr(mcp_mod, "_agent_home_not_owned", lambda _name: None)


async def _put(body: dict):
    return await mcp_mod.api_mcp_server_detail(
        _request(body, match_info={"name": "newsrv"}, method="PUT")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "warning"),
    [
        ({"command": ["node", "s.js"]}, "'command' must be a non-empty string"),
        ({"command": "node", "args": "s.js"}, "'args' must be a list of strings"),
        ({"command": "node", "env": ["A=1"]}, "'env' must be an object of string values"),
    ],
)
async def test_a_wrongly_shaped_field_saves_as_before_and_is_named(
    sandbox: SimpleNamespace, body: dict, warning: str
) -> None:
    resp = await _put(body)

    assert resp.status == 200, _payload(resp)
    assert _payload(resp).get("warnings") == [warning]
    assert _read_global(sandbox)["newsrv"]["command"] == body["command"]


@pytest.mark.asyncio
async def test_a_well_shaped_body_carries_no_warnings(sandbox: SimpleNamespace) -> None:
    resp = await _put({"command": "node", "args": ["s.js"], "env": {"A": "1"}})

    assert resp.status == 200, _payload(resp)
    assert "warnings" not in _payload(resp)

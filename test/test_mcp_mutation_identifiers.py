"""The MCP mutation endpoints must type-check their identifiers.

``toggle``, ``toggle-tool`` and ``remove`` used to call ``.strip()`` directly
on ``name`` / ``server`` / ``tool``, so a truthy non-string from a malformed
client (an array, an object, a number) surfaced as HTTP 500 — AttributeError —
before any validation ran. Worse, the 500 could happen while the handler
already held the config lock or after persistence seams had been reached on
sibling paths. These tests pin the contract: a wrong field TYPE is a
deterministic 400 with a stable machine-readable code, and NO mutation seam
(lock, mcp.json write) is touched; missing/blank identifiers keep the exact
pre-existing required-field responses (#5621).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload

from kiro_crew.dashboard.handlers import mcp as h


def _req(body: object) -> web.Request:
    app = web.Application()
    raw = json.dumps(body).encode()
    req = make_mocked_request(
        "POST",
        "/api/mcp/toggle",
        app=app,
        headers={"Content-Length": str(len(raw))},
        payload=BodyStreamPayload(raw),
    )
    return req


def _payload(resp: web.Response) -> dict:
    """json_response builds the body eagerly, so tests can read it directly."""
    return json.loads(resp.text)


@pytest.fixture(autouse=True)
def _sealed_mutation_seams(monkeypatch: pytest.MonkeyPatch):
    """No identifier rejection may reach a mutation seam: the config lock and
    the mcp.json writer stay untouched for every bad-type payload below."""
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=False)
    lock_mock = MagicMock(return_value=lock)
    write_mock = MagicMock()
    monkeypatch.setattr(h, "_get_mcp_lock", lock_mock)
    monkeypatch.setattr(h, "_write_mcp_json", write_mock)
    yield {"lock": lock_mock, "write": write_mock}


# ── wrong TYPES are deterministic 400s with stable codes ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[], {}, 5])
async def test_toggle_rejects_a_non_string_name(bad, _sealed_mutation_seams) -> None:
    resp = await h.api_mcp_toggle(_req({"name": bad, "enabled": True}))
    assert resp.status == 400
    assert _payload(resp)["code"] == "mcp.name_not_string"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,bad", [("server", {}), ("tool", [])])
async def test_toggle_tool_rejects_non_string_identifiers(
    field, bad, _sealed_mutation_seams
) -> None:
    body = {"server": "s", "tool": "t", "enabled": True}
    body[field] = bad
    resp = await h.api_mcp_toggle_tool(_req(body))
    assert resp.status == 400
    assert _payload(resp)["code"] == f"mcp.{field}_not_string"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"n": 1}, 7])
async def test_remove_rejects_a_non_string_name(bad, _sealed_mutation_seams) -> None:
    resp = await h.api_mcp_remove(_req({"name": bad}))
    assert resp.status == 400
    assert _payload(resp)["code"] == "mcp.name_not_string"


# ── the rejection happens BEFORE any mutation seam is touched ───────────


@pytest.mark.asyncio
async def test_a_bad_type_never_enters_the_config_lock(_sealed_mutation_seams) -> None:
    await h.api_mcp_toggle(_req({"name": ["x"]}))
    _sealed_mutation_seams["lock"].return_value.__aenter__.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bad_type_never_writes_mcp_json(_sealed_mutation_seams) -> None:
    await h.api_mcp_remove(_req({"name": {"deep": 1}}))
    _sealed_mutation_seams["write"].assert_not_called()


# ── missing / blank keep the pre-existing responses verbatim ────────────


@pytest.mark.asyncio
async def test_missing_name_keeps_the_original_required_response() -> None:
    resp = await h.api_mcp_toggle(_req({}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_blank_name_keeps_the_original_required_response() -> None:
    resp = await h.api_mcp_toggle(_req({"name": "   "}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_blank_server_and_tool_keep_the_original_combined_response() -> None:
    resp = await h.api_mcp_toggle_tool(_req({"server": "", "tool": ""}))
    assert resp.status == 400

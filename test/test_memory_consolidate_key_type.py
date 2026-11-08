"""``POST /api/memory/consolidate`` refuses a session key that is not a string.

``read_bounded_json`` guarantees the body is a JSON object, not the type of any
field in it. A non-string ``key`` such as ``{"key": 1}`` is answered with 400 and
a machine-readable code, and nothing is dispatched, the way the missing-key case
already answers. Without the type check ``.strip()`` raises ``AttributeError``,
which aiohttp turns into a 500.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _body,
    _make_request,
    _make_state,
    _reset_setup_status,
)

import kiro_crew.dashboard.handlers.memory as mem_mod


def _consolidator() -> Any:
    c = MagicMock()
    c._running = set()
    c._busy = lambda key: key in c._running
    c._tasks = set()
    c._consolidate = AsyncMock(return_value=None)
    return c


async def _call(req: Any) -> web.Response:
    try:
        return await mem_mod.api_memory_consolidate(req)
    except Exception as exc:
        # What aiohttp's request handler does with an exception that escapes.
        return web.Response(status=500, text=repr(exc))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [1, 0, 1.5, True, None, [], ["s1"], {}, {"k": "s1"}], ids=repr)
async def test_non_string_key_is_a_400_with_a_code(key: Any) -> None:
    cons = _consolidator()
    state = _make_state(consolidator=cons)
    req = _make_request(state, method="POST", json_body={"key": key}, session_key="dashboard:ui")
    resp = await _call(req)
    assert resp.status == 400, resp.text
    assert _body(resp) == {
        "error": "session key must be a string",
        "code": "invalid_session_key",
    }
    # Refused before anything acts on the value.
    cons._consolidate.assert_not_called()
    assert not cons._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": "   "}], ids=repr)
async def test_missing_or_blank_key_keeps_the_existing_refusal(body: dict[str, Any]) -> None:
    """The control: an omitted or blank key still gets the unchanged missing-key answer."""
    cons = _consolidator()
    state = _make_state(consolidator=cons)
    req = _make_request(state, method="POST", json_body=body, session_key="dashboard:ui")
    resp = await _call(req)
    assert resp.status == 400
    assert _body(resp) == {"error": "session key required"}
    cons._consolidate.assert_not_called()

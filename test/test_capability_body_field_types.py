"""A non-string ``server_id`` / ``package`` on the MCP and skill capability
mutations is a 400, not an unhandled ``AttributeError`` -> 500.

``read_bounded_json`` already refuses a body that is not a JSON object, but the
four handlers then did ``body.get("<field>", "").strip()``: a valid object whose
field is a number, list or object (``{"server_id": 1}``, ``{"package": []}``)
reached ``.strip()`` on a non-string and escaped the handler. The same module's
``_mutate_agent_package`` already answers that shape with a 400; these tests pin
the other four mutations to the same contract. Validation sits after the owner
gate, which these tests stub open and which is unchanged.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List

import pytest
from aiohttp import web

import kiro_crew.platform.context as platform_context
from kiro_crew.dashboard.handlers import agents as agents_handler

_HANDLERS = {
    "api_capability_mcp_install": "server_id",
    "api_capability_mcp_uninstall": "server_id",
    "api_capability_skills_install": "package",
    "api_capability_skills_uninstall": "package",
}

_NON_STRINGS = [1, 0, 1.5, True, None, [], ["a"], {}, {"a": "b"}]


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Pass the owner gate: the behavior under test is past it."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class _Manager:
    """Records every call; unavailable, so a string field stops at a 503."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def available(self) -> bool:
        self.calls.append("available")
        return False


@pytest.fixture()
def manager(monkeypatch) -> _Manager:
    mgr = _Manager()

    class _Ctx:
        capability_manager = mgr

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    return mgr


@pytest.fixture()
def ownership_probe(monkeypatch) -> List[Any]:
    """Stub the MCP handlers' agent-home ownership preflight, recording its arg."""
    seen: List[Any] = []

    def _probe(server_id: Any) -> None:
        seen.append(server_id)
        return None

    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp._agent_home_not_owned", _probe)
    return seen


class _Request:
    """A request ``read_bounded_json(max_bytes=None)`` accepts."""

    content_type = "application/json"
    can_read_body = True

    def __init__(self, body: Any) -> None:
        self._body = body
        self.app = {"state": None}

    async def json(self) -> Any:
        return self._body


def _call(handler: str, body: Any) -> web.Response:
    try:
        return asyncio.run(getattr(agents_handler, handler)(_Request(body)))
    except Exception as exc:
        # What aiohttp's request handler does with an exception that escapes.
        return web.Response(status=500, text=repr(exc))


@pytest.mark.parametrize("value", _NON_STRINGS, ids=repr)
@pytest.mark.parametrize("handler", sorted(_HANDLERS))
def test_non_string_field_is_a_400(handler, value, manager, ownership_probe):
    field = _HANDLERS[handler]
    resp = _call(handler, {field: value})
    assert resp.status == 400, resp.text
    assert json.loads(resp.body.decode()) == {"error": f"{field} required"}
    # Refused before anything acts on the value.
    assert manager.calls == []
    assert ownership_probe == []


@pytest.mark.parametrize("handler", sorted(_HANDLERS))
def test_missing_or_blank_field_is_still_a_400(handler, manager, ownership_probe):
    field = _HANDLERS[handler]
    for body in ({}, {field: ""}, {field: "   "}):
        resp = _call(handler, body)
        assert resp.status == 400
        assert json.loads(resp.body.decode()) == {"error": f"{field} required"}
    assert manager.calls == []


@pytest.mark.parametrize("handler", sorted(_HANDLERS))
def test_string_field_is_stripped_and_passes_validation(handler, manager, ownership_probe):
    """Control: a string still clears validation (stripped) and reaches the manager."""
    field = _HANDLERS[handler]
    resp = _call(handler, {field: "  SomePkg  "})
    assert resp.status == 503
    assert manager.calls == ["available"]
    if field == "server_id":
        assert ownership_probe == ["SomePkg"]

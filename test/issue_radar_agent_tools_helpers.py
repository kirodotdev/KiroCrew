"""Drive Issue Radar's agent tools through the real ``kirocrew-core`` call path.

The three tools are declared in the app's shipped ``app.json`` (``agentTools``) and
served by ``backend/agent_tools.py``. :func:`forwarded` runs a call the way production
does, ``mcp_core._call_tool_inner`` -> ``app_tools.app_agent_tool`` (the strict-session
gate, the post, the redaction and the cap) -> the app's ``handle_agent_tool_call``, with
two things replaced: the gateway hop (``mcp_core._post`` hands the body straight to the
app's handler, as ``/api/app-tools/call`` does through the route registry) and the
app's own route legs, which answer from the dicts a test passes. A leg answer carrying
``"error"`` is a refusal (HTTP 409), as a route's error body is.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.builtins.issue_radar.backend import agent_tools, crew_routes
from kiro_crew.validation import validate_tool_args

READ_TOOL = "issue_radar_crew_read"
RECORD_TOOL = "issue_radar_crew_record"
INVESTIGATION_TOOL = "issue_radar_record_investigation"
TOOLS = (INVESTIGATION_TOOL, READ_TOOL, RECORD_TOOL)
#: The calling crew session the strict resolver answers by default.
CREW_SESSION = "crew-c_7f3a"


def shipped(name: str) -> app_agent_tools.DeclaredTool:
    """The tool *name* as the app's shipped manifest declares it."""
    return next(t for t in app_agent_tools.shipped_tools() if t.name == name)


def descriptor(name: str) -> dict[str, Any]:
    """The ``tools/list`` descriptor of *name*."""
    return shipped(name).decl.descriptor()


def clean(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """*args* validated as the core validates a call of *name* (raises ``ValidationError``)."""
    return validate_tool_args(args, app_agent_tools.build_tool_schema(shipped(name)))


def _declared(name: str) -> app_agent_tools.DeclaredTool | None:
    return next((t for t in app_agent_tools.shipped_tools() if t.name == name), None)


def _answer(payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload, status=409 if "error" in payload else 200)


def _as_post_result(resp: web.Response) -> dict[str, Any]:
    payload = json.loads(resp.text or "{}")
    if resp.status >= 400:
        return {"error": str(payload.get("error", resp.status))}
    return payload


@contextmanager
def listed() -> Iterator[None]:
    """The app's tools listed, as for an installed and enabled Issue Radar."""
    with (
        patch.object(app_agent_tools, "declared_tools", app_agent_tools.shipped_tools),
        patch("kiro_crew.mcp_tools.app_tools.app_enabled_state", lambda name: True),
    ):
        yield


@contextmanager
def forwarded(
    *,
    session_key: str = CREW_SESSION,
    read: dict[str, Any] | None = None,
    work: Any = None,
    investigation: Any = None,
) -> Iterator[dict[str, Any]]:
    """Patch the gateway hop and the app's legs; yield what the legs were handed.

    *read* answers the crew read leg, *work* the crew write leg (a dict, or a callable
    of the body), *investigation* the investigation write (likewise). The yielded dict
    gains ``get_session_key`` / ``put_session_key`` (the ``X-Session-Key`` each crew leg
    saw), ``body`` (what the write leg was handed) and ``post`` (the forwarded call).
    """
    captured: dict[str, Any] = {}

    async def fake_read(request: web.Request) -> web.Response:
        captured["get_session_key"] = request.headers.get("X-Session-Key", "")
        captured["reads"] = captured.get("reads", 0) + 1
        return _answer({} if read is None else read)

    async def fake_work(request: web.Request, body: dict[str, Any]) -> web.Response:
        captured["put_session_key"] = request.headers.get("X-Session-Key", "")
        captured["body"] = body
        if callable(work):
            return _answer(work(body))
        if work is not None:
            return _answer(work)
        return _answer(
            {"item": {"phase": body.get("phase") or "claimed", "next": body.get("next") or ""}}
        )

    async def fake_investigation(body: dict[str, Any]) -> web.Response:
        captured["body"] = body
        if callable(investigation):
            return _answer(investigation(body))
        return _answer(investigation if investigation is not None else {"investigation": {}})

    def fake_post(path: str, body: Any = None, *, timeout: float = 30, session_key: Any = None):
        captured["post"] = {"path": path, "body": body, "session_key": session_key}
        headers = {"Content-Type": "application/json"}
        if session_key:
            headers["X-Session-Key"] = session_key
        request = make_mocked_request("POST", path, headers=headers)
        request["internal_auth"] = True
        # The handler reads the body the gateway already read; hand it the bytes.
        request._read_bytes = json.dumps(body).encode()
        return _as_post_result(asyncio.run(agent_tools.handle_agent_tool_call(request)))

    with ExitStack() as stack:
        stack.enter_context(patch.object(app_agent_tools, "declared_tool", _declared))
        stack.enter_context(
            patch.object(mcp_core, "_resolve_session_key_strict", return_value=session_key)
        )
        stack.enter_context(patch.object(mcp_core, "_post", side_effect=fake_post))
        stack.enter_context(patch.object(crew_routes, "crew_read_for_session", fake_read))
        stack.enter_context(patch.object(crew_routes, "crew_work_for_session", fake_work))
        stack.enter_context(
            patch.object(agent_tools.investigation, "put_investigation", fake_investigation)
        )
        yield captured


def call(name: str, args: dict[str, Any]) -> str:
    """One ``tools/call`` of *name* with already-validated *args*, through the core."""
    return mcp_core._call_tool_inner(name, args)

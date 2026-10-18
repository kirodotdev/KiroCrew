"""The native browser panel's command-bus routes: what each answers for each body and outcome.

``POST /api/browser/command`` (the ``browser`` MCP tool's call), ``/command-drain`` (the
Electron main process's long-poll) and ``/command-result`` (its answer) live in
``kiro_crew.dashboard.messaging_api.browser_routes`` and are reached through the messaging
handlers. Each validates its body before it touches the command bus, maps every bus outcome
to the status the caller acts on -- a fast 503 so the tool falls back to playwright-cli, 429,
504, 204 for an empty poll, 404 for an unknown result -- and refuses a caller without the
proven internal secret, recording the refusal in the SEL. The bus is a stand-in that records
what each route handed it, so no panel, queue or clock is involved.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.browser.command_bus import (
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_DRAIN_WAIT_MS,
    NoPanelError,
    QueueFullError,
)
from kiro_crew.dashboard.handlers import messaging as mod


class _Bus:
    """Records each call; answers with the outcome a test sets, or raises it."""

    def __init__(
        self,
        *,
        outcome: dict[str, Any] | None = None,
        raises: BaseException | None = None,
        drained: dict[str, Any] | None = None,
        matched: bool = True,
    ) -> None:
        self.outcome = outcome or {}
        self.raises = raises
        self.drained = drained
        self.matched = matched
        self.calls: list[tuple[Any, ...]] = []

    async def submit(
        self, session_key: str, op: str, args: dict[str, Any], *, timeout_ms: int
    ) -> dict[str, Any]:
        self.calls.append(("submit", session_key, op, args, timeout_ms))
        if self.raises is not None:
            raise self.raises
        return self.outcome

    async def drain(self, session_keys: list[str], *, wait_ms: int) -> dict[str, Any] | None:
        self.calls.append(("drain", session_keys, wait_ms))
        return self.drained

    async def complete(
        self, command_id: str, ok: bool, *, result: Any = None, error: str | None = None
    ) -> bool:
        self.calls.append(("complete", command_id, ok, result, error))
        return self.matched


@pytest.fixture
def sel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock()
    monkeypatch.setattr(mod, "_sel", lambda: log)
    return log


def _bus(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Bus:
    bus = _Bus(**kwargs)
    monkeypatch.setattr(mod, "get_command_bus", lambda: bus)
    return bus


def _request(path: str, body: object, *, internal: bool = True, raw: bytes | None = None):
    req = make_mocked_request("POST", path)
    if internal:
        req["internal_auth"] = True
    req._read_bytes = raw if raw is not None else json.dumps(body).encode()
    return req


def _json(resp) -> dict[str, Any]:
    return json.loads(resp.body)


_ROUTES = (
    ("/api/browser/command", "api_browser_command", "browser_command"),
    ("/api/browser/command-drain", "api_browser_command_drain", "browser_command_drain"),
    ("/api/browser/command-result", "api_browser_command_result", "browser_command_result"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "route", "operation"), _ROUTES)
async def test_a_caller_without_the_internal_secret_is_refused_and_audited(
    path: str, route: str, operation: str, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch)
    resp = await getattr(mod, route)(_request(path, {"op": "x"}, internal=False))
    assert resp.status == 403
    assert _json(resp)["code"] == "loopback_only"
    assert sel.log_api_access.call_args.kwargs["operation"] == operation
    assert sel.log_api_access.call_args.kwargs["outcome"] == "denied"
    assert bus.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "route", "_operation"), _ROUTES)
@pytest.mark.parametrize("raw", [b"not json", b"[]", b"null"], ids=["unparsable", "list", "null"])
async def test_a_body_that_is_not_a_json_object_is_a_400(
    path: str,
    route: str,
    _operation: str,
    raw: bytes,
    sel: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _bus(monkeypatch)
    resp = await getattr(mod, route)(_request(path, None, raw=raw))
    assert resp.status == 400
    assert _json(resp)["code"] == "invalid_json"
    assert bus.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"session_key": "s"}, 400, "op_required"),
        ({"op": "", "session_key": "s"}, 400, "op_required"),
        ({"op": "click", "args": [1], "session_key": "s"}, 400, "args_must_be_object"),
        ({"op": "click"}, 503, "no_native_panel"),
        ({"op": "click", "session_key": ""}, 503, "no_native_panel"),
    ],
    ids=["no-op", "empty-op", "args-not-object", "no-session", "empty-session"],
)
async def test_a_command_the_bus_cannot_address_never_reaches_it(
    body: dict[str, Any], status: int, code: str, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch)
    resp = await mod.api_browser_command(_request("/api/browser/command", body))
    assert resp.status == status
    assert _json(resp)["code"] == code
    assert bus.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_ms", "submitted"),
    [
        (2500, 2500),
        (None, DEFAULT_COMMAND_TIMEOUT_MS),
        (0, DEFAULT_COMMAND_TIMEOUT_MS),
        (-5, DEFAULT_COMMAND_TIMEOUT_MS),
        (True, DEFAULT_COMMAND_TIMEOUT_MS),
        ("9", DEFAULT_COMMAND_TIMEOUT_MS),
    ],
    ids=["given", "missing", "zero", "negative", "bool", "string"],
)
async def test_a_command_is_submitted_with_a_positive_integer_timeout(
    timeout_ms: object, submitted: int, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch, outcome={"id": "c1", "ok": True, "result": {"title": "t"}})
    body = {"op": "snapshot", "session_key": "slot-1", "timeout_ms": timeout_ms}
    resp = await mod.api_browser_command(_request("/api/browser/command", body))
    assert resp.status == 200
    assert _json(resp) == {"id": "c1", "ok": True, "result": {"title": "t"}}
    assert bus.calls == [("submit", "slot-1", "snapshot", {}, submitted)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "answer"),
    [
        ({"id": "c2", "ok": False, "error": "no such element"}, "no such element"),
        ({"id": "c3", "ok": False}, "error"),
    ],
    ids=["reported", "unreported"],
)
async def test_a_failed_op_answers_200_with_its_error(
    outcome: dict[str, Any], answer: str, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch, outcome=outcome)
    body = {"op": "click", "session_key": "slot-1", "args": {"ref": "e1"}}
    resp = await mod.api_browser_command(_request("/api/browser/command", body))
    assert resp.status == 200
    assert _json(resp) == {"id": outcome["id"], "ok": False, "error": answer}
    assert bus.calls[0][3] == {"ref": "e1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (NoPanelError("none"), 503, "no_native_panel"),
        (QueueFullError("full"), 429, "queue_full"),
        (asyncio.TimeoutError(), 504, "timeout"),
    ],
    ids=["no-panel", "queue-full", "timeout"],
)
async def test_each_bus_refusal_maps_to_the_status_the_tool_acts_on(
    raised: BaseException, status: int, code: str, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bus(monkeypatch, raises=raised)
    body = {"op": "click", "session_key": "slot-1"}
    resp = await mod.api_browser_command(_request("/api/browser/command", body))
    assert resp.status == status
    assert _json(resp)["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_keys", [None, "slot-1", ["slot-1", 2]], ids=["missing", "not-a-list", "non-string"]
)
async def test_a_drain_needs_a_list_of_session_keys(
    session_keys: object, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch)
    body = {"session_keys": session_keys}
    resp = await mod.api_browser_command_drain(_request("/api/browser/command-drain", body))
    assert resp.status == 400
    assert _json(resp)["code"] == "session_keys_invalid"
    assert bus.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wait_ms", "drained_with"),
    [
        (0, 0),
        (750, 750),
        (None, DEFAULT_DRAIN_WAIT_MS),
        (-1, DEFAULT_DRAIN_WAIT_MS),
        (False, DEFAULT_DRAIN_WAIT_MS),
    ],
    ids=["heartbeat", "given", "missing", "negative", "bool"],
)
async def test_an_empty_drain_answers_204_after_waiting_the_valid_time(
    wait_ms: object, drained_with: int, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch, drained=None)
    body = {"session_keys": [], "wait_ms": wait_ms}
    resp = await mod.api_browser_command_drain(_request("/api/browser/command-drain", body))
    assert resp.status == 204
    assert bus.calls == [("drain", [], drained_with)]


@pytest.mark.asyncio
async def test_a_drain_hands_the_queued_command_to_the_panel(
    sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = {"id": "c4", "session_key": "slot-1", "op": "goto", "args": {"url": "u"}}
    bus = _bus(monkeypatch, drained=command)
    body = {"session_keys": ["slot-1"], "wait_ms": 10}
    resp = await mod.api_browser_command_drain(_request("/api/browser/command-drain", body))
    assert resp.status == 200
    assert _json(resp) == command
    assert bus.calls == [("drain", ["slot-1"], 10)]


@pytest.mark.asyncio
@pytest.mark.parametrize("command_id", [None, "", 7], ids=["missing", "empty", "not-a-string"])
async def test_a_result_needs_the_command_id(
    command_id: object, sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch)
    body = {"id": command_id, "ok": True}
    resp = await mod.api_browser_command_result(_request("/api/browser/command-result", body))
    assert resp.status == 400
    assert _json(resp)["code"] == "id_required"
    assert bus.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "completed"),
    [
        ({"id": "c5", "ok": True, "result": [1]}, ("complete", "c5", True, [1], None)),
        ({"id": "c6", "ok": 0, "error": "boom"}, ("complete", "c6", False, None, "boom")),
        ({"id": "c7", "error": {"why": 1}}, ("complete", "c7", False, None, "{'why': 1}")),
    ],
    ids=["success", "failure", "non-string-error"],
)
async def test_a_result_completes_its_command(
    body: dict[str, Any],
    completed: tuple[Any, ...],
    sel: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _bus(monkeypatch, matched=True)
    resp = await mod.api_browser_command_result(_request("/api/browser/command-result", body))
    assert resp.status == 200
    assert _json(resp) == {"ok": True}
    assert bus.calls == [completed]


@pytest.mark.asyncio
async def test_a_result_for_an_unknown_command_is_a_404(
    sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _bus(monkeypatch, matched=False)
    body = {"id": "gone", "ok": True}
    resp = await mod.api_browser_command_result(_request("/api/browser/command-result", body))
    assert resp.status == 404
    assert _json(resp)["code"] == "unknown_command"
    assert bus.calls == [("complete", "gone", True, None, None)]

"""A session subscribes, lists and removes its own resource subscriptions (RFC Phase 4).

``rfc-mcp-resource-subscriptions.md`` §4.9.3 and the Phase 4 row of §5. The
routes in ``dashboard/handlers/resource_subscriptions.py`` are driven with the
real service; the gateway tests run a real gatewayd against
``fake_pool_mcp_server.py`` and a real stub claimed for the session, as the
Phase 2 tests do. Session recognition is replaced, as ``test_session_ledger.py``
does: its own suites cover it.

Pinned here:

* a session can subscribe, list and remove its own subscriptions, and
  subscribing again keeps the id and replaces the instruction;
* each refusal of §4.9.3 is returned with its code and a reason, and the tools
  relay it;
* another session's id is answered ``not_found`` and never listed;
* every route refuses a caller that did not present the internal secret before
  it reads the session key, changing nothing, and the CLI exits non-zero on that
  refusal;
* the operator route refuses a session, and the CLI removes through the store
  when no service runs;
* with the switch off nothing on the route-registration path imports the
  feature (owner requirement 3).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import subprocess
import sys
import threading
import urllib.error
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from fake_pool_mcp_server import recorded
from mcp_resource_subscription_helpers import INIT_PARAMS, StubProcess, running_daemon, spawn_stub

from kiro_crew import cli_resource_subs, mcp_core
from kiro_crew import platform_compat as pc
from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
from kiro_crew.dashboard.handlers import resource_subscriptions as routes
from kiro_crew.mcp_gateway import resource_subscriptions as rs
from kiro_crew.mcp_gateway.backend_leases import RESOURCE_URI_MAX_LEN
from kiro_crew.mcp_gateway.claim import mint_stub_session_token, send_claim
from kiro_crew.mcp_gateway.resource_holder import HolderPaused
from kiro_crew.mcp_gateway.resource_subscription_store import (
    STORE_DIR_LEAF,
    HolderRecord,
    ResourceSubscriptionStore,
    SubscriptionMeta,
)
from kiro_crew.mcp_gateway.resource_subscriptions import ResourceSubscriptionService
from kiro_crew.mcp_tools import resources as tools
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import async_wait_until, default_timeout, finished

SESSION = "dashboard:tool-tab"
OTHER = "dashboard:other-tab"
CHANNEL = "C_TOOLS"
SERVER = "fake"
URI = "crux://reviews/CR-9"


@pytest.fixture(autouse=True)
def _clean_service() -> Iterator[None]:
    rs._UNRETIRED.clear()
    yield
    rs.set_active_service(None)
    rs._UNRETIRED.clear()


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """``mcp_gateway.resource_subscriptions.enabled`` as the routes read it."""
    state = {"on": True}

    class _Section:
        @property
        def enabled(self) -> bool:
            return state["on"]

    cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(resource_subscriptions=_Section()))
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: cfg)
    return state


@pytest.fixture
def recognized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _ok)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *_a: False)


def _request(
    method: str,
    path: str,
    *,
    sk: str = SESSION,
    body: Any = ...,
    state: Any = None,
    internal: bool = True,
) -> web.Request:
    app = web.Application()
    app["state"] = MagicMock() if state is None else state
    headers = {"X-Session-Key": sk} if sk else {}
    req = make_mocked_request(method, path, app=app, headers=headers)
    if internal:
        # What token_auth sets on a constant-time X-Internal-Secret match: how the
        # tools and the CLI arrive. A cookie-granted request arrives without it.
        req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _call(handler: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    resp = await handler(_request(method, path, **kwargs))
    payload = json.loads(resp.text)
    payload["_status"] = resp.status
    return payload


def _subscribe_body(uri: str = URI, instruction: str = "check it") -> dict[str, Any]:
    return {"server": SERVER, "uri": uri, "instruction": instruction}


def _record(session: str, uris: tuple[str, ...], ids: tuple[str, ...]) -> HolderRecord:
    return HolderRecord(
        session_key=session,
        channel_id="",
        server=SERVER,
        register={"server_name": SERVER},
        initialize={},
        uris=list(uris),
        subscriptions={u: SubscriptionMeta(id=i, instruction="x") for u, i in zip(uris, ids)},
    )


async def _offline_service(
    tmp_path: Path, *records: HolderRecord, enabled: bool = True
) -> ResourceSubscriptionService:
    """The real service over *records* with no gateway: nothing connects."""
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    for record in records:
        store.put(record)
    settings = McpResourceSubscriptionsConfig(enabled=enabled)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=rs.RecordingSink(), settings=lambda: settings
    )
    # Started with the switch off, so no stored holder tries to connect; the
    # service's own switch is read per call, so subscribes still see `enabled`.
    started = McpResourceSubscriptionsConfig(enabled=False)
    service._settings = lambda: started
    await service.start()
    service._settings = lambda: settings
    rs.set_active_service(service)
    return service


# -- refusals that need no gateway ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(False, "disabled"), (True, "gateway_unavailable")],
    ids=["disabled", "gateway-unavailable"],
)
async def test_every_unavailable_tool_call_is_refused_and_audited(
    tmp_path: Path,
    switch: dict[str, bool],
    audit: "_AuditLog",
    enabled: bool,
    expected: str,
) -> None:
    switch["on"] = enabled
    for handler, method, path, body, operation in (
        (
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            _subscribe_body(),
            "resource_subscribe",
        ),
        (
            routes.api_resource_subscriptions_list,
            "GET",
            "/api/resource-subscriptions",
            ...,
            "resource_subscriptions",
        ),
        (
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            {"subscription_id": "rsub_x"},
            "resource_unsubscribe",
        ),
    ):
        before = len(audit.events)
        got = await _call(handler, method, path, body=body, state=_gateway_state())
        assert (got["ok"], got["refused"]) == (False, expected), got
        assert audit.events[before:] == [
            (operation, "allowed", "live_slot"),
            (operation, "denied", f"refused={expected}"),
        ]

    if not enabled:
        await _offline_service(tmp_path)
        got = await _call(
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            body=_subscribe_body(),
            state=_gateway_state(),
        )
        assert got["refused"] == "disabled"
        assert audit.events[-1] == (
            "resource_subscribe",
            "denied",
            "refused=disabled",
        )


@pytest.mark.asyncio
async def test_a_session_without_a_dashboard_tab_is_refused(
    tmp_path: Path, switch: dict[str, bool], recognized: None
) -> None:
    await _offline_service(tmp_path)
    got = await _call(
        routes.api_resource_subscribe,
        "POST",
        "/api/resource-subscriptions",
        sk="slack:C123:1700000000.1",
        body=_subscribe_body(),
    )
    assert got["refused"] == "no_dashboard_slot" and "dashboard" in got["reason"]


@pytest.mark.asyncio
async def test_a_uri_over_the_bound_is_refused(
    tmp_path: Path, switch: dict[str, bool], recognized: None
) -> None:
    await _offline_service(tmp_path)
    got = await _call(
        routes.api_resource_subscribe,
        "POST",
        "/api/resource-subscriptions",
        body=_subscribe_body(uri="x://" + "a" * RESOURCE_URI_MAX_LEN),
    )
    assert got["refused"] == "uri_too_long" and str(RESOURCE_URI_MAX_LEN) in got["reason"]
    # The tool advertises the same bound the gateway enforces.
    (subscribe,) = (s for s in tools.schemas() if s["name"] == "resource_subscribe")
    assert subscribe["inputSchema"]["properties"]["uri"]["maxLength"] == RESOURCE_URI_MAX_LEN


@pytest.mark.asyncio
async def test_a_twenty_first_subscription_is_refused(
    tmp_path: Path, switch: dict[str, bool], recognized: None
) -> None:
    uris = tuple(f"x://r{i}" for i in range(rs.MAX_SUBSCRIPTIONS_PER_SESSION))
    ids = tuple(f"rsub_{i:016d}" for i in range(len(uris)))
    await _offline_service(tmp_path, _record(SESSION, uris, ids))
    got = await _call(
        routes.api_resource_subscribe, "POST", "/api/resource-subscriptions", body=_subscribe_body()
    )
    assert got["refused"] == "session_limit" and "20" in got["reason"]


@pytest.mark.asyncio
async def test_an_unverified_caller_is_refused_by_the_tool_and_the_route(
    monkeypatch: pytest.MonkeyPatch, switch: dict[str, bool]
) -> None:
    monkeypatch.setattr(
        tools.mcp_core, "require_strict_session_key", lambda refusal, *_a: ("", refusal)
    )
    posted = MagicMock()
    monkeypatch.setattr(tools.mcp_core, "_post", posted)
    for name, args in (
        ("resource_subscribe", _subscribe_body()),
        ("resource_subscriptions", {}),
        ("resource_unsubscribe", {"subscription_id": "rsub_x"}),
    ):
        assert tools.HANDLERS[name](name, args).startswith("Refused (identity)")
    posted.assert_not_called()

    # A request with no session key never reaches the service.
    resp = await routes.api_resource_subscriptions_list(
        _request("GET", "/api/resource-subscriptions", sk="")
    )
    assert resp.status == 400


@pytest.mark.parametrize(
    "reply, expected",
    [
        (
            {"ok": False, "refused": "pinned_backends", "reason": "would pin 17"},
            "Refused (pinned_backends): would pin 17",
        ),
        (
            {
                "ok": False,
                "refused": "server_refused",
                "reason": "the server refused the subscribe",
                "server_error": {"code": -32002, "message": "nope"},
            },
            'Server error: {"code": -32002, "message": "nope"}',
        ),
        ({"ok": True, "id": "rsub_1", "server_result": {}}, '"subscription_id": "rsub_1"'),
        ({"error": "gateway down"}, "Error: gateway down"),
    ],
    ids=["refused", "server-refused", "subscribed", "error"],
)
def test_the_subscribe_tool_relays_the_routes_answer(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any], expected: str
) -> None:
    monkeypatch.setattr(tools.mcp_core, "require_strict_session_key", lambda *_a: (SESSION, ""))
    sent: list[tuple[str, dict[str, Any], str]] = []

    def _post(path: str, body: dict[str, Any], *, session_key: str, timeout: float) -> Any:
        sent.append((path, body, session_key))
        return reply

    monkeypatch.setattr(tools.mcp_core, "_post", _post)
    assert expected in tools.resource_subscribe("resource_subscribe", _subscribe_body())
    assert sent == [("/api/resource-subscriptions", _subscribe_body(), SESSION)]


@pytest.mark.parametrize(
    "reply, expected",
    [
        (
            {"ok": False, "refused": "disabled", "reason": "the switch is off"},
            "Refused (disabled): the switch is off",
        ),
        ({"ok": True, "subscriptions": []}, "This session holds no resource subscriptions."),
        (
            {"ok": True, "subscriptions": [{"id": "rsub_1", "server": SERVER, "uri": URI}]},
            '"id": "rsub_1"',
        ),
        ({"error": "gateway down"}, "Error: gateway down"),
    ],
    ids=["refused", "empty", "listed", "error"],
)
def test_the_list_tool_relays_the_routes_answer(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any], expected: str
) -> None:
    monkeypatch.setattr(tools.mcp_core, "require_strict_session_key", lambda *_a: (SESSION, ""))
    asked: list[tuple[str, str]] = []

    def _get(path: str, *, session_key: str) -> Any:
        asked.append((path, session_key))
        return reply

    monkeypatch.setattr(tools.mcp_core, "_get", _get)
    assert expected in tools.resource_subscriptions("resource_subscriptions", {})
    assert asked == [("/api/resource-subscriptions", SESSION)]


@pytest.mark.parametrize(
    "reply, expected",
    [
        ({"ok": True, "removed": "rsub_1"}, "Removed rsub_1."),
        (
            {"ok": False, "refused": "not_found", "reason": "no subscription rsub_1"},
            "Refused (not_found): no subscription rsub_1",
        ),
        ({"error": "gateway down"}, "Error: gateway down"),
    ],
    ids=["removed", "not-found", "error"],
)
def test_the_unsubscribe_tool_relays_the_routes_answer(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any], expected: str
) -> None:
    monkeypatch.setattr(tools.mcp_core, "require_strict_session_key", lambda *_a: (SESSION, ""))
    sent: list[tuple[str, dict[str, Any], str]] = []

    def _post(path: str, body: dict[str, Any], *, session_key: str) -> Any:
        sent.append((path, body, session_key))
        return reply

    monkeypatch.setattr(tools.mcp_core, "_post", _post)
    got = tools.resource_unsubscribe("resource_unsubscribe", {"subscription_id": "rsub_1"})
    assert expected in got
    assert sent == [("/api/resource-subscriptions/remove", {"subscription_id": "rsub_1"}, SESSION)]


@pytest.mark.parametrize("args", [{}, {"subscription_id": ""}, {"subscription_id": 7}])
def test_the_unsubscribe_tool_refuses_a_missing_id_before_any_request(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> None:
    posted = MagicMock()
    monkeypatch.setattr(tools.mcp_core, "_post", posted)
    got = tools.resource_unsubscribe("resource_unsubscribe", args)
    assert got == "Error: subscription_id is required"
    posted.assert_not_called()


# -- another session's id ------------------------------------------------------


@pytest.mark.asyncio
async def test_another_sessions_id_is_not_found_and_never_listed(
    tmp_path: Path, switch: dict[str, bool], recognized: None
) -> None:
    service = await _offline_service(
        tmp_path,
        _record(SESSION, (URI,), ("rsub_mine0000000000",)),
        _record(OTHER, ("x://theirs",), ("rsub_theirs0000000",)),
    )
    listed = await _call(
        routes.api_resource_subscriptions_list, "GET", "/api/resource-subscriptions"
    )
    assert [row["id"] for row in listed["subscriptions"]] == ["rsub_mine0000000000"]
    assert "session_key" not in listed["subscriptions"][0]
    assert "theirs" not in json.dumps(listed)

    got = await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        body={"subscription_id": "rsub_theirs0000000"},
    )
    assert got["refused"] == "not_found"
    assert [r.session_key for r in service.records(OTHER)] == [OTHER]

    removed = await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        body={"subscription_id": "rsub_mine0000000000"},
    )
    assert removed == {"ok": True, "removed": "rsub_mine0000000000", "_status": 200}
    assert service.records(SESSION) == []


# -- the audit trail of a refusal ----------------------------------------------


class _AuditLog:
    """Every ``log_api_access`` the routes and session recognition write."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def log_api_access(
        self, *, operation: str, outcome: str, resources: str = "", **_: Any
    ) -> None:
        self.events.append((operation, outcome, resources))


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _AuditLog:
    log = _AuditLog()
    monkeypatch.setattr(routes, "sel", lambda: log)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: log)
    return log


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "method", "path", "body", "code"),
    [
        (routes.api_resource_subscribe, "POST", "/api/resource-subscriptions", [], "invalid_body"),
        (
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            {"server": "", "uri": URI, "instruction": "x"},
            "invalid_server_name",
        ),
        (
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            {"server": SERVER, "uri": "", "instruction": "x"},
            "invalid_uri",
        ),
        (
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            {"server": SERVER, "uri": URI, "instruction": 7},
            "invalid_instruction",
        ),
        (
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            {},
            "invalid_subscription_id",
        ),
    ],
    ids=["body", "server", "uri", "instruction", "subscription-id"],
)
async def test_invalid_requests_after_recognition_are_audited_as_denied(
    audit: _AuditLog,
    handler: Any,
    method: str,
    path: str,
    body: Any,
    code: str,
) -> None:
    got = await _call(handler, method, path, body=body, state=_gateway_state())

    assert (got["_status"], got["code"]) == (400, code)
    operation = (
        "resource_unsubscribe"
        if handler is routes.api_resource_unsubscribe
        else "resource_subscribe"
    )
    assert audit.events == [
        (operation, "allowed", "live_slot"),
        (operation, "denied", f"refused={code}"),
    ]


def _gateway_state(*, slot_restricted: bool = False, restricted_keys: tuple[str, ...] = ()) -> Any:
    """A dashboard state that the real session recognition reads."""
    slots = {} if restricted_keys else {"tool-tab": SimpleNamespace(is_restricted=slot_restricted)}
    return SimpleNamespace(
        _slots=slots,
        _restricted_keys=set(restricted_keys),
        sessions=None,
        subagents=None,
        context_builder=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sk, state, reason",
    [
        ("dashboard:ui", _gateway_state(), "dashboard_ui"),
        (SESSION, _gateway_state(slot_restricted=True), "restricted_session"),
        (SESSION, _gateway_state(restricted_keys=(SESSION,)), "restricted_session"),
    ],
    ids=["dashboard-ui", "incognito-slot", "temporary-key"],
)
async def test_a_refused_caller_is_audited_as_denied_with_its_reason(
    tmp_path: Path, switch: dict[str, bool], audit: _AuditLog, sk: str, state: Any, reason: str
) -> None:
    await _offline_service(tmp_path)
    got = await _call(
        routes.api_resource_subscribe,
        "POST",
        "/api/resource-subscriptions",
        sk=sk,
        body=_subscribe_body(),
        state=state,
    )
    assert (got["ok"], got["refused"]) == (False, "identity"), got
    assert audit.events[-1] == ("resource_subscribe", "denied", reason)


@pytest.mark.asyncio
async def test_a_permitted_call_is_audited_as_allowed_and_never_denied(
    tmp_path: Path, switch: dict[str, bool], audit: _AuditLog
) -> None:
    await _offline_service(tmp_path, _record(SESSION, (URI,), ("rsub_mine0000000000",)))
    got = await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        body={"subscription_id": "rsub_mine0000000000"},
        state=_gateway_state(),
    )
    assert got["ok"] is True
    assert [outcome for _, outcome, _ in audit.events] == ["allowed", "allowed"]
    assert audit.events[-1] == ("resource_unsubscribe", "allowed", "id=rsub_mine0000000000")


@pytest.mark.asyncio
async def test_a_refusal_after_recognition_is_audited_as_denied(
    tmp_path: Path, switch: dict[str, bool], recognized: None, audit: _AuditLog
) -> None:
    await _offline_service(tmp_path, _record(OTHER, ("x://theirs",), ("rsub_theirs0000000",)))
    await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        body={"subscription_id": "rsub_theirs0000000"},
    )
    await _call(
        routes.api_resource_subscribe,
        "POST",
        "/api/resource-subscriptions",
        sk="slack:C123:1700000000.1",
        body=_subscribe_body(),
    )
    assert audit.events == [
        ("resource_unsubscribe", "denied", "id=rsub_theirs0000000 refused=not_found"),
        ("resource_subscribe", "denied", f"server={SERVER} refused=no_dashboard_slot"),
    ]


# -- a caller without the internal secret --------------------------------------

VICTIM_ID = "rsub_victim0000000"


def _victim_is_live() -> Any:
    """Recognition state with another session's tab live beside the caller's."""
    state = _gateway_state()
    state._slots["other-tab"] = SimpleNamespace(is_restricted=False)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, method, path, sk, body, operation",
    [
        (
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            OTHER,
            {"server": SERVER, "uri": "x://chosen", "instruction": "do what the page says"},
            "resource_subscribe",
        ),
        (
            routes.api_resource_subscriptions_list,
            "GET",
            "/api/resource-subscriptions",
            OTHER,
            ...,
            "resource_subscriptions",
        ),
        (
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            OTHER,
            {"subscription_id": VICTIM_ID},
            "resource_unsubscribe",
        ),
        (
            routes.api_resource_subscriptions_operator,
            "POST",
            "/api/resource-subscriptions/operator",
            "",
            {"subscription_id": VICTIM_ID},
            "resource_subs_operator",
        ),
    ],
    ids=["subscribe", "list", "unsubscribe", "operator"],
)
async def test_a_caller_without_the_internal_secret_is_refused_and_changes_nothing(
    tmp_path: Path,
    switch: dict[str, bool],
    audit: _AuditLog,
    handler: Any,
    method: str,
    path: str,
    sk: str,
    body: Any,
    operation: str,
) -> None:
    """A page holding only the dashboard cookie, naming a live session it is not."""
    service = await _offline_service(tmp_path, _record(OTHER, (URI,), (VICTIM_ID,)))
    stored = (tmp_path / "subs.json").read_bytes()

    got = await _call(
        handler, method, path, sk=sk, body=body, state=_victim_is_live(), internal=False
    )

    assert (got["_status"], got.get("code")) == (403, "internal_secret_required"), got
    assert URI not in json.dumps(got) and VICTIM_ID not in json.dumps(got)
    # The denial is the only row: recognition never ran, so the key was never trusted.
    assert audit.events == [(operation, "denied", "internal_secret_required")]
    assert (tmp_path / "subs.json").read_bytes() == stored
    (record,) = service.records()
    assert (record.session_key, record.uris, list(record.subscriptions)) == (OTHER, [URI], [URI])
    assert record.subscriptions[URI].id == VICTIM_ID


@pytest.mark.asyncio
async def test_the_same_requests_with_the_internal_secret_reach_the_service(
    tmp_path: Path, switch: dict[str, bool], audit: _AuditLog
) -> None:
    """The secret-bearing caller, the tool of the session the key names, is served."""
    service = await _offline_service(
        tmp_path,
        _record(OTHER, (URI,), (VICTIM_ID,)),
        _record(SESSION, ("x://mine",), ("rsub_mine0000000000",)),
    )
    state = _victim_is_live()

    listed = await _call(
        routes.api_resource_subscriptions_list,
        "GET",
        "/api/resource-subscriptions",
        sk=OTHER,
        state=state,
    )
    assert [row["id"] for row in listed["subscriptions"]] == [VICTIM_ID]
    # With no gateway no holder can start, so the answer is the service's own.
    subscribed = await _call(
        routes.api_resource_subscribe,
        "POST",
        "/api/resource-subscriptions",
        sk=OTHER,
        body=_subscribe_body(uri="x://another"),
        state=state,
    )
    assert (subscribed["_status"], subscribed["refused"]) == (200, "gateway_unavailable")
    removed = await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        sk=OTHER,
        body={"subscription_id": VICTIM_ID},
        state=state,
    )
    assert removed == {"ok": True, "removed": VICTIM_ID, "_status": 200}
    operator = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        sk="",
        body={"subscription_id": "rsub_mine0000000000"},
    )
    assert (operator["ok"], operator["running"]) == (True, True), operator
    assert service.records() == []
    assert [e for e in audit.events if e[2] == "internal_secret_required"] == []


@pytest.mark.asyncio
async def test_the_cli_exits_non_zero_and_keeps_the_store_when_the_route_answers_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real operator route's refusal, over a socket, to the CLI's own request.

    The test server runs no auth middleware, which leaves the request where a
    cookie-granted one lands: at the handler with ``internal_auth`` unset.
    """
    service = await _offline_service(tmp_path, _record(OTHER, (URI,), (VICTIM_ID,)))
    stored = (tmp_path / "subs.json").read_bytes()
    app = web.Application()
    app.router.add_post(
        "/api/resource-subscriptions/operator", routes.api_resource_subscriptions_operator
    )
    server = TestServer(app)
    monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda *_a: "")
    monkeypatch.setattr(
        "kiro_crew.port_resolution.resolve_client_port_ex", lambda _port: (server.port, None)
    )
    monkeypatch.setattr(
        cli_resource_subs, "_store", lambda: ResourceSubscriptionStore(tmp_path / "subs.json")
    )

    def _remove() -> object:
        try:
            return cli_resource_subs._remove(VICTIM_ID)
        except SystemExit as stopped:
            return stopped.code

    await server.start_server()
    try:
        code = await asyncio.to_thread(_remove)
    finally:
        await server.close()

    assert code == 1
    assert "the gateway answered HTTP 403" in capsys.readouterr().err
    assert (tmp_path / "subs.json").read_bytes() == stored
    assert service.subscription(OTHER, SERVER, URI) is not None


# -- operator surface ----------------------------------------------------------


def _patch_gateway_request(monkeypatch: pytest.MonkeyPatch, result: bytes | BaseException) -> None:
    monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda *_a: "")
    monkeypatch.setattr(
        "kiro_crew.port_resolution.resolve_client_port_ex", lambda _port: (5476, None)
    )
    if isinstance(result, BaseException):
        opener = MagicMock(side_effect=result)
    else:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = result
        opener = MagicMock(return_value=response)
    monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", opener)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True], ids=["session", "operator"])
async def test_a_stale_subscription_id_never_removes_its_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recognized: None,
    audit: _AuditLog,
    operator: bool,
) -> None:
    old_id = "rsub_original000000"
    replacement_id = "rsub_replaced000000"
    kept_uri = "x://kept"
    service = await _offline_service(
        tmp_path,
        _record(
            SESSION,
            (URI, kept_uri),
            (old_id, "rsub_kept000000000"),
        ),
    )
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    real_unsubscribe = service.unsubscribe

    async def replace_then_unsubscribe(
        session_key: str,
        server: str,
        uri: str,
        *,
        expected_subscription_id: Optional[str] = None,
    ) -> bool:
        async with service._write_lock:
            current = service._records[(session_key, server)]
            replacement = HolderRecord.from_json(current.to_json())
            assert replacement is not None
            replacement.subscriptions[uri] = SubscriptionMeta(
                id=replacement_id, instruction="replacement"
            )
            await asyncio.to_thread(store.put, replacement)
            service._records[replacement.key] = replacement
        return await real_unsubscribe(
            session_key,
            server,
            uri,
            expected_subscription_id=expected_subscription_id,
        )

    monkeypatch.setattr(service, "unsubscribe", replace_then_unsubscribe)
    if operator:
        got = await _call(
            routes.api_resource_subscriptions_operator,
            "POST",
            "/api/resource-subscriptions/operator",
            sk="",
            body={"subscription_id": old_id},
        )
        operation = "resource_subs_remove"
        assert got["running"] is True
    else:
        got = await _call(
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            body={"subscription_id": old_id},
        )
        operation = "resource_unsubscribe"

    assert got["refused"] == "not_found", got
    assert audit.events[-1] == (
        operation,
        "denied",
        f"id={old_id} refused=not_found",
    )
    (live,) = service.records(SESSION)
    assert live.uris == [URI, kept_uri]
    assert live.subscriptions[URI].id == replacement_id
    assert store.load() == [live]


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True], ids=["session", "operator"])
async def test_a_removal_that_does_not_commit_is_refused_and_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recognized: None,
    audit: _AuditLog,
    operator: bool,
) -> None:
    subscription_id = "rsub_uncommitted000"
    service = await _offline_service(tmp_path, _record(SESSION, (URI,), (subscription_id,)))
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    real_unsubscribe = service.unsubscribe

    async def stop_then_unsubscribe(
        session_key: str,
        server: str,
        uri: str,
        *,
        expected_subscription_id: Optional[str] = None,
    ) -> bool:
        service._closed = True
        return await real_unsubscribe(
            session_key,
            server,
            uri,
            expected_subscription_id=expected_subscription_id,
        )

    monkeypatch.setattr(service, "unsubscribe", stop_then_unsubscribe)
    if operator:
        got = await _call(
            routes.api_resource_subscriptions_operator,
            "POST",
            "/api/resource-subscriptions/operator",
            sk="",
            body={"subscription_id": subscription_id},
        )
        operation = "resource_subs_remove"
        assert got["running"] is True
    else:
        got = await _call(
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            body={"subscription_id": subscription_id},
        )
        operation = "resource_unsubscribe"

    assert got["refused"] == "gateway_unavailable", got
    assert "try again" in got["reason"]
    assert audit.events[-1] == (
        operation,
        "denied",
        f"id={subscription_id} refused=gateway_unavailable",
    )
    assert store.load()[0].subscriptions[URI].id == subscription_id

    if operator:
        monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: got)
        assert cli_resource_subs._remove(subscription_id) == 1
        cli_error = capsys.readouterr().err
        assert "gateway_unavailable" in cli_error
        assert "try again" in cli_error


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True], ids=["session", "operator"])
async def test_a_removal_the_store_refuses_is_gateway_unavailable_not_a_server_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recognized: None,
    audit: _AuditLog,
    operator: bool,
) -> None:
    subscription_id = "rsub_storefull0000"
    service = await _offline_service(tmp_path, _record(SESSION, (URI,), (subscription_id,)))
    store = ResourceSubscriptionStore(tmp_path / "subs.json")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(service._store, "put", refuse)
    monkeypatch.setattr(service._store, "remove", refuse)

    if operator:
        got = await _call(
            routes.api_resource_subscriptions_operator,
            "POST",
            "/api/resource-subscriptions/operator",
            sk="",
            body={"subscription_id": subscription_id},
        )
        operation = "resource_subs_remove"
        assert got["running"] is True
    else:
        got = await _call(
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            body={"subscription_id": subscription_id},
        )
        operation = "resource_unsubscribe"

    assert got["_status"] == 200, got
    assert got["refused"] == "gateway_unavailable", got
    # A store that could not be written is the removal not committing, not the
    # gateway moving on, and the reason the model and the operator read says so.
    assert got["reason"] == "the removal did not commit; try again"
    assert audit.events[-1] == (
        operation,
        "denied",
        f"id={subscription_id} refused=gateway_unavailable",
    )
    assert service.records(SESSION)[0].subscriptions[URI].id == subscription_id
    assert store.load()[0].subscriptions[URI].id == subscription_id

    if operator:
        monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: got)
        assert cli_resource_subs._remove(subscription_id) == 1
        cli_error = capsys.readouterr().err
        assert "gateway_unavailable" in cli_error
        assert "try again" in cli_error


@pytest.mark.asyncio
async def test_operator_remove_while_service_is_starting_is_retryable_and_never_edits_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _record(OTHER, (URI,), ("rsub_starting00000",))
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(record)
    original_load = store.load
    load_started = threading.Event()
    load_can_finish = threading.Event()

    def held_load() -> list[HolderRecord]:
        snapshot = original_load()
        load_started.set()
        if not load_can_finish.wait(default_timeout()):
            raise AssertionError("service start was never released")
        return snapshot

    monkeypatch.setattr(store, "load", held_load)
    settings = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
    )
    rs.set_active_service(service)
    start = asyncio.create_task(service.start())
    try:
        await async_wait_until(load_started.is_set)
        starting = await _call(
            routes.api_resource_subscriptions_operator,
            "POST",
            "/api/resource-subscriptions/operator",
            sk="",
            body={"subscription_id": "rsub_starting00000"},
        )
        assert starting == {
            "ok": False,
            "refused": "starting",
            "reason": "the gateway subscription service is starting; try again",
            "running": True,
            "_status": 200,
        }
        monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
        monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: starting)
        assert cli_resource_subs._remove("rsub_starting00000") == 1
        assert original_load()[0].subscriptions[URI].id == "rsub_starting00000"
        assert "starting; try again" in capsys.readouterr().err
    finally:
        load_can_finish.set()
        await start

    removed = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        sk="",
        body={"subscription_id": "rsub_starting00000"},
    )
    assert removed["ok"] is True and removed["running"] is True
    assert original_load() == []


@pytest.mark.asyncio
async def test_stopping_service_remains_active_until_its_close_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=ResourceSubscriptionStore(tmp_path / "subs.json"),
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
    )
    await service.start()
    close_started = asyncio.Event()
    close_can_finish = asyncio.Event()

    async def held_close() -> None:
        close_started.set()
        await close_can_finish.wait()

    monkeypatch.setattr(service, "close", held_close)
    rs.set_active_service(service)
    stopping = asyncio.create_task(rs.stop_active_service())
    try:
        await async_wait_until(close_started.is_set)
        assert rs.active_service() is service
    finally:
        close_can_finish.set()
        await stopping
    assert rs.active_service() is None


@pytest.mark.asyncio
async def test_operator_not_found_from_running_service_never_allows_an_offline_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(OTHER, (URI,), ("rsub_kept000000000",)))
    settings = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
    )
    await service.start()
    rs.set_active_service(service)

    missing = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        sk="",
        body={"subscription_id": "rsub_missing0000000"},
    )
    assert missing["refused"] == "not_found" and missing["running"] is True
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: missing)
    assert cli_resource_subs._remove("rsub_missing0000000") == 1
    assert store.load()[0].subscriptions[URI].id == "rsub_kept000000000"
    assert "no subscription rsub_missing0000000" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure, message",
    [
        (TimeoutError("read timed out"), "did not answer within 30 seconds"),
        (
            urllib.error.HTTPError("http://127.0.0.1:5476", 503, "Service Unavailable", {}, None),
            "answered HTTP 503",
        ),
        (b"not-json", "could not read the gateway reply"),
    ],
    ids=["timeout", "http-5xx", "unreadable"],
)
def test_gateway_failure_never_allows_an_offline_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: bytes | BaseException,
    message: str,
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(OTHER, (URI,), ("rsub_transport0000",)))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    _patch_gateway_request(monkeypatch, failure)

    with pytest.raises(SystemExit) as stopped:
        cli_resource_subs._remove("rsub_transport0000")
    assert stopped.value.code == 1
    assert store.load()[0].subscriptions[URI].id == "rsub_transport0000"
    assert message in capsys.readouterr().err


def test_connection_refused_still_allows_the_offline_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(OTHER, (URI,), ("rsub_offline000000",)))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    _patch_gateway_request(
        monkeypatch,
        urllib.error.URLError(ConnectionRefusedError(111, "connection refused")),
    )

    assert cli_resource_subs._remove("rsub_offline000000") == 0
    assert store.load() == []
    assert "no subscription service running" in capsys.readouterr().out


def _operator_server(monkeypatch: pytest.MonkeyPatch) -> TestServer:
    """The real operator route on a loopback port the CLI's own request resolves to."""

    @web.middleware
    async def internal(request: web.Request, handler: Any) -> web.StreamResponse:
        # What token_auth sets on a constant-time X-Internal-Secret match.
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[internal])
    app.router.add_post(
        "/api/resource-subscriptions/operator", routes.api_resource_subscriptions_operator
    )
    server = TestServer(app)
    monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda *_a: "")
    monkeypatch.setattr(
        "kiro_crew.port_resolution.resolve_client_port_ex", lambda _port: (server.port, None)
    )
    return server


@pytest.mark.asyncio
async def test_a_service_started_during_an_offline_edit_gives_the_subscription_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: _AuditLog
) -> None:
    """An offline edit is not left undone by a service that loaded meanwhile.

    The first ask finds no service, so the CLI edits the store. A service is
    built over the same store and loads the subscription before that edit lands,
    which is the ordering that leaves it holding and serving a subscription the
    file does not list, and writing it back on its next put.
    """
    subscription_id = "rsub_raced00000000"
    record = _record(OTHER, (URI,), (subscription_id,))
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(record)
    server = _operator_server(monkeypatch)
    edit_reached = threading.Event()
    service_started = threading.Event()

    def store_for_the_edit() -> ResourceSubscriptionStore:
        edit_reached.set()
        if not service_started.wait(default_timeout()):
            raise AssertionError("the racing service was never started")
        return store

    monkeypatch.setattr(cli_resource_subs, "_store", store_for_the_edit)
    await server.start_server()
    removal = asyncio.ensure_future(asyncio.to_thread(cli_resource_subs._remove, subscription_id))
    try:
        await async_wait_until(edit_reached.is_set)
        service = await _offline_service(tmp_path, record)
        held = service.find_subscription(OTHER, subscription_id)
        service_started.set()
        code = await finished(removal, "the CLI's removal")
    finally:
        service_started.set()
        if not removal.done():
            await asyncio.wait([removal])
        await server.close()

    assert held is not None
    assert code == 0
    assert service.find_subscription(OTHER, subscription_id) is None
    assert store.load() == []


_CONFIRM_ID = "rsub_confirmed000"
_LATER = {"running": True, "refused": routes.NOT_FOUND, "reason": "none held"}
_STARTS = {"running": True, "refused": routes.OPERATOR_STARTING, "reason": "wait"}
_STUCK = {"running": True, "refused": "gateway_unavailable", "reason": "no commit"}


@pytest.mark.parametrize(
    ("second", "code", "asks", "needle"),
    [
        (_LATER, 0, 2, f"Removed {_CONFIRM_ID}."),
        (_STARTS, 1, cli_resource_subs._CONFIRM_ASKS + 1, "still starting"),
        ("the gateway answered HTTP 503", 1, 2, "the gateway could not be asked"),
        (_STUCK, 1, 2, "the gateway answered no commit"),
        ({"ok": False}, 1, 2, "the gateway answered no removal"),
    ],
    ids=["not-found", "starting", "failed", "refused", "unreadable"],
)
def test_the_ask_confirming_an_offline_edit_decides_the_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    second: Any,
    code: int,
    asks: int,
    needle: str,
) -> None:
    """The edit stands only once the gateway says no running service holds it.

    ``not_found`` is a service that loaded after the edit, so it never held it;
    ``starting`` is one that may be loading it, and is asked again to the bound.
    """
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(OTHER, (URI,), (_CONFIRM_ID,)))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    asked: list[dict[str, Any]] = []
    pauses: list[float] = []

    def ask(body: dict[str, Any]) -> Optional[dict[str, Any]]:
        asked.append(body)
        if len(asked) == 1:
            return None
        if isinstance(second, str):
            cli_resource_subs._gateway_error(second)
            return None
        return second

    monkeypatch.setattr(cli_resource_subs, "_ask_gateway", ask)
    monkeypatch.setattr(cli_resource_subs, "_pause", pauses.append)

    assert cli_resource_subs._remove(_CONFIRM_ID) == code
    assert len(asked) == asks
    assert pauses == [cli_resource_subs._CONFIRM_PAUSE_SECS] * (asks - 2)
    printed = capsys.readouterr()
    assert needle in (printed.out if code == 0 else printed.err)
    if code:
        assert f"{_CONFIRM_ID} is removed from the store" in printed.err
        assert "run this command again" in printed.err
    assert store.load() == []


@pytest.mark.asyncio
async def test_the_operator_route_refuses_a_session(tmp_path: Path) -> None:
    await _offline_service(tmp_path, _record(OTHER, (URI,), ("rsub_op000000000000",)))
    got = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        body={"subscription_id": "rsub_op000000000000"},
    )
    assert got["_status"] == 403 and got["code"] == "operator_only"
    removed = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        sk="",
        body={"subscription_id": "rsub_op000000000000"},
    )
    assert removed["ok"] is True and removed["running"] is True


def test_the_cli_lists_and_removes_through_the_store_with_no_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(OTHER, (URI, "x://second"), ("rsub_cli0000000000a", "rsub_cli0000000000b")))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: None)

    cli_resource_subs.resource_subs(SimpleNamespace(resource_subs_action="list"))
    out = capsys.readouterr().out
    assert "rsub_cli0000000000a" in out and OTHER in out and URI in out

    with pytest.raises(SystemExit) as done:
        cli_resource_subs.resource_subs(
            SimpleNamespace(resource_subs_action="remove", subscription_id="rsub_cli0000000000a")
        )
    assert done.value.code == 0
    (left,) = store.load()
    assert left.uris == ["x://second"] and list(left.subscriptions) == ["x://second"]

    with pytest.raises(SystemExit) as missing:
        cli_resource_subs.resource_subs(
            SimpleNamespace(resource_subs_action="remove", subscription_id="rsub_nope")
        )
    assert missing.value.code == 1


def test_the_cli_falls_back_to_the_services_own_store_in_its_hidden_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    path = cli_resource_subs._store().path
    assert path == rs.default_store().path
    assert path.parent.name == STORE_DIR_LEAF and path.parent.parent == tmp_path


#: An ESC/CSI line erase and cursor-up, an OSC title write, a carriage return, a
#: newline and a bell: each one rewrites or fakes what the operator's terminal shows.
_HOSTILE = "\x1b[2K\x1b[1A\x1b]0;pwned\x07\rfake\nrow\x07"
_CONTROL_BYTES = ("\x1b", "\x07", "\r", "\x9b")


def _assert_terminal_safe(text: str) -> None:
    assert not [b for b in _CONTROL_BYTES if b in text], repr(text)
    assert "pwned" not in text  # the OSC payload goes with its introducer


def test_the_cli_list_prints_no_control_sequence_from_any_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile_uri = f"x://u{_HOSTILE}end"
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(
        HolderRecord(
            session_key=f"dashboard:s{_HOSTILE}",
            channel_id="",
            server=f"srv{_HOSTILE}",
            register={"server_name": SERVER},
            initialize={},
            uris=[],
            paused_uris={hostile_uri: f"why{_HOSTILE}because"},
            subscriptions={
                hostile_uri: SubscriptionMeta(
                    id=f"rsub_h{_HOSTILE}", instruction=f"before{_HOSTILE}after"
                )
            },
        )
    )
    store.put(_record(OTHER, (URI,), ("rsub_plain00000000a",)))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)

    cli_resource_subs.resource_subs(SimpleNamespace(resource_subs_action="list"))
    out = capsys.readouterr().out

    _assert_terminal_safe(out)
    lines = out.splitlines()
    # Three lines per subscription: a newline in a value cannot add a row.
    assert len(lines) == 6
    rows = [line for line in lines if line.startswith("  rsub_")]
    assert len(rows) == 2 and any("rsub_plain00000000a" in r for r in rows)
    (hostile_row,) = (r for r in rows if r.startswith("  rsub_h"))
    for visible in ("dashboard:s", "srv", "paused (why", "because)", "fake\\x0arow"):
        assert visible in hostile_row
    assert "uri: x://ufake\\x0arowend" in out
    assert "instruction: beforefake\\x0arowafter" in out


def test_the_cli_remove_prints_no_control_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    hostile_id = f"rsub_{_HOSTILE}"
    store.put(_record(OTHER, (URI,), (hostile_id,)))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    replies: list[Optional[dict[str, Any]]] = [
        {"running": True, "ok": False, "reason": f"refused{_HOSTILE}"},
        {"running": True, "ok": True},
        # The offline edit and the ask that confirms no service holds it, then
        # the ask for an id the store does not hold.
        None,
        None,
        None,
    ]
    monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: replies.pop(0))

    for expected_code, subscription_id in ((1, hostile_id), (0, hostile_id), (0, hostile_id)):
        with pytest.raises(SystemExit) as done:
            cli_resource_subs.resource_subs(
                SimpleNamespace(resource_subs_action="remove", subscription_id=subscription_id)
            )
        assert done.value.code == expected_code
    with pytest.raises(SystemExit) as missing:
        cli_resource_subs.resource_subs(
            SimpleNamespace(resource_subs_action="remove", subscription_id=hostile_id)
        )
    assert missing.value.code == 1

    got = capsys.readouterr()
    _assert_terminal_safe(got.out + got.err)
    assert got.err.splitlines() == [
        "Error: refusedfake\\x0arow",
        "Error: no subscription rsub_fake\\x0arow",
    ]
    assert got.out.splitlines() == [
        "Removed rsub_fake\\x0arow.",
        "Removed rsub_fake\\x0arow from the store (no subscription service running).",
    ]


# -- the boot path -------------------------------------------------------------


def test_registering_the_routes_imports_nothing_of_the_feature(tmp_path: Path) -> None:
    """Owner requirement 3: route registration with the switch off loads no feature code.

    In a fresh interpreter, because this test module itself imports the handlers,
    and a package attribute left by that import would hide a re-import.
    """
    feature = [
        "kiro_crew.mcp_gateway.resource_subscriptions",
        "kiro_crew.dashboard.handlers.resource_subscriptions",
        "kiro_crew.dashboard.resource_wakes",
    ]
    program = (
        "import json, sys\n"
        "from aiohttp import web\n"
        "from kiro_crew.dashboard import server\n"
        "server._register_mcp_routes(web.Application())\n"
        f"print(json.dumps([n for n in {feature!r} if n in sys.modules]))\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "KIROCREW_HOME": str(home), "HOME": str(home)}
    done = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        env=env,
        # Half the test's own timeout: the start measured 1.6 s on an idle host.
        timeout=default_timeout(),
        check=False,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []


# -- with a real gateway -------------------------------------------------------


@asynccontextmanager
async def _live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sock_dir: Path,
    *,
    extra_env: Optional[dict[str, str]] = None,
    cap: int = 16,
) -> AsyncIterator[dict[str, Any]]:
    """A gatewayd, the service on it, and a function that attaches a session's stub."""
    work, home = tmp_path / "ws", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    sock = sock_dir / "gw.sock"
    logs = [tmp_path / name for name in ("launches.txt", "subscribes.txt", "callers.txt")]
    settings = McpResourceSubscriptionsConfig(enabled=True, max_pinned_backends=cap)
    service = ResourceSubscriptionService(
        store=ResourceSubscriptionStore(tmp_path / "subs.json"),
        socket_path=lambda: sock,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
        ping_interval_secs=0.2,
        reconnect_delay_secs=0.05,
    )
    rs.set_active_service(service)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            running_daemon(monkeypatch, sock, work, *logs, extra_env=extra_env)
        )
        await service.start()
        stack.push_async_callback(service.close)

        async def attach(session_key: str = SESSION) -> None:
            token = mint_stub_session_token()
            await send_claim(str(sock), os.getpid(), session_key, CHANNEL, token)
            stub = StubProcess(await spawn_stub(sock, work, home, session_token=token))
            stack.push_async_callback(stub.close)
            assert "result" in await stub.request(1, "initialize", INIT_PARAMS)

        yield {"service": service, "attach": attach, "subscribes": logs[1]}


_gateway = pytest.mark.skipif(
    pc.IS_WINDOWS, reason="claims resolve the peer's ancestry via SO_PEERCRED"
)


@_gateway
@pytest.mark.asyncio
@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.usefixtures("switch", "recognized")
async def test_a_session_subscribes_lists_and_removes_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_sock_dir: Path
) -> None:
    async with _live(monkeypatch, tmp_path, short_sock_dir) as live:
        await live["attach"]()
        first = await _call(
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            body=_subscribe_body(instruction="first"),
        )
        assert first["ok"] is True and first["id"].startswith("rsub_"), first
        again = await _call(
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            body=_subscribe_body(instruction="second"),
        )
        assert again["id"] == first["id"]

        listed = await _call(
            routes.api_resource_subscriptions_list, "GET", "/api/resource-subscriptions"
        )
        assert listed["subscriptions"] == [
            {
                "id": first["id"],
                "server": SERVER,
                "uri": URI,
                "instruction": "second",
                "state": "active",
                "reason": "",
                "wakes_today": 0,
            }
        ]
        removed = await _call(
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            body={"subscription_id": first["id"]},
        )
        assert removed["ok"] is True
        listed = await _call(
            routes.api_resource_subscriptions_list, "GET", "/api/resource-subscriptions"
        )
        assert listed["subscriptions"] == []


@_gateway
@pytest.mark.asyncio
@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.usefixtures("switch", "recognized")
@pytest.mark.parametrize(
    "attach, uri, extra_env, cap, code, words",
    [
        (False, URI, None, 16, "server_not_attached", SERVER),
        (True, "refused://nothing-here", None, 16, "server_refused", "refused"),
        (
            True,
            URI,
            {"FAKE_POOL_NO_SUBSCRIBE_CAPABILITY": "1"},
            16,
            "not_subscribable",
            "resources.subscribe",
        ),
        (True, URI, None, 0, "pinned_backends", "max_pinned_backends"),
    ],
    ids=["server_not_attached", "server_refused", "not_subscribable", "pinned_backends"],
)
async def test_each_gateway_refusal_is_returned_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_sock_dir: Path,
    attach: bool,
    uri: str,
    extra_env: Optional[dict[str, str]],
    cap: int,
    code: str,
    words: str,
) -> None:
    async with _live(monkeypatch, tmp_path, short_sock_dir, extra_env=extra_env, cap=cap) as live:
        if attach:
            await live["attach"]()
        got = await _call(
            routes.api_resource_subscribe,
            "POST",
            "/api/resource-subscriptions",
            body=_subscribe_body(uri=uri),
        )
        assert got["ok"] is False and got["refused"] == code, got
        assert words in got["reason"]
        if code == "server_refused":
            # The refusal is the server's own: the subscribe reached it as this session.
            assert f"subscribe {uri} {SESSION}" in recorded(live["subscribes"])
            assert got["server_error"]["message"] == "this server refuses that resource"
        assert live["service"].records(SESSION) == []


# -- answer budgets -------------------------------------------------------------

_HANG = 30.0  # only turns a hang into a named failure; the budget decides


class _NeverReadyHolder:
    """A holder this subscribe started that never connects, and honours its bound."""

    def __init__(self) -> None:
        self.uris: list[str] = []
        self.paused_reason = ""
        self.paused_code = ""
        self.closed = False
        self.waiting = asyncio.Event()

    async def wait_ready(self, timeout: float) -> None:
        self.waiting.set()
        await asyncio.sleep(timeout)
        raise HolderPaused(f"the holder did not connect within {timeout:g}s")

    async def subscribe(self, uri: str) -> dict[str, Any]:
        raise AssertionError("a holder that never connected cannot subscribe")

    async def close(self) -> None:
        self.closed = True


class _StallingHolder:
    """A live holder whose post-commit round trip never answers on its own."""

    def __init__(self, uris: tuple[str, ...]) -> None:
        self._uris = list(uris)
        self.paused_reason = ""
        self.paused_code = ""
        self.ended_session = False
        self.closed = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    async def unsubscribe(self, uri: str) -> None:
        # What the real holder does first: it forgets the URI, so a reconnect
        # re-subscribes without it whatever happens to this round trip.
        if uri in self._uris:
            self._uris.remove(uri)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            # The real holder ends its session here, as it does for one that
            # timed out on the wire, so what the server still holds goes too.
            self.ended_session = True
            raise

    async def close(self) -> None:
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.closed = True


def _budgeted_service(
    tmp_path: Path,
    *records: HolderRecord,
    subscribe_budget: float = 1.5,
    commit_reserve: float = 0.1,
    unsubscribe_budget: float = 0.3,
) -> ResourceSubscriptionService:
    """The real service over *records* with budgets small enough to observe."""
    store = ResourceSubscriptionStore(tmp_path / "budget-subs.json")
    for record in records:
        store.put(record)
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: tmp_path / "gateway.sock",
        sink=rs.RecordingSink(),
        settings=lambda: settings,
        subscribe_budget_secs=subscribe_budget,
        subscribe_commit_reserve_secs=commit_reserve,
        unsubscribe_budget_secs=unsubscribe_budget,
    )
    for record in records:
        service._records[record.key] = record
    service._started = True
    rs.set_active_service(service)
    return service


def _describes(monkeypatch: pytest.MonkeyPatch, *, answers: bool) -> None:
    """``describe-session`` that answers at once, or never answers at all."""

    async def _answer(_path: Any, session_key: str, server: str) -> dict[str, Any]:
        if not answers:
            await asyncio.sleep(_HANG)
        return {
            "type": "session-described",
            "channel_id": CHANNEL,
            "register": {"server_name": server, "session_key": session_key},
            "initialize": {"method": "initialize"},
        }

    monkeypatch.setattr(rs, "describe_session", _answer)


def _never_ready(
    monkeypatch: pytest.MonkeyPatch, service: ResourceSubscriptionService
) -> _NeverReadyHolder:
    holder = _NeverReadyHolder()

    def _install(record: HolderRecord, **_kwargs: Any) -> Any:
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_start_holder", _install)
    return holder


@pytest.mark.asyncio
async def test_a_removal_held_off_by_a_subscribe_is_refused_and_keeps_the_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: dict[str, bool], recognized: None
) -> None:
    """The reviewer's trigger: the removal answers inside its budget, and commits later."""
    kept = _record(OTHER, (URI,), ("rsub_kept",))
    service = _budgeted_service(tmp_path, kept)
    _describes(monkeypatch, answers=True)
    holder = _never_ready(monkeypatch, service)

    blocked = asyncio.ensure_future(service.subscribe(SESSION, SERVER, URI))
    try:
        await async_wait_until(holder.waiting.is_set, timeout=_HANG, describe=lambda: "lock held")
        got = await finished(
            _call(
                routes.api_resource_unsubscribe,
                "POST",
                "/api/resource-subscriptions/remove",
                sk=OTHER,
                body={"subscription_id": "rsub_kept"},
                state=_gateway_state(),
            ),
            "the held-off removal answered",
            timeout=_HANG,
        )
    finally:
        with pytest.raises(rs.SubscribeRefused):
            await asyncio.wait_for(blocked, timeout=_HANG)

    assert (got["ok"], got["refused"]) == (False, "gateway_unavailable"), got
    assert "still in progress" in got["reason"]
    # Nothing moved: the subscription the caller was told about is still its own.
    assert [r.uris for r in service.records(OTHER)] == [[URI]]
    assert [r.uris for r in service._store.load() if r.session_key == OTHER] == [[URI]]

    retried = await finished(
        _call(
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            sk=OTHER,
            body={"subscription_id": "rsub_kept"},
            state=_gateway_state(),
        ),
        "the retried removal answered",
        timeout=_HANG,
    )
    assert retried["ok"] is True and retried["removed"] == "rsub_kept"
    assert service.records(OTHER) == []


@pytest.mark.asyncio
async def test_a_subscribe_held_off_by_another_subscribe_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: dict[str, bool], recognized: None
) -> None:
    """The held-off subscribe answers inside its budget: no record, no holder, no event."""
    service = _budgeted_service(tmp_path)
    _describes(monkeypatch, answers=True)
    holder = _never_ready(monkeypatch, service)
    sink = service.sink
    assert isinstance(sink, rs.RecordingSink)

    blocked = asyncio.ensure_future(service.subscribe(SESSION, SERVER, URI))
    try:
        await async_wait_until(holder.waiting.is_set, timeout=_HANG, describe=lambda: "lock held")
        got = await asyncio.wait_for(
            _call(
                routes.api_resource_subscribe,
                "POST",
                "/api/resource-subscriptions",
                sk=OTHER,
                body=_subscribe_body(),
                state=_gateway_state(),
            ),
            timeout=_HANG,
        )
    finally:
        with pytest.raises(rs.SubscribeRefused):
            await asyncio.wait_for(blocked, timeout=_HANG)

    assert (got["ok"], got["refused"]) == (False, "gateway_unavailable"), got
    # Whichever bound it hit, the lock wait or what the lock wait left for the
    # handshake, it was refused rather than queued past its caller's timeout.
    assert any(
        words in got["reason"]
        for words in ("still in progress", "reached its bound", "did not connect")
    ), got
    assert service.records(OTHER) == [] and service._store.load() == []
    assert (OTHER, SERVER) not in service._holders
    assert [event.session_key for event in sink.events] == []


@pytest.mark.asyncio
async def test_a_subscribe_that_cannot_take_the_lock_in_time_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: dict[str, bool], recognized: None
) -> None:
    """The lock wait alone bounds a subscribe, before it describes or starts a holder."""
    service = _budgeted_service(tmp_path, subscribe_budget=0.3)
    _describes(monkeypatch, answers=True)
    holder = _never_ready(monkeypatch, service)
    sink = service.sink
    assert isinstance(sink, rs.RecordingSink)

    # The wait is held here rather than by another subscribe, so only the lock
    # bound can end it: no other call's own refusal can stand in for it.
    await service._lock.acquire()
    try:
        got = await asyncio.wait_for(
            _call(
                routes.api_resource_subscribe,
                "POST",
                "/api/resource-subscriptions",
                sk=OTHER,
                body=_subscribe_body(),
                state=_gateway_state(),
            ),
            timeout=_HANG,
        )
    finally:
        service._lock.release()

    assert (got["ok"], got["refused"]) == (False, "gateway_unavailable"), got
    assert "still in progress" in got["reason"]
    # Refused before anything moved: it never described and never started a holder.
    assert not holder.waiting.is_set() and not holder.closed
    assert service.records(OTHER) == [] and service._store.load() == []
    assert (OTHER, SERVER) not in service._holders
    assert [event.session_key for event in sink.events] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stalls", ["describe", "handshake"])
async def test_an_uncontended_subscribe_past_its_budget_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stalls: str
) -> None:
    service = _budgeted_service(tmp_path, subscribe_budget=0.5)
    _describes(monkeypatch, answers=stalls != "describe")
    holder = _never_ready(monkeypatch, service)

    with pytest.raises(rs.SubscribeRefused) as refused:
        await asyncio.wait_for(service.subscribe(SESSION, SERVER, URI), timeout=_HANG)

    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE
    if stalls == "describe":
        assert "did not answer in time" in str(refused.value)
        assert not holder.closed  # no holder was started at all
    else:
        assert "the holder did not connect" in str(refused.value) or "reached its bound" in str(
            refused.value
        )
        assert holder.closed
    assert service.records(SESSION) == [] and service._store.load() == []
    assert (SESSION, SERVER) not in service._holders


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [(URI, "crux://reviews/CR-8"), (URI,)])
async def test_a_removal_whose_holder_stalls_still_reports_the_removal(
    tmp_path: Path, remaining: tuple[str, ...]
) -> None:
    ids = tuple(f"rsub_{n}" for n in range(len(remaining)))
    record = _record(SESSION, remaining, ids)
    service = _budgeted_service(tmp_path, record, unsubscribe_budget=0.3)
    holder = _StallingHolder(remaining)
    service._holders[record.key] = holder  # type: ignore[assignment]

    try:
        # A mutation finishes before it delivers a cancellation, so the ceiling
        # here must report the overrun rather than wait for a cancel to land.
        removed = await finished(
            service.unsubscribe(SESSION, SERVER, URI, expected_subscription_id=ids[0]),
            "the removal answered",
            timeout=_HANG,
        )

        assert removed is True
        assert holder.entered.is_set()
        assert [r.uris for r in service.records(SESSION)] == (
            [] if len(remaining) == 1 else [list(remaining[1:])]
        )
        assert [r.uris for r in service._store.load()] == (
            [] if len(remaining) == 1 else [list(remaining[1:])]
        )
        if len(remaining) == 1:
            # Nothing live was left for it to hold, so it was dropped, not asked.
            assert service._holders.get(record.key) is None
            holder.release.set()
            await async_wait_until(lambda: holder.closed, timeout=_HANG)
            await async_wait_until(lambda: not rs._LATE_HOLDER_CLOSES, timeout=_HANG)
        else:
            assert holder.uris == list(remaining[1:])
            assert holder.ended_session
            assert not rs._LATE_HOLDER_CLOSES
    finally:
        # A stall that outlasts the ceiling above must not leave a shielded close
        # waiting in the process-wide set the other branch asserts on.
        holder.release.set()


def _declared_default(module: Any, function: str, parameter: str) -> float:
    """The default *parameter* of *function*, as its own module declares it.

    Read from the source: the test floor replaces ``mcp_core._post`` with a
    recorder whose signature says nothing about the real caller timeout.
    """
    for node in ast.parse(inspect.getsource(module)).body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for name, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if name.arg == parameter and default is not None:
                    return float(ast.literal_eval(default))
    raise AssertionError(f"{module.__name__}.{function} declares no {parameter} default")


def test_every_answer_budget_stays_under_its_callers_timeout() -> None:
    """Each budget plus its margin is below the timeout the caller gives up at."""
    post_default = _declared_default(mcp_core, "_post", "timeout")
    get_default = _declared_default(mcp_core, "_get", "timeout")

    assert rs.SUBSCRIBE_BUDGET_SECS + rs.BUDGET_MARGIN_SECS < tools._SUBSCRIBE_TIMEOUT_S
    assert rs.SUBSCRIBE_COMMIT_RESERVE_SECS < rs.SUBSCRIBE_BUDGET_SECS
    # A removal answers two callers: the tool's POST and the operator CLI.
    removal = rs.UNSUBSCRIBE_BUDGET_SECS + rs.HOLDER_CALL_FLOOR_SECS + rs.BUDGET_MARGIN_SECS
    assert removal < post_default
    assert removal < cli_resource_subs._TIMEOUT_SECS
    # The asks that confirm an offline edit stay inside one ask's own bound.
    confirming = cli_resource_subs._CONFIRM_ASKS * cli_resource_subs._CONFIRM_PAUSE_SECS
    assert confirming < cli_resource_subs._TIMEOUT_SECS
    # The list route holds no lock and awaits nothing on the service, which is
    # why its 10 s caller needs no budget of its own.
    assert get_default > 0
    assert "await service." not in inspect.getsource(routes.api_resource_subscriptions_list)

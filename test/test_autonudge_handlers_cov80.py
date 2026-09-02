"""Coverage tests for the auto-nudge HTTP mapping (``dashboard/handlers/autonudge.py``).

``test_autonudge.py`` drives ``AutoNudgeService`` itself and ``test_autonudge_stop_auth.py``
drives the transport-agnostic authorizer. What neither touches is the thin HTTP layer
between them: the read routes (list / get), the "service is absent" 503+``enabled: false``
shapes, the malformed-body 400s, and the DELETE route's audit record — which has to name
the removed loop's ``slot_key`` even though the loop is gone by the time it logs.

Everything is driven through aiohttp's ``make_mocked_request`` (no socket bound) against a
fake service, so no timer task is armed and no loop store is written. ``sel()`` is replaced
with a mock so the audit call can be asserted on rather than appended to a real event log.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h
from kiro_crew.monitoring.models import MonitorOutcome, MonitorState


class _FakeSvc:
    """Just the four methods the HTTP layer calls on the service."""

    def __init__(self, loops: list[NudgeLoop] | None = None) -> None:
        self.loops = loops or []
        self.removed: list[str] = []

    def list_all(self) -> list[NudgeLoop]:
        return list(self.loops)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return next((lp for lp in self.loops if lp.slot_key == slot_key), None)

    def get_by_id(self, loop_id: str) -> NudgeLoop | None:
        return next((lp for lp in self.loops if lp.id == loop_id), None)

    async def remove(self, loop_id: str) -> None:
        self.removed.append(loop_id)


def _loop(loop_id: str = "lp-1", slot_key: str = "chat-1-111") -> NudgeLoop:
    return NudgeLoop(id=loop_id, slot_key=slot_key, message="keep checking", idle_secs=300)


def _monitor_loop(loop_id: str = "mon-1", slot_key: str = "chat-1-111") -> NudgeLoop:
    loop = _loop(loop_id, slot_key)
    loop.monitor = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        created_ts=1.0,
    )
    return loop


@pytest.fixture(autouse=True)
def sel_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the audit sink so nothing is written to a real event log."""
    sink = MagicMock()
    monkeypatch.setattr(h, "sel", lambda: sink)
    return sink


def _svc(monkeypatch: pytest.MonkeyPatch, svc: Any) -> Any:
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
    return svc


def _mk(
    method: str,
    path: str,
    *,
    match: dict[str, str] | None = None,
    body: Any = ...,
    state: Any = None,
    headers: dict[str, str] | None = None,
    user: str | None = "local-app",
    app_claim: str | None = "",
    internal_auth: bool = False,
) -> web.Request:
    app = web.Application()
    request_state = state if state is not None else MagicMock()
    if state is None:
        request_state.owner_id = ""
    app["state"] = request_state
    req = make_mocked_request(
        method,
        path,
        app=app,
        match_info=match or {},
        headers=headers,
    )
    if user is not None:
        req["user"] = user
    if app_claim is not None:
        req["app"] = app_claim
    if internal_auth:
        req["internal_auth"] = True
    if body is not ...:
        if body is None:
            req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        else:
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


# --- /api/monitors ----------------------------------------------------------


@pytest.mark.asyncio
async def test_session_monitor_read_requires_and_uses_authenticated_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop(slot_key="chat-1-111")
    assert loop.monitor is not None
    loop.monitor.wake_count = 3
    _svc(monkeypatch, _FakeSvc([loop]))

    cookie_only = await h.api_session_monitor_get(
        _mk(
            "GET",
            "/api/autonudge/session-monitor",
            headers={"X-Session-Key": "dashboard:chat-1-111"},
        )
    )
    assert cookie_only.status == 403
    missing = await h.api_session_monitor_get(
        _mk("GET", "/api/autonudge/session-monitor", internal_auth=True)
    )
    assert missing.status == 401
    request = _mk(
        "GET",
        "/api/autonudge/session-monitor",
        headers={"X-Session-Key": "dashboard:chat-1-111"},
        internal_auth=True,
    )
    payload = _body(await h.api_session_monitor_get(request))

    assert payload["monitor_id"] == loop.id
    assert payload["monitor"]["target"] == "https://github.com/acme/widgets/pull/7"
    assert payload["monitor"]["wake_count"] == 3


@pytest.mark.asyncio
async def test_session_monitor_read_redacts_provider_controlled_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    loop = _monitor_loop(slot_key="chat-1-111")
    assert loop.monitor is not None
    loop.monitor.last_observation = {
        "blocking_review": "none",
        "checks": {"failed": [f"deploy?token={secret}"]},
        "draft": False,
        "head_revision": "abc123",
        "kind": "github_pull_request",
        "mergeability": "mergeable",
        "review_decision": "approved",
        "review_threads_complete": True,
        "state": "open",
        "target": f"github.com/acme/widgets#7?token={secret}",
        "unresolved_review_threads": 0,
    }
    loop.monitor.last_provider_error = f"provider rejected token {secret}"
    _svc(monkeypatch, _FakeSvc([loop]))

    request = _mk(
        "GET",
        "/api/autonudge/session-monitor",
        headers={"X-Session-Key": "dashboard:chat-1-111"},
        internal_auth=True,
    )
    payload = _body(await h.api_session_monitor_get(request))

    rendered = json.dumps(payload)
    assert secret not in rendered
    assert "provider rejected token" in payload["monitor"]["last_provider_error"]


@pytest.mark.asyncio
async def test_session_monitor_read_audits_missing_binding_denial(
    sel_mock: MagicMock,
) -> None:
    response = await h.api_session_monitor_get(
        _mk("GET", "/api/autonudge/session-monitor", internal_auth=True)
    )

    assert response.status == 401
    denied = [
        call.kwargs
        for call in sel_mock.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
    ]
    assert denied == [
        {
            "caller": "local-app",
            "operation": "session_monitor_get",
            "outcome": "denied",
            "source": "dashboard",
            "resources": "/api/autonudge/session-monitor",
            "error": "authenticated session binding required",
        }
    ]


@pytest.mark.asyncio
async def test_session_monitor_read_rejects_legacy_only_webex_binding(
    sel_mock: MagicMock,
) -> None:
    session_key = "webex:kirocrew:direct:operator@example.com"
    response = await h.api_session_monitor_get(
        _mk(
            "GET",
            "/api/autonudge/session-monitor",
            headers={"X-Session-Key": session_key},
            internal_auth=True,
        )
    )

    assert response.status == 401
    assert _body(response)["code"] == "session_required"
    assert any(
        call.kwargs.get("operation") == "session_monitor_get"
        and call.kwargs.get("outcome") == "denied"
        for call in sel_mock.log_api_access.call_args_list
    )


def test_session_monitor_read_is_strict_internal() -> None:
    from kiro_crew.dashboard.server import (
        _MIXED_INTERNAL_API_PATHS,
        _STRICT_INTERNAL_API_PATHS,
    )

    path = "/api/autonudge/session-monitor"
    assert path in _STRICT_INTERNAL_API_PATHS
    assert path not in _MIXED_INTERNAL_API_PATHS


@pytest.mark.asyncio
async def test_monitor_browser_routes_require_dashboard_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop()
    assert loop.monitor is not None
    loop.monitor.outcome = MonitorOutcome.USER_STOP
    _svc(monkeypatch, _FakeSvc([loop]))
    monkeypatch.setattr(
        h,
        "authorize_and_add_nudge",
        AsyncMock(return_value=(loop, None, 200)),
    )
    monkeypatch.setattr(
        h,
        "authorize_and_update_monitor",
        AsyncMock(return_value=(loop, None, 200)),
    )
    monkeypatch.setattr(
        h,
        "authorize_and_stop_monitor",
        AsyncMock(return_value=(loop, None, 200)),
    )
    state = MagicMock(owner_id="U_OWNER")
    requests = [
        (h.api_monitors_list, _mk("GET", "/api/monitors", state=state)),
        (
            h.api_monitor_slot_get,
            _mk(
                "GET",
                "/api/monitors/slot/chat-1-111",
                match={"slot_key": "chat-1-111"},
                state=state,
            ),
        ),
        (
            h.api_monitor_create,
            _mk(
                "POST",
                "/api/monitors",
                body={
                    "slot_key": "chat-1-111",
                    "target": "https://github.com/acme/widgets/pull/7",
                },
                state=state,
            ),
        ),
        (
            h.api_monitor_update,
            _mk(
                "PATCH",
                "/api/monitors/mon-1",
                match={"monitor_id": "mon-1"},
                body={"wake_instructions": "Inspect the review."},
                state=state,
            ),
        ),
        (
            h.api_monitor_stop,
            _mk(
                "POST",
                "/api/monitors/mon-1/stop",
                match={"monitor_id": "mon-1"},
                body={},
                state=state,
            ),
        ),
        (
            h.api_monitor_restart,
            _mk(
                "POST",
                "/api/monitors/mon-1/restart",
                match={"monitor_id": "mon-1"},
                state=state,
            ),
        ),
    ]
    for handler, request in requests:
        request["user"] = "U_OTHER"
        response = await handler(request)
        assert response.status == 403, handler.__name__
        assert _body(response)["code"] == "dashboard_owner_required"


@pytest.mark.asyncio
async def test_monitor_list_excludes_persistence_only_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    loop = _monitor_loop()
    assert loop.monitor is not None
    loop.monitor.extra_fields["raw_provider_payload"] = "must-not-escape"
    loop.monitor._raw_payload = {"raw_provider_payload": "must-not-escape"}
    loop.monitor.last_observation = {
        "blocking_review": "none",
        "checks": {"failed": [], "passed": [], "pending": [], "unknown": []},
        "draft": False,
        "head_revision": "abc123",
        "kind": "github_pull_request",
        "mergeability": "mergeable",
        "review_decision": "approved",
        "review_threads_complete": True,
        "state": "open",
        "target": f"github.com/acme/widgets#7?token={secret}",
        "unresolved_review_threads": 0,
    }
    _svc(monkeypatch, _FakeSvc([loop]))

    payload = _body(await h.api_monitors_list(_mk("GET", "/api/monitors")))

    assert "must-not-escape" not in json.dumps(payload)
    assert secret not in json.dumps(payload)
    assert (
        "github.com/acme/widgets" in payload["monitors"][0]["monitor"]["last_observation"]["target"]
    )


@pytest.mark.asyncio
async def test_monitor_create_uses_bounded_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock(return_value=(_monitor_loop("new-mon"), None, 200))
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)
    request = _mk(
        "POST",
        "/api/monitors",
        body={
            "slot_key": "chat-1-111",
            "target": "https://github.com/acme/widgets/pull/7",
        },
    )
    response = await h.api_monitor_create(request)
    assert response.status == 200
    kwargs = authorize.await_args.kwargs
    assert kwargs["svc"] is svc
    assert kwargs["monitor"].budgets.max_runtime_secs == 14_400
    assert kwargs["monitor"].budgets.max_agent_turns == 8
    assert kwargs["monitor"].budgets.max_tokens == 250_000
    assert kwargs["monitor"].budgets.max_provider_errors == 3
    assert kwargs["replace_existing"] is False


@pytest.mark.asyncio
async def test_monitor_create_rejects_unlimited_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk(
        "POST",
        "/api/monitors",
        body={
            "slot_key": "chat-1-111",
            "target": "https://github.com/acme/widgets/pull/7",
            "max_runtime_secs": 0,
        },
    )
    response = await h.api_monitor_create(request)
    assert response.status == 400
    assert _body(response)["code"] == "invalid_monitor"


@pytest.mark.asyncio
async def test_monitor_create_rejects_webex_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock()
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)
    request = _mk(
        "POST",
        "/api/monitors",
        body={
            "slot_key": "webex:kirocrew:direct:operator@example.com",
            "target": "https://github.com/acme/widgets/pull/7",
        },
    )

    response = await h.api_monitor_create(request)

    assert response.status == 400
    assert _body(response)["code"] == "monitor_session_unsupported"
    authorize.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_stop_retains_authoritative_record(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _monitor_loop()
    _svc(monkeypatch, _FakeSvc([loop]))
    stop = AsyncMock(return_value=(loop, None, 200))
    monkeypatch.setattr(h, "authorize_and_stop_monitor", stop)
    request = _mk("POST", "/api/monitors/mon-1/stop", match={"monitor_id": "mon-1"}, body={})
    payload = _body(await h.api_monitor_stop(request))
    assert payload["monitor"]["id"] == "mon-1"
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_restart_rejects_a_future_version_without_rewriting_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    future_monitor = {
        "version": 99,
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
        "future_policy": {"wake_every_time": True},
    }
    store = {
        "version": 1,
        "loops": [
            {
                "id": "future01",
                "slot_key": "chat-1-123",
                "message": "future instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": future_monitor,
            }
        ],
    }
    store_path = tmp_path / "autonudge.json"
    store_path.write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)
    service._load()
    before = store_path.read_bytes()
    authorize = AsyncMock(return_value=(service._loops["future01"], None, 200))
    monkeypatch.setattr(h, "_autonudge_get", lambda: service)
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)
    request = _mk(
        "POST",
        "/api/monitors/future01/restart",
        match={"monitor_id": "future01"},
    )

    response = await h.api_monitor_restart(request)

    assert response.status == 409
    assert _body(response)["code"] == "unsupported_monitor_version"
    authorize.assert_not_awaited()
    assert store_path.read_bytes() == before
    assert service._serialize_state()["loops"][0]["monitor"] == future_monitor


@pytest.mark.asyncio
async def test_monitor_restart_is_conditional_on_the_record_it_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop()
    assert loop.monitor is not None
    loop.active = False
    loop.monitor.outcome = MonitorOutcome.USER_STOP
    loop.monitor.config_generation = 7
    _svc(monkeypatch, _FakeSvc([loop]))
    authorize = AsyncMock(return_value=(loop, None, 200))
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)
    request = _mk(
        "POST",
        "/api/monitors/mon-1/restart",
        match={"monitor_id": "mon-1"},
    )

    response = await h.api_monitor_restart(request)

    assert response.status == 200
    assert authorize.await_args.kwargs["expected_existing_monitor_id"] == "mon-1"
    assert authorize.await_args.kwargs["expected_existing_config_generation"] == 7


@pytest.mark.asyncio
async def test_monitor_update_sends_only_explicit_patch_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop()
    _svc(monkeypatch, _FakeSvc([loop]))
    update = AsyncMock(return_value=(loop, None, 200))
    monkeypatch.setattr(h, "authorize_and_update_monitor", update)
    request = _mk(
        "PATCH",
        "/api/monitors/mon-1",
        match={"monitor_id": "mon-1"},
        body={"wake_instructions": "Check the failing jobs."},
    )

    response = await h.api_monitor_update(request)

    assert response.status == 200
    assert update.await_args.kwargs["patch"] == {"wake_instructions": "Check the failing jobs."}


@pytest.mark.asyncio
async def test_monitor_update_sends_only_explicit_budget_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop()
    _svc(monkeypatch, _FakeSvc([loop]))
    update = AsyncMock(return_value=(loop, None, 200))
    monkeypatch.setattr(h, "authorize_and_update_monitor", update)
    request = _mk(
        "PATCH",
        "/api/monitors/mon-1",
        match={"monitor_id": "mon-1"},
        body={"max_tokens": 75_000},
    )

    response = await h.api_monitor_update(request)

    assert response.status == 200
    assert update.await_args.kwargs["patch"] == {"budget_patch": {"max_tokens": 75_000}}


@pytest.mark.asyncio
async def test_legacy_patch_rejects_a_structured_monitor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop()
    _svc(monkeypatch, _FakeSvc([loop]))
    legacy_update = AsyncMock()
    monkeypatch.setattr(h, "authorize_and_update_nudge", legacy_update)
    request = _mk(
        "PATCH",
        "/api/autonudge/mon-1",
        match={"loop_id": "mon-1"},
        body={"message": "legacy overwrite", "active": False},
    )

    response = await h.api_autonudge_update(request)

    assert response.status == 409
    assert _body(response)["code"] == "structured_monitor_requires_monitor_api"
    legacy_update.assert_not_awaited()


# --- GET /api/autonudge ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_disabled_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    response = await h.api_autonudge_list(_mk("GET", "/api/autonudge"))
    assert _body(response) == {"enabled": False, "loops": []}


@pytest.mark.asyncio
async def test_legacy_list_excludes_structured_monitors(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = _loop("lp-1")
    structured = _monitor_loop("mon-1", "chat-2-222")
    assert structured.monitor is not None
    structured.monitor.extra_fields["secret"] = "must-not-escape"
    structured.monitor._raw_payload = {"secret": "must-not-escape"}
    _svc(monkeypatch, _FakeSvc([legacy, structured]))
    payload = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    assert payload["enabled"] is True
    assert [lp["id"] for lp in payload["loops"]] == ["lp-1"]
    # The legacy dataclass fields still round-trip as JSON, without the new marker.
    assert payload["loops"][0]["idle_secs"] == 300
    assert payload["loops"][0]["slot_key"] == "chat-1-111"
    assert "monitor" not in payload["loops"][0]
    assert "must-not-escape" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_prompt_gated_loop_remains_on_legacy_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gated = _monitor_loop("gate-1", "chat-2-222")
    gated.gate = True
    _svc(monkeypatch, _FakeSvc([gated]))

    legacy = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    structured = _body(await h.api_monitors_list(_mk("GET", "/api/monitors")))

    assert [loop["id"] for loop in legacy["loops"]] == ["gate-1"]
    assert structured["monitors"] == []


# --- GET /api/autonudge/{slot_key} -------------------------------------------


@pytest.mark.asyncio
async def test_get_reports_disabled_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    request = _mk("GET", "/api/autonudge/chat-1-111", match={"slot_key": "chat-1-111"})
    assert _body(await h.api_autonudge_get(request)) == {"enabled": False, "loop": None}


@pytest.mark.asyncio
async def test_get_returns_the_loop_bound_to_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc([_loop("lp-9", "chat-7-777")]))
    request = _mk("GET", "/api/autonudge/chat-7-777", match={"slot_key": "chat-7-777"})
    payload = _body(await h.api_autonudge_get(request))
    assert payload["enabled"] is True
    assert payload["loop"]["id"] == "lp-9"
    assert "monitor" not in payload["loop"]


@pytest.mark.asyncio
async def test_legacy_get_hides_a_structured_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop("mon-9", "chat-7-777")
    assert loop.monitor is not None
    loop.monitor.extra_fields["secret"] = "must-not-escape"
    loop.monitor._raw_payload = {"secret": "must-not-escape"}
    _svc(monkeypatch, _FakeSvc([loop]))
    request = _mk("GET", "/api/autonudge/chat-7-777", match={"slot_key": "chat-7-777"})

    payload = _body(await h.api_autonudge_get(request))

    assert payload == {"enabled": True, "loop": None}
    assert "must-not-escape" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_get_returns_null_loop_for_an_unbound_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc([_loop("lp-9", "chat-7-777")]))
    request = _mk("GET", "/api/autonudge/chat-8-888", match={"slot_key": "chat-8-888"})
    assert _body(await h.api_autonudge_get(request)) == {"enabled": True, "loop": None}


# --- POST /api/autonudge -----------------------------------------------------


@pytest.mark.asyncio
async def test_start_503_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    response = await h.api_autonudge_start(_mk("POST", "/api/autonudge", body={}))
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_start_400_on_undecodable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    response = await h.api_autonudge_start(_mk("POST", "/api/autonudge", body=None))
    assert response.status == 400
    assert _body(response) == {"error": "invalid JSON"}


@pytest.mark.asyncio
async def test_start_rejects_a_fractional_idle_secs(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk("POST", "/api/autonudge", body={"slot_key": "s", "idle_secs": 1.5})
    response = await h.api_autonudge_start(request)
    assert response.status == 400
    assert _body(response)["code"] == "not_a_whole_number"


@pytest.mark.asyncio
async def test_start_rejects_a_non_integer_max_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk("POST", "/api/autonudge", body={"slot_key": "s", "max_cycles": "abc"})
    response = await h.api_autonudge_start(request)
    assert response.status == 400
    assert "integers" in _body(response)["error"]


@pytest.mark.asyncio
async def test_start_carries_the_gate_opt_out_to_the_authorizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the route the dashboard arms through, so the escape must exist here.

    An opt-out offered on only one of the arming surfaces is not an opt-out.
    """
    _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock(return_value=(_loop("lp-new"), None, 200))
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)

    await h.api_autonudge_start(
        _mk("POST", "/api/autonudge", body={"slot_key": "s", "message": "m", "gate": False})
    )
    assert authorize.await_args.kwargs["gate"] is False

    authorize.reset_mock()
    await h.api_autonudge_start(
        _mk("POST", "/api/autonudge", body={"slot_key": "s", "message": "m"})
    )
    assert authorize.await_args.kwargs["gate"] is False, (
        "absent must mean UNGATED on this route: its only caller is the goal popover, "
        "where the work is usually not a pull request even when the instruction names "
        "one -- gating on that mention would deactivate a recurring task when the PR "
        "closed. An earlier version of this test asserted the opposite; only "
        "monitor_start's own directive has the evidence to gate by default."
    )


@pytest.mark.asyncio
async def test_start_refuses_a_non_boolean_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """`"false"` is truthy, so coercing it would gate a loop that asked not to be."""
    _svc(monkeypatch, _FakeSvc())
    request = _mk("POST", "/api/autonudge", body={"slot_key": "s", "gate": "false"})
    response = await h.api_autonudge_start(request)
    assert response.status == 400
    assert _body(response)["code"] == "not_a_boolean"


@pytest.mark.asyncio
async def test_start_passes_create_only_coerced_values_to_the_authorizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session_key wins over slot_key, and the three numbers arrive as ints."""
    svc = _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock(return_value=(_loop("lp-new"), None, 200))
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)
    request = _mk(
        "POST",
        "/api/autonudge",
        body={
            "session_key": "chat-3-333",
            "slot_key": "ignored",
            "message": "poll it",
            "idle_secs": 120.0,
            "max_cycles": 4,
            "max_runtime_secs": 900,
        },
    )
    payload = _body(await h.api_autonudge_start(request))
    assert payload["ok"] is True
    assert payload["loop"]["id"] == "lp-new"
    assert authorize.await_args is not None
    kwargs = authorize.await_args.kwargs
    assert kwargs["svc"] is svc
    assert kwargs["slot_key"] == "chat-3-333"
    assert (kwargs["idle_secs"], kwargs["max_cycles"], kwargs["max_runtime_secs"]) == (120, 4, 900)
    assert kwargs["source"] == "dashboard"
    assert kwargs["replace_existing"] is False


@pytest.mark.asyncio
async def test_start_surfaces_the_authorizer_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    monkeypatch.setattr(
        h, "authorize_and_add_nudge", AsyncMock(return_value=(None, "slot not yours", 403))
    )
    request = _mk("POST", "/api/autonudge", body={"slot_key": "chat-1-111", "message": "go"})
    response = await h.api_autonudge_start(request)
    assert response.status == 403
    assert _body(response) == {
        "error": "slot not yours",
        "code": "autonudge_not_armed",
    }


# --- PATCH /api/autonudge/{loop_id} ------------------------------------------


@pytest.mark.asyncio
async def test_update_503_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    request = _mk("PATCH", "/api/autonudge/lp-1", match={"loop_id": "lp-1"}, body={})
    response = await h.api_autonudge_update(request)
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_update_400_on_undecodable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk("PATCH", "/api/autonudge/lp-1", match={"loop_id": "lp-1"}, body=None)
    response = await h.api_autonudge_update(request)
    assert response.status == 400
    assert _body(response) == {"error": "invalid JSON"}


@pytest.mark.asyncio
async def test_update_forwards_raw_fields_to_the_authorizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP layer coerces nothing here — the authorizer owns that."""
    _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock(return_value=(_loop("lp-1"), None, 200))
    monkeypatch.setattr(h, "authorize_and_update_nudge", authorize)
    request = _mk(
        "PATCH",
        "/api/autonudge/lp-1",
        match={"loop_id": "lp-1"},
        body={"message": "new", "idle_secs": "900", "active": False},
    )
    payload = _body(await h.api_autonudge_update(request))
    assert payload == {"ok": True, "loop": h._serialize(_loop("lp-1"))}
    assert authorize.await_args is not None
    kwargs = authorize.await_args.kwargs
    assert kwargs["loop_id"] == "lp-1"
    assert kwargs["idle_secs"] == "900"
    assert kwargs["active"] is False
    assert kwargs["max_cycles"] is None and kwargs["max_runtime_secs"] is None


@pytest.mark.asyncio
async def test_update_surfaces_the_authorizer_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    monkeypatch.setattr(
        h, "authorize_and_update_nudge", AsyncMock(return_value=(None, "no such loop", 404))
    )
    request = _mk("PATCH", "/api/autonudge/lp-x", match={"loop_id": "lp-x"}, body={"active": True})
    response = await h.api_autonudge_update(request)
    assert response.status == 404
    assert _body(response) == {"error": "no such loop", "code": "autonudge_update_refused"}


@pytest.mark.asyncio
async def test_update_503_refusal_carries_a_retryable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fail-closed policy refusal must be machine-distinguishable from a 400."""
    _svc(monkeypatch, _FakeSvc())
    monkeypatch.setattr(
        h,
        "authorize_and_update_nudge",
        AsyncMock(return_value=(None, "Safety checks are temporarily unavailable", 503)),
    )
    request = _mk("PATCH", "/api/autonudge/lp-x", match={"loop_id": "lp-x"}, body={"active": True})
    response = await h.api_autonudge_update(request)
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_policy_unavailable"


# --- DELETE /api/autonudge/{loop_id} -----------------------------------------


@pytest.mark.asyncio
async def test_delete_503_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    request = _mk("DELETE", "/api/autonudge/lp-1", match={"loop_id": "lp-1"})
    response = await h.api_autonudge_delete(request)
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_delete_removes_and_audits_the_owning_slot(
    monkeypatch: pytest.MonkeyPatch, sel_mock: MagicMock
) -> None:
    """slot_key must be captured BEFORE remove(), or the audit record is anonymous."""
    svc = _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk("DELETE", "/api/autonudge/lp-1", match={"loop_id": "lp-1"})
    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    assert svc.removed == ["lp-1"]
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["session_key"] == "chat-5-555"
    assert kwargs["tool_name"] == "autonudge_delete"
    assert kwargs["outcome"] == "success"
    assert kwargs["metadata"]["loop_id"] == "lp-1"


@pytest.mark.asyncio
async def test_legacy_delete_of_structured_monitor_audits_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop()
    svc = _svc(monkeypatch, _FakeSvc([loop]))
    stop = AsyncMock(return_value=(None, "audit log unavailable — monitor not stopped", 503))
    monkeypatch.setattr(h, "authorize_and_stop_monitor", stop)
    state = MagicMock(owner_id="U_OWNER")
    request = _mk(
        "DELETE",
        "/api/autonudge/mon-1",
        match={"loop_id": "mon-1"},
        state=state,
        user="U_OTHER",
    )

    response = await h.api_autonudge_delete(request)

    assert response.status == 403
    assert _body(response)["code"] == "dashboard_owner_required"
    assert svc.list_all() == [loop]
    assert svc.removed == []
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_of_an_unknown_loop_is_audited_as_a_noop(
    monkeypatch: pytest.MonkeyPatch, sel_mock: MagicMock
) -> None:
    svc = _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk("DELETE", "/api/autonudge/lp-gone", match={"loop_id": "lp-gone"})
    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    assert svc.removed == ["lp-gone"]
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["outcome"] == "noop"
    assert kwargs["session_key"] == ""

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

import dataclasses
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h
from kiro_crew.monitoring.models import MonitorOutcome, MonitorState, monitor_state_public_dict


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
async def test_legacy_list_includes_structured_monitors(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = _loop("lp-1")
    structured = _monitor_loop("mon-1", "chat-2-222")
    assert structured.monitor is not None
    structured.monitor.extra_fields["secret"] = "must-not-escape"
    structured.monitor._raw_payload = {"secret": "must-not-escape"}
    _svc(monkeypatch, _FakeSvc([legacy, structured]))
    payload = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    assert payload["enabled"] is True
    assert [lp["id"] for lp in payload["loops"]] == ["lp-1", "mon-1"]
    # The legacy dataclass fields still round-trip as JSON, without the new marker.
    assert payload["loops"][0]["idle_secs"] == 300
    assert payload["loops"][0]["slot_key"] == "chat-1-111"
    # The structured row says a monitor is armed, on what cadence and in what
    # state, and carries NOTHING describing what it watches.
    assert payload["loops"][1]["active"] is True
    assert payload["loops"][1]["idle_secs"] == 300
    assert "monitor" not in payload["loops"][1]
    assert "message" not in payload["loops"][1]
    assert "must-not-escape" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_legacy_reads_withhold_every_owner_scoped_monitor_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary guard: what is being watched must not reach an un-gated route.

    Asserted structurally against ``monitor_state_public_dict`` rather than as a
    hand-written field list, so ADDING a field to ``MonitorState`` cannot quietly
    widen this route: EVERY public monitor field is owner-scoped here, and this
    route publishes none of them.
    """
    loop = _monitor_loop("mon-1", "chat-2-222")
    assert loop.monitor is not None
    loop.monitor.wake_instructions = "drive PR 7 to green, then stop"
    loop.monitor.last_fingerprint = "sha-abc123"
    loop.monitor.last_observation = {"target": "github.com/acme/widgets#7", "state": "open"}
    loop.monitor.extra_fields["secret"] = "must-not-escape"
    loop.monitor._raw_payload = {"secret": "must-not-escape"}
    _svc(monkeypatch, _FakeSvc([loop]))

    listed = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    per_slot = _body(
        await h.api_autonudge_get(
            _mk(
                "GET",
                "/api/autonudge/slot/chat-2-222",
                match={"slot_key": "chat-2-222"},
            )
        )
    )

    # Two names appear on BOTH dataclasses -- ``created_ts`` and
    # ``stopped_reason`` -- and on the row they carry the LOOP's meaning, so they
    # are a name collision rather than a leak. Everything else the monitor
    # publishes is owner-scoped and must not appear at all.
    loop_fields = {field.name for field in dataclasses.fields(loop)}
    owner_scoped = set(monitor_state_public_dict(loop.monitor)) - loop_fields
    assert "target" in owner_scoped and "wake_instructions" in owner_scoped
    for row in (listed["loops"][0], per_slot["loop"]):
        # The record is absent outright, not reduced under a key of its own.
        assert "monitor" not in row
        assert owner_scoped.isdisjoint(row)
        # ``message`` IS the wake instructions on a structured monitor, so the
        # innocuous-looking legacy field is the one that would leak them.
        assert "message" not in row
        assert "banner" not in row
        assert "stop_sentinel_path" not in row
    # Nothing describing the subject survives anywhere in either response.
    for blob in (json.dumps(listed), json.dumps(per_slot)):
        assert "drive PR 7 to green" not in blob
        assert "sha-abc123" not in blob
        assert "acme/widgets" not in blob
        assert "review_ready" not in blob
        assert "github_pull_request" not in blob
        assert "must-not-escape" not in blob


@pytest.mark.asyncio
async def test_structured_legacy_row_carries_exactly_the_entitled_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the surviving key set, in both directions.

    The projection is built by filtering ``fields(NudgeLoop)``, so a NEW loop
    field would join this route silently. This fails when that happens, which
    forces the entitlement question to be answered once rather than by default.
    """
    loop = _monitor_loop("mon-1", "chat-2-222")
    _svc(monkeypatch, _FakeSvc([loop]))

    row = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))["loops"][0]

    assert set(row) == {
        "id",
        "slot_key",
        "idle_secs",
        "active",
        "created_ts",
        "max_runtime_secs",
        "gate",
        "stopped_reason",
        "approval_stalled",
        "next_due_ts",
        "self_armed",
        # Mapped from the monitor's own accounting, not withheld -- withholding
        # them handed the component a default whose label reads "0 = infinity".
        "max_cycles",
        "cycle_count",
        "last_fire_ts",
    }
    mapped = {name for name, _ in h._MONITOR_MAPPED_LEGACY_FIELDS}
    assert mapped <= set(row)
    # Withheld + published still covers every loop field, and the mapped names
    # are the exact overlap between the two sets.
    assert set(h._MONITOR_WITHHELD_LEGACY_FIELDS) | set(row) == {
        field.name for field in dataclasses.fields(loop)
    }
    assert mapped == set(h._MONITOR_WITHHELD_LEGACY_FIELDS) & set(row)


@pytest.mark.asyncio
async def test_legacy_list_omits_cycle_accounting_only_for_structured_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three MAPPED fields, asserted on both sides.

    A structured monitor never writes these on the loop, but each has a truthful
    equivalent in the monitor's own state, so they carry the real value instead of
    being withheld. Withholding was worse: the component defaults an absent
    ``max_cycles`` to 0 under a label reading "0 = infinity", so the panel claimed
    a budget-bounded monitor runs forever. A plain loop is untouched.
    """
    legacy = _loop("lp-1")
    legacy.cycle_count = 4
    legacy.max_cycles = 24
    legacy.last_fire_ts = 1700.0
    structured = _monitor_loop("mon-1", "chat-2-222")
    assert structured.monitor is not None
    structured.monitor.budgets = dataclasses.replace(structured.monitor.budgets, max_agent_turns=7)
    structured.monitor.agent_turns = 3
    structured.monitor.last_completed_at = 1650.0
    structured.monitor.probe_count = 12
    _svc(monkeypatch, _FakeSvc([legacy, structured]))

    rows = {
        lp["id"]: lp
        for lp in _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))["loops"]
    }

    assert rows["lp-1"]["max_cycles"] == 24
    assert rows["lp-1"]["cycle_count"] == 4
    assert rows["lp-1"]["last_fire_ts"] == 1700.0
    # The monitor carries its OWN values under the same names, because each has a
    # truthful equivalent. Withholding them let the component default max_cycles
    # to 0, whose label reads "0 = infinity" -- a stronger falsehood about a
    # budget-bounded record than a coarse-but-true number.
    assert rows["mon-1"]["max_cycles"] == 7  # budgets.max_agent_turns
    assert rows["mon-1"]["cycle_count"] == 3  # agent_turns spent
    assert rows["mon-1"]["last_fire_ts"] == 1650.0  # last_completed_at
    assert rows["mon-1"]["active"] is True
    assert rows["mon-1"]["idle_secs"] == 300
    assert rows["mon-1"]["next_due_ts"] == 0.0
    assert rows["mon-1"]["stopped_reason"] == ""


@pytest.mark.asyncio
async def test_prompt_gated_loop_remains_on_legacy_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gated = _monitor_loop("gate-1", "chat-2-222")
    gated.gate = True
    gated.max_cycles = 24
    gated.cycle_count = 3
    _svc(monkeypatch, _FakeSvc([gated]))

    legacy = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    structured = _body(await h.api_monitors_list(_mk("GET", "/api/monitors")))

    assert [loop["id"] for loop in legacy["loops"]] == ["gate-1"]
    assert structured["monitors"] == []
    # A gated loop delivers down the legacy path, so its cycle accounting is
    # real and must survive the projection that drops a structured monitor's.
    assert legacy["loops"][0]["max_cycles"] == 24
    assert legacy["loops"][0]["cycle_count"] == 3


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
async def test_legacy_get_returns_a_structured_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _monitor_loop("mon-9", "chat-7-777")
    assert loop.monitor is not None
    loop.monitor.extra_fields["secret"] = "must-not-escape"
    loop.monitor._raw_payload = {"secret": "must-not-escape"}
    _svc(monkeypatch, _FakeSvc([loop]))
    request = _mk("GET", "/api/autonudge/slot/chat-7-777", match={"slot_key": "chat-7-777"})

    payload = _body(await h.api_autonudge_get(request))

    assert payload["enabled"] is True
    assert payload["loop"]["id"] == "mon-9"
    assert payload["loop"]["active"] is True
    assert "monitor" not in payload["loop"]
    assert payload["loop"]["max_cycles"] == loop.monitor.budgets.max_agent_turns
    assert "must-not-escape" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_terminal_monitor_reports_its_outcome_on_the_legacy_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State is the fourth thing the popover needs, and it survives the reduction.

    ``outcome`` is liveness, not subject: it says the watch finished and how, with
    no reference to what was watched. A person deciding whether to intervene needs
    exactly this, and it is the difference between a monitor still working and one
    that stopped without saying so.
    """
    loop = _monitor_loop("mon-3", "chat-3-333")
    assert loop.monitor is not None
    loop.monitor.outcome = MonitorOutcome.BLOCKED
    loop.monitor.probe_count = 31
    loop.active = False
    loop.stopped_reason = "monitor_terminal"
    _svc(monkeypatch, _FakeSvc([loop]))

    row = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))["loops"][0]

    assert row["active"] is False
    assert row["stopped_reason"] == "monitor_terminal"
    # The loop's own state answers "is it still running"; the monitor's terminal
    # OUTCOME is not published here -- it rides the owner-gated route.
    assert "monitor" not in row


@pytest.mark.asyncio
async def test_both_legacy_reads_return_a_monitor_armed_through_the_create_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arm one the way production does, then read it off BOTH legacy routes.

    The loop is built from the kwargs ``api_monitor_create`` actually hands the
    arming chokepoint, not from a fixture, so the values under test are the ones a
    real arm writes -- including the ``message`` that is really the wake
    instructions, and the ``max_cycles=0`` the legacy shape cannot express. Before
    this fix the list dropped the row and the per-slot read answered ``loop:
    None``, so the goal popover reported nothing armed while the monitor probed.
    """
    svc = _svc(monkeypatch, _FakeSvc())

    async def _arm(**kwargs: Any) -> tuple[NudgeLoop, None, int]:
        armed = NudgeLoop(
            id="mon-armed",
            slot_key=kwargs["slot_key"],
            message=kwargs["message"],
            idle_secs=kwargs["idle_secs"],
            max_cycles=kwargs["max_cycles"],
            max_runtime_secs=kwargs["max_runtime_secs"],
            monitor=kwargs["monitor"],
        )
        svc.loops.append(armed)
        return armed, None, 200

    monkeypatch.setattr(h, "authorize_and_add_nudge", _arm)
    created = await h.api_monitor_create(
        _mk(
            "POST",
            "/api/monitors",
            body={
                "slot_key": "chat-4-444",
                "target": "https://github.com/acme/widgets/pull/7",
                "wake_instructions": "fix the red lane",
                "cadence_secs": 900,
            },
        )
    )
    assert created.status == 200
    armed = svc.loops[0]
    assert h.is_structured_monitor_loop(armed)
    assert armed.max_cycles == 0  # what the legacy shape would call "unlimited"
    assert armed.message == "fix the red lane"  # the wake instructions, verbatim

    listed = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    per_slot = _body(
        await h.api_autonudge_get(
            _mk(
                "GET",
                "/api/autonudge/slot/chat-4-444",
                match={"slot_key": "chat-4-444"},
            )
        )
    )

    assert [lp["id"] for lp in listed["loops"]] == ["mon-armed"]
    assert per_slot["loop"]["id"] == "mon-armed"
    for row in (listed["loops"][0], per_slot["loop"]):
        # Existence, cadence, liveness, state -- and nothing about the subject.
        assert row["active"] is True
        assert row["idle_secs"] == 900
        for withheld in ("monitor", "message", "banner", "stop_sentinel_path"):
            assert withheld not in row
        # Mapped, not withheld: the real turn budget rather than a 0 that reads
        # as unlimited.
        assert row["max_cycles"] == 8  # the create route's default budget
    for blob in (json.dumps(listed), json.dumps(per_slot)):
        assert "fix the red lane" not in blob
        assert "acme/widgets" not in blob
    # The owner-gated route is unchanged and still carries the full record.
    owner_view = _body(await h.api_monitors_list(_mk("GET", "/api/monitors")))
    assert owner_view["monitors"][0]["monitor"]["target"] == (
        "https://github.com/acme/widgets/pull/7"
    )
    assert owner_view["monitors"][0]["monitor"]["wake_instructions"] == "fix the red lane"


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
    assert _body(response) == {"error": "no such loop"}


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


# --- #9194: an armed auto-nudge loop must read as armed, distinct from none ---


def _authed_session_monitor_request() -> web.Request:
    return _mk(
        "GET",
        "/api/autonudge/session-monitor",
        headers={"X-Session-Key": "dashboard:chat-1-111"},
        internal_auth=True,
    )


@pytest.mark.asyncio
async def test_session_monitor_read_reports_no_loop_as_not_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with NOTHING armed reads as not armed.

    This is the negative case #9194 turns on: a loop that did not arm must be
    distinguishable from one that did. Here no loop exists at all.
    """
    _svc(monkeypatch, _FakeSvc([]))

    payload = _body(await h.api_session_monitor_get(_authed_session_monitor_request()))

    assert payload["monitor"] is None
    assert payload["autonudge_loop"] is None


@pytest.mark.asyncio
async def test_session_monitor_read_reports_armed_autonudge_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain auto-nudge loop reads as armed via ``autonudge_loop``.

    Previously this collapsed to ``monitor: None`` — identical to the no-loop
    case above — which is the observability gap the issue reports.
    """
    loop = _loop(slot_key="chat-1-111")
    loop.cycle_count = 4
    loop.last_fire_ts = 123.0
    loop.message = "keep driving PR 42"  # agent-controlled: must NOT be echoed
    _svc(monkeypatch, _FakeSvc([loop]))

    payload = _body(await h.api_session_monitor_get(_authed_session_monitor_request()))

    # The structured monitor genuinely does not exist, so that stays None...
    assert payload["monitor"] is None
    # ...but the auto-nudge loop is now readable, and the reading is distinct
    # from the no-loop case (a dict, not None).
    reading = payload["autonudge_loop"]
    assert reading is not None
    assert reading["id"] == loop.id
    assert reading["active"] is True
    assert reading["idle_secs"] == 300
    assert reading["cycle_count"] == 4
    assert reading["last_fire_ts"] == 123.0
    # The free-text instruction is not surfaced by this presence reading.
    assert "message" not in reading
    assert "keep driving PR 42" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_session_monitor_read_structured_monitor_carries_null_autonudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured monitor keeps its authoritative reading; autonudge_loop null."""
    loop = _monitor_loop(slot_key="chat-1-111")
    _svc(monkeypatch, _FakeSvc([loop]))

    payload = _body(await h.api_session_monitor_get(_authed_session_monitor_request()))

    assert payload["monitor_id"] == loop.id
    assert payload["monitor"]["target"] == "https://github.com/acme/widgets/pull/7"
    assert payload["autonudge_loop"] is None

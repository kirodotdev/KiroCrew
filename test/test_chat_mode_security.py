"""Security contract of ``api_chat_mode``.

``api_chat_mode`` (``src/kiro_crew/dashboard/chat_handlers.py``) carried three
defects, all in the ordering between slot validation and global mutation:

1. ``trust_reads`` silently widened to EVERY slot when the named slot did not
   resolve — its ``trust``/``normal`` siblings answer ``400 unknown slot``.
2. A request rejected for an unknown slot had already revoked the
   process-global safety override (``safety_override().deactivate()`` ran
   before the ``400``). A refused request must leave the global grant and
   every slot untouched.
3. ``deactivate()`` — which writes a SEL event — ran inline on the gateway
   loop, unlike the sibling ``activate()`` which is offloaded with
   ``asyncio.to_thread``.

Every test drives the real handler through an aiohttp ``TestClient``; the auth
middleware is stood in by ``_dashboard_owner_request``, and ``safety_override``
is either the real singleton (happy paths) or a recording fake (the rejection
paths, where the contract is "never even called").

An app's sends onto a user slot are tested in ``test_chat_mode_security_send.py``; the
app-token middleware and its app, the fixtures, ``_app_client`` and the app-control target
table live in ``chat_mode_security_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _APP_CONTROL_TARGETS,
    _app_client,
    _hermetic_home,
    _isolate_safety_override,
    _make_app_control_target,
    state_fixture,
)

from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.state import SlotOrigin, row_mid
from kiro_crew.safety_override import safety_override as real_safety_override


@web.middleware
async def _dashboard_owner_request(request: web.Request, handler):
    """Stand in for the auth middleware: a dashboard-owner request.

    ``deny_non_dashboard_caller`` accepts a caller matching the configured
    owner, or a local bootstrap subject when no owner is configured; these
    tests configure no owner, so ``local-app`` passes.
    """
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


def _make_mode_app(state) -> web.Application:
    app = web.Application(middlewares=[_dashboard_owner_request])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


def _client(state) -> TestClient:
    return TestClient(TestServer(_make_mode_app(state)))


class _FakeOverride:
    """Recording stand-in for the SafetyOverride singleton.

    ``active`` starts True when the test wants a live global grant; a grant a
    rejected request must not have touched stays True. ``is_declared`` mirrors
    the real singleton's property — a declared grant is exempt from the
    slot-scoped narrowing, so the fake defaults to the common ad-hoc case.
    """

    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.activate_calls: list[str] = []
        self.deactivate_calls: list[str] = []
        self.is_declared = False

    def activate(self, source: str) -> _FakeOverride:
        self.activate_calls.append(source)
        return self

    def deactivate(self, source: str) -> None:
        self.deactivate_calls.append(source)
        self.active = False

    def is_active(self) -> bool:
        return self.active


@pytest.mark.parametrize(
    ("mode", "expected_trust", "expected_trust_reads"),
    [
        ("normal", False, False),
        ("trust_reads", False, True),
        ("trust", True, False),
    ],
)
@pytest.mark.asyncio
async def test_app_with_grant_can_set_user_slot_mode(
    state,
    mode: str,
    expected_trust: bool,
    expected_trust_reads: bool,
) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    if mode == "normal":
        slot._trust = True
        slot._trust_reads = True
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})
            assert resp.status == 200
    assert slot._trust is expected_trust
    assert slot._trust_reads is expected_trust_reads


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
@pytest.mark.asyncio
async def test_app_non_yolo_mode_target_boundary(state, target: str, allowed: bool) -> None:
    slot = _make_app_control_target(state, target)
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == (200 if allowed else 404)
    assert slot._trust is allowed


@pytest.mark.asyncio
async def test_app_cannot_arm_global_yolo_even_with_grant(state) -> None:
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    override = _FakeOverride()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo", "slot": "s1"})
            body = await resp.json()
    assert resp.status == 403
    assert body["code"] == "app_yolo_forbidden"
    assert override.activate_calls == []


@pytest.mark.asyncio
async def test_app_normal_does_not_revoke_global_yolo(state) -> None:
    # The override is process-global; an app's per-slot ``normal`` must not end
    # the operator's YOLO grant on every other session.
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    override = _FakeOverride()
    override.active = True
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
    assert resp.status == 200
    assert slot._trust is False
    assert override.deactivate_calls == []
    assert override.active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"mode": "trust"}, {"mode": "trust", "slot": ""}])
async def test_app_mode_change_requires_explicit_slot(state, body: dict) -> None:
    # A missing slot and an EMPTY slot both normalize to the all-slots path, so
    # both must be refused for an app caller -- ``""`` once slipped past an
    # ``is None`` check and trusted every session.
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == "slot_required"
    audit.log_api_access.assert_any_call(
        caller="crew-keyboard",
        operation="chat_mode",
        outcome="allowed",
        source="app_isolation",
        resources="permissions.sessionApproval",
    )
    assert state._slots["s1"]._trust is False
    assert state._slots["s2"]._trust is False


@pytest.mark.asyncio
async def test_app_with_grant_can_resolve_user_slot_approval(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "approved", "request_id": "req-1"},
            )
            assert resp.status == 200
    assert future.result() == "approved"
    assert audit.log_api_access.call_args.kwargs["caller"] == "app:crew-keyboard"


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
@pytest.mark.parametrize("action", ["approved", "rejected"])
@pytest.mark.asyncio
async def test_app_approval_target_boundary(state, target: str, allowed: bool, action: str) -> None:
    slot = _make_app_control_target(state, target)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": action, "request_id": "req-1"},
            )
            assert resp.status == (200 if allowed else 404)
    if allowed:
        assert future.result() == action
    else:
        assert future.done() is False


@pytest.mark.parametrize("target", [t for t, _allowed in _APP_CONTROL_TARGETS])
@pytest.mark.parametrize("action", ["approved", "rejected"])
@pytest.mark.asyncio
async def test_app_never_resolves_state_level_approvals(state, target: str, action: str) -> None:
    # State-level approvals are raised by background sources (cron, autonudge,
    # subagent, taskrunner) and only parked in a user's tab. The grant reaches
    # the user's own session -- whose prompts live on the slot future -- so an
    # app token gets 404 here whatever slot the approval is attributed to.
    state.get_or_create_slot("addressed", origin=SlotOrigin.USER)
    _make_app_control_target(state, target)
    future = asyncio.get_running_loop().create_future()
    state._approval_futures["req-state"] = future
    state._pending_approvals["req-state"] = {"id": "req-state", "slot": "s1", "source": "subagent"}
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/addressed/approve",
                json={"action": action, "request_id": "req-state"},
            )
            body = await resp.json()
    assert resp.status == 404
    assert body["code"] == "slot_not_found"
    assert future.done() is False


@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
@pytest.mark.asyncio
async def test_dashboard_still_resolves_state_level_approvals(state, action: str) -> None:
    # The dashboard owner keeps the pre-existing fallback: a parked background
    # approval is theirs to answer from the tab it appears in.
    state.get_or_create_slot("addressed", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    state._approval_futures["req-state"] = future
    state._pending_approvals["req-state"] = {"id": "req-state", "slot": "s1", "source": "cron"}

    @web.middleware
    async def _dashboard_request(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_dashboard_request])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/addressed/approve",
            json={"action": action, "request_id": "req-state"},
        )
    assert resp.status == 200
    assert future.result() is (action == "approved")


@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
@pytest.mark.asyncio
async def test_dashboard_decision_targets_slot_and_request_with_colliding_ids(
    state, action
) -> None:
    other = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    loop = asyncio.get_running_loop()
    other_future = loop.create_future()
    selected_future = loop.create_future()
    sibling_future = loop.create_future()
    other._approval_futures["same-id"] = other_future
    selected._approval_futures.update({"same-id": selected_future, "sibling": sibling_future})
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": action, "request_id": "same-id"},
        )
        assert response.status == 200
        assert selected_future.result() == action
        assert not other_future.done()
        assert not sibling_future.done()
        state.broadcast_ws.assert_any_call(
            "approval_resolved",
            {
                "id": "same-id",
                "approved": action == "approved",
                "slot": "selected",
                "origin": "native",
            },
        )
        # An expired request cannot fall through to the colliding other session.
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": action, "request_id": "same-id"},
        )
        assert response.status == 404
        assert not other_future.done()
        assert not sibling_future.done()


@pytest.mark.parametrize("replacement_slot", [False, True])
@pytest.mark.asyncio
async def test_dashboard_native_stale_row_cannot_resolve_reused_id(state, replacement_slot):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    old_row = selected.append("permission", "Old tool", json.dumps({"request_id": "reused"}))
    old = asyncio.get_running_loop().create_future()
    selected.register_approval("reused", old, old_row)
    old.set_result("rejected")
    if replacement_slot:
        state._slots.pop("selected")
        selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    current_row = selected.append(
        "permission", "Replacement tool", json.dumps({"request_id": "reused"})
    )
    current = asyncio.get_running_loop().create_future()
    selected.register_approval("reused", current, current_row)
    assert row_mid(old_row) != row_mid(current_row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "approved",
                "request_id": "reused",
                "origin": "native",
                "request_mid": row_mid(old_row),
            },
        )
        # The card's request was replaced by a live one under the same id: the
        # refusal is the plain gone answer, and names nothing about the newer one.
        assert await response.json() == {
            "error": "no pending approval",
            "code": "approval_not_pending",
        }
    assert response.status == 404
    assert not current.done()
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", ["approve", "reject", "reject_once"])
@pytest.mark.parametrize(
    "pending", ["live", "missing", "done", "record_missing", "wrong_slot", "wrong_instance"]
)
@pytest.mark.asyncio
async def test_dashboard_strict_coordinator_never_falls_into_native(state, action, pending):
    loop = asyncio.get_running_loop()
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    other = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    native = [loop.create_future(), loop.create_future()]
    selected._approval_futures["same-id"] = native[0]
    other._approval_futures["same-id"] = native[1]
    coordinator = loop.create_future()
    if pending != "missing":
        state._approval_futures["same-id"] = coordinator
    if pending == "done":
        coordinator.set_result(False)
    if pending != "record_missing":
        state._pending_approvals["same-id"] = {
            "id": "same-id",
            "slot": "other" if pending == "wrong_slot" else "dashboard:selected",
            # The record a stale card was rendered from carries another instance:
            # the caller reused the request id, and this is the replacement.
            "instance": "replacement" if pending == "wrong_instance" else "shown",
        }
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    target = {"origin": "coordinator", "slot": "dashboard:selected", "instance": "shown"}
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/approvals/same-id/{action}", params=target, json={})
        assert response.status == (200 if pending == "live" else 404)
        if pending == "live":
            assert coordinator.result() is (action == "approve")
            # A repeated click after resolution is stale even before record cleanup.
            response = await client.post(f"/api/approvals/same-id/{action}", params=target, json={})
            assert response.status == 404
        elif pending != "done":
            assert not coordinator.done()
        assert all(not future.done() for future in native)


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.asyncio
async def test_dashboard_coordinator_target_without_an_instance_is_refused(state, action):
    """A coordinator target names the instance it saw, as a native target names its
    row; one that names none is malformed and mutates nothing, even for a live
    record whose id and slot it does name."""
    coordinator = asyncio.get_running_loop().create_future()
    state._approval_futures["same-id"] = coordinator
    state._pending_approvals["same-id"] = {
        "id": "same-id",
        "slot": "dashboard:selected",
        "instance": "shown",
    }
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/approvals/same-id/{action}",
            params={"origin": "coordinator", "slot": "dashboard:selected"},
            json={},
        )
    assert response.status == 400
    assert not coordinator.done()


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.asyncio
async def test_a_slotless_coordinator_target_settles_only_the_instance_it_names(state, action):
    """An approval with no owning slot (a cron, autonudge or task-runner request)
    decides with an empty slot and its instance. A card left up from an earlier
    request under the recurring id names that request's instance and settles
    nothing; the card for the live request settles it; a target that omits the
    slot key altogether is malformed. Negative control: an empty slot never
    matches a record that has one."""
    coordinator = asyncio.get_running_loop().create_future()
    state._approval_futures["cron-id"] = coordinator
    state._pending_approvals["cron-id"] = {"id": "cron-id", "slot": "", "instance": "live"}
    slotted = asyncio.get_running_loop().create_future()
    state._approval_futures["slotted-id"] = slotted
    state._pending_approvals["slotted-id"] = {
        "id": "slotted-id",
        "slot": "dashboard:selected",
        "instance": "shown",
    }
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        stale = {"origin": "coordinator", "slot": "", "instance": "earlier"}
        response = await client.post(f"/api/approvals/cron-id/{action}", params=stale, json={})
        assert response.status == 404
        assert not coordinator.done()

        no_slot_key = {"origin": "coordinator", "instance": "live"}
        response = await client.post(
            f"/api/approvals/cron-id/{action}", params=no_slot_key, json={}
        )
        assert response.status == 400
        assert not coordinator.done()

        unslotted = {"origin": "coordinator", "slot": "", "instance": "shown"}
        response = await client.post(
            f"/api/approvals/slotted-id/{action}", params=unslotted, json={}
        )
        assert response.status == 404
        assert not slotted.done()

        live = {"origin": "coordinator", "slot": "", "instance": "live"}
        response = await client.post(f"/api/approvals/cron-id/{action}", params=live, json={})
        assert response.status == 200
        assert coordinator.result() is (action == "approve")


@web.middleware
async def _internal_secret_request(request: web.Request, handler):
    """Stand in for the auth middleware: a loopback ``X-Internal-Secret`` caller."""
    request["internal_auth"] = True
    return await handler(request)


@web.middleware
async def _peer_verified_request(request: web.Request, handler):
    """Stand in for the auth middleware: a peer-verified unix-socket caller (an
    MCP or agent subprocess), which carries no ``app`` claim."""
    request["peer_verified"] = True
    return await handler(request)


@web.middleware
async def _unclaimed_request(request: web.Request, handler):
    """Stand in for the auth middleware leaving no caller claim at all."""
    return await handler(request)


@pytest.mark.parametrize(
    "caller",
    [_internal_secret_request, _peer_verified_request, _unclaimed_request],
    ids=["internal_auth", "peer_verified_no_app", "no_claim"],
)
@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.asyncio
async def test_a_slotless_coordinator_target_is_refused_to_an_internal_caller(
    state, action, caller
):
    """Before the empty-slot target, a slotless (cron, autonudge, task-runner)
    approval could be decided only by id, from the owner's browser session. The
    target names the request without widening who may decide it: every caller
    that is not the owner's browser session (an internal-secret caller, a
    peer-verified subprocess with no ``app`` claim, or a request with no claim)
    naming the exact instance is refused and the request stays pending.
    Control: the same caller may still decide a slotted record by its exact
    target, as on main."""
    coordinator = asyncio.get_running_loop().create_future()
    state._approval_futures["cron-id"] = coordinator
    state._pending_approvals["cron-id"] = {"id": "cron-id", "slot": "", "instance": "live"}
    slotted = asyncio.get_running_loop().create_future()
    state._approval_futures["slotted-id"] = slotted
    state._pending_approvals["slotted-id"] = {
        "id": "slotted-id",
        "slot": "dashboard:selected",
        "instance": "shown",
    }
    app = web.Application(middlewares=[caller])
    app["state"] = state
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        live = {"origin": "coordinator", "slot": "", "instance": "live"}
        response = await client.post(f"/api/approvals/cron-id/{action}", params=live, json={})
        assert response.status == 403
        assert (await response.json())["code"] == "approval_owner_required"
        assert not coordinator.done()
        assert "cron-id" in state._pending_approvals

        exact = {"origin": "coordinator", "slot": "dashboard:selected", "instance": "shown"}
        response = await client.post(f"/api/approvals/slotted-id/{action}", params=exact, json={})
        assert response.status == 200
        assert slotted.result() is (action == "approve")


@pytest.mark.parametrize("sent", ["shown", "replacement"])
@pytest.mark.asyncio
async def test_a_slotless_coordinator_target_is_bound_by_its_instance(state, sent):
    """A coordinator approval raised with no owning slot (a cron job's) names an
    empty slot, which is present and must match the record's own empty slot; the
    instance still decides which request under the recurring id is resolved."""
    coordinator = asyncio.get_running_loop().create_future()
    state._approval_futures["cron-id"] = coordinator
    state._pending_approvals["cron-id"] = {"id": "cron-id", "slot": "", "instance": "shown"}
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/approvals/cron-id/approve",
            params={"origin": "coordinator", "slot": "", "instance": sent},
            json={},
        )
    assert response.status == (200 if sent == "shown" else 404)
    assert coordinator.done() is (sent == "shown")


_REGISTRATION_STEPS = 10_000


async def _until_registered(ready, what: str) -> None:
    """Yield to the loop until *ready()* holds, failing by name past a bound.

    The spawned ``request_approval`` publishes its record within a few loop
    steps; one that never does (it raised first) must fail here, at the poll,
    instead of hanging the worker until pytest-timeout kills it.
    """
    for _ in range(_REGISTRATION_STEPS):
        if ready():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{what} was never registered")


@pytest.mark.asyncio
async def test_a_task_gate_is_decided_by_its_own_instance_and_no_other(state):
    """Two concurrent runs can each park a gate on the same task index. Each
    gate's id names its run and task for the project page to find its own gate,
    and a decide binds by the record's instance: run B's instance cannot start
    run A's task, and neither run nor task travels on the decide."""
    loop = asyncio.get_running_loop()
    gate_a = loop.create_task(
        state.request_approval(
            "task-gate-run-a-1-aaaa",
            "taskrunner",
            "Task 1: build",
        )
    )
    gate_b = loop.create_task(
        state.request_approval(
            "task-gate-run-b-1-bbbb",
            "taskrunner",
            "Task 1: build",
        )
    )
    try:
        await _until_registered(
            lambda: {"task-gate-run-a-1-aaaa", "task-gate-run-b-1-bbbb"}
            <= set(state._pending_approvals),
            "both task gates",
        )
        record_a = state._pending_approvals["task-gate-run-a-1-aaaa"]
        record_b = state._pending_approvals["task-gate-run-b-1-bbbb"]
        # The id is the only place the run and task live.
        assert "task_run" not in record_a
        assert "task_index" not in record_a
        app = _make_mode_app(state)
        app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
        async with TestClient(TestServer(app)) as client:
            crossed = await client.post(
                "/api/approvals/task-gate-run-a-1-aaaa/approve",
                params={"origin": "coordinator", "slot": "", "instance": record_b["instance"]},
                json={},
            )
            assert crossed.status == 404
            assert not gate_a.done()
            own = await client.post(
                "/api/approvals/task-gate-run-a-1-aaaa/approve",
                params={"origin": "coordinator", "slot": "", "instance": record_a["instance"]},
                json={},
            )
            assert own.status == 200
        assert await gate_a is True
        assert not gate_b.done()
    finally:
        for gate in (gate_a, gate_b):
            gate.cancel()
        await asyncio.gather(gate_a, gate_b, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_stale_cards_target_cannot_decide_the_request_that_replaced_it(state):
    """The id is the caller's and recurs: request A's card, left up after B took
    the id over, names A's instance. Decided through its owner-bound target it
    resolves nothing, B stays pending, and only B's own target decides B."""
    loop = asyncio.get_running_loop()
    slot = "dashboard:selected"
    request_a = loop.create_task(
        state.request_approval("same-id", "subagent", "fs_write", slot=slot)
    )
    request_b: asyncio.Task[bool] | None = None
    try:
        await _until_registered(lambda: "same-id" in state._pending_approvals, "request A")
        instance_a = state._pending_approvals["same-id"]["instance"]
        request_b = loop.create_task(
            state.request_approval("same-id", "subagent", "fs_write", slot=slot)
        )
        await _until_registered(
            lambda: state._pending_approvals["same-id"]["instance"] != instance_a, "request B"
        )
        instance_b = state._pending_approvals["same-id"]["instance"]

        app = _make_mode_app(state)
        app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)

        async def decide(target_slot: str, instance: str) -> tuple[int, dict]:
            response = await client.post(
                "/api/approvals/same-id/approve",
                params={"origin": "coordinator", "slot": target_slot, "instance": instance},
                json={},
            )
            return response.status, await response.json()

        async with TestClient(TestServer(app)) as client:
            # A's card gets the plain gone answer: it names nothing about the
            # newer request under its id.
            assert await decide(slot, instance_a) == (404, {"error": "not found or expired"})
            assert not request_b.done()
            # The right instance under the wrong owner names no request either,
            # and learns nothing about the owner's live request.
            assert await decide("other", instance_b) == (404, {"error": "not found or expired"})
            assert not request_b.done()
            assert (await decide(slot, instance_b))[0] == 200
        assert await request_b is True
    finally:
        pending = [t for t in (request_a, request_b) if t is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_coordinator_target_never_decides_a_runner_request_under_its_id(state):
    """Only the coordinator record the target names is decided: a chat runner's
    pending request under the same id is left for its own slot route."""
    loop = asyncio.get_running_loop()
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    native = loop.create_future()
    selected._approval_futures["same-id"] = native
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/approvals/same-id/approve",
            params={"origin": "coordinator", "slot": "dashboard:selected", "instance": "any"},
            json={},
        )
        assert response.status == 404
    assert not native.done()


def test_resolution_frames_name_their_registry():
    """Every ``approval_resolved`` frame says which registry resolved it, so a
    client settles only rows from that registry whatever their ids are. The
    caller names the registry: a coordinator record that somehow lacks an
    instance is still reported as a coordinator resolution, never as native."""
    from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator

    frames: list[dict] = []
    fake = MagicMock()
    fake.broadcast_ws = lambda kind, payload: frames.append(payload)
    ApprovalCoordinator.audit_and_broadcast(
        fake,
        "s1",
        "id",
        True,
        "",
        audit_provider=MagicMock(),
        origin="coordinator",
        instance="inst",
    )
    ApprovalCoordinator.audit_and_broadcast(
        fake, "s1", "id", False, "rejected", audit_provider=MagicMock(), origin="native", mid="row"
    )
    ApprovalCoordinator.audit_and_broadcast(
        fake, "s1", "id", False, "", audit_provider=MagicMock(), origin="coordinator"
    )
    assert frames == [
        {"id": "id", "approved": True, "slot": "s1", "origin": "coordinator", "instance": "inst"},
        {"id": "id", "approved": False, "slot": "s1", "origin": "native", "mid": "row"},
        {"id": "id", "approved": False, "slot": "s1", "origin": "coordinator"},
    ]


def test_a_coordinator_resolution_names_the_instance_it_resolved():
    """Both coordinator exits -- a decision and an expiry -- broadcast the
    instance of the record they resolved, so a client can tell it from an
    earlier request's row under the same recurring id."""
    from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator

    class _State:
        _log = MagicMock()

        def __init__(self) -> None:
            self.frames: list[dict] = []
            self._approval_futures: dict = {}
            self._pending_approvals = {
                "id": {"id": "id", "slot": "", "instance": "live"},
                "exp": {"id": "exp", "slot": "cron:job", "instance": "gone"},
            }

        def broadcast_ws(self, kind: str, payload: dict) -> None:
            self.frames.append({"kind": kind, **payload})

        def _audit_and_broadcast_approval(
            self, session_key, approval_id, approved, decision="", *, origin="", instance=""
        ):
            ApprovalCoordinator.audit_and_broadcast(
                self,
                session_key,
                approval_id,
                approved,
                decision,
                audit_provider=MagicMock(),
                origin=origin,
                instance=instance,
            )

    fake = _State()
    loop = asyncio.new_event_loop()
    try:
        fake._approval_futures["id"] = loop.create_future()
        assert ApprovalCoordinator.resolve_state(fake, "id", True)
    finally:
        loop.close()
    ApprovalCoordinator._retire_unresolved(fake, "exp", "cron:job")
    assert fake.frames == [
        {
            "kind": "approval_resolved",
            "id": "id",
            "approved": True,
            "origin": "coordinator",
            "instance": "live",
        },
        {
            "kind": "approval_resolved",
            "id": "exp",
            "approved": False,
            "slot": "cron:job",
            "origin": "coordinator",
            "instance": "gone",
            "decision": "expired",
        },
    ]


def test_coordinator_records_carry_a_distinct_instance_per_request():
    """The request id is the caller's and can recur; the instance is minted here,
    once per request, so two requests sharing an id are told apart."""
    from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator

    class _State:
        _APPROVAL_TIMEOUT = 0.01
        _BACKGROUND_APPROVAL_TIMEOUT_SECS = 0.01

        def __init__(self):
            self._approval_futures = {}
            self._pending_approvals = {}
            self.records = []

        def broadcast_ws(self, kind, payload):
            if kind == "approval":
                self.records.append(dict(payload))

        def push_slots_update(self):
            pass

        def _audit_and_broadcast_approval(self, *args, **kwargs):
            pass

    async def run():
        st = _State()
        for _ in range(2):
            await ApprovalCoordinator.request(
                st,
                "same-id",
                "dashboard",
                "shell",
                tool_input="",
                tool_purpose="",
                slot="dashboard:selected",
                is_background=False,
                redact_url=lambda t: (t, []),
                redact_secret=lambda t: (t, []),
            )
        return st.records

    records = asyncio.run(run())
    assert len(records) == 2 and all(r["id"] == "same-id" for r in records)
    assert all(len(r["instance"]) == 32 for r in records)
    assert records[0]["instance"] != records[1]["instance"]


@pytest.mark.parametrize("proof", [None, "", [], {}, 1, "wrong-row"])
@pytest.mark.asyncio
async def test_dashboard_native_instance_proof_fails_closed(state, proof):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    row = selected.append("permission", "Tool", json.dumps({"request_id": "id"}))
    current = asyncio.get_running_loop().create_future()
    selected.register_approval("id", current, row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "id",
                "request_mid": proof,
                "action": "approved",
            },
        )
    assert response.status == (404 if proof == "wrong-row" else 400)
    assert not current.done()
    assert selected.approval_instance("id") == row_mid(row)
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
@pytest.mark.parametrize("pending", ["live", "missing", "done"])
@pytest.mark.parametrize("coordinator_slot", ["selected", "other"])
@pytest.mark.asyncio
async def test_dashboard_strict_native_never_falls_into_coordinator(
    state, action, pending, coordinator_slot
):
    loop = asyncio.get_running_loop()
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    other = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    native = loop.create_future()
    permission = selected.append(
        "permission", "Current tool", json.dumps({"request_id": "same-id"})
    )
    if pending != "missing":
        selected.register_approval("same-id", native, permission)
    if pending == "done":
        native.set_result("rejected_once")
    other_future = loop.create_future()
    sibling = loop.create_future()
    other._approval_futures["same-id"] = other_future
    selected._approval_futures["sibling"] = sibling
    coordinator = loop.create_future()
    state._approval_futures["same-id"] = coordinator
    state._pending_approvals["same-id"] = {"id": "same-id", "slot": coordinator_slot}
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": action,
                "request_id": "same-id",
                "origin": "native",
                "request_mid": row_mid(permission),
            },
        )
        assert response.status == (200 if pending == "live" else 404)
        if pending == "live":
            assert native.result() == action
        elif pending == "missing":
            assert not native.done()
        assert not coordinator.done()
        assert not other_future.done()
        assert not sibling.done()
        assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", ["trust", "trust_reads"])
@pytest.mark.asyncio
async def test_a_stale_cards_trust_cannot_grant_the_request_that_replaced_it(state, action):
    # Card A's row stays on screen after request B reuses its id. A trust press
    # from A names A's row mid, so it decides nothing and widens no trust; B's
    # own card, naming B's row, gets the grant.
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    row_a = selected.append(
        "permission", "Tool A", json.dumps({"request_id": "same-id", "trust_grantable": "1"})
    )
    row_b = selected.append(
        "permission", "Tool B", json.dumps({"request_id": "same-id", "trust_grantable": "1"})
    )
    future_b = asyncio.get_running_loop().create_future()
    selected.register_approval("same-id", future_b, row_b)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        stale = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "same-id",
                "request_mid": row_mid(row_a),
                "action": action,
            },
        )
        assert stale.status == 404
        assert not future_b.done()
        assert not selected._trust and not selected._trust_reads
        current = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "same-id",
                "request_mid": row_mid(row_b),
                "action": action,
            },
        )
    assert current.status == 200
    assert future_b.done()


@pytest.mark.parametrize("action", ["approve", "reject", "reject_once"])
@pytest.mark.asyncio
async def test_dashboard_legacy_coordinator_endpoint_keeps_native_fallback(state, action):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    selected._approval_futures["legacy"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/approvals/legacy/{action}", json={})
    assert response.status == 200
    assert (
        future.result()
        == {"approve": "approved", "reject": "rejected", "reject_once": "rejected_once"}[action]
    )


@pytest.mark.parametrize(
    "origin,request_id,action",
    [
        ("coordinator", "same-id", "approved"),
        (None, "same-id", "approved"),
        ("native", "", "approved"),
        ("native", [], "approved"),
        ("native", "same-id", "trust"),
        ("native", "same-id", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_dashboard_strict_native_invalid_target_refuses_without_mutation(
    state, origin, request_id, action
):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    selected._approval_futures["same-id"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={"origin": origin, "request_id": request_id, "action": action},
        )
    assert response.status == 400
    assert not future.done()
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", [[], ["approved"], {"a": 1}, 1, None])
@pytest.mark.asyncio
async def test_dashboard_strict_native_non_string_action_is_a_bad_target_not_a_crash(state, action):
    # A well-formed native target with an unhashable or non-string action
    # reached the frozenset membership test and raised TypeError (HTTP 500).
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    selected._approval_futures["same-id"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "same-id",
                "request_mid": "m1",
                "action": action,
            },
        )
        body = await response.json()
    assert response.status == 400
    assert body["code"] == "invalid_approval_target"
    assert not future.done()
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize(
    "origin,slot,action",
    [
        ("native", "selected", "approve"),
        ("", "selected", "approve"),
        ("coordinator", "", "approve"),
        ("coordinator", "selected", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_dashboard_strict_coordinator_invalid_target_refuses_without_mutation(
    state, origin, slot, action
):
    future = asyncio.get_running_loop().create_future()
    state._approval_futures["same-id"] = future
    state._pending_approvals["same-id"] = {"id": "same-id", "slot": "selected"}
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/approvals/same-id/{action}",
            params={"origin": origin, "slot": slot},
            json={},
        )
    assert response.status == 400
    assert not future.done()


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.asyncio
async def test_dashboard_native_origin_does_not_adopt_replacement_slot_future(state, strict):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    replacement = state.get_or_create_slot("replacement", origin=SlotOrigin.USER)
    selected.linked_session_key = replacement.linked_session_key = "slack:thread"
    future = asyncio.get_running_loop().create_future()
    replacement._approval_futures["same-id"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "approved",
                "request_id": "same-id",
                **({"origin": "native", "request_mid": "missing-instance"} if strict else {}),
            },
        )
    assert response.status == (404 if strict else 200)
    assert future.done() is not strict


@pytest.mark.parametrize("proof", ["owner-row", "wrong-row"])
@pytest.mark.asyncio
async def test_strict_native_decide_reaches_a_session_sharing_owner_only_by_its_mid(state, proof):
    """A strict native press on a slot whose future lives on a session-sharing
    owner resolves that future only when the mid names the owner's row; a
    same-id future on a slot with another session identity is never touched."""
    loop = asyncio.get_running_loop()
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    owner = state.get_or_create_slot("owner", origin=SlotOrigin.USER)
    stranger = state.get_or_create_slot("stranger", origin=SlotOrigin.USER)
    selected.linked_session_key = owner.linked_session_key = "slack:thread"
    stranger.linked_session_key = "slack:other"
    owned = loop.create_future()
    owner_row = owner.append("permission", "Owner tool", json.dumps({"request_id": "same-id"}))
    owner.register_approval("same-id", owned, owner_row)
    foreign = loop.create_future()
    stranger_row = stranger.append(
        "permission", "Other tool", json.dumps({"request_id": "same-id"})
    )
    stranger.register_approval("same-id", foreign, stranger_row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "approved",
                "request_id": "same-id",
                "origin": "native",
                "request_mid": row_mid(owner_row if proof == "owner-row" else stranger_row),
            },
        )
    assert response.status == (200 if proof == "owner-row" else 404)
    assert owned.done() is (proof == "owner-row")
    assert not foreign.done()


@pytest.mark.asyncio
async def test_app_yolo_approval_is_refused_and_audited_as_the_app(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    override = _FakeOverride()
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.safety_override",
            return_value=override,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "yolo", "request_id": "req-1"},
            )
            body = await resp.json()
    assert resp.status == 403
    assert body["code"] == "app_yolo_forbidden"
    assert override.activate_calls == []
    assert future.done() is False
    assert audit.log_api_access.call_args.kwargs["caller"] == "crew-keyboard"


@pytest.mark.asyncio
async def test_app_trust_does_not_persist_linked_channel_trust(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._slack_channel = "ch1"
    channel = MagicMock(trusted=False)
    state.channel_manager = MagicMock(_channels={"ch1": channel})
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == 200
    assert slot._trust is True
    assert channel.trusted is False
    channel._save.assert_not_called()


@pytest.mark.asyncio
async def test_app_normal_does_not_clear_linked_channel_trust(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    slot._slack_channel = "ch1"
    channel = MagicMock(trusted=True)
    state.channel_manager = MagicMock(_channels={"ch1": channel})
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert resp.status == 200
    assert slot._trust is False
    assert channel.trusted is True
    channel._save.assert_not_called()


# ── defect 1: trust_reads must not widen on an unknown slot ──


@pytest.mark.asyncio
async def test_trust_reads_unknown_slot_is_400_and_revokes_nothing(state) -> None:
    """A slot-scoped request naming a missing slot widens to nothing.

    The live global grant must survive the refusal: ``deactivate`` is never
    even reached, and no slot's flags change.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust_reads", "slot": "ghost"}
            )
            assert resp.status == 400
            assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []
    assert all(not s._trust_reads and not s._trust for s in state._slots.values())


@pytest.mark.asyncio
async def test_trust_reads_non_string_slot_key_is_rejected(state) -> None:
    """A truthy non-string key is rejected, not routed to the all-slots branch."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": 123})
        assert resp.status == 400
    assert state._slots["s1"]._trust_reads is False


@pytest.mark.asyncio
async def test_falsy_non_string_slot_key_is_rejected_for_trust(state) -> None:
    """Falsy non-strings (``[]``/``{}``/``0``/``False``) must not erase into the all-slots scope.

    ``body.get("slot") or None`` collapses an empty list -- and every other
    falsy non-string -- into ``None``, which is the documented "all slots"
    request; before this fix ``{"mode": "trust", "slot": []}`` trusted EVERY
    slot. The raw value must be refused before that normalization, and the
    live global grant must survive the refusal.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            for bad in ([], {}, 0, False):
                resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": bad})
                assert resp.status == 400, bad
                assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []
    assert all(not s._trust_reads and not s._trust for s in state._slots.values())


@pytest.mark.asyncio
async def test_falsy_non_string_slot_key_is_rejected_for_trust_reads(state) -> None:
    state.get_or_create_slot("s1")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": []})
            assert resp.status == 400
            assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []
    assert state._slots["s1"]._trust_reads is False
    assert state._slots["s1"]._trust is False


# ── defect 2: a rejected request must leave the global grant untouched ──


@pytest.mark.asyncio
async def test_rejected_normal_request_leaves_the_global_grant_active(state) -> None:
    """'{"mode": "normal", "slot": " "}' must not revoke the grant.

    The unknown-slot 400 must be raised BEFORE the revocation, so a refused
    request cannot silently end YOLO mode.
    """
    state.get_or_create_slot("s1")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": " "})
            assert resp.status == 400
            assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []


@pytest.mark.asyncio
async def test_rejected_trust_request_leaves_the_global_grant_active(state) -> None:
    state.get_or_create_slot("s1")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "ghost"})
            assert resp.status == 400
    assert override.active is True
    assert override.deactivate_calls == []


@pytest.mark.asyncio
async def test_rejected_trust_reads_does_not_touch_existing_slots(state) -> None:
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    with patch(
        "kiro_crew.dashboard.chat_handlers.safety_override",
        return_value=_FakeOverride(active=True),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust_reads", "slot": "ghost"}
            )
            assert resp.status == 400
    assert all(not s._trust_reads and not s._trust for s in state._slots.values())


# ── happy paths: the repaired scope semantics hold ──


@pytest.mark.asyncio
async def test_trust_reads_named_slot_only(state) -> None:
    """The named slot is the only one that trusts reads (widening regression)."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})
        assert resp.status == 200
        assert (await resp.json())["mode"] == "trust_reads"
    assert state._slots["s1"]._trust_reads is True
    assert state._slots["s2"]._trust_reads is False


@pytest.mark.asyncio
async def test_trust_named_slot_only(state) -> None:
    """The named slot is the only one trusted (mirrors the trust_reads case).

    Guards the same widening regression on the ``trust`` branch: a slot-scoped
    trust request must not flip its siblings, and the approval policy must be
    set to ``auto`` for the named slot's session only.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
        assert resp.status == 200
        assert (await resp.json())["mode"] == "trust"
    assert state._slots["s1"]._trust is True
    assert state._slots["s2"]._trust is False
    state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "auto")


@pytest.mark.asyncio
async def test_trust_without_a_slot_is_still_global(state) -> None:
    """An absent slot key keeps the documented all-slots meaning for trust."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust"})
        assert resp.status == 200
    assert all(s._trust for s in state._slots.values())


# ── interplay: a slot-scoped trust/trust_reads must not revoke
# ── the process-global YOLO grant (the grant is global, the mode is per-slot)


@pytest.mark.asyncio
async def test_named_slot_trust_reads_leaves_an_active_grant_live(state) -> None:
    """A named-slot trust_reads applies to that slot and does NOT revoke YOLO.

    The trust-grant narrowing and the slot isolation must hold
    together: only the named slot trusts reads, and the operator's live grant
    survives the request.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})
            assert resp.status == 200
    assert override.active is True
    assert override.deactivate_calls == []
    assert state._slots["s1"]._trust_reads is True
    assert state._slots["s2"]._trust_reads is False


@pytest.mark.asyncio
async def test_named_slot_trust_leaves_an_active_grant_live(state) -> None:
    """A named-slot trust applies to that slot and does NOT revoke YOLO."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == 200
    assert override.active is True
    assert override.deactivate_calls == []
    assert state._slots["s1"]._trust is True
    assert state._slots["s2"]._trust is False


@pytest.mark.asyncio
async def test_trust_reads_without_a_slot_is_still_global(state) -> None:
    """An absent slot key keeps its documented all-slots meaning."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust_reads"})
        assert resp.status == 200
    assert all(s._trust_reads for s in state._slots.values())


@pytest.mark.asyncio
async def test_normal_mode_named_slot_only(state) -> None:
    """A named-slot normal request revokes that slot, not its siblings."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    state._slots["s1"]._trust = True
    state._slots["s2"]._trust = True
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
        assert resp.status == 200
    assert state._slots["s1"]._trust is False
    assert state._slots["s2"]._trust is True


# ── defect 3: deactivate runs off the event loop ──


@pytest.mark.asyncio
async def test_deactivate_runs_off_the_event_loop(state) -> None:
    """deactivate() writes a SEL event and must not run on the gateway loop."""
    state.get_or_create_slot("s1")
    captured: dict[str, int] = {}

    class _TrackingOverride:
        is_declared = False

        def deactivate(self, source: str) -> None:
            captured["thread"] = threading.get_ident()

        def is_active(self) -> bool:
            return False

    with patch(
        "kiro_crew.dashboard.chat_handlers.safety_override",
        return_value=_TrackingOverride(),
    ):
        async with _client(state) as client:
            loop_thread = threading.get_ident()
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            assert resp.status == 200
    assert captured["thread"] != loop_thread


@pytest.mark.asyncio
async def test_valid_normal_mode_touches_the_real_singleton(state) -> None:
    """The happy path exercises the real SafetyOverride, not just the fake."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "normal"})
        assert resp.status == 200
    assert real_safety_override().is_active() is False


# ── native resolution frames name the row they settled ──
#
# ``approval_instance`` answers only while the request is pending, so every
# producer must read the row mid BEFORE it resolves the future. A frame that
# loses its mid makes the client fall back to settling every native row under
# the id, which retires the live card of a newer request that reused it.


def _approval_frames(state) -> list[dict]:
    return [
        call.args[1]
        for call in state.broadcast_ws.call_args_list
        if call.args and call.args[0] == "approval_resolved"
    ]


@pytest.mark.asyncio
async def test_the_approve_route_frame_names_the_row_it_settled(state) -> None:
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    row = selected.append("permission", "Tool", json.dumps({"request_id": "id"}))
    fut = asyncio.get_running_loop().create_future()
    selected.register_approval("id", fut, row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "id",
                "request_mid": row_mid(row),
                "action": "approved",
            },
        )
        assert response.status == 200
    assert _approval_frames(state) == [
        {"id": "id", "approved": True, "slot": "selected", "origin": "native", "mid": row_mid(row)}
    ]


@pytest.mark.asyncio
async def test_the_coordinator_slot_resolution_frame_names_the_row_it_settled(state) -> None:
    from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator
    from kiro_crew.dashboard.state import _mark_permission_resolved

    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    row = selected.append("permission", "Tool", json.dumps({"request_id": "id"}))
    fut = asyncio.get_running_loop().create_future()
    selected.register_approval("id", fut, row)
    with patch("kiro_crew.dashboard.state.sel"):
        assert ApprovalCoordinator.resolve_on_slot(
            state,
            selected,
            "id",
            False,
            rejected_once=False,
            permission_marker=_mark_permission_resolved,
            expected_future=fut,
        )
    frames = _approval_frames(state)
    assert len(frames) == 1
    assert frames[0]["origin"] == "native"
    assert frames[0]["mid"] == row_mid(row)


@pytest.mark.asyncio
async def test_the_bulk_trust_sweep_frame_names_the_row_it_settled(state) -> None:
    selected = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    row = selected.append("permission", "Tool", json.dumps({"request_id": "id"}))
    fut = asyncio.get_running_loop().create_future()
    selected.register_approval("id", fut, row)
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
        assert resp.status == 200
    assert fut.result() == "approved"
    assert _approval_frames(state) == [
        {"id": "id", "approved": True, "slot": "s1", "origin": "native", "mid": row_mid(row)}
    ]


@pytest.mark.parametrize("live_first", [True, False])
@pytest.mark.asyncio
async def test_a_strict_decide_marks_only_the_row_it_names(state, live_first) -> None:
    """Two permission rows share a request id (an earlier request's card is
    still in the transcript). A strict native decide marks and persists the
    row it names, never the other one."""
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    meta = json.dumps({"request_id": "same-id"})
    first = selected.append("permission", "Tool A", meta)
    second = selected.append("permission", "Tool B", meta)
    live, other = (first, second) if live_first else (second, first)
    fut = asyncio.get_running_loop().create_future()
    selected.register_approval("same-id", fut, live)
    selected._dirty = False
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "same-id",
                "request_mid": row_mid(live),
                "action": "rejected",
            },
        )
        assert response.status == 200
    assert fut.result() == "rejected"
    assert json.loads(live["cls"])["resolved"] == "rejected"
    assert "resolved" not in json.loads(other["cls"])
    assert selected._dirty is True

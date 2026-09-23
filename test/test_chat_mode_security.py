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
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import (
    _app_may_send_to_slot,
    api_chat_mode,
    api_chat_slot_approve,
)
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.safety_override import (
    reset_singleton,
)
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


@web.middleware
async def _app_request(request: web.Request, handler):
    request["app"] = "crew-keyboard"
    request["user"] = ""
    return await handler(request)


def _make_app_token_mode_app(state) -> web.Application:
    app = web.Application(middlewares=[_app_request])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Redirect KIROCREW_HOME so SEL writes never touch the developer's home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_safety_override():
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _client(state) -> TestClient:
    return TestClient(TestServer(_make_mode_app(state)))


def _app_client(state) -> TestClient:
    return TestClient(TestServer(_make_app_token_mode_app(state)))


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


_APP_CONTROL_TARGETS = (
    ("user", True),
    ("cron", False),
    ("system", False),
    ("member", False),
    ("remote", False),
    ("cron-linked", False),
    ("channel-linked", False),
    ("other-app", False),
    ("own-app", True),
)


def _make_app_control_target(state, target: str):
    if target == "user":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    if target == "cron":
        return state.get_or_create_slot("s1", origin=SlotOrigin.CRON)
    if target == "system":
        return state.get_or_create_slot("s1", origin=SlotOrigin.SYSTEM)
    if target == "member":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER, mode="member")
    if target == "remote":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.executor = "remote"
        slot.instance_id = "peer-1"
        slot.remote_slot = "remote-s1"
        return slot
    if target == "cron-linked":
        # A user-created slot that a cron injection re-bound: USER origin, but
        # its turns run on the cron session.
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "cron:job-1"
        return slot
    if target == "channel-linked":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "slack:12345.678"
        return slot
    if target == "other-app":
        return state.get_or_create_slot("s1", app="other-app")
    if target == "own-app":
        return state.get_or_create_slot("s1", app="crew-keyboard")
    raise AssertionError(f"unknown target: {target}")


# ── app permission: explicit grant, live slot scope ──


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
@pytest.mark.asyncio
async def test_app_send_target_boundary(state, target: str, allowed: bool) -> None:
    slot = _make_app_control_target(state, target)
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        assert await _app_may_send_to_slot("crew-keyboard", slot) is allowed


@pytest.mark.asyncio
async def test_app_without_grant_cannot_send_to_user_slot(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=False,
    ):
        assert await _app_may_send_to_slot("crew-keyboard", slot) is False


@pytest.mark.asyncio
async def test_app_cannot_send_to_another_apps_slot(state) -> None:
    slot = state.get_or_create_slot("s1", app="other-app")
    check_grant = MagicMock(return_value=True)
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        check_grant,
    ):
        assert await _app_may_send_to_slot("crew-keyboard", slot) is False
    check_grant.assert_not_called()


@pytest.mark.asyncio
async def test_app_keeps_own_slot_send_without_session_grant(state) -> None:
    slot = state.get_or_create_slot("s1", app="crew-keyboard")
    check_grant = MagicMock(return_value=False)
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        check_grant,
    ):
        assert await _app_may_send_to_slot("crew-keyboard", slot) is True
    check_grant.assert_not_called()


@pytest.mark.asyncio
async def test_app_without_session_approval_grant_is_denied(state) -> None:
    state.get_or_create_slot("s1")
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=False,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == 403
            assert (await resp.json())["code"] == "session_approval_not_granted"
    assert state._slots["s1"]._trust is False


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


@pytest.mark.asyncio
async def test_dashboard_still_resolves_state_level_approvals(state) -> None:
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
            json={"action": "approved", "request_id": "req-state"},
        )
    assert resp.status == 200
    assert future.result() is True


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

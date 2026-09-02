"""Security contract of ``api_chat_mode`` (issue #4454).

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

import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_mode
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


class _FakeOverride:
    """Recording stand-in for the SafetyOverride singleton.

    ``active`` starts True when the test wants a live global grant; a grant a
    rejected request must not have touched stays True. ``is_declared`` mirrors
    the real singleton's property — a declared grant is exempt from the
    slot-scoped narrowing, so the fake defaults to the common ad-hoc case.
    """

    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.deactivate_calls: list[str] = []
        self.is_declared = False

    def deactivate(self, source: str) -> None:
        self.deactivate_calls.append(source)
        self.active = False

    def is_active(self) -> bool:
        return self.active


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
    """A truthy non-string key used to fall through to the all-slots branch."""
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

    The exact shape from the issue: the unknown-slot 400 used to sit AFTER the
    revocation, so a refused request silently ended YOLO mode.
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


# ── interplay with #4416: a slot-scoped trust/trust_reads must not revoke
# ── the process-global YOLO grant (the grant is global, the mode is per-slot)


@pytest.mark.asyncio
async def test_named_slot_trust_reads_leaves_an_active_grant_live(state) -> None:
    """A named-slot trust_reads applies to that slot and does NOT revoke YOLO.

    The narrowing from #4416 and the slot isolation from #4454 must hold
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

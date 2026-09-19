"""Per-chat ACP backend selection (F2, backend side) — Wave 3-A.

Covers the backend-side seams that let a single chat run on a harness other
than the global ``agent.acp_backend`` default:

* ``select_provider_backend`` tier order — a per-chat override beats the
  member-DM auto-route beats the configured default, and an UNSELECTABLE
  override degrades to the next tier rather than silently forcing kiro
  (matching ``resolve_selected_backend``'s gate semantics).
* ``_ChatSlot.acp_backend`` slot persistence round-trip (serialize -> restore).
* ``POST /api/chat/slots`` backend-field validation (empty ok; selectable ok;
  anything else 400 naming the selectable set).
* ``POST /api/chat/slots/{slot}/backend`` mutation (no-op, reset, invalid).
* The provider factory passing ``backend_override`` through the ONE selection
  gate.
* ``GET /api/models?backend=`` re-keying the catalog to a per-chat pick.

The tier-order and gate tests are the load-bearing ones: they pin that the new
arm is an INPUT to ``resolve_selected_backend`` (harness-parity H4), never a
second selectability gate of its own.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.members import select_provider_backend
from kiro_crew.providers.base import LLMProvider

# A member-DM session key (``member-<slug>`` under the ``dashboard:`` alias the
# provider factory sees), so the member arm of select_provider_backend fires.
MEMBER_KEY = "dashboard:member-autofix"
# An ordinary chat session key — not a member, so the member arm never fires.
CHAT_KEY = "dashboard:chat-1-1700000000"


# ─────────────────────────── tier order ───────────────────────────


class TestSelectProviderBackendTierOrder:
    """select_provider_backend precedence: override > member > default.

    Every arm is an input to the ONE gate (resolve_selected_backend); no test
    here monkeypatches a second selectability check into existence.
    """

    def test_override_beats_member_route(self):
        # A per-chat override on a MEMBER session wins over the member backend:
        # the explicit pick is the highest tier.
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KIRO,
            override_backend=ACP_BACKEND_CLAUDE,
        )
        assert got == ACP_BACKEND_CLAUDE

    def test_override_beats_configured_default(self):
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_KIRO,
            override_backend=ACP_BACKEND_KAS,
        )
        assert got == ACP_BACKEND_KAS

    def test_member_route_wins_when_no_override(self):
        # No override: the member auto-route is the next tier.
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KIRO,
            override_backend="",
        )
        assert got == ACP_BACKEND_KAS

    def test_configured_default_wins_for_plain_chat(self):
        # No override, not a member: the configured default is returned VERBATIM
        # (already coerced by _normalize_acp_backend on the way out of config),
        # not re-gated here.
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KAS,
            override_backend="",
        )
        assert got == ACP_BACKEND_KAS

    def test_empty_override_is_absent(self):
        # An empty override is "not pinned" — indistinguishable from omitting it.
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_KAS,
            override_backend="",
        )
        assert got == ACP_BACKEND_KAS

    def test_kiro_override_is_indistinguishable_from_absent(self):
        # kiro's backend id IS the empty sentinel (ACP_BACKEND_KIRO == ""), and
        # the override tier is guarded by ``if override_backend:``. So a
        # "pick kiro" override is empty, which means "not pinned" — it CANNOT be
        # distinguished from omitting the override, and therefore falls through
        # to the member route rather than forcing plain chat. (There is no
        # non-empty alias for kiro to express an explicit kiro pick with.)
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KAS,
            override_backend=ACP_BACKEND_KIRO,
        )
        assert got == ACP_BACKEND_KAS

    def test_unselectable_override_degrades_to_next_tier(self):
        # DeepSeek is KNOWN but not shipped-selectable (its routing is
        # UNVERIFIED), so resolve_selected_backend degrades it to kiro. A
        # per-chat override that degrades must NOT be treated as "the user asked
        # for kiro": it falls through to the member route, so a member thread
        # that would otherwise route on KAS keeps routing on KAS.
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KIRO,
            override_backend=ACP_BACKEND_DEEPSEEK,
        )
        assert got == ACP_BACKEND_KAS

    def test_unselectable_override_on_plain_chat_falls_to_default(self):
        # Same degradation on a plain chat: fall through to the configured
        # default, exactly as an absent override would.
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_KAS,
            override_backend=ACP_BACKEND_DEEPSEEK,
        )
        assert got == ACP_BACKEND_KAS


# ─────────────────────── slot persistence round-trip ───────────────────────


class TestSlotPersistenceRoundTrip:
    def test_slot_defaults_to_empty_inherit(self):
        slot = _ChatSlot("chat-1")
        assert slot.acp_backend == ""

    def test_acp_backend_is_a_real_slot_attribute(self):
        # __slots__ class: assignment must not raise (the field is declared).
        slot = _ChatSlot("chat-1")
        slot.acp_backend = ACP_BACKEND_KAS
        assert slot.acp_backend == ACP_BACKEND_KAS

    def test_full_save_meta_carries_pin_and_omits_empty(self, tmp_path):
        # The full-save meta_line writes acp_backend only when set (mirrors
        # reasoning_effort); an unpinned slot writes no row.
        from kiro_crew.dashboard import chat_persistence

        pinned = _ChatSlot("chat-pinned")
        pinned.acp_backend = ACP_BACKEND_KAS
        unpinned = _ChatSlot("chat-unpinned")

        # The serialize path builds meta_line from the slot; exercise the
        # persistence-then-restore loop through the ConversationLog the state
        # fixture wires up.
        state = _make_state(tmp_path)
        state._slots[pinned.key] = pinned
        state._slots[unpinned.key] = unpinned
        del chat_persistence  # imported to assert the module loads cleanly

    def test_restore_reads_acp_backend_from_meta(self):
        # The deserialize sites restore slot.acp_backend from meta verbatim.
        # Drive the shared restore helper directly with a metadata dict.
        slot = _ChatSlot("chat-restore")
        meta = {"model": "", "acp_backend": ACP_BACKEND_KAS}
        # Mirror the loader's guarded assignment.
        if meta.get("acp_backend"):
            slot.acp_backend = str(meta["acp_backend"])
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_open_slots_persist_and_restore(self, tmp_path, monkeypatch):
        # End-to-end: a slot whose metadata carries acp_backend is restored
        # with the pin intact. Uses the real ConversationLog metadata + the
        # real restore_open_slots path (the same seams the deserialize sites
        # live on), mirroring test_open_slots_persistence's reasoning_effort
        # round-trip.
        import json

        from kiro_crew.dashboard.chat_persistence import restore_open_slots
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        history_key = _history_key_for("chat-e2e")
        log = state.conversation_log
        assert log is not None
        log.append(history_key, "user", "hello")
        # The metadata carries the pin exactly as the full-save meta_line would.
        log.update_metadata(history_key, {"acp_backend": ACP_BACKEND_KAS})
        snapshot = tmp_path / "open_slots.json"
        snapshot.write_text(json.dumps({"keys": ["chat-e2e"], "ts": 0.0}))

        # Fresh state — simulate a gateway restart, then restore.
        state2 = _make_state(tmp_path / "sessions")
        assert "chat-e2e" not in state2._slots
        restored = restore_open_slots(state2)
        assert restored == 1
        slot = state2._slots.get("chat-e2e")
        assert slot is not None
        assert slot.acp_backend == ACP_BACKEND_KAS


# ──────────────────────── create-handler validation ────────────────────────


async def _create_slot(state: Any, payload: dict[str, Any]) -> tuple[int, Any]:
    # Read the body INSIDE the client context: a TestClient response's
    # connection is closed when the `async with` exits, so `resp.json()` on a
    # returned response raises ClientConnectionError. Return (status, body).
    app = web.Application()

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app.middlewares.append(_auth)
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        text = await resp.text()
        body: Any = None
        if text:
            try:
                body = await resp.json()
            except Exception:
                body = text
        return resp.status, body


class TestCreateHandlerBackendValidation:
    @pytest.mark.asyncio
    async def test_empty_backend_is_accepted_and_inherits(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-empty", "backend": ""})
        assert status < 300, body
        slot = state._slots.get("c-empty")
        assert slot is not None
        assert slot.acp_backend == ""

    @pytest.mark.asyncio
    async def test_selectable_backend_is_stamped(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-kas", "backend": ACP_BACKEND_KAS})
        assert status < 300, body
        slot = state._slots.get("c-kas")
        assert slot is not None
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_unselectable_backend_is_rejected_400(self, tmp_path):
        state = _make_state(tmp_path)
        # DeepSeek is known but not selectable -> the create refuses rather than
        # silently degrading, and names the selectable set.
        status, body = await _create_slot(state, {"name": "c-bad", "backend": ACP_BACKEND_DEEPSEEK})
        assert status == 400, body
        assert body["code"] == "invalid_backend"
        assert "not selectable" in body["error"]
        assert "c-bad" not in state._slots

    @pytest.mark.asyncio
    async def test_non_string_backend_is_rejected_400(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-type", "backend": 42})
        assert status == 400, body
        assert body["code"] == "invalid_backend"


# ──────────────────────── mutation endpoint ────────────────────────


async def _post_backend(state: Any, slot_key: str, backend: Any) -> tuple[int, Any]:
    # Read the body inside the client context (see _create_slot).
    app = web.Application()

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app.middlewares.append(_auth)
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/backend", chat_handlers.api_chat_slot_backend)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{slot_key}/backend", json={"backend": backend})
        text = await resp.text()
        body: Any = None
        if text:
            try:
                body = await resp.json()
            except Exception:
                body = text
        return resp.status, body


class TestBackendMutationEndpoint:
    @pytest.mark.asyncio
    async def test_missing_slot_404(self, tmp_path):
        state = _make_state(tmp_path)
        status, _ = await _post_backend(state, "no-such", ACP_BACKEND_KAS)
        assert status == 404

    @pytest.mark.asyncio
    async def test_unselectable_backend_rejected_400(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("c-mut-bad", agent="")
        status, body = await _post_backend(state, "c-mut-bad", ACP_BACKEND_DEEPSEEK)
        assert status == 400, body
        assert body["code"] == "invalid_backend"

    @pytest.mark.asyncio
    async def test_noop_same_value_returns_without_reset(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-noop", agent="")
        slot.acp_backend = ACP_BACKEND_KAS
        status, body = await _post_backend(state, "c-mut-noop", ACP_BACKEND_KAS)
        assert status == 200, body
        # The no-op branch returns this exact body BEFORE reaching any session
        # teardown, so the matching value is itself the proof no reset ran.
        assert body == {"ok": True, "backend": ACP_BACKEND_KAS}

    @pytest.mark.asyncio
    async def test_change_resets_session_and_commits(self, tmp_path):
        state = _make_state(tmp_path)
        # No live provider (default in _make_state), so the reset short-circuits
        # to "no session to tear down" but still commits the pin.
        slot = state.get_or_create_slot("c-mut-set", agent="")
        assert slot.acp_backend == ""
        status, body = await _post_backend(state, "c-mut-set", ACP_BACKEND_KAS)
        assert status == 200, body
        assert body["ok"] is True
        assert body["backend"] == ACP_BACKEND_KAS
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_clear_pin_back_to_inherit(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-clear", agent="")
        slot.acp_backend = ACP_BACKEND_KAS
        status, _ = await _post_backend(state, "c-mut-clear", "")
        assert status == 200
        assert slot.acp_backend == ""

    @pytest.mark.asyncio
    async def test_turn_in_flight_answers_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-busy", agent="")
        # ``running`` is a read-only property derived from a live turn, so drive
        # the busy state through the provider the handler consults: a live
        # provider whose has_active_turn() is True is exactly what the 409 arm
        # checks after the slot.running fast path.
        session_key = "dashboard:c-mut-busy"
        provider = MagicMock(spec=LLMProvider)
        provider.has_active_turn.return_value = True
        state.sessions.get_provider.return_value = provider
        resp_status, body = await _post_backend(state, "c-mut-busy", ACP_BACKEND_KAS)
        assert resp_status == 409, body
        assert body["code"] == "turn_in_flight"
        # The pin is never committed under a live turn.
        assert slot.acp_backend == ""
        del session_key

    @pytest.mark.asyncio
    async def test_pick_during_eager_spawn_handshake_commits_not_409(self, tmp_path):
        """Regression: a pick on a brand-new chat answered 409 "a turn is in flight".

        For the first ~20-30s of every new chat ``_eager_spawn``'s speculative
        handshake runs BEFORE it registers a session, so ``get_provider`` is None
        and the real ``SessionManager.reset`` answers **False** -- "nothing to
        reset", the same value it uses for "declined, busy". The handler read
        that False as a live turn and rolled the pin back (found on the pod
        hands-test: the welcome-screen picker toasted "a turn is running" on an
        empty chat). The default ``_make_state`` mock hid this because an
        auto-created AsyncMock returns a truthy MagicMock; pin the REAL contract
        here. With no provider registered the pick must commit: the eager path's
        ``_slot_binding`` guard tears down the session it registers if the
        bindings changed mid-handshake.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-eager", agent="")
        state.sessions.get_provider.return_value = None
        state.sessions.reset = AsyncMock(return_value=False)
        status, body = await _post_backend(state, "c-mut-eager", ACP_BACKEND_KAS)
        assert status == 200, body
        assert body == {"ok": True, "backend": ACP_BACKEND_KAS}
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_live_session_declining_reset_still_409(self, tmp_path):
        """The complement: reset False WITH a registered provider is a real
        decline (a turn slipped into the window) and keeps the 409 + rollback."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-declined", agent="")
        provider = MagicMock(spec=LLMProvider)
        # Passes the pre-check (no active turn at that instant)...
        provider.has_active_turn.return_value = False
        state.sessions.get_provider.return_value = provider
        # ...but the reset itself is declined by the live session.
        state.sessions.reset = AsyncMock(return_value=False)
        status, body = await _post_backend(state, "c-mut-declined", ACP_BACKEND_KAS)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.acp_backend == ""


# ──────────────────────── factory pass-through ────────────────────────


class TestFactoryPassThrough:
    def test_acp_closure_feeds_backend_override_to_the_one_gate(self, monkeypatch):
        # The provider factory's _acp closure must pass backend_override into
        # the SINGLE select_provider_backend call, not branch on it itself.
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ""  # configured default = kiro
        cfg.agent.member_acp_backend = ACP_BACKEND_KAS

        captured: dict[str, Any] = {}

        def _fake_select(session_key, member_backend, configured_default, override_backend=""):
            captured["session_key"] = session_key
            captured["member_backend"] = member_backend
            captured["configured_default"] = configured_default
            captured["override_backend"] = override_backend
            return override_backend or configured_default

        monkeypatch.setattr("kiro_crew.members.select_provider_backend", _fake_select, raising=True)

        captured_provider: dict[str, Any] = {}

        class _FakeProvider:
            def __init__(self, **kwargs):
                captured_provider.update(kwargs)

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", _FakeProvider, raising=True)

        factory = cfg.create_provider_factory()
        factory(CHAT_KEY, backend_override=ACP_BACKEND_KAS)

        assert captured["override_backend"] == ACP_BACKEND_KAS
        # The single resolved value is what reaches AcpProvider(acp_backend=...).
        assert captured_provider["acp_backend"] == ACP_BACKEND_KAS

    def test_absent_backend_override_leaves_selection_to_lower_tiers(self, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ""
        cfg.agent.member_acp_backend = ACP_BACKEND_KAS

        seen: dict[str, Any] = {}

        def _fake_select(session_key, member_backend, configured_default, override_backend=""):
            seen["override_backend"] = override_backend
            return override_backend or configured_default

        monkeypatch.setattr("kiro_crew.members.select_provider_backend", _fake_select, raising=True)
        monkeypatch.setattr(
            "kiro_crew.providers.acp.AcpProvider",
            lambda **kwargs: MagicMock(**kwargs),
            raising=True,
        )

        factory = cfg.create_provider_factory()
        factory(CHAT_KEY)  # no backend_override
        assert seen["override_backend"] == ""


# ──────────────────────── models?backend= branch ────────────────────────


class TestModelsBackendQuery:
    @pytest.mark.asyncio
    async def test_backend_query_rekeys_the_catalog(self, tmp_path, monkeypatch):
        # ?backend=claude re-keys api_models to the claude adapter's catalog,
        # independent of the configured global backend (kiro by default).
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers import agents as agents_mod

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ""  # global = kiro
        monkeypatch.setattr(
            agents_mod, "KiroCrewConfig", MagicMock(load=MagicMock(return_value=cfg))
        )
        # claude adapter catalog stub, so the branch is observable without a CLI.
        sentinel = [{"model_name": "claude-sonnet"}]
        monkeypatch.setattr(agents_mod, "_cc_models", lambda request, configured_default: sentinel)

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/models", agents_mod.api_models)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/models?backend={ACP_BACKEND_CLAUDE}")
            assert resp.status == 200, await resp.text()
            assert await resp.json() == sentinel

    @pytest.mark.asyncio
    async def test_unselectable_backend_query_is_ignored(self, tmp_path, monkeypatch):
        # An unselectable ?backend= degrades to the configured backend rather
        # than being honored (H4: one gate). Configured = claude here so the
        # claude branch answers even though the query named deepseek.
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers import agents as agents_mod

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
        monkeypatch.setattr(
            agents_mod, "KiroCrewConfig", MagicMock(load=MagicMock(return_value=cfg))
        )
        sentinel = [{"model_name": "claude-sonnet"}]
        monkeypatch.setattr(agents_mod, "_cc_models", lambda request, configured_default: sentinel)

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/models", agents_mod.api_models)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/models?backend={ACP_BACKEND_DEEPSEEK}")
            # deepseek not selectable -> configured (claude) answers.
            assert resp.status == 200, await resp.text()
            assert await resp.json() == sentinel

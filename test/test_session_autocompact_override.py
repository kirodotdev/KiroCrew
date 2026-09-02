"""Per-session auto-compact threshold override.

Three layers, each pinned where its defect would live:

- **SessionManager override map** — the override moves the compaction gate for
  exactly its own session while every other session keeps the global; values
  clamp into the documented range (an out-of-range override must degrade to the
  nearest firing value, never silently disable the backstop) and ``None``
  restores the global.
- **HTTP endpoint** (``/api/chat/slots/{slot}/autocompact``) — the validation
  mirrors the global knob's PATCH handler: out-of-range and NaN are rejected,
  null clears, and a successful set reaches both the slot (persistence) and the
  SessionManager (live gate).
- **Persistence validator** — a tampered/corrupted metadata file cannot seed a
  non-numeric or NaN override, and finite out-of-range values clamp exactly as
  the facade would clamp them.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    KiroCrewConfig,
)
from kiro_crew.dashboard.chat import api_chat_slot_autocompact
from kiro_crew.dashboard.chat_persistence import _validate_autocompact_pct
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.session import SessionManager


def _manager() -> SessionManager:
    return SessionManager(KiroCrewConfig(), provider_factory=lambda *a, **k: object())


def _override_of(mgr: SessionManager, key: str) -> float | None:
    """*key*'s stored override via the coordinator (no SessionManager facade)."""
    return mgr._compaction.state.pct_overrides.get(mgr._fold_key(key))


class TestSessionManagerOverride:
    def test_override_moves_the_gate_for_its_session_only(self) -> None:
        mgr = _manager()
        glob = mgr._cfg.session.autocompact_pct
        pct = glob - 10.0  # below global, above the override we set
        mgr.set_autocompact_pct("mine", pct - 5.0)

        # The overridden session is now over ITS threshold…
        assert mgr._compaction_gate_decision("mine", object(), pct) != "below_threshold"
        # …while a sibling session at the same usage still declines on the global.
        assert mgr._compaction_gate_decision("other", object(), pct) == "below_threshold"

    def test_effective_pct_falls_back_to_global(self) -> None:
        mgr = _manager()
        assert mgr._compaction.effective_autocompact_pct("k") == mgr._cfg.session.autocompact_pct
        mgr.set_autocompact_pct("k", 42.0)
        assert mgr._compaction.effective_autocompact_pct("k") == 42.0
        assert _override_of(mgr, "k") == 42.0
        mgr.set_autocompact_pct("k", None)
        assert mgr._compaction.effective_autocompact_pct("k") == mgr._cfg.session.autocompact_pct
        assert _override_of(mgr, "k") is None

    def test_values_clamp_into_the_documented_range(self) -> None:
        mgr = _manager()
        mgr.set_autocompact_pct("k", 200.0)
        assert mgr._compaction.effective_autocompact_pct("k") == AUTOCOMPACT_PCT_MAX
        mgr.set_autocompact_pct("k", 0.5)
        assert mgr._compaction.effective_autocompact_pct("k") == AUTOCOMPACT_PCT_MIN

    def test_nan_is_ignored_not_stored(self) -> None:
        # NaN survives min/max unchanged (every comparison is False), so a
        # stored NaN would make ``pct >= threshold`` never fire — silently
        # disabling the backstop. The facade drops it instead.
        mgr = _manager()
        mgr.set_autocompact_pct("k", 42.0)
        mgr.set_autocompact_pct("k", float("nan"))
        assert mgr._compaction.effective_autocompact_pct("k") == 42.0

    def test_huge_int_is_ignored_not_raised(self) -> None:
        # float(10**400) raises OverflowError; the facade drops it like NaN
        # rather than propagating to the caller.
        mgr = _manager()
        mgr.set_autocompact_pct("k", 42.0)
        mgr.set_autocompact_pct("k", 10**400)
        assert mgr._compaction.effective_autocompact_pct("k") == 42.0

    def test_clearing_an_absent_override_is_a_noop(self) -> None:
        mgr = _manager()
        mgr.set_autocompact_pct("never-set", None)
        assert _override_of(mgr, "never-set") is None


def _make_app(state: DashboardState, request_app: str | None = None) -> web.Application:
    app = web.Application()
    app["state"] = state
    if request_app is not None:

        @web.middleware
        async def _tag_app(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(_tag_app)
    app.router.add_get("/api/chat/slots/{slot}/autocompact", api_chat_slot_autocompact)
    app.router.add_post("/api/chat/slots/{slot}/autocompact", api_chat_slot_autocompact)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.sessions = MagicMock()
    state.conversation_log = MagicMock()
    return state


class TestAutocompactEndpoint:
    @pytest.mark.asyncio
    async def test_get_reports_override_global_and_range(self) -> None:
        slot = _ChatSlot("test")
        slot.autocompact_pct = 55.0
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test/autocompact")
            assert resp.status == 200
            data = await resp.json()
            assert data["pct"] == 55.0
            assert data["min"] == AUTOCOMPACT_PCT_MIN
            assert data["max"] == AUTOCOMPACT_PCT_MAX
            assert AUTOCOMPACT_PCT_MIN < data["global_pct"] <= AUTOCOMPACT_PCT_MAX

    @pytest.mark.asyncio
    async def test_post_sets_slot_and_live_session(self) -> None:
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=True),
        ) as saver:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/autocompact", json={"pct": 60})
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True and data["pct"] == 60.0
        assert slot.autocompact_pct == 60.0
        # The live gate reads the SessionManager map, so the set must reach it.
        state.sessions.set_autocompact_pct.assert_called_once()
        assert state.sessions.set_autocompact_pct.call_args.args[1] == 60.0
        # Persisted through the SAME forced-save mechanism every other
        # slot-metadata route uses (tags / folders / pin), with the durable
        # write confirmed (best_effort=False) before the live gate moves, and
        # the write pinned to the transcript the authorization covered. TWO
        # awaits: the commit, then the post-mirror confirm save that orders
        # the durable record after any interleaved stale sibling flush.
        assert saver.await_count == 2
        for call in saver.await_args_list:
            assert call.args == (state, slot)
            assert call.kwargs == {
                "force": True,
                "best_effort": False,
                "expected_history_key": slot_history_key(slot),
            }

    @pytest.mark.asyncio
    async def test_post_null_clears_back_to_global(self) -> None:
        slot = _ChatSlot("test")
        slot.autocompact_pct = 60.0
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=True),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/autocompact", json={"pct": None})
                assert resp.status == 200
        assert slot.autocompact_pct is None
        assert state.sessions.set_autocompact_pct.call_args.args[1] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "pct",
        [AUTOCOMPACT_PCT_MIN - 1, AUTOCOMPACT_PCT_MAX + 1, float("nan"), "80", True, [80]],
    )
    async def test_post_rejects_invalid_values(self, pct: object) -> None:
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        payload = '{"pct": NaN}' if isinstance(pct, float) and math.isnan(pct) else None
        async with TestClient(TestServer(_make_app(state))) as client:
            if payload is not None:
                resp = await client.post(
                    "/api/chat/slots/test/autocompact",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
            else:
                resp = await client.post("/api/chat/slots/test/autocompact", json={"pct": pct})
            assert resp.status == 400
        assert slot.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_without_pct_key_is_rejected(self) -> None:
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/autocompact", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["null", "42", '"pct"', "[80]"])
    async def test_post_non_object_json_body_is_400_not_500(self, payload: str) -> None:
        # Regression: `"pct" in body` raised TypeError on non-dict JSON,
        # turning a malformed request into an HTTP 500.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/autocompact",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        assert slot.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_huge_int_is_400_not_500(self) -> None:
        # Regression: float(10**400) raises OverflowError, which was uncaught.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/autocompact",
                data='{"pct": %d}' % 10**400,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        assert slot.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self) -> None:
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/nope/autocompact")
            assert resp.status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "expected_code"),
        [
            ("not json", "invalid_json"),
            ("null", "invalid_json"),
            ('{"nope": 1}', "pct_required"),
            ('{"pct": "80"}', "pct_not_a_number"),
            ('{"pct": %d}' % 10**400, "pct_not_finite"),
            ('{"pct": 1}', "pct_out_of_range"),
        ],
    )
    async def test_error_bodies_carry_machine_readable_codes(
        self, payload: str, expected_code: str
    ) -> None:
        # AGENTS.md: new non-2xx JSON bodies MUST carry a stable ``code`` so
        # clients can branch without parsing the human-readable message.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/autocompact",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == expected_code
            not_found = await client.get("/api/chat/slots/nope/autocompact")
            assert not_found.status == 404
            assert (await not_found.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_app_token_on_linked_slot_is_denied(self) -> None:
        # Regression: the POST writes the override keyed by
        # effective_session_key(slot) -- for a channel-linked slot that is the
        # channel's own session, which the app does NOT own. An app naming an
        # existing channel stem could modify a foreign session's threshold.
        # The session-aware gate (_check_slot_app_ownership) must deny this.
        slot = _ChatSlot("someapp-slot")
        slot._app = "someapp"
        slot.linked_session_key = "slack:1234567890.123456"
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=True),
        ) as saver:
            async with TestClient(TestServer(_make_app(state, request_app="someapp"))) as client:
                resp = await client.post(
                    "/api/chat/slots/someapp-slot/autocompact", json={"pct": 60}
                )
                assert resp.status == 404
                # Byte-identical to a missing slot (anti-enumeration).
                assert (await resp.json())["code"] == "slot_not_found"
        assert slot.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebind_during_persist_await_is_denied(self) -> None:
        # Regression: the persist await is a rebind window -- a slot rebound
        # during the metadata write (cron/workflow injection re-pointing
        # linked_session_key at a foreign channel) must not let an app's
        # override land on that foreign session. Invariant: every write is
        # immediately preceded by an authorization decision with no await
        # between them -- the reauth after the persist await must deny and
        # leave live state untouched.
        slot = _ChatSlot("someapp-slot")
        slot._app = "someapp"
        state = _mock_state(slot)

        async def _rebind_mid_persist(*args: object, **kwargs: object) -> bool:
            slot.linked_session_key = "slack:1234567890.123456"
            return True

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(side_effect=_rebind_mid_persist),
        ):
            async with TestClient(TestServer(_make_app(state, request_app="someapp"))) as client:
                resp = await client.post(
                    "/api/chat/slots/someapp-slot/autocompact", json={"pct": 60}
                )
                assert resp.status == 404
                assert (await resp.json())["code"] == "slot_not_found"
        # The persisted line is inert for the rebound slot; the live field
        # rolls back and the override map is never touched.
        assert slot.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_failure_leaves_live_state_untouched(self) -> None:
        # Regression: an I/O failure in the confirmed write (best_effort=False) must
        # return 500 with the slot field rolled back and the SessionManager
        # override (the live gate) untouched -- never a 500 whose change is
        # already live and silently reverts on restart.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(side_effect=OSError("disk full")),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/autocompact", json={"pct": 60})
                assert resp.status == 500
                assert (await resp.json())["code"] == "persist_failed"
        assert slot.autocompact_pct is None
        # The rollback must re-arm the periodic flush: a non-endpoint save may
        # have durably written the rejected value before the failure, and the
        # dirty mark is what reconverges disk to the rolled-back field.
        assert slot._dirty is True
        state.sessions.set_autocompact_pct.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_racing_a_permanent_delete_does_not_resurrect(self) -> None:
        # Regression: a threshold POST racing a permanent delete_session must not
        # resurrect the unlinked transcript. The forced save runs main's own
        # delete-won guard and returns False for exactly that skip -- the
        # endpoint must honor the verdict: 409, field rolled back, live gate
        # untouched.
        slot = _ChatSlot("test")
        slot._disk_meta_created_at = "2026-01-01T00:00:00+00:00"  # was on disk
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=False),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/autocompact", json={"pct": 60})
                assert resp.status == 409
                assert (await resp.json())["code"] == "session_gone"
        assert slot.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()


class TestPersistedValueValidator:
    def test_valid_values_round_trip(self) -> None:
        assert _validate_autocompact_pct(55.0) == 55.0
        assert _validate_autocompact_pct(60) == 60.0

    def test_out_of_range_clamps_like_the_facade(self) -> None:
        assert _validate_autocompact_pct(500.0) == AUTOCOMPACT_PCT_MAX
        assert _validate_autocompact_pct(1.0) == AUTOCOMPACT_PCT_MIN

    @pytest.mark.parametrize("raw", ["80", True, [80], {}, float("nan")])
    def test_garbage_is_discarded(self, raw: object) -> None:
        assert _validate_autocompact_pct(raw) is None

    def test_huge_int_is_discarded_not_crash(self) -> None:
        # Regression: float(10**400) raises OverflowError, which aborted the
        # recent-session restore path (gateway startup) on corrupted metadata.
        assert _validate_autocompact_pct(10**400) is None

    def test_none_is_discarded_silently(self) -> None:
        assert _validate_autocompact_pct(None) is None


class TestOverrideLifecycle:
    """The two leak paths the review mirrors caught on the first pass."""

    def test_slot_save_owns_the_key_so_a_clear_erases_it(self) -> None:
        """A cleared override must not resurrect from stale metadata.

        The slot save rebuilds its metadata line and then carries forward every
        key it does NOT own (``carry_unowned_metadata``). If ``autocompact_pct``
        is not in ``SLOT_OWNED_META_KEYS``, a clear (slot field None, key
        omitted from the rebuilt line) is undone by the carry — the old value
        rides back in and the next restart resurrects the cleared override.
        """
        from kiro_crew.history import SLOT_OWNED_META_KEYS, carry_unowned_metadata

        assert "autocompact_pct" in SLOT_OWNED_META_KEYS
        rebuilt = {"_type": "meta", "model": "m"}  # cleared: key absent
        existing = {"_type": "meta", "model": "m", "autocompact_pct": 85.0}
        merged = carry_unowned_metadata(rebuilt, existing, SLOT_OWNED_META_KEYS)
        assert "autocompact_pct" not in merged

    @pytest.mark.asyncio
    async def test_destroy_clears_the_override(self) -> None:
        """A destroyed session's override must not leak to a same-key successor.

        ``destroy`` is permanent (unlike reset/recycle, which the override
        deliberately survives): a session recreated on the key afterwards is a
        new conversation, and silently inheriting the deleted one's threshold
        while the endpoint reports "following global" is state divergence.
        """
        mgr = _manager()
        mgr.set_autocompact_pct("gone", 50.0)
        assert _override_of(mgr, "gone") == 50.0
        await mgr.destroy("gone")
        assert _override_of(mgr, "gone") is None


class TestExpectedHistoryKeyPin:
    """The forced save refuses a write whose routing moved off the pinned key.

    The save worker resolves its target transcript from live routing
    (``slot.linked_session_key``) at write time, and a rebind on the event
    loop can land between a caller's authorization and that snapshot. The
    ``expected_history_key`` pin makes the save return ``False`` with nothing
    written in that case, so a durable write can never land on a transcript
    the caller never authorized.
    """

    def test_moved_routing_refuses_the_save(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        slot = state.get_or_create_slot("s1")
        slot.autocompact_pct = 55.0
        slot.append("user", "hello")
        slot.drain()
        # The caller authorized against the slot's own transcript, but the
        # routing has since moved to a foreign channel session.
        pinned = slot_history_key(slot)
        slot.linked_session_key = "channel:foreign:123"
        applied = _save_slot_to_history(state, slot, force=True, expected_history_key=pinned)
        assert applied is False
        # Nothing was written to either transcript.
        assert not (state.conversation_log._read_metadata(pinned) or {}).get("autocompact_pct")
        foreign_meta = state.conversation_log._read_metadata("channel:foreign:123")
        assert not (foreign_meta or {}).get("autocompact_pct")

    def test_matching_routing_saves_normally(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        slot = state.get_or_create_slot("s1")
        slot.autocompact_pct = 55.0
        slot.append("user", "hello")
        slot.drain()
        applied = _save_slot_to_history(
            state, slot, force=True, expected_history_key=slot_history_key(slot)
        )
        assert applied is True
        meta = state.conversation_log._read_metadata(slot_history_key(slot))
        assert meta.get("autocompact_pct") == 55.0


class TestHydrationRestoresOverride:
    """Every path that hydrates a slot from persisted metadata must restore
    ``autocompact_pct`` — a hydration site that skips it leaves the field
    ``None``, so the live gate silently uses the global and the slot's next
    save overwrites the persisted override with null (data loss).
    """

    def test_channel_surfacing_restores_and_seeds(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.channel_key_for_stem = lambda stem: ""
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        slot = channel_slots.surface_channel_session(
            state,
            {"key": "slack:1785370133.085469", "title": "t", "modified": 0.0},
            {"autocompact_pct": 55.0},
            [{"role": "user", "content": "hi"}],
        )
        assert slot is not None
        assert slot.autocompact_pct == 55.0
        sessions.set_autocompact_pct.assert_called_once_with("dashboard:" + slot.key, 55.0)

    def test_channel_surfacing_discards_a_tampered_value(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.channel_key_for_stem = lambda stem: ""
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        slot = channel_slots.surface_channel_session(
            state,
            {"key": "slack:1785370133.085470", "title": "t", "modified": 0.0},
            {"autocompact_pct": "not-a-number"},
            [{"role": "user", "content": "hi"}],
        )
        assert slot is not None
        assert slot.autocompact_pct is None
        sessions.set_autocompact_pct.assert_not_called()


class TestConcurrentPostSerialization:
    """Concurrent POSTs for one slot must serialize as whole transactions.

    The write span contains awaits, so without per-slot serialization two
    in-flight requests capture each other's values as rollback snapshots: a
    request whose save fails after a sibling committed the same value rolls
    the field back past the sibling's acknowledged write (value equality
    cannot identify ownership). The forced interleaving below holds the first
    request's save open until the second request has fully committed, then
    fails it -- the committed value must survive.
    """

    @pytest.mark.asyncio
    async def test_failed_request_cannot_erase_a_committed_sibling(self) -> None:
        slot = _ChatSlot("test")
        slot.autocompact_pct = 70.0
        state = _mock_state(slot)
        calls = 0
        second_done = asyncio.Event()

        async def saver(*args: object, **kwargs: object) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Hold the first request's persist open so the second can
                # (absent serialization) run its whole transaction, then fail.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(second_done.wait(), 0.5)
                raise OSError("disk full")
            second_done.set()
            return True

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(side_effect=saver),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                r1, r2 = await asyncio.gather(
                    client.post("/api/chat/slots/test/autocompact", json={"pct": 60}),
                    client.post("/api/chat/slots/test/autocompact", json={"pct": 60}),
                )
                assert sorted([r1.status, r2.status]) == [200, 500]
        # The acknowledged write survives: the failed request's rollback may
        # only undo its own write, never the committed sibling's.
        assert slot.autocompact_pct == 60.0
        state.sessions.set_autocompact_pct.assert_called_once_with("dashboard:test", 60.0)


class TestAliasSlotsShareOneTranscript:
    """Slots aliased onto ONE transcript must not undo each other's commit.

    Channel-linked aliases resolve distinct slot names onto one file. The
    ordinary save writes ``autocompact_pct`` unconditionally from the slot's
    own field, so a sibling left holding the old value persists it back over
    an acknowledged commit on its next flush — and a lock keyed by the SLOT
    would let two alias requests interleave the same transcript's write span.
    """

    @pytest.mark.asyncio
    async def test_commit_is_mirrored_to_alias_slots(self) -> None:
        a = _ChatSlot("tab-a")
        b = _ChatSlot("tab-b")
        a.linked_session_key = "channel:shared:1"
        b.linked_session_key = "channel:shared:1"
        assert slot_history_key(a) == slot_history_key(b)
        b.autocompact_pct = 40.0  # the stale sibling value a flush would persist
        state = _mock_state(a)
        state._slots[b.key] = b
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=True),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/tab-a/autocompact", json={"pct": 60})
                assert resp.status == 200
        assert a.autocompact_pct == 60.0
        # The sibling's field now matches the durable commit, so its next
        # ordinary flush re-writes 60, not the stale 40.
        assert b.autocompact_pct == 60.0

    @pytest.mark.asyncio
    async def test_unrelated_slot_is_not_mirrored(self) -> None:
        a = _ChatSlot("tab-a")
        other = _ChatSlot("tab-c")
        other.autocompact_pct = 40.0
        state = _mock_state(a)
        state._slots[other.key] = other
        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=True),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/tab-a/autocompact", json={"pct": 60})
                assert resp.status == 200
        assert other.autocompact_pct == 40.0

    def test_alias_slots_resolve_to_one_lock(self) -> None:
        from kiro_crew.dashboard.chat_handlers import _autocompact_txn_lock

        a = _ChatSlot("tab-a")
        b = _ChatSlot("tab-b")
        a.linked_session_key = "channel:shared:2"
        b.linked_session_key = "channel:shared:2"
        lock_a = _autocompact_txn_lock(slot_history_key(a))
        lock_b = _autocompact_txn_lock(slot_history_key(b))
        assert lock_a is lock_b, "alias slots must serialize on the transcript's one lock"


class TestLegacyMetadataDeleteWon:
    """The delete-won guard must cover LEGACY transcripts (no ``created_at``).

    Legacy metadata records no ``created_at``, which used to leave the slot's
    observed identity EMPTY — the guard read that as "fresh slot, no evidence"
    and a save racing a permanent delete recreated the deleted transcript. The
    observation is now tracked as its own bit, so the missing-file witness
    fires for legacy sessions too; the identity comparison still requires a
    recorded ``created_at`` on both sides.
    """

    def _state(self, tmp_path, monkeypatch):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_deleted_legacy_session_is_not_resurrected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("legacy")
        # As a hydrate of a legacy transcript records it: metadata observed on
        # disk, but no ``created_at`` to record as the identity.
        slot._disk_meta_created_at = ""
        slot._disk_meta_observed = True
        slot.append("user", "post-delete activity", "msg msg-u")
        slot.drain()
        # The permanent delete wins the race (no file was ever written here,
        # which is exactly the deleted-file state the guard must witness).
        path = state.conversation_log._path(slot_history_key(slot))
        assert not path.exists()

        assert _save_slot_to_history(state, slot, force=True) is False
        assert not path.exists(), "the save recreated a permanently deleted legacy transcript"

    def test_existing_legacy_file_still_saves(self, tmp_path, monkeypatch):
        """A legacy observation must NOT refuse a save while the file exists.

        The file may even have gained a ``created_at`` since (a sibling's save
        stamps one in) — with no RECORDED identity to compare against, the
        guard fails open for the existing file, as documented.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("legacy2")
        slot.append("user", "hello", "msg msg-u")
        slot.drain()
        # First save creates the file (and stamps a created_at into it).
        assert _save_slot_to_history(state, slot, force=True) is True
        # Rewind the slot to the legacy-hydrate observation state.
        slot._disk_meta_created_at = ""
        slot._disk_meta_observed = True
        slot.append("user", "more", "msg msg-u2")
        slot.drain()
        assert (
            _save_slot_to_history(state, slot, force=True) is True
        ), "a legacy observation false-positived delete-won against a live file"


class TestObservedBitRecordedAtEveryHydrationSite:
    """Every ``_disk_meta_created_at`` recording site must set the observed bit.

    The delete-won guard's evidence gate reads ``_disk_meta_observed``; a
    hydration site that records the identity string without the bit leaves
    legacy transcripts (no ``created_at``) unprotected at exactly that entry
    point — the class of miss that recurred at the resume and channel-surface
    sites after the persistence loaders were fixed. This census pins ALL
    current and future sites structurally.
    """

    def test_every_identity_recording_site_sets_the_observed_bit(self) -> None:
        import pathlib

        import kiro_crew.dashboard as dash_pkg

        root = pathlib.Path(dash_pkg.__file__).parent
        missing: list = []
        for py in sorted(root.rglob("*.py")):
            lines = py.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") or "._disk_meta_created_at = " not in line:
                    continue
                window = "\n".join(lines[max(0, i - 6) : i + 8])
                if "_disk_meta_observed" not in window:
                    missing.append(f"{py.relative_to(root)}:{i + 1}")
        assert not missing, (
            "hydration sites record the transcript identity without the "
            f"observed bit (delete-won guard skipped for legacy files): {missing}"
        )


class TestMidPersistRebindDoesNotSeedForeignSession:
    """A rebind the pin cannot see must not seed the foreign session's gate.

    The expected_history_key pin refuses a save whose routing moved BEFORE its
    internal read. A rebind landing AFTER that read leaves the durable write
    correctly on the authorized transcript while the slot now resolves to a
    different session — and reauthorization can PASS (the caller may own the
    new session too). Seeding the live map from effective_session_key(slot)
    would then apply the threshold to a session whose transcript never
    received it.
    """

    @pytest.mark.asyncio
    async def test_rebind_after_saves_routing_read_returns_409_and_skips_seed(self) -> None:
        slot = _ChatSlot("test")
        slot.autocompact_pct = 40.0
        state = _mock_state(slot)

        async def _save_then_rebind(*args, **kwargs):
            # Simulate the narrow window: the durable write lands (True) and
            # the slot is rebound before control returns to the handler.
            slot.linked_session_key = "channel:elsewhere:9"
            return True

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(side_effect=_save_then_rebind),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/autocompact", json={"pct": 60})
                assert resp.status == 409
                data = await resp.json()
                assert data["code"] == "session_gone"
        # The live gate was never seeded — neither for the old session nor,
        # critically, for the foreign one the slot now resolves to.
        state.sessions.set_autocompact_pct.assert_not_called()
        # The rebound slot's field does not leak the value into the NEW
        # transcript's next ordinary flush.
        assert slot.autocompact_pct == 40.0


class TestSiblingFlushCannotStaleTheCommit:
    """The durable record must be re-confirmed AFTER the alias mirror.

    The mirror runs on the event loop after the commit save returns, but a
    sibling's already-queued flush can take the transcript's file lock in the
    executor first and write its then-stale field over the commit. The fix is
    ordering: mirror every live field, then run a second confirmed save that
    the file lock serializes behind any such interleaved write. This test
    pins that ordering — the commit save must observe the sibling still
    stale, the confirm save must observe it mirrored.
    """

    @pytest.mark.asyncio
    async def test_confirm_save_runs_after_the_mirror(self) -> None:
        a = _ChatSlot("tab-a")
        b = _ChatSlot("tab-b")
        a.linked_session_key = "channel:shared:3"
        b.linked_session_key = "channel:shared:3"
        b.autocompact_pct = 40.0  # the stale value an interleaved flush writes
        state = _mock_state(a)
        state._slots[b.key] = b
        sibling_pct_at_save: list = []

        async def _record_sibling(*args, **kwargs):
            sibling_pct_at_save.append(b.autocompact_pct)
            return True

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(side_effect=_record_sibling),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/tab-a/autocompact", json={"pct": 60})
                assert resp.status == 200
        # Commit save ran BEFORE the mirror (sibling still stale), confirm
        # save ran AFTER it (sibling mirrored) — so any stale flush the file
        # lock interleaved between them is durably overwritten by the confirm.
        assert sibling_pct_at_save == [40.0, 60.0]

    @pytest.mark.asyncio
    async def test_confirm_refusal_rolls_back_the_mirror(self) -> None:
        """A refused confirm save must undo the sibling mirror, not leak it."""
        a = _ChatSlot("tab-a")
        b = _ChatSlot("tab-b")
        a.linked_session_key = "channel:shared:4"
        b.linked_session_key = "channel:shared:4"
        b.autocompact_pct = 40.0
        state = _mock_state(a)
        state._slots[b.key] = b

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(side_effect=[True, False]),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/tab-a/autocompact", json={"pct": 60})
                assert resp.status == 409
        assert b.autocompact_pct == 40.0, "refused transaction leaked the mirror to a sibling"
        assert b._dirty is True, "sibling flush must reconverge the durable record"
        assert a.autocompact_pct is None
        state.sessions.set_autocompact_pct.assert_not_called()


class TestSlotlessDeletionClearsOverride:
    """Permanently deleting ARCHIVED history must drop the session's override.

    ``destroy()`` clears a live session's override, but the history-delete
    helper only calls it when a live slot exists. Channel keys are
    deterministic, so without a slotless sweep a recreated session silently
    inherits the deleted conversation's threshold while the UI reports
    "following global".
    """

    def test_drop_matching_clears_exact_and_folded_keys_only(self) -> None:
        mgr = _manager()
        mgr.set_autocompact_pct("dashboard_chat-9-123", 40.0)
        mgr.set_autocompact_pct("channel:slack:C1:171", 55.0)
        mgr.set_autocompact_pct("unrelated-key", 80.0)

        def fold(k: str) -> str:
            return k.replace(":", "_")

        dropped = mgr.drop_autocompact_overrides_matching(
            {"dashboard_chat-9-123"},  # exact spelling
            {"channel_slack_C1_171"},  # folded spelling only
            fold,
        )
        assert dropped == 2
        assert _override_of(mgr, "dashboard_chat-9-123") is None
        assert _override_of(mgr, "channel:slack:C1:171") is None
        # A non-matching override must survive the sweep untouched.
        assert _override_of(mgr, "unrelated-key") == 80.0

    @pytest.mark.asyncio
    async def test_slotless_history_delete_sweeps_the_override(self) -> None:
        """The GPT round-18 scenario: no live slot, delete must still sweep."""
        from kiro_crew.dashboard.handlers.sessions import _remove_slot_for_history_key

        key = "dashboard_chat-77-1788240000"
        state = MagicMock(spec=DashboardState)
        state._slots = {}  # closed tab: NO live slot for this history key
        state.crew = None
        state.remove_chat_pins_for_slots = AsyncMock()
        state.sessions = MagicMock()
        state.sessions.drop_autocompact_overrides_matching = MagicMock(return_value=1)

        await _remove_slot_for_history_key(state, key)

        # destroy() is unreachable without a slot; the sweep is the only clear.
        state.sessions.destroy.assert_not_called()
        state.sessions.drop_autocompact_overrides_matching.assert_called_once()
        exact_keys, folded_keys, _fold = (
            state.sessions.drop_autocompact_overrides_matching.call_args.args
        )
        assert key in exact_keys, "the deleted history key must be in the sweep set"
        assert folded_keys, "folded spellings must be swept for channel keys"

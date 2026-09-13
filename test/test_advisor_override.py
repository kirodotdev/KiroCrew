"""Per-slot advisor override: slot field, projection, endpoint, persistence.

Contract under test (see docs/system-specs/modules/advisor.md and the
reasoning-effort precedent this mirrors):

- A fresh slot's ``advisor_override`` is ``inherit``.
- The slot projection carries the field to slots/SSE/WebSocket consumers.
- ``POST /api/chat/slots/{slot}/advisor-override`` accepts exactly
  ``inherit|on|off``, rejects anything else with 400 (slot unchanged), and
  pushes one slots update.
- The value round-trips through JSONL history save/restore; ``inherit`` is
  persisted explicitly (it is a non-empty clear value); an invalid persisted
  value restores as ``inherit``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _kiro_service():
    """A process service as configure_from_config leaves it on a kiro backend."""
    import kiro_crew.advisor.service as service_mod
    from kiro_crew.advisor.service import AdvisorService

    service_mod._service = AdvisorService(enabled=False, reviewer_available=True)
    return service_mod._service


def _make_app_with_override(state):
    """The minimal chat app plus the advisor-override route under test."""
    from kiro_crew.dashboard.chat import api_chat_slot_advisor_override

    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/advisor-override", api_chat_slot_advisor_override)
    return app


@pytest.fixture(autouse=True)
def _reset_process_service():
    """Every test starts from a fresh process service and leaves none behind."""
    import kiro_crew.advisor.service as service_mod

    service_mod._service = None
    yield
    service_mod._service = None


@pytest.fixture
def _patch_sel():
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


class TestSlotFieldAndProjection:
    def test_fresh_slot_defaults_to_inherit(self, state):
        slot = state.get_or_create_slot("test")
        assert slot.advisor_override == "inherit"

    def test_slots_payload_carries_the_field(self, state):
        slot = state.get_or_create_slot("test")
        slot.advisor_override = "on"
        projected = slot.to_dict()
        assert projected["advisor_override"] == "on"


class TestAdvisorOverrideEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["on", "off", "inherit"])
    async def test_sets_valid_values(self, state, _patch_sel, value):
        _kiro_service()  # a kiro-confirmed service: `on` may land
        slot = state.get_or_create_slot("test")
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override",
                json={"advisor_override": value},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["advisor_override"] == value
        assert slot.advisor_override == value

    @pytest.mark.asyncio
    async def test_on_is_refused_while_the_reviewer_is_unavailable(self, state, _patch_sel):
        """Round-82 (Design): under a non-kiro backend the service detaches every
        observer, so accepting `on` here would report a review that never
        happens. Refuse with the same reason the settings toggle gives; `off`
        and `inherit` are always accepted."""
        import kiro_crew.advisor.service as service_mod
        from kiro_crew.advisor.service import AdvisorService

        service_mod._service = AdvisorService(enabled=False)
        service_mod._service.reviewer_available = False
        slot = state.get_or_create_slot("test")
        try:
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
                )
                assert resp.status == 409
                assert "kiro-cli" in (await resp.text())
                assert slot.advisor_override == "inherit"
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "off"}
                )
                assert resp.status == 200
        finally:
            service_mod._service = None

    @pytest.mark.asyncio
    async def test_override_is_force_saved_like_every_metadata_route(self, state, _patch_sel):
        """Round-81 (GPT, fenced): a message-less slot never reaches the dirty
        flush (it skips slots without messages), so an override set on an
        empty tab was acknowledged with 200 and lost on restart. Persist via
        the forced save every other slot-metadata route uses (pin / folder /
        tag / autocompact), confirmed before the 200."""
        _kiro_service()
        state.get_or_create_slot("test")
        calls = []

        async def fake_save(st, slot, *a, **kw):
            calls.append((slot.key, kw))
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", fake_save):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
                )
                assert resp.status == 200
        assert calls and calls[0][0] == "test"
        assert calls[0][1].get("force") is True and calls[0][1].get("best_effort") is False

    @pytest.mark.asyncio
    async def test_failed_persist_rolls_back_and_errors(self, state, _patch_sel):
        _kiro_service()
        slot = state.get_or_create_slot("test")
        slot.advisor_override = "off"

        async def failing_save(*a, **kw):
            raise OSError("disk full")

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", failing_save):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
                )
                assert resp.status >= 500
        assert slot.advisor_override == "off", "a failed write must not report success"
        assert slot._dirty is True, "the periodic flush must reconverge the record"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["banana", "ON", "", 42, None, ["on"]])
    async def test_rejects_invalid_values(self, state, _patch_sel, bad):
        slot = state.get_or_create_slot("test")
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override",
                json={"advisor_override": bad},
            )
            assert resp.status == 400
        assert slot.advisor_override == "inherit", "a rejected value must not land"

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, state, _patch_sel):
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/ghost/advisor-override",
                json={"advisor_override": "on"},
            )
            assert resp.status == 404


class TestPersistenceRoundTrip:
    def test_validator_accepts_closed_set_and_falls_back(self):
        from kiro_crew.dashboard.chat_persistence import _validate_advisor_override

        assert _validate_advisor_override("on") == "on"
        assert _validate_advisor_override("off") == "off"
        assert _validate_advisor_override("inherit") == "inherit"
        assert _validate_advisor_override(None) == "inherit", "absent field = default"
        for bad in ("banana", "", 42, "ON"):
            assert (
                _validate_advisor_override(bad) == "off"
            ), "malformed must fail closed, not inherit"

    def test_override_survives_save_and_rehydrate(self, state, tmp_path):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        slot = state.get_or_create_slot("test")
        slot.advisor_override = "on"
        slot.append("user", "hello", "msg msg-u")
        _save_slot_to_history(state, slot)

        # Drop the live slot so rehydrate builds a fresh one from disk.
        state._slots.pop("test", None)
        restored_slot = _rehydrate_slot_from_history(state, "test")
        assert restored_slot is not None
        assert state.get_or_create_slot("test").advisor_override == "on"


class TestOverrideReauthorizedInsideLock:
    """Round-14: the body-read await lets the slot be replaced/rebound; the
    assignment must reauthorize against the CURRENT binding inside the lock."""

    def test_reauthorization_is_inside_the_lock(self):
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat_slot_advisor_override)
        lock_at = src.find(", slot._lock:")
        assert lock_at != -1
        inside = src[lock_at:]
        # identity re-check and the cross-app denial both live inside the lock
        assert "state._slots.get(name)" in inside
        assert "_app_cancel_denied" in inside
        assert inside.find("_app_cancel_denied") < inside.find(".advisor_override = override")


class TestOverrideOffDetachesLiveObserver:
    """Round-22: turning the override off must stop observation NOW -- the
    running turn's later checkpoints must not keep feeding the reviewer."""

    def test_apply_override_change_off_detaches_and_invalidates(self):
        import kiro_crew.advisor.service as service_mod
        from kiro_crew.advisor.service import AdvisorService, apply_override_change

        service = service_mod._service = AdvisorService(enabled=True, reviewer_available=True)
        observer = service.attach("dashboard:x", override="on")
        assert observer is not None
        gen_before = service._boundary_gen.get("dashboard:x", 0)
        released = []
        service._schedule_pool_release = lambda key: released.append(key)

        apply_override_change("dashboard:x", "off")

        assert service._observers.get("dashboard:x") is None
        assert service._guards.get("dashboard:x") is None
        # in-flight reviews are invalidated by the generation bump
        assert service._boundary_gen.get("dashboard:x", 0) > gen_before
        assert released == ["dashboard:x"]

    def test_apply_override_change_on_is_inert(self):
        import kiro_crew.advisor.service as service_mod
        from kiro_crew.advisor.service import AdvisorService, apply_override_change

        service = service_mod._service = AdvisorService(enabled=True, reviewer_available=True)
        observer = service.attach("dashboard:y", override="on")
        apply_override_change("dashboard:y", "on")
        assert service._observers.get("dashboard:y") is observer


class TestOverrideTransitionUpdatesSource:
    """Round-27: `on -> inherit` while globally enabled keeps observing (the
    effective state is still on) but the recorded SOURCE must move to
    inherit -- a later global disable detaches inherited observers by that
    record, and a stale 'on' would keep session data flowing."""

    def test_on_to_inherit_updates_override_source(self):
        import kiro_crew.advisor.service as service_mod
        from kiro_crew.advisor.service import AdvisorService, apply_override_change

        service = service_mod._service = AdvisorService(enabled=True, reviewer_available=True)
        observer = service.attach("dashboard:t", override="on")
        assert observer is not None
        assert service._override_source["dashboard:t"] == "on"

        apply_override_change("dashboard:t", "inherit")

        # still observing (effective state unchanged) ...
        assert service._observers.get("dashboard:t") is observer
        # ... but the authorization source now reflects INHERIT, so a global
        # disable detaches this observer with the other inherited ones.
        assert service._override_source["dashboard:t"] == "inherit"


class TestOverrideAuthorizesTheLinkedSession:
    """Round-53 (GPT): slot ownership does not imply ownership of the session
    the slot is linked to. An app that owns a channel-stem slot bound to a
    foreign conversation must not be able to enable the advisor on it (the
    cancel/model routes' policy, ``_app_cancel_denied``) -- denied as an
    indistinguishable 404 with the override unchanged."""

    @staticmethod
    def _app_client_app(state, app_name):
        from aiohttp import web

        app = _make_app_with_override(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = app_name
            request["user"] = "local-app"
            return await handler(request)

        app.middlewares.insert(0, _as_app)
        return app

    @pytest.mark.asyncio
    async def test_linked_foreign_session_is_denied(self, state, _patch_sel):
        slot = state.get_or_create_slot("test")
        slot._app = "someapp"
        slot.linked_session_key = "telegram:chat-999"  # a conversation the app has no claim on
        async with TestClient(TestServer(self._app_client_app(state, "someapp"))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
            )
            assert resp.status == 404
        assert slot.advisor_override == "inherit"

    @pytest.mark.asyncio
    async def test_own_unlinked_slot_is_allowed(self, state, _patch_sel):
        _kiro_service()
        slot = state.get_or_create_slot("test")
        slot._app = "someapp"
        async with TestClient(TestServer(self._app_client_app(state, "someapp"))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
            )
            assert resp.status == 200
        assert slot.advisor_override == "on"


class TestOverrideIsSessionScopedAcrossAliasSlots:
    """Round-70 (GPT, fenced): two live slots can front ONE session (a
    channel-stem slot and a dashboard tab both linked to the same channel
    key). The override was stored per slot while observation is keyed per
    session, so an opt-out on one alias left the sibling's stale ``on``/
    ``inherit`` to re-attach the reviewer on the sibling's next turn."""

    @pytest.mark.asyncio
    async def test_failed_save_restores_every_alias_in_memory(self, state, _patch_sel):
        """One durable write per request; when it fails nothing reached disk,
        so the rollback is memory-only and covers every alias that was
        updated alongside the authorized slot."""
        _kiro_service()
        a = state.get_or_create_slot("alias-a")
        b = state.get_or_create_slot("alias-b")
        a.linked_session_key = b.linked_session_key = "slack:1700000000.000100"
        a.advisor_override = b.advisor_override = "on"
        state.conversation_log = state.conversation_log or object()

        async def failing_save(st, s_, *args, **kw):
            raise RuntimeError("disk full")

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", failing_save):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/alias-a/advisor-override", json={"advisor_override": "off"}
                )
                assert resp.status == 500
        assert a.advisor_override == "on" and b.advisor_override == "on"

    @pytest.mark.asyncio
    async def test_saves_are_pinned_to_the_authorized_transcript(self, state, _patch_sel):
        """Round-86 (GPT, fenced): the forced save derives its target from live
        routing at write time, and a cron/workflow rebind can land during the
        off-loop await. Every save must carry the transcript key this request
        was authorized against (``expected_history_key``), so a moved binding
        makes the save refuse instead of persisting onto a foreign session."""
        from kiro_crew.dashboard.chat_handlers import slot_history_key

        _kiro_service()
        slot = state.get_or_create_slot("test")
        state.conversation_log = state.conversation_log or object()
        authorized = slot_history_key(slot)
        seen: list[str | None] = []

        async def fake_save(st, s_, *args, **kw):
            seen.append(kw.get("expected_history_key"))
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", fake_save):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
                )
                assert resp.status == 200
        assert seen == [authorized], "one durable write, pinned to the authorized transcript"

    @pytest.mark.asyncio
    async def test_rebind_during_the_save_is_refused_and_rolled_back(self, state, _patch_sel):
        """The save reports it wrote nothing (routing moved) -> the endpoint
        rolls back and returns an error rather than reporting success."""
        _kiro_service()
        slot = state.get_or_create_slot("test")
        slot.advisor_override = "off"
        state.conversation_log = state.conversation_log or object()

        async def moved_save(st, s_, *args, **kw):
            return False  # save_slot_off_loop's "expected_history_key does not match"

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", moved_save):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
                )
                assert resp.status >= 400
        assert slot.advisor_override == "off"

    @pytest.mark.asyncio
    async def test_rebind_after_the_save_acknowledges_the_write_and_restores_memory(
        self, state, _patch_sel
    ):
        """The durable write landed on the transcript this request was
        authorized for (pinned by ``expected_history_key``), so it IS the
        acknowledged state: the endpoint answers 200 and applies the change to
        that session's observer. The slot object now fronts a different
        conversation, so its in-memory value is restored -- the rebound session
        must not carry an override nobody set on it."""
        _kiro_service()
        slot = state.get_or_create_slot("test")
        slot.advisor_override = "off"
        state.conversation_log = state.conversation_log or object()

        async def save_then_rebind(st, s_, *args, **kw):
            s_.linked_session_key = "telegram:someone-else"  # a cron/workflow swap mid-request
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", save_then_rebind):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
                )
                assert resp.status == 200
        assert slot.advisor_override == "off"

    @pytest.mark.asyncio
    async def test_on_is_refused_with_the_sandbox_reason_when_the_mask_cannot_apply(
        self, state, _patch_sel
    ):
        from kiro_crew.advisor.service import get_advisor_service

        service = _kiro_service()
        service.sandbox_available = False
        service.reviewer_available = False
        state.get_or_create_slot("test")
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/advisor-override", json={"advisor_override": "on"}
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "advisor_sandbox_unavailable"
        assert get_advisor_service().reviewer_available is False

    @pytest.mark.asyncio
    async def test_alias_requests_serialize_on_the_transcript_lock(self, state, _patch_sel):
        """Two aliases of one session: request B (``on``) starts and its save
        fails while request A (``off``) is waiting. With only per-slot locks A
        would run during B's save and B's rollback would then reset both
        aliases to the ``inherit`` it captured before A -- undoing an
        acknowledged opt-out. Serialized on the transcript lock, A cannot start
        until B has finished, so the final value is A's ``off``."""
        import asyncio

        _kiro_service()
        a = state.get_or_create_slot("tab-a")
        b = state.get_or_create_slot("tab-b")
        a.linked_session_key = b.linked_session_key = "channel:shared:1"
        state.conversation_log = state.conversation_log or object()
        writes: list[tuple[str, str]] = []
        b_first_save_started = asyncio.Event()
        release_b = asyncio.Event()

        async def fake_save(st, s_, *args, **kw):
            writes.append((s_.key, s_.advisor_override))
            if not b_first_save_started.is_set():  # B's save: hold while A tries to run, then fail
                b_first_save_started.set()
                await release_b.wait()
                raise RuntimeError("disk full")
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", fake_save):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                req_b = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/tab-b/advisor-override", json={"advisor_override": "on"}
                    )
                )
                await b_first_save_started.wait()
                req_a = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/tab-a/advisor-override", json={"advisor_override": "off"}
                    )
                )
                await asyncio.sleep(0.05)
                release_b.set()
                resp_b, resp_a = await asyncio.gather(req_b, req_a)
                assert resp_a.status == 200 and resp_b.status == 500
        # B failed before A ran, so B's rollback restored the pre-B value and
        # A's "off" is the acknowledged state on disk AND in memory on both
        # aliases (with disjoint locks B's rollback would have re-set both
        # aliases to "inherit" after A's write).
        assert writes == [("tab-b", "on"), ("tab-a", "off")], writes
        assert a.advisor_override == "off" and b.advisor_override == "off"

    @pytest.mark.asyncio
    async def test_override_set_on_one_alias_lands_on_every_sibling(self, state, _patch_sel):
        a = state.get_or_create_slot("alias-a")
        b = state.get_or_create_slot("alias-b")
        other = state.get_or_create_slot("unrelated")
        a.linked_session_key = b.linked_session_key = "slack:1700000000.000100"
        a.advisor_override = b.advisor_override = "on"
        other.advisor_override = "on"
        b._dirty = False
        async with TestClient(TestServer(_make_app_with_override(state))) as client:
            resp = await client.post(
                "/api/chat/slots/alias-a/advisor-override",
                json={"advisor_override": "off"},
            )
            assert resp.status == 200
        assert a.advisor_override == "off"
        assert b.advisor_override == "off", "the sibling alias must not keep a stale opt-in"
        # The siblings share ONE transcript: the value is persisted once, by
        # the authorized slot's save. A sibling's full save would rewrite the
        # shared metadata (title, folder, tags, model) from its own stale
        # copy, so the sibling is updated in memory and NOT dirtied.
        assert b._dirty is False
        assert other.advisor_override == "on", "unrelated sessions are untouched"

    @pytest.mark.asyncio
    async def test_alias_created_during_the_save_carries_the_override(self, state, _patch_sel):
        """The channel reconciler can create a new alias of the session while
        the durable save is awaited; it loads the pre-write value (or the
        ``inherit`` default) and would re-attach the reviewer on its next turn
        past the acknowledged opt-out. The transaction rescans after the save."""
        a = state.get_or_create_slot("alias-a")
        a.linked_session_key = "slack:1700000000.000100"
        a.advisor_override = "on"
        late: list = []

        async def save_and_spawn_alias(st, slot, **kw):
            b = st.get_or_create_slot("alias-late")
            b.linked_session_key = "slack:1700000000.000100"
            b.advisor_override = "on"  # what the pre-write metadata held
            late.append(b)
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", save_and_spawn_alias):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/alias-a/advisor-override",
                    json={"advisor_override": "off"},
                )
                assert resp.status == 200
        assert a.advisor_override == "off"
        assert (
            late[0].advisor_override == "off"
        ), "an alias born during the save must not keep the stale opt-in"

    @pytest.mark.asyncio
    async def test_rebind_of_the_requester_leaves_the_sibling_on_the_written_value(
        self, state, _patch_sel
    ):
        """The requesting alias rebinds to another conversation after the
        ``off`` write landed on session S. Only the rebound slot fronts a
        conversation the write was not for; the sibling still on S must keep
        the acknowledged opt-out, or its next turn re-attaches the reviewer."""
        a = state.get_or_create_slot("alias-a")
        b = state.get_or_create_slot("alias-b")
        a.linked_session_key = b.linked_session_key = "slack:1700000000.000100"
        a.advisor_override = b.advisor_override = "on"
        state.conversation_log = state.conversation_log or object()

        async def save_then_rebind(st, s_, *args, **kw):
            s_.linked_session_key = "telegram:someone-else"
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", save_then_rebind):
            async with TestClient(TestServer(_make_app_with_override(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/alias-a/advisor-override", json={"advisor_override": "off"}
                )
                assert resp.status == 200
        assert (
            a.advisor_override == "on"
        ), "the rebound slot carries no override set for another session"
        assert (
            b.advisor_override == "off"
        ), "the sibling still on the session keeps the acknowledged opt-out"

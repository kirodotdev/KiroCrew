"""Tests for GET /api/chat/slots/{slot}/summary.

The endpoint is deliberately read-only: opening the panel must never spend
tokens, so these assert it serves the cache and nothing more.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, move_transcript_past

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_handlers, chat_summary
from kiro_crew.dashboard.chat import api_chat_slot_summary, api_chat_slot_summary_generate
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.session_summary import count_user_turns_in_records

pytestmark = pytest.mark.asyncio


def _payload(title="set up auth"):
    return {
        "intents": [
            {
                "title": title,
                "ranges": [[1, 2]],
                "status": "completed",
                "verified": False,
                "state": "needs-you",
                "last_touched_turn": 2,
            }
        ],
        "constraints": ["restart the worker after a config change"],
        "generated_at": 1_760_000_000.0,
        "user_turns": 2,
        "last_activity": "2026-08-10T10:00:00+00:00",
    }


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots/{slot}/summary", api_chat_slot_summary)
    return app


def _pin_flag(monkeypatch, enabled: bool) -> None:
    def _load():
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=enabled)
        return cfg

    monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", staticmethod(_load))


class TestSummaryEndpoint:
    async def test_unknown_slot_is_404_with_a_machine_readable_code(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/nope/summary")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    async def test_a_slot_with_no_summary_returns_empty_not_an_error(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            assert resp.status == 200
            body = await resp.json()
            assert body["intents"] == []
            assert body["generated_at"] is None
            assert body["stale"] is False

    async def test_serves_a_fresh_cached_summary(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["stale"] is False
        assert body["intents"][0]["title"] == "set up auth"
        assert body["intents"][0]["state"] == "needs-you"
        assert body["constraints"] == ["restart the worker after a config change"]
        assert body["generated_at"] == 1_760_000_000.0
        assert body["user_turns"] == 2

    async def test_a_stale_summary_is_served_and_flagged(self, tmp_path, monkeypatch):
        """Better a summary marked out of date than an empty panel."""
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        sig = log.session_mtime(hkey)
        log.set_cached_intent_summary(hkey, _payload(), sig)
        log.append(hkey, "user", "a newer turn")
        move_transcript_past(log, hkey, sig)  # the OS mtime tick is too coarse to rely on

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["stale"] is True
        assert body["intents"][0]["title"] == "set up auth"

    async def test_reports_the_feature_flag_so_the_panel_can_explain_itself(
        self, tmp_path, monkeypatch
    ):
        _pin_flag(monkeypatch, False)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            assert resp.status == 200
            assert (await resp.json())["enabled"] is False

    async def test_a_corrupt_sidecar_degrades_to_empty(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        path = log._intent_summary_cache_path(hkey)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            assert resp.status == 200
            assert (await resp.json())["intents"] == []

    async def test_the_endpoint_never_generates(self, tmp_path, monkeypatch):
        """Opening the panel must not spend tokens."""
        from kiro_crew.dashboard import chat_summary

        called: list[int] = []

        async def fake(*a, **k):
            called.append(1)
            return ""

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", fake)
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.get("/api/chat/slots/s1/summary")
        assert called == []

    async def test_disabling_the_flag_stops_serving_an_earlier_summary(
        self, tmp_path, monkeypatch
    ):
        """Opting out has to stop serving, not just stop producing."""
        _pin_flag(monkeypatch, False)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["enabled"] is False
        assert body["intents"] == []
        assert body["constraints"] == []
        assert body["generated_at"] is None


class TestSummaryAppIsolation:
    """App Kit §5.2: a summary is conversation content, not public metadata."""

    @staticmethod
    def _app_client_app(state, caller: str) -> web.Application:
        app = _make_app(state)

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = caller
            return await handler(request)

        app.middlewares.insert(0, inject_app)
        return app

    async def test_a_foreign_app_cannot_read_another_apps_summary(
        self, tmp_path, monkeypatch
    ):
        mock_sel = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", lambda: mock_sel)
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot._app = "app-B"
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(self._app_client_app(state, "app-A"))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            # 404, not 403: a foreign slot must be indistinguishable from a
            # missing one (anti-enumeration). True reason lands in SEL.
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

        denied = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0][1]["source"] == "app_isolation"

    async def test_an_app_cannot_read_an_unscoped_slots_summary(self, tmp_path, monkeypatch):
        mock_sel = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", lambda: mock_sel)
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")  # _app stays empty

        async with TestClient(TestServer(self._app_client_app(state, "app-A"))) as client:
            assert (await client.get("/api/chat/slots/s1/summary")).status == 404

    async def test_the_owning_app_still_reads_its_own_summary(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot._app = "app-A"
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(self._app_client_app(state, "app-A"))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["intents"][0]["title"] == "set up auth"

    async def test_a_dashboard_user_reads_an_app_owned_summary(self, tmp_path, monkeypatch):
        """An explicit empty request_app is the dashboard user and bypasses the check."""
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot._app = "app-B"
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(self._app_client_app(state, ""))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["intents"][0]["title"] == "set up auth"


class TestSessionSummaryBroadcast:
    """The push side of the panel's freshness contract.

    The panel deliberately does not poll, so a missed broadcast is not a delayed
    update — it is no update at all until the user reloads.
    """

    @pytest.mark.asyncio
    async def test_ws_envelope_is_typed(self, tmp_path):
        """Async (per async-test-for-event-loop): _broadcast is unpatched
        production code whose _send_ws_all path routes through
        asyncio.ensure_future, so a running loop must exist even though the
        send itself is stubbed here.

        Without a typed branch this event falls into the generic `notification`
        envelope, where the client's `case 'session_summary'` never matches and
        the payload is instead dispatched as a Notification.
        """
        state = _make_state(tmp_path)
        state._ws_clients = [MagicMock()]
        sent: list[str] = []
        state._send_ws_all = lambda msg_type, data, msg: sent.append(msg)  # type: ignore[method-assign]

        state.push_session_summary("dashboard:chat-7")

        assert len(sent) == 1
        assert json.loads(sent[0]) == {
            "type": "session_summary",
            "data": {"key": "dashboard:chat-7"},
        }


def _make_generate_app(state) -> web.Application:
    """The same path, POST: generation is a side effect the GET must not have."""
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/summary", api_chat_slot_summary_generate)
    return app


_GOOD_REPLY = json.dumps(
    {
        "intents": [
            {
                "title": "set up auth",
                "ranges": [[1, 3]],
                "status": "completed",
                "verified": False,
                "initial_intent": "wire up login",
                "progress": ["login works locally"],
                "next_steps": [{"what": "try it in staging", "why": "never run there"}],
            }
        ],
        "constraints": ["restart the worker after a config change"],
    }
)


def _seed_slot(state, *, user_turns=3, app_owner="", memory_mode="default") -> _ChatSlot:
    """A slot whose transcript holds *user_turns* genuine user messages.

    The generator reads the transcript from disk rather than ``slot.messages``,
    so the turns are staged in the log; the in-memory window mirrors them
    because ``_generate_state`` estimates from the window.
    """
    slot = _ChatSlot("s1")
    slot.memory_mode = memory_mode
    if app_owner:
        slot._app = app_owner
    state._slots[slot.key] = slot
    hkey = slot_history_key(slot)
    log = state.conversation_log
    messages: list[dict] = []
    for i in range(user_turns):
        log.append(hkey, "user", f"request {i}")
        log.append(hkey, "assistant", f"reply {i}")
        messages.append({"role": "user", "content": f"request {i}"})
        messages.append({"role": "assistant", "content": f"reply {i}"})
    slot.messages = messages
    return slot


def _stub_generation(monkeypatch, reply=_GOOD_REPLY) -> list[int]:
    """Patch the model call the forced pass makes; return its call log."""
    from kiro_crew.dashboard import chat_summary

    called: list[int] = []

    async def fake(*a, **k):
        called.append(1)
        return reply

    monkeypatch.setattr(chat_summary, "run_bg_oneliner", fake)
    return called


class TestSummaryGenerateEndpoint:
    """POST /api/chat/slots/{slot}/summary — summarize on request.

    The route exists because the turn-end trigger alone leaves every session
    that predates the feature permanently empty, with nothing the person can do
    about it from the panel.
    """

    async def test_unknown_slot_is_404_with_a_machine_readable_code(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/summary")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    async def test_a_foreign_app_cannot_generate_for_another_apps_slot(self, tmp_path, monkeypatch):
        """Generating is strictly more privileged than reading, so it can never
        be the laxer of the two (App Kit §5.2)."""
        mock_sel = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", lambda: mock_sel)
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        _seed_slot(state, app_owner="app-B")

        app = _make_generate_app(state)

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            # 404, not 403: a foreign slot must be indistinguishable from a
            # missing one (anti-enumeration). True reason lands in SEL.
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

        assert called == []
        denied = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0][1]["source"] == "app_isolation"
        assert denied[0][1]["operation"] == "slot_summary_generate"

    async def test_the_feature_being_off_is_409_summary_disabled(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, False)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        _seed_slot(state)
        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 409
            assert (await resp.json())["code"] == "summary_disabled"
        assert called == []

    async def test_a_pass_already_running_is_409_summary_in_flight(self, tmp_path, monkeypatch):
        """Reported separately from the generic failure because it is the one
        the panel can explain as "already working" rather than "could not"."""
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        slot = _seed_slot(state)
        slot._summary_in_flight = True
        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 409
            assert (await resp.json())["code"] == "summary_in_flight"
        assert called == []
        # The guard belongs to the pass that took it: a refused request must not
        # release another pass's guard on its way out.
        assert slot._summary_in_flight is True

    async def test_a_turn_in_progress_is_409_summary_turn_running(self, tmp_path, monkeypatch):
        """Distinct from the generic refusal because it is a wait-and-retry, not
        a failure: the generator's ``running`` gate would decline the pass anyway
        (it holds even under force), so saying so here keeps the panel from
        reporting a transient mid-turn state as "could not summarize" -- and
        keeps a partial transcript from being cached as the whole session."""
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        slot = _seed_slot(state)
        # `running` is a property over the turn task; stage liveness the way
        # production does, with a task that has not finished.
        slot.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        assert slot.running is True

        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 409
            assert (await resp.json())["code"] == "summary_turn_running"
        assert called == []
        assert state.conversation_log.get_cached_intent_summary(slot_history_key(slot)) is None

    async def test_too_short_a_session_is_409_summary_unavailable(self, tmp_path, monkeypatch):
        """The turn minimum is not lifted by consent, so the click is refused
        with a code rather than spending a call to discover there is no intent
        structure."""
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        _seed_slot(state, user_turns=1)
        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 409
            assert (await resp.json())["code"] == "summary_unavailable"
        assert called == []

    async def test_a_successful_pass_returns_the_panel_payload(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        slot = _seed_slot(state)
        # No stop reason: the idle restored slot this route exists for. force
        # lifts that gate, so the pass must still run.
        assert slot._last_stop_reason == ""

        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 200
            body = await resp.json()

        assert len(called) == 1
        assert body["enabled"] is True
        assert body["stale"] is False
        assert body["intents"][0]["title"] == "set up auth"
        assert body["intents"][0]["state"] == "needs-you"
        assert body["constraints"] == ["restart the worker after a config change"]
        assert body["generated_at"] > 0
        assert body["user_turns"] == 3
        assert body["generate_state"] == "ready"

    async def test_an_existing_current_summary_is_returned_without_a_second_call(
        self, tmp_path, monkeypatch
    ):
        """A forced pass over an unchanged transcript is free, and the endpoint
        reads the sidecar back rather than trusting the pass's return value --
        "produced nothing" and "already current" are opposite outcomes."""
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        slot = _seed_slot(state)
        log = state.conversation_log
        hkey = slot_history_key(slot)
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 200
            body = await resp.json()

        assert called == []
        assert body["intents"][0]["title"] == "set up auth"

    async def test_an_unusable_reply_is_409_not_a_500(self, tmp_path, monkeypatch):
        """Generation is best-effort throughout; a garbage reply leaves no
        sidecar, so the panel is told it could not summarize."""
        _pin_flag(monkeypatch, True)
        _stub_generation(monkeypatch, reply="I'm afraid I can't do that")
        state = _make_state(tmp_path)
        _seed_slot(state)
        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 409
            assert (await resp.json())["code"] == "summary_unavailable"

    async def test_an_incognito_slot_cannot_be_summarized_on_demand(self, tmp_path, monkeypatch):
        """Consent to spend tokens is not consent to persist: the sidecar would
        outlive the transcript it describes."""
        _pin_flag(monkeypatch, True)
        called = _stub_generation(monkeypatch)
        state = _make_state(tmp_path)
        slot = _seed_slot(state, memory_mode="incognito")
        async with TestClient(TestServer(_make_generate_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert resp.status == 409
            assert (await resp.json())["code"] == "summary_unavailable"
        assert called == []
        assert state.conversation_log.get_cached_intent_summary(slot_history_key(slot)) is None


class TestPanelGateDrift:
    """The panel's affordance and the generator's gate are two implementations of
    one rule, kept separate on purpose: ``_generate_state`` answers on every panel
    mount from the slot's in-memory window, while ``_should_summarize`` is
    authoritative and needs the on-disk turn count. Deriving one from the other
    would put a transcript read on every session switch.

    The cost of that separation is drift: a gate added to the generator and not
    mirrored here shows up as a button that appears and then refuses. These tests
    are what make the separation safe — the first fails when a NEW gate is added
    without classifying it, so the omission surfaces here rather than as a failed
    click on someone's session.
    """

    # Every reason `_should_summarize` can return must fall in exactly one bucket.
    _MIRRORED = {"disabled", "in_flight", "memory_mode", "too_few_turns"}
    _FORCE_LIFTED = {"stop_reason", "cadence"}
    _PANEL_OWNED = {"running"}

    async def test_every_generator_gate_is_classified(self):
        """Fails when a gate is added to `_should_summarize` and nowhere else.

        Reads the reasons out of the function's own source rather than restating
        them, because a hand-maintained list would go stale in exactly the case
        this test exists to catch.
        """
        src = inspect.getsource(chat_summary._should_summarize)
        # Plain `return "x"` plus the one f-string form, `f"stop_reason:{...}"`.
        reasons = set(re.findall(r'return f?"([a-z_]+)', src)) - {""}
        classified = self._MIRRORED | self._FORCE_LIFTED | self._PANEL_OWNED
        unclassified = reasons - classified
        assert not unclassified, (
            f"new generator gate(s) {sorted(unclassified)} are not classified. Add each to "
            "_MIRRORED (the panel must also refuse), _FORCE_LIFTED (a forced pass ignores "
            "it), or _PANEL_OWNED (the panel decides it client-side) — and mirror it in "
            "_generate_state if it belongs in the first bucket."
        )
        # And the buckets must not name a gate that no longer exists, which would
        # let a real omission hide behind a stale entry.
        assert not classified - reasons - {"stop_reason"}, (
            f"classified reason(s) {sorted(classified - reasons - {'stop_reason'})} are no "
            "longer returned by _should_summarize — drop them from the buckets."
        )

    async def test_each_mirrored_gate_refuses_on_both_sides(self):
        """The panel never offers a button for a session the generator refuses."""
        cases: list[tuple[str, KiroCrewConfig, _ChatSlot]] = []

        def _slot(**attrs: object) -> _ChatSlot:
            slot = _ChatSlot("s1")
            slot.messages = [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "two"},
            ]
            slot._last_stop_reason = "end_turn"
            for name, value in attrs.items():
                setattr(slot, name, value)
            return slot

        def _cfg(enabled: bool = True) -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            cfg.session_summary = SessionSummaryConfig(enabled=enabled, min_user_turns=2)
            return cfg

        cases.append(("disabled", _cfg(enabled=False), _slot()))
        cases.append(("in_flight", _cfg(), _slot(_summary_in_flight=True)))
        cases.append(("memory_mode", _cfg(), _slot(memory_mode="incognito")))
        short = _slot()
        short.messages = [{"role": "user", "content": "only one"}]
        cases.append(("too_few_turns", _cfg(), short))

        for reason, cfg, slot in cases:
            panel = chat_handlers._generate_state(cfg, slot)
            turns = count_user_turns_in_records(slot.messages)
            gate = chat_summary._should_summarize(cfg, slot, turns, force=True)
            assert panel != "ready", f"{reason}: panel offered a button"
            assert gate == reason, f"{reason}: generator returned {gate!r}"

    async def test_a_ready_panel_state_is_not_refused_for_a_mirrored_reason(self):
        """The converse: an offered button must not fail for a shared reason.

        `running` is excluded deliberately — the panel disables the button from its
        own live turn state, so the generator legitimately refuses a slot this
        function still reports `ready`.
        """
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "two"},
        ]
        slot._last_stop_reason = "end_turn"

        assert chat_handlers._generate_state(cfg, slot) == "ready"
        gate = chat_summary._should_summarize(
            cfg, slot, count_user_turns_in_records(slot.messages), force=True
        )
        assert gate not in self._MIRRORED, f"ready panel state refused as {gate!r}"


class TestGenerateState:
    """``_generate_state`` — which affordance the panel offers.

    Three values because the panel has three honest things to say: a bool could
    only carry two, and collapsing "unavailable" into "too few messages" would
    print a reason that is untrue for an incognito session.
    """

    async def test_ready_for_a_long_enough_session(self):
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "two"},
        ]
        assert chat_handlers._generate_state(cfg, slot) == "ready"

    async def test_too_few_turns_below_the_minimum(self):
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        slot = _ChatSlot("s1")
        slot.messages = [{"role": "user", "content": "only one"}]
        assert chat_handlers._generate_state(cfg, slot) == "too_few_turns"

    async def test_injected_rows_do_not_make_a_session_look_long_enough(self):
        """Automation posts under role "user"; counting it would offer a button
        the generator then refuses."""
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "real ask"},
            {"role": "user", "content": "[Subagent completion event] done"},
            {"role": "user", "content": '[Cron notification from "x"] fired'},
        ]
        assert chat_handlers._generate_state(cfg, slot) == "too_few_turns"

    async def test_unavailable_when_the_feature_is_off(self):
        cfg = KiroCrewConfig()  # enabled defaults to False
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
        ]
        assert chat_handlers._generate_state(cfg, slot) == "unavailable"

    async def test_unavailable_while_a_pass_is_in_flight(self):
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
        ]
        slot._summary_in_flight = True
        assert chat_handlers._generate_state(cfg, slot) == "unavailable"

    async def test_unavailable_for_an_incognito_or_temporary_slot(self):
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        for mode in ("incognito", "Incognito", "temporary"):
            slot = _ChatSlot("s1")
            slot.memory_mode = mode
            slot.messages = [
                {"role": "user", "content": "one"},
                {"role": "user", "content": "two"},
            ]
            assert chat_handlers._generate_state(cfg, slot) == "unavailable"

    async def test_a_turn_in_flight_is_still_reported_ready(self):
        """Mid-turn is deliberately NOT a value of this field.

        The field is only refreshed when a summary is WRITTEN, so a verdict that
        begins and ends mid-turn would arrive stale and stick: a turn that ends
        without producing a summary (stopped, or gated by cadence) pushes no
        event, and the panel would sit on a dead "unavailable" until it
        remounted. The panel already holds a live per-slot turn signal and owns
        that presentation; the server-side refusals are the generator's
        ``running`` gate and the POST's 409 ``summary_turn_running``, both
        asserted elsewhere in this file.
        """
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True, min_user_turns=2)
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "two"},
        ]
        # `running` is a property over the turn task, so liveness is staged the
        # way production makes it true: a task that has not finished.
        slot.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        assert slot.running is True
        assert chat_handlers._generate_state(cfg, slot) == "ready"


class TestGetReportsGenerateState:
    """The GET carries the affordance state so the panel can render the button
    without a second request."""

    async def test_ready_is_reported_for_a_long_enough_session(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot.messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "two"},
        ]
        state._slots[slot.key] = slot
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["generate_state"] == "ready"

    async def test_too_few_turns_is_reported_for_a_short_session(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")  # no messages at all
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["generate_state"] == "too_few_turns"

    async def test_unavailable_is_reported_when_the_feature_is_off(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, False)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["enabled"] is False
        assert body["generate_state"] == "unavailable"

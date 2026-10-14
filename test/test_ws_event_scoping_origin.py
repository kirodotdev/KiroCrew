"""Per-app WebSocket event scope: a slot's origin decides whether it reads as the user's.

A cron- or system-initiated slot keeps its origin across a restart, the auth middleware sets
the dashboard-user flag positively, and a slot with a missing or undeclared origin fails
closed -- on the read side and on the write side -- instead of reading as USER.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import ws_event_scoping_helpers
from ws_event_scoping_helpers import _allowed, _make_state

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.ws_event_scope import ws_event_allowed

# The shared fixtures, bound here by name so pytest registers them for this module.
_clear_module_caches = ws_event_scoping_helpers._clear_module_caches
_clear_ws_scope_module_caches = ws_event_scoping_helpers._clear_ws_scope_module_caches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat_handler_source() -> str:
    """chat_handlers.py and the chat_api owners it composes, as one text."""
    import kiro_crew.dashboard.chat_handlers as _ch

    facade = Path(_ch.__file__)
    owners = sorted((facade.parent / "chat_api").glob("[!_]*.py"))
    assert owners, "no chat_api owner found beside chat_handlers.py"
    return "\n".join(path.read_text(encoding="utf-8") for path in [facade, *owners])


def _state_source() -> str:
    """state.py and the state_parts owners it composes, as one text."""
    import kiro_crew.dashboard.state as _st

    facade = Path(_st.__file__)
    owners = sorted((facade.parent / "state_parts").glob("[!_]*.py"))
    assert owners, "no state_parts owner found beside state.py"
    return "\n".join(path.read_text(encoding="utf-8") for path in [facade, *owners])


# ---------------------------------------------------------------------------
# _origin persistence — cron/system slots must survive a restart with the
# correct origin so the WS scope gate doesn't mis-classify them.
# ---------------------------------------------------------------------------


class TestOriginPersistence:
    """A cron- or system-initiated slot rehydrated from disk must keep its
    original origin so ``slots:user`` scopes don't inadvertently grant an app
    visibility into events it wasn't authorised for.

    These drive the REAL save/restore functions against a REAL
    ``ConversationLog``. An earlier version of this class rebuilt the
    ``meta_line`` assembly inside the test and asserted on its own local dict —
    so it stayed green while ``_save_slot_to_history`` never wrote ``origin``
    at all, and every slot silently rehydrated unattributed. Never restate the
    production logic here; call it.
    """

    @staticmethod
    def _state(tmp_path):
        from unittest.mock import AsyncMock

        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.recycle_background = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        return state

    def test_slot_has_origin_attribute(self):
        from kiro_crew.dashboard.state import _ChatSlot

        assert hasattr(_ChatSlot("s1"), "_origin")

    def test_to_dict_includes_origin(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._origin = SlotOrigin.CRON
        assert slot.to_dict()["origin"] == SlotOrigin.CRON

    def test_save_writes_origin_to_meta(self, tmp_path, monkeypatch):
        """The REAL save path must persist origin — this is the write side that
        was missing, letting every restored slot come back unattributed."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("cron-1")
        slot._origin = SlotOrigin.CRON
        slot.append("user", "hi")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log.get_metadata("dashboard:cron-1")
        assert meta.get("origin") == SlotOrigin.CRON

    def test_untagged_origin_not_persisted(self, tmp_path, monkeypatch):
        """The fail-closed empty sentinel must not be written as a real value."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._origin = ""
        slot.append("user", "hi")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        assert "origin" not in state.conversation_log.get_metadata("dashboard:s1")

    @pytest.mark.parametrize("origin", [SlotOrigin.CRON, SlotOrigin.USER, SlotOrigin.APP])
    def test_origin_round_trips_through_rehydrate(self, tmp_path, monkeypatch, origin):
        """save -> _rehydrate_slot_from_history must return the SAME origin."""
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._origin = origin
        if origin == SlotOrigin.APP:
            slot._app = "mochi-pet"
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._origin == origin

    def test_origin_round_trips_through_bulk_restore(self, tmp_path, monkeypatch):
        """The bulk restore path (gateway boot) must restore origin too — the
        two restore sites are separate code and both read ``meta["origin"]``."""
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            restore_recent_sessions,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("cron-1")
        slot._origin = SlotOrigin.CRON
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["cron-1"]

        restore_recent_sessions(state)

        assert state._slots["cron-1"]._origin == SlotOrigin.CRON

    def test_restored_cron_slot_still_hidden_from_slots_user(self, tmp_path, monkeypatch):
        """The security property the persistence exists for: a CRON slot that
        survived a restart must STILL be withheld from a ``slots:user`` app.
        Without the write side it rehydrated untagged and this gate flipped."""
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("cron-1")
        slot._origin = SlotOrigin.CRON
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["cron-1"]
        restored = _rehydrate_slot_from_history(state, "cron-1")
        assert restored is not None

        assert not ws_event_allowed(
            "chat_message",
            {"slot": "cron-1"},
            app="some-app",
            allowed_events=frozenset({"slots:user"}),
            state=state,
        )


# ---------------------------------------------------------------------------
# Positive is_dashboard_user flag from token_auth — a token that does not
# come through the auth middleware (or fails it) MUST NOT bypass the gate.
# ---------------------------------------------------------------------------


class TestAuthMiddlewareIsDashboardUser:
    """The auth middleware sets ``request["is_dashboard_user"]`` positively
    only for verified dashboard-user tokens.  App tokens and unauthenticated
    requests never see the flag flip to True.
    """

    def test_dashboard_user_bypasses_scope_gate(self):
        # Behavioral: a ws with positive _is_dashboard_user flag must bypass
        # the scope gate entirely (that's the CWE-269 fix's whole point).
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = MagicMock()
        ws.closed = False
        ws.get.side_effect = lambda k, default=None: {
            "_is_dashboard_user": True,
        }.get(k, default)
        # Even an unknown event that would deny app tokens passes for
        # dashboard users.
        assert state._ws_client_allowed(ws, "totally_unknown_event", {}) is True

    def test_missing_flag_treats_as_app_token(self):
        # Behavioral: without the positive flag, gate applies (deny-by-default).
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = MagicMock()
        ws.closed = False
        ws.get.side_effect = lambda k, default=None: {
            # Deliberately missing _is_dashboard_user.
            "_app": "mochi-pet",
            "_allowed_events": frozenset(),
        }.get(k, default)
        assert state._ws_client_allowed(ws, "totally_unknown_event", {}) is False


# ---------------------------------------------------------------------------
# Missing _origin fails closed — a slot without the attribute must not be
# treated as USER by default.
# ---------------------------------------------------------------------------


class TestMissingOriginFailsClosed:
    """CWE-269 defense-in-depth: if ``_origin`` is somehow missing (pre-
    migration slot, race, bug), it must remain invisible to any ``:user``
    scope rather than being silently classified as ``SlotOrigin.USER``.
    """

    def _slot_without_origin(self, owner_app: str = "") -> MagicMock:
        # Deliberately DO NOT set _origin on the mock — spec=[] means
        # hasattr(slot, "_origin") is False.
        slot = MagicMock(spec=["_app"])
        slot._app = owner_app
        return slot

    def test_slot_visible_denied_when_origin_missing(self):
        slot = self._slot_without_origin()
        state = _make_state({"s1": slot})
        # slots:user must NOT grant visibility to a slot with no _origin.
        assert (
            ws_event_allowed(
                "chat_chunk",
                {"slot": "s1"},
                app="mochi-pet",
                allowed_events=_allowed("slots:user"),
                state=state,
            )
            is False
        )

    def test_subagent_visible_denied_when_origin_missing(self):
        slot = self._slot_without_origin()
        state = _make_state({"s1": slot})
        assert (
            ws_event_allowed(
                "subagent_chunk",
                {"slot": "s1"},
                app="mochi-pet",
                allowed_events=_allowed("subagent:user"),
                state=state,
            )
            is False
        )


# ---------------------------------------------------------------------------
# Write-side origin: an undeclared slot must NOT be called USER
# ---------------------------------------------------------------------------


class TestUntaggedOriginIsNotUser:
    """The read side already failed closed on a missing ``_origin``; the WRITE
    side did not. ``origin or (APP if app else USER)`` labelled every untagged
    caller USER -- cron result injection, workflow inject, Slack, the
    OpenAI-compatible endpoint -- and an app holding ``slots:user`` received
    that private content."""

    def test_untagged_non_app_slot_is_not_user(self):
        from kiro_crew.dashboard.state import SlotOrigin, request_slot_origin

        src = _state_source()
        # The fail-open derivation must be gone from the CREATION path. It
        # still exists once, inside request_slot_origin -- that is the point:
        # the derivation moved to the layer that can actually make it.
        assert (
            "slot._origin = origin or (SlotOrigin.APP if app else SlotOrigin.USER)" not in src
        ), "get_or_create_slot must not infer USER; it cannot know"
        assert 'slot._origin = origin or (SlotOrigin.APP if app else "")' in src
        assert (
            src.count("SlotOrigin.APP if app else SlotOrigin.USER") == 1
        ), "the USER derivation belongs only in request_slot_origin"

        # USER is decided by the layer that knows: did a request carry a token?
        assert request_slot_origin("") == SlotOrigin.USER
        assert request_slot_origin("some-app") == SlotOrigin.APP

    def test_every_handler_slot_creation_declares_an_origin(self):
        """No slot-creation path in the handlers may fall through to the default.

        Two legitimate sources, and the distinction matters: a NEW slot's origin
        comes from the request (an app token means APP, its absence means the
        dashboard user), while RESUMING a persisted conversation must take the
        origin that conversation was stored with -- deriving it from the resumer
        relabels a cron slot as USER. Counting both together is the invariant;
        pinning one of them everywhere is what got the resume path wrong.
        """
        src = _chat_handler_source()
        creates = src.count("state.get_or_create_slot(")
        from_request = src.count("origin=request_slot_origin(")
        # A resume constructs from the metadata codec's keywords, whose ``origin``
        # is the persisted conversation's (pinned behaviourally below, by
        # ``test_every_restore_path_keeps_the_persisted_origin``).
        from_persisted = src.count('origin=str(meta.get("origin", ""))') + src.count(
            "**metadata_codec.slot_args("
        )
        assert creates >= 3, f"the scan found only {creates} slot creations"
        assert creates == from_request + from_persisted, (
            f"{creates} slot creations but only {from_request} declare a "
            f"request-derived origin and {from_persisted} a persisted one"
        )

    def test_resume_takes_the_persisted_origin_not_the_resumer(self):
        """The resume endpoint must read metadata BEFORE it creates the slot.

        Creating the slot from the request identity and reading the history
        metadata a dozen lines later relabels a resumed cron conversation as
        USER, and `slots:user` then hands its replayed content to any app holding
        that scope. Resume creates its slot by calling
        ``_materialise_slot_from_history`` (which owns the ``get_or_create_slot``
        and takes the persisted origin through ``metadata_codec.slot_args``), so
        the invariant is that resume reads ``get_metadata`` before that call.
        Anchoring on the call site rather than the origin spelling keeps this
        correct while the creation lives in the shared helper: the persisted
        origin itself is pinned by ``test_every_restore_path_keeps_the_persisted_origin``.
        """
        src = _chat_handler_source()
        # Search from the resume handler's definition so the helper (defined
        # earlier in the file) is not what the indices land on.
        resume_def = src.index("async def api_chat_slot_resume(")
        meta_read = src.index("meta = state.conversation_log.get_metadata(history_key)", resume_def)
        resume_create = src.index("_materialise_slot_from_history(", resume_def)
        assert meta_read < resume_create, (
            "the resume path must read the persisted metadata before it calls "
            "_materialise_slot_from_history, or the origin that call passes cannot "
            "come from that metadata"
        )

    def test_cron_injection_declares_cron(self):
        import kiro_crew.dashboard.cron_inject as _ci

        assert "origin=SlotOrigin.CRON" in Path(_ci.__file__).read_text(
            encoding="utf-8"
        ), "a cron result is the job's output, never something the user typed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", [SlotOrigin.CRON, SlotOrigin.USER, ""])
    async def test_every_restore_path_keeps_the_persisted_origin(
        self, stored, tmp_path, monkeypatch
    ):
        """Re-deriving on restart or resume would relabel a cron slot USER (leak) and
        a real user slot untagged (dropping a grant an app legitimately holds)."""
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_handlers as ch
        from kiro_crew.dashboard import chat_persistence as cp

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        meta = {"created_at": "2026-01-01T00:00:00", "origin": stored}
        rows = [{"role": "user", "content": "hi", "ts": "2026-01-01T00:00:01"}]
        for name in ("open-tab", "resumed"):
            lines = [{"_type": "metadata", **meta}, *rows]
            (tmp_path / f"dashboard_{name}.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
            )
        restored = cp._rehydrate_slot_from_history(state, "open-tab")
        cp._apply_recent_session(
            state,
            "dashboard_recent",
            "recent",
            {"title": "t"},
            dict(meta),
            list(rows),
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=None,
        )
        outcome = await ch.resume_slot_from_history(
            state, name="resumed", history_key="dashboard:resumed"
        )
        assert outcome.refusal is None
        origins = [restored._origin, state._slots["recent"]._origin, outcome.slot._origin]
        assert origins == [stored] * 3

"""A session's own turn time limit.

Pinned where each defect would live:

- **Resolution** (``turn_dispatch.resolve_turn_limit``): a session's own limit
  beats ``agent.chat_turn_timeout_secs`` for that session's turns only, a
  cleared limit falls back to the global, and a value that is not an integer
  (the metadata line is agent-writable) is ignored.
- **Dispatch**: ``spawn_guarded_turn`` and ``bounded_chat_turn`` bound the turn
  at the session's limit and the timeout card names which limit applied.
- **Transport**: the limit a turn runs under reaches the ACP prompt wait, so a
  session limit above the transport's default is one the system honours.
- **HTTP endpoint** (``/api/chat/slots/{slot}/turn-timeout``): the global
  setting's range and unit, null clears, and the value is persisted before the
  slot runs under it.
- **Restore**: the metadata codec and the channel surfacing path read it back.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp import client as acp_client
from kiro_crew.config.loader import CHAT_TURN_TIMEOUT_MAX, CHAT_TURN_TIMEOUT_MIN
from kiro_crew.dashboard import turn_dispatch as td
from kiro_crew.dashboard.chat import api_chat_slot_turn_timeout
from kiro_crew.dashboard.chat_persistence import _validate_turn_timeout_secs
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.session_surface import set_dashboard_surfaced
from kiro_crew.turn_limit import TURN_LIMIT_SECS, current_turn_limit_secs


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The slots these tests open publish to the process-global dashboard-surface
    registry; reset it so their keys never leak into other tests."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


@contextlib.contextmanager
def _running_turn_limit(secs: float) -> Iterator[None]:
    """Publish *secs* as the running turn's limit, restoring the previous value."""
    previous = TURN_LIMIT_SECS.get()
    TURN_LIMIT_SECS.set(secs)
    try:
        yield
    finally:
        TURN_LIMIT_SECS.set(previous)


def _slot(secs: object = None) -> MagicMock:
    slot = MagicMock()
    slot.key = "chat-1-123"
    slot.turn_timeout_secs = secs
    return slot


def _state() -> MagicMock:
    state = MagicMock()
    state._background_tasks = set()
    return state


def _global_limit(monkeypatch, secs: float) -> None:
    monkeypatch.setattr(td, "chat_turn_timeout_secs", lambda: secs)


def _transport_config(monkeypatch, *, turn: int = 14400, subagent: int = 600) -> None:
    """Give ``resolve_prompt_timeout`` a known config without touching disk."""
    from kiro_crew.config import loader

    cfg = MagicMock()
    cfg.agent.chat_turn_timeout_secs = turn
    cfg.agent.subagent_timeout_secs = subagent
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))


class TestResolution:
    def test_a_session_limit_beats_the_global(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        assert td.resolve_turn_limit(_slot(28800)) == (28800.0, td.TURN_LIMIT_SESSION)

    def test_a_lower_session_limit_beats_the_global_too(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        assert td.resolve_turn_limit(_slot(600)) == (600.0, td.TURN_LIMIT_SESSION)

    def test_a_cleared_limit_falls_back_to_the_global(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 7200.0)
        assert td.resolve_turn_limit(_slot(None)) == (7200.0, td.TURN_LIMIT_GLOBAL)

    def test_no_slot_follows_the_global(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 7200.0)
        assert td.resolve_turn_limit() == (7200.0, td.TURN_LIMIT_GLOBAL)

    @pytest.mark.parametrize("raw", [True, 3600.0, "3600", [3600], MagicMock()])
    def test_a_value_that_is_not_an_integer_is_ignored(self, monkeypatch, raw: object) -> None:
        _global_limit(monkeypatch, 7200.0)
        assert td.session_turn_timeout_secs(_slot(raw)) is None
        assert td.resolve_turn_limit(_slot(raw)) == (7200.0, td.TURN_LIMIT_GLOBAL)

    def test_an_out_of_range_integer_is_held_to_the_global_range(self) -> None:
        assert td.session_turn_timeout_secs(_slot(1)) == CHAT_TURN_TIMEOUT_MIN
        assert td.session_turn_timeout_secs(_slot(10**9)) == CHAT_TURN_TIMEOUT_MAX

    def test_a_session_limit_is_not_clamped_to_the_transport_default(self, monkeypatch) -> None:
        """The transport follows a session limit, so the clamp the global value
        gets against the transport's own wait must not cut it."""
        monkeypatch.setattr(td, "_acp_prompt_ceiling", lambda: 14400.0)
        assert td.resolve_turn_limit(_slot(CHAT_TURN_TIMEOUT_MAX))[0] == CHAT_TURN_TIMEOUT_MAX


class TestDispatch:
    @pytest.mark.asyncio
    async def test_spawned_turn_runs_under_the_session_limit(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        monkeypatch.setattr(td, "native_cancel_ack_timeout_secs", lambda: 0.1)
        seen: list[float | None] = []

        async def _turn() -> None:
            seen.append(current_turn_limit_secs())

        await td.spawn_guarded_turn(_state(), _slot(28800), _turn())
        assert seen == [28800.0]

    @pytest.mark.asyncio
    async def test_bounded_turn_runs_under_the_session_limit(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        seen: list[float | None] = []

        async def _turn() -> None:
            seen.append(current_turn_limit_secs())

        await td.bounded_chat_turn(_turn(), state=_state(), slot=_slot(900))
        assert seen == [900.0]

    @pytest.mark.asyncio
    async def test_the_published_limit_does_not_outlive_the_turn(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)

        async def _turn() -> None:
            return None

        await td.bounded_chat_turn(_turn(), state=_state(), slot=_slot(900))
        assert TURN_LIMIT_SECS.get() is None

    @pytest.mark.asyncio
    async def test_the_card_names_the_session_limit(self, monkeypatch) -> None:
        state, slot = _state(), _slot(28800)
        monkeypatch.setattr(td, "native_cancel_ack_timeout_secs", lambda: 0.1)
        monkeypatch.setattr(td, "session_turn_timeout_secs", lambda s: 0.01)

        async def _forever() -> None:
            await asyncio.Event().wait()

        task = td.spawn_guarded_turn(state, slot, _forever())
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await task
        await asyncio.sleep(0)
        errors = [c for c in slot.append.call_args_list if c.args[0] == "error"]
        assert len(errors) == 1
        assert errors[0].kwargs["meta"] == {
            "kind": td.TURN_TIMEOUT_KIND,
            "limit_scope": td.TURN_LIMIT_SESSION,
        }
        assert td.SESSION_TURN_LIMIT_MENU in errors[0].args[1]

    def test_session_card_points_at_the_session_menu_not_settings(self) -> None:
        card = td.format_turn_timeout_card(28800.0, limit_scope=td.TURN_LIMIT_SESSION)
        assert "8-hour" in card
        assert td.SESSION_TURN_LIMIT_MENU in card
        # The card's own picker comes first: the user is looking at it.
        assert card.index(td.CARD_TURN_LIMIT_BUTTON) < card.index(
            td.SESSION_TURN_LIMIT_MENU + " in the session menu"
        )
        assert "Settings → Chat → Advanced" not in card

    def test_global_card_points_at_settings_and_names_the_session_option(self) -> None:
        card = td.format_turn_timeout_card(14400.0)
        assert "Settings → Chat → Advanced" in card
        assert "session menu" in card

    @pytest.mark.asyncio
    async def test_approval_window_sizes_against_the_running_turn_limit(self, monkeypatch) -> None:
        """A session limit below the global leaves the approval window inside it."""
        _global_limit(monkeypatch, 14400.0)
        holder = MagicMock()
        holder.load.return_value.agent.tool_approval_timeout_secs = 7200
        monkeypatch.setattr(td, "KiroCrewConfig", holder)
        with _running_turn_limit(900.0):
            window = td.tool_approval_timeout_secs()
        assert window <= 900.0 - td.APPROVAL_TURN_MARGIN_SECS


class TestTransportFollowsTheTurnLimit:
    def test_outside_a_turn_the_transport_keeps_its_default(self, monkeypatch) -> None:
        _transport_config(monkeypatch)
        assert acp_client.resolve_prompt_timeout() == acp_client._DEFAULT_PROMPT_TIMEOUT

    def test_a_turn_limit_above_the_default_lengthens_the_wait(self, monkeypatch) -> None:
        _transport_config(monkeypatch)
        with _running_turn_limit(28800.0):
            wait = acp_client.resolve_prompt_timeout()
        assert wait == 28800.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS

    def test_a_turn_limit_below_the_default_never_shortens_it(self, monkeypatch) -> None:
        _transport_config(monkeypatch)
        with _running_turn_limit(600.0):
            wait = acp_client.resolve_prompt_timeout()
        assert wait == acp_client._DEFAULT_PROMPT_TIMEOUT

    def test_unreadable_config_still_honours_the_turn_limit(self, monkeypatch) -> None:
        from kiro_crew.config import loader

        def _boom(cls):  # type: ignore[no-untyped-def]
            raise RuntimeError("no config here")

        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(_boom))
        with _running_turn_limit(28800.0):
            wait = acp_client.resolve_prompt_timeout()
        assert wait == 28800.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS

    @pytest.mark.asyncio
    async def test_the_prompt_wait_inside_a_session_turn_follows_its_limit(
        self, monkeypatch
    ) -> None:
        """End to end through the off-loop resolution every prompt uses."""
        _transport_config(monkeypatch)
        _global_limit(monkeypatch, 14400.0)
        waits: list[float] = []

        async def _turn() -> None:
            waits.append(await acp_client._effective_prompt_timeout_async(None))

        await td.bounded_chat_turn(_turn(), state=_state(), slot=_slot(43200))
        assert waits == [43200.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS]


class TestStagedSave:
    """The endpoint's staged value is what a save writes, whatever the slot holds."""

    def _slot(self, tmp_path, monkeypatch):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        return state.get_or_create_slot("staged")

    def test_a_staged_limit_reaches_both_save_forms(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        slot = self._slot(tmp_path, monkeypatch)
        folds = codec.SaveFolds(memory_mode="persistent", turn_timeout_secs=(28800,))
        assert codec.encode(slot, folds=folds)["turn_timeout_secs"] == 28800
        assert codec.encode(slot, merge=True, folds=folds)["turn_timeout_secs"] == 28800
        assert slot.turn_timeout_secs is None

    def test_a_staged_clear_overrides_the_live_limit(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        slot = self._slot(tmp_path, monkeypatch)
        slot.turn_timeout_secs = 28800
        folds = codec.SaveFolds(memory_mode="persistent", turn_timeout_secs=(None,))
        assert "turn_timeout_secs" not in codec.encode(slot, folds=folds)
        assert codec.encode(slot, merge=True, folds=folds)["turn_timeout_secs"] is None

    def test_nothing_staged_writes_the_live_limit(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        slot = self._slot(tmp_path, monkeypatch)
        slot.turn_timeout_secs = 7200
        folds = codec.SaveFolds(memory_mode="persistent")
        assert codec.encode(slot, folds=folds)["turn_timeout_secs"] == 7200
        assert codec.encode(slot, merge=True, folds=folds)["turn_timeout_secs"] == 7200


def _make_app(state: DashboardState, request_app: str | None = None) -> web.Application:
    app = web.Application()
    app["state"] = state
    if request_app is not None:

        @web.middleware
        async def _tag_app(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(_tag_app)
    app.router.add_get("/api/chat/slots/{slot}/turn-timeout", api_chat_slot_turn_timeout)
    app.router.add_post("/api/chat/slots/{slot}/turn-timeout", api_chat_slot_turn_timeout)
    return app


def _mock_state(*slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot for slot in slots}
    state.sessions = MagicMock()
    state.conversation_log = MagicMock()
    return state


_SAVE = "kiro_crew.dashboard.chat_handlers.save_slot_off_loop"


async def _committing_save(*args, **kwargs):  # type: ignore[no-untyped-def]
    """A save whose write commits: it runs the commit hook, as the real one does."""
    kwargs["after_commit_under_lock"]()
    return True


_GLOBAL = "kiro_crew.dashboard.chat_handlers.chat_turn_timeout_secs"


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_get_reports_the_session_limit_the_global_and_the_range(self) -> None:
        slot = _ChatSlot("test")
        slot.turn_timeout_secs = 28800
        with patch(_GLOBAL, return_value=14400.0):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.get("/api/chat/slots/test/turn-timeout")
                assert resp.status == 200
                assert await resp.json() == {
                    "secs": 28800,
                    "global_secs": 14400,
                    "min": CHAT_TURN_TIMEOUT_MIN,
                    "max": CHAT_TURN_TIMEOUT_MAX,
                }

    @pytest.mark.asyncio
    async def test_post_persists_the_limit_before_the_slot_runs_under_it(self) -> None:
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        during_save: list[tuple[object, float]] = []

        async def _save(*args, **kwargs):  # type: ignore[no-untyped-def]
            # While the write is in flight the live field still holds the
            # committed value: a turn dispatched now runs under the old limit.
            during_save.append((slot.turn_timeout_secs, td.resolve_turn_limit(slot)[0]))
            kwargs["after_commit_under_lock"]()
            return True

        with (
            patch(_SAVE, new=AsyncMock(side_effect=_save)) as saver,
            patch(_GLOBAL, return_value=14400.0),
            patch.object(td, "chat_turn_timeout_secs", return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 28800})
                assert resp.status == 200
                data = await resp.json()
        assert data["ok"] is True and data["secs"] == 28800 and data["global_secs"] == 14400
        assert during_save == [(None, 14400.0)]
        assert slot.turn_timeout_secs == 28800
        # One confirmed save, pinned to the transcript the authorization
        # covered, carrying the new value as a staged fold.
        assert saver.await_count == 1
        call = saver.await_args_list[0]
        assert call.args == (state, slot)
        assert call.kwargs["turn_timeout_override"] == (28800,)
        assert {
            k: v
            for k, v in call.kwargs.items()
            if k not in ("turn_timeout_override", "after_commit_under_lock")
        } == {
            "force": True,
            "best_effort": False,
            "expected_history_key": slot_history_key(slot),
        }

    @pytest.mark.asyncio
    async def test_a_tab_with_no_record_yet_keeps_the_limit_in_memory(self) -> None:
        """No metadata line exists to be the authority: the save reports success
        without committing, and the slot's first full save writes the field."""
        slot = _ChatSlot("test")
        with (
            patch(_SAVE, new=AsyncMock(return_value=True)),
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 200
        assert slot.turn_timeout_secs == 7200

    @pytest.mark.asyncio
    async def test_post_null_returns_the_session_to_the_global(self) -> None:
        slot = _ChatSlot("test")
        slot.turn_timeout_secs = 28800
        with (
            patch(_SAVE, new=AsyncMock(side_effect=_committing_save)) as saver,
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": None})
                assert resp.status == 200
                assert (await resp.json())["secs"] is None
        assert slot.turn_timeout_secs is None
        assert saver.await_args.kwargs["turn_timeout_override"] == (None,)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("secs", "code"),
        [
            (CHAT_TURN_TIMEOUT_MIN - 1, "secs_out_of_range"),
            (CHAT_TURN_TIMEOUT_MAX + 1, "secs_out_of_range"),
            (3600.5, "secs_not_an_integer"),
            (3600.0, "secs_not_an_integer"),
            ("3600", "secs_not_an_integer"),
            (True, "secs_not_an_integer"),
            ([3600], "secs_not_an_integer"),
        ],
    )
    async def test_post_refuses_what_the_global_setting_would(
        self, secs: object, code: str
    ) -> None:
        slot = _ChatSlot("test")
        with patch(_SAVE, new=AsyncMock(return_value=True)) as saver:
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": secs})
                assert resp.status == 400
                assert (await resp.json())["code"] == code
        assert slot.turn_timeout_secs is None
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_without_secs_is_refused(self) -> None:
        slot = _ChatSlot("test")
        async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
            resp = await client.post("/api/chat/slots/test/turn-timeout", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "secs_required"

    @pytest.mark.asyncio
    async def test_unknown_slot_is_not_found(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state()))) as client:
            resp = await client.post("/api/chat/slots/nope/turn-timeout", json={"secs": 3600})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_a_failed_persist_leaves_the_limit_unchanged(self) -> None:
        slot = _ChatSlot("test")
        slot.turn_timeout_secs = 3600
        with patch(_SAVE, new=AsyncMock(side_effect=OSError("disk full"))):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 500
                assert (await resp.json())["code"] == "persist_failed"
        assert slot.turn_timeout_secs == 3600

    @pytest.mark.asyncio
    async def test_a_refused_save_leaves_the_limit_unchanged(self) -> None:
        slot = _ChatSlot("test")
        with patch(_SAVE, new=AsyncMock(return_value=False)):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 409
                assert (await resp.json())["code"] == "session_gone"
        assert slot.turn_timeout_secs is None

    @pytest.mark.asyncio
    async def test_a_failed_persist_never_reaches_alias_slots(self) -> None:
        slot = _ChatSlot("test")
        alias = _ChatSlot("alias")
        alias.turn_timeout_secs = 3600
        state = _mock_state(slot, alias)
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key", return_value="one-transcript"
            ),
            patch(_SAVE, new=AsyncMock(side_effect=OSError("disk full"))),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 500
        assert slot.turn_timeout_secs is None
        assert alias.turn_timeout_secs == 3600

    @pytest.mark.asyncio
    async def test_a_committed_limit_reaches_alias_slots_of_the_same_transcript(self) -> None:
        slot = _ChatSlot("test")
        alias = _ChatSlot("alias")
        state = _mock_state(slot, alias)
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key", return_value="one-transcript"
            ),
            patch(_SAVE, new=AsyncMock(side_effect=_committing_save)),
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 200
        assert slot.turn_timeout_secs == 7200
        assert alias.turn_timeout_secs == 7200

    @pytest.mark.asyncio
    async def test_an_alias_bound_while_the_save_runs_gets_the_committed_limit(self) -> None:
        # A second tab on the same transcript appears during the persist await,
        # holding the old limit; the commit reaches it, so its next save cannot
        # write the old value back.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        late = _ChatSlot("late")
        late.turn_timeout_secs = 3600

        async def _alias_appears_then_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
            state._slots[late.key] = late
            kwargs["after_commit_under_lock"]()
            return True

        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key", return_value="one-transcript"
            ),
            patch(_SAVE, new=AsyncMock(side_effect=_alias_appears_then_commit)),
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 200
        assert slot.turn_timeout_secs == 7200
        assert late.turn_timeout_secs == 7200
        assert late._turn_timeout_hydrated is True

    @pytest.mark.asyncio
    async def test_a_slot_rebound_during_the_persist_keeps_its_new_transcripts_limit(self) -> None:
        # The write commits, then the tab is bound to another transcript and that
        # transcript's limit is restored before the request resumes. The request
        # is refused, and the slot keeps the restored limit rather than the
        # value it held before the request.
        slot = _ChatSlot("test")
        slot.linked_session_key = "first"
        slot.turn_timeout_secs = 600
        state = _mock_state(slot)

        async def _commit_then_rebind(*args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["after_commit_under_lock"]()
            slot.linked_session_key = "second"
            slot.turn_timeout_secs = 28800
            slot._turn_timeout_hydrated = True
            return True

        with (
            patch(_SAVE, new=AsyncMock(side_effect=_commit_then_rebind)),
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 409
        assert slot.turn_timeout_secs == 28800
        assert slot._turn_timeout_hydrated is True

    @pytest.mark.asyncio
    async def test_the_commit_hook_does_not_publish_to_a_slot_already_rebound(self) -> None:
        slot = _ChatSlot("test")
        slot.linked_session_key = "first"
        state = _mock_state(slot)

        async def _rebind_then_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
            slot.linked_session_key = "second"
            slot.turn_timeout_secs = 28800
            kwargs["after_commit_under_lock"]()
            return True

        with (
            patch(_SAVE, new=AsyncMock(side_effect=_rebind_then_commit)),
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 409
        assert slot.turn_timeout_secs == 28800

    @pytest.mark.asyncio
    async def test_an_app_cannot_set_a_slot_it_does_not_own(self) -> None:
        slot = _ChatSlot("test")
        with patch(_SAVE, new=AsyncMock(return_value=True)) as saver:
            async with TestClient(
                TestServer(_make_app(_mock_state(slot), request_app="some-app"))
            ) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status in (403, 404)
        assert slot.turn_timeout_secs is None
        saver.assert_not_awaited()


class TestRestore:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (28800, 28800),
            (1, CHAT_TURN_TIMEOUT_MIN),
            (10**9, CHAT_TURN_TIMEOUT_MAX),
            (None, None),
            (True, None),
            (3600.0, None),
            ("3600", None),
        ],
    )
    def test_a_persisted_value_is_validated(self, raw: object, want: int | None) -> None:
        assert _validate_turn_timeout_secs(raw) == want

    def test_channel_surfacing_restores_the_limit(self, tmp_path, monkeypatch) -> None:
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
            {"key": "slack:1785370133.085471", "title": "t", "modified": 0.0},
            {"turn_timeout_secs": 28800},
            [{"role": "user", "content": "hi"}],
        )
        assert slot is not None
        assert slot.turn_timeout_secs == 28800


def _cron_job(job_id: str = "j1") -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.name = "nightly"
    job.agent_id = ""
    job.member_id = ""
    job.memory_store = ""
    job.persistent_session = True
    job.hide_in_chat = False
    return job


class TestCronReopen:
    """A cron tab closed with its own limit gets it back when it reopens."""

    def _closed_tab(self, tmp_path, monkeypatch, secs: int = 28800) -> DashboardState:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        slot = state.get_or_create_slot("cron-j1")
        slot.linked_session_key = "cron:j1"
        slot.turn_timeout_secs = secs
        slot.append("assistant", "run one", "")
        assert _save_slot_to_history(state, slot, force=True)
        state._slots.pop("cron-j1")
        assert state.conversation_log.get_metadata("cron:j1")["turn_timeout_secs"] == secs
        return state

    @pytest.mark.asyncio
    async def test_a_reopened_tab_runs_under_its_own_limit_again(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        state = self._closed_tab(tmp_path, monkeypatch)
        await ensure_cron_slot(state, _cron_job())
        slot = state.get_slot("cron-j1")
        assert slot is not None and slot.linked_session_key == "cron:j1"
        assert slot.turn_timeout_secs == 28800
        assert slot._turn_timeout_hydrated is True

    @pytest.mark.asyncio
    async def test_a_save_before_the_limit_is_read_keeps_it_on_disk(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.cron_inject import _bind_cron_slot, restore_cron_turn_timeout

        state = self._closed_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        assert log is not None
        slot = _bind_cron_slot(state, _cron_job(), log.read_messages("cron:j1"), [])
        assert slot is not None
        assert slot.turn_timeout_secs is None and slot._turn_timeout_hydrated is False
        slot.append("assistant", "run two", "")
        assert _save_slot_to_history(state, slot, force=True)
        assert log.get_metadata("cron:j1")["turn_timeout_secs"] == 28800
        await restore_cron_turn_timeout(state, "j1")
        assert slot.turn_timeout_secs == 28800

    @pytest.mark.asyncio
    async def test_an_unreadable_line_leaves_the_tab_carrying_the_limit(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import _bind_cron_slot, restore_cron_turn_timeout

        state = self._closed_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        assert log is not None
        slot = _bind_cron_slot(state, _cron_job(), log.read_messages("cron:j1"), [])
        assert slot is not None
        monkeypatch.setattr(log, "get_metadata_status", MagicMock(side_effect=OSError("disk")))
        await restore_cron_turn_timeout(state, "j1")
        assert slot.turn_timeout_secs is None and slot._turn_timeout_hydrated is False

    def test_the_save_fold_prefers_a_staged_value_then_the_slot_once_read(self) -> None:
        from kiro_crew.dashboard.slot_persistence.metadata_line import _turn_timeout_fold

        on_disk = {"turn_timeout_secs": 28800}
        slot = MagicMock(_turn_timeout_hydrated=False)
        assert _turn_timeout_fold(slot, None, on_disk) == (28800,)
        assert _turn_timeout_fold(slot, (3600,), on_disk) == (3600,)
        assert _turn_timeout_fold(slot, None, {"turn_timeout_secs": "x"}) == (None,)
        slot._turn_timeout_hydrated = True
        assert _turn_timeout_fold(slot, None, on_disk) is None

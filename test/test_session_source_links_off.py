"""The per-session PR and Issue chip switch (``_ChatSlot.source_links_off``).

``PATCH /api/chat/slots/{slot}/source-links-off`` shares its body with the two
mute toggles (``_toggle_slot_user_flag``), so the provenance and
persist-before-publish contracts pinned for those hold here too. These tests pin
what is specific to the chip switch: it empties the session's chips and skips the
transcript scan, a link mentioned later gets no chip, its chips feed no status
refresh, the staged value travels as ``source_links_off_override``, the route
publishes a full slots frame (``source_links`` cannot ride a one-row patch), and
the flag round-trips through the metadata line.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.source_providers as sp
from kiro_crew.dashboard.chat_folders import api_chat_slot_source_links_off
from kiro_crew.dashboard.slot_persistence import metadata_codec as mc
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history_schema import SLOT_OWNED_META_KEYS

PRS = [f"https://github.com/acme/widgets/pull/{n}" for n in (11, 12)]
ISSUE = "https://github.com/acme/widgets/issues/21"
LATER_PR = "https://github.com/acme/widgets/pull/99"


def _slot(key: str = "s1") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.append("assistant", "\n".join([*PRS, ISSUE]), ts="t1")
    return slot


def _dashboard(tmp_path, monkeypatch, *slots: _ChatSlot) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )
    state._slots = {slot.key: slot for slot in slots}
    return state


@pytest.fixture(autouse=True)
def _global_chips_on(_floor_monkeypatch):
    # The global switch lives in a module snapshot; pin it on so a sibling test
    # that turned it off cannot decide these.
    _floor_monkeypatch.setattr(sp, "_session_card_chips_snapshot", True)


class TestPayload:
    def test_off_empties_both_fields_and_skips_extraction(self) -> None:
        slot = _slot()
        assert len(slot.to_dict()["source_links"]) == 3
        slot.source_links_off = True
        with patch.object(
            _ChatSlot, "_pr_source_links", side_effect=AssertionError("extracted while off")
        ):
            payload = slot.to_dict()
        assert payload["source_links"] == []
        assert payload["source_links_total"] == 0
        assert payload["source_links_off"] is True

    def test_a_link_mentioned_while_off_gets_no_chip_and_on_brings_all_back(self) -> None:
        slot = _slot()
        slot.source_links_off = True
        slot.append("assistant", LATER_PR, ts="t2")
        assert slot.to_dict()["source_links"] == []

        slot.source_links_off = False
        urls = [link["url"] for link in slot.to_dict()["source_links"]]
        assert LATER_PR in urls
        assert slot.to_dict()["source_links_total"] == 4

    def test_only_the_switched_session_loses_its_chips(self, tmp_path, monkeypatch) -> None:
        on, off = _slot("on"), _slot("off")
        off.source_links_off = True
        state = _dashboard(tmp_path, monkeypatch, on, off)
        by_key = {p["key"]: p for p in state.serialize_slots()}
        assert len(by_key["on"]["source_links"]) == 3
        assert by_key["off"]["source_links"] == []


class TestStatusRefreshFeed:
    def test_no_url_of_a_switched_session_reaches_any_feed(self, tmp_path, monkeypatch) -> None:
        on, off = _slot("on"), _slot("off")
        off.append("assistant", LATER_PR, ts="t2")
        off.source_links_off = True
        state = _dashboard(tmp_path, monkeypatch, on, off)

        assert set(state.source_link_urls()) == set(PRS)
        assert state.source_link_urls_for_slot("off") == []
        # The idle tier: every slot here is idle, and the switched one adds nothing.
        assert LATER_PR not in state.idle_source_link_urls()

    def test_the_turn_boundary_refresh_spawns_no_provider_call(self, tmp_path, monkeypatch) -> None:
        slot = _slot()
        slot.source_links_off = True
        state = _dashboard(tmp_path, monkeypatch, slot)
        state._owner_ws_clients = {MagicMock()}
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.request_check_refresh_now"
        ) as refresh:
            state.refresh_slot_source_status("s1")
        refresh.assert_not_called()


def _make_app(state: MagicMock, *, internal_auth: bool = False):
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_identity(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        request["internal_auth"] = internal_auth
        request["is_dashboard_user"] = not internal_auth
        return await handler(request)

    app.middlewares.append(_publish_identity)
    app.router.add_patch("/api/chat/slots/{slot}/source-links-off", api_chat_slot_source_links_off)
    return app


def _fences():
    return (
        patch("kiro_crew.dashboard.chat_folders.refuse_unattributable_caller", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.member_slot_write_refused", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.deny_app_slot_access", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.app_owns_transcript", return_value=True),
        patch("kiro_crew.dashboard.chat_folders._effective_request_app", return_value=""),
    )


async def _patch(state: MagicMock, payload: object, save, **app_kwargs):
    f1, f2, f3, f4, f5 = _fences()
    with f1, f2, f3, f4, f5, patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", save):
        async with TestClient(TestServer(_make_app(state, **app_kwargs))) as client:
            resp = await client.patch("/api/chat/slots/chat-1-100/source-links-off", json=payload)
            return resp.status, await resp.json()


def _state(slot: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state._slots = {slot.key: slot}
    return state


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_an_agent_caller_is_refused_before_any_write(self) -> None:
        slot = _ChatSlot("chat-1-100")
        save = AsyncMock(return_value=True)
        status, body = await _patch(
            _state(slot), {"source_links_off": True}, save, internal_auth=True
        )
        assert status == 403
        assert body["code"] == "source_links_off_user_only"
        assert slot.source_links_off is False
        save.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_non_bool_is_400(self) -> None:
        slot = _ChatSlot("chat-1-100")
        status, body = await _patch(_state(slot), {"source_links_off": "true"}, AsyncMock())
        assert status == 400
        assert body["code"] == "source_links_off_not_bool"
        assert slot.source_links_off is False

    @pytest.mark.asyncio
    async def test_stages_the_value_flips_after_commit_and_pushes_a_full_frame(self) -> None:
        slot = _ChatSlot("chat-1-100")
        state = _state(slot)
        seen: dict[str, object] = {}

        async def _save(*args, **kwargs):
            seen["live_during_save"] = slot.source_links_off
            seen["source_links_off_override"] = kwargs.get("source_links_off_override")
            seen["muted_override"] = kwargs.get("muted_override")
            seen["mutes_opened_override"] = kwargs.get("mutes_opened_override")
            kwargs["after_commit_under_lock"]()
            return True

        status, body = await _patch(state, {"source_links_off": True}, _save)
        assert status == 200
        assert body == {"ok": True, "source_links_off": True, "changed": True}
        assert seen == {
            "live_during_save": False,
            "source_links_off_override": True,
            "muted_override": None,
            "mutes_opened_override": None,
        }
        assert slot.source_links_off is True
        assert slot.muted is False
        state.push_slots_update.assert_called_once_with()
        state.push_slot_patch.assert_not_called()


class TestPersistence:
    def test_flag_round_trips_and_staged_value_wins(self) -> None:
        slot = _ChatSlot("chat-1-100")
        folds = mc.SaveFolds(memory_mode="persistent", source_links_off=True)
        assert mc.encode(slot, folds=folds).get("source_links_off") is True
        assert mc.encode(slot, merge=True, folds=folds).get("source_links_off") is True
        slot.source_links_off = True
        plain = mc.SaveFolds(memory_mode="persistent")
        assert mc.encode(slot, folds=plain).get("source_links_off") is True
        slot.source_links_off = False
        assert "source_links_off" not in mc.encode(slot, folds=plain)

    def test_absence_retracts_it(self) -> None:
        # Turning the chips back on writes no key, so the key must be slot-owned
        # or a carried-forward ``true`` could never be cleared.
        assert "source_links_off" in SLOT_OWNED_META_KEYS


CHANNEL_KEY = "slack:1785370133.085471"


def _logged_state(tmp_path, monkeypatch) -> DashboardState:
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.channel_key_for_stem = lambda stem: ""
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _cron_job() -> MagicMock:
    job = MagicMock()
    job.id = "j1"
    job.name = "nightly"
    job.agent_id = ""
    job.member_id = ""
    job.memory_store = ""
    job.persistent_session = True
    job.hide_in_chat = False
    job.on_tab_close = "resume"
    return job


def _stored(state: DashboardState, key: str) -> object:
    log = state.conversation_log
    assert log is not None
    return log.get_metadata(key).get("source_links_off")


class TestReSurface:
    """A tab a binder re-creates keeps the switch the person set before it closed.

    Each test turns the chips off, saves through the real save path, closes the
    tab, re-surfaces it through the binder, saves again and reads the line back.
    """

    def _closed_cron_tab(self, tmp_path, monkeypatch) -> DashboardState:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _logged_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("cron-j1")
        slot.linked_session_key = "cron:j1"
        slot.source_links_off = True
        slot.append("assistant", f"run one {PRS[0]}", "")
        assert _save_slot_to_history(state, slot, force=True)
        state._slots.pop("cron-j1")
        assert _stored(state, "cron:j1") is True
        return state

    @pytest.mark.asyncio
    async def test_a_reopened_cron_tab_keeps_its_chips_off(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        state = self._closed_cron_tab(tmp_path, monkeypatch)
        await ensure_cron_slot(state, _cron_job())
        slot = state.get_slot("cron-j1")
        assert slot is not None and slot.linked_session_key == "cron:j1"
        assert slot.source_links_off is True and slot._source_links_off_hydrated is True
        assert slot.to_dict()["source_links"] == []
        slot.append("assistant", "run two", "")
        assert _save_slot_to_history(state, slot, force=True)
        assert _stored(state, "cron:j1") is True

    @pytest.mark.asyncio
    async def test_a_cron_save_before_the_read_keeps_it_on_disk(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.cron_inject import _bind_cron_slot, restore_cron_turn_timeout

        state = self._closed_cron_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        assert log is not None
        slot = _bind_cron_slot(state, _cron_job(), log.read_messages("cron:j1"), [])
        assert slot is not None
        assert slot.source_links_off is False and slot._source_links_off_hydrated is False
        slot.append("assistant", "run two", "")
        assert _save_slot_to_history(state, slot, force=True)
        assert _stored(state, "cron:j1") is True
        await restore_cron_turn_timeout(state, "j1")
        assert slot.source_links_off is True and slot._source_links_off_hydrated is True

    @pytest.mark.asyncio
    async def test_an_unreadable_cron_line_is_never_overwritten(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.cron_inject import _bind_cron_slot, restore_cron_turn_timeout

        state = self._closed_cron_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        assert log is not None
        slot = _bind_cron_slot(state, _cron_job(), log.read_messages("cron:j1"), [])
        assert slot is not None
        with patch.object(log, "get_metadata_status", return_value=({}, False)):
            await restore_cron_turn_timeout(state, "j1")
        assert slot._source_links_off_hydrated is False
        slot.append("assistant", "run two", "")
        assert _save_slot_to_history(state, slot, force=True)
        assert _stored(state, "cron:j1") is True

    @pytest.mark.asyncio
    async def test_a_run_delivery_binds_the_closed_tab_with_its_chips_off(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.cron import CronJob
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.slack import gateway

        state = self._closed_cron_tab(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.cron_inject.cron_tab_workspace", lambda job: "")
        real_inject = gateway.inject_cron_result_to_dashboard
        seen: list[bool] = []

        def _inject(st, job, *args, **kwargs):  # type: ignore[no-untyped-def]
            slot = st.get_slot("cron-j1")
            seen.append(slot is not None and slot.source_links_off)
            return real_inject(st, job, *args, **kwargs)

        monkeypatch.setattr(gateway, "inject_cron_result_to_dashboard", _inject)
        gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
        gw.dashboard_state = state
        gw.slack = None
        gw._deliver_cron_to_channel = AsyncMock(return_value=False)
        job = CronJob(id="j1", name="nightly", message="Check the PRs.", persistent_session=True)
        job.set_run_result(f"run two {PRS[1]}")
        await gateway.GatewayOrchestrator._deliver_cron_agent_result(
            gw, job, "cron:j1", MagicMock(), f"run two {PRS[1]}", "hash", None, None
        )
        assert seen == [True]
        slot = state.get_slot("cron-j1")
        assert slot is not None and slot.to_dict()["source_links"] == []
        assert _save_slot_to_history(state, slot, force=True)
        assert _stored(state, "cron:j1") is True

    def _closed_channel_tab(self, tmp_path, monkeypatch) -> DashboardState:
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _logged_state(tmp_path, monkeypatch)
        slot = channel_slots.surface_channel_session(
            state,
            {"key": CHANNEL_KEY, "title": "t", "modified": 0.0},
            {},
            [{"role": "user", "content": PRS[0]}],
            session_key=CHANNEL_KEY,
        )
        assert slot is not None
        slot.source_links_off = True
        slot._source_links_off_hydrated = True  # what the toggle's commit leaves
        slot.append("assistant", f"reply {PRS[1]}", "")
        assert _save_slot_to_history(state, slot, force=True)
        state._slots.pop(slot.key)
        assert _stored(state, CHANNEL_KEY) is True
        return state

    def _resurface(self, state: DashboardState, meta: dict):
        from kiro_crew.dashboard import channel_slots

        slot = channel_slots.surface_channel_session(
            state,
            {"key": CHANNEL_KEY, "title": "t", "modified": 0.0},
            meta,
            [{"role": "user", "content": PRS[0]}],
            session_key=CHANNEL_KEY,
        )
        assert slot is not None
        return slot

    def test_a_reopened_channel_tab_keeps_its_chips_off(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = self._closed_channel_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        assert log is not None
        slot = self._resurface(state, log.get_metadata(CHANNEL_KEY))
        assert slot.source_links_off is True
        assert slot.to_dict()["source_links"] == []
        slot.append("assistant", "later", "")
        assert _save_slot_to_history(state, slot, force=True)
        assert _stored(state, CHANNEL_KEY) is True

    def test_a_channel_line_the_pass_did_not_read_is_carried_forward(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = self._closed_channel_tab(tmp_path, monkeypatch)
        slot = self._resurface(state, {})
        assert slot.source_links_off is False and slot._source_links_off_hydrated is False
        slot.append("assistant", "later", "")
        assert _save_slot_to_history(state, slot, force=True)
        assert _stored(state, CHANNEL_KEY) is True

    def test_the_save_fold_prefers_the_toggle_then_disk_until_read(self) -> None:
        from kiro_crew.dashboard.slot_persistence.metadata_line import source_links_off_fold

        slot = _ChatSlot("cron-j1")
        assert source_links_off_fold(slot, None, {"source_links_off": True}) is None
        slot._source_links_off_hydrated = False
        assert source_links_off_fold(slot, None, {"source_links_off": True}) is True
        assert source_links_off_fold(slot, None, {}) is False
        assert source_links_off_fold(slot, False, {"source_links_off": True}) is False

    @pytest.mark.asyncio
    async def test_a_committed_toggle_outranks_an_older_restore(self) -> None:
        from kiro_crew.dashboard.cron_inject import apply_cron_source_links_off

        slot = _ChatSlot("chat-1-100")
        slot._source_links_off_hydrated = False

        async def _save(*args, **kwargs):
            kwargs["after_commit_under_lock"]()
            return True

        status, _ = await _patch(_state(slot), {"source_links_off": True}, _save)
        assert status == 200
        assert slot._source_links_off_hydrated is True
        assert apply_cron_source_links_off(slot, {}) is False
        assert slot.source_links_off is True

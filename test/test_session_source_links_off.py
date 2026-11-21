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

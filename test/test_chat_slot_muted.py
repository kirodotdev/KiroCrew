"""Per-row session mute (`slot.muted`): endpoint, persistence and feed-note sink.

`PATCH /api/chat/slots/{slot}/muted` shares its body with the "mute sessions it
opens" toggle, so the provenance and persist-before-publish contracts pinned in
``test_chat_slot_mutes_opened_provenance.py`` hold here too. These tests pin what
is specific to the per-row flag: the user-only gate names the right code, the
staged value travels as ``muted_override`` (and never as ``mutes_opened_override``),
and the flag round-trips through the metadata line.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import api_chat_slot_muted
from kiro_crew.dashboard.slot_persistence import metadata_codec as mc
from kiro_crew.dashboard.state import _ChatSlot


def _make_app(state: MagicMock, *, declared_app: str = "", internal_auth: bool = False):
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_identity(request: web.Request, handler):
        request["app"] = declared_app
        request["user"] = "local-app"
        request["internal_auth"] = internal_auth
        request["is_dashboard_user"] = declared_app == "" and not internal_auth
        return await handler(request)

    app.middlewares.append(_publish_identity)
    app.router.add_patch("/api/chat/slots/{slot}/muted", api_chat_slot_muted)
    return app


def _state(slot: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state._slots = {slot.key: slot}
    state.push_slot_patch = MagicMock()
    return state


def _fences():
    return (
        patch("kiro_crew.dashboard.chat_folders.refuse_unattributable_caller", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.member_slot_write_refused", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.deny_app_slot_access", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.app_owns_transcript", return_value=True),
        patch("kiro_crew.dashboard.chat_folders._effective_request_app", return_value=""),
    )


async def _patch(state: MagicMock, payload: object, **app_kwargs):
    app = _make_app(state, **app_kwargs)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/chat/slots/chat-1-100/muted", json=payload)
        return resp.status, await resp.json()


class TestMutedEndpoint:
    @pytest.mark.asyncio
    async def test_agent_caller_is_refused_before_any_write(self) -> None:
        slot = _ChatSlot("chat-1-100")
        state = _state(slot)
        f1, f2, f3, f4, f5 = _fences()
        save = AsyncMock(return_value=True)
        with f1, f2, f3, f4, f5, patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", save):
            status, body = await _patch(state, {"muted": True}, internal_auth=True)
        assert status == 403
        assert body["code"] == "muted_user_only"
        assert slot.muted is False
        save.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_bool_is_400(self) -> None:
        slot = _ChatSlot("chat-1-100")
        f1, f2, f3, f4, f5 = _fences()
        with f1, f2, f3, f4, f5:
            status, body = await _patch(_state(slot), {"muted": "true"})
        assert status == 400
        assert body["code"] == "muted_not_bool"
        assert slot.muted is False

    @pytest.mark.asyncio
    async def test_user_mute_stages_muted_override_and_flips_after_commit(self) -> None:
        slot = _ChatSlot("chat-1-100")
        state = _state(slot)
        seen: dict[str, object] = {}

        async def _save(*args, **kwargs):
            seen["live_during_save"] = slot.muted
            seen["muted_override"] = kwargs.get("muted_override")
            seen["mutes_opened_override"] = kwargs.get("mutes_opened_override")
            kwargs["after_commit_under_lock"]()
            return True

        f1, f2, f3, f4, f5 = _fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _save),
        ):
            status, body = await _patch(state, {"muted": True})
        assert status == 200
        assert body == {"ok": True, "muted": True, "changed": True}
        assert seen == {
            "live_during_save": False,
            "muted_override": True,
            # The sibling flag is not touched by a per-row mute.
            "mutes_opened_override": None,
        }
        assert slot.muted is True
        assert slot.mutes_opened is False
        state.push_slot_patch.assert_called_once_with("chat-1-100", ("muted",))


class TestMutedPersistence:
    def test_flag_round_trips_and_staged_value_wins(self) -> None:
        slot = _ChatSlot("chat-1-100")
        folds = mc.SaveFolds(memory_mode="persistent", muted=True)
        assert mc.encode(slot, folds=folds).get("muted") is True
        assert mc.encode(slot, merge=True, folds=folds).get("muted") is True
        # Default fold reads the live flag; an unmuted slot omits the key so
        # absence retracts it on disk.
        slot.muted = True
        assert mc.encode(slot, folds=mc.SaveFolds(memory_mode="persistent")).get("muted") is True
        slot.muted = False
        line = mc.encode(slot, folds=mc.SaveFolds(memory_mode="persistent"))
        assert "muted" not in line

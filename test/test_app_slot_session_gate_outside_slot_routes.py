"""An app's own slot that runs on another conversation is refused outside the slot routes too.

The per-slot checkpoint refuses an owner app's slot linked to a channel thread on every
``/api/chat/slots/{slot}`` route (``test_slot_ownership_checkpoint_owner_session.py``).
Two paths decide ownership in their own handler instead: the tunnel send
(``api_instances_send_session``) and the OpenAI-compatible completion. Each applies the same
session-and-transcript test here and answers with the body it already gives for another
app's slot.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _async_value,
    _isolate_import_tmp,
    _OwnerReq,
)
from slot_ownership_checkpoint_helpers import state_fixture  # noqa: F401 -- requested by name

from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.dashboard import openai_compat
from kiro_crew.dashboard import session_transfer as st
from kiro_crew.dashboard import slot_ownership
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

OWNER_APP = "owner-app"
CHANNEL_KEY = "slack:1700000000.000100"
# A channel transcript stem that also passes the completion's ``id`` grammar (no dot).
# ``channel_origin`` moves the transcript only for a slot named like a channel stem;
# on a plain name such as ``"mine"`` the slot still writes ``dashboard:mine``.
CHANNEL_STEM = "telegram_100200300"
# Bounds each completion call, so a request that wrongly reaches the turn fails here
# instead of waiting on a ``done`` the mocked ``_run_chat`` never emits.
COMPLETION_TIMEOUT_S = 30.0


def _unbound_channel_slot(state):
    slot = state.get_or_create_slot(CHANNEL_STEM, app=OWNER_APP)
    slot.channel_origin = True
    assert not slot_ownership.app_owns_slot_session(OWNER_APP, slot), "fixture must be foreign"
    return slot


async def _complete(state, slot_key: str):
    return await asyncio.wait_for(
        openai_compat.api_completions(_completion_request(state, slot_key)),
        timeout=COMPLETION_TIMEOUT_S,
    )


class _Ready(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return True


# ── the tunnel send ──────────────────────────────────────────────────────


def _send_request(state, slot_key: str, app: str = OWNER_APP) -> _OwnerReq:
    return _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        claims={"user": "owner", "app": app},
        json=_async_value({"slot": slot_key}),
    )


def _send_state(slot) -> SimpleNamespace:
    return SimpleNamespace(
        _slots={slot.key: slot},
        instances_manager=SimpleNamespace(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )


@pytest.fixture
def builds(monkeypatch) -> list[dict]:
    """Past the owner gate (an app token stops at it today), recording each build."""
    calls: list[dict] = []

    async def _record(_state, slot, **kwargs):
        calls.append({"slot": slot, **kwargs})
        raise st.SnapshotUnstable("recorded")

    monkeypatch.setattr(hi, "_guard", lambda _request, _op: None)
    monkeypatch.setattr(hi, "build_transfer_bundle_async", _record)
    return calls


class TestTheTunnelSendJudgesTheConversation:
    @pytest.mark.asyncio
    async def test_an_owned_slot_linked_to_a_channel_is_the_missing_slot_404(
        self, state, builds
    ) -> None:
        slot = state.get_or_create_slot("mine", app=OWNER_APP)
        slot.linked_session_key = CHANNEL_KEY

        resp = await hi.api_instances_send_session(_send_request(_send_state(slot), "mine"))

        assert resp.status == 404
        assert json.loads(resp.body) == {
            "error": "session not found",
            "code": "transfer_slot_not_found",
        }
        assert builds == [], "nothing may be bundled from a conversation the app does not own"

    @pytest.mark.asyncio
    async def test_an_unbound_channel_origin_slot_is_the_missing_slot_404(
        self, state, builds
    ) -> None:
        slot = _unbound_channel_slot(state)

        resp = await hi.api_instances_send_session(_send_request(_send_state(slot), CHANNEL_STEM))

        assert resp.status == 404
        assert json.loads(resp.body)["code"] == "transfer_slot_not_found"
        assert builds == []

    @pytest.mark.asyncio
    async def test_the_apps_own_conversation_is_built(self, state, builds) -> None:
        slot = state.get_or_create_slot("mine", app=OWNER_APP)

        await hi.api_instances_send_session(_send_request(_send_state(slot), "mine"))

        assert len(builds) == 1
        assert builds[0]["slot"] is slot


# ── the OpenAI-compatible completion ─────────────────────────────────────


def _completion_request(state, slot_key: str, app: str = OWNER_APP) -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "model": "kirocrew",
            "id": slot_key,
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    request.app = {"state": state, "kiro_prerequisite_service": object.__new__(_Ready)}
    request.get = MagicMock(side_effect=lambda key, default="": app if key == "app" else default)
    return request


class TestTheCompletionJudgesTheConversation:
    @pytest.mark.asyncio
    async def test_a_linked_slot_answers_like_another_apps_slot(self, state, monkeypatch) -> None:
        run = AsyncMock()
        monkeypatch.setattr(openai_compat, "_run_chat", run)
        linked = state.get_or_create_slot("mine", app=OWNER_APP)
        linked.linked_session_key = CHANNEL_KEY
        state.get_or_create_slot("theirs", app="another-app")

        refused = await _complete(state, "mine")
        foreign = await _complete(state, "theirs")

        assert refused.status == foreign.status == 403
        assert json.loads(refused.body) == json.loads(
            foreign.body
        ), "the answer must not tell an app which of its slots carry a link"
        assert json.loads(refused.body)["code"] == "slot_owned_by_another_app"
        run.assert_not_called()
        assert linked.messages == [], "no turn may be appended to the linked conversation"

    @pytest.mark.asyncio
    async def test_an_unbound_channel_origin_slot_is_refused(self, state, monkeypatch) -> None:
        run = AsyncMock()
        monkeypatch.setattr(openai_compat, "_run_chat", run)
        _unbound_channel_slot(state)

        resp = await _complete(state, CHANNEL_STEM)

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "slot_owned_by_another_app"
        run.assert_not_called()

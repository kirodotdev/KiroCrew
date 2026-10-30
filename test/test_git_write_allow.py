"""``session_create(allow_git_writes=True)`` and the card's "Allow git writes in this session".

With ``hooks.confirm_git_writes`` on, the per-session allow is how a session
writes git history without a card. These pin the two places that set it -- a
dispatching session at create time and the person on a git-write card -- that it
is not inherited, that a session opened with it cannot pass it on, that the card
route refuses it for a card that is not a git write, and that setting the
approval mode to Normal ends it.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from chat_test_helpers import _make_state as _make_chat_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _agent_resolves,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.git_write_grant import BY_PERSON, BY_SESSION_CREATE


class TestSessionCreate:
    def test_the_child_holds_the_allow_only_when_asked(self, tmp_path):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        plain = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
        assert state.get_slot(plain["target"])._git_writes_allowed is False

        granted = asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=True)
        )
        child = state.get_slot(granted["target"])
        assert child._git_writes_allowed is True
        assert child._git_writes_by == f"{BY_SESSION_CREATE}{caller.key}"
        # Granting the child changes nothing on the caller.
        assert caller._git_writes_allowed is False

    def test_the_callers_own_allow_is_not_inherited(self, tmp_path):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller._trust = True
        caller._git_writes_allowed = True
        caller._git_writes_by = BY_PERSON
        created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
        child = state.get_slot(created["target"])
        assert child._trust is True
        assert child._git_writes_allowed is False

    def test_a_session_opened_with_the_allow_cannot_pass_it_on(self, tmp_path):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller._git_writes_allowed = True
        caller._git_writes_by = f"{BY_SESSION_CREATE}chat-0"
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=True)
            )
        assert exc.value.code == "git_writes_not_delegable"

    def test_ending_or_regranting_does_not_lift_the_opened_with_it_mark(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import git_write_grant

        monkeypatch.setattr(git_write_grant, "sel", lambda: MagicMock())
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller._git_writes_allowed = True
        caller._git_writes_by = f"{BY_SESSION_CREATE}chat-0"
        git_write_grant.end_git_writes(caller, caller="x")
        assert caller._git_writes_allowed is False
        git_write_grant.allow_git_writes(caller, by=BY_PERSON, caller="x")
        assert caller._git_writes_by == f"{BY_SESSION_CREATE}chat-0"
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=True)
            )
        assert exc.value.code == "git_writes_not_delegable"

    def test_a_person_granted_session_can_dispatch_with_it(self, tmp_path):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller._git_writes_allowed = True
        caller._git_writes_by = BY_PERSON
        created = asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=True)
        )
        assert state.get_slot(created["target"])._git_writes_allowed is True

    @pytest.mark.parametrize("raw", ["true", 1, None])
    def test_a_non_boolean_is_refused_before_anything_is_created(self, tmp_path, raw):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        before = set(state._slots)
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=raw)
            )
        assert exc.value.code == "invalid_field_type"
        assert set(state._slots) == before

    def test_the_create_audit_records_the_allow(self, tmp_path):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        seen: dict[str, object] = {}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sc, "_audit", lambda **kw: seen.update(kw))
            asyncio.run(
                sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=True)
            )
        assert seen["detail"]["allow_git_writes"] == "true"


def _pending_card(state, slot, *, git_write: bool):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    meta = {"request_id": "req-1", "trust_grantable": "1"}
    if git_write:
        meta["git_write"] = "1"
    row = slot.append("permission", "git commit -m x", json.dumps(meta))
    slot.register_approval("req-1", fut, row)
    return fut


async def _post(state, path: str, body: dict):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(path, json=body)
        return resp.status, await resp.json()


class TestTheCardRoute:
    @pytest.mark.asyncio
    async def test_allow_on_a_git_write_card_sets_it_and_approves(self, tmp_path):
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        fut = _pending_card(state, slot, git_write=True)
        status, _ = await _post(
            state,
            f"/api/chat/slots/{slot.key}/approve",
            {"action": "git_writes", "request_id": "req-1"},
        )
        assert status == 200
        assert fut.result() == "approved"
        assert slot._git_writes_allowed is True
        assert slot._git_writes_by == BY_PERSON

    @pytest.mark.asyncio
    async def test_allow_is_refused_on_a_card_that_is_not_a_git_write(self, tmp_path):
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        fut = _pending_card(state, slot, git_write=False)
        try:
            status, body = await _post(
                state,
                f"/api/chat/slots/{slot.key}/approve",
                {"action": "git_writes", "request_id": "req-1"},
            )
            assert status == 400
            assert body["code"] == "not_a_git_write"
            assert not fut.done()
            assert slot._git_writes_allowed is False
        finally:
            fut.cancel()

    @pytest.mark.asyncio
    async def test_normal_mode_ends_the_allow(self, tmp_path):
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        slot._git_writes_allowed = True
        slot._git_writes_by = BY_PERSON
        status, _ = await _post(state, "/api/chat/mode", {"mode": "normal", "slot": slot.key})
        assert status == 200
        assert slot._git_writes_allowed is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "yolo"])
    async def test_switching_to_trust_or_yolo_leaves_a_git_write_card_open(
        self, tmp_path, mode, monkeypatch
    ):
        from kiro_crew.dashboard import chat_handlers

        override = MagicMock()
        override.activate.return_value = MagicMock(active=True)
        override.is_declared = False
        monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        git_card = _pending_card(state, slot, git_write=True)
        try:
            status, _ = await _post(state, "/api/chat/mode", {"mode": mode, "slot": slot.key})
            assert status == 200
            assert not git_card.done()
        finally:
            git_card.cancel()

    @pytest.mark.asyncio
    async def test_switching_to_trust_still_approves_an_ordinary_card(self, tmp_path):
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        card = _pending_card(state, slot, git_write=False)
        status, _ = await _post(state, "/api/chat/mode", {"mode": "trust", "slot": slot.key})
        assert status == 200
        assert card.result() == "approved"

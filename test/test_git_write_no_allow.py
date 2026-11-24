"""A git-write confirm card is answered only by the person, and nothing lifts it.

With ``hooks.confirm_git_writes`` on, every git command other than a known read
asks once, every time, under every approval mode. There is no per-session allow:
``session_create`` takes no ``allow_git_writes``, the card route takes no
``git_writes`` decision, and switching a session to Trust or YOLO never settles a
pending confirm card. The only off switch is ``hooks.confirm_git_writes: false``.
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


def _pending_card(state, slot, meta: dict, *, request_id: str = "req-1"):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    row = slot.append(
        "permission", "git commit -m x", json.dumps({"request_id": request_id, **meta})
    )
    slot.register_approval(request_id, fut, row)
    return fut


async def _post(state, path: str, body: dict):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(path, json=body)
        return resp.status, await resp.json()


@pytest.fixture
def _safety_override(monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    override = MagicMock()
    override.activate.return_value = MagicMock(active=True)
    override.is_declared = False
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)


class TestTheModeSweep:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "yolo"])
    async def test_switching_to_trust_or_yolo_never_settles_a_git_confirm_card(
        self, tmp_path, mode, _safety_override
    ):
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        card = _pending_card(state, slot, {"git_confirm": "1"})
        try:
            status, _ = await _post(state, "/api/chat/mode", {"mode": mode, "slot": slot.key})
            assert status == 200
            assert not card.done()
        finally:
            card.cancel()

    @pytest.mark.asyncio
    async def test_switching_to_trust_still_approves_an_ordinary_card(self, tmp_path):
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        card = _pending_card(state, slot, {"trust_grantable": "1"})
        status, _ = await _post(state, "/api/chat/mode", {"mode": "trust", "slot": slot.key})
        assert status == 200
        assert card.result() == "approved"


class TestThereIsNoAllow:
    def test_session_create_takes_no_allow_git_writes(self, tmp_path):
        state = _make_chat_state(tmp_path)
        caller = _slot(state, "chat-1")
        with pytest.raises(TypeError):
            asyncio.run(
                sc.create_session(state, caller_session_key=_key(caller), allow_git_writes=True)
            )

    def test_the_session_create_tool_has_no_allow_git_writes_parameter(self):
        from kiro_crew.mcp_dashboard_session_tools import _session_tools

        create = next(t for t in _session_tools() if t.name == "session_create")
        assert "allow_git_writes" not in json.dumps(create.schema)

    def test_no_allow_module_remains(self):
        import importlib.util

        assert importlib.util.find_spec("kiro_crew.dashboard.git_write_grant") is None

    @pytest.mark.asyncio
    async def test_a_git_writes_decision_never_approves_the_card(self, tmp_path):
        # The route knows no such decision: it is read as a rejection, as every
        # unknown decision is, and nothing on the slot changes.
        state = _make_chat_state(tmp_path)
        slot = _slot(state, "chat-1")
        card = _pending_card(state, slot, {"git_confirm": "1"})
        try:
            await _post(
                state,
                f"/api/chat/slots/{slot.key}/approve",
                {"action": "git_writes", "request_id": "req-1"},
            )
            assert not (card.done() and card.result() == "approved")
            assert not hasattr(slot, "_git_writes_allowed")
        finally:
            card.cancel()

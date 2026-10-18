"""Fixtures and helpers the slot-ownership checkpoint test files share.

``APP``, the app the tests call as, ``_GRANT``, the session-grant check they patch, and
``_NOT_FOUND``, the uniform refusal body; the ``state`` fixture, a dashboard state built
under the test's ``tmp_path``, and the ``sel_spy`` fixture with the security-event-log
spy behind it; ``_as``, the token-auth stand-in, and ``_client``, which serves the real
chat handlers behind the checkpoint as a chosen caller; ``_with_reply``, which gives a
slot one exchange in memory, ``_persist``, which saves a slot's transcript, and
``_persist_and_close``, which also closes the slot; and the ``regen_run`` and
``chat_run`` fixtures that replace the chat run.
``test_slot_ownership_checkpoint.py``, ``test_slot_ownership_checkpoint_owner_session.py``
and ``test_slot_ownership_checkpoint_post_await.py`` import what they read from here by
name.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state, close_before_resume

from kiro_crew.dashboard import slot_ownership
from kiro_crew.dashboard.state import _ChatSlot

APP = "crew-keyboard"
_GRANT = "kiro_crew.apps.permissions.app_can_manage_session_approvals"
_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}


class _SelSpy:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(kw)

    def denials(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r.get("outcome") == "denied"]


@pytest.fixture(name="sel_spy")
def sel_spy_fixture(monkeypatch: pytest.MonkeyPatch) -> _SelSpy:
    spy = _SelSpy()
    monkeypatch.setattr(slot_ownership, "sel", lambda: spy)
    return spy


@pytest.fixture(name="state")
def state_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _as(app_name: str | None):
    """Middleware standing in for token auth: the ``app`` claim, or none at all."""

    @web.middleware
    async def identity(request: web.Request, handler):
        if app_name is not None:
            request["app"] = app_name
            request["user"] = "" if app_name else "local-app"
        return await handler(request)

    return identity


def _client(state, caller: str | None) -> TestClient:
    from kiro_crew.dashboard.chat import (
        api_chat_slot_create,
        api_chat_slot_drop,
        api_chat_slot_generate_title,
        api_chat_slot_mirror_unlink,
        api_chat_slot_slack_link,
        api_chat_slot_summary_generate,
    )
    from kiro_crew.dashboard.session_export import api_chat_slot_export

    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/generate-title", api_chat_slot_generate_title)
    app.router.add_post("/api/chat/slots/{slot}/drop", api_chat_slot_drop)
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/mirror-unlink", api_chat_slot_mirror_unlink)
    app.router.add_get("/api/chat/slots/{slot}/export", api_chat_slot_export)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_post("/api/chat/slots/{slot}/summary", api_chat_slot_summary_generate)
    app.middlewares.insert(0, _as(caller))
    return TestClient(TestServer(app))


def _with_reply(slot: _ChatSlot, reply: str = "the user's ORIGINAL reply") -> None:
    slot.append("user", "the user's question")
    slot.append("assistant", reply)
    slot.drain()


@pytest.fixture(name="regen_run")
def regen_run_fixture(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate._run_chat", run)
    return run


async def _persist(state, slot: _ChatSlot) -> None:
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    slot._titled = True
    slot.append("user", "USER-EARLIER")
    slot.append("assistant", "USER-EARLIER-REPLY")
    await save_slot_off_loop(state, slot)


async def _persist_and_close(state, slot: _ChatSlot) -> None:
    from kiro_crew.dashboard.chat_handlers import close_slot

    await _persist(state, slot)
    await close_slot(state, slot, slot.key)
    assert slot.key not in state._slots
    # Closed BEFORE any resume the test then runs, whatever the clock's resolution.
    close_before_resume(state.conversation_log, f"dashboard:{slot.key}")


@pytest.fixture(name="chat_run")
def chat_run_fixture(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run)
    return run

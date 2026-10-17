"""The app and the state fixture the chat regenerate test modules share.

``state`` (a dashboard state whose sessions double lets edit-resend reach its commit;
declared ``@pytest.fixture(name="state")`` and imported as ``state_fixture``), and
``_make_regen_app`` and ``_client`` (all three ``chat_regenerate`` routes on one app, and a
test client over it).
``test_chat_regenerate_cov80.py`` and ``test_chat_regenerate_cov80_edit_resend_boundary.py``
import them by name.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
    api_chat_slot_switch_variant,
)


def _make_regen_app(state) -> web.Application:
    """App exposing all three chat_regenerate routes, including edit-resend."""
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    return app


@pytest.fixture(name="state")
def state_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    # edit-resend is now a real conversation boundary: it discards the native
    # ACP conversation and flushes the cleared resume sid BEFORE persisting the
    # truncated history. Configure the sessions double so the happy paths reach
    # commit -- discard succeeds (returns True), the flush is a no-op, and the
    # orphan-session lookup returns "" so no cleanup is attempted.
    st.sessions.discard_conversation = AsyncMock(return_value=True)
    st.sessions.aflush = AsyncMock()
    st.sessions._session_map.get = MagicMock(return_value="")
    return st


def _client(state):
    return TestClient(TestServer(_make_regen_app(state)))

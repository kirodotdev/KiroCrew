"""Shared stand-ins for the chat mode and app-send security tests (``test_chat_mode_security*.py``).

The app-token middleware and the app it mounts, the autouse ``_hermetic_home`` and
``_isolate_safety_override`` fixtures, the ``state`` fixture, the ``_app_client`` test
client, and the app-control target table with its slot factory.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import (
    api_chat,
    api_chat_mode,
    api_chat_slot_approve,
)
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.safety_override import (
    reset_singleton,
)


@web.middleware
async def _app_request(request: web.Request, handler):
    request["app"] = "crew-keyboard"
    request["user"] = ""
    return await handler(request)


def _make_app_token_mode_app(state) -> web.Application:
    app = web.Application(middlewares=[_app_request])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the family calls monkeypatch.undo()
def _hermetic_home(tmp_path, monkeypatch):
    """Redirect KIROCREW_HOME so SEL writes never touch the developer's home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_safety_override():
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture(name="state")
def state_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _app_client(state) -> TestClient:
    return TestClient(TestServer(_make_app_token_mode_app(state)))


_APP_CONTROL_TARGETS = (
    ("user", True),
    ("cron", False),
    ("system", False),
    ("member", False),
    ("remote", False),
    ("cron-linked", False),
    ("channel-linked", False),
    ("other-app", False),
    ("own-app", True),
)


def _make_app_control_target(state, target: str):
    if target == "user":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    if target == "cron":
        return state.get_or_create_slot("s1", origin=SlotOrigin.CRON)
    if target == "system":
        return state.get_or_create_slot("s1", origin=SlotOrigin.SYSTEM)
    if target == "member":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER, mode="member")
    if target == "remote":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.executor = "remote"
        slot.instance_id = "peer-1"
        slot.remote_slot = "remote-s1"
        return slot
    if target == "cron-linked":
        # A user-created slot that a cron injection re-bound: USER origin, but
        # its turns run on the cron session.
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "cron:job-1"
        return slot
    if target == "channel-linked":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "slack:12345.678"
        return slot
    if target == "other-app":
        return state.get_or_create_slot("s1", app="other-app")
    if target == "own-app":
        return state.get_or_create_slot("s1", app="crew-keyboard")
    raise AssertionError(f"unknown target: {target}")

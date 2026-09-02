"""Tests for ``GET /api/sessions?exclude_open=1``.

The sidebar's Older-sessions pane renders the complement of the open tabs listed
above it. The endpoint listed every session file on disk, so every open tab was
repeated in that pane — the newest one, the conversation the user is in, always
landing at its top.

The exclusion is opt-in, applied server-side, and counted before pagination.
Each of those three is load-bearing and has a test here:

- opt-in, because the full inventory is what memory consolidation and the
  command palette's recents read;
- server-side, because the client advances its offset by the row count it
  received;
- resolved through ``slot_history_key``, because a channel tab's transcript is
  its ``linked_session_key`` and a derived ``dashboard:<slot>`` name misses it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import api_sessions
from kiro_crew.history import ConversationLog


class _FakeSlot:
    """Minimal stand-in for ``_ChatSlot`` — only what key resolution reads."""

    def __init__(
        self,
        key: str,
        *,
        linked_session_key: str = "",
        channel_origin: bool = False,
    ) -> None:
        self.key = key
        self.linked_session_key = linked_session_key
        self.channel_origin = channel_origin


def _make_request(
    sessions: list[dict],
    *,
    slots: dict[str, _FakeSlot] | None = None,
    query: dict[str, str] | None = None,
) -> web.Request:
    """Build a minimal ``web.Request`` with a fake ``conversation_log`` + ``_slots``."""
    conv_log = MagicMock()
    conv_log.list_sessions.return_value = sessions
    # The handler folds stacked ``dashboard_`` prefixes through this; wire the
    # real implementation so the fold is actually exercised, not mocked away.
    conv_log._canonical_key = ConversationLog._canonical_key

    state = MagicMock()
    state.conversation_log = conv_log
    state._slots = slots or {}

    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.query = query or {}
    return request


async def _call(request: web.Request) -> dict:
    resp = await api_sessions(request)
    return json.loads(resp.body.decode("utf-8"))


def _keys(body: dict) -> list[str]:
    return [s["key"] for s in body["sessions"]]


@pytest.mark.asyncio
async def test_open_sessions_are_listed_without_the_opt_in() -> None:
    """Default stays a full inventory — consolidation must not skip live chats."""
    sessions = [{"key": "dashboard_chat-1"}, {"key": "dashboard_chat-2"}]
    request = _make_request(sessions, slots={"chat-1": _FakeSlot("chat-1")})

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-1", "dashboard_chat-2"]
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_exclude_open_drops_a_session_a_live_slot_holds() -> None:
    sessions = [{"key": "dashboard_chat-1"}, {"key": "dashboard_chat-2"}]
    request = _make_request(
        sessions,
        slots={"chat-1": _FakeSlot("chat-1")},
        query={"exclude_open": "1"},
    )

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-2"]


@pytest.mark.asyncio
async def test_exclude_open_keeps_a_closed_session() -> None:
    """A session with no live slot is exactly what this pane is for."""
    sessions = [{"key": "dashboard_chat-9"}]
    request = _make_request(sessions, slots={}, query={"exclude_open": "1"})

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-9"]


@pytest.mark.asyncio
async def test_exclude_open_recognises_a_channel_tab_by_its_linked_key() -> None:
    """A channel tab's transcript is its ``linked_session_key``, not its slot name.

    Deriving ``dashboard:<slot>`` instead would leave every Slack and Discord tab
    listed in the pane, which is half the duplicates.
    """
    sessions = [{"key": "slack_1712793600.123456"}, {"key": "dashboard_chat-2"}]
    slots = {
        "slack_1712793600.123456": _FakeSlot(
            "slack_1712793600.123456",
            linked_session_key="slack:1712793600.123456",
            channel_origin=True,
        )
    }
    request = _make_request(sessions, slots=slots, query={"exclude_open": "1"})

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-2"]


@pytest.mark.asyncio
async def test_exclude_open_folds_a_stacked_dashboard_prefix() -> None:
    """``list_sessions`` reports the raw stem of a resume round-trip's duplicate."""
    sessions = [{"key": "dashboard_dashboard_chat-1"}, {"key": "dashboard_chat-2"}]
    request = _make_request(
        sessions,
        slots={"chat-1": _FakeSlot("chat-1")},
        query={"exclude_open": "1"},
    )

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-2"]


@pytest.mark.asyncio
async def test_total_and_has_more_describe_the_filtered_list() -> None:
    """Counting before the exclusion promises a page the pane cannot deliver."""
    sessions = [
        {"key": "dashboard_chat-1"},
        {"key": "dashboard_chat-2"},
        {"key": "dashboard_chat-3"},
    ]
    request = _make_request(
        sessions,
        slots={"chat-1": _FakeSlot("chat-1")},
        query={"exclude_open": "1", "limit": "1", "offset": "0"},
    )

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-2"]
    assert body["total"] == 2
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_last_filtered_page_reports_no_more() -> None:
    """The offset the client sends back must land on the end of the SAME list."""
    sessions = [
        {"key": "dashboard_chat-1"},
        {"key": "dashboard_chat-2"},
        {"key": "dashboard_chat-3"},
    ]
    request = _make_request(
        sessions,
        slots={"chat-1": _FakeSlot("chat-1")},
        query={"exclude_open": "1", "limit": "1", "offset": "1"},
    )

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-3"]
    assert body["has_more"] is False

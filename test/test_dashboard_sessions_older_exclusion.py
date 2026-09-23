"""Tests for the two opt-in narrowings of ``GET /api/sessions``.

``exclude_open=1`` drops what a live slot holds; ``user_only=1`` drops the
machine namespaces. Both serve the same pane and both are opt-in, so they are
tested together.

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


@pytest.mark.asyncio
async def test_user_only_drops_the_machine_namespaces() -> None:
    """The defect: a titleless subagent transcript renders its own key as a row.

    ``wf-worker_`` rides along to pin a sibling the telemetry registry never registered,
    which is why this list is spelled literally rather than derived. ``secretary_`` and
    ``channel_`` are here so that every member of the tuple is covered by some assertion:
    an unasserted member can be deleted without any test noticing, and the failure mode
    that leaves is silent — its rows simply come back.
    """
    sessions = [
        {"key": "dashboard_chat-1"},
        {"key": "subagent_ba1f91c9"},
        {"key": "wf-worker_run7_a1b2"},
        # Every remaining member of the tuple gets an assertion too, so removing any one
        # of them fails a test rather than silently restoring its rows to the pane.
        {"key": "secretary_a41f"},
        {"key": "channel_C0AP3QR_agent7"},
        {"key": "dashboard_chat-2"},
    ]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-1", "dashboard_chat-2"]
    # Counted after the drop, like ``exclude_open``: the client advances its
    # offset by the rows it received, so a total describing the unfiltered list
    # promises a page that does not exist.
    assert body["total"] == 2
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_user_only_keeps_a_cron_row() -> None:
    """A cron job without ``hide_in_chat`` backs a real slot the user follows.

    The key does not record which kind wrote it, so dropping the namespace would
    take the followed ones with the silent ones.
    """
    sessions = [{"key": "cron_nightly-digest"}, {"key": "dashboard_chat-1"}]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == ["cron_nightly-digest", "dashboard_chat-1"]


@pytest.mark.asyncio
async def test_user_only_keeps_a_channel_conversation() -> None:
    """A Slack or Discord thread is a conversation a person held, not a machine run."""
    sessions = [
        {"key": "slack_1712793600.123456"},
        {"key": "discord_99"},
        {"key": "subagent_ba1f91c9"},
    ]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == ["slack_1712793600.123456", "discord_99"]


@pytest.mark.asyncio
async def test_user_only_classifies_both_separator_spellings_alike() -> None:
    """``_safe_key`` folds ``:`` to ``_`` on the way to disk.

    Every other subagent guard in the tree is spelled ``startswith("subagent:")``,
    which can never match a filename stem — which is why the pane showed these
    rows in the first place. The filter must catch the stem, and must not stop
    catching the live spelling a caller may still hand it.
    """
    sessions = [
        {"key": "subagent:abc"},
        {"key": "subagent_abc"},
        {"key": "wf-pool:1"},
        {"key": "wf_run-2"},
        {"key": "dashboard_chat-1"},
    ]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [{}, {"user_only": "0"}])
async def test_without_the_flag_the_full_inventory_is_unchanged(query: dict) -> None:
    """Opt-in, byte for byte.

    The memory "Consolidate all" action and the command palette's recents read
    this endpoint for every session there is; either would silently skip the
    machine transcripts if the default narrowed.
    """
    sessions = [
        {"key": "dashboard_chat-1"},
        {"key": "subagent_ba1f91c9"},
        {"key": "wf-unpooled_run7_2"},
        {"key": "cron_nightly-digest"},
    ]

    resp = await api_sessions(_make_request(sessions, query=query))

    assert resp.body == json.dumps({"sessions": sessions, "total": 4, "has_more": False}).encode(
        "utf-8"
    )


@pytest.mark.asyncio
async def test_the_two_narrowings_compose() -> None:
    """Older sessions asks for both, so one must not shadow the other."""
    sessions = [
        {"key": "dashboard_chat-1"},
        {"key": "subagent_ba1f91c9"},
        {"key": "dashboard_chat-2"},
    ]
    request = _make_request(
        sessions,
        slots={"chat-1": _FakeSlot("chat-1")},
        query={"exclude_open": "1", "user_only": "1"},
    )

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-2"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_user_only_keeps_a_side_panel_session() -> None:
    """``sel.py`` classifies ``side:`` as a dashboard surface: the user typed it.

    No ``side_`` transcript is expected to exist — the handler documents side
    messages as never reaching a persistent store — which is the point: dropping
    the namespace buys nothing and risks hiding a real conversation.
    """
    sessions = [{"key": "side_chat-1"}, {"key": "side_chat-1_2"}, {"key": "subagent_ba1f91c9"}]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == ["side_chat-1", "side_chat-1_2"]


@pytest.mark.asyncio
async def test_user_only_keeps_a_taskrunner_row_as_the_declared_residual() -> None:
    """``taskrunner`` is deliberately NOT dropped, and that trade is a contract.

    The namespace holds both runs (``taskrunner:run:<spec stem>``) and real
    conversations: ``POST /api/taskrunner/{id}/to-chat`` opens a titled chat slot on
    ``taskrunner:<task_id>:chat:<token>``. The key cannot reliably separate them once
    ``_safe_key`` has folded every ``:``, so the whole namespace stays listed. Showing
    a machine row costs less than hiding a conversation, and this test is what stops a
    later edit from quietly taking the other side of that trade.
    """
    sessions = [
        {"key": "taskrunner_5f2a_chat_3f9c1a24e8b7455da0c6ef1290bd7c58"},
        {"key": "taskrunner_run_nightly"},
        {"key": "subagent_ba1f91c9"},
    ]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == [
        "taskrunner_5f2a_chat_3f9c1a24e8b7455da0c6ef1290bd7c58",
        "taskrunner_run_nightly",
    ]


@pytest.mark.asyncio
async def test_user_only_drops_the_workflow_siblings_the_registry_never_listed() -> None:
    """``wf-unpooled``, ``wf-worker`` and ``wf-scope`` are minted as session keys but
    are absent from ``_TELEMETRY_LOCAL_PREFIXES``, so a filter derived from that
    registry silently missed them. The literal list is what closes that gap, and
    ``_in_namespace`` requires a separator so ``wf`` cannot swallow its own siblings.
    """
    sessions = [
        {"key": "wf_run7_1"},
        {"key": "wf-pool_run7"},
        {"key": "wf-unpooled_run7_2"},
        {"key": "wf-worker_run7_a1b2"},
        {"key": "wf-author_run7_a1"},
        {"key": "wf-scope_run7"},
        {"key": "dashboard_chat-1"},
    ]
    request = _make_request(sessions, query={"user_only": "1"})

    body = await _call(request)

    assert _keys(body) == ["dashboard_chat-1"]
    assert body["total"] == 1

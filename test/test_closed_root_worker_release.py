"""The workers of a closed Crew Mode root or Bot thread leave the live-owner set.

``_sync_dashboard_slots`` tells SessionManager which sessions have an open tab, and
the idle sweep releases the runtime of a session whose tab is gone. A worker's row
shows only under its root, so once the root's tab closes no tab shows the worker,
and it must not keep its runtime (and per-session MCP servers) until the idle timeout.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import crew_mode
from kiro_crew.dashboard import chat_utils_slots
from kiro_crew.dashboard.chat_utils_slots import _sync_dashboard_slots


def _slot(key: str, created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(key=key, linked_session_key="", _created_by=created_by)


def _published(monkeypatch: pytest.MonkeyPatch, *slots: SimpleNamespace) -> set[str]:
    monkeypatch.setattr(chat_utils_slots, "set_dashboard_surfaced", lambda _keys: None)
    state = SimpleNamespace(
        _slots={s.key: s for s in slots}, sessions=MagicMock(), push_listing_refresh=None
    )
    _sync_dashboard_slots(state)  # type: ignore[arg-type]
    (keys,), _ = state.sessions.set_active_dashboard_slots.call_args
    return set(keys)


@pytest.fixture
def _roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crew_mode, "known_roots", lambda: frozenset({"chat-root"}))
    monkeypatch.setattr(crew_mode, "known_mate_roots", lambda: frozenset({"chat-bot"}))
    monkeypatch.setattr(crew_mode, "mate_sessions_hidden", lambda cfg=None: True)


@pytest.mark.usefixtures("_roots")
def test_workers_of_a_closed_root_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    published = _published(
        monkeypatch,
        _slot("chat-w1", created_by="chat-root"),
        _slot("chat-w2", created_by="chat-bot"),
        _slot("chat-mine"),
        _slot("chat-other", created_by="cron:job-1"),
    )

    assert published == {"dashboard:chat-mine", "dashboard:chat-other"}


@pytest.mark.usefixtures("_roots")
def test_workers_of_an_open_root_stay_published(monkeypatch: pytest.MonkeyPatch) -> None:
    published = _published(
        monkeypatch, _slot("chat-root"), _slot("chat-w1", created_by="chat-root")
    )

    assert published == {"dashboard:chat-root", "dashboard:chat-w1"}


@pytest.mark.usefixtures("_roots")
def test_bot_workers_stay_published_while_bot_sessions_are_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the preview off a Bot's sessions are ordinary rows in the person's list.
    monkeypatch.setattr(crew_mode, "mate_sessions_hidden", lambda cfg=None: False)

    published = _published(monkeypatch, _slot("chat-w2", created_by="chat-bot"))

    assert published == {"dashboard:chat-w2"}

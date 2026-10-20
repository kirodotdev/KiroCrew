"""Fixtures and text helpers shared by ``test_slack_handler_coverage.py`` and its siblings.

``_clean_handler_state`` is the autouse fixture that clears every module-level cache the
handler mutates, before and after each test; every file that uses these helpers imports
it. ``owner`` makes ``U1`` the owner, ``slack`` is a ``MockSlackClient`` and ``sessions`` a
``SessionManager`` double; ``_texts`` joins the post, update, block and ephemeral texts and
``_reply`` narrows a handler reply to ``str``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import MockSlackClient
from kiro_crew.slack import handler as h


# ──────────────────────────────────────────────────────────────────────
# state hygiene
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_handler_state():
    """Clear every module-level cache the handler mutates.

    handler.py keeps its per-thread overrides, approvals and voice settings in
    module globals, so a leaked entry would make a later test order-dependent.
    """

    def _reset() -> None:
        h._pending_approvals.clear()
        h._linked_approvals.clear()
        h._trusted_sessions.clear()
        h._thread_agents.clear()
        h._thread_projects.clear()
        h._hydrated_sessions.clear()
        h._thread_temporary.clear()
        h._thread_incognito.clear()
        h._titled_threads.clear()
        h._review_drafts.clear()
        h._vc.sessions.clear()
        h._vc.voices.clear()
        h._vc.engines.clear()
        h._vc.rates.clear()
        h._vc.pitches.clear()
        h._vc.global_enabled = False
        h._cached_default_agent = None
        h._dashboard_state = None
        h._orch_cfg = None
        h._tracking_channels = set()

    _reset()
    yield
    _reset()


@pytest.fixture(name="owner")
def owner_fixture(monkeypatch):
    """Make ``U1`` the owner (and therefore an allowed user)."""
    monkeypatch.setattr(h, "_owner_id", "U1")
    return "U1"


@pytest.fixture(name="slack")
def slack_fixture():
    return MockSlackClient()


@pytest.fixture(name="sessions")
def sessions_fixture():
    """SessionManager double: only the members the command paths touch."""
    sm = MagicMock()
    sm.remove = AsyncMock()
    sm.destroy = AsyncMock()
    sm.discard_conversation = AsyncMock()
    sm.stop_turn = AsyncMock(return_value="soft")
    sm.try_acquire = AsyncMock(return_value=False)
    sm.has_session = MagicMock(return_value=False)
    sm.set_slack_link = MagicMock()
    sm.set_approval_policy = MagicMock()
    sm._session_map = None
    return sm


def _texts(slack_client: MockSlackClient) -> str:
    """Concatenate every text-bearing action so assertions read simply."""
    return "\n".join(
        str(a[1].get("text") or "")
        for a in slack_client.actions
        if a[0] in ("post", "update", "blocks", "ephemeral")
    )


def _reply(value: str | None) -> str:
    """Assert a handler returned a reply and narrow it to ``str``."""
    assert value is not None
    return value

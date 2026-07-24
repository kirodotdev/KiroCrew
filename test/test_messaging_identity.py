"""Unit tests for the shared per-turn identity publisher (messaging.identity).

These lock the publish semantics that every turn-running surface now delegates
to via ``publish_turn_identity`` (#232): publish with the session's host pid
and key, no-op when the pid is not yet known, and never let a failure break the
turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from kiro_crew.messaging import identity


class _Sessions:
    def __init__(self, pid: object) -> None:
        self._pid = pid

    def get_pid(self, key: str) -> object:
        return self._pid


def test_publishes_with_host_pid_and_key() -> None:
    sessions = _Sessions(4242)
    with patch.object(identity, "publish_session_pid") as pub:
        asyncio.run(
            identity.publish_turn_identity(sessions, "telegram:kirocrew:direct:7")
        )
        pub.assert_called_once_with(4242, "telegram:kirocrew:direct:7")


def test_no_publish_when_pid_unavailable() -> None:
    sessions = _Sessions(None)  # session not spawned yet -> get_pid None
    with patch.object(identity, "publish_session_pid") as pub:
        asyncio.run(identity.publish_turn_identity(sessions, "slack:kirocrew:C1:T1"))
        pub.assert_not_called()


def test_swallows_get_pid_error() -> None:
    sessions = MagicMock()
    sessions.get_pid.side_effect = RuntimeError("boom")
    with patch.object(identity, "publish_session_pid") as pub:
        # A get_pid / filesystem failure must never propagate out of a turn.
        asyncio.run(identity.publish_turn_identity(sessions, "discord:kirocrew:g:t"))
        pub.assert_not_called()

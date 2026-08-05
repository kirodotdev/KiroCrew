"""Tests for the override-expiry Slack notification gate (agent.notify_override_expiry)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.dashboard.server import (
    _dispatch_override_expiry_notification,
    _dispatch_owner_dm,
    _dm_owner,
)


def _make_state() -> MagicMock:
    state = MagicMock()
    state._background_tasks = set()
    return state


def _cfg(notify: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(notify_override_expiry=notify))


def test_dispatch_skipped_when_disabled() -> None:
    """notify_override_expiry=False skips the DM and schedules no task."""
    state = _make_state()
    factory = MagicMock()
    with patch("kiro_crew.dashboard.server.KiroCrewConfig.load", return_value=_cfg(False)):
        scheduled = _dispatch_override_expiry_notification(state, factory)

    assert scheduled is False
    assert state._background_tasks == set()
    factory.assert_not_called()


def test_dispatch_schedules_when_enabled() -> None:
    """notify_override_expiry=True schedules the DM task on the running loop."""

    async def _run() -> bool:
        state = _make_state()

        async def _noop() -> None:
            return None

        with patch(
            "kiro_crew.dashboard.server.KiroCrewConfig.load", return_value=_cfg(True)
        ):
            scheduled = _dispatch_override_expiry_notification(state, _noop)
        # A task was registered (tracked to prevent GC); drain it to completion.
        assert len(state._background_tasks) == 1
        await asyncio.gather(*list(state._background_tasks))
        return scheduled

    assert asyncio.run(_run()) is True


def test_dispatch_skipped_without_event_loop() -> None:
    """No running event loop → skipped gracefully (returns False)."""
    state = _make_state()
    factory = MagicMock()
    with patch("kiro_crew.dashboard.server.KiroCrewConfig.load", return_value=_cfg(True)):
        scheduled = _dispatch_override_expiry_notification(state, factory)

    assert scheduled is False
    assert state._background_tasks == set()


def _slack_state(slack_client=..., owner_id="U123") -> MagicMock:
    """State with an AsyncMock Slack client (open_dm → 'D1', post_message)."""
    state = _make_state()
    if slack_client is ...:
        slack_client = MagicMock()
        slack_client.open_dm = AsyncMock(return_value="D1")
        slack_client.post_message = AsyncMock()
    state.slack_client = slack_client
    state.owner_id = owner_id
    return state


class TestDmOwner:
    """_dm_owner — the single shared owner-DM exit point."""

    def test_posts_to_owner_dm(self) -> None:
        state = _slack_state()
        asyncio.run(_dm_owner(state, "hello owner"))
        state.slack_client.open_dm.assert_awaited_once_with("U123")
        state.slack_client.post_message.assert_awaited_once_with("D1", "hello owner")

    def test_noop_without_slack_client(self) -> None:
        state = _slack_state(slack_client=None)
        # Must not raise; nothing to assert beyond "no crash".
        asyncio.run(_dm_owner(state, "hi"))

    def test_noop_without_owner_id(self) -> None:
        state = _slack_state(owner_id="")
        asyncio.run(_dm_owner(state, "hi"))
        state.slack_client.open_dm.assert_not_awaited()

    def test_exception_is_swallowed(self) -> None:
        state = _slack_state()
        state.slack_client.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        # Best-effort: a Slack failure must not propagate.
        asyncio.run(_dm_owner(state, "hi"))

    def test_redacts_before_posting(self) -> None:
        """Defense-in-depth: text is redacted before it reaches Slack."""
        state = _slack_state()
        with (
            patch(
                "kiro_crew.dashboard.server.redact_exfiltration_urls",
                return_value=("no-exfil", []),
            ) as m_exfil,
            patch(
                "kiro_crew.dashboard.server.redact_credentials",
                return_value=("REDACTED", []),
            ) as m_cred,
        ):
            asyncio.run(_dm_owner(state, "leak https://evil.example AKIA..."))
        m_exfil.assert_called_once()
        m_cred.assert_called_once_with("no-exfil")
        state.slack_client.post_message.assert_awaited_once_with("D1", "REDACTED")


class TestDispatchOwnerDm:
    """_dispatch_owner_dm — fire-and-forget wrapper."""

    def test_schedules_tracked_task(self) -> None:
        async def _run() -> None:
            state = _slack_state()
            _dispatch_owner_dm(state, "warn")
            assert len(state._background_tasks) == 1
            await asyncio.gather(*list(state._background_tasks))
            # Task drained → the DM actually went out.
            state.slack_client.post_message.assert_awaited_once()
            # Done-callback removes the task from the tracking set.
            assert state._background_tasks == set()

        asyncio.run(_run())

    def test_noop_without_event_loop(self) -> None:
        """No running loop → skipped gracefully, no task scheduled."""
        state = _slack_state()
        _dispatch_owner_dm(state, "warn")
        assert state._background_tasks == set()

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

        with patch("kiro_crew.dashboard.server.KiroCrewConfig.load", return_value=_cfg(True)):
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
        state.channel_transports = {}
        # Must not raise; nothing to assert beyond "no crash".
        asyncio.run(_dm_owner(state, "hi"))

    def test_noop_without_owner_id(self) -> None:
        state = _slack_state(owner_id="")
        state.channel_transports = {}
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


def _channel_transport(*, available: bool = True, proactive: bool = True) -> MagicMock:
    """A transport double answering the reachability questions `_dm_owner` asks."""
    transport = MagicMock()
    transport.channel_type = "teams"
    transport.capabilities = SimpleNamespace(supports_proactive_send=proactive)
    transport.configured_targets = MagicMock(
        return_value=[
            SimpleNamespace(target_id="user:me@example.com", label="Teams DM", available=available)
        ]
    )
    transport.resolve_configured_target = AsyncMock(return_value=("conv-1", None))
    transport.send_message = AsyncMock(return_value="mid-1")
    return transport


class TestOwnerNoticeReachesNonSlackChannels:
    """An operator does not necessarily live in Slack.

    This notice used to no-op entirely without Slack, so an expiring unattended
    grant was INVISIBLE on a Teams-only, Discord-only or Telegram-only install —
    silence about a security grant lapsing is the one outcome it exists to prevent.
    """

    def test_a_slackless_install_still_gets_the_notice(self) -> None:
        state = _slack_state(slack_client=None)
        transport = _channel_transport()
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "your grant expired"))

        transport.send_message.assert_awaited_once_with("conv-1", "your grant expired", None)

    def test_slack_is_preferred_and_the_channel_is_not_also_notified(self) -> None:
        """One notice, not one per surface."""
        state = _slack_state()
        transport = _channel_transport()
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "hi"))

        state.slack_client.post_message.assert_awaited_once()
        transport.send_message.assert_not_awaited()

    def test_a_failing_slack_falls_through_to_the_channel(self) -> None:
        state = _slack_state()
        state.slack_client.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        transport = _channel_transport()
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "hi"))

        transport.send_message.assert_awaited_once()

    def test_an_unreachable_target_is_skipped_not_sent_to(self) -> None:
        """Reachability is the TRANSPORT's answer, so an unavailable row is not a target."""
        state = _slack_state(slack_client=None)
        transport = _channel_transport(available=False)
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "hi"))

        transport.send_message.assert_not_awaited()

    def test_a_channel_that_cannot_send_proactively_is_skipped(self) -> None:
        state = _slack_state(slack_client=None)
        transport = _channel_transport(proactive=False)
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "hi"))

        transport.send_message.assert_not_awaited()

    def test_a_channel_that_cannot_be_enumerated_does_not_hide_the_owner(self) -> None:
        """Independence at the ENUMERATION step: one broken transport must not silence
        the notice, because a raising `configured_targets` would otherwise look like a
        second candidate's worth of ambiguity and refuse everybody."""
        state = _slack_state(slack_client=None)
        broken = _channel_transport()
        broken.channel_type = "discord"
        broken.configured_targets = MagicMock(side_effect=RuntimeError("transport down"))
        working = _channel_transport()
        state.channel_transports = {"discord": broken, "teams": working}

        asyncio.run(_dm_owner(state, "hi"))

        working.send_message.assert_awaited_once()

    def test_two_channels_with_different_single_identities_are_two_people(self) -> None:
        """A per-channel "exactly one target" rule misses this, and it is the real case.

        Teams allow-listing alice and Discord allow-listing bob is two humans, each with a
        singleton list; delivering to both hands one of them the other's security state.
        The count therefore spans the whole install, not one channel.
        """
        state = _slack_state(slack_client=None)
        teams = _channel_transport()
        discord = _channel_transport()
        discord.channel_type = "discord"
        discord.configured_targets = MagicMock(
            return_value=[SimpleNamespace(target_id="user:bob", label="Discord DM", available=True)]
        )
        state.channel_transports = {"teams": teams, "discord": discord}

        asyncio.run(_dm_owner(state, "hi"))

        teams.send_message.assert_not_awaited()
        discord.send_message.assert_not_awaited()

    def test_a_channel_with_several_configured_targets_is_not_guessed_at(self) -> None:
        """This notice is the OPERATOR's security state, and an allow-list is not an owner.

        A Teams allow-list routinely holds several people; sending to the first reachable
        one hands one allow-listed human another's auto-approve state. Same premise as
        `/sessions`' owner-only rule: with more than one identity configured, refuse
        everybody rather than pick.
        """
        state = _slack_state(slack_client=None)
        transport = _channel_transport()
        transport.configured_targets = MagicMock(
            return_value=[
                SimpleNamespace(target_id="user:a@example.com", label="A", available=True),
                SimpleNamespace(target_id="user:b@example.com", label="B", available=True),
            ]
        )
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "hi"))

        transport.send_message.assert_not_awaited()

    def test_a_second_target_that_is_unreachable_still_blocks_the_guess(self) -> None:
        """Counted over ALL configured targets: one learned route out of three is a guess."""
        state = _slack_state(slack_client=None)
        transport = _channel_transport()
        transport.configured_targets = MagicMock(
            return_value=[
                SimpleNamespace(target_id="user:a@example.com", label="A", available=True),
                SimpleNamespace(target_id="user:b@example.com", label="B", available=False),
            ]
        )
        state.channel_transports = {"teams": transport}

        asyncio.run(_dm_owner(state, "hi"))

        transport.send_message.assert_not_awaited()

    def test_the_channel_leg_gets_the_REDACTED_text(self) -> None:
        state = _slack_state(slack_client=None)
        transport = _channel_transport()
        state.channel_transports = {"teams": transport}
        with (
            patch(
                "kiro_crew.dashboard.server.redact_exfiltration_urls",
                return_value=("no-exfil", []),
            ),
            patch(
                "kiro_crew.dashboard.server.redact_credentials",
                return_value=("REDACTED", []),
            ),
        ):
            asyncio.run(_dm_owner(state, "leak https://evil.example AKIA..."))

        transport.send_message.assert_awaited_once_with("conv-1", "REDACTED", None)


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

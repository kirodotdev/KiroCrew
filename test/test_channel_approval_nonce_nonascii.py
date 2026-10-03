"""A non-ASCII approval nonce is a mismatch, not an exception.

Every channel's widget path compares the client-echoed nonce against the one
minted for the live prompt. ``secrets.compare_digest`` raises ``TypeError`` on a
``str`` holding a non-ASCII character, so a press carrying one would turn what
the callers document as an audited, fail-closed refusal into an unaudited 500.
These tests pin each channel's press path — and the shared compare the channels
resolve through — at a clean refusal.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.discord.renderer import DiscordApprovalDecider
from kiro_crew.messaging.approval import PendingApprovals
from kiro_crew.slack.renderer import SlackApprovalDecider
from kiro_crew.teams.approvals import TeamsApprovalDecider, registry_key
from kiro_crew.telegram.renderer import TelegramApprovalDecider
from kiro_crew.webex.cards import LiveChoices

#: Any non-ASCII character raises inside a bare ``str``/``str`` compare.
_NON_ASCII = "é"
#: Built at runtime: a lone-surrogate literal in this file breaks pytest collection.
_LONE_SURROGATE = chr(0xDCFF)


async def _until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never met"
        await asyncio.sleep(0.01)


def _event(request_id: str | int = 1) -> SimpleNamespace:
    return SimpleNamespace(request_id=request_id, title="fs_write", tool_kind="write")


class TestSharedCompare:
    def test_the_shared_compare_is_total_over_every_str(self) -> None:
        from kiro_crew.messaging.renderer import nonce_eq

        assert nonce_eq("n1", "n1") is True
        assert nonce_eq("n1", _NON_ASCII) is False
        assert nonce_eq("n1", _LONE_SURROGATE) is False
        # Equal non-ASCII sides compare instead of raising: the helper's contract
        # is total over every str, not only over the ASCII the minter produces.
        assert nonce_eq(_NON_ASCII, _NON_ASCII) is True

    def test_an_empty_side_denies(self) -> None:
        from kiro_crew.messaging.renderer import nonce_eq

        assert nonce_eq("", "") is False
        assert nonce_eq("n1", "") is False
        assert nonce_eq("", "n1") is False


class TestChannelPressPaths:
    def test_slack_refuses_a_non_ascii_nonce(self) -> None:
        key = "slack:C9:t9:nonce-nonascii"
        SlackApprovalDecider._NONCES[key] = "n1"
        assert SlackApprovalDecider.nonce_matches(key, _NON_ASCII) is False
        assert SlackApprovalDecider.nonce_matches(key, _LONE_SURROGATE) is False
        assert SlackApprovalDecider.nonce_matches(key, "n1") is True
        SlackApprovalDecider._NONCES.pop(key, None)

    def test_telegram_refuses_a_non_ascii_nonce(self) -> None:
        key = "tg:chat:nonce-nonascii"
        TelegramApprovalDecider._NONCES[key] = "n1"
        assert TelegramApprovalDecider.nonce_matches(key, _NON_ASCII) is False
        assert TelegramApprovalDecider.nonce_matches(key, _LONE_SURROGATE) is False
        assert TelegramApprovalDecider.nonce_matches(key, "n1") is True
        TelegramApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_discord_refuses_a_non_ascii_nonce(self) -> None:
        key = "discord:channel:nonce-nonascii"
        future = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._NONCES[key] = "n1"
        DiscordApprovalDecider._REGISTRY[key] = future
        try:
            assert DiscordApprovalDecider.resolve_global(key, True, nonce=_NON_ASCII) is False
            assert DiscordApprovalDecider.resolve_global(key, True, nonce=_LONE_SURROGATE) is False
            # The refusal must come from the nonce compare, not from a missing
            # waiter: the pending decision is untouched by the malformed presses.
            assert future.done() is False
            assert DiscordApprovalDecider.resolve_global(key, True, nonce="n1") is True
            assert future.done() is True
        finally:
            DiscordApprovalDecider._NONCES.pop(key, None)
            DiscordApprovalDecider._REGISTRY.pop(key, None)

    @pytest.mark.asyncio
    async def test_teams_refuses_a_non_ascii_nonce(self) -> None:
        decider = TeamsApprovalDecider(session_key="teams:nonce-nonascii")
        decider.arm("7", "n1")  # on the loop: nonce + pending future + registry entry
        future = decider._futures["7"]
        try:
            assert decider.resolve("7", _NON_ASCII, approved=True) is False
            assert decider.resolve("7", _LONE_SURROGATE, approved=True) is False
            # The refusal must come from the nonce compare, not from a missing
            # waiter: the pending decision is untouched by the malformed presses.
            assert future.done() is False
            assert decider.resolve("7", "n1", approved=True) is True
            assert future.done() is True
        finally:
            decider.discard_reservations()
            TeamsApprovalDecider._REGISTRY.pop(registry_key(decider.session_key, "7"), None)

    def test_webex_refuses_a_non_ascii_nonce(self) -> None:
        live = LiveChoices()
        live.publish("s1-nonascii", "n1", ["A", "B"])
        assert live.take("s1-nonascii", "0", _NON_ASCII) == ""
        assert live.take("s1-nonascii", "0", _LONE_SURROGATE) == ""
        assert live.take("s1-nonascii", "0", "n1") == "A"

    @pytest.mark.asyncio
    async def test_shared_widget_resolve_refuses_a_non_ascii_nonce(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1-nonascii", _event()))
        await _until(lambda: pending.has_pending("s1-nonascii"))
        pending.reserve("s1-nonascii", 1)

        assert (
            pending.resolve("s1-nonascii", True, request_id=1, expected_nonce=_NON_ASCII) is False
        )
        assert (
            pending.resolve("s1-nonascii", True, request_id=1, expected_nonce=_LONE_SURROGATE)
            is False
        )
        assert task.done() is False

        pending.resolve("s1-nonascii", False, request_id=1)
        assert await task is False

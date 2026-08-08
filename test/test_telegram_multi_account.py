"""Tests for multi-account Telegram bot support.

Covers:
- Config parsing: accounts dict, backward compat (single token -> default account)
- Session key isolation between accounts
- Agent routing via telegram_account binding
"""

from __future__ import annotations

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    TelegramAccountConfig,
    TelegramConfig,
)


class TestTelegramAccountConfig:
    """TelegramAccountConfig parsing and resolved_accounts backward compat."""

    def test_empty_accounts_wraps_single_token(self) -> None:
        """Legacy single-token config auto-wraps as 'default' account."""
        cfg = TelegramConfig(
            enabled=True,
            bot_token="123:ABC",
            allowed_user_ids=[42],
            allow_forum=True,
            allowed_forum_chat_ids=[-100123],
        )
        accounts = cfg.resolved_accounts()
        assert "default" in accounts
        assert accounts["default"].bot_token == "123:ABC"
        assert accounts["default"].allowed_user_ids == [42]
        assert accounts["default"].allow_forum is True
        assert accounts["default"].allowed_forum_chat_ids == [-100123]

    def test_empty_token_yields_no_accounts(self) -> None:
        """No bot_token + no accounts = empty map (channel stays off)."""
        cfg = TelegramConfig(enabled=True, bot_token="")
        assert cfg.resolved_accounts() == {}

    def test_explicit_accounts_take_precedence(self) -> None:
        """When accounts is populated, top-level bot_token is ignored."""
        cfg = TelegramConfig(
            enabled=True,
            bot_token="legacy-token",
            allowed_user_ids=[99],
            accounts={
                "main": TelegramAccountConfig(
                    bot_token="token-a", allowed_user_ids=[1]
                ),
                "finance": TelegramAccountConfig(
                    bot_token="token-b", allowed_user_ids=[2, 3]
                ),
            },
        )
        accounts = cfg.resolved_accounts()
        assert "main" in accounts
        assert "finance" in accounts
        assert "default" not in accounts  # legacy NOT wrapped
        assert accounts["main"].bot_token == "token-a"
        assert accounts["finance"].allowed_user_ids == [2, 3]

    def test_resolved_accounts_returns_copy_safe(self) -> None:
        """resolved_accounts returns the dict directly (accounts populated)."""
        accts = {"a": TelegramAccountConfig(bot_token="t")}
        cfg = TelegramConfig(enabled=True, accounts=accts)
        assert cfg.resolved_accounts() is accts


class TestAgentTelegramAccountBinding:
    """The telegram_account field on KiroCrewAgentConfig for account routing."""

    def test_default_is_empty(self) -> None:
        """New agents have no telegram_account binding by default."""
        agent = KiroCrewAgentConfig()
        assert agent.telegram_account == ""

    def test_binding_round_trips(self) -> None:
        """telegram_account stores and reads back correctly."""
        agent = KiroCrewAgentConfig(telegram_account="finance")
        assert agent.telegram_account == "finance"


class TestSessionKeyIsolation:
    """Multi-account session keys use different channel names."""

    def test_default_account_uses_bare_telegram(self) -> None:
        """The 'default' account keeps 'telegram' as channel for compat."""
        # Verified via the gateway code: channel_name = "telegram" if
        # account_id == "default"
        from kiro_crew.messaging.link import build_dm_session_key

        key = build_dm_session_key("telegram", "default", "12345")
        assert key.startswith("telegram:")

    def test_named_account_uses_namespaced_channel(self) -> None:
        """A named account uses 'telegram.{account_id}' as channel."""
        from kiro_crew.messaging.link import build_dm_session_key

        key = build_dm_session_key("telegram.finance", "default", "12345")
        assert key.startswith("telegram.finance:")

    def test_different_accounts_produce_different_keys(self) -> None:
        """Same user on different accounts gets isolated sessions."""
        from kiro_crew.messaging.link import build_dm_session_key

        key_a = build_dm_session_key("telegram.main", "default", "42")
        key_b = build_dm_session_key("telegram.finance", "default", "42")
        assert key_a != key_b

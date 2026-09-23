"""The deprecated, inert ``telegram.accounts`` surface.

Multi-bot operation is withdrawn, but the config keys it introduced shipped in
release candidates. These tests lock in the two properties that withdrawal has
to preserve: an existing config still round-trips through save (so an operator's
tokens are not erased by the next write), and the withdrawal is announced rather
than silently dropping the operator's channel.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config import validation
from kiro_crew.config.loader import (
    CRED_TELEGRAM_BOT_TOKEN,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    _parse_telegram_accounts,
)
from kiro_crew.slack.gateway import GatewayOrchestrator

_ACCOUNTS_RAW = {
    "ops": {
        "bot_token": "111:ops-token",
        "allowed_user_ids": [7, 8],
        "allow_forum": True,
        "allowed_forum_chat_ids": [-100123],
        "soft_threshold_pct": 55,
    },
    "finance": {"bot_token": "222:finance-token", "allowed_user_ids": [9]},
}


class TestAccountsSurvivesSave:
    """A config written by an earlier release must not lose data on rewrite."""

    def test_accounts_round_trip_through_to_dict(self):
        cfg = KiroCrewConfig()
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)

        serialized = cfg.to_dict()["telegram"]["accounts"]

        assert set(serialized) == {"ops", "finance"}
        assert serialized["ops"]["bot_token"] == "111:ops-token"
        assert serialized["ops"]["allowed_user_ids"] == [7, 8]
        assert serialized["ops"]["allow_forum"] is True
        assert serialized["ops"]["allowed_forum_chat_ids"] == [-100123]
        assert serialized["ops"]["soft_threshold_pct"] == 55
        assert serialized["finance"]["bot_token"] == "222:finance-token"

    def test_save_preserves_accounts_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        cfg_path = tmp_path / "config.json"

        cfg = KiroCrewConfig()
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
            cfg.save()

        written = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert written["telegram"]["accounts"]["ops"]["bot_token"] == "111:ops-token"
        assert written["telegram"]["accounts"]["finance"]["bot_token"] == "222:finance-token"

    def test_agent_binding_round_trips(self):
        cfg = KiroCrewConfig()
        cfg.agents["ops-agent"] = KiroCrewAgentConfig(telegram_account="ops")

        assert cfg.to_dict()["agents"]["ops-agent"]["telegram_account"] == "ops"

    def test_entries_without_a_token_are_still_skipped(self):
        parsed = _parse_telegram_accounts({"ops": {"bot_token": ""}, "bad": "not-a-dict"})

        assert parsed == {}


class TestEmptyAccountsAreNotMaterialized:
    """An empty map protects no token, so it is neither written back nor announced.

    Before this, ``to_dict()`` emitted ``accounts: {}`` from the dataclass default
    into every saved config, and validation warned about the deprecated key on
    every launch -- to an operator who never wrote it and had nothing to migrate.
    """

    def test_to_dict_omits_an_empty_accounts_map(self):
        cfg = KiroCrewConfig()
        assert cfg.telegram.accounts == {}

        assert "accounts" not in cfg.to_dict()["telegram"]

    def test_save_writes_no_accounts_key_for_a_fresh_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        cfg_path = tmp_path / "config.json"

        with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
            KiroCrewConfig().save()

        written = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "accounts" not in written["telegram"]

    def test_an_empty_map_left_by_an_earlier_build_is_dropped_on_the_next_save(
        self, tmp_path, monkeypatch
    ):
        """The migration path: a config an older build materialized converges by itself."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {"agent": {"provider": "acp"}, "telegram": {"enabled": False, "accounts": {}}}
            )
        )
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(L, "config_path", lambda: cfg_path)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        cfg = KiroCrewConfig.load()
        cfg.save()

        written = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "accounts" not in written["telegram"]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_loading_an_empty_map_logs_no_deprecation(self, tmp_path, monkeypatch, caplog):
        """Until that save happens, the leftover key is not a deprecation either."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"agent": {"provider": "acp"}, "telegram": {"accounts": {}}})
        )
        monkeypatch.setattr(L, "config_path", lambda: cfg_path)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            KiroCrewConfig.load()

        assert not any(
            "deprecated field 'telegram.accounts'" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_a_populated_map_is_still_announced_as_deprecated(self, caplog):
        """The notice keeps firing for the operator who does have a token to move."""
        data = {"agent": {"provider": "acp"}, "telegram": {"accounts": dict(_ACCOUNTS_RAW)}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)

        notices = [
            r.getMessage()
            for r in caplog.records
            if "deprecated field 'telegram.accounts'" in r.getMessage()
        ]
        assert len(notices) == 1
        assert "telegram.bot_token" in notices[0]


class TestWithdrawalIsAnnounced:
    """The operator hears about a configured account that has stopped serving."""

    def _build(self, cfg):
        with patch.object(cfg, "load_credentials", return_value={}):
            return GatewayOrchestrator(cfg)

    def test_warns_and_names_every_account(self, caplog):
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            self._build(cfg)

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        accounts_warning = [w for w in warnings if "telegram.accounts is no longer served" in w]
        assert len(accounts_warning) == 1
        assert "finance" in accounts_warning[0]
        assert "ops" in accounts_warning[0]
        assert "telegram.bot_token" in accounts_warning[0]

    def test_warning_says_the_channel_stays_off(self, caplog):
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            orch = self._build(cfg)

        assert orch._telegram_enabled is False
        assert any("stays OFF" in r.getMessage() for r in caplog.records)

    def test_no_warning_without_accounts(self, caplog):
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.bot_token = "999:served"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            self._build(cfg)

        assert not any("telegram.accounts" in r.getMessage() for r in caplog.records)


class TestAccountsDoNotStartABot:
    """A shadowed top-level token must not come back to life on upgrade."""

    def test_accounts_alone_do_not_enable_telegram(self):
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.bot_token = ""
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)

        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg)

        assert orch._telegram_enabled is False

    def test_stale_top_level_token_stays_shadowed(self):
        """An account map shadowed the top-level token; withdrawal keeps it shadowed.

        Serving it would reopen a bot the operator stopped when they migrated,
        under whatever allow-list the top-level fields still carry.
        """
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.bot_token = "000:stale-top-level"
        cfg.telegram.allowed_user_ids = [111, 222]
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)

        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg)

        assert orch._telegram_enabled is False

    def test_stale_env_credential_stays_shadowed(self):
        """The env credential overrides cfg.bot_token, so it needs the same gate."""
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.accounts = _parse_telegram_accounts(_ACCOUNTS_RAW)

        with patch.object(
            cfg, "load_credentials", return_value={CRED_TELEGRAM_BOT_TOKEN: "000:from-env"}
        ):
            orch = GatewayOrchestrator(cfg)

        assert orch._telegram_enabled is False

    def test_channel_serves_once_the_accounts_block_is_removed(self):
        cfg = KiroCrewConfig()
        cfg.telegram.enabled = True
        cfg.telegram.bot_token = "999:served"
        cfg.telegram.accounts = {}

        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg)

        assert orch._telegram_enabled is True

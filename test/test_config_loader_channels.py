"""The messaging channel sections load fail-closed.

Slack reactions, tracking channels, enterprise ids and the Home tab cap; the
Feishu, Telegram and WeChat (``weixin``) allow-lists; the legacy ``wechat`` key
that still populates WeCom; and the shared ``messaging`` scope and reset fields. A
malformed value degrades to the safe default, never to an open gate. The WeCom
section's own field pins are in ``test_config_loader.py``.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

from config_loader_helpers import _load_from_dict, _load_from_dict_with_logs

from kiro_crew.config.loader import KiroCrewConfig


def test_slack_home_tab_sessions_per_kind_parsed_and_round_trips():
    # Regression: slack.home_tab_sessions_per_kind was declared + consumed (the
    # Slack Home Tab per-category cap) and emitted by to_dict(), but NOT parsed
    # in load(), so an operator value was silently ignored (stuck at 5) and
    # overwritten on the next save().
    loaded = _load_from_dict({"slack": {"home_tab_sessions_per_kind": 42}})
    assert loaded.slack.home_tab_sessions_per_kind == 42
    # Survives a to_dict() -> load() round-trip.
    reloaded = _load_from_dict(loaded.to_dict())
    assert reloaded.slack.home_tab_sessions_per_kind == 42


class TestReactionsEmptyStringFiltering:
    """Empty-string reaction values must be filtered out, preserving defaults."""

    def test_empty_string_reaction_filtered(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"slack": {"reactions": {"done": "", "error": "boom"}}}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        # Empty string should be dropped
        assert "done" not in cfg.slack.reactions
        # Non-empty value preserved
        assert cfg.slack.reactions["error"] == "boom"


class TestReactionsNullSuppression:
    """``null`` (JSON) / ``None`` (Python) values must be preserved as suppression sentinels."""

    def test_null_reaction_preserved(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"slack": {"reactions": {"done": None, "error": "boom"}}}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        # null should be preserved (distinct from absent key)
        assert "done" in cfg.slack.reactions
        assert cfg.slack.reactions["done"] is None
        # Non-empty value preserved
        assert cfg.slack.reactions["error"] == "boom"

    def test_non_string_non_null_filtered(self, tmp_path: Path) -> None:
        """Values that are neither strings nor null (e.g. numbers, bools) are dropped."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"slack": {"reactions": {"done": 42, "error": True, "tool": "ok"}}})
        )
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        assert "done" not in cfg.slack.reactions
        assert "error" not in cfg.slack.reactions
        assert cfg.slack.reactions["tool"] == "ok"


class TestTrackingChannelsValidation:
    """Tests for slack.tracking_channels validation and coercion."""

    def test_dict_format_passes_through(self) -> None:
        """Proper dict format with channel_id is accepted as-is."""
        data = {"slack": {"tracking_channels": [{"channel_id": "C0B371VEW5S", "name": "ops"}]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 1
        assert cfg.slack.tracking_channels[0]["channel_id"] == "C0B371VEW5S"

    def test_bare_string_coerced_to_dict(self) -> None:
        """Bare channel ID strings are auto-coerced to dict format."""
        data = {"slack": {"tracking_channels": ["C0B371VEW5S"]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 1
        assert cfg.slack.tracking_channels[0]["channel_id"] == "C0B371VEW5S"

    def test_bare_string_coercion_logs_warning(self) -> None:
        """Bare string coercion produces a warning log."""
        data = {"slack": {"tracking_channels": ["C0B371VEW5S", "C1234567890"]}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert len(cfg.slack.tracking_channels) == 2
        assert any("bare string" in msg for msg in logs)

    def test_invalid_entries_rejected(self) -> None:
        """Entries that are neither valid dicts nor channel-ID strings are dropped."""
        data = {"slack": {"tracking_channels": [123, None, {"name": "no-id"}]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 0

    def test_invalid_entries_log_warning(self) -> None:
        """Invalid entries produce a warning log."""
        data = {"slack": {"tracking_channels": [42, "not-a-channel-id"]}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert any("invalid entries" in msg for msg in logs)

    def test_mixed_format_all_valid_coerced(self) -> None:
        """Mix of dicts and bare strings both work."""
        data = {
            "slack": {
                "tracking_channels": [
                    {"channel_id": "C111", "name": "one"},
                    "C222",
                ]
            }
        }
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 2
        ids = {c["channel_id"] for c in cfg.slack.tracking_channels}
        assert ids == {"C111", "C222"}

    def test_empty_list_no_warnings(self) -> None:
        """Empty tracking_channels produces no warnings."""
        data = {"slack": {"tracking_channels": []}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert cfg.slack.tracking_channels == []
        assert not any("tracking_channels" in msg for msg in logs)


class TestAllowedEnterpriseIdsFiltering:
    """Tests for ``slack.allowed_enterprise_ids`` prefix filtering.

    The loader accepts both ``E``-prefix Slack enterprise IDs (org-level) and
    ``T``-prefix workspace IDs (Enterprise Grid child workspaces).  Other
    prefixes and non-string entries are dropped.
    """

    def test_e_prefix_enterprise_id_kept(self) -> None:
        """Standard E-prefix enterprise IDs (Slack org-level) are preserved."""
        data = {"slack": {"allowed_enterprise_ids": ["E015GUGD2V6"]}}
        cfg = _load_from_dict(data)
        assert "E015GUGD2V6" in cfg.slack.allowed_enterprise_ids

    def test_t_prefix_workspace_id_kept(self) -> None:
        """T-prefix workspace IDs (Enterprise Grid child workspaces) are preserved."""
        data = {"slack": {"allowed_enterprise_ids": ["T016NEJQWE9"]}}
        cfg = _load_from_dict(data)
        assert "T016NEJQWE9" in cfg.slack.allowed_enterprise_ids

    def test_mixed_e_and_t_prefix_kept(self) -> None:
        """Both E- and T-prefix IDs coexist in the allowlist."""
        data = {"slack": {"allowed_enterprise_ids": ["E015GUGD2V6", "T016NEJQWE9"]}}
        cfg = _load_from_dict(data)
        assert "E015GUGD2V6" in cfg.slack.allowed_enterprise_ids
        assert "T016NEJQWE9" in cfg.slack.allowed_enterprise_ids

    def test_invalid_prefix_dropped(self) -> None:
        """IDs with neither E nor T prefix are stripped."""
        data = {"slack": {"allowed_enterprise_ids": ["X999INVALID", "ABCDEF"]}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == []

    def test_non_string_entries_dropped(self) -> None:
        """Non-string entries (int, None) are dropped without raising."""
        data = {"slack": {"allowed_enterprise_ids": [42, None, "E015GUGD2V6"]}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == ["E015GUGD2V6"]

    def test_empty_list_yields_empty_allowlist(self) -> None:
        """Empty list produces an empty allowlist."""
        data = {"slack": {"allowed_enterprise_ids": []}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == []


class TestFeishuConfigCoercionFailsClosed:
    """A malformed feishu section must degrade to deny, never open a gate.

    Two layers hold this: the schema type check rejects a wrong-typed value and
    substitutes the field default, and the per-channel parse uses ``_safe_bool``
    / ``_coerce_opaque_str_ids`` rather than raw ``bool()`` and comprehensions.
    These tests pin the OBSERVABLE end state, so the guarantee survives either
    layer being refactored.
    """

    def test_string_false_does_not_open_the_group_gate(self) -> None:
        cfg = _load_from_dict(
            {
                "feishu": {
                    "enabled": "true",
                    "allow_group": "false",
                    "allowed_group_ids": ["oc_g1"],
                }
            }
        )
        assert cfg.feishu.allow_group is False
        assert cfg.feishu.enabled is False

    def test_real_bools_are_preserved(self) -> None:
        cfg = _load_from_dict({"feishu": {"enabled": True, "allow_group": True}})
        assert cfg.feishu.enabled is True
        assert cfg.feishu.allow_group is True

    def test_null_id_lists_do_not_crash_config_load(self) -> None:
        # A null or non-list value must yield the deny-everybody default rather
        # than reaching an iteration that would raise during gateway startup.
        cfg = _load_from_dict({"feishu": {"allowed_open_ids": None, "allowed_group_ids": "oc_g1"}})
        assert cfg.feishu.allowed_open_ids == []
        assert cfg.feishu.allowed_group_ids == []

    def test_opaque_ids_survive_and_are_deduped(self) -> None:
        # Feishu ids are opaque (ou_/oc_ prefixes), so a digit-only filter would
        # drop every entry and lock out the intended senders.
        cfg = _load_from_dict(
            {
                "feishu": {
                    "allowed_open_ids": ["ou_abc123", "  ou_abc123 ", "", "ou_zzz"],
                    "allowed_group_ids": ["oc_g1"],
                }
            }
        )
        assert cfg.feishu.allowed_open_ids == ["ou_abc123", "ou_zzz"]
        assert cfg.feishu.allowed_group_ids == ["oc_g1"]


class TestTelegramAllowedUserIdsGuard:
    """Finding: a non-list allowed_user_ids must not iterate char-by-char."""

    def test_string_value_yields_empty_not_char_list(self) -> None:
        cfg = _load_from_dict({"telegram": {"enabled": True, "allowed_user_ids": "12345"}})
        # A hand-edited string must NOT become [1, 2, 3, 4, 5]; treat non-list
        # as empty (fail closed).
        assert cfg.telegram.allowed_user_ids == []

    def test_real_list_is_preserved(self) -> None:
        cfg = _load_from_dict(
            {"telegram": {"enabled": True, "allowed_user_ids": [8743158320, -100]}}
        )
        assert cfg.telegram.allowed_user_ids == [8743158320, -100]

    def test_malformed_entries_skipped_not_crash(self) -> None:
        # "--100"/"1.5"/"abc" would raise in int() and crash config load; a
        # bool must not sneak in. Only clean base-10 ints survive.
        cfg = _load_from_dict(
            {
                "telegram": {
                    "enabled": True,
                    "allowed_user_ids": ["--100", "1.5", "abc", 42, "-7", True],
                }
            }
        )
        assert cfg.telegram.allowed_user_ids == [42, -7]

    def test_non_numeric_soft_threshold_defaults_not_crash(self) -> None:
        # "abc" would raise in int() and crash config load; must fall back to
        # the default (80) instead.
        cfg = _load_from_dict({"telegram": {"enabled": True, "soft_threshold_pct": "abc"}})
        assert cfg.telegram.soft_threshold_pct == 80


class TestMessagingConfigValidation:
    def test_normalizes_bad_scope_mode_and_clamps_resets(self) -> None:
        from kiro_crew.config.loader import MessagingConfig

        c = MessagingConfig(
            dm_scope="bogus",
            queue_mode="nope",
            idle_reset_minutes=-5,
            daily_reset_hour=99,
        )
        assert c.dm_scope == "per-channel-peer"
        assert c.queue_mode == "steer"
        assert c.idle_reset_minutes == 0
        assert c.daily_reset_hour == -1

    def test_keeps_valid_values(self) -> None:
        from kiro_crew.config.loader import MessagingConfig

        c = MessagingConfig(
            dm_scope="unified",
            queue_mode="queue",
            idle_reset_minutes=30,
            daily_reset_hour=4,
        )
        assert c.dm_scope == "unified"
        assert c.queue_mode == "queue"
        assert c.idle_reset_minutes == 30
        assert c.daily_reset_hour == 4

    def test_load_hydrates_messaging_fields_from_config(self) -> None:
        # Guards the load() gap: fields must be read from config.json, not just
        # defaulted. Without hydration these would all be the defaults.
        cfg = _load_from_dict(
            {
                "messaging": {
                    "dm_scope": "unified",
                    "queue_mode": "queue",
                    "idle_reset_minutes": 30,
                    "daily_reset_hour": 4,
                }
            }
        )
        assert cfg.messaging.dm_scope == "unified"
        assert cfg.messaging.queue_mode == "queue"
        assert cfg.messaging.idle_reset_minutes == 30
        assert cfg.messaging.daily_reset_hour == 4


def test_legacy_wechat_config_key_still_populates_wecom():
    """A config written before the wechat->wecom rename keeps its WeCom settings.

    load() falls back to the legacy
    "wechat" key so existing installs don't silently lose their allow-list /
    thresholds / enabled flag on upgrade.
    """
    cfg = _load_from_dict(
        {
            "wechat": {
                "enabled": True,
                "allowed_users": [{"userid": "zhangsan", "name": "Z"}],
                "hard_threshold_pct": 90,
            }
        }
    )
    assert cfg.wecom.enabled is True
    assert cfg.wecom.allowed_users == [{"userid": "zhangsan", "name": "Z"}]
    assert cfg.wecom.hard_threshold_pct == 90


class TestWeixinConfig(unittest.TestCase):
    """The WeChat allow-list must survive a config round trip.

    WeChat/iLink user IDs are opaque (``wxid_*``, ``<hex>@im.bot``), not numeric.
    A digit-only coercion silently emptied the list, and because dm_policy
    defaults to deny-by-default that locked out every intended sender.
    """

    def test_opaque_allowed_user_ids_survive_the_round_trip(self):
        cfg = _load_from_dict(
            {
                "weixin": {
                    "enabled": True,
                    "dm_policy": "allowlist",
                    "allowed_user_ids": [
                        "wxid_abc123",
                        "a5ace6fd482e@im.bot",
                        "12345",
                    ],
                }
            }
        )
        self.assertEqual(
            cfg.weixin.allowed_user_ids,
            ["wxid_abc123", "a5ace6fd482e@im.bot", "12345"],
        )

    def test_allowed_user_ids_still_fail_closed_on_bad_shape(self):
        cfg = _load_from_dict({"weixin": {"allowed_user_ids": "not-a-list"}})
        self.assertEqual(cfg.weixin.allowed_user_ids, [])

    def test_blank_and_duplicate_ids_are_dropped(self):
        cfg = _load_from_dict({"weixin": {"allowed_user_ids": ["  wxid_a  ", "wxid_a", "", "   "]}})
        self.assertEqual(cfg.weixin.allowed_user_ids, ["wxid_a"])

    def test_dm_policy_defaults_to_deny_by_default(self):
        cfg = _load_from_dict({"weixin": {"enabled": True}})
        self.assertEqual(cfg.weixin.dm_policy, "allowlist")

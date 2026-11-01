"""Coverage tests for the dashboard messaging handlers' notification and channel routes.

The notification log and channel routes, the Slack pins and reactions proxies, the small
helpers, the Teams config and activity routes, the env writer and the token validators,
the Slack and Webex config getters, the Slack manifest, message deletion, and the guards
on the module's exposed route handlers and its Slack timestamp regex. Handlers are driven
through the request double in ``handlers_messaging_coverage_helpers.py``; the subagent
lifecycle routes are tested in ``test_handlers_messaging_coverage.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from handlers_messaging_coverage_helpers import (
    _BAD_JSON,
    _payload,
    _Req,
    _run,
    _state,
)
from named_cases_helpers import named_cases

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod
from conftest import forget_env_at_teardown
from kiro_crew.secrets import SecretVault

# ── notifications ──


class TestNotificationRoutes:
    def test_list_returns_log_and_unread(self) -> None:
        state = _state(_notification_log=[{"ts": "1"}], _unread_count=3)
        resp = _run(mod.api_notifications, _Req(state))
        assert _payload(resp) == {"notifications": [{"ts": "1"}], "unread": 3}

    @pytest.mark.parametrize(
        "handler",
        [mod.api_notification_delete, mod.api_notification_ack, mod.api_notification_unack],
    )
    def test_ts_routes_reject_invalid_json(self, handler: Any) -> None:
        resp = _run(handler, _Req(_state(), _BAD_JSON))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.parametrize(
        "handler",
        [mod.api_notification_delete, mod.api_notification_ack, mod.api_notification_unack],
    )
    def test_ts_routes_require_ts(self, handler: Any) -> None:
        resp = _run(handler, _Req(_state(), {}))
        assert resp.status == 400
        assert _payload(resp)["error"] == "ts is required"

    def test_delete_forwards_to_state(self) -> None:
        state = _state(delete_notification=AsyncMock(return_value=True))
        assert _payload(_run(mod.api_notification_delete, _Req(state, {"ts": "1"}))) == {"ok": True}
        state.delete_notification.assert_awaited_once_with("1")

    def test_clear_forwards_to_state(self) -> None:
        state = _state(clear_notifications=AsyncMock())
        assert _payload(_run(mod.api_notifications_clear, _Req(state))) == {"ok": True}
        state.clear_notifications.assert_awaited_once()

    def test_ack_forwards_to_state(self) -> None:
        state = _state(ack_notification=AsyncMock(return_value=False))
        assert _payload(_run(mod.api_notification_ack, _Req(state, {"ts": "1"}))) == {"ok": False}

    def test_unack_of_a_cron_notification_also_unacks_the_job(self) -> None:
        state = _state(
            _notification_log=[{"ts": "1", "kind": "cron", "job_id": "j1"}],
            unack_notification=AsyncMock(return_value=True),
        )
        state.crons.unack_job_async = AsyncMock()
        assert _payload(_run(mod.api_notification_unack, _Req(state, {"ts": "1"})))["ok"] is True
        state.crons.unack_job_async.assert_awaited_once_with("j1")

    def test_unack_survives_a_busy_cron_store(self) -> None:
        from kiro_crew.cron import CronStoreBusy

        state = _state(
            _notification_log=[{"ts": "1", "kind": "cron", "job_id": "j1"}],
            unack_notification=AsyncMock(return_value=True),
        )
        state.crons.unack_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
        assert _payload(_run(mod.api_notification_unack, _Req(state, {"ts": "1"})))["ok"] is True

    def test_unack_survives_an_unreadable_cron_store(self) -> None:
        """The acked-item trim is best-effort, so a refused write must not 500.

        Twin of the busy test above. `unack_job_async` refuses BEFORE mutating
        once the store cannot be read, and that refusal is a new exception on
        this path -- untranslated it escapes the handler and aiohttp turns it
        into a 500, failing a notification unack that does not depend on the
        cron store at all.
        """
        from kiro_crew.cron import CronStoreUnreadable

        state = _state(
            _notification_log=[{"ts": "1", "kind": "cron", "job_id": "j1"}],
            unack_notification=AsyncMock(return_value=True),
        )
        state.crons.unack_job_async = AsyncMock(
            side_effect=CronStoreUnreadable("move the file aside")
        )
        assert _payload(_run(mod.api_notification_unack, _Req(state, {"ts": "1"})))["ok"] is True

    def test_ack_all_marks_every_entry_and_rewrites(self) -> None:
        log: list[dict[str, Any]] = [{"ts": "1", "acked": False}, {"ts": "2"}]
        state = _state(_notification_log=log, _rewrite_notifications_async=AsyncMock())
        assert _payload(_run(mod.api_notifications_ack_all, _Req(state))) == {"ok": True}
        assert all(n["acked"] for n in log)
        state._rewrite_notifications_async.assert_awaited_once()
        assert state.broadcast_ws.call_args.args == ("notification_ack", {"ts": "*"})


class TestNotificationChannels:
    def test_merges_registered_and_stored_channels(self) -> None:
        state = _state()
        state.notification_bus.channels.return_value = {"system.approval": "high"}
        state.notification_channel_settings.all_settings.return_value = {
            "app:demo.alerts": {"muted": True}
        }
        channels = _payload(_run(mod.api_notification_channels, _Req(state)))["channels"]
        by_name = {c["channel"]: c for c in channels}
        assert by_name["system.approval"]["protected"] is True
        assert by_name["system.approval"]["registered"] is True
        stale = by_name["app:demo.alerts"]
        assert stale["registered"] is False
        assert stale["default_priority"] is None
        assert stale["source"] == "app:demo"
        assert stale["settings"] == {"muted": True}


class TestNotificationChannelSettings:
    def _state_with_update(self, entry: Any = None, error: Any = None) -> Any:
        state = _state()
        if error is not None:
            state.notification_channel_settings.update.side_effect = error
        else:
            state.notification_channel_settings.update.return_value = entry or {"muted": True}
        return state

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_notification_channel_settings, _Req(_state(), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON body"

    @pytest.mark.parametrize("body", [[], None, "str"])
    def test_400_on_non_object_body(self, body: Any) -> None:
        resp = _run(mod.api_notification_channel_settings, _Req(_state(), body))
        assert resp.status == 400
        assert _payload(resp)["error"] == "body must be a JSON object"

    @pytest.mark.parametrize("channel", [None, "", "   ", 7])
    def test_400_without_a_channel(self, channel: Any) -> None:
        req = _Req(_state(), {"channel": channel})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "channel is required"

    def test_400_on_overlong_channel(self) -> None:
        req = _Req(_state(), {"channel": "c" * 257})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "channel name too long"

    def test_400_on_non_boolean_muted(self) -> None:
        req = _Req(_state(), {"channel": "a.b", "muted": "yes"})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "muted must be a boolean"

    def test_400_on_non_string_priority(self) -> None:
        req = _Req(_state(), {"channel": "a.b", "priority": 3})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "priority must be a string or null"

    def test_settings_error_becomes_400(self) -> None:
        from kiro_crew.notifications.settings import ChannelSettingsError

        state = self._state_with_update(error=ChannelSettingsError("cannot mute approval"))
        req = _Req(state, {"channel": "system.approval", "muted": True})
        resp = _run(mod.api_notification_channel_settings, req)
        assert resp.status == 400
        assert _payload(resp)["error"] == "cannot mute approval"

    def test_null_priority_clears_the_override(self) -> None:
        state = self._state_with_update(entry={"muted": False})
        req = _Req(state, {"channel": " a.b ", "priority": None})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp) == {"ok": True, "channel": "a.b", "settings": {"muted": False}}
        kwargs = state.notification_channel_settings.update.call_args.kwargs
        assert kwargs["clear_priority"] is True and kwargs["priority"] is None

    def test_explicit_priority_is_forwarded(self) -> None:
        state = self._state_with_update(entry={"priority": "high"})
        req = _Req(state, {"channel": "a.b", "priority": "high", "muted": True})
        assert _run(mod.api_notification_channel_settings, req).status == 200
        kwargs = state.notification_channel_settings.update.call_args.kwargs
        assert kwargs["priority"] == "high" and kwargs["clear_priority"] is False


# ── Slack pins / reactions proxies ──


def _track(monkeypatch, tracked: bool) -> None:
    monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: tracked)


class TestSlackPins:
    def test_skipped_without_a_slack_client(self) -> None:
        resp = _run(mod.api_slack_pins, _Req(_state(), {"action": "list"}))
        assert _payload(resp) == {"ok": True, "skipped": "no_slack"}

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=MagicMock()), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_unknown_action(self) -> None:
        req = _Req(_state(slack_client=MagicMock()), {"action": "toggle"})
        resp = _run(mod.api_slack_pins, req)
        assert resp.status == 400
        assert "action must be" in _payload(resp)["error"]

    @pytest.mark.parametrize("channel", [7, "", "not-a-channel"])
    def test_400_on_bad_channel(self, channel: Any) -> None:
        req = _Req(_state(slack_client=MagicMock()), {"action": "list", "channel": channel})
        resp = _run(mod.api_slack_pins, req)
        assert _payload(resp)["error"] == "invalid channel ID format"

    @pytest.mark.parametrize("ts", [None, "nope"])
    def test_400_on_bad_timestamp_for_mutations(self, ts: Any) -> None:
        body = {"action": "add", "channel": "C0123ABC456", "ts": ts}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 400
        assert "Slack timestamp" in _payload(resp)["error"]

    def test_403_for_untracked_channel(self, monkeypatch) -> None:
        _track(monkeypatch, False)
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 403
        assert "not in tracked channels" in _payload(resp)["error"]

    @pytest.mark.parametrize("action,method", [("add", "add_pin"), ("remove", "remove_pin")])
    def test_add_and_remove_call_through(self, monkeypatch, action: str, method: str) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        setattr(slack, method, AsyncMock())
        body = {"action": action, "channel": "C0123ABC456", "ts": "1712793600.123456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert _payload(resp) == {"ok": True}
        getattr(slack, method).assert_awaited_once_with("C0123ABC456", "1712793600.123456")

    def test_list_redacts_pinned_text(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.list_pins = AsyncMock(return_value=[{"text": "token xoxb-1234567890-secret"}])
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert "xoxb-1234567890-secret" not in _payload(resp)["pins"][0]["text"]

    def test_list_tolerates_a_pin_without_text(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.list_pins = AsyncMock(return_value=[{"ts": "1.0"}])
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert _payload(resp)["pins"][0]["text"] == ""

    def test_slack_failure_becomes_502(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.list_pins = AsyncMock(side_effect=RuntimeError("slack down"))
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert resp.status == 502
        assert _payload(resp)["error"] == "slack down"


class TestSlackReactions:
    _CHANNEL = "C0123ABC456"
    _TS = "1712793600.123456"

    def test_skipped_without_a_slack_client(self) -> None:
        resp = _run(mod.api_slack_reactions, _Req(_state(), {"action": "add"}))
        assert _payload(resp) == {"ok": True, "skipped": "no_slack"}

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_list_action(self) -> None:
        """Reactions has no list op -- only add/remove."""
        req = _Req(_state(slack_client=MagicMock()), {"action": "list"})
        resp = _run(mod.api_slack_reactions, req)
        assert _payload(resp)["error"] == "action must be 'add' or 'remove'"

    @pytest.mark.parametrize("channel", [7, "bogus"])
    def test_400_on_bad_channel(self, channel: Any) -> None:
        req = _Req(_state(slack_client=MagicMock()), {"action": "add", "channel": channel})
        resp = _run(mod.api_slack_reactions, req)
        assert _payload(resp)["error"] == "invalid channel ID format"

    def test_400_on_bad_timestamp(self) -> None:
        body = {"action": "add", "channel": self._CHANNEL, "ts": "now"}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), body))
        assert "Slack timestamp" in _payload(resp)["error"]

    @pytest.mark.parametrize("emoji", [7, "", "not valid!"])
    def test_400_on_bad_emoji(self, emoji: Any) -> None:
        body = {"action": "add", "channel": self._CHANNEL, "ts": self._TS, "emoji": emoji}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid emoji name"

    def test_403_for_untracked_channel(self, monkeypatch) -> None:
        _track(monkeypatch, False)
        body = {
            "action": "add",
            "channel": self._CHANNEL,
            "ts": self._TS,
            "emoji": "white_check_mark",
        }
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 403

    @pytest.mark.parametrize(
        "action,method", [("add", "add_reaction"), ("remove", "remove_reaction")]
    )
    def test_add_and_remove_raise_on_error(self, monkeypatch, action: str, method: str) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        setattr(slack, method, AsyncMock())
        body = {"action": action, "channel": self._CHANNEL, "ts": self._TS, "emoji": "eyes"}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=slack), body))
        assert _payload(resp) == {"ok": True}
        getattr(slack, method).assert_awaited_once_with(
            self._CHANNEL, self._TS, "eyes", raise_on_error=True
        )

    def test_slack_failure_becomes_502_with_redacted_error(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.add_reaction = AsyncMock(side_effect=RuntimeError("failed for xoxb-9999999999-abc"))
        body = {"action": "add", "channel": self._CHANNEL, "ts": self._TS, "emoji": "eyes"}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=slack), body))
        assert resp.status == 502
        assert "xoxb-9999999999-abc" not in _payload(resp)["error"]


# ── small helpers ──


class TestHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", ""),
            ("xoxb-1234-abcdwxyz", "xoxb-••••wxyz"),
            ("abcdefgh", "••••efgh"),
            ("ab", "••••"),
        ],
        # Safe display labels: without them pytest embeds the token-shaped
        # value in the test ID, which then lands in derived artifacts
        # (.test_durations, junit XML) and trips GitHub push protection.
        ids=["empty", "slack-token", "generic", "too-short"],
    )
    def test_mask_secret(self, value: str, expected: str) -> None:
        assert mod._mask_secret(value) == expected

    def test_clean_id_list_drops_blanks_and_normalizes(self) -> None:
        assert mod._clean_id_list([" a ", "", "b"], lambda v: True, "id") == ["a", "b"]

    def test_clean_id_list_rejects_a_non_list(self) -> None:
        with pytest.raises(ValueError, match="ids must be a list"):
            mod._clean_id_list("a", lambda v: True, "id")

    def test_clean_id_list_rejects_an_invalid_entry(self) -> None:
        with pytest.raises(ValueError, match="invalid id: bad"):
            mod._clean_id_list(["bad"], lambda v: v != "bad", "id")

    @pytest.mark.parametrize(
        "value,valid",
        [
            *named_cases(
                "test_is_valid_webex_email",
                "value,valid",
                [
                    ("kyle@example.com", True),
                    ("", False),
                    ("a" * 260, False),
                    ("no-at-sign", False),
                ],
            ),
            *named_cases(
                "test_is_valid_webex_email_shape_rules",
                "value,valid",
                [
                    ("kyle@example.com", True),
                    ("has space@example.com", False),
                    ("two@@example.com", False),
                    ("nodot@example", False),
                ],
            ),
        ],
    )
    def test_webex_email_validity(self, value: str, valid: bool) -> None:
        assert mod._is_valid_webex_email(value) is valid

    @pytest.mark.parametrize(
        "value,valid",
        [
            ("Zezhen.Xu-1@corp", True),
            ("", False),
            ("u" * 65, False),
            ("有中文", False),
            ("has space", False),
        ],
    )
    def test_is_valid_wecom_userid(self, value: str, valid: bool) -> None:
        assert mod._is_valid_wecom_userid(value) is valid

    @pytest.mark.parametrize(
        "value,valid",
        [
            ("kyle@example.com", True),
            ("00000000-0000-0000-0000-000000000000", True),
            ("", False),
            ("p" * 255, False),
            ("has space", False),
        ],
    )
    def test_is_valid_teams_principal(self, value: str, valid: bool) -> None:
        assert mod._is_valid_teams_principal(value) is valid

    def test_missing_scope_message_names_a_single_scope(self) -> None:
        msg = mod._missing_scope_message("users:read")
        assert "the users:read OAuth scope" in msg
        assert "add users:read to" in msg

    def test_missing_scope_message_pluralizes(self) -> None:
        msg = mod._missing_scope_message("users:read, pins:write")
        assert "OAuth scopes" in msg

    def test_missing_scope_message_falls_back_when_unnamed(self) -> None:
        msg = mod._missing_scope_message("")
        assert "requires an OAuth scope" in msg
        assert "add the required scope to" in msg

    def test_sanitize_blocks_redacts_keys_and_values(self) -> None:
        from kiro_crew.security import redact_credentials

        blocks = [{"text": {"type": "mrkdwn", "text": "tok xoxb-1234567890-secret"}}]
        out = mod._sanitize_blocks(blocks, redact_credentials)
        assert "xoxb-1234567890-secret" not in json.dumps(out)
        assert blocks[0]["text"]["text"].endswith("secret")  # input untouched

    def test_sanitize_blocks_truncates_deep_structures(self) -> None:
        from kiro_crew.security import redact_credentials

        deep: Any = "leaf"
        for _ in range(mod._MAX_WALK_DEPTH + 3):
            deep = {"child": deep}
        out = mod._sanitize_blocks([deep], redact_credentials)
        assert isinstance(out, list) and len(out) == 1

    def test_sanitize_blocks_caps_the_block_count(self) -> None:
        from kiro_crew.security import redact_credentials

        blocks = [{"i": i} for i in range(mod._MAX_BLOCKS + 5)]
        assert len(mod._sanitize_blocks(blocks, redact_credentials)) == mod._MAX_BLOCKS


class TestResolveSessionTarget:
    def test_rejects_any_target_other_than_origin(self) -> None:
        assert mod._resolve_session_target(_state(), "chat-1", "cron:j1") == (None, None)

    def test_rejects_a_non_cron_caller(self) -> None:
        assert mod._resolve_session_target(_state(), "origin", "dashboard:chat-1") == (None, None)

    def test_returns_none_for_an_unknown_job(self) -> None:
        state = _state()
        state.crons.list_jobs.return_value = []
        assert mod._resolve_session_target(state, "origin", "cron:j1") == (None, None)

    def test_returns_none_for_a_job_with_no_originating_session(self) -> None:
        state = _state()
        state.crons.list_jobs.return_value = [SimpleNamespace(id="j1", session_key="", name="n")]
        assert mod._resolve_session_target(state, "origin", "cron:j1") == (None, None)

    def test_strips_the_dashboard_prefix_from_the_slot_key(self) -> None:
        state = _state()
        state.crons.list_jobs.return_value = [
            SimpleNamespace(id="j1", session_key="dashboard:chat-3", name="Nightly")
        ]
        assert mod._resolve_session_target(state, "origin", "cron:j1:run7") == (
            "chat-3",
            "Nightly",
        )


# ── Teams config API ──


class TestTeamsConfigGet:
    def test_reports_status_and_read_only_flag(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
        monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        state = _state(teams_connected=True, teams_connect_error="x" * 200)
        resp = _run(mod.api_teams_config_get, _Req(state))
        data = _payload(resp)
        assert data["connected"] is True
        assert len(data["connect_error"]) == 120
        assert data["read_only"] is True
        assert data["configured"] is False
        assert data["allowed_emails"] == []


class TestTeamsConfigSave:
    def _save(self, monkeypatch, tmp_path: Path, body: Any) -> tuple[web.Response, Path, Path]:
        env = tmp_path / ".env"
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(loader, "env_path", lambda: env)
        monkeypatch.setattr(loader, "config_path", lambda: cfg)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "")

        async def _accept(app_id: str, app_password: str, tenant_id: str) -> None:
            """The save verifies a changed credential against Azure AD, which a
            unit test must never actually reach. The reject / unreachable /
            accepted branches are covered in test_teams_config_handlers.py."""
            return None

        monkeypatch.setattr(mod, "_validate_teams_app_credentials", _accept)
        return _run(mod.api_teams_config_save, _Req(_state(), body)), env, cfg

    def test_403_from_remote_sessions(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        resp = _run(mod.api_teams_config_save, _Req(_state(), {"enabled": True}))
        assert resp.status == 403
        assert "read-only" in _payload(resp)["error"]

    def test_400_on_invalid_json(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, _BAD_JSON)
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_non_object_body(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, [1, 2])
        assert _payload(resp)["error"] == "body must be an object"

    def test_400_on_non_boolean_clear_flag(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"app_password_clear": "yes"})
        assert _payload(resp)["error"] == "app_password_clear must be a boolean"

    def test_400_on_whitespace_in_the_secret(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"app_password": "a b"})
        assert _payload(resp)["error"] == "app_password must not contain whitespace"

    def test_400_on_non_boolean_enabled(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"enabled": "yes"})
        assert _payload(resp)["error"] == "enabled must be a boolean"

    def test_400_on_non_string_app_id(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"app_id": 7})
        assert _payload(resp)["error"] == "app_id must be a string"

    def test_400_on_whitespace_in_tenant_id(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"tenant_id": "a b"})
        assert _payload(resp)["error"] == "tenant_id must not contain whitespace"

    def test_400_on_an_invalid_principal(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"allowed_emails": ["has space"]})
        assert "invalid principal" in _payload(resp)["error"]

    def test_500_on_corrupt_config_json(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
        resp, _, _ = self._save(monkeypatch, tmp_path, {"enabled": True})
        assert resp.status == 500
        assert _payload(resp)["error"] == "config.json is corrupt"

    def test_persists_secret_to_env_and_config_to_json(self, monkeypatch, tmp_path: Path) -> None:
        body = {
            "enabled": True,
            "app_id": "  app-1  ",
            "tenant_id": "tenant-1",
            "allowed_emails": ["kyle@example.com"],
            "app_password": "MICROSOFT_APP_PASSWORD=super-secret",
        }
        resp, env, cfg = self._save(monkeypatch, tmp_path, body)
        assert _payload(resp)["ok"] is True
        assert _payload(resp)["restart_required"] is True
        assert "MICROSOFT_APP_PASSWORD=secret://MICROSOFT_APP_PASSWORD" in env.read_text(
            encoding="utf-8"
        )
        stored = SecretVault(env.parent).get("MICROSOFT_APP_PASSWORD")
        assert stored is not None and stored.reveal() == "super-secret"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["teams"]["app_id"] == "app-1"
        assert data["teams"]["allowed_emails"] == ["kyle@example.com"]
        assert "app_password" not in json.dumps(data["teams"]).replace('"app_password"', "")

    def test_clear_flag_deletes_the_stored_secret(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "# keep me\nMICROSOFT_APP_PASSWORD=old\nOTHER=1\n", encoding="utf-8"
        )
        resp, env, _ = self._save(monkeypatch, tmp_path, {"app_password_clear": True})
        assert resp.status == 200
        text = env.read_text(encoding="utf-8")
        assert "MICROSOFT_APP_PASSWORD" not in text
        assert "# keep me" in text and "OTHER=1" in text

    def test_purges_a_legacy_plaintext_secret_from_config_json(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # The purge is safe only when the credential is also held in .env or being
        # written to .env this save (Finding 1: purging the sole copy on a
        # metadata-only save would erase the credential). Scenario: password in
        # BOTH config.json AND os.environ (simulating a migrated, leaked copy).
        env = tmp_path / ".env"
        env.write_text("MICROSOFT_APP_PASSWORD=leaked\n", encoding="utf-8")
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"teams": {"app_password": "leaked"}}), encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        # The credential is held in os.environ (safe to purge the config copy).
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "leaked")

        async def _accept(*a, **kw):
            return None

        monkeypatch.setattr(mod, "_validate_teams_app_credentials", _accept)
        resp = _run(mod.api_teams_config_save, _Req(_state(), {"enabled": True}))
        assert resp.status == 200
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert (
            data["teams"]["app_password"] == ""
        ), "When password is also in os.environ/.env, purge the legacy config.json copy"

    def test_does_not_purge_legacy_secret_that_is_the_sole_credential_copy(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # Finding 1 regression: app_password ONLY in legacy config.json (not in
        # .env or os.environ) must survive a metadata-only save.
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"app_password": "legacy-only"}}), encoding="utf-8"
        )
        resp, _, cfg = self._save(monkeypatch, tmp_path, {"enabled": True})
        # _save sets MICROSOFT_APP_PASSWORD="" so os.environ fallback is empty.
        assert resp.status == 200
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["teams"].get("app_password") == "legacy-only", (
            "Password that lives ONLY in legacy config.json must survive a "
            "metadata-only save (Finding 1)"
        )

    def test_no_op_save_reports_no_restart_needed(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"enabled": False}}), encoding="utf-8"
        )
        resp, _, _ = self._save(monkeypatch, tmp_path, {"enabled": False})
        assert _payload(resp)["restart_required"] is False

    def test_replaces_a_non_dict_teams_section(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(json.dumps({"teams": "oops"}), encoding="utf-8")
        resp, _, cfg = self._save(monkeypatch, tmp_path, {"enabled": True})
        assert resp.status == 200
        assert json.loads(cfg.read_text(encoding="utf-8"))["teams"]["enabled"] is True

    def test_clear_config_write_failure_does_not_leave_env_cleared(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """On a CLEAR the config.json purge runs BEFORE the .env delete. If the
        config write fails the .env must be untouched — otherwise a restart would
        fall back to any legacy config.json app_password, resurrecting the
        credential the operator asked to clear."""
        env = tmp_path / ".env"
        cfg_path = tmp_path / "config.json"
        env.write_text("MICROSOFT_APP_PASSWORD=live-pw\n", encoding="utf-8")
        cfg_path.write_text(json.dumps({"teams": {"app_password": "legacy-pw"}}), encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "")

        def _boom(*_a, **_k):
            raise OSError("disk full during config write")

        # The save writes through ``update_config_locked``, whose file write is
        # the loader's ``write_config_atomically``; failing THAT is the disk-full.
        monkeypatch.setattr(loader, "write_config_atomically", _boom)
        try:
            _run(mod.api_teams_config_save, _Req(_state(), {"app_password_clear": True}))
        except Exception:
            pass
        assert "MICROSOFT_APP_PASSWORD=live-pw" in env.read_text(encoding="utf-8")


class TestTeamsActivity:
    def test_503_when_the_channel_is_not_enabled(self) -> None:
        req = _Req(_state(teams_on_activity=None))
        resp = _run(mod.api_teams_activity, req)
        assert resp.status == 503
        assert resp.text == "Teams channel not enabled"


# ── env writer ──


class TestWriteEnvUpdates:
    def test_appends_new_keys_and_preserves_comments(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# header\nA=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"B": "2"})
        assert env.read_text(encoding="utf-8").splitlines() == ["# header", "A=1", "B=2"]

    def test_replaces_an_existing_key_in_place(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\nB=2\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"A": "9"})
        assert env.read_text(encoding="utf-8").splitlines() == ["A=9", "B=2"]

    def test_creates_the_file_when_absent(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / "nested" / ".env"
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"A": "1"})
        assert env.read_text(encoding="utf-8") == "A=1\n"

    def test_deleting_the_only_key_leaves_an_empty_file(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"A": None})
        assert env.read_text(encoding="utf-8") == ""

    def test_a_none_value_for_an_absent_key_is_a_no_op(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"MISSING": None})
        assert env.read_text(encoding="utf-8") == "A=1\n"

    def test_a_failed_permission_lockdown_is_warned_not_raised(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from kiro_crew import platform_compat

        env = tmp_path / ".env"
        monkeypatch.setattr(loader, "env_path", lambda: env)

        def _boom(path: Any) -> None:
            raise OSError("chmod refused")

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)
        mod._write_env_updates({"A": "1"})
        assert env.read_text(encoding="utf-8") == "A=1\n"

    def test_the_owner_lockdown_precedes_any_content_byte(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The ordering IS the security property: fchmod_safe is a no-op on
        Windows, so a lockdown applied after the write leaves the tokens
        readable under the directory-inherited DACL for the whole write. The
        shared helper restricts the empty temp file first; assert the sequence
        through the same os.write seam the helper's own ordering test uses."""
        import os as _os

        from kiro_crew import platform_compat

        env = tmp_path / ".env"
        monkeypatch.setattr(loader, "env_path", lambda: env)
        # An existing vault, so the recorded writes are the store and the .env.
        SecretVault(tmp_path).set_sync("SLACK_BOT_TOKEN", "placeholder-old")

        events: list[str] = []
        real_restrict = platform_compat.restrict_to_owner
        real_os_write = _os.write

        def _spy(path: Any) -> None:
            events.append("restrict")
            return real_restrict(path)

        def _tracking_write(fd: int, data: Any) -> int:
            events.append("write")
            return real_os_write(fd, data)

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _spy)
        monkeypatch.setattr(_os, "write", _tracking_write)

        mod._write_env_updates({"SLACK_BOT_TOKEN": "xoxb-secret"})

        # Vault store, then .env: each locked down before its first content byte.
        assert events == ["restrict", "write", "restrict", "write"], events
        assert env.read_text(encoding="utf-8") == "SLACK_BOT_TOKEN=secret://SLACK_BOT_TOKEN\n"

    def test_aborts_when_shared_env_lock_is_held(self, monkeypatch, tmp_path: Path) -> None:
        """A channel/token save serializes on the SAME .env.lock the importer
        and the Weixin handler use, so it aborts (rather than racing the commit)
        when another writer holds the lock — and leaves .env untouched."""
        import os

        from kiro_crew import platform_compat
        from kiro_crew.secrets.migrate import _env_lock_path

        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)

        # Simulate the importer holding the shared advisory lock.
        lock_path = _env_lock_path(env)
        held_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(held_fd, exclusive=True)
        try:
            with pytest.raises(OSError):
                mod._write_env_updates({"B": "2"})
            # .env is untouched — the aborted save did not partially write.
            assert env.read_text(encoding="utf-8") == "A=1\n"
        finally:
            platform_compat.release_lock(held_fd)
            os.close(held_fd)


class _FakeResponse:
    """Minimal aiohttp response double: ``status`` plus an awaitable ``json()``."""

    def __init__(self, status: int, payload: Any = None, *, json_raises: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._json_raises = json_raises

    async def json(self, content_type: Any = None) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class _FakeGetCtx:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeResponse:
        return self._resp

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _fake_http(monkeypatch, resp: _FakeResponse) -> None:
    """Replace ``aiohttp.ClientSession`` so token validators make no real call."""
    import aiohttp as _aiohttp

    class _Session:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def get(self, *a: Any, **kw: Any) -> _FakeGetCtx:
            return _FakeGetCtx(resp)

    monkeypatch.setattr(_aiohttp, "ClientSession", _Session)


class TestTokenValidators:
    def test_discord_accepts_a_2xx(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200))
        assert asyncio.run(mod._validate_discord_token("tok")) is None

    def test_discord_surfaces_the_api_message(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(401, {"message": "401: Unauthorized"}))
        assert asyncio.run(mod._validate_discord_token("tok")) == "401: Unauthorized"

    def test_discord_falls_back_to_the_status_code(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(503, json_raises=True))
        assert asyncio.run(mod._validate_discord_token("tok")) == "HTTP 503"

    def test_telegram_accepts_an_ok_envelope(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200, {"ok": True}))
        assert asyncio.run(mod._validate_telegram_token("tok")) is None

    def test_telegram_surfaces_the_description(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(401, {"ok": False, "description": "Unauthorized"}))
        assert asyncio.run(mod._validate_telegram_token("tok")) == "Unauthorized"

    def test_telegram_falls_back_to_rejected(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200, ["not", "a", "dict"]))
        assert asyncio.run(mod._validate_telegram_token("tok")) == "rejected"

    def test_webex_accepts_a_2xx(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200))
        assert asyncio.run(mod._validate_webex_token("tok")) is None

    @pytest.mark.parametrize("status", [401, 403])
    def test_webex_rejects_an_unauthorized_token(self, monkeypatch, status: int) -> None:
        _fake_http(monkeypatch, _FakeResponse(status))
        assert asyncio.run(mod._validate_webex_token("tok")) == f"invalid_token (http {status})"

    def test_webex_treats_5xx_as_unverifiable(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(503))
        with pytest.raises(RuntimeError, match="webex verify http 503"):
            asyncio.run(mod._validate_webex_token("tok"))

    def _fake_slack(self, monkeypatch, error: Any = None) -> list[str]:
        """Patch AsyncWebClient; returns the list of methods the validator called."""
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web import async_client

        calls: list[str] = []

        class _Client:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def auth_test(self) -> None:
                calls.append("auth_test")
                if error is not None:
                    raise SlackApiError("nope", error)

            async def apps_connections_open(self, app_token: str | None = None) -> None:
                calls.append("apps_connections_open")
                if error is not None:
                    raise SlackApiError("nope", error)

        monkeypatch.setattr(async_client, "AsyncWebClient", _Client)
        return calls

    def test_slack_bot_token_uses_auth_test(self, monkeypatch) -> None:
        calls = self._fake_slack(monkeypatch)
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "xoxb-1")) is None
        assert calls == ["auth_test"]

    def test_slack_app_token_uses_connections_open(self, monkeypatch) -> None:
        calls = self._fake_slack(monkeypatch)
        assert asyncio.run(mod._validate_slack_token("SLACK_APP_TOKEN", "xapp-1")) is None
        assert calls == ["apps_connections_open"]

    def test_slack_returns_the_api_error_code(self, monkeypatch) -> None:
        self._fake_slack(monkeypatch, error={"error": "invalid_auth"})
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "x")) == "invalid_auth"

    def test_slack_falls_back_to_rejected_for_an_empty_error(self, monkeypatch) -> None:
        self._fake_slack(monkeypatch, error={"error": ""})
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "x")) == "rejected"

    def test_slack_falls_back_when_the_response_is_unreadable(self, monkeypatch) -> None:
        self._fake_slack(monkeypatch, error="not-a-mapping")
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "x")) == "rejected"


class TestConfigGetHandlers:
    def _isolate(self, monkeypatch, tmp_path: Path, env: str, cfg: str) -> None:
        env_file = tmp_path / ".env"
        cfg_file = tmp_path / "config.json"
        env_file.write_text(env, encoding="utf-8")
        cfg_file.write_text(cfg, encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env_file)
        monkeypatch.setattr(loader, "config_path", lambda: cfg_file)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    def test_slack_config_get_masks_the_tokens(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        # load_credentials() propagates .env values into os.environ via
        # setdefault() so spawned children inherit them (real, deliberate
        # behavior). A bare monkeypatch.delenv(raising=False) is NOT enough here:
        # when the variable is absent pytest records no undo, so the OWNER_ID this
        # load writes survived the test and reached later ones on the worker
        # (observed in a full run). The helper records the pre-test state --
        # present or absent -- as the undo, so teardown restores exactly that.
        forget_env_at_teardown(monkeypatch, "OWNER_ID")
        self._isolate(
            monkeypatch,
            tmp_path,
            "SLACK_BOT_TOKEN=xoxb-1234567890-wxyz\nOWNER_ID=U_OWNER\n",
            json.dumps({"slack": {"enabled": True}}),
        )
        state = _state(slack_socket_connected=False, slack_connect_error="invalid_auth")
        data = _payload(_run(mod.api_slack_config_get, _Req(state)))
        assert data["bot_token_set"] is True
        assert data["bot_token_preview"].endswith("wxyz")
        assert "xoxb-1234567890-wxyz" not in json.dumps(data)
        assert data["app_token_set"] is False
        assert data["read_only"] is False

    def test_webex_config_get_masks_the_token(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)
        self._isolate(
            monkeypatch,
            tmp_path,
            "WEBEX_BOT_TOKEN=webex-abcdwxyz\n",
            json.dumps({"webex": {"enabled": True, "allowed_emails": ["kyle@example.com"]}}),
        )
        state = _state(webex_connected=True, webex_connect_error="")
        data = _payload(_run(mod.api_webex_config_get, _Req(state)))
        assert data["configured"] is True
        assert data["bot_token_preview"].endswith("wxyz")
        assert "webex-abcdwxyz" not in json.dumps(data)
        assert data["allowed_emails"] == ["kyle@example.com"]


class TestSlackManifest:
    def test_400_on_an_invalid_alias(self) -> None:
        resp = _run(mod.api_slack_manifest, _Req(_state(), query={"alias": "bad alias"}))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid alias"

    def test_renders_the_template_and_builds_a_create_url(self) -> None:
        resp = _run(mod.api_slack_manifest, _Req(_state(), query={"alias": "zezhen"}))
        data = _payload(resp)
        assert data["alias"] == "zezhen"
        assert "{{ALIAS}}" not in data["manifest"]
        assert data["create_url"].startswith("https://api.slack.com/apps?new_app=1&manifest_yaml=")

    def test_defaults_to_a_non_identifying_alias(self) -> None:
        assert _payload(_run(mod.api_slack_manifest, _Req(_state())))["alias"] == "kirocrew"


class _FakeBus:
    """Command-bus double: records calls, returns/raises what the test asks for."""

    def __init__(
        self,
        *,
        submit: Any = None,
        drain: Any = None,
        complete: bool = True,
    ) -> None:
        self._submit = submit if submit is not None else {"id": "c1", "ok": True, "result": 1}
        self._drain = drain
        self._complete = complete
        self.submit_calls: list[tuple[Any, ...]] = []
        self.drain_calls: list[tuple[Any, int]] = []
        self.complete_calls: list[tuple[str, bool, Any, Any]] = []

    async def submit(self, session_key: str, op: str, args: dict, *, timeout_ms: int) -> Any:
        self.submit_calls.append((session_key, op, args, timeout_ms))
        if isinstance(self._submit, BaseException):
            raise self._submit
        return self._submit

    async def drain(self, session_keys: list[str], *, wait_ms: int) -> Any:
        self.drain_calls.append((session_keys, wait_ms))
        return self._drain

    async def complete(self, cid: str, ok: bool, *, result: Any = None, error: Any = None) -> bool:
        self.complete_calls.append((cid, ok, result, error))
        return self._complete


def _install_bus(monkeypatch, bus: _FakeBus) -> _FakeBus:
    monkeypatch.setattr(mod, "get_command_bus", lambda: bus)
    return bus


_INTERNAL = {"internal_auth": True}


class TestDeleteMessage:
    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_delete_message, _Req(_state(), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.parametrize("body", [{"channel": "", "ts": "1.0"}, {"channel": "C1", "ts": ""}])
    def test_400_without_channel_and_ts(self, body: dict[str, str]) -> None:
        resp = _run(mod.api_delete_message, _Req(_state(), body))
        assert resp.status == 400
        assert _payload(resp)["error"] == "channel and ts required"

    def test_503_without_a_slack_client(self) -> None:
        body = {"channel": "C1", "ts": "1.0"}
        resp = _run(mod.api_delete_message, _Req(_state(), body))
        assert resp.status == 503
        assert _payload(resp)["error"] == "Slack not connected"


def test_module_exposes_every_route_handler_under_test() -> None:
    """Guard against a rename silently skipping a whole block of these tests."""
    for name in (
        "api_spawn",
        "api_spawn_continue",
        "api_spawn_steer",
        "api_spawn_release",
        "api_spawn_lost",
        "api_spawn_mark_collected",
        "api_spawn_status",
        "api_spawn_list",
        "api_spawn_retry",
        "api_spawn_delete",
        "api_notification_channels",
        "api_notification_channel_settings",
        "api_slack_pins",
        "api_slack_reactions",
        "api_browser_token_put",
        "api_browser_install_get",
        "api_browser_install_start",
        "api_browser_view_get",
        "api_browser_view_start",
        "api_browser_open",
        "api_teams_config_save",
    ):
        assert callable(getattr(mod, name)), name


def test_slack_timestamp_contract_matches_the_handler_regex() -> None:
    """The pins/reactions ts guard is a literal ``\\d+\\.\\d+`` shape check."""
    assert re.match(r"^\d+\.\d+$", "1712793600.123456")
    assert not re.match(r"^\d+\.\d+$", "1712793600")

"""Plan-credit alerts: one notice per crossed percentage, once per plan period.

``telemetry.plan_alert_pcts`` is empty by default, which is off. The decision is a pure
function of the reading, the percentages and what was already told, so most cases run
on :func:`plan_credit_alerts.decide`; the rest cover the stored record, the notification
that is pushed, the setting's parsing, and the two refresh routes that run the check.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.config.loader_builders import _percent_list
from kiro_crew.config.sections_telemetry import TelemetryConfig
from kiro_crew.dashboard.handlers import plan_credit_alerts

PCTS = [50.0, 80.0, 100.0]


def _usage(used: float, plan: float = 100.0, **extra):
    return {"credits_used": used, "credits_plan": plan, "resets": "2026-12-01", **extra}


class TestDecide:
    def test_below_every_percentage_says_nothing(self):
        text, kept = plan_credit_alerts.decide(_usage(49.9), PCTS, {})
        assert text is None
        assert kept == {"period": "2026-12-01", "told": []}

    def test_crossing_a_percentage_names_it_and_remembers_it(self):
        text, kept = plan_credit_alerts.decide(_usage(52), PCTS, {})
        assert text is not None and "52%" in text and "50%" in text
        assert kept["told"] == [50.0]

    def test_the_same_crossing_is_told_once(self):
        _, kept = plan_credit_alerts.decide(_usage(52), PCTS, {})
        text, again = plan_credit_alerts.decide(_usage(60), PCTS, kept)
        assert text is None and again == kept

    def test_a_jump_over_two_percentages_is_one_notice_for_the_highest(self):
        text, kept = plan_credit_alerts.decide(_usage(85), PCTS, {})
        assert text is not None and "80%" in text and "50%" not in text
        assert kept["told"] == [50.0, 80.0]

    def test_the_next_percentage_is_told_later(self):
        _, kept = plan_credit_alerts.decide(_usage(52), PCTS, {})
        text, kept = plan_credit_alerts.decide(_usage(81), PCTS, kept)
        assert text is not None and "80%" in text
        assert kept["told"] == [50.0, 80.0]

    def test_a_new_plan_period_starts_the_list_over(self):
        _, kept = plan_credit_alerts.decide(_usage(95), PCTS, {})
        text, _ = plan_credit_alerts.decide(_usage(55, resets="2027-01-01"), PCTS, kept)
        assert text is not None and "50%" in text

    def test_used_credits_falling_back_makes_a_percentage_tellable_again(self):
        _, kept = plan_credit_alerts.decide(_usage(85), PCTS, {})
        _, kept = plan_credit_alerts.decide(_usage(10), PCTS, kept)
        assert kept["told"] == []
        text, _ = plan_credit_alerts.decide(_usage(51), PCTS, kept)
        assert text is not None

    def test_overage_counts_for_a_hundred_percent_alert(self):
        text, _ = plan_credit_alerts.decide(_usage(120), PCTS, {})
        assert text is not None and "100%" in text

    @pytest.mark.parametrize(
        "reading",
        [
            {"available": False},
            _usage(90, stale=True),
            {"credits_used": 90},
            _usage(90, plan=0),
            {"credits_used": "90", "credits_plan": 100},
            {"credits_used": True, "credits_plan": 100},
            {"credits_used": float("nan"), "credits_plan": 100},
        ],
    )
    def test_a_reading_that_cannot_be_judged_changes_nothing(self, reading):
        record = {"period": "2026-12-01", "told": [50.0]}
        assert plan_credit_alerts.decide(reading, PCTS, record) == (None, record)

    def test_no_percentages_never_fires(self):
        assert plan_credit_alerts.decide(_usage(99), [], {})[0] is None


@pytest.fixture
def _alerts(tmp_path):
    """Percentages [50, 80, 100] on, with the record under ``tmp_path``."""

    def _cfg(pcts):
        return patch.object(
            plan_credit_alerts.KiroCrewConfig,
            "load",
            return_value=SimpleNamespace(telemetry=SimpleNamespace(plan_alert_pcts=pcts)),
        )

    with patch.object(plan_credit_alerts, "config_dir", lambda: tmp_path):
        yield _cfg, tmp_path


class TestNotify:
    @pytest.mark.asyncio
    async def test_pushes_one_notification_and_not_a_second(self, _alerts):
        cfg, _ = _alerts
        bus = MagicMock()
        state = SimpleNamespace(notification_bus=bus)
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(state, _usage(82))
            await plan_credit_alerts.notify_if_crossed(state, _usage(83))
        bus.push.assert_called_once()
        payload = bus.push.call_args.args[0]
        assert payload.channel == "system.agent"
        assert payload.group_key == plan_credit_alerts.GROUP_KEY
        assert "80%" in payload.body

    @pytest.mark.asyncio
    async def test_a_restart_does_not_tell_the_same_crossing_again(self, _alerts):
        cfg, path = _alerts
        bus = MagicMock()
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(SimpleNamespace(notification_bus=bus), _usage(60))
        assert (path / plan_credit_alerts.STATE_FILE).is_file()
        again = MagicMock()
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(
                SimpleNamespace(notification_bus=again), _usage(61)
            )
        bus.push.assert_called_once()
        again.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_setting_is_off_and_writes_nothing(self, _alerts):
        cfg, path = _alerts
        bus = MagicMock()
        with cfg([]):
            await plan_credit_alerts.notify_if_crossed(SimpleNamespace(notification_bus=bus), _usage(99))
        bus.push.assert_not_called()
        assert not (path / plan_credit_alerts.STATE_FILE).exists()

    @pytest.mark.asyncio
    async def test_a_failing_bus_does_not_raise(self, _alerts):
        cfg, _ = _alerts
        bus = MagicMock()
        bus.push.side_effect = RuntimeError("boom")
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(SimpleNamespace(notification_bus=bus), _usage(99))

    @pytest.mark.asyncio
    async def test_unreadable_record_is_treated_as_empty(self, _alerts):
        cfg, path = _alerts
        (path / plan_credit_alerts.STATE_FILE).write_text("not json", encoding="utf-8")
        bus = MagicMock()
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(SimpleNamespace(notification_bus=bus), _usage(60))
        bus.push.assert_called_once()


class TestSetting:
    def test_default_is_empty_which_is_off(self):
        assert TelemetryConfig().plan_alert_pcts == []

    def test_percent_list_keeps_valid_numbers_sorted_and_distinct(self):
        assert _percent_list([80, 50, 80, 100.5], []) == [50.0, 80.0, 100.5]

    def test_percent_list_drops_what_is_not_a_percentage(self):
        assert _percent_list([0, -5, 1001, True, "80", None, 50], []) == [50.0]

    @pytest.mark.parametrize("value", ["80", 80, {"a": 1}, None])
    def test_a_non_list_reads_as_the_default(self, value):
        assert _percent_list(value, []) == []


class TestRoutesRunTheCheck:
    @pytest.mark.asyncio
    async def test_refresh_route_checks_after_the_refresh(self):
        state = SimpleNamespace(_background_tasks=set())
        request = MagicMock()
        request.app = {"state": state}
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock(return_value=None)),
            patch.object(plan_credit_alerts, "notify_if_crossed", AsyncMock()) as check,
        ):
            resp = await sessions_mod.api_sessions_usage_refresh(request)
        assert resp.status == 200
        check.assert_awaited_once()
        assert check.await_args.args[0] is state

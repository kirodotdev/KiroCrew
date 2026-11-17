"""Plan-credit alerts: one notice per crossed percentage, once per plan period and account.

``telemetry.plan_alert_pcts`` is empty by default, which is off. The decision is a pure
function of the reading, its account, the percentages and what was already told, so most
cases run on :func:`plan_credit_alerts.decide`; the rest cover the account key, the stored
record, two evaluations racing for one crossing, the notification that is pushed, the
setting's parsing, and the refresh wrapper that runs the check on the reading the usage
GET would serve.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.config.loader_builders import _percent_list
from kiro_crew.config.sections_telemetry import TelemetryConfig
from kiro_crew.dashboard.handlers import plan_credit_alerts
from kiro_crew.testing.wait import wait_until

PCTS = [50.0, 80.0, 100.0]
ACCT = "acct-a"


def _usage(used: float, plan: float = 100.0, **extra):
    return {
        "credits_used": used,
        "credits_plan": plan,
        "resets": "2026-12-01",
        "email": "a@example.com",
        **extra,
    }


class TestDecide:
    def test_below_every_percentage_says_nothing(self):
        text, kept = plan_credit_alerts.decide(_usage(49.9), PCTS, {}, account=ACCT)
        assert text is None
        assert kept == {"account": ACCT, "period": "2026-12-01", "told": []}

    def test_crossing_a_percentage_names_it_and_remembers_it(self):
        text, kept = plan_credit_alerts.decide(_usage(52), PCTS, {}, account=ACCT)
        assert text is not None and "52%" in text and "50%" in text
        assert kept["told"] == [50.0]

    def test_the_same_crossing_is_told_once(self):
        _, kept = plan_credit_alerts.decide(_usage(52), PCTS, {}, account=ACCT)
        text, again = plan_credit_alerts.decide(_usage(60), PCTS, kept, account=ACCT)
        assert text is None and again == kept

    def test_a_jump_over_two_percentages_is_one_notice_for_the_highest(self):
        text, kept = plan_credit_alerts.decide(_usage(85), PCTS, {}, account=ACCT)
        assert text is not None and "80%" in text and "50%" not in text
        assert kept["told"] == [50.0, 80.0]

    def test_the_next_percentage_is_told_later(self):
        _, kept = plan_credit_alerts.decide(_usage(52), PCTS, {}, account=ACCT)
        text, kept = plan_credit_alerts.decide(_usage(81), PCTS, kept, account=ACCT)
        assert text is not None and "80%" in text
        assert kept["told"] == [50.0, 80.0]

    def test_a_new_plan_period_starts_the_list_over(self):
        _, kept = plan_credit_alerts.decide(_usage(95), PCTS, {}, account=ACCT)
        text, _ = plan_credit_alerts.decide(
            _usage(55, resets="2027-01-01"), PCTS, kept, account=ACCT
        )
        assert text is not None and "50%" in text

    def test_another_accounts_list_does_not_silence_this_one(self):
        told_b = {"account": "acct-b", "period": "2026-12-01", "told": [50.0, 80.0]}
        text, kept = plan_credit_alerts.decide(_usage(85), PCTS, told_b, account=ACCT)
        assert text is not None and "80%" in text
        assert kept == {"account": ACCT, "period": "2026-12-01", "told": [50.0, 80.0]}

    def test_used_credits_falling_back_makes_a_percentage_tellable_again(self):
        _, kept = plan_credit_alerts.decide(_usage(85), PCTS, {}, account=ACCT)
        _, kept = plan_credit_alerts.decide(_usage(10), PCTS, kept, account=ACCT)
        assert kept["told"] == []
        text, _ = plan_credit_alerts.decide(_usage(51), PCTS, kept, account=ACCT)
        assert text is not None

    def test_overage_counts_for_a_hundred_percent_alert(self):
        text, _ = plan_credit_alerts.decide(_usage(120), PCTS, {}, account=ACCT)
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
        record = {"account": ACCT, "period": "2026-12-01", "told": [50.0]}
        assert plan_credit_alerts.decide(reading, PCTS, record, account=ACCT) == (None, record)

    def test_a_reading_that_names_no_account_changes_nothing(self):
        record = {"account": ACCT, "period": "2026-12-01", "told": []}
        assert plan_credit_alerts.decide(_usage(90), PCTS, record, account="") == (None, record)

    def test_no_percentages_never_fires(self):
        assert plan_credit_alerts.decide(_usage(99), [], {}, account=ACCT)[0] is None


class TestAccountKey:
    @pytest.mark.parametrize(
        "reading", [{"credits_used": 1.0}, {"account_type": "ApiKey"}, {"email": ""}]
    )
    def test_a_reading_that_names_no_account_has_no_key(self, reading):
        assert plan_credit_alerts.account_key(reading) == ""

    def test_each_identity_part_tells_two_accounts_apart(self):
        key = plan_credit_alerts.account_key
        base = key({"email": "a@example.com", "start_url": "https://a.example/start"})
        assert base != key({"email": "b@example.com", "start_url": "https://a.example/start"})
        assert base != key({"email": "a@example.com", "start_url": "https://b.example/start"})
        assert key({"email": "a@example.com"}, user_id="u-1") != key(
            {"email": "a@example.com"}, user_id="u-2"
        )
        assert key({}, vault_fp="fp-a") != key({}, vault_fp="fp-b")

    def test_a_crew_sign_in_is_named_by_its_fingerprint_alone(self):
        key = plan_credit_alerts.account_key
        assert key({"email": "a@example.com"}, vault_fp="fp-a") == key({}, vault_fp="fp-a")

    def test_the_key_is_a_digest_that_holds_no_identity(self):
        key = plan_credit_alerts.account_key({"email": "a@example.com"}, user_id="u-1")
        assert len(key) == 64 and "example" not in key and "u-1" not in key


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


def _told(path) -> list[list[float]]:
    """The told percentages the stored record keeps, one list per account, oldest first."""
    stored = json.loads((path / plan_credit_alerts.STATE_FILE).read_text(encoding="utf-8"))
    return [entry["told"] for entry in stored["accounts"].values()]


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
            await plan_credit_alerts.notify_if_crossed(
                SimpleNamespace(notification_bus=bus), _usage(60)
            )
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
            await plan_credit_alerts.notify_if_crossed(
                SimpleNamespace(notification_bus=bus), _usage(99)
            )
        bus.push.assert_not_called()
        assert not (path / plan_credit_alerts.STATE_FILE).exists()

    @pytest.mark.asyncio
    async def test_a_failing_bus_does_not_raise(self, _alerts):
        cfg, path = _alerts
        bus = MagicMock()
        bus.push.side_effect = RuntimeError("boom")
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(
                SimpleNamespace(notification_bus=bus), _usage(99)
            )
        assert _told(path) == [[50.0, 80.0]]

    @pytest.mark.asyncio
    async def test_unreadable_record_is_treated_as_empty(self, _alerts):
        cfg, path = _alerts
        (path / plan_credit_alerts.STATE_FILE).write_text("not json", encoding="utf-8")
        bus = MagicMock()
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(
                SimpleNamespace(notification_bus=bus), _usage(60)
            )
        bus.push.assert_called_once()
        assert _told(path) == [[50.0]]


class TestPerAccount:
    """What one account was told never silences another's crossing in the same period."""

    @pytest.mark.asyncio
    async def test_another_account_with_the_same_reset_date_is_told_its_own_crossing(self, _alerts):
        cfg, path = _alerts
        bus = MagicMock()
        state = SimpleNamespace(notification_bus=bus)
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(state, _usage(82, email="a@example.com"))
            await plan_credit_alerts.notify_if_crossed(state, _usage(83, email="b@example.com"))
        assert bus.push.call_count == 2
        assert _told(path) == [[50.0, 80.0], [50.0, 80.0]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("first", "second", "pushes"),
        [
            ({"user_id": "u-1"}, {"user_id": "u-2"}, 2),
            ({"user_id": "u-1"}, {"user_id": "u-1"}, 1),
            ({"vault_fp": "fp-a"}, {"vault_fp": "fp-b"}, 2),
            ({"vault_fp": "fp-a"}, {"vault_fp": "fp-a"}, 1),
        ],
    )
    async def test_the_identity_kept_beside_the_reading_names_the_account(
        self, _alerts, first, second, pushes
    ):
        cfg, path = _alerts
        bus = MagicMock()
        state = SimpleNamespace(notification_bus=bus)
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(state, _usage(82), **first)
            await plan_credit_alerts.notify_if_crossed(state, _usage(83), **second)
        assert bus.push.call_count == pushes
        assert _told(path) == [[50.0, 80.0]] * pushes

    @pytest.mark.asyncio
    async def test_a_reading_that_names_no_account_sends_nothing_and_keeps_the_record(
        self, _alerts
    ):
        cfg, path = _alerts
        bus = MagicMock()
        state = SimpleNamespace(notification_bus=bus)
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(state, _usage(82))
            kept = (path / plan_credit_alerts.STATE_FILE).read_text(encoding="utf-8")
            await plan_credit_alerts.notify_if_crossed(state, _usage(99, email=None))
        bus.push.assert_called_once()
        assert (path / plan_credit_alerts.STATE_FILE).read_text(encoding="utf-8") == kept

    @pytest.mark.asyncio
    async def test_the_record_names_no_account_in_clear_text(self, _alerts):
        cfg, path = _alerts
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(
                SimpleNamespace(notification_bus=MagicMock()), _usage(82), user_id="u-1"
            )
        stored = (path / plan_credit_alerts.STATE_FILE).read_text(encoding="utf-8")
        assert "a@example.com" not in stored and "u-1" not in stored

    @pytest.mark.asyncio
    async def test_switching_to_another_account_and_back_does_not_tell_again(self, _alerts):
        cfg, path = _alerts
        bus = MagicMock()
        state = SimpleNamespace(notification_bus=bus)
        with cfg(PCTS):
            await plan_credit_alerts.notify_if_crossed(state, _usage(85, email="a@example.com"))
            await plan_credit_alerts.notify_if_crossed(state, _usage(20, email="b@example.com"))
            await plan_credit_alerts.notify_if_crossed(state, _usage(85, email="a@example.com"))
        assert bus.push.call_count == 1
        assert _told(path) == [[50.0, 80.0], []]

    @pytest.mark.asyncio
    async def test_the_record_keeps_at_most_the_newest_accounts(self, _alerts):
        cfg, path = _alerts
        state = SimpleNamespace(notification_bus=MagicMock())
        with cfg(PCTS):
            for n in range(plan_credit_alerts.MAX_ACCOUNTS + 3):
                await plan_credit_alerts.notify_if_crossed(state, _usage(55), user_id=f"u-{n}")
        stored = json.loads((path / plan_credit_alerts.STATE_FILE).read_text(encoding="utf-8"))
        assert len(stored["accounts"]) == plan_credit_alerts.MAX_ACCOUNTS
        newest = plan_credit_alerts.account_key(
            _usage(55), user_id=f"u-{plan_credit_alerts.MAX_ACCOUNTS + 2}"
        )
        assert list(stored["accounts"])[-1] == newest


class _SignallingLock:
    """A lock that sets *contended* when a caller finds it already held."""

    def __init__(self, contended: threading.Event) -> None:
        self._lock = threading.Lock()
        self._contended = contended

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._contended.set()
            self._lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


class TestOneClaimPerCrossing:
    @pytest.mark.asyncio
    async def test_two_evaluations_at_once_tell_a_crossing_once(self, _alerts):
        # The first evaluation to read the record holds its read open until the second
        # has arrived: either it read the record too (nothing serializes the two, so both
        # saw it empty) or it is waiting for the lock the first one holds.
        cfg, path = _alerts
        arrived = threading.Event()
        readers = itertools.count()
        real_read = plan_credit_alerts._read_record

        def read(path):
            record = real_read(path)
            if next(readers) == 0:
                wait_until(arrived.is_set, describe=lambda: "the second evaluation never came")
            else:
                arrived.set()
            return record

        bus = MagicMock()
        state = SimpleNamespace(notification_bus=bus)
        lock = _SignallingLock(arrived)
        with (
            cfg(PCTS),
            patch.object(plan_credit_alerts, "_read_record", read),
            patch.object(plan_credit_alerts, "_record_lock", lock, create=True),
        ):
            await asyncio.gather(
                plan_credit_alerts.notify_if_crossed(state, _usage(82)),
                plan_credit_alerts.notify_if_crossed(state, _usage(82)),
            )
        bus.push.assert_called_once()
        assert _told(path) == [[50.0, 80.0]]


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


@pytest.fixture
def _cache(monkeypatch):
    """Set the usage cache and the identity kept beside it, restored after the test."""

    def _set(usage, *, vault_fp="", user_id=None, fetching=False):
        monkeypatch.setattr(sessions_mod, "_usage_cache", usage)
        monkeypatch.setattr(sessions_mod, "_usage_cache_vault_fp", vault_fp)
        monkeypatch.setattr(sessions_mod, "_usage_cache_user_id", user_id)
        monkeypatch.setattr(sessions_mod, "_usage_fetching", fetching)

    return _set


class TestRefreshRunsTheCheck:
    """The refresh wrapper checks only a refresh that ran, on the reading the GET serves."""

    @pytest.mark.asyncio
    async def test_refresh_route_checks_after_the_refresh(self, _alerts, _cache):
        cfg, _ = _alerts
        reading = _usage(82)
        _cache(reading, vault_fp="fp-a")
        state = SimpleNamespace(_background_tasks=set())
        request = MagicMock()
        request.app = {"state": state}
        with (
            cfg(PCTS),
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch("kiro_crew.auth.bridge.vault_identity_fingerprint", return_value="fp-a"),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock(return_value=None)),
            patch.object(plan_credit_alerts, "notify_if_crossed", AsyncMock()) as check,
        ):
            resp = await sessions_mod.api_sessions_usage_refresh(request)
        assert resp.status == 200
        check.assert_awaited_once_with(state, reading, vault_fp="fp-a", user_id=None)

    @pytest.mark.asyncio
    async def test_a_refresh_already_running_is_not_checked_again(self, _alerts, _cache):
        # The call returns at once because another refresh holds the flag; that refresh
        # checks its own reading, so this one must not announce what the cache holds.
        cfg, _ = _alerts
        _cache(_usage(82), user_id="u-1", fetching=True)
        with (
            cfg(PCTS),
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
            patch.object(plan_credit_alerts, "notify_if_crossed", AsyncMock()) as check,
        ):
            await sessions_mod._fetch_usage_then_alert(SimpleNamespace(), vault_owned=False)
        check.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("vault_owns", "current_fp"),
        [(True, "fp-b"), (False, "fp-a")],
        ids=["another-crew-sign-in", "crew-sign-in-does-not-own-usage"],
    )
    async def test_a_reading_the_get_would_not_serve_is_not_checked(
        self, _alerts, _cache, vault_owns, current_fp
    ):
        cfg, _ = _alerts
        _cache(_usage(82), vault_fp="fp-a")
        with (
            cfg(PCTS),
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=vault_owns)),
            patch("kiro_crew.auth.bridge.vault_identity_fingerprint", return_value=current_fp),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock(return_value=None)),
            patch.object(plan_credit_alerts, "notify_if_crossed", AsyncMock()) as check,
        ):
            await sessions_mod._fetch_usage_then_alert(SimpleNamespace(), vault_owned=True)
        check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cli_reading_is_checked_with_the_identity_beside_it(self, _alerts, _cache):
        cfg, _ = _alerts
        reading = _usage(82)
        _cache(reading, user_id="u-1")
        state = SimpleNamespace()
        with (
            cfg(PCTS),
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock(return_value=None)),
            patch.object(plan_credit_alerts, "notify_if_crossed", AsyncMock()) as check,
        ):
            await sessions_mod._fetch_usage_then_alert(state, vault_owned=False)
        check.assert_awaited_once_with(state, reading, vault_fp="", user_id="u-1")

    @pytest.mark.asyncio
    async def test_off_reads_no_identity(self, _alerts, _cache):
        cfg, _ = _alerts
        _cache(_usage(82), vault_fp="fp-a")
        owns = AsyncMock(return_value=True)
        with (
            cfg([]),
            patch.object(sessions_mod, "_vault_owns_usage", owns),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock(return_value=None)),
            patch.object(plan_credit_alerts, "notify_if_crossed", AsyncMock()) as check,
        ):
            await sessions_mod._fetch_usage_then_alert(SimpleNamespace(), vault_owned=True)
        owns.assert_not_awaited()
        check.assert_not_awaited()

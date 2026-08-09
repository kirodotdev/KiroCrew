"""Watchdog idle windows are bounded by the prompt timeout, and long deferrals
are visible at the default log level.

Two properties are pinned here:

1. **Reachability.** Every ``watchdog.*`` idle window must be strictly inside the
   deadline the dispatch loop enforces. A window at or past it makes the
   UNKNOWN-verdict branch unreachable — the turn's timeout fires first, so the
   user gets the generic "turn hit the limit" card instead of the targeted
   tool-stall recovery. The bound is ``_DEFAULT_PROMPT_TIMEOUT``, the one
   deadline every caller shares; a lower DASHBOARD ceiling
   (``agent.chat_turn_timeout_secs``) is reported but not enforced, because the
   same handle serves callers that pass their own larger prompt timeout.

2. **Observability.** A WORKING verdict defers indefinitely by design. Past a
   meaningful share of the turn's deadline that deferral is logged at WARNING,
   so the one decision able to hold a turn silent up to its ceiling leaves a
   trace at the default ``agent.log_level`` instead of only at INFO.
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import session_handle
from kiro_crew.acp.session_handle import (
    _DEFAULT_PROMPT_TIMEOUT,
    _TURN_BOUNDED_WINDOWS,
    _TURN_CEILING_WINDOW_FRACTION,
    _WORKING_LOG_INTERVAL_SECS,
    _WORKING_NEVER_LOGGED,
    _WORKING_WARN_AFTER_SECS,
    _WORKING_WARN_DEADLINE_FRACTION,
    AcpSessionHandle,
    WatchdogSettings,
    _load_watchdog_settings,
)
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig, WatchdogConfig
from kiro_crew.constants import CHAT_TURN_TIMEOUT

_LOGGER_NAME = "kiro_crew.acp.session_handle"
_WINDOW_BUDGET = _DEFAULT_PROMPT_TIMEOUT * _TURN_CEILING_WINDOW_FRACTION


def _fake_config(*, turn_timeout: float = CHAT_TURN_TIMEOUT, **watchdog: float) -> KiroCrewConfig:
    """A real config object with only the two sections under test set.

    Real ``AgentConfig``/``WatchdogConfig`` rather than a namespace double, so a
    key that no longer exists in production fails the test instead of silently
    passing on an invented attribute.
    """
    return KiroCrewConfig(
        agent=AgentConfig(chat_turn_timeout_secs=int(turn_timeout)),
        watchdog=WatchdogConfig(**watchdog),
    )


def _load_with(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> WatchdogSettings:
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    return _load_watchdog_settings()


# ── Reachability ─────────────────────────────────────────────────────────────


def test_shipped_defaults_are_reachable_within_the_prompt_timeout() -> None:
    """Ratchet: no default window may reach the prompt deadline.

    A default above it silently disables the branch it governs on every
    install, which is exactly how the UNKNOWN class became dead code.
    """
    defaults = WatchdogSettings()
    for key in _TURN_BOUNDED_WINDOWS:
        assert getattr(defaults, key) <= _WINDOW_BUDGET, f"{key} default exceeds the budget"


def test_shipped_defaults_also_fit_the_default_chat_ceiling() -> None:
    """The dashboard is the common caller, so the defaults must be actionable
    there too — not merely inside the transport bound."""
    defaults = WatchdogSettings()
    for key in _TURN_BOUNDED_WINDOWS:
        assert getattr(defaults, key) <= CHAT_TURN_TIMEOUT, f"{key} outruns a default turn"


def test_loader_defaults_match_the_handle_snapshot_defaults() -> None:
    """The two default sets are documented as mirrors; drift between them makes
    a config-less context behave differently from a default config."""
    config_defaults = WatchdogConfig()
    snapshot_defaults = WatchdogSettings()
    for key in (*_TURN_BOUNDED_WINDOWS, "wellness_sample_secs"):
        assert getattr(snapshot_defaults, key) == getattr(config_defaults, key)


def test_over_ceiling_window_is_clamped_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _fake_config(tool_stall_suspect_secs=10800.0, tool_stall_hard_cap_secs=10800.0)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        wd = _load_with(monkeypatch, cfg)

    assert wd.tool_stall_suspect_secs == _WINDOW_BUDGET
    assert wd.tool_stall_hard_cap_secs == _WINDOW_BUDGET
    assert "tool_stall_suspect_secs" in caplog.text
    assert "clamping" in caplog.text


def test_in_range_windows_pass_through_untouched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _fake_config(check_after_secs=45.0, tool_stall_suspect_secs=1200.0)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        wd = _load_with(monkeypatch, cfg)

    assert wd.check_after_secs == 45.0
    assert wd.tool_stall_suspect_secs == 1200.0
    assert caplog.text == ""


def test_a_lowered_chat_ceiling_warns_but_does_not_shrink_the_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The dashboard ceiling is advisory here. Enforcing it would shrink the
    windows for callers that pass their own larger prompt timeout (a review run,
    a cron turn) and cancel their live work at a fraction of their deadline.
    """
    cfg = _fake_config(turn_timeout=300.0, tool_stall_suspect_secs=3600.0)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        wd = _load_with(monkeypatch, cfg)

    assert wd.tool_stall_suspect_secs == 3600.0
    assert "chat_turn_timeout_secs" in caplog.text
    assert "clamping" not in caplog.text


def test_sampling_interval_is_not_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """``wellness_sample_secs`` spaces CPU/IO samples; it is never compared
    against elapsed idle, so the ceiling does not apply to it."""
    cfg = _fake_config(wellness_sample_secs=99999.0)
    wd = _load_with(monkeypatch, cfg)
    assert wd.wellness_sample_secs == 99999.0


def test_load_failure_still_yields_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(cls: type) -> KiroCrewConfig:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert _load_watchdog_settings() == WatchdogSettings()


# ── Observability ────────────────────────────────────────────────────────────


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())


def test_first_deferral_logs_on_a_freshly_booted_host(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``time.monotonic()`` counts from boot on Linux, so a host up for less
    than the rate-limit interval must not read as "already logged" and swallow
    the first deferral line — the one a just-restarted gateway most needs."""
    monkeypatch.setattr(session_handle.time, "monotonic", lambda: 5.0)
    handle = _handle()
    assert handle._working_logged_ts == _WORKING_NEVER_LOGGED
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        handle._log_working_deferral(120.0, "live shell child 4242", _DEFAULT_PROMPT_TIMEOUT)

    assert len(caplog.records) == 1


def test_short_deferral_stays_at_info(caplog: pytest.LogCaptureFixture) -> None:
    handle = _handle()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        handle._log_working_deferral(
            _WORKING_WARN_AFTER_SECS - 1, "live shell child 4242", _DEFAULT_PROMPT_TIMEOUT
        )

    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_long_deferral_escalates_to_warning(caplog: pytest.LogCaptureFixture) -> None:
    handle = _handle()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        handle._log_working_deferral(
            _WORKING_WARN_AFTER_SECS, "live shell child 4242", _DEFAULT_PROMPT_TIMEOUT
        )

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "verdict WORKING" in caplog.text


def test_escalation_scales_down_to_a_short_turn(caplog: pytest.LogCaptureFixture) -> None:
    """On a turn far shorter than the default, the fixed 30-minute mark would
    never be reached — the deferral would eat the whole turn at INFO. The
    threshold is the lower of the fixed mark and a share of this deadline."""
    short_turn = 600.0
    idle = short_turn * _WORKING_WARN_DEADLINE_FRACTION
    assert idle < _WORKING_WARN_AFTER_SECS  # the fixed mark alone would stay INFO

    handle = _handle()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        handle._log_working_deferral(idle, "live shell child 4242", short_turn)

    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_long_turn_does_not_delay_escalation(caplog: pytest.LogCaptureFixture) -> None:
    """The fraction only ever lowers the threshold: a caller with a deadline
    above the default must not push escalation past the fixed mark."""
    handle = _handle()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        handle._log_working_deferral(
            _WORKING_WARN_AFTER_SECS, "live shell child 4242", _DEFAULT_PROMPT_TIMEOUT * 4
        )

    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_escalation_still_honours_the_rate_limit(caplog: pytest.LogCaptureFixture) -> None:
    """Escalating the level must not turn a per-tick deferral into a log flood:
    the once-per-interval budget is shared by both levels."""
    handle = _handle()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        for _ in range(5):
            handle._log_working_deferral(
                _WORKING_WARN_AFTER_SECS * 2, "live shell child 4242", _DEFAULT_PROMPT_TIMEOUT
            )

    assert len(caplog.records) == 1

    # Past the interval the next line is emitted, still at WARNING.
    handle._working_logged_ts = time.monotonic() - (_WORKING_LOG_INTERVAL_SECS + 1)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        handle._log_working_deferral(
            _WORKING_WARN_AFTER_SECS * 2, "live shell child 4242", _DEFAULT_PROMPT_TIMEOUT
        )

    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING]

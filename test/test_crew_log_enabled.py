"""``crew_log_enabled``: the crew log is on unless explicitly switched off."""

from __future__ import annotations

import pytest

from kiro_crew.constants import CREW_LOG_ENV, ENV_FALSY, ENV_TRUTHY, crew_log_enabled

_NAME = CREW_LOG_ENV


def test_unset_is_on(monkeypatch):
    monkeypatch.delenv(_NAME, raising=False)
    assert crew_log_enabled() is True


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_is_on(monkeypatch, value):
    monkeypatch.setenv(_NAME, value)
    assert crew_log_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " False ", "No"])
def test_a_falsy_spelling_is_off(monkeypatch, value):
    monkeypatch.setenv(_NAME, value)
    assert crew_log_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_a_truthy_spelling_is_on(monkeypatch, value):
    monkeypatch.setenv(_NAME, value)
    assert crew_log_enabled() is True


@pytest.mark.parametrize("value", ["2", "disable", "fasle", " Disabled ", "nope"])
def test_an_unrecognised_value_fails_closed(monkeypatch, value):
    """Setting a default-on flag at all means opting out, so a typo turns it off."""
    monkeypatch.setenv(_NAME, value)
    assert crew_log_enabled() is False


def test_an_unrecognised_value_is_warned_about_once(monkeypatch, caplog):
    from kiro_crew import constants

    monkeypatch.setattr(constants, "_WARNED_UNRECOGNISED", set())
    monkeypatch.setenv(CREW_LOG_ENV, "disable")
    with caplog.at_level("WARNING", logger="kiro_crew.constants"):
        crew_log_enabled()
        crew_log_enabled()
    warned = [r for r in caplog.records if CREW_LOG_ENV in r.getMessage()]
    assert len(warned) == 1
    assert "is OFF" in warned[0].getMessage()
    assert "1, true, yes or on" in warned[0].getMessage()


@pytest.mark.parametrize("value", ["", "1", "off"])
def test_a_recognised_or_empty_value_is_not_warned_about(monkeypatch, caplog, value):
    from kiro_crew import constants

    monkeypatch.setattr(constants, "_WARNED_UNRECOGNISED", set())
    monkeypatch.setenv(CREW_LOG_ENV, value)
    with caplog.at_level("WARNING", logger="kiro_crew.constants"):
        crew_log_enabled()
    assert not [r for r in caplog.records if CREW_LOG_ENV in r.getMessage()]


def test_the_falsy_and_truthy_sets_do_not_overlap():
    assert not ENV_FALSY & ENV_TRUTHY


def test_the_crew_log_readers_use_the_one_helper(monkeypatch):
    """Both spellings of the crew-log switch read the same default."""
    from kiro_crew.crew_log import emit
    from kiro_crew.dashboard.handlers import crew_log as routes

    assert routes.CREW_LOG_ENV == emit.CREW_LOG_ENV
    monkeypatch.delenv(emit.CREW_LOG_ENV, raising=False)
    assert emit.enabled() is True
    assert crew_log_enabled() is True
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    assert emit.enabled() is False
    monkeypatch.setenv(emit.CREW_LOG_ENV, "fasle")
    assert emit.enabled() is False
    assert crew_log_enabled() is False

"""What the safety override's governance permits answer when the governance layer is missing.

``safety_override_permits`` asks the enterprise ceiling through imports deferred to the
call, so the module itself never depends on governance at import time. These tests pin
the answers it gives when those imports fail: a duration member and a deniable approval
mode are permitted (with no governance layer there is no ceiling to deny them), and the
non-deniable set falls back to the interactive floor alone, the direction that makes the
mode check consult policy for more modes, never fewer.

Each case also proves the answer came from the fallback, not from a governance layer
that happened to permit: the fallback's own debug record names the missing layer, and a
governed answer writes none.
"""

from __future__ import annotations

import logging
import sys

import pytest

from kiro_crew import safety_override

GOVERNANCE_PROFILES = "kiro_crew.platform.governance_profiles"
GOVERNANCE = "kiro_crew.platform.governance"
LOGGER = "kiro_crew.safety_override"


def _fallback_records(caplog: pytest.LogCaptureFixture, marker: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and marker in r.getMessage()]


@pytest.fixture
def without_governance_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``None`` entry makes ``from <module> import ...`` raise ``ImportError``."""
    monkeypatch.setitem(sys.modules, GOVERNANCE_PROFILES, None)


def test_a_duration_member_is_permitted_without_the_governance_layer(
    without_governance_profiles: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert safety_override.declared_grant_permitted() is True
        assert safety_override.until_shutdown_permitted() is True
    assert _fallback_records(caplog, "governance layer unavailable") == [
        "governance layer unavailable; permitting permanent",
        "governance layer unavailable; permitting until_shutdown",
    ]


def test_a_deniable_mode_is_permitted_without_the_governance_layer(
    without_governance_profiles: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert "yolo" not in safety_override._non_deniable_approval_modes()
        assert safety_override.approval_mode_permitted("yolo") is True
    assert _fallback_records(caplog, "governance layer unavailable") == [
        "governance layer unavailable; permitting mode yolo"
    ]


def test_the_non_deniable_modes_fall_back_to_the_interactive_floor_without_the_catalog(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, GOVERNANCE, None)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert safety_override._non_deniable_approval_modes() == ("normal",)
    assert _fallback_records(caplog, "catalog unavailable") == [
        "catalog unavailable; assuming only 'normal' is non-deniable"
    ]


def test_a_governed_answer_writes_no_fallback_record(caplog: pytest.LogCaptureFixture) -> None:
    """Non-vacuity for the checks above: with the governance layer present the same calls
    log no fallback record, so the record is what tells the two answers apart."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        safety_override.declared_grant_permitted()
        safety_override.approval_mode_permitted("yolo")
        safety_override._non_deniable_approval_modes()
    assert _fallback_records(caplog, "unavailable") == []

"""An LTS lock no enterprise policy term can lift says so.

``edition_locks.locked_by_edition`` reads ``enterprise_policy_translate.MANAGED``: a
scope no row lands on keeps its LTS baseline whatever an admin sets, so a setting API
reports ``locked_by_edition`` and the dashboard names the
edition instead of a policy. A lock a term can lift, and every oss lock, keep the
policy wording.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType

import pytest
from computer_use_api_helpers import (  # noqa: F401 -- fixtures are requested by name
    home_fixture,
    profiles_dir_fixture,
)

from kiro_crew import edition_locks
from kiro_crew.computer_use import enable_state
from kiro_crew.dashboard.handlers import computer_use as cu_api
from kiro_crew.dashboard.handlers import credential_redaction as redaction_api
from kiro_crew.platform import enterprise_policy_translate as translate
from kiro_crew.platform.governance_parts.baseline import BASELINE_CONTROLS
from kiro_crew.security import redaction_switch

_COMPUTER_USE = "capabilities.computer_use"


@pytest.fixture
def lts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture
def computer_use_term(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cloud term that lands on ``capabilities.computer_use``, as a later one might."""
    rows = dict(translate.MANAGED)
    rows["computer_use"] = translate.Managed(("capabilities", "computer_use"), translate.SWITCH)
    monkeypatch.setattr(translate, "MANAGED", MappingProxyType(rows))


def test_oss_never_reports_an_edition_lock():
    assert not any(edition_locks.locked_by_edition(scope) for scope in BASELINE_CONTROLS)


def test_lts_scope_with_no_term_is_locked_by_edition(lts):
    assert edition_locks.locked_by_edition(_COMPUTER_USE)
    assert edition_locks.locked_by_edition(redaction_switch.DISABLE_SCOPE)


def test_lts_scope_a_term_reaches_is_not(lts):
    assert not edition_locks.locked_by_edition("capabilities.browse")  # browser_use
    # An ``also`` target: the skill, mcp and agent install terms.
    assert not edition_locks.locked_by_edition("capabilities.extension_install")
    # A term on an inner list (``channels.members``) reaches its scope.
    assert not edition_locks.locked_by_edition("channels")
    # An inner list of a scope a term reaches (``subagent`` -> ``capabilities.spawn``).
    assert not edition_locks.locked_by_edition("capabilities.spawn.agents")


def test_a_new_term_flips_the_answer(lts, computer_use_term):
    assert not edition_locks.locked_by_edition(_COMPUTER_USE)
    assert not enable_state.locked_by_edition()


def _computer_use_view(monkeypatch: pytest.MonkeyPatch, *, locked: bool) -> dict:
    monkeypatch.setattr(enable_state, "locked_by_policy", lambda **_: locked)
    return cu_api._snapshot()


def test_lts_computer_use_baseline_lock_names_the_edition(lts, home, monkeypatch):
    view = _computer_use_view(monkeypatch, locked=True)
    assert view["locked_by_policy"] is True
    assert view["locked_by_edition"] is True


def test_lts_computer_use_lock_a_term_can_lift_keeps_the_policy_wording(
    lts, computer_use_term, home, monkeypatch
):
    view = _computer_use_view(monkeypatch, locked=True)
    assert view["locked_by_policy"] is True
    assert view["locked_by_edition"] is False


def test_oss_computer_use_policy_lock_keeps_the_policy_wording(home, monkeypatch):
    view = _computer_use_view(monkeypatch, locked=True)
    assert view["locked_by_policy"] is True
    assert view["locked_by_edition"] is False


def test_an_unlocked_setting_reports_no_edition_lock(lts, home, monkeypatch):
    assert _computer_use_view(monkeypatch, locked=False)["locked_by_edition"] is False


@pytest.mark.parametrize(
    ("edition", "may_disable", "expected"),
    [("lts", False, True), ("oss", False, False), ("lts", True, False)],
)
def test_credential_redaction_view(monkeypatch, edition, may_disable, expected):
    monkeypatch.setattr("kiro_crew.edition.EDITION", edition)
    monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: may_disable)
    view = asyncio.run(redaction_api._view(redaction_switch.RedactionState(True, "")))
    assert view["locked_by_policy"] is (not may_disable)
    assert view["locked_by_edition"] is expected

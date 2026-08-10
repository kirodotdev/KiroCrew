"""A regex-tier deny that only a governance PIN produced is policy state.

A governance pin re-adds a built-in rule the user disabled, so the same match
carries two different meanings: the host enforces this rule, or policy currently
overrides the host's opt-out. Only the first says anything about the tool being
attempted.

The distinction is load-bearing because a cron counts security blocks toward a
durable auto-pause that clears ``auto_paused`` without restoring ``enabled`` —
so mislabeling policy state strands a job that a later loosening cannot revive.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew import hooks as hooks_mod
from kiro_crew import security
from kiro_crew.hooks import HookManager, HooksConfig
from kiro_crew.platform import context as ctx_mod

_PINNED_ID = "credential-exfil-s3-cp"


def _manager_with_rule_disabled() -> HookManager:
    """A host whose operator opted OUT of one built-in rule."""
    mgr = HookManager()
    mgr._config = HooksConfig.from_dict(
        {"denied_commands": {"disabled_ids": [_PINNED_ID], "disable_all": False}}
    )
    return mgr


@pytest.fixture(autouse=True)
def _reset():
    yield
    ctx_mod.reset_context()


def _pattern_of(rule_id: str) -> str:
    return next(r.pattern for r in security.BUILTIN_DENIED_RULES if r.id == rule_id)


def test_a_pin_re_adds_the_rule_the_user_disabled(monkeypatch) -> None:
    """The enforcement set is unchanged: tightest-wins still holds."""
    monkeypatch.setattr(security, "pinned_builtin_command_ids", lambda: {_PINNED_ID})
    mgr = _manager_with_rule_disabled()

    assert _pattern_of(_PINNED_ID) in mgr.effective_denied_regexes()


def test_the_pin_free_set_omits_it(monkeypatch) -> None:
    """The classification set reflects the user's own opt-out alone.

    This is the difference the mechanism label is derived from — it must never
    be used to DECIDE a deny, only to explain one.
    """
    monkeypatch.setattr(security, "pinned_builtin_command_ids", lambda: {_PINNED_ID})
    mgr = _manager_with_rule_disabled()

    assert _pattern_of(_PINNED_ID) not in mgr.effective_denied_regexes(
        include_governance_pins=False
    )


def test_a_rule_the_user_still_enforces_appears_in_both(monkeypatch) -> None:
    """Without an opt-out there is no provenance question to answer."""
    monkeypatch.setattr(security, "pinned_builtin_command_ids", lambda: {_PINNED_ID})
    mgr = HookManager()

    pattern = _pattern_of(_PINNED_ID)
    assert pattern in mgr.effective_denied_regexes()
    assert pattern in mgr.effective_denied_regexes(include_governance_pins=False)


def test_an_ungoverned_host_resolves_both_sets_identically() -> None:
    """The common case pays no behavioral difference."""
    mgr = HookManager()

    assert mgr.effective_denied_regexes() == mgr.effective_denied_regexes(
        include_governance_pins=False
    )


def test_the_resolver_keeps_pins_by_default() -> None:
    """Enforcement callers that pass nothing must still get the pinned set."""
    cfg = HooksConfig.from_dict(
        {"denied_commands": {"disabled_ids": [_PINNED_ID], "disable_all": False}}
    )
    ctx = dataclasses.replace(ctx_mod.current_context())

    with_default = hooks_mod.resolve_effective_denied_regexes(cfg, ctx)
    explicit = hooks_mod.resolve_effective_denied_regexes(cfg, ctx, include_governance_pins=True)

    assert with_default == explicit

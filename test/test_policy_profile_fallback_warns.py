"""``kirocrew policy profile <broken>`` warns that it is the fallback profile.

A profile file that is unreadable, unparseable, or has a missing/chained
``extends`` is substituted by the fallback profile (deny-all, or a looser one the
policy declares) and stored under the same stem, so ``get_store_profile``
returns a valid-looking profile. Without a warning the broken file reads as a
deliberate one; the command now
prints a stderr warning naming it as the fallback, and still prints the profile.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import cli_commands_security as m
from kiro_crew.platform import governance_profiles as gp


def _run_profile(monkeypatch: pytest.MonkeyPatch, name: str, is_fallback: bool):
    prof = gp._fallback_profile(name)
    monkeypatch.setattr(gp, "get_store_profile", lambda n: prof if n == name else None)
    monkeypatch.setattr(
        gp, "fallback_profile_names", lambda: frozenset({name}) if is_fallback else frozenset()
    )
    m._policy(SimpleNamespace(policy_action="profile", name=name))


def test_a_broken_profile_warns_and_still_prints_the_profile(monkeypatch, capsys):
    _run_profile(monkeypatch, "broken-prof", is_fallback=True)
    out = capsys.readouterr()
    # The warning goes to stderr and names the profile and the fallback.
    assert "broken-prof" in out.err
    assert "fallback profile shown below" in out.err
    # The fallback may be a policy-declared one, so no deny-all claim is made.
    assert "deny-all" not in out.err
    assert "failed to load" in out.err
    # The profile itself is still printed to stdout (fallback behavior kept).
    assert "📄 Profile" in out.out


def test_a_healthy_profile_prints_no_fallback_warning(monkeypatch, capsys):
    # Not in the fallback set: no warning, same output as before.
    _run_profile(monkeypatch, "good-prof", is_fallback=False)
    out = capsys.readouterr()
    assert "fallback profile" not in out.err
    assert "failed to load" not in out.err
    assert "📄 Profile" in out.out

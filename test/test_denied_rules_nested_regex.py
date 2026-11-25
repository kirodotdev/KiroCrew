"""A deny regex nested too deep for ``re`` to compile is skipped, never raised.

``re.compile`` raises ``RecursionError`` (not ``re.error``) for a pattern nested a
few hundred groups deep. The matcher promises that a malformed stored rule is
disabled so one bad rule cannot wedge the gate, so such a rule must read as
unsafe, the way a pattern that does not parse does, and every other rule must keep
denying.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.security import BUILTIN_DENIED_RULES, DeniedCommandRule, is_denied
from kiro_crew.security.denied_rules import is_safe_user_regex

# Built at run time so no line of this file spells the denied command.
_DENIED_COMMAND = " ".join(["aws", "ec2", "terminate" + "-instances", "--instance-ids", "i-1"])
_WORD = "zq" + "bogus" + "marker"


def _nested(depth: int, core: str = "a") -> str:
    return "(" * depth + core + ")" * depth


def _builtins() -> list[str]:
    return [rule.pattern for rule in BUILTIN_DENIED_RULES]


@pytest.mark.parametrize("depth", [600, 1500])
def test_a_rule_nested_too_deep_is_unsafe_not_raised(depth) -> None:
    assert is_safe_user_regex(_nested(depth)) is False


@pytest.mark.parametrize("depth", [600, 1500])
def test_a_too_deep_rule_does_not_stop_the_other_rules(depth) -> None:
    reason = is_denied(_DENIED_COMMAND, denied_regexes=[_nested(depth), *_builtins()])
    assert reason is not None


def test_a_too_deep_rule_alone_allows_a_plain_command() -> None:
    assert is_denied("ls -la", denied_regexes=[_nested(600)]) is None


def test_a_too_deep_rule_is_skipped_whatever_its_place_in_the_list() -> None:
    rules = [*_builtins(), _nested(1500), _nested(600)]
    assert is_denied(_DENIED_COMMAND, denied_regexes=rules) is not None
    assert is_denied("ls -la", denied_regexes=rules) is None


def test_a_rule_nested_a_few_hundred_deep_is_still_enforced() -> None:
    rule = _nested(300, _WORD)
    assert is_safe_user_regex(rule) is True
    assert is_denied(f"echo {_WORD}", denied_regexes=[rule]) is not None
    assert is_denied("echo nothing", denied_regexes=[rule]) is None


def test_a_malformed_rule_is_still_skipped() -> None:
    assert is_safe_user_regex("(unclosed") is False
    assert is_denied(_DENIED_COMMAND, denied_regexes=["(unclosed", *_builtins()]) is not None
    assert is_denied("ls -la", denied_regexes=["(unclosed"]) is None


def test_an_edition_rule_nested_too_deep_is_not_published(monkeypatch) -> None:
    class _Source:
        def denied_rules(self):
            return [
                DeniedCommandRule(
                    id="acme-too-deep", pattern=_nested(1500), category="acme", description="d"
                ),
                DeniedCommandRule(id="acme-plain", pattern=_WORD, category="acme", description="d"),
            ]

    base = build_default_context(KiroCrewConfig())
    monkeypatch.setattr(
        ctx_mod,
        "current_context",
        lambda: dataclasses.replace(base, denied_rules=_Source()),
    )
    assert [rule.id for rule in security.edition_denied_rules()] == ["acme-plain"]

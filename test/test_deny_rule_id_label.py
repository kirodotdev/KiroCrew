"""A refusal from a catalog deny rule names the rule's id to the model.

Before this, the model saw ``Blocked by security policy: <pattern>``, a regex,
next to a tool result saying the user had refused. The id says which rule fired.
The tests use a made-up edition rule, so they hold for any rule a companion adds.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew import deny_guidance as dg
from kiro_crew import security
from kiro_crew.dashboard.state import (
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_POLICY,
    build_refusal_recovery_prompt,
    build_refusal_steer_notice,
)

_RULE = security.DeniedCommandRule(
    id="example-token-dir-path",
    pattern=r"(?:[~/$][\w./$-]*\.exampletoken\b)",
    category="sensitive-file-read",
    description="Made-up edition rule for these tests.",
)

#: The dashboard recovery card's own reader of a refusal line (RecoveryCard.tsx).
_POLICY_RE = re.compile(r"Blocked by security policy:\s*(.+?)\s*$", re.M)


@pytest.fixture
def edition_rule(monkeypatch):
    monkeypatch.setattr(security, "edition_denied_rules", lambda: [_RULE])
    dg.reset_rule_class_index()
    yield _RULE
    dg.reset_rule_class_index()


def test_rule_id_is_found_from_the_pattern(edition_rule):
    reason = security._deny_reason(edition_rule.pattern, None)
    assert dg.rule_id_for(reason) == "example-token-dir-path"


def test_rule_id_is_found_when_the_reason_leads_with_the_id(edition_rule):
    reason = security._deny_reason(edition_rule.id, None)
    assert dg.rule_id_for(reason) == "example-token-dir-path"


def test_a_built_in_rule_is_named_too():
    rule = next(iter(security.BUILTIN_DENIED_RULES))
    dg.reset_rule_class_index()
    try:
        assert dg.rule_id_for(security._deny_reason(rule.pattern, None)) == rule.id
    finally:
        dg.reset_rule_class_index()


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "Blocked: access to sensitive path: /x/y",
        "Blocked by security policy: an operator's own pattern no rule carries",
        # an operator note on the second line is never read as a rule identity
        "Blocked by security policy: unknown\nexample-token-dir-path",
    ],
)
def test_a_reason_naming_no_catalog_rule_gets_no_id(edition_rule, reason):
    assert dg.rule_id_for(reason) is None


def test_steer_notice_names_the_rule_and_keeps_the_pattern_as_detail(edition_rule):
    reason = security._deny_reason(edition_rule.pattern, None)
    notice = build_refusal_steer_notice("cat ~/.exampletoken/t", reason, cause=DENY_CAUSE_POLICY)
    assert "Blocked: cat ~/.exampletoken/t: blocked by rule example-token-dir-path. " in notice
    assert "NOT a user action" in notice
    # The recovery card still reads the same pattern chip out of the notice.
    assert _POLICY_RE.findall(notice) == [edition_rule.pattern]


def test_steer_notice_for_a_non_policy_cause_is_unchanged(edition_rule):
    reason = security._deny_reason(edition_rule.pattern, None)
    notice = build_refusal_steer_notice("t", reason, cause=DENY_CAUSE_HOOK_ERROR)
    assert "Blocked by rule" not in notice
    assert f"Blocked: t: {reason}" in notice


def test_steer_notice_without_a_rule_is_unchanged(edition_rule):
    reason = "Blocked: access to sensitive path: /x/y"
    notice = build_refusal_steer_notice("t", reason, cause=DENY_CAUSE_POLICY)
    assert "Blocked by rule" not in notice
    assert f"Blocked: t: {reason}" in notice


def test_recovery_prompt_names_the_rule_and_keeps_the_chip(edition_rule):
    reason = security._deny_reason(edition_rule.pattern, None)
    body = build_refusal_recovery_prompt([("cat ~/.exampletoken/t", reason)])
    assert "  - cat ~/.exampletoken/t: blocked by rule example-token-dir-path. " in body
    assert _POLICY_RE.findall(body) == [edition_rule.pattern]


def test_recovery_prompt_without_a_rule_is_unchanged(edition_rule):
    body = build_refusal_recovery_prompt([("t", "Blocked: x")])
    assert "  - t: Blocked: x" in body
    assert "blocked by rule" not in body

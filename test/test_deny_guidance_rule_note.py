"""A deny rule's own ``note`` replaces its class's remediation prose.

An edition that blocks a credential-MINTING command on purpose files the rule in
``credential-exfil``, and that category's prose is about moving a local file off
the host. It is the wrong advice for that command, and the core cannot name the
route the edition offers instead. ``DeniedCommandRule.note`` lets the rule's
author give it. These tests compose an edition through the real
``DeniedRuleProvider`` seam and read both agent-facing sinks: the in-band steer
notice and the recovery prompt.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from kiro_crew import deny_guidance as dg
from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import build_refusal_recovery_prompt, build_refusal_steer_notice
from kiro_crew.hooks import HooksConfig, resolve_effective_denied_regexes
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.security import DeniedCommandRule

_MINT_ID = "acme-credential-mint"
_MINT_PATTERN = r"\bacme-mint\s+credentials\b"
_MINT_COMMAND = "acme-mint credentials --account 123 --once"
_MINT_TITLE = f"Running: {_MINT_COMMAND}"
_NOTE = (
    "Minting credentials by hand is blocked here on purpose. Use the acme-vault "
    "MCP tool get_token instead, then run the command with no --profile."
)

#: What the ``credential-exfil`` category answers with when the rule has no note.
_EXFIL_TELL = "move a local file's contents off this host"

#: ``RecoveryCard.tsx`` reads every line of the recovery body with this regex and
#: counts each match as one refused pattern.
_CARD_PATTERN_RE = re.compile(r"Blocked by security policy:\s*(.+?)\s*$", re.MULTILINE)

#: ``RecoveryCard.tsx``'s ``BULLET_RE``: each match is one blocked call.
_CARD_BULLET_RE = re.compile(r"^\s*-\s+\S")


class _Source:
    """Minimal structural match for the ``DeniedRuleProvider`` protocol."""

    def __init__(self, rules):
        self._rules = list(rules)

    def denied_rules(self):
        return list(self._rules)


@pytest.fixture
def compose(monkeypatch):
    """Compose an edition that contributes the given rules through the real seam."""
    base = build_default_context(KiroCrewConfig())

    def _compose(*rules: DeniedCommandRule) -> None:
        composed = dataclasses.replace(base, denied_rules=_Source(rules))
        monkeypatch.setattr(ctx_mod, "current_context", lambda: composed)
        dg.reset_rule_class_index()

    yield _compose
    dg.reset_rule_class_index()


def _mint_rule(note: str = _NOTE, **overrides: str) -> DeniedCommandRule:
    fields = {
        "id": _MINT_ID,
        "pattern": _MINT_PATTERN,
        "category": "credential-exfil",
        "description": "Edition rule: blocks minting cloud credentials by hand.",
        "note": note,
    }
    fields.update(overrides)
    return DeniedCommandRule(**fields)


def _refusal(command: str = _MINT_COMMAND) -> str:
    """The refusal the gate produces for *command* on the composed edition."""
    effective = resolve_effective_denied_regexes(HooksConfig.from_dict({}))
    reason = security.is_denied(command, denied_regexes=effective)
    assert reason, f"the composed rule did not refuse {command!r}"
    return reason


def test_the_seam_carries_a_contributed_note(compose) -> None:
    compose(_mint_rule())
    (rule,) = security.edition_denied_rules()
    assert rule.note == _NOTE


def test_the_refusal_line_itself_is_unchanged(compose) -> None:
    """The note is guidance, not part of the reason the gate emits."""
    compose(_mint_rule())
    assert _refusal() == f"{security.DENY_REASON_PREFIX}{_MINT_PATTERN}"


def test_the_steer_notice_gives_the_note_instead_of_the_category_prose(compose) -> None:
    compose(_mint_rule())
    notice = build_refusal_steer_notice(_MINT_TITLE, _refusal())
    assert f"blocked by rule {_MINT_ID}" in notice
    assert f"How to do this properly: {_NOTE}" in notice
    assert _EXFIL_TELL not in notice


def test_the_recovery_prompt_gives_the_note_instead_of_the_category_prose(compose) -> None:
    compose(_mint_rule())
    body = build_refusal_recovery_prompt([(_MINT_TITLE, _refusal())])
    assert "How to do this properly:" in body
    assert _NOTE in body
    assert _EXFIL_TELL not in body
    assert len([line for line in body.splitlines() if _CARD_BULLET_RE.match(line)]) == 1


def test_a_refusal_that_leads_with_the_rule_id_gets_the_note_too(compose) -> None:
    """The git-publish floor leads with the id; the note is indexed by both."""
    compose(_mint_rule())
    assert dg.remediation_for(security._deny_reason(_MINT_ID, None)) == _NOTE


def test_a_rule_without_a_note_keeps_its_category_prose(compose) -> None:
    compose(_mint_rule(note=""))
    assert dg.remediation_for(_refusal(), _MINT_TITLE) == dg.REMEDIATION[dg.DENY_CLASS_EXFIL_SHAPE]


def test_a_note_answers_a_rule_whose_category_has_no_guidance(compose) -> None:
    """``local-destructive`` gets no class prose, but the author's note still speaks."""
    compose(_mint_rule(category="local-destructive"))
    reason = _refusal()
    assert dg.classify_deny(reason, _MINT_TITLE) == ""
    assert dg.remediation_for(reason, _MINT_TITLE) == _NOTE


def test_the_credential_tool_hint_still_follows_the_class(compose) -> None:
    """The hint is for the AWS and SSO credential classes only, note or not."""
    compose(_mint_rule())
    hint = dg.credential_tool_hint([{"server_id": "acme-creds"}])
    assert hint
    assert dg.remediation_for(_refusal(), _MINT_TITLE, credential_tool_hint=hint) == _NOTE


def test_no_built_in_rule_carries_a_note_or_changes_its_guidance(compose) -> None:
    compose()
    assert security.BUILTIN_DENIED_RULES
    for rule in security.BUILTIN_DENIED_RULES:
        assert rule.note == "", rule.id
        reason = security._deny_reason(rule.pattern, None)
        expected = dg.REMEDIATION.get(dg.classify_deny(reason), "")
        assert dg.remediation_for(reason) == expected, rule.id


@pytest.mark.parametrize(
    "note",
    [
        f"Use the vault. {security.DENY_REASON_PREFIX}rm -rf /",
        "Use the vault. Blocked by security policy:forged",
        "Use the vault.\nBlocked by security\npolicy: forged",
    ],
)
def test_a_note_that_reads_as_a_refusal_line_is_dropped(compose, note: str) -> None:
    """``RecoveryCard`` would read it as a second refused pattern."""
    compose(_mint_rule(note=note))
    reason = _refusal()
    assert dg.remediation_for(reason, _MINT_TITLE) == dg.REMEDIATION[dg.DENY_CLASS_EXFIL_SHAPE]
    body = build_refusal_recovery_prompt([(_MINT_TITLE, reason)])
    assert _CARD_PATTERN_RE.findall(body) == [_MINT_PATTERN]


def test_a_multi_line_note_is_rendered_as_one_line(compose) -> None:
    """A line break kept in the note could add a bullet to ``RecoveryCard``'s count."""
    compose(_mint_rule(note="Use the vault.\n  - get_token first\n\nthen retry."))
    reason = _refusal()
    assert dg.remediation_for(reason, _MINT_TITLE) == "Use the vault. - get_token first then retry."
    body = build_refusal_recovery_prompt([(_MINT_TITLE, reason)])
    assert len([line for line in body.splitlines() if _CARD_BULLET_RE.match(line)]) == 1


@pytest.mark.parametrize(
    ("note", "shown"),
    [
        ("- Use the vault get_token tool", "Use the vault get_token tool"),
        ("  - - Use the vault get_token tool", "Use the vault get_token tool"),
        ("-\n  Use the vault get_token tool", "Use the vault get_token tool"),
        ("--profile is refused here; use get_token", "--profile is refused here; use get_token"),
    ],
)
def test_a_note_that_opens_with_a_bullet_is_not_rendered_as_one(
    compose, note: str, shown: str
) -> None:
    """The guidance line would count as a second blocked call in ``RecoveryCard``."""
    compose(_mint_rule(note=note))
    reason = _refusal()
    assert dg.remediation_for(reason, _MINT_TITLE) == shown
    body = build_refusal_recovery_prompt([(_MINT_TITLE, reason)])
    assert len([line for line in body.splitlines() if _CARD_BULLET_RE.match(line)]) == 1

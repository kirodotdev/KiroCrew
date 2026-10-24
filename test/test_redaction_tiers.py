"""Every redaction rule carries exactly one tier from ``credential_patterns``.

These tests pin that the table is complete and well formed. What each tier does
to a value is pinned in ``test_redaction_key_id_identifier.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kiro_crew import credential_patterns as cp
from kiro_crew.security import redaction, redaction_labels, secret_catalog

SECURITY_DIR = Path(redaction.__file__).resolve().parent


def _core_rule_ids() -> set[str]:
    """Rule ids the core credential redactor can write into a record."""
    ids = {rule for _pattern, rule in redaction._PASS1_RULES}
    ids |= set(redaction_labels.RULE_IDS)
    # Passes 2 and 4 and the pass-1 fallback name their rule as ``("<id>", "")``.
    tree = ast.parse(Path(redaction.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Tuple)
            and len(node.elts) == 2
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)
            and node.elts[1].value == ""  # type: ignore[attr-defined]
        ):
            ids.add(node.elts[0].value)  # type: ignore[attr-defined]
    return ids


def _exfil_rule_ids() -> set[str]:
    """Rule ids the outbound-URL pass can write into a blocked-link record."""
    ids: set[str] = set()
    for path in sorted(SECURITY_DIR.glob("exfil*.py")):
        # A rule id is a whole string constant; a function name never is.
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"exfil_[a-z_]+", node.value)
            ):
                ids.add(node.value)
    return ids


def _vendor_labels() -> set[str]:
    return {label for label, _fragment in cp.VENDOR_TOKEN_PATTERNS}


def test_every_rule_has_a_tier() -> None:
    known = _core_rule_ids() | _exfil_rule_ids() | _vendor_labels()
    missing = sorted(known - set(cp.RULE_TIERS))
    assert missing == [], f"rules with no tier: {missing}"


def test_tier_table_names_no_unknown_rule() -> None:
    known = _core_rule_ids() | _exfil_rule_ids() | _vendor_labels()
    stale = sorted(set(cp.RULE_TIERS) - known)
    assert stale == [], f"tier rows for rules that no longer exist: {stale}"


def test_every_tier_is_one_of_the_three() -> None:
    assert cp.TIERS == ("secret", "bearer_link", "identifier")
    bad = {rule: tier for rule, tier in cp.RULE_TIERS.items() if tier not in cp.TIERS}
    assert bad == {}


def test_rule_tier_refuses_an_unknown_rule() -> None:
    with pytest.raises(KeyError):
        cp.rule_tier("not_a_rule")


@pytest.mark.parametrize(
    ("text", "rule", "tier"),
    [
        ("tok glpat-IOSFODNN7EXAMPLE end", "gitlab_token", cp.TIER_SECRET),
        ("https://h.example/o?Signature=abcdef0123", "request_signature", cp.TIER_BEARER_LINK),
        (
            "aws_secret_access_key=not-a-real-secret-value",
            "aws_secret_access_key",
            cp.TIER_SECRET,
        ),
        ("tok ghp_" + "a" * 36 + " end", "github_token", cp.TIER_SECRET),
    ],
)
def test_recorded_rules_have_the_expected_tier(text: str, rule: str, tier: str) -> None:
    _cleaned, _warnings, records = redaction.redact_credentials_with_records(text)
    assert [(r.rule, cp.rule_tier(r.rule)) for r in records] == [(rule, tier)]


def test_no_rule_carries_the_identifier_tier() -> None:
    """An identifier is never redacted, so no redaction rule names one."""
    assert cp.TIER_IDENTIFIER not in cp.RULE_TIERS.values()


def test_every_catalog_rule_has_a_tier() -> None:
    """The generated catalog names a tier for every rule, from the same vocabulary."""
    ids = secret_catalog.catalog_rule_ids()
    assert ids, "the generated catalog is empty"
    assert {secret_catalog.catalog_rule_tier(rule) for rule in ids} <= {
        cp.TIER_SECRET,
        cp.TIER_IDENTIFIER,
    }


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (
            "export AWS_SECRET_" + "ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYEXAMPLEKEY",
            "aws_secret_access_key",
        ),
        ('{"SessionToken": "FwoGZXIvYXdzEXAMPLETOKEN0123"}', "aws_session_token"),
        ("Authorization: Basic dXNlcjpleGFtcGxlMTIz", "authorization_header"),
        ("db_password = example-pw-1234", "secret_label"),
        ("AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "aws_key_pair"),
    ],
)
def test_context_rules_are_secret_tier(text: str, rule: str) -> None:
    _cleaned, _warnings, records = redaction.redact_credentials_with_records(text)
    assert [(r.rule, cp.rule_tier(r.rule)) for r in records] == [(rule, cp.TIER_SECRET)]

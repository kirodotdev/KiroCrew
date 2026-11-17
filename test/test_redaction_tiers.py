"""Every redaction rule carries exactly one tier from ``credential_patterns``.

The tier is metadata: these tests pin that the table is complete and well formed,
and that tagging a rule changes nothing about what is redacted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kiro_crew import credential_patterns as cp
from kiro_crew.security import redaction
from kiro_crew.security.redaction_structured import compat
from kiro_crew.security.redaction_structured.model import SECRET
from kiro_crew.security.redaction_structured.table import TABLE
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET, github_classic

SECURITY_DIR = Path(redaction.__file__).resolve().parent


def _core_rule_ids() -> set[str]:
    """Rule ids the core credential redactor can write into a record."""
    # A Secret hit of a table row is recorded under its pass-1 name (``old_rule``).
    ids = {compat.old_rule(rule) for rule, _detector, tier in TABLE.rule_ids() if tier == SECRET}
    # The whole-text fallback names its rule as ``("<id>", "")``.
    for module in (redaction, compat):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
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
        (
            f"aws_secret_access_key={SAMPLE_AWS_SECRET}",
            "aws_secret_access_key",
            cp.TIER_SECRET,
        ),
        (f"tok {github_classic(seed=333)} end", "github_token", cp.TIER_SECRET),
    ],
)
def test_recorded_rules_have_the_expected_tier(text: str, rule: str, tier: str) -> None:
    _cleaned, _warnings, records = redaction.redact_credentials_with_records(text)
    assert [(r.rule, cp.rule_tier(r.rule)) for r in records] == [(rule, tier)]


def test_identifier_tier_key_id_is_kept_visible() -> None:
    """A key ID is an identifier: no rule masks it, so it is left in the text."""
    assert "aws_access_key_id" not in cp.RULE_TIERS
    cleaned, _warnings, records = redaction.redact_credentials_with_records(
        "id AKIAIOSFODNN7EXAMPLE end"
    )
    assert cleaned == "id AKIAIOSFODNN7EXAMPLE end"
    assert records == []

"""A data-driven credential rule table.

``rules.yaml`` defines every rule: its tier (what a matched value can do), its
detector (how the value is found) and what is replaced. :func:`hits` resolves the
rules' overlapping claims on a text and :func:`redact_with_findings` replaces
secret and bearer-link values, keeps identifiers, and returns value-free records.

No seam calls this package yet. ``security.redaction`` and
``security.text_redaction`` remain the redactors every surface uses; this table
runs beside them until it is wired in.
"""

from __future__ import annotations

from kiro_crew.security.redaction_rules.model import (
    PLACEHOLDER,
    Detector,
    Hit,
    Record,
    RedactionFailed,
    RedactionResult,
    Tier,
)
from kiro_crew.security.redaction_rules.redact import hits, redact_with_findings
from kiro_crew.security.redaction_rules.rules import (
    Rule,
    RuleTable,
    RuleTableError,
    build_table,
    load_table,
    table_from_yaml,
)

__all__ = [
    "PLACEHOLDER",
    "Detector",
    "Hit",
    "RedactionFailed",
    "RedactionResult",
    "Record",
    "Rule",
    "RuleTable",
    "RuleTableError",
    "Tier",
    "build_table",
    "hits",
    "load_table",
    "redact_with_findings",
    "table_from_yaml",
]

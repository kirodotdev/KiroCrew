"""The policy-body builder the governance policy test modules share.

``test_governance_policy.py`` (archetypes, loader, resolution engine),
``test_governance_policy_signatures.py`` (``identity.signature`` verification) and
``test_governance_policy_decisions.py`` (what ``resolve`` and ``gate_decision`` hand
back for an unnamed capability or an alias group) all start from this minimal,
valid policy body.
"""

from __future__ import annotations


# ── A minimal, valid policy body reused across tests ──
def _policy_body(**overrides) -> dict:
    body = {
        "version": 1,
        "boot": {"fail_closed": True},
    }
    body.update(overrides)
    return body

"""Regression: deploy IAM must Deny tampering the append-only audit LogBucket.

CWE-732: the S3BucketLevel/S3ObjectLevel Allow grants use the kirocrew-deploy-*
wildcard, which also matches the audit bucket kirocrew-deploy-logs-* (CloudTrail
data events + S3 access logs). An explicit Deny (Deny overrides Allow) fences the
deploy principal out of Put/Delete on that bucket without affecting real deploys.
"""

from __future__ import annotations

from kiro_crew.deploy.iam import policy_document


def _by_sid(doc: dict, sid: str) -> dict | None:
    for st in doc["Statement"]:
        if st.get("Sid") == sid:
            return st
    return None


def test_policy_denies_audit_log_bucket_tamper():
    # Present in every tier (it lives in the base statements list).
    for tier in ("static", "fullstack"):
        doc = policy_document(tier=tier)
        deny = _by_sid(doc, "S3DenyAuditLogTamper")
        assert deny is not None, f"{tier}: S3DenyAuditLogTamper missing"
        assert deny["Effect"] == "Deny"
        acts = deny["Action"] if isinstance(deny["Action"], list) else [deny["Action"]]
        for need in ("s3:PutObject", "s3:DeleteObject", "s3:DeleteBucket"):
            assert need in acts, f"{tier}: Deny missing {need}"
        res = deny["Resource"] if isinstance(deny["Resource"], list) else [deny["Resource"]]
        assert res and all("kirocrew-deploy-logs-" in r for r in res), res
        # The legitimate object-level Allow is still present (real deploys work).
        assert _by_sid(doc, "S3ObjectLevel") is not None, f"{tier}: S3ObjectLevel missing"

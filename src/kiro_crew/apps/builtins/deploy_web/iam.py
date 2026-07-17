"""deploy-web IAM — least-privilege policy generator + read-only reachability check.

The policy text is *generated for the user to apply themselves* (design §12, Option A);
KiroCrew never performs an IAM write. Verification is **read-only reachability only**
(§9.3/Q3): it confirms the profile resolves and the services are reachable — it CANNOT
confirm create/write perms without writing (CloudFront has no --dry-run), so it is
labelled "access reachable", and the first real deploy is the true permission test.
"""
from __future__ import annotations

import json
from typing import Any

from kiro_crew.apps.builtins.deploy_web import engine

# Scoping levers (design §7): S3 name prefix + CloudFront resource tag.
S3_PREFIX = "kirocrew-web-*"
MANAGED_TAG_KEY = "kirocrew:managed"


def policy_document(*, include_custom_domain: bool = False) -> dict[str, Any]:
    """Return the cycle-006 least-privilege customer-managed policy as a dict."""
    statements: list[dict[str, Any]] = [
        {
            "Sid": "S3BucketLevel",
            "Effect": "Allow",
            "Action": [
                "s3:CreateBucket", "s3:ListBucket", "s3:GetBucketLocation",
                "s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:DeleteBucketPolicy",
                "s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock",
                "s3:PutBucketOwnershipControls", "s3:GetBucketOwnershipControls",
                "s3:PutEncryptionConfiguration", "s3:PutBucketTagging", "s3:DeleteBucket",
            ],
            "Resource": f"arn:aws:s3:::{S3_PREFIX}",
        },
        {
            "Sid": "S3ObjectLevel",
            "Effect": "Allow",
            "Action": [
                "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
                "s3:ListBucketMultipartUploads", "s3:AbortMultipartUpload",
            ],
            "Resource": f"arn:aws:s3:::{S3_PREFIX}/*",
        },
        {
            "Sid": "CloudFrontCreateList",
            "Effect": "Allow",
            "Action": [
                "cloudfront:CreateDistribution", "cloudfront:CreateDistributionWithTags",
                "cloudfront:CreateOriginAccessControl", "cloudfront:ListDistributions",
                "cloudfront:ListOriginAccessControls", "cloudfront:TagResource",
                "cloudfront:ListTagsForResource",
            ],
            "Resource": "*",
        },
        {
            "Sid": "CloudFrontManageTagged",
            "Effect": "Allow",
            "Action": [
                "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig",
                "cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution",
                "cloudfront:GetOriginAccessControl", "cloudfront:UpdateOriginAccessControl",
                "cloudfront:DeleteOriginAccessControl", "cloudfront:CreateInvalidation",
                "cloudfront:GetInvalidation", "cloudfront:ListInvalidations",
            ],
            "Resource": "*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            "Sid": "DiscoveryAndIdentity",
            "Effect": "Allow",
            "Action": ["tag:GetResources", "sts:GetCallerIdentity", "s3:ListAllMyBuckets"],
            "Resource": "*",
        },
    ]
    if include_custom_domain:
        statements += [
            {
                "Sid": "AcmForCloudFront",
                "Effect": "Allow",
                "Action": ["acm:RequestCertificate", "acm:DescribeCertificate",
                           "acm:ListCertificates", "acm:GetCertificate",
                           "acm:AddTagsToCertificate", "acm:DeleteCertificate"],
                "Resource": "*",
            },
            {
                "Sid": "Route53Alias",
                "Effect": "Allow",
                "Action": ["route53:ListHostedZones", "route53:GetHostedZone",
                           "route53:ListResourceRecordSets", "route53:GetChange"],
                "Resource": "*",
            },
        ]
    return {"Version": "2012-10-17", "Statement": statements}


def policy_json(*, include_custom_domain: bool = False) -> str:
    return json.dumps(policy_document(include_custom_domain=include_custom_domain), indent=2)


def reachability_check(profile: str) -> dict[str, Any]:
    """Read-only reachability (NOT full verification, §9.3/Q3).

    Confirms the profile resolves (sts:GetCallerIdentity) and that S3/CloudFront
    are reachable (harmless list calls). Returns a dict the UI/skill can render.
    Never mutates anything; the first deploy is the real permission test.
    """
    result: dict[str, Any] = {
        "reachable": False, "account": "", "s3_reachable": False,
        "cloudfront_reachable": False, "note": "", "detail": "",
    }
    rc, out, err = engine.run_aws(["sts", "get-caller-identity", "--output", "json"], profile)
    if rc != 0:
        result["detail"] = (err or "could not resolve credentials").strip()[:200]
        result["note"] = "Profile did not resolve — run `aws sso login --profile <name>` and retry."
        return result
    try:
        result["account"] = json.loads(out or "{}").get("Account", "")
    except json.JSONDecodeError:
        pass
    result["reachable"] = True

    s3_rc, _o, _e = engine.run_aws(["s3api", "list-buckets", "--output", "json"], profile)
    result["s3_reachable"] = s3_rc == 0
    cf_rc, _o2, _e2 = engine.run_aws(["cloudfront", "list-distributions", "--output", "json"], profile)
    result["cloudfront_reachable"] = cf_rc == 0

    result["note"] = ("Access reachable (not fully verified — create/write perms can't be "
                      "checked without writing). First deploy is the real test; on AccessDenied "
                      "the exact missing IAM statement is reported.")
    return result

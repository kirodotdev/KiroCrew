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

from kiro_crew.deploy import engine
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Scoping levers (design §7): S3 name prefix + CloudFront resource tag.
# The bucket template creates kirocrew-deploy-${AccountId}-${Region}; the
# engine.py HTTP-publish path creates kirocrew-web-<random>. The IAM policy
# must cover BOTH prefixes to work with both code paths.
S3_PREFIX = "kirocrew-deploy-*"
S3_PREFIX_WEB = f"arn:aws:s3:::{engine.BUCKET_PREFIX}*"
MANAGED_TAG_KEY = "kirocrew:managed"

# R23 F1: name of the customer-managed permissions-boundary policy that MUST
# be attached to every role the fullstack deploy principal creates. Without a
# boundary, iam:CreateRole + iam:PutRolePolicy + iam:PassRole (to Lambda) +
# lambda:InvokeFunction composes into account-admin privilege escalation: the
# principal creates a prefixed role, attaches an arbitrary inline policy,
# passes it to a Lambda, and invokes attacker-controlled code under it.
BOUNDARY_POLICY_NAME = "kirocrew-deploy-app-boundary"


def boundary_policy_document() -> dict[str, Any]:
    """Permissions boundary for app exec/authz roles (R23 F1).

    Caps the EFFECTIVE permissions of every role created by the fullstack
    deploy principal to what the app templates legitimately need: CloudWatch
    Logs (AWSLambdaBasicExecutionRole equivalent) + DynamoDB CRUD on this
    feature's own tables. Any inline policy beyond this cap is inert.
    The user creates this once: aws iam create-policy
    --policy-name kirocrew-deploy-app-boundary --policy-document file://...
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "LambdaLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup", "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "arn:aws:logs:*:*:*",
            },
            {
                "Sid": "OwnTableCrud",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem", "dynamodb:PutItem",
                    "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:Query", "dynamodb:Scan",
                    "dynamodb:BatchWriteItem", "dynamodb:BatchGetItem",
                ],
                "Resource": "arn:aws:dynamodb:*:*:table/kirocrew-deploy-app-*",
            },
            {
                # Cap for the reaper Lambda's role (same boundary, union cap —
                # app roles are still limited to what their templates grant).
                "Sid": "ReaperOpsCap",
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket", "s3:GetObject", "s3:DeleteObject",
                    "s3:PutObject", "s3:DeleteBucket", "s3:GetBucketTagging",
                ],
                "Resource": [
                    f"arn:aws:s3:::{S3_PREFIX}", f"arn:aws:s3:::{S3_PREFIX}/*",
                    S3_PREFIX_WEB, f"{S3_PREFIX_WEB}/*",
                ],
            },
            {
                # R27 F2 (Claude): the boundary INTERSECTS with inline policies,
                # so an unconditioned Resource:"*" here would let a boundaried
                # role's inline {cloudfront:DeleteDistribution:"*"} delete ANY
                # distribution in the account — re-opening the exact escalation
                # chain the boundary exists to close. Destructive/mutating
                # distribution actions are therefore tag-scoped to
                # kirocrew-managed resources only.
                "Sid": "ReaperCloudFrontMutateTaggedCap",
                "Effect": "Allow",
                "Action": [
                    "cloudfront:UpdateDistribution",
                    "cloudfront:DeleteDistribution",
                    "cloudfront:CreateInvalidation",
                ],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"
                    }
                },
            },
            {
                # Read/list CloudFront actions are non-destructive; OAC delete
                # cannot be tag-conditioned (OACs are an untaggable resource
                # type — R13/R22) and its blast radius is quota churn, not
                # data destruction. The reaper additionally verifies OAC names
                # against the per-deployment pattern before deleting (R18 F3).
                "Sid": "ReaperCloudFrontReadAndOacCap",
                "Effect": "Allow",
                "Action": [
                    "cloudfront:GetDistributionConfig",
                    "cloudfront:ListTagsForResource",
                    "cloudfront:ListOriginAccessControls",
                    "cloudfront:GetOriginAccessControl",
                    "cloudfront:DeleteOriginAccessControl",
                ],
                "Resource": "*",
            },
            {
                "Sid": "ReaperCfnCap",
                "Effect": "Allow",
                "Action": ["cloudformation:DescribeStacks", "cloudformation:DeleteStack"],
                "Resource": "arn:aws:cloudformation:*:*:stack/kirocrew-deploy-app-*/*",
            },
            {
                "Sid": "ReaperStsCap",
                "Effect": "Allow",
                "Action": ["sts:GetCallerIdentity"],
                "Resource": "*",
            },
            {
                # R25 F3: the boundary INTERSECTS with the role policy — it
                # must cover everything reaper.yaml's role legitimately does,
                # or the reaper is silently broken. Cascade cleanup surface:
                "Sid": "ReaperCascadeCap",
                "Effect": "Allow",
                "Action": [
                    "lambda:GetFunction", "lambda:DeleteFunction",
                    "lambda:RemovePermission", "lambda:GetFunctionUrlConfig",
                    "lambda:DeleteFunctionUrlConfig",
                    "dynamodb:DescribeTable", "dynamodb:DeleteTable",
                ],
                "Resource": [
                    "arn:aws:lambda:*:*:function:kirocrew-deploy-app-*",
                    "arn:aws:dynamodb:*:*:table/kirocrew-deploy-app-*",
                ],
            },
            {
                "Sid": "ReaperCascadeIamCap",
                "Effect": "Allow",
                "Action": [
                    "iam:GetRole", "iam:DeleteRole", "iam:DeleteRolePolicy",
                    "iam:DetachRolePolicy", "iam:ListRolePolicies",
                    "iam:ListAttachedRolePolicies",
                ],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-app-*",
            },
            {
                "Sid": "ReaperApiGwCap",
                "Effect": "Allow",
                "Action": ["apigateway:GET", "apigateway:DELETE"],
                "Resource": [
                    "arn:aws:apigateway:*::/apis",
                    "arn:aws:apigateway:*::/apis/*",
                ],
            },
        ],
    }


def policy_document(*, include_custom_domain: bool = False, tier: str = "static") -> dict[str, Any]:
    """Return the least-privilege customer-managed policy as a dict.

    Tiers:
      - static (default): S3, CloudFront, CloudFormation on the base stack.
      - fullstack: adds scoped Lambda, API Gateway, DynamoDB, IAM PassRole
        for kirocrew-deploy-app-* stacks, and CloudFront invalidation.
    """
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
                # Versioning + lifecycle on the retained OriginBucket:
                # CFN calls these bucket-level APIs during base-stack create/update.
                "s3:PutBucketVersioning", "s3:GetBucketVersioning",
                "s3:PutLifecycleConfiguration", "s3:GetLifecycleConfiguration",
            ],
            "Resource": [f"arn:aws:s3:::{S3_PREFIX}", S3_PREFIX_WEB],
        },
        {
            "Sid": "S3ObjectLevel",
            "Effect": "Allow",
            "Action": [
                "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
                "s3:ListBucketMultipartUploads", "s3:AbortMultipartUpload",
            ],
            "Resource": [f"arn:aws:s3:::{S3_PREFIX}/*", f"{S3_PREFIX_WEB}/*"],
        },
        {
            "Sid": "CloudFrontCreateList",
            "Effect": "Allow",
            "Action": [
                "cloudfront:CreateDistribution", "cloudfront:CreateDistributionWithTags",
                "cloudfront:CreateOriginAccessControl", "cloudfront:ListDistributions",
                "cloudfront:ListOriginAccessControls", "cloudfront:TagResource",
                "cloudfront:ListTagsForResource",
                # CloudFront Function (index-rewrite) — created by base-stack.yaml.
                "cloudfront:CreateFunction", "cloudfront:DescribeFunction",
                "cloudfront:PublishFunction", "cloudfront:ListFunctions",
                # ResponseHeadersPolicy (security headers) — created by base-stack.yaml.
                "cloudfront:CreateResponseHeadersPolicy",
                "cloudfront:GetResponseHeadersPolicy",
                "cloudfront:ListResponseHeadersPolicies",
            ],
            "Resource": "*",
        },
        {
            "Sid": "CloudFrontManageTagged",
            "Effect": "Allow",
            "Action": [
                "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig",
                "cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution",
                "cloudfront:CreateInvalidation",
                "cloudfront:GetInvalidation", "cloudfront:ListInvalidations",
                # Function + headers policy updates on tagged distributions.
                "cloudfront:UpdateFunction", "cloudfront:DeleteFunction",
                "cloudfront:UpdateResponseHeadersPolicy",
                "cloudfront:DeleteResponseHeadersPolicy",
            ],
            "Resource": "*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            # OAC resources do NOT support resource tags (CloudFront limitation),
            # so the kirocrew:managed tag condition can never match and no
            # resource-level narrowing is possible. R39 F1 (revising R32 F2):
            # account-wide Delete on the DEPLOY profile lets any compromised
            # deploy session delete every OAC in the account, including ones
            # belonging to unrelated production distributions — so Delete is
            # REMOVED here. OAC cleanup belongs to the reaper role (a
            # separately controlled, boundary-capped identity with R18's
            # name-fullmatch verification); destroy() reports the deferred
            # cleanup explicitly instead of claiming complete destruction.
            "Sid": "CloudFrontOACManage",
            "Effect": "Allow",
            "Action": [
                "cloudfront:GetOriginAccessControl",
                "cloudfront:CreateOriginAccessControl",
            ],
            "Resource": "arn:aws:cloudfront::${aws:PrincipalAccount}:origin-access-control/*",
        },
        {
            "Sid": "CloudFormationBaseStack",
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateStack", "cloudformation:UpdateStack",
                "cloudformation:DeleteStack", "cloudformation:DescribeStacks",
                "cloudformation:DescribeStackEvents", "cloudformation:GetTemplate",
                "cloudformation:ListStackResources",
                # `aws cloudformation deploy` works via change sets.
                "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
                "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
            ],
            "Resource": "arn:aws:cloudformation:*:*:stack/kirocrew-deploy-*/*",
        },
        {
            "Sid": "DiscoveryAndIdentity",
            "Effect": "Allow",
            "Action": ["tag:GetResources", "sts:GetCallerIdentity", "s3:ListAllMyBuckets"],
            "Resource": "*",
        },
    ]
    statements.append({
        "Sid": "CloudFormationValidate",
        "Effect": "Allow",
        # ValidateTemplate does not support resource-level scoping.
        "Action": ["cloudformation:ValidateTemplate"],
        "Resource": "*",
    })
    if tier == "fullstack":
        statements += [
            {
                "Sid": "LambdaFullstack",
                "Effect": "Allow",
                "Action": [
                    "lambda:CreateFunction", "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration", "lambda:DeleteFunction",
                    "lambda:GetFunction", "lambda:GetFunctionConfiguration",
                    "lambda:InvokeFunction", "lambda:AddPermission",
                    "lambda:RemovePermission", "lambda:TagResource",
                ],
                "Resource": "arn:aws:lambda:*:*:function:kirocrew-deploy-app-*",
            },
            {
                # R35 F1 + R39 F1: reads are unconditioned; unconditioned POST
                # is restricted to the /apis COLLECTION (API creation only).
                # POST beneath an existing API id (/apis/*) can create routes
                # and integrations under ANY API, so it moves into the
                # tag-conditioned statement below with the other mutations.
                "Sid": "ApiGatewayFullstackRead",
                "Effect": "Allow",
                "Action": ["apigateway:GET"],
                "Resource": [
                    "arn:aws:apigateway:*::/apis",
                    "arn:aws:apigateway:*::/apis/*",
                    "arn:aws:apigateway:*::/tags/*",
                ],
            },
            {
                "Sid": "ApiGatewayFullstackCreateRoot",
                "Effect": "Allow",
                "Action": ["apigateway:POST"],
                "Resource": "arn:aws:apigateway:*::/apis",
            },
            {
                # R35 F1: mutations gated on the kirocrew:managed tag — the
                # templates tag every AWS::ApiGatewayV2::Api at creation, so
                # only KiroCrew-created APIs are mutable/deletable. An
                # unrelated production API (no tag) is untouchable even with
                # its ID in hand. R39 F1: POST joins the gated set (route/
                # integration creation under an existing API is a mutation).
                "Sid": "ApiGatewayFullstackMutateManagedOnly",
                "Effect": "Allow",
                "Action": [
                    "apigateway:POST", "apigateway:PUT", "apigateway:PATCH",
                    "apigateway:DELETE",
                ],
                "Resource": [
                    "arn:aws:apigateway:*::/apis/*",
                    "arn:aws:apigateway:*::/tags/*",
                ],
                "Condition": {
                    "StringEquals": {"aws:ResourceTag/kirocrew:managed": "true"}
                },
            },
            {
                "Sid": "DynamoDBFullstack",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:CreateTable", "dynamodb:DeleteTable",
                    "dynamodb:DescribeTable", "dynamodb:UpdateTable",
                    "dynamodb:UpdateContinuousBackups",  # enable PITR
                    "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem",
                    "dynamodb:Query", "dynamodb:Scan", "dynamodb:TagResource",
                ],
                "Resource": "arn:aws:dynamodb:*:*:table/kirocrew-deploy-app-*",
            },
            {
                # R23 F1: CreateRole is only allowed when the new role carries
                # the kirocrew-deploy-app-boundary permissions boundary — this
                # caps whatever inline policies are later attached, closing the
                # CreateRole+PutRolePolicy+PassRole privilege-escalation chain.
                # R32 F3 (self-inflicted by R29): the deploy-backend.sh /
                # install-reaper.sh boundary preflight calls iam:GetPolicy, but
                # the generated policy never granted it — a principal using
                # exactly the documented policy got AccessDenied, which the
                # preflight misreported as "boundary absent". Read-only, scoped
                # to the single boundary policy ARN.
                "Sid": "BoundaryPreflightRead",
                "Effect": "Allow",
                "Action": ["iam:GetPolicy"],
                "Resource": (
                    "arn:aws:iam::${aws:PrincipalAccount}:policy/"
                    + BOUNDARY_POLICY_NAME
                ),
            },
            {
                "Sid": "IAMCreateRoleWithBoundaryOnly",
                "Effect": "Allow",
                "Action": ["iam:CreateRole"],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-app-*",
                "Condition": {
                    "ArnLike": {
                        "iam:PermissionsBoundary": (
                            f"arn:aws:iam::*:policy/{BOUNDARY_POLICY_NAME}"
                        )
                    }
                },
            },
            {
                "Sid": "IAMRoleLifecycleFullstack",
                "Effect": "Allow",
                "Action": [
                    "iam:DeleteRole", "iam:GetRole",
                    "iam:PutRolePolicy", "iam:DeleteRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:TagRole",
                ],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-app-*",
            },
            {
                # R23 F1: the boundary is only effective if it cannot be
                # removed or swapped by the same principal.
                "Sid": "IAMDenyBoundaryTampering",
                "Effect": "Deny",
                "Action": [
                    "iam:DeleteRolePermissionsBoundary",
                    "iam:PutRolePermissionsBoundary",
                ],
                "Resource": [
                    "arn:aws:iam::*:role/kirocrew-deploy-app-*",
                    "arn:aws:iam::*:role/kirocrew-deploy-reaper*",
                ],
            },
            {
                "Sid": "IAMAttachRolePolicyFullstack",
                "Effect": "Allow",
                "Action": ["iam:AttachRolePolicy"],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-app-*",
                "Condition": {
                    "ArnEquals": {
                        "iam:PolicyARN": [
                            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        ]
                    }
                },
            },
            {
                "Sid": "IAMPassRoleLambdaOnly",
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-app-*",
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": "lambda.amazonaws.com"
                    }
                },
            },
            {
                "Sid": "ReaperLambda",
                "Effect": "Allow",
                "Action": [
                    "lambda:CreateFunction", "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration", "lambda:DeleteFunction",
                    "lambda:GetFunction", "lambda:GetFunctionConfiguration",
                    "lambda:InvokeFunction", "lambda:AddPermission",
                    "lambda:RemovePermission", "lambda:TagResource",
                ],
                "Resource": "arn:aws:lambda:*:*:function:kirocrew-deploy-reaper*",
            },
            {
                "Sid": "ReaperCloudFormation",
                "Effect": "Allow",
                "Action": [
                    "cloudformation:CreateStack", "cloudformation:UpdateStack",
                    "cloudformation:DeleteStack", "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents", "cloudformation:GetTemplate",
                    "cloudformation:ListStackResources",
                    "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
                    "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
                ],
                "Resource": "arn:aws:cloudformation:*:*:stack/kirocrew-deploy-reaper/*",
            },
            {
                # R23 F1: same boundary requirement as app roles — CreateRole
                # without a boundary + PutRolePolicy + PassRole is the same
                # privilege-escalation chain on the reaper prefix.
                "Sid": "ReaperIAMCreateRoleWithBoundaryOnly",
                "Effect": "Allow",
                "Action": ["iam:CreateRole"],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-reaper*",
                "Condition": {
                    "ArnLike": {
                        "iam:PermissionsBoundary": (
                            f"arn:aws:iam::*:policy/{BOUNDARY_POLICY_NAME}"
                        )
                    }
                },
            },
            {
                "Sid": "ReaperIAMRole",
                "Effect": "Allow",
                "Action": [
                    "iam:DeleteRole", "iam:GetRole",
                    "iam:PutRolePolicy", "iam:DeleteRolePolicy",
                    "iam:AttachRolePolicy", "iam:DetachRolePolicy",
                    "iam:TagRole",
                ],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-reaper*",
            },
            {
                # PassRole scoped to lambda only — the reaper execution role is
                # passed solely to the EventBridge-timed reaper Lambda. Mirrors
                # IAMPassRoleLambdaOnly for app roles so the role can't be
                # passed to any other service (resource prefix bounds WHICH
                # role; PassedToService bounds TO WHICH service).
                "Sid": "ReaperPassRoleLambdaOnly",
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": "arn:aws:iam::*:role/kirocrew-deploy-reaper*",
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": "lambda.amazonaws.com"
                    }
                },
            },
            {
                "Sid": "ReaperEvents",
                "Effect": "Allow",
                "Action": [
                    "events:PutRule", "events:PutTargets",
                    "events:DeleteRule", "events:RemoveTargets",
                    "events:DescribeRule", "events:ListTargetsByRule",
                ],
                "Resource": "arn:aws:events:*:*:rule/kirocrew-deploy-reaper*",
            },
            {
                # OAC Update/Delete only needed by the reaper for quota cleanup.
                # Scoped to the caller's own account via aws:PrincipalAccount
                # policy variable (OAC cannot be tagged — see CloudFrontOACManage).
                "Sid": "ReaperOACCleanup",
                "Effect": "Allow",
                "Action": [
                    "cloudfront:UpdateOriginAccessControl",
                    "cloudfront:DeleteOriginAccessControl",
                    "cloudfront:GetOriginAccessControl",
                ],
                "Resource": "arn:aws:cloudfront::${aws:PrincipalAccount}:origin-access-control/*",
            },
        ]
    if include_custom_domain:
        statements += [
            {
                "Sid": "AcmForCloudFront",
                "Effect": "Allow",
                "Action": ["acm:RequestCertificate", "acm:DescribeCertificate",
                           "acm:ListCertificates", "acm:GetCertificate",
                           "acm:AddTagsToCertificate"],
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


def policy_json(*, include_custom_domain: bool = False, tier: str = "static") -> str:
    return json.dumps(policy_document(include_custom_domain=include_custom_domain, tier=tier), indent=2)


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
        raw = (err or "could not resolve credentials").strip()[:200]
        raw, _ = redact_credentials(raw)
        raw, _ = redact_exfiltration_urls(raw)
        result["detail"] = raw
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

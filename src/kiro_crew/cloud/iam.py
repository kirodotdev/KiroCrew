"""cloud IAM — least-privilege policy generator + read-only reachability check.

Same philosophy as ``deploy_web/iam.py``: **KiroCrew never performs an IAM
write.** We *generate* the policy text for the user to apply themselves (or they
already have it, being the account owner). Verification is **read-only
reachability only** — ``sts get-caller-identity`` plus harmless ``describe``
calls; the first ``cloudformation deploy`` is the true permission test, and on
``AccessDenied`` the exact missing action is surfaced (see ``cloud.aws``).

The launch path is heavier than deploy-web's: it creates an IAM role and passes
it to EC2 (``iam:PassRole``), which requires ``CAPABILITY_IAM`` on the deploy.
We surface that honestly in the wizard rather than trying to shrink below what
CloudFormation needs.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.cloud import aws

# Resource tag every launcher-created resource carries (mirrors deploy-web's
# kirocrew:managed). Scopes the tag-conditioned statements below.
MANAGED_TAG_KEY = "kirocrew:managed"

# The IAM role name pattern the template uses; PassRole is scoped to it.
ROLE_NAME_PREFIX = "kirocrew-ec2-"

# The permissions-boundary managed-policy name. This is a SINGLE, shared,
# account/region-agnostic, CONTENT-FIXED managed policy (NO per-tag suffix), so
# its content is identical for every launch and it can be created ONCE and reused
# immutably. It is created by launcher CODE (source.ensure_instance_boundary),
# NOT per-launch CloudFormation, and the launcher policy grants only
# CreatePolicy/GetPolicy on this exact name (never CreatePolicyVersion/Delete*),
# so once the correct boundary exists a leaked launcher credential cannot make it
# permissive. iam:CreateRole is gated on this name so a kirocrew-ec2-* role MUST
# carry it. NB: this is a superset match of ROLE_NAME_PREFIX + "boundary", kept
# distinct so the PermissionsBoundary condition can't be satisfied by a role.
BOUNDARY_NAME = "kirocrew-ec2-boundary"

# Back-compat alias: several call sites and tests referred to the (now-removed)
# per-tag prefix. The name is exact now, so the "prefix" IS the full name.
BOUNDARY_NAME_PREFIX = BOUNDARY_NAME


def boundary_arn(account: str) -> str:
    """The deterministic ARN of the shared, immutable instance permissions boundary.

    ``account`` is the 12-digit AWS account id. The launcher fills this into the
    template's ``PermissionsBoundaryArn`` parameter and
    :func:`source.ensure_instance_boundary` creates the policy at this ARN once.
    """
    return f"arn:aws:iam::{account}:policy/{BOUNDARY_NAME}"


# The exact AmazonSSMManagedInstanceCore action set (Session Manager + messages).
# The instance MUST be able to run all of these to register with SSM. This is
# the content-fixed floor of the shared boundary and mirrors the action list in
# the AWS-managed policy of the same name — kept here (not attached) so the
# boundary is self-contained + immutable.
_SSM_CORE_ACTIONS = [
    "ssm:DescribeAssociation",
    "ssm:GetDeployablePatchSnapshotForInstance",
    "ssm:GetDocument",
    "ssm:DescribeDocument",
    "ssm:GetManifest",
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:ListAssociations",
    "ssm:ListInstanceAssociations",
    "ssm:PutInventory",
    "ssm:PutComplianceItems",
    "ssm:PutConfigurePackageResult",
    "ssm:UpdateAssociationStatus",
    "ssm:UpdateInstanceAssociationStatus",
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ec2messages:AcknowledgeMessage",
    "ec2messages:DeleteMessage",
    "ec2messages:FailMessage",
    "ec2messages:GetEndpoint",
    "ec2messages:GetMessages",
    "ec2messages:SendReply",
]


def boundary_policy_document(account: str = "*") -> dict[str, Any]:
    """The CONTENT-FIXED document of the shared instance permissions boundary.

    A permissions boundary is a CEILING, not a grant, so making it identical for
    every launch is safe: the ``s3:GetObject`` statement can cover the WHOLE
    launcher bucket prefix (``kirocrew-src-<account>-*/*``) instead of the
    per-launch object, because the per-object restriction is ALREADY enforced by
    the role's INLINE ``SourceObjectRead`` policy (which stays per-tag,
    derived-ARN — see the template). The boundary therefore = the exact SSM-core
    action set + ``s3:GetObject`` on the account's launcher buckets.

    IAM policies are GLOBAL (one boundary per account, not per-region), so the S3
    resource is region-agnostic (``kirocrew-src-<account>-*``) — a byte-stable
    document per account so the create-once policy is genuinely reusable and
    immutable across every region the operator launches in. ``account="*"`` yields
    the fully-wildcard form used only for display/tests; the real create path
    passes the resolved 12-digit account id.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "SsmCore",
                "Effect": "Allow",
                "Action": list(_SSM_CORE_ACTIONS),
                "Resource": "*",
            },
            {
                # Launcher-bucket read. Covering the whole account bucket prefix
                # (all regions) is safe (a boundary only CAPS; it never grants):
                # the role's inline SourceObjectRead policy still pins the actual
                # read to this launch's single derived object.
                "Sid": "SourceBucketRead",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::kirocrew-src-{account}-*/*",
            },
        ],
    }


def boundary_policy_json(account: str = "*") -> str:
    """The boundary document as compact JSON (what ``iam create-policy`` receives)."""
    return json.dumps(boundary_policy_document(account))


# The CloudFormation stack-name prefix (mirrors ec2.STACK_PREFIX); the stack
# mutation/delete statement is scoped to it so the policy can't touch unrelated
# stacks. Kept as a local constant to avoid importing the ec2 module here.
STACK_PREFIX = "kirocrew-"


def policy_document() -> dict[str, Any]:
    """Return the least-privilege customer-managed policy for the launcher."""
    statements: list[dict[str, Any]] = [
        {
            # Stack-mutating verbs scoped to kirocrew-* stacks so the applied
            # policy can't create/update/delete UNRELATED stacks.
            "Sid": "CloudFormationStackMutate",
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateStack",
                "cloudformation:UpdateStack",
                "cloudformation:DeleteStack",
                "cloudformation:DescribeStacks",
                "cloudformation:DescribeStackEvents",
                "cloudformation:DescribeStackResources",
            ],
            "Resource": f"arn:aws:cloudformation:*:*:stack/{STACK_PREFIX}*/*",
        },
        {
            # Change-set verbs (`aws cloudformation deploy` always goes through a
            # change set) authorize on the change-set ARN
            # (changeSet/<name>/<id>), NOT just the stack ARN — scoping only to
            # stack/* would deny the launch under the generated policy. Grant
            # BOTH the changeSet and stack ARN forms, still kirocrew-*-scoped.
            "Sid": "CloudFormationChangeSet",
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateChangeSet",
                "cloudformation:ExecuteChangeSet",
                "cloudformation:DescribeChangeSet",
                "cloudformation:DeleteChangeSet",
            ],
            "Resource": [
                f"arn:aws:cloudformation:*:*:changeSet/{STACK_PREFIX}*/*",
                f"arn:aws:cloudformation:*:*:stack/{STACK_PREFIX}*/*",
            ],
        },
        {
            # Account-wide read-only calls that can't be stack-ARN-scoped:
            # ListStacks enumerates, GetTemplateSummary validates the template
            # body before a stack exists.
            "Sid": "CloudFormationRead",
            "Effect": "Allow",
            "Action": [
                "cloudformation:ListStacks",
                "cloudformation:GetTemplateSummary",
            ],
            "Resource": "*",
        },
        {
            # `kirocrew cloud list` discovers instances via the tagging API.
            "Sid": "TagDiscovery",
            "Effect": "Allow",
            "Action": ["tag:GetResources"],
            "Resource": "*",
        },
        {
            "Sid": "Ec2Discovery",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceStatus",
                "ec2:DescribeImages",
                "ec2:DescribeVpcs",
                "ec2:DescribeSubnets",
                "ec2:DescribeRouteTables",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeKeyPairs",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeInstanceTypeOfferings",
            ],
            "Resource": "*",
        },
        {
            # ec2:RunInstances — request-tag-gated on the NEW instance only.
            #
            # RunInstances authorizes PER-RESOURCE across every ARN the call
            # touches: the new instance/volume/network-interface it creates AND the
            # pre-existing image/subnet/security-group it references. aws:RequestTag
            # is evaluated per-resource, so requiring kirocrew:managed=true on the
            # WHOLE action denies the launch — the referenced (existing, untagged)
            # security group + subnet + AMI don't carry the request tag, and this
            # template's TagSpecifications tag only the INSTANCE (not the volume or
            # ENI). Empirically confirmed with a least-privilege assume-role
            # `run-instances --dry-run`: a blanket request-tag on `security-group/*`
            # DENIED an otherwise-correct tagged launch on the referenced SG.
            #
            # So: the request-tag condition applies ONLY to `instance/*` (the one
            # resource CFN tags and the only one an escalation cares about — a
            # leaked credential can't RunInstances an UNtagged instance, which
            # would sit outside the tag-gated Stop/Terminate statements). All the
            # other ARNs a launch legitimately needs (its own volume/ENI + the
            # referenced image/subnet/SG/key-pair) are granted WITHOUT the
            # condition — they can't create a tag-gated resource on their own (the
            # volume/ENI exist only as sub-resources of the tagged instance in the
            # same call; the rest are read-only references). Validated end-to-end
            # with a live CREATE_COMPLETE + SSM Online launch (admin launches
            # bypass the policy, so the dry-run/harness is the condition oracle).
            "Sid": "Ec2RunInstancesTaggedInstance",
            "Effect": "Allow",
            "Action": ["ec2:RunInstances"],
            "Resource": "arn:aws:ec2:*:*:instance/*",
            "Condition": {"StringEquals": {f"aws:RequestTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            "Sid": "Ec2RunInstancesSupportingResources",
            "Effect": "Allow",
            "Action": ["ec2:RunInstances"],
            "Resource": [
                "arn:aws:ec2:*:*:volume/*",
                "arn:aws:ec2:*:*:network-interface/*",
                "arn:aws:ec2:*:*:subnet/*",
                "arn:aws:ec2:*:*:security-group/*",
                "arn:aws:ec2:*:*:key-pair/*",
                "arn:aws:ec2:*:*:elastic-ip/*",
                "arn:aws:ec2:*::image/*",
                "arn:aws:ec2:*::snapshot/*",
            ],
        },
        {
            # ec2:CreateSecurityGroup — request-tag-gated on the NEW security group.
            # CreateSecurityGroup creates the SG (tagged kirocrew:managed=true at
            # creation by the template's TagSpecifications) and authorizes against
            # the target vpc/*. Require the request tag on `security-group/*` (so a
            # leaked credential can't create an UNtagged SG that escapes the
            # tag-gated Authorize/Delete statements) and allow `vpc/*` unconditioned
            # (pre-existing, referenced, not tagged by this call). Validated with
            # the least-privilege `create-security-group --dry-run`.
            "Sid": "Ec2CreateSecurityGroupTagged",
            "Effect": "Allow",
            "Action": ["ec2:CreateSecurityGroup"],
            "Resource": "arn:aws:ec2:*:*:security-group/*",
            "Condition": {"StringEquals": {f"aws:RequestTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            "Sid": "Ec2CreateSecurityGroupVpc",
            "Effect": "Allow",
            "Action": ["ec2:CreateSecurityGroup"],
            "Resource": "arn:aws:ec2:*:*:vpc/*",
        },
        {
            # SG rule mutation is gated to KiroCrew-tagged security groups: the
            # stack's SG carries kirocrew:managed=true from creation
            # (TagSpecifications), so CFN can add its egress/SSH rules — but a
            # leaked launcher credential can't Authorize/Revoke rules on an
            # UNRELATED security group (which would expose other account
            # resources). Revoke is already in Ec2DestructiveTagged; Authorize is
            # here.
            "Sid": "Ec2SecurityGroupRulesTagged",
            "Effect": "Allow",
            "Action": [
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:AuthorizeSecurityGroupIngress",
            ],
            "Resource": "*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            # Tagging is allowed ONLY as part of a create operation
            # (RunInstances / CreateSecurityGroup) via the ec2:CreateAction
            # condition. Without this, ec2:CreateTags on "*" would let a leaked
            # launcher credential tag ANY existing EC2 resource with
            # kirocrew:managed=true and thereby bring it under the tag-gated
            # Stop/Terminate/DeleteSecurityGroup statements below — subverting
            # the very control those tags gate. CFN tags the instance + SG at
            # creation (TagSpecifications in the create call), so this covers the
            # legitimate path while blocking standalone re-tagging.
            "Sid": "Ec2TagOnCreate",
            "Effect": "Allow",
            "Action": ["ec2:CreateTags"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "ec2:CreateAction": ["RunInstances", "CreateSecurityGroup"],
                }
            },
        },
        {
            # Destructive verbs, gated to KiroCrew-tagged resources so a leaked
            # launcher credential can't delete/retag security groups it never
            # created. The stack's SG carries kirocrew:managed=true from creation.
            "Sid": "Ec2DestructiveTagged",
            "Effect": "Allow",
            "Action": [
                "ec2:RevokeSecurityGroupIngress",
                "ec2:DeleteSecurityGroup",
                "ec2:DeleteTags",
            ],
            "Resource": "*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            "Sid": "Ec2LifecycleTagged",
            "Effect": "Allow",
            "Action": [
                "ec2:StopInstances",
                "ec2:StartInstances",
                "ec2:TerminateInstances",
                "ec2:RebootInstances",
            ],
            "Resource": "*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            # Role create MUST carry our SHARED, PRE-CREATED permissions boundary.
            # iam:CreateRole is the enforcement point: a kirocrew-ec2-* role can
            # ONLY be created WITH our permissions boundary (iam:PermissionsBoundary
            # must ArnLike-match arn:...:policy/kirocrew-ec2-boundary). Because the
            # boundary is a single content-fixed policy the launcher creates ONCE
            # and can never re-version/delete (see IamInstanceBoundaryCreateOnce),
            # this ceiling is real even against a leaked LAUNCHER credential: it
            # can't author a permissive boundary at that name (CreatePolicy on an
            # existing name fails EntityAlreadyExists), so any role it creates is
            # capped to SSM-core + source read. A boundary set at creation can't be
            # removed by PutRolePolicy (only DeleteRolePermissionsBoundary does
            # that, which we don't grant), so every such role stays permanently
            # capped — even if an admin inline policy is later added, its EFFECTIVE
            # permissions can't exceed the boundary.
            #
            # NB: ArnLike (NOT StringEquals) — the condition value is a wildcard
            # ARN pattern (the account id is a `*` so one printed policy works in
            # any account); StringEquals does literal matching and would never
            # match a real boundary ARN, denying CreateRole entirely.
            "Sid": "IamCreateRoleWithBoundary",
            "Effect": "Allow",
            "Action": ["iam:CreateRole"],
            "Resource": f"arn:aws:iam::*:role/{ROLE_NAME_PREFIX}*",
            "Condition": {
                "ArnLike": {"iam:PermissionsBoundary": f"arn:aws:iam::*:policy/{BOUNDARY_NAME}"}
            },
        },
        {
            # PutRolePolicy is scoped to the kirocrew-ec2-* role ARN AND gated on
            # the role carrying our creation tag (aws:ResourceTag/
            # kirocrew:managed=true). The name-prefix scope alone let a leaked
            # launcher credential target a PRE-EXISTING kirocrew-ec2-* role that a
            # third party created out-of-band WITHOUT our permissions boundary,
            # inline an admin policy, and pass it to EC2. The tag condition makes
            # that non-spoofable: only a role WE created via CreateRole (which
            # applies Tags atomically — see the template's InstanceRole.Tags) is
            # tagged kirocrew:managed=true, and CreateRole is boundary-gated
            # (IamCreateRoleWithBoundary). A foreign same-named role won't match.
            #
            # NB: the role is tagged AT CreateRole (Tags is a CreateRole
            # parameter), so there is NO untagged window before CFN's subsequent
            # PutRolePolicy — the condition is satisfied on the legitimate deploy
            # path (validated live: role created+tagged+inline-policy'd, SSM
            # Online). We do NOT add an iam:PermissionsBoundary condition here:
            # that key isn't in PutRolePolicy's request context, so it would deny
            # the call. aws:ResourceTag (not iam:ResourceTag) — verified with the
            # IAM policy simulator that PutRolePolicy honors the global key.
            "Sid": "IamPutRolePolicyForInstance",
            "Effect": "Allow",
            "Action": ["iam:PutRolePolicy"],
            "Resource": f"arn:aws:iam::*:role/{ROLE_NAME_PREFIX}*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            # Non-escalating role/profile management (no boundary condition
            # needed: none of these can widen the role's permissions).
            #
            # NB: iam:TagRole is NOT here — it is boundary-gated in its OWN
            # statement below (IamTagRoleWithBoundary). Leaving it unconditioned
            # here would DEFEAT the aws:ResourceTag gate on PutRolePolicy/PassRole:
            # a leaked launcher credential could TAG a pre-existing, out-of-band,
            # unbounded kirocrew-ec2-* role as kirocrew:managed=true, then inline
            # admin + pass it to EC2. The tag is only non-spoofable if the same
            # policy can't apply it to an arbitrary role.
            "Sid": "IamRoleForInstance",
            "Effect": "Allow",
            "Action": [
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:GetRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:CreateInstanceProfile",
                "iam:DeleteInstanceProfile",
                "iam:GetInstanceProfile",
                "iam:AddRoleToInstanceProfile",
                "iam:RemoveRoleFromInstanceProfile",
            ],
            "Resource": [
                f"arn:aws:iam::*:role/{ROLE_NAME_PREFIX}*",
                f"arn:aws:iam::*:instance-profile/{ROLE_NAME_PREFIX}*",
            ],
        },
        {
            # iam:TagRole is REQUIRED because CloudFormation's CreateRole passes the
            # role's Tags inline (the template's InstanceRole.Tags), and AWS
            # authorizes that inline tagging as iam:TagRole (see AWS docs
            # id_tags_roles.html) — without it the boundary-gated CreateRole fails
            # 403 and the launch can't tag the role kirocrew:managed=true (which the
            # PutRolePolicy/PassRole gate then requires). But leaving it
            # unconditioned would DEFEAT that downstream tag gate: a leaked launcher
            # credential could tag a pre-existing, out-of-band, UNBOUNDED
            # kirocrew-ec2-* role kirocrew:managed=true, then inline admin + pass it
            # to EC2 (the tag is only non-spoofable if the same policy can't apply
            # it to an arbitrary role).
            #
            # Gate: aws:ResourceTag/kirocrew:managed=true — the role must ALREADY be
            # tagged managed. This is the non-spoofable distinguisher, proven live
            # with a least-privilege assumed-role principal:
            #   * (a) A boundary-gated CreateRole WITH inline Tags is ALLOWED: at
            #     CreateRole, AWS evaluates the embedded TagRole authorization with
            #     aws:ResourceTag reflecting the tags BEING applied, so the
            #     kirocrew:managed=true tag is already "present" in context → match.
            #   * (b) A STANDALONE tag-role on a pre-existing unbounded victim
            #     (which does NOT yet carry kirocrew:managed) is DENIED — the key is
            #     absent/mismatched → no match. So the launcher can tag roles it is
            #     creating (which already carry the tag) but CANNOT add the managed
            #     tag to a role that lacks it.
            # NB: we do NOT use iam:PermissionsBoundary here — validated live that
            # AWS does NOT propagate that key into the CreateRole-embedded TagRole
            # check (it DENIED case (a)); aws:ResourceTag is the key that works.
            # This closes the full chain: TagRole(victim) is denied, so
            # PutRolePolicy/PassRole never get their tag precondition either.
            "Sid": "IamTagRoleOnManaged",
            "Effect": "Allow",
            "Action": ["iam:TagRole"],
            "Resource": f"arn:aws:iam::*:role/{ROLE_NAME_PREFIX}*",
            "Condition": {"StringEquals": {f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            # The SHARED, IMMUTABLE instance permissions boundary. The launcher
            # CODE (source.ensure_instance_boundary) creates this ONCE, idempotently
            # (tolerating EntityAlreadyExists), from a content-fixed document — it
            # is NOT created per-launch by CloudFormation anymore. We grant ONLY
            # CreatePolicy + GetPolicy + GetPolicyVersion, scoped to the EXACT
            # boundary name (no wildcard suffix):
            #   * GetPolicy — check whether the boundary already exists + read its
            #     default version id.
            #   * GetPolicyVersion — read the existing boundary's document so the
            #     launcher can VERIFY it matches the content-fixed document before
            #     reusing it (source._verify_instance_boundary_content); a permissive
            #     boundary seeded at this name is detected + refused, not trusted.
            #   * CreatePolicy — create it the first time.
            # We deliberately do NOT grant CreatePolicyVersion / DeletePolicyVersion
            # / DeletePolicy / SetDefaultPolicyVersion. That is the crux of the fix:
            # CreatePolicy on a FIXED name fails with EntityAlreadyExists once the
            # boundary exists, and without a version/delete verb a holder of the
            # generated policy CANNOT replace the content of an existing boundary —
            # only (harmlessly) try to re-create the identical one. So the ceiling
            # is immutable against a leaked launcher credential, not just against
            # the on-box agent.
            #
            # Residual (documented in docs/setup/AS_BUILT.md): the first CreatePolicy
            # is a first-write race, but now for AVAILABILITY only — the launcher
            # verifies the existing boundary's content and FAILS CLOSED on a
            # mismatch, so a permissive boundary seeded at this name is refused
            # (never used to under-cap a role), it can only block launches (a DoS).
            # Operators who want to eliminate even that pre-create the boundary as an
            # admin (kirocrew cloud iam-boundary) and drop this statement — the
            # launcher then only *references* the boundary ARN.
            "Sid": "IamInstanceBoundaryCreateOnce",
            "Effect": "Allow",
            "Action": [
                "iam:CreatePolicy",
                "iam:GetPolicy",
                "iam:GetPolicyVersion",
            ],
            "Resource": f"arn:aws:iam::*:policy/{BOUNDARY_NAME}",
        },
        {
            # Attach/detach are split out and constrained by iam:PolicyARN to the
            # SINGLE AWS-managed policy the template attaches
            # (AmazonSSMManagedInstanceCore). Without this condition, a holder of
            # the launcher policy could AttachRolePolicy AdministratorAccess onto
            # a kirocrew-ec2-* role and pass it to EC2 — a full escalation. The
            # allowlist is a hard cap: no other managed policy can be attached.
            "Sid": "IamAttachManagedPolicyForInstance",
            "Effect": "Allow",
            "Action": [
                "iam:AttachRolePolicy",
                "iam:DetachRolePolicy",
            ],
            "Resource": f"arn:aws:iam::*:role/{ROLE_NAME_PREFIX}*",
            "Condition": {
                "ArnEquals": {
                    "iam:PolicyARN": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
                }
            },
        },
        {
            # PassRole is scoped to the kirocrew-ec2-* role ARN, gated on the EC2
            # service (iam:PassedToService) AND on our creation tag
            # (aws:ResourceTag/kirocrew:managed=true) — the same non-spoofable
            # constraint as PutRolePolicy. Without the tag, a leaked launcher
            # credential could pass a PRE-EXISTING, unbounded kirocrew-ec2-* role
            # (created out-of-band by a third party) to EC2. Only a role WE
            # created (boundary-gated CreateRole, tagged atomically) matches, so
            # the role EC2 receives is always boundary-capped. Both StringEquals
            # conditions must hold. (Validated live + policy simulator.)
            "Sid": "IamPassRoleToEc2",
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": f"arn:aws:iam::*:role/{ROLE_NAME_PREFIX}*",
            "Condition": {
                "StringEquals": {
                    "iam:PassedToService": "ec2.amazonaws.com",
                    f"aws:ResourceTag/{MANAGED_TAG_KEY}": "true",
                }
            },
        },
        {
            # The sensitive verbs — StartSession (interactive shell / tunnel)
            # and SendCommand (effectively RCE) — are gated to KiroCrew-tagged
            # INSTANCES so a leaked launcher credential can't open sessions or
            # run commands on every SSM-managed box in the account (mirrors
            # Ec2LifecycleTagged). The tag condition is on the instance resource
            # only: SSM documents don't carry our tag, so gating them too would
            # (per-resource evaluation) deny the whole call — the documents are
            # allowed unconditioned in the statement below.
            "Sid": "SsmSessionOnManagedInstances",
            "Effect": "Allow",
            "Action": [
                "ssm:StartSession",
                "ssm:SendCommand",
            ],
            "Resource": "arn:aws:ec2:*:*:instance/*",
            "Condition": {"StringEquals": {f"ssm:resourceTag/{MANAGED_TAG_KEY}": "true"}},
        },
        {
            # The SSM documents used by StartSession/SendCommand. These are
            # AWS-owned, not sensitive, and can't be tag-gated; the instance
            # tag condition above is what constrains WHERE the session/command
            # can actually land.
            "Sid": "SsmSessionDocuments",
            "Effect": "Allow",
            "Action": [
                "ssm:StartSession",
                "ssm:SendCommand",
            ],
            "Resource": [
                "arn:aws:ssm:*::document/AWS-StartPortForwardingSession",
                "arn:aws:ssm:*::document/AWS-RunShellScript",
                "arn:aws:ssm:*:*:session/*",
            ],
        },
        {
            # Read-only session/instance status the launcher actually uses. These
            # Describe*/Get* calls are read-only and not usefully resource-scoped.
            #
            # NB: ssm:TerminateSession / ssm:ResumeSession are NOT granted — the
            # launcher never calls them (the local session-manager-plugin child
            # owns port-forward teardown; nothing in cloud/ issues
            # `aws ssm terminate-session`/`resume-session`). Granting them on `*`
            # would let a leaked launcher credential disrupt UNRELATED SSM sessions
            # account-wide, and they can't be usefully scoped to caller-owned
            # sessions here — so we simply drop them (least privilege).
            #
            # GetCommandInvocation is the ONLY command-history read granted, and
            # only because run_command() must poll a send-command's result. We do
            # NOT grant ListCommandInvocations (never called by the launcher):
            # narrowing the command-history read surface limits how easily a
            # leaked launcher credential could enumerate + recover the dashboard
            # token that mint_token transits through send-command output (that
            # token is already TTL-short and loopback-only; this shrinks the
            # discovery path further). The agent chokepoint additionally denies
            # GetCommandInvocation from an agent session (see cloud.aws).
            "Sid": "SsmSessionControlAndRead",
            "Effect": "Allow",
            "Action": [
                "ssm:DescribeInstanceInformation",
                "ssm:DescribeSessions",
                "ssm:GetConnectionStatus",
                "ssm:GetCommandInvocation",
            ],
            "Resource": "*",
        },
        {
            "Sid": "SsmAmiParameter",
            "Effect": "Allow",
            "Action": ["ssm:GetParameter", "ssm:GetParameters"],
            "Resource": "arn:aws:ssm:*::parameter/aws/service/*",
        },
        {
            "Sid": "SourceBucket",
            "Effect": "Allow",
            "Action": [
                "s3:CreateBucket",
                # NB: no `s3:HeadBucket` — it isn't a real IAM action name (the
                # HeadBucket API is authorized by s3:ListBucket), and including
                # it makes the printed policy fail to create.
                "s3:PutBucketPublicAccessBlock",
                "s3:PutObject",
                "s3:GetObject",
                "s3:DeleteObject",
                "s3:ListBucket",
            ],
            "Resource": [
                "arn:aws:s3:::kirocrew-src-*",
                "arn:aws:s3:::kirocrew-src-*/*",
            ],
        },
        {
            "Sid": "Identity",
            "Effect": "Allow",
            "Action": ["sts:GetCallerIdentity"],
            "Resource": "*",
        },
    ]
    return {"Version": "2012-10-17", "Statement": statements}


def policy_json() -> str:
    """The policy as indented JSON text (what the user pastes into IAM)."""
    return json.dumps(policy_document(), indent=2)


def reachability_check(profile: str, region: str = "") -> dict[str, Any]:
    """Read-only reachability (NOT full verification).

    Confirms the profile resolves (``sts get-caller-identity``) and that EC2 +
    CloudFormation are reachable (harmless ``describe`` calls). Never mutates
    anything; the first real deploy is the true permission test.
    """
    result: dict[str, Any] = {
        "reachable": False,
        "account": "",
        "arn": "",
        "ec2_reachable": False,
        "cloudformation_reachable": False,
        "ssm_reachable": False,
        "note": "",
        "detail": "",
    }
    rc, out, err = aws.run_aws(["sts", "get-caller-identity", "--output", "json"], profile, region)
    if rc != 0:
        result["detail"] = (err or "could not resolve credentials").strip()[:300]
        result["note"] = aws.env_credentials_hint() or (
            "Profile did not resolve — run `aws configure sso --profile <name>` "
            "(or `aws configure --profile <name>`) and retry."
        )
        return result
    try:
        ident = json.loads(out or "{}")
        result["account"] = ident.get("Account", "")
        result["arn"] = ident.get("Arn", "")
    except json.JSONDecodeError:
        pass
    result["reachable"] = True

    ec2_rc, _o, _e = aws.run_aws(
        ["ec2", "describe-vpcs", "--max-results", "5", "--output", "json"], profile, region
    )
    result["ec2_reachable"] = ec2_rc == 0
    cf_rc, _o2, _e2 = aws.run_aws(
        ["cloudformation", "list-stacks", "--output", "json"], profile, region
    )
    result["cloudformation_reachable"] = cf_rc == 0
    ssm_rc, _o3, _e3 = aws.run_aws(
        ["ssm", "describe-instance-information", "--max-results", "5", "--output", "json"],
        profile,
        region,
    )
    result["ssm_reachable"] = ssm_rc == 0

    result["note"] = (
        "Access reachable (not fully verified — create/write and PassRole perms "
        "can't be checked without writing). The first launch is the real test; on "
        "AccessDenied the exact missing action is reported."
    )
    return result

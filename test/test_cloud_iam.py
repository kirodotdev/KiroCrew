"""Unit tests for the cloud IAM policy generator + reachability (cloud/iam.py)."""

from __future__ import annotations

import json

from kiro_crew.cloud import aws, iam


class TestPolicyDocument:
    def test_is_valid_policy_shape(self):
        doc = iam.policy_document()
        assert doc["Version"] == "2012-10-17"
        assert isinstance(doc["Statement"], list)
        for st in doc["Statement"]:
            assert st["Effect"] == "Allow"
            assert "Action" in st and "Resource" in st and "Sid" in st

    def test_covers_core_launch_actions(self):
        actions = {a for st in iam.policy_document()["Statement"] for a in st["Action"]}
        for needed in (
            "cloudformation:CreateStack",
            "cloudformation:DeleteStack",
            "ec2:RunInstances",
            "iam:PassRole",
            "iam:CreateRole",
            "ssm:StartSession",
            "ssm:GetParameter",
            "sts:GetCallerIdentity",
            "ec2:DescribeInstanceTypeOfferings",
            # discover_network verifies subnet egress via route tables
            "ec2:DescribeRouteTables",
            "s3:CreateBucket",
            "s3:PutObject",
            # `aws cloudformation deploy` always goes through a change set.
            "cloudformation:CreateChangeSet",
            "cloudformation:ExecuteChangeSet",
            "cloudformation:DescribeChangeSet",
            "cloudformation:DeleteChangeSet",
            # `kirocrew cloud list` discovers instances via the tagging API.
            "tag:GetResources",
        ):
            assert needed in actions, f"missing {needed}"

    def test_passrole_scoped_to_role_prefix_and_ec2(self):
        st = next(s for s in iam.policy_document()["Statement"] if s["Sid"] == "IamPassRoleToEc2")
        assert iam.ROLE_NAME_PREFIX in st["Resource"]
        cond = st["Condition"]["StringEquals"]
        assert cond["iam:PassedToService"] == "ec2.amazonaws.com"
        # Tag-gated so a pre-existing (unbounded) same-named role can't be passed:
        # only a role WE created (tagged at CreateRole) matches.
        assert cond[f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"

    def test_put_role_policy_is_tag_scoped(self):
        # PutRolePolicy must be gated on aws:ResourceTag/kirocrew:managed=true (in
        # addition to the role-ARN prefix) so a leaked launcher credential can't
        # inline a policy onto a PRE-EXISTING, out-of-band, unbounded
        # kirocrew-ec2-* role. The role is tagged atomically at CreateRole, so the
        # legitimate CFN deploy still satisfies it.
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "IamPutRolePolicyForInstance"
        )
        assert st["Action"] == ["iam:PutRolePolicy"]
        assert iam.ROLE_NAME_PREFIX in st["Resource"]
        assert st["Condition"]["StringEquals"][f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        # No dead iam:PermissionsBoundary condition (that key isn't in
        # PutRolePolicy's request context — it would deny the call).
        assert "iam:PermissionsBoundary" not in str(st["Condition"])

    def test_put_role_policy_and_passrole_not_tag_scoped_regression(self):
        # Guard: both PutRolePolicy and PassRole on a kirocrew-ec2-* role ARN must
        # carry the managed-tag condition — a regression that drops it re-opens
        # the pre-existing-unbounded-role escalation.
        for sid in ("IamPutRolePolicyForInstance", "IamPassRoleToEc2"):
            st = next(s for s in iam.policy_document()["Statement"] if s["Sid"] == sid)
            se = st.get("Condition", {}).get("StringEquals", {})
            assert (
                se.get(f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}") == "true"
            ), f"{sid} lost its aws:ResourceTag/kirocrew:managed gate"

    def test_tag_role_gated_on_existing_managed_tag(self):
        # iam:TagRole must be a SEPARATE statement gated on aws:ResourceTag/
        # kirocrew:managed=true — NOT unconditioned, and NOT in IamRoleForInstance.
        # If it were unconditioned, a leaked launcher credential could tag a
        # pre-existing UNBOUNDED kirocrew-ec2-* role kirocrew:managed=true and
        # thereby satisfy the PutRolePolicy/PassRole tag gate, defeating it. The
        # aws:ResourceTag gate means the launcher can only tag a role that is
        # ALREADY managed — which, at CreateRole, AWS evaluates against the tags
        # being applied (so the boundary-gated create still works), but a standalone
        # re-tag of an unmanaged role is denied. (Both validated live with a
        # least-privilege assumed-role principal.)
        st = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "IamTagRoleOnManaged"
        )
        assert st["Action"] == ["iam:TagRole"]
        assert iam.ROLE_NAME_PREFIX in st["Resource"]
        assert st["Condition"]["StringEquals"][f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        # It must NOT use iam:PermissionsBoundary — validated live that AWS does
        # NOT propagate that key into the CreateRole-embedded TagRole check, so a
        # boundary condition would DENY the legitimate least-priv deploy.
        assert "iam:PermissionsBoundary" not in str(st["Condition"])

    def test_tag_role_not_unconditioned_anywhere(self):
        # Guard: iam:TagRole must NOT appear in any statement WITHOUT the
        # aws:ResourceTag/kirocrew:managed=true gate — an unconditioned TagRole
        # (e.g. re-added to IamRoleForInstance) re-opens the tag-spoofing hole.
        for st in iam.policy_document()["Statement"]:
            if "iam:TagRole" in st.get("Action", []):
                se = st.get("Condition", {}).get("StringEquals", {})
                assert se.get(f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}") == "true", (
                    f"{st['Sid']} grants iam:TagRole without the "
                    "aws:ResourceTag/kirocrew:managed=true gate"
                )
        # And specifically not in the plain role-management statement.
        base = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "IamRoleForInstance"
        )
        assert "iam:TagRole" not in base["Action"]

    def test_cloudformation_mutation_scoped_to_kirocrew_stacks(self):
        st = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "CloudFormationStackMutate"
        )
        assert "cloudformation:DeleteStack" in st["Action"]
        # scoped to kirocrew-* stacks, not "*"
        assert st["Resource"] == f"arn:aws:cloudformation:*:*:stack/{iam.STACK_PREFIX}*/*"
        # read-only enumerate/validate stays account-wide (can't be stack-scoped)
        read = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "CloudFormationRead"
        )
        assert "cloudformation:ListStacks" in read["Action"]

    def test_changeset_actions_scoped_to_changeset_and_stack_arns(self):
        # `aws cloudformation deploy` authorizes change-set verbs on the
        # changeSet ARN (not just the stack ARN) — scoping to stack/* only would
        # deny the launch under the generated policy. Both ARN forms must be
        # present, still kirocrew-*-scoped.
        st = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "CloudFormationChangeSet"
        )
        for verb in (
            "cloudformation:CreateChangeSet",
            "cloudformation:ExecuteChangeSet",
            "cloudformation:DescribeChangeSet",
            "cloudformation:DeleteChangeSet",
        ):
            assert verb in st["Action"], f"missing {verb}"
        res = st["Resource"]
        assert any("changeSet/" in r for r in res), "changeSet ARN missing"
        assert any(":stack/" in r for r in res), "stack ARN missing"
        assert all(iam.STACK_PREFIX in r for r in res)  # all kirocrew-*-scoped

    def test_stack_prefix_matches_ec2(self):
        from kiro_crew.cloud import ec2

        assert iam.STACK_PREFIX == ec2.STACK_PREFIX

    def test_create_role_requires_boundary_with_arnlike(self):
        # iam:CreateRole is the boundary enforcement point: a created
        # kirocrew-ec2-* role MUST carry our permissions boundary. The condition
        # MUST be ArnLike (not StringEquals) — the value is a wildcard ARN pattern
        # (account id is `*`) and StringEquals would literal-match and DENY
        # CreateRole for anyone using the generated policy. The boundary name is
        # now EXACT (no per-tag suffix) — the shared immutable boundary.
        st = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "IamCreateRoleWithBoundary"
        )
        assert st["Action"] == ["iam:CreateRole"]
        assert "StringEquals" not in st["Condition"], "must be ArnLike, not StringEquals"
        cond = st["Condition"]["ArnLike"]["iam:PermissionsBoundary"]
        # The exact fixed boundary name, no trailing wildcard on the policy name.
        assert cond == f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}"
        assert cond.startswith("arn:aws:iam::")

    def test_put_role_policy_scoped_no_dead_boundary_condition(self):
        # PutRolePolicy is a SEPARATE statement scoped to the role ARN prefix. It
        # carries the aws:ResourceTag/kirocrew:managed gate (see
        # test_put_role_policy_is_tag_scoped) but must NOT carry an
        # iam:PermissionsBoundary condition — that key isn't in PutRolePolicy's
        # request context, so it would never match and DENY the call. The boundary
        # escalation is already closed at CreateRole time (the boundary can't be
        # removed by PutRolePolicy).
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "IamPutRolePolicyForInstance"
        )
        assert st["Action"] == ["iam:PutRolePolicy"]
        assert iam.ROLE_NAME_PREFIX in st["Resource"]
        assert "iam:PermissionsBoundary" not in str(st.get("Condition", {}))  # no dead key
        # CreateRole/PutRolePolicy must not appear in the plain role statement.
        base = next(
            s for s in iam.policy_document()["Statement"] if s["Sid"] == "IamRoleForInstance"
        )
        assert "iam:PutRolePolicy" not in base["Action"]
        assert "iam:CreateRole" not in base["Action"]

    def test_create_role_boundary_uses_arnlike_everywhere(self):
        # Guard: any statement conditioning on iam:PermissionsBoundary with a
        # wildcard ARN must use ArnLike/StringLike, never StringEquals (which
        # would silently deny the gated action).
        for st in iam.policy_document()["Statement"]:
            se = st.get("Condition", {}).get("StringEquals", {})
            assert (
                "iam:PermissionsBoundary" not in se
            ), f"{st['Sid']} uses StringEquals on iam:PermissionsBoundary (wildcard won't match)"

    def test_boundary_create_once_is_immutable(self):
        # The launcher CODE creates the shared boundary once (not per-launch CFN).
        # The generated policy must grant ONLY CreatePolicy + GetPolicy on the
        # EXACT boundary ARN — and NEVER the version/delete verbs, because those
        # would let a leaked launcher credential mutate/replace an existing
        # boundary's content (the whole vulnerability). CreatePolicy on a fixed
        # name fails EntityAlreadyExists once it exists, so it can't be made
        # permissive after the fact.
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "IamInstanceBoundaryCreateOnce"
        )
        # CreatePolicy + the two READ verbs the content-verification needs
        # (GetPolicy for the default version id, GetPolicyVersion for the doc).
        # NO version/delete/set-default verbs.
        assert set(st["Action"]) == {
            "iam:CreatePolicy",
            "iam:GetPolicy",
            "iam:GetPolicyVersion",
        }
        assert st["Resource"] == f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}"
        # No trailing wildcard on the policy name (would let CreatePolicy target
        # other, e.g. permissive, boundary-prefixed names).
        assert not st["Resource"].endswith("*")

    def test_no_boundary_mutation_verbs_anywhere(self):
        # Guard: the mutating boundary verbs must not reappear ANYWHERE in the
        # policy — re-adding CreatePolicyVersion/DeletePolicyVersion/DeletePolicy/
        # SetDefaultPolicyVersion is exactly the escalation this fix closes.
        actions = {a for st in iam.policy_document()["Statement"] for a in st["Action"]}
        for forbidden in (
            "iam:CreatePolicyVersion",
            "iam:DeletePolicyVersion",
            "iam:DeletePolicy",
            "iam:SetDefaultPolicyVersion",
        ):
            assert forbidden not in actions, f"{forbidden} re-enables boundary mutation"

    def test_boundary_name_is_fixed_no_per_tag_suffix(self):
        # The boundary is a single shared account-level policy with a FIXED name —
        # no per-StackTag suffix — so its content is identical for every launch
        # and it can be created once and reused immutably.
        assert iam.BOUNDARY_NAME == "kirocrew-ec2-boundary"
        assert not iam.BOUNDARY_NAME.endswith("-")  # not a prefix awaiting a suffix
        assert iam.boundary_arn("123456789012") == (
            "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary"
        )

    def test_boundary_document_shape(self):
        # The content-fixed boundary = exact SSM-core action set + s3:GetObject on
        # the account launcher-bucket prefix (region-agnostic; a boundary only
        # caps, so the whole-prefix read is safe — the role's inline policy pins
        # the actual object).
        doc = iam.boundary_policy_document("123456789012")
        assert doc["Version"] == "2012-10-17"
        sids = {s["Sid"] for s in doc["Statement"]}
        assert sids == {"SsmCore", "SourceBucketRead"}
        ssm_core = next(s for s in doc["Statement"] if s["Sid"] == "SsmCore")
        # A representative sample of the SSM-core action set the SSM agent needs.
        for act in (
            "ssm:UpdateInstanceInformation",
            "ssmmessages:OpenDataChannel",
            "ec2messages:GetMessages",
        ):
            assert act in ssm_core["Action"]
        s3 = next(s for s in doc["Statement"] if s["Sid"] == "SourceBucketRead")
        assert s3["Action"] == ["s3:GetObject"]
        assert s3["Resource"] == "arn:aws:s3:::kirocrew-src-123456789012-*/*"
        # roundtrips as JSON
        assert json.loads(iam.boundary_policy_json("123456789012")) == doc

    def test_authorize_security_group_is_tag_gated(self):
        # SG rule mutation must be gated to kirocrew:managed=true SGs so a leaked
        # credential can't open ingress on unrelated security groups.
        st = self._stmt("Ec2ManagedResourceMutateTagged")
        assert {
            "ec2:AuthorizeSecurityGroupEgress",
            "ec2:AuthorizeSecurityGroupIngress",
        } <= set(st["Action"])
        assert st["Condition"]["StringEquals"][f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        # ...and they're not in any of the provision (create) statements.
        prov_sids = {
            "Ec2CreateTaggedResources",
            "Ec2RunInstancesSupportingResources",
            "Ec2CreateSecurityGroupVpc",
        }
        prov_actions = {
            a
            for s in iam.policy_document()["Statement"]
            if s["Sid"] in prov_sids
            for a in s["Action"]
        }
        assert "ec2:AuthorizeSecurityGroupIngress" not in prov_actions

    def test_create_verbs_request_tag_gated_on_created_resources(self):
        # Every EC2 resource this policy can CREATE and that a ResourceTag-gated
        # verb later acts on must be creatable ONLY with the managed request tag —
        # otherwise a leaked credential could make an UNtagged twin that escapes
        # Stop/Terminate/Delete/Cancel. The supporting ARNs (volume/ENI + the
        # referenced subnet/SG/AMI/launch-template) stay UNgated: aws:RequestTag is
        # evaluated per-resource, so demanding it there 403s the launch (proven
        # live with run-instances --dry-run).
        tagged = self._stmt("Ec2CreateTaggedResources")
        assert set(tagged["Action"]) == {
            "ec2:RunInstances",
            "ec2:CreateSecurityGroup",
            "ec2:CreateLaunchTemplate",
        }
        assert set(tagged["Resource"]) == {
            "arn:aws:ec2:*:*:instance/*",
            "arn:aws:ec2:*:*:security-group/*",
            # --spot: CFN creates the launch template carrying the market options.
            "arn:aws:ec2:*:*:launch-template/*",
        }
        assert (
            tagged["Condition"]["StringEquals"][f"aws:RequestTag/{iam.MANAGED_TAG_KEY}"] == "true"
        )
        support = self._stmt("Ec2RunInstancesSupportingResources")
        assert support["Action"] == ["ec2:RunInstances"]
        assert "Condition" not in support  # sub-resources/references can't be request-tagged
        # the supporting statement must NOT include the request-tag-gated creation
        # ARNs (that would nullify the gate above)
        assert not any(r.endswith(":instance/*") for r in support["Resource"])
        for needed in ("volume/*", "network-interface/*", "launch-template/*"):
            assert any(r.endswith(needed) for r in support["Resource"]), f"missing {needed}"
        vpc = self._stmt("Ec2CreateSecurityGroupVpc")
        assert vpc["Action"] == ["ec2:CreateSecurityGroup"]
        assert vpc["Resource"] == "arn:aws:ec2:*:*:vpc/*"
        assert "Condition" not in vpc

    def test_spot_request_arn_is_unconditioned_on_run_instances(self):
        # AWS's own IAM example doc (ExamplePolicies_EC2.html
        # #iam-example-spot-instances) states that an aws:RequestTag condition on
        # the spot-instances-request resource for RunInstances is NOT SUPPORTED,
        # and the gate would be theatre anyway: an UNtagged request carries no
        # TagSpecification, so IAM never evaluates the ARN and the condition never
        # fires. Adding it back would only 403 our own (correctly tagged) launch.
        # So the ARN lives in the UNCONDITIONED supporting statement.
        support = self._stmt("Ec2RunInstancesSupportingResources")
        assert "arn:aws:ec2:*:*:spot-instances-request/*" in support["Resource"]
        assert "Condition" not in support
        for st in iam.policy_document()["Statement"]:
            res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            if any(r.endswith(":spot-instances-request/*") for r in res):
                cond = st.get("Condition", {}).get("StringEquals", {})
                assert f"aws:RequestTag/{iam.MANAGED_TAG_KEY}" not in cond, st["Sid"]

    def test_no_untagged_creation_of_tag_gated_resources(self):
        # Guard: no statement may grant a creation verb on the resource type it
        # creates WITHOUT the managed request-tag condition — that would re-open
        # untagged-resource creation and with it an escape from the ResourceTag
        # gate. (RunInstances on security-group/launch-template is a REFERENCE,
        # not a creation, so those pairs are deliberately absent here.)
        pairs = [
            ("ec2:RunInstances", ":instance/*"),
            ("ec2:CreateSecurityGroup", ":security-group/*"),
            ("ec2:CreateLaunchTemplate", ":launch-template/*"),
        ]
        for st in iam.policy_document()["Statement"]:
            acts = set(st.get("Action", []))
            res_list = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            cond_tag = (
                st.get("Condition", {})
                .get("StringEquals", {})
                .get(f"aws:RequestTag/{iam.MANAGED_TAG_KEY}")
            )
            for action, suffix in pairs:
                if action in acts and any(r.endswith(suffix) for r in res_list):
                    assert cond_tag == "true", f"{st['Sid']}: {action} on {suffix} untagged"

    def test_lifecycle_is_tag_scoped(self):
        st = self._stmt("Ec2ManagedResourceMutateTagged")
        cond = st["Condition"]["StringEquals"]
        assert cond[f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        assert "ec2:TerminateInstances" in st["Action"]

    def test_spot_actions_present_and_correctly_gated(self):
        # --spot end-to-end grants, pinned so a regression can't silently drop the
        # launch (CreateLaunchTemplate / RunInstances-on-the-request) or the
        # teardown (CancelSpotInstanceRequests), the latter being what stops EC2
        # from launching a REPLACEMENT instance when destroy terminates this one.
        doc = iam.policy_document()
        actions = {a for st in doc["Statement"] for a in st["Action"]}
        for needed in (
            "ec2:CreateLaunchTemplate",
            "ec2:DeleteLaunchTemplate",
            "ec2:DescribeLaunchTemplates",
            "ec2:DescribeLaunchTemplateVersions",
            "ec2:DescribeSpotInstanceRequests",
            "ec2:CancelSpotInstanceRequests",
            "iam:CreateServiceLinkedRole",
        ):
            assert needed in actions, f"missing {needed}"
        # Cancel is destructive and must be ResourceTag-gated, never on its own
        # ungated statement.
        for st in doc["Statement"]:
            if "ec2:CancelSpotInstanceRequests" in st.get("Action", []):
                cond = st.get("Condition", {}).get("StringEquals", {})
                assert cond.get(f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}") == "true", st["Sid"]
        # CreateLaunchTemplate must be tag-on-create-able (CFN passes the LT's
        # tags inline, which AWS authorizes as ec2:CreateTags).
        tag_on_create = self._stmt("Ec2TagOnCreate")
        assert "CreateLaunchTemplate" in tag_on_create["Condition"]["StringEquals"]["ec2:CreateAction"]

    def test_service_linked_role_pinned_to_spot_service(self):
        # The first --spot launch in an account needs AWSServiceRoleForEC2Spot,
        # which the console creates silently but the CLI/API does not. This is the
        # only unconditioned-looking IAM write in the policy, so it must be pinned
        # BOTH by resource path and by iam:AWSServiceName — otherwise it becomes a
        # create-any-service-linked-role grant.
        st = self._stmt("IamCreateSpotServiceLinkedRole")
        assert st["Action"] == ["iam:CreateServiceLinkedRole"]
        assert st["Resource"] == (
            "arn:aws:iam::*:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot*"
        )
        assert st["Condition"]["StringEquals"]["iam:AWSServiceName"] == "spot.amazonaws.com"

    def test_policy_fits_iam_managed_policy_limit(self):
        # A customer managed policy is capped at 6,144 characters (whitespace not
        # counted) and CANNOT be raised — over that, `aws iam create-policy` fails
        # with LimitExceeded and the printed policy is unusable for every operator
        # who isn't already an admin. The policy sits close to the cap, so this is
        # a real gate, not a formality: adding statements requires either merging
        # equivalent ones (see Ec2CreateTaggedResources /
        # Ec2ManagedResourceMutateTagged) or splitting the printed policy in two.
        compact = json.dumps(iam.policy_document(), separators=(",", ":"))
        assert len(compact) <= 6144, (
            f"launcher policy is {len(compact)} chars — over IAM's 6,144 managed-policy "
            "limit; merge equivalent statements or split the policy"
        )

    def _stmt(self, sid):
        return next(s for s in iam.policy_document()["Statement"] if s["Sid"] == sid)

    def test_ssm_session_and_sendcommand_gated_to_managed_instances(self):
        # The RCE-adjacent verbs must be tag-scoped to KiroCrew instances so a
        # leaked launcher credential can't run commands account-wide.
        st = self._stmt("SsmSessionOnManagedInstances")
        assert set(st["Action"]) == {"ssm:StartSession", "ssm:SendCommand"}
        assert st["Resource"] == "arn:aws:ec2:*:*:instance/*"
        assert st["Condition"]["StringEquals"][f"ssm:resourceTag/{iam.MANAGED_TAG_KEY}"] == "true"

    def test_no_unconditioned_sendcommand_on_all_instances(self):
        # Guard against a regression that re-adds account-wide SendCommand/
        # StartSession on instance resources without the tag condition.
        for st in iam.policy_document()["Statement"]:
            acts = set(st.get("Action", []))
            if acts & {"ssm:SendCommand", "ssm:StartSession"}:
                res = st["Resource"]
                res_list = res if isinstance(res, list) else [res]
                targets_instances = any("instance/" in r for r in res_list)
                if targets_instances:
                    assert "Condition" in st, f"{st['Sid']} grants session/command on instances "
                    "without a tag condition"

    def test_destructive_ec2_verbs_tag_scoped(self):
        st = self._stmt("Ec2ManagedResourceMutateTagged")
        cond = st["Condition"]["StringEquals"]
        assert cond[f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        for verb in (
            "ec2:DeleteSecurityGroup",
            "ec2:RevokeSecurityGroupIngress",
            "ec2:DeleteTags",
            # --spot teardown: CFN removes the launch template, and destroy
            # cancels the persistent request before delete-stack.
            "ec2:DeleteLaunchTemplate",
            "ec2:CancelSpotInstanceRequests",
        ):
            assert verb in st["Action"]
        # creation verbs live in the provision statements; destructive verbs don't.
        run_st = self._stmt("Ec2CreateTaggedResources")
        assert "ec2:RunInstances" in run_st["Action"]
        prov_actions = {
            a
            for sid in (
                "Ec2CreateTaggedResources",
                "Ec2RunInstancesSupportingResources",
                "Ec2CreateSecurityGroupVpc",
            )
            for a in self._stmt(sid)["Action"]
        }
        assert "ec2:DeleteSecurityGroup" not in prov_actions
        assert "ec2:DeleteLaunchTemplate" not in prov_actions

    def test_create_tags_only_on_create(self):
        # ec2:CreateTags must be gated by ec2:CreateAction so a leaked credential
        # can't tag arbitrary existing resources as kirocrew:managed=true and
        # bring them under the tag-gated Stop/Terminate/Delete statements.
        st = self._stmt("Ec2TagOnCreate")
        assert st["Action"] == ["ec2:CreateTags"]
        actions = st["Condition"]["StringEquals"]["ec2:CreateAction"]
        # CreateLaunchTemplate joins the allowlist for --spot: CFN passes the
        # launch template's tags inline, which AWS authorizes as ec2:CreateTags.
        assert set(actions) == {"RunInstances", "CreateSecurityGroup", "CreateLaunchTemplate"}
        # and it's not in the provision (create) statements
        prov_actions = {
            a
            for sid in (
                "Ec2CreateTaggedResources",
                "Ec2RunInstancesSupportingResources",
                "Ec2CreateSecurityGroupVpc",
            )
            for a in self._stmt(sid)["Action"]
        }
        assert "ec2:CreateTags" not in prov_actions

    def test_attach_role_policy_pinned_to_ssm_core(self):
        # AttachRolePolicy must be constrained by iam:PolicyARN to exactly the
        # SSM-core managed policy, else a holder could attach AdministratorAccess
        # to a kirocrew-ec2-* role and pass it to EC2 (full escalation).
        st = self._stmt("IamAttachManagedPolicyForInstance")
        assert set(st["Action"]) == {"iam:AttachRolePolicy", "iam:DetachRolePolicy"}
        pinned = st["Condition"]["ArnEquals"]["iam:PolicyARN"]
        assert pinned == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        assert iam.ROLE_NAME_PREFIX in st["Resource"]
        # unconstrained Attach/Detach must NOT remain in the broad role statement
        role_st = self._stmt("IamRoleForInstance")
        assert "iam:AttachRolePolicy" not in role_st["Action"]
        assert "iam:DetachRolePolicy" not in role_st["Action"]

    def test_no_unconstrained_attach_role_policy(self):
        # Guard against a regression that re-adds AttachRolePolicy without an
        # iam:PolicyARN condition anywhere in the policy.
        for st in iam.policy_document()["Statement"]:
            if "iam:AttachRolePolicy" in st.get("Action", []):
                cond = st.get("Condition", {})
                assert (
                    "ArnEquals" in cond and "iam:PolicyARN" in cond["ArnEquals"]
                ), f"{st['Sid']} grants AttachRolePolicy without an iam:PolicyARN cap"

    def test_command_history_read_is_minimal(self):
        # The launcher polls send-command results via GetCommandInvocation, but
        # must NOT grant ListCommandInvocations — narrowing the command-history
        # read surface limits blind enumeration of the dashboard token that
        # mint_token transits through send-command output.
        actions = {a for st in iam.policy_document()["Statement"] for a in st["Action"]}
        assert "ssm:GetCommandInvocation" in actions
        assert "ssm:ListCommandInvocations" not in actions

    def test_no_invalid_s3_headbucket_action(self):
        # s3:HeadBucket is not a real IAM action — its presence makes the printed
        # policy fail to create; the HeadBucket API is authorized by s3:ListBucket.
        actions = {a for st in iam.policy_document()["Statement"] for a in st["Action"]}
        assert "s3:HeadBucket" not in actions
        assert "s3:ListBucket" in actions

    def test_policy_json_roundtrips(self):
        assert json.loads(iam.policy_json()) == iam.policy_document()


class TestReachabilityCheck:
    def test_profile_unresolved(self, monkeypatch):
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: (255, "", "Unable to locate credentials")
        )
        r = iam.reachability_check("bogus")
        assert r["reachable"] is False
        assert "did not resolve" in r["note"]
        assert "Unable to locate credentials" in r["detail"]

    def test_all_reachable(self, monkeypatch):
        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            if args[0] == "sts":
                return (
                    0,
                    json.dumps({"Account": "814959995281", "Arn": "arn:aws:iam::x:user/a"}),
                    "",
                )
            return (0, "{}", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        r = iam.reachability_check("dev", "us-east-1")
        assert r["reachable"] is True
        assert r["account"] == "814959995281"
        assert r["ec2_reachable"] and r["cloudformation_reachable"] and r["ssm_reachable"]

    def test_partial_reachability(self, monkeypatch):
        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            if args[0] == "sts":
                return (0, json.dumps({"Account": "123"}), "")
            if args[0] == "ec2":
                return (0, "{}", "")
            # cloudformation + ssm denied
            return (255, "", "AccessDenied")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        r = iam.reachability_check("dev")
        assert r["reachable"] is True
        assert r["ec2_reachable"] is True
        assert r["cloudformation_reachable"] is False
        assert r["ssm_reachable"] is False


class TestAgentDenyListForCloudVerbs:
    """The cloud teardown/provision verbs are human-only; the agent must be
    blocked from the destructive AWS CLI strings. That block is enforced at
    KiroCrew's own PreToolUse gate (``security.is_denied`` via the ported
    ``BUILTIN_DENIED_RULES``), NOT by injecting ``deniedCommands`` into the
    kiro agent config (that path is retired) — guard it so the guarantee can't
    silently regress."""

    @staticmethod
    def _denied(cmd: str) -> bool:
        from kiro_crew.security import is_denied

        return is_denied(cmd) is not None

    def test_destructive_aws_cli_verbs_denied(self):
        # Assert BEHAVIOR at the enforcement point: each destructive verb must be
        # blocked by is_denied. This is the real guarantee and survives
        # pattern-syntax changes.
        must_deny = [
            "aws ec2 terminate-instances --instance-ids i-1",
            "aws ec2 delete-security-group --group-id sg-1",
            "aws cloudformation delete-stack --stack-name x",
            "aws ssm send-command --instance-ids i --document-name d",
            "aws ssm start-session --target i-1",
            "aws ssm get-command-invocation --command-id c --instance-id i",
            "aws ssm list-command-invocations",
        ]
        for cmd in must_deny:
            assert self._denied(cmd), f"is_denied does not deny {cmd!r}"

    def test_global_args_do_not_bypass_deny(self):
        # Regression: the deny patterns must tolerate AWS global options in BOTH
        # positions — before the service (`aws --region r ec2 terminate-...`) AND
        # between the service and the operation (`aws ec2 --region r
        # terminate-...`), otherwise an agent trivially bypasses the denylist.
        # Also confirm read-only calls are still ALLOWED (no over-broad match).
        bypass_attempts = [
            # options before the service
            "aws --profile dev --region us-east-1 ec2 terminate-instances --instance-ids i-1",
            "aws --region us-east-1 cloudformation delete-stack --stack-name x",
            "aws --profile dev ssm send-command --instance-ids i --document-name d",
            "aws --output json --profile p ssm start-session --target i-1",
            "aws --profile p s3 rm s3://bucket/key",
            # options BETWEEN service and operation (the newer bypass class)
            "aws ec2 --region us-east-1 terminate-instances --instance-ids i-1",
            "aws cloudformation --region x delete-stack --stack-name y",
            "aws ssm --region x send-command --instance-ids i --document-name d",
            "aws s3 --profile p rm s3://bucket/key",
            "aws iam --region x put-role-policy --role-name r --policy-name p",
            # options in BOTH positions
            "aws --profile p ec2 --region r terminate-instances --instance-ids i-1",
        ]
        still_allowed = [
            "aws ec2 describe-instances",
            "aws --profile dev cloudformation describe-stacks",
            "aws ec2 --region x describe-instances",
            "aws cloudformation --region x describe-stacks",
            "aws ssm describe-instance-information",
            "aws s3 ls s3://bucket",
            "aws s3 --profile p ls s3://bucket",
            "aws iam get-role --role-name r",
        ]
        for cmd in bypass_attempts:
            assert self._denied(cmd), f"global-args bypass not denied: {cmd!r}"
        for cmd in still_allowed:
            assert not self._denied(cmd), f"read-only call wrongly denied: {cmd!r}"

    def test_launcher_creation_verbs_denied(self):
        # The cloud launcher's CREATE/mutation verbs are human/installer-only —
        # an agent shell must not be able to provision resources (bypassing the
        # run_aws chokepoint + the not-an-MCP-tool boundary). Deny the full
        # provision path, not just the destructive verbs. READ/discovery stays
        # allowed.
        must_deny = [
            "aws cloudformation deploy --template-file t --stack-name kirocrew-x",
            "aws --profile dev cloudformation create-stack --stack-name x",
            "aws cloudformation execute-change-set --change-set-name c",
            "aws ec2 run-instances --image-id ami-1",
            "aws --region us-east-1 ec2 create-security-group --group-name g",
            "aws ec2 authorize-security-group-ingress --group-id sg-1",
            "aws iam create-role --role-name kirocrew-ec2-x",
            "aws iam put-role-policy --role-name r --policy-name p --policy-document {}",
            "aws iam attach-role-policy --role-name r --policy-arn a",
            "aws --profile p iam create-instance-profile --instance-profile-name p",
            "aws iam create-policy --policy-name kirocrew-ec2-boundary --policy-document {}",
            "aws iam create-policy-version --policy-arn a --policy-document {}",
        ]
        still_allowed = [
            "aws cloudformation describe-stacks",
            "aws cloudformation list-stacks",
            "aws ec2 describe-instances",
            "aws iam get-role --role-name r",
            "aws iam list-roles",
            "aws iam get-policy --policy-arn a",
            "aws iam list-policies",
        ]
        for cmd in must_deny:
            assert self._denied(cmd), f"creation verb not denied: {cmd!r}"
        for cmd in still_allowed:
            assert not self._denied(cmd), f"read-only call wrongly denied: {cmd!r}"

    def test_s3api_write_verbs_denied(self):
        # The launcher IAM grants s3:PutObject to kirocrew-src-* buckets; if the
        # agent shell can reach the low-level `aws s3api put-object` (or the
        # multipart / copy / bucket-policy verbs), it has a data-exfiltration
        # path that the high-level `aws s3 cp` denies don't cover. Block the whole
        # s3api write surface; keep s3api READS allowed.
        must_deny = [
            "aws s3api put-object --bucket b --key k --body /etc/passwd",
            "aws --profile dev --region us-east-1 s3api put-object --bucket b --key k --body f",
            "aws s3api create-multipart-upload --bucket b --key k",
            "aws s3api upload-part --bucket b --key k --part-number 1 --body f",
            "aws s3api complete-multipart-upload --bucket b --key k --upload-id u",
            "aws s3api copy-object --bucket b --key k --copy-source s/x",
            "aws s3api put-bucket-policy --bucket b --policy p",
        ]
        still_allowed = [
            "aws s3api get-object --bucket b --key k out",
            "aws s3api list-objects-v2 --bucket b",
            "aws s3api head-bucket --bucket b",
        ]
        for cmd in must_deny:
            assert self._denied(cmd), f"s3api write not denied: {cmd!r}"
        for cmd in still_allowed:
            assert not self._denied(cmd), f"s3api read wrongly denied: {cmd!r}"

    def test_kirocrew_cloud_wrapper_denied(self):
        # `kirocrew cloud destroy` is a wrapper that internally runs
        # `aws cloudformation delete-stack`; the gate only sees the wrapper
        # string, so it must be blocked in its own right or the agent bypasses
        # the raw-CLI teardown block.
        for cmd in (
            "kirocrew cloud destroy --yes --tag kc-1",
            "kirocrew cloud stop",
            "kiro-crew cloud launch",
            "kirocrew cloud connect",  # mints/prints a dashboard token
            "kirocrew cloud tunnel",
            "kirocrew cloud login",
        ):
            assert self._denied(cmd), f"kirocrew cloud wrapper not denied: {cmd!r}"
        # read-only observation stays allowed
        for allowed in ("kirocrew cloud list", "kirocrew cloud status"):
            assert not self._denied(allowed), f"read-only wrongly denied: {allowed!r}"

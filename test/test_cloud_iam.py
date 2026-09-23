"""Unit tests for the cloud IAM policy generator + reachability (cloud/iam.py)."""

from __future__ import annotations

import json

from kiro_crew.cloud import aws, iam

# The interactive Session Manager documents AWS ships today. These are the concrete
# sample DenyStartSessionOutsideTheLane's inversion is measured against: none of them
# is exempt from that Deny, and no Allow grants any of them. The list lives in the
# test rather than in cloud/iam.py because no production code reads it.
KNOWN_INTERACTIVE_DOCUMENTS = (
    "SSM-SessionManagerRunShell",
    "AWS-StartInteractiveCommand",
    "AWS-StartSSHSession",
    "AWS-StartNonInteractiveCommand",
)


class TestPolicyDocument:
    def test_is_valid_policy_shape(self):
        doc = iam.policy_document()
        assert doc["Version"] == "2012-10-17"
        assert isinstance(doc["Statement"], list)
        for st in doc["Statement"]:
            assert st["Effect"] in {"Allow", "Deny"}
            assert "Action" in st and "Sid" in st
            # Exactly one of the two resource forms, never both (IAM rejects a
            # statement carrying both) and never neither. NotResource is spelled
            # here rather than assumed absent because DenyStartSessionOutsideTheLane
            # is inverted; which statements may invert is pinned separately by
            # test_no_allow_statement_inverts_its_resource_list.
            assert ("Resource" in st) ^ ("NotResource" in st), st["Sid"]

    def test_the_only_deny_statement_is_the_start_session_lane_block(self):
        """This policy is Allow-only except for one deliberate Deny.

        The shape check above accepts the two legal effects rather than asserting
        every statement is an Allow, because StartSession outside this lane's own
        resources is denied explicitly: an explicit Deny cannot be overridden by a
        later Allow, and that is the only construct keeping the container shell
        closed against an additive edit.

        The property that check would otherwise carry is stated here instead -- the
        Deny set is exactly one known Sid. A new Deny has to be argued for, and an
        Allow silently flipped to Deny (which would break the launcher rather than
        secure it) fails here. The Sid names the lane rather than a document set
        because the statement denies by exemption rather than by enumeration.
        """
        denies = {st["Sid"] for st in iam.policy_document()["Statement"] if st["Effect"] == "Deny"}
        assert denies == {"DenyStartSessionOutsideTheLane"}, denies

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
            # DNS preflight: detect a private hosted zone that shadows a host the
            # bootstrap downloads from (NXDOMAIN with no public fallthrough).
            "route53:ListHostedZonesByVPC",
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
        # legitimate CFN deploy still satisfies it. PutRolePolicy + TagRole are
        # MERGED into one statement (same Effect + role ARN + managed-tag
        # Condition), so the action list holds exactly those two.
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "IamPutRolePolicyAndTagRoleOnManaged"
        )
        assert set(st["Action"]) == {"iam:PutRolePolicy", "iam:TagRole"}
        assert iam.ROLE_NAME_PREFIX in st["Resource"]
        assert st["Condition"]["StringEquals"][f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        # No dead iam:PermissionsBoundary condition (that key isn't in
        # PutRolePolicy's request context — it would deny the call).
        assert "iam:PermissionsBoundary" not in str(st["Condition"])

    def test_put_role_policy_and_passrole_not_tag_scoped_regression(self):
        # Guard: both PutRolePolicy and PassRole on a kirocrew-ec2-* role ARN must
        # carry the managed-tag condition — a regression that drops it re-opens
        # the pre-existing-unbounded-role escalation. PutRolePolicy now lives in
        # the merged IamPutRolePolicyAndTagRoleOnManaged statement.
        for sid in ("IamPutRolePolicyAndTagRoleOnManaged", "IamPassRoleToEc2"):
            st = next(s for s in iam.policy_document()["Statement"] if s["Sid"] == sid)
            se = st.get("Condition", {}).get("StringEquals", {})
            assert (
                se.get(f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}") == "true"
            ), f"{sid} lost its aws:ResourceTag/kirocrew:managed gate"

    def test_tag_role_gated_on_existing_managed_tag(self):
        # iam:TagRole must be gated on aws:ResourceTag/kirocrew:managed=true —
        # NOT unconditioned, and NOT in IamRoleForInstance. If it were
        # unconditioned, a leaked launcher credential could tag a pre-existing
        # UNBOUNDED kirocrew-ec2-* role kirocrew:managed=true and thereby satisfy
        # the PutRolePolicy/PassRole tag gate, defeating it. The aws:ResourceTag
        # gate means the launcher can only tag a role that is ALREADY managed —
        # which, at CreateRole, AWS evaluates against the tags being applied (so
        # the boundary-gated create still works), but a standalone re-tag of an
        # unmanaged role is denied. (Both validated live with a least-privilege
        # assumed-role principal.) TagRole now shares the merged
        # IamPutRolePolicyAndTagRoleOnManaged statement with PutRolePolicy — same
        # Effect + role ARN + Condition, so the merge is permission-neutral.
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "IamPutRolePolicyAndTagRoleOnManaged"
        )
        assert set(st["Action"]) == {"iam:PutRolePolicy", "iam:TagRole"}
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
        # read-only enumerate/validate stays account-wide (can't be stack-scoped);
        # it now lives in the merged CloudFormationAndResourceDiscovery statement.
        read = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "CloudFormationAndResourceDiscovery"
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
            if s["Sid"] == "IamPutRolePolicyAndTagRoleOnManaged"
        )
        assert set(st["Action"]) == {"iam:PutRolePolicy", "iam:TagRole"}
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
        # The grant names two exact policies, so Resource is a list. The property
        # this test carries: the EC2 boundary is named EXACTLY, never by a prefix a
        # leaked credential could author into.
        resources = st["Resource"]
        assert isinstance(resources, list), resources
        assert f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}" in resources
        # No trailing wildcard on ANY name in the grant (would let CreatePolicy
        # target other, e.g. permissive, boundary-prefixed names).
        for resource in resources:
            assert not resource.endswith("*"), f"prefix wildcard in the grant: {resource}"

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
        # credential can't open ingress on unrelated security groups. It lives in
        # the merged Ec2ManagedResourceMutateTagged statement (same Effect +
        # Resource "*" + managed-tag Condition as the destructive/lifecycle verbs).
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "Ec2ManagedResourceMutateTagged"
        )
        # EXACT set-equality pin on the whole merged action list — the only
        # statement here with Resource "*" and a mutating verb set, so a later
        # change that appends a new mutating verb (e.g. ec2:ModifyInstanceAttribute)
        # to this "*"-scoped statement must land red here, not green. A subset
        # check would let action creep onto the wildcard resource unreviewed.
        assert set(st["Action"]) == {
            "ec2:AuthorizeSecurityGroupEgress",
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:RevokeSecurityGroupIngress",
            "ec2:DeleteSecurityGroup",
            "ec2:DeleteTags",
            "ec2:StopInstances",
            "ec2:StartInstances",
            "ec2:TerminateInstances",
            "ec2:RebootInstances",
        }
        assert st["Condition"]["StringEquals"][f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        # ...and they're not in any of the provision (create) statements.
        prov_sids = {
            "Ec2RunInstancesTaggedInstance",
            "Ec2RunInstancesSupportingResources",
            "Ec2CreateSecurityGroupTagged",
            "Ec2CreateSecurityGroupVpc",
        }
        prov_actions = {
            a
            for s in iam.policy_document()["Statement"]
            if s["Sid"] in prov_sids
            for a in s["Action"]
        }
        assert "ec2:AuthorizeSecurityGroupIngress" not in prov_actions

    def test_run_instances_request_tag_gated_on_instance_only(self):
        # ec2:RunInstances must require aws:RequestTag/kirocrew:managed=true, but
        # ONLY on the instance ARN — RunInstances authorizes per-resource across
        # the instance it creates AND the volume/ENI it creates + the referenced
        # image/subnet/security-group, none of which carry the request tag; a
        # blanket request-tag on the whole action 403s the launch (proven live
        # with run-instances --dry-run). So there are TWO statements: the
        # instance ARN gated, the supporting ARNs ungated.
        tagged = self._stmt("Ec2RunInstancesTaggedInstance")
        assert tagged["Action"] == ["ec2:RunInstances"]
        assert tagged["Resource"] == "arn:aws:ec2:*:*:instance/*"
        assert (
            tagged["Condition"]["StringEquals"][f"aws:RequestTag/{iam.MANAGED_TAG_KEY}"] == "true"
        )
        support = self._stmt("Ec2RunInstancesSupportingResources")
        assert support["Action"] == ["ec2:RunInstances"]
        assert "Condition" not in support  # sub-resources/references can't be request-tagged
        # the supporting statement must NOT include instance/* (that would bypass
        # the request-tag gate on the instance)
        assert not any(r.endswith(":instance/*") for r in support["Resource"])
        for needed in ("volume/*", "network-interface/*"):
            assert any(r.endswith(needed) for r in support["Resource"]), f"missing {needed}"

    def test_create_security_group_request_tag_gated(self):
        # ec2:CreateSecurityGroup must require the managed request-tag on the NEW
        # security-group ARN (so a leaked cred can't create an untagged SG that
        # escapes the tag-gated Authorize/Delete verbs); the referenced vpc/* is
        # ungated (pre-existing, not tagged by this call).
        tagged = self._stmt("Ec2CreateSecurityGroupTagged")
        assert tagged["Action"] == ["ec2:CreateSecurityGroup"]
        assert tagged["Resource"] == "arn:aws:ec2:*:*:security-group/*"
        assert (
            tagged["Condition"]["StringEquals"][f"aws:RequestTag/{iam.MANAGED_TAG_KEY}"] == "true"
        )
        vpc = self._stmt("Ec2CreateSecurityGroupVpc")
        assert vpc["Action"] == ["ec2:CreateSecurityGroup"]
        assert vpc["Resource"] == "arn:aws:ec2:*:*:vpc/*"
        assert "Condition" not in vpc

    def test_no_untagged_run_instances_or_create_sg_on_instance_or_sg(self):
        # Guard: no statement may grant ec2:RunInstances on instance/* OR
        # ec2:CreateSecurityGroup on security-group/* WITHOUT the managed
        # request-tag condition — that would re-open untagged-resource creation.
        for st in iam.policy_document()["Statement"]:
            # Allow only: this guard is about what the policy GRANTS, and the one Deny
            # carries NotResource rather than Resource.
            if st["Effect"] != "Allow":
                continue
            acts = set(st.get("Action", []))
            res_list = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            cond_tag = (
                st.get("Condition", {})
                .get("StringEquals", {})
                .get(f"aws:RequestTag/{iam.MANAGED_TAG_KEY}")
            )
            if "ec2:RunInstances" in acts and any(r.endswith(":instance/*") for r in res_list):
                assert cond_tag == "true", f"{st['Sid']} runs instances on instance/* untagged"
            if "ec2:CreateSecurityGroup" in acts and any(
                r.endswith(":security-group/*") for r in res_list
            ):
                assert cond_tag == "true", f"{st['Sid']} creates SG on security-group/* untagged"

    def test_lifecycle_is_tag_scoped(self):
        st = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "Ec2ManagedResourceMutateTagged"
        )
        cond = st["Condition"]["StringEquals"]
        assert cond[f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}"] == "true"
        assert "ec2:TerminateInstances" in st["Action"]

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
            # Allow only, for the same reason as the untagged-creation guard above: a
            # Deny grants nothing, and the one Deny here carries NotResource.
            if st["Effect"] != "Allow":
                continue
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
        for verb in ("ec2:DeleteSecurityGroup", "ec2:RevokeSecurityGroupIngress", "ec2:DeleteTags"):
            assert verb in st["Action"]
        # creation verbs live in the provision statements; destructive verbs don't.
        run_st = self._stmt("Ec2RunInstancesTaggedInstance")
        assert "ec2:RunInstances" in run_st["Action"]
        prov_actions = {
            a
            for sid in (
                "Ec2RunInstancesTaggedInstance",
                "Ec2RunInstancesSupportingResources",
                "Ec2CreateSecurityGroupTagged",
                "Ec2CreateSecurityGroupVpc",
            )
            for a in self._stmt(sid)["Action"]
        }
        assert "ec2:DeleteSecurityGroup" not in prov_actions

    def test_create_tags_only_on_create(self):
        # ec2:CreateTags must be gated by ec2:CreateAction so a leaked credential
        # can't tag arbitrary existing resources as kirocrew:managed=true and
        # bring them under the tag-gated Stop/Terminate/Delete statements.
        st = self._stmt("Ec2TagOnCreate")
        assert st["Action"] == ["ec2:CreateTags"]
        actions = st["Condition"]["StringEquals"]["ec2:CreateAction"]
        assert set(actions) == {"RunInstances", "CreateSecurityGroup"}
        # and it's not in the provision (create) statements
        prov_actions = {
            a
            for sid in (
                "Ec2RunInstancesTaggedInstance",
                "Ec2RunInstancesSupportingResources",
                "Ec2CreateSecurityGroupTagged",
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

    def test_policy_fits_iam_managed_policy_limit(self):
        # A customer managed policy is capped at 6,144 characters with WHITESPACE
        # NOT COUNTED (AWS IAM quota "Managed policy size", non-adjustable — see
        # docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html).
        # Over that, `aws iam create-policy` fails with LimitExceeded and the
        # printed policy is unusable for every operator who isn't already an admin.
        # The headroom target sits BELOW the hard cap so the next feature (EC2
        # Spot, ~+152 chars) still fits without another shrink; merge equivalent
        # statements (see Ec2ManagedResourceMutateTagged /
        # IamPutRolePolicyAndTagRoleOnManaged / CloudFormationAndResourceDiscovery)
        # rather than letting the policy creep back over.
        compact = json.dumps(iam.policy_document(), separators=(",", ":"))
        assert len(compact) <= 6144, (
            f"launcher policy is {len(compact)} chars — over IAM's 6,144 managed-policy "
            "limit; merge equivalent statements or split the policy"
        )
        # Headroom gate BELOW the hard cap so growth is caught well before it hits
        # the platform limit. Sized to sit above where the next feature (EC2 Spot,
        # ~+152 chars -> ~5,970) lands so that feature is not tripped by the very
        # reserve meant to admit it, while still leaving ~100 chars of margin under
        # the 6,144 cap. If a feature legitimately needs more, raise this in the
        # same change and say why — do not let the policy drift up to the cap.
        assert len(compact) <= 6044, (
            f"launcher policy is {len(compact)} chars — over the 6,044 headroom gate "
            "(100 chars under IAM's 6,144 cap). Merge equivalent statements or, if a "
            "feature genuinely needs the room, raise this gate deliberately."
        )

    def test_shrink_preserved_the_permission_set_byte_for_byte(self):
        # Merging statements that share Effect + Resource + Condition to stay under
        # the cap MUST NOT change the effective permission set. Flatten the
        # pre-merge fixture and the current policy into canonical (Effect, Action,
        # Resource-form, Resource, Condition) tuple sets and assert equality — this
        # is the guarantee that the merges are permission-neutral.
        import pathlib

        fixture = (
            pathlib.Path(__file__).resolve().parent
            / "fixtures"
            / "cloud_iam_policy_pre_shrink.json"
        )
        old = json.loads(fixture.read_text(encoding="utf-8"))
        new = iam.policy_document()

        def flatten(doc):
            tuples = set()
            for st in doc["Statement"]:
                effect = st["Effect"]
                actions = st.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "Resource" in st:
                    form = "Resource"
                    resources = st["Resource"]
                else:
                    form = "NotResource"
                    resources = st["NotResource"]
                if isinstance(resources, str):
                    resources = [resources]
                cond = json.dumps(st.get("Condition"), sort_keys=True)
                for action in actions:
                    for resource in resources:
                        tuples.add((effect, action, form, resource, cond))
            return tuples

        old_tuples = flatten(old)
        new_tuples = flatten(new)
        assert new_tuples == old_tuples, {
            "only_in_old": sorted(old_tuples - new_tuples),
            "only_in_new": sorted(new_tuples - old_tuples),
        }
        # The old policy really was over the hard cap (the reason for this PR),
        # so the fixture is the genuine pre-shrink state, not a copy of the new one.
        assert len(json.dumps(old, separators=(",", ":"))) > 6144

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


class TestFargateSessionGrants:
    """The caller's permission to reach a Fargate crew, and what it must NOT grant.

    Every assertion here is paired: what is present, and what is absent. The
    absences are the security property -- a policy that grants the right thing and
    also grants a shell is not a policy that passes.
    """

    def _statements(self):
        return iam.policy_document()["Statement"]

    def _by_sid(self, sid):
        return next(s for s in self._statements() if s.get("Sid") == sid)

    def test_the_caller_can_start_a_session_on_a_crew_task(self):
        """A NEW statement, because the instance one cannot reach a task ARN.

        ``SsmSessionOnManagedInstances`` is pinned to ``ec2:*:*:instance/*``, which
        no ECS task ARN matches. Widening that statement instead would have read as
        if it covered both lanes while authorising only one.
        """
        statement = self._by_sid("SsmSessionOnCrewTasks")
        assert statement["Effect"] == "Allow"
        assert statement["Resource"] == "arn:aws:ecs:*:*:task/kirocrew-crew-*/*"
        # Scoped to the crew clusters this launcher creates, not to every task in the account.
        assert statement["Resource"] != "arn:aws:ecs:*:*:task/*/*"
        assert "task/kirocrew-crew-" in statement["Resource"]

    def test_that_statement_grants_no_send_command(self):
        """SendCommand on a task would be a permission with no reachable use.

        RunCommand cannot target an ECS task at all. The sibling statements pair
        StartSession with SendCommand, which is exactly what makes adding it here by
        reflex easy, so its absence is pinned.
        """
        assert self._by_sid("SsmSessionOnCrewTasks")["Action"] == ["ssm:StartSession"]

    def test_the_inverted_deny_names_only_this_lane_resources(self):
        """ALLOW direction: a legitimate port-forward cannot be caught by this Deny.

        The statement denies ``ssm:StartSession`` against everything it does NOT
        name, so what has to be asserted is the exemption list, not a list of
        forbidden documents. Every resource a real port-forward presents -- the
        port-forward document, the EC2 or Fargate target, the session itself -- is
        named here, which is why the Deny cannot match the call. That argument holds
        whichever subset of those resources IAM evaluates, and it is the property
        making the inversion safe to ship.
        """
        statement = self._by_sid("DenyStartSessionOutsideTheLane")
        assert statement["Effect"] == "Deny"
        assert statement["Action"] == ["ssm:StartSession"]
        assert "Resource" not in statement, "an inverted statement carries NotResource only"
        assert set(statement["NotResource"]) == {
            "arn:aws:ssm:*::document/AWS-StartPortForwardingSession",
            "arn:aws:ec2:*:*:instance/*",
            "arn:aws:ecs:*:*:task/kirocrew-crew-*/*",
            "arn:aws:ssm:*:*:session/*",
        }, statement["NotResource"]
        assert len(statement["NotResource"]) == 4, "a duplicate would pass the set check"

    def test_every_start_session_allow_resource_is_exempt_from_the_deny(self):
        """The lane cannot be broken by an Allow the Deny does not exempt.

        This is the drift the inversion introduces, and the direction it fails in is
        the reason it needs pinning: an Allow added for a new StartSession target
        whose resource is not also added to ``NotResource`` is denied, so the lane
        stops working rather than opening. Fail-closed, but a silent outage, so it
        fails here instead.

        ``AWS-RunShellScript`` is the one deliberate exception -- a SendCommand
        document that no StartSession call ever names, so the inversion retiring its
        StartSession half is the intended narrowing rather than drift.
        """
        exempt = set(self._by_sid("DenyStartSessionOutsideTheLane")["NotResource"])
        deliberately_not_exempt = {"arn:aws:ssm:*::document/AWS-RunShellScript"}
        granted: set[str] = set()
        for statement in self._statements():
            if statement["Effect"] != "Allow":
                continue
            if "ssm:StartSession" not in statement["Action"]:
                continue
            resources = statement["Resource"]
            granted.update(resources if isinstance(resources, list) else [resources])
        assert granted, "no Allow grants StartSession; this test would be vacuous"
        unexempt = granted - exempt - deliberately_not_exempt
        assert not unexempt, f"StartSession allowed on {sorted(unexempt)}, denied by the lane"

    def test_no_interactive_document_is_exempt_from_the_deny(self):
        """DENY direction: every interactive document falls outside the exemption.

        Each name below is denied because it is ABSENT from ``NotResource``, which is
        the same reason a document AWS ships tomorrow is denied. A list of forbidden
        names can only ever reach the names on it; an exemption list reaches the class.

        The last two assertions are what keep that true: the only document exempted
        is the port-forward one, and no entry is a wildcard broad enough to exempt
        documents as a class. Without them a later ``document/*`` entry would silently
        re-open everything while the name checks above still passed.
        """
        exempt = self._by_sid("DenyStartSessionOutsideTheLane")["NotResource"]
        rendered = " ".join(exempt)
        for name in KNOWN_INTERACTIVE_DOCUMENTS:
            assert name not in rendered, f"{name} is exempt from the lane Deny"
        documents = {arn.split("document/", 1)[1] for arn in exempt if "document/" in arn}
        assert documents == {"AWS-StartPortForwardingSession"}, documents
        for arn in exempt:
            assert arn != "*", "a bare wildcard would exempt everything"
            assert not arn.endswith("document/*"), f"{arn} exempts every document"

    def test_no_interactive_document_is_allowed_anywhere(self):
        """The other barrier: no Allow reaches any of them.

        Default deny refuses an interactive document even without the Deny above, so
        this property holds independently of it. The names come from the module
        constant because an inverted statement carries no enumerated resource list to
        read them off.
        """
        for statement in self._statements():
            if statement["Effect"] != "Allow":
                continue
            resources = statement["Resource"]
            rendered = " ".join(resources) if isinstance(resources, list) else resources
            for name in KNOWN_INTERACTIVE_DOCUMENTS:
                assert name not in rendered, f"{statement.get('Sid')} allows {name}"

    def test_no_allow_statement_inverts_its_resource_list(self):
        """Inversion is safe in a Deny and unsafe in an Allow, so only the Deny may.

        ``NotResource`` on a Deny narrows: it denies everything unnamed. The same
        keyword on an Allow would GRANT everything unnamed, which is the reach the
        Fargate templates forbid outright in
        test_no_statement_inverts_the_enumeration. This policy needs the inverted
        form for its one Deny, so the ban is expressed as a direction rather than as
        an absence -- and ``NotAction`` stays banned outright, in either effect.
        """
        for statement in self._statements():
            assert "NotAction" not in statement, f"{statement['Sid']} inverts its action list"
            if statement["Effect"] == "Allow":
                assert "NotResource" not in statement, (
                    f"{statement['Sid']} is an Allow with NotResource, which grants "
                    "every resource it does not name"
                )

    def test_no_policy_grants_ecs_execute_command(self):
        """R1. This is the permission that would hand out a root shell in the task.

        enableExecuteCommand makes the task permanently shell-capable -- the
        platform bind-mounts its SSM agent in -- so IAM is the only barrier, and
        port-forwarding does not need this action. AWS documents stopping
        non-ECS-Exec sessions with a Deny on ssm:StartSession scoped to the task,
        which would be pointless if this action gated the path.
        """
        assert "ecs:ExecuteCommand" not in iam.policy_json()
        actions = {a for st in self._statements() for a in st["Action"]}
        assert not any(a.startswith("ecs:Execute") for a in actions), actions

    def test_no_start_session_statement_is_unscoped(self):
        """R2. No ``Resource: "*"`` on anything that can GRANT a session.

        Allow only. The one Deny carries ``NotResource``, and a broad Deny is the
        point of it rather than a finding against it.
        """
        for statement in self._statements():
            if statement["Effect"] != "Allow":
                continue
            if "ssm:StartSession" not in statement["Action"]:
                continue
            resources = statement["Resource"]
            assert resources != "*", statement.get("Sid")
            if isinstance(resources, list):
                assert "*" not in resources, statement.get("Sid")

    def test_the_remote_host_document_is_not_allowed(self):
        """The plain port-forward document reaches a task, so ToRemoteHost is not needed.

        It takes a caller-supplied ``host``, so allowing it would let a tunnel be
        aimed at any host the task can reach. Leaving it out is the tighter policy,
        and it is also what keeps this lane clear of the SSRF advisory against that
        document. Asserted as absent so a future edit has to argue for it.
        """
        assert "AWS-StartPortForwardingSessionToRemoteHost" not in iam.policy_json()
        assert "AWS-StartPortForwardingSession" in iam.policy_json()


class TestCrewPermissionsBoundary:
    """The Fargate lane's ceiling, and why it is not the EC2 one."""

    def test_the_ceiling_is_exactly_the_four_ssm_channel_actions(self):
        statements = iam.crew_boundary_policy_document()["Statement"]
        assert len(statements) == 1, statements
        actions = statements[0]["Action"]
        assert set(actions) == {
            "ssmmessages:CreateControlChannel",
            "ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel",
            "ssmmessages:OpenDataChannel",
        }, actions
        assert len(actions) == 4, f"duplicates would pass the set check: {actions}"

    def test_the_ceiling_is_tighter_than_the_task_roles_grant_is_wide(self):
        """Reusing the EC2 boundary was considered and is rejected by this number.

        A permissions boundary only caps. The EC2 ceiling's content is the full
        AmazonSSMManagedInstanceCore action set plus an S3 read -- it names
        ``ec2messages:*``, ``ssm:GetParameter`` and a dozen more. Capping a role
        whose entire grant is four ``ssmmessages:*`` actions with that would cap
        nothing while looking like compliance, because a boundary would be
        attached. Asserted as a comparison rather than argued in prose.
        """
        crew = iam.crew_boundary_policy_document()["Statement"][0]["Action"]
        ec2 = iam.boundary_policy_document("123456789012")["Statement"][0]["Action"]
        assert len(crew) == 4 and len(ec2) > 20, (len(crew), len(ec2))
        assert set(crew) < set(ec2), "the crew ceiling must be a strict subset"

    def test_the_ceiling_admits_nothing_the_task_does_not_need(self):
        actions = iam.crew_boundary_policy_document()["Statement"][0]["Action"]
        for forbidden in ("secretsmanager:", "ssm:", "kms:", "ec2messages:", "ecs:"):
            assert not any(a.startswith(forbidden) for a in actions), forbidden
        assert not any(a.endswith("*") for a in actions), actions

    def test_the_ceiling_is_content_fixed(self):
        """No account or region in it, which is what makes create-once reusable."""
        rendered = iam.crew_boundary_policy_json()
        assert "123456789012" not in rendered
        assert iam.crew_boundary_policy_json() == rendered  # deterministic

    def test_the_boundary_arn_names_the_pinned_policy(self):
        arn = iam.crew_boundary_arn("123456789012")
        assert arn == "arn:aws:iam::123456789012:policy/kirocrew-crew-boundary"
        assert iam.CREW_BOUNDARY_NAME == "kirocrew-crew-boundary"

    def test_the_create_once_grant_names_both_boundaries_exactly(self):
        """Three exact names, never a prefix.

        ``policy/kirocrew-*`` would let a leaked launcher credential author any
        policy whose name began that way and attach it, which is the escalation
        this statement's shape exists to prevent. Only the three create-once verbs,
        so an existing boundary's content cannot be replaced.
        """
        statement = next(
            s
            for s in iam.policy_document()["Statement"]
            if s["Sid"] == "IamInstanceBoundaryCreateOnce"
        )
        assert set(statement["Resource"]) == {
            f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}",
            f"arn:aws:iam::*:policy/{iam.CREW_BOUNDARY_NAME}",
            f"arn:aws:iam::*:policy/{iam.CREW_EXEC_BOUNDARY_NAME}",
        }, statement["Resource"]
        assert len(statement["Resource"]) == 3, "a duplicate would pass the set check"
        for resource in statement["Resource"]:
            assert not resource.endswith("kirocrew-*"), resource
        assert set(statement["Action"]) == {
            "iam:CreatePolicy",
            "iam:GetPolicy",
            "iam:GetPolicyVersion",
        }, statement["Action"]
        # Never the verbs that could re-author an existing boundary.
        for forbidden in (
            "iam:CreatePolicyVersion",
            "iam:DeletePolicy",
            "iam:SetDefaultPolicyVersion",
        ):
            assert forbidden not in statement["Action"]

    def test_the_template_parameter_pins_this_exact_policy_name(self):
        """The template and this module must not drift on the boundary's name.

        ``kirocrew-fargate-crew.yaml`` accepts only an ARN ending in this policy
        name, so a rename here without a matching edit there would produce a
        boundary nothing can reference.
        """
        from pathlib import Path

        template = (
            Path(iam.__file__).resolve().parent / "templates" / "kirocrew-fargate-crew.yaml"
        ).read_text(encoding="utf-8")
        assert f"policy/{iam.CREW_BOUNDARY_NAME}$" in template


class TestCrewExecutionBoundary:
    """The execution role's ceiling, and why it is not the task role's."""

    def test_the_ceiling_is_exactly_what_the_execution_role_is_granted(self):
        """A boundary caps to identity AND ceiling, so it must COVER the grant.

        The execution role fetches the crew's secret and opens its log stream before
        the container starts. Capping it with the task role's four ssmmessages
        actions denies both, so no task launches at all -- the failure is a launch
        failure rather than a policy-simulator complaint, which is why it is pinned
        as a set rather than described.
        """
        statements = iam.crew_exec_boundary_policy_document()["Statement"]
        assert len(statements) == 1, statements
        actions = statements[0]["Action"]
        assert set(actions) == {
            "secretsmanager:GetSecretValue",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "ecr:GetAuthorizationToken",
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
        }, actions
        assert len(actions) == 7, f"duplicates would pass the set check: {actions}"
        assert not any(a.endswith("*") for a in actions), actions

    def test_it_admits_no_ssm_channel_and_the_task_ceiling_admits_no_secret(self):
        """Two ceilings rather than one union, and this is the reason.

        A union would permit ``secretsmanager:GetSecretValue`` under the role a
        prompt can reach. Keeping the secret read out of the task ceiling makes
        "the container never holds the secret-reading role" a property of the
        ceiling too, not only of the identity policy.
        """
        execution = set(iam.crew_exec_boundary_policy_document()["Statement"][0]["Action"])
        task = set(iam.crew_boundary_policy_document()["Statement"][0]["Action"])
        assert not any(a.startswith("ssmmessages:") for a in execution), execution
        assert "secretsmanager:GetSecretValue" not in task, task
        assert not execution & task, "the two ceilings overlap, so one of them is wrong"

    def test_the_ceiling_is_content_fixed(self):
        rendered = iam.crew_exec_boundary_policy_json()
        assert "123456789012" not in rendered
        assert iam.crew_exec_boundary_policy_json() == rendered

    def test_the_boundary_arn_names_the_pinned_policy(self):
        arn = iam.crew_exec_boundary_arn("123456789012")
        assert arn == "arn:aws:iam::123456789012:policy/kirocrew-crew-exec-boundary"
        assert iam.CREW_EXEC_BOUNDARY_NAME == "kirocrew-crew-exec-boundary"
        assert iam.CREW_EXEC_BOUNDARY_NAME != iam.CREW_BOUNDARY_NAME

    def test_the_template_parameter_pins_this_exact_policy_name(self):
        from pathlib import Path

        template = (
            Path(iam.__file__).resolve().parent / "templates" / "kirocrew-fargate-crew.yaml"
        ).read_text(encoding="utf-8")
        assert f"policy/{iam.CREW_EXEC_BOUNDARY_NAME}$" in template

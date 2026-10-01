"""The printed launcher policy grants what CloudFormation's handlers call.

``test/fixtures/cfn_schema_handler_permissions.json`` pins, for every resource
type in ``kirocrew-ec2.yaml``, the ``handlers.create/update/delete.permissions``
lists from the public CloudFormation resource schemas. Each listed permission
either is allowed by ``iam.policy_document()`` for the concrete request the
launch makes (action, resource ARN under ``ROLE_PATH``, and the request's
condition keys), or carries a named reason in ``NOT_GRANTED``.
"""

import fnmatch
import json
import pathlib

import pytest

from kiro_crew.cloud import iam

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "cfn_schema_handler_permissions.json"
)
_TEMPLATE = pathlib.Path(iam.__file__).resolve().parent / "templates" / "kirocrew-ec2.yaml"

_ACCOUNT = "111122223333"
_REGION = "us-east-1"
_ROLE = f"arn:aws:iam::{_ACCOUNT}:role{iam.ROLE_PATH}{iam.ROLE_NAME_PREFIX}demo"
_PROFILE = f"arn:aws:iam::{_ACCOUNT}:instance-profile/{iam.ROLE_NAME_PREFIX}demo"
_SG = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:security-group/sg-0123456789abcdef0"
_VPC = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:vpc/vpc-0123456789abcdef0"
_INSTANCE = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:instance/i-0123456789abcdef0"
_SUBNET = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/subnet-0123456789abcdef0"
_ENI = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:network-interface/eni-0123456789abcdef0"
_VOLUME = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:volume/vol-0123456789abcdef0"
_IMAGE = f"arn:aws:ec2:{_REGION}::image/ami-0123456789abcdef0"
_MANAGED = {f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}": "true"}
_REQUEST_MANAGED = {f"aws:RequestTag/{iam.MANAGED_TAG_KEY}": "true"}
_BOUNDARY = f"arn:aws:iam::{_ACCOUNT}:policy/{iam.BOUNDARY_NAME}"
_SSM_CORE = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"

# The authorization requests one permission produces during a launch: every
# (resource ARN, request context) pair must be allowed. An action absent here
# is a read that authorizes on "*".
_REQUESTS = {
    "iam:CreateRole": [(_ROLE, {"iam:PermissionsBoundary": _BOUNDARY, **_REQUEST_MANAGED})],
    "iam:PutRolePolicy": [(_ROLE, _MANAGED)],
    "iam:TagRole": [(_ROLE, _MANAGED)],
    "iam:AttachRolePolicy": [(_ROLE, {"iam:PolicyARN": _SSM_CORE})],
    "iam:DetachRolePolicy": [(_ROLE, {"iam:PolicyARN": _SSM_CORE})],
    "iam:GetRole": [(_ROLE, {})],
    "iam:GetRolePolicy": [(_ROLE, {})],
    "iam:DeleteRole": [(_ROLE, {})],
    "iam:DeleteRolePolicy": [(_ROLE, {})],
    "iam:ListAttachedRolePolicies": [(_ROLE, {})],
    "iam:ListRolePolicies": [(_ROLE, {})],
    "iam:PassRole": [(_ROLE, {"iam:PassedToService": "ec2.amazonaws.com"})],
    "iam:CreateInstanceProfile": [(_PROFILE, {})],
    "iam:AddRoleToInstanceProfile": [(_PROFILE, {})],
    "iam:RemoveRoleFromInstanceProfile": [(_PROFILE, {})],
    "iam:GetInstanceProfile": [(_PROFILE, {})],
    "iam:DeleteInstanceProfile": [(_PROFILE, {})],
    "ec2:CreateSecurityGroup": [(_SG, _REQUEST_MANAGED), (_VPC, {})],
    "ec2:AuthorizeSecurityGroupEgress": [(_SG, _MANAGED)],
    "ec2:AuthorizeSecurityGroupIngress": [(_SG, _MANAGED)],
    "ec2:RevokeSecurityGroupEgress": [(_SG, _MANAGED)],
    "ec2:RevokeSecurityGroupIngress": [(_SG, _MANAGED)],
    "ec2:DeleteSecurityGroup": [(_SG, _MANAGED)],
    "ec2:DeleteTags": [(_SG, _MANAGED), (_INSTANCE, _MANAGED)],
    "ec2:CreateTags": [
        (_SG, {"ec2:CreateAction": "CreateSecurityGroup"}),
        (_INSTANCE, {"ec2:CreateAction": "RunInstances"}),
    ],
    "ec2:RunInstances": [
        (_INSTANCE, _REQUEST_MANAGED),
        (_SUBNET, {}),
        (_SG, {}),
        (_ENI, {}),
        (_VOLUME, {}),
        (_IMAGE, {}),
    ],
    "ec2:DescribeInstanceAttribute": [(_INSTANCE, _MANAGED)],
    "ec2:StartInstances": [(_INSTANCE, _MANAGED)],
    "ec2:StopInstances": [(_INSTANCE, _MANAGED)],
    "ec2:TerminateInstances": [(_INSTANCE, _MANAGED)],
}

# Schema permissions the printed policy deliberately does not grant, by reason.
# The schema lists a handler's whole superset; these are only called for a
# property the template does not set, or for an update the launcher must never
# be able to make.
_PROPERTY_UNUSED = "the template sets no property whose handler path calls it"
_ESCALATION = "withheld: would let the launcher rewrite the role's trust or boundary"
NOT_GRANTED = {
    "AWS::IAM::Role": {
        "iam:UntagRole": "the template never removes a role tag",
        "iam:UpdateRole": _PROPERTY_UNUSED,
        "iam:UpdateRoleDescription": _PROPERTY_UNUSED,
        "iam:UpdateAssumeRolePolicy": _ESCALATION,
        "iam:PutRolePermissionsBoundary": _ESCALATION,
        "iam:DeleteRolePermissionsBoundary": _ESCALATION,
    },
    "AWS::EC2::SecurityGroup": {
        "ec2:UpdateSecurityGroupRuleDescriptionsIngress": _PROPERTY_UNUSED,
        "ec2:UpdateSecurityGroupRuleDescriptionsEgress": _PROPERTY_UNUSED,
    },
    "AWS::EC2::SecurityGroupIngress": {
        "ec2:UpdateSecurityGroupRuleDescriptionsIngress": _PROPERTY_UNUSED,
    },
    "AWS::EC2::Instance": {
        "ec2:ModifyPrivateDnsNameOptions": _PROPERTY_UNUSED,
        "ec2:AssociateIamInstanceProfile": _PROPERTY_UNUSED,
        "ec2:DisassociateIamInstanceProfile": _PROPERTY_UNUSED,
        "ec2:ReplaceIamInstanceProfileAssociation": _PROPERTY_UNUSED,
        "ec2:ModifyInstanceAttribute": _PROPERTY_UNUSED,
        "ec2:ModifyInstanceCreditSpecification": _PROPERTY_UNUSED,
        "ec2:ModifyInstanceMaintenanceOptions": _PROPERTY_UNUSED,
        "ec2:ModifyInstanceMetadataOptions": _PROPERTY_UNUSED,
        "ec2:ModifyInstancePlacement": _PROPERTY_UNUSED,
        "ec2:MonitorInstances": _PROPERTY_UNUSED,
        "ec2:UnmonitorInstances": _PROPERTY_UNUSED,
        "ec2:AttachVolume": _PROPERTY_UNUSED,
        "ec2:DetachVolume": _PROPERTY_UNUSED,
        "ssm:CreateAssociation": _PROPERTY_UNUSED,
        "ssm:DeleteAssociation": _PROPERTY_UNUSED,
        "ec2:DescribeLaunchTemplateVersions": (
            "denied: returns launch-template user data; the template sets no LaunchTemplate"
        ),
    },
}


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _condition_holds(condition, context):
    for operator, block in (condition or {}).items():
        for key, expected in block.items():
            if key not in context:
                # IAM: an absent key never matches; a negated operator is true.
                if operator == "StringNotEquals":
                    continue
                return False
            actual = context[key]
            if operator in ("StringEquals", "ArnEquals"):
                ok = actual in _as_list(expected)
            elif operator == "StringNotEquals":
                ok = actual not in _as_list(expected)
            elif operator in ("StringLike", "ArnLike"):
                ok = any(fnmatch.fnmatchcase(actual, pat) for pat in _as_list(expected))
            else:
                raise AssertionError(f"evaluator does not model {operator}")
            if not ok:
                return False
    return True


def _matches(statement, action, resource, context):
    if "Action" in statement:
        if not any(fnmatch.fnmatchcase(action, a) for a in _as_list(statement["Action"])):
            return False
    elif any(fnmatch.fnmatchcase(action, a) for a in _as_list(statement["NotAction"])):
        return False
    if "Resource" in statement:
        if not any(fnmatch.fnmatchcase(resource, r) for r in _as_list(statement["Resource"])):
            return False
    elif any(fnmatch.fnmatchcase(resource, r) for r in _as_list(statement["NotResource"])):
        return False
    return _condition_holds(statement.get("Condition"), context)


def allowed(action, resource, context):
    statements = iam.policy_document()["Statement"]
    hits = [s for s in statements if _matches(s, action, resource, context)]
    return any(s["Effect"] == "Allow" for s in hits) and not any(
        s["Effect"] == "Deny" for s in hits
    )


def _schema():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["types"]


def _cases():
    for rtype, handlers in _schema().items():
        for handler, permissions in handlers.items():
            for permission in permissions:
                yield pytest.param(rtype, handler, permission, id=f"{rtype}-{handler}-{permission}")


def test_fixture_covers_every_resource_type_in_the_template():
    text = _TEMPLATE.read_text(encoding="utf-8")
    body = text.split("\nResources:\n", 1)[1].split("\nOutputs:\n", 1)[0]
    types = {
        line.split("Type:", 1)[1].strip()
        for line in body.splitlines()
        if line.startswith("    Type: ")
    }
    assert types == set(_schema())


@pytest.mark.parametrize(("rtype", "handler", "permission"), list(_cases()))
def test_schema_permission_is_granted_or_named(rtype, handler, permission):
    reason = NOT_GRANTED.get(rtype, {}).get(permission)
    requests = _REQUESTS.get(permission, [("*", {})])
    granted = all(allowed(permission, res, ctx) for res, ctx in requests)
    if reason is None:
        assert granted, (
            f"{rtype} {handler} calls {permission}, which the printed launcher "
            f"policy denies for {requests}"
        )
    else:
        assert not granted, f"{permission} is granted now; drop it from NOT_GRANTED"


def test_not_granted_entries_are_all_in_the_schema():
    schema = _schema()
    for rtype, entries in NOT_GRANTED.items():
        listed = {p for perms in schema[rtype].values() for p in perms}
        assert set(entries) <= listed, sorted(set(entries) - listed)


def test_revoke_security_group_egress_is_required_and_granted():
    # The launch failure this check exists for: SecurityGroup create revokes
    # the default allow-all egress before applying the declared egress.
    assert "ec2:RevokeSecurityGroupEgress" in _schema()["AWS::EC2::SecurityGroup"]["create"]
    assert allowed("ec2:RevokeSecurityGroupEgress", _SG, _MANAGED)


def test_user_data_reads_limited_to_managed_instances():
    assert allowed("ec2:DescribeInstanceAttribute", _INSTANCE, _MANAGED)
    assert not allowed("ec2:DescribeInstanceAttribute", _INSTANCE, {})
    other = {f"aws:ResourceTag/{iam.MANAGED_TAG_KEY}": "false"}
    assert not allowed("ec2:DescribeInstanceAttribute", _INSTANCE, other)
    for action in (
        "ec2:DescribeLaunchTemplateVersions",
        "ec2:DescribeSpotFleetRequests",
        "ec2:DescribeSpotInstanceRequests",
        "ec2:DescribeVpnConnections",
    ):
        assert not allowed(action, "*", {}), action
    assert allowed("ec2:DescribeSpotFleetRequestHistory", "*", {})


def test_passrole_denied_for_a_root_path_role():
    root_role = f"arn:aws:iam::{_ACCOUNT}:role/{iam.ROLE_NAME_PREFIX}demo"
    ctx = {"iam:PassedToService": "ec2.amazonaws.com", **_MANAGED}
    assert allowed("iam:PassRole", _ROLE, ctx)
    assert not allowed("iam:PassRole", root_role, ctx)

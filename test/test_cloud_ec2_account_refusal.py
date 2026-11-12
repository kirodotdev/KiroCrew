"""A launch EC2 refuses on an account limit leads with the plain cause and next step.

Split from ``test_cloud_ec2.py``, which is at the test-file line cap.
"""

from __future__ import annotations

import pytest

import kiro_crew.cloud.source as source_mod
from kiro_crew.cloud import aws, ec2, sizes

_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary"
_GENERIC_DEPLOY_ERR = (
    "\naws: [ERROR]: Failed to create/update the stack. Run the following command\n"
    "to fetch the list of events leading up to the failure\n"
    "aws cloudformation describe-stack-events --stack-name kirocrew-kc-22d3b9\n"
)
# CloudFormation's resource-handler shape: EC2's message, no EC2 error code.
_VCPU_REASON = (
    'Resource handler returned message: "You have requested more vCPU capacity than '
    "your current vCPU limit of 5 allows for the instance bucket that the specified "
    'instance type belongs to. (Service: Ec2, Status Code: 400, Request ID: 88d2172b)" '
    "(RequestToken: f52f585a, HandlerErrorCode: InvalidRequest)"
)
_QUOTA_LEAD = (
    "cloudformation deploy failed: this AWS account's vCPU quota in this Region has no "
    "room for the {vcpu} vCPU of the {label} size: request a higher Running On-Demand "
    "Standard instances quota (L-1216C47A) in Service Quotas{smaller} and retry — aws: [ERROR]:"
)
_VALIDATION_LEAD = (
    "cloudformation deploy failed: this AWS account is still being validated for "
    "launches in this Region: AWS emails you when it is done (usually within minutes, "
    "up to 4 hours), then retry — aws: [ERROR]:"
)


@pytest.mark.parametrize(
    ("tier_key", "reason", "expected_start"),
    [
        (
            "power",
            _VCPU_REASON,
            _QUOTA_LEAD.format(vcpu=16, label="Power", smaller=", or pick a smaller size,"),
        ),
        ("light", _VCPU_REASON, _QUOTA_LEAD.format(vcpu=4, label="Light", smaller="")),
        (
            "balanced",
            'Resource handler returned message: "Your request for accessing resources in '
            "this region is being validated, and you will not be able to launch additional "
            "resources in this region until the validation is complete. (Service: Ec2, "
            'Status Code: 400, Request ID: 1a2b)" (RequestToken: 3c4d, '
            "HandlerErrorCode: InvalidRequest)",
            _VALIDATION_LEAD,
        ),
    ],
    ids=["vcpu-quota-power", "vcpu-quota-light", "account-validation"],
)
def test_deploy_leads_with_the_cause_of_an_ec2_account_refusal(
    monkeypatch, tier_key, reason, expected_start
):
    monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
    monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
    monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "k"))
    monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
    monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (1, "", _GENERIC_DEPLOY_ERR))
    monkeypatch.setattr(
        ec2,
        "get_stack_failures",
        lambda *a, **k: [{"resource": "Instance", "status": "CREATE_FAILED", "reason": reason}],
    )
    with pytest.raises(aws.AWSError) as caught:
        ec2.deploy(
            tag="kc-22d3b9", tier=sizes.TIERS_BY_KEY[tier_key], profile="dev", region="us-east-1"
        )
    message = str(caught.value)
    assert caught.value.missing_action is None
    assert message.startswith(expected_start)
    assert message.endswith(f"root cause: Instance: {reason}")

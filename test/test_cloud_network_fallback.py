"""The network a launch with no default VPC falls back to is named before the stack.

``ec2.discover_network`` takes the Region's only VPC when there is no default one, a
pick the user never made; it reports that pick through ``on_fallback``, and
``ec2.deploy`` hands it to its caller before ``cloudformation deploy`` runs.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud import aws, ec2, sizes


def _route_table(target: dict, subnet_ids: list[str]) -> dict:
    return {
        "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "State": "active", **target}],
        "Associations": [{"SubnetId": sid} for sid in subnet_ids],
    }


@pytest.mark.parametrize(
    ("has_default", "target", "via"),
    [
        (False, {"NatGatewayId": "nat-xyz"}, "NAT"),
        (False, {"GatewayId": "igw-abc"}, "an internet gateway"),
        (True, {"GatewayId": "igw-abc"}, None),
    ],
)
def test_only_a_fallback_network_is_named(monkeypatch, has_default, target, via):
    # The default VPC is the pick a user expects, so only the fallback is named.
    def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
        if "describe-vpcs" in args:
            if "Name=isDefault,Values=true" in args and not has_default:
                return {"Vpcs": []}
            return {"Vpcs": [{"VpcId": "vpc-0ccf"}]}
        if "describe-subnets" in args:
            return {"Subnets": [{"SubnetId": "subnet-06dc", "AvailabilityZone": "us-east-2a"}]}
        if "describe-route-tables" in args:
            return {"RouteTables": [_route_table(target, ["subnet-06dc"])]}
        return {}

    monkeypatch.setattr(aws, "checked_json", fake_json)
    notes: list[str] = []
    picked = ec2.discover_network("dev", "us-east-2", on_fallback=notes.append)

    assert picked[:2] == ("vpc-0ccf", "subnet-06dc")
    if via is None:
        assert notes == []
        return
    assert notes == [
        "No default VPC in us-east-2, so this launch uses the only VPC there, vpc-0ccf, "
        f"with subnet subnet-06dc (internet through {via}). This launch goes on; to use "
        "another network next time, give a subnet ID (the Subnet ID field, or --subnet)."
    ]


def test_the_note_reaches_the_caller_before_the_stack_is_created(monkeypatch):
    import kiro_crew.cloud.source as source_mod

    monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
    monkeypatch.setattr(
        source_mod,
        "ensure_instance_boundary",
        lambda *a, **k: "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary",
    )
    monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "t1/k.tar.gz"))
    monkeypatch.setattr(
        ec2, "describe", lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"}
    )
    events: list[str] = []

    def fake_discover(profile, region, instance_type="", on_fallback=None):
        assert on_fallback is not None
        on_fallback("note")
        return ("vpc-1", "subnet-1", "igw")

    def fake_run(argv, profile="", region="", *, timeout=ec2._DEPLOY_TIMEOUT, proc_sink=None):
        if argv[:2] == ["cloudformation", "deploy"]:
            events.append("deploy")
        return (0, "ok", "")

    monkeypatch.setattr(ec2, "discover_network", fake_discover)
    monkeypatch.setattr(aws, "run_aws", fake_run)
    ec2.deploy(
        tag="t1",
        tier=sizes.default_tier(),
        profile="dev",
        region="us-east-1",
        on_network_fallback=events.append,
    )
    assert events == ["note", "deploy"]

"""A default route to a gateway that is gone is named, not reported missing.

Deleting a NAT gateway leaves its ``0.0.0.0/0`` route in the table with state
``blackhole``. The launch still refuses the subnet, but the refusal names that
route and its target instead of asking the user to add a route that exists.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud import aws, ec2

_DEAD_NAT = {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-gone", "State": "blackhole"}


def test_explicit_subnet_names_its_blackholed_default_route(monkeypatch):
    def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
        if "describe-subnets" in args and "--subnet-ids" in args:
            subnet = {"SubnetId": "subnet-bh", "VpcId": "vpc-1", "AvailabilityZone": "us-east-1a"}
            return {"Subnets": [subnet]}
        if "describe-route-tables" in args:
            table = {"Routes": [_DEAD_NAT], "Associations": [{"SubnetId": "subnet-bh"}]}
            return {"RouteTables": [table]}
        return {}

    monkeypatch.setattr(aws, "checked_json", fake_json)
    with pytest.raises(aws.AWSError) as ei:
        ec2.resolve_explicit_subnet("subnet-bh", "dev", "us-east-1")
    msg = str(ei.value)
    assert "nat-gone" in msg and "blackhole" in msg
    assert "add a NAT" not in msg


def test_discovery_names_a_blackholed_main_table_route(monkeypatch):
    def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
        if "describe-vpcs" in args:
            return {"Vpcs": [{"VpcId": "vpc-1"}]}
        if "describe-subnets" in args:
            return {"Subnets": [{"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"}]}
        if "describe-route-tables" in args:
            return {"RouteTables": [{"Routes": [_DEAD_NAT], "Associations": [{"Main": True}]}]}
        return {}

    monkeypatch.setattr(aws, "checked_json", fake_json)
    with pytest.raises(aws.AWSError) as ei:
        ec2.discover_network("dev", "us-east-1")
    msg = str(ei.value)
    assert "subnet-a" in msg and "nat-gone" in msg and "blackhole" in msg
    assert "Add an internet gateway" not in msg

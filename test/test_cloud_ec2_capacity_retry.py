"""A launch EC2 refuses for lack of capacity in one zone moves to another zone.

``ec2.deploy`` deletes the refused stack and launches again in a subnet of a zone
not tried yet, until one works or every zone that offers the size has refused.
The last failure then says so in plain words and names another size and Region.
No real AWS call: ``aws.run_aws`` and the discovery reads are faked.
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


def _capacity_reason(instance_type: str, zone: str) -> str:
    # CloudFormation's resource-handler shape of EC2's InsufficientInstanceCapacity.
    return (
        f'Resource handler returned message: "We currently do not have sufficient '
        f"{instance_type} capacity in the Availability Zone you requested ({zone}). Our "
        "system will be working on provisioning additional capacity. (Service: Ec2, "
        'Status Code: 500, Request ID: 1a2b)" (RequestToken: 3c4d, '
        "HandlerErrorCode: GeneralServiceException)"
    )


class _FakeAws:
    """Each ``cloudformation deploy`` fails in the next zone of ``zones`` until it
    runs out, then succeeds. Records every deploy's subnet and every delete."""

    def __init__(self, monkeypatch, tier, zones, subnets):
        self.tier = tier
        self.zones = list(zones)
        self.subnets = subnets  # zone -> subnet id, in discovery order
        self.deployed: list[str] = []
        self.deleted = 0
        self.skip_seen: list[frozenset] = []
        self.notes: list[str] = []
        self.cleaned = 0
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
        monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "kc/k.tar.gz"))
        monkeypatch.setattr(source_mod, "delete_source", self._delete_source)
        monkeypatch.setattr(ec2, "assert_download_hosts_resolvable", lambda *a, **k: None)
        monkeypatch.setattr(ec2, "discover_network", self._discover)
        monkeypatch.setattr(ec2, "destroy", self._destroy)
        monkeypatch.setattr(aws, "run_aws", self._run)
        monkeypatch.setattr(ec2, "get_stack_failures", self._failures)
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"},
        )

    def _delete_source(self, *a, **k):
        self.cleaned += 1
        return {"removed": True}

    def _discover(self, profile, region, instance_type="", on_fallback=None, skip_zones=None):
        skip = frozenset(skip_zones or ())
        self.skip_seen.append(skip)
        for zone, subnet in self.subnets.items():
            if zone not in skip:
                return ("vpc-1", subnet, "igw")
        raise aws.AWSError(f"no subnet in an AZ that offers {instance_type}")

    def _destroy(self, tag, profile="", region="", **k):
        self.deleted += 1
        return {"destroyed": True}

    def _run(self, argv, profile="", region="", **k):
        subnet = next(a.split("=", 1)[1] for a in argv if a.startswith("SubnetId="))
        self.deployed.append(subnet)
        if self.zones:
            self.failing = self.zones.pop(0)
            return 1, "", _GENERIC_DEPLOY_ERR
        return 0, "", ""

    def _failures(self, *a, **k):
        reason = _capacity_reason(self.tier.instance_type, self.failing)
        return [{"resource": "Instance", "status": "CREATE_FAILED", "reason": reason}]


def _deploy(fake: _FakeAws, **kw):
    return ec2.deploy(
        tag="kc-22d3b9",
        tier=fake.tier,
        profile="dev",
        region="us-east-1",
        ship_source=True,
        on_network_fallback=fake.notes.append,
        **kw,
    )


def test_a_capacity_refusal_moves_the_launch_to_another_zone(monkeypatch):
    tier = sizes.TIERS_BY_KEY["power"]
    fake = _FakeAws(
        monkeypatch,
        tier,
        zones=["us-east-1c"],
        subnets={"us-east-1c": "subnet-0c", "us-east-1a": "subnet-0a"},
    )

    result = _deploy(fake)

    assert result.instance_id == "i-1"
    assert fake.deployed == ["subnet-0c", "subnet-0a"]
    assert fake.deleted == 1  # the refused stack is removed before the next try
    assert fake.skip_seen == [frozenset(), frozenset({"us-east-1c"})]
    assert fake.notes == [
        "AWS has no m7g.4xlarge capacity in us-east-1c right now, so this launch is "
        "trying again in subnet subnet-0a."
    ]
    assert fake.cleaned == 0  # the uploaded source stays for the launch that worked


def test_every_zone_out_of_capacity_says_so_and_names_another_size(monkeypatch):
    tier = sizes.TIERS_BY_KEY["power"]
    fake = _FakeAws(
        monkeypatch,
        tier,
        zones=["us-east-1c", "us-east-1a"],
        subnets={"us-east-1c": "subnet-0c", "us-east-1a": "subnet-0a"},
    )

    with pytest.raises(aws.AWSError) as caught:
        _deploy(fake)

    message = str(caught.value)
    assert message.startswith(
        "cloudformation deploy failed: AWS has no m7g.4xlarge capacity right now in any "
        "zone this launch could use (us-east-1c, us-east-1a): pick the Power (x86_64) "
        "size (--size power-x86), or pick another Region, and retry — aws: [ERROR]:"
    )
    assert message.endswith(
        f"root cause: Instance: {_capacity_reason('m7g.4xlarge', 'us-east-1a')}"
    )
    assert fake.deployed == ["subnet-0c", "subnet-0a"]
    assert fake.deleted == 2  # every refused stack was removed; none is left billing
    assert fake.cleaned == 1


def test_a_chosen_subnet_is_not_moved_and_the_error_names_the_subnet_field(monkeypatch):
    tier = sizes.TIERS_BY_KEY["balanced"]
    fake = _FakeAws(monkeypatch, tier, zones=["us-east-1c"], subnets={})
    monkeypatch.setattr(
        ec2,
        "resolve_explicit_subnet",
        lambda subnet_id, *a, **k: ("vpc-1", subnet_id, "igw"),
    )

    with pytest.raises(aws.AWSError) as caught:
        _deploy(fake, subnet_id="subnet-0123abcd")

    assert str(caught.value).startswith(
        "cloudformation deploy failed: AWS has no m7g.2xlarge capacity in us-east-1c right "
        "now: clear the Subnet ID (--subnet) so the launch tries the other zones, or pick "
        "the Development (x86_64) size (--size balanced-x86), or pick another Region, and "
        "retry — aws: [ERROR]:"
    )
    assert fake.deployed == ["subnet-0123abcd"]
    assert fake.deleted == 0


def test_keep_on_failure_keeps_the_stack_and_does_not_retry(monkeypatch):
    tier = sizes.TIERS_BY_KEY["balanced"]
    fake = _FakeAws(
        monkeypatch,
        tier,
        zones=["us-east-1c"],
        subnets={"us-east-1c": "subnet-0c", "us-east-1a": "subnet-0a"},
    )

    with pytest.raises(aws.AWSError) as caught:
        _deploy(fake, disable_rollback=True)

    assert "AWS has no m7g.2xlarge capacity in us-east-1c right now" in str(caught.value)
    assert fake.deployed == ["subnet-0c"]
    assert fake.deleted == 0


def test_another_failure_is_not_retried(monkeypatch):
    tier = sizes.TIERS_BY_KEY["balanced"]
    fake = _FakeAws(
        monkeypatch,
        tier,
        zones=["us-east-1c"],
        subnets={"us-east-1c": "subnet-0c", "us-east-1a": "subnet-0a"},
    )
    monkeypatch.setattr(
        ec2,
        "get_stack_failures",
        lambda *a, **k: [{"resource": "WaitCondition", "status": "CREATE_FAILED", "reason": "x"}],
    )

    with pytest.raises(aws.AWSError) as caught:
        _deploy(fake)

    assert "capacity" not in str(caught.value)
    assert fake.deployed == ["subnet-0c"]
    assert fake.deleted == 0


def test_discovery_leaves_out_the_skipped_zones(monkeypatch):
    def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
        if "describe-vpcs" in args:
            return {"Vpcs": [{"VpcId": "vpc-1"}]}
        if "describe-subnets" in args:
            return {
                "Subnets": [
                    {"SubnetId": "subnet-0c", "AvailabilityZone": "us-east-1c"},
                    {"SubnetId": "subnet-0a", "AvailabilityZone": "us-east-1a"},
                ]
            }
        if "describe-route-tables" in args:
            return {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "State": "active",
                                "GatewayId": "igw-1",
                            }
                        ],
                        "Associations": [{"SubnetId": "subnet-0c"}, {"SubnetId": "subnet-0a"}],
                    }
                ]
            }
        return {}

    monkeypatch.setattr(aws, "checked_json", fake_json)

    assert ec2.discover_network("dev", "us-east-1")[1] == "subnet-0c"
    picked = ec2.discover_network("dev", "us-east-1", skip_zones=frozenset({"us-east-1c"}))
    assert picked[1] == "subnet-0a"
    with pytest.raises(aws.AWSError):
        ec2.discover_network("dev", "us-east-1", skip_zones=frozenset({"us-east-1c", "us-east-1a"}))

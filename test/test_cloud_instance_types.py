"""Custom EC2 instance types for the built-in Remote Crew launch.

``cloud.instance_types`` lists the types a Region offers that fit a Crew host and
turns a picked one into a size tier; ``GET /api/cloud/instance-types`` serves the
list, and ``POST /api/cloud/launch`` refuses a type the Region does not offer.
No real AWS call: ``aws.checked_json`` is faked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.cloud import aws, ec2, instance_types
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud import sizes
from kiro_crew.dashboard import handlers_cloud as hc
from kiro_crew.dashboard import handlers_cloud_instance_types as hcit

_OFFERINGS = [
    ("m7g.large", "us-east-1a"),
    ("m7g.large", "us-east-1b"),
    ("m7i.xlarge", "us-east-1a"),
    ("t3.small", "us-east-1a"),  # 2 GiB: below the 16 GB floor
    ("trn1.2xlarge", "us-east-1a"),  # starts with t, but not a t family
    ("g5.xlarge", "us-east-1a"),  # GPU family
    ("c7g.2xlarge", "us-east-1c"),
    ("mac2.metal", "us-east-1a"),
]


def _facts(name, vcpu, mib, arches):
    return {
        "InstanceType": name,
        "VCpuInfo": {"DefaultVCpus": vcpu},
        "MemoryInfo": {"SizeInMiB": mib},
        "ProcessorInfo": {"SupportedArchitectures": arches},
    }


_TYPES = [
    _facts("m7i.xlarge", 4, 16384, ["x86_64"]),
    _facts("m7g.large", 2, 8192, ["arm64"]),  # 8 GiB: below the 16 GB floor
    _facts("t3.small", 2, 2048, ["x86_64", "i386"]),
    _facts("trn1.2xlarge", 8, 32768, ["x86_64"]),
    _facts("g5.xlarge", 4, 16384, ["x86_64"]),
    _facts("c7g.2xlarge", 8, 16384, ["arm64"]),
    _facts("m5.large", 2, 8192, ["x86_64"]),  # current but not offered here
]


class _FakeEc2:
    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        self.fail = False
        monkeypatch.setattr(aws, "checked_json", self)

    def __call__(self, args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
        self.calls.append(args[1])
        if self.fail:
            raise aws.AWSError("denied", action=action)
        if args[1] == "describe-instance-type-offerings":
            return {
                "InstanceTypeOfferings": [
                    {"InstanceType": n, "Location": z, "LocationType": "availability-zone"}
                    for n, z in _OFFERINGS
                ]
            }
        if args[1] == "describe-instance-types":
            assert "Name=current-generation,Values=true" in args
            return {"InstanceTypes": _TYPES}
        raise AssertionError(args)


@pytest.fixture
def fake_ec2(monkeypatch):
    return _FakeEc2(monkeypatch)


def test_the_list_keeps_offered_crew_host_types_only(fake_ec2):
    rows = instance_types.list_offered("dev", "us-east-1")

    assert [r.to_dict() for r in rows] == [
        {
            "instance_type": "c7g.2xlarge",
            "vcpu": 8,
            "memory_gib": 16.0,
            "arch": "arm64",
            "zones": ["us-east-1c"],
        },
        {
            "instance_type": "m7i.xlarge",
            "vcpu": 4,
            "memory_gib": 16.0,
            "arch": "x86_64",
            "zones": ["us-east-1a"],
        },
    ]


def test_the_list_is_cached_per_profile_and_region_until_it_expires(fake_ec2):
    now = [1000.0]
    instance_types.list_offered("dev", "us-east-1", clock=lambda: now[0])
    instance_types.list_offered("dev", "us-east-1", clock=lambda: now[0])
    assert len(fake_ec2.calls) == 2  # one read of each kind

    instance_types.list_offered("dev", "us-west-2", clock=lambda: now[0])
    assert len(fake_ec2.calls) == 4

    now[0] += instance_types.CACHE_TTL_SECONDS
    instance_types.list_offered("dev", "us-east-1", clock=lambda: now[0])
    assert len(fake_ec2.calls) == 6


def test_a_failed_read_is_not_cached(fake_ec2):
    fake_ec2.fail = True
    with pytest.raises(aws.AWSError):
        instance_types.list_offered("dev", "us-east-1")
    fake_ec2.fail = False
    assert instance_types.list_offered("dev", "us-east-1")


@pytest.mark.parametrize(
    "name",
    ["", "m7i", ".xlarge", "m7i.", "M7I.xlarge", "m7i.xlarge;id", "m7i.x.large", "7m.large"],
)
def test_a_name_that_is_not_an_instance_type_is_refused(name):
    with pytest.raises(ValueError):
        instance_types.validate_name(name)


def test_a_custom_tier_takes_the_type_and_its_architecture(fake_ec2):
    tier = instance_types.custom_tier("c7g.2xlarge", "dev", "us-east-1")

    assert (tier.instance_type, tier.arch, tier.vcpu, tier.ram_gb) == (
        "c7g.2xlarge",
        sizes.ARCH_ARM64,
        8,
        16,
    )
    assert tier.disk_gb == instance_types.CUSTOM_DISK_GB


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("m5.large", "is not offered in us-east-1"),
        ("t3.small", "Kiro Crew needs at least 16 GB memory, and t3.small has 2 GiB"),
        ("m7g.large", "Kiro Crew needs at least 16 GB memory, and m7g.large has 8 GiB"),
        ("g5.xlarge", "is not in a family Kiro Crew runs on"),
    ],
)
def test_a_type_the_region_does_not_offer_is_refused(fake_ec2, name, said):
    with pytest.raises(ValueError, match=said):
        instance_types.custom_tier(name, "dev", "us-east-1")


def test_the_engine_launches_the_custom_type(fake_ec2, monkeypatch):
    from kiro_crew.cloud.launch_engine import RealLaunchEngine

    seen = {}

    def fake_deploy(**kw):
        seen["tier"] = kw["tier"]
        return SimpleNamespace(instance_id="i-1")

    monkeypatch.setattr(ec2, "deploy", fake_deploy)
    RealLaunchEngine().provision(
        tag="kc-1",
        size_key="balanced",
        profile="dev",
        region="us-east-1",
        instance_type="m7i.xlarge",
    )
    assert (seen["tier"].instance_type, seen["tier"].arch) == ("m7i.xlarge", sizes.ARCH_X86_64)


# ── HTTP ──────────────────────────────────────────────────────────────────


class _Engine:
    def __init__(self):
        self.seen: dict = {}

    def preflight(self, profile, region):
        pass

    def provision(self, *, tag, size_key, profile, region, instance_type="", subnet_id=""):
        self.seen["instance_type"] = instance_type
        return "i-0abc123456789def0"

    def begin_signin(self, *, instance_id, profile, region):
        return SimpleNamespace(
            already_logged_in=True,
            url="",
            code="",
            ports=[],
            wait=lambda cancel: True,
            close=lambda: None,
        )

    def register(self, *, instance_id, tag, profile, region):
        pass

    def teardown(self, *, tag, profile, region):
        pass


def _state(tmp_path):
    return SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_engine=_Engine(),
        cloud_launch_store=lj.LaunchJobStore(root=tmp_path / "launch-jobs"),
    )


def _req(method, path, *, state, slack=False, body=None):
    app = web.Application()
    app["state"] = state
    headers = {"X-Session-Key": "slack:x"} if slack else {}
    req = make_mocked_request(method, path, headers=headers, app=app)
    req["user"] = "owner-1"
    req["app"] = ""
    if body is not None:

        async def _json():
            return body

        req.json = _json  # type: ignore[assignment]
    return req


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _posix_host(_floor_monkeypatch):
    _floor_monkeypatch.setattr(hc.sys, "platform", "linux")


@pytest.mark.asyncio
class TestRoutes:
    async def test_the_list_route_answers_the_offered_types(self, tmp_path, fake_ec2):
        resp = await hcit.api_cloud_instance_types(
            _req(
                "GET",
                "/api/cloud/instance-types?profile=dev&region=us-east-1",
                state=_state(tmp_path),
            )
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["region"] == "us-east-1"
        assert [r["instance_type"] for r in body["instance_types"]] == [
            "c7g.2xlarge",
            "m7i.xlarge",
        ]

    async def test_the_list_route_is_owner_only(self, tmp_path, fake_ec2):
        resp = await hcit.api_cloud_instance_types(
            _req(
                "GET",
                "/api/cloud/instance-types?region=us-east-1",
                state=_state(tmp_path),
                slack=True,
            )
        )
        assert resp.status == 403
        assert fake_ec2.calls == []

    async def test_the_list_route_refuses_a_bad_region(self, tmp_path, fake_ec2):
        resp = await hcit.api_cloud_instance_types(
            _req("GET", "/api/cloud/instance-types?region=nowhere", state=_state(tmp_path))
        )
        assert resp.status == 400
        assert fake_ec2.calls == []

    async def test_the_list_route_refuses_an_empty_region(self, tmp_path, fake_ec2):
        resp = await hcit.api_cloud_instance_types(
            _req("GET", "/api/cloud/instance-types", state=_state(tmp_path))
        )
        assert resp.status == 400
        assert fake_ec2.calls == []

    async def test_the_list_route_reports_an_aws_failure(self, tmp_path, fake_ec2):
        fake_ec2.fail = True
        resp = await hcit.api_cloud_instance_types(
            _req("GET", "/api/cloud/instance-types?region=us-east-1", state=_state(tmp_path))
        )
        assert resp.status == 502
        assert _body(resp)["code"] == "aws_call_failed"

    async def test_a_read_refused_in_an_agent_session_is_a_coded_403(self, tmp_path, monkeypatch):
        def refuse(*a, **k):
            raise aws.CloudActionDenied(
                "ec2 describe-instance-types is not allowed from an agent session"
            )

        monkeypatch.setattr(aws, "checked_json", refuse)
        resp = await hcit.api_cloud_instance_types(
            _req("GET", "/api/cloud/instance-types?region=us-east-1", state=_state(tmp_path))
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "cloud_action_denied"
        state = _state(tmp_path)
        body = {"region": "us-east-1", "size_key": "balanced", "instance_type": "m7i.xlarge"}
        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state, body=body)
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "cloud_action_denied"
        assert state.cloud_launch_store.list() == []

    async def test_a_launch_with_an_offered_type_carries_it_to_the_engine(self, tmp_path, fake_ec2):
        state = _state(tmp_path)
        body = {
            "profile": "dev",
            "region": "us-east-1",
            "size_key": "balanced",
            "instance_type": " m7i.xlarge ",
        }
        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state, body=body)
        )
        assert resp.status == 202, _body(resp)
        assert _body(resp)["instance_type"] == "m7i.xlarge"
        job = state.cloud_launch_store.get(_body(resp)["id"])
        assert job.instance_type == "m7i.xlarge"
        # The job names the size it launched, not the tier the form had selected.
        assert job.size_key == instance_types.CUSTOM_TIER_KEY
        assert state.cloud_launch_engine.seen["instance_type"] == "m7i.xlarge"

    @pytest.mark.parametrize("name", ["m5.large", "g5.xlarge", "m7g.large", "m7i.xlarge;id"])
    async def test_a_launch_with_a_type_the_region_does_not_offer_is_refused(
        self, tmp_path, fake_ec2, name
    ):
        state = _state(tmp_path)
        body = {
            "profile": "dev",
            "region": "us-east-1",
            "size_key": "balanced",
            "instance_type": name,
        }
        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state, body=body)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_instance_type"
        if name == "m7g.large":
            assert "Kiro Crew needs at least 16 GB memory" in _body(resp)["error"]
        assert state.cloud_launch_store.list() == []

    async def test_a_pinned_subnet_is_checked_against_the_custom_type(
        self, tmp_path, fake_ec2, monkeypatch
    ):
        checked: list = []
        monkeypatch.setattr(
            ec2,
            "resolve_explicit_subnet",
            lambda subnet_id, profile, region, instance_type="": checked.append(instance_type)
            or ("vpc-1", subnet_id, "igw"),
        )
        state = _state(tmp_path)
        body = {
            "profile": "dev",
            "region": "us-east-1",
            "size_key": "balanced",
            "subnet_id": "subnet-0123abcd",
            "instance_type": "m7i.xlarge",
        }
        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state, body=body)
        )
        assert resp.status == 202, _body(resp)
        assert checked == ["m7i.xlarge"]
        job = state.cloud_launch_store.get(_body(resp)["id"])
        assert job.instance_type == "m7i.xlarge"
        assert state.cloud_launch_engine.seen["instance_type"] == "m7i.xlarge"

    async def test_a_launch_without_a_type_reads_nothing_from_aws(self, tmp_path, fake_ec2):
        state = _state(tmp_path)
        body = {"profile": "dev", "region": "us-east-1", "size_key": "balanced"}
        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state, body=body)
        )
        assert resp.status == 202
        assert fake_ec2.calls == []
        assert state.cloud_launch_engine.seen["instance_type"] == ""


def test_the_job_keeps_its_instance_type_across_a_reload(tmp_path):
    store = lj.LaunchJobStore(root=tmp_path / "jobs")
    job = store.create(
        profile="p", region="us-east-1", size_key="balanced", instance_type="m7i.xlarge"
    )
    assert store.get(job.id).instance_type == "m7i.xlarge"
    assert store.get(job.id).size_key == instance_types.CUSTOM_TIER_KEY
    assert lj.LaunchJob.from_dict({"id": "x", "size_key": "balanced"}).instance_type == ""

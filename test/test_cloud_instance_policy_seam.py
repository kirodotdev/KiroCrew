"""The ``cloud_instance_policy`` seam: what an edition adds to an EC2 crew's role.

An organization that requires a host agent on every instance needs its managed
policy on the instance role AND room for it under the permissions boundary, or
the instance is non-compliant from boot. The public build adds nothing; an
edition supplies both lists. These tests pin the default (byte-identical to a
build without the seam), the widened documents, the validation floor, and the
admin upgrade that moves an existing account's boundary forward.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re

import pytest

from kiro_crew import cli_cloud
from kiro_crew.cloud import aws, ec2, host_policy, iam, sizes, source
from kiro_crew.platform.context import current_context, reset_context, set_context
from kiro_crew.platform.interfaces import CloudInstancePolicy

_ACCT = "123456789012"
_BOUNDARY = f"arn:aws:iam::{_ACCT}:policy/kirocrew-ec2-boundary"
_HOST_ARN = "arn:aws:iam::{account}:policy/HostAgent/HostMonitoring"
_STATEMENTS = (
    {
        "Sid": "HostAgentLogsWrite",
        "Effect": "Allow",
        "Action": ["s3:PutObject"],
        "Resource": "arn:aws:s3:::host-agent-logs-*",
        "Condition": {"StringEquals": {"s3:ResourceAccount": "111122223333"}},
    },
    {
        "Sid": "HostAgentMetrics",
        "Effect": "Allow",
        "Action": "cloudwatch:PutMetricData",
        "Resource": "*",
    },
)


class _Fixed:
    def __init__(self, policy: CloudInstancePolicy) -> None:
        self._policy = policy

    def instance_policy(self) -> CloudInstancePolicy:
        return self._policy


@pytest.fixture
def install_policy():
    def _install(policy: CloudInstancePolicy) -> None:
        set_context(dataclasses.replace(current_context(), cloud_instance_policy=_Fixed(policy)))

    yield _install
    reset_context()


@pytest.fixture
def host_agent(install_policy):
    install_policy(
        CloudInstancePolicy(managed_policy_arns=(_HOST_ARN,), boundary_statements=_STATEMENTS)
    )


def _attach_condition(doc: dict) -> object:
    stmt = next(s for s in doc["Statement"] if s["Sid"] == "IamAttachManagedPolicyForInstance")
    return stmt["Condition"]["ArnEquals"]["iam:PolicyARN"]


class TestDefaultAddsNothing:
    def test_boundary_is_the_base_document(self):
        assert iam.boundary_policy_document(_ACCT) == iam.base_boundary_policy_document(_ACCT)
        assert iam.previous_boundary_documents(_ACCT) == []
        assert iam.previous_kms_boundary_documents(_ACCT) == []

    def test_launcher_policy_attach_is_session_manager_alone(self):
        assert (
            _attach_condition(iam.policy_document())
            == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        )
        assert iam.host_policy_document() is None

    def test_argv_carries_no_extra_policies(self):
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=sizes.get_tier("balanced"),
            vpc_id="vpc-1",
            subnet_id="subnet-1",
            permissions_boundary_arn=_BOUNDARY,
        )
        assert not any(a.startswith("ExtraManagedPolicyArns=") for a in argv)


class TestEditionPolicy:
    def test_boundary_appends_the_statements_after_the_built_in_ones(self, host_agent):
        doc = iam.boundary_policy_document(_ACCT)
        assert [s["Sid"] for s in doc["Statement"]] == [
            "SsmCore",
            "SourceBucketRead",
            "HostAgentLogsWrite",
            "HostAgentMetrics",
        ]
        assert iam.previous_boundary_documents(_ACCT) == [iam.base_boundary_policy_document(_ACCT)]

    def test_kms_boundary_carries_them_too_and_keeps_kms_last(self, host_agent):
        sids = [s["Sid"] for s in iam.kms_boundary_policy_document(_ACCT)["Statement"]]
        assert sids[-1] == "SsmSessionKms"
        assert "HostAgentMetrics" in sids
        (old,) = iam.previous_kms_boundary_documents(_ACCT)
        assert [s["Sid"] for s in old["Statement"]] == [
            "SsmCore",
            "SourceBucketRead",
            "SsmSessionKms",
        ]

    def test_documents_are_copies_of_the_provider_statements(self, host_agent):
        iam.boundary_policy_document(_ACCT)["Statement"][2]["Action"].append("s3:DeleteObject")
        assert iam.boundary_policy_document(_ACCT)["Statement"][2]["Action"] == ["s3:PutObject"]

    def test_the_main_launcher_policy_is_unchanged(self, host_agent):
        """It sits a few dozen characters under IAM's 6,144 cap; the grant goes elsewhere."""
        assert (
            _attach_condition(iam.policy_document())
            == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        )
        assert len(json.dumps(iam.policy_document(), separators=(",", ":"))) <= 6144

    def test_the_addendum_attaches_only_on_the_boundary_gated_role_path(self, host_agent):
        doc = iam.host_policy_document()
        assert doc is not None
        (stmt,) = doc["Statement"]
        assert stmt["Action"] == ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        # Never LEGACY_ROLE_ARN: a root-path role may predate the boundary, and an
        # attached policy there would be uncapped.
        assert stmt["Resource"] == iam.INSTANCE_ROLE_ARN
        # Tag-gated: a role at this path created out-of-band, without the
        # boundary, carries no kirocrew:managed tag and cannot be given them.
        assert stmt["Condition"] == {
            "ArnEquals": {"iam:PolicyARN": ["arn:aws:iam::*:policy/HostAgent/HostMonitoring"]},
            "StringEquals": {"aws:ResourceTag/kirocrew:managed": "true"},
        }

    def test_argv_names_the_policy_in_the_launch_account(self, host_agent):
        arns = tuple(host_policy.managed_policy_arns(ec2._account_of_policy_arn(_BOUNDARY)))
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=sizes.get_tier("balanced"),
            vpc_id="vpc-1",
            subnet_id="subnet-1",
            permissions_boundary_arn=_BOUNDARY,
            extra_managed_policy_arns=arns,
        )
        value = f"arn:aws:iam::{_ACCT}:policy/HostAgent/HostMonitoring"
        assert f"ExtraManagedPolicyArns={value}" in argv
        assert _template_pattern("ExtraManagedPolicyArns").match(value)

    def test_a_provider_that_raises_is_refused_by_every_reader(self, install_policy):
        """An empty read would build the narrow boundary and attach nothing."""

        class _Broken:
            def instance_policy(self):
                raise RuntimeError("edition adapter failed")

        set_context(dataclasses.replace(current_context(), cloud_instance_policy=_Broken()))
        for read in (
            lambda: iam.boundary_policy_document(_ACCT),
            lambda: iam.kms_boundary_policy_document(_ACCT),
            lambda: iam.host_policy_document(),
            lambda: host_policy.managed_policy_arns(_ACCT),
        ):
            with pytest.raises(aws.AWSError, match="could not be read"):
                read()

    def test_a_provider_that_answers_no_descriptor_is_refused(self, install_policy):
        class _None:
            def instance_policy(self):
                return None

        set_context(dataclasses.replace(current_context(), cloud_instance_policy=_None()))
        with pytest.raises(aws.AWSError, match="not a CloudInstancePolicy"):
            host_policy.instance_policy()

    def test_a_composition_failure_is_not_turned_into_a_refusal(self, install_policy):
        from kiro_crew.platform.context import PlatformCompositionError

        class _Uncomposed:
            def instance_policy(self):
                raise PlatformCompositionError("companion did not compose")

        set_context(dataclasses.replace(current_context(), cloud_instance_policy=_Uncomposed()))
        with pytest.raises(PlatformCompositionError):
            host_policy.instance_policy()

    def test_a_provider_that_raises_refuses_the_launch(self, install_policy):
        class _Broken:
            def instance_policy(self):
                raise RuntimeError("edition adapter failed")

        set_context(dataclasses.replace(current_context(), cloud_instance_policy=_Broken()))
        with pytest.raises(aws.AWSError, match="could not be read"):
            ec2.deploy(tag="t1", tier=sizes.get_tier("balanced"), dry_run=True, ship_source=False)


def _template_pattern(name: str) -> re.Pattern[str]:
    block = re.search(rf"\n  {name}:\n((?:    .*\n)+)", ec2.load_template())
    assert block, f"template parameter {name} missing"
    m = re.search(r'AllowedPattern: "(.*)"', block.group(1))
    assert m, f"{name} has no AllowedPattern"
    return re.compile(m.group(1).replace("\\\\", "\\"))


class TestDeploy:
    def _stub(self, monkeypatch, deploy_result):
        monkeypatch.setattr(aws, "assert_human_action", lambda *_a: None)
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY)
        monkeypatch.setattr(source, "upload_source", lambda *a, **k: ("bucket-1", "t1/src.tgz"))
        monkeypatch.setattr(source, "delete_source", lambda *a, **k: None)
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
        monkeypatch.setattr(ec2, "assert_download_hosts_resolvable", lambda *a, **k: None)
        monkeypatch.setattr(
            ec2,
            "get_stack_failures",
            lambda *a, **k: [
                {
                    "resource": "InstanceRole",
                    "reason": "not authorized to perform: iam:AttachRolePolicy",
                }
            ],
        )
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: deploy_result)

    def _deploy(self):
        return ec2.deploy(
            tag="t1", tier=sizes.get_tier("balanced"), profile="dev", region="us-east-1"
        )

    def test_a_denied_host_policy_attach_names_the_second_policy(self, host_agent, monkeypatch):
        self._stub(monkeypatch, (255, "", "Failed to create/update the stack"))
        with pytest.raises(aws.AWSError, match="iam-policy --host-policies"):
            self._deploy()

    def test_without_host_policies_the_hint_is_not_added(self, monkeypatch):
        self._stub(monkeypatch, (255, "", "Failed to create/update the stack"))
        with pytest.raises(aws.AWSError) as exc:
            self._deploy()
        assert "--host-policies" not in str(exc.value)


class TestTemplate:
    def test_pattern_accepts_eight_and_refuses_nine_or_a_wildcard(self):
        pattern = _template_pattern("ExtraManagedPolicyArns")
        one = f"arn:aws:iam::{_ACCT}:policy/A/B"
        assert pattern.match("")
        assert pattern.match(",".join([one] * 8))
        assert not pattern.match(",".join([one] * 9))
        assert not pattern.match(f"arn:aws:iam::{_ACCT}:policy/*")
        assert not pattern.match("arn:aws:iam::*:policy/A")

    def test_role_attaches_session_manager_alone_unless_the_parameter_is_set(self):
        text = ec2.load_template()
        assert 'HasExtraManagedPolicies: !Not [!Equals [!Ref ExtraManagedPolicyArns, ""]]' in text
        role = text.split("\n  InstanceRole:\n", 1)[1].split("\n  SsmSessionKmsPolicy:", 1)[0]
        assert "ManagedPolicyArns: !If\n        - HasExtraManagedPolicies" in role
        assert (
            '!Sub "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore,${ExtraManagedPolicyArns}"'
            in role
        )
        assert "        - - arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" in role


@pytest.mark.parametrize(
    ("policy", "needle"),
    [
        (CloudInstancePolicy(managed_policy_arns=("arn:aws:iam::*:policy/A",)), "not a managed"),
        (
            CloudInstancePolicy(managed_policy_arns=("arn:aws:iam::aws:policy/A,B",)),
            "not a managed",
        ),
        (
            CloudInstancePolicy(
                managed_policy_arns=("arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",)
            ),
            "already attached",
        ),
        (CloudInstancePolicy(managed_policy_arns=("arn:aws:iam::aws:policy/A",) * 2), "twice"),
        (
            CloudInstancePolicy(
                managed_policy_arns=tuple(f"arn:aws:iam::aws:policy/P{i}" for i in range(9))
            ),
            "more than 8",
        ),
        (
            CloudInstancePolicy(boundary_statements=({**_STATEMENTS[1], "Effect": "Deny"},)),
            "must be an Allow",
        ),
        (
            CloudInstancePolicy(
                boundary_statements=(
                    {"Sid": "X", "Effect": "Allow", "NotAction": "s3:*", "Resource": "*"},
                )
            ),
            "unsupported keys",
        ),
        (
            CloudInstancePolicy(boundary_statements=({**_STATEMENTS[1], "Action": "*"},)),
            "names action",
        ),
        (
            CloudInstancePolicy(
                boundary_statements=({**_STATEMENTS[1], "Action": "iam:PassRole"},)
            ),
            "names action",
        ),
        (
            CloudInstancePolicy(
                boundary_statements=({**_STATEMENTS[1], "Action": ["STS:AssumeRole"]},)
            ),
            "names action",
        ),
        (
            CloudInstancePolicy(boundary_statements=({**_STATEMENTS[1], "Action": "*:*"},)),
            "names action",
        ),
        (
            CloudInstancePolicy(
                boundary_statements=({**_STATEMENTS[1], "Action": "?am:PassRole"},)
            ),
            "names action",
        ),
        (
            CloudInstancePolicy(
                managed_policy_arns=("arn:aws:iam::aws:policy/" + "a/" * 300 + "Name",)
            ),
            "not a managed",
        ),
        (
            CloudInstancePolicy(boundary_statements=({**_STATEMENTS[1], "Sid": "SsmCore"},)),
            "reserved or repeated",
        ),
        (
            CloudInstancePolicy(boundary_statements=(_STATEMENTS[1], _STATEMENTS[1])),
            "reserved or repeated",
        ),
        (
            CloudInstancePolicy(boundary_statements=({**_STATEMENTS[1], "Resource": []},)),
            "non-empty",
        ),
    ],
)
def test_an_invalid_descriptor_refuses_rather_than_trims(install_policy, policy, needle):
    install_policy(policy)
    with pytest.raises(aws.AWSError, match=needle):
        host_policy.instance_policy()
    with pytest.raises(aws.AWSError):
        iam.boundary_policy_document(_ACCT)


def test_an_invalid_descriptor_refuses_the_dry_run_too(install_policy):
    install_policy(CloudInstancePolicy(managed_policy_arns=("arn:aws:iam::*:policy/A",)))
    with pytest.raises(aws.AWSError, match="not a managed"):
        ec2.deploy(tag="t1", tier=sizes.get_tier("balanced"), dry_run=True, ship_source=False)


class _FakeIam:
    """A one-policy IAM: get-policy, get-policy-version, create-policy-version."""

    def __init__(self, document: dict | None, create_err: str = "") -> None:
        self.document = document
        self.create_err = create_err
        self.calls: list[list[str]] = []

    def __call__(self, args, *_a, **_k):
        self.calls.append(list(args))
        op = args[1]
        if self.document is None:
            return 254, "", "An error occurred (NoSuchEntity)"
        if op == "get-policy":
            return 0, json.dumps({"Policy": {"DefaultVersionId": "v1"}}), ""
        if op == "get-policy-version":
            return 0, json.dumps({"PolicyVersion": {"Document": self.document}}), ""
        if op == "create-policy-version":
            if self.create_err:
                return 254, "", self.create_err
            assert "--set-as-default" in args
            self.document = json.loads(args[args.index("--policy-document") + 1])
            return 0, "{}", ""
        raise AssertionError(f"unexpected call {args}")

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "create-policy-version"]


@pytest.fixture
def account(monkeypatch):
    monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT)
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)


_DENIED_VERSION = (
    "An error occurred (AccessDenied) when calling the CreatePolicyVersion operation: "
    "User is not authorized to perform: iam:CreatePolicyVersion"
)


class TestMigration:
    def test_a_launch_on_the_earlier_boundary_upgrades_it_when_allowed(
        self, host_agent, account, monkeypatch, capsys
    ):
        fake = _FakeIam(iam.base_boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        assert source.ensure_instance_boundary() == _BOUNDARY
        assert len(fake.writes()) == 1
        assert fake.document == iam.boundary_policy_document(_ACCT)
        assert f"Upgraded the permissions boundary '{iam.BOUNDARY_NAME}'" in capsys.readouterr().err

    def test_a_launch_denied_the_re_version_names_the_upgrade(
        self, host_agent, account, monkeypatch
    ):
        earlier = iam.base_boundary_policy_document(_ACCT)
        fake = _FakeIam(earlier, create_err=_DENIED_VERSION)
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="iam-boundary --upgrade"):
            source.ensure_instance_boundary()
        assert fake.document == earlier

    def test_a_launch_at_the_version_limit_names_the_upgrade(
        self, host_agent, account, monkeypatch
    ):
        earlier = iam.base_boundary_policy_document(_ACCT)
        fake = _FakeIam(
            earlier,
            create_err="An error occurred (LimitExceeded) when calling CreatePolicyVersion",
        )
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="iam-boundary --upgrade"):
            source.ensure_instance_boundary()
        assert fake.document == earlier

    def test_a_launch_from_an_agent_session_does_not_upgrade(
        self, host_agent, account, monkeypatch
    ):
        fake = _FakeIam(iam.base_boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "chat-1")
        with pytest.raises(aws.AWSError, match="iam-boundary --upgrade"):
            source.ensure_instance_boundary()
        assert fake.writes() == []

    def test_a_launch_on_a_hand_edited_boundary_writes_nothing(
        self, host_agent, account, monkeypatch
    ):
        edited = iam.base_boundary_policy_document(_ACCT)
        edited["Statement"][0]["Action"] = "*"
        fake = _FakeIam(edited)
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="does NOT match"):
            source.ensure_instance_boundary()
        assert fake.writes() == []

    def test_a_launch_on_a_current_boundary_writes_nothing(self, host_agent, account, monkeypatch):
        fake = _FakeIam(iam.boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        assert source.ensure_instance_boundary() == _BOUNDARY
        assert fake.writes() == []

    def test_upgrade_re_versions_the_earlier_document_and_a_launch_then_accepts_it(
        self, host_agent, account, monkeypatch
    ):
        fake = _FakeIam(iam.base_boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        assert source.upgrade_instance_boundary() == source.UPGRADE_UPGRADED
        assert len(fake.writes()) == 1
        assert fake.document == iam.boundary_policy_document(_ACCT)
        assert source.ensure_instance_boundary() == _BOUNDARY

    def test_upgrade_of_a_current_boundary_writes_nothing(self, host_agent, account, monkeypatch):
        fake = _FakeIam(iam.boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        assert source.upgrade_instance_boundary() == source.UPGRADE_CURRENT
        assert fake.writes() == []

    def test_upgrade_refuses_a_hand_edited_boundary(self, host_agent, account, monkeypatch):
        edited = iam.base_boundary_policy_document(_ACCT)
        edited["Statement"][0]["Action"] = "*"
        fake = _FakeIam(edited)
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="refusing to overwrite"):
            source.upgrade_instance_boundary()
        assert fake.writes() == []

    def test_without_edition_statements_there_is_nothing_to_upgrade_to(self, account, monkeypatch):
        edited = iam.base_boundary_policy_document(_ACCT)
        edited["Statement"].append(dict(_STATEMENTS[1]))
        fake = _FakeIam(edited)
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="refusing to overwrite"):
            source.upgrade_instance_boundary()
        assert fake.writes() == []

    def test_upgrade_says_what_to_do_at_the_version_limit(self, host_agent, account, monkeypatch):
        fake = _FakeIam(
            iam.base_boundary_policy_document(_ACCT),
            create_err="An error occurred (LimitExceeded) when calling CreatePolicyVersion",
        )
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="delete a non-default one"):
            source.upgrade_instance_boundary()

    def test_upgrade_of_a_missing_boundary_points_at_creating_it(
        self, host_agent, account, monkeypatch
    ):
        monkeypatch.setattr(aws, "run_aws", _FakeIam(None))
        with pytest.raises(aws.AWSError, match="kirocrew cloud iam-boundary"):
            source.upgrade_instance_boundary()

    def test_upgrade_refuses_when_the_seam_cannot_be_read(
        self, install_policy, account, monkeypatch
    ):
        """An empty fallback here would report an out-of-date boundary as current."""

        class _Broken:
            def instance_policy(self):
                raise RuntimeError("edition adapter failed")

        set_context(dataclasses.replace(current_context(), cloud_instance_policy=_Broken()))
        fake = _FakeIam(iam.base_boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="could not be read"):
            source.upgrade_instance_boundary()
        assert fake.calls == []

    def test_upgrade_is_refused_in_an_agent_session(self, host_agent, account, monkeypatch):
        fake = _FakeIam(iam.base_boundary_policy_document(_ACCT))
        monkeypatch.setattr(aws, "run_aws", fake)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "chat-1")
        with pytest.raises(aws.CloudActionDenied):
            source.upgrade_instance_boundary()
        assert fake.calls == []

    def test_a_kms_launch_on_the_earlier_kms_boundary_names_the_kms_upgrade(
        self, host_agent, account, monkeypatch
    ):
        fake = _FakeIam(
            iam._with_session_kms(iam.base_boundary_policy_document(_ACCT)),
            create_err=_DENIED_VERSION,
        )
        monkeypatch.setattr(aws, "run_aws", fake)
        with pytest.raises(aws.AWSError, match="iam-boundary --upgrade --ssm-kms"):
            source.ensure_instance_kms_boundary()

    def test_a_kms_launch_on_the_earlier_kms_boundary_upgrades_it_when_allowed(
        self, host_agent, account, monkeypatch
    ):
        fake = _FakeIam(iam._with_session_kms(iam.base_boundary_policy_document(_ACCT)))
        monkeypatch.setattr(aws, "run_aws", fake)
        assert source.ensure_instance_kms_boundary() == iam.kms_boundary_arn(_ACCT)
        (write,) = fake.writes()
        assert write[write.index("--policy-arn") + 1] == iam.kms_boundary_arn(_ACCT)
        assert fake.document == iam.kms_boundary_policy_document(_ACCT)

    def test_a_missing_kms_boundary_points_at_its_own_create_step(
        self, host_agent, account, monkeypatch
    ):
        monkeypatch.setattr(aws, "run_aws", _FakeIam(None))
        with pytest.raises(aws.AWSError, match="iam-boundary --ssm-kms"):
            source.upgrade_instance_boundary(kms=True)

    def test_kms_upgrade_targets_the_kms_boundary(self, host_agent, account, monkeypatch):
        fake = _FakeIam(iam._with_session_kms(iam.base_boundary_policy_document(_ACCT)))
        monkeypatch.setattr(aws, "run_aws", fake)
        assert source.upgrade_instance_boundary(kms=True) == source.UPGRADE_UPGRADED
        (write,) = fake.writes()
        assert write[write.index("--policy-arn") + 1] == iam.kms_boundary_arn(_ACCT)
        assert fake.document == iam.kms_boundary_policy_document(_ACCT)


class TestIamPolicyCli:
    def test_plain_form_prints_one_document_and_names_the_addendum(self, host_agent, capsys):
        assert cli_cloud._cloud_iam_policy(argparse.Namespace(host_policies=False)) == 0
        out = capsys.readouterr()
        assert json.loads(out.out) == iam.policy_document()
        assert "--host-policies" in out.err

    def test_host_policies_form_prints_the_addendum(self, host_agent, capsys):
        assert cli_cloud._cloud_iam_policy(argparse.Namespace(host_policies=True)) == 0
        assert json.loads(capsys.readouterr().out) == iam.host_policy_document()

    def test_without_edition_policies_there_is_no_addendum(self, capsys):
        assert cli_cloud._cloud_iam_policy(argparse.Namespace(host_policies=True)) == 1
        assert cli_cloud._cloud_iam_policy(argparse.Namespace(host_policies=False)) == 0
        assert "--host-policies" not in capsys.readouterr().err

    def test_an_invalid_descriptor_still_prints_the_main_policy(self, install_policy, capsys):
        install_policy(CloudInstancePolicy(managed_policy_arns=("arn:aws:iam::*:policy/A",)))
        assert cli_cloud._cloud_iam_policy(argparse.Namespace(host_policies=False)) == 1
        out = capsys.readouterr()
        assert json.loads(out.out) == iam.policy_document()
        assert "not a managed-policy ARN" in out.err


class TestCli:
    def _args(self, **kw) -> argparse.Namespace:
        return argparse.Namespace(profile="", region="", ssm_kms=False, upgrade=True, **kw)

    def test_upgrade_flag_upgrades_and_creates_nothing(self, monkeypatch):
        calls: list[bool] = []
        monkeypatch.setattr(cli_cloud, "_resolve", lambda a: ("p", "r"))
        monkeypatch.setattr(
            source,
            "upgrade_instance_boundary",
            lambda p, r, kms=False: calls.append(kms) or source.UPGRADE_UPGRADED,
        )
        monkeypatch.setattr(
            source, "ensure_instance_boundary", lambda *a: pytest.fail("must not create")
        )
        assert cli_cloud._cloud_iam_boundary(self._args()) == 0
        assert calls == [False]

    def test_a_refused_upgrade_exits_non_zero(self, monkeypatch):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda a: ("p", "r"))

        def _refuse(*_a, **_k):
            raise aws.AWSError("refusing to overwrite it")

        monkeypatch.setattr(source, "upgrade_instance_boundary", _refuse)
        assert cli_cloud._cloud_iam_boundary(self._args()) == 1

    def test_with_ssm_kms_both_boundaries_are_upgraded(self, monkeypatch):
        calls: list[bool] = []
        monkeypatch.setattr(cli_cloud, "_resolve", lambda a: ("p", "r"))
        monkeypatch.setattr(
            source,
            "upgrade_instance_boundary",
            lambda p, r, kms=False: calls.append(kms) or source.UPGRADE_CURRENT,
        )
        args = self._args()
        args.ssm_kms = True
        assert cli_cloud._cloud_iam_boundary(args) == 0
        assert calls == [False, True]

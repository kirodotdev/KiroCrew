"""`kirocrew cloud launch --ssm-kms-key`: opt-in KMS for encrypted Session Manager.

An org that pins a KMS key for Session Manager needs the on-box SSM agent to call
``kms:Decrypt`` on that key, and the default instance boundary caps it away. The
fix is opt-in: a second content-fixed boundary that only an admin creates, plus an
inline grant on exactly the passed key for this instance's own sessions. Without the flag, the launch argv, the
default boundary document and the template's default render are unchanged.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from kiro_crew import cli_cloud
from kiro_crew.cloud import aws, ec2, iam, sizes, wizard
from kiro_crew.validation import ValidationError

_KEY = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
_MRK = "arn:aws:kms:eu-west-1:111122223333:key/mrk-1234abcd12ab34cd56ef1234567890ab"
_BOUNDARY = "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary"
_KMS_BOUNDARY = "arn:aws:iam::123456789012:policy/kirocrew-ec2-kms-boundary"

# The default boundary document, written out literally. A change here changes the
# ceiling of every default launch and fails the content check of every existing
# install, so it is pinned literally rather than derived.
_DEFAULT_BOUNDARY_ACTIONS = [
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


def _default_boundary(account: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "SsmCore",
                "Effect": "Allow",
                "Action": list(_DEFAULT_BOUNDARY_ACTIONS),
                "Resource": "*",
            },
            {
                "Sid": "SourceBucketRead",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::kirocrew-src-{account}-*/*",
            },
        ],
    }


def _template() -> str:
    return ec2.load_template()


def _param_block(name: str) -> str:
    m = re.search(rf"\n  {name}:\n((?:    .*\n)+)", _template())
    assert m, f"template parameter {name} missing"
    return m.group(1)


def _allowed_pattern(name: str) -> re.Pattern[str]:
    m = re.search(r'AllowedPattern: "(.*)"', _param_block(name))
    assert m, f"{name} has no AllowedPattern"
    return re.compile(m.group(1).replace("\\\\", "\\"))


def _resource_block(name: str) -> str:
    """The body of one top-level Resources entry (its 4-space-indented lines)."""
    body = _template().split("\nResources:\n", 1)[1]
    lines = body.splitlines()
    start = lines.index(f"  {name}:")
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("    ") or not line.strip():
            out.append(line)
            continue
        break
    assert out, f"template resource {name} missing"
    return "\n".join(out)


class TestFlagOffIsUnchanged:
    def test_default_boundary_document_is_byte_identical(self):
        assert iam.boundary_policy_document("123456789012") == _default_boundary("123456789012")
        assert iam.boundary_policy_json("123456789012") == json.dumps(
            _default_boundary("123456789012")
        )

    def test_default_deploy_argv_carries_no_kms_override(self):
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=sizes.get_tier("balanced"),
            vpc_id="vpc-1",
            subnet_id="subnet-1",
            permissions_boundary_arn=_BOUNDARY,
        )
        assert not any(a.startswith("SsmKmsKeyArn=") for a in argv)
        assert f"PermissionsBoundaryArn={_BOUNDARY}" in argv

    def test_default_deploy_creates_only_the_default_boundary(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        calls: list[str] = []

        class _Stop(Exception):
            pass

        def _base(*_a):
            calls.append("base")
            raise _Stop

        monkeypatch.setattr(aws, "assert_human_action", lambda *_a: None)
        monkeypatch.setattr(ec2, "find_stack", lambda *_a: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", _base)
        monkeypatch.setattr(
            source_mod,
            "ensure_instance_kms_boundary",
            lambda *_a: pytest.fail("a default launch must not touch the KMS boundary"),
        )
        with pytest.raises(_Stop):
            ec2.deploy(
                tag="t1",
                tier=sizes.default_tier(),
                profile="dev",
                region="us-east-1",
                ship_source=True,
            )
        assert calls == ["base"]

    def test_template_default_adds_no_kms_grant(self):
        assert 'Default: ""' in _param_block("SsmKmsKeyArn")
        assert 'HasSsmKmsKey: !Not [!Equals [!Ref SsmKmsKeyArn, ""]]' in _template()
        # The grant is its own conditional resource; InstanceRole does not mention KMS.
        assert "Condition: HasSsmKmsKey" in _resource_block("SsmSessionKmsPolicy")
        assert "kms" not in _resource_block("InstanceRole").lower()

    def test_iam_boundary_step_without_flag_creates_only_the_three(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        made: list[str] = []
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        for fn in ("ensure_instance_boundary", "ensure_crew_boundary", "ensure_crew_exec_boundary"):
            monkeypatch.setattr(source_mod, fn, lambda *_a, _n=fn: made.append(_n) or "arn")
        monkeypatch.setattr(
            source_mod,
            "ensure_instance_kms_boundary",
            lambda *_a: pytest.fail("must not create the KMS boundary without --ssm-kms"),
        )
        assert cli_cloud._cloud_iam_boundary(argparse.Namespace(profile="", region="")) == 0
        assert made == [
            "ensure_instance_boundary",
            "ensure_crew_boundary",
            "ensure_crew_exec_boundary",
        ]


class TestKmsBoundary:
    def test_is_base_plus_exactly_one_ssm_scoped_statement(self):
        base = iam.boundary_policy_document("123456789012")
        kms = iam.kms_boundary_policy_document("123456789012")
        assert kms["Statement"][: len(base["Statement"])] == base["Statement"]
        extra = kms["Statement"][len(base["Statement"]) :]
        assert extra == [
            {
                "Sid": "SsmSessionKms",
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": "arn:aws:kms:*:*:key/*",
                "Condition": {
                    "Null": {
                        "kms:EncryptionContext:aws:ssm:SessionId": "false",
                        "kms:EncryptionContext:aws:ssm:TargetId": "false",
                    }
                },
            }
        ]

    def test_building_it_does_not_mutate_the_default(self):
        before = json.dumps(iam.boundary_policy_document("123456789012"))
        iam.kms_boundary_policy_document("123456789012")
        assert json.dumps(iam.boundary_policy_document("123456789012")) == before

    def test_name_and_arn(self):
        assert iam.KMS_BOUNDARY_NAME == "kirocrew-ec2-kms-boundary"
        assert iam.kms_boundary_arn("123456789012") == _KMS_BOUNDARY

    def test_ensure_routes_through_the_shared_verify_sequence(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        seen: dict = {}
        monkeypatch.setattr(source_mod, "_require_account", lambda *_a: "123456789012")
        monkeypatch.setattr(
            source_mod, "_ensure_boundary", lambda **kw: seen.update(kw) or kw["arn"]
        )
        assert source_mod.ensure_instance_kms_boundary("dev", "us-east-1") == _KMS_BOUNDARY
        assert seen["name"] == "kirocrew-ec2-kms-boundary"
        assert seen["expected"] == iam.kms_boundary_policy_document("123456789012")
        # A launch may not create it; the refusal names the admin step.
        assert seen["admin_step"] == "kirocrew cloud iam-boundary --ssm-kms"
        source_mod.ensure_instance_kms_boundary("dev", "us-east-1", create=True)
        assert seen["admin_step"] == ""

    def test_launch_refuses_an_absent_kms_boundary_without_creating_it(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        calls: list[list[str]] = []

        def _run(argv, *_a, **_k):
            calls.append(list(argv))
            if argv[:2] == ["iam", "get-policy"]:
                return 254, "", "An error occurred (NoSuchEntity) when calling GetPolicy"
            return pytest.fail(f"unexpected AWS call {argv[:2]}")

        monkeypatch.setattr(source_mod, "_require_account", lambda *_a: "123456789012")
        monkeypatch.setattr(aws, "run_aws", _run)
        with pytest.raises(aws.AWSError) as exc:
            source_mod.ensure_instance_kms_boundary("dev", "us-east-1")
        assert "kirocrew cloud iam-boundary --ssm-kms" in str(exc.value)
        assert [c[:2] for c in calls] == [["iam", "get-policy"]]

    def test_an_existing_kms_boundary_is_verified_before_use(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        verified: list[str] = []
        monkeypatch.setattr(source_mod, "_require_account", lambda *_a: "123456789012")
        monkeypatch.setattr(aws, "run_aws", lambda argv, *a, **k: (0, "{}", ""))
        monkeypatch.setattr(
            source_mod,
            "_verify_boundary_content",
            lambda arn, name, expected, *_a: verified.append(name),
        )
        assert source_mod.ensure_instance_kms_boundary("dev", "us-east-1") == _KMS_BOUNDARY
        assert verified == ["kirocrew-ec2-kms-boundary"]

    def test_template_boundary_pattern_accepts_exactly_the_two_names(self):
        pat = _allowed_pattern("PermissionsBoundaryArn")
        assert pat.fullmatch(_BOUNDARY) and pat.fullmatch(_KMS_BOUNDARY)
        for bad in (
            "arn:aws:iam::123456789012:policy/kirocrew-ec2-admin-boundary",
            "arn:aws:iam::123456789012:policy/kirocrew-ec2-kms-boundaryx",
            "arn:aws:iam::123456789012:policy/AdministratorAccess",
        ):
            assert not pat.fullmatch(bad), bad


class TestRoleGrant:
    def test_inline_policy_names_only_the_passed_key_via_ssm(self):
        block = _resource_block("SsmSessionKmsPolicy")
        assert "Type: AWS::IAM::RolePolicy" in block
        assert "RoleName: !Ref InstanceRole" in block
        assert "Action: kms:Decrypt\n" in block
        assert "GenerateDataKey" not in block
        assert "Resource: !Ref SsmKmsKeyArn" in block
        # The agent calls KMS itself, so the request has no kms:ViaService; the
        # Session Manager encryption context is the scope, pinned to this stack's
        # own instance.
        assert "kms:EncryptionContext:aws:ssm:TargetId: !Ref Instance" in block
        assert 'kms:EncryptionContext:aws:ssm:SessionId: "false"' in block
        assert "ViaService" not in block
        assert "*" not in block

    def test_flag_on_argv_passes_key_and_kms_boundary(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(aws, "assert_human_action", lambda *_a: None)
        monkeypatch.setattr(ec2, "find_stack", lambda *_a: None)
        monkeypatch.setattr(
            source_mod,
            "ensure_instance_boundary",
            lambda *_a: pytest.fail("a --ssm-kms-key launch uses the KMS boundary"),
        )
        monkeypatch.setattr(source_mod, "ensure_instance_kms_boundary", lambda *_a: _KMS_BOUNDARY)
        monkeypatch.setattr(ec2, "resolve_explicit_subnet", lambda *a: ("vpc-1", "subnet-1", "igw"))
        monkeypatch.setattr(ec2, "assert_download_hosts_resolvable", lambda *a: None)
        seen: dict = {}

        def _run(argv, *a, **k):
            seen["argv"] = argv
            return 0, "", ""

        monkeypatch.setattr(aws, "run_aws", _run)
        monkeypatch.setattr(ec2, "get_stack_failures", lambda *a: [])
        try:
            ec2.deploy(
                tag="t1",
                tier=sizes.default_tier(),
                profile="dev",
                region="us-east-1",
                subnet_id="subnet-0123456789abcdef0",
                ship_source=False,
                ref="v1",
                ssm_kms_key_arn=_KEY,
            )
        except Exception:  # noqa: BLE001 - post-deploy reads are not under test
            pass
        argv = seen["argv"]
        assert f"SsmKmsKeyArn={_KEY}" in argv
        assert f"PermissionsBoundaryArn={_KMS_BOUNDARY}" in argv


class TestValidation:
    @pytest.mark.parametrize(
        "arn",
        [
            _KEY,
            _MRK,
        ],
    )
    def test_valid_key_arn(self, arn):
        assert ec2.validate_ssm_kms_key_arn(arn) == arn
        assert _allowed_pattern("SsmKmsKeyArn").fullmatch(arn)

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:kms:us-east-1:111122223333:alias/aws/ssm",
            # Other partitions: the ceiling is an arn:aws ARN, so they would be
            # capped to nothing.
            "arn:aws-us-gov:kms:us-gov-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "arn:aws-cn:kms:cn-north-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "alias/ssm",
            "arn:aws:kms:us-east-1:111122223333:key/*",
            "arn:aws:kms:*:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "arn:aws:kms:us-east-1:*:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "*",
            "1234abcd-12ab-34cd-56ef-1234567890ab",
            "arn:aws:s3:::bucket/key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "arn:aws:iam::111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            _KEY + ";x",
            _KEY + "\nx",
            "",
        ],
    )
    def test_bad_arn_is_refused(self, arn):
        with pytest.raises(ValidationError):
            ec2.validate_ssm_kms_key_arn(arn)
        if arn:
            assert not _allowed_pattern("SsmKmsKeyArn").fullmatch(arn), arn

    def test_deploy_refuses_a_bad_arn_before_any_aws_call(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("must not hit AWS"))
        with pytest.raises(ValidationError):
            ec2.deploy(
                tag="t1",
                tier=sizes.default_tier(),
                ssm_kms_key_arn="arn:aws:kms:us-east-1:111122223333:alias/ssm",
                dry_run=True,
            )

    def test_cli_refuses_a_bad_arn_before_the_wizard(self, monkeypatch):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
        monkeypatch.setattr(
            cli_cloud.wizard, "launch", lambda **kw: pytest.fail("wizard must not run")
        )
        ns = argparse.Namespace(profile="", region="", yes=True, ssm_kms_key="key/*")
        assert cli_cloud._cloud_launch(ns) == 2


class TestCliThreading:
    def test_launch_passes_the_key_to_the_wizard(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
        monkeypatch.setattr(cli_cloud.wizard, "launch", lambda **kw: captured.update(kw) or 0)
        ns = argparse.Namespace(profile="", region="", yes=True, ssm_kms_key=_KEY)
        assert cli_cloud._cloud_launch(ns) == 0
        assert captured["ssm_kms_key_arn"] == _KEY

    def test_launch_without_flag_passes_empty(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
        monkeypatch.setattr(cli_cloud.wizard, "launch", lambda **kw: captured.update(kw) or 0)
        assert cli_cloud._cloud_launch(argparse.Namespace(profile="", region="", yes=True)) == 0
        assert captured["ssm_kms_key_arn"] == ""

    def test_wizard_threads_the_key_into_deploy(self, monkeypatch):
        seen: dict = {}

        def fake_deploy(**kw):
            seen.update(kw)
            return ec2.DeployResult(tag="t1", stack_name="s", region="us-east-1", status="ok")

        monkeypatch.setattr(wizard.ec2, "deploy", fake_deploy)
        monkeypatch.setattr(wizard, "_stream_progress", lambda *a, **k: None)
        wizard._deploy_with_progress(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="us-east-1",
            ssm_kms_key_arn=_KEY,
        )
        assert seen["ssm_kms_key_arn"] == _KEY

    def test_iam_boundary_flag_adds_the_kms_boundary(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        made: list[str] = []
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        for fn in (
            "ensure_instance_boundary",
            "ensure_crew_boundary",
            "ensure_crew_exec_boundary",
            "ensure_instance_kms_boundary",
        ):
            monkeypatch.setattr(
                source_mod, fn, lambda *_a, _n=fn, **kw: made.append((_n, kw)) or "arn"
            )
        ns = argparse.Namespace(profile="", region="", ssm_kms=True)
        assert cli_cloud._cloud_iam_boundary(ns) == 0
        # The admin step is the one caller that may create it.
        assert made[-1] == ("ensure_instance_kms_boundary", {"create": True})
        assert len(made) == 4


class TestLauncherPolicy:
    def _st(self, sid: str) -> dict:
        return next(s for s in iam.policy_document()["Statement"] if s["Sid"] == sid)

    def test_create_role_lists_exactly_the_two_boundary_names(self):
        cond = self._st("IamCreateRoleWithBoundary")["Condition"]
        assert cond == {
            "ArnLike": {
                "iam:PermissionsBoundary": [
                    "arn:aws:iam::*:policy/kirocrew-ec2-boundary",
                    "arn:aws:iam::*:policy/kirocrew-ec2-kms-boundary",
                ]
            }
        }

    def test_the_launcher_reads_but_can_never_create_the_kms_boundary(self):
        # A launcher-held CreatePolicy on this name would leave it claimable in
        # every account that never opts in: a leaked credential could author a
        # permissive policy there and create a role capped by it.
        kms = "arn:aws:iam::*:policy/kirocrew-ec2-kms-boundary"
        assert self._st("IamKmsBoundaryRead") == {
            "Sid": "IamKmsBoundaryRead",
            "Effect": "Allow",
            "Action": ["iam:GetPolicy", "iam:GetPolicyVersion"],
            "Resource": kms,
        }
        assert kms not in self._st("IamInstanceBoundaryCreateOnce")["Resource"]
        for st in iam.policy_document()["Statement"]:
            if st["Effect"] != "Allow" or "iam:CreatePolicy" not in st["Action"]:
                continue
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            assert kms not in resources and "*" not in resources, st["Sid"]

    def test_no_kms_or_policy_version_verbs_for_the_launcher(self):
        actions = {a for s in iam.policy_document()["Statement"] for a in s["Action"]}
        assert not any(a.startswith("kms:") for a in actions)
        for verb in (
            "iam:CreatePolicyVersion",
            "iam:DeletePolicyVersion",
            "iam:DeletePolicy",
            "iam:SetDefaultPolicyVersion",
            "iam:PutRolePermissionsBoundary",
            "iam:DeleteRolePermissionsBoundary",
        ):
            assert verb not in actions

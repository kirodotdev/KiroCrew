"""`kirocrew cloud launch --ami` / `--extra-packages`: additive launch customization.

Covers the CLI flags, their validation (the values reach the root UserData
script, so the charset is an allowlist), the template parameters they fill, and
that a launch WITHOUT either flag produces exactly the stock deploy argv.
"""

from __future__ import annotations

import argparse
import re

import pytest

from kiro_crew import cli, cli_cloud
from kiro_crew.cloud import aws, ec2, sizes, wizard
from kiro_crew.validation import ValidationError

_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary"


def _param_block(name: str) -> str:
    text = ec2.load_template()
    m = re.search(rf"\n  {name}:\n((?:    .*\n)+)", text)
    assert m, f"template parameter {name} missing"
    return m.group(1)


def _allowed_pattern(name: str) -> re.Pattern[str]:
    m = re.search(r'AllowedPattern: "(.*)"', _param_block(name))
    assert m, f"{name} has no AllowedPattern"
    # YAML double-quoted scalar: "\\" is one backslash.
    return re.compile(m.group(1).replace("\\\\", "\\"))


class TestStockLaunchUnchanged:
    def test_stock_deploy_argv_is_byte_identical(self):
        # The exact argv a launch with no customization flags produced before
        # these flags existed. Any drift here changes every stock launch.
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="vpc-1",
            subnet_id="subnet-1",
            permissions_boundary_arn=_BOUNDARY_ARN,
        )
        assert argv == [
            "cloudformation",
            "deploy",
            "--stack-name",
            "kirocrew-t1",
            "--template-file",
            str(ec2._template_path()),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--tags",
            "kirocrew:managed=true",
            "kirocrew:instance=t1",
            "--parameter-overrides",
            f"InstanceType={tier.instance_type}",
            f"Architecture={tier.arch}",
            f"VolumeSizeGb={tier.disk_gb}",
            "VpcId=vpc-1",
            "SubnetId=subnet-1",
            "AssociatePublicIp=true",
            "StackTag=t1",
            f"PermissionsBoundaryArn={_BOUNDARY_ARN}",
        ]

    def test_stock_dry_run_carries_no_customization_overrides(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(source_mod, "find_repo_root", lambda: object())
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run hit AWS"))
        r = ec2.deploy(
            tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1", dry_run=True
        )
        assert not any(a.startswith(("CustomAmiId=", "ExtraPackages=")) for a in r.argv)

    def test_template_defaults_keep_the_stock_ami_and_package_set(self):
        assert 'Default: ""' in _param_block("CustomAmiId")
        assert 'Default: ""' in _param_block("ExtraPackages")
        text = ec2.load_template()
        # The stock AMI is still the SSM-resolved Amazon Linux 2023 image.
        assert 'HasCustomAmi: !Not [!Equals [!Ref CustomAmiId, ""]]' in text
        assert "{{resolve:ssm:${Param}}}" in text
        # An empty ExtraPackages makes the install loop run zero times.
        assert "for p in ${ExtraPackages}; do" in text


class TestDeployArgvCustomization:
    def test_ami_and_packages_reach_the_overrides(self):
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=sizes.get_tier("balanced"),
            vpc_id="v",
            subnet_id="s",
            permissions_boundary_arn=_BOUNDARY_ARN,
            ami_id="ami-0123456789abcdef0",
            extra_packages="gh jq",
        )
        assert "CustomAmiId=ami-0123456789abcdef0" in argv
        assert "ExtraPackages=gh jq" in argv

    def test_deploy_dry_run_validates_and_passes_through(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(source_mod, "find_repo_root", lambda: object())
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run hit AWS"))
        r = ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="us-east-1",
            ami_id="ami-0abc1234",
            extra_packages="gh, jq",
            dry_run=True,
        )
        assert "CustomAmiId=ami-0abc1234" in r.argv
        assert "ExtraPackages=gh jq" in r.argv

    def test_deploy_refuses_a_bad_package_before_any_aws_call(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("must not hit AWS"))
        with pytest.raises(ValidationError):
            ec2.deploy(
                tag="t1",
                tier=sizes.default_tier(),
                extra_packages="gh;reboot",
                dry_run=True,
            )


class TestValidation:
    @pytest.mark.parametrize("ami", ["ami-0abc1234", "ami-0123456789abcdef0"])
    def test_valid_ami(self, ami):
        assert ec2.validate_ami_id(ami) == ami

    @pytest.mark.parametrize("ami", ["ami-xyz", "ami-0ABC1234", "0abc1234", "ami-0abc1234;x", ""])
    def test_invalid_ami(self, ami):
        with pytest.raises(ValidationError):
            ec2.validate_ami_id(ami)

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("gh", "gh"),
            ("gh,jq", "gh jq"),
            (" gh , jq  ripgrep ", "gh jq ripgrep"),
            ("python3.12-devel,gcc-c++", "python3.12-devel gcc-c++"),
            ("gh,gh", "gh"),
            ("", ""),
        ],
    )
    def test_normalize_packages(self, raw, want):
        assert ec2.normalize_extra_packages(raw) == want

    @pytest.mark.parametrize(
        "raw",
        [
            "gh;reboot",
            "gh && curl x",
            "$(id)",
            "`id`",
            "gh|sh",
            "--nogpgcheck",
            "-y",
            "pkg'x",
            'pkg"x',
            "pkg\\x",
            "pkg/x",
            "pkg*",
            "pkg\nreboot",
            "päckage",
        ],
    )
    def test_injection_shaped_names_are_refused(self, raw):
        with pytest.raises(ValidationError):
            ec2.normalize_extra_packages(raw)

    def test_total_length_cap(self):
        long_names = ",".join(c * 90 for c in "abc")
        assert len(long_names) > ec2.EXTRA_PACKAGES_MAX_LEN
        with pytest.raises(ValidationError):
            ec2.normalize_extra_packages(long_names)

    def test_template_patterns_mirror_the_cli(self):
        # A direct `aws cloudformation deploy` bypasses the CLI, so the template
        # itself must refuse what the CLI refuses.
        pkg = _allowed_pattern("ExtraPackages")
        assert pkg.fullmatch("")
        assert pkg.fullmatch("gh jq python3.12-devel gcc-c++")
        for bad in ("gh;reboot", "$(id)", "-y", "gh  jq", " gh", "gh\nreboot", "gh|sh"):
            assert not pkg.fullmatch(bad), bad
        assert f"MaxLength: {ec2.EXTRA_PACKAGES_MAX_LEN}" in _param_block("ExtraPackages")
        ami = _allowed_pattern("CustomAmiId")
        assert ami.fullmatch("") and ami.fullmatch("ami-0123456789abcdef0")
        assert not ami.fullmatch("ami-0abc;x")


class TestUserDataSizeWithPackages:
    def test_worst_case_packages_fit_under_the_ceiling(self):
        # The existing size guard renders every !Sub variable at its worst
        # case; ExtraPackages must have an entry sized to its MaxLength.
        from test_cloud_ec2 import TestUserDataSize

        guard = TestUserDataSize()
        assert len(guard._WORST_CASE["ExtraPackages"]) >= ec2.EXTRA_PACKAGES_MAX_LEN
        guard.test_expanded_userdata_stays_under_ceiling()


class TestCliFlags:
    def _parse(self, *argv: str) -> argparse.Namespace:
        import sys
        from unittest.mock import patch

        with (
            patch.object(sys, "argv", ["kirocrew", "cloud", "launch", *argv]),
            patch("kiro_crew.cli.handle_cloud", return_value=0) as h,
        ):
            with pytest.raises(SystemExit):
                cli.main()
            h.assert_called_once()
            return h.call_args[0][0]

    def test_flags_parse(self):
        ns = self._parse("--ami", "ami-0abc1234", "--extra-packages", "gh,jq")
        assert ns.ami == "ami-0abc1234"
        assert ns.extra_packages == "gh,jq"

    def test_flags_default_empty(self):
        ns = self._parse()
        assert ns.ami == "" and ns.extra_packages == ""

    def test_launch_passes_flags_to_wizard(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
        monkeypatch.setattr(cli_cloud.wizard, "launch", lambda **kw: captured.update(kw) or 0)
        ns = argparse.Namespace(
            profile="", region="", yes=True, ami="ami-0abc1234", extra_packages="gh, jq"
        )
        assert cli_cloud._cloud_launch(ns) == 0
        assert captured["ami_id"] == "ami-0abc1234"
        assert captured["extra_packages"] == "gh jq"

    def test_launch_refuses_bad_values_before_the_wizard(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
        monkeypatch.setattr(
            cli_cloud.wizard, "launch", lambda **kw: pytest.fail("wizard must not run")
        )
        ns = argparse.Namespace(profile="", region="", yes=True, ami="", extra_packages="gh;id")
        assert cli_cloud._cloud_launch(ns) == 2
        ns = argparse.Namespace(profile="", region="", yes=True, ami="ami-bad", extra_packages="")
        assert cli_cloud._cloud_launch(ns) == 2

    def test_wizard_threads_values_into_deploy(self, monkeypatch):
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
            ami_id="ami-0abc1234",
            extra_packages="gh",
        )
        assert seen["ami_id"] == "ami-0abc1234"
        assert seen["extra_packages"] == "gh"

    @pytest.mark.parametrize(
        ("kw", "flag"),
        [({"ami_id": "ami-0abc1234"}, "--ami"), ({"extra_packages": "gh"}, "--extra-packages")],
    )
    def test_existing_stack_refuses_creation_only_flags_under_yes(
        self, monkeypatch, capsys, kw, flag
    ):
        # The image and package set are fixed at creation; a script that asked
        # for them must not silently get the old box.
        from kiro_crew.cloud.launch_state import LaunchState

        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.iam,
            "reachability_check",
            lambda *a, **k: {
                "reachable": True,
                "account": "1",
                "ec2_reachable": True,
                "cloudformation_reachable": True,
                "ssm_reachable": True,
            },
        )
        monkeypatch.setattr(wizard, "_ensure_session_manager_plugin", lambda **k: True)
        monkeypatch.setattr(wizard, "_select_existing_launch", lambda *a, **k: object())
        monkeypatch.setattr(
            wizard, "_deploy_with_progress", lambda **k: pytest.fail("must not deploy")
        )
        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, **kw)
        assert rc == 1
        assert f"{flag} cannot apply to the existing stack" in capsys.readouterr().out

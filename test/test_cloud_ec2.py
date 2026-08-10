"""Unit tests for the EC2 lifecycle engine (cloud/ec2.py).

All AWS I/O is mocked at the cloud.aws chokepoint (run_aws / checked / checked_json).
"""

from __future__ import annotations

import json
import re

import pytest

from kiro_crew.cloud import aws, ec2, sizes
from kiro_crew.validation import ValidationError


class TestSubTemplateSyntax:
    def test_every_sub_variable_is_legal(self):
        # The UserData is one big CloudFormation !Sub. Every "${...}" is parsed as a
        # Sub reference -- INCLUDING ones inside bash "#" comments, which are still part
        # of the Sub string. Each must be the "${!...}" literal escape or a plausible
        # reference: an AWS:: pseudo-param, or an identifier starting with a letter
        # (a Parameter, a Resource logical id, a Sub variable-map key, optionally with
        # a ".Attribute"). A bare "${...}" ellipsis in a comment matches neither and
        # broke change-set creation twice, so it must fail here.
        text = ec2.load_template()
        ref = re.compile(r"AWS::[A-Za-z0-9]+|[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*\Z")
        bad = [
            inner
            for inner in re.findall(r"\$\{([^}]*)\}", text)
            if not inner.startswith("!") and not ref.match(inner)
        ]
        assert not bad, f"illegal !Sub variable(s): {bad}"


class TestValidation:
    def test_valid_tag(self):
        assert ec2.validate_tag("kirocrew-7f3a") == "kirocrew-7f3a"

    def test_empty_tag_rejected(self):
        with pytest.raises(ValidationError):
            ec2.validate_tag("")

    def test_tag_with_bad_chars_rejected(self):
        with pytest.raises(ValidationError):
            ec2.validate_tag("bad;rm -rf")

    def test_tag_length_capped_for_iam_role_name(self):
        # kirocrew-ec2-<tag> must fit IAM's 64-char role-name limit; 13-char
        # prefix + tag <= 64 => tag <= 51.
        assert len("kirocrew-ec2-") + 51 == 64
        ec2.validate_tag("a" * 51)  # ok
        with pytest.raises(ValidationError):
            ec2.validate_tag("a" * 52)

    def test_region_pattern(self):
        assert ec2.validate_region("us-east-1") == "us-east-1"
        with pytest.raises(ValidationError):
            ec2.validate_region("not a region")

    def test_stack_name(self):
        assert ec2.stack_name("abc") == "kirocrew-abc"

    def test_cidr_valid(self):
        assert ec2._validate_cidr("1.2.3.4/32") == "1.2.3.4/32"
        assert ec2._validate_cidr("10.1.0.0/16") == "10.1.0.0/16"
        assert ec2._validate_cidr("") == ""

    def test_cidr_host_bits_normalized(self):
        # A CIDR with host bits set is normalized to its canonical network so the
        # SG ingress rule is unambiguous (1.2.3.4/24 -> 1.2.3.0/24), not passed
        # through raw.
        assert ec2._validate_cidr("1.2.3.4/24") == "1.2.3.0/24"
        assert ec2._validate_cidr("192.168.5.77/32") == "192.168.5.77/32"  # /32 unchanged

    def test_cidr_out_of_range_rejected(self):
        # charset-shaped but invalid octets/mask — must fail early, not at deploy
        with pytest.raises(ValidationError):
            ec2._validate_cidr("999.999.999.999/99")
        with pytest.raises(ValidationError):
            ec2._validate_cidr("1.2.3.4/40")

    def test_cidr_bad_charset_rejected(self):
        with pytest.raises(ValidationError):
            ec2._validate_cidr("1.2.3.4/32; rm -rf")

    def test_cidr_wider_than_slash16_hard_refused(self):
        # SSH to a personal box should be your own IP; wider than /16 is refused.
        for cidr in ("0.0.0.0/0", "128.0.0.0/1", "16.0.0.0/7", "10.0.0.0/8", "10.0.0.0/15"):
            with pytest.raises(ValidationError):
                ec2._validate_cidr(cidr)

    def test_cidr_wide_range_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.cloud.ec2"):
            assert ec2._validate_cidr("10.1.0.0/16") == "10.1.0.0/16"  # accepted, warned
            # Host bits are normalized away: 10.1.2.0/20 -> 10.1.0.0/20 (the
            # canonical network for that range), so the SG rule is unambiguous.
            assert ec2._validate_cidr("10.1.2.0/20") == "10.1.0.0/20"
        assert sum("wide range" in r.message for r in caplog.records) == 2
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.cloud.ec2"):
            ec2._validate_cidr("192.168.1.0/24")  # /24+ is fine, no warning
            ec2._validate_cidr("1.2.3.4/32")
        assert not caplog.records

    def test_repo_ref_charset(self):
        from kiro_crew.validation import validate_field

        assert validate_field("https://github.com/x/y.git", ec2._REPO_SPEC)
        assert validate_field("main", ec2._REF_SPEC)
        with pytest.raises(ValidationError):
            validate_field("x'; rm -rf /", ec2._REF_SPEC)


class TestTemplate:
    def test_template_loads_and_has_key_resources(self):
        text = ec2.load_template()
        assert "AWSTemplateFormatVersion" in text
        assert "AWS::EC2::Instance" in text
        assert "AWS::CloudFormation::WaitCondition" in text
        assert "AmazonSSMManagedInstanceCore" in text
        # resolve:ssm AMI alias, not a hardcoded AMI id
        assert "resolve:ssm" in text
        # No hardcoded AMI id like ami-0abc123... (the alias path contains the
        # literal "ami-amazon-linux-latest", which is fine).
        import re as _re

        assert not _re.search(r"ami-[0-9a-f]{8,}", text)

    def test_source_read_grant_pinned_to_derived_arn_not_params(self):
        # The instance role's INLINE SourceObjectRead s3:GetObject must be scoped
        # to the DERIVED launcher path, NOT the user-controlled SourceBucket/
        # SourceKey params — otherwise a caller could grant the box read on an
        # arbitrary S3 object and exfiltrate it. Guards against a regression back
        # to ${SourceBucket}/${SourceKey}. (The per-launch boundary that also
        # carried this derived ARN is gone — the shared boundary now covers the
        # whole account bucket prefix, safe because it only CAPS; the inline
        # policy below is the one that actually pins the single object.)
        text = ec2.load_template()
        derived = (
            "arn:aws:s3:::kirocrew-src-${AWS::AccountId}-${AWS::Region}"
            "/${StackTag}/kirocrew-src.tar.gz"
        )
        # Only the inline SourceObjectRead policy uses the derived ARN now.
        assert text.count(derived) == 1
        # And NO s3:GetObject Resource references the raw params.
        assert "arn:aws:s3:::${SourceBucket}/${SourceKey}" not in text

    def test_boundary_is_referenced_by_param_not_created_per_launch(self):
        # The permissions boundary must NO LONGER be an in-template
        # AWS::IAM::ManagedPolicy created per launch (that was the self-authorship
        # hole). Instead the InstanceRole references the pre-created shared
        # boundary via the PermissionsBoundaryArn parameter.
        text = ec2.load_template()
        # No per-launch managed-policy boundary resource remains.
        assert "InstanceBoundary:" not in text
        assert "kirocrew-ec2-boundary-${StackTag}" not in text
        # The role references the boundary by the new parameter.
        assert "PermissionsBoundaryArn:" in text
        assert "PermissionsBoundary: !Ref PermissionsBoundaryArn" in text

    def test_permissions_boundary_arn_param_pattern(self):
        # The PermissionsBoundaryArn param must carry an AllowedPattern (like the
        # other ARN params) matching exactly the fixed shared boundary name, so a
        # direct `aws cloudformation deploy` can't point it at an arbitrary
        # (permissive) policy.
        import re as _re

        text = ec2.load_template()
        block = _re.search(r"  PermissionsBoundaryArn:\n(?:    .+\n|    #.+\n)+", text)
        assert block, "PermissionsBoundaryArn param missing"
        assert "AllowedPattern" in block.group(0)
        assert "kirocrew-ec2-boundary" in block.group(0)

    def test_userdata_params_have_allowed_patterns(self):
        # Every string parameter that flows into the root user-data script must
        # carry an AllowedPattern so a direct `aws cloudformation deploy`
        # (bypassing the CLI's FieldSpec validation) still rejects shell
        # metacharacters at template-validation time.
        import re as _re

        text = ec2.load_template()
        for param in ("SourceBucket", "SourceKey", "KirocrewRepo", "KirocrewRef", "AllowSshCidr"):
            block = _re.search(rf"  {param}:\n(?:    .+\n)+", text)
            assert block, f"parameter {param} missing"
            assert "AllowedPattern" in block.group(0), f"{param} lacks AllowedPattern"

    def test_stacktag_pattern_matches_cli_length_cap(self):
        # The template's StackTag AllowedPattern must cap at 51 (not 63) to mirror
        # the CLI _TAG_RE: the role name "kirocrew-ec2-${StackTag}" + IAM's 64-char
        # role-name limit => 13 + 51 = 64. A 52-63 char tag would otherwise pass
        # template validation on a direct deploy, then fail opaquely at role
        # creation. Keep this in lockstep with ec2._TAG_RE.
        import re as _re

        text = ec2.load_template()
        block = _re.search(r"  StackTag:\n(?:    .+\n)+(?:    #.+\n)*(?:    .+\n)*", text)
        assert block and "{1,51}" in block.group(0), "StackTag AllowedPattern must cap at {1,51}"
        assert "{1,63}" not in block.group(0)
        # The CLI cap it mirrors:
        assert ec2._TAG_RE.pattern == r"^[a-zA-Z0-9-]{1,51}$"

    def test_bootstrap_verifies_kiro_cli_before_success(self):
        # The install step tolerates a nonzero exit; the template must then
        # verify the binary and `fail` the WaitCondition if it's missing, so a
        # broken chat backend can't be signaled healthy.
        text = ec2.load_template()
        assert "command -v kiro-cli" in text
        assert 'fail "kiro-cli did not install' in text

    def test_bootstrap_verifies_dashboard_built_before_success(self):
        # install.sh treats a frontend build failure as non-fatal (legacy
        # fallback), so a cloud crew could reach CREATE_COMPLETE serving the
        # "not built" stub (HTTP 200, passes the health probe) with a pane that
        # never loads. The template must verify the built SPA exists and `fail`
        # the WaitCondition otherwise, so a failed build rolls the stack back.
        text = ec2.load_template()
        assert "src/kiro_crew/static/dist/index.html" in text
        assert 'fail "dashboard frontend build missing' in text
        # The failure reason must fold the real build error from the setup log, so it
        # is diagnosable even when the crew ran a cloned install.sh that did not itself
        # hard-fail (the default clone-of-main path).
        assert 'grep -aiE' in text and '"$LOG"' in text
        assert "Build errors:" in text

    def test_bootstrap_requires_the_frontend_build(self):
        # A cloud crew is useless without its dashboard, so the bootstrap must force
        # install.sh's frontend build to be fatal (it is a non-fatal warning by
        # default, for local CLI users) — which is what lets the install retry
        # actually re-run a transient first-boot build failure.
        text = ec2.load_template()
        assert "KIROCREW_REQUIRE_FRONTEND=1" in text

    def test_instance_enforces_imdsv2(self):
        text = ec2.load_template()
        assert "MetadataOptions" in text
        assert "HttpTokens: required" in text

    def test_sub_escape_for_shell_vars(self):
        # ${!tail_ctx} is CFN !Sub's escape syntax: it renders the literal
        # ${tail_ctx} into the bash script. Without the !, Sub would try to
        # resolve tail_ctx as a template parameter and fail at create-time.
        # This test guards against a well-meaning "fix" that removes the !.
        text = ec2.load_template()
        assert "${!tail_ctx}" in text

    def test_no_non_ascii_in_property_values(self):
        """EC2 rejects non-ASCII in values like GroupDescription — guard against it.

        Comment lines (starting with '#') may contain unicode; everything else
        (property values sent to AWS APIs) must be pure ASCII.
        """
        for i, line in enumerate(ec2.load_template().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assert line.isascii(), f"non-ASCII in template line {i}: {line!r}"


_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary"


class TestBuildDeployArgv:
    def test_core_argv(self):
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="vpc-1",
            subnet_id="subnet-1",
            permissions_boundary_arn=_BOUNDARY_ARN,
        )
        assert argv[:2] == ["cloudformation", "deploy"]
        assert "--stack-name" in argv and "kirocrew-t1" in argv
        assert "CAPABILITY_NAMED_IAM" in argv
        assert f"InstanceType={tier.instance_type}" in argv
        assert "Architecture=arm64" in argv
        assert "VpcId=vpc-1" in argv
        assert "SubnetId=subnet-1" in argv
        assert "StackTag=t1" in argv
        # the pre-created shared boundary ARN is passed to the template param
        assert f"PermissionsBoundaryArn={_BOUNDARY_ARN}" in argv
        # discovery tags applied to the stack
        assert "kirocrew:managed=true" in argv
        assert "kirocrew:instance=t1" in argv

    def test_source_params_included_when_set(self):
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="v",
            subnet_id="s",
            permissions_boundary_arn=_BOUNDARY_ARN,
            source_bucket="kirocrew-src-123-us-east-1",
            source_key="t1/kirocrew-src.tar.gz",
        )
        assert "SourceBucket=kirocrew-src-123-us-east-1" in argv
        assert "SourceKey=t1/kirocrew-src.tar.gz" in argv

    def test_ssh_cidr_and_repo_included_when_set(self):
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="v",
            subnet_id="s",
            permissions_boundary_arn=_BOUNDARY_ARN,
            repo="https://example.com/x.git",
            ref="dev",
            allow_ssh_cidr="1.2.3.4/32",
        )
        assert "KirocrewRepo=https://example.com/x.git" in argv
        assert "KirocrewRef=dev" in argv
        assert "AllowSshCidr=1.2.3.4/32" in argv

    def test_ssh_cidr_omitted_by_default(self):
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="v",
            subnet_id="s",
            permissions_boundary_arn=_BOUNDARY_ARN,
        )
        assert not any(a.startswith("AllowSshCidr=") for a in argv)


class TestDeployDryRun:
    def test_dry_run_returns_argv_without_aws(self, monkeypatch):
        # If run_aws is called during a dry run, fail loudly.
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.deploy(
            tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1", dry_run=True
        )
        assert r.dry_run is True
        assert r.status == "DRY_RUN"
        assert r.argv[:2] == ["cloudformation", "deploy"]
        assert "VpcId=<auto>" in r.argv
        # source-shipping placeholders present by default
        assert "SourceBucket=<auto>" in r.argv

    def test_dry_run_no_source_when_disabled(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            ship_source=False,
            dry_run=True,
        )
        assert not any(a.startswith("SourceBucket=") for a in r.argv)


class TestDeployShipsSource:
    def test_deploy_uploads_source_and_passes_params(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
        monkeypatch.setattr(
            source_mod,
            "upload_source",
            lambda tag, profile="", region="": (
                "kirocrew-src-1-us-east-1",
                f"{tag}/kirocrew-src.tar.gz",
            ),
        )
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1"))
        captured = {}

        def fake_run(argv, profile="", region="", *, timeout=ec2._DEPLOY_TIMEOUT, proc_sink=None):
            captured["argv"] = argv
            return (0, "ok", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"},
        )
        r = ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")
        assert "SourceBucket=kirocrew-src-1-us-east-1" in captured["argv"]
        assert "SourceKey=t1/kirocrew-src.tar.gz" in captured["argv"]
        # the pre-created shared boundary ARN flows into the deploy params
        assert f"PermissionsBoundaryArn={_BOUNDARY_ARN}" in captured["argv"]
        # git repo/ref suppressed when shipping source
        assert not any(a.startswith("KirocrewRepo=") for a in captured["argv"])
        assert r.instance_id == "i-1"


class TestDeployAbortsOnUnownedStack:
    def test_deploy_aborts_before_upload_on_name_collision(self, monkeypatch):
        # An untagged same-named stack -> find_stack raises -> deploy must abort
        # BEFORE uploading source or calling cloudformation deploy.
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(
            ec2,
            "find_stack",
            lambda *a, **k: (_ for _ in ()).throw(
                aws.AWSError("stack ... NOT tagged", action="cloudformation:DescribeStacks")
            ),
        )

        def _boom_upload(*a, **k):  # pragma: no cover - must not upload
            raise AssertionError("must not upload source when the stack is unowned")

        monkeypatch.setattr(source_mod, "upload_source", _boom_upload)
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: pytest.fail("must not call cloudformation deploy")
        )
        with pytest.raises(aws.AWSError, match="NOT tagged"):
            ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")


class TestDeployCleansSourceOnEarlyFailure:
    def test_network_discovery_failure_deletes_uploaded_source(self, monkeypatch):
        # upload_source runs BEFORE discover_network; a discovery failure must
        # not orphan the just-uploaded tarball in S3.
        import kiro_crew.cloud.source as source_mod

        deleted: list[str] = []
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
        monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "t1/k.tar.gz"))
        monkeypatch.setattr(source_mod, "delete_source", lambda tag, *a, **k: deleted.append(tag))
        monkeypatch.setattr(
            ec2,
            "discover_network",
            lambda *a, **k: (_ for _ in ()).throw(
                aws.AWSError("no default VPC", action="ec2:DescribeVpcs")
            ),
        )

        with pytest.raises(aws.AWSError):
            ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")
        assert deleted == ["t1"]


def _igw_route_table(subnet_ids):
    """A route table with an internet-gateway default route, associated to
    ``subnet_ids`` (explicit associations)."""
    return {
        "Routes": [
            {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-abc", "State": "active"}
        ],
        "Associations": [{"SubnetId": sid} for sid in subnet_ids],
    }


def _nat_route_table(subnet_ids):
    """A route table whose default route is a NAT gateway (private-subnet
    egress), associated to ``subnet_ids``."""
    return {
        "Routes": [
            {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-xyz", "State": "active"}
        ],
        "Associations": [{"SubnetId": sid} for sid in subnet_ids],
    }


class TestDiscoverNetwork:
    def test_prefers_default_vpc_and_public_subnet(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-default"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-private",
                            "MapPublicIpOnLaunch": False,
                            "AvailabilityZone": "us-east-1a",
                        },
                        {
                            "SubnetId": "subnet-public",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1b",
                        },
                    ]
                }
            if "describe-route-tables" in args:
                return {"RouteTables": [_igw_route_table(["subnet-private", "subnet-public"])]}
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        vpc, subnet = ec2.discover_network("dev", "us-east-1")
        assert vpc == "vpc-default"
        assert subnet == "subnet-public"

    def test_skips_az_that_does_not_offer_type(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-default"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        # public but in the unsupported AZ — must be skipped
                        {
                            "SubnetId": "subnet-1e",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1e",
                        },
                        {
                            "SubnetId": "subnet-1b",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1b",
                        },
                    ]
                }
            if "describe-instance-type-offerings" in args:
                return {"InstanceTypeOfferings": [{"Location": "us-east-1b"}]}
            if "describe-route-tables" in args:
                return {"RouteTables": [_igw_route_table(["subnet-1e", "subnet-1b"])]}
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        vpc, subnet = ec2.discover_network("dev", "us-east-1", "t4g.xlarge")
        assert subnet == "subnet-1b"

    def test_raises_when_no_subnet_has_internet_egress(self, monkeypatch):
        # Public-IP flag set, but no route table has a 0.0.0.0/0 route -> the
        # launch would hang to WaitCondition timeout; fail fast with guidance.
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-1"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-a",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1a",
                        }
                    ]
                }
            if "describe-route-tables" in args:
                # only a local route, no default egress
                return {
                    "RouteTables": [
                        {
                            "Routes": [
                                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}
                            ],
                            "Associations": [{"SubnetId": "subnet-a"}],
                        }
                    ]
                }
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        with pytest.raises(aws.AWSError, match="internet egress"):
            ec2.discover_network("dev", "us-east-1")

    def test_main_route_table_egress_covers_unassociated_subnet(self, monkeypatch):
        # A subnet with no explicit RT association inherits the VPC main RT.
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-1"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-main",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1a",
                        }
                    ]
                }
            if "describe-route-tables" in args:
                return {
                    "RouteTables": [
                        {
                            "Routes": [
                                {
                                    "DestinationCidrBlock": "0.0.0.0/0",
                                    "GatewayId": "igw-main",
                                    "State": "active",
                                }
                            ],
                            "Associations": [{"Main": True}],
                        }
                    ]
                }
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        _vpc, subnet = ec2.discover_network("dev", "us-east-1")
        assert subnet == "subnet-main"

    def test_explicit_no_egress_subnet_not_covered_by_main_table(self, monkeypatch):
        # A subnet EXPLICITLY bound to a no-egress (local-only) route table must
        # NOT inherit the main table's egress — its explicit binding overrides
        # the main table. Otherwise discover_network could pick a dead subnet and
        # hang the launch to the WaitCondition timeout. Here the only subnet is
        # explicitly bound to a local-only table, while the main table HAS an IGW
        # route — the subnet must still be treated as no-egress -> raise.
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-1"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-private",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1a",
                        }
                    ]
                }
            if "describe-route-tables" in args:
                return {
                    "RouteTables": [
                        # Main table HAS egress (IGW) — but the subnet is NOT
                        # associated with it.
                        {
                            "Routes": [
                                {
                                    "DestinationCidrBlock": "0.0.0.0/0",
                                    "GatewayId": "igw-main",
                                    "State": "active",
                                }
                            ],
                            "Associations": [{"Main": True}],
                        },
                        # subnet-private is EXPLICITLY bound to a local-only table.
                        {
                            "Routes": [
                                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}
                            ],
                            "Associations": [{"SubnetId": "subnet-private"}],
                        },
                    ]
                }
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        with pytest.raises(aws.AWSError, match="internet egress"):
            ec2.discover_network("dev", "us-east-1")

    def test_prefers_nat_subnet_over_igw(self, monkeypatch):
        # When both a NAT subnet and an IGW subnet exist, prefer NAT: it gives
        # egress regardless of a public IP, so it's the safest default.
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-1"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-igw",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1a",
                        },
                        {
                            "SubnetId": "subnet-nat",
                            "MapPublicIpOnLaunch": False,
                            "AvailabilityZone": "us-east-1b",
                        },
                    ]
                }
            if "describe-route-tables" in args:
                return {
                    "RouteTables": [
                        _igw_route_table(["subnet-igw"]),
                        _nat_route_table(["subnet-nat"]),
                    ]
                }
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        _vpc, subnet = ec2.discover_network("dev", "us-east-1")
        assert subnet == "subnet-nat"

    def test_igw_subnet_without_public_ip_is_usable(self, monkeypatch):
        # An IGW-routed subnet with MapPublicIpOnLaunch=False is still chosen —
        # the template forces AssociatePublicIpAddress so egress works. The old
        # code returned it only with a warning; the new code accepts it as a
        # first-class IGW candidate when no NAT subnet exists.
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-1"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-igw-nopub",
                            "MapPublicIpOnLaunch": False,
                            "AvailabilityZone": "us-east-1a",
                        }
                    ]
                }
            if "describe-route-tables" in args:
                return {"RouteTables": [_igw_route_table(["subnet-igw-nopub"])]}
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        _vpc, subnet = ec2.discover_network("dev", "us-east-1")
        assert subnet == "subnet-igw-nopub"

    def test_raises_when_no_az_offers_type(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": [{"VpcId": "vpc-default"}]}
            if "describe-subnets" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-1e",
                            "MapPublicIpOnLaunch": True,
                            "AvailabilityZone": "us-east-1e",
                        }
                    ]
                }
            if "describe-instance-type-offerings" in args:
                return {"InstanceTypeOfferings": [{"Location": "us-east-1b"}]}
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        with pytest.raises(aws.AWSError, match="offers"):
            ec2.discover_network("dev", "us-east-1", "t4g.xlarge")

    def test_no_default_vpc_raises_actionable(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-vpcs" in args:
                return {"Vpcs": []}  # no default and (below) not exactly one
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        with pytest.raises(aws.AWSError) as ei:
            ec2.discover_network("dev", "us-east-1")
        assert "no default VPC" in str(ei.value)


class TestStatusAndList:
    def _stack(self, status="CREATE_COMPLETE", instance="i-0abc"):
        return {
            "StackName": "kirocrew-t1",
            "StackStatus": status,
            "Tags": [{"Key": "kirocrew:managed", "Value": "true"}],
            "Outputs": [
                {"OutputKey": "InstanceId", "OutputValue": instance},
                {"OutputKey": "PublicDnsName", "OutputValue": "ec2-x.compute.amazonaws.com"},
                {"OutputKey": "Region", "OutputValue": "us-east-1"},
            ],
        }

    def test_find_stack_raises_on_untagged_name_collision(self, monkeypatch):
        # A stack merely NAMED kirocrew-<tag> but not tagged managed=true is a
        # foreign collision — find_stack must RAISE (returning None would read as
        # "absent" to deploy(), which would then deploy against the foreign stack).
        monkeypatch.setattr(
            aws,
            "run_aws",
            lambda *a, **k: (
                0,
                json.dumps(
                    {"Stacks": [{"StackName": "kirocrew-t1", "StackStatus": "CREATE_COMPLETE"}]}
                ),
                "",
            ),
        )
        with pytest.raises(aws.AWSError, match="NOT tagged"):
            ec2.find_stack("t1", "dev", "us-east-1")

    def test_find_stack_raises_on_instance_tag_mismatch(self, monkeypatch):
        # A managed stack named kirocrew-t1 but whose kirocrew:instance tag is a
        # DIFFERENT value isn't this launch's stack — find_stack must RAISE so
        # destroy/stop/start --tag t1 can't act on it.
        monkeypatch.setattr(
            aws,
            "run_aws",
            lambda *a, **k: (
                0,
                json.dumps(
                    {
                        "Stacks": [
                            {
                                "StackName": "kirocrew-t1",
                                "StackStatus": "CREATE_COMPLETE",
                                "Tags": [
                                    {"Key": "kirocrew:managed", "Value": "true"},
                                    {"Key": "kirocrew:instance", "Value": "somethingelse"},
                                ],
                            }
                        ]
                    }
                ),
                "",
            ),
        )
        with pytest.raises(aws.AWSError, match="isn't this launch's stack|not 't1'"):
            ec2.find_stack("t1", "dev", "us-east-1")

    def test_find_stack_ok_when_instance_tag_matches(self, monkeypatch):
        monkeypatch.setattr(
            aws,
            "run_aws",
            lambda *a, **k: (
                0,
                json.dumps(
                    {
                        "Stacks": [
                            {
                                "StackName": "kirocrew-t1",
                                "StackStatus": "CREATE_COMPLETE",
                                "Tags": [
                                    {"Key": "kirocrew:managed", "Value": "true"},
                                    {"Key": "kirocrew:instance", "Value": "t1"},
                                ],
                            }
                        ]
                    }
                ),
                "",
            ),
        )
        st = ec2.find_stack("t1", "dev", "us-east-1")
        assert st is not None and st["StackName"] == "kirocrew-t1"

    def test_describe_absent(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "does not exist"))
        r = ec2.describe("t1", "dev", "us-east-1")
        assert r == {"tag": "t1", "exists": False}

    def test_find_stack_absent_on_does_not_exist(self, monkeypatch):
        monkeypatch.setattr(
            aws,
            "run_aws",
            lambda *a, **k: (255, "", "ValidationError: Stack ... does not exist"),
        )
        assert ec2.find_stack("t1", "dev", "us-east-1") is None

    def test_find_stack_raises_on_access_denied(self, monkeypatch):
        # A permission/throttle error must NOT be mistaken for 'stack absent'.
        monkeypatch.setattr(
            aws,
            "run_aws",
            lambda *a, **k: (
                255,
                "",
                "AccessDenied: not authorized to perform: " "cloudformation:DescribeStacks",
            ),
        )
        with pytest.raises(aws.AWSError):
            ec2.find_stack("t1", "dev", "us-east-1")

    def test_find_stack_raises_on_throttle(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "Throttling: Rate exceeded"))
        with pytest.raises(aws.AWSError):
            ec2.find_stack("t1", "dev", "us-east-1")

    def test_describe_present(self, monkeypatch):
        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-stacks" in args:
                return (0, json.dumps({"Stacks": [self._stack()]}), "")
            if "describe-instances" in args:
                return (0, "running\n", "")
            return (0, "", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        r = ec2.describe("t1", "dev", "us-east-1")
        assert r["exists"] is True
        assert r["instance_id"] == "i-0abc"
        assert r["instance_state"] == "running"
        assert r["stack_status"] == "CREATE_COMPLETE"

    def test_list_instances(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            return {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:ec2:us-east-1:1:instance/i-0abc",
                        "Tags": [{"Key": "kirocrew:instance", "Value": "t1"}],
                    },
                ]
            }

        monkeypatch.setattr(aws, "checked_json", fake_json)
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "running\n", ""))
        rows = ec2.list_instances("dev", "us-east-1")
        assert rows == [{"tag": "t1", "instance_id": "i-0abc", "instance_state": "running"}]

    def test_list_instances_skips_terminated(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            return {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:ec2:us-east-1:1:instance/i-live",
                        "Tags": [{"Key": "kirocrew:instance", "Value": "t1"}],
                    },
                    {
                        "ResourceARN": "arn:aws:ec2:us-east-1:1:instance/i-dead",
                        "Tags": [{"Key": "kirocrew:instance", "Value": "t0"}],
                    },
                ]
            }

        states = {"i-live": "running", "i-dead": "terminated"}

        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            iid = args[args.index("--instance-ids") + 1]
            return (0, states[iid] + "\n", "")

        monkeypatch.setattr(aws, "checked_json", fake_json)
        monkeypatch.setattr(aws, "run_aws", fake_run)
        rows = ec2.list_instances("dev", "us-east-1")
        assert rows == [{"tag": "t1", "instance_id": "i-live", "instance_state": "running"}]

    def test_list_stacks_filters_kirocrew_prefix(self, monkeypatch):
        captured = {}

        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            captured["args"] = args
            captured["action"] = action
            return {
                "StackSummaries": [
                    {"StackName": "kirocrew-kc-b", "StackStatus": "CREATE_COMPLETE"},
                    {"StackName": "other", "StackStatus": "CREATE_COMPLETE"},
                    {"StackName": "kirocrew-kc-a", "StackStatus": "UPDATE_COMPLETE"},
                ]
            }

        monkeypatch.setattr(aws, "checked_json", fake_json)
        rows = ec2.list_stacks("dev", "us-east-1")
        assert rows == [
            {"tag": "kc-a", "stack_name": "kirocrew-kc-a", "stack_status": "UPDATE_COMPLETE"},
            {"tag": "kc-b", "stack_name": "kirocrew-kc-b", "stack_status": "CREATE_COMPLETE"},
        ]
        assert captured["action"] == "cloudformation:ListStacks"
        assert "list-stacks" in captured["args"]


class TestLifecycle:
    def test_stop_calls_stop_instances(self, monkeypatch):
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        captured = {}

        def fake_checked(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            captured["args"] = args
            captured["action"] = action
            return ""

        monkeypatch.setattr(aws, "checked", fake_checked)
        r = ec2.stop("t1", "dev", "us-east-1")
        assert r["action"] == "stop"
        assert "stop-instances" in captured["args"]
        assert captured["action"] == "ec2:StopInstances"

    def test_start_calls_start_instances(self, monkeypatch):
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        captured = {}
        monkeypatch.setattr(
            aws,
            "checked",
            lambda args, *a, action="", **k: captured.update(args=args, action=action) or "",
        )
        r = ec2.start("t1", "dev", "us-east-1")
        assert r["action"] == "start"
        assert "start-instances" in captured["args"]

    def test_stop_missing_instance_raises(self, monkeypatch):
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        with pytest.raises(aws.AWSError):
            ec2.stop("t1", "dev", "us-east-1")


class TestStackEventsAndFailures:
    _EVENTS = {
        "StackEvents": [
            # newest-first, as the API returns them
            {
                "EventId": "e3",
                "LogicalResourceId": "WaitCondition",
                "ResourceStatus": "CREATE_FAILED",
                "ResourceStatusReason": "WaitCondition received failed message: "
                "'kirocrew install.sh failed' :: ...node-rc=1|No match for nodejs",
            },
            {
                "EventId": "e2",
                "LogicalResourceId": "Instance",
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceStatusReason": "",
            },
            {
                "EventId": "e1",
                "LogicalResourceId": "kirocrew-t1",
                "ResourceStatus": "CREATE_IN_PROGRESS",
                "ResourceStatusReason": "",
            },
        ]
    }

    def test_list_stack_events_oldest_first(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, json.dumps(self._EVENTS), ""))
        evs = ec2.list_stack_events("t1", "dev", "us-east-1")
        assert [e["id"] for e in evs] == ["e1", "e2", "e3"]  # reversed to oldest-first
        assert evs[-1]["status"] == "CREATE_FAILED"

    def test_get_stack_failures_surfaces_bootstrap_reason(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, json.dumps(self._EVENTS), ""))
        fails = ec2.get_stack_failures("t1", "dev", "us-east-1")
        assert fails
        assert fails[0]["resource"] == "WaitCondition"
        assert "install.sh failed" in fails[0]["reason"]
        assert "node-rc=1" in fails[0]["reason"]  # on-box log tail folded in

    def test_get_stack_failures_empty_on_error(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "boom"))
        assert ec2.get_stack_failures("t1", "dev", "us-east-1") == []

    def test_get_stack_failures_puts_specific_reason_first(self, monkeypatch):
        # Events are newest-first; the generic "[WaitCondition]" cascade line is
        # usually the newest FAILED event, so a naive append would bury the real
        # root cause behind it. The specific reason must sort to failures[0].
        events = {
            "StackEvents": [
                {
                    "EventId": "e3",
                    "LogicalResourceId": "kirocrew-t1",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": (
                        "The following resource(s) failed to create: [WaitCondition]."
                    ),
                },
                {
                    "EventId": "e2",
                    "LogicalResourceId": "WaitCondition",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "WaitCondition received failed message: bootstrap boom",
                },
                {
                    "EventId": "e1",
                    "LogicalResourceId": "Instance",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "Resource creation cancelled",
                },
            ]
        }
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, json.dumps(events), ""))
        fails = ec2.get_stack_failures("t1", "dev", "us-east-1")
        assert fails[0]["resource"] == "WaitCondition"
        assert "bootstrap boom" in fails[0]["reason"]
        # generic cascade lines dropped entirely once a specific reason exists
        assert all("bootstrap boom" in f["reason"] for f in fails)

    def test_get_stack_failures_keeps_generic_when_no_specific(self, monkeypatch):
        # If every FAILED event is generic, still report something rather than
        # returning an empty list.
        events = {
            "StackEvents": [
                {
                    "EventId": "e1",
                    "LogicalResourceId": "kirocrew-t1",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": (
                        "The following resource(s) failed to create: [WaitCondition]."
                    ),
                }
            ]
        }
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, json.dumps(events), ""))
        fails = ec2.get_stack_failures("t1", "dev", "us-east-1")
        assert len(fails) == 1
        assert fails[0]["resource"] == "kirocrew-t1"

    def test_deploy_disable_rollback_appends_flag(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
        monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "k"))
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1"))
        captured = {}

        def fake_run(argv, profile="", region="", *, timeout=ec2._DEPLOY_TIMEOUT, proc_sink=None):
            captured["argv"] = argv
            return (0, "ok", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"},
        )
        ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            disable_rollback=True,
        )
        assert "--disable-rollback" in captured["argv"]

    def test_deploy_failure_attaches_root_cause(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
        monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "k"))
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1"))
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: (1, "", "Failed to create/update the stack")
        )
        monkeypatch.setattr(
            ec2,
            "get_stack_failures",
            lambda *a, **k: [
                {
                    "resource": "WaitCondition",
                    "status": "CREATE_FAILED",
                    "reason": "kirocrew install.sh failed :: node-rc=1",
                }
            ],
        )
        with pytest.raises(aws.AWSError, match="root cause"):
            ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")


class TestHumanActionGuard:
    """Mutating cloud ops must refuse from an agent session (KIROCREW_SESSION_KEY
    set) — closes the bypass where an agent calls ec2.destroy()/deploy() from a
    Python snippet, sidestepping the shell deniedCommands."""

    def test_mutations_denied_under_agent_session(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-123")
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: pytest.fail("must not reach AWS under agent session")
        )
        with pytest.raises(aws.CloudActionDenied):
            ec2.destroy("t1", "dev", "us-east-1")
        with pytest.raises(aws.CloudActionDenied):
            ec2.stop("t1", "dev", "us-east-1")
        with pytest.raises(aws.CloudActionDenied):
            ec2.start("t1", "dev", "us-east-1")
        with pytest.raises(aws.CloudActionDenied):
            ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")

    def test_dry_run_allowed_under_agent_session(self, monkeypatch):
        # A read-only dry run (no AWS mutation) is fine even from an agent.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-123")
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.destroy("t1", "dev", "us-east-1", dry_run=True)
        assert r["dry_run"] is True

    def test_mutations_allowed_without_session_key(self, monkeypatch):
        # Human terminal: no KIROCREW_SESSION_KEY -> the guard is a no-op.
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        aws.assert_human_action("cloudformation:DeleteStack")  # must not raise


class TestDestroy:
    def test_build_destroy_argv(self):
        assert ec2.build_destroy_argv("t1") == [
            "cloudformation",
            "delete-stack",
            "--stack-name",
            "kirocrew-t1",
        ]

    def test_dry_run(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.destroy("t1", "dev", "us-east-1", dry_run=True)
        assert r["dry_run"] is True
        assert r["argv"] == ["cloudformation", "delete-stack", "--stack-name", "kirocrew-t1"]

    def test_already_absent_is_success(self, monkeypatch):
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        r = ec2.destroy("t1", "dev", "us-east-1")
        assert r["destroyed"] is True
        assert r["already_absent"] is True

    def test_destroy_propagates_query_error(self, monkeypatch):
        # find_stack raising (e.g. AccessDenied) must NOT be reported as success;
        # the error propagates so the CLI never claims a billed stack was removed.
        def boom(*a, **k):
            raise aws.AWSError("query failed", action="cloudformation:DescribeStacks")

        monkeypatch.setattr(ec2, "find_stack", boom)
        with pytest.raises(aws.AWSError):
            ec2.destroy("t1", "dev", "us-east-1")

    def test_destroy_deletes_and_waits(self, monkeypatch):
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: {"StackName": "kirocrew-t1"})
        calls = {}
        monkeypatch.setattr(
            aws,
            "checked",
            lambda args, *a, action="", **k: calls.update(delete=args, action=action) or "",
        )
        monkeypatch.setattr(ec2, "wait_for_delete", lambda *a, **k: True)
        r = ec2.destroy("t1", "dev", "us-east-1")
        assert r["destroyed"] is True
        assert r["waited"] is True
        assert "delete-stack" in calls["delete"]
        assert calls["action"] == "cloudformation:DeleteStack"

    def test_destroy_no_wait(self, monkeypatch):
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: {"StackName": "kirocrew-t1"})
        monkeypatch.setattr(aws, "checked", lambda *a, **k: "")
        r = ec2.destroy("t1", "dev", "us-east-1", wait=False)
        assert r["destroyed"] is True
        assert r["waited"] is False

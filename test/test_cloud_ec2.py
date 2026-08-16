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

    def test_bootstrap_enforces_node_major_floor(self):
        # The frontend build (vite 8 + rolldown) needs Node >=22; AL2023's default
        # AppStream nodejs is 18, which fails with a node:util/styleText SyntaxError.
        # The template must (a) declare the >=22 floor, (b) upgrade via a PINNED
        # official nodejs.org tarball when the installed node is too old (dnf/NodeSource
        # is a dead end on AL2023 — its modular filtering keeps reinstalling node 18),
        # verifying the tarball's SHA-256 before extracting as root, and (c) fail the
        # bootstrap if it still cannot reach the floor.
        text = ec2.load_template()
        assert "NODE_MAJOR_MIN=22" in text
        assert "nodejs.org/dist/" in text
        assert 'fail "Node.js too old' in text
        # The tarball MUST be integrity-checked before it is extracted as root.
        assert "sha256sum -c" in text
        assert "9e7905fdee722f9650a03ae644b51c4c6effd3b98ac93c588700072ab35c9ddb" in text
        assert "e05a4d65232ae2b27b3d77da2e368522fb46b923335b8e0d5f77624c32484044" in text


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

    def test_subnet_id_valid(self):
        assert ec2.validate_subnet_id("subnet-0123456789abcdef0") == "subnet-0123456789abcdef0"
        assert ec2.validate_subnet_id("subnet-12345678") == "subnet-12345678"  # classic 8-hex

    def test_subnet_id_bad_charset_rejected(self):
        # flows into subprocess argv — charset-validate like the other fields
        for bad in ("subnet-XYZ", "subnet-123", "vpc-0123456789abcdef0", "subnet-1; rm -rf"):
            with pytest.raises(ValidationError):
                ec2.validate_subnet_id(bad)

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

    def test_bootstrap_installs_voice_extra_before_gateway_boot(self):
        # Remote instances need the Transcribe SDK in their venv before the
        # gateway imports boto3. Keep both the first attempt and retry aligned.
        text = ec2.load_template()
        assert text.count("bash install.sh --voice") == 2

    def test_instance_enforces_imdsv2(self):
        text = ec2.load_template()
        assert "MetadataOptions" in text
        assert "HttpTokens: required" in text

    def test_public_ip_is_conditional_on_egress_kind(self):
        # A NAT-routed (private) subnet must not get a public IP; only IGW
        # subnets (where it is required for egress) do. The launcher passes the
        # AssociatePublicIp parameter from the computed egress kind.
        text = ec2.load_template()
        assert "AssociatePublicIp:" in text  # the parameter exists
        assert 'WantPublicIp: !Equals [!Ref AssociatePublicIp, "true"]' in text
        assert "AssociatePublicIpAddress: !If [WantPublicIp, true, false]" in text
        assert "AssociatePublicIpAddress: true" not in text  # never hardcoded

    def test_spot_is_opt_in_and_defaults_to_on_demand(self):
        # The Spot parameter must default to "false" so an unchanged launch is
        # on-demand, and the market options must hang off the IsSpot condition
        # (resolving to AWS::NoValue otherwise) rather than always being emitted.
        text = ec2.load_template()
        block = re.search(r"  Spot:\n(?:    .+\n)+", text)
        assert block, "Spot parameter missing"
        assert 'Default: "false"' in block.group(0)
        assert 'AllowedValues: ["true", "false"]' in block.group(0)
        assert 'IsSpot: !Equals [!Ref Spot, "true"]' in text
        assert "!Ref AWS::NoValue" in text

    def test_spot_market_options_are_not_on_the_instance(self):
        # AWS::EC2::Instance has NO InstanceMarketOptions property — a template
        # that puts one there fails validation (cfn-lint E3002). Spot on a
        # CloudFormation single instance is expressible ONLY via a launch
        # template the instance references, so a regression that "simplifies" it
        # back onto the Instance must fail here rather than at deploy time.
        text = ec2.load_template()
        market_idx = text.index("InstanceMarketOptions:")
        assert text.index("SpotLaunchTemplate:") < market_idx
        assert text.index("  Instance:\n") > market_idx, (
            "InstanceMarketOptions must live in SpotLaunchTemplate, not on the Instance"
        )

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


class TestSpotTemplateStructure:
    """Parse the template as YAML and assert the spot wiring structurally.

    Substring assertions can't tell "under the right resource" from "somewhere in
    the file", and the whole point of the launch-template design is WHERE each
    property sits. CloudFormation's short tags (!Ref/!Sub/!If/...) aren't valid
    YAML, so a multi_constructor maps them to their long form instead of pulling
    in a new dependency.
    """

    @staticmethod
    def _load():
        import yaml
        from yaml_helpers import load_with

        class _CfnLoader(yaml.SafeLoader):
            pass

        def _cfn_tag(loader, tag_suffix, node):
            name = "Fn::" + tag_suffix if tag_suffix != "Ref" else "Ref"
            if isinstance(node, yaml.ScalarNode):
                return {name: loader.construct_scalar(node)}
            if isinstance(node, yaml.SequenceNode):
                return {name: loader.construct_sequence(node, deep=True)}
            return {name: loader.construct_mapping(node, deep=True)}

        _CfnLoader.add_multi_constructor("!", _cfn_tag)
        return load_with(_CfnLoader, ec2.load_template())

    def test_spot_defaults_to_false(self):
        # The parameter default is what makes --spot opt-in: a deploy that sends
        # no Spot override must stay on-demand.
        doc = self._load()
        assert doc["Parameters"]["Spot"]["Default"] == "false"
        assert doc["Parameters"]["Spot"]["AllowedValues"] == ["true", "false"]
        assert doc["Conditions"]["IsSpot"] == {"Fn::Equals": [{"Ref": "Spot"}, "true"]}

    def test_launch_template_exists_only_under_is_spot(self):
        lt = self._load()["Resources"]["SpotLaunchTemplate"]
        assert lt["Type"] == "AWS::EC2::LaunchTemplate"
        assert lt["Condition"] == "IsSpot"

    def test_spot_options_are_persistent_stop_and_never_expire(self):
        # The root volume is DeleteOnTermination: true, so AWS's DEFAULTS
        # (one-time + terminate) would wipe the data home on an interruption.
        # persistent+stop keeps the EBS volume AND lets EC2 restart the box
        # itself (only EC2 can resume an interruption-stop). The explicit
        # far-future ValidUntil defeats the launch-template variant's documented
        # 7-day default: request expiry counts as cancellation, and cancelling
        # the request of a STOPPED spot instance auto-terminates it — silent data
        # loss on any box left stopped for a week.
        opts = self._load()["Resources"]["SpotLaunchTemplate"]["Properties"][
            "LaunchTemplateData"
        ]["InstanceMarketOptions"]
        assert opts["MarketType"] == "spot"
        assert opts["SpotOptions"] == {
            "SpotInstanceType": "persistent",
            "InstanceInterruptionBehavior": "stop",
            "ValidUntil": "2099-01-01T00:00:00Z",
        }

    def test_spot_request_is_tagged_for_discovery(self):
        # `cloud destroy` finds the persistent request by these tags to cancel it
        # BEFORE delete-stack; without the cancel, terminating the instance flips
        # the request open and EC2 launches a replacement outside the stack. The
        # tags also gate the IAM create/cancel conditions.
        specs = self._load()["Resources"]["SpotLaunchTemplate"]["Properties"][
            "LaunchTemplateData"
        ]["TagSpecifications"]
        req = next(s for s in specs if s["ResourceType"] == "spot-instances-request")
        tags = {t["Key"]: t["Value"] for t in req["Tags"]}
        assert tags[ec2.MANAGED_TAG_KEY] == "true"
        assert tags[ec2.INSTANCE_TAG_KEY] == {"Ref": "StackTag"}

    def test_launch_template_tags_the_instance_and_volume_it_launches(self):
        # The load-bearing one for a REPLACEMENT instance: when the persistent
        # request re-opens, the Spot service relaunches from the request's launch
        # specification, so CloudFormation's Instance tags never reach that
        # instance. Only LT-level tags do — and without them the replacement is
        # invisible to the sweep's tag-filtered describe AND denied by the
        # launcher policy's aws:ResourceTag-gated ec2:TerminateInstances, i.e.
        # the exact orphan the sweep exists for would be the one it can't kill.
        specs = self._load()["Resources"]["SpotLaunchTemplate"]["Properties"][
            "LaunchTemplateData"
        ]["TagSpecifications"]
        # All three, pinned: dropping any one of them silently re-opens a leak.
        assert [s["ResourceType"] for s in specs] == [
            "spot-instances-request",
            "instance",
            "volume",
        ]
        expected = {
            "Name": {"Fn::Sub": "kirocrew-${StackTag}"},
            ec2.MANAGED_TAG_KEY: "true",
            ec2.INSTANCE_TAG_KEY: {"Ref": "StackTag"},
        }
        for resource_type in ("instance", "volume"):
            spec = next(s for s in specs if s["ResourceType"] == resource_type)
            assert {t["Key"]: t["Value"] for t in spec["Tags"]} == expected
        # On the PRIMARY launch these duplicate what CloudFormation already puts
        # on the Instance — same keys AND same values, so the documented
        # request-wins merge is a no-op rather than a conflict.
        instance_tags = {
            t["Key"]: t["Value"] for t in self._load()["Resources"]["Instance"]["Properties"]["Tags"]
        }
        assert instance_tags == expected

    def test_launch_template_itself_is_tagged(self):
        # So the launcher policy can tag-gate ec2:DeleteLaunchTemplate, the same
        # treatment the security group gets.
        specs = self._load()["Resources"]["SpotLaunchTemplate"]["Properties"][
            "TagSpecifications"
        ]
        lt = next(s for s in specs if s["ResourceType"] == "launch-template")
        tags = {t["Key"]: t["Value"] for t in lt["Tags"]}
        assert tags[ec2.MANAGED_TAG_KEY] == "true"
        assert tags[ec2.INSTANCE_TAG_KEY] == {"Ref": "StackTag"}

    def test_instance_references_the_launch_template_conditionally(self):
        # On-demand must resolve to AWS::NoValue so the rendered Instance is
        # exactly what it was before --spot existed.
        instance = self._load()["Resources"]["Instance"]["Properties"]
        assert instance["LaunchTemplate"] == {
            "Fn::If": [
                "IsSpot",
                {
                    "LaunchTemplateId": {"Ref": "SpotLaunchTemplate"},
                    "Version": {"Fn::GetAtt": "SpotLaunchTemplate.LatestVersionNumber"},
                },
                {"Ref": "AWS::NoValue"},
            ]
        }
        # ...and the market options are NOT on the instance (invalid property).
        assert "InstanceMarketOptions" not in instance

    def test_launch_template_carries_market_options_only(self):
        # Everything that defines the box (AMI, networking, IMDSv2, block
        # devices, UserData, tags) must stay on the Instance so the spot and
        # on-demand paths can't drift apart.
        data = self._load()["Resources"]["SpotLaunchTemplate"]["Properties"][
            "LaunchTemplateData"
        ]
        assert set(data) == {"InstanceMarketOptions", "TagSpecifications"}


class TestUserDataSize:
    """Guard the EXPANDED UserData size against EC2's hard 16 KB limit.

    EC2 rejects a launch whose DECODED UserData exceeds 16,384 bytes, and the
    limit applies AFTER CloudFormation resolves the !Sub — so the raw literal
    in the template is not the number that matters. Each ``${...}`` grows at
    render time (the WaitHandle presigned S3 URL alone is ~250 chars), so a
    template can look comfortably sized in the file yet be rejected at launch.
    This test renders a worst-case expansion and enforces a ceiling with real
    headroom, so a regression fails here instead of at a user's
    ``kirocrew cloud launch``.
    """

    # EC2's hard limit on the decoded UserData payload, in bytes.
    _EC2_USERDATA_LIMIT = 16_384

    # Enforced ceiling: 2 KB of headroom under the hard limit, ON TOP of the
    # already-pessimistic substitution values below. When this trips, slim the
    # script by MOVING knowledge into the template comments above UserData
    # (see the "Bootstrap script rationale" block) — never by deleting it or
    # making the script cryptic.
    _CEILING = _EC2_USERDATA_LIMIT - 2_048

    # Worst-case value per !Sub variable. Parameter lengths come from the
    # template's own AllowedPattern caps; pseudo-params and the WaitHandle use
    # pessimistic constants (a presigned S3 URL measures ~250 chars — 512
    # doubles that for margin). A NEW substitution variable fails the test
    # until an entry is added here, forcing its growth to be sized.
    _WORST_CASE = {
        "WaitHandle": "h" * 512,
        "SourceBucket": "b" * 63,
        "SourceKey": "k" * 255,
        "KirocrewRepo": "r" * 255,
        "KirocrewRef": "f" * 128,
        "DashboardPort": "65535",
        "StackTag": "t" * 51,
        "AWS::AccountId": "1" * 12,
        "AWS::Region": "ap-southeast-99",
        "AWS::StackName": "s" * 128,
    }

    def _raw_userdata(self) -> str:
        text = ec2.load_template()
        m = re.search(r"Fn::Base64: !Sub \|\n((?: {10}.*\n|\n)+)", text)
        assert m, "UserData !Sub block scalar not found in the template"
        # Strip the 10-space YAML block indent — CloudFormation does the same
        # when it materializes the scalar.
        script = "".join(
            (line[10:] if line.startswith(" " * 10) else line) + "\n"
            for line in m.group(1).splitlines()
        )
        # Guard the extraction itself: a regex that silently matched a stub
        # would turn this whole test into a no-op.
        assert script.startswith("#!/bin/bash"), "extracted UserData is not the bootstrap script"
        assert len(script) > 4_000, "extracted UserData is implausibly small"
        return script

    def _expand(self, script: str) -> str:
        # Substitute every real ${Var}; leave ${!x} literal escapes for the
        # final unescape step, exactly as CloudFormation's Sub does.
        def sub_one(m: re.Match[str]) -> str:
            var = m.group(1)
            assert var in self._WORST_CASE, (
                f"!Sub variable ${{{var}}} has no worst-case size entry — add one "
                f"to {type(self).__name__}._WORST_CASE so its render-time growth "
                "is accounted for"
            )
            return self._WORST_CASE[var]

        expanded = re.sub(r"\$\{([^!}][^}]*)\}", sub_one, script)
        return expanded.replace("${!", "${")

    def test_expanded_userdata_stays_under_ceiling(self):
        script = self._raw_userdata()
        expanded = self._expand(script)
        size = len(expanded.encode("utf-8"))
        assert size <= self._CEILING, (
            f"worst-case expanded UserData is {size} bytes, over the "
            f"{self._CEILING}-byte ceiling ({self._EC2_USERDATA_LIMIT} EC2 limit "
            f"minus headroom). Slim the bootstrap script by relocating comments "
            f"into the template's rationale block above UserData — do not delete "
            f"the knowledge or obfuscate the script."
        )

    def test_expansion_grows_the_payload(self):
        # Confidence-check the harness: the worst-case render must be LARGER than
        # the raw literal (the substitutions net-add bytes). If this fails the
        # worst-case table has degraded into an optimistic one.
        script = self._raw_userdata()
        assert len(self._expand(script).encode()) > len(
            script.replace("${!", "${").encode()
        )


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

    def test_spot_included_when_set(self):
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="v",
            subnet_id="s",
            permissions_boundary_arn=_BOUNDARY_ARN,
            spot=True,
        )
        assert "Spot=true" in argv

    def test_spot_omitted_by_default(self):
        # On-demand must stay byte-for-byte what it was before --spot existed:
        # no override at all, so the template's Spot="false" default applies.
        tier = sizes.get_tier("balanced")
        argv = ec2.build_deploy_argv(
            tag="t1",
            tier=tier,
            vpc_id="v",
            subnet_id="s",
            permissions_boundary_arn=_BOUNDARY_ARN,
        )
        assert not any(a.startswith("Spot=") for a in argv)


class TestDeployDryRun:
    def test_dry_run_returns_argv_without_aws(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        # If run_aws is called during a dry run, fail loudly.
        monkeypatch.setattr(source_mod, "find_repo_root", lambda: object())
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

    def test_dry_run_shows_explicit_subnet(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="us-east-1",
            subnet_id="subnet-0123456789abcdef0",
            dry_run=True,
        )
        assert "SubnetId=subnet-0123456789abcdef0" in r.argv
        assert "VpcId=<auto>" in r.argv  # resolved from the subnet at real-run time
        assert "AssociatePublicIp=<auto>" in r.argv  # egress kind known only at real run

    def test_dry_run_shows_spot(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="us-east-1",
            spot=True,
            dry_run=True,
        )
        assert "Spot=true" in r.argv

    def test_dry_run_omits_spot_by_default(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        r = ec2.deploy(
            tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1", dry_run=True
        )
        assert not any(a.startswith("Spot=") for a in r.argv)

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

    def test_dry_run_defaults_to_public_clone_without_checkout(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(source_mod, "find_repo_root", lambda: None)
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))

        r = ec2.deploy(tag="t1", tier=sizes.default_tier(), dry_run=True)

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
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
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
        # IGW-routed subnet -> the public IP is required for egress
        assert "AssociatePublicIp=true" in captured["argv"]
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


class TestDeployExplicitSubnet:
    def _stub_deploy_deps(self, monkeypatch):
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
        monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("b", "t1/k.tar.gz"))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"},
        )

    def test_explicit_subnet_skips_discovery(self, monkeypatch):
        self._stub_deploy_deps(monkeypatch)
        monkeypatch.setattr(
            ec2,
            "discover_network",
            lambda *a, **k: pytest.fail("--subnet must bypass discover_network"),
        )
        monkeypatch.setattr(
            ec2,
            "resolve_explicit_subnet",
            lambda subnet_id, *a, **k: ("vpc-dedicated", subnet_id, "nat"),
        )
        captured = {}

        def fake_run(argv, profile="", region="", *, timeout=ec2._DEPLOY_TIMEOUT, proc_sink=None):
            captured["argv"] = argv
            return (0, "ok", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="ap-southeast-1",
            subnet_id="subnet-0123456789abcdef0",
        )
        assert "VpcId=vpc-dedicated" in captured["argv"]
        assert "SubnetId=subnet-0123456789abcdef0" in captured["argv"]
        # NAT-routed pin -> no public IP on the instance
        assert "AssociatePublicIp=false" in captured["argv"]

    def test_discovered_nat_subnet_suppresses_public_ip(self, monkeypatch):
        # The egress-kind wiring must also cover the auto-discovery path.
        self._stub_deploy_deps(monkeypatch)
        monkeypatch.setattr(
            ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-priv", "nat")
        )
        captured = {}

        def fake_run(argv, profile="", region="", *, timeout=ec2._DEPLOY_TIMEOUT, proc_sink=None):
            captured["argv"] = argv
            return (0, "ok", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")
        assert "AssociatePublicIp=false" in captured["argv"]

    def test_bad_subnet_id_rejected_before_any_aws_call(self, monkeypatch):
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: pytest.fail("must not reach AWS on a bad subnet id")
        )
        with pytest.raises(ValidationError):
            ec2.deploy(
                tag="t1",
                tier=sizes.default_tier(),
                profile="dev",
                region="us-east-1",
                subnet_id="subnet-nope!",
            )

    def test_explicit_subnet_failure_deletes_uploaded_source(self, monkeypatch):
        # Same cleanup contract as discovery: a validation failure after the
        # source upload must not orphan the tarball in S3.
        import kiro_crew.cloud.source as source_mod

        deleted: list[str] = []
        self._stub_deploy_deps(monkeypatch)
        monkeypatch.setattr(source_mod, "delete_source", lambda tag, *a, **k: deleted.append(tag))
        monkeypatch.setattr(
            ec2,
            "resolve_explicit_subnet",
            lambda *a, **k: (_ for _ in ()).throw(
                aws.AWSError("no verified internet egress", action="ec2:DescribeRouteTables")
            ),
        )
        with pytest.raises(aws.AWSError):
            ec2.deploy(
                tag="t1",
                tier=sizes.default_tier(),
                profile="dev",
                region="us-east-1",
                subnet_id="subnet-0123456789abcdef0",
            )
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
        vpc, subnet, kind = ec2.discover_network("dev", "us-east-1")
        assert vpc == "vpc-default"
        assert subnet == "subnet-public"
        assert kind == "igw"

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
        vpc, subnet, kind = ec2.discover_network("dev", "us-east-1", "t4g.xlarge")
        assert subnet == "subnet-1b"
        assert kind == "igw"

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
        _vpc, subnet, _kind = ec2.discover_network("dev", "us-east-1")
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
        _vpc, subnet, _kind = ec2.discover_network("dev", "us-east-1")
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
        _vpc, subnet, _kind = ec2.discover_network("dev", "us-east-1")
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


class TestResolveExplicitSubnet:
    def test_resolves_vpc_and_keeps_private_nat_subnet(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-subnets" in args and "--subnet-ids" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-priv",
                            "VpcId": "vpc-dedicated",
                            "AvailabilityZone": "ap-southeast-1a",
                        }
                    ]
                }
            if "describe-route-tables" in args:
                return {"RouteTables": [_nat_route_table(["subnet-priv"])]}
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        vpc, subnet, kind = ec2.resolve_explicit_subnet("subnet-priv", "dev", "ap-southeast-1")
        assert (vpc, subnet, kind) == ("vpc-dedicated", "subnet-priv", "nat")

    def test_missing_subnet_raises(self, monkeypatch):
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {"Subnets": []})
        with pytest.raises(aws.AWSError, match="not found"):
            ec2.resolve_explicit_subnet("subnet-gone", "dev", "us-east-1")

    def test_no_egress_subnet_raises(self, monkeypatch):
        # An isolated subnet (local route only) would hang the launch to the
        # WaitCondition timeout — the explicit path must fail fast like discovery.
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-subnets" in args and "--subnet-ids" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-iso",
                            "VpcId": "vpc-1",
                            "AvailabilityZone": "us-east-1a",
                        }
                    ]
                }
            if "describe-route-tables" in args:
                return {
                    "RouteTables": [
                        {
                            "Routes": [
                                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}
                            ],
                            "Associations": [{"SubnetId": "subnet-iso"}],
                        }
                    ]
                }
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        with pytest.raises(aws.AWSError, match="egress"):
            ec2.resolve_explicit_subnet("subnet-iso", "dev", "us-east-1")

    def test_az_not_offering_type_raises(self, monkeypatch):
        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            if "describe-subnets" in args and "--subnet-ids" in args:
                return {
                    "Subnets": [
                        {
                            "SubnetId": "subnet-1e",
                            "VpcId": "vpc-1",
                            "AvailabilityZone": "us-east-1e",
                        }
                    ]
                }
            if "describe-instance-type-offerings" in args:
                return {"InstanceTypeOfferings": [{"Location": "us-east-1b"}]}
            return {}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        with pytest.raises(aws.AWSError, match="does not offer"):
            ec2.resolve_explicit_subnet("subnet-1e", "dev", "us-east-1", "t4g.xlarge")


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
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
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
        monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
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


# What cancel_spot_requests() returns for an on-demand stack: nothing found,
# nothing cancelled, nothing failed.
_EMPTY_SWEEP = {
    "cancelled": [],
    "failed": [],
    "error": "",
    "error_kind": "",
    "terminated": [],
    "terminate_failed": [],
    "terminate_error": "",
}


def test_empty_spot_sweep_matches_the_documented_outcome_shape():
    # Every caller reads this dict by key, so the "nothing found" shape and the
    # real outcome shape must not drift apart.
    assert ec2._empty_spot_sweep() == _EMPTY_SWEEP


class TestDestroy:
    @pytest.fixture(autouse=True)
    def _no_spot_requests(self, monkeypatch):
        # destroy() sweeps for a leftover Spot request on the stack-exists path.
        # These tests are about the delete-stack path, so stub the sweep to the
        # on-demand answer (none) and keep them hermetic — without this they
        # would shell out to a real `aws ec2 describe-spot-instance-requests`.
        monkeypatch.setattr(ec2, "cancel_spot_requests", lambda *a, **k: _EMPTY_SWEEP)

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
        monkeypatch.setattr(
            ec2, "cancel_spot_requests", lambda *a, **k: pytest.fail("no stack — no sweep here")
        )
        r = ec2.destroy("t1", "dev", "us-east-1")
        assert r["destroyed"] is True
        assert r["already_absent"] is True
        # The no-stack orphan sweep is the CLI's job (it never calls destroy() on
        # a describe miss); doing it here too would only add a mutation to the
        # path that has nothing to delete.
        assert r["spot_sweep"] == _EMPTY_SWEEP

    def test_destroy_propagates_query_error(self, monkeypatch):
        # find_stack raising (e.g. AccessDenied) must NOT be reported as success;
        # the error propagates so the CLI never claims a billed stack was removed.
        # And because the lookup runs FIRST, the abort is total: nothing was
        # cancelled and nothing terminated, so "could not query stack" is the
        # whole truth rather than a half-done teardown the user isn't told about.
        def boom(*a, **k):
            raise aws.AWSError("query failed", action="cloudformation:DescribeStacks")

        monkeypatch.setattr(ec2, "find_stack", boom)
        monkeypatch.setattr(
            ec2,
            "cancel_spot_requests",
            lambda *a, **k: pytest.fail("must not mutate before the stack lookup succeeds"),
        )
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


class TestDestroyCancelsSpotRequest:
    """A --spot stack owns one resource CloudFormation does not: the persistent
    Spot *request*. Terminating its instance (which delete-stack does) flips the
    request back to `open` and EC2 launches a REPLACEMENT instance outside the
    stack — a box nobody tracks and nobody stops billing for. So destroy cancels
    the request FIRST.
    """

    @staticmethod
    def _record(monkeypatch, *, requests):
        """Stub the aws layer, returning the recorded (json_calls, checked_calls).

        ``requests`` items are either a bare request id (an ``active`` request
        with no instance recorded) or a full describe record dict.
        """
        records = [
            r if isinstance(r, dict) else {"SpotInstanceRequestId": r, "State": "active"}
            for r in requests
        ]
        json_calls: list = []
        checked_calls: list = []

        def fake_checked_json(args, *a, action="", **k):
            json_calls.append((args, action))
            return {"SpotInstanceRequests": records}

        def fake_checked(args, *a, action="", **k):
            checked_calls.append((args, action))
            return ""

        monkeypatch.setattr(aws, "checked_json", fake_checked_json)
        monkeypatch.setattr(aws, "checked", fake_checked)
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: {"StackName": "kirocrew-t1"})
        monkeypatch.setattr(ec2, "wait_for_delete", lambda *a, **k: True)
        return json_calls, checked_calls

    def test_finds_requests_by_instance_tag_without_a_state_filter(self, monkeypatch):
        json_calls, _ = self._record(
            monkeypatch, requests=[{"SpotInstanceRequestId": "sir-1", "State": "disabled"}]
        )
        assert ec2.find_spot_requests("t1", "dev", "us-east-1") == [
            {"id": "sir-1", "instance_id": ""}
        ]
        args, action = json_calls[0]
        assert args[:2] == ["ec2", "describe-spot-instance-requests"]
        assert f"Name=tag:{ec2.INSTANCE_TAG_KEY},Values=t1" in args
        assert f"Name=tag:{ec2.MANAGED_TAG_KEY},Values=true" in args
        # No state filter at all: `disabled` — the state of a persistent request
        # whose instance is stopped, i.e. exactly the one that most needs
        # sweeping — is NOT a documented value for the `state` FILTER (only
        # open|active|closed|cancelled|failed are). Filtering happens client-side
        # on the returned State instead.
        assert not any(str(a).startswith("Name=state") for a in args)
        assert action == "ec2:DescribeSpotInstanceRequests"

    def test_the_lookup_demands_the_managed_tag_too(self, monkeypatch):
        # The lookup feeds cancel_spot_requests, which CANCELS what it finds and
        # terminates the instances behind it — so it obeys the same ownership rule
        # as every other destructive path: kirocrew:managed=true says "we created
        # it", kirocrew:instance says "for this tag". The instance tag alone is a
        # plain user tag anyone can set, so a foreign request carrying it (or a
        # collision on a common tag like `dev`) would be cancelled and its box
        # terminated by `cloud destroy`. Both filters, ANDed by the API.
        json_calls, _ = self._record(
            monkeypatch, requests=[{"SpotInstanceRequestId": "sir-1", "State": "active"}]
        )
        ec2.find_spot_requests("t1", "dev", "us-east-1")
        args, _action = json_calls[0]
        assert args[args.index("--filters") + 1 :] == [
            f"Name=tag:{ec2.MANAGED_TAG_KEY},Values=true",
            f"Name=tag:{ec2.INSTANCE_TAG_KEY},Values=t1",
        ]

    def test_terminal_states_are_excluded_client_side(self, monkeypatch):
        # Exclude the terminal set rather than allow-list the live one, so a state
        # AWS adds later defaults to "sweep it" instead of leaking a billing box.
        self._record(
            monkeypatch,
            requests=[
                {"SpotInstanceRequestId": "sir-dead", "State": "cancelled"},
                {"SpotInstanceRequestId": "sir-closed", "State": "closed"},
                {"SpotInstanceRequestId": "sir-failed", "State": "failed"},
                {"SpotInstanceRequestId": "sir-live", "State": "disabled", "InstanceId": "i-0dead"},
                {"SpotInstanceRequestId": "sir-future", "State": "something-new"},
            ],
        )
        assert ec2.find_spot_requests("t1", "dev", "us-east-1") == [
            {"id": "sir-live", "instance_id": "i-0dead"},
            {"id": "sir-future", "instance_id": ""},
        ]

    def test_cancel_runs_before_delete_stack(self, monkeypatch):
        _, checked_calls = self._record(monkeypatch, requests=["sir-1", "sir-2"])
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["spot_sweep"]["cancelled"] == ["sir-1", "sir-2"]
        verbs = [args[1] for args, _ in checked_calls]
        assert verbs == ["cancel-spot-instance-requests", "delete-stack"], (
            "the request must be cancelled BEFORE the stack delete terminates the "
            "instance, or EC2 launches a replacement"
        )
        cancel_args, cancel_action = checked_calls[0]
        assert cancel_args[-2:] == ["sir-1", "sir-2"]
        assert cancel_action == "ec2:CancelSpotInstanceRequests"

    def test_on_demand_stack_costs_one_describe_and_cancels_nothing(self, monkeypatch):
        json_calls, checked_calls = self._record(monkeypatch, requests=[])
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["spot_sweep"]["cancelled"] == []
        assert len(json_calls) == 1
        assert [args[1] for args, _ in checked_calls] == ["delete-stack"]

    def test_denied_describe_warns_and_still_destroys(self, monkeypatch):
        # A principal without Describe/Cancel on spot requests must never be
        # BLOCKED from the uninstall path — whether the denial is also harmless
        # is a separate question, graded from the stack's Spot parameter by
        # grade_spot_sweep, not decided here.
        def denied(*a, **k):
            raise aws.AWSError("AccessDenied", action="ec2:DescribeSpotInstanceRequests")

        _, checked_calls = self._record(monkeypatch, requests=[])
        monkeypatch.setattr(aws, "checked_json", denied)
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["destroyed"] is True
        assert res["spot_sweep"]["cancelled"] == []
        assert [args[1] for args, _ in checked_calls] == ["delete-stack"]

    def test_denied_cancel_is_reported_not_swallowed(self, monkeypatch):
        # A denied cancel must be DISTINGUISHABLE from "nothing to cancel": the
        # request is still live and will hand out a replacement instance, so the
        # CLI must be able to warn instead of printing "you won't be billed".
        json_calls, checked_calls = self._record(monkeypatch, requests=["sir-1"])

        def denied(args, *a, action="", **k):
            checked_calls.append((args, action))
            if args[1] == "cancel-spot-instance-requests":
                raise aws.AWSError("AccessDenied", action="ec2:CancelSpotInstanceRequests")
            return ""

        monkeypatch.setattr(aws, "checked", denied)
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["destroyed"] is True
        assert res["spot_sweep"]["cancelled"] == []
        assert res["spot_sweep"]["failed"] == ["sir-1"]
        assert "AccessDenied" in res["spot_sweep"]["error"]
        assert "delete-stack" in [args[1] for args, _ in checked_calls]

    def test_denied_describe_is_reported_with_no_ids(self, monkeypatch):
        def denied(*a, **k):
            raise aws.AWSError("AccessDenied", action="ec2:DescribeSpotInstanceRequests")

        self._record(monkeypatch, requests=[])
        monkeypatch.setattr(aws, "checked_json", denied)
        sweep = ec2.cancel_spot_requests("t1", "dev", "us-east-1")
        assert sweep["cancelled"] == [] and sweep["failed"] == []
        assert "AccessDenied" in sweep["error"]

    def test_cancel_terminates_the_requests_instances(self, monkeypatch):
        # Cancelling an ACTIVE request leaves its running instance alive, and a
        # REPLACEMENT instance is not a stack resource — no delete-stack will ever
        # terminate it. So the sweep terminates what the requests point at.
        _, checked_calls = self._record(
            monkeypatch,
            requests=[
                {"SpotInstanceRequestId": "sir-1", "State": "active", "InstanceId": "i-0live"},
                {"SpotInstanceRequestId": "sir-2", "State": "open"},
            ],
        )
        sweep = ec2.cancel_spot_requests("t1", "dev", "us-east-1")
        assert sweep["cancelled"] == ["sir-1", "sir-2"]
        assert sweep["terminated"] == ["i-0live"]
        verbs = [args[1] for args, _ in checked_calls]
        assert verbs == ["cancel-spot-instance-requests", "terminate-instances"]
        term_args, term_action = checked_calls[1]
        assert term_args[-1] == "i-0live"
        assert term_action == "ec2:TerminateInstances"

    def test_denied_terminate_is_reported_and_cancel_still_counts(self, monkeypatch):
        # A replacement instance EC2 launched off a re-opened request may not carry
        # the managed tags, so the tag-gated TerminateInstances can be denied. The
        # cancel still succeeded, but the box is still billing — report both.
        self._record(
            monkeypatch,
            requests=[
                {"SpotInstanceRequestId": "sir-1", "State": "active", "InstanceId": "i-0orphan"}
            ],
        )

        def denied(args, *a, action="", **k):
            if args[1] == "terminate-instances":
                raise aws.AWSError("AccessDenied", action="ec2:TerminateInstances")
            return ""

        monkeypatch.setattr(aws, "checked", denied)
        sweep = ec2.cancel_spot_requests("t1", "dev", "us-east-1")
        assert sweep["cancelled"] == ["sir-1"]
        assert sweep["terminated"] == []
        assert sweep["terminate_failed"] == ["i-0orphan"]
        assert "AccessDenied" in sweep["terminate_error"]

    def test_stack_lookup_runs_before_the_mutating_sweep(self, monkeypatch):
        # Ordering guard for the whole teardown: find_stack FIRST, sweep second.
        # With the sweep first, a throttled/denied cloudformation:DescribeStacks
        # aborts AFTER the request was cancelled and the instance terminated, and
        # the CLI reports only "could not query stack" — no hint the box is gone.
        _, checked_calls = self._record(monkeypatch, requests=["sir-1"])
        order: list[str] = []
        monkeypatch.setattr(
            ec2, "find_stack", lambda *a, **k: order.append("find_stack") or {"StackName": "s"}
        )
        real_cancel = ec2.cancel_spot_requests
        monkeypatch.setattr(
            ec2,
            "cancel_spot_requests",
            lambda *a, **k: order.append("sweep") or real_cancel(*a, **k),
        )
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert order == ["find_stack", "sweep"]
        # …and the sweep is still ahead of delete-stack, which is what stops the
        # terminate from flipping the request open into a replacement instance.
        assert res["spot_sweep"]["cancelled"] == ["sir-1"]
        assert [args[1] for args, _ in checked_calls] == [
            "cancel-spot-instance-requests",
            "delete-stack",
        ]

    def test_the_stacks_own_instance_is_left_to_delete_stack(self, monkeypatch):
        # THE deletion-safety rule of this path: while the stack stands, its
        # instance is CloudFormation's to terminate. Terminating it here is not
        # equivalent — if the delete-stack that follows is refused (denied,
        # throttled) we have destroyed the box AND its DeleteOnTermination root
        # volume out from under a stack that still exists, so the user loses
        # ~/.kiro/crew and gets nothing they asked for. The request is still
        # cancelled first (that is what stops the replacement launch).
        _, checked_calls = self._record(
            monkeypatch,
            requests=[
                {"SpotInstanceRequestId": "sir-1", "State": "active", "InstanceId": "i-0stack"}
            ],
        )
        monkeypatch.setattr(
            ec2,
            "find_stack",
            lambda *a, **k: {
                "StackName": "kirocrew-t1",
                "Outputs": [{"OutputKey": "InstanceId", "OutputValue": "i-0stack"}],
            },
        )
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["spot_sweep"]["cancelled"] == ["sir-1"]
        assert res["spot_sweep"]["terminated"] == []
        assert [args[1] for args, _ in checked_calls] == [
            "cancel-spot-instance-requests",
            "delete-stack",
        ]

    def test_an_instance_that_is_not_the_stacks_is_still_terminated(self, monkeypatch):
        # A REPLACEMENT instance from an earlier re-opened request is not a stack
        # resource — no delete-stack will ever touch it, so the exclusion must be
        # exactly the stack's own id, not "skip the terminate on the stack path".
        _, checked_calls = self._record(
            monkeypatch,
            requests=[
                {"SpotInstanceRequestId": "sir-1", "State": "active", "InstanceId": "i-0stack"},
                {"SpotInstanceRequestId": "sir-2", "State": "active", "InstanceId": "i-0zombie"},
            ],
        )
        monkeypatch.setattr(
            ec2,
            "find_stack",
            lambda *a, **k: {
                "StackName": "kirocrew-t1",
                "Outputs": [{"OutputKey": "InstanceId", "OutputValue": "i-0stack"}],
            },
        )
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["spot_sweep"]["terminated"] == ["i-0zombie"]
        term_args = next(args for args, _ in checked_calls if args[1] == "terminate-instances")
        assert "i-0stack" not in term_args

    def test_the_orphan_path_terminates_everything_it_finds(self, monkeypatch):
        # No stack means no delete-stack is coming for any of them, so the caller
        # that sweeps orphans (the CLI / the dashboard route on `already_absent`)
        # passes no exclusions and every instance the requests point at is
        # terminated.
        self._record(
            monkeypatch,
            requests=[
                {"SpotInstanceRequestId": "sir-1", "State": "active", "InstanceId": "i-0orphan"}
            ],
        )
        assert ec2.cancel_spot_requests("t1", "dev", "us-east-1")["terminated"] == ["i-0orphan"]

    def test_no_stack_does_not_sweep_here(self, monkeypatch):
        # A --spot launch that CloudFormation rolled back can leave the request
        # behind with no stack at all — but that orphan is swept by the CLI on its
        # own `describe` miss (see test_cloud_cli's
        # test_destroy_sweeps_spot_request_when_no_stack_exists), which never
        # reaches destroy(). Sweeping here too would just re-add the mutation the
        # lookup-first ordering exists to avoid.
        _, checked_calls = self._record(monkeypatch, requests=["sir-orphan"])
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
        res = ec2.destroy("t1", "dev", "us-east-1")
        assert res["already_absent"] is True
        assert res["spot_sweep"] == _EMPTY_SWEEP
        assert checked_calls == []

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (
                aws.AWSError(
                    "ec2:DescribeSpotInstanceRequests failed: User: arn:aws:iam::1:user/u is "
                    "not authorized to perform: ec2:DescribeSpotInstanceRequests",
                    action="ec2:DescribeSpotInstanceRequests",
                    missing_action="ec2:DescribeSpotInstanceRequests",
                    stderr="is not authorized to perform: ec2:DescribeSpotInstanceRequests",
                ),
                ec2.SWEEP_ERROR_ACCESS_DENIED,
            ),
            (
                aws.AWSError(
                    "ec2:DescribeSpotInstanceRequests failed: UnauthorizedOperation",
                    action="ec2:DescribeSpotInstanceRequests",
                    stderr="UnauthorizedOperation",
                ),
                ec2.SWEEP_ERROR_ACCESS_DENIED,
            ),
            (
                aws.AWSError(
                    "ec2:DescribeSpotInstanceRequests failed: Throttling: Rate exceeded",
                    action="ec2:DescribeSpotInstanceRequests",
                    stderr="Throttling: Rate exceeded",
                ),
                ec2.SWEEP_ERROR_FAILED,
            ),
            (
                aws.AWSError(
                    "ec2:DescribeSpotInstanceRequests: could not parse aws JSON output: x",
                    action="ec2:DescribeSpotInstanceRequests",
                ),
                ec2.SWEEP_ERROR_FAILED,
            ),
        ],
    )
    def test_describe_failure_records_its_cause(self, monkeypatch, exc, expected):
        # The cause is reported rather than flattened into a bare error string
        # because the caller grades on it: a denial can be shrugged off when the
        # STACK says Spot=false (there was never a request), while throttling /
        # no network / unparseable JSON is a failure on any stack. Neither
        # verdict is reachable if both arrive as "the lookup failed".
        self._record(monkeypatch, requests=[])

        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(aws, "checked_json", boom)
        sweep = ec2.cancel_spot_requests("t1", "dev", "us-east-1")
        assert sweep["error_kind"] == expected
        assert sweep["error"] == str(exc)
        assert sweep["cancelled"] == [] and sweep["failed"] == []

    def test_agent_session_refusal_is_caught_not_raised(self, monkeypatch):
        # The chokepoint denies all three verbs (none is in _AGENT_READ_ALLOWLIST).
        # The CLI's no-stack sweep is the one mutating entry point with no
        # assert_human_action in front of it, so letting CloudActionDenied escape
        # turned an agent-session `cloud destroy` on a missing stack from a clean
        # "nothing to remove" into a hard failure. "Never raises" means never.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-123")
        sweep = ec2.cancel_spot_requests("t1", "dev", "us-east-1")
        assert sweep["error_kind"] == ec2.SWEEP_ERROR_AGENT_SESSION
        assert "agent session" in sweep["error"]
        assert sweep["cancelled"] == [] and sweep["failed"] == []
        assert sweep["terminate_failed"] == []

    def test_dry_run_still_makes_no_aws_call(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        monkeypatch.setattr(
            aws, "checked_json", lambda *a, **k: pytest.fail("dry run must not hit AWS")
        )
        assert ec2.destroy("t1", "dev", "us-east-1", dry_run=True)["dry_run"] is True


class TestDestroyRefusesToDeleteAfterABadSweep:
    """The delete is what CREATES the zombie: terminating the instance while its
    persistent request is still open makes EC2 launch a replacement outside the
    stack — untracked, and billing until someone finds it. So a --spot stack
    whose sweep could not prove the request is gone is NOT deleted.
    """

    _SPOT_STACK = {
        "StackName": "kirocrew-t1",
        "Parameters": [{"ParameterKey": "Spot", "ParameterValue": "true"}],
    }

    @staticmethod
    def _run(monkeypatch, *, stack, sweep):
        """destroy() with a canned stack + sweep; returns (result, delete_calls)."""
        deletes: list = []
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: dict(stack))
        monkeypatch.setattr(ec2, "cancel_spot_requests", lambda *a, **k: dict(sweep))
        monkeypatch.setattr(
            aws, "checked", lambda args, *a, **k: deletes.append(args[1]) or ""
        )
        monkeypatch.setattr(ec2, "wait_for_delete", lambda *a, **k: True)
        return ec2.destroy("t1", "dev", "us-east-1"), deletes

    @pytest.mark.parametrize(
        "sweep_kw",
        [
            # The cancel was refused: the request is provably still live.
            {"failed": ["sir-1"], "error": "AccessDenied"},
            # The lookup never answered, so a live request cannot be ruled out.
            # Denied counts too, deliberately: on a Spot=true stack the denial
            # hides exactly the request that zombies (grade_spot_sweep already
            # calls it a failure for the same reason).
            {"error": "AccessDenied", "error_kind": ec2.SWEEP_ERROR_ACCESS_DENIED},
            {"error": "refused from an agent session",
             "error_kind": ec2.SWEEP_ERROR_AGENT_SESSION},
            {"error": "Throttling: Rate exceeded", "error_kind": ec2.SWEEP_ERROR_FAILED},
        ],
    )
    def test_a_spot_stack_is_left_standing(self, monkeypatch, sweep_kw):
        sweep = dict(_EMPTY_SWEEP, **sweep_kw)
        res, deletes = self._run(monkeypatch, stack=self._SPOT_STACK, sweep=sweep)
        assert deletes == [], "delete-stack must not run while the request may be open"
        assert res["destroyed"] is False
        # `aborted` is the whole branch every caller reads: a failed sweep is the
        # only thing that stops the delete, so there is no second reason code to
        # tell apart.
        assert res["aborted"] is True
        # The outcome IS the message: the caller renders its ids and remedies,
        # which is why this is a returned result and not a raised exception.
        assert res["spot_sweep"] == sweep
        assert res["stack_is_spot"] is True

    def test_an_on_demand_stack_deletes_exactly_as_before(self, monkeypatch):
        # Nothing can relaunch without a request, so a failed sweep on a
        # Spot=false stack must not start refusing teardowns — that would break
        # the uninstall path for every user still on the old launcher policy.
        res, deletes = self._run(
            monkeypatch,
            stack={"StackName": "kirocrew-t1"},
            sweep=dict(_EMPTY_SWEEP, error="AccessDenied", error_kind=ec2.SWEEP_ERROR_FAILED),
        )
        assert deletes == ["delete-stack"]
        assert res["destroyed"] is True
        assert "aborted" not in res

    def test_a_failed_terminate_still_deletes(self, monkeypatch):
        # The cancel SUCCEEDED, so the request is gone and cannot relaunch
        # anything; the instance we could not kill is leftover work to report,
        # not a reason to leave the stack (and its bill) standing.
        res, deletes = self._run(
            monkeypatch,
            stack=self._SPOT_STACK,
            sweep=dict(
                _EMPTY_SWEEP,
                cancelled=["sir-1"],
                terminate_failed=["i-0orphan"],
                terminate_error="AccessDenied",
            ),
        )
        assert deletes == ["delete-stack"]
        assert res["destroyed"] is True
        assert "aborted" not in res

    def test_a_clean_sweep_on_a_spot_stack_deletes(self, monkeypatch):
        res, deletes = self._run(
            monkeypatch,
            stack=self._SPOT_STACK,
            sweep=dict(_EMPTY_SWEEP, cancelled=["sir-1"]),
        )
        assert deletes == ["delete-stack"]
        assert res["destroyed"] is True

    @pytest.mark.parametrize(
        "sweep_kw,risk",
        [
            ({}, False),
            ({"cancelled": ["sir-1"], "terminated": ["i-0abc"]}, False),
            ({"failed": ["sir-1"], "error": "AccessDenied"}, True),
            ({"error": "boom", "error_kind": ec2.SWEEP_ERROR_FAILED}, True),
            # A cancel that worked leaves nothing that can relaunch, whatever the
            # terminate did.
            ({"cancelled": ["sir-1"], "terminate_failed": ["i-0x"],
              "terminate_error": "AccessDenied"}, False),
        ],
    )
    def test_live_risk_is_exactly_an_unproven_cancel(self, sweep_kw, risk):
        assert ec2.spot_sweep_leaves_live_risk(dict(_EMPTY_SWEEP, **sweep_kw)) is risk


class TestProbeSpotRequests:
    """The read-only half of the sweep. Cancelling a `disabled` request makes EC2
    terminate its STOPPED instance, so a surface that has to ask the user first
    (the CLI's no-stack path) needs to look without touching anything.
    """

    def test_it_returns_what_it_found_and_a_clean_outcome(self, monkeypatch):
        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda *a, **k: {
                "SpotInstanceRequests": [
                    {"SpotInstanceRequestId": "sir-1", "State": "disabled",
                     "InstanceId": "i-0stopped"}
                ]
            },
        )
        monkeypatch.setattr(
            aws, "checked", lambda *a, **k: pytest.fail("the probe must not mutate anything")
        )
        found, outcome = ec2.probe_spot_requests("t1", "dev", "us-east-1")
        assert found == [{"id": "sir-1", "instance_id": "i-0stopped"}]
        assert outcome == _EMPTY_SWEEP

    @pytest.mark.parametrize(
        "exc,kind",
        [
            (
                aws.AWSError(
                    "AccessDenied",
                    action="ec2:DescribeSpotInstanceRequests",
                    stderr="UnauthorizedOperation",
                ),
                ec2.SWEEP_ERROR_ACCESS_DENIED,
            ),
            (
                aws.AWSError("Throttling", action="ec2:DescribeSpotInstanceRequests"),
                ec2.SWEEP_ERROR_FAILED,
            ),
        ],
    )
    def test_a_failed_lookup_comes_back_as_a_gradable_outcome(self, monkeypatch, exc, kind):
        # Never raises, and grades identically to the cancelling path — the CLI
        # hands this straight to grade_spot_sweep, so the two must not word the
        # same failure differently.
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(aws, "checked_json", boom)
        found, outcome = ec2.probe_spot_requests("t1", "dev", "us-east-1")
        assert found == []
        assert outcome["error"] == str(exc)
        assert outcome["error_kind"] == kind

    def test_the_agent_guard_is_caught_here_too(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-123")
        found, outcome = ec2.probe_spot_requests("t1", "dev", "us-east-1")
        assert found == []
        assert outcome["error_kind"] == ec2.SWEEP_ERROR_AGENT_SESSION


class TestStackUsesSpot:
    """Whether a teardown can leave a Spot request behind is a property of the
    STACK, not of the principal running destroy — an admin can launch --spot and
    a restricted profile can destroy it, so "this profile couldn't have created
    a Spot stack" is not a safe reason to reassure anyone.
    """

    @pytest.mark.parametrize(
        "params,expected",
        [
            ([{"ParameterKey": "Spot", "ParameterValue": "true"}], True),
            ([{"ParameterKey": "Spot", "ParameterValue": "True"}], True),
            ([{"ParameterKey": "Spot", "ParameterValue": "false"}], False),
            # No Spot parameter at all: a stack from before the flag existed.
            ([{"ParameterKey": "InstanceType", "ParameterValue": "t4g.large"}], False),
            ([], False),
        ],
    )
    def test_reads_the_spot_template_parameter(self, params, expected):
        assert ec2.stack_uses_spot({"Parameters": params}) is expected

    @pytest.mark.parametrize("stack", [None, {}, {"Parameters": None}, {"Parameters": "junk"}])
    def test_unreadable_parameters_read_as_on_demand(self, stack):
        # Only ever used to make the caller LOUDER, so the safe default when the
        # payload is missing or malformed is the pre---spot behaviour (quiet)
        # rather than a false Spot alarm on every on-demand destroy.
        assert ec2.stack_uses_spot(stack) is False

    def test_destroy_reports_the_stacks_spot_parameter_without_an_extra_call(self, monkeypatch):
        # It comes off the describe-stacks payload find_stack already fetched —
        # if this ever needs its own AWS call, the free-signal argument is gone.
        monkeypatch.setattr(
            ec2,
            "find_stack",
            lambda *a, **k: {
                "StackName": "kirocrew-t1",
                "Parameters": [{"ParameterKey": "Spot", "ParameterValue": "true"}],
            },
        )
        monkeypatch.setattr(ec2, "cancel_spot_requests", lambda *a, **k: dict(_EMPTY_SWEEP))
        monkeypatch.setattr(aws, "checked", lambda *a, **k: "")
        monkeypatch.setattr(ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(
            aws, "checked_json", lambda *a, **k: pytest.fail("no extra describe for the parameter")
        )
        assert ec2.destroy("t1", "dev", "us-east-1")["stack_is_spot"] is True
        assert ec2.destroy("t1", "dev", "us-east-1", wait=False)["stack_is_spot"] is True

    def test_destroy_reports_on_demand_for_a_stack_without_the_parameter(self, monkeypatch):
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: {"StackName": "kirocrew-t1"})
        monkeypatch.setattr(ec2, "cancel_spot_requests", lambda *a, **k: dict(_EMPTY_SWEEP))
        monkeypatch.setattr(aws, "checked", lambda *a, **k: "")
        monkeypatch.setattr(ec2, "wait_for_delete", lambda *a, **k: True)
        assert ec2.destroy("t1", "dev", "us-east-1")["stack_is_spot"] is False


class TestSpotStartFailureHint:
    """A failed `cloud start` on a --spot crew is almost always an EC2
    interruption stop — the one Spot event every user eventually meets. Raw, the
    AWS error reads like a broken box, and the obvious "fix" (delete it, launch a
    new one) destroys the DeleteOnTermination root volume the interruption
    deliberately preserved. So the failure path says so.
    """

    _SPOT_STACK = {"Parameters": [{"ParameterKey": "Spot", "ParameterValue": "true"}]}

    def test_a_spot_stack_gets_the_interruption_hint(self, monkeypatch):
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: dict(self._SPOT_STACK))
        hint = ec2.spot_start_failure_hint("t1", "dev", "us-east-1")
        blob = " ".join(hint)
        assert "INTERRUPTION" in blob
        assert "Only EC2 can restart" in blob
        # The two claims that stop a user from destroying the box to fix it.
        assert "data is intact" in blob
        assert "Do NOT destroy" in blob

    def test_an_on_demand_stack_gets_nothing(self, monkeypatch):
        # A start that fails on an on-demand box has nothing to do with Spot;
        # inventing an interruption story would send the user off to wait for an
        # auto-resume that is never coming.
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: {"Parameters": []})
        assert ec2.spot_start_failure_hint("t1", "dev", "us-east-1") == []

    @pytest.mark.parametrize(
        "exc",
        [
            aws.AWSError("throttled", action="cloudformation:DescribeStacks"),
            RuntimeError("aws CLI vanished"),
        ],
    )
    def test_a_failed_lookup_stays_silent_instead_of_raising(self, monkeypatch, exc):
        # This runs while a REAL error is already being reported. A second
        # failure here must not replace the message the user needs, and must
        # certainly not turn a reported failure into a traceback.
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(ec2, "find_stack", boom)
        assert ec2.spot_start_failure_hint("t1", "dev", "us-east-1") == []

    def test_a_successful_start_never_looks_the_parameter_up(self, monkeypatch):
        # The happy path must cost exactly what it always did: the hint's own
        # describe-stacks belongs to the failure path alone.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-1"})
        monkeypatch.setattr(aws, "checked", lambda *a, **k: "")
        monkeypatch.setattr(
            ec2, "find_stack", lambda *a, **k: pytest.fail("no stack lookup on a good start")
        )
        assert ec2.start("t1", "dev", "us-east-1")["action"] == "start"


class TestGradeSpotSweep:
    """The single grader both surfaces use. `kirocrew cloud destroy` renders it
    to the terminal and exits non-zero on ``failed``; the dashboard destroy route
    returns the same lines as response warnings. They must never disagree about
    whether a teardown left something billing.
    """

    @staticmethod
    def _sweep(**kw):
        out = dict(_EMPTY_SWEEP)
        out.update(kw)
        return out

    def test_a_clean_sweep_says_nothing(self):
        grade = ec2.grade_spot_sweep(
            self._sweep(cancelled=["sir-1"], terminated=["i-0abc"]), "t1", "dev", "us-east-1"
        )
        assert grade == {"failed": False, "problems": [], "notes": []}

    def test_a_failed_cancel_carries_the_ids_and_the_runnable_remedy(self):
        grade = ec2.grade_spot_sweep(
            self._sweep(failed=["sir-1"], error="AccessDenied"), "t1", "dev", "us-east-1"
        )
        assert grade["failed"] is True
        (problem,) = grade["problems"]
        assert "sir-1" in problem["summary"]
        # The raw AWS error first, the runnable remedy LAST — the order every
        # surface depends on: the terminal ends on the actionable line, and the
        # dashboard peels the trailing command off the flattened line to render
        # it as copyable code (a detail after the command would be dragged in).
        assert "AccessDenied" in problem["details"][0]
        assert problem["details"][-1].endswith(
            "aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1 "
            "--profile dev --region us-east-1"
        )

    def test_a_failed_terminate_carries_the_ids_and_the_runnable_remedy(self):
        grade = ec2.grade_spot_sweep(
            self._sweep(
                cancelled=["sir-1"], terminate_failed=["i-0orphan"], terminate_error="AccessDenied"
            ),
            "t1",
            "dev",
            "us-east-1",
        )
        assert grade["failed"] is True
        (problem,) = grade["problems"]
        assert "i-0orphan" in problem["summary"]
        assert "AccessDenied" in problem["details"][0]
        assert problem["details"][-1].endswith(
            "aws ec2 terminate-instances --instance-ids i-0orphan --profile dev "
            "--region us-east-1"
        )

    @pytest.mark.parametrize(
        "kind,stack_is_spot,failed",
        [
            # THE fix: the destroying principal need not be the launching one, so
            # a denied describe on a stack we KNOW is Spot=true hides a possibly
            # live persistent request — a failure, not a shrug.
            (ec2.SWEEP_ERROR_ACCESS_DENIED, True, True),
            (ec2.SWEEP_ERROR_ACCESS_DENIED, False, False),
            (ec2.SWEEP_ERROR_ACCESS_DENIED, None, False),
            # Same reasoning for the agent-guard refusal (unreachable on the
            # stack path today, since destroy() asserts a human action first —
            # the grading must not silently depend on that).
            (ec2.SWEEP_ERROR_AGENT_SESSION, True, True),
            (ec2.SWEEP_ERROR_AGENT_SESSION, False, False),
            (ec2.SWEEP_ERROR_AGENT_SESSION, None, False),
            # Throttling/network/JSON stays loud even for Spot=false: a stack
            # re-deployed without --spot can still carry a request left by its
            # Spot generation.
            (ec2.SWEEP_ERROR_FAILED, True, True),
            (ec2.SWEEP_ERROR_FAILED, False, True),
            (ec2.SWEEP_ERROR_FAILED, None, True),
        ],
    )
    def test_an_unanswered_lookup_is_graded_by_the_stack_not_the_principal(
        self, kind, stack_is_spot, failed
    ):
        grade = ec2.grade_spot_sweep(
            self._sweep(error="lookup boom", error_kind=kind),
            "t1",
            "dev",
            "us-east-1",
            stack_is_spot=stack_is_spot,
        )
        assert grade["failed"] is failed
        if failed:
            (problem,) = grade["problems"]
            assert problem["summary"].startswith(
                "Could NOT check for a leftover persistent Spot request"
            )
            assert "lookup boom" in problem["details"][0]
            assert (
                "aws ec2 describe-spot-instance-requests --filters "
                f"Name=tag:{ec2.MANAGED_TAG_KEY},Values=true "
                f"Name=tag:{ec2.INSTANCE_TAG_KEY},Values=t1 --profile dev --region us-east-1"
                in problem["details"][1]
            )
            assert grade["notes"] == []
        else:
            assert grade["problems"] == []
            assert len(grade["notes"]) == 1

    def test_the_denied_note_is_justified_by_the_stack_when_there_is_one(self):
        (note,) = ec2.grade_spot_sweep(
            self._sweep(error="AccessDenied", error_kind=ec2.SWEEP_ERROR_ACCESS_DENIED),
            "t1",
            stack_is_spot=False,
        )["notes"]
        assert "no permission" in note
        # The reassurance now comes from the stack, not from an inference about
        # which policy the caller is on.
        assert "on-demand (Spot=false)" in note

    def test_the_denied_note_admits_it_cannot_rule_a_request_out_with_no_stack(self):
        (note,) = ec2.grade_spot_sweep(
            self._sweep(error="AccessDenied", error_kind=ec2.SWEEP_ERROR_ACCESS_DENIED), "t1"
        )["notes"]
        # No stack means no Spot parameter to appeal to, so this may not claim
        # there was never a request — only that it could not look.
        assert "no permission" in note
        assert "nothing to prove it either way" in note

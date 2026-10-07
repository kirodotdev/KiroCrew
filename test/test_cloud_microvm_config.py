"""The ``microvm`` block: complete means the lane exists, anything else means it does not."""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud.config import CloudConfig
from kiro_crew.cloud.microvm.api import MAX_LIFETIME_SECONDS
from kiro_crew.cloud.microvm.config import MicroVmConfig

_COMPLETE = {
    # A BASE image, a build role and a recipe bucket: the lane BUILDS a crew's
    # image from this base plus that crew's own bundle, as the Fargate lane builds
    # a task definition. The prebuilt ``image_identifier`` / ``image_version`` pair
    # is an optional override and is deliberately absent here, so the normal path
    # is the one the fixture exercises.
    "base_image_arn": "arn:aws:lambda:us-east-1:123456789012:microvm-image/al2023-base",
    "build_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-build",
    "recipe_bucket": "kirocrew-microvm-recipes-123456789012-us-east-1",
    # The crew bundle ``packaging.build`` produced -- the same artifact the Fargate
    # lane's crew layer is built from. Required on the build path, because a block
    # that can build and names no bundle would register a lane whose every launch
    # fails at preflight.
    "bundle_dir": "/srv/kirocrew/bundles/demo",
    "kms_key_id": "arn:aws:kms:us-east-1:123456789012:key/" "11111111-2222-3333-4444-555555555555",
    # Required, and measured so against real SSM: ``ssm:CreateActivation`` refuses
    # the call with no ``--iam-role``, so a block without this would register a
    # lane whose every launch fails at its first AWS call.
    "activation_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-crew",
    # The crew's MODEL credential, by reference. Required because the launch tag
    # is minted by the launcher, so a name derived from it cannot exist before
    # the launch that invents it.
    "identity_secret_ref": "kirocrew/identity/demo-crew",
}


def _block(**overrides):
    data = dict(_COMPLETE)
    data.update(overrides)
    return data


class TestCompleteness:
    def test_a_complete_block_is_a_lane(self):
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None and config.is_complete()

    @pytest.mark.parametrize("missing", sorted(_COMPLETE))
    def test_every_required_field_is_required(self, missing):
        """A half-written block leaves the lane unregistered, not registered and refusing."""
        assert MicroVmConfig.from_mapping(_block(**{missing: ""})) is None

    def test_a_bare_image_name_with_a_slash_is_refused(self):
        """``RunMicrovm`` rejects anything that is not an ARN or an image id."""
        assert MicroVmConfig.from_mapping(_block(base_image_arn="my/image")) is None

    def test_an_image_id_without_a_full_arn_is_accepted(self):
        config = MicroVmConfig.from_mapping(_block(base_image_arn="al2023-microvm-base"))
        assert config is not None

    def test_an_aws_managed_base_image_arn_is_accepted(self):
        """The managed base images the service offers, which is what the lane
        actually builds on.

        Their ARN is not the owner-account shape: the account field is the literal
        ``aws`` and the separator before the name is a COLON, not a slash. A
        pattern accepting only ``:<twelve digits>:microvm-image/<name>`` rejects
        the one real base image available, and because an incomplete block means
        an ABSENT lane rather than a refusing one, the lane then silently does not
        register -- no error names the ARN. Taken from
        ``ListManagedMicrovmImages`` in us-east-1.
        """
        config = MicroVmConfig.from_mapping(
            _block(base_image_arn="arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1")
        )
        assert config is not None and config.is_complete()

    def test_the_owner_account_image_arn_is_still_accepted(self):
        """Widening for the managed shape must not drop the shape an operator's
        own built image has -- it is what every rebuild of this lane produces."""
        config = MicroVmConfig.from_mapping(
            _block(
                base_image_arn=(
                    "arn:aws:lambda:us-east-1:123456789012:microvm-image/kirocrew-crew-abc"
                )
            )
        )
        assert config is not None and config.is_complete()

    def test_a_managed_arn_in_another_partition_is_accepted(self):
        """The partition is read, not assumed: the same lane in an isolated
        partition names ``aws-us-gov`` and is otherwise identical."""
        config = MicroVmConfig.from_mapping(
            _block(base_image_arn="arn:aws-us-gov:lambda:us-gov-west-1:aws:microvm-image:al2023-1")
        )
        assert config is not None

    def test_a_word_meaning_newest_is_not_a_version(self):
        """An unset version resolves at call time, so a mid-launch build retargets it."""
        pinned = {"image_identifier": "arn:aws:lambda:us-east-1:123456789012:microvm-image/x"}
        assert MicroVmConfig.from_mapping(_block(image_version="latest", **pinned)) is None
        assert MicroVmConfig.from_mapping(_block(image_version="newest", **pinned)) is None

    def test_a_numeric_version_is_accepted(self):
        assert (
            MicroVmConfig.from_mapping(
                _block(
                    image_identifier="arn:aws:lambda:us-east-1:123456789012:microvm-image/x",
                    image_version="12",
                )
            )
            is not None
        )

    def test_an_alias_is_an_acceptable_kms_reference(self):
        assert MicroVmConfig.from_mapping(_block(kms_key_id="alias/kc-lane")) is not None

    def test_a_malformed_bucket_name_is_refused(self):
        assert MicroVmConfig.from_mapping(_block(recipe_bucket="Not_A_Bucket")) is None

    def test_a_malformed_secret_prefix_is_refused(self):
        assert MicroVmConfig.from_mapping(_block(secret_path_prefix="/leading")) is None


class TestTypes:
    def test_a_non_object_block_is_refused(self):
        for value in ("hello", [1, 2], 42, None, True):
            assert MicroVmConfig.from_mapping(value) is None

    def test_a_string_field_of_the_wrong_type_drops_the_block(self):
        """``str()`` on a raw value made ``false`` the non-empty string "False"."""
        assert MicroVmConfig.from_mapping(_block(build_role_arn=False)) is None
        assert MicroVmConfig.from_mapping(_block(recipe_bucket=7)) is None

    def test_an_oversized_string_drops_the_block(self):
        assert MicroVmConfig.from_mapping(_block(secret_path_prefix="a" * 4000)) is None

    def test_an_absent_lifetime_takes_the_engine_default(self):
        """ONE HOUR, not the platform's maximum. The home is not kept past the
        wall on this lane, so the default is how much conversation an owner can
        lose by walking away -- and that is a different number from the longest
        lifetime the platform will allow."""
        from kiro_crew.cloud.microvm.engine import DEFAULT_WALL_SECONDS

        config = MicroVmConfig.from_mapping(_block())
        assert config is not None and config.wall_seconds is None
        assert config.launch_spec().wall_seconds == DEFAULT_WALL_SECONDS
        assert DEFAULT_WALL_SECONDS == 3600
        assert DEFAULT_WALL_SECONDS < MAX_LIFETIME_SECONDS

    def test_a_present_lifetime_is_carried_through(self):
        config = MicroVmConfig.from_mapping(_block(wall_seconds=900))
        assert config is not None and config.launch_spec().wall_seconds == 900

    def test_a_boolean_is_not_a_lifetime(self):
        """``bool`` is an ``int`` subclass, so ``true`` would read as one second."""
        assert MicroVmConfig.from_mapping(_block(wall_seconds=True)) is None

    def test_a_fractional_lifetime_is_refused(self):
        assert MicroVmConfig.from_mapping(_block(wall_seconds=900.5)) is None

    def test_an_explicit_null_lifetime_drops_the_block(self):
        """A present ``null`` is a value of the wrong type, not an absent key."""
        assert MicroVmConfig.from_mapping(_block(wall_seconds=None)) is None

    @pytest.mark.parametrize("wall", [0, -1, MAX_LIFETIME_SECONDS + 1])
    def test_a_lifetime_the_platform_refuses_drops_the_block(self, wall):
        """Otherwise the lane registers and its first launch raises."""
        assert MicroVmConfig.from_mapping(_block(wall_seconds=wall)) is None


class TestLaunchRecipient:
    def test_it_names_the_image_and_the_key_in_full(self):
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None
        recipient = config.launch_recipient()
        assert _COMPLETE["base_image_arn"] in recipient
        assert _COMPLETE["kms_key_id"] in recipient

    def test_it_names_the_bundle_the_image_will_be_built_from(self):
        """On the build path the bundle IS what will run, so the owner confirms it."""
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None
        assert _COMPLETE["bundle_dir"] in config.launch_recipient()

    def test_an_incomplete_block_has_nothing_to_confirm(self):
        assert MicroVmConfig(base_image_arn="x").launch_recipient() == ""


class TestABuildableBlockNeedsABundle:
    """The bundle is required exactly when a build will run, and not otherwise."""

    def test_a_block_that_can_build_but_names_no_bundle_is_no_lane(self):
        """It would otherwise register a lane whose every launch failed at preflight."""
        assert MicroVmConfig.from_mapping(_block(bundle_dir="")) is None

    def test_a_prebuilt_pin_needs_no_bundle(self):
        """There is nothing to build, so demanding the input to that build is asking
        the operator for an artifact nothing reads."""
        config = MicroVmConfig.from_mapping(
            _block(
                bundle_dir="",
                image_identifier="arn:aws:lambda:us-east-1:123456789012:microvm-image/x",
                image_version="4",
            )
        )
        assert config is not None and config.is_complete()

    def test_the_bundles_existence_is_not_checked_here(self, tmp_path):
        """Completeness is read every time the roster is, so a bundle on a volume
        that is not mounted yet must not make the lane blink out of the roster --
        preflight reads the directory and reports a reason."""
        config = MicroVmConfig.from_mapping(_block(bundle_dir=str(tmp_path / "not-created-yet")))
        assert config is not None and config.is_complete()

    def test_it_reaches_the_engine_spec(self):
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None
        assert config.launch_spec().bundle_dir == _COMPLETE["bundle_dir"]


class TestCloudConfigWiring:
    def test_the_block_round_trips_through_the_file(self, tmp_path):
        path = tmp_path / "cloud.json"
        path.write_text(json.dumps({"region": "us-west-2", "microvm": _block()}))
        record = CloudConfig.load(path)
        assert record.region == "us-west-2"
        config = record.microvm_config()
        assert config is not None and config.build_role_arn.endswith("kirocrew-microvm-build")

    def test_an_absent_block_is_no_lane(self, tmp_path):
        path = tmp_path / "cloud.json"
        path.write_text(json.dumps({"region": "us-west-2"}))
        assert CloudConfig.load(path).microvm_config() is None

    def test_the_raw_block_is_kept_as_written(self, tmp_path):
        """A reader sees the operator's half-finished edit, not a sanitized version."""
        path = tmp_path / "cloud.json"
        path.write_text(json.dumps({"microvm": {"image_version": "7"}}))
        record = CloudConfig.load(path)
        assert record.microvm == {"image_version": "7"}
        assert record.microvm_config() is None

    def test_a_malformed_microvm_block_does_not_break_the_fargate_block(self, tmp_path):
        path = tmp_path / "cloud.json"
        path.write_text(json.dumps({"microvm": "nonsense", "region": "eu-west-1"}))
        record = CloudConfig.load(path)
        assert record.microvm_config() is None
        assert record.region == "eu-west-1"


class TestProvisionerRegistration:
    def test_the_lane_is_absent_without_a_block(self, monkeypatch):
        from kiro_crew.platform import defaults

        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider, "_microvm_config", staticmethod(lambda: None)
        )
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider, "_fargate_config", staticmethod(lambda: None)
        )
        provider = defaults.DefaultRemoteProvisionerProvider()
        assert [row.id for row in provider.provisioners()] == [defaults.BUILTIN_PROVISIONER_ID]

    def test_a_complete_block_offers_the_lane(self, monkeypatch):
        """Registration is what makes a lane reachable, and it is reachable now.

        It was deliberately withheld while the guest half was absent, because a
        crew launched then either never enrolled or ran with no suspend and no
        self-pack until the platform terminated it and its home went with the disk
        -- losing the crew being the outcome this lane exists to remove. The guest
        half serves the lifecycle hooks and reads the wall-clock edges the run
        payload carries, so the offer stands.
        """
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_microvm_config",
            staticmethod(lambda: config),
        )
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider, "_fargate_config", staticmethod(lambda: None)
        )
        rows = {row.id: row for row in defaults.DefaultRemoteProvisionerProvider().provisioners()}
        assert defaults.MICROVM_PROVISIONER_ID in rows
        row = rows[defaults.MICROVM_PROVISIONER_ID]
        # The owner confirms the base, the bundle and the key, spelled out: a
        # value obtainable only by attempting a launch and reading the refusal
        # makes confirming a ritual rather than a decision.
        assert row.confirm_before_launch == config.launch_recipient()
        assert _COMPLETE["bundle_dir"] in row.confirm_before_launch
        assert _COMPLETE["kms_key_id"] in row.confirm_before_launch

    def test_an_incomplete_block_offers_nothing(self, monkeypatch):
        """A row whose every launch is refused is worse than no row."""
        from kiro_crew.platform import defaults

        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_microvm_config",
            staticmethod(lambda: None),
        )
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider, "_fargate_config", staticmethod(lambda: None)
        )
        rows = {row.id: row for row in defaults.DefaultRemoteProvisionerProvider().provisioners()}
        assert defaults.MICROVM_PROVISIONER_ID not in rows
        assert list(rows) == [defaults.BUILTIN_PROVISIONER_ID]

    def test_the_descriptor_and_its_recipient_are_ready_for_that_commit(self):
        """What the row WILL carry, pinned so re-enabling it is one append.

        The descriptor and the confirmation string are the parts a reviewer of that
        later commit should not have to re-derive, so they are built and checked
        here even though nothing publishes them yet.
        """
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        assert config is not None
        assert defaults.MICROVM_REMOTE_PROVISIONER.kind == defaults.MICROVM_PROVISIONER_ID
        recipient = config.launch_recipient()
        assert _COMPLETE["base_image_arn"] in recipient
        assert _COMPLETE["kms_key_id"] in recipient

    def test_the_engine_still_answers_for_the_id(self, monkeypatch):
        """Withholding the OFFER is not withdrawing the lane.

        A caller that names the id explicitly still gets the engine, which is what
        keeps the launch path testable while the row is unpublished.
        """
        from kiro_crew.cloud.microvm.engine import MicroVmLaunchEngine
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_microvm_config",
            staticmethod(lambda: config),
        )
        monkeypatch.setattr(
            "kiro_crew.sandbox.require_unaliased_cloud_config", lambda *a, **k: None
        )
        engine = defaults.DefaultRemoteProvisionerProvider().engine_for(
            defaults.MICROVM_PROVISIONER_ID
        )
        assert isinstance(engine, MicroVmLaunchEngine)

    def test_the_steps_do_not_claim_an_instance_is_created(self):
        """The built-in wording is false here: nothing is installed at launch."""
        from kiro_crew.platform import defaults

        labels = dict(defaults.MICROVM_REMOTE_PROVISIONER.step_labels)
        assert set(labels) == {"preflight", "provision", "signin", "connect"}
        assert "install" not in " ".join(labels.values()).lower()

    def test_an_unconfigured_lane_raises_the_same_keyerror_as_an_unknown_one(self, monkeypatch):
        from kiro_crew.platform import defaults

        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider, "_microvm_config", staticmethod(lambda: None)
        )
        provider = defaults.DefaultRemoteProvisionerProvider()
        with pytest.raises(KeyError):
            provider.engine_for(defaults.MICROVM_PROVISIONER_ID)
        with pytest.raises(KeyError):
            provider.engine_for("no-such-lane")

    def test_a_configured_lane_hands_out_a_microvm_engine(self, monkeypatch):
        from kiro_crew.cloud.microvm.engine import MicroVmLaunchEngine
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_microvm_config",
            staticmethod(lambda: config),
        )
        monkeypatch.setattr(
            "kiro_crew.sandbox.require_unaliased_cloud_config", lambda *a, **k: None
        )
        engine = defaults.DefaultRemoteProvisionerProvider().engine_for(
            defaults.MICROVM_PROVISIONER_ID
        )
        assert isinstance(engine, MicroVmLaunchEngine)
        assert engine.spec is not None
        assert engine.spec.build_role_arn.endswith("kirocrew-microvm-build")

    def test_the_ec2_lane_is_untouched(self, monkeypatch):
        """Adding a third lane must not change what the first one hands out."""
        from kiro_crew.cloud.launch_engine import RealLaunchEngine
        from kiro_crew.platform import defaults

        engine = defaults.DefaultRemoteProvisionerProvider().engine_for(
            defaults.BUILTIN_PROVISIONER_ID
        )
        assert isinstance(engine, RealLaunchEngine)


class TestActivationRoleIsRequired:
    """The base template published a role the engine had nowhere to read.

    ``ssm:CreateActivation`` refuses the call without ``--iam-role``, so a block
    that omits this registers a lane whose every launch fails at its first AWS
    call -- exactly the offered-and-refusing state ``is_complete`` exists to
    prevent.
    """

    def test_a_block_without_it_is_not_a_lane(self):
        assert MicroVmConfig.from_mapping(_block(activation_role_arn="")) is None

    def test_a_block_with_it_is_a_lane(self):
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None and config.activation_role_arn

    def test_it_reaches_the_engine_spec(self):
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None
        assert (
            config.launch_spec().activation_role_arn
            == "arn:aws:iam::123456789012:role/kirocrew-microvm-crew"
        )

    def test_a_role_name_is_accepted_as_well_as_an_arn(self):
        """The template outputs an ARN; making the operator transcribe a name is
        how a one-character error becomes a launch failure."""
        config = MicroVmConfig.from_mapping(_block(activation_role_arn="kirocrew-microvm-crew"))
        assert config is not None

    def test_it_is_distinct_from_the_execution_role(self):
        """Two roles, two jobs: one the VM runs as, one the SSM node registers under."""
        config = MicroVmConfig.from_mapping(_block())
        assert config is not None
        assert config.execution_role_arn == ""
        assert config.activation_role_arn != config.execution_role_arn


class TestTheBaseTemplate:
    """Two properties of the base stack that CloudFormation Lint cannot judge.

    Both are about what a stack DELETE leaves behind, which is the operation
    someone runs while tidying up and the one that can lose a crew's home.
    """

    @staticmethod
    def _template() -> str:
        from pathlib import Path

        import kiro_crew.cloud as cloud_pkg

        path = Path(cloud_pkg.__file__).parent / "templates" / "kirocrew-microvm-base.yaml"
        return path.read_text(encoding="utf-8")

    def test_the_lane_key_is_retained(self):
        """A secret encrypted under a key that was scheduled for deletion is
        unreadable, and the crews holding those secrets outlive the stack."""
        import re

        block = re.search(r"^  LaneKey:\n(.*?)(?=^  \w|\Z)", self._template(), re.S | re.M)
        assert block, "LaneKey is not in the template"
        body = block.group(1)
        assert "DeletionPolicy: Retain" in body, "the lane key is not retained on delete"
        assert "UpdateReplacePolicy: Retain" in body, "the lane key is not retained on replace"

    def test_the_recipe_bucket_is_not_retained(self):
        """Deliberately the opposite: a recipe is named by the digest of its own
        content and is re-uploaded on the next cache miss, so retaining it keeps
        bytes nothing will read."""
        import re

        block = re.search(r"^  RecipeBucket:\n(.*?)(?=^  \w|\Z)", self._template(), re.S | re.M)
        assert block, "RecipeBucket is not in the template"
        assert "DeletionPolicy: Retain" not in block.group(1)

    @staticmethod
    def _resource(name: str) -> str:
        import re

        block = re.search(
            rf"^  {name}:\n(.*?)(?=^  \w|\Z)",
            TestTheBaseTemplate._template(),
            re.S | re.M,
        )
        assert block, f"{name} is not in the template"
        return block.group(1)

    def test_the_build_role_can_write_the_build_logs(self):
        """A build role that cannot write its log group does not build without
        logs; it does not build at all.

        The image version is accepted and then never progresses -- PENDING with
        no timeout and no error -- and because the version is the last one the
        image cannot be deleted either, so the image NAME, which is the recipe
        digest, is burned for that content. Measured live. CloudFormation Lint
        cannot judge this: the template is valid either way.
        """
        body = self._resource("ImageBuildRole")
        for action in ("logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"):
            assert action in body, f"the build role cannot {action}"

    def test_the_build_logs_grant_is_scoped_to_the_platforms_own_groups(self):
        """Not every log group in the account. The build writes under the
        platform's own prefix, so that is the whole of what it may write."""
        body = self._resource("ImageBuildRole")
        assert "log-group:/aws/lambda-microvms/*" in body
        assert 'Resource: "*"' not in body

    @pytest.mark.parametrize("role", ["HybridActivationRole", "ExecutionRole"])
    def test_both_guest_roles_may_decrypt_the_lane_key(self, role):
        """The guest reads its own two secrets, which are encrypted under this key.

        Both roles carry the grant because the effective identity the platform
        hands the guest has been observed as either one, and a grant on only one
        of them makes a secret readable or not depending on which arrives.
        """
        body = self._resource(role)
        assert "kms:Decrypt" in body, f"{role} cannot read its own secrets"

    @pytest.mark.parametrize("role", ["HybridActivationRole", "ExecutionRole"])
    def test_neither_guest_role_may_encrypt_under_the_lane_key(self, role):
        """The guest READS its secrets; the control plane writes them. A guest that
        could encrypt under this key could write a secret its owner never
        minted."""
        body = self._resource(role)
        assert "kms:Encrypt" not in body, role
        assert "kms:GenerateDataKey" not in body, role

    @pytest.mark.parametrize("role", ["HybridActivationRole", "ExecutionRole"])
    def test_neither_guest_role_reaches_s3(self, role):
        """Nothing on this lane writes the crew's home anywhere, so no guest needs
        a bucket. A grant with no caller is a grant nobody is auditing."""
        body = self._resource(role)
        assert "s3:" not in body, role

    def test_the_execution_role_is_published_for_the_config_block(self):
        """``run-microvm`` refuses ``--logging`` without it, and without per-VM
        logging a launch that fails before the guest registers leaves no trace
        anywhere: an activation with zero registrations and a VM in RUNNING."""
        text = self._template()
        assert "ExecutionRoleArn:" in text
        assert "microvm.execution_role_arn" in text


class TestTheConfirmationIsBoundToTheConfigTheSpecComesFrom:
    """S3's first half: the comparison happens where the spec is built.

    ``provisioners()`` publishes the recipient, the operator confirms that
    string, and ``engine_for`` then reads the file AGAIN to construct the engine.
    An edit between the two -- or one racing a launch whose store checks are still
    running -- means the owner approved one image and key and a different pair
    would run. The recipient names exactly those, so it is the thing to compare,
    and it has to be compared against the SAME config object the spec is built
    from rather than a later read of anything.
    """

    def _with_config(self, monkeypatch, config):
        from kiro_crew.platform import defaults

        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_microvm_config",
            staticmethod(lambda: config),
        )

    def test_the_confirmed_recipient_is_accepted(self, monkeypatch):
        """Non-vacuity: the launch the operator did confirm must go through."""
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        self._with_config(monkeypatch, config)

        engine = defaults.DefaultRemoteProvisionerProvider().engine_for(
            defaults.MICROVM_PROVISIONER_ID, confirmed_recipient=config.launch_recipient()
        )
        assert engine is not None

    def test_a_config_that_changed_after_the_confirmation_is_refused(self, monkeypatch):
        """The operator confirmed one image; the file now names another."""
        from kiro_crew.platform import defaults

        moved = MicroVmConfig.from_mapping(
            _block(base_image_arn="arn:aws:lambda:us-east-1:aws:microvm-image/other")
        )
        self._with_config(monkeypatch, moved)

        with pytest.raises(defaults.RecipientMoved):
            defaults.DefaultRemoteProvisionerProvider().engine_for(
                defaults.MICROVM_PROVISIONER_ID,
                confirmed_recipient="image=something-else key=whatever",
            )

    def test_a_launch_that_confirmed_nothing_is_not_refused(self, monkeypatch):
        """The confirmation is the caller's to supply. A path that sends none is
        not asking for this check and must not be stopped by it."""
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        self._with_config(monkeypatch, config)

        engine = defaults.DefaultRemoteProvisionerProvider().engine_for(
            defaults.MICROVM_PROVISIONER_ID
        )
        assert engine is not None

    def test_the_refusal_names_no_credential(self, monkeypatch):
        """The recipient is an image and a key id, and the message quotes neither
        back: a refusal is read by whoever is nearby, and the two strings it would
        otherwise carry are what the operator is being asked to go and compare."""
        from kiro_crew.platform import defaults

        config = MicroVmConfig.from_mapping(_block())
        self._with_config(monkeypatch, config)

        with pytest.raises(defaults.RecipientMoved) as refusal:
            defaults.DefaultRemoteProvisionerProvider().engine_for(
                defaults.MICROVM_PROVISIONER_ID, confirmed_recipient="stale"
            )
        assert _COMPLETE["kms_key_id"] not in str(refusal.value)
        assert _COMPLETE["base_image_arn"] not in str(refusal.value)

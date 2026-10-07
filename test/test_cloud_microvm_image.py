"""Image identity, the recipe cache, and the build wait.

A crew's image on this lane is BUILT from an AWS-managed base plus that crew's
own signed bundle, and a build is minutes of someone else's compute charged to the
owner's account. So two things have to be right, and this file pins both:

**Identity.** An image is identified by its INPUTS, not by a build number. A name
that merely labels a build says nothing about content, so a cache keyed on it can
serve the wrong image; a name that IS the digest of its inputs cannot.

**Usability.** An image existing and an image being launchable are different
facts. ``CreateMicrovmImage`` answers with an image in ``CREATING``, and
``RunMicrovm`` needs a VERSION whose status is ``ACTIVE`` -- so an image that
reached ``CREATED`` with no active version is a finished build that produced
nothing runnable, and reusing it would be a launch the service refuses.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud.microvm import api
from kiro_crew.cloud.microvm.image import (
    HOOK_ENABLED,
    ImageResolver,
    Recipe,
    ResolvedImage,
)

BASE = "arn:aws:lambda:us-east-1:123456789012:microvm-image/al2023-base"
ROLE = "arn:aws:iam::123456789012:role/kirocrew-microvm-build"


def _recipe(**overrides) -> Recipe:
    fields = {
        "base_image_arn": BASE,
        "base_digest": "sha256:" + "a" * 64,
        "bundle_digest": "sha256:" + "b" * 64,
    }
    fields.update(overrides)
    return Recipe(**fields)  # type: ignore[arg-type]


class TestRecipeIdentity:
    def test_the_same_inputs_give_the_same_digest(self):
        assert _recipe().digest() == _recipe().digest()

    def test_a_different_bundle_gives_a_different_digest(self):
        assert _recipe().digest() != _recipe(bundle_digest="sha256:" + "c" * 64).digest()

    def test_a_different_base_gives_a_different_digest(self):
        assert _recipe().digest() != _recipe(base_digest="sha256:" + "c" * 64).digest()

    def test_the_two_digests_cannot_run_together(self):
        """Joined with a separator neither can contain, so no pair of inputs can
        produce another pair's digest by concatenation."""
        a = _recipe(base_digest="xy", bundle_digest="z").digest()
        b = _recipe(base_digest="x", bundle_digest="yz").digest()
        assert a != b

    def test_the_name_is_the_digest(self):
        recipe = _recipe()
        assert recipe.digest().startswith(recipe.image_name().rsplit("-", 1)[-1])

    def test_the_name_is_one_the_service_accepts(self):
        import re

        assert re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", _recipe().image_name())

    def test_a_recipe_missing_any_input_is_refused(self):
        """An image identified by less than its whole input is one a cache can
        serve for the wrong content."""
        for field in ("base_image_arn", "base_digest", "bundle_digest"):
            with pytest.raises(ValueError, match="needs a"):
                _recipe(**{field: ""})


class TestResolvedImage:
    def test_both_coordinates_are_required(self):
        with pytest.raises(ValueError, match="needs both"):
            ResolvedImage(image_identifier="arn:x", image_version="", reused=True)
        with pytest.raises(ValueError, match="needs both"):
            ResolvedImage(image_identifier="", image_version="3", reused=True)


class _Fake:
    """A scripted image control plane, recording the calls it received."""

    def __init__(self):
        self.images: list[api.MicroVmImage] = []
        self.version_status: dict[tuple[str, str], str] = {}
        self.created: list[dict] = []
        self.uploads: list[Recipe] = []
        self.get_sequence: list[api.MicroVmImage] = []

    def upload(self, recipe: Recipe) -> str:
        self.uploads.append(recipe)
        return f"s3://recipes/{recipe.digest()}.zip"

    def install(self, monkeypatch):
        import kiro_crew.cloud.microvm.image as image_module

        monkeypatch.setattr(
            image_module.api,
            "list_microvm_images",
            lambda **kw: [
                i for i in self.images if not kw.get("name_filter") or kw["name_filter"] in i.name
            ],
        )
        monkeypatch.setattr(
            image_module.api,
            "image_version_status",
            lambda ident, ver, **kw: self.version_status.get((ident, ver)),
        )

        def create(**kwargs):
            self.created.append(kwargs)
            built = api.MicroVmImage(
                image_arn=f"arn:aws:lambda:us-east-1:1:microvm-image/{kwargs['name']}",
                name=kwargs["name"],
                state="CREATING",
            )
            self.images.append(built)
            return built

        monkeypatch.setattr(image_module.api, "create_microvm_image", create)
        monkeypatch.setattr(
            image_module.api,
            "get_microvm_image",
            lambda ident, **kw: self.get_sequence.pop(0) if self.get_sequence else None,
        )


def _resolver(fake: _Fake, **overrides) -> ImageResolver:
    fields = {
        "base_image_arn": BASE,
        "build_role_arn": ROLE,
        "upload_recipe": fake.upload,
        "region": "us-east-1",
        "sleep": lambda _s: None,
    }
    fields.update(overrides)
    return ImageResolver(**fields)  # type: ignore[arg-type]


class TestCacheLookup:
    def test_an_absent_image_is_a_miss(self, monkeypatch):
        fake = _Fake()
        fake.install(monkeypatch)
        assert _resolver(fake).lookup(_recipe()) is None

    def test_an_image_with_an_active_version_is_a_hit(self, monkeypatch):
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:aws:lambda:us-east-1:1:microvm-image/{name}"
        fake.images.append(
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="4")
        )
        fake.version_status[(arn, "4")] = "ACTIVE"
        fake.install(monkeypatch)
        found = _resolver(fake).lookup(recipe)
        assert found is not None
        assert found.image_version == "4" and found.reused is True

    def test_an_image_with_no_active_version_is_a_MISS(self, monkeypatch):
        """A finished build that produced nothing launchable is not reusable."""
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        fake.images.append(
            api.MicroVmImage(
                image_arn=f"arn:x/{name}", name=name, state="CREATED", latest_active_version=""
            )
        )
        fake.install(monkeypatch)
        assert _resolver(fake).lookup(recipe) is None

    def test_an_inactive_version_is_a_miss(self, monkeypatch):
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        fake.images.append(
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="4")
        )
        fake.version_status[(arn, "4")] = "INACTIVE"
        fake.install(monkeypatch)
        assert _resolver(fake).lookup(recipe) is None

    def test_a_prefix_sibling_is_not_a_hit(self, monkeypatch):
        """``nameFilter`` is a filter, not an equality check, and launching a
        sibling's image is launching unknown content."""
        fake = _Fake()
        recipe = _recipe()
        sibling = recipe.image_name() + "-other"
        arn = f"arn:x/{sibling}"
        fake.images.append(
            api.MicroVmImage(
                image_arn=arn, name=sibling, state="CREATED", latest_active_version="4"
            )
        )
        fake.version_status[(arn, "4")] = "ACTIVE"
        fake.install(monkeypatch)
        assert _resolver(fake).lookup(recipe) is None

    def test_a_different_bundle_does_not_hit_the_cache(self, monkeypatch):
        """The property the whole cache rests on."""
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        fake.images.append(
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="4")
        )
        fake.version_status[(arn, "4")] = "ACTIVE"
        fake.install(monkeypatch)
        resolver = _resolver(fake)
        assert resolver.lookup(recipe) is not None
        assert resolver.lookup(_recipe(bundle_digest="sha256:" + "c" * 64)) is None


class TestResolve:
    def test_a_cache_hit_uploads_nothing_and_builds_nothing(self, monkeypatch):
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        fake.images.append(
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="4")
        )
        fake.version_status[(arn, "4")] = "ACTIVE"
        fake.install(monkeypatch)
        resolved = _resolver(fake).resolve(recipe)
        assert resolved.reused is True
        assert fake.uploads == [] and fake.created == []

    def test_a_miss_uploads_then_builds_then_waits(self, monkeypatch):
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:aws:lambda:us-east-1:1:microvm-image/{name}"
        fake.get_sequence = [
            api.MicroVmImage(image_arn=arn, name=name, state="CREATING"),
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="1"),
        ]
        fake.version_status[(arn, "1")] = "ACTIVE"
        fake.install(monkeypatch)
        resolved = _resolver(fake).resolve(recipe)
        assert resolved.reused is False
        assert resolved.image_version == "1"
        assert [r.digest() for r in fake.uploads] == [recipe.digest()]
        assert len(fake.created) == 1

    def test_the_build_is_named_for_the_recipe_and_tagged_with_it(self, monkeypatch):
        """The truncated digest names it; the whole digest is on the tag, so the
        inputs are recoverable from the image itself."""
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:aws:lambda:us-east-1:1:microvm-image/{name}"
        fake.get_sequence = [
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="1")
        ]
        fake.version_status[(arn, "1")] = "ACTIVE"
        fake.install(monkeypatch)
        _resolver(fake).resolve(recipe)
        created = fake.created[0]
        assert created["name"] == name
        assert created["tags"]["kirocrew:recipe"] == recipe.digest()
        assert created["base_image_arn"] == BASE
        assert created["build_role_arn"] == ROLE
        assert created["code_artifact_uri"].startswith("s3://")

    def test_every_platform_hook_is_wired(self, monkeypatch):
        """The only channel into the guest; an unwired one is a VM that bills and
        answers nothing.

        Each hook member is a SWITCH rather than the path it carried in an
        earlier shape of this API -- the platform owns the paths and refuses a
        path here against an ``[DISABLED, ENABLED]`` enum. ``HOOK_PREFIX`` is
        still where the guest SERVES them, which the posture tests pin.
        """
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        fake.get_sequence = [
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="1")
        ]
        fake.version_status[(arn, "1")] = "ACTIVE"
        fake.install(monkeypatch)
        _resolver(fake).resolve(recipe)
        created = fake.created[0]
        for hook in ("run_hook", "ready_hook", "suspend_hook", "resume_hook", "terminate_hook"):
            assert created[hook] == HOOK_ENABLED, hook

    def test_a_failed_build_raises_rather_than_waiting_it_out(self, monkeypatch):
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        fake.get_sequence = [
            api.MicroVmImage(
                image_arn=arn, name=name, state="CREATE_FAILED", latest_failed_version="1"
            )
        ]
        fake.install(monkeypatch)
        with pytest.raises(RuntimeError, match="CREATE_FAILED"):
            _resolver(fake).resolve(recipe)

    def test_an_image_that_vanishes_mid_build_raises(self, monkeypatch):
        fake = _Fake()
        fake.get_sequence = []
        fake.install(monkeypatch)
        with pytest.raises(RuntimeError, match="vanished"):
            _resolver(fake).resolve(_recipe())

    def test_a_build_that_never_activates_times_out_with_a_recoverable_message(self, monkeypatch):
        """The build is not cancelled, so the next launch's lookup can still find it."""
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        clock = {"t": 0.0}

        def now() -> float:
            clock["t"] += 100.0
            return clock["t"]

        fake.get_sequence = [api.MicroVmImage(image_arn=arn, name=name, state="CREATING")] * 100
        fake.install(monkeypatch)
        with pytest.raises(TimeoutError, match="cache lookup finds it"):
            _resolver(fake, now=now).resolve(recipe)


class TestImageApiShapes:
    """What ``CreateMicrovmImage`` refuses, on the argv the lane builds."""

    @pytest.fixture()
    def captured(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(list(args))
            return {
                "imageArn": "arn:aws:lambda:us-east-1:1:microvm-image/x",
                "name": "x",
                "state": "CREATING",
            }

        monkeypatch.setattr(api, "checked_json", fake)
        return calls

    def _create(self, **overrides):
        fields = {
            "name": "kirocrew-crew-abc",
            "base_image_arn": BASE,
            "build_role_arn": ROLE,
            "code_artifact_uri": "s3://recipes/abc.zip",
            "cpu_architecture": "ARM_64",
            "minimum_memory_mib": 2048,
            "hook_port": 8080,
            "run_hook": HOOK_ENABLED,
            "ready_hook": HOOK_ENABLED,
            "suspend_hook": HOOK_ENABLED,
            "resume_hook": HOOK_ENABLED,
            "terminate_hook": HOOK_ENABLED,
            "region": "us-east-1",
        }
        fields.update(overrides)
        return api.create_microvm_image(**fields)

    def test_a_retry_of_the_same_recipe_gets_a_fresh_client_token(self, monkeypatch):
        """A purely content-derived idempotency key outlives the image it made.

        After a failed build is deleted and the SAME recipe resubmitted, that key
        makes the service swallow the retry: the call returns the old record's
        shape, no build is dispatched, and the version sits PENDING with
        ``updatedAt`` equal to ``createdAt`` -- indistinguishable from a build
        role that cannot write its logs. The IMAGE's identity stays purely
        content-derived (the name is the recipe digest, and the cache is matched
        on it); only the retry key moves, which is what a retry key is for.
        """
        tokens: list[str] = []
        clock = {"t": 1000.0}

        def run_once() -> None:
            fake = _Fake()
            recipe = _recipe()
            name = recipe.image_name()
            arn = f"arn:x/{name}"
            fake.get_sequence = [
                api.MicroVmImage(
                    image_arn=arn, name=name, state="CREATED", latest_active_version="1"
                )
            ]
            fake.version_status[(arn, "1")] = "ACTIVE"
            fake.install(monkeypatch)
            clock["t"] += 60.0
            _resolver(fake, now=lambda: clock["t"]).resolve(recipe)
            tokens.append(fake.created[0]["client_token"])
            # The NAME is the content and must not move between attempts.
            assert fake.created[0]["name"] == name

        run_once()
        run_once()
        assert tokens[0] != tokens[1], "a retry reuses the first attempt's idempotency key"
        assert all(len(t) <= 64 for t in tokens), "the service bounds the token at 64 chars"
        from kiro_crew.cloud.microvm.image import _DIGEST_IN_NAME

        digest_part = _recipe().digest()[:_DIGEST_IN_NAME]
        assert all(t.startswith(digest_part) for t in tokens), "the token lost its content root"

    def test_the_build_log_group_reaches_the_create_call(self, captured):
        """Without it a failed build says only "The container image build failed"
        and writes nothing anywhere, so a red build's cause is unreachable.

        The shape matters as much as the presence: the API takes a nested
        ``cloudWatch.logGroup`` structure rather than a flat string.
        """
        self._create(log_group="/aws/lambda-microvms/kirocrew")
        argv = captured[0]
        assert "--logging" in argv, "the build's logs have nowhere to go"
        assert json.loads(argv[argv.index("--logging") + 1]) == {
            "cloudWatch": {"logGroup": "/aws/lambda-microvms/kirocrew"}
        }

    def test_no_log_group_sends_no_logging_flag_at_all(self, captured):
        """An empty group is the operator choosing no build logs. Sending the flag
        with an empty value would be a refused call rather than that choice."""
        self._create(log_group="")
        assert "--logging" not in captured[0]

    def test_the_architecture_is_the_apis_enum_and_not_a_machine_string(self, captured):
        """``cpuConfigurations`` is validated against ``[ARM_64, X86_64]``, so the
        ``uname -m`` spelling the guest reports is refused here:

            Value 'arm64' at 'cpuConfigurations.1.member.architecture' failed to
            satisfy constraint: Member must satisfy enum value set

        Pinned on the constant the resolver actually sends, so a revert to the
        machine string fails here rather than at a launch.
        """
        from kiro_crew.cloud.microvm.image import DEFAULT_CPU_ARCHITECTURE

        assert DEFAULT_CPU_ARCHITECTURE == "ARM_64"
        self._create(cpu_architecture=DEFAULT_CPU_ARCHITECTURE)
        argv = captured[0]
        sent = json.loads(argv[argv.index("--cpu-configurations") + 1])
        assert sent == [{"architecture": "ARM_64"}]

    def test_the_code_artifact_is_a_json_structure_with_a_uri(self, captured):
        self._create()
        argv = captured[0]
        value = json.loads(argv[argv.index("--code-artifact") + 1])
        assert value == {"uri": "s3://recipes/abc.zip"}

    def test_a_recipe_that_is_not_an_s3_uri_is_refused_before_the_call(self, captured):
        with pytest.raises(ValueError, match="S3 URI"):
            self._create(code_artifact_uri="/local/path/recipe.zip")
        assert captured == []

    @pytest.mark.parametrize(
        "field", ["name", "base_image_arn", "build_role_arn", "code_artifact_uri"]
    )
    def test_every_required_input_is_required(self, captured, field):
        with pytest.raises(ValueError, match="needs a"):
            self._create(**{field: ""})
        assert captured == []

    def test_the_build_and_run_hooks_are_in_their_own_phases(self, captured):
        """A run hook in the build set is a build that passes and a VM that never
        starts, so the two sets are checked separately."""
        self._create()
        argv = captured[0]
        hooks = json.loads(argv[argv.index("--hooks") + 1])
        assert set(hooks["microvmImageHooks"]) >= {"ready", "readyTimeoutInSeconds"}
        assert set(hooks["microvmHooks"]) >= {"run", "suspend", "resume", "terminate"}
        assert "run" not in hooks["microvmImageHooks"]
        assert "ready" not in hooks["microvmHooks"]
        assert hooks["port"] == 8080

    def test_the_run_hook_budget_is_the_documented_maximum(self, captured):
        """60 s is why the guest must not wait for its SSM node inside the hook."""
        self._create()
        argv = captured[0]
        hooks = json.loads(argv[argv.index("--hooks") + 1])
        assert hooks["microvmHooks"]["runTimeoutInSeconds"] == 60

    def test_the_architecture_and_memory_are_sent_as_lists_of_structures(self, captured):
        self._create()
        argv = captured[0]
        assert json.loads(argv[argv.index("--cpu-configurations") + 1]) == [
            {"architecture": "ARM_64"}
        ]
        assert json.loads(argv[argv.index("--resources") + 1]) == [{"minimumMemoryInMiB": 2048}]

    def test_the_egress_connector_is_a_region_scoped_arn(self, captured):
        """Same refusal as on ``RunMicrovm``: a bare mode name is malformed."""
        self._create()
        argv = captured[0]
        value = argv[argv.index("--egress-network-connectors") + 1]
        assert value.startswith("arn:aws:lambda:us-east-1:aws:network-connector:")


class TestImageResponseReading:
    def test_a_response_missing_the_state_is_refused(self):
        from kiro_crew.cloud.aws import AWSError

        with pytest.raises(AWSError, match="missing"):
            api.MicroVmImage.from_response({"imageArn": "arn:x"})

    def test_the_launchable_version_comes_from_the_active_field_only(self):
        """The image's own state says whether the BUILD finished, not whether
        anything runnable came out of it."""
        image = api.MicroVmImage(
            image_arn="arn:x",
            name="x",
            state="CREATED",
            latest_active_version="",
            latest_failed_version="2",
        )
        assert image.launchable_version == ""

    def test_the_failed_states_are_ones_no_wait_leaves(self):
        assert "CREATE_FAILED" in api.FAILED_IMAGE_STATES
        assert "DELETED" in api.FAILED_IMAGE_STATES
        assert "CREATING" not in api.FAILED_IMAGE_STATES

    def test_active_is_the_only_launchable_status(self):
        assert api.IMAGE_VERSION_ACTIVE == "ACTIVE"
        assert set(api.IMAGE_VERSION_STATUSES) == {"ACTIVE", "INACTIVE"}


class TestImageContract:
    """The image operations pinned against botocore's installed model."""

    @pytest.fixture(scope="class")
    def model(self):
        pytest.importorskip("botocore")
        import botocore.session

        try:
            return botocore.session.get_session().get_service_model("lambda-microvms")
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"botocore is installed but does not know the service: {exc}")

    def test_create_requires_exactly_what_the_lane_sends(self, model):
        op = model.operation_model("CreateMicrovmImage")
        assert set(op.input_shape.required_members) == {
            "baseImageArn",
            "buildRoleArn",
            "codeArtifact",
            "name",
        }

    def test_the_code_artifact_carries_a_uri(self, model):
        shape = model.operation_model("CreateMicrovmImage").input_shape.members["codeArtifact"]
        assert "uri" in shape.members

    def test_the_hooks_have_two_phase_sets(self, model):
        shape = model.operation_model("CreateMicrovmImage").input_shape.members["hooks"]
        assert {"microvmHooks", "microvmImageHooks", "port"} == set(shape.members)

    def test_the_image_states_the_lane_knows_are_the_models(self, model):
        shape = model.operation_model("GetMicrovmImage").output_shape.members["state"]
        enum = tuple(shape.metadata.get("enum") or shape.enum)
        assert tuple(api.MICROVM_IMAGE_STATES) == enum
        assert api.FAILED_IMAGE_STATES <= set(enum)

    def test_the_version_statuses_the_lane_knows_are_the_models(self, model):
        shape = model.operation_model("GetMicrovmImageVersion").output_shape.members["status"]
        enum = tuple(shape.metadata.get("enum") or shape.enum)
        assert tuple(api.IMAGE_VERSION_STATUSES) == enum


class TestGuestEnvironment:
    """What the built image carries, and why the strict flag is set HERE.

    One image serves both lanes and they are bounded by different things, so the
    container's strictness is a property of the deployment rather than of the
    image's code. Setting it globally would impose this lane's answer on every
    deployment whose own placement never asked for it, the moment it was relaunched
    on this image; setting it on the images THIS lane builds reaches exactly the
    deployments with no network bound to lean on.
    """

    def test_the_lane_turns_the_strict_gate_on_for_its_own_images(self):
        from kiro_crew.cloud.microvm.image import GUEST_ENVIRONMENT

        assert GUEST_ENVIRONMENT["SMC_REQUIRE_AUTH_ALL_ROUTES"] == "1"

    # That the CONTAINER's own default is off is asserted in the container's own
    # suite, by ``container_tests/test_front_microvm_image_posture.py``, and not
    # here. Importing the guest package from a gateway test crosses the boundary
    # the container package exists to keep: ``container.common.__init__`` imports
    # both of its modules, so one import here puts two guest files into the
    # gateway suite's coverage report, measured by a suite that does not exercise
    # them. The guest's files answer to the guest's job.

    def test_the_guest_skips_the_embedding_model_and_the_sandbox(self):
        from kiro_crew.cloud.microvm.image import GUEST_ENVIRONMENT

        assert GUEST_ENVIRONMENT["KIROCREW_SKIP_MODEL_DOWNLOAD"] == "1"
        assert GUEST_ENVIRONMENT["KIROCREW_ALLOW_UNSANDBOXED"] == "1"

    def test_the_environment_reaches_the_build_call(self, monkeypatch):
        fake = _Fake()
        recipe = _recipe()
        name = recipe.image_name()
        arn = f"arn:x/{name}"
        fake.get_sequence = [
            api.MicroVmImage(image_arn=arn, name=name, state="CREATED", latest_active_version="1")
        ]
        fake.version_status[(arn, "1")] = "ACTIVE"
        fake.install(monkeypatch)
        _resolver(fake).resolve(recipe)
        assert fake.created[0]["environment_variables"]["SMC_REQUIRE_AUTH_ALL_ROUTES"] == "1"

    def test_the_environment_is_sent_as_a_json_map(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(list(args))
            return {"imageArn": "arn:x", "name": "x", "state": "CREATING"}

        monkeypatch.setattr(api, "checked_json", fake)
        api.create_microvm_image(
            name="kirocrew-crew-abc",
            base_image_arn=BASE,
            build_role_arn=ROLE,
            code_artifact_uri="s3://recipes/abc.zip",
            cpu_architecture="ARM_64",
            minimum_memory_mib=2048,
            hook_port=8080,
            run_hook=HOOK_ENABLED,
            ready_hook=HOOK_ENABLED,
            suspend_hook=HOOK_ENABLED,
            resume_hook=HOOK_ENABLED,
            terminate_hook=HOOK_ENABLED,
            environment_variables={"SMC_REQUIRE_AUTH_ALL_ROUTES": "1"},
            region="us-east-1",
        )
        argv = calls[0]
        assert json.loads(argv[argv.index("--environment-variables") + 1]) == {
            "SMC_REQUIRE_AUTH_ALL_ROUTES": "1"
        }


class TestTheDigestCoversTheBaseArn:
    """An image's name must change when any input to its build changes.

    ``base_digest`` covers the recipes and the wheel -- the content this lane
    builds -- and says nothing about which AWS-managed base the build runs ON. Left
    out of the digest, an operator who changes ``microvm.base_image_arn`` gets a
    cache HIT on the image built against the old base, and the reuse is silent:
    the name matches, so nothing compares further.
    """

    def test_a_changed_base_arn_changes_the_digest(self):
        from kiro_crew.cloud.microvm.image import Recipe

        common = {"base_digest": "sha256:aaa", "bundle_digest": "sha256:bbb"}
        first = Recipe(base_image_arn="arn:aws:lambda:us-east-1:1:microvm-image/a", **common)
        second = Recipe(base_image_arn="arn:aws:lambda:us-east-1:1:microvm-image/b", **common)
        assert first.digest() != second.digest()

    def test_a_changed_base_arn_changes_the_image_name(self):
        from kiro_crew.cloud.microvm.image import Recipe

        common = {"base_digest": "sha256:aaa", "bundle_digest": "sha256:bbb"}
        first = Recipe(base_image_arn="arn:aws:lambda:us-east-1:1:microvm-image/a", **common)
        second = Recipe(base_image_arn="arn:aws:lambda:us-east-1:1:microvm-image/b", **common)
        assert first.image_name() != second.image_name()

    def test_the_same_three_inputs_still_give_the_same_digest(self):
        """The cache has to hit when nothing changed, or every launch rebuilds."""
        from kiro_crew.cloud.microvm.image import Recipe

        fields = {
            "base_image_arn": "arn:aws:lambda:us-east-1:1:microvm-image/a",
            "base_digest": "sha256:aaa",
            "bundle_digest": "sha256:bbb",
        }
        assert Recipe(**fields).digest() == Recipe(**fields).digest()

    def test_no_pair_of_inputs_can_impersonate_another(self):
        """The fields are labelled and newline-separated, so content cannot run
        together across the boundary."""
        from kiro_crew.cloud.microvm.image import Recipe

        a = Recipe(base_image_arn="x", base_digest="y", bundle_digest="z")
        b = Recipe(base_image_arn="x\nbase=y", base_digest="z", bundle_digest="w")
        assert a.digest() != b.digest()

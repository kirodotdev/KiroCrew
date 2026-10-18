"""The image a launch runs: the prebuilt pin, or the image it builds from the crew bundle.

``ApiVmLauncher.resolve_image`` answers a prebuilt pin as it stands and otherwise builds
from the bundle and the staged wheel, uploading the recipe under its digest and returning
the image ``CreateMicrovmImage`` names; a lane with neither, a buildable lane with no
bundle and a launch with no staged wheel are refused, and a changed wheel gives the crew a
new image name. Preflight refuses a buildable lane whose bundle is absent or short a
member, and passes a complete bundle or a prebuilt pin. The recipe upload carries
S3-managed encryption rather than the lane's key. Before it builds the wheel, the shell
staging script exports the ``SOURCE_DATE_EPOCH`` constant the Python stager uses, and it
keeps an operator's exported value. ``spec_account_id`` reads the account the image pins
from the operator's own ARNs and refuses a spec that names two, and a launch bakes its own
account and region into the image. The engine's steps and their order are pinned in
``test_cloud_microvm_engine.py``, and what a launch and a teardown keep true around those
steps in ``test_cloud_microvm_engine_lifecycle.py``.
"""

from __future__ import annotations

import zipfile

import pytest
from cloud_microvm_engine_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SPEC,
    _no_real_secret_writes,
)

from kiro_crew.cloud.microvm import api
from kiro_crew.cloud.microvm.engine import MicroVmLaunchEngine, MicroVmLaunchSpec
from kiro_crew.cloud.microvm.record import CrewStore


def _stage_wheel(path):
    """A minimal but REAL wheel at ``path``.

    A wheel is a zip and the lane reads it as one: ``recipe.assemble`` asks which
    members it carries, so that it can refuse a wheel holding the dashboard assets
    this lane's crew never serves. A stand-in that is not a readable archive is
    refused for being unreadable, which is not the case any of these tests is about.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("kiro_crew/__init__.py", date_time=(2020, 1, 2, 3, 4, 6)),
            b"# serving code\n",
        )
    return path


class TestResolveImage:
    """The image a launch runs: the prebuilt pin, and the build it does otherwise.

    The build path is what makes the recipe code something a launch runs rather
    than something a test builds, so it is asserted end to end: the bundle's digest
    goes into the recipe, the recipe is uploaded under that digest, and the image
    that comes back is the one ``CreateMicrovmImage`` named.
    """

    BUILD_SPEC = MicroVmLaunchSpec(
        base_image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/al2023",
        build_role_arn="arn:aws:iam::123456789012:role/CrewImageBuild",
        recipe_bucket="kc-recipes",
        kms_key_id="arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555",
    )

    @pytest.fixture(autouse=True)
    def _staged_wheel(self, tmp_path, _floor_monkeypatch):
        """Pretend a wheel is staged beside the recipes.

        A launch READS the wheel rather than building one, so these tests supply
        one instead of running a build toolchain.
        """
        from kiro_crew.cloud.microvm import engine as engine_mod

        wheel = _stage_wheel(tmp_path / "kiro_crew-0.0.0-py3-none-any.whl")
        _floor_monkeypatch.setattr(engine_mod, "_staged_wheel", lambda: wheel)
        return wheel

    @pytest.fixture()
    def bundle(self, tmp_path):
        root = tmp_path / "bundle"
        (root / "skills").mkdir(parents=True)
        (root / "manifest.json").write_text('{"crew_name": "demo"}\n')
        (root / "agent.json").write_text('{"name": "demo"}\n')
        (root / "mcp.json").write_text("{}\n")
        return root

    def test_a_prebuilt_pin_short_circuits_the_build(self):
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        launcher = ApiVmLauncher(SPEC)
        assert launcher.resolve_image(profile="p", region="us-east-1") == (
            SPEC.image_identifier,
            SPEC.image_version,
        )

    def test_a_lane_with_neither_refuses_rather_than_guessing(self):
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        bare = MicroVmLaunchSpec(kms_key_id="k")
        with pytest.raises(ValueError, match="no image to launch"):
            ApiVmLauncher(bare).resolve_image(profile="p", region="us-east-1")

    def test_a_buildable_lane_with_no_bundle_refuses(self):
        """Building from a guessed bundle is the error this lane will not make."""
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        with pytest.raises(ValueError, match="no crew bundle to build it from"):
            ApiVmLauncher(self.BUILD_SPEC).resolve_image(profile="p", region="us-east-1")

    def test_the_build_path_uploads_the_recipe_and_returns_the_built_image(
        self, bundle, _staged_wheel, monkeypatch
    ):
        import dataclasses

        from kiro_crew.cloud.microvm import engine as engine_mod
        from kiro_crew.cloud.microvm import recipe as recipe_mod
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        spec = dataclasses.replace(self.BUILD_SPEC, bundle_dir=str(bundle))
        uploaded: list[tuple[str, str]] = []
        built: list[str] = []

        monkeypatch.setattr(
            engine_mod.api,
            "get_microvm_image",
            lambda ident, **kw: api.MicroVmImage(
                image_arn=ident,
                name="base" if "al2023" in ident else "crew",
                state="CREATED",
                latest_active_version="base-9" if "al2023" in ident else "3",
            ),
        )
        monkeypatch.setattr(engine_mod.api, "list_microvm_images", lambda **kw: [])
        monkeypatch.setattr(
            engine_mod.api,
            "put_recipe_object",
            lambda bucket, key, body, **kw: uploaded.append((bucket, key)),
        )

        def _create(**kwargs):
            built.append(kwargs["code_artifact_uri"])
            return api.MicroVmImage(
                image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/crew",
                name=kwargs["name"],
                state="CREATED",
                latest_active_version="3",
            )

        monkeypatch.setattr(engine_mod.api, "create_microvm_image", _create)
        monkeypatch.setattr(
            engine_mod.api, "image_version_status", lambda *a, **kw: api.IMAGE_VERSION_ACTIVE
        )

        identifier, version = ApiVmLauncher(spec).resolve_image(profile="p", region="us-east-1")

        assert identifier.endswith("microvm-image/crew")
        assert version == "3"
        from kiro_crew.cloud.microvm.engine import spec_account_id
        from kiro_crew.cloud.microvm.image import Recipe, default_build_digest

        digest = Recipe(
            base_image_arn=spec.base_image_arn,
            base_digest=recipe_mod.base_recipe_digest(_staged_wheel),
            bundle_digest=recipe_mod.bundle_digest_of(bundle),
            # The launch's own pins, which are part of the image's identity: the
            # guest refuses a payload for any other account or region.
            build_digest=default_build_digest(account_id=spec_account_id(spec), region="us-east-1"),
        ).digest()
        assert uploaded == [("kc-recipes", f"recipes/{digest}.zip")]
        assert built == [f"s3://kc-recipes/recipes/{digest}.zip"]

    def test_a_launch_with_no_staged_wheel_is_refused(self, bundle, monkeypatch):
        """A launch reads the wheel; it does not build one. Building inside a launch
        would let the launch decide which version of the product the crew runs, and
        would need a build toolchain on the owner's machine."""
        import dataclasses

        from kiro_crew.cloud.microvm import engine as engine_mod
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        spec = dataclasses.replace(self.BUILD_SPEC, bundle_dir=str(bundle))
        monkeypatch.setattr(
            engine_mod,
            "_staged_wheel",
            lambda: (_ for _ in ()).throw(ValueError("no Kiro Crew wheel is staged")),
        )
        with pytest.raises(ValueError, match="no Kiro Crew wheel is staged"):
            ApiVmLauncher(spec).resolve_image(profile="p", region="us-east-1")

    def test_a_rebuilt_base_gives_the_crew_a_different_image_name(self, bundle, _staged_wheel):
        """The base's identity is the digest of its inputs, because this lane has no
        separately built base image to take a digest of. So a changed wheel is a
        changed base, and the crew gets a new image rather than reusing one built on
        the old content."""
        from kiro_crew.cloud.microvm import recipe as recipe_mod
        from kiro_crew.cloud.microvm.image import Recipe, default_build_digest

        digest = recipe_mod.bundle_digest_of(bundle)
        first = Recipe(
            base_image_arn=self.BUILD_SPEC.base_image_arn,
            base_digest=recipe_mod.base_recipe_digest(_staged_wheel),
            bundle_digest=digest,
            build_digest=default_build_digest(),
        ).image_name()
        _staged_wheel.write_bytes(b"PK\x03\x04 a different wheel entirely")
        second = Recipe(
            base_image_arn=self.BUILD_SPEC.base_image_arn,
            base_digest=recipe_mod.base_recipe_digest(_staged_wheel),
            bundle_digest=digest,
            build_digest=default_build_digest(),
        ).image_name()
        assert first != second


class TestBothWheelStagingPathsDateTheWheelAlike:
    """``_staged_wheel`` reads whatever staged the wheel, and the lane hashes it.

    The wheel's bytes go into ``recipe.base_recipe_digest``, which is half this
    lane's image cache key, so a wheel dated from the clock makes every restage
    of unchanged code a cache miss and a full multi-minute image build. TWO
    scripts stage that same wheel into ``runtime/vendor``:
    ``scripts/build_microvm_image_zip.py``, whose build env is pinned and tested
    in ``test_cloud_microvm_recipe.py``, and ``scripts/build_crew_base_image.sh``,
    which was not -- so a wheel staged by the shell path missed the cache.

    Pinned by READING the shell script, the way
    ``test_crew_image_publish_contract.py`` pins the same file: running it needs
    docker and a full wheel build, so the contract a test can hold it to is its
    text.
    """

    @staticmethod
    def _shell_text() -> str:
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "build_crew_base_image.sh"
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _python_stager():
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "build_microvm_image_zip.py"
        spec = importlib.util.spec_from_file_location("_build_microvm_image_zip", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_shell_script_sets_the_epoch_before_it_builds_the_wheel(self):
        text = self._shell_text()
        assert "SOURCE_DATE_EPOCH" in text, "the shell path stages an unreproducible wheel"
        assert text.index("SOURCE_DATE_EPOCH") < text.index(
            "python3 -m build"
        ), "the epoch is exported after the wheel is already built"

    def test_it_is_the_constant_the_python_stager_uses(self):
        """One wheel, two producers: a second constant would mean two sets of
        bytes for the same source and a cache the two paths keep missing."""
        assert f"SOURCE_DATE_EPOCH:-{self._python_stager().SOURCE_DATE_EPOCH}}}" in (
            self._shell_text()
        )

    def test_an_operators_own_value_is_kept(self):
        """The ``:-`` form, so an exported value survives -- any constant makes
        the wheel reproducible, and theirs may be pinned to something they can
        explain. The same choice ``_build_env``'s ``setdefault`` makes."""
        text = self._shell_text()
        assert 'export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-' in text


class TestPreflightChecksTheBundle:
    """The layout check, moved to the earliest place it can run.

    The Fargate lane's ``docker build`` finds a bundle short a member in seconds,
    on the operator's own machine. On this lane the build runs on Lambda, so the
    same mistake would cost minutes and land in a log the operator does not hold.
    """

    BUILD_SPEC = TestResolveImage.BUILD_SPEC

    def test_a_buildable_lane_with_no_bundle_fails_preflight(self, tmp_path):
        built = MicroVmLaunchEngine(spec=self.BUILD_SPEC, store=CrewStore(tmp_path / "c.json"))
        with pytest.raises(ValueError, match="no crew bundle to build it from"):
            built.preflight("prof", "us-east-1")

    def test_an_empty_bundle_dir_is_not_read_as_the_working_directory(self, tmp_path):
        """``Path("")`` is the CWD, which is a real directory, so an unset bundle
        would otherwise be checked for a crew layout and reported as a bundle here
        that is missing everything."""
        built = MicroVmLaunchEngine(spec=self.BUILD_SPEC, store=CrewStore(tmp_path / "c.json"))
        with pytest.raises(ValueError) as caught:
            built.preflight("prof", "us-east-1")
        assert "manifest.json" not in str(caught.value)

    def test_a_bundle_short_a_member_fails_preflight(self, tmp_path):
        import dataclasses

        root = tmp_path / "bundle"
        (root / "skills").mkdir(parents=True)
        (root / "manifest.json").write_text("{}\n")
        (root / "agent.json").write_text("{}\n")
        built = MicroVmLaunchEngine(
            spec=dataclasses.replace(self.BUILD_SPEC, bundle_dir=str(root)),
            store=CrewStore(tmp_path / "c.json"),
        )
        with pytest.raises(Exception, match="missing"):
            built.preflight("prof", "us-east-1")

    def test_a_complete_bundle_passes_preflight(self, tmp_path):
        import dataclasses

        root = tmp_path / "bundle"
        (root / "skills").mkdir(parents=True)
        (root / "manifest.json").write_text("{}\n")
        (root / "agent.json").write_text("{}\n")
        (root / "mcp.json").write_text("{}\n")
        built = MicroVmLaunchEngine(
            spec=dataclasses.replace(self.BUILD_SPEC, bundle_dir=str(root)),
            store=CrewStore(tmp_path / "c.json"),
        )
        built.preflight("prof", "us-east-1")

    def test_a_prebuilt_pin_needs_no_bundle(self, tmp_path):
        built = MicroVmLaunchEngine(spec=SPEC, store=CrewStore(tmp_path / "c.json"))
        built.preflight("prof", "us-east-1")


class TestTheRecipeIsNotEncryptedWithTheLaneKey:
    """The recipe and the lane's CMK have different readers, so one cannot serve both.

    The recipe is read by the BUILD ROLE, whose template grants it ``s3:GetObject``
    on the recipe bucket and no ``kms:Decrypt`` on the lane's key. Encrypting the
    recipe under that key makes the upload succeed and every cache-miss build fail
    with AccessDenied -- minutes later, in a log the operator does not hold.
    """

    def test_the_upload_names_no_kms_key(self, tmp_path, monkeypatch):
        import dataclasses

        from kiro_crew.cloud.microvm import engine as engine_mod
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        root = tmp_path / "bundle"
        (root / "skills").mkdir(parents=True)
        for name in ("manifest.json", "agent.json", "mcp.json"):
            (root / name).write_text("{}\n")
        wheel = _stage_wheel(tmp_path / "kiro_crew-0.0.0-py3-none-any.whl")
        monkeypatch.setattr(engine_mod, "_staged_wheel", lambda: wheel)
        spec = dataclasses.replace(TestResolveImage.BUILD_SPEC, bundle_dir=str(root))
        seen: list[dict] = []

        monkeypatch.setattr(engine_mod.api, "list_microvm_images", lambda **kw: [])
        monkeypatch.setattr(
            engine_mod.api,
            "get_microvm_image",
            lambda ident, **kw: api.MicroVmImage(
                image_arn=ident, name="crew", state="CREATED", latest_active_version="1"
            ),
        )
        monkeypatch.setattr(
            engine_mod.api,
            "put_recipe_object",
            lambda bucket, key, body, **kw: seen.append(kw),
        )
        monkeypatch.setattr(
            engine_mod.api,
            "create_microvm_image",
            lambda **kw: api.MicroVmImage(
                image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/crew",
                name=kw["name"],
                state="CREATED",
                latest_active_version="1",
            ),
        )
        monkeypatch.setattr(
            engine_mod.api, "image_version_status", lambda *a, **kw: api.IMAGE_VERSION_ACTIVE
        )

        ApiVmLauncher(spec).resolve_image(profile="p", region="us-east-1")

        assert seen, "the recipe was never uploaded"
        assert not seen[0].get(
            "kms_key_id"
        ), "the recipe was encrypted with the lane CMK, which the build role cannot decrypt"

    def test_an_upload_with_no_key_still_sends_an_encryption_header(self, monkeypatch):
        """S3-managed, not absent. A bucket that denies unencrypted puts tests the
        request HEADER, and a bucket default does not set one."""
        from kiro_crew.cloud.microvm import api as api_mod

        argvs: list[list[str]] = []
        monkeypatch.setattr(
            api_mod, "checked_json", lambda args, *a, **kw: argvs.append(args) or {}
        )
        api_mod.put_recipe_object("kc-recipes", "recipes/x.zip", "/dev/null")
        assert "--server-side-encryption" in argvs[0]
        assert "AES256" in argvs[0]
        assert "--ssekms-key-id" not in argvs[0]


class TestTheAccountBakedIntoTheImageIsReadFromTheOperatorsOwnArns:
    """The guest's ``/run`` refuses a payload outside the account baked into its
    image, so the control plane has to know which account that is.

    Read from the ARNs the operator already wrote rather than from
    ``sts:GetCallerIdentity``, for the reason ``cloud/source.py`` gives about
    pinning a bucket owner: a transient STS failure answers ``""``, and ``""`` is
    the value the guest reads as "not pinned" -- so an outage would turn the check
    off rather than fail the launch.
    """

    @staticmethod
    def _spec(**overrides) -> MicroVmLaunchSpec:
        import dataclasses

        return dataclasses.replace(MicroVmLaunchSpec(kms_key_id=""), **overrides)

    def test_the_activation_role_names_the_account(self):
        from kiro_crew.cloud.microvm.engine import spec_account_id

        spec = self._spec(
            activation_role_arn="arn:aws:iam::123456789012:role/kirocrew-microvm-crew"
        )
        assert spec_account_id(spec) == "123456789012"

    def test_the_configured_spec_in_this_file_resolves_to_its_account(self):
        from kiro_crew.cloud.microvm.engine import spec_account_id

        assert spec_account_id(SPEC) == "123456789012"

    def test_the_aws_managed_base_image_is_not_read_for_an_account(self):
        """A managed base ARN carries no account at all
        (``arn:aws:lambda:::microvm-image/...``), so reading it would answer ``""``
        for the normal case and quietly leave the image unpinned."""
        from kiro_crew.cloud.microvm.engine import spec_account_id

        spec = self._spec(base_image_arn="arn:aws:lambda:::microvm-image/al2023")
        assert spec_account_id(spec) == ""

    @pytest.mark.parametrize(
        "value",
        [
            "kirocrew-microvm-crew",
            "11111111-2222-3333-4444-555555555555",
            "alias/kirocrew",
            "",
            "arn:aws:iam::not-an-account:role/Crew",
        ],
    )
    def test_anything_that_is_not_an_arn_account_answers_empty(self, value):
        """A bare role name, a KMS key id and an alias are all spellings an
        operator may have written, and a fragment of one baked as a pin is a guest
        that refuses every payload."""
        from kiro_crew.cloud.microvm.engine import spec_account_id

        assert spec_account_id(self._spec(kms_key_id=value)) == ""

    def test_two_accounts_in_one_spec_are_refused(self):
        """One account holds both secrets and registers the node, so there is no
        single value to bake -- and picking one would bake a pin that refuses every
        payload naming the other."""
        from kiro_crew.cloud.microvm.engine import spec_account_id

        spec = self._spec(
            activation_role_arn="arn:aws:iam::111111111111:role/Crew",
            build_role_arn="arn:aws:iam::222222222222:role/Build",
        )
        with pytest.raises(ValueError, match="more than one AWS account"):
            spec_account_id(spec)

    def test_the_same_account_in_several_fields_is_not_a_conflict(self):
        from kiro_crew.cloud.microvm.engine import spec_account_id

        spec = self._spec(
            activation_role_arn="arn:aws:iam::111111111111:role/Crew",
            build_role_arn="arn:aws:iam::111111111111:role/Build",
            execution_role_arn="arn:aws:iam::111111111111:role/Exec",
        )
        assert spec_account_id(spec) == "111111111111"

    def test_a_launch_bakes_its_own_account_and_region_into_the_image(self, tmp_path, monkeypatch):
        """End to end through the build path: what the resolver sends to
        ``CreateMicrovmImage`` is this launch's own pair."""
        import dataclasses

        from kiro_crew.cloud.microvm import engine as engine_mod
        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        root = tmp_path / "bundle"
        (root / "skills").mkdir(parents=True)
        for name in ("manifest.json", "agent.json", "mcp.json"):
            (root / name).write_text("{}\n")
        wheel = _stage_wheel(tmp_path / "kiro_crew-0.0.0-py3-none-any.whl")
        monkeypatch.setattr(engine_mod, "_staged_wheel", lambda: wheel)
        spec = dataclasses.replace(
            TestResolveImage.BUILD_SPEC,
            bundle_dir=str(root),
            activation_role_arn="arn:aws:iam::123456789012:role/kirocrew-microvm-crew",
        )
        created: list[dict] = []
        monkeypatch.setattr(engine_mod.api, "list_microvm_images", lambda **kw: [])
        monkeypatch.setattr(
            engine_mod.api, "put_recipe_object", lambda bucket, key, body, **kw: None
        )
        monkeypatch.setattr(
            engine_mod.api,
            "create_microvm_image",
            lambda **kw: created.append(kw)
            or api.MicroVmImage(
                image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/crew",
                name=kw["name"],
                state="CREATED",
                latest_active_version="1",
            ),
        )
        monkeypatch.setattr(
            engine_mod.api,
            "get_microvm_image",
            lambda ident, **kw: api.MicroVmImage(
                image_arn=ident, name="crew", state="CREATED", latest_active_version="1"
            ),
        )
        monkeypatch.setattr(
            engine_mod.api, "image_version_status", lambda *a, **kw: api.IMAGE_VERSION_ACTIVE
        )

        ApiVmLauncher(spec).resolve_image(profile="p", region="eu-west-1")

        sent = created[0]["environment_variables"]
        assert sent["SMC_MICROVM_ACCOUNT"] == "123456789012"
        assert sent["SMC_MICROVM_REGION"] == "eu-west-1"

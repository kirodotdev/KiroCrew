"""The launch engine: the record it writes first, and the cleanup order it owes."""

from __future__ import annotations

import threading
import zipfile

import pytest

from kiro_crew.cloud.microvm import api, states
from kiro_crew.cloud.microvm.engine import (
    Activation,
    MicroVmLaunchEngine,
    MicroVmLaunchSpec,
    MicroVmSigninHandle,
    VmLauncher,
)
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore


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


SPEC = MicroVmLaunchSpec(
    image_identifier="arn:aws:lambda:us-east-1:123456789012:microvm-image/kirocrew",
    image_version="7",
    kms_key_id="arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555",
    identity_secret_ref="kirocrew/identity/demo-crew",
    wall_seconds=900,
)


class FakeLauncher(VmLauncher):
    """A scripted platform that records the ORDER it was asked to do things."""

    def __init__(self, *, fail_at: str = ""):
        self.calls: list[str] = []
        self.fail_at = fail_at
        self.payloads: list[str] = []
        self.client_tokens: list[str] = []

    def _maybe_fail(self, step: str) -> None:
        if self.fail_at == step:
            raise RuntimeError(f"scripted failure at {step}")

    def create_activation(self, *, tag, profile, region):
        self.calls.append("create_activation")
        self._maybe_fail("create_activation")
        return Activation(activation_id="act-1", activation_code="code-1")

    def run(self, *, payload, profile, region, client_token):
        self.calls.append("run")
        self.payloads.append(payload)
        self.client_tokens.append(client_token)
        self._maybe_fail("run")
        return api.MicroVm(
            microvm_id="mvm-1",
            state="PENDING",
            endpoint="https://mvm-1.example/",
            started_at="2026-10-06T21:00:00Z",
        )

    def wait_online(self, *, activation_id, profile, region):
        self.calls.append("wait_online")
        self._maybe_fail("wait_online")
        return "mi-0123456789abcdef0"

    def terminate(self, microvm_id, *, profile, region):
        self.calls.append("terminate")

    def wait_terminated(self, microvm_id, *, profile, region):
        self.calls.append("wait_terminated")


@pytest.fixture(autouse=True)
def _no_real_secret_writes(_floor_monkeypatch):
    """The launch mints the crew's control secret, which is a real AWS write.

    Stubbed for every test in this file: what these tests are about is the ORDER
    and the records, and a test that reached Secrets Manager would be a test that
    needs an account.
    """
    from kiro_crew.cloud.microvm import engine as engine_mod

    _floor_monkeypatch.setattr(
        engine_mod.api, "put_secret", lambda name, value, **kw: f"arn:fake:{name}"
    )
    _floor_monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    store = CrewStore(tmp_path / "crews.json")
    launcher = FakeLauncher()
    monkeypatch.setattr(
        "kiro_crew.cloud.microvm.engine._delete_activation",
        lambda activation_id, *, profile, region: launcher.calls.append("delete_activation"),
    )
    built = MicroVmLaunchEngine(spec=SPEC, store=store, launcher=launcher)
    return built, store, launcher


class TestPreflight:
    def test_an_engine_with_no_spec_names_what_it_lacks(self, tmp_path):
        engine = MicroVmLaunchEngine(store=CrewStore(tmp_path / "c.json"))
        with pytest.raises(ValueError, match="image version"):
            engine.preflight("prof", "us-east-1")

    def test_preflight_makes_no_platform_call(self, engine):
        built, _store, launcher = engine
        built.preflight("prof", "us-east-1")
        assert launcher.calls == []

    def test_a_bad_region_is_refused(self, engine):
        built, _store, _launcher = engine
        with pytest.raises(Exception):
            built.preflight("prof", "not a region")

    def test_a_lifetime_over_the_platform_bound_is_refused(self, tmp_path):
        import dataclasses

        engine = MicroVmLaunchEngine(
            spec=dataclasses.replace(SPEC, wall_seconds=api.MAX_LIFETIME_SECONDS + 1),
            store=CrewStore(tmp_path / "c.json"),
        )
        with pytest.raises(ValueError, match="not adjustable"):
            engine.preflight("prof", "us-east-1")


class TestProvision:
    def test_the_record_exists_before_the_vm_is_created(self, engine):
        """Teardown is handed only the tag, so a VM with no record is unreachable
        by the rollback that has to delete it."""
        built, store, launcher = engine
        seen: list[bool] = []
        original_run = launcher.run

        def run(**kwargs):
            seen.append(store.get("kc-a") is not None)
            return original_run(**kwargs)

        launcher.run = run  # type: ignore[method-assign]
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert seen == [True]

    def test_it_returns_the_managed_node_id_and_not_the_microvm_id(self, engine):
        """That id is what ``register`` puts in the instances registry."""
        built, _store, _launcher = engine
        assert (
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
            == "mi-0123456789abcdef0"
        )

    def test_the_happy_path_order(self, engine):
        built, _store, launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == ["create_activation", "run", "wait_online"]

    def test_the_record_ends_running_with_both_coordinates(self, engine):
        built, store, _launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        record = store.get("kc-a")
        assert record.state == states.RUNNING
        assert record.microvm_id == "mvm-1"
        assert record.mi_id == "mi-0123456789abcdef0"
        assert record.endpoint == "https://mvm-1.example/"

    def test_the_generation_increments_across_relaunches(self, engine):
        """A readiness answer from the previous VM must not satisfy this one."""
        built, store, _launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-a").generation == 1
        store.put(store.get("kc-a").evolve(state=states.RUNNING))
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-a").generation == 2

    def test_a_relaunch_of_a_terminated_crew_starts_a_fresh_generation(self, engine):
        """The record survives a terminate so the sweeper can tell a leak from a
        live crew; a relaunch under the same tag is a new VM and says so."""
        built, store, _launcher = engine
        store.put(CrewRecord(tag="kc-a", state=states.TERMINATED, generation=4))
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-a").generation == 5

    def test_each_launch_gets_its_own_client_token(self, engine):
        """Without one a CLI retry creates a second billing VM."""
        built, store, launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        store.put(store.get("kc-a").evolve(state=states.RUNNING))
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert len(set(launcher.client_tokens)) == 2

    def test_the_size_key_is_accepted_and_ignored(self, engine):
        """A MicroVM has no instance type, and the shared route passes one anyway."""
        built, _store, _launcher = engine
        built.provision(tag="kc-a", size_key="t3.large", profile="p", region="us-east-1")


class TestTheLaunchRecordsTheCrewNameTheGuestWillServe:
    """The tag is this launch's id; the crew's NAME comes from the bundle.

    The guest sets ``SMC_CREW_NAME`` from the bundle manifest baked into its
    image, and its front answers a turn addressed to anything else with a 404.
    The turn path holds an instance row and a tag, so unless the launch records
    the name nothing on this side can address the crew.
    """

    @staticmethod
    def _bundle(tmp_path, crew_name="l2crew"):
        import json as _json

        root = tmp_path / "bundle"
        root.mkdir()
        (root / "manifest.json").write_text(_json.dumps({"crew_name": crew_name}))
        return root

    def _engine(self, tmp_path, monkeypatch, bundle_dir):
        import dataclasses

        store = CrewStore(tmp_path / "crews.json")
        launcher = FakeLauncher()
        monkeypatch.setattr(
            "kiro_crew.cloud.microvm.engine._delete_activation",
            lambda activation_id, *, profile, region: None,
        )
        spec = dataclasses.replace(SPEC, bundle_dir=str(bundle_dir))
        return MicroVmLaunchEngine(spec=spec, store=store, launcher=launcher), store

    def test_the_bundles_name_lands_in_the_record(self, tmp_path, monkeypatch):
        built, store = self._engine(tmp_path, monkeypatch, self._bundle(tmp_path))
        built.provision(tag="kc-22d27f", size_key="", profile="p", region="us-east-1")
        record = store.get("kc-22d27f")
        assert record.crew_name == "l2crew"
        assert record.tag == "kc-22d27f"

    def test_an_unreadable_manifest_leaves_the_name_empty_rather_than_failing(
        self, tmp_path, monkeypatch
    ):
        """The launch works without the name -- the guest reads its own manifest --
        so failing a provision over this would cost the owner a crew to protect a
        field only the turn path reads. The turn path says so instead."""
        root = tmp_path / "bundle"
        root.mkdir()
        (root / "manifest.json").write_text("{not json")
        built, store = self._engine(tmp_path, monkeypatch, root)
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-a").crew_name == ""

    def test_a_lane_with_no_bundle_dir_records_no_name(self, tmp_path, monkeypatch):
        """``Path("")`` is the working directory, so an empty bundle_dir must not
        be read as a bundle that happens to be here."""
        built, store = self._engine(tmp_path, monkeypatch, "")
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-a").crew_name == ""


class TestProvisionCleanup:
    def test_a_failed_run_terminates_nothing_and_deletes_the_activation(self, engine):
        """The measured leak: an activation that enrolled nothing."""
        built, store, launcher = engine
        launcher.fail_at = "run"
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == ["create_activation", "run", "delete_activation"]
        assert store.get("kc-a").state == states.LAUNCH_FAILED

    def test_a_failed_online_wait_terminates_the_vm_before_the_activation(self, engine):
        """Deleting the activation first removes the only id for the node being orphaned."""
        built, _store, launcher = engine
        launcher.fail_at = "wait_online"
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == [
            "create_activation",
            "run",
            "wait_online",
            "terminate",
            "wait_terminated",
            "delete_activation",
        ]

    def test_the_activation_id_is_recorded_before_the_vm_runs(self, engine):
        """So a crash between the two leaves something the sweeper can find."""
        built, store, launcher = engine
        seen: list[str] = []
        original_run = launcher.run

        def run(**kwargs):
            seen.append(store.get("kc-a").activation_id)
            return original_run(**kwargs)

        launcher.run = run  # type: ignore[method-assign]
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert seen == ["act-1"]

    def test_a_cleanup_failure_does_not_mask_the_launch_error(self, engine):
        built, _store, launcher = engine
        launcher.fail_at = "wait_online"

        def boom(*a, **k):
            raise RuntimeError("cleanup also failed")

        launcher.terminate = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="scripted failure at wait_online"):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")


class TestSignin:
    def test_the_handle_is_already_done(self):
        handle = MicroVmSigninHandle("mi-1")
        assert handle.already_logged_in is True
        assert handle.error == ""
        assert handle.url == "" and handle.code == ""

    def test_it_cannot_raise_because_a_raise_here_is_not_rolled_back(self, engine):
        built, _store, _launcher = engine
        handle = built.begin_signin(instance_id="mi-1", profile="p", region="us-east-1")
        assert handle.wait(threading.Event()) is True
        handle.close()

    def test_a_cancelled_launch_does_not_report_a_done_step(self, engine):
        built, _store, _launcher = engine
        handle = built.begin_signin(instance_id="mi-1", profile="p", region="us-east-1")
        cancel = threading.Event()
        cancel.set()
        assert handle.wait(cancel) is False

    def test_abort_is_explicit_and_confirms_there_is_nothing_to_stop(self):
        """A missing method records "not confirmed stopped", which is wrong here."""
        assert MicroVmSigninHandle("mi-1").abort() is True

    def test_a_non_default_identity_is_refused_at_preflight(self, engine):
        built, _store, _launcher = engine

        class Target:
            is_default = False

            def describe(self):
                return "Identity Center (acme)"

        refusal = built.login_target_refusal(Target())
        assert "Identity Center (acme)" in refusal
        assert "cannot sign in" in refusal

    def test_the_default_identity_is_not_refused(self, engine):
        built, _store, _launcher = engine

        class Target:
            is_default = True

        assert built.login_target_refusal(Target()) == ""


class TestRegister:
    def test_a_failed_registration_raises(self, engine, monkeypatch):
        """Swallowing it marks the launch done for a crew the owner cannot see."""
        built, _store, _launcher = engine
        monkeypatch.setattr("kiro_crew.cloud.connect.register_instance", lambda *a, **k: None)
        with pytest.raises(RuntimeError, match="billing"):
            built.register(instance_id="mi-1", tag="kc-a", profile="p", region="us-east-1")

    def test_the_crew_is_registered_as_an_ssm_peer(self, engine, monkeypatch):
        """``mi-`` already validates, so no registry or argv change is needed."""
        built, _store, _launcher = engine
        captured: dict = {}

        def fake(instance_id, **kwargs):
            captured["instance_id"] = instance_id
            captured.update(kwargs)
            return "row"

        monkeypatch.setattr("kiro_crew.cloud.connect.register_instance", fake)
        built.register(instance_id="mi-1", tag="kc-a", profile="p", region="us-east-1")
        assert captured["instance_id"] == "mi-1"
        assert captured["connection_method"] == "ssm"
        assert captured["provisioner_id"] == "microvm"


class TestTeardown:
    def test_an_unknown_tag_reports_nothing_to_do(self, engine):
        built, _store, _launcher = engine
        assert built.teardown(tag="kc-nope", profile="p", region="us-east-1") is False

    def test_it_terminates_the_vm_and_deletes_the_activation(self, engine):
        built, store, launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        launcher.calls.clear()
        assert built.teardown(tag="kc-a", profile="p", region="us-east-1") is True
        assert launcher.calls == ["terminate", "wait_terminated", "delete_activation"]

    def test_a_torn_down_crew_ends_terminated(self, engine):
        """The VM is gone, and the home that was on its disk went with it."""
        built, store, _launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        built.teardown(tag="kc-a", profile="p", region="us-east-1")
        assert store.get("kc-a").state == states.TERMINATED


class TestActivationArguments:
    """What ``ssm:CreateActivation`` refuses, pinned on the argv the lane builds.

    A loopback fake accepts any of these, and two of the three refusals come from
    the CLI's own parser and never reach a service at all -- so the local harness
    was green on all of them while the live call failed.
    """

    @pytest.fixture()
    def captured(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(list(args))
            return {"ActivationId": "act-1", "ActivationCode": "code-1"}

        monkeypatch.setattr("kiro_crew.cloud.microvm.engine.checked_json", fake)
        return calls

    def _launcher(self, **overrides):
        import dataclasses

        from kiro_crew.cloud.microvm.engine import ApiVmLauncher

        fields = {
            "activation_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-crew",
            **overrides,
        }
        return ApiVmLauncher(dataclasses.replace(SPEC, **fields), sleep=lambda _s: None)

    def test_the_role_is_passed_at_all(self, captured):
        """``the following arguments are required: --iam-role`` -- the call needs it."""
        self._launcher().create_activation(tag="kc-a", profile="p", region="us-east-1")
        assert "--iam-role" in captured[0]

    def test_the_role_is_sent_as_a_NAME_and_not_an_ARN(self, captured):
        """``iamRole``'s own pattern has no colon in it, so an ARN is refused."""
        self._launcher().create_activation(tag="kc-a", profile="p", region="us-east-1")
        value = captured[0][captured[0].index("--iam-role") + 1]
        assert value == "kirocrew-microvm-crew"
        assert ":" not in value

    def test_a_role_name_is_accepted_unchanged(self, captured):
        """The operator may write either form; the base template outputs an ARN."""
        self._launcher(activation_role_arn="kirocrew-microvm-crew").create_activation(
            tag="kc-a", profile="p", region="us-east-1"
        )
        assert captured[0][captured[0].index("--iam-role") + 1] == "kirocrew-microvm-crew"

    def test_a_role_inside_a_path_is_named_by_its_last_segment(self, captured):
        self._launcher(
            activation_role_arn="arn:aws:iam::123456789012:role/some/path/Crew"
        ).create_activation(tag="kc-a", profile="p", region="us-east-1")
        assert captured[0][captured[0].index("--iam-role") + 1] == "Crew"

    def test_a_missing_role_is_refused_before_the_call(self, captured):
        """Named, with where to write it, rather than an opaque AWS refusal."""
        with pytest.raises(ValueError, match="activation_role_arn"):
            self._launcher(activation_role_arn="").create_activation(
                tag="kc-a", profile="p", region="us-east-1"
            )
        assert captured == []

    def test_each_tag_is_its_own_argv_element(self, captured):
        """One element holding two ``Key=`` pairs is refused:
        ``Second instance of key "Key" encountered``."""
        self._launcher().create_activation(tag="kc-a", profile="p", region="us-east-1")
        argv = captured[0]
        tags = []
        for item in argv[argv.index("--tags") + 1 :]:
            if item.startswith("--"):
                break
            tags.append(item)
        assert tags == ["Key=kirocrew:managed,Value=true", "Key=kirocrew:launch,Value=kc-a"]
        for tag in tags:
            assert tag.count("Key=") == 1, tag

    def test_the_activation_registers_exactly_one_node(self, captured):
        """A leaked code must not be able to enroll a second machine."""
        self._launcher().create_activation(tag="kc-a", profile="p", region="us-east-1")
        argv = captured[0]
        assert argv[argv.index("--registration-limit") + 1] == "1"


class TestRoleName:
    """The ARN-to-name reduction, on its own."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Crew", "Crew"),
            ("arn:aws:iam::123456789012:role/Crew", "Crew"),
            ("arn:aws:iam::123456789012:role/a/b/Crew", "Crew"),
            ("arn:aws-us-gov:iam::123456789012:role/Crew", "Crew"),
            ("  arn:aws:iam::123456789012:role/Crew  ", "Crew"),
            ("arn:aws:iam::123456789012:role/Crew/", "Crew"),
        ],
    )
    def test_it_reduces_every_form_to_the_name(self, value, expected):
        from kiro_crew.cloud.microvm.engine import _role_name

        assert _role_name(value) == expected


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
        from kiro_crew.cloud.microvm.image import Recipe

        digest = Recipe(
            base_image_arn=spec.base_image_arn,
            base_digest=recipe_mod.base_recipe_digest(_staged_wheel),
            bundle_digest=recipe_mod.bundle_digest_of(bundle),
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
        from kiro_crew.cloud.microvm.image import Recipe

        digest = recipe_mod.bundle_digest_of(bundle)
        first = Recipe(
            base_image_arn=self.BUILD_SPEC.base_image_arn,
            base_digest=recipe_mod.base_recipe_digest(_staged_wheel),
            bundle_digest=digest,
        ).image_name()
        _staged_wheel.write_bytes(b"PK\x03\x04 a different wheel entirely")
        second = Recipe(
            base_image_arn=self.BUILD_SPEC.base_image_arn,
            base_digest=recipe_mod.base_recipe_digest(_staged_wheel),
            bundle_digest=digest,
        ).image_name()
        assert first != second


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


class TestRegistrationNamesTheGuestsRunAsUser:
    """The registry's default is an EC2 user, and no MicroVM guest has it.

    Asserted against the call rather than the constant, because what breaks a
    live crew is the VALUE that reaches the registry: every in-guest command goes
    out as ``sudo -u <run_as> -i``, and the dashboard's token mint is the first
    one. A failed mint takes the tunnel with it, so the crew is online,
    registered and unreachable.
    """

    def test_the_registry_is_given_the_guests_user(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod
        from kiro_crew.cloud.microvm import recipe as recipe_mod
        from kiro_crew.cloud.microvm.engine import MicroVmLaunchEngine
        from kiro_crew.cloud.microvm.record import CrewStore

        seen: list[dict] = []
        monkeypatch.setattr(
            engine_mod.connect,
            "register_instance",
            lambda instance_id, **kw: seen.append(kw) or "inst-1",
        )
        built = MicroVmLaunchEngine(spec=SPEC, store=CrewStore(tmp_path / "c.json"))
        built.register(instance_id="mi-0123456789abcdef0", tag="c", profile="p", region="us-east-1")
        assert seen and seen[0]["ssm_run_as"] == recipe_mod.GUEST_RUN_AS

    def test_it_is_not_the_registrys_default(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod
        from kiro_crew.cloud.microvm.engine import MicroVmLaunchEngine
        from kiro_crew.cloud.microvm.record import CrewStore
        from kiro_crew.instances.registry import _DEFAULT_SSM_RUN_AS

        seen: list[dict] = []
        monkeypatch.setattr(
            engine_mod.connect,
            "register_instance",
            lambda instance_id, **kw: seen.append(kw) or "inst-1",
        )
        built = MicroVmLaunchEngine(spec=SPEC, store=CrewStore(tmp_path / "c.json"))
        built.register(instance_id="mi-0123456789abcdef0", tag="c", profile="p", region="us-east-1")
        assert seen[0]["ssm_run_as"] != _DEFAULT_SSM_RUN_AS

    def test_a_lane_that_names_no_user_keeps_the_registry_default(self):
        """EC2 and Fargate are unchanged: an omitted user means the registry's own
        default, not an empty one."""
        from kiro_crew.cloud.connect import _run_as_kwargs

        assert _run_as_kwargs("") == {}
        assert _run_as_kwargs("crew") == {"ssm_run_as": "crew"}


class TestTheControlSecretExistsBeforeTheVmReadsIt:
    """The payload carries a REFERENCE, so the launcher has to have written it.

    The tag is fresh per launch, so there is never an existing secret under it. A
    VM pointed at a name nobody created ends at its secrets stage and bills to its
    wall without serving anything.
    """

    def test_the_secret_is_written_before_the_vm_runs(self, engine, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, _store, launcher = engine
        order: list[str] = []
        monkeypatch.setattr(
            engine_mod.api,
            "put_secret",
            lambda name, value, **kw: order.append("secret") or f"arn:fake:{name}",
        )
        original_run = launcher.run

        def run(**kwargs):
            order.append("run")
            return original_run(**kwargs)

        launcher.run = run  # type: ignore[method-assign]
        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        assert order[: order.index("run") + 1][-2:] == ["secret", "run"]

    def test_the_value_is_never_an_argv_element(self):
        """A secret in argv is readable by anything that can list processes.

        Read from the FILE rather than through ``inspect``, because the autouse
        fixture above replaces the function with a stub and ``getsource`` would
        return the stub.
        """
        from pathlib import Path

        from kiro_crew.cloud.microvm import api as api_mod

        source = Path(api_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def put_secret(", 1)[1].split("\ndef ", 1)[0]
        assert 'f"file://{path}"' in body
        assert "0o600" in body, "the value file is readable by other users"
        assert "os.remove(path)" in body, "the value file outlives the call"

    def test_the_reference_is_recorded_so_a_reader_can_find_it(self, engine):
        built, store, _launcher = engine
        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        record = store.get("c")
        assert record is not None
        assert record.control_secret_ref.endswith("/c/CONTROL_SECRET")

    def test_the_identity_reference_is_carried_and_never_derived(self):
        """The launch TAG is not the operator's to choose.

        A dashboard launch builds its job with no tag and ``run_launch`` mints
        ``kc-<random hex>``, so a secret name built from the tag cannot exist
        before the launch that invents it. Deriving one sent every launch to its
        secrets stage while the VM billed to its wall. The operator creates the
        secret once, at a path they choose, and the spec carries that reference.
        """
        assert not hasattr(
            SPEC, "identity_secret_name"
        ), "a derived identity path is back; it names a secret nobody can create"
        assert hasattr(SPEC, "identity_secret_ref")

    def test_the_payload_carries_the_configured_reference(self, engine):
        built, store, launcher = engine
        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        import json

        body = json.loads(launcher.payloads[-1]) if launcher.payloads else {}
        assert body.get("identityRef") == SPEC.identity_secret_ref
        # And the two secrets stay distinct: one the lane owns, one it does not.
        assert body["identityRef"] != body["secretRef"]

    def test_teardown_deletes_the_secret_after_the_vm_is_gone(self, engine, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, _store, launcher = engine
        order: list[str] = []
        monkeypatch.setattr(
            engine_mod.api, "delete_secret", lambda name, **kw: order.append("secret")
        )
        monkeypatch.setattr(
            engine_mod, "_delete_activation", lambda *a, **kw: order.append("activation")
        )
        original = launcher.wait_terminated

        def wait_terminated(microvm_id, *, profile, region):
            order.append("terminated")
            return original(microvm_id, profile=profile, region=region)

        launcher.wait_terminated = wait_terminated  # type: ignore[method-assign]
        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        built.teardown(tag="c", profile="p", region="us-east-1")
        assert order.index("terminated") < order.index("secret")

    def test_teardown_deregisters_the_node_itself(self, engine, monkeypatch):
        """The host deregisters the managed node rather than trusting the guest to.

        The guest's own shutdown path does it, and on a live teardown it did not get
        there: the VM was cut first, and the node outlived it. An account then lists a
        managed instance with nothing behind it, and the sweeper reports it until
        someone removes it by hand.
        """
        import dataclasses

        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, _launcher = engine
        order: list[str] = []
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)
        monkeypatch.setattr(
            engine_mod, "_delete_activation", lambda *a, **kw: order.append("activation")
        )
        monkeypatch.setattr(
            engine_mod,
            "_deregister_node",
            lambda mi_id, **kw: order.append(f"deregister:{mi_id}"),
        )

        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        store.put(dataclasses.replace(store.get("c"), mi_id="mi-09f2beeee69ba"))
        assert built.teardown(tag="c", profile="p", region="us-east-1") is True

        assert "deregister:mi-09f2beeee69ba" in order, "the node outlived its VM"
        # After the activation, so a VM that outlived the terminate confirmation cannot
        # re-register under it in the gap between the two calls.
        assert order.index("activation") < order.index("deregister:mi-09f2beeee69ba")

    def test_teardown_asks_for_no_node_when_none_was_recorded(self, engine, monkeypatch):
        """Non-vacuity: a launch that never got a node must not send an empty id,
        which `deregister-managed-instance` would answer with a validation error that
        replaces the teardown's own outcome."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, _store, _launcher = engine
        asked: list[str] = []
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)
        monkeypatch.setattr(engine_mod, "_delete_activation", lambda *a, **kw: None)
        monkeypatch.setattr(engine_mod, "_deregister_node", lambda mi_id, **kw: asked.append(mi_id))

        import dataclasses

        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        store = built._require_store()
        store.put(dataclasses.replace(store.get("c"), mi_id=""))
        built.teardown(tag="c", profile="p", region="us-east-1")
        assert asked == []

    def test_the_deregister_names_the_action_it_needs(self):
        """The call is shelled out like the rest of this lane, and the IAM action it
        names is what an operator's policy has to allow -- so a typo here is a
        permission error at teardown and a node nobody can remove."""
        import inspect

        from kiro_crew.cloud.microvm import engine as engine_mod

        source = inspect.getsource(engine_mod._deregister_node)
        assert "ssm:DeregisterManagedInstance" in source
        assert "deregister-managed-instance" in source

    def test_the_delete_is_scheduled_not_forced(self):
        """The recovery window is the only thing that makes a mistaken teardown
        reversible."""
        import inspect

        from kiro_crew.cloud.microvm import api as api_mod

        assert "--force-delete-without-recovery" not in inspect.getsource(api_mod.delete_secret)


class TestALifecycleStepRechecksTheRecordAfterItWaits:
    """One invariant, two sites: a step reads a crew's state, WAITS, then acts.

    Both waits here run for minutes -- the online poll and the terminate poll --
    and the owner can tear a crew down or relaunch it inside either. A step that
    acts on the copy it read before the wait writes a state that has moved on, or
    releases a resource that now belongs to a different crew.
    """

    def test_a_teardown_during_the_online_wait_is_not_overwritten(self, engine, monkeypatch):
        """S1. The row says terminated and the VM is gone; the launch must not put
        its pre-wait copy back and leave a row claiming a running crew."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        deregistered: list[str] = []
        monkeypatch.setattr(
            engine_mod, "_deregister_node", lambda mi, **kw: deregistered.append(mi)
        )

        original = launcher.wait_online

        def wait_online(**kwargs):
            mi = original(**kwargs)
            # The owner's teardown finishes while this launch waits.
            store.apply_event("c", states.EVENT_TERMINATED)
            return mi

        launcher.wait_online = wait_online  # type: ignore[method-assign]

        with pytest.raises(engine_mod.LaunchSuperseded):
            built.provision(tag="c", size_key="", profile="p", region="us-east-1")

        assert store.get("c").state == states.TERMINATED, "the late write revived the row"
        assert store.get("c").mi_id == "", "the late node id was recorded anyway"
        # And the resources that late registration created are released rather
        # than left billing with nothing naming them.
        assert deregistered, "the node that registered during the wait was left behind"
        assert "terminate" in launcher.calls

    def test_a_relaunch_during_the_terminate_wait_keeps_its_own_resources(
        self, engine, monkeypatch
    ):
        """S2. A relaunch of the same tag is a DIFFERENT crew. The teardown must
        release the generation it was asked to end and leave the new one alone."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        deregistered: list[str] = []
        activations: list[str] = []
        monkeypatch.setattr(
            engine_mod, "_deregister_node", lambda mi, **kw: deregistered.append(mi)
        )
        monkeypatch.setattr(
            engine_mod, "_delete_activation", lambda act, **kw: activations.append(act)
        )
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)

        built.provision(tag="c", size_key="", profile="p", region="us-east-1")
        ours = store.get("c")

        original = launcher.wait_terminated

        def wait_terminated(microvm_id, **kwargs):
            original(microvm_id, **kwargs)
            # A new launch takes the tag: new generation, new activation, new node.
            store.put(
                ours.evolve(
                    generation=ours.generation + 1,
                    state=states.RUNNING,
                    activation_id="act-NEW",
                    mi_id="mi-NEW",
                )
            )

        launcher.wait_terminated = wait_terminated  # type: ignore[method-assign]

        assert built.teardown(tag="c", profile="p", region="us-east-1") is True

        assert "act-NEW" not in activations, "the new crew's activation was deleted"
        assert "mi-NEW" not in deregistered, "the new crew's node was deregistered"
        assert ours.activation_id in activations, "our own activation was left behind"
        # The new crew's row is not this teardown's to end.
        assert store.get("c").state == states.RUNNING
        assert store.get("c").generation == ours.generation + 1

    def test_a_teardown_during_the_vm_create_is_not_overwritten(self, engine):
        """S1b. ``launcher.run`` is a wait as well, and the write that records the
        VM it made is a WHOLE-record write of the copy read before it.

        The fence further down cannot catch this: by the time it looks, this write
        has already put ``pending`` back, so the row reads live and of this
        generation and the fence passes. The cancellation has to be seen here.
        """
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        original = launcher.run

        def run(**kwargs):
            vm = original(**kwargs)
            # The owner's teardown finishes while the VM is being created.
            store.apply_event("c", states.EVENT_TERMINATED)
            return vm

        launcher.run = run  # type: ignore[method-assign]

        with pytest.raises(engine_mod.LaunchSuperseded):
            built.provision(tag="c", size_key="", profile="p", region="us-east-1")

        assert store.get("c").state == states.TERMINATED, "the late write revived the row"
        assert store.get("c").microvm_id == "", "the superseded VM id was recorded anyway"
        # The VM and the activation this launch created are released rather than
        # left billing with a terminated row naming neither.
        assert "terminate" in launcher.calls, "the VM was left running"
        assert "delete_activation" in launcher.calls, "the activation was left behind"
        # And the minutes-long online poll is not entered for a crew already gone.
        assert "wait_online" not in launcher.calls

    def test_a_teardown_during_the_activation_create_is_not_overwritten(self, engine):
        """S1c. The same whole-record write, one step earlier: the activation id is
        stored with a ``put`` of the copy read before ``create_activation``."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        original = launcher.create_activation

        def create_activation(**kwargs):
            activation = original(**kwargs)
            store.apply_event("c", states.EVENT_TERMINATED)
            return activation

        launcher.create_activation = create_activation  # type: ignore[method-assign]

        with pytest.raises(engine_mod.LaunchSuperseded):
            built.provision(tag="c", size_key="", profile="p", region="us-east-1")

        assert store.get("c").state == states.TERMINATED, "the late write revived the row"
        assert store.get("c").activation_id == "", "the superseded activation was recorded"
        # The activation is the one resource that exists at this point, and an
        # activation with no registrations counts against the account's limits.
        assert "delete_activation" in launcher.calls, "the activation was left behind"
        # No VM was ever asked for.
        assert "run" not in launcher.calls

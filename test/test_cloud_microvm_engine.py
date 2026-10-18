"""The launch engine: the record it writes first, and the cleanup order it owes.

This module pins preflight, the provision order and the cleanup a failed provision owes,
the crew name a launch records, the replay armed around the create alone, sign-in,
registration as an SSM peer, teardown, a terminate that answers not found, and the
activation argv with its role-name reduction. The image a launch runs is pinned in
``test_cloud_microvm_engine_image.py``; the control secret, the client token's
derivation, the registration's run-as user and rollback, the recheck after each wait, the
coordinates a teardown uses and the wall sweep in
``test_cloud_microvm_engine_lifecycle.py``. The launch spec, the scripted launcher, the
secret stub and the ``engine`` fixture these files share live in
``cloud_microvm_engine_helpers.py``.
"""

from __future__ import annotations

import threading

import pytest
from cloud_microvm_engine_helpers import (  # noqa: F401 -- fixtures are requested by name
    SPEC,
    FakeLauncher,
    _no_real_secret_writes,
    engine_fixture,
)

from kiro_crew.cloud.microvm import api, states
from kiro_crew.cloud.microvm.engine import (
    Activation,
    MicroVmLaunchEngine,
    MicroVmSigninHandle,
    VmLauncher,
)
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore


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
    def test_a_failed_run_reconciles_the_token_then_releases_both(self, engine):
        """A ``run`` that fails is a failure of the ANSWER, not necessarily of the
        call: the service may be holding a VM this process never saw an id for.

        So the failure path replays the identical request with the identical
        idempotency token, and whatever that returns -- the VM the first call made,
        or a fresh one -- is terminated before the activation is deleted. The
        activation goes last for the reason it always does: it is the only id for
        the node a terminate is about to orphan.
        """
        built, store, launcher = engine
        launcher.fail_at = "run"
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == [
            "create_activation",
            "run",
            "reconcile",
            "terminate",
            "wait_terminated",
            "delete_activation",
        ]
        assert store.get("kc-a").state == states.LAUNCH_FAILED

    def test_a_run_that_never_reached_the_service_terminates_nothing(self, engine):
        """Non-vacuity for the test above: when the token created no VM, there is
        nothing to terminate and the activation is still released.

        The measured leak this path was written for: an activation that enrolled
        nothing bills nothing while counting against the account's limits, and is
        invisible until a sweep looks for it.
        """
        built, store, launcher = engine
        launcher.fail_at = "run"
        launcher.reconcile_answer = None
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == ["create_activation", "run", "reconcile", "delete_activation"]
        assert store.get("kc-a").state == states.LAUNCH_FAILED

    def test_the_reconciled_vm_id_is_recorded_before_it_is_terminated(self, engine):
        """A cleanup that also fails must leave the id somewhere a later teardown
        or sweep can read, or the VM bills to its wall with nothing naming it."""
        built, store, launcher = engine
        launcher.fail_at = "run"
        seen: list[str] = []

        def terminate(microvm_id, *, profile, region):
            seen.append(store.get("kc-a").microvm_id)

        launcher.terminate = terminate  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert seen == ["mvm-recovered"]

    def test_the_client_token_is_the_same_on_the_replay(self, engine):
        """The replay is only a reconciliation because the token is identical. A
        random suffix would make it a second request and a second billing VM."""
        built, _store, launcher = engine
        launcher.fail_at = "run"
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert len(launcher.client_tokens) == 2
        assert launcher.client_tokens[0] == launcher.client_tokens[1]

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


class SplitLauncher(VmLauncher):
    """A launcher that keeps :class:`VmLauncher`'s own ``run`` and ``reconcile``.

    ``FakeLauncher`` overrides both, which is what the ordering tests want and
    exactly what these must not have: the thing under test is the split between
    resolving an image and creating the VM, and that split lives in the base
    class. So this one scripts the two halves and inherits the wiring.
    """

    def __init__(self, *, resolutions, create_outcomes):
        #: One ``(identifier, version)`` per resolve, or an exception to raise.
        #: A resolve this list has no entry for is an ``IndexError``, which is
        #: how a second resolve nobody expected shows up as a failure.
        self.resolutions = list(resolutions)
        #: One per create: an exception to raise, or the id of the VM to answer.
        self.create_outcomes = list(create_outcomes)
        self.calls: list[str] = []
        #: ``(identifier, version, client_token)`` per create, so a test can
        #: assert the replay sent the same parameters as the first call.
        self.creates: list[tuple[str, str, str]] = []

    def create_activation(self, *, tag, profile, region):
        self.calls.append("create_activation")
        return Activation(activation_id="act-1", activation_code="code-1")

    def resolve_image(self, *, profile, region):
        self.calls.append("resolve_image")
        answer = self.resolutions.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def create_microvm(
        self, *, image_identifier, image_version, payload, profile, region, client_token
    ):
        self.calls.append("create_microvm")
        self.creates.append((image_identifier, image_version, client_token))
        answer = self.create_outcomes.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return api.MicroVm(
            microvm_id=answer, state="PENDING", endpoint=f"https://{answer}.example/"
        )

    def wait_online(self, *, activation_id, profile, region):
        self.calls.append("wait_online")
        return "mi-0123456789abcdef0"

    def terminate(self, microvm_id, *, profile, region):
        self.calls.append("terminate")

    def wait_terminated(self, microvm_id, *, profile, region):
        self.calls.append("wait_terminated")


class TestTheReplayIsArmedAroundTheCreateAlone:
    """``RunMicrovm`` is replayable under its token; resolving an image is not.

    Resolving can BUILD a multi-minute image, and the version it answers is the
    newest at the time it answers. So a replay that resolves again is a second
    build on the failure path, and if it answers a different version the same
    idempotency token goes out with different parameters -- while the VM the
    first attempt may have created is still unrecorded.
    """

    @pytest.fixture()
    def split(self, tmp_path, monkeypatch):
        def build(*, resolutions, create_outcomes):
            store = CrewStore(tmp_path / "crews.json")
            launcher = SplitLauncher(resolutions=resolutions, create_outcomes=create_outcomes)
            monkeypatch.setattr(
                "kiro_crew.cloud.microvm.engine._delete_activation",
                lambda activation_id, *, profile, region: launcher.calls.append(
                    "delete_activation"
                ),
            )
            built = MicroVmLaunchEngine(spec=SPEC, store=store, launcher=launcher)
            return built, store, launcher

        return build

    def test_a_create_that_failed_is_replayed_and_the_image_is_resolved_once(self, split):
        built, store, launcher = split(
            resolutions=[("img-a", "7")],
            create_outcomes=[RuntimeError("no answer from RunMicrovm"), "mvm-recovered"],
        )
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == [
            "create_activation",
            "resolve_image",
            "create_microvm",
            "create_microvm",
            "terminate",
            "wait_terminated",
            "delete_activation",
        ]
        assert launcher.calls.count("resolve_image") == 1
        assert store.get("kc-a").state == states.LAUNCH_FAILED

    def test_the_replay_sends_the_image_the_attempt_already_resolved(self, split):
        """Not whatever a fresh resolve would answer. The second entry here is
        the version a rebuild would have produced, and the replay must not take
        it: the token's two requests would then differ in their parameters."""
        built, _store, launcher = split(
            resolutions=[("img-a", "7"), ("img-a", "8")],
            create_outcomes=[RuntimeError("no answer from RunMicrovm"), "mvm-recovered"],
        )
        with pytest.raises(RuntimeError):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        identifiers = [(ident, version) for ident, version, _token in launcher.creates]
        assert identifiers == [("img-a", "7"), ("img-a", "7")]
        tokens = {token for _i, _v, token in launcher.creates}
        assert len(tokens) == 1, "the replay is only a reconciliation under one token"
        assert launcher.resolutions == [("img-a", "8")], "the replay resolved again"

    def test_an_image_that_could_not_be_resolved_replays_nothing(self, split):
        """No create was sent, so no VM can exist under this token and there is
        nothing to ask the service about. A replay here would resolve again --
        another build attempt -- and a build that succeeded the second time would
        create a VM only to terminate it."""
        built, store, launcher = split(
            resolutions=[RuntimeError("the image build failed")], create_outcomes=[]
        )
        with pytest.raises(RuntimeError, match="image build failed"):
            built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert launcher.calls == ["create_activation", "resolve_image", "delete_activation"]
        assert launcher.creates == []
        assert store.get("kc-a").state == states.LAUNCH_FAILED

    def test_a_later_attempts_resolve_failure_does_not_replay_the_earlier_image(self, split):
        """The engine holds one launcher for its whole life, so the pair the
        first attempt resolved is still on it when the second attempt starts."""
        built, _store, launcher = split(
            resolutions=[("img-a", "7"), RuntimeError("the image build failed")],
            create_outcomes=["mvm-1"],
        )
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        launcher.calls.clear()
        with pytest.raises(RuntimeError, match="image build failed"):
            built.provision(tag="kc-b", size_key="", profile="p", region="us-east-1")
        assert "create_microvm" not in launcher.calls
        assert len(launcher.creates) == 1

    def test_reconcile_on_a_launcher_that_never_resolved_asks_nothing(self):
        """The unit under the two tests above, with no engine around it."""
        launcher = SplitLauncher(resolutions=[], create_outcomes=[])
        assert (
            launcher.reconcile(payload="p", profile="p", region="us-east-1", client_token="t")
            is None
        )
        assert launcher.calls == []


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
        assert launcher.calls == [
            "terminate",
            "wait_terminated",
            "delete_activation",
            "deregister_node",
        ]

    def test_a_torn_down_crew_ends_terminated(self, engine):
        """The VM is gone, and the home that was on its disk went with it."""
        built, store, _launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        built.teardown(tag="kc-a", profile="p", region="us-east-1")
        assert store.get("kc-a").state == states.TERMINATED


class TestATerminateThatAnswersNotFoundIsACrewAlreadyGone:
    """The wall takes the VM and tells the control plane nothing.

    That is the case the wall sweep exists for, and a ``ResourceNotFoundException``
    from the terminate is what it meets. Read as a failed teardown it released
    nothing: the activation kept counting against the account's limits, the
    managed node stayed listed with nothing behind it, the control secret stayed
    readable, and the row stayed ``running`` -- so the next launch swept the same
    row and failed the same way.
    """

    @pytest.fixture()
    def walled(self, engine, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        released: list[tuple[str, str]] = []
        monkeypatch.setattr(
            engine_mod,
            "_delete_activation",
            lambda activation_id, *, profile, region: released.append(
                ("activation", activation_id)
            ),
        )
        monkeypatch.setattr(
            engine_mod,
            "_deregister_node",
            lambda mi_id, *, profile, region: released.append(("node", mi_id)),
        )
        monkeypatch.setattr(
            engine_mod.api, "delete_secret", lambda name, **kw: released.append(("secret", name))
        )
        store.put(
            CrewRecord(
                tag="kc-walled",
                state=states.RUNNING,
                microvm_id="mvm-walled",
                mi_id="mi-walled",
                activation_id="act-walled",
                control_secret_ref="kirocrew/crew/kc-walled/CONTROL_SECRET",
                profile="p",
                region="us-east-1",
                wall_at=100.0,
            )
        )
        return built, store, launcher, released

    @staticmethod
    def _answer(message):
        from kiro_crew.cloud.aws import AWSError

        def terminate(microvm_id, *, profile, region):
            raise AWSError(message, action="lambda:TerminateMicrovm")

        return terminate

    def test_the_release_is_finished_and_the_row_ends_terminated(self, walled):
        built, store, launcher, released = walled
        launcher.terminate = self._answer(  # type: ignore[method-assign]
            "ResourceNotFoundException: no microvm mvm-walled"
        )
        assert built.teardown(tag="kc-walled", profile="p", region="us-east-1") is True
        assert released == [
            ("activation", "act-walled"),
            ("node", "mi-walled"),
            ("secret", "kirocrew/crew/kc-walled/CONTROL_SECRET"),
        ]
        assert store.get("kc-walled").state == states.TERMINATED

    def test_the_confirmation_wait_is_skipped(self, walled):
        """It polls the same service that just said the VM is gone, and the poll
        runs for minutes before giving up."""
        built, _store, launcher, released = walled
        launcher.terminate = self._answer(  # type: ignore[method-assign]
            "ResourceNotFoundException: no microvm mvm-walled"
        )
        assert built.teardown(tag="kc-walled", profile="p", region="us-east-1") is True
        assert released, "the teardown stopped instead of reaching the release"
        assert "wait_terminated" not in launcher.calls

    def test_a_terminate_the_platform_refused_still_stops_the_teardown(self, walled):
        """Non-vacuity. Any other error means the VM may well be running, and
        releasing its activation, node and secret then would cut the owner off
        from a crew that is still billing."""
        built, store, launcher, released = walled
        launcher.terminate = self._answer("AccessDeniedException: not authorized")  # type: ignore[method-assign]
        assert built.teardown(tag="kc-walled", profile="p", region="us-east-1") is False
        assert released == []
        assert store.get("kc-walled").state == states.RUNNING

    def test_the_sweep_releases_such_a_crew_instead_of_retrying_it(self, walled):
        """The whole point of the wall sweep: this row is exactly what it looks
        for, and before this it was the one row it could never clear."""
        built, store, launcher, _released = walled
        launcher.terminate = self._answer(  # type: ignore[method-assign]
            "ResourceNotFoundException: no microvm mvm-walled"
        )
        assert built.reap(profile="p", region="us-east-1", now=1e9) == ["kc-walled"]
        assert store.get("kc-walled").state == states.TERMINATED


class TestActivationArguments:
    """What ``ssm:CreateActivation`` refuses, pinned on the argv the lane builds.

    A loopback fake accepts any of these, and two of the three refusals come from
    the CLI's own parser and never reach a service at all -- so the local harness
    was green on all of them while the live call failed.
    """

    @pytest.fixture()
    def captured(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0, visible_dirs=()):
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

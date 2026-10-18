"""What a launch and a teardown keep true beyond the order of their steps.

The control secret is written before the VM runs, its value never reaches argv, its
reference is recorded, and the payload carries it beside, and distinct from, the
operator's identity reference, which the spec carries rather than derives. A teardown
deletes the secret after the VM is gone, with its recovery window kept, and deregisters
the node itself after the activation. The client token is fixed per launch attempt and
differs per relaunch. Registration names the guest's run-as user, and a failed
registration terminates the VM and clears the record. A step that waits rereads the
record, so a launch does not overwrite a teardown that lands during the wait, and a
teardown leaves alone the resources of a relaunch that lands during its own. A teardown
sends every call to the profile and region the record names, falling back to the request
only for a row that recorded none. A launch records its wall when the VM exists and sweeps
before it creates anything: rows past their wall and grace are released, live, unwalled,
terminal and its own rows are left alone, and a release that is refused or raises keeps
its running row without failing the sweep. The sweep stops once its time budget is spent,
checking between rows, so a release already begun finishes. The engine's steps and their
order are pinned in ``test_cloud_microvm_engine.py``, and the image a launch runs in
``test_cloud_microvm_engine_image.py``.
"""

from __future__ import annotations

import pytest
from cloud_microvm_engine_helpers import (  # noqa: F401 -- fixtures are requested by name
    SPEC,
    _no_real_secret_writes,
    engine_fixture,
)

from kiro_crew.cloud.microvm import states
from kiro_crew.cloud.microvm.record import CrewRecord


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
        head = source.split("def put_secret(", 1)[1].split("\ndef ", 1)[0]
        assert "staging_dir(" in head, "the value is not staged under the masked root"
        body = source.split("def _put_secret_staged(", 1)[1].split("\ndef ", 1)[0]
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


class TestTheClientTokenIsDeterministic:
    """``RunMicrovm``'s token is both an idempotency key and a recovery handle.

    As an idempotency key it makes two deliveries of one request one VM. As a
    recovery handle it is the ONLY way back to a VM whose creating call never
    answered: the call carries no tags, the list has no attributable field, and
    the id is precisely what was lost. A random part defeats both at once, because
    the replay is then a second request and makes a second billing VM.
    """

    def test_the_same_launch_attempt_gets_the_same_token(self):
        from kiro_crew.cloud.microvm.engine import _client_token

        assert _client_token("kc-a", 3) == _client_token("kc-a", 3)

    def test_a_relaunch_gets_a_different_one(self):
        """A retry of one launch is one VM; a relaunch is a new one."""
        from kiro_crew.cloud.microvm.engine import _client_token

        assert _client_token("kc-a", 3) != _client_token("kc-a", 4)
        assert _client_token("kc-a", 3) != _client_token("kc-b", 3)

    def test_it_stays_inside_the_services_bound(self):
        from kiro_crew.cloud.microvm.engine import _client_token

        assert len(_client_token("kc-" + "f" * 200, 9)) <= 64

    def test_the_launch_sends_that_token_and_nothing_added_to_it(self, engine):
        from kiro_crew.cloud.microvm.engine import _client_token

        built, store, launcher = engine
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        generation = store.get("kc-a").generation
        assert launcher.client_tokens == [_client_token("kc-a", generation)]


class TestAFailedRegistrationIsRolledBack:
    """``register`` runs AFTER the step rollback covers, so it has to undo itself.

    The launch job's rollback is scoped to a failed ``provision``. A raise here
    with nothing else done leaves a VM running, billing to its wall, and absent
    from the one registry anything in the dashboard reads -- so neither the owner
    nor the orphan reaper can see it, and only the record names it.
    """

    @pytest.fixture()
    def launched(self, engine, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)
        monkeypatch.setattr(engine_mod, "_deregister_node", lambda mi_id, **kw: None)
        monkeypatch.setattr(engine_mod.connect, "register_instance", lambda *a, **k: None)
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        launcher.calls.clear()
        return built, store, launcher

    def test_the_vm_is_terminated(self, launched):
        built, _store, launcher = launched
        with pytest.raises(RuntimeError, match="terminated"):
            built.register(
                instance_id="mi-0123456789abcdef0", tag="kc-a", profile="p", region="us-east-1"
            )
        assert launcher.calls[:2] == ["terminate", "wait_terminated"]

    def test_the_record_is_cleared(self, launched):
        """A row left ``running`` for a VM that is gone is a crew the owner is
        offered and the sweeper keeps reporting."""
        built, store, _launcher = launched
        with pytest.raises(RuntimeError):
            built.register(
                instance_id="mi-0123456789abcdef0", tag="kc-a", profile="p", region="us-east-1"
            )
        assert store.get("kc-a").state == states.TERMINATED

    def test_a_rollback_that_fails_says_the_vm_is_still_billing(self, launched):
        """The owner's next action differs between the two outcomes, so the
        message cannot be the same one."""
        built, _store, launcher = launched

        def boom(*a, **k):
            raise RuntimeError("terminate refused")

        launcher.terminate = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="still running and billing"):
            built.register(
                instance_id="mi-0123456789abcdef0", tag="kc-a", profile="p", region="us-east-1"
            )

    def test_a_successful_registration_tears_nothing_down(self, engine, monkeypatch):
        """Non-vacuity: the rollback is on the failure path only."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        monkeypatch.setattr(engine_mod.connect, "register_instance", lambda *a, **k: "row")
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        launcher.calls.clear()
        built.register(
            instance_id="mi-0123456789abcdef0", tag="kc-a", profile="p", region="us-east-1"
        )
        assert launcher.calls == []
        assert store.get("kc-a").state == states.RUNNING


class TestTeardownUsesTheRecordsOwnCoordinates:
    """A crew lives where it was launched, and nothing moves it.

    ``cloud.json`` can be edited between a launch and a teardown, a dashboard
    passes whatever the operator is looking at now, and a sweep acts on rows it
    did not launch. Against the wrong region the terminate fails and every
    best-effort delete fails quietly -- and the row is marked ``terminated`` all
    the same, so the VM bills to its wall with a record saying it is gone.
    """

    @pytest.fixture()
    def launched(self, engine, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        seen: dict[str, tuple[str, str]] = {}
        monkeypatch.setattr(
            engine_mod,
            "_delete_activation",
            lambda act, *, profile, region: seen.__setitem__("activation", (profile, region)),
        )
        monkeypatch.setattr(
            engine_mod,
            "_deregister_node",
            lambda mi_id, *, profile, region: seen.__setitem__("node", (profile, region)),
        )
        monkeypatch.setattr(
            engine_mod.api,
            "delete_secret",
            lambda name, *, profile, region, **kw: seen.__setitem__("secret", (profile, region)),
        )
        built.provision(tag="kc-a", size_key="", profile="launch-prof", region="us-west-2")
        store.put(store.get("kc-a").evolve(mi_id="mi-0123456789abcdef0"))
        launcher.coords.clear()
        return built, store, launcher, seen

    def test_the_terminate_goes_to_the_launch_region(self, launched):
        built, _store, launcher, _seen = launched
        built.teardown(tag="kc-a", profile="other-prof", region="eu-west-1")
        assert launcher.coords == [
            ("terminate", "launch-prof", "us-west-2"),
            ("wait_terminated", "launch-prof", "us-west-2"),
        ]

    def test_every_best_effort_delete_goes_there_too(self, launched):
        """These three fail QUIETLY in the wrong region, which is what makes a
        wrong-region teardown read as a clean one."""
        built, _store, _launcher, seen = launched
        built.teardown(tag="kc-a", profile="other-prof", region="eu-west-1")
        assert seen["activation"] == ("launch-prof", "us-west-2")
        assert seen["node"] == ("launch-prof", "us-west-2")
        assert seen["secret"] == ("launch-prof", "us-west-2")

    def test_a_row_with_no_coordinates_falls_back_to_the_request(self, engine, monkeypatch):
        """Rows written before this lane recorded them still have to tear down."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        monkeypatch.setattr(engine_mod, "_delete_activation", lambda *a, **kw: None)
        monkeypatch.setattr(engine_mod, "_deregister_node", lambda *a, **kw: None)
        store.put(
            CrewRecord(
                tag="kc-old", state=states.RUNNING, microvm_id="mvm-old", profile="", region=""
            )
        )
        built.teardown(tag="kc-old", profile="req-prof", region="ap-south-1")
        assert ("terminate", "req-prof", "ap-south-1") in launcher.coords


class TestTheWallIsRecordedAndSwept:
    """The platform takes the VM at its wall whether or not anything noticed.

    Nothing else on this lane does: the row stays ``running``, the managed node
    keeps being listed with nothing behind it, the activation counts against the
    account's limits and the control secret stays readable. The owner is offered
    an openable crew, and the open fails at the tunnel.
    """

    @pytest.fixture()
    def swept(self, engine, monkeypatch):
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)
        monkeypatch.setattr(engine_mod, "_delete_activation", lambda *a, **kw: None)
        monkeypatch.setattr(engine_mod, "_deregister_node", lambda *a, **kw: None)
        return built, store, launcher

    def test_the_wall_is_written_when_the_vm_exists(self, engine):
        """A record with no ``wall_at`` is a deadline no sweep can act on and no
        owner can read."""
        built, store, _launcher = engine
        built.now = lambda: 1_000.0
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-a").wall_at == 1_000.0 + SPEC.wall_seconds

    def test_the_wall_is_the_lifetime_that_was_asked_for(self, engine):
        built, store, _launcher = engine
        built.now = lambda: 500.0
        built.provision(tag="kc-a", size_key="", profile="p", region="us-east-1")
        record = store.get("kc-a")
        assert record.wall_at - 500.0 == record.wall_seconds

    def test_an_expired_crew_is_released(self, swept):
        from kiro_crew.cloud.microvm.engine import WALL_GRACE_SECONDS

        built, store, launcher = swept
        store.put(
            CrewRecord(
                tag="kc-old",
                state=states.RUNNING,
                microvm_id="mvm-old",
                mi_id="mi-old",
                activation_id="act-old",
                profile="p",
                region="us-east-1",
                wall_at=100.0,
            )
        )
        released = built.reap(profile="p", region="us-east-1", now=100.0 + WALL_GRACE_SECONDS + 1)
        assert released == ["kc-old"]
        assert store.get("kc-old").state == states.TERMINATED
        assert "terminate" in launcher.calls

    def test_a_crew_still_inside_its_wall_is_left_alone(self, swept):
        built, store, launcher = swept
        store.put(
            CrewRecord(
                tag="kc-live",
                state=states.RUNNING,
                microvm_id="mvm-live",
                profile="p",
                region="us-east-1",
                wall_at=10_000.0,
            )
        )
        assert built.reap(profile="p", region="us-east-1", now=500.0) == []
        assert store.get("kc-live").state == states.RUNNING
        assert launcher.calls == []

    def test_a_row_with_no_wall_is_not_swept(self, swept):
        """Zero is "never recorded", not "expired at the epoch". Reading it as a
        deadline would terminate every crew launched before the field existed."""
        built, store, launcher = swept
        store.put(
            CrewRecord(tag="kc-nowall", state=states.RUNNING, microvm_id="mvm-x", wall_at=0.0)
        )
        assert built.reap(profile="p", region="us-east-1", now=1e9) == []
        assert launcher.calls == []

    def test_a_terminal_row_is_not_swept_again(self, swept):
        """The record outlives the VM so a leak can be told from a live crew, so a
        terminated row is the normal resting state and not work to redo."""
        built, store, launcher = swept
        store.put(
            CrewRecord(tag="kc-done", state=states.TERMINATED, microvm_id="mvm-done", wall_at=1.0)
        )
        assert built.reap(profile="p", region="us-east-1", now=1e9) == []
        assert launcher.calls == []

    def test_the_launch_that_is_asking_is_never_swept(self, swept):
        """Its own row is mid-flight: this launch wrote it seconds ago and the
        wall it carries is the previous generation's."""
        built, store, launcher = swept
        store.put(
            CrewRecord(
                tag="kc-a",
                state=states.RUNNING,
                microvm_id="mvm-prev",
                profile="p",
                region="us-east-1",
                wall_at=1.0,
            )
        )
        assert built.reap(profile="p", region="us-east-1", skip_tag="kc-a", now=1e9) == []
        assert launcher.calls == []

    def test_a_launch_sweeps_before_it_creates_anything(self, swept):
        """The sweep is driven from ``provision`` because that is the one moment
        this lane's code is certainly running; a scheduler hook is deferred."""
        built, store, launcher = swept
        store.put(
            CrewRecord(
                tag="kc-old",
                state=states.RUNNING,
                microvm_id="mvm-old",
                profile="p",
                region="us-east-1",
                wall_at=1.0,
            )
        )
        built.provision(tag="kc-new", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-old").state == states.TERMINATED
        assert launcher.calls.index("terminate") < launcher.calls.index("create_activation")

    def test_one_stuck_crew_does_not_fail_the_launch_that_noticed_it(self, swept, monkeypatch):
        """Stuck means the platform REFUSED the terminate, so the VM may still be
        running. Not a terminate that answered ``ResourceNotFoundException``:
        that one is a crew the wall already took, which is what this sweep is for
        and which ``TestATerminateThatAnswersNotFoundIsACrewAlreadyGone`` covers.
        """
        from kiro_crew.cloud.aws import AWSError

        built, store, launcher = swept

        def refused(microvm_id, *, profile, region):
            raise AWSError(
                "AccessDeniedException: not authorized", action="lambda:TerminateMicrovm"
            )

        launcher.terminate = refused  # type: ignore[method-assign]
        store.put(
            CrewRecord(
                tag="kc-stuck",
                state=states.RUNNING,
                microvm_id="mvm-stuck",
                profile="p",
                region="us-east-1",
                wall_at=1.0,
            )
        )
        assert built.reap(profile="p", region="us-east-1", now=1e9) == []
        # The row keeps naming what it left behind, rather than being marked gone.
        assert store.get("kc-stuck").state == states.RUNNING

    def test_a_release_that_raises_outright_does_not_fail_the_launch_either(self, swept):
        """The other half: ``teardown`` answers ``False`` for a refused terminate,
        and a release that raises instead must not reach the caller -- the caller
        is the owner's launch."""
        built, store, launcher = swept

        def boom(microvm_id, *, profile, region):
            raise RuntimeError("terminate blew up")

        launcher.terminate = boom  # type: ignore[method-assign]
        store.put(
            CrewRecord(
                tag="kc-stuck",
                state=states.RUNNING,
                microvm_id="mvm-stuck",
                profile="p",
                region="us-east-1",
                wall_at=1.0,
            )
        )
        assert built.reap(profile="p", region="us-east-1", now=1e9) == []
        assert store.get("kc-stuck").state == states.RUNNING


class TestASweepInsideALaunchIsTimeBounded:
    """The sweep runs from ``provision``, so its cost is the owner's wait.

    A teardown is not quick: the terminate confirmation polls for up to
    ``ONLINE_TIMEOUT_SECONDS``. Unbounded, a launch waited on every expired crew
    in turn -- one per expired row -- before it created anything of its own.
    """

    @pytest.fixture()
    def slow(self, engine, monkeypatch):
        """A sweep whose every teardown costs 100 seconds of the engine's clock."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, launcher = engine
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)
        monkeypatch.setattr(engine_mod, "_delete_activation", lambda *a, **kw: None)
        monkeypatch.setattr(engine_mod, "_deregister_node", lambda *a, **kw: None)
        clock = [1_000.0]
        built.now = lambda: clock[0]
        original = launcher.wait_terminated

        def wait_terminated(microvm_id, *, profile, region):
            clock[0] += 100.0
            original(microvm_id, profile=profile, region=region)

        launcher.wait_terminated = wait_terminated  # type: ignore[method-assign]
        for tag in ("kc-1", "kc-2", "kc-3"):
            store.put(
                CrewRecord(
                    tag=tag,
                    state=states.RUNNING,
                    microvm_id=f"mvm-{tag}",
                    mi_id=f"mi-{tag}",
                    activation_id=f"act-{tag}",
                    profile="p",
                    region="us-east-1",
                    wall_at=100.0,
                )
            )
        return built, store, launcher

    def test_the_sweep_stops_when_the_budget_is_spent(self, slow):
        built, _store, _launcher = slow
        assert built.reap(profile="p", region="us-east-1", now=1e9, budget_seconds=30.0) == ["kc-1"]

    def test_the_rows_it_left_still_name_what_they_left_behind(self, slow):
        """Left, not lost: the next launch or an explicit teardown releases them,
        and until then the row is what names the node and the activation."""
        built, store, _launcher = slow
        built.reap(profile="p", region="us-east-1", now=1e9, budget_seconds=30.0)
        for tag in ("kc-2", "kc-3"):
            row = store.get(tag)
            assert row.state == states.RUNNING
            assert (row.microvm_id, row.mi_id, row.activation_id) == (
                f"mvm-{tag}",
                f"mi-{tag}",
                f"act-{tag}",
            )
            assert row.wall_at == 100.0, "the deadline the next sweep reads"

    def test_a_release_already_begun_is_not_interrupted(self, slow):
        """Which is what bounds a launch at the budget PLUS one teardown, rather
        than at the budget: the check is between rows and nothing cancels a
        terminate that has been sent."""
        built, store, _launcher = slow
        built.reap(profile="p", region="us-east-1", now=1e9, budget_seconds=30.0)
        assert store.get("kc-1").state == states.TERMINATED

    def test_a_sweep_that_stays_inside_its_budget_releases_every_expired_row(
        self, engine, monkeypatch
    ):
        """Non-vacuity for the three above: the cap is reached by the clock the
        teardowns spend, not by the number of rows."""
        from kiro_crew.cloud.microvm import engine as engine_mod

        built, store, _launcher = engine
        monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)
        monkeypatch.setattr(engine_mod, "_delete_activation", lambda *a, **kw: None)
        monkeypatch.setattr(engine_mod, "_deregister_node", lambda *a, **kw: None)
        built.now = lambda: 1_000.0
        for tag in ("kc-1", "kc-2", "kc-3"):
            store.put(
                CrewRecord(
                    tag=tag,
                    state=states.RUNNING,
                    microvm_id=f"mvm-{tag}",
                    profile="p",
                    region="us-east-1",
                    wall_at=100.0,
                )
            )
        released = built.reap(profile="p", region="us-east-1", now=1e9, budget_seconds=30.0)
        assert released == ["kc-1", "kc-2", "kc-3"]

    def test_a_launch_uses_the_bounded_default(self, slow):
        """``provision`` passes no budget, so the module default applies -- and a
        launch behind three expired crews creates its own activation rather than
        waiting on all three."""
        built, store, launcher = slow
        built.provision(tag="kc-new", size_key="", profile="p", region="us-east-1")
        assert store.get("kc-1").state == states.TERMINATED
        assert store.get("kc-2").state == states.RUNNING
        assert "create_activation" in launcher.calls

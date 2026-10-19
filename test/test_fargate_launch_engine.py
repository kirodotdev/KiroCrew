"""The Fargate engine conforms to the launch Protocol and owns only what it marked.

Two things are pinned here. The engine satisfies ``LaunchEngine`` structurally and
is injectable where the EC2 one is, so PR 3's seam needs no change. And teardown's
ownership rule refuses exactly the cases that must be refused -- including a
launch tag written into the managed marker, which is the shape that would
otherwise orphan a task from teardown.

Provision and teardown against a scripted AWS chokepoint, the backend-agnostic comparison,
two mutation anchors and the credential-recipient check are in
``test_fargate_launch_engine_aws.py``; the lifetime sweep and the running cap that bound a
launched task are in ``test_fargate_launch_engine_bounds.py``; the constants and builders
they share are in ``fargate_launch_engine_helpers.py``.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import threading
import typing
from typing import Callable

import pytest
from fargate_launch_engine_helpers import (
    ARN,
    EXPECTED_TARGET,
    LAUNCH_TAG_KEY,
    OTHER_ARN,
    REGISTRABLE_ARN,
    RUNTIME_ID,
    TAG,
    TASK_ID,
    _spec,
)

from kiro_crew.cloud import fargate, fargate_engine
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate_engine import (
    MANAGED_TAG_VALUE,
    FargateLaunchEngine,
    FargateSigninHandle,
    Ownership,
    TaskSighting,
    classify_task,
    plan_teardown,
)
from kiro_crew.cloud.launch_job import LaunchEngine, SigninHandle
from kiro_crew.instances.validation import split_ecs_target
from kiro_crew.platform.defaults import FARGATE_PROVISIONER_ID
from kiro_crew.platform.interfaces import BUILTIN_PROVISIONER_ID

STARTED_BY = "kirocrew-cloud/kc-a1b2c3"


def _task(last_status: str, *, task_arn: str = REGISTRABLE_ARN, **over: object) -> dict:
    """One ``DescribeTasks`` entry, as the API shapes it."""
    entry: dict = {"taskArn": task_arn, "lastStatus": last_status}
    entry.update(over)
    return entry


def _running_task(*, task_arn: str = REGISTRABLE_ARN) -> dict:
    return _task(
        "RUNNING",
        task_arn=task_arn,
        containers=[{"name": "crew", "runtimeId": RUNTIME_ID}],
    )


def _stopped_task(stopped_reason: str) -> dict:
    return _task("STOPPED", stoppedReason=stopped_reason, containers=[])


def _crashed_task(stopped_reason: str) -> dict:
    """STOPPED, but its container ran long enough to be assigned a runtime id.

    The startup-crash shape, and the one ``_stopped_task`` cannot express: ECS
    keeps reporting ``runtimeId`` for a container that reached RUNNING and then
    exited, so this is a task that answers every question needed to build a
    target and is nonetheless unreachable.
    """
    return _task(
        "STOPPED",
        stoppedReason=stopped_reason,
        containers=[{"name": fargate.CREW_CONTAINER_NAME, "runtimeId": RUNTIME_ID}],
    )


def _winding_down(last_status: str) -> dict:
    """A task in one of the states between ``RUNNING`` and ``STOPPED``.

    Billable, so a billing predicate calls it running; still carrying the
    container's ``runtimeId``, so every field a target needs is readable; and
    unreachable, because the ENI is being torn down. ``stoppedReason`` is set
    because ECS records the cause once the stop begins, not once it completes.
    """
    return _task(
        last_status,
        desiredStatus="STOPPED",
        stoppedReason="Task stopped by user",
        containers=[{"name": fargate.CREW_CONTAINER_NAME, "runtimeId": RUNTIME_ID}],
    )


def _never_sleep(_seconds: object) -> None:
    """A poll sleep that must not happen: these reads end on their first answer."""
    raise AssertionError("the wait slept when the first read already settled it")


def _patch_describe(monkeypatch, answers: list) -> None:
    """Serve *answers* to successive ``describe_task`` calls, last one repeating.

    Patched at the engine's own read rather than at the AWS chokepoint because
    what is under test is the POLL -- how many reads it takes and when it stops --
    and a scripted sequence is the only way to express a task that is
    PROVISIONING and then RUNNING. ``None`` entries stand for the task ECS no
    longer lists, which is what ``describe_task`` returns for it.
    """
    remaining = list(answers)

    def _describe_task(self, *, task_arn: str, profile: str, region: str):
        entry = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return None if entry is None else fargate_engine.sighting_from_task(entry)

    monkeypatch.setattr(FargateLaunchEngine, "describe_task", _describe_task)


def _marked(tag: str = TAG, **over: object) -> TaskSighting:
    fields: dict = {
        "task_arn": ARN,
        "tags": {MANAGED_TAG_KEY: MANAGED_TAG_VALUE, LAUNCH_TAG_KEY: tag},
        "started_by": STARTED_BY,
        "last_status": "RUNNING",
    }
    fields.update(over)
    return TaskSighting(**fields)  # type: ignore[arg-type]


def _plan(sightings: list[TaskSighting]) -> fargate_engine.TeardownPlan:
    return plan_teardown(sightings, launch_tag=TAG, started_by=STARTED_BY)


def _classify(sighting: TaskSighting) -> Ownership:
    return classify_task(sighting, launch_tag=TAG, started_by=STARTED_BY)


# ── The Protocol seam ────────────────────────────────────────────────────────


def test_engine_satisfies_the_launch_engine_protocol() -> None:
    """Structural conformance, checked by signature and not by duck-typing luck.

    ``LaunchEngine`` is a plain ``Protocol``, so a missing method or a renamed
    keyword is only discovered where the engine is called. Comparing signatures
    here makes that a test failure instead of a launch failure.
    """
    engine = FargateLaunchEngine()
    for name, expected in inspect.getmembers(LaunchEngine, inspect.isfunction):
        if name.startswith("_"):
            continue
        actual = getattr(engine, name, None)
        assert actual is not None, f"FargateLaunchEngine is missing {name}"
        want = inspect.signature(expected)
        got = inspect.signature(actual)
        want_params = [p for p in want.parameters if p != "self"]
        got_params = list(got.parameters)
        assert got_params == want_params, f"{name}: {got_params} != {want_params}"


def test_engine_is_injectable_where_the_ec2_engine_is() -> None:
    """The seam returns it unchanged through the real resolution path.

    ``LaunchEngine`` is not ``@runtime_checkable``, so an ``isinstance`` check
    cannot stand in for this -- and adding that decorator to someone else's
    Protocol to make a test pass would be the wrong direction. Instead this
    exercises what actually resolves the engine: ``handlers_cloud._engine`` reads
    ``state.cloud_launch_engine`` (typed ``Any`` in ``state_parts/dashboard_state.py``) and returns
    the injected hook ahead of the provisioner seam.
    """
    from kiro_crew.dashboard import handlers_cloud

    class _StubState:
        def __init__(self, engine: object) -> None:
            self.cloud_launch_engine = engine

    engine = FargateLaunchEngine()
    resolved = handlers_cloud._engine(_StubState(engine))  # type: ignore[arg-type]
    assert resolved is engine


# ── Sign-in: there is nothing to wait for ────────────────────────────────────


def test_signin_handle_has_every_signin_handle_protocol_member() -> None:
    """The handle carries every member ``SigninHandle`` declares, attributes included.

    ``SigninHandle`` declares five ATTRIBUTES (``already_logged_in``, ``url``,
    ``code``, ``ports``, ``error``) beside its two methods, and ``run_launch``
    reads ``handle.error`` and ``handle.already_logged_in`` unconditionally
    before anything else. A check
    written against the Protocol's ``def`` lines alone would pass a handle with
    no attributes at all, and that handle raises ``AttributeError`` on every
    launch. So the member set is taken from the Protocol's annotations AND its
    functions, and each one must resolve on a real handle.

    ``SigninHandle`` is not ``@runtime_checkable`` and it is another module's
    Protocol, so ``isinstance`` is not available and the members are asserted
    directly.
    """
    handle = FargateSigninHandle(task_arn=ARN)
    attributes = set(typing.get_type_hints(SigninHandle))
    methods = {n for n, _ in inspect.getmembers(SigninHandle, inspect.isfunction) if n[0] != "_"}
    assert attributes == {"already_logged_in", "url", "code", "ports", "error"}
    assert methods == {"wait", "close", "abort"}
    for name in attributes | methods:
        assert hasattr(handle, name), f"FargateSigninHandle is missing {name}"


def test_signin_handle_reports_already_signed_in_with_no_prompt() -> None:
    """``already_logged_in`` is true as a fact, and there is no prompt to show.

    The container is handed its credential at run time, so ``run_launch``'s
    already-signed-in branch -- mark the step done, show nothing -- is the correct
    path and not a shortcut. An empty ``url`` is what keeps the prompt branch from
    ever being taken.
    """
    handle = FargateSigninHandle(task_arn=ARN)
    assert handle.already_logged_in is True
    assert handle.url == ""
    assert handle.code == ""
    assert handle.ports == []


def test_signin_completes_without_waiting() -> None:
    """No interactive sign-in exists for this container, so the handle returns at once."""
    handle = FargateLaunchEngine().begin_signin(instance_id=ARN, profile="p", region="us-west-2")
    assert isinstance(handle, FargateSigninHandle)
    assert handle.wait(threading.Event()) is True
    handle.close()


def test_signin_honours_a_cancel_already_set() -> None:
    """A launch cancelled before this step must not report the step as completed."""
    cancelled = threading.Event()
    cancelled.set()
    handle = FargateSigninHandle(task_arn=ARN)
    assert handle.wait(cancelled) is False


def test_signin_honours_the_default_login_target() -> None:
    """The Builder ID target, explicit or omitted, is the already-signed-in path.

    ``login_target`` is the keyword the ``LaunchEngine`` Protocol gained for
    Identity Center. The empty target is what every managed launch carried
    before it existed, so passing it must change nothing: no ``error``, and the
    handle still reports the step done without a prompt.
    """
    from kiro_crew.cloud.login_target import KiroLoginTarget

    engine = FargateLaunchEngine()
    for target in (None, KiroLoginTarget()):
        handle = engine.begin_signin(
            instance_id=ARN, profile="p", region="us-west-2", login_target=target
        )
        assert handle.error == ""
        assert handle.already_logged_in is True


def test_identity_center_target_is_refused_at_preflight_before_provision() -> None:
    """A non-default identity fails the launch before anything is provisioned or billed.

    The container is credentialed by its API key and never signs in, so an
    Identity Center target has nothing to act on. The engine says so through
    ``login_target_refusal``, which ``launch_job._check_signin_target_supported``
    reads at PREFLIGHT: the job fails there, ``provision`` never runs, and no
    task exists to strand. Refusing later, at the sign-in step, would land after
    the task is billing, which is the shape this engine must never take.
    """
    from kiro_crew.cloud.login_target import KiroLoginTarget

    target = KiroLoginTarget(
        license="pro", start_url="https://example.awsapps.com/start", region="us-west-2"
    )
    assert not target.is_default
    engine = FargateLaunchEngine()
    reason = engine.login_target_refusal(target)
    assert reason
    assert "API key" in reason
    assert engine.login_target_refusal(KiroLoginTarget()) == ""

    job = lj.LaunchJob(
        id="j",
        profile="p",
        region="us-west-2",
        size_key="balanced",
        instance_id="",
        login_target=target,
    )
    with pytest.raises(RuntimeError, match="API key"):
        lj._check_signin_target_supported(engine, job)
    # And the runner's preflight is the same call, so the launch never provisions.
    job.login_target = KiroLoginTarget()
    lj._check_signin_target_supported(engine, job)


def test_signin_time_identity_center_target_is_a_programming_error_guard() -> None:
    """``begin_signin`` with a non-default target raises: preflight was skipped.

    Through ``run_launch`` this path is unreachable, because the preflight check
    above fails the job first. The raise is the guard for a caller that bypassed
    preflight, mirroring the never-taken branch in
    ``launch_job._begin_signin_with_target``; it is not a refusal channel, so
    the handle's ``error`` stays empty on every path that returns one.
    """
    from kiro_crew.cloud.login_target import KiroLoginTarget

    target = KiroLoginTarget(
        license="pro", start_url="https://example.awsapps.com/start", region="us-west-2"
    )
    with pytest.raises(RuntimeError, match="API key"):
        FargateLaunchEngine().begin_signin(
            instance_id=ARN, profile="p", region="us-west-2", login_target=target
        )
    handle = FargateLaunchEngine().begin_signin(instance_id=ARN, profile="p", region="us-west-2")
    assert handle.error == ""


def test_register_resolves_a_target_and_registers_the_fargate_crew(monkeypatch) -> None:
    """A launched task lands in the registry, so the crew is switchable at once.

    ``provision`` hands over a task ARN, which the registry cannot address. This
    asserts the four things the record must carry -- the ECS target composed from
    the DescribeTasks read, the ``fargate`` method, the port the task definition
    publishes, and the Fargate provisioner id -- because each one wrong is its own
    silent failure: a stale target connects to nothing, ``ssm`` is refused by the
    registry, the EC2 lane's default port forwards to a port the container does
    not listen on, and the EC2 provisioner id resolves the EC2 engine for a task.
    """
    calls: list[dict] = []

    def _register_instance(target, **kw):
        calls.append({"target": target, **kw})
        return "inst-1"

    monkeypatch.setattr(fargate_engine.connect, "register_instance", _register_instance)
    _patch_describe(monkeypatch, [_running_task()])

    FargateLaunchEngine(_spec()).register(
        instance_id=REGISTRABLE_ARN, tag=TAG, profile="p", region="us-west-2"
    )

    assert len(calls) == 1, calls
    assert calls[0]["target"] == EXPECTED_TARGET
    assert calls[0]["connection_method"] == "fargate"
    assert calls[0]["remote_port"] == fargate.FRONT_PORT
    assert calls[0]["provisioner_id"] == FARGATE_PROVISIONER_ID
    # The id the platform resolves an engine from, so the EC2 default here would
    # hand a task to the EC2 engine and label it an EC2 stack in the dashboard.
    assert calls[0]["provisioner_id"] != BUILTIN_PROVISIONER_ID
    assert TAG in calls[0]["name"]
    # split_ecs_target is the registry's own reader: a target it rejects would be
    # refused at reg.add as a traceback about a string nobody typed.
    assert split_ecs_target(calls[0]["target"]) == (
        "crews",
        TASK_ID,
        RUNTIME_ID,
    )


def test_register_raises_registration_unavailable_when_the_registry_declines(
    monkeypatch,
) -> None:
    """``register_instance`` returns None for BOTH "feature absent" and "write
    raised". Ignoring it would report the crew as added while it is absent."""
    monkeypatch.setattr(fargate_engine.connect, "register_instance", lambda *a, **k: None)
    _patch_describe(monkeypatch, [_running_task()])

    with pytest.raises(lj.RegistrationUnavailable) as caught:
        FargateLaunchEngine(_spec()).register(
            instance_id=REGISTRABLE_ARN, tag=TAG, profile="p", region="us-west-2"
        )
    # The message has to carry what a human needs to finish by hand.
    assert REGISTRABLE_ARN in str(caught.value)
    assert EXPECTED_TARGET in str(caught.value)


def test_register_fails_the_launch_when_the_task_is_not_running(monkeypatch) -> None:
    """A task that stopped has nothing to register and nothing to tear down.

    The raise is a plain error and NOT :class:`RegistrationUnavailable`, which
    means "the launch stands, only the row is missing". A stopped task has no
    running crew behind it, so reporting the launch as launched would leave the
    job DONE for something unreachable and unbillable.
    """
    reached: list[object] = []
    monkeypatch.setattr(
        fargate_engine.connect, "register_instance", lambda *a, **k: reached.append(a)
    )
    _patch_describe(monkeypatch, [_stopped_task("CannotPullContainerError: manifest unknown")])

    with pytest.raises(RuntimeError) as caught:
        FargateLaunchEngine(_spec()).register(
            instance_id=REGISTRABLE_ARN, tag=TAG, profile="p", region="us-west-2"
        )
    assert reached == []
    assert not isinstance(caught.value, lj.RegistrationUnavailable)
    assert "CannotPullContainerError" in str(caught.value)


def test_register_fails_the_launch_for_a_task_that_crashed_after_starting(monkeypatch) -> None:
    """The startup-crash path: a container that reached RUNNING keeps its runtime
    id, so every field a target needs is readable for a task that is already
    dead. This is the case a runtime-id-first read reports as a good launch."""
    reached: list[object] = []
    monkeypatch.setattr(
        fargate_engine.connect, "register_instance", lambda *a, **k: reached.append(a)
    )
    _patch_describe(monkeypatch, [_crashed_task("Essential container in task exited")])

    with pytest.raises(RuntimeError) as caught:
        FargateLaunchEngine(_spec()).register(
            instance_id=REGISTRABLE_ARN, tag=TAG, profile="p", region="us-west-2"
        )
    assert reached == []
    assert not isinstance(caught.value, lj.RegistrationUnavailable)
    assert "Essential container in task exited" in str(caught.value)


def test_register_keeps_a_live_but_unregistered_task_non_fatal(monkeypatch) -> None:
    """A read that failed says nothing about the task, so the launch stands.

    The counterpart to the two above: the task may be running and billing, and a
    red card over the launch would hide the crew whose remedy is to add it by
    hand or tear it down.
    """
    reached: list[object] = []
    monkeypatch.setattr(
        fargate_engine.connect, "register_instance", lambda *a, **k: reached.append(a)
    )

    def _raise(**_kw):
        raise AWSError("AccessDeniedException: denied", action="ecs:DescribeTasks")

    monkeypatch.setattr(FargateLaunchEngine, "describe_task", lambda self, **kw: _raise(**kw))

    with pytest.raises(lj.RegistrationUnavailable) as caught:
        FargateLaunchEngine(_spec()).register(
            instance_id=REGISTRABLE_ARN, tag=TAG, profile="p", region="us-west-2"
        )
    assert reached == []
    assert "AccessDeniedException" in str(caught.value)


class TestTheRuntimeIdIsReadFromTheCrewContainer:
    """``runtimeId`` is the one ECS target field ``RunTask`` does not answer with."""

    def test_it_is_matched_by_container_name_not_position(self) -> None:
        """A sidecar added to the task definition must not move the crew's channel.

        Position is not identity: ``containers[0]`` would point every registration
        at the sidecar's runtime id, and the tunnel would forward to whatever that
        container listens on.
        """
        sighting = fargate_engine.sighting_from_task(
            {
                "taskArn": REGISTRABLE_ARN,
                "lastStatus": "RUNNING",
                "containers": [
                    {"name": "log-router", "runtimeId": "f" * 32 + "-99"},
                    {"name": fargate.CREW_CONTAINER_NAME, "runtimeId": RUNTIME_ID},
                ],
            }
        )
        assert sighting.runtime_id == RUNTIME_ID

    def test_a_task_with_no_containers_yet_reports_no_runtime_id(self) -> None:
        """PROVISIONING carries no containers, and absent is not an error."""
        sighting = fargate_engine.sighting_from_task(
            {"taskArn": REGISTRABLE_ARN, "lastStatus": "PROVISIONING"}
        )
        assert sighting.runtime_id == ""

    def test_a_started_container_without_a_runtime_id_reports_none(self) -> None:
        """Present-but-empty and absent are the same to the caller: no target."""
        sighting = fargate_engine.sighting_from_task(
            {
                "taskArn": REGISTRABLE_ARN,
                "lastStatus": "RUNNING",
                "containers": [{"name": fargate.CREW_CONTAINER_NAME}],
            }
        )
        assert sighting.runtime_id == ""


class TestAwaitingTheRegistrationTarget:
    """The bounded DescribeTasks poll, and a named reason for every way it ends."""

    def test_it_polls_until_the_container_reports_a_runtime_id(self, monkeypatch) -> None:
        """A Fargate task is PROVISIONING for tens of seconds. Reading once would
        make registration fail for every launch that is not already warm."""
        slept: list[int] = []
        monkeypatch.setattr(fargate_engine, "_sleep", slept.append)
        _patch_describe(
            monkeypatch,
            [
                _task("PROVISIONING", containers=[]),
                _task("PENDING", containers=[{"name": fargate.CREW_CONTAINER_NAME}]),
                _running_task(),
            ],
        )

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert (target, reason, alive) == (EXPECTED_TARGET, "", True)
        # Two waits for three reads: it returns ON the answer, never after a
        # further sleep, so a task that starts fast costs only the time it took.
        assert slept == [
            fargate_engine.REGISTER_TARGET_POLL_SECONDS,
            fargate_engine.REGISTER_TARGET_POLL_SECONDS,
        ]

    def test_a_container_that_started_then_died_yields_no_target(self, monkeypatch) -> None:
        """``runtimeId`` outlives the container that had it, so a crashed task
        still carries one. Composing a target from it would hand the registry a
        well-formed address for something dead and call the launch connectable."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        _patch_describe(monkeypatch, [_crashed_task("Essential container in task exited")])

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert target == ""
        assert alive is False
        assert "Essential container in task exited" in reason
        # The runtime id was READ -- the guard is what refuses it, not its absence.
        assert fargate_engine.sighting_from_task(_crashed_task("x")).runtime_id == RUNTIME_ID

    @pytest.mark.parametrize("status", ["DEACTIVATING", "STOPPING", "DEPROVISIONING"])
    def test_a_task_on_its_way_down_is_refused_not_registered(self, monkeypatch, status) -> None:
        """The states between ``RUNNING`` and ``STOPPED`` are billable, keep the
        container's runtime id, and cannot be connected to: the ENI is being torn
        down. Asking whether the task is billable admits all three, so the poll has
        to ask whether it is serving."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        _patch_describe(monkeypatch, [_winding_down(status)])

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert target == ""
        assert alive is False
        assert status in reason
        # Billable and carrying a runtime id: neither fact makes it reachable.
        sighting = fargate_engine.sighting_from_task(_winding_down(status))
        assert sighting.is_running is True
        assert sighting.runtime_id == RUNTIME_ID
        assert sighting.is_serving is False

    def test_a_task_told_to_stop_is_refused_while_still_reporting_running(
        self, monkeypatch
    ) -> None:
        """``desiredStatus`` moves first. A task ECS has been told to stop is on its
        way down even while ``lastStatus`` still says ``RUNNING``, and that stop does
        not get reversed."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        doomed = _task(
            "RUNNING",
            desiredStatus="STOPPED",
            containers=[{"name": fargate.CREW_CONTAINER_NAME, "runtimeId": RUNTIME_ID}],
        )
        _patch_describe(monkeypatch, [doomed])

        target, _reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert target == ""
        assert alive is False

    @pytest.mark.parametrize("status", ["PROVISIONING", "PENDING", "ACTIVATING"])
    def test_a_task_not_serving_yet_is_polled_rather_than_refused(
        self, monkeypatch, status
    ) -> None:
        """The counterpart to the three above, and why one predicate cannot answer
        both: these are also not serving, but they are worth waiting for. Refusing
        them would fail a launch whose crew is seconds from coming up."""
        slept: list[int] = []
        monkeypatch.setattr(fargate_engine, "_sleep", slept.append)
        # The state persists, so the poll can only end on the budget.
        monkeypatch.setattr(
            FargateLaunchEngine,
            "describe_task",
            lambda self, **kw: fargate_engine.sighting_from_task(_task(status, containers=[])),
        )

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert target == ""
        assert alive is True
        assert status in reason
        assert sum(slept) == fargate_engine.REGISTER_TARGET_TIMEOUT_SECONDS

    def test_a_stopped_task_ends_the_wait_and_quotes_ecs(self, monkeypatch) -> None:
        """The cause lives in ``stoppedReason`` -- an image it could not pull, a
        secret it could not read -- so it is carried, not summarised away."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        _patch_describe(monkeypatch, [_stopped_task("ResourceInitializationError: secret")])

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert target == ""
        assert alive is False
        assert "ResourceInitializationError: secret" in reason

    def test_a_stopped_task_with_no_reason_still_says_so(self, monkeypatch) -> None:
        """ECS does not always give one, and an empty tail would read as truncated."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        _patch_describe(monkeypatch, [_stopped_task("")])

        _target, reason, _alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )
        assert "ECS gave no reason" in reason

    def test_a_task_seen_and_then_gone_is_not_rounded_to_a_state(self, monkeypatch) -> None:
        """``describe_task`` answers None for a task ECS has dropped. Once a read
        has SEEN the task, that absence is a disappearance: there is no crew left
        to register, and guessing 'running' would write a dead target."""
        slept: list[float] = []
        monkeypatch.setattr(fargate_engine, "_sleep", slept.append)
        _patch_describe(
            monkeypatch,
            [_task("PENDING", containers=[{"name": fargate.CREW_CONTAINER_NAME}]), None],
        )

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )
        assert target == ""
        assert alive is False
        assert "no longer lists" in reason
        # One wait: the PENDING read was polled through, the absence ended it.
        assert slept == [fargate_engine.REGISTER_TARGET_POLL_SECONDS]

    def test_a_task_absent_from_the_first_reads_is_polled_not_called_gone(
        self, monkeypatch
    ) -> None:
        """``RunTask`` and ``DescribeTasks`` are eventually consistent, so a task
        accepted moments ago is legitimately missing from the first read. Calling
        that gone fails the launch of a task that goes on to start and bill."""
        slept: list[int] = []
        monkeypatch.setattr(fargate_engine, "_sleep", slept.append)
        _patch_describe(monkeypatch, [None, None, _running_task()])

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert (target, reason, alive) == (EXPECTED_TARGET, "", True)
        # Two absences were waited through, not returned on.
        assert slept == [
            fargate_engine.REGISTER_TARGET_POLL_SECONDS,
            fargate_engine.REGISTER_TARGET_POLL_SECONDS,
        ]

    def test_a_task_ecs_never_listed_expires_as_may_come_up(self, monkeypatch) -> None:
        """A task no read ever saw is not a task that died. It ends on the budget,
        with ``alive`` True, because the remedy is to look again rather than to
        treat the launch as failed over a task that may be billing."""
        slept: list[float] = []
        monkeypatch.setattr(fargate_engine, "_sleep", slept.append)
        _patch_describe(monkeypatch, [None])

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )
        assert target == ""
        assert alive is True
        assert "did not list this task" in reason
        # Not the phrasing used for a task that WAS listed and stayed starting.
        assert "no longer lists" not in reason
        # It polled the whole budget rather than returning on the first absence.
        assert sum(slept) >= fargate_engine.REGISTER_TARGET_TIMEOUT_SECONDS

    def test_the_last_sleep_is_cut_to_the_remaining_budget(self, monkeypatch) -> None:
        """The ceiling is elapsed time, so each ``DescribeTasks`` round trip spends
        it. Sleeping a full poll interval regardless would push the documented
        180s past itself by one round trip per poll."""
        slept: list[float] = []
        clock = {"now": 0.0}
        read_cost = 1.0

        def _advance(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        def _slow_read(_self, *, task_arn: str, profile: str, region: str):
            # The latency the old per-iteration counter could not see.
            clock["now"] += read_cost
            return fargate_engine.sighting_from_task(
                _task("PENDING", containers=[{"name": fargate.CREW_CONTAINER_NAME}])
            )

        monkeypatch.setattr(fargate_engine, "_sleep", _advance)
        monkeypatch.setattr(fargate_engine, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(FargateLaunchEngine, "describe_task", _slow_read)

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert (target, alive) == ("", True)
        assert str(fargate_engine.REGISTER_TARGET_TIMEOUT_SECONDS) in reason
        # Waiting never overspends the budget, and the final wait is the remainder
        # rather than another whole interval.
        budget = fargate_engine.REGISTER_TARGET_TIMEOUT_SECONDS
        assert sum(slept) <= budget
        assert slept[-1] < fargate_engine.REGISTER_TARGET_POLL_SECONDS
        # The only overshoot is the read in flight when the budget ran out. A
        # per-iteration counter would instead have slept a full interval per poll
        # and overshot by one read latency for EVERY poll.
        assert clock["now"] <= budget + read_cost
        assert len(slept) * read_cost > read_cost, "the poll must have taken several reads"

    def test_a_denied_read_is_reported_as_itself(self, monkeypatch) -> None:
        """An AccessDenied on ecs:DescribeTasks is a permission to fix, not a
        task state. The AWS error names the caller ARN, so it is carried."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)

        def _raise(**_kw):
            raise AWSError(
                "AccessDeniedException: user arn:aws:iam::1:user/x", action="ecs:DescribeTasks"
            )

        monkeypatch.setattr(FargateLaunchEngine, "describe_task", lambda self, **kw: _raise(**kw))

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )
        assert target == ""
        assert "AccessDeniedException" in reason
        # A read that did not happen says nothing about the task. Calling it dead
        # here would fail a launch whose crew is running and billing.
        assert alive is True

    def test_the_budget_is_a_ceiling_and_its_expiry_is_named(self, monkeypatch) -> None:
        """A task still starting at the budget is the one case where looking again
        later works, so the reason says that instead of implying the task failed."""
        slept: list[int] = []
        monkeypatch.setattr(fargate_engine, "_sleep", slept.append)
        # Always PROVISIONING: the poll can only end on the budget.
        monkeypatch.setattr(
            FargateLaunchEngine,
            "describe_task",
            lambda self, **kw: fargate_engine.sighting_from_task(
                _task("PROVISIONING", containers=[])
            ),
        )

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=REGISTRABLE_ARN, profile="p", region="us-west-2"
        )

        assert target == ""
        assert alive is True
        assert str(fargate_engine.REGISTER_TARGET_TIMEOUT_SECONDS) in reason
        assert "Remote crew" in reason
        # Bounded, and bounded by the documented budget rather than by a count
        # that happens to match it.
        assert sum(slept) == fargate_engine.REGISTER_TARGET_TIMEOUT_SECONDS
        assert set(slept) == {fargate_engine.REGISTER_TARGET_POLL_SECONDS}

    def test_an_arn_naming_no_cluster_falls_back_to_the_spec(self, monkeypatch) -> None:
        """The short ARN format carries no cluster. The spec's is the only other
        source, and the read must still be attempted rather than refused."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        short = f"arn:aws:ecs:us-west-2:111122223333:task/{TASK_ID}"
        monkeypatch.setattr(
            FargateLaunchEngine,
            "describe_task",
            lambda self, **kw: fargate_engine.sighting_from_task(_running_task(task_arn=short)),
        )

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=short, profile="p", region="us-west-2"
        )
        assert (target, reason, alive) == (EXPECTED_TARGET, "", True)

    def test_coordinates_that_do_not_form_a_target_are_refused_here(self, monkeypatch) -> None:
        """The registry requires a 32-hex task id. A cluster or id that cannot be
        read back is refused where the reason can name the parts."""
        monkeypatch.setattr(fargate_engine, "_sleep", _never_sleep)
        # ARN with a SHORT task id: well-formed as an ARN, not as an ECS target.
        stumpy = "arn:aws:ecs:us-west-2:111122223333:task/crews/1111111111111111"
        monkeypatch.setattr(
            FargateLaunchEngine,
            "describe_task",
            lambda self, **kw: fargate_engine.sighting_from_task(_running_task(task_arn=stumpy)),
        )

        target, reason, alive = FargateLaunchEngine(_spec()).await_registration_target(
            task_arn=stumpy, profile="p", region="us-west-2"
        )
        assert target == ""
        assert alive is True
        assert "1111111111111111" in reason


# ── Ownership: the marker gates, the identifier names ────────────────────────


def test_marked_task_with_our_tag_is_ours() -> None:
    assert _classify(_marked()) is Ownership.OURS


def test_marked_task_with_another_tag_and_another_starter_belongs_to_another_launch() -> None:
    """Foreign tag AND foreign ``startedBy``: nothing about it points at this launcher."""
    assert (
        _classify(_marked(tag="kc-zzzzzz", started_by="kirocrew-cloud/kc-zzzzzz"))
        is Ownership.OTHER_LAUNCH
    )


def test_marked_task_we_started_whose_tag_names_another_launch_is_mislabelled() -> None:
    """Our ``startedBy`` with a tag that says otherwise is a task whose labels disagree.

    It is not OTHER_LAUNCH: that member says someone else's launch owns the task,
    and a task this launcher started is not someone else's. Calling it so is what
    lets teardown skip it in silence while it bills.
    """
    assert _classify(_marked(tag="kc-zzzzzz")) is Ownership.MISLABELLED


def test_marked_task_we_started_with_no_launch_tag_is_mislabelled() -> None:
    """A missing tag is the same disagreement as a foreign one: nothing authorises the claim."""
    sighting = TaskSighting(
        task_arn=ARN,
        tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
        started_by=STARTED_BY,
        last_status="RUNNING",
    )
    assert _classify(sighting) is Ownership.MISLABELLED


def test_launch_tag_written_into_the_managed_marker_is_not_ours() -> None:
    """A launch tag written into the managed marker does not make a task ours.

    Emitting ``{"key": MANAGED_TAG_KEY, "value": launch_tag}`` produces
    ``kirocrew:managed=kc-a1b2c3``. Every convention-following consumer demands
    ``== "true"`` (``ec2.py:804``, ``ec2.py:983``, the ``cloud/iam.py`` conditions),
    so this task classifies as UNMARKED -- and an UNMARKED task is never deleted.
    Without this, a mislabelled marker would orphan the task from every teardown.
    """
    sighting = TaskSighting(
        task_arn=ARN,
        tags={MANAGED_TAG_KEY: TAG, LAUNCH_TAG_KEY: TAG},
        started_by=STARTED_BY,
        last_status="RUNNING",
    )
    assert _classify(sighting) is Ownership.UNMARKED
    assert _plan([sighting]).delete == ()


def test_classification_requires_a_launch_tag() -> None:
    with pytest.raises(ValueError, match="launch tag is required"):
        classify_task(_marked(), launch_tag="", started_by=STARTED_BY)


# ── The identity key belongs to its writer, not to this module ───────────────


def test_ownership_is_read_under_the_writers_own_key() -> None:
    """Identity is looked up under the writer's key and under nothing else.

    A task tagged under the writer's key is ours. The same launch tag written
    under any other key is NOT ours -- and a module holding a private spelling
    would be reading under exactly such a key whenever the writer's spelling
    differs. Because the task still carries this launch's ``startedBy``, the
    misread does not pass as another launch's: it classifies MISLABELLED, and the
    plan declines to confirm instead of stopping nothing while reporting success.
    """
    assert _classify(_marked()) is Ownership.OURS
    elsewhere = _marked(
        tags={
            MANAGED_TAG_KEY: MANAGED_TAG_VALUE,
            "kirocrew:not-what-the-writer-uses": TAG,
        }
    )
    assert _classify(elsewhere) is Ownership.MISLABELLED
    plan = _plan([elsewhere])
    assert plan.delete == (), "a tag under a key nobody reads must match no task"
    assert plan.confirmed is False, "a key nobody reads must not pass as a clean teardown"
    assert ARN in plan.warning


def test_the_key_is_the_writers_object_and_no_caller_can_substitute_one() -> None:
    """The reader's key IS the writer's, and neither reader takes one as input.

    Importing the writer's own name is what makes a disagreement impossible; a
    key threaded in as a parameter only relocates the chance to get it wrong to
    every call site, and there is no import cycle that would force that.
    """
    assert fargate_engine.LAUNCH_TAG_KEY is fargate.LAUNCH_TAG_KEY
    for fn in (classify_task, plan_teardown):
        assert "launch_tag_key" not in inspect.signature(fn).parameters, fn.__name__
    with pytest.raises(TypeError):
        classify_task(  # type: ignore[call-arg]
            _marked(), launch_tag=TAG, launch_tag_key=LAUNCH_TAG_KEY, started_by=STARTED_BY
        )


def test_module_defines_no_name_its_owners_already_export() -> None:
    """This module assigns nothing that ``cloud.fargate`` or ``cloud.ec2`` owns.

    Derived from the owners' surfaces rather than from a list of names, so the
    NEXT value someone re-spells here is caught before review rather than by it.
    A ``kirocrew:`` literal is the same defect in its narrowest form, so it is
    checked too: either way the module would hold a second copy of a value whose
    meaning another module enforces, unchecked against that owner and free to
    drift.
    """
    owned = set(fargate.__all__) | {"MANAGED_TAG_KEY", "INSTANCE_TAG_KEY"}
    tree = ast.parse(inspect.getsource(fargate_engine))
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    respelled = sorted(assigned & owned)
    assert respelled == [], respelled
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("kirocrew:")
    ]
    assert literals == [], literals


# ── The teardown plan ────────────────────────────────────────────────────────


def test_nothing_present_confirms_removal() -> None:
    """Idempotent: a tag whose task is already gone is a success, not an error."""
    plan = _plan([])
    assert plan.delete == ()
    assert plan.confirmed is True
    assert plan.warning == ""


def test_our_task_is_deleted_and_confirmed() -> None:
    plan = _plan([_marked()])
    assert plan.delete == (ARN,)
    assert plan.confirmed is True


def test_another_launchs_task_is_left_alone_without_a_warning() -> None:
    """It has an owner, and that owner tears it down. Silence is correct here."""
    plan = _plan(
        [_marked(tag="kc-zzzzzz", task_arn=OTHER_ARN, started_by="kirocrew-cloud/kc-zzzzzz")]
    )
    assert plan.delete == ()
    assert plan.confirmed is True
    assert plan.warning == ""


def test_mislabelled_task_we_started_refuses_rather_than_confirming() -> None:
    """Marked, started by us, tagged as someone else's: refuse, and say which refusal.

    Deleting it would act on the ``startedBy`` match while the tag -- the field
    that authorises a claim -- says otherwise. Skipping it would report the
    account clean while a task this launcher started keeps billing. The warning
    must say the marker IS present, because the operator's next step is to check
    the tag, which is a different step from the one an unmarked task calls for.
    """
    plan = _plan([_marked(tag="kc-zzzzzz")])
    assert plan.delete == ()
    assert plan.confirmed is False
    assert ARN in plan.warning
    assert "still billing" in plan.warning
    assert f"{MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} marker, but" in plan.warning
    assert LAUNCH_TAG_KEY in plan.warning, "the operator must be told which tag to check"
    assert "carry no" not in plan.warning, "this is not the unmarked refusal"


def test_mislabelled_task_with_no_launch_tag_refuses_the_same_way() -> None:
    sighting = TaskSighting(
        task_arn=ARN,
        tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
        started_by=STARTED_BY,
        last_status="RUNNING",
    )
    plan = _plan([sighting])
    assert plan.delete == ()
    assert plan.confirmed is False
    assert ARN in plan.warning


def test_a_stopped_mislabelled_task_raises_no_warning() -> None:
    """Symmetric with the unmarked refusal: only a billable task is worth a warning."""
    plan = _plan([_marked(tag="kc-zzzzzz", last_status="STOPPED")])
    assert plan.delete == ()
    assert plan.confirmed is True
    assert plan.warning == ""


def test_both_refusals_together_are_told_apart_in_one_warning() -> None:
    """Two refused tasks for two reasons: the warning names each under its own reason."""
    unmarked = TaskSighting(
        task_arn=OTHER_ARN, tags={}, started_by=STARTED_BY, last_status="RUNNING"
    )
    plan = _plan([_marked(tag="kc-zzzzzz"), unmarked])
    assert plan.delete == ()
    assert plan.confirmed is False
    first, _, second = plan.warning.partition("still billing.")
    assert OTHER_ARN in first and "carry no" in first and ARN not in first
    assert ARN in second and "marker, but" in second and OTHER_ARN not in second


def test_unmarked_task_we_started_refuses_rather_than_guessing() -> None:
    """The ambiguous case: started by us, but nothing authorises a delete.

    Not deleted, because a ``startedBy`` match without the marker is a guess. Not
    ignored, because it bills. ``confirmed=False`` is how the existing caller
    (``launch_job.py:507-515``) turns this into a user-visible warning that
    something may still be running.
    """
    sighting = TaskSighting(task_arn=ARN, tags={}, started_by=STARTED_BY, last_status="RUNNING")
    plan = _plan([sighting])
    assert plan.delete == ()
    assert plan.confirmed is False
    assert ARN in plan.warning
    assert "still billing" in plan.warning


def test_started_by_is_required_so_the_refusal_cannot_be_switched_off() -> None:
    """Omitting ``started_by`` is a ``TypeError``, not a silent opt-out.

    The ambiguous case is defined by a ``startedBy`` match, so a caller that does
    not supply the value would get a plan in which no task is ever ambiguous and
    every unmarked task this launcher started is quietly ignored while it bills.
    """
    for fn in (classify_task, plan_teardown):
        param = inspect.signature(fn).parameters["started_by"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__
        assert param.default is inspect.Parameter.empty, fn.__name__
    sighting = TaskSighting(task_arn=ARN, tags={}, started_by=STARTED_BY, last_status="RUNNING")
    with pytest.raises(TypeError, match="started_by"):
        plan_teardown([sighting], launch_tag=TAG)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="started_by"):
        classify_task(_marked(), launch_tag=TAG)  # type: ignore[call-arg]


def test_unmarked_task_started_by_someone_else_is_not_our_problem() -> None:
    """A foreign task in the same cluster is neither deleted nor warned about."""
    sighting = TaskSighting(
        task_arn=OTHER_ARN, tags={}, started_by="some-service", last_status="RUNNING"
    )
    plan = _plan([sighting])
    assert plan.delete == ()
    assert plan.confirmed is True
    assert plan.warning == ""


def test_a_stopped_unmarked_task_raises_no_warning() -> None:
    """Only a billable task is worth warning about; STOPPED costs nothing."""
    sighting = TaskSighting(task_arn=ARN, tags={}, started_by=STARTED_BY, last_status="STOPPED")
    plan = _plan([sighting])
    assert plan.confirmed is True
    assert plan.warning == ""


def test_ours_and_an_ambiguous_task_together_stop_ours_and_decline_to_confirm() -> None:
    """Authority to stop our task comes from its own marker, not from its neighbours.

    The task we own is deleted: leaving it up because an unrelated task nearby
    lacks a marker would keep billing a resource this launcher is entitled to stop.
    The plan still returns ``False``, because reporting ``True`` with an
    unclaimable task running would tell the user billing had stopped when it had
    not. And the warning names both halves, so the user knows what was stopped and
    what still needs a hand.
    """
    plan = _plan(
        [
            _marked(),
            TaskSighting(task_arn=OTHER_ARN, tags={}, started_by=STARTED_BY, last_status="RUNNING"),
        ]
    )
    assert plan.delete == (ARN,)
    assert plan.confirmed is False
    assert ARN in plan.warning, "the warning must name what is being stopped"
    assert OTHER_ARN in plan.warning, "the warning must name what could not be claimed"
    assert "still billing" in plan.warning


# ── The invariant, derived from the taxonomy rather than from a list ─────────
#
# Every case above pins one instance. This one pins the rule the instances are
# instances of: a plan that returns confirmed=True must have a definite answer
# for EVERY sighting it examined. The input space is built from the module's own
# axes -- the marker, the launch tag, startedBy, the lifecycle state -- so a
# combination the plan skips in silence has to be one of these, and each of
# these has to carry a reason. A combination skipped in silence while carrying
# this launcher's startedBy matches no entry and turns the test red.

FOREIGN_TAG = TAG + "-not-this-launch"
FOREIGN_STARTED_BY = STARTED_BY + "-not-this-launcher"

#: Silence the plan is allowed, each with the fact that makes it definite. An
#: entry no combination uses is stale and fails the test; a silent skip no entry
#: covers is an unjustified one and fails the test.
_JUSTIFIED_SILENT_SKIPS: tuple[tuple[str, Callable[[TaskSighting], bool]], ...] = (
    (
        "startedBy is not this launcher's: whoever started it owns its teardown",
        lambda s: s.started_by != STARTED_BY,
    ),
    (
        "STOPPED is the one ECS state that does not bill, so there is nothing to stop",
        lambda s: not s.is_running,
    ),
)


def _every_sighting() -> list[TaskSighting]:
    """The product of every axis the classifier and the plan read.

    The marker axis carries the correct value, no value, and the launch tag
    written into the marker; the launch-tag axis carries ours, none, and another
    launch's; then both ``startedBy`` values and both billing states.
    """
    markers: tuple[str | None, ...] = (MANAGED_TAG_VALUE, None, TAG)
    launch_tags: tuple[str | None, ...] = (TAG, None, FOREIGN_TAG)
    starters = (STARTED_BY, FOREIGN_STARTED_BY)
    statuses = ("RUNNING", "STOPPED")
    out: list[TaskSighting] = []
    for marker, launch_tag, starter, status in itertools.product(
        markers, launch_tags, starters, statuses
    ):
        tags: dict[str, str] = {}
        if marker is not None:
            tags[MANAGED_TAG_KEY] = marker
        if launch_tag is not None:
            tags[LAUNCH_TAG_KEY] = launch_tag
        out.append(TaskSighting(task_arn=ARN, tags=tags, started_by=starter, last_status=status))
    return out


def test_every_confirmed_plan_has_a_definite_answer_for_every_sighting() -> None:
    """No combination is skipped in silence unless an entry says why it may be.

    For each combination the plan does exactly one of three things: deletes the
    task (then it is OURS and the plan confirms), names it in the warning (then
    the plan declines to confirm, and the task carries this launcher's
    ``startedBy`` -- another launch's task is never put in this operator's
    warning), or skips it in silence -- and silence has to match a justified
    entry. Every ``Ownership`` member has to be produced by the enumeration, so a
    member nothing reaches, or a combination no member describes, fails here too.
    """
    sightings = _every_sighting()
    assert len(sightings) == 36
    reached: set[Ownership] = set()
    used: set[str] = set()
    deleted = warned = silent = 0
    for sighting in sightings:
        kind = _classify(sighting)
        reached.add(kind)
        plan = _plan([sighting])
        if ARN in plan.delete:
            deleted += 1
            assert kind is Ownership.OURS, sighting
            assert plan.confirmed is True and plan.warning == "", sighting
        elif ARN in plan.warning:
            warned += 1
            assert plan.confirmed is False, sighting
            assert sighting.started_by == STARTED_BY, sighting
            assert sighting.is_running, sighting
        else:
            silent += 1
            assert plan.confirmed is True and plan.warning == "", sighting
            reasons = [reason for reason, fits in _JUSTIFIED_SILENT_SKIPS if fits(sighting)]
            assert reasons, f"skipped in silence with no stated reason: {sighting}"
            used.update(reasons)
    assert reached == set(Ownership), set(Ownership) - reached
    assert used == {reason for reason, _ in _JUSTIFIED_SILENT_SKIPS}, "a stale justification"
    assert (deleted, warned, silent) == (4, 8, 24)


# ── The refusal must be SEEN, not merely returned ────────────────────────────
#
# plan_teardown returning confirmed=False is worth nothing on its own: its value
# is entirely in launch_job surfacing it to the person whose task is still
# billing. These two tests drive the real consumers with an engine that declines
# to confirm, so they fail if that warning path stops firing -- which a test on
# the return value alone would not notice.


class _DecliningEngine:
    """A conforming engine whose teardown cannot confirm the delete."""

    def preflight(self, profile: str, region: str) -> None:
        return None

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        return ARN

    def begin_signin(self, *, instance_id: str, profile: str, region: str, login_target=None):
        return FargateSigninHandle(task_arn=instance_id)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        return None

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        return False


def test_unconfirmed_teardown_after_cancel_reaches_the_user(tmp_path) -> None:
    """``launch_job.py:510-514`` must turn a declined confirm into a visible warning."""
    from kiro_crew.cloud import launch_job as lj

    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    job = store.create(profile="dev", region="us-east-1", size_key="balanced")
    job.tag = TAG  # create() leaves this empty; run_launch assigns it before teardown
    lj._rollback_cancelled_stack(job, store, _DecliningEngine())
    assert job.error, "a teardown that did not confirm must record an error on the job"
    assert "did NOT confirm" in job.error
    assert "may still be running and billing" in job.error
    assert TAG in job.error, "the warning must name WHICH resource, or it is not actionable"


def test_unconfirmed_teardown_after_failed_provision_reaches_the_user(tmp_path) -> None:
    """``launch_job.py:550-553`` must do the same, and keep the original failure visible."""
    from kiro_crew.cloud import launch_job as lj

    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    job = store.create(profile="dev", region="us-east-1", size_key="balanced")
    job.tag = TAG
    job.error = "Setup failed: subnet unavailable."
    lj._rollback_failed_provision(job, store, _DecliningEngine())
    assert "subnet unavailable" in job.error, "the original failure must not be overwritten"
    assert "did NOT confirm" in job.error
    assert "may still be running and billing" in job.error
    assert TAG in job.error

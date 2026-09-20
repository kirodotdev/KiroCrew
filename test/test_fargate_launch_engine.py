"""The Fargate engine conforms to the launch Protocol and owns only what it marked.

Two things are pinned here. The engine satisfies ``LaunchEngine`` structurally and
is injectable where the EC2 one is, so PR 3's seam needs no change. And teardown's
ownership rule refuses exactly the cases that must be refused -- including a
launch tag written into the managed marker, which is the shape that would
otherwise orphan a task from teardown.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
import json
import logging
import threading
import typing
from typing import Callable

import pytest

from kiro_crew.cloud import fargate, fargate_engine
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud import sizes
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate import (
    CREW_TAG_KEY,
    Placement,
    SecretRef,
    TaskDefinitionSpec,
    credential_recipient,
    default_log_spec,
    revision_fingerprint,
    spec_binding,
)
from kiro_crew.cloud.fargate_engine import (
    DEFAULT_MAX_RUNNING_TASKS,
    DEFAULT_TASK_TTL_SECONDS,
    MANAGED_TAG_VALUE,
    FargateLaunchEngine,
    FargateLaunchSpec,
    FargateSigninHandle,
    Ownership,
    TaskBounds,
    TaskSighting,
    classify_task,
    plan_bounds_sweep,
    plan_teardown,
)
from kiro_crew.cloud.launch_job import LaunchEngine, SigninHandle

TAG = "kc-a1b2c3"
ARN = "arn:aws:ecs:us-west-2:111122223333:task/crews/1111111111111111"
OTHER_ARN = "arn:aws:ecs:us-west-2:111122223333:task/crews/2222222222222222"
STARTED_BY = "kirocrew-cloud/kc-a1b2c3"

#: The tag key carrying launch identity, as the CALLER supplies it. The engine
#: module refuses to spell this key itself because the module that writes the tag
#: owns it, so every test hands it over the same way production code must.
LAUNCH_TAG_KEY = "kirocrew:launch"


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
    ``state.cloud_launch_engine`` (typed ``Any`` at ``state.py:5254``) and returns
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


def test_register_is_a_silent_no_op() -> None:
    """``register`` carries no exit criteria; registry visibility is out of scope.

    A raise would fail an otherwise-successful launch, because ``run_launch`` calls
    the step unconditionally.
    """
    assert (
        FargateLaunchEngine().register(instance_id=ARN, tag=TAG, profile="p", region="us-west-2")
        is None
    )


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


# ── The AWS-touching methods, driven against a monkeypatched chokepoint ──────
#
# Every AWS call goes through cloud/aws.py's checked / checked_json. These tests
# replace that one chokepoint with a scripted double, so provision and teardown
# are exercised end to end without a credential and without boto3 — the same
# testability the fargate module's own tests rely on.


def _secret() -> SecretRef:
    return SecretRef(
        name="kirocrew/crew/demo/KIRO_API_KEY",
        arn="arn:aws:secretsmanager:us-west-2:111122223333:secret:kirocrew/crew/demo/KIRO_API_KEY-AbCdEf",
    )


_IMAGE = "123456789012.dkr.ecr.us-west-2.amazonaws.com/crew@sha256:" + "a" * 64


def _spec(**over: object) -> FargateLaunchSpec:
    """A launchable spec: every field set, INCLUDING the operator's confirmation.

    ``confirmed_recipient`` is derived through the shipped renderer rather than written out,
    so a change to how a recipient is rendered cannot leave this fixture confirming a string
    the engine does not produce -- the fixture would then test the renderer's output against
    itself, and every test through it would refuse for a reason none of them is about.
    """
    fields: dict = dict(
        placement=Placement(
            cluster="crews",
            subnets=("subnet-aaaa",),
            security_groups=("sg-bbbb",),
        ),
        image=_IMAGE,
        secrets=(_secret(),),
        cpu_architecture="X86_64",
        confirmed_recipient=credential_recipient(_IMAGE, (_secret(),)),
    )
    fields.update(over)
    return FargateLaunchSpec(**fields)


class _EcsDouble:
    """A scripted stand-in for the ``cloud/aws.py`` chokepoint.

    ``checked_json`` answers each ECS operation from its second argv token, and
    ``list_tasks`` is switchable so teardown can be driven over an empty cluster
    (nothing of ours remains) or a cluster holding this launch's own task.
    """

    def __init__(self) -> None:
        self.stops: list[str] = []
        self.tasks_for_teardown: list[dict] = []
        #: Tasks served only on the SECOND ListTasks page, so a reader that stops
        #: at the first page can be told from one that paginates.
        self.tasks_on_second_page: list[dict] = []
        self.described_fingerprint: str | None = "USE_REAL"
        self.run_requests: list[dict] = []
        #: Every ECS operation reached, in order. A test that must show an operation did
        #: NOT happen needs the whole sequence: asserting on `run_requests` alone would
        #: pass for a launch that registered a durable task definition and then stopped.
        self.ops: list[str] = []
        #: Every ListTasks argv, so a test can assert the scope of a read.
        self.list_calls: list[list[str]] = []
        #: Every DescribeTasks batch, in order. The API caps its ``tasks`` list, so a
        #: test needs the batches themselves rather than a call count to show a
        #: cluster-wide read was split rather than sent whole.
        self.describe_batches: list[list[str]] = []

    def checked_json(self, args, profile="", region="", *, action, timeout=60):
        op = args[1]
        self.ops.append(op)
        if op == "register-task-definition":
            return {"taskDefinition": {"revision": 1}}
        if op == "run-task":
            self.run_requests.append(json.loads(args[args.index("--cli-input-json") + 1]))
            return {"tasks": [{"taskArn": ARN}], "failures": []}
        if op == "describe-task-definition":
            fp = self.described_fingerprint
            if fp == "USE_REAL":
                fp = revision_fingerprint(_spec_taskdef(region))
            return {"tags": [{"key": "kirocrew:revision-key", "value": fp}]}
        if op == "list-tasks":
            # ListTasks documents startedBy as exclusive: "When you specify
            # startedBy as the filter, it must be the only filter that you use."
            # The real API answers a violation with InvalidParameterException, so a
            # double that accepted one would be no evidence that the request this
            # engine builds is well formed. --cluster is excluded because it scopes
            # the search rather than filtering it.
            if "--started-by" in args:
                clash = sorted(
                    f
                    for f in ("--desired-status", "--family", "--service-name", "--launch-type")
                    if f in args
                )
                if clash:
                    raise AssertionError(
                        "ListTasks rejects --started-by combined with "
                        f"{', '.join(clash)}: startedBy must be the only filter"
                    )
            self.list_calls.append(list(args))
            if "--next-token" in args:
                return {"taskArns": [t["taskArn"] for t in self.tasks_on_second_page]}
            page: dict = {"taskArns": [t["taskArn"] for t in self.tasks_for_teardown]}
            if self.tasks_on_second_page:
                page["nextToken"] = "page-2"
            return page
        if op == "describe-tasks":
            # Answer only what was asked for, the way DescribeTasks does, so a
            # paginated read is observed one page at a time rather than seeing the
            # whole pool on its first call.
            wanted = list(args[args.index("--tasks") + 1 : args.index("--include")])
            self.describe_batches.append(wanted)
            # The real API refuses above its documented ceiling, so a double that
            # accepted an over-sized batch would let the defect this guards pass as
            # a green test. Refusing here is what makes the batch size observable.
            if len(wanted) > fargate_engine.DESCRIBE_TASKS_MAX:
                raise AssertionError(
                    f"DescribeTasks was handed {len(wanted)} tasks, above the "
                    f"{fargate_engine.DESCRIBE_TASKS_MAX} the API accepts"
                )
            pool = [*self.tasks_for_teardown, *self.tasks_on_second_page]
            return {"tasks": [t for t in pool if t["taskArn"] in set(wanted)]}
        raise AssertionError(f"unexpected checked_json op {op!r}")

    def checked(self, args, profile="", region="", *, action, timeout=60):
        if args[1] == "stop-task":
            self.stops.append(args[args.index("--task") + 1])
            return ""
        raise AssertionError(f"unexpected checked op {args[1]!r}")


def _spec_taskdef(region: str):
    s = _spec()
    return TaskDefinitionSpec(
        image=s.image,
        secrets=s.secrets,
        cpu_architecture=s.cpu_architecture,
        log=default_log_spec(region),
    )


def _patch_aws(monkeypatch, double: _EcsDouble) -> None:
    from kiro_crew.cloud import aws as aws_mod

    monkeypatch.setattr(aws_mod, "checked_json", double.checked_json)
    monkeypatch.setattr(aws_mod, "checked", double.checked)


def test_preflight_validates_region_and_refuses_without_a_spec() -> None:
    """A refusal, not a probe: no ECS call, and it names the missing fields."""
    from kiro_crew.cloud.fargate.identity import DocumentRefused

    engine = FargateLaunchEngine(_spec())
    engine.preflight("p", "us-west-2")  # a spec present: returns without an AWS call

    with pytest.raises(ValueError, match="launch spec"):
        FargateLaunchEngine().preflight("p", "us-west-2")
    with pytest.raises(DocumentRefused, match="region"):
        FargateLaunchEngine(_spec()).preflight("p", "not a region")


def test_provision_registers_runs_and_returns_the_task_arn(monkeypatch) -> None:
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    arn = FargateLaunchEngine(_spec()).provision(
        tag=TAG, size_key="1024/2048", profile="p", region="us-west-2"
    )
    assert arn == ARN


def test_provision_refuses_a_tag_outside_the_charset(monkeypatch) -> None:
    _patch_aws(monkeypatch, _EcsDouble())
    with pytest.raises(ValueError, match="correlation value"):
        FargateLaunchEngine(_spec()).provision(
            tag="kc/slash", size_key="1024/2048", profile="p", region="us-west-2"
        )


def test_provision_refuses_an_unusable_size(monkeypatch) -> None:
    _patch_aws(monkeypatch, _EcsDouble())
    with pytest.raises(ValueError, match="Fargate"):
        FargateLaunchEngine(_spec()).provision(
            tag=TAG, size_key="999/999", profile="p", region="us-west-2"
        )


@pytest.mark.parametrize("tier_key", sizes.INTERACTIVE_TIER_KEYS)
def test_an_interactive_tier_key_resolves_to_the_ladders_own_shape(tier_key: str) -> None:
    """The three EC2 tier keys are accepted here, mapped from ``sizes.py``.

    A caller who never asked for Fargate carries one of these, and
    ``sizes.DEFAULT_TIER_KEY`` is one of them -- so the key refused by a lane that
    did not map them is the one a caller gets by not choosing.

    The expectation is DERIVED from the ladder, not written out: a raised ladder
    must reach this lane, and a second table here would keep asserting the
    retired shape while agreeing with itself.
    """
    tier = sizes.get_tier(tier_key)
    parsed = fargate_engine._parse_size(tier_key)
    assert parsed.cpu == str(tier.vcpu * 1024)
    assert parsed.memory == str(tier.ram_gb * 1024)
    assert parsed.ephemeral_storage_gib == tier.disk_gb


@pytest.mark.parametrize("tier_key", sizes.INTERACTIVE_TIER_KEYS)
def test_every_tier_shape_is_legal_on_fargates_own_table(tier_key: str) -> None:
    """Derived numbers run through the same bounds check a hand-written pair does.

    The mapping returns a KEY, so a tier whose shape leaves Fargate's cpu/memory
    table is refused by name rather than handed to ``RunTask``. This asserts none
    of the three does today, which is what makes the mapping usable rather than a
    refusal in a new place.
    """
    respelled = fargate_engine._tier_as_pair(tier_key)
    assert respelled is not None
    cpu, memory, _gib = respelled.split(fargate_engine.FARGATE_SIZE_SEP)
    low, high, step = fargate_engine.FARGATE_MEMORY_FOR_CPU[cpu]
    assert low <= int(memory) <= high
    assert (int(memory) - low) % step == 0


def test_a_non_tier_key_is_left_alone() -> None:
    """Only the three interactive keys are respelled.

    ``light-x86`` is a real ladder key and deliberately NOT one of them: mapping it
    would silently pick an architecture the caller did not ask this lane for.
    """
    assert fargate_engine._tier_as_pair("light-x86") is None
    assert fargate_engine._tier_as_pair("1024/2048") is None
    assert fargate_engine._tier_as_pair("nonsense") is None


def test_a_refusal_names_the_key_the_caller_passed() -> None:
    """Not its respelling, and it lists the tier keys as legal.

    A message naming ``8192/32768/60`` for a caller who typed ``balanced`` sends
    them looking for a value they never wrote.
    """
    with pytest.raises(ValueError) as raised:
        fargate_engine._parse_size("nonsense")
    message = str(raised.value)
    assert "'nonsense'" in message
    for tier_key in sizes.INTERACTIVE_TIER_KEYS:
        assert tier_key in message, f"{tier_key} is accepted but not listed as legal"


def test_a_refusal_for_a_tier_names_the_tier_the_caller_passed(monkeypatch) -> None:
    """Not the pair it was respelled into.

    A tier key cannot fail on today's ladder -- the test above asserts all three
    are legal -- so the only way to exercise this is to move the ladder, which is
    a thing that has happened. With a tier whose derived shape leaves Fargate's
    table, the caller must read back the word they typed: a message naming
    ``64000/256000/80`` for someone who wrote ``power`` sends them looking for a
    value they never wrote.
    """
    real_get_tier = sizes.get_tier

    def absurd(key: str):
        tier = real_get_tier(key)
        # A vCPU count no Fargate cpu size can match, so the respelling is refused.
        return dataclasses.replace(tier, vcpu=tier.vcpu * 1000)

    monkeypatch.setattr(sizes, "get_tier", absurd)

    with pytest.raises(ValueError) as raised:
        fargate_engine._parse_size("power")
    message = str(raised.value)
    assert "'power'" in message, f"the refusal must name the caller's key: {message}"
    respelled = fargate_engine._tier_as_pair("power")
    assert respelled is not None
    assert (
        respelled not in message
    ), f"the refusal names the respelling {respelled!r}, which the caller never wrote"


def test_a_storage_refusal_also_names_the_key_the_caller_passed(monkeypatch) -> None:
    """Both ephemeral-storage branches, not only the cpu and memory ones.

    The docstring's claim is universal, so every refusal has to carry it. A tier's
    GiB arrives from the DERIVED respelling, so the out-of-range branch is reached
    exactly the way the cpu branch is -- by moving the ladder -- and a caller who
    typed ``power`` must not read back a GiB number they never wrote.
    """
    with pytest.raises(ValueError) as raised:
        fargate_engine._parse_size("1024/2048/xx")
    assert "'1024/2048/xx'" in str(raised.value)

    real_get_tier = sizes.get_tier

    def oversized_disk(key: str):
        tier = real_get_tier(key)
        # One GiB past Fargate's ceiling. vcpu and ram_gb stay legal, so the
        # storage branch is the one this reaches.
        return dataclasses.replace(tier, disk_gb=fargate_engine.EPHEMERAL_STORAGE_MAX_GIB + 1)

    monkeypatch.setattr(sizes, "get_tier", oversized_disk)

    with pytest.raises(ValueError) as raised:
        fargate_engine._parse_size("power")
    message = str(raised.value)
    assert "ephemeral storage" in message, f"a different branch refused: {message}"
    assert "'power'" in message, f"the refusal must name the caller's key: {message}"


@pytest.mark.parametrize("size_key", ["1024/2048", "1024/2048/30"])
def test_fargates_own_vocabulary_is_unchanged(size_key: str) -> None:
    """The pair and triple spellings parse exactly as before the tier mapping."""
    parsed = fargate_engine._parse_size(size_key)
    parts = size_key.split(fargate_engine.FARGATE_SIZE_SEP)
    assert parsed.cpu == parts[0]
    assert parsed.memory == parts[1]
    assert parsed.ephemeral_storage_gib == (int(parts[2]) if len(parts) == 3 else None)


def test_provision_confirms_a_cached_revision_fingerprint(monkeypatch) -> None:
    """A remembered revision number is launched only after DescribeTaskDefinition
    confirms its fingerprint tag still equals the spec's."""
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec())
    engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")
    # Second launch is a cache hit; the double returns the real fingerprint, so it
    # confirms and launches revision 1 again.
    assert engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2") == ARN


def test_provision_refuses_a_stale_cached_revision(monkeypatch) -> None:
    """A cached number whose fingerprint tag differs from the spec's is a stale
    cache, not a launch: it raises rather than running the wrong content."""
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec())
    engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")
    double.described_fingerprint = "0" * 64  # the account now reports a different key
    with pytest.raises(AWSError, match="stale cache"):
        engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")


def _our_task(status: str = "RUNNING") -> dict:
    return {
        "taskArn": ARN,
        "startedBy": fargate_engine._started_by_for(TAG),
        "lastStatus": status,
        "tags": [
            {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
            {"key": LAUNCH_TAG_KEY, "value": TAG},
        ],
    }


def test_the_stamp_the_writer_sends_is_the_one_teardown_classifies_by(monkeypatch) -> None:
    """The value provision stamps is the value teardown hands the classifier, per launch.

    The classifier's `started_by` must identify ONE launch. A launcher-wide value
    would make two coexisting launches classify each other MISLABELLED, so every
    teardown would refuse to confirm while a sibling launch is up. This reads the
    `startedBy` actually sent to RunTask rather than restating the prefix, so a
    writer that changed the format or dropped the tag from it fails here.
    """
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec())
    engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")
    stamped = double.run_requests[-1]["startedBy"]
    assert TAG in stamped, "a launcher-wide stamp cannot tell two launches apart"

    other = "kc-zzzzzz"
    engine.provision(tag=other, size_key="1024/2048", profile="p", region="us-west-2")
    assert double.run_requests[-1]["startedBy"] != stamped, "the stamp must vary per launch"

    # Teardown of TAG classifies a task carrying the stamp the writer really sent.
    double.tasks_for_teardown = [
        {
            "taskArn": ARN,
            "startedBy": stamped,
            "lastStatus": "RUNNING",
            "tags": [
                {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
                {"key": LAUNCH_TAG_KEY, "value": TAG},
            ],
        }
    ]
    assert engine.teardown(tag=TAG, profile="p", region="us-west-2") is True
    assert double.stops == [ARN]


def test_teardown_stops_our_task_and_confirms(monkeypatch) -> None:
    double = _EcsDouble()
    double.tasks_for_teardown = [_our_task()]
    _patch_aws(monkeypatch, double)
    confirmed = FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2")
    assert confirmed is True
    assert double.stops == [ARN]


def test_teardown_over_an_empty_cluster_confirms_and_stops_nothing(monkeypatch) -> None:
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    assert FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2") is True
    assert double.stops == []


def test_teardown_refuses_an_unmarked_task_we_started(monkeypatch) -> None:
    """An unmarked task this launcher started returns unconfirmed and stops
    nothing — the ownership rule refuses to delete on a startedBy guess."""
    double = _EcsDouble()
    double.tasks_for_teardown = [
        {
            "taskArn": OTHER_ARN,
            "startedBy": fargate_engine._started_by_for(TAG),
            "lastStatus": "RUNNING",
            "tags": [],
        }
    ]
    _patch_aws(monkeypatch, double)
    assert FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2") is False
    assert double.stops == []


def test_teardown_refuses_a_mislabelled_task_and_says_which_one(monkeypatch, caplog) -> None:
    """A marked task this launcher started whose launch tag names another launch is
    refused, and the refusal NAMES it.

    ``LaunchEngine.teardown`` returns a bool, so the plan's warning has exactly one
    way out of the engine. ``launch_job`` turns the False into "it may still be
    running and billing", which tells the operator to look but not where, and an
    operator who cannot tell an unclaimable task from one of their own that is
    wrongly tagged cannot take either next step. This fails if the engine goes back
    to discarding ``TeardownPlan.warning``.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [
        {
            "taskArn": OTHER_ARN,
            "startedBy": fargate_engine._started_by_for(TAG),
            "lastStatus": "RUNNING",
            "tags": [
                {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
                {"key": LAUNCH_TAG_KEY, "value": "kc-zzzzzz"},  # another launch's tag
            ],
        }
    ]
    _patch_aws(monkeypatch, double)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.cloud.fargate_engine"):
        confirmed = FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2")
    assert confirmed is False
    assert double.stops == [], "a mislabelled task must not be stopped on a startedBy match"
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert OTHER_ARN in emitted, "the refusal must name the task the operator has to look at"


# ── Criterion 4: nothing above LaunchEngine branches on backend ──────────────


class _Ec2ShapedEngine:
    """A conforming engine standing in for the EC2 lane, for the comparison.

    ``RealLaunchEngine`` needs AWS to provision, so a byte-comparison of the two
    real engines is not what this asserts. The RFC's criterion is that nothing
    ABOVE the ``LaunchEngine`` boundary branches on backend: the orchestrator and
    the rollback path must treat any conforming engine identically. So the
    comparison drives the SAME orchestrator over two engines and asserts the same
    observable provision and teardown outcomes. Only provision and teardown, per
    the amended RFC section 8: registry visibility is out of this phase and only
    the EC2 engine produces a registry record, so register is deliberately not
    compared.
    """

    def preflight(self, profile: str, region: str) -> None:
        return None

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        return ARN

    def begin_signin(self, *, instance_id: str, profile: str, region: str, login_target=None):
        return FargateSigninHandle(task_arn=instance_id)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        return None

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        self.torn_down = True
        return True


def _run_through_engine(engine, store_root):
    """Resolve one engine through the real ``_engine`` seam and drive provision +
    teardown through it, returning the observable outcomes.

    Provision and teardown ONLY, per the amended RFC section 8: signin and
    register are out of this phase's comparison (only the EC2 engine produces a
    registry record). The launch job is created through the real store so the
    ``size_key`` and ``tag`` a provision reads come from the same orchestration
    state both engines see.
    """
    from kiro_crew.dashboard import handlers_cloud

    class _StubState:
        def __init__(self, e) -> None:
            self.cloud_launch_engine = e

    # The engine resolves through the same seam segment 1 injects through.
    resolved = handlers_cloud._engine(_StubState(engine))  # type: ignore[arg-type]
    assert resolved is engine

    store = lj.LaunchJobStore(root=store_root)
    job = store.create(
        profile="p",
        region="us-west-2",
        size_key="1024/2048",
        provider_id="aws_fargate",
    )
    job.tag = TAG

    identity = resolved.provision(
        tag=job.tag, size_key=job.size_key, profile=job.profile, region=job.region
    )
    teardown_confirmed = resolved.teardown(tag=job.tag, profile=job.profile, region=job.region)
    return {
        "provision_identity_present": bool(identity),
        "teardown_confirmed": teardown_confirmed,
    }


def test_both_engines_are_observably_identical_for_provision_and_teardown(
    monkeypatch, tmp_path
) -> None:
    """The same seam, resolving the EC2-shaped engine and the real Fargate engine,
    produces identical provision and teardown outcomes.

    The Fargate engine's AWS chokepoint is scripted; the EC2-shaped one returns
    directly. If anything above ``LaunchEngine`` branched on backend, one of these
    two observations would differ. Register is not compared: only the EC2 engine
    produces a registry record and registry visibility is out of this phase.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [_our_task()]
    _patch_aws(monkeypatch, double)

    ec2_like = _run_through_engine(_Ec2ShapedEngine(), tmp_path / "ec2")
    fargate = _run_through_engine(FargateLaunchEngine(_spec()), tmp_path / "fargate")

    assert ec2_like == fargate
    assert fargate["provision_identity_present"] is True
    assert fargate["teardown_confirmed"] is True


# ── Mutation anchors: the two claims that must be a test failure to break ─────


def test_mutation_managed_gate_is_value_not_key_presence(monkeypatch) -> None:
    """Loosening the managed-tag gate from ``== "true"`` to mere key presence must
    turn a test red.

    A task whose ``kirocrew:managed`` holds anything other than ``"true"`` — here
    the launch tag written into the marker — is UNMARKED and is never stopped. If
    the gate degraded to key presence, this task would classify OURS and teardown
    would stop it, so this assertion fails, which is the point.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [
        {
            "taskArn": ARN,
            "startedBy": fargate_engine._started_by_for(TAG),
            "lastStatus": "RUNNING",
            "tags": [
                {"key": MANAGED_TAG_KEY, "value": TAG},  # tag in the marker, not "true"
                {"key": LAUNCH_TAG_KEY, "value": TAG},
            ],
        }
    ]
    _patch_aws(monkeypatch, double)
    confirmed = FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2")
    assert double.stops == [], "an UNMARKED task must never be stopped"
    assert confirmed is False, "a running task we started but cannot claim is unconfirmed"


def test_mutation_teardown_returns_the_plan_confirmation(monkeypatch) -> None:
    """Making ``teardown`` return ``True`` on an unconfirmed plan must turn a test
    red.

    An unmarked running task this launcher started yields ``confirmed=False``, and
    teardown must return that unchanged. Hard-coding ``True`` here would report
    billing stopped when a task may still be running; this assertion is what
    catches that.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [
        {
            "taskArn": OTHER_ARN,
            "startedBy": fargate_engine._started_by_for(TAG),
            "lastStatus": "RUNNING",
            "tags": [],
        }
    ]
    _patch_aws(monkeypatch, double)
    assert FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2") is False


class TestTheCredentialRecipientIsConfirmed:
    """A launch delivers the model credential into a container ``cloud.json`` names, so the
    operator confirms WHICH container before it is handed over.

    This is what makes the file safe to leave as an ordinary file. Every alternative defends
    the file itself -- a read-only seal, a hidden mount, an alias check -- and each of those
    is a protection over a NAME that has to be complete across every platform and every way a
    name can be aliased. The confirmation comes from the launch request instead, so no
    filesystem property is load-bearing and rewriting the file produces a refusal that names
    both values rather than a launch nobody was asked about.
    """

    def test_an_unconfirmed_launch_is_refused_and_shows_the_recipient(self, monkeypatch):
        double = _EcsDouble()
        _patch_aws(monkeypatch, double)
        engine = FargateLaunchEngine(_spec(confirmed_recipient=""))

        with pytest.raises(ValueError) as exc:
            engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")

        # The refusal has to SHOW the recipient, or the operator cannot confirm it: the
        # value they must supply is derived from a file they may not have written.
        assert credential_recipient(_IMAGE, (_secret(),)) in str(exc.value)
        # And it must be THIS refusal, not the mismatch one. An empty confirmation also
        # fails the mismatch comparison, so the two checks overlap and nothing unconfirmed
        # gets through either way -- but an operator who confirmed nothing needs to be told
        # to confirm, not told that their confirmation disagrees with the file. Asserting
        # only "some ValueError naming the recipient" cannot tell the two apart, and a
        # mutation that deletes this branch then survives.
        assert "nothing in the request confirmed" in str(exc.value), str(exc.value)
        assert double.ops == [], "an unconfirmed launch reached AWS"

    def test_a_recipient_that_changed_since_it_was_confirmed_is_refused(self, monkeypatch):
        """The tampering case, which is the whole point.

        The operator confirms the image they chose; the block then names another. Both values
        appear in the refusal, because "does not match" without them leaves an operator
        unable to see WHAT was substituted.
        """
        double = _EcsDouble()
        _patch_aws(monkeypatch, double)
        theirs = credential_recipient(_IMAGE, (_secret(),))
        substituted = "public.ecr.aws/attacker/x@sha256:" + "b" * 64
        engine = FargateLaunchEngine(_spec(image=substituted, confirmed_recipient=theirs))

        with pytest.raises(ValueError) as exc:
            engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")

        message = str(exc.value)
        assert theirs in message and substituted in message, message
        assert double.ops == [], "a substituted recipient reached AWS"

    def test_a_confirmed_launch_proceeds(self, monkeypatch):
        """The complement, so neither test above can be satisfied by refusing everything."""
        double = _EcsDouble()
        _patch_aws(monkeypatch, double)

        engine = FargateLaunchEngine(_spec())
        engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")

        assert "run-task" in double.ops, double.ops

    def test_nothing_is_registered_before_the_recipient_is_confirmed(self, monkeypatch):
        """Refused before ``RegisterTaskDefinition``, not only before ``RunTask``.

        A revision is DURABLE in the account and carries the secret ARNs, so a confirmation
        checked only at run time would already have written the recipient down where a later
        launch could name the revision number directly.
        """
        double = _EcsDouble()
        _patch_aws(monkeypatch, double)
        engine = FargateLaunchEngine(_spec(confirmed_recipient="something else entirely"))

        with pytest.raises(ValueError):
            engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")

        assert "register-task-definition" not in double.ops, double.ops

    def test_the_check_runs_before_every_other_refusal_provision_makes(self):
        """Ordered FIRST, pinned structurally.

        The tag and architecture checks refuse too, and a recipient check sitting after them
        would let a launch with a malformed tag report the tag and never mention that its
        credential recipient was also unconfirmed -- so an operator fixing the tag would meet
        the real refusal only on the second attempt.
        """
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(inspect.getsource(FargateLaunchEngine.provision))
        body = ast.parse(source).body[0].body
        raising = [
            n
            for n in body
            if isinstance(n, ast.If) and any(isinstance(x, ast.Raise) for x in ast.walk(n))
        ]
        assert raising, "provision refuses nothing, so this pin measures nothing"
        first = ast.dump(raising[0])
        assert "confirmed_recipient" in first, ast.unparse(raising[0])


# ── What bounds a launched task ──────────────────────────────────────────────
#
# The executor for plan_teardown exists above. These cover the other half of
# what bounds a task: a task nobody tears down by hand still ends, and a launcher
# cannot accumulate tasks without limit. Every case here is keyed on the tag a
# read reports rather than on an ARN a test captured, so it covers the property
# and not one instance.

NOW = 1_800_000_000.0


def _crew() -> str:
    """The crew this fixture's spec binds to, DERIVED the way the engine derives it.

    Through ``spec_binding`` over the spec's own secret ARNs, not written out, for
    the same reason ``_spec`` derives its confirmed recipient through the shipped
    renderer: a hardcoded name would let the fixture agree with itself while the
    engine matched on something else, and every attribution test would then pass
    without attributing anything.
    """
    return spec_binding(_spec_taskdef("us-west-2")).crew


def _other_crew() -> str:
    """A crew name that is not this spec's, for the colleague on a shared cluster."""
    return _crew() + "-colleague"


def _bounded(
    *,
    tag: str = TAG,
    arn: str = ARN,
    age: float | None = 0.0,
    status: str = "RUNNING",
    tags: dict | None = None,
    started_by: str | None = None,
    crew: str | None = None,
) -> TaskSighting:
    """One sighting of a task this launcher started, *age* seconds ago.

    ``started_by`` is DERIVED from the tag by default, because the sweep checks
    that a task's stamp agrees with the task's own tag. That check is NOT a
    launcher or host discriminator -- the stamp is a fixed prefix plus that same
    tag, so a genuine task of any crew satisfies it -- which is why the default
    tags also carry the crew tag the sweep actually attributes by. ``age=None`` is
    the read that reports no start time at all.
    """
    return TaskSighting(
        task_arn=arn,
        tags=(
            {
                MANAGED_TAG_KEY: MANAGED_TAG_VALUE,
                LAUNCH_TAG_KEY: tag,
                CREW_TAG_KEY: _crew() if crew is None else crew,
            }
            if tags is None
            else tags
        ),
        started_by=fargate_engine._started_by_for(tag) if started_by is None else started_by,
        last_status=status,
        started_at=None if age is None else NOW - age,
    )


def _sweep(sightings: list[TaskSighting], *, crew: str | None = None, **over):
    bounds = TaskBounds(**over) if over else TaskBounds()
    return plan_bounds_sweep(
        sightings, bounds=bounds, crew=_crew() if crew is None else crew, now=NOW
    )


def test_the_default_lifetime_is_the_only_session_length_the_cloud_lane_states() -> None:
    """The TTL is READ from ``cloud/connect.py``, not chosen here.

    ``_safe_ttl`` falls back to ``"6h"`` for anything it cannot parse, which is the
    window this package already treats as one working session. If either number
    moves without the other, two answers to one question are live at once with
    nothing comparing them, which is what this pins.
    """
    from kiro_crew.cloud import connect

    assert connect._safe_ttl("nonsense") == "6h"
    assert DEFAULT_TASK_TTL_SECONDS == 6 * 60 * 60


def test_a_task_past_its_lifetime_is_stopped_and_named() -> None:
    plan = _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1)])
    assert plan.stop == (ARN,)
    assert plan.running == ()
    assert ARN in plan.warning, "the sweep must name what it stopped"


def test_a_task_inside_its_lifetime_is_left_alone() -> None:
    plan = _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS - 1)])
    assert plan.stop == ()
    assert plan.running == (ARN,)


def test_the_lifetime_bound_spans_launch_tags() -> None:
    """A TTL must reach a leftover whose tag nobody remembers.

    ``launch_job._new_tag`` mints a fresh random tag per launch, so a sweep scoped
    to one tag would bound the task that is certainly fine and never the
    leftovers. Two expired tasks under two different tags must both be stopped.
    """
    plan = _sweep(
        [
            _bounded(tag=TAG, arn=ARN, age=DEFAULT_TASK_TTL_SECONDS + 5),
            _bounded(tag="kc-zzzzzz", arn=OTHER_ARN, age=DEFAULT_TASK_TTL_SECONDS + 5),
        ]
    )
    assert sorted(plan.stop) == sorted([ARN, OTHER_ARN])


def test_an_unmarked_task_is_never_stopped_for_age() -> None:
    """The managed gate holds for the sweep exactly as it holds for teardown.

    Here the launch tag is written into the marker, which is the mistake that
    would otherwise cost a task: it classifies UNMARKED, so it is not ours to
    stop however old it is.
    """
    plan = _sweep(
        [
            _bounded(
                age=DEFAULT_TASK_TTL_SECONDS * 10,
                tags={MANAGED_TAG_KEY: TAG, LAUNCH_TAG_KEY: TAG},
            )
        ]
    )
    assert plan.stop == ()


def test_a_task_whose_stamp_disagrees_with_its_own_tag_is_left_alone() -> None:
    """A managed task whose ``startedBy`` is not the stamp its own launch tag
    derives is refused.

    This is what the stamp check buys, and it is worth stating narrowly because it
    is easy to read as more: the stamp is a fixed prefix plus the task's own tag,
    so it carries no host or crew component and a genuine task of ANY crew passes
    it. What fails here is a DISAGREEMENT between the two labels -- a task marked
    and tagged by this product but stamped with something else, which no launch of
    this product produces. Attribution to a crew is a separate rule, pinned by the
    colleague tests below.
    """
    plan = _sweep(
        [_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1, started_by="kirocrew-cloud-kc-someoneelse")]
    )
    assert plan.stop == ()
    assert plan.running == ()


def test_a_colleagues_expired_task_on_a_shared_cluster_is_left_alone() -> None:
    """The case a cluster-wide sweep exists to get right.

    A colleague's crew in the same cluster is the same product with the same tag
    KEYS and a stamp that agrees with its own tag, because that is what every
    genuine launch produces. So every check except the crew tag passes on it, and
    it is past the TTL. Stopping it would irreversibly end a running crew this
    launcher does not own, which is the guess teardown refuses to make.
    """
    plan = _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1, crew=_other_crew())])
    assert plan.stop == ()
    assert plan.running == ()


def test_a_colleagues_task_is_not_counted_against_this_crews_cap() -> None:
    """The cap measures this crew's population, not the cluster's.

    Counting a colleague's tasks would refuse this crew's launches for someone
    else's spend, which is a bound on the wrong thing.
    """
    plan = _sweep([_bounded(age=1.0, crew=_other_crew())])
    assert plan.running == ()
    assert plan.stop == ()


def test_only_this_crews_expired_task_is_stopped_when_both_are_present() -> None:
    """Both in one read, which is the shape a shared cluster actually returns:
    the sweep must separate them rather than take the whole cluster or none of it."""
    plan = _sweep(
        [
            _bounded(age=DEFAULT_TASK_TTL_SECONDS + 1),
            _bounded(arn=OTHER_ARN, age=DEFAULT_TASK_TTL_SECONDS + 1, crew=_other_crew()),
        ]
    )
    assert plan.stop == (ARN,)


def test_a_managed_task_with_no_crew_tag_is_not_stopped_but_is_counted_and_named() -> None:
    """A task that cannot be attributed is never stopped, and never silent either.

    It may be this crew's from a launcher too old to have written the tag, or a
    colleague's from the same, and nothing in the read decides which. So it takes
    the safe direction on stopping and the conservative one on spend: left
    running, counted against the cap, and named in the warning. Counting it can
    refuse a launch, and this module prefers that -- a refusal costs a launch, a
    wrong stop costs a running crew.
    """
    plan = _sweep(
        [
            _bounded(
                age=DEFAULT_TASK_TTL_SECONDS + 1,
                tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE, LAUNCH_TAG_KEY: TAG},
            )
        ]
    )
    assert plan.stop == ()
    assert plan.running == (ARN,)
    assert CREW_TAG_KEY in plan.warning
    assert ARN in plan.warning


def test_the_sweep_refuses_an_empty_crew_rather_than_matching_everything() -> None:
    """An empty crew is the value that would look like a wildcard to a reader.

    Left unchecked it matches no task's crew tag, so a caller who passed one by
    mistake would get a sweep that quietly stopped nothing -- or, if the check
    were written the other way round, one that swept the whole cluster. Refusing
    is the only answer that cannot be misread.
    """
    with pytest.raises(ValueError, match="crew name is required"):
        _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1)], crew="")


def test_a_task_carrying_no_launch_tag_is_left_alone() -> None:
    plan = _sweep(
        [_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1, tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE})]
    )
    assert plan.stop == ()


def test_a_stopped_task_is_neither_stopped_again_nor_counted() -> None:
    """STOPPED is the one ECS state that costs nothing, so it is outside both
    bounds: nothing to stop, and nothing to count against the cap."""
    plan = _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1, status="STOPPED")])
    assert plan.stop == ()
    assert plan.running == ()


def test_a_task_with_no_start_time_is_not_stopped_and_is_named() -> None:
    """An age that cannot be established is not an age of zero and not an age of
    forever. The task is left running -- stopping on an unknown age could stop one
    that started a second ago -- it still presses on the cap, and it is NAMED, so
    the case a bound cannot decide is auditable rather than silent."""
    plan = _sweep([_bounded(age=None)])
    assert plan.stop == ()
    assert plan.running == (ARN,)
    assert ARN in plan.warning


def test_a_lifetime_or_cap_of_zero_is_refused() -> None:
    """There is no spelling here for an unbounded task, and none for a cap that
    refuses every launch."""
    with pytest.raises(ValueError, match="ttl_seconds"):
        TaskBounds(ttl_seconds=0)
    with pytest.raises(ValueError, match="max_running"):
        TaskBounds(max_running=0)


def test_the_default_cap_is_the_fan_out_width_the_rfc_names() -> None:
    assert DEFAULT_MAX_RUNNING_TASKS == 10
    assert TaskBounds().max_running == DEFAULT_MAX_RUNNING_TASKS


# ── The bound executor, against the scripted chokepoint ──────────────────────


def _aged_task(
    arn: str = ARN,
    *,
    tag: str = TAG,
    started_at: object = "2020-01-01T00:00:00Z",
    crew: str | None = None,
):
    """An ECS-shaped task this launcher started, as a read returns it.

    Carries the crew tag because ``run_task_request`` writes one onto every task
    it starts; a fixture without it would be a task no launch produces.
    """
    task: dict = {
        "taskArn": arn,
        "startedBy": fargate_engine._started_by_for(tag),
        "lastStatus": "RUNNING",
        "tags": [
            {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
            {"key": LAUNCH_TAG_KEY, "value": tag},
            {"key": CREW_TAG_KEY, "value": _crew() if crew is None else crew},
        ],
    }
    if started_at is not None:
        task["startedAt"] = started_at
    return task


def test_reap_stops_an_expired_task_through_the_stop_channel(monkeypatch) -> None:
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task()]
    _patch_aws(monkeypatch, double)
    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert plan.stop == (ARN,)
    assert double.stops == [ARN]


def test_reap_reads_the_whole_cluster_rather_than_one_launch(monkeypatch) -> None:
    """The read must NOT filter by startedBy: a lifetime has to reach a task whose
    tag the caller has forgotten, and a startedBy filter is built from a tag."""
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task()]
    _patch_aws(monkeypatch, double)
    FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert double.list_calls, "reap must read the cluster"
    for args in double.list_calls:
        assert "--started-by" not in args


def test_reap_over_an_empty_cluster_stops_nothing(monkeypatch) -> None:
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert plan.stop == ()
    assert double.stops == []


def test_reap_without_a_spec_stops_nothing(monkeypatch) -> None:
    """No spec means no cluster to read, so there is nothing to claim. It reports
    that rather than raising, because provision calls it before its own refusals
    would have run."""
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    plan = FargateLaunchEngine().reap(profile="p", region="us-west-2")
    assert plan == fargate_engine.BoundsSweepPlan(stop=(), running=())
    assert double.list_calls == []


def test_the_reader_paginates_so_a_task_past_the_first_page_is_bounded(monkeypatch) -> None:
    """A cluster-wide read that stopped at the first page would leave every task
    past it unbounded, which is the state the sweep exists to end."""
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(ARN)]
    double.tasks_on_second_page = [_aged_task(OTHER_ARN, tag="kc-zzzzzz")]
    _patch_aws(monkeypatch, double)
    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert sorted(plan.stop) == sorted([ARN, OTHER_ARN])
    assert sorted(double.stops) == sorted([ARN, OTHER_ARN])


def test_a_cli_rendered_timestamp_is_read_as_an_age(monkeypatch) -> None:
    """The ``aws`` CLI renders an ECS timestamp as an ISO string with an offset. A
    reader that could not parse it would see every task as ageless and stop
    nothing, which looks exactly like a healthy cluster."""
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(started_at="2020-01-01T00:00:00.123000+00:00")]
    _patch_aws(monkeypatch, double)
    assert FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2").stop == (ARN,)


def test_the_describe_batch_is_capped_when_the_cli_merges_every_page(monkeypatch) -> None:
    """A cluster-wide read must split its DescribeTasks calls, not send the lot.

    This is the shape the REAL CLI produces and the earlier fixture could not
    express. The CLI auto-paginates ``ListTasks`` and merges every page into one
    response with no ``nextToken`` -- the same behaviour ``ec2.list_instances``
    relies on for ``get-resources``, with no token loop at all -- so the token
    loop runs ONCE and holds the whole cluster. Sizing the describe batch by "one
    page" therefore sizes it by nothing, and above the API's ceiling every
    ``DescribeTasks`` fails, which propagates through ``reap`` and fails every
    launch on exactly the busy cluster this sweep is for.

    Driven through ``reap`` rather than ``_sightings`` so the assertion covers the
    path a launch actually takes.
    """
    count = fargate_engine.DESCRIBE_TASKS_MAX * 2 + 7
    double = _EcsDouble()
    double.tasks_for_teardown = [
        _aged_task(f"{ARN}{index:04d}", tag=f"kc-b{index:04d}") for index in range(count)
    ]
    _patch_aws(monkeypatch, double)

    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")

    assert double.list_calls, "reap must read the cluster"
    assert len(double.list_calls) == 1, "the merged response carries no token to follow"
    assert double.describe_batches, "the read must describe what it listed"
    for batch in double.describe_batches:
        assert 0 < len(batch) <= fargate_engine.DESCRIBE_TASKS_MAX
    # Every ARN described exactly once: a cap that dropped or duplicated tasks
    # would bound the batch and lose the sweep.
    described = [arn for batch in double.describe_batches for arn in batch]
    assert sorted(described) == sorted(t["taskArn"] for t in double.tasks_for_teardown)
    assert len(described) == len(set(described))
    # And the sweep still reached its verdict on all of them.
    assert len(plan.stop) == count


def test_an_empty_cluster_describes_nothing(monkeypatch) -> None:
    """No ARNs means no DescribeTasks call at all, which the chunking must preserve:
    ``range`` over an empty list is what makes that true without a guard."""
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert double.describe_batches == []
    assert "describe-tasks" not in double.ops


def test_provision_sweeps_an_expired_task_before_it_launches(monkeypatch) -> None:
    """The bound arrives WITH the launch path, not after it. Sequencing is the
    whole point: between a launch that works and a bound that
    does not exist yet, a bug bills real money."""
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(OTHER_ARN, tag="kc-zzzzzz")]
    _patch_aws(monkeypatch, double)
    arn = FargateLaunchEngine(_spec()).provision(
        tag=TAG, size_key="1024/2048", profile="p", region="us-west-2"
    )
    assert arn == ARN
    assert double.stops == [OTHER_ARN], "the leftover must be stopped by the launch that follows it"
    assert double.run_requests, "the launch itself must still happen"


def test_provision_refuses_when_the_running_cap_is_reached(monkeypatch) -> None:
    """The cap is a REFUSAL, never a stop. Which of several running tasks is the
    leak is not knowable from a read, so stopping one on that guess is the error
    the ownership rule exists to avoid: a refusal costs a launch, a wrong stop
    costs a running crew."""
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(OTHER_ARN, started_at=None)]
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec(), bounds=TaskBounds(max_running=1))
    with pytest.raises(RuntimeError, match="cap of 1"):
        engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")
    assert double.run_requests == [], "a refused launch must not reach RunTask"
    assert double.stops == [], "the cap must not stop anything"


def test_the_cap_is_measured_after_the_sweep(monkeypatch) -> None:
    """A task that should already be gone must not fill the cap. With a cap of one
    and one EXPIRED task present, the sweep stops it and the launch proceeds."""
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(OTHER_ARN)]
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec(), bounds=TaskBounds(max_running=1))
    arn = engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")
    assert arn == ARN
    assert double.stops == [OTHER_ARN]


def test_a_free_refusal_still_costs_no_aws_call(monkeypatch) -> None:
    """The sweep sits after every refusal that needs no AWS call, so a launch that
    was going to be refused for its tag or its size is still free."""
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec())
    with pytest.raises(ValueError):
        engine.provision(tag="not a tag", size_key="1024/2048", profile="p", region="us-west-2")
    with pytest.raises(ValueError):
        engine.provision(tag=TAG, size_key="nonsense", profile="p", region="us-west-2")
    assert double.list_calls == []


def test_an_engine_given_no_bounds_is_still_bounded() -> None:
    """A caller who supplies nothing must still get a bounded task. An optional
    bound that defaulted to absent would be the unbounded launch this engine is
    not allowed to make."""
    assert FargateLaunchEngine(_spec())._bounds == TaskBounds()


def test_a_task_that_never_started_is_aged_from_when_it_was_created(monkeypatch) -> None:
    """ECS reports no ``startedAt`` until a task starts, so a task stuck in
    PROVISIONING would be ageless forever and never bounded. ``createdAt`` is the
    fallback, which is why such a task still has an age."""
    double = _EcsDouble()
    task = _aged_task(started_at=None)
    task["createdAt"] = "2020-01-01T00:00:00Z"
    double.tasks_for_teardown = [task]
    _patch_aws(monkeypatch, double)
    assert FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2").stop == (ARN,)


def test_the_first_moment_present_wins_and_epoch_zero_is_a_moment() -> None:
    """The fallback is an explicit None check, not an ``or`` chain.

    ``0.0`` is a moment and a falsy one, so an ``or`` would discard a startedAt of
    exactly the Unix epoch and read createdAt in its place -- a different task's
    age. Only when NOTHING carries a moment is the answer None.
    """
    assert fargate_engine._first_moment("1970-01-01T00:00:00Z", "2020-01-01T00:00:00Z") == 0.0
    assert fargate_engine._first_moment(None, "2020-01-01T00:00:00Z") is not None
    assert fargate_engine._first_moment(None, "") is None


# ── Mutation anchors for the bounds ──────────────────────────────────────────


def test_mutation_the_sweep_is_reached_from_the_launch_path(monkeypatch) -> None:
    """Removing the ``reap`` call from ``provision`` must turn a test red.

    Without it the sweep is another pure producer with no caller, which is the
    state a planner must never ship in. The expired task below is
    stopped only because a launch drove the sweep.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(OTHER_ARN, tag="kc-zzzzzz")]
    _patch_aws(monkeypatch, double)
    FargateLaunchEngine(_spec()).provision(
        tag=TAG, size_key="1024/2048", profile="p", region="us-west-2"
    )
    assert double.stops == [OTHER_ARN]


def test_mutation_the_age_comparison_is_against_the_bound(monkeypatch) -> None:
    """Widening the lifetime comparison so a fresh task is stopped must turn a test
    red, and narrowing it so an expired one survives must too. Both directions are
    asserted on the same shape, one second either side of the bound."""
    assert _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS + 1)]).stop == (ARN,)
    assert _sweep([_bounded(age=DEFAULT_TASK_TTL_SECONDS - 1)]).stop == ()


def test_mutation_the_stamp_must_still_agree_with_the_tag(monkeypatch) -> None:
    """Dropping the ``startedBy`` equality from the sweep must turn a test red.

    What it protects is narrower than it looks: a managed, tagged task whose stamp
    is not the one its own tag derives, which no launch of this product produces.
    Attribution to a crew is the separate rule below, and this test deliberately
    does not stand in for it -- that conflation is what left the crew case open.
    """
    foreign = _aged_task()
    foreign["startedBy"] = "kirocrew-cloud-kc-someoneelse"
    double = _EcsDouble()
    double.tasks_for_teardown = [foreign]
    _patch_aws(monkeypatch, double)
    assert FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2").stop == ()
    assert double.stops == []


def test_mutation_the_crew_tag_decides_what_the_sweep_may_stop(monkeypatch) -> None:
    """Dropping the crew equality must turn this red, through the real stop channel.

    The read is cluster-wide and unfiltered by ``startedBy``, so it returns a
    colleague's crew whole: managed, launch-tagged, and stamped in agreement with
    its own tag. Every other check passes on it. If the crew tag stops deciding,
    ``ecs:StopTask`` is called on a colleague's running crew, and there is no
    undoing that.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(crew=_other_crew())]
    _patch_aws(monkeypatch, double)
    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert plan.stop == ()
    assert plan.running == ()
    assert double.stops == []


def test_reap_matches_on_the_crew_the_spec_itself_binds_to(monkeypatch) -> None:
    """The name the sweep matches on is derived from the spec, not supplied beside it.

    Same read, two tasks differing only in their crew tag: the spec's own crew is
    stopped and the other is not. That pins the derivation as well as the check --
    a sweep matching on some other crew name would stop neither.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [
        _aged_task(),
        _aged_task(OTHER_ARN, crew=_other_crew()),
    ]
    _patch_aws(monkeypatch, double)
    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")
    assert plan.stop == (ARN,)
    assert double.stops == [ARN]


def test_the_cap_does_not_count_a_colleagues_tasks(monkeypatch) -> None:
    """A cluster full of a colleague's live tasks must not refuse this crew's launch.

    The cap exists to catch this crew leaking tasks. Measured over the cluster
    instead, a busy shared cluster would refuse every launch here while nothing of
    this crew's was running at all.
    """
    double = _EcsDouble()
    double.tasks_for_teardown = [
        _aged_task(f"{ARN}{index}", tag=f"kc-other{index}", started_at=None, crew=_other_crew())
        for index in range(DEFAULT_MAX_RUNNING_TASKS + 2)
    ]
    _patch_aws(monkeypatch, double)
    arn = FargateLaunchEngine(_spec()).provision(
        tag=TAG, size_key="1024/2048", profile="p", region="us-west-2"
    )
    assert arn
    assert double.stops == []


def test_no_idle_bound_is_claimed_anywhere(monkeypatch) -> None:
    """The bound set is a TTL and a cap, and it says so.

    An idle-stop needs a last-activity signal, and no read available to a stopper
    reports one: DescribeTasks carries lifecycle timestamps and no use, and
    reaching into a task is deferred by RFC section 6. This pins the ABSENCE so a
    later reader does not take a bound that is not here for one that is: if an
    idle field appears on TaskBounds, it must arrive with a signal and with this
    test rewritten.
    """
    assert {f.name for f in dataclasses.fields(TaskBounds)} == {"ttl_seconds", "max_running"}

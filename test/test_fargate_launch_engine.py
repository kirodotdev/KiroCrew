"""The Fargate engine conforms to the launch Protocol and owns only what it marked.

Two things are pinned here. The engine satisfies ``LaunchEngine`` structurally and
is injectable where the EC2 one is, so PR 3's seam needs no change. And teardown's
ownership rule refuses exactly the cases that must be refused -- including a
launch tag written into the managed marker, which is the shape that would
otherwise orphan a task from teardown.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import threading
import typing
from typing import Callable

import pytest

from kiro_crew.cloud import fargate, fargate_engine
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

    ``SigninHandle`` declares four ATTRIBUTES (``already_logged_in``, ``url``,
    ``code``, ``ports``) beside its two methods, and ``run_launch`` reads
    ``handle.already_logged_in`` unconditionally before anything else. A check
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
    assert attributes == {"already_logged_in", "url", "code", "ports"}
    assert methods == {"wait", "close"}
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

    def begin_signin(self, *, instance_id: str, profile: str, region: str):
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


# ── The AWS-touching methods refuse with a reason, not silently ──────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda e: e.preflight("p", "us-west-2"),
        lambda e: e.provision(tag=TAG, size_key="balanced", profile="p", region="us-west-2"),
        lambda e: e.teardown(tag=TAG, profile="p", region="us-west-2"),
    ],
)
def test_unwritten_methods_raise_with_a_stated_cause(call) -> None:
    """A placeholder must say why it is one, so it cannot be mistaken for done."""
    with pytest.raises(NotImplementedError) as excinfo:
        call(FargateLaunchEngine())
    assert "signatures are final" in str(excinfo.value)

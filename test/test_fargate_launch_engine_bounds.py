"""What bounds a launched Fargate task: the lifetime sweep and the running cap.

``plan_bounds_sweep`` decides which of a crew's tasks a sweep stops and which count against
the cap; the bound executor runs it against the scripted ``_EcsDouble``, through ``reap``
and through the sweep ``provision`` runs first. Mutation anchors for the bounds close the
module. Shared constants and builders come from ``fargate_launch_engine_helpers.py``.
"""

from __future__ import annotations

import dataclasses

import pytest
from fargate_launch_engine_helpers import (
    ARN,
    EXPECTED_TARGET,
    LAUNCH_TAG_KEY,
    OTHER_ARN,
    REGISTRABLE_ARN,
    TAG,
    _EcsDouble,
    _patch_aws,
    _spec,
    _spec_taskdef,
)

from kiro_crew.cloud import fargate_engine
from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate import CREW_TAG_KEY, INTERNAL_ONLY_ENV, TASK_TTL_ENV, spec_binding
from kiro_crew.cloud.fargate_engine import (
    DEFAULT_MAX_RUNNING_TASKS,
    DEFAULT_TASK_TTL_SECONDS,
    MANAGED_TAG_VALUE,
    FargateLaunchEngine,
    TaskBounds,
    TaskSighting,
    plan_bounds_sweep,
)

# ── What bounds a launched task ──────────────────────────────────────────────
#
# The executor for plan_teardown is tested in ``test_fargate_launch_engine_aws.py``.
# These cover the other half of what bounds a task: a task nobody tears down by
# hand still ends, and a launcher cannot accumulate tasks without limit. Every case
# here is keyed on the tag a read reports rather than on an ARN a test captured, so
# it covers the property and not one instance.

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


def test_reap_removes_the_expired_tasks_registry_row(monkeypatch, tmp_path) -> None:
    """This sweep is the path that reaches a REGISTERED task in ordinary operation:
    it runs on every provision, so a crew that outlives the default TTL is stopped
    by the next launch. Leaving its row puts a dead crew in the list, and the TTL
    sweep is not a teardown, so nothing else comes along to prune it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.instances.registry import InstancesRegistry

    reg = InstancesRegistry(tmp_path / "instances.json")
    reg.add(
        name="Expired",
        ssm_target=EXPECTED_TARGET,
        connection_method="fargate",
        instance_id="expired",
    )
    double = _EcsDouble()
    double.tasks_for_teardown = [_aged_task(REGISTRABLE_ARN)]
    _patch_aws(monkeypatch, double)

    plan = FargateLaunchEngine(_spec()).reap(profile="p", region="us-west-2")

    assert plan.stop == (REGISTRABLE_ARN,)
    assert double.stops == [REGISTRABLE_ARN]
    assert [i.id for i in InstancesRegistry(tmp_path / "instances.json").list()] == []


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


def test_the_launch_carries_the_same_lifetime_the_sweep_enforces(monkeypatch) -> None:
    """The bound reaches the task, not only the sweep that runs at launch time.

    This is the half the sweep cannot cover: a cluster whose last launch has
    already happened is never swept again, so a bound that exists only here stops
    nothing. Asserted against the request this engine actually sends, and against
    the engine's OWN number, so a launch that quietly sent a different lifetime
    than the one it enforces would fail.
    """
    double = _EcsDouble()
    _patch_aws(monkeypatch, double)
    engine = FargateLaunchEngine(_spec(), bounds=TaskBounds(ttl_seconds=1234))

    engine.provision(tag=TAG, size_key="1024/2048", profile="p", region="us-west-2")

    sent = double.run_requests[0]["overrides"]["containerOverrides"][0]["environment"]
    assert {e["name"]: e["value"] for e in sent}[TASK_TTL_ENV] == "1234"


def test_the_launch_carries_the_lanes_internal_only_claim(monkeypatch) -> None:
    """The claim must reach the request this engine sends, and only when the lane made it.

    This is the last link in the chain: operator file, spec, request, container. Every
    other link is asserted elsewhere, and none of them proves the engine puts the value
    in the document -- a spec holding the claim with the forwarding deleted would pass
    all of them and launch a task that refuses to start.

    Both directions in one test, because the interesting failure is a value hardcoded
    rather than read: that hands the loosened posture to a lane whose operator never
    claimed the boundary, and a one-direction test cannot see it.
    """
    for claimed, expected in ((True, "1"), (False, "0")):
        double = _EcsDouble()
        _patch_aws(monkeypatch, double)
        spec = dataclasses.replace(_spec(), internal_only=claimed)

        FargateLaunchEngine(spec).provision(
            tag=TAG, size_key="1024/2048", profile="p", region="us-west-2"
        )

        sent = double.run_requests[0]["overrides"]["containerOverrides"][0]["environment"]
        emitted = {e["name"]: e["value"] for e in sent}
        assert emitted[INTERNAL_ONLY_ENV] == expected, f"internal_only={claimed}"


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

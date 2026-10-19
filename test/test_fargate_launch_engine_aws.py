"""The Fargate engine's AWS-touching methods, driven against a scripted chokepoint.

``_patch_aws`` installs ``_EcsDouble`` over ``cloud/aws.py``'s ``checked`` /
``checked_json``, so provision and teardown run end to end without a credential. The module
also holds the size-key cases, the comparison showing that nothing above ``LaunchEngine``
branches on backend, two mutation anchors and ``TestTheCredentialRecipientIsConfirmed``.
Shared constants and builders come from ``fargate_launch_engine_helpers.py``.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
from fargate_launch_engine_helpers import (
    _IMAGE,
    ARN,
    EXPECTED_TARGET,
    LAUNCH_TAG_KEY,
    OTHER_ARN,
    REGISTRABLE_ARN,
    RUNTIME_ID,
    TAG,
    _EcsDouble,
    _patch_aws,
    _secret,
    _spec,
)

from kiro_crew.cloud import fargate_engine
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud import sizes
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate import credential_recipient
from kiro_crew.cloud.fargate_engine import (
    MANAGED_TAG_VALUE,
    FargateLaunchEngine,
    FargateSigninHandle,
)

# ── The AWS-touching methods, driven against a monkeypatched chokepoint ──────
#
# Every AWS call goes through cloud/aws.py's checked / checked_json. These tests
# replace that one chokepoint with a scripted double, so provision and teardown
# are exercised end to end without a credential and without boto3 — the same
# testability the fargate module's own tests rely on.


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


def _our_registrable_task(status: str = "RUNNING") -> dict:
    """``_our_task`` on the ARN whose task id the REGISTRY can address.

    The shared ``ARN`` fixture's id is sixteen hex characters, so no ECS target
    built from it survives ``split_ecs_target`` and no registry row can name it.
    """
    return {
        "taskArn": REGISTRABLE_ARN,
        "startedBy": fargate_engine._started_by_for(TAG),
        "lastStatus": status,
        "tags": [
            {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
            {"key": LAUNCH_TAG_KEY, "value": TAG},
        ],
    }


def test_teardown_removes_the_stopped_tasks_registry_row(monkeypatch, tmp_path) -> None:
    """``register`` is this lane's last launch step, so a cancel observed just
    after it unwinds through teardown with the row already written. Stopping the
    task and leaving the row offers a crew whose target resolves to nothing, and
    nothing else removes it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.instances.registry import InstancesRegistry

    reg = InstancesRegistry(tmp_path / "instances.json")
    reg.add(
        name="Kiro Crew Cloud (kc-a1b2c3)",
        ssm_target=EXPECTED_TARGET,
        connection_method="fargate",
        instance_id="doomed",
    )
    double = _EcsDouble()
    double.tasks_for_teardown = [_our_registrable_task()]
    _patch_aws(monkeypatch, double)

    assert FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2") is True

    assert double.stops == [REGISTRABLE_ARN]
    assert [i.id for i in InstancesRegistry(tmp_path / "instances.json").list()] == []


def test_teardown_leaves_another_tasks_row_alone(monkeypatch, tmp_path) -> None:
    """The removal is keyed on the task it stopped, so a sibling crew in the same
    cluster keeps its record."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.instances.registry import InstancesRegistry

    other_task_id = "f" * 32
    reg = InstancesRegistry(tmp_path / "instances.json")
    reg.add(
        name="Doomed",
        ssm_target=EXPECTED_TARGET,
        connection_method="fargate",
        instance_id="doomed",
    )
    reg.add(
        name="Sibling",
        ssm_target=f"ecs:crews_{other_task_id}_{RUNTIME_ID}",
        connection_method="fargate",
        instance_id="sibling",
    )
    double = _EcsDouble()
    double.tasks_for_teardown = [_our_registrable_task()]
    _patch_aws(monkeypatch, double)

    FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2")

    assert [i.id for i in InstancesRegistry(tmp_path / "instances.json").list()] == ["sibling"]


def test_teardown_does_not_confirm_when_the_row_could_not_be_removed(monkeypatch, tmp_path) -> None:
    """A confirmation says nothing of ours may remain, and a listed crew addressing
    a stopped task is something of ours. The task did stop, so returning True would
    tell the operator the cleanup is finished while a row they cannot see the cause
    of is still there."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.cloud import connect as connect_mod

    monkeypatch.setattr(
        connect_mod, "unregister_ecs_task", lambda *_a, **_k: connect_mod.UNREGISTER_FAILED
    )
    double = _EcsDouble()
    double.tasks_for_teardown = [_our_registrable_task()]
    _patch_aws(monkeypatch, double)

    confirmed = FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2")

    # The stop still happened -- this is a narrower miss than an unstopped task.
    assert double.stops == [REGISTRABLE_ARN]
    assert confirmed is False


def test_teardown_confirms_when_there_was_no_row_to_remove(monkeypatch, tmp_path) -> None:
    """An absent row is not a failure: a task registered by nothing, or already
    removed by hand, leaves nothing behind, so the confirmation stands."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    double = _EcsDouble()
    double.tasks_for_teardown = [_our_registrable_task()]
    _patch_aws(monkeypatch, double)

    assert FargateLaunchEngine(_spec()).teardown(tag=TAG, profile="p", region="us-west-2") is True
    assert double.stops == [REGISTRABLE_ARN]


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

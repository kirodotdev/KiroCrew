"""Constants and builders shared by the three ``test_fargate_launch_engine*.py`` modules.

Each imports the launch tag, the task ARNs and the ``_spec`` launch-spec factory; the AWS
and bounds modules also drive ``_EcsDouble``, the scripted stand-in for ``cloud/aws.py``'s
``checked`` / ``checked_json`` chokepoint that ``_patch_aws`` installs.
"""

from __future__ import annotations

import json

from kiro_crew.cloud import fargate_engine
from kiro_crew.cloud.fargate import (
    Placement,
    SecretRef,
    TaskDefinitionSpec,
    credential_recipient,
    default_log_spec,
    revision_fingerprint,
)
from kiro_crew.cloud.fargate_engine import FargateLaunchSpec

TAG = "kc-a1b2c3"
ARN = "arn:aws:ecs:us-west-2:111122223333:task/crews/1111111111111111"
OTHER_ARN = "arn:aws:ecs:us-west-2:111122223333:task/crews/2222222222222222"


#: The tag key carrying launch identity, as the CALLER supplies it. The engine
#: module refuses to spell this key itself because the module that writes the tag
#: owns it, so every test hands it over the same way production code must.
LAUNCH_TAG_KEY = "kirocrew:launch"

#: A task id and ARN the REGISTRY can address, which ``ARN`` above deliberately is
#: not: an ECS target's task id is 32 hex characters and the shared fixture's is
#: sixteen, so a target built from it is refused by ``split_ecs_target``. Kept
#: separate rather than widening ``ARN``, because that short id is what
#: :meth:`~TestAwaitingTheRegistrationTarget.test_coordinates_that_do_not_form_a_target_are_refused_here`
#: uses to show the refusal, and the two facts would otherwise cancel out.
TASK_ID = "0123456789abcdef0123456789abcdef"
RUNTIME_ID = f"{'a' * 32}-1234567890"
REGISTRABLE_ARN = f"arn:aws:ecs:us-west-2:111122223333:task/crews/{TASK_ID}"
EXPECTED_TARGET = f"ecs:crews_{TASK_ID}_{RUNTIME_ID}"


def _secret() -> SecretRef:
    return SecretRef(
        name="kirocrew/crew/demo/KIRO_IDENTITY",
        arn="arn:aws:secretsmanager:us-west-2:111122223333:secret:kirocrew/crew/demo/KIRO_IDENTITY-AbCdEf",
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
        # Mirrors what the engine's own _taskdef_spec states, so a fingerprint
        # computed here equals the one the engine computes for the same launch.
        store=None,
    )


def _patch_aws(monkeypatch, double: _EcsDouble) -> None:
    from kiro_crew.cloud import aws as aws_mod

    monkeypatch.setattr(aws_mod, "checked_json", double.checked_json)
    monkeypatch.setattr(aws_mod, "checked", double.checked)

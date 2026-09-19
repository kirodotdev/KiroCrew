"""The ``RunTask`` request for a crew's Fargate task, as data.

The request is where the credential decision can be undone, so this is where it
is refused. ``ContainerOverride`` has no ``secrets`` field, so a credential put
into a ``RunTask`` request is plain text in ``environment``: written to the
CloudTrail record of the request and readable out of ``DescribeTasks``. The
definition already delivers it through ``secrets``, so an ``environment``
override naming one of those variables is either shadowing a secret with a
plaintext copy or fighting it. Both are refused.

The task-level overrides ``RunTask`` accepts include ``executionRoleArn`` and
``taskRoleArn``. Neither is ever emitted. The definition's execution role is the
only role permitted to read the secrets that definition names, and overriding it
would reopen exactly the hole the definition's agreement check closes: the
document would name one crew and the request would run it under another crew's
fetcher. The allowlist is enforced against the produced override rather than
trusted to the code above it.

The same rule governs which definition runs. The family is derived from the spec
inside :func:`run_task_request` and the caller supplies only a revision number,
so a request cannot name a family other than the one every refusal here reads.
A parameter that could name it is a parameter the refusals do not cover, however
carefully they are written.

**Absent is not a value.** For every field of every type here, empty, zero and
unparseable are refused rather than carrying a meaning. The rule is written down
because the opposite kept happening one field at a time: a missing size let the
registration floor become the runtime shape, an open ``environment`` let a caller
contradict the spec, an unparseable ARN was readable to one parser and refused by
the other, and an empty ``security_groups`` made ECS substitute the VPC's default
group, which admits traffic from anything else in it. Every one of those produced
a request that looked correct. A field added below is on one side of this rule or
the other, and saying which is part of adding it.

Two defaults are deliberate exceptions, recorded so they are not later mistaken
for oversights and "fixed" by deleting the distinction:

* ``Placement.assign_public_ip = False`` is legitimate because false is the safe
  direction. The flag is not the boundary: a Fargate task in a public subnet with
  no NAT gateway cannot pull its image without an address, so what bounds who
  reaches the container is the security group. That is why an empty security group
  is refused and this default is not.
* A ``TaskSize`` equal to the registration floor is legitimate because requiring
  the field already removed the silence. The floor arriving because nobody chose
  was the defect; the floor's value was never wrong. Refusing it would be this
  module holding a policy about how large a crew must be, which it has no basis
  for.

``Placement.cluster`` is caller-owned and stays that way, and the two questions
about it have different answers. It is refused when empty, because an empty
cluster means the account's implicit ``default`` cluster, which is absent being
read as a value. It is NOT in the closed set, because the two-limb membership test
does not reach it: a closed name is one whose caller-supplied value could
contradict what the request already asserts, and a task-definition spec says
nothing about a cluster, so no cluster can contradict it. Ownership and
emptiness are independent, and the cluster is the field that shows it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Collection, Mapping

from kiro_crew.cloud.fargate.identity import CrewBinding, DocumentRefused, task_family
from kiro_crew.cloud.fargate.taskdef import (
    CREDENTIAL_ENV,
    CREW_CONTAINER_NAME,
    CREW_TAG_KEY,
    MANAGED_TAG_VALUE,
    TaskDefinitionSpec,
    secret_destinations,
    spec_binding,
)
from kiro_crew.cloud.iam import MANAGED_TAG_KEY

#: The caller's own correlation value, in a key of its own. This mirrors
#: ``cloud/ec2.py``, which tags ``kirocrew:managed=true`` beside
#: ``kirocrew:instance=<tag>``: the marker says who owns the resource and the
#: second tag says which launch it belongs to. One key cannot answer both, because
#: the first answer has to be constant for teardown to match it and the second has
#: to vary for a caller to find their own task.
LAUNCH_TAG_KEY = "kirocrew:launch"

#: Task-level override keys this request may carry. ``executionRoleArn`` and
#: ``taskRoleArn`` are absent by decision, not by omission.
TASK_OVERRIDE_KEYS = frozenset({"cpu", "memory", "ephemeralStorage", "containerOverrides"})

#: Container-level override keys this request may carry. ``command`` is absent by
#: decision. Replacing the image's command replaces the supervisor, and the
#: secrets are injected and the task role attached before any command runs, so an
#: override would run arbitrary code holding the model credential with none of
#: the supervisor's sandbox verification or environment scrubbing. The narrower
#: alternative, pinning the override to the supervisor's own command, would copy
#: the image's entrypoint into this module and give two places to keep in
#: agreement. Not offering the parameter needs no agreement.
CONTAINER_OVERRIDE_KEYS = frozenset({"name", "environment"})

#: Container variables whose value this module DERIVES and writes itself. A
#: caller cannot supply them, so a request cannot contradict the spec it was
#: built from. ``SMC_CREW_NAME`` comes from the crew the spec's secrets name, and
#: pairing it with the wrong image then fails the container's own
#: ``manifest crew_name == SMC_CREW_NAME`` check by naming both crews.
#: ``SMC_SINGLE_PRINCIPAL`` is forced true because this backend exists for one
#: owner; leaving it to the caller made a task that exits at startup
#: constructible, which is not a posture worth offering.
DERIVED_ENV: frozenset[str] = frozenset({"SMC_CREW_NAME", "SMC_SINGLE_PRINCIPAL"})

#: Container variables this module neither writes nor accepts. Each is either a
#: credential (so it belongs in the task definition's ``secrets`` or nowhere), or
#: a value that would defeat something the definition already asserts:
#: ``SMC_BUNDLE_DIR`` redirects which bundle the crew-name check reads, and
#: ``SMC_FRONT_PORT`` moves the listener away from the declared ``portMappings``.
REFUSED_ENV: frozenset[str] = frozenset({"SMC_BUNDLE_DIR", "SMC_FRONT_PORT"} | set(CREDENTIAL_ENV))

#: The closed set a caller may not name. Everything outside it is the caller's:
#: a bucket, a route prefix, a log level, whatever the deploy track passes.
#:
#: A name belongs here when a caller-supplied value could CONTRADICT what this
#: request already asserts. Two limbs, and every member sits on one of them:
#: it decides WHAT THE TASK IS, which the spec's secrets already fix through the
#: crew they name, or it decides WHO MAY REACH IT, which is the credential set
#: and the trust-domain declaration. A bucket cannot contradict the spec because
#: the spec says nothing about buckets, which is why it stays the caller's.
#:
#: A test derives the container's full set of read names from its own config
#: module and fails when one is neither derived, refused, nor deliberately left
#: to the caller. That is what stops the next variable added there from arriving
#: as an open channel before anyone reviews it.
CLOSED_ENV: frozenset[str] = DERIVED_ENV | REFUSED_ENV

#: ``startedBy`` is capped by the API. Exceeded, the whole call fails, so the
#: cap is checked here where the message can say which field was too long.
STARTED_BY_MAX = 36

#: The charset a correlation value uses, for ``startedBy`` and the launch tag. The
#: API rejects anything else at launch, and a launch-time refusal is the deferred
#: failure this module converts into a generation-time one.
_TAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Placement:
    """Where the task runs. ``awsvpc`` is the only network mode Fargate has."""

    cluster: str
    subnets: tuple[str, ...]
    security_groups: tuple[str, ...]
    assign_public_ip: bool = False


#: Fargate's valid memory range per CPU size, as (minimum, maximum, step) in MiB.
#: Encoded because an invalid pair is refused by ``RunTask`` at launch, and a
#: launch-time refusal is the deferred failure this module exists to convert into
#: a generation-time one. "AWS will reject it for us" is the reasoning that let
#: four earlier caller-supplied values through.
FARGATE_MEMORY_FOR_CPU: dict[str, tuple[int, int, int]] = {
    "256": (512, 2048, 512),
    "512": (1024, 4096, 1024),
    "1024": (2048, 8192, 1024),
    "2048": (4096, 16384, 1024),
    "4096": (8192, 30720, 1024),
    "8192": (16384, 61440, 4096),
    "16384": (32768, 122880, 8192),
}

#: Fargate's ephemeral-storage bounds in GiB. 20 or less is not a smaller disk,
#: it is a request ``RunTask`` rejects.
EPHEMERAL_STORAGE_MIN_GIB = 21
EPHEMERAL_STORAGE_MAX_GIB = 200


@dataclass(frozen=True)
class TaskSize:
    """The size a task actually runs at.

    Required at every launch. The definition carries a registration floor only
    because ``RegisterTaskDefinition`` demands a value for a Fargate task, and a
    launch that supplies no size is refused rather than silently inheriting it.

    A size equal to that floor is ACCEPTED, and the distinction is the point:
    requiring the field is what removed the silence, so ``TaskSize("256", "512")``
    can now only be a caller stating a small size deliberately and visibly. What
    was wrong was the floor arriving because nobody chose, not the floor's value.
    Refusing a legal Fargate size here would be this module inventing a policy
    about how large a crew must be, which it has no basis to hold.
    """

    cpu: str
    memory: str
    ephemeral_storage_gib: int | None = None


def _refuse_unusable_size(size: TaskSize) -> None:
    for field, value in (("cpu", size.cpu), ("memory", size.memory)):
        if not value.isdigit() or int(value) <= 0:
            raise DocumentRefused(
                f"size {field}={value!r} is not a positive integer of Fargate units. The task "
                "definition's registration floor exists only to satisfy the API and is never "
                "the shape a task should run at, so a launch states its own size"
            )
    allowed = FARGATE_MEMORY_FOR_CPU.get(size.cpu)
    if allowed is None:
        raise DocumentRefused(
            f"size cpu={size.cpu!r} is not a Fargate CPU size; "
            f"choose one of {', '.join(sorted(FARGATE_MEMORY_FOR_CPU, key=int))}"
        )
    low, high, step = allowed
    memory = int(size.memory)
    if memory < low or memory > high or (memory - low) % step:
        raise DocumentRefused(
            f"size memory={size.memory!r} is not a Fargate memory value for cpu={size.cpu!r}, "
            f"which takes {low} to {high} MiB in steps of {step}. RunTask refuses this pair at "
            "launch, where the reason is harder to read than it is here"
        )
    if size.ephemeral_storage_gib is not None and not (
        EPHEMERAL_STORAGE_MIN_GIB <= size.ephemeral_storage_gib <= EPHEMERAL_STORAGE_MAX_GIB
    ):
        raise DocumentRefused(
            f"ephemeral storage {size.ephemeral_storage_gib} GiB is outside Fargate's "
            f"{EPHEMERAL_STORAGE_MIN_GIB} to {EPHEMERAL_STORAGE_MAX_GIB} GiB range"
        )


def derived_environment(binding: CrewBinding) -> dict[str, str]:
    """The identity and trust-domain variables this module writes, not accepts.

    ``SMC_CREW_NAME`` is the crew the spec's secrets name. Writing it here is what
    makes the container's own ``manifest crew_name == SMC_CREW_NAME`` refusal do
    useful work: pairing one crew's secrets with another crew's image now fails
    inside the container with a message naming both, where before both values came
    from the caller and could agree with each other while contradicting the spec.

    ``SMC_SINGLE_PRINCIPAL`` is forced true. This backend exists for exactly one
    owner, so a request that left the declaration to the caller could only produce
    a task that refuses to start. Enforcement belongs to the front app's startup
    check, which is where the reasoning about caller identity lives; writing the
    value here makes the unset case unconstructible rather than merely unlikely.
    """
    return {"SMC_CREW_NAME": binding.crew, "SMC_SINGLE_PRINCIPAL": "1"}


def _refuse_closed_environment(environment: Mapping[str, str], delivered: Collection[str]) -> None:
    """Refuse a caller name inside the closed set, then the shadowing case.

    The first refusal reads no part of the spec. Four rounds of review on this
    module found the same defect four times, once per variable: ``environment``
    was an open channel through which a caller could contradict the spec, and
    each fix closed one name. A guarantee that holds only for the names someone
    has already thought of is not a guarantee. So the channel is closed by set
    membership and the module writes the derived values itself, which covers the
    next name added to the container before anyone reviews it.

    The second refusal is the general shadowing case: a variable the definition
    delivers from Secrets Manager, fought over by a plaintext override. That one
    is genuinely about the spec's contents, so an intersection is its right shape.
    """
    closed = sorted(set(environment) & CLOSED_ENV)
    if closed:
        raise DocumentRefused(
            f"environment override names {', '.join(closed)}, which this request derives or "
            "refuses rather than accepts. Those names decide which crew the task serves and "
            "who may reach it, and the spec already fixes both, so a caller-supplied value "
            "could only contradict it. Everything outside that set stays the caller's"
        )
    shadowed = sorted(set(environment) & set(delivered))
    if shadowed:
        raise DocumentRefused(
            f"environment override names {', '.join(shadowed)}, which the task definition "
            "delivers from Secrets Manager. One variable takes one value, so an override "
            "here and a secret there have no defined winner"
        )


def _refuse_empty_placement(placement: Placement) -> None:
    """Every field of a placement that has no meaningful empty value.

    A missing security group is not a smaller boundary, it is a different one:
    ECS applies the VPC's DEFAULT security group, which admits traffic from
    anything else in that group. The task's front process answers two routes
    without the control secret, a turn and a liveness check, so a workload
    sharing the default group could take a turn on this crew using this crew's
    model credential. There is no launch that means to ask for that.

    ``assign_public_ip`` has no equivalent refusal on purpose. A Fargate task in
    a public subnet with no NAT gateway cannot pull its image without one, so the
    flag answers a real deployment shape rather than publishing the task: what
    bounds who can reach port 8080 is the security group, which is why an empty
    one is refused here rather than compensated for later.
    """
    if not placement.cluster:
        raise DocumentRefused("no cluster is named")
    if not placement.subnets:
        raise DocumentRefused("no subnet is named; an awsvpc task needs at least one")
    if not placement.security_groups:
        raise DocumentRefused(
            "no security group is named. ECS then applies the VPC default group, which "
            "admits traffic from anything else in it, and the task's turn endpoint answers "
            "without the control secret. Name the group that bounds who may reach it"
        )


def _refuse_keys_outside(payload: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    """Refuse a produced override carrying a key outside its allowlist.

    Reads the output rather than the arguments, so a key added by a future edit
    is refused unless the allowlist is widened in the same change.
    """
    extra = sorted(set(payload) - allowed)
    if extra:
        raise DocumentRefused(
            f"{where} carries {', '.join(extra)}, outside the set a crew launch may override "
            f"({', '.join(sorted(allowed))})"
        )


def run_task_request(
    *,
    revision: int,
    taskdef: TaskDefinitionSpec,
    placement: Placement,
    size: TaskSize,
    launch_tag: str,
    environment: Mapping[str, str] | None = None,
    started_by: str = "",
) -> dict[str, Any]:
    """The ``RunTask`` request body for one crew task, or refuse.

    ``revision`` is a revision NUMBER, not an identifier. The family is derived
    from ``taskdef``, so the definition this request runs and the definition
    every refusal here reads are the same one by construction. A free identifier
    string would let a caller pair one crew's spec with another crew's family:
    the request would run the second and be tagged as the first, and every
    refusal would pass, because each reads the spec rather than the string that
    decides what executes.

    What this does NOT establish, plainly: that revision N of this family holds
    the content ``taskdef`` describes. That needs the registered definition's
    ``kirocrew:revision-key`` tag, which is an AWS call, and this module makes
    none. The caller is the verifier. A launch engine that registered the
    revision itself already knows; one reading a cached number must confirm the
    tag against :func:`revision_fingerprint` through ``DescribeTaskDefinition``
    before calling this, and a mismatch is a stale cache, not a launch.

    ``environment`` is a CLOSED channel. Names in :data:`CLOSED_ENV` are refused;
    the values in :data:`DERIVED_ENV` are computed here from the crew the spec's
    secrets name and written into the override, so a caller cannot contradict the
    spec by declaring a different crew or a weaker trust domain. Everything
    outside that set is passed through untouched.
    """
    if revision < 1:
        raise DocumentRefused(
            f"revision {revision!r} is not a task-definition revision; ECS numbers them from 1"
        )
    _refuse_unusable_size(size)
    _refuse_empty_placement(placement)
    environment = dict(environment or {})
    _refuse_closed_environment(environment, secret_destinations(taskdef))
    if len(started_by) > STARTED_BY_MAX:
        raise DocumentRefused(
            f"startedBy is {len(started_by)} characters; the API accepts at most {STARTED_BY_MAX}"
        )
    if started_by and not _TAG_VALUE_RE.match(started_by):
        raise DocumentRefused(
            f"startedBy={started_by!r} is outside the letters, digits, hyphen and underscore "
            "the API accepts. RunTask rejects it at launch, where the reason is harder to read"
        )
    if not launch_tag:
        raise DocumentRefused(
            f"no launch tag is given. The task would still carry {MANAGED_TAG_KEY}="
            f"{MANAGED_TAG_VALUE} and so remain visible to teardown, but nothing would say "
            "which launch it belongs to"
        )
    if not _TAG_VALUE_RE.match(launch_tag):
        raise DocumentRefused(
            f"launch tag {launch_tag!r} is outside the letters, digits, hyphen and underscore "
            "a correlation value uses"
        )

    binding = spec_binding(taskdef)
    container_override: dict[str, Any] = {"name": CREW_CONTAINER_NAME}
    emitted = dict(environment)
    emitted.update(derived_environment(binding))
    container_override["environment"] = [
        {"name": name, "value": emitted[name]} for name in sorted(emitted)
    ]
    override: dict[str, Any] = {
        "cpu": size.cpu,
        "memory": size.memory,
        "containerOverrides": [container_override],
    }
    if size.ephemeral_storage_gib is not None:
        override["ephemeralStorage"] = {"sizeInGiB": size.ephemeral_storage_gib}

    _refuse_keys_outside(override, TASK_OVERRIDE_KEYS, "overrides")
    _refuse_keys_outside(
        container_override, CONTAINER_OVERRIDE_KEYS, "overrides.containerOverrides[0]"
    )

    request: dict[str, Any] = {
        "cluster": placement.cluster,
        "taskDefinition": f"{task_family(binding)}:{revision}",
        "launchType": "FARGATE",
        "count": 1,
        # The SSM channel the owner reaches this task through. Set here, at the
        # top level, NOT in ``overrides``: it is a RunTask field rather than a
        # task-level override, so TASK_OVERRIDE_KEYS does not govern it and
        # ``executionRoleArn``/``taskRoleArn`` staying absent from the overrides is
        # unaffected.
        #
        # Unconditional, and not offered as a parameter. The flag cannot be turned
        # on for a task that is already running, and this lane publishes no
        # ingress, so a task launched without it is unreachable with no remedy but
        # teardown and relaunch. A knob whose false value can only produce a dead
        # crew is a footgun, not a posture.
        #
        # The cost, stated because it is real: this makes the task PERMANENTLY
        # shell-capable. The Fargate agent bind-mounts the SSM agent in, commands
        # run as root even where the container declares a user, and
        # ``readonlyRootFilesystem`` becomes unusable. IAM is the only thing
        # between a principal and that shell, which is why the caller policy
        # grants ``ecs:ExecuteCommand`` to nobody and carries an explicit Deny on
        # the interactive session documents.
        "enableExecuteCommand": True,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": list(placement.subnets),
                "securityGroups": list(placement.security_groups),
                "assignPublicIp": "ENABLED" if placement.assign_public_ip else "DISABLED",
            }
        },
        "overrides": override,
        "tags": [
            {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
            {"key": LAUNCH_TAG_KEY, "value": launch_tag},
            {"key": CREW_TAG_KEY, "value": binding.crew},
        ],
    }
    if started_by:
        request["startedBy"] = started_by
    return request

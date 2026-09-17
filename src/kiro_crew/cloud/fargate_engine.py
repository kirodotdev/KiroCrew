"""The Fargate launch engine, and the ownership rule its teardown turns on.

Implements the five-method :class:`~kiro_crew.cloud.launch_job.LaunchEngine`
Protocol for Fargate. The AWS-touching bodies call ``cloud/fargate/*`` to build
the ``RegisterTaskDefinition`` and ``RunTask`` request bodies and route every AWS
call through the single ``aws`` CLI chokepoint in :mod:`kiro_crew.cloud.aws`. The
two things that do NOT depend on any AWS call are here too: the ownership rule
teardown finds resources by, and the reason ``begin_signin`` has nothing to do.

**Why ownership is a module of its own rather than a line inside teardown.**
A resource is ours only if it carries the managed marker, and the marker is a
boolean that is separate from the identifier. Those are two different fields doing
two different jobs, and conflating them has a specific failure: a launch tag
written into the marker leaves a task that every convention-following consumer is
blind to, including this one. Writing the rule down as data, with the classifier
as a pure function, is what lets both halves of that property be tested -- a task
whose marker holds anything but ``"true"`` classifies as UNMARKED, and UNMARKED is
never deleted.

**Two keys, two jobs, copied from the EC2 path rather than invented.**
``ec2.py:32-33`` defines ``MANAGED_TAG_KEY = "kirocrew:managed"`` and
``INSTANCE_TAG_KEY = "kirocrew:instance"``, and every consumer treats the first as
a boolean and the second as the identifier: ``ec2.py:804`` compares
``tags.get(MANAGED_TAG_KEY) != "true"``, ``ec2.py:983`` filters
``Key=kirocrew:managed,Values=true``, and the request-tag and resource-tag
conditions in ``cloud/iam.py`` all demand ``== "true"``. So the gate is
``managed == "true"`` and the launch tag lives under its own key. Both halves of
that marker are IMPORTED here, never re-spelled: a second copy of a value whose
meaning is enforced by IAM conditions elsewhere is a copy that can drift out of
agreement with the policy that gates it.

**The launch tag's key is not spelled here either.** The module that writes the
tag onto a task owns that key, so :func:`classify_task` and
:func:`plan_teardown` read it under the imported :data:`LAUNCH_TAG_KEY`. A
private spelling here would be a second copy that nothing checks against the
writer: the moment the two disagree, every task classifies as another launch's,
teardown deletes nothing, and it reports success while the task keeps billing.
Importing the writer's own name is what makes that disagreement impossible rather
than merely unlikely, and it leaves a caller no way to hand in a wrong key.
"""

from __future__ import annotations

import enum
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from kiro_crew.cloud import aws, sizes
from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate import (
    CPU_ARCHITECTURES,
    EPHEMERAL_STORAGE_MAX_GIB,
    EPHEMERAL_STORAGE_MIN_GIB,
    FARGATE_MEMORY_FOR_CPU,
    FINGERPRINT_TAG_KEY,
    LAUNCH_TAG_KEY,
    MANAGED_TAG_VALUE,
    STARTED_BY_MAX,
    Placement,
    SecretRef,
    TaskDefinitionSpec,
    TaskSize,
    default_log_spec,
    revision_fingerprint,
    run_task_request,
    spec_binding,
    task_definition_document,
    task_family,
    validated_region,
)
from kiro_crew.cloud.login_target import KiroLoginTarget

logger = logging.getLogger(__name__)

__all__ = [
    "MANAGED_TAG_VALUE",
    "Ownership",
    "TaskSighting",
    "TeardownPlan",
    "FargateLaunchSpec",
    "classify_task",
    "plan_teardown",
    "FargateSigninHandle",
    "FargateLaunchEngine",
]

#: Delimiter between the fields of a Fargate size_key vocabulary string
#: ("<cpu>/<memory>" with an optional "/<gib>"). A data delimiter in this
#: provisioner own size language, not a filesystem path separator, so
#: it carries no platform meaning and _parse_size splits on it on every OS.
FARGATE_SIZE_SEP = "/"


class Ownership(enum.Enum):
    """What a sighted task is, with respect to this launcher.

    Four outcomes and not two, because "not ours", "cannot tell" and "ours but
    labelled as someone else's" have different correct actions. Collapsing them is
    how a teardown either deletes something it does not own or silently abandons
    something it does.
    """

    #: Marked managed and carrying this launch's tag. The only deletable class.
    OURS = "ours"

    #: Marked managed, carrying a DIFFERENT launch's tag (or none), and started by
    #: someone other than this launcher. Another launch owns it; deleting it would
    #: tear down a running crew that is not this one.
    OTHER_LAUNCH = "other_launch"

    #: Marked managed and started by THIS launcher, yet its launch tag is absent or
    #: names a different launch. Its labels disagree with each other: the
    #: ``startedBy`` says this launcher ran it and the tag says otherwise. Never
    #: deleted -- the tag is what authorises a claim, and it declines -- and never
    #: skipped in silence either, because a task this launcher started bills until
    #: someone stops it.
    MISLABELLED = "mislabelled"

    #: Not marked managed at all, or marked with something other than ``"true"``.
    #: Never deleted. A launch tag written into the marker lands here rather than
    #: in :attr:`OURS`, which is what keeps that mistake from costing a task.
    UNMARKED = "unmarked"


@dataclass(frozen=True)
class TaskSighting:
    """One task as a read returned it.

    Deliberately not an ECS response object. The classifier takes the fields it
    reasons about so it can be tested exhaustively without a client, and so the
    engine cannot accidentally decide ownership from a field that was never read.
    """

    task_arn: str
    tags: Mapping[str, str]
    started_by: str = ""
    last_status: str = ""

    @property
    def is_running(self) -> bool:
        """Whether this task is still consuming money.

        ``STOPPED`` is the only ECS lifecycle state that is not billable, so
        everything else counts as running for the purpose of warning a human.
        """
        return (self.last_status or "").upper() != "STOPPED"


def classify_task(sighting: TaskSighting, *, launch_tag: str, started_by: str) -> Ownership:
    """Decide what *sighting* is with respect to *launch_tag*.

    Launch identity is read under :data:`LAUNCH_TAG_KEY`, imported from the module
    that writes it onto the task, the way :data:`MANAGED_TAG_KEY` is imported from
    the module that owns the marker. This module keeps no spelling of its own: a
    key spelled here that the writer does not use matches no real task, and then
    every task classifies as another launch's and teardown deletes nothing while
    reporting success.

    *started_by* is the ``startedBy`` value the launcher stamps, and the contract
    is that it identifies ONE launch, not the launcher: it must vary with
    *launch_tag*. A launcher-wide value would make two coexisting launches
    classify each other :attr:`Ownership.MISLABELLED`, so every teardown would
    refuse to confirm and warn about billing whenever a sibling launch is up,
    turning the refusal that exists to catch a real stray into noise an operator
    learns to ignore.

    It decides between the two marked-but-not-ours outcomes: a marked task whose
    tag names another launch is :attr:`Ownership.OTHER_LAUNCH` only when someone
    else started it, and :attr:`Ownership.MISLABELLED` when this launch did.
    Without that split the classifier calls a task this launch started "another
    launch's", and teardown skips it in silence while it bills.

    The gate is checked BEFORE the identifier, which is the whole point: a task
    whose ``kirocrew:managed`` is anything other than ``"true"`` is
    :attr:`Ownership.UNMARKED` no matter what else it carries, including the case
    where the launch tag was written into the marker itself. That is not a
    defensive extra -- it is the behaviour that keeps a mislabelled task out of
    the deletable set instead of letting this engine delete it by mistake.
    """
    if not launch_tag:
        raise ValueError("a launch tag is required to classify ownership")
    if sighting.tags.get(MANAGED_TAG_KEY) != MANAGED_TAG_VALUE:
        return Ownership.UNMARKED
    if sighting.tags.get(LAUNCH_TAG_KEY) == launch_tag:
        return Ownership.OURS
    if sighting.started_by == started_by:
        return Ownership.MISLABELLED
    return Ownership.OTHER_LAUNCH


@dataclass(frozen=True)
class TeardownPlan:
    """What teardown should do about a set of sightings, and what it must say.

    ``confirmed`` is the value ``LaunchEngine.teardown`` returns, and it is not
    cosmetic: ``launch_job.py:507-515`` turns ``False`` into a user-visible
    "requested but did NOT confirm -- it may still be running and billing". So
    ``False`` is the honest answer whenever something might be left up, and
    ``True`` must mean nothing of ours remains.

    ``warning`` names the ARNs and says which refusal fired, and its delivery path
    is a decision rather than an omission: the Protocol returns a bool, so the
    engine emits this string through its module logger where ``teardown`` is
    implemented. Logs suffice here and the Protocol is not widened for it, because
    the two channels answer different questions -- the caller's line tells the
    operator to look, and this one says which task to look at and whether it is
    unclaimable or theirs and wrongly tagged, which are different next steps.
    Widening ``LaunchEngine`` would put a Fargate-shaped return on the EC2 engine
    for one message.
    """

    delete: tuple[str, ...]
    confirmed: bool
    warning: str = ""


def plan_teardown(
    sightings: Sequence[TaskSighting],
    *,
    launch_tag: str,
    started_by: str,
) -> TeardownPlan:
    """Decide teardown's actions and its return value from what a read returned.

    Launch identity is read under :data:`LAUNCH_TAG_KEY`, imported from the module
    that writes it (see :func:`classify_task`).
    *started_by* is the ``startedBy`` value the launcher stamps for THIS launch,
    and it is required because the ambiguous case below is defined by it: without
    it there is no way to tell a task this launch plausibly started from a foreign
    one, so the refusal that case exists for could not fire.

    Four cases, and the last two are the ones worth stating plainly.

    Nothing of ours is present and nothing is ambiguous: ``confirmed=True`` with
    an empty delete list. Teardown is idempotent, so a tag whose task is already
    gone is a success and not an error.

    Tasks classified :attr:`Ownership.OURS`: delete exactly those, and confirm.
    :attr:`Ownership.OTHER_LAUNCH` tasks are left alone silently -- another launch
    owns them and will tear them down itself.

    A running :attr:`Ownership.UNMARKED` task whose ``startedBy`` says this
    launcher started it: this is the ambiguous case, and it REFUSES to claim that
    task. It is not deleted, because deleting on a ``startedBy`` match alone is
    deleting by a guess when the marker that authorises deletion is absent. It is
    not ignored either, because a task this launcher plausibly started and cannot
    claim is a task that bills until a human intervenes. So the plan returns
    ``confirmed=False`` and names the ARN -- which surfaces through the existing
    caller as a warning the user can act on. A mislabelled marker produces exactly
    this shape in production, so it is the case with a test.

    A running :attr:`Ownership.MISLABELLED` task: the same refusal through a
    different door. The marker is present and the ``startedBy`` is this launcher's,
    but the launch tag -- the value that authorises this launch to claim a task --
    is absent or names another launch. Deleting on the ``startedBy`` match would
    override the one field whose job is to say whose task it is; skipping it would
    leave a task this launcher started to bill unattended. So it is refused the
    same way, and the warning says WHICH refusal fired, because the operator's
    next step differs: an unmarked task may not be theirs at all, while a
    mislabelled one is theirs and wrongly tagged.

    Both refusals apply only to a running task. ``STOPPED`` is the one ECS state
    that costs nothing, and a warning that tells the operator to stop a task that
    is already stopped is noise that trains them to ignore the real one.

    The refusal is per task, not per teardown. Authority to stop an
    :attr:`Ownership.OURS` task comes from that task's own marker and is not
    withdrawn by an unclaimable neighbour, so when both are present the plan still
    deletes ours, still returns ``confirmed=False`` because something may be left
    billing, and the warning names both halves: what is being stopped and what
    could not be claimed.
    """
    if not launch_tag:
        raise ValueError("a launch tag is required to plan a teardown")
    ours: list[str] = []
    unmarked: list[str] = []
    mislabelled: list[str] = []
    for sighting in sightings:
        kind = classify_task(sighting, launch_tag=launch_tag, started_by=started_by)
        if kind is Ownership.OURS:
            ours.append(sighting.task_arn)
        elif not sighting.is_running:
            continue
        elif kind is Ownership.MISLABELLED:
            mislabelled.append(sighting.task_arn)
        elif kind is Ownership.UNMARKED and sighting.started_by == started_by:
            unmarked.append(sighting.task_arn)
    if not unmarked and not mislabelled:
        return TeardownPlan(delete=tuple(ours), confirmed=True)
    parts: list[str] = []
    if ours:
        parts.append(
            f"Stopping {len(ours)} task(s) marked as {launch_tag} ({', '.join(sorted(ours))})."
        )
    if unmarked:
        parts.append(
            f"{len(unmarked)} running task(s) were started by this launcher "
            f"({', '.join(sorted(unmarked))}) but carry no "
            f"{MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} marker, so {launch_tag} cannot be "
            "torn down completely: without the marker there is nothing authorising a "
            "delete, and deleting on the startedBy match alone would be a guess. Stop "
            "them from the console or the CLI after confirming they are yours -- they "
            "are still billing."
        )
    if mislabelled:
        parts.append(
            f"{len(mislabelled)} running task(s) were started by this launch "
            f"({', '.join(sorted(mislabelled))}) and carry the "
            f"{MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} marker, but their {LAUNCH_TAG_KEY} "
            f"tag does not name {launch_tag}, so {launch_tag} cannot be torn down "
            "completely: the tag is what authorises this launch to claim a task and it "
            "says otherwise, so stopping them on the startedBy match alone would be a "
            f"guess. Check their {LAUNCH_TAG_KEY} tag from the console or the CLI and "
            "stop them once you have confirmed they are yours -- they are still billing."
        )
    return TeardownPlan(delete=tuple(ours), confirmed=False, warning=" ".join(parts))


class FargateSigninHandle:
    """The sign-in step, which for Fargate has nothing to wait for.

    The evidence is in the image rather than in an argument: ``runtime/Dockerfile:130``
    states the credential is "Supplied at run time, never baked in",
    ``supervisor/__main__.py:422`` calls ``require_api_key(env)`` before serving, and a
    search of the whole runtime subtree for device-code, SSO, OAuth or interactive-login
    strings returns zero hits. The container is handed a key and refuses to boot without
    one; nobody ever signs it in.

    So this handle completes immediately rather than polling. It reports success
    because the credential's PRESENCE was already enforced at container start --
    and only presence. ``require_api_key``'s own docstring is explicit that a
    present key is not a working one and that validity "can only be established by
    a real turn", so this handle must not be read as evidence the credential works.

    ``run_launch`` reads ``error`` first and ``already_logged_in`` next, and with an
    empty ``error`` it takes the already-signed-in branch: that branch marks the step
    done without ever showing a prompt, which is exactly the right path for a container
    that is handed its credential at run time. So ``already_logged_in`` is ``True``
    as a statement of fact, and ``url``, ``code`` and ``ports`` are empty because
    there is no prompt to show. Reading the order the other way round would describe a
    handle whose refusal is ignored.
    """

    def __init__(self, task_arn: str) -> None:
        self.task_arn = task_arn
        self.already_logged_in: bool = True
        self.url: str = ""
        self.code: str = ""
        self.ports: list = []
        # The Protocol's VERIFIED-refusal channel. Always empty here: the one
        # identity this engine cannot honour is refused at preflight by
        # ``login_target_refusal``, before anything is provisioned or billed.
        self.error: str = ""

    def wait(self, cancel: threading.Event) -> bool:
        """Return immediately; there is no interactive step to wait for.

        The cancel event is accepted to satisfy the Protocol and is honoured: a
        launch cancelled before this point should not report a completed step.
        """
        return not cancel.is_set()

    def close(self) -> None:
        """Nothing to release. No browser, no device-code poller, no session."""


#: A ``started_by`` value the engine derives from the launch tag, in the charset
#: ``RunTask`` accepts. It correlates a task to the launcher that started it, which
#: is what the ambiguous-teardown branch reads: a running unmarked task whose
#: ``startedBy`` is this prefix plus the tag is the case that refuses rather than
#: guessing. ``kirocrew-cloud-`` plus ``kc-`` plus six hex is 24 characters, inside
#: :data:`STARTED_BY_MAX`.
_STARTED_BY_PREFIX = "kirocrew-cloud-"

#: The charset ``RunTask`` accepts for ``startedBy`` and a tag. The API rejects
#: anything else at launch; refusing it here turns that deferred failure into one
#: the operator reads at the point they can fix it.
_TAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _started_by_for(tag: str) -> str:
    """The ``startedBy`` a task launched under *tag* carries."""
    return f"{_STARTED_BY_PREFIX}{tag}"


@dataclass(frozen=True)
class FargateLaunchSpec:
    """The placement, image and secrets a Fargate launch needs, supplied to the
    engine at construction rather than invented by :meth:`FargateLaunchEngine.provision`.

    ``LaunchEngine.provision`` carries only ``tag``, ``size_key``, ``profile`` and
    ``region``, and ``CloudConfig`` holds only ``profile``, ``region`` and
    ``last_tag`` -- there is no configuration home on this branch for a subnet, a
    security group, an image or a secret ARN. Guessing any of them is the same
    class of error as deleting a task on a guess: an unnamed subnet or security
    group is not a smaller boundary but a different one, and an unpinned image is a
    different workload. So the engine refuses to run without this, and where an
    operator writes it down is deliberately a separate change.
    """

    placement: Placement
    image: str
    secrets: tuple[SecretRef, ...]
    cpu_architecture: str


def _tier_as_pair(size_key: str) -> Optional[str]:
    """*size_key* respelled as ``"<cpu>/<memory>/<gib>"`` when it is one of the three
    interactive tier keys, else ``None``.

    RFC section 9 promises this twice: "The existing size keys keep working", and
    "the Fargate backend maps the same three keys to CPU and memory pairs, so a
    launch that does not name a backend behaves as it does today". Without it a
    caller who never asked for Fargate, and whose ``size_key`` is therefore
    ``light``/``balanced``/``power``, is refused by a lane that claims nothing
    above the seam changes. ``sizes.DEFAULT_TIER_KEY`` is ``balanced``, so the
    key refused is the one a caller gets by not choosing.

    The shape is DERIVED from ``sizes.py``'s own tier table rather than written
    out again here. That table is where a tier's vCPU, RAM and disk are decided,
    and it has been changed before -- its own comment records a raised ladder
    while the keys stayed. Deriving means such a raise reaches this lane too; a
    second table here would keep answering with the retired shape, and nothing
    would compare the two.

    Returned as a KEY rather than a finished :class:`TaskSize`, so the derived
    numbers run through exactly the bounds check a hand-written pair does. A tier
    whose shape leaves Fargate's own table is then refused by name instead of
    being handed to ``RunTask``.
    """
    if size_key not in sizes.INTERACTIVE_TIER_KEYS:
        return None
    tier = sizes.get_tier(size_key)
    return FARGATE_SIZE_SEP.join(
        (str(tier.vcpu * 1024), str(tier.ram_gb * 1024), str(tier.disk_gb))
    )


def _parse_size(size_key: str) -> TaskSize:
    """Map a Fargate ``size_key`` to a :class:`TaskSize`, or raise.

    Two spellings are accepted. The first is Fargate's own vocabulary, a
    cpu/memory pair in Fargate units, ``"<cpu>/<memory>"``, with an optional
    ephemeral-storage GiB as a third field, ``"<cpu>/<memory>/<gib>"``;
    ``launch_job.py`` states that a non-EC2 provisioner's ``size_key`` is that
    provisioner's own vocabulary, validated in its own ``provision``. The second
    is one of the three interactive tier keys, which :func:`_tier_as_pair`
    respells into the first form.

    The EC2 ladder is READ for that mapping and is not widened: no Fargate shape
    is added to ``sizes.py``, and an instance type is never consulted. Whichever
    spelling arrives, the numbers are validated identically -- the cpu against
    :data:`FARGATE_MEMORY_FOR_CPU`, the memory against that entry's minimum,
    maximum and step, and the storage against Fargate's GiB range, the same
    checks ``run_task_request`` makes, run here so an unusable key is refused
    with the legal pairs listed before any AWS call. A refusal names the key the
    caller passed, never its respelling.
    """
    parts = (_tier_as_pair(size_key) or size_key).split(FARGATE_SIZE_SEP)
    legal = (
        'a Fargate size is "<cpu>/<memory>" in Fargate units, with an optional '
        '"/<gib>" ephemeral storage, or one of '
        + ", ".join(sizes.INTERACTIVE_TIER_KEYS)
        + "; the cpu/memory pairs are "
        + "; ".join(
            f"{cpu}/{low}..{high} step {step}"
            for cpu, (low, high, step) in sorted(
                FARGATE_MEMORY_FOR_CPU.items(), key=lambda kv: int(kv[0])
            )
        )
    )
    if len(parts) not in (2, 3) or not all(p.strip() for p in parts):
        raise ValueError(f"size {size_key!r} is not a Fargate cpu/memory pair; {legal}")
    cpu, memory = parts[0].strip(), parts[1].strip()
    if not cpu.isdigit() or not memory.isdigit():
        raise ValueError(f"size {size_key!r} is not a Fargate cpu/memory pair; {legal}")
    allowed = FARGATE_MEMORY_FOR_CPU.get(cpu)
    if allowed is None:
        raise ValueError(
            f"size {size_key!r} resolves to cpu={cpu!r}, which is not a Fargate CPU size; "
            f"choose one of {', '.join(sorted(FARGATE_MEMORY_FOR_CPU, key=int))}"
        )
    low, high, step = allowed
    mem = int(memory)
    if mem < low or mem > high or (mem - low) % step:
        raise ValueError(
            f"size {size_key!r} resolves to memory={memory!r}, which is not a Fargate memory "
            f"value for cpu={cpu!r}, which takes {low} to {high} MiB in steps of {step}"
        )
    storage: Optional[int] = None
    if len(parts) == 3:
        gib = parts[2].strip()
        if not gib.isdigit():
            raise ValueError(
                f"size {size_key!r} resolves to ephemeral storage {gib!r}, which is not a "
                f"whole number of GiB; {legal}"
            )
        storage = int(gib)
        if not (EPHEMERAL_STORAGE_MIN_GIB <= storage <= EPHEMERAL_STORAGE_MAX_GIB):
            raise ValueError(
                f"size {size_key!r} resolves to ephemeral storage {storage} GiB, which is "
                f"outside Fargate's {EPHEMERAL_STORAGE_MIN_GIB} to "
                f"{EPHEMERAL_STORAGE_MAX_GIB} GiB range"
            )
    return TaskSize(cpu=cpu, memory=memory, ephemeral_storage_gib=storage)


class FargateLaunchEngine:
    """``LaunchEngine`` for Fargate.

    Every AWS call goes through the single ``aws`` CLI chokepoint in
    :mod:`kiro_crew.cloud.aws`; there is no ``boto3`` and no direct subprocess. That
    chokepoint's agent-session allowlist names no ``ecs`` pair, so every ECS call
    here is refused inside an agent session by design -- a Fargate launch is a
    human/installer action, run from a terminal, exactly as the EC2 lane is.

    The engine is given its :class:`FargateLaunchSpec` at construction rather than
    inventing one in :meth:`provision`, because the launch job hands ``provision``
    no placement, image or secrets and there is no configuration home for them on
    this branch. Without a spec, :meth:`preflight` and :meth:`provision` refuse by
    naming the fields an operator must supply.

    The revision cache maps a spec's :func:`revision_fingerprint` to the task
    definition revision NUMBER that carries it. A cache hit is confirmed against
    the account through ``DescribeTaskDefinition`` before it is launched: a
    remembered number whose ``kirocrew:revision-key`` tag differs from the spec's
    fingerprint is a stale cache, not a launch.
    """

    def __init__(self, spec: Optional[FargateLaunchSpec] = None) -> None:
        self._spec = spec
        #: fingerprint -> confirmed revision number. Populated on registration and
        #: on a confirmed cache hit; a per-process memory, never authoritative.
        self._revisions: dict[str, int] = {}

    def _require_spec(self) -> FargateLaunchSpec:
        if self._spec is None:
            raise ValueError(
                "this Fargate engine was constructed without a launch spec, so it cannot "
                "launch: it needs a placement (cluster, subnets, security_groups), a "
                "digest-pinned image, the crew's secret references, and a cpu architecture. "
                "Supply a FargateLaunchSpec; guessing any of them is the same error as "
                "deleting a task on a guess."
            )
        return self._spec

    def _taskdef_spec(self, spec: FargateLaunchSpec, region: str) -> TaskDefinitionSpec:
        return TaskDefinitionSpec(
            image=spec.image,
            secrets=spec.secrets,
            cpu_architecture=spec.cpu_architecture,
            log=default_log_spec(region),
        )

    def preflight(self, profile: str, region: str) -> None:
        """Validate the region and refuse, by name, what the engine lacks.

        A refusal, not a probe: it makes no ECS call. The region is validated
        through ``validated_region`` and, when the engine holds no spec, it raises
        naming every field an operator must supply -- the same fields
        :meth:`provision` would otherwise have to guess.
        """
        validated_region(region, source="region")
        self._require_spec()

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        """Register or reuse a task definition revision and ``RunTask`` it.

        Owns the two obligations the ``fargate`` module leaves to its caller. It
        refuses a ``tag`` or derived ``started_by`` outside the accepted charset
        (or a ``started_by`` longer than :data:`STARTED_BY_MAX`) here rather than
        at launch. And on a cache hit it confirms, through
        ``DescribeTaskDefinition``, that the remembered revision's
        ``kirocrew:revision-key`` tag still equals :func:`revision_fingerprint` of
        the spec before launching that number; a mismatch is a stale cache and
        raises rather than launching the wrong content.

        Returns the task ARN ``RunTask`` reports.
        """
        spec = self._require_spec()
        if not tag or not _TAG_VALUE_RE.match(tag):
            raise ValueError(
                f"launch tag {tag!r} is outside the letters, digits, hyphen and underscore a "
                "correlation value uses; RunTask rejects it at launch"
            )
        started_by = _started_by_for(tag)
        if len(started_by) > STARTED_BY_MAX:
            raise ValueError(
                f"startedBy {started_by!r} is {len(started_by)} characters; the API accepts at "
                f"most {STARTED_BY_MAX}"
            )
        if not _TAG_VALUE_RE.match(started_by):
            raise ValueError(
                f"startedBy {started_by!r} is outside the letters, digits, hyphen and "
                "underscore the API accepts"
            )
        if spec.cpu_architecture not in CPU_ARCHITECTURES:
            raise ValueError(
                f"cpu architecture {spec.cpu_architecture!r} is not one of "
                f"{', '.join(sorted(CPU_ARCHITECTURES))}"
            )

        size = _parse_size(size_key)
        taskdef = self._taskdef_spec(spec, region)
        revision = self._revision_for(taskdef, profile=profile, region=region)

        request = run_task_request(
            revision=revision,
            taskdef=taskdef,
            placement=spec.placement,
            size=size,
            launch_tag=tag,
            started_by=started_by,
        )
        result = aws.checked_json(
            ["ecs", "run-task", "--cli-input-json", _json(request)],
            profile,
            region,
            action="ecs:RunTask",
        )
        tasks = (result or {}).get("tasks") or []
        failures = (result or {}).get("failures") or []
        if not tasks:
            detail = (
                "; ".join(f"{f.get('reason', '?')} ({f.get('arn', '?')})" for f in failures)
                or "RunTask returned no task"
            )
            raise aws.AWSError(f"ecs:RunTask started no task: {detail}", action="ecs:RunTask")
        arn = str(tasks[0].get("taskArn") or "")
        if not arn:
            raise aws.AWSError("ecs:RunTask returned a task with no ARN", action="ecs:RunTask")
        return arn

    def _revision_for(self, taskdef: TaskDefinitionSpec, *, profile: str, region: str) -> int:
        """The revision number that carries *taskdef*'s fingerprint, confirmed.

        On a cache hit, confirm the remembered number's ``kirocrew:revision-key``
        tag through ``DescribeTaskDefinition`` before returning it; a mismatch is a
        stale cache and raises. On a miss, ``RegisterTaskDefinition`` a fresh
        revision from :func:`~kiro_crew.cloud.fargate.task_definition_document` and
        remember it.
        """
        fingerprint = revision_fingerprint(taskdef)
        binding = spec_binding(taskdef)
        family = task_family(binding)
        cached = self._revisions.get(fingerprint)
        if cached is not None:
            described = aws.checked_json(
                [
                    "ecs",
                    "describe-task-definition",
                    "--task-definition",
                    f"{family}:{cached}",
                    "--include",
                    "TAGS",
                ],
                profile,
                region,
                action="ecs:DescribeTaskDefinition",
            )
            tags = {
                str(t.get("key")): str(t.get("value"))
                for t in ((described or {}).get("tags") or [])
            }
            if tags.get(FINGERPRINT_TAG_KEY) != fingerprint:
                raise aws.AWSError(
                    f"cached revision {family}:{cached} carries "
                    f"{FINGERPRINT_TAG_KEY}={tags.get(FINGERPRINT_TAG_KEY)!r}, not the "
                    f"{fingerprint!r} this spec fingerprints to. A remembered revision whose "
                    "content changed is a stale cache, not a launch.",
                    action="ecs:DescribeTaskDefinition",
                )
            return cached

        document = task_definition_document(taskdef)
        registered = aws.checked_json(
            ["ecs", "register-task-definition", "--cli-input-json", _json(document)],
            profile,
            region,
            action="ecs:RegisterTaskDefinition",
        )
        revision = int(((registered or {}).get("taskDefinition") or {}).get("revision") or 0)
        if revision < 1:
            raise aws.AWSError(
                "ecs:RegisterTaskDefinition returned no revision number",
                action="ecs:RegisterTaskDefinition",
            )
        self._revisions[fingerprint] = revision
        return revision

    #: Why this engine cannot sign in as an Identity Center identity. Read by
    #: ``launch_job._check_signin_target_supported`` at PREFLIGHT, so the launch
    #: fails before ``provision`` runs and nothing is billed.
    _IDENTITY_CENTER_REFUSAL = (
        "a Fargate crew is credentialed by the API key its container is started "
        "with and has no interactive sign-in. Launch with the default Builder ID "
        "identity, or use the EC2 provisioner for Identity Center."
    )

    def login_target_refusal(self, target: KiroLoginTarget) -> str:
        """Return the reason a non-default ``target`` cannot be honoured, else ``""``.

        The default (Builder ID) target is what every managed launch carried
        before ``login_target`` existed and is honoured trivially: the container
        never signs in, so there is no identity to get wrong. Any other target
        names an Identity Center instance the container has no way to sign in
        to, and saying so here, at preflight, is what keeps the refusal free.
        """
        if target.is_default:
            return ""
        return self._IDENTITY_CENTER_REFUSAL

    def begin_signin(
        self,
        *,
        instance_id: str,
        profile: str,
        region: str,
        login_target: KiroLoginTarget | None = None,
    ) -> FargateSigninHandle:
        """Return a handle that completes at once. See :class:`FargateSigninHandle`.

        Implemented rather than deferred because it depends on no AWS call and on
        no signature: the container's own code establishes that there is no
        interactive sign-in to perform.

        ``login_target`` is accepted because the ``LaunchEngine`` Protocol
        declares it. A non-default target never reaches this method through
        ``run_launch``: :meth:`login_target_refusal` fails the launch at
        preflight. Receiving one anyway means a caller skipped preflight, which
        is a programming error; raising here is the same guard
        ``launch_job._begin_signin_with_target`` keeps for its own
        never-taken branch, and not a runtime path.
        """
        target = login_target or KiroLoginTarget()
        if not target.is_default:
            raise RuntimeError(
                f"cannot sign in as {target.describe()}: {self._IDENTITY_CENTER_REFUSAL}"
            )
        return FargateSigninHandle(task_arn=instance_id)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        """Do nothing, deliberately.

        ``instances/registry.py`` closes its transport set to ``("ssh", "ssm")``
        and raises for anything outside it, so a Fargate crew cannot be registered
        without changing registry code. Registry visibility is out of scope for
        this phase and ``register`` carries no exit criteria, so a no-op is the
        specified behaviour rather than a gap --
        and it is a no-op rather than a raise because ``run_launch`` calls this step
        unconditionally and a raise would fail a launch that otherwise succeeded.
        """

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        """Stop the tasks this launch owns, and confirm only when nothing of ours
        may remain.

        Discovers the tasks a spec's cluster holds, reads their tags and
        ``startedBy`` through ``DescribeTasks``, builds a :class:`TaskSighting` for
        each, and hands the set to :func:`plan_teardown`. It then acts on the
        verdict: it stops exactly the tasks the plan names and returns the plan's
        ``confirmed``. A plan that does not confirm -- an unmarked task this
        launcher plausibly started, which the ownership rule refuses to delete on a
        ``startedBy`` guess -- returns ``False`` unchanged, which ``launch_job.py``
        turns into the user-visible "requested but did NOT confirm" warning. A
        partial teardown is a ``False``, never a ``True``.

        Without a spec there is no cluster to discover in, so teardown reports it
        did not confirm rather than claiming success it cannot stand behind.
        """
        if self._spec is None:
            return False
        cluster = self._spec.placement.cluster
        started_by = _started_by_for(tag)

        listed = aws.checked_json(
            [
                "ecs",
                "list-tasks",
                "--cluster",
                cluster,
                "--started-by",
                started_by,
                # startedBy must be the only ListTasks filter (the ECS API rejects
                # it combined with any other), so no --desired-status here; RUNNING
                # is the API default and ownership is decided from each task's
                # lastStatus in classify_task, not from this filter.
            ],
            profile,
            region,
            action="ecs:ListTasks",
        )
        arns = [str(a) for a in ((listed or {}).get("taskArns") or [])]
        sightings: list[TaskSighting] = []
        if arns:
            described = aws.checked_json(
                [
                    "ecs",
                    "describe-tasks",
                    "--cluster",
                    cluster,
                    "--tasks",
                    *arns,
                    "--include",
                    "TAGS",
                ],
                profile,
                region,
                action="ecs:DescribeTasks",
            )
            for task in (described or {}).get("tasks") or []:
                tags = {str(t.get("key")): str(t.get("value")) for t in (task.get("tags") or [])}
                sightings.append(
                    TaskSighting(
                        task_arn=str(task.get("taskArn") or ""),
                        tags=tags,
                        started_by=str(task.get("startedBy") or ""),
                        last_status=str(task.get("lastStatus") or ""),
                    )
                )

        plan = plan_teardown(
            sightings,
            launch_tag=tag,
            started_by=started_by,
        )
        for arn in plan.delete:
            aws.checked(
                ["ecs", "stop-task", "--cluster", cluster, "--task", arn],
                profile,
                region,
                action="ecs:StopTask",
            )
        if plan.warning:
            # The Protocol returns a bool, so this is the only channel the named
            # refusal has. ``launch_job`` turns False into "it may still be running
            # and billing", which tells the operator to look but not where: the plan
            # names the ARNs and says which are unclaimable and which are theirs
            # and wrongly tagged, and those two need different next steps.
            logger.warning("fargate teardown %s: %s", tag, plan.warning)
        return plan.confirmed


def _json(payload: object) -> str:
    """Serialise a request body for an ``aws ... --cli-input-json`` argument."""
    return json.dumps(payload)

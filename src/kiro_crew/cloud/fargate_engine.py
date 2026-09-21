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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from kiro_crew.cloud import aws, sizes
from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate import (
    CPU_ARCHITECTURES,
    CREW_TAG_KEY,
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
    credential_recipient,
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
    "TaskBounds",
    "BoundsSweepPlan",
    "DEFAULT_TASK_TTL_SECONDS",
    "DEFAULT_MAX_RUNNING_TASKS",
    "FargateLaunchSpec",
    "classify_task",
    "plan_teardown",
    "plan_bounds_sweep",
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

    #: When this task began, as epoch seconds, or ``None`` when the read did not
    #: say. It is optional because ECS does not report one until a task starts,
    #: and ``None`` is what :func:`plan_bounds_sweep` reads as "age unknown": a
    #: task whose age cannot be established is never stopped for age, and is
    #: named rather than skipped in silence. The engine fills this from
    #: ``startedAt`` and falls back to ``createdAt``, so a task that was created
    #: and never started still has an age.
    started_at: Optional[float] = None

    #: The lifecycle fields a status READ reports and the ownership rule never
    #: reads: where ECS wants the task to be, when it stopped, and ECS's own
    #: sentence for why. Empty or ``None`` when the read did not carry them (a
    #: running task has no stop yet). :func:`classify_task` and
    #: :func:`plan_bounds_sweep` take none of these, so a sighting built without
    #: them classifies exactly as before.
    desired_status: str = ""
    stopped_at: Optional[float] = None
    stopped_reason: str = ""

    @property
    def is_running(self) -> bool:
        """Whether this task is still consuming money.

        ``STOPPED`` is the only ECS lifecycle state that is not billable, so
        everything else counts as running for the purpose of warning a human.
        """
        return (self.last_status or "").upper() != "STOPPED"


def sighting_from_task(task: Mapping[str, Any]) -> TaskSighting:
    """One ``DescribeTasks`` entry as a :class:`TaskSighting`.

    The ONE place an ECS task becomes the fields this module reasons about.
    :meth:`FargateLaunchEngine._sightings` (the cluster walk teardown and the
    bound sweep read) and :meth:`FargateLaunchEngine.describe_task` (the
    single-task read the dashboard shows) both go through it, so the two reads
    cannot disagree about which field carries a status or a moment: a second
    mapping would be a second place for ``lastStatus`` to be misspelled, and a
    misspelling there reads every task as never started.
    """
    tags = {str(t.get("key")): str(t.get("value")) for t in (task.get("tags") or [])}
    return TaskSighting(
        task_arn=str(task.get("taskArn") or ""),
        tags=tags,
        started_by=str(task.get("startedBy") or ""),
        last_status=str(task.get("lastStatus") or ""),
        started_at=_first_moment(task.get("startedAt"), task.get("createdAt")),
        desired_status=str(task.get("desiredStatus") or ""),
        stopped_at=_first_moment(task.get("stoppedAt")),
        stopped_reason=str(task.get("stoppedReason") or ""),
    )


def split_task_arn(task_arn: str) -> tuple[str, str]:
    """``(cluster, task id)`` from an ECS task ARN; either is ``""`` when absent.

    The long ARN format ECS has issued since 2018 carries the cluster:
    ``arn:aws:ecs:<region>:<account>:task/<cluster>/<task id>``. The short one
    (``.../task/<task id>``) does not, and then the cluster is ``""`` and the
    caller falls back to the spec's. Anything that is not a task ARN gives two
    empty strings rather than a guess at which segment is the id.
    """
    marker = ":task/"
    at = task_arn.find(marker)
    if at < 0:
        return "", ""
    rest = task_arn[at + len(marker) :]
    if not rest:
        return "", ""
    cluster, sep, task_id = rest.rpartition("/")
    if not sep:
        return "", rest
    return cluster, task_id


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

    def abort(self) -> bool:
        """Nothing to stop, and this lane says so rather than leaving it inferred.

        ``True`` here means "confirmed there is no remote login to leave running",
        not "killed one": the container never runs a device-code login (the
        credential arrives as an environment variable), so a cancelled sign-in on
        this lane has no poller that could complete the sign-in later. Explicit
        so that :func:`~kiro_crew.cloud.launch_job._abort_signin` never has to
        guess what a missing method means.
        """
        return True


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
_TAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")


def _started_by_for(tag: str) -> str:
    """The ``startedBy`` a task launched under *tag* carries."""
    return f"{_STARTED_BY_PREFIX}{tag}"


#: How many task ARNs one ``DescribeTasks`` call may carry.
#:
#: The API's own documented ceiling for its ``tasks`` list. It is a constant here
#: rather than an inline number because the reason it exists is not obvious at the
#: call site: the AWS CLI AUTO-PAGINATES ``ListTasks`` by default and merges every
#: page into one response with no ``nextToken`` -- which is the behaviour
#: ``ec2.list_instances`` already relies on for ``resourcegroupstaggingapi
#: get-resources``, with no token loop at all -- so a cluster-wide read hands back
#: every task at once and nothing about a page boundary bounds the describe batch.
#: Without an explicit cap, a cluster holding more tasks than this makes every
#: ``DescribeTasks`` call fail, and with it every launch, on exactly the busy or
#: leaking cluster a lifetime sweep exists for.
DESCRIBE_TASKS_MAX = 100


#: How long a task may run before this launcher stops it, in seconds.
#:
#: Six hours, and the number is READ rather than chosen. ``cloud/connect.py``
#: states the only session length this package commits to: ``mint_token`` takes
#: ``ttl: str = "6h"`` and ``_safe_ttl`` falls back to ``"6h"`` for anything it
#: cannot parse. That is the window the cloud lane already treats as one working
#: session, so a task that outlives it has outlived the only session length the
#: lane names. Inventing a second number here would leave two answers to one
#: question with nothing comparing them, which is the same defect as a second
#: spelling of a tag key.
#:
#: It answers the RFC's open question ("session lifetime against task lifetime
#: ... needs a number") with a number rather than a judgement. A launch that
#: needs longer passes its own :class:`TaskBounds`; what it cannot do is pass
#: none, because the engine holds a default.
DEFAULT_TASK_TTL_SECONDS = 6 * 60 * 60

#: How many of this launcher's tasks may run at once in one cluster.
#:
#: Ten, which is the fan-out width the RFC itself names when it weighs the
#: deferral of registry parity ("ten unregistered fan-out workers are ten things
#: the owner cannot see"). It is a CEILING on the population, not a target: a
#: normal launch runs one task, so an operator reaches this only by fanning out
#: deliberately or by leaking, and the second is what it exists to catch.
DEFAULT_MAX_RUNNING_TASKS = 10


@dataclass(frozen=True)
class TaskBounds:
    """What bounds a task's cost, as two numbers with stated defaults.

    A Fargate task is unattended by construction (RFC section 7), which is the
    difference from the EC2 lane: an instance is launched by someone who is
    watching, and a disposable task is not. So the bound cannot be "a human
    notices", and this is where the alternative is written down.

    ``ttl_seconds`` is a wall-clock cap on one task's life, enforced by stopping
    it. ``max_running`` is a cap on how many of this launcher's tasks may run at
    once in one cluster, enforced by REFUSING a launch rather than by stopping
    anything: which of several running tasks is the leak is not knowable from a
    read, and stopping one on that guess is the error the ownership rule exists
    to avoid. A refusal costs a launch; a wrong stop costs a running crew.

    There is deliberately no idle bound here. An idle-stop needs a last-activity
    signal, and no read available to a stopper reports one: ``DescribeTasks``
    carries lifecycle timestamps and no use, the only activity a task has is
    inside it, and reaching into a task is deferred by RFC section 6. A quiet
    CloudWatch stream is not evidence of idleness either -- a crew that is busy
    without logging reads as idle, so an idle-stop built on it would stop work in
    progress. The TTL is the hard bound on spend; an idle bound only tightens it,
    so its absence leaves nothing unbounded.
    """

    ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS
    max_running: int = DEFAULT_MAX_RUNNING_TASKS

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0:
            raise ValueError(
                f"ttl_seconds={self.ttl_seconds!r} is not a lifetime; a bound of zero or less "
                "would stop a task the moment it started, and there is no spelling here for "
                "an unbounded task"
            )
        if self.max_running <= 0:
            raise ValueError(
                f"max_running={self.max_running!r} would refuse every launch; a cap of at "
                "least one is what makes a launch possible"
            )


@dataclass(frozen=True)
class BoundsSweepPlan:
    """What a bound sweep should stop, and what it found still inside its bounds.

    ``stop`` is the ARNs to stop, and the rule is the same one teardown obeys:
    only a task this launcher can CLAIM is ever stopped. What makes the claim is
    the crew tag ``run_task_request`` writes onto every task it starts -- the
    managed marker says a task is this system's, and the crew tag says whose, and
    a cluster-wide sweep needs the second because a shared cluster can hold a
    colleague's crew. ``running`` is the tasks that are ours, alive and not yet
    over their lifetime -- the population the ``max_running`` cap is measured
    against, which is why the plan reports it rather than deciding the cap itself.
    The engine owns the refusal because the refusal belongs to a launch, and this
    function knows nothing about one.

    ``warning`` names what the sweep did and what it could not judge. A task whose
    read reports no start time, and a managed task carrying no crew tag, are both
    left alone and NAMED: they are still billing, and a bound that silently skips
    the cases it cannot decide is a bound an operator cannot audit.
    """

    stop: tuple[str, ...]
    running: tuple[str, ...]
    warning: str = ""


def plan_bounds_sweep(
    sightings: Sequence[TaskSighting],
    *,
    bounds: TaskBounds,
    crew: str,
    now: float,
) -> BoundsSweepPlan:
    """Decide which of *crew*'s sighted tasks are over their lifetime, and which are
    inside it.

    Deliberately takes no launch tag. A TTL has to reach a task whose tag the
    caller has forgotten -- ``launch_job._new_tag`` mints a fresh random tag for
    every launch, so a sweep scoped to the tag in hand would bound the one task
    that is certainly fine and never the leftovers, which are the whole reason a
    TTL exists. So each sighting supplies its own launch tag and ownership is
    decided per task.

    Attribution is what *crew* is for, and it is the rule this sweep turns on.
    A cluster-wide read reaches every task in the cluster, and a shared account
    can hold a colleague's crew, so the sweep needs a value that says WHOSE a
    managed task is. Two candidates do not: :func:`classify_task` against a
    sighting's own tag can only answer the MARKER half, because ``launch_tag``
    equals the tag the sighting itself carries and so matches by construction;
    and :func:`_started_by_for` is a prefix plus that same tag, carrying no host
    or crew component, so a genuine task of ANY crew satisfies it. Both are kept
    -- the first keeps an unmarked task out of the stop list including the case
    where the launch tag was written into the marker itself, and the second
    refuses a task whose ``startedBy`` disagrees with its own tag -- but neither
    can attribute, and treating them as if they could is how a sweep stops a
    neighbour's running crew. :data:`CREW_TAG_KEY` is the value that attributes:
    ``runtask.run_task_request`` writes the launching crew's name onto every task
    it starts, and a crew name is stable across launches, which is exactly the
    property a TTL needs and a launch tag lacks.

    An empty *crew* is refused rather than treated as a wildcard. It is the one
    value that would match nothing and therefore appear to match everything to a
    reader, and a bound sweep that silently widened to the whole cluster is the
    failure this parameter exists to prevent.

    A ``STOPPED`` task is skipped before anything else. It costs nothing, so it
    is neither stopped again nor counted against the cap.

    A task whose age cannot be established -- no ``startedAt`` and no
    ``createdAt`` in the read -- is never stopped for age. That is the safe
    direction: stopping on an unknown age would stop a task that may have started
    a second ago. It is counted as running, so it still presses against the cap,
    and it is named in the warning so the case is auditable rather than silent.

    A managed task carrying a launch tag and this launcher's stamp but NO crew tag
    is treated the same way: never stopped, counted as running, and named. It
    cannot be attributed, so it may be a leftover of this crew from a launcher too
    old to have written the tag, or a neighbour's from the same. Counting it
    presses the cap and can refuse a launch that would otherwise have gone ahead,
    which is the direction this module already chose -- a refusal costs a launch,
    a wrong stop costs a running crew. A task whose crew tag names a DIFFERENT
    crew is skipped in silence, because on a shared cluster it is not this
    launcher's business and naming every one of them would be noise an operator
    learns to scroll past.
    """
    if not crew:
        raise ValueError(
            "a crew name is required to sweep for bounds; without one the sweep cannot tell "
            "this launcher's tasks from a colleague's on a shared cluster, and stopping a "
            "task it cannot attribute is the guess teardown refuses to make"
        )
    stop: list[str] = []
    running: list[str] = []
    ageless: list[str] = []
    unattributed: list[str] = []
    for sighting in sightings:
        if not sighting.is_running:
            continue
        tag = sighting.tags.get(LAUNCH_TAG_KEY, "")
        if not tag:
            continue
        stamped = _started_by_for(tag)
        if classify_task(sighting, launch_tag=tag, started_by=stamped) is not Ownership.OURS:
            continue
        if sighting.started_by != stamped:
            continue
        sighted_crew = sighting.tags.get(CREW_TAG_KEY, "")
        if not sighted_crew:
            unattributed.append(sighting.task_arn)
            running.append(sighting.task_arn)
            continue
        if sighted_crew != crew:
            continue
        if sighting.started_at is None:
            ageless.append(sighting.task_arn)
            running.append(sighting.task_arn)
            continue
        if now - sighting.started_at >= bounds.ttl_seconds:
            stop.append(sighting.task_arn)
        else:
            running.append(sighting.task_arn)
    parts: list[str] = []
    if stop:
        parts.append(
            f"Stopping {len(stop)} task(s) past the {bounds.ttl_seconds}s lifetime bound "
            f"({', '.join(sorted(stop))})."
        )
    if ageless:
        parts.append(
            f"{len(ageless)} running task(s) report no start time "
            f"({', '.join(sorted(ageless))}), so their age cannot be established and the "
            "lifetime bound was not applied to them. They are still billing: check them from "
            "the console or the CLI."
        )
    if unattributed:
        parts.append(
            f"{len(unattributed)} running task(s) carry no {CREW_TAG_KEY} tag "
            f"({', '.join(sorted(unattributed))}), so they cannot be attributed to a crew and "
            "the lifetime bound was not applied to them. They are still billing, and they "
            "count against the running cap: check whose they are from the console or the CLI."
        )
    return BoundsSweepPlan(stop=tuple(stop), running=tuple(running), warning=" ".join(parts))


def _epoch_seconds(value: object) -> Optional[float]:
    """*value* as epoch seconds, or ``None`` when it does not carry a moment.

    The ``aws`` CLI renders an ECS timestamp as an ISO 8601 string, and the same
    field arrives as a number from a caller that read the API directly, so both
    are accepted. Anything else -- absent, empty, or unparseable -- is ``None``
    rather than a guess, and :func:`plan_bounds_sweep` reads ``None`` as an age
    it may not act on.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        # A naive timestamp is the CLI's own rendering with the offset dropped.
        # ECS reports UTC, so reading it as UTC is the field's own meaning rather
        # than this host's clock, which would shift the age by the local offset.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _first_moment(*values: object) -> Optional[float]:
    """The first of *values* that carries a moment, as epoch seconds.

    Explicit rather than an ``or`` chain, because ``0.0`` is a moment and a falsy
    one: a timestamp of exactly the Unix epoch would be discarded by ``or`` and
    the next field read in its place, which is a different task's age. Returns
    ``None`` only when NONE of them carries a moment, which is what
    :func:`plan_bounds_sweep` reads as an age it may not act on.
    """
    for value in values:
        moment = _epoch_seconds(value)
        if moment is not None:
            return moment
    return None


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
    #: The credential recipient the OPERATOR confirmed for this launch, as
    #: ``CloudConfig.fargate_config().credential_recipient()`` renders it -- the image that
    #: receives the model credential, and the ARN of the secret that delivers it.
    #:
    #: It is what keeps the protections ON ``cloud.json`` defence in depth rather than
    #: load-bearing. Every field above is read from that file, so an agent that could write the
    #: file could choose the container the credential is handed to, and every defence that
    #: answers this by protecting the file is a protection over a NAME -- a seal, a mask, an
    #: alias check -- which has to be complete across platforms and shapes to hold. This value
    #: comes from somewhere else entirely: the launch request the operator made.
    #: :meth:`FargateLaunchEngine.provision` compares it to what it resolves from the spec and
    #: refuses when they differ, so rewriting the file turns a silent substitution into a
    #: refused launch that names both values.
    #:
    #: Empty means NOT CONFIRMED and is refused, not waved through. The default exists so a
    #: caller constructing a spec has to pass the confirmation to get a usable one, rather
    #: than getting a launch it never confirmed.
    confirmed_recipient: str = ""


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

    def __init__(
        self,
        spec: Optional[FargateLaunchSpec] = None,
        *,
        bounds: Optional[TaskBounds] = None,
    ) -> None:
        self._spec = spec
        #: What bounds a task's cost. Defaulted rather than required, because a
        #: caller who supplies nothing must still get a bounded task: an optional
        #: bound that defaults to absent is the unbounded launch this engine is
        #: not allowed to make.
        self._bounds = bounds or TaskBounds()
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

        Refuses first of all when the operator has not confirmed the credential recipient
        this launch resolves (:attr:`FargateLaunchSpec.confirmed_recipient`), before the tag
        checks and before anything is registered or run.

        Owns the two obligations the ``fargate`` module leaves to its caller. It
        refuses a ``tag`` or derived ``started_by`` outside the accepted charset
        (or a ``started_by`` longer than :data:`STARTED_BY_MAX`) here rather than
        at launch. And on a cache hit it confirms, through
        ``DescribeTaskDefinition``, that the remembered revision's
        ``kirocrew:revision-key`` tag still equals :func:`revision_fingerprint` of
        the spec before launching that number; a mismatch is a stale cache and
        raises rather than launching the wrong content.

        It also bounds what it is about to create. Before registering anything it
        runs :meth:`reap`, which stops this launcher's tasks that are past
        :attr:`TaskBounds.ttl_seconds` anywhere in the spec's cluster, and then
        refuses this launch when the tasks still running reach
        :attr:`TaskBounds.max_running`. The two are one step because they answer
        one question -- how much of this launcher is already running -- and
        sweeping first is what keeps a cap from being reached by tasks that should
        already be gone.

        Returns the task ARN ``RunTask`` reports.
        """
        spec = self._require_spec()
        # FIRST, before the tag checks and before any AWS call: this launch delivers the
        # model credential into a container that `cloud.json` names, so the operator must
        # have confirmed which container that is. Resolved from the SPEC -- what is about to
        # run -- rather than re-read from the file, and compared to what the operator
        # confirmed in the launch request, which is not on disk at all.
        #
        # That is what lets `cloud.json` be an ordinary file. A rewrite of it changes the
        # value resolved here, so it stops matching the confirmation and the launch is
        # refused with both values shown, instead of silently handing the credential to an
        # image the operator never chose. No seal, mask or alias check is involved, so none
        # of the ways those can be incomplete reaches this.
        #
        # Refused BEFORE `RegisterTaskDefinition` as well as before `RunTask`: a revision
        # carrying the secret ARNs is durable in the account, so a confirmation checked only
        # at run time would already have written the recipient down.
        resolved = credential_recipient(spec.image, spec.secrets)
        if not spec.confirmed_recipient:
            raise ValueError(
                "this launch would hand the model credential to "
                f"{resolved}, and nothing in the request confirmed that recipient; "
                "re-request the launch confirming it"
            )
        if spec.confirmed_recipient != resolved:
            raise ValueError(
                "the confirmed credential recipient does not match this launch: confirmed "
                f"{spec.confirmed_recipient}, would deliver to {resolved}. The Fargate block "
                "in cloud.json changed since it was confirmed, so nothing was launched"
            )
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

        # Bound BEFORE registering anything, and after every free refusal above, so
        # a launch that was going to be refused for its tag costs no AWS call. The
        # sweep is here because provision is the one place a Fargate launch is
        # driven, and a bound that arrives after the launch path leaves a window
        # where a bug bills real money.
        swept = self.reap(profile=profile, region=region)
        if len(swept.running) >= self._bounds.max_running:
            raise RuntimeError(
                f"{len(swept.running)} running task(s) in cluster "
                f"{spec.placement.cluster!r} are this crew's or carry no crew tag to attribute "
                f"them by, which is the cap of {self._bounds.max_running}. Tear one down "
                "before launching another: they are billing, and stopping one of them here "
                "would be a guess at which is the leak."
            )

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

    def _sightings(
        self,
        *,
        cluster: str,
        profile: str,
        region: str,
        started_by: str = "",
    ) -> list[TaskSighting]:
        """Read *cluster*'s tasks as :class:`TaskSighting` values.

        One reader for both consumers. ``teardown`` asks about one launch and
        passes *started_by*; the bound sweep asks about the whole cluster and
        passes nothing, because a lifetime has to reach a task whose tag nobody
        remembers. A second reader would be two places deciding which fields
        ownership is judged from, and the moment they disagreed one consumer
        would classify every task as another launch's.

        ``startedBy`` must be the ONLY ``ListTasks`` filter -- the API rejects it
        combined with any other -- so there is no ``--desired-status`` here.
        ``RUNNING`` is the API default and ownership is decided from each task's
        ``lastStatus``, not from this filter.

        Paginated, which the single-launch read did not need and the cluster-wide
        one does: ``ListTasks`` answers at most a page at a time, so stopping at
        the first page would leave every task past it unbounded -- and unbounded
        is the state this sweep exists to end. The token loop is kept even though
        the CLI normally makes it unnecessary, because a profile configured not to
        auto-paginate would otherwise read one page and silently under-sweep, and
        a silent under-sweep is worse than a loop that iterates once.

        The ``DescribeTasks`` batch is capped HERE, at
        :data:`DESCRIBE_TASKS_MAX`, and not by a page boundary. The CLI
        auto-paginates ``ListTasks`` and merges every page into one response
        carrying no ``nextToken``, so ``arns`` normally holds the WHOLE cluster in
        a single iteration -- which means a batch sized by "one page" is sized by
        nothing at all. Above the API's ceiling ``DescribeTasks`` refuses, and
        because that refusal propagates out through ``reap`` to ``provision``, the
        result would be that every launch fails on a cluster holding more than
        that many tasks. That is the cluster this sweep is for, so the cap is the
        difference between a bound that works where it matters and one that only
        works on a quiet account.
        """
        sightings: list[TaskSighting] = []
        token = ""
        while True:
            args = ["ecs", "list-tasks", "--cluster", cluster]
            if started_by:
                args += ["--started-by", started_by]
            if token:
                args += ["--next-token", token]
            listed = aws.checked_json(args, profile, region, action="ecs:ListTasks") or {}
            arns = [str(a) for a in (listed.get("taskArns") or [])]
            for start in range(0, len(arns), DESCRIBE_TASKS_MAX):
                batch = arns[start : start + DESCRIBE_TASKS_MAX]
                described = aws.checked_json(
                    [
                        "ecs",
                        "describe-tasks",
                        "--cluster",
                        cluster,
                        "--tasks",
                        *batch,
                        "--include",
                        "TAGS",
                    ],
                    profile,
                    region,
                    action="ecs:DescribeTasks",
                )
                for task in (described or {}).get("tasks") or []:
                    sightings.append(sighting_from_task(task))
            token = str(listed.get("nextToken") or "")
            if not token:
                return sightings

    def describe_task(self, *, task_arn: str, profile: str, region: str) -> Optional[TaskSighting]:
        """Read ONE task by ARN: the lane's own answer to "is this crew still up".

        This is the read the dashboard's cloud panel shows for a Fargate launch.
        The EC2 lane's panel keys liveness on the Instances registry, which a
        teardown updates; this lane registers nothing (see :meth:`register`), so
        the registry cannot speak for its task and ECS is the only source that
        can. ``DescribeTasks`` is that source, through the same
        :func:`sighting_from_task` mapping the cluster walk uses.

        The cluster is taken from the ARN when the ARN carries it (the long
        format), and from the spec only when it does not: a launch recorded
        under a cluster the operator has since renamed in ``cloud.json`` would
        otherwise be looked up in the wrong cluster and read as absent.

        Returns ``None`` when ECS lists the ARN under ``failures`` (its reason is
        ``MISSING``): ECS keeps a stopped task for about an hour and then drops
        it, and a task ECS has dropped has no status to report. The caller says
        exactly that -- ECS does not list it -- and never rounds it to
        "stopped" (unknowable from here) or to "running" (false). A read that
        does not complete raises :class:`aws.AWSError` like every other read in
        this module, and the caller reports THAT as an error, not as any state.
        """
        cluster, _task_id = split_task_arn(task_arn)
        if not cluster:
            if self._spec is None:
                raise ValueError(
                    f"cannot read {task_arn!r}: the ARN names no cluster and this engine has no spec"
                )
            cluster = self._spec.placement.cluster
        described = aws.checked_json(
            [
                "ecs",
                "describe-tasks",
                "--cluster",
                cluster,
                "--tasks",
                task_arn,
                "--include",
                "TAGS",
            ],
            profile,
            region,
            action="ecs:DescribeTasks",
        )
        for task in (described or {}).get("tasks") or []:
            if str(task.get("taskArn") or "") == task_arn:
                return sighting_from_task(task)
        return None

    def reap(self, *, profile: str, region: str, now: Optional[float] = None) -> BoundsSweepPlan:
        """Stop this launcher's tasks that are past their lifetime, and report what
        is left inside it.

        The executing half of :func:`plan_bounds_sweep`, through the same
        ``ecs:StopTask`` channel ``teardown`` uses. Scoped to the spec's cluster
        and to the spec's own CREW, so it stops a leftover from a launch whose tag
        is long forgotten and leaves a colleague's crew in the same cluster alone.
        The crew is derived here the way :meth:`_revision_for` derives it, from
        ``spec_binding`` over this spec's secret ARNs, so the name the sweep
        matches on is the same one ``run_task_request`` tags a task with and the
        two cannot disagree. It is not a new field on the spec for the same
        reason: a second place to say whose tasks these are is a second place for
        the answer to be wrong.

        The read it sweeps is cluster-wide and deliberately unfiltered by
        ``startedBy``, because a lifetime has to reach a task whose tag nobody
        remembers and ``startedBy`` varies with the tag. That is exactly why the
        crew tag carries the attribution instead.

        Called by :meth:`provision`, which is the one place a Fargate launch is
        driven, so a bound arrives with the launch rather than after it. It is
        public because that caller is not the only one worth having: a periodic
        caller would reach a cluster whose last launch has ended, and there is no
        scheduler in this package to register one with today. Being public is what
        lets that land without touching this engine again.

        Returns the plan rather than a bool, because its two answers have
        different readers: the ARNs it stopped are a log line, and the ones still
        running are what :meth:`provision` measures its cap against.
        """
        if self._spec is None:
            return BoundsSweepPlan(stop=(), running=())
        cluster = self._spec.placement.cluster
        plan = plan_bounds_sweep(
            self._sightings(cluster=cluster, profile=profile, region=region),
            bounds=self._bounds,
            crew=spec_binding(self._taskdef_spec(self._spec, region)).crew,
            now=time.time() if now is None else now,
        )
        for arn in plan.stop:
            aws.checked(
                ["ecs", "stop-task", "--cluster", cluster, "--task", arn],
                profile,
                region,
                action="ecs:StopTask",
            )
        if plan.warning:
            logger.warning("fargate bound sweep: %s", plan.warning)
        return plan

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

        sightings = self._sightings(
            cluster=cluster, profile=profile, region=region, started_by=started_by
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

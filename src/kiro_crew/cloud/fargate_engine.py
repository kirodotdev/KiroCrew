"""The Fargate launch engine, and the ownership rule its teardown turns on.

Implements the five-method :class:`~kiro_crew.cloud.launch_job.LaunchEngine`
Protocol for Fargate. The AWS-touching bodies are deliberately absent for now --
they call ``cloud/fargate/*``, whose signatures are still moving -- but the two
things that do NOT depend on those signatures are here and are real: the
ownership rule teardown finds resources by, and the reason ``begin_signin`` has
nothing to do.

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
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence

from kiro_crew.cloud.ec2 import MANAGED_TAG_KEY
from kiro_crew.cloud.fargate import LAUNCH_TAG_KEY, MANAGED_TAG_VALUE

__all__ = [
    "MANAGED_TAG_VALUE",
    "Ownership",
    "TaskSighting",
    "TeardownPlan",
    "classify_task",
    "plan_teardown",
    "FargateSigninHandle",
    "FargateLaunchEngine",
]


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

    ``run_launch`` reads ``already_logged_in`` first and unconditionally, and takes
    the already-signed-in branch when it is true: that branch marks the step done
    without ever showing a prompt, which is exactly the right path for a container
    that is handed its credential at run time. So ``already_logged_in`` is ``True``
    as a statement of fact, and ``url``, ``code`` and ``ports`` are empty because
    there is no prompt to show.
    """

    def __init__(self, task_arn: str) -> None:
        self.task_arn = task_arn
        self.already_logged_in: bool = True
        self.url: str = ""
        self.code: str = ""
        self.ports: list = []

    def wait(self, cancel: threading.Event) -> bool:
        """Return immediately; there is no interactive step to wait for.

        The cancel event is accepted to satisfy the Protocol and is honoured: a
        launch cancelled before this point should not report a completed step.
        """
        return not cancel.is_set()

    def close(self) -> None:
        """Nothing to release. No browser, no device-code poller, no session."""


class FargateLaunchEngine:
    """``LaunchEngine`` for Fargate.

    The AWS-touching bodies raise :class:`NotImplementedError` with the reason,
    because they must call ``cloud/fargate/*`` and that module's public surface is
    not settled yet. Writing calls against a moving signature produces rework, not
    progress; the refusals are placeholders with a stated cause, not an abandoned
    design.

    What is settled and NOT placeholder: the ownership rule in :func:`plan_teardown`,
    the sign-in step's absence in :class:`FargateSigninHandle`, and the fact that
    ``register`` does nothing.
    """

    def preflight(self, profile: str, region: str) -> None:
        raise NotImplementedError(
            "preflight must read the cluster, subnets and the crew's secret before "
            "a launch; it is unwritten until cloud/fargate's signatures are final."
        )

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        raise NotImplementedError(
            "provision must register (or reuse) a task definition revision and call "
            "RunTask; it is unwritten until cloud/fargate's signatures are final."
        )

    def begin_signin(self, *, instance_id: str, profile: str, region: str) -> FargateSigninHandle:
        """Return a handle that completes at once. See :class:`FargateSigninHandle`.

        Implemented rather than deferred because it depends on no AWS call and on
        no signature: the container's own code establishes that there is no
        interactive sign-in to perform.
        """
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
        raise NotImplementedError(
            "teardown must list tasks by startedBy, classify them with "
            "plan_teardown, stop the ones it owns and deregister unused task "
            "definition revisions; the listing call is unwritten until "
            "cloud/fargate's signatures are final. The ownership rule it will use "
            "is already implemented and tested in plan_teardown."
        )

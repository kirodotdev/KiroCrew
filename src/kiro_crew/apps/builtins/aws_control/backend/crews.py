"""Remote crews — the deployed-crew inventory behind the console's Crews pane.

A remote crew is a Kiro Crew gateway the owner deployed into their OWN AWS account
as a service their customers can reach: one CloudFormation stack per crew, one ECS
service inside it, behind the shared load balancer the base stack owns. This module
answers what exists and what state it is in. It creates nothing.

Two vocabulary notes, because the word is overloaded in this codebase:

* On the Agents page a "crew" is a LOCAL agent, and its card component is called
  ``CrewCard``. The UI here says "remote crews" for that reason.
* ``kirocrew-drive-*`` buckets belong to the personal cloud drive and are
  unrelated. A crew's own bucket is ``smc-<account>-<region>``.

Everything routes through :func:`kiro_crew.deploy.engine.run_aws`, the AWS CLI
subprocess chokepoint, exactly as the drive does. No boto3.

**The account binding is asserted, not assumed.** ``profile`` is a name resolved by
a child CLI process, so a profile repointed from account A to account B would have
this module report B's crews under a request for A. Every listing therefore
re-derives the account from ``sts get-caller-identity`` through the SAME profile and
refuses when it disagrees with the account the caller verified. That is the drive's
posture (see ``storage.find_drive``) applied to a read-only surface, because the
consequence here is disclosure rather than a misdirected write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from kiro_crew.deploy import engine

#: Crew stacks are named ``smc-crew-<crew>``; the base stack is ``smc-base``.
#: Anchored so a stack merely CONTAINING the prefix cannot be read as a crew.
_STACK_RE = re.compile(r"^smc-crew-([a-z0-9][a-z0-9-]{0,30}[a-z0-9])$")

#: Stack states that mean the crew is present enough to describe. A stack being
#: deleted is deliberately included: the owner needs to see it while it drains,
#: and hiding it is how a half-deleted crew becomes a surprise on the next bill.
_LIVE_STATES = (
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "ROLLBACK_COMPLETE",
    "CREATE_IN_PROGRESS",
    "UPDATE_IN_PROGRESS",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
)


@dataclass
class RemoteCrew:
    """One deployed crew, as the console renders it.

    Every field is either read from AWS or left at its default. Nothing here is
    inferred from a name: ``memory`` comes from the stack's own parameter, not from
    whether a bucket happens to exist, because a crew whose template says chatbot
    while its container carries a bucket is exactly the disagreement worth showing.
    """

    name: str
    stack: str
    stack_status: str = ""
    #: chatbot | persistent | "" when the stack predates the parameter.
    memory: str = ""
    service: str = ""
    #: running/desired, so "1/1" reads as healthy and "0/1" as not.
    running: int = 0
    desired: int = 0
    image: str = ""
    control_base: str = ""
    region: str = ""

    @property
    def healthy(self) -> bool | None:
        """True, False, or None when nothing measured it.

        ``running``/``desired`` are only populated by :func:`describe_crew`, which
        is the one path that calls ECS. On a LIST payload nothing has asked ECS
        anything, so a boolean here would report ``False`` for every crew in the
        account and read as "they are all down" when it means "nobody looked".
        None is the honest third state, and the UI renders the stack status
        instead rather than dressing an absence up as a measurement.

        A crew scaled to zero is a separate case that is also not False: nothing
        is faulty, nothing was asked for. ``desired == 0`` therefore answers None
        as well, and the detail view says so in words.
        """
        if self.desired == 0:
            return None
        return self.running == self.desired


@dataclass
class CrewInventory:
    """What one account holds. ``crews`` is sorted by name for a stable render."""

    account: str
    region: str
    crews: list[RemoteCrew] = field(default_factory=list)
    #: Present when the base stack is missing, which means no crew can exist yet.
    base_missing: bool = False


class AccountMismatch(RuntimeError):
    """The profile resolved to a different account than the caller verified."""


def _checked(args: list[str], profile: str, *, action: str) -> str:
    rc, out, err = engine.run_aws(args, profile)
    if rc != 0:
        raise RuntimeError(f"{action} failed: {engine._trimmed_stderr(err)}")
    return out


def _assert_account(profile: str, region: str, *, account: str) -> None:
    """Refuse to report anything if the profile does not resolve to ``account``."""
    out = _checked(
        [
            "sts",
            "get-caller-identity",
            "--query",
            "Account",
            "--output",
            "text",
            "--region",
            region or engine.DEFAULT_REGION,
        ],
        profile,
        action="sts:GetCallerIdentity",
    )
    resolved = (out or "").strip()
    if resolved != account:
        raise AccountMismatch(
            f"profile resolves to account {resolved or '(unknown)'}, not {account}"
        )


def _stacks(profile: str, region: str) -> list[dict]:
    out = _checked(
        [
            "cloudformation",
            "describe-stacks",
            "--output",
            "json",
            "--region",
            region or engine.DEFAULT_REGION,
        ],
        profile,
        action="cloudformation:DescribeStacks",
    )
    try:
        return json.loads(out or "{}").get("Stacks", [])
    except json.JSONDecodeError:
        return []


def _param(stack: dict, key: str) -> str:
    for p in stack.get("Parameters", []):
        if p.get("ParameterKey") == key:
            return str(p.get("ParameterValue", ""))
    return ""


def _output(stack: dict, key: str) -> str:
    for o in stack.get("Outputs", []):
        if o.get("OutputKey") == key:
            return str(o.get("OutputValue", ""))
    return ""


def list_crews(profile: str, region: str, *, account: str) -> CrewInventory:
    """Every deployed crew in the account, with its serving state.

    One ``describe-stacks`` call answers presence, mode and endpoint for every
    crew at once. Service state needs one call per crew, and that is deliberately
    NOT made here: the list view shows what the stacks say, and
    :func:`describe_crew` fills in the running count when a crew is opened. A
    console that fanned out N ECS calls to draw a list would make the list slower
    for every crew the owner is not looking at.
    """
    _assert_account(profile, region, account=account)
    stacks = _stacks(profile, region)
    inv = CrewInventory(account=account, region=region or engine.DEFAULT_REGION)
    inv.base_missing = not any(s.get("StackName") == "smc-base" for s in stacks)
    for s in stacks:
        m = _STACK_RE.match(str(s.get("StackName", "")))
        if not m or str(s.get("StackStatus", "")) not in _LIVE_STATES:
            continue
        inv.crews.append(
            RemoteCrew(
                name=m.group(1),
                stack=str(s["StackName"]),
                stack_status=str(s.get("StackStatus", "")),
                memory=_param(s, "Memory"),
                image=_param(s, "ImageUri"),
                control_base=_output(s, "ControlBaseUrl"),
                region=inv.region,
            )
        )
    inv.crews.sort(key=lambda c: c.name)
    return inv


def describe_crew(profile: str, region: str, *, account: str, crew: str) -> Optional[RemoteCrew]:
    """One crew with its ECS service state, or None when no such stack exists."""
    inv = list_crews(profile, region, account=account)
    found = next((c for c in inv.crews if c.name == crew), None)
    if found is None:
        return None
    found.service = f"smc-{found.name}"
    out = _checked(
        [
            "ecs",
            "describe-services",
            "--cluster",
            "smc",
            "--services",
            found.service,
            "--query",
            "services[0].[runningCount,desiredCount]",
            "--output",
            "json",
            "--region",
            found.region,
        ],
        profile,
        action="ecs:DescribeServices",
    )
    try:
        counts = json.loads(out or "[]")
    except json.JSONDecodeError:
        counts = []
    if isinstance(counts, list) and len(counts) == 2:
        found.running = int(counts[0] or 0)
        found.desired = int(counts[1] or 0)
    return found


def to_json(inv: CrewInventory) -> dict:
    """The wire shape. Field names match ``types.ts`` RemoteCrew exactly."""
    return {
        "account": inv.account,
        "region": inv.region,
        "baseMissing": inv.base_missing,
        "crews": [crew_json(c) for c in inv.crews],
    }


def crew_json(c: RemoteCrew) -> dict:
    return {
        "name": c.name,
        "stack": c.stack,
        "stackStatus": c.stack_status,
        "memory": c.memory,
        "service": c.service,
        "running": c.running,
        "desired": c.desired,
        "image": c.image,
        "controlBase": c.control_base,
        "region": c.region,
        "healthy": c.healthy,
    }

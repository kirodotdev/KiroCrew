"""The edition's additions to an EC2 crew's instance role, read and checked.

The ``cloud_instance_policy`` seam (:class:`CloudInstancePolicy`) lets an edition
attach managed policies to the instance role and widen its permissions boundary
for them. Three places consume it, and all three read it through this module so
they cannot disagree: the boundary document (``iam.boundary_policy_document``),
the launcher-policy addendum (``iam.host_policy_document``) and the
template's ``ExtraManagedPolicyArns`` parameter (``ec2.deploy``).

The checks here are a floor, not a review of the edition's policy. They refuse
the shapes that would make the boundary stop being a ceiling (``NotAction``,
``NotResource``, a ``Deny``, a bare ``*`` action, any IAM or STS action) and
anything the template or the launcher policy could not carry safely. A
descriptor that fails them refuses the launch rather than being trimmed:
launching with part of what the organization requires is the non-compliant
instance this seam exists to prevent.

A seam read that FAILS (the provider raises) refuses the same way, every time.
Reading it as empty instead would build the narrow boundary and attach nothing,
which is again the non-compliant instance; and an empty read in the boundary
upgrade would call an out-of-date boundary current.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from kiro_crew.cloud import aws

#: Stands for the launch account's id in a managed-policy ARN.
ACCOUNT_PLACEHOLDER = "{account}"

#: IAM attaches at most ten managed policies to a role by default and Session
#: Manager's takes one. Eight leaves room without letting the list grow past it.
MAX_MANAGED_POLICIES = 8
MAX_BOUNDARY_STATEMENTS = 16

#: A managed-policy ARN: AWS-managed, the launch account's (``{account}``), or one
#: named account. No wildcard (it becomes a CloudFormation value and an
#: ``iam:PolicyARN`` condition), and no comma (the template splits the list on it).
_POLICY_ARN_RE = re.compile(
    r"\Aarn:aws:iam::(aws|\{account\}|[0-9]{12}):policy/"
    r"(?:[A-Za-z0-9+=.@_-]+/)*[A-Za-z0-9+=.@_-]{1,128}\Z"
)
_SSM_CORE_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"

#: Statement keys this module passes through. ``NotAction`` and ``NotResource``
#: are absent on purpose: either one turns an allow into "everything but".
_STATEMENT_KEYS = frozenset({"Sid", "Effect", "Action", "Resource", "Condition"})
_SID_RE = re.compile(r"\A[A-Za-z0-9]{1,128}\Z")
#: Sids the built-in boundaries already use; an added statement may not shadow one.
RESERVED_SIDS = frozenset({"SsmCore", "SourceBucketRead", "SsmSessionKms"})
#: Services whose actions never belong on a crew's ceiling: either one lets the
#: role change what it, or another principal, may do.
_REFUSED_SERVICES = ("iam:", "sts:")
_SERVICE_RE = re.compile(r"\A[a-z0-9-]{1,64}\Z")
_MAX_STRING_LEN = 2048
#: The template's ``ExtraManagedPolicyArns`` pattern allows 512 characters after
#: ``policy/``; a longer ARN would pass here and fail at CloudFormation, after the
#: boundary and the source upload.
_MAX_POLICY_PATH_LEN = 512


def _refuse(message: str) -> aws.AWSError:
    return aws.AWSError(
        f"the edition's instance-role policy is invalid: {message} — refusing to launch "
        "with part of it.",
        action="cloud_instance_policy",
    )


def _read_seam() -> Any:
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        return current_context().cloud_instance_policy.instance_policy()
    except PlatformCompositionError:
        # A host that could not compose its edition must abort, never act on
        # defaults: the CPP fail-closed invariant.
        raise
    except Exception as exc:
        raise _refuse(f"the seam could not be read ({type(exc).__name__})") from exc


def _strings(value: Any, field: str) -> list[str]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not items:
        raise _refuse(f"{field} must be a non-empty string or list of strings")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item or len(item) > _MAX_STRING_LEN:
            raise _refuse(f"{field} holds a value that is not a short non-empty string")
        out.append(item)
    return out


def _check_statement(stmt: Any, seen: set[str]) -> dict[str, Any]:
    if not isinstance(stmt, dict):
        raise _refuse("a boundary statement is not an object")
    extra = set(stmt) - _STATEMENT_KEYS
    if extra:
        raise _refuse(f"a boundary statement carries unsupported keys {sorted(extra)}")
    sid = stmt.get("Sid")
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        raise _refuse("every boundary statement needs an alphanumeric Sid")
    if sid in RESERVED_SIDS or sid in seen:
        raise _refuse(f"boundary statement Sid {sid!r} is reserved or repeated")
    seen.add(sid)
    if stmt.get("Effect") != "Allow":
        raise _refuse(f"boundary statement {sid} must be an Allow")
    for action in _strings(stmt.get("Action"), f"{sid}.Action"):
        # The service prefix must be spelled out: a wildcard there ("*:*",
        # "?am:PassRole") would reach the IAM and STS actions refused below.
        service, sep, _op = action.lower().partition(":")
        if not sep or not _SERVICE_RE.match(service) or f"{service}:" in _REFUSED_SERVICES:
            raise _refuse(f"boundary statement {sid} names action {action!r}")
    _strings(stmt.get("Resource"), f"{sid}.Resource")
    if "Condition" in stmt and not isinstance(stmt["Condition"], dict):
        raise _refuse(f"boundary statement {sid} has a Condition that is not an object")
    return copy.deepcopy(stmt)


def instance_policy() -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """The validated (managed policy ARNs, boundary statements) the edition adds.

    Raises :class:`aws.AWSError` when the descriptor fails a check or the seam
    cannot be read. Statements are deep copies, so a caller that edits a document
    cannot reach the provider's.
    """
    policy = _read_seam()
    # A provider answering something that is not a descriptor is refused, not
    # read as "adds nothing": that would launch without the required policies.
    if not hasattr(policy, "managed_policy_arns") or not hasattr(policy, "boundary_statements"):
        raise _refuse(f"the seam answered {type(policy).__name__}, not a CloudInstancePolicy")
    arns = tuple(policy.managed_policy_arns or ())
    statements = tuple(policy.boundary_statements or ())
    if len(arns) > MAX_MANAGED_POLICIES:
        raise _refuse(f"more than {MAX_MANAGED_POLICIES} managed policies")
    if len(statements) > MAX_BOUNDARY_STATEMENTS:
        raise _refuse(f"more than {MAX_BOUNDARY_STATEMENTS} boundary statements")
    for arn in arns:
        if (
            not isinstance(arn, str)
            or not _POLICY_ARN_RE.match(arn)
            or len(arn.split(":policy/", 1)[1]) > _MAX_POLICY_PATH_LEN
        ):
            raise _refuse(f"{arn!r} is not a managed-policy ARN")
        if arn == _SSM_CORE_ARN:
            raise _refuse("Session Manager's policy is already attached")
    if len(set(arns)) != len(arns):
        raise _refuse("a managed policy is listed twice")
    seen: set[str] = set()
    checked = tuple(_check_statement(s, seen) for s in statements)
    return arns, checked


def boundary_statements() -> list[dict[str, Any]]:
    """The validated statements to append to the instance permissions boundary."""
    return list(instance_policy()[1])


def managed_policy_arns(account: str) -> list[str]:
    """The validated managed-policy ARNs for *account* (``{account}`` filled in).

    ``account="*"`` gives the account-agnostic form the printed launcher policy
    uses, so one document still works in every account.
    """
    return [arn.replace(ACCOUNT_PLACEHOLDER, account) for arn in instance_policy()[0]]

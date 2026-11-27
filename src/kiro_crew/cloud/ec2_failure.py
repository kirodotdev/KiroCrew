"""Why a ``cloudformation deploy`` of the EC2 stack failed, in plain words.

``ec2.deploy`` hands the CLI's stderr and the stack's failed events here and
raises the :class:`aws.AWSError` this module builds. The cause leads, ahead of
the raw error, so the dashboard's 400-character cut keeps it.

EC2 refusals are matched on EC2's own message, because CloudFormation's
resource handler drops EC2's error code from the event reason.
"""

from __future__ import annotations

from typing import Optional

from kiro_crew.cloud import aws, sizes

# EC2's InsufficientInstanceCapacity message, as CloudFormation relays it:
# "We currently do not have sufficient m7g.2xlarge capacity in the
# Availability Zone you requested (us-east-1c). ..."
_CAPACITY_MARK = "do not have sufficient"
_CAPACITY_CODE = "InsufficientInstanceCapacity"
_ZONE_MARK = "Availability Zone you requested ("


def capacity_zone(failures: list[dict[str, str]]) -> Optional[str]:
    """The zone of an EC2 out-of-capacity refusal in ``failures``.

    ``None`` when no failure is one; ``""`` when one is, but EC2 named no zone.
    """
    for f in failures:
        reason = f.get("reason", "")
        if _CAPACITY_CODE not in reason and not (_CAPACITY_MARK in reason and "capacity" in reason):
            continue
        start = reason.find(_ZONE_MARK)
        if start < 0:
            return ""
        start += len(_ZONE_MARK)
        end = reason.find(")", start)
        return reason[start:end].strip() if end > start else ""
    return None


def _other_arch_tier(tier: sizes.SizeTier) -> Optional[sizes.SizeTier]:
    """The tier with the same vCPU and RAM on the other architecture."""
    for t in sizes.all_tiers():
        if t.arch != tier.arch and t.vcpu == tier.vcpu and t.ram_gb == tier.ram_gb:
            return t
    return None


def capacity_refusal(tier: sizes.SizeTier, zones: list[str], *, explicit_subnet: bool) -> str:
    """The plain-words cause and next step when EC2 had no capacity for ``tier``.

    ``zones`` lists every zone that refused, in launch order. More than one
    means the launch already moved to other zones and every one refused.
    """
    named = [z for z in zones if z]
    if len(named) > 1:
        lead = (
            f"AWS has no {tier.instance_type} capacity right now in any zone this launch "
            f"could use ({', '.join(named)})"
        )
    else:
        where = named[0] if named else "the zone this launch used"
        lead = f"AWS has no {tier.instance_type} capacity in {where} right now"
    steps = []
    if explicit_subnet:
        steps.append("clear the Subnet ID (--subnet) so the launch tries the other zones")
    other = _other_arch_tier(tier)
    if other:
        steps.append(f"pick the {other.label} size (--size {other.key})")
    steps.append("pick another Region")
    return f"{lead}: {', or '.join(steps)}, and retry"


def account_refusal(failures: list[dict[str, str]], tier: sizes.SizeTier) -> str:
    """The plain-words cause and next step when EC2 refused the instance on an
    account limit, else ``""``.
    """
    reasons = " ".join(f["reason"] for f in failures)
    if "current vCPU limit" in reasons:
        smaller = any(t.vcpu < tier.vcpu for t in sizes.all_tiers())
        return (
            f"this AWS account's vCPU quota in this Region has no room for the {tier.vcpu} "
            f"vCPU of the {tier.label} size: request a higher Running On-Demand Standard "
            "instances quota (L-1216C47A) in Service Quotas"
            + (", or pick a smaller size," if smaller else "")
            + " and retry"
        )
    if "in this region is being validated" in reasons:
        return (
            "this AWS account is still being validated for launches in this Region: AWS "
            "emails you when it is done (usually within minutes, up to 4 hours), then retry"
        )
    return ""


def deploy_error(
    *,
    rc: int,
    err: str,
    failures: list[dict[str, str]],
    tier: sizes.SizeTier,
    extra_managed_policy_arns: tuple[str, ...],
    capacity_zones: list[str],
    explicit_subnet: bool,
) -> aws.AWSError:
    """The error ``ec2.deploy`` raises for a failed ``cloudformation deploy``.

    ``capacity_zones`` lists the zones that refused for lack of capacity; empty
    when no attempt did.
    """
    detail = ""
    if failures:
        lines = "; ".join(f"{f['resource']}: {f['reason']}" for f in failures[:4])
        detail = f" — root cause: {lines}"
    missing = aws.map_missing_action(err)
    # The whole stderr, so its describe-stack-events command keeps the full
    # stack name.
    cause = err.strip()
    if extra_managed_policy_arns and "iam:AttachRolePolicy" in (err + detail):
        # A denied attach of the edition's host policies means the second
        # launcher policy was never applied; the first already grants the one
        # attach it allows, so its grant line would not fix this.
        cause = (
            "this build attaches host policies to the instance role: apply the second "
            "launcher policy from `kirocrew cloud iam-policy --host-policies` and retry"
            f" — {cause}"
        )
    else:
        # A denial CloudFormation meets creating a resource (iam:PassRole for the
        # instance) is only in the events. The WaitCondition's reason is the
        # instance's own setup log, so a denial there is not the launcher's grant.
        for f in failures:
            if not missing and f["resource"] != "WaitCondition":
                missing = aws.map_missing_action(f["reason"])
        if missing:
            cause = f"grant `{missing}` (see `kirocrew cloud iam-policy`) and retry — {cause}"
        elif refusal := account_refusal(failures, tier):
            cause = f"{refusal} — {cause}"
        elif capacity_zones:
            refusal = capacity_refusal(tier, capacity_zones, explicit_subnet=explicit_subnet)
            cause = f"{refusal} — {cause}"
    return aws.AWSError(
        f"cloudformation deploy failed: {cause}{detail}",
        action="cloudformation:CreateStack",
        missing_action=missing,
        returncode=rc,
        stderr=err,
    )

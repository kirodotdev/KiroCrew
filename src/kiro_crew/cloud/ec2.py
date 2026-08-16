"""EC2 lifecycle for the cloud launcher — provision / status / stop / start / destroy.

Every AWS interaction goes through :mod:`kiro_crew.cloud.aws` (the ``run_aws``
chokepoint). Provisioning is a **CloudFormation** stack (``kirocrew-ec2.yaml``):
one atomic, rollback-safe deploy; ``destroy`` is a ``delete-stack`` that removes
*every* resource the launch created (instance, role, instance profile, security
group, EBS volume), preceded by an explicit cancel of the persistent Spot request
a ``--spot`` launch leaves outside the stack (see :func:`cancel_spot_requests`).
Nothing is left behind — this is the clean uninstall/remove-from-AWS path.

Discovery is **stateless-by-tag** (like deploy-web): every resource carries
``kirocrew:managed=true`` + ``kirocrew:instance=<tag>``, and the stack itself is
named deterministically, so ``status``/``stop``/``start``/``destroy`` find the
right stack without relying on a local cache that could drift.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from kiro_crew.cloud import aws, sizes
from kiro_crew.validation import FieldSpec, ValidationError, validate_field

logger = logging.getLogger(__name__)

# Physical stack naming: kirocrew-<tag>. The tag is charset-validated.
STACK_PREFIX = "kirocrew-"
MANAGED_TAG_KEY = "kirocrew:managed"
INSTANCE_TAG_KEY = "kirocrew:instance"

# Terminal CloudFormation stack states.
_COMPLETE_STATES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_FAILED_STATES = {
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "DELETE_FAILED",
    "UPDATE_ROLLBACK_FAILED",
}
_DELETE_DONE = "DELETE_COMPLETE"
_DISCOVERABLE_STACK_STATES = sorted(
    _COMPLETE_STATES
    | _FAILED_STATES
    | {
        "CREATE_IN_PROGRESS",
        "DELETE_FAILED",
        "IMPORT_COMPLETE",
        "IMPORT_IN_PROGRESS",
        "IMPORT_ROLLBACK_COMPLETE",
        "IMPORT_ROLLBACK_FAILED",
        "IMPORT_ROLLBACK_IN_PROGRESS",
        "REVIEW_IN_PROGRESS",
        "ROLLBACK_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
    }
)

# deploy waits can take many minutes (instance boot + dnf + npm build + pip).
# Give the aws CLI a ceiling above the template WaitCondition timeout (1500s).
_DEPLOY_TIMEOUT = 1800
_POLL_TIMEOUT = 60

# --- input validation (values flow into subprocess argv) -------------------
# Cap at 51 chars: the template names the IAM role/instance-profile
# `kirocrew-ec2-${StackTag}` (13-char prefix), and IAM role names max out at 64,
# so 13 + 51 = 64. A longer tag would fail role creation at deploy time.
_TAG_RE = re.compile(r"^[a-zA-Z0-9-]{1,51}$")
_TAG_SPEC = FieldSpec(name="tag", type=str, max_len=51, pattern=_TAG_RE)
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")
_REGION_SPEC = FieldSpec(name="region", type=str, max_len=32, pattern=_REGION_RE)
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROFILE_SPEC = FieldSpec(name="profile", type=str, max_len=128, pattern=_PROFILE_RE)
_CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
_CIDR_SPEC = FieldSpec(name="allow_ssh_cidr", type=str, max_len=18, pattern=_CIDR_RE)
# repo/ref reach a `git clone --branch '<ref>' '<repo>'` in the instance
# UserData; charset-validate them so a crafted value can't break out of the
# single quotes and run as root on the box (defense in depth even though these
# are not CLI-wired today).
_REPO_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,255}$")
_REPO_SPEC = FieldSpec(name="repo", type=str, max_len=255, pattern=_REPO_RE)
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")
_REF_SPEC = FieldSpec(name="ref", type=str, max_len=128, pattern=_REF_RE)
# EC2 subnet ids are `subnet-` + 8 (EC2-Classic era) or 17 hex chars.
_SUBNET_ID_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SUBNET_ID_SPEC = FieldSpec(name="subnet_id", type=str, max_len=24, pattern=_SUBNET_ID_RE)


def _validate_cidr(cidr: str) -> str:
    """Charset- + range-validate a CIDR (octets 0-255, mask 0-32).

    Charset first (so only safe chars ever reach argv), then a precise
    ``ipaddress`` range check for an early, clear error instead of a late,
    opaque CloudFormation failure.
    """
    import ipaddress

    val = validate_field(cidr, _CIDR_SPEC) or ""
    if not val:
        return ""
    try:
        net = ipaddress.ip_network(val, strict=False)
    except ValueError as exc:
        raise ValidationError("allow_ssh_cidr", f"invalid CIDR: {exc}") from None
    # SSH ingress on a personal box should be your own address, and SSM needs
    # no inbound access at all — so require /16 or narrower and hard-refuse
    # anything wider (a /15 is 130k+ hosts; /8 is 16M). Prefer /32.
    if net.prefixlen < 16:
        raise ValidationError(
            "allow_ssh_cidr",
            f"{val} opens SSH to {net.num_addresses:,} addresses (/{net.prefixlen}); "
            "use /16 or narrower (ideally your own IP/32), or omit it entirely — "
            "SSM needs no inbound access",
        )
    if net.prefixlen < 24:
        logger.warning(
            "allow_ssh_cidr %s opens SSH to a wide range (/%d) — prefer your own "
            "IP/32 (SSM needs no inbound access at all)",
            val,
            net.prefixlen,
        )
    # Return the NORMALIZED network (host bits cleared): `1.2.3.4/24` becomes
    # `1.2.3.0/24`. ip_network(strict=False) accepts host bits, but passing an
    # un-normalized CIDR to an SG ingress rule is ambiguous/non-canonical — emit
    # the canonical form so the rule is exactly what the range implies.
    return str(net)


def _template_path() -> Path:
    return Path(__file__).parent / "templates" / "kirocrew-ec2.yaml"


def load_template() -> str:
    """Read the bundled CloudFormation template text."""
    return _template_path().read_text(encoding="utf-8")


def stack_name(tag: str) -> str:
    """Deterministic stack name for a discovery tag."""
    return f"{STACK_PREFIX}{tag}"


def validate_profile(profile: str) -> str:
    return validate_field(profile, _PROFILE_SPEC) or ""


def validate_region(region: str) -> str:
    return validate_field(region, _REGION_SPEC) or ""


def validate_tag(tag: str) -> str:
    val = validate_field(tag, _TAG_SPEC)
    if not val:
        raise ValidationError("tag", "required")
    return val


def validate_subnet_id(subnet_id: str) -> str:
    return validate_field(subnet_id, _SUBNET_ID_SPEC) or ""


@dataclass
class DeployResult:
    """Outcome of a launch."""

    tag: str
    stack_name: str
    region: str
    instance_id: str = ""
    public_dns: str = ""
    status: str = ""
    reused: bool = False
    dry_run: bool = False
    argv: list[str] = field(default_factory=list)


# --- network auto-discovery (so the user never picks a VPC/subnet) ----------


def azs_offering_instance_type(instance_type: str, profile: str, region: str) -> set[str]:
    """AZ names that offer ``instance_type`` (empty set = could not determine).

    Not every AZ offers every instance type (e.g. us-east-1e lacks t4g.xlarge),
    so the subnet we pick MUST be in an AZ that offers the chosen type.
    """
    data = aws.checked_json(
        [
            "ec2",
            "describe-instance-type-offerings",
            "--location-type",
            "availability-zone",
            "--filters",
            f"Name=instance-type,Values={instance_type}",
        ],
        profile,
        region,
        action="ec2:DescribeInstanceTypeOfferings",
    )
    offerings = data.get("InstanceTypeOfferings", []) if isinstance(data, dict) else []
    return {o.get("Location", "") for o in offerings if o.get("Location")}


def discover_network(
    profile: str, region: str, instance_type: str = ""
) -> tuple[str, str, str]:
    """Resolve a (vpc_id, subnet_id, egress_kind) to launch into.

    ``egress_kind`` is ``"nat"`` or ``"igw"`` — the caller uses it to decide
    whether the instance needs a public IP (IGW egress requires one; a NAT
    subnet must NOT get one).

    Prefers the account's **default VPC** and a public subnet within it. When an
    ``instance_type`` is given, the subnet is chosen in an AZ that actually
    offers that type (avoids the "not supported in this AZ" launch failure).
    The chosen subnet must also have a **verified internet-egress route**
    (internet gateway or NAT in its effective route table) — a public-IP flag
    alone doesn't guarantee reachability, and a subnet without egress would hang
    the launch until the WaitCondition times out. Raises :class:`aws.AWSError`
    with actionable text if no default VPC, no AZ-compatible subnet, or no
    egress-capable subnet is available.
    """
    vpcs = aws.checked_json(
        ["ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true"],
        profile,
        region,
        action="ec2:DescribeVpcs",
    )
    vpc_list = vpcs.get("Vpcs", []) if isinstance(vpcs, dict) else []
    if not vpc_list:
        # Fall back to any VPC if there is exactly one.
        allv = aws.checked_json(
            ["ec2", "describe-vpcs"], profile, region, action="ec2:DescribeVpcs"
        )
        vpc_list = allv.get("Vpcs", []) if isinstance(allv, dict) else []
        if len(vpc_list) != 1:
            raise aws.AWSError(
                "no default VPC found — create one (`aws ec2 create-default-vpc`) "
                "or pass `--subnet <subnet-id>`, then retry.",
                action="ec2:DescribeVpcs",
            )
    vpc_id = vpc_list[0]["VpcId"]

    subnets = aws.checked_json(
        ["ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}"],
        profile,
        region,
        action="ec2:DescribeSubnets",
    )
    subnet_list = subnets.get("Subnets", []) if isinstance(subnets, dict) else []
    if not subnet_list:
        raise aws.AWSError(f"VPC {vpc_id} has no subnets", action="ec2:DescribeSubnets")

    # Constrain to AZs that offer the instance type (if we can determine them).
    ok_azs: set[str] = set()
    if instance_type:
        try:
            ok_azs = azs_offering_instance_type(instance_type, profile, region)
        except aws.AWSError:
            ok_azs = set()  # non-fatal — fall back to any subnet

    def _az_ok(s: dict[str, Any]) -> bool:
        return not ok_azs or s.get("AvailabilityZone", "") in ok_azs

    candidates = [s for s in subnet_list if _az_ok(s)]
    if not candidates:
        raise aws.AWSError(
            f"no subnet in an AZ that offers {instance_type} — try a different size or region.",
            action="ec2:DescribeInstanceTypeOfferings",
        )

    # The box needs outbound internet (dnf/npm/pip + SSM). A default route alone
    # isn't enough — the egress KIND matters: a NAT route works from a private
    # subnet, but an internet-gateway route only works with a public IP on the
    # ENI. The template requests AssociatePublicIpAddress, so an IGW subnet works
    # even without MapPublicIpOnLaunch — but we still must not pick a subnet whose
    # only "egress" is an IGW when the launch can't get a public IP. Prefer, in
    # order: NAT (always works) > IGW (works via the associated public IP).
    egress = _subnet_egress_kinds(vpc_id, profile, region)  # {subnet_id: "igw"|"nat"}
    nat = [s for s in candidates if egress.get(s["SubnetId"]) == "nat"]
    if nat:
        return vpc_id, nat[0]["SubnetId"], "nat"
    igw = [s for s in candidates if egress.get(s["SubnetId"]) == "igw"]
    if igw:
        # Prefer one that also auto-assigns a public IP, but the template's
        # conditional AssociatePublicIpAddress makes any IGW subnet workable.
        igw.sort(key=lambda s: not s.get("MapPublicIpOnLaunch"))
        return vpc_id, igw[0]["SubnetId"], "igw"
    raise aws.AWSError(
        f"no subnet in VPC {vpc_id} has a verified internet egress route (internet "
        "gateway or NAT). KiroCrew needs outbound access to install packages and "
        "reach SSM. Add an internet gateway + public route (or a NAT), or pass a "
        "subnet that has one via `--subnet`, then retry.",
        action="ec2:DescribeRouteTables",
    )


def resolve_explicit_subnet(
    subnet_id: str, profile: str, region: str, instance_type: str = ""
) -> tuple[str, str, str]:
    """Resolve a user-chosen ``--subnet`` to ``(vpc_id, subnet_id, egress_kind)``.

    The explicit path skips VPC discovery entirely (the point of the flag:
    launching into a dedicated VPC that auto-discovery would never pick while a
    default VPC exists) but keeps the launch-time guarantees discover_network
    provides: the subnet must exist, its AZ must offer ``instance_type``, and it
    must have a verified internet-egress route (NAT or IGW) — a subnet without
    egress would hang the launch until the WaitCondition timeout, so fail fast
    with actionable text instead.
    """
    data = aws.checked_json(
        ["ec2", "describe-subnets", "--subnet-ids", subnet_id],
        profile,
        region,
        action="ec2:DescribeSubnets",
    )
    subnet_list = data.get("Subnets", []) if isinstance(data, dict) else []
    if not subnet_list:
        raise aws.AWSError(
            f"subnet {subnet_id} not found in {region} — check the id and --region.",
            action="ec2:DescribeSubnets",
        )
    subnet = subnet_list[0]
    vpc_id = subnet.get("VpcId", "")
    az = subnet.get("AvailabilityZone", "")
    if instance_type:
        try:
            ok_azs = azs_offering_instance_type(instance_type, profile, region)
        except aws.AWSError:
            ok_azs = set()  # non-fatal — same fallback as discover_network
        if ok_azs and az not in ok_azs:
            raise aws.AWSError(
                f"subnet {subnet_id} is in {az}, which does not offer "
                f"{instance_type} — pick a subnet in one of "
                f"{', '.join(sorted(ok_azs))}, or a different size.",
                action="ec2:DescribeInstanceTypeOfferings",
            )
    egress = _subnet_egress_kinds(vpc_id, profile, region)
    if subnet_id not in egress:
        raise aws.AWSError(
            f"subnet {subnet_id} has no verified internet egress route (NAT or "
            "internet gateway). Kiro Crew needs outbound access to install "
            "packages and reach SSM — add a NAT (or IGW) default route to the "
            "subnet's route table, then retry.",
            action="ec2:DescribeRouteTables",
        )
    return vpc_id, subnet_id, egress[subnet_id]


def _subnet_egress_kinds(vpc_id: str, profile: str, region: str) -> dict:
    """Map subnet id -> egress kind (``"nat"`` or ``"igw"``) for subnets in ``vpc_id``.

    A subnet uses its explicitly-associated route table, else the VPC main route
    table. NAT is distinguished from IGW because reachability differs: a NAT
    default route gives outbound access from a private subnet, whereas an
    internet-gateway route only works when the instance has a public IP. Subnets
    with no 0.0.0.0/0 route are omitted (no egress).
    """
    rts = aws.checked_json(
        ["ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc_id}"],
        profile,
        region,
        action="ec2:DescribeRouteTables",
    )
    rt_list = rts.get("RouteTables", []) if isinstance(rts, dict) else []

    def _egress_kind(rt: dict[str, Any]) -> Optional[str]:
        # NAT wins over IGW if both somehow present (private-subnet semantics).
        kind = None
        for route in rt.get("Routes", []):
            if route.get("DestinationCidrBlock", "") != "0.0.0.0/0":
                continue
            if route.get("State", "active") != "active":
                continue
            if route.get("NatGatewayId") or route.get("NetworkInterfaceId"):
                return "nat"
            if route.get("GatewayId", "").startswith("igw-"):
                kind = "igw"
        return kind

    main_kind: Optional[str] = None
    # A subnet's EXPLICIT route-table association overrides the main table — even
    # when that explicit table has NO egress. So track which subnets are
    # explicitly bound (regardless of egress) separately from their egress kind:
    # an explicitly-bound no-egress subnet must be EXCLUDED from the main-table
    # fallback, not silently treated as having the main table's egress (which
    # would let discover_network pick a dead subnet and hang to WaitCondition).
    explicit_kind: dict = {}
    explicitly_bound: set = set()
    for rt in rt_list:
        kind = _egress_kind(rt)
        for assoc in rt.get("Associations", []):
            if assoc.get("Main") and kind:
                main_kind = kind
            subnet_id = assoc.get("SubnetId")
            if subnet_id:
                explicitly_bound.add(subnet_id)
                if kind:
                    explicit_kind[subnet_id] = kind

    result: dict = dict(explicit_kind)
    if main_kind:
        # Only subnets with NO explicit association fall back to the main table.
        subnets = aws.checked_json(
            ["ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}"],
            profile,
            region,
            action="ec2:DescribeSubnets",
        )
        for s in subnets.get("Subnets", []) if isinstance(subnets, dict) else []:
            sid = s.get("SubnetId", "")
            if sid and sid not in explicitly_bound:
                result[sid] = main_kind
    return result


def build_deploy_argv(
    *,
    tag: str,
    tier: sizes.SizeTier,
    vpc_id: str,
    subnet_id: str,
    associate_public_ip: str = "true",
    permissions_boundary_arn: str,
    repo: str = "",
    ref: str = "",
    allow_ssh_cidr: str = "",
    source_bucket: str = "",
    source_key: str = "",
    dashboard_port: int = 0,
    spot: bool = False,
) -> list[str]:
    """Assemble the exact ``aws cloudformation deploy`` argv (also the dry-run output).

    ``permissions_boundary_arn`` is the ARN of the SHARED, pre-created immutable
    instance permissions boundary (``source.ensure_instance_boundary`` creates it
    once); it fills the template's ``PermissionsBoundaryArn`` parameter so the
    InstanceRole is capped by it instead of a per-launch CFN-authored boundary.
    """
    overrides = [
        f"InstanceType={tier.instance_type}",
        f"Architecture={tier.arch}",
        f"VolumeSizeGb={tier.disk_gb}",
        f"VpcId={vpc_id}",
        f"SubnetId={subnet_id}",
        f"AssociatePublicIp={associate_public_ip}",
        f"StackTag={tag}",
        f"PermissionsBoundaryArn={permissions_boundary_arn}",
    ]
    # Only sent when the caller allocated one, so a direct deploy keeps the
    # template default rather than being pinned by an implicit 0.
    if dashboard_port:
        overrides.append(f"DashboardPort={dashboard_port}")
    # Opt-in only: an omitted override leaves the template's Spot="false", so the
    # on-demand argv is byte-for-byte what it was before the flag existed.
    if spot:
        overrides.append("Spot=true")
    # Prefer the S3 source (private-repo safe); else pass git repo/ref fallback.
    if source_bucket:
        overrides.append(f"SourceBucket={source_bucket}")
        overrides.append(f"SourceKey={source_key}")
    if repo:
        overrides.append(f"KirocrewRepo={repo}")
    if ref:
        overrides.append(f"KirocrewRef={ref}")
    if allow_ssh_cidr:
        overrides.append(f"AllowSshCidr={allow_ssh_cidr}")
    return [
        "cloudformation",
        "deploy",
        "--stack-name",
        stack_name(tag),
        "--template-file",
        str(_template_path()),
        "--capabilities",
        "CAPABILITY_NAMED_IAM",
        "--tags",
        f"{MANAGED_TAG_KEY}=true",
        f"{INSTANCE_TAG_KEY}={tag}",
        "--parameter-overrides",
        *overrides,
    ]


def deploy(
    *,
    tag: str,
    tier: sizes.SizeTier,
    profile: str = "",
    region: str = "",
    subnet_id: str = "",
    repo: str = "",
    ref: str = "",
    allow_ssh_cidr: str = "",
    ship_source: Optional[bool] = None,
    dashboard_port: int = 0,
    spot: bool = False,
    disable_rollback: bool = False,
    dry_run: bool = False,
    proc_sink: Optional[Any] = None,
) -> DeployResult:
    """Provision (or update) the KiroCrew stack. Idempotent by stack name.

    By default the local source is packaged and uploaded only when Kiro Crew is
    running from a checkout; packaged installs use the template's public-repo
    clone path. Explicit ``ship_source=True`` remains fail-closed when no checkout
    exists. ``subnet_id`` pins the launch to an explicit subnet (validated by
    :func:`resolve_explicit_subnet`) instead of auto-discovery. ``spot``
    (``--spot``) requests Spot pricing — the template turns that into a
    persistent, stop-on-interruption request so an interruption preserves the
    root volume. ``dry_run`` returns the exact argv without calling AWS.
    ``proc_sink`` is forwarded to :func:`aws.run_aws` for the (long) deploy call
    so a caller running deploy on a background thread can terminate the child on
    Ctrl+C.
    """
    if not dry_run:
        aws.assert_human_action("cloudformation:CreateStack")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    if subnet_id:
        subnet_id = validate_subnet_id(subnet_id)
    if allow_ssh_cidr:
        allow_ssh_cidr = _validate_cidr(allow_ssh_cidr)
    if repo:
        repo = validate_field(repo, _REPO_SPEC) or ""
    if ref:
        ref = validate_field(ref, _REF_SPEC) or ""

    from kiro_crew.cloud import source as source_mod

    if ship_source is None:
        ship_source = source_mod.find_repo_root() is not None
        if not ship_source:
            logger.info("no checkout found; the instance will clone the public repo")

    if dry_run:
        # For the dry run we can't hit AWS for the VPC or account id, so show
        # placeholders the real run will resolve. The argv shape is what matters
        # for review/tests.
        argv = build_deploy_argv(
            tag=tag,
            tier=tier,
            vpc_id="<auto>",
            subnet_id=subnet_id or "<auto>",
            associate_public_ip="<auto>",
            permissions_boundary_arn="<auto>",
            repo=repo,
            ref=ref,
            allow_ssh_cidr=allow_ssh_cidr,
            source_bucket="<auto>" if ship_source else "",
            source_key=f"{tag}/kirocrew-src.tar.gz" if ship_source else "",
            dashboard_port=dashboard_port,
            spot=spot,
        )
        return DeployResult(
            tag=tag,
            stack_name=stack_name(tag),
            region=region,
            status="DRY_RUN",
            dry_run=True,
            argv=argv,
        )

    existing = find_stack(tag, profile, region)
    reused = existing is not None

    # Ensure the SHARED, immutable instance permissions boundary exists (created
    # once by launcher code, not per-launch CFN — see source.ensure_instance_boundary
    # / cloud.iam). Every launch references it by its deterministic ARN; the
    # InstanceRole is capped by it. Done before the source upload so a
    # boundary-create failure (e.g. missing iam:CreatePolicy) surfaces before we
    # ship anything to S3.
    boundary_arn = source_mod.ensure_instance_boundary(profile, region)

    # Package + upload the local source so the box installs from S3 (no GitHub
    # access needed). Fall back to a git clone only if source shipping is off.
    source_bucket = source_key = ""
    if ship_source:
        source_bucket, source_key = source_mod.upload_source(tag, profile, region)

    def _cleanup_uploaded_source() -> None:
        # A failed launch must leave nothing behind in S3 either (the object is
        # otherwise only removed on destroy). Best-effort. NB: the shared boundary
        # is intentionally NOT removed here — it's account-wide, reused by every
        # launch, and immutable; teardown leaves it in place.
        if ship_source and source_key:
            try:
                source_mod.delete_source(tag, profile, region)
            except Exception:  # pragma: no cover - best effort
                logger.info("could not remove uploaded source after failed launch")

    # Any failure from here to a successful deploy orphans the uploaded source —
    # network discovery/validation included — so clean it up on the way out.
    try:
        if subnet_id:
            vpc_id, subnet_id, egress_kind = resolve_explicit_subnet(
                subnet_id, profile, region, tier.instance_type
            )
        else:
            vpc_id, subnet_id, egress_kind = discover_network(
                profile, region, tier.instance_type
            )
    except Exception:
        _cleanup_uploaded_source()
        raise
    argv = build_deploy_argv(
        tag=tag,
        tier=tier,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        # A NAT-routed (private) subnet must NOT get a public IP — it is unused
        # surface and can violate SCPs that deny RunInstances-with-public-IP.
        # An IGW subnet REQUIRES one for egress.
        associate_public_ip="false" if egress_kind == "nat" else "true",
        permissions_boundary_arn=boundary_arn,
        repo="" if ship_source else repo,
        ref="" if ship_source else ref,
        allow_ssh_cidr=allow_ssh_cidr,
        source_bucket=source_bucket,
        source_key=source_key,
        dashboard_port=dashboard_port,
        spot=spot,
    )
    # `cloudformation deploy` blocks until the stack settles (WaitCondition gates
    # on the gateway being healthy). "No changes" exits 0 with a message on reuse.
    if disable_rollback:
        argv = [*argv, "--disable-rollback"]
    rc, out, err = aws.run_aws(argv, profile, region, timeout=_DEPLOY_TIMEOUT, proc_sink=proc_sink)
    if rc != 0 and "No changes to deploy" not in (out + err):
        missing = aws.map_missing_action(err)
        hint = f" — grant `{missing}` and retry" if missing else ""
        # The `aws cloudformation deploy` error is generic ("Failed to
        # create/update the stack"); the real cause is in the stack's FAILED
        # events (which now include the on-box bootstrap log tail). Fetch and
        # attach them so the caller sees WHY it failed, not just THAT it did.
        failures = get_stack_failures(tag, profile, region)
        detail = ""
        if failures:
            lines = "; ".join(f"{f['resource']}: {f['reason']}" for f in failures[:4])
            detail = f" — root cause: {lines}"
        _cleanup_uploaded_source()
        raise aws.AWSError(
            f"cloudformation deploy failed: {err.strip()[:200]}{hint}{detail}",
            action="cloudformation:CreateStack",
            missing_action=missing,
            returncode=rc,
            stderr=err,
        )

    st = describe(tag, profile, region)
    return DeployResult(
        tag=tag,
        stack_name=stack_name(tag),
        region=region,
        instance_id=st.get("instance_id", ""),
        public_dns=st.get("public_dns", ""),
        status=st.get("stack_status", ""),
        reused=reused,
        argv=argv,
    )


# --- discovery / status -----------------------------------------------------


def find_stack(tag: str, profile: str = "", region: str = "") -> Optional[dict[str, Any]]:
    """Return the CloudFormation stack summary for ``tag``, or None if absent.

    Only a genuine "does not exist" is reported as absent (``None``). Any other
    non-zero exit — throttling, network, expired SSO, or ``AccessDenied`` on
    ``cloudformation:DescribeStacks`` — is raised as an :class:`aws.AWSError`, so
    callers like :func:`destroy` never mistake a transient failure for "already
    gone" and falsely report that a still-billing stack was removed.
    """
    rc, out, err = aws.run_aws(
        ["cloudformation", "describe-stacks", "--stack-name", stack_name(tag), "--output", "json"],
        profile,
        region,
        timeout=_POLL_TIMEOUT,
    )
    if rc != 0:
        # CloudFormation returns a ValidationError with this phrasing when the
        # stack truly doesn't exist — that (and only that) means "absent".
        if "does not exist" in err:
            return None
        missing = aws.map_missing_action(err)
        raise aws.AWSError(
            f"could not query stack {stack_name(tag)}: {err.strip()[:300]}",
            action="cloudformation:DescribeStacks",
            missing_action=missing,
            returncode=rc,
            stderr=err,
        )
    import json

    try:
        stacks = json.loads(out or "{}").get("Stacks", [])
    except json.JSONDecodeError:
        return None
    if not stacks:
        return None
    stack = stacks[0]
    # A stack merely NAMED kirocrew-<tag> isn't necessarily ours — verify the
    # kirocrew:managed tag. A same-named but UNTAGGED stack is a collision with
    # something we didn't create: raise (not return None), because "None" means
    # "absent" to deploy() and would make it run `cloudformation deploy` against
    # the foreign stack. Raising makes every caller (deploy/destroy/stop/start/
    # status) abort instead.
    tags = {t.get("Key"): t.get("Value") for t in stack.get("Tags", [])}
    if tags.get(MANAGED_TAG_KEY) != "true":
        raise aws.AWSError(
            f"a CloudFormation stack named {stack_name(tag)} already exists but is "
            f"NOT tagged {MANAGED_TAG_KEY}=true — refusing to touch it (it wasn't "
            "created by KiroCrew). Use a different --tag, or remove that stack.",
            action="cloudformation:DescribeStacks",
        )
    # Also require the instance tag to match THIS tag. A managed stack that
    # merely shares the kirocrew- name prefix but carries a different
    # kirocrew:instance value is not this launch's stack — destroy/stop/start
    # must not act on it. (The stack is looked up by name, so this guards against
    # a managed stack whose instance tag was changed or that a companion tool
    # created under the same prefix.)
    inst = tags.get(INSTANCE_TAG_KEY)
    if inst is not None and inst != tag:
        raise aws.AWSError(
            f"stack {stack_name(tag)} is {MANAGED_TAG_KEY}=true but its "
            f"{INSTANCE_TAG_KEY} tag is '{inst}', not '{tag}' — refusing to touch "
            "it (it isn't this launch's stack). Use the correct --tag.",
            action="cloudformation:DescribeStacks",
        )
    return stack


def stack_uses_spot(stack: Optional[dict[str, Any]]) -> bool:
    """True when this stack was deployed with ``Spot=true`` (so it HAS a request).

    The authority on "could this teardown leave a persistent Spot request
    behind?" is the STACK, not the principal running destroy: an admin can
    launch ``--spot`` and a restricted profile can destroy it, so a denied
    ``ec2:DescribeSpotInstanceRequests`` says nothing about whether a request
    exists. The Spot template parameter does, and it costs no extra API call —
    ``cloudformation describe-stacks`` (:func:`find_stack`) already returns
    ``Parameters`` alongside the tags.

    Absent/unparseable parameters read as False, which is the pre-``--spot``
    behaviour (quiet) rather than a false alarm on every on-demand destroy. It
    is only ever used to make the caller LOUDER, never to silence a lookup that
    genuinely failed.
    """
    if not stack:
        return False
    params = stack.get("Parameters") or []
    if not isinstance(params, list):
        return False
    for param in params:
        if isinstance(param, dict) and param.get("ParameterKey") == "Spot":
            return str(param.get("ParameterValue", "")).strip().lower() == "true"
    return False


def get_stack_failures(tag: str, profile: str = "", region: str = "") -> list[dict[str, str]]:
    """Return the FAILED stack events (most-relevant first) for a failed deploy.

    Turns the opaque ``aws cloudformation deploy`` error into the actual root
    cause. Each entry is ``{resource, status, reason}``. For a WaitCondition
    failure the ``reason`` includes the on-box bootstrap log tail (folded in by
    the template's ``fail()``), so the user sees exactly what went wrong on the
    instance even though CloudFormation already terminated it.
    """
    rc, out, _err = aws.run_aws(
        [
            "cloudformation",
            "describe-stack-events",
            "--stack-name",
            stack_name(tag),
            "--output",
            "json",
        ],
        profile,
        region,
        timeout=_POLL_TIMEOUT,
    )
    if rc != 0:
        return []
    import json

    try:
        events = json.loads(out or "{}").get("StackEvents", [])
    except json.JSONDecodeError:
        return []
    # The generic cascade lines CloudFormation adds to siblings ("failed to
    # create: [WaitCondition]", "creation cancelled") are usually the NEWEST
    # events, so they'd sort to the front and bury the real root cause. Collect
    # specific and generic reasons separately, then concatenate specific-first
    # so a consumer reading failures[0] always gets the most useful reason.
    generic_reasons = {
        "",
        "Resource creation cancelled",
        "The following resource(s) failed to create: [WaitCondition].",
    }
    specific: list[dict[str, str]] = []
    generic: list[dict[str, str]] = []
    for ev in events:  # events are newest-first
        status = ev.get("ResourceStatus", "")
        if "FAILED" not in status:
            continue
        reason = ev.get("ResourceStatusReason", "") or ""
        entry = {
            "resource": ev.get("LogicalResourceId", ""),
            "status": status,
            "reason": reason,
        }
        (generic if reason in generic_reasons else specific).append(entry)
    # Drop generic noise entirely when a specific reason exists; otherwise keep
    # the generic ones so the caller still has something to report.
    return specific if specific else generic


def list_stack_events(tag: str, profile: str = "", region: str = "") -> list[dict[str, str]]:
    """Return stack events oldest-first: ``{id, resource, status, reason}``.

    Used by the launcher to stream live progress during ``deploy`` so the user
    sees each resource come up (and the failure reason) instead of a spinner.
    """
    rc, out, _err = aws.run_aws(
        [
            "cloudformation",
            "describe-stack-events",
            "--stack-name",
            stack_name(tag),
            "--output",
            "json",
        ],
        profile,
        region,
        timeout=_POLL_TIMEOUT,
    )
    if rc != 0:
        return []
    import json

    try:
        events = json.loads(out or "{}").get("StackEvents", [])
    except json.JSONDecodeError:
        return []
    out_events: list[dict[str, str]] = []
    for ev in reversed(events):  # oldest first
        out_events.append(
            {
                "id": ev.get("EventId", ""),
                "resource": ev.get("LogicalResourceId", ""),
                "status": ev.get("ResourceStatus", ""),
                "reason": ev.get("ResourceStatusReason", "") or "",
            }
        )
    return out_events


def _outputs(stack: dict[str, Any]) -> dict[str, str]:
    return {o["OutputKey"]: o.get("OutputValue", "") for o in stack.get("Outputs", [])}


def describe(tag: str, profile: str = "", region: str = "") -> dict[str, Any]:
    """Full status for one instance: stack state + outputs + live EC2 state."""
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    stack = find_stack(tag, profile, region)
    if not stack:
        return {"tag": tag, "exists": False}

    outs = _outputs(stack)
    instance_id = outs.get("InstanceId", "")
    result: dict[str, Any] = {
        "tag": tag,
        "exists": True,
        "stack_name": stack.get("StackName", stack_name(tag)),
        "stack_status": stack.get("StackStatus", ""),
        "instance_id": instance_id,
        "public_dns": outs.get("PublicDnsName", ""),
        "region": outs.get("Region", region),
    }
    if instance_id:
        result["instance_state"] = _instance_state(instance_id, profile, region)
    return result


def _instance_state(instance_id: str, profile: str, region: str) -> str:
    rc, out, _err = aws.run_aws(
        [
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--query",
            "Reservations[0].Instances[0].State.Name",
            "--output",
            "text",
        ],
        profile,
        region,
        timeout=_POLL_TIMEOUT,
    )
    return out.strip() if rc == 0 else "unknown"


def list_instances(profile: str = "", region: str = "") -> list[dict[str, Any]]:
    """List all launcher-managed instances live by tag (stateless-by-tag)."""
    profile = validate_profile(profile)
    region = validate_region(region)
    data = aws.checked_json(
        [
            "resourcegroupstaggingapi",
            "get-resources",
            "--tag-filters",
            f"Key={MANAGED_TAG_KEY},Values=true",
            "--resource-type-filters",
            "ec2:instance",
        ],
        profile,
        region,
        action="tag:GetResources",
    )
    out: list[dict[str, Any]] = []
    for mapping in data.get("ResourceTagMappingList", []) if isinstance(data, dict) else []:
        arn = mapping.get("ResourceARN", "")
        tags = {t["Key"]: t["Value"] for t in mapping.get("Tags", [])}
        tag = tags.get(INSTANCE_TAG_KEY, "")
        instance_id = arn.rsplit("/", 1)[-1] if "/" in arn else ""
        state = _instance_state(instance_id, profile, region) if instance_id else ""
        # The tagging API keeps returning terminated instances for a while; only
        # surface live ones (running/stopped/pending/stopping/...), not
        # terminated/shutting-down.
        if state in ("terminated", "shutting-down"):
            continue
        out.append({"tag": tag, "instance_id": instance_id, "instance_state": state})
    return sorted(out, key=lambda r: r["tag"])


def list_stacks(profile: str = "", region: str = "") -> list[dict[str, Any]]:
    """List KiroCrew CloudFormation stacks by deterministic stack prefix."""
    profile = validate_profile(profile)
    region = validate_region(region)
    data = aws.checked_json(
        [
            "cloudformation",
            "list-stacks",
            "--stack-status-filter",
            *_DISCOVERABLE_STACK_STATES,
            "--output",
            "json",
        ],
        profile,
        region,
        action="cloudformation:ListStacks",
    )
    out: list[dict[str, Any]] = []
    summaries = data.get("StackSummaries", []) if isinstance(data, dict) else []
    for summary in summaries:
        name = str(summary.get("StackName", ""))
        if not name.startswith(STACK_PREFIX):
            continue
        tag = name[len(STACK_PREFIX) :]
        if not tag:
            continue
        out.append(
            {
                "tag": tag,
                "stack_name": name,
                "stack_status": str(summary.get("StackStatus", "")),
            }
        )
    return sorted(out, key=lambda r: r["tag"])


# --- pause / resume ---------------------------------------------------------


def _instance_id_for(tag: str, profile: str, region: str) -> str:
    st = describe(tag, profile, region)
    if not st.get("exists"):
        raise aws.AWSError(f"no KiroCrew instance found for tag '{tag}'")
    iid = st.get("instance_id", "")
    if not iid:
        raise aws.AWSError(f"instance for '{tag}' has no instance id (stack still creating?)")
    return iid


def stop(tag: str, profile: str = "", region: str = "") -> dict[str, Any]:
    """Stop the instance (pauses compute billing; EBS still bills)."""
    aws.assert_human_action("ec2:StopInstances")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    iid = _instance_id_for(tag, profile, region)
    aws.checked(
        ["ec2", "stop-instances", "--instance-ids", iid],
        profile,
        region,
        action="ec2:StopInstances",
    )
    return {"tag": tag, "instance_id": iid, "action": "stop"}


def start(tag: str, profile: str = "", region: str = "") -> dict[str, Any]:
    """Start a stopped instance."""
    aws.assert_human_action("ec2:StartInstances")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    iid = _instance_id_for(tag, profile, region)
    aws.checked(
        ["ec2", "start-instances", "--instance-ids", iid],
        profile,
        region,
        action="ec2:StartInstances",
    )
    return {"tag": tag, "instance_id": iid, "action": "start"}


# What to say when `start` fails on a stack that was launched with `--spot`.
# Kept as separate lines so each surface can render them its own way (the CLI as
# detail lines under the failure, the dashboard joined into the error body).
#
# This is the ONE Spot event every user eventually hits, and the raw AWS error
# ("only Amazon EC2 can restart an interrupted stopped Spot Instance", or worse,
# an opaque IncorrectSpotRequestState) reads like a broken box. The dangerous
# reading is the obvious one: "start is broken, so delete it and launch a new
# one" — and destroy takes the DeleteOnTermination root volume, i.e. ~/.kiro/crew,
# with it. The docs already call the failing start "the tell"; this says it at the
# moment the user is actually looking at it.
#
# Reword these freely. The dashboard has no rich channel for a failed start (the
# panel renders the error STRING), so `handlers_cloud` flattens the hint into it —
# but behind a NEWLINE, which is what the panel splits on. No sentence here is a
# marker, so nothing in this wording is load-bearing for that split.
SPOT_START_FAILURE_HINT: tuple[str, ...] = (
    "This crew was launched with --spot, so the most likely cause is an EC2 "
    "INTERRUPTION stop, not a broken instance.",
    "Only EC2 can restart an interruption-stopped Spot instance. It resumes on "
    "its own when Spot capacity comes back — there is nothing to fix here.",
    "Your data is intact: an interruption stops the instance, it does not "
    "terminate it, so the root volume (and ~/.kiro/crew on it) is untouched.",
    "Do NOT destroy the instance to 'fix' this — destroy deletes that volume and "
    "everything on it. Wait for the auto-resume, or check `kirocrew cloud status`.",
)


def spot_start_failure_hint(tag: str, profile: str = "", region: str = "") -> list[str]:
    """Lines to append to a FAILED ``start``, or ``[]`` when they don't apply.

    Call this ONLY on the failure path. It costs one ``describe-stacks``, and the
    happy path must not pay for it — ``start`` does look the stack up (via
    :func:`describe`), but it throws the ``Parameters`` away, and threading them
    out of the success path just to read them on the rare failure would make
    every successful start carry the plumbing for an error it didn't hit.

    Never raises, for any reason. It runs while a real error is already being
    reported, so a second failure here (throttling, a denied DescribeStacks, the
    foreign-stack guard in :func:`find_stack`) must not replace the error the
    user actually needs to see — it just means we say nothing extra.
    """
    try:
        if not stack_uses_spot(find_stack(tag, profile, region)):
            return []
    except Exception as exc:  # noqa: BLE001 - see docstring: advisory, never fatal
        logger.debug("could not read the Spot parameter for '%s' (%s)", tag, exc)
        return []
    return list(SPOT_START_FAILURE_HINT)


# --- destroy (full uninstall / remove from AWS) -----------------------------


def build_destroy_argv(tag: str) -> list[str]:
    """The exact ``delete-stack`` argv (also the dry-run output)."""
    return ["cloudformation", "delete-stack", "--stack-name", stack_name(tag)]


# Spot request states that are DONE and need no action. Everything else —
# ``open`` (awaiting capacity), ``active`` (has an instance), ``disabled`` (a
# persistent request whose instance is stopped) — can still produce an instance
# and must be swept. Excluding the terminal set rather than allow-listing the
# live one is deliberate: a state AWS adds later defaults to "sweep it" instead
# of silently leaking a billing instance.
_TERMINAL_SPOT_REQUEST_STATES = frozenset({"cancelled", "closed", "failed"})


def find_spot_requests(tag: str, profile: str = "", region: str = "") -> list[dict[str, str]]:
    """Live Spot requests we own for ``<tag>``, as ``{id, instance_id}``.

    ``instance_id`` is ``""`` for an ``open`` request that has no instance yet.
    Empty for an on-demand stack (which never creates a request) — the call is
    one ``describe`` and costs nothing else.

    OWNERSHIP takes BOTH tags. This lookup feeds ``cancel_spot_requests``, which
    cancels what it finds and terminates the instances behind it, so it obeys the
    same rule as every other destructive path here (cf. :func:`find_stack`):
    ``kirocrew:managed=true`` asserts "Kiro Crew created this", and
    ``kirocrew:instance=<tag>`` narrows it to THIS tag. The instance tag alone is
    neither — it is a plain user tag anyone can set, and a foreign Spot request
    that happens to carry it (a hand-made one, a collision on a common tag like
    ``dev``) would be cancelled and its instance terminated by ``cloud destroy``.
    The launch template tags our requests with both keys (see the
    ``spot-instances-request`` TagSpecifications in ``kirocrew-ec2.yaml``), and
    the tag-gated ``ec2:CancelSpotInstanceRequests`` grant already keys on the
    managed tag, so filtering on both here is what makes the two agree.

    The state filter is applied CLIENT-side, not by the API: the documented
    values for the ``state`` filter are ``open|active|closed|cancelled|failed``,
    and ``disabled`` — the real state of a persistent request whose instance is
    stopped, and the one that most needs sweeping — is NOT among them. Filtering
    here on the returned ``State`` avoids relying on an undocumented filter
    value.
    """
    data = aws.checked_json(
        [
            "ec2",
            "describe-spot-instance-requests",
            "--filters",
            f"Name=tag:{MANAGED_TAG_KEY},Values=true",
            f"Name=tag:{INSTANCE_TAG_KEY},Values={tag}",
        ],
        profile,
        region,
        action="ec2:DescribeSpotInstanceRequests",
        timeout=_POLL_TIMEOUT,
    )
    requests = data.get("SpotInstanceRequests", []) if isinstance(data, dict) else []
    return [
        {"id": r["SpotInstanceRequestId"], "instance_id": r.get("InstanceId", "") or ""}
        for r in requests
        if r.get("SpotInstanceRequestId")
        and str(r.get("State", "")).lower() not in _TERMINAL_SPOT_REQUEST_STATES
    ]


# ``error_kind`` values for a FAILED Spot-request lookup. The cause decides how
# loud the caller is allowed to be, so it must not be flattened into a bare
# string. None of them is quiet on its OWN, though: what settles whether a
# missed lookup can be shrugged off is the STACK (see :func:`stack_uses_spot`),
# never an inference about the principal — the profile running destroy need not
# be the one that launched, so "this principal could not have created a --spot
# stack" is not a safe reason to reassure anybody.
#   ACCESS_DENIED — IAM said no. Nothing was learned. Harmless only when the
#     stack itself says Spot=false (it never created a request); on a Spot
#     stack it is a SWEEP FAILURE like any other unanswered lookup.
#   AGENT_SESSION — the in-process chokepoint refused (agent/ACP session). We
#     learned nothing, but nothing was mutated either; a human re-run sweeps.
#     Graded like the denial: a quiet note unless the stack is Spot=true.
#   FAILED — throttling, network, expired SSO, unparseable JSON… Always a SWEEP
#     FAILURE: warn, drop the "you won't be billed" claim, exit non-zero. It
#     stays loud even for a Spot=false stack, because a stack RE-deployed
#     without --spot can still carry a request left by its Spot generation.
SWEEP_ERROR_ACCESS_DENIED = "access_denied"
SWEEP_ERROR_AGENT_SESSION = "agent_session"
SWEEP_ERROR_FAILED = "failed"


def _sweep_error_kind(exc: aws.AWSError) -> str:
    """Classify a failed Spot-request lookup as an IAM denial or a real failure.

    ``missing_action`` is set only when :func:`aws.map_missing_action` parsed an
    ``is not authorized to perform: <action>`` out of stderr; a denial without
    that clause (``UnauthorizedOperation``, a bare ``AccessDenied``) still has
    the markers in the text, so the raw stderr AND the rendered message are both
    checked. Anything else — throttling, no network, expired SSO, unparseable
    JSON — is a genuine failure and must NOT borrow the denial's quiet path.
    """
    if exc.missing_action:
        return SWEEP_ERROR_ACCESS_DENIED
    if aws.is_access_denied(exc.stderr) or aws.is_access_denied(str(exc)):
        return SWEEP_ERROR_ACCESS_DENIED
    return SWEEP_ERROR_FAILED


def _empty_spot_sweep() -> dict[str, Any]:
    """A ``cancel_spot_requests`` outcome with nothing found and nothing failed."""
    return {
        "cancelled": [],
        "failed": [],
        "error": "",
        "error_kind": "",
        "terminated": [],
        "terminate_failed": [],
        "terminate_error": "",
    }


def probe_spot_requests(
    tag: str, profile: str = "", region: str = ""
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Read-only ``(requests, outcome)`` — the LOOKUP half of the sweep, alone.

    Mutates nothing, and never raises, for the same reasons
    :func:`cancel_spot_requests` doesn't (it is this function's error handling
    that makes that promise keepable). The outcome is an otherwise-empty sweep
    carrying ``error``/``error_kind`` when the lookup failed, so a caller can
    hand it straight to :func:`grade_spot_sweep` and report the failure in the
    identical words the cancelling path would have used; on success it is the
    clean ``_empty_spot_sweep()`` and the requests are in the first element.

    Exists because cancelling is DESTRUCTIVE in a way the verb hides: cancelling
    a ``disabled`` request (a persistent request whose instance is STOPPED)
    makes EC2 terminate that instance. So a surface that must show the user what
    it found and ask before touching it — ``kirocrew cloud destroy`` on a tag
    with no stack — needs to look WITHOUT sweeping. It re-runs the lookup inside
    the cancel afterwards, which is the honest ordering anyway: what gets
    cancelled is what is live when the user says yes, not what was live when the
    prompt was drawn.
    """
    outcome = _empty_spot_sweep()
    try:
        return find_spot_requests(tag, profile, region), outcome
    except aws.CloudActionDenied as exc:
        logger.warning("Spot-request sweep for '%s' refused from an agent session (%s)", tag, exc)
        outcome["error"] = str(exc)
        outcome["error_kind"] = SWEEP_ERROR_AGENT_SESSION
        return [], outcome
    except aws.AWSError as exc:
        logger.warning(
            "could not look up Spot requests for '%s' (%s) — if this was a --spot "
            "instance, check for a leftover request in the EC2 console",
            tag,
            exc,
        )
        outcome["error"] = str(exc)
        outcome["error_kind"] = _sweep_error_kind(exc)
        return [], outcome


def cancel_spot_requests(
    tag: str,
    profile: str = "",
    region: str = "",
    *,
    exclude_instance_id: str = "",
) -> dict[str, Any]:
    """Cancel this tag's live Spot requests (and terminate their instances).

    Returns an OUTCOME dict — ``{cancelled, failed, error, error_kind,
    terminated, terminate_failed, terminate_error}`` — never just the happy-path
    ids: the caller has to be able to tell "nothing to cancel" from "the cancel
    was denied", because only the second one means an instance may still be
    billing and must not be reported as a clean teardown. The described instance
    ids are NOT reported: what a caller can act on is what we FAILED to
    terminate (``terminate_failed``), not what we looked at.

    MUST run BEFORE ``delete-stack`` on a ``--spot`` stack. A *persistent* Spot
    request does not go away when its instance does: terminating a running OR
    stopped instance flips the request back to ``open`` and EC2 launches a
    REPLACEMENT instance — which, once the stack is gone, is an orphan nobody
    tracks and nobody stops billing for. Cancelling first removes that.

    Cancelling is NOT symmetric across states, which is why the terminate step
    exists: cancelling an ``active`` request leaves its RUNNING instance alive
    (so this can't pull the box out from under a stack delete — delete-stack
    still does that terminating), while cancelling a ``disabled`` request (a
    STOPPED instance) auto-terminates that instance — EC2's doing, not ours, and
    unavoidable if the request is to be cancelled at all. So for the orphan case
    — a replacement instance that is not a stack resource and that no
    delete-stack will ever touch — the cancel alone would leave a live, billing
    box behind. We terminate the described instances explicitly: a no-op for the
    stopped ones the cancel already killed.

    ``exclude_instance_id`` is an instance this call must NOT terminate itself
    (the state above still applies: a stopped one the cancel kills is EC2's
    doing). :func:`destroy` passes the STACK's own instance on the stack-exists
    path, because there the terminating is CloudFormation's job and doing it
    ourselves is not equivalent: if ``delete-stack`` is then refused (denied,
    throttled) we have destroyed the instance AND its DeleteOnTermination root
    volume out from under a stack that still exists — the user's ``~/.kiro``
    gone, with nothing deleted that they asked to have deleted. The orphan path
    (no stack) excludes nothing: there is no delete-stack coming for those.

    Never raises — for real, not just for :class:`aws.AWSError`. A user still on
    the PREVIOUS launcher policy has no ``ec2:DescribeSpotInstanceRequests`` /
    ``ec2:CancelSpotInstanceRequests``, and an on-demand stack (which is every
    stack that policy could have created) has nothing to cancel — so a denial
    must be REPORTED and let the teardown proceed, not block the uninstall path.
    :class:`aws.CloudActionDenied` (none of these three verbs is in the agent
    read allowlist) is caught for the same reason: the CLI's no-stack sweep is
    the one mutating entry point with no ``assert_human_action`` in front of it,
    and an agent-session ``cloud destroy`` on a missing stack must still end in a
    clean "nothing to remove", not a raw failure. Catching it on the DESCRIBE is
    sufficient to make the whole function total ONLY because
    ``ec2 describe-spot-instance-requests`` is absent from
    ``aws._AGENT_READ_ALLOWLIST``: an agent session is refused at the describe
    and never reaches the cancel/terminate calls. If that describe is ever added
    to the allowlist, those two mutating calls need their own catches to keep
    this contract.

    The failure CAUSE is reported as ``error_kind`` (see the constants above) —
    a denial is provably nothing-to-sweep, any other lookup failure is not, and
    only the caller that can tell them apart can decide whether "you won't be
    billed" is true.
    """
    found, outcome = probe_spot_requests(tag, profile, region)
    if outcome["error"] or not found:
        return outcome

    ids = [r["id"] for r in found]
    instance_ids = [
        r["instance_id"]
        for r in found
        if r["instance_id"] and r["instance_id"] != exclude_instance_id
    ]
    try:
        aws.checked(
            ["ec2", "cancel-spot-instance-requests", "--spot-instance-request-ids", *ids],
            profile,
            region,
            action="ec2:CancelSpotInstanceRequests",
            timeout=_POLL_TIMEOUT,
        )
    except aws.AWSError as exc:
        logger.warning("could not cancel Spot requests %s for '%s' (%s)", ids, tag, exc)
        outcome["failed"] = ids
        outcome["error"] = str(exc)
        return outcome
    outcome["cancelled"] = ids

    if not instance_ids:
        return outcome
    try:
        aws.checked(
            ["ec2", "terminate-instances", "--instance-ids", *instance_ids],
            profile,
            region,
            action="ec2:TerminateInstances",
            timeout=_POLL_TIMEOUT,
        )
    except aws.AWSError as exc:
        # The tag-gated ec2:TerminateInstances covers anything the template
        # created, but a REPLACEMENT instance EC2 launched off a re-opened
        # request may not carry the tags, so this can be denied. Report the ids
        # and the exact command rather than claiming the teardown is clean.
        logger.warning("could not terminate Spot instances %s for '%s' (%s)", instance_ids, tag, exc)
        outcome["terminate_failed"] = instance_ids
        outcome["terminate_error"] = str(exc)
        return outcome
    outcome["terminated"] = instance_ids
    return outcome


def spot_sweep_leaves_live_risk(sweep: dict[str, Any]) -> bool:
    """True when the sweep did not PROVE this tag's persistent request is gone.

    Either the cancel was refused (``failed``) or the lookup never answered
    (``error`` — denied, agent-guarded, throttled, offline). Both mean a
    persistent request may still be open, and an open persistent request turns
    the next terminate of its instance into a REPLACEMENT launch.

    A failed TERMINATE is deliberately not in here: it means the cancel
    succeeded, so the request is gone and cannot relaunch anything. That is
    leftover work to report (the box still bills until the user kills it), not a
    reason to leave the stack standing.
    """
    return bool(sweep.get("failed") or sweep.get("error"))


def grade_spot_sweep(
    sweep: dict[str, Any],
    tag: str,
    profile: str = "",
    region: str = "",
    *,
    stack_is_spot: Optional[bool] = None,
) -> dict[str, Any]:
    """Turn a sweep outcome into what to SAY about it and whether it FAILED.

    Returns ``{"failed": bool, "problems": [{"summary", "details"}], "notes":
    [str]}``. ``problems`` are the things that may still be billing — each one
    carries the leftover ids and the exact runnable ``aws`` command that
    finishes the job; ``notes`` are the quiet "nothing was learned but nothing
    can be live either" lines. A clean sweep yields all three empty, so a
    caller can attach the result unconditionally and stay silent when there is
    nothing to say.

    DETAIL ORDER IS PART OF THE CONTRACT: the raw AWS error (when there is one)
    comes first and the runnable ``aws`` remedy is ALWAYS the last detail. In a
    terminal that puts the actionable line where the eye lands; in the dashboard
    it is what makes the command recoverable, because those surfaces flatten
    ``summary + details`` into one string and the panel peels the trailing
    command back off it to render as copyable code. A remedy followed by more
    prose would drag that prose into the code block.

    Lives HERE, not in the CLI, because two surfaces must grade the same
    outcome identically: ``kirocrew cloud destroy`` (which exits non-zero) and
    the dashboard's destroy route (which returns the same warnings in its
    response). A stack destroyed from the dashboard leaks exactly as much money
    as one destroyed from a terminal, so the two must never drift.

    ``stack_is_spot`` is the stack's own ``Spot`` parameter (see
    :func:`stack_uses_spot`), or ``None`` when there is NO stack — the orphan
    case, where nothing can be proven either way and the wording has to admit
    it. It is what decides whether an unanswered lookup (IAM denial, agent
    guard) is a shrug or a failure; the identity of the destroying principal
    never is.
    """
    scope = ""
    if profile:
        scope += f" --profile {profile}"
    if region:
        scope += f" --region {region}"

    problems: list[dict[str, Any]] = []
    notes: list[str] = []

    failed = list(sweep.get("failed") or [])
    if failed:
        details = []
        if sweep.get("error"):
            details.append(str(sweep["error"]))
        details.append(
            "Cancel them yourself or EC2 keeps launching replacements: "
            "aws ec2 cancel-spot-instance-requests --spot-instance-request-ids "
            f"{' '.join(failed)}{scope}"
        )
        problems.append(
            {
                "summary": (
                    "Could NOT cancel this tag's persistent Spot request(s): "
                    f"{', '.join(failed)}"
                ),
                "details": details,
            }
        )

    terminate_failed = list(sweep.get("terminate_failed") or [])
    if terminate_failed:
        details = []
        if sweep.get("terminate_error"):
            details.append(str(sweep["terminate_error"]))
        details.append(
            "They keep billing until you do: aws ec2 terminate-instances --instance-ids "
            f"{' '.join(terminate_failed)}{scope}"
        )
        problems.append(
            {
                "summary": (
                    f"Could NOT terminate the Spot instance(s): {', '.join(terminate_failed)}"
                ),
                "details": details,
            }
        )

    kind = sweep.get("error_kind", "")
    if kind:
        # An unanswered lookup is only safe to shrug off when the STACK proves
        # there is nothing to find. A denial/agent-refusal on a stack we KNOW was
        # deployed with Spot=true is the false-reassurance case: delete-stack
        # terminates the instance, the persistent request flips open, and EC2
        # hands out a replacement nobody tracks.
        unresolved_is_harmless = (
            kind in (SWEEP_ERROR_ACCESS_DENIED, SWEEP_ERROR_AGENT_SESSION) and not stack_is_spot
        )
        if unresolved_is_harmless and kind == SWEEP_ERROR_ACCESS_DENIED:
            notes.append(
                "Could not check for a leftover Spot request (no permission) — "
                + (
                    "and with no stack left there is nothing to prove it either way; "
                    "check the EC2 console if this tag ever ran --spot."
                    if stack_is_spot is None
                    else "this stack is on-demand (Spot=false), so it never created one."
                )
            )
        elif unresolved_is_harmless:
            notes.append("Agent sessions can't sweep Spot requests — run destroy from a terminal.")
        else:
            if kind == SWEEP_ERROR_ACCESS_DENIED:
                summary = (
                    "Could NOT check for a leftover persistent Spot request — the "
                    "lookup was denied, and this stack was launched with --spot."
                )
            elif kind == SWEEP_ERROR_AGENT_SESSION:
                summary = (
                    "Could NOT check for a leftover persistent Spot request — an agent "
                    "session can't run the lookup, and this stack was launched with --spot."
                )
            else:
                summary = "Could NOT check for a leftover persistent Spot request — the lookup failed."
            details = []
            if sweep.get("error"):
                details.append(str(sweep["error"]))
            # Both tag filters, exactly as :func:`find_spot_requests` runs it: the
            # hand-check has to answer the same question the sweep would have
            # ("is there a request WE own for this tag?"), and the wider
            # instance-tag-only form can surface a foreign request the sweep would
            # never have touched — inviting the user to cancel someone else's.
            details.append(
                "Check it yourself: aws ec2 describe-spot-instance-requests --filters "
                f"Name=tag:{MANAGED_TAG_KEY},Values=true "
                f"Name=tag:{INSTANCE_TAG_KEY},Values={tag}{scope}"
            )
            problems.append({"summary": summary, "details": details})

    return {"failed": bool(problems), "problems": problems, "notes": notes}


def destroy(
    tag: str,
    profile: str = "",
    region: str = "",
    *,
    wait: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Full teardown: ``delete-stack`` removes every resource the launch created.

    This is the clean uninstall/remove-from-AWS path — instance, IAM role +
    instance profile, security group, and EBS volume all go away. Idempotent:
    deleting an already-gone stack is a no-op success.

    A ``--spot`` launch has ONE resource CloudFormation does not own: the
    persistent Spot *request*, which EC2 creates as a side effect of RunInstances
    and which outlives the instance. So teardown cancels it BEFORE
    ``delete-stack`` (see :func:`cancel_spot_requests`) — otherwise delete-stack's
    terminate would flip the request open and EC2 would launch a replacement
    instance the stack no longer tracks. It also terminates the instances those
    requests point at — EXCEPT the stack's own, which CloudFormation terminates
    as part of the delete that is about to run — because a replacement instance
    is NOT a stack resource and no ``delete-stack`` will ever touch it. For an
    on-demand stack it is one ``describe`` that finds nothing.

    If that sweep leaves live risk on a ``Spot=true`` stack — the cancel was
    refused, or the lookup never answered (see
    :func:`spot_sweep_leaves_live_risk`) — the ``delete-stack`` is NOT issued at
    all. Issuing it would terminate the instance while the persistent request is
    still open, which is precisely how EC2 hands out an untracked replacement.
    The result then reads ``destroyed: False``, ``aborted: True``, and the
    caller must report the sweep's problems and remedies and NOT claim the stack
    is gone (it is intact, including the root volume, and destroy is re-runnable
    once the request is cancelled). ``aborted`` is the whole branch — a failed
    sweep is the only thing that can stop the delete, so a reason code beside it
    would be a second spelling of it that no caller reads. An on-demand stack
    never aborts: with no request there is
    nothing that could relaunch, so its teardown is byte-identical to the
    pre-``--spot`` one.

    ORDER: ``find_stack`` runs FIRST, the sweep second. The reverse (what this
    did originally) means a throttled or denied ``cloudformation:DescribeStacks``
    aborts AFTER the request was cancelled and the instance terminated, leaving
    the caller told only "could not query stack" with no hint that the box is
    already gone. Nothing is lost by waiting: the no-stack orphan case — a rolled
    back ``--spot`` launch whose request outlived its stack — is swept by
    whichever surface saw the miss (:func:`kiro_crew.cli_cloud._cloud_destroy`
    on its "no stack found" early return, or the dashboard destroy route when
    this function reports ``already_absent``), not by this function itself.

    The sweep's full outcome is returned as ``spot_sweep`` — and ONLY as that:
    a happy-path shorthand for the cancelled ids would be a second spelling of
    ``spot_sweep["cancelled"]`` that a reader could consult without ever seeing
    the failure fields beside it, which is the reporting mistake this whole path
    exists to prevent. Callers read the outcome, so they can report a DENIED
    cancel/terminate instead of claiming the teardown left nothing billing.
    Alongside it, ``stack_is_spot`` reports the stack's own ``Spot`` parameter
    (free — ``describe-stacks`` already returned it): it is what lets the caller
    tell a denied lookup that PROVABLY had nothing to find from one that hid a
    live persistent request, without guessing from the destroying principal's
    permissions. Feed both to :func:`grade_spot_sweep`.

    ``dry_run`` returns the argv without calling AWS. When ``wait`` is true this
    blocks until the stack reaches ``DELETE_COMPLETE`` (or raises on
    ``DELETE_FAILED``).
    """
    if not dry_run:
        aws.assert_human_action("cloudformation:DeleteStack")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)

    argv = build_destroy_argv(tag)
    if dry_run:
        return {
            "tag": tag,
            "stack_name": stack_name(tag),
            "action": "destroy",
            "dry_run": True,
            "argv": argv,
        }

    # Look the stack up BEFORE mutating anything: if this query fails (throttled,
    # denied, expired SSO) we abort having cancelled nothing and terminated
    # nothing, so "could not query stack" is the whole truth.
    stack = find_stack(tag, profile, region)
    if not stack:
        return {
            "tag": tag,
            "stack_name": stack_name(tag),
            "action": "destroy",
            "destroyed": True,
            "already_absent": True,
            # Nothing swept HERE: the no-stack orphan case belongs to the
            # caller — the CLI sweeps on its own `describe` miss, the dashboard
            # destroy route sweeps when it sees this `already_absent` result.
            "spot_sweep": _empty_spot_sweep(),
            # No stack, so no Spot parameter to read. Harmless: the empty sweep
            # above has no error_kind, so there is nothing to grade either way.
            "stack_is_spot": False,
        }

    # Does this stack have a persistent Spot request at all? Read it from the
    # describe-stacks payload we already have (no extra call) — the sweep's
    # honesty depends on it when the lookup below cannot answer.
    is_spot = stack_uses_spot(stack)

    # The stack exists and is about to be deleted — cancel the persistent request
    # first so delete-stack's terminate can't flip it open into a replacement.
    # The stack's OWN instance is excluded from the sweep's terminate: while the
    # stack stands, terminating its instance is CloudFormation's job, and doing
    # it here would take the DeleteOnTermination root volume with it even if the
    # delete-stack below never gets accepted. The id is free — it is an output of
    # the describe-stacks payload find_stack already returned.
    sweep = cancel_spot_requests(
        tag, profile, region, exclude_instance_id=_outputs(stack).get("InstanceId", "")
    )

    if is_spot and spot_sweep_leaves_live_risk(sweep):
        # Deleting NOW is the billing zombie this whole path exists to prevent:
        # delete-stack terminates the instance, the persistent request we could
        # not cancel (or could not even look up) flips back to `open`, and EC2
        # launches a REPLACEMENT instance outside a stack that no longer exists —
        # untracked, unstoppable from here, billing. Leaving the stack up costs
        # the same instance-hours the user already has, keeps their disk, and
        # keeps `destroy` re-runnable once the request is gone. So: report,
        # don't delete. Reported (not raised) because the sweep outcome IS the
        # message — the caller renders its ids and runnable remedies, which an
        # exception string cannot carry.
        #
        # A lookup that was DENIED or agent-refused counts, deliberately: on a
        # Spot=true stack an unanswered lookup hides exactly the request that
        # zombies (grade_spot_sweep already grades it as a failure for the same
        # reason). On-demand stacks never reach this branch — nothing can zombie
        # without a request — so the quiet on-demand teardown is untouched, old
        # launcher policies included.
        logger.warning(
            "not deleting stack %s: its Spot request sweep left live risk (%s)",
            stack_name(tag),
            sweep.get("error") or ", ".join(sweep.get("failed") or []),
        )
        return {
            "tag": tag,
            "stack_name": stack_name(tag),
            "action": "destroy",
            "destroyed": False,
            "aborted": True,
            "spot_sweep": sweep,
            "stack_is_spot": is_spot,
        }

    aws.checked(argv, profile, region, action="cloudformation:DeleteStack", timeout=_POLL_TIMEOUT)
    if not wait:
        return {
            "tag": tag,
            "stack_name": stack_name(tag),
            "action": "destroy",
            "destroyed": True,
            "waited": False,
            "spot_sweep": sweep,
            "stack_is_spot": is_spot,
        }

    final = wait_for_delete(tag, profile, region)
    return {
        "tag": tag,
        "stack_name": stack_name(tag),
        "action": "destroy",
        "destroyed": final,
        "waited": True,
        "spot_sweep": sweep,
        "stack_is_spot": is_spot,
    }


def wait_for_delete(tag: str, profile: str = "", region: str = "") -> bool:
    """Block until the stack is gone. Returns True on DELETE_COMPLETE."""
    rc, _out, _err = aws.run_aws(
        ["cloudformation", "wait", "stack-delete-complete", "--stack-name", stack_name(tag)],
        profile,
        region,
        timeout=_DEPLOY_TIMEOUT,
    )
    # `wait` exits 0 when the stack reaches DELETE_COMPLETE (including "not found").
    return rc == 0

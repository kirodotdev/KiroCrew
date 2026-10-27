"""The drive bucket's identity and its key namespace.

The naming scheme and the tags discovery requires, the console sections and their key
prefixes with the key-length ceiling, and finding and creating the drive by tag. Key
validation itself, ``validate_key``, stays in the facade.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Optional

from kiro_crew.deploy import engine
from kiro_crew.deploy.engine import AWSError, _checked, _harden_bucket

BUCKET_PREFIX = "kirocrew-drive-"
#: The complete naming scheme new_bucket_name() produces: prefix + 12 hex
#: chars. Discovery requires a FULL match so a similarly-prefixed foreign
#: bucket can never be adopted as the drive.
_BUCKET_NAME_RE = re.compile(r"kirocrew-drive-[0-9a-f]{12}")
TAG_DRIVE = "kirocrew:drive"
#: One drive per account for now; the tag VALUE is reserved for a future
#: multi-drive world so discovery never has to change shape.
DRIVE_ID = "default"

#: Console section → key prefix. The section name is the API-level concept;
#: handlers map it here and a raw prefix never crosses the HTTP boundary.
SECTION_PREFIXES: dict[str, str] = {
    "library": "artifacts/",
    "drive": "drive/",
    "backup": "backup/",
}

_MAX_KEY_LEN = 900


def section_key(section: str, key: str) -> str:
    """The full object key for ``key`` inside ``section`` (validated)."""
    prefix = SECTION_PREFIXES[section]
    return f"{prefix}{key}"


def new_bucket_name() -> str:
    return f"{BUCKET_PREFIX}{secrets.token_hex(6)}"


# --- discovery (stateless-by-tag) ------------------------------------------


def searched_region(region: str) -> str:
    """The one region :func:`find_drive` searches when asked with ``region``."""
    return region or engine.DEFAULT_REGION


def no_drive_message(region: str) -> str:
    """What to tell the owner when :func:`find_drive` found no drive.

    The drive is one per account, but the tagging API answers for one region only,
    so "no drive" means "none in this region". Saying which region was searched is
    what lets an owner whose drive was created from a profile in another region see
    why it is missing, instead of creating a second one.
    """
    return (
        f"this account has no drive yet in {searched_region(region)}; only that region "
        "is searched, so a drive created from a profile in another region is not seen"
    )


def find_drive(profile: str, region: str, *, account: str) -> Optional[str]:
    """Resolve the account's drive bucket by tags, or None when absent.

    Same trust posture as deploy-web's ``find_site_by_tag``: both tags ANDed,
    naming scheme required, ambiguity fails loud.

    ``account`` is the identity the caller verified, and the bucket that comes
    back is checked against it before it is returned. Discovery goes through the
    tagging API with a PROFILE, and a profile is a name resolved by a child CLI
    process -- repointed from A to B it discovers B's bucket, and a request for
    ``/drive/A`` would then read and write B's drive without B's owner ever
    consenting. The tags cannot carry that binding (they are attacker-writable in
    the same way the config file is), so the binding is asserted against S3
    itself. Every drive route resolves its bucket through here, which is why one
    assertion at this choke point binds the whole surface.
    """
    out = _checked(
        [
            "resourcegroupstaggingapi",
            "get-resources",
            "--tag-filters",
            f"Key={TAG_DRIVE},Values={DRIVE_ID}",
            f"Key={engine.TAG_MANAGED},Values=true",
            "--resource-type-filters",
            "s3:bucket",
            "--region",
            searched_region(region),
            "--output",
            "json",
        ],
        profile,
        action="tag:GetResources",
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return None
    buckets: list[str] = []
    for mapping in data.get("ResourceTagMappingList", []):
        arn = mapping.get("ResourceARN", "")
        # Match the SERVICE, not the partition. An S3 bucket ARN is
        # ``arn:<partition>:s3:::<name>`` and the partition is not always
        # ``aws``: GovCloud tags come back as ``arn:aws-us-gov:s3:::...`` and
        # China as ``arn:aws-cn:s3:::...``. A hardcoded ``arn:aws:s3:::`` prefix
        # silently drops the drive we ourselves created on those partitions, so
        # the console reports no drive and a second confirm mints a second
        # billable bucket. Anchoring on ``:s3:::`` is partition-independent and
        # still rejects any other service's ARN.
        if ":s3:::" not in arn or not arn.startswith("arn:"):
            continue
        candidate = arn.split(":s3:::", 1)[1]
        # Full naming-scheme match, not a prefix: a bucket named
        # "kirocrew-drive-company-data" that somehow carries both tags must
        # not become the mutation target. Our names are always
        # BUCKET_PREFIX + token_hex(6) (see new_bucket_name).
        if _BUCKET_NAME_RE.fullmatch(candidate):
            buckets.append(candidate)
    if len(buckets) > 1:
        raise AWSError(
            f"ambiguous drive: {len(buckets)} buckets carry the drive tags — "
            "refusing to guess; remove the tag from the impostor"
        )
    if not buckets:
        return None
    # The tags said which bucket; S3 says whose it is. Only the second is
    # trustworthy, and it is asked BEFORE the name is handed to any caller.
    _assert_owned_by(buckets[0], profile, account)
    return buckets[0]


# --- creation ---------------------------------------------------------------


def _assert_owned_by(bucket: str, profile: str, account: str) -> None:
    """Refuse to continue unless ``bucket`` really is owned by ``account``.

    The caller verifies the account by probing the profile's live identity, but
    ``create-bucket`` then runs in a FRESH CLI process that resolves the profile
    itself against a config file any local writer can change. No amount of
    re-ordering closes that: the two resolutions are separate processes, so the
    only way to know which account the bucket landed in is to ask about the
    bucket.

    ``head-bucket --expected-bucket-owner`` is that question -- S3 answers 403
    when the bucket is not owned by the id passed in. Binding credentials
    instead (resolving the profile once and reusing the material for both calls)
    would make this app read credential material, which the names-only invariant
    forbids for exactly the reasons that invariant exists.

    On mismatch this raises WITHOUT deleting the bucket. Two reasons: a delete is
    a blind destructive call into an account we just failed to identify, and it
    is not needed for safety -- the discovery tags have not been written yet, so
    the bucket is not a drive, is never returned by discovery, and never receives
    a single object. What is left behind is an empty, untagged, unbilled bucket,
    named in the error so the owner can remove it deliberately.
    """
    rc, _out, err = engine.run_aws(
        [
            "s3api",
            "head-bucket",
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            account,
        ],
        profile,
        30,
    )
    if rc == 0:
        return
    # Ambiguity is treated exactly like mismatch. A throttle or a network blip
    # leaves us unable to say which account this is, and proceeding to tag it
    # would turn "unknown" into "this is your drive".
    # _trimmed_stderr, never a raw slice: it redacts BEFORE truncating. Cutting
    # first can split a credential across the boundary, and a half-token matches
    # no redactor pattern downstream, so the fragment would travel into this
    # response and the audit log looking harmless.
    raise AWSError(
        f"bucket {bucket} could not be confirmed to belong to account {account}; "
        f"refusing to use it. If it was just created it is empty and untagged, is "
        f"not a drive, and can be removed. ({engine._trimmed_stderr(err)})"
    )


def create_drive(profile: str, region: str, account: str) -> str:
    """Create + harden the drive bucket, versioning ON. Returns the name.

    Caller holds the confirm gate; by the time this runs a human has approved
    the resource. ``account`` is the identity the caller verified, and is
    re-checked against the bucket itself once it exists -- see
    :func:`_assert_owned_by`.

    Recovery-safe: if a prior attempt created the bucket but died before
    tagging, discovery misses it — acceptable at this stage because the opaque
    name never collides and hardening puts are idempotent.
    """
    bucket = new_bucket_name()
    create = ["s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    _checked(create, profile, action="s3:CreateBucket")
    # BEFORE anything makes this bucket usable or findable: confirm whose it is.
    _assert_owned_by(bucket, profile, account)
    # Versioning BEFORE the discovery tags (the drive's delta from
    # deploy-web): tags are what make the bucket discoverable, so everything
    # a discovered drive promises must already hold by the time they land.
    # A crash or missing permission here leaves an untagged bucket that
    # discovery never returns — an orphan to clean up, never a
    # half-configured drive that silently loses overwrite history.
    _checked(
        [
            "s3api",
            "put-bucket-versioning",
            "--bucket",
            bucket,
            "--versioning-configuration",
            "Status=Enabled",
        ],
        profile,
        action="s3:PutBucketVersioning",
    )
    _harden_bucket(
        bucket,
        profile,
        f"TagSet=[{{Key={engine.TAG_MANAGED},Value=true}},"
        f"{{Key={TAG_DRIVE},Value={DRIVE_ID}}}]",
    )
    return bucket

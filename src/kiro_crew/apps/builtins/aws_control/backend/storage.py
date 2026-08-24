"""Drive storage engine — one private bucket per account, three prefixes.

The bucket is the substrate for three console sections, each a view over one
key prefix: ``artifacts/`` (Library), ``drive/`` (Drive), ``backup/``
(Backup). One engine, one discipline, three views.

Everything routes through :func:`kiro_crew.deploy.engine.run_aws` — the AWS
CLI subprocess chokepoint (``--profile``, fixed argv, OS sandbox). No boto3,
no credential material, gateway-side only. The deploy engine's discipline is
inherited deliberately:

* **Stateless-by-tag discovery.** The bucket carries an opaque generated name
  (``kirocrew-drive-<12hex>``) and is found by tags, requiring BOTH
  ``kirocrew:managed=true`` AND ``kirocrew:drive=default``, plus the naming
  scheme — and multiple matches fail loud rather than last-match-wins,
  because discovery is a trust decision (delete/overwrite operate on what it
  returns).
* **Hardened at creation** via the deploy engine's own ``_harden_bucket``
  (BPA on, AES256 SSE, BucketOwnerEnforced), THEN versioning is enabled — the
  drive's deliberate delta from deploy-web. deploy-web keeps versioning off
  because its teardown empties with ``s3 rm`` (current versions only); the
  drive has no teardown surface in this PR, and artifact versions ↔ object
  versions is the point of the Library. A future destroy needs the
  version-aware purge the spec calls out.

CALLER CONTRACT (load-bearing): these functions do NOT check consent. Every
HTTP handler must gate with ``aws_consent.refuse_and_log(SERVICE_S3, ...)``
before calling in, and every mutating handler must run the two-call confirm
gate. The functions are sync (subprocess-bound) — call via
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from typing import Any, Optional

from kiro_crew.deploy import engine
from kiro_crew.deploy.engine import AWSError, _checked, _harden_bucket

logger = logging.getLogger(__name__)

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

#: SigV4's own ceiling. Real expiry can be SHORTER: a URL signed with
#: temporary credentials (SSO / assumed role) dies when that session ends.
#: The UI labels shares accordingly instead of promising the full window.
PRESIGN_MAX_SECS = 7 * 24 * 3600

#: Object keys are user-derived (file and folder names). One conservative
#: shape: printable segments joined by ``/``, no empty / dot / dot-dot
#: segment, no leading slash, bounded length. S3 allows far more; the drive
#: does not need to.
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+@=-]{0,254}$")
_MAX_KEY_LEN = 900


def validate_key(key: str) -> Optional[str]:
    """Return an error string when ``key`` is not a drive-shaped object key."""
    if not key or len(key) > _MAX_KEY_LEN:
        return "key must be 1-900 characters"
    if key.startswith("/") or key.endswith("/"):
        return "key must not start or end with '/'"
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            return "key must not contain empty, '.' or '..' segments"
        if not _KEY_SEGMENT_RE.match(segment):
            return (
                "key segments must start alphanumeric and use only letters, "
                "digits, spaces, and ._()+@=- (max 255 chars each)"
            )
    return None


def section_key(section: str, key: str) -> str:
    """The full object key for ``key`` inside ``section`` (validated)."""
    prefix = SECTION_PREFIXES[section]
    return f"{prefix}{key}"


def new_bucket_name() -> str:
    return f"{BUCKET_PREFIX}{secrets.token_hex(6)}"


# --- discovery (stateless-by-tag) ------------------------------------------


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
            region or engine.DEFAULT_REGION,
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


# --- object I/O -------------------------------------------------------------


def list_section(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    subpath: str = "",
    token: str = "",
    *,
    account: str,
) -> dict[str, Any]:
    """One '/'-delimited listing page under a section (folders + files)."""
    prefix = SECTION_PREFIXES[section] + (f"{subpath}/" if subpath else "")
    args = [
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        prefix,
        "--delimiter",
        "/",
        "--max-items",
        "500",
        "--expected-bucket-owner",
        account,
        "--output",
        "json",
    ]
    if token:
        args += ["--starting-token", token]
    out = _checked(args, profile, action="s3:ListBucket", timeout=60)
    data = json.loads(out or "{}")

    def _safe_name(name: str) -> str:
        # Object keys can be authored OUTSIDE this app (console uploads,
        # other tools): a key embedding a credential or beacon URL must not
        # reach the dashboard verbatim. Same double-pass discipline as every
        # other egress surface.
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        name, _ = redact_credentials(name)
        name, _ = redact_exfiltration_urls(name)
        return name

    files = [
        {
            "key": _safe_name(obj["Key"][len(SECTION_PREFIXES[section]) :]),
            "size": obj.get("Size", 0),
            "modified": obj.get("LastModified", ""),
        }
        for obj in data.get("Contents", [])
        if obj.get("Key", "") != prefix  # the folder placeholder itself
    ]
    folders = [
        _safe_name(cp["Prefix"][len(SECTION_PREFIXES[section]) :].rstrip("/"))
        for cp in data.get("CommonPrefixes", [])
    ]
    return {
        "files": files,
        "folders": folders,
        "nextToken": data.get("NextToken", ""),
    }


#: Ceiling for a single owner-pinned transfer. ``put-object`` is one request and
#: S3 rejects a body over 5 GiB; ``s3 cp`` would have split it into a multipart
#: upload, but no ``aws s3`` command accepts ``--expected-bucket-owner``, so a
#: transfer that cannot be owner-pinned is refused rather than sent unbound. The
#: drive's own upload cap is far below this; only a session archive could approach
#: it, and it is better for that to fail with a reason than to move unpinned.
_MAX_PINNED_TRANSFER_BYTES = 5 * 1024 * 1024 * 1024


def put_file(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    key: str,
    local_path: str,
    *,
    account: str,
    timeout: int = 600,
) -> None:
    """Upload one local file to ``section/key``, pinned to the bucket's owner.

    ``s3api put-object`` rather than ``s3 cp``: the high-level ``aws s3`` commands
    do not accept ``--expected-bucket-owner`` (checked against their own help
    output), and without it a transfer trusts only the bucket NAME. S3 bucket
    names are globally unique, so a name that becomes free -- our bucket deleted,
    by anyone who can -- can be re-created in another account, and a bucket policy
    there can allow the write. The upload would then succeed into a stranger's
    bucket carrying the owner's file. ``--expected-bucket-owner`` is what makes S3
    itself reject that, per request, whatever the policy says.
    """
    size = os.path.getsize(local_path)
    if size > _MAX_PINNED_TRANSFER_BYTES:
        raise AWSError(
            f"{size} bytes exceeds the {_MAX_PINNED_TRANSFER_BYTES}-byte limit for a "
            "single owner-pinned upload; refusing rather than transferring without "
            "the bucket-owner check"
        )
    _checked(
        [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, key),
            "--body",
            local_path,
            "--expected-bucket-owner",
            account,
        ],
        profile,
        action="s3:PutObject",
        timeout=timeout,
    )


def get_file(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    key: str,
    dest_path: str,
    *,
    account: str,
    timeout: int = 600,
) -> None:
    """Download ``section/key`` to a local path, pinned to the bucket's owner.

    Same reason as :func:`put_file`: a name-only transfer would read from whatever
    account currently holds that bucket name. On the read side the damage is
    inverted -- a restore would write a stranger's bytes into the owner's session
    directory -- so the same guard applies.
    """
    _checked(
        [
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, key),
            "--expected-bucket-owner",
            account,
            dest_path,
        ],
        profile,
        action="s3:GetObject",
        timeout=timeout,
    )


def delete_key(
    profile: str, region: str, bucket: str, section: str, key: str, *, account: str
) -> None:
    """Delete one object. On the versioned bucket this writes a delete marker,
    so 'deleted' is recoverable at the S3 layer until a purge exists."""
    _checked(
        [
            "s3api",
            "delete-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, key),
            "--expected-bucket-owner",
            account,
        ],
        profile,
        action="s3:DeleteObject",
    )


def object_exists(
    profile: str, region: str, bucket: str, section: str, key: str, *, account: str
) -> bool:
    """Whether ``section/key`` currently exists (head-object).

    Presigning is LOCAL signing — S3 is never consulted — so without this
    check a typo'd key would mint a working-looking URL that 404s for the
    recipient AND leave a phantom entry in the share ledger.
    """
    rc, _out, _err = engine.run_aws(
        [
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, key),
            "--expected-bucket-owner",
            account,
        ],
        profile,
        timeout=30,
    )
    return rc == 0


def presign(
    profile: str, region: str, bucket: str, section: str, key: str, expires_secs: int
) -> str:
    """A time-boxed share URL for one object.

    ``expires_secs`` is clamped to [60, PRESIGN_MAX_SECS]. The caller records
    the share in the ledger; this function only mints the URL.
    """
    expires = max(60, min(int(expires_secs), PRESIGN_MAX_SECS))
    out = _checked(
        [
            "s3",
            "presign",
            f"s3://{bucket}/{section_key(section, key)}",
            "--expires-in",
            str(expires),
            "--region",
            region or engine.DEFAULT_REGION,
        ],
        profile,
        action="s3:GetObject",
    )
    url = (out or "").strip()
    if not url.startswith("https://"):
        raise AWSError("presign returned no URL")
    return url


# --- usage ------------------------------------------------------------------


def usage(profile: str, region: str, bucket: str, *, account: str) -> dict[str, Any]:
    """Objects + bytes per section, by paginated listing.

    Listing the whole bucket is acceptable at drive scale (LIST is cheap and
    this is cached by the caller); CloudWatch storage metrics would need
    another permission grant for a day-old number.
    """
    per_section: dict[str, dict[str, int]] = {
        name: {"objects": 0, "bytes": 0} for name in SECTION_PREFIXES
    }
    out = _checked(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--output",
            "json",
            "--query",
            "Contents[].{Key: Key, Size: Size}",
            "--expected-bucket-owner",
            account,
        ],
        profile,
        action="s3:ListBucket",
        timeout=120,
    )
    try:
        rows = json.loads(out or "[]") or []
    except json.JSONDecodeError:
        rows = []
    for row in rows:
        key = row.get("Key", "")
        for name, prefix in SECTION_PREFIXES.items():
            if key.startswith(prefix):
                per_section[name]["objects"] += 1
                per_section[name]["bytes"] += int(row.get("Size", 0) or 0)
                break
    total_bytes = sum(s["bytes"] for s in per_section.values())
    total_objects = sum(s["objects"] for s in per_section.values())
    return {
        "bytes": total_bytes,
        "objects": total_objects,
        "sections": per_section,
    }

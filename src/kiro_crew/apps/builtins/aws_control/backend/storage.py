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
from urllib.parse import quote

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


def list_library_folders(profile: str, region: str, bucket: str, *, account: str) -> list[str]:
    """Every immediate folder name directly under ``artifacts/`` — RAW, unredacted.

    Singular rather than section-parameterized, unlike its object-I/O siblings.
    The Library is the only section with a local ledger to reconcile, so a
    ``section`` argument here would have exactly one reachable value; the prefix
    is anchored from ``SECTION_PREFIXES`` inside, which keeps the rule that a raw
    prefix never comes from a caller.

    Deliberately NOT :func:`list_section`. That one is a DISPLAY read: it runs
    every name through the egress redactors, which is right for a name rendered
    in the dashboard and wrong for an IDENTITY read. The Library reconcile
    compares these names against ledger KEYS, and a redacted name matches no
    key — so a reconcile fed the display listing could read a cloud copy that
    is present as absent, and drop a live ledger entry on that reading.

    Also deliberately without a page token. Omitting ``--max-items`` lets the
    CLI auto-paginate and applies ``--query`` to the MERGED result (the same
    property :func:`usage` relies on), so the answer is either the COMPLETE
    set of folders or a raised error — never a first page a caller could
    mistake for the whole prefix. Callers here reason about ABSENCE, and
    absence from a partial listing is not absence.

    For the same reason an unreadable response RAISES instead of degrading to
    an empty list, unlike :func:`usage`: empty means "nothing in the cloud",
    and a caller acting on that would discard every record it holds.
    """
    prefix = SECTION_PREFIXES["library"]
    out = _checked(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--delimiter",
            "/",
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
            "--query",
            "CommonPrefixes[].Prefix",
        ],
        profile,
        action="s3:ListBucket",
        timeout=60,
    )
    try:
        rows = json.loads(out or "[]") or []
    except json.JSONDecodeError:
        raise AWSError(
            "the folder listing returned a response that could not be read as JSON; "
            "refusing to report the section as empty"
        ) from None
    return [
        row[len(prefix) :].rstrip("/")
        for row in rows
        if isinstance(row, str) and row.startswith(prefix) and row[len(prefix) :].strip("/")
    ]


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


def copy_object(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    from_key: str,
    to_key: str,
    *,
    account: str,
    timeout: int = 600,
) -> None:
    """Server-side copy of ``section/from_key`` to ``section/to_key``.

    ``s3api copy-object`` rather than ``s3 cp`` for the same reason as
    :func:`put_file`: the high-level ``aws s3`` commands cannot carry the
    bucket-owner pin. Both ends are pinned — ``--expected-bucket-owner`` for
    the destination write and ``--expected-source-bucket-owner`` for the read
    — so a renamed bucket in a stranger's account can serve neither side.

    The copy source travels inside an HTTP header, so its key is URL-encoded
    here (``/`` kept as the separator); the destination ``--key`` is a plain
    request parameter and stays raw. Bytes never transit this host: S3 copies
    within the bucket, which is what makes copy-then-delete a safe move — the
    caller deletes the source only after this call returned without raising.
    """
    source = quote(f"{bucket}/{section_key(section, from_key)}", safe="/")
    _checked(
        [
            "s3api",
            "copy-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, to_key),
            "--copy-source",
            source,
            "--expected-bucket-owner",
            account,
            "--expected-source-bucket-owner",
            account,
        ],
        profile,
        action="s3:PutObject",
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


#: ``delete-objects`` accepts at most 1000 keys per request (a hard S3 API
#: limit, not a tunable). A folder with more objects than that MUST be paged,
#: so the constant is the batch size the caller walks the listing in — never an
#: assumption that one call clears the whole prefix.
_DELETE_BATCH_MAX = 1000

#: Byte ceiling for the serialized ``--delete`` document, which travels as ONE
#: argv element. Two limits bound it, and the tighter one wins:
#:
#:   * Linux caps a single argument at MAX_ARG_STRLEN (128 KiB).
#:   * Windows caps the WHOLE command line near 32 KiB (32767 chars) - and
#:     ``subprocess`` builds that line with ``list2cmdline``, which escapes every
#:     ``"`` as ``\"``. An S3 key may legitimately contain quotes (nothing stops
#:     another tool writing one), and a JSON document is quote-dense by
#:     construction, so a batch can DOUBLE on the way to CreateProcess.
#:
#: So the budget is set for the worst case rather than the typical one:
#: 12 KiB * 2 (every byte escaped) + roughly 300 bytes of fixed argv is about
#: 25 KiB, comfortably inside 32767. ``_WINDOWS_CMDLINE_MAX`` and the test that
#: multiplies these together keep the relationship honest if the cap is ever
#: raised - 1000 keys of 1024 chars would otherwise serialize to ~1 MB and fail
#: to spawn at all, which is an OSError and a 500 rather than a delete.
_DELETE_PAYLOAD_MAX_BYTES = 12 * 1024

#: The ceiling the budget above is derived from. Not a tunable: it is the
#: documented CreateProcess command-line limit.
_WINDOWS_CMDLINE_MAX = 32767


def _delete_batches(keys: list[str]) -> list[list[str]]:
    """Split ``keys`` into batches that fit BOTH S3's count cap and argv limits.

    Order is preserved and every key appears exactly once: a split that dropped
    or duplicated a key would under-delete (leaving objects behind) or make the
    reported count a lie. A single key that alone exceeds the budget still gets
    its own batch - refusing it here would silently skip an object the caller
    asked to remove, so the spawn is attempted and any failure surfaces.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    # {"Objects":[],"Quiet":true} plus the per-key {"Key":"..."} wrapper.
    overhead = len(json.dumps({"Objects": [], "Quiet": True}, separators=(",", ":")))
    size = overhead
    for key in keys:
        entry = len(json.dumps({"Key": key}, separators=(",", ":")).encode()) + 1
        too_big = size + entry > _DELETE_PAYLOAD_MAX_BYTES
        if current and (too_big or len(current) >= _DELETE_BATCH_MAX):
            batches.append(current)
            current, size = [], overhead
        current.append(key)
        size += entry
    if current:
        batches.append(current)
    return batches


def _raise_on_delete_errors(out: str) -> None:
    """Turn a per-key DeleteObjects failure into an ``AWSError``.

    ``delete-objects`` answers 200 with an ``Errors`` array when it could not
    remove some keys, so the CLI exits 0 and the caller would otherwise count
    them as deleted.

    An EMPTY body is success: with ``Quiet`` a fully successful call returns
    nothing. A non-empty body that will not parse is NOT success - the call site
    pins ``--output json``, so unparseable output means something unexpected
    happened, and on a destructive path the honest answer is to report failure
    rather than to assume the objects are gone.
    """
    if not (out or "").strip():
        return
    try:
        parsed = json.loads(out) or {}
    except json.JSONDecodeError:
        raise AWSError(
            "delete-objects returned a response that could not be read as JSON; "
            "refusing to report the folder as deleted"
        ) from None
    errors = parsed.get("Errors") or []
    if not errors:
        return
    first = errors[0] if isinstance(errors[0], dict) else {}
    code = first.get("Code", "unknown")
    key = first.get("Key", "?")
    raise AWSError(
        f"delete-objects could not remove {len(errors)} object(s) — "
        f"first: {key} ({code}); the folder is only partially deleted"
    )


def folder_placeholder_key(section: str, path: str) -> str:
    """The zero-byte object key that MAKES a folder exist.

    S3 has no directories: an empty folder is only ever a zero-byte object whose
    key ends in ``/``. The listing (:func:`list_section`) computes its page
    prefix as ``SECTION_PREFIXES[section] + f"{subpath}/"`` and drops the one
    object whose key EQUALS that prefix, treating it as the folder marker rather
    than a file. This function produces exactly that key -- ``section_key`` plus a
    trailing ``/`` -- so a folder created here is filtered out of ``files`` and
    surfaces as a ``folder`` instead. If this shape drifts from the listing's
    filter, a created folder would show up as a zero-byte FILE.
    """
    return f"{section_key(section, path)}/"


def create_folder(
    profile: str, region: str, bucket: str, section: str, path: str, *, account: str
) -> None:
    """Create an empty folder as its zero-byte, ``/``-terminated placeholder.

    ``path`` is a validated drive key (no trailing slash, no escape segment) --
    the ``/`` that makes it a folder is appended HERE via
    :func:`folder_placeholder_key`, never accepted from the caller, so the key
    shape the listing filters on cannot be spoofed into some other form.

    Owner-pinned like every other write: ``--expected-bucket-owner`` makes S3
    itself reject the put if the globally-unique bucket name is no longer this
    account's, the same reason :func:`put_file` cannot use ``s3 cp``. A body is
    deliberately omitted so the object is zero bytes.
    """
    _checked(
        [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            folder_placeholder_key(section, path),
            "--expected-bucket-owner",
            account,
        ],
        profile,
        action="s3:PutObject",
    )


def delete_prefix(
    profile: str, region: str, bucket: str, section: str, path: str, *, account: str
) -> int:
    """Delete every object under ``section/path/`` and return the count removed.

    A folder delete is a BLAST-RADIUS decision, so the prefix is constructed
    here, never taken raw: it is ``section_key(section, path)`` plus a trailing
    ``/``. The trailing slash is load-bearing -- deleting under ``drive/photos``
    (no slash) would also sweep a sibling ``drive/photos-backup/``; anchoring on
    ``drive/photos/`` confines the delete to the folder the caller named. The
    caller validates ``path`` with :func:`validate_key` first, which rejects an
    empty or ``/``-only value, so this can never be asked to delete a whole
    section (``drive/``) or the whole bucket.

    S3 caps ``delete-objects`` at 1000 keys per request, so this walks the folder
    in rounds: list one ``list-objects-v2`` window under the prefix
    (owner-pinned), batch-delete the keys it returned, then list AGAIN from the
    prefix -- deliberately WITHOUT ``--starting-token``.

    Resuming with a token would be wrong here. ``--max-items`` is CLIENT-side
    pagination: when the CLI truncates inside a server page it emits a composite
    token carrying an intra-page offset (``boto_truncate_amount``), and S3 is
    free to return a short page, so the CLI may fetch another to reach the
    requested count and truncate mid-page. Resuming then re-lists and skips N
    items -- but those N were just deleted, so the skip lands on SURVIVING keys,
    which are never removed while the call reports completion. Re-listing has no
    offset to get wrong: what was deleted is gone, so the next window starts at
    the next survivor. It is also memory-bounded, unlike collecting every key
    before deleting any.

    The walk therefore depends on each round removing what it listed, which the
    per-key error check guarantees. If a listing ever repeats without shrinking,
    the round made no progress and this raises rather than spinning.

    Each delete is owner-pinned for the same name-reuse reason as every other
    write. On the versioned bucket each removal is a delete MARKER, so 'deleted'
    is recoverable at the S3 layer until a version purge exists -- matching
    :func:`delete_key`.
    """
    full_prefix = f"{section_key(section, path)}/"
    removed = 0
    #: A round that lists keys must delete them, so the same first key twice means
    #: no progress. Two strikes rather than one: a concurrent writer re-creating a
    #: key is not by itself a stall.
    _MAX_STALLED_ROUNDS = 2
    stalled = 0
    last_first_key = ""
    while True:
        list_args = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            full_prefix,
            "--max-items",
            str(_DELETE_BATCH_MAX),
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
        ]
        out = _checked(list_args, profile, action="s3:ListBucket", timeout=60)
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            # A garbled listing page must not crash mid-delete and leave the
            # folder half-removed. Degrade to an empty page and stop the walk --
            # the same read-path tolerance usage() applies. Nothing further is
            # deleted, so a bad page can only UNDER-delete (safe), never over.
            break
        contents = data.get("Contents", []) or []
        keys = [obj["Key"] for obj in contents if obj.get("Key")]
        if not keys:
            # Nothing left under the prefix: the folder is gone.
            break
        for batch in _delete_batches(keys):
            # ``delete-objects`` takes a JSON document, passed as ONE argv element
            # (run_aws builds a fixed argv with no shell, so there is nothing to
            # quote-escape). That single element is why the page is split by
            # SERIALIZED SIZE and not only by S3's 1000-key cap: 1000 keys of up
            # to 1024 chars each serialize to ~1 MB, past the per-argument
            # ceiling on Linux (MAX_ARG_STRLEN, 128 KiB) and far past Windows'
            # whole-command-line limit, which would surface as an OSError and a
            # 500 rather than as a delete.
            payload = json.dumps(
                {"Objects": [{"Key": k} for k in batch], "Quiet": True},
                separators=(",", ":"),
            )
            out = _checked(
                [
                    "s3api",
                    "delete-objects",
                    "--bucket",
                    bucket,
                    "--delete",
                    payload,
                    "--expected-bucket-owner",
                    account,
                    # The error check below reads this response as JSON. Without
                    # pinning the format, a user's `output = text` (or yaml) in
                    # ~/.aws/config would make the body unparseable and turn that
                    # check into a no-op - a guard that works only on some
                    # machines is worse than no guard, because it reads as one.
                    "--output",
                    "json",
                ],
                profile,
                action="s3:DeleteObject",
            )
            # DeleteObjects reports per-key failures INSIDE a 200 response, so
            # the CLI exits 0 and _checked (which raises only on rc != 0) sees
            # success. Counting the batch here would tell the caller the folder
            # is gone while objects it could not touch are still in the bucket.
            # Quiet=True means a fully successful call returns an empty body, so
            # only a parsed, non-empty `Errors` is a failure.
            _raise_on_delete_errors(out)
            removed += len(batch)
        # No token: the next round lists the prefix again, where the keys just
        # removed are gone. A repeat of the same first key means the round made no
        # progress, so stop rather than spin.
        if keys[0] == last_first_key:
            stalled += 1
            if stalled >= _MAX_STALLED_ROUNDS:
                raise AWSError(
                    "folder delete made no progress: the listing keeps returning "
                    f"{keys[0]!r} after a delete that reported success"
                )
        else:
            stalled = 0
        last_first_key = keys[0]
    return removed


def object_exists(
    profile: str, region: str, bucket: str, section: str, key: str, *, account: str
) -> bool:
    """Whether ``section/key`` currently exists (head-object).

    Presigning is LOCAL signing — S3 is never consulted — so without this
    check a typo'd key would mint a working-looking URL that 404s for the
    recipient AND leave a phantom entry in the share ledger.

    Only a HEAD that S3 itself answered 404/NotFound reads as "absent".
    Any other failure — a timeout, a throttle, a credential lapse, an
    owner-pin 403 — RAISES instead of returning ``False``: the move handler
    treats ``False`` on the destination as permission to copy over that key,
    so folding a transient error into "absent" would turn one failed HEAD
    into an overwrite plus a source delete.
    """
    rc, _out, err = engine.run_aws(
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
    if rc == 0:
        return True
    # head-object reports a missing key as "(404)... Not Found" on stderr
    # (HEAD carries no body, so there is no NoSuchKey code to parse).
    text = err or ""
    if "(404)" in text or "Not Found" in text:
        return False
    raise AWSError(
        "head-object failed — cannot tell whether the key exists. "
        f"({engine._trimmed_stderr(err)})"
    )


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

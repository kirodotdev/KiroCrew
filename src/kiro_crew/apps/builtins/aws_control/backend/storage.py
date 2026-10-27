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
  versions is the point of the Library. Versioning also means a plain delete
  reclaims nothing: :func:`delete_key` and :func:`delete_prefix` write delete
  MARKERS and the bytes stay behind them as noncurrent versions, still billed.
  :func:`list_object_versions` and :func:`delete_object_versions` are the pair
  that erases bytes, and the backup retention sweep is their one caller; a
  future whole-drive destroy needs them too.

CALLER CONTRACT (load-bearing): these functions do NOT check consent. Every
HTTP handler must gate with ``aws_consent.refuse_and_log(SERVICE_S3, ...)``
before calling in, and every mutating handler must run the two-call confirm
gate. The functions are sync (subprocess-bound) — call via
``asyncio.to_thread``.
"""

# This module is the drive storage engine's facade and keeps its object operations:
# every name is reached as ``kiro_crew.apps.builtins.aws_control.backend.storage.<name>``.
# A read is served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`), so a
# test patches this module and never an owner. Owners never import this module. New
# work goes to the owner of its responsibility, or here for an object operation:
#
#   ``storage_drive`` -- the bucket's naming scheme and tags, the sections and their key
#       prefixes, and discovery and creation by tag
#   ``storage_staging`` -- the agent-masked staging root and the ranged read a preview
#       stages
#   ``storage_versions`` -- version ids, batched deletes, and the version listing and
#       erasure the retention sweep runs

from __future__ import annotations

import importlib as _importlib
import json
import logging
import mimetypes
import os
import re
import stat
import sys as _sys
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from kiro_crew import _facade_mirror, platform_compat
from kiro_crew.apps.builtins.aws_control.backend.storage_drive import (
    _MAX_KEY_LEN,
    SECTION_PREFIXES,
    section_key,
)
from kiro_crew.apps.builtins.aws_control.backend.storage_versions import (
    _DELETE_BATCH_MAX,
    _MAX_VERSION_ID_LEN,
    DeleteObjectsPartialFailure,
    PartialVersionDelete,
    _delete_batches,
    _raise_on_delete_errors,
    validate_version_id,
)
from kiro_crew.deploy import engine
from kiro_crew.deploy.engine import AWSError, _checked
from kiro_crew.security import (  # noqa: F401 - pinned by test_campaign_facades_base_public_names.py
    redact_credentials,
    redact_exfiltration_urls,
)

logger = logging.getLogger(__name__)


#: SigV4's own ceiling. Real expiry can be SHORTER: a URL signed with
#: temporary credentials (SSO / assumed role) dies when that session ends.
#: The UI labels shares accordingly instead of promising the full window.
PRESIGN_MAX_SECS = 7 * 24 * 3600

#: Object keys are user-derived (file and folder names). One conservative
#: shape: printable segments joined by ``/``, no empty / dot / dot-dot
#: segment, no leading slash, bounded length. S3 allows far more; the drive
#: does not need to.
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+@=-]{0,254}$")


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
        from kiro_crew.security import redact

        name = redact(name)
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


def list_object_keys(profile: str, region: str, bucket: str, *, account: str) -> set[str]:
    """Every object key in the drive — RAW, unredacted, complete or raised.

    The share ledger's rows name objects, and only the bucket can say whether
    one is still there. This is the read that answers it for a whole render at
    once: one listing, membership-tested per row.

    Deliberately NOT :func:`object_exists` per row, which is the obvious shape
    and the wrong one here. That function answers ``rc == 0``, so a throttle, a
    timeout, an expired session and a 404 are one answer. Collapsing them is
    correct where it lives — a mint refuses rather than signing a URL for an
    object it could not see — and is the opposite of correct on this path,
    where "could not see" would report a live share as broken. One listing that
    fails LOUDLY replaces up to ``shares._MAX_SHARES`` probes that cannot.

    The same two rules :func:`list_library_folders` states hold here, for the
    same reason — the caller reasons about ABSENCE, and absence from a partial
    listing is not absence:

    * No ``--max-items`` and no page token. The CLI auto-paginates and applies
      ``--query`` to the MERGED result, so the answer is the COMPLETE key set
      or an error, never a first page a caller could mistake for the drive.
    * An unreadable response RAISES instead of degrading to an empty set,
      unlike :func:`usage`. Empty means "the drive holds nothing", and a caller
      acting on that would mark every share it holds as pointing at nothing.

    Also deliberately NOT redacted, for the reason :func:`list_library_folders`
    gives: these keys are compared against LEDGER keys, and a redacted key
    matches none of them — so a share whose object is present would read as
    absent. Nothing here reaches the dashboard; only the membership answer does.

    The whole bucket rather than one section per call: a share row can name any
    shareable section, and one listing has one failure mode where several would
    have one each. Listing the whole bucket at drive scale is the cost
    :func:`usage` already accepts.
    """
    out = _checked(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
            "--query",
            "Contents[].Key",
        ],
        profile,
        action="s3:ListBucket",
        timeout=120,
    )
    try:
        rows = json.loads(out or "[]") or []
    except json.JSONDecodeError:
        raise AWSError(
            "the object listing returned a response that could not be read as JSON; "
            "refusing to report the drive as empty"
        ) from None
    return {row for row in rows if isinstance(row, str)}


#: Ceiling for a single owner-pinned transfer. ``put-object`` is one request and
#: S3 rejects a body over 5 GiB; ``s3 cp`` would have split it into a multipart
#: upload, but no ``aws s3`` command accepts ``--expected-bucket-owner``, so a
#: transfer that cannot be owner-pinned is refused rather than sent unbound. The
#: drive's own upload cap is far below this; only a session archive could approach
#: it, and it is better for that to fail with a reason than to move unpinned.
_MAX_PINNED_TRANSFER_BYTES = 5 * 1024 * 1024 * 1024

#: Content-Type prefixes the upload is allowed to declare. The preview dialog
#: renders these through a presigned URL in an ``<img>``/``<video>``/``<audio>``/
#: ``<iframe>``, and a browser only renders inline what the object's stored
#: Content-Type says it is -- with S3's ``binary/octet-stream`` default, a PDF
#: downloads instead of showing. Everything else stays on that default ON
#: PURPOSE: ``text/html`` and ``image/svg+xml`` would make a shared or downloaded
#: object render as a live document on the bucket origin, script included, when
#: the same file opened in-app goes through the text preview as inert bytes.
_INLINE_CONTENT_TYPE_PREFIXES = ("image/", "video/", "audio/")
_INLINE_CONTENT_TYPES = frozenset({"application/pdf"})
_INLINE_CONTENT_TYPE_DENY = frozenset({"image/svg+xml"})


def inline_content_type(key: str) -> str:
    """The Content-Type to store for ``key``, or ``""`` to keep S3's default.

    Guessed from the extension and then filtered to the inline-safe set above;
    a type outside it returns ``""`` rather than the guess, so an ``.html``
    upload is stored as an opaque blob exactly as it was before previews.
    """
    guessed, _ = mimetypes.guess_type(key)
    if not guessed or guessed in _INLINE_CONTENT_TYPE_DENY:
        return ""
    if guessed in _INLINE_CONTENT_TYPES or guessed.startswith(_INLINE_CONTENT_TYPE_PREFIXES):
        return guessed
    return ""


#: The body spelling that makes the CLI child read OUR descriptor instead of
#: re-resolving a name. ``/dev/stdin`` is a symlink to ``/proc/self/fd/0`` on
#: Linux and a character device with the same meaning on macOS, so the child
#: opens whatever descriptor it inherited as fd 0 -- here, the descriptor this
#: process opened and checked. The Linux sandbox is a user + mount namespace
#: that bind-mounts empty directories over the trees it hides (see
#: ``sandbox.wrap_argv``); it does not remount ``/proc`` or ``/dev``, and
#: Seatbelt remounts nothing, so the spelling survives both backends.
_DESCRIPTOR_BODY = "/dev/stdin"

#: Whether the CLI can be handed a descriptor rather than a name for its body.
#: Windows has no ``/dev/stdin``; that platform takes the pinned-name arm in
#: :func:`put_file`, which is sound there for a reason POSIX cannot borrow -- a
#: held directory handle blocks a rename of the directory and of every directory
#: above it, so the name cannot be re-pointed while we hold it.
_CAN_PASS_BODY_DESCRIPTOR = platform_compat.IS_POSIX


def _assert_uploadable_handle(fd: int) -> os.stat_result:
    """Refuse *fd* unless it is a regular file. The only check a HANDED-OVER fd gets.

    A caller that created its payload exclusively and has held the descriptor ever
    since has already established which inode this is, and holding it is what keeps
    that true: no rename, unlink or hard link can make a descriptor point somewhere
    else. So the link count says nothing here, and re-checking it would be actively
    wrong in both directions -- a same-UID process that merely UNLINKS the staging
    name leaves the held inode at zero links, and one that adds a second name
    leaves it at two, and in both cases the bytes about to be sent are still the
    ones the caller built and measured. Refusing them would let anyone who can
    write the staging directory cancel a scheduled backup by touching a name
    nothing reads any more.

    ``S_ISREG`` is kept because it is about the descriptor's own kind rather than
    about its names: a pipe or a character device handed here would make the upload
    send an unbounded stream under a size taken from ``fstat``, which is a
    correctness failure whatever its provenance.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise AWSError(
            "the upload body is not a regular file, so the bytes that would be sent are "
            "not this file's; refusing rather than uploading whatever it resolves to"
        )
    return info


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
    body_fd: int | None = None,
) -> str:
    """Upload one local file to ``section/key``, pinned to the bucket's owner.

    Returns the ``VersionId`` S3 assigned, or ``""`` when the response names none.
    A caller that does not care may ignore it; backup retention records it, because
    on a versioned bucket the version id is the only thing identifying WHICH bytes
    under a key an uploader wrote.

    **The bytes uploaded are one inode's, and it is the inode this function
    checked.** The backup ARCHIVE is staged in a directory a same-UID process can
    write, so on ``main`` a NAME handed to the CLI was resolved again: a size taken
    from one resolution, the CLI's ``--body`` open from another, and a caller's own
    fingerprint from a third were three answers about three moments, and a process
    that replaced the file between any two of them made the object carry bytes
    nothing checked -- off-host, unattended, with no recall. So the archive callers
    now open the file ONCE, check the descriptor, and hand it over as *body_fd*; the
    size, the fingerprints and the upload all read that one descriptor.

    *body_fd* lets a caller that has already opened and checked the payload hand
    that same descriptor over, so its own measurements and this upload describe
    one inode rather than two resolutions that agreed. It stays the caller's to
    close. The backup archive and the snapshot payload hand over a descriptor their
    producer pinned with :func:`kiro_crew...backup._open_pinned_archive_fd`
    (O_NOFOLLOW + S_ISREG + st_nlink == 1 + owner, plus a Windows deny-write open).
    Omitted, the body is the bare NAME -- the path the label sidecar, the library
    push and the drive spool take. Those bodies are held only by the name, not by a
    checked descriptor.

    ``s3api put-object`` rather than ``s3 cp``: the high-level ``aws s3`` commands
    do not accept ``--expected-bucket-owner`` (checked against their own help
    output), and without it a transfer trusts only the bucket NAME. S3 bucket
    names are globally unique, so a name that becomes free -- our bucket deleted,
    by anyone who can -- can be re-created in another account, and a bucket policy
    there can allow the write. The upload would then succeed into a stranger's
    bucket carrying the owner's file. ``--expected-bucket-owner`` is what makes S3
    itself reject that, per request, whatever the policy says.

    The stored Content-Type is guessed from the KEY's extension. Without it S3
    defaults to ``binary/octet-stream``, and a presigned URL then serves a PDF
    or a video as a forced download instead of rendering inline — the preview
    surface depends on the browser trusting this header. An extension
    ``mimetypes`` cannot place keeps the S3 default rather than guessing.
    """
    if body_fd is None:
        # No descriptor handed over: a bare-name body (the label sidecar, the library
        # push, the drive spool). The size is taken from the name and the name is the
        # CLI's ``--body``. These bodies are held only by the name; the descriptor
        # pin is used for the backup ARCHIVE, which the archive callers hand over via
        # ``body_fd``.
        size = os.path.getsize(local_path)
        if size > _MAX_PINNED_TRANSFER_BYTES:
            raise AWSError(
                f"{size} bytes exceeds the {_MAX_PINNED_TRANSFER_BYTES}-byte limit for a "
                "single owner-pinned upload; refusing rather than transferring without "
                "the bucket-owner check"
            )
        args = [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, key),
            "--body",
            local_path,
        ]
        content_type = inline_content_type(key)
        if content_type:
            args += ["--content-type", content_type]
        args += ["--expected-bucket-owner", account]
        args += ["--output", "json"]
        out = _checked(args, profile, action="s3:PutObject", timeout=timeout)
        return _put_version_id(out)

    # A descriptor handed over by the caller: the backup archive / snapshot payload,
    # pinned by its producer with ``_open_pinned_archive_fd`` (O_NOFOLLOW + S_ISREG +
    # st_nlink == 1 + owner, plus a Windows deny-write open). The size, the caller's
    # fingerprints and this upload all read THAT one descriptor, so no step
    # re-resolves the name. The content is NOT held unrewritable in place against a
    # same-UID writer for the whole transfer: the descriptor fixes which inode the
    # name reaches, not that its bytes cannot be rewritten under it.
    fd = body_fd
    _assert_uploadable_handle(fd)
    size = os.fstat(fd).st_size
    if size > _MAX_PINNED_TRANSFER_BYTES:
        raise AWSError(
            f"{size} bytes exceeds the {_MAX_PINNED_TRANSFER_BYTES}-byte limit for a "
            "single owner-pinned upload; refusing rather than transferring without "
            "the bucket-owner check"
        )
    if _CAN_PASS_BODY_DESCRIPTOR:
        # POSIX: hand the descriptor to the child as its stdin and spell ``--body``
        # as ``/dev/stdin``, so the CLI resolves no path at all. Set the position to
        # 0 first -- it arrives wherever the caller's last fingerprint read left it,
        # and an upload started mid-file would send a truncated object.
        body = _DESCRIPTOR_BODY
        stdin_fd: int | None = fd
        os.lseek(fd, 0, os.SEEK_SET)
    else:
        # Windows (no ``/dev/stdin``): the CLI re-resolves the NAME, held by the
        # caller's pinned directory (a pinned directory can be neither renamed nor
        # deleted) and the producer's deny-write open on the file. ``_assert_same_file``
        # below is the post-transfer inode backstop.
        body = local_path
        stdin_fd = None
    args = [
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        section_key(section, key),
        "--body",
        body,
    ]
    content_type = inline_content_type(key)
    if content_type:
        args += ["--content-type", content_type]
    args += ["--expected-bucket-owner", account]
    # `--output json` for the same reason the version delete pins it: the parse
    # below would otherwise become a no-op on a machine whose ~/.aws/config sets
    # `output = text`, and this caller needs the response, not just the exit code.
    args += ["--output", "json"]
    out = _checked(
        args,
        profile,
        action="s3:PutObject",
        timeout=timeout,
        stdin_fd=stdin_fd,
    )
    if not _CAN_PASS_BODY_DESCRIPTOR:
        _assert_same_file(fd, local_path)
    return _put_version_id(out)


def _assert_same_file(fd: int, local_path: str) -> None:
    """Refuse unless *local_path* still names the file *fd* holds.

    Only the pinned-name arm needs this, and it is a BACKSTOP rather than the
    protection: the deny-write guard taken before the transfer is what stops the
    bytes changing, and this says whether the NAME still reaches the same inode. It
    cannot do more than report -- the bytes are already in the bucket by the time it
    runs -- but reporting is worth having, because the alternative is recording a
    successful upload of bytes this process never read, and a restore would then
    hand those bytes back as the owner's own archive.

    Inode identity only, deliberately. An in-place content rewrite would pass this
    check, and that gap is closed by refusing the writer rather than by widening the
    comparison: a digest taken here would still be a digest taken after the object
    was sent.
    """
    held = os.fstat(fd)
    try:
        landed = os.stat(local_path)
    except OSError as exc:
        raise AWSError(
            f"the upload body could not be re-checked after the transfer: {exc.strerror}"
        ) from exc
    if (landed.st_dev, landed.st_ino) != (held.st_dev, held.st_ino):
        raise AWSError(
            "the upload body was replaced during the transfer, so the object now in the "
            "bucket may not be the file that was checked"
        )
    if landed.st_nlink != 1:
        raise AWSError("the upload body was linked elsewhere during the transfer")


def _put_version_id(out: str) -> str:
    """The ``VersionId`` a ``put-object`` response reports, or ``""``.

    Empty is a real answer rather than a failure: an unversioned bucket reports no
    version at all, and a response that will not parse cannot be claimed as one
    either. The upload has already succeeded by the time this runs, since
    ``_checked`` raises otherwise, so refusing here would fail a transfer that
    completed.

    What an empty answer COSTS is the caller's decision. Backup retention treats a
    key with no recorded version as one it must not retire, which is the
    fail-closed direction: the alternative is erasing bytes nothing proves are ours.
    """
    if not (out or "").strip():
        return ""
    try:
        parsed = json.loads(out) or {}
    except json.JSONDecodeError:
        return ""
    version = parsed.get("VersionId")
    # Bounded here as well as on the read path at the version listing, because the
    # comment on `_MAX_VERSION_ID_LEN` claims every retained variable-length field is
    # bounded and this one is retained: it reaches `_record_run` and is persisted in
    # `backup.json`. Over-long reads as ABSENT rather than being cut to fit, and the
    # paragraph above already says what absent costs -- retention will not retire a key
    # it has no version for, which is the fail-closed direction. A truncated id would
    # be worse than none: it names a different version, or no version at all, while
    # looking like proof of ownership.
    if not isinstance(version, str) or len(version) > _MAX_VERSION_ID_LEN:
        return ""
    return version


def get_file(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    key: str,
    dest_path: str,
    *,
    account: str,
    version: str = "",
    timeout: int = 600,
) -> None:
    """Download ``section/key`` to a local path, pinned to the bucket's owner.

    Same reason as :func:`put_file`: a name-only transfer would read from whatever
    account currently holds that bucket name. On the read side the damage is
    inverted -- a restore would write a stranger's bytes into the owner's session
    directory -- so the same guard applies.

    ``version`` pins the read to ONE stored version instead of whatever is current
    at that name. Empty -- the default, and what every pre-existing caller passes by
    saying nothing -- keeps the current-version read byte for byte, so this widens
    the primitive without moving any caller that does not ask.

    Naming a version is the read-side half of the argument :func:`put_file` makes
    about recording one. A key is a NAME, the drive is reachable by more than one
    install by design, and versioning is on for exactly that reason: a co-writer
    overwriting a recorded key leaves this install's bytes behind as a noncurrent
    version. Without this parameter those bytes are on the drive and no code path
    can ask for them, which is the gap this closes
    (``backup.restore_download``). The owner pin stays on the pinned read for the
    same reason it is on the unpinned one -- a version id is meaningless in the
    wrong account, and pinning the version is not a substitute for pinning who
    answers.

    The id is validated rather than trusted (:func:`validate_version_id`), and
    raises :class:`ValueError` rather than reaching the CLI: it travels as its own
    argv element after ``--version-id``, where a leading ``-`` would be read as
    another option. A caller holding an id it cannot vouch for should check it
    first and decide what to do, rather than letting this raise -- the restore path
    does, because for it an unusable recorded id is a refusal to report, not an
    error to surface.

    ``dest_path`` stays LAST in the argv: ``s3api get-object`` takes the output file
    positionally, so an option inserted after it would not be read as an option.
    """
    args = [
        "s3api",
        "get-object",
        "--bucket",
        bucket,
        "--key",
        section_key(section, key),
    ]
    if version:
        err = validate_version_id(version)
        if err:
            # Deliberately not folded into an AWSError: nothing has been asked of
            # AWS yet, and reporting a local state problem as a service failure
            # would send a reader to the wrong place.
            raise ValueError(f"refusing to fetch by version id: {err}")
        args += ["--version-id", version]
    args += [
        "--expected-bucket-owner",
        account,
        dest_path,
    ]
    _checked(
        args,
        profile,
        # A version-pinned GetObject is authorized against `s3:GetObjectVersion`,
        # a DIFFERENT action from `s3:GetObject`. `_checked` renders the action
        # name as the remediation hint on AccessDenied, so reporting the
        # unversioned one here sends the reader to add a permission they already
        # hold and be denied again.
        action="s3:GetObjectVersion" if version else "s3:GetObject",
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
    itself reject the put if the globally-unique bucket name is not this
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
    return head_object_meta(profile, region, bucket, section, key, account=account) is not None


def head_object_meta(
    profile: str, region: str, bucket: str, section: str, key: str, *, account: str
) -> Optional[dict[str, Any]]:
    """``head-object`` for ``section/key``: its metadata, or ``None`` when absent.

    The same HEAD :func:`object_exists` makes, with the response kept: the
    download path needs the stored ``ContentType`` so the dashboard can tell a
    real PDF from a ``.pdf``-named object uploaded before content types were
    set (those are served as octet-stream, which a sandboxed iframe can neither
    render nor download). Same absent/raise contract as ``object_exists``: only
    an S3 404 reads as ``None``; anything else raises.
    """
    rc, out, err = engine.run_aws(
        [
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            section_key(section, key),
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
        ],
        profile,
        timeout=30,
    )
    if rc == 0:
        try:
            meta = json.loads(out or "{}")
        except json.JSONDecodeError:
            meta = {}
        return meta if isinstance(meta, dict) else {}
    # head-object reports a missing key as "(404)... Not Found" on stderr
    # (HEAD carries no body, so there is no NoSuchKey code to parse).
    text = err or ""
    if "(404)" in text or "Not Found" in text:
        return None
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


# --- search -----------------------------------------------------------------

#: One listing window per round-trip. Same client-side pagination the drive's
#: other walks use; the token loop below is what lets a hit-heavy search stop
#: without listing the rest of the section.
_SEARCH_PAGE_ITEMS = 1000

#: How many hits a search hands back before it stops walking. Public because
#: the search route echoes it in the response and the dashboard interpolates
#: it into the "showing the first N" notice -- this constant is the ONLY place
#: the number lives, so changing it never strands a translation.
SEARCH_MAX_RESULTS = 200


def search_keys(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    query: str,
    *,
    account: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Case-insensitive filename search across one section's whole prefix.

    S3 has no server-side substring filter, so this pages ``list-objects-v2``
    under the section prefix and matches locally — against the ENTIRE
    section-relative key, not just the basename, so ``reports/2026`` finds a
    file by its folder as well as its name. Folder placeholders (keys ending
    in ``/``) are navigation structure, not files, and are skipped.

    Returns ``(results, capped)``. ``capped`` is True when a match BEYOND the
    :data:`SEARCH_MAX_RESULTS` cap was observed and the walk stopped EARLY —
    exactly the cap's worth of hits is a complete result set, not a truncated
    one. The remaining pages are never requested, which is what keeps a broad
    query on a large drive bounded.

    Matching runs on the RAW relative key; the key handed back is run through
    the same egress redactors as :func:`list_section`, because these names
    render in the dashboard and can be authored outside this app.
    """

    def _safe_name(name: str) -> str:
        from kiro_crew.security import redact

        name = redact(name)
        return name

    prefix = SECTION_PREFIXES[section]
    needle = query.lower()
    results: list[dict[str, Any]] = []
    token = ""
    while True:
        args = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--max-items",
            str(_SEARCH_PAGE_ITEMS),
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
        ]
        if token:
            args += ["--starting-token", token]
        out = _checked(args, profile, action="s3:ListBucket", timeout=60)
        data = json.loads(out or "{}")
        for obj in data.get("Contents", []) or []:
            key = obj.get("Key", "")
            rel = key[len(prefix) :]
            if not rel or rel.endswith("/"):
                continue
            if needle in rel.lower():
                # ``capped`` means "there were MORE than the cap", so it is
                # decided by the first match past the cap, not by the cap-th
                # one: exactly SEARCH_MAX_RESULTS hits is a complete result set
                # and must not be reported as truncated.
                if len(results) >= SEARCH_MAX_RESULTS:
                    return results, True
                results.append(
                    {
                        "key": _safe_name(rel),
                        "size": obj.get("Size", 0),
                        "modified": obj.get("LastModified", ""),
                    }
                )
        token = data.get("NextToken", "")
        if not token:
            return results, False


# The two partial-failure errors name this module as their home, so a traceback and a
# logged error type read the same whichever module raises them.
for _moved in (DeleteObjectsPartialFailure, PartialVersionDelete):
    _moved.__module__ = __name__
del _moved

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.aws_control.backend.storage_drive",
    "kiro_crew.apps.builtins.aws_control.backend.storage_staging",
    "kiro_crew.apps.builtins.aws_control.backend.storage_versions",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.aws_control.backend.storage_drive import (  # noqa: F401
        _BUCKET_NAME_RE,
        BUCKET_PREFIX,
        DRIVE_ID,
        TAG_DRIVE,
        _assert_owned_by,
        create_drive,
        find_drive,
        new_bucket_name,
        no_drive_message,
        searched_region,
    )
    from kiro_crew.apps.builtins.aws_control.backend.storage_staging import (  # noqa: F401
        _S3_INVALID_RANGE_CODE,
        _STAGING_READ_CHUNK,
        STAGING_DIR_LEAF,
        _preview_staging_parent,
        get_object_head_bytes,
        pinned_staging,
        staging_root,
    )
    from kiro_crew.apps.builtins.aws_control.backend.storage_versions import (  # noqa: F401
        _DELETE_PAYLOAD_MAX_BYTES,
        _MAX_MODIFIED_LEN,
        _VERSION_ID_RE,
        _VERSION_PAGE_ITEMS,
        _VERSION_ROWS_MAX,
        _WINDOWS_CMDLINE_MAX,
        _delete_entry_batches,
        delete_object_versions,
        list_object_versions,
    )


if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.config.paths import data_home  # noqa: F401
    from kiro_crew.deploy.engine import _harden_bucket  # noqa: F401
    from kiro_crew.platform_compat import is_link_or_junction  # noqa: F401
    from kiro_crew.sandbox import carveout_shadowed_by_foreign_mask  # noqa: F401
    from kiro_crew.sandbox import crew_home_visible_spellings  # noqa: F401
    from kiro_crew.sandbox import effective_sandbox_mode  # noqa: F401

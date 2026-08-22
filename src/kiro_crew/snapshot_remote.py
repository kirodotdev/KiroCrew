"""Off-host destination for snapshot bundles — a bucket set up once, then written to.

A snapshot written into ``<data home>/snapshots/`` lives inside the directory it is
protecting, so it does not survive losing the host. This module gives the bundle a
destination in the operator's own AWS account.

The shape matters more than the mechanics. Provisioning is a **one-time, explicit
act** (``kirocrew backup setup``) that creates the bucket, applies every at-rest
control, verifies them against the API's own answer, and records the result. Backing
up afterwards writes only to that recorded bucket.

That split is deliberate, and it replaced a design where every backup run tried to
decide, automatically, whether an arbitrary bucket was safe to write to. Proving that
is not a job code can finish: a tag says who *intended* a bucket for backup, a bucket
policy has to be parsed to learn who can read it, Block Public Access does not
neutralise a CloudFront origin grant, and each of those was a separate hole. Creating
the bucket ourselves and refusing everything else answers the question once instead.

What remains in the write path is one guarantee, and S3 enforces it rather than this
code: every object write carries ``--expected-bucket-owner``, so a bucket that is no
longer ours — deleted and re-created by someone else under the same name — fails the
write instead of receiving it.

No new credential surface: every AWS call goes through the deploy module's single
``aws`` CLI chokepoint, which resolves credentials from a profile *name*. Kiro Crew
never reads ``~/.aws`` and never holds a key, and ``boto3`` stays optional.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.deploy import engine
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.sel import SecurityEvent, sel

# Marks a bucket this feature created. Distinct from deploy's ``kirocrew:managed``,
# which keys its teardown and its scheduled reaper — a backup bucket carrying that tag
# could be deleted by them.
TAG_BACKUP = "kirocrew:backup"

BUCKET_PREFIX = "kirocrew-backup-"

# Bucket naming rules, narrowed: lowercase, no dots (dots break virtual-host style
# TLS), 3-63 chars. Narrower than S3 accepts on purpose — this value reaches subprocess
# argv, and a rejected legal-but-odd name costs the operator one rename.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9!_.*'()/-]{0,512}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")
_HOSTID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# Floor transfer rate assumed when sizing an upload timeout, in bytes per second.
# 1 MB/s is deliberately pessimistic: the 30s default that suits a control-plane call
# would kill a multi-hundred-megabyte upload on a home connection, and a killed upload
# looks like a failed backup.
_ASSUMED_FLOOR_BYTES_PER_SEC = 1_000_000
_TIMEOUT_FLOOR_SECS = 120

# A single PutObject is capped at 5 GiB by S3. Bundles are far smaller than that today
# (memory is ~20 MB, a full one under half a gigabyte), and put-object is what carries
# --expected-bucket-owner, which `aws s3 cp` does not support.
_MAX_SINGLE_PUT_BYTES = 5 * 1024**3

# Transient failures are retried; the transfer is idempotent (same key, same bytes).
_UPLOAD_ATTEMPTS = 3
_RETRY_SLEEP_SECS = 10

# How long old bundle versions are kept once versioning is on. Versioning is what makes
# an accidental or malicious overwrite recoverable, and the lifecycle rule is what stops
# that history growing without bound.
NONCURRENT_RETENTION_DAYS = 30
# What the noncurrent rule does and does not bound, stated here so the next reader does
# not repeat the mistake. Each run writes a NEW timestamped key, so bundles are all
# current versions and essentially never become noncurrent — the rule therefore bounds
# the history of an OVERWRITTEN key (a same-second re-run, a corrupt re-upload), which
# is exactly the case versioning exists to make recoverable. It does NOT bound the
# number of bundles: that grows by one per run, and nothing here deletes it.
#
# Automatic expiry of CURRENT bundles is deliberately not configured. A lifecycle rule
# cannot express "keep the newest N", only "delete older than N days", so on a host that
# stopped backing up — the dead machine this feature exists for — such a rule would
# delete the last surviving copy of its memory precisely when it is needed. Bounded
# growth is not worth that: at M1 sizes a daily bundle is a few gigabytes a year.
# Operators who want remote pruning can add their own rule; `backup status` reports the
# count so it is visible rather than silent.


class DestinationError(Exception):
    """The destination is unusable, and no AWS call should be attempted."""


class DestinationNotConfigured(DestinationError):
    """No destination has been set up yet."""


# Every exception class the AWS path can surface, so a caller has one thing to catch and
# cannot leak a traceback out of a CLI command. ``engine.AWSError`` comes from the deploy
# chokepoint's own ``_checked``; ``SandboxUnavailableError`` is raised by ``wrap_argv`` on
# a host with no OS-level sandbox backend (a real configuration, not a bug);
# ``OSError``/``SubprocessError`` cover a missing or unrunnable ``aws``.
UPLOAD_FAILURES: tuple[type[BaseException], ...] = (
    DestinationError,
    engine.AWSError,
    SandboxUnavailableError,
    OSError,
    subprocess.SubprocessError,
)


def _machine_fingerprint() -> str:
    """A stable per-machine value, hashed, so two hosts cannot share a backup prefix.

    The hostname alone is not an identifier. Two machines built from one image, or two
    default installs, routinely answer the same name — and identical name plus identical
    bundle filename is an identical S3 key, where versioning turns the loser into a
    noncurrent version that the lifecycle rule eventually expires. A machine's backup
    disappearing because another machine shares its name is exactly the loss this feature
    exists to prevent.

    Sources in order of preference, all hashed rather than published: the OS machine id,
    then a random value persisted next to the destination record. Only the first 8 hex
    characters are used — enough that a collision needs ~2^32 hosts, while keeping the key
    readable — and the raw id never reaches the key, because the prefix is visible to
    anyone who can list the bucket.
    """
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            raw = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw:
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    # No OS id (macOS, Windows, a container without one): persist our own. It lives beside
    # the destination record so it shares that directory's protection.
    own = _destination_dir() / "host-fingerprint"
    try:
        raw = own.read_text(encoding="utf-8").strip()
        if raw:
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    except OSError:
        pass
    generated = secrets.token_hex(16)
    try:
        platform_compat.make_owner_only_dir(own.parent)
        own.write_text(generated, encoding="utf-8")
        platform_compat.restrict_to_owner(own)
    except OSError:
        # Unwritable home: still return a value so the backup can proceed. It is not
        # stable across runs, which costs a tidy prefix, not correctness — a fresh prefix
        # separates hosts just as well as a remembered one.
        pass
    return hashlib.sha256(generated.encode("utf-8")).hexdigest()[:8]


def host_id() -> str:
    """Short, filesystem-and-S3-safe identifier for this machine.

    Backups are namespaced per host so several machines can share one bucket without
    interleaving, and so a restore can name which machine's backup it wants.

    The hostname leads because an operator recovering a dead machine has to recognise
    which prefix is theirs; the fingerprint that follows is what makes the prefix actually
    unique. A restore can still address any prefix explicitly, so a machine whose
    fingerprint changes does not lose access to its earlier backups.
    """
    raw = socket.gethostname().split(".")[0].lower()
    cleaned = re.sub(r"[^a-z0-9._-]", "-", raw).strip("-") or "host"
    return f"{cleaned[:54]}-{_machine_fingerprint()}"


@dataclass(frozen=True)
class Destination:
    """A bucket this feature created, and the account that owns it."""

    bucket: str
    region: str
    account: str
    created_at: str

    def key_for(self, filename: str, hostid: str | None = None) -> str:
        return f"backups/{hostid or host_id()}/{filename}"

    def url_for(self, filename: str, hostid: str | None = None) -> str:
        return f"s3://{self.bucket}/{self.key_for(filename, hostid)}"

    def prefix_url(self, hostid: str | None = None) -> str:
        return f"s3://{self.bucket}/backups/{hostid or host_id()}/"


def _destination_dir() -> Path:
    """The keystone directory holding the destination record and its authorization.

    Classified sensitive as a DIRECTORY, not just by its leaf file, so nothing an agent
    can drive is able to write anything in here.
    """
    return config_dir() / "backup"


def _registry_path() -> Path:
    return _destination_dir() / "destination.json"


def load_destination() -> Destination:
    """Return the configured destination, or raise :class:`DestinationNotConfigured`.

    Refusing here is the whole point of the split: a backup must not invent a
    destination, and it must not adopt one it did not create.
    """
    path = _registry_path()
    if not path.is_file():
        raise DestinationNotConfigured(
            "no backup destination is configured. Run `kirocrew backup setup` once — it "
            "creates a private, encrypted, versioned bucket in your own AWS account and "
            "records it here."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise DestinationError(f"{path} is unreadable ({e}); re-run `kirocrew backup setup`") from e
    try:
        dest = Destination(
            bucket=str(raw["bucket"]),
            region=str(raw["region"]),
            account=str(raw["account"]),
            created_at=str(raw.get("created_at", "")),
        )
    except (KeyError, TypeError) as e:
        raise DestinationError(f"{path} is missing {e}; re-run `kirocrew backup setup`") from e
    # Validate on the way OUT as well as in: these values reach subprocess argv, and the
    # file is on disk where anything could have edited it.
    if not _BUCKET_RE.match(dest.bucket):
        raise DestinationError(f"recorded bucket name is invalid: {dest.bucket!r}")
    if not _REGION_RE.match(dest.region):
        raise DestinationError(f"recorded region is invalid: {dest.region!r}")
    if not dest.account.isdigit() or len(dest.account) != 12:
        raise DestinationError(f"recorded account id is invalid: {dest.account!r}")
    return dest


def _save_destination(dest: Destination) -> Path:
    """Record the destination atomically.

    A torn write here is not a cosmetic problem: the record IS what every scheduled
    `--to-s3` reads, so a truncated file turns every future backup into a refusal —
    silently, on a machine nobody is watching, which is exactly the state this feature
    exists to avoid. So the bytes land in a sibling temporary file, are flushed to the
    platter, and are moved into place with ``os.replace``, which is atomic: a reader sees
    either the whole old record or the whole new one, never a partial one.
    """
    path = _registry_path()
    # `make_owner_only_dir`, not `restrict_to_owner`: the latter applies 0600, which is
    # right for a secret-bearing file and wrong for a directory — it strips the execute
    # bit and makes the directory untraversable, so the very write below fails. The
    # directory needs protecting because `os.chmod` is a POSIX-only guarantee: on Windows
    # it does not touch the DACL, so the directory and file inherited whatever the data
    # home allowed. On a machine with more than one OS account that leaves another user
    # able to replace this record — the trust anchor every later `--to-s3` reads to
    # decide where memory goes.
    platform_compat.make_owner_only_dir(path.parent)
    payload = (
        json.dumps(
            {
                "bucket": dest.bucket,
                "region": dest.region,
                "account": dest.account,
                "created_at": dest.created_at,
            },
            indent=2,
        )
        + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".destination-", suffix=".json", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Locked down BEFORE the rename, so the record is never momentarily readable
        # under inherited permissions at its final name.
        platform_compat.restrict_to_owner(str(tmp))
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # Also flush the directory entry, so the rename itself survives a power loss.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return path
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
    return path


def caller_account(profile: str) -> str:
    """This identity's AWS account id. Raises when it cannot be determined."""
    code, out, err = engine.run_aws(
        ["sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        profile,
        timeout=30,
    )
    account = out.strip()
    if code != 0 or not account.isdigit():
        hint = engine.map_access_denied(err)
        raise DestinationError(
            f"could not determine the AWS account for profile {profile!r}: "
            f"{hint or err.strip()[:200] or 'unknown error'}"
        )
    return account


def default_bucket_name(account: str, region: str) -> str:
    """A stable name, so `setup` run twice finds the same bucket.

    Deliberately built from the account and region only. S3 bucket names live in one
    globally-unique namespace that anyone can probe for existence, so a name is the
    wrong place to put the operator's identity: including the OS username would
    publish "this person uses Kiro Crew, and here is their account" to anyone willing
    to guess, and it buys nothing, because the account ID already makes the name
    unique.

    The region is included because a bucket is regional while its name is global:
    without it, running `setup` in a second region collides with the first bucket
    instead of creating a new one. (Host identity does appear in the *key* prefix --
    see `host_id` -- because keys are inside a bucket that is not publicly listable,
    and an operator restoring a backup needs to recognise which machine it came from.)
    """
    reg = re.sub(r"[^a-z0-9-]", "-", region.lower()).strip("-") or "region"
    name = f"{BUCKET_PREFIX}{account}-{reg}"
    return name[:63].rstrip("-")


def bucket_exists(bucket: str, profile: str, account: str) -> bool:
    """True when the bucket exists AND is owned by *account*.

    ``--expected-bucket-owner`` makes S3 answer the ownership question, so a bucket
    that was deleted and re-created by someone else under the same name reads as
    absent rather than as ours.
    """
    code, _out, _err = engine.run_aws(
        ["s3api", "head-bucket", "--bucket", bucket, "--expected-bucket-owner", account],
        profile,
        timeout=30,
    )
    return code == 0


def verify_bucket_private(bucket: str, profile: str) -> dict[str, object]:
    """Read back the controls that matter and return what AWS actually reports.

    Asserted from the API's answer rather than inferred from the put having succeeded,
    because "we called put-public-access-block" and "public access is blocked" are
    different claims.
    """
    result: dict[str, object] = {
        "block_public_access": {},
        "sse": None,
        "versioning": None,
        "ownership": None,
    }
    code, out, _err = engine.run_aws(
        ["s3api", "get-public-access-block", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    if code == 0:
        try:
            result["block_public_access"] = json.loads(out).get(
                "PublicAccessBlockConfiguration", {}
            )
        except ValueError:
            pass
    code, out, _err = engine.run_aws(
        ["s3api", "get-bucket-encryption", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    if code == 0:
        try:
            rules = json.loads(out).get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if rules:
                result["sse"] = (
                    rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
                )
        except (ValueError, AttributeError, IndexError):
            pass
    code, out, _err = engine.run_aws(
        ["s3api", "get-bucket-versioning", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    if code == 0:
        try:
            result["versioning"] = json.loads(out or "{}").get("Status")
        except ValueError:
            pass
    # Ownership is read because hardening SETS it. A control that is applied but never
    # verified is one an operator can remove without this reporting anything, which
    # defeats the point of asserting from the API's answer rather than from the put
    # having succeeded. BucketOwnerEnforced is what disables ACLs, so without it an
    # object ACL can grant read to another principal while all four BPA flags still
    # report clean.
    code, out, _err = engine.run_aws(
        ["s3api", "get-bucket-ownership-controls", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    if code == 0:
        try:
            rules = json.loads(out or "{}").get("OwnershipControls", {}).get("Rules", [])
            if rules:
                result["ownership"] = rules[0].get("ObjectOwnership")
        except (ValueError, AttributeError, IndexError):
            pass
    return result


def authorization_token_path() -> Path:
    """Where the operator authorizes `backup setup`.

    Sits inside the same directory as the recorded destination, which is classified
    sensitive — so the agent's file tools refuse it and any shell command naming the
    path is refused too. Those are policy layers over the agent's tools, not an OS
    boundary: neither inspects the body of a program, so creating this file is an act
    that records the operator's CHOICE of destination rather than one an agent is
    unable to perform. It is still worth requiring, because it makes a
    silently-provisioned destination take deliberate circumvention instead of being the
    default outcome. A terminal check would not even do that much — a pty is
    allocatable by anyone.
    """
    return _destination_dir() / "setup-authorized"


def _recorded_bucket() -> str | None:
    """The already-recorded destination bucket, or None when setup has not run.

    Deliberately tolerant: a missing or unreadable record simply means "no recorded
    destination", which makes the caller apply the stricter path rather than the
    looser one.
    """
    try:
        return load_destination().bucket
    except (DestinationNotConfigured, DestinationError):
        return None


def bucket_is_one_of_ours(bucket: str, profile: str) -> bool:
    """True only when the bucket demonstrably carries this feature's own tag.

    Fails closed: unreadable tags return False.

    Read the role of this check carefully, because an earlier revision used a tag
    for something it cannot do. A tag is **not** evidence that a bucket is safe to
    put secrets in -- the bucket owner can set any tag, so trusting one to mean
    "private" was the mistake that got removed. Here the tag answers a much smaller
    and answerable question: *did this code create this bucket?* It is a NECESSARY
    condition for reuse, never a sufficient one -- reuse additionally requires no
    bucket policy and a passing read-back of every at-rest control.

    Why reuse needs gating at all, beyond exposure. `setup` applies tagging, a
    versioning configuration and a lifecycle configuration, and
    ``put-bucket-lifecycle-configuration`` REPLACES the bucket's entire lifecycle
    config. Point ``--bucket`` at an unrelated bucket that happens to be
    policy-free and setup would silently discard that bucket's own rules and start
    expiring its noncurrent objects on our 30-day schedule. That is destroying
    someone's data to set up a backup, so a bucket we did not create is refused.
    """
    code, out, err = engine.run_aws(
        ["s3api", "get-bucket-tagging", "--bucket", bucket, "--output", "json"], profile, timeout=30
    )
    if code != 0:
        # NoSuchTagSet means "definitely not tagged", anything else means "cannot
        # tell". Both refuse; they are only distinguished for the error message.
        return False
    try:
        tags = json.loads(out or "{}").get("TagSet") or []
    except (ValueError, TypeError):
        return False
    return any(
        isinstance(t, dict) and t.get("Key") == TAG_BACKUP and t.get("Value") == "true"
        for t in tags
    )


POLICY_ABSENT = "absent"
POLICY_PRESENT = "present"
POLICY_UNKNOWN = "unknown"


def bucket_policy_state(bucket: str, profile: str) -> str:
    """Whether the bucket has a policy: ``absent``, ``present``, or ``unknown``.

    The three states are kept distinct because the two callers want different
    behaviour on ``unknown``, and collapsing them forces one of them to be wrong.
    ``setup`` runs once with a human present, so it treats ``unknown`` as a refusal.
    The upload path cannot: refusing to back up because an auxiliary API call failed
    turns a transient AWS error into "no off-host copy today", which is the outcome
    this whole feature exists to prevent.
    """
    code, out, err = engine.run_aws(
        ["s3api", "get-bucket-policy", "--bucket", bucket, "--output", "json"], profile, timeout=30
    )
    if code == 0:
        return POLICY_PRESENT if out.strip() else POLICY_ABSENT
    if "NoSuchBucketPolicy" in (err or ""):
        return POLICY_ABSENT
    return POLICY_UNKNOWN


def has_no_bucket_policy(bucket: str, profile: str) -> bool:
    """True only when the bucket demonstrably has NO bucket policy at all.

    Fails closed: an unreadable policy returns False, because "we could not tell"
    and "nobody else can read it" are not the same answer. Used by ``setup``, where
    a human is present to fix whatever made the answer unreadable.

    Why the test is *absence* rather than *analysis*. Block Public Access and
    ``BucketOwnerEnforced`` do not touch a bucket policy, so a bucket in our own
    account can be private to the internet and still grant read to another
    principal. Deciding whether a given policy is safe means parsing statements,
    resolving principals and conditions, and re-deciding whenever AWS adds a way
    to express access -- work that cannot be finished, which is the whole reason
    the destination is created rather than adopted. A bucket this code created has
    no policy, so requiring none is a decidable test that admits our own buckets
    (including one an earlier host set up, which is how several machines share a
    destination) and refuses every bucket whose exposure we would have to reason
    about.
    """
    return bucket_policy_state(bucket, profile) == POLICY_ABSENT


def is_fully_private(report: dict[str, object]) -> bool:
    """True only with all four BPA flags on, SSE and versioning set, and ACLs disabled.

    Ownership is part of the predicate because hardening sets it and BPA does not
    subsume it: ``BucketOwnerEnforced`` is what disables ACLs entirely, so without it
    an object ACL can still grant read to another principal while every BPA flag
    reports clean. Verifying only what BPA covers would let that removal pass.
    """
    bpa = report.get("block_public_access")
    if not isinstance(bpa, dict) or not bpa:
        return False
    flags = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
    if not all(bpa.get(f) is True for f in flags):
        return False
    if not report.get("sse"):
        return False
    if report.get("ownership") != "BucketOwnerEnforced":
        return False
    return report.get("versioning") == "Enabled"


LIFECYCLE_RULE_ID = "kirocrew-backup-expire-old-versions"


def _our_lifecycle_rule() -> dict:
    return {
        "ID": LIFECYCLE_RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": NONCURRENT_RETENTION_DAYS},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    }


def _put_lifecycle_rule(bucket: str, profile: str) -> None:
    """Install our rule while leaving every other rule on the bucket intact.

    ``put-bucket-lifecycle-configuration`` REPLACES the whole configuration, so the
    naive call silently discards rules this feature did not write. That is not a
    hypothetical: the user documentation tells operators that remote bundle count is
    unmanaged and that they may add their own expiry rule — and re-running `setup` is
    the documented way to repair a bucket. Following both instructions would have
    deleted the rule the first one suggested.

    So the existing configuration is read first, rules are matched by ID, and only ours
    is replaced or appended. An unreadable configuration is the one case where the whole
    set is written fresh: there is nothing to preserve, and `NoSuchLifecycleConfiguration`
    is exactly the "no rules yet" answer of a bucket we just created.
    """
    code, out, err = engine.run_aws(
        ["s3api", "get-bucket-lifecycle-configuration", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    existing: list[dict] = []
    if code == 0:
        try:
            rules = json.loads(out or "{}").get("Rules")
        except (ValueError, TypeError) as e:
            # Same fail-open trap as the tag read: treating an unparseable answer as
            # "no rules" would make the replacement below delete rules we never saw.
            raise DestinationError(
                f"the lifecycle configuration of {bucket!r} could not be parsed ({e}), "
                f"so applying ours would risk discarding rules already on the bucket. "
                f"Nothing was changed."
            ) from e
        if rules is not None and not isinstance(rules, list):
            raise DestinationError(
                f"the lifecycle configuration of {bucket!r} came back in an unexpected "
                f"shape ({type(rules).__name__}); refusing rather than replacing it."
            )
        existing = [r for r in (rules or []) if isinstance(r, dict)]
    elif "NoSuchLifecycleConfiguration" not in (err or ""):
        # Could not tell what is there. Refuse rather than overwrite an unknown set --
        # this runs during setup, where a human can act on the message.
        hint = engine.map_access_denied(err)
        raise DestinationError(
            f"could not read the existing lifecycle configuration of {bucket!r} "
            f"({hint or err.strip()[:160] or 'unknown error'}), so applying ours would "
            f"risk discarding rules already on the bucket. Nothing was changed."
        )

    kept = [r for r in existing if r.get("ID") != LIFECYCLE_RULE_ID]
    rules = kept + [_our_lifecycle_rule()]
    _checked_aws(
        [
            "s3api",
            "put-bucket-lifecycle-configuration",
            "--bucket",
            bucket,
            "--lifecycle-configuration",
            json.dumps({"Rules": rules}),
        ],
        profile,
        what="set the version-expiry lifecycle rule",
    )


def _existing_tagset(bucket: str, profile: str) -> list[dict]:
    """The bucket's current tags, or a refusal if they cannot be read.

    ``put-bucket-tagging`` REPLACES the whole tag set, so applying only our marker
    silently deletes whatever else was there — an operator's cost-allocation tag, say.
    Same API semantics as the lifecycle configuration, and the same fix: read, merge,
    write. Refuses on an unreadable answer rather than replacing tags it cannot see.
    """
    code, out, err = engine.run_aws(
        ["s3api", "get-bucket-tagging", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    if code == 0:
        try:
            parsed = json.loads(out or "{}")
            tags = parsed.get("TagSet")
        except (ValueError, TypeError) as e:
            # Returning [] here would be the same fail-open bug one layer down: the
            # caller would then REPLACE the tag set with just our marker, deleting tags
            # it never managed to read. A profile configured with `output = text` is
            # enough to trigger this, which is why the call above pins `--output json`
            # and this path refuses rather than guessing.
            raise DestinationError(
                f"the tags of {bucket!r} could not be parsed ({e}), so applying ours "
                f"would risk deleting tags that are already there. Nothing was changed."
            ) from e
        if tags is None:
            return []
        if not isinstance(tags, list):
            raise DestinationError(
                f"the tags of {bucket!r} came back in an unexpected shape "
                f"({type(tags).__name__}); refusing rather than replacing them."
            )
        return [t for t in tags if isinstance(t, dict) and "Key" in t]
    if "NoSuchTagSet" in (err or ""):
        return []
    hint = engine.map_access_denied(err)
    raise DestinationError(
        f"could not read the existing tags of {bucket!r} "
        f"({hint or err.strip()[:160] or 'unknown error'}), so applying ours would risk "
        f"deleting tags already on the bucket. Nothing was changed."
    )


def _merged_tagset_arg(bucket: str, profile: str) -> str:
    """The `--tagging` argument for harden_bucket: existing tags plus our marker."""
    kept = [t for t in _existing_tagset(bucket, profile) if t.get("Key") != TAG_BACKUP]
    kept.append({"Key": TAG_BACKUP, "Value": "true"})
    return json.dumps({"TagSet": kept})


def _existing_encryption(bucket: str, profile: str) -> dict | None:
    """The bucket's current SSE configuration, or None when it confirmably has none.

    Refuses on an unreadable answer instead of returning None, and the distinction is
    the whole point. Treating "cannot read" as "no encryption configured" is how a
    missing ``s3:GetBucketEncryption`` permission turns into a silent **downgrade**: the
    preservation step concludes there is no customer-managed key to protect, and the
    hardening step then replaces SSE-KMS with AES256. Failing open on a read is what
    makes the write destructive.
    """
    code, out, err = engine.run_aws(
        ["s3api", "get-bucket-encryption", "--bucket", bucket, "--output", "json"],
        profile,
        timeout=30,
    )
    if code != 0:
        # The one error that means "definitely not configured".
        if "ServerSideEncryptionConfigurationNotFoundError" in (err or ""):
            return None
        hint = engine.map_access_denied(err)
        raise DestinationError(
            f"could not read the encryption configuration of {bucket!r} "
            f"({hint or err.strip()[:160] or 'unknown error'}). Refusing to continue: "
            f"hardening would apply AES256, which would silently downgrade the bucket if "
            f"it is using a customer-managed key. Grant s3:GetBucketEncryption and "
            f"re-run."
        )
    try:
        cfg = json.loads(out or "{}").get("ServerSideEncryptionConfiguration")
    except (ValueError, TypeError) as e:
        raise DestinationError(
            f"the encryption configuration of {bucket!r} could not be parsed ({e}), so "
            f"whether it uses a customer-managed key is unknown. Nothing was changed."
        ) from e
    return cfg if isinstance(cfg, dict) and cfg.get("Rules") else None


def _uses_kms(cfg: dict | None) -> bool:
    if not cfg:
        return False
    for rule in cfg.get("Rules") or []:
        algo = (rule.get("ApplyServerSideEncryptionByDefault") or {}).get("SSEAlgorithm")
        # S3 spells KMS two ways: `aws:kms` and `aws:kms:dsse` (dual-layer). Matching
        # only the first read a DSSE-KMS bucket as "not KMS", so hardening's AES256
        # stood and the operator's dual-layer configuration was never put back — a
        # silent downgrade of exactly the setting they went out of their way to choose.
        if algo and algo.startswith("aws:kms"):
            return True
    return False


def _audit_authorization(outcome: str, detail: str) -> None:
    """Record the authorization decision in the security event log.

    Emitted HERE, beside the consumption, for the same reason the check itself lives
    here: a caller that reaches `setup_destination` directly makes a real authorization
    decision, and a decision the log never saw did not happen as far as an auditor is
    concerned. The CLI's pre-flight cannot stand in for it — that runs before the token
    is consumed, so it can only report what it expects to happen, not what did.

    Both answers are recorded. A deny is as interesting as an allow, because a run of
    denies is what an attempt to redirect the backup looks like.

    Best-effort: a logging failure must never be the reason a backup cannot be set up,
    so it degrades to a warning.
    """
    try:
        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="backup_setup_authorization",
                caller_identity=os.environ.get("USER", "unknown"),
                agent="kirocrew",
                source="library",
                operation="backup_setup_authorization",
                outcome=outcome,
                resources=detail,
            )
        )
    except Exception as e:  # pragma: no cover - logging must not break setup
        print(f"⚠️  Could not record the authorization decision in the audit log: {e}")


@contextmanager
def _authorization_held() -> Iterator[None]:
    """Serialise the read-validate-consume of the authorization across processes.

    Single use is the property the deletion exists to provide, and a read followed by a
    later unlink does not provide it: two invocations can both read the same token, both
    find it valid, and both go on to provision and record a destination, with the second
    unlink a no-op. One operator authorization then approves two destinations.

    The lock is taken on a dedicated sibling rather than on the token, because the token
    is deleted inside the critical section — a lock held on a path that stops existing
    protects nothing for whoever is waiting on it. It lives in the same keystone-fenced
    directory and is owner-only.

    ``file_lock`` fails closed, so an unobtainable lock refuses instead of proceeding
    unserialised, which is the whole point of taking it.
    """
    lock_path = authorization_token_path()
    lock_path = lock_path.parent / (lock_path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        platform_compat.restrict_to_owner(lock_path)
        with platform_compat.file_lock(fd, exclusive=True, required=True):
            yield
    finally:
        os.close(fd)


def consume_authorization(account: str, region: str, bucket: str) -> None:
    """Require, verify and consume the out-of-band authorization. Raises on refusal.

    THE chokepoint for "may this process record a backup destination". It lives here,
    in the library, and not in the CLI wrapper, because a guard is only worth what its
    narrowest path is worth: with the check in `backup_main`, anything able to run code
    in this process could call `setup_destination` directly and provision plus record an
    arbitrary destination with no authorization at all. The lesson, arrived at the hard
    way, was: put the check where the mutation happens.

    What this gate is worth, stated honestly, because the stronger claim was made here
    and was wrong. The authorization records that an OPERATOR CHOSE a destination. It
    does NOT establish that an agent could not have produced it. The keystone floor over
    the token's directory is two policy layers -- a sensitive-path check that the agent's
    file tools honour, and a deny pattern over command lines naming it -- and neither
    inspects the body of a program. An agent that writes a script elsewhere and runs it
    by path defeats both without doing anything exotic. There is no separate uid and no
    ACL this process lacks. Making the stronger claim true needs a control this module
    does not own (a PreToolUse denial of agent-issued `backup setup`, or an OS-level
    keystone), so this module does not assert it.

    It is still worth having: it turns "an agent sets up a backup destination" from the
    default outcome of asking for one into something that requires deliberate
    circumvention. What holds independently of it is the part to lean on -- the backup
    path takes no bucket from its caller, every write carries --expected-bucket-owner so
    S3 refuses a foreign-owned bucket, and setup verifies privacy before recording.

    The authorization NAMES its destination, and the name has to match what the caller
    actually resolved. A blank permission slip is not an authorization: with several
    profiles registered, an operator could create the token intending one account while
    the caller consumes it against another, and every later backup would go somewhere
    never approved.

    Consumed (deleted) on success, so one authorization cannot be replayed into a second
    redirection later.
    """
    token = authorization_token_path()
    if not token.is_file():
        _audit_authorization("denied", f"no authorization file; account={account} region={region}")
        raise DestinationError(
            "backup setup is not authorized. Create this file, naming the destination "
            f"you intend:\n\n     {token}\n\n"
            f'     {{"account": "{account}", "region": "{region}"}}\n\n'
            "   The values shown are the ones THIS invocation resolved — check they are "
            "the account and region you actually want before pasting them.\n"
            "   Creating it is a deliberate act on your part: that is what makes it an "
            "authorization. It records your choice of destination — it is not a control "
            "that an agent is unable to defeat."
        )
    with _authorization_held():
        # Re-read the existence check INSIDE the lock. The check above only spares the
        # honest no-authorization path from creating a lock file; it is this one that
        # decides, because a concurrent invocation may have consumed the token between
        # the two.
        if not token.is_file():
            _audit_authorization("denied", f"authorization already consumed; account={account}")
            raise DestinationError(
                "the authorization was consumed by another `backup setup` running at "
                "the same time. One authorization approves one destination: create a "
                "fresh one if you meant to record a second."
            )
        _consume_verified_authorization(account, region, bucket, token)


def _consume_verified_authorization(account: str, region: str, bucket: str, token: Path) -> None:
    """Validate the authorization body and delete it. Runs under the lock, never alone."""
    try:
        # JSON is UTF-8 by specification, and the destination sibling reader pins it
        # too. Without it this decodes with the platform's locale codepage, so on a
        # Windows host an authorization file holding any non-ASCII byte is refused as
        # unreadable rather than honoured.
        approved = json.loads(token.read_text(encoding="utf-8") or "{}")
        if not isinstance(approved, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        _audit_authorization("denied", f"unreadable authorization: {e}")
        raise DestinationError(
            f"the authorization file could not be read as JSON ({e}). It must name the "
            f'destination, e.g. {{"account": "{account}", "region": "{region}"}}'
        )

    mismatches = []
    if str(approved.get("account", "")).strip() != account:
        mismatches.append(f"account (authorized {approved.get('account')!r}, resolved {account!r})")
    if str(approved.get("region", "")).strip() != region:
        mismatches.append(f"region (authorized {approved.get('region')!r}, resolved {region!r})")
    approved_bucket = str(approved.get("bucket", "")).strip()
    if approved_bucket and approved_bucket != bucket:
        mismatches.append(f"bucket (authorized {approved_bucket!r}, would use {bucket!r})")
    if mismatches:
        _audit_authorization("denied", "authorization mismatch: " + "; ".join(mismatches))
        raise DestinationError(
            "the authorization does not match this invocation: "
            + "; ".join(mismatches)
            + ". Nothing was recorded — an authorization is for one destination, not a "
            "blank permission slip."
        )
    # Consumed only after every check passes, so a refused attempt does not burn it.
    token.unlink(missing_ok=True)
    _audit_authorization(
        "completed", f"authorized: account={account} region={region} bucket={bucket}"
    )


def _refuse_unusable_existing_bucket(name: str, profile: str) -> None:
    """Refuse an existing bucket setup must not adopt. READ-ONLY, and that is the point.

    Every reason to reject a pre-existing bucket lives here, in ONE function, called
    BEFORE ``consume_authorization``. The ordering is the requirement, not a detail: the
    authorization is one-shot and deleted on use, so a refusal raised after consumption
    charges the operator a fresh out-of-band token for a bucket problem they can simply
    fix and retry. Refusals that cost nothing keep the honest path cheap.

    Keeping them together is what stops that from regressing. The checks arrived one at
    a time and each new one was added at the site it guarded — which sat after
    consumption — so the ordering had to be rediscovered every time. A single
    pre-consumption chokepoint means a check added here inherits the ordering instead of
    re-litigating it.

    Three reasons, all read-only:

    1. **SSE-KMS configured.** Hardening applies AES256 unconditionally, so an
       operator's KMS default could only be kept by overwriting and restoring it, and a
       failure between those two steps leaves the bucket readable without the key with
       nothing reporting it.
    2. **Not ours.** Setup REPLACES the lifecycle configuration, so adopting an
       unrelated bucket discards its rules and starts expiring its noncurrent objects on
       our schedule — destroying data in order to set up a backup.
    3. **Carries a bucket policy.** Hardening sets public-access, ownership and
       encryption controls, none of which revoke a policy, so a bucket already granting
       read elsewhere would publish the memory it is about to receive.

    The recorded destination is admitted without the tag read: re-running setup on our
    own recorded bucket is the documented repair path, and it must keep working even if
    the tag call is unavailable.
    """
    if _uses_kms(_existing_encryption(name, profile)):
        raise DestinationError(
            f"bucket {name!r} is configured with SSE-KMS, and setup cannot preserve "
            f"that while applying its own at-rest controls: the hardening step writes "
            f"AES256, and restoring your key afterwards leaves a window where a failure "
            f"would silently downgrade the bucket.\n"
            f"   Nothing was changed and nothing was recorded.\n"
            f"   Either let setup create its own bucket (omit --bucket), or point "
            f"--bucket at one without a KMS default."
        )
    if name != _recorded_bucket() and not bucket_is_one_of_ours(name, profile):
        raise DestinationError(
            f"bucket {name!r} already exists but does not carry the "
            f"{TAG_BACKUP} tag, so this cannot confirm it was created as a Kiro "
            f"Crew backup destination (the tag may also just be unreadable). "
            f"Setting up would replace its lifecycle configuration and start "
            f"expiring its old versions, so nothing was done. A destination is "
            f"created, not adopted: choose a name that does not exist yet."
        )
    if not has_no_bucket_policy(name, profile):
        raise DestinationError(
            f"bucket {name!r} carries a bucket policy (or its policy could not be "
            f"read), so this cannot confirm who is able to read it. Nothing was "
            f"recorded. Remove the policy if the bucket is genuinely yours and "
            f"unshared, or choose a name that does not exist yet."
        )


def setup_destination(
    profile: str,
    region: str,
    bucket: str | None = None,
) -> tuple[Destination, bool, dict[str, object]]:
    """Create (or repair) the backup bucket and record it. Returns (dest, created, report).

    Idempotent: run it twice and the second run re-applies every control to the same
    bucket. That is deliberate — a control weakened out of band is repaired by re-running
    setup rather than silently tolerated by a backup.

    Versioning plus a noncurrent-expiration lifecycle rule are applied here and not
    treated as optional. Together they are what makes an overwritten or truncated bundle
    recoverable, and what stops that history growing forever. (The deploy module avoids
    versioning because its teardown cannot empty a versioned bucket; backup has no
    teardown, so that constraint does not carry over.)
    """
    if not _REGION_RE.match(region):
        raise DestinationError(f"invalid region: {region!r}")
    account = caller_account(profile)
    name = bucket or default_bucket_name(account, region)
    if not _BUCKET_RE.match(name):
        raise DestinationError(
            f"invalid bucket name {name!r}: 3-63 chars, lowercase letters, digits and "
            f"hyphens only"
        )

    # Before the first mutation, and before anything is recorded. `caller_account` above
    # is a read, so refusing here has changed nothing in AWS or on disk.
    # Read-only pre-flight FIRST, so every refusal below costs the operator nothing --
    # not an AWS mutation, and not the authorization. `consume_authorization` deletes the
    # token, so anything that can refuse on facts we can simply look up has to run before
    # it: otherwise a legitimate retry ("fix the bucket and run it again") needs a fresh
    # authorization created out of band, which is friction on the honest path only.
    exists = bucket_exists(name, profile, account)
    if exists:
        _refuse_unusable_existing_bucket(name, profile)

    consume_authorization(account, region, name)

    created = False
    if not exists:
        create = ["s3api", "create-bucket", "--bucket", name, "--region", region]
        if region != "us-east-1":
            create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
        code, _out, err = engine.run_aws(create, profile, timeout=60)
        if code != 0:
            hint = engine.map_access_denied(err)
            raise DestinationError(
                f"could not create bucket {name!r}: {hint or err.strip()[:200] or 'unknown error'}"
            )
        created = True
    else:
        # Already vetted by _refuse_unusable_existing_bucket, above, before the
        # authorization was consumed. Nothing to re-check here.
        pass

    # At-rest controls, via the deploy module's single implementation so a control added
    # there applies here too. Tags are read first and MERGED, because that implementation
    # writes complete configurations and S3 replaces rather than merges them.
    #
    # Encryption is handled by refusing, not by preserving. The shared implementation
    # applies AES256 unconditionally, so a bucket the operator configured with SSE-KMS
    # could only be kept by overwriting it and putting the original back afterwards --
    # and that leaves a window nothing can close. Any failure between the two steps
    # leaves the bucket downgraded to AES256, and if this bucket was ALREADY recorded by
    # an earlier setup, the record survives the failed run: later uploads then land in a
    # bucket readable without the KMS key the operator chose, with no error anywhere.
    # Narrowing that window was tried twice; the window is the design.
    #
    # So a pre-existing KMS bucket is refused BEFORE anything is mutated -- see the
    # read-only pre-flight above, which also runs before the authorization is consumed.
    # Giving `harden_bucket` a flag to skip encryption was the alternative, and it is
    # worse: its docstring says delegating exists so a new at-rest control reaches every
    # caller, and an opt-out is precisely what would break that.
    engine.harden_bucket(name, profile, _merged_tagset_arg(name, profile))
    _checked_aws(
        [
            "s3api",
            "put-bucket-versioning",
            "--bucket",
            name,
            "--versioning-configuration",
            "Status=Enabled",
        ],
        profile,
        what="enable versioning",
    )
    _put_lifecycle_rule(name, profile)

    report = verify_bucket_private(name, profile)
    if not is_fully_private(report):
        raise DestinationError(
            f"bucket {name!r} does not report itself private, encrypted and versioned "
            f"after setup: {report}. Nothing was recorded; fix the bucket or choose "
            f"another name."
        )

    dest = Destination(
        bucket=name,
        region=region,
        account=account,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    _save_destination(dest)
    return dest, created, report


def _checked_aws(args: list[str], profile: str, *, what: str) -> None:
    code, _out, err = engine.run_aws(args, profile, timeout=30)
    if code != 0:
        hint = engine.map_access_denied(err)
        raise DestinationError(f"could not {what}: {hint or err.strip()[:200] or 'unknown error'}")


def timeout_for_bytes(size: int) -> int:
    """Seconds to allow for transferring *size* bytes.

    Sized from the payload rather than left at the control-plane default: a memory
    bundle is tens of megabytes but a full one can reach well over a gigabyte, and the
    same 30s ceiling cannot serve both.
    """
    return _TIMEOUT_FLOOR_SECS + max(0, size) // _ASSUMED_FLOOR_BYTES_PER_SEC


def _unique_object_name(filename: str) -> str:
    """Give the object its own key, because the bundle's name is only second-resolution.

    A snapshot is named to the second, so two taken inside the same second produce the
    same name and therefore the same key. Uploading the second one does not fail: it
    overwrites, and the first becomes a NONCURRENT version -- absent from the listing the
    operator uses to find their backups, and removed for good when the lifecycle rule
    expires it. A backup silently replacing an earlier backup is the outcome this whole
    path exists to prevent.

    The suffix goes before the extension so the name still reads as an archive, and it
    comes from `token_hex` rather than a counter because two hosts, or two processes on
    one host, have no shared sequence to draw from.
    """
    stem = filename
    for suffix in (".tar.gz", ".tgz"):
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            return f"{stem}-{secrets.token_hex(4)}{suffix}"
    path = Path(filename)
    return f"{path.stem}-{secrets.token_hex(4)}{path.suffix}"


def upload(
    bundle: Path,
    dest: Destination,
    profile: str,
    *,
    key_name: str | None = None,
    sleep=time.sleep,
) -> str:
    """Upload *bundle* to the configured destination and return its ``s3://`` URL.

    *key_name* names the object independently of the local filename, because the bytes
    that get uploaded can be a redacted COPY of the bundle rather than the bundle itself.
    The key must stay the original's name so a restore finds the backup where the local
    archive's name says it should be, not under a temp file's name.

    Uses ``s3api put-object`` rather than ``s3 cp`` for one reason: it accepts
    ``--expected-bucket-owner``, so S3 itself refuses the write if the bucket is no
    longer owned by the account setup recorded. That check is worth more than the
    multipart machinery ``s3 cp`` would give us at these sizes.

    Retried on failure. The write is idempotent — same key, same bytes — so a retry can
    only re-send work that did not land.
    """
    if not bundle.is_file():
        raise DestinationError(f"bundle not found: {bundle}")
    size = bundle.stat().st_size
    if size > _MAX_SINGLE_PUT_BYTES:
        raise DestinationError(
            f"bundle is {size / 1024**3:.1f} GiB, above the {_MAX_SINGLE_PUT_BYTES / 1024**3:.0f} "
            f"GiB single-object limit. Back up fewer components, or split the bundle."
        )
    key = dest.key_for(_unique_object_name(key_name or bundle.name))

    # Exposure is re-checked here, not just at setup, because a bucket policy can be
    # added AFTER the destination was recorded and Block Public Access does not stop a
    # grant to a specific named account. Setup's verification is a point-in-time fact;
    # this is the state at the moment the memory is written.
    #
    # Fail CLOSED on an unreadable answer, and the reasoning is worth recording because
    # an earlier revision did the opposite. Warning-and-proceeding looks kinder -- a
    # transient throttle should not cost you a backup -- but the failure it optimises
    # for is not the one that happens. A profile simply lacking `s3:GetBucketPolicy`
    # makes the answer PERMANENTLY unknown, so every run warns and proceeds, the
    # operator learns to ignore the line, and the check silently becomes decorative
    # exactly when it is load-bearing. Refusing is also cheaper than it looks: the
    # local bundle is already written, so what is deferred is the off-host copy, with a
    # message naming what to fix -- not the backup itself.
    state = bucket_policy_state(dest.bucket, profile)
    if state != POLICY_ABSENT:
        detail = (
            "has acquired a bucket policy since it was set up"
            if state == POLICY_PRESENT
            else "has a bucket policy that could not be read (the profile may lack "
            "s3:GetBucketPolicy)"
        )
        raise DestinationError(
            f"bucket {dest.bucket!r} {detail}, so who can read it is not established. "
            f"Refusing to upload the memory bundle; your local snapshot is unaffected. "
            f"Inspect the policy or grant the profile s3:GetBucketPolicy, then re-run — "
            f"or run `kirocrew backup setup` against a fresh bucket."
        )

    # The reason the policy is re-read above is that setup's verification is a
    # point-in-time fact. That reason does not single out the policy: BPA, SSE,
    # versioning and ownership were established by the same setup and can be removed
    # the same way, and losing them is what makes the bundle readable by someone else.
    # So the whole predicate is re-asserted here, from what AWS reports right now.
    #
    # Unreadable counts as failure, for the reason spelled out above: a profile that
    # permanently cannot answer would otherwise turn this into a line the operator
    # learns to ignore. Setup ran this same read successfully, so a profile that can
    # upload can answer it.
    if not is_fully_private(verify_bucket_private(dest.bucket, profile)):
        raise DestinationError(
            f"bucket {dest.bucket!r} no longer reports the protections it was set up "
            f"with (public-access block, default encryption, versioning, and ACLs "
            f"disabled), or they could not be read. Refusing to upload the memory "
            f"bundle; your local snapshot is unaffected. Run `kirocrew backup status` "
            f"to see which control is missing."
        )

    args = [
        "s3api",
        "put-object",
        "--bucket",
        dest.bucket,
        "--key",
        key,
        "--body",
        str(bundle),
        "--expected-bucket-owner",
        dest.account,
    ]
    last = ""
    for attempt in range(1, _UPLOAD_ATTEMPTS + 1):
        try:
            code, _out, err = engine.run_aws(args, profile, timeout=timeout_for_bytes(size))
        except subprocess.TimeoutExpired:
            # The retry existed for transient network failures, and a timeout IS the
            # transient network failure for a large upload — but `run_aws` RAISES on
            # timeout rather than returning non-zero, so it escaped the loop entirely.
            # The advertised retry therefore did not cover its most likely trigger.
            code, err = 1, (f"the upload exceeded its {timeout_for_bytes(size)}s budget")
        if code == 0:
            return f"s3://{dest.bucket}/{key}"
        last = err.strip()
        hint = engine.map_access_denied(err)
        if hint:
            # A permissions or ownership failure will not fix itself; do not burn retries.
            raise DestinationError(f"upload refused: {hint}")
        if attempt < _UPLOAD_ATTEMPTS:
            print(f"  upload attempt {attempt} failed; retrying in {_RETRY_SLEEP_SECS}s")
            sleep(_RETRY_SLEEP_SECS)
    raise DestinationError(
        f"upload failed after {_UPLOAD_ATTEMPTS} attempts: {last[:200] or 'unknown error'}"
    )


# Every C0 control plus DEL and C1, and the bidi embedding/override/isolate controls. The
# first version of this excluded \x09 and \x0a on the theory that tab and newline are
# harmless whitespace. They are not, here: a key containing a newline lets `backup list`
# print FORGED lines, so the operator sees entries that do not exist in the bucket — and a
# tab can misalign the columns enough to hide a real one. Whitespace is only harmless when
# it is not being used to compose the output.
#
# The bidi controls are the same class one layer up: they carry no glyph, so they survive a
# control-byte filter untouched, yet they REORDER what the terminal draws. A key holding
# U+202E renders its own tail reversed, which is enough to display a bucket path the
# operator did not see and would not choose. Only the embeddings, overrides and isolates
# are escaped — those have no legitimate place in an object key — while the bare LRM/RLM
# marks are left alone because they appear in genuine right-to-left names and cannot by
# themselves rewrite a line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _escape_one(ch: str) -> str:
    """Render one stripped character in a form that cannot be re-interpreted.

    Two widths, because a single `\\xNN` spelling would be a lie for a code point above
    0xFF: `\\x202e` reads as `\\x20` followed by a literal `2e`, which is exactly the kind
    of ambiguous output this function exists to prevent.
    """
    code = ord(ch)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def safe_for_terminal(text: str, limit: int = 300) -> str:
    """Render S3-derived text printable.

    Object keys are attacker-influenceable by anyone who can write to the bucket, and
    S3 permits almost any byte in a key. Printing one raw means the terminal INTERPRETS
    whatever escape sequences it contains — which can repaint the screen, hide lines, or
    make `backup list` misreport what is in the bucket. That matters most in exactly the
    situation you would run it: after something has gone wrong.

    Control bytes and bidi controls become an escaped form, so the value is still
    recognisable rather than silently altered, and the length is capped so one long key
    cannot flood the view.
    """
    cleaned = _CONTROL_CHARS.sub(lambda m: _escape_one(m.group()), str(text))
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…(truncated)"
    return cleaned


def list_backups(dest: Destination, profile: str) -> dict[str, list[str]]:
    """Bundle keys under each host prefix, so a restore can name a machine."""
    code, out, err = engine.run_aws(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            dest.bucket,
            "--prefix",
            "backups/",
            "--expected-bucket-owner",
            dest.account,
            "--query",
            "Contents[].Key",
            "--output",
            "json",
        ],
        profile,
        timeout=60,
    )
    if code != 0:
        hint = engine.map_access_denied(err)
        raise DestinationError(f"could not list backups: {hint or err.strip()[:200]}")
    try:
        keys = json.loads(out or "null") or []
    except ValueError as e:
        raise DestinationError(f"unreadable listing: {e}") from e
    by_host: dict[str, list[str]] = {}
    for key in keys:
        parts = str(key).split("/")
        if len(parts) >= 3 and parts[0] == "backups" and parts[-1].endswith(".tar.gz"):
            by_host.setdefault(parts[1], []).append(str(key))
    for host in by_host:
        by_host[host].sort()
    return by_host


# --- restore side -------------------------------------------------------------
#
# Reading is not the dangerous direction, so a restore may name any s3:// object the
# caller's credentials can read. What it must never do is trust the bytes: the
# downloaded file goes through the same extraction filter and integrity check a local
# bundle does.


@dataclass(frozen=True)
class S3Object:
    bucket: str
    key: str


def parse_s3_url(url: str) -> S3Object:
    """Parse ``s3://bucket/key`` down to a bundle object, or raise."""
    if not url.startswith("s3://"):
        raise DestinationError(f"not an s3 URL: {url!r} (expected s3://bucket/key)")
    if url.rstrip().endswith("/"):
        raise DestinationError(
            f"{url!r} names a prefix, not an object — pass the full s3://bucket/key of a bundle"
        )
    rest = url[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not _BUCKET_RE.match(bucket):
        raise DestinationError(
            f"invalid bucket name {bucket!r}: 3-63 chars, lowercase letters, digits and "
            f"hyphens only"
        )
    if not _PREFIX_RE.match(key):
        raise DestinationError(f"invalid key {key!r}")
    if not key.endswith(".tar.gz"):
        raise DestinationError(
            f"{url!r} does not name a bundle — pass the full s3://bucket/key of a .tar.gz "
            f"written by `kirocrew snapshot`"
        )
    return S3Object(bucket=bucket, key=key)


def _claim_free_name(into: Path, filename: str) -> Path:
    """Atomically reserve an unused path in *into* and return it.

    The reservation is a zero-byte placeholder created with ``O_CREAT | O_EXCL``, which
    the filesystem guarantees only one caller wins. The download is later moved onto it
    with ``os.replace``, which is atomic and overwrites nothing but our own placeholder.
    """
    stem = filename[: -len(".tar.gz")] if filename.endswith(".tar.gz") else filename
    for n in range(0, 1000):
        candidate = into / (filename if n == 0 else f"{stem}.{n}.tar.gz")
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise DestinationError(
        f"cannot find a free local name for {filename!r} in {into} "
        f"(1000 collisions) — clear old bundles first"
    )


def download(url: str, into: Path, profile: str) -> Path:
    """Download a bundle object into *into* and return the local path."""
    obj = parse_s3_url(url)
    filename = obj.key.rsplit("/", 1)[-1]
    if shutil.which("aws") is None:
        raise DestinationError("the aws CLI is not installed or not on PATH")
    into.mkdir(parents=True, exist_ok=True)
    # Two different keys can share a basename (…/laptop/snap.tar.gz and
    # …/desktop/snap.tar.gz), and this lands beside retained bundles. Never overwrite
    # one: claim a free name ATOMICALLY so a second restore running concurrently cannot
    # pick the same one. A plain `exists()` test followed by a move is a race — both
    # processes see the name free, both move, and the loser's bundle is silently
    # replaced by the winner's, which a restore then reads as if it were its own.
    # O_CREAT|O_EXCL is the arbitration: exactly one process creates the placeholder,
    # the other gets EEXIST and tries the next name.
    local = _claim_free_name(into, filename)
    # Download into an owner-only staging directory rather than straight to the final
    # path. `aws s3 cp` creates its output with the process umask (0644 under the
    # common 022), and the snapshot directory can be shared, so writing there first
    # would leave the whole memory store world-readable for the length of the
    # transfer -- before restore_main gets a chance to restrict it. Staging inside a
    # 0700 directory means the bundle is never reachable by another user at any point;
    # it is chmod'd 0600 and only then moved into place.
    # Acquired INSIDE the block that releases it. The placeholder above is already
    # claimed at this point, and only the `except` below unlinks it -- so creating the
    # staging directory before entering the try left a failed `mkdtemp` (a full or
    # inode-exhausted filesystem, an `into` that just became unwritable) with the
    # placeholder still on disk. It is named exactly like a snapshot and is the newest
    # by mtime, so `--keep` counts it and prunes a real bundle in its place.
    #
    # `staging` starts as None so the `finally` cannot raise UnboundLocalError over the
    # original failure -- masking the real error with a cleanup error is how a full disk
    # would have been reported as a NameError.
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix="kirocrew-fetch-", dir=str(into)))
        # No chmod on POSIX: mkdtemp already creates the directory 0o700, which is
        # exactly the property wanted, and re-applying it would only add a literal
        # for a static analyser to misread as "widely permissive".
        #
        # Windows needs an explicit act, because mode bits are not the access
        # control that matters there: a new directory INHERITS the parent's DACL,
        # so a shared snapshot directory would hand its permissions to the staged
        # bundle no matter what the mode says. restrict_to_owner strips inheritance
        # and applies an owner-only DACL.
        if platform_compat.IS_WINDOWS:
            platform_compat.restrict_to_owner(staging)
        tmp = staging / filename
        code, _out, err = engine.run_aws(
            ["s3", "cp", url, str(tmp)],
            profile,
            timeout=timeout_for_bytes(2 * 1024**3),
        )
        if code != 0:
            hint = engine.map_access_denied(err)
            raise DestinationError(f"download failed: {hint or err.strip() or 'unknown error'}")
        if not tmp.is_file():
            raise DestinationError(f"download reported success but {tmp} is missing")
        # Owner-only on both platforms before it leaves the staging directory.
        platform_compat.restrict_to_owner(tmp)
        # os.replace, not shutil.move: atomic, and the only thing it overwrites is the
        # placeholder this call reserved.
        os.replace(str(tmp), str(local))
    except BaseException:
        # The reservation is ours, so a failure must not leave a zero-byte file behind
        # that looks like a bundle to `backup list` or to the next name claim.
        local.unlink(missing_ok=True)
        raise
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return local

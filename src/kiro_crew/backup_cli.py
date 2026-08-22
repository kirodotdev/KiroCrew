"""`kirocrew backup` — provision the off-host destination once, then inspect it.

Provisioning is deliberately its own command rather than something a backup run does
implicitly. Creating the bucket ourselves, recording it, and refusing every other bucket
answers "is this safe to write my memory into" once, visibly, with a human present —
instead of asking a backup job to re-derive that answer automatically on every run.

`kirocrew snapshot --components memory --to-s3` is the thing you schedule; it writes only
to what `setup` recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from kiro_crew import snapshot_remote as remote
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.snapshot import DestinationUnresolved, _resolve_aws_profile


def _profile_and_region(args: argparse.Namespace) -> tuple[str, str]:
    profile, region = _resolve_aws_profile(getattr(args, "aws_profile", None))
    return profile, getattr(args, "region", None) or region


def _audit_setup(outcome: str, detail: str) -> None:
    """Record the authorization decision in the security event log.

    This is the one decision in the feature that changes where the whole memory store
    gets sent, so both answers are worth a durable trace — a deny is as interesting as an
    allow, because a stream of denies is what an attempt to redirect backups looks like.
    Best-effort by design: a logging failure must not be able to block a backup setup, so
    it degrades to a warning rather than an exception.
    """
    try:
        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="backup_setup_authorization",
                caller_identity=os.environ.get("USER", "unknown"),
                agent="kirocrew",
                source="cli",
                operation="backup_setup_authorization",
                outcome=outcome,
                resources=detail,
            )
        )
    except Exception as e:  # pragma: no cover - logging must never break setup
        print(f"⚠️  Could not write the security audit event ({e})")


def _confirm_destination(profile: str, region: str, account: str, bucket: str | None) -> bool:
    """Require an authorization the calling process cannot manufacture, then confirm.

    An earlier revision gated this on ``sys.stdin.isatty()`` and described it as a
    human-presence check. It is not one: a pty is a thing any process can allocate, so
    `printf 'yes\\n' | script -qec 'kirocrew backup setup ...' /dev/null` satisfies both
    the isatty test and the prompt. That was demonstrated in practice, not hypothesised
    -- the recording made to showcase the guard drove it exactly that way.

    A terminal cannot carry authorization because the property being tested has to be
    something the caller is unable to produce. What it *can* be is the same primitive
    this codebase already uses when an agent must not be able to enable something: a
    file under a keystone path. ``<data home>/backup/`` is classified sensitive, so both
    the agent's file tools and any shell command naming that path are refused. A token
    inside it is therefore writable by the operator and by nothing the agent can drive,
    which is exactly the asymmetry a confirmation needs.

    So: the operator creates the token once, out of band; setup consumes it and deletes
    it, so a single authorization cannot be replayed into a second redirection later.
    The prompt is kept as well -- it is genuinely useful for catching the wrong profile
    -- but it is no longer load-bearing for security, and the docstring says so rather
    than leaving the next reader to assume otherwise.
    """
    token = remote.authorization_token_path()
    if not token.is_file():
        print(
            "❌ `backup setup` needs to be authorized out of band before it can record "
            "where your memory is sent.\n"
            f"   Create this file, naming the destination you intend:\n\n     {token}\n\n"
            f'     {{"account": "{account}", "region": "{region}"}}\n\n'
            "   The values shown are the ones THIS invocation resolved — check they are "
            "the account and region you actually want before pasting them.\n"
            "   It is deleted once used, so each authorization is good for one setup.\n"
            "   Creating it is a deliberate act on your part, which is the point: a "
            "terminal check would not do, because a pty is something any process can "
            "allocate. It records your choice of destination — it is not a control that "
            "an agent is unable to defeat.\n"
            "   Nothing else needs this. `kirocrew snapshot --to-s3` is safe to "
            "schedule once a destination exists."
        )
        _audit_setup("denied", f"unauthorized: account={account} region={region}")
        return False

    # The authorization names its destination, and it has to match. A blank permission
    # slip is not an authorization: with several profiles registered, an operator could
    # create the token intending one account while the caller consumes it with
    # `--aws-profile other`, and every later backup would go somewhere never approved.
    # Binding account and region makes the token answer "authorized to send WHERE",
    # which is the question that matters.
    try:
        approved = json.loads(token.read_text(encoding="utf-8") or "{}")
        if not isinstance(approved, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        print(
            f"❌ The authorization file could not be read as JSON ({e}).\n"
            f'   It must name the destination, e.g. {{"account": "{account}", '
            f'"region": "{region}"}}'
        )
        _audit_setup("denied", f"unreadable authorization: {e}")
        return False

    mismatches = []
    if str(approved.get("account", "")).strip() != account:
        mismatches.append(f"account (authorized {approved.get('account')!r}, resolved {account!r})")
    if str(approved.get("region", "")).strip() != region:
        mismatches.append(f"region (authorized {approved.get('region')!r}, resolved {region!r})")
    approved_bucket = str(approved.get("bucket", "")).strip()
    if approved_bucket:
        # Compare against the name setup will ACTUALLY use, which is the default when no
        # --bucket was passed. The first version of this wrote
        # `approved_bucket != (bucket or approved_bucket)`, and that `or` made the whole
        # comparison vacuous whenever --bucket was omitted: it compared the approved
        # bucket with itself and passed, so a token pinned to one bucket authorized the
        # default one instead. A conditional default inside a comparison is a good way
        # to write a check that cannot fail.
        effective = bucket or remote.default_bucket_name(account, region)
        if approved_bucket != effective:
            mismatches.append(f"bucket (authorized {approved_bucket!r}, would use {effective!r})")
    if mismatches:
        print(
            "❌ The authorization does not match this invocation: " + "; ".join(mismatches) + ".\n"
            "   Nothing was recorded. This is the check working: an authorization is "
            "for one destination, not a blank permission slip.\n"
            "   If the resolved values are the ones you want, update the file; if they "
            "are not, look at which AWS profile is being used."
        )
        _audit_setup("denied", f"authorization mismatch: {'; '.join(mismatches)}")
        return False

    print()
    print("About to record a backup destination:")
    print(f"  AWS account : {account}")
    print(f"  Region      : {region}")
    print(f"  Profile     : {profile}")
    print(f"  Bucket      : {bucket or '(default name derived from the account)'}")
    print()
    print("Every future `kirocrew snapshot --to-s3` will send your memory there.")
    if sys.stdin.isatty():
        try:
            answer = input("Type 'yes' to continue: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n❌ Cancelled. Nothing was recorded.")
            _audit_setup("denied", f"interrupted: account={account}")
            return False
        if answer != "yes":
            print("❌ Cancelled. Nothing was recorded.")
            _audit_setup("denied", f"declined: account={account}")
            return False
    else:
        print("(authorized out of band; no terminal to prompt on)")

    # NOT consumed here. `remote.setup_destination` re-checks and consumes it, because
    # the check has to sit where the mutation is: a guard only in this wrapper left
    # `setup_destination` callable directly, with no authorization at all. Everything
    # above is a read-only pre-flight kept for its error messages and the prompt, so a
    # mistake is caught before any AWS call rather than after one.
    _audit_setup(
        "completed",
        f"authorized: account={account} region={region} bucket={bucket or '(default)'}",
    )
    return True


def setup_main(args: argparse.Namespace) -> int:
    try:
        profile, region = _profile_and_region(args)
    except (DestinationUnresolved, OSError, ValueError) as e:
        print(f"❌ Could not resolve an AWS profile: {e}")
        print("   Register one with the deploy profile registry, or pass --aws-profile NAME.")
        return 1

    # Resolve the account first, read-only, so the confirmation can name the account
    # the memory would go to. "Wrong profile" is the mistake most worth catching here,
    # and an account number is what makes it visible.
    try:
        account = remote.caller_account(profile)
    except remote.UPLOAD_FAILURES as e:
        print(f"❌ {type(e).__name__}: {e}")
        return 1

    if not _confirm_destination(profile, region, account, getattr(args, "bucket", None)):
        return 1

    print(f"Setting up a backup destination in AWS (profile {profile}, region {region})")
    try:
        dest, created, report = remote.setup_destination(
            profile, region, bucket=getattr(args, "bucket", None)
        )
    except remote.UPLOAD_FAILURES as e:
        print(f"❌ {type(e).__name__}: {e}")
        return 1

    print(f"{'✅ Created' if created else '✅ Reused'} bucket {dest.bucket} in {dest.region}")
    bpa = report.get("block_public_access")
    blocked = isinstance(bpa, dict) and len(bpa) == 4 and all(bool(v) for v in bpa.values())
    print(f"  Public access blocked : {blocked}")
    print(f"  Encryption at rest    : {report.get('sse')}")
    print(
        f"  Versioning            : {report.get('versioning')} "
        f"(a replaced bundle stays recoverable for "
        f"{remote.NONCURRENT_RETENTION_DAYS} days)"
    )
    print(f"  Owner account         : {dest.account}")
    print(f"  This host's prefix    : {dest.prefix_url()}")
    print()
    print("Back up now:")
    print("  kirocrew snapshot --components memory --to-s3")
    print("Schedule it (nothing in Kiro Crew takes a backup for you):")
    print("  0 3 * * *  kirocrew snapshot --components memory --to-s3")
    return 0


def status_main(args: argparse.Namespace) -> int:
    try:
        dest = remote.load_destination()
    except remote.DestinationNotConfigured as e:
        print(f"No backup destination configured.\n  {e}")
        return 1
    except remote.DestinationError as e:
        print(f"❌ {e}")
        return 1

    print(f"Bucket        : {dest.bucket} ({dest.region})")
    print(f"Owner account : {dest.account}")
    print(f"Set up at     : {dest.created_at or 'unknown'}")
    print(f"This host      : {dest.prefix_url()}")

    if getattr(args, "offline", False):
        return 0
    try:
        profile, _region = _profile_and_region(args)
        report = remote.verify_bucket_private(dest.bucket, profile)
    except (DestinationUnresolved, OSError, ValueError) as e:
        print(f"\n⚠️  Could not check the live bucket: {e}")
        return 0
    except remote.UPLOAD_FAILURES as e:
        print(f"\n⚠️  Could not check the live bucket: {type(e).__name__}: {e}")
        return 0

    healthy = remote.is_fully_private(report)
    print(
        f"\nLive check     : {'✅ private, encrypted, versioned' if healthy else '❌ ' + str(report)}"
    )
    if not healthy:
        print("  Re-run `kirocrew backup setup` to re-apply the controls.")
        return 1
    return 0


def list_main(args: argparse.Namespace) -> int:
    try:
        dest = remote.load_destination()
        profile, _region = _profile_and_region(args)
    except remote.DestinationNotConfigured as e:
        print(f"❌ {e}")
        return 1
    except (DestinationUnresolved, remote.DestinationError, OSError, ValueError) as e:
        print(f"❌ {e}")
        return 1

    try:
        by_host = remote.list_backups(dest, profile)
    except remote.UPLOAD_FAILURES as e:
        print(f"❌ {type(e).__name__}: {e}")
        return 1

    if not by_host:
        print(f"No backups yet in s3://{dest.bucket}/backups/")
        return 0
    here = remote.host_id()
    for host in sorted(by_host):
        marker = "  (this host)" if host == here else ""
        # Host segments and keys both come from S3 object keys, which anyone able to write
        # to the bucket controls. Escape them before the terminal interprets them.
        print(f"{remote.safe_for_terminal(host, 120)}{marker}")
        for key in by_host[host][-5:]:
            print(f"    s3://{dest.bucket}/{remote.safe_for_terminal(key)}")
        if len(by_host[host]) > 5:
            print(f"    … {len(by_host[host]) - 5} older")
    print()
    print("Restore one with:  kirocrew restore s3://<bucket>/<key>")
    return 0


def backup_main(args: argparse.Namespace) -> int:
    sub = getattr(args, "backup_cmd", None)
    if sub == "setup":
        return setup_main(args)
    if sub == "status":
        return status_main(args)
    if sub == "list":
        return list_main(args)
    print("Usage: kirocrew backup {setup|status|list}")
    return 2

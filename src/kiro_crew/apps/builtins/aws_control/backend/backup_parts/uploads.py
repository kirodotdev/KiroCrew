"""The upload gate: live re-authorization before each paid call, and the skip proof.

:func:`_authorize_upload` runs immediately before an archive or label PUT, a baseline
probe, or a retention delete. It re-checks that the connection still points at the
account, that the app is still enabled, and that consent still names the account.
For an unattended caller it also re-reads the per-kind grant and the scheduling
block. :func:`_authorize_recovery_read` asks the same questions before a restore's
one extra version read. :func:`_unchanged_baseline` decides whether a run may skip
its upload.

Every outbound PUT stays in the facade. This module sends no payload: its only AWS
calls are the STS identity check each gate makes and the baseline HEAD probe.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, NoReturn, Optional

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.fingerprints import (
    _is_provable_version_id,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import KIND_SESSIONS
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import last_runs
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.nightly import (
    _NIGHTLY_CONSENT_READERS,
    scheduled_sessions_blocked_reason,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import APP_NAME
from kiro_crew.deploy.engine import AWSError
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE_MODULE)


#: Who triggered an upload, as the SEL record names them.
#:
#: An attribution field only earns its place if it DISTINGUISHES, so neither of
#: these is a default: ``caller`` is a required keyword all the way down to
#: ``_authorize_upload``. A new call site has to say which it is rather than
#: inheriting whichever guess happened to be written first -- and the guess that
#: was written first here was the interactive one, which attributed unattended
#: nightly work to a human who was not present.
CALLER_OWNER = "dashboard-owner"


CALLER_SCHEDULED = f"app:{APP_NAME}"


#: SEL operation names for the two decisions the backup engine asks
#: :func:`_authorize_upload` to make.
#:
#: Separate, because by the time retention runs the upload has ALREADY succeeded.
#: Filing a refused sweep as a denied ``backup_upload`` would put a denial in the
#: log for a transfer that completed, and an auditor counting denied uploads would
#: be counting a push that happened.
SEL_OP_UPLOAD = "aws_control.backup_upload"


SEL_OP_RETENTION = "aws_control.backup_retention"


#: The unchanged-check's own probe of the previous archive. A separate operation name
#: rather than reusing :data:`SEL_OP_UPLOAD`, because what it authorizes is different in
#: kind: one non-mutating ``head-object`` on this install's own key, taken to decide
#: whether an upload is needed at all. An audit reader who cannot tell that from a
#: refused archive PUT cannot tell which decision was actually being made.
SEL_OP_BASELINE_PROBE = "aws_control.backup_baseline_probe"


#: Teardown signal, set by the app's ``on_shutdown`` hook and honoured by the
#: last gate in :func:`_authorize_upload`. A ``threading.Event`` rather than an
#: asyncio one because the only reader is a worker THREAD; cancelling the loop's
#: await cannot reach it. This is why disabling the app stops a backup that is
#: still building instead of only stopping the scheduler.
_STOP = threading.Event()


def signal_stop() -> None:
    """Refuse further uploads. Called from app teardown."""
    _STOP.set()


def clear_stop() -> None:
    """Allow uploads again. Called when the app is (re-)enabled."""
    _STOP.clear()


def _refuse_upload(
    account: str,
    reason: str,
    *,
    caller: str,
    outcome: str = "denied",
    operation: str = SEL_OP_UPLOAD,
) -> NoReturn:
    """Record why an upload was refused in the SEL, then refuse.

    A refusal is the outcome an auditor most wants evidence of, and it was the
    one leaving no trace. Moving the work off the request path moved the
    authorization decision off the audited path with it: the route's audit has
    already recorded ``successful`` by the time a worker thread reaches
    ``put_file``, and the Job SDK only records that the run ``failed``. To a
    reader scanning SEL events for denials, a real denial looked like nothing at
    all.

    The event shape is the one this app already uses for a refused mutation
    (``routes._audit`` -> ``sel().log_api_access``) rather than a second
    convention for the same kind of decision. It is emitted HERE, at the
    decision, and not in the Job SDK runner: ``_authorize_upload`` is also
    reached from the nightly loop in ``hooks.py``, and a runner-level catch would
    leave that path unaudited.

    ``caller`` is passed in rather than assumed, because covering the nightly path
    is exactly what makes a hardcoded interactive caller a lie: an unattended run
    refused at 03:00 must not be recorded against the dashboard owner. Each entry
    point states its own (``CALLER_OWNER`` / ``CALLER_SCHEDULED``), so attribution
    stays true on both instead of being flattened to a neutral string that is
    honest for one path and lossy for the other.

    ``outcome`` is ``denied`` for the access decisions and ``failed`` for
    teardown. Every refusal leaves a record -- one covered path among several
    would make the rest look like non-events -- but a routine restart is not an
    access decision, and filing it as ``denied`` would put it in the same bucket
    as a withdrawn consent and devalue every real denial in the log. Both values
    are from the vocabulary ``sel.py`` documents for this field.

    ``operation`` names WHICH decision was refused, because the same gate now
    guards two of them: the archive upload, and the retention sweep that follows
    a successful one. They must not share a name -- a refused sweep filed as a
    denied upload is a denial recorded against a transfer that completed. See
    :data:`SEL_OP_UPLOAD` and :data:`SEL_OP_RETENTION`.

    Best-effort, like the route's audit: a failed audit must never convert a
    refusal into an upload.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="aws-control",
            resources=f"account={account}"[:200],
            error=reason[:200],
        )
    except Exception:
        logger.debug("aws-control SEL audit failed", exc_info=True)
    raise RuntimeError(reason)


def _authorize_upload(
    account: str,
    profile: str,
    region: str,
    *,
    caller: str,
    payload_kind: Optional[str],
    operation: str = SEL_OP_UPLOAD,
) -> None:
    """Re-check the authorization decisions at the moment of upload.

    An archive build can run for minutes inside a worker thread; consent
    withdrawal, the app being disabled, or the profile being REPOINTED at a
    different account during the build must stop the upload — the bytes have
    not left the machine until ``put_file`` runs. The account check is a LIVE
    ``sts:GetCallerIdentity`` (free, non-mutating) through the package's
    single sync chokepoint, not the cached snapshot.

    ``payload_kind`` names the kind whose payload these bytes ARE, and is
    required with no default for the same reason ``caller`` is: the value that
    would make a sensible default is the one that checks nothing. ``None`` is a
    real answer, not an opt-out -- it says this write carries no kind's payload
    (the caption in :func:`_publish_label`, which is written under both prefixes
    on purpose), so no per-kind grant governs it.
    """
    import json as _json

    from kiro_crew import aws_consent
    from kiro_crew.apps.manager import is_app_enabled
    from kiro_crew.deploy.engine import _checked

    # Order matters: the network round-trip (STS) runs FIRST, and the cheap
    # local decisions (app enabled, consent) run LAST — so no seconds-long
    # window sits between a local check and put_file for a withdrawal to slip
    # into. TOCTOU cannot be zero here (the upload itself takes time), but no
    # check is separated from the upload by another blocking call.
    out = _checked(
        ["sts", "get-caller-identity", "--output", "json"],
        profile,
        action="sts:GetCallerIdentity",
    )
    try:
        live = str(_json.loads(out or "{}").get("Account", ""))
    except _json.JSONDecodeError:
        live = ""
    if live != account:
        _refuse_upload(
            account,
            "this connection no longer points at the requested account; upload refused",
            caller=caller,
            operation=operation,
        )
    if not is_app_enabled("aws-control"):
        _refuse_upload(
            account,
            "aws-control was disabled during the backup build; upload refused",
            caller=caller,
            operation=operation,
        )
    # ONE read of the grant: the profile+region check below and the account check
    # after it both judge this record, so they cannot disagree about which grant
    # is in force.
    grant = aws_consent.read_grant(aws_consent.SERVICE_S3)
    granted, reason = aws_consent.is_granted(
        aws_consent.SERVICE_S3, profile=profile, region=region, grant=grant
    )
    if not granted:
        _refuse_upload(
            account,
            f"S3 consent no longer holds; upload refused: {reason}",
            caller=caller,
            operation=operation,
        )
    # `is_granted` is only the LOCAL half of the gate and its own docstring says
    # so: it matches profile+region and deliberately does not look at the
    # account. Checking the live account (above) against our target is therefore
    # not enough on its own -- the recorded grant may belong to a DIFFERENT
    # account that was configured under this same profile name in between, in
    # which case this upload would proceed on a consent the owner never gave for
    # THIS account. `aws_consent.authorize` exists for exactly this pairing but
    # is async and re-probes; this worker is sync and has already probed through
    # the package's single sync chokepoint, so the grant's account is compared
    # here instead. A grant naming no account is refused for the same reason
    # `authorize` refuses one: it cannot be verified against anything.
    if grant is None:
        _refuse_upload(
            account,
            "S3 consent was withdrawn during the backup build; upload refused",
            caller=caller,
            operation=operation,
        )
    if not grant.account or grant.account != account:
        _refuse_upload(
            account,
            "the recorded S3 consent does not name this account; upload refused",
            caller=caller,
            operation=operation,
        )
    # The unattended grant, re-read here and nowhere else in this gate. Every
    # other check above is about whether we may reach AWS at all; this one is
    # about whether the owner still wants THIS payload sent, which is a
    # different question and the only one whose withdrawal is unrecoverable once
    # ignored -- transcripts on S3 cannot be taken back. The window is the same
    # minutes-long build window the checks above already exist for, so leaving
    # this one out would defend every authorization except the one the operator
    # is most likely to change their mind about.
    #
    # Scheduled callers only. An owner who clicked the button is present and
    # authorized the run by clicking; the nightly bit is not their permission
    # slip, it is the one standing in for a person who is not there.
    if caller == CALLER_SCHEDULED and payload_kind is not None:
        reader = _NIGHTLY_CONSENT_READERS.get(payload_kind)
        if reader is None:
            # Fail closed on a kind nobody registered a grant for, rather than
            # letting it through on the strength of not being listed. A kind
            # added without its bit is then refused loudly instead of uploading
            # unattended under no authorization at all.
            _refuse_upload(
                account,
                f"no unattended grant is defined for {payload_kind!r}; upload refused",
                caller=caller,
            )
        elif not reader(account):
            _refuse_upload(
                account,
                "the unattended grant for this payload no longer holds; upload refused",
                caller=caller,
            )
    # The same re-read, for the other precondition a scheduled transcript upload
    # stands on. The grant above answers "does the owner still want this sent";
    # this answers "may a scheduled transcript archive be sent at all", and it can
    # change during the build for the same reason the grant can: the operator acts
    # while the archive is being written. Turning redaction ON mid-build is an
    # ordinary thing to do, and the already-built archive is unredacted -- the
    # sessions payload has no redaction seam, which is the whole reason the
    # nightly is withheld when redaction is on. Without this the build starts
    # under one answer and the PUT proceeds on it after it stopped being true.
    #
    # Deliberately the same predicate the due-check reads rather than a second
    # spelling of it: a cause added there is then refused here too, with nobody
    # having to remember this call site exists.
    if caller == CALLER_SCHEDULED and payload_kind == KIND_SESSIONS:
        blocked_now = scheduled_sessions_blocked_reason()
        if blocked_now is not None:
            _refuse_upload(
                account,
                f"a scheduled transcript upload is no longer allowed here: {blocked_now}",
                caller=caller,
            )
    # Last, and deliberately after every other check: app teardown. A worker
    # thread cannot be killed, so cancelling the loop's await leaves the archive
    # build running; this is what makes that build stop short of uploading.
    if _STOP.is_set():
        _refuse_upload(
            account,
            "aws-control is shutting down; upload refused",
            caller=caller,
            outcome="failed",
            operation=operation,
        )


def _unchanged_baseline(
    account: str,
    kind: str,
    tree: str,
    profile: str,
    region: str,
    bucket: str,
    *,
    caller: str,
) -> Optional[dict[str, Any]]:
    """The prior run this archive matches, or ``None`` when the upload must go ahead.

    Returning the matched RECORD rather than a bool is what lets the caller record a
    skip that carries the baseline's own key, body fingerprint and version, so the
    baseline survives for tomorrow's comparison instead of being replaced by a run that
    points at nothing.

    Every branch that is not a proven match returns ``None``, which means UPLOAD. That
    direction is the only safe one: a needless upload costs one archive, while a skip
    taken on weak evidence means the operator's newest data is not off-host and nothing
    says so. Concretely, all of these upload:

    * No tree fingerprint for this run, or none recorded on the prior run -- an install
      upgraded into this feature has no baseline, and unknown is not a pass. The same
      rule the rest of the backup engine applies to an empty body fingerprint.
    * The fingerprints differ: the tree moved, which is the whole point.
    * The prior run recorded no key, so there is nothing to prove.
    * The recorded object is GONE. A record proves this install wrote the key once, not
      that anything is there now -- and retention deletes by design, so a baseline
      ageing out of the keep window is an ordinary occurrence, not an exotic one. A
      skip against a deleted archive would leave the drive holding nothing for this
      kind while every run reported success.
    * The object at the recorded key is not the VERSION this install wrote. See below.
    * Its length is not the length we uploaded either -- a second, independent reading
      of the same question, kept because it costs nothing and does not depend on the
      bucket being versioned.
    * The HEAD could not be answered at all -- a throttle, a timeout, a credential
      lapse, an owner-pin refusal. ``head_object_meta`` raises rather than folding those
      into "absent", so this catches them and treats them as unproven.

    **Identity is the VERSION, not the length.** One drive is reachable by several
    installs by design, so a co-writer can overwrite a recorded key -- and an overwrite
    that happens to match the recorded byte length would pass a length-only check. The
    skip would then hold, uploads would stop while the tree was unchanged, and the
    object a restore fetches first would be the foreign one: ``restore_download`` reads
    the key's CURRENT version, so its fingerprint check rejects that object. Since
    :func:`_recover_recorded_version` the restore then makes one more read, of the
    version this install recorded, so there IS now an automated path back to our bytes
    -- but it depends on a provable recorded version, and the very buckets that make
    this failure likely are the ones that supply none (see the unversioned and
    suspended cases below). A skip must therefore not lean on it: silent stopped
    backups whose recovery is conditional is still the outcome worth spending a
    comparison to avoid, so the current version must be the one we recorded writing.
    This is the same question :func:`_current_version_is_ours` answers for retention,
    asked here of one key.

    A consequence worth stating: on an UNVERSIONED bucket no version is recorded and
    the HEAD names none, and on a SUSPENDED one both sides report ``"null"``, which
    names a version slot rather than one version. Neither can be shown to be ours, so
    the skip never fires there. That is deliberate and matches how retention already
    reads a missing version -- absence is "do not touch" -- and the app creates its
    drive with versioning on, so the cost falls on a bucket this product did not make.

    **The probe is authorized.** The ``head-object`` is a request to a paid service on
    the operator's account, and an archive build runs for minutes, so consent can be
    withdrawn or the app disabled between the run starting and this point. The gate is
    re-taken immediately before the HEAD under its own operation name
    (:data:`SEL_OP_BASELINE_PROBE`), which leaves the pre-PUT re-check at the push
    untouched: that one still guards the bytes leaving, and this one guards the metadata
    read. Deliberately placed AFTER the local checks above, so a run that could not skip
    anyway spends no round trip discovering it.
    """
    if not tree:
        return None
    last = last_runs(account).get(kind)
    if not isinstance(last, dict):
        return None
    if not isinstance(last.get("tree"), str) or last.get("tree") != tree:
        return None
    key = last.get("key")
    if not isinstance(key, str) or not key:
        return None
    try:
        _authorize_upload(
            account,
            profile,
            region,
            caller=caller,
            payload_kind=None,
            operation=SEL_OP_BASELINE_PROBE,
        )
        meta = storage.head_object_meta(profile, region, bucket, "backup", key, account=account)
    except (AWSError, OSError) as exc:
        logger.info(
            "aws-control: %s backup for %s could not confirm the previous archive is still "
            "in the drive, so it is uploading rather than skipping: %s",
            kind,
            account,
            exc,
        )
        return None
    if meta is None:
        logger.info(
            "aws-control: %s backup for %s has an unchanged tree, but the archive it would "
            "skip against is no longer in the drive, so it is uploading a full copy",
            kind,
            account,
        )
        return None
    recorded_version = last.get("version")
    stored_version = meta.get("VersionId")
    if not _is_provable_version_id(recorded_version):
        logger.info(
            "aws-control: %s backup for %s has an unchanged tree, but no provable version "
            "was recorded for the previous archive, so nothing shows the object now at "
            "that key is the one this install wrote; uploading a full copy. A drive with "
            "versioning off or suspended reports no usable version, so its nightly keeps "
            "uploading in full and its stored size keeps growing",
            kind,
            account,
        )
        return None
    if not _is_provable_version_id(stored_version) or stored_version != recorded_version:
        logger.warning(
            "aws-control: %s backup for %s has an unchanged tree, but the current version "
            "at the recorded key is not the one this install wrote, so the object there is "
            "not our archive; uploading a full copy",
            kind,
            account,
        )
        return None
    recorded_size = last.get("bytes")
    stored_size = meta.get("ContentLength")
    if not isinstance(recorded_size, int) or not isinstance(stored_size, int):
        return None
    if recorded_size != stored_size:
        logger.warning(
            "aws-control: %s backup for %s has an unchanged tree, but the object at the "
            "recorded key is %s bytes where this install uploaded %s, so it is not the "
            "archive we wrote; uploading a full copy",
            kind,
            account,
            stored_size,
            recorded_size,
        )
        return None
    return last


def _authorize_recovery_read(profile: str, region: str, *, account: str) -> Optional[str]:
    """``None`` if the recorded-version read may be made, else why it may not.

    The recovery's extra read is the one AWS call in a restore that the caller did
    not ask for, and the first read can take minutes -- long enough for the owner to
    disable the app or withdraw the grant, and long enough for the profile to be
    repointed at a different account. The route's pre-flight ran before any of that
    could happen, so it cannot speak for it.

    The same four questions :func:`_authorize_upload` asks, in the same order and for
    the same reason it documents: the network round-trip runs FIRST and the cheap
    local decisions LAST, so no window sits between a local check and the call it
    guards. The two gates differ only in what a refusal DOES -- the upload raises,
    because a refused upload is a failed run, while this returns a reason, because a
    refused recovery is the honest refusal the restore already had.

    The stored grant is read ONCE and its profile, region and account all checked
    against that single snapshot. Grant reads are unlocked while writes take the
    consent lock, so checking profile and region against one read and the account
    against a second would let a re-grant landing between them satisfy each half from
    a different record -- a refusal turned into an allow. A profile repointed between
    the grant and now must not reach AWS under a consent the owner never gave for THIS
    account, which is a different question from whether any S3 consent exists.
    """
    import json as _json

    from kiro_crew import aws_consent
    from kiro_crew.apps.manager import is_app_enabled
    from kiro_crew.deploy.engine import _checked

    try:
        out = _checked(
            ["sts", "get-caller-identity", "--output", "json"],
            profile,
            action="sts:GetCallerIdentity",
        )
    except (AWSError, OSError, ValueError) as exc:
        # An unanswerable probe is a refusal, not a fault to surface: this caller's
        # honest answer for an unprovable archive is the one it already has.
        return f"the account this connection points at could not be confirmed ({exc})"
    try:
        live = str(_json.loads(out or "{}").get("Account", ""))
    except _json.JSONDecodeError:
        live = ""
    if live != account:
        return "this connection does not point at the requested account"
    if not is_app_enabled("aws-control"):
        return "aws-control was disabled before the recorded version could be read"
    # ONE read of the grant, with all three fields checked against that one snapshot.
    # Grant reads are unlocked while writes take the consent lock, so asking
    # `is_granted` (which reads the grant and checks profile and region) and then
    # reading the grant AGAIN for its account compares two different snapshots: a
    # re-grant landing between them passes the profile check against the old record
    # and the account check against the new one, which turns a refusal into an allow.
    # One snapshot cannot disagree with itself. `_authorize_upload` reads the grant
    # once in the same way.
    grant = aws_consent.read_grant(aws_consent.SERVICE_S3)
    if grant is None:
        return "S3 use is not confirmed, so no consent covers reading the recorded version"
    if grant.profile != profile or grant.region != region:
        return (
            "the S3 grant names "
            f"{aws_consent.credential_source(grant.profile)} in region "
            f"{grant.region or '(provider default)'}, which is not this call"
        )
    if not grant.account or grant.account != account:
        return "the recorded S3 consent does not name this account"
    return None

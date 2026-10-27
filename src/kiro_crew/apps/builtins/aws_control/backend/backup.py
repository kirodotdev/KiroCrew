"""Backup — memory/workspace snapshots and session archives on ``backup/``.

Two backup kinds, one push path:

* **Snapshot** (the mockup's "Memory & workspace" row): the existing
  ``kiro_crew.snapshot`` engine builds its portable ``.tar.gz`` (memory,
  crons, config, skills, workspace, notifications, security — its component
  set, unchanged), and the archive is pushed to
  ``backup/snapshots/<install>/<name>.tar.gz``.
* **Sessions archive** (the "Sessions archive" row): one tarball of BOTH
  session halves — ``<data home>/sessions/`` (transcripts + rotated
  archives) and ``<kiro home>/sessions/cli/`` (the CLI replay logs) -- plus a
  table-scoped export of the kiro-cli TERMINAL conversation store (see
  "Terminal conversations" in ``backup_parts.conversations``), pushed to
  ``backup/sessions/<install>/<stamp>.tar.gz``. Whole-set, not per-session:
  the "both halves move together" invariant is honoured by construction, and a
  run whose trees have not moved since the archive already in the drive uploads
  nothing at all -- see "Unchanged runs upload nothing" below. Splitting the set
  into per-session objects and sending only the changed ones is a different
  feature and deliberately not this one: the RFC lists incremental and
  deduplicating transfer among its non-goals.

**One drive can be reached by several installs.** Discovery is by tag, so a
second install finds the first one's bucket and writes to it by design — the
``<install>`` segment is what keeps the two apart afterwards, and it is a
random per-install id held in this app's own state, never the telemetry
install id. An archive uploaded before that segment existed carries no id and
is reported as being of unknown origin rather than claimed by whoever is
reading. The "install identity" note in ``backup_parts.identity`` says why the id
decides what is permitted while the human-readable label decides only what is
displayed.

State (`<app data dir>/backup.json`): this install's identity, plus the last
run per kind, the nightly toggle per account, and the per-account retention
count. The nightly loop lives in the app's ``on_startup`` hook.

**Unchanged runs upload nothing.** Every run builds its archive, then asks whether
that archive carries anything the drive does not already hold; if not, it records a
run saying so and sends no bytes. The comparison CANNOT be over archive bytes -- a
``tar.gz`` embeds per-entry mtimes and a gzip stamp, so two runs over an identical
tree produce different bytes and an archive-level check would report "changed" every
night. It is taken over the entry set instead (path, kind, permission mode, size and
content hash per member: ``_tree_fingerprint``), read from the packed payload so it
cannot drift from what would actually be sent. A skip is refused unless the previous
archive is PROVEN still in the drive at its recorded key and its recorded length,
because a record proves only that this install once wrote that key -- retention
deletes by design and a co-writer can overwrite a name. Every uncertain branch
uploads. See ``_unchanged_baseline``, which lists them.

**Retention runs after a successful push, never before it.** Both key shapes
above carry a timestamp, so nothing is ever overwritten and an unbounded drive
was the default: a nightly backup added one archive a night forever. Each push
therefore ends by retiring this install's oldest archives OF THAT KIND, but only
once an operator has set a count: retention is opt-in, and with no usable count
every archive is kept. The bucket is
versioned, so the sweep deletes object VERSIONS rather than objects -- a plain
delete would leave a marker and go on billing for the bytes behind it. It is
best-effort: a cleanup that fails logs one line and leaves the successful backup
alone. See ``_prune_remote_archives``.

**One import path over private owners.** This module is the engine's only import
path and its only patch surface. The responsibilities it composes live in
``backup_parts``, lowest layer first:

* ``egress_text`` -- the one redaction sequence for text that is published or shown;
* ``state`` -- ``backup.json``, its locks and lock order, the recovery overlay;
* ``identity`` -- the install id and label, and the archive key namespace;
* ``fingerprints`` -- body, tree and version-id proof;
* ``traversal`` -- the descriptor-pinned walk and the platform capability;
* ``ledger`` -- run records, and what this install uploaded;
* ``layer_b`` -- the Layer B grant and its scope;
* ``nightly`` -- the unattended grants, failure records, backoff and due-ness;
* ``uploads`` -- the upload gate, the recovery-read gate and the skip proof;
* ``catalog`` -- remote listings;
* ``retention`` -- the opt-in sweep;
* ``archive_inputs`` -- the pinned staged archive and the screened tree walk;
* ``conversations`` -- the terminal conversation export;
* ``restore`` -- the staged restore and the recovery read behind it.

What stays here is what those owners are composed INTO: both archive builders and
every outbound archive and label PUT, and the Job SDK runner. The redaction-sink
registry names this module as the backup boundary for those PUTs. Every owner's
names resolve here, and a write through this module reaches every module holding
the name, so patching ``backup.X`` changes what the engine runs wherever that name
is read. The composition note at the end of this module says how, and what a patch
harness sees of it.

CALLER CONTRACT: handlers hold the consent gate; sync, subprocess/tar-bound
— call via ``asyncio.to_thread`` (pushes of a large sessions set can run
minutes; handlers use generous timeouts).
"""

from __future__ import annotations

import builtins as _builtins
import contextlib
import datetime as dt
import importlib as _importlib
import json
import logging
import os
import sys as _sys
import tarfile
import tempfile
import typing as _typing
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import Any

from kiro_crew import snapshot
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.archive_inputs import (
    _add_tree,
    _open_pinned_archive_fd,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.catalog import (
    other_install_ids,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.conversations import (
    _ConversationExport,
    _ConversationTooLarge,
    _export_cli_conversations,
    _RedactionFailed,
    _ScratchExportUnsafe,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.fingerprints import (
    _body_fingerprint,
    _tree_fingerprint,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    KIND_SESSIONS,
    KIND_SNAPSHOT,
    KIND_SUBPATHS,
    LABEL_OBJECT_NAME,
    _stamp,
    install_identity,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.layer_b import (
    _audit_layer_b_decision,
    layer_b_grant_covers_conversations,
    sel,
    sessions_layer_b_enabled,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import (
    _record_run,
    _record_skip,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.nightly import (
    nightly_run_witness,
    record_nightly_failure,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.retention import (
    _audit_retention,
    _prune_remote_archives,
    _record_last_sweep,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (
    _PUSH_TIMEOUT_SECS,
    APP_NAME,
    _upload_lock,
    a_retained_archive_carries_conversations,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.traversal import (
    _CAN_PIN_TRAVERSAL,
    _NO_PINNING_REASON,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.uploads import (
    _STOP,
    CALLER_OWNER,
    CALLER_SCHEDULED,
    JOB_PARAM_CALLER,
    JOB_PARAM_PROFILE,
    JOB_PARAM_REGION,
    ScheduledUploadWithheld,
    _authorize_upload,
    _refuse_upload,
    _unchanged_baseline,
)
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.snapshot import snapshot_main

logger = logging.getLogger(__name__)


# The conversation export's errors name this module as their home, so a traceback and
# a logged error type read the same whichever module raises them.
for _moved in (_ConversationTooLarge, _RedactionFailed, _ScratchExportUnsafe):
    _moved.__module__ = __name__
del _moved


def _publish_label(
    account: str,
    profile: str,
    region: str,
    bucket: str,
    identity: dict[str, str],
    *,
    caller: str,
) -> None:
    """Write this install's label beside its own archives. Best-effort, always.

    Without this, every archive from the OTHER machine reads as 32 hex characters
    -- and on a replacement machine, where nothing is provably ours, EVERY row
    does. A hex blob nobody can read is not attribution, so the label has to reach
    the reader, and the only channel between two installs is the bucket.

    **Takes its own authorization, once per PUT.** An authorization is only good
    for the write that immediately follows it: any S3 round trip in between is time
    in which consent can be withdrawn, so a single gate covering two uploads leaves
    the second one running on a decision that has expired. Ordering the writes
    differently cannot fix that -- it only chooses which write is exposed -- so the
    gate belongs to the write, and it lives INSIDE this function so a caller cannot
    separate the two by moving a call.

    Publishes under BOTH kind prefixes, not just the kind that triggered it. The
    reader takes the first sidecar it finds across kinds, so a rename followed by a
    backup of only one kind would leave the other prefix holding the old name and
    the reader could keep showing it. Writing both keeps every copy current, and
    they are a hundred bytes each.

    Never raises. A backup that reached the bucket must not be reported as failed
    because a caption did not, and the reader degrades to the id on its own when
    the sidecar is missing.

    The document carries the label and the time, and deliberately NOT the id: the
    id is in the KEY, where S3 put it, and repeating it in a body a writer controls
    would invite a reader to trust the copy that can lie.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="kc-backup-label-") as tmp:
            path = Path(tmp) / LABEL_OBJECT_NAME
            path.write_text(
                json.dumps(
                    {
                        "label": identity["label"],
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    }
                ),
                encoding="utf-8",
            )
            for sub in KIND_SUBPATHS.values():
                # Inside the loop, not before it. The first PUT is an S3 round trip,
                # so a gate hoisted above the loop would leave the second write
                # running on a decision taken before that trip.
                #
                # `payload_kind=None` because this write is not any kind's payload:
                # it is one document, a label and a time, deliberately published
                # under BOTH prefixes. Keying it to the prefix it happens to be
                # writing would make an install with one kind's nightly off lose
                # that prefix's caption -- which is a rename going unseen, not a
                # transcript leaving the machine.
                _authorize_upload(account, profile, region, caller=caller, payload_kind=None)
                storage.put_file(
                    profile,
                    region,
                    bucket,
                    "backup",
                    f"{sub}/{identity['id']}/{LABEL_OBJECT_NAME}",
                    str(path),
                    account=account,
                    timeout=60,
                )
    except Exception:
        logger.warning(
            "aws-control: this install's backup label could not be published; another "
            "install will show its id instead of its name",
            exc_info=True,
        )


def run_snapshot_backup(
    account: str, profile: str, region: str, bucket: str, *, caller: str
) -> dict[str, Any]:
    """Build a snapshot archive and push it. Returns the run record."""
    identity = install_identity()
    with storage.pinned_staging("kc-backup-") as (tmp_dir, dir_fd):
        tmp = str(tmp_dir)
        rc = snapshot_main([tmp, "--keep", "1"])
        if rc != 0:
            raise RuntimeError(f"snapshot build failed (rc={rc})")
        archives = sorted(Path(tmp).glob("kirocrew-snapshot-*.tar.gz"))
        if not archives:
            raise RuntimeError("snapshot build produced no archive")
        archive = archives[-1]
        # The bytes that LEAVE are redacted when the operator has opted in; the local
        # bundle is never touched. This is the one part of an off-host backup the app does
        # not own: the bucket, its hardening, the consent grant and the transport are all
        # here, but rewriting the payload is the snapshot format's own business, so the
        # snapshot module owns it and this is where it attaches.
        #
        # Deliberately BEFORE `_authorize_upload` and the push: a redaction that cannot be
        # completed must stop the upload rather than fall through to sending the bundle
        # unredacted, and `RedactionFailed` carries the reason (an unprovable payload
        # database, a file that is not text, an unreadable switch) for the caller to
        # surface. `tmp` is this function's own directory and is removed with it, so the
        # redacted copy never outlives the push.
        redacted = snapshot.prepare_redacted_copy(archive, Path(tmp), list(snapshot.COMPONENTS))
        payload = redacted or archive
        # Take hold of the payload ONCE, and read nothing by name afterwards. Both
        # files here are created by another module (``snapshot_main`` and
        # ``prepare_redacted_copy``), so the earliest this run can pin one is now --
        # but from here the entry-set digest, the size, the body digest and the AWS
        # CLI's body all come from this descriptor. A name resolved once per step in
        # a directory a same-UID process can write is a different answer per step,
        # and a file swapped between two of them makes the upload carry bytes
        # nothing measured. ``_open_pinned_archive_fd`` also refuses a link or a
        # multiply-named file AT the name, which is what a bundle replaced before
        # this point would be.
        verified_fd = _open_pinned_archive_fd(tmp_dir, dir_fd, payload.name)
        # The snapshot body is a NAMED on-disk file ``snapshot_main`` /
        # ``prepare_redacted_copy`` write, so it is held by pinning its INODE
        # (``_open_pinned_archive_fd``: O_NOFOLLOW + S_ISREG + st_nlink == 1 + owner),
        # plus a deny-write open on Windows and the post-transfer ``_assert_same_file``
        # inode backstop. The size, both fingerprints and the upload all read THIS one
        # descriptor, so no step re-resolves the name. Its CONTENT is not held
        # unrewritable in place: a same-UID writer the staging census cannot see could
        # rewrite the file before the transfer finishes. Holding the content for the
        # whole transfer needs the producer to write into a descriptor it never lets a
        # name reach, which this inode pin does not provide for either kind.
        payload_fd = verified_fd
        try:
            # Does this archive carry anything the drive does not already hold? Taken
            # over the PAYLOAD, so it is the bytes that would actually leave that are
            # compared -- a redaction switch flipped since the last run changes those
            # without the source tree moving, and this notices.
            #
            # Placed BEFORE `_authorize_upload` deliberately. The gate's contract is
            # that it sits immediately before the PUT with nothing in between, so a
            # decision that can end the run has to be taken on this side of it; and a
            # run that is about to send nothing has no upload to authorize in the
            # first place.
            tree = _tree_fingerprint(payload, volatile_root=True, fd=payload_fd)
            baseline = _unchanged_baseline(
                account, KIND_SNAPSHOT, tree, profile, region, bucket, caller=caller
            )
            if baseline is not None:
                record = _record_skip(account, KIND_SNAPSHOT, baseline, tree)
                if record is not None:
                    logger.info(
                        "aws-control: snapshot backup for %s found the tree unchanged since "
                        "the archive already in the drive, so it uploaded nothing",
                        account,
                    )
                    # No label publish and no retention sweep. Both exist to follow a
                    # push: a local rename reaches the drive on the next real upload
                    # rather than on this skip, and retention retires copies by count --
                    # running it here would let a stretch of unchanged nights walk the
                    # keep window down and delete the very archive the next skip has to
                    # prove is present.
                    return record
                logger.info(
                    "aws-control: snapshot backup for %s could not record its skip because "
                    "the recorded baseline moved while the archive was being built, so it "
                    "is uploading a full copy",
                    account,
                )
            # snapshot_main names by second-resolution timestamp; a racing pair
            # would collide on the key, so the pushed key carries its own
            # entropy (the _stamp shape) rather than trusting the file name.
            #
            # The install id is a SEPARATE segment rather than more characters in the
            # file name, and the shape is what buys the listing its answer: one
            # delimited list of ``snapshots/`` returns the id of every install writing
            # here as a folder AND the pre-namespace archives as files, so "whose is
            # this" and "is another install writing here" come back together. An id
            # folded into the name would need the whole prefix walked to learn either.
            key = (
                f"{KIND_SUBPATHS[KIND_SNAPSHOT]}/{identity['id']}/"
                f"kirocrew-snapshot-{_stamp()}.tar.gz"
            )
            # The gate sits IMMEDIATELY before the archive PUT with nothing in
            # between -- no other network call, no second upload -- so the decision
            # that authorizes these bytes cannot go stale before they leave. The
            # label's own PUT takes its own authorization inside `_publish_label`,
            # which is why it can safely run afterwards.
            _authorize_upload(account, profile, region, caller=caller, payload_kind=KIND_SNAPSHOT)
            version = storage.put_file(
                profile,
                region,
                bucket,
                "backup",
                key,
                str(payload),
                account=account,
                timeout=_PUSH_TIMEOUT_SECS,
                body_fd=payload_fd,
            )
            record = _record_run(
                account,
                KIND_SNAPSHOT,
                key,
                os.fstat(payload_fd).st_size,
                _body_fingerprint(fd=payload_fd),
                version,
                tree=tree,
            )
        finally:
            os.close(payload_fd)
        # After the archive and after the ledger write, and with its own
        # authorization: a caption must never delay or endanger the payload.
        _publish_label(account, profile, region, bucket, identity, caller=caller)
        # LAST, and after a push that succeeded. This is the only step here that
        # deletes, so it runs once everything proving this run worked is already
        # done -- and it cannot fail the run. See _prune_remote_archives.
        _prune_remote_archives(
            account, profile, region, bucket, KIND_SNAPSHOT, identity["id"], key, caller=caller
        )
        return record


def run_sessions_backup(
    account: str, profile: str, region: str, bucket: str, *, caller: str
) -> dict[str, Any]:
    """Tar the session halves the operator permits, and push. Returns the run record.

    Refuses outright on a platform that cannot pin the traversal to descriptors.
    The session directories are agent-writable and this archive is uploaded
    unattended, so a name-based walk would trade an unrecoverable outcome
    (credentials reached by a junction swapped in after the check) for a
    convenience. See :func:`_add_tree`.

    The crew half (the display transcript) always rides. The kiro-cli half --
    Layer B, the unredacted model context -- and the terminal's own conversation
    export ride only on the operator's
    standing permission (:func:`sessions_layer_b_enabled`), and the run record
    says which way it went, so which layers an archive holds is readable from the
    record instead of being a guess.

    Raises ``RuntimeError`` when that permission is revoked while the archive is
    being built: the bytes are discarded unuploaded and unrecorded rather than
    shipped under a permission the operator has withdrawn.
    """
    if not _CAN_PIN_TRAVERSAL:
        raise RuntimeError(_NO_PINNING_REASON)
    identity = install_identity()
    crew_sessions = data_home() / SESSIONS_DIR_NAME
    cli_sessions = kiro_sessions_dir()
    # Read ONCE, before the archive is opened, so a write landing mid-build cannot
    # make the tar carry Layer B under one half of the build and omit it under the
    # other. The record is taken from what was actually added, not from this
    # answer, so the two cannot disagree about what is inside the archive -- which
    # is the reading a restore would otherwise trust. A withdrawal landing in that
    # window is caught before the upload instead, by refusing -- see the recheck
    # below, which adds no second answer for the record to disagree with.
    layer_b = sessions_layer_b_enabled(account)
    # Read beside the permission, so both describe the same moment. A grant recorded
    # before the conversation export was disclosed covers the `cli` half only; see
    # `layer_b_grant_covers_conversations`.
    layer_b_conversations = layer_b and layer_b_grant_covers_conversations(account)
    # Named for the record. Present only for the state that needs explaining: the
    # permission is on, and its grant does not reach the conversation export. A grant
    # that does reach it, or no grant at all, needs no scope line -- `layer_b` already
    # says which of those happened.
    layer_b_scope = "cli" if layer_b and not layer_b_conversations else ""
    _audit_layer_b_decision(account, layer_b, conversations=layer_b_conversations, caller=caller)
    name = f"sessions-{_stamp()}.tar.gz"
    # Stage into a private, owner-only directory this app cuts and PINS
    # (``storage.pinned_staging``: ``mkdtemp`` 0700 under the masked staging root,
    # then a pinned directory handle), build the tar there by name, and then take
    # hold of it ONCE through ``_open_pinned_archive_fd``. From that point the
    # entry-set digest, the size, the body digest and the AWS CLI's body all come
    # from that one descriptor checked at the point it is taken (O_NOFOLLOW +
    # S_ISREG + st_nlink == 1 + owner), so no step re-resolves the name. This is the
    # same hold the snapshot path uses, and it works on every platform the backup
    # runs on (Linux, macOS, the BSDs, Windows).
    #
    # The inode pin fixes WHICH file the descriptor reaches; it does not hold the
    # file's CONTENT unrewritable in place against a same-UID writer for the whole
    # transfer. That needs the producer to write the body into a descriptor it never
    # lets a name reach, which this pin does not provide.
    with storage.pinned_staging("kc-backup-") as (tmp_dir, dir_fd):
        archive = tmp_dir / name
        with tarfile.open(archive, "w:gz") as tar:
            count = _add_tree(tar, crew_sessions, "crew")
            # Counted separately because the RECORD below must describe the
            # archive, not the permission. A permitted run whose kiro-cli
            # directory is absent or empty -- an ordinary state on a fresh or
            # CLI-idle install -- adds nothing, and the crew half alone keeps
            # `count` past the guard, so recording the permission would file a
            # crew-only archive as carrying Layer B. Nothing corrects that
            # afterwards: a run record is written once, and a later run with real
            # kiro-cli files records only itself. A restore reading it would go
            # looking for a fidelity the object does not hold.
            layer_b_files = _add_tree(tar, cli_sessions, "cli") if layer_b else 0
            count += layer_b_files
            # The kiro-cli terminal conversation store (the chat tables in
            # `data.sqlite3`) is disjoint from both transcript halves above. It is
            # exported table-scoped, never file-copied, because the same file holds
            # live bearer tokens.
            #
            # It rides on the SAME permission as the `cli` tree, not on the crew
            # half's terms, because it is the same data class: both carry what a
            # model actually held, unredacted, while the crew transcript carries
            # what was DISPLAYED with display-time redaction applied. An export
            # that rode ungated would carry unredacted terminal context out of an
            # install whose operator withheld exactly that, through a second path
            # the permission does not watch -- and an object already in a bucket
            # cannot be un-sent. It is also what puts the export behind the
            # withdrawal recheck below, which keys off `layer_b`.
            #
            # Counted separately for the same reason as `layer_b_files`, and rows
            # and members are kept apart because they disagree: a present-but-empty
            # allowlisted table is carried so a restore sees the real schema, which
            # is a `conversations/` root with zero rows. Folding that into the row
            # count alone would let the "nothing to archive" guard below throw away
            # members this already wrote.
            # Gated on the grant's SCOPE, not just on the permission. Both reasonless
            # exits here are the operator's own decision rather than a failed read: no
            # grant at all, and a grant whose recorded scope does not reach this
            # payload. Per the invariant this module walks, a policy decline may be
            # reasonless -- and deliberately sets NO `conversations_skipped`, because
            # that field suppresses the retention sweep. Writing one here would freeze
            # retention on EVERY install that granted Layer B before the export
            # existed, all at once, which is the unbounded-accumulation failure the
            # suppression exists to avoid rather than an instance of it.
            #
            # And suppressing nothing is SAFE here, which is the claim that makes the
            # reasonless exit legitimate rather than convenient. The sweep is only
            # dangerous when an earlier archive holds conversations this run does not,
            # and no released version wrote one: in every released build the sessions
            # archive has exactly the `crew` and `cli` roots and the export does not
            # exist. Nothing needs protecting, under any grant. The bound on that claim
            # is a host that ran an UNRELEASED build, which could hold conversations
            # under a legacy grant; that is a test host, not an operator install.
            #
            # Visibility is carried as the grant's STATE, the way the Layer B gate
            # itself is: `layer_b_scope` below says the grant covers `cli`, rather than
            # claiming an export was skipped.
            conversations = (
                _export_cli_conversations(tar)
                if layer_b_conversations
                else _ConversationExport(0, 0)
            )
            count += conversations.rows
        if count == 0 and conversations.members == 0:
            raise RuntimeError("no session files to archive")
        # Take hold of the finished archive ONCE and read nothing by name after.
        # ``_open_pinned_archive_fd`` opens it O_NOFOLLOW relative to the pinned
        # staging directory and refuses a symlink, a non-regular file, a hard link,
        # or a file this process does not own -- so what the descriptor reaches is
        # the archive this run just wrote, and the size, both fingerprints and the
        # upload all read THAT descriptor.
        archive_fd = _open_pinned_archive_fd(tmp_dir, dir_fd, name)
        try:
            # The descriptor was taken on the archive this run wrote and every later
            # step reads it, so the size, the tree digest, the body digest and the
            # uploaded bytes all describe one object reached without re-resolving the
            # name.
            #
            # `volatile_root=False`: this archive's roots are `crew` and `cli`, which
            # are meaningful and stable. Only the snapshot bundle carries a timestamped
            # root. The entry set is read from the pinned descriptor (``fd``), not by
            # re-opening the name.
            tree = _tree_fingerprint(None, volatile_root=False, fd=archive_fd)
            baseline = _unchanged_baseline(
                account, KIND_SESSIONS, tree, profile, region, bucket, caller=caller
            )
            if baseline is not None:
                record = _record_skip(
                    account,
                    KIND_SESSIONS,
                    baseline,
                    tree,
                    layer_b=(layer_b_files > 0 or conversations.members > 0),
                    conversations_skipped=conversations.skipped,
                    layer_b_scope=layer_b_scope,
                )
                if record is not None:
                    logger.info(
                        "aws-control: sessions backup for %s found both session trees unchanged "
                        "since the archive already in the drive, so it uploaded nothing",
                        account,
                    )
                    # As on the snapshot path, the label follows a push, so a local rename
                    # reaches the drive on the next real upload rather than on this skip.
                    # The retention sweep also stays on the path that pushed a new archive.
                    return record
                logger.info(
                    "aws-control: sessions backup for %s could not record its skip because "
                    "the recorded baseline moved while the archive was being built, so it "
                    "is uploading a full copy",
                    account,
                )
            key = f"{KIND_SUBPATHS[KIND_SESSIONS]}/{identity['id']}/{name}"
            # A WITHDRAWAL landing during the build must not ship. The permission is
            # read once at the top so one answer decides the whole tar, and that
            # invariant is deliberate -- but it leaves a window: enabled at the
            # read, withdrawn while the tar is written, and these bytes upload under a
            # permission the operator has withdrawn. Re-reading and REFUSING closes it
            # without breaking the invariant, because nothing is uploaded and nothing
            # is recorded, so there is no record to disagree with anything. Rebuilding
            # without Layer B instead would be the torn state the read-once rule
            # exists to prevent.
            #
            # Skipped on ONE path -- the attended owner's withheld run -- and when held,
            # taken BEFORE `_authorize_upload` so the whole decision-to-upload span is one
            # critical section. `_authorize_upload` states the invariant both halves of that
            # serve -- no check is separated from the upload by another blocking call.
            # Acquiring the lock after the authorization would put a blocking wait
            # between the consent check and `put_file`, because a concurrent account's
            # backup can hold this lock across its own upload and the recheck below
            # covers Layer B rather than consent.
            #
            # Which path may skip it is decided by what is RE-READ inside the block, not
            # by `layer_b` alone. The recheck below short-circuits when `layer_b` is
            # False, so it contributes no second read there -- but `_authorize_upload`
            # re-reads the unattended grant for a SCHEDULED caller, and that grant's
            # setter (`set_nightly_sessions`) writes under this same sidecar lock. The
            # crew display half rides on every run, withheld or not, so a scheduled
            # withheld run still has a permission that can be withdrawn mid-block and a
            # payload that ships if the withdrawal is missed. It keeps the lock.
            #
            # The attended owner's withheld run is the one shape with neither: both
            # scheduled-only re-reads are skipped, the recheck short-circuits, and what
            # remains -- `is_app_enabled`, `aws_consent`, STS -- is not stored in this
            # module's state file and takes no lock of ours, so an exclusive hold would
            # order nothing. Taking none satisfies the invariant directly:
            # `_authorize_upload` and `put_file` sit adjacent with no blocking call
            # between them. Taking one costs what an exclusive hold costs -- the lock
            # file is `_state_path()`'s sidecar, one path for every account, so every
            # state writer of every account (`_record_run`, `set_sessions_layer_b`,
            # `set_retention_keep`, the nightly loop) waits out this upload up to
            # `_STATE_LOCK_TIMEOUT_SECS` for a guarantee this one path does not need.
            # `contextlib.nullcontext` keeps that as one expression, so the body below
            # reads the same either way.
            #
            # `_upload_lock`, not `_state_lock`: the sidecar FILE lock alone, without
            # `_run_lock`. The setters (`set_sessions_layer_b` and `set_nightly_sessions`,
            # each -> `_locked_state_update` -> `_state_lock`) take this same file lock
            # exclusively, so an exclusive hold here still orders a revocation wholly
            # before or wholly after this block, in this process and in a second install
            # writing the same state -- the guarantee this gate exists for is untouched.
            # `_run_lock` ALSO
            # serializes `last_runs`, which the dashboard's backup-status read goes
            # through, so holding it across a PUT allowed `_PUSH_TIMEOUT_SECS` would
            # stall every account's status surface for one account's upload -- which is
            # why this block does not take it. Same shape, and same reason, as
            # `_delete_under_the_retention_gate`: it composes the sidecar file lock with
            # a dedicated gate rather than `_run_lock`, so a purge does not stall the
            # status read either.
            #
            # Nothing inside the block re-enters this lock. `_authorize_upload` reaches
            # `is_app_enabled`, `aws_consent`, an STS call, `_refuse_upload`, and -- for a
            # scheduled caller -- the unattended grant readers and
            # `scheduled_sessions_blocked_reason`. The last two READ this module's state
            # file, which is what the hold above orders them against, but they read it
            # without taking the lock, so naming them here costs no reentrancy. The list
            # is written out in full deliberately: a list that stops at STS reads as
            # though the withheld path has no permission left to lose, which is the
            # reasoning the hold above exists to refuse.
            #
            # The run record is written after the block. `_record_run` reaches the
            # same file lock through `_state_lock`, but only after this block has
            # released, and it holds no `_run_lock` while it waits for it -- so it
            # cannot deadlock against this block and it cannot drag the status read in
            # with it. Both halves of that are load-bearing: omitting `_run_lock` HERE
            # is not enough on its own, because the stall arrives through the contending
            # writer rather than through this block. See the lock-order note above
            # `_state_lock`. The
            # retention sweep takes the same FILE lock under `_RETENTION_GATE`, but it
            # runs after this block has released, not inside it.
            #
            # The cost, on the permitted path, is that a same-account revocation and the
            # nightly loop wait for the in-flight upload, bounded by
            # `_PUSH_TIMEOUT_SECS`. A revocation that appears slow is the price of one
            # that cannot be overtaken, and the exposure it prevents has no recovery.
            # What does NOT wait is every status read: `last_runs` and
            # `uploaded_objects` take only `_run_lock`, which neither this block nor a
            # writer parked on the file lock holds.
            # Stated in the positive and checked in the negative, so a caller nobody
            # anticipated holds the lock rather than skipping it -- the direction to be
            # wrong in, since what the lock orders is unrecoverable once missed.
            withheld_and_attended = not layer_b and caller == CALLER_OWNER
            with contextlib.nullcontext() if withheld_and_attended else _upload_lock():
                # The live checks: the connection still points at this account, the app
                # is still enabled, and consent still stands. Immediately before the
                # upload, and under the lock when one is held, so none of them can go
                # stale between here and the upload.
                _authorize_upload(
                    account, profile, region, caller=caller, payload_kind=KIND_SESSIONS
                )
                # Only the withdrawn direction refuses. A grant landing mid-build leaves
                # an archive without Layer B, which is the withholding default and needs
                # no refusal -- the next run picks the grant up.
                #
                # Through `_refuse_upload` rather than a bare raise, so the refusal lands
                # in the SEL beside every other refused upload. A withdrawn permission is
                # exactly the denial an incident review looks for, and one refusal path
                # that leaves no record would make the audited ones look complete. It
                # takes no state lock itself, so it is safe to reach from in here.
                if layer_b and not sessions_layer_b_enabled(account):
                    _refuse_upload(
                        account,
                        "the Layer B permission was withdrawn while this archive was being"
                        " built, so it was not uploaded; start the backup again to store"
                        " the transcript half",
                        caller=caller,
                    )
                # The SCOPE is rechecked on the same footing, because the grant staying on
                # does not mean it still covers this payload. A disable followed by an
                # enable that names no scope leaves the permission ON with the marker gone,
                # so the check above passes while the conversations already written into
                # this tar sit outside what the grant now covers -- and the object cannot be
                # recalled once it is PUT. Same asymmetry as above: only the withdrawn
                # direction refuses, since a scope granted mid-build leaves an archive
                # without the
                # conversations, which is the withholding default and needs no refusal.
                if layer_b_conversations and not layer_b_grant_covers_conversations(account):
                    _refuse_upload(
                        account,
                        "the Layer B conversation scope was withdrawn while this archive"
                        " was being built, so it was not uploaded; start the backup again"
                        " to store the transcript half",
                        caller=caller,
                    )
                version = storage.put_file(
                    profile,
                    region,
                    bucket,
                    "backup",
                    key,
                    str(archive),
                    account=account,
                    timeout=_PUSH_TIMEOUT_SECS,
                    body_fd=archive_fd,
                )
            record = _record_run(
                account,
                KIND_SESSIONS,
                key,
                os.fstat(archive_fd).st_size,
                _body_fingerprint(fd=archive_fd),
                version,
                tree=tree,
                layer_b=(layer_b_files > 0 or conversations.members > 0),
                conversations_skipped=conversations.skipped,
                layer_b_scope=layer_b_scope,
                # Records that THIS archive carries a `conversations/` root, so a later run
                # that carries none knows an older archive is the only copy. Keyed on
                # MEMBERS rather than rows, because a present-but-empty allowlisted table is
                # still carried and a restore still needs it.
                conversations_retained=conversations.members > 0,
            )
            # After the archive and after the ledger write, and with its own
            # authorization: a caption must never delay or endanger the payload.
            _publish_label(account, profile, region, bucket, identity, caller=caller)
            # LAST, and after a push that succeeded. This is the only step here that
            # deletes, so it runs once everything proving this run worked is already
            # done -- and it cannot fail the run. See _prune_remote_archives.
            #
            # SKIPPED whenever the conversation export reported ANY reason. Deliberately a
            # predicate on the field, not membership in a list of reasons: every reason this
            # module emits belongs in that list, so the list was only a slower way of
            # writing "any reason at all" -- and a sixth reason added later by someone who
            # never read this comment cannot be forgotten from a predicate, while it can
            # absolutely be forgotten from a frozenset. The two lists would have had to be
            # kept in sync forever, with a silent data-loss bug as the cost of drift.
            #
            # This includes a relocation. The relocation flag is read from THIS PROCESS's
            # environment, so a daemon-launched run and a shell-launched run can disagree
            # about it with the operator relocating nothing -- which means an earlier archive
            # really may hold conversations this one does not.
            #
            # Retention protects only the key this run just
            # uploaded, so at `keep=1` retiring the previous archive would erase the one
            # copy that still held the conversations, and `delete_object_versions` erases
            # versions outright -- there is no recovery for the retired object, while the
            # gap here recovers on the next successful run. Keeping one archive too many
            # costs storage; retiring the last complete one costs the data. The archives
            # accumulate past the keep count only while the condition persists, and
            # `conversations_skipped` in the record is what tells the operator why.
            #
            # It does NOT over-suppress, but the line is not where it first looks. What may
            # stay reasonless is a run that READ everything the host holds, or one where the
            # operator withheld the permission -- a consented withdrawal, and the default,
            # so an ordinary install prunes exactly as it did before this feature existed.
            # An ABSENT store does not qualify: the question is whether an earlier archive
            # holds rows this one does not, and a store missing at nightly-run time may have
            # been present when that archive was written. The accepted cost is narrow
            # because the permission is owner-gated and defaults off -- only a host that
            # opted INTO conversation backup and has no store to back up stops pruning, and
            # that combination is a misconfiguration the record now names rather than a
            # working state.
            #
            # THE DECLINE IS AUDITED. Suppressing the sweep suppresses the DELETION, never
            # the record of the decision: `_audit_retention` is only reachable from inside
            # `_prune_remote_archives`, so an early return here would have filed no SEL
            # event at all, on the one path in the app that erases object versions for good
            # -- exactly the invisibility that function exists to prevent, and its contract
            # says every terminal outcome files one "including the ones that deleted
            # nothing". A decline is a terminal outcome. It is filed as `failed` with the
            # reason as `error`, matching the sweep's own refusal-to-act on a listing that
            # does not show the archive just uploaded: nothing was deleted and the operator
            # needs to know why, which is not the same as a withdrawn consent (`denied`
            # belongs to the gate). This is also what keeps the accumulation VISIBLE: while
            # the condition persists the archives pile up past the keep count, and one event
            # per run naming the reason is how an auditor sees that rather than inferring it
            # from a sweep that silently never ran.
            # TWO independent conditions, not one replacing the other. The first is this
            # run's own export coming up short, which is the `skipped` reason above. The
            # second is this run carrying no conversations at all while an older RETAINED
            # archive carries them -- which the grant's own scope can produce with nothing
            # wrong: an in-scope run uploads conversations, the scope is then narrowed, and
            # the next run's archive omits them while the sweep would retire the one that
            # holds them. That path sets no skip reason, because a scope the operator
            # narrowed is a policy decline rather than a failed read, so the first condition
            # cannot see it.
            #
            # Expressed as a predicate over one persisted boolean rather than a set of
            # qualifying cases -- same reason the `skipped` suppression is a predicate: a
            # second list to keep in sync is a place to forget one, and the cost of
            # forgetting here is a permanent delete.
            decline = conversations.skipped
            if not decline and conversations.members == 0:
                if a_retained_archive_carries_conversations(account):
                    decline = "conversations_retained_in_an_older_archive"
            if decline:
                logger.warning(
                    "aws-control: skipping the sessions retention sweep for %s (%s), so an "
                    "older archive that may hold conversations this one does not is kept",
                    account,
                    decline,
                )
                reason = f"retention declined: {decline}"
                declined: dict[str, Any] = {
                    "kind": KIND_SESSIONS,
                    # "off" is reserved for "no count configured". The count may well be
                    # set here; this run simply did not act on it, which `result` and
                    # `error` say. Naming a number would claim a sweep that never ran.
                    "keep": "declined",
                    "live": 0,
                    "retired": 0,
                    "versions": 0,
                    "unclaimed": 0,
                    "unclaimedBytes": 0,
                    "unrecorded": 0,
                    "unrecordedBytes": 0,
                    "skipped": "",
                }
                _audit_retention(
                    account,
                    declined,
                    caller=caller,
                    result="failed",
                    error=reason,
                )
                # Published as well as audited, for the reason every ending inside the
                # sweep is: without it the sessions row keeps serving whatever the last
                # sweep that RAN said, so a decline repeated every night would read as
                # the success before it. The reason rides in `skipped` here only; the
                # audit event carries it as `error`, as it always has.
                _record_last_sweep(
                    account, KIND_SESSIONS, {**declined, "skipped": reason}, "failed"
                )
            else:
                _prune_remote_archives(
                    account,
                    profile,
                    region,
                    bucket,
                    KIND_SESSIONS,
                    identity["id"],
                    key,
                    caller=caller,
                    # Only when THIS run carried no conversations. A run that carried them
                    # set the fact itself, so re-checking would refuse its own sweep.
                    recheck_conversations_retained=conversations.members == 0,
                )
            return record
        finally:
            os.close(archive_fd)


#: The two Job SDK kinds this app registers. Same strings as ``KIND_*`` so a run
#: record read by a human names the backup the owner asked for.
JOB_KINDS = (KIND_SNAPSHOT, KIND_SESSIONS)


def _nightly_subject(kind: str) -> str:
    """The SEL subject for one scheduled backup kind. One spelling, so a run's
    records all file under the same subject for the kind."""
    return f"backup/{KIND_SUBPATHS[kind]}"


def _nightly_audit(operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """SEL record for an UNATTENDED backup step.

    The HTTP handlers get their audit from the dashboard layer; a scheduled run
    has no request, so without this the one S3 mutation this app makes with nobody
    present would leave no trail of having run, succeeded, found nothing to do, or
    failed. Best-effort by the same rule the handlers use: an audit failure must
    never abort the backup.
    """
    try:
        sel().log_api_access(
            caller="aws-control-nightly",
            operation=f"aws_control.{operation}",
            outcome=outcome,
            source=APP_NAME,
            resources=resources[:200],
            error=error[:200],
        )
    except Exception:
        logger.debug("aws-control nightly SEL audit failed", exc_info=True)


def make_job_runner(sdk: Any, kind: str) -> Any:
    """Build the Job SDK runner for ``kind``. Registered once, at app startup.

    A PLAIN ``def``, and it must stay one. ``JobSDK._execute`` calls the runner
    and DISCARDS its return value, so an ``async def`` here would hand back a
    coroutine nobody awaits: the body would never execute, nothing would raise,
    and the record would settle on ``done`` reporting a backup that never
    happened. ``register()`` validates the kind and not the callable, so this
    property is the app's to keep.

    That constraint is what shapes the resolution below. The SDK gives a runner
    its handle and nothing else, so the run's target is read back out of its own
    record, where ``start`` put it:

    * The ACCOUNT comes from ``dedupe_key``. It is the right carrier on its own
      merits, because the account is exactly this run's concurrency identity --
      two snapshot backups of one account must not both do the paid upload, and
      the SDK's index is ``(kind, dedupe_key)`` so snapshot and sessions for the
      same account still run independently. It is also the only field a runner
      can read without a private attribute (``get`` is public; the key is
      withheld from the HTTP view and never logged by the SDK).
    * The CALLER role comes from ``handle.params`` under ``JOB_PARAM_CALLER``.
      This is what lets ONE runner serve both the owner click and the nightly
      loop: the loop states ``CALLER_SCHEDULED`` when it starts the run, and this
      reads it back so the run applies the scheduled-only gates. An absent or
      unrecognised value is the owner path, the safe default. Because both
      callers share one ``(kind, dedupe_key)``, an owner's "Back up now" during a
      nightly ADOPTS the in-flight scheduled run -- the SDK returns the first
      caller's run and params do not merge, so the adopted run keeps
      ``CALLER_SCHEDULED`` and still runs the scheduled-only gates. The rule that
      keeps this honest for the owner is in the failure handler below: a
      scheduled-only precondition the owner controls (``ScheduledUploadWithheld``:
      the unattended grant is withdrawn, or scheduled transcripts are blocked here)
      is audited ``withheld`` and NOT fed into the nightly backoff, so an owner
      whose adopting click surfaces one of those gates never sees the schedule's
      backoff grow for a run they asked for.
    * profile/region/bucket are RE-RESOLVED here rather than carried, which is
      the rule this app already documents for the nightly loop: the drive is
      tag-discovered per run rather than trusted from memory. The one exception
      is the SCHEDULED path's profile/region: ``resolve_account_profile_cached``
      serves only from the warm snapshot and returns ``None`` once it is past
      ``accounts._PROBE_TTL_SECS``, and ``start_async`` is fire-and-forget, so
      this runner resolves LATER than the loop's live probe -- a gap longer than
      the TTL would audit a just-verified nightly as failed. The loop names the
      ``(profile, region)`` it freshly probed under ``JOB_PARAM_PROFILE`` /
      ``JOB_PARAM_REGION``, and a scheduled run falls back to it when the cache
      has expired. Wrong-account risk stays closed: the live
      ``sts:GetCallerIdentity`` below re-verifies the account before any byte
      leaves. The bucket is still always re-discovered.

    Every resolution step is therefore sync. ``accounts.resolve_account_profile``
    and ``aws_consent.authorize`` are coroutines and are NOT reachable from a
    worker thread -- ``asyncio.run`` would build a second event loop, which is
    the failure this package already carries a ``LoopBoundLock`` to avoid
    -- so this uses the sync cached resolver and lets the sync
    :func:`_authorize_upload` gate inside each runner make the paid-service
    decision. That gate is the real one: it re-checks the LIVE account against
    the target, that the app is still enabled, that S3 consent still holds for
    this profile+region, and that the recorded grant names THIS account, all
    immediately before ``put_file``. So a run started through the generic
    ``_jobs`` surface, which does not pass this app's HTTP pre-flight, is
    authorized by the same gate as one started through it.

    Refusals raise. ``_execute`` records the exception's text as the run's
    ``error`` and the status as ``failed``, which is the honest terminal state
    for a request that named no reachable target. The messages deliberately do
    NOT quote the dedupe key: it is caller-supplied, and the SDK withholds it
    from both the log and the HTTP view for that reason.
    """
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown backup job kind: {kind!r}")

    def _run(handle: Any) -> None:
        run = sdk.get(handle.run_id)
        account = run.dedupe_key if run is not None else ""
        # The caller role rides in `params` under JOB_PARAM_CALLER; the account
        # already rides in `dedupe_key`. This is the one extra fact a start has to
        # carry so a single registered runner serves BOTH the owner click and the
        # nightly loop: the loop states CALLER_SCHEDULED, and reading it back here
        # is what makes `_authorize_upload` and `work` apply the unattended grant
        # and sessions-redaction re-reads a scheduled payload must honour. Absent,
        # empty or unrecognised means the owner path -- the safe default, since
        # only the scheduled loop sets it and the owner path is the fuller one at
        # the drive gate.
        params = handle.params if hasattr(handle, "params") else {}
        caller = (
            CALLER_SCHEDULED if params.get(JOB_PARAM_CALLER) == CALLER_SCHEDULED else CALLER_OWNER
        )
        # An empty key reaches here from `POST /_jobs/{kind}/start` with no body:
        # the generic surface defaults `dedupe_key` to "". There is no account to
        # act on, and picking one would be acting on an account nobody named.
        if not account:
            raise RuntimeError("this backup run names no account; nothing was sent to AWS")
        if not (account.isdigit() and len(account) == 12):
            raise RuntimeError(
                "this backup run does not name an account id; nothing was sent to AWS"
            )
        # The run slot's identity, read BEFORE the attempt and only for a scheduled
        # run: `record_nightly_failure` refuses to write when the slot has moved
        # since, so the witness has to be the slot as it stood before this attempt
        # could change it. An owner run never records a backoff (someone is present
        # and sees the failure), so it needs no witness.
        scheduled = caller == CALLER_SCHEDULED
        run_witness = nightly_run_witness(account, kind) if scheduled else None
        try:
            resolved = accounts_mod.resolve_account_profile_cached(account)
            if resolved is None and scheduled:
                # The cache serves only from the warm snapshot and returns None once
                # it is past `accounts._PROBE_TTL_SECS`. `start_async` is
                # fire-and-forget, so this runner thread resolves LATER than the
                # nightly loop's live `probe_identity` check; a gap longer than the
                # TTL would audit a just-verified nightly as failed. The loop names
                # the `(profile, region)` it freshly probed in `params`, so fall back
                # to it rather than re-failing. No wrong-account risk: the live
                # `sts:GetCallerIdentity` in `_authorize_upload` below still verifies
                # the account before anything leaves. The owner path keeps the
                # cache-only resolve: its route pre-flight has just warmed the snapshot.
                verified_profile = params.get(JOB_PARAM_PROFILE) or ""
                verified_region = params.get(JOB_PARAM_REGION) or ""
                if verified_profile and verified_region:
                    resolved = (verified_profile, verified_region)
            if resolved is None:
                raise RuntimeError(
                    "no working connection for this account — reconnect it, "
                    "then run the backup again"
                )
            profile, region = resolved
            # Authorize BEFORE discovery, not just before the upload. `find_drive`
            # reaches AWS to resolve the bucket by tags, so with consent withdrawn or
            # the app disabled the old order sent tagging-API requests on the owner's
            # credentials before any gate had run -- unauthorized calls made in the
            # course of refusing the work. The gate needs no bucket, so nothing forces
            # it to wait for discovery.
            #
            # This does NOT replace the pre-upload re-check inside `work`: an archive
            # build takes minutes, and consent can be withdrawn during it. This one
            # decides whether we may touch AWS at all; that one decides whether the
            # bytes may leave. Both are needed, and both audit through the same helper.
            _authorize_upload(account, profile, region, caller=caller, payload_kind=kind)
            bucket = storage.find_drive(profile, region, account=account)
            if not bucket:
                # The scheduled loop checks the drive BEFORE claiming a run, so a
                # scheduled run never reaches here with no drive; this guards the
                # generic `_jobs` entry point, where it is an honest failure for a
                # request that named a target with nowhere to push.
                raise RuntimeError("this account has no drive yet; nothing was sent to AWS")
            # A scheduled run spends the owner's money with nobody present, so it
            # records that another install already backs up to this drive -- evidence
            # a later panel view cannot reconstruct, since the console's `others`
            # count is LIVE. A NOTICE, not a gate: the keys carry an install id, so
            # two installs write to two prefixes and refusing would silently stop
            # backing one machine up. ONE list call, against the snapshot prefix only
            # (so the snapshot kind carries it, not sessions): a both-due wake must
            # not pay a second LIST, and a sessions-only wake must not pay a
            # snapshot-prefix LIST for a question about a run that is not happening.
            # Never raises -- a listing failure is one less log line, not a missed
            # nightly.
            if scheduled and kind == KIND_SNAPSHOT:
                try:
                    others = other_install_ids(profile, region, bucket, account=account)
                except Exception:
                    others = []
                    logger.debug("aws-control nightly: shared-drive check failed", exc_info=True)
                if others:
                    logger.warning(
                        "aws-control nightly: %d other install(s) also back up to this "
                        "drive (%s); each writes under its own prefix, so this run proceeds",
                        len(others),
                        ", ".join(others[:5]),
                    )
                    _nightly_audit("backup_shared_drive", f"installs={len(others)}", "invoked")
            # Resolved by NAME at call time, not captured at registration: the module
            # attribute stays the single definition of what a snapshot backup is.
            work = run_snapshot_backup if kind == KIND_SNAPSHOT else run_sessions_backup
            # Both callers reach this one runner now: an owner click through the app's
            # route or the `_jobs` surface, and the nightly loop, which claims its run
            # through `start_async` and names CALLER_SCHEDULED in `params` so this run
            # applies the unattended gates rather than attributing a 03:00 push to a
            # human who is not present.
            #
            # A scheduled run has no HTTP handler to add the audit the dashboard layer
            # puts on every owner-driven mutation, so it emits the same invoked /
            # (succeeded|unchanged) / failed trail here. The split matters: a run that
            # found the tree unchanged sent nothing, so recording it as a push would
            # file a SEL entry against a key no bytes reached tonight -- which the SDK's
            # own `done` record cannot tell apart from an ordinary upload.
            if scheduled:
                _nightly_audit("backup_nightly", _nightly_subject(kind), "invoked")
            record = work(account, profile, region, bucket, caller=caller)
            if scheduled:
                if isinstance(record, dict) and record.get("uploaded") is False:
                    _nightly_audit("backup_nightly", str(record.get("key", "")), "unchanged")
                else:
                    key = str(record.get("key", "")) if isinstance(record, dict) else ""
                    _nightly_audit("backup_nightly", key, "succeeded")
        except Exception as exc:
            # A SCHEDULED failure feeds the loop's backoff so a deterministic fault
            # is not re-attempted every wake (`due_for_nightly` reads this record,
            # not the SDK run), then re-raises so `_execute` still records the run
            # `failed`. The loop only claims a scheduled run once a drive exists, so
            # "no drive yet" never reaches here as a scheduled fault. A TEARDOWN is
            # not a fault: app disable / gateway stop surface as the stop-event
            # refusal `_authorize_upload` raises once `_STOP` is set, and counting
            # that would let a restart push the next night out, so it is recorded
            # nowhere. A scheduled-only precondition the owner controls -- the
            # unattended grant withdrawn, or scheduled transcripts blocked here
            # (`ScheduledUploadWithheld`) -- is likewise not a fault: it is the owner's
            # own setting declining the upload, so it is audited `withheld` and NOT
            # recorded into the backoff. This is the rule an ADOPTED run keeps: because
            # both callers share one `(kind, dedupe_key)`, an owner's "Back up now"
            # during a nightly adopts the in-flight scheduled run (the SDK keeps the
            # first caller's `CALLER_SCHEDULED` params; they do not merge), so without
            # this branch an owner surfacing one of these gates would see a failure they
            # did not cause and watch the backoff grow. An owner run just re-raises --
            # its failure is the owner's to see, not the schedule's.
            if scheduled and isinstance(exc, ScheduledUploadWithheld):
                # Owner-controlled withdrawal, not a fault: audited as withheld and
                # NOT recorded, so a withdrawn grant cannot push the next night out.
                _nightly_audit("backup_nightly", _nightly_subject(kind), "withheld")
            elif scheduled and not _STOP.is_set():
                _nightly_audit("backup_nightly", _nightly_subject(kind), "failed", error=str(exc))
                record_nightly_failure(account, kind, str(exc), run_witness=run_witness)
            elif scheduled:
                # Teardown, not a fault: audited as such and NOT recorded, so a clean
                # shutdown cannot push the next night out.
                _nightly_audit("backup_nightly", _nightly_subject(kind), "cancelled")
            raise

    return _run


# ---------------------------------------------------------------------------
# Composition: one import path and one patch surface over the owners
# ---------------------------------------------------------------------------
# The engine is split by responsibility across ``backup_parts``, and this module is
# its only import path. Callers and tests reach every name as ``backup.X`` -- private
# helpers included, because tests read and patch them -- so two properties hold.
#
# 1. A read answers with the object the owner holds. A name this module does not use
#    itself is NOT bound here: ``__getattr__`` reads it from its owner on each
#    access, through ``sys.modules``, which is the one-storage rule
#    ``test_mirrored_owner_storage.py`` enforces on every module of this shape. The
#    names this module's own functions use are bound here by ordinary imports, the
#    same way each part binds what it imports from a lower part.
# 2. A write reaches every binding of the name. A part resolves a name through its
#    own globals, and so does every part that imported it, so a patch that landed
#    only on this module would leave the code under test running the unpatched
#    object -- the test would pass while testing nothing. ``_Facade`` therefore writes
#    the value into every module that holds the name, which keeps the engine one
#    namespace for writes: shadowing a builtin reaches every part as well. Patch the
#    facade, never a part directly: a write into one part reaches no other holder.
#
# One consequence of (1) is visible to a patch harness. ``mock.patch`` undoes a name
# this module does not bind by deleting it and then writing the original back -- and
# under ``create=True`` it only deletes -- and the delete reaches every holder. So a
# patch of such a name never passes ``create=True`` (the composition-contract test fails
# on any test module that patches a forwarded name with ``create=True``, apart from its
# one allowlisted premise case), and a thread started inside it is joined before the
# patch ends.
#
# The machinery below holds dotted module NAMES, never module objects, and reads
# ``sys``, ``importlib`` and ``builtins`` through private aliases a patch of
# ``backup.sys`` cannot redirect. The composition-contract test pins that every
# module holding a name holds the SAME object, so a name and the symbol it denotes
# cannot come apart.

#: The owners, lowest layer first. A part imports only parts earlier in this order,
#: so for a name the package defines, the first part holding it is its definer; a
#: name a part imports from outside the package resolves from its first importer,
#: which holds the same object as every other holder.
_PART_MODULES: tuple[str, ...] = tuple(
    f"{__name__.rpartition('.')[0]}.backup_parts.{leaf}"
    for leaf in (
        "egress_text",
        "state",
        "identity",
        "fingerprints",
        "traversal",
        "ledger",
        "layer_b",
        "nightly",
        "uploads",
        "catalog",
        "retention",
        "archive_inputs",
        "conversations",
        "restore",
    )
)

#: Builtin names, which a module shadows by binding them in its own namespace.
_BUILTIN_NAMES = frozenset(name for name in vars(_builtins) if not name.startswith("__"))


def _part(module: str) -> _ModuleType:
    """Return one part, read from where modules are stored.

    :data:`sys.modules` answers first, so a purged or replaced part is seen at once.
    ``importlib.import_module`` answers only a miss: it is an attribute any test can
    patch, and resolving every read through it would reroute this whole surface to
    that patch while it is installed.
    """
    try:
        return _sys.modules[module]
    except KeyError:
        return _importlib.import_module(module)


def _holder_tables() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """``(exported, also_held)``: the parts holding each name, lowest layer first.

    A name this module binds itself goes to the second table, and only the parts that
    hold the SAME object count as holders of it; every other name a part holds goes to
    the first.
    """
    own = globals()
    exported: dict[str, list[str]] = {}
    also_held: dict[str, list[str]] = {}
    for module in _PART_MODULES:
        for name, value in list(vars(_part(module)).items()):
            if name.startswith("__"):
                continue
            if name in own:
                if own[name] is value:
                    also_held.setdefault(name, []).append(module)
            else:
                exported.setdefault(name, []).append(module)
    return (
        {name: tuple(holders) for name, holders in exported.items()},
        {name: tuple(holders) for name, holders in also_held.items()},
    )


_holder_split = _holder_tables()

#: Name -> the parts that hold it, lowest layer first, for every name a part holds and
#: this module does not bind. A read resolves the first; a write reaches them all.
_EXPORTS: dict[str, tuple[str, ...]] = _holder_split[0]

#: Name -> the parts that hold a name this module ALSO binds for its own functions.
#: A read answers from the binding here; a write reaches this module and all of them.
_ALSO_HELD: dict[str, tuple[str, ...]] = _holder_split[1]

del _holder_split


def _holders(name: str) -> tuple[str, ...]:
    """The parts a write of ``name`` through this module has to reach."""
    held = _EXPORTS.get(name) or _ALSO_HELD.get(name)
    if held is not None:
        return held
    return _PART_MODULES if name in _BUILTIN_NAMES else ()


if _typing.TYPE_CHECKING:
    # The exported names, as the type checker sees them: every one of them resolves
    # from its owner at run time through ``__getattr__`` below, which a checker is not
    # shown, so a misspelled or mis-called ``backup.X`` stays a type error. The
    # composition-contract test pins this list equal to ``_EXPORTS``.
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.archive_inputs import (  # noqa: F401
        _platform_compat,
        is_link_or_junction,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.catalog import (  # noqa: F401
        MAX_OTHER_INSTALLS,
        _archive_row,
        _archive_sort_key,
        _checked,
        _install_folders,
        list_remote_backups,
        read_remote_label,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.conversations import (  # noqa: F401
        _CONVERSATION_BATCH_ROWS,
        _CONVERSATION_MAX_CELL_BYTES,
        _CONVERSATION_READ_ID,
        _CONVERSATION_TABLES,
        _CONVERSATIONS_ARC_PREFIX,
        _CONVERSATIONS_DB_ARCNAME,
        _CONVERSATIONS_MANIFEST_ARCNAME,
        IO,
        NamedTuple,
        _add_bytes,
        _add_open_file,
        _conversation_scratch_parent,
        _copy_table,
        _kiro_cli_conversation_db,
        _open_pinned_scratch,
        _redacted_row,
        _refuse_an_oversized_cell,
        _store_relocated_outside_the_fence,
        _store_write_time,
        first_linked_ancestor,
        hooks,
        sqlite3,
        state_db_candidates,
        sys,
        urllib,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import (  # noqa: F401
        LABEL_MAX_CHARS,
        _redact_egress,
        redact_credentials,
        redact_exfiltration_urls,
        sanitize_label,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.fingerprints import (  # noqa: F401
        _SNAPSHOT_MANIFEST_NAME,
        _VOLATILE_MANIFEST_FIELDS,
        _archive_entries,
        _entries_of,
        _is_provable_version_id,
        _manifest_digest,
        _OffsetReader,
        _read_at,
        hashlib,
        io,
        stat,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (  # noqa: F401
        _INSTALL_ID_RE,
        _KIND_BY_SUBPATH,
        INSTALL_KEY,
        KEY_SEP,
        ORIGIN_LEGACY,
        ORIGIN_OTHER,
        ORIGIN_SELF,
        ORIGIN_UNVERIFIED,
        UnprovenArchive,
        _default_label,
        _fallback_identity,
        _fallback_lock,
        _key_basename,
        _key_segments,
        _stored_identity,
        classify_key,
        re,
        secrets,
        set_install_label,
        uuid,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.layer_b import (  # noqa: F401
        SESSIONS_LAYER_B_KEY,
        SESSIONS_LAYER_B_SCOPE_KEY,
        SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS,
        _audit_layer_b_grant,
        set_sessions_layer_b,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import (  # noqa: F401
        _UNCONDITIONAL_RUN_WRITE,
        _record_run_locked,
        _run_process,
        _run_sequence,
        _uploaded_objects_locked,
        last_runs,
        remembered_archives,
        retention_owned_keys,
        uploaded_keys,
        uploaded_objects,
        uploaded_versions,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.nightly import (  # noqa: F401
        _NIGHTLY_CONSENT_READERS,
        BLOCK_HOST_UNSUPPORTED,
        BLOCK_OTHER_ACCOUNT,
        BLOCK_REDACTION_ON,
        FAILURE_ERROR_MAX_CHARS,
        NIGHTLY_RETRY_BACKOFF_SECS,
        NIGHTLY_WINDOW_SECS,
        Callable,
        _a_day_since_last_run,
        _backoff_withholds,
        _granted,
        _unattended_sessions_redaction_gap,
        clear_nightly_no_key_skip,
        due_for_nightly,
        due_for_sessions_nightly,
        nightly_enabled,
        nightly_enabled_any_account,
        nightly_failures,
        nightly_no_key_skip,
        nightly_retry_delay_secs,
        nightly_sessions_enabled,
        record_nightly_no_key_skip,
        scheduled_sessions_blocked_code,
        scheduled_sessions_blocked_reason,
        set_nightly,
        set_nightly_sessions,
        snapshot_redact,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.restore import (  # noqa: F401
        STAGING_NAME_MAX_BYTES,
        _recover_recorded_version,
        _staging_name,
        restore_download,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.retention import (  # noqa: F401
        _RETENTION_GATE,
        RETENTION_KEEP_MIN,
        RETENTION_KEEP_STATE_KEY,
        RETENTION_LAST_SWEEP_STATE_KEY,
        RETENTION_UNCLAIMED_STATE_KEY,
        RETENTION_UNRECORDED_STATE_KEY,
        _audit_unfiled_authorization,
        _authorization_ending,
        _clamp_retention_keep,
        _current_version_is_ours,
        _delete_under_the_retention_gate,
        _filed_by_the_gate,
        _newest_first,
        _prune_recorded_versions,
        _record_unclaimed,
        _record_unrecorded,
        _retention_keep_for_sweep,
        _RetentionAuthorizationWithdrawn,
        _RetentionCountWithdrawn,
        redact_log_via_context,
        retention_keep,
        retention_last_sweep,
        retention_unclaimed,
        retention_unrecorded,
        set_retention_keep,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (  # noqa: F401
        _AUTHORIZE_TIMEOUT_SECS,
        _FACADE_MODULE,
        _RUN_CONVERSATIONS_RETAINED,
        _STATE_LOCK_TIMEOUT_SECS,
        MAX_RECORDED_VERSIONS,
        MAX_REMEMBERED_UPLOADS,
        NIGHTLY_FAILURE_STATE_KEY,
        NIGHTLY_NO_KEY_STATE_KEY,
        SESSIONS_CONVERSATIONS_RETAINED_KEY,
        STATE_DIR_LEAF,
        Optional,
        _account_state,
        _account_view,
        _account_view_checked,
        _clear_nightly_failure,
        _forget_unpersisted,
        _held_runs,
        _locked_state_update,
        _merge_pending,
        _merge_unpersisted,
        _merge_uploads,
        _read_state_checked,
        _read_state_for_update,
        _release_persisted_versions,
        _remember_unpersisted,
        _run_is_newer,
        _run_lock,
        _set_conversations_retained,
        _state_key,
        _state_lock,
        _state_path,
        _StateUnreadable,
        _unpersisted_lock,
        _unpersisted_runs,
        _unpersisted_uploads,
        _unpersisted_versions,
        app_data_dir,
        atomic_write,
        errno,
        file_lock,
        open_lock_file,
        read_state,
        threading,
        write_state,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.traversal import (  # noqa: F401
        _MAX_TREE_DEPTH,
        _O_DIRECTORY,
        _O_NOFOLLOW,
        _O_NONBLOCK,
        _add_pinned,
        kind_unavailable_reason,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.uploads import (  # noqa: F401
        _TEARDOWN_REFUSAL,
        SEL_OP_BASELINE_PROBE,
        SEL_OP_RETENTION,
        SEL_OP_UPLOAD,
        AWSError,
        NoReturn,
        _authorize_recovery_read,
        clear_stop,
        signal_stop,
    )
else:

    def __getattr__(name: str) -> Any:
        """Read an exported name from the part that owns it (:pep:`562`)."""
        holders = _EXPORTS.get(name)
        if holders is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_part(holders[0]), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


class _Facade(_ModuleType):
    """Write a name into every module that holds it.

    An exported name is written to its parts only, so this module never holds a copy
    that would shadow the owner and go stale on the owner's next write -- including a
    ``global`` rebind inside the owner. ``monkeypatch`` and ``mock.patch`` restore by
    writing the remembered value back through here, so a patch and its undo reach the
    same bindings.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        for module in _holders(name):
            setattr(_part(module), name, value)
        if name not in _EXPORTS:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        for module in _holders(name):
            part = _part(module)
            if name in vars(part):
                delattr(part, name)
        if name not in _EXPORTS:
            super().__delattr__(name)


# ``from ... import *`` consults this list and never reaches ``__getattr__``, so it
# is derived to carry the public names a star import of one flat module would: the
# names bound here plus the exported ones, minus the private names a star import
# never carries.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _Facade

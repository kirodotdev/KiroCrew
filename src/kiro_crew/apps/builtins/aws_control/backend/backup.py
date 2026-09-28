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
  "Terminal conversations" below), pushed to
  ``backup/sessions/<install>/<stamp>.tar.gz``. Whole-set, not per-session:
  the "both halves move together" invariant is honoured by construction, and a
  run whose trees have not moved since the archive already in the drive uploads
  nothing at all -- see "Unchanged runs upload nothing" below. Splitting the set
  into per-session objects and sending only the changed ones is a different
  feature and deliberately not this one: the RFC lists incremental and
  deduplicating transfer among its non-goals.

**Terminal conversations (``conversations/`` root).** The two transcript halves
above are the GATEWAY's session state; the terminal (kiro-cli itself) keeps its
own conversations in ``conversations`` and ``conversations_v2`` inside
``~/.local/share/kiro-cli/data.sqlite3``, a store disjoint from both halves.
That file is ALSO the identity auth store -- ``hooks.py`` classifies it as a
token path and it holds live bearer tokens -- so the archive
must never carry the file. It carries a table-scoped export instead: a fresh
database holding ONLY the tables in ``_CONVERSATION_TABLES`` (an allowlist, so no
identity or token TABLE can leak even if kiro-cli adds one -- the bound is per
table and not per column, see ``_copy_table``), read from the live
store under a single read-only snapshot as ``_export_cli_conversations``
documents, under
the ``conversations/`` archive root beside ``crew`` and ``cli``. The store is
looked up among FIXED, home-anchored locations only: ``XDG_DATA_HOME`` /
``LOCALAPPDATA`` are not consulted, because the fence that keeps agent file tools
out of this store is home-anchored and does not follow a redirected root, and this
archive is uploaded off-host unattended. A host that relocates its store therefore
gets no ``conversations/`` root, and the run record says so through
``conversations_skipped`` rather than leaving an operator to infer it from an
absent member. The DECLARED
BOUNDARY of "conversation state" for this app is exactly the terminal's two chat
tables, ``conversations`` and ``conversations_v2``; a store that has not migrated
holds its rows in the first and a migrated one holds them in the second, so
carrying only one of them would leave an un-migrated install's conversations
behind while the run recorded a store with no conversation table at all.
If a future table is genuinely conversation state and not auth, it is added to
``_CONVERSATION_TABLES`` and this sentence is updated in the same change --
there is no other place the boundary is expressed. The export rides the SAME
standing permission as the ``cli`` half, :func:`sessions_layer_b_enabled`, and
invents no new grant: both carry what a model actually held, where
the crew transcript carries what was displayed with display-time redaction
applied. Neither is shipped raw: every text column of an exported conversation row
goes through ``_redacted_row`` first, so what leaves the host is
EGRESS-REDACTED, so an install whose operator withholds Layer B gets an archive with no
``conversations/`` root either. Reading the store is RECORDED through the
sanctioned credential-read audit
(``hooks.emit_internal_read_audit`` under ``aws_control.conversation_export``,
registered in ``hooks._AUDIT_ONLY_READ_IDS``), which runs after the read rather
than gating it -- it is an access log, not an authorization. What FAILS CLOSED is
the SHIPPING: an export whose
access cannot be recorded is dropped from the archive rather than shipped
unaudited, because the file holds live bearer tokens whatever this reader
touches.

**One drive can be reached by several installs.** Discovery is by tag, so a
second install finds the first one's bucket and writes to it by design — the
``<install>`` segment is what keeps the two apart afterwards, and it is a
random per-install id held in this app's own state, never the telemetry
install id. An archive uploaded before that segment existed carries no id and
is reported as being of unknown origin rather than claimed by whoever is
reading. The "install identity" note in ``backup_parts.identity`` says why the id
decides what is permitted while the human-readable label decides only what is
displayed.

**Restore is a download, deliberately.** A restore lands the archive in
``<app data dir>/restore/`` and hands back the path; nothing hot-swaps a
live ``memory.db`` or sessions dir under a running gateway. The snapshot
engine's own merge/replace tooling (or a stopped gateway) takes it from
there, and the UI copy says exactly that.

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
* ``retention`` -- the opt-in sweep.

What stays here is what those owners are composed INTO: both archive builders and
every outbound archive and label PUT, the terminal conversation export, the
screened tree walk, the staged restore, and the Job SDK runner. Several of these
are pinned to this file by name: the link-screen baseline declares four of their
functions, and the redaction-sink registry names this module as the backup
boundary. Every owner's names resolve here, and a write through this module reaches
every module holding the name, so patching ``backup.X`` changes what the engine runs
wherever that name is read. The composition note at the end of this module says how,
and what a patch harness sees of it.

CALLER CONTRACT: handlers hold the consent gate; sync, subprocess/tar-bound
— call via ``asyncio.to_thread`` (pushes of a large sessions set can run
minutes; handlers use generous timeouts).
"""

from __future__ import annotations

import builtins as _builtins
import contextlib
import datetime as dt
import hashlib
import importlib as _importlib
import io
import json
import logging
import os
import sqlite3
import stat
import sys
import sys as _sys
import tarfile
import tempfile
import typing as _typing
import urllib.parse
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import IO, Any, NamedTuple, Optional

from kiro_crew import hooks, snapshot
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import (
    _redact_egress,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.fingerprints import (
    _body_fingerprint,
    _is_provable_version_id,
    _tree_fingerprint,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    KIND_SESSIONS,
    KIND_SNAPSHOT,
    KIND_SUBPATHS,
    LABEL_OBJECT_NAME,
    ORIGIN_SELF,
    ORIGIN_UNVERIFIED,
    UnprovenArchive,
    _key_basename,
    _stamp,
    classify_key,
    install_identity,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.layer_b import (
    _audit_layer_b_decision,
    layer_b_grant_covers_conversations,
    sessions_layer_b_enabled,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import (
    _record_run,
    _record_skip,
    uploaded_objects,
    uploaded_versions,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.retention import (
    _audit_retention,
    _prune_remote_archives,
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
    _O_DIRECTORY,
    _O_NOFOLLOW,
    _O_NONBLOCK,
    _add_pinned,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.uploads import (
    CALLER_OWNER,
    _authorize_recovery_read,
    _authorize_upload,
    _refuse_upload,
    _unchanged_baseline,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.deploy.engine import AWSError
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.identity_stores import _store_write_time, state_db_candidates
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction
from kiro_crew.snapshot import snapshot_main

logger = logging.getLogger(__name__)


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
    with tempfile.TemporaryDirectory(prefix="kc-backup-") as tmp:
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
        # Does this archive carry anything the drive does not already hold? Taken over
        # the PAYLOAD, so it is the bytes that would actually leave that are compared --
        # a redaction switch flipped since the last run changes those without the source
        # tree moving, and this notices.
        #
        # Placed BEFORE `_authorize_upload` deliberately. The gate's contract is that it
        # sits immediately before the PUT with nothing in between, so a decision that
        # can end the run has to be taken on this side of it; and a run that is about to
        # send nothing has no upload to authorize in the first place.
        tree = _tree_fingerprint(payload, volatile_root=True)
        baseline = _unchanged_baseline(
            account, KIND_SNAPSHOT, tree, profile, region, bucket, caller=caller
        )
        if baseline is not None:
            record = _record_skip(account, KIND_SNAPSHOT, baseline, tree)
            if record is not None:
                logger.info(
                    "aws-control: snapshot backup for %s found the tree unchanged since the "
                    "archive already in the drive, so it uploaded nothing",
                    account,
                )
                # No label publish and no retention sweep. Both exist to follow a push:
                # a local rename reaches the drive on the next real upload rather than
                # on this skip, and retention retires copies by count -- running it here
                # would let a stretch of unchanged nights walk the keep window down and
                # delete the very archive the next skip has to prove is present.
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
        key = f"{KIND_SUBPATHS[KIND_SNAPSHOT]}/{identity['id']}/kirocrew-snapshot-{_stamp()}.tar.gz"
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
        )
        record = _record_run(
            account,
            KIND_SNAPSHOT,
            key,
            payload.stat().st_size,
            _body_fingerprint(payload),
            version,
            tree=tree,
        )
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


def _add_tree(tar: tarfile.TarFile, root: Path, arc_prefix: str) -> int:
    """Add a directory tree to ``tar``, following no filesystem link.

    The session directories are agent-writable, so a link planted inside them
    must not become a read of whatever it points at, and an ancestor swapped
    mid-traversal must not redirect a read either.

    The descent is descriptor-pinned end to end (:func:`_add_pinned`): each level
    is a held descriptor, every child is opened relative to it, and the bytes are
    streamed from that same descriptor. No path is ever resolved twice, so there
    is no check-then-open window at any level.

    There is deliberately NO name-based fallback. A platform without ``openat``
    (``dir_fd``) and an fd-accepting ``os.scandir`` cannot make the check and the
    open one operation, so a name-based walk of these directories leaves a swap
    race open: a validated directory replaced by a junction to ``~/.aws`` between
    the check and the descent gets archived, and this archive is then uploaded
    unattended. Hardening narrows that window but nothing on such a platform
    closes it. Losing the backup there is a missing convenience; uploading
    credentials is not recoverable, so this refuses instead -- see
    :func:`run_sessions_backup`, which states the refusal before any work starts.

    Returns the number of files added.
    """
    if not _CAN_PIN_TRAVERSAL:
        # Defense in depth: run_sessions_backup refuses earlier and with a better
        # message. This is here so a future caller cannot reintroduce a
        # name-based walk of these directories by accident.
        raise RuntimeError(_NO_PINNING_REASON)
    if not root.is_dir() or is_link_or_junction(root):
        return 0
    try:
        root_fd = os.open(str(root), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError:
        return 0
    try:
        return _add_pinned(tar, root_fd, arc_prefix, depth=0)
    finally:
        os.close(root_fd)


#: The exact tables the conversation export carries out of the kiro-cli store,
#: and the ONLY ones. Everything else in ``data.sqlite3`` -- every identity /
#: token / usage table ``hooks.py`` classifies as an auth store, whatever its
#: name -- is left behind by construction: the export writes THIS allowlist and
#: nothing else, so the archive can never carry a byte of a table outside it, even
#: if kiro-cli adds a new credential TABLE tomorrow. A denylist would fail OPEN the
#: day such a table appeared; an allowlist fails closed. The bound is per table and
#: not per column: every column an allowlisted table declares is copied, so a
#: credential column added to one of THESE tables would ride -- see
#: :func:`_copy_table`.
#:
#: ``conversations`` and ``conversations_v2`` are the terminal's own chat stores --
#: the un-migrated and migrated shapes of the same data. A store carries whichever
#: its install has reached, and on a store that has migrated the older table is
#: present and empty, so carrying both costs nothing there and is the only way an
#: install that never migrated has its conversations exported at all. Carrying just
#: one also makes the ``no_conversation_table`` outcome untrue on the other kind of
#: store: the rows exist, they are simply outside the allowlist, and the run record
#: would report an absence rather than a coverage gap. The
#: boundary of what counts as "conversation state" is declared in this module's
#: header; widening this tuple is the one change that widens that boundary, so it
#: is the single place a reviewer looks.
_CONVERSATION_TABLES: tuple[str, ...] = ("conversations", "conversations_v2")


#: The archive member names for the conversation export. The database rides under
#: its own ``conversations/`` root (a third root beside ``crew`` and ``cli``), and
#: the manifest beside it records the table set and per-table row count so an
#: archive that carried the conversations is distinguishable from one that did
#: not, and a silently-empty export is caught by comparing these counts to source.
_CONVERSATIONS_ARC_PREFIX = "conversations"


_CONVERSATIONS_DB_ARCNAME = f"{_CONVERSATIONS_ARC_PREFIX}/conversations.sqlite3"


_CONVERSATIONS_MANIFEST_ARCNAME = f"{_CONVERSATIONS_ARC_PREFIX}/CONVERSATIONS_MANIFEST.json"


#: The sanctioned credential-read audit id for opening the kiro-cli store. The
#: store holds live bearer tokens, so every reader owes an SEL trail; this id is
#: registered in ``hooks._AUDIT_ONLY_READ_IDS`` and the export fails closed if the
#: audit cannot be recorded. The registry holds its own literal, so this constant
#: does not make the two strings one -- what catches a drift is
#: ``test_the_conversation_read_id_is_registered_in_hooks``, which asserts this
#: value is present there. An unregistered id fails every read closed, so a drift
#: is loud rather than silent, but it is the test that keeps them equal.
_CONVERSATION_READ_ID = "aws_control.conversation_export"


#: The per-cell byte ceiling for a copied conversation value, and the number of rows
#: the copy fetches at a time. Together they are the export's peak-memory bound: at
#: most ``_CONVERSATION_BATCH_ROWS * _CONVERSATION_MAX_CELL_BYTES`` of conversation
#: text is live at once. A row count ALONE bounds nothing, because one field can be
#: arbitrarily wide, and an allocation failure here would take the whole sessions
#: backup down with it rather than costing only this sub-member.
#:
#: Both numbers come from a live store rather than a guess: its widest
#: ``conversations_v2`` value measures 2.5 MiB, and the table totals 1.1 GiB across
#: 3226 rows, so a 500-row batch of real data is roughly 180 MiB. The ceiling sits
#: well above the widest real value so an ordinary host is never refused, and the
#: batch is small because an average row here is hundreds of kilobytes.
_CONVERSATION_MAX_CELL_BYTES = 16 * 1024 * 1024


_CONVERSATION_BATCH_ROWS = 4


def _kiro_cli_conversation_db() -> tuple[Path | None, str]:
    r"""This host's kiro-cli store, and why there is none when there is none.

    Resolved through :func:`identity_stores.state_db_candidates` against FIXED,
    home-anchored locations. The environment is deliberately NOT consulted -- not
    ``XDG_DATA_HOME`` on POSIX, not ``LOCALAPPDATA`` or ``APPDATA`` on Windows --
    which is why an empty mapping is passed rather than ``os.environ``.

    **Why a relocated store is skipped rather than found.** The fence that makes
    this store unreadable and unwritable by agent file tools is home-anchored:
    ``security.paths`` splices in :func:`identity_stores.fenced_home_dirs`, and its
    own comment records that "a profile redirected outside the home directory is
    not covered". So a store re-rooted by one of those variables sits OUTSIDE the
    fence, where an agent can author rows. This function feeds an archive that is
    uploaded off-host unattended, so honouring the variable would let an agent
    plant rows in a ``conversations_v2`` table at a location it may write and have
    a scheduled backup ship them. An uploaded object cannot be un-sent.
    :func:`kiro_prerequisite` records the same decision for the same two variables
    and states the rule this follows: a fixed anchor cannot be pointed at
    something the agent may write. Being wrong in this direction costs a
    relocated-store install its terminal conversations, which the run record shows
    as an absent ``conversations/`` root; being wrong in the other direction
    uploads agent-authored content and cannot be undone.

    The candidates come back current-platform, most likely first, deduped; the
    first that is a regular file reached through no redirection is this host's.

    **The second return value says WHY no store was used, and it is never empty when
    none was.** It is a reason string, and the caller suppresses the retention sweep on
    any reason, so each of these cases keeps an earlier archive alive rather than
    letting this run retire it. Three cases reach it, and none is exotic:
    ``store_rejected_link`` for a candidate refused by either redirection test -- and
    the ancestor walk rejects on ANY parent from ``/`` down, including the home
    directory, so an ordinary symlinked ``~/.local/share`` or a symlinked home takes
    this exit permanently -- ``store_unreadable`` for one whose stat raised ``OSError``,
    and ``store_absent`` when nothing is at any fenced location.

    Absence reports a reason for a reason worth stating, since the opposite reads as
    obvious: the question the caller asks is whether an EARLIER archive holds rows this
    one does not, which is about backup history rather than about what is on disk now. A
    store wiped to clear corruption, removed by a reinstall, or on a volume not mounted
    at nightly-run time was present when last week's archive was written. Nothing pins a
    store's presence from one run to the next, so a per-run observation cannot answer
    the question, and answering it optimistically erases the last archive that held the
    conversations.

    **The ORDER of the two redirection tests and the file test is the guard, not a
    detail.** ``is_file()`` on a LOCAL-looking path whose ANCESTOR is a junction to
    ``\\host\share`` opens an outbound SMB connection that authenticates as this
    process, and it does so inside the stat itself -- before any check of ours can
    reject anything. So the ancestor walk runs FIRST, on every candidate, before
    the path is stat-ed at all. :func:`platform_compat.first_linked_ancestor` tests
    ancestors root-first and stops at the first link, so the walk never traverses
    one either. It deliberately excludes the leaf, which is why
    :func:`platform_compat.is_link_or_junction` still tests the candidate itself:
    ``islink`` alone answers False for a Windows junction, so a bare symlink check
    would accept a junction and read the store it points at instead of this host's.
    A fixed anchor bounds where a candidate may live; it does not stop a link
    planted AT that anchor from redirecting the read, so both guards are needed.
    """
    # Fixed home-anchored candidates only -- an empty mapping, never `os.environ`.
    # See the relocation note above: a redirected root falls outside the
    # agent-file-tool fence, and this feeds an off-host upload.
    candidates = state_db_candidates(sys.platform, Path.home(), {})
    # Why a candidate was declined, for the caller. The FIRST decline is kept rather
    # than the last: candidates come back most-likely-first, so the earliest one is
    # the store this host would have used.
    declined = ""
    # Every candidate that clears both redirection tests AND is a regular file, not
    # just the first. On Windows the table lists Local (the current layout) before
    # Roaming (legacy), so returning the first would let a leftover in the abandoned
    # root mask the live account. `identity_stores.selected_store` already arbitrates
    # that exact state by write time; this reuses its READING rather than its answer,
    # because that function stats its candidates itself, before anything has checked
    # them for redirection -- wrapping it would put the outbound stat back ahead of
    # the guard, which is the hole the ordering above exists to close.
    #
    # `_store_write_time` is imported rather than reimplemented even though it is that
    # module's private name. Two copies of "newest write across the main file and its
    # WAL sidecar" can drift, and the cost of drift here is exporting a stale store
    # while reporting success, silently; a rename instead breaks the import loudly, at
    # import time, under mypy and every test that loads this module.
    cleared: list[Path] = []
    for db in candidates:
        try:
            # Redirection tests BEFORE `is_file()`. See the order note above: the
            # stat is the outbound connection, so it must not run on a path this
            # has not already cleared.
            if first_linked_ancestor(db) or is_link_or_junction(db):
                declined = declined or "store_rejected_link"
                continue
            if db.is_file():
                cleared.append(db)
        except OSError:
            declined = declined or "store_unreadable"
            continue
    if cleared:
        try:
            # `max` keeps the FIRST maximal element, so equal write times prefer the
            # earlier table row -- Local, the current layout -- exactly as
            # `selected_store` resolves a tie. `_store_write_time` also reads the
            # `-wal` sidecar, because a commit lands there and the main file's mtime
            # does not advance until a checkpoint, so the main file alone
            # under-reports recency on the very store being written.
            return max(cleared, key=_store_write_time), ""
        except OSError:
            # A store vanished between the check above and the stat. Fall back to the
            # current-layout row rather than losing the export, which is what
            # `selected_store` does when a write time cannot be read.
            return cleared[0], ""
    # Absence is reported too, and is NOT the reasonless case it looks like. The
    # question the caller asks this field is "could an older archive hold
    # conversations this one does not", which is about backup HISTORY, not about
    # whether a store is here now. A store wiped to clear corruption, removed by a
    # reinstall, or sitting on an unmounted volume at nightly-run time was present
    # last week, so last week's archive holds rows this run cannot carry. Nothing
    # pins a store's presence across runs, which is the same reason the relocation
    # flag could not be treated as standing: a per-run observation cannot answer a
    # question about earlier archives.
    return None, declined or "store_absent"


def _store_relocated_outside_the_fence() -> bool:
    """Whether this host's environment re-roots the store away from the fenced set.

    :func:`_kiro_cli_conversation_db` deliberately reads only fixed, home-anchored
    candidates, so a relocated store is skipped. Skipping it SILENTLY is the
    failure this answers: an operator whose store lives outside home would believe
    an archive holds their terminal conversations when it holds none, and nothing in
    the run record would say otherwise.

    Compares the two candidate sets by PATH and touches the filesystem not at all --
    no ``stat``, no open, nothing. That is deliberate rather than incidental: the
    relocated root is outside the agent-file-tool fence, and probing a path there is
    the very thing the ancestor guard in :func:`_kiro_cli_conversation_db` exists to
    stop. A set difference needs no probe, so this reports the condition without
    reproducing the risk.

    True means "the environment names at least one store location this export will
    not read". It does NOT mean a store exists there; that question cannot be
    answered without a probe, and is not worth one. Reporting the relocation is
    enough for an operator to understand an absent ``conversations/`` root, which is
    the whole job.

    **Only a variable that MOVES a candidate counts, and that is the whole test.**
    ``LOCALAPPDATA`` and ``XDG_DATA_HOME`` re-root their own candidate, so the
    difference sees them. ``APPDATA`` does not, and is deliberately not compared: the
    Windows Roaming candidate is a fixed home anchor because
    :func:`identity_stores.state_db_candidates` does not follow that variable -- "the
    current generation writes the ``LOCALAPPDATA`` location, and the roaming default
    is retained only as a legacy fallback". A store kiro-cli does not write to cannot
    be relocated away from this export, so an ``APPDATA`` mismatch is not evidence of
    a relocation. Comparing it anyway reported one on any host with a redirected
    Roaming folder, which is an ordinary enterprise configuration, and that false
    positive froze retention permanently while a readable Local store sat beside it
    inside the fence. Nothing is lost by leaving it out: a legacy install that really
    does keep its store at a redirected Roaming root has no store at either fixed
    candidate, so the lookup reports ``store_absent`` and the sweep is suppressed on
    that path instead.
    """
    fixed = set(state_db_candidates(sys.platform, Path.home(), {}))
    relocatable = state_db_candidates(sys.platform, Path.home(), os.environ)
    return any(candidate not in fixed for candidate in relocatable)


class _ConversationExport(NamedTuple):
    """What one conversation export actually put in the archive.

    ``rows`` and ``members`` are tracked SEPARATELY because they answer different
    questions and genuinely disagree on a path this module takes: a present-but-
    empty allowlisted table IS carried, so a restore sees the real schema, and that
    archive holds two members and zero rows. Measuring content by rows alone reads
    such an archive as empty, and :func:`run_sessions_backup`'s "nothing to
    archive" guard would then discard members it had already written.

    ``rows`` feeds the archive's content count and the unchanged-run comparison.
    ``members`` answers only "is there a ``conversations/`` root in here".
    ``skipped`` names a reason the export carried less than the host holds, for the
    run record, and is empty when there is none. It exists because a silent skip is
    how an operator ends up believing they hold a backup they do not hold.

    **Any non-empty ``skipped`` also suppresses the retention sweep**, so setting it
    is not merely a reporting act -- see :func:`run_sessions_backup`. That is why the
    suppression is a predicate on this field rather than a list of qualifying reasons:
    a new reason added here cannot be forgotten from a predicate, and every reason
    this module emits qualifies anyway. Leave it EMPTY only when this run READ
    everything the host holds -- a successful export -- or when the operator has
    withheld the permission, which is a consented withdrawal rather than a gap. An
    absent store is NOT one of those: the question is whether an EARLIER archive holds
    rows this one does not, and a store that is missing now may have been present when
    that archive was written.
    """

    rows: int
    members: int
    skipped: str = ""


def _add_bytes(tar: tarfile.TarFile, payload: bytes, arcname: str) -> None:
    """Add ``payload`` to ``tar`` as ``arcname`` (mode 0600, deterministic)."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(payload))


class _ScratchExportUnsafe(Exception):
    """The scratch export this module wrote is not the file it wrote.

    Raised BEFORE the first tar write, so the caller reports a reason and carries
    nothing rather than shipping a member whose bytes came from somewhere else.
    """


def _conversation_scratch_parent() -> Path:
    """The agent-masked directory the conversation export is written under.

    This is the FIRST line of defence and it removes the attack rather than detecting
    it. A shared temp root cannot be made safe by descriptor pinning alone, because the
    pinning happens after a name the agent can already reach: a same-UID agent that
    replaces the temp DIRECTORY before this process opens it hands over a directory of
    its own, in which every pinned check passes on a file the attacker chose.

    ``app_data_dir(APP_NAME)`` is masked from agent sandboxes as a whole directory
    (``sandbox._CREW_HIDDEN_LEAVES`` carries ``apps/aws-control/data``), and it is the
    STRICTER of the two masked roots this app has: the sibling ``aws-control-staging``
    is deliberately granted to the AWS CLI spawn, and this scratch file is read only by
    this process, so it has no reason to be reachable from that child.

    Guarded exactly as :func:`restore_archive`'s staging is, and for the same reasons a
    per-file check cannot cover: a link planted AT the root would put the scratch file
    outside the fence wholesale, and ``exist_ok=True`` happily accepts a pre-existing
    link, so the resolve is re-checked after the ``mkdir`` rather than before it.
    """
    base = app_data_dir(APP_NAME)
    scratch = base / "conversations"
    if is_link_or_junction(scratch):
        raise ValueError("conversation scratch directory is not a real directory")
    # `mode=` at creation rather than a chmod afterwards: it leaves no instant in which
    # the directory exists group- or world-readable. umask can only clear bits, so the
    # result is never wider than 0700. Ignored on Windows, where the masked parent and
    # its own ACL are what restrict this.
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    if scratch.resolve() != (base.resolve() / "conversations"):
        raise ValueError("conversation scratch directory resolves outside app storage")
    if not scratch.is_dir():
        raise ValueError("conversation scratch directory is not a real directory")
    return scratch


def _add_open_file(tar: tarfile.TarFile, fh: IO[bytes], size: int, arcname: str) -> None:
    """Add an ALREADY-OPEN, already-validated file to ``tar`` as ``arcname``.

    Takes the handle rather than a path because the caller's descriptor IS the
    authorization: re-deriving the file from its name here would reopen the swap
    window the caller just closed.
    """
    info = tarfile.TarInfo(name=arcname)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.type = tarfile.REGTYPE
    tar.addfile(info, fh)


def _open_pinned_scratch(dir_fd: int, name: str) -> tuple[IO[bytes], int]:
    """Open ``name`` under ``dir_fd`` and prove it is still the file we wrote.

    An earlier version of this read the scratch export BY PATH and stated that
    pinning was unnecessary "because the source is a file this process created under
    its own private ``TemporaryDirectory``". That justification was WRONG, and the way
    it was wrong is the reusable part: ``TemporaryDirectory`` is mode 0700, which
    excludes other USERS and not the same-UID agent this product's threat model
    assumes -- the one :mod:`kiro_crew.sandbox` describes planting links in the
    world-writable root. The directory was never private from the attacker that
    matters, so ``stat`` then ``open`` on a name left exactly the swap window
    :func:`_add_pinned` exists to close, and it paid out as a host file uploaded
    off-host with no recall.

    Three checks, each closing a different substitution:

    * ``O_NOFOLLOW`` -- the name may not resolve through a symlink.
    * ``S_ISREG`` on the DESCRIPTOR -- not a FIFO or device that would make the read
      block or return a stream that is not the export.
    * ``st_nlink == 1`` -- a hard link defeats the other two by construction, because
      the target is a genuine regular file reached under our own name.

    The size comes from the same ``fstat`` as the checks, so the header cannot
    describe one file while the body streams another.

    Reachable only through :func:`run_sessions_backup`, which refuses outright on a
    platform without descriptor pinning, so these flags are real here and never the
    ``getattr`` zero fallback.
    """
    fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dir_fd)
    handle = open(fd, "rb", closefd=True)
    try:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _ScratchExportUnsafe("the scratch export is not a regular file")
        if st.st_nlink != 1:
            raise _ScratchExportUnsafe(f"the scratch export carries {st.st_nlink} links")
        return handle, st.st_size
    except BaseException:
        handle.close()
        raise


class _ConversationTooLarge(Exception):
    """One conversation field is wider than the ceiling, so nothing this export read ships.

    Its own exit rather than a folded-in ``store_unreadable``: the store was read
    fine and one field is pathological, which asks a different thing of the operator.
    The export carries nothing instead of dropping the row, for the reason
    :class:`_RedactionFailed` gives -- a partial copy produces an archive a restore
    reads as complete.
    """


class _RedactionFailed(Exception):
    """A redactor raised on a value, so nothing this export read may be shipped.

    Its own exit, not folded into ``store_unreadable``: the store WAS read here, and
    the two states need different reasons because they call for different operator
    action. Silently dropping the row would ship an archive a restore reads as
    complete, and falling back to the raw value would ship the credential this pass
    exists to remove -- so the export carries nothing and says why.
    """


def _redacted_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """One source row with credentials and exfiltration URLs removed from its text.

    Only ``str`` values are rewritten. The allowlisted table's real schema is
    ``key``/``conversation_id``/``value`` TEXT plus two INTEGER timestamps, measured on
    a live store where every text column reports ``typeof() == 'text'``, so no BLOB
    carries conversation text and an integer has nothing to scrub.

    Raises :class:`_RedactionFailed` rather than returning the row: a caller that
    cannot scrub a value must not choose between dropping it and shipping it.
    """
    out: list[Any] = []
    for value in row:
        if not isinstance(value, str):
            out.append(value)
            continue
        try:
            cleaned = _redact_egress(value)
        except Exception as exc:  # noqa: BLE001 - any failure here means do not ship
            raise _RedactionFailed(str(exc)) from exc
        out.append(cleaned)
    return tuple(out)


def _refuse_an_oversized_cell(source: sqlite3.Connection, table: str, cols: list[str]) -> None:
    """Raise unless every cell of ``table`` fits :data:`_CONVERSATION_MAX_CELL_BYTES`.

    Measured in SQLite, in the caller's read transaction, BEFORE the first
    ``fetchmany``: ``length(cast(c as blob))`` yields a byte count without handing
    Python the value, so the ceiling is established rather than discovered by
    allocating. Being inside the snapshot is what makes one pass enough for the whole
    copy -- a writer committing a wider value mid-copy is outside it and cannot be
    read. One extra scan of the table is the price of a bound that precedes the fetch
    rather than following it.

    The message names the column and the byte count, never the value.
    """
    widest = ", ".join(f'max(length(cast("{c}" as blob)))' for c in cols)
    measured = source.execute(f'SELECT {widest} FROM "{table}"').fetchone() or ()
    for name, size in zip(cols, measured):
        if size is not None and size > _CONVERSATION_MAX_CELL_BYTES:
            raise _ConversationTooLarge(
                f"{table}.{name} holds a {size}-byte value, over the "
                f"{_CONVERSATION_MAX_CELL_BYTES}-byte ceiling"
            )


def _copy_table(source: sqlite3.Connection, target: sqlite3.Connection, table: str) -> int:
    """Copy every row of ONE allowlisted table into ``target``. Returns row count.

    The destination schema is taken from the source's own ``CREATE TABLE`` text
    (``sqlite_schema.sql``), and rows are moved through a named column list built
    from ``PRAGMA table_info`` rather than a literal ``SELECT *``. Be precise about
    what that buys: the list is derived from whatever columns the source declares
    at copy time, so it is NOT an allowlist and does NOT hold a column back. A
    column added to this table upstream -- including a credential-bearing one --
    is enumerated by the same ``PRAGMA`` and copied. What the named list gives is a
    stable, quoted column ORDER shared by the SELECT and the INSERT, so the copy
    cannot silently mis-align if the two ever saw different column sets. The
    fail-closed boundary is the TABLE allowlist in
    :data:`_CONVERSATION_TABLES`, one level up; column granularity is not
    implemented here.

    The table name is validated against the caller's allowlist before it reaches
    here, so it is never attacker-controlled; column identifiers are quoted
    defensively all the same.

    Peak memory is bounded by :data:`_CONVERSATION_BATCH_ROWS` rows of at most
    :data:`_CONVERSATION_MAX_CELL_BYTES` each, and a table holding a wider cell is
    refused outright rather than copied -- see :func:`_refuse_an_oversized_cell`.
    """
    create_sql = source.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not create_sql or not create_sql[0]:
        return 0
    target.execute(create_sql[0])
    cols = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not cols:
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    count = 0
    _refuse_an_oversized_cell(source, table, cols)
    cursor = source.execute(f'SELECT {col_list} FROM "{table}"')
    insert = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
    while True:
        rows = cursor.fetchmany(_CONVERSATION_BATCH_ROWS)
        if not rows:
            break
        # A GENERATOR, not a list comprehension: the raw batch is already held, and
        # materialising the redacted copy beside it doubles the peak for the length of
        # the statement. ``executemany`` consumes one row at a time, so only one
        # redacted row is live at once.
        target.executemany(insert, (_redacted_row(row) for row in rows))
        count += len(rows)
    return count


def _export_cli_conversations(tar: tarfile.TarFile) -> _ConversationExport:
    """Export ONLY the terminal conversation tables into ``tar``. Returns row count.

    ``data.sqlite3`` is BOTH the terminal's conversation store and its identity
    auth store: ``hooks.py`` classifies the file as a token path and it
    holds live bearer tokens. Tar-ing the file would upload live
    credentials off-host, strictly worse than the gap this closes. So this reads
    the source and writes a FRESH database holding only :data:`_CONVERSATION_TABLES`
    -- an allowlist, so no byte of any table OUTSIDE it (no identity row, no token
    column of an auth table) can reach the archive even if kiro-cli adds a
    credential table later. The bound is per TABLE, not per column: every column an
    allowlisted table declares is copied, so a credential column added to
    ``conversations_v2`` itself would ride. See :func:`_copy_table`.

    **The source is a LIVE WAL-mode database, so a consistent read needs care.**
    A commit lands in the ``-wal`` sidecar and folds into the main file only on a
    checkpoint, so the main file and its ``-wal`` only agree at an instant. The
    safe read does NOT copy those files, and does NOT checkpoint: a checkpoint is
    a WRITE, and this opens the operator's store ``mode=ro``, on which a
    ``wal_checkpoint`` cannot run. What a read-only connection DOES give is a
    consistent WAL-aware view -- SQLite applies the committed ``-wal`` frames
    transparently on read -- so the whole export runs inside ONE explicit read
    transaction (``BEGIN``), which pins a single snapshot for the life of the
    copy. Every allowlisted table is then read against that one snapshot, so a
    writer committing mid-copy cannot make two tables disagree. This is why
    copying the ``-wal`` and then the main file separately would be wrong: it
    takes them at two different times, and a WAL copied before its commit was
    checkpointed replays over newer pages so the archive restores BACKWARDS.

    Steps:

    1. Open the source READ-ONLY (``mode=ro``) so this writes no database page and
       runs no checkpoint against the operator's live store. It is not a promise of
       zero filesystem writes: on a WAL store SQLite may still update the ``-shm``
       shared-memory sidecar to take a read mark, which is how a reader
       participates in WAL at all. The store's own DATA is what is untouchable
       here. Open one deferred read transaction so all reads share a single
       consistent snapshot including committed WAL frames.
    2. Copy the allowlisted tables through a named per-table column list, which
       fixes a stable column order for the copy -- see :func:`_copy_table` for what
       that does and does not bound, since the list is derived from the source's
       declared columns and is not a column allowlist. The fail-closed boundary is
       the TABLE allowlist in :data:`_CONVERSATION_TABLES`.

    Best-effort at the STORE level (a missing store, an unreadable one, a store that
    cannot be resolved at all) -- those
    return an empty :class:`_ConversationExport` and the sessions backup proceeds
    with the transcript halves. NOT
    best-effort at the ROW level: once a readable store is found, every row of
    every allowlisted table is copied and counted, and the manifest's count is
    asserted against the source in the tests, so a partial copy fails loudly
    rather than shipping a short archive.

    **One exit deliberately does NOT report a skip: a failure while WRITING the
    members into the tar.** ``tarfile.addfile`` raising part-way leaves the archive in
    an undefined state, so swallowing it would upload a damaged tarball under a record
    saying the run succeeded -- worse than the failure it hides, and the one case where
    losing the whole archive is the correct outcome. Everything before the first tar
    write is guarded and reports; from the first tar write onward, an
    exception propagates.
    """
    # RELOCATION FIRST, before any lookup. A file may still sit at the fixed anchor
    # after the environment says the store moved -- a leftover from before the
    # relocation -- and looking there first would export those stale conversations
    # and report complete coverage, because the relocation would only be noticed
    # when the fixed lookup found nothing. `identity_stores.selected_store` arbitrates
    # this same "leftover in the abandoned root" state by mtime, so it is a state
    # this codebase already expects rather than a hypothetical. A store the
    # environment has moved away from is stale by definition, and exporting stale
    # conversations while recording complete coverage is worse than exporting
    # nothing and saying so.
    # Discovery is GUARDED, not because either call is expected to raise, but because
    # an exception escaping here would fail the whole sessions backup and throw away a
    # correct transcript archive over a missing sub-member. Both calls reach
    # ``Path.home()``, which raises when the home directory cannot be determined, and
    # the candidate walk touches the filesystem. This function's contract is
    # best-effort at the store level, so an unusable store must look the same however
    # it became unusable. ``RuntimeError`` is named for ``Path.home()`` specifically.
    try:
        relocated = _store_relocated_outside_the_fence()
        db, declined = (None, "") if relocated else _kiro_cli_conversation_db()
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "aws-control: the kiro-cli conversation store could not be resolved, so no "
            "conversations were carried in this archive: %s",
            redact_log_via_context(str(exc)),
        )
        return _ConversationExport(0, 0, "store_discovery_failed")
    if relocated:
        logger.warning(
            "aws-control: the kiro-cli conversation export reads only fixed, "
            "home-anchored store locations, and this host's environment names a "
            "relocated one, so no conversations were carried in this archive"
        )
        return _ConversationExport(0, 0, "store_relocated_outside_fence")
    if db is None:
        # No store was USED, and every way of reaching that reports a reason. The
        # tempting exemption is absence: a host with no store looks like it has no
        # conversations for an older archive to hold. That asks about the store NOW,
        # while retention decides the fate of an archive written EARLIER -- a store wiped
        # to clear corruption, dropped by a reinstall, or on an unmounted volume was
        # present when that archive was written. An ordinary symlinked home reaches the
        # rejection case permanently, and either case erases the last complete archive
        # if it prunes, so both suppress.
        logger.warning(
            "aws-control: the kiro-cli conversation export used no store (%s), so no "
            "conversations were carried in this archive",
            declined,
        )
        return _ConversationExport(0, 0, declined)
    per_table: dict[str, int] = {}
    # Cut under the AGENT-MASKED app data root, not the system temp directory. That is
    # the defence; the descriptor pinning below is depth behind it. See
    # `_conversation_scratch_parent`.
    #
    # Its own failure is a REASON rather than an exception: a host whose data home
    # cannot hold a directory has not failed the backup, and the transcript halves are
    # already archived by the time this runs.
    try:
        scratch_parent = _conversation_scratch_parent()
    except (OSError, ValueError) as exc:
        logger.warning(
            "aws-control: kiro-cli conversation export dropped -- its masked scratch "
            "root was unusable, so nothing was carried: %s",
            redact_log_via_context(str(exc)),
        )
        return _ConversationExport(0, 0, "scratch_root_unusable")
    with tempfile.TemporaryDirectory(prefix="kc-conv-", dir=str(scratch_parent)) as tmp:
        dst = Path(tmp) / "conversations.sqlite3"
        src_uri = f"file:{urllib.parse.quote(str(db))}?mode=ro"
        try:
            with contextlib.closing(sqlite3.connect(src_uri, uri=True)) as source:
                # One consistent snapshot for the whole copy: a deferred read
                # transaction opened by the first SELECT holds a single point in
                # time, so a writer committing mid-copy cannot make two tables
                # disagree. No checkpoint (a write, impossible on mode=ro) and no
                # WAL refusal -- the read already sees committed WAL frames.
                source.execute("BEGIN")
                present = {
                    row[0]
                    for row in source.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    ).fetchall()
                }
                with contextlib.closing(sqlite3.connect(str(dst))) as target:
                    for table in _CONVERSATION_TABLES:
                        if table not in present:
                            # The allowlist names the terminal's un-migrated and
                            # migrated chat tables, and a store holds whichever its
                            # install has reached, so one of them being absent is an
                            # ordinary state. Skip it; do not fail the export.
                            continue
                        per_table[table] = _copy_table(source, target, table)
                    target.commit()
        except _RedactionFailed as exc:
            # The store was read and could not be SANITISED. Per the invariant this
            # module walks, that is a failed read rather than a policy decline, so it
            # carries a reason and suppresses the retention sweep. The audit records a
            # successful contact, because the read itself succeeded -- what failed is
            # the shipping, and conflating the two would misreport which half broke.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success")
            logger.warning(
                "aws-control: kiro-cli conversation export could not redact a value, so "
                "nothing was carried rather than shipping it unredacted: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "conversations_unredactable")
        except _ConversationTooLarge as exc:
            # Read fine, refused on width. Same shape as the redaction exit: the READ
            # succeeded, so the audit says so, and the reason suppresses the retention
            # sweep because an older archive may hold what this one does not.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success")
            logger.warning(
                "aws-control: kiro-cli conversation export refused a value wider than "
                "its per-cell ceiling, so nothing was carried: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "conversations_oversized")
        except MemoryError:
            # The bound above is meant to make this unreachable; it is caught anyway
            # because the alternative is losing a correct transcript archive over a
            # sub-member. Nothing is formatted into the message, since a handler for an
            # allocation failure should not ask for more memory.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success")
            logger.warning(
                "aws-control: kiro-cli conversation export ran out of memory, so it was "
                "left out of this archive and the rest of the backup continues"
            )
            return _ConversationExport(0, 0, "conversations_memory_exhausted")
        except (OSError, sqlite3.Error) as exc:
            # The store was opened (or the open failed) -- either way the contact
            # with a credential-bearing file owes a trail. Record it as unreadable
            # and carry nothing.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "unreadable")
            logger.warning(
                "aws-control: kiro-cli conversation export could not read the store, so it "
                "was left out of this archive: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "store_unreadable")
        total = sum(per_table.values())
        if not per_table:
            # NO chat table existed at all -- neither the un-migrated nor the migrated
            # one -- so this reason means the terminal genuinely holds no conversations
            # rather than holding them in a table the allowlist omits. Nothing to carry,
            # and no empty member to add. (A present-but-empty table DOES get carried, so
            # a restore sees the real schema.) The store WAS opened, so the access
            # is still audited.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "no_table")
            return _ConversationExport(0, 0, "no_conversation_table")
        # The store holds live bearer tokens, so opening it -- even to copy only
        # the conversation allowlist -- goes through the sanctioned credential-read
        # audit, and FAILS CLOSED: an export whose access cannot be recorded is
        # dropped from the archive rather than shipped unaudited. A logger line is
        # not an SEL audit.
        if not hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success"):
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- its credential-read "
                "audit could not be recorded, so the conversations are left out of this "
                "archive rather than shipped unaudited"
            )
            return _ConversationExport(0, 0, "credential_audit_unavailable")
        added: list[str] = []
        # Open and VALIDATE before the first tar write, so a substituted scratch file
        # costs the conversations member and a reason -- not a damaged archive. The tar
        # write itself is deliberately outside this guard: per this function's contract,
        # everything before the first write reports and the write onward propagates.
        #
        # PLATFORM: the pinned read needs `openat`, and on a host without it the checks
        # have no equivalent worth inventing -- `os.open` cannot open a directory on
        # Windows, `O_NOFOLLOW` and `O_DIRECTORY` do not exist there, and `st_nlink` from
        # `fstat` is not a dependable link count. Rather than degrade to a weaker read,
        # this reports. It is unreachable in production: `run_sessions_backup` refuses
        # outright without `_CAN_PIN_TRAVERSAL` (`kind_unavailable_reason`), so the whole
        # sessions kind is already unavailable on such a host. The branch exists so a
        # DIRECT caller cannot quietly obtain the unpinned read the refusal exists to
        # prevent.
        if not _CAN_PIN_TRAVERSAL:
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- this platform has "
                "no descriptor-pinned open, so the scratch export cannot be read safely"
            )
            return _ConversationExport(0, 0, "scratch_pinning_unavailable")
        try:
            tmp_fd = os.open(tmp, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        except OSError as exc:
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- its scratch "
                "directory could not be pinned: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "scratch_export_unsafe")
        try:
            handle, size = _open_pinned_scratch(tmp_fd, dst.name)
        except (OSError, _ScratchExportUnsafe) as exc:
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- the scratch export "
                "was not the file this run wrote, so nothing was carried: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "scratch_export_unsafe")
        finally:
            os.close(tmp_fd)
        with handle:
            _add_open_file(tar, handle, size, _CONVERSATIONS_DB_ARCNAME)
        added.append(_CONVERSATIONS_DB_ARCNAME)
        manifest = json.dumps(
            {"tables": dict(sorted(per_table.items())), "total_rows": total},
            sort_keys=True,
        ).encode("utf-8")
        _add_bytes(tar, manifest, _CONVERSATIONS_MANIFEST_ARCNAME)
        added.append(_CONVERSATIONS_MANIFEST_ARCNAME)
    # Members are accumulated as they are written rather than asserted as a
    # constant, so the number the caller reads cannot drift from what this added.
    return _ConversationExport(total, len(added))


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
    with tempfile.TemporaryDirectory(prefix="kc-backup-") as tmp:
        archive = Path(tmp) / f"sessions-{_stamp()}.tar.gz"
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
            # and no released version wrote one: verified against this PR's base and
            # against main, where the sessions archive has exactly the `crew` and `cli`
            # roots and the export does not exist. Nothing needs protecting, under any
            # grant. The bound on that claim is a host that ran an UNRELEASED build of
            # this branch, which could hold conversations under a legacy grant; that is
            # a pre-merge test host, not an operator install.
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
        # `volatile_root=False`: this archive's roots are `crew` and `cli`, which are
        # meaningful and stable. Only the snapshot bundle carries a timestamped root.
        tree = _tree_fingerprint(archive, volatile_root=False)
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
        key = f"{KIND_SUBPATHS[KIND_SESSIONS]}/{identity['id']}/{archive.name}"
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
        # remains -- `is_app_enabled`, `aws_consent`, STS -- is not stored in the
        # engine's state file and takes no lock of ours, so an exclusive hold would
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
        # `scheduled_sessions_blocked_reason`. The last two READ the engine's state
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
            _authorize_upload(account, profile, region, caller=caller, payload_kind=KIND_SESSIONS)
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
            )
        record = _record_run(
            account,
            KIND_SESSIONS,
            key,
            archive.stat().st_size,
            _body_fingerprint(archive),
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
            _audit_retention(
                account,
                {
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
                },
                caller=caller,
                result="failed",
                error=f"retention declined: {decline}",
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


#: The two Job SDK kinds this app registers. Same strings as ``KIND_*`` so a run
#: record read by a human names the backup the owner asked for.
JOB_KINDS = (KIND_SNAPSHOT, KIND_SESSIONS)


def make_job_runner(sdk: Any, kind: str) -> Any:
    """Build the Job SDK runner for ``kind``. Registered once, at app startup.

    A PLAIN ``def``, and it must stay one. ``JobSDK._execute`` calls the runner
    and DISCARDS its return value, so an ``async def`` here would hand back a
    coroutine nobody awaits: the body would never execute, nothing would raise,
    and the record would settle on ``done`` reporting a backup that never
    happened. ``register()`` validates the kind and not the callable, so this
    property is the app's to keep.

    That constraint is what shapes the resolution below. The SDK gives a runner
    its handle and nothing else -- there is no ``params`` channel in P1 -- so the
    run's target is read back out of its own record, where ``start`` put it:

    * The ACCOUNT comes from ``dedupe_key``. It is the right carrier on its own
      merits, because the account is exactly this run's concurrency identity --
      two snapshot backups of one account must not both do the paid upload, and
      the SDK's index is ``(kind, dedupe_key)`` so snapshot and sessions for the
      same account still run independently. It is also the only field a runner
      can read without a private attribute (``get`` is public; the key is
      withheld from the HTTP view and never logged by the SDK).
    * profile/region/bucket are RE-RESOLVED here rather than carried, which is
      the rule this app already documents for the nightly loop: the drive is
      tag-discovered per run rather than trusted from memory.

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
        # An empty key reaches here from `POST /_jobs/{kind}/start` with no body:
        # the generic surface defaults `dedupe_key` to "". There is no account to
        # act on, and picking one would be acting on an account nobody named.
        if not account:
            raise RuntimeError("this backup run names no account; nothing was sent to AWS")
        if not (account.isdigit() and len(account) == 12):
            raise RuntimeError(
                "this backup run does not name an account id; nothing was sent to AWS"
            )
        resolved = accounts_mod.resolve_account_profile_cached(account)
        if resolved is None:
            raise RuntimeError(
                "no working connection for this account — reconnect it, then run the backup again"
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
        _authorize_upload(account, profile, region, caller=CALLER_OWNER, payload_kind=kind)
        bucket = storage.find_drive(profile, region, account=account)
        if not bucket:
            raise RuntimeError("this account has no drive yet; nothing was sent to AWS")
        # Resolved by NAME at call time, not captured at registration: the module
        # attribute stays the single definition of what a snapshot backup is.
        work = run_snapshot_backup if kind == KIND_SNAPSHOT else run_sessions_backup
        # A job exists because an owner asked for one through the app's route or
        # the `_jobs` surface, both owner-gated. The nightly loop does not come
        # through here and states `CALLER_SCHEDULED` for itself.
        work(account, profile, region, bucket, caller=CALLER_OWNER)

    return _run


#: Longest staged filename, in bytes. ``NAME_MAX`` is 255 on ext4 and on the other
#: filesystems this app is deployed to, and a key segment is capped at 255 characters
#: upstream, so a prefix added to a basename can otherwise overrun it and the restore
#: fails with ``ENAMETOOLONG`` instead of producing a file.
STAGING_NAME_MAX_BYTES = 255


def _staging_name(key: str) -> str:
    """The staging filename for an object key, derived from the WHOLE key.

    Namespacing is exactly what lets two distinct objects share a basename: each
    install writes under its own prefix, and nothing stops two of them naming an
    archive the same. A basename-only destination would let a restore of one
    silently replace an archive already staged from the other, so the name carries a
    digest of the full key. It is stable, so re-staging one key overwrites its own
    file rather than accumulating copies, and the basename is kept on the end so the
    file is still recognisable to whoever is looking at the directory.
    """
    prefix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] + "-"
    # Bounded in BYTES rather than characters, because that is the unit the limit is
    # in: the route's own validator caps a key segment at 255 characters, so a
    # basename plus this prefix overruns it, and counting bytes stays correct if the
    # validated character set is ever widened past ASCII. Decoding with "ignore"
    # drops a multibyte character a cut landed in.
    #
    # The budget comes out of the BASENAME and never out of the digest. That is what
    # keeps truncation from bringing the collision back: the digest covers the WHOLE
    # key, so two keys stay on two files however little of the basename survives.
    # Shortening the digest to win back room for a longer name is therefore the one
    # edit here that reintroduces the defect this function exists to prevent, and it
    # would still look correct -- the names remain distinct for every key a person
    # would try by hand.
    keep = STAGING_NAME_MAX_BYTES - len(prefix)
    return prefix + _key_basename(key).encode("utf-8")[:keep].decode("utf-8", "ignore")


def _recover_recorded_version(
    profile: str,
    region: str,
    bucket: str,
    key: str,
    *,
    account: str,
    staging: Path,
    expected: str,
) -> Optional[Path]:
    """One bounded read of the version this install recorded, or ``None``.

    Called only when the object CURRENT at ``key`` failed the body fingerprint. That
    failure means a co-writer overwrote a key this install recorded -- the drive is
    reachable by every install pointed at the account, versioning is on for exactly
    that reason, and an overwrite leaves our bytes behind as a noncurrent version.
    Before this, no code path could ask for them: :func:`storage.get_file` named no
    version, so a restore read whatever was current and the operator's own archive
    sat on the drive, intact and unreachable.

    Returns a path to a temp file holding bytes that PASSED the same fingerprint,
    never a path to bytes that merely arrived. ``None`` means the caller should fall
    back to the refusal it would have raised anyway, so every uncertain branch
    returns ``None``:

    * No recorded fingerprint to compare against. An empty one matches nothing, and
      unknown is not a pass -- the same rule the rest of the backup engine applies.
    * No recorded version for this key, or one that names a version SLOT rather
      than one version (see :func:`_is_provable_version_id`, which rejects
      ``"null"``: a suspended-versioning bucket gives that id to every write, so
      two different bodies at one key both report it).
    * A recorded version that is not well-formed enough to pass to the CLI at all
      (see :func:`storage.validate_version_id`).
    * The read is not authorized at the moment it would be made -- see
      :func:`_authorize_recovery_read`. The extra read is the one AWS call in a
      restore the caller did not ask for, so a disabled app, a withdrawn grant, a
      grant naming another account, or a profile repointed during the first download
      all stop it.
    * The version is gone -- deleted, expired out of the keep window, or never
      there. AWS answers with an error and it is reported as a refusal, not raised:
      for this caller an unusable recorded id is a refusal to report, not a fault.
    * The bytes came back and do NOT match the fingerprint. This is the case worth
      being precise about: it is not a recovery that failed, it is a second set of
      foreign bytes, and it is discarded exactly like the first.

    A fingerprint match is the WHOLE test, and nothing else is asked of the bytes.
    Whether they still open as a ``tar.gz`` is a different question, and one this
    module answers the same way everywhere: the current-version read accepts on the
    fingerprint alone, and the upload side pushes payloads it cannot read
    (:func:`_tree_fingerprint` returns ``""`` for an unreadable ``tar.gz``), so a
    recorded fingerprint can honestly name a malformed archive. Refusing one HERE
    would mean the operator gets their own archive when nobody overwrote the key and
    a refusal when somebody did, for the same bytes -- so this path hands back what
    the fingerprint proves is theirs, exactly as the other one does.

    Never widens what a restore will accept. The fingerprint is re-taken over the
    bytes that actually arrived on THIS read rather than carried over from the
    first, so the pin is on the object in hand and not on a claim about it.

    Exactly one extra read, and only on a path that was already going to refuse.
    There is no loop and no walk of the version list: the recorded id names one
    version, and if that one is not there this install has nothing to recover.
    Costing a second request on the way to the same refusal is the worst case.

    The id comes from local state, which is where a version id can be trusted from
    -- it is written by this install's own successful push, and
    ``apps/aws-control/data`` is neither agent-readable nor agent-writable (it sits
    behind the agent file-tool floor and is bind-masked from every agent sandbox). It
    is still validated on the way OUT, because a stored value read back later can be
    truncated or partially rewritten, and it travels as a separate argv element where
    a leading ``-`` would change what the command means. There is no shell in the
    path.
    """
    if not expected:
        return None
    recorded_version = uploaded_versions(account).get(key, "")
    if not _is_provable_version_id(recorded_version):
        return None
    if storage.validate_version_id(recorded_version) is not None:
        # Malformed enough that the call would be refused by the primitive. Reported
        # as a refusal rather than allowed to raise: this is a local state problem,
        # and the caller's honest answer for it is the one it already has.
        logger.warning(
            "aws-control: the archive now at a recorded key for %s is not the one this "
            "install uploaded, and the version recorded for that key is not well-formed, "
            "so there is nothing to recover and the restore is refused",
            account,
        )
        return None
    # The extra read is authorized HERE, immediately before it is made, by the same
    # four questions the paid upload is gated on. A refusal means the recovery does
    # not RUN, which leaves exactly the refusal this caller already had -- the same
    # shape as every other uncertain branch above.
    refusal = _authorize_recovery_read(profile, region, account=account)
    if refusal is not None:
        logger.warning(
            "aws-control: the archive now at a recorded key for %s is not the one this "
            "install uploaded, and the recorded version is not read because %s, so the "
            "restore is refused",
            account,
            refusal,
        )
        return None
    fd, alt_name = tempfile.mkstemp(prefix=".kc-restore-v-", dir=str(staging))
    os.close(fd)
    alt = Path(alt_name)
    try:
        storage.get_file(
            profile,
            region,
            bucket,
            "backup",
            key,
            str(alt),
            account=account,
            version=recorded_version,
        )
        # Inside the same guard as the download: reading the bytes back is part of
        # fetching them, and a staged copy that cannot be hashed is the same
        # outcome as one that never arrived -- a refusal to report, not an error to
        # surface. Left outside, an OSError here would escape a helper whose whole
        # contract is that every non-matching outcome returns the existing refusal,
        # and would leak the staged file this function owns.
        landed = _body_fingerprint(alt)
    except (AWSError, OSError, ValueError) as exc:
        # The version id is deliberately absent from this message, as is anything
        # derived from the object's bytes. An operator needs to know the recovery was
        # attempted and did not land; the id identifies nothing they can act on.
        logger.warning(
            "aws-control: the archive now at a recorded key for %s is not the one this "
            "install uploaded, and the version it did upload could not be read back, so "
            "the restore is refused: %s",
            account,
            exc,
        )
        alt.unlink(missing_ok=True)
        return None
    if landed != expected:
        logger.warning(
            "aws-control: the archive now at a recorded key for %s is not the one this "
            "install uploaded, and the version it recorded does not match either, so the "
            "restore is refused",
            account,
        )
        alt.unlink(missing_ok=True)
        return None
    logger.warning(
        "aws-control: the archive now at a recorded key for %s is not the one this install "
        "uploaded -- another writer replaced it -- so the restore used the version this "
        "install recorded writing, which matches byte for byte",
        account,
    )
    return alt


def restore_download(
    profile: str,
    region: str,
    bucket: str,
    key: str,
    *,
    account: str,
    foreign_ok: bool = False,
) -> dict[str, Any]:
    """Download one backup archive to the staging dir; return its local path.

    ``key`` is section-relative (``snapshots/...`` or ``sessions/...``) and
    validated by the handler with the same key rules as every drive key.

    Refuses every archive it cannot PROVE is this install's own -- a co-tenant's,
    one under this install's prefix with no matching upload record, and one from
    before install ids existed -- unless ``foreign_ok`` says the caller means it. This is the point of the whole change: one bucket is reached
    by every install pointed at the account, so before the namespace existed the
    operator chose an archive by TIMESTAMP and replacing this machine's
    ``memory.db`` with another machine's was one unguarded click. The decision is
    made on the id in the KEY -- see :func:`classify_key` -- and on nothing a
    writer authors. In particular the published label is NOT read here: a label is
    a caption an install writes about itself, so letting it reach this gate would
    mean an install could name itself into being restorable.

    ``foreign_ok`` is an override rather than a hard wall because disaster recovery
    is precisely the case where every archive is foreign.

    The staging dir is agent-writable, so the download never writes through the
    final name: a link planted at that path would have the S3 bytes land on its
    target. Two separate checks are needed:

    * The staging DIRECTORY itself, and every component of it under the app data
      dir, must be a real directory. A linked ``restore/`` puts both the
      ``mkstemp`` temp file and the ``os.replace`` target outside app storage,
      which no per-file check can see.
    * The destination NAME must not already be a link or a non-regular file.

    Bytes then go to an exclusively-created temp file in the same directory and
    are atomically moved into place.
    """
    recorded = uploaded_objects(account)
    origin, owner = classify_key(key, install_identity()["id"], set(recorded))
    # A recorded key makes this a CANDIDATE for ours; the verdict is decided on the
    # bytes, after the download, below. Deciding it here from a separate metadata
    # read would leave a window: the check and the transfer would be two requests,
    # and a writer to this shared drive could replace the object between them, so
    # what was verified would not be what arrived.
    #
    # One rule -- everything except a proven self archive needs the caller to say it
    # accepts the risk -- enforced at two points, because one of its inputs does not
    # exist yet. Here it uses what local state alone decides: a co-tenant's archive,
    # one carrying no id, and one under this install's own prefix that the upload
    # ledger has never heard of. None of those needs a byte to reject, so none of
    # them is paid for: a co-writer who plants an object under this install's
    # discoverable prefix cannot make an un-overridden restore download it. What is
    # left is a key the ledger DOES name, and only the bytes can settle that one.
    #
    # The rule lives HERE rather than in a confirmation dialog, so a caller that
    # never opens the dashboard is held to it too.
    if origin != ORIGIN_SELF and not foreign_ok:
        raise UnprovenArchive(origin, owner)
    base = app_data_dir(APP_NAME)
    staging = base / "restore"
    if is_link_or_junction(staging):
        raise ValueError("restore staging directory is not a real directory")
    staging.mkdir(parents=True, exist_ok=True)
    # Re-check after mkdir: exist_ok=True happily accepts a pre-existing link,
    # and resolving both sides is what catches a component swapped higher up.
    if staging.resolve() != (base.resolve() / "restore"):
        raise ValueError("restore staging directory resolves outside app storage")
    if not staging.is_dir():
        raise ValueError("restore staging directory is not a real directory")
    dest = staging / _staging_name(key)
    if is_link_or_junction(dest) or (dest.exists() and not dest.is_file()):
        raise ValueError("restore destination is not a regular file")
    fd, tmp_name = tempfile.mkstemp(prefix=".kc-restore-", dir=str(staging))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        storage.get_file(profile, region, bucket, "backup", key, str(tmp), account=account)
        size = tmp.stat().st_size
        if origin == ORIGIN_SELF:
            # The bytes that actually arrived, against the fingerprint taken from
            # the file this install sent. There is no window here for an overwrite
            # to slip through: this is not a claim about the object, it IS the
            # object. A mismatch means some other archive now sits at that key, so
            # the self claim does not hold.
            expected = recorded.get(key, "")
            if _body_fingerprint(tmp) != expected:
                # A mismatch alone does not settle it in the one case where this
                # install's own archive is still ON the drive: a co-writer overwrote
                # the key, so our bytes are the noncurrent version. One bounded read
                # of the version we RECORDED writing settles it on the same evidence
                # -- the same fingerprint, re-taken over the bytes that arrive on
                # that read.
                #
                # `None` keeps the original outcome exactly, so the refusal below is
                # still what an unrecoverable mismatch reaches. Nothing here can
                # make a restore accept bytes that failed the fingerprint; it can
                # only find bytes that pass it.
                #
                # Only where the mismatch would REFUSE. `foreign_ok` means the
                # caller has already said it will take whatever is current at the
                # key without proof, so under it there is no refusal to rescue --
                # and reaching past the current object would hand back different
                # bytes than that caller asked for, labelled a proven self archive
                # instead of the unverified one it accepted. The override keeps the
                # meaning it has today and this change is confined to the outcome it
                # exists to change.
                recovered = (
                    None
                    if foreign_ok
                    else _recover_recorded_version(
                        profile,
                        region,
                        bucket,
                        key,
                        account=account,
                        staging=staging,
                        expected=expected,
                    )
                )
                if recovered is None:
                    origin = ORIGIN_UNVERIFIED
                else:
                    # Onto the path the outer cleanup already owns, so there stays
                    # exactly one temp file to unlink on the way out. Same
                    # directory, so this is atomic.
                    os.replace(recovered, tmp)
                    # Re-read: `size` was measured on the overwriting object, and
                    # the reply reports the length of the bytes being handed back.
                    size = tmp.stat().st_size
        if origin != ORIGIN_SELF and not foreign_ok:
            # Refused after the transfer, which only an overwritten own-archive
            # reaches. The staged bytes are discarded and the destination is never
            # touched, so a refusal leaves nothing behind for anyone to apply.
            raise UnprovenArchive(origin, owner)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # The origin travels with the result. Every origin except a proven self archive
    # reached this point only because the caller passed the override, so the reply
    # is where a client learns WHICH of them it just accepted -- a co-tenant's, one
    # under this install's prefix with no upload record, or one carrying no id at
    # all. None of the three should be assumed to be this machine's.
    return {
        "path": str(dest),
        "bytes": size,
        "origin": origin,
        "install": owner,
    }


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
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.catalog import (  # noqa: F401
        MAX_OTHER_INSTALLS,
        _archive_row,
        _archive_sort_key,
        _checked,
        _install_folders,
        list_remote_backups,
        other_install_ids,
        read_remote_label,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import (  # noqa: F401
        LABEL_MAX_CHARS,
        redact_credentials,
        redact_exfiltration_urls,
        sanitize_label,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.fingerprints import (  # noqa: F401
        _SNAPSHOT_MANIFEST_NAME,
        _VOLATILE_MANIFEST_FIELDS,
        _archive_entries,
        _manifest_digest,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (  # noqa: F401
        _INSTALL_ID_RE,
        _KIND_BY_SUBPATH,
        INSTALL_KEY,
        KEY_SEP,
        ORIGIN_LEGACY,
        ORIGIN_OTHER,
        _default_label,
        _fallback_identity,
        _fallback_lock,
        _key_segments,
        _stored_identity,
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
        sel,
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
        due_for_nightly,
        due_for_sessions_nightly,
        nightly_enabled,
        nightly_failures,
        nightly_retry_delay_secs,
        nightly_run_witness,
        nightly_sessions_enabled,
        record_nightly_failure,
        scheduled_sessions_blocked_code,
        scheduled_sessions_blocked_reason,
        set_nightly,
        set_nightly_sessions,
        snapshot_redact,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.retention import (  # noqa: F401
        _RETENTION_GATE,
        RETENTION_KEEP_MIN,
        RETENTION_KEEP_STATE_KEY,
        RETENTION_UNCLAIMED_STATE_KEY,
        RETENTION_UNRECORDED_STATE_KEY,
        _audit_unfiled_authorization,
        _clamp_retention_keep,
        _current_version_is_ours,
        _delete_under_the_retention_gate,
        _newest_first,
        _prune_recorded_versions,
        _record_unclaimed,
        _record_unrecorded,
        _retention_keep_for_sweep,
        _RetentionAuthorizationWithdrawn,
        _RetentionCountWithdrawn,
        retention_keep,
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
        SESSIONS_CONVERSATIONS_RETAINED_KEY,
        STATE_DIR_LEAF,
        _account_state,
        _account_view,
        _account_view_checked,
        _clear_nightly_failure,
        _forget_unpersisted,
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
        kind_unavailable_reason,
    )
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts.uploads import (  # noqa: F401
        _STOP,
        CALLER_SCHEDULED,
        SEL_OP_BASELINE_PROBE,
        SEL_OP_RETENTION,
        SEL_OP_UPLOAD,
        NoReturn,
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

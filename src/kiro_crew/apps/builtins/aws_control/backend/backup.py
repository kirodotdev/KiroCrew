"""Backup — memory/workspace snapshots and session archives on ``backup/``.

Two backup kinds, one push path:

* **Snapshot** (the mockup's "Memory & workspace" row): the existing
  ``kiro_crew.snapshot`` engine builds its portable ``.tar.gz`` (memory,
  crons, config, skills, workspace, notifications, security — its component
  set, unchanged), and the archive is pushed to
  ``backup/snapshots/<install>/<name>.tar.gz``.
* **Sessions archive** (the "Sessions archive" row): one tarball of BOTH
  session halves — ``<data home>/sessions/`` (transcripts + rotated
  archives) and ``<kiro home>/sessions/cli/`` (the CLI replay logs) — pushed
  to ``backup/sessions/<install>/<stamp>.tar.gz``. Whole-set, not per-session:
  the "both halves move together" invariant is honoured by construction, and
  the per-session incremental integration with the storage inventory is future
  work.

**One drive can be reached by several installs.** Discovery is by tag, so a
second install finds the first one's bucket and writes to it by design — the
``<install>`` segment is what keeps the two apart afterwards, and it is a
random per-install id held in this app's own state, never the telemetry
install id. An archive uploaded before that segment existed carries no id and
is reported as being of unknown origin rather than claimed by whoever is
reading. See the "install identity" section below for why the id decides what
is permitted while the human-readable label decides only what is displayed.

**Restore is a download, deliberately.** A restore lands the archive in
``<app data dir>/restore/`` and hands back the path; nothing hot-swaps a
live ``memory.db`` or sessions dir under a running gateway. The snapshot
engine's own merge/replace tooling (or a stopped gateway) takes it from
there, and the UI copy says exactly that.

State (`<app data dir>/backup.json`): this install's identity, plus the last
run per kind, the nightly toggle per account, and the per-account retention
count. The nightly loop lives in the app's ``on_startup`` hook.

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

CALLER CONTRACT: handlers hold the consent gate; sync, subprocess/tar-bound
— call via ``asyncio.to_thread`` (pushes of a large sessions set can run
minutes; handlers use generous timeouts).
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, NoReturn, Optional

from kiro_crew import snapshot
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.deploy.engine import AWSError, _checked
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import file_lock, is_link_or_junction, open_lock_file
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.snapshot import snapshot_main

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"

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
KIND_SNAPSHOT = "snapshot"
KIND_SESSIONS = "sessions"

#: Wall clock for one backup push to S3, passed by both runners into
#: :func:`storage.put_file` rather than relying on its 600s default. The
#: nightly snapshot push runs unattended, and an owner-triggered sessions
#: archive may legitimately need the full hour -- the size ceiling is
#: ``storage._MAX_PINNED_TRANSFER_BYTES`` (5 GiB), which at 3600s still
#: requires a ~12 Mbit/s uplink, so a slower push fails at the bound rather
#: than holding the owner-billed transfer open indefinitely. Tests assert the
#: constant reaches the uploader on both paths, so it cannot go unread.
_PUSH_TIMEOUT_SECS = 3600


#: Backup state, holding the ``nightly`` bit that AUTHORIZES the unattended
#: upload loop. ``security._CREW_SECRET_LEAVES`` carries the matching
#: ``apps/aws-control/data`` entry, which puts this file -- and the atomic-write
#: temporary it is renamed from, and every sibling state file -- behind the
#: shared agent file-tool floor. The owner toggles nightly through the
#: owner-gated endpoint, and an agent cannot flip it by writing any path in
#: there. A test pins the two together, because moving this file out of that
#: directory would silently un-protect it.
STATE_DIR_LEAF = f"apps/{APP_NAME}/data"


def _state_path() -> Path:
    return app_data_dir(APP_NAME) / "backup.json"


def _read_state_checked() -> tuple[dict[str, Any], bool]:
    """``(state, readable)`` from ONE read of the state file.

    ``readable`` is False only when the file EXISTS and could not be read or
    parsed as an object. An ABSENT file is readable: there is nothing configured
    to misread, and every default in this module is written for that case.

    The distinction exists for one caller. :func:`read_state` folds absent,
    unreadable and corrupt into ``{}`` because every other reader wants a
    default; the retention sweep must not, because there a default silently
    replaces a configured value and the difference is measured in permanently
    erased bytes. See :func:`_retention_keep_for_sweep`.
    """
    try:
        raw = _state_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`, so an OSError-only
        # catch lets a state file of non-UTF-8 bytes escape as an exception -- exactly
        # the "exists but could not be read" case this function promises to answer
        # `({}, False)` for. It matters more here than anywhere else in the module: the
        # sweep resolves its keep count through this before entering its own
        # best-effort handler, and its caller's comment promises retention cannot fail
        # the run, so an escaping decode error would report a backup already off-host
        # as failed AND skip the audited unreadable branch.
        #
        # Deliberately not the wider `ValueError`: a surprising one is a bug that
        # should be loud rather than folded into "unreadable".
        return {}, False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    return data, True


def read_state() -> dict[str, Any]:
    return _read_state_checked()[0]


def write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=1))


class _StateUnreadable(OSError):
    """The state document exists but could not be read.

    A distinct type so :func:`_record_run` can say WHICH half of its
    read-modify-write failed. Both halves reach it as an ``OSError`` and the two
    are not interchangeable to whoever reads the log: "could not be read" sends
    that reader to check permissions and file handles, which is the wrong place
    to look when the truth is that the read was fine and ``write_state`` hit a
    full disk.

    It stays an ``OSError`` SUBCLASS deliberately. The other caller of
    :func:`_locked_state_update` -- :func:`set_nightly`, which lets the error
    reach its handler -- keeps behaving exactly as before this split, so nothing
    outside this module has to learn the new type to stay correct.
    """


def _read_state_for_update() -> dict[str, Any]:
    """The state document a read-modify-write is allowed to publish over.

    :func:`read_state` is a DISPLAY read: every failure collapses to ``{}`` so a
    render never crashes on a state file it could not load. That reading is
    wrong as the BASE of a mutation, because :func:`_locked_state_update` writes
    the whole document back -- an empty base there does not mean "no fields to
    carry forward", it means "replace every account's nightly toggle and run
    history with this one field". The sidecar lock does not help: it serializes
    writers, and the loss happens inside it.

    Only the missing file is a failure where ``{}`` is the truth (nothing has
    been written yet). An unreadable one -- a transient EACCES/EIO, a scanner
    holding the handle on Windows -- is state we still have, so the error is
    allowed to propagate and the mutation is abandoned rather than published
    over state nobody read.

    Corruption keeps its existing repair-on-write behaviour, which is a
    deliberate decision documented on :func:`_account_state`: a document that
    parsed to nothing usable carries nothing to lose. That covers a file which
    DECODED and then failed to parse. Bytes that are not UTF-8 never reached the
    parser, so they are the unreadable kind, not the corrupt kind, and the
    mutation is abandoned rather than published over them.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except UnicodeDecodeError as exc:
        # Repair-on-write below is justified for a document that PARSED to nothing
        # usable. Bytes that are not UTF-8 never reached the parser, so that reasoning
        # does not cover them: the file is still state we have, and publishing over it
        # would replace every account's toggles, retention count and run history --
        # including the `upload_versions` records the sweep's ownership test depends on
        # -- on the strength of a document nobody read. It is caught explicitly because
        # it is a `ValueError`, so the `OSError` clause below cannot see it.
        raise _StateUnreadable(
            errno.EILSEQ, "state file is not valid UTF-8", str(_state_path())
        ) from exc
    except OSError as exc:
        raise _StateUnreadable(
            exc.errno, exc.strerror or "state file could not be read", exc.filename
        ) from exc
    return data if isinstance(data, dict) else {}


def _locked_state_update(mutate) -> Any:
    """Read-modify-write the state file under the sidecar lock.

    Two backup kinds can finish concurrently (a manual run racing the
    nightly loop); an unlocked read-modify-write would let the later atomic
    write silently discard the earlier run record. Same sidecar-lock shape
    as the share ledger.

    Raises ``OSError`` when the existing state could not be read; see
    :func:`_read_state_for_update` for why that is not collapsed to an empty
    document here.
    """
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with _run_lock, open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
            state = _read_state_for_update()
            pending, uploads = _merge_pending(state)
            result = mutate(state)
            write_state(state)
            for account, kind, record in pending:
                _forget_unpersisted(account, kind, record)
            with _unpersisted_lock:
                for key, fingerprints in uploads.items():
                    held = _unpersisted_uploads.get(key, {})
                    held_versions = _unpersisted_versions.get(key, {})
                    for name, fingerprint in fingerprints.items():
                        if held.get(name) == fingerprint:
                            held.pop(name, None)
                            # The version is cleared with the fingerprint it arrived
                            # with, never on its own: the persisted state now carries
                            # both, so keeping either would be a second copy that can
                            # go stale.
                            held_versions.pop(name, None)
                    if not held:
                        _unpersisted_uploads.pop(key, None)
                    if not held_versions:
                        _unpersisted_versions.pop(key, None)
    return result


def _account_state(state: dict[str, Any], account: str) -> dict[str, Any]:
    """The per-account slice of the state file.

    Keyed by account, not global: two connected accounts each own their
    nightly toggle and run records, so switching the default cannot make one
    console report the other's backups. A corrupted file where either level
    decoded to a non-dict is REPLACED so mutations repair rather than crash
    (the read path treats the same corruption as empty).
    """
    accounts = state.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = state["accounts"] = {}
    entry = accounts.setdefault(account, {})
    if not isinstance(entry, dict):
        entry = accounts[account] = {}
    return entry


# --- install identity -------------------------------------------------------
#
# One drive can be reached by more than one install: discovery is by tag, so a
# second install finds the first one's bucket and writes into it BY DESIGN. What
# was missing is any way to tell the archives apart afterwards. The key carried a
# timestamp and six hex characters of collision avoidance -- ``_stamp``'s own
# docstring says that suffix is not an identity -- so an operator restoring an
# archive picked it by timestamp alone, and replacing this machine's memory with
# another machine's is the mistake the flat namespace made easy.
#
# The fix is a per-install id in the key prefix, plus a human-readable label
# beside it. The two are NOT interchangeable and the split is the whole design:
# the ID decides what is allowed, the LABEL decides only what a human reads.

#: Top-level key in ``backup.json`` holding this install's identity.
#:
#: Top level rather than under ``accounts``: the install is the same install
#: whichever account it backs up to, and keying it per account would mint a
#: second id for the same machine the first time the owner connects a second
#: account -- which would make one machine look like two in the very listing this
#: id exists to disambiguate.
INSTALL_KEY = "install"

#: An install id: ``uuid4().hex``. Fixed length and charset, which is what lets
#: :func:`classify_key` decide whether a key segment IS an install id rather than
#: an ordinary folder name someone created in the console.
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: The label sidecar each install writes at its OWN archive prefix
#: (``snapshots/<install>/_label.json``), so another install can render a name
#: instead of hex. It sits with the archives it labels, which means the same
#: listing that enumerates those archives also reveals whether a label exists --
#: no extra probe to find out, and one GET only for an install whose rows are
#: actually being displayed.
#:
#: The leading underscore is load-bearing twice over. It keeps the sidecar out of
#: the archive rows by a rule rather than by a name comparison, and
#: ``storage.validate_key`` requires a segment to START alphanumeric -- so this
#: object cannot be named by any request that comes through the drive or restore
#: routes, which validate every caller-supplied key.
LABEL_OBJECT_NAME = "_label.json"

#: Ceiling on a rendered label. A label written by ANOTHER install is
#: foreign-authored text arriving through the same door object names arrive
#: through, so it is bounded before it is rendered; the row it lands in is one
#: line of 12px caption.
LABEL_MAX_CHARS = 64

#: How many OTHER installs' prefixes one expanded listing will enumerate.
#:
#: A RUNAWAY BOUND, not a display choice, and the distinction is what makes the
#: number this large. An earlier draft capped this at 8 and picked the shown
#: subset with ``sorted(other_ids)[:8]`` -- hex order, which is arbitrary with
#: respect to recency, so a stale prefix (a reinstall, or the process-local
#: fallback id minting a fresh one per restart) could displace the install
#: holding the newest surviving archive. That is a coin flip on exactly the
#: replacement-machine path this expansion exists for. Set high enough that
#: truncation cannot bite a real drive, the subset stops being a decision at all:
#: every install present is listed, and which one sorts first only affects the
#: order rows appear in, which the timestamp sort then fixes anyway.
#:
#: Each install still costs a list call per kind plus at most one label read, all
#: on the owner's bill -- which is why the whole expansion is opt-in. What this
#: bound protects is the pathological case, not the ordinary one: two machines is
#: what the feature is for.
MAX_OTHER_INSTALLS = 32

#: Where an archive came from, as the listing and the restore reply name it.
#:
#: ``legacy`` is not a synonym for "somebody else's": it means the key predates
#: the namespace and carries no id at all, so its origin is genuinely UNKNOWN.
#: It is rendered as unknown for that reason and never claimed as this install's.
ORIGIN_SELF = "self"
ORIGIN_OTHER = "other"
ORIGIN_LEGACY = "legacy"

#: An archive sitting under THIS install's prefix that this install has no record
#: of uploading.
#:
#: The prefix alone cannot prove ownership, and that is not a detail. An install id
#: is a random 32 hex, but it is carried as a KEY PREFIX, which makes it a folder
#: name anyone who can list the bucket can read -- and a bucket the feature shares
#: by design has other writers. So a co-writer can list the prefixes and PUT an
#: archive under this install's own, and a gate that trusted the prefix would then
#: hand that archive back with no confirmation at all. "The id is the one part
#: another install cannot restate" is true of a READER and false of a WRITER.
#:
#: What this install genuinely knows is which keys IT uploaded, because it wrote
#: them down: :func:`_record_run` records every successful push in state that the
#: shared agent file-tool fence already protects. So ``self`` now means "in that
#: record" and nothing weaker, and everything else under the prefix is this --
#: probably ours, not provably ours, and therefore refused by
#: :func:`restore_download` without an explicit override, exactly like an archive
#: that carries no id at all. The refusal is in the BACKEND on purpose: a
#: confirmation dialog only binds the client that shows it.
ORIGIN_UNVERIFIED = "unverified"

#: How many of this install's own uploaded keys are remembered per account. The
#: panel lists 20 per kind, so this covers a long history of both kinds while
#: keeping the state document bounded; the oldest entry is dropped when a new
#: upload arrives. Falling off the end is not a correctness problem -- an archive
#: whose record has aged out reads as :data:`ORIGIN_UNVERIFIED` and asks, which is
#: the safe direction to fail in.
MAX_REMEMBERED_UPLOADS = 200

#: Longest staged filename, in bytes. ``NAME_MAX`` is 255 on ext4 and on the other
#: filesystems this app is deployed to, and a key segment is capped at 255 characters
#: upstream, so a prefix added to a basename can otherwise overrun it and the restore
#: fails with ``ENAMETOOLONG`` instead of producing a file.
STAGING_NAME_MAX_BYTES = 255

#: Subpath per backup kind. One place, because three call sites (upload, listing,
#: key classification) have to agree on it or the namespace splits.
KIND_SUBPATHS: dict[str, str] = {KIND_SNAPSHOT: "snapshots", KIND_SESSIONS: "sessions"}

#: The floor, and not a style choice: at ``keep=0`` the sweep would delete the
#: archive the run has just uploaded, so a backup would end by destroying itself.
#: Every configured value is clamped through :func:`_clamp_retention_keep`. The
#: newest complete archive is protected SEPARATELY from this floor, because a floor
#: is a property of the number and the protection must not depend on the number.
RETENTION_KEEP_MIN = 1

#: Where the count lives in ``backup.json``, and the whole switch: this key present
#: and holding a usable count is the only thing that enables retention. Absent, or
#: holding anything else, means keep everything -- there is no default that deletes.
#:
#: Deleting an object version cannot be undone, and these are the operator's bytes
#: in the operator's bucket, so the two ways of being wrong do not compare. Shipping
#: off costs storage an operator can see in a listing and fix with one command;
#: shipping on silently destroys archives somebody was deliberately keeping and
#: leaves them nothing to restore from. It is also the house shape for this kind of
#: switch rather than a new policy: ``nightly`` ships off, reads fail-closed, and is
#: never inferred from an adjacent grant. Retention defaulting to delete would be
#: the first capability here to act destructively on operator data unasked.
#:
#: Read only through :func:`_retention_keep_for_sweep`, never inferred from
#: ``nightly`` or any other grant in either direction: authorizing unattended
#: UPLOADS is not authorizing permanent DELETES.
#:
#: Per ACCOUNT, because the drive is per account: two connected accounts are two
#: buckets and two bills, and one number for both would apply a decision made about
#: one to the other.
RETENTION_KEEP_STATE_KEY = "retention_keep"

#: SEL operation names for the two decisions this module asks
#: :func:`_authorize_upload` to make.
#:
#: Separate, because by the time retention runs the upload has ALREADY succeeded.
#: Filing a refused sweep as a denied ``backup_upload`` would put a denial in the
#: log for a transfer that completed, and an auditor counting denied uploads would
#: be counting a push that happened.
SEL_OP_UPLOAD = "aws_control.backup_upload"
SEL_OP_RETENTION = "aws_control.backup_retention"

#: An id for a process that could not persist one. See :func:`install_identity`.
_fallback_identity: dict[str, str] = {}
_fallback_lock = threading.Lock()


#: The separator inside an S3 OBJECT KEY, which is not a filesystem separator.
#: S3 keys are ``/``-delimited by the S3 API on every platform: a key written on
#: Linux is read back as the same string on Windows, and ``os.sep`` must never
#: appear in one. Every key this module parses goes through :data:`KEY_SEP` and the
#: two helpers below so that fact is stated once, in the place a reader would
#: otherwise have to infer it -- and so a future edit cannot quietly turn key
#: parsing into path parsing.
KEY_SEP = "/"


def _key_segments(key: str) -> list[str]:
    """The delimited segments of an S3 object key."""
    return key.split(KEY_SEP)


def _key_basename(key: str) -> str:
    """The last segment of an S3 object key."""
    return key.rsplit(KEY_SEP, 1)[-1]


def _body_fingerprint(path: Path) -> str:
    """The MD5 of a file's bytes.

    Recording a KEY proves this install wrote something at that path; it does not
    prove the object sitting there NOW is that something. A key is a name, and
    anyone who can write to the bucket can write to a name -- so on a shared drive
    a co-writer can overwrite an archive after it was recorded, and a check that
    only matched keys would hand back their bytes as ours.

    A fingerprint closes that, and it closes it WITHOUT depending on anything S3
    reports. This is taken twice over local bytes: once over the file this install
    uploads, and once over the file a restore has finished downloading. The restore
    compares the two. No object metadata is read, so there is no ETag to reason
    about -- which also means no multipart or encryption-mode caveat, because S3's
    ETag stops equalling the body MD5 under multipart and under SSE-KMS, and this
    comparison never consults it either way.

    ``usedforsecurity=False`` because this is not a security digest: it detects an
    overwrite between two points in this install's own timeline. It is passed so the
    call still works where a hardened build refuses MD5 by default.
    """
    digest = hashlib.md5(usedforsecurity=False)  # noqa: S324 -- content hash, not a security digest
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_label(install_id: str) -> str:
    """The label a fresh install starts with.

    Deliberately NOT the hostname, the user name, or anything else about the
    machine. This string is published to the bucket so another install can read
    it, and an identifier the owner never chose to share must not be the value
    that leaves by default. Four hex characters of the id make two installs
    distinguishable on sight, which is all a default has to achieve; the owner
    renames it to something meaningful whenever they care to.
    """
    return f"install-{install_id[:4]}"


def sanitize_label(label: Any, *, fallback: str = "") -> str:
    """A label safe to render, from a value that may not be ours.

    Applied to a label read from the BUCKET, where the writer is another install
    and the bytes are foreign-authored text. ``storage.list_section`` already runs
    object names through these same two redactors for exactly this reason -- a key
    authored outside this app can embed a credential or a beacon URL -- and a
    label arrives through the same door with the same problem, so it gets the same
    treatment rather than a weaker one because it is "just a name".

    Control characters go first: they are what turns one caption line into
    something that overwrites the row above it, and they survive both redactors
    untouched. Then the two egress redactors, then the length bound.

    Applied to the LOCAL label too, on the way in. The owner types that one, so it
    is not hostile -- but it is the string this install publishes to a drive
    another install reads, and a value that would be scrubbed on arrival has no
    business being sent.
    """
    if not isinstance(label, str):
        return fallback
    text = "".join(ch for ch in label if ch.isprintable()).strip()
    if not text:
        return fallback
    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    text = text.strip()
    if not text:
        return fallback
    return text[:LABEL_MAX_CHARS]


def _stored_identity(state: dict[str, Any]) -> Optional[dict[str, str]]:
    """This install's identity as ``state`` holds it, or None when it has none."""
    entry = state.get(INSTALL_KEY)
    if not isinstance(entry, dict):
        return None
    install_id = entry.get("id")
    if not isinstance(install_id, str) or not _INSTALL_ID_RE.match(install_id):
        return None
    return {
        "id": install_id,
        "label": sanitize_label(entry.get("label"), fallback=_default_label(install_id)),
    }


def install_identity() -> dict[str, str]:
    """This install's id and label, minting the id on first use.

    Minted HERE rather than reused from ``beacon.install_id()``. That one is the
    telemetry egress identity and is only materialised once telemetry consent
    exists -- ``metrics/provider.py`` says so where it reads it -- so calling it
    from the backup path would create a telemetry identity on a host that opted
    out of telemetry. The technique is worth copying (a random ``uuid4`` hex, no
    machine facts in it); the VALUE is not, and the two must not become the same
    number, or turning one off would change the other's meaning.

    Written through :func:`_locked_state_update`, which buys the atomic write and
    the agent file-tool fence that already protect the nightly toggle in this same
    document, and serialises two processes racing the first mint.

    A state file that cannot be read or written falls back to a PROCESS-LOCAL id.
    That is the lesser of the two evils available: refusing would turn an
    unwritable state file into a backup that stops running, which is a regression
    on today's behaviour (an unpersistable run already uploads and is held in
    memory -- see :func:`_record_run`), while a fresh id per process at least
    keeps one process's archives together and still tells them apart from another
    install's. It is logged, and a restart on a still-broken file yields a new
    prefix -- strictly better than the single unattributable pile it replaces.
    """
    stored = _stored_identity(read_state())
    if stored is not None:
        return stored

    def mutate(state: dict[str, Any]) -> dict[str, str]:
        entry = state.get(INSTALL_KEY)
        if not isinstance(entry, dict):
            entry = state[INSTALL_KEY] = {}
        # Re-read INSIDE the lock: another process may have minted between the
        # unlocked read above and here, and two ids for one install is the one
        # outcome this whole change exists to prevent.
        current = _stored_identity(state)
        if current is not None:
            return current
        # A transient failure that has since cleared must persist the id this
        # process ALREADY UPLOADED UNDER, not mint a second one: minting would
        # split one install's archives across two prefixes and make the earlier
        # ones unverifiable, which is the exact confusion the id exists to remove.
        with _fallback_lock:
            fresh = _fallback_identity.get("id") or uuid.uuid4().hex
        entry["id"] = fresh
        entry["label"] = sanitize_label(entry.get("label"), fallback=_default_label(fresh))
        return {"id": fresh, "label": entry["label"]}

    try:
        return _locked_state_update(mutate)
    except OSError as exc:
        with _fallback_lock:
            if not _fallback_identity:
                fresh = uuid.uuid4().hex
                _fallback_identity.update({"id": fresh, "label": _default_label(fresh)})
                logger.error(
                    "aws-control: this install's backup id could not be stored (%s), so a "
                    "temporary id is used for the life of this process; archives it uploads "
                    "are attributed to it and not to an earlier one",
                    exc,
                )
            return dict(_fallback_identity)


def set_install_label(label: str) -> dict[str, str]:
    """Rename this install, and return the identity as stored.

    The label is the only part an owner edits, and editing it changes nothing
    except what a human reads: :func:`classify_key` and the restore gate key on
    the id, so a rename cannot make a foreign archive restorable or this
    install's own archive refused.
    """
    identity = install_identity()
    cleaned = sanitize_label(label, fallback=_default_label(identity["id"]))

    def mutate(state: dict[str, Any]) -> dict[str, str]:
        entry = state.get(INSTALL_KEY)
        if not isinstance(entry, dict):
            entry = state[INSTALL_KEY] = {}
        entry.setdefault("id", identity["id"])
        entry["label"] = cleaned
        return {"id": str(entry["id"]), "label": cleaned}

    return _locked_state_update(mutate)


def classify_key(key: str, install_id: str, uploaded: Optional[set[str]] = None) -> tuple[str, str]:
    """``(origin, owning install id)`` for one backup archive key.

    Reads the KEY and, for the one verdict that needs more than a key, this
    install's own record of what it uploaded. It never reads the published label,
    the archive's contents, or anything else a writer authors -- so no string an
    install writes about itself can move an archive across this line.

    The owning id comes from the key prefix, which is where S3 itself put it. But
    a prefix is a FOLDER NAME on a bucket that, by this feature's own premise, has
    other writers: a co-writer can list the prefixes and upload beneath this
    install's own. So the prefix proves who the key CLAIMS to belong to and
    nothing more, and ``self`` is reserved for a key this install can show it
    wrote -- see :data:`ORIGIN_UNVERIFIED` for why the two must not be conflated.
    Pass ``uploaded`` (from :func:`uploaded_keys`) wherever the answer decides
    something; omit it and a key under this install's prefix reads as unverified,
    which is the safe default for a caller that did not ask the question.

    A key with no id segment is ``legacy``: written before the namespace existed,
    origin unknown. Unknown is reported as unknown rather than resolved in either
    direction, because both readings are wrong. Claiming it as this install's
    would re-create the exact mistake the namespace prevents, and calling it
    foreign would refuse an operator their own archive from before the upgrade.
    """
    parts = _key_segments(key)
    if len(parts) == 3 and _INSTALL_ID_RE.match(parts[1]):
        owner = parts[1]
        if owner != install_id:
            return ORIGIN_OTHER, owner
        if uploaded is not None and key in uploaded:
            return ORIGIN_SELF, owner
        return ORIGIN_UNVERIFIED, owner
    return ORIGIN_LEGACY, ""


def uploaded_objects(account: str) -> dict[str, str]:
    """Every archive key THIS install recorded uploading, with its body fingerprint.

    The trusted half of :func:`classify_key`. It is trustworthy for one reason: it
    is local. It is written only by :func:`_record_run`, after this process's own
    successful push, into the state document the shared agent file-tool fence
    already covers -- so unlike the key prefix, nothing that can write to the
    BUCKET can add to it.

    The fingerprint is what makes the record about an OBJECT rather than a path.
    :func:`restore_download` compares it against the bytes that actually arrive, so
    an archive overwritten at a recorded key stops counting as ours. An entry may
    carry an empty fingerprint (a run recorded before one was available), which
    authenticates as unproven rather than as ours -- unknown is not a pass.

    Includes this process's unpersisted uploads. A push whose state write failed
    still happened, and the archive is still in the bucket; leaving it out would
    make an operator confirm an archive this process uploaded minutes ago.
    """
    with _run_lock:
        return _uploaded_objects_locked(account)


def _uploaded_objects_locked(account: str) -> dict[str, str]:
    entry = _account_view(account)
    stored = entry.get("uploads")
    objects: dict[str, str] = {}
    if isinstance(stored, dict):
        objects = {k: v for k, v in stored.items() if isinstance(k, str) and isinstance(v, str)}
    elif isinstance(stored, list):
        # A document written before the fingerprint existed. Its keys are still
        # this install's own, but nothing pins their bytes, so they carry no
        # fingerprint and authenticate as unproven.
        objects = {k: "" for k in stored if isinstance(k, str)}
    path = _state_key()
    with _unpersisted_lock:
        objects.update(_unpersisted_uploads.get((path, account), {}))
        for (state_path, acct, _kind), record in _unpersisted_runs.items():
            if state_path == path and acct == account:
                held = record.get("key")
                if isinstance(held, str):
                    objects[held] = str(record.get("fingerprint", "") or "")
    return objects


def uploaded_keys(account: str) -> set[str]:
    """Just the keys, for the offline classification the listing does per row."""
    return set(uploaded_objects(account))


def uploaded_versions(account: str) -> dict[str, str]:
    """Key -> the ``VersionId`` this install recorded writing under it.

    A key is absent when no version was recorded for it: an archive uploaded before
    this was recorded at all, an unversioned bucket, or a ``put-object`` response
    that named none. Retention reads absence as "do not touch", which is the only
    safe reading -- being in ``uploaded_keys`` proves this install wrote A version
    of a key, and only this map says WHICH.

    Merges the in-process records for the same reason :func:`uploaded_objects`
    does: a run whose state write failed is still this install's own upload, and
    its version is the one thing that makes it retireable.
    """
    entry = _account_view(account)
    stored = entry.get("upload_versions")
    versions: dict[str, str] = {}
    if isinstance(stored, dict):
        versions = {
            key: value
            for key, value in stored.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }
    path = _state_key()
    with _unpersisted_lock:
        versions.update(_unpersisted_versions.get((path, account), {}))
        for (state_path, acct, _kind), record in _unpersisted_runs.items():
            if state_path != path or acct != account:
                continue
            held = record.get("key")
            version = record.get("version")
            if isinstance(held, str) and isinstance(version, str) and version:
                versions[held] = version
    return versions


class UnprovenArchive(RuntimeError):
    """A restore named an archive this install cannot prove is its own.

    Raised for every origin except :data:`ORIGIN_SELF`, and that uniformity is the
    point. An earlier draft refused only :data:`ORIGIN_OTHER` and left the
    unverified and legacy cases to a confirmation dialog in the dashboard -- which
    means the guarantee existed in the FRONTEND and not in the backend, so any
    caller that did not come through that dialog restored a planted archive with no
    override at all. A safety property that only one client enforces is not a
    safety property. So the rule is now stated once, where the bytes are: prove it
    is ours, or say explicitly that you accept it might not be.

    Carries the origin and the owning id so the refusal can NAME what it refused.
    "another install" with nothing after it gives the operator nothing to decide
    on, and the three cases need different words -- an archive from a co-tenant, an
    archive under our own prefix we have no record of writing, and an archive from
    before install ids existed are three different situations to be told about.

    Overridable on purpose, and the override is not a formality. Restoring onto a
    replacement machine means nothing in the bucket is provably this install's --
    that is what disaster recovery IS -- so a refusal with no way past it would
    block the one case the backup exists for. What the gate buys is that such a
    restore becomes a decision someone made rather than a timestamp they misread.
    """

    #: Prose per origin. Distinct sentences rather than one generic refusal,
    #: because what the operator should check differs in each case.
    _REASONS = {
        ORIGIN_OTHER: (
            "this archive was uploaded by another install; restoring it would replace "
            "this machine's data with that one's"
        ),
        ORIGIN_UNVERIFIED: (
            "this archive sits under this install's own prefix but this install has no "
            "record of uploading it, and anything that can write to the drive can create "
            "that prefix; restoring it may replace this machine's data with another's"
        ),
        ORIGIN_LEGACY: (
            "this archive carries no install id, so which machine wrote it is unknown; "
            "restoring it may replace this machine's data with another's"
        ),
    }

    def __init__(self, origin: str, install_id: str) -> None:
        super().__init__(self._REASONS.get(origin, self._REASONS[ORIGIN_UNVERIFIED]))
        self.origin = origin
        self.install_id = install_id


#: Runs whose archive reached the bucket but whose state write did not land,
#: held for the life of THIS process. :func:`last_runs` merges them in, and that
#: is the whole point: it is what stops :func:`due_for_nightly` re-firing the
#: unattended loop on a stamp that was never persisted. See :func:`_record_run`.
#:
#: Keyed by the state FILE as well as the account and kind. An entry is a claim
#: about one state document -- "this file is missing a run it should have" -- so it
#: must never answer for a different one. Production resolves a single fixed path
#: (``app_data_dir`` is ``app_dir(name) / "data"``, and nothing repoints it), so
#: this is not guarding a live scenario; what it buys is that the tests are
#: hermetic by construction instead of through a reset hook every future test has
#: to remember to call. :func:`_state_key` resolves the element without raising.
#:
#: Bounded by the accounts the owner has actually connected times the two backup
#: kinds, and an entry is dropped as soon as one write for that key succeeds.
_unpersisted_runs: dict[tuple[str, str, str], dict[str, Any]] = {}
_unpersisted_uploads: dict[tuple[str, str], dict[str, str]] = {}
#: Key -> the ``VersionId`` of a held upload, the version half of the map above.
#: Retention can only retire a key whose version it knows, so a held upload that
#: recovers its fingerprint and loses its version recovers into an archive nothing
#: can ever reclaim. Trimmed to the keys ``_unpersisted_uploads`` holds rather than
#: to its own count, so exactly ONE bound governs both and they cannot drift.
_unpersisted_versions: dict[tuple[str, str], dict[str, str]] = {}
_unpersisted_lock = threading.Lock()
# Serialize record creation through recovery/acknowledgement in this process.
# The sidecar still serializes disk updates across processes; it cannot order
# successful uploads whose state was inaccessible to another process.
_run_lock = threading.RLock()
_run_process = uuid.uuid4().hex
_run_sequence = 0


def _run_is_newer(candidate: dict[str, Any], previous: Any) -> bool:
    """Local sequence orders one process; other/legacy records use wall time."""
    if not isinstance(previous, dict):
        return True
    process = candidate.get("process")
    sequence, old_sequence = candidate.get("sequence"), previous.get("sequence")
    if (
        isinstance(process, str)
        and process
        and process == previous.get("process")
        and type(sequence) is int
        and type(old_sequence) is int
    ):
        return sequence > old_sequence
    old_at = previous.get("at")
    return not isinstance(old_at, str) or old_at < str(candidate.get("at", ""))


def _merge_uploads(
    entry: dict[str, Any],
    additions: dict[str, str],
    versions: Optional[dict[str, str]] = None,
) -> None:
    uploads = entry.setdefault("uploads", {})
    if not isinstance(uploads, dict):
        uploads = entry["uploads"] = {}
    uploads.update(additions)
    for stale in list(uploads)[: max(0, len(uploads) - MAX_REMEMBERED_UPLOADS)]:
        uploads.pop(stale, None)
    # The version map lives beside `uploads` rather than inside its values, because
    # `uploaded_objects` filters that map to STRING values and would silently drop a
    # key whose value became a dict -- taking `classify_key` and the restore
    # ownership check down with it.
    #
    # It is trimmed to the keys `uploads` still holds rather than to its own count,
    # so exactly ONE bound exists and the two maps cannot drift apart: a key that
    # fell off `uploads` can never be retired, so its version would be dead weight,
    # and a version whose key is gone answers a question nobody asks.
    recorded = entry.setdefault("upload_versions", {})
    if not isinstance(recorded, dict):
        recorded = entry["upload_versions"] = {}
    recorded.update(versions or {})
    for orphan in [key for key in recorded if key not in uploads]:
        recorded.pop(orphan, None)


def _merge_pending(state: dict[str, Any]) -> tuple[list, dict]:
    """Carry bounded recovery metadata into the next successful state update."""
    path = _state_key()
    with _unpersisted_lock:
        pending = [
            (account, kind, record)
            for (state_path, account, kind), record in _unpersisted_runs.items()
            if state_path == path
        ]
        uploads = {
            key: dict(fingerprints)
            for key, fingerprints in _unpersisted_uploads.items()
            if key[0] == path
        }
        versions = {key: dict(ids) for key, ids in _unpersisted_versions.items() if key[0] == path}
    for account, kind, record in pending:
        entry = _account_state(state, account)
        runs = entry.setdefault("runs", {})
        if not isinstance(runs, dict):
            runs = entry["runs"] = {}
        if _run_is_newer(record, runs.get(kind)):
            runs[kind] = record
    for (_, account), fingerprints in uploads.items():
        # The versions travel with the fingerprints. Recovering a key WITHOUT its
        # version persists an upload retention can never retire, and the key carries
        # a timestamp and entropy so it is never re-uploaded to self-correct.
        _merge_uploads(
            _account_state(state, account),
            fingerprints,
            versions.get((path, account), {}),
        )
    return pending, uploads


def _state_key() -> str:
    """The state-file element of a :data:`_unpersisted_runs` key, without raising.

    :func:`_state_path` is NOT a pure path join. It goes through
    :func:`app_data_dir`, whose last statement is
    ``mkdir(parents=True, exist_ok=True)``, so merely resolving the path raises
    ``OSError`` on a read-only filesystem, on EACCES/ENOSPC, or when a parent
    path is a file. Those are precisely the conditions this overlay exists to
    survive, which makes an unguarded key derivation self-defeating:
    :func:`_record_run` derives the key from INSIDE its own except handler, where
    an exception would 500 a request whose archive is already in the bucket --
    the exact defect this change exists to remove, reintroduced one layer in.
    The read is already guarded (:func:`read_state` swallows ``OSError``), so
    without this the failure is absorbed once and then raised by the very next
    statement.

    A failure returns a SENTINEL rather than skipping the work. Skipping would
    drop the held record in exactly the case the hold exists for. One sentinel is
    consistent for the life of the process, so the overlay still answers
    :func:`last_runs`, the completed upload still reports, and no caller raises.

    All three key sites go through here rather than each guarding itself: one
    place to reason about, and one place a future edit cannot forget.
    """
    try:
        return str(_state_path())
    except OSError:
        return ""


def _remember_unpersisted(account: str, kind: str, record: dict[str, Any]) -> None:
    path = _state_key()
    with _unpersisted_lock:
        run_key = (path, account, kind)
        if _run_is_newer(record, _unpersisted_runs.get(run_key)):
            _unpersisted_runs[run_key] = record
        uploads = _unpersisted_uploads.setdefault((path, account), {})
        uploads[record["key"]] = str(record.get("fingerprint", "") or "")
        version = str(record.get("version", "") or "")
        versions = _unpersisted_versions.setdefault((path, account), {})
        if version:
            versions[record["key"]] = version
        for stale in list(uploads)[: max(0, len(uploads) - MAX_REMEMBERED_UPLOADS)]:
            uploads.pop(stale, None)
        # One bound, applied to `uploads`, then mirrored: a version whose key has
        # fallen off can never be retired, so it would be dead weight.
        for orphan in [key for key in versions if key not in uploads]:
            versions.pop(orphan, None)


def _forget_unpersisted(account: str, kind: str, persisted: dict[str, Any]) -> None:
    """Clear exactly the held record processed by a successful state update.

    Its fingerprint is merged even when the final run slot supersedes it.
    Equal wall times do not identify equal runs. Full record equality also
    keeps legacy records without process/sequence metadata acknowledgeable.
    """
    key = (_state_key(), account, kind)
    with _unpersisted_lock:
        if _unpersisted_runs.get(key) == persisted:
            _unpersisted_runs.pop(key, None)


def _merge_unpersisted(account: str, runs: dict[str, Any]) -> dict[str, Any]:
    """Overlay this process's unpersisted runs onto what the state file holds.

    Same-process runs use local sequence, including equal/backwards wall times.
    Other processes and legacy records retain the wall-time fallback; this is
    not proof of global order when their state updates were unobservable.
    """
    path = _state_key()
    with _unpersisted_lock:
        remembered = {
            kind: record
            for (state_path, acct, kind), record in _unpersisted_runs.items()
            if state_path == path and acct == account
        }
    for kind, record in remembered.items():
        if _run_is_newer(record, runs.get(kind)):
            runs[kind] = record
    return runs


def _record_run(
    account: str, kind: str, key: str, size: int, fingerprint: str = "", version: str = ""
) -> dict[str, Any]:
    with _run_lock:
        return _record_run_locked(account, kind, key, size, fingerprint, version)


def _record_run_locked(
    account: str, kind: str, key: str, size: int, fingerprint: str, version: str = ""
) -> dict[str, Any]:
    global _run_sequence
    _run_sequence += 1
    record: dict[str, Any] = {
        # Include PID so a fork cannot reuse its parent's sequence namespace.
        "process": f"{_run_process}:{os.getpid()}",
        "sequence": _run_sequence,
        "key": key,
        "bytes": size,
        # Carried on the held record as well, so an upload whose state write failed
        # can still be authenticated from this process's memory.
        "fingerprint": fingerprint,
        # The S3 version this upload created. On a versioned bucket this is the only
        # value that identifies WHICH bytes under the key we wrote, which is what
        # retention needs before it erases anything. Empty when the bucket is
        # unversioned or the response named none, and retention then declines the
        # key rather than guessing.
        "version": version,
        # Provisional. The authoritative stamp is taken inside `mutate`, under the
        # sidecar lock -- see there. This value survives only on the path where the
        # READ fails, because `mutate` never runs then.
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        entry = _account_state(state, account)
        runs = entry.setdefault("runs", {})
        if not isinstance(runs, dict):
            # A corrupted non-dict `runs` must not crash AFTER the archive
            # already uploaded (500 + no ledger entry + duplicate on retry).
            runs = entry["runs"] = {}
        _merge_uploads(entry, {key: fingerprint}, {key: version} if version else None)
        # Keep the observed wall time, even on a coarse or backwards clock.
        # Local sequence, not timestamp precision, orders this process's runs.
        record["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        # This new locked write supersedes prior state, regardless of its clock
        # or process. Recency comparisons belong only to best-effort recovery.
        runs[kind] = record
        return record

    try:
        recorded = _locked_state_update(mutate)
    except OSError as exc:
        # Two things are true here and only one of them was handled before.
        #
        # (1) The archive is ALREADY in the bucket, so raising would 500 a
        # request whose upload succeeded and send the operator back to the button
        # for a duplicate -- the same harm the corrupted-`runs` branch above
        # avoids. So this still does not raise.
        #
        # (2) Not raising is not the end of it. `due_for_nightly` decides
        # due-ness from the PERSISTED stamp and `hooks._run_once` calls it on
        # every wake, so a write that never landed leaves the loop permanently
        # due: it re-uploads, unattended and billable, on every wake for as long
        # as this process lives, behind one log line nobody reads. Holding the
        # run in process-local memory -- which `last_runs` merges in -- bounds
        # that to at most one extra upload per gateway restart.
        #
        # Which half failed decides the wording, because both arrive as OSError
        # and they send a reader to different places: `_StateUnreadable` means
        # the existing document could not be read and was deliberately not
        # published over, while a plain OSError means the read was fine and
        # `write_state` failed (ENOSPC, EROFS, EIO). Reporting a full disk as
        # "could not be read" points at permissions instead.
        stage = "could not be read" if isinstance(exc, _StateUnreadable) else "could not be written"
        _remember_unpersisted(account, kind, record)
        logger.error(
            "aws-control: %s backup for %s uploaded, but its state file %s, so the run is "
            "not on disk; holding it in memory for this process so the nightly loop does "
            "not re-upload the same archive: %s",
            kind,
            account,
            stage,
            exc,
        )
        return record
    return recorded


def _stamp() -> str:
    """A second-resolution timestamp plus entropy.

    A manual run racing the nightly loop can land in the same second; on a
    versioned bucket an identical key does not destroy the earlier archive,
    but it hides it — listings and restore only see the current version. The
    hex suffix keeps every archive its own key.
    """
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"


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
    account: str, profile: str, region: str, *, caller: str, operation: str = SEL_OP_UPLOAD
) -> None:
    """Re-check the authorization decisions at the moment of upload.

    An archive build can run for minutes inside a worker thread; consent
    withdrawal, the app being disabled, or the profile being REPOINTED at a
    different account during the build must stop the upload — the bytes have
    not left the machine until ``put_file`` runs. The account check is a LIVE
    ``sts:GetCallerIdentity`` (free, non-mutating) through the package's
    single sync chokepoint, not the cached snapshot.
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
    granted, reason = aws_consent.is_granted(aws_consent.SERVICE_S3, profile=profile, region=region)
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
    grant = aws_consent.read_grant(aws_consent.SERVICE_S3)
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


def _clamp_retention_keep(raw: Any) -> int | None:
    """A usable stored count, or ``None`` when there is none.

    ``None`` means keep everything. Absent, a string, a float, a bool -- none of
    those is a count somebody chose, and the only safe reading of a value nobody
    chose is not to delete. There is deliberately no fallback number: a fallback
    here would be a permanent delete performed on a value the operator never wrote.

    Zero and negatives ARE counts somebody wrote, just unusable ones, so they clamp
    UP to :data:`RETENTION_KEEP_MIN` rather than turning retention off -- switching
    it off on a typo would silently stop doing the thing the operator asked for, and
    that direction deletes FEWER archives than the stored number named. There is no
    ceiling to clamp down to: a count larger than the number of archives that exist
    simply keeps all of them, which is not a harm worth refusing an operator over, and
    honouring what they wrote beats reading it as something smaller or as nothing.

    ``bool`` is screened before ``int`` on purpose: ``True`` IS an ``int`` in
    Python, so a state file carrying ``"retention_keep": true`` would otherwise
    resolve to ``keep=1`` -- a plausible-looking number nobody configured, and the
    most destructive one available.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return max(RETENTION_KEEP_MIN, raw)


def _retention_keep_for_sweep(account: str) -> tuple[int | None, str]:
    """The sweep's keep count, or ``None`` and why the sweep is keeping everything.

    Retention is OFF unless this account's state holds a usable count, so ``None``
    is the ordinary answer on an install nobody has configured, not an error. The
    reason comes back with it because the ways of reaching ``None`` need different
    handling downstream: an ABSENT key is the configured behaviour and a successful
    sweep that had nothing to do, while a state file this process could not read --
    or a key that is present and does not resolve to a usable count -- is an anomaly
    worth a warning even though it keeps everything too. Those two are the same
    reason on purpose: in both, somebody's intent is not being honoured, and the
    difference between "unreadable file" and "unreadable value" is not one an
    operator can act on differently.

    Both are fail-closed, the direction :func:`nightly_enabled` picks for the same
    kind of reason. A sweep that does not run costs storage the operator can see and
    reclaim; a sweep run on a value nobody configured erases archives permanently.
    An operator who set ``keep=50`` and meets a transient read failure after the
    upload must not have 47 archives deleted by a number this process guessed.

    Never derived from ``nightly`` or any other grant, in either direction.
    Authorizing unattended uploads is not authorizing permanent deletes, and a
    configured count is not withdrawn by turning the nightly off.
    """
    view, readable = _account_view_checked(account)
    if not readable:
        return None, "the retention setting could not be read"
    if RETENTION_KEEP_STATE_KEY not in view:
        return None, "retention is not enabled"
    keep = _clamp_retention_keep(view.get(RETENTION_KEEP_STATE_KEY))
    if keep is None:
        # The key is THERE and does not resolve. Somebody configured something and it
        # is not being honoured, so this is the anomaly reason rather than the off one:
        # reporting it as "not enabled" would audit a successful sweep and leave an
        # operator believing a count they wrote is in force.
        return None, "the retention setting could not be read"
    return keep, ""


#: Serializes a sweep's FINAL count check with the delete it authorizes, and with
#: :func:`set_retention_keep`. Re-reading the count is not enough on its own: whatever
#: sits between the read and the delete is a window, and an owner's ``keep:null``
#: landing inside it still loses versions they chose to keep.
#:
#: One lock for retention rather than one per account, deliberately. A per-account map
#: would be unbounded state keyed by a caller-supplied string, and the cost of the
#: coarser lock is only that two accounts' sweeps queue at their last step. It is NOT
#: :data:`_run_lock`: that one also serializes :func:`last_runs`, so holding it across
#: a purge would stall a status read for the length of the deletion.
_RETENTION_GATE = threading.Lock()


class _RetentionCountWithdrawn(Exception):
    """The count stopped authorizing the candidate set while the gate was held."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RetentionAuthorizationWithdrawn(Exception):
    """Consent was gone by the time the gate was held, so nothing may be deleted.

    Carries the original refusal, because the audit entry the caller files reports
    WHY authorization failed and a flattened message would lose that.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _delete_under_the_retention_gate(
    account: str,
    keep: int,
    profile: str,
    region: str,
    bucket: str,
    versions: list[tuple[str, str]],
    *,
    caller: str,
) -> int:
    """Re-read the count and erase ``versions`` without letting a write interleave.

    The re-read happens with :data:`_RETENTION_GATE` held and the delete runs before
    it is released, so there is no instant at which the count can change between being
    checked and being acted on. :func:`set_retention_keep` takes the same lock, which
    is what makes the two orderings the only ones possible: either the write lands
    first and this read sees it, or the delete completes and the write applies to the
    next sweep.

    A count that GREW protects keys this candidate set was built to delete, so the set
    is stale and this raises. A count that shrank authorizes every key in the set and
    more, so the set stays valid. Unreadable raises, as the first read does.
    """
    # BOTH locks, because either alone leaves a real hole. `_RETENTION_GATE` orders
    # other THREADS in this process; the state file's sidecar lock orders other
    # PROCESSES, and this module's own docstrings describe a second install writing
    # into the first one's bucket and state as a designed-for case -- so a clear issued
    # over there would otherwise not be ordered against this delete at all. A thread
    # lock cannot see another process, and a file lock alone would not serialize two
    # threads here, since each would open its own descriptor.
    #
    # The cost is deliberate: a state write in ANY process waits for this purge. That
    # is the price of ordering an irreversible remote delete against a local
    # withdrawal, and what waits is one batched delete rather than the whole sweep.
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with (
        _RETENTION_GATE,
        open_lock_file(lock_path) as fd,
        file_lock(fd, exclusive=True, required=True),
    ):
        keep_now, off_now = _retention_keep_for_sweep(account)
        if keep_now is None or keep_now > keep:
            raise _RetentionCountWithdrawn(off_now or "the retention count changed before deletion")
        # Consent is re-checked HERE, inside both locks, for the same reason the count
        # is: acquiring these locks can wait on another purge, and a gate is good for
        # the call that follows it rather than for one on the far side of a wait. This
        # is the LAST thing before the irreversible call.
        #
        # It costs an STS round trip inside the critical section, which is the trade
        # already taken for the delete: a longer wait for other state writers buys a
        # delete that cannot run on authority withdrawn while this waited.
        try:
            _authorize_upload(account, profile, region, caller=caller, operation=SEL_OP_RETENTION)
        except Exception as exc:
            raise _RetentionAuthorizationWithdrawn(exc) from exc
        return storage.delete_object_versions(
            profile, region, bucket, "backup", versions, account=account
        )


def _newest_first(keys: dict[str, list[dict[str, Any]]]) -> list[str]:
    """``keys`` ordered newest archive first, by the same rule the panel sorts by.

    One ordering for the listing and the sweep, because two would eventually
    disagree and the operator would then be shown a row that retention had
    already decided was old. :func:`_archive_sort_key` leads on S3's own
    ``LastModified`` and tie-breaks on the basename, so a key put here by some
    other tool -- carrying no ``_stamp`` and therefore no time in its name --
    still sorts by when the bucket says it arrived.
    """

    def _entry(key: str) -> dict[str, Any]:
        newest = max((str(v.get("modified", "")) for v in keys[key]), default="")
        return {"key": key, "modified": newest}

    return sorted(keys, key=lambda k: _archive_sort_key(_entry(k)), reverse=True)


def _current_version_is_ours(rows: list[dict[str, Any]], recorded: str) -> bool:
    """Whether the version a restore would fetch under this key is ``recorded``.

    `storage.get_file` names no version, so a restore reads the key's CURRENT
    version. That makes "is this a restorable archive of ours" a question about one
    version only, and the answer decides both whether the key may hold a ``keep``
    slot and whether the sweep may run at all.

    False for a key whose current version is a delete marker, and false for one
    whose current version is a co-writer's. In the second case our bytes are still
    on the drive as a noncurrent version, which is exactly why the count-based
    reading of this looked fine: they are present, and they are also unreachable
    through the only restore this product offers.

    Empty ``recorded`` is therefore false as well: with no recorded id nothing can
    be shown to be ours, which is the fail-closed end.

    It is deliberately a question about the CURRENT version rather than the
    newest-by-timestamp one, so a key ordered by `_newest_first` also carries our
    version as its newest -- one rule, not two that can drift.
    """
    if not recorded:
        return False
    current = [row for row in rows if row.get("latest")]
    if not current:
        return False
    row = current[0]
    if row.get("deleteMarker"):
        return False
    return str(row.get("versionId", "")) == recorded


def _audit_retention(
    account: str,
    outcome: dict[str, Any],
    *,
    caller: str,
    result: str,
    error: str = "",
) -> None:
    """Record in the SEL what the sweep DID, not only what it refused.

    :func:`_refuse_upload` covers the gate, so a REFUSED sweep was already on the
    record and a sweep that RAN was not. That asymmetry is the one an auditor
    cannot work around, because this is the only path in the app that erases
    object versions permanently: a purge leaving no event is indistinguishable
    from no purge at all, and so is a purge that failed halfway. Each terminal
    outcome therefore files one :data:`SEL_OP_RETENTION` event carrying the
    counts that say which of them happened.

    ``result`` is ``successful`` when the sweep completed, whether or not it had
    anything to delete, and ``failed`` when it did not -- an AWS error, or the
    refusal to act on a listing that does not show the archive just uploaded.
    Neither is ``denied``: that value belongs to the access decisions
    :func:`_refuse_upload` files, and putting a cloud error in the same bucket as
    a withdrawn consent would devalue every real denial in the log.

    Best-effort and last, for the same reason the sweep itself is: the archive is
    already off-host, so a SEL write that fails must not reach the caller.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation=SEL_OP_RETENTION,
            outcome=result,
            source="aws-control",
            resources=(
                f"account={account} kind={outcome['kind']} keep={outcome['keep']} "
                f"live={outcome['live']} retired={outcome['retired']} "
                f"versions={outcome['versions']} unclaimed={outcome['unclaimed']} "
                f"unclaimedBytes={outcome['unclaimedBytes']}"
            )[:200],
            error=error[:200],
        )
    except Exception:
        logger.debug("aws-control SEL audit failed", exc_info=True)


def _audit_unfiled_authorization(
    account: str,
    outcome: dict[str, Any],
    exc: BaseException,
    *,
    caller: str,
) -> None:
    """File a retention event for an authorization failure nobody else filed.

    :func:`_refuse_upload` files its own ``denied`` event and THEN raises, so a
    refusal is already on the record and filing again here would record one
    decision twice. Every other way :func:`_authorize_upload` can fail -- an
    :class:`AWSError` out of the live ``sts:GetCallerIdentity``, an expired or
    missing credential, a transport error -- never reaches ``_refuse_upload``,
    and leave the gate on the permanent-delete path with no event at all. A
    credential failure is ordinary, so that is the common case this covers.

    The exception TYPE is the discriminator because it is the one thing
    ``_refuse_upload`` guarantees: it ends in ``raise RuntimeError(reason)``.
    ``AWSError`` derives from ``Exception``, not ``RuntimeError``, and a test
    pins that relationship -- if it ever changed, this would silently go back to
    treating a real credential failure as already audited.

    ``failed`` rather than ``denied``, per :func:`_audit_retention`: a cloud error
    is not an access decision, and filing it as a denial would devalue the real
    ones.
    """
    if isinstance(exc, RuntimeError):
        return
    _audit_retention(
        account,
        outcome,
        caller=caller,
        result="failed",
        error=redact_log_via_context(str(exc)),
    )


def _prune_remote_archives(
    account: str,
    profile: str,
    region: str,
    bucket: str,
    kind: str,
    install_id: str,
    newest_key: str,
    *,
    caller: str,
) -> dict[str, Any]:
    """Retire this install's oldest archives of ``kind``, keeping the newest ``keep``.

    Without this the drive only ever grows. Both push paths mint a key carrying
    :func:`_stamp`, so nothing is ever overwritten and a nightly backup adds one
    archive a night forever -- measured on a real drive: 15 snapshot archives,
    10.1 GB, the newest 2.49 GB, oldest three weeks old, on a bucket whose size
    had never once gone down.

    **Deleting the OBJECT would not have helped.** The drive has versioning
    enabled and no lifecycle rule, so ``delete-object`` without a version id
    writes a delete marker and leaves the bytes as a noncurrent version that goes
    on being billed -- a retention pass built that way would empty the listing
    and save nothing. So the sweep is version-aware end to end: it lists versions
    (:func:`storage.list_object_versions`) and deletes them pinned to their
    ``VersionId`` (:func:`storage.delete_object_versions`), which erases the bytes
    and leaves no marker behind.

    **BEST-EFFORT AND LAST.** The upload is the point of the run; retention is
    housekeeping after it. Every failure below is caught and logged as one line,
    because a backup whose archive is safely off-host must never be reported as
    failed over a cleanup that was not -- the only cost of a skipped sweep is a
    bill, and the next successful run collects it.

    **AUDITED EITHER WAY.** Being best-effort is why the SEL entry matters: a
    failure that only logs is a failure nobody reviewing the audit trail can see,
    and this is the one path in the app that erases object versions for good. So
    every terminal outcome files one event through :func:`_audit_retention` --
    including the ones that deleted nothing -- while a refusal by the gate is left
    to :func:`_refuse_upload`, which already recorded it where the decision was
    made.

    Three scoping rules, each made load-bearing by the shape of this bucket:

    * **Per install.** One drive is reachable by several installs BY DESIGN (see
      "install identity"), so the listing and every delete are anchored on THIS
      install's prefix. Another machine's archives are not ours to retire and its
      retention setting is not ours to apply.
    * **Per kind.** ``snapshots/`` and ``sessions/`` are separate histories with
      separate cadences. Counted together, a burst of one kind would evict the
      other kind's only copy.
    * **Never the newest, whatever the count.** The key this run just uploaded is
      dropped from the candidates unconditionally, by name, and that is a SEPARATE
      guarantee from the count rather than a consequence of it. The floor at
      :data:`RETENTION_KEEP_MIN` also happens to spare the first entry of the age
      order, but the two are not the same claim: the run's own key is only first in
      that order while nothing else carries a later timestamp, and a co-writer with
      a skewed clock or a future-dated object is enough to move it. Tying the
      guarantee to the number would make it hold by luck exactly when the ordering
      surprises us, which is the case it exists for.

    And one refusal, which is the cloud form of the guard ``--keep`` already
    carries locally: a bundle that omits data it was asked to carry does not
    prune, so an incomplete backup cannot replace a complete one. Here, if the
    listing does not show the archive this run just uploaded, NOTHING is pruned. A
    view missing the newest object is a view that cannot be trusted about which
    objects are old, and acting on one is how a retention pass deletes the history
    and keeps nothing.

    Returns a record of what it did, for the log line and for tests. Callers
    ignore it: there is no outcome here that should change the run's own result.
    """
    keep, off_reason = _retention_keep_for_sweep(account)
    outcome: dict[str, Any] = {
        "kind": kind,
        # "off" rather than a number when nothing is configured or the state could
        # not be read. Recording a number here would name a count this sweep did not
        # act on, and the absence of one is the whole point.
        "keep": "off" if keep is None else keep,
        "live": 0,
        "retired": 0,
        "versions": 0,
        # Archives this install wrote and can never retire. Zero until the listing
        # is read, and reported even when the sweep deletes nothing, because their
        # whole problem is that no sweep ever collects them.
        "unclaimed": 0,
        "unclaimedBytes": 0,
        "skipped": "",
    }
    # First, and before any cloud call, because neither branch needs one to decline.
    #
    # Retention is off unless this account holds a usable count, so an install nobody
    # configured -- fresh or upgraded -- reaches here and keeps every archive. That
    # is the whole opt-in: no first run after an upgrade deletes anything.
    #
    # The two reasons are audited differently on purpose. Not enabled is the
    # configured behaviour, so the sweep SUCCEEDED at having nothing to do, and
    # filing it as a failure would put the ordinary state of every unconfigured
    # install in the same bucket as a real fault. A state file this process could not
    # read keeps everything too, but it is an anomaly: an operator who configured a
    # count is silently not getting it, so it stays a failure with its reason.
    if keep is None:
        outcome["skipped"] = off_reason
        if off_reason == "retention is not enabled":
            logger.debug(
                "aws-control: %s retention for %s is not enabled, so every archive is "
                "kept; set %s on this account to bound the pile",
                kind,
                account,
                RETENTION_KEEP_STATE_KEY,
            )
            _audit_retention(account, outcome, caller=caller, result="successful")
            return outcome
        logger.warning(
            "aws-control: skipping %s retention for %s: the app's state file could not "
            "be read, so a configured keep count cannot be seen and guessing one could "
            "erase archives the owner asked to keep; the backup itself succeeded and "
            "nothing was deleted",
            kind,
            account,
        )
        _audit_retention(account, outcome, caller=caller, result="failed", error=outcome["skipped"])
        return outcome
    # Re-authorized, like the archive PUT itself. These are DELETES of the
    # owner's data running after a build that may have taken minutes, and
    # consent can be withdrawn, the app disabled or the profile repointed in
    # between -- so the live gate runs again rather than the sweep inheriting a
    # decision made before the push. Its own operation name keeps a refused sweep
    # out of the upload's audit bucket: this run's upload succeeded.
    #
    # It gets its OWN handler so one refusal files one event: `_refuse_upload`
    # records the decision as `denied` at the point it is made, and letting that
    # RuntimeError fall into the sweep's broad handler below would file a second
    # `failed` event for the same decision. A failure that never reaches
    # `_refuse_upload` -- an expired credential, a dead STS call -- files nothing
    # by itself, so `_audit_unfiled_authorization` covers exactly that half.
    try:
        _authorize_upload(account, profile, region, caller=caller, operation=SEL_OP_RETENTION)
    except Exception as exc:
        outcome["skipped"] = "authorization refused"
        # `redact_log_via_context`, not the two bare egress redactors this module
        # uses for object NAMES above: this is a gate-side log line, so a host with
        # a companion policy loaded must not have it scanned with the weaker OSS
        # pass. It never raises, and with no context installed it runs that same
        # OSS pass, so the spelling costs nothing where nothing composes.
        logger.warning(
            "aws-control: %s retention for %s was not authorized; the backup itself "
            "succeeded and nothing was deleted: %s",
            kind,
            account,
            redact_log_via_context(str(exc)),
        )
        _audit_unfiled_authorization(account, outcome, exc, caller=caller)
        return outcome
    try:
        sub = f"{KIND_SUBPATHS[kind]}/{install_id}"
        rows = storage.list_object_versions(profile, region, bucket, "backup", sub, account=account)
        # What this install can PROVE it wrote, not whatever sits under a prefix.
        # The prefix is shared by design, so a co-writer -- another tool pointed at
        # the same drive, or a compromised one -- can put an object under it, and
        # erasing versions cannot be undone. The restore path already draws exactly
        # this line for a far cheaper operation: anything outside this record reads
        # as ORIGIN_UNVERIFIED and is refused without an explicit override. A
        # permanent delete must be at least as strict as a read.
        #
        # `_record_run` writes this run's own key before the sweep is called, and
        # `uploaded_objects` also merges runs whose state write failed, so
        # `newest_key` is in here on both paths.
        ours = uploaded_keys(account)
        our_versions = uploaded_versions(account)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = str(row.get("key", ""))
            # The label sidecar shares the prefix with the archives it labels. It
            # is not an archive: it must not consume a `keep` slot, and it must
            # not be deleted -- another install reads it to render a name instead
            # of hex.
            if not key or _key_basename(key) == LABEL_OBJECT_NAME:
                continue
            # Not ours to retire, and it must not consume a `keep` slot either:
            # `keep` counts what this install keeps of its OWN archives, so letting
            # a foreign object fill a slot would let a co-writer's upload push one
            # of ours over the edge and delete it.
            if key not in ours:
                continue
            by_key.setdefault(key, []).append(row)
        # A key counts as a live archive of OURS only when the version a restore
        # would actually fetch is the version this install wrote. `storage.get_file`
        # takes no version id, so a restore reads whatever is CURRENT under the key:
        # if a co-writer's version is on top, our bytes are still on the drive but
        # unreachable through the product's own restore path, so the key is not a
        # restorable copy and must not hold a `keep` slot.
        #
        # Two ways a key fails that, both left entirely alone. Its current version is
        # a delete marker: the noncurrent bytes are the separate, pre-existing cost
        # of the manual delete path, and erasing them here would silently revoke the
        # recoverability `storage.delete_key` documents. Or its current version is
        # foreign: erasing our unreachable version under it would reclaim bytes, but
        # it would also be this sweep deciding that a key a co-writer is actively
        # writing is finished with, which is not a call retention gets to make.
        live = {
            key: versions
            for key, versions in by_key.items()
            if _current_version_is_ours(versions, our_versions.get(key, ""))
        }
        outcome["live"] = len(live)
        # The keys this install wrote that carry no recorded version: an archive
        # pushed before the record existed, an unversioned bucket, or a put response
        # that named none. `_current_version_is_ours` is false for all of them, so
        # they hold no `keep` slot and no sweep can ever delete them -- their bytes
        # are a permanent cost. Every version under such a key is billed, so the
        # total is over the whole key rather than its current version. Reported so
        # the cost appears in the audit trail rather than only on an invoice.
        unclaimed = [key for key in by_key if not our_versions.get(key, "")]
        outcome["unclaimed"] = len(unclaimed)
        outcome["unclaimedBytes"] = sum(
            int(row.get("size", 0) or 0) for key in unclaimed for row in by_key[key]
        )
        if unclaimed:
            logger.info(
                "aws-control: %s retention for %s under install %s cannot own %d archive(s) "
                "holding %d byte(s): no version id was recorded for them, so no sweep will "
                "ever reclaim them",
                kind,
                account,
                install_id,
                outcome["unclaimed"],
                outcome["unclaimedBytes"],
            )
        if newest_key not in live:
            # Two different faults, and an auditor needs to tell them apart: a
            # listing that omits the upload cannot be trusted about age at all,
            # while one that shows the key under a foreign current version says the
            # archive this run just wrote is already not the restorable copy.
            if newest_key in by_key:
                outcome["skipped"] = (
                    "the archive this run uploaded is not the current version of its key"
                )
            else:
                outcome["skipped"] = "the listing does not show the archive this run uploaded"
            logger.warning(
                "aws-control: skipping %s retention for %s under install %s: %s, so the "
                "listing cannot be trusted about which archives are old; nothing was deleted",
                kind,
                account,
                install_id,
                outcome["skipped"],
            )
            _audit_retention(
                account, outcome, caller=caller, result="failed", error=outcome["skipped"]
            )
            return outcome
        by_age = _newest_first(live)
        candidates = [key for key in by_age[keep:] if key != newest_key]
        # `ours` proved the KEY. This proves the VERSION, which is what the delete
        # below actually erases: a key can carry a version this install did not
        # write, and the recorded id is the only thing that says which one is ours.
        #
        # One way a candidate drops out here, and it is left entirely alone:
        # recorded but absent from the listing, meaning our version is already gone,
        # so whatever remains under that key is not ours to erase -- exactly the
        # case a version COUNT read as ownership and got wrong. The membership test
        # beside it re-asserts an invariant rather than filtering a second case:
        # every candidate came from `live`, and `_current_version_is_ours` is false
        # without a recorded id, so a key with none is counted as unclaimed above
        # and never reaches this point.
        listed = {key: {str(row.get("versionId", "")) for row in by_key[key]} for key in candidates}
        versions = [
            (key, our_versions[key])
            for key in candidates
            if key in our_versions and our_versions[key] in listed[key]
        ]
        retire = [key for key, _ in versions]
        if not retire:
            logger.info(
                "aws-control: %s retention for %s kept all %d archive(s) under install %s "
                "(keep=%d)",
                kind,
                account,
                len(live),
                install_id,
                keep,
            )
            _audit_retention(account, outcome, caller=caller, result="successful")
            return outcome
        # The SECOND gate, immediately before the only irreversible call in this
        # function. The one at the top is separated from here by a
        # `list_object_versions` round trip, and consent withdrawn inside that
        # window would otherwise permanently erase versions nobody is authorized
        # to touch any more. `_publish_label` holds the same rule for its own
        # write: a gate is good for the call that FOLLOWS it, not for a later one.
        #
        # Its own handler, for the reason the first gate has one, and it returns
        # instead of falling through: nothing has been deleted at this point, so
        # this is a refusal and not a half-finished purge.
        # The COUNT gets the same treatment as the consent gate below, and for the
        # same reason: it was read once before a `list_object_versions` round trip
        # that takes as long as the network takes, and an owner who switched
        # retention off inside that window has chosen to keep these versions. A
        # count read before the listing is good for the listing, not for a delete
        # that happens after it.
        #
        # Re-reading here rather than holding `_run_lock` across the deletion is
        # deliberate: that lock also serializes `last_runs`, so holding it through
        # network calls would stall a status read for the length of a purge.
        #
        try:
            removed = _delete_under_the_retention_gate(
                account, keep, profile, region, bucket, versions, caller=caller
            )
        except _RetentionAuthorizationWithdrawn as withdrawn:
            cause = withdrawn.cause
            outcome["skipped"] = "authorization withdrawn before deletion"
            logger.warning(
                "aws-control: %s retention for %s was authorized before the listing but "
                "no longer at the moment of deletion; the backup itself succeeded and "
                "nothing was deleted: %s",
                kind,
                account,
                redact_log_via_context(str(cause)),
            )
            _audit_unfiled_authorization(account, outcome, cause, caller=caller)
            return outcome
        except _RetentionCountWithdrawn as exc:
            # Nothing has been deleted: the gate checked the count and refused before
            # calling S3, so this is a refusal and not a half-finished purge.
            outcome["skipped"] = exc.reason
            logger.warning(
                "aws-control: %s retention for %s was set to keep %s when the listing "
                "began and no longer authorized that set at the moment of deletion; the "
                "backup itself succeeded and nothing was deleted: %s",
                kind,
                account,
                keep,
                exc.reason,
            )
            # An owner who changed their mind is not a failure. An unreadable setting
            # is, which is the same split the first read files.
            if exc.reason == "the retention setting could not be read":
                _audit_retention(account, outcome, caller=caller, result="failed", error=exc.reason)
            else:
                _audit_retention(account, outcome, caller=caller, result="successful")
            return outcome
        except storage.PartialVersionDelete as exc:
            # Those bytes are already gone, so the count has to survive into the
            # audit the broad handler below files -- otherwise a purge that failed
            # halfway is recorded as having erased nothing.
            #
            # `retired` stays at 0 on purpose: batches are filled to the API's
            # limit without regard to key boundaries, so the erased versions do
            # not map to a number of retired KEYS, and a figure nothing measured
            # is worse in an audit record than an absent one.
            outcome["versions"] = exc.removed
            raise
        outcome["retired"] = len(retire)
        outcome["versions"] = removed
        logger.info(
            "aws-control: %s retention for %s kept %d of %d archive(s) under install %s "
            "(keep=%d), erasing %d object version(s)",
            kind,
            account,
            len(live) - len(retire),
            len(live),
            install_id,
            keep,
            removed,
        )
        _audit_retention(account, outcome, caller=caller, result="successful")
        return outcome
    except Exception as exc:
        # Deliberately broad, and it is the whole point of this function's
        # contract: the archive is already off-host and the run has already been
        # recorded, so nothing this sweep can fail at is worth converting a
        # successful backup into a failed one. One line, with the reason, and the
        # next run tries again.
        outcome["skipped"] = "cleanup failed"
        # Gate-side, so the same context-aware log spelling as the refusal above.
        # One redaction feeds both the log line and the audit event's `error`: a
        # composed companion's regexes apply to both, and in the one state that
        # withholds the text the audit entry still fires carrying the placeholder,
        # which is the shape an auditor can act on.
        reason = redact_log_via_context(str(exc))
        # Two spellings, because one of them would be false. A failure AFTER the
        # listing may already have erased versions permanently, and the audit entry
        # below carries that count deliberately -- so a log line next to it claiming
        # nothing was deleted contradicts the record an auditor reads beside it, and
        # points them at a purge that did not happen. Only the version count is named:
        # batches are filled to the API's limit without regard to key boundaries, so
        # `retired` stays 0 on that path and a number of KEYS is not something this
        # failure measured.
        if outcome["versions"]:
            logger.warning(
                "aws-control: %s retention for %s failed after erasing %d object "
                "version(s); those bytes are gone and cannot be recovered, and the "
                "backup itself succeeded: %s",
                kind,
                account,
                outcome["versions"],
                reason,
            )
        else:
            logger.warning(
                "aws-control: %s retention for %s could not run; the backup itself "
                "succeeded and nothing was deleted: %s",
                kind,
                account,
                reason,
            )
        # A failure AFTER the listing may already have erased some versions, so
        # `outcome` carries whatever the sweep got through -- an entry saying
        # nothing happened would be the one shape an auditor must not be handed.
        _audit_retention(account, outcome, caller=caller, result="failed", error=reason)
        return outcome


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
                _authorize_upload(account, profile, region, caller=caller)
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
        _authorize_upload(account, profile, region, caller=caller)
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


#: ``O_NOFOLLOW`` refuses to open a symlink at all, which is what makes the
#: descriptor-pinned add below race-free rather than merely check-then-open. It
#: does not exist on Windows, where the fallback is the ``S_ISREG`` fstat plus the
#: directory pruning: a swap is still caught the moment the descriptor is
#: inspected, it just cannot be refused at open time.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: ``O_NONBLOCK`` is what keeps the open itself from being a denial of service.
#: Opening a FIFO for reading BLOCKS until some writer appears, so a single named
#: pipe planted in an agent-writable session directory would hang the backup
#: thread forever -- the fstat that rejects it never gets to run. With this flag
#: the open returns immediately and ``S_ISREG`` does the rejecting. Regular files
#: ignore it, so nothing legitimate changes. Also absent on Windows, which has no
#: FIFOs to open.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


#: ``O_DIRECTORY`` makes "open this only if it is a directory" atomic with the
#: open, so a pinned descent cannot be tricked into opening a file (or, with
#: ``O_NOFOLLOW`` alongside it, a link) where a directory was expected. Absent on
#: Windows, which is one of the two reasons the fallback walk exists.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

#: Depth ceiling for the pinned descent. One descriptor is held per level, so a
#: pathological tree could otherwise exhaust the process's fd budget. Session
#: trees are two or three deep; anything past this is not a session layout.
_MAX_TREE_DEPTH = 32

#: Whether this platform can do the pinned traversal at all. Both are needed:
#: ``dir_fd`` for ``os.open`` (the ``openat`` syscall) and an fd-accepting
#: ``os.scandir``. POSIX has both; Windows has neither.
_CAN_PIN_TRAVERSAL = (
    os.open in getattr(os, "supports_dir_fd", set())
    and os.scandir in getattr(os, "supports_fd", set())
    and _O_DIRECTORY != 0
)

#: Why the sessions backup refuses rather than degrading to a name-based walk.
#: Phrased for a human reading a failed run record, so it says what is missing and
#: that the refusal is the safe outcome rather than a bug to work around.
_NO_PINNING_REASON = (
    "sessions backup needs descriptor-pinned directory traversal (openat), which "
    "this platform does not provide. Walking these agent-writable directories by "
    "name would leave a window in which a directory swapped for a link could be "
    "archived and uploaded, so the backup is refused instead."
)


def _add_pinned(tar: tarfile.TarFile, dir_fd: int, arc_prefix: str, depth: int) -> int:
    """Archive one directory level, addressing every child RELATIVE to ``dir_fd``.

    This is what closes the ancestor-swap window that a path-based walk cannot.
    ``os.walk`` yields NAMES, and re-opening ``a/b/c.json`` re-resolves ``a`` and
    ``b`` from scratch: swapping either for a link between the check and the open
    redirects the read, and no amount of pre-checking the name helps because the
    check and the open are two separate resolutions of the same string.

    Here each level is held open as a descriptor and every child is opened with
    ``dir_fd=`` -- the kernel resolves the child against THAT descriptor, not
    against a path, so an ancestor renamed or relinked afterwards cannot change
    what is read. Combined with ``O_NOFOLLOW`` (the child itself may not be a
    link) and ``O_DIRECTORY`` (a directory child must really be a directory),
    the traversal never leaves the tree it was handed.
    """
    added = 0
    if depth > _MAX_TREE_DEPTH:
        logger.warning("aws-control backup: tree deeper than %s levels; pruned", _MAX_TREE_DEPTH)
        return added
    try:
        with os.scandir(dir_fd) as it:
            names = sorted(entry.name for entry in it)
    except OSError:
        return added
    for name in names:
        try:
            child = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dir_fd)
        except OSError:
            # ELOOP (a link), ENOENT (gone mid-scan), EACCES, ENXIO (a FIFO with
            # no writer): not ours to archive, never a hard failure.
            continue
        try:
            st = os.fstat(child)
            if stat.S_ISDIR(st.st_mode):
                added += _add_pinned(tar, child, f"{arc_prefix}/{name}", depth + 1)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_nlink != 1:
                # A HARD link defeats every other defense here by construction:
                # it is a regular file (S_ISREG passes), it is not a symlink so
                # O_NOFOLLOW does not reject it, it carries no reparse point, and
                # it is opened relative to the pinned descriptor like any real
                # session file -- while pointing at another file's inode. So
                # `os.link("~/.aws/credentials", "<session dir>/notes.json")` in
                # an agent-writable directory would archive the credential bytes
                # and upload them. The link COUNT is what tells the two apart, and
                # it is read from the fstat of the descriptor being archived, so it
                # describes the inode actually about to be read. A genuine session
                # file has exactly one link; anything else is not ours to send.
                continue
            info = tarfile.TarInfo(name=f"{arc_prefix}/{name}")
            info.size = st.st_size
            info.mtime = int(st.st_mtime)
            info.mode = stat.S_IMODE(st.st_mode)
            info.type = tarfile.REGTYPE
            with os.fdopen(child, "rb", closefd=False) as fh:
                tar.addfile(info, fh)
            added += 1
        finally:
            os.close(child)
    return added


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


def run_sessions_backup(
    account: str, profile: str, region: str, bucket: str, *, caller: str
) -> dict[str, Any]:
    """Tar both session halves and push. Returns the run record.

    Refuses outright on a platform that cannot pin the traversal to descriptors.
    The session directories are agent-writable and this archive is uploaded
    unattended, so a name-based walk would trade an unrecoverable outcome
    (credentials reached by a junction swapped in after the check) for a
    convenience. See :func:`_add_tree`.
    """
    if not _CAN_PIN_TRAVERSAL:
        raise RuntimeError(_NO_PINNING_REASON)
    identity = install_identity()
    crew_sessions = data_home() / SESSIONS_DIR_NAME
    cli_sessions = kiro_sessions_dir()
    with tempfile.TemporaryDirectory(prefix="kc-backup-") as tmp:
        archive = Path(tmp) / f"sessions-{_stamp()}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            count = _add_tree(tar, crew_sessions, "crew")
            count += _add_tree(tar, cli_sessions, "cli")
        if count == 0:
            raise RuntimeError("no session files to archive")
        key = f"{KIND_SUBPATHS[KIND_SESSIONS]}/{identity['id']}/{archive.name}"
        # The gate sits IMMEDIATELY before the archive PUT with nothing in
        # between -- no other network call, no second upload -- so the decision
        # that authorizes these bytes cannot go stale before they leave. The
        # label's own PUT takes its own authorization inside `_publish_label`,
        # which is why it can safely run afterwards.
        _authorize_upload(account, profile, region, caller=caller)
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
        )
        # After the archive and after the ledger write, and with its own
        # authorization: a caption must never delay or endanger the payload.
        _publish_label(account, profile, region, bucket, identity, caller=caller)
        # LAST, and after a push that succeeded. This is the only step here that
        # deletes, so it runs once everything proving this run worked is already
        # done -- and it cannot fail the run. See _prune_remote_archives.
        _prune_remote_archives(
            account, profile, region, bucket, KIND_SESSIONS, identity["id"], key, caller=caller
        )
        return record


#: The two Job SDK kinds this app registers. Same strings as ``KIND_*`` so a run
#: record read by a human names the backup the owner asked for.
JOB_KINDS = (KIND_SNAPSHOT, KIND_SESSIONS)


def kind_unavailable_reason(kind: str) -> str | None:
    """Why ``kind`` cannot run on THIS platform, or ``None`` when it can.

    The refusal itself is not new -- :func:`run_sessions_backup` has always
    raised on a platform without descriptor-pinned traversal, and that fail-close
    is correct and stays. What was missing is a way to ASK before starting: the
    kind was registered and offered identically everywhere, so on Windows the
    owner pressed a button and got a ``RuntimeError`` back as a failed run
    record. A capability question deserves an answer before the work, not an
    exception after it, so the same condition is readable up front here and the
    route layer turns it into a stated refusal.

    Returns the prose reason so every surface quotes ONE explanation. Callers
    must treat a non-``None`` result as "offer this as unavailable", not as an
    error to log.
    """
    if kind == KIND_SESSIONS and not _CAN_PIN_TRAVERSAL:
        return _NO_PINNING_REASON
    return None


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
        _authorize_upload(account, profile, region, caller=CALLER_OWNER)
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


def _install_folders(
    profile: str, region: str, bucket: str, kind: str, *, account: str
) -> set[str]:
    """Every install id with a prefix under ``kind`` — COMPLETE, unredacted.

    Deliberately not :func:`storage.list_section`, whose page is capped and whose
    names are run through the egress redactors. Both are right for a listing that
    is about to be displayed and wrong for the ONE caller that reasons about
    ABSENCE: the nightly loop asks "is another install writing here" and answers
    "no" by finding nothing, and nothing-on-the-first-page is not nothing. The
    ``--query`` projection with no ``--max-items`` lets the CLI auto-paginate and
    apply the projection to the merged result, the same property
    ``storage.list_library_folders`` relies on, so the answer is the complete set
    or a raised error.

    The prefix is built from :data:`storage.SECTION_PREFIXES`, not passed in, which
    keeps the rule that a raw S3 prefix never comes from a caller.
    """
    prefix = storage.SECTION_PREFIXES["backup"] + f"{KIND_SUBPATHS[kind]}/"
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
            "the backup folder listing returned a response that could not be read as "
            "JSON; refusing to report the prefix as unshared"
        ) from None
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, str) or not row.startswith(prefix):
            continue
        name = row[len(prefix) :].rstrip("/")
        if _INSTALL_ID_RE.match(name):
            found.add(name)
    return found


def other_install_ids(
    profile: str,
    region: str,
    bucket: str,
    *,
    account: str,
    kind: str = KIND_SNAPSHOT,
) -> list[str]:
    """Install ids OTHER than this one that have written to this drive.

    The nightly loop's evidence that the drive is shared. Reads the key namespace
    and nothing a writer authors, so it cannot be talked out of the answer.

    Scoped to ONE kind, defaulting to the snapshot prefix, and its only caller is
    the nightly loop -- which uploads snapshots and nothing else. Sweeping both
    prefixes cost two paid LIST calls per scheduled run to answer a question one
    call answers for the run actually happening.

    This is a cost tradeoff, not a complete census, and it is worth being exact
    about what it gives up: the two manual runners write one kind each, so an
    install whose only backup was an on-demand SESSIONS upload has no folder under
    ``snapshots/`` and this read does not see it. The notice is therefore a
    best-effort signal about a shared drive rather than proof of who else writes
    here. :func:`list_remote_backups` is the complete view, and it is the one a
    reader consults before restoring.
    """
    mine = install_identity()["id"]
    seen = _install_folders(profile, region, bucket, kind, account=account)
    return sorted(seen - {mine})


def read_remote_label(
    profile: str, region: str, bucket: str, kind: str, install_id: str, *, account: str
) -> str:
    """The label another install published for itself, sanitised for display.

    LAZY on purpose: called only for an install whose rows are about to be shown,
    so an ordinary listing pays nothing and an expanded one pays one bounded GET
    per foreign install.

    The transfer is RANGE-bounded through :func:`storage.get_object_head_bytes`
    rather than a plain download. The object is written by another install, so its
    size is that install's choice; a full ``get-object`` of a file named
    ``_label.json`` would let a multi-gigabyte object be pulled onto this disk, on
    the owner's transfer bill, to render one caption. Two kilobytes is far more
    than a label needs and is the whole exposure.

    Every failure answers the empty string, which the caller renders as the id.
    A caption that could not be read must not break the listing that would have
    told the operator whose archives these are.
    """
    key = f"{KIND_SUBPATHS[kind]}/{install_id}/{LABEL_OBJECT_NAME}"
    try:
        raw, _size = storage.get_object_head_bytes(
            profile, region, bucket, "backup", key, account=account, max_bytes=2048
        )
        doc = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception:
        logger.debug("aws-control: no readable backup label at %s", key, exc_info=True)
        return ""
    if not isinstance(doc, dict):
        return ""
    return sanitize_label(doc.get("label"))


def _archive_row(entry: dict[str, Any], install_id: str, uploaded: set[str]) -> dict[str, Any]:
    """One listing row, with its origin decided from the key plus what we uploaded."""
    origin, owner = classify_key(str(entry.get("key", "")), install_id, uploaded)
    return {**entry, "install": owner, "origin": origin}


def _archive_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    """Newest first, ACROSS installs.

    Sorting by the whole key would sort by install id first, so two machines'
    archives would render as two blocks and the newest overall would not be at the
    top -- which is how an operator picks the wrong one.

    ``modified`` leads because it is S3's own answer about the object rather than
    an inference from its name. The basename is the tie-break and not the primary
    key: it happens to order archives by time today, since every name this app
    writes begins with the same ``%Y%m%dT%H%M%SZ`` stamp, but an object put under
    these prefixes by any other tool carries no such promise, and one differently
    named file would then sort the whole list wrong. A missing or non-string
    timestamp degrades to the name rather than raising.
    """
    modified = entry.get("modified")
    return (modified if isinstance(modified, str) else "", _key_basename(str(entry.get("key", ""))))


def list_remote_backups(
    profile: str,
    region: str,
    bucket: str,
    *,
    account: str,
    include_others: bool = False,
) -> dict[str, Any]:
    """Remote backup listings for both kinds, attributed to the installs that wrote them.

    Three listing calls per kind in the default view. The nested key shape is what
    buys the first one two answers at once: a '/'-delimited list of ``snapshots/``
    returns every install id present as a FOLDER and every pre-namespace archive as
    a FILE, so "who else writes here" and "what is unattributed" arrive together.
    The second is :func:`_install_folders`, which re-reads the ids unredacted -- the
    display page above is capped and passes names through the egress redactors, so a
    hex id could in principle come back rewritten and a co-tenant would then read as
    absent. The third reads this install's own prefix.

    ``include_others`` enumerates the other installs' prefixes too, capped at
    :data:`MAX_OTHER_INSTALLS`. It is opt-in because it costs a list call per
    install per kind plus a label read, and it EXISTS because a replacement machine
    has no archives of its own: every archive in the bucket is foreign there, so a
    view that only ever showed this install's own prefix would show a fresh install
    nothing at all -- on the one occasion the backup is the only copy left.
    """
    identity = install_identity()
    mine = identity["id"]
    mine_uploads = uploaded_keys(account)
    result: dict[str, Any] = {}
    other_ids: set[str] = set()

    for kind, sub in KIND_SUBPATHS.items():
        # Call 1: folders name the installs, files are the legacy flat archives.
        page = storage.list_section(profile, region, bucket, "backup", sub, account=account)
        rows = [_archive_row(f, mine, mine_uploads) for f in page["files"]]
        # The install ids come from `_install_folders`, not from this page's
        # `folders`. One implementation of "which install ids have prefixes here"
        # rather than two, and it is the COMPLETE, unredacted one: `list_section`
        # is a display read whose page is capped and whose names pass through the
        # egress redactors, so a hex id could in principle come back rewritten and
        # a co-tenant would then read as absent.
        other_ids |= _install_folders(profile, region, bucket, kind, account=account) - {mine}
        # Call 2: this install's own archives.
        prefixes = [mine]
        if include_others:
            prefixes += sorted(other_ids)[:MAX_OTHER_INSTALLS]
        for install_id in prefixes:
            owned = storage.list_section(
                profile, region, bucket, "backup", f"{sub}/{install_id}", account=account
            )
            rows += [
                _archive_row(f, mine, mine_uploads)
                for f in owned["files"]
                # The label sidecar shares the prefix with the archives it labels.
                # It is not an archive and must never be offered for restore.
                if _key_basename(str(f.get("key", ""))) != LABEL_OBJECT_NAME
            ]
        rows.sort(key=_archive_sort_key, reverse=True)
        result[kind] = rows[:20]

    # Labels, for the installs whose rows are actually on screen. This install's
    # own comes from local state; a foreign one is fetched, once, and only when its
    # archives are being listed.
    installs: list[dict[str, Any]] = [
        {"id": mine, "label": identity["label"], "origin": ORIGIN_SELF}
    ]
    shown = sorted(other_ids)[:MAX_OTHER_INSTALLS]
    for install_id in shown:
        label = ""
        if include_others:
            for kind in KIND_SUBPATHS:
                label = read_remote_label(
                    profile, region, bucket, kind, install_id, account=account
                )
                if label:
                    break
        installs.append({"id": install_id, "label": label, "origin": ORIGIN_OTHER})
    result["installs"] = installs
    result["others"] = len(other_ids)
    result["truncated"] = len(other_ids) > MAX_OTHER_INSTALLS
    # The cap travels with the answer rather than being inferred from the roster
    # length. `installs` carries THIS install as its first entry, so a reader
    # deriving the cap from its length is off by one on the very message that
    # exists to state the cap -- and it would be off silently.
    result["max"] = MAX_OTHER_INSTALLS
    return result


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
            if _body_fingerprint(tmp) != recorded.get(key, ""):
                origin = ORIGIN_UNVERIFIED
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
    return {"path": str(dest), "bytes": size, "origin": origin, "install": owner}


def _account_view_checked(account: str) -> tuple[dict[str, Any], bool]:
    """:func:`_account_view` plus whether the state was actually readable.

    A non-dict level in a corrupted file is itself a read failure, not a missing
    key, so it reports False rather than an empty view that looks configured-as-
    default. Only :func:`_retention_keep_for_sweep` consults the flag; everything
    else goes through :func:`_account_view` and keeps its defaults.
    """
    state, readable = _read_state_checked()
    accounts = state.get("accounts", {})
    if not isinstance(accounts, dict):
        return {}, False
    entry = accounts.get(account, {})
    if not isinstance(entry, dict):
        return {}, False
    return entry, readable


def _account_view(account: str) -> dict[str, Any]:
    """Shape-safe read of one account's sub-dict: any non-dict level in a
    corrupted state file reads as empty instead of raising on ``.get``."""
    return _account_view_checked(account)[0]


def nightly_enabled(account: str) -> bool:
    """Whether the owner has authorized unattended uploads for this account.

    Reads through :func:`read_state`, so an unreadable state file answers False.
    That is FAIL-CLOSED, and it is the opposite of what :func:`last_runs` does
    with a run it could not persist -- the asymmetry is deliberate, because the
    two answers cost different things when they are wrong.

    This bit AUTHORIZES spending the owner's money without them present. Read it
    optimistically and a corrupt or unreadable file becomes a reason to start
    uploading; refuse, and a transient failure costs one skipped nightly window
    that the next wake picks up. The run record is the mirror image: it is a
    record of something that ALREADY happened and is already paid for, so
    dropping it does not prevent a charge, it causes one.
    """
    return bool(_account_view(account).get("nightly"))


def set_nightly(account: str, enabled: bool) -> None:
    def mutate(state: dict[str, Any]) -> None:
        _account_state(state, account)["nightly"] = bool(enabled)

    _locked_state_update(mutate)


def set_retention_keep(account: str, count: int | None) -> None:
    """Write this account's retention count, or clear it to turn retention off.

    ``None`` REMOVES the key rather than storing a sentinel, so "off" has exactly one
    representation: the state an install has before anyone configures anything. A
    second spelling of off would be a second thing
    :func:`_retention_keep_for_sweep` has to agree about.

    Rejects a ``bool`` rather than coercing it, which is the same screen
    :func:`_clamp_retention_keep` applies on the way out. ``True`` IS an ``int`` in
    Python, so coercing here would let a stringly-typed caller store ``keep=1`` --
    the most destructive value available -- while believing it had sent a flag.

    Out-of-range is REFUSED rather than clamped, and there is only one end to be out of:
    below the floor. A count above any particular number is not refused, because keeping
    more archives than exist is not a harm. Clamping a below-floor value up here would
    store a number the caller did not ask for.

    Raises ``ValueError`` on anything it will not store, and propagates ``OSError``
    from the state write rather than reporting a count the next read contradicts.
    """
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise ValueError("retention count must be an int or None")
    if count is not None and count < RETENTION_KEEP_MIN:
        raise ValueError(f"retention count must be at least {RETENTION_KEEP_MIN}")

    def mutate(state: dict[str, Any]) -> None:
        entry = _account_state(state, account)
        if count is None:
            entry.pop(RETENTION_KEEP_STATE_KEY, None)
        else:
            entry[RETENTION_KEEP_STATE_KEY] = count

    # The same gate the sweep's final check holds. Without it here the lock there
    # protects nothing: this write is the one it exists to be ordered against. A
    # caller may wait for a purge already authorized to finish, which is the point --
    # it makes the two orderings the only ones possible, rather than leaving a window
    # where this write lands after the count was checked and before S3 was called.
    with _RETENTION_GATE:
        _locked_state_update(mutate)


def retention_keep(account: str) -> int | None:
    """This account's configured count, or ``None`` when retention is off.

    Reported by the backup status read so a client can see what it would act on. NO
    console renderer ships with this: the setting is reachable over HTTP only, and the
    read exists so an operator who sets a count can confirm what was stored rather than
    having to trust the write. Deliberately the same
    resolution the sweep uses, so the number returned is the number that would be acted
    on rather than the raw stored value -- reporting a count the sweep would clamp or
    ignore is worse than reporting nothing.
    """
    return _retention_keep_for_sweep(account)[0]


def last_runs(account: str) -> dict[str, Any]:
    """The last run per kind, including runs this process could not persist.

    The merge is not cosmetic. A run whose state write failed really did upload,
    and :func:`due_for_nightly` reads its answer from here -- so without the
    overlay the nightly loop treats the account as never backed up and uploads
    again on every wake. See :data:`_unpersisted_runs`.
    """
    with _run_lock:
        runs = _account_view(account).get("runs", {})
        runs = dict(runs) if isinstance(runs, dict) else {}
        return _merge_unpersisted(account, runs)


def due_for_nightly(account: str, now: Optional[dt.datetime] = None) -> bool:
    """True when the nightly snapshot has not run in the last ~23 hours."""
    if not nightly_enabled(account):
        return False
    runs = last_runs(account).get(KIND_SNAPSHOT)
    if not runs:
        return True
    try:
        last = dt.datetime.fromisoformat(runs["at"])
    except (KeyError, ValueError, TypeError):
        # TypeError: a corrupted state file carrying a non-string (list/number).
        # Anything unusable reads as "due" -- an unparseable stamp must not be
        # the reason a backup the owner enabled silently stops running.
        return True
    if last.tzinfo is None:
        # A timezone-less stamp parses FINE, so it escapes the try above and
        # would raise TypeError on the aware subtraction below -- outside the
        # guard, in the nightly loop, every wake. costs.is_fresh and
        # shares._prune already normalize this; this site was the one left out.
        last = last.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - last).total_seconds() > 23 * 3600

"""Install identity, and the archive key namespace that carries it.

The id decides what is permitted, and the label decides only what a human reads;
the "install identity" note below explains the split. The module also owns the key
grammar every other part parses (:data:`KIND_SUBPATHS`, :data:`KEY_SEP`, the stamped
name :func:`_stamp` mints) and :func:`classify_key`, the one reader of a key's
origin.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import threading
import uuid
from typing import Any, Optional

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import sanitize_label
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (
    _locked_state_update,
    read_state,
)

logger = logging.getLogger(_FACADE_MODULE)


KIND_SNAPSHOT = "snapshot"


KIND_SESSIONS = "sessions"


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


#: Subpath per backup kind. One place, because three call sites (upload, listing,
#: key classification) have to agree on it or the namespace splits.
KIND_SUBPATHS: dict[str, str] = {KIND_SNAPSHOT: "snapshots", KIND_SESSIONS: "sessions"}


#: The reverse of :data:`KIND_SUBPATHS`, for attributing a recorded key back to
#: the kind that wrote it. DERIVED rather than written out a second time, so a
#: kind added to the table above cannot be missing from this one -- a missing
#: entry would not raise, it would silently leave that kind's archives
#: uncounted.
_KIND_BY_SUBPATH: dict[str, str] = {sub: kind for kind, sub in KIND_SUBPATHS.items()}


#: An id for a process that could not persist one. See :func:`install_identity`.
_fallback_identity: dict[str, str] = {}


_fallback_lock = threading.Lock()


#: The separator inside an S3 OBJECT KEY, which is not a filesystem separator.
#: S3 keys are ``/``-delimited by the S3 API on every platform: a key written on
#: Linux is read back as the same string on Windows, and ``os.sep`` must never
#: appear in one. Every key the backup engine parses goes through :data:`KEY_SEP` and the
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


def _stamp() -> str:
    """A second-resolution timestamp plus entropy.

    A manual run racing the nightly loop can land in the same second; on a
    versioned bucket an identical key does not destroy the earlier archive,
    but it hides it — listings show only the current version, and a restore
    starts there and will only look past it for the one version this install
    recorded. The hex suffix keeps every archive its own key.
    """
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"

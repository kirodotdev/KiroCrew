"""The staged restore: one archive downloaded into this app's staging directory.

**Restore is a download, deliberately.** A restore lands the archive in
``<app data dir>/restore/`` and hands back the path; nothing hot-swaps a
live ``memory.db`` or sessions dir under a running gateway. The snapshot
engine's own merge/replace tooling (or a stopped gateway) takes it from
there, and the UI copy says exactly that.

:func:`restore_download` refuses an archive whose key it cannot prove is this install's
own unless the caller overrides it, names the staged file from the whole key, and
never writes through the final name; :func:`_recover_recorded_version` reads the
version this install recorded when the current object fails its fingerprint.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.fingerprints import (
    _body_fingerprint,
    _is_provable_version_id,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    ORIGIN_SELF,
    ORIGIN_UNVERIFIED,
    UnprovenArchive,
    _key_basename,
    classify_key,
    install_identity,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import (
    uploaded_objects,
    uploaded_versions,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import APP_NAME
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.uploads import (
    _authorize_recovery_read,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.deploy.engine import AWSError
from kiro_crew.platform_compat import is_link_or_junction

logger = logging.getLogger(_FACADE_MODULE)


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
    Whether they still open as a ``tar.gz`` is a different question, and one the
    engine answers the same way everywhere: the current-version read accepts on the
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
    # True only when a co-writer had overwritten the key and the bytes handed back
    # are this install's recorded version. The reply carries it because the
    # overwrite outlives this restore: the next upload to the key can be
    # overwritten the same way, and the gateway log is not where an operator looks.
    recovered_version = False
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
                # instead of the unverified one it accepted. The override keeps its
                # meaning; only this outcome changes.
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
                    recovered_version = True
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
        "recovered": recovered_version,
    }

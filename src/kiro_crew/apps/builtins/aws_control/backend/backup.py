"""Backup — memory/workspace snapshots and session archives on ``backup/``.

Two backup kinds, one push path:

* **Snapshot** (the mockup's "Memory & workspace" row): the existing
  ``kiro_crew.snapshot`` engine builds its portable ``.tar.gz`` (memory,
  crons, config, skills, workspace, notifications, security — its component
  set, unchanged), and the archive is pushed to
  ``backup/snapshots/<name>.tar.gz``.
* **Sessions archive** (the "Sessions archive" row): one tarball of BOTH
  session halves — ``<data home>/sessions/`` (transcripts + rotated
  archives) and ``<kiro home>/sessions/cli/`` (the CLI replay logs) — pushed
  to ``backup/sessions/<stamp>.tar.gz``. Whole-set, not per-session: the
  "both halves move together" invariant is honoured by construction, and the
  per-session incremental integration with the storage inventory is future
  work.

**Restore is a download, deliberately.** A restore lands the archive in
``<app data dir>/restore/`` and hands back the path; nothing hot-swaps a
live ``memory.db`` or sessions dir under a running gateway. The snapshot
engine's own merge/replace tooling (or a stopped gateway) takes it from
there, and the UI copy says exactly that.

State (`<app data dir>/backup.json`): last run per kind + the nightly
toggle. The nightly loop lives in the app's ``on_startup`` hook.

CALLER CONTRACT: handlers hold the consent gate; sync, subprocess/tar-bound
— call via ``asyncio.to_thread`` (pushes of a large sessions set can run
minutes; handlers use generous timeouts).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import stat
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from kiro_crew import snapshot
from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.platform_compat import file_lock, is_link_or_junction
from kiro_crew.snapshot import snapshot_main

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
KIND_SNAPSHOT = "snapshot"
KIND_SESSIONS = "sessions"
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


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=1))


def _locked_state_update(mutate) -> Any:
    """Read-modify-write the state file under the sidecar lock.

    Two backup kinds can finish concurrently (a manual run racing the
    nightly loop); an unlocked read-modify-write would let the later atomic
    write silently discard the earlier run record. Same sidecar-lock shape
    as the share ledger.
    """
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            state = read_state()
            result = mutate(state)
            write_state(state)
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


def _record_run(account: str, kind: str, key: str, size: int) -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        entry = _account_state(state, account)
        runs = entry.setdefault("runs", {})
        if not isinstance(runs, dict):
            # A corrupted non-dict `runs` must not crash AFTER the archive
            # already uploaded (500 + no ledger entry + duplicate on retry).
            runs = entry["runs"] = {}
        runs[kind] = {
            "key": key,
            "bytes": size,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        return runs[kind]

    return _locked_state_update(mutate)


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


def _authorize_upload(account: str, profile: str, region: str) -> None:
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
        raise RuntimeError(
            "this connection no longer points at the requested account; upload refused"
        )
    if not is_app_enabled("aws-control"):
        raise RuntimeError("aws-control was disabled during the backup build; upload refused")
    granted, reason = aws_consent.is_granted(aws_consent.SERVICE_S3, profile=profile, region=region)
    if not granted:
        raise RuntimeError(f"S3 consent no longer holds; upload refused: {reason}")
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
        raise RuntimeError("S3 consent was withdrawn during the backup build; upload refused")
    if not grant.account or grant.account != account:
        raise RuntimeError("the recorded S3 consent does not name this account; upload refused")
    # Last, and deliberately after every other check: app teardown. A worker
    # thread cannot be killed, so cancelling the loop's await leaves the archive
    # build running; this is what makes that build stop short of uploading.
    if _STOP.is_set():
        raise RuntimeError("aws-control is shutting down; upload refused")


def run_snapshot_backup(account: str, profile: str, region: str, bucket: str) -> dict[str, Any]:
    """Build a snapshot archive and push it. Returns the run record."""
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
        key = f"snapshots/kirocrew-snapshot-{_stamp()}.tar.gz"
        _authorize_upload(account, profile, region)
        storage.put_file(
            profile,
            region,
            bucket,
            "backup",
            key,
            str(payload),
            account=account,
            timeout=_PUSH_TIMEOUT_SECS,
        )
        return _record_run(account, KIND_SNAPSHOT, key, payload.stat().st_size)


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


def run_sessions_backup(account: str, profile: str, region: str, bucket: str) -> dict[str, Any]:
    """Tar both session halves and push. Returns the run record.

    Refuses outright on a platform that cannot pin the traversal to descriptors.
    The session directories are agent-writable and this archive is uploaded
    unattended, so a name-based walk would trade an unrecoverable outcome
    (credentials reached by a junction swapped in after the check) for a
    convenience. See :func:`_add_tree`.
    """
    if not _CAN_PIN_TRAVERSAL:
        raise RuntimeError(_NO_PINNING_REASON)
    crew_sessions = data_home() / SESSIONS_DIR_NAME
    cli_sessions = kiro_sessions_dir()
    with tempfile.TemporaryDirectory(prefix="kc-backup-") as tmp:
        archive = Path(tmp) / f"sessions-{_stamp()}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            count = _add_tree(tar, crew_sessions, "crew")
            count += _add_tree(tar, cli_sessions, "cli")
        if count == 0:
            raise RuntimeError("no session files to archive")
        key = f"sessions/{archive.name}"
        _authorize_upload(account, profile, region)
        storage.put_file(
            profile,
            region,
            bucket,
            "backup",
            key,
            str(archive),
            account=account,
            timeout=_PUSH_TIMEOUT_SECS,
        )
        return _record_run(account, KIND_SESSIONS, key, archive.stat().st_size)


def list_remote_backups(profile: str, region: str, bucket: str, *, account: str) -> dict[str, Any]:
    """Remote backup listings for both kinds (newest first, capped page)."""
    result: dict[str, Any] = {}
    for kind, sub in ((KIND_SNAPSHOT, "snapshots"), (KIND_SESSIONS, "sessions")):
        page = storage.list_section(profile, region, bucket, "backup", sub, account=account)
        files = sorted(page["files"], key=lambda f: f.get("key", ""), reverse=True)
        result[kind] = files[:20]
    return result


def restore_download(
    profile: str, region: str, bucket: str, key: str, *, account: str
) -> dict[str, Any]:
    """Download one backup archive to the staging dir; return its local path.

    ``key`` is section-relative (``snapshots/...`` or ``sessions/...``) and
    validated by the handler with the same key rules as every drive key.

    The staging dir is agent-writable, so the download never writes through the
    final name: a link planted at that path would have the S3 bytes land on its
    target. Two separate checks are needed, and round 17 only had the second:

    * The staging DIRECTORY itself, and every component of it under the app data
      dir, must be a real directory. A linked ``restore/`` puts both the
      ``mkstemp`` temp file and the ``os.replace`` target outside app storage,
      which no per-file check can see.
    * The destination NAME must not already be a link or a non-regular file.

    Bytes then go to an exclusively-created temp file in the same directory and
    are atomically moved into place.
    """
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
    dest = staging / Path(key).name
    if is_link_or_junction(dest) or (dest.exists() and not dest.is_file()):
        raise ValueError("restore destination is not a regular file")
    fd, tmp_name = tempfile.mkstemp(prefix=".kc-restore-", dir=str(staging))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        storage.get_file(profile, region, bucket, "backup", key, str(tmp), account=account)
        size = tmp.stat().st_size
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"path": str(dest), "bytes": size}


def _account_view(account: str) -> dict[str, Any]:
    """Shape-safe read of one account's sub-dict: any non-dict level in a
    corrupted state file reads as empty instead of raising on ``.get``."""
    accounts = read_state().get("accounts", {})
    if not isinstance(accounts, dict):
        return {}
    entry = accounts.get(account, {})
    return entry if isinstance(entry, dict) else {}


def nightly_enabled(account: str) -> bool:
    return bool(_account_view(account).get("nightly"))


def set_nightly(account: str, enabled: bool) -> None:
    def mutate(state: dict[str, Any]) -> None:
        _account_state(state, account)["nightly"] = bool(enabled)

    _locked_state_update(mutate)


def last_runs(account: str) -> dict[str, Any]:
    runs = _account_view(account).get("runs", {})
    return runs if isinstance(runs, dict) else {}


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

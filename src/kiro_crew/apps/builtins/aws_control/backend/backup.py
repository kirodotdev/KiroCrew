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
from typing import Any, NoReturn, Optional

from kiro_crew import snapshot
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.platform_compat import file_lock, is_link_or_junction
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
    parsed to nothing usable carries nothing to lose.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
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
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            state = _read_state_for_update()
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
_unpersisted_lock = threading.Lock()


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
    with _unpersisted_lock:
        _unpersisted_runs[(_state_key(), account, kind)] = record


def _forget_unpersisted(account: str, kind: str, persisted_at: str) -> None:
    """Drop the held entry once a write for the same key has persisted.

    Conditional, not unconditional, and the condition is the point. This runs
    AFTER the sidecar lock is released, so another run for the same key can fail
    its write and cache a NEWER record inside the window between this run's write
    and this pop; an unconditional pop would evict that record, and the panel
    would then report the older archive as the last run while the newer upload
    has no record anywhere.

    Taking the sidecar lock for the pop would not fix it. The matching
    :func:`_remember_unpersisted` also runs outside that lock, and more
    fundamentally two gateway processes hold SEPARATE in-memory caches, so no
    file lock can serialize one process's pop against the other's cache. The
    invariant that holds in both cases is monotonic: never evict a record
    STRICTLY NEWER than the one just persisted.

    An EQUAL stamp evicts. Two back-to-back runs can stamp identically where the
    clock is coarse -- Windows granularity is far above a microsecond -- and
    keeping the held record on a tie makes it immortal for the life of the
    process, since no later write can ever compare greater. A tie means the two
    records are simultaneous and the persisted one is on disk, so retaining the
    held copy buys nothing. A stamp that is missing or not a string cannot be
    ordered and is unusable, so it is dropped.
    """
    key = (_state_key(), account, kind)
    with _unpersisted_lock:
        held = _unpersisted_runs.get(key)
        if held is None:
            return
        held_at = held.get("at")
        if not isinstance(held_at, str) or held_at <= persisted_at:
            _unpersisted_runs.pop(key, None)


def _merge_unpersisted(account: str, runs: dict[str, Any]) -> dict[str, Any]:
    """Overlay this process's unpersisted runs onto what the state file holds.

    Newest wins, rather than memory always winning: a second gateway process on
    the same data home can persist a NEWER run while this one still remembers a
    write that failed, and the sidecar lock exists precisely because that other
    process can exist. Both stamps come from the same UTC
    ``isoformat(timespec="microseconds")`` call, so comparing the strings orders
    them -- and microseconds rather than seconds is what makes that comparison
    able to separate two uploads that finished in the same second. A persisted
    stamp that is not a string is unusable and loses.
    """
    path = _state_key()
    with _unpersisted_lock:
        remembered = {
            kind: record
            for (state_path, acct, kind), record in _unpersisted_runs.items()
            if state_path == path and acct == account
        }
    for kind, record in remembered.items():
        persisted = runs.get(kind)
        persisted_at = persisted.get("at") if isinstance(persisted, dict) else None
        if not isinstance(persisted_at, str) or persisted_at < str(record.get("at", "")):
            runs[kind] = record
    return runs


def _record_run(account: str, kind: str, key: str, size: int) -> dict[str, Any]:
    record: dict[str, Any] = {
        "key": key,
        "bytes": size,
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
        # Stamp HERE, not where `record` was built. `mutate` runs inside the
        # sidecar lock, so a stamp taken here is ordered by the same lock that
        # orders the writes; a stamp taken before the lock is not. Two runs can
        # stamp in one order and acquire the lock in the other -- a manual run
        # racing the nightly loop -- and then the older-stamped record writes
        # LAST and the ledger reports the wrong archive as the last run.
        #
        # This is load-bearing for more than the ledger: everything that compares
        # these stamps (the overlay's newest-wins in `_merge_unpersisted`, the
        # monotonic eviction in `_forget_unpersisted`) is only sound if stamp
        # order matches WRITE order, which is exactly what generating it in here
        # buys. Microsecond precision alone does not: it separates two stamps
        # without telling you which write landed first.
        record["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        runs[kind] = record
        return runs[kind]

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
    _forget_unpersisted(account, kind, record["at"])
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


def _refuse_upload(account: str, reason: str, *, caller: str, outcome: str = "denied") -> NoReturn:
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

    Best-effort, like the route's audit: a failed audit must never convert a
    refusal into an upload.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="aws_control.backup_upload",
            outcome=outcome,
            source="aws-control",
            resources=f"account={account}"[:200],
            error=reason[:200],
        )
    except Exception:
        logger.debug("aws-control SEL audit failed", exc_info=True)
    raise RuntimeError(reason)


def _authorize_upload(account: str, profile: str, region: str, *, caller: str) -> None:
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
        )
    if not is_app_enabled("aws-control"):
        _refuse_upload(
            account,
            "aws-control was disabled during the backup build; upload refused",
            caller=caller,
        )
    granted, reason = aws_consent.is_granted(aws_consent.SERVICE_S3, profile=profile, region=region)
    if not granted:
        _refuse_upload(
            account, f"S3 consent no longer holds; upload refused: {reason}", caller=caller
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
        )
    if not grant.account or grant.account != account:
        _refuse_upload(
            account,
            "the recorded S3 consent does not name this account; upload refused",
            caller=caller,
        )
    # Last, and deliberately after every other check: app teardown. A worker
    # thread cannot be killed, so cancelling the loop's await leaves the archive
    # build running; this is what makes that build stop short of uploading.
    if _STOP.is_set():
        _refuse_upload(
            account, "aws-control is shutting down; upload refused", caller=caller, outcome="failed"
        )


def run_snapshot_backup(
    account: str, profile: str, region: str, bucket: str, *, caller: str
) -> dict[str, Any]:
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
        _authorize_upload(account, profile, region, caller=caller)
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
        _authorize_upload(account, profile, region, caller=caller)
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
    the #4800 failure this package already carries a ``LoopBoundLock`` to avoid
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


def last_runs(account: str) -> dict[str, Any]:
    """The last run per kind, including runs this process could not persist.

    The merge is not cosmetic. A run whose state write failed really did upload,
    and :func:`due_for_nightly` reads its answer from here -- so without the
    overlay the nightly loop treats the account as never backed up and uploads
    again on every wake. See :data:`_unpersisted_runs`.
    """
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

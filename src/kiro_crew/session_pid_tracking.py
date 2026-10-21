"""Which pids Kiro Crew is tracking, and how a record names the process it means.

The two tracking files -- ``kiro_pids.txt`` (bare and ``child:parent`` lines) and
``kiro_session_pids.txt`` (``<gateway>:<pid>:<start token>`` roots) -- with their paths,
locks, lenient reads, atomic rewrites, the track and untrack writers and the Windows
pending-pin rows no pass may prune; the identity probes a record is keyed by (start token,
age, spawn grace); and the in-process registry of sweep-protected pids.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.owner_only_files import ensure_directory, owner_only_opener_for
from kiro_crew.runtime_ownership import PidRefcount

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


_PID_FILE = "kiro_pids.txt"


_SESSION_PID_FILE = "kiro_session_pids.txt"

# ── Orphan-sweep spawn grace period ──────────────────────────────────────────
# A freshly spawned kiro-cli PID is tracked in kiro_session_pids.txt immediately
# by _track_session_pid(), but the _starting_pids protection set is only
# populated AFTER provider.start() returns (multi-second window). During this
# window the sweep may classify the PID as orphaned and SIGKILL it. To prevent
# this, any tracked PID younger than SWEEP_SPAWN_GRACE_SECONDS is unconditionally
# skipped (left alive) in _sweep_pid_entries. A missed kill self-heals next
# cycle; a wrong kill does not.
SWEEP_SPAWN_GRACE_SECONDS = 120

# Windows ``get_process_start_id`` returns the creation ``FILETIME`` as decimal
# text in 100-ns units since the FILETIME epoch (1601-01-01 UTC), NOT Unix
# seconds like the darwin value. Converting to a Unix-epoch age therefore needs
# both a unit scale (100 ns → s) and an epoch shift (1601 → 1970); the naive
# ``time.time() - float(start_id)`` the darwin arm uses would be wrong by the
# epoch offset. 11644473600 is the whole seconds between 1601-01-01 and the Unix
# epoch; 1e7 is the number of 100-ns ticks per second.
_FILETIME_TICKS_PER_SECOND = 10_000_000


_FILETIME_UNIX_EPOCH_OFFSET_SECONDS = 11_644_473_600


def _pid_age_seconds(pid: int, proc_root: str = "/proc") -> float | None:
    """Return the process age in seconds, or None if it cannot be determined.

    On Linux, reads /proc/<pid>/stat field 22 (starttime in clock ticks since
    boot) through ``platform_compat.read_proc_stat`` and dates it against
    ``<proc_root>/uptime`` with ``platform_compat.process_age_secs``.

    On macOS (and other POSIX without /proc): derived from
    ``platform_compat.get_process_start_id``, whose darwin value is the process
    start time in epoch ``seconds.microseconds`` — so this needs no
    ``subprocess`` and is safe on the event loop. Empirically required: the
    startup sweep SIGKILL'd a live kiro-cli off a stale dead-gateway entry on
    macOS because the grace window silently did not apply there.

    On Windows: derived from ``platform_compat.get_process_start_id``, whose
    value is the process creation ``FILETIME`` (100-ns units since the 1601
    epoch). It is converted to a Unix-epoch instant before dating it against
    ``time.time()`` — see ``_FILETIME_UNIX_EPOCH_OFFSET_SECONDS``. Like the
    macOS path this needs no ``subprocess`` and is safe on the event loop.
    Without it the spawn grace silently did not apply on Windows, so the orphan
    sweep could SIGKILL a provider still inside its multi-second spawn window.

    The *proc_root* parameter allows injection of a fake /proc tree for testing.
    """
    if platform_compat.IS_WINDOWS:
        start_id = platform_compat.get_process_start_id(pid)
        if start_id is None:
            return None
        try:
            unix_start = (
                float(start_id) / _FILETIME_TICKS_PER_SECOND - _FILETIME_UNIX_EPOCH_OFFSET_SECONDS
            )
        except ValueError:
            return None
        return max(0.0, time.time() - unix_start)
    if sys.platform != "linux":
        start_id = platform_compat.get_process_start_id(pid)
        if start_id is None:
            return None
        try:
            return max(0.0, time.time() - float(start_id))
        except ValueError:
            return None
    stat = platform_compat.read_proc_stat(pid, proc_root=Path(proc_root))
    if stat is None or stat.start_ticks is None:
        return None
    try:
        uptime = float(Path(f"{proc_root}/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return platform_compat.process_age_secs(stat.start_ticks, now=uptime)


def _pid_in_spawn_grace(pid: int) -> bool:
    """Return True if the PID is within the spawn grace period and should be skipped.

    - All platforms (Linux via /proc, macOS via ``get_process_start_id``,
      Windows via the creation ``FILETIME``) + successful age read: True if
      age < SWEEP_SPAWN_GRACE_SECONDS.
    - Age read failure (age is None): True (treat as young — safe direction;
      dead processes are already pruned by the earlier liveness check).
    """
    age = _pid_age_seconds(pid)
    if age is None:
        return True  # cannot determine age → treat as young (safe direction)
    return age < SWEEP_SPAWN_GRACE_SECONDS


def _pid_start_token(pid: int) -> str | None:
    """Stable, persistable identity token for a live PID (PID-recycle guard).

    A thin delegate to ``platform_compat.get_process_start_id``, which is
    in-process on every platform (``/proc`` read on Linux, ``libproc`` ctypes on
    macOS) — deliberately NOT ``ps``, so the token lookup itself is non-blocking
    and safe to call from the asyncio event loop. (Whether an enclosing tracker
    may run on the loop is governed by that tracker's exclusive file lock, not
    by this lookup — see ``AUTOSDE: no-blocking-call-on-event-loop``.)

    Returns ``None`` when identity cannot be determined, meaning a process we may
    not introspect or a read that failed. Every platform Crew supports HAS a
    source — ``/proc`` on Linux, ``libproc`` on macOS, the creation FILETIME on
    Windows — so ``None`` is a failed read rather than an unsupported host.
    Callers MUST treat ``None`` as "unknown", never as a mismatch — see the sweep
    call sites.

    Note this cannot reuse ``acp.client._get_start_time``: that hashes with
    builtin ``hash()``, which is PYTHONHASHSEED-randomized per interpreter and
    therefore meaningless once written to disk and compared by a later gateway.

    SUBTRACTIVE ONLY. A live value that differs from the recorded one proves the PID
    was recycled and prunes the entry without a signal; a value that MATCHES never
    authorizes one, because the tracking file is same-uid-writable and this token is
    readable from ``/proc`` for any introspectable PID. What authorizes a kill is the
    argv test plus each arm's own reparent or gateway-liveness condition.

    That rule is also why the Linux value needs no boot scope here: it counts
    ``/proc`` start ticks from BOOT, so a post-reboot PID can repeat an earlier
    boot's pair — which under subtractive use costs a missed prune, not a wrong kill.
    ``platform_compat._own_identity_token`` is the reboot-unique form, should a
    future change want one.
    """
    return platform_compat.get_process_start_id(pid)


def _pid_file_path() -> Path:
    return config_dir() / _PID_FILE


def _session_pid_file_path() -> Path:
    return config_dir() / _SESSION_PID_FILE


@contextmanager
def _session_pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for session PID file operations."""
    lock_path = _session_pid_file_path().with_suffix(".lock")
    ensure_directory(lock_path.parent)
    # Open non-truncating; see ``platform_compat.open_lock_file`` for why ``"w"``
    # loses the lock on Windows (GH-9248). The helper does the create-or-open in
    # one syscall; the parent mkdir above stays because it does not.
    with platform_compat.open_lock_file(lock_path) as lock_fd:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            yield


def _track_session_pid(pid: int, start_token: str | None = None) -> None:
    """Record a kiro-cli root in the session tracking file.

    Entries are written as ``<gateway_pid>:<child_pid>:<start_token>`` so each
    gateway instance can identify and sweep only its own children, and so the
    sweep can verify the PID still names the SAME process before killing
    (PID-recycle guard — see ``_pid_start_token``). A token is available on every
    platform Crew supports, Windows included, so the legacy
    ``<gateway_pid>:<child_pid>`` form is written only when the probe itself
    fails; the sweep then rests on the argv gate and the spawn grace alone. That gate
    recognises every registered harness (``_MANAGED_AGENT_BASENAMES``, projected from
    the backend registry) wherever a command line is readable, and on Windows only an
    image name is -- so an interpreter-hosted adapter reads as ``node.exe`` there and a
    token-less entry for one is not recognised. See :func:`_is_managed_agent_process`.

    *start_token* is the identity the caller read at spawn. Pass it: a caller
    that also retires by identity (:func:`_untrack_root_by_identity`) must record
    the SAME token it will later compare, and two probes of one number are two
    reads that can disagree once the number changes hands. Omitted, the token is
    probed here, which is what every caller did before spawn learned to keep it.
    The recorded identity is always the token in hand; the one place this
    function probes the number again (before replacing another line, below) is an
    OCCUPANCY check -- does the number still name this process -- never the
    source of what gets written.

    One line per ``gw:pid`` number. The exact entry already present is a re-track
    and is left alone. A line under the same number with a DIFFERENT token is a
    predecessor's: this caller is recording the process that holds the number
    NOW, so whatever that line named has exited, and it is REPLACED rather than
    kept. A kept stale line is the hole the identity-bound retirement falls
    through -- the successor never gets a line of its own, so nothing about it is
    ever in this file. A token-less write never replaces a tokened line: with no
    identity to offer it proves nothing about who holds the number, and the sweep
    prunes a stale tokened line by liveness on its own.

    A refused replacement RAISES, like a refused append: failing to record a live
    root is the one unrecoverable direction here (see ``_rewrite_pid_file``) -- a
    root in neither file is unreachable by every reaper until reboot.
    """
    token = start_token if start_token else _pid_start_token(pid)
    prefix = f"{os.getpid()}:{pid}"
    entry = f"{prefix}:{token}" if token else prefix
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        ensure_directory(path.parent)
        if path.exists():
            lines = _read_pid_file_text(path).splitlines()
            kept: list[str] = []
            already_present = False
            stale_predecessor = False
            for ln in lines:
                stripped = ln.strip()
                if stripped == entry:
                    # Ours. Not kept here: the single write below is what puts
                    # exactly one line for the number back, whichever order
                    # the file listed ours and a stale one in.
                    already_present = True
                    continue
                if stripped == prefix or stripped.startswith(prefix + ":"):
                    if not token:
                        # Dedup on the gw:pid prefix, as before: a token-less
                        # re-track never duplicates -- or downgrades -- a line
                        # that carries an identity.
                        return
                    stale_predecessor = True
                    continue
                kept.append(ln)
            if already_present and not stale_predecessor:
                return
            if stale_predecessor:
                # The token in hand was read at SPAWN. This write may be late --
                # a tracker that ran after its own root died and the number was
                # handed on -- in which case the "stale predecessor" line above
                # is the live successor's only record, and replacing it would
                # leave that root reachable by nothing. Re-read the number's
                # identity NOW, under the lock: only while it still names the
                # process this token belongs to is the replacement ours to make.
                # A number that has moved on (or cannot be read) is not recorded
                # by this caller at all -- deny-by-default, the direction every
                # writer in this module fails toward.
                if _pid_start_token(pid) != token:
                    return
                kept.append(entry)
                if not _rewrite_pid_file(path, "\n".join(kept) + "\n"):
                    raise OSError(f"could not record root PID {pid} in {path}")
                return
        with open(path, "a", encoding="utf-8", opener=owner_only_opener_for(path)) as f:
            f.write(f"{entry}\n")


@contextmanager
def _pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for all PID file read-modify-write operations."""
    lock_path = _pid_file_path().with_suffix(".lock")
    ensure_directory(lock_path.parent)
    # Open non-truncating; see ``platform_compat.open_lock_file`` for why ``"w"``
    # loses the lock on Windows (GH-9248). The helper does the create-or-open in
    # one syscall; the parent mkdir above stays because it does not.
    with platform_compat.open_lock_file(lock_path) as lock_fd:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            yield


def _read_pid_file_text(path: Path) -> str:
    """A PID tracking file's text; raises ``OSError`` exactly as ``read_text`` does.

    The files hold ASCII digits and colons, so a byte that is not UTF-8 is
    damage, and a strict decode would raise ``UnicodeDecodeError`` -- which no
    ``except OSError`` around these reads catches -- out of every reader,
    including the boot sweep. Undecodable bytes become U+FFFD instead, so the
    field they sit in fails its ``int()`` parse and each reader's
    malformed-entry rule applies to it exactly as to an ASCII-garbled one.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _rewrite_pid_file(path: Path, content: str) -> bool:
    """Replace *path*'s content atomically; log and return ``False`` on failure.

    Atomic (temp file + rename) because a plain ``write_text`` truncates the
    file to zero BEFORE writing the kept entries: a failure inside that window
    leaves a SHORT file whose surviving content is perfectly well-formed, and
    every dropped entry is an agent runtime no reaper can find again.

    A failure here is REPORTED, not propagated. Pruning an entry is idempotent
    and self-retrying: the next sweep — or the next gateway start — re-reads the
    file, finds that PID already dead, and prunes it again, so a failed rewrite
    costs one stale line rather than a runtime. Propagating would be worse than
    the problem: ``cleanup_orphaned_sessions`` runs unguarded on the gateway's
    startup path, and on Windows ``replace_with_retry`` deliberately declines to
    retry a sharing violation while an event loop is running (an indexer or AV
    scanner holding the temp file is enough), so an escaping error there aborts
    startup entirely.

    The tracking direction is the opposite case and must NOT be quieted this
    way: failing to RECORD a freshly spawned PID is unrecoverable, because no
    reaper can identify that runtime afterwards.
    """
    try:
        atomic_write(path, content)
        return True
    except OSError:
        logger.error(
            "Could not rewrite PID file %s; its entries stay until the next sweep",
            path,
            exc_info=True,
        )
        return False


def _pid_gone_or_unmanaged(pid: int) -> bool:
    """Return ``True`` when it is safe to *untrack* ``pid`` from the PID files.

    Safe means the process is confirmed gone. Returns ``False`` when a process
    with this PID is still alive (or is unsignalable): a teardown kill may have
    failed to reap our agent (``killpg`` misses children in other process
    groups; a mid-init crash can race the descendant scan in ``_kill_process``),
    so the tracking entry is **retained**. The periodic orphan sweep — which
    re-validates ownership via ``_is_managed_agent_process`` before it kills
    anything — then reaps a genuine survivor and skips a recycled PID.
    Untracking a live survivor here would orphan it permanently, since every
    sweep mechanism keys off these files (the ``kiro-cli-chat acp`` memory-leak
    class). Fail-safe: any inconclusive result retains.

    Routes through ``platform_compat.pid_liveness`` (a non-blocking probe, safe
    on the asyncio event loop) rather than a raw ``os.kill(pid, 0)`` — on
    Windows that call TERMINATES the target. This is stricter than upstream
    ``33da30e6``, which untracks on ``PermissionError`` (assumes a recycled,
    other-user PID): ``pid_liveness`` collapses EPERM into ``PID_UNSIGNALABLE``,
    which we treat as "retain", so an unsignalable PID stays tracked for the
    sweep to re-validate off the hot path. Never orphaning a live survivor is
    the invariant that matters; a retained-but-recycled PID is harmless (the
    sweep's ownership recheck skips it). It deliberately does NOT call
    ``_is_managed_agent_process`` (which shells out to ``ps`` on macOS): that
    would block the loop and could mislabel a live-but-transiently-unreadable
    agent as unmanaged — the exact leak this guards against.
    """
    return platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD


def _collect_active_pids(sessions: "dict") -> tuple[set[int], bool]:
    """Extract PIDs from live sessions. Returns ``(pids, ok)``.

    If any session's PID is not an int or extraction fails,
    returns ``(partial_set, False)`` — caller should skip the sweep.
    """
    pids: set[int] = _protected_pids()  # shared _bg / subagent runtimes shielded from the sweep
    for sess in sessions.values():
        # ACP provider: long-lived process PID via client._pid
        client = getattr(sess.provider, "client", None)
        if client is not None:
            try:
                pid = client._pid  # type: ignore[attr-defined]
                if not isinstance(pid, int):
                    logger.warning(
                        "PID for session is not an int (%r) — skipping orphan sweep this cycle", pid
                    )
                    return pids, False
                pids.add(pid)
            except Exception:
                logger.warning("Failed to read PID for session — skipping orphan sweep this cycle")
                return pids, False
        # CC provider: protect long-lived process PID (per_session mode)
        cc_proc = getattr(sess.provider, "_proc", None)
        if cc_proc is not None and cc_proc.returncode is None:
            pids.add(cc_proc.pid)
        # CC provider: protect in-flight subprocess PID (ephemeral mode)
        active_proc = getattr(sess.provider, "_active_proc", None)
        if active_proc is not None and active_proc.returncode is None:
            pids.add(active_proc.pid)
    return pids, True


def _windows_pending_pid_entry(line: str) -> bool:
    """Pending exact pins prevent every PID-file sweep from retiring the record."""
    if not platform_compat.IS_WINDOWS:
        return False
    parts = line.strip().split(":")
    try:
        pid = int(parts[1] if len(parts) in (2, 3) else parts[0])
    except ValueError:
        return False
    token = parts[2] if len(parts) == 3 else None
    return platform_compat.windows_tree_cleanup_pending(pid, token)


def _windows_pending_child_entry(line: str) -> bool:
    """The same rule for the tracked-child file, whose rows have a different shape.

    ``kiro_pids.txt`` rows are ``child_pid:parent_pid[:child-start-id]``, so the
    positions invert the session file's ``gw:pid[:start-id]``: the pid this row is
    ABOUT is first and the optional identity belongs to that child, never to the
    parent. Both ends are therefore asked by pid alone -- a pin is keyed by the
    root it owns, so a child's own identity cannot match one, and the parent's
    identity is not recorded here to match with.

    Either end being an unretired root retains the row: the parent because the
    drain still owns the tree this child belongs to, the child because a nested
    runtime is itself a root. Retention is bounded by the reservation budget,
    and an unretired pin is what a restart needs to find these identities at all.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    parts = line.strip().split(":")
    try:
        ends = [int(parts[0])] if len(parts) == 1 else [int(parts[0]), int(parts[1])]
    except (ValueError, IndexError):
        return False
    return any(platform_compat.windows_tree_cleanup_pending(pid, None) for pid in ends)


def _write_back_pid_file(killed_or_dead: set[str]) -> None:
    """Remove *killed_or_dead* entries from the session PID file.

    Rewrites via :func:`atomic_write` (temp file + rename). A plain
    ``write_text`` truncates the file to zero BEFORE writing the kept entries,
    so a crash or a write failure inside that window leaves a SHORT file whose
    surviving content is perfectly well-formed — every dropped entry becomes an
    agent runtime no reaper can ever find again, with nothing raised and
    nothing logged. Rename makes the file either wholly old or wholly new.
    """
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if path.exists():
            current = _read_pid_file_text(path).splitlines()
            keep = [
                entry
                for entry in current
                if entry.strip()
                and (entry.strip() not in killed_or_dead or _windows_pending_pid_entry(entry))
            ]
            _rewrite_pid_file(path, ("\n".join(keep) + "\n") if keep else "")


def _session_pid_entry_index(my_gw_pid: int) -> dict[int, tuple[str, str | None]]:
    """``{child_pid: (entry_line, recorded_token)}`` over *my_gw_pid*'s entries.

    The periodic sweep runs in two phases with an event-loop hop between them, so
    the kill phase cannot be handed a verdict computed in the scan phase and trust
    it: the PID may have been reallocated while the loop was doing something else.
    It re-reads what the ENTRY recorded and re-derives the verdict against the live
    process, which is both fresher and the only thing that can be re-derived.

    Returning the entry LINE matters as much as the token. Entry text is what
    :func:`_write_back_pid_file` matches on, and a ``<gw>:<pid>`` string rebuilt
    from parts matches only a two-field entry — so a token-bearing entry stays in
    the file after its process is reaped, and the sweep meets a dead PID there on
    every later pass.
    """
    index: dict[int, tuple[str, str | None]] = {}
    path = _session_pid_file_path()
    try:
        with _session_pid_file_lock():
            if not path.exists():
                return index
            lines = _read_pid_file_text(path).splitlines()
    except OSError:
        logger.warning("Could not read %s for the kill phase", path, exc_info=True)
        return index
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(":")
        if len(parts) not in (2, 3):
            continue
        try:
            gw_pid = int(parts[0])
            child_pid = int(parts[1])
        except ValueError:
            continue
        if gw_pid != my_gw_pid or child_pid <= 0:
            continue
        recorded_token = parts[2] or None if len(parts) == 3 else None
        index[child_pid] = (stripped, recorded_token)
    return index


def retained_gateway_pids() -> frozenset[int]:
    """The gateway pids that still have an entry in ``kiro_session_pids.txt``.

    Read-only, under the file's own lock, and the file is not changed. This is
    the evidence ``session_work_dir`` uses for a predecessor gateway of THIS data
    home: :func:`cleanup_orphaned_sessions` has already reaped every child it
    could confirm dead or kill and removed those entries, so a gateway pid that
    still has one may have something alive and its run directories are kept. A
    ledger that cannot be read raises ``OSError`` rather than answering "nothing
    retained": the callers decide a deletion on this answer and fail closed. A
    byte that is not UTF-8 gets the malformed-entry rule an ASCII-garbled one
    does: a gateway-pid field it lands in fails its parse and the line is skipped,
    as the boot sweep has already pruned it.
    """
    path = _session_pid_file_path()
    with _session_pid_file_lock():
        if not path.exists():
            return frozenset()
        text = _read_pid_file_text(path)
    lines = text.splitlines()
    retained: set[int] = set()
    for line in lines:
        parts = line.strip().split(":")
        if len(parts) not in (2, 3):
            continue
        try:
            gw_pid = int(parts[0])
        except ValueError:
            continue
        if gw_pid > 0:
            retained.add(gw_pid)
    return frozenset(retained)


def retire_windows_tree_tracking(root_pid: int) -> None:
    """Retire tracking under the cleanup owner's exact root pin, or fail closed."""
    if not _untrack_pid(root_pid) or not _untrack_session_pid(root_pid):
        raise OSError("Windows tree tracking retirement did not complete")
    unregister_protected_pid(root_pid)


def _track_pid(pid: int) -> None:
    """Append a PID to the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        ensure_directory(path.parent)
        with open(path, "a", encoding="utf-8", opener=owner_only_opener_for(path)) as f:
            f.write(f"{pid}\n")


def _track_child_pids(pids: Mapping[int, object], parent_pid: int = 0) -> None:
    """Append descendant PIDs to the tracking file as ``child:parent[:start-id]``.

    The third field is the child's process-start identity
    (:func:`_pid_start_token` -- colon-free, in-process and non-blocking on
    every platform), recorded so the orphan sweep can prove a PID was
    recycled before killing it. A child whose identity cannot be read at
    track time is written in the legacy two-field shape.
    """
    if not pids:
        return
    with _pid_file_lock():
        path = _pid_file_path()
        ensure_directory(path.parent)
        existing = set(_read_pid_file_text(path).splitlines()) if path.exists() else set()
        with open(path, "a", encoding="utf-8", opener=owner_only_opener_for(path)) as f:
            for pid in pids:
                key = f"{pid}:{parent_pid}"
                if any(e == key or e.startswith(key + ":") for e in existing):
                    continue
                token = _pid_start_token(pid)
                entry = f"{key}:{token}" if token else key
                f.write(f"{entry}\n")
                existing.add(entry)


def _recorded_start_token(record: object) -> str | None:
    """The start identity a caller already captured, as a file field.

    The write path must NEVER read a live pid's identity for itself. Reading it
    at write time reopens the reuse window the caller closed: a descendant that
    exited and had its number taken would be written with the STRANGER's token,
    and the sweep's guard compares live against recorded -- both the stranger's,
    so they match, and it kills an unrelated process. A caller earns the right to
    write a token by capturing it while the pid was confirmed to be its own; this
    only carries that value through.

    Accepts the ``(start_id, basename)`` record shape and the legacy scalar. A
    value that cannot be a field -- absent, or carrying the ``:`` the format
    separates on -- degrades to ``None``, which writes the two-field shape and
    leaves the sweep with no token to match on.
    """
    value = record[0] if isinstance(record, tuple) and record else record
    if value is None:
        return None
    token = str(value)
    return token if token and ":" not in token else None


def _replace_child_pids(
    pids: Mapping[int, object], parent_pid: int, *, drop: Iterable[int] = ()
) -> bool:
    """Rewrite this parent's lines for the children the caller names.

    The whole-set counterpart to :func:`_track_child_pids`, for a caller that
    re-enumerates its tree and knows the complete answer each time. It is what
    :func:`~kiro_crew.acp.runtime.AcpRuntime._snapshot_descendants` needs and an
    append cannot give: a pid whose start identity changed already has a line
    under the same ``child:parent`` key, and the append dedupes on that prefix,
    so the stale identity would survive. Removing the line and appending a fresh
    one is two writes, and a caller cannot tell that the first one failed --
    ``_untrack_child_pids`` discards the rewrite's answer, by design, because
    pruning a dead entry is self-retrying. Replacing a live entry is not.

    One lock, one atomic rewrite, and the answer is returned.

    **Only the caller's own children are touched.** A line is removed when its
    child pid is named in *pids* or in *drop* -- never merely because it sits
    under this ``parent_pid``. A root pid is reused like any other: a descendant
    that outlived an earlier runtime holding this number is still tracked under
    it, and wiping the block by owner alone would untrack that survivor
    permanently, which is the leak this file exists to prevent. Lines under
    another parent, and the bare root lines, are likewise untouched.

    *drop* names the children to remove without rewriting -- the ones the caller
    has confirmed gone. Passing neither *pids* nor *drop* writes nothing.

    Field 3 is :func:`_recorded_start_token` of the mapping's VALUE, so the
    identity written is the one the caller captured under confirmation. Same
    two-field fallback as :func:`_track_child_pids`, so the file stays one
    format.

    Returns ``False`` when the rewrite failed, so the caller can leave its own
    in-memory state alone and retry on its next pass.
    """
    if not parent_pid:
        return False
    owned = {str(p) for p in pids} | {str(p) for p in drop}
    if not owned:
        return True
    with _pid_file_lock():
        path = _pid_file_path()
        ensure_directory(path.parent)
        lines = _read_pid_file_text(path).splitlines() if path.exists() else []
        kept: list[str] = []
        for raw in lines:
            entry = raw.strip()
            if not entry:
                continue
            fields = entry.split(":")
            if len(fields) >= 2 and fields[1] == str(parent_pid) and fields[0] in owned:
                continue
            kept.append(entry)
        for pid, record in pids.items():
            key = f"{pid}:{parent_pid}"
            token = _recorded_start_token(record)
            kept.append(f"{key}:{token}" if token else key)
        return _rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")


def _untrack_child_pids(pids: Mapping[int, object]) -> None:
    """Remove descendant PIDs from the tracking file."""
    if not pids:
        return
    to_remove = {str(p) for p in pids}
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = _read_pid_file_text(path).splitlines()
        lines = [
            ln for ln in lines if ":" not in ln.strip() or ln.strip().split(":")[0] not in to_remove
        ]
        _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_pid(pid: int) -> bool:
    """Remove a PID, reporting whether its tracking file was updated."""
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return True
        lines = _read_pid_file_text(path).splitlines()
        lines = [ln for ln in lines if ln.strip() != str(pid)]
        return _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_session_pid(pid: int) -> bool:
    """Remove this gateway's ``<gw_pid>:<pid>`` entry from the session PID
    tracking file.  Called on clean provider shutdown so the periodic
    orphan sweep doesn't race against legitimate still-running kiro-cli
    processes whose in-memory session entry has transiently gone away
    (e.g. during compaction/reset/replace). Return whether the write succeeded.
    """
    prefix = f"{os.getpid()}:{pid}"
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if not path.exists():
            return True
        lines = _read_pid_file_text(path).splitlines()
        # Match both the legacy ``gw:pid`` form and the token-bearing
        # ``gw:pid:token`` form (see _track_session_pid).
        lines = [
            ln for ln in lines if ln.strip() != prefix and not ln.strip().startswith(prefix + ":")
        ]
        return _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_pid_if_dead(pid: int) -> bool:
    """Remove *pid*'s bare ``kiro_pids.txt`` line only if the number is DEAD now.

    The liveness probe runs INSIDE ``_pid_file_lock``, which is the lock
    :func:`_track_pid` appends under, so at the moment of the probe the number's
    holder is one of exactly two things. Either a successor already holds the
    number and has appended its bare line -- it cannot be mid-append, because it
    would need the lock this call holds -- in which case ``pid_exists`` is True
    and the line is RETAINED; or the number's holder has not called
    :func:`_track_pid` yet (or there is none), in which case a live holder still
    answers the probe and the line is RETAINED, and a dead number does not and
    the line is REMOVED. A retained bare line naming a LIVE successor is exactly
    the line that successor's own ``_track_pid`` writes, so retaining is never
    wrong. A bare line retained for a live holder that is NOT one of ours (an
    unrelated process that inherited the number) simply stays until the number
    goes dead: the bare-line prune (``_cleanup_orphaned_mcp_servers``) removes a
    bare line on ``pid_exists`` alone and never signals by it, so the cost of the
    stale line is one entry, not a kill.
    ``pid_exists`` treats EPERM as alive: deny-by-default, the direction every
    reaper in this module fails toward.

    Returns whether the bare-line step is SETTLED: the line was removed, or it
    was correctly retained for a live holder. A refused rewrite returns False.
    """
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return True
        if platform_compat.pid_exists(pid):
            return True
        lines = _read_pid_file_text(path).splitlines()
        lines = [ln for ln in lines if ln.strip() != str(pid)]
        return _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_root_by_identity(pid: int, start_token: str | None) -> bool:
    """Retire a root's registry lines only while they still name THAT process.

    For the caller that learned of a death by OBSERVING it (the runtime reader
    hitting EOF) rather than by causing it. A root that exited from outside has no
    process left to re-check, so the number alone cannot say whose lines these
    are: between the caller's liveness probe and this write the kernel can hand
    the same number to a replacement root this gateway just tracked, and a
    prefix-matched untrack would then erase the successor's only durable record.
    The recorded start token is the identity the number lacks, so under
    ``_session_pid_file_lock`` ONLY the line equal to
    ``<gateway_pid>:<pid>:<start_token>`` is removed -- a line carrying a
    different token is a successor's and is left alone, and a line with no token
    cannot be proven ours and is left to the sweep. With no token to compare
    (``None``) nothing is touched at all, which is the same deny-by-default every
    reaper in this module fails toward.

    The bare ``<pid>`` root line in ``kiro_pids.txt`` carries no identity, so the
    session file is not consulted about the number's new holder even though a
    successor now REPLACES a stale predecessor line with its own
    (:func:`_track_session_pid`): a successor whose spawn has not reached that
    write yet has no line, and one whose identity could not be read writes a
    token-less line that names nobody. The kernel can say what the file cannot.
    The bare line goes through :func:`_untrack_pid_if_dead`, which probes
    ``pid_exists`` inside ``_pid_file_lock`` -- the lock ``_track_pid`` appends
    under -- and removes the line only when the number is dead at that moment; a
    live holder keeps it. The two per-file locks are taken in turn, never nested,
    and no lock spans the pair: a successor's tracking may land between the two
    steps, and then it finds its own bare line retained and its session line
    written by itself, so neither ordering loses a live root.

    Returns whether the retirement COMMITTED -- every write this call owed, not
    merely the session one: the session rewrite landed AND the bare-line step
    settled (removed, or retained for a live holder). The caller logs a full
    retirement on a true answer (``acp/runtime_liveness.py``), so reporting the session
    rewrite alone would claim a clean retirement while a stale bare root line
    survives a refused ``kiro_pids.txt`` write; a refusal is not a commit. The
    sweep does prune such a line within its tick, but a return value that
    outruns the writes it reports is what the log then repeats.
    """
    if not start_token:
        return False
    ours = f"{os.getpid()}:{pid}:{start_token}"
    with _session_pid_file_lock():
        session_path = _session_pid_file_path()
        if not session_path.exists():
            return False
        lines = _read_pid_file_text(session_path).splitlines()
        kept = [ln for ln in lines if ln.strip() != ours]
        if len(kept) == len(lines):
            return False
        if not _rewrite_pid_file(session_path, "\n".join(kept) + "\n" if kept else ""):
            return False
    return _untrack_pid_if_dead(pid)


# ── Sweep-protected PIDs ──────────────────────────────────────────────────
# Live agent-process PIDs tracked in the PID file but NOT registered as
# SessionMap sessions (e.g. app-managed worker pools / shared ACP runtimes).
# The periodic orphan sweep consults _protected_pids() to avoid killing them.
#
# COUNTED, not listed. Several independent holders shield one pid -- an
# app-managed worker pool, the knowledge LLM pool, a shared ACP runtime -- and
# each pairs its own register with its own unregister. As a plain set the first
# holder to leave tore the shield off a process the others were still using, and
# the sweep then reaped a live runtime. A pid stays shielded until the LAST
# holder drops it.
_PROTECTED_PIDS: PidRefcount = PidRefcount()


_PROTECTED_LOCK = threading.Lock()


def register_protected_pid(pid: int) -> None:
    """Shield a live agent-process PID from the periodic orphan sweep.

    For app-managed worker pools whose processes are tracked in the PID file but
    not registered as SessionMap sessions. Pair every call with
    ``unregister_protected_pid`` on worker shutdown/replacement -- the shield is
    reference counted, so a second holder of the same pid takes its own
    reference and an unpaired call leaks one."""
    if isinstance(pid, int) and pid > 0:
        with _PROTECTED_LOCK:
            _PROTECTED_PIDS.add(pid)


def unregister_protected_pid(pid: int) -> None:
    """Drop ONE reference on a sweep-protected PID (worker shut down / replaced).

    The pid stops being shielded only when the last holder drops it, so a pool
    that replaces one of several workers on a shared process does not expose the
    process to the sweep."""
    with _PROTECTED_LOCK:
        _PROTECTED_PIDS.discard(pid)


def _protected_pids() -> set[int]:
    with _PROTECTED_LOCK:
        return set(_PROTECTED_PIDS)

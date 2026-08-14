"""Shared drain-and-restart primitives for applying updates (RFC §5).

The update-architecture RFC (`docs/request-for-change/rfc-update-architecture.md`,
§5 "Drain-then-swap") specifies one lifecycle for applying an update to a
running gateway: take a lease, stop starting new background work, wait for
in-flight work to finish (bounded), checkpoint, swap, restart, verify. Before
this module, each restart-after-update path did a different subset:

* ``SlackGateway._auto_apply_update`` (boot auto-apply) restarted immediately —
  no lease, no drain; in-flight turns died mid-prompt.
* ``POST /api/update`` (dashboard apply) likewise, and two concurrent POSTs
  raced each other's ``git pull`` + ``pip install`` with no serialization.
* The stale-asset watchdog (``dashboard/stale_asset_watchdog.py``) drained,
  but its in-flight counter missed cron executions and subagent runs — both
  die with the gateway (subagent kiro-cli processes are children of it).

This module extracts the three shared primitives so every path agrees:

``UpdateLease``
    "One update in flight, ever" (§5 step 2) as a filesystem lease that
    outlives the gateway process. The apply paths refuse to start while a
    live lease exists, and the boot path performs the §5 step-9 verification
    handshake — an update is only reported successful once the relaunched
    gateway confirms it is running a different version.

``drain_gate``
    A process-local flag marking "an update is draining" (§5 step 3 for
    *background* intake). ``CronService`` checks it before claiming due jobs
    and ``AutoNudgeService`` checks it before firing a loop — both defer, so
    the work is picked up after the restart instead of being started seconds
    before an intentional process swap. Interactive turn intake is
    deliberately NOT refused here: refusing inbound messages without a
    queue is the message-loss bug tracked in issue #2217, and closing it
    properly (queue + replay) is the remaining Phase-3 work in the RFC.

``drain_in_flight``
    The bounded wait (§5 step 5), generalized from the watchdog's private
    helper: poll a counter until it reaches zero, the deadline passes, or an
    external shutdown wins. Any failure to count is treated as idle — a
    broken predicate must never wedge a shutdown or an update.

The lease file lives in the gateway's ``run/`` directory (owner-only 0700,
same trust model as the run marker) so a supervisor or a second gateway
process can read it while the original process is gone during the swap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_LEASE_FILE = "update-lease.json"

# Defaults for the bounded in-flight drain used by the update paths. The
# stale-asset watchdog keeps its own (shorter) defaults — an asset vanish has
# already broken dashboard serving, so it drains with more urgency.
DEFAULT_DRAIN_TIMEOUT_SECS = 300.0
DEFAULT_DRAIN_POLL_SECS = 2.0


class _ShutdownSignal(Protocol):
    """Minimal contract needed from a shutdown-signalling event."""

    def is_set(self) -> bool:
        ...

    async def wait(self) -> bool:
        ...


# ---------------------------------------------------------------------------
# Drain gate — background-intake quiesce (§5 step 3)
# ---------------------------------------------------------------------------


class DrainGate:
    """Process-local "an update is draining" flag.

    Reference-counted: the dashboard apply path holds it across the whole
    pull/build/install while the shared restart helper takes it again for the
    final drain — nested holders must compose without the inner ``exit()``
    dropping the outer hold. Writers are the lease-holding apply/restart
    paths; readers (cron tick, autonudge fire) only ever need a synchronous
    ``is_draining()`` at their claim/fire choke points. Event-loop-confined,
    so no locking.

    ``drain_event`` lets long sleepers wake the moment a drain begins: a cron
    job in its jitter window (up to 59 min for daily jobs) waits on this
    event with its jitter as the timeout — when a drain starts it runs
    immediately instead of being killed mid-sleep by the exec after its
    scheduled minute has passed. Jitter is load-spreading, not semantics, and
    during a drain the herd it spreads does not exist.

    Note the scope asymmetry: this gate is PROCESS-LOCAL while the update
    lease is cross-process. A second gateway on the same data home keeps
    claiming its own cron jobs during this process's apply; the lease (which
    that gateway's own apply paths honor) is the cross-process boundary.
    """

    def __init__(self) -> None:
        self._depth = 0
        self._event: asyncio.Event | None = None

    def is_draining(self) -> bool:
        return self._depth > 0

    def drain_event(self) -> asyncio.Event:
        """The event set while draining (lazily created on the running loop)."""
        if self._event is None:
            self._event = asyncio.Event()
        return self._event

    def enter(self) -> None:
        self._depth += 1
        self.drain_event().set()

    def exit(self) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth == 0 and self._event is not None:
            self._event.clear()


#: The gateway-wide gate. ``cron.py`` and ``autonudge.py`` import this module
#: (a leaf — it imports neither) and consult the gate at their single
#: claim/fire choke points.
drain_gate = DrainGate()


# ---------------------------------------------------------------------------
# Bounded drain (§5 step 5)
# ---------------------------------------------------------------------------


async def drain_in_flight(
    shutdown_event: _ShutdownSignal,
    count_in_flight: Callable[[], int] | None,
    *,
    drain_timeout: float,
    drain_poll: float = DEFAULT_DRAIN_POLL_SECS,
    what: str = "update",
) -> bool:
    """Wait (bounded) for in-flight work to finish.

    Returns ``True`` when the count reached zero, ``False`` when the deadline
    elapsed with work still in flight or an external shutdown interrupted the
    wait. Callers proceed either way — the deadline exists precisely so a
    wedged turn cannot defer a restart forever (§5 step 5) — but the return
    value lets them log honestly which case occurred.

    Any failure to count is treated as "idle": a broken predicate must never
    wedge shutdown. Mirrors the defensive shape the stale-asset watchdog
    established.
    """
    if count_in_flight is None or drain_timeout <= 0:
        return True
    try:
        pending = count_in_flight()
    except Exception:
        logger.debug("%s drain: initial in-flight count failed — skipping", what, exc_info=True)
        return True
    if not isinstance(pending, int):
        # A counter returning a non-int (a Mock in duck-typed tests, a broken
        # accessor) is a broken predicate — treat as idle; it must never spin
        # the drain or crash the apply/shutdown path. NB: int() coercion is
        # NOT equivalent — Mock defines __int__, silently becoming a nonzero
        # constant that waits out the whole timeout.
        logger.debug("%s drain: non-integer in-flight count — treating as idle", what)
        return True
    if pending <= 0:
        return True

    logger.info(
        "%s drain: waiting for %d in-flight task(s) (up to %.0fs)…",
        what,
        pending,
        drain_timeout,
    )
    loop = asyncio.get_event_loop()
    deadline = loop.time() + drain_timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        # Sleep interruptibly: an external SIGTERM sets shutdown_event and
        # wakes us immediately — a real shutdown always outranks a drain.
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=min(drain_poll, remaining))
            logger.warning("%s drain: external shutdown during drain — stopping", what)
            return False
        except asyncio.TimeoutError:
            pass
        try:
            pending = count_in_flight()
        except Exception:
            logger.debug("%s drain: count failed mid-drain — treating as idle", what, exc_info=True)
            return True
        if not isinstance(pending, int):
            logger.debug("%s drain: non-integer count mid-drain — treating as idle", what)
            return True
        if pending <= 0:
            logger.info("%s drain: all in-flight work finished", what)
            return True

    logger.warning(
        "%s drain: timeout (%.0fs) elapsed with %d task(s) still in flight — "
        "proceeding; open sessions resume from snapshot on restart.",
        what,
        drain_timeout,
        pending,
    )
    return False


# ---------------------------------------------------------------------------
# Update lease (§5 steps 2–8) + post-restart verification (§5 step 9)
# ---------------------------------------------------------------------------


#: Construction grace for an unparsable lease file: younger than this, it may
#: be another process between its O_EXCL create and its json write — leave it.
_CORRUPT_LEASE_GRACE_SECS = 60.0


def self_path_mtime(path: Path) -> float:
    """Small seam for the lease-construction grace (patchable in tests)."""
    return path.stat().st_mtime


def current_head_commit() -> str:
    """Resolve the running install's HEAD SHA, '' when not a git checkout.

    This is the ONLY git invocation in the update chain — the async call
    sites go through :func:`current_head_commit_async` — so the binary-trust
    invariant lives in one place: ``git`` is resolved from fixed system
    directories via ``platform_compat.trusted_system_bin``, never from
    ``PATH``. A gateway's ``PATH`` can legitimately lead with agent-writable
    directories (a worktree venv's ``bin``), and this probe runs with the
    unsandboxed gateway's credentials, so a bare argv name would let a
    planted ``git`` shim execute arbitrary code. No trusted binary means no
    commit identity: return '' and let the handshake fall back to version
    comparison, its documented degradation.

    Sync subprocess — callers run off-loop (verification already runs inside
    ``asyncio.to_thread``). Best-effort: any failure returns '' and the
    handshake falls back to version comparison.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj or not os.path.exists(os.path.join(proj, ".git")):
        return ""
    git_bin = platform_compat.trusted_system_bin("git")
    if git_bin is None:
        return ""
    try:
        out = subprocess.run(
            [git_bin, "rev-parse", "HEAD"],
            cwd=proj,
            capture_output=True,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


async def current_head_commit_async() -> str:
    """Async twin of :func:`current_head_commit` for on-loop callers."""
    return await asyncio.to_thread(current_head_commit)


def _lease_path() -> Path:
    return Path(config_dir()) / "run" / _LEASE_FILE


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # platform_compat.pid_exists is the cross-OS probe. A raw
    # ``os.kill(pid, 0)`` is NOT usable here: on Windows any signal other
    # than CTRL_C_EVENT/CTRL_BREAK_EVENT is delivered via TerminateProcess,
    # so "checking" a live lease holder would kill it.
    try:
        return platform_compat.pid_exists(pid)
    except OverflowError:
        # A pid too large for the OS pid type cannot name a live process —
        # it is a malformed lease value, not a holder.
        return False


def _normalize_lease_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce the numeric lease fields; a payload that cannot coerce is corrupt.

    Readers do arithmetic on ``pid`` / ``acquired_at`` (holder liveness, lease
    age), so a hand-edited or torn lease carrying e.g. a string pid must
    degrade to the corrupt-lease path — refusal at acquire, grace-then-consume
    at boot verification — instead of raising out of update handling and
    leaving the lease permanently blocking updates. Non-finite values are
    rejected too: ``json.loads`` accepts ``Infinity``/``NaN``, which pass a
    bare ``float()`` but blow up age arithmetic.
    """
    for key, cast in (("pid", int), ("acquired_at", float), ("restart_at", float)):
        value = data.get(key)
        if value is None:
            continue
        try:
            coerced = cast(value)
        except (TypeError, ValueError, OverflowError):
            return {}
        if isinstance(coerced, float) and not math.isfinite(coerced):
            return {}
        data[key] = coerced
    return data


class UpdateLease:
    """Filesystem lease serializing update application across processes.

    States: ``draining`` (apply path is quiescing + swapping bytes) and
    ``restarting`` (the process is about to exec the new code; the next boot
    owns verification). The lease survives the process on purpose — with
    ``os.execv`` the PID persists across the swap, and with an external
    supervisor relaunch the original process is gone entirely; in both cases
    an in-memory flag would vanish or lie.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _lease_path()
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict[str, Any] | None:
        """Return the current lease payload, or None when absent/corrupt."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            logger.debug("update lease: unreadable at %s", self._path, exc_info=True)
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            # A corrupt lease is a crashed writer; treat as reclaimable.
            return {}
        if not isinstance(data, dict):
            return {}
        return _normalize_lease_fields(data)

    def acquire(self, *, from_version: str, source: str) -> str | None:
        """Try to take the lease. Returns None on success, else a refusal reason.

        ``O_CREAT | O_EXCL`` is the ONLY arbiter: any existing lease file is a
        refusal, full stop. There is deliberately no reclaim-by-unlink here —
        an unlink-then-create window lets two contenders both "reclaim" a
        stale file and both succeed, which is precisely the double-apply the
        lease exists to prevent. Stale leases are consumed ONLY through
        :func:`_consume_lease_if_unchanged` (cross-process lock + identity
        re-check): by :func:`verify_after_restart` at boot, and by the CLI
        update path for a dead holder's leftover before it retries its own
        acquire.

        Fails CLOSED: if the lease file cannot be created (unwritable data
        home, disk error), the update is refused rather than run unleased —
        an unserialized apply can corrupt the install, which is strictly
        worse than an update that reports an actionable environment error.
        """
        existing = self.read()
        if existing is not None:
            holder_pid = int(existing.get("pid") or 0)
            age = time.time() - float(existing.get("acquired_at") or 0)
            state = str(existing.get("state") or "corrupt")
            return (
                f"an update is already in flight (state={state}, "
                f"pid={holder_pid}, started {int(age)}s ago); if this is a "
                f"stale leftover it is cleared on the next gateway boot, or "
                f"remove {self._path} manually"
            )

        payload = {
            "pid": os.getpid(),
            "acquired_at": time.time(),
            "state": "draining",
            "from_version": from_version,
            "source": source,
        }
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return "an update is already in flight (lease appeared concurrently)"
        except OSError as exc:
            logger.error("update lease: cannot write %s (%s) — refusing update", self._path, exc)
            return f"cannot create the update lease at {self._path}: {exc}"
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError as exc:
            # The file exists but is unreadable garbage — remove OUR OWN
            # just-created file (exclusive create proves ownership) and refuse.
            logger.error("update lease: write failed at %s (%s) — refusing update", self._path, exc)
            try:
                self._path.unlink()
            except OSError:
                pass
            return f"cannot write the update lease at {self._path}: {exc}"
        self._held = True
        return None

    def mark_restarting(
        self, *, target: str = "", target_commit: str = "", expect_change: bool = True
    ) -> None:
        """Record that the swap is done and the restart is imminent (§5 step 8).

        The relaunched process finds this state and runs the verification
        handshake. ``target_commit`` is the post-swap HEAD SHA and is the
        PRIMARY identity on the git engine: ``__version__`` bumps rarely,
        while the git engine updates per-commit — a version-keyed handshake
        would report false failures for most successful pulls and vacuous
        successes when the version file didn't change. ``target`` (a version
        string) remains the fallback identity for engines without commits.
        ``expect_change=False`` marks a bare restart handoff (no bytes were
        swapped — e.g. ``POST /api/restart``): verification then consumes the
        lease silently. A failed handoff write RAISES rather than being
        swallowed: exec preserves the PID, so a lease stuck in 'draining'
        after a suppressed failure would 409 every later update/restart until
        the next boot — the caller must abort the restart and release.
        """
        if not self._held:
            return
        data = self.read() or {}
        data["state"] = "restarting"
        data["restart_at"] = time.time()
        data["expect_change"] = expect_change
        if target_commit:
            data["target_commit"] = target_commit
        if target:
            data["target"] = target
        try:
            # atomic_write = mkstemp (unpredictable name) + rename in the
            # lease's own directory. A deterministic sibling like
            # ``update-lease.tmp`` can be pre-created as a symlink by a local
            # attacker, turning this handoff write into an arbitrary-file
            # overwrite with the gateway's credentials; mkstemp's O_EXCL
            # random name makes that unreachable (module invariant: no
            # predictable temp paths, ever).
            atomic_write(self._path, json.dumps(data), mode=0o600)
        except OSError:
            # MUST propagate: exec preserves the PID, so a lease left in
            # 'draining' state after a swallowed handoff failure is
            # indistinguishable from a live apply — every later update and
            # restart would 409 until the next boot. Raising lets the caller
            # abort the restart and release the lease instead.
            logger.error("update lease: handoff write failed at %s", self._path)
            raise

    def release(self) -> None:
        """Drop the lease (apply failed or was refused before restart)."""
        if not self._held:
            return
        self._held = False
        try:
            self._path.unlink()
        except OSError:
            pass

    # -- async twins -------------------------------------------------------
    # Every lease operation touches the filesystem (read/mkdir/open/replace/
    # unlink). The apply paths run on the gateway's single event loop, and a
    # data home on a network mount or a stalled disk would freeze every chat
    # turn and the liveness heartbeat (the no-blocking-call-on-event-loop
    # rule). Async callers use these; the sync forms remain for boot-time and
    # test use off-loop.

    async def acquire_async(self, *, from_version: str, source: str) -> str | None:
        return await asyncio.to_thread(self.acquire, from_version=from_version, source=source)

    async def mark_restarting_async(
        self, *, target: str = "", target_commit: str = "", expect_change: bool = True
    ) -> None:
        await asyncio.to_thread(
            self.mark_restarting,
            target=target,
            target_commit=target_commit,
            expect_change=expect_change,
        )

    async def release_async(self) -> None:
        await asyncio.to_thread(self.release)


def _consume_lease_if_unchanged(lease: UpdateLease, snapshot: dict[str, Any]) -> bool:
    """Unlink the lease iff it still holds the payload the verdict was based on.

    The consume decision in :func:`verify_after_restart` is made from a read
    taken before slow work (a pid liveness probe, a git subprocess with a 10s
    timeout). By the time the unlink runs, a peer gateway on the same data
    home may have consumed the same dead lease and a new apply may have
    legitimately re-acquired the path via ``O_EXCL``. An unguarded path-unlink
    would then delete the LIVE lease — reopening the concurrent double-apply
    the lease exists to prevent. So consumption is serialized under a
    cross-process file lock and re-validated against the snapshot:

    * dict snapshot: unlink only when the re-read payload is identical — a
      re-acquired lease always differs (fresh pid + acquired_at).
    * corrupt snapshot (``{}``): unlink only when the file is still unparsable
      AND still past the construction grace — a fresh contender's mid-write
      file is young and is left alone.

    ``acquire`` itself needs no lock: its ``O_EXCL`` create only succeeds
    after an unlink, and every unlink is either serialized here or
    owner-scoped (``release`` / the failed-write cleanup act on a payload the
    same object just created exclusively). The lock file is deliberately never
    deleted — unlinking a lock file reintroduces the same race on the lock
    itself (a waiter blocked on the old inode and a fresh creator can both
    hold "the" lock at once).

    Returns True when this process consumed the lease, False when it was left
    alone (already consumed, re-acquired, still in grace, or lock unavailable
    — leaving a leftover for the next boot is strictly safer than an
    unserialized unlink).
    """
    lock_path = lease.path.with_name(lease.path.name + ".lock")
    try:
        # O_NOFOLLOW (where the platform has it) keeps the same symlink
        # discipline as the lease writes: a pre-planted symlink at the lock
        # path must fail the open, not silently redirect the lock (and the
        # O_CREAT) to an attacker-chosen target.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError:
        logger.warning("update lease: cannot open consume lock %s — leaving lease", lock_path)
        return False
    try:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            current = lease.read()
            if current is None:
                return False  # already consumed by a peer
            if snapshot:
                if current != snapshot:
                    return False  # re-acquired or rewritten since the verdict
            else:
                if current:
                    return False  # was corrupt, now parses: a new live lease
                try:
                    age = time.time() - self_path_mtime(lease.path)
                except OSError:
                    return False  # vanished under the lock
                if age < _CORRUPT_LEASE_GRACE_SECS:
                    return False  # fresh contender mid-write
            try:
                lease.path.unlink()
            except OSError:
                return False
            return True
    except OSError:
        # file_lock fails closed (Windows raises on a stuck holder): leaving
        # the lease for the next boot beats an unserialized unlink.
        logger.warning("update lease: consume lock unavailable at %s — leaving lease", lock_path)
        return False
    finally:
        os.close(lock_fd)


def verify_after_restart(current_version: str) -> str | None:
    """§5 step 9: the boot-time half of the update handshake.

    Called once at gateway startup. If a lease in ``restarting`` state exists,
    the previous process swapped bytes and restarted into us — report whether
    the swap actually took (version changed / matches target) and clear the
    lease. Returns a human-readable outcome line for the caller to surface,
    or None when there was no restart to verify.

    Without this, "update succeeded" means "we started something", not "the
    new version is serving" — the exact omission §5 calls the most expensive.
    """
    lease = UpdateLease()
    data = lease.read()
    if data is None:
        return None
    if not data:
        # Empty/corrupt payload. This is what a lease looks like in the
        # microsecond window between another process's O_EXCL create and its
        # json.dump — unlinking it here would let a third contender acquire
        # the same path and run a concurrent apply. Give it a construction
        # grace; only garbage that stays unparsable past the grace is a
        # crashed writer's leftover and gets consumed.
        try:
            age = time.time() - self_path_mtime(lease.path)
        except OSError:
            return None  # vanished under us — nothing to verify
        if age < _CORRUPT_LEASE_GRACE_SECS:
            logger.debug(
                "update lease: unparsable but %.1fs young — leaving it (may be mid-write)", age
            )
            return None
        logger.warning("update lease: unparsable and stale (%.0fs) — consuming", age)
        _consume_lease_if_unchanged(lease, {})
        return None
    state = str(data.get("state") or "")
    holder = int(data.get("pid") or 0)
    holder_alive = _pid_alive(holder)
    # Holder-aware consumption. This is the ONLY place leases are consumed
    # (acquire never reclaims), so it must not touch a lease that is still
    # someone's live serialization token:
    #  * live pid in ANOTHER process = a second gateway on the same data home
    #    (e.g. --port 9999) is mid-apply right now;
    #  * OUR pid in 'draining' state = an apply already in flight in this
    #    process (the dashboard accepts POST /api/update before this boot
    #    task runs).
    # Only a dead holder's leftover, or our own 'restarting' handoff
    # (os.execv preserves the PID), is consumed here.
    if holder_alive and holder != os.getpid():
        logger.info(
            "update lease: held by live pid %d (state=%s) — leaving it to its owner",
            holder,
            state or "corrupt",
        )
        return None
    outcome: str | None = None
    if state == "restarting":
        prior = str(data.get("from_version") or "")
        target = str(data.get("target") or "")
        target_commit = str(data.get("target_commit") or "")
        current_commit = current_head_commit()
        if not data.get("expect_change", True):
            # Bare-restart handoff (POST /api/restart): no bytes were swapped,
            # so an unchanged version is the expected outcome — consume
            # silently rather than reporting a failed update.
            logger.info("Gateway restart handoff consumed (no update expected).")
        elif target_commit and current_commit:
            # PRIMARY identity on the git engine: updates are per-commit while
            # __version__ bumps rarely — a version-keyed handshake reports
            # false failures for most successful pulls and vacuous successes
            # when the version file didn't change.
            if current_commit == target_commit:
                outcome = (
                    f"Update verified: now at {current_commit[:9]} "
                    f"(running {current_version})."
                )
            else:
                outcome = (
                    f"Update did NOT take effect: running {current_commit[:9]} "
                    f"but the update installed {target_commit[:9]} — check the "
                    f"update logs."
                )
                logger.critical("%s", outcome)
        elif target and current_version == target:
            outcome = f"Update verified: now running {current_version} (was {prior})."
        elif prior and current_version != prior:
            outcome = f"Update verified: now running {current_version} (was {prior})."
        else:
            outcome = (
                f"Update did NOT take effect: still running {current_version} "
                f"after a restart that expected a new version"
                + (f" ({target})" if target else "")
                + " — check the update logs."
            )
            logger.critical("%s", outcome)
    elif state == "draining":
        if holder_alive:
            # Our own pid, still draining: an apply is in flight in THIS
            # process right now — not a leftover, do not consume.
            return None
        # Dead holder mid-apply (crash or supervisor kill during the swap).
        # The tree may be half-updated.
        outcome = (
            "Previous update was interrupted mid-apply (lease was still in "
            "'draining' state at boot) — the install may be inconsistent; "
            "re-run the update to converge."
        )
        logger.warning("%s", outcome)
    # A consumed handoff, a dead holder's leftover, or a corrupt lease: the
    # restart owns it either way — but only while the file still holds the
    # exact payload this verdict was computed from. If a peer consumed it (or
    # a new apply re-acquired the path) since our read, the verdict belongs to
    # that peer: consume nothing and report nothing.
    if not _consume_lease_if_unchanged(lease, data):
        return None
    return outcome

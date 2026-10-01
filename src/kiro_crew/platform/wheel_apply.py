"""One way to run the managed-venv shadow apply, for every caller.

:mod:`kiro_crew.platform.wheel_engine` does the work and is synchronous. This
module holds what each caller of it would otherwise copy:

* the preflight, in one order: the policy source pin on both CDN bases, then the
  CDN base shape (:func:`preflight_bases`);
* the memory copy around promotion: readiness checked before anything is
  downloaded (:func:`check_memory_ready`), the copy itself taken just before the
  stable link flips (:func:`memory_snapshot_hook`);
* the AppArmor question an unattended apply must ask before it moves the
  launcher a userns profile is attached to (:func:`userns_reattach_needed`);
* the check every caller runs before it restarts onto a promotion, so a restart
  never re-runs the version already running (:func:`restart_reaches`), and the
  one the unattended apply runs before it builds, so a gateway that loads its
  code through the stable link moves off it first (:func:`relaunch_before_apply`);
* the one classification of how an apply ended (:func:`classify`), which the
  CLI uses too;
* for the gateway's callers (the update coordinator and
  ``POST /api/update/approve``): the worker the apply runs on, progress pushes,
  the overall deadline, and the record of applies in flight that a shutdown or a
  process replacement stops first (:func:`run_wheel_apply`,
  :func:`cancel_wheel_applies`, :func:`stop_wheel_applies`).

The record of applies in flight is process-wide on purpose: the update lock
admits one writer per layout, and the process that exits or execs is the one
whose applies must stop. Every exec seam, :func:`platform_compat.hard_exit` and
the gateway's shutdown reach :func:`cancel_wheel_applies` through
:func:`platform_compat.cancel_wheel_applies_in_flight`, which finds this module
in ``sys.modules`` when it is loaded and does nothing when it is not.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kiro_crew import platform_compat

# The module, not its names: every engine call resolves at call time, so a test
# (or a caller) that patches ``wheel_engine`` patches this path too.
from kiro_crew.platform import wheel_engine
from kiro_crew.platform.wheel_engine import (
    FAILURE_TEXT_CHARS,
    ApplyCancel,
    WheelUpdateBusy,
    WheelUpdateCancelled,
    WheelUpdateError,
    WheelUpdateIncompatible,
    WheelUpdateNotReady,
    WheelUpdateSnapshotFailed,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Wall-clock bound on one gateway-side apply, counted from the moment it holds
#: the update lock and enforced through the apply's cancel, so it kills a build
#: child exactly the way a shutdown does. The steps' own bounds add up to far
#: more (the dependency install alone may take 900 s); this caps their sum.
APPLY_DEADLINE_SECS = 1800.0

#: How long a shutdown, or a cancelled caller, waits for a cancelled apply to
#: unwind. Setting the cancel kills a build child and shuts a download's socket
#: at once; what is left to wait for is the step in progress noticing (a poll
#: interval) and the partial tree being renamed aside.
STOP_GRACE_SECS = 3.0


class WheelApplyRefused(Exception):
    """The preflight refused the apply; nothing was fetched."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def preflight_bases() -> tuple[str, str]:
    """``(feed base, artifact base)`` once both pass the preflight, in one order.

    The policy source pin first, on the feed base and then the artifact base:
    a pinned fleet's policy decides where this host may take code from. Then
    the shape check on ``KIROCREW_CDN_BASE``, which is operator-set.
    """
    from kiro_crew.platform.update_governance import update_blocked_reason
    from kiro_crew.platform.update_layout import cdn_bases, cdn_bases_are_safe

    feed_base, artifact_base = cdn_bases()
    blocked = update_blocked_reason(feed_base) or update_blocked_reason(artifact_base)
    if blocked:
        raise WheelApplyRefused("blocked_by_policy", blocked)
    if not cdn_bases_are_safe():
        raise WheelApplyRefused(
            "bad_cdn", "CDN base URL contains disallowed characters or is not HTTPS"
        )
    return feed_base, artifact_base


# ── The memory copy ─────────────────────────────────────────────────────────


def check_memory_ready() -> None:
    """Refuse, before anything is downloaded, an update no memory copy could cover.

    Raises :class:`WheelUpdateNotReady` while this process's memory startup is
    still preparing (retry shortly), and :class:`WheelUpdateSnapshotFailed` for
    a structural startup failure (with the fence's own message) or an active
    store whose file cannot be resolved. A store that failed its own startup is
    NOT a refusal: it gets an unverified raw copy at promotion
    (:func:`snapshot_memory_before_update`).
    """
    from kiro_crew.memory_startup import (
        MemoryStartupUnavailable,
        memory_startup_preparing,
        require_memory_prepared,
    )
    from kiro_crew.memory_stores import active_store_names, owned_store_path

    if memory_startup_preparing():
        raise WheelUpdateNotReady("memory is still being prepared; the update retries shortly")
    try:
        require_memory_prepared()
    except MemoryStartupUnavailable as exc:
        raise WheelUpdateSnapshotFailed(str(exc)) from exc
    missing = [name for name in active_store_names() if owned_store_path(name) is None]
    if missing:
        raise WheelUpdateSnapshotFailed(
            f"{len(missing)} memory store(s) cannot be resolved to a file, so no copy of "
            "them can be taken. Repair them first: kirocrew memory backups"
        )


def _raw_copy(db_path: Path) -> bool:
    """Copy a store's files byte for byte, unverified, into ``backups/unverified/``.

    For a store this process could not open at startup: there is no integrity
    gate to pass, but a copy of what is on disk still exists before the new
    version runs against it. Kept apart from the verified backups, so restore
    and retention never treat it as one.
    """
    from kiro_crew.memory_backup import backup_dir_for

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = backup_dir_for(db_path) / "unverified"
    try:
        platform_compat.make_owner_only_dir(out_dir)
        copied = False
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{db_path}{suffix}")
            dest = out_dir / f"{db_path.stem}.{stamp}{db_path.suffix}{suffix}"
            try:
                if _copy_own_regular_file(source, dest):
                    copied = True
            except _RedirectedInput:
                logger.warning(
                    "refusing an unverified copy of %s: it is not a plain file of the store",
                    source,
                )
                return False
        return copied
    except OSError:
        logger.warning("could not take an unverified copy of %s", db_path, exc_info=True)
        return False


class _RedirectedInput(Exception):
    """A store file that is a link, or not a regular file, so it may name another file."""


def _copy_own_regular_file(source: Path, dest: Path) -> bool:
    """Copy *source* to a new *dest* without following anything it points at.

    The memory directory is writable from inside the agent sandbox and this runs
    outside it, so a store file there may be a planted link to a file the
    sandbox masks (``.env``). The source is opened ``O_NOFOLLOW`` and must be a
    regular file with one link; the destination is created ``O_EXCL``. Returns
    ``False`` when *source* does not exist; raises :class:`_RedirectedInput` for
    a link, a hard link, or anything that is not a regular file.
    """
    try:
        st = os.lstat(source)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise _RedirectedInput(str(source))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(source, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _RedirectedInput(str(source)) from exc
        raise
    with os.fdopen(fd, "rb") as src:
        opened = os.fstat(src.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)
        ):
            raise _RedirectedInput(str(source))
        out_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        with os.fdopen(os.open(dest, out_flags, 0o600), "wb") as out:
            shutil.copyfileobj(src, out)
    return True


def snapshot_memory_before_update() -> tuple[int, str]:
    """Copy every memory store now; return ``(stores copied, failure or "")``.

    The retention interval is bypassed (``force=True``) and ``memory.backup_keep``
    is passed, so a configured history longer than the sweep's default is kept.
    A store that failed this process's memory startup gets an unverified raw
    copy instead of the verified backup it cannot have, with a WARNING.
    """
    from kiro_crew import memory_backup
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.memory_startup import memory_store_startup_error
    from kiro_crew.memory_stores import active_store_names, owned_store_path

    try:
        keep = int(KiroCrewConfig.load().memory.backup_keep)
        snapshot = memory_backup.back_up_all_stores(keep, force=True)
    except Exception as exc:  # noqa: BLE001 - any failure refuses the update
        return 0, str(exc) or exc.__class__.__name__
    failed, copied = int(snapshot["failed"]), int(snapshot["backed_up"])
    for name in active_store_names():
        path = owned_store_path(name)
        if path is None or not memory_store_startup_error(name):
            continue
        if _raw_copy(path):
            logger.warning(
                "memory store %r failed its startup; took an UNVERIFIED raw copy of %s "
                "before the update",
                name,
                path,
            )
            failed, copied = failed - 1, copied + 1
    return copied, (f"{failed} store(s) not copied" if failed > 0 else "")


def memory_snapshot_hook(progress: Callable[[str], None]) -> Callable[[], None]:
    """The engine's ``before_promote`` step: copy memory, or refuse the promotion."""

    def _snapshot() -> None:
        from kiro_crew import memory_backup

        copied, failure = snapshot_memory_before_update()
        if failure:
            raise WheelUpdateSnapshotFailed(
                f"Pre-update memory snapshot failed: {failure}. Not updating: the store "
                "would be rewritten with no fresh copy of it. Check the log for the "
                "reason, then repair it: kirocrew memory backups"
            )
        newest = memory_backup.newest_backup()
        where = f"; default store copies in {newest.parent}" if newest is not None else ""
        progress(f"Memory snapshot: {copied} store(s) copied{where}")

    return _snapshot


# ── What the operator is told ───────────────────────────────────────────────


def _redacted(text: str) -> str:
    """*text* through the context redactor, for output an operator is shown.

    ``redact_via_context`` applies a loaded companion's patterns too. Where the
    companion could not be composed it refuses rather than downgrade, and the
    log-safe spelling then withholds the text instead of raising out of a
    failure path.
    """
    from kiro_crew.platform.context import redact_log_via_context, redact_via_context

    try:
        return redact_via_context(text)
    except Exception:  # noqa: BLE001 - a failure path must not raise
        return redact_log_via_context(text)


def installer_rerun_command(channel: str) -> str:
    """The installer re-run that moves this install where the engine cannot.

    Redacted: the command embeds the CDN base, which an operator override may
    give credentials.
    """
    from kiro_crew.platform.update_layout import wheel_update_command

    return _redacted(wheel_update_command(channel))


def shown_failure_text(text: str, limit: int | None = FAILURE_TEXT_CHARS) -> str:
    """Failure text for an operator: redacted in full, then (when *limit*) capped.

    The cap keeps the step that failed (everything before the first ``": "``)
    and the TAIL of the detail, where pip and venv put their final ``ERROR:``
    line, never the oldest part of a long log. Shared by every surface that
    shows it: the dashboard step, the ERROR log line, the approve audit, and
    ``kirocrew update``'s own print.
    """
    full = _redacted(text)
    logger.debug("Wheel update failure detail: %s", full)
    if limit is None or len(full) <= limit:
        return full
    head_end = full.find(": ")
    head = full[: head_end + 2] if 0 <= head_end < limit // 3 else ""
    return head + "…" + full[-(limit - len(head) - 1) :]


def userns_reattach_needed(version: str) -> bool:
    """Would promoting *version* move the launcher a userns profile applies to?

    On a host that restricts unprivileged user namespaces, ``kirocrew service
    install`` attaches the ``kirocrew-userns`` AppArmor profile to the RESOLVED
    launcher path, and promotion makes that path resolve into the new tree. The
    running gateway keeps its label, but the next fresh service start would run
    unconfined and the agent sandbox could not be built. True only when the
    profile applies to the launcher today (the same predicate ``kirocrew doctor``
    reports) and the promoted one differs.
    """
    if not sys.platform.startswith("linux"):
        return False
    from kiro_crew.service import apparmor
    from kiro_crew.service import linux as service_linux

    if not (apparmor.apparmor_is_active() and apparmor.userns_restricted()):
        return False
    attached = apparmor.service_profile_attachment(
        service_linux.kirocrew_bin(), service_linux.UNIT_PATH
    )
    if attached is None:
        return False
    layout = wheel_engine.managed_venv_layout()
    promoted = os.path.realpath(layout.versioned_tree(version) / "bin" / "kirocrew")
    return promoted != attached


def userns_reattach_remedy(version: str) -> str:
    """What the operator runs when :func:`userns_reattach_needed` holds."""
    return (
        f"Kiro Crew {version} is ready. Applying it moves the launcher the kirocrew-userns "
        "AppArmor profile is attached to, so the sandbox profile would stop applying at "
        "the next service start. Run `kirocrew update`, then `kirocrew service install` "
        "(which re-attaches the profile and restarts the service onto the new version). "
        "Both are needed after every update while that profile is in use; `kirocrew "
        "doctor` reports the attachment under Sandbox."
    )


def userns_reattach_after_apply(version: str) -> str:
    """What the operator runs when *version* was promoted while that profile applies."""
    return (
        f"Kiro Crew {version} is installed, and the kirocrew-userns AppArmor profile is "
        "still attached to the previous launcher, so the sandbox profile stops applying "
        "at the next service start. Run `kirocrew service install`, which re-attaches "
        "the profile and restarts the service onto the new version. `kirocrew doctor` "
        "reports the attachment under Sandbox."
    )


def restart_reaches(version: str) -> bool:
    """Would a restart now exec the tree *version* was just promoted into?

    :func:`wheel_engine.respawn_executable` falls back to ``sys.executable``
    whenever the stable link cannot carry a restart (a missing target, a target
    outside the layout, no interpreter). After a promotion that fallback is not
    an update: it starts the version already running, whose next update cycle
    promotes and restarts again, for ever. Compared by the interpreter's
    directory, resolved, against the versioned tree the engine promotes.
    """
    target = Path(wheel_engine.respawn_executable()).parent
    promoted = wheel_engine.managed_venv_layout().versioned_tree(version) / "bin"
    return os.path.realpath(target) == os.path.realpath(promoted)


def relaunch_before_apply() -> bool:
    """Must this process restart onto its resolved tree before an apply builds?

    A gateway an earlier version restarted as ``crew-venv-current/bin/python3``
    loads its code through the stable link
    (:func:`wheel_engine.runs_through_stable_link`). Promoting under it while it
    keeps serving, which a busy gateway's deferred restart allows, would load the
    new version's modules into the old process. A restart onto
    :func:`wheel_engine.respawn_executable` (the link's resolved tree, the same
    version) moves it off the link first. ``False`` when that restart would
    land on the link again, because it would then repeat on every cycle.
    """
    if not wheel_engine.runs_through_stable_link():
        return False
    target = Path(wheel_engine.respawn_executable()).parent.parent
    return not wheel_engine.runs_through_stable_link(str(target))


def restart_unreachable_remedy(version: str, channel: str) -> str:
    """What the operator is told when :func:`restart_reaches` is false."""
    return (
        f"Kiro Crew {version} was installed, but a restart would start the running "
        "version again, because the crew-venv-current link does not lead to the new "
        "tree. The gateway is not restarting. Re-run the installer, which repoints "
        f"the link: {installer_rerun_command(channel)}"
    )


def incompatible_remedy(channel: str) -> str:
    """What the operator is told for an ``incompatible`` outcome.

    The engine builds on the interpreter it runs on and cannot provision another,
    so only the installer moves this install onto a Python the release accepts
    (``docs/build/release.md``, "Raising the Python floor").
    """
    return (
        "Re-run the installer, which provisions the Python series it pins: "
        f"{installer_rerun_command(channel)}"
    )


# ── How an apply ended ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class WheelApplyOutcome:
    """How one apply ended.

    ``status`` is one of ``promoted``, ``busy`` (another apply holds the update
    lock, or one is already running in this process; nothing was touched),
    ``deferred`` (a precondition is still settling; retry shortly),
    ``incompatible`` (the signed release metadata excludes this host),
    ``snapshot_failed`` (the memory copy did not land, so nothing was promoted),
    ``cancelled`` (stopped by a shutdown or a process replacement), ``timed_out``
    (:data:`APPLY_DEADLINE_SECS`) or ``failed``. ``message`` is redacted and
    capped, ready to show.
    """

    status: str
    message: str = ""


#: Outcomes the gateway retries on its short cadence rather than the check interval.
RETRY_SOON = frozenset({"busy", "deferred", "cancelled"})


def classify(exc: BaseException | None) -> WheelApplyOutcome:
    """The one mapping from how the engine ended to an outcome. ``None`` = promoted."""
    if exc is None:
        return WheelApplyOutcome("promoted")
    if isinstance(exc, WheelUpdateBusy):
        return WheelApplyOutcome("busy", str(exc))
    if isinstance(exc, WheelUpdateNotReady):
        return WheelApplyOutcome("deferred", str(exc))
    if isinstance(exc, WheelUpdateIncompatible):
        return WheelApplyOutcome("incompatible", shown_failure_text(str(exc)))
    if isinstance(exc, WheelUpdateCancelled):
        if exc.reason == "deadline":
            return WheelApplyOutcome(
                "timed_out",
                f"the update did not finish within {APPLY_DEADLINE_SECS / 60:.0f} minutes "
                "and was stopped; the current install is unchanged",
            )
        return WheelApplyOutcome("cancelled", str(exc))
    if isinstance(exc, WheelUpdateSnapshotFailed):
        return WheelApplyOutcome("snapshot_failed", shown_failure_text(str(exc)))
    if isinstance(exc, WheelUpdateError):
        return WheelApplyOutcome("failed", shown_failure_text(str(exc)))
    # A staging-filesystem error the engine does not wrap (a full or unwritable
    # disk at mkdir/tempdir time) surfaces raw.
    logger.error("Wheel update failed unexpectedly", exc_info=exc)
    return WheelApplyOutcome("failed", "the update failed unexpectedly — check the log")


# ── Applies in flight (the gateway's callers) ───────────────────────────────


@dataclass(eq=False)
class _Running:
    cancel: ApplyCancel
    future: asyncio.Future[Path]


_IN_FLIGHT: set[_Running] = set()


def applies_in_flight() -> int:
    """How many applies this process is running (in-flight work for a restart gate)."""
    return len(_IN_FLIGHT)


def cancel_wheel_applies(reason: str = "shutdown") -> None:
    """Cancel every apply this process is running. Synchronous and quick.

    Setting an apply's cancel kills its build child and shuts a download's
    socket before returning, so this is safe right before the process ends.
    Every exit path reaches it through
    :func:`platform_compat.cancel_wheel_applies_in_flight` (see the module
    docstring).
    """
    for running in list(_IN_FLIGHT):
        running.cancel.set(reason)


async def stop_wheel_applies() -> None:
    """Cancel every apply in flight and wait up to :data:`STOP_GRACE_SECS` for each.

    A cancellation of the caller ends the wait and propagates: the applies were
    already told to stop, and a shutdown's own deadline must never be lost.
    """
    running = list(_IN_FLIGHT)
    cancel_wheel_applies()
    pending = {entry.future for entry in running if not entry.future.done()}
    if pending:
        await asyncio.wait(pending, timeout=STOP_GRACE_SECS)


def _retrieve(future: asyncio.Future[Path]) -> None:
    # Always consume the result: a caller whose grace ran out stops awaiting
    # the future, and an unretrieved exception is logged as a crash.
    if not future.cancelled():
        future.exception()


async def run_wheel_apply(
    *,
    channel: str,
    version: str,
    feed_base: str,
    artifact_base: str,
    state: DashboardState | None = None,
    held_lock_fd: int | None = None,
) -> WheelApplyOutcome:
    """Run one shadow apply on the update worker and report how it ended.

    ``busy`` at once, without queueing, when this process already runs one,
    unless the caller holds the update lock (*held_lock_fd*, a lock it took with
    :func:`wheel_engine.hold_update_lock`): every other apply then loses on that
    lock, so the holder's apply is the one that runs. Progress reaches the
    dashboard only once the update lock is held, so a caller that loses the lock
    to another apply pushes nothing onto that apply's feed; the deadline starts
    then too. A held lock is released when the apply ends, or once its worker
    finishes when the cancel grace ran out first. A cancel of the awaiting task
    stops the apply (killing its build child at once) and waits up to
    :data:`STOP_GRACE_SECS` for it to unwind before re-raising.
    """
    from kiro_crew.executors import update_executor

    # An entry whose loop is gone can never be awaited again; it is not an
    # apply in flight.
    _IN_FLIGHT.difference_update(
        {entry for entry in _IN_FLIGHT if entry.future.get_loop().is_closed()}
    )
    if _IN_FLIGHT and held_lock_fd is None:
        return WheelApplyOutcome("busy", "an update is already being applied in this process")
    loop = asyncio.get_running_loop()
    cancel = ApplyCancel()
    expiry: list[asyncio.TimerHandle] = []

    def _push(step: str, message: str) -> None:
        # Called from the worker thread; pushed on the serving loop.
        if state is not None:
            loop.call_soon_threadsafe(state.push_update_progress, step, message)

    def _arm_deadline() -> None:
        expiry.append(loop.call_later(APPLY_DEADLINE_SECS, cancel.set, "deadline"))

    def _on_locked() -> None:
        loop.call_soon_threadsafe(_arm_deadline)
        _push("pulling", f"Applying update to v{version}…")

    call = functools.partial(
        wheel_engine.apply_wheel_update,
        channel=channel,
        feed_base=feed_base,
        artifact_base=artifact_base,
        expected_version=version,
        progress=lambda message: _push("building", message),
        cancel=cancel,
        on_locked=_on_locked,
        preflight=check_memory_ready,
        before_promote=memory_snapshot_hook(lambda message: _push("building", message)),
        held_lock_fd=held_lock_fd,
    )

    def _apply_and_release() -> Path:
        try:
            return call()
        finally:
            # The worker owns the descriptor through its last filesystem step,
            # even if the caller's cancel grace expires or its loop closes.
            if held_lock_fd is not None:
                wheel_engine.release_update_lock(held_lock_fd)

    try:
        future: asyncio.Future[Path] = loop.run_in_executor(update_executor(), _apply_and_release)
    except BaseException:
        # Submission failed, so no worker took ownership of the held lock.
        if held_lock_fd is not None:
            await asyncio.shield(asyncio.to_thread(wheel_engine.release_update_lock, held_lock_fd))
        raise
    future.add_done_callback(_retrieve)
    running = _Running(cancel, future)
    _IN_FLIGHT.add(running)
    try:
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError:
            cancel.set("shutdown")
            await asyncio.wait({future}, timeout=STOP_GRACE_SECS)
            if not future.done():
                logger.warning(
                    "Wheel update to %s cancelled; it was still unwinding when the wait ended",
                    version,
                )
            elif not future.cancelled() and future.exception() is None:
                logger.warning(
                    "Wheel update to %s cancelled after promotion; the next start runs it",
                    version,
                )
            else:
                logger.warning(
                    "Wheel update to %s cancelled before promotion; the running install "
                    "is unchanged",
                    version,
                )
            raise
    finally:
        for handle in expiry:
            handle.cancel()
        _IN_FLIGHT.discard(running)
    outcome = classify(None if future.cancelled() else future.exception())
    if outcome.status in ("failed", "timed_out", "incompatible", "snapshot_failed"):
        logger.error("Wheel update to %s %s: %s", version, outcome.status, outcome.message)
    elif outcome.status != "promoted":
        logger.info("Wheel update to %s %s: %s", version, outcome.status, outcome.message)
    return outcome


__all__ = [
    "APPLY_DEADLINE_SECS",
    "RETRY_SOON",
    "STOP_GRACE_SECS",
    "WheelApplyOutcome",
    "WheelApplyRefused",
    "applies_in_flight",
    "cancel_wheel_applies",
    "check_memory_ready",
    "classify",
    "incompatible_remedy",
    "installer_rerun_command",
    "memory_snapshot_hook",
    "preflight_bases",
    "relaunch_before_apply",
    "restart_reaches",
    "restart_unreachable_remedy",
    "run_wheel_apply",
    "shown_failure_text",
    "snapshot_memory_before_update",
    "stop_wheel_applies",
    "userns_reattach_after_apply",
    "userns_reattach_needed",
    "userns_reattach_remedy",
]

"""Starting a dashboard slot's session before its first message: the eager spawn's and the
resume prefetch's backoff, admission caps and TTL."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kiro_crew import resource_status

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        CapabilityError,
        CapabilityStartupError,
        _ChatSlot,
        logger,
        redact_and_truncate,
    )

# Debounce before a speculative spawn. Absorbs rapid consecutive signals
# (slot create immediately followed by a project set, or a user re-picking
# the project) so only the settled state spawns a session.
_EAGER_SPAWN_DEBOUNCE_SECS = 1.5


# Background starts of a slot whose agent keeps failing to start. Each failure
# spawns and tears down a whole process tree, and the signals that schedule an
# eager spawn (focus, reconnect, slot create, reset) fire again and again, so an
# unbounded retry turns one broken start into a steady churn of processes. After
# each failure, every signal inside a backoff window is skipped: nothing is
# queued for later, and the first signal after the window starts normally. With
# a cap of 3 the reachable windows are 10s after the first failure and 20s after
# the second (the doubling and its 300s ceiling bound a larger cap). After the cap the slot stops starting in the background and says so once (one ERROR
# log line and one error row in the chat). From then on background starts stay
# OFF for that slot until a start succeeds -- the user's own next message still
# starts it, and that success clears the count -- or the gateway restarts: the
# count lives only on the in-memory slot, so a restart starts it again at zero.
#
# Only a failure of the agent START counts: an exception out of the session
# allocation in ``_spawn_admitted_prefetch``. A failed pending-reset consume, a
# binding/selection write, the admission bookkeeping around it, or a capability
# refusal raised before any process ran (``_is_pre_spawn_refusal``) spawned no
# agent process, so it is logged and does not move the slot toward the cap.
_EAGER_SPAWN_FAILURE_CAP = 3


_EAGER_SPAWN_BACKOFF_BASE_SECS = 10.0


_EAGER_SPAWN_BACKOFF_MAX_SECS = 300.0


#: Bound on the last-error text the stop notice carries into the chat.
_EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS = 500


def _eager_spawn_backoff_secs(failures: int) -> float:
    """Seconds the next background start waits after *failures* in a row."""
    if failures <= 0:
        return 0.0
    return min(
        _EAGER_SPAWN_BACKOFF_BASE_SECS * (2 ** min(failures - 1, 16)),
        _EAGER_SPAWN_BACKOFF_MAX_SECS,
    )


def _eager_spawn_held_off(slot: "_ChatSlot") -> bool:
    """Whether *slot*'s failed starts hold its next background start back."""
    failures = getattr(slot, "_eager_spawn_failures", 0)
    if failures >= _EAGER_SPAWN_FAILURE_CAP:
        return True
    return time.monotonic() < getattr(slot, "_eager_spawn_retry_at", 0.0)


def _note_eager_spawn_failure(slot: "_ChatSlot", exc: BaseException) -> None:
    """Count a failed background start; at the cap, stop and tell the user."""
    failures = getattr(slot, "_eager_spawn_failures", 0) + 1
    slot._eager_spawn_failures = failures
    slot._eager_spawn_retry_at = time.monotonic() + _eager_spawn_backoff_secs(failures)
    if failures > _EAGER_SPAWN_FAILURE_CAP:
        # Already stopped and already said so. ``schedule_eager_spawn`` refuses a
        # slot at the cap, so this is only a spawn that was in flight when the
        # cap was reached; the stop is announced once, not per late failure.
        logger.debug("Eager spawn: slot %s failed again after the stop", slot.key)
        return
    if failures < _EAGER_SPAWN_FAILURE_CAP:
        logger.warning(
            "Eager spawn: slot %s failed to start (%d/%d); next background start " "in %.0fs",
            slot.key,
            failures,
            _EAGER_SPAWN_FAILURE_CAP,
            _eager_spawn_backoff_secs(failures),
        )
        return
    logger.error(
        "Eager spawn: slot %s failed to start %d times in a row; background starts "
        "stay off until a start succeeds or the gateway restarts",
        slot.key,
        failures,
    )
    # Redacted over the whole text, then bounded: an agent error can carry a
    # near-frame-limit payload, and this row is retained and broadcast.
    detail = redact_and_truncate(str(exc), _EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS)
    try:
        slot.append(
            "error",
            f"This chat's agent failed to start {failures} times in a row, so it "
            "will not be started again in the background. Send a message to try "
            f"again. Last error: {detail or type(exc).__name__}",
            "msg msg-err",
        )
    except Exception:
        logger.debug("Eager spawn: could not post the stop notice for %s", slot.key, exc_info=True)


#: ``CapabilityStartupError`` codes raised only before any agent process exists:
#: the member, its enrollment state or its prepared generation cannot be read, or
#: the provider cannot host it. ``capability_runtime_unverified`` is raised only
#: after a process ran. ``capability_runtime_cwd_changed`` and
#: ``capability_startup_raced`` come from ``verify_saved``, which runs both before
#: the start and after it, so the code alone cannot say whether a process ran;
#: they are left counted, the side that keeps a churning start bounded.
_PRE_SPAWN_CAPABILITY_CODES = frozenset(
    {
        "capability_member_missing",
        "capability_state_unreadable",
        "capability_generation_missing",
        "capability_harness_unsupported",
        "capability_tool_limits_unsupported",
        "capability_runtime_not_fresh",
    }
)


def _is_pre_spawn_refusal(exc: BaseException) -> bool:
    """Whether *exc* refused a background start before any agent process ran.

    A ``CapabilityError`` is a refusal of the member's capability spec, which
    the user fixes in Capabilities rather than by retrying; counting it would
    end in a notice that blames a failed start. Pre-spawn capability startup
    codes are the same kind of refusal. Neither spawns a process, so neither
    feeds the churn the background-start cap bounds.
    """
    if isinstance(exc, CapabilityError):
        return True
    return isinstance(exc, CapabilityStartupError) and str(exc) in _PRE_SPAWN_CAPABILITY_CODES


def _clear_eager_spawn_failures(slot: "_ChatSlot") -> None:
    """Forget *slot*'s failed starts once one start succeeded."""
    slot._eager_spawn_failures = 0
    slot._eager_spawn_retry_at = 0.0


# How long a speculatively RESUMED session may sit unclaimed before it is
# torn down. A resumed session holds kiro-cli's native per-session lock, so
# a prefetch the user walked away from must release it cleanly rather than
# wait out the 30-minute idle sweep. Fresh (non-resumed) eager sessions hold
# no prior transcript's lock, so they skip the TTL and are reaped by the idle
# sweep — but they still count against the live-population cap below.
_RESUME_PREFETCH_TTL_SECS = 600.0


# Population cap on live-but-unclaimed speculative sessions — resumed AND
# fresh. The spawn semaphore bounds concurrent SPAWNS, not accumulated LIVE
# processes: restored resumable tabs flipped through after a gateway restart,
# or slots created/reconfigured in sequence, each stack one full kiro-cli
# process (RSS, plus the session's own MCP servers; the native session lock
# too when resumed) until the TTL or idle sweep fires. The cap is HOST-
# DERIVED (``resource_status.prewarm_allowance``): the fixed ceiling below
# on an ample host, one session when memory is tight, none when it is
# critical — so a host that cannot afford idle agent processes stops
# pre-warming them instead of stacking one per slot and then evicting. The
# allowance is applied twice: as ADMISSION before the spawn (room is made by
# evicting the oldest unclaimed session first, or the spawn is skipped when
# the allowance is zero or making room fails) and, PROBED AFRESH, as
# EVICTION after registration, for an allowance that shrank while the
# handshake ran. A claimed session is never touched by either — it just
# falls out of the accounting.
_RESUME_PREFETCH_MAX_LIVE = resource_status.PREWARM_MAX_LIVE

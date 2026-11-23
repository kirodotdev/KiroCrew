"""The YOLO grant kept across a gateway exit the user did not choose.

An ad-hoc YOLO grant (the dashboard picker, Slack ``/kirocrew yolo``, the API) stays
on across any restart, crash or host reboot, with the deadline it had: a timed grant
keeps its original wall-clock expiry and is never extended, and an ``until_shutdown``
grant lasts until the user stops Kiro Crew. A stop the user chose ends it
(``kept_grants.boot_verdict``). Declared grants are not recorded: the operator's
``agent.dangerously_skip_permissions`` and ``gateway --approval yolo`` are armed again
by every boot from config and argv. Task-scoped grants are not recorded either.

* :func:`request_sync` writes the live session-wide grant to the signed
  ``kept_grants.YOLO_RECORD``, or removes it when there is none. Every grant
  transition calls it (a ``safety_override_breadcrumb`` transition listener), on the
  breadcrumb worker, so it never does I/O on the transition's thread and two syncs
  land in the order their transitions did. Each derives the record from live state.
* :func:`restore_at_boot` runs once per boot, after the declared grant is armed and
  before the chats are restored. It restores the recorded grant only where the
  ceiling in force still permits it: the ``approval_modes`` verdict, the
  ``yolo_max_seconds`` cap counted from the grant's ORIGINAL activation, and the
  ``yolo_duration`` governance that decides whether a grant may be ``until_shutdown``.
  Only then does this process start writing its own record.

Every restore is audited fail-closed through ``SafetyOverride._commit_activation`` like
any other arming, and with the ``mode_change:yolo`` event a manual pick writes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew import kept_grants
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Sources armed again by every boot, so never recorded: the declared config grant and
#: ``gateway --approval yolo``.
_NOT_KEPT_SOURCES = frozenset({"config", "cli"})
#: Longest source name a record may carry; every real source is a short word.
_MAX_SOURCE_LEN = 64

_state_lock = threading.Lock()
#: Orders a revocation's removal and a queued write of the record (``request_sync``).
_io_lock = threading.Lock()
#: Bumped by every sync request; a queued write commits only while it is unchanged.
_gen = 0
#: False until this process's boot has read the previous record; no write before.
_armed = False
_listening = False


@dataclass(frozen=True)
class KeptYolo:
    """A grant :func:`restore_at_boot` put back. ``remaining_secs`` is -1 for no expiry."""

    source: str
    remaining_secs: int


def _snapshot(so: Any) -> dict[str, Any] | None:
    """The live session-wide grant as a record body, or ``None`` when none is kept.

    Read under the grant's own lock without going through ``is_active``, so a sync on
    the worker thread never runs the expiry callbacks, which are loop-affine.
    """
    from kiro_crew.safety_override import POLICY_REVOKED_SOURCE

    now_mono = time.monotonic()
    now_wall = time.time()
    with so._lock:
        permanent = bool(so._permanent)
        active = bool(so._active) and (permanent or so._expires_at > now_mono)
        source = str(so._source or "")
        expires_at = float(so._expires_at)
        activated_at = float(so._activated_at)
    if not active or not source or source in _NOT_KEPT_SOURCES:
        return None
    if source == POLICY_REVOKED_SOURCE:
        return None
    return {
        "source": source,
        "permanent": permanent,
        "expires_at": 0.0 if permanent else now_wall + (expires_at - now_mono),
        "activated_at": now_wall - max(0.0, now_mono - activated_at),
    }


def request_sync() -> None:
    """Sync the live grant's record. Never raises.

    A grant that is not kept (turned off, expired, revoked by policy) has its record
    removed here, on the transition's thread, before the change is acknowledged: a
    crash in the moment before a queued job ran would otherwise restore a grant the
    user just ended. A grant that is kept is written by a job queued on the
    breadcrumb worker, where the I/O belongs; a write lost to a crash loses the
    grant, never extends it.

    Every call bumps ``_gen`` under ``_io_lock``. A queued write puts its file in
    place only under that lock and only while ``_gen`` is still the one it sampled,
    so a write sampled before a revocation can never land after its removal; the
    job the newer call queued writes the newer state. The lock is held for a
    snapshot of memory plus one unlink or rename, never across a write.
    """
    global _gen
    if not _armed:
        return
    try:
        from kiro_crew.safety_override import _enqueue_job, safety_override

        # Resolved on the calling thread, so a write that runs later lands in the data
        # home this change belonged to.
        home = config_dir()
        with _io_lock:
            _gen += 1
            if _snapshot(safety_override()) is None:
                kept_grants.remove_record(kept_grants.YOLO_RECORD, home=home)

        def _job() -> None:
            with _io_lock:
                gen = _gen
                body = _snapshot(safety_override())
                if body is None:
                    kept_grants.remove_record(kept_grants.YOLO_RECORD, home=home)
                    return
            kept_grants.write_record(
                kept_grants.YOLO_RECORD,
                "yolo",
                body,
                home=home,
                commit=(_io_lock, lambda: _gen == gen),
            )

        _enqueue_job(_job)
    except Exception:
        logger.debug("kept YOLO: could not sync the record", exc_info=True)


def drop_record() -> None:
    """Remove the kept YOLO record now: an off-switch, before it is acknowledged.

    Runs on the deactivating thread, armed or not, so a Normal picked while the boot
    has not restored the record yet survives a crash too. Bumps ``_gen`` so a queued
    write sampled earlier never lands after it. One unlink; never raises.
    """
    global _gen
    try:
        with _io_lock:
            _gen += 1
            kept_grants.remove_record(kept_grants.YOLO_RECORD)
    except Exception:
        logger.debug("kept YOLO: could not drop the record", exc_info=True)


def _arm() -> None:
    global _armed, _listening
    with _state_lock:
        _armed = True
        if not _listening:
            from kiro_crew.safety_override import add_transition_listener

            add_transition_listener(request_sync)
            _listening = True
    request_sync()


def _audit(outcome: str, resources: str) -> None:
    try:
        from kiro_crew.safety_override import sel

        sel().log_api_access(
            caller="dashboard:kept_after_restart",
            operation="mode_change:yolo",
            outcome=outcome,
            resources=resources,
        )
    except Exception:
        logger.warning("SEL audit failed for a kept YOLO grant", exc_info=True)


def _parse(body: dict[str, Any]) -> tuple[str, bool, float, float] | None:
    source = body.get("source")
    permanent = body.get("permanent")
    expires_at = body.get("expires_at")
    activated_at = body.get("activated_at")
    if not isinstance(source, str) or not source or len(source) > _MAX_SOURCE_LEN:
        return None
    if not isinstance(permanent, bool):
        return None
    if not all(isinstance(v, (int, float)) for v in (expires_at, activated_at)):
        return None
    return source, permanent, float(expires_at), float(activated_at)  # type: ignore[arg-type]


def _restore(body: dict[str, Any], fence: int) -> KeptYolo | None:
    from kiro_crew.safety_override import safety_override, yolo_policy_permits, yolo_ttl_cap

    parsed = _parse(body)
    if parsed is None:
        _audit("denied", "kept_after_restart, reason:malformed")
        return None
    source, permanent, expires_at, activated_at = parsed
    if source in _NOT_KEPT_SOURCES:
        return None
    so = safety_override()
    if so.has_grant():
        # Boot already armed the declared grant, which is the one in force.
        return None
    if not yolo_policy_permits():
        _audit("denied", f"kept_after_restart, source:{source}, reason:policy")
        return None
    now = time.time()
    if now < activated_at:
        # The wall clock moved back across the exit, so no remaining time read from
        # it can be trusted not to extend the grant.
        _audit("denied", f"kept_after_restart, source:{source}, reason:clock")
        return None
    age = now - activated_at
    remaining = 0
    if not permanent:
        remaining = int(expires_at - now)
        if remaining <= 0:
            _audit("expired", f"kept_after_restart, source:{source}")
            return None
    cap = yolo_ttl_cap()
    if cap:
        cap = min(cap, so._MAX_TTL)
        left = int(cap - age)
        if left <= 0:
            _audit("expired", f"kept_after_restart, source:{source}, reason:yolo_max_seconds")
            return None
        remaining = left if permanent else min(remaining, left)
        permanent = False
    if permanent and not so.current_adhoc_duration()[1]:
        # The configured, governed duration does not allow ``until_shutdown`` at
        # this boot, so the grant comes back for the duration that applies now.
        permanent = False
        remaining = so.current_adhoc_duration()[0]
    if not permanent:
        remaining = max(1, min(remaining, so._MAX_TTL))
    # Refused at commit when the owner turned YOLO off (any deactivate, with a grant
    # or not) after this boot sampled *fence*: an acknowledged off-switch wins.
    result = so._commit_activation(
        source, ttl=0 if permanent else remaining, permanent=permanent, fence=fence, age=age
    )
    if not result.active:
        _audit("denied", f"kept_after_restart, source:{source}, reason:activation_refused")
        return None
    # The commit counted the grant's life from its original activation (``age``), so
    # ``yolo_max_seconds`` and a later cap bound it as without the restart.
    remaining = remaining if permanent else min(remaining, max(1, result.ttl))
    kept = KeptYolo(source=source, remaining_secs=-1 if permanent else remaining)
    _audit(
        "enabled",
        f"kept_after_restart, source:{source}, "
        f"remaining:{'none' if permanent else f'{remaining}s'}",
    )
    return kept


def restore_at_boot() -> KeptYolo | None:
    """Put back the grant the previous process held, if this boot keeps it.

    Once per boot, off the event loop, after the declared grant is armed. Nothing is
    restored after a stop the user chose. Whatever happens, this process then starts
    recording its own grant. Never raises.
    """
    try:
        # The epoch a process starts at: an owner's off-switch at ANY point since this
        # process started, before the restore ran or while it runs, refuses it at
        # commit (``SafetyOverride._commit_activation``'s fence). Every deactivate
        # bumps it, and nothing in boot deactivates on its own.
        fence = 0
        if kept_grants.boot_verdict() != kept_grants.VERDICT_KEPT:
            cleared = kept_grants.cleared_record(kept_grants.YOLO_RECORD)
            if cleared is not None:
                source = str(cleared.get("source", ""))[:_MAX_SOURCE_LEN]
                _audit("disabled", f"cleared_by_user_stop, source:{source}")
            return None
        body = kept_grants.read_record(kept_grants.YOLO_RECORD, "yolo")
        if body is None:
            return None
        return _restore(body, fence)
    except Exception:
        logger.warning("kept YOLO: could not restore the previous grant", exc_info=True)
        return None
    finally:
        try:
            _arm()
        except Exception:
            logger.warning("kept YOLO: could not start recording the grant", exc_info=True)


def describe(kept: KeptYolo) -> str:
    """The dashboard note body for a grant a restart kept."""
    if kept.remaining_secs < 0:
        return (
            "Kiro Crew restarted without being stopped, so auto-approve is still on. "
            "It stays on until you stop Kiro Crew or pick another approval mode."
        )
    minutes = max(1, kept.remaining_secs // 60)
    return (
        "Kiro Crew restarted without being stopped, so auto-approve is still on, "
        f"with the time it had left ({minutes} min)."
    )


def _reset_for_tests() -> None:
    """Stop recording until the next boot arms again, and drop the transition listener."""
    global _armed, _listening, _gen
    with _state_lock:
        _armed = False
        _gen = 0
        if _listening:
            from kiro_crew import safety_override_breadcrumb

            try:
                safety_override_breadcrumb._transition_listeners.remove(request_sync)
            except ValueError:
                pass
            _listening = False

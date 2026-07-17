"""Time-limited safety override — replaces permanent YOLO mode.

Provides a ``SafetyOverride`` class that can be activated for a bounded TTL
(default per-source) and automatically expires.  A 5-minute grace window
after expiry allows renew() to reactivate without a full re-activation flow.

Sources and default TTLs:
- slack     → 30 min
- dashboard → 6 h
- config    → 24 h  (startup only)

Hard ceiling: 24 h regardless of requested TTL.

All state changes are logged to the Security Event Log (SEL).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from kiro_crew.sel import sel as _get_sel

logger = logging.getLogger(__name__)


def sel():  # noqa: ANN201 — thin wrapper kept for test patchability
    """Return the SEL singleton.

    Defined at module level so tests can patch ``kiro_crew.safety_override.sel``.
    """
    return _get_sel()


# ─── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class ActivationResult:
    """Returned by SafetyOverride.activate()."""

    active: bool
    ttl: int
    source: str
    activated_at_iso: str


@dataclass
class RenewResult:
    """Returned by SafetyOverride.renew()."""

    renewed: bool
    ttl: int  # 0 if not renewed
    source: str
    reason: str = ""  # populated on denial


@dataclass
class OverrideStatus:
    """Snapshot returned by SafetyOverride.status()."""

    active: bool
    source: str
    remaining_secs: int
    activation_count: int
    activated_at_iso: Optional[str]  # None when inactive
    expires_at_iso: Optional[str]  # None when inactive
    last_renewed_at_iso: Optional[str]  # None if never renewed
    last_renewed_by: str


# ─── Core class ──────────────────────────────────────────────────────────────


class SafetyOverride:
    """Time-limited safety override with SEL audit trail.

    All public methods are thread-safe.
    """

    # ── Constants ────────────────────────────────────────────────────────────

    _MAX_TTL: int = 86400  # 24 h hard ceiling
    _SLACK_TTL: int = 1800  # 30 min
    _DASHBOARD_TTL: int = 21600  # 6 h
    _CONFIG_TTL: int = 86400  # 24 h (config-triggered startup)
    _RENEW_GRACE_SECS: int = 300  # 5-min grace window after expiry

    _SOURCE_TTLS: dict[str, int] = {
        "slack": _SLACK_TTL,
        "dashboard": _DASHBOARD_TTL,
        "config": _CONFIG_TTL,
    }

    # Class-level default lock for instances created via object.__new__() (e.g. tests).
    # Each real instance gets its own lock in __init__; this is just a safe fallback.
    _lock: threading.Lock

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: bool = False
        self._source: str = ""
        self._activated_at: float = 0.0
        self._expires_at: float = 0.0
        self._activation_count: int = 0
        self._last_renewed_at: float = 0.0
        self._last_renewed_by: str = ""
        self._on_expired: Optional[Callable[[str], None]] = None
        self._on_activated: Optional[Callable[[str, int], None]] = None

    def __getattr__(self, name: str) -> object:
        # Provide a fallback _lock for instances created with object.__new__()
        # that have not gone through __init__ (test fixtures bypass __init__).
        if name == "_lock":
            lock = threading.Lock()
            object.__setattr__(self, "_lock", lock)
            return lock
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # ── Callback properties ──────────────────────────────────────────────────

    @property
    def on_expired(self) -> Optional[Callable[[str], None]]:
        return self._on_expired

    @on_expired.setter
    def on_expired(self, cb: Optional[Callable[[str], None]]) -> None:
        self._on_expired = cb

    @property
    def on_activated(self) -> Optional[Callable[[str, int], None]]:
        return self._on_activated

    @on_activated.setter
    def on_activated(self, cb: Optional[Callable[[str, int], None]]) -> None:
        self._on_activated = cb

    # ── Public API ───────────────────────────────────────────────────────────

    def activate(self, source: str, ttl: Optional[int] = None) -> ActivationResult:
        """Activate the override for the given source.

        Args:
            source: Trigger source (``slack``, ``dashboard``, ``config``, …).
            ttl: Override TTL in seconds.  Defaults to the source's default TTL.
                 Capped at ``_MAX_TTL``.

        Returns:
            ActivationResult with effective TTL and wall-clock activation time.
        """
        if ttl is None:
            ttl = self._SOURCE_TTLS.get(source, self._SLACK_TTL)
        ttl = min(ttl, self._MAX_TTL)

        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc)
        activated_at_iso = now_wall.isoformat()

        # Snapshot state under lock for reactivation check
        with self._lock:
            was_active = self._active
            prev_source = self._source
            prev_remaining = max(0, int(self._expires_at - now_mono)) if self._active else 0

        # Audit BEFORE committing — fail-closed with no race window
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:activate",
                outcome="enabled",
                resources=f"source:{source}, ttl:{ttl}s",
                critical=True,
            )
        except Exception:
            logger.error("SEL audit failed; refusing safety override activation", exc_info=True)
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        # Log reactivation only after critical audit succeeds
        if was_active:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:reactivate",
                outcome="enabled",
                resources=f"prev_source:{prev_source}, prev_remaining:{prev_remaining}s, new_source:{source}, new_ttl:{ttl}s",
            )

        # Only commit after audit succeeds
        with self._lock:
            self._active = True
            self._source = source
            self._activated_at = now_mono
            self._expires_at = now_mono + ttl
            self._activation_count += 1
            self._last_renewed_at = 0.0
            self._last_renewed_by = ""

        cb = self._on_activated
        if cb is not None:
            try:
                cb(source, ttl)
            except Exception:
                logger.warning("on_activated callback raised", exc_info=True)

        return ActivationResult(
            active=True,
            ttl=ttl,
            source=source,
            activated_at_iso=activated_at_iso,
        )

    def renew(self, source: str) -> RenewResult:
        """Renew (extend) the override using the source's default TTL.

        Succeeds if the override is currently active OR if it expired within
        the ``_RENEW_GRACE_SECS`` grace window.

        Returns:
            RenewResult.renewed=True on success, False otherwise.
        """
        now_mono = time.monotonic()
        ttl = 0

        denied = False
        with self._lock:
            currently_active = self._active and self._expires_at > now_mono
            in_grace = (
                not currently_active
                and self._expires_at > 0
                and (now_mono - self._expires_at) <= self._RENEW_GRACE_SECS
            )
            if currently_active or in_grace:
                ttl = self._SOURCE_TTLS.get(source, self._SLACK_TTL)
                ttl = min(ttl, self._MAX_TTL)
                self._active = True
                self._expires_at = now_mono + ttl
                self._last_renewed_at = now_mono
                self._last_renewed_by = source
            else:
                denied = True

        if denied:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew",
                outcome="denied",
                resources="reason:not_active",
            )
            return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")

        self._log_sel(
            caller="safety_override",
            operation="safety_override:renew",
            outcome="renewed",
            resources=f"source:{source}, new_ttl:{ttl}s",
        )
        return RenewResult(renewed=True, ttl=ttl, source=source)

    def deactivate(self, source: str) -> None:
        """Deactivate the override immediately.  No-op if already inactive."""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._expires_at = 0.0

        self._log_sel(
            caller="safety_override",
            operation="safety_override:deactivate",
            outcome="disabled",
            resources=f"source:{source}",
        )

    def is_active(self) -> bool:
        """Return True if the override is currently active.

        Triggers expiry bookkeeping (callback + SEL log) when the TTL lapses.
        """
        now_mono = time.monotonic()

        with self._lock:
            if not self._active:
                return False

            if now_mono < self._expires_at:
                return True

            # TTL lapsed — expire now
            self._active = False
            expired_source = self._source

        # Callbacks and SEL logging happen outside the lock to avoid deadlocks.
        self._log_sel(
            caller="safety_override",
            operation="safety_override:expired",
            outcome="expired",
            resources=f"source:{expired_source}",
        )

        cb = self._on_expired
        if cb is not None:
            try:
                cb(expired_source)
            except Exception:
                logger.warning("on_expired callback raised", exc_info=True)

        return False

    def remaining_secs(self) -> int:
        """Return seconds remaining, 0 if inactive or expired."""
        self.is_active()
        now_mono = time.monotonic()
        with self._lock:
            if not self._active:
                return 0
            remaining = self._expires_at - now_mono
            return max(0, int(remaining))

    def status(self) -> OverrideStatus:
        """Return a point-in-time status snapshot.

        Monotonic timestamps are converted to wall-clock ISO 8601 UTC by
        computing the offset from ``time.monotonic()`` to ``datetime.now()``.
        """
        self.is_active()

        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc).timestamp()

        with self._lock:
            active = self._active and self._expires_at > now_mono
            source = self._source
            count = self._activation_count
            activated_at = self._activated_at
            expires_at = self._expires_at
            last_renewed_at = self._last_renewed_at
            last_renewed_by = self._last_renewed_by

        def _mono_to_iso(mono_ts: float) -> Optional[str]:
            if mono_ts <= 0.0:
                return None
            wall_ts = now_wall + (mono_ts - now_mono)
            return datetime.fromtimestamp(wall_ts, tz=timezone.utc).isoformat()

        remaining = 0
        if active:
            remaining = max(0, int(expires_at - now_mono))

        return OverrideStatus(
            active=active,
            source=source,
            remaining_secs=remaining,
            activation_count=count,
            activated_at_iso=_mono_to_iso(activated_at) if active else None,
            expires_at_iso=_mono_to_iso(expires_at) if active else None,
            last_renewed_at_iso=_mono_to_iso(last_renewed_at),
            last_renewed_by=last_renewed_by,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _log_sel(
        self,
        *,
        caller: str,
        operation: str,
        outcome: str,
        resources: str = "",
        critical: bool = False,
    ) -> None:
        """Log a SEL event.

        When ``critical=True`` the exception is re-raised so the caller can
        enforce fail-closed behaviour (e.g. activation must roll back).
        Otherwise the failure is swallowed and only a warning is emitted.
        """
        try:
            sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome=outcome,
                source="safety_override",
                resources=resources,
                critical=critical,
            )
        except Exception:
            if critical:
                raise
            logger.warning("SEL log failed for %s/%s", operation, outcome, exc_info=True)


# ─── Module-level singleton ──────────────────────────────────────────────────

_singleton: Optional[SafetyOverride] = None
_singleton_lock = threading.Lock()


def safety_override() -> SafetyOverride:
    """Return the module-level singleton SafetyOverride instance."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SafetyOverride()
    return _singleton


def reset_singleton() -> None:
    """Reset the singleton.  Intended for use in tests only."""
    global _singleton
    with _singleton_lock:
        _singleton = None

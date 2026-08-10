"""Time-limited safety override — replaces permanent YOLO mode.

Provides a ``SafetyOverride`` class with two kinds of grant:

- **Ad-hoc** — YOLO toggled mid-session from Slack, the dashboard picker or the
  API. Bounded by ONE duration shared by every surface (``agent.yolo_duration``,
  default 6 h, hard ceiling 24 h) and automatically expires. A 5-minute grace
  window after expiry allows renew() to reactivate without a full
  re-activation flow.
- **Declared** — ``agent.dangerously_skip_permissions: true`` in operator-owned
  config (the camelCase and legacy ``yolo`` spellings are also read). A standing
  instruction, so it does NOT expire: it is re-established and re-audited on
  every startup (state is in-memory), cleared the moment the operator picks
  another approval mode, and deniable by the enterprise governance ceiling via
  the ``yolo_duration`` scope's ``permanent`` member — which downgrades it to the
  ad-hoc duration.

Per-surface TTLs (30 min Slack / 6 h dashboard / 24 h config) were removed: the
same operator re-enabling the same grant got a different lifetime depending on
where they clicked, which was unpredictable without buying any security.

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
    # True when the live grant was DECLARED in config and has no expiry at all.
    # ``remaining_secs`` is -1 and ``expires_at_iso`` is None in that case.
    permanent: bool = False


# ─── Core class ──────────────────────────────────────────────────────────────


class SafetyOverride:
    """Time-limited safety override with SEL audit trail.

    All public methods are thread-safe.
    """

    # ── Constants ────────────────────────────────────────────────────────────

    _MAX_TTL: int = 86400  # 24 h hard ceiling for an AD-HOC grant
    # ONE duration for every ad-hoc surface. Enabling YOLO from Slack and from
    # the dashboard picker is the same decision made from different places, so
    # they expire the same way. Per-surface TTLs (30 min Slack / 6 h dashboard)
    # made the behavior unpredictable without buying security: the same operator
    # re-enabled the same grant either way. Overridable via
    # ``agent.yolo_duration``, clamped to ``_MAX_TTL``.
    _ADHOC_TTL_DEFAULT: int = 21600  # 6 h
    _RENEW_GRACE_SECS: int = 300  # 5-min grace window after expiry

    # The one source carrying STANDING authority: a grant the operator DECLARED
    # in config (``dangerouslySkipPermissions``), as opposed to one toggled ad hoc
    # mid-session. A declared grant does not expire — see ``activate_declared``.
    _DECLARED_SOURCE: str = "config"

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
        # True when the live grant has NO expiry: either DECLARED in config, or
        # an ad-hoc grant under ``yolo_duration: until_shutdown``. Policy
        # permits a standing grant. A permanent grant has no deadline at all, so
        # ``_expires_at`` is not consulted while it is set — but it is still kept
        # finite so the 0.0 "never activated / deactivated" sentinel and the
        # renew grace window keep their meaning for every other path.
        self._permanent: bool = False
        # Ad-hoc TTL in force, seeded from ``agent.yolo_duration`` at startup.
        self._adhoc_ttl: int = self._ADHOC_TTL_DEFAULT
        # True when ``agent.yolo_duration`` is ``until_shutdown``: an ad-hoc grant
        # then has no timed expiry and lasts until the process stops. Still
        # in-memory, so it cannot survive a restart the way a DECLARED grant does.
        self._adhoc_until_shutdown: bool = False
        # Resolves the ad-hoc duration from LIVE config at activation time.
        # Installed in production by ``install_duration_resolver``; ``None`` in
        # tests, which set ``adhoc_ttl`` / ``adhoc_until_shutdown`` directly.
        # Reading it live is what makes a duration saved from Settings apply to
        # the next activation instead of only after a restart.
        self._duration_resolver: Optional[Callable[[], tuple[int, bool]]] = None
        # Task-scoped auto-approve grants: scope key -> (activated_at, expires_at)
        # monotonic. Independent of the global override; each grant is TTL-bounded,
        # audited on activation, and slide-renewable up to a 24h ceiling from first
        # activation, so a caller (e.g. the task runner) can hold a narrow, expiring
        # grant without flipping the session-wide override.
        self._scoped: dict[str, tuple[float, float]] = {}

    def __getattr__(self, name: str) -> object:
        # Provide a fallback _lock for instances created with object.__new__()
        # that have not gone through __init__ (test fixtures bypass __init__).
        if name == "_lock":
            lock = threading.Lock()
            object.__setattr__(self, "_lock", lock)
            return lock
        if name == "_scoped":
            scoped: dict[str, tuple[float, float]] = {}
            object.__setattr__(self, "_scoped", scoped)
            return scoped
        # Same reason as _lock/_scoped: test fixtures build instances via
        # object.__new__() and set fields by hand, so the expiry path must still
        # be able to read these.
        if name == "_permanent":
            object.__setattr__(self, "_permanent", False)
            return False
        if name == "_adhoc_ttl":
            object.__setattr__(self, "_adhoc_ttl", self._ADHOC_TTL_DEFAULT)
            return self._ADHOC_TTL_DEFAULT
        if name == "_adhoc_until_shutdown":
            object.__setattr__(self, "_adhoc_until_shutdown", False)
            return False
        if name == "_duration_resolver":
            object.__setattr__(self, "_duration_resolver", None)
            return None
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

    @property
    def adhoc_ttl(self) -> int:
        """Seconds an ad-hoc grant lasts (Slack, dashboard, API — all the same)."""
        return self._adhoc_ttl

    @adhoc_ttl.setter
    def adhoc_ttl(self, secs: int) -> None:
        self._adhoc_ttl = max(1, min(int(secs), self._MAX_TTL))

    @property
    def adhoc_until_shutdown(self) -> bool:
        """True when an ad-hoc grant should last until the process stops."""
        return bool(self._adhoc_until_shutdown)

    @adhoc_until_shutdown.setter
    def adhoc_until_shutdown(self, value: bool) -> None:
        self._adhoc_until_shutdown = bool(value)

    @property
    def duration_resolver(self) -> Optional[Callable[[], tuple[int, bool]]]:
        return self._duration_resolver

    @duration_resolver.setter
    def duration_resolver(self, fn: Optional[Callable[[], tuple[int, bool]]]) -> None:
        self._duration_resolver = fn

    def current_adhoc_duration(self) -> tuple[int, bool]:
        """``(ttl_secs, until_shutdown)`` for a NEW ad-hoc grant, resolved live.

        Consults the installed resolver (live config + governance clamp) so a
        duration saved from Settings applies to the next activation without a
        restart. Falls back to the last known values if the resolver fails, so a
        transient config read error cannot wedge activation.
        """
        resolver = self._duration_resolver
        if resolver is not None:
            try:
                ttl, until_shutdown = resolver()
                return max(1, min(int(ttl), self._MAX_TTL)), bool(until_shutdown)
            except Exception:
                logger.warning(
                    "ad-hoc duration resolver failed; using the last known value",
                    exc_info=True,
                )
        return self._adhoc_ttl, bool(self._adhoc_until_shutdown)

    @property
    def is_permanent(self) -> bool:
        """True when the live grant has no expiry at all."""
        return bool(self._permanent) and bool(self._active)

    # ── Public API ───────────────────────────────────────────────────────────

    def activate(self, source: str, ttl: Optional[int] = None) -> ActivationResult:
        """Activate a TTL-bounded (ad-hoc) override for the given source.

        Every ad-hoc surface gets the SAME duration — see ``_ADHOC_TTL_DEFAULT``.
        When ``agent.yolo_duration`` is ``until_shutdown`` an ad-hoc grant has no
        timed expiry and lasts until the process stops (still in-memory, so a
        restart clears it). For the operator's declared
        ``dangerouslySkipPermissions`` grant, which is re-established on every
        startup, use :meth:`activate_declared` instead.

        Args:
            source: Trigger source (``slack``, ``dashboard``, ``config``, …).
            ttl: Explicit TTL in seconds. Defaults to the in-force ad-hoc
                 duration. Capped at ``_MAX_TTL``. Passing an explicit ttl always
                 produces a timed grant, even under ``until_shutdown``.

        Returns:
            ActivationResult with effective TTL and wall-clock activation time.
        """
        if ttl is None:
            ttl, until_shutdown = self.current_adhoc_duration()
            if until_shutdown:
                return self._commit_activation(source, ttl=0, permanent=True)
        ttl = min(ttl, self._MAX_TTL)
        return self._commit_activation(source, ttl=ttl, permanent=False)

    def activate_declared(self, source: str = _DECLARED_SOURCE) -> ActivationResult:
        """Activate a NON-EXPIRING override for an operator-declared grant.

        ``dangerouslySkipPermissions`` is a standing instruction, not a session-scoped
        one: honouring it for 24h and then silently reverting to
        prompt-for-everything is the defect this replaces. The grant is still
        re-established and re-audited on every startup (state is in-memory), is
        cleared the moment the operator picks another approval mode, and is
        deniable by the enterprise governance ceiling — callers must consult
        :func:`declared_grant_permitted` first and fall back to ``activate`` when
        policy forbids a standing grant.
        """
        return self._commit_activation(source, ttl=0, permanent=True)

    def _commit_activation(self, source: str, *, ttl: int, permanent: bool) -> ActivationResult:
        """Shared activation commit: audit fail-closed, then install the grant."""
        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc)
        activated_at_iso = now_wall.isoformat()
        ttl_desc = "permanent" if permanent else f"{ttl}s"

        # Snapshot state under lock for reactivation check
        with self._lock:
            was_active = self._active
            prev_source = self._source
            prev_remaining = (
                -1
                if (self._active and self._permanent)
                else (max(0, int(self._expires_at - now_mono)) if self._active else 0)
            )

        # Audit BEFORE committing — fail-closed with no race window
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:activate",
                outcome="enabled",
                resources=f"source:{source}, ttl:{ttl_desc}",
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
                resources=f"prev_source:{prev_source}, prev_remaining:{prev_remaining}s, new_source:{source}, new_ttl:{ttl_desc}",
            )

        # Only commit after audit succeeds
        with self._lock:
            self._active = True
            self._source = source
            self._permanent = permanent
            self._activated_at = now_mono
            # Kept finite even when permanent so the 0.0 inactive sentinel and
            # the renew grace window keep working; it is simply not consulted.
            self._expires_at = now_mono + (ttl if ttl > 0 else self._MAX_TTL)
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
        # Resolved BEFORE taking the lock: the resolver reads config from disk,
        # and holding the state lock across that I/O would stall every concurrent
        # is_active() check.
        renew_ttl = min(self.current_adhoc_duration()[0], self._MAX_TTL)

        denied = False
        with self._lock:
            # A permanent grant has nothing to extend and must never be
            # downgraded to a finite deadline by a renew.
            if self._active and self._permanent:
                return RenewResult(renewed=True, ttl=-1, source=source)
            currently_active = self._active and self._expires_at > now_mono
            in_grace = (
                not currently_active
                and self._expires_at > 0
                and (now_mono - self._expires_at) <= self._RENEW_GRACE_SECS
            )
            if currently_active or in_grace:
                ttl = renew_ttl
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
        """Deactivate the override immediately.

        Emits a ``safety_override:deactivate`` SEL event whenever a grant
        exists in ANY form — live, or already lapsed via lazy expiry. Lazy
        expiry (``is_active``) clears only ``_active`` and leaves the rest of
        the grant's state in place, so ``_expires_at`` still holding a nonzero
        deadline is what distinguishes "lapsed" from "never activated": the
        0.0 sentinel means no grant ever existed (or it was already explicitly
        deactivated), and only that case stays silent. The SEL stream is the
        durable record of who changed the auto-approval posture, so an
        operator's explicit decision to switch back to normal mode must be
        recorded even when the TTL happened to elapse first.

        Zeroing ``_expires_at`` here also closes the renew grace window, so a
        grant the operator explicitly revoked cannot be resurrected by a
        subsequent ``renew()`` — regardless of whether it was live or lapsed
        at the time of the call.
        """
        now_mono = time.monotonic()
        with self._lock:
            if not self._active and self._expires_at <= 0.0:
                return
            # _active alone can overstate liveness: a lapsed TTL is only
            # reconciled when is_active() polls, so derive liveness the same
            # way renew() does — permanence or an unexpired deadline.
            was_active = self._active and (self._permanent or self._expires_at > now_mono)
            was_permanent = was_active and self._permanent
            prior_source = self._source
            remaining = (
                -1
                if was_permanent
                else (max(0, int(self._expires_at - now_mono)) if was_active else 0)
            )
            self._active = False
            self._permanent = False
            self._expires_at = 0.0

        # SEL write happens OUTSIDE the lock (same rule as renew(): never hold
        # the state lock across I/O). This is a REVOCATION, not a grant, so it
        # is deliberately NOT fail-closed like _commit_activation: refusing to
        # deactivate because an audit write failed would leave auto-approval
        # ON, which is strictly worse. The state change above is unconditional.
        self._log_sel(
            caller="safety_override",
            operation="safety_override:deactivate",
            outcome="disabled",
            resources=(
                f"source:{source}, was_active:{was_active}, "
                f"was_permanent:{was_permanent}, remaining:{remaining}s, "
                f"prior_source:{prior_source}"
            ),
        )

    # ── Task-scoped grants ───────────────────────────────────────────────────

    def activate_scoped(
        self, scope: str, source: str, ttl: Optional[int] = None
    ) -> ActivationResult:
        """Activate a narrow, TTL-bounded auto-approve grant for ``scope``.

        Unlike ``activate()`` this does NOT flip the session-wide override; it
        records an expiring grant for a single scope key (e.g. one task run).
        The activation is audited fail-closed to the SEL BEFORE it is committed,
        exactly like the global ``activate()``, so no grant exists without an
        audit trail. TTL defaults to the source's default and is capped at the
        24h hard ceiling.
        """
        if ttl is None:
            ttl = self._adhoc_ttl
        ttl = min(ttl, self._MAX_TTL)
        now_mono = time.monotonic()
        activated_at_iso = datetime.now(tz=timezone.utc).isoformat()

        # Fail-closed audit before commit — no grant without a trace.
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:activate_scoped",
                outcome="enabled",
                resources=f"scope:{scope}, source:{source}, ttl:{ttl}s",
                critical=True,
            )
        except Exception:
            logger.error(
                "SEL audit failed; refusing scoped safety override activation", exc_info=True
            )
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        with self._lock:
            self._scoped[scope] = (now_mono, now_mono + ttl)

        return ActivationResult(
            active=True, ttl=ttl, source=source, activated_at_iso=activated_at_iso
        )

    def renew_scoped(
        self, scope: str, source: str, ttl: Optional[int] = None
    ) -> RenewResult:
        """Slide a scoped grant's expiry forward on activity, capped at the ceiling.

        Extends the grant to ``min(now + ttl, activated_at + _MAX_TTL)`` so an
        actively-progressing run does not lose trust at the base TTL, while the
        absolute 24h hard ceiling from first activation is still honored (an
        abandoned run with no activity simply lapses). No-op / not-renewed if the
        grant is absent or the ceiling is already reached. Intentionally NOT
        SEL-logged per call — it extends an already-audited grant within its
        audited ceiling, and per-tool-call logging would flood the SEL.
        """
        if ttl is None:
            ttl = self._adhoc_ttl
        ttl = min(ttl, self._MAX_TTL)
        now_mono = time.monotonic()
        with self._lock:
            entry = self._scoped.get(scope)
            if entry is None:
                return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")
            activated_at, _ = entry
            ceiling = activated_at + self._MAX_TTL
            if now_mono >= ceiling:
                return RenewResult(renewed=False, ttl=0, source=source, reason="ceiling_reached")
            new_expiry = min(now_mono + ttl, ceiling)
            self._scoped[scope] = (activated_at, new_expiry)
            remaining = max(0, int(new_expiry - now_mono))
        return RenewResult(renewed=True, ttl=remaining, source=source)

    def is_scope_active(self, scope: str) -> bool:
        """Return True if ``scope`` has a live (unexpired) grant.

        Expires the grant and logs a SEL event when its TTL has lapsed.
        """
        now_mono = time.monotonic()
        with self._lock:
            entry = self._scoped.get(scope)
            if entry is None:
                return False
            if now_mono < entry[1]:
                return True
            del self._scoped[scope]

        self._log_sel(
            caller="safety_override",
            operation="safety_override:scope_expired",
            outcome="expired",
            resources=f"scope:{scope}",
        )
        return False

    def deactivate_scope(self, scope: str) -> None:
        """Revoke a scoped grant immediately. No-op if absent."""
        with self._lock:
            existed = self._scoped.pop(scope, None) is not None
        if existed:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:deactivate_scope",
                outcome="disabled",
                resources=f"scope:{scope}",
            )

    def scope_remaining_secs(self, scope: str) -> int:
        """Return seconds remaining on a scoped grant, 0 if absent/expired.

        Pure read — does NOT expire or SEL-log a lapsed grant (that is the
        enforcement path's job via ``is_scope_active``), so a status/UI poll can
        never emit a ``scope_expired`` event or mutate state.
        """
        now_mono = time.monotonic()
        with self._lock:
            entry = self._scoped.get(scope)
            if entry is None:
                return 0
            return max(0, int(entry[1] - now_mono))

    def is_active(self) -> bool:
        """Return True if the override is currently active.

        Triggers expiry bookkeeping (callback + SEL log) when the TTL lapses.
        A DECLARED grant has no deadline, so it never reaches that path.
        """
        now_mono = time.monotonic()

        with self._lock:
            if not self._active:
                return False

            # Declared grants do not expire — the operator's config IS the
            # authority, and it is re-read on every startup.
            if self._permanent:
                return True

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
        """Return seconds remaining; 0 if inactive, -1 if it never expires."""
        self.is_active()
        now_mono = time.monotonic()
        with self._lock:
            if not self._active:
                return 0
            if self._permanent:
                return -1
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
            permanent = bool(self._permanent)
            # A permanent grant is active regardless of the (unconsulted)
            # deadline — deriving ``active`` from ``_expires_at`` alone would
            # report it inactive once that finite placeholder passed.
            active = self._active and (permanent or self._expires_at > now_mono)
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
            remaining = -1 if permanent else max(0, int(expires_at - now_mono))

        return OverrideStatus(
            active=active,
            source=source,
            remaining_secs=remaining,
            activation_count=count,
            activated_at_iso=_mono_to_iso(activated_at) if active else None,
            expires_at_iso=None if permanent else (_mono_to_iso(expires_at) if active else None),
            last_renewed_at_iso=_mono_to_iso(last_renewed_at),
            last_renewed_by=last_renewed_by,
            permanent=permanent and active,
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


_PERMANENT_MEMBER = "permanent"
_UNTIL_SHUTDOWN_MEMBER = "until_shutdown"
_GOVERNANCE_SCOPE = "yolo_duration"


def _duration_member_permitted(member: str) -> bool:
    """Ask the enterprise ceiling whether a duration member may be selected.

    Evaluated against the HOST profile (these are gateway-level decisions, not
    per-session ones) with ``fail_closed=True``, so a governance-evaluation error
    DENIES the riskier duration rather than silently granting it. With no policy
    configured — the standalone default — an ungoverned scope permits, so a solo
    operator's config is honoured.
    """
    # Deferred import: keeps this module free of a governance/config dependency
    # at import time (it is imported very early by the security/hook layers), so
    # no import cycle is possible regardless of which entrypoint loads first.
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )
    except Exception:
        logger.debug("governance layer unavailable; permitting %s", member, exc_info=True)
        return True
    decision = governance_permits(
        _GOVERNANCE_SCOPE,
        member,
        session_key=HOST_SESSION_KEY,
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))


def declared_grant_permitted() -> bool:
    """True when policy allows a DECLARED grant to persist without expiry.

    ``dangerouslySkipPermissions: true`` is the operator's standing instruction,
    but on a managed fleet an admin must be able to forbid a never-expiring
    grant. Denying the ``permanent`` member of the ``yolo_duration`` scope forces
    a declared grant back onto the ordinary ad-hoc duration.
    """
    return _duration_member_permitted(_PERMANENT_MEMBER)


def until_shutdown_permitted() -> bool:
    """True when policy allows the ad-hoc ``until_shutdown`` duration."""
    return _duration_member_permitted(_UNTIL_SHUTDOWN_MEMBER)


def resolve_configured_duration() -> tuple[int, bool]:
    """``(ttl_secs, until_shutdown)`` from live config, with the policy clamp.

    Read at every ad-hoc activation, so a duration saved from Settings takes
    effect on the next activation rather than only after a restart.
    ``until_shutdown`` is clamped back to the default TTL when policy forbids it.
    """
    from kiro_crew.config.loader import (
        YOLO_UNTIL_SHUTDOWN,
        KiroCrewConfig,
        yolo_duration_to_secs,
    )

    label = KiroCrewConfig.load().agent.yolo_duration
    if label == YOLO_UNTIL_SHUTDOWN:
        if until_shutdown_permitted():
            return SafetyOverride._ADHOC_TTL_DEFAULT, True
        logger.info(
            "Enterprise policy forbids the until_shutdown auto-approve duration; "
            "using the default timed duration"
        )
        return SafetyOverride._ADHOC_TTL_DEFAULT, False
    return yolo_duration_to_secs(label), False


def install_duration_resolver() -> None:
    """Make ad-hoc activations read their duration from live config.

    Called from every entrypoint that can hand out an ad-hoc grant, so Slack, the
    dashboard and the API all agree — and so a duration change applies without a
    restart. Idempotent.
    """
    safety_override().duration_resolver = resolve_configured_duration


def apply_config_duration() -> int:
    """Seed the ad-hoc duration once and return the TTL (0 for until_shutdown).

    Kept for the startup log and for callers that want the value up front; the
    resolver installed by :func:`install_duration_resolver` is what keeps it
    current afterwards.
    """
    so = safety_override()
    install_duration_resolver()
    try:
        ttl, until_shutdown = resolve_configured_duration()
    except Exception:
        logger.warning("could not read agent.yolo_duration; using the default", exc_info=True)
        so.adhoc_until_shutdown = False
        so.adhoc_ttl = SafetyOverride._ADHOC_TTL_DEFAULT
        return so.adhoc_ttl
    so.adhoc_until_shutdown = until_shutdown
    so.adhoc_ttl = ttl
    return 0 if until_shutdown else ttl


def grant_declared_yolo() -> ActivationResult:
    """Install the operator's declared ``dangerouslySkipPermissions`` grant.

    Permanent when policy permits, otherwise clamped to the ad-hoc duration so
    the admin ceiling wins. Shared by the dashboard and Slack startup paths so a
    headless ``--slack-only`` gateway behaves identically to a full one.
    """
    apply_config_duration()
    so = safety_override()
    if declared_grant_permitted():
        return so.activate_declared()
    logger.info(
        "Enterprise policy forbids a never-expiring auto-approve grant; "
        "the declared grant falls back to the ad-hoc duration"
    )
    return so.activate(SafetyOverride._DECLARED_SOURCE)

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

import asyncio
import json
import logging
import os
import queue
import stat
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.platform.context import governance_generation
from kiro_crew.sel import sel as _get_sel

logger = logging.getLogger(__name__)


def sel():  # noqa: ANN201 — thin wrapper kept for test patchability
    """Return the SEL singleton.

    Defined at module level so tests can patch ``kiro_crew.safety_override.sel``.
    """
    return _get_sel()


# ─── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class DroppedGrant:
    """A timed grant that was live when the process went down.

    Returned by :func:`take_dropped_grant` so a startup can TELL the operator
    their override is gone. Carries no authority: it is a notice, never a
    restored grant, which is why the record it comes from is not signed -- see
    :func:`_write_breadcrumb`.
    """

    source: str
    remaining_secs: int


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
        # Orders breadcrumb publishes against overlapping transitions -- see
        # ``_sync_breadcrumb``. Bumped under ``_lock`` by every transition;
        # ``_breadcrumb_published_gen`` is read and written under
        # ``_breadcrumb_io_lock`` only.
        self._breadcrumb_gen: int = 0
        self._breadcrumb_published_gen: int = 0

    def __getattr__(self, name: str) -> object:
        # Provide a fallback _lock for instances created with object.__new__()
        # that have not gone through __init__ (test fixtures bypass __init__).
        if name == "_lock":
            lock = threading.Lock()
            object.__setattr__(self, "_lock", lock)
            return lock
        if name in ("_breadcrumb_gen", "_breadcrumb_published_gen"):
            # Same reason as the fields below: test fixtures build instances via
            # object.__new__(), and every transition touches these.
            object.__setattr__(self, name, 0)
            return 0
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

    @property
    def is_declared(self) -> bool:
        """True when the live grant is the operator's DECLARED config grant.

        Identity is the grant's SOURCE, not the absence of a deadline. Permanence
        does not separate the two cases in either direction: an ad-hoc grant under
        ``yolo_duration: until_shutdown`` also has no expiry, and a declared grant
        the governance ceiling refused to make permanent is timed.
        """
        return bool(self._active) and self._source == self._DECLARED_SOURCE

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

    def _log_policy_refusal(self, source: str, *, scope: str) -> None:
        """Audit an arming refused by an ``approval_modes`` deny of ``yolo``.

        Non-critical by design: an SEL write failure must never turn a refusal
        into a grant, so ``_log_sel`` swallows and warns here. It is the mirror
        of the fail-closed audit on the GRANT path — a grant without a trace is
        refused, while a refusal without a trace is still a refusal.
        """
        self._log_sel(
            caller="safety_override",
            operation="safety_override:activate",
            outcome="denied",
            resources=f"source:{source}, scope:{scope or 'session'}, "
            "reason:approval_modes_policy_denies_yolo",
        )

    def _commit_activation(self, source: str, *, ttl: int, permanent: bool) -> ActivationResult:
        """Shared activation commit: audit fail-closed, then install the grant."""
        # Policy gate: an ``approval_modes`` deny of ``yolo`` disables YOLO
        # entirely, so arming is refused here BEFORE any commit — this covers the
        # session-wide ad-hoc and declared grants that both funnel through here,
        # regardless of config or the runtime toggle. Fail-closed. The refusal is
        # audited so a blocked escalation attempt leaves a trace in the security
        # event log, not only a log line.
        # Authoritative read, not the cached one: arming is a rare, deliberate act
        # whose answer must not be up to a TTL out of date, and this path already
        # does filesystem I/O (the fail-closed SEL audit below), so one governance
        # read costs nothing new. Its async callers offload the whole call.
        if not _resolve_yolo_policy_blocking():
            self._log_policy_refusal(source, scope="")
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")
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
            self._breadcrumb_gen += 1

        # Record that a grant is live so a restart can TELL the operator it is
        # gone. Derived from live state and generation-ordered, so a concurrent
        # revocation cannot be undone by this write.
        self._sync_breadcrumb()

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

        A renewal extends auto-approval authority, so it follows the same
        fail-closed discipline as ``_commit_activation``: the SEL event is
        written with ``critical=True`` BEFORE the deadline moves, and an audit
        failure leaves the grant untouched. The SEL write must not run under
        ``_lock`` (it is I/O and would stall every concurrent ``is_active()``),
        so eligibility is re-verified under the lock before committing — a
        grant deactivated during the audit window must not be resurrected.

        Returns:
            RenewResult.renewed=True on success, False otherwise.
        """
        now_mono = time.monotonic()
        # Resolved BEFORE taking the lock: the resolver reads config from disk,
        # and holding the state lock across that I/O would stall every concurrent
        # is_active() check.
        renew_ttl = min(self.current_adhoc_duration()[0], self._MAX_TTL)

        def _arms(at: float) -> tuple[bool, bool]:
            # (currently_active, in_grace). Caller must hold ``_lock``. A
            # deactivate() on a LIVE grant zeroes ``_expires_at``, so both arms
            # go false; a lapsed grant keeps its past deadline and stays
            # renewable within the grace window.
            currently_active = self._active and self._expires_at > at
            in_grace = (
                not currently_active
                and self._expires_at > 0
                and (at - self._expires_at) <= self._RENEW_GRACE_SECS
            )
            return currently_active, in_grace

        with self._lock:
            # A permanent grant has nothing to extend and must never be
            # downgraded to a finite deadline by a renew.
            if self._active and self._permanent:
                return RenewResult(renewed=True, ttl=-1, source=source)
            began_active, began_in_grace = _arms(now_mono)
            # Every activation bumps the count, so an unchanged count proves no
            # new grant was installed while the audit ran with the lock released.
            count_snapshot = self._activation_count

        if not (began_active or began_in_grace):
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew",
                outcome="denied",
                resources="reason:not_active",
            )
            return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")

        ttl = renew_ttl
        # Audit BEFORE committing — fail-closed with no unrecorded extension:
        # a renewal that cannot be written to the SEL must not move the deadline.
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew",
                outcome="renewed",
                resources=f"source:{source}, new_ttl:{ttl}s",
                critical=True,
            )
        except Exception:
            logger.error("SEL audit failed; refusing safety override renewal", exc_info=True)
            return RenewResult(renewed=False, ttl=0, source=source, reason="audit_failed")

        # The audit ran with the lock released, so re-verify before committing:
        # a concurrent deactivate() during that window must not be undone here,
        # and a concurrent activate() (which re-audits its own grant) must not
        # have its fresh deadline overwritten by this stale renewal.
        commit_mono = time.monotonic()
        commit_refused = False
        refusal_reason = ""
        with self._lock:
            still_active, still_in_grace = _arms(commit_mono)
            # The commit must hold on the ARM the renewal began on. A renewal
            # that began active may not slide into the grace arm: a grant that
            # went from active to lapsed during the audit window either expired
            # naturally near its deadline or was explicitly deactivated (an
            # explicit deactivate of an already-LAPSED grant leaves
            # ``_expires_at`` intact, so lapsed-plus-in-grace cannot distinguish
            # "expired" from "operator said off") — refuse rather than risk
            # undoing an operator's explicit off. A renewal that began in grace
            # may still commit from grace: nothing new lapsed in the window.
            arm_holds = still_active if began_active else (still_active or still_in_grace)
            # Every activation bumps the count, and a permanent grant can only
            # appear via an activation, so this one guard also covers a
            # permanent grant installed during the audit window — the refusal
            # below keeps it untouched.
            if self._activation_count != count_snapshot:
                commit_refused = True
                refusal_reason = "superseded_by_activation"
            elif arm_holds:
                self._active = True
                self._expires_at = commit_mono + ttl
                self._last_renewed_at = commit_mono
                self._last_renewed_by = source
                self._breadcrumb_gen += 1
            else:
                commit_refused = True
                refusal_reason = "not_active_at_commit"

        if commit_refused:
            # The "renewed" event above is already persisted; record that the
            # commit was refused so an auditor does not read a renewal that
            # never took effect. Non-critical: audited-but-not-extended is the
            # safe direction.
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew",
                outcome="denied",
                resources=f"reason:{refusal_reason}",
            )
            return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")

        # The deadline moved, so the record of it has to move too -- otherwise a
        # restart after a renewal would report the OLD remaining time, or none.
        self._sync_breadcrumb()
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
            self._breadcrumb_gen += 1

        # The operator said off, so no restart notice is owed. Published outside
        # the lock for the same reason the SEL write below is: no I/O while
        # holding the state lock.
        self._sync_breadcrumb()

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

        # Policy gate: an ``approval_modes`` deny of ``yolo`` disables
        # auto-approve entirely, including narrow scoped grants. Fail-closed,
        # before commit, and audited like the session-wide arm above.
        # Authoritative, for the same reason as the session-wide arm above.
        if not _resolve_yolo_policy_blocking():
            self._log_policy_refusal(source, scope=scope)
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

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
        # A grant policy no longer permits must not have its expiry slid forward:
        # renewal is what keeps an active run's grant alive indefinitely inside the
        # 24h ceiling, so sliding it after a deny would extend the very authority
        # the deny withdrew. The grant is REVOKED rather than left to lapse, for the
        # reason spelled out on ``is_active``. Reported as not-renewed, the same
        # shape as an absent grant or a reached ceiling, so no caller needs a branch.
        verdict = yolo_policy_verdict()
        if verdict != _YOLO_PERMITTED:
            # Same three-state split as ``is_scope_active`` above: only a definite
            # deny withdraws the grant. On UNKNOWN the slide is refused -- which is
            # the safe direction, since a renewal extends authority -- but the grant
            # is left intact for the refresh to settle.
            if verdict == _YOLO_DENIED:
                self.deactivate_scope(scope)
            return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")

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
        # Policy first, exactly as in ``is_active()``, and it REVOKES rather than
        # masks -- see the reasoning there. Gating only at arming left the scoped
        # grant honoured until its own TTL, and this is the consult point
        # ``task_executor`` reads before EVERY approval, which is what made the gap
        # reachable for up to 24h. Reading the verdict costs no filesystem access
        # (see ``yolo_policy_verdict``), which is what makes it safe here.
        #
        # Three-state, for the same reason as ``is_active`` and with a sharper
        # consequence here. This runs before EVERY approval in an unattended run, and
        # ``deactivate_scope`` pops the entry permanently while ``task_executor`` then
        # clears ``run.auto_approve`` -- nothing re-arms it. So collapsing UNKNOWN
        # onto revocation meant a mid-session ``policy_distribution.apply_ceiling``
        # that STILL PERMITS yolo would stall a legitimately granted unattended run
        # for its whole remainder, on nothing but the off-loop refresh window.
        verdict = yolo_policy_verdict()
        if verdict != _YOLO_PERMITTED:
            if verdict == _YOLO_DENIED:
                self.deactivate_scope(scope)
            return False

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
        # Policy first, and it outranks BOTH the deadline and a declared grant.
        # Gating only at arming left a live grant honoured until its own TTL, so an
        # admin who denied ``yolo`` mid-session kept auto-approving every tool for
        # up to 24h -- the control announced a state it was not enforcing. Checked
        # here rather than at each of the ~8 call sites because this predicate IS
        # the consult point every transport passes to ``TurnDriver``.
        #
        # REVOKE, do not merely mask. An earlier revision left ``_active`` set and
        # only reported False, so relaxing the policy would restore the grant with
        # nothing to re-arm. That is wrong, because this same predicate is what every
        # "is there a grant to clear?" caller reads: Slack's ``!yolo off`` does
        # `if is_yolo_mode(): disable_yolo()`, so inside a denial window it reported
        # "already off" and cleared NOTHING -- and a later policy relaxation then
        # resurrected auto-approve the operator had explicitly revoked. Tearing the
        # grant down makes both readings agree, and costs only that a policy which
        # denies then relaxes needs a fresh arm, which is the honest outcome anyway.
        #
        # Clearing ``_active`` is NOT the whole revocation. A dashboard grant also
        # writes ``approval_policy="auto"`` onto the slots, and a spawned subagent
        # reads that policy rather than this flag -- so dropping only the flag left
        # spawn admission and every child tool auto-approved against a policy that
        # denies it. The TTL-lapse path below already solves this by firing
        # ``_on_expired``, whose handler resets those policies and clears the shared
        # trust mapping; a policy revocation owes the same cleanup, so it fires the
        # same callback rather than growing a second, divergent teardown.
        #
        # Gated on there actually being a grant, which makes both the teardown and
        # the callback fire EXACTLY ONCE: this runs per tool call, and re-firing a
        # handler that broadcasts and rewrites slot policies on every call would be
        # its own defect. Reading the verdict itself costs no filesystem access (see
        # ``yolo_policy_verdict``).
        #
        # ONLY a definite DENIED revokes. ``yolo_policy_verdict`` also answers
        # UNKNOWN -- "current policy could not be read" -- and that must stop
        # auto-approval WITHOUT tearing the grant down, because the teardown is
        # permanent and the unknown window opens on every ceiling install, including
        # ones that still permit YOLO. Treating unknown as a denial would delete
        # live grants on unrelated policy refreshes; treating it as a permit is the
        # bypass this whole predicate exists to prevent. Neither, so: no approval,
        # no revocation, and the scheduled refresh settles it within one round trip.
        #
        # The grant test is ``self._active`` ALONE. It used to be
        # ``self._active or self._expires_at > 0.0``, which double-fired: the
        # natural-expiry branch below clears ``_active`` but deliberately leaves
        # ``_expires_at`` set (``deactivate`` reads that nonzero deadline to tell
        # "lapsed" from "never armed", so it can still SEL-record an explicit off
        # after a lapse). A later deny then saw the stale deadline, called
        # ``deactivate`` again and fired a SECOND expiry teardown for an
        # already-expired grant -- a duplicate "Safety override expired" DM to the
        # owner and a redundant ``yolo_expired`` broadcast. ``_active`` alone still
        # covers every grant that is actually being honoured, live or declared.
        verdict = yolo_policy_verdict()
        if verdict != _YOLO_PERMITTED:
            if verdict == _YOLO_DENIED:
                with self._lock:
                    had_grant = self._active
                if had_grant:
                    self.deactivate("policy")
                    cb = self._on_expired
                    if cb is not None:
                        try:
                            cb("policy")
                        except Exception:
                            logger.warning(
                                "on_expired callback raised after a policy revocation",
                                exc_info=True,
                            )
            return False

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
            self._breadcrumb_gen += 1

        # The grant reached its own deadline, so a later restart owes no notice:
        # nothing was taken from the operator that the clock was not taking.
        self._sync_breadcrumb()

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

    def has_grant(self) -> bool:
        """Whether a grant EXISTS, ignoring policy entirely.

        Deliberately not ``is_active``. That predicate answers "may a tool be
        auto-approved right now", which policy can veto -- and an explicit off is a
        different question: "is there something to tear down". Reading the
        policy-filtered answer for it inverted the control. During the UNKNOWN window
        ``is_active`` reports False, so Slack's ``if is_yolo_mode(): disable_yolo()``
        skipped the teardown, reported "already off", and left the grant standing --
        which then RESUMED once the refresh settled. The operator had revoked
        auto-approve and it came back.

        This is the same class as the mask-vs-revoke defect on ``is_active``: the two
        readings of one flag must not disagree. The fix is to let an explicit
        revocation see the grant regardless of what policy currently says about it.
        """
        with self._lock:
            return self._active

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

    def _sync_breadcrumb(self) -> None:
        """Publish the breadcrumb to match the grant as it is RIGHT NOW.

        Called after EVERY state transition (activation, renewal, explicit
        deactivation, lazy expiry) instead of each site writing or clearing from
        its own locals. Two properties come from that:

        * The content is derived from live state, so a write that is delayed past
          a concurrent revocation cannot resurrect the revoked grant -- the late
          writer re-reads and publishes the same "no grant" the revocation
          wanted.
        * A generation counter, bumped under the state lock by each transition,
          orders the writes: a sync holding an older generation than the one
          already published returns without touching the file, so two overlapping
          transitions cannot land out of order (found in review).

        The state lock is held only to snapshot -- never across the file I/O,
        which is this module's standing rule and the reason a second lock exists.
        The I/O itself is handed to a worker thread, because these callers sit on
        the gateway's event loop (found in review).
        """
        now_mono = time.monotonic()
        with self._lock:
            gen = self._breadcrumb_gen
            permanent = self._permanent
            active = self._active and (permanent or self._expires_at > now_mono)
            source = self._source
            remaining = 0 if permanent else max(0, int(self._expires_at - now_mono))

        # Wall clock, because only another process reads it. Computed HERE rather
        # than on the worker so a queued publish carries the deadline as it was at
        # the transition, not as it is whenever the worker gets to it. A permanent
        # grant records no remaining time, so even if the ``permanent`` guard in
        # ``take_dropped_grant`` were removed it would fail safe to silence.
        expires_at_wall = datetime.now(tz=timezone.utc).timestamp() + remaining

        def _publish() -> None:
            with _breadcrumb_io_lock:
                if gen < self._breadcrumb_published_gen:
                    return
                self._breadcrumb_published_gen = gen
                if not active:
                    _clear_breadcrumb()
                    return
                _write_breadcrumb(
                    source=source,
                    expires_at_wall=expires_at_wall,
                    permanent=permanent,
                )

        # Wrapped for the same reason the write itself is: the grant is ALREADY
        # committed by the time this runs, so an exception escaping here would
        # report a failed activation while tools are in fact auto-approved
        # (found in review). Starting the worker can fail on its own -- a thread
        # quota is a real limit -- so the enqueue is inside the guard, not just
        # the file I/O.
        try:
            _enqueue_breadcrumb(_publish)
        except Exception:
            logger.debug("safety override: breadcrumb enqueue failed", exc_info=True)

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

# ─── Restart-drop breadcrumb ─────────────────────────────────────────────────
#
# A grant lives in memory only, so a restart ends it. That is the DESIGNED
# behaviour and this module does not change it -- what it changes is that the
# ending used to be SILENT: an operator who granted six hours of auto-approval,
# then restarted the gateway an hour later, got no reply telling them the
# remaining five hours were gone. The next unattended run simply stopped and
# waited for an approval nobody was watching for.
#
# So the file below records that a grant WAS live, never that it may resume. It
# holds the wall-clock deadline (the in-memory deadlines are
# ``time.monotonic()``, which means nothing to another process), the source, and
# whether the grant had an expiry at all. Startup reads it once, tells the
# operator when a TIMED grant still had time left, and deletes it.
#
# It is deliberately NOT signed, and that follows from what it can do: the file
# confers no authority, so the worst a forged one achieves is a spurious "your
# override was dropped" notice. A restored GRANT would need signing -- and a
# planned-vs-crash discriminator this codebase does not have -- which is exactly
# why restoring one is not what this does.
#
# One record per data home is correct, not a shared-state hazard: the file lives
# in ``config_dir()``, which is the very directory ``gateway_lock`` holds an
# exclusive advisory flock on for a gateway's whole lifetime, and a second
# gateway on the same home is REFUSED at startup rather than allowed to race
# (see gateway_lock's module docstring -- the invariant exists because shared-home
# writers clobber ``sessions/*.jsonl``). So there is never a sibling gateway to
# consume this record out from under the one that wrote it.
_BREADCRUMB_FILE = "safety_override_last_grant.json"

#: Serializes breadcrumb I/O. Separate from the state lock ON PURPOSE: this
#: module's rule is that no I/O happens while the state lock is held (the SEL
#: writes obey it too), so ordering the file against concurrent transitions
#: needs its own lock plus the generation counter below -- not the state lock.
_breadcrumb_io_lock = threading.Lock()

#: Publishes run on ONE long-lived daemon worker, never inline. ``activate`` and
#: ``is_active`` are called from async request handlers (a Slack YOLO toggle, and
#: the approval-policy read on every dashboard turn), so a synchronous
#: ``atomic_write`` there would put filesystem latency on the gateway's event
#: loop -- and a slow or stalled filesystem would then stall gateway traffic and
#: the heartbeat with it (found in review). A queue rather than a thread per
#: transition so thread churn is bounded, and FIFO ordering reinforces the
#: generation guard.
_breadcrumb_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
_breadcrumb_worker: Optional[threading.Thread] = None
_breadcrumb_worker_lock = threading.Lock()
#: Set while nothing is queued AND nothing is mid-write. ``flush`` waits on this
#: rather than joining the queue: a write stalled on a hung filesystem would make
#: an unconditional ``join()`` wait forever, and the caller is a restart path, so
#: blocking it is worse than losing the record (found in review).
_breadcrumb_idle = threading.Event()
_breadcrumb_idle.set()
_breadcrumb_pending = 0
_breadcrumb_pending_lock = threading.Lock()

#: A record is ~120 bytes. Reading it unbounded let anything that can write into
#: the data home turn startup into a memory-exhaustion restart loop (found in
#: review), so an oversized file is discarded rather than parsed. Generous enough
#: that a future field cannot trip it.
_BREADCRUMB_MAX_BYTES = 4096

#: Identity of THIS process image, minted at import. Not the PID: both instrumented
#: restart paths end in ``os.execv``, which replaces the image but PRESERVES the
#: pid -- so a pid comparison would read the previous image's record as our own and
#: swallow the notice on exactly the restarts this feature exists for (found in
#: review). ``process_start_time`` is no better, since exec preserves that too. A
#: fresh import is the one thing an exec guarantees, so a value minted here is the
#: discriminator.
_IMAGE_TOKEN = uuid.uuid4().hex


def _breadcrumb_pump() -> None:
    global _breadcrumb_pending
    while True:
        job = _breadcrumb_queue.get()
        try:
            job()
        except Exception:
            logger.debug("safety override: breadcrumb publish failed", exc_info=True)
        finally:
            _breadcrumb_queue.task_done()
            with _breadcrumb_pending_lock:
                _breadcrumb_pending -= 1
                if _breadcrumb_pending <= 0:
                    _breadcrumb_pending = 0
                    _breadcrumb_idle.set()


def _enqueue_breadcrumb(job: Callable[[], None]) -> None:
    """Hand a publish to the worker, starting it on first use."""
    global _breadcrumb_worker, _breadcrumb_pending
    with _breadcrumb_worker_lock:
        if _breadcrumb_worker is None or not _breadcrumb_worker.is_alive():
            _breadcrumb_worker = threading.Thread(
                target=_breadcrumb_pump, name="kirocrew-safety-breadcrumb", daemon=True
            )
            _breadcrumb_worker.start()
    with _breadcrumb_pending_lock:
        _breadcrumb_pending += 1
        _breadcrumb_idle.clear()
    _breadcrumb_queue.put(job)


def flush_breadcrumb_writes(timeout: float = 5.0) -> bool:
    """Wait up to *timeout* for queued publishes to land; report whether they did.

    STRICTLY bounded, by construction rather than by a polling loop. Callers are
    restart paths: a flush that could wait forever on a stalled write would freeze
    a gateway that was trying to re-exec, which is a worse failure than losing the
    record it was trying to save (found in review). Also used by tests.
    """
    return _breadcrumb_idle.wait(max(0.0, timeout))


def _breadcrumb_path() -> Path:
    return config_dir() / _BREADCRUMB_FILE


def _write_breadcrumb(*, source: str, expires_at_wall: float, permanent: bool) -> None:
    """Record that a grant is live. Best-effort: never raises into the grant path.

    A failed write costs the operator a notice, never a grant, so it must not
    fail an activation -- and above all must not fail a DEACTIVATION, where
    raising would leave auto-approval on.
    """
    try:
        payload = json.dumps(
            {
                "version": 1,
                "source": source,
                # Wall clock, because the reader is a different process.
                "expires_at": expires_at_wall,
                "permanent": permanent,
                # Whose record this is. A reader in the SAME process image must not
                # treat it as a dropped grant -- that is what lets the read happen
                # anywhere in startup rather than having to run before this
                # process can write one of its own (found in review). Keyed on the
                # import-time nonce, NOT the pid: os.execv keeps the pid.
                "image": _IMAGE_TOKEN,
                # Whose record this is. A reader in the SAME process must not treat
                # it as a dropped grant -- that is what lets the read happen
                # anywhere in startup rather than having to run before this
                # process can write one of its own (found in review).
            }
        )
        # 0600: the record names the auto-approval posture and its deadline.
        atomic_write(_breadcrumb_path(), payload, mode=0o600)
    except Exception:
        logger.debug("safety override: breadcrumb write failed", exc_info=True)


def _clear_breadcrumb() -> None:
    """Drop the record. Best-effort, for the same reason the write is."""
    try:
        _breadcrumb_path().unlink(missing_ok=True)
    except Exception:
        logger.debug("safety override: breadcrumb clear failed", exc_info=True)


def take_dropped_grant() -> Optional[DroppedGrant]:
    """Consume the breadcrumb; return a notice when a TIMED grant lost time.

    Serialized against the publisher: ``_consume_breadcrumb`` runs under
    ``_breadcrumb_io_lock`` from the open through the identity check to the
    clear, because a publish landing in the middle of that span would be
    UNLINKED by the clear -- deleting the record for a grant that is live right
    now and leaving the next restart with nothing to report (found in review).
    The audit below stays outside the lock: it is unrelated I/O.
    """
    with _breadcrumb_io_lock:
        dropped = _consume_breadcrumb()
    if dropped is None:
        return None
    # Audited like every other posture change in this module, so the operator's
    # lost grant is on the durable record and not only in a notification.
    try:
        sel().log_api_access(
            caller="safety_override",
            operation="safety_override:dropped_by_restart",
            outcome="expired",
            source="safety_override",
            resources=f"source:{dropped.source}, remaining:{dropped.remaining_secs}s",
        )
    except Exception:
        logger.debug("safety override: dropped-grant audit failed", exc_info=True)
    return dropped


def _consume_breadcrumb() -> Optional[DroppedGrant]:
    """Read and consume the record. Caller MUST hold ``_breadcrumb_io_lock``.

    Single-shot by construction: the record is deleted whatever the verdict, so
    one dropped grant cannot notify twice, and a stale record from an older
    install cannot notify forever.

    Returns ``None`` -- no notice is owed -- in three cases:

    * **No record.** No grant was live.
    * **The grant had no expiry** (``permanent``). A DECLARED grant is
      re-established from config on this very startup, so it was not lost at
      all; an ``until_shutdown`` grant is already contracted to the operator as
      "stays on until Kiro Crew restarts", so its ending is the documented
      behaviour rather than news.
    * **The deadline has passed.** The grant would have expired on its own by
      now, so the restart cost the operator nothing.
    """
    path = _breadcrumb_path()
    # Opened by descriptor rather than read by name, because the SHAPE of the file
    # matters as much as its size. A FIFO planted at this path reports st_size 0,
    # so it sails through a size check and then blocks FOREVER on read -- which
    # would hang gateway initialization before readiness (found in review). So:
    # O_NONBLOCK so no open or read can wait on a writer, O_NOFOLLOW so a symlink
    # cannot redirect the read at something else, and an fstat that refuses
    # anything that is not a regular file.
    #
    # Neither flag exists on Windows, so an lstat check carries the symlink
    # refusal there: without it the link is FOLLOWED and its target parses
    # normally (caught by the Windows CI shard). O_NOFOLLOW stays as the
    # race-free guard where it exists -- lstat-then-open is TOCTOU, which is
    # tolerable only because a forged record confers no authority.
    _nonblock = getattr(os, "O_NONBLOCK", 0)
    _nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink():
            logger.warning("safety override: discarding a restart record that is a link")
            _clear_breadcrumb()
            return None
    except OSError:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | _nonblock | _nofollow)
    except FileNotFoundError:
        return None
    except OSError:
        # ELOOP from O_NOFOLLOW lands here: a symlink IS a refusal, not an error
        # to investigate.
        logger.debug("safety override: breadcrumb could not be opened", exc_info=True)
        _clear_breadcrumb()
        return None

    # The verdict is decided while the descriptor is open, but every unlink
    # happens AFTER it is closed: Windows refuses to remove an open file, so
    # clearing here left an oversized or malformed record in place to be
    # rediscovered on every subsequent startup (caught by the Windows CI shard).
    raw: Optional[str] = None
    discard = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            logger.warning("safety override: discarding a restart record that is not a file")
            discard = True
        elif info.st_size > _BREADCRUMB_MAX_BYTES:
            logger.warning(
                "safety override: discarding an oversized restart record (%d bytes)",
                info.st_size,
            )
            discard = True
        else:
            # Capped at the bound regardless of what fstat said, so a file that
            # grew between the stat and the read cannot exceed it either.
            raw = os.read(fd, _BREADCRUMB_MAX_BYTES).decode("utf-8", errors="replace")
    except Exception:
        logger.debug("safety override: breadcrumb read failed", exc_info=True)
        discard = True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if discard or raw is None:
        _clear_breadcrumb()
        return None

    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            _clear_breadcrumb()
            return None
        writer_image = str(record.get("image") or "")
        permanent = bool(record.get("permanent"))
        expires_at = float(record.get("expires_at") or 0.0)
        source = str(record.get("source") or "")
    except Exception:
        logger.debug("safety override: breadcrumb unreadable", exc_info=True)
        _clear_breadcrumb()
        return None

    # THIS process image's own live grant. Left untouched -- consuming it would
    # delete the record for a grant that is in force, so a later restart would
    # have nothing to report. This is also what frees the read from having to run
    # before the startup grant is applied (found in review). Compared on the
    # import-time nonce, because os.execv preserves the pid and every restart this
    # feature instruments goes through exec.
    if writer_image and writer_image == _IMAGE_TOKEN:
        return None

    # THIS process's own live grant. Left untouched -- consuming it would delete
    # the record for a grant that is in force, so a later restart would have
    # nothing to report. This is also what frees the read from having to run
    # before the startup grant is applied (found in review).

    # From here the record belongs to a previous process, so it is consumed
    # whatever the verdict: one dropped grant cannot notify twice, and a record
    # left by an older install cannot notify forever.
    _clear_breadcrumb()

    if permanent:
        return None

    remaining = int(expires_at - datetime.now(tz=timezone.utc).timestamp())
    if remaining <= 0:
        return None

    dropped = DroppedGrant(
        source=source,
        remaining_secs=remaining,
    )
    # The audit is emitted by the CALLER, outside the lock -- see
    # ``take_dropped_grant``. Nothing unrelated to the file belongs in this span.
    return dropped


def describe_dropped_grant(dropped: DroppedGrant) -> str:
    """One channel-neutral line telling the operator what they lost."""
    return (
        f"Auto-approve (YOLO) is OFF: the grant from {dropped.source or 'an earlier session'} "
        f"had {fmt_grant_duration(dropped.remaining_secs)} left when Kiro Crew restarted. "
        "Grants live in memory only, so a restart ends them. Re-enable it if you still want it."
    )


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
    """Reset the singleton.  Intended for use in tests only.

    Drops the cached ``approval_modes`` verdict too. The cache is module state with
    a time-based TTL, so without this a test that ran under a permissive policy
    leaks its verdict into the next test for the length of the TTL -- the grant
    would be honoured against a policy that denies it, for reasons that have
    nothing to do with the code under test.
    """
    global _singleton
    with _singleton_lock:
        _singleton = None
    reset_yolo_policy_cache()


_PERMANENT_MEMBER = "permanent"
_UNTIL_SHUTDOWN_MEMBER = "until_shutdown"
_GOVERNANCE_SCOPE = "yolo_duration"
_APPROVAL_MODES_SCOPE = "approval_modes"
_YOLO_MODE = "yolo"


# ── ``approval_modes`` verdict for YOLO: cached, and never resolved on the loop ──
#
# Resolving the scope walks the governance profiles dir (``iterdir`` + per-file
# ``stat``). Two call shapes make a per-call read unacceptable:
#
# * ``is_active()`` is the auto-approve predicate every transport passes to
#   ``TurnDriver`` (``auto_approve_session=lambda: safety_override().is_active()``),
#   so it runs per TOOL CALL.
# * arming reaches this module from the event loop through synchronous callers
#   (``taskrunner._grant_run_trust``, the Slack slash handlers).
#
# So there are two forms, and which one a caller wants follows from its frequency:
#
# * ``yolo_policy_permits()`` -- pure memory, for the per-tool-call predicate. Hands
#   a stale value back while a refresh runs on a worker thread. Staleness is bounded
#   by the TTL *within one ceiling*; across a ceiling CHANGE the TTL says nothing, so
#   the entry carries the governance generation it was resolved under and a newly
#   installed ceiling EXPIRES it at once rather than letting a permit primed under
#   the previous policy live out the remaining TTL.
# * ``_resolve_yolo_policy_blocking()`` -- authoritative, for ARMING. A deliberate,
#   rare act should not be decided by a value up to a TTL old, and arming already
#   does filesystem I/O for its fail-closed audit. Its async callers offload it.
_YOLO_POLICY_TTL = 5.0
#: Serialises the resolve-and-stamp sequence in ``_resolve_yolo_policy_blocking``.
#:
#: That sequence is a read-modify-write across a filesystem read -- sample the
#: generation, resolve, sample it again, store -- and it runs from MORE THAN ONE
#: thread: arming offloads it via ``asyncio.to_thread``, and the off-loop refresh
#: scheduled by ``yolo_policy_verdict`` runs it in another worker. Interleaved, one
#: caller's post-read sample can straddle the other's store, so a stamp is written
#: for a generation the resolve did not actually observe -- and the next reader then
#: either serves a verdict resolved under a different ceiling, or (with the entry
#: left un-stamped) reads UNKNOWN and fails closed on a grant that was just armed
#: successfully. Under parallel load that surfaced as a yolo arm being honoured in
#: one run and refused in the next.
#:
#: Held ACROSS the governance read, deliberately. Serialising concurrent resolves is
#: the point: the second caller waits and then finds a fresh entry, rather than
#: duplicating the same filesystem walk and racing to store it. Never taken on the
#: event loop -- every caller of this function is already off-loop or a sync CLI/test
#: path.
_yolo_policy_lock = threading.Lock()
#: The three verdict states. See ``yolo_policy_verdict`` for why UNKNOWN is a state
#: of its own rather than folded into either boolean.
_YOLO_PERMITTED = "permitted"
_YOLO_DENIED = "denied"
_YOLO_UNKNOWN = "unknown"
#: ``(resolved_at_monotonic, permitted, governance_generation)``. The generation is
#: what makes a policy CHANGE invalidate the entry immediately instead of at the end
#: of the TTL: the TTL only bounds staleness while the same ceiling stays installed.
#:
#: ``resolved_at`` is MONOTONIC, not wall clock. ``time.time()`` can move backwards
#: -- an NTP step, a VM restore, an operator correcting the clock -- and a backwards
#: jump makes ``now - resolved_at`` negative, so the entry reads as fresh and the TTL
#: never elapses again. On a safety predicate that is a permit with no expiry at all,
#: for a reason that has nothing to do with policy.
_yolo_policy_cache: tuple[float, bool, int] = (0.0, True, -1)
#: The in-flight refresh, as ``(loop, task)`` -- deliberately NOT a bare boolean.
#:
#: A boolean could not be un-stuck. It was set before the task was created and
#: cleared in that task's ``finally``, so a loop torn down while the refresh was
#: still pending left it ``True`` with nothing alive to clear it: every later call
#: took the early return, and the verdict cache then never refreshed again for the
#: whole life of the process. That is a stuck-open cache on a SAFETY predicate --
#: a policy tightening would stop landing at all. A test suite hits it immediately
#: (a fresh loop per test); the gateway hits it on any loop replacement.
#:
#: Recording the LOOP is what makes staleness decidable: a task still pending on a
#: loop that is gone, or on a different loop than this caller's, is not an
#: in-flight refresh *for this caller* and must not suppress a fresh one. Holding
#: the task STRONGLY is the other half -- asyncio keeps only a weak reference to a
#: bare ``create_task`` result, so a fire-and-forget task can be collected
#: mid-flight, which is the "Task was destroyed but it is pending!" noise this also
#: removes.
_yolo_policy_refresh: tuple[asyncio.AbstractEventLoop, asyncio.Task[None]] | None = None


def _governance_generation() -> int:
    """Current governance generation, or ``-1`` when it cannot be read.

    ``-1`` never equals a stored generation, so an unreadable counter makes every
    read treat the cache as belonging to a different ceiling -- it resolves rather
    than trusting a value it cannot date.
    """
    try:
        return int(governance_generation())
    except Exception:
        logger.debug("governance generation unavailable", exc_info=True)
        return -1


def _resolve_yolo_policy_blocking() -> bool:
    """Resolve the YOLO verdict and store it. MUST run off the event loop.

    Reads the generation BEFORE resolving, so a ceiling installed while this was
    resolving leaves the entry stamped with the older generation -- which the reader
    then treats as stale rather than as a fresh answer for the new policy.
    """
    global _yolo_policy_cache
    with _yolo_policy_lock:
        return _resolve_yolo_policy_locked()


def _resolve_yolo_policy_locked() -> bool:
    """The body of the resolve. Caller MUST hold ``_yolo_policy_lock``."""
    global _yolo_policy_cache
    # The generation is read on BOTH sides and the answer is stamped only when it
    # did not move across the resolve. Either one-sided reading is wrong, and each
    # fails in its own direction:
    #
    # * stamp the PRE-read generation and the first governance read in a process
    #   mis-stamps itself. That read is what installs the lazy default context, so
    #   it bumps the counter as a side effect; the answer then claimed a ceiling
    #   that was already superseded, every later read saw a mismatch, and the
    #   verdict was pinned at UNKNOWN for the life of the process -- fail-closed,
    #   but permanently, which breaks auto-approve outright.
    # * stamp the POST-read generation and the mirror error appears: a ceiling
    #   installed DURING the read would claim an answer taken under the old one,
    #   which is the stale-permit class this whole verdict exists to close.
    #
    # Three attempts because the lazy-install bump happens at most once per
    # process, so a stable pair is reached immediately after it.
    for _ in range(3):
        before = _governance_generation()
        try:
            permitted = bool(approval_mode_permitted(_YOLO_MODE))
        except Exception:
            logger.debug("could not resolve approval_modes for yolo", exc_info=True)
            # Fail CLOSED, and deliberately do not restamp. Returning the previous
            # verdict here used to hand a stale ``True`` to the two authoritative
            # callers -- arming, and the status field -- so a governance read that
            # kept failing let a grant be armed against a policy nobody could read.
            # Leaving the entry un-restamped is also what keeps
            # ``yolo_policy_verdict`` on UNKNOWN rather than letting the old
            # generation masquerade as current.
            return False
        if _governance_generation() == before:
            _yolo_policy_cache = (time.monotonic(), permitted, before)
            return permitted
    logger.debug("governance generation kept moving across the yolo resolve")
    return False


def _schedule_yolo_policy_refresh() -> None:
    """Refresh off-loop, at most one refresh in flight PER LOOP."""
    global _yolo_policy_refresh
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop: nothing to protect, so resolve inline. This is also what
        # keeps a cold cache from handing the permissive default to the first caller
        # in a sync context (CLI, tests).
        _resolve_yolo_policy_blocking()
        return
    pending = _yolo_policy_refresh
    if pending is not None and pending[0] is loop and not pending[1].done():
        # A refresh for THIS loop is genuinely still running. Any other shape --
        # a finished task, or one belonging to a loop that has since been replaced
        # -- says nothing about this loop, so it must NOT suppress a fresh refresh.
        return

    async def _refresh() -> None:
        global _yolo_policy_refresh
        # Identity captured HERE, not in the ``finally``. The finally can run while
        # the loop is tearing this task down (``GeneratorExit``), and
        # ``asyncio.current_task()`` needs a RUNNING loop -- so asking there raised
        # RuntimeError from inside an exception handler, which surfaces as an
        # UNRAISABLE exception nothing can catch. On entry a running loop is
        # guaranteed, and the identity is all the finally actually needs.
        me = asyncio.current_task()
        try:
            await asyncio.to_thread(_resolve_yolo_policy_blocking)
        except Exception:
            logger.debug("yolo policy refresh failed", exc_info=True)
        finally:
            # Retract ONLY our own record. Clearing unconditionally would let a
            # finishing task erase a newer one and permit two concurrent refreshes.
            if _yolo_policy_refresh is not None and _yolo_policy_refresh[1] is me:
                _yolo_policy_refresh = None

    # create_task first, then store: this runs ON the loop thread, so the task
    # cannot start before the record exists for its ``finally`` to match against.
    _yolo_policy_refresh = (loop, loop.create_task(_refresh()))


def yolo_policy_verdict() -> str:
    """``permitted`` / ``denied`` / ``unknown``. Pure memory; safe on the loop.

    THREE states, not two, and that is the whole point of this function.

    Four consecutive review rounds on this span all reported the same shape: a
    permit being honoured that no current resolve backed. Each was patched at its
    own site -- gate at arming, then key the cache to the governance generation,
    then move the resolve off the loop -- and the next round found another way in,
    because a boolean cannot express the state that actually causes it. "I have
    not been able to read current policy" is not "policy allows this", and it is
    not "policy forbids this" either. Collapsing it onto either one is what kept
    reopening the hole:

    * collapse UNKNOWN onto PERMITTED and a stale ``True`` auto-approves every
      tool -- the finding, in three different disguises. The last one needed no
      exotic timing at all: ``_resolve_yolo_policy_blocking`` returns without
      restamping when the resolve raises, so a persistently failing resolve served
      the old permit indefinitely.
    * collapse UNKNOWN onto DENIED and ``is_active`` revokes -- permanently, by
      design -- on a window that opens on EVERY ceiling install, including ones
      that still permit YOLO. That destroys live grants on unrelated refreshes.

    So UNKNOWN is returned as itself and each caller collapses it in the direction
    that is safe for what IT decides: approval fails closed (see
    ``yolo_policy_permits``), revocation does not fire (see ``is_active``).

    UNKNOWN is exactly "the entry was not resolved under the ceiling now
    installed", which is decidable from memory because
    ``_resolve_yolo_policy_blocking`` stamps the generation ONLY on a successful
    resolve. Detecting it therefore costs no filesystem access, and the refresh it
    schedules stays on a worker thread.
    """
    ts, cached, generation = _yolo_policy_cache
    if generation != _governance_generation():
        _schedule_yolo_policy_refresh()
        # Re-read: with no running loop the schedule resolves INLINE, which can
        # settle the verdict on this very call. With a loop it did not, and the
        # honest answer is that current policy is not yet known.
        ts, cached, generation = _yolo_policy_cache
        if generation != _governance_generation():
            return _YOLO_UNKNOWN
    if time.monotonic() - ts >= _YOLO_POLICY_TTL:
        _schedule_yolo_policy_refresh()
        ts, cached, generation = _yolo_policy_cache
    return _YOLO_PERMITTED if cached else _YOLO_DENIED


def yolo_policy_permits() -> bool:
    """Whether policy PERMITS YOLO. Fails closed on UNKNOWN.

    The approval direction: a tool must not be auto-approved on a verdict that no
    current resolve backs. This is the predicate every transport hands to
    ``TurnDriver``, so it is deliberately the strict reading -- but see
    ``is_active``, which must NOT read a failure to resolve as a revocation.
    """
    return yolo_policy_verdict() == _YOLO_PERMITTED


def reset_yolo_policy_cache() -> None:
    """Drop the cached verdict. For tests and for an explicit policy reload."""
    global _yolo_policy_cache, _yolo_policy_refresh
    _yolo_policy_cache = (0.0, True, -1)
    _yolo_policy_refresh = None


# ── The status field, derived from the SAME verdict the enforcement path reads ──
#
# ``approval_modes`` governs exactly one mode today: ``yolo``. ``normal`` is the
# interactive floor, and ``trust`` / ``trust_reads`` are non-deniable because their
# live consumption predicates are not gated -- a policy naming any of the three is
# refused at parse time (see the ``SCOPE_CATALOG`` entry).
#
# These two helpers exist so the dashboard's status field and the per-tool-call
# enforcement predicate cannot disagree. ``dashboard/state.py`` used to keep its own
# TTL cache of the same question, which had already drifted: this one is
# generation-aware, so a tightening lands at once, while a TTL-only copy could report
# YOLO selectable for up to a TTL after enforcement stopped honouring it. One
# mechanism cannot drift from itself.


def cached_disabled_approval_modes() -> list[str]:
    """Modes the policy forbids, from memory. Safe on the event loop.

    Backs ``status_snapshot``, which is emitted on the 5s WS push, so it must never
    touch the filesystem. Presentation only -- enforcement is ``api_chat_mode``, the
    slot-approve gate, and arming in this module.
    """
    return [] if yolo_policy_permits() else [_YOLO_MODE]


def resolve_disabled_approval_modes_blocking() -> list[str]:
    """Authoritative form. Touches the filesystem, so callers MUST be off-loop.

    Also primes the shared cache, which is what lets the reader above stay
    filesystem-free.
    """
    return [] if _resolve_yolo_policy_blocking() else [_YOLO_MODE]


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


def _non_deniable_approval_modes() -> tuple[str, ...]:
    """Modes the ``approval_modes`` scope may never forbid, read from the catalog.

    Read rather than hardcoded so this function and the parse-time refusal cannot
    drift apart: the catalog entry is the single declaration of what is deniable.
    Falls back to the interactive floor alone if the governance layer is missing,
    which is the safe direction -- it only ever makes this check consult policy for
    MORE modes, never fewer.
    """
    try:
        from kiro_crew.platform.governance import SCOPE_CATALOG

        spec = SCOPE_CATALOG.get(_APPROVAL_MODES_SCOPE)
        return tuple(getattr(spec, "always_permitted", ()) or ("normal",))
    except Exception:
        logger.debug("catalog unavailable; assuming only 'normal' is non-deniable")
        return ("normal",)


def approval_mode_permitted(mode: str) -> bool:
    """True when policy allows the dashboard approval *mode* to be selected.

    Backed by the ``approval_modes`` deny-list scope, e.g.
    ``{"approval_modes": {"mode": "deny", "deny": ["yolo"]}}``.

    A **non-deniable** mode short-circuits to True without consulting governance at
    all. That is what makes "non-deniable" mean the same thing at runtime as it does
    at parse time: the parse-time refusal stops an admin from WRITING such a deny,
    and this stops a governance-evaluation error from producing one anyway. Without
    it the ``fail_closed=True`` below could deny ``trust`` on a resolve error --
    refusing a mode whose enforcement this scope does not even implement, which
    surfaced as an unrelated trust grant silently failing.

    Everything else is evaluated against the HOST profile with ``fail_closed=True``,
    so a governance-evaluation error denies the riskier auto-approve mode rather than
    silently granting it. With no policy configured the scope is ungoverned and
    permits every mode, so a solo operator's picker is unchanged.
    """
    if mode in _non_deniable_approval_modes():
        return True
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )
    except Exception:
        logger.debug("governance layer unavailable; permitting mode %s", mode, exc_info=True)
        return True
    decision = governance_permits(
        _APPROVAL_MODES_SCOPE,
        mode,
        session_key=HOST_SESSION_KEY,
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))


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


# ── User-facing grant-lifetime text (channel-neutral) ──

NO_EXPIRY_TEXT = "stays on until Kiro Crew restarts"


def fmt_grant_duration(secs: int) -> str:
    """Render an ad-hoc TTL for a user-facing message (e.g. "6h", "30min")."""
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    return f"{secs // 60}min"


def describe_grant_lifetime() -> str:
    """Describe the LIVE grant's lifetime truthfully.

    A grant can have no timed expiry at all, in which case ``remaining_secs()``
    is -1. Claiming such a grant "auto-expires" would tell the operator the
    skip-every-approval mode disarms itself when it never does.
    """
    so = safety_override()
    if not so.is_active():
        return "off"
    if so.is_permanent:
        return NO_EXPIRY_TEXT
    return f"{max(0, so.remaining_secs()) // 60}min remaining"


def describe_new_grant(result_ttl: int) -> str:
    """Describe the lifetime of a grant that was just created."""
    if result_ttl <= 0:
        return NO_EXPIRY_TEXT
    return f"auto-expires in {fmt_grant_duration(result_ttl)}"

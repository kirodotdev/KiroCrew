"""The safety override's grants and their enforcement: ``SafetyOverride``, its singleton, the YOLO verdict.

``SafetyOverride`` holds the ad-hoc, declared and scoped grants, their expiry,
renewal and SEL audit trail; ``safety_override()`` is the process singleton. The
``approval_modes`` verdict for YOLO is pushed here at every ceiling install
(``_push_yolo_policy``) and read with no I/O by every consumer
(``yolo_policy_permits``); a deny revokes every live grant. These share one module
because each rebinds module state the others read, and the class and the verdict
call each other.

:mod:`kiro_crew.safety_override` stays the import path and the patch surface for every
name here, and this module logs under that name. It imports the records, permits and
breadcrumb owners, never the facade.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional

from kiro_crew.platform.context import current_context
from kiro_crew.safety_override_breadcrumb import (
    _breadcrumb_io_lock,
    _breadcrumb_path,
    _clear_breadcrumb,
    _enqueue_breadcrumb,
    _write_breadcrumb,
)
from kiro_crew.safety_override_permits import _YOLO_MODE, approval_mode_permitted
from kiro_crew.safety_override_records import (
    POLICY_REVOKED_SOURCE,
    ActivationResult,
    OverrideStatus,
    RenewResult,
    sel,
)

# Logs under the facade's name: a record names the import path callers use.
logger = logging.getLogger("kiro_crew.safety_override")


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
        # The loop ``_on_expired`` was installed from -- see the property setter.
        self._on_expired_loop: Optional[asyncio.AbstractEventLoop] = None
        # Clears the grant's inherited state synchronously -- see the property.
        self._on_policy_revoked: Optional[Callable[[str], None]] = None
        # Suspends inherited state before a ceiling publishes -- see the property.
        self._on_policy_suspend: Optional[Callable[[], Callable[[], None] | None]] = None
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
        if name in (
            "_duration_resolver",
            "_on_expired",
            "_on_expired_loop",
            "_on_activated",
            "_on_policy_revoked",
            "_on_policy_suspend",
        ):
            object.__setattr__(self, name, None)
            return None
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # ── Callback properties ──────────────────────────────────────────────────

    @property
    def on_expired(self) -> Optional[Callable[[str], None]]:
        return self._on_expired

    @on_expired.setter
    def on_expired(self, cb: Optional[Callable[[str], None]]) -> None:
        self._on_expired = cb
        # Capture the loop this handler belongs to, because the ceiling-install
        # revocation can fire from a WORKER THREAD (``policy_distribution`` refreshes
        # off-loop) and the handler is loop-affine: it schedules the Slack expiry DM
        # and the unattended notice with ``loop.create_task`` behind a
        # ``get_running_loop()`` probe, so called off-loop it silently posts nothing.
        # An operator hearing nothing when policy revokes their grant is the one
        # outcome the notice exists to prevent.
        #
        # Captured HERE rather than looked up at fire time because at fire time there
        # may be no loop to find. The dashboard assigns this from inside
        # ``start_dashboard``, so a running loop is present exactly when the handler
        # that needs it is installed. ``None`` (a sync assignment, or clearing the
        # handler) means "call it inline", which is what a test or a CLI wants.
        try:
            self._on_expired_loop = asyncio.get_running_loop() if cb is not None else None
        except RuntimeError:
            self._on_expired_loop = None

    @property
    def on_policy_revoked(self) -> Optional[Callable[[str], None]]:
        """Clear the grant's INHERITED state. Runs synchronously, on any thread.

        The counterpart to ``on_expired``, split off because the two have different
        deadlines. ``on_expired`` broadcasts and DMs, so it is loop-affine and a
        policy revocation arriving on a worker thread has to schedule it. But the
        state a revocation must destroy -- the ``approval_policy="auto"`` a grant
        wrote onto its slots and into the shared channel-trust mapping -- is read
        DIRECTLY by ``subagent_manager.admission.parent_trusted``, which consults no
        flag in this module. Deferring that half leaves a loop-turn window in which a
        spawn is auto-approved against a ceiling that already denies, and the subagent
        it launched is not un-spawned by the later cleanup.

        So the handler assigned here MUST be thread-safe and MUST do no loop work: it
        is called inline, on whichever thread installed the denying ceiling, before
        anything is scheduled. It must also be idempotent -- ``on_expired`` runs the
        same teardown, so the two overlap on a policy revocation.
        """
        return self._on_policy_revoked

    @on_policy_revoked.setter
    def on_policy_revoked(self, cb: Optional[Callable[[str], None]]) -> None:
        self._on_policy_revoked = cb

    @property
    def on_policy_suspend(self) -> Optional[Callable[[], Callable[[], None] | None]]:
        """Suspend the grant's INHERITED state; returns a restore callable, or None.

        The third callback, and it exists because the other two run too late for one
        reader. ``on_policy_revoked`` clears inherited ``approval_policy="auto"`` once a
        deny has been RESOLVED against a new ceiling -- but that resolve is a governance
        read (``iterdir`` + per-file ``stat``), and the ceiling is already published
        while it runs. ``_on_ceiling_invalidating`` masks ``is_active()`` for that
        window, yet ``subagent_manager.admission.parent_trusted`` never consults
        ``is_active()``: it reads the slot's approval policy directly. So for the whole
        width of the resolve, a spawn from a slot with inherited trust was auto-approved
        against a ceiling that may deny -- and nothing un-spawns it afterwards.

        This callback is called BEFORE the new ceiling is published and must suspend
        that inherited state synchronously, on whatever thread is installing. It
        returns a restore callable which the install hook calls if the resolved ceiling
        still permits YOLO (most installs do -- central distribution re-installs on
        every refresh), and discards if it denies, since ``on_policy_revoked`` then
        clears everything for good. Returning None means nothing was suspended.

        Same thread-safety contract as ``on_policy_revoked``: no loop work, no I/O
        beyond the session store.
        """
        return self._on_policy_suspend

    @on_policy_suspend.setter
    def on_policy_suspend(self, cb: Optional[Callable[[], Callable[[], None] | None]]) -> None:
        self._on_policy_suspend = cb

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
        # A plain memory read, and it is not an approximation of a governance read:
        # the verdict is resolved once per ceiling INSTALL (see
        # ``_on_ceiling_installed``), so the flag is the answer for the ceiling in
        # force rather than a sample of it taken some time ago.
        if not yolo_policy_permits():
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

        # Only commit after audit succeeds -- and only if policy STILL permits.
        #
        # The gate above is a check, this is the act, and the fail-closed SEL audit
        # between them is filesystem I/O: long enough for a denying ceiling to be
        # installed in the gap. Committing anyway would leave a grant that
        # ``revoke_for_policy`` had already run past, so nothing would ever tear it
        # down -- and the dashboard caller then writes ``approval_policy="auto"``
        # onto its slots, which ``admission.parent_trusted`` reads directly.
        #
        # Re-reading under ``_lock`` is what makes every interleaving safe, because
        # the push writes the verdict BEFORE calling ``revoke_for_policy``, which
        # takes this same lock: if this read sees a permit, the flag had not been
        # written yet, so the revoke's acquisition comes after this release and it
        # observes the committed grant; if the revoke got the lock first, the flag is
        # already denied and this read refuses. The read itself is pure memory, so
        # holding the lock across it costs nothing.
        committed = False
        with self._lock:
            if _yolo_policy_permitted_now():
                committed = True
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

        if not committed:
            # Audited outside the lock (it writes to the SEL). The trail reads
            # "enabled, then denied": the enabled event is written before the commit
            # by design, so a refusal after it is the honest record of an arm that
            # was audited and then refused rather than one that took effect.
            self._log_policy_refusal(source, scope="")
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        # Record that a grant is live so a restart can TELL the operator it is
        # gone. Derived from live state and generation-ordered, so a concurrent
        # revocation cannot be undone by this write.
        self._sync_breadcrumb()

        # Report the grant that ACTUALLY exists, not the one that was committed. A
        # deny installed after the commit revokes through ``revoke_for_policy``, and
        # the CALLER acts on this result rather than on the grant: the dashboard
        # writes ``approval_policy="auto"`` onto its slots when it reads
        # ``active=True``, and ``admission.parent_trusted`` reads that policy
        # directly -- so reporting a grant that has already been torn down puts back
        # the inherited trust the revocation had just cleared, which is the one thing
        # a deny is supposed to remove.
        #
        # Checked here rather than only at the commit because the two answer different
        # questions: the commit gate decides whether a grant may be CREATED, and this
        # decides what the caller is TOLD. There is nothing left to tear down -- the
        # revocation already did that, including its own breadcrumb and expiry
        # callback -- so this only corrects the report, and the ``on_activated``
        # callback below is skipped along with it.
        if not self._committed_grant_survives():
            self._log_policy_refusal(source, scope="")
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

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
        # before commit, and audited like the session-wide arm above. Memory-only,
        # for the same reason as the session-wide arm above.
        if not yolo_policy_permits():
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

        # Re-read the verdict under ``_lock`` before recording the grant, for the
        # same reason as the session-wide arm above: the fail-closed audit between the
        # gate and here is filesystem I/O, and a deny installed in that gap would have
        # revoked before this entry existed.
        with self._lock:
            committed = _yolo_policy_permitted_now()
            if committed:
                self._scoped[scope] = (now_mono, now_mono + ttl)

        if not committed:
            self._log_policy_refusal(source, scope=scope)
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        # Same reason as the session-wide arm: report the grant that survived, since
        # the caller acts on this result. ``taskrunner._grant_run_trust`` persists
        # ``run.auto_approve`` straight from it, and an unattended run that believes
        # it holds a grant nothing consults is the divergence that function exists to
        # prevent.
        if not self._committed_grant_survives(scope):
            self._log_policy_refusal(source, scope=scope)
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        return ActivationResult(
            active=True, ttl=ttl, source=source, activated_at_iso=activated_at_iso
        )

    def renew_scoped(self, scope: str, source: str, ttl: Optional[int] = None) -> RenewResult:
        """Slide a scoped grant's expiry forward on activity, capped at the ceiling.

        Extends the grant to ``min(now + ttl, activated_at + _MAX_TTL)`` so an
        actively-progressing run does not lose trust at the base TTL, while the
        absolute 24h hard ceiling from first activation is still honored (an
        abandoned run with no activity simply lapses). No-op / not-renewed if the
        grant is absent or the ceiling is already reached. Intentionally NOT
        SEL-logged per call — it extends an already-audited grant within its
        audited ceiling, and per-tool-call logging would flood the SEL.
        """
        # A grant the policy has stopped permitting must not have its expiry slid forward:
        # renewal is what keeps an active run's grant alive indefinitely inside the
        # 24h ceiling, so sliding it after a deny would extend the very authority
        # the deny withdrew. Reported as not-renewed, the same shape as an absent
        # grant or a reached ceiling, so no caller needs a branch.
        #
        # The grant itself is already gone: the deny was pushed at install time and
        # revoked every live scope then (see ``_on_ceiling_installed``). This check
        # is the mask that makes the two agree even if that teardown was incomplete.
        if not yolo_policy_permits():
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
        # Policy first, exactly as in ``is_active()``. Gating only at arming left the
        # scoped grant honoured until its own TTL, and this is the consult point
        # ``task_executor`` reads before EVERY approval, which is what made the gap
        # reachable for up to 24h. The read costs no filesystem access -- the verdict
        # was resolved when the ceiling was installed -- which is what makes a check
        # on this path affordable at all.
        #
        # A MASK, not the revocation: the deny already tore every live scope down at
        # install time. That ordering is what removed the old three-state verdict
        # here. While the answer was polled behind a TTL, this path also had to cope
        # with "policy could not be read yet", and it could collapse that neither way
        # -- revoking would stall a legitimately granted unattended run for its whole
        # remainder on any unrelated ceiling install, since ``deactivate_scope`` pops
        # the entry permanently and nothing re-arms it, while permitting was the
        # bypass the check exists to close. A pushed verdict is never unresolved for a
        # ceiling that is installed, so there is no third case to collapse.
        if not yolo_policy_permits():
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
        # the consult point every transport passes to ``TurnDriver``, so it runs per
        # TOOL CALL -- which is why the read has to be a bare attribute read.
        #
        # A MASK, and the teardown lives elsewhere ON PURPOSE. The grant that a deny
        # withdraws is destroyed by ``revoke_for_policy`` at the moment the denying
        # ceiling is installed, not by the next caller of this predicate:
        #
        # * Masking without a teardown was tried and is wrong: this same predicate is
        #   what every "is there a grant to clear?" caller reads -- Slack's
        #   ``!yolo off`` is ``if is_yolo_mode(): disable_yolo()`` -- so inside a
        #   denial window it reported "already off" and cleared NOTHING, and a later
        #   policy relaxation resurrected auto-approve the operator had revoked.
        # * Tearing down FROM HERE was tried too, and it puts a teardown that
        #   broadcasts and rewrites every slot's approval policy on the per-tool-call
        #   path, where it has to be re-guarded against firing twice, and cannot run
        #   until something asks -- which is the ``approval_policy="auto"`` window
        #   this design closes.
        #
        # Revoking at install does both jobs at once and needs no guard: the install
        # is a single discrete event, so the teardown and its ``_on_expired`` callback
        # happen exactly once, and by the time anything reads this predicate the grant
        # is already gone. The mask stays anyway, as the fail-closed floor if that
        # teardown was partial -- it is the one check that cannot be skipped.
        if not yolo_policy_permits():
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

    def grant_epoch(self) -> int:
        """A token that changes whenever a grant is created OR destroyed.

        Derived from ``_activation_count`` and the live-grant shape, so an explicit
        ``deactivate`` between two reads is visible even though the count alone would
        not move. Read under ``_lock``. It makes a deferred action conditional on
        the grant that motivated it still being the one in force -- see
        ``_push_yolo_policy``'s restore.
        """
        with self._lock:
            live = (1 if self._active else 0) | (2 if self._scoped else 0)
            return (self._activation_count << 2) | live

    def has_any_grant(self) -> bool:
        """Whether ANY grant exists -- session-wide or scoped -- ignoring policy.

        Distinct from ``has_grant``, which reports the session-wide grant alone. The
        policy-revocation path needs this wider question, because it revokes both kinds
        and must not skip a deny install whose only live grant is a scoped one.
        """
        with self._lock:
            return bool(self._active) or bool(self._scoped)

    def _committed_grant_survives(self, scope: str = "") -> bool:
        """Whether the grant just committed still exists. Shared by both arming paths.

        ``scope`` names a scoped grant; empty means the session-wide one. Read under
        ``_lock``, which is the same lock ``revoke_for_policy`` takes, so the answer is
        never a half-applied teardown. See each caller for why the report has to be
        derived from live state rather than from the commit having happened.
        """
        with self._lock:
            return (scope in self._scoped) if scope else self._active

    def revoke_for_policy(self) -> bool:
        """Destroy every grant because policy now forbids YOLO. Returns had-grant.

        Called from the ceiling-install push, which is the only place that learns a
        deny has arrived. Session-wide and scoped grants both go, because the scope
        denies the MODE and a scoped grant is the same authority in a narrower frame.

        The return value is the SESSION-WIDE grant specifically, because that is what
        decides whether ``_on_expired`` should fire: its handler broadcasts an expiry
        and resets slot approval policies, which only a session-wide grant ever wrote.
        Scoped grants are torn down silently, exactly as ``deactivate_scope`` already
        does on a natural revoke -- their consumers re-check ``is_scope_active`` before
        every approval, so they need no notification to stop.

        Deliberately does NOT consult policy itself: the caller has just resolved it,
        and re-reading here would put a governance read back on a path that must not
        do I/O to decide *whether* to revoke.
        """
        with self._lock:
            had_grant = self._active
            scopes = list(self._scoped)
        for scope in scopes:
            self.deactivate_scope(scope)
        if had_grant:
            self.deactivate(POLICY_REVOKED_SOURCE)
        return had_grant

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

        # Resolved HERE, on the calling thread, for exactly the reason the deadline
        # above is: the publish runs later on the worker, and a job that resolved
        # the path when it RAN would act on whatever the module names at that
        # moment rather than on the file this transition was about. Nothing in
        # production repoints it, so this changes no behaviour there -- but a job
        # that outlives the context it was queued in would otherwise delete and
        # overwrite an unrelated file.
        path = _breadcrumb_path()

        def _publish() -> None:
            with _breadcrumb_io_lock:
                if gen < self._breadcrumb_published_gen:
                    return
                self._breadcrumb_published_gen = gen
                if not active:
                    _clear_breadcrumb(path)
                    return
                _write_breadcrumb(
                    path=path,
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

    Forgets the pushed ``approval_modes`` verdict too. Both are module state, so
    without this a test that ran under a denying policy leaks that verdict into the
    next test, which then refuses a grant for reasons having nothing to do with the
    code under test.
    """
    global _singleton
    with _singleton_lock:
        _singleton = None
    reset_yolo_policy_state()


# ── ``approval_modes`` verdict for YOLO: PUSHED at ceiling install, never polled ──
#
# Resolving the scope walks the governance profiles dir (``iterdir`` + per-file
# ``stat``), and every consumer is on a path that must not do that:
#
# * ``is_active()`` is the auto-approve predicate every transport passes to
#   ``TurnDriver`` (``auto_approve_session=lambda: safety_override().is_active()``),
#   so it runs per TOOL CALL.
# * ``cached_disabled_approval_modes()`` backs ``status_snapshot``, emitted on the
#   5s WebSocket push.
# * arming reaches this module from the event loop through synchronous callers
#   (``taskrunner._grant_run_trust``, the Slack slash handlers).
#
# So the answer is computed ONCE PER CEILING, at the moment a ceiling is installed,
# and every consumer reads the resulting flag. ``platform.context._install`` is the
# single writer of the active context -- central distribution
# (``policy_distribution.apply_ceiling``), boot (``bootstrap``), the lazy default and
# the test reset all go through it -- so a registered hook sees every ceiling this
# process ever holds.
#
# The alternative was tried and is what this replaces: a memory cache behind a 5s TTL
# and a governance-generation stamp, refreshed on a worker thread. Polling a value
# that only changes on a discrete event needs a freshness key, a third
# "not resolved under the installed ceiling yet" verdict for the window before the
# refresh lands, and a per-caller rule for collapsing it -- and each of those is a
# window in which a permit resolved under a retired ceiling is still served. Pushing
# removes the windows rather than shortening them: a flag is either the answer for
# the ceiling in force, or there is no ceiling installed at all.
#
#: True when the ``approval_modes`` scope permits ``yolo`` under the ceiling now
#: installed. Read with no I/O, no TTL and no lock.
#:
#: Starts DENIED, which is the fail-closed direction and is never actually observed:
#: every read goes through ``yolo_policy_permits``, which resolves first if no
#: ceiling has been pushed yet. It matters only if that bootstrap itself fails.
_yolo_policy_permitted: bool = False
#: Whether a ceiling has ever been resolved into the flag above.
#:
#: This is bookkeeping for the bootstrap, NOT a third verdict state: no consumer
#: branches on it, and it can never be False at the moment a consumer reads the flag.
#: It exists because the flag is pushed BY an install, and a process can reach a
#: consumer without one having happened yet -- a unit test that never boots the
#: platform, a CLI, or ``safety_override.py`` imported after boot already installed the
#: context (registration deliberately does not replay that install, since resolving
#: governance from inside an import invites a cycle).
_yolo_policy_resolved: bool = False


def _on_ceiling_invalidating() -> None:
    """Withdraw the permit before a new ceiling becomes visible. Pure memory.

    Runs ahead of the assignment to ``_ACTIVE``, which is what makes the verdict
    fail closed for the whole time the new ceiling is live and unresolved. Publishing
    first and resolving after left a window as wide as one governance resolution --
    an ``iterdir`` plus a per-file ``stat`` -- in which the denying ceiling was in
    force and ``is_active()`` still returned the previous permit, so a tool call
    landing there was auto-approved against it.

    It only MASKS: no revocation, because the incoming ceiling has not been read yet
    and most installs permit yolo. A grant is torn down by ``_push_yolo_policy`` only
    once a real deny has been resolved.

    ``_yolo_policy_resolved`` is forced True with it, and that is load-bearing rather
    than tidy: left unresolved, the next read would bootstrap, resolve against the
    ceiling still installed at that moment -- the OUTGOING one -- and write its permit
    straight back over this mask.

    The flag is not the only grant-derived state, and the mask is not enough on its
    own. ``admission.parent_trusted`` reads a slot's ``approval_policy`` directly, never
    ``is_active()``, so with a live grant the inherited ``"auto"`` on the slots has to be
    SUSPENDED here too -- through ``on_policy_suspend`` -- or a spawn during the resolve
    is auto-approved against a ceiling that may deny, and the launched subagent is never
    un-spawned. The restore callable it returns is kept for ``_push_yolo_policy``,
    which puts the policies back if the resolved ceiling still permits.
    """
    global _yolo_policy_permitted, _yolo_policy_resolved, _suspended_trust_restore
    _yolo_policy_permitted = False
    _yolo_policy_resolved = True
    _suspended_trust_restore = None
    so = safety_override()
    # Session-wide only: scoped grants write no inherited slot trust, so there is
    # nothing to suspend for them (see ``_revoke_grants_for_policy_deny``).
    if not so.has_grant():
        return
    suspend = so.on_policy_suspend
    if suspend is None:
        return
    try:
        restore = suspend()
        if restore is not None:
            # Remember WHICH grant this suspension belongs to, so the restore can be
            # refused if that grant is gone by the time the ceiling resolves.
            _suspended_trust_restore = (so.grant_epoch(), restore)
    except Exception:
        logger.warning(
            "on_policy_suspend callback raised; inherited trust may be live during the "
            "ceiling resolve",
            exc_info=True,
        )


#: The restore half of an in-flight suspension (see ``on_policy_suspend``) paired
#: with the ``grant_epoch`` it was taken under, held between the invalidate hook and
#: the install hook of the same install. Module-level because the two hooks are
#: separate calls from ``platform.context._install``; installs are serialised by their
#: caller, so there is never more than one in flight.
_suspended_trust_restore: Optional[tuple[int, Callable[[], None]]] = None


def _push_yolo_policy() -> None:
    """Resolve the verdict, store it, and revoke live grants if it now denies.

    Fails CLOSED: a governance-evaluation error resolves to DENIED, because the
    alternative is auto-approving every tool against a policy nobody could read.
    ``approval_mode_permitted`` already passes ``fail_closed=True``, so the only
    error that reaches here is one it could not evaluate at all (a ceiling that
    refuses to compose raises ``PlatformCompositionError`` through it).
    """
    global _yolo_policy_permitted, _yolo_policy_resolved, _suspended_trust_restore
    try:
        permitted = bool(approval_mode_permitted(_YOLO_MODE))
    except Exception:
        # WARNING, not debug, and that level is the point. Deleting the old three-state
        # verdict folded "policy could not be read" into "policy denies", so the
        # operator now sees the org-policy refusal for both -- and a solo operator with
        # no policy at all being told a phantom organization blocked them is a
        # misattribution this code fixed once already. Enforcement stays collapsed (a
        # deny is the only fail-closed answer), so the CAUSE has to be visible
        # somewhere: this line is where it is, and it names the consequence rather than
        # just the error.
        logger.warning(
            "could not resolve the approval_modes policy for yolo; denying it. "
            "Auto-approve will be refused with the organization-policy message until "
            "a ceiling installs successfully.",
            exc_info=True,
        )
        permitted = False
    _yolo_policy_permitted = permitted
    _yolo_policy_resolved = True
    suspended = _suspended_trust_restore
    _suspended_trust_restore = None
    if permitted:
        # The ceiling still permits, so the inherited trust suspended before
        # publication goes back exactly as it was -- PROVIDED the grant it belonged to
        # is still the one in force. The governance read between suspend and here is
        # long enough for an operator to have revoked YOLO explicitly (``!yolo off``,
        # the picker's ``normal``), and that revocation cleared the same slot policies
        # this would put back. Restoring over it would resurrect an auto-approve the
        # operator just withdrew, on the very read ``admission.parent_trusted`` makes.
        # The epoch moves on any activate or deactivate, so a stale one means "not the
        # grant you suspended for" and the restore is dropped.
        if suspended is not None:
            epoch, restore = suspended
            if safety_override().grant_epoch() != epoch:
                logger.debug(
                    "grant changed while the ceiling resolved; not restoring suspended "
                    "inherited trust"
                )
                return
            try:
                restore()
            except Exception:
                logger.warning(
                    "could not restore suspended inherited trust after a permitting "
                    "ceiling install",
                    exc_info=True,
                )
        return
    # Denied: the suspension becomes permanent. ``on_policy_revoked`` below clears the
    # same state (idempotently) and the shared channel-trust mapping with it, so the
    # restore callable is simply dropped.
    _revoke_grants_for_policy_deny()


def _revoke_grants_for_policy_deny() -> None:
    """Destroy every live grant, then tell the dashboard so it clears inherited trust.

    Dropping the grant is not the whole revocation. A dashboard grant also writes
    ``approval_policy="auto"`` onto the slots and into the shared channel-trust
    mapping, and ``subagent_manager.admission.parent_trusted`` reads THAT policy
    rather than any flag in this module -- so a revocation that stopped at the flag
    left ``spawn_run`` auto-approved, and a subagent already launched under it is not
    un-spawned when the policy lands. ``_on_expired`` is the handler that resets those
    policies and clears the mapping, and it is the same one a TTL lapse fires, so a
    policy revocation reuses it rather than growing a second, divergent teardown.

    THREAD. ``policy_distribution.apply_ceiling`` can install a ceiling from a worker
    thread (its refresh poller), while the handler is loop-affine: it schedules the
    Slack expiry DM and the unattended-run notice with ``loop.create_task`` behind a
    ``get_running_loop()`` probe, so run off-loop it silently posts nothing. Silence
    about a security grant being revoked is precisely what that notice exists to
    prevent, so the callback is SCHEDULED onto the loop it was installed from
    (``call_soon_threadsafe``) whenever this is not already running on that loop, and
    called inline otherwise. Inline is also the answer when no loop was recorded -- a
    sync CLI or a test -- where scheduling would drop the call entirely.

    The grant teardown itself is NOT deferred: it is thread-safe (``_lock``) and it is
    the part that must be true the instant the ceiling is installed. Only the
    notification half crosses the thread boundary.
    """
    so = safety_override()
    # Nothing to revoke, and nothing derived from a grant to clear.
    if not so.has_any_grant():
        return

    # Only a SESSION-WIDE grant has inherited state. The dashboard writes
    # ``approval_policy="auto"`` onto its slots when the session-wide override arms,
    # and the shared channel-trust mapping is fed by the same kind of grant. A scoped
    # grant (a taskrunner run, an Issue Radar crew) writes neither: ``task_executor``
    # consults ``is_scope_active`` directly, so there is nothing inherited to clear and
    # running the clear anyway would revoke an independent Trust press on some
    # unrelated channel session -- trust this override never handed out. Scoped grants
    # are still revoked below, by ``revoke_for_policy``.
    had_session_wide = so.has_grant()

    # INHERITED state FIRST -- before the grant flag drops, not after it.
    #
    # The two live in different stores and cannot be written atomically, so one of them
    # goes first and the other trails. Which one is not a detail, because the two
    # orderings fail in opposite directions:
    #
    # * grant first, inherited second: for the statements in between, ``is_active()``
    #   already reports no grant while the slots still carry ``approval_policy="auto"``
    #   -- and ``admission.parent_trusted`` reads THAT policy directly, so a spawn in
    #   that gap is auto-approved against the denying ceiling. Nothing recovers it: the
    #   subagent is already launched and no later event un-spawns it.
    # * inherited first, grant second: for the same statements ``is_active()`` still
    #   reports the grant while the inherited half is already gone. A spawn there finds
    #   no inherited trust and takes the ordinary approval path; a tool call there is
    #   auto-approved for a few instructions longer, which is recoverable -- the deny
    #   lands microseconds later and the approval was already audited.
    #
    # So the unrecoverable direction is the one that gets closed. Doing it under a
    # single lock instead would mean holding ``_lock`` across ``set_approval_policy``
    # -- session-store I/O on the lock every per-tool-call ``is_active()`` contends,
    # which is the stall this design exists to remove.
    sync_cb = so.on_policy_revoked if had_session_wide else None
    if sync_cb is not None:
        try:
            sync_cb(POLICY_REVOKED_SOURCE)
        except Exception:
            logger.warning(
                "on_policy_revoked callback raised; inherited trust may survive",
                exc_info=True,
            )

    if not so.revoke_for_policy():
        # Someone else tore the grant down between the check above and here. The
        # inherited clear already ran, which is what their teardown would have done
        # too, so there is nothing left to do and no second notice to send.
        return

    cb = so.on_expired
    if cb is None:
        return

    def _fire() -> None:
        try:
            cb(POLICY_REVOKED_SOURCE)
        except Exception:
            logger.warning("on_expired callback raised after a policy revocation", exc_info=True)

    loop = so._on_expired_loop
    if loop is None or loop.is_closed():
        _fire()
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _fire()
        return
    try:
        loop.call_soon_threadsafe(_fire)
    except RuntimeError:
        # The loop died between the closed check and the call. Nothing can be
        # scheduled onto it, and the teardown above has already happened, so the
        # notification is simply lost -- which is what a gateway shutting down means.
        logger.debug("could not schedule the policy-revocation notice", exc_info=True)


def yolo_policy_permits() -> bool:
    """Whether policy PERMITS YOLO, from memory. Safe on the event loop.

    The single read every consumer uses -- the arming gates, ``is_active``,
    ``is_scope_active``, ``renew_scoped`` and the dashboard status field -- so the
    status the picker renders and the predicate that enforces it cannot disagree.

    Bootstraps once if no ceiling has been pushed yet (see ``_yolo_policy_resolved``),
    which is what keeps this honest in a process that never booted the platform: it
    resolves through ``current_context()``, so the standalone default is composed and
    installed. That lazy install is deliberately SILENT (``notify=False``), so it is
    ``_bootstrap_yolo_policy``'s own direct ``_push_yolo_policy()`` that writes the
    verdict, not the install hook. Either way it happens once and then every later
    ceiling arrives through the hook, which is why this is a one-shot bootstrap rather
    than a cache that can go stale.
    """
    if not _yolo_policy_resolved:
        _bootstrap_yolo_policy()
    return _yolo_policy_permitted


def _yolo_policy_permitted_now() -> bool:
    """The pushed verdict, with NO bootstrap. Safe to call while holding a lock.

    :func:`yolo_policy_permits` resolves when nothing has been pushed yet, and that
    resolve installs a context, which fires the facade's own install hook, which can call
    ``revoke_for_policy`` -- so calling it under ``SafetyOverride._lock`` would
    re-enter that non-reentrant lock on the same thread and deadlock. The two commit
    points that must re-read the verdict inside the lock use this instead, and they
    can: their own gate already went through ``yolo_policy_permits`` a few lines
    earlier, so the verdict is resolved by the time they look again.
    """
    return _yolo_policy_permitted


def _bootstrap_yolo_policy() -> None:
    """Resolve the verdict for the first time, when no install has pushed one.

    Reached at most once per process, and normally not at all: boot installs a
    context long before anything arms or consults a grant.

    Composing the context is what does the work, but NOT by firing the install hook:
    the lazy default installs silently (``notify=False``), because it is reached from
    inside a governance read and a hook there would re-enter a mid-load profile store.
    So the direct ``_push_yolo_policy`` below is what writes the verdict -- on both
    orderings, the silent lazy install and a context that was ALREADY installed before
    ``safety_override.py`` registered its hook. Composing still matters: it is what makes a
    ceiling exist to resolve against.

    A context that refuses to compose (a governed host whose boot did not run, where
    ``current_context`` raises rather than handing out open-source defaults) leaves
    the flag at its fail-closed initial value and marks it resolved: there is no
    ceiling to read, so YOLO stays off until a real one is installed -- at which point
    the hook pushes the true answer. Marking it resolved is what stops every
    subsequent tool call from re-attempting the same failing composition.
    """
    global _yolo_policy_resolved
    try:
        current_context()
    except Exception:
        logger.debug("no installed ceiling for the yolo verdict; denying", exc_info=True)
        _yolo_policy_resolved = True
        return
    if not _yolo_policy_resolved:
        _push_yolo_policy()


def reset_yolo_policy_state() -> None:
    """Forget the pushed verdict, so the next read resolves again.

    For tests, and for :func:`platform.context.reset_context` -- both mean "the
    ceiling this answer belonged to is not installed any more".
    """
    global _yolo_policy_permitted, _yolo_policy_resolved, _suspended_trust_restore
    _yolo_policy_permitted = False
    _yolo_policy_resolved = False
    _suspended_trust_restore = None

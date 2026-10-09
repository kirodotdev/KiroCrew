"""Worker slots -- the supported way for an app to own an agent session.

An app that drives a background agent (a spec writer, a researcher) needs a chat
slot that is hidden from the main sidebar, runs in the app's project directory,
and may carry a bounded trust grant so an unattended turn does not stall on
approvals. Without this module the app stamps the private attributes behind those
behaviours itself (``slot._app``, ``slot.project``, ``slot._trust``) and has to
re-stamp them on every acquire, because another create path (ChatEmbed's own
POST, for one) can create the slot without them.

:func:`acquire_worker_slot` owns that stamping and returns a
:class:`WorkerSlotLease`; while the lease is held the slot is the app's worker.

Bounds built in:

* **How many at once.** Each app may hold at most ``limit`` leases at a time,
  :data:`DEFAULT_WORKER_SLOT_LIMIT` (1) unless the app changes it with
  :func:`set_worker_slot_limit`. A further acquire waits for a release, for at
  most ``timeout`` seconds, then raises :class:`WorkerSlotTimeout` naming the
  app, the limit and the wait. The limit is a convenience the app sets for
  itself, not a ceiling it cannot raise.
* **One owner per slot.** A slot key is leased by one lease at a time across
  every app, and a slot already owned by another app is refused, so one app's
  trust grant or working directory never lands on another app's worker.
* **Leases follow their slot.** A lease whose slot was deleted or replaced is
  reclaimed by the next acquire, so a lost lease cannot lock an app out.
* **How long trust lasts.** ``trust=True`` is never written as the interactive
  ``slot._trust`` flag, which does not expire and is cached as the session's
  approval policy for subagents. It is a ``SafetyOverride`` scoped grant
  (``slot._trust_scope``): SEL-audited fail-closed before it exists, re-checked
  on every approval, and never cached as a session policy. ``trusted_patterns``
  are audited fail-closed the same way before they are added. Both are withdrawn
  when the lease is released or after ``trust_ttl_secs``, whichever is first,
  and the TTL cannot exceed :data:`MAX_TRUST_TTL_SECS`.

Usage, from an app backend that holds the dashboard ``state``::

    async with await acquire_worker_slot(
        state, "my-app", "my-app-job-42", project="/path/to/repo",
        trusted_patterns=["npm test"], timeout=30,
    ) as lease:
        ...  # send work to lease.slot

Leases live in the gateway process and are not persisted, so a restart starts
every app with no leases held.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from kiro_crew.apps.audit_sdk import AuditSDK
from kiro_crew.safety_override import safety_override
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: How many worker slots one app may hold at once unless it asks for more.
DEFAULT_WORKER_SLOT_LIMIT = 1
#: How long :func:`acquire_worker_slot` waits for a free slot by default.
DEFAULT_ACQUIRE_TIMEOUT_SECS = 30.0
#: Longest a trust grant may last; the ``SafetyOverride`` ad-hoc ceiling.
MAX_TRUST_TTL_SECS = 24 * 60 * 60
#: Trust lifetime when the caller does not pick one.
DEFAULT_TRUST_TTL_SECS = 60 * 60
#: How often a waiting acquire re-checks for leases whose slot is gone.
_RECLAIM_POLL_SECS = 0.5


class WorkerSlotTimeout(TimeoutError):
    """No worker slot became free before the acquire timeout."""


def _positive_finite(value: float, name: str) -> float:
    # ``bool`` is an int subclass; a True limit reading as 1 would hide a bug.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return float(value)


def worker_trust_scope(app: str, key: str) -> str:
    """``SafetyOverride`` scope key holding one worker slot's blanket grant."""
    return f"app:{app}:worker:{key}:autoapprove"


@dataclass
class _Registry:
    """Every held lease on one event loop, keyed by slot key."""

    loop: asyncio.AbstractEventLoop | None = None
    cond: asyncio.Condition | None = None
    leases: dict[str, "WorkerSlotLease"] = field(default_factory=dict)
    #: Keys reserved by an acquire that has not created its slot yet.
    pending: dict[str, str] = field(default_factory=dict)

    def condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self.cond is None or self.loop is not loop:
            # Leases never outlive their loop, so a new loop starts empty.
            self.loop = loop
            self.cond = asyncio.Condition()
            self.leases = {}
            self.pending = {}
        return self.cond

    def held_by(self, app: str) -> int:
        live = sum(1 for lease in self.leases.values() if lease.app == app)
        return live + sum(1 for owner in self.pending.values() if owner == app)

    def busy(self, key: str) -> bool:
        return key in self.leases or key in self.pending


_registry = _Registry()
_limits: dict[str, int] = {}


def _require_app(app: str) -> str:
    if not isinstance(app, str) or not app.strip():
        raise ValueError("app must be the app's non-empty name")
    return app


def _limit(app: str) -> int:
    return _limits.get(app, DEFAULT_WORKER_SLOT_LIMIT)


def set_worker_slot_limit(app: str, limit: int) -> None:
    """Let *app* hold up to *limit* worker slots at once.

    Takes effect at once: raising it wakes waiting acquires; lowering it never
    revokes a lease already held, it only makes new acquires wait longer.
    """
    _require_app(app)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be an integer >= 1, got {limit!r}")
    _limits[app] = limit
    reg = _registry
    if reg.cond is not None and reg.loop is not None and not reg.loop.is_closed():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is reg.loop:
            reg.loop.create_task(_notify_all(reg.cond))


async def _notify_all(cond: asyncio.Condition) -> None:
    async with cond:
        cond.notify_all()


def _audit_patterns_fail_closed(app: str, key: str, count: int, ttl: float) -> bool:
    """Write the pattern grant's SEL row before the grant exists. False on failure."""
    try:
        sel().log_api_access(
            caller=f"app:{app}",
            operation=f"{app}.worker_slot_trust",
            outcome="granted",
            source="app",
            resources=f"{key} patterns={count} ttl={ttl:g}s",
            critical=True,
        )
    except Exception:
        logger.error("app %s: worker pattern grant refused, its audit failed", app)
        return False
    return True


@dataclass
class WorkerSlotLease:
    """One held worker slot. Release it when the app is done with the worker."""

    app: str
    key: str
    slot: Any
    _state: Any = field(default=None, repr=False)
    _scope: str = ""
    _granted_patterns: frozenset[str] = frozenset()
    _expiry: asyncio.TimerHandle | None = None
    _released: bool = False
    _audit: AuditSDK | None = field(default=None, repr=False)

    @property
    def released(self) -> bool:
        return self._released

    @property
    def trust_granted(self) -> bool:
        """Whether this lease holds a blanket grant (False if it was refused)."""
        return bool(self._scope)

    def _slot_is_gone(self) -> bool:
        get = getattr(self._state, "get_slot", None)
        if not callable(get):
            return False
        try:
            return get(self.key) is not self.slot
        except Exception:
            return False

    def _withdraw_trust(self, reason: str) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        if not (self._scope or self._granted_patterns):
            return
        if self._scope:
            safety_override().deactivate_scope(self._scope)
            if getattr(self.slot, "_trust_scope", "") == self._scope:
                self.slot._trust_scope = ""
        patterns = getattr(self.slot, "_trusted_patterns", None)
        if isinstance(patterns, set):
            patterns.difference_update(self._granted_patterns)
        self._scope = ""
        self._granted_patterns = frozenset()
        if self._audit is not None:
            self._audit.record(
                "worker_slot_trust", "revoked", resources=f"{self.key} reason={reason}"
            )

    def _on_trust_expired(self) -> None:
        self._expiry = None
        self._withdraw_trust("ttl expired")

    def _drop(self, reason: str) -> None:
        """Mark released and forget this lease. Caller holds the condition."""
        self._released = True
        self._withdraw_trust(reason)
        if _registry.leases.get(self.key) is self:
            del _registry.leases[self.key]

    async def release(self) -> None:
        """Withdraw this lease's trust grant and free the slot for the next acquire.

        Safe to call more than once.
        """
        if self._released:
            return
        cond = _registry.condition()
        async with cond:
            self._drop("lease released")
            cond.notify_all()

    async def __aenter__(self) -> "WorkerSlotLease":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()


def _reclaim_lost_leases() -> None:
    """Free leases whose slot was deleted or replaced. Caller holds the condition."""
    for lease in list(_registry.leases.values()):
        if lease._slot_is_gone():
            lease._drop("slot removed")


def _grant_trust(
    lease: WorkerSlotLease, slot: Any, trust: bool, patterns: frozenset[str], ttl: float
) -> None:
    app, key = lease.app, lease.key
    if trust and not getattr(slot, "_trust", False):
        current = str(getattr(slot, "_trust_scope", "") or "")
        scope = worker_trust_scope(app, key)
        # A different scope already on the slot is someone else's grant: leave it.
        if not current or current == scope:
            result = safety_override().activate_scoped(
                scope, source=f"app:{app}", ttl=max(1, math.ceil(ttl))
            )
            if result.active:
                slot._trust_scope = scope
                lease._scope = scope
            else:
                slot._trust_scope = ""
                logger.error(
                    "app %s: worker trust grant for %s refused; the worker will "
                    "fall back to interactive approval",
                    app,
                    key,
                )
    existing = getattr(slot, "_trusted_patterns", None)
    if not isinstance(existing, set):
        existing = set()
        slot._trusted_patterns = existing
    added = frozenset(patterns - existing)
    if added and _audit_patterns_fail_closed(app, key, len(added), ttl):
        existing.update(added)
        lease._granted_patterns = added
    if lease._scope or lease._granted_patterns:
        lease._expiry = asyncio.get_running_loop().call_later(ttl, lease._on_trust_expired)


async def _reserve(app: str, key: str, timeout: float) -> None:
    """Wait until *app* has room and *key* is free, then reserve *key* for *app*."""
    cond = _registry.condition()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with cond:
        while True:
            _reclaim_lost_leases()
            if _registry.held_by(app) < _limit(app) and not _registry.busy(key):
                _registry.pending[key] = app
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                held = _registry.held_by(app)
                busy = f"slot {key!r} is already leased; " if _registry.busy(key) else ""
                raise WorkerSlotTimeout(
                    f"app {app!r} already holds {held} of its {_limit(app)} worker "
                    f"slot(s); {busy}waited {timeout:g}s for one to be released. "
                    "Release a lease (lease.release(), or leave its 'async with' "
                    "block) or raise the limit with set_worker_slot_limit()."
                )
            try:
                await asyncio.wait_for(cond.wait(), min(remaining, _RECLAIM_POLL_SECS))
            except asyncio.TimeoutError:
                pass


async def acquire_worker_slot(
    state: Any,
    app: str,
    key: str,
    *,
    project: str,
    agent: str = "",
    model: str = "",
    trust: bool = False,
    trusted_patterns: Iterable[str] = (),
    trust_ttl_secs: float = DEFAULT_TRUST_TTL_SECS,
    timeout: float = DEFAULT_ACQUIRE_TIMEOUT_SECS,
) -> WorkerSlotLease:
    """Acquire *key* as one of *app*'s worker slots and stamp it for the app.

    Waits up to *timeout* seconds while *app* already holds as many leases as its
    limit (default 1), or while *key* is leased by anyone, and raises
    :class:`WorkerSlotTimeout` if neither frees. Raises ``ValueError`` if the
    slot belongs to another app. The slot is created if missing and re-stamped
    either way: hidden as *app*'s and working in *project*. Requested trust lasts
    until release or *trust_ttl_secs* (capped at :data:`MAX_TRUST_TTL_SECS`); a
    grant whose audit cannot be written is not made, and
    :attr:`WorkerSlotLease.trust_granted` reports that. Every acquire pushes a
    slot-list update so the dashboard hides the app's slot at once.
    """
    _require_app(app)
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty slot key")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project must be the worker's working directory")
    timeout = _positive_finite(timeout, "timeout")
    patterns = frozenset(p for p in trusted_patterns if isinstance(p, str) and p.strip())
    ttl = 0.0
    if trust or patterns:
        ttl = _positive_finite(trust_ttl_secs, "trust_ttl_secs")
        if ttl > MAX_TRUST_TTL_SECS:
            raise ValueError(
                f"trust_ttl_secs {ttl:g} exceeds the {MAX_TRUST_TTL_SECS}s cap; "
                "re-acquire to renew the grant instead"
            )

    await _reserve(app, key, timeout)
    cond = _registry.condition()
    lease: WorkerSlotLease | None = None
    try:
        slot = state.get_or_create_slot(name=key, agent=agent, app=app, model=model)
        owner = str(getattr(slot, "_app", "") or "")
        if owner and owner != app:
            raise ValueError(f"slot {key!r} belongs to app {owner!r}")
        # The registry may fold the requested name; the folded key is the identity.
        canonical = str(getattr(slot, "key", key) or key)
        if canonical != key and (
            canonical in _registry.leases or _registry.pending.get(canonical, app) != app
        ):
            raise ValueError(f"slot {canonical!r} is already leased")
        slot._app = app
        slot.project = project
        lease = WorkerSlotLease(
            app=app, key=canonical, slot=slot, _state=state, _audit=AuditSDK(app)
        )
        if trust or patterns:
            _grant_trust(lease, slot, trust, patterns, ttl)
        async with cond:
            _registry.pending.pop(key, None)
            _registry.leases[canonical] = lease
        push = getattr(state, "push_slots_update", None)
        if callable(push):
            push()
        return lease
    except BaseException:
        if lease is not None:
            lease._withdraw_trust("acquire failed")
        async with cond:
            _registry.pending.pop(key, None)
            cond.notify_all()
        raise

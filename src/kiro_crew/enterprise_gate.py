"""The enterprise gate: whether an enterprise build serves anything but login.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, section
"Enterprise gate". In an enterprise build (``edition.is_enterprise()``) the
gateway does not start at all without a kiro login (``kiro_crew.enterprise_boot``
exits before the bind), so the gate starts OPEN. It closes when that login is
lost or expires while the gateway runs, and reopens once a probe sees a valid
login again. While it is closed, the gateway middleware in
``dashboard/enterprise_gate_middleware.py`` denies every route except the few a
login screen needs. An ``oss`` build never builds a gate.

The gate is closed while ANY reason holds it closed, and open when none does.
Each concern owns one reason and only ever clears its own:

* :data:`REASON_LOGIN_REQUIRED` is owned by :class:`LoginWatch` here.
* A later concern (governance rules that fail to fetch) calls
  ``gate.close("<its reason>")`` and ``gate.clear("<its reason>")`` with its own
  reason string. Nothing here needs to change for it.

:class:`LoginWatch` reuses the existing login check
(``KiroPrerequisiteService.login_verified``, the setup gate's ``whoami`` probe);
there is no second login check.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

#: The gate is closed because no valid kiro login was seen.
REASON_LOGIN_REQUIRED = "login_required"

#: The ``code`` of the 403 the gateway answers while the gate is closed. Internal
#: clients match on it to fail closed rather than read the 403 as a decision.
GATE_CLOSED_CODE = "enterprise_gate_closed"

_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# How often the login watch checks, and how old the last probe may be before a
# check re-probes. While closed it checks often, so a fresh login reopens the
# gate within seconds; the check is a local identity-store read unless the
# probe is due. While open it checks less often; the full ``whoami`` probe that
# catches an expired token runs at most once per ``_OPEN_MAX_AGE_SECS``. An ACP
# auth failure closes the gate sooner, through the latch (see ``LoginWatch.sync``).
_CLOSED_INTERVAL_SECS = 5.0
_CLOSED_MAX_AGE_SECS = 60.0
_OPEN_INTERVAL_SECS = 60.0
_OPEN_MAX_AGE_SECS = 900.0


@dataclass(frozen=True)
class GateStatus:
    """A snapshot of the gate: open, or closed with the reasons holding it."""

    open: bool
    reasons: tuple[str, ...]

    @property
    def reason(self) -> str:
        """The first reason holding the gate closed, ``""`` when open."""
        return self.reasons[0] if self.reasons else ""


GateListener = Callable[[GateStatus], None]


class EnterpriseGate:
    """Open or closed, plus why. Safe to call from any thread.

    Starts closed for every reason in *closed_for*, open when it is empty (the
    gateway passes it empty: its boot login check already passed). ``close`` and
    ``clear`` return whether the open/closed state changed, and call each
    listener with the new status when it did. Listeners run on the caller's thread, outside the lock;
    one that raises is logged and does not stop the others.
    """

    def __init__(self, closed_for: tuple[str, ...] = (REASON_LOGIN_REQUIRED,)) -> None:
        self._lock = threading.Lock()
        self._reasons: dict[str, None] = {}
        for reason in closed_for:
            self._reasons[_checked(reason)] = None
        self._listeners: list[GateListener] = []

    @property
    def is_open(self) -> bool:
        with self._lock:
            return not self._reasons

    def status(self) -> GateStatus:
        with self._lock:
            return GateStatus(open=not self._reasons, reasons=tuple(self._reasons))

    def close(self, reason: str) -> bool:
        """Hold the gate closed for *reason*. Returns True if it was open."""
        reason = _checked(reason)
        with self._lock:
            was_open = not self._reasons
            self._reasons[reason] = None
            status = GateStatus(open=False, reasons=tuple(self._reasons))
        if was_open:
            logger.warning("Enterprise gate closed: %s", reason)
            self._notify(status)
        return was_open

    def clear(self, reason: str) -> bool:
        """Drop *reason*. Returns True if that opened the gate."""
        reason = _checked(reason)
        with self._lock:
            if reason not in self._reasons:
                return False
            del self._reasons[reason]
            opened = not self._reasons
            status = GateStatus(open=opened, reasons=tuple(self._reasons))
        if opened:
            logger.info("Enterprise gate opened")
            self._notify(status)
        return opened

    def add_listener(self, listener: GateListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def _notify(self, status: GateStatus) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(status)
            except Exception:
                logger.exception("Enterprise gate listener failed")


def _checked(reason: str) -> str:
    if not isinstance(reason, str) or not _REASON_RE.match(reason):
        raise ValueError(f"invalid enterprise gate reason: {reason!r}")
    return reason


class LoginSource(Protocol):
    """What :class:`LoginWatch` reads; ``KiroPrerequisiteService`` provides it."""

    @property
    def authenticated(self) -> bool: ...

    async def login_verified(self, *, max_age_secs: float) -> bool: ...


class LoginWatch:
    """Keeps :data:`REASON_LOGIN_REQUIRED` in step with the kiro login.

    ``check`` is the only path that OPENS for login: it asks the service, which
    re-probes when due. ``sync`` is the per-request read of the latch and only
    ever CLOSES, so a latch narrowed by an ACP auth failure closes the gate on
    the next request, while a probe that failed (and so left the latch as it
    was) cannot be undone by it.
    """

    def __init__(
        self,
        gate: EnterpriseGate,
        source: LoginSource,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gate = gate
        self._source = source
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        # Set when the gate closes, so a watch sleeping its long open interval
        # checks at once and then runs at the closed cadence.
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        gate.add_listener(self._on_gate_change)

    def _on_gate_change(self, status: GateStatus) -> None:
        loop, wake = self._loop, self._wake
        if status.open or loop is None or wake is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(wake.set)

    def sync(self) -> None:
        """Close the gate if the latched verdict says signed out. Never opens."""
        try:
            authenticated = self._source.authenticated
        except Exception:
            logger.warning("Kiro login latch unreadable; closing the gate", exc_info=True)
            authenticated = False
        if not authenticated:
            self._gate.close(REASON_LOGIN_REQUIRED)

    async def check(self) -> bool:
        """Ask for a verified login and open or close the gate to match."""
        max_age = _OPEN_MAX_AGE_SECS if self._gate.is_open else _CLOSED_MAX_AGE_SECS
        try:
            ok = bool(await self._source.login_verified(max_age_secs=max_age))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Kiro login check raised; closing the gate", exc_info=True)
            ok = False
        if ok:
            self._gate.clear(REASON_LOGIN_REQUIRED)
        else:
            self._gate.close(REASON_LOGIN_REQUIRED)
        return ok

    async def run(self) -> None:
        """Check now, then keep checking until cancelled."""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while True:
            await self.check()
            # A close during the check is already reflected in the interval.
            self._wake.clear()
            interval = _OPEN_INTERVAL_SECS if self._gate.is_open else _CLOSED_INTERVAL_SECS
            await self._pause(interval)

    async def _pause(self, secs: float) -> None:
        """Sleep *secs*, or less if the gate closes meanwhile."""
        assert self._wake is not None
        sleeper = asyncio.ensure_future(self._sleep(secs))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (sleeper, waker):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(sleeper, waker, return_exceptions=True)

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="enterprise-login-watch")
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

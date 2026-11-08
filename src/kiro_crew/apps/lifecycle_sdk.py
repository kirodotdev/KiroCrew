"""Lifecycle SDK — an app's own enabled state, and admission under its own lifecycle lock.

An app that starts long work from a request (a run, a sync, an import) has to refuse
it once the app is switched off, and a plain "is it enabled?" read races the disable:
the read can say yes while the disable is between its stop hook and its ``enabled``
write, and the work then starts after the stop hook already ran, so nothing stops it.
The gateway closes that race for itself by holding the app's lifecycle lock across the
stop hook and the write. Without this seam an app could close it only the same way, by
importing ``apps.manager.is_app_enabled`` and ``apps.manager.app_lifecycle_lock``: a
private layout an installed app breaks on whenever the wheel moves, and a raw lock an
app could hold for as long as it liked, which keeps the operator's disable, update and
uninstall of that app waiting behind it.

**Its own app only.** The app name comes from the context the SDK was built with, and
no method takes one, so an app can neither read another app's switch through it nor
contend on another app's lock. Like :class:`~kiro_crew.apps.audit_sdk.AuditSDK` this is
cooperative rather than unforgeable: hook code runs in the gateway process and could
build ``AppLifecycleSDK("other-app")`` or import the manager itself. What the seam
removes is the private import, not process isolation.

**Bounded, so the app cannot hold its own disable hostage.** :meth:`admit` never hands
the lock out. It takes it, reads the switch, runs the app's short check off the loop
when the app is enabled, and releases it, all inside one deadline
(:data:`MAX_ADMIT_TIMEOUT_SEC` at most). A check that outlives the deadline is
abandoned: the lock is released, the worker thread runs on and its answer is
discarded, and the app is told it was not admitted. Waiting for a disable that holds
the lock counts against the same deadline, which is also what bounds a call made from
the app's own ``on_shutdown`` hook (the disable holds the lock around that hook, and
the lock is not re-entrant): the call answers ``timed_out`` instead of deadlocking.

**No permission gates it.** It reads only the app's own record and serializes only
against the app's own lifecycle, so it grants no reach an app lacks; it is populated
by :func:`~kiro_crew.apps.context.build_app_context` for every app, like ``audit``.
It is ``None`` on a hand-built context, because a default instance would have to
invent an app name.

The manager facade is imported at call time, so this module stays cheap to import
and aiohttp-free, like the context that carries it.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: The deadline :meth:`AppLifecycleSDK.admit` uses when the app names none.
DEFAULT_ADMIT_TIMEOUT_SEC = 10.0
#: The longest deadline an app may ask for. A longer one is clamped to this, so no
#: app can keep its own disable, update or uninstall waiting for longer than it.
MAX_ADMIT_TIMEOUT_SEC = 30.0


@dataclass(frozen=True)
class Admission:
    """What :meth:`AppLifecycleSDK.admit` decided.

    ``admitted`` is True only when the app was enabled under its lifecycle lock and
    the check, if any, returned within the deadline; ``value`` is then the check's
    return value. ``timed_out`` says the deadline passed (waiting for the lock, or in
    the check), so the app may offer a retry; otherwise a refusal means disabled. A
    check that raised propagates instead of producing an ``Admission``.
    """

    admitted: bool
    value: Any = None
    timed_out: bool = False


def _bounded_timeout(timeout: float) -> float:
    """*timeout* clamped into ``(0, MAX_ADMIT_TIMEOUT_SEC]``; a non-number is the default."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return DEFAULT_ADMIT_TIMEOUT_SEC
    if math.isnan(timeout) or timeout <= 0:
        return DEFAULT_ADMIT_TIMEOUT_SEC
    return min(float(timeout), MAX_ADMIT_TIMEOUT_SEC)


class AppLifecycleSDK:
    """The app's view of its own lifecycle. Reached as ``ctx.lifecycle``."""

    def __init__(self, app_name: str) -> None:
        self._app_name = app_name

    @property
    def app_name(self) -> str:
        """The app this SDK answers for: the context's own name."""
        return self._app_name

    def is_enabled(self) -> bool:
        """Whether this app is installed and switched on.

        A blocking read of the app's install record that never writes, safe on a
        worker thread; call it through ``asyncio.to_thread`` from the event loop. A
        missing record reads as not enabled. For a decision that starts work, use
        :meth:`admit`, which reads the same switch without racing a disable.
        """
        from kiro_crew.apps import manager

        return manager.is_app_enabled(self._app_name)

    async def admit(
        self,
        check: Callable[[], Any] | None = None,
        *,
        timeout: float = DEFAULT_ADMIT_TIMEOUT_SEC,
    ) -> Admission:
        """Decide, under this app's lifecycle lock, whether work may start now.

        Takes the lock, reads the switch and, when the app is enabled, runs *check*
        on a worker thread, then releases the lock. A disable either completes before
        this reads the switch (not admitted), or starts after it and runs the app's
        stop hook later, so a stop counter or run record the check reads stays
        honest. Keep *check* short: it runs while the operator's disable waits.

        Must be awaited on the gateway's event loop. *timeout* (seconds, at most
        :data:`MAX_ADMIT_TIMEOUT_SEC`) covers both the wait for the lock and the
        check; past it the answer is ``Admission(False, timed_out=True)``. An
        exception *check* raises propagates to the caller, after the lock is
        released.
        """
        from kiro_crew.apps import manager

        def _decide() -> Admission:
            if not self.is_enabled():
                return Admission(admitted=False)
            return Admission(admitted=True, value=check() if check is not None else None)

        async def _under_lock() -> Admission:
            async with manager.app_lifecycle_lock(self._app_name):
                return await asyncio.to_thread(_decide)

        try:
            return await asyncio.wait_for(_under_lock(), _bounded_timeout(timeout))
        except asyncio.TimeoutError:
            logger.warning("app %s: lifecycle admission timed out; not admitted", self._app_name)
            return Admission(admitted=False, timed_out=True)

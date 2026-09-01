"""Reusable runtime guard for on-loop SQLite access in the store layer.

The gateway runs one asyncio event loop. A blocking SQLite call made ON that
loop stalls every session's turn while it waits, and a stall held past
``dashboard.loop_stall_exit_after_secs`` (25s) makes the watchdog kill the
process and drop every in-flight turn in every channel (#1572, #3057).

``scripts/check_sync_io_in_async.py`` catches that defect only where it is
written *lexically* inside an ``async def``. It cannot see the same call one
frame down -- an ``async def`` that calls a plain synchronous helper which runs
the query -- and no name-based AST scan can, without whole-program type
inference. Closing that interprocedural half is what #3057 called remedy A: put
the check at the store's connection accessor, where it fires for every caller
regardless of how deep in the stack the call sits.

Adopting the guard in a store is two lines: build one module-level
:class:`OnLoopDBGuard` and call :meth:`OnLoopDBGuard.check` at the top of the
connection accessor.

``history.py`` keeps its own guard rather than adopting this one. Its
:class:`~kiro_crew.history.OnLoopPersistError` subclasses ``AssertionError`` and
it carries a ``ContextVar`` opt-out for the tests that drive its low-level
``_locked`` primitive on purpose -- semantics this module does not reproduce and
that PR must not change.

A NOTE ON THE STRICTNESS SWITCH, because it is the subtle part. "Strict" means
"this surface is fully offloaded, so enforce it". That is a property of a
SURFACE, not of the process, and two surfaces can be at different stages of the
same cleanup. ``KIROCREW_STRICT_ON_LOOP_PERSIST`` is already exported into the
e2e gateway by ``setup.py``'s ``test_e2e`` and ``.github/workflows/ci.yml``,
scoped when it was added to history's clean surface. A store still carrying
known un-offloaded on-loop callers must therefore NOT be armed by that flag, or
arming history's discipline reds an unrelated gate. Hence ``strict_env``: one
parser, one spelling of the truthy/falsy rules, one switch per surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from kiro_crew.constants import ENV_TRUTHY

logger = logging.getLogger(__name__)

#: The shared switch, read by ``history.py``'s own guard and the default here.
#: Already exported by the e2e harness, so it means "history's surface is
#: offloaded" and nothing more.
STRICT_ENV = "KIROCREW_STRICT_ON_LOOP_PERSIST"

#: The store layer's switch. Separate from :data:`STRICT_ENV` so a store with a
#: tracked offload backlog is not armed by the flag CI already exports for
#: history. Export this to make an on-loop store take raise instead of ship.
STORE_STRICT_ENV = "KIROCREW_STRICT_ON_LOOP_STORE"

#: Turning strict on for a developer gateway run.
DEV_MODE_ENV = "KIROCREW_DEV_MODE"

#: An explicit falsy value force-disables strict even under dev mode, so the
#: production warn-and-proceed path stays reachable for testing. Mirrors
#: ``history._ON_LOOP_FALSY``.
_ENV_FALSY = frozenset({"0", "false", "no", "off"})

#: Throttle window for the production diagnostic, so a mis-wired hot path
#: cannot flood the log while the warning still fires once per window.
DEFAULT_WARN_INTERVAL_S = 60.0


class OnLoopStoreError(RuntimeError):
    """A store connection was taken on the event loop under strict mode.

    Raised by :meth:`OnLoopDBGuard.check`. Not an ``AssertionError`` (unlike
    ``history.OnLoopPersistError``) so that running under ``python -O`` cannot
    change whether the discipline is enforced.
    """


def strict_enabled(*, strict_env: str = STRICT_ENV, include_dev_mode: bool = True) -> bool:
    """Whether an on-loop store entry should RAISE rather than warn-and-proceed.

    Resolution order, matching ``history._on_loop_persist_strict()`` exactly
    when both keyword arguments are left at their defaults:

    1. a truthy *strict_env* -> strict (the explicit opt-in);
    2. an explicitly falsy value -> NOT strict, even under dev mode (this is
       what keeps the production path testable);
    3. otherwise, a truthy ``KIROCREW_DEV_MODE`` -> strict, but only when the
       caller opted into that branch via *include_dev_mode*.

    It is deliberately NOT auto-on under bare pytest: the suite's own async
    harness reaches several stores directly on the loop as a convenience rather
    than as a production path, so auto-strict would flag harness code instead of
    real drift.
    """
    raw = os.environ.get(strict_env, "").strip().lower()
    if raw in ENV_TRUTHY:
        return True
    if raw in _ENV_FALSY:
        return False
    return include_dev_mode and os.environ.get(DEV_MODE_ENV, "").strip().lower() in ENV_TRUTHY


class OnLoopDBGuard:
    """One store's on-loop entry guard.

    Each guard keeps its OWN throttle clock, so a chatty store cannot mute the
    diagnostic for an unrelated one -- the reason this is an instance rather
    than the module-level globals the pattern's first copies used.
    """

    __slots__ = ("_label", "_remedy", "_strict_env", "_include_dev_mode", "_warn_last")

    def __init__(
        self,
        *,
        label: str,
        remedy: str,
        strict_env: str = STRICT_ENV,
        dev_mode_arms_strict: bool = True,
    ) -> None:
        """
        :param label: what was opened, for the message ("knowledge store").
        :param remedy: how the caller should have done it -- named concretely,
            because the message is the only thing the person who hits this
            reads.
        :param strict_env: the env var that arms the raise for THIS surface.
            Defaults to the shared :data:`STRICT_ENV`. A store with known
            un-offloaded on-loop callers passes :data:`STORE_STRICT_ENV` (or its
            own name) so that arming another surface's discipline does not raise
            on its tracked backlog -- see the module docstring.
        :param dev_mode_arms_strict: whether ``KIROCREW_DEV_MODE`` alone turns
            this guard strict. Default True, matching ``history.py``. A store
            with a known backlog passes False for the same reason: raising on
            tracked work reports it as if it were a regression, and the
            developer's rational response is to stop exporting
            ``KIROCREW_DEV_MODE`` -- which silences every OTHER surface's guard
            too. Flip it back to True once that backlog is empty.
        """
        self._label = label
        self._remedy = remedy
        self._strict_env = strict_env
        self._include_dev_mode = dev_mode_arms_strict
        # None (not 0.0) means "never warned". A 0.0 sentinel compares against
        # time.monotonic(), which on a freshly-booted host is a small number, so
        # the very first warning could fall inside the throttle window and be
        # swallowed -- exactly the diagnostic that matters most.
        self._warn_last: float | None = None

    @property
    def strict(self) -> bool:
        """Whether this guard is currently in raising mode."""
        return strict_enabled(strict_env=self._strict_env, include_dev_mode=self._include_dev_mode)

    def check(self) -> None:
        """Enforce (strict) or diagnose (production) an on-loop connection take.

        No running event loop means the caller is already off-loop (worker
        thread, executor lane, CLI, cron, subagent) -- the common and correct
        case -- and this is a no-op.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # off-loop: the sanctioned path -- nothing to flag
        if self.strict:
            raise OnLoopStoreError(
                f"{self._label} connection taken on the event loop; a contended "
                f"query here blocks every task (including the watchdog "
                f"heartbeat) for the connection's whole busy timeout, and a "
                f"stall past 25s makes the watchdog kill the gateway. "
                f"{self._remedy}"
            )
        now = time.monotonic()
        last = self._warn_last
        if last is None or now - last >= DEFAULT_WARN_INTERVAL_S:
            self._warn_last = now
            logger.warning(
                "%s: connection taken ON the event loop without offloading; a "
                "contended query here blocks every task (including the watchdog "
                "heartbeat) for the connection's whole busy timeout. %s",
                self._label,
                self._remedy,
                stack_info=True,
            )

    def reset_throttle(self) -> None:
        """Re-open the throttle window. For tests asserting the warn path."""
        self._warn_last = None

"""Which update step this gateway is running owns a missing dashboard bundle.

Some update steps leave the served bundle missing while they run: the
managed-venv installer or a policy ``apply_command`` replacing the install in
place, and a frontend build while ``static/dist`` is still the dev-mode link
into ``website/dist`` (Vite empties its output directory first). If the
stale-asset watchdog shuts the gateway down then, the shutdown cancels the step
mid-write, and a cancelled installer leaves a venv without its console scripts.

So each such step registers here for as long as it rewrites the install
(:func:`step`, :func:`owning`), and so does the restart into an applied update,
which ``_restart_after_update`` and the dashboard's ``_restart_gateway`` own for
as long as they run. The stale-asset watchdog stands down while any entry is
live. Each kind carries a generous maximum duration: an entry past it stops
counting, with a WARNING, so a step wedged on an unbounded wait cannot switch
the watchdog off for good.

A restart the update deliberately deferred is owned for a while too
(:func:`note_restart_deferred`), so the watchdog does not force the restart the
update just put off. That window opens once per pending update and is not
renewed by the coordinator's retries; it ends at its deadline, when a restart
commits (:func:`restart_committed`), or when a restart finds no usable
interpreter (:func:`clear_restart_deferral`): the pruned tree took the bundle
too, and the watchdog's exit is what lets the supervisor relaunch.

In-process only, and deliberately so: a shutdown can cancel only this
process's own steps, never another process's installer. Everything here runs
on the event loop and does no I/O.
"""

from __future__ import annotations

import contextlib
import enum
import functools
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Iterator, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class Step(enum.Enum):
    """An update step that can own a missing bundle: ``(label, max seconds)``.

    The maximum is generous on purpose; it exists only so a wedged step cannot
    own the gap forever. Each comfortably outlasts the step's own bounded calls.
    """

    GIT_AUTO_UPDATE = ("the git auto-update", 3600.0)
    MANAGED_VENV_INSTALLER = ("the managed-venv installer", 1800.0)
    POLICY_APPLY = ("the policy apply command", 900.0)
    DASHBOARD_UPDATE = ("the dashboard update", 3600.0)
    RESTART = ("the restart into an applied update", 300.0)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def max_secs(self) -> float:
        return self.value[1]


#: How long a restart the update deferred keeps owning a missing bundle,
#: counted from the FIRST deferral. The coordinator retries it every few minutes
#: and a retry does not extend it; past this the watchdog's own confirm, drain and
#: exit take over.
DEFERRED_RESTART_MAX_SECS = 900.0


# eq=False: step() removes its OWN entry, and two entries of one kind opened in
# the same clock tick would otherwise compare equal and remove each other's.
@dataclass(eq=False)
class _Entry:
    step: Step
    deadline: float
    expired_logged: bool = False


_live: list[_Entry] = []
_deferred_restart_until: float | None = None

#: This module's clock. Tests replace this name, never ``time.monotonic``: that is
#: the event loop's clock too, and freezing it stops every ``wait_for`` timeout.
_now: Callable[[], float] = time.monotonic


@contextlib.contextmanager
def step(kind: Step) -> Iterator[None]:
    """Own a missing bundle for as long as the block runs, up to the kind's maximum."""
    entry = _Entry(kind, _now() + kind.max_secs)
    registry = _live
    registry.append(entry)
    try:
        yield
    finally:
        registry.remove(entry)


def owning(
    kind: Step,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Decorate a coroutine function so each call owns a missing bundle while it runs.

    The context manager :func:`step` cannot decorate a coroutine function
    directly: it would own the gap only while the coroutine OBJECT is created.
    """

    def _decorate(fn: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(fn)
        async def _owned(*args: P.args, **kwargs: P.kwargs) -> R:
            with step(kind):
                return await fn(*args, **kwargs)

        return _owned

    return _decorate


def note_restart_deferred() -> None:
    """A restart into an applied update was deferred: keep owning the gap a while.

    Armed once: a retry that defers again keeps the first deadline, so a deferral
    that never clears cannot hold the watchdog off for good.
    """
    global _deferred_restart_until
    if _deferred_restart_until is None:
        _deferred_restart_until = _now() + DEFERRED_RESTART_MAX_SECS


def restart_committed() -> None:
    """A restart is past its last refusal and about to exec: no deferral stands.

    Called where the restart commits (right before it closes the sessions), not
    where it starts, so a restart that coalesces, or refuses with an interpreter
    still in place, leaves the deferral it found in place.
    """
    clear_restart_deferral()


def clear_restart_deferral() -> None:
    """No deferral stands: a restart committed, or found no usable interpreter.

    The no-interpreter refusal clears it rather than leave it standing, because
    a deferral armed by an earlier drain would otherwise keep the watchdog from
    the exit that lets the supervisor relaunch through its own command.
    """
    global _deferred_restart_until
    _deferred_restart_until = None


def current_owner() -> str | None:
    """The label of what owns a missing bundle right now, or ``None``.

    The newest live entry names the owner; an expired one is skipped, whatever
    its kind. ``_live`` is shared by every task in the process, so the entry
    before an expired restart can be an unrelated update step that still owns
    the gap (no update step nests its restart: it ends its own entry first).
    """
    now = _now()
    for entry in reversed(_live):
        if now < entry.deadline:
            return entry.step.label
        if not entry.expired_logged:
            entry.expired_logged = True
            logger.warning(
                "%s has run past its %.0fs maximum; it no longer holds off the "
                "stale-asset watchdog.",
                entry.step.label,
                entry.step.max_secs,
            )
    if _deferred_restart_until is not None and now < _deferred_restart_until:
        return "a deferred restart into an applied update"
    return None

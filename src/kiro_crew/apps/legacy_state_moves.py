"""Core-owned one-time moves of a built-in's state out of an earlier core-owned layout.

A built-in whose earlier layout kept state where core masked and handed it back by name
(outside the app's own ``data/``) has its state moved into ``apps/<name>/data/`` by
core, once, in the gateway. Until that move seals, the app's backend must not start (it
would open an empty state directory), and adoption must not swap the record onto its
repository: the adopted backend is not the shipped one whose start runs the move, so on
a host where the move had not sealed yet (the app disabled at the upgrade, or a sandbox
of an earlier build still alive) the state would stay where only core reads it, out of
the app's reach, and to the user it would be gone.

:data:`LEGACY_STATE_MOVES` is the table: one data row per app, written by core and
never read from a manifest, so an app cannot add, widen or name a move. A row carries
two callables of the core module that owns the move:

- ``seal()``: run the move under its own safety checks and answer whether the state is
  final in the app's data directory. It never raises; a False is retried by the next
  caller.
- ``start_refusal()``: why the app's backend must not start now (the move not sealed
  included), or ``None``.

Who asks, and on what provenance (the move writes the state into ``apps/<name>/data/``,
so it runs only for a record that is the former built-in, never for another app that
took the name):

- the backend spawn, for the shipped built-in (``is_builtin_app`` on the executed path)
  or a record adopted from the wheel's pin
  (``builtin_adoption_gate.adopted_under_pin_trust``): :func:`backend_start_refusal`;
- APPLY, for a builtin-owned record about to be swapped: :func:`sealed_for_adoption`,
  which holds the swap (the ledger keeps ``staged``) until the move seals, whether or
  not the app is enabled, and is asked again at the next boot;
- APPLY, for a record already adopted under pin trust: :func:`seal_adopted`, so a host
  that adopted before its move sealed still gets it at every boot, enabled or not.

The row keys are the one place the adoption machinery names an app. The exception is
narrow: a row exists only for a layout core itself created, so the move's code is
core's, and so is the row that binds it to the app. The built-in cannot declare it, since
its package leaves the wheel while an adopted host still needs the move. A row goes with
its move, once no supported upgrade path can still hold that layout.
``test_builtin_adoption_pins.py`` fails on a key outside its stated exception list, and
``test_core_md_notebook_decoupling.py`` lists the Notes key with that reason. Owning
spec: app-kit-platform.md § Former built-in adoption, "A legacy state move holds the
swap".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from kiro_crew import md_notebook_state as _md_notebook_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacyStateMove:
    """One app's core-owned state move: see the module docstring for both callables."""

    seal: Callable[[], bool]
    start_refusal: Callable[[], str | None]


#: The core-owned rows, keyed by app name: each key is a literal, so both name gates see
#: it. Each callable is looked up on its owner at call time, so the owner's own tests can
#: patch it.
LEGACY_STATE_MOVES: Mapping[str, LegacyStateMove] = MappingProxyType(
    {
        "md-notebook": LegacyStateMove(
            seal=lambda: _md_notebook_state.sealed_default_legacy_state(),
            start_refusal=lambda: _md_notebook_state.backend_start_refusal(),
        ),
    }
)


def legacy_state_move(name: str) -> LegacyStateMove | None:
    """*name*'s row, or ``None`` for every app core moves no state for."""
    return LEGACY_STATE_MOVES.get(name)


def sealed_for_adoption(name: str) -> bool:
    """Whether adoption may swap *name*'s builtin-owned record now. Blocking; never raises.

    True when *name* has no row. Otherwise runs the row's move and answers whether it
    sealed. The caller holds *name*'s lifecycle lock and has checked that the record is
    builtin-owned, so the move writes into the built-in's own ``data/``.
    """
    move = legacy_state_move(name)
    if move is None:
        return True
    if move.seal():
        return True
    logger.warning(
        "Adoption of %s waits: its one-time state move has not finished (see the warning "
        "above); the built-in keeps running, and the next start tries again",
        name,
    )
    return False


def seal_adopted(name: str) -> None:
    """Run *name*'s move if its record is adopted from the wheel's pin. Blocking; never raises.

    A record adopted under any other trust (a third-party record under the name, a grant
    bound to another repository) gets nothing: the move would write the former built-in's
    state into that app's data directory.
    """
    move = legacy_state_move(name)
    if move is None:
        return
    from kiro_crew.apps.builtin_adoption_gate import adopted_under_pin_trust

    if adopted_under_pin_trust(name):
        move.seal()


def backend_start_refusal(name: str, *, builtin: bool) -> str | None:
    """Why *name*'s backend must not start now, or ``None``. Blocking.

    *builtin* is whether this spawn executes the shipped built-in (``is_builtin_app`` on
    the executed path). Asked of the row only for that, or for a record adopted under pin
    trust; every other app, every app without a row included, starts as before.
    """
    move = legacy_state_move(name)
    if move is None:
        return None
    if not builtin:
        from kiro_crew.apps.builtin_adoption_gate import adopted_under_pin_trust

        if not adopted_under_pin_trust(name):
            return None
    return move.start_refusal()

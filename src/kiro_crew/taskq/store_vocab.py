"""The vocabulary every ``TaskStore`` owner shares: the state-set SQL and the store's error.

``TaskStoreUnavailable`` is the one error type a store method reports a database failure
as, and ``_typed_read`` is how a read reports one. The ``_SQL_*`` fragments spell the
``model`` state sets that several owners filter on as the ``IN (...)`` lists their
statements interpolate; a fragment one owner alone reads lives in that owner.

``kiro_crew.taskq.store`` stays the import path of every name here. The store's owners
import them from this module because an owner never imports the store itself.
"""

from __future__ import annotations

import functools
import sqlite3
from typing import Any, Callable

from .model import ACTIVE, ADMITTED, CLAIMABLE, TERMINAL

_SQL_CLAIMABLE = "(" + ",".join(f"'{s}'" for s in sorted(CLAIMABLE)) + ")"
_SQL_TERMINAL = "(" + ",".join(f"'{s}'" for s in sorted(TERMINAL)) + ")"
_SQL_ACTIVE = "(" + ",".join(f"'{s}'" for s in sorted(ACTIVE)) + ")"
#: Claimable rows plus ``admitted`` ones: every row accepted and not yet started.
#: A caller that subtracts the rows this process has registered as runs is left
#: with exactly the accepted work no run exists for yet (``include_admitted``).
_UNSTARTED: frozenset[str] = CLAIMABLE | {ADMITTED}
_SQL_UNSTARTED = "(" + ",".join(f"'{s}'" for s in sorted(_UNSTARTED)) + ")"


class TaskStoreUnavailable(RuntimeError):
    """The store could not answer; a write did not commit.

    Raised for a locked or unwritable database, a schema the build cannot
    write, or a full disk. Never raised for a row the transition table
    refuses -- that is a ``False`` return, because the store is fine.

    It is the ONE error type every public method of :class:`TaskStore` reports a
    database failure as, reads included (:func:`_typed_read`). A read that let
    ``sqlite3.Error`` through would be indistinguishable from a bug in this
    module at every call site, and ``journal_mode=delete`` -- what a data home
    on a network filesystem gets -- blocks readers behind a competing writer,
    so a bare ``SELECT`` is exactly as failure-prone there as a transaction.
    """


# A traceback renders a raised class as ``module.qualname``; this one renders under the
# store's import path, where callers, logs and docs name it.
TaskStoreUnavailable.__module__ = "kiro_crew.taskq.store"


def _typed_read(fn: "Callable[..., Any]") -> "Callable[..., Any]":
    """Report a read's database failure as :class:`TaskStoreUnavailable`.

    Every WRITE on this class already converts ``sqlite3.Error`` at its own
    boundary; a read must too, because a caller cannot act on a distinction it
    did not choose. That the gap was an omission and not a design is visible in
    the callers: ``except TaskStoreUnavailable`` already guards ``state_of`` /
    ``get`` / ``events`` / ``count_pending`` / ``list_rows`` / ``active_rows``
    reads across the runner adapter, the dependency coordinator and the
    admission bridge -- arms that cannot fire for a read raising
    ``sqlite3.OperationalError`` instead. Wrapping HERE makes those arms live
    once; an ``except sqlite3.Error`` per call site would be the same rule
    restated at every read in those three subsystems, each copy free to drift.
    """

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise TaskStoreUnavailable(f"task store read failed: {exc}") from exc

    return _wrapper

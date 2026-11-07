"""The rate-limited failure report the crew-log writer and the emitter share.

Split out of :mod:`kiro_crew.crew_log.writer`, which re-exports it, so that import path
keeps working. Records go to the writer's logger name by default, as before the split.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable

_logger = logging.getLogger("kiro_crew.crew_log.writer")


class WarningBudget:
    """Report a failure at warning level once per KIND, then stay quiet for a window.

    :meth:`kind` keys on the operation and the exception, so an ENOSPC is still named
    after an unrelated listener error spent a slot. Only *op* (a fixed string) reaches the
    key; *what* (store, unit or entry type) is for the reader.

    Repeats of a named kind are swallowed and COUNTED, and the count rides on the next
    warning for that kind after ``rearm_seconds``, so a spent budget still says so.

    Both records carry the failure as TEXT (the traceback rendered while the exception is
    live, not ``exc_info``). A handler that keeps records (pytest capture, a
    ``MemoryHandler``) would otherwise keep the job frames and the ``CrewLog`` handle in
    them; see :meth:`CrewLogWriter._run_job` for why that handle must not outlive the pass.

    Shared by the writer and the emitter, so one budget governs the crew log path.
    ``max_kinds`` bounds the map (oldest evicted); key parts are program and OS constants,
    so routine traffic never reaches it.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        rearm_seconds: float = 300.0,
        max_kinds: int = 64,
    ) -> None:
        self._log = logger if logger is not None else _logger
        self._clock = clock
        self._rearm_seconds = rearm_seconds
        self._max_kinds = max_kinds
        self._lock = threading.Lock()
        #: Failure kind -> (clock instant it last warned, failures swallowed since).
        self._spent: "OrderedDict[tuple[str, str, str], tuple[float, int]]" = OrderedDict()

    @staticmethod
    def kind(op: str, exc: BaseException) -> "tuple[str, str, str]":
        """What KIND of failure this is, for the budget to spend a slot on.

        Three stable parts: the operation that failed, the exception's class, and the
        code the operating system or the store put on it. So a disk that is full and a
        disk that is failing arrive as two kinds out of one ``OSError``, and a lost write
        lease is not filed under an unrelated listener's ``RuntimeError``.

        What is deliberately absent is the UNIT -- no store name, no session id, no entry
        type. Those live in ``what`` for the message and never in the key, because many
        units failing at once is one cause repeating, and a key holding the unit would
        hand each of them its own warning and flood the log this budget exists to
        protect. Every part is a program or OS constant, so the map is bounded with no
        list of kinds for anyone to maintain: a new call site gets its own slot by naming
        its own ``op``.
        """
        code = getattr(exc, "code", "") or getattr(exc, "errno", "")
        return (op, type(exc).__qualname__, str(code or ""))

    def report(self, what: str, exc: BaseException, *, op: str) -> None:
        """Name *exc* at warning level unless its kind already spoke this window."""
        kind = self.kind(op, exc)
        now = self._clock()
        with self._lock:
            held = self._spent.get(kind)
            if held is None:
                speak, swallowed = True, 0
            else:
                warned_at, swallowed = held
                speak = now - warned_at >= self._rearm_seconds
            if speak:
                self._spent[kind] = (now, 0)
                self._spent.move_to_end(kind)
                while len(self._spent) > self._max_kinds:
                    self._spent.popitem(last=False)
            else:
                self._spent[kind] = (warned_at, swallowed + 1)
        if speak:
            self._log.warning(
                "session log writes are failing (%s: %s%s)%s; further failures of "
                "this kind are logged at debug only for the next %.0fs",
                what,
                str(exc),
                f", code={getattr(exc, 'code', '')}" if getattr(exc, "code", "") else "",
                (
                    f", and {swallowed} more went unreported since it was last named"
                    if swallowed
                    else ""
                ),
                self._rearm_seconds,
            )
        elif self._log.isEnabledFor(logging.DEBUG):
            # The traceback rendered to text while the exception is live: full
            # diagnostics on the record, and a string holds no frames.
            self._log.debug(
                "session log %s failed:\n%s",
                what,
                "".join(traceback.format_exception(exc)).rstrip(),
            )

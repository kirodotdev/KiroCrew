"""A clock that makes the resolver child's answer arrive "late" without any real delay.

Under CPU load the thread that sent a request to the resolver child can get the GIL
back after its deadline although the child answered in time: the answer is already
in the pipe and only the read is late.  :class:`LateAnswerClock` reproduces that
order of events deterministically.  On the installing thread, each bounded read in
the executor first waits for the child's answer to be readable, then moves the
executor's monotonic clock an hour forward, so the deadline has passed by the time
``_read_exactly`` looks at the clock.  Other threads (the pool's reaper) keep the
real clock.

``test_subprocess_pool.py`` and ``test_security_path_gate_under_load.py`` import it
by name.
"""

from __future__ import annotations

import select
import threading
from typing import Any

import pytest

from kiro_crew.subprocess_pool import executor as executor_mod

# Longer than any budget a test passes, so one jump always passes the deadline.
_JUMP_SECS = 3600.0

# How long a read waits for the child's answer before the double gives up.  A
# healthy child answers in milliseconds; this bounds a broken one.
_ANSWER_WAIT_SECS = 30.0


class LateAnswerClock:
    """Install on the current thread; ``late_reads`` counts the reads it made late."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tid = threading.get_ident()
        self._offset = 0.0
        self.late_reads = 0
        # The ``drain_secs`` each late read was made with, header read first.
        self.drains: list[float] = []
        real_time = executor_mod.time
        real_read = executor_mod._read_exactly
        clock = self

        class _ThreadScopedTime:
            """The ``time`` module, with ``monotonic`` shifted on the installing thread only."""

            def __getattr__(self, name: str) -> Any:
                return getattr(real_time, name)

            @staticmethod
            def monotonic() -> float:
                if threading.get_ident() == clock._tid:
                    return real_time.monotonic() + clock._offset
                return real_time.monotonic()

        def _late_read(
            stream: Any, count: int, deadline: float | None, **kwargs: Any
        ) -> bytes | None:
            if deadline is not None and threading.get_ident() == clock._tid:
                ready, _, _ = select.select([stream.fileno()], [], [], _ANSWER_WAIT_SECS)
                assert ready, "the resolver child never answered"
                clock._offset += _JUMP_SECS
                clock.late_reads += 1
                clock.drains.append(kwargs.get("drain_secs", 0.0))
            return real_read(stream, count, deadline, **kwargs)

        monkeypatch.setattr(executor_mod, "time", _ThreadScopedTime())
        monkeypatch.setattr(executor_mod, "_read_exactly", _late_read)

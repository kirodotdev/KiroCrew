"""Regression: Stats.__new__ must fully initialize before publishing (#427).

Publishing ``cls._instance`` before ``_init_counters()`` let a second thread on
the lock-free fast path observe a half-built instance (no ``_mu`` / ``_c``) and
raise AttributeError on the next ``.snapshot()`` / ``.inc_*()``.
"""

from __future__ import annotations

import threading

from kiro_crew.stats import Stats


def test_new_publishes_only_after_init() -> None:
    # Force __new__ down its build path under contention.
    Stats._instance = None
    errors: list[BaseException] = []
    barrier = threading.Barrier(32)

    def worker() -> None:
        try:
            barrier.wait()
            # A half-built singleton would raise AttributeError here.
            Stats().snapshot()
            Stats().inc_message_received()
        except BaseException as exc:  # noqa: BLE001 -- capture any race fallout
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"half-built singleton observed: {errors!r}"
    # Fully-initialized: counters exist and increments landed.
    assert "messages_received" in Stats().snapshot()
    Stats().reset()

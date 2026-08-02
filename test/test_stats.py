"""Tests for kiro_crew.stats module."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from kiro_crew.stats import Stats


class TestStats(unittest.TestCase):

    def setUp(self) -> None:
        Stats().reset()

    # -- singleton --

    def test_singleton(self) -> None:
        assert Stats() is Stats()

    # -- counters --

    def test_increment_and_read(self) -> None:
        s = Stats()
        s.inc_message_received()
        s.inc_message_received()
        s.inc_message_success()
        s.inc_tool_approval()
        snap = s.snapshot()
        assert snap["messages_received"] == 2
        assert snap["messages_success"] == 1
        assert snap["tool_approvals"] == 1
        assert snap["messages_failed"] == 0

    # -- summary --

    def test_summary_format(self) -> None:
        s = Stats()
        s.inc_message_received()
        s.inc_message_success()
        text = s.summary()
        assert "msgs 1" in text
        assert "ok 1" in text
        assert "uptime" in text

    # -- daily_report health levels --

    def test_daily_report_healthy(self) -> None:
        s = Stats()
        for _ in range(10):
            s.inc_message_received()
        for _ in range(9):
            s.inc_message_success()
        s.inc_message_failed()
        report = s.daily_report()
        assert "🟢 healthy" in report
        assert "90%" in report

    def test_daily_report_degraded(self) -> None:
        s = Stats()
        for _ in range(10):
            s.inc_message_received()
        for _ in range(8):
            s.inc_message_success()
        for _ in range(2):
            s.inc_message_failed()
        report = s.daily_report()
        assert "🟡 degraded" in report
        assert "80%" in report

    def test_daily_report_critical(self) -> None:
        s = Stats()
        for _ in range(10):
            s.inc_message_received()
        for _ in range(5):
            s.inc_message_success()
        for _ in range(5):
            s.inc_message_failed()
        report = s.daily_report()
        assert "🔴 critical" in report
        assert "50%" in report

    def test_daily_report_no_messages(self) -> None:
        report = Stats().daily_report()
        assert "🔇 no messages" in report

    # -- reset --

    def test_reset(self) -> None:
        s = Stats()
        s.inc_message_received()
        s.inc_tool_approval()
        s.reset()
        snap = s.snapshot()
        assert all(v == 0 for v in snap.values())

    # -- uptime --

    def test_uptime_str(self) -> None:
        s = Stats()
        with patch("kiro_crew.stats.time") as mock_time:
            mock_time.monotonic.return_value = s._start_time + 3661
            assert s.uptime_str() == "1h 1m"

    def test_uptime_str_with_days(self) -> None:
        s = Stats()
        with patch("kiro_crew.stats.time") as mock_time:
            mock_time.monotonic.return_value = s._start_time + 3 * 86400 + 14 * 3600 + 22 * 60
            assert s.uptime_str() == "3d 14h 22m"

    # -- snapshot keys --

    def test_snapshot_keys(self) -> None:
        expected = {
            "messages_received",
            "messages_success",
            "messages_failed",
            "tool_approvals",
            "tool_denials",
            "tool_auto_approved",
            "timeouts",
            "sessions_created",
            "sessions_cleaned",
            "subagents_spawned",
            "subagents_completed",
            "subagents_failed",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total_turns",
            "total_duration_ms",
        }
        assert set(Stats().snapshot().keys()) == expected

    # -- thread safety --

    def test_singleton_publish_after_init(self) -> None:
        """Regression test for #427: singleton must be fully initialized before publish.

        This test deterministically triggers the race by injecting a barrier into
        _init_counters that forces the following interleaving:

        Thread A                          Thread B
        --------                          --------
        __new__: acquires lock
        __new__: creates instance
        __new__: calls _init_counters
          _init_counters: barrier.wait()
                                          __new__: lock-free check sees non-None
                                          __new__: returns half-built instance
                                          snapshot(): AttributeError on _mu/_c
          _init_counters: finishes

        If the singleton is published before _init_counters completes, thread B
        sees a half-built instance and snapshot() raises AttributeError. The fix
        is to assign cls._instance only after _init_counters returns.
        """
        # Reset singleton so we can create a fresh one
        Stats._instance = None

        barrier = threading.Barrier(2)
        results: list[object] = [None, None]  # [thread_b_result_or_error, thread_a_instance]

        original_init_counters = Stats._init_counters

        def slow_init_counters(self: Stats) -> None:
            # Signal thread B that we're inside _init_counters but not done
            barrier.wait()
            # Give thread B time to grab the half-built instance and call snapshot
            import time

            time.sleep(0.05)
            original_init_counters(self)

        def thread_a() -> None:
            with patch.object(Stats, "_init_counters", slow_init_counters):
                results[1] = Stats()

        def thread_b() -> None:
            barrier.wait()  # Wait for thread A to be inside _init_counters
            try:
                inst = Stats()
                # Try to use the instance - this will fail if it's half-built
                results[0] = inst.snapshot()
            except AttributeError as e:
                results[0] = e

        t_a = threading.Thread(target=thread_a)
        t_b = threading.Thread(target=thread_b)

        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        # With the fix, thread B should get a fully initialized instance (dict result)
        # Without the fix, thread B gets AttributeError
        assert isinstance(
            results[0], dict
        ), f"Expected snapshot dict, got {type(results[0]).__name__}: {results[0]}"

    def test_thread_safety(self) -> None:
        s = Stats()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            for _ in range(100):
                s.inc_message_received()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert s.snapshot()["messages_received"] == 1000


if __name__ == "__main__":
    unittest.main()

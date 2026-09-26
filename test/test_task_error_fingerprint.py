from __future__ import annotations

from kiro_crew.task_executor import _error_fingerprint


class TestErrorFingerprint:
    def test_volatile_runs_match(self) -> None:
        first = "Connection reset by peer (port 51234) after 120s pid 9912"
        second = "Connection reset by peer (port 51235) after 121s pid 9940"
        assert _error_fingerprint(first) == _error_fingerprint(second)

    def test_distinct_failures_differ(self) -> None:
        assert _error_fingerprint("No module named foo") != _error_fingerprint(
            "No module named bar"
        )

    def test_test_output_truncates_to_identity(self) -> None:
        head = "FAILED test/test_x.py::test_y - assert 1 == 2\n"
        first = "Tests failed:\n" + head + "ticks: 12\n" + "x" * 5000
        second = "Tests failed:\n" + head + "ticks: 13\n" + "x" * 5000
        assert _error_fingerprint(first) == _error_fingerprint(second)

    def test_different_tests_differ(self) -> None:
        first = "Tests failed:\nFAILED test/test_x.py::test_y - assert 1 == 2"
        second = "Tests failed:\nFAILED test/test_x.py::test_z - assert 1 == 2"
        assert _error_fingerprint(first) != _error_fingerprint(second)

    def test_fingerprint_is_bounded(self) -> None:
        assert len(_error_fingerprint("E" * 100_000)) <= 1000

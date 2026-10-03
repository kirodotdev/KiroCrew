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

    def test_distinct_errors_differ_only_by_number(self) -> None:
        """Bare digits are identity, not noise.

        Masking every digit run made a steadily-advancing agent look stuck:
        ``error variant 1`` and ``error variant 2`` are different work, and on
        the third attempt the fingerprint replaced the real error with
        "Loop detected". Pinned by
        ``test/test_scenarios_v2_logic.py::TestScenarioCycleDetection::
        test_different_errors_no_cycle``.
        """
        assert _error_fingerprint("error variant 1") != _error_fingerprint("error variant 2")

    def test_distinct_assertion_values_differ(self) -> None:
        assert _error_fingerprint("assert 3 == 0") != _error_fingerprint("assert 1 == 0")

    def test_test_output_truncates_to_identity(self) -> None:
        """The failing-test identity survives ``run_tests``' tail truncation.

        ``run_tests`` trims a failing run to its last 2000 chars, so the
        ``FAILED`` summary is the stable part rather than the head of the text.
        """
        summary = "FAILED test/test_x.py::test_y - assert 1 == 2"
        first = "Tests failed:\n" + "x" * 5000 + "\n" + summary + "\nticks: 12\n"
        second = "Tests failed:\n" + "x" * 5000 + "\n" + summary + "\nticks: 13\n"
        assert _error_fingerprint(first) == _error_fingerprint(second)

    def test_different_tests_differ(self) -> None:
        first = "Tests failed:\nFAILED test/test_x.py::test_y - assert 1 == 2"
        second = "Tests failed:\nFAILED test/test_x.py::test_z - assert 1 == 2"
        assert _error_fingerprint(first) != _error_fingerprint(second)

    def test_fingerprint_is_bounded(self) -> None:
        assert len(_error_fingerprint("E" * 100_000)) <= 1000

    def test_long_summaries_differing_past_the_bound_differ(self) -> None:
        shared = "Tests failed:\n" + "\n".join(
            f"FAILED test/test_a.py::test_{i} - boom" for i in range(40)
        )
        first = shared + "\nFAILED test/test_b.py::test_x - boom"
        second = shared + "\nFAILED test/test_b.py::test_y - boom"
        assert len(shared) > 1000
        assert _error_fingerprint(first) != _error_fingerprint(second)
        assert _error_fingerprint(first) == _error_fingerprint(first + "\nticks: 7\n")

    def test_generic_error_with_error_log_lines_keeps_its_identity(self) -> None:
        first = "RuntimeError: db locked\nERROR    root: retrying\nERROR    root: giving up"
        second = "ValueError: bad config\nERROR    root: retrying\nERROR    root: giving up"
        assert _error_fingerprint(first) != _error_fingerprint(second)

    def test_short_hex_parametrized_cases_differ(self) -> None:
        first = "Tests failed:\nFAILED test/test_x.py::test_y[0x1] - boom"
        second = "Tests failed:\nFAILED test/test_x.py::test_y[0x2] - boom"
        assert _error_fingerprint(first) != _error_fingerprint(second)
        addr_a = "Tests failed:\nFAILED test/test_x.py::test_y - <Obj at 0x7f3a9c0012d0>"
        addr_b = "Tests failed:\nFAILED test/test_x.py::test_y - <Obj at 0x7f3a9c00ffe0>"
        assert _error_fingerprint(addr_a) == _error_fingerprint(addr_b)

    def test_separator_and_timezone_forms_are_volatile(self) -> None:
        first = "bind failed port: 51234 pid=9912 at 2026-09-29T05:00:00Z"
        second = "bind failed port: 51299 pid=9001 at 2026-09-29T05:03:11Z"
        assert _error_fingerprint(first) == _error_fingerprint(second)

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

    def test_time_unit_node_id_params_are_identity(self) -> None:
        """A parametrized run advancing through time-unit ids is not a loop.

        ``pytest -x`` steps through ``[1s]``, ``[2s]``, ``[30m]`` -- each a
        different case. The duration mask would collapse those bracketed node
        ids to one fingerprint, so on the third failure ``_check_error_loop``
        would overwrite the real error with "Loop detected" and fail the step.
        Bracketed node-id params are lifted out before masking, so they stay
        distinct; this repo's own suite uses such ids (e.g. ``[20s]``,
        ``[99999h]`` in ``test/test_instances.py``).
        """
        a = "Tests failed:\nFAILED test/test_x.py::test_y[1s] - boom"
        b = "Tests failed:\nFAILED test/test_x.py::test_y[2s] - boom"
        c = "Tests failed:\nFAILED test/test_x.py::test_y[30m] - boom"
        assert _error_fingerprint(a) != _error_fingerprint(b)
        assert _error_fingerprint(b) != _error_fingerprint(c)

    def test_volatile_forms_outside_node_ids_still_mask(self) -> None:
        """Protecting node ids must not stop masking volatile text elsewhere."""
        first = "Tests failed:\nFAILED test/test_x.py::test_y[cold] - timed out after 120s"
        second = "Tests failed:\nFAILED test/test_x.py::test_y[cold] - timed out after 121s"
        assert _error_fingerprint(first) == _error_fingerprint(second)

    def test_bracketed_log_prefix_outside_a_node_id_is_masked(self) -> None:
        """A bracketed volatile prefix that is NOT a node id compares equal.

        ``[2026-09-29T05:00:00Z]``, ``[pid 9912]`` and ``[120ms]`` are log
        prefixes, not pytest node ids: the ``[`` is not attached to an
        identifier. Only an identifier-attached ``[...]`` (``test_x[case-1]``)
        is protected, so these prefixes have their volatile contents masked and
        the same error gets the same fingerprint on every retry -- otherwise
        ``_check_error_loop`` would never fire for an error whose only moving
        part is such a prefix.
        """
        # Timestamp prefix.
        ts_a = "[2026-09-29T05:00:00Z] ConnectionError: reset"
        ts_b = "[2026-09-29T05:03:11Z] ConnectionError: reset"
        assert _error_fingerprint(ts_a) == _error_fingerprint(ts_b)
        # pid prefix.
        pid_a = "[pid 9912] worker exited"
        pid_b = "[pid 9940] worker exited"
        assert _error_fingerprint(pid_a) == _error_fingerprint(pid_b)
        # duration prefix.
        dur_a = "[120ms] slow query"
        dur_b = "[121ms] slow query"
        assert _error_fingerprint(dur_a) == _error_fingerprint(dur_b)
        # An identifier-attached node id with the same bracket shape stays
        # distinct across cases -- it is identity, not a volatile prefix.
        node_a = "Tests failed:\nFAILED test/test_x.py::test_y[case-1] - boom"
        node_b = "Tests failed:\nFAILED test/test_x.py::test_y[case-2] - boom"
        assert _error_fingerprint(node_a) != _error_fingerprint(node_b)

    def test_same_test_failing_at_different_locations_is_not_a_loop(self) -> None:
        """A test converging through different assertions must not read as a loop.

        On a non-tty pytest truncates the ``FAILED <id> - <msg>`` summary line to
        the terminal width, so the assertion message is often cut off. If a step
        fixes one assertion and the SAME test then fails at the next assertion,
        all attempts would share the one truncated summary line and
        ``_check_error_loop`` would fire -- the exact false loop this feature
        exists to avoid. The failure LOCATION (the ``E`` line and the
        ``<path>:<line>: <ExcType>`` traceback line) is folded into the identity,
        so two different failing locations give two different fingerprints.
        """
        # The summary line is identical (truncated to the node id); only the
        # failing location differs between the two attempts.
        first = (
            "Tests failed:\n"
            "=================================== FAILURES ===================================\n"
            "____________________________________ test_y ___________________________________\n"
            ">       assert first_step == expected\n"
            "E       assert 1 == 2\n"
            "test/test_x.py:17: AssertionError\n"
            "=========================== short test summary info ============================\n"
            "FAILED test/test_x.py::test_y - assert 1 == 2\n"
        )
        second = (
            "Tests failed:\n"
            "=================================== FAILURES ===================================\n"
            "____________________________________ test_y ___________________________________\n"
            ">       assert second_step == expected\n"
            "E       assert 3 == 4\n"
            "test/test_x.py:42: AssertionError\n"
            "=========================== short test summary info ============================\n"
            "FAILED test/test_x.py::test_y - assert 3 == 4\n"
        )
        assert _error_fingerprint(first) != _error_fingerprint(second)

    def test_identical_test_failure_still_reads_as_a_loop(self) -> None:
        """A genuinely stuck test -- same location, same assertion -- still collapses.

        The location is part of the identity, but it is the SAME location on
        every attempt here, and the only text that moves is a volatile value
        (an address). The two attempts must still share one fingerprint so loop
        detection fires on a truly stuck test.
        """
        first = (
            "Tests failed:\n"
            ">       assert obj is None\n"
            "E       assert <Thing at 0x7f3a9c0012d0> is None\n"
            "test/test_x.py:9: AssertionError\n"
            "FAILED test/test_x.py::test_z - assert <Thing at 0x7f3a9c0012d0> is None\n"
        )
        second = (
            "Tests failed:\n"
            ">       assert obj is None\n"
            "E       assert <Thing at 0x7f3a9c00ffe0> is None\n"
            "test/test_x.py:9: AssertionError\n"
            "FAILED test/test_x.py::test_z - assert <Thing at 0x7f3a9c00ffe0> is None\n"
        )
        assert _error_fingerprint(first) == _error_fingerprint(second)

"""Windows collection rules for the Spec Builder backend suite.

Mirrors the convention in ``test/conftest.py``: a named burn-down list of tests
skipped on Windows, rather than scattering ``skipif`` markers. That conftest
cannot cover this suite -- its hook is rooted at ``test/`` and these tests live
under ``src/kiro_crew/apps/builtins/`` -- so the same policy is applied here for
the one file that needs it.

Why these specific tests, and why skipping is correct rather than a paper-over:
``routes.py`` pins the spec directory with a non-following ``O_DIRECTORY``
descriptor and does every sentinel create/rename/unlink relative to that
descriptor. Windows has neither ``O_NOFOLLOW`` nor ``dir_fd`` support, so
``_CAN_PIN_DIR`` is False and ``_write_stop_sentinel`` / ``_clear_stop_sentinel``
**fail closed by design** -- they refuse to operate by path, because the agent
runs inside the user's project and could swap the directory for a junction
between the check and the write. The tests below assert the pinned POSIX
behaviour, so on Windows they are asserting a capability the product
deliberately declines to have.

Skipping is therefore recording a platform boundary the source already states,
not hiding a defect: the Windows behaviour (no sentinel; halt via loop removal
plus turn cancellation, both in-process) is itself covered by the tests that do
run. The remaining entries are path-shape gaps of the same per-feature kind
``test/conftest.py`` already tracks.

Delete a line when its test is made portable. Anything NOT listed still fails
the Windows job, so this list cannot silently absorb a real regression.
"""

from __future__ import annotations

import pytest

from kiro_crew import platform_compat

#: POSIX ``dir_fd`` pinning is unavailable, so the sentinel helpers fail closed
#: and every assertion about a written/cleared STOP file is unreachable.
_POSIX_SENTINEL_PINNING = {
    "test_sentinel_helpers_fail_closed_without_directory_pinning",
    "test_sentinel_write_is_pinned_to_the_verified_directory",
    "test_sentinel_clear_is_pinned_to_the_verified_directory",
    "test_stop_sentinel_write_destroys_planted_symlink",
    "test_prepare_handoff_reports_tasks_and_clears_a_stale_sentinel",
    "test_prepare_handoff_refuses_a_tasks_file_with_no_open_task",
    "test_prepare_handoff_still_clears_for_the_matching_identity",
    "test_prepare_handoff_unpinned_call_keeps_working",
    "test_stop_write_is_refused_for_a_replaced_spec",
    "test_halt_execution_leaves_user_trust_alone",
    "test_verified_spec_dir_accepts_an_ordinary_directory",
    # Duplicate creation writes through the SAME pinned descriptor and fails
    # closed without it. These assert bytes written by that POSIX-only path.
    "test_duplicate_doc_create_only_succeeds_while_the_file_is_absent",
    "test_duplicate_doc_create_retries_short_writes",
    "test_duplicate_copies_the_documents_into_a_fresh_spec",
}

#: POSIX path shape: separators, absolute-path spelling and ``~`` expansion all
#: differ, so these compare against a literal that is only correct on POSIX.
_POSIX_PATH_SHAPE = {
    "test_only_our_own_slot_is_adopted_and_a_missing_one_is_created",
    "test_discovered_spec_gets_a_scoped_slot",
    "test_index_derived_strings_are_redacted_on_egress",
    "test_safe_dir_still_accepts_absolute_and_tilde",
}

#: Process-control timing previously listed ``test_cancelled_git_is_killed_...``
#: here. Its root cause was not Windows at all: the sandbox-preparation thread
#: hop sat inside the window its wait bounds, so it timed out on any heavily
#: parallel shard (it went red on Linux 3.10 too). The hop is stubbed in the test
#: now, so the entry is gone rather than carried as a reason that no longer
#: applies. If Windows reds on it again it is a genuinely different failure and
#: belongs back here with that reason stated.

_WINDOWS_GAPS = _POSIX_SENTINEL_PINNING | _POSIX_PATH_SHAPE


def pytest_collection_modifyitems(config, items):
    """Skip the tracked Windows gaps (all parametrizations of each name)."""
    if not platform_compat.IS_WINDOWS:
        return
    marker = pytest.mark.skip(
        reason="known Windows gap -- tracked in this suite's conftest "
        "(POSIX dir_fd pinning / path shape / process timing)"
    )
    for item in items:
        if item.originalname in _WINDOWS_GAPS or item.name.split("[")[0] in _WINDOWS_GAPS:
            item.add_marker(marker)

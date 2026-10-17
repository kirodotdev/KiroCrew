"""A completed check run that failed to start must bucket as failed, not pending.

GitHub's CheckConclusionState includes STARTUP_FAILURE. Before the fix it matched
none of the known sets in ``_github_check`` and fell through to ``pending``, so a
finished check read as still running and the CI glyph never turned red.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.source_providers.github import _github_check


def test_completed_startup_failure_is_failed() -> None:
    row = _github_check({"name": "x", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"})
    assert row["bucket"] == "failed"


def test_lowercase_startup_failure_is_normalized_to_failed() -> None:
    row = _github_check({"name": "x", "status": "completed", "conclusion": "startup_failure"})
    assert row["bucket"] == "failed"


@pytest.mark.parametrize(
    ("status", "conclusion", "bucket"),
    [
        ("IN_PROGRESS", "STARTUP_FAILURE", "pending"),
        ("COMPLETED", "FAILURE", "failed"),
        ("COMPLETED", "SUCCESS", "passed"),
        ("COMPLETED", "SOMETHING_NEW", "pending"),
    ],
)
def test_neighbouring_buckets_are_unchanged(status: str, conclusion: str, bucket: str) -> None:
    row = _github_check({"name": "x", "status": status, "conclusion": conclusion})
    assert row["bucket"] == bucket

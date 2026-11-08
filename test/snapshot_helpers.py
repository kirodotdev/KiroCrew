"""Helpers shared by ``test_snapshot.py`` and the suites split out of it.

``test_snapshot.py`` re-exports ``unpinnable_argv`` for the other ``test_snapshot_*.py``
modules. ``_no_gateway`` is an autouse fixture each module binds by name.
"""

from __future__ import annotations

import pytest


def unpinnable_argv() -> list[str]:
    """``--allow-unpinned-staging``, but ONLY where the platform cannot pin a tree walk.

    Without directory descriptors ``_staging_is_pinned`` refuses rather than fall back to a
    by-name walk, so a test must opt in as an operator would. Passing it everywhere would
    move Linux off the pinned path that ships. A test whose SUBJECT is the pinned guarantee
    skips on such a platform instead.
    """
    from kiro_crew import pinned_fs

    return [] if pinned_fs.supports_pinned_tree_walk() else ["--allow-unpinned-staging"]


@pytest.fixture(autouse=True)
def _no_gateway(_floor_monkeypatch):
    """Prevent gateway-running check from blocking restore in tests.

    Uses the deterministic env seam (not a function patch) so refusal tests can
    override it with ``=1`` and the result never depends on a real socket probe.
    """
    _floor_monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")

"""Helpers shared by ``test_snapshot.py`` and the suites split out of it.

``unpinnable_argv`` is the ``--allow-unpinned-staging`` flag a snapshot or restore call
needs only where the platform cannot pin a tree walk; ``test_snapshot.py`` re-exports it
for the other ``test_snapshot_*.py`` modules that import it from there. ``_no_gateway`` is
the autouse fixture that pins the gateway-running seam off; every module whose tests need
it binds it by name.
"""

from __future__ import annotations

import pytest


def unpinnable_argv() -> list[str]:
    """``--allow-unpinned-staging``, but ONLY where the platform cannot pin a tree walk.

    ``_staging_is_pinned`` refuses rather than falling back when there are no directory
    descriptors, which is deliberate: a by-name walk is the mechanism whose failure closed
    two pull requests, so the weaker mode is never something the tool picks on the
    operator's behalf. A test that drives snapshot or restore therefore has to say the same
    thing an operator on such a platform has to say, or it dies at the refusal instead of
    reaching its own subject -- which is what left the whole snapshot suite red on Windows.

    Returned CONDITIONALLY, never unconditionally. Passing the flag everywhere would move
    Linux onto the by-name traversal too and quietly delete this suite's coverage of the
    pinned path, which is the path that actually ships. Where pinning works this is empty
    and nothing changes.

    Not for a test whose SUBJECT is the pinned guarantee itself (an ancestor swap being
    refused, a nested symlink not being copied). That guarantee does not exist on a
    platform without descriptors, so such a test skips there rather than asserting a
    promise the platform cannot keep.
    """
    from kiro_crew import pinned_fs

    return [] if pinned_fs.supports_pinned_tree_walk() else ["--allow-unpinned-staging"]


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse gateway seam of the snapshot suites moved verbatim; no test in these modules calls monkeypatch.undo()
def _no_gateway(monkeypatch):
    """Prevent gateway-running check from blocking restore in tests.

    Uses the deterministic env seam (not a function patch) so refusal tests can
    override it with ``=1`` and the result never depends on a real socket probe.
    """
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")

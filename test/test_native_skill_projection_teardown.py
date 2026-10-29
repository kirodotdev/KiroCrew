"""The skill projection suites end every projection a test prepared before its ``tmp_path`` goes.

pytest removes a passing test's ``tmp_path`` and gives the same name to the next
test. A projection left to its finalizer ends whenever the collector frees it, and
that end's prune recreates the agents directory it was given, which is then the
next test's directory, mid-setup. ``native_skill_projection_helpers`` ends them at
teardown instead; these tests pin that its helper does.
"""

from __future__ import annotations

import shutil

import native_skill_projection_helpers
from native_skill_projection_helpers import (
    _active_projections_now,
    _end_projections_started_since,
)

from kiro_crew.acp import skill_projection as projection

native_tree = native_skill_projection_helpers.native_tree


def test_a_projection_still_held_at_teardown_cannot_prune_into_a_removed_tree(
    native_tree, tmp_path
):
    """The late end is run by hand, where the collector would run it, so the
    race is reproduced without waiting for a collection."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    before = _active_projections_now()
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    late_end = prepared._lease_finalizer
    _end_projections_started_since(before)
    shutil.rmtree(tmp_path / "kiro")
    shutil.rmtree(tmp_path / "crew", ignore_errors=True)
    if late_end is not None and late_end.alive:
        late_end()
    assert not agents.exists(), "a projection ended after its tree was removed"


def test_a_projection_registered_before_the_test_is_left_alone(native_tree):
    """Only what this test started is ended: an earlier test's projection, whose
    tree is already gone, is never pruned into again."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    earlier = projection.prepare_native_skill_projection(project)
    assert earlier is not None
    before = _active_projections_now()
    mine = projection.prepare_native_skill_projection(project)
    assert mine is not None
    _end_projections_started_since(before)
    assert mine._lease_finalizer is None
    assert earlier._lease_finalizer is not None and earlier._lease_finalizer.alive
    earlier.release()

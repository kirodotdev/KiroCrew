"""The pure counting logic of the session-tree OTel fold.

Exercises :func:`depth_observation` and :func:`count_roots_with_children` as pure
functions over plain inputs -- no store, no bus, no OpenTelemetry. The
step/render/registration they feed lives in the OTel export skeleton; here we prove
the two measurements are right in isolation.
"""

from __future__ import annotations

from kiro_crew.crew_log.otel_session_tree_fold import (
    count_roots_with_children,
    depth_observation,
)
from kiro_crew.crew_log.session_tree import TreeNode


def _node(slot: str, parent_slot: str | None = None, cycle: bool = False) -> TreeNode:
    return TreeNode(slot=slot, parent_slot=parent_slot, cycle=cycle)


def _tree(*nodes: TreeNode) -> dict[str, TreeNode]:
    return {n.slot: n for n in nodes}


# --- depth_observation -----------------------------------------------------


def test_depth_present_is_observed():
    assert depth_observation({"depth": 3}) == 3


def test_depth_zero_is_observed_not_skipped():
    # A root that stated its depth is a real low-end observation.
    assert depth_observation({"depth": 0}) == 0


def test_missing_depth_is_skipped():
    # An old entry (no depth key) contributes nothing, never a zero.
    assert depth_observation({"agent": "kirocrew", "slot": "chat-7"}) is None


def test_a_negative_depth_is_skipped():
    assert depth_observation({"depth": -1}) is None


def test_a_bool_depth_is_skipped():
    assert depth_observation({"depth": True}) is None
    assert depth_observation({"depth": False}) is None


def test_a_non_int_depth_is_skipped():
    assert depth_observation({"depth": "2"}) is None
    assert depth_observation({"depth": 2.0}) is None


def test_none_data_is_skipped():
    assert depth_observation(None) is None


def test_non_mapping_data_is_skipped():
    assert depth_observation([1, 2, 3]) is None  # type: ignore[arg-type]


# --- count_roots_with_children ---------------------------------------------


def test_empty_tree_has_no_roots_with_children():
    assert count_roots_with_children({}) == 0
    assert count_roots_with_children(None) == 0


def test_a_lone_root_has_no_children():
    assert count_roots_with_children(_tree(_node("a"))) == 0


def test_a_root_with_one_child_is_counted_once():
    tree = _tree(_node("a"), _node("b", parent_slot="a"))
    assert count_roots_with_children(tree) == 1


def test_a_root_with_two_children_is_still_counted_once():
    # The count is of ROOTS with children, not of children.
    tree = _tree(_node("a"), _node("b", parent_slot="a"), _node("c", parent_slot="a"))
    assert count_roots_with_children(tree) == 1


def test_two_roots_each_with_children_count_two():
    tree = _tree(
        _node("a"),
        _node("b", parent_slot="a"),
        _node("x"),
        _node("y", parent_slot="x"),
    )
    assert count_roots_with_children(tree) == 2


def test_a_deep_chain_counts_only_the_root():
    # a <- b <- c <- d. Only `a` is a root; b, c have children but are not roots.
    tree = _tree(
        _node("a"),
        _node("b", parent_slot="a"),
        _node("c", parent_slot="b"),
        _node("d", parent_slot="c"),
    )
    assert count_roots_with_children(tree) == 1


def test_a_grandchild_still_counts_its_root_through_the_middle_node():
    # a (root) <- b <- c. `a` has a child (b), so it is counted; the grandchild c
    # confirms b branched but makes no new root.
    tree = _tree(_node("a"), _node("b", parent_slot="a"), _node("c", parent_slot="b"))
    assert count_roots_with_children(tree) == 1


def test_a_citation_with_no_node_makes_no_root():
    # `b` cites a parent `ghost` that has no node of its own (a citation the fold
    # could not place). `ghost` is not in the tree, so it is not a root here.
    tree = _tree(_node("b", parent_slot="ghost"))
    assert count_roots_with_children(tree) == 0


def test_a_root_on_a_cycle_is_not_a_root():
    # A cycle-marked node is never a root, even with parent_slot None.
    tree = _tree(_node("a", cycle=True), _node("b", parent_slot="a"))
    assert count_roots_with_children(tree) == 0


def test_a_child_on_a_cycle_does_not_prove_its_root_has_children():
    # `b` is on a cycle; its citation of `a` does not count `a` as a branched root.
    tree = _tree(_node("a"), _node("b", parent_slot="a", cycle=True))
    assert count_roots_with_children(tree) == 0


def test_a_root_counts_when_a_placed_child_exists_even_beside_a_cycle_child():
    # `a` has one placed child (b) and one cycle child (c). The placed child is
    # enough to count `a` once.
    tree = _tree(
        _node("a"),
        _node("b", parent_slot="a"),
        _node("c", parent_slot="a", cycle=True),
    )
    assert count_roots_with_children(tree) == 1

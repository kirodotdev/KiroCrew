"""Unit tests for kiro_crew.session_tree (the nested-agent orchestration tree)."""

from __future__ import annotations

from kiro_crew.session_tree import SessionNode, SessionTree


def test_add_autocreates_root_and_sets_depth() -> None:
    t = SessionTree()
    # Spawning a top-level subagent names a not-yet-registered root parent.
    child = t.add("subagent:a1", parent_key="dashboard:main")
    root = t.get("dashboard:main")
    assert root is not None and root.is_root and root.depth == 0
    assert child.depth == 1 and child.parent_key == "dashboard:main"
    assert "subagent:a1" in t.get("dashboard:main").children


def test_add_is_idempotent() -> None:
    t = SessionTree()
    n1 = t.add("subagent:a1", "dashboard:main")
    n2 = t.add("subagent:a1", "dashboard:main")
    assert n1 is n2
    assert len(t) == 2  # root + one subagent, no duplicate


def test_nested_depth_and_descendants() -> None:
    t = SessionTree()
    t.add("subagent:a1", "dashboard:main")   # depth 1
    t.add("subagent:b1", "subagent:a1")      # depth 2
    t.add("subagent:c1", "subagent:b1")      # depth 3
    t.add("subagent:b2", "subagent:a1")      # depth 2 (sibling)
    assert t.depth_of("subagent:c1") == 3
    assert t.depth_of("subagent:b2") == 2
    assert set(t.descendants("subagent:a1")) == {"subagent:b1", "subagent:c1", "subagent:b2"}
    # subtree size includes self by default
    assert t.subtree_size("subagent:a1") == 4
    assert t.subtree_size("subagent:a1", include_self=False) == 3


def test_root_of_walks_to_top() -> None:
    t = SessionTree()
    t.add("subagent:a1", "dashboard:main")
    t.add("subagent:b1", "subagent:a1")
    assert t.root_of("subagent:b1") == "dashboard:main"
    assert t.root_of("subagent:a1") == "dashboard:main"
    assert t.root_of("dashboard:main") == "dashboard:main"
    assert t.root_of("subagent:unknown") is None


def test_prune_subtree_removes_branch_and_detaches() -> None:
    t = SessionTree()
    t.add("subagent:a1", "dashboard:main")
    t.add("subagent:b1", "subagent:a1")
    t.add("subagent:c1", "subagent:b1")
    removed = t.prune_subtree("subagent:b1")
    assert set(removed) == {"subagent:b1", "subagent:c1"}
    assert "subagent:b1" not in t and "subagent:c1" not in t
    # a1 keeps existing and no longer lists b1 as a child
    assert "subagent:b1" not in t.get("subagent:a1").children
    assert t.subtree_size("subagent:a1", include_self=False) == 0


def test_prune_unknown_is_noop() -> None:
    t = SessionTree()
    assert t.prune_subtree("subagent:nope") == []


def test_aggregate_joins_runtime_cost_over_subtree() -> None:
    t = SessionTree()
    t.add("subagent:a1", "dashboard:main")
    t.add("subagent:b1", "subagent:a1")
    t.add("subagent:b2", "subagent:a1")
    # Simulated per-agent RSS (GB) keyed by session_key; root + unknowns contribute 0.
    rss = {"subagent:a1": 0.4, "subagent:b1": 0.5, "subagent:b2": 0.6}
    # Whole tree from the root: only the three subagents have cost.
    assert t.aggregate("dashboard:main", lambda k: rss.get(k)) == 1.5
    # Subtree from a1 excluding itself.
    assert t.aggregate("subagent:a1", lambda k: rss.get(k), include_self=False) == 1.1


def test_aggregate_tolerates_bad_values() -> None:
    t = SessionTree()
    t.add("subagent:a1", "dashboard:main")
    t.add("subagent:b1", "subagent:a1")

    def value_fn(k: str):
        if k == "subagent:a1":
            return "not-a-number"
        if k == "subagent:b1":
            raise RuntimeError("transient")
        return None

    # Bad string, raised exception, and None all contribute 0 -> no crash.
    assert t.aggregate("dashboard:main", value_fn) == 0.0


def test_descendants_cycle_safe() -> None:
    t = SessionTree()
    t.add("subagent:a1", "dashboard:main")
    t.add("subagent:b1", "subagent:a1")
    # Force a malformed cycle a1 -> b1 -> a1 and confirm no infinite loop.
    t.get("subagent:b1").children.add("subagent:a1")
    desc = t.descendants("subagent:a1")
    assert set(desc) == {"subagent:b1", "subagent:a1"}
    assert t.root_of("subagent:b1") in {"dashboard:main", "subagent:a1", "subagent:b1"}


def test_session_node_defaults() -> None:
    n = SessionNode(key="subagent:x", parent_key=None, depth=0, is_root=True)
    assert n.children == set()


def test_remove_and_reparent_preserves_live_children() -> None:
    """Completing a parent reparents its children to the grandparent.

    When a parent subagent finishes before its children, the children must
    stay in the tree (counted toward the per-tree node cap). prune_subtree
    would delete them, making the cap under-count live agents.
    """
    t = SessionTree()
    t.add("subagent:parent", "dashboard:main")
    t.add("subagent:child1", "subagent:parent")
    t.add("subagent:child2", "subagent:parent")
    t.add("subagent:grandchild", "subagent:child1")

    assert t.subtree_size("dashboard:main", include_self=False) == 4

    # Parent completes while children are still live.
    reparented = t.remove_and_reparent("subagent:parent")
    assert set(reparented) == {"subagent:child1", "subagent:child2"}

    # Parent is gone.
    assert "subagent:parent" not in t

    # Children reparented under the grandparent (dashboard root).
    assert t.get("subagent:child1").parent_key == "dashboard:main"
    assert t.get("subagent:child2").parent_key == "dashboard:main"
    assert "subagent:child1" in t.get("dashboard:main").children
    assert "subagent:child2" in t.get("dashboard:main").children

    # Grandchild still reachable from its parent.
    assert t.get("subagent:grandchild").parent_key == "subagent:child1"

    # Subtree size decremented by 1 (only the finished parent), not 4.
    assert t.subtree_size("dashboard:main", include_self=False) == 3


def test_remove_and_reparent_unknown_key_is_noop() -> None:
    t = SessionTree()
    assert t.remove_and_reparent("subagent:nope") == []

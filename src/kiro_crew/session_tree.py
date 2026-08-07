"""Orchestration session tree — topology of the nested-agent forest.

Kiro Crew owns the logical tree of agent sessions; each node still executes as a
FLAT kiro-cli session (its own process, or a session on a shared runtime). This
module is the pure-topology index the ``SubagentManager`` joins against its
per-agent runtime state (``SubagentInfo``): parent/child links, depth, root, and
subtree operations.

Model
-----
- Nodes are keyed by Kiro Crew ``session_key``: ``"subagent:<id>"`` for a
  subagent, and the originating ``"dashboard:<slot>"`` / ``"slack:<ch>:<ts>"`` /
  ``"cron:<id>"`` (etc.) for a **root**. The tree spans ALL session_keys, not
  just subagents — a root is the user/cron/dashboard session that is not itself
  a subagent.
- **Depth**: a root is depth ``0``; a top-level subagent (child of a root) is
  depth ``1``; its child is depth ``2``; and so on. This matches
  ``SubagentInfo.depth`` produced by the depth guard (a top-level subagent is
  depth 1).
- **Pure structure**: runtime state (RSS/CPU cost, status, sandbox) lives on
  ``SubagentInfo``. :meth:`SessionTree.aggregate` takes a ``value_fn`` keyed by
  session_key so the tree never duplicates or couples to that mutable state.

Concurrency: not independently locked. The ``SubagentManager`` mutates it from
the single asyncio thread, the same discipline that already guards ``_agents``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["SessionNode", "SessionTree"]


@dataclass
class SessionNode:
    """One node in the orchestration forest, keyed by session_key."""

    key: str
    parent_key: str | None
    depth: int
    is_root: bool = False
    children: set[str] = field(default_factory=set)


class SessionTree:
    """A forest of :class:`SessionNode` keyed by session_key.

    Roots (user/cron/dashboard sessions) are auto-created the first time a
    subagent names them as a parent, so callers only ever :meth:`add` the child
    they are spawning.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, SessionNode] = {}

    # ---- introspection -------------------------------------------------

    def __contains__(self, key: str) -> bool:
        return key in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def get(self, key: str) -> SessionNode | None:
        return self._nodes.get(key)

    def depth_of(self, key: str) -> int:
        node = self._nodes.get(key)
        return node.depth if node is not None else 0

    # ---- mutation ------------------------------------------------------

    def add(self, key: str, parent_key: str | None = None) -> SessionNode:
        """Register ``key`` under ``parent_key`` and return its node.

        Idempotent: re-adding an existing key is a no-op that returns the
        existing node (spawn may retry). When ``parent_key`` is given but not
        yet registered, it is auto-created as a root (depth 0) — that is the
        originating user/cron/dashboard session, which is never spawned through
        this tree itself.
        """
        existing = self._nodes.get(key)
        if existing is not None:
            return existing

        if parent_key is None:
            node = SessionNode(key=key, parent_key=None, depth=0, is_root=True)
            self._nodes[key] = node
            return node

        parent = self._nodes.get(parent_key)
        if parent is None:
            parent = SessionNode(key=parent_key, parent_key=None, depth=0, is_root=True)
            self._nodes[parent_key] = parent
        node = SessionNode(key=key, parent_key=parent_key, depth=parent.depth + 1, is_root=False)
        parent.children.add(key)
        self._nodes[key] = node
        return node

    def remove_and_reparent(self, key: str) -> list[str]:
        """Remove *only* ``key``, reparenting its children to its parent.

        Returns the list of reparented child keys (empty if the node has no
        children or doesn't exist). Live children stay in the tree — the
        per-tree node cap remains accurate even when a parent subagent
        finishes before its children.
        """
        node = self._nodes.get(key)
        if node is None:
            return []
        grandparent_key = node.parent_key
        reparented: list[str] = []
        for child_key in list(node.children):
            child = self._nodes.get(child_key)
            if child is not None:
                child.parent_key = grandparent_key
                # Depth stays unchanged: it's observability-only and a parent
                # finishing doesn't change where the child sits logically.
                reparented.append(child_key)
                if grandparent_key and grandparent_key in self._nodes:
                    self._nodes[grandparent_key].children.add(child_key)
        # Detach key from its parent's child set
        if grandparent_key and grandparent_key in self._nodes:
            self._nodes[grandparent_key].children.discard(key)
        self._nodes.pop(key, None)
        return reparented

    def prune_subtree(self, key: str) -> list[str]:
        """Remove ``key`` and every descendant. Returns the removed keys.

        Detaches ``key`` from its parent's child set. Use ONLY where the node
        provably has no live descendants — a rejected spawn that never started,
        or process shutdown. Every per-agent teardown path must use
        :meth:`remove_and_reparent` instead: neither ``cancel`` nor the reaper
        cascades, so a finishing or reaped node can still have live children,
        and dropping them from the tree silently frees their budget under the
        per-tree node cap.
        """
        node = self._nodes.get(key)
        if node is None:
            return []
        removed = [key, *self.descendants(key)]
        if node.parent_key and node.parent_key in self._nodes:
            self._nodes[node.parent_key].children.discard(key)
        for k in removed:
            self._nodes.pop(k, None)
        return removed

    # ---- queries -------------------------------------------------------

    def descendants(self, key: str) -> list[str]:
        """All keys strictly below ``key`` (cycle-safe, iterative)."""
        node = self._nodes.get(key)
        if node is None:
            return []
        out: list[str] = []
        seen: set[str] = set()
        stack = list(node.children)
        while stack:
            k = stack.pop()
            if k in seen or k not in self._nodes:
                continue
            seen.add(k)
            out.append(k)
            stack.extend(self._nodes[k].children)
        return out

    def subtree_size(self, key: str, *, include_self: bool = True) -> int:
        """Number of nodes in the subtree rooted at ``key`` (0 if unknown)."""
        if key not in self._nodes:
            return 0
        n = len(self.descendants(key))
        return n + 1 if include_self else n

    def root_of(self, key: str) -> str | None:
        """Walk parent links to the root of ``key``'s tree (cycle-safe)."""
        node = self._nodes.get(key)
        if node is None:
            return None
        seen: set[str] = set()
        while node.parent_key is not None and node.parent_key in self._nodes:
            if node.key in seen:  # defensive: never loop on a malformed cycle
                break
            seen.add(node.key)
            node = self._nodes[node.parent_key]
        return node.key

    def aggregate(
        self, key: str, value_fn: Callable[[str], float | None], *, include_self: bool = True
    ) -> float:
        """Sum ``value_fn(session_key)`` over the subtree rooted at ``key``.

        The join to runtime state (e.g. per-agent RSS on ``SubagentInfo``) lives
        in ``value_fn``; keys with no value / a non-numeric value contribute 0.
        """
        keys = self.descendants(key)
        if include_self and key in self._nodes:
            keys = [key, *keys]
        total = 0.0
        for k in keys:
            try:
                v = value_fn(k)
            except Exception:
                continue
            if v is None:
                continue
            try:
                total += float(v)
            except (TypeError, ValueError):
                continue
        return total

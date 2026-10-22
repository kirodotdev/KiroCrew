"""The agent's own context-bar items, merged with the workspace's.

Slack keeps at most five context-bar items per channel and ``setProperties``
replaces the whole list, so there is ONE publisher -- the workspace service --
and the agent's items ride along with it: the workspace's repo / branch / PR / CI
items first (they are the channel's ground truth), then the agent's, keys unique,
five at most. :func:`merge` is that rule; the workspace calls
:func:`merge_for_channel` on every publish.

The agent may not use the workspace's keys (:data:`RESERVED_KEYS`), so it can
never relabel the branch or point the PR item somewhere else.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The keys the workspace publishes; the agent's items may not use them.
RESERVED_KEYS = frozenset({"repo", "folder", "branch", "pr", "ci"})
MAX_ITEMS = 5


def merge(
    base: Iterable[dict[str, str]], extra: Iterable[dict[str, str]]
) -> tuple[list[dict[str, str]], list[str]]:
    """``(items, dropped_keys)``: *base* then *extra*, keys unique, at most five."""
    items: list[dict[str, str]] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for source, is_extra in ((base, False), (extra, True)):
        for item in source:
            key = str(item.get("key") or "")
            if not key or key in seen or (is_extra and key in RESERVED_KEYS):
                if is_extra and key:
                    dropped.append(key)
                continue
            if len(items) >= MAX_ITEMS:
                if is_extra:
                    dropped.append(key)
                continue
            seen.add(key)
            items.append(dict(item))
    return items, dropped


def agent_items(channel_id: str) -> tuple[dict[str, str], ...]:
    """The agent's stored items for *channel_id* (empty when none)."""
    from kiro_crew.slack.code_channels.surface_store import store

    record = store().get(channel_id)
    return record.context_bar if record is not None else ()


def merge_for_channel(base: list[dict[str, str]], channel_id: str) -> list[dict[str, str]]:
    """The workspace's *base* items plus the agent's, as the bar is published."""
    try:
        extra = agent_items(channel_id)
    except Exception:
        # The agent's items are decoration; the workspace bar publishes regardless.
        return base
    return merge(base, extra)[0]

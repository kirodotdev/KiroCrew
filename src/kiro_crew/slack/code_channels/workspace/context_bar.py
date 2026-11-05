"""The channel's context bar: repo, branch, PR, CI (or the folder, without git).

Slack keeps at most five items, keyed uniquely; ``context_bar_items`` is a FULL
replacement on every ``setProperties``, so the whole list is rebuilt and resent
each time. Keys follow Slack's documented conventions (``repo``, ``branch``,
``pr``, ``ci``); icons come from its fixed set. Every item here is ``info``: a
link item carries a ``url``, and an ``action`` item would have to carry none.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from kiro_crew.platform.context import redact_via_context as redact
from kiro_crew.slack.code_channels.workspace.pr import PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef

MAX_ITEMS = 5
_MAX_KEY = 64
_MAX_LABEL = 128
_MAX_URL = 2048

#: Slack's icon vocabulary for context-bar items.
ICONS = frozenset(
    {
        "branch",
        "folder",
        "hierarchy",
        "life-ring",
        "link",
        "globe",
        "terminal",
        "code",
        "search",
        "lock",
    }
)

_PR_LABEL = {
    "open": "PR #{n} is open",
    "draft": "Draft PR #{n}",
    "merged": "PR #{n} merged",
    "closed": "PR #{n} closed",
}
_CI_LABEL = {"passed": "Tests passing", "failed": "Tests failing", "running": "Tests running"}


@dataclass(frozen=True)
class BarState:
    """What the bar shows. Every field is optional; absent fields add no item."""

    #: Folder name for a project that is not a git repository.
    folder: str = ""
    repo: RepoRef | None = None
    #: Repository directory name when the remote is not a recognised forge.
    repo_name: str = ""
    branch: str = ""
    #: Whether ``branch`` exists on the remote, so its web link resolves.
    branch_pushed: bool = False
    pr: PullRequest | None = None


def _item(key: str, label: str, icon: str, url: str = "") -> dict[str, str]:
    # Labels and links are repository- and forge-derived text bound for Slack:
    # the gateway's outbound redaction runs on them like on any other post.
    label = redact(label)
    item = {"key": key[:_MAX_KEY], "label": label[:_MAX_LABEL] or key, "icon": icon}
    if url and len(url) <= _MAX_URL and url.startswith("https://") and redact(url) == url:
        item["url"] = url
    return item


def build_items(state: BarState) -> list[dict[str, str]]:
    """The context-bar items for ``state``: at most five, keys unique, valid icons."""
    items: list[dict[str, str]] = []
    if state.repo is not None:
        items.append(_item("repo", state.repo.slug, "folder", state.repo.web_url))
    elif state.repo_name:
        items.append(_item("repo", state.repo_name, "folder"))
    elif state.folder:
        items.append(
            _item("folder", os.path.basename(state.folder.rstrip("/\\")) or state.folder, "folder")
        )
    if state.branch:
        url = state.repo.tree_url(state.branch) if state.repo and state.branch_pushed else ""
        items.append(_item("branch", state.branch, "branch", url))
    pr = state.pr
    if pr is not None and pr.state in _PR_LABEL:
        items.append(_item("pr", _PR_LABEL[pr.state].format(n=pr.number), "hierarchy", pr.url))
        # CI belongs to a live PR; a merged or closed one's last run is history.
        if pr.ci in _CI_LABEL and pr.state in {"open", "draft"}:
            items.append(_item("ci", _CI_LABEL[pr.ci], "terminal", f"{pr.url}/checks"))
    seen: set[str] = set()
    unique = []
    for item in items:
        if item["key"] not in seen and item["icon"] in ICONS:
            seen.add(item["key"])
            unique.append(item)
    return unique[:MAX_ITEMS]

"""The channel's context bar: repo, branch, PR, the channel actions, the workers.

Slack keeps at most five items, keyed uniquely; ``context_bar_items`` is a FULL
replacement on every ``setProperties``, so the whole list is rebuilt and resent
each time. Keys follow Slack's documented conventions (``repo``, ``branch``,
``pr``); icons come from its fixed set; a key is at most 64 characters, a label
128 and a url 2048 (`Code channel properties`_).

An item is either a LINK (``url``) or an ACTION (``item_type: "action"``, no
url): Slack saves an item carrying both as plain info and warns, so
:func:`_action` never sets a url. A click on an action reaches the app as a
``code_channel_action`` event (:mod:`..actions`).

**The budget.** Slack keeps the FIRST five items of a longer list and only warns
(``too_many_context_bar_items``, `agents.conversations.setProperties`_), so the
bar is budgeted here instead of losing whatever happened to come sixth. In
priority order, each at most once:

1. ``workers`` -- a conductor-led channel's board in one line
   (:func:`..board.model.bar_label`: ``2 working · 1 needs you``,
   ``3 of 3 done``), linked to the pinned board message
2. ``repo`` (the folder, without git)
3. ``branch``
4. ``stop`` -- the Stop action, during the channel's own session's turn (it is
   that session the Stop stops, as Slack's own Stop button does; decided when the
   bar is sent, so a refresh racing a turn start or end never sends a stale one)
5. ONE pull-request slot: the channel's own PR with its CI (``PR #42 is open ·
   tests passing``) -- once it has merged or closed, only while no worker PR is
   still open; else a single worker PR the same way; else, with several
   worker PRs, ``prs`` (``3 PRs open``) linked to the board; else, with no PR at
   all and commits on the channel's branch, the ``create-pr`` action

That is five at most, so the rest only ever take a slot nothing above wanted:
the agent's own items (``code_channel_set_context_bar``), then ``dashboard``
(Open in dashboard, a plain link, never a sign-in token). Without a board the
workspace's own items take at most four slots, so it fits unless the agent's
items fill the fifth; a conductor channel's board links the conductor's session
itself. Every per-worker PR is on the board, not here.

CI shares the PR's item because five slots cannot hold repo, branch, PR, CI and
the actions the channel needs; the PR link still leads to its checks.

.. _agents.conversations.setProperties: https://docs.slack.dev/reference/methods/agents.conversations.setProperties

.. _Code channel properties: https://docs.slack.dev/ai/slack-code/code-channel-properties
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.platform.context import redact_via_context as redact
from kiro_crew.slack.code_channels.workspace.pr import PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot

MAX_ITEMS = 5
MAX_KEY = 64
MAX_LABEL = 128
MAX_URL = 2048

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

#: Action keys this module places; :mod:`..actions` answers their clicks.
ACTION_CREATE_PR = "create-pr"
ACTION_STOP = "stop"
DASHBOARD_KEY = "dashboard"

#: Labels for the built-in actions and the dashboard link.
CREATE_PR_LABEL = "Create PR"
STOP_LABEL = "Stop"
DASHBOARD_LABEL = "Open in dashboard"

_PR_LABEL = {
    "open": "PR #{n} is open",
    "draft": "Draft PR #{n}",
    "merged": "PR #{n} merged",
    "closed": "PR #{n} closed",
}
_CI_LABEL = {"passed": "tests passing", "failed": "tests failing", "running": "tests running"}


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
    #: The channel conductor's work board, when it has one: the Workers item.
    board: BoardSnapshot | None = None
    #: The pinned board message's permalink (``chat.getPermalink``; "" = none yet).
    board_url: str = ""
    #: Whether the channel can open a pull request (``commands.pr_capability``).
    can_open_pr: bool = False
    #: Whether the channel's branch has commits of its own, so a PR has content.
    branch_has_commits: bool = False
    #: Whether the Stop action is offered (the bound session's turn is running).
    stop: bool = False
    #: The bound session's dashboard link, without any credential ("" = none).
    dashboard_url: str = ""


def _clean_url(url: str) -> str:
    """*url* when it is an https link Slack accepts and redaction leaves intact."""
    if url and len(url) <= MAX_URL and url.startswith("https://") and redact(url) == url:
        return url
    return ""


def _item(key: str, label: str, icon: str, url: str = "") -> dict[str, str]:
    # Labels and links are repository- and forge-derived text bound for Slack:
    # the gateway's outbound redaction runs on them like on any other post.
    label = redact(label)
    item = {"key": key[:MAX_KEY], "label": label[:MAX_LABEL] or key[:MAX_KEY], "icon": icon}
    clean = _clean_url(url)
    if clean:
        item["url"] = clean
    return item


def _action(key: str, label: str, icon: str) -> dict[str, str]:
    """An action item: never a url, or Slack saves it as plain info."""
    return {
        "key": key[:MAX_KEY],
        "label": redact(label)[:MAX_LABEL] or key[:MAX_KEY],
        "icon": icon,
        "item_type": "action",
    }


def pr_label(pr: PullRequest) -> str:
    """``PR #42 is open · tests passing``: the PR's state with a live PR's CI."""
    label = _PR_LABEL[pr.state].format(n=pr.number)
    # CI belongs to a live PR; a merged or closed one's last run is history.
    if pr.ci in _CI_LABEL and pr.state in {"open", "draft"}:
        label += f" · {_CI_LABEL[pr.ci]}"
    return label


#: Keys of the Workers item and the several-worker-PRs item.
WORKERS_KEY = "workers"
PRS_KEY = "prs"


def _worker_prs(board: BoardSnapshot | None) -> list[PullRequest]:
    """The worker PRs the board LISTS (its shown rows, the ones whose PRs are
    looked up), in board order; a number reported but unconfirmed included."""
    if board is None:
        return []
    return [
        PullRequest(r.pr_number, r.pr_url, r.pr_state, r.pr_ci)
        for r in board.shown
        if r.pr_number > 0
    ]


def prs_label(prs: list[PullRequest]) -> str:
    """``3 PRs open`` / ``3 PRs: 2 open · 1 merged``: several worker PRs in one label."""
    counts = {"open": 0, "merged": 0, "closed": 0}
    for pr in prs:
        state = "open" if pr.state == "draft" else pr.state
        if state in counts:
            counts[state] += 1
    named = [f"{n} {word}" for word, n in counts.items() if n]
    if len(named) == 1 and sum(counts.values()) == len(prs):
        word = next(w for w, n in counts.items() if n)
        return f"{len(prs)} PRs {word}"
    return f"{len(prs)} PRs" + (": " + " · ".join(named) if named else "")


def _pr_item(pr: PullRequest) -> dict[str, str]:
    label = pr_label(pr) if pr.state in _PR_LABEL else f"PR #{pr.number}"
    return _item("pr", label, "hierarchy", pr.url)


def _pr_slot(state: BarState) -> dict[str, str] | None:
    """The bar's ONE pull-request item (see the module's budget), or None."""
    pr = state.pr
    workers = _worker_prs(state.board)
    live = any(w.state in {"open", "draft"} for w in workers)
    # The channel's own PR, unless it has landed while workers' PRs are still open.
    if pr is not None and pr.state in _PR_LABEL and (pr.state in {"open", "draft"} or not live):
        return _pr_item(pr)
    if len(workers) == 1:
        return _pr_item(workers[0])
    if workers:
        return _item(PRS_KEY, prs_label(workers), "hierarchy", state.board_url)
    if state.can_open_pr and state.branch and state.branch_has_commits:
        return _action(ACTION_CREATE_PR, CREATE_PR_LABEL, "hierarchy")
    return None


def build_items(state: BarState) -> list[dict[str, str]]:
    """The context-bar items for ``state``: at most five, keys unique, valid icons.

    In the module's priority order; :func:`with_dashboard` adds the dashboard
    link afterwards, only into a slot left free.
    """
    from kiro_crew.slack.code_channels.board.model import bar_label

    items: list[dict[str, str]] = []
    if state.board is not None and state.board.rows:
        items.append(_item(WORKERS_KEY, bar_label(state.board), "hierarchy", state.board_url))
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
    if state.stop:
        items.append(_action(ACTION_STOP, STOP_LABEL, "terminal"))
    slot = _pr_slot(state)
    if slot is not None:
        items.append(slot)
    return with_dashboard(_unique(items), state.dashboard_url)


def with_dashboard(items: list[dict[str, str]], url: str) -> list[dict[str, str]]:
    """*items* plus Open in dashboard in a free slot, when *url* is a usable link.

    Last, after the agent's own items too (the workspace merges those in between),
    so it only ever takes a slot nothing else wanted.
    """
    items = list(items)[:MAX_ITEMS]
    if _clean_url(url) and len(items) < MAX_ITEMS:
        items = _unique(items + [_item(DASHBOARD_KEY, DASHBOARD_LABEL, "link", url)])
    return items


def _unique(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique = []
    for item in items:
        if item["key"] not in seen and item["icon"] in ICONS:
            seen.add(item["key"])
            unique.append(item)
    return unique


def conforms(item: dict[str, str]) -> bool:
    """Whether one item is within Slack's documented schema (tests, the client)."""
    key, label, icon = item.get("key", ""), item.get("label", ""), item.get("icon", "")
    url, kind = item.get("url", ""), item.get("item_type", "info")
    allowed = {"key", "label", "icon", "url", "item_type"}
    return (
        set(item) <= allowed
        and 0 < len(key) <= MAX_KEY
        and 0 < len(label) <= MAX_LABEL
        and (not icon or icon in ICONS)
        and kind in {"info", "action"}
        and not (kind == "action" and url)
        and (not url or (len(url) <= MAX_URL and url.startswith("https://")))
    )


#: ``agent_resource`` limits (code channel properties).
MAX_RESOURCE_TITLE = 255
_RESOURCE_STATE = {"draft": "draft", "merged": "merged", "closed": "closed"}


def pr_resource(number: int, url: str, title: str, state: str) -> dict[str, str] | None:
    """The channel's ``agent_resource`` for its pull request, or None without one.

    ``title`` reads ``Fix the flaky login test (#42)``, with ``, merged`` /
    ``, closed`` / ``, draft`` inside the parentheses once the PR is no longer
    simply open, so the property follows the PR as it is renamed and lands.
    Redacted like every other forge-derived text bound for Slack, and cut to
    Slack's 255 characters.
    """
    clean = _clean_url(url)
    if number <= 0 or not clean:
        return None
    suffix = (
        f" (#{number}" + (f", {_RESOURCE_STATE[state]}" if state in _RESOURCE_STATE else "") + ")"
    )
    name = redact((title or "").strip()) or f"Pull request #{number}"
    room = MAX_RESOURCE_TITLE - len(suffix)
    if len(name) > room:
        name = name[: room - 1].rstrip() + "…"
    return {
        "url": clean,
        "resource_type": "pull_request",
        "title": name + suffix,
        "provider": "github",
    }

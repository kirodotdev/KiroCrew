"""The goal conductor's task board: the data shape its drawer template reads, and its provider.

``kirocrew-conductor`` gets ``agent_panel_templates/kirocrew-conductor.html`` because the
file is named for the crew (``agent_panel.template_for_crew`` slugifies the crew name and
looks for that id).

THE TASK LIST COMES FROM THE WORK FOLD, never from the agent. The board used to be
written by hand with ``panel_publish`` each patrol turn, so a cycle that skipped the
publish left the drawer showing hours-old tasks while the ledger already held the new
ones. :func:`build_conductor_board` derives every task, the needs-you band and the
done count from the same ``work`` fold the Dashboard tab's ``goal-board`` reads, so the
board is current with no agent write at all. A published payload may still add the
two things no ledger records -- ``next`` and ``repo`` -- and ``next`` is labelled with
its own time once the ledger has moved past it (``next_from``).

``test/test_conductor_board_contract_parity.py`` asserts the template reads exactly
the leaves of :class:`ConductorBoardPanel`, in both directions, and that the
template's state and step vocabularies equal :data:`TASK_STATES` and :data:`STEPS`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final, NotRequired, TypedDict

from kiro_crew.work_vocab import WorkBoardItem, WorkBoardView

#: The crew whose drawer renders this board, and the template id it slugifies to.
BOARD_CREW_NAME: Final = "kirocrew-conductor"
BOARD_TEMPLATE_ID: Final = "kirocrew-conductor"

#: A task's ``state``. The template matches these case-insensitively and reads ``_`` /
#: ``-`` as a space, so ``needs_you`` and ``Needs you`` both land on ``needs you``. Any
#: other word still renders, in a neutral pill.
TASK_STATES: Final = ("done", "working", "testing", "waiting", "needs you", "stuck")

#: A task's ``step``: where it is on the way to landed. Matched case-insensitively.
STEPS: Final = ("Code", "Test", "PR", "CI", "Done")

#: How many tasks the board carries. The fold already bounds its own item list; this
#: keeps the derived payload under the panel store's data cap whatever that bound is.
MAX_TASKS: Final = 60

#: Longest text one board string carries, so a long summary cannot crowd the cap.
_MAX_TEXT: Final = 240

_REPO_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConductorBoardTask(TypedDict):
    """One work item on the board."""

    task: str
    state: str
    step: str
    #: A pull-request number or ``#N`` text. With the panel's ``repo`` it is a button
    #: asking the host to open the PR (``kirocrew-dashboard:open``); the sandbox has no
    #: ``allow-popups``, so a plain link would be a dead click. Without ``repo``, text.
    pr: NotRequired[str | int]


class ConductorBoardPanel(TypedDict):
    """The whole board, as the drawer's data island."""

    #: The one thing only the user can unblock; empty when nothing needs them.
    needs_you: str
    #: ``"N of M"`` -- items accepted out of the board's total.
    done: str
    #: When the board last changed, as display text.
    updated: str
    #: The conductor's next move, one line. Agent-written; empty when it wrote none.
    next: str
    #: When ``next`` was written, as display text -- set only when the ledger has moved
    #: on since, so an old line is never presented as the current one.
    next_from: NotRequired[str]
    tasks: list[ConductorBoardTask]
    #: ``owner/name`` of the code host repo the ``pr`` numbers belong to. Optional.
    repo: NotRequired[str]


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def stamp(text: Any) -> str:
    """An ISO timestamp as ``YYYY-MM-DD HH:MM UTC``, or ``""`` when it is not one."""
    when = _when(text)
    if when is None:
        return ""
    try:
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError):
        return ""


def is_newer(a: Any, b: Any) -> bool:
    """Whether ISO stamp *a* is strictly later than *b*. Unparseable reads as not newer."""
    left, right = _when(a), _when(b)
    return left is not None and right is not None and left > right


def _needs_you(item: WorkBoardItem) -> bool:
    # The goal-board's own rule: an open item whose worker asked or is stuck.
    return item.get("state") == "open" and item.get("status") in ("question", "blocked")


def _ask(item: WorkBoardItem) -> str:
    """One needs-you line: the item's title, then what its worker asked."""
    title = _text(item.get("title"), 120) or _text(item.get("item_id"))
    summary = _text(item.get("summary"), 160)
    return f"{title}: {summary}" if summary else title


def _state(item: WorkBoardItem) -> str:
    state = item.get("state")
    if state == "accepted":
        return "done"
    if state == "rejected":
        return "stuck"
    if state == "abandoned":
        return "abandoned"
    if _needs_you(item):
        return "needs you"
    status = item.get("status")
    if status == "done":
        # Claimed done, waiting on the conductor's acceptance check.
        return "testing"
    if status == "progress" or item.get("worker_session_key"):
        return "working"
    return "waiting"


def _step(item: WorkBoardItem) -> str:
    if item.get("state") == "accepted":
        return "Done"
    if isinstance(item.get("pr"), int):
        return "CI" if item.get("status") == "done" else "PR"
    return "Code"


def _task(item: WorkBoardItem) -> ConductorBoardTask:
    task: ConductorBoardTask = {
        "task": _text(item.get("title")) or _text(item.get("item_id")) or "Untitled task",
        "state": _state(item),
        "step": _step(item),
    }
    pr = item.get("pr")
    if isinstance(pr, int) and not isinstance(pr, bool):
        task["pr"] = pr
    return task


def _repo(published: Mapping[str, Any], items: list[WorkBoardItem]) -> str:
    repo = _text(published.get("repo"))
    if _REPO_RE.match(repo):
        return repo
    # A pr_checks acceptance names the repo its PR belongs to.
    for item in items:
        acceptance = item.get("acceptance")
        if isinstance(acceptance, dict):
            named = _text(acceptance.get("repo"))
            if _REPO_RE.match(named):
                return named
    return ""


def build_conductor_board(
    view: WorkBoardView,
    published: Mapping[str, Any] | None,
    published_at: str,
) -> ConductorBoardPanel:
    """The board, derived from the ``work`` fold, plus the published extras.

    *published* is the agent's last ``panel_publish`` data (or ``None``), read only for
    ``next`` and ``repo``: the task list, the needs-you band, the count and the
    ``updated`` time are the fold's, so a skipped publish can never leave them stale.
    """
    extras: Mapping[str, Any] = published if isinstance(published, Mapping) else {}
    items = [i for i in view.get("items") or [] if isinstance(i, dict)]
    last_entry_at = view.get("conductor", {}).get("last_entry_at") or ""

    asks = [_ask(i) for i in items if _needs_you(i)]
    accepted = sum(1 for i in items if i.get("state") == "accepted")
    board: ConductorBoardPanel = {
        "needs_you": _text("; ".join(asks), 600),
        "done": f"{accepted} of {len(items)}",
        "updated": stamp(last_entry_at),
        "next": _text(extras.get("next")),
        "tasks": [_task(i) for i in items[:MAX_TASKS]],
    }
    if board["next"] and is_newer(last_entry_at, published_at):
        board["next_from"] = stamp(published_at) or "an earlier update"
    repo = _repo(extras, items)
    if repo:
        board["repo"] = repo
    return board

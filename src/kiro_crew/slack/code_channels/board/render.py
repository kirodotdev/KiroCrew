"""The work board as Slack sees it: blocks, fallback text, the needs-you reply.

Pure functions of a :class:`~.model.BoardSnapshot`. Every string that reaches
Slack was already reduced to the published set by :mod:`.source` (titles and
summaries redacted and de-pinged); the branch names and URLs added here are
forge- or git-derived and pass the same redaction before they are used.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from kiro_crew.platform.context import redact_via_context
from kiro_crew.slack.code_channels import wrapup as _wrapup
from kiro_crew.slack.code_channels.board import model

#: Builds a dashboard deep link for a worker slot key ("" when none can be built).
LinkFor = Callable[[str], str]

#: The wrap-up's own words for a PR's state and its CI, so every surface agrees.
PR_WORDS = _wrapup._PR_WORDS
CI_WORDS = _wrapup._CI_WORDS

FALLBACK_PREFIX = "Work board"

#: The built-in Code tab's static ``tab_id`` (it has no persisted view id).
CODE_TAB_ID = "code"
#: A Slack id we put into a ``channel`` element: a channel id or a view id.
_SLACK_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


@dataclass(frozen=True)
class Tabs:
    """The channel's own tabs a message may deep-link to.

    Each is linked with a ``rich_text`` ``channel`` element whose ``tab_id`` is the
    view id ``setView`` answered with (`agents.conversations.setView`_), and the
    built-in Code tab with the static :data:`CODE_TAB_ID`. Only ids this gateway
    recorded for its own views are ever put here.

    .. _agents.conversations.setView: https://docs.slack.dev/reference/methods/agents.conversations.setView
    """

    channel_id: str = ""
    #: The Workers tab's view id ("" while it has none).
    workers: str = ""
    #: Whether the Code tab shows the channel's diff (so a link to it lands on something).
    code: bool = False
    #: ``(PR number, view id)`` of each native pull-request tab.
    prs: tuple[tuple[int, str], ...] = ()

    def pr(self, number: int) -> str:
        for n, view_id in self.prs:
            if n == number:
                return view_id
        return ""

    @property
    def any(self) -> bool:
        return bool(self.channel_id) and bool(self.workers or self.code or self.prs)


def tab_element(channel_id: str, tab_id: str) -> dict | None:
    """A ``channel`` rich-text element that opens *tab_id* in *channel_id*, or None."""
    if not (_SLACK_ID.match(channel_id or "") and _SLACK_ID.match(tab_id or "")):
        return None
    return {"type": "channel", "channel_id": channel_id, "tab_id": tab_id}


def tab_links(tabs: Tabs | None, *, workers: bool = True, code: bool = True) -> list[dict]:
    """``Workers · Code`` as channel-tab elements (empty when no tab can be linked)."""
    if tabs is None or not tabs.channel_id:
        return []
    links = []
    if workers and tabs.workers:
        links.append(tab_element(tabs.channel_id, tabs.workers))
    if code and tabs.code:
        links.append(tab_element(tabs.channel_id, CODE_TAB_ID))
    out: list[dict] = []
    for link in links:
        if link is None:
            continue
        if out:
            out.append({"type": "text", "text": " · "})
        out.append(link)
    return out


def _escape(text: str) -> str:
    """Slack mrkdwn control characters, escaped as Slack documents."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(url: str, text: str) -> str:
    if not url.startswith("https://") and not url.startswith("http://"):
        return _escape(text)
    if redact_via_context(url) != url or any(c in url for c in "<>| \n"):
        return _escape(text)
    return f"<{url}|{_escape(text)}>"


def pr_text(row: model.WorkerRow) -> str:
    """``"PR #12 open · tests passing"`` as plain text (CI only for a live PR), or ``""``.

    The one wording of a worker's PR, used by the board, the context bar and the
    Code tab's worker headers.
    """
    if not row.pr_number:
        return ""
    head = f"PR #{row.pr_number} {PR_WORDS.get(row.pr_state, '')}".strip()
    ci = CI_WORDS.get(row.pr_ci, "")
    return f"{head} · {ci}" if ci and row.pr_state in {"open", "draft"} else head


def pr_words(row: model.WorkerRow) -> str:
    """:func:`pr_text` as mrkdwn, the ``PR #n`` part linked when the forge confirmed it."""
    text = pr_text(row)
    if not text:
        return ""
    label = f"PR #{row.pr_number}"
    head = _link(row.pr_url, label) if row.pr_url else _escape(label)
    return head + _escape(text[len(label) :])


def header_line(snapshot: model.BoardSnapshot) -> str:
    """The header line: the round, the overall word, then the aggregate line.

    The overall word leads only when the aggregate line does not already say it
    (an empty or finished board reads from the aggregate alone).
    """
    parts = [f"Round {snapshot.round}"] if snapshot.round else []
    overall = snapshot.overall
    if overall in {model.OVERALL_EMPTY, model.OVERALL_DONE}:
        parts.append(snapshot.aggregate)
    else:
        parts.append(f"{overall[:1].upper()}{overall[1:]}: {snapshot.aggregate}")
    return " · ".join(parts)


def row_text(row: model.WorkerRow, link_for: LinkFor, *, now: datetime | None = None) -> str:
    """One worker's mrkdwn (the fallback look): title, then the same details and output."""
    when = now or datetime.now(timezone.utc)
    lines = [f"*{_escape(row.title)}*"]
    detail = "".join(_mrkdwn(e) for e in _details(row, when))
    if detail:
        lines.append(detail)
    meta: list[str] = []
    if row.branch:
        meta.append(f"`{_code_safe(redact_via_context(row.branch))}`")
    output = "".join(_mrkdwn(e) for e in _output(row))
    if output:
        meta.append(output)
    link = link_for(row.session_key) if row.session_key else ""
    if link:
        meta.append(_link(link, "Open in dashboard"))
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)[:2900]


def _code_safe(text: str) -> str:
    """Text for inside a mrkdwn code span: one line, no backtick, mrkdwn escaped."""
    return _escape(" ".join(text.split()).replace("`", "'"))


def _mrkdwn(element: dict) -> str:
    """A rich-text element of :func:`_details` / :func:`_output` as mrkdwn."""
    text = str(element.get("text") or "")
    if element.get("type") == "user":
        user = str(element.get("user_id") or "")
        return f"<@{user}>" if user.isalnum() else ""
    if element.get("type") == "link":
        return _link(str(element.get("url") or ""), text)
    style = element.get("style") or {}
    if style.get("code"):
        return f"`{_code_safe(text)}`"
    if style.get("bold"):
        return f"*{_escape(text)}*"
    return _escape(text)


def board_blocks(
    snapshot: model.BoardSnapshot,
    link_for: LinkFor,
    *,
    conductor_link: str = "",
    updated: str = "",
    look: str = "plan",
    block_seed: str = "",
    now: datetime | None = None,
    tabs: Tabs | None = None,
) -> list[dict]:
    """The pinned board message: Slack's own ``plan`` block, or the mrkdwn rows.

    *look* ``plan`` (the board) or ``rows`` (the fallback a client that refuses
    the plan block gets). *block_seed* makes every ``block_id`` new for each
    edit, as Slack requires of a plan; ``""`` leaves ids out (the digest's render).
    *tabs* deep-links the channel's own tabs: a ``Workers · Code`` line under the
    header and each task's ``PR #n`` to its pull-request tab. The mrkdwn rows
    carry no tab links (mrkdwn has no way to name a tab).
    """
    when = now or datetime.now(timezone.utc)
    stamp = f"Work board · updated {updated}" if updated else "Work board"
    context = stamp
    if conductor_link:
        context += " · " + _link(conductor_link, "Open the conductor in the dashboard")
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape(snapshot.aggregate)}*"}},
    ]
    links = tab_links(tabs) if look == "plan" else []
    if links:
        blocks.append(_rich([{"type": "text", "text": "Tabs: "}, *links]))
    if look == "plan" and snapshot.rows:
        blocks.append(plan_block(snapshot, link_for, now=when, tabs=tabs))
    elif not snapshot.rows:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "No work dispatched yet. Each worker appears here once it starts.",
                },
            }
        )
    else:
        for row in snapshot.shown:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": row_text(row, link_for, now=when)},
                }
            )
        if snapshot.omitted:
            folded = f"+{snapshot.omitted} more items, counted above and listed in the dashboard"
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": folded}]})
    if block_seed:
        for index, block in enumerate(blocks):
            block["block_id"] = f"board-{block_seed}-{index}"
    return blocks


MAX_TASKS = model.MAX_TASKS
_TASK_STATUS = {
    model.WORKING: "in_progress",
    model.WAITING_ON_YOU: "in_progress",
    model.BLOCKED: "error",
    model.IDLE: "in_progress",
    model.DONE: "complete",
    model.LOST: "error",
}
_ISSUE_PREFIX = re.compile(r"^(#\d+:\s*)")


def _common_prefix(rows: list[model.WorkerRow]) -> str:
    """The ``#123: `` prefix every row's title shares (``""`` when they do not all)."""
    first = _ISSUE_PREFIX.match(rows[0].title) if rows else None
    if first is None or len(rows) < 2:
        return ""
    prefix = first.group(1)
    return prefix if all(r.title.startswith(prefix) for r in rows) else ""


def _elapsed(stamp: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        secs = int((now - then).total_seconds())
    except (TypeError, ValueError):
        return ""
    if secs < 0:
        return ""
    if secs < 60:
        return "<1m"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


_STEP_VERBS = ("Running ", "Editing ", "Reading ")


def _step_elements(step: str) -> list[dict]:
    """``Running `pytest -q``` as rich text: the verb plain, the call in code style."""
    for verb in _STEP_VERBS:
        if step.startswith(verb) and len(step) > len(verb):
            return [
                {"type": "text", "text": verb},
                {"type": "text", "text": step[len(verb) :], "style": {"code": True}},
            ]
    return [{"type": "text", "text": step}] if step else []


def _rich(elements: list[dict]) -> dict:
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}


def _details(row: model.WorkerRow, now: datetime, elapsed: bool = True) -> list[dict]:
    """The row's status words; *elapsed* off leaves the minute counts out (a digest)."""
    if row.stopped:
        # A Stop from the Workers tab, until the worker's next turn starts.
        out: list[dict] = [
            {"type": "text", "text": "Stopped by "},
            {"type": "user", "user_id": row.stopped_by},
        ]
        if row.stopped_at:
            out.append({"type": "text", "text": f" · {row.stopped_at}"})
        return out
    if row.state == model.WAITING_ON_YOU:
        out = [{"type": "text", "text": "Waiting on you:", "style": {"bold": True}}]
        if row.waiting_on == "approval":
            asked = row.step
            for verb in _STEP_VERBS:
                asked = asked.removeprefix(verb) if asked.startswith(verb) else asked
            out.append({"type": "text", "text": " approve "})
            if asked:
                out.append({"type": "text", "text": asked, "style": {"code": True}})
            else:
                out.append({"type": "text", "text": row.approval_tool or "a tool"})
        elif row.summary:
            out.append({"type": "text", "text": " " + row.summary})
        return out
    if row.state == model.WORKING:
        out = _step_elements(row.step or model.STEP_THINKING)
        took = _elapsed(row.turn_started_at, now) if elapsed else ""
        if took:
            out.append({"type": "text", "text": f" · {took}"})
        return out
    if row.state == model.IDLE:
        idle_for = _elapsed(row.report_stamp, now) if elapsed else ""
        text = f"Idle · {idle_for}" if idle_for else "Idle"
        note = row.step or row.summary
        return [{"type": "text", "text": f"{text} · {note}" if note else text}]
    label = model.row_label(row)
    text = f"{label[:1].upper()}{label[1:]}"
    if row.summary:
        text += f" · {row.summary}"
    return [{"type": "text", "text": text}]


def _ci_counts(row: model.WorkerRow) -> str:
    parts = row.pr_checks.split("/") if row.pr_checks else []
    if len(parts) != 3 or row.pr_state not in {"open", "draft"}:
        return CI_WORDS.get(row.pr_ci, "") if row.pr_state in {"open", "draft"} else ""
    passed, total, running = (int(p) for p in parts)
    if not total:
        return ""
    text = f"CI {passed}/{total} passing"
    return f"{text} · {running} running" if running else text


def _output(row: model.WorkerRow, tabs: Tabs | None = None) -> list[dict]:
    """``PR #12 · CI 3/5 passing``: the PR linked to its own tab when the channel has
    one (the tab is named ``PR #12``), else to the forge."""
    if not row.pr_number:
        return []
    label = f"PR #{row.pr_number}"
    url = row.pr_url if _safe_url(row.pr_url) else ""
    tab = tab_element(tabs.channel_id, tabs.pr(row.pr_number)) if tabs is not None else None
    head: dict
    if tab is not None:
        head = tab
    elif url:
        head = {"type": "link", "url": url, "text": label}
    else:
        head = {"type": "text", "text": label}
    out = [head]
    state = PR_WORDS.get(row.pr_state, "") if row.pr_state not in {"open"} else ""
    if state:
        out.append({"type": "text", "text": f" {state}"})
    ci = _ci_counts(row)
    if ci:
        out.append({"type": "text", "text": f" · {ci}"})
    return out


def _safe_url(url: str) -> bool:
    return (
        bool(url)
        and url.startswith("https://")
        and redact_via_context(url) == url
        and not any(c in url for c in "<>| \n")
    )


def plan_block(
    snapshot: model.BoardSnapshot,
    link_for: LinkFor,
    *,
    now: datetime,
    tabs: Tabs | None = None,
) -> dict:
    """One ``plan`` with one task per worker (Slack's own status marks, no emoji)."""
    shown = list(snapshot.plan_rows)
    more = len(snapshot.rows) - len(shown)
    prefix = _common_prefix(shown)
    tasks: list[dict] = []
    for row in shown:
        if row.state == model.CLOSED:
            status = "error" if row.closed_as == "rejected" else "complete"
        else:
            status = _TASK_STATUS.get(row.state, "in_progress")
        title = row.title[len(prefix) :] if prefix else row.title
        task: dict = {
            "task_id": row.item_id,
            "title": title or row.title,
            "status": status,
        }
        details = _details(row, now)
        if details:
            task["details"] = _rich(details)
        output = _output(row, tabs)
        if output:
            task["output"] = _rich(output)
        if row.pr_number and _safe_url(row.pr_url):
            task["sources"] = [{"type": "url", "url": row.pr_url, "text": f"PR #{row.pr_number}"}]
        tasks.append(task)
    if more:
        tasks.append(
            {
                "task_id": "more",
                "title": f"+{more} more, counted above and listed in the dashboard",
                "status": "in_progress",
            }
        )
    return {"type": "plan", "title": snapshot.aggregate, "tasks": tasks}


def fallback_text(snapshot: model.BoardSnapshot) -> str:
    """Notification / screen-reader text for the board (no markup)."""
    return f"{FALLBACK_PREFIX}: {snapshot.aggregate}"


def needs_you_text(row: model.WorkerRow, owner_id: str, link_for: LinkFor) -> str:
    """The threaded reply that pages the owner for a worker that waits on them."""
    who = f"<@{owner_id}> " if owner_id and owner_id.isalnum() else ""
    if row.waiting_on == "approval":
        text = f"{who}*{_escape(row.title)}* has a tool approval waiting on you"
        if row.approval_tool:
            text += f": approve {_escape(row.approval_tool)}"
    else:
        text = f"{who}*{_escape(row.title)}* is waiting on you"
        if row.summary:
            text += f": {_escape(row.summary)}"
    link = link_for(row.session_key) if row.session_key else ""
    if link:
        text += f" — {_link(link, 'Open in dashboard')}"
    return text


def outcome_line(row: model.WorkerRow) -> str:
    """One wrap-up line: ``Fix login: PR #12 merged`` / ``rejected, PR #9 closed``."""
    pr = pr_words(row)
    if row.state == model.CLOSED:
        outcome = row.closed_as or "closed"
        if pr:
            outcome += f", {pr}"
    elif pr:
        outcome = pr
    else:
        outcome = f"{_escape(model.row_label(row))}, no PR"
    return f"• {_escape(row.title)}: {outcome}"


# ── The Workers tab (a block_kit view, published by ..tabs) ──────────────

#: The Workers tab's buttons. Stop's ``value`` is the row's ledger item id, an
#: opaque handle :mod:`..worker_stop` resolves against this channel's OWN board;
#: Open in dashboard is a url button (Slack still posts a no-op click for it).
WORKER_STOP_ACTION = "kc_cc_worker_stop"
WORKER_OPEN_ACTION = "kc_cc_worker_open"
#: Open workers the tab lists in full (three blocks each, under Slack's 50 with
#: the header, the overflow line and the done line).
WORKERS_TAB_ROWS = 14
#: Room the settled workers' one-line list may take (a context text holds 3,000).
_DONE_CHARS = 2400
#: Slack's limits on a confirm dialog's title and a button's text.
_CONFIRM_TITLE = 100
_BUTTON_TEXT = 75


def _status_elements(row: model.WorkerRow, now: datetime, elapsed: bool = True) -> list[dict]:
    """``Working · Running `pytest -q` · 4m`` / ``Waiting on you: approve `git push```."""
    if row.stopped:
        return _details(row, now, elapsed)
    if row.state == model.WORKING:
        return [{"type": "text", "text": "Working · "}, *_details(row, now, elapsed)]
    if row.state in {model.WAITING_ON_YOU, model.IDLE}:
        return _details(row, now, elapsed)
    word = {model.BLOCKED: "Stuck", model.LOST: "Lost"}.get(row.state)
    if word is None:
        return _details(row, now, elapsed)
    text = f"{word} · {row.summary}" if row.summary else word
    return [{"type": "text", "text": text}]


def _dashboard_url(url: str) -> str:
    """A url button target: http(s), unchanged by the redaction, no markup characters."""
    if not url or not (url.startswith("https://") or url.startswith("http://")):
        return ""
    if redact_via_context(url) != url or any(c in url for c in "<>| \n") or len(url) > 3000:
        return ""
    return url


def _plain(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _worker_actions(row: model.WorkerRow, link_for: LinkFor) -> dict | None:
    elements: list[dict] = []
    url = _dashboard_url(link_for(row.session_key)) if row.session_key else ""
    if url:
        elements.append(
            {
                "type": "button",
                "action_id": WORKER_OPEN_ACTION,
                "text": {"type": "plain_text", "text": "Open in dashboard"},
                "url": url,
            }
        )
    if row.state == model.WORKING and row.session_key and not row.stopped:
        # Outlined (no style): Stop is a rare, deliberate act, never the primary one.
        elements.append(
            {
                "type": "button",
                "action_id": WORKER_STOP_ACTION,
                "text": {"type": "plain_text", "text": "Stop"},
                "value": row.item_id,
                "confirm": {
                    "title": {
                        "type": "plain_text",
                        "text": _plain(f"Stop {row.title}?", _CONFIRM_TITLE),
                    },
                    "text": {
                        "type": "plain_text",
                        "text": (
                            "This stops this worker's current turn only. The conductor "
                            "and the other workers keep going."
                        ),
                    },
                    "confirm": {"type": "plain_text", "text": "Stop"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            }
        )
    if not elements:
        return None
    for element in elements:
        element["text"]["text"] = _plain(element["text"]["text"], _BUTTON_TEXT)
    return {"type": "actions", "block_id": f"worker-{row.item_id}", "elements": elements}


def _settled_line(row: model.WorkerRow, tabs: Tabs | None) -> list[dict]:
    """``Done: <title> · PR #N · CI x/y`` as rich text, the PR opening its tab.

    A PR without a tab of its own (one that merged has none) links the forge. A
    closed item reads ``Rejected: <title>`` (or ``Abandoned``).
    """
    if row.state == model.DONE:
        head = "Done: "
    else:
        head = f"{(row.closed_as or 'closed').capitalize()}: "
    out: list[dict] = [{"type": "text", "text": head}, {"type": "text", "text": row.title}]
    if row.pr_number:
        out.append({"type": "text", "text": " · "})
        out.append(_output(row, tabs)[0])
        counts = _CHECKS.fullmatch(row.pr_checks or "")
        if counts is not None and int(counts.group(2)):
            out.append({"type": "text", "text": f" · CI {counts.group(1)}/{counts.group(2)}"})
    return out


#: ``"<passed>/<total>/<running>"`` (``WorkerRow.pr_checks``).
_CHECKS = re.compile(r"(\d+)/(\d+)/(\d+)")


def _line_length(elements: list[dict]) -> int:
    return sum(len(str(e.get("text") or "")) or 12 for e in elements)


def workers_view_blocks(
    snapshot: model.BoardSnapshot,
    link_for: LinkFor,
    *,
    tabs: Tabs | None = None,
    updated: str = "",
    now: datetime | None = None,
    elapsed: bool = True,
) -> list[dict]:
    """The interactive Workers tab: the board's own rows, with Stop and Open in dashboard.

    The same snapshot (and so the same redacted, de-pinged, host-scrubbed text)
    as the pinned board, laid out per worker: the title in bold; a status line
    (state, the call in flight, elapsed); the PR -- linked to its own tab when
    the channel has one -- with its CI count; then the buttons. Stop shows only
    while the worker is working. Settled workers fold into one line each at the
    bottom. *updated* (``HH:MM UTC``) and, with *elapsed* off, the minute counts
    are left out of the digest's render, so time passing alone changes nothing.
    """
    when = now or datetime.now(timezone.utc)
    header = f"Updated {updated}" if updated else "Workers"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape(snapshot.aggregate)}*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": header}]},
    ]
    open_rows = [r for r in snapshot.rows if not r.settled]
    settled = [r for r in snapshot.rows if r.settled]
    for row in open_rows[:WORKERS_TAB_ROWS]:
        lines: list[dict] = [{"type": "text", "text": row.title, "style": {"bold": True}}]
        status = _status_elements(row, when, elapsed)
        if status:
            lines += [{"type": "text", "text": "\n"}, *status]
        output = _output(row, tabs)
        if output:
            lines += [{"type": "text", "text": "\n"}, *output]
        blocks.append({"type": "divider"})
        block = _rich(lines)
        block["block_id"] = f"worker-{row.item_id}-text"
        blocks.append(block)
        actions = _worker_actions(row, link_for)
        if actions is not None:
            blocks.append(actions)
    more = len(open_rows) - WORKERS_TAB_ROWS
    if more > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"+{more} more, listed in the dashboard"}],
            }
        )
    if settled:
        done_lines: list[dict] = []
        used = 0
        kept = 0
        for row in reversed(settled):  # the most recent first
            line = _settled_line(row, tabs)
            size = _line_length(line) + 1
            if used + size > _DONE_CHARS:
                break
            if done_lines:
                done_lines.append({"type": "text", "text": "\n"})
            done_lines += line
            used += size
            kept += 1
        rest = len(settled) - kept
        if rest:
            done_lines.append({"type": "text", "text": f"\n+{rest} more"})
        blocks.append({"type": "divider"})
        done_block = _rich(done_lines)
        done_block["block_id"] = "workers-settled"
        blocks.append(done_block)
    return blocks

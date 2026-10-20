"""The work board as Slack sees it: blocks, fallback text, the needs-you reply.

Pure functions of a :class:`~.model.BoardSnapshot`. Every string that reaches
Slack was already reduced to the published set by :mod:`.source` (titles and
summaries redacted and de-pinged); the branch names and URLs added here are
forge- or git-derived and pass the same redaction before they are used.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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
) -> list[dict]:
    """The pinned board message: Slack's own ``plan`` block, or the mrkdwn rows.

    *look* ``plan`` (the board) or ``rows`` (the fallback a client that refuses
    the plan block gets). *block_seed* makes every ``block_id`` new for each
    edit, as Slack requires of a plan; ``""`` leaves ids out (the digest's render).
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
    if look == "plan" and snapshot.rows:
        blocks.append(plan_block(snapshot, link_for, now=when))
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


def _details(row: model.WorkerRow, now: datetime) -> list[dict]:
    if row.state == model.WAITING_ON_YOU:
        out: list[dict] = [{"type": "text", "text": "Waiting on you:", "style": {"bold": True}}]
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
        elapsed = _elapsed(row.turn_started_at, now)
        if elapsed:
            out.append({"type": "text", "text": f" · {elapsed}"})
        return out
    if row.state == model.IDLE:
        elapsed = _elapsed(row.report_stamp, now)
        text = f"Idle · {elapsed}" if elapsed else "Idle"
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


def _output(row: model.WorkerRow) -> list[dict]:
    if not row.pr_number:
        return []
    label = f"PR #{row.pr_number}"
    url = row.pr_url if _safe_url(row.pr_url) else ""
    head: dict = (
        {"type": "link", "url": url, "text": label} if url else {"type": "text", "text": label}
    )
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


def plan_block(snapshot: model.BoardSnapshot, link_for: LinkFor, *, now: datetime) -> dict:
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
        output = _output(row)
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

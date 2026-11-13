"""One crew member's recap, read from records only: no model call.

``GET /api/chat/slots/{slot}/crew-workers/{worker}/recap`` answers what a crewmate's
Sessions tab shows when the person expands one session in place
(``docs/request-for-change/rfc-mate-sessions-folder-or-lineage.md``, "Owner decision:
a click shows the recap in place"). Every line is a plain render of what the work
ledger and the session's own crew log already hold:

- the work item (``work/recorded`` entries, read through the board's seam): title,
  status and reason, state, last report summary, pr and artifacts, the conductor's
  decision, ``last_report_at``;
- the session's ``timeline`` fold for its newest ``turn/completed`` when no report
  stamps a time;
- its ``approvals`` fold for the pending requests, and its ``tools`` fold for the
  ``ask_question`` calls still open (a count: the log holds no question text).

The model-written intent summary is never read. The route takes the Crew panel's
gate and its tree check (``crew_workers``): owner only, app tokens refused, and the
session must be in the named root's own tree, a Crew Mode root's or a mate's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers import crew_workers

logger = logging.getLogger(__name__)

#: Pending approvals the recap names; past it the count stays exact.
RECAP_APPROVALS_MAX = 5
#: The longest text line the recap carries (title, summary, decision, reason).
RECAP_TEXT_MAX = 2000
#: Artifact pointers the recap carries, and the longest key and value of each.
RECAP_ARTIFACTS_MAX = 16
RECAP_ARTIFACT_KEY_MAX = 64
RECAP_ARTIFACT_VALUE_MAX = 512
#: The longest approval tool name the recap names.
RECAP_TOOL_MAX = 200
#: The tool a question card is opened with, under any server prefix.
ASK_QUESTION_TOOL = "ask_question"


def _text(value: Any) -> str:
    """*value* when it is a string, whole: it is cut only after redaction."""
    return value if isinstance(value, str) else ""


def _cut(value: Any, cap: int) -> str:
    """A redacted string cut to *cap*; a cut never splits a secret, since redaction ran first."""
    return value[:cap] if isinstance(value, str) else ""


def _redacted(fields: Mapping[str, Any]) -> dict[str, Any]:
    """*fields* through the work-ledger board's own egress redactor.

    ``work_ledger_board._redact_deep`` runs ``redact_via_context`` (the credential
    and exfiltration-URL scanners, through the host's credential policy) on every
    string and every mapping key, so a token a worker pasted into its report is
    removed before the recap reaches the browser.
    """
    from kiro_crew.dashboard.handlers import work_ledger_board

    out = work_ledger_board._redact_deep(dict(fields))
    return out if isinstance(out, dict) else {}


def _crew_item(root: str, members: Iterable[str], worker: str) -> Any:
    """*worker*'s item: the root's own ledger first, then each crew member's. BLOCKING."""
    from kiro_crew import session_ledger

    key = session_ledger.ledger_key(worker)
    for board in dict.fromkeys([root, *members]):
        if board and board != worker:
            item = crew_workers._ledger_work_items(board).get(key)
            if item is not None:
                return item
    return None


def _pr_link_of(value: str) -> str:
    """The canonical link of *value* when it is a GitHub pull-request URL, else ``""``.

    Parsed by the repository's own GitHub pull-request parser
    (``monitoring.github_pull_request.parse_github_pull_request_target``), the one
    the PR monitor uses, so a branch named ``pull/7`` is not a pull request.
    """
    from kiro_crew.monitoring.github_pull_request import parse_github_pull_request_target

    try:
        target = parse_github_pull_request_target(value)
    except ValueError:
        return ""
    return f"https://github.com/{target.owner}/{target.repo}/pull/{target.number}"


def _github_pr_url(item: Any) -> str:
    """The item's pull-request link.

    Built from the item's typed fields first: its ``pr`` number on the repo its
    acceptance names. Failing that, a reported artifact that is a GitHub
    pull-request URL. Both go through :func:`_pr_link_of`; ``""`` otherwise.
    """
    pr = getattr(item, "pr", None)
    acceptance = getattr(item, "acceptance", None) or {}
    repo = acceptance.get("repo") if isinstance(acceptance, Mapping) else None
    if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0 and isinstance(repo, str):
        link = _pr_link_of(f"https://github.com/{repo}/pull/{pr}")
        if link:
            return link
    for value in (getattr(item, "artifacts", None) or {}).values():
        if isinstance(value, str):
            link = _pr_link_of(value)
            if link:
                return link
    return ""


def _artifacts(item: Any) -> dict[str, str]:
    raw = getattr(item, "artifacts", None) or {}
    out: dict[str, str] = {}
    for key, value in list(raw.items())[:RECAP_ARTIFACTS_MAX]:
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def _cut_artifacts(redacted: Any) -> dict[str, str]:
    """Redacted artifact pointers, each key and value cut to its cap."""
    out: dict[str, str] = {}
    if isinstance(redacted, Mapping):
        for key, value in redacted.items():
            if isinstance(key, str) and isinstance(value, str):
                out[_cut(key, RECAP_ARTIFACT_KEY_MAX)] = _cut(value, RECAP_ARTIFACT_VALUE_MAX)
    return out


def _unit_of(state: Any, slot: Any, worker: str) -> str:
    """The crew-log unit to read for *worker*, ``""`` when none resolves. BLOCKING.

    A live session's serving unit first. A closed tab has no provider, so its
    newest retained unit is read from the store by the slot its headers name
    (``newest_unit_in_succession``): its folds still hold its last turn.
    """
    sessions = getattr(state, "sessions", None)
    if slot is not None and sessions is not None:
        try:
            from kiro_crew.crew_log.resolve import unit_for_session_key
            from kiro_crew.dashboard.chat_utils import effective_session_key

            key = effective_session_key(slot)
            unit = unit_for_session_key(sessions, key) if isinstance(key, str) and key else ""
            if unit:
                return unit
        except Exception:
            logger.debug("crew recap: no serving unit for this session", exc_info=True)
    try:
        from kiro_crew import session_ledger
        from kiro_crew.crew_log.projection_readers import newest_unit_in_succession

        # Only a unit the ledger seam still lists counts: ``crew_log_units`` drops a
        # permanently deleted session's units, which the raw store listing keeps.
        units = session_ledger.crew_log_units(worker)
        if not units:
            return ""
        unit = newest_unit_in_succession(worker)
        return unit if unit in units else units[-1]
    except Exception:
        logger.debug("crew recap: no saved unit for this session", exc_info=True)
        return ""


def _iso_ms(ms: Any) -> str:
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    return ""


def _session_folds(unit: str) -> dict[str, Any]:
    """The session's last turn end, pending approvals and open questions. BLOCKING."""
    out: dict[str, Any] = {"last_turn": "", "approvals": [], "approvals_count": 0, "questions": 0}
    if not unit:
        return out
    try:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.projection_session_memo import read_projection

        if not crew_log_emit.enabled():
            return out
        timeline = read_projection(unit, "timeline").value
        approvals = read_projection(unit, "approvals").value
        tools = read_projection(unit, "tools").value
    except Exception:
        logger.debug("crew recap: the session's folds could not be read", exc_info=True)
        return out
    for moment in reversed(timeline.get("moments") or []):
        if isinstance(moment, Mapping) and moment.get("type") == "turn/completed":
            out["last_turn"] = _iso_ms(moment.get("time"))
            break
    pending = [p for p in approvals.get("pending_requests") or [] if isinstance(p, Mapping)]
    out["approvals_count"] = int(approvals.get("pending") or 0)
    out["approvals"] = [
        {"tool": _text(p.get("tool")), "requested_at": _iso_ms(p.get("time"))}
        for p in pending[:RECAP_APPROVALS_MAX]
    ]
    out["questions"] = sum(
        1
        for call in tools.get("open_calls") or []
        if isinstance(call, Mapping) and _is_ask_question(call.get("name"))
    )
    return out


def _is_ask_question(name: Any) -> bool:
    """``ask_question`` under any prefix the harness spells it with."""
    if not isinstance(name, str):
        return False
    tail = name.replace("@", "").replace("/", "_").rsplit("::", 1)[-1]
    return tail == ASK_QUESTION_TOOL or tail.endswith("_" + ASK_QUESTION_TOOL)


def build_recap(
    state: Any, root: str, worker: str, row: Mapping[str, Any] | None, members: list[str]
) -> dict[str, Any]:
    """The recap of *worker* under *root*. BLOCKING: ledger and crew-log reads."""
    item = _crew_item(root, members, worker)
    slot_title = str((row or {}).get("title") or "")
    if row is None:
        slot_title = crew_workers._closed_titles(state, [worker]).get(worker, ("", ""))[0]
    get_slot = getattr(state, "get_slot", None)
    slot = get_slot(worker) if row is not None and callable(get_slot) else None
    folds = _session_folds(_unit_of(state, slot, worker))
    text = _redacted(
        {
            "title": _text(getattr(item, "title", "")) if item is not None else "",
            "summary": _text(getattr(item, "summary", "")) if item is not None else "",
            "decision": _text(getattr(item, "decision", "")) if item is not None else "",
            "artifacts": _artifacts(item) if item is not None else {},
            "pr_url": _github_pr_url(item) if item is not None else "",
            "approvals": folds["approvals"],
        }
    )
    pr = getattr(item, "pr", None) if item is not None else None
    return {
        "key": worker,
        "root": root,
        "title": _cut(text.get("title"), RECAP_TEXT_MAX)
        or slot_title
        or crew_workers._short_key(worker),
        "ledger_item": str(getattr(item, "item_id", "") or "") if item is not None else "",
        "status": str(getattr(item, "status", "") or "") if item is not None else "",
        "reason": str(getattr(item, "reason", "") or "") if item is not None else "",
        "state": str(getattr(item, "state", "") or "") if item is not None else "",
        "summary": _cut(text.get("summary"), RECAP_TEXT_MAX),
        "decision": _cut(text.get("decision"), RECAP_TEXT_MAX),
        "pr": pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
        "pr_url": text.get("pr_url") or "",
        "artifacts": _cut_artifacts(text.get("artifacts")),
        "last_update": (str(getattr(item, "last_report_at", "") or "") if item is not None else "")
        or folds["last_turn"],
        "open_approvals": folds["approvals_count"],
        "approvals": [
            {
                "tool": _cut(a.get("tool"), RECAP_TOOL_MAX),
                "requested_at": _cut(a.get("requested_at"), RECAP_TOOL_MAX),
            }
            for a in text.get("approvals") or []
            if isinstance(a, Mapping)
        ],
        "open_questions": folds["questions"],
        "live": row is not None,
    }


async def api_crew_worker_recap(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/crew-workers/{worker}/recap -- one session's recap."""
    refused = await crew_workers._gate(request, "crew_mode.recap")
    if refused is not None:
        return refused
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    worker = request.match_info.get("worker", "")
    rows = crew_workers._stamped_rows(state)
    if not (crew_workers._live_root(rows, root)[0] or crew_workers._closed_root_kind(state, root)):
        return crew_workers._not_root()
    live = crew_workers._tree_workers(rows, root)
    live_keys = {str(r.get("key")) for r in rows}
    closed = crew_workers._closed_workers(root, live_keys)
    row = next((r for r in live if r.get("key") == worker), None)
    if row is None and worker not in closed:
        return crew_workers._bad(
            "not_a_worker", "that session is not one of this conductor's crew members", status=404
        )
    members = [*(str(r.get("key")) for r in live), *closed]
    recap = await asyncio.to_thread(build_recap, state, root, worker, row, members)
    return web.json_response(recap)

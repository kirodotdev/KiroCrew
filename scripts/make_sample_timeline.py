#!/usr/bin/env python3
"""Write ``sample_timeline.json``: the fixture group B's two pages are shot from.

NOT a capture, and its name says so, the same way ``sample_workstreams.json`` does. It
is a ``workstreams`` fold value in the shape the fold now renders -- including the
``created_at`` and ``closed_at`` the spans change added -- arranged so that every case
the ``timeline`` and ``org-chart`` pages have to tell apart is actually on the screen
rather than only described in a manifest:

- five worker sessions running AT THE SAME TIME, which is the thing a timeline exists
  to show and the thing no single-session page can
- one worker session bound to TWO tasks, so the one-bill-per-session rule is visible
- a blocked task and a question task, which need a person rather than more time
- a task quiet past twenty minutes and one quiet past two hours
- a task with no report at all, which is not the same fact as a long silence
- a task with NO START STAMP, which must be listed as unplaceable, not dropped
- a terminal task with NO CLOSE STAMP, which must say so rather than draw to now
- a nested board, so a sub-lead appears in both the timeline's lead section and the
  org chart's graph
- messy multi-line summaries, so the card's one-line cleaning is exercised
- a board with ``tasks_omitted``, so the truncation line renders

``now`` pins the page's clock, so a screenshot taken any day shows the elapsed figures
this fixture means.

Run: python scripts/make_sample_timeline.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime(2026, 10, 6, 0, 40, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def ago(**kw: float) -> str:
    return iso(NOW - timedelta(**kw))


def task(
    item_id: str,
    title: str,
    *,
    spender: str | None,
    created_at: str,
    closed_at: str = "",
    state: str = "open",
    status: str | None = None,
    summary: str = "",
    verdict: str | None = None,
    pr: str | None = None,
    credits: float = 0.0,
    credits_reported: bool = False,
    duration_ms: int = 0,
    last_report_at: str = "",
    decision: str = "",
    events: list[dict[str, object]] | None = None,
    events_seen: int | None = None,
    round_: int = 5,
) -> dict[str, object]:
    tail = events or []
    return {
        "item_id": item_id,
        "title": title,
        "state": state,
        "status": status,
        "summary": summary,
        "verdict": verdict,
        "pr": pr,
        "round": round_,
        "spender": spender,
        "credits": credits,
        "credits_reported": credits_reported,
        "duration_ms": duration_ms,
        "last_report_at": last_report_at,
        "created_at": created_at,
        "closed_at": closed_at,
        "decision": decision,
        "events": tail,
        "events_seen": events_seen if events_seen is not None else len(tail),
    }


def ev(at: str, kind: str, text: str, status: str | None = None) -> dict[str, object]:
    return {"at": at, "kind": kind, "status": status, "text": text}


# --------------------------------------------------------------------------- #
# board 1: the round-5 lead, five workers running at once
# --------------------------------------------------------------------------- #
B1 = {
    "id": "board-1",
    "goal": "Round 5: manage the crew from the page",
    "round": 5,
    "parent": None,
    "total": 8,
    "accepted": 2,
    "open": 5,
    "needs_you": 1,
    "credits": 41.86,
    "credits_reported": True,
    "last_activity_at": ago(minutes=5),
    "tasks_omitted": 0,
    "series": [],
    "tasks": [
        task(
            "it_a",
            "Needs-you desk and the card action route",
            spender="1",
            created_at=ago(hours=6, minutes=30),
            status="progress",
            summary="  Route and the two postMessage types are in;\n  the undo window is wired to the\tserver's own deadline  ",
            credits=9.14,
            credits_reported=True,
            duration_ms=16_800_000,
            last_report_at=ago(minutes=9),
            decision="The page never names a button. The card's own actions array is the whole vocabulary.",
            events=[
                ev(ago(minutes=9), "report", "action route green, 11 tests", "progress"),
                ev(ago(hours=2), "decide", "owner-gated, same two auth gates as the GET"),
                ev(ago(hours=6, minutes=30), "create", "created from the round plan"),
            ],
            events_seen=14,
        ),
        task(
            "it_b",
            "Timeline and the reporting graph",
            spender="2",
            created_at=ago(hours=6, minutes=28),
            status="progress",
            summary="Fold spans landed with 11 tests; both templates drawing, shots next",
            credits=7.52,
            credits_reported=True,
            duration_ms=15_400_000,
            last_report_at=ago(minutes=14),
            decision="New built-in templates rather than another section of the report page.",
            events=[
                ev(ago(minutes=14), "report", "timeline renders at 430 and wide", "progress"),
                ev(ago(hours=3), "report", "created_at/closed_at on the task row", "progress"),
                ev(ago(hours=6, minutes=28), "create", "created from the round plan"),
            ],
            events_seen=9,
        ),
        task(
            "it_c",
            "Live session rail",
            spender="3",
            created_at=ago(hours=6, minutes=25),
            status="blocked",
            summary="The rail needs a session-open message the host does not have yet",
            credits=4.31,
            credits_reported=True,
            duration_ms=7_200_000,
            last_report_at=ago(hours=3),
            decision="",
            events=[
                ev(ago(hours=3), "report", "blocked on the host handler", "blocked"),
                ev(ago(hours=6, minutes=25), "create", "created from the round plan"),
            ],
        ),
        task(
            "it_d",
            "Plan lane: what the crew will do next",
            spender="4",
            created_at=ago(hours=6, minutes=20),
            status="progress",
            summary="Two mockups drawn, waiting on a pick",
            credits=6.08,
            credits_reported=True,
            duration_ms=9_000_000,
            last_report_at=ago(hours=2, minutes=35),
            events=[
                ev(ago(hours=2, minutes=35), "report", "mockups up, which one", "progress"),
                ev(ago(hours=6, minutes=20), "create", "created from the round plan"),
            ],
        ),
        task(
            "it_e",
            "Merge the five branches and shoot the demo",
            spender="5",
            created_at=ago(minutes=50),
            status=None,
            summary="",
            credits=0.0,
            credits_reported=False,
            last_report_at="",
            events=[ev(ago(minutes=50), "create", "created once the five were dispatched")],
        ),
        task(
            "it_f",
            "Round-4 report page",
            spender="6",
            created_at=ago(hours=12, minutes=40),
            closed_at=ago(hours=8),
            state="accepted",
            status="done",
            verdict="pass",
            pr="14982",
            summary="One report page replaces the three round-3 boards",
            credits=14.81,
            credits_reported=True,
            duration_ms=21_600_000,
            last_report_at=ago(hours=8, minutes=12),
            events=[
                ev(ago(hours=8), "close", "accepted"),
                ev(ago(hours=8, minutes=12), "report", "shots in, PR green", "done"),
            ],
        ),
        task(
            "it_g",
            "The spend half of the workstreams fold",
            spender="6",
            created_at=ago(hours=10, minutes=40),
            closed_at=ago(hours=8, minutes=30),
            state="accepted",
            status="done",
            verdict="pass",
            summary="Cost counts only what a worker reported",
            credits=14.81,
            credits_reported=True,
            duration_ms=21_600_000,
            last_report_at=ago(hours=8, minutes=40),
            events=[ev(ago(hours=8, minutes=30), "close", "accepted")],
        ),
        task(
            "it_h",
            "Rebuilt from a baseline: its opener never reached this fold",
            spender="7",
            created_at="",
            status="progress",
            summary="Carried over from the pruned round-3 board",
            credits=2.19,
            credits_reported=True,
            last_report_at=ago(hours=5),
            events=[],
            events_seen=6,
        ),
    ],
}

# --------------------------------------------------------------------------- #
# board 2: a SUB-LEAD -- worker 2's own board, hanging off it_b
# --------------------------------------------------------------------------- #
B2 = {
    "id": "board-2",
    "goal": "Views: a timeline and a reporting graph",
    "round": 1,
    "parent": {
        "board": "board-1",
        "item_id": "it_b",
        "title": "Timeline and the reporting graph",
    },
    "total": 3,
    "accepted": 1,
    "open": 2,
    "needs_you": 1,
    "credits": 12.44,
    "credits_reported": True,
    "last_activity_at": ago(minutes=5),
    "tasks_omitted": 0,
    "series": [],
    "tasks": [
        task(
            "it_b1",
            "Task spans on the fold, with tests",
            spender="8",
            created_at=ago(hours=5, minutes=40),
            closed_at=ago(hours=3, minutes=10),
            state="accepted",
            status="done",
            verdict="pass",
            summary="created_at and closed_at per task; two mutations caught",
            credits=5.02,
            credits_reported=True,
            duration_ms=7_800_000,
            last_report_at=ago(hours=3, minutes=20),
            round_=1,
            events=[
                ev(ago(hours=3, minutes=10), "close", "accepted"),
                ev(ago(hours=3, minutes=20), "report", "11 tests, both mutations caught", "done"),
            ],
        ),
        task(
            "it_b2",
            "The timeline page",
            spender="9",
            created_at=ago(hours=3, minutes=5),
            status="progress",
            summary="Overlap strip, now-line and the silence tail all drawing",
            credits=4.77,
            credits_reported=True,
            duration_ms=9_600_000,
            last_report_at=ago(minutes=5),
            round_=1,
            events=[ev(ago(minutes=5), "report", "430 and wide both read", "progress")],
        ),
        task(
            "it_b3",
            "The reporting graph and the latest-run cards",
            spender="10",
            created_at=ago(hours=2, minutes=40),
            status="question",
            summary="A card cannot link to a session: the fold aliases the key on purpose. Which way?",
            credits=2.65,
            credits_reported=True,
            duration_ms=5_400_000,
            last_report_at=ago(minutes=45),
            round_=1,
            events=[
                ev(ago(minutes=45), "report", "needs a ruling on the session link", "question"),
                ev(ago(hours=2, minutes=40), "create", "created from the views plan"),
            ],
        ),
    ],
}

# --------------------------------------------------------------------------- #
# board 3: an older board, finished and mixed
# --------------------------------------------------------------------------- #
B3 = {
    "id": "board-3",
    "goal": "Feature recording: the side panel beside chat",
    "round": 2,
    "parent": None,
    "total": 7,
    "accepted": 0,
    "open": 2,
    "needs_you": 0,
    "credits": 9.3,
    "credits_reported": True,
    "last_activity_at": ago(hours=4),
    "tasks_omitted": 3,
    "series": [],
    "tasks": [
        task(
            "it_r1",
            "Record the panel",
            spender="11",
            created_at=ago(hours=10, minutes=40),
            closed_at=ago(hours=6, minutes=40),
            state="rejected",
            status="done",
            verdict="fail",
            summary="A dialog covered the feature in every take",
            credits=5.4,
            credits_reported=True,
            duration_ms=12_000_000,
            last_report_at=ago(hours=6, minutes=50),
            round_=2,
            events=[ev(ago(hours=6, minutes=40), "close", "rejected")],
        ),
        task(
            "it_r2",
            "Pick the playwright interpreter",
            spender="12",
            created_at=ago(hours=9, minutes=40),
            closed_at=ago(hours=9, minutes=10),
            state="abandoned",
            status=None,
            summary="",
            credits=0.0,
            credits_reported=False,
            round_=2,
            events=[ev(ago(hours=9, minutes=10), "close", "abandoned")],
        ),
        task(
            "it_r3",
            "Re-shoot without the dialog",
            spender="11",
            created_at=ago(hours=4, minutes=40),
            status="progress",
            summary="Second take running",
            credits=5.4,
            credits_reported=True,
            duration_ms=12_000_000,
            last_report_at=ago(hours=4, minutes=10),
            round_=2,
            events=[ev(ago(hours=4, minutes=10), "report", "take two rolling", "progress")],
        ),
        task(
            "it_r4",
            "An accept whose close stamp this fold never saw",
            spender="13",
            created_at=ago(hours=9, minutes=40),
            closed_at="",
            state="accepted",
            status="done",
            verdict="pass",
            summary="Folded from a unit whose conductor entry is not in this read",
            credits=3.9,
            credits_reported=True,
            last_report_at=ago(hours=9),
            round_=2,
            events=[],
            events_seen=4,
        ),
    ],
}

VALUE = {
    "schema": 1,
    "slot": "member-crew-lead.memory-member-crew-lead-6f1a2c",
    "items": [B1, B2, B3],
    "omitted": 1,
    "series": [],
    "unattributed": {"credits": 0.42, "credits_reported": True},
    "spenders_omitted": 2,
    "last_entry_at": ago(minutes=5),
}

DOC = {
    "note": __doc__,
    "slot": VALUE["slot"],
    "agentic": {},
    "agentic_written_at": {},
    "now": iso(NOW),
    "folds": {"workstreams": {"seq": 918, "value": VALUE}},
}

if __name__ == "__main__":
    out = (
        Path(__file__).resolve().parents[1]
        / "test/fixtures/dashboard_templates/sample_timeline.json"
    )
    out.write_text(json.dumps(DOC, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")

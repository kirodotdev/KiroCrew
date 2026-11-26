#!/usr/bin/env python3
"""Evidence the miss ledger keeps beside its rows: flaky files, main-red spans, settled passes.

`scripts/ci_test_selection_ledger.py` decides whether a failure was caused by
its pull request's diff. Three of its answers need more than the failure's own
row, and this module holds them. It reads nothing from GitHub itself.

`flake`. A file is flaky when a shard that failed it ended green on a rerun of
the same head (`rerun_green`). A failure of that file under ANOTHER `source`
within `FLAKE_DAYS` is then labelled `flake`. A rerun under the failure's own
`source` clears nothing here: a diff that makes a test flaky is that diff's
doing.

`from_main`. The watcher's main-red issue names the test files that are red on
`main`, from its first red run until it closes. A pass that runs after the
issue closed, or before it was filed, sees no open issue, so each pass writes
what the issues it listed say to the notes file and the report reads the spans
from there: a failure is `from_main` when a span names its file, its run is no
older than the span's first red run, and it was created no later than the
span's close. Only an issue the watcher wrote counts, read with the watcher's
own checks; an issue or a field that does not read as one adds no span.

`settled`. A pass reads newest runs first and stops at its budget, so the
evidence that clears a failure may sit in a run it has not reached. A pass is
settled when it left no listed run unread. On an unsettled pass the Windows
auto-off line holds what the newest settled pass said, when that pass is no
older than `SETTLED_WITHIN`; with none that recent it is judged on the rows as
they stand, so a ledger that never settles cannot keep selection on.

The notes file is JSONL beside the ledger, one object per line:

    {"kind": "main_red", "issue", "first_run", "closed", "files", "date"}
    {"kind": "pass", "date", "settled", "ok"}

`date` is when a pass last listed the issue, or ended. The file is data: every
line is checked, and a file with one line that does not read as a note, or over
`MAX_NOTES_BYTES`, is read as no notes at all. No note then labels a failure
and no pass counts as settled.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

Row = dict[str, Any]

FLAKE_DAYS = 14.0
SETTLED_WITHIN = timedelta(hours=36)
# Days a closed span is kept: the ledger's own.
SPAN_DAYS = 90
# Days a pass note is kept: several times `SETTLED_WITHIN`.
PASS_DAYS = 7
MAX_NOTES_BYTES = 8 * 1024 * 1024
MAX_SPAN_FILES = 100
# The order a row's reasons are listed in.
REASONS = ("rerun_green", "flake", "main_red", "from_main", "shared")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_FILE = re.compile(r"[A-Za-z0-9_./-]{1,300}\.py")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_stamp(value: object) -> bool:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        return False
    try:
        _when(value)
    except ValueError:
        return False
    return True


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def valid_note(row: object) -> bool:
    """Whether one parsed line of the notes file has the shape this module writes."""
    if not isinstance(row, dict) or not _is_stamp(row.get("date")):
        return False
    if row.get("kind") == "pass":
        return isinstance(row.get("settled"), bool) and (
            row.get("ok") is None or isinstance(row.get("ok"), bool)
        )
    if row.get("kind") == "main_red":
        files = row.get("files")
        return (
            _count(row.get("issue"))
            and _count(row.get("first_run"))
            and (row.get("closed") is None or _is_stamp(row.get("closed")))
            and isinstance(files, list)
            and len(files) <= MAX_SPAN_FILES
            and all(isinstance(name, str) and _FILE.fullmatch(name) for name in files)
        )
    return False


def read_notes(path: Path) -> list[Row]:
    """The notes at `path`; none when it is missing or any part of it is not a note."""
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > MAX_NOTES_BYTES:
            raise ValueError(f"over {MAX_NOTES_BYTES} bytes")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if not all(valid_note(row) for row in rows):
            raise ValueError("a line is not a note")
    except (OSError, ValueError, RecursionError) as error:
        print(f"the notes are not read, so they label nothing: {error}", file=sys.stderr)
        return []
    return rows


def write_notes(path: Path, rows: list[Row]) -> None:
    """Replace the notes file whole, so no reader sees a half-written line."""
    text = "".join(json.dumps(row, allow_nan=False, sort_keys=True) + "\n" for row in rows)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def issue_spans(issues: object, watch: Any, label: str, now: datetime) -> list[Row]:
    """A `main_red` note for each listed issue the watcher wrote, open or closed.

    `watch` is the watcher's module: its author check, title prefixes and state
    record decide what is the watcher's. An issue that fails one of them, a
    closed one with no close time, and a file name that is not a test path are
    left out.
    """
    spans: list[Row] = []
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        labels = issue.get("labels")
        if not isinstance(labels, list) or not any(
            isinstance(lab, dict) and lab.get("name") == label for lab in labels
        ):
            continue
        if not watch.is_watcher_author(issue.get("user")):
            continue
        if not str(issue.get("title", "")).startswith(watch.TITLE_PREFIXES):
            continue
        state = watch.parse_state(str(issue.get("body") or ""))
        closed = issue.get("closed_at") if issue.get("state") == "closed" else None
        if state is None or issue.get("state") not in ("open", "closed"):
            continue
        if issue.get("state") == "closed" and not _is_stamp(closed):
            continue
        files = sorted({str(nodeid).split("::", 1)[0] for nodeid in state["failures"]})
        span = {
            "kind": "main_red",
            "issue": issue.get("number"),
            "first_run": state["first_run_id"],
            "closed": closed,
            "files": [name for name in files if _FILE.fullmatch(name)][:MAX_SPAN_FILES],
            "date": stamp(now),
        }
        if valid_note(span):
            spans.append(span)
    return spans


def merge_notes(old: list[Row], fresh: list[Row], now: datetime) -> list[Row]:
    """`old` with `fresh` laid over it, less what is too old to matter.

    One span an issue: what the newest pass listed says when it started and
    closed, and the files are every file a pass saw it name, since the watcher
    rewrites the list on each red run of `main`.
    """
    spans: dict[int, Row] = {}
    passes: list[Row] = []
    for row in [*old, *fresh]:
        if row["kind"] == "pass":
            passes.append(row)
            continue
        held = spans.get(row["issue"])
        files = sorted({*(held["files"] if held else ()), *row["files"]})[:MAX_SPAN_FILES]
        spans[row["issue"]] = {**row, "files": files}
    kept = [
        span
        for _issue, span in sorted(spans.items())
        if now - _when(span["closed"] or span["date"]) <= timedelta(days=SPAN_DAYS)
    ]
    recent = [row for row in passes if now - _when(row["date"]) <= timedelta(days=PASS_DAYS)]
    return kept + sorted(recent, key=lambda row: str(row["date"]))


def pass_note(now: datetime, settled: bool, ok: bool | None) -> Row:
    return {"kind": "pass", "date": stamp(now), "settled": settled, "ok": ok}


def from_main(row: Row, notes: list[Row]) -> bool:
    """Whether a main-red span names the failure's file and holds its run."""
    return any(
        note["kind"] == "main_red"
        and row["file"] in note["files"]
        and row["run"] >= note["first_run"]
        and (note["closed"] is None or str(row["date"]) <= note["closed"])
        for note in notes
    )


def flake(row: Row, same_file: list[Row], days: float = FLAKE_DAYS) -> bool:
    """Whether another `source` failed the file and passed it on a rerun, within `days`."""
    at = _when(str(row["date"]))
    return any(
        other["rerun_green"]
        and other["source"] != row["source"]
        and abs((_when(str(other["date"])) - at).total_seconds()) <= days * 86400
        for other in same_file
    )


def reasons(row: Row) -> str:
    """The reasons a classified row is not diff-caused, as one table cell."""
    named = [key for key in REASONS if row.get(key)]
    if "main_red" in named and "from_main" in named:
        named.remove("from_main")
    return ", ".join(named)


def settle(status: Row, notes: list[Row], now: datetime) -> Row:
    """`status` with `settled`, holding the newest settled pass's answer on an unsettled one."""
    passes = sorted((n for n in notes if n["kind"] == "pass"), key=lambda n: str(n["date"]))
    out = {**status, "settled": bool(passes) and passes[-1]["settled"]}
    if out["settled"]:
        return out
    for note in reversed(passes):
        recent = now - _when(note["date"]) <= SETTLED_WITHIN
        if note["settled"] and isinstance(note["ok"], bool) and recent:
            return {**out, "windows_selection_ok": note["ok"], "held_from": note["date"]}
    return out

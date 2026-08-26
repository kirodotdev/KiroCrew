"""Fold Kiro Crew's own auto-triage pipeline into a three-level object model.

The pipeline is a chain of scheduled jobs, not a resident worker pool: a scanner
labels new issues, a triage pass classifies them, a dispatcher opens one chat
session per accepted item, that session does the work and opens the pull
request, and a cleanup pass reaps what died. This module reads the trail those
jobs already leave behind and answers three questions, each one level deeper
than the last:

* **L0 — the pipeline.** Which steps exist, how much each one has moved
  cumulatively, how much it moved recently, and how many items are in flight
  inside it right now.
* **L1 — one step.** Which items are sitting in that step, with the facts an
  operator triages on: title, labels, author, whether a human is assigned,
  how long it has waited.
* **L2 — one item.** Which agent sessions have worked it, and what each cost:
  model, turns, credits, wall time, and the pull request it produced.

READ-ONLY BY CONSTRUCTION. Nothing here writes, creates, or mutates any file,
record, or remote object. That is deliberate and load-bearing: the jobs whose
output this reads are live, and a record that merely *describes* state must
never also *authorize* execution. Every path in is a read; there is no write
helper in this module to call by accident.

The step model is this pipeline's own, spelled out rather than configurable.
Generalizing to other repositories' pipelines is a later, separate concern; a
premature configuration layer here would have to guess at a second tenant that
does not exist yet.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from kiro_crew import hooks
from kiro_crew.apps.builtins.issue_radar.backend import store as issue_radar_store
from kiro_crew.apps.manager import app_dir
from kiro_crew.config.paths import data_home
from kiro_crew.memory import workspace_dir
from kiro_crew.security import redact

logger = logging.getLogger(__name__)


class FoldError(Exception):
    """A data source could not be read safely. Carries no absolute path."""


# --------------------------------------------------------------------------
# Bounds. Every one of these exists because the inputs are files on disk that
# other programs append to concurrently, so any of them can be larger, longer
# or more hostile than the writer intended.
# --------------------------------------------------------------------------

#: Refuse an event log larger than this rather than reading it into memory.
MAX_LOG_BYTES = 64 * 1024 * 1024

#: Refuse a queue or cache file larger than this.
MAX_JSON_BYTES = 16 * 1024 * 1024

#: Highest item number we will parse out of external text.
MAX_ITEM_NUMBER = 10_000_000

#: Digit-count ceiling applied BEFORE int(), because int() on a million-digit
#: string burns CPU before any value check could reject it.
MAX_ITEM_DIGITS = 9

#: Cap on rendered collection sizes so one response cannot be unbounded.
MAX_ROWS = 2000

#: Cap on how many retired slots one item can report. ONE constant on purpose:
#: L1 and L2 answer the same real question ("how many sessions has this item
#: had"), and two different literals would let the item table and the session
#: table disagree about the same item.
MAX_SLOTS_PER_ITEM = 50

#: Default recent-throughput window.
DEFAULT_RECENT_HOURS = 24


def _bounded_int(raw: Any) -> int | None:
    """Parse an item number from untrusted text, or return None.

    ``isdecimal`` rather than ``isdigit``: ``"\u2460".isdigit()`` is True while
    ``int("\u2460")`` raises, so the laxer test would let a value through the
    guard and fail in the conversion it was meant to protect.
    """
    if isinstance(raw, bool):  # bool is an int subclass; not an item number.
        return None
    if isinstance(raw, int):
        return raw if 0 < raw <= MAX_ITEM_NUMBER else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or not text.isdecimal() or len(text) > MAX_ITEM_DIGITS:
        return None
    value = int(text)
    return value if 0 < value <= MAX_ITEM_NUMBER else None


#: Characters that must never reach a terminal or a DOM attribute as themselves:
#: C0 controls (including ESC, which starts an ANSI sequence), DEL, the C1 range,
#: and the bidirectional-override marks that can visually reorder a line.
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _printable(raw: Any, limit: int = 160) -> str:
    """Render untrusted text safely WITHOUT destroying legitimate Unicode.

    Three passes, and the ORDER of all three matters.

    First the shared redactors (`security.redact` = exfiltration URLs +
    credentials). Issue titles are not our text: they come from the forge, where
    anyone can open an issue and write anything in the title, and this fold hands
    them straight to a route that renders them in the dashboard. That makes this a
    dashboard-bound sink for attacker-controlled text, so it owes the same
    redaction every other such sink in this codebase applies -- there is one
    helper for it and this was not calling it.

    Then the control-character filter. An earlier version used ``ascii()``, which
    does neutralize control and ANSI sequences -- and also escapes every non-ASCII
    character, so a Chinese or Cyrillic issue title rendered as a wall of
    ``\\uXXXX`` and the operator could not read the title of their own issue. The
    threat is control and bidi-override characters, not non-Latin scripts.

    Truncation happens LAST, and after redaction specifically: cutting first could
    split a credential so that only its tail survives to be matched, leaving the
    head in the output. Cutting last also keeps a multi-character sequence from
    being bisected into a fragment the filter no longer recognises.
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    text = redact(text)
    return _UNSAFE_CHARS.sub("\ufffd", text)[:limit]


def _read_text_bounded(path: Path, limit: int, what: str) -> str:
    """Read a file as text through the sensitive-path gate, under a size cap.

    Routed through ``hooks.safe_read_file_bytes`` so the centralized
    ``is_sensitive_path`` check, realpath canonicalization and ``O_NOFOLLOW``
    open all apply: a symlink planted where a data file is expected cannot turn
    this into a credential read.

    The reader enforces its OWN ceiling, which is lower than this module's. Both
    halves are handled: the effective limit is the smaller of the two, and the
    reader's refusal is translated into ``FoldError`` as well. Relying on the
    comparison alone would leave a file between the two ceilings passing this
    check and then raising out of the route as an HTTP 500.
    """
    effective = min(limit, hooks.MAX_FILE_BYTES)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise FoldError(f"{what} could not be examined: {exc.strerror}") from exc
    if stat.st_size > effective:
        raise FoldError(f"{what} is larger than the {effective}-byte ceiling; refusing to read it")

    try:
        data = hooks.safe_read_file_bytes(str(path))
    except hooks.FileTooLargeError as exc:
        # Reachable if the file grows between the stat above and the read, or if
        # the reader's cap moves below ours. Either way it is a refusal to report,
        # not an unhandled crash.
        raise FoldError(f"{what} exceeded the reader's size cap") from exc
    if data is None:
        raise FoldError(f"{what} was refused by the sensitive-path gate")
    return data.decode("utf-8", errors="replace")


def _split_glued(line: str) -> tuple[list[str], bool]:
    """Split records a missing trailing newline concatenated onto one line.

    Observed in the live log: a writer appends a record without its trailing
    newline, so the NEXT writer's record lands on the same line as ``}{``. Both
    records are well-formed JSON; only the separator is missing. Dropping the
    line therefore discards real state transitions -- in the real trail this
    silently lost a ``pr_opened`` and a ``review_round_fixed``.

    Returns ``(pieces, complete)``. ``complete`` is False when the line still had
    trailing text that could not be decoded, and the pieces decoded BEFORE that
    point are returned anyway. Returning nothing in that case would lose an intact
    leading record to a torn trailing one -- and that combination is not exotic,
    it is what a glued write followed by a mid-append tail looks like, which is
    precisely the state a live append-only log is in while it is being read.

    Uses a decoder that reports where each value ended rather than splitting on
    the ``}{`` text, so a brace pair inside a string value cannot cause a bad cut.
    """
    decoder = json.JSONDecoder()
    pieces: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        while index < length and line[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            _value, end = decoder.raw_decode(line, index)
        except (ValueError, RecursionError):
            # RecursionError as well as ValueError: CPython's decoder recurses per
            # nesting level, so a deeply nested value raises RecursionError rather
            # than a JSON error. `_iter_jsonl` already catches both around its own
            # `json.loads`, but it calls THIS function outside that handler -- so a
            # RecursionError raised here escaped the fold entirely and became an
            # HTTP 500 on a log the walk is supposed to tolerate line by line.
            return pieces, False
        pieces.append(line[index:end])
        index = end
    return pieces, True


def _iter_jsonl(text: str) -> Iterator[tuple[dict[str, Any] | None, bool]]:
    """Yield ``(record, ok)`` per record.

    A torn final line is the EXPECTED state of a log another process is
    appending to, so a line that cannot be recovered yields ``(None, False)``
    and the walk continues. Refusing the whole file on one bad line would make
    this unusable against its only real input.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            recovered, complete = _split_glued(line)
            for piece in recovered:
                try:
                    sub = json.loads(piece)
                except (ValueError, RecursionError):
                    yield None, False
                    continue
                yield (sub, True) if isinstance(sub, dict) else (None, False)
            if not complete:
                # Account for the undecodable remainder exactly once, whether or
                # not anything was recovered ahead of it.
                yield None, False
            continue
        if isinstance(record, dict):
            yield record, True
        else:
            yield None, False


def _int_field(raw: Any, default: int = 0) -> int:
    """Read a plain non-negative int from an untrusted record field.

    Distinct from ``_bounded_int``, which parses an ITEM NUMBER and rejects zero:
    a counter legitimately reads zero, and rejecting it would silently turn "no
    resumes" into the default for "unreadable".
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw if 0 <= raw <= MAX_ITEM_NUMBER else default


def _dig(record: dict[str, Any], path: Sequence[str]) -> str | None:
    """Follow a nested key path to a STRING value, or return None.

    Event ``details`` is an arbitrary per-caller dict with no schema, so every
    hop tolerates a missing key, a null, or a scalar where a dict was expected
    without raising. Only a string is accepted: the discriminator compares
    against a set of routing names, and coercing a number or a bool into that
    comparison would invent an outcome the writer never recorded.
    """
    current: Any = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current if isinstance(current, str) and current else None


def _iter_strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Yield string values from a record, bounded in depth and count.

    Used instead of serializing the whole record: the URL fallback only ever
    needs to look at strings, and ``json.dumps`` on every event measured at 35%
    of the L1 fold's total time on the real trail. Depth is capped because
    ``details`` is an arbitrary caller-supplied blob and nothing guarantees it is
    shallow.
    """
    if depth > 3:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in list(value.values())[:60]:
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:60]:
            yield from _iter_strings(item, depth + 1)


#: Matches a pull-request URL tail, e.g. ".../KiroCrew/pull/5600".
_PR_URL_RE = re.compile(r"/pull/(\d{1,%d})" % MAX_ITEM_DIGITS)


def _extract_pr(record: dict[str, Any]) -> int | None:
    """Find a pull-request number in an event, whichever shape the writer used.

    The pipeline's writers do not agree on where this goes: measured on the real
    trail, 19 of 83 ``pr_opened`` events carry a top-level ``pr`` while 62 carry
    only a URL inside ``details``, and 4 carry neither. The number also appears on
    ``cleanup``, ``pr_green`` and ``implement_done``. Reading one shape off one
    event name would therefore drop most of the real associations, so this accepts
    all three and every caller applies it to every event.

    Free prose is deliberately NOT parsed. Some records spell the number only in a
    sentence ("PR 5327 head abc1234"), but the same prose also says things like
    "rebased over PR 5191" -- so a text pattern yields a confidently WRONG link,
    and an operator who clicks it acts on the wrong pull request. A missing link is
    the safer failure.
    """
    direct = _bounded_int(record.get("pr"))
    if direct is not None:
        return direct
    details = record.get("details")
    if isinstance(details, dict):
        nested = _bounded_int(details.get("pr"))
        if nested is not None:
            return nested
    for text in _iter_strings(record):
        if "/pull/" not in text:
            continue
        match = _PR_URL_RE.search(text)
        if match is not None:
            return _bounded_int(match.group(1))
    return None


def _parse_ts(raw: Any) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds, or None."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# --------------------------------------------------------------------------
# The step model — this pipeline's own five steps.
#
# `entered` names the events that mean "this step took the item on".
# `done` names the events that mean "this step delivered it onward".
# `skipped` names the events where the step deliberately declined it.
#
# One choice here is load-bearing and is NOT the obvious one. For the implement
# step, `done` is `pr_opened`, not `implement_done`. The terminal event is an
# unenforced contract on the worker session -- a session that exits early never
# logs one, and the real log shows 83 `pr_opened` against 25 `implement_done`.
# Counting terminal events would under-report delivered work by two thirds and
# make a working pipeline look broken.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StepSpec:
    key: str
    label: str
    entered: frozenset[str]
    done: frozenset[str]
    skipped: frozenset[str]
    #: Events that are PROGRESS INSIDE the step, neither an entry nor an exit:
    #: a gate run, a review round, a push answering one. Kept distinct from
    #: ``skipped`` because "78 gate runs" and "78 items declined" are opposite
    #: facts and a view that conflates them misreports a working step as a
    #: rejecting one.
    churn: frozenset[str] = frozenset()
    #: Some steps record their decision in a FIELD rather than in the event
    #: name -- triage logs one ``triage`` event per item and puts the routing in
    #: ``details.classification``. Without this, every triaged item looks like it
    #: entered and never left, and the largest number in the view would be a
    #: phantom backlog of items that were in fact resolved at triage.
    outcome_field: tuple[str, ...] = ()
    #: Values of ``outcome_field`` that mean "handed onward to the next step".
    #: Any other value is an exit OUT of the pipeline at this step.
    outcome_forward: frozenset[str] = frozenset()
    #: True when this step opens an agent session per item, so its throughput
    #: is counted in sessions rather than issues.
    session_bearing: bool = False


STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="scan",
        label="Scan",
        entered=frozenset({"scan"}),
        done=frozenset({"label"}),
        skipped=frozenset({"scan_skip_claimed", "skip", "dedupe"}),
    ),
    StepSpec(
        key="triage",
        label="Triage",
        entered=frozenset({"triage"}),
        done=frozenset({"implement_queued"}),
        skipped=frozenset({"answer"}),
        # Triage logs ONE event per item and records where it sent it.
        # `auto-fixable` is the only value that continues into dispatch; every
        # other value hands the item back to a human, which is an exit, not a
        # queue. Measured on the real trail: 102 auto-fixable against 290 routed
        # out (needs-investigation, needs-human, skipped-has-pr, already-fixed).
        outcome_field=("details", "classification"),
        outcome_forward=frozenset({"auto-fixable"}),
    ),
    StepSpec(
        key="dispatch",
        label="Dispatch",
        entered=frozenset({"implement_queued"}),
        done=frozenset({"implement_start", "implement_resume"}),
        skipped=frozenset(
            {
                "dispatch_skip_open_pr_exists",
                "dispatch_skip_resume_session_live",
                "dispatch_skip_issue_closed",
                "dispatch_jitter",
                "dispatch_error",
                "backoff",
                # Reaped: see the implement step below for why a cleanup enter has
                # to exit every working step, not only the one it was reported on.
                "cleanup",
                "cleanup_sweep",
            }
        ),
    ),
    StepSpec(
        key="implement",
        label="Implement",
        entered=frozenset({"implement_start", "implement_resume"}),
        done=frozenset({"pr_opened"}),
        #: `cleanup`/`cleanup_sweep` are EXITS here, not just entries to the cleanup
        #: step. Without them an item that started implementing and was then reaped
        #: had no transition out: it stayed in this step's in-flight count while the
        #: cleanup step counted it too, so a retired item sat in the live backlog and
        #: was double-counted across two steps. Measured on the real trail: 6 of the
        #: 27 items this step reported as in-flight had a cleanup as their LAST event.
        #:
        #: Applied to dispatch and verify as well. Those measure zero reaped items
        #: today, but the gap is identical and would appear the first time cleanup
        #: fires on an item sitting in either -- fixing only the step the finding
        #: named would leave the same defect in its siblings.
        skipped=frozenset({"implement_fail", "escalate", "cleanup", "cleanup_sweep"}),
        session_bearing=True,
    ),
    StepSpec(
        key="verify",
        label="Verify",
        entered=frozenset({"pr_opened"}),
        done=frozenset({"pr_green"}),
        # Reaped: same reasoning as the implement step above.
        skipped=frozenset({"cleanup", "cleanup_sweep"}),
        churn=frozenset(
            {
                "review_round",
                "review_round_fixed",
                "review_done",
                "gates_green",
                "gates_pass",
                "push",
                "ci",
            }
        ),
        session_bearing=True,
    ),
    StepSpec(
        key="cleanup",
        label="Cleanup",
        entered=frozenset({"cleanup", "cleanup_sweep"}),
        done=frozenset({"implement_done", "cleanup_work_already_complete"}),
        skipped=frozenset(
            {"cleanup_skip_needs_human", "cleanup_resume_requested", "cleanup_scratch"}
        ),
    ),
)

STEP_BY_KEY: dict[str, StepSpec] = {s.key: s for s in STEPS}

#: Every event name the step table classifies, for coverage reporting.
MAPPED_EVENTS: frozenset[str] = frozenset(
    name for s in STEPS for name in (s.entered | s.done | s.skipped | s.churn)
)


# --------------------------------------------------------------------------
# Source resolution
# --------------------------------------------------------------------------

AUDIT_LOG_NAME = "gh-autofix-audit.jsonl"
QUEUE_NAME = "gh-autofix-dispatch-queue.jsonl"


def _workspace() -> Path:
    return workspace_dir()


def audit_log_path(root: Path | None = None) -> Path:
    return (root if root is not None else _workspace()) / AUDIT_LOG_NAME


def queue_path(root: Path | None = None) -> Path:
    return (root if root is not None else _workspace()) / QUEUE_NAME


# --------------------------------------------------------------------------
# L0 — the pipeline
# --------------------------------------------------------------------------


@dataclass
class StepCounts:
    key: str
    label: str
    session_bearing: bool
    entered: int = 0
    done: int = 0
    skipped: int = 0
    churn: int = 0
    recent_entered: int = 0
    recent_done: int = 0
    #: Distinct items that entered this step and have not yet left it.
    in_flight: int = 0
    #: Distinct items ever admitted / delivered. Kept separate from the event
    #: counts above because they answer different questions: the event count is
    #: WORK PERFORMED (a retried item is started twice and that is two units of
    #: work), the distinct count is THROUGHPUT (it is still one item). On the
    #: real trail the implement step shows 197 start events across 113 distinct
    #: items -- 1.74 starts each -- so collapsing the two would either hide the
    #: rework or overstate the delivery.
    distinct_entered: int = 0
    distinct_done: int = 0
    #: For a field-routed step, how many items went each way.
    routed: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": "sessions" if self.session_bearing else "issues",
            "entered": self.entered,
            "done": self.done,
            "skipped": self.skipped,
            "churn": self.churn,
            "recentEntered": self.recent_entered,
            "recentDone": self.recent_done,
            "inFlight": self.in_flight,
            "distinctEntered": self.distinct_entered,
            "distinctDone": self.distinct_done,
            "routed": [
                {"outcome": name, "count": count}
                for name, count in sorted(self.routed.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }


@dataclass
class PipelineFold:
    steps: list[StepCounts] = field(default_factory=list)
    total_events: int = 0
    unparseable: int = 0
    unmapped: dict[str, int] = field(default_factory=dict)
    first_event_at: float | None = None
    last_event_at: float | None = None
    recent_hours: int = DEFAULT_RECENT_HOURS

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "totalEvents": self.total_events,
            "unparseable": self.unparseable,
            "unmappedEvents": [
                {"event": name, "count": count}
                for name, count in sorted(self.unmapped.items(), key=lambda kv: (-kv[1], kv[0]))[
                    :40
                ]
            ],
            "firstEventAt": self.first_event_at,
            "lastEventAt": self.last_event_at,
            "recentHours": self.recent_hours,
        }


def fold_pipeline(
    *,
    root: Path | None = None,
    recent_hours: int = DEFAULT_RECENT_HOURS,
    now: float | None = None,
) -> PipelineFold:
    """Fold the whole event trail into per-step throughput.

    ``recent_hours`` bounds the "moved lately" counters so the view can
    distinguish a pipeline that is running now from one that ran last week and
    stopped. It is CLAMPED into ``[1, 2160]`` rather than validated: a caller
    asking for an impossible window gets the nearest real one instead of an
    error, because this is a display parameter and a broken query string should
    not blank the page. Note that clamping means there is no "all time" value --
    the cumulative counters already answer that, and ``recent`` is only ever a
    window.
    """
    recent_hours = max(1, min(int(recent_hours or DEFAULT_RECENT_HOURS), 24 * 90))
    clock = time.time() if now is None else now
    cutoff = clock - recent_hours * 3600

    text = _read_text_bounded(audit_log_path(root), MAX_LOG_BYTES, "The pipeline event log")

    counts = {s.key: StepCounts(s.key, s.label, s.session_bearing) for s in STEPS}
    result = PipelineFold(recent_hours=recent_hours)
    # Per step: every item that ever entered, every item ever DELIVERED onward, and
    # -- separately -- whether each item is currently INSIDE the step.
    #
    # The last of those cannot be a set difference. `entered - left` is blind to
    # order, so an item that entered, exited, and entered AGAIN stayed marked as
    # gone forever. Re-entry is not an edge case in this pipeline: the implement
    # step logs 197 starts across 113 distinct items, so most items that are
    # re-worked would have been missing from the in-flight count. The event trail is
    # chronological, so the honest reading is the LAST transition per (step, item).
    entered_items: dict[str, set[int]] = {s.key: set() for s in STEPS}
    done_items: dict[str, set[int]] = {s.key: set() for s in STEPS}
    inside: dict[str, dict[int, bool]] = {s.key: {} for s in STEPS}

    for record, ok in _iter_jsonl(text):
        if not ok or record is None:
            result.unparseable += 1
            continue
        result.total_events += 1
        name = record.get("event")
        if not isinstance(name, str) or not name:
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is not None:
            if result.first_event_at is None or ts < result.first_event_at:
                result.first_event_at = ts
            if result.last_event_at is None or ts > result.last_event_at:
                result.last_event_at = ts
        number = _bounded_int(record.get("issue"))
        recent = ts is not None and ts >= cutoff

        if name not in MAPPED_EVENTS:
            result.unmapped[_printable(name, 80)] = result.unmapped.get(_printable(name, 80), 0) + 1
            continue

        for spec in STEPS:
            bucket = counts[spec.key]
            if name in spec.entered:
                bucket.entered += 1
                if recent:
                    bucket.recent_entered += 1
                if number is not None:
                    entered_items[spec.key].add(number)
                    # Re-entry puts the item back INSIDE, which is why this is an
                    # assignment rather than adding to a set it could never leave.
                    inside[spec.key][number] = True
                # A step that records its routing in a field decides the item's
                # fate on the SAME event that admitted it, so resolve it here
                # rather than waiting for an exit event that will never come.
                if spec.outcome_field:
                    value = _dig(record, spec.outcome_field)
                    if value is not None:
                        label = _printable(value, 60)
                        bucket.routed[label] = bucket.routed.get(label, 0) + 1
                        if label not in spec.outcome_forward and number is not None:
                            inside[spec.key][number] = False
            if name in spec.done:
                bucket.done += 1
                if recent:
                    bucket.recent_done += 1
                if number is not None:
                    inside[spec.key][number] = False
                    done_items[spec.key].add(number)
            if name in spec.skipped:
                bucket.skipped += 1
                if number is not None:
                    inside[spec.key][number] = False
            if name in spec.churn:
                bucket.churn += 1

    for spec in STEPS:
        bucket = counts[spec.key]
        bucket.in_flight = sum(1 for still_in in inside[spec.key].values() if still_in)
        bucket.distinct_entered = len(entered_items[spec.key])
        bucket.distinct_done = len(done_items[spec.key])

    result.steps = [counts[s.key] for s in STEPS]
    return result


# --------------------------------------------------------------------------
# L1 — the items inside one step
# --------------------------------------------------------------------------


@dataclass
class ItemRow:
    """One issue as it appears in a step's table.

    Carries the item's identity, its timing and its latest event NAME -- not its
    whole trail. The trail used to ship here for an expanded-row phase strip that
    was removed: a strip of the pipeline's internal event names answers a question
    this level does not ask, and it pushed the cost table below the fold. Shipping
    a field with no renderer is not free either -- up to 200 events per item across
    up to 2000 items -- so the field went out with the view that wanted it. Item
    relationships belong to a dependency view, which Issue Radar already owns.
    """

    number: int
    title: str = ""
    labels: list[str] = field(default_factory=list)
    author: str = ""
    assignees: list[str] = field(default_factory=list)
    # None means "the cache has no answer", which is NOT the same fact as zero
    # comments. The neighbouring labels/assignees already render as "Not cached" for
    # exactly this reason; comments defaulted to 0 and so asserted that an issue
    # nobody has cached has no discussion on it.
    comments: int | None = None
    queued_at: float | None = None
    dispatched_at: float | None = None
    resume_count: int = 0
    slot: str = ""
    previous_slots: list[str] = field(default_factory=list)
    withdrawn: bool = False
    needs_human: bool = False
    pr: int | None = None
    last_event: str = ""
    last_event_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "labels": self.labels,
            "author": self.author,
            "assignees": self.assignees,
            "comments": self.comments,
            "queuedAt": self.queued_at,
            "dispatchedAt": self.dispatched_at,
            "resumeCount": self.resume_count,
            "slot": self.slot,
            "previousSlots": self.previous_slots,
            "withdrawn": self.withdrawn,
            "needsHuman": self.needs_human,
            "pr": self.pr,
            "lastEvent": self.last_event,
            "lastEventAt": self.last_event_at,
        }


def _read_queue(root: Path | None = None) -> dict[int, dict[str, Any]]:
    """Read the dispatch queue into ``{issue: entry}``.

    Later lines win: the queue is rewritten in place by the dispatcher, and a
    torn or duplicated entry should resolve to the most recent statement about
    that item rather than the first one seen.
    """
    text = _read_text_bounded(queue_path(root), MAX_JSON_BYTES, "The dispatch queue")
    out: dict[int, dict[str, Any]] = {}
    for record, ok in _iter_jsonl(text):
        if not ok or record is None:
            continue
        number = _bounded_int(record.get("issue"))
        if number is None:
            continue
        out[number] = record
    return out


def _issue_cache_dir(owner: str, repo: str) -> Path:
    """Locate the issue cache WITHOUT creating anything.

    Both obvious helpers create as a side effect: Issue Radar's ``repo_data_dir``
    calls ``mkdir(parents=True)``, and so does ``app_data_dir`` one level up. A GET
    for a repository that has never been cached would therefore leave directory
    trees behind -- and this module's whole contract is that it writes nothing, so
    a read path that quietly creates would make that claim false.

    ``app_dir`` is pure path composition, which is why the ``data`` segment is
    restated here rather than borrowed from a helper that would materialise it.
    """
    return app_dir(issue_radar_store.APP_NAME) / "data" / "repos" / owner / repo


def _read_issue_cache(number: int, owner: str, repo: str) -> dict[str, Any]:
    """Read one cached issue's DETAIL, or {} when absent or unreadable.

    The cache file is a wrapper -- ``{owner, repo, number, detail, timeline}`` --
    and every field this view wants (title, labels, assignees, author, comments)
    lives under ``detail``. Returning the wrapper silently discarded all of it:
    ``labels`` and ``assignees`` were then always absent, which rendered as "not
    cached" for issues that were in fact fully cached, and made a real defect look
    like a property of the data.

    A missing entry is still normal -- the cache is populated by Issue Radar on
    demand, so an item nobody has opened has no local copy. Degrading to empty
    keeps the row present with the facts we do have instead of dropping a real
    pipeline item because its title is unknown.
    """
    path = _issue_cache_dir(owner, repo) / f"issue-{number}.json"
    try:
        text = _read_text_bounded(path, MAX_JSON_BYTES, "An issue cache entry")
    except FoldError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    detail = data.get("detail")
    # Tolerate both shapes: the wrapper is what the cache writes today, but a
    # flat record should not be thrown away if the writer ever changes.
    return detail if isinstance(detail, dict) else data


def _comment_count(raw: Any) -> int | None:
    """Comment count from the cache, or None when the cache has no answer.

    A list is counted; a number is taken as given; ANYTHING ELSE -- absent, null, a
    string -- yields None rather than 0, because "we never cached this issue" and
    "this issue has no comments" are different facts and only one of them is ours to
    assert. A present, genuine zero survives as 0.
    """
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw == raw and raw not in (float("inf"), float("-inf")):
        return int(raw)
    return None


def _names(raw: Any) -> list[str]:
    """Extract login/name strings from a GitHub-shaped list or scalar."""
    out: list[str] = []
    if isinstance(raw, dict):
        raw = [raw]
    # A bare string is ONE name, not a non-answer. `author` is cached as a scalar
    # login, so returning [] here discarded it and the row rendered "Not cached"
    # for an author that WAS cached -- the same class of error as reading the
    # cache's outer wrapper instead of its `detail`: our own defect displayed as
    # missing data. The docstring already claimed to accept a scalar; the body
    # did not.
    elif isinstance(raw, str):
        raw = [raw] if raw else []
    if not isinstance(raw, list):
        return out
    for entry in raw[:50]:
        if isinstance(entry, str):
            value = entry
        elif isinstance(entry, dict):
            value = entry.get("login") or entry.get("name") or ""
        else:
            continue
        if isinstance(value, str) and value:
            out.append(_printable(value, 80))
    return out


def list_step_items(
    step: str,
    *,
    owner: str,
    repo: str,
    root: Path | None = None,
    limit: int = MAX_ROWS,
) -> list[ItemRow]:
    """Return the items currently sitting in ``step``.

    "Currently sitting in" means the item entered this step and no event has
    been observed taking it out -- the same in-flight relation L0 counts, so the
    number on the step card and the length of this list cannot disagree.
    """
    spec = STEP_BY_KEY.get(step)
    if spec is None:
        raise FoldError(f"unknown pipeline step: {_printable(step, 40)}")
    limit = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))

    text = _read_text_bounded(audit_log_path(root), MAX_LOG_BYTES, "The pipeline event log")
    entered: set[int] = set()
    # Whether each item is currently INSIDE this step, by its LAST transition. Not a
    # set of departures: an item that entered, exited and entered again would stay
    # marked as gone, and this pipeline re-works items routinely. Must agree with
    # the in-flight count L0 reports, or the number on the step card and the length
    # of this list would disagree about the same relation.
    inside: dict[int, bool] = {}
    latest: dict[int, tuple[str, float | None]] = {}
    pr_of: dict[int, int] = {}

    for record, ok in _iter_jsonl(text):
        if not ok or record is None:
            continue
        name = record.get("event")
        if not isinstance(name, str) or not name:
            continue
        number = _bounded_int(record.get("issue"))
        if number is None:
            continue
        ts = _parse_ts(record.get("ts"))
        safe_name = _printable(name, 60)
        latest[number] = (safe_name, ts)

        pr_number = _extract_pr(record)
        if pr_number is not None:
            pr_of[number] = pr_number

        if name in spec.entered:
            entered.add(number)
            inside[number] = True
            if spec.outcome_field:
                value = _dig(record, spec.outcome_field)
                if value is not None and _printable(value, 60) not in spec.outcome_forward:
                    inside[number] = False
        if name in spec.done or name in spec.skipped:
            inside[number] = False

    resident = sorted((n for n, still_in in inside.items() if still_in), reverse=True)[:limit]
    queue = _read_queue(root)
    rows: list[ItemRow] = []
    for number in resident:
        entry = queue.get(number, {})
        cached = _read_issue_cache(number, owner, repo)
        last_name, last_ts = latest.get(number, ("", None))
        rows.append(
            ItemRow(
                number=number,
                title=_printable(cached.get("title") or entry.get("title") or "", 200),
                labels=_names(cached.get("labels")),
                author=(_names(cached.get("author")) or [""])[0],
                assignees=_names(cached.get("assignees")),
                comments=_comment_count(cached.get("comments")),
                queued_at=_parse_ts(entry.get("queued_at")),
                dispatched_at=_parse_ts(entry.get("dispatched_at")),
                resume_count=_int_field(entry.get("resume_count")),
                slot=_printable(entry.get("slot") or "", 80),
                previous_slots=(
                    [
                        _printable(s, 80)
                        for s in entry.get("previous_slots", [])[:MAX_SLOTS_PER_ITEM]
                    ]
                    if isinstance(entry.get("previous_slots"), list)
                    else []
                ),
                withdrawn=bool(entry.get("withdrawn")),
                needs_human=bool(entry.get("needs_human")),
                pr=pr_of.get(number),
                last_event=last_name,
                last_event_at=last_ts,
            )
        )
    return rows


# --------------------------------------------------------------------------
# L2 — the sessions that worked one item
# --------------------------------------------------------------------------

#: Hard ceiling on how many usage shards one fold will open -- a SAFETY bound, not
#: a reporting window.
#:
#: This was a 30-day window whose comment claimed shards are retained for 30 days so
#: "scanning more buys nothing". That premise is false: a real installation was
#: holding 37 shards. The L2 table's whole point is the total summed ACROSS RETRIES,
#: and an item can be reworked for weeks -- so a window silently turned "what this
#: item cost" into "what it cost recently", under-reporting exactly the long-running
#: items whose cost matters most. Truncating a total without telling the reader is
#: the failure mode this feature refuses elsewhere; it should not have been here.
#:
#: The bound is now whatever the writer retains, and this ceiling only stops an
#: unbounded read if retention ever grows without limit. Each shard read is itself
#: byte-bounded, so the cost is linear in retained days.
MAX_USAGE_SHARDS = 400

#: Numeric usage fields summed per session.
USAGE_SUMS = ("input", "output", "cache_create", "cache_read", "cost", "credits", "turns")


@dataclass
class SessionRow:
    """One agent session that worked an item, with what it cost.

    A resumed item has SEVERAL sessions: the dispatcher retires a dead slot by
    pushing its key into ``previous_slots`` and creating a new one, so an item's
    total spend is the sum over all of them. Reporting only the current slot
    would under-count every retried item -- and retried items are the expensive
    ones, which is exactly what an operator is looking for.
    """

    slot: str
    model: str = ""
    agent: str = ""
    surface: str = ""
    current: bool = False
    started_at: float | None = None
    last_at: float | None = None
    rows: int = 0
    input: float = 0.0
    output: float = 0.0
    cache_create: float = 0.0
    cache_read: float = 0.0
    cost: float = 0.0
    credits: float = 0.0
    turns: float = 0.0
    duration_ms: float = 0.0
    context_used: float = 0.0
    context_window: float = 0.0
    last_phase: str = ""
    last_stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "model": self.model,
            "agent": self.agent,
            "surface": self.surface,
            "current": self.current,
            "startedAt": self.started_at,
            "lastAt": self.last_at,
            # The usage endpoint's contract is one row per turn, so the row count IS
            # the turn count, and it ships under ONE name. An earlier version sent
            # `rows` alongside an identical `turns`, which is the same mistake as the
            # `rawTurns` key removed before it: two keys for one number invite a
            # consumer to believe they measure different things.
            "turns": self.rows,
            "input": self.input,
            "output": self.output,
            "cacheCreate": self.cache_create,
            "cacheRead": self.cache_read,
            "cost": self.cost,
            "credits": self.credits,
            "durationMs": self.duration_ms,
            "contextUsed": self.context_used,
            "contextWindow": self.context_window,
            "lastPhase": self.last_phase,
            "lastStopReason": self.last_stop_reason,
        }


def _usage_dir() -> Path:
    return data_home() / "usage" / "tokens"


def _usage_shards(limit: int = MAX_USAGE_SHARDS) -> list[Path]:
    """Return every retained usage shard, newest first, capped at ``limit``.

    Shard names are local dates, so the set is derived from the filename rather
    than from mtime: a shard rewritten later still belongs to its own day.

    Every retained shard is read, not a recent slice of them -- see
    ``MAX_USAGE_SHARDS`` for why a window was wrong here.
    """
    directory = _usage_dir()
    try:
        entries = sorted(directory.glob("*.jsonl"), reverse=True)
    except OSError:
        return []
    return entries[: max(1, min(limit, MAX_USAGE_SHARDS))]


def _number(raw: Any) -> float:
    """Coerce a usage field to a finite float, or 0.0.

    A row whose field is null, a string, or a NaN/inf must not poison a sum: the
    total is displayed as money and turns, and one bad row silently turning a
    session's cost into NaN would make the whole table unreadable.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return value


def list_item_sessions(
    number: int,
    *,
    root: Path | None = None,
    shard_limit: int = MAX_USAGE_SHARDS,
) -> list[SessionRow]:
    """Return every session that worked item ``number``, newest first.

    Slots come from the dispatch queue entry -- the current ``slot`` plus every
    retired key in ``previous_slots`` -- and the per-turn usage shards supply the
    cost. An item with no queue entry has no sessions to report, which is a real
    answer rather than an error: the early pipeline steps run as batch jobs and
    open no session at all.
    """
    item = _bounded_int(number)
    if item is None:
        raise FoldError("item number out of range")

    entry = _read_queue(root).get(item, {})
    current_slot = entry.get("slot")
    current_slot = current_slot if isinstance(current_slot, str) and current_slot else ""
    previous = entry.get("previous_slots")
    previous_list = (
        [s for s in previous if isinstance(s, str) and s][:MAX_SLOTS_PER_ITEM]
        if isinstance(previous, list)
        else []
    )
    wanted = {
        s: (s == current_slot) for s in ([current_slot] if current_slot else []) + previous_list
    }
    if not wanted:
        return []

    sessions: dict[str, SessionRow] = {
        slot: SessionRow(slot=_printable(slot, 80), current=is_current)
        for slot, is_current in wanted.items()
    }

    # OLDEST first. The shard list is newest-first because that is how the window
    # is capped, but the identity fields below are last-wins, so iterating in that
    # order let the OLDEST shard have the final say and a resumed session reported
    # the model and phase it started on weeks ago rather than its current ones.
    for shard in reversed(_usage_shards(shard_limit)):
        # An unreadable shard PROPAGATES rather than being skipped. Skipping it
        # dropped that day's rows from a figure presented as the lifetime total, so
        # the operator read a smaller number with nothing saying it was partial --
        # the same defect as the 30-shard window removed above, arriving by a
        # different route. This feature refuses that trade everywhere else (the
        # oversized-audit-log refusal is deliberate for the same reason): an honest
        # error the reader can see beats a total whose definition quietly changed.
        text = _read_text_bounded(shard, MAX_LOG_BYTES, "A usage shard")
        for record, ok in _iter_jsonl(text):
            if not ok or record is None:
                continue
            # Only per-turn TOKEN records. The shard is a mixed log and every
            # reader in the owning module gates on this discriminator at each of
            # its five read sites; omitting it here let any other record that
            # happens to carry a `slot` field add its numbers to a session's
            # totals. Credits is the headline figure of this table, so a polluted
            # sum is not a cosmetic error -- it is the wrong answer to the one
            # question the table exists to answer.
            if record.get("_type") != "tokens":
                continue
            slot = record.get("slot")
            if not isinstance(slot, str) or slot not in sessions:
                continue
            row = sessions[slot]
            row.rows += 1
            row.input += _number(record.get("input"))
            row.output += _number(record.get("output"))
            row.cache_create += _number(record.get("cache_create"))
            row.cache_read += _number(record.get("cache_read"))
            row.cost += _number(record.get("cost"))
            row.credits += _number(record.get("credits"))
            row.turns += _number(record.get("turns"))
            row.duration_ms += _number(record.get("duration_ms"))
            # Identity and context are LAST-WINS rather than summed: a model or a
            # window is a property of the session, not a quantity, and summing a
            # context window across turns would report a nonsense number.
            for attr, key in (
                ("model", "model"),
                ("agent", "agent"),
                ("surface", "surface"),
                ("last_phase", "phase"),
                ("last_stop_reason", "stop_reason"),
            ):
                value = record.get(key)
                if isinstance(value, str) and value:
                    setattr(row, attr, _printable(value, 80))
            row.context_used = _number(record.get("context_used")) or row.context_used
            row.context_window = _number(record.get("context_window")) or row.context_window
            ts = _parse_ts(record.get("ts"))
            if ts is not None:
                if row.started_at is None or ts < row.started_at:
                    row.started_at = ts
                if row.last_at is None or ts > row.last_at:
                    row.last_at = ts

    ordered = sorted(
        sessions.values(),
        key=lambda r: (r.current, r.last_at or 0.0),
        reverse=True,
    )
    return ordered


#: Numeric session columns a view might render, in display order.
SESSION_NUMERIC_COLUMNS = (
    "credits",
    "durationMs",
    "contextUsed",
    "input",
    "output",
    "cacheCreate",
    "cacheRead",
    "cost",
)


def populated_columns(rows: Sequence[SessionRow]) -> list[str]:
    """Name the numeric columns that carry a non-zero value in ``rows``.

    Several usage fields are structurally zero on every row today (tokens, cost
    and the per-row ``turns``), so a table that renders them prints a column of
    zeros beside a real credit total and invites the reader to believe the work
    was free. Deciding this here rather than in the view keeps it testable, and
    makes the column appear on its own the day the writers start populating it.
    """
    serialized = [r.to_dict() for r in rows]
    return [
        column
        for column in SESSION_NUMERIC_COLUMNS
        if any(_number(row.get(column)) != 0.0 for row in serialized)
    ]

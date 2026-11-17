"""``kirocrew trace show``: a waterfall of one traced turn from the local span files.

Reads the shards ``tracing/exporter.py`` writes (``<data home>/traces/spans-*.jsonl``)
and prints one trace as an indented tree with a time bar per span and a line for
each stretch where nothing under a parent was running. It writes nothing, needs no
gateway and no OpenTelemetry SDK, and prints only the span name, its timing and the
``error.type`` attribute: span attributes are allowlisted in ``correlation.py`` and
never hold prompt or tool content, and every name still passes
``safe_terminal_line`` because the file is data, not our own output.

Reads are bounded: a shard over :data:`MAX_SHARD_BYTES` is skipped with a warning, a
line over :data:`MAX_LINE_CHARS` or one that is not a span counts toward ONE warning
per shard, and :data:`MAX_SPANS` spans (newest shards first) are kept in all.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.terminal_safe import safe_terminal_line

MAX_SHARD_BYTES = 50 * 1024 * 1024
MAX_LINE_CHARS = 256 * 1024
MAX_SPANS = 100_000
MAX_ROWS = 500
MAX_DEPTH = 24
MAX_KEY_CHARS = 256
_BAR_WIDTH = 30
_NAME_WIDTH = 46
#: The attributes that name a turn or a session, besides the trace id itself.
_KEY_ATTRS = (
    "kirocrew.turn_id",
    "kirocrew.acp_session_id",
    "kirocrew.local.session_key",
    "kirocrew.session_key_hash",
    "session.id",
)


class Span(NamedTuple):
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    start: float
    end: float
    error: str  # "" when the span did not fail, else its error.type (or "error")
    keys: frozenset[str]


def _hex(value: object) -> str:
    """An id's lowercase hex digits, or ``""`` when *value* is absent or not a hex id."""
    text = value.removeprefix("0x").lower() if isinstance(value, str) else ""
    return text if len(text) <= 32 and all(c in "0123456789abcdef" for c in text) else ""


def _epoch(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_span(raw: object) -> Span | None:
    """One ``ReadableSpan.to_json`` record as a :class:`Span`, or ``None`` if it is not one."""
    if not isinstance(raw, dict):
        return None
    ctx, attrs, status = raw.get("context"), raw.get("attributes"), raw.get("status")
    if not isinstance(ctx, dict) or not isinstance(raw.get("name"), str):
        return None
    start, end = _epoch(raw.get("start_time")), _epoch(raw.get("end_time"))
    trace_id, span_id = _hex(ctx.get("trace_id")), _hex(ctx.get("span_id"))
    if start is None or end is None or not trace_id or not span_id:
        return None
    attrs = attrs if isinstance(attrs, dict) else {}
    failed = isinstance(status, dict) and status.get("status_code") == "ERROR"
    kind = attrs.get("error.type")
    return Span(
        trace_id,
        span_id,
        _hex(raw.get("parent_id")),
        raw["name"],
        start,
        max(start, end),
        (kind if isinstance(kind, str) and kind else "error") if failed else "",
        frozenset(v for k in _KEY_ATTRS if isinstance(v := attrs.get(k), str)),
    )


def _warn(text: str) -> None:
    print(f"Warning: {safe_terminal_line(text)}", file=sys.stderr)


def read_spans(directory: Path) -> list[Span]:
    """Every span in the shards under *directory*, newest shards first, bounded.

    A missing directory is no spans. An unreadable shard stops the run, so a
    lookup never answers "nothing matched" for data it could not read.
    """
    from kiro_crew.hooks import safe_read_file
    from kiro_crew.metrics.local_exporter import shard_name_re

    shard = shard_name_re("spans")
    try:
        names = sorted((p for p in directory.iterdir() if shard.fullmatch(p.name)), reverse=True)
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(f"Error: cannot read the traces folder: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    spans: list[Span] = []
    for path in names:
        if path.is_dir():
            continue
        try:
            if path.stat().st_size > MAX_SHARD_BYTES:
                _warn(f"skipped {path.name}: larger than {MAX_SHARD_BYTES // 2**20} MB")
                continue
            text = safe_read_file(str(path))
        except FileNotFoundError:
            continue  # pruned between the listing and the read
        except (OSError, UnicodeDecodeError) as exc:
            print(
                f"Error: cannot read {safe_terminal_line(path.name)}: {type(exc).__name__}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        bad = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                span = parse_span(json.loads(line)) if len(line) <= MAX_LINE_CHARS else None
            except (ValueError, RecursionError):  # not JSON, or nested past the parser's limit
                span = None
            if span is None:
                bad += 1
            elif len(spans) < MAX_SPANS:
                spans.append(span)
        if bad:
            _warn(f"skipped {bad} line(s) in {path.name} that are not spans")
        if len(spans) >= MAX_SPANS:
            _warn(f"stopped at {MAX_SPANS} spans; older shards were not read")
            break
    return spans


def pick_trace(spans: list[Span], key: str) -> tuple[list[Span], int]:
    """The most recent trace holding *key* (a trace id, turn id or session id) and how many others."""
    key = key.strip().removeprefix("0x")
    hits = {s.trace_id for s in spans if s.trace_id == key or key in s.keys}
    if not hits or not key or len(key) > MAX_KEY_CHARS:
        return [], 0
    first: dict[str, float] = {}
    for s in spans:
        if s.trace_id in hits:
            first[s.trace_id] = min(first.get(s.trace_id, s.start), s.start)
    chosen = max(first, key=lambda t: first[t])
    return [s for s in spans if s.trace_id == chosen], len(hits) - 1


def _duration(seconds: float) -> str:
    if seconds < 0.001:
        return "<1ms"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"


def _bar(start: float, end: float, t0: float, total: float, fill: str) -> str:
    first = min(_BAR_WIDTH - 1, int((start - t0) / total * _BAR_WIDTH))
    last = min(_BAR_WIDTH, max(first + 1, round((end - t0) / total * _BAR_WIDTH)))
    return " " * first + fill * (last - first) + " " * (_BAR_WIDTH - last)


def render(spans: list[Span]) -> list[str]:
    """The waterfall of one trace's *spans*, one string per row."""
    by_id = {s.span_id for s in spans}
    kids: dict[str, list[Span]] = {}
    for s in sorted(spans, key=lambda s: (s.start, s.end)):
        kids.setdefault(s.parent_id if s.parent_id in by_id else "", []).append(s)
    t0, t1 = min(s.start for s in spans), max(s.end for s in spans)
    total = max(t1 - t0, 1e-6)
    gap_min = max(0.001, total * 0.02)
    stamp = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = [f"trace {spans[0].trace_id}  {len(spans)} spans  {_duration(total)}  {stamp}"]
    # Explicit stack: a span is (Span, depth); a gap is (None, depth, start, end).
    stack: list[tuple[Any, ...]] = [(s, 0) for s in reversed(kids.get("", []))]
    drawn = 0
    while stack and len(rows) <= MAX_ROWS:
        item = stack.pop()
        pad = "  " * min(item[1], MAX_DEPTH)
        if item[0] is None:
            _, _, start, end = item
            label = f"{pad}· gap {_duration(end - start)}"
            rows.append(
                f"{label:<{_NAME_WIDTH}} {_duration(start - t0):>7}  {_bar(start, end, t0, total, '.')}"
            )
            continue
        span = item[0]
        drawn += 1
        label = safe_terminal_line(f"{pad}{span.name}")[:_NAME_WIDTH]
        tail = f"  ! {safe_terminal_line(span.error)}" if span.error else ""
        rows.append(
            f"{label:<{_NAME_WIDTH}} {_duration(span.start - t0):>7} {_duration(span.end - span.start):>7}  "
            f"{_bar(span.start, span.end, t0, total, '#')}{tail}"
        )
        pending: list[tuple[Any, ...]] = []
        reach = span.start
        for child in kids.get(span.span_id, []):
            if child.start - reach >= gap_min:
                pending.append((None, item[1] + 1, reach, child.start))
            pending.append((child, item[1] + 1))
            reach = max(reach, child.end)
        if kids.get(span.span_id) and span.end - reach >= gap_min:
            pending.append((None, item[1] + 1, reach, span.end))
        stack.extend(reversed(pending))
    if stack:
        rows.append(f"... {len(spans) - drawn} more spans not shown")
    return rows


def trace_show(args: Any) -> None:
    """``kirocrew trace show <key>``: print the waterfall, or one line saying nothing matched."""
    from kiro_crew.tracing.provider import traces_dir

    directory = traces_dir()
    spans, others = pick_trace(read_spans(directory), str(args.key))
    if not spans:
        print(f"No trace matches {safe_terminal_line(str(args.key)[:MAX_KEY_CHARS])}.")
        return
    if others:
        print(
            f"{others} other trace(s) match; showing the most recent. Pass a trace id to pick one."
        )
    for row in render(spans):
        print(row.rstrip())

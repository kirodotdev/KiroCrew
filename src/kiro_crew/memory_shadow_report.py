"""``kirocrew memory shadow report``: what each memory stage's new policy would change.

Read-only. It reads only what the stages already record and writes nothing:
the write stages (origin gate, tier policy, fact origin gate, private dedup,
Dreaming) from the store's ``memory_meta`` counter rows (``<prefix>counts:<day>``)
and window keys (``rules_changed_at``, ``rules_version``), through a
``mode=ro`` connection, and the session-start and outcome lines of the
injection receipts under ``<data home>/usage/injections``.

The report carries ids-free counts only. The counters come from ``read_meta``,
which never selects a sample row, an id row or the Dreaming approval list. The
outcome scorecard comes from ``read_trace``, which pulls only the ``key``,
``path``, ``sk``, ``at`` and ``ts`` fields out of the id and control rows inside
SQLite (``json_each``), so an entry's value text never leaves the database. A missing or unreadable source is "no data", never an error.

Session starts come from the receipts (``phase == "session_start"`` lines of the
store) since each stage's window start.

The scorecard joins what a stage would drop with what followed
(:mod:`kiro_crew.memory_shadow_trace`). A dropped item is *missed* when a later
outcome line names its key: a ``memory_recall`` that delivered it, or a
``learn_add`` that came within the re-teach bar of it. A write stage changes the
store, so its item is the key and any later outcome counts. The control sample,
rows the stage keeps, is scored the same way. Verdict: ``wait`` until the bar (:data:`DAYS_BAR` days,
:data:`SESSIONS_BAR` session starts), a control sample and at least one outcome
line in the window exist; then ``pass`` when the miss rate is at
most :data:`MISS_BAR` and at most the control's, else ``fail``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Error as StdlibSQLiteError
from typing import Any, Final

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import memory_sections as _modes

logger = logging.getLogger(__name__)

#: The RFC's bar before a stage may move to enforce: days observed and session starts.
DAYS_BAR: Final = 14
SESSIONS_BAR: Final = 200
#: The most a stage may miss of what it drops: one item in :data:`_MISS_BAR_ITEMS`.
_MISS_BAR_ITEMS: Final = 100
MISS_BAR: Final = 1 / _MISS_BAR_ITEMS

VERDICT_OFF: Final = "off"
VERDICT_WAIT: Final = "wait"
VERDICT_PASS: Final = "pass"
VERDICT_FAIL: Final = "fail"

STATUS_OFF: Final = "off"
STATUS_WAITING: Final = "waiting"
STATUS_ENOUGH: Final = "enough data"

_SHADOW_MODE: Final = "shadow"
_SESSION_START: Final = "session_start"


@dataclass(frozen=True)
class Stage:
    """One stage: its ``memory.<name>`` flag, where it records, and its known bias."""

    name: str
    source: str
    mode_from: Callable[[object], str]
    bias: str = ""
    #: The id-row ``path`` values that are a drop or demotion; ``None`` for every entry.
    drop_paths: tuple[str, ...] | None = None


STAGES: Final[tuple[Stage, ...]] = (
    Stage("lesson_origin_gate", "memory_origin_log:", _modes.lesson_origin_gate_mode_from),
    Stage(
        "lesson_tier_policy",
        "memory_tier_log:",
        _modes.lesson_tier_policy_mode_from,
        drop_paths=("consolidation",),
    ),
    Stage("fact_origin_gate", "memory_fact_origin_log:", _modes.fact_origin_gate_mode_from),
    Stage("member_lesson_dedup", "memory_dedup_log:", _modes.member_lesson_dedup_mode_from),
    Stage("dream", "memory_dream:", _modes.dream_mode_from),
)
STAGE_NAMES: Final[tuple[str, ...]] = tuple(s.name for s in STAGES)
#: Stages with no outcome trace yet: Dreaming proposes but records nothing to join.
_UNTRACED: Final = frozenset({"dream"})
#: Outcome receipt kinds, as :mod:`kiro_crew.injection_receipts` names them.
_OUTCOMES: Final = ("recall", "learn_add")


# ── Parsing helpers ─────────────────────────────────────────────────────────


def _count(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _instant(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _day(raw: object) -> str | None:
    """*raw* when it is a ``YYYY-MM-DD`` day, else ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def parse_since(raw: str | None, *, today: date | None = None) -> datetime | None:
    """``--since``: a ``YYYY-MM-DD`` day or ``<n>d`` days back, as a UTC instant.

    Raises ``ValueError`` on anything else, which the CLI prints as one line.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text.endswith("d") and text[:-1].isdigit():
        start = (today or datetime.now(timezone.utc).date()) - timedelta(days=int(text[:-1]))
    else:
        try:
            start = date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"--since wants YYYY-MM-DD or <n>d, not {raw!r}") from None
    return datetime(start.year, start.month, start.day, tzinfo=timezone.utc)


# ── Readers (read-only) ─────────────────────────────────────────────────────


def read_receipts(receipt_dir: Path, since: datetime | None) -> list[dict]:
    """The receipt lines at or after *since*, keeping only the fields the report reads.

    Shards are named by local day, so a shard one day before *since* is still read
    and each line is filtered on its own ``ts``.
    """
    keep = ("ts", "session_key", "sk", "phase", "memory_store", *_OUTCOMES)
    floor = (since.date() - timedelta(days=1)).isoformat() if since else ""
    lines: list[dict] = []
    try:
        shards = sorted(p for p in receipt_dir.glob("*.jsonl") if p.stem >= floor)
    except OSError:
        return lines
    for shard in shards:
        try:
            text = shard.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict):
                continue
            at = _instant(line.get("ts"))
            if at is None or (since is not None and at < since):
                continue
            row = {k: line[k] for k in keep if k in line}
            row["ts"] = at
            lines.append(row)
    return lines


def read_meta(db_path: Path | None) -> dict[str, str]:
    """The counter and window keys of every write stage in *db_path*'s ``memory_meta``.

    Opened ``mode=ro``. Only ``counts:*`` rows and the window keys are selected, so a
    sample row, an id row or the Dreaming list (which may hold memory text) is never
    read. A missing file or table is ``{}``.
    """
    if db_path is None or not db_path.exists():
        return {}
    patterns: list[str] = []
    for stage in STAGES:
        patterns.append(stage.source + "counts:*")
        patterns.append(stage.source + "rules_changed_at")
        patterns.append(stage.source + "rules_version")
    where = " OR ".join("key GLOB ?" for _ in patterns)
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, StdlibSQLiteError):
        return {}
    try:
        rows = conn.execute(
            f"SELECT key, value FROM memory_meta WHERE {where}", patterns
        )  # nosec B608
        return {str(k): str(v) for k, v in rows.fetchall()}
    except (sqlite3.Error, StdlibSQLiteError):
        logger.debug("no memory_meta in %s", db_path, exc_info=True)
        return {}
    finally:
        conn.close()


def read_trace(db_path: Path | None) -> list[tuple[str, str, dict]]:
    """``(row key, role, fields)`` for every id and control entry.

    Opened ``mode=ro``. ``json_each`` unpacks each row inside SQLite and only the
    ``key``, ``path``, ``sk``, ``at`` and ``ts`` fields are selected, so an entry's
    value text is never read. *role* is ``ids`` or ``control``. A missing file or
    table is ``[]``.
    """
    if db_path is None or not db_path.exists():
        return []
    patterns: list[str] = []
    for stage in STAGES:
        if stage.name in _UNTRACED:
            continue
        patterns.append(f"{stage.source}ids:*")
        patterns.append(f"{stage.source}control:*")
    where = " OR ".join("m.key GLOB ?" for _ in patterns)
    fields = ("key", "path", "sk", "at", "ts")
    select = ", ".join(f"json_extract(j.value, '$.{name}')" for name in fields)
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, StdlibSQLiteError):
        return []
    out: list[tuple[str, str, dict]] = []
    try:
        rows = conn.execute(
            f"SELECT m.key, {select} FROM memory_meta m, "  # nosec B608: placeholders only
            "json_each(CASE WHEN json_valid(m.value) THEN m.value ELSE '[]' END) j "
            f"WHERE ({where}) AND j.type = 'object'",
            patterns,
        ).fetchall()
    except (sqlite3.Error, StdlibSQLiteError):
        logger.debug("no trace rows in %s", db_path, exc_info=True)
        return []
    finally:
        conn.close()
    for row in rows:
        row_key = str(row[0])
        role = row_key.rsplit(":", 2)[-2] if row_key.count(":") >= 2 else ""
        out.append((row_key, role, {n: v for n, v in zip(fields, row[1:]) if v is not None}))
    return out


# ── Outcome scoring ─────────────────────────────────────────────────────────


def _end_of_day(day: str | None) -> datetime | None:
    """An entry with no timestamp sits at the end of its row's day, so no outcome
    earlier that day is read as following it."""
    if day is None:
        return None
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return start + timedelta(days=1) - timedelta(microseconds=1)


def _outcomes(lines: list[dict], store: str) -> dict[str, list[tuple[datetime, str]]]:
    """``{memory key: [(ts, session tag), ...]}`` over the outcome lines of *store*."""
    out: dict[str, list[tuple[datetime, str]]] = {}
    for line in lines:
        kind = line.get("phase")
        field = line.get(kind) if isinstance(kind, str) and kind in _OUTCOMES else None
        if not isinstance(field, dict) or not _store_matches(line, store):
            continue
        ids = field.get("ids")
        sk = line.get("sk")
        tag = sk if isinstance(sk, str) else ""
        for key in dict.fromkeys(ids if isinstance(ids, list) else ()):
            if isinstance(key, str):
                out.setdefault(key, []).append((line["ts"], tag))
    return out


def _first_seen(units: dict, unit: tuple[str, str], at: datetime) -> None:
    if unit not in units or at < units[unit]:
        units[unit] = at


def _score(
    units: dict[tuple[str, str], datetime],
    outcomes: dict[str, list[tuple[datetime, str]]],
) -> dict:
    """Of *units* (``("", key) -> first time``), how many a later outcome names."""
    missed = 0
    for (_, key), at in units.items():
        missed += any(ts > at for ts, _sk in outcomes.get(key, ()))
    total = len(units)
    return {"items": total, "missed": missed, "rate": round(missed / total, 4) if total else None}


def _write_units(stage: Stage, trace: list, start) -> tuple[dict, dict]:
    dropped: dict = {}
    control: dict = {}
    for row_key, role, fields in trace:
        if not row_key.startswith(stage.source):
            continue
        key = fields.get("key")
        if not isinstance(key, str):
            continue
        at = _instant(fields.get("at") or fields.get("ts")) or _end_of_day(
            _day(row_key.rsplit(":", 1)[-1])
        )
        if at is None or (start is not None and at < start):
            continue
        if role == "control":
            _first_seen(control, ("", key), at)
        elif stage.drop_paths is None or fields.get("path") in stage.drop_paths:
            _first_seen(dropped, ("", key), at)
    return dropped, control


def _over(missed: int, items: int, bar_missed: int, bar_items: int) -> bool:
    """``missed / items > bar_missed / bar_items`` without rounding either side."""
    return missed * bar_items > bar_missed * items


def _verdict(card: dict, outcomes_in_window: int) -> tuple[str, str]:
    if card["mode"] == "off":
        return VERDICT_OFF, ""
    if card["stage"] in _UNTRACED:
        return VERDICT_WAIT, "no outcome trace"
    if card["days"] < DAYS_BAR or card["sessions"] < SESSIONS_BAR:
        return (
            VERDICT_WAIT,
            f"{card['days']}/{DAYS_BAR} days, {card['sessions']}/{SESSIONS_BAR} sessions",
        )
    miss, ctrl = card["miss"], card["control"]
    if not ctrl["items"]:
        return VERDICT_WAIT, "no control sample"
    # No outcome line in the window means nothing was observed, not nothing missed.
    if not outcomes_in_window:
        return VERDICT_WAIT, "no outcome trace in window"
    if miss["items"] and _over(miss["missed"], miss["items"], 1, _MISS_BAR_ITEMS):
        return VERDICT_FAIL, f"miss {miss['missed'] / miss['items']:.2%} > {MISS_BAR:.0%}"
    if miss["items"] and _over(miss["missed"], miss["items"], ctrl["missed"], ctrl["items"]):
        return (
            VERDICT_FAIL,
            f"miss {miss['missed'] / miss['items']:.2%} > "
            f"control {ctrl['missed'] / ctrl['items']:.2%}",
        )
    return VERDICT_PASS, ""


# ── Per-stage scorecards ────────────────────────────────────────────────────


def _store_matches(line: dict, store: str) -> bool:
    return (line.get("memory_store") or "default") == store


def _session_starts(lines: Iterable[dict], store: str, start: datetime | None) -> int:
    """Each ``session_start`` line counts, so a session key whose context restarts counts again."""
    return sum(
        1
        for line in lines
        if line.get("phase") == _SESSION_START
        and _store_matches(line, store)
        and (start is None or line["ts"] >= start)
    )


def _window_start(a: datetime | None, b: datetime | None) -> datetime | None:
    """Exclude a partial UTC day whose counters can mix policy versions."""
    start = max(a, b) if a is not None and b is not None else a or b
    if start is None:
        return None
    start = start.astimezone(timezone.utc)
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight if start == midnight else midnight + timedelta(days=1)


def _first_day(days: Iterable[str]) -> datetime | None:
    first = min(days, default=None)
    return datetime.fromisoformat(first).replace(tzinfo=timezone.utc) if first else None


def _meta_counts(meta: dict[str, str], stage: Stage) -> dict[str, dict]:
    prefix = stage.source + "counts:"
    out: dict[str, dict] = {}
    for key, raw in meta.items():
        day = _day(key[len(prefix) :]) if key.startswith(prefix) else None
        if day is None:
            continue
        try:
            out[day] = _dict(json.loads(raw))
        except ValueError:
            continue
    return out


def _sum_into(total: dict[str, int], fields: object) -> None:
    for name, n in _dict(fields).items():
        if _count(n):
            total[str(name)] = total.get(str(name), 0) + _count(n)


def _day_tally(stage: Stage, counts: dict) -> dict[str, int]:
    """One day's shadow counters (Dreaming: propose) folded to ``{field: n}``."""
    total: dict[str, int] = {}
    if stage.name == "dream":
        _sum_into(total, counts)
        return total
    by_mode = _dict(counts.get(_SHADOW_MODE))
    if stage.name == "member_lesson_dedup":
        _sum_into(total, by_mode)
    else:
        for fields in by_mode.values():
            _sum_into(total, fields)
    return total


def _base_and_changed(stage: Stage, total: dict[str, int]) -> tuple[int, int | None]:
    """Checks or rows seen, and changes where the counters measure them."""
    if stage.name in ("lesson_origin_gate", "lesson_tier_policy"):
        return total.get("lessons", 0), total.get("demoted", 0)
    if stage.name == "fact_origin_gate":
        return total.get("facts", 0), total.get("gated", 0)
    if stage.name == "member_lesson_dedup":
        return total.get("lessons", 0) + total.get("checked", 0), sum(
            n for k, n in total.items() if k.endswith("_folds")
        )
    return total.get("rows", 0), None


#: The stages that judge each standing lesson by the person's quote.
_QUOTE_STAGES: Final = frozenset({"lesson_tier_policy"})
#: The first tier-policy rules version whose counters measure quotes.
_QUOTE_RULES_VERSION: Final = 2


def _quote_summary(stage: Stage, total: dict[str, int], policy: int | None) -> dict | None:
    """How the quotes behind a lesson stage's standing lessons came out, or ``None``.

    ``absent``, ``fail`` and ``pass`` count measured ``always`` lessons; ``backed``
    is the passing quotes that state their rule; ``unmeasured`` the lessons whose
    shadow quote call failed. ``support`` counts passing quotes by cosine bucket.
    """
    if stage.name not in _QUOTE_STAGES or (policy or 0) < _QUOTE_RULES_VERSION:
        return None
    absent = total.get("always", 0) - total.get("quote_present", 0) - total.get("unmeasured", 0)
    prefix = "support_"
    return {
        "absent": max(absent, 0),
        "fail": total.get("quote_fail", 0),
        "pass": total.get("quote_pass", 0),
        "backed": total.get("backed", 0),
        "unmeasured": total.get("unmeasured", 0),
        "support": {k[len(prefix) :]: n for k, n in sorted(total.items()) if k.startswith(prefix)},
        "calls": total.get("quote_calls", 0),
        "call_failed": total.get("quote_call_failed", 0),
        "cut": total.get("quote_cut", 0),
    }


def _meta_stage(stage: Stage, meta: dict[str, str], lines: list[dict], store: str, since) -> dict:
    # The window opens on the first day with shadow counters, or later at the
    # stage's rules-change instant. ``shadow_started_at`` is not read: the origin,
    # tier and dedup stages stamp it only on the first pass that keeps an id, after
    # their counters have already started.
    # Only the tier policy stamps ``rules_changed_at``, with its first counter
    # write under a new rules version, so the window skips older days.
    # shortcut: dedup records no rules-change instant and its counters carry no
    # rules_version, so a rules bump there needs that window key written with the
    # counters before this report can split the days.
    changed_at = _instant(meta.get(stage.source + "rules_changed_at"))
    raw_version = meta.get(stage.source + "rules_version")
    policy = int(raw_version) if raw_version and raw_version.isdigit() else None
    by_day = {
        day: tally
        for day, counts in _meta_counts(meta, stage).items()
        if (tally := _day_tally(stage, counts))
    }
    start = _window_start(_window_start(_first_day(by_day), changed_at), since)
    if start is not None:
        by_day = {d: v for d, v in by_day.items() if d >= start.date().isoformat()}
    total: dict[str, int] = {}
    for tally in by_day.values():
        _sum_into(total, tally)
    base, changed = _base_and_changed(stage, total)
    if stage.name == "dream":
        total["proposals"] = sum(n for k, n in total.items() if k.startswith("proposed_"))
    return _card(stage, policy, start, by_day, base, changed, total, lines, store)


def _card(stage, policy, start, by_day, base, changed, detail, lines, store) -> dict:
    sessions = _session_starts(lines, store, start)
    days = len(by_day)
    return {
        "stage": stage.name,
        "source": stage.source,
        "policy": policy,
        "window_start": start.isoformat() if start else None,
        "days": days,
        "sessions": sessions,
        "base": base,
        "changed": changed,
        "rate": round(changed / base, 4) if base and changed is not None else None,
        "detail": detail,
        "by_day": dict(sorted(by_day.items())),
        "data": bool(days or base),
        "bias": stage.bias,
    }


def build_report(
    *,
    store: str,
    receipt_dir: Path,
    db_path: Path | None,
    memory_config: object,
    since: datetime | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    """The report as plain data: one scorecard per stage, ids-free."""
    lines = read_receipts(receipt_dir, since)
    meta = read_meta(db_path)
    trace = read_trace(db_path)
    outcomes = _outcomes(lines, store)
    cards = []
    for s in STAGES:
        if stage is not None and s.name != stage:
            continue
        card = _meta_stage(s, meta, lines, store, since)
        card["quotes"] = _quote_summary(s, card["detail"], card["policy"])
        if s.name in _QUOTE_STAGES and card["quotes"] is None and card["data"]:
            card["bias"] = (
                "upper bound: these counters predate the quote measurement "
                f"(rules version < {_QUOTE_RULES_VERSION}), so every standing rule counts as demoted"
            )
        mode = s.mode_from(getattr(memory_config, s.name, None))
        card["mode"] = mode
        if mode == "off":
            card["status"] = STATUS_OFF
        elif card["days"] >= DAYS_BAR and card["sessions"] >= SESSIONS_BAR:
            card["status"] = STATUS_ENOUGH
        else:
            card["status"] = STATUS_WAITING
        start = _instant(card["window_start"])
        dropped, control = _write_units(s, trace, start)
        # A unit the stage also drops (a dedup vector fold of a row first kept) is
        # not a kept row, so it leaves the control.
        control = {u: t for u, t in control.items() if u not in dropped}
        card["miss"] = _score(dropped, outcomes)
        card["control"] = _score(control, outcomes)
        in_window = sum(
            1
            for line in lines
            if line.get("phase") in _OUTCOMES
            and _store_matches(line, store)
            and (start is None or line["ts"] >= start)
        )
        card["verdict"], card["verdict_reason"] = _verdict(card, in_window)
        cards.append(card)
    return {
        "store": store,
        "since": since.isoformat() if since else None,
        "bar": {"days": DAYS_BAR, "sessions": SESSIONS_BAR, "miss": MISS_BAR},
        "outcomes": {
            k: sum(1 for line in lines if line.get("phase") == k and _store_matches(line, store))
            for k in _OUTCOMES
        },
        "stages": cards,
    }


# ── Text rendering ──────────────────────────────────────────────────────────

_COLUMNS: Final = (
    ("stage", 20),
    ("mode", 8),
    ("policy", 6),
    ("start", 10),
    ("days", 5),
    ("sessions", 8),
    ("seen", 6),
    ("changed", 7),
    ("rate", 6),
    ("miss", 11),
    ("ctrl", 11),
)


def _fraction(score: dict) -> str:
    """``missed/items``, or ``-`` when nothing was scored."""
    return f"{score['missed']}/{score['items']}" if score["items"] else "-"


def _quote_line(card: dict) -> str:
    q = card["quotes"]
    support = " ".join(f"{k}={v}" for k, v in q["support"].items()) or "-"
    return (
        f"quotes {card['stage']}: absent={q['absent']} fail={q['fail']} pass={q['pass']} "
        f"(backed={q['backed']}) unmeasured={q['unmeasured']}  support {support}  "
        f"quote calls={q['calls']} failed={q['call_failed']} cut={q['cut']}"
    )


def _row(cells: Iterable[str]) -> str:
    return "  ".join(str(c).ljust(w) for c, (_, w) in zip(cells, _COLUMNS)).rstrip()


def render_text(report: dict[str, Any]) -> list[str]:
    """The report as terminal lines."""
    since = report["since"][:10] if report["since"] else "window start"
    out = [f"Memory shadow report  store={report['store']}  since={since}", ""]
    out.append(_row(name for name, _ in _COLUMNS) + "  verdict")
    for card in report["stages"]:
        head = [card["stage"], card["mode"]]
        if not card["data"]:
            out.append(_row(head + ["-"] * (len(_COLUMNS) - 2)) + f"  {card['verdict']} (no data)")
            continue
        rate = f"{card['rate'] * 100:.0f}%" if card["rate"] is not None else "-"
        cells = head + [
            str(card["policy"] or "-"),
            card["window_start"][:10] if card["window_start"] else "-",
            str(card["days"]),
            str(card["sessions"]),
            str(card["base"]),
            str(card["changed"]) if card["changed"] is not None else "-",
            rate,
            _fraction(card["miss"]),
            _fraction(card["control"]),
        ]
        verdict = card["verdict"]
        if card["verdict_reason"]:
            verdict += f" ({card['verdict_reason']})"
        out.append(_row(cells) + "  " + verdict)
    notes = [c for c in report["stages"] if c["bias"]]
    if notes:
        out.append("")
        out.extend(f"note {c['stage']}: {c['bias']}" for c in notes)
    quoted = [c for c in report["stages"] if c.get("quotes") and c["data"]]
    if quoted:
        out.append("")
        out.extend(_quote_line(c) for c in quoted)
    out.append("")
    out.append(
        "seen = items the stage looked at; "
        "changed = how many the new policy would change. "
        "miss = dropped items a later recall or re-teach named, of all dropped; "
        "ctrl = the same for a sample of kept items. "
        f"Pass: >={DAYS_BAR} days, >={SESSIONS_BAR} session starts, "
        f"miss <={MISS_BAR:.0%} and <= ctrl."
    )
    if len(report["stages"]) == 1:
        card = report["stages"][0]
        if card["by_day"]:
            out.append("")
            out.append(f"{card['stage']} by day:")
            for day, tally in card["by_day"].items():
                fields = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
                out.append(f"  {day}  {fields}")
    return out

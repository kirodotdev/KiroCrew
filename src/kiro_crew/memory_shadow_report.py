"""``kirocrew memory shadow report``: what each memory stage's new policy would change.

Read-only. It reads only what the stages already record and writes nothing:

* the render stages (rule authority order, lesson fold, relevance rule) from the
  injection receipts under ``<data home>/usage/injections``: the daily counters
  and the policy window in ``stage-log.json``, and the sampled stage fields on the
  ``YYYY-MM-DD.jsonl`` lines;
* the write stages (kind routing, origin gate, tier policy, private dedup,
  Dreaming) from the store's ``memory_meta`` counter rows (``<prefix>counts:<day>``)
  and window keys (``rules_changed_at``, ``rules_version``), through a
  ``mode=ro`` connection.

The report carries ids-free counts only. It never reads a sample row, an id row or
the Dreaming approval list, so no memory text reaches it. A missing or unreadable
source is "no data", never an error.

Session starts come from the receipts (``phase == "session_start"`` lines of the
store) since each stage's window start, because the write-stage logs carry no
session key. The stage-log counters are per data home, so a render stage's days
and line counts cover every store; its change rate comes from the store's own
sampled lines.
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

STATUS_OFF: Final = "off"
STATUS_WAITING: Final = "waiting"
STATUS_ENOUGH: Final = "enough data"

SOURCE_RECEIPTS: Final = "receipts"
_SHADOW_MODE: Final = "shadow"
_SESSION_START: Final = "session_start"
_STAGE_LOG_NAME: Final = "stage-log.json"


@dataclass(frozen=True)
class Stage:
    """One stage: its ``memory.<name>`` flag, where it records, and its known bias."""

    name: str
    source: str
    mode_from: Callable[[object], str]
    bias: str = ""


STAGES: Final[tuple[Stage, ...]] = (
    Stage(
        "rule_authority_order",
        SOURCE_RECEIPTS,
        _modes.rule_authority_order_mode_from,
        "inflated while lesson_origin_gate is not enforce: "
        "an agent's own learn_add counts as user-taught",
    ),
    Stage("relevance_rule", SOURCE_RECEIPTS, _modes.relevance_rule_mode_from),
    Stage("lesson_fold", SOURCE_RECEIPTS, _modes.lesson_fold_mode_from),
    Stage(
        "kind_routing",
        "memory_kind_log:",
        _modes.kind_routing_mode_from,
        "shadow adds kind text to the consolidation prompt, "
        "so these days are not a clean off baseline",
    ),
    Stage("lesson_origin_gate", "memory_origin_log:", _modes.lesson_origin_gate_mode_from),
    Stage(
        "lesson_tier_policy",
        "memory_tier_log:",
        _modes.lesson_tier_policy_mode_from,
        "upper bound: shadow asks for no quote, so every standing rule counts as demoted",
    ),
    Stage("member_lesson_dedup", "memory_dedup_log:", _modes.member_lesson_dedup_mode_from),
    Stage("dream", "memory_dream:", _modes.dream_mode_from),
)
STAGE_NAMES: Final[tuple[str, ...]] = tuple(s.name for s in STAGES)


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


def read_stage_log(receipt_dir: Path) -> dict:
    """``stage-log.json``, or ``{}`` when it is missing or unreadable."""
    try:
        return _dict(json.loads((receipt_dir / _STAGE_LOG_NAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def read_receipts(receipt_dir: Path, since: datetime | None) -> list[dict]:
    """The receipt lines at or after *since*, keeping only the fields the report reads.

    Shards are named by local day, so a shard one day before *since* is still read
    and each line is filtered on its own ``ts``.
    """
    keep = ("ts", "session_key", "phase", "memory_store") + tuple(
        s.name for s in STAGES if s.source == SOURCE_RECEIPTS
    )
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
        if stage.source == SOURCE_RECEIPTS:
            continue
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


def _receipt_stage(stage: Stage, stage_log: dict, lines: list[dict], store: str, since) -> dict:
    window = _dict(_dict(stage_log.get("stages")).get(stage.name))
    started = _instant(window.get("started_at"))
    policy = _count(window.get("policy")) or None
    by_day: dict[str, dict[str, int]] = {}
    for raw_day, kinds in _dict(stage_log.get("counts")).items():
        day = _day(raw_day)
        if day is None:
            continue
        tally = _dict(_dict(_dict(kinds).get(stage.name)).get(_SHADOW_MODE))
        if not tally:
            continue
        by_day[day] = {
            name: _count(tally.get(name)) for name in ("lines", "would_add", "would_drop")
        }
    start = _window_start(started or _first_day(by_day), since)
    if start is not None:
        by_day = {d: v for d, v in by_day.items() if d >= start.date().isoformat()}
    sampled = changed = 0
    for line in lines:
        field = line.get(stage.name)
        if not isinstance(field, dict) or field.get("mode") != _SHADOW_MODE:
            continue
        if not _store_matches(line, store) or (start is not None and line["ts"] < start):
            continue
        sampled += 1
        changed += int(
            bool(field.get("would_add") or field.get("would_drop") or field.get("pairs"))
        )
    detail = {
        "lines": sum(v["lines"] for v in by_day.values()),
        "would_add": sum(v["would_add"] for v in by_day.values()),
        "would_drop": sum(v["would_drop"] for v in by_day.values()),
    }
    return _card(stage, policy, start, by_day, sampled, changed, detail, lines, store)


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
    if stage.name in ("member_lesson_dedup", "kind_routing"):
        _sum_into(total, by_mode)
    else:
        for fields in by_mode.values():
            _sum_into(total, fields)
    return total


def _base_and_changed(stage: Stage, total: dict[str, int]) -> tuple[int, int | None]:
    """Checks or rows seen, and changes where the counters measure them."""
    if stage.name == "kind_routing":
        return sum(total.get(k, 0) for k in ("fact", "event", "state")), total.get("state", 0)
    if stage.name in ("lesson_origin_gate", "lesson_tier_policy"):
        return total.get("lessons", 0), total.get("demoted", 0)
    if stage.name == "member_lesson_dedup":
        return total.get("lessons", 0) + total.get("checked", 0), sum(
            n for k, n in total.items() if k.endswith("_folds")
        )
    return total.get("rows", 0), None


def _meta_stage(stage: Stage, meta: dict[str, str], lines: list[dict], store: str, since) -> dict:
    # The window opens on the first day with shadow counters, or later at the
    # stage's rules-change instant. ``shadow_started_at`` is not read: the origin,
    # tier and dedup stages stamp it only on the first pass that keeps an id, after
    # their counters have already started.
    # shortcut: origin/tier/dedup record no rules-change instant and their counters
    # carry no rules_version, so a rules bump there needs a window key written
    # with the counters before this report can split the days.
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
    stage_log = read_stage_log(receipt_dir)
    lines = read_receipts(receipt_dir, since)
    meta = read_meta(db_path)
    cards = []
    for s in STAGES:
        if stage is not None and s.name != stage:
            continue
        if s.source == SOURCE_RECEIPTS:
            card = _receipt_stage(s, stage_log, lines, store, since)
        else:
            card = _meta_stage(s, meta, lines, store, since)
        mode = s.mode_from(getattr(memory_config, s.name, None))
        card["mode"] = mode
        if mode == "off":
            card["status"] = STATUS_OFF
        elif card["days"] >= DAYS_BAR and card["sessions"] >= SESSIONS_BAR:
            card["status"] = STATUS_ENOUGH
        else:
            card["status"] = STATUS_WAITING
        cards.append(card)
    return {
        "store": store,
        "since": since.isoformat() if since else None,
        "bar": {"days": DAYS_BAR, "sessions": SESSIONS_BAR},
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
)


def _row(cells: Iterable[str]) -> str:
    return "  ".join(str(c).ljust(w) for c, (_, w) in zip(cells, _COLUMNS)).rstrip()


def render_text(report: dict[str, Any]) -> list[str]:
    """The report as terminal lines."""
    since = report["since"][:10] if report["since"] else "window start"
    out = [f"Memory shadow report  store={report['store']}  since={since}", ""]
    out.append(_row(name for name, _ in _COLUMNS) + "  status")
    for card in report["stages"]:
        head = [card["stage"], card["mode"]]
        if not card["data"]:
            out.append(_row(head + ["-"] * (len(_COLUMNS) - 2)) + f"  {card['status']} (no data)")
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
        ]
        status = card["status"]
        if status == STATUS_WAITING:
            status += (
                f" ({card['days']}/{DAYS_BAR} days, {card['sessions']}/{SESSIONS_BAR} sessions)"
            )
        out.append(_row(cells) + "  " + status)
    notes = [c for c in report["stages"] if c["bias"]]
    if notes:
        out.append("")
        out.extend(f"note {c['stage']}: {c['bias']}" for c in notes)
    out.append("")
    out.append(
        "seen = lines (render stages: sampled receipt lines) or items the stage looked at; "
        "changed = how many the new policy would change. "
        f"Bar: >={DAYS_BAR} days and >={SESSIONS_BAR} session starts."
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

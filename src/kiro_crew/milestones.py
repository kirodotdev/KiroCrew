"""Milestones -- a built-in, deterministic table of new-feature and usage milestones.

The design record is ``docs/request-for-change/rfc-achievement-feature-discovery.md``.
This module owns three things:

* the CATALOG (:data:`CATALOG`): a constant shipped with the release, never a
  manifest and never model output, because a starter task is text the dashboard
  may send to a model on the user's behalf;
* the local STATE FILE (``milestones.json`` in :func:`config_dir`): ids, counts,
  timestamps and source labels only -- never chat content, file names, links or
  channel names;
* the COUNTING HELPERS the hook sites call (:func:`record`,
  :func:`record_user_turn`, :func:`record_when`).

Counting is by event, not by final state: each hook runs at the moment the thing
happened, a counter only ever grows and a ``reached`` entry is never removed, so
deleting the artifact, task or chat later undoes nothing. The ``used_when``-style
probes run once, as a backfill on upgrade, applied by the first hook write or
the first read, whichever comes first (:func:`ensure_backfilled`).

Every helper is best-effort and never raises into its caller: a milestone must not
be able to break an artifact save, a cron add, a chat birth or a turn. Writes run
off the event loop and are coalesced: hook calls queue an operation, and one
executor job applies every queued operation in a single read-modify-write. A
sidecar ``flock`` serializes that write against other processes on the same home
(the cron MCP server adds jobs from its own process).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.metrics.events import emit_counter
from kiro_crew.platform_lock_compat import file_lock, open_lock_file

logger = logging.getLogger(__name__)

_FILE = "milestones.json"
_VERSION = 1

#: The only rows a starter task may SEND on click (RFC 4.5, D1): one-time, local,
#: no config change, nothing more needed from the user, bounded spend. Every other
#: row prefills, and catalog validation drops a ``send`` row that is not here.
DIRECT_SEND_IDS = frozenset({"widgets", "parallel-chats"})

#: How long after a starter click an event still counts as led by that starter.
_STARTER_WINDOW_S = 24 * 3600

# Metric names (RFC 4.11). Attribute values are ids from the fixed table and
# closed enums only.
M_STARTER_CLICKED = "kirocrew.milestone.starter_clicked"
M_REACHED = "kirocrew.milestone.reached"
M_SKIPPED = "kirocrew.milestone.skipped"
M_MARK_ALL_DONE = "kirocrew.milestone.mark_all_done"
M_PANEL_OPENED = "kirocrew.milestone.panel_opened"
M_NOTICE_SHOWN = "kirocrew.milestone.notice_shown"
M_FEATURES_COMPLETED = "kirocrew.milestone.features_completed"


# ── Catalog ──

Group = Literal["features", "sessions", "turns", "streak", "concurrent"]


@dataclass(frozen=True)
class Starter:
    """How a feature row's starter task reaches the user."""

    kind: Literal["prompt", "route"]
    send: Literal["prefill", "send"] = "prefill"
    route: str = ""
    agent: str = ""
    crew_mode: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "send": self.send,
            "route": self.route,
            "agent": self.agent,
            "crew_mode": self.crew_mode,
        }


@dataclass(frozen=True)
class Milestone:
    """One row of the table. ``id`` is also the i18n key, counter key and state key."""

    id: str
    group: Group
    order: int
    doc: str = ""
    video_id: str = ""
    starter: Starter | None = None
    tiers: tuple[int, ...] = (1,)
    counter_mode: Literal["sum", "peak", "streak"] = "sum"
    backfill_when: tuple[str, ...] = ()
    available_when: tuple[str, ...] = ()
    min_version: str = ""


_ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_GROUPS = ("features", "sessions", "turns", "streak", "concurrent")


def _row_is_valid(row: Milestone) -> bool:
    """Whether *row* may be served. Logs the reason when it is not."""
    from kiro_crew.feature_videos import _CTA_ROUTE_RE
    from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

    reason = ""
    tiers = row.tiers
    if not _ID_RE.fullmatch(row.id) or len(row.id) > 64:
        reason = "bad id"
    elif row.group not in _GROUPS:
        reason = f"unknown group {row.group!r}"
    elif (
        not tiers
        or any(isinstance(t, bool) or not isinstance(t, int) or t < 1 for t in tiers)
        or any(b <= a for a, b in zip(tiers, tiers[1:]))
    ):
        reason = "tiers must be positive and strictly increasing"
    elif row.doc and row.doc not in TIP_DOC_ALLOWLIST:
        reason = f"doc {row.doc!r} is not in the tips doc allowlist"
    elif row.group != "features":
        # This rule is what keeps a usage milestone or a streak from ever
        # becoming an empty-state card or a "Mark as done" row.
        if row.starter is not None:
            reason = "only feature rows may carry a starter"
        elif max(tiers) <= 1:
            reason = "a usage row needs a tier above 1"
    else:
        starter = row.starter
        if not row.doc or starter is None or tiers != (1,):
            reason = "a feature row needs doc, starter and tiers == (1,)"
        elif starter.send not in ("prefill", "send") or starter.kind not in ("prompt", "route"):
            reason = "bad starter kind/send"
        elif starter.send == "send" and row.id not in DIRECT_SEND_IDS:
            reason = "direct send is only for the low-cost rows"
        elif starter.kind == "route" and not _CTA_ROUTE_RE.fullmatch(starter.route):
            reason = f"unsafe route {starter.route!r}"
        elif starter.kind == "prompt" and starter.route:
            reason = "a prompt starter names no route"
    if not reason and row.min_version:
        from kiro_crew.apps.version import parse_version

        try:
            parse_version(row.min_version)
        except ValueError:
            reason = f"unparseable min_version {row.min_version!r}"
    if reason:
        logger.warning("milestone %r dropped from the catalog: %s", row.id, reason)
        return False
    return True


def _mate_route() -> str:
    from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME

    return f"/members?member={ASSISTANT_MEMBER_NAME}"


def _build_catalog() -> tuple[Milestone, ...]:
    return (
        Milestone(
            id="widgets",
            group="features",
            order=1,
            doc="artifacts.md",
            starter=Starter(kind="prompt", send="send"),
            backfill_when=("artifacts_nonempty",),
        ),
        Milestone(
            id="scheduled-task",
            group="features",
            order=2,
            doc="cron-and-scheduling.md",
            starter=Starter(kind="prompt"),
            backfill_when=("sel_event_ok:cron_add",),
        ),
        Milestone(
            id="mate",
            group="features",
            order=3,
            doc="crew-members.md",
            starter=Starter(kind="route", route=_mate_route()),
            backfill_when=("mate_thread_has_user_turn",),
            available_when=("mate_missing",),
        ),
        Milestone(
            id="parallel-chats",
            group="features",
            order=4,
            doc="subagents.md",
            starter=Starter(kind="prompt", send="send"),
            backfill_when=("sel_event_ok:session_create",),
        ),
        Milestone(
            id="slack",
            group="features",
            order=5,
            doc="slack-integration.md",
            starter=Starter(kind="prompt"),
            backfill_when=("channel_connected:slack",),
        ),
        Milestone(
            id="pr-watch",
            group="features",
            order=6,
            doc="monitor-loops.md",
            starter=Starter(kind="prompt"),
            backfill_when=("sel_directive_ok:monitor_start", "sel_directive_ok:monitor_watch"),
        ),
        # No backfill: crediting a past Crew Mode turn would mean scanning every
        # session's metadata. "Mark as done" covers the gap.
        Milestone(
            id="crew-mode",
            group="features",
            order=7,
            doc="crew-members.md",
            starter=Starter(kind="prompt", crew_mode=True),
        ),
        Milestone(id="chats-opened", group="sessions", order=1, tiers=(10, 100, 1000)),
        Milestone(id="turns-sent", group="turns", order=1, tiers=(100, 1000, 10000)),
        Milestone(
            id="streak-best", group="streak", order=1, tiers=(3, 7, 30, 100), counter_mode="streak"
        ),
    )


CATALOG: tuple[Milestone, ...] = _build_catalog()

_catalog_cache: tuple[Milestone, ...] | None = None


def catalog() -> tuple[Milestone, ...]:
    """The catalog with invalid rows dropped (validated once per process)."""
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = tuple(r for r in CATALOG if _row_is_valid(r))
    return _catalog_cache


def _row(milestone_id: str) -> Milestone | None:
    return next((r for r in catalog() if r.id == milestone_id), None)


def feature_rows() -> tuple[Milestone, ...]:
    return tuple(sorted((r for r in catalog() if r.group == "features"), key=lambda r: r.order))


def is_feature_id(milestone_id: object) -> bool:
    return isinstance(milestone_id, str) and any(r.id == milestone_id for r in feature_rows())


# ── State file ──


def _path() -> Path:
    return config_dir() / _FILE


def _num(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        out = float(value)
    except (ValueError, OverflowError):
        return 0.0
    return out if math.isfinite(out) and out >= 0 else 0.0


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _empty() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "counters": {},
        "streak": {"current": 0, "best": 0, "last_day": ""},
        "reached": {},
        "skipped": [],
        "noticed": [],
        "backfilled": False,
        "starter_clicked": {},
    }


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v for v in value if isinstance(v, str) and len(v) <= 80))


def load(*, strict: bool = False) -> dict[str, Any]:
    """Read the state file, degrading malformed fields to their empty form.

    The file is the user's own and may be hand-edited, so each field is checked
    on its own: a bad entry is dropped rather than failing the whole read.
    Writers use ``strict`` to reject unreadable files and non-object roots;
    only a missing file may start a new state.
    """
    st = _empty()
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return st
    except (OSError, ValueError, RecursionError):
        if strict:
            raise
        logger.warning("milestones.json unreadable; using empty state", exc_info=True)
        return st
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("milestones.json must contain an object")
        return st
    counters = raw.get("counters")
    if isinstance(counters, dict):
        for key, row in counters.items():
            if isinstance(key, str) and isinstance(row, dict):
                st["counters"][key] = {
                    "count": _count(row.get("count")),
                    "first_seen": _num(row.get("first_seen")),
                }
    streak = raw.get("streak")
    if isinstance(streak, dict):
        last_day = streak.get("last_day")
        st["streak"] = {
            "current": _count(streak.get("current")),
            "best": _count(streak.get("best")),
            "last_day": last_day if isinstance(last_day, str) and len(last_day) == 10 else "",
        }
    reached = raw.get("reached")
    if isinstance(reached, dict):
        for key, row in reached.items():
            if isinstance(key, str) and isinstance(row, dict):
                source = row.get("source")
                if source in ("event", "backfill", "manual"):
                    st["reached"][key] = {"at": _num(row.get("at")), "source": source}
    st["skipped"] = _str_list(raw.get("skipped"))
    st["noticed"] = _str_list(raw.get("noticed"))
    st["backfilled"] = raw.get("backfilled") is True
    clicked = raw.get("starter_clicked")
    if isinstance(clicked, dict):
        st["starter_clicked"] = {k: _num(v) for k, v in clicked.items() if isinstance(k, str)}
    return st


def _save(st: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only: the file records which features this user has and has not
    # used, a behavioural profile that must not be world-readable on a shared box.
    atomic_write(
        path,
        json.dumps(st, indent=2, sort_keys=True) + "\n",
        restrict_to_owner=True,
        restrict_on_error="warn",
    )


@contextmanager
def _file_lock() -> Iterator[None]:
    """Cross-process exclusive lock on a sidecar, so two processes cannot lose a count.

    ``file_lock`` covers POSIX and Windows and fails closed (raises) rather
    than entering the read-modify-write unserialized.
    """
    lock_path = _path().with_name(_FILE + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock_path) as fd, file_lock(fd):
        yield


# ── Pure state transitions ──
#
# An op mutates the loaded state in place and appends ``(metric, attrs)`` pairs
# to *events*; metrics are emitted only after the write succeeded.

Op = Callable[[dict[str, Any], float, list[tuple[str, dict[str, Any]]]], None]


def _tier_key(row: Milestone, tier: int) -> str:
    return row.id if row.group == "features" else f"{row.id}:{tier}"


def _value(st: dict[str, Any], row: Milestone) -> int:
    if row.counter_mode == "streak":
        return int(st["streak"]["best"])
    return int(st["counters"].get(row.id, {}).get("count", 0))


def _feature_done(st: dict[str, Any], row: Milestone) -> bool:
    return row.id in st["reached"] or _value(st, row) >= 1


def _features_complete(st: dict[str, Any]) -> bool:
    rows = feature_rows()
    return bool(rows) and all(_feature_done(st, r) for r in rows)


def _via(st: dict[str, Any], row: Milestone, source: str, now: float) -> str:
    if source != "event":
        return source
    if row.group != "features":
        return "organic"
    clicked = st["starter_clicked"].get(row.id, 0.0)
    return "starter" if clicked and now - clicked <= _STARTER_WINDOW_S else "organic"


def _mark_reached(
    st: dict[str, Any], row: Milestone, source: str, now: float, events: list
) -> None:
    """Write a ``reached`` entry for every tier *row*'s value now meets."""
    value = _value(st, row)
    for tier in row.tiers:
        key = _tier_key(row, tier)
        if value < tier or key in st["reached"]:
            continue
        st["reached"][key] = {"at": now, "source": source}
        # Backfilled and manual milestones are never announced.
        if source != "event" and key not in st["noticed"]:
            st["noticed"].append(key)
        events.append(
            (M_REACHED, {"group": row.group, "milestone": key, "via": _via(st, row, source, now)})
        )


def _features_completed_event(st: dict[str, Any], events: list) -> None:
    sources = {st["reached"].get(r.id, {}).get("source", "event") for r in feature_rows()}
    via = "manual" if sources == {"manual"} else ("mixed" if "manual" in sources else "organic")
    events.append((M_FEATURES_COMPLETED, {"via": via}))


def _bump(st: dict[str, Any], counter_id: str, now: float) -> None:
    row = st["counters"].setdefault(counter_id, {"count": 0, "first_seen": 0.0})
    row["count"] = row["count"] + 1
    if not row["first_seen"]:
        row["first_seen"] = now


def _op_count(milestone_id: str) -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        row = _row(milestone_id)
        if row is None or row.counter_mode != "sum":
            return
        was_complete = _features_complete(st)
        _bump(st, milestone_id, now)
        _mark_reached(st, row, "event", now, events)
        if not was_complete and _features_complete(st):
            _features_completed_event(st, events)

    return apply


def local_today() -> date:
    """Today in the user's time zone: the configured ``timezone``, else the machine's."""
    tz: tzinfo | None = None
    try:
        from zoneinfo import ZoneInfo

        from kiro_crew.config.loader_snapshots import published_config_timezone

        name = published_config_timezone()
        if name:
            tz = ZoneInfo(name)
    except Exception:
        logger.debug("milestones: configured timezone unusable; using the machine's", exc_info=True)
    return (datetime.now(tz) if tz is not None else datetime.now().astimezone()).date()


def _op_active_day(today: date) -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        streak = st["streak"]
        iso = today.isoformat()
        if streak["last_day"] == iso:
            return
        yesterday = (today - timedelta(days=1)).isoformat()
        streak["current"] = streak["current"] + 1 if streak["last_day"] == yesterday else 1
        streak["last_day"] = iso
        streak["best"] = max(streak["best"], streak["current"])
        row = _row("streak-best")
        if row is not None:
            _mark_reached(st, row, "event", now, events)

    return apply


def current_streak(st: dict[str, Any], today: date) -> int:
    """The streak to SHOW: 0 once a whole day has been missed, without a write."""
    last_day = st["streak"]["last_day"]
    if last_day in (today.isoformat(), (today - timedelta(days=1)).isoformat()):
        return int(st["streak"]["current"])
    return 0


# ── Applying ops ──

_pending: list[Op] = []
_pending_lock = threading.Lock()
_flush_scheduled = False
#: One writer at a time inside this process; :func:`_file_lock` covers other processes.
_write_lock = threading.Lock()


def _policy_permits() -> bool:
    """Whether the capability policy allows milestones. In-memory, any thread.

    Asked only of an INSTALLED context, and False when none is installed: with
    no context the policy is unknown, so nothing is counted or written. The
    gateway and the MCP server processes install one at boot. ``policy_permits``
    would otherwise compose and memoize the default context from whichever
    thread asked first; a hook flush runs on a worker thread and must not be the
    one that installs process-wide state.
    """
    from kiro_crew.edition_locks import policy_permits
    from kiro_crew.platform.context import installed_context

    if installed_context() is None:
        return False
    return policy_permits("capabilities.milestones")


def _drop_pending() -> None:
    global _flush_scheduled
    with _pending_lock:
        _pending.clear()
        _flush_scheduled = False


def _flush() -> None:
    """Apply every queued op in ONE read-modify-write. Never raises.

    On a home not yet backfilled, the backfill op is applied first in the same
    transaction, so a hook event never claims a row the backfill would credit.
    """
    global _flush_scheduled
    try:
        if not _policy_permits():
            # A policy denial of the scope stops counting entirely: the queued
            # ops are dropped with no probe, no state write and no metric. The
            # config switch (dashboard.milestones_enabled) is not read here, so
            # a user who hides milestones keeps counting.
            _drop_pending()
            return
        with _pending_lock:
            if not _pending:
                _flush_scheduled = False
                return
        backfill: Op | None = None
        try:
            # Probes run outside both locks: they can read the audit log.
            backfill = _backfill_op()
        except Exception:
            # The hook ops still apply; ``backfilled`` stays False, so a later
            # flush or read tries the backfill again.
            logger.warning("milestones backfill probes failed", exc_info=True)
        with _write_lock:
            with _pending_lock:
                ops = list(_pending)
                _pending.clear()
                _flush_scheduled = False
            if not ops:
                return
            if backfill is not None:
                ops.insert(0, backfill)
            events: list[tuple[str, dict[str, Any]]] = []
            with _file_lock():
                st = load(strict=True)
                now = time.time()
                for op in ops:
                    try:
                        op(st, now, events)
                    except Exception:
                        logger.warning("milestone op failed; skipped", exc_info=True)
                _save(st)
        for name, attrs in events:
            emit_counter(name, attrs)
    except Exception:
        logger.warning("milestones state write failed", exc_info=True)


def _submit(op: Op) -> None:
    """Queue *op* and make sure a flush runs, off the event loop when there is one."""
    global _flush_scheduled
    try:
        with _pending_lock:
            _pending.append(op)
            schedule = not _flush_scheduled
            _flush_scheduled = True
        if not schedule:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a CLI, an MCP server process, a worker thread or a test.
            _flush()
            return
        # Fire-and-forget is safe only because _flush cannot raise.
        loop.run_in_executor(None, _flush)
    except Exception:
        logger.warning("milestone record failed", exc_info=True)


def apply_now(op: Op) -> None:
    """Apply *op* in its own transaction and return once it is on disk. Blocking.

    For a client-requested write (a mark, a skip, a notice acknowledgement):
    unlike the hook path it RAISES when the write fails, so the route can answer
    an error instead of claiming a change that was never persisted.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    with _write_lock:
        with _file_lock():
            st = load(strict=True)
            op(st, time.time(), events)
            _save(st)
    for name, attrs in events:
        emit_counter(name, attrs)


# ── Hook-site helpers (never raise) ──


def record(milestone_id: str) -> None:
    """Count one event for *milestone_id*. Call only after the action succeeded."""
    _submit(_op_count(milestone_id))


def record_active_day() -> None:
    """Count today toward the streak."""
    try:
        today = local_today()
    except Exception:
        logger.warning("milestones: could not resolve today", exc_info=True)
        return
    _submit(_op_active_day(today))


def record_user_turn(*, mate_thread: bool, crew_mode: bool) -> None:
    """Count one user-sent turn: messages, the streak, and the Mate / Crew Mode rows."""
    record("turns-sent")
    record_active_day()
    if mate_thread:
        record("mate")
    if crew_mode:
        record("crew-mode")


def record_when(milestone_id: str, signal: str) -> None:
    """Count *milestone_id* if the probe *signal* fires; the probe runs off the loop."""

    def apply(st: dict[str, Any], now: float, events: list) -> None:
        if _probe(signal):
            _op_count(milestone_id)(st, now, events)

    _submit(apply)


# ── Backfill ──


def _probe(signal: str) -> bool:
    from kiro_crew.feature_videos import probe_fires

    return probe_fires(signal)


def _count_chat_transcripts() -> int:
    """Count distinct user chats with a persistent header, reading only bounded first lines."""
    from kiro_crew.history_derivation import (
        TRANSCRIPT_HEADER_MAX_BYTES,
        memory_mode_from_header_line,
    )
    from kiro_crew.history_files import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER

    base = config_dir() / "sessions"
    stems: set[str] = set()
    for directory in (base, base / ARCHIVE_DIR_NAME):
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    name = entry.name
                    if not name.startswith("dashboard_chat-") or not name.endswith(".jsonl"):
                        continue
                    stem = name[: -len(".jsonl")].split(ARCHIVE_SEGMENT_DELIMITER)[0]
                    if stem in stems:
                        continue
                    try:
                        with open(entry.path, "rb") as fh:
                            header = fh.readline(TRANSCRIPT_HEADER_MAX_BYTES)
                    except OSError:
                        continue
                    if memory_mode_from_header_line(header) == "persistent":
                        stems.add(stem)
        except OSError:
            continue
    return len(stems)


def _backfill_op() -> Op | None:
    """Run the backfill probes and return the op that applies them. Blocking.

    Returns None when the state file already says ``backfilled`` (a tolerant
    read). Raises when a probe fails. Call it BEFORE taking the write lock: the
    probes can read the audit log. The op re-checks ``backfilled``, so two
    racing callers apply it once. Credited rows are stamped ``source: backfill``
    and go straight into ``noticed``: an existing user gets no notices on
    upgrade day.
    """
    if load()["backfilled"]:
        return None
    fired = {r.id for r in feature_rows() if any(_probe(s) for s in r.backfill_when)}
    chats = _count_chat_transcripts()

    def apply(st: dict[str, Any], now: float, events: list) -> None:
        if st["backfilled"]:
            return
        for row in catalog():
            if row.id in fired:
                counter = st["counters"].setdefault(row.id, {"count": 0, "first_seen": 0.0})
                counter["count"] = max(counter["count"], 1)
                counter["first_seen"] = counter["first_seen"] or now
                _mark_reached(st, row, "backfill", now, events)
            elif row.id == "chats-opened" and chats:
                counter = st["counters"].setdefault(row.id, {"count": 0, "first_seen": 0.0})
                counter["count"] = max(counter["count"], chats)
                counter["first_seen"] = counter["first_seen"] or now
                _mark_reached(st, row, "backfill", now, events)
        st["backfilled"] = True

    return apply


def ensure_backfilled() -> None:
    """Run the one-time backfill if it has not run on this home. Blocking. Never raises."""
    try:
        op = _backfill_op()
    except Exception:
        logger.warning("milestones backfill probes failed", exc_info=True)
        return
    if op is None:
        return
    try:
        apply_now(op)
    except Exception:
        # Best-effort like the hooks: the next read tries again.
        logger.warning("milestones backfill write failed", exc_info=True)


# ── Manual and client-reported ops (validated by the route before submission) ──


def op_mark_done(milestone_id: str) -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        row = _row(milestone_id)
        if row is None or row.group != "features" or _feature_done(st, row):
            return
        was_complete = _features_complete(st)
        st["reached"][row.id] = {"at": now, "source": "manual"}
        if row.id not in st["noticed"]:
            st["noticed"].append(row.id)
        events.append((M_REACHED, {"group": row.group, "milestone": row.id, "via": "manual"}))
        if not was_complete and _features_complete(st):
            _features_completed_event(st, events)

    return apply


def op_mark_all_done() -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        events.append((M_MARK_ALL_DONE, {}))
        for row in feature_rows():
            op_mark_done(row.id)(st, now, events)

    return apply


def op_skip(milestone_id: str, surface: str) -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        if milestone_id not in st["skipped"]:
            st["skipped"].append(milestone_id)
        events.append((M_SKIPPED, {"surface": surface, "milestone": milestone_id}))

    return apply


def op_skip_all() -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        for row in feature_rows():
            if not _feature_done(st, row) and row.id not in st["skipped"]:
                st["skipped"].append(row.id)
        events.append((M_SKIPPED, {"surface": "empty_state", "milestone": "all"}))

    return apply


def op_starter_clicked(milestone_id: str, surface: str) -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        row = _row(milestone_id)
        if row is None or row.starter is None:
            return
        st["starter_clicked"][row.id] = now
        send = "route" if row.starter.kind == "route" else row.starter.send
        events.append((M_STARTER_CLICKED, {"surface": surface, "milestone": row.id, "send": send}))

    return apply


def op_notice_shown(key: str) -> Op:
    def apply(st: dict[str, Any], now: float, events: list) -> None:
        if key not in st["reached"] or key in st["noticed"]:
            return
        st["noticed"].append(key)
        row = _row(key.split(":", 1)[0])
        events.append((M_NOTICE_SHOWN, {"group": row.group if row else "features"}))

    return apply


def emit_panel_opened() -> None:
    emit_counter(M_PANEL_OPENED, {})


# ── Read model (GET /api/milestones) ──


def _version_ok(row: Milestone) -> bool:
    if not row.min_version:
        return True
    import kiro_crew
    from kiro_crew.feature_videos import _version_ok as fv_version_ok

    return fv_version_ok(row.min_version, kiro_crew.__version__)


def _unavailable(row: Milestone) -> bool:
    return not _version_ok(row) or any(_probe(s) for s in row.available_when)


def _video_payload(video_id: str) -> dict[str, object] | None:
    """The Feature Videos offer for *video_id* when its clip is playable here, else None."""
    if not video_id:
        return None
    try:
        from kiro_crew import feature_videos as fv
        from kiro_crew import feature_videos_cache as cache_mod

        offers = list(fv._bundled_offers())
        current = cache_mod.feature_video_cache().current_manifest()
        if current is not None:
            offers.extend(fv._hosted_offers(current))
        offer = next((o for o in offers if o.id == video_id), None)
        return offer.payload() if offer is not None else None
    except Exception:
        logger.debug("milestone video lookup failed", exc_info=True)
        return None


def snapshot(today: date | None = None) -> dict[str, Any]:
    """The ``GET /api/milestones`` body for an enabled install. Blocking.

    Reads the state file only; ``available_when`` probes run only for rows that
    declare them.
    """
    st = load()
    today = today or local_today()
    feature_items: list[dict[str, Any]] = []
    recommended: list[str] = []
    done = total = 0
    for row in feature_rows():
        reached = st["reached"].get(row.id)
        counter = st["counters"].get(row.id, {})
        if _unavailable(row):
            state = "unavailable"
        elif _feature_done(st, row):
            state = "done"
        elif row.id in st["skipped"]:
            state = "skipped"
        else:
            state = "open"
        if state != "unavailable":
            total += 1
            done += state == "done"
        if state == "open" and len(recommended) < 3:
            recommended.append(row.id)
        done_at = None
        if state == "done":
            done_at = reached["at"] if reached else (counter.get("first_seen") or None)
        feature_items.append(
            {
                "id": row.id,
                "state": state,
                "done_at": done_at,
                "source": (reached["source"] if reached else "event") if state == "done" else None,
                "video": _video_payload(row.video_id),
                "starter": row.starter.payload() if row.starter else None,
                "doc": row.doc,
            }
        )
    groups: list[dict[str, Any]] = [{"id": "features", "items": feature_items}]
    for group in ("sessions", "turns", "streak", "concurrent"):
        rows = sorted((r for r in catalog() if r.group == group), key=lambda r: r.order)
        if not rows:
            continue
        items = []
        for row in rows:
            reached_at = {
                str(t): st["reached"][_tier_key(row, t)]["at"]
                for t in row.tiers
                if _tier_key(row, t) in st["reached"]
            }
            items.append(
                {
                    "id": row.id,
                    "value": _value(st, row),
                    "tiers": list(row.tiers),
                    "tiers_reached_at": reached_at,
                }
            )
        groups.append({"id": group, "items": items})
    notices = sorted(
        (k for k, v in st["reached"].items() if v["source"] == "event" and k not in st["noticed"]),
        key=lambda k: st["reached"][k]["at"],
    )
    return {
        "enabled": True,
        "features": {"done": done, "total": total},
        "streak": {"current": current_streak(st, today), "best": int(st["streak"]["best"])},
        "groups": groups,
        "recommended": recommended,
        "notices": notices,
    }

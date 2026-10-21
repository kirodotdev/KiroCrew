"""A crewmate's goal record: its goals and the key results that measure them.

The record is the user's word on what the crewmate works toward and how far it
is from getting there (RFC "Crewmate goals"). What the crewmate is doing right
now is not part of it: that is the crewmate's work ledger.

* ``goals`` -- the crewmate's goals, each ``{id, text, created_at,
  updated_at, updated_by}``; a goal's text is one paragraph. The user adds,
  changes and removes them directly (the Goals tab); the crewmate only proposes
  one, through a ``crewmate.goal`` change card the user confirms (``set_goal``
  on ``kirocrew-mate``), which adds a goal or replaces one goal's text by id. A
  record written before goals were a list holds one ``long_term`` string: it
  reads as a one-goal list (:data:`LEGACY_GOAL_ID`) and is written in the list
  shape on its next write.
* ``key_results`` -- readings that say how far
  the crewmate is from its goals, each ``{id, text, target, current, updated_at,
  goal_ids}``. They are ONE shared set: ``goal_ids`` names the goals a key result
  serves (none, one or several), and removing a goal strips its id from them.
  The crewmate proposes the whole set through a ``crewmate.key_results`` card
  (``set_key_results``); once confirmed it records readings directly
  (``key_result_progress``). The user edits or removes them on the Goals tab.
  A reading is free text: a number ("27", "80%") or a short qualitative note.
* ``schedules`` -- the four check-in switches (Daily Goal Reminder, Weekly
  report, Monthly review, Heartbeat), the one shared time of day, the weekday,
  the day of the month and the heartbeat's interval in minutes; the gateway
  schedules they drive are owned by :mod:`kiro_crew.dashboard.goal_reminder`.
* ``reports`` -- pointers to the newest weekly reports and monthly reviews, each
  saved as an artifact under ``Crewmates/<name>/Reports``.

Where it lives
--------------
``trust/member-goals/<slug>.json``, beside the permanent rules, NOT in the
agent-writable ``members/<slug>/`` directory that holds ``briefing.md``. A
goal needs the user's button to change, and a file the crewmate's own file
tools can write would let it change a goal without one. The protection is
exactly ``member-rules/``'s: ``trust/`` is in the sensitive-path floor, so the
agent's file-write and edit tools are refused there (the tool gate). It is not
an OS seal: ``trust/`` is a read-write leaf of the crew sandbox, so a shell
command in a sandboxed turn can still write it, the same as the rules file. It
is still a different file from ``member-rules/``: rules are the user's safety
boundary, and nothing about a goal, not even an applied card, ever writes them.

Because a sandboxed shell can write ``trust/``, it can also put a symlink there,
and the gateway that writes this record is not sandboxed. So the record is never
reached through a link: ``trust``, ``member-goals`` and the record itself are
each opened relative to the one before without following a symlink, and the
write goes into that pinned directory (:func:`_open_goals_dir`). A link at any of
them is refused (:class:`GoalsUnreadable`), so a crewmate cannot point the
directory at ``profiles/`` or another sealed leaf and have the gateway write
there. Where the platform has no ``O_NOFOLLOW`` and ``dir_fd`` (Windows), each
component is checked with ``lstat`` before the path is used.

Keyed like every lossy-slug member file: the payload records the exact member
name (and its stable ``member_id`` when it has one), and a read whose name does
not match answers "no goal" rather than another member's record.

Blocking file IO throughout: async callers go through ``asyncio.to_thread``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: Directory under ``trust/`` holding one ``<slug>.json`` per member.
GOALS_DIR_NAME = "member-goals"

#: Bounds. The record is injected into every session of the crewmate, so each
#: part is capped on WRITE (refused, never truncated) and the whole stays small.
#: One goal's text. There is no cap on how many goals or key results a record
#: holds: the crewmate is guided toward a small set, the user decides, and the
#: whole record stays inside :data:`_RECORD_MAX_BYTES`.
LONG_TERM_MAX_CHARS = 2000
GOAL_TEXT_MAX_CHARS = LONG_TERM_MAX_CHARS
KR_TEXT_MAX_CHARS = 200
#: A target: a number ("27", "80%") or a word ("yes").
KR_VALUE_MAX_CHARS = 40
#: A reading: free text, a number or a short qualitative note.
KR_READING_MAX_CHARS = 120
#: One saved weekly or monthly report: its title and its Markdown body.
REPORT_TITLE_MAX_CHARS = 200
REPORT_MAX_CHARS = 40_000
#: The crewmate's notes (``save_notes``): what its briefing injects, which is
#: ``members.MEMBER_BRIEFING_MAX_CHARS`` (spelled here because this module
#: imports no ``members``; a test pins the two equal).
NOTES_MAX_CHARS = 4000
#: The record file itself, as read back: a hand-edited file past this is
#: unreadable rather than loaded whole.
_RECORD_MAX_BYTES = 64 * 1024


def _now() -> float:
    """The record's clock: every timestamp it writes reads this one function."""
    return time.time()


BY_USER = "user"
BY_CREWMATE = "crewmate"
_AUTHORS = (BY_USER, BY_CREWMATE)

DEFAULT_REMINDER_TIME = "09:00"
#: The four check-ins, each its own switch and its own schedule. The heartbeat
#: wakes the crewmate every :data:`HEARTBEAT_INTERVALS` minutes to move its work
#: forward on its own (RFC "Crewmate heartbeat").
CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCE_MONTHLY = "monthly"
CADENCE_HEARTBEAT = "heartbeat"
CADENCES = (CADENCE_DAILY, CADENCE_WEEKLY, CADENCE_MONTHLY, CADENCE_HEARTBEAT)
#: The heartbeat's intervals, in minutes, and the one a record without one reads as.
HEARTBEAT_INTERVALS = (15, 30, 60, 120, 240)
DEFAULT_HEARTBEAT_INTERVAL = 30
#: Report kinds: what a weekly and a monthly check-in each produce.
REPORT_KINDS = (CADENCE_WEEKLY, CADENCE_MONTHLY)
#: Cron day of week (0 = Sunday); Monday by default.
DEFAULT_WEEKDAY = 1
#: Day of the month, capped at 28 so every month has it.
DEFAULT_MONTH_DAY = 1
MAX_MONTH_DAY = 28
#: How long a key result's readings are kept, and how many at most.
READINGS_KEEP_SECS = 90 * 24 * 3600
MAX_READINGS = 200
#: How many report pointers the record keeps.
MAX_REPORTS = 12
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_KR_ID_RE = re.compile(r"^k[0-9a-f]{8}$")
_GOAL_ID_RE = re.compile(r"^g[0-9a-f]{8}$")
#: The id a record holding one legacy ``long_term`` string gives that goal. Fixed,
#: so the goal reads with the same id until the record is written in list shape.
LEGACY_GOAL_ID = "g00000000"


def is_goal_id(value: Any) -> bool:
    """Whether *value* has the shape of a goal id (``g`` and 8 hex digits)."""
    return isinstance(value, str) and bool(_GOAL_ID_RE.match(value))


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class GoalError(ValueError):
    """A refused goal write: ``code`` is machine-readable, the text is sayable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GoalsUnreadable(RuntimeError):
    """The record exists but cannot be read; a write must not paper over it."""


def _lock_for(slug: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(slug, threading.Lock())


def member_goals_path(slug: str) -> Path:
    """Absolute path to one member's goal record.

    Lexical, under the resolved data home: ``trust`` and ``member-goals`` are
    NOT resolved, because a sandboxed shell can replace either with a symlink.
    :func:`_open_goals_dir` refuses such a link; this path is what the
    no-``dir_fd`` fallback and the tests use.
    """
    from kiro_crew.members import MemberSlugError, validate_slug

    validate_slug(slug)
    root = data_home().resolve() / "trust" / GOALS_DIR_NAME
    target = root / f"{slug}.json"
    if target.parent != root:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


#: POSIX-only flags, read once so the module still type-checks for Windows,
#: where :data:`_PINNED` is False and these are never used.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)

#: Whether the record can be opened component by component without following a
#: link (``openat`` with ``O_NOFOLLOW``). False on Windows.
_PINNED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    # os.replace takes src_dir_fd/dst_dir_fd like os.rename, but only rename
    # is listed in supports_dir_fd.
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)


class _Linked(OSError):
    """A component of the goal record's path is a symlink."""


def _open_dir_at(parent_fd: int, name: str, *, create: bool) -> int | None:
    """*name* under *parent_fd* as a directory fd, never following a link."""
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
    except NotADirectoryError as exc:
        raise _Linked(f"{name} is not a plain directory") from exc
    except OSError as exc:
        # ELOOP: *name* is a symlink and O_NOFOLLOW refused it.
        raise _Linked(f"{name} is a symlink") from exc
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _Linked(f"{name} is a symlink") from exc


def _open_goals_dir(*, create: bool) -> int | None:
    """A pinned fd of ``trust/member-goals``, or None when it does not exist.

    Each component below the data home is opened relative to the one before
    without following a link, so a symlink planted at ``trust`` or
    ``member-goals`` is refused instead of redirecting the gateway's write.
    """
    home = os.open(str(data_home().resolve()), os.O_RDONLY | _O_DIRECTORY)
    try:
        trust = _open_dir_at(home, "trust", create=create)
    finally:
        os.close(home)
    if trust is None:
        return None
    try:
        goals = _open_dir_at(trust, GOALS_DIR_NAME, create=create)
        if goals is not None and create:
            for fd in (trust, goals):
                try:
                    getattr(os, "fchmod")(fd, 0o700)
                except OSError:
                    logger.debug("could not tighten mode on the goals dir", exc_info=True)
        return goals
    finally:
        os.close(trust)


def _check_unlinked(path: Path) -> None:
    """The no-``dir_fd`` fallback: refuse when ``trust``, the dir or *path* is a link.

    A Windows directory junction counts as a link: ``is_symlink`` is False for
    one, so a planted junction would otherwise redirect the write.
    """
    for p in (path.parent.parent, path.parent, path):
        if platform_compat.is_link_or_junction(p):
            raise _Linked(f"{p} is a symlink or junction")


def default_schedules() -> dict[str, Any]:
    return {
        "time": DEFAULT_REMINDER_TIME,
        "timezone": "",
        CADENCE_DAILY: False,
        CADENCE_WEEKLY: False,
        "weekday": DEFAULT_WEEKDAY,
        CADENCE_MONTHLY: False,
        "month_day": DEFAULT_MONTH_DAY,
        CADENCE_HEARTBEAT: False,
        "interval": DEFAULT_HEARTBEAT_INTERVAL,
    }


def empty_record() -> dict[str, Any]:
    """The record of a crewmate that has no goal yet (every existing crewmate)."""
    return {
        "goals": [],
        "key_results": [],
        "schedules": default_schedules(),
        "reports": [],
        "removed_key_result": None,
        "removed_goal": None,
    }


def has_goal(rec: dict[str, Any]) -> bool:
    """Whether *rec* holds at least one goal."""
    return bool(rec.get("goals"))


def goal_ids(rec: dict[str, Any]) -> list[str]:
    """The ids of *rec*'s goals, in list order."""
    return [g["id"] for g in rec.get("goals") or []]


def goal_numbers(rec: dict[str, Any]) -> dict[str, int]:
    """Each goal id's number as the user sees it: its 1-based place in the list."""
    return {gid: n for n, gid in enumerate(goal_ids(rec), start=1)}


def _clean_text(raw: Any, *, max_chars: int, multiline: bool, what: str) -> str:
    from kiro_crew.members import _strip_controls

    if not isinstance(raw, str):
        raise GoalError("invalid_text", f"{what} must be text")
    text = _strip_controls(raw, keep_newlines=multiline).strip()
    if len(text) > max_chars:
        raise GoalError("too_long", f"{what} is longer than {max_chars} characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise GoalError(
            "invalid_text", f"{what} contains characters that cannot be saved"
        ) from None
    return text


def clean_text(raw: Any, *, max_chars: int, multiline: bool, what: str) -> str:
    """*raw* normalised exactly as the record stores it (:class:`GoalError` if refused)."""
    return _clean_text(raw, max_chars=max_chars, multiline=multiline, what=what)


def _valid_kr(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("text"), str)
        and isinstance(item.get("target"), str)
        and isinstance(item.get("current"), str)
        and isinstance(item.get("updated_at"), (int, float))
    )


def _valid_reading(r: Any) -> bool:
    return (
        isinstance(r, dict)
        and isinstance(r.get("value"), str)
        and isinstance(r.get("at"), (int, float))
    )


def _prune_readings(readings: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    keep = [r for r in readings if now - float(r["at"]) <= READINGS_KEEP_SECS]
    return keep[-MAX_READINGS:]


def _valid_goal(item: Any) -> bool:
    return (
        isinstance(item, dict) and is_goal_id(item.get("id")) and isinstance(item.get("text"), str)
    )


def _clean_goal_ids(raw: Any, valid: set[str]) -> list[str]:
    """*raw* as a key result's ``goal_ids``: ids of goals in *valid*, each once, in order."""
    out: list[str] = []
    for gid in raw if isinstance(raw, list) else []:
        if isinstance(gid, str) and gid in valid and gid not in out:
            out.append(gid)
    return out


def _normalize_kr(item: dict[str, Any], valid: set[str]) -> dict[str, Any]:
    kr = dict(item)
    raw = item.get("readings")
    kr["readings"] = [
        {"value": r["value"], "at": float(r["at"])}
        for r in (raw if isinstance(raw, list) else [])
        if _valid_reading(r)
    ][-MAX_READINGS:]
    kr["goal_ids"] = _clean_goal_ids(item.get("goal_ids"), valid)
    return kr


def _loose_krs(records: list[Any]) -> list[dict[str, Any]]:
    """Key-result *records* normalized with every well-formed goal id kept.

    For a card Undo's records, whose links are checked against the goals the
    record holds at the time of the write, not here.
    """
    valid = {
        g
        for r in records
        if isinstance(r, dict) and isinstance(r.get("goal_ids"), list)
        for g in r["goal_ids"]
        if is_goal_id(g)
    }
    return [_normalize_kr(r, valid) for r in records if _valid_kr(r)]


def _normalize_goals(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("goals")
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for g in raw:
            if not _valid_goal(g) or any(o["id"] == g["id"] for o in out):
                continue
            out.append(
                {
                    "id": g["id"],
                    "text": g["text"][:GOAL_TEXT_MAX_CHARS],
                    "created_at": (
                        float(g["created_at"])
                        if isinstance(g.get("created_at"), (int, float))
                        else 0.0
                    ),
                    "updated_at": (
                        float(g["updated_at"])
                        if isinstance(g.get("updated_at"), (int, float))
                        else 0.0
                    ),
                    "updated_by": g["updated_by"] if g.get("updated_by") in _AUTHORS else "",
                }
            )
        return out
    # A record written before goals were a list: its one ``long_term`` string.
    legacy = data.get("long_term")
    if isinstance(legacy, str) and legacy.strip():
        at = data.get("long_term_updated_at")
        at = float(at) if isinstance(at, (int, float)) else 0.0
        by = data.get("long_term_updated_by")
        return [
            {
                "id": LEGACY_GOAL_ID,
                "text": legacy[:GOAL_TEXT_MAX_CHARS],
                "created_at": at,
                "updated_at": at,
                "updated_by": by if by in _AUTHORS else "",
            }
        ]
    return []


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    # Only the fields empty_record names survive: any other key in the file
    # (an older record's ``short_term`` list or ``long_term`` string, say) is
    # read past here and dropped on the next write.
    rec = empty_record()
    rec["goals"] = _normalize_goals(data)
    valid = set(goal_ids(rec))
    krs = data.get("key_results")
    if isinstance(krs, list):
        rec["key_results"] = [_normalize_kr(k, valid) for k in krs if _valid_kr(k)]
    sched = data.get("schedules")
    if isinstance(sched, dict):
        out = rec["schedules"]
        for cadence in CADENCES:
            out[cadence] = sched.get(cadence) is True
        if isinstance(sched.get("time"), str) and _TIME_RE.match(sched["time"]):
            out["time"] = sched["time"]
        if isinstance(sched.get("timezone"), str):
            out["timezone"] = sched["timezone"][:64]
        if isinstance(sched.get("weekday"), int) and 0 <= sched["weekday"] <= 6:
            out["weekday"] = sched["weekday"]
        day = sched.get("month_day")
        if isinstance(day, int) and 1 <= day <= MAX_MONTH_DAY:
            out["month_day"] = day
        if is_heartbeat_interval(sched.get("interval")):
            out["interval"] = sched["interval"]
    reports = data.get("reports")
    if isinstance(reports, list):
        rec["reports"] = [
            dict(r)
            for r in reports
            if isinstance(r, dict)
            and r.get("kind") in REPORT_KINDS
            and isinstance(r.get("slug"), str)
            and isinstance(r.get("title"), str)
            and isinstance(r.get("at"), (int, float))
        ][-MAX_REPORTS:]
    removed = data.get("removed_key_result")
    if (
        isinstance(removed, dict)
        and _valid_kr(removed.get("kr"))
        and isinstance(removed.get("index"), int)
        and isinstance(removed.get("at"), (int, float))
    ):
        kr = _normalize_kr(removed["kr"], valid)
        rec["removed_key_result"] = {
            "kr": kr,
            "index": removed["index"],
            "at": float(removed["at"]),
        }
    gone = data.get("removed_goal")
    if (
        isinstance(gone, dict)
        and _valid_goal(gone.get("goal"))
        and isinstance(gone.get("index"), int)
        and isinstance(gone.get("at"), (int, float))
    ):
        g = _normalize_goals({"goals": [gone["goal"]]})
        if g:
            links = gone.get("key_result_ids")
            rec["removed_goal"] = {
                "goal": g[0],
                "index": gone["index"],
                "at": float(gone["at"]),
                "key_result_ids": [
                    k for k in (links if isinstance(links, list) else []) if isinstance(k, str)
                ],
            }
    return rec


def _read_pinned(path: Path) -> bytes | None:
    d = _open_goals_dir(create=False)
    if d is None:
        return None
    try:
        try:
            # O_NONBLOCK: a FIFO planted at the name must not hang the read
            # (and the per-member lock held around it); fstat refuses it below.
            fd = os.open(
                path.name, os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=d
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _Linked(f"{path.name} is a symlink") from exc
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise _Linked(f"{path.name} is not a regular file")
        with os.fdopen(fd, "rb") as fh:
            return fh.read(_RECORD_MAX_BYTES + 1)
    finally:
        os.close(d)


def _read_raw(slug: str) -> dict[str, Any] | None:
    path = member_goals_path(slug)
    try:
        if _PINNED:
            raw = _read_pinned(path)
            if raw is None:
                return None
        else:
            _check_unlinked(path)
            raw = read_bytes_with_retry(path, max_bytes=_RECORD_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GoalsUnreadable(f"goal record for {slug!r} cannot be read") from exc
    if len(raw) > _RECORD_MAX_BYTES:
        raise GoalsUnreadable(f"goal record for {slug!r} is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise GoalsUnreadable(f"goal record for {slug!r} is malformed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("member"), str):
        raise GoalsUnreadable(f"goal record for {slug!r} is malformed")
    return data


def _owned_by(data: dict[str, Any], slug: str, member: str) -> bool:
    # Name-scoped like the rules: a colliding slug's file holds another exact
    # name's goal, which for THIS member is "no goal yet". A stable member id
    # equal to the slug is the member itself, whatever its name is now.
    return data.get("member_id") == slug or data.get("member") == member


def read_goals(slug: str, member: str) -> dict[str, Any]:
    """*member*'s goal record, or :func:`empty_record` when it has none.

    Raises :class:`GoalsUnreadable` for a record that exists and cannot be read,
    so a writer never overwrites it and a reader can say so.
    """
    data = _read_raw(slug)
    if data is None or not _owned_by(data, slug, member):
        return empty_record()
    return _normalize(data)


def _payload(slug: str, member: str, rec: dict[str, Any]) -> str:
    """The exact text :func:`_write` stores for *rec*."""
    from kiro_crew.members import _stable_member_slug

    payload = {
        "member": member,
        "member_id": slug if _stable_member_slug(slug, member) else "",
        "slug": slug,
        **rec,
    }
    return json.dumps(payload, ensure_ascii=False)


def _write_pinned(path: Path, data: bytes) -> None:
    d = _open_goals_dir(create=True)
    assert d is not None
    tmp = f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600, dir_fd=d)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            # rename(2) replaces a symlink at the target name itself; it never
            # writes through one.
            os.replace(tmp, path.name, src_dir_fd=d, dst_dir_fd=d)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=d)
            except OSError:
                pass
            raise
        try:
            os.fsync(d)
        except OSError:
            logger.debug("could not fsync the goals dir", exc_info=True)
    finally:
        os.close(d)


#: Called as ``listener(slug, member, record)`` after each write of a goal record.
_change_listeners: list[Any] = []


def add_change_listener(listener: Any) -> None:
    """Call *listener* after every write of a goal record, with the record written.

    The gateway registers one (``dashboard.crewmate_feed``) to record the goal into
    the crewmate's crew log for its Dashboard page. A listener runs on the writing
    thread and must hand real work elsewhere; one that raises is logged and skipped.
    """
    _change_listeners.append(listener)


def _notify_change(slug: str, member: str, rec: dict[str, Any]) -> None:
    for listener in list(_change_listeners):
        try:
            listener(slug, member, rec)
        except Exception:
            logger.debug("goal change listener failed for %s", slug, exc_info=True)


def _write(slug: str, member: str, rec: dict[str, Any]) -> None:
    _write_record(slug, member, rec)
    _notify_change(slug, member, rec)


def _write_record(slug: str, member: str, rec: dict[str, Any]) -> None:
    path = member_goals_path(slug)
    data = _payload(slug, member, rec)
    try:
        if _PINNED:
            _write_pinned(path, data.encode("utf-8"))
            return
        _check_unlinked(path)
    except _Linked as exc:
        raise GoalsUnreadable(f"goal record for {slug!r} is behind a symlink") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_unlinked(path)
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    atomic_write(path, data, fsync=True)
    fsync_dir(path.parent, best_effort=True)


def _mutate(slug: str, member: str, fn: Any) -> Any:
    """Read, change and write *member*'s record under its lock; returns ``fn``'s value."""
    with _lock_for(slug):
        data = _read_raw(slug)
        if data is not None and not _owned_by(data, slug, member):
            # Another exact name's record under a colliding slug: never written
            # over from here. The owner's routes refuse ambiguous slugs first.
            raise GoalError("slug_collision", "another crewmate's goal is stored under this name")
        rec = _normalize(data) if data is not None else empty_record()
        result = fn(rec)
        if _fit(slug, member, rec) and isinstance(result, dict) and "record" in result:
            result["record"] = copy.deepcopy(rec)
        _write(slug, member, rec)
        return result


def _fit(slug: str, member: str, rec: dict[str, Any]) -> bool:
    """Prune the oldest readings until *rec* fits :data:`_RECORD_MAX_BYTES`.

    The read cap and this budget are the same number, so every record written
    is readable. Readings are the only part that grows without a count cap the
    user sees; when the record is still too big with none left, the write is
    refused (``record_too_large``). Returns whether anything was pruned.
    """
    pruned = False
    while len(_payload(slug, member, rec).encode("utf-8")) > _RECORD_MAX_BYTES:
        oldest = None
        for kr in rec["key_results"]:
            if kr.get("readings") and (
                oldest is None or kr["readings"][0]["at"] < oldest["readings"][0]["at"]
            ):
                oldest = kr
        removed = rec.get("removed_key_result")
        if isinstance(removed, dict) and removed["kr"].get("readings"):
            if oldest is None or removed["kr"]["readings"][0]["at"] < oldest["readings"][0]["at"]:
                oldest = removed["kr"]
        if oldest is None:
            raise GoalError("record_too_large", "the goal record is too large to save")
        oldest["readings"].pop(0)
        pruned = True
    return pruned


def keep_unscrubbed(stored: str, sent: str) -> str:
    """*stored* when *sent* is only its scrubbed form, else *sent*.

    Every goal string leaves the record through :func:`scrub_text`, so the
    page, the tools and a card's public Undo body hold the scrubbed form. Writing
    that form back unchanged would replace the real text with redaction
    placeholders the user never typed; this keeps the stored text instead.
    """
    if sent != stored and sent == scrub_text(stored):
        return stored
    return sent


def _new_goal_id(taken: set[str]) -> str:
    while True:
        gid = "g" + secrets.token_hex(4)
        if gid not in taken and gid != LEGACY_GOAL_ID:
            return gid


def _strip_goal(rec: dict[str, Any], gid: str) -> list[str]:
    """Take *gid* off every key result; returns the ids of those that served it."""
    served = []
    for kr in rec["key_results"]:
        if gid in kr.get("goal_ids", []):
            kr["goal_ids"] = [g for g in kr["goal_ids"] if g != gid]
            served.append(kr["id"])
    return served


def _no_goals_left(rec: dict[str, Any]) -> None:
    # A crewmate with no goal has nothing to be reminded of.
    if not rec["goals"]:
        for cadence in CADENCES:
            rec["schedules"][cadence] = False


def set_goal(
    slug: str,
    member: str,
    text: Any,
    *,
    by: str,
    goal_id: str | None = None,
    expected: str | None = None,
    restore_from: str | None = None,
) -> dict[str, Any]:
    """Add a goal, or change or clear the goal *goal_id*.

    Returns ``{"before", "after", "goal_id", "added", "record"}``. Without
    *goal_id* the text is added as a new goal (``duplicate_goal`` when another
    goal already reads the
    same); *expected* may then only be ``""``, "the crewmate has no goal yet",
    which refuses (``changed_meanwhile``) once it has one. With *goal_id* the
    text replaces that goal's (``goal_not_found`` when there is none), an empty
    text removes it and strips it from every key result, and *expected* is a
    compare-and-set on that goal's text, so a card's Apply or Undo never
    overwrites a newer edit. A record left with no goal turns every check-in off.

    A text that is only the scrubbed form of the stored goal keeps the stored
    text (:func:`keep_unscrubbed`). *restore_from* is a card Undo's raw
    pre-change text of that goal, read server-side from the card's private
    snapshot: the plan's body carries it scrubbed, and the raw text is what
    goes back.
    """
    clean = _clean_text(text, max_chars=GOAL_TEXT_MAX_CHARS, multiline=True, what="the goal")
    if goal_id is not None and not is_goal_id(goal_id):
        raise GoalError("invalid_goal_id", "a goal id is g and 8 hex digits")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        if goal_id is None:
            if expected is not None and (expected != "" or rec["goals"]):
                raise GoalError("changed_meanwhile", "the goals were changed since this was shown")
            if not clean:
                raise GoalError("empty_text", "a goal needs some text")
            if any(clean in (g["text"], scrub_text(g["text"])) for g in rec["goals"]):
                raise GoalError("duplicate_goal", "another goal already says this")
            gid = _new_goal_id(set(goal_ids(rec)))
            rec["goals"].append(
                {"id": gid, "text": clean, "created_at": now, "updated_at": now, "updated_by": by}
            )
            return {
                "before": "",
                "after": clean,
                "goal_id": gid,
                "added": True,
                "record": copy.deepcopy(rec),
            }
        goal = next((g for g in rec["goals"] if g["id"] == goal_id), None)
        if goal is None:
            raise GoalError("goal_not_found", "no goal has that id")
        before = goal["text"]
        # The page holds the scrubbed text it was shown (public_view).
        if expected is not None and expected not in (before, scrub_text(before)):
            raise GoalError("changed_meanwhile", "the goal was changed since this was shown")
        new = keep_unscrubbed(restore_from if restore_from is not None else before, clean)
        if not new:
            rec["goals"] = [g for g in rec["goals"] if g["id"] != goal_id]
            _strip_goal(rec, goal_id)
            _no_goals_left(rec)
        else:
            if any(
                new in (g["text"], scrub_text(g["text"]))
                for g in rec["goals"]
                if g["id"] != goal_id
            ):
                raise GoalError("duplicate_goal", "another goal already says this")
            goal.update({"text": new, "updated_at": now, "updated_by": by})
        return {
            "before": before,
            "after": new,
            "goal_id": goal_id,
            "added": False,
            "record": copy.deepcopy(rec),
        }

    return _mutate(slug, member, fn)


def remove_goal(slug: str, member: str, goal_id: Any) -> dict[str, Any]:
    """Remove one goal, keeping it (and which key results served it) for one Undo.

    Its id comes off every key result. Only the newest removal is kept;
    :func:`restore_goal` puts it back while no goal reads the same.
    """

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        index = next((n for n, g in enumerate(rec["goals"]) if g["id"] == goal_id), None)
        if index is None:
            raise GoalError("goal_not_found", "no goal has that id")
        goal = rec["goals"].pop(index)
        served = _strip_goal(rec, goal["id"])
        rec["removed_goal"] = {
            "goal": copy.deepcopy(goal),
            "index": index,
            "at": _now(),
            "key_result_ids": served,
        }
        _no_goals_left(rec)
        return {"removed": copy.deepcopy(goal), "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def restore_goal(slug: str, member: str, goal_id: Any) -> dict[str, Any]:
    """Put back the goal :func:`remove_goal` removed, with the links still possible.

    A key result that served it and is still in the set serves it again.
    """

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        removed = rec.get("removed_goal")
        if not isinstance(removed, dict) or removed["goal"]["id"] != goal_id:
            raise GoalError("undo_unavailable", "that removal can no longer be undone")
        goal = removed["goal"]
        if any(g["id"] == goal["id"] for g in rec["goals"]) or any(
            g["text"] == goal["text"] for g in rec["goals"]
        ):
            rec["removed_goal"] = None
            return {"stale": True}
        rec["goals"].insert(min(int(removed["index"]), len(rec["goals"])), copy.deepcopy(goal))
        for kr in rec["key_results"]:
            if kr["id"] in removed["key_result_ids"] and goal["id"] not in kr["goal_ids"]:
                kr["goal_ids"].append(goal["id"])
        rec["removed_goal"] = None
        return {"restored": copy.deepcopy(goal), "record": copy.deepcopy(rec)}

    result = _mutate(slug, member, fn)
    if result.get("stale"):
        raise GoalError("stale_undo", "the goals changed since; nothing was undone")
    return result


def key_result_shape(items: Any) -> list[dict[str, str]]:
    """The ``{text, target}`` of each key result: what a set's compare-and-set compares."""
    return [
        {"text": str(i.get("text", "")), "target": str(i.get("target", ""))}
        for i in (items or [])
        if isinstance(i, dict)
    ]


def _clean_kr_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise GoalError("invalid_key_results", "key results must be a list")
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise GoalError("invalid_key_results", "each key result is {text, target, current?}")
        text = _clean_text(
            raw.get("text"), max_chars=KR_TEXT_MAX_CHARS, multiline=False, what="a key result"
        )
        target = _clean_text(
            raw.get("target"), max_chars=KR_VALUE_MAX_CHARS, multiline=False, what="a target"
        )
        current = _clean_text(
            raw.get("current", ""),
            max_chars=KR_READING_MAX_CHARS,
            multiline=False,
            what="a reading",
        )
        if not text or not target:
            raise GoalError("invalid_key_results", "each key result needs a text and a target")
        if any((k["text"], k["target"]) == (text, target) for k in out):
            # A key result is known by its text and target: two the same would
            # share one id and one set of readings.
            raise GoalError("duplicate_key_result", "two key results have the same text and target")
        item: dict[str, Any] = {"text": text, "target": target, "current": current}
        links = raw.get("goal_ids")
        if links is not None:
            # The goals this key result serves; set_key_results checks each is
            # one of the record's goals. Absent: a key result kept from the set
            # keeps the goals it served.
            if not isinstance(links, list) or not all(is_goal_id(g) for g in links):
                raise GoalError("invalid_key_results", "goal_ids is a list of goal ids")
            item["goal_ids"] = list(dict.fromkeys(links))
        kid = raw.get("id")
        if kid is not None:
            # The owner's editor names the key result it edits, so a reworded
            # one keeps its readings; set_key_results checks it is in the set.
            if not isinstance(kid, str) or not _KR_ID_RE.match(kid):
                raise GoalError("invalid_key_results", "a key result id is k and 8 hex digits")
            if any(k.get("id") == kid for k in out):
                raise GoalError("duplicate_key_result", "two key results have the same id")
            item["id"] = kid
        out.append(item)
    return out


def set_key_results(
    slug: str,
    member: str,
    items: Any,
    *,
    expected: Any = None,
    expected_ids: list[str] | None = None,
    expected_links: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Replace the whole set of key results. Returns ``{"before", "record"}``.

    *expected* is the ``{text, target}`` list (:func:`key_result_shape`) of the
    set this write replaces; the write is refused (``changed_meanwhile``) when the
    stored set has another shape, so a card never overwrites a newer edit.
    A key result kept with the same text and target keeps its id and reading.
    An item that names an ``id`` (the owner's editor does) must be one of the
    stored key results (``unknown_key_result``); it keeps that id, reading and
    readings under its new text and target. Card Apply also supplies the ids
    and goal links from its preview; these are checked under the write lock,
    without comparing readings.
    """
    clean = _clean_kr_items(items)
    if expected is not None and not (
        isinstance(expected, list)
        and all(
            isinstance(k, dict)
            and isinstance(k.get("text"), str)
            and isinstance(k.get("target"), str)
            for k in expected
        )
    ):
        raise GoalError("invalid_expected", "expected must be the key results shown")
    if expected_ids is not None and not (
        isinstance(expected_ids, list)
        and all(isinstance(k, str) and _KR_ID_RE.fullmatch(k) for k in expected_ids)
        and len(set(expected_ids)) == len(expected_ids)
    ):
        raise GoalError("invalid_expected", "expected_ids must be the key result ids shown")
    if expected_links is not None and not (
        isinstance(expected_links, dict)
        and all(
            isinstance(k, str)
            and _KR_ID_RE.fullmatch(k)
            and isinstance(v, list)
            and all(is_goal_id(g) for g in v)
            for k, v in expected_links.items()
        )
        and (expected_ids is None or set(expected_links) == set(expected_ids))
    ):
        raise GoalError("invalid_expected", "expected_links must map key result ids to goals")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        before = copy.deepcopy(rec["key_results"])
        shown = key_result_shape([_scrub_kr(k) for k in before])
        if (
            expected is not None
            and key_result_shape(expected) not in (key_result_shape(before), shown)
            or expected_ids is not None
            and [k["id"] for k in before] != expected_ids
            or expected_links is not None
            and {k["id"]: sorted(k.get("goal_ids") or []) for k in before}
            != {k: sorted(v) for k, v in expected_links.items()}
        ):
            raise GoalError(
                "changed_meanwhile", "the key results were changed since this was shown"
            )
        if clean and not has_goal(rec):
            raise GoalError("no_goal", "set a goal before its key results")
        valid = set(goal_ids(rec))
        if any(g not in valid for item in clean for g in item.get("goal_ids", [])):
            raise GoalError("unknown_goal", "a key result names a goal that is not in the list")
        by_id = {k["id"]: k for k in before}
        named = {item["id"] for item in clean if "id" in item}
        if not named <= set(by_id):
            raise GoalError("unknown_key_result", "no such key result in this set")
        # Unnamed items match by text and target, never one an item named by id.
        kept = {(k["text"], k["target"]): k for k in before if k["id"] not in named}
        # The page and the tools hold the scrubbed text: match that form too.
        for k in before:
            if k["id"] not in named:
                kept.setdefault((scrub_text(k["text"]), scrub_text(k["target"])), k)
        taken = {k["id"] for k in before}
        now = _now()
        out = []
        for raw in clean:
            item = {k: v for k, v in raw.items() if k not in ("id", "goal_ids")}
            old = by_id[raw["id"]] if "id" in raw else kept.get((item["text"], item["target"]))
            if "goal_ids" in raw:
                links = list(raw["goal_ids"])
            elif old is not None:
                links = list(old.get("goal_ids") or [])
            else:
                links = []
            if old is not None:
                item["text"] = keep_unscrubbed(old["text"], item["text"])
                item["target"] = keep_unscrubbed(old["target"], item["target"])
            if old is not None and not item["current"]:
                out.append(
                    {**old, "text": item["text"], "target": item["target"], "goal_ids": links}
                )
                continue
            kid = old["id"] if old is not None else _new_kr_id(taken)
            taken.add(kid)
            readings = list(old.get("readings") or []) if old is not None else []
            if item["current"]:
                readings.append({"value": item["current"], "at": now})
            out.append(
                {
                    "id": kid,
                    **item,
                    "updated_at": now if item["current"] else 0.0,
                    "readings": _prune_readings(readings, now),
                    "goal_ids": links,
                }
            )
        rec["key_results"] = out
        # Undo of a removal covers only the set it was removed from.
        rec["removed_key_result"] = None
        return {"before": before, "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def restore_key_results(
    slug: str,
    member: str,
    records: Any,
    *,
    expected: Any = None,
    expected_ids: list[str] | None = None,
    expected_links: dict[str, list[str]] | None = None,
    raw_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Put back a whole earlier set of key results, ids and readings included.

    *expected_links* maps each id the card's write left to the goals it served
    then: a newer card that changed only those links is refused, not undone.

    *raw_records* are the same pre-change records unscrubbed, read server-side
    from the card's private snapshot: a record in *records* that is exactly the
    scrubbed form of the raw one under its id is put back raw, so an Undo never
    stores redaction placeholders in place of the real text.

    A key-results card's Undo: *records* are the records the card read before it
    applied (scrubbed, since they travel in the card's public plan), *expected*
    the ``{text, target}`` set it applied and *expected_ids* the ids its write
    left, so the same compare-and-set as :func:`set_key_results` refuses a newer
    edit, and a set recreated since with the same wording (new ids, new
    readings) is refused too rather than replaced. A key result the card kept is
    still in the set under its id: it stays exactly as it is now, readings
    recorded since the card included. Only the ones the card removed come back
    from *records*.
    """
    if not isinstance(records, list):
        raise GoalError("invalid_key_results", "the key results to restore are malformed")
    if not all(_valid_kr(r) for r in records):
        raise GoalError("invalid_key_results", "the key results to restore are malformed")
    if len({r["id"] for r in records}) != len(records) or not all(
        _KR_ID_RE.match(r["id"]) for r in records
    ):
        raise GoalError("invalid_key_results", "the key results to restore are malformed")
    clean = _loose_krs(records)
    if raw_records:
        raw = {r["id"]: r for r in _loose_krs(raw_records)}
        clean = [
            (raw[k["id"]] if k["id"] in raw and _scrub_kr(raw[k["id"]]) == _scrub_kr(k) else k)
            for k in clean
        ]

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        before = copy.deepcopy(rec["key_results"])
        shown = key_result_shape([_scrub_kr(k) for k in before])
        if expected is not None and key_result_shape(expected) not in (
            key_result_shape(before),
            shown,
        ):
            raise GoalError(
                "changed_meanwhile", "the key results were changed since this was shown"
            )
        if expected_ids is not None and [k["id"] for k in before] != list(expected_ids):
            raise GoalError(
                "changed_meanwhile", "the key results were changed since this was shown"
            )
        if expected_links is not None and {
            k["id"]: sorted(k.get("goal_ids") or []) for k in before
        } != {kid: sorted(v) for kid, v in expected_links.items()}:
            # A newer card that changed only which goals a milestone serves.
            raise GoalError(
                "changed_meanwhile", "the key results were changed since this was shown"
            )
        if clean and not has_goal(rec):
            raise GoalError("no_goal", "set a goal before its key results")
        now = _now()
        valid = set(goal_ids(rec))
        live = {k["id"]: k for k in rec["key_results"]}
        # A kept key result keeps its readings as they are now and goes back to
        # the goals it served before the card; a goal removed since is not put back.
        rec["key_results"] = [
            {
                **(
                    copy.deepcopy(live[k["id"]])
                    if k["id"] in live
                    else {**k, "readings": _prune_readings(k["readings"], now)}
                ),
                "goal_ids": _clean_goal_ids(k.get("goal_ids"), valid),
            }
            for k in clean
        ]
        rec["removed_key_result"] = None
        return {"before": before, "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def remove_key_result(slug: str, member: str, kr_id: Any) -> dict[str, Any]:
    """Remove one key result, keeping it (with its readings) for one Undo.

    Only the newest removal is kept; :func:`restore_key_result` puts it back
    while the set holds no key result with the same text and
    target, and any later write to the set (:func:`set_key_results`) ends it.
    """

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        index = next((n for n, k in enumerate(rec["key_results"]) if k["id"] == kr_id), None)
        if index is None:
            raise GoalError("key_result_not_found", "no key result has that id")
        kr = rec["key_results"].pop(index)
        rec["removed_key_result"] = {"kr": copy.deepcopy(kr), "index": index, "at": _now()}
        return {"removed": copy.deepcopy(kr), "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def restore_key_result(slug: str, member: str, kr_id: Any) -> dict[str, Any]:
    """Put back the key result :func:`remove_key_result` removed, readings and all."""

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        removed = rec.get("removed_key_result")
        if not isinstance(removed, dict) or removed["kr"]["id"] != kr_id:
            raise GoalError("undo_unavailable", "that removal can no longer be undone")
        kr = removed["kr"]
        shape = (kr["text"], kr["target"])
        if (
            not has_goal(rec)
            or any((k["text"], k["target"]) == shape for k in rec["key_results"])
            or any(k["id"] == kr["id"] for k in rec["key_results"])
        ):
            # Refused, but the dead Undo is cleared on disk first: it can never
            # succeed now, and a kept one would refuse every later click too.
            rec["removed_key_result"] = None
            return {"stale": True}
        index = min(int(removed["index"]), len(rec["key_results"]))
        rec["key_results"].insert(index, copy.deepcopy(kr))
        rec["removed_key_result"] = None
        return {"restored": copy.deepcopy(kr), "record": copy.deepcopy(rec)}

    result = _mutate(slug, member, fn)
    if result.get("stale"):
        raise GoalError("stale_undo", "the key results changed since; nothing was undone")
    return result


def _new_kr_id(taken: set[str]) -> str:
    while True:
        kid = "k" + secrets.token_hex(4)
        if kid not in taken:
            return kid


def record_key_result(slug: str, member: str, kr_id: Any, current: Any) -> dict[str, Any]:
    """Record a new reading for one key result (the crewmate's, no sign-off)."""
    if not isinstance(kr_id, str) or not _KR_ID_RE.match(kr_id):
        raise GoalError("invalid_id", "that is not a key result id")
    reading = _clean_text(
        current, max_chars=KR_READING_MAX_CHARS, multiline=False, what="a reading"
    )
    if not reading:
        raise GoalError("empty_text", "a reading needs a value")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        for item in rec["key_results"]:
            if item["id"] == kr_id:
                now = _now()
                item["current"] = reading
                item["updated_at"] = now
                item["readings"] = _prune_readings(
                    [*item.get("readings", []), {"value": reading, "at": now}], now
                )
                return {"item": dict(item), "record": copy.deepcopy(rec)}
        raise GoalError("key_result_not_found", "no key result has that id")

    return _mutate(slug, member, fn)


def is_check_in_time(value: str) -> bool:
    """Whether *value* is a check-in time the record accepts (``HH:MM``, 24-hour)."""
    return bool(_TIME_RE.match(value))


def is_heartbeat_interval(value: Any) -> bool:
    """Whether *value* is one of :data:`HEARTBEAT_INTERVALS` (an int, never a bool)."""
    return isinstance(value, int) and not isinstance(value, bool) and value in HEARTBEAT_INTERVALS


def _validate_schedules(changes: Any) -> None:
    allowed = {*CADENCES, "time", "timezone", "weekday", "month_day", "interval"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise GoalError("invalid_schedule", "give switches or timing to change")
    for cadence in CADENCES:
        if cadence in changes and not isinstance(changes[cadence], bool):
            raise GoalError("invalid_schedule", f"{cadence} must be true or false")
    if "time" in changes and (
        not isinstance(changes["time"], str) or not _TIME_RE.match(changes["time"])
    ):
        raise GoalError("invalid_time", "time must be HH:MM (24-hour)")
    if "timezone" in changes and (
        not isinstance(changes["timezone"], str) or len(changes["timezone"]) > 64
    ):
        raise GoalError("invalid_timezone", "timezone must be an IANA name")
    weekday = changes.get("weekday", 0)
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise GoalError("invalid_weekday", "weekday must be 0 (Sunday) to 6")
    day = changes.get("month_day", 1)
    if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= MAX_MONTH_DAY:
        raise GoalError("invalid_month_day", f"month_day must be 1 to {MAX_MONTH_DAY}")
    if "interval" in changes and not is_heartbeat_interval(changes["interval"]):
        raise GoalError(
            "invalid_interval",
            "interval must be one of " + ", ".join(str(m) for m in HEARTBEAT_INTERVALS),
        )


def set_schedules(
    slug: str,
    member: str,
    changes: dict[str, Any],
    *,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Change the check-in switches and their timing. Returns the record.

    *changes* holds any of ``daily``/``weekly``/``monthly``/``heartbeat``
    (bool), ``time`` (``HH:MM``), ``timezone`` (IANA name), ``weekday`` (0-6,
    0 = Sunday), ``month_day`` (1-28) and ``interval`` (the heartbeat's minutes,
    one of :data:`HEARTBEAT_INTERVALS`). Turning any switch on needs a goal
    (``no_goal``). Only the owner's route and an applied card call this, so the
    crewmate never turns a switch on by itself.
    When supplied, *expected* covers every written field and is compared under
    the write lock; omitted fields stay untouched. No expectation keeps the
    ordinary owner's partial-update behavior.
    """
    _validate_schedules(changes)
    if expected is not None:
        try:
            _validate_schedules(expected)
        except GoalError:
            raise GoalError("invalid_expected", "expected must be the schedules shown") from None
        if not set(changes) <= set(expected):
            raise GoalError("invalid_expected", "expected must cover every changed field")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        if expected is not None and any(rec["schedules"].get(k) != v for k, v in expected.items()):
            raise GoalError("changed_meanwhile", "the check-ins were changed since this was shown")
        if not has_goal(rec) and any(changes.get(c) is True for c in CADENCES):
            raise GoalError("no_goal", "set a goal before turning a check-in on")
        rec["schedules"].update(changes)
        return copy.deepcopy(rec)

    return _mutate(slug, member, fn)


def record_report(slug: str, member: str, *, kind: str, artifact: str, title: str) -> None:
    """Remember a saved report so the Goals tab can list it."""
    if kind not in REPORT_KINDS:
        raise GoalError("invalid_report_kind", "a report is weekly or monthly")

    def fn(rec: dict[str, Any]) -> None:
        rec["reports"] = [
            *rec["reports"],
            {"kind": kind, "slug": artifact, "title": title[:REPORT_TITLE_MAX_CHARS], "at": _now()},
        ][-MAX_REPORTS:]

    _mutate(slug, member, fn)


def key_result_movement(rec: dict[str, Any], *, since: float) -> list[dict[str, Any]]:
    """Per key result: its reading at the start of the window and now, from ``readings``.

    The start is the last reading at or before *since* (else the first in the
    window); ``None`` when the key result was never read in or before it.
    """
    out = []
    for kr in rec["key_results"]:
        readings = sorted(kr.get("readings") or [], key=lambda r: r["at"])
        before = [r for r in readings if r["at"] <= since]
        inside = [r for r in readings if r["at"] > since]
        start: dict[str, Any] | None
        if before:
            start = before[-1]
        elif inside:
            start = inside[0]
        else:
            start = None
        out.append(
            {
                "id": kr["id"],
                "text": kr["text"],
                "target": kr["target"],
                "start": start["value"] if start else None,
                "now": kr["current"] or None,
                "readings_in_window": len(inside),
            }
        )
    return out


#: A number in a target or reading, the comparison a target may lead with, and
#: the unit word right after it ("4 fixes", "0 failing", "85%").
_NUMBER_RE = re.compile(
    r"(?P<cmp><=|>=|≤|≥|<|>)?\s*(?P<num>-?\d+(?:[.,]\d+)?)\s*%?(?:\s*(?P<unit>[^\W\d_]+))?"
)
#: Words after a number that are not its unit ("3 of 5", "1 or more").
_NOT_UNITS = frozenset({"of", "or", "out", "and", "to", "in", "on", "per"})
#: A reading that says the target is reached, whatever the target's form.
_DONE_WORDS = frozenset({"met", "done", "yes", "reached", "achieved", "complete", "completed"})
#: Phrases in a target that ask for at least its number, before or after it.
_AT_LEAST = re.compile(
    r"\b(?:at least|no fewer than|over|above|more than)\s*-?\d|\d\S*\s*(?:or more|\+)"
)
#: Words in a milestone's text or target that make a smaller reading the better one.
_REDUCE_WORDS = re.compile(
    r"\b(?:reduce[ds]?|reducing|cut|lower|fewer|less|below|under|at most|no more than|"
    r"up to|max|down to|shrink|drop|remaining|left|open|failing|errors?|mismatch(?:es)?)\b"
)


def _number(text: str) -> tuple[str, float, str] | None:
    m = _NUMBER_RE.search(text)
    if m is None:
        return None
    unit = (m.group("unit") or "").casefold()
    if unit in _NOT_UNITS:
        unit = ""
    return m.group("cmp") or "", float(m.group("num").replace(",", ".")), unit


def _same_unit(a: str, b: str) -> bool:
    """Whether two unit words name one thing: either is absent, or they match up to a plural."""
    if not a or not b:
        return True
    return a.rstrip("s").removesuffix("e") == b.rstrip("s").removesuffix("e")


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold())


def key_result_met(kr: dict[str, Any]) -> bool:
    """Whether *kr*'s latest reading reaches its target, judged from the record alone.

    A reading is free text, so this answers only what the text settles:

    * a reading that leads with a word saying so (``done``, ``met``, ``yes``...)
      and carries no number;
    * a word target the reading repeats from its start ("all docs in sync");
    * numbers, when both the reading and the target carry one and their unit
      words agree (a missing unit agrees with any): the target's own comparison
      when it names one ("<= 2", "> 80%", "1 or more", "at most 3"); otherwise
      fewer is better when the milestone's text or target speaks of reducing
      (reduce, below, at most, left, open, failing...) or the target is 0, and
      more is better in every other case ("4 fixes proposed" meets "3").

    Anything else is not met: a reading this cannot place reads as still open.
    """
    current = str(kr.get("current") or "").strip()
    target = str(kr.get("target") or "").strip()
    if not current or not target:
        return False
    words = _words(current)
    target_words = _words(target)
    cur_num = _number(current)
    if words and words[0] in _DONE_WORDS and cur_num is None:
        return True
    tgt_num = _number(target)
    if tgt_num is None or cur_num is None:
        return bool(target_words) and words[: len(target_words)] == target_words
    cmp, goal, goal_unit = tgt_num
    _cmp, now, now_unit = cur_num
    if not _same_unit(now_unit, goal_unit):
        return False
    if cmp in ("<=", "≤"):
        return now <= goal
    if cmp in (">=", "≥"):
        return now >= goal
    if cmp == "<":
        return now < goal
    if cmp == ">":
        return now > goal
    low_target = target.casefold()
    if _AT_LEAST.search(low_target):
        return now >= goal
    reduce = goal == 0 or _REDUCE_WORDS.search(f"{kr.get('text') or ''} {target}".casefold())
    return now <= goal if reduce else now >= goal


def milestones_all_met(rec: dict[str, Any]) -> bool:
    """Whether a crewmate with a goal has no open milestone: none set, or every one met."""
    return has_goal(rec) and all(key_result_met(kr) for kr in rec["key_results"])


def _removed_view(removed: Any) -> dict[str, Any] | None:
    if not isinstance(removed, dict):
        return None
    kr = removed["kr"]
    return {"id": kr["id"], "text": scrub_text(kr["text"]), "at": removed["at"]}


def scrub_text(text: Any) -> str:
    """*text* through the dashboard's credential and exfiltration-URL scrubbers.

    Every goal string can be the crewmate's own writing, so each one that leaves
    the record for a page, a tool result or a report goes through here.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    return redact_credentials(redact_exfiltration_urls(str(text or ""))[0])[0]


def _scrub_kr(kr: dict[str, Any]) -> dict[str, Any]:
    out = dict(kr)
    for key in ("text", "target", "current"):
        out[key] = scrub_text(kr.get(key, ""))
    out["readings"] = [
        {**r, "value": scrub_text(r.get("value", ""))} for r in kr.get("readings") or []
    ]
    return out


def _goal_view(goal: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": goal["id"],
        "text": scrub_text(goal["text"]),
        "created_at": goal["created_at"] or None,
        "updated_at": goal["updated_at"] or None,
        "updated_by": goal["updated_by"] or None,
    }


def _removed_goal_view(removed: Any) -> dict[str, Any] | None:
    if not isinstance(removed, dict):
        return None
    goal = removed["goal"]
    return {"id": goal["id"], "text": scrub_text(goal["text"]), "at": removed["at"]}


def public_view(rec: dict[str, Any]) -> dict[str, Any]:
    """The record as the Goals tab and ``goal_read`` see it, scrubbed."""
    return {
        "goals": [{**_goal_view(g), "number": n} for n, g in enumerate(rec["goals"], start=1)],
        "key_results": [_scrub_kr(k) for k in rec["key_results"]],
        "schedules": dict(rec["schedules"]),
        "reports": sorted(
            ({**r, "title": scrub_text(r["title"])} for r in rec["reports"]),
            key=lambda r: -float(r["at"]),
        ),
        "removed_key_result": _removed_view(rec.get("removed_key_result")),
        "removed_goal": _removed_goal_view(rec.get("removed_goal")),
        "limits": {
            "goal_text_max_chars": GOAL_TEXT_MAX_CHARS,
            "key_result_text_max_chars": KR_TEXT_MAX_CHARS,
            "key_result_value_max_chars": KR_VALUE_MAX_CHARS,
            "key_result_reading_max_chars": KR_READING_MAX_CHARS,
        },
    }


#: Cap on the goal block injected into the crewmate's context, so a long goal
#: never crowds the prompt.
CONTEXT_MAX_CHARS = 4000


def goal_lines(rec: dict[str, Any]) -> list[str]:
    """``"Goals:"`` and one numbered line per goal, or nothing without a goal.

    The numbers are the ones the Goals tab shows, so "goal 2" means the same
    thing to the crewmate and to the user.
    """
    if not rec["goals"]:
        return []
    return ["Goals:", *(f"{n}. {g['text']}" for n, g in enumerate(rec["goals"], start=1))]


def serves_note(rec: dict[str, Any], kr: dict[str, Any]) -> str:
    """``" (serves goals 1, 3)"`` for a key result's linked goals, or ``""``."""
    numbers = goal_numbers(rec)
    linked = [str(numbers[g]) for g in kr.get("goal_ids") or [] if g in numbers]
    if not linked:
        return ""
    return f" (serves goal{'s' if len(linked) > 1 else ''} {', '.join(linked)})"


def _cap(text: str) -> str:
    if len(text) > CONTEXT_MAX_CHARS:
        text = text[:CONTEXT_MAX_CHARS].rstrip() + "\n[... goals cut at their cap]"
    return text


def progress_block(rec: dict[str, Any]) -> str:
    """The goals with each key result's latest reading, for a one-off check-in turn.

    Unlike :func:`context_block` this carries the readings: a check-in prompt is
    sent once, so a changing value costs nothing on the turns after it.
    """
    lines = goal_lines(rec)
    if rec["key_results"]:
        lines.append("Key results (id: text — reading / done when):")
        lines.extend(
            f"- {k['id']}: {k['text']} — {k['current'] or 'not read yet'} / {k['target']}"
            + serves_note(rec, k)
            for k in rec["key_results"]
        )
    return _cap("\n".join(lines))


def context_block(rec: dict[str, Any]) -> str:
    """The goals text for the member section, or ``""`` when there is no goal.

    Every goal, numbered as the Goals tab numbers them, and the key results'
    text, target and the goals each serves, never their readings. The section
    rides every turn and is dropped from the wire while
    it is byte-identical to what the conversation already acknowledged
    (:mod:`kiro_crew.essential_delivery`); a reading in here would change it on
    every ``key_result_progress`` and resend the whole section, identity, rules
    and briefing included. The crewmate reads current values with ``goal_read``.
    """
    lines = goal_lines(rec)
    if rec["key_results"]:
        lines.append("Key results (id: text — done when):")
        lines.extend(
            f"- {k['id']}: {k['text']} — {k['target']}" + serves_note(rec, k)
            for k in rec["key_results"]
        )
    return _cap("\n".join(lines))

"""What the conductor work ledger accepts, and how it judges an item.

The field checks every write runs before its first byte lands (text bounds, choices,
acceptance, artifacts, PR numbers, counts), and the read-side predicates: who may conduct
and at what depth, whether an item is orphaned or stale, whether its acceptance is
concrete, and how a stored stamp parses. :mod:`kiro_crew.work_ledger` binds every name
defined here, so each one reads, imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from kiro_crew.work_ledger_records import (
    ACCEPTANCE_CHECK_NAME,
    ACCEPTANCE_CHECK_REF,
    ACCEPTANCE_GH_REPO,
    ACCEPTANCE_KINDS,
    ACCEPTANCE_PLACEHOLDERS,
    ACCEPTANCE_READ_FIELDS,
    CODE_DEPTH_EXCEEDED,
    CODE_FIELD_TOO_LONG,
    CODE_INVALID_VALUE,
    CODE_WAKE_AT_IN_PAST,
    CODE_WAKE_AT_INVALID,
    CODE_WAKE_AT_REQUIRES_PROGRESS,
    CODE_WAKE_AT_TOO_FAR,
    DEFAULT_STALE_WINDOW_SECS,
    MAX_ARTIFACT_KEY_CHARS,
    MAX_ARTIFACT_KEYS,
    MAX_ARTIFACT_VALUE_CHARS,
    MAX_DEPTH,
    MAX_PR,
    MAX_RECORD_BYTES,
    MAX_WAKE_AT_SECS,
    MIN_PR,
    PENDING_BUDGET_SECS,
    STALE_ELIGIBLE_STATUSES,
    WAITING_STATUSES,
    WorkItem,
    WorkLedgerError,
    _finite_int,
    logger,
)
from kiro_crew.work_vocab import (
    WORK_MAX_DEPTH_MAX,
    WORK_MAX_DEPTH_MIN,
    WORK_MAX_DEPTH_SETTING,
)

# Validation. Every bound is checked BEFORE the first write, so a refusal leaves
# every file byte-identical.


def _require_text(value: Any, cap: int, name: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise WorkLedgerError(f"{name} is required", code=CODE_INVALID_VALUE, field=name)
        return ""
    if not isinstance(value, str):
        raise WorkLedgerError(f"{name} must be a string", code=CODE_INVALID_VALUE, field=name)
    if len(value) > cap:
        raise WorkLedgerError(
            f"{name} is {len(value)} chars; the cap is {cap}",
            code=CODE_FIELD_TOO_LONG,
            field=name,
        )
    return value


def _require_choice(value: Any, allowed: frozenset[str], name: str, code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise WorkLedgerError(
            f"{name} must be one of {sorted(allowed)}; got {value!r}",
            code=code,
            field=name,
        )
    return value


def _require_acceptance(value: Any) -> dict[str, Any]:
    """``acceptance`` is stored VERBATIM and never interpreted here.

    ``accept_eval.py`` is the only thing that decides whether an item passed, so
    this store validates the container and not the contents — a shape check here
    would be a second, drifting copy of that script's contract.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkLedgerError(
            "acceptance must be an object", code=CODE_INVALID_VALUE, field="acceptance"
        )
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WorkLedgerError(
            f"acceptance must be JSON-serialisable: {exc}",
            code=CODE_INVALID_VALUE,
            field="acceptance",
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_RECORD_BYTES // 2:
        raise WorkLedgerError(
            "acceptance is too large to store",
            code=CODE_FIELD_TOO_LONG,
            field="acceptance",
        )
    return value


def _require_artifacts(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkLedgerError(
            "artifacts must be an object", code=CODE_INVALID_VALUE, field="artifacts"
        )
    if len(value) > MAX_ARTIFACT_KEYS:
        raise WorkLedgerError(
            f"artifacts has {len(value)} keys; the cap is {MAX_ARTIFACT_KEYS}",
            code=CODE_FIELD_TOO_LONG,
            field="artifacts",
        )
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise WorkLedgerError(
                "artifacts must map strings to strings",
                code=CODE_INVALID_VALUE,
                field="artifacts",
            )
        if len(key) > MAX_ARTIFACT_KEY_CHARS:
            raise WorkLedgerError(
                f"artifacts key is {len(key)} chars; the cap is " f"{MAX_ARTIFACT_KEY_CHARS}",
                code=CODE_FIELD_TOO_LONG,
                field="artifacts",
            )
        if len(item) > MAX_ARTIFACT_VALUE_CHARS:
            raise WorkLedgerError(
                f"artifacts value is {len(item)} chars; the cap is " f"{MAX_ARTIFACT_VALUE_CHARS}",
                code=CODE_FIELD_TOO_LONG,
                field="artifacts",
            )
        out[key] = item
    return out


def _require_pr(value: Any) -> int | None:
    """A worker's CLAIMED pull-request number.

    A claim only: filling ``acceptance.pr`` from it would let a worker name any
    already-green pull request and pass. Promoting a claim into ``acceptance`` is a
    conductor action.
    """
    if value is None:
        return None
    number = _finite_int(value)
    if number is None or not (MIN_PR <= number <= MAX_PR):
        raise WorkLedgerError(
            f"pr must be an integer in {MIN_PR}..{MAX_PR}; got {value!r}",
            code=CODE_INVALID_VALUE,
            field="pr",
        )
    return number


def _require_wake_at(value: Any, status: str) -> str | None:
    """A report's ``wake_at``: an instant with a zone, at most a day ahead, on ``progress``.

    Returned as a UTC stamp at seconds precision, so every reader parses one shape and
    its width is that of the store's other stamps. ``None`` when the report names no
    wait. Each rule refuses with its own code.
    """
    if value is None:
        return None
    if status != "progress":
        raise WorkLedgerError(
            "wake_at is allowed only with status progress; a longer wait is 'blocked'",
            code=CODE_WAKE_AT_REQUIRES_PROGRESS,
            field="wake_at",
        )
    parsed = _parse_iso(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise WorkLedgerError(
            f"wake_at must be an ISO-8601 time with a zone; got {value!r}",
            code=CODE_WAKE_AT_INVALID,
            field="wake_at",
        )
    ahead = (parsed - datetime.now(timezone.utc)).total_seconds()
    if ahead <= 0:
        raise WorkLedgerError(
            f"wake_at {value!r} is not in the future", code=CODE_WAKE_AT_IN_PAST, field="wake_at"
        )
    if ahead > MAX_WAKE_AT_SECS:
        raise WorkLedgerError(
            f"wake_at {value!r} is more than a day ahead; a longer wait is a 'blocked' report",
            code=CODE_WAKE_AT_TOO_FAR,
            field="wake_at",
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _require_count(value: Any, name: str) -> int:
    number = _finite_int(value)
    if number is None or number < 0:
        raise WorkLedgerError(
            f"{name} must be a non-negative integer; got {value!r}",
            code=CODE_INVALID_VALUE,
            field=name,
        )
    return number


# Depth


#: The fallback reasons already warned about. ``max_depth`` runs on every ledger guard,
#: so an unreadable config would otherwise log the same line on each call; one warning
#: per distinct reason is enough for an operator to find it.
_WARNED_DEPTH_FALLBACKS: set[str] = set()


def _warn_depth_fallback(exc: BaseException) -> None:
    """Say, once per reason, that the configured cap could not be read.

    Without this an operator who set ``work_ledger.max_depth`` to 3 sees
    ``depth_exceeded`` at level 2 and nothing in the log. The warning names the value
    found in the config file (best effort) and the default now in force.
    """
    reason = f"{type(exc).__name__}: {exc}"
    if reason in _WARNED_DEPTH_FALLBACKS:
        return
    _WARNED_DEPTH_FALLBACKS.add(reason)
    configured: object = "<unreadable>"
    try:
        from kiro_crew.config.loader import _raw_config

        section = _raw_config().get("work_ledger")
        configured = section.get("max_depth", "<unset>") if isinstance(section, dict) else "<unset>"
    except Exception:  # noqa: BLE001 -- the warning must not raise
        pass
    logger.warning(
        "work ledger: could not read %s from config (%s); configured value %r, "
        "using the default depth cap %d",
        WORK_MAX_DEPTH_SETTING,
        reason,
        configured,
        MAX_DEPTH,
    )


def max_depth() -> int:
    """The conducting depth cap in force: the ``work_ledger.max_depth`` config value, else 2.

    The config loader holds the value to ``[WORK_MAX_DEPTH_MIN, WORK_MAX_DEPTH_MAX]``: a
    value outside that range, or not an integer, is refused there with a warning and the
    default applies, never a clamped value. With nothing set the answer is
    :data:`MAX_DEPTH`, so nothing changes for an install that sets nothing.
    Read per call, not at import: the config loader caches by file fingerprint, so a
    changed setting is seen without a restart.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().work_ledger.max_depth
    except Exception as exc:  # noqa: BLE001 -- a cap is not worth failing a ledger call over
        _warn_depth_fallback(exc)
        return MAX_DEPTH


def depth_cap_hint() -> str:
    """The sentence a depth refusal ends with: which setting changes the cap."""
    return (
        f"the cap is {max_depth()} levels; raise it with {WORK_MAX_DEPTH_SETTING} "
        f"(allowed {WORK_MAX_DEPTH_MIN}-{WORK_MAX_DEPTH_MAX})"
    )


def may_conduct(depth: int) -> bool:
    """Whether a session at *depth* may hold a ledger and dispatch from it.

    The one reading of the cap (:func:`max_depth`). :func:`child_depth`,
    :func:`ensure_conductor` and :func:`_create_item` all decide through this predicate
    so that the level a dispatch admits, the level a ledger may open at and the level
    that may create an item are the same level by construction.
    """
    return depth < max_depth()


def child_depth(parent_depth: int) -> int:
    """The depth a conductor dispatched BY one at *parent_depth* would have.

    Refuses rather than clamping: a clamped depth would let a third conducting
    level exist while reporting as the second, which is the failure the cap exists
    to stop. The refusal lands HERE, at the child's first ledger call and before
    any ledger exists for it: the child is told it may only work, and its parent
    never watches a child that opened a ledger and then could create nothing in
    it.
    """
    depth = _require_count(parent_depth, "depth")
    child = depth + 1
    if not may_conduct(child):
        raise WorkLedgerError(
            f"depth {depth} is the last conducting level ({depth_cap_hint()}); "
            "this session may not dispatch a conductor",
            code=CODE_DEPTH_EXCEEDED,
            field="depth",
        )
    return child


# Derived flags. Pure functions.


def is_orphaned(item: WorkItem, *, conductor_slot_exists: bool) -> bool:
    """Whether nothing is left to read *item*'s reports.

    Derived, never stored: nothing is running at session-close time to stamp a flag,
    a missed stamp would stay wrong forever, and a derived flag self-heals when the
    session is reopened. The worker keeps writing — its binding is still valid — and
    the writes simply accumulate unread.
    """
    return not conductor_slot_exists and not item.is_terminal


def is_stale(
    item: WorkItem,
    *,
    worker_running: bool,
    worker_closed: bool = False,
    worker_looping: bool = False,
    worker_loop_held: bool = False,
    now: datetime | None = None,
    window_secs: float = DEFAULT_STALE_WINDOW_SECS,
) -> bool:
    """Whether *item* has gone quiet in a way that is worth waking someone for.

    A CONJUNCTION: quiet plus not running. A worker in a thirty-minute build is running,
    so it is never flagged. The window covers the gap between binding and the first
    report, and catches a session that ended without reporting.

    ``worker_looping`` says the worker's session holds an ACTIVE loop (a
    ``monitor_start`` prompt loop or a structured monitor) that will wake it, so it is
    alive between cycles and not flagged. A stopped or expired loop wakes nobody and the
    caller passes ``False``; a closed session outranks a loop record. ``False`` means
    "unknown".

    ``worker_closed`` skips ONLY THE WINDOW, and only for an item that has REPORTED AT
    LEAST ONCE. "Not running" is also true of a worker thinking between turns, so time
    tells the two apart; a session that spoke and then ended has no turn coming. With no
    report the window stays: a caller answers "closed" by failing to find a session, and
    before a first report that absence may just mean "not registered yet". Every other
    clause still holds. ``False`` means "unknown".

    The flag follows who owns the next move. A terminal item is never stale. A last
    report of ``done``, ``blocked`` or ``question`` already woke the conductor and hands
    the move to it or a person, so silence is the expected wait. Exceptions that hand
    the move back to the worker: a ``decide`` at or after a ``blocked`` / ``question``
    report (the window then runs from the decision), and a ``done`` item ruled
    ``verdict: fail`` and left OPEN (a retry). A ``blocked`` / ``question`` item whose
    worker CLOSED after reporting is flagged at once: nobody is left to act on the answer.
    :data:`STALE_ELIGIBLE_STATUSES` names the statuses that count, and so does no report.

    An item with no report is measured from ``created_at``, and one whose ``progress``
    report named a ``wake_at`` from that time (:func:`quiet_since`), so a declared wait
    is not flagged before it ends. ``worker_loop_held`` says the worker's session holds
    an active loop that cannot fire (held on an approval, or past its bound): the wait
    that report declared leans on a loop that wakes nobody, so ``wake_at`` is ignored
    and the normal window runs. ``False`` means "unknown". An unparseable timestamp
    reads as stale, or the item could never be flagged.
    """
    if item.is_terminal or worker_running:
        return False
    if worker_closed and item.last_report_at and item.status in WAITING_STATUSES:
        # Waiting on the conductor, but the worker the answer goes back to is gone, so
        # no answer can be acted on. Flagged with or without a ``decide`` yet.
        return True
    if not _worker_owns_next_move(item):
        return False
    if worker_looping and not worker_closed:
        return False
    if worker_closed and item.last_report_at:
        # An OPEN item whose next move is the worker's, and its worker is gone: the
        # window cannot change the answer. ``last_report_at`` is required because
        # "closed" means "session not found", which before a first report may be a
        # session never registered where the caller looks (the bind gap, a slot table
        # still rehydrating). A report proves the worker existed; without one the
        # window decides.
        return True
    reference = quiet_since(item, wake=not worker_loop_held)
    if not reference:
        return True
    # Through ``parse_stamp``, whose UTC fallback covers a stamp at either end of
    # the calendar: a hand-edited ``created_at`` of year 1 is a sentinel, not a
    # moment, and the flag derived from it must not be what fails the read.
    stamped = parse_stamp(reference)
    if stamped is None:
        return True
    moment = now or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment - stamped > timedelta(seconds=max(window_secs, 0.0))


def quiet_since(item: WorkItem, *, wake: bool = True) -> str:
    """The stamp :func:`is_stale` measures *item*'s quiet window from, ``""`` for none.

    The conductor's ``decide`` when it answered a ``blocked`` / ``question`` report,
    else the last report, else the item's creation. A ``wake_at`` later than that
    stamp wins: the worker said it is idle until then, so its silence starts there.
    *wake* False skips ``wake_at`` (the worker's loop is held; see :func:`is_stale`).
    """
    handed_back = _decided_after_report(item) if item.status in WAITING_STATUSES else None
    since = handed_back or item.last_report_at or item.created_at or ""
    if wake and item.wake_at:
        due = parse_stamp(item.wake_at)
        start = parse_stamp(since) if since else None
        if due is not None and start is not None and due > start:
            return item.wake_at
    return since


def _worker_owns_next_move(item: WorkItem) -> bool:
    """Whether *item*'s last report leaves the next move with the WORKER.

    The half of :func:`is_stale` that keeps the flag pointed at a worker rather than at
    whoever is reading. No report yet counts (the worker owes the first one), and so
    does a ``done`` whose verdict came back ``fail`` on a still-open item — the
    conductor handed that work back -- and so does a ``blocked`` / ``question`` the
    conductor has answered with ``decide``.
    """
    if item.status is None or item.status in STALE_ELIGIBLE_STATUSES:
        return True
    if item.status in WAITING_STATUSES:
        return _decided_after_report(item) is not None
    return item.status == "done" and item.verdict == "fail"


def _decided_after_report(item: WorkItem) -> str | None:
    """*item*'s ``decided_at`` when it is at or after its last report, else ``None``.

    "At or after" because both stamps are seconds-precision and a ``decide`` answers the
    report it follows, so a decision in the report's own second is the answer to it.
    An unreadable stamp reads as no hand-back, which leaves a waiting item unflagged
    as it is without a decision.
    """
    if not item.decided_at:
        return None
    decided = parse_stamp(item.decided_at)
    if decided is None:
        return None
    if not item.last_report_at:
        return item.decided_at
    reported = parse_stamp(item.last_report_at)
    if reported is None:
        return None
    try:
        return item.decided_at if decided >= reported else None
    except TypeError:
        return None


def is_pending_overdue(item: WorkItem, *, now: datetime | None = None) -> bool:
    """Whether *item*'s acceptance has read ``pending`` for longer than the budget.

    ``accept_eval.py`` keeps no state between cycles, so a check that never reports (no
    free runner, a deleted required check, a fork run nobody approves) answers
    ``pending`` on every cycle, and polling it never ends. ``pending_since`` is when the
    current run of ``pending`` verdicts began; past :data:`PENDING_BUDGET_SECS` the
    conductor re-arms the item once, then closes it. Derived at read time, like
    :func:`is_stale`.

    Never for a terminal item, nor for a ``human_approval`` bar: that one is ``pending``
    until a person answers, and the conductor settles it by asking, not by polling. No
    ``pending_since`` (no run yet, or a record from before the field) is not overdue. An
    unparseable stamp is, because the run keeps it and the alternative is an item that
    can never be flagged.
    """
    if item.is_terminal or item.verdict != "pending" or not item.pending_since:
        return False
    if item.acceptance.get("kind") == "human_approval":
        return False
    started = parse_stamp(item.pending_since)
    if started is None:
        return True
    moment = now or datetime.now().astimezone()
    return moment - started > timedelta(seconds=PENDING_BUDGET_SECS)


def is_acceptance_concrete(acceptance: Any) -> bool:
    """Whether *acceptance* names a bar ``accept_eval.py`` can actually evaluate.

    Derived at read time like :func:`is_stale`: the answer changes when the conductor
    promotes the bar, so a stamped flag would go stale.

    A conductor may write ``"TBD"`` until a worker reports the real value, and
    ``accept_eval.py`` answers such a bar with ``error``, which reads like a real failure.
    So a non-concrete bar is left OUT of :func:`accept_batch` until an ``accept`` fills
    it in, and this predicate makes the omission visible.

    Judged FIELD BY FIELD over :data:`ACCEPTANCE_READ_FIELDS`, never a whole-object scan:
    an acceptance may carry metadata the evaluator ignores (a note, a `cmd` argv that
    mentions TBD), and walking caller-supplied nesting on the read path could fail the
    whole slot's read on a deep record.

    Non-concrete means: an empty acceptance; a ``kind`` outside :data:`ACCEPTANCE_KINDS`;
    a placeholder STRING (blank, or ``"TBD"`` in any case) in a read field; or a read
    field mistyped: a ``pr_checks`` / ``pr_merged`` ``pr`` that is not a positive
    integer, a ``pr_merged`` ``repo`` or ``edition_check`` ``check`` / ``ref`` that does
    not match the evaluator's pattern, a ``file`` ``path`` that is not a string, a
    ``file`` ``exists`` that is not a bool. An explicit ``null`` in an OPTIONAL read
    field is absence, so concrete; in a required one the type rule refuses it.

    The type rules mirror ``accept_eval.py``'s error-only guards across a process
    boundary; a test pins them against the script. A ``cmd`` bar stays in the batch,
    because ``refused`` is a verdict the conductor must receive.
    """
    if not isinstance(acceptance, dict) or not acceptance:
        return False
    kind = acceptance.get("kind")
    if not isinstance(kind, str) or kind not in ACCEPTANCE_KINDS:
        return False
    for name in ACCEPTANCE_READ_FIELDS[kind]:
        # An ABSENT read field is not a placeholder: ``repo`` and ``exists`` are both
        # optional in the evaluator, and the type rules below are what catch a missing
        # field that is actually required.
        if name in acceptance and _is_placeholder(acceptance[name]):
            return False
    if kind == "pr_checks":
        pr = acceptance.get("pr")
        # ``bool`` is an ``int`` subclass, and ``pr: true`` is not a pull request.
        if isinstance(pr, bool) or not isinstance(pr, int) or pr < MIN_PR:
            return False
    if kind == "pr_merged":
        pr = acceptance.get("pr")
        if isinstance(pr, bool) or not isinstance(pr, int) or pr < MIN_PR:
            return False
        # Required here, unlike ``pr_checks``, and held to the evaluator's pattern.
        repo = acceptance.get("repo")
        if not isinstance(repo, str) or not ACCEPTANCE_GH_REPO.fullmatch(repo):
            return False
    if kind == "edition_check":
        check = acceptance.get("check")
        ref = acceptance.get("ref")
        if not isinstance(check, str) or not ACCEPTANCE_CHECK_NAME.fullmatch(check):
            return False
        if not isinstance(ref, str) or not ACCEPTANCE_CHECK_REF.fullmatch(ref):
            return False
    if kind == "file":
        if not isinstance(acceptance.get("path"), str):
            return False
        # ``exists`` defaults to True in the evaluator, so its ABSENCE is fine; only a
        # present non-bool is not. ``1``/``0`` are rejected there too, and coercing
        # here would let a truthy ``"false"`` invert an absence check into a presence
        # one — the reason that guard is a type check rather than ``bool()``.
        if not isinstance(acceptance.get("exists", True), bool):
            return False
    return True


def _is_placeholder(value: Any) -> bool:
    """Whether ONE field value still says "not filled in yet".

    Only a STRING can say it. ``None`` is absence to the evaluator (``repo: null`` omits
    ``--repo``); where a field is required, the per-kind type rules refuse ``None``.
    Flat on purpose, never walked over a caller-supplied object: a container in a read
    field is a wrong type, which the type rules refuse.
    """
    return isinstance(value, str) and value.strip().lower() in ACCEPTANCE_PLACEHOLDERS


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_stamp(value: str) -> datetime | None:
    """An ISO-8601 stamp as an AWARE datetime, or ``None`` when it is not one.

    :func:`_now_iso` writes local time with an offset; a stamp without one (a caller's
    ``since``, an older record) is taken as local time, because comparing naive with
    aware raises and a read filter must never raise.

    A naive stamp at either end of the calendar (``0001-01-01T00:00:00``,
    ``9999-12-31T23:59:59``) may not shift into the local zone, and the platform raises
    ``OverflowError`` or ``ValueError`` depending on the host. Such a sentinel is read as
    UTC, so ``since=0001-01-01T00:00:00`` means "everything", never a failed read.
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed
    try:
        return parsed.astimezone()
    except (OverflowError, ValueError, OSError):
        return parsed.replace(tzinfo=timezone.utc)

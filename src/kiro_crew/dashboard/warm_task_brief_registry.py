"""The warm brief's fact registry: one checker per fact kind, run once before
any text is written (RFC section 3.10).

Each checker reads one source and returns facts, each
``{kind, subject, label, values, source, read_at}``. The gateway numbers them
``f1``, ``f2``… in kind order. A check that could not run is a fact too when
its kind can say so (a PR read "not checked since HH:MM"); a checker that
raises yields nothing for its kind.

Worker reports are hints, not facts: a report older than a fresh read of the
same pull request is superseded by it (:func:`superseded`). The finished
table is kept per thread in memory (:func:`remember`), so the dashboard stamps
the same facts on its work items without a network read (:func:`item_facts`).

A wake turn that proves file, deploy or ticket checks with its own tool calls
is one more source: its facts join the table after this one, and a stat-only
file fact no line cites gives way to them (:func:`prefer_wake_files`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs

logger = logging.getLogger(__name__)

# The most artifact paths one brief stats.
MAX_FILE_STATS = 10
# Threads whose last fact table the dashboard can read back.
_MAX_CACHE = 256
# The order kinds are numbered in, so ids read in a stable order.
KINDS = ("pr", "task", "waits_on_you", "chat", "approval", "ask", "file", "time")
_WAITING_STATUSES = ("question", "blocked")


@dataclass(frozen=True)
class Inputs:
    """What every checker reads. *items* are the work-item payloads the brief
    shows; *state* is the dashboard state, read only on the loop."""

    slot_key: str
    items: tuple[Mapping[str, Any], ...] = ()
    live: tuple[Mapping[str, Any], ...] = ()
    waiting: tuple[Mapping[str, Any], ...] = ()
    now: float = 0.0
    state: Any = None
    # The item ids whose worker turn runs, read on the loop by :func:`gather`.
    running: frozenset[str] = frozenset()


Source = Callable[[Inputs], list[dict[str, Any]]]


def _actionable(item: Mapping[str, Any]) -> bool:
    """An item something can still be asked of: open on this ledger. A closed
    one keeps its task fact for the catch-up and nothing that asks."""
    from kiro_crew.dashboard.handlers.work_ledger_board import work_item_is_terminal

    return not work_item_is_terminal(item.get("state"))


def _local_worker(item: Mapping[str, Any]) -> str:
    """The item's worker session key when its worker runs on this gateway; a
    remote worker has no slot here, so its chat and approvals are not read."""
    key = item.get("worker_session_key")
    if item.get("worker_instance_id") or not (isinstance(key, str) and key):
        return ""
    return key


def pr_subject(ref: inputs.PrRef | None) -> str:
    return f"{ref.repo}#{ref.number}" if ref is not None else ""


def _pr_facts(inp: Inputs) -> list[dict[str, Any]]:
    out = []
    for entry in inp.live:
        if entry.get("checked") is not True:
            values: dict[str, Any] = {
                "not_checked_since": entry.get("not_checked_since") or "never"
            }
        else:
            values = {
                k: entry[k] for k in ("state", "review", "mergeable", "checks") if entry.get(k)
            }
        number = str(entry.get("pr", "")).lstrip("#")
        out.append(
            {
                "kind": "pr",
                "subject": f"{entry.get('repo')}#{number}",
                "pr": entry.get("pr"),
                "repo": entry.get("repo"),
                "label": f"{entry.get('repo')}{entry.get('pr')}",
                "values": values,
                "source": "github",
                "read_at": entry.get("read_at", ""),
                "_read_epoch": entry.get("_read_epoch"),
            }
        )
    return out


def _task_facts(inp: Inputs) -> list[dict[str, Any]]:
    out = []
    for item in inp.items:
        state = item.get("state")
        values = {
            "state": state if state != "open" else "",
            "status": item.get("status"),
            "reason": item.get("reason"),
            "reported": inputs.clock(inputs.parse_epoch(item.get("last_report_at"))),
            "decided": inputs.clock(inputs.parse_epoch(item.get("decided_at"))),
            "pr": pr_subject(inputs.ledger_ref(item)),
        }
        out.append(
            {
                "kind": "task",
                "subject": item.get("item_id"),
                "item_id": item.get("item_id"),
                "label": str(item.get("title") or item.get("item_id") or ""),
                "values": {k: v for k, v in values.items() if v},
                "source": "tasks",
                "_reported": inputs.parse_epoch(item.get("last_report_at")),
            }
        )
    return out


def _item_fact(item: Mapping[str, Any], kind: str, values: dict[str, Any], source: str) -> dict:
    return {
        "kind": kind,
        "subject": item.get("item_id"),
        "item_id": item.get("item_id"),
        "label": str(item.get("title") or item.get("item_id") or ""),
        "values": values,
        "source": source,
    }


def _waits_facts(inp: Inputs) -> list[dict[str, Any]]:
    """Whether each asking item waits on the PERSON: the work-ledger board's
    own ``awaits_person``, over the item's event tail, so the welcome and the
    board agree. FILE I/O: off the loop."""
    from kiro_crew.dashboard.handlers.work_ledger_board import item_awaits_person

    return [
        _item_fact(item, "waits_on_you", {"waits": _awaits(inp, item, item_awaits_person)}, "tasks")
        for item in inp.items
        if item.get("status") in _WAITING_STATUSES and _actionable(item)
    ]


def _awaits(inp: Inputs, item: Mapping[str, Any], test: Callable[..., bool]) -> bool:
    """The board's wait test with the board's own running guard."""
    return test(inp.slot_key, item, running=str(item.get("item_id")) in inp.running)


def _worker_slot(inp: Inputs, item: Mapping[str, Any]) -> Any:
    from kiro_crew.dashboard.handlers.work_ledger_board import worker_slot

    key = _local_worker(item)
    return worker_slot(inp.state, key) if key and _actionable(item) else None


def chat_state(state: Any, worker_key: str) -> str:
    """``running`` / ``interrupted`` / ``idle`` / ``closed`` for a worker chat:
    the board's own word, with ``interrupted`` when the slot's last turn ended
    without a reply (the sidebar's own test). Memory reads, on the loop."""
    from kiro_crew.dashboard.handlers.work_ledger_board import _alive_of, worker_slot
    from kiro_crew.dashboard.slot_projection import summary_rows
    from kiro_crew.dashboard.state import is_turn_interrupted

    word = _alive_of(state, worker_key)
    slot = worker_slot(state, worker_key) if word == "idle" else None
    if slot is not None and is_turn_interrupted(list(summary_rows(slot))):
        return "interrupted"
    return word


def _chat_facts(inp: Inputs) -> list[dict[str, Any]]:
    out = []
    for item in inp.items:
        key = _local_worker(item)
        if inp.state is None or not key or not _actionable(item):
            continue
        fact = _item_fact(item, "chat", {"state": chat_state(inp.state, key)}, "chat")
        # A worker that reported done may close its chat: that is a finish.
        out.append({**fact, "_item_status": item.get("status")})
    return out


def _approval_facts(inp: Inputs) -> list[dict[str, Any]]:
    """Tool approvals waiting in each worker chat: the slot's own futures and
    the coordinator's records for it, the two the sidebar counts."""
    out = []
    for item in inp.items:
        slot = _worker_slot(inp, item) if inp.state is not None else None
        if slot is None:
            continue
        futures = getattr(slot, "_approval_futures", None) or {}
        pending = sum(1 for future in futures.values() if not future.done())
        # The state-level registry the sidebar counts (session_status.py).
        pending_for = getattr(inp.state, "pending_coordinator_approvals", None)
        if callable(pending_for):
            pending += len(pending_for(slot.key) or [])
        out.append(_item_fact(item, "approval", {"pending": pending}, "chat"))
    return out


def _ask_facts(inp: Inputs) -> list[dict[str, Any]]:
    """The offered answers per subject: the conductor's goal-board ``asks``
    entry per item, then each question or change card waiting (``wN``, its
    index in the waiting list). A reply's free ``[OPTIONS:]`` line is not a
    source: it is not keyed by item. FILE I/O: off the loop."""
    from kiro_crew.dashboard.handlers.work_ledger_board import (
        _conductor_asks,
        item_awaits_person,
    )

    # A stored entry outlives its question: it counts only while the item
    # still waits on the person (the board's own test).
    waiting = [i for i in inp.items if _actionable(i) and _awaits(inp, i, item_awaits_person)]
    ids = {str(item.get("item_id")) for item in waiting}
    asks = _conductor_asks(inp.slot_key, ids)
    out = [
        _item_fact(item, "ask", {"options": asks[str(item.get("item_id"))]}, "goal_board")
        for item in waiting
        if str(item.get("item_id")) in asks
    ]
    return out + _waiting_asks(inp)


def _waiting_asks(inp: Inputs) -> list[dict[str, Any]]:
    """Each question or change card waiting, numbered as the prompt's
    "waiting_on_you" lists them (options lines left out). Memory reads."""
    cards = [entry for entry in inp.waiting if entry.get("kind") != "options"]
    return [
        {
            "kind": "ask",
            "subject": f"w{index}",
            "label": str(entry.get("text") or ""),
            "values": {"options": [str(o) for o in entry.get("options") or []]},
            "source": "waiting",
        }
        for index, entry in enumerate(cards)
    ]


def _stat_path(raw: str, root: str) -> str | None:
    """*raw* resolved, when it is an absolute path inside *root* that the
    sensitive-path resolver allows; else ``None``."""
    from kiro_crew.security import _is_unc_path, is_sensitive_path

    expanded = os.path.expanduser(raw)
    # A UNC path is refused by its spelling: resolving one opens a network
    # connection to the host it names. The platform's own absolute-path test
    # keeps a drive-qualified Windows path.
    if _is_unc_path(expanded) or not os.path.isabs(expanded):
        return None

    def inside(path: str) -> bool:
        try:
            return os.path.commonpath([root, path]) == root
        except ValueError:  # another drive
            return False

    # Contained by spelling first, so nothing outside *root* is ever resolved;
    # then again after resolving, so a link cannot lead out.
    if not inside(os.path.normpath(expanded)):
        return None
    path = os.path.realpath(expanded)
    if not inside(path):
        return None
    return None if is_sensitive_path(path) else path


def _file_facts(inp: Inputs) -> list[dict[str, Any]]:
    """``os.stat`` on the paths items name in ``artifacts``: at most
    ``MAX_FILE_STATS``, inside the workspace root, never a sensitive path,
    never the contents. FILE I/O: off the loop."""
    from kiro_crew.config.loader import workspace_root

    root = os.path.realpath(str(workspace_root(create=False)))
    seen: set[str] = set()
    out = []
    for item in inp.items:
        artifacts = item.get("artifacts")
        for raw in artifacts.values() if isinstance(artifacts, Mapping) else ():
            path = _stat_path(raw.strip(), root) if isinstance(raw, str) else None
            if path is None or path in seen or len(seen) >= MAX_FILE_STATS:
                continue
            seen.add(path)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                values: dict[str, Any] = {"exists": False}
            except OSError:
                continue
            else:
                values = {"exists": True, "size": st.st_size, "modified": inputs.clock(st.st_mtime)}
            fact = _item_fact(item, "file", values, "stat")
            out.append(
                {**fact, "subject": path, "label": raw.strip(), "read_at": inputs.clock(inp.now)}
            )
    return out


def _time_facts(inp: Inputs) -> list[dict[str, Any]]:
    now = inputs.clock(inp.now)
    return [{"kind": "time", "subject": "now", "label": "now", "values": {"now": now},
             "source": "gateway", "read_at": now}]  # fmt: skip


# Checkers by where they run: ledger and file reads off the loop, slot reads on it.
OFF_LOOP: tuple[Source, ...] = (_task_facts, _waits_facts, _ask_facts, _file_facts)
ON_LOOP: tuple[Source, ...] = (_chat_facts, _approval_facts)


def _run(source: Source, inp: Inputs) -> list[dict[str, Any]]:
    try:
        return source(inp)
    except Exception:
        logger.debug("warm brief checker %s failed", source.__name__, exc_info=True)
        return []


def _running_ids(inp: Inputs) -> frozenset[str]:
    """The items whose worker turn runs, by the board's reader. On the loop."""
    if inp.state is None:
        return frozenset()
    from kiro_crew.dashboard.handlers.work_ledger_board import item_running

    out = set()
    for item in inp.items:
        try:
            if item_running(inp.state, item):
                out.add(str(item.get("item_id")))
        except Exception:
            logger.debug("no worker liveness for a warm brief item", exc_info=True)
    return frozenset(out)


def is_bad(fact: Mapping[str, Any]) -> bool:
    """A fact the owner must see: a conflict, a failed check, requested
    changes, a closed worker chat on an open item, or an approval waiting."""
    values = fact.get("values") or {}
    kind = fact.get("kind")
    if kind == "pr":
        checks = values.get("checks")
        failed = checks.get("failed") if isinstance(checks, Mapping) else 0
        return (
            values.get("mergeable") == "conflicting"
            or values.get("review") == "changes_requested"
            or (isinstance(failed, int) and failed > 0)
        )
    if kind == "chat":
        return values.get("state") == "closed" and fact.get("_item_status") != "done"
    if kind == "approval":
        return isinstance(values.get("pending"), int) and values["pending"] > 0
    return False


def number(found: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """*found* in kind order, numbered ``f1``…, each marked ``bad``, at most
    *limit*. The clock fact is always kept; past the limit the facts dropped
    are counted on it (``dropped``, by subject), so the check line says so."""
    clock = [f for f in found if f["kind"] == "time"][:1]
    rest = sorted(
        (f for f in found if f["kind"] != "time"),
        key=lambda f: KINDS.index(f["kind"]) if f["kind"] in KINDS else 99,
    )
    kept, cut = rest[: max(0, limit - len(clock))], rest[max(0, limit - len(clock)) :]
    if clock and cut:
        dropped = len({(f["kind"], f.get("subject")) for f in cut})
        clock = [{**clock[0], "values": {**clock[0]["values"], "dropped": dropped}}]
    return [
        {"id": f"f{n}", **fact, "bad": is_bad(fact)} for n, fact in enumerate([*kept, *clock], 1)
    ]


async def gather(inp: Inputs, limit: int, extra: Sequence[Source] = ()) -> list[dict[str, Any]]:
    """Every checker once: the fact table this brief is written from."""
    on_loop = [fact for source in ON_LOOP for fact in _run(source, inp)]
    inp = replace(inp, running=_running_ids(inp))
    off_loop = await asyncio.to_thread(
        lambda: [fact for source in (*OFF_LOOP, *extra) for fact in _run(source, inp)]
    )
    return number([*_pr_facts(inp), *off_loop, *on_loop, *_time_facts(inp)], limit)


def on_loop_again(table: Sequence[Mapping[str, Any]], inp: Inputs) -> list[dict[str, Any]]:
    """*table* with what is read on the loop (worker chats, approvals, the
    waiting cards) read again from *inp* and renumbered. No await: the other
    facts keep the read they had."""
    again = [*(f for s in ON_LOOP for f in _run(s, inp)), *_waiting_asks(inp)]
    kept = [
        {k: v for k, v in f.items() if k not in ("id", "bad")}
        for f in table
        if f["kind"] not in ("chat", "approval") and f.get("source") != "waiting"
    ]
    return number([*kept, *again], len(table) + len(again))


def loop_state(inp: Inputs) -> str:
    """One digest of what :func:`gather` reads on the loop: worker chat
    states, pending approvals and which worker turns run. No await, so a
    caller compares it in the same step it posts in."""
    import hashlib
    import json

    rows = [
        [f.get("kind"), f.get("subject"), f.get("values")] for s in ON_LOOP for f in _run(s, inp)
    ]
    blob = json.dumps([rows, sorted(_running_ids(inp))], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint(table: Sequence[Mapping[str, Any]]) -> str:
    """One digest of every fact's kind, subject and values, minus the clock:
    equal fingerprints mean the gateway would draw the same cards."""
    import hashlib
    import json

    rows = sorted(
        json.dumps([f.get("kind"), f.get("subject"), f.get("values")], sort_keys=True, default=str)
        for f in table
        if f.get("kind") != "time"
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def prompt_facts(table: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The table as the prompt shows it: private keys dropped."""
    return [{k: v for k, v in fact.items() if not k.startswith("_")} for fact in table]


def superseded(table: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """item_id -> the PR fact that replaces its worker's last report: a read
    of the item's own pull request taken after that report."""
    prs = {
        f["subject"]: f
        for f in table
        if f.get("kind") == "pr" and isinstance(f.get("_read_epoch"), (int, float))
    }
    out = {}
    for fact in table:
        pr = (
            prs.get((fact.get("values") or {}).get("pr", ""))
            if fact.get("kind") == "task"
            else None
        )
        reported = fact.get("_reported")
        if pr is not None and isinstance(reported, float) and pr["_read_epoch"] > reported:
            out[str(fact.get("item_id"))] = pr
    return out


def time_texts(table: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every time a fact carries: the only HH:MM a brief may write besides
    its "as of" time (RFC section 3.9.6)."""
    return [
        text
        for fact in table
        for v in (fact.get("read_at"), *(fact.get("values") or {}).values())
        for text in (v if isinstance(v, list) else [v])
        if isinstance(text, str) and text
    ]


def prefer_wake_files(
    table: Sequence[Mapping[str, Any]], cited: set[str]
) -> list[Mapping[str, Any]]:
    """*table* with the stat-only file facts no line cites dropped once a wake
    check proved a file fact: the wake's tool calls are the source, the stat
    the fallback. Ids are kept as they are."""
    if not any(f.get("kind") == "file" and f.get("source") == "wake" for f in table):
        return list(table)
    return [f for f in table if not (f.get("source") == "stat" and f.get("id") not in cited)]


# How long the dashboard shows a cached table: the PR read's own reuse window
# times two, so a stale chip never outlives a second welcome's reread.
_CACHE_MAX_AGE_SECS = 2 * inputs.LIVE_PR_TTL_SECS
# slot key -> (when the table was gathered, the table). Memory only.
# shortcut: a restart empties it until the next welcome; persist it in the
# crew-log projection when the board must show tags right after a restart.
_cache: dict[str, tuple[float, tuple[Mapping[str, Any], ...]]] = {}


def remember(slot_key: str, table: Sequence[Mapping[str, Any]], at: float | None = None) -> None:
    _cache.pop(slot_key, None)
    _cache[slot_key] = (time.time() if at is None else at, tuple(table))
    while len(_cache) > _MAX_CACHE:
        _cache.pop(next(iter(_cache)))


def item_facts(slot_key: str, item_id: str, current_pr: str) -> dict[str, Any] | None:
    """The cached facts on one work item, for the dashboard's item rows: its
    PR's values, whether they are bad, and that read's own time. ``None``
    when nothing is cached, the item's PR was not read, or the item now holds
    another PR than *current_pr* (``owner/repo#N``, its current one)."""
    cached = _cache.get(slot_key)
    if cached is None or time.time() - cached[0] > _CACHE_MAX_AGE_SECS:
        return None
    table = cached[1]
    mine = {f["kind"]: f for f in table if f.get("item_id") == item_id}
    task = mine.get("task")
    if task is None:
        return None
    subject = (task.get("values") or {}).get("pr")
    if not subject or subject != current_pr:
        return None
    pr = next((f for f in table if f.get("kind") == "pr" and f.get("subject") == subject), None)
    values = (pr or {}).get("values") or {}
    # Only a PR this welcome really read, stamped with that read's own time:
    # an unread one must not look like a fresh all-clear. Chat state and
    # approvals change within seconds, so the cache never stamps them.
    if pr is None or "not_checked_since" in values or not pr.get("read_at"):
        return None
    return {"pr": {**values, "bad": bool(pr.get("bad"))}, "checked_at": pr["read_at"]}

"""No async test touches a crew log from the event-loop thread.

A crew log is guarded by a per-unit file lock, and two background threads take it on
their own schedule: the writer that lands every append, and the eager folder that
reads the unit right after each entry. ``file_lock`` takes one attempt and never waits
on the event-loop thread, so a crew-log call made straight from an ``async def`` is
refused whenever one of those threads holds the lock -- at random, on whichever shard
is slow that day. A refused write raises ``CrewLedgerNotRecorded``; a refused read
raises ``OSError`` or, where the reader is best-effort, quietly answers an empty record.
Product callers make these calls through ``asyncio.to_thread``.

This pin reads every test module and fails on an ``async def`` that makes one of the
calls in ``_CREW_LOG_CALLS`` directly, or calls a sync helper in the same module that
does. A helper that hands the call to ``off_loop`` (``test/off_loop_helpers.py``) or
``asyncio.to_thread`` only REFERENCES it, so it does not count. ``_ON_LOOP_ON_PURPOSE``
lists the tests whose subject is the on-loop refusal itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Calls that take a crew log's unit lock. A bare name is unique to the crew log; a
#: dotted name is matched only through that spelling, since ``read_state`` alone is
#: also the name of unrelated readers.
_CREW_LOG_CALLS = frozenset(
    {
        "commit_work_progress",
        "record_crew_checkpoint",
        "open_session_log",
        "CrewLog.open",
        "iter_from",
        "session_ledger.read_state",
        "sl.read_state",
        # Removal waits for the eager folder to finish its batch before unlinking, and
        # on the event-loop thread that wait is skipped: Windows then refuses to
        # unlink the segment the fold has open, and the unit is reported not removed.
        "store.sweep_expired",
        "store.remove_unit",
    }
)

#: Substrings that make a module worth parsing.
_SCAN_IF = (
    "commit_work_progress",
    "record_crew_checkpoint",
    "open_session_log",
    "CrewLog.open",
    "iter_from",
    "read_state",
    "sweep_expired",
    "remove_unit",
)

#: ``(file name, async function)`` pairs that make the on-loop call on purpose.
_ON_LOOP_ON_PURPOSE = frozenset(
    {
        # The negative control proving the held-lock probe is real contention.
        ("test_issue_radar_crew_runtime.py", "on_the_loop"),
    }
)

_TEST_DIR = Path(__file__).resolve().parent


def _called_names(call: ast.Call) -> set[str]:
    fn = call.func
    if isinstance(fn, ast.Name):
        return {fn.id}
    if isinstance(fn, ast.Attribute):
        names = {fn.attr}
        if isinstance(fn.value, ast.Name):
            names.add(f"{fn.value.id}.{fn.attr}")
        return names
    return set()


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_calls(fn: ast.AST) -> list[ast.Call]:
    """The calls ``fn``'s own body makes, not those of a function or lambda it defines.

    A nested ``def`` or a lambda is a fake handed to something else (``monkeypatch``
    installing a replacement emitter); it runs where its caller runs, not here.
    """
    found: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED):
            continue
        if isinstance(node, ast.Call):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _calls(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for call in _own_calls(fn):
        out |= _called_names(call)
    return out


def on_loop_crew_log_calls(source: str) -> list[tuple[str, int]]:
    """``(async function, line)`` for each on-loop crew-log call in ``source``."""
    tree = ast.parse(source)
    sync_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    touching = set(_CREW_LOG_CALLS)
    grew = True
    while grew:
        grew = False
        for fn in sync_defs:
            if fn.name not in touching and _calls(fn) & touching:
                touching.add(fn.name)
                grew = True
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for call in _own_calls(fn):
            if _called_names(call) & touching:
                found.append((fn.name, call.lineno))
    return found


def test_no_async_test_touches_a_crew_log_on_the_loop() -> None:
    offenders = []
    scanned = 0
    for path in sorted(_TEST_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in _SCAN_IF):
            continue
        scanned += 1
        for name, line in on_loop_crew_log_calls(source):
            if (path.name, name) not in _ON_LOOP_ON_PURPOSE:
                offenders.append(f"{path.relative_to(_TEST_DIR)}:{line} in {name}")
    assert scanned >= 5, "the scan found none of the crew-log tests; the pin is vacuous"
    assert offenders == [], (
        "these async tests touch a crew log on the event-loop thread, where its lock "
        "is never waited for; route the call through off_loop(...) or "
        f"asyncio.to_thread: {offenders}"
    )


def test_the_scan_sees_direct_and_helper_calls_and_spares_a_hop() -> None:
    source = """
def helper():
    cs.commit_work_progress(1)

def hopped():
    off_loop(cs.commit_work_progress, 1)

def reader():
    return projection.open_session_log("u")

async def direct():
    cs.record_crew_checkpoint(1)

async def via_helper():
    helper()

async def reads():
    sl.read_state("k")

async def via_reader():
    reader()

def folded():
    handle = CrewLog.open("session", "s")
    return tuple(handle.iter_from(1))

async def via_folded():
    folded()

async def unrelated_read_state():
    redaction_switch.read_state()

async def sweeps():
    store.sweep_expired(30)

async def unrelated_sweep():
    decisions_log.sweep_expired()

async def via_hop():
    hopped()
    off_loop(reader)
    off_loop(store.sweep_expired, 30)
    await asyncio.to_thread(cs.commit_work_progress, 1)

def installs_a_fake(monkeypatch):
    def fake(unit):
        return projection.open_session_log(unit)
    monkeypatch.setattr(emit, "on", fake)

async def installs_fakes(monkeypatch):
    installs_a_fake(monkeypatch)
    monkeypatch.setattr(emit, "on", lambda unit: projection.open_session_log(unit))
"""
    assert [name for name, _ in on_loop_crew_log_calls(source)] == [
        "direct",
        "via_helper",
        "reads",
        "via_reader",
        "via_folded",
        "sweeps",
    ]

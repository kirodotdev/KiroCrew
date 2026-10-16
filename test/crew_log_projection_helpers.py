"""Logs, entries and fixture writers shared by the crew-log projection tests.

``test_crew_log_projection.py`` and its ``_session_reads``, ``_bounds`` and ``_class``
siblings import these by name. ``_isolated_home`` is autouse, so every one of them writes
into its own data home. The rest opens a session log, appends turn, tool and opener entries,
writes a long fixture as grouped appends, builds the busy log the incremental-fold tests
split, and reads a log's entries back.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog

SESSION = "s-fold"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_crew_log_projection*.py modules calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _log(unit_id: str = SESSION, **fields) -> CrewLog:
    fields.setdefault("owner", "raymond")
    fields.setdefault("agent", "kirocrew")
    return CrewLog.create(lg.KIND_SESSION, unit_id, **fields)


def _opened(handle: CrewLog, *, resumed: bool = False, model: str = "opus") -> None:
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "dashboard:1",
            "model": model,
            "cwd": "/w",
            "owner": "raymond",
            "resumed": resumed,
        },
        src=GATEWAY,
    )


def _turn(
    handle: CrewLog,
    turn: int,
    *,
    credits: float | None = 0.5,
    tokens: dict[str, int] | None = None,
    stop_reason: str = "end_turn",
    attempt: int | None = None,
    model: str = "opus",
) -> None:
    """A whole turn: started, one step, completed."""
    for item in _turn_items(
        turn,
        credits=credits,
        tokens=tokens,
        stop_reason=stop_reason,
        attempt=attempt,
        model=model,
    ):
        handle.append(item["type"], item["data"], src=GATEWAY)


def _tool(
    handle: CrewLog, turn: int, call_id: str, name: str, *, status: str = "completed"
) -> None:
    for item in _tool_items(turn, call_id, name, status=status):
        handle.append(item["type"], item["data"], src=GATEWAY)


# --- writing a fixture that has to be LONG --------------------------------- #
#
# A bound in the projection tests is a bound on retained state, so reaching one costs as
# many entries as the bound itself, and the largest fixture they build runs past 2048
# entries. One ``append`` per entry is one cross-process lock,
# one tail scan and one ``os.fsync`` EACH, which measures 76-90 ms per entry on
# the Windows CI runner: that puts a 2049-entry fixture at 156-185 s against a
# 180 s per-test cap. A breach there does not fail one test. Windows has no
# SIGALRM, so pytest-timeout falls back to the thread method and terminates the
# whole xdist worker, and the shard runs ``--max-worker-restart=0`` deliberately,
# so one fixture can fail a shard of 1600 tests in a file its author never opened.
#
# So a fixture past a few hundred entries is written with ``append_many``, the
# production group write: the same validation, the same consecutive seqs, the same
# file, one fsync per group. A group shares one ``time`` -- which is what a
# production group write does as well -- and no bound they pin is a bound on wall
# clock. ``test_a_grouped_fixture_is_the_log_one_append_at_a_time_writes`` (in
# ``test_crew_log_projection_bounds.py``) pins that equivalence, so this stays a
# change in what the fixture COSTS.

_GROUP_ENTRIES = 512


def _append_grouped(
    handle: CrewLog, items: list[dict[str, Any]], *, group: int = _GROUP_ENTRIES
) -> None:
    """Append *items* with one lock and one fsync per bounded group.

    Bounded rather than one write of everything, so a long fixture still crosses
    the tail scan and the ``needs_newline`` path more than once. *group* is only
    for the test that pins this helper, which must cross a group boundary without
    paying for a long fixture to do it.
    """
    for start in range(0, len(items), group):
        handle.append_many(items[start : start + group], src=GATEWAY)


def _turn_items(
    turn: int,
    *,
    credits: float | None = 0.5,
    tokens: dict[str, int] | None = None,
    stop_reason: str = "end_turn",
    attempt: int | None = None,
    model: str = "opus",
) -> list[dict[str, Any]]:
    """The four entries of one whole turn, unwritten."""
    start: dict[str, Any] = {"turn": turn, "actor": "user", "depth": 0}
    if attempt is not None:
        start["attempt"] = attempt
    done: dict[str, Any] = {
        "turn": turn,
        "stop_reason": stop_reason,
        "depth": 0,
        "duration_ms": 900,
        "model": model,
        "provider": "kiro",
    }
    if credits is not None:
        done["credits"] = credits
        done["tokens"] = tokens or {
            "input": 100,
            "output": 20,
            "cache_read": 5,
            "cache_write": 1,
        }
        # The provider's occupancy reading. Present on the MEASURED closer only, which
        # is what carries it in production -- and load-bearing for
        # ``test_a_fold_never_reaches_into_the_state_it_was_handed``: ``usage`` stamps
        # this reading onto the context rows its ``context/composed`` already
        # appended, and those row dicts are SHARED with an earlier snapshot by
        # ``_usage_copy``. Without a closer that carries occupancy, the copier's miss
        # is invisible -- the fold produces the right value while editing a caller's
        # state, and nothing raises. A composition must precede this entry in the same
        # turn for the stamp to have a row to reach (see ``_busy_log``).
        done["context"] = {"used": 4_200, "window": 200_000}
    return [
        {"type": "turn/started", "data": start},
        {"type": "step/started", "data": {"turn": turn, "step": 1}},
        {"type": "step/completed", "data": {"turn": turn, "step": 1, "ms": 120}},
        {"type": "turn/completed", "data": done},
    ]


def _tool_items(
    turn: int, call_id: str, name: str, *, status: str = "completed"
) -> list[dict[str, Any]]:
    """The call and the completion of one tool frame, unwritten."""
    return [
        {
            "type": "tool/called",
            "data": {
                "turn": turn,
                "call_id": call_id,
                "name": name,
                "server": "core",
                "kind": "mcp",
            },
        },
        {
            "type": "tool/completed",
            "data": {
                "turn": turn,
                "call_id": call_id,
                "name": name,
                "server": "core",
                "status": status,
                "elapsed_ms": 42,
            },
        },
    ]


def _busy_log() -> CrewLog:
    """A session with something for every fold to see."""
    handle = _log()
    _opened(handle)
    handle.append("model/selected", {"model": "opus", "source": "config"}, src=GATEWAY)
    handle.append(
        "request/configured",
        {"turn": 1, "model": "opus", "provider": "kiro", "context_window": 200000},
        src=GATEWAY,
    )
    handle.append(
        "context/composed",
        {
            "turn": 1,
            "step": 1,
            "sources": [
                {"kind": "system", "chars": 400, "tokens": 100},
                {"kind": "memory", "chars": 800, "tokens": 200},
            ],
            "chars": 1200,
            "tokens": 300,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    _turn(handle, 1)
    _tool(handle, 1, "c1", "fs_read")
    _tool(handle, 1, "c2", "fs_write", status="refused")
    handle.append(
        "approval/requested",
        {"turn": 1, "approval_id": "a1", "tool": "shell", "reason": "run tests"},
        src=GATEWAY,
    )
    handle.append(
        "approval/decided",
        {"turn": 1, "approval_id": "a1", "decision": "allow", "by": "raymond"},
        src=GATEWAY,
    )
    handle.append(
        "compaction/applied",
        {"pct_before": 82.0, "pct_after": 41.0, "freed_pct": 41.0},
        src=GATEWAY,
    )
    _turn(handle, 2, credits=None, stop_reason="interrupted")
    handle.append("write/dropped", {"dropped_count": 3, "dropped_bytes": 900}, src=GATEWAY)
    handle.append("session/closed", {"reason": "reset"}, src=GATEWAY)
    return handle


def _entries(handle: CrewLog) -> tuple:
    return tuple(handle.iter_from(1))

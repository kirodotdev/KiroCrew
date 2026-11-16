"""Two dashboard routes read a string bool flag as the bool it names.

``include_history`` on ``POST /api/memory/consolidate`` and ``fresh`` on
``POST /api/taskrunner/{task_id}/execute`` were tested for truth, so the JSON
string ``"false"`` was truthy: consolidation still included history (and ran
the history-only retry-backoff check), and a plan executed with ``fresh=True``.
The shared ``_coerce_bool`` reader maps the usual spellings; a JSON bool, an
absent key and any other value read as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _body,
    _make_request,
    _make_state,
    _reset_setup_status,
)
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _request,
    _runner,
    _state,
)

import kiro_crew.dashboard.handlers.memory as mem_mod
import kiro_crew.dashboard.handlers.taskrunner as tr_mod
from kiro_crew.testing.wait import await_until

_ABSENT = object()

_CASES = [
    ("false", False),
    ("False", False),
    (" off ", False),
    ("0", False),
    ("true", True),
    (False, False),
    (True, True),
]
_IDS = ["false", "False", "off", "zero", "true", "json-false", "json-true"]


def _consolidator() -> Any:
    c = MagicMock()
    c._running = set()
    c._busy = lambda key: key in c._running
    c._tasks = set()
    c._consolidate = AsyncMock(return_value=None)
    c.retry_eligible = MagicMock(return_value=True)
    c._log.consolidation_counts = MagicMock(return_value=(3, 0))
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "expected"), [*_CASES, (_ABSENT, True)], ids=[*_IDS, "absent"])
async def test_include_history_is_read_as_the_bool_it_names(flag: Any, expected: bool) -> None:
    from kiro_crew.history import ConversationLog

    cons = _consolidator()
    state = _make_state(consolidator=cons)
    state.conversation_log = ConversationLog()
    body: dict[str, Any] = {"key": "s1"}
    if flag is not _ABSENT:
        body["include_history"] = flag
    req = _make_request(state, method="POST", json_body=body, session_key="dashboard:ui")
    assert _body(await mem_mod.api_memory_consolidate(req)) == {"ok": True, "key": "s1"}
    for task in list(cons._tasks):
        await await_until(task, what="the spawned consolidation task never finished")
    cons._consolidate.assert_awaited_once_with("s1", expected)
    # The retry-backoff check only applies with history.
    assert cons.retry_eligible.called is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "expected"), [*_CASES, (_ABSENT, False)], ids=[*_IDS, "absent"])
async def test_fresh_is_read_as_the_bool_it_names(
    tmp_path: Path, flag: Any, expected: bool
) -> None:
    runner = _runner(tmp_path)
    body: dict[str, Any] = {"agent": "a"}
    if flag is not _ABSENT:
        body["fresh"] = flag
    admit = tr_mod._admit_existing_run
    with patch.object(tr_mod, "_admit_existing_run", wraps=admit) as spy:
        resp = await tr_mod.api_taskrunner_execute_plan(
            _request(_state(runner), match_info={"task_id": "p1"}, json_body=body)
        )
    assert _body(resp) == {"ok": True, "task_id": "p1"}
    assert runner.execute_plan.await_args.kwargs["fresh"] is expected
    # The admission body carries the same value, so a replay keys on it too.
    assert spy.call_args.kwargs["body"][1] is expected

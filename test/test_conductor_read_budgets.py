"""A conductor's two patrol reads fit a busy gateway: ``session_status`` and ``dashboard_fields``.

Both GETs do real work on the gateway's shared default executor. The roster scans the
transcript catalog and folds each ended child's crew log; the field list reads the
dashboard instance, the mistake book, the history, the page's values and the template
catalog. When that executor is saturated, every worker-thread hop waits for its queue
before the work starts, so a request pays the queue's wait once per hop, while
``mcp_core._get`` hands a read that names no budget the 10 s a telemetry read needs.

Pinned here, each half on its own:

* the CLIENT budget each tool gives its GET, through the real ``mcp_core`` plumbing
  with only the socket faked, against a route slower than the telemetry default;
* the field list's SERVER half paying ONE queue wait for all of its reads, counted on
  a default executor that records what is submitted to it rather than timed.

The roster's server half is unchanged here; how it classifies a closed or lost child is
pinned in ``test_session_status.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
import json
import threading
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request
from test_session_ledger import _TimingUrlopen

from kiro_crew import dashboard_agentic, mcp_core, mcp_dashboard, mcp_panel
from kiro_crew import members as members_mod
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.mcp_dashboard import SESSION_STATUS_READ_TIMEOUT_SECS
from kiro_crew.mcp_tools.dashboard_client import LoopbackDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

#: Longer than ``mcp_core._get``'s 10 s telemetry default and shorter than either
#: tool's budget: the route the old bound failed on and the new one must not.
SLOW_ROUTE_SECS = 12.0

CALLER = "dashboard:chat-1"


@pytest.fixture
def slow_gateway(monkeypatch):
    """Point ``mcp_core`` at a socket that times out below ``SLOW_ROUTE_SECS``."""

    def _install(payload: dict[str, Any]) -> _TimingUrlopen:
        socket = _TimingUrlopen(SLOW_ROUTE_SECS, payload)
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: CALLER)
        monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "secret")
        monkeypatch.setattr(mcp_core, "_resolve_api_target", lambda: ("http://127.0.0.1:9", ""))
        monkeypatch.setattr(mcp_core, "_api_urlopen", socket)
        return socket

    return _install


# ── The client budgets ───────────────────────────────────────────────────────


def test_session_status_reads_a_roster_slower_than_the_telemetry_budget(slow_gateway):
    """The patrol read on a busy gateway. Given only the 10 s ``_get`` default it
    answered ``could not read your session roster: timed out`` while the same
    conductor's ``session_send`` calls, on ``_post``'s 30 s, kept landing."""
    socket = slow_gateway(
        {
            "ok": True,
            "tree": "readable",
            "history": "readable",
            "sessions": [{"target": "chat-2", "title": "worker", "status": "idle"}],
        }
    )
    ctx = ToolContext(LoopbackDashboardClient(), Caller.strict(CALLER))

    out = mcp_dashboard.TABLE.call("session_status", {}, ctx)

    assert (
        "could not read your session roster" not in out
    ), f"the roster read was dialled with timeout={socket.timeouts}"
    assert "chat-2" in out
    assert socket.timeouts and min(socket.timeouts) >= SLOW_ROUTE_SECS


_FIELDS_PAYLOAD = {
    "template": {"id": "oncall", "version": 3},
    "instance_version": 7,
    "fields": [
        {"field": "open_items", "type": "number", "source": "agentic"},
        {"field": "asks", "type": "object", "source": "agentic"},
    ],
    "mistakes": [],
}


def test_dashboard_fields_reads_a_field_list_slower_than_the_telemetry_budget(slow_gateway):
    socket = slow_gateway(_FIELDS_PAYLOAD)

    out = mcp_panel._call_tool_inner("dashboard_fields", {})

    assert not out.startswith(
        "Error:"
    ), f"the field list was dialled with timeout={socket.timeouts}: {out}"
    assert "open_items" in out
    assert socket.timeouts and min(socket.timeouts) >= SLOW_ROUTE_SECS


def test_a_clear_reads_the_field_types_on_the_same_budget(slow_gateway):
    """``clear: true`` asks the same route for the field's type before it writes.
    On the old bound a slow gateway made that lookup fail, and an OBJECT field was
    then cleared with ``[]``, which the write route refuses as ``wrong_type`` and
    records in the mistake book as the agent's own error."""
    socket = slow_gateway(_FIELDS_PAYLOAD)

    assert mcp_panel._empty_value_for("asks", CALLER) == {}
    assert socket.timeouts and min(socket.timeouts) >= SLOW_ROUTE_SECS


def test_both_read_budgets_match_the_write_path():
    """The reads get the budget of the writes their callers make beside them.

    ``session_send`` and ``dashboard_write`` reach the same gateway through
    ``_post``, which leaves ``_send``'s default alone. A read allowed less fails
    first on a busy gateway while its sibling write succeeds -- the asymmetry no
    caller can predict from the tools' descriptions. Both floors are read off the
    transport rather than restated, so they cannot drift apart silently.
    """
    write_default = inspect.signature(mcp_core._send).parameters["timeout"].default
    telemetry_default = inspect.signature(mcp_core._get).parameters["timeout"].default
    for budget in (SESSION_STATUS_READ_TIMEOUT_SECS, mcp_panel.DASHBOARD_FIELDS_READ_TIMEOUT_SECS):
        assert budget >= write_default
        assert budget > telemetry_default


# ── The field list pays one queue wait ───────────────────────────────────────


class _CountingPool(concurrent.futures.ThreadPoolExecutor):
    """The loop's default executor, recording what each job submitted to it runs.

    On a saturated gateway every job submitted here first waits behind the queue, so
    the number of submissions a request makes is the number of queue waits it pays.
    Counting them measures that without a clock.
    """

    def __init__(self) -> None:
        super().__init__(max_workers=2, thread_name_prefix="counted-default")
        self.submitted: list[str] = []

    def submit(self, fn, /, *args, **kwargs):
        target = fn.args[0] if isinstance(fn, functools.partial) and fn.args else fn
        self.submitted.append(getattr(target, "__qualname__", repr(target)))
        return super().submit(fn, *args, **kwargs)


#: Every read the field list makes after its caller gate.
_FIELD_LIST_READS = ("read_instance", "_mistake_book", "_instance_history", "_current_values")


@pytest.mark.asyncio
async def test_the_field_list_pays_one_queue_wait_for_all_its_reads(monkeypatch):
    """The five reads after the gate run inside ONE worker job.

    Each sequential ``asyncio.to_thread`` hop queues on the shared executor, so on a
    saturated gateway the read paid the queue's wait five times over work that takes
    milliseconds. The gate is replaced here because its hops are its own and are not
    what this pins; every read below it runs for real against a shipped page. A
    crewmate that adopted nothing is shown a page only when a shipped one is named for
    it, so the crewmate here is ``kirocrew-conductor``, whose board is that page.

    Mutation guards: give any read its own hop again and the count is two; move one
    onto the event loop and its thread is the loop's.
    """
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    crew = "kirocrew-conductor"
    slug = members_mod.member_slug(crew)
    slot = members_mod.member_slot_key(slug)

    async def _vetted(_request, _operation):
        return (slug, crew, slot), None

    monkeypatch.setattr(routes, "_resolve_dashboard_caller", _vetted)
    ran_on: dict[str, int] = {}

    def _recording(name: str, real):
        def _read(*args, **kwargs):
            ran_on[name] = threading.get_ident()
            return real(*args, **kwargs)

        return _read

    for name in _FIELD_LIST_READS:
        monkeypatch.setattr(routes, name, _recording(name, getattr(routes, name)))
    monkeypatch.setattr(
        dashboard_agentic,
        "fields_for_agent",
        _recording("fields_for_agent", dashboard_agentic.fields_for_agent),
    )
    pool = _CountingPool()
    asyncio.get_running_loop().set_default_executor(pool)
    try:
        response = await asyncio.wait_for(
            routes.api_dashboard_fields(
                make_mocked_request("GET", "/api/agent-panel/dashboard/fields")
            ),
            timeout=60,
        )
    finally:
        pool.shutdown(wait=True)

    assert response.status == 200, response.body
    assert json.loads(response.body)["fields"], "the reads still answer with the field list"
    assert len(pool.submitted) == 1, f"one queue wait, not one per read: {pool.submitted}"
    assert set(ran_on) == {*_FIELD_LIST_READS, "fields_for_agent"}
    assert len(set(ran_on.values())) == 1, "every read ran inside the one job"
    assert threading.get_ident() not in ran_on.values(), "and none on the event loop"
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()

"""Chat send admission work: whole-inventory reads before a turn's first byte.

A chat send must not pay for a session listing, an agent catalog scan or a
process-tree walk on its way to the first SSE frame, because at scale each of
those is O(sessions), O(specs) or O(processes) and a cold-start herd multiplies
it.

What this covers: the real ``POST /api/chat`` handler, with ``_run_chat``
replaced by a turn that emits one chunk and parks. The counts span the
handler's own admission (slot lookup, auth, the SSE response) up to that frame,
and each is pinned at its measured value, 0.

What this does not cover: everything inside the real ``_run_chat``. That is
``_turn_open``, ``_turn_slash_gate``, ``_turn_admit`` (which runs
``refresh_materialized_agents`` on a cold or incomplete snapshot,
``chat_turn/turn_admission.py``), ``_turn_dispatch_prep``, the app-agent warm in
``chat_turn/eager_spawn.py``, agent binding and the runtime spawn. Those need an
in-process provider double to run a turn on, and the strict xfail below holds
that budget open until one exists.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.testing.wait import async_wait_until

from .conftest import Spy, harness_fail

pytestmark = pytest.mark.scale_budget

# A stream that never sends the fake turn's first chunk fails by name here
# instead of hanging the xdist worker until pytest-timeout kills it.
TURN_PARK_CEILING_S = 30.0


def _install_counters(spy: Spy) -> list[tuple[object, str]]:
    """Spy every inventory read on each name a turn-path call site binds.

    Returns the ``(module, name)`` bindings wrapped, so the test can check each
    still points at its spy.
    """
    from kiro_crew.acp import runtime_process_tree, runtime_tree_kill
    from kiro_crew.agent_discovery_parts import agent_roster
    from kiro_crew.config import loader_snapshots
    from kiro_crew.history import ConversationLog

    wrapped: list[tuple[object, str]] = []
    for name in ("list_sessions", "list_sessions_indexed", "has_messages"):
        if hasattr(ConversationLog, name):
            spy.wrap(ConversationLog, name, f"listing:{name}")
            wrapped.append((ConversationLog, name))
    spy.wrap(loader_snapshots, "_scan_materialized_index", "catalog:scan")
    wrapped.append((loader_snapshots, "_scan_materialized_index"))
    if hasattr(agent_roster, "_dir_signature"):
        spy.wrap(agent_roster, "_dir_signature", "catalog:list_agents")
        wrapped.append((agent_roster, "_dir_signature"))
    # ``runtime_tree_kill`` imports ``_get_child_pids`` by name, so a walk
    # reached through it never touches ``runtime_process_tree``'s binding.
    for module in (runtime_process_tree, runtime_tree_kill):
        if hasattr(module, "_get_child_pids"):
            spy.wrap(module, "_get_child_pids", "process_tree:walk")
            wrapped.append((module, "_get_child_pids"))
    return wrapped


@pytest.mark.asyncio
async def test_handler_admission_costs_no_inventory_read(tmp_path, monkeypatch, spy) -> None:
    """Listing, catalog and process-tree calls before the first SSE frame are each 0."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    release = asyncio.Event()
    finished = asyncio.Event()

    async def fake_run_chat(st, sl, msg, **_origin_kwargs):
        sl.append("chunk", "first byte", "chunk")
        try:
            await asyncio.wait_for(release.wait(), timeout=TURN_PARK_CEILING_S)
        finally:
            finished.set()

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    async with TestClient(TestServer(_make_app(state))) as client:
        wrapped = _install_counters(spy)
        resp = await asyncio.wait_for(
            client.post("/api/chat", json={"message": "hello", "slot": "s1"}),
            timeout=TURN_PARK_CEILING_S,
        )
        assert resp.status == 200
        seen = b""

        async def _read_to_first_byte() -> None:
            nonlocal seen
            async for chunk in resp.content.iter_any():
                seen += chunk
                if b"first byte" in seen:
                    return

        await asyncio.wait_for(_read_to_first_byte(), timeout=TURN_PARK_CEILING_S)
        if b"first byte" not in seen:
            harness_fail(f"the SSE stream ended before the fake turn's first chunk: {seen[:200]!r}")
        measured = Counter(spy.counts)
        release.set()
        resp.close()
        await async_wait_until(finished.is_set, describe=lambda: "the fake turn never ended")

    over = {name: n for name, n in measured.items() if n > 0}
    assert not over, (
        f"api_chat handler admission ran {over} before the first SSE frame "
        f"(budget 0 each; all counts {dict(measured)})"
    )
    # Positive controls: each spy is still the binding its call sites read, and
    # the listing spy counts a call made through the same log.
    stale = [
        f"{getattr(m, '__name__', m)}.{n}"
        for m, n in wrapped
        if getattr(m, n).__name__ != "counted"
    ]
    if stale:
        harness_fail(f"spies replaced before the measurement ended: {stale}")
    state.conversation_log.list_sessions()
    if spy.counts["listing:list_sessions"] != measured["listing:list_sessions"] + 1:
        harness_fail("the listing spy did not count a list_sessions call on the state's log")


#: The in-process seam that runs a real ``_run_chat`` to its first provider frame:
#: a provider double the turn path accepts, so the counters above can span
#: ``_turn_admit``. ``kiro_crew.testing.fake_acp_backend`` is not that seam: it is
#: an ACP subprocess for ``spawn_feature_gateway``, whose calls no in-process spy
#: sees. ``None`` until the F16 PR adds the seam and the budget together.
TURN_PATH_PROVIDER_DOUBLE = None


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F16: no in-process provider double runs a real _run_chat, so turn admission is unbudgeted",
)
def test_turn_path_admission_is_budgeted() -> None:
    """Placeholder for the turn-path budget the handler test above cannot reach.

    The F16 PR replaces this body with the real budget: one send through the
    real ``_run_chat`` to its first provider frame costs 0 listings, 0
    process-tree walks and at most 1 catalog scan (the cold-snapshot rescan in
    ``_turn_admit``), and a second send costs 0 catalog scans.
    """
    assert TURN_PATH_PROVIDER_DOUBLE is not None, (
        "scale-F16: the real turn path (_turn_admit, eager_spawn, agent binding, runtime "
        "spawn) has no in-process provider double, so it has no budget"
    )

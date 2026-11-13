"""Chat send admission work: whole-inventory reads before a turn's first byte.

A chat send must not pay for a session listing, an agent catalog scan or a
process-tree walk on its way to the first SSE frame, because at scale each of
those is O(sessions), O(specs) or O(processes) and a cold-start herd multiplies
it.

What this covers: the real ``POST /api/chat`` handler, with ``_run_chat``
replaced by a turn that emits one chunk and parks. The counts span the
handler's own admission (slot lookup, auth, the SSE response) up to that frame,
and each is pinned at its measured value, 0.

The second test covers the real ``_run_chat`` behind it: ``_turn_open``,
``_turn_slash_gate``, ``_turn_admit``, ``_turn_dispatch_prep`` and agent
binding, up to the provider's stream. Its provider is the scripted session
double of ``chat_runner_coverage_helpers``, so the runtime spawn and the ACP
session are doubles and their own cost is not counted here.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot
from chat_test_helpers import _make_app, _make_state

from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.providers.base import LLMEvent
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


def _refreshes(spy: Spy) -> int:
    """Agent snapshot refreshes counted so far: one scan of the user agents directory each."""
    from kiro_crew.config.paths import kiro_agents_dir

    user_dir = kiro_agents_dir()
    return sum(
        1 for args, _ in spy.calls.get("catalog:scan_dir", []) if args and args[0] == user_dir
    )


def _turn(spy: Spy) -> dict[str, int]:
    return {
        "listing": sum(n for label, n in spy.counts.items() if label.startswith("listing:")),
        "process_tree": spy.counts["process_tree:walk"],
        "catalog_refresh": _refreshes(spy),
    }


@pytest.mark.asyncio
async def test_turn_path_admission_is_budgeted(tmp_path, spy) -> None:
    """A real ``_run_chat`` send: 0 listings, 0 tree walks, at most 1 catalog refresh.

    The first send refreshes the cold agent snapshot once (``dispatch_kiro_agent``
    during agent binding scans the user and crewmate agents directories); the
    second send on the same slot reads the snapshot from memory and refreshes
    nothing. The root conftest resets the snapshot before each test, so the first
    send here is always the cold one.
    """
    from kiro_crew.config import loader_snapshots

    state, client = _runner_state(tmp_path)
    slot = _slot()
    wrapped = _install_counters(spy)
    spy.wrap(loader_snapshots, "_scan_materialized_index", "catalog:scan_dir", record=True)

    turns = []
    for text in ("first", "second"):
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text=text), _complete()])
        await _drive(state, slot, text)
        if client.stream.call_count != 1:
            harness_fail(f"the {text} send never reached the provider stream")
        turns.append(_turn(spy))
        spy.reset()

    first, second = turns
    assert first["listing"] == 0 and second["listing"] == 0, f"sends listed sessions: {turns}"
    assert (
        first["process_tree"] == 0 and second["process_tree"] == 0
    ), f"sends walked a process tree: {turns}"
    assert first["catalog_refresh"] <= 1, f"the first send refreshed the catalog: {first}"
    assert second["catalog_refresh"] == 0, f"the second send refreshed the catalog: {second}"
    stale = [
        f"{getattr(m, '__name__', m)}.{n}"
        for m, n in wrapped
        if getattr(m, n).__name__ != "counted"
    ]
    if stale:
        harness_fail(f"spies replaced before the measurement ended: {stale}")
    # Positive control: the counter sees a refresh made through the same binding.
    # Moving the spec generation makes it scan rather than reuse the snapshot.
    from kiro_crew.agent_discovery import clear_list_agents_cache

    clear_list_agents_cache()
    loader_snapshots.refresh_materialized_agents(heal_default=False)
    if _refreshes(spy) != 1:
        harness_fail("the catalog spy did not count a direct refresh_materialized_agents call")

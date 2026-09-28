"""Tests for ``GET /api/sessions/memory``.

Follows the handler-test convention in ``test_dashboard_sessions_clear.py``: call
the handler directly with a faked request/state rather than standing up aiohttp.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.sessions import api_sessions_memory


class _FakeSlot:
    def __init__(self, title: str) -> None:
        self.display_title = title


def _make_request(
    rows: list[dict[str, object]],
    *,
    slots: dict[str, _FakeSlot] | None = None,
    tasks: list[dict[str, object]] | None = None,
    with_subagents: bool = True,
) -> web.Request:
    slots = slots or {}
    sessions = MagicMock()
    sessions.runtime_pids.return_value = rows
    state = MagicMock()
    state.sessions = sessions
    state.get_slot.side_effect = lambda name: slots.get(name)
    if with_subagents:
        subagents = MagicMock()
        subagents.task_memory_rows.return_value = tasks or []
        state.subagents = subagents
    else:
        state.subagents = None
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    return request


def _row(key: str, pid: int | None) -> dict[str, object]:
    return {
        "key": key,
        "agent": "kirocrew",
        "pid": pid,
        "owns_runtime": True,
        "created_at": 1000.0,
        "prompts": 1,
    }


@pytest.fixture(autouse=True)
def stub_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the handler off real /proc — the sampler is unit-tested separately."""
    from kiro_crew.dashboard import session_memory as sm

    monkeypatch.setattr(sm.sys, "platform", "linux")
    # The sampler hands its already-walked set to _get_rss_tree_mb as pids=, so
    # the stub must tolerate that keyword. The walk-counting test below restores
    # the real function deliberately: this stub would hide the walk it performs.
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid, **kw: 1525.0)
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid, **kw: [pid, pid + 1])
    monkeypatch.setattr(sm, "process_matches", lambda pid, needles: False)
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid, **kw: 0)
    # One host parent map per poll feeds every row's walk; keep it off real /proc.
    monkeypatch.setattr(sm, "proc_child_map", lambda: {})
    monkeypatch.setattr(sm, "_get_static_system_info", lambda: {"mem_total_gb": 48.0})


async def _call(request: web.Request) -> tuple[int, dict]:
    resp = await api_sessions_memory(request)
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_returns_titles_resolved_through_the_slot() -> None:
    request = _make_request(
        [_row("dashboard:chat-69", 7)],
        slots={"chat-69": _FakeSlot("GitHub PR review explanation request")},
    )
    status, body = await _call(request)

    assert status == 200
    assert body["sessions"][0]["title"] == "GitHub PR review explanation request"
    assert body["sessions"][0]["slot_key"] == "chat-69"


@pytest.mark.asyncio
async def test_payload_carries_tasks_totals_and_history() -> None:
    tasks = [{"id": "t1", "task": "aspect-review", "rss_mb": 900.0, "sampled": True}]
    status, body = await _call(_make_request([_row("dashboard:a", 7)], tasks=tasks))

    assert status == 200
    assert body["tasks"] == tasks
    assert body["totals"]["rss_mb"] == 1525.0
    assert body["totals"]["host_mb"] == pytest.approx(49152.0)
    assert len(body["history"]) >= 1


@pytest.mark.asyncio
async def test_works_before_the_subagent_manager_exists() -> None:
    """The dashboard serves requests during startup, when state.subagents is None;
    a task-manager view must degrade to sessions-only rather than 500."""
    status, body = await _call(_make_request([_row("dashboard:a", 7)], with_subagents=False))

    assert status == 200
    assert body["tasks"] == []


# ── channel field ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_row_carries_channel_from_telemetry_channel_of() -> None:
    """Every row must carry a ``channel`` derived from ``telemetry_channel_of``.

    The assertion compares against the function's output, NOT a hardcoded string,
    so the test cannot drift from the taxonomy.
    """
    from kiro_crew.messaging.link import telemetry_channel_of

    key = "dashboard:chat-42"
    request = _make_request([_row(key, 7)])
    status, body = await _call(request)

    assert status == 200
    row = body["sessions"][0]
    assert "channel" in row
    assert row["channel"] == telemetry_channel_of(key)


@pytest.mark.asyncio
async def test_non_dashboard_session_gets_its_own_channel() -> None:
    """A cron or Slack session must resolve to its own channel, not dashboard."""
    from kiro_crew.messaging.link import telemetry_channel_of

    key = "cron:daily-check"
    request = _make_request([_row(key, 8)])
    status, body = await _call(request)

    assert status == 200
    row = body["sessions"][0]
    assert row["channel"] == telemetry_channel_of(key)
    # Coherence check: a cron key must NOT resolve to "dashboard"
    assert row["channel"] != "dashboard"


@pytest.mark.asyncio
async def test_non_string_key_does_not_raise_and_still_has_channel() -> None:
    """The production code guards non-string keys with isinstance; prove it."""
    from kiro_crew.messaging.link import telemetry_channel_of

    row_data = {
        "key": 12345,  # non-string key
        "agent": "kirocrew",
        "pid": 9,
        "owns_runtime": True,
        "created_at": 1000.0,
        "prompts": 1,
    }
    request = _make_request([row_data])
    status, body = await _call(request)

    assert status == 200
    row = body["sessions"][0]
    assert "channel" in row
    # Non-string -> telemetry_channel_of(None) -> "unknown"
    assert row["channel"] == telemetry_channel_of(None)


# ── credits / turns fields ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_row_carries_credits_and_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A session with shard rows surfaces credits and turns on its row."""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    now = datetime.now(timezone.utc)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")
    rows = [
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-69-100", "credits": 4.5},
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-69-100", "credits": 2.5},
    ]
    shard.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    request = _make_request(
        [_row("dashboard:chat-69-100", 7)],
        slots={"chat-69-100": _FakeSlot("Test session")},
    )
    status, body = await _call(request)
    assert status == 200
    row = body["sessions"][0]
    assert row["credits"] == pytest.approx(7.0)
    assert row["turns"] == 2


@pytest.mark.asyncio
async def test_session_row_credits_null_when_no_shard_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A session without shard rows gets credits=null, turns=null — NOT zero."""
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    tmp.mkdir(exist_ok=True)
    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    request = _make_request([_row("dashboard:chat-99", 7)])
    status, body = await _call(request)
    assert status == 200
    row = body["sessions"][0]
    assert row["credits"] is None
    assert row["turns"] is None


def test_one_descendant_walk_per_distinct_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """One poll walks each session's process tree ONCE for its RSS and process
    metadata, and once per PID.

    A cost bound, so it is asserted structurally -- the number of walks, not
    elapsed time: a wall-clock budget over a /proc walk claims a figure for
    whatever else the runner is doing and flakes on a loaded one. The walk is
    where a sample's cost sits (measured over 245 live trees: a median 10.3ms
    for one walk against 0.8ms to sum RSS over the set it returns), the call is
    on a browser poll, and the bound can be lost in two ways. Per ROW:
    co-tenants of a multiplexed runtime share a pid, so a walk per row costs the
    page a multiple of its own budget. Per SAMPLE: the RSS total and the process
    metadata both come off the same set, so reaching them through two helpers
    walks the same pids twice. Both stay green under every payload assertion, so
    the counter goes on the runtime module as well as this module's binding, and
    the RSS path is left REAL -- stubbing it hides the walk it performs, which is
    how the per-sample half went unnoticed. Three rows on two pids plus an
    unstarted one must be two walks.

    The CPU reading is counted here too, because it does not enumerate for
    itself: it is handed the set this walk returned. A regression that drops
    ``pids=`` there does not raise the count in THIS test (the walker it would
    fall back to is a different function) -- that direction is pinned by
    ``test_the_cpu_total_is_summed_over_the_walked_set``.
    """
    from kiro_crew.acp import runtime
    from kiro_crew.dashboard import session_memory as sm
    from kiro_crew.dashboard.handlers import usage

    walked: list[int] = []

    def counting_walk(
        pid: int, max_depth: int | None = None, *, children: dict[int, list[int]] | None = None
    ) -> list[int]:
        walked.append(pid)
        return [pid, pid + 1]

    monkeypatch.setattr(sm, "_iter_descendant_pids", counting_walk)
    monkeypatch.setattr(runtime, "_iter_descendant_pids", counting_walk)
    monkeypatch.setattr(runtime, "_get_rss_mb", lambda pid: 6.0)
    # Undo the module fixture's RSS stub: this test needs the real function, both
    # so the total is shown to come off the set the single walk returned, and so
    # that a regression dropping ``pids=`` walks again and gets counted.
    monkeypatch.setattr(sm, "_get_rss_tree_mb", runtime._get_rss_tree_mb)
    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "process_matches", lambda pid, needles: False)
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid, **kw: 0)
    monkeypatch.setattr(sm, "proc_child_map", lambda: {})
    monkeypatch.setattr(usage, "slot_spend", lambda: {})

    rows = [
        _row("dashboard:chat-1", 7),  # owns the runtime
        _row("dashboard:chat-2", 7),  # co-tenant: same pid, must not walk again
        _row("dashboard:chat-3", 9),
        _row("dashboard:chat-4", None),  # not started: nothing to walk
    ]
    sampler = sm.SessionMemorySampler()
    samples = sampler._blocking_sample(rows)

    assert walked == [7, 9]
    per_pid = samples["per_pid"]
    assert isinstance(per_pid, dict)
    assert sorted(per_pid) == [7, 9]
    # Not just cheaper -- still correct: the total is summed over the set the
    # single walk returned (two pids at 6.0 MiB), so the pass that was removed
    # was the duplicate one, not the one that produces the number.
    assert per_pid[7]["rss_mb"] == 12.0
    assert per_pid[7]["procs"] == 2


def test_the_host_parent_map_is_built_once_per_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's parent map is read ONCE for the whole poll, and every row's
    walk reads its edges from it.

    The map is what replaced the per-root question to the kernel, which cost one
    read per THREAD of every process visited -- so building it per ROW would put
    the host scan on the very multiplier it was introduced to remove. Counted,
    not timed, for the reason the walk counter above gives.

    The second assertion is the one that makes the first mean anything: a map
    built and then not handed down is a scan paid for and wasted, and it looks
    identical to this one from the call count alone.
    """
    from kiro_crew.dashboard import session_memory as sm
    from kiro_crew.dashboard.handlers import usage

    builds: list[int] = []
    host_map = {7: [71], 9: [91]}

    def counting_map() -> dict[int, list[int]]:
        builds.append(1)
        return host_map

    seen_maps: list[object] = []

    def recording_walk(
        pid: int, max_depth: int | None = None, *, children: dict[int, list[int]] | None = None
    ) -> list[int]:
        seen_maps.append(children)
        return [pid, *(children or {}).get(pid, [])]

    monkeypatch.setattr(sm, "proc_child_map", counting_map)
    monkeypatch.setattr(sm, "_iter_descendant_pids", recording_walk)
    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid, **kw: 1.0)
    monkeypatch.setattr(sm, "process_matches", lambda pid, needles: False)
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid, **kw: 0)
    monkeypatch.setattr(usage, "slot_spend", lambda: {})

    rows = [
        _row("dashboard:chat-1", 7),
        _row("dashboard:chat-2", 7),  # co-tenant on the same pid
        _row("dashboard:chat-3", 9),
        _row("dashboard:chat-4", None),
    ]
    sampler = sm.SessionMemorySampler()
    samples = sampler._blocking_sample(rows)

    assert len(builds) == 1
    # Handed to every row that walked, and it is the map that was built.
    assert seen_maps == [host_map, host_map]
    per_pid = samples["per_pid"]
    assert isinstance(per_pid, dict)
    # The tree came OUT of the map: pid 7 plus the one child the map gives it.
    assert per_pid[7]["procs"] == 2


def test_the_cpu_total_is_summed_over_the_walked_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CPU figure is totalled over the subtree the poll already walked, and
    the subtree walker is not asked for a second enumeration.

    This is the half the walk counter above cannot see: a regression that stops
    handing ``pids=`` down falls back to ``proc_subtree_sample``, which walks the
    tree itself and is a different function, so the count of THIS module's walks
    would not move. Asserting the walker is never entered is what pins it.

    ``_subtree_cpu_jiffies`` is left REAL here for the same reason the RSS path is
    left real in the counter above: stubbing it is exactly what hides the
    traversal under test.
    """
    from kiro_crew import platform_compat
    from kiro_crew import subagent as sa
    from kiro_crew.acp import runtime
    from kiro_crew.dashboard import session_memory as sm
    from kiro_crew.dashboard.handlers import usage

    walker_calls: list[int] = []

    def refuse_walk(pid: int, **kw: object) -> object:
        walker_calls.append(pid)
        raise AssertionError("the subtree walker must not be entered")

    monkeypatch.setattr(platform_compat, "proc_subtree_sample", refuse_walk)
    # One jiffy per pid, so the total names how many pids were summed.
    monkeypatch.setattr(platform_compat, "_proc_cpu_jiffies", lambda pid: 1)
    monkeypatch.setattr(sm.sys, "platform", "linux")
    # Undo the module fixture's stubs for the two functions under test: stubbing
    # either is exactly what hides the traversal this test is about.
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", sa._subtree_cpu_jiffies)
    monkeypatch.setattr(sm, "_iter_descendant_pids", runtime._iter_descendant_pids)
    monkeypatch.setattr(sm, "proc_child_map", lambda: {7: [71, 72]})
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid, **kw: 1.0)
    monkeypatch.setattr(sm, "process_matches", lambda pid, needles: False)
    monkeypatch.setattr(usage, "slot_spend", lambda: {})

    sampler = sm.SessionMemorySampler()
    # First poll seeds the CPU baseline; the second produces a rate from it.
    sampler._blocking_sample([_row("dashboard:chat-1", 7)])
    baseline = sampler._cpu_prev[7]
    sampler._blocking_sample([_row("dashboard:chat-1", 7)])

    assert walker_calls == []
    # Three pids (the root and the map's two children), one jiffy each, on both
    # polls -- so the total describes the set the row's other figures describe.
    assert baseline[0] == 3
    assert sampler._cpu_prev[7][0] == 3


class TestTheWalkReadsItsEdgesFromTheMap:
    """``_iter_descendant_pids(children=...)``: same walk, edges from a map.

    Only where an edge comes from changes, so what must be pinned is that the
    walk's own rules are untouched -- the depth bound, the single visit, and what
    an absent process yields -- and that the map route never reads ``/proc``.
    """

    def test_it_returns_the_whole_subtree_from_the_map(self) -> None:
        from kiro_crew.acp import runtime

        tree = {1: [2, 3], 2: [4], 4: [5]}
        assert sorted(runtime._iter_descendant_pids(1, children=tree)) == [1, 2, 3, 4, 5]

    def test_it_does_not_touch_proc_when_given_a_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point: a map route that still read per root would keep the
        cost it was introduced to remove, and every other assertion here passes
        either way."""
        from kiro_crew.acp import runtime

        def refuse(*_a: object, **_k: object) -> object:
            raise AssertionError("the map route must not ask the kernel per root")

        monkeypatch.setattr(runtime, "_own_children", refuse)
        assert runtime._iter_descendant_pids(1, children={1: [2]}) == [1, 2]

    def test_the_depth_bound_still_holds(self) -> None:
        from kiro_crew.acp import runtime

        tree = {1: [2], 2: [3], 3: [4]}
        assert runtime._iter_descendant_pids(1, 0, children=tree) == [1]
        assert sorted(runtime._iter_descendant_pids(1, 1, children=tree)) == [1, 2]
        assert sorted(runtime._iter_descendant_pids(1, 2, children=tree)) == [1, 2, 3]

    def test_a_pid_reachable_twice_is_visited_once(self) -> None:
        """A damaged or looping map must terminate and count each pid once, the
        same single-visit rule the kernel route has."""
        from kiro_crew.acp import runtime

        diamond = {1: [2, 3], 2: [4], 3: [4], 4: [1]}
        order = runtime._iter_descendant_pids(1, children=diamond)
        assert sorted(order) == [1, 2, 3, 4]
        assert len(order) == len(set(order))

    def test_a_process_absent_from_the_map_is_the_root_alone(self) -> None:
        """What an unreadable ``children`` file yields on the kernel route."""
        from kiro_crew.acp import runtime

        assert runtime._iter_descendant_pids(99, children={1: [2]}) == [99]

    def test_without_a_map_it_asks_the_kernel_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path is unchanged: the RSS watchdog and every other caller
        that names no map keep the walk they had."""
        from kiro_crew.acp import runtime

        asked: list[int] = []

        def fake_children(pid: int) -> list[int]:
            asked.append(pid)
            return [pid + 1] if pid == 1 else []

        monkeypatch.setattr(runtime, "_own_children", fake_children)
        assert runtime._iter_descendant_pids(1) == [1, 2]
        assert asked == [1, 2]


def test_the_stub_count_is_matched_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mcp`` asks ``platform_compat.process_matches``, the one helper the
    cross-platform table names for matching a command line.

    A second matcher answering the same question is what this pins out. The
    needle is asserted too, not just the call: a helper asked about the wrong
    needle counts the wrong processes while every row still renders.
    """
    from kiro_crew.dashboard import session_memory as sm
    from kiro_crew.dashboard.handlers import usage

    asked: list[tuple[int, tuple[str, ...]]] = []

    def fake_matches(pid: int, needles: tuple[str, ...]) -> bool:
        asked.append((pid, needles))
        return pid in (71, 72)

    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "process_matches", fake_matches, raising=False)
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid, **kw: [pid, 71, 72, 73])
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid, **kw: 1.0)
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid, **kw: 0)
    monkeypatch.setattr(sm, "proc_child_map", lambda: {})
    monkeypatch.setattr(usage, "slot_spend", lambda: {})

    sampler = sm.SessionMemorySampler()
    out = sampler._blocking_sample([_row("dashboard:chat-1", 7)])

    per_pid = out["per_pid"]
    assert isinstance(per_pid, dict)
    assert per_pid[7]["mcp"] == 2
    # Every process in the walked set is offered, and always with the stub needle.
    assert [pid for pid, _ in asked] == [7, 71, 72, 73]
    assert {needles for _, needles in asked} == {(sm._STUB_MARKER,)}


def test_a_session_tree_wider_than_the_walker_ceiling_is_counted_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session row's figures are UNCAPPED: a tree larger than
    ``platform_compat._SUBTREE_MAX_PROCS`` reports its real size, not the ceiling.

    A PIN, not a regression test -- this is already the behaviour. It exists
    because the opposite is a one-line change that no payload assertion would
    notice: a capped ``procs`` is a plain integer indistinguishable from a
    complete one, so the card would present the ceiling as an exact count. Both
    the count and the CPU total are checked, because they reach the tree by
    different routes (``len`` of the walk, and the pid hand-over to
    ``_subtree_cpu_jiffies``) and only the second one can silently fall back to
    the capped shared walker.
    """
    from kiro_crew import platform_compat
    from kiro_crew import subagent as sa
    from kiro_crew.acp import runtime
    from kiro_crew.dashboard import session_memory as sm
    from kiro_crew.dashboard.handlers import usage

    wide = platform_compat._SUBTREE_MAX_PROCS + 45
    children = {7: list(range(100, 100 + wide - 1))}

    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "_iter_descendant_pids", runtime._iter_descendant_pids)
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", sa._subtree_cpu_jiffies)
    monkeypatch.setattr(sm, "proc_child_map", lambda: children)
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid, **kw: 1.0)
    monkeypatch.setattr(sm, "process_matches", lambda pid, needles: True)
    # One jiffy per pid, so the CPU total names how many processes were summed.
    monkeypatch.setattr(platform_compat, "_proc_cpu_jiffies", lambda pid: 1)
    monkeypatch.setattr(usage, "slot_spend", lambda: {})

    sampler = sm.SessionMemorySampler()
    out = sampler._blocking_sample([_row("dashboard:chat-1", 7)])

    per_pid = out["per_pid"]
    assert isinstance(per_pid, dict)
    assert wide > platform_compat._SUBTREE_MAX_PROCS
    assert per_pid[7]["procs"] == wide
    assert per_pid[7]["mcp"] == wide
    # The CPU baseline spans the same set, so no figure on the row stops at 256.
    assert sampler._cpu_prev[7][0] == wide

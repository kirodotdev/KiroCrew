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
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid: [pid, pid + 1])
    monkeypatch.setattr(sm, "_read_cmdline", lambda pid: "")
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid: 0)
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

    Not counted here: the CPU reading traverses the subtree through a different
    walker (``platform_compat.proc_subtree_sample``), which this test stubs out
    via ``_subtree_cpu_jiffies``. This counter therefore bounds the RSS and
    process-metadata pass only.
    """
    from kiro_crew.acp import runtime
    from kiro_crew.dashboard import session_memory as sm
    from kiro_crew.dashboard.handlers import usage

    walked: list[int] = []

    def counting_walk(pid: int, max_depth: int | None = None) -> list[int]:
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
    monkeypatch.setattr(sm, "_read_cmdline", lambda pid: "")
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid: 0)
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

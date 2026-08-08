"""Tests for per-session / per-task memory accounting.

The load-bearing behaviours here are the ones a naive implementation gets wrong:
a multiplexed runtime must be counted ONCE in the total (co-tenants share a pid),
an unsampled row must read as unknown rather than zero, and an untitled session
must stay distinguishable from every other untitled session.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import session_memory as sm


class _FakeSlot:
    def __init__(self, title: str) -> None:
        self.display_title = title


class _FakeSessions:
    """Stands in for SessionManager: only ``runtime_pids()`` is consumed."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def runtime_pids(self) -> list[dict[str, object]]:
        return self._rows


class _FakeSubagents:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def task_memory_rows(self) -> list[dict[str, object]]:
        return self._rows


def _row(
    key: str, pid: int | None, *, owns: bool = True, created: float = 1000.0
) -> dict[str, object]:
    return {
        "key": key,
        "agent": "kirocrew",
        "pid": pid,
        "owns_runtime": owns,
        "created_at": created,
        "prompts": 3,
    }


# ── session_title ──────────────────────────────────────────────────────────


def test_title_uses_the_slot_display_title() -> None:
    out = sm.session_title("dashboard:chat-69", lambda k: _FakeSlot("Windows testing issue found"))
    assert out == {
        "title": "Windows testing issue found",
        "slot_key": "chat-69",
        "untitled": False,
    }


def test_untitled_session_is_flagged_and_keeps_its_slot_key() -> None:
    """Without the slot key every untitled session renders identically."""
    out = sm.session_title("dashboard:chat-70", lambda k: _FakeSlot(sm.NEW_SESSION_TITLE))
    assert out["untitled"] is True
    assert out["slot_key"] == "chat-70"


def test_missing_slot_falls_back_instead_of_raising() -> None:
    """A session can outlive its dashboard slot; that must not break the page."""
    out = sm.session_title("dashboard:chat-71", lambda k: None)
    assert out["title"] == sm.NEW_SESSION_TITLE
    assert out["untitled"] is True


def test_a_credential_in_a_title_is_redacted_at_the_output_boundary() -> None:
    """Load-bearing, not belt-and-braces: chat_handlers.py:2645 assigns a
    client-supplied body["title"] to slot.title with no scan, so a title can reach
    this serializer unredacted. running_agents_for redacts task text in the same
    payload; titles must not be treated more loosely."""
    leaky = "Debug AKIAIOSFODNN7EXAMPLE rotation"
    out = sm.session_title("dashboard:chat-9", lambda k: _FakeSlot(leaky))

    assert "AKIAIOSFODNN7EXAMPLE" not in str(out["title"])
    assert out["title"] == "Debug [REDACTED: credential] rotation"


def test_redaction_does_not_mangle_an_ordinary_title() -> None:
    out = sm.session_title("dashboard:chat-9", lambda k: _FakeSlot("Windows testing issue found"))
    assert out["title"] == "Windows testing issue found"


def test_untitled_is_decided_before_redaction() -> None:
    """The untitled flag compares against the NEW_SESSION_TITLE sentinel; deciding
    it after redaction would break if the sentinel ever contained a matched
    pattern."""
    out = sm.session_title("dashboard:chat-9", lambda k: _FakeSlot(sm.NEW_SESSION_TITLE))
    assert out["untitled"] is True


@pytest.mark.parametrize(
    ("key", "expected"),
    [(sm.BACKGROUND_KEY, "Background"), ("slack:C123", "slack:C123")],
)
def test_non_dashboard_sessions_keep_a_readable_name(key: str, expected: str) -> None:
    assert sm.session_title(key, lambda k: None)["title"] == expected


# ── history ring ───────────────────────────────────────────────────────────


def test_history_is_bounded_and_ordered_oldest_first() -> None:
    sampler = sm.SessionMemorySampler(history_len=3)
    for i in range(5):
        sampler.record_total(float(i), now=float(i))
    assert [p["mb"] for p in sampler.series()] == [2.0, 3.0, 4.0]


# ── cpu baseline ───────────────────────────────────────────────────────────


def test_first_cpu_sample_reports_unknown_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """CPU is a rate: one observation cannot produce one, and reporting 0.0 would
    claim the session is idle."""
    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid: 100)
    sampler = sm.SessionMemorySampler()
    assert sampler._cpu_cores(42, 10.0) is None


def test_second_cpu_sample_uses_the_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm.sys, "platform", "linux")
    jiffies = iter([100, 100 + sm._CLK_TCK * 2])
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid: next(jiffies))
    sampler = sm.SessionMemorySampler()
    sampler._cpu_cores(42, 10.0)
    # 2 core-seconds of CPU over 4 wall seconds = 0.5 cores.
    assert sampler._cpu_cores(42, 14.0) == pytest.approx(0.5)


def test_dead_pid_baselines_are_pruned() -> None:
    sampler = sm.SessionMemorySampler()
    sampler._cpu_prev = {1: (0, 0.0), 2: (0, 0.0)}
    sampler._prune_cpu_baselines({2})
    assert set(sampler._cpu_prev) == {2}


# ── sample() ───────────────────────────────────────────────────────────────


@pytest.fixture()
def stub_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid: {7: 3238.0, 8: 843.0}.get(pid, 0.0))
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid: [pid, pid + 100, pid + 200])
    monkeypatch.setattr(sm, "_read_cmdline", lambda pid: "python -m kiro_crew.mcp_gateway.stub")
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid: 0)
    monkeypatch.setattr(sm, "_get_static_system_info", lambda: {"mem_total_gb": 124.0})


@pytest.mark.asyncio
async def test_sample_reports_per_session_rows(stub_proc: None) -> None:
    sessions = _FakeSessions([_row("dashboard:a", 7), _row(sm.BACKGROUND_KEY, 8, owns=False)])
    out = await sm.SessionMemorySampler().sample(
        sessions, None, get_slot=lambda k: _FakeSlot("Ported CR")
    )

    rows = {r["key"]: r for r in out["sessions"]}  # type: ignore[union-attr]
    assert rows["dashboard:a"]["rss_mb"] == 3238.0
    assert rows["dashboard:a"]["title"] == "Ported CR"
    assert rows["dashboard:a"]["procs"] == 3
    assert rows["dashboard:a"]["mcp"] == 3
    # A co-tenant of a multiplexed runtime must be labelled, not presented as
    # exclusively owning the measurement.
    assert rows[sm.BACKGROUND_KEY]["owns_runtime"] is False


@pytest.mark.asyncio
async def test_shared_runtime_is_counted_once_in_the_total(stub_proc: None) -> None:
    """Two sessions on ONE runtime report the same pid. Summing per row would
    double a shared runtime into the host total."""
    sessions = _FakeSessions([_row("dashboard:a", 7), _row("dashboard:b", 7, owns=False)])
    out = await sm.SessionMemorySampler().sample(sessions, None)

    totals = out["totals"]
    assert totals["rss_mb"] == 3238.0  # type: ignore[index]
    assert totals["runtimes"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_co_tenants_split_the_shared_runtime_measurement(stub_proc: None) -> None:
    """A ``shared`` row must show its SPLIT of the runtime, not the whole thing.

    The card's tooltip states a shared row's figure is "that runtime's
    measurement divided between them, not an exclusive one". Handing every
    co-tenant the full figure made that copy false and inflated the apparent
    footprint: N sharers read as N times the memory that exists, and any one of
    them could outrank a genuinely large exclusive session.

    An even split is an attribution rather than a measurement -- per-session
    usage inside one interpreter is not observable from /proc -- which is why the
    row keeps its ``shared`` badge.
    """
    sessions = _FakeSessions(
        [
            _row("dashboard:a", 7),
            _row("dashboard:b", 7, owns=False),
            _row("dashboard:c", 7, owns=False),
        ]
    )
    out = await sm.SessionMemorySampler().sample(sessions, None)

    rows = {r["key"]: r for r in out["sessions"]}  # type: ignore[union-attr]
    for key in ("dashboard:a", "dashboard:b", "dashboard:c"):
        assert rows[key]["rss_mb"] == pytest.approx(3238.0 / 3), f"{key} not split"
    # The TOTAL is a measurement, so it stays whole: the runtime really does
    # occupy 3238 MB regardless of how many sessions claim it.
    assert out["totals"]["rss_mb"] == 3238.0  # type: ignore[index]


@pytest.mark.asyncio
async def test_an_exclusive_runtime_is_never_divided(stub_proc: None) -> None:
    """One row on a pid means no sharers -- the figure must pass through intact."""
    sessions = _FakeSessions([_row("dashboard:a", 7)])
    out = await sm.SessionMemorySampler().sample(sessions, None)

    rows = {r["key"]: r for r in out["sessions"]}  # type: ignore[union-attr]
    assert rows["dashboard:a"]["rss_mb"] == 3238.0


@pytest.mark.asyncio
async def test_session_without_a_pid_yields_unknown_not_zero(stub_proc: None) -> None:
    sessions = _FakeSessions([_row("dashboard:a", None)])
    out = await sm.SessionMemorySampler().sample(sessions, None)

    row = out["sessions"][0]  # type: ignore[index]
    assert row["rss_mb"] is None
    assert row["procs"] is None
    assert out["totals"]["rss_mb"] == 0.0  # type: ignore[index]


@pytest.mark.asyncio
async def test_host_percentage_is_relative_to_physical_memory(stub_proc: None) -> None:
    sessions = _FakeSessions([_row("dashboard:a", 7)])
    out = await sm.SessionMemorySampler().sample(sessions, None)

    totals = out["totals"]
    assert totals["host_mb"] == pytest.approx(126976.0)  # type: ignore[index]
    assert totals["host_pct"] == pytest.approx(2.55, abs=0.01)  # type: ignore[index]
    # The number sums per-process RSS, so shared pages are counted more than
    # once; consumers must be able to say so.
    assert totals["rss_is_upper_bound"] is True  # type: ignore[index]


@pytest.mark.asyncio
async def test_tasks_are_passed_through_and_history_records_the_total(stub_proc: None) -> None:
    tasks = [{"id": "t1", "task": "aspect-review", "rss_mb": 1940.0, "sampled": True}]
    sampler = sm.SessionMemorySampler()
    out = await sampler.sample(_FakeSessions([_row("dashboard:a", 7)]), _FakeSubagents(tasks))

    assert out["tasks"] == tasks
    assert [p["mb"] for p in out["history"]] == [3238.0]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_dying_pid_does_not_fail_the_whole_page(
    stub_proc: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(pid: int) -> float:
        raise OSError("vanished")

    monkeypatch.setattr(sm, "_get_rss_tree_mb", boom)
    out = await sm.SessionMemorySampler().sample(_FakeSessions([_row("dashboard:a", 7)]), None)

    assert out["sessions"][0]["rss_mb"] is None  # type: ignore[index]


class _RuntimeStub:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _ClientShapedProvider:
    """AcpProvider shape: the runtime hangs off an inner client."""

    def __init__(self, pid: int) -> None:
        self._client = _RuntimeStub.__new__(_RuntimeStub)
        self._client._runtime = _RuntimeStub(pid)  # type: ignore[attr-defined]


class _SelfShapedProvider:
    """AcpSessionProvider shape (session.py:1331): ``_runtime`` on the provider."""

    def __init__(self, pid: int, *, owns: bool) -> None:
        self._runtime = _RuntimeStub(pid)
        self._owns_runtime = owns


def _manager():
    from unittest.mock import MagicMock

    from kiro_crew.session import SessionManager

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    return SessionManager(cfg=cfg, provider_factory=None)


def test_runtime_pids_resolves_both_provider_shapes() -> None:
    """A provider that stores ``_runtime`` on ITSELF must still report its pid.

    Only AcpProvider nests the runtime under ``_client``; AcpSessionProvider (the
    unified/task-runner path) holds it directly. Resolving just the nested shape
    silently reported pid=None for the latter, so those sessions rendered as
    "unknown memory" forever -- invisible because chat sessions use the other shape.
    """
    from kiro_crew.session import _Session

    mgr = _manager()
    mgr._sessions["dashboard:nested"] = _Session(provider=_ClientShapedProvider(4242))
    mgr._sessions["taskrunner:flat"] = _Session(
        provider=_SelfShapedProvider(5353, owns=False)
    )

    rows = {r["key"]: r for r in mgr.runtime_pids()}

    assert rows["dashboard:nested"]["pid"] == 4242
    assert rows["taskrunner:flat"]["pid"] == 5353, "flat provider shape lost its pid"
    # The flat shape also carries the co-tenancy flag, so it must not be
    # presented as exclusively owning the measurement.
    assert rows["taskrunner:flat"]["owns_runtime"] is False


def test_runtime_pids_reports_unknown_when_there_is_no_runtime() -> None:
    """A non-ACP provider has no runtime at all -- that is unknown, not a crash."""
    from kiro_crew.session import _Session

    mgr = _manager()
    mgr._sessions["dashboard:plain"] = _Session(provider=object())

    rows = {r["key"]: r for r in mgr.runtime_pids()}
    assert rows["dashboard:plain"]["pid"] is None
    # Absent attribute means no shared runtime -- exclusive by construction.
    assert rows["dashboard:plain"]["owns_runtime"] is True


@pytest.mark.asyncio
async def test_background_session_records_the_agent_it_runs_as() -> None:
    """``_bg`` must carry its agent, not read as agent-less.

    The background session runs as a real Kiro agent, but the name was passed
    ONLY to the provider factory -- the ``_Session`` record kept the ``agent=""``
    default. Every consumer reading ``sess.agent`` therefore saw no agent, which
    surfaced as an em-dash in the Session & Task Memory card's Agent column even
    though the agent was perfectly well known one line earlier.
    """
    from unittest.mock import MagicMock

    from kiro_crew.session import BACKGROUND_AGENT, BACKGROUND_KEY, SessionManager

    factory_saw: list[str] = []

    class _StartableProvider(_ClientShapedProvider):
        async def start(self) -> None:
            return None

    def factory(key: str, agent: str = "", **_: object) -> object:
        factory_saw.append(agent)
        return _StartableProvider(9191)

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    mgr = SessionManager(cfg=cfg, provider_factory=factory)
    await mgr._ensure_background()

    row = {r["key"]: r for r in mgr.runtime_pids()}[BACKGROUND_KEY]
    assert row["agent"] == BACKGROUND_AGENT, "background session lost its agent"
    # Both sites must agree: the factory and the record read the same constant,
    # so they cannot drift back apart.
    assert factory_saw == [BACKGROUND_AGENT]


class _FakeRuntime:
    """Stands in for AcpRuntime: only pid / is_alive / _spawn_monotonic are read."""

    def __init__(self, pid: int, *, alive: bool = True, spawn: float | None = None) -> None:
        self._pid = pid
        self._alive = alive
        if spawn is not None:
            self._spawn_monotonic = spawn

    @property
    def pid(self) -> int:
        return self._pid

    def is_alive(self) -> bool:
        return self._alive


def test_runtime_pids_includes_runtimes_held_only_on_the_manager() -> None:
    """``_bg_runtime`` and companion runtimes are real trees, not rounding error.

    Both live ONLY as SessionManager attributes -- so far outside ``_sessions``
    that ``_companion_runtime_pids`` has to shield them from the orphan sweep
    with ``register_protected_pid``. Iterating ``_sessions`` alone omitted a
    whole runtime each from the host total, understating "Kiro Crew Used" by the
    200-400 MB a runtime costs and hiding the very process a user chasing memory
    would want to see.
    """
    import time as _time

    mgr = _manager()
    mgr._bg_runtime = _FakeRuntime(4711, spawn=_time.monotonic() - 90.0)
    mgr._subagent_runtimes = {"dashboard:parent": _FakeRuntime(4712)}

    rows = {r["key"]: r for r in mgr.runtime_pids()}

    assert rows["Background runtime"]["pid"] == 4711
    assert rows["Subagent runtime (dashboard:parent)"]["pid"] == 4712
    # The row IS the runtime -- no session co-tenant claims its pid, so there is
    # nothing to divide it between.
    assert rows["Background runtime"]["owns_runtime"] is True
    # _spawn_monotonic is monotonic; it must be projected onto the wall clock so
    # the consumer's `now - created_at` yields a real age.
    created = rows["Background runtime"]["created_at"]
    assert isinstance(created, float)
    assert 85.0 < (_time.time() - created) < 95.0
    # A runtime with no spawn stamp reads as unknown uptime, never as "just now".
    assert rows["Subagent runtime (dashboard:parent)"]["created_at"] is None


def test_runtime_pids_skips_dead_manager_runtimes() -> None:
    """A dead runtime must not be counted -- it should be reaped, not displayed."""
    mgr = _manager()
    mgr._bg_runtime = _FakeRuntime(4711, alive=False)

    keys = {r["key"] for r in mgr.runtime_pids()}
    assert "Background runtime" not in keys


# ── orphan detection (_all_runtime_pids / _unattributed) ───────────────────


def test_all_runtime_pids_returns_none_on_non_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None means 'cannot ask', not 'none found'."""
    monkeypatch.setattr(sm.sys, "platform", "darwin")
    assert sm._all_runtime_pids() is None


def test_all_runtime_pids_finds_matching_cmdlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm.os, "listdir", lambda path: ["1", "2", "3", "notpid"])
    cmdlines = {
        1: "python -m kirocrew_sandbox --port 8811",
        2: "/usr/bin/kiro-cli chat --agent kirocrew",
        3: "vim session_memory.py",
    }
    monkeypatch.setattr(sm, "_read_cmdline", lambda pid: cmdlines.get(pid, ""))
    assert sm._all_runtime_pids() == {1, 2}


def test_unattributed_returns_none_when_platform_cannot_enumerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """null in the payload, NOT a zero record."""
    monkeypatch.setattr(sm, "_all_runtime_pids", lambda: None)
    result = sm._unattributed(set(), 999)
    assert result is None


def test_unattributed_excludes_owned_and_gateway_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned pids + the gateway's own tree must be subtracted from every runtime."""
    # Pretend pids 10, 20, 30, 40 are runtimes
    monkeypatch.setattr(sm, "_all_runtime_pids", lambda: {10, 20, 30, 40})
    # Owned set from sessions: 10, 20
    owned = {10, 20}
    # Gateway pid 99 with descendants 99, 100, 101 -- 30 is the gateway's child
    monkeypatch.setattr(
        sm, "_iter_descendant_pids", lambda pid: [99, 100, 101, 30] if pid == 99 else []
    )
    monkeypatch.setattr(sm, "_rss_mb_for_pid", lambda pid: 512.0)
    monkeypatch.setattr(sm, "_process_uptime_s", lambda pid: 3600.0)
    result = sm._unattributed(owned, 99)
    assert result is not None
    # Only pid 40 is unattributed (10,20 owned; 30 in gateway tree)
    assert result["procs"] == 1
    assert result["rss_mb"] == 512.0
    assert result["oldest_uptime_s"] == 3600.0


def test_rss_mb_for_pid_returns_none_on_unreadable_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished/restricted pid returns None without raising."""

    def _raise(*_a: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("builtins.open", _raise)
    assert sm._rss_mb_for_pid(99999) is None


def test_unattributed_rss_is_none_when_every_pid_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """procs counts the pids even when RSS is unreadable for all of them."""
    monkeypatch.setattr(sm, "_all_runtime_pids", lambda: {50, 51})
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid: [])
    monkeypatch.setattr(sm, "_rss_mb_for_pid", lambda pid: None)
    monkeypatch.setattr(sm, "_process_uptime_s", lambda pid: None)
    result = sm._unattributed(set(), 1)
    assert result is not None
    assert result["procs"] == 2
    assert result["rss_mb"] is None


def test_oldest_uptime_reports_maximum_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MAXIMUM age across the orphan set, not first or last."""
    monkeypatch.setattr(sm, "_all_runtime_pids", lambda: {60, 61, 62})
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid: [])
    monkeypatch.setattr(sm, "_rss_mb_for_pid", lambda pid: 100.0)
    ages = {60: 100.0, 61: 9999.0, 62: 500.0}
    monkeypatch.setattr(sm, "_process_uptime_s", lambda pid: ages.get(pid))
    result = sm._unattributed(set(), 1)
    assert result is not None
    assert result["oldest_uptime_s"] == 9999.0


# ── credits / turns (slot_spend — unified aggregator in usage.py) ──────────


def test_slot_spend_sums_credits_and_counts_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A slot with shard rows gets summed credits and a turns count."""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    now = datetime.now(timezone.utc)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")
    rows = [
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-1-999", "credits": 5.5},
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-1-999", "credits": 3.2},
        {"_type": "tokens", "ts": now.isoformat(), "slot": "slack:C123", "credits": 10.0},
    ]
    shard.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    result = usage.slot_spend()
    # Bare chat keys are normalized to their full session key form.
    assert result["dashboard:chat-1-999"]["credits"] == pytest.approx(8.7)
    assert result["dashboard:chat-1-999"]["turns"] == 2
    # Non-dashboard keys pass through unchanged.
    assert result["slack:C123"]["credits"] == pytest.approx(10.0)
    assert result["slack:C123"]["turns"] == 1


def test_slot_spend_returns_empty_for_no_shards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A slot with no rows means credits=None and turns=None in the payload."""
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    tmp.mkdir(exist_ok=True)

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    result = usage.slot_spend()
    assert result == {}


def test_slot_spend_drops_nan_and_infinity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """NaN/Infinity in a shard row must not poison the slot total."""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    now = datetime.now(timezone.utc)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")
    rows = [
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-5-111", "credits": float("nan")},
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-5-111", "credits": float("inf")},
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-5-111", "credits": 7.0},
    ]
    shard.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    result = usage.slot_spend()
    # Only the valid row (7.0) survives
    assert result["dashboard:chat-5-111"]["credits"] == pytest.approx(7.0)
    assert result["dashboard:chat-5-111"]["turns"] == 1


def test_slot_spend_cache_expires_as_the_cutoff_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A row aging past the cutoff drops out even when no shard file changes.

    The cache is keyed on shard size + mtime, but the result also depends on
    ``cutoff = now - days*86400``, which moves continuously. On an idle machine
    nothing writes a shard, so without a time component in the cache key a row
    that has aged out keeps being counted -- the number goes silently wrong.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    real_now = datetime.now(timezone.utc)
    # 30s inside the 7-day window, so a small clock advance takes it outside.
    row_ts = real_now - timedelta(days=usage.SPEND_WINDOW_DAYS) + timedelta(seconds=30)
    shard = tmp / real_now.strftime("%Y-%m-%d.jsonl")
    shard.write_text(
        _json.dumps({
            "_type": "tokens",
            "ts": row_ts.isoformat(),
            "slot": "chat-9-777",
            "credits": 4.0,
        }) + "\n",
    )

    # Pin the shard SET so this exercises the cache key alone; otherwise a clock
    # advance across midnight could invalidate it by changing which files match.
    monkeypatch.setattr(usage, "_shards_in_window", lambda days: [shard])
    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_AT", 0.0)

    clock = {"t": real_now.timestamp()}
    monkeypatch.setattr(usage.time, "time", lambda: clock["t"])

    first = usage.slot_spend()
    assert first["dashboard:chat-9-777"]["credits"] == pytest.approx(4.0)

    # Advance past the TTL. The row is now older than the window; the shard file
    # is untouched, so the signature is identical.
    clock["t"] += usage._SLOT_SPEND_TTL_S + 1
    second = usage.slot_spend()
    assert "dashboard:chat-9-777" not in second, (
        "row aged past the cutoff but the cache still served it"
    )


def test_slot_spend_excludes_non_session_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A non-session slot must not appear in the mapping."""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    now = datetime.now(timezone.utc)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")
    rows = [
        {"_type": "tokens", "ts": now.isoformat(), "slot": "internal:pool-0", "credits": 50.0},
        {"_type": "tokens", "ts": now.isoformat(), "slot": "chat-9-222", "credits": 2.0},
    ]
    shard.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: s.startswith("chat-"))
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    result = usage.slot_spend()
    assert "internal:pool-0" not in result
    assert "dashboard:chat-9-222" in result


def test_slot_spend_cache_invalidates_on_shard_growth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """Cache returns the same object when shards unchanged, recomputes on growth."""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    now = datetime.now(timezone.utc)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")
    shard.write_text(
        _json.dumps({"_type": "tokens", "ts": now.isoformat(),
                     "slot": "chat-7-333", "credits": 1.0}) + "\n"
    )

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    first = usage.slot_spend()
    second = usage.slot_spend()
    assert first is second, "cache should return same object on unchanged shards"

    # Append a new row -> shard grows
    with shard.open("a") as fh:
        fh.write(
            _json.dumps({"_type": "tokens", "ts": now.isoformat(),
                         "slot": "chat-7-333", "credits": 2.0}) + "\n"
        )

    third = usage.slot_spend()
    assert third is not first, "cache should invalidate after shard growth"
    assert third["dashboard:chat-7-333"]["credits"] == pytest.approx(3.0)
    assert third["dashboard:chat-7-333"]["turns"] == 2


# ── Regression: Sessions table and Spend tab share the same window ─────────


def test_slot_spend_window_matches_cost_breakdown_window() -> None:
    """The Sessions table and the Spend tab MUST use the same window.

    Regression pin: the old code used _CREDITS_WINDOW_DAYS=14 in session_memory
    while cost_breakdown defaulted to 7. A turn aged 8-14 days was counted by
    Sessions but not Spend, making the two disagree.
    """
    # Both use the same constant; this pinning test catches any drift.
    import inspect

    from kiro_crew.dashboard.handlers import usage

    sig = inspect.signature(usage.cost_breakdown)
    cost_default = sig.parameters["days"].default
    assert cost_default == usage.SPEND_WINDOW_DAYS
    # slot_spend also uses the same default
    sig2 = inspect.signature(usage.slot_spend)
    assert sig2.parameters["days"].default == usage.SPEND_WINDOW_DAYS


def test_slot_spend_applies_per_row_timestamp_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """A row inside the boundary shard but older than the cutoff is NOT counted.

    Regression pin: the old _slot_spend in session_memory filtered by shard file
    date only, not by each row's timestamp. A shard named 2026-08-01 (within
    window) could contain rows timestamped 2026-07-25 (outside the per-row
    cutoff) which were then over-counted.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    now = datetime.now(timezone.utc)
    # Create a shard with today's date (will be picked up by _shards_in_window)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")

    # One recent row (should be counted) and one old row (should NOT be counted)
    recent_ts = now.isoformat()
    old_ts = (now - timedelta(days=usage.SPEND_WINDOW_DAYS + 1)).isoformat()
    rows = [
        {"_type": "tokens", "ts": recent_ts, "slot": "chat-1-999", "credits": 5.0},
        {"_type": "tokens", "ts": old_ts, "slot": "chat-1-999", "credits": 100.0},
    ]
    shard.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())

    result = usage.slot_spend()
    # Only the recent row is counted; the 100-credit old row is excluded.
    assert result["dashboard:chat-1-999"]["credits"] == pytest.approx(5.0)
    assert result["dashboard:chat-1-999"]["turns"] == 1

"""Bounded delayed-RSS fault injection through the real spawn/pump/controller.

Only provider execution, host observations and time are fake. No child process
or large allocation is created; the real durable queue, admission and adaptive
actuator decide which workers may start.
"""

from __future__ import annotations

import asyncio
import json
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import Clock, mock_ctx, mock_sessions

import kiro_crew.subagent as subagent_mod
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.resource_status import POSTURE_AMPLE, AdmissionDecision
from kiro_crew.subagent import SubagentInfo, SubagentManager, _startup_memory_reserve_gb
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator


@pytest.mark.parametrize("platform", ["WINDOWS", "MACOS"])
@pytest.mark.parametrize("free,ok", [(3.0, False), (8.0, True), (-1.0, True)])
def test_startup_memory_guard_uses_native_host_reader(monkeypatch, platform, free, ok):
    for name in ("LINUX", "WINDOWS", "MACOS"):
        monkeypatch.setattr(subagent_mod.platform_compat, "IS_" + name, name == platform)
    reader = (
        "_windows_available_memory_gb" if platform == "WINDOWS" else "_macos_available_memory_gb"
    )
    monkeypatch.setattr(subagent_mod, reader, lambda: free)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (ok, free)


def test_startup_memory_guard_admits_a_host_with_no_native_reader(monkeypatch):
    """Neither Linux, macOS nor Windows means there is no reader for host memory,
    so the guard must ADMIT. Blocking instead would refuse every spawn forever on
    such a host, and back-pressure that cannot measure is not back-pressure."""
    for name in ("LINUX", "WINDOWS", "MACOS"):
        monkeypatch.setattr(subagent_mod.platform_compat, "IS_" + name, False)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (True, -1.0)


def test_startup_memory_guard_respects_container_headroom(monkeypatch):
    import io

    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: io.StringIO("MemAvailable: 33554432 kB"))
    monkeypatch.setattr(subagent_mod, "_cgroup_available_gb", lambda: 3.0)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (False, 3.0)


@pytest.mark.parametrize(
    ("rows", "running", "expected"),
    [
        ([], 0, 0.5),
        ([], 2, 1.5),  # Claimed starts not registered yet plus the next start.
        ([{"last_rss_gb": 0.1}], 1, 0.9),
        ([{"last_rss_gb": 0.5}], 1, 0.5),  # Observed RSS already reduced free memory.
        ([{"last_rss_gb": 0.1, "_slot_released": True}], 0, 0.9),
        ([{"_session_sharing": True, "peak_rss_gb": 4.0}], 1, 0.5),
        ([{"_session_sharing": True}, {"_session_sharing": True}], 2, 0.5),
        ([{"done": True}, {"queued": True}], 0, 0.5),
        ([{"last_rss_gb": 0.6, "peak_rss_gb": 0.8}], 1, 1.0),
    ],
)
def test_startup_reserve_tracks_unobserved_dedicated_memory(rows, running, expected) -> None:
    agents = [SubagentInfo(id=str(i), task="work", **row) for i, row in enumerate(rows)]
    assert _startup_memory_reserve_gb(agents, running_count=running, cost_gb=0.5) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(
    ("rows", "running", "expected"),
    [
        # Nothing live: the next start alone, at the learned price.
        ([], 0, 6.0),
        # Two claims awaiting registration plus the next start, all warming.
        ([], 2, 18.0),
        # A dedicated worker the reaper has not sampled yet is priced like a
        # start: this is the window the fallback under-priced twelve-fold.
        ([{"_pid": 1}], 1, 12.0),
        # ONE sample can land mid-growth: a worker seen once at 1 GB still owes
        # the learned figure less what it holds (6 + 5), not just its own peak.
        ([{"last_rss_gb": 1.0, "peak_rss_gb": 1.0, "_rss_samples": 1}], 1, 11.0),
        # A SETTLED worker (two sweeps) owes only its own gap (configured cost
        # vs observed RSS), never learned-minus-observed: a p90 far above what
        # this worker turned out to need must not become a reserve no sample
        # can retire.
        ([{"last_rss_gb": 1.0, "peak_rss_gb": 1.0, "_rss_samples": 2}], 1, 6.0),
        ([{"last_rss_gb": 0.1, "peak_rss_gb": 0.1, "_rss_samples": 2}], 1, 6.4),
        # An observed peak above the learned figure raises the next start too.
        ([{"last_rss_gb": 7.0, "peak_rss_gb": 7.5, "_rss_samples": 2}], 1, 8.0),
        # Shared sessions launch no process: no dedicated-start price.
        ([{"_session_sharing": True}], 1, 6.0),
        # A settled worker's gap is against ITS OWN peak: the 7.5 GB sibling
        # raises the next start (7.5) but not the 1 GB worker's gap (0).
        (
            [
                {"last_rss_gb": 7.0, "peak_rss_gb": 7.5, "_rss_samples": 2},
                {"last_rss_gb": 1.0, "peak_rss_gb": 1.0, "_rss_samples": 2},
            ],
            2,
            8.0,
        ),
    ],
)
def test_startup_reserve_prices_unmeasured_starts_at_the_learned_cost(
    rows, running, expected
) -> None:
    agents = [SubagentInfo(id=str(i), task="work", **row) for i, row in enumerate(rows)]
    assert _startup_memory_reserve_gb(
        agents, running_count=running, cost_gb=0.5, next_start_gb=6.0
    ) == pytest.approx(expected)


def test_effective_next_start_price_folds_in_live_peaks() -> None:
    from kiro_crew.subagent import _effective_next_start_gb

    idle = _effective_next_start_gb([], cost_gb=0.5, next_start_gb=6.0)
    heavy = SubagentInfo(id="h", task="work", peak_rss_gb=7.5, last_rss_gb=7.0, _rss_samples=2)
    shared = SubagentInfo(id="s", task="work", peak_rss_gb=9.0, _session_sharing=True)
    assert idle == pytest.approx(6.0)
    assert _effective_next_start_gb([heavy], cost_gb=0.5, next_start_gb=6.0) == pytest.approx(7.5)
    assert _effective_next_start_gb([shared], cost_gb=0.5, next_start_gb=6.0) == pytest.approx(6.0)
    assert _effective_next_start_gb([], cost_gb=0.5, next_start_gb=None) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("configured", "learned", "expected"),
    [(0.5, 6.0, 6.0), (0.5, None, 0.5), (2.0, 1.0, 2.0), (0.5, "bad", 0.5)],
)
def test_startup_cost_is_the_larger_of_configured_and_learned(configured, learned, expected):
    agent = SimpleNamespace(subagent_cost_gb=configured)
    assert subagent_mod._startup_cost_gb(agent, learned) == pytest.approx(expected)


def test_reaper_sweep_publishes_the_learned_cost_off_loop(monkeypatch, tmp_path) -> None:
    """The gate reads ``_learned_costs_gb``; the sweep is what fills it.

    Redirect the cost log, seed three samples, run the (off-loop) sweep body
    and check the manager now carries the p90. An unreadable log keeps the
    previous value instead of blanking it.
    """
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("kirocrew", 6.0, 0.1)
        sc.append_cost_sample("light", 1.0, 0.1)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        assert mgr._learned_costs_gb == {}
        mgr._sample_live_costs()
        expected = {
            "kirocrew": pytest.approx(6.0),
            "light": pytest.approx(1.0),
        }
        assert mgr._learned_costs_gb == expected
        # A read that raises keeps the figures.
        monkeypatch.setattr(
            subagent_mod, "read_learned_costs_checked", MagicMock(side_effect=OSError)
        )
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == expected
        # An INCOMPLETE read that reached nothing (an over-cap record ended the
        # parse at once) keeps them too.
        monkeypatch.setattr(subagent_mod, "read_learned_costs_checked", lambda *a, **k: ({}, False))
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == expected
        # An incomplete read that reached one bucket keeps the unreached one and
        # takes the new figure for the one it did yield -- up or down.
        monkeypatch.setattr(
            subagent_mod, "read_learned_costs_checked", lambda *a, **k: ({"light": 0.7}, False)
        )
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {
            "kirocrew": pytest.approx(6.0),
            "light": pytest.approx(0.7),
        }
        # A COMPLETE read is authoritative: a bucket it does not yield has
        # expired or fallen below min_samples, and its held price retires.
        monkeypatch.setattr(
            subagent_mod, "read_learned_costs_checked", lambda *a, **k: ({"light": 0.7}, True)
        )
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {"light": pytest.approx(0.7)}
        # The operator's reset -- the log is gone -- is honoured.
        log.unlink()
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {}
    finally:
        mgr._taskq.close()


def test_cost_samples_are_written_under_the_bucket_the_gate_reads(monkeypatch, tmp_path) -> None:
    """An agent-less run inheriting a template builds THAT template's bucket."""
    from kiro_crew import subagent_cost as sc
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        heavy = ExecutionContext(
            member_id=None,
            store=MemoryStoreRef(store_id="default"),
            selection_kind="template",
            template_id="heavy",
        )
        inherited = SubagentInfo(
            id="a", task="w", agent="", peak_rss_gb=6.0, execution_context=heavy
        )
        named = SubagentInfo(id="b", task="w", agent="light", peak_rss_gb=1.0)
        bare = SubagentInfo(id="c", task="w", agent="", peak_rss_gb=0.4)
        shared = SubagentInfo(
            id="d", task="w", agent="light", peak_rss_gb=0.2, _session_sharing=True
        )
        for info in (inherited, named, bare, shared):
            mgr._record_cost(info)
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [r["agent"] for r in rows] == ["heavy", "light", "kirocrew", "light"]
        # A shared run's sample is marked; a dedicated run's record shape is unchanged.
        assert [r.get("shared") for r in rows] == [None, None, None, True]
        assert subagent_mod._cost_bucket("", heavy) == "heavy"
        assert subagent_mod._cost_bucket("named", heavy) == "named"
        assert subagent_mod._cost_bucket("", None) == ""
    finally:
        mgr._taskq.close()


def test_dedicated_pricing_leaves_shared_session_shares_out(monkeypatch, tmp_path) -> None:
    """A bucket of per-session shares must not price a start that runs as a process."""
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("kirocrew", 0.3, 0.1, shared=True)  # divided shares
        sc.append_cost_sample("heavy", 6.0, 0.1)  # dedicated processes
    sc.append_cost_sample("kirocrew", 5.5, 0.1)  # one dedicated run of the default agent
    assert sc.read_learned_costs("mem_gb") == {
        "kirocrew": pytest.approx(3.94, abs=0.01),
        "heavy": pytest.approx(6.0),
    }
    # Dedicated-only: the default bucket has one qualifying sample, below
    # min_samples, so it drops out and a spawn under it is priced from the
    # configured cost (plus live peaks) instead of a share-diluted p90.
    dedicated = sc.read_learned_costs("mem_gb", dedicated_only=True)
    assert dedicated == {"heavy": pytest.approx(6.0)}
    assert sc.learned_cost_for(dedicated, "kirocrew") is None
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {"heavy": pytest.approx(6.0)}
    finally:
        mgr._taskq.close()


def test_learned_cost_for_prices_a_spawn_by_its_own_agent() -> None:
    from kiro_crew.subagent_cost import learned_cost_for

    costs = {"kirocrew": 6.0, "light": 1.0}
    assert learned_cost_for(costs, "light") == pytest.approx(1.0)
    assert learned_cost_for(costs, "kirocrew") == pytest.approx(6.0)
    assert learned_cost_for(costs, "") == pytest.approx(6.0)  # unnamed = default agent
    # No dedicated history of its own: NOT the heaviest known -- on a
    # sharing-default backend a share-eligible agent never forms a dedicated
    # bucket, and pricing it at an unrelated heavy figure would defer its every
    # spawn for good. The caller prices from the configured cost + live peaks.
    assert learned_cost_for(costs, "brand-new") is None
    assert learned_cost_for({}, "light") is None


def test_cost_log_identity_tells_absent_from_uninspectable(monkeypatch, tmp_path) -> None:
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "subagents" / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    assert sc.cost_log_identity() is None  # genuinely absent: the reset
    log.parent.mkdir()
    log.write_bytes(b"x\n")  # bytes: text mode would write \r\n on Windows
    ident = sc.cost_log_identity()
    assert ident is not None and len(ident) == 3 and ident[2] == 2
    monkeypatch.setattr(sc.os, "stat", MagicMock(side_effect=PermissionError))
    assert sc.cost_log_identity() == ("unknown",)  # os.path.exists would have said False


def test_a_reset_recreated_within_one_sweep_is_read_fresh(monkeypatch, tmp_path) -> None:
    """The operator deletes the log; the next run re-creates it before the sweep.

    ``append_cost_sample`` re-creates the path at once, so a sweep may never see
    the log absent. A new inode (or a shrunk size) is the evidence that what
    the held map was learned from is gone, so it is read fresh, not merged.
    """
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("heavy", 6.0, 0.1)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {"heavy": pytest.approx(6.0)}
        log.unlink()
        sc.append_cost_sample("light", 1.0, 0.1)  # one record: below min_samples
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {}, "a replaced log must not carry the old p90 over"
        for _ in range(2):
            sc.append_cost_sample("light", 1.0, 0.1)
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {"light": pytest.approx(1.0)}
    finally:
        mgr._taskq.close()


def test_compaction_keeps_dedicated_history_under_a_flood_of_shared_runs(
    monkeypatch, tmp_path
) -> None:
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("kirocrew", 6.0, 0.1)
    for _ in range(60):
        sc.append_cost_sample("kirocrew", 0.2, 0.1, shared=True)
    sc.compact_cost_log(window=50)
    assert sc.read_learned_costs("mem_gb", dedicated_only=True) == {"kirocrew": pytest.approx(6.0)}
    rows = log.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 53  # 3 dedicated kept, shared trimmed to its own window


def test_expired_samples_do_not_price_a_start(monkeypatch, tmp_path) -> None:
    """A p90 learned under a workload that is gone expires on its own."""
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    old = int(time.time()) - sc._SAMPLE_MAX_AGE_SECS - 60
    _seed = [{"agent": "kirocrew", "mem_gb": 6.0, "cpu_cores": 0.1, "ts": old} for _ in range(3)]
    log.write_text("".join(json.dumps(r) + "\n" for r in _seed), encoding="utf-8")
    horizon = sc._SAMPLE_MAX_AGE_SECS
    assert sc.read_learned_costs("mem_gb", max_age_secs=horizon) == {}
    # The cap's reader applies no horizon: its result is what it always was.
    assert sc.read_learned_cost("mem_gb") == pytest.approx(6.0)
    # A record without a timestamp (a legacy line) still counts.
    log.write_text(
        "".join(
            json.dumps({"agent": "kirocrew", "mem_gb": 6.0, "cpu_cores": 0.1}) + "\n"
            for _ in range(3)
        ),
        encoding="utf-8",
    )
    assert sc.read_learned_costs("mem_gb", max_age_secs=horizon) == {"kirocrew": pytest.approx(6.0)}
    # Fresh samples count; a host idle for less than the horizon keeps its figure.
    log.unlink()
    for _ in range(3):
        sc.append_cost_sample("kirocrew", 6.0, 0.1)
    assert sc.read_learned_costs("mem_gb", max_age_secs=horizon) == {"kirocrew": pytest.approx(6.0)}


def test_an_expired_bucket_retires_on_a_long_lived_gateway(monkeypatch, tmp_path) -> None:
    """The age horizon must take effect without a restart or a log reset."""
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("heavy", 6.0, 0.1)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {"heavy": pytest.approx(6.0)}
        # Same file, same inode, only grown: a fresh light run lands while the
        # heavy samples cross the horizon.
        real_time = time.time
        monkeypatch.setattr(sc.time, "time", lambda: real_time() + sc._SAMPLE_MAX_AGE_SECS + 120)
        sc.append_cost_sample("light", 1.0, 0.1)
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {}, "a complete read that omits the bucket retires it"
    finally:
        mgr._taskq.close()


def test_the_held_map_is_bounded_against_an_agent_writable_log(monkeypatch, tmp_path) -> None:
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for i in range(sc._MAX_BUCKETS + 10):
        for _ in range(3):
            sc.append_cost_sample(f"agent-{i:03d}", 0.1 + i * 0.01, 0.1)
    for _ in range(3):
        sc.append_cost_sample("x" * (sc._BUCKET_KEY_CAP + 1), 9.0, 0.1)  # not an agent name
    costs = sc.read_learned_costs("mem_gb")
    # The over-long key is never a bucket; of the rest, the HEAVIEST
    # _MAX_BUCKETS are returned (the parse ceiling is far above this count).
    assert len(costs) == sc._MAX_BUCKETS
    assert all(len(k) <= sc._BUCKET_KEY_CAP for k in costs)
    assert sorted(costs) == [f"agent-{i:03d}" for i in range(10, sc._MAX_BUCKETS + 10)]
    merged = sc.cap_buckets({**costs, "extra": 50.0})
    assert len(merged) == sc._MAX_BUCKETS and "extra" in merged


def test_a_log_replaced_during_the_read_keeps_the_prior_map(monkeypatch, tmp_path) -> None:
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("heavy", 6.0, 0.1)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {"heavy": pytest.approx(6.0)}
        real_read = subagent_mod.read_learned_costs_checked

        def read_then_replace(*a, **k):
            out = real_read(*a, **k)
            log.unlink()
            sc.append_cost_sample("light", 1.0, 0.1)
            return out

        monkeypatch.setattr(subagent_mod, "read_learned_costs_checked", read_then_replace)
        mgr._sample_live_costs()
        # Pre-reset figures were NOT paired with the replacement file.
        assert mgr._learned_costs_gb == {"heavy": pytest.approx(6.0)}
        monkeypatch.setattr(subagent_mod, "read_learned_costs_checked", real_read)
        mgr._sample_live_costs()
        assert mgr._learned_costs_gb == {}  # the replaced log is read fresh
    finally:
        mgr._taskq.close()


def test_a_sweep_that_straddles_a_respawn_does_not_settle_the_new_process(monkeypatch) -> None:
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        info = SubagentInfo(id="r", task="w", _pid=4242)
        mgr._agents["r"] = info

        def read_then_respawn(_pid):
            # The respawn lands while the off-loop /proc read is in flight.
            info._rss_samples = 0
            info.last_rss_gb = 0.0
            info._rss_generation += 1
            return subagent_mod.platform_compat.SubtreeSample(6 * 1024 * 1024, 0, 3, 0)

        monkeypatch.setattr(subagent_mod, "_proc_subtree_sample", read_then_respawn)
        mgr._sample_live_costs()
        assert info._rss_samples == 0 and info.last_rss_gb == 0.0
        monkeypatch.setattr(
            subagent_mod,
            "_proc_subtree_sample",
            lambda _pid: subagent_mod.platform_compat.SubtreeSample(6 * 1024 * 1024, 0, 3, 0),
        )
        mgr._sample_live_costs()
        assert info._rss_samples == 1 and info.last_rss_gb == pytest.approx(6.0)
    finally:
        mgr._taskq.close()


@pytest.mark.parametrize(
    ("cost_gb", "running", "expected"),
    [(-8.0, 0, 0.0), (-8.0, 2, 0.0), (0.0, 0, 0.0), (0.0, 2, 0.0), (0.5, 0, 0.5), (0.5, 2, 1.5)],
)
def test_startup_reserve_cannot_discount_claims(cost_gb, running, expected):
    assert _startup_memory_reserve_gb([], running_count=running, cost_gb=cost_gb) == expected


@pytest.mark.parametrize("cost_gb", [-8.0, 0.0, 0.5])
def test_measured_peak_still_binds_with_nonpositive_configured_cost(cost_gb):
    info = SubagentInfo(id="live", task="work", peak_rss_gb=0.8, last_rss_gb=0.6)
    assert _startup_memory_reserve_gb([info], running_count=1, cost_gb=cost_gb) == pytest.approx(
        1.0
    )


@pytest.mark.parametrize("cost_gb", [-8.0, 0.0, 0.5])
@pytest.mark.parametrize("floor_gb", [0.0, 4.0])
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_startup_cost_cannot_lower_enabled_floor_on_exhausted_cgroup(
    monkeypatch, cost_gb, floor_gb
):
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = cost_gb
    cfg.agent.spawn_min_memory_gb = floor_gb
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(
        subagent_mod,
        "open",
        lambda *a, **kw: StringIO("MemAvailable: 33554432 kB\n"),
        raising=False,
    )
    monkeypatch.setattr(subagent_mod, "_cgroup_available_gb", lambda: 0.0)
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=32.0),
    )
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    mgr._spawn_stagger_secs = 0.0
    worker = AsyncMock()
    monkeypatch.setattr(mgr, "_run", worker)
    try:
        info = await mgr.spawn_async("work", parent_session_key="dash:memory-floor")
        assert info is not None
        assert info.queued is (floor_gb > 0)
        if floor_gb > 0:
            assert info.id not in mgr._tasks
            worker.assert_not_called()
        else:
            await asyncio.wait_for(mgr._tasks[info.id], 5)
            worker.assert_awaited_once()
    finally:
        mgr._shutting_down = True
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()


@pytest.mark.parametrize("shock_gb", [0.0, 16.0])
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_delayed_dedicated_rss_does_not_spend_the_startup_reserve(
    monkeypatch, shock_gb
) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.max_subagents = 64
    cfg.agent.subagent_spawn_stagger_secs = 0.25
    cfg.agent.subagent_cost_gb = 0.5
    cfg.session.pool_size = 0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    clock = Clock()
    epoch = clock()
    monkeypatch.setattr(subagent_mod, "time", SimpleNamespace(monotonic=clock, time=time.time))
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=64)
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    mgr._spawn_stagger_secs = cfg.agent.subagent_spawn_stagger_secs
    starts: dict[str, float] = {}
    finishes: dict[str, asyncio.Future] = {}
    launch_times: list[float] = []
    external_gb = 0.0
    free_samples: list[float] = []
    refused_at: list[float] = []
    decisions: list[str] = []
    timer_handles = []
    loop = asyncio.get_running_loop()
    real_call_later = loop.call_later

    def call_later(delay, callback, *args, **kwargs):
        # Drive only the pump's timers with virtual time; asyncio's own
        # wait_for deadlines retain the real clock and remain bounded.
        if callback == mgr._drain_queue:
            handle = real_call_later(3600, callback, *args, **kwargs)
            timer_handles.append(handle)
            return handle
        return real_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", call_later)

    def available() -> float:
        resident = sum(
            0.5 if clock() - started >= 5.0 else 0.05
            for agent_id, started in starts.items()
            if not mgr._agents[agent_id].done
        )
        return 24.0 - external_gb - resident

    def memory_check(*, min_gb, **_kw):
        free = available()
        if free < min_gb:
            refused_at.append(clock())
        return free >= min_gb, free

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    # Keep the posture cache ample to exercise the absolute spawn guard even
    # when the slower cached posture observation has not noticed the shock.
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=24.0),
    )

    async def worker(info: SubagentInfo) -> None:
        starts[info.id] = clock()
        launch_times.append(clock())
        info._pid = 1000 + len(starts)
        info._exec_started = time.time()
        info._session_sharing = False
        done = finishes[info.id] = loop.create_future()
        await done
        info.done = True
        info.result = "ok"
        mgr._claim_finalize(info)
        if mgr._release_slot(info):
            mgr._running_count -= 1
            mgr._drain_queue()

    monkeypatch.setattr(mgr, "_run", worker)
    ctl = AdaptiveController(
        mgr,
        cfg=cfg,
        clock=clock,
        host_probe=lambda: HostSample(free_mem_mb=available() * 1024),
    )

    async def pump() -> None:
        mgr._drain_queue()
        task = getattr(mgr, "_drain_task", None)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 5)
        # Registration schedules the worker; a loop barrier lets it expose
        # its start before the next virtual host observation.
        await asyncio.sleep(0)

    try:
        await ctl.tick()
        for i in range(64):
            await mgr.spawn_async(
                f"work-{i}", parent_session_key="dash:memory-wave", batch_id="wave", batch_total=64
            )
        await pump()
        for step in range(1, 101):
            clock.advance(0.25)
            for agent_id, started in starts.items():
                mgr._agents[agent_id].last_rss_gb = 0.5 if clock() - started >= 5.0 else 0.05
            if step in (20, 40):
                # One real completion earns each slow-start increase; the
                # rest of the dedicated workers remain resident.
                oldest = next(agent_id for agent_id in starts if not mgr._agents[agent_id].done)
                finishes[oldest].set_result(None)
                await asyncio.wait_for(asyncio.shield(mgr._tasks[oldest]), 5)
            if step % 20 == 0:
                decisions.append((await ctl.tick()).action)
            if step == 40:
                # Another application takes memory just AFTER the controller
                # sampled. New workers would grow five seconds after passing
                # a raw free-memory check, inside its next sampling window.
                external_gb = shock_gb
            await pump()
            free_samples.append(available())

        assert "increase" in decisions
        assert min(free_samples) >= cfg.agent.spawn_min_memory_gb, (
            min(free_samples),
            len(starts),
            decisions,
        )
        if shock_gb:
            assert refused_at, "the real admission guard must stop the drain"
        else:
            assert not refused_at
            assert len(starts) == 18, "ample hosts must fill the earned 16 slots quickly"
        assert len(starts) < 64
        assert mgr._queue or mgr._taskq.count(state="queued")
        assert all(b - a >= 0.25 for a, b in zip(launch_times, launch_times[1:]))
        assert clock() - epoch == 25.0
    finally:
        mgr._shutting_down = True
        for handle in timer_handles:
            handle.cancel()
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        drain = getattr(mgr, "_drain_task", None)
        if drain is not None and not drain.done():
            tasks.append(drain)
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()

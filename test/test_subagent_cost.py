"""Tests for the append-only learned-cost store (subagent_cost).

Covers append/round-trip, typical-cost aggregation with outlier robustness,
median-across-agents, min-sample fallback, empty/corrupt fail-open, concurrent
appends, and FIFO compaction bound.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import kiro_crew.subagent as subagent
import kiro_crew.subagent_cost as sc
from kiro_crew.config.sections import AgentConfig


@pytest.fixture
def cost_log(tmp_path, monkeypatch):
    """Redirect the cost log to a temp file."""
    p = tmp_path / "subagents" / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: p)
    return p


def _seed(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


# --- append ----------------------------------------------------------------


def test_append_writes_jsonl_line(cost_log):
    sc.append_cost_sample("kirocrew", 0.34, 0.82)
    lines = cost_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent"] == "kirocrew"
    assert rec["mem_gb"] == 0.34
    assert rec["cpu_cores"] == 0.82
    assert "ts" in rec


def test_append_normalizes_empty_agent(cost_log):
    sc.append_cost_sample("", 0.3, 0.5)
    rec = json.loads(cost_log.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "kirocrew"


def test_append_skips_zero_zero(cost_log):
    sc.append_cost_sample("kirocrew", 0.0, 0.0)
    assert not cost_log.exists() or cost_log.read_text(encoding="utf-8").strip() == ""


def test_concurrent_appends_do_not_lose_samples(cost_log):
    for i in range(20):
        sc.append_cost_sample("kirocrew", 0.3 + i * 0.001, 0.5)
    lines = cost_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20  # O_APPEND keeps every line


# --- read_cap_costs --------------------------------------------------------


def test_typical_cost_ignores_single_outlier(cost_log):
    # 20 samples ~0.3 plus one pathological 9.9 → the median stays at 0.3,
    # not dominated by the lone outlier at the top.
    recs = [{"agent": "kirocrew", "mem_gb": 0.30, "cpu_cores": 0.1} for _ in range(20)]
    recs.append({"agent": "kirocrew", "mem_gb": 9.9, "cpu_cores": 0.1})
    _seed(cost_log, recs)
    val = sc.read_cap_costs("mem_gb")[0]
    assert val is not None
    assert val < 1.0  # outlier did not dominate


def test_median_across_agents(cost_log):
    recs = (
        [{"agent": "kirocrew-lite", "mem_gb": 0.30, "cpu_cores": 0.1} for _ in range(5)]
        + [{"agent": "kirocrew", "mem_gb": 0.40, "cpu_cores": 0.1} for _ in range(5)]
        + [{"agent": "builder", "mem_gb": 9.0, "cpu_cores": 0.1} for _ in range(5)]
    )
    _seed(cost_log, recs)
    val = sc.read_cap_costs("mem_gb")[0]
    assert val == pytest.approx(0.40, abs=0.01)  # the heaviest type does not win


def _heavy_agent_window(heavy: int) -> list[dict]:
    """One agent with *heavy* 22 GB build runs in 50, three agents at ~2 GB."""
    recs = [{"agent": "builder", "mem_gb": 22.0, "cpu_cores": 1.0} for _ in range(heavy)]
    recs += [{"agent": "builder", "mem_gb": 2.0, "cpu_cores": 1.0} for _ in range(50 - heavy)]
    for name, gb in (("kirocrew", 1.9), ("kirocrew-lite", 2.0), ("reviewer", 2.1)):
        recs += [{"agent": name, "mem_gb": gb, "cpu_cores": 0.5} for _ in range(10)]
    return recs


@pytest.mark.parametrize(
    ("heavy", "heavy_peak", "mem_term"), [(5, 4.0, 64), (6, 22.0, 55), (7, 22.0, 55)]
)
def test_auto_cap_prices_slots_at_a_typical_run(
    cost_log, monkeypatch, heavy, heavy_peak, mem_term
):
    # 171.8 GB available, 20% buffer, pool of 2, ~2 GB a slot, the heavy agent's
    # p90 reserved once: floor((171.8 * 0.8 - 2 * 2 - heavy_peak) / 2). Pricing
    # every slot at that p90 instead would swing the term between 32 and 4.
    _seed(cost_log, _heavy_agent_window(heavy))
    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: 171.8)
    cfg = types.SimpleNamespace(
        agent=types.SimpleNamespace(
            max_subagents=0,
            subagent_mem_buffer_pct=20,
            subagent_cost_gb=0.315,
            subagent_auto_max=AgentConfig().subagent_auto_max,
            # The memory terms size the subagent cap only with the spawn floor
            # off; with it on the cap is the ceiling whatever the terms say.
            spawn_min_memory_gb=0,
        ),
        session=types.SimpleNamespace(pool_size=2),
    )
    typical, peak = sc.read_cap_costs("mem_gb")
    assert typical == pytest.approx(2.0, abs=0.01)
    assert peak == pytest.approx(heavy_peak, abs=0.01)
    assert subagent._host_mem_term(cfg) == mem_term
    # What users get: min(mem_term, 32), so the default ``subagent_auto_max``
    # clamp binds in every case.
    assert subagent.compute_memory_sized_parallel_cap(cfg) == min(mem_term, 32) == 32
    assert subagent.compute_max_subagents(cfg) == AgentConfig().subagent_auto_max == 32


def test_typical_cost_counts_every_agent_past_the_bucket_cap(cost_log):
    # More qualifying agents than the held-bucket cap: the light majority still
    # sets the median, it is not dropped in favour of the heaviest buckets.
    recs = []
    for i in range(sc._MAX_BUCKETS):
        recs += [{"agent": f"light-{i}", "mem_gb": 2.0, "cpu_cores": 0.1} for _ in range(3)]
    for i in range(sc._MAX_BUCKETS // 2):
        recs += [{"agent": f"heavy-{i}", "mem_gb": 22.0, "cpu_cores": 0.1} for _ in range(3)]
    _seed(cost_log, recs)
    assert sc.read_cap_costs("mem_gb") == (pytest.approx(2.0), pytest.approx(22.0))


def test_min_samples_fallback_returns_none(cost_log):
    _seed(cost_log, [{"agent": "kirocrew", "mem_gb": 0.5, "cpu_cores": 0.1}])  # only 1
    assert sc.read_cap_costs("mem_gb", min_samples=3) == (None, None)


def test_empty_log_returns_none(cost_log):
    assert sc.read_cap_costs("mem_gb") == (None, None)


def test_corrupt_lines_skipped(cost_log):
    cost_log.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"agent": "kirocrew", "mem_gb": 0.4, "cpu_cores": 0.1})
    cost_log.write_text(f"{good}\nNOT JSON\n{good}\n{good}\n", encoding="utf-8")
    val = sc.read_cap_costs("mem_gb", min_samples=3)[0]
    assert val == pytest.approx(0.4, abs=0.01)  # 3 good lines, corrupt skipped


def test_window_limits_to_recent(cost_log):
    # Old cheap samples then recent expensive ones; window=3 → only recent count.
    recs = [{"agent": "kirocrew", "mem_gb": 0.1, "cpu_cores": 0.1} for _ in range(10)]
    recs += [{"agent": "kirocrew", "mem_gb": 0.9, "cpu_cores": 0.1} for _ in range(3)]
    _seed(cost_log, recs)
    val = sc.read_cap_costs("mem_gb", window=3, min_samples=3)[0]
    assert val == pytest.approx(0.9, abs=0.01)


# --- compaction ------------------------------------------------------------


def test_compaction_bounds_per_agent(cost_log):
    recs = [{"agent": "kirocrew", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": i} for i in range(100)]
    _seed(cost_log, recs)
    sc.compact_cost_log(window=10)
    lines = cost_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 10  # trimmed to last 10
    # kept the most recent (highest ts)
    kept_ts = sorted(json.loads(line)["ts"] for line in lines)
    assert kept_ts == list(range(90, 100))


def test_compaction_noop_when_within_bound(cost_log):
    recs = [{"agent": "kirocrew", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": i} for i in range(5)]
    _seed(cost_log, recs)
    sc.compact_cost_log(window=50)
    assert len(cost_log.read_text(encoding="utf-8").strip().splitlines()) == 5


def test_compaction_per_agent_independent(cost_log):
    recs = (
        [{"agent": "a", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": i} for i in range(20)]
        + [{"agent": "b", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": 100 + i} for i in range(20)]
    )
    _seed(cost_log, recs)
    sc.compact_cost_log(window=5)
    lines = [json.loads(line) for line in cost_log.read_text(encoding="utf-8").strip().splitlines()]
    assert sum(1 for r in lines if r["agent"] == "a") == 5
    assert sum(1 for r in lines if r["agent"] == "b") == 5


class TestOverCapRecordDoesNotLoseData:
    """Compaction REPLACES the log with what it parsed.

    So this reader cannot skip an over-cap record the way a read-only consumer
    can -- a skipped record would be permanently deleted by the next
    compaction. This site is not skip-safe: ``compact_cost_log`` rewrites the
    log, not just the percentile consumer that reads it.
    """

    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        # raising=False so this file also RUNS against a pre-fix source, where
        # the attribute does not exist: the test then fails on behaviour (the
        # record compaction deleted) rather than erroring on a missing name.
        monkeypatch.setattr(sc, "_RECORD_CAP", 200, raising=False)

    def test_compaction_refuses_to_run_on_an_incomplete_read(self, cost_log):
        """Red on base: base skips the over-cap record and rewrites without it."""
        over_cap = json.dumps({"agent": "a", "mem_gb": 1.0, "cpu_cores": 1.0, "pad": "z" * 300})
        assert len(over_cap.encode()) > 200
        cost_log.parent.mkdir(parents=True, exist_ok=True)
        cost_log.write_text(
            over_cap
            + "\n"
            + "".join(
                json.dumps({"agent": "a", "mem_gb": 1.0, "cpu_cores": 1.0, "ts": i}) + "\n"
                for i in range(60)
            ),
            encoding="utf-8",
        )
        before = cost_log.read_bytes()
        sc.compact_cost_log(window=10)
        assert cost_log.read_bytes() == before, (
            "compaction rewrote the log from a partial read, permanently deleting "
            "the record the reader refused"
        )

    def test_compaction_refuses_to_run_on_an_undecodable_record(self, cost_log):
        """Red on base: base raises UnicodeDecodeError out of the reader.

        Decoding with replacement would be worse than the crash, because
        compaction PERSISTS what it read -- ``os.replace`` would substitute
        U+FFFD for the original bytes. The strict reader refuses instead, so
        compaction declines and the log keeps its bytes.
        """
        cost_log.parent.mkdir(parents=True, exist_ok=True)
        cost_log.write_bytes(
            b'{"agent":"a","mem_gb":1.0,"cpu_cores":1.0,"note":"\xff"}\n'
            + b"".join(
                (json.dumps({"agent": "a", "mem_gb": 1.0, "cpu_cores": 1.0, "ts": i}) + "\n").encode()
                for i in range(60)
            )
        )
        before = cost_log.read_bytes()
        sc.compact_cost_log(window=10)
        assert cost_log.read_bytes() == before, (
            "compaction rewrote the log after a lossy decode, replacing the "
            "original bytes with U+FFFD"
        )

    def test_compaction_still_trims_a_readable_log(self, cost_log):
        """The refusal is specific to an incomplete read, not to every compaction."""
        _seed(
            cost_log,
            [{"agent": "a", "mem_gb": 1.0, "cpu_cores": 1.0, "ts": i} for i in range(60)],
        )
        sc.compact_cost_log(window=10)
        kept = [json.loads(x) for x in cost_log.read_text(encoding="utf-8").splitlines() if x]
        assert len(kept) == 10

    def test_percentile_read_still_degrades(self, cost_log):
        """The read-only consumer keeps using what it could read, and never raises."""
        over_cap = json.dumps({"agent": "a", "mem_gb": 9.0, "cpu_cores": 1.0, "pad": "z" * 300})
        cost_log.parent.mkdir(parents=True, exist_ok=True)
        cost_log.write_text(
            "".join(
                json.dumps({"agent": "a", "mem_gb": 1.0, "cpu_cores": 1.0, "ts": i}) + "\n"
                for i in range(5)
            )
            + over_cap
            + "\n",
            encoding="utf-8",
        )
        rows, complete = sc._read_samples_checked()
        assert len(rows) == 5, "records before the over-cap one must survive"
        assert complete is False, "the caller that writes must be able to see the truncation"


# --- settled runtime readings (the dedicated start projection) --------------


def test_append_writes_settled_only_when_measured(cost_log):
    sc.append_cost_sample("kirocrew", 1.4, 0.5, settled_gb=0.62)
    sc.append_cost_sample("kirocrew", 1.4, 0.5)
    first, second = (json.loads(x) for x in cost_log.read_text(encoding="utf-8").splitlines())
    assert first["settled_gb"] == 0.62
    assert first["mem_gb"] == 1.4, "the whole-run peak the cap reads is unchanged"
    assert "settled_gb" not in second


def test_a_settled_reading_alone_is_still_recorded(cost_log):
    sc.append_cost_sample("kirocrew", 0.0, 0.0, settled_gb=0.5)
    assert json.loads(cost_log.read_text(encoding="utf-8"))["settled_gb"] == 0.5


def test_settled_reader_is_dedicated_only_per_bucket_p90(cost_log):
    _seed(
        cost_log,
        [{"agent": "kirocrew", "mem_gb": 9.0, "settled_gb": v} for v in (0.5, 0.6, 0.7)]
        + [{"agent": "kirocrew", "mem_gb": 9.0, "settled_gb": 50.0, "shared": True}] * 5
        + [{"agent": "heavy", "mem_gb": 9.0, "settled_gb": 1.5}] * 2
        + [{"agent": "kirocrew", "mem_gb": 9.0}] * 5,
    )
    costs, complete = sc.read_learned_costs_checked("settled_gb", dedicated_only=True)
    assert complete is True
    # Shared shares and records without a settled reading teach nothing; a
    # bucket with fewer than three readings is not trusted yet.
    assert costs == {"kirocrew": pytest.approx(0.68)}


def test_settled_lookup_reads_one_bucket_and_normalizes_the_default():
    costs = {"kirocrew": 0.6, "heavy": 1.5}
    assert sc.learned_settled_for(costs, "") == 0.6
    assert sc.learned_settled_for(costs, "heavy") == 1.5
    assert sc.learned_settled_for(costs, "other") is None
    assert sc.learned_settled_for({}, "heavy") is None
    assert sc.learned_settled_for(None, "heavy") is None

"""Tests for the append-only learned-cost store (subagent_cost).

Covers append/round-trip, p90 aggregation with tail-outlier robustness,
max-across-agents, min-sample fallback, empty/corrupt fail-open, concurrent
appends, and FIFO compaction bound.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.subagent_cost as sc


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


# --- read_learned_cost -----------------------------------------------------


def test_p90_ignores_single_outlier(cost_log):
    # 20 samples ~0.3 plus one pathological 9.9 → p90 (rank 18 of 0..20) stays
    # at 0.3, not dominated by the lone outlier at the top.
    recs = [{"agent": "kirocrew", "mem_gb": 0.30, "cpu_cores": 0.1} for _ in range(20)]
    recs.append({"agent": "kirocrew", "mem_gb": 9.9, "cpu_cores": 0.1})
    _seed(cost_log, recs)
    val = sc.read_learned_cost("mem_gb")
    assert val is not None
    assert val < 1.0  # outlier did not dominate


def test_max_across_agents(cost_log):
    recs = (
        [{"agent": "kirocrew-lite", "mem_gb": 0.30, "cpu_cores": 0.1} for _ in range(5)]
        + [{"agent": "kirocrew", "mem_gb": 0.55, "cpu_cores": 0.1} for _ in range(5)]
    )
    _seed(cost_log, recs)
    val = sc.read_learned_cost("mem_gb")
    assert val == pytest.approx(0.55, abs=0.01)  # heaviest type wins


def test_min_samples_fallback_returns_none(cost_log):
    _seed(cost_log, [{"agent": "kirocrew", "mem_gb": 0.5, "cpu_cores": 0.1}])  # only 1
    assert sc.read_learned_cost("mem_gb", min_samples=3) is None


def test_empty_log_returns_none(cost_log):
    assert sc.read_learned_cost("mem_gb") is None


def test_corrupt_lines_skipped(cost_log):
    cost_log.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"agent": "kirocrew", "mem_gb": 0.4, "cpu_cores": 0.1})
    cost_log.write_text(f"{good}\nNOT JSON\n{good}\n{good}\n", encoding="utf-8")
    val = sc.read_learned_cost("mem_gb", min_samples=3)
    assert val == pytest.approx(0.4, abs=0.01)  # 3 good lines, corrupt skipped


def test_window_limits_to_recent(cost_log):
    # Old cheap samples then recent expensive ones; window=3 → only recent count.
    recs = [{"agent": "kirocrew", "mem_gb": 0.1, "cpu_cores": 0.1} for _ in range(10)]
    recs += [{"agent": "kirocrew", "mem_gb": 0.9, "cpu_cores": 0.1} for _ in range(3)]
    _seed(cost_log, recs)
    val = sc.read_learned_cost("mem_gb", window=3, min_samples=3)
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
    """#6345: compaction REPLACES the log with what it parsed.

    So this reader cannot skip an over-cap record the way a read-only consumer
    can -- a skipped record would be permanently deleted by the next
    compaction. Found by GPT 5.6 review on PR #7651; the audit had classified
    this site skip-safe by tracing only its percentile consumer and missing
    ``compact_cost_log``'s rewrite.
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

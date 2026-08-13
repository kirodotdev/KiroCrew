"""Drive the REAL telemetry aggregation over synthetic OTEL metric shards.

These exercise production code paths in dashboard/handlers/telemetry.py
(``_pct_from_buckets``, ``_Hist``, ``_aggregate``) rather than replicating the
logic, so a regression in the shard parser or percentile math fails the test.
"""
import json
from pathlib import Path

from kiro_crew.dashboard.handlers.telemetry import _aggregate, _Hist, _pct_from_buckets

_BOUNDS = [10, 20, 30, 40, 50]


def test_pct_from_buckets_interpolates_within_bucket():
    # bucket_counts has len(bounds)+1 entries; all 4 obs fall in the 20-30 bucket.
    counts = [0, 0, 4, 0, 0, 0]
    p50 = _pct_from_buckets(counts, _BOUNDS, 0.50)
    assert 20.0 <= p50 <= 30.0


def test_pct_from_buckets_empty_is_zero():
    assert _pct_from_buckets([0, 0], [10], 0.5) == 0.0


def test_pct_from_buckets_overflow_bucket_returns_lower_bound():
    # All obs in the +Inf overflow bucket (index == len(bounds)).
    assert _pct_from_buckets([0, 0, 0, 0, 0, 3], _BOUNDS, 0.90) == float(_BOUNDS[-1])


def test_hist_merges_data_points():
    h = _Hist()
    dp = {
        "count": 2, "sum": 30.0, "min": 10.0, "max": 20.0,
        "bucket_counts": [0, 1, 1, 0, 0, 0], "explicit_bounds": _BOUNDS,
    }
    h.add(dp)
    h.add(dp)
    s = h.stats()
    assert s["count"] == 4
    assert s["min_ms"] == 10.0
    assert s["max_ms"] == 20.0
    assert s["mean_ms"] == 15.0  # 60.0 / 4


def _write_shard(tmp_path: Path, metrics: list) -> Path:
    line = {"resource_metrics": [{"scope_metrics": [{"metrics": metrics}]}]}
    p = tmp_path / "metrics-2026-07-11-1234.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return p


def _startup_dp(attrs: dict, count: int = 1, bucket: int = 1) -> dict:
    counts = [0] * (len(_BOUNDS) + 1)
    counts[bucket] = count
    return {
        "attributes": attrs,
        "count": count,
        "sum": float(count * 15),
        "min": 15.0,
        "max": 15.0,
        "bucket_counts": counts,
        "explicit_bounds": _BOUNDS,
    }


def test_aggregate_counts_only_the_end_to_end_startup_point(tmp_path: Path):
    """Per-phase points are components of one startup, not startups.

    The kiro backend emits phase=total PLUS one point per internal phase. Before
    the fix all four were summed, inflating the startup count ~4x and stacking
    four unrelated latency distributions into one set of buckets.
    """
    ready = {"outcome": "ready", "backend": "kiro", "spawned": True}
    startup = {"name": "kirocrew.session.startup.duration", "data": {"data_points": [
        _startup_dp({**ready, "phase": "total"}, bucket=4),
        _startup_dp({**ready, "phase": "spawn_init"}, bucket=2),
        _startup_dp({**ready, "phase": "session_new"}, bucket=3),
        _startup_dp({**ready, "phase": "set_model"}, bucket=0),
    ]}}

    s = _aggregate([_write_shard(tmp_path, [startup])])["startup"]

    # One startup, not four.
    assert s["overall"]["count"] == 1
    assert s["outcome"] == {"ready": 1}
    assert s["daily"][0]["count"] == 1
    # ...and the distribution holds only the end-to-end sample.
    assert sum(s["distribution"]["buckets"]) == 1
    # The phase detail is preserved, just kept out of the startup totals.
    assert [p["name"] for p in s["phases"]] == ["session_new", "set_model", "spawn_init"]
    assert all(p["count"] == 1 for p in s["phases"])


def test_aggregate_kiro_startup_counts_as_cold(tmp_path: Path):
    """spawned=True on the kiro path must land in cold, not warm.

    Regression guard: the kiro emit previously carried no ``spawned`` attribute,
    so bool(None) filed every cold start as warm and cold read as empty forever.
    """
    startup = {"name": "kirocrew.session.startup.duration", "data": {"data_points": [
        _startup_dp({"outcome": "ready", "backend": "kiro", "phase": "total", "spawned": True}),
    ]}}
    s = _aggregate([_write_shard(tmp_path, [startup])])["startup"]
    assert s["cold"]["count"] == 1
    assert s["warm"]["count"] == 0


def test_aggregate_treats_missing_phase_as_the_total(tmp_path: Path):
    """The claude path emits no phase attribute at all — still one startup."""
    startup = {"name": "kirocrew.session.startup.duration", "data": {"data_points": [
        _startup_dp({"outcome": "ready", "spawned": False}),
    ]}}
    s = _aggregate([_write_shard(tmp_path, [startup])])["startup"]
    assert s["overall"]["count"] == 1
    assert s["warm"]["count"] == 1
    assert s["phases"] == []


def test_aggregate_startup_turn_and_other(tmp_path: Path):
    startup = {"name": "kirocrew.session.startup.duration", "data": {"data_points": [
        {"attributes": {"outcome": "ready", "spawned": True}, "count": 3, "sum": 45.0,
         "min": 10.0, "max": 25.0, "bucket_counts": [0, 1, 1, 1, 0, 0], "explicit_bounds": _BOUNDS},
    ]}}
    turn = {"name": "kirocrew.turn.duration", "data": {"data_points": [
        {"attributes": {"outcome": "ok"}, "count": 3, "sum": 30.0, "min": 5.0, "max": 15.0,
         "bucket_counts": [1, 1, 1, 0, 0, 0], "explicit_bounds": _BOUNDS},
        {"attributes": {"outcome": "error"}, "count": 1, "sum": 45.0, "min": 45.0, "max": 45.0,
         "bucket_counts": [0, 0, 0, 0, 1, 0], "explicit_bounds": _BOUNDS},
    ]}}
    warm = {"name": "kirocrew.mcp.warm_pool.acquire", "data": {"data_points": [
        {"attributes": {"result": "hit"}, "value": 3},
        {"attributes": {"result": "miss"}, "value": 1},
    ]}}

    result = _aggregate([_write_shard(tmp_path, [startup, turn, warm])])

    # Startup: split by spawned, distribution buckets surfaced.
    assert result["startup"]["overall"]["count"] == 3
    assert result["startup"]["cold"]["count"] == 3  # spawned=True
    assert result["startup"]["warm"]["count"] == 0
    assert result["startup"]["distribution"]["buckets"]

    # Turn: outcome split + fault rate = non-ok / total.
    assert result["turn"]["outcome"] == {"ok": 3, "error": 1}
    assert result["turn"]["fault_rate"] == 0.25  # 1 error / 4

    # Other: warm-pool counter with per-attr breakdown.
    warm_rows = [o for o in result["other"] if o["name"] == "kirocrew.mcp.warm_pool.acquire"]
    assert warm_rows and warm_rows[0]["kind"] == "counter"
    assert warm_rows[0]["total"] == 4.0
    assert warm_rows[0]["by_attr"]["result=hit"] == 3.0
    assert warm_rows[0]["by_attr"]["result=miss"] == 1.0


def _turn_dp(attrs: dict, count: int = 1) -> dict:
    """Minimal turn histogram data-point (single bucket, no distribution)."""
    counts = [0] * (len(_BOUNDS) + 1)
    counts[1] = count
    return {
        "attributes": attrs,
        "count": count,
        "sum": float(count * 20),
        "min": 20.0,
        "max": 20.0,
        "bucket_counts": counts,
        "explicit_bounds": _BOUNDS,
    }


def test_fault_rate_excludes_watchdog_recovery_outcomes(tmp_path: Path):
    """F4 regression: tool_stall and stale_recover must NOT count toward
    fault_rate even though they are not 'ok'. Only genuine terminal faults
    (error, timeout, unknown) are faults; watchdog recovery outcomes are
    tracked separately under kirocrew.watchdog.recovery.outcome.

    'unknown' IS included because pre-labelling metric shards use it for
    unclassified non-ok outcomes; excluding it would silently inflate the
    denominator without matching the numerator on the 14-day lookback."""
    turn = {"name": "kirocrew.turn.duration", "data": {"data_points": [
        _turn_dp({"outcome": "ok"}, count=4),
        _turn_dp({"outcome": "error"}, count=1),      # terminal fault
        _turn_dp({"outcome": "timeout"}, count=1),    # terminal fault
        _turn_dp({"outcome": "unknown"}, count=1),    # legacy shard — terminal fault
        _turn_dp({"outcome": "tool_stall"}, count=3),     # watchdog recovery — NOT a fault
        _turn_dp({"outcome": "stale_recover"}, count=2),  # watchdog recovery — NOT a fault
    ]}}
    result = _aggregate([_write_shard(tmp_path, [turn])])

    total = 4 + 1 + 1 + 1 + 3 + 2  # = 12
    true_faults = 1 + 1 + 1          # error + timeout + unknown
    expected_rate = round(true_faults / total, 4)

    assert result["turn"]["outcome"] == {
        "ok": 4, "error": 1, "timeout": 1, "unknown": 1, "tool_stall": 3, "stale_recover": 2,
    }
    assert result["turn"]["fault_rate"] == expected_rate  # = 0.25

    # Ensure genuine error/timeout STILL count as faults (not accidentally
    # excluded by an overly aggressive allowlist).
    sub = tmp_path / "sub"
    sub.mkdir(exist_ok=True)
    error_only_turn = {"name": "kirocrew.turn.duration", "data": {"data_points": [
        _turn_dp({"outcome": "ok"}, count=3),
        _turn_dp({"outcome": "error"}, count=1),
    ]}}
    result2 = _aggregate([_write_shard(sub, [error_only_turn])])
    assert result2["turn"]["fault_rate"] == 0.25  # 1 error / 4 — unchanged


def test_fault_rate_counts_exhausted_stall_turns_as_faults(tmp_path: Path):
    """A stall_exhausted turn is a dead session and must reach fault_rate.

    The recovered-stall exclusion labels recovery turns tool_stall /
    stale_recover — but the final turn of a cycle whose budget dies with
    "start a new chat" labels stall_exhausted at the emit site, which the
    aggregator's allowlist counts as a terminal fault. Recovered stalls stay
    excluded; dead sessions count; fault_rate remains single-series."""
    turn = {"name": "kirocrew.turn.duration", "data": {"data_points": [
        _turn_dp({"outcome": "ok"}, count=5),
        _turn_dp({"outcome": "tool_stall"}, count=3),        # retries — NOT faults
        _turn_dp({"outcome": "stale_recover"}, count=1),     # recovered — NOT a fault
        _turn_dp({"outcome": "stall_exhausted"}, count=1),   # dead session — fault
    ]}}
    # The recovery counter is pure mechanism telemetry: it must NOT feed
    # fault_rate (the exhausted turn above already carries the fault).
    recovery = {"name": "kirocrew.watchdog.recovery.outcome", "data": {"data_points": [
        {"attributes": {"mechanism": "tool_stall", "outcome": "exhausted",
                        "attempt_bucket": 3}, "value": 1},
        {"attributes": {"mechanism": "stale_recover", "outcome": "recovered",
                        "attempt_bucket": 1}, "value": 1},
    ]}}
    result = _aggregate([_write_shard(tmp_path, [turn, recovery])])

    # 1 exhausted turn / 10 turns; recovery-counter points change nothing.
    assert result["turn"]["fault_rate"] == 0.1


def test_every_turn_outcome_label_is_classified_fault_or_excluded():
    """Cross-module drift gate between _turn_outcome and the fault allowlist.

    ``_turn_outcome`` (chat_runner) mints the labels; ``_TERMINAL_FAULT_OUTCOMES``
    (telemetry) decides which count toward fault_rate. They are hand-synced lists
    in different modules, and because the aggregator is an allowlist, a label
    added to the emitter but classified in neither set would silently fall out of
    the fault_rate numerator while still growing the denominator — an optimistic
    dashboard with no failing test. Labels are harvested from _turn_outcome's
    return statements via AST so a new branch cannot dodge this gate."""
    import ast
    import inspect

    from kiro_crew.dashboard.chat_runner import _turn_outcome
    from kiro_crew.dashboard.handlers.telemetry import _TERMINAL_FAULT_OUTCOMES

    labels: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(_turn_outcome))):
        if isinstance(node, ast.Return) and node.value is not None:
            labels |= {
                c.value
                for c in ast.walk(node.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
    # Self-check that the AST harvest actually captured the emitter's range —
    # an empty/partial harvest would make the assertions below vacuous.
    assert {"ok", "error", "stall_exhausted"} <= labels

    # Non-faults, each with its exclusion reason pinned by the tests above:
    # "ok" succeeded; "tool_stall"/"stale_recover" are recovered-in-place stalls
    # tracked under kirocrew.watchdog.recovery.outcome. Add a new label here or
    # to _TERMINAL_FAULT_OUTCOMES — never leave it unclassified.
    excluded = {"ok", "tool_stall", "stale_recover"}
    unclassified = labels - _TERMINAL_FAULT_OUTCOMES - excluded
    assert not unclassified, (
        f"_turn_outcome label(s) {sorted(unclassified)} are neither terminal "
        "faults nor explicitly excluded — classify them so fault_rate stays "
        "truthful."
    )
    # A label can't be both a fault and excluded.
    assert not (_TERMINAL_FAULT_OUTCOMES & excluded)
    # The reverse direction: every fault entry must have a producer — a
    # _turn_outcome label or "unknown" (minted by the aggregator itself for
    # attribute-less points). A dead entry can't be caught by the harvest
    # above and would misdocument what fault_rate counts.
    dead = _TERMINAL_FAULT_OUTCOMES - labels - {"unknown"}
    assert not dead, (
        f"_TERMINAL_FAULT_OUTCOMES entr(ies) {sorted(dead)} have no producer "
        "— no _turn_outcome branch returns them and the aggregator does not "
        "mint them."
    )


# ── Bucket-generation truthfulness + the acquire warm/cold split ──────────
#
# Two shipped defects are pinned here:
#
#   1. ``other_generations`` was pasted onto the turn and startup blocks by the
#      response builder, so the generic ``other`` instruments never carried it.
#      A window straddling a boundary change reported ONE generation's count and
#      percentiles with nothing saying a generation had been dropped — the MCP
#      acquire card showed that subset beside a full-window counter.
#   2. The MCP cold-load card read ``kirocrew.mcp.lazy_load.duration``, emitted
#      only by the legacy pre-ensure_backend spawn path, so it read "no data yet"
#      forever while real cold spawns were being recorded on the acquire
#      histogram under ``warm=false``.

_OLD_BOUNDS = [1, 2, 3, 4, 5]  # a second, incompatible bounds generation


def _hist_dp(
    attrs: dict,
    *,
    count: int = 1,
    bounds: list | None = None,
    bucket: int = 1,
    ns: int = 1,
    each_ms: float = 15.0,
) -> dict:
    b = bounds if bounds is not None else _BOUNDS
    counts = [0] * (len(b) + 1)
    counts[bucket] = count
    return {
        "attributes": attrs,
        "count": count,
        "sum": float(count) * each_ms,
        "min": each_ms,
        "max": each_ms,
        "bucket_counts": counts,
        "explicit_bounds": b,
        "time_unix_nano": ns,
    }


def test_stats_carries_other_generations_even_when_empty():
    """The caveat travels with the numbers it qualifies, not beside them."""
    empty = _Hist().stats()
    assert empty["other_generations"] == 0
    assert empty["total_count"] == 0

    h = _Hist()
    h.add(_hist_dp({}, ns=2))                      # newest generation
    h.add(_hist_dp({}, bounds=_OLD_BOUNDS, ns=1))  # older, dropped
    s = h.stats()
    assert s["count"] == 1, "only the newest generation is reported"
    assert s["other_generations"] == 1


def test_total_count_is_the_full_population_not_the_group_count():
    """A generation count cannot be reconciled against a full-window number.

    Two dropped generations holding 7 and 5 samples are ONE "2 generations"
    string but 12 missing samples; only the sample figure is comparable to the
    reported ``count`` and to a counter shown beside it.
    """
    h = _Hist()
    h.add(_hist_dp({}, count=3, ns=30))                              # reported
    h.add(_hist_dp({}, count=7, bounds=_OLD_BOUNDS, ns=20))          # dropped
    h.add(_hist_dp({}, count=5, bounds=[2, 4, 6, 8, 10], ns=10))     # dropped
    s = h.stats()
    assert s["count"] == 3
    assert s["other_generations"] == 2
    assert s["total_count"] == 15  # 3 reported + 7 + 5 dropped


def test_other_histograms_report_dropped_generations(tmp_path: Path):
    """Regression: the ``other`` surface used to omit other_generations."""
    acquire = {"name": "kirocrew.mcp.backend.acquire.duration", "data": {
        "data_points": [
            _hist_dp({"warm": True}, count=4, ns=20),
            _hist_dp({"warm": True}, count=7, bounds=_OLD_BOUNDS, ns=10),
        ]}}

    result = _aggregate([_write_shard(tmp_path, [acquire])])
    row = next(o for o in result["other"]
               if o["name"] == "kirocrew.mcp.backend.acquire.duration")

    assert row["count"] == 4, "newest generation only"
    assert row["other_generations"] == 1, "and it says so"
    assert row["total_count"] == 11, "with the full-window population"


def test_acquire_splits_expose_the_cold_side(tmp_path: Path):
    """The cold-spawn card is fed by the ``warm=false`` half of acquire."""
    acquire = {"name": "kirocrew.mcp.backend.acquire.duration", "data": {
        "data_points": [
            _hist_dp({"warm": True}, count=9, each_ms=15.0),
            _hist_dp({"warm": False}, count=2, bucket=4, each_ms=45.0),
        ]}}

    result = _aggregate([_write_shard(tmp_path, [acquire])])
    row = next(o for o in result["other"]
               if o["name"] == "kirocrew.mcp.backend.acquire.duration")

    assert row["count"] == 11
    assert set(row["splits"]) == {"warm=true", "warm=false"}
    assert row["splits"]["warm=false"]["count"] == 2
    assert row["splits"]["warm=true"]["count"] == 9
    # Each side keeps its own percentiles rather than the merged ones.
    assert row["splits"]["warm=false"]["p50_ms"] > row["splits"]["warm=true"]["p50_ms"]
    # And the caveat is per-split too.
    assert row["splits"]["warm=false"]["other_generations"] == 0


def test_splits_are_restricted_to_named_low_cardinality_attrs(tmp_path: Path):
    """method/route must NOT spawn a sub-histogram per endpoint."""
    req = {"name": "kirocrew.gateway.request.duration", "data": {
        "data_points": [
            _hist_dp({"method": "GET", "route": "/api/a"}),
            _hist_dp({"method": "POST", "route": "/api/b"}),
        ]}}
    skill = {"name": "kirocrew.skill.lazy_load.duration", "data": {
        "data_points": [_hist_dp({"transport": "stdio"})]}}

    result = _aggregate([_write_shard(tmp_path, [req, skill])])
    by_name = {o["name"]: o for o in result["other"]}

    assert "splits" not in by_name["kirocrew.gateway.request.duration"]
    assert "splits" not in by_name["kirocrew.skill.lazy_load.duration"]


def test_turn_and_startup_generation_count_comes_from_stats(tmp_path: Path):
    """Single source: the field arrives with the stats, not as a sibling."""
    turn = {"name": "kirocrew.turn.duration", "data": {"data_points": [
        _hist_dp({"outcome": "ok"}, count=3, ns=20),
        _hist_dp({"outcome": "ok"}, count=5, bounds=_OLD_BOUNDS, ns=10),
    ]}}
    ready = {"outcome": "ready", "backend": "kiro", "spawned": True,
             "phase": "total"}
    startup = {"name": "kirocrew.session.startup.duration", "data": {
        "data_points": [
            _hist_dp(ready, count=2, ns=20),
            _hist_dp(ready, count=6, bounds=_OLD_BOUNDS, ns=10),
        ]}}

    result = _aggregate([_write_shard(tmp_path, [turn, startup])])

    assert result["turn"]["count"] == 3
    assert result["turn"]["other_generations"] == 1
    assert result["turn"]["total_count"] == 8
    assert result["startup"]["overall"]["count"] == 2
    assert result["startup"]["overall"]["other_generations"] == 1
    assert result["startup"]["overall"]["total_count"] == 8


class TestTelemetryPosture:
    """``_telemetry_cfg`` reports the EFFECTIVE state, not the stored flag.

    ``KIROCREW_TELEMETRY`` overrides ``telemetry.enabled`` inside the collector, so
    a panel that echoed the config value alone would say "off" on a host that is
    recording — and would offer a switch whose write the collector ignores. The
    pin is resolved through ``metrics.provider`` so the control and the collector
    cannot disagree about what "on" means.
    """

    def _cfg(self, enabled: bool):
        from types import SimpleNamespace
        from unittest.mock import patch as _patch

        return _patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            return_value=SimpleNamespace(telemetry=SimpleNamespace(enabled=enabled)),
        )

    def test_config_flag_when_env_unset(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.telemetry import _telemetry_cfg

        monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
        with self._cfg(True):
            state = _telemetry_cfg()
        assert state.enabled is True
        assert state.env_pinned is False

    def test_env_truthy_overrides_a_false_config(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.telemetry import _telemetry_cfg

        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        with self._cfg(False):
            state = _telemetry_cfg()
        assert state.enabled is True
        assert state.env_pinned is True
        assert state.env_var == "KIROCREW_TELEMETRY"

    def test_env_falsy_overrides_a_true_config(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.telemetry import _telemetry_cfg

        monkeypatch.setenv("KIROCREW_TELEMETRY", "off")
        with self._cfg(True):
            state = _telemetry_cfg()
        assert state.enabled is False
        assert state.env_pinned is True

    def test_blank_env_is_not_a_pin(self, monkeypatch) -> None:
        # An exported-but-empty variable is the shape a shell leaves behind; it
        # must defer to the config file rather than pinning the switch off.
        from kiro_crew.dashboard.handlers.telemetry import _telemetry_cfg

        monkeypatch.setenv("KIROCREW_TELEMETRY", "  ")
        with self._cfg(True):
            state = _telemetry_cfg()
        assert state.enabled is True
        assert state.env_pinned is False

    def test_env_var_name_comes_from_the_collector(self) -> None:
        # The message names a variable for the user to unset, so the name must be
        # the one the collector reads, not a copy that can drift from it.
        from kiro_crew.dashboard.handlers.telemetry import _telemetry_cfg
        from kiro_crew.metrics.provider import TELEMETRY_ENV_VAR

        assert _telemetry_cfg().env_var == TELEMETRY_ENV_VAR

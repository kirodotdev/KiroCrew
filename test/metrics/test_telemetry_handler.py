"""Drive the REAL telemetry aggregation over synthetic OTEL metric shards.

These exercise production code paths in dashboard/handlers/telemetry.py
(``_pct_from_buckets``, ``_Hist``, ``_aggregate``) rather than replicating the
logic, so a regression in the shard parser or percentile math fails the test.
"""

import json
import math
import sys
from pathlib import Path

from kiro_crew.dashboard.handlers.telemetry import _aggregate, _Hist, _pct_from_buckets
from kiro_crew.metrics.schema import RESOURCE_ATTR_PROCESS_START_TIME

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
        "count": 2,
        "sum": 30.0,
        "min": 10.0,
        "max": 20.0,
        "bucket_counts": [0, 1, 1, 0, 0, 0],
        "explicit_bounds": _BOUNDS,
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
    startup = {
        "name": "kirocrew.session.startup.duration",
        "data": {
            "data_points": [
                _startup_dp({**ready, "phase": "total"}, bucket=4),
                _startup_dp({**ready, "phase": "spawn_init"}, bucket=2),
                _startup_dp({**ready, "phase": "session_new"}, bucket=3),
                _startup_dp({**ready, "phase": "set_model"}, bucket=0),
            ]
        },
    }

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
    startup = {
        "name": "kirocrew.session.startup.duration",
        "data": {
            "data_points": [
                _startup_dp(
                    {"outcome": "ready", "backend": "kiro", "phase": "total", "spawned": True}
                ),
            ]
        },
    }
    s = _aggregate([_write_shard(tmp_path, [startup])])["startup"]
    assert s["cold"]["count"] == 1
    assert s["warm"]["count"] == 0


def test_aggregate_treats_missing_phase_as_the_total(tmp_path: Path):
    """The claude path emits no phase attribute at all — still one startup."""
    startup = {
        "name": "kirocrew.session.startup.duration",
        "data": {
            "data_points": [
                _startup_dp({"outcome": "ready", "spawned": False}),
            ]
        },
    }
    s = _aggregate([_write_shard(tmp_path, [startup])])["startup"]
    assert s["overall"]["count"] == 1
    assert s["warm"]["count"] == 1
    assert s["phases"] == []


def test_aggregate_startup_turn_and_other(tmp_path: Path):
    startup = {
        "name": "kirocrew.session.startup.duration",
        "data": {
            "data_points": [
                {
                    "attributes": {"outcome": "ready", "spawned": True},
                    "count": 3,
                    "sum": 45.0,
                    "min": 10.0,
                    "max": 25.0,
                    "bucket_counts": [0, 1, 1, 1, 0, 0],
                    "explicit_bounds": _BOUNDS,
                },
            ]
        },
    }
    turn = {
        "name": "kirocrew.turn.duration",
        "data": {
            "data_points": [
                {
                    "attributes": {"outcome": "ok"},
                    "count": 3,
                    "sum": 30.0,
                    "min": 5.0,
                    "max": 15.0,
                    "bucket_counts": [1, 1, 1, 0, 0, 0],
                    "explicit_bounds": _BOUNDS,
                },
                {
                    "attributes": {"outcome": "error"},
                    "count": 1,
                    "sum": 45.0,
                    "min": 45.0,
                    "max": 45.0,
                    "bucket_counts": [0, 0, 0, 0, 1, 0],
                    "explicit_bounds": _BOUNDS,
                },
            ]
        },
    }
    warm = {
        "name": "kirocrew.mcp.warm_pool.acquire",
        "data": {
            # Real SDK shards always mark a Sum with aggregation_temporality /
            # is_monotonic (a Gauge's data block carries neither) — the aggregator
            # classifies on that, so the fixture must carry it too.
            "aggregation_temporality": 1,
            "is_monotonic": True,
            "data_points": [
                {"attributes": {"result": "hit"}, "value": 3},
                {"attributes": {"result": "miss"}, "value": 1},
            ],
        },
    }

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


def test_aggregate_cumulative_sums_are_window_relative_and_add_across_pids(tmp_path: Path):
    """CUMULATIVE sums: window-relative delta per PID stream; PIDs add together.

    Observable counters (CPU seconds, GC stats) export a lifetime snapshot
    every cycle (temporality 2). Within one process the stream's first
    in-window sample is the baseline and re-emissions after a telemetry off/on
    provider rebuild are idempotent no-ops — so each PID contributes only the
    activity accrued inside the window (150-100 and 30-10), and the
    cross-process total is the sum of those deltas, never 100+150+10+30 and
    never the raw lifetime snapshots.
    """

    def cpu(v1: float, v2: float) -> dict:
        return {
            "name": "kirocrew.process.cpu.seconds",
            "data": {
                "aggregation_temporality": 2,
                "is_monotonic": True,
                "data_points": [
                    {"attributes": {}, "value": v1, "time_unix_nano": 100},
                    {"attributes": {}, "value": v2, "time_unix_nano": 200},
                ],
            },
        }

    p1 = tmp_path / "metrics-2026-08-21-1000.jsonl"
    p1.write_text(
        json.dumps({"resource_metrics": [{"scope_metrics": [{"metrics": [cpu(100.0, 150.0)]}]}]})
        + "\n",
        encoding="utf-8",
    )
    p2 = tmp_path / "metrics-2026-08-21-2000.jsonl"
    p2.write_text(
        json.dumps({"resource_metrics": [{"scope_metrics": [{"metrics": [cpu(10.0, 30.0)]}]}]})
        + "\n",
        encoding="utf-8",
    )

    result = _aggregate([p1, p2])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["kind"] == "counter"
    # In-window delta per PID ((150-100) + (30-10)), summed across PIDs.
    assert rows[0]["total"] == 70.0


def test_aggregate_lifetime_total_gauges_are_reduced_window_relative(tmp_path: Path):
    """A lifetime-total GAUGE reports the window's activity, not the total.

    The process CPU/GC readings became observable gauges so that nothing
    Kiro Crew exports is cumulative, but their value is still "since this
    process started". Reporting the newest sample the way a state gauge is
    reported would put an ever-growing number on the panel; they take the
    window-relative path instead, so this reads 40 (150-110), not 150.
    """
    metric = {
        "name": "kirocrew.process.cpu.seconds",
        "data": {  # no aggregation_temporality / is_monotonic: a Gauge block
            "data_points": [
                {"attributes": {}, "value": 110.0, "time_unix_nano": 100},
                {"attributes": {}, "value": 130.0, "time_unix_nano": 200},
                {"attributes": {}, "value": 150.0, "time_unix_nano": 300},
            ],
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["kind"] == "counter"
    assert rows[0]["total"] == 40.0


def test_aggregate_stitches_a_lifetime_total_across_the_instrument_switch(tmp_path: Path):
    """One continuous series across shards written before AND after the switch.

    The window outlives the change: shards written while these were observable
    counters carry CUMULATIVE sums, newer ones carry gauges. Both shapes must
    reduce into ONE row for the metric — routing the gauge to the gauge reducer
    would split the window into a counter row and a gauge row, and the panel
    would show the same instrument twice with two different meanings.
    """
    old_shape = {
        "name": "kirocrew.process.cpu.seconds",
        "data": {
            "aggregation_temporality": 2,
            "is_monotonic": True,
            "data_points": [
                {"attributes": {}, "value": 100.0, "time_unix_nano": 100},
                {"attributes": {}, "value": 120.0, "time_unix_nano": 200},
            ],
        },
    }
    new_shape = {
        "name": "kirocrew.process.cpu.seconds",
        "data": {
            "data_points": [
                {"attributes": {}, "value": 160.0, "time_unix_nano": 300},
            ],
        },
    }
    result = _aggregate([_write_shard(tmp_path, [old_shape, new_shape])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert len(rows) == 1, f"the switch split one instrument into {len(rows)} rows: {rows}"
    # Same process, same stream: baseline 100 (first in window), newest 160.
    assert rows[0]["total"] == 60.0


def test_aggregate_state_gauges_are_not_reduced_like_lifetime_totals(tmp_path: Path):
    """Only the DECLARED lifetime totals take the differencing path.

    RSS is the counter-example that matters: memory falls as well as rises, so
    differencing it would report a leak as "0 bytes" and a release as a drop.
    Its newest sample IS the answer.
    """
    metric = {
        "name": "kirocrew.process.memory.rss_bytes",
        "data": {
            "data_points": [
                {"attributes": {}, "value": 400.0, "time_unix_nano": 100},
                {"attributes": {}, "value": 900.0, "time_unix_nano": 200},
            ],
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.memory.rss_bytes"]
    assert rows and rows[0]["kind"] == "gauge"
    assert rows[0]["latest"] == 900.0


def test_the_instrument_modules_stay_off_the_import_path(tmp_path: Path):
    """Importing this handler must not pull the instrument modules in.

    The gateway imports its handlers while it is still assembling routes, before
    the socket is bound. Importing ``process_gauges`` there costs a measured 22ms
    and 14 extra modules (sqlite3 among them, via platform_compat) for a telemetry
    subsystem that is off by default. The lifetime-total roster is needed only
    inside ``_aggregate``, so it is imported on first use -- this pins that, and
    pins that the deferred import still works when the aggregation path runs.
    """
    handler = sys.modules["kiro_crew.dashboard.handlers.telemetry"]
    assert not hasattr(handler, "LIFETIME_TOTAL_METRICS"), (
        "the roster was imported at module scope again, putting the instrument "
        "modules back on the gateway boot path"
    )

    names = handler._lifetime_total_gauge_names()
    assert "kirocrew.process.cpu.seconds" in names
    assert "kirocrew.inventory.probe.failures" in names, "the inventory half is missing"
    # Memoized: the second call must hand back the same object, not re-import.
    assert handler._lifetime_total_gauge_names() is names

    # And the deferred path is what _aggregate actually consults.
    metric = {
        "name": "kirocrew.inventory.probe.failures",
        "data": {
            "data_points": [
                {"attributes": {"probe": "crons"}, "value": 2.0, "time_unix_nano": 100},
                {"attributes": {"probe": "crons"}, "value": 5.0, "time_unix_nano": 200},
            ]
        },
    }
    rows = [
        o
        for o in _aggregate([_write_shard(tmp_path, [metric])])["other"]
        if o["name"] == "kirocrew.inventory.probe.failures"
    ]
    assert rows and rows[0]["kind"] == "counter", rows
    assert rows[0]["by_attr"] == {"probe=crons": 3.0}, rows[0]


def test_a_reused_pid_cannot_make_a_lifetime_total_go_negative(tmp_path: Path):
    """PID reuse across a restart must not produce a negative increment here.

    A lifetime-total GAUGE has no counter-reset semantics of its own, so a naive
    consumer differencing consecutive samples would read the restart as a large
    negative step once the kernel wraps `pid_max` and a new process inherits the
    old PID. This aggregator is not exposed to that: the local exporter stamps
    each record with the writing process's OS start-time token, and streams are
    keyed by (PID, token), so the reuser is a brand-new stream whose own
    first-in-window sample is its baseline. Both processes contribute their own
    in-window activity and the row stays positive.
    """

    def cpu(identity: str, first: float, second: float, t0: int) -> dict:
        return {
            "resource": {"attributes": {RESOURCE_ATTR_PROCESS_START_TIME: identity}},
            "scope_metrics": [
                {
                    "metrics": [
                        {
                            "name": "kirocrew.process.cpu.seconds",
                            "data": {  # Gauge block: no temporality markers
                                "data_points": [
                                    {"attributes": {}, "value": first, "time_unix_nano": t0},
                                    {
                                        "attributes": {},
                                        "value": second,
                                        "time_unix_nano": t0 + 100,
                                    },
                                ],
                            },
                        }
                    ]
                }
            ],
        }

    # One shard file = one PID by name. The first process ran for hours (900s of
    # CPU); the reuser starts from nothing.
    shard = tmp_path / "metrics-2026-07-11-4242.jsonl"
    shard.write_text(
        json.dumps({"resource_metrics": [cpu("proc-a", 900.0, 940.0, 100)]})
        + "\n"
        + json.dumps({"resource_metrics": [cpu("proc-b", 2.0, 7.0, 300)]})
        + "\n",
        encoding="utf-8",
    )

    result = _aggregate([shard])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert len(rows) == 1, f"one instrument must yield one row, got {rows}"
    # 40 from the first process + 5 from the reuser. Never negative, and never
    # the 938 a single-stream reading would subtract.
    assert rows[0]["total"] == 45.0, rows[0]


def test_aggregate_cumulative_detects_counter_reset_on_pid_reuse(tmp_path: Path):
    """A cumulative stream dropping below its own max is a process boundary.

    PID reuse within the shard window makes two processes share a (pid, attr)
    stream key. Reset detection banks the finished segment when the value
    drops, so the later process is never credited against the earlier one's
    counter: the first process's only sample (100) is the stream baseline
    (its pre-sample activity is unattributable), and the restarted process —
    whose in-window start is proven by the reset — contributes its full 30.
    """
    metric = {
        "name": "kirocrew.process.cpu.seconds",
        "data": {
            "aggregation_temporality": 2,
            "is_monotonic": True,
            "data_points": [
                {"attributes": {}, "value": 100.0, "time_unix_nano": 100},
                {"attributes": {}, "value": 10.0, "time_unix_nano": 200},
                {"attributes": {}, "value": 30.0, "time_unix_nano": 300},
            ],
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 30.0


def _cumulative_metric(points: list[tuple[int, float]]) -> dict:
    return {
        "name": "kirocrew.process.cpu.seconds",
        "data": {
            "aggregation_temporality": 2,
            "is_monotonic": True,
            "data_points": [
                {"attributes": {}, "value": v, "time_unix_nano": ts} for ts, v in points
            ],
        },
    }


def _identity_line(metrics: list, identity: str | None) -> str:
    """One export-cycle line, optionally stamped like the local exporter.

    The attribute key is spelled literally on purpose: it pins the WIRE format
    already sitting in shards on disk, so renaming the schema constant cannot
    silently orphan every stamped shard.
    """
    rm: dict = {"scope_metrics": [{"metrics": metrics}]}
    if identity is not None:
        rm["resource"] = {"attributes": {"kirocrew.process.start_time": identity}}
    return json.dumps({"resource_metrics": [rm]})


def test_aggregate_cumulative_identity_splits_reused_pid_without_a_value_drop(tmp_path: Path):
    """A changed identity is a process boundary even when no value drop exists.

    The value heuristic's one blind spot: PID reuse where the new process's
    FIRST snapshot (150) already exceeds the old process's max (140), so no
    drop is ever observed and the two lifetimes merge into one stream —
    reporting 175-100=75, which credits the 140→150 gap between the processes
    as if it were observed activity. The resource-level identity makes the
    boundary deterministic: each process is its own stream with its own
    window-relative baseline, (140-100) + (175-150) = 65.
    """
    shard = tmp_path / "metrics-2026-08-21-1234.jsonl"
    shard.write_text(
        _identity_line([_cumulative_metric([(100, 100.0), (200, 140.0)])], "111")
        + "\n"
        + _identity_line([_cumulative_metric([(300, 150.0), (400, 175.0)])], "222")
        + "\n",
        encoding="utf-8",
    )
    result = _aggregate([shard])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 65.0


def test_aggregate_cumulative_same_identity_stitches_across_provider_rebuild(tmp_path: Path):
    """An unchanged identity keeps rebuild segments in ONE stream.

    A telemetry off/on toggle rebuilds the provider in-process; the rebuilt
    exporter stamps the SAME module-cached token, so its re-emitted snapshots
    join the existing stream and stay idempotent no-ops — 150-100=50, never a
    doubled total and never a fresh baseline per rebuild.
    """
    shard = tmp_path / "metrics-2026-08-21-1234.jsonl"
    shard.write_text(
        _identity_line([_cumulative_metric([(100, 100.0), (200, 140.0)])], "111")
        + "\n"
        + _identity_line([_cumulative_metric([(300, 140.0), (400, 150.0)])], "111")
        + "\n",
        encoding="utf-8",
    )
    result = _aggregate([shard])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 50.0


def test_aggregate_cumulative_identity_stream_treats_a_drop_as_garbage_not_reset(tmp_path: Path):
    """Within one identity, a value below the running max is never banked.

    One identity is one OS process, whose observable counters are monotonic —
    so a lower sample is shard garbage. Banking it as a reset would count the
    pre-drop segment AND the recovery: 140+150-100=190 for a stream whose real
    in-window growth is 150-100=50.
    """
    shard = tmp_path / "metrics-2026-08-21-1234.jsonl"
    shard.write_text(
        _identity_line(
            [_cumulative_metric([(100, 100.0), (200, 140.0), (300, 5.0), (400, 150.0)])],
            "111",
        )
        + "\n",
        encoding="utf-8",
    )
    result = _aggregate([shard])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 50.0


def test_aggregate_cumulative_non_string_identity_reads_as_identity_less(tmp_path: Path):
    """A malformed identity type must not mint a stream or mute the heuristic.

    The exporter only ever writes a string token. A corrupt shard carrying a
    number/list/object there would, if stringified, create an identity-keyed
    stream and silently disable reset banking — turning a genuine 100→10→30
    reset (30 of activity) into max-baseline arithmetic (0). Non-strings read
    as identity-less, so the value heuristic still banks the reset.
    """
    line = {
        "resource_metrics": [
            {
                "resource": {"attributes": {"kirocrew.process.start_time": 12345}},
                "scope_metrics": [
                    {"metrics": [_cumulative_metric([(100, 100.0), (200, 10.0), (300, 30.0)])]}
                ],
            }
        ]
    }
    shard = tmp_path / "metrics-2026-08-21-1234.jsonl"
    shard.write_text(json.dumps(line) + "\n", encoding="utf-8")
    result = _aggregate([shard])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 30.0


def test_aggregate_cumulative_legacy_lines_keep_the_value_heuristic(tmp_path: Path):
    """Identity-less shards aggregate bit-for-bit as before, alongside stamped ones.

    The legacy stream (no resource field, written before the identity existed)
    still banks on a value drop — baseline 100, reset to 10, growth to 30 ⇒ 30
    — while an identity-carrying stream from another process contributes its
    own window-relative delta (175-150=25). Streams add: 55.
    """
    legacy = tmp_path / "metrics-2026-08-21-1234.jsonl"
    legacy.write_text(
        _identity_line([_cumulative_metric([(100, 100.0), (200, 10.0), (300, 30.0)])], None) + "\n",
        encoding="utf-8",
    )
    stamped = tmp_path / "metrics-2026-08-21-5678.jsonl"
    stamped.write_text(
        _identity_line([_cumulative_metric([(400, 150.0), (500, 175.0)])], "222") + "\n",
        encoding="utf-8",
    )
    result = _aggregate([legacy, stamped])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 55.0


def test_aggregate_cumulative_totals_are_shard_order_independent(tmp_path: Path):
    """Reset detection runs on time-ordered samples, not shard file order.

    A per-PID stream spans one shard per day (plus rotations). Processing a
    newer shard first must not make the older day's smaller value look like a
    counter reset — that banked the newer segment and inflated the total
    (160+140=300 for a monotonic 100→160 stream).
    """

    def cpu(points: list[tuple[int, float]]) -> dict:
        return {
            "name": "kirocrew.process.cpu.seconds",
            "data": {
                "aggregation_temporality": 2,
                "is_monotonic": True,
                "data_points": [
                    {"attributes": {}, "value": v, "time_unix_nano": ts} for ts, v in points
                ],
            },
        }

    older = tmp_path / "metrics-2026-08-20-1234.jsonl"
    older.write_text(
        json.dumps(
            {
                "resource_metrics": [
                    {"scope_metrics": [{"metrics": [cpu([(100, 100.0), (200, 140.0)])]}]}
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    newer = tmp_path / "metrics-2026-08-21-1234.jsonl"
    newer.write_text(
        json.dumps(
            {
                "resource_metrics": [
                    {"scope_metrics": [{"metrics": [cpu([(300, 150.0), (400, 160.0)])]}]}
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    for order in ([older, newer], [newer, older]):
        result = _aggregate(order)
        rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
        # One monotonic stream 100→160: in-window activity 60, in either order.
        assert rows and rows[0]["total"] == 60.0


def test_aggregate_cumulative_reports_window_activity_not_lifetime(tmp_path: Path):
    """A process older than the window reports in-window activity only.

    The first in-window snapshot of a long-lived process already carries its
    lifetime total (e.g. 5000 CPU seconds); keeping the stream maximum would
    present that lifetime as the "Last 14d" total. The stream's first
    in-window sample is the baseline, so only the delta accrued inside the
    window (5010 - 5000) is reported.
    """
    metric = {
        "name": "kirocrew.process.cpu.seconds",
        "data": {
            "aggregation_temporality": 2,
            "is_monotonic": True,
            "data_points": [
                {"attributes": {}, "value": 5000.0, "time_unix_nano": 100},
                {"attributes": {}, "value": 5010.0, "time_unix_nano": 200},
            ],
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows and rows[0]["total"] == 10.0


def test_aggregate_rejects_non_finite_scalars(tmp_path: Path):
    """Infinity/NaN in a shard record degrades that point, never the endpoint.

    Python's json module parses the Infinity/NaN literals, so a poisoned
    shard would otherwise raise OverflowError in timestamp conversion or
    poison sums with inf.
    """
    metric = {
        "name": "kirocrew.process.threads.os",
        "data": {
            "data_points": [
                {"attributes": {}, "value": float("inf"), "time_unix_nano": 5},
                {"attributes": {}, "value": 96.0, "time_unix_nano": float("inf")},
                {"attributes": {}, "value": 97.0, "time_unix_nano": 9},
            ]
        },
    }
    # A Sum whose aggregation_temporality is itself poisoned: int(inf) raises
    # OverflowError, which the coercion's except tuple must absorb (the point
    # degrades to non-cumulative instead of 500ing the endpoint).
    poisoned_temporality = {
        "name": "kirocrew.poisoned.temporality",
        "data": {
            "data_points": [{"attributes": {}, "value": 3.0, "time_unix_nano": 7}],
            "aggregation_temporality": float("inf"),
            "is_monotonic": True,
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric, poisoned_temporality])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.threads.os"]
    assert rows and rows[0]["kind"] == "gauge"
    # inf value skipped; inf timestamp coerces the point to ts=0 (sorts
    # oldest), so ts=9 wins.
    assert rows[0]["latest"] == 97.0
    # The poisoned-temporality Sum degrades to a delta counter (cumulative
    # False), still counted -- and the payload stays strict JSON.
    poisoned = [o for o in result["other"] if o["name"] == "kirocrew.poisoned.temporality"]
    assert poisoned and poisoned[0]["kind"] == "counter"
    json.dumps(result, allow_nan=False)


def test_aggregate_rejects_non_finite_histogram_fields(tmp_path: Path):
    """Histogram mirror of the scalar guard: one poisoned data point degrades
    that point, never the endpoint.

    Python's json module parses Infinity/NaN literals, so before the fix a
    poisoned histogram point flowed straight through ``_Hist.add``'s bare
    float()/int() coercions: an inf sum/min/bound reached json.dumps and
    emitted an ``Infinity`` literal (which browser JSON.parse rejects), and
    an inf count/bucket_count/timestamp raised uncaught OverflowError."""
    poisoned_sum = _hist_dp({}, ns=2)
    poisoned_sum["sum"] = float("inf")
    poisoned_min = _hist_dp({}, ns=3)
    poisoned_min["min"] = float("nan")
    poisoned_bound = _hist_dp({}, ns=4)
    poisoned_bound["explicit_bounds"] = [10, float("inf"), 30, 40, 50]
    poisoned_count = _hist_dp({}, ns=5)
    poisoned_count["count"] = float("inf")
    poisoned_bucket = _hist_dp({}, ns=6)
    poisoned_bucket["bucket_counts"][1] = float("nan")
    poisoned_ts = _hist_dp({}, count=3, ns=7)
    poisoned_ts["time_unix_nano"] = float("inf")
    # json accepts arbitrary-precision ints: float(10**400) raises
    # OverflowError, a third path past a naive isfinite check.
    oversized_count = _hist_dp({}, ns=9)
    oversized_count["count"] = 10**400
    # A truthy non-list container raises TypeError at iteration, not inside
    # the element coercion.
    scalar_bounds = _hist_dp({}, ns=10)
    scalar_bounds["explicit_bounds"] = 5
    scalar_buckets = _hist_dp({}, ns=11)
    scalar_buckets["bucket_counts"] = True
    good = _hist_dp({}, count=2, ns=8, each_ms=25.0)

    metric = {
        "name": "kirocrew.mcp.backend.acquire.duration",
        "data": {
            "data_points": [
                poisoned_sum,
                poisoned_min,
                poisoned_bound,
                poisoned_count,
                poisoned_bucket,
                poisoned_ts,
                oversized_count,
                scalar_bounds,
                scalar_buckets,
                good,
            ]
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    row = next(o for o in result["other"] if o["name"] == "kirocrew.mcp.backend.acquire.duration")

    # Structurally poisoned points (sum/bound/count/bucket) are skipped whole;
    # the nan-min point survives with min degraded; the inf-timestamp point
    # survives sorting oldest. 1 (poisoned_min) + 3 (poisoned_ts) + 2 (good).
    assert row["count"] == 6
    assert row["other_generations"] == 0
    # The emitted payload must serialize to strict JSON — no Infinity/NaN
    # literal anywhere (this is what the browser's JSON.parse enforces).
    json.dumps(result, allow_nan=False)


def test_hist_add_validates_the_whole_point_before_mutation():
    """The _Hist.add invariant: nothing invalid ever enters group state.

    Three residual classes past the plain non-finite guard:
    - EXACTNESS: integer counts must not roundtrip through float
      (int(float(2**53 + 1)) silently rounds and corrupts emitted counts).
    - ACCUMULATION: two individually finite 1e308 sums overflow the
      accumulator to inf, which json.dumps emits as an Infinity literal.
    - STRUCTURE: a bucket_counts shorter/longer than len(bounds)+1 cannot
      merge into the group's shape (IndexError in percentile interpolation);
      the buckets degrade to absent while the point's scalars still count.
    """
    big = 2**53 + 1  # not representable as float; float roundtrip rounds it
    h = _Hist()
    exact = _hist_dp({}, ns=1)
    exact["count"] = big
    exact["bucket_counts"] = [0, big, 0, 0, 0, 0]
    h.add(exact)
    assert h.count == big, "integer count must be preserved exactly"
    assert h.buckets[1] == big

    # Prospective accumulated-sum guard: the second point is skipped whole.
    h2 = _Hist()
    a = _hist_dp({}, ns=1)
    a["sum"] = 1e308
    b = _hist_dp({}, ns=2)
    b["sum"] = 1e308
    h2.add(a)
    h2.add(b)
    g = h2._groups[tuple(float(x) for x in _BOUNDS)]
    assert math.isfinite(g["sum"])
    assert h2.count == 1, "the overflowing point is skipped whole"

    # Structural guard: a bucket list disagreeing with len(bounds)+1 cannot
    # merge into the group's shape. The buckets are dropped but the point's
    # independently-validated scalars still accumulate (pinned upstream by
    # test_telemetry_handlers_cov80.py -- no IndexError, count keeps counting).
    h3 = _Hist()
    short = _hist_dp({}, ns=1)
    short["explicit_bounds"] = [10]
    short["bucket_counts"] = [0, 0, 1]  # needs exactly 2 for one bound
    h3.add(short)
    assert h3.count == 1, "shape-mismatched buckets degrade; the point still counts"
    assert h3.buckets == [], "the disagreeing bucket list itself is dropped"
    # Integer-field strictness: oversized, boolean, negative, and fractional
    # counts are rejected whole, never clamped, coerced, or truncated.
    h3b = _Hist()
    for bad_count in (2**64, True, -1, 2.5):
        p = _hist_dp({}, ns=1)
        p["count"] = bad_count
        h3b.add(p)
    assert h3b.count == 0
    # A FALSY non-list container (false/0/"") is garbage, not "absent" —
    # only a genuinely missing or null key defaults to empty.
    for bad_container in (False, 0, ""):
        p = _hist_dp({}, ns=1)
        p["bucket_counts"] = bad_container
        h3b.add(p)
    assert h3b.count == 0
    none_ok = _hist_dp({}, ns=1)
    none_ok["explicit_bounds"] = None  # null key = absent, point stays legal
    none_ok["bucket_counts"] = None
    h3.add(none_ok)
    assert h3.count == 1

    # Falsy garbage never substitutes a default: "" as sum must skip the
    # point, not silently record 0.0 and skew the mean.
    h4 = _Hist()
    bad_sum = _hist_dp({}, ns=1)
    bad_sum["sum"] = ""
    h4.add(bad_sum)
    assert h4.count == 0
    # protobuf JSON encodes uint64 as strings: parse exactly, no float round.
    quoted = _hist_dp({}, ns=1)
    quoted["count"] = str(big)
    quoted["bucket_counts"] = [0, big, 0, 0, 0, 0]
    h4.add(quoted)
    assert h4.count == big, "quoted integer count must be preserved exactly"

    # Derived arithmetic must stay finite: individually finite bounds whose
    # adjacent span overflows (hi - lo = inf) would poison percentile
    # interpolation with an Infinity literal.
    h5 = _Hist()
    wide = _hist_dp({}, ns=1)
    wide["explicit_bounds"] = [-1e308, 1e308]
    wide["bucket_counts"] = [0, 1, 0]
    h5.add(wide)
    assert h5.count == 0, "non-finite interpolation span is skipped whole"


def test_aggregate_gauges_keep_latest_not_sum(tmp_path: Path):
    """Point-in-time gauges (no Sum markers in the data block) must report the
    NEWEST sample, never a total that grows with export-cycle count."""

    def gauge_metric(ts: int, value: float, attrs: dict | None = None) -> dict:
        return {
            "name": "kirocrew.process.threads.os",
            "data": {
                "data_points": [
                    {"attributes": attrs or {}, "value": value, "time_unix_nano": ts},
                ]
            },
        }

    shard = _write_shard(
        tmp_path,
        [gauge_metric(100, 70.0), gauge_metric(300, 72.0), gauge_metric(200, 71.0)],
    )
    result = _aggregate([shard])

    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.threads.os"]
    assert rows and rows[0]["kind"] == "gauge"
    # Three cycles observed 70/72/71 threads: the report is the newest sample
    # (72, ts=300), not 213.
    assert rows[0]["latest"] == 72.0


def test_aggregate_gauge_attr_sets_are_independent(tmp_path: Path):
    """Attributed gauge samples keep the newest value PER attribute set."""
    metric = {
        "name": "kirocrew.process.memory.rss_bytes",
        "data": {
            "data_points": [
                {"attributes": {"estimate": "current"}, "value": 100.0, "time_unix_nano": 1},
                {"attributes": {"estimate": "current"}, "value": 90.0, "time_unix_nano": 2},
            ]
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.memory.rss_bytes"]
    assert rows and rows[0]["kind"] == "gauge"
    # A gauge that went DOWN reports the newer, lower value — a sum (190) or a
    # max (100) would both misreport reclaimed memory.
    assert rows[0]["by_attr"]["estimate=current"] == 90.0


def test_aggregate_gauges_do_not_collapse_across_pids(tmp_path: Path):
    """Concurrent processes exporting the same gauge stay distinguishable.

    Shards are per-PID (metrics-YYYY-MM-DD-<pid>.jsonl). The gateway and an MCP
    daemon both export kirocrew.process.threads.os; timestamp-newest-wins
    across processes would display an arbitrary process as gateway state.
    """

    def shard(pid: int, ts: int, value: float) -> Path:
        metric = {
            "name": "kirocrew.process.threads.os",
            "data": {
                "data_points": [
                    {"attributes": {}, "value": value, "time_unix_nano": ts},
                ]
            },
        }
        p = tmp_path / f"metrics-2026-08-21-{pid}.jsonl"
        rm = {"resource_metrics": [{"scope_metrics": [{"metrics": [metric]}]}]}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rm) + "\n")
        return p

    gateway = shard(pid=100, ts=50, value=96.0)  # older sample, the gateway
    daemon = shard(pid=200, ts=99, value=8.0)  # newer sample, a small daemon

    result = _aggregate([gateway, daemon])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.threads.os"]
    assert rows and rows[0]["kind"] == "gauge"
    # Both processes keep their own newest sample, keyed by pid.
    assert rows[0]["by_attr"]["pid=100"] == 96.0
    assert rows[0]["by_attr"]["pid=200"] == 8.0
    # Headline is the newest process's reading (the daemon exported last).
    assert rows[0]["latest"] == 8.0


def test_single_pid_gauge_keeps_simple_shape(tmp_path: Path):
    """One process in the window: no pid= keys appear in by_attr."""
    metric = {
        "name": "kirocrew.process.open_fds",
        "data": {
            "data_points": [
                {"attributes": {}, "value": 144.0, "time_unix_nano": 7},
            ]
        },
    }
    p = tmp_path / "metrics-2026-08-21-4242.jsonl"
    rm = {"resource_metrics": [{"scope_metrics": [{"metrics": [metric]}]}]}
    p.write_text(json.dumps(rm) + "\n", encoding="utf-8")
    result = _aggregate([p])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.open_fds"]
    assert rows and rows[0]["latest"] == 144.0
    assert not any(k.startswith("pid=") for k in rows[0]["by_attr"])


def test_malformed_scalar_records_never_crash_aggregate(tmp_path: Path):
    """Garbage in one shard record degrades that point, never the endpoint.

    Shards are external input: a non-numeric timestamp sorts oldest, a
    non-numeric value skips only that data point, and well-formed records in
    the same shard still aggregate. An uncaught ValueError here is an HTTP 500
    for /api/telemetry/startup.
    """
    metric = {
        "name": "kirocrew.process.threads.os",
        "data": {
            "data_points": [
                {"attributes": {}, "value": 96.0, "time_unix_nano": "not-a-number"},
                {"attributes": {}, "value": "garbage", "time_unix_nano": 5},
                {"attributes": {}, "value": 97.0, "time_unix_nano": 9},
            ]
        },
    }
    result = _aggregate([_write_shard(tmp_path, [metric])])
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.threads.os"]
    assert rows and rows[0]["kind"] == "gauge"
    # ts=9 beats the ts=0-coerced garbage-timestamp point; the garbage-value
    # point is skipped entirely.
    assert rows[0]["latest"] == 97.0


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
    turn = {
        "name": "kirocrew.turn.duration",
        "data": {
            "data_points": [
                _turn_dp({"outcome": "ok"}, count=4),
                _turn_dp({"outcome": "error"}, count=1),  # terminal fault
                _turn_dp({"outcome": "timeout"}, count=1),  # terminal fault
                _turn_dp({"outcome": "unknown"}, count=1),  # legacy shard — terminal fault
                _turn_dp({"outcome": "tool_stall"}, count=3),  # watchdog recovery — NOT a fault
                _turn_dp({"outcome": "stale_recover"}, count=2),  # watchdog recovery — NOT a fault
            ]
        },
    }
    result = _aggregate([_write_shard(tmp_path, [turn])])

    total = 4 + 1 + 1 + 1 + 3 + 2  # = 12
    true_faults = 1 + 1 + 1  # error + timeout + unknown
    expected_rate = round(true_faults / total, 4)

    assert result["turn"]["outcome"] == {
        "ok": 4,
        "error": 1,
        "timeout": 1,
        "unknown": 1,
        "tool_stall": 3,
        "stale_recover": 2,
    }
    assert result["turn"]["fault_rate"] == expected_rate  # = 0.25

    # Ensure genuine error/timeout STILL count as faults (not accidentally
    # excluded by an overly aggressive allowlist).
    sub = tmp_path / "sub"
    sub.mkdir(exist_ok=True)
    error_only_turn = {
        "name": "kirocrew.turn.duration",
        "data": {
            "data_points": [
                _turn_dp({"outcome": "ok"}, count=3),
                _turn_dp({"outcome": "error"}, count=1),
            ]
        },
    }
    result2 = _aggregate([_write_shard(sub, [error_only_turn])])
    assert result2["turn"]["fault_rate"] == 0.25  # 1 error / 4 — unchanged


def test_fault_rate_counts_exhausted_stall_turns_as_faults(tmp_path: Path):
    """A stall_exhausted turn is a dead session and must reach fault_rate.

    The recovered-stall exclusion labels recovery turns tool_stall /
    stale_recover — but the final turn of a cycle whose budget dies with
    "start a new chat" labels stall_exhausted at the emit site, which the
    aggregator's allowlist counts as a terminal fault. Recovered stalls stay
    excluded; dead sessions count; fault_rate remains single-series."""
    turn = {
        "name": "kirocrew.turn.duration",
        "data": {
            "data_points": [
                _turn_dp({"outcome": "ok"}, count=5),
                _turn_dp({"outcome": "tool_stall"}, count=3),  # retries — NOT faults
                _turn_dp({"outcome": "stale_recover"}, count=1),  # recovered — NOT a fault
                _turn_dp({"outcome": "stall_exhausted"}, count=1),  # dead session — fault
            ]
        },
    }
    # The recovery counter is pure mechanism telemetry: it must NOT feed
    # fault_rate (the exhausted turn above already carries the fault).
    recovery = {
        "name": "kirocrew.watchdog.recovery.outcome",
        "data": {
            "data_points": [
                {
                    "attributes": {
                        "mechanism": "tool_stall",
                        "outcome": "exhausted",
                        "attempt_bucket": 3,
                    },
                    "value": 1,
                },
                {
                    "attributes": {
                        "mechanism": "stale_recover",
                        "outcome": "recovered",
                        "attempt_bucket": 1,
                    },
                    "value": 1,
                },
            ]
        },
    }
    result = _aggregate([_write_shard(tmp_path, [turn, recovery])])

    # 1 exhausted turn / 10 turns; recovery-counter points change nothing.
    assert result["turn"]["fault_rate"] == 0.1


def test_every_turn_outcome_label_is_classified_fault_or_excluded():
    """Cross-module drift gate between turn_outcome and the fault allowlist.

    ``metrics.turns.turn_outcome`` mints the labels; ``_TERMINAL_FAULT_OUTCOMES``
    (telemetry) decides which count toward fault_rate. They are hand-synced lists
    in different modules, and because the aggregator is an allowlist, a label
    added to the emitter but classified in neither set would silently fall out of
    the fault_rate numerator while still growing the denominator — an optimistic
    dashboard with no failing test. Labels are harvested from turn_outcome's
    return statements via AST so a new branch cannot dodge this gate.

    Harvested from ``metrics.turns`` rather than ``chat_runner``: the mapping
    moved there when the emit was widened to every dispatch surface, and
    ``chat_runner._turn_outcome`` is now a delegate whose source carries no label
    constants at all — pointed at it, this gate would harvest an empty set and
    pass no matter what the emitter did."""
    import ast
    import inspect

    from kiro_crew.dashboard.handlers.telemetry import _TERMINAL_FAULT_OUTCOMES
    from kiro_crew.metrics.turns import turn_outcome

    labels: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(turn_outcome))):
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
    # tracked under kirocrew.watchdog.recovery.outcome; "cancelled" is the
    # operator pressing Stop, so counting it would report a deliberate user
    # action as the system failing (it used to fold into "error" and did exactly
    # that); "unclassified" is a turn whose surface had no stop reason to give,
    # so calling it a fault would invent one for every clean background turn. Add
    # a new label here or to _TERMINAL_FAULT_OUTCOMES — never leave it
    # unclassified.
    excluded = {"ok", "tool_stall", "stale_recover", "cancelled", "unclassified"}
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
    h.add(_hist_dp({}, ns=2))  # newest generation
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
    h.add(_hist_dp({}, count=3, ns=30))  # reported
    h.add(_hist_dp({}, count=7, bounds=_OLD_BOUNDS, ns=20))  # dropped
    h.add(_hist_dp({}, count=5, bounds=[2, 4, 6, 8, 10], ns=10))  # dropped
    s = h.stats()
    assert s["count"] == 3
    assert s["other_generations"] == 2
    assert s["total_count"] == 15  # 3 reported + 7 + 5 dropped


def test_other_histograms_report_dropped_generations(tmp_path: Path):
    """Regression: the ``other`` surface used to omit other_generations."""
    acquire = {
        "name": "kirocrew.mcp.backend.acquire.duration",
        "data": {
            "data_points": [
                _hist_dp({"warm": True}, count=4, ns=20),
                _hist_dp({"warm": True}, count=7, bounds=_OLD_BOUNDS, ns=10),
            ]
        },
    }

    result = _aggregate([_write_shard(tmp_path, [acquire])])
    row = next(o for o in result["other"] if o["name"] == "kirocrew.mcp.backend.acquire.duration")

    assert row["count"] == 4, "newest generation only"
    assert row["other_generations"] == 1, "and it says so"
    assert row["total_count"] == 11, "with the full-window population"


def test_acquire_splits_expose_the_cold_side(tmp_path: Path):
    """The cold-spawn card is fed by the ``warm=false`` half of acquire."""
    acquire = {
        "name": "kirocrew.mcp.backend.acquire.duration",
        "data": {
            "data_points": [
                _hist_dp({"warm": True}, count=9, each_ms=15.0),
                _hist_dp({"warm": False}, count=2, bucket=4, each_ms=45.0),
            ]
        },
    }

    result = _aggregate([_write_shard(tmp_path, [acquire])])
    row = next(o for o in result["other"] if o["name"] == "kirocrew.mcp.backend.acquire.duration")

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
    req = {
        "name": "kirocrew.gateway.request.duration",
        "data": {
            "data_points": [
                _hist_dp({"method": "GET", "route": "/api/a"}),
                _hist_dp({"method": "POST", "route": "/api/b"}),
            ]
        },
    }
    skill = {
        "name": "kirocrew.skill.lazy_load.duration",
        "data": {"data_points": [_hist_dp({"transport": "stdio"})]},
    }

    result = _aggregate([_write_shard(tmp_path, [req, skill])])
    by_name = {o["name"]: o for o in result["other"]}

    assert "splits" not in by_name["kirocrew.gateway.request.duration"]
    assert "splits" not in by_name["kirocrew.skill.lazy_load.duration"]


def test_turn_and_startup_generation_count_comes_from_stats(tmp_path: Path):
    """Single source: the field arrives with the stats, not as a sibling."""
    turn = {
        "name": "kirocrew.turn.duration",
        "data": {
            "data_points": [
                _hist_dp({"outcome": "ok"}, count=3, ns=20),
                _hist_dp({"outcome": "ok"}, count=5, bounds=_OLD_BOUNDS, ns=10),
            ]
        },
    }
    ready = {"outcome": "ready", "backend": "kiro", "spawned": True, "phase": "total"}
    startup = {
        "name": "kirocrew.session.startup.duration",
        "data": {
            "data_points": [
                _hist_dp(ready, count=2, ns=20),
                _hist_dp(ready, count=6, bounds=_OLD_BOUNDS, ns=10),
            ]
        },
    }

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

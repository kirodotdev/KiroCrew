"""The Telemetry panel's selectable window: resolution, clamping and every reader.

One window drives every card on ``/api/telemetry/startup``, so the contract is
pinned in one place:

  * ``resolve_window`` turns ``days`` / ``since`` / ``until`` into an effective
    ``[start, end)`` and CLAMPS anything out of range instead of refusing it;
  * each reader (spend, context occupancy, OTEL shards, the per-turn
    drill-down) drops rows on both sides of a fixed range, not only before it;
  * the spend comparison is the equal-length period right before the window;
  * caches key on the window, so two ranges over the same files never share an
    answer, while a rolling window still keys on its length alone.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import telemetry as h
from kiro_crew.dashboard.handlers import usage as usage_mod

_DAY = 86400.0
_NOW = 1_800_000_000.0

# One instant for the tests' own expectations AND for every clock the readers
# under test consult (``time.time``, ``time.time_ns``, ``datetime.now`` in the
# telemetry and usage handlers), so no assertion depends on two clock reads
# landing on the same second or the same day.
_FROZEN = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp()
_FROZEN_DT = datetime.fromtimestamp(_FROZEN, timezone.utc)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime.fromtimestamp(_FROZEN, tz)


class _FrozenTime:
    """The ``time`` module with ``time()`` and ``time_ns()`` pinned to ``_FROZEN``."""

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    @staticmethod
    def time() -> float:
        return _FROZEN

    @staticmethod
    def time_ns() -> int:
        return int(_FROZEN) * 10**9


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod in (h, usage_mod):
        monkeypatch.setattr(mod, "time", _FrozenTime())
        monkeypatch.setattr(mod, "datetime", _FrozenDateTime)


pytestmark = pytest.mark.usefixtures("frozen_clock")


# --- resolve_window ----------------------------------------------------------


def test_no_parameters_is_a_rolling_default() -> None:
    w = usage_mod.resolve_window(default_days=7, now=_NOW)
    assert w == usage_mod.TimeWindow(_NOW - 7 * _DAY, _NOW, True)
    assert w.days == pytest.approx(7)
    assert w.until is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1.0),
        # A rolling window is never shorter than one day: the former minimum.
        ("0.5", 1.0),
        ("0.001", 1.0),
        ("1.5", 1.5),
        ("30", 30.0),
        ("9999", 90.0),
        # A finite length below the minimum clamps to it, as main's
        # ``max(1, days)`` did; only an unusable value takes the default.
        ("0", 1.0),
        ("-3", 1.0),
        ("-inf", 7.0),
        ("banana", 7.0),
        ("inf", 7.0),
        ("nan", 7.0),
    ],
)
def test_days_is_clamped_not_refused(raw: str, expected: float) -> None:
    w = usage_mod.resolve_window(days=raw, default_days=7, now=_NOW)
    assert w.rolling
    assert w.days == pytest.approx(expected)


def test_epoch_range_is_fixed() -> None:
    w = usage_mod.resolve_window(
        since=str(_NOW - 3 * _DAY), until=str(_NOW - _DAY), default_days=7, now=_NOW
    )
    assert w == usage_mod.TimeWindow(_NOW - 3 * _DAY, _NOW - _DAY, False)
    assert w.until == _NOW - _DAY


def test_a_future_end_is_capped_at_now() -> None:
    w = usage_mod.resolve_window(
        since=str(_NOW - _DAY), until=str(_NOW + 5 * _DAY), default_days=7, now=_NOW
    )
    assert (w.start, w.end) == (_NOW - _DAY, _NOW)


def test_an_inverted_range_is_swapped() -> None:
    w = usage_mod.resolve_window(
        since=str(_NOW - _DAY), until=str(_NOW - 3 * _DAY), default_days=7, now=_NOW
    )
    assert (w.start, w.end) == (_NOW - 3 * _DAY, _NOW - _DAY)


def test_a_start_past_the_ceiling_is_clamped() -> None:
    w = usage_mod.resolve_window(since="0", until=str(_NOW), default_days=7, now=_NOW)
    assert w.start == _NOW - usage_mod.MAX_WINDOW_DAYS * _DAY
    assert w.days == pytest.approx(usage_mod.MAX_WINDOW_DAYS)


def test_a_range_wholly_in_the_future_becomes_the_last_day() -> None:
    w = usage_mod.resolve_window(
        since=str(_NOW + _DAY), until=str(_NOW + 2 * _DAY), default_days=7, now=_NOW
    )
    assert (w.start, w.end) == (_NOW - _DAY, _NOW)


def test_a_range_wholly_past_the_ceiling_becomes_the_oldest_legal_day() -> None:
    w = usage_mod.resolve_window(since="10", until="20", default_days=7, now=_NOW)
    floor = _NOW - usage_mod.MAX_WINDOW_DAYS * _DAY
    assert (w.start, w.end) == (floor, floor + _DAY)


def test_since_alone_runs_to_now_and_until_alone_runs_back_the_default() -> None:
    w = usage_mod.resolve_window(since=str(_NOW - 2 * _DAY), default_days=7, now=_NOW)
    assert (w.start, w.end, w.rolling) == (_NOW - 2 * _DAY, _NOW, False)
    w = usage_mod.resolve_window(until=str(_NOW - _DAY), default_days=7, now=_NOW)
    assert (w.start, w.end) == (_NOW - 8 * _DAY, _NOW - _DAY)


def test_unparseable_bounds_fall_back_to_the_rolling_default() -> None:
    w = usage_mod.resolve_window(since="soon", until="later", default_days=7, now=_NOW)
    assert w == usage_mod.TimeWindow(_NOW - 7 * _DAY, _NOW, True)


# --- the token-row readers ---------------------------------------------------


def _row(age_days: float, *, slot: str = "chat-1-1700000000", credits: float = 1.0) -> dict:
    ts = _FROZEN_DT - timedelta(days=age_days)
    return {
        "_type": "tokens",
        "ts": ts.isoformat(),
        "slot": slot,
        "model": "m",
        "credits": credits,
        "context_used": 100_000,
        "context_window": 1_000_000,
    }


@pytest.fixture()
def rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A row store whose shard files are named by local day, as the writer names them."""
    shard_dir = tmp_path / "usage" / "tokens"
    shard_dir.mkdir(parents=True)
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    monkeypatch.setattr(usage_mod, "is_session_slot", lambda s: True)
    for name in ("_COST_CACHE", "_COST_CACHE_KEY", "_CONTEXT_CACHE", "_CONTEXT_CACHE_KEY"):
        monkeypatch.setattr(usage_mod, name, None)

    def _write(entries: list[dict]) -> None:
        for r in entries:
            day = usage_mod._parse_row_day(r["ts"])
            with (shard_dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(r) + "\n")

    return _write


def test_cost_breakdown_drops_rows_after_a_fixed_end(rows) -> None:
    rows([_row(0.1, credits=100.0), _row(2.5, credits=5.0), _row(4.5, credits=3.0)])
    until = _FROZEN - _DAY
    out = usage_mod.cost_breakdown(2, until)
    # Window: 3 days ago -> 1 day ago. Only the 2.5-day-old row is in it.
    assert out["credits"] == 5.0
    assert out["turns"] == 1
    # Prior: the 2 days before the window (5 -> 3 days ago).
    assert out["prior_credits"] == 3.0
    assert out["window_days"] == 2


def test_cost_breakdown_rolling_window_is_unchanged(rows) -> None:
    rows([_row(0.1, credits=100.0), _row(2.5, credits=5.0)])
    out = usage_mod.cost_breakdown(2)
    assert out["credits"] == 100.0
    assert out["prior_credits"] == 5.0


def test_cost_cache_keys_on_the_window(rows) -> None:
    rows([_row(0.1, credits=100.0), _row(2.5, credits=5.0)])
    rolling = usage_mod.cost_breakdown(2)
    fixed = usage_mod.cost_breakdown(2, _FROZEN - _DAY)
    assert rolling["credits"] == 100.0
    assert fixed["credits"] == 5.0


def test_context_occupancy_honours_both_ends(rows) -> None:
    rows([_row(0.1, slot="chat-1-1"), _row(2.5, slot="chat-2-2"), _row(9, slot="chat-3-3")])
    out = usage_mod.context_occupancy(3, _FROZEN - _DAY)
    assert [s["slot"] for s in out["sessions"]] == ["chat-2-2"]
    assert out["window_days"] == 3


def test_slot_turn_usage_honours_both_ends(rows) -> None:
    rows([_row(0.1, slot="s"), _row(2.5, slot="s"), _row(9, slot="s")])
    assert len(usage_mod.slot_turn_usage("s", 3, until=_FROZEN - _DAY)) == 1
    assert len(usage_mod.slot_turn_usage("s", 30)) == 3


def test_token_shards_after_a_fixed_end_are_not_opened(rows) -> None:
    rows([_row(0.1), _row(5)])
    picked = usage_mod._shards_in_window(2, _FROZEN - 4 * _DAY)
    assert [p.stem for p in picked] == [usage_mod._parse_row_day(_row(5)["ts"])]


@pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="shifting the local zone needs POSIX time.tzset"
)
def test_token_shard_window_start_across_a_dst_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window's first local day is found in epoch time, not with END's offset.

    The zone below starts DST on the second Sunday of March. A 2-day window that
    ends half an hour after midnight EDT two days later starts at 23:30 EST on
    the Saturday, so Saturday's shard holds in-range turns. Subtracting the days
    from the aware END keeps its -04:00 offset and lands at 00:30 on the Sunday,
    which would skip Saturday's shard.
    """
    shard_dir = tmp_path / "usage" / "tokens"
    shard_dir.mkdir(parents=True)
    for day in ("2026-03-06", "2026-03-07", "2026-03-08"):
        (shard_dir / f"{day}.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    try:
        with monkeypatch.context() as mp:
            mp.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
            time.tzset()
            until = datetime(2026, 3, 10, 4, 30, tzinfo=timezone.utc).timestamp()
            picked = sorted(p.stem for p in usage_mod._shards_in_window(2, until))
    finally:
        time.tzset()
    assert picked == ["2026-03-07", "2026-03-08"]


# --- the OTEL shard reader ---------------------------------------------------


def _counter_shard(directory: Path, points: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    day = _FROZEN_DT.strftime("%Y-%m-%d")
    line = {
        "resource_metrics": [
            {
                "scope_metrics": [
                    {
                        "metrics": [
                            {
                                "name": "kirocrew.zz.window_probe",
                                "data": {"aggregation_temporality": 1, "data_points": points},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    path = directory / f"metrics-{day}-77.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


def _probe_total(result: dict[str, Any]) -> float:
    (row,) = [o for o in result["other"] if o["name"] == "kirocrew.zz.window_probe"]
    return float(row["total"])


def test_aggregate_drops_points_outside_the_span(tmp_path: Path) -> None:
    now = _FROZEN
    shard = _counter_shard(
        tmp_path,
        [
            {"value": 1.0, "time_unix_nano": int((now - 60) * 1e9)},
            {"value": 10.0, "time_unix_nano": int((now - 3 * 3600) * 1e9)},
            {"value": 100.0},
        ],
    )
    # The last hour cuts today's shard: an untimed point may sit anywhere in
    # the day, so it is not counted.
    span = ((now - 3600) * 1e9, now * 1e9)
    assert _probe_total(h._aggregate([shard], span)) == 1.0
    assert _probe_total(h._aggregate([shard])) == 111.0


def test_aggregate_keeps_untimed_points_when_their_whole_day_is_inside(tmp_path: Path) -> None:
    now = _FROZEN
    day0 = (
        datetime.fromtimestamp(now, timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    shard = _counter_shard(
        tmp_path, [{"value": 1.0, "time_unix_nano": int((now - 1) * 1e9)}, {"value": 100.0}]
    )
    span = ((day0 - 3600) * 1e9, (now + 1) * 1e9)
    assert _probe_total(h._aggregate([shard], span)) == 101.0


def test_metric_shards_after_a_fixed_end_are_not_opened(tmp_path: Path) -> None:
    shard = _counter_shard(tmp_path, [{"value": 1.0}])
    assert h._shards_in_window(tmp_path, 1) == [shard]
    assert h._shards_in_window(tmp_path, 1, _FROZEN - 3 * _DAY) == []


def test_range_bounds_compare_in_whole_nanoseconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A point 1 ns either side of a bound lands on its own side of it.

    A 2026 epoch in nanoseconds (~1.79e18) is past float's 2**53 integer range,
    where neighbouring floats are 256 ns apart, so a float comparison folds a
    point 1 ns before the start onto the start (and counts it) and one 1 ns
    before the end onto the end (and drops it). The window runs over yesterday,
    UTC, through the route's own reader.
    """
    yesterday = _FROZEN_DT.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    start_s = int(yesterday.timestamp()) + 6 * 3600
    end_s = start_s + 6 * 3600
    s_ns, e_ns = start_s * 10**9, end_s * 10**9
    shard = _counter_shard(
        tmp_path,
        [
            {"value": 1.0, "time_unix_nano": s_ns - 1},
            {"value": 2.0, "time_unix_nano": s_ns},
            {"value": 4.0, "time_unix_nano": s_ns + 1},
            {"value": 8.0, "time_unix_nano": e_ns - 1},
            {"value": 16.0, "time_unix_nano": e_ns},
            {"value": 32.0, "time_unix_nano": str(e_ns + 1)},
        ],
    )
    shard.rename(tmp_path / f"metrics-{yesterday.strftime('%Y-%m-%d')}-77.jsonl")
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path))
    monkeypatch.setattr(h, "_CACHE", None)
    monkeypatch.setattr(h, "_CACHE_KEY", None)
    window = h.TimeWindow(float(start_s), float(end_s), False)
    # Inside [start, end): the point at the start, 1 ns after it, 1 ns before the end.
    assert _probe_total(h._parse_startup_metrics(window)) == 2.0 + 4.0 + 8.0


# --- the route --------------------------------------------------------------


def _state(directory: Path, retention_days: int = 0) -> h._TelemetryState:
    return h._TelemetryState(
        enabled=True,
        directory=directory,
        env_pinned=False,
        env_var=h.TELEMETRY_ENV_VAR,
        otlp_configured=False,
        retention_days=retention_days,
    )


def _body(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    assert isinstance(response.body, bytes)
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_route_with_no_window_keeps_mains_per_card_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``days``/``since``/``until`` answers exactly what main answers.

    Main summed spend over 7 days (``SPEND_WINDOW_DAYS``) and scanned the OTEL
    shards and the occupancy rows over 14 (``_WINDOW_DAYS``). A range picker
    must not move either default; only a pick does.
    """
    seen: dict[str, h.TimeWindow] = {}

    def _record(name: str, result: Any):
        def _fn(window: h.TimeWindow, **_kw: Any) -> Any:
            seen[name] = window
            return result

        return _fn

    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path))
    monkeypatch.setattr(
        h, "_parse_startup_metrics", _record("otel", {"other": [], "shard_count": 0})
    )
    monkeypatch.setattr(h, "_context_block", _record("context", None))
    monkeypatch.setattr(h, "_cost_block", _record("cost", None))
    request = make_mocked_request("GET", "/api/telemetry/startup", app=web.Application())
    payload = _body(await h.api_telemetry_startup(request))
    assert seen["otel"].rolling and seen["otel"].days == pytest.approx(14)
    assert seen["context"].days == pytest.approx(14)
    assert seen["cost"].rolling and seen["cost"].days == pytest.approx(7)
    assert payload["window_days"] == 14
    assert payload["window_default"] is True
    assert payload["cost_window_days"] == 7

    # Any pick, even one naming main's own spend week, drives every reader.
    seen.clear()
    request = make_mocked_request("GET", "/api/telemetry/startup?days=7", app=web.Application())
    payload = _body(await h.api_telemetry_startup(request))
    assert len(seen) == 3 and all(w.days == pytest.approx(7) for w in seen.values())
    assert payload["window_default"] is False
    assert payload["cost_window_days"] == 7


@pytest.mark.asyncio
async def test_route_with_no_window_reads_whole_shards_like_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No pick sums every point of the in-window shards, as main's reader did.

    Main picked the last 14 days of shard FILES and kept every point in them.
    The oldest such shard holds points a little more than 14x24h old, plus
    untimed points dated only by that boundary day; the default must still count
    them. A named 14-day window is a real range and drops them.
    """
    now = _FROZEN
    oldest_day = (_FROZEN_DT - timedelta(days=14)).strftime("%Y-%m-%d")
    shard = _counter_shard(
        tmp_path,
        [
            {"value": 1.0, "time_unix_nano": int((now - 60) * 1e9)},
            {"value": 10.0, "time_unix_nano": int((now - 14 * _DAY - 3600) * 1e9)},
            {"value": 100.0},
        ],
    )
    shard.rename(tmp_path / f"metrics-{oldest_day}-77.jsonl")
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path))
    monkeypatch.setattr(h, "_CACHE", None)
    monkeypatch.setattr(h, "_CACHE_KEY", None)
    monkeypatch.setattr(h, "_context_block", lambda _w: None)
    monkeypatch.setattr(h, "_cost_block", lambda _w: None)

    request = make_mocked_request("GET", "/api/telemetry/startup", app=web.Application())
    assert _probe_total(_body(await h.api_telemetry_startup(request))) == 111.0

    request = make_mocked_request("GET", "/api/telemetry/startup?days=14", app=web.Application())
    assert _probe_total(_body(await h.api_telemetry_startup(request))) == 1.0


@pytest.mark.asyncio
async def test_route_echoes_the_effective_window_to_every_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[h.TimeWindow] = []

    def _record(result: Any):
        def _fn(window: h.TimeWindow, **_kw: Any) -> Any:
            seen.append(window)
            return result

        return _fn

    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path, retention_days=10))
    monkeypatch.setattr(h, "_parse_startup_metrics", _record({"other": [], "shard_count": 0}))
    monkeypatch.setattr(h, "_context_block", _record(None))
    monkeypatch.setattr(h, "_cost_block", _record(None))
    until = _FROZEN - _DAY
    since = until - 3 * _DAY
    request = make_mocked_request(
        "GET",
        f"/api/telemetry/startup?since={since}&until={until}",
        app=web.Application(),
    )
    payload = _body(await h.api_telemetry_startup(request))
    assert payload["window_days"] == 3
    assert payload["window_start"] == h._iso_utc(since)
    assert payload["window_end"] == h._iso_utc(until)
    assert payload["window_end"].endswith("Z")
    assert payload["window_rolling"] is False
    assert payload["metrics_retention_days"] == 10
    # One window, handed to all three readers.
    assert len(seen) == 3 and len(set(seen)) == 1
    assert seen[0].until == until


@pytest.mark.asyncio
async def test_route_clamps_an_over_wide_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path))
    monkeypatch.setattr(h, "_parse_startup_metrics", lambda _w, **_kw: {"other": []})
    monkeypatch.setattr(h, "_context_block", lambda _w: None)
    monkeypatch.setattr(h, "_cost_block", lambda _w: None)
    request = make_mocked_request("GET", "/api/telemetry/startup?days=400", app=web.Application())
    payload = _body(await h.api_telemetry_startup(request))
    assert payload["window_days"] == usage_mod.MAX_WINDOW_DAYS


@pytest.mark.asyncio
async def test_usage_turns_reads_the_same_range(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[float, float | None]] = []

    def _turns(slot: str, days: float, *, app: str | None, until: float | None) -> list:
        seen.append((days, until))
        return []

    monkeypatch.setattr(h, "slot_turn_usage", _turns)
    until = _FROZEN - _DAY
    request = make_mocked_request(
        "GET",
        f"/api/usage/turns?slot=chat-1-1&since={until - 2 * _DAY}&until={until}",
        app=web.Application(),
    )
    payload = _body(await h.api_usage_turns(request))
    assert payload["days"] == 2
    # Only the length is echoed; nothing reads start/end bounds off this route.
    assert set(payload) == {"slot", "days", "turns"}
    assert seen == [(pytest.approx(2.0), until)]

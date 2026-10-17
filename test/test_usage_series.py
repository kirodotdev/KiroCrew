"""Tests for kiro_crew.dashboard.handlers.usage_series (the Usage tab's stacked series)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.dashboard.handlers.usage_series as series_mod
import kiro_crew.sel as sel_mod
from kiro_crew.dashboard.handlers.usage_series import (
    OTHER_KEY,
    UNATTRIBUTED_KEY,
    _Row,
    api_usage_series,
    build_series,
    load_rows,
)
from kiro_crew.testing.clock import ManualClock

#: The one instant the shard-reading tests run at, as a LOCAL wall time (naive).
#: The product keys everything on local days -- shard names, the day axis, the
#: cohort week -- so pinning the wall time rather than an epoch makes the frozen
#: clock read Friday noon in every zone a runner can be in, and the expectations
#: below derive from this same constant instead of a second clock read (D5).
FROZEN_LOCAL = datetime(2026, 10, 2, 12, 0)
TODAY = FROZEN_LOCAL.date()  # a Friday, so its ISO week opens on the Monday four days earlier
MONDAY = (TODAY - timedelta(days=TODAY.isoweekday() - 1)).isoformat()


def _row(
    day: str,
    credits: float = 1.0,
    *,
    slot: str = "chat-1-1700000000",
    channel: str = "dashboard",
    agent: str = "kirocrew",
    model: str = "m",
    hour: int = 12,
) -> _Row:
    ts = datetime.fromisoformat(f"{day}T{hour:02d}:00:00").astimezone().timestamp()
    return _Row(
        day=day,
        ts=ts,
        slot=slot,
        channel=channel,
        agent=agent,
        model=model,
        credits=credits,
    )


def _by_key(payload: dict) -> dict[str, dict]:
    return {s["key"]: s for s in payload["series"]}


class TestBuildSeries:
    def test_dense_day_axis_is_zero_filled(self):
        rows = [_row("2026-10-01", 2.5), _row("2026-10-02", 1.5)]

        out = build_series(rows, by="channel", days=5, today=TODAY)

        assert out["dates"] == [
            "2026-09-28",
            "2026-09-29",
            "2026-09-30",
            "2026-10-01",
            "2026-10-02",
        ]
        assert out["series"] == [
            {"key": "dashboard", "kind": "bucket", "values": [0, 0, 0, 2.5, 1.5], "total": 4.0}
        ]
        assert out["total"] == 4.0
        assert out["truncated"] is False
        assert out["dropped_rows"] == 0
        assert out["complete_from"] is None

    def test_rows_outside_the_window_are_ignored(self):
        rows = [_row("2026-09-01", 99.0), _row("2026-10-02", 1.0)]

        out = build_series(rows, by="channel", days=7, today=TODAY)

        assert out["total"] == 1.0

    def test_value_dimensions_stack_largest_first(self):
        rows = [
            _row("2026-10-02", 1.0, channel="background"),
            _row("2026-10-02", 5.0, channel="cron"),
            _row("2026-10-01", 3.0, channel="dashboard"),
        ]

        out = build_series(rows, by="channel", days=2, today=TODAY)

        assert [s["key"] for s in out["series"]] == ["cron", "dashboard", "background"]

    def test_top_n_folds_the_remainder_into_other(self):
        rows = [_row("2026-10-02", float(n), agent=f"agent-{n}") for n in range(1, 6)]

        out = build_series(rows, by="agent", days=1, top=2, today=TODAY)

        keys = [s["key"] for s in out["series"]]
        assert keys == ["agent-5", "agent-4", OTHER_KEY]
        other = _by_key(out)[OTHER_KEY]
        assert other == {
            "key": OTHER_KEY,
            "kind": "other",
            "values": [6.0],
            "total": 6.0,
            "members": 3,
        }
        # Folding loses nothing: the stack's top edge is still the day's spend.
        assert out["total"] == 15.0

    def test_empty_dimension_value_is_an_explicit_unattributed_series(self):
        rows = [_row("2026-10-02", 2.0, model=""), _row("2026-10-02", 3.0, model="opus")]

        out = build_series(rows, by="model", days=1, today=TODAY)

        assert [s["key"] for s in out["series"]] == ["opus", UNATTRIBUTED_KEY]
        assert _by_key(out)[UNATTRIBUTED_KEY]["total"] == 2.0
        assert out["total"] == 5.0

    def test_unattributed_and_other_are_omitted_when_empty(self):
        out = build_series([_row("2026-10-02", 1.0)], by="channel", days=1, today=TODAY)

        assert [s["kind"] for s in out["series"]] == ["bucket"]

    def test_zero_valued_rows_do_not_create_buckets(self):
        rows = [
            _row("2026-10-02", 0.0, channel="cron"),
            _row("2026-10-02", 1.0, channel="dashboard"),
        ]

        out = build_series(rows, by="channel", days=1, today=TODAY)

        assert [s["key"] for s in out["series"]] == ["dashboard"]

    def test_a_row_that_would_overflow_a_day_is_dropped_not_served_as_infinity(self):
        rows = [_row("2026-10-02", 1.0), _row("2026-10-02", 1.7e308), _row("2026-10-02", 1.7e308)]

        out = build_series(rows, by="channel", days=1, today=TODAY)

        assert out["total"] == 1.0 + 1.7e308
        assert json.dumps(out, allow_nan=False)

    def test_folded_and_total_sums_cannot_overflow_to_infinity_either(self):
        rows = [
            _row("2026-10-01", 1.7e308, channel="kept"),
            _row("2026-10-02", 1.0e308, channel="folded-a"),
            _row("2026-10-02", 1.0e308, channel="folded-b"),
        ]

        out = build_series(rows, by="channel", days=2, top=1, today=TODAY)

        assert _by_key(out)["kept"]["total"] == 1.7e308
        assert _by_key(out)["__other__"]["values"] == [0.0, 1.0e308]
        assert out["total"] == 1.7e308
        assert json.dumps(out, allow_nan=False)

    def test_cohort_is_the_sessions_first_seen_iso_week_in_stack_order_oldest_first(self):
        rows = [
            # s-old starts Tue 09-15 (week of Mon 09-14) and keeps spending into October:
            # every one of its rows belongs to the 09-14 cohort, not the week it was spent.
            _row("2026-09-15", 1.0, slot="s-old"),
            _row("2026-10-02", 4.0, slot="s-old"),
            # s-new starts Thu 10-01 (week of Mon 09-28).
            _row("2026-10-01", 2.0, slot="s-new"),
            _row("2026-10-02", 8.0, slot="s-new"),
        ]

        out = build_series(rows, by="cohort", days=30, today=TODAY)

        assert [s["key"] for s in out["series"]] == ["2026-09-14", "2026-09-28"]
        cohorts = _by_key(out)
        assert cohorts["2026-09-14"]["total"] == 5.0
        assert cohorts["2026-09-28"]["total"] == 10.0
        # Oldest first even though the newer cohort is the bigger one.
        assert cohorts["2026-09-14"]["total"] < cohorts["2026-09-28"]["total"]

    def test_cohort_first_seen_is_judged_inside_the_window_only(self):
        rows = [_row("2026-09-01", 1.0, slot="s"), _row("2026-10-02", 1.0, slot="s")]

        out = build_series(rows, by="cohort", days=7, today=TODAY)

        # The September row is outside a 7-day window, so the session's first
        # appearance inside the window is the October row: week of Mon 09-28.
        assert [s["key"] for s in out["series"]] == ["2026-09-28"]

    def test_cohort_row_without_a_slot_is_unattributed(self):
        rows = [_row("2026-10-02", 1.0, slot=""), _row("2026-10-02", 2.0, slot="s")]

        out = build_series(rows, by="cohort", days=1, today=TODAY)

        assert [s["key"] for s in out["series"]] == ["2026-09-28", UNATTRIBUTED_KEY]

    def test_dropped_count_is_disclosed_as_truncation(self):
        out = build_series(
            [_row("2026-10-02", 1.0)],
            by="channel",
            days=1,
            dropped=3,
            complete_from="2026-10-02",
            today=TODAY,
        )

        assert out["truncated"] is True
        assert out["dropped_rows"] == 3
        assert out["complete_from"] == "2026-10-02"

    def test_complete_from_is_withheld_when_nothing_was_dropped(self):
        out = build_series(
            [_row("2026-10-02", 1.0)], by="channel", days=1, complete_from="2026-10-02", today=TODAY
        )

        assert (out["truncated"], out["complete_from"]) == (False, None)


def _freeze(monkeypatch) -> ManualClock:
    """Freeze the clocks the shard window, the day axis and the scan cache read.

    One clock on both modules: ``usage`` picks the shards by their date and
    ``usage_series`` builds the axis and stamps the cache, and the two must agree
    on what day it is. ``FROZEN_LOCAL.timestamp()`` is read in the process's zone,
    so the installed ``datetime.now()`` answers that wall time whatever the zone.
    """
    clock = ManualClock(start=FROZEN_LOCAL.timestamp())
    clock.install(monkeypatch, usage_mod, time=False, datetime=True)
    clock.install(monkeypatch, series_mod, time=True, datetime=True)
    return clock


def _patch_shards(monkeypatch, tmp_path):
    _freeze(monkeypatch)
    shard_dir = tmp_path / "tokens"
    shard_dir.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    monkeypatch.setattr(series_mod, "_SCAN_CACHE", None)
    monkeypatch.setattr(series_mod, "_SCAN_CACHE_KEY", None)
    monkeypatch.setattr(series_mod, "_SCAN_CACHE_TS", 0.0)
    return shard_dir


def _write_shard(shard_dir, day: str, records: list[dict]) -> None:
    (shard_dir / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _stamp(offset: timedelta = timedelta(0)) -> str:
    """A row ``ts`` at ``FROZEN_LOCAL + offset``, in the aware form the writer stores."""
    return (FROZEN_LOCAL + offset).astimezone().isoformat()


def _day(offset: timedelta = timedelta(0)) -> str:
    """The local day of ``FROZEN_LOCAL + offset``: a shard name and an axis entry."""
    return (TODAY + offset).isoformat()


def _record(ts: str, **fields) -> dict:
    base = {
        "_type": "tokens",
        "ts": ts,
        "slot": "chat-1-1700000000",
        "provider": "acp",
        "model": "m",
        "surface": "dashboard",
        "agent": "kirocrew",
        "input": 0,
        "output": 0,
        "cache_create": 0,
        "cache_read": 0,
        "credits": 1.0,
    }
    base.update(fields)
    return base


class TestLoadRows:
    def test_reads_tokens_rows_and_skips_the_rest(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(
            shard_dir,
            _day(),
            [
                _record(_stamp(), credits=2.5, input=10, output=5, cache_create=1, cache_read=4),
                {"_type": "context", "ts": _stamp(), "slot": "chat-1"},
                {"_type": "tokens", "ts": "not a timestamp", "credits": 99},
                {"_type": "tokens", "ts": _stamp(), "credits": "nan", "slot": "chat-2"},
            ],
        )
        (shard_dir / f"{_day()}.jsonl").open("a", encoding="utf-8").write("{not json\n")

        scan = load_rows()

        assert [(r.slot, r.credits) for r in scan.rows] == [
            ("chat-1-1700000000", 2.5),
            ("chat-2", 0.0),
        ]
        assert scan.dropped == 0

    def test_channel_comes_from_the_session_key_not_the_surface_field(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(
            shard_dir,
            _day(),
            [
                # A historical row can carry a wrong but non-empty surface; the key wins.
                _record(_stamp(), slot="telegram:1234", surface="dashboard"),
                _record(_stamp(), slot="cron:nightly", surface="dashboard"),
                _record(_stamp(), slot="chat-7-1700000000", surface=""),
                # No key: nothing to derive from, so the row stays unattributed
                # rather than taking the classifier's "unknown".
                _record(_stamp(), slot="", surface="dashboard"),
            ],
        )

        scan = load_rows()

        assert [r.channel for r in scan.rows] == ["telegram", "cron", "dashboard", ""]

    def test_retained_strings_are_clipped_at_the_field_bound(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        long = "a" * (series_mod.MAX_FIELD_CHARS + 50)
        _write_shard(shard_dir, _day(), [_record(_stamp(), slot=long, agent=long, model=long)])

        (row,) = load_rows().rows

        assert len(row.slot) == len(row.agent) == len(row.model) == series_mod.MAX_FIELD_CHARS

    def test_row_cap_keeps_the_newest_rows_and_counts_the_rest(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(series_mod, "MAX_ROWS", 3)
        yesterday = timedelta(days=-1)
        _write_shard(
            shard_dir,
            _day(yesterday),
            [_record(_stamp(yesterday), credits=float(c)) for c in (1, 2, 3)],
        )
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=float(c)) for c in (4, 5)])

        scan = load_rows()

        # Today's rows whole, then the newest of yesterday's; the two oldest counted.
        assert sorted(r.credits for r in scan.rows) == [3.0, 4.0, 5.0]
        assert scan.dropped == 2
        # Yesterday lost rows, so today is the first day the chart shows whole.
        assert scan.complete_from == _day()
        out = build_series(
            scan.rows, by="channel", dropped=scan.dropped, complete_from=scan.complete_from
        )
        assert (out["truncated"], out["dropped_rows"], out["complete_from"]) == (True, 2, _day())

    def test_complete_from_is_the_oldest_whole_day_even_across_a_day_with_no_shard(
        self, tmp_path, monkeypatch
    ):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(series_mod, "MAX_ROWS", 3)
        three_back, two_back = timedelta(days=-3), timedelta(days=-2)
        _write_shard(shard_dir, _day(three_back), [_record(_stamp(three_back))] * 2)
        _write_shard(shard_dir, _day(two_back), [_record(_stamp(two_back))] * 2)
        _write_shard(shard_dir, _day(), [_record(_stamp())])

        scan = load_rows()

        # Today (1) and two days back (2) fill the cap; three days back is refused
        # entirely. Yesterday had no shard, which leaves it complete, so the whole
        # stretch from two days back is intact.
        assert (len(scan.rows), scan.dropped) == (3, 2)
        assert scan.complete_from == _day(two_back)

    def test_complete_from_stops_at_an_unreadable_shard(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(series_mod, "MAX_ROWS", 2)
        three_back, two_back, yesterday = timedelta(days=-3), timedelta(days=-2), timedelta(days=-1)
        _write_shard(shard_dir, _day(three_back), [_record(_stamp(three_back))] * 2)
        _write_shard(shard_dir, _day(two_back), [_record(_stamp(two_back))])
        _write_shard(shard_dir, _day(yesterday), [_record(_stamp(yesterday))])
        _write_shard(shard_dir, _day(), [_record(_stamp())])
        real_shard_rows = series_mod._shard_rows

        def unreadable_yesterday(path, budget):
            if path.stem == _day(yesterday):
                raise OSError("simulated unreadable shard")
            return real_shard_rows(path, budget)

        monkeypatch.setattr(series_mod, "_shard_rows", unreadable_yesterday)

        scan = load_rows()

        # Yesterday's rows are lost, so "complete" cannot reach past it even
        # though the day before it was read whole; three days back is refused.
        assert (len(scan.rows), scan.dropped) == (2, 2)
        assert scan.complete_from == _day()

    def test_complete_from_is_none_when_the_cap_trips_inside_the_newest_shard(
        self, tmp_path, monkeypatch
    ):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(series_mod, "MAX_ROWS", 1)
        _write_shard(shard_dir, _day(), [_record(_stamp())] * 2)

        scan = load_rows()
        out = build_series(
            scan.rows, by="channel", dropped=scan.dropped, complete_from=scan.complete_from
        )

        assert (len(scan.rows), scan.dropped, scan.complete_from) == (1, 1, None)
        assert (out["truncated"], out["complete_from"]) == (True, None)

    def test_a_shard_wider_than_the_budget_is_never_held_whole(self, tmp_path, monkeypatch):
        """The count bound applies while a shard streams past, not once it is parsed.

        Rows are counted as they are built and released, so the peak is the budget
        plus the one row in hand when the oldest is let go; a reader that parsed
        the file first would peak at the file's whole row count.
        """
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        live = peak = 0

        class _CountedRow(_Row):
            def __init__(self, *args, **kwargs):
                nonlocal live, peak
                super().__init__(*args, **kwargs)
                live += 1
                peak = max(peak, live)

            def __del__(self):
                nonlocal live
                live -= 1

        monkeypatch.setattr(series_mod, "_Row", _CountedRow)
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=float(c)) for c in range(1, 11)])

        kept, refused = series_mod._shard_rows(shard_dir / f"{_day()}.jsonl", 3)

        assert ([r.credits for r in kept], refused) == ([8.0, 9.0, 10.0], 7)
        assert peak == 4, f"{peak} rows were live at once for a budget of 3"

    def test_row_cap_is_silent_when_the_window_fits(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(series_mod, "MAX_ROWS", 2)
        _write_shard(shard_dir, _day(), [_record(_stamp()) for _ in range(2)])

        scan = load_rows()

        assert (len(scan.rows), scan.dropped) == (2, 0)

    def test_the_shard_before_the_axis_opens_neither_spends_the_budget_nor_counts_as_refused(
        self, tmp_path, monkeypatch
    ):
        """The shard window's inclusive cutoff yields one shard older than the chart.

        Its rows are never drawn, so a cap that trips on them would disclose a
        shortened window while every charted day is whole.
        """
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(series_mod, "MAX_ROWS", 2)
        before_axis = timedelta(days=-series_mod.WINDOW_DAYS)
        _write_shard(shard_dir, _day(before_axis), [_record(_stamp(before_axis)) for _ in range(3)])
        _write_shard(shard_dir, _day(), [_record(_stamp()) for _ in range(2)])

        scan = load_rows()
        out = build_series(scan.rows, by="channel", dropped=scan.dropped, today=TODAY)

        assert (len(scan.rows), scan.dropped) == (2, 0)
        assert all(r.day >= out["dates"][0] for r in scan.rows)
        assert (out["truncated"], out["dropped_rows"]) == (False, 0)

    def test_a_credits_figure_wider_than_a_double_counts_zero_instead_of_failing_the_read(
        self, tmp_path, monkeypatch
    ):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=10**400)])

        scan = load_rows()
        out = build_series(scan.rows, by="channel", days=1)

        assert [r.credits for r in scan.rows] == [0.0]
        assert out["total"] == 0.0
        assert json.dumps(out, allow_nan=False)

    def test_rows_are_cached_until_a_shard_changes(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=1.0)])

        first = load_rows()
        assert len(first.rows) == 1
        # An append changes the shard's size, so the fingerprint misses.
        with (shard_dir / f"{_day()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_record(_stamp(), credits=2.0)) + "\n")
        second = load_rows()

        assert len(second.rows) == 2
        assert load_rows() is second

    def test_missing_directory_yields_no_rows(self, tmp_path, monkeypatch):
        _patch_shards(monkeypatch, tmp_path)
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path / "absent")

        assert load_rows().rows == []


class TestApiUsageSeries:
    async def _get(self, query: str, app_name: str = ""):
        app = web.Application()

        @web.middleware
        async def stamp_app(request, handler):
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(stamp_app)
        app.router.add_get("/api/usage/series", api_usage_series)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/usage/series" + query)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_defaults_to_channel_and_credits_over_the_whole_window(
        self, tmp_path, monkeypatch
    ):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=3.0, slot="cron:tick")])

        status, body = await self._get("")

        assert status == 200
        assert len(body["dates"]) == series_mod.WINDOW_DAYS
        assert body["dates"][-1] == _day()
        assert [s["key"] for s in body["series"]] == ["cron"]
        assert body["total"] == 3.0
        assert (body["truncated"], body["dropped_rows"], body["complete_from"]) == (False, 0, None)

    @pytest.mark.asyncio
    async def test_unknown_dimension_is_a_400(self, tmp_path, monkeypatch):
        _patch_shards(monkeypatch, tmp_path)

        status, body = await self._get("?by=slot")

        assert (status, body["code"]) == (400, "invalid_dimension")

    @pytest.mark.asyncio
    async def test_cohort_dimension_over_the_wire(self, tmp_path, monkeypatch):
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=1.0)])

        status, body = await self._get("?by=cohort")

        assert status == 200
        assert [s["key"] for s in body["series"]] == [MONDAY]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("zone", ["UTC", "Pacific/Kiritimati", "America/St_Johns"])
    async def test_every_reader_agrees_on_the_local_day_whatever_the_zone(
        self, tmp_path, monkeypatch, local_tz, zone
    ):
        """The shard window, the day axis and the cohort week read one LOCAL day.

        Each reader derives its day from a stamp carrying the zone's offset, so a
        reader that fell back to the UTC day would put a Kiritimati noon on the
        day before. Pinned under UTC, a zone a calendar day ahead of it for most
        of every day, and a half-hour offset (D5).
        """
        local_tz(zone)
        shard_dir = _patch_shards(monkeypatch, tmp_path)
        _write_shard(shard_dir, _day(), [_record(_stamp(), credits=1.0)])

        status, body = await self._get("?by=cohort")

        assert status == 200
        assert body["dates"][-1] == _day()
        assert [s["key"] for s in body["series"]] == [MONDAY]
        assert body["total"] == 1.0

    @pytest.mark.asyncio
    async def test_app_token_is_refused_as_not_found_and_the_refusal_is_audited(
        self, tmp_path, monkeypatch
    ):
        _patch_shards(monkeypatch, tmp_path)
        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())

        status, body = await self._get("", app_name="some-app")

        assert (status, body["code"]) == (404, "not_found")
        assert audits == [
            {
                "caller": "some-app",
                "operation": "usage_series",
                "outcome": "denied",
                "source": "app_isolation",
                "error": "dashboard-only endpoint",
            }
        ]

"""``kirocrew usage``: the terminal spend report over the series rows.

``usage_series.build_report`` aggregates the rows ``usage_series.load_rows`` reads
(the "Spend over time" chart's population: the whole install, subagent turns
included) and ``cli_commands._usage_report`` prints them. Shards are seeded into a
tmp dir through the usage facade's ``_TOKEN_USAGE_DIR``, never the real data home,
and every clock the window reads is frozen at one local wall time.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.cli_commands_usage as cli_usage_mod
import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.dashboard.handlers.usage_series as series_mod
import kiro_crew.dashboard.handlers.usage_spend as spend_mod
from kiro_crew import cli_commands, cli_parser_gateway
from kiro_crew.testing.clock import ManualClock

#: The one LOCAL wall time every test runs at; days below derive from it.
FROZEN_LOCAL = datetime(2026, 11, 14, 12, 0)
TODAY = FROZEN_LOCAL.date()
UNATTRIBUTED = series_mod.UNATTRIBUTED_KEY


def _day(back: int) -> str:
    return (TODAY - timedelta(days=back)).isoformat()


def _record(back: int, **fields) -> dict:
    base = {
        "_type": "tokens",
        "ts": (FROZEN_LOCAL - timedelta(days=back)).astimezone().isoformat(),
        "slot": "chat-1-1700000000",
        "provider": "acp",
        "model": "model-a",
        "agent": "agent-a",
        "input": 0,
        "output": 0,
        "cache_create": 0,
        "cache_read": 0,
        "credits": 1.0,
        "cost": 0.0,
    }
    base.update(fields)
    return base


@pytest.fixture
def shards(tmp_path, monkeypatch):
    """Seed shards by day offset: ``shards({0: [record, ...], 2: [...]})``."""
    clock = ManualClock(start=FROZEN_LOCAL.timestamp())
    clock.install(monkeypatch, usage_mod, time=False, datetime=True)
    clock.install(monkeypatch, series_mod, time=True, datetime=True)
    clock.install(monkeypatch, spend_mod, time=True, datetime=False)
    clock.install(monkeypatch, cli_usage_mod, time=False, datetime=True)
    shard_dir = tmp_path / "tokens"
    shard_dir.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    monkeypatch.setattr(series_mod, "_SCAN_CACHE", None)
    monkeypatch.setattr(series_mod, "_SCAN_CACHE_KEY", None)
    monkeypatch.setattr(series_mod, "_SCAN_CACHE_TS", 0.0)

    def seed(by_day: dict[int, list[dict]]) -> None:
        for back, records in by_day.items():
            (shard_dir / f"{_day(back)}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
            )

    return seed


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kirocrew")
    cli_parser_gateway.register_usage_parser(root.add_subparsers(dest="command"))
    return root


def _run(capsys, *argv: str) -> str:
    cli_commands._usage_report(_parser().parse_args(["usage", *argv]))
    return capsys.readouterr().out


def _report(capsys, *argv: str) -> dict:
    out = _run(capsys, *argv, "--json")
    return json.loads(out)


def _rows(report: dict) -> dict[str, dict]:
    return {row["key"]: row for row in report["rows"]}


def test_per_day_credits_match_daily_credits_on_a_dense_axis(shards, capsys):
    shards(
        {
            0: [_record(0, credits=1.25), _record(0, credits=0.5, slot="cron:tick")],
            2: [_record(2, credits=3.0)],
        }
    )

    report = _report(capsys, "--days", "5")

    assert [row["key"] for row in report["rows"]] == [_day(n) for n in (4, 3, 2, 1, 0)]
    expected = usage_mod.daily_credits()
    assert {k: r["credits"] for k, r in _rows(report).items()} == {
        _day(n): expected.get(_day(n), 0.0) for n in range(5)
    }
    assert (report["from"], report["to"], report["days"]) == (_day(4), _day(0), 5)
    assert report["totals"]["credits"] == 4.75


def test_by_agent_splits_the_total_and_a_no_agent_row_is_unattributed(shards, capsys):
    shards(
        {
            0: [_record(0, credits=2.0), _record(0, credits=1.0, agent="")],
            1: [_record(1, credits=4.0, agent="agent-b")],
        }
    )

    report = _report(capsys, "--by", "agent")

    assert [(r["key"], r["credits"]) for r in report["rows"]] == [
        ("agent-b", 4.0),
        ("agent-a", 2.0),
        (UNATTRIBUTED, 1.0),
    ]
    assert report["totals"]["credits"] == 7.0
    assert "(unattributed)" in _run(capsys, "--by", "agent")


def test_model_channel_and_session_bucket_by_their_field(shards, capsys):
    shards(
        {
            0: [
                _record(0, credits=1.0, model="model-a", slot="chat-1-1700000000"),
                _record(0, credits=2.0, model="model-b", slot="cron:nightly"),
                _record(0, credits=0.5, slot="subagent:one"),
                _record(0, credits=0.25, slot="subagent:two"),
            ]
        }
    )

    by_model = _rows(_report(capsys, "--by", "model"))
    by_channel = _rows(_report(capsys, "--by", "channel"))
    by_session = _report(capsys, "--by", "session")

    assert {k: r["credits"] for k, r in by_model.items()} == {"model-a": 1.75, "model-b": 2.0}
    assert {k: r["credits"] for k, r in by_channel.items()} == {
        "cron": 2.0,
        "dashboard": 1.0,
        "subagent": 0.75,
    }
    assert {r["key"]: (r["credits"], r["turns"]) for r in by_session["rows"]} == {
        "cron:nightly": (2.0, 1),
        "chat-1-1700000000": (1.0, 1),
        "subagent": (0.75, 2),
    }
    assert by_session["totals"]["credits"] == 3.75


def test_a_zero_credit_turn_still_counts_a_turn_and_its_tokens(shards, capsys):
    shards({0: [_record(0, credits=1.0), _record(0, credits=0.0, input=40, output=2)]})

    report = _report(capsys, "--days", "1")

    assert report["rows"] == [
        {"key": _day(0), "credits": 1.0, "cost": None, "turns": 2, "tokens": 42}
    ]


def test_tokens_sum_four_parts_and_an_unusable_part_counts_zero(shards, capsys):
    shards(
        {
            0: [
                _record(0, input=10, output=5, cache_create=3, cache_read=2),
                _record(0, input="12", output=float("nan"), cache_create=None, cache_read=7),
                _record(0, input=True, output=1),
            ]
        }
    )

    report = _report(capsys, "--days", "1")

    assert [r.tokens for r in series_mod.load_rows().rows] == [20, 7, 1]
    assert report["totals"]["tokens"] == 28


class TestNotReportedCells:
    def test_a_credits_only_store_reports_no_cost_or_tokens(self, shards, capsys):
        shards({0: [_record(0, credits=2.0)], 1: [_record(1, credits=1.0, agent="agent-b")]})

        report = _report(capsys, "--days", "3")
        table = _run(capsys, "--days", "3")

        cells = [*report["rows"], report["totals"]]
        assert all(c["cost"] is None and c["tokens"] is None for c in cells)
        assert "Cost" not in table and "Tokens" not in table
        assert "Credits" in table and "Turns" in table

    def test_a_dollar_store_reports_no_credits_on_its_spend_cells(self, shards, capsys):
        shards({0: [_record(0, credits=0.0, cost=0.4, input=100)]})

        rows = _rows(_report(capsys, "--days", "2"))

        assert rows[_day(0)] == {
            "key": _day(0),
            "credits": None,
            "cost": 0.4,
            "turns": 1,
            "tokens": 100,
        }
        assert rows[_day(1)] == {
            "key": _day(1),
            "credits": 0.0,
            "cost": None,
            "turns": 0,
            "tokens": None,
        }

    def test_a_mixed_store_nulls_each_cell_where_its_own_rows_report_nothing(self, shards, capsys):
        shards(
            {
                0: [_record(0, credits=2.0, agent="agent-k")],
                1: [_record(1, credits=0.0, cost=0.3, input=50, agent="agent-d")],
            }
        )

        by_day = _report(capsys, "--days", "2")
        by_agent = _report(capsys, "--days", "2", "--by", "agent")
        table = _run(capsys, "--days", "2", "--by", "agent")

        days = _rows(by_day)
        assert (days[_day(0)]["cost"], days[_day(0)]["tokens"]) == (None, None)
        assert days[_day(1)]["credits"] is None
        agents = _rows(by_agent)
        assert agents["agent-k"] == {
            "key": "agent-k",
            "credits": 2.0,
            "cost": None,
            "turns": 1,
            "tokens": None,
        }
        assert agents["agent-d"] == {
            "key": "agent-d",
            "credits": None,
            "cost": 0.3,
            "turns": 1,
            "tokens": 50,
        }
        assert by_agent["totals"] == {"credits": 2.0, "cost": 0.3, "turns": 2, "tokens": 50}
        lines = {line.split()[0]: line.split() for line in table.splitlines()[1:]}
        assert lines["agent-k"] == ["agent-k", "2.00", "1", "-", "-"]
        assert lines["agent-d"] == ["agent-d", "-", "1", "0.30", "50"]
        assert lines["TOTAL"] == ["TOTAL", "2.00", "2", "0.30", "50"]


class TestTruncation:
    def test_rows_dropped_before_a_window_with_a_first_day_shard_are_not_disclosed(self):
        rows = [series_mod._Row(_day(0), 0.0, "s", "dashboard", "a", "m", 1.0)]

        report = series_mod.build_report(
            rows, days=3, dropped=5, complete_from=_day(2), today=TODAY
        )

        assert (report["truncated"], report["complete_from"]) == (False, None)

    def test_rows_dropped_inside_the_window_are_disclosed(self, shards, monkeypatch, capsys):
        monkeypatch.setattr(series_mod, "MAX_ROWS", 2)
        shards({0: [_record(0)], 1: [_record(1)], 2: [_record(2), _record(2)]})

        report = _report(capsys, "--days", "3")
        table = _run(capsys, "--days", "3")

        assert (report["truncated"], report["complete_from"]) == (True, _day(1))
        assert "may undercount" in table and f"every turn from {_day(1)} on is counted" in table

    def test_a_cap_tripped_inside_the_newest_shard_names_no_day(self):
        report = series_mod.build_report([], days=2, dropped=1, complete_from=None, today=TODAY)

        assert (report["truncated"], report["complete_from"]) == (True, None)


def test_json_is_one_document_and_the_two_window_flags_agree(shards, capsys):
    shards({0: [_record(0, credits=1.5)], 3: [_record(3, credits=2.0)]})

    by_days = _report(capsys, "--days", "4")
    by_since = _report(capsys, "--since", _day(3))
    table = _run(capsys, "--days", "4")

    assert by_days == by_since
    total = table.splitlines()[-1].split()
    assert total == ["TOTAL", f"{by_days['totals']['credits']:.2f}", "2"]
    with pytest.raises(SystemExit) as refused:
        _parser().parse_args(["usage", "--days", "3", "--since", _day(1)])
    assert refused.value.code == 2


def test_an_empty_window_prints_one_line_and_well_formed_json(shards, capsys):
    shards({})

    table = _run(capsys)
    report = _report(capsys)

    assert table.splitlines()[1:] == ["No usage recorded in this window."]
    assert report["totals"] == {"credits": 0.0, "cost": None, "turns": 0, "tokens": None}
    assert len(report["rows"]) == 7


@pytest.mark.asyncio
async def test_the_http_series_route_still_refuses_session(shards):
    app = web.Application()
    app.router.add_get("/api/usage/series", series_mod.api_usage_series)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/usage/series?by=session")
        status, body = resp.status, await resp.json()

    assert series_mod.SERIES_DIMENSIONS == ("channel", "agent", "model", "cohort")
    assert (status, body["code"]) == (400, "invalid_dimension")

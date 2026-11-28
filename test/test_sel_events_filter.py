"""Field filters and exports on ``kirocrew security events``: ``select_events`` and its CLI.

The command picks rows by operation, outcome, source and caller (exact match) beside the
time window. Every case that runs a real log writes through ``SecurityEventLog``. The
cases that pin today's behavior (no new flag, same output) pass on main.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures are requested by name
    _make_event,
    log_fixture,
    reset_singleton,
    sel_dir_fixture,
)

import kiro_crew.cli_commands as cc
from kiro_crew import cli_parser_admin

_BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)

#: (operation, outcome, caller, source), oldest first; one row per minute.
_ROWS = [
    ("execute_bash", "approved", "alice", "dashboard"),
    ("execute_bash", "denied", "alice", "dashboard"),
    ("fs_write", "denied", "bob", "slack"),
    ("execute_bash", "denied", "bob", "slack"),
    ("fs_write", "approved", "alice", "cron"),
    ("execute_bash", "denied", "alice", "cron"),
]


def _seed(log) -> list[dict]:
    """Write ``_ROWS`` and return what the log holds, newest first."""
    for i, (operation, outcome, caller, source) in enumerate(_ROWS):
        log.log(
            _make_event(
                event_id=f"evt{i}",
                timestamp=(_BASE + timedelta(minutes=i)).isoformat(),
                operation=operation,
                outcome=outcome,
                caller_identity=caller,
                source=source,
            )
        )
    return log.recent(limit=100)


def _ids(rows: list[dict]) -> list[str]:
    return [row["event_id"] for row in rows]


def _pick(log, **overrides):
    from kiro_crew.sel_filter import select_events

    kwargs = {"limit": 100, "filters": {}, "since": None, "until": None, "scan_limit": 100}
    kwargs.update(overrides)
    return select_events(log, **kwargs)


def _manifest(rows: list[dict], **overrides):
    from kiro_crew.sel_filter import build_manifest

    kwargs = {"fmt": "jsonl", "payload": "x", "filters": {}, "since": _BASE, "until": None}
    return build_manifest(rows, scan_capped=False, now=_BASE, **{**kwargs, **overrides})


def _parse(*argv: str) -> argparse.Namespace:
    root = argparse.ArgumentParser(prog="kirocrew")
    cli_parser_admin.register_security_parser(root.add_subparsers(dest="command"))
    return root.parse_args(["security", "events", *argv])


def _run_cli(log, *argv: str) -> None:
    with patch("kiro_crew.cli_commands.sel", return_value=log):
        cc._security(_parse(*argv))


def _forged() -> MagicMock:
    row = {"entry_hash": "\x1b]52;c;aGk=\x07z", "error": "a\ud800b"}
    return MagicMock(recent=MagicMock(return_value=[row]))


class TestSelectEvents:
    def test_a_row_is_kept_only_when_every_named_field_matches_exactly(self, log) -> None:
        _seed(log)
        picked = _pick(log, filters={"outcome": "denied", "caller": "alice", "operation": ""})
        assert _ids(picked.rows) == ["evt5", "evt1"]
        assert picked.scan_capped is False

    @pytest.mark.parametrize("value", ["bash", "execute_*", "EXECUTE_BASH", "execute_bash "])
    def test_a_value_is_compared_whole_not_as_a_substring_or_pattern(self, log, value) -> None:
        _seed(log)
        assert _pick(log, filters={"operation": value}).rows == []

    def test_the_limit_cuts_after_the_filter_not_before(self, log) -> None:
        _seed(log)
        picked = _pick(log, limit=2, filters={"outcome": "denied"})
        # Four rows were denied; the newest two are the answer, though the log's own two
        # newest rows (evt5, evt4) include an approved one.
        assert _ids(picked.rows) == ["evt5", "evt3"]

    def test_the_window_bounds_the_rows(self, log) -> None:
        _seed(log)
        picked = _pick(
            log,
            filters={"source": "slack"},
            since=_BASE + timedelta(minutes=3),
            until=_BASE + timedelta(minutes=5),
        )
        assert _ids(picked.rows) == ["evt3"]

    def test_a_scan_that_used_its_budget_says_so(self, log) -> None:
        _seed(log)
        picked = _pick(log, filters={"outcome": "denied"}, scan_limit=3)
        assert _ids(picked.rows) == ["evt5", "evt3"]
        assert picked.scan_capped is True

    def test_a_filled_limit_is_not_a_capped_scan(self, log) -> None:
        _seed(log)
        picked = _pick(log, limit=1, filters={"outcome": "denied"}, scan_limit=3)
        assert _ids(picked.rows) == ["evt5"]
        assert picked.scan_capped is False

    def test_no_field_filter_asks_the_log_for_exactly_the_limit(self) -> None:
        fake = MagicMock()
        fake.recent.return_value = [{"event_id": "e1"}]
        picked = _pick(fake, limit=7, filters={"operation": "", "outcome": ""}, scan_limit=50)
        fake.recent.assert_called_once_with(limit=7, since=None, until=None)
        assert picked.rows == [{"event_id": "e1"}]

    def test_a_forged_non_string_field_is_never_equal_to_a_filter(self) -> None:
        fake = MagicMock()
        fake.recent.return_value = [{"outcome": ["denied"]}, {"outcome": "denied"}]
        picked = _pick(fake, filters={"outcome": "denied"}, scan_limit=50)
        assert picked.rows == [{"outcome": "denied"}]


class TestRenderers:
    def test_csv_has_one_header_a_fixed_column_order_and_inert_cells(self) -> None:
        from kiro_crew.sel_filter import CSV_COLUMNS, render_csv

        rows = [
            {
                "timestamp": "2026-08-21T00:00:00+00:00",
                "operation": "=HYPERLINK(1)",
                "outcome": "+1",
                "caller_identity": "-x",
                "source": "@home",
                "resources": "\tcell",
                "error": "ok\x1b[31m red",
                "metadata": {"k": "v"},
            }
        ]
        parsed = list(csv.reader(io.StringIO(render_csv(rows))))
        assert (tuple(parsed[0]), len(parsed)) == (CSV_COLUMNS, 2)
        cells = dict(zip(parsed[0], parsed[1]))
        inert = [cells[k] for k in ("operation", "outcome", "caller_identity", "source")]
        assert inert == ["'=HYPERLINK(1)", "'+1", "'-x", "'@home"]
        assert "\x1b" not in cells["error"]
        assert json.loads(cells["metadata"]) == {"k": "v"}
        assert cells["agent"] == ""

    def test_the_manifest_names_the_ends_of_the_chain(self, log) -> None:
        oldest_first = list(reversed(_seed(log)))
        manifest = _manifest(oldest_first)
        assert manifest["first_timestamp"] == oldest_first[0]["timestamp"]
        assert manifest["last_timestamp"] == oldest_first[-1]["timestamp"]
        assert (manifest["since"], manifest["until"]) == (_BASE.isoformat(), None)
        assert manifest["exported_at"] == _BASE.isoformat()
        assert manifest["sha256"] == hashlib.sha256(b"x").hexdigest()
        assert (_manifest([])["rows"], _manifest([])["chain_hash_first"]) == (0, None)


class TestCli:
    def test_default_text_output_is_unchanged(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "-n", "2")
        out = capsys.readouterr().out
        assert out == (
            "📋 Last 2 security event(s):\n\n"
            "  2026-08-21T00:05:00  [cron] tool_invocation: execute_bash → denied"
            "  (caller: alice)\n"
            "  2026-08-21T00:04:00  [cron] tool_invocation: fs_write → approved"
            "  (caller: alice)\n"
        )

    def test_the_new_options_default_to_nothing(self) -> None:
        args = _parse()
        assert (args.operation, args.outcome, args.source, args.caller) == ("", "", "", "")
        assert (args.format, args.out, args.limit, args.limit_given) == ("text", "", 20, False)

    def test_text_output_honors_a_field_filter(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "--outcome", "denied", "--caller", "bob")
        out = capsys.readouterr().out
        assert out.count("→ denied") == 2
        assert "alice" not in out
        assert "Last 2 security event(s)" in out

    def test_a_filter_with_no_match_says_so(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "--operation", "nothing_like_this")
        assert "No security events matched the filters." in capsys.readouterr().out

    @pytest.mark.parametrize("extra", [["-n", "10"], ["--format", "jsonl"]])
    def test_a_scan_that_hit_its_budget_is_named_on_stderr(
        self, log, monkeypatch: pytest.MonkeyPatch, capsys, extra: list[str]
    ) -> None:
        _seed(log)
        monkeypatch.setattr("kiro_crew.sel_filter.CLI_SCAN_LIMIT", 3)
        _run_cli(log, "--outcome", "denied", *extra)
        assert "looked at the newest" in capsys.readouterr().err

    def test_an_earlier_until_reaches_the_row_a_capped_scan_missed(
        self, log, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _seed(log)
        monkeypatch.setattr("kiro_crew.sel_filter.CLI_SCAN_LIMIT", 3)
        only_evt1 = ("--outcome", "denied", "--caller", "alice", "--source", "dashboard")
        _run_cli(log, *only_evt1)
        captured = capsys.readouterr()
        assert "No security events matched the filters." in captured.out
        assert "set an earlier --until to reach older rows" in captured.err
        assert "--since" not in captured.err
        _run_cli(log, *only_evt1, "--until", (_BASE + timedelta(minutes=3)).isoformat())
        reached = capsys.readouterr()
        assert "Last 1 security event(s)" in reached.out
        assert "execute_bash → denied" in reached.out

    def test_jsonl_export_writes_every_match_oldest_first_with_a_manifest(
        self, log, tmp_path: Path, capsys
    ) -> None:
        held = _seed(log)
        target = tmp_path / "deny.jsonl"
        _run_cli(log, "--outcome", "denied", "--format", "jsonl", "--out", str(target))
        written = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        assert written == [r for r in reversed(held) if r["outcome"] == "denied"]
        assert _ids(written) == ["evt1", "evt2", "evt3", "evt5"]
        manifest = json.loads((tmp_path / "deny.jsonl.manifest.json").read_text(encoding="utf-8"))
        assert (manifest["rows"], manifest["filters"]) == (4, {"outcome": "denied"})
        assert manifest["chain_hash_first"] == written[0]["entry_hash"]
        out = capsys.readouterr().out
        assert "Wrote 4 event(s) to deny.jsonl" in out and str(tmp_path) not in out
        capped = tmp_path / "capped.jsonl"  # -n still cuts an export
        _run_cli(log, "--outcome", "denied", "-n", "2", "--format", "jsonl", "--out", str(capped))
        kept = [json.loads(x) for x in capped.read_text(encoding="utf-8").splitlines()]
        assert _ids(kept) == ["evt3", "evt5"]

    def test_an_export_claims_both_names_owner_only_before_text(self, log, tmp_path: Path) -> None:
        _seed(log)
        target = tmp_path / "all.jsonl"
        locked: list[tuple[str, int]] = []

        def lock(path) -> None:
            locked.append((Path(path).name, os.path.getsize(path)))
            if len(locked) == 1:  # a second export to the name this run holds is refused
                with pytest.raises(SystemExit, match="1"):
                    _run_cli(log, "--source", "slack", "--format", "jsonl", "--out", str(target))

        with patch("kiro_crew.platform_compat.restrict_to_owner", side_effect=lock):
            _run_cli(log, "--format", "jsonl", "--out", str(target))
        assert locked == [("all.jsonl", 0), ("all.jsonl.manifest.json", 0)]
        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        assert [b["prev_hash"] for b in rows[1:]] == [a["entry_hash"] for a in rows[:-1]]
        manifest = json.loads((tmp_path / "all.jsonl.manifest.json").read_text(encoding="utf-8"))
        assert (manifest["rows"], manifest["chain_hash_last"]) == (6, rows[-1]["entry_hash"])
        assert manifest["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()

    @pytest.mark.parametrize("error", [PermissionError(13, "Denied"), RuntimeError("/x")])
    def test_a_failed_manifest_claim_removes_the_export_this_run_made(
        self, log, tmp_path: Path, capsys, error: Exception
    ) -> None:
        _seed(log)
        (tmp_path / "out").mkdir()

        def lock(path) -> None:
            if str(path).endswith(".manifest.json"):
                raise error

        with patch("kiro_crew.platform_compat.restrict_to_owner", side_effect=lock):
            with pytest.raises(SystemExit, match="1"):
                _run_cli(log, "--format", "jsonl", "--out", str(tmp_path / "out" / "a.jsonl"))
        assert list((tmp_path / "out").iterdir()) == []
        err = capsys.readouterr().err
        assert type(error).__name__ in err and "/x" not in err

    def test_a_forged_chain_hash_prints_no_control_character(self, capsys) -> None:
        _run_cli(_forged(), "--format", "jsonl")
        assert not any(ord(c) < 32 for c in capsys.readouterr().err.rstrip("\n"))

    def test_a_lone_surrogate_exports_as_valid_utf8_csv(self, tmp_path: Path) -> None:
        target = tmp_path / "s.csv"
        _run_cli(_forged(), "--format", "csv", "--out", str(target))
        text = target.read_bytes().decode("utf-8")
        assert text.startswith("timestamp,event_id,event_type,") and "a\\ud800b" in text
        manifest = json.loads((tmp_path / "s.csv.manifest.json").read_text(encoding="utf-8"))
        assert manifest["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        if os.name != "nt":
            assert {p.stat().st_mode & 0o777 for p in tmp_path.glob("s.csv*")} == {0o600}

    def test_without_out_the_rows_go_to_stdout_and_the_hashes_to_stderr(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "--format", "jsonl", "--source", "slack")
        captured = capsys.readouterr()
        rows = [json.loads(line) for line in captured.out.splitlines()]
        assert _ids(rows) == ["evt2", "evt3"]
        assert f"first={rows[0]['entry_hash']} last={rows[1]['entry_hash']}" in captured.err

    @pytest.mark.parametrize(
        ("blocker", "out", "shown"),
        [
            ("taken.jsonl", "taken.jsonl", "taken.jsonl"),
            ("taken.jsonl.manifest.json", "taken.jsonl", "taken.jsonl"),
            ("a_file", "a_file/a.jsonl", "a.jsonl"),
        ],
    )
    def test_an_export_that_cannot_be_written_stops_and_prints_only_the_file_name(
        self, log, tmp_path: Path, capsys, blocker: str, out: str, shown: str
    ) -> None:
        _seed(log)
        (tmp_path / blocker).write_text("keep me", encoding="utf-8")
        with pytest.raises(SystemExit, match="1"):
            _run_cli(log, "--format", "jsonl", "--out", str(tmp_path / out))
        assert (tmp_path / blocker).read_text(encoding="utf-8") == "keep me"
        made = {n for n in ("taken.jsonl", "taken.jsonl.manifest.json") if (tmp_path / n).exists()}
        assert made <= {blocker}
        err = capsys.readouterr().err
        assert err.startswith("Error:") and shown in err and str(tmp_path) not in err

    @pytest.mark.parametrize("out", [".", "/", "~no_such_user_x/a.jsonl", "a\0b.jsonl"])
    def test_an_unusable_out_exits_1_with_one_error_line_and_writes_nothing(
        self, log, tmp_path: Path, monkeypatch, capsys, out: str
    ) -> None:
        monkeypatch.chdir(tmp_path)
        before = set(tmp_path.iterdir())  # the log's own files
        with pytest.raises(SystemExit, match="1"):
            _run_cli(log, "--format", "jsonl", "--out", out)
        err = capsys.readouterr().err
        assert err.startswith("Error:") and err.count("\n") == 1 and str(tmp_path) not in err
        assert set(tmp_path.iterdir()) == before

    @pytest.mark.parametrize(
        ("argv", "warning"),
        [
            (["--out", "NAME"], "--out applies to --format jsonl or csv"),
            (["--format", "xml"], "is not text, jsonl or csv"),
        ],
    )
    def test_an_unusable_export_option_is_dropped_with_a_warning(
        self, log, tmp_path: Path, capsys, argv: list[str], warning: str
    ) -> None:
        _seed(log)
        target = tmp_path / "ignored.txt"
        _run_cli(log, *[str(target) if a == "NAME" else a for a in argv], "-n", "1")
        captured = capsys.readouterr()
        assert warning in captured.err
        assert "Last 1 security event(s)" in captured.out
        assert not target.exists()


_EDGE_INSTANTS = ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"]


class TestBoundaryYearInstant:
    """An instant at the edge of the date range with an offset is unplaceable, not a crash."""

    @pytest.mark.parametrize("text", _EDGE_INSTANTS)
    def test_the_selector_refuses_it_with_the_usual_message(self, text: str) -> None:
        with pytest.raises(ValueError, match="cannot read .* as a time"):
            cc.parse_time_selector(text)

    @pytest.mark.parametrize("text", _EDGE_INSTANTS)
    def test_the_reader_treats_it_as_unplaceable(self, text: str) -> None:
        from kiro_crew.sel_reader import _parse_timestamp

        assert _parse_timestamp(text) is None

    def test_instants_that_convert_cleanly_are_unchanged(self) -> None:
        from kiro_crew.sel_reader import _parse_timestamp

        edge = datetime(1, 1, 1, 5, tzinfo=timezone.utc)
        assert cc.parse_time_selector("0001-01-01T05:00:00+00:00") == edge
        assert _parse_timestamp("0001-01-01T05:00:00+00:00") == edge
        assert _parse_timestamp("2026-08-21T10:00:00+02:00") == _BASE.replace(hour=8)

    @pytest.mark.parametrize("flag", ["--since", "--until"])
    def test_the_command_exits_2_with_one_line_on_stderr(self, log, capsys, flag: str) -> None:
        _seed(log)
        with pytest.raises(SystemExit) as stopped:
            _run_cli(log, flag, _EDGE_INSTANTS[0])
        assert stopped.value.code == 2
        err = capsys.readouterr().err
        assert err.startswith("❌ cannot read ") and err.count("\n") == 1

    def test_one_such_line_does_not_stop_a_windowed_read(self, log) -> None:
        log.log(_make_event(event_id="a", timestamp=_BASE.isoformat()))
        forged = {"event_id": "forged", "timestamp": _EDGE_INSTANTS[0]}
        with log._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged) + "\n")
        assert _ids(log.recent(limit=10)) == ["forged", "a"]
        assert _ids(log.recent(limit=10, since=_BASE - timedelta(days=1))) == ["a"]

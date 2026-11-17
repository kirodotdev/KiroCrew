"""``kirocrew trace show``: the waterfall of one traced turn, read from the local span files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew.tracing import render

TRACE_A = "a" * 32
TRACE_B = "b" * 32
TURN_A = "turn-aaaa"
SESSION = "chat:session-1"


@pytest.fixture(autouse=True)
def _traces_dir(tmp_path: Path, _floor_monkeypatch):
    """Point the command at a throwaway traces directory."""
    directory = tmp_path / "traces"
    directory.mkdir()
    _floor_monkeypatch.setattr("kiro_crew.tracing.provider.traces_dir", lambda: directory)
    return directory


def _span(name, span_id, parent, start, end, *, trace=TRACE_A, attrs=None, error=None):
    """One ``ReadableSpan.to_json`` record; times are seconds past 22:00:00 UTC."""

    def stamp(seconds):
        whole, frac = divmod(round(seconds * 1_000_000), 1_000_000)
        return f"2026-11-14T22:{whole // 60:02d}:{whole % 60:02d}.{frac:06d}Z"

    status = {"status_code": "ERROR" if error else "UNSET"}
    return {
        "name": name,
        "context": {"trace_id": f"0x{trace}", "span_id": f"0x{span_id:016x}", "trace_state": "[]"},
        "parent_id": None if parent is None else f"0x{parent:016x}",
        "start_time": stamp(start),
        "end_time": stamp(end),
        "status": status,
        "attributes": {**(attrs or {}), **({"error.type": error} if error else {})},
    }


def _write(directory: Path, rows, name="spans-2026-11-14-100.jsonl", extra_lines=()):
    lines = [json.dumps(r) for r in rows] + list(extra_lines)
    (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _turn(trace=TRACE_A, base=1, attrs=None):
    ids = {"kirocrew.turn_id": TURN_A, "kirocrew.local.session_key": SESSION, **(attrs or {})}
    return [
        _span("invoke_agent kirocrew", base, None, 0, 10, trace=trace, attrs=ids),
        _span("kirocrew.step", base + 1, base, 0.5, 2.5, trace=trace),
        _span("execute_tool shell", base + 2, base, 3.0, 4.0, trace=trace),
        _span("execute_tool read", base + 3, base, 3.5, 4.5, trace=trace, error="Timeout"),
        _span("kirocrew.step", base + 4, base, 9.0, 10.0, trace=trace),
    ]


def _show(capsys, key):
    render.trace_show(argparse.Namespace(key=key))
    captured = capsys.readouterr()
    return captured.out.splitlines(), captured.err


def test_a_turn_prints_as_an_indented_waterfall_with_its_gaps(_traces_dir, capsys):
    _write(_traces_dir, _turn())
    rows, err = _show(capsys, TURN_A)
    assert err == ""
    assert rows[0].startswith(f"trace {TRACE_A}  5 spans  10.00s  2026-11-14 22:00:00 UTC")
    names = [row[: render._NAME_WIDTH].strip() for row in rows[1:]]
    assert names == [
        "invoke_agent kirocrew",
        "· gap 500ms",
        "kirocrew.step",
        "· gap 500ms",
        "execute_tool shell",
        "execute_tool read",
        "· gap 4.50s",
        "kirocrew.step",
    ]
    # Children sit one level in; the first step starts 0.5 s into the turn.
    assert rows[3].startswith("  kirocrew.step") and "500ms" in rows[3].split()
    # The overlapping tool calls are two rows, not a gap between them.
    assert "gap" not in rows[5] and "gap" not in rows[6]
    # A span whose status is an error says so, with its type.
    assert rows[6].endswith("! Timeout")
    # Bars are proportional: the 1 s final step sits at the right end of a 10 s turn.
    assert rows[-1].rstrip().endswith("#" * 3)


def test_a_session_key_picks_its_most_recent_trace_and_counts_the_others(_traces_dir, capsys):
    _write(_traces_dir, _turn(TRACE_A), "spans-2026-11-13-100.jsonl")
    later = _turn(TRACE_B, base=20)
    for row in later:
        row["start_time"] = row["start_time"].replace("22:0", "23:0")
        row["end_time"] = row["end_time"].replace("22:0", "23:0")
    _write(_traces_dir, later, "spans-2026-11-14-200.jsonl")
    rows, _ = _show(capsys, SESSION)
    assert rows[0].startswith("1 other trace(s) match")
    assert rows[1].startswith(f"trace {TRACE_B}")


def test_a_trace_id_picks_that_trace_even_with_a_prefix(_traces_dir, capsys):
    _write(_traces_dir, _turn(TRACE_A) + _turn(TRACE_B, base=20))
    rows, _ = _show(capsys, f"0x{TRACE_A}")
    assert rows[0].startswith(f"trace {TRACE_A}")


@pytest.mark.parametrize("key", ["no-such-turn", "", "x" * 1000])
def test_an_unknown_key_prints_one_line_and_returns(_traces_dir, capsys, key):
    _write(_traces_dir, _turn())
    rows, err = _show(capsys, key)
    assert len(rows) == 1 and rows[0].startswith("No trace matches ") and err == ""


def test_no_traces_directory_reads_as_nothing_matched(tmp_path, _floor_monkeypatch, capsys):
    _floor_monkeypatch.setattr("kiro_crew.tracing.provider.traces_dir", lambda: tmp_path / "none")
    rows, _ = _show(capsys, TURN_A)
    assert rows == [f"No trace matches {TURN_A}."]


def test_a_malformed_line_is_skipped_with_one_warning_per_shard(_traces_dir, capsys):
    junk = [
        "{not json",
        "[1, 2]",
        json.dumps({"name": "no-context"}),
        "x" * (render.MAX_LINE_CHARS + 1),
    ]
    _write(_traces_dir, _turn(), extra_lines=junk)
    rows, err = _show(capsys, TURN_A)
    assert rows[0].startswith(f"trace {TRACE_A}  5 spans")
    assert err.count("Warning:") == 1 and "skipped 4 line(s) in spans-2026-11-14-100.jsonl" in err


def test_a_shard_over_the_size_cap_is_skipped_with_a_warning(
    _traces_dir, _floor_monkeypatch, capsys
):
    _write(_traces_dir, _turn())
    _floor_monkeypatch.setattr(render, "MAX_SHARD_BYTES", 10)
    rows, err = _show(capsys, TURN_A)
    assert rows[0].startswith("No trace matches")
    assert "skipped spans-2026-11-14-100.jsonl: larger than" in err


def test_the_span_count_is_bounded_and_the_newest_shards_are_read_first(
    _traces_dir, _floor_monkeypatch, capsys
):
    _write(_traces_dir, _turn(TRACE_A), "spans-2026-11-13-100.jsonl")
    _write(_traces_dir, _turn(TRACE_B, base=20), "spans-2026-11-14-100.jsonl")
    _floor_monkeypatch.setattr(render, "MAX_SPANS", 5)
    rows, err = _show(capsys, TURN_A)
    assert rows[0].startswith(f"trace {TRACE_B}")
    assert "stopped at 5 spans" in err
    assert render.read_spans(_traces_dir)[0].trace_id == TRACE_B


def test_an_unreadable_shard_stops_the_run_naming_only_its_file_and_class(
    _traces_dir, _floor_monkeypatch, capsys
):
    _write(_traces_dir, _turn())

    def refuse(path, **kwargs):
        raise PermissionError(f"Blocked: access to sensitive path: {path!r}")

    _floor_monkeypatch.setattr("kiro_crew.hooks.safe_read_file", refuse)
    with pytest.raises(SystemExit) as exited:
        render.trace_show(argparse.Namespace(key=TURN_A))
    captured = capsys.readouterr()
    assert exited.value.code == 1 and captured.out == ""
    assert captured.err == "Error: cannot read spans-2026-11-14-100.jsonl: PermissionError\n"


def test_a_directory_named_like_a_shard_and_other_files_are_left_out(_traces_dir, capsys):
    _write(_traces_dir, _turn())
    (_traces_dir / "spans-2026-11-14-300.jsonl").mkdir()
    (_traces_dir / "notes.jsonl").write_text("not a span\n", encoding="utf-8")
    rows, err = _show(capsys, TURN_A)
    assert rows[0].startswith(f"trace {TRACE_A}") and err == ""


def test_names_are_printed_without_control_sequences_and_attributes_stay_out(_traces_dir, capsys):
    rows = _turn(attrs={"kirocrew.tool_call_id": "toolcall-secret-id"})
    rows.append(_span("hostile \x1b]0;pwn\x07name\nforged line", 9, 1, 5, 6, error="E\x1b[31mX"))
    _write(_traces_dir, rows)
    printed, _ = _show(capsys, TURN_A)
    text = "\n".join(printed)
    assert "\x1b" not in text and "\x07" not in text
    assert any(row.startswith("  hostile ") and "\\x0a" in row for row in printed)
    assert "toolcall-secret-id" not in text and SESSION not in text


def test_spans_whose_parent_is_missing_are_shown_at_the_top(_traces_dir, capsys):
    _write(_traces_dir, [_span("orphan", 5, 99, 0, 1), _span("root", 1, None, 0, 2)])
    rows, _ = _show(capsys, TRACE_A)
    assert [row[:6].strip() for row in rows[1:]] == ["orphan", "root"]


def test_the_rows_are_bounded(_traces_dir, _floor_monkeypatch, capsys):
    _write(
        _traces_dir,
        [_span("root", 1, None, 0, 100)] + [_span("s", 2 + i, 1, i, i + 1) for i in range(30)],
    )
    _floor_monkeypatch.setattr(render, "MAX_ROWS", 10)
    rows, _ = _show(capsys, TRACE_A)
    assert len(rows) == 12 and rows[-1].startswith("... ") and rows[-1].endswith("not shown")


def test_the_cut_line_counts_every_span_never_shown_not_just_the_queued_ones(_traces_dir, capsys):
    """Two subtrees of 600 spans under one root: the cut falls inside the first one."""
    ids = iter(range(10, 10_000))
    spans = [_span("root", 1, None, 0, 1300), _span("a", 2, 1, 0, 650), _span("b", 3, 1, 650, 1300)]
    for parent, base in ((2, 0), (3, 650)):
        spans += [_span("s", next(ids), parent, base + i, base + i + 1) for i in range(600)]
    _write(_traces_dir, spans)
    rows, _ = _show(capsys, TRACE_A)
    shown = sum(1 for row in rows[1:-1] if "· gap" not in row)
    assert len(spans) == 1203 and shown < 600
    assert rows[-1] == f"... {len(spans) - shown} more spans not shown"
    assert len(spans) - shown > 700


def test_the_command_writes_nothing(_traces_dir, capsys):
    _write(_traces_dir, _turn())
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in _traces_dir.iterdir()}
    _show(capsys, TURN_A)
    after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in _traces_dir.iterdir()}
    assert before == after


def test_it_reads_what_the_real_exporter_wrote(_traces_dir, capsys):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from kiro_crew.tracing.exporter import JsonlSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(_traces_dir)))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("turn", attributes={"kirocrew.turn_id": "real-turn"}):
        with tracer.start_as_current_span("child"):
            pass
    provider.shutdown()
    rows, err = _show(capsys, "real-turn")
    spans = [row[: render._NAME_WIDTH].strip() for row in rows[1:] if "gap" not in row]
    assert spans == ["turn", "child"] and err == ""
    assert rows[0].split()[2:4] == ["2", "spans"]


def test_a_trace_id_that_is_not_hex_is_not_a_span_and_never_reaches_the_terminal(
    _traces_dir, capsys
):
    ids = {"kirocrew.turn_id": TURN_A}
    hostile = _span("root", 1, None, 20, 21, trace="\x1b]0;pwn\x07", attrs=ids)
    _write(_traces_dir, [hostile, *_turn(TRACE_A)])
    rows, err = _show(capsys, TURN_A)
    assert "\x1b" not in "\n".join(rows) and "\x07" not in "\n".join(rows)
    assert rows[0].startswith(f"trace {TRACE_A}  5 spans") and "skipped 1 line(s)" in err
    assert render.parse_span(hostile) is None


def test_a_line_nested_past_the_parser_limit_is_skipped_like_any_other_bad_line(
    _traces_dir, capsys
):
    _write(_traces_dir, _turn(), extra_lines=["[" * 100_000, '{"a":' * 100_000])
    rows, err = _show(capsys, TURN_A)
    assert rows[0].startswith(f"trace {TRACE_A}  5 spans")
    assert err.count("Warning:") == 1 and "skipped 2 line(s)" in err


def test_a_traces_path_that_cannot_be_listed_stops_with_its_class_only(
    tmp_path, _floor_monkeypatch, capsys
):
    not_a_directory = tmp_path / "traces-file"
    not_a_directory.write_text("x", encoding="utf-8")
    _floor_monkeypatch.setattr("kiro_crew.tracing.provider.traces_dir", lambda: not_a_directory)
    with pytest.raises(SystemExit) as exited:
        render.trace_show(argparse.Namespace(key=TURN_A))
    captured = capsys.readouterr()
    assert exited.value.code == 1 and captured.out == ""
    assert captured.err.startswith("Error: cannot read the traces folder: ")
    assert captured.err.count("\n") == 1 and str(not_a_directory) not in captured.err

"""``artifact show --xlsx``: each pipe table of an artifact becomes one text-only sheet."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from openpyxl import load_workbook  # type: ignore[import-untyped]
from test_cli_artifact_show_docx import _parse, _show  # same fake gateway as the Word tests

from kiro_crew import artifact_xlsx
from kiro_crew import cli_commands as cc

TWO = "# Plan\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n## Costs\n\n| c |\n|---|\n| 3 |\n"


def _sheets(tmp_path: Path, content: str, kind: str = "markdown") -> dict[str, list[list]]:
    target = tmp_path / "out.xlsx"
    _show(["--xlsx", str(target)], {"kind": kind, "content": content})
    book = load_workbook(str(target))
    return {ws.title: [[c.value for c in row] for row in ws.iter_rows()] for ws in book.worksheets}


def test_each_table_is_a_sheet_named_after_the_heading_above_it(tmp_path) -> None:
    assert _sheets(tmp_path, TWO) == {"Plan": [["a", "b"], ["1", "2"]], "Costs": [["c"], ["3"]]}


def test_every_cell_is_text_and_a_formula_looking_cell_is_never_a_formula(tmp_path) -> None:
    cells = ['=HYPERLINK("x")', "+1", "-2", "@x", "007"]
    _sheets(tmp_path, "| h |\n|---|\n" + "".join(f"| {c} |\n" for c in cells))
    sheet = load_workbook(str(tmp_path / "out.xlsx")).worksheets[0]
    got = [(c.value, c.data_type) for (c,) in sheet.iter_rows(min_row=2)]
    assert got == [(c, "s") for c in cells]


def test_cells_keep_their_marks_and_escaped_pipes_and_a_short_row_is_padded(tmp_path) -> None:
    src = "| a | b |\n|---|---|\n| **x** | 1\\|2 |\n| only |\n"
    assert _sheets(tmp_path, src) == {"Table 1": [["a", "b"], ["**x**", "1|2"], ["only", None]]}


@pytest.mark.parametrize(
    ("headings", "expected"),
    [
        (["Plan", "Plan", "plan"], ["Plan", "Plan 2", "plan 3"]),
        (["x" * 40], ["x" * 31]),
        (["a:b/c?"], ["a b c"]),
        (["'quoted'", "''"], ["quoted", "Table 2"]),
        (["history", "History", "Table 3"], ["Table 1", "Table 2", "Table 3"]),
        (["x" * 31, "x" * 31], ["x" * 31, "x" * 29 + " 2"]),
    ],
)
def test_sheet_titles(tmp_path, headings, expected) -> None:
    src = "".join(f"# {h}\n\n| a |\n|---|\n| 1 |\n\n" for h in headings)
    assert list(_sheets(tmp_path, src)) == expected


def test_a_text_artifact_with_a_table_is_written_too(tmp_path) -> None:
    assert _sheets(tmp_path, "| a |\n|---|\n| 1 |\n", kind="text") == {"Table 1": [["a"], ["1"]]}


def test_a_table_inside_a_code_block_is_not_a_table(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _sheets(tmp_path, "```\n| a |\n|---|\n| 1 |\n```\n")
    assert exc.value.code == 1
    assert "no table" in capsys.readouterr().err
    assert not (tmp_path / "out.xlsx").exists()


def test_control_characters_are_dropped_and_the_file_opens(tmp_path) -> None:
    assert _sheets(tmp_path, "| a\x00b |\n|---|\n| c\x1bd\ud800 |\n") == {
        "Table 1": [["ab"], ["cd"]]
    }


@pytest.mark.parametrize(
    "content",
    [
        "".join(f"# t{n}\n\n| a |\n|---|\n| 1 |\n\n" for n in range(51)),
        "| a |\n|---|\n" + "| 1 |\n" * 100_000,
        "| a |\n|---|\n| " + "x" * 32_768 + " |\n",
        "x" * 1_000_001,
        "|a" * 400 + "|\n" + "|-" * 400 + "|\n" + "| x |\n" * 170,
    ],
    ids=["tables", "cells", "cell-length", "characters", "padding"],
)
def test_over_a_cap_writes_nothing(tmp_path, content, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _show(["--xlsx", str(tmp_path / "out.xlsx")], {"kind": "markdown", "content": content})
    assert exc.value.code == 1
    assert "too large" in capsys.readouterr().err
    assert not (tmp_path / "out.xlsx").exists()


WIDE = ["|a" * 400 + "|", "|-" * 400 + "|"] + ["x"] * 400
LISTED = "* x\n" + WIDE[0] + "\n  " + "\n".join(WIDE[1:]) + "\n"
WIDE_TEXTS = ["\n".join(WIDE) + "\n", "".join(f"> {r}\n" for r in WIDE), LISTED]


@pytest.mark.parametrize("text", WIDE_TEXTS, ids=["top-level", "in-a-quote", "after-a-list"])
def test_a_wide_header_over_short_lines_is_refused_before_its_cells_are_built(
    monkeypatch, text
) -> None:
    # The parser pads short rows to the header's width: no body cell may be built here.
    from markdown_it.rules_block.state_block import StateBlock

    built, push = [], StateBlock.push

    def counting_push(state, kind, *rest):
        if kind == "td_open":
            built.append(kind)
        return push(state, kind, *rest)

    monkeypatch.setattr(StateBlock, "push", counting_push)
    with pytest.raises(artifact_xlsx.XlsxTooLarge):
        artifact_xlsx.build(text)
    assert built == []


def test_tables_under_headings_with_no_blank_line_between_are_all_written(tmp_path) -> None:
    table = "|" + " a |" * 10 + "\n" + "|---" * 10 + "|\n" + ("|" + " 1 |" * 10 + "\n") * 20
    assert len(_sheets(tmp_path, "".join(f"## T{n}\n{table}" for n in range(40)))) == 40


def test_a_cell_of_exactly_the_limit_is_written(tmp_path) -> None:
    cell = "x" * artifact_xlsx.MAX_CELL_CHARS
    assert _sheets(tmp_path, f"| {cell} |\n|---|\n| 1 |\n") == {"Table 1": [[cell], ["1"]]}


@pytest.mark.parametrize("kind", ["widget", "html", "json", "svg", "image"])
def test_other_kinds_write_nothing(tmp_path, kind, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _show(["--xlsx", str(tmp_path / "out.xlsx")], {"kind": kind, "content": TWO})
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert kind in err and "spreadsheet file" in err
    assert not (tmp_path / "out.xlsx").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_the_file_is_owner_only(tmp_path) -> None:
    _sheets(tmp_path, TWO)
    assert (tmp_path / "out.xlsx").stat().st_mode & 0o777 == 0o600


def test_an_existing_file_is_left_alone_and_the_path_is_not_printed(tmp_path, capsys) -> None:
    target = tmp_path / "out.xlsx"
    target.write_bytes(b"mine")
    with pytest.raises(SystemExit) as exc:
        _show(["--xlsx", str(target)], {"kind": "markdown", "content": TWO})
    assert exc.value.code == 1
    assert target.read_bytes() == b"mine"
    err = capsys.readouterr().err
    assert "out.xlsx" in err
    assert str(tmp_path) not in err


def test_a_sensitive_path_or_an_empty_name_is_refused_before_anything_is_fetched(
    tmp_path, capsys
) -> None:
    target = tmp_path / "out.xlsx"
    with (
        patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True) as gate,
        patch("kiro_crew.cli_commands._gateway_send") as send,
        pytest.raises(SystemExit) as exc,
    ):
        cc._artifact(_parse(["artifact", "show", "s1", "--xlsx", str(target)]))
    assert exc.value.code == 1
    gate.assert_called_once_with(str(target.resolve()))
    with patch("kiro_crew.cli_commands._gateway_send") as send2, pytest.raises(SystemExit):
        cc._artifact(_parse(["artifact", "show", "s1", "--xlsx", ""]))
    send.assert_not_called()
    send2.assert_not_called()
    assert not target.exists()
    err = capsys.readouterr().err
    assert str(tmp_path) not in err and "spreadsheet file" in err


def test_xlsx_is_exclusive_with_meta_and_docx(tmp_path) -> None:
    assert _parse(["artifact", "show", "s1", "--xlsx", "o.xlsx"]).xlsx == "o.xlsx"
    for other in (["--meta"], ["--docx", str(tmp_path / "o.docx")]):
        with pytest.raises(SystemExit) as exc:
            _parse(["artifact", "show", "s1", "--xlsx", "o.xlsx", *other])
        assert exc.value.code == 2


def test_without_the_flag_the_body_prints_as_before(capsys) -> None:
    _show([], {"kind": "markdown", "content": TWO})
    assert capsys.readouterr().out == TWO + "\n"

"""``kirocrew artifact show SLUG --docx FILE`` writes a markdown or text artifact as Word.

The file is created exclusively and owner-only; each check reads ``word/document.xml``
back. Without the flag the body prints as it always did.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse
from docx import Document

from kiro_crew import cli_commands as cc
from kiro_crew.cli_parser_agent import register_artifact_parser
from kiro_crew.config.loader import KiroCrewConfig

_MARKDOWN = (
    "# Title\n\nHello **world** and *softly*\n\n- first\n- second\n\n1. one\n2. two\n\n"
    "```\nprint('x')\n```\n\n| name | qty |\n| --- | --- |\n| bolt | 4 |\n"
)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kirocrew")
    register_artifact_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def _show(argv: list[str], payload: dict) -> None:
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch("kiro_crew.cli_commands._gateway_send", side_effect=[_FakeResponse(payload)]),
    ):
        cc._artifact(_parse(["artifact", "show", "s1", *argv]))


def _write(tmp_path: Path, content: str, kind: str = "markdown") -> str:
    out = tmp_path / "o.docx"
    _show(["--docx", str(out)], {"content": content, "kind": kind})
    with zipfile.ZipFile(out) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def test_headings_spans_and_lists(tmp_path, capsys) -> None:
    xml = _write(tmp_path, _MARKDOWN)
    assert 'w:val="Heading1"' in xml and "Title" in xml
    assert "Hello " in xml and "<w:b/>" in xml and "<w:i/>" in xml
    assert "**" not in xml and "*softly*" not in xml
    assert xml.count('w:val="ListBullet"') == 2 and "1. </w:t>" in xml and "2. </w:t>" in xml
    assert capsys.readouterr().out == "Wrote o.docx\n"


@pytest.mark.parametrize(
    ("source", "texts"),
    [
        ("7. first\n   - nested\n8. second\n", ["7. first", "nested", "8. second"]),
        ("1. a\n2. b\n\nmid\n\n1. c\n2. d\n", ["1. a", "2. b", "mid", "1. c", "2. d"]),
        ("0. zero\n1. one\n", ["0. zero", "1. one"]),
        ("1. # First\n2. # Second\n", ["1. ", "# First", "2. ", "# Second"]),
        ("1. ```\n   x = 1\n   ```\n2. b\n", ["1. ", "x = 1", "2. b"]),
        ("1. <div>x</div>\n2. b\n", ["1. ", "<div>x</div>", "2. b"]),
        ("1. - a\n2. b\n", ["1. ", "a", "2. b"]),
    ],
)
def test_every_list_item_keeps_its_number_and_first_block(tmp_path, source, texts) -> None:
    _write(tmp_path, source)  # "# " marks a paragraph written in the Heading 1 style
    paragraphs = Document(str(tmp_path / "o.docx")).paragraphs
    assert [("# " if p.style.name == "Heading 1" else "") + p.text for p in paragraphs] == texts


def test_a_fenced_block_is_monospace_and_keeps_its_text(tmp_path) -> None:
    xml = _write(tmp_path, "```\nprint('x')\n  indented **raw**\n```\n")
    assert "Courier New" in xml and "print('x')" in xml and "indented **raw**" in xml


def test_quotes_code_spans_nested_lists_and_raw_html_keep_their_text(tmp_path) -> None:
    xml = _write(tmp_path, "> said\n\nuse `x` <b>raw</b>\n\n- a\n  - b\n\n<div>blk</div>\n")
    assert 'w:val="Quote"' in xml and 'w:val="ListBullet2"' in xml and "Courier New" in xml
    assert "said" in xml and "&lt;b&gt;" in xml and "&lt;div&gt;blk" in xml


def test_a_pipe_table_becomes_a_table(tmp_path) -> None:
    xml = _write(tmp_path, _MARKDOWN)
    assert xml.count("<w:tbl>") == 1 and xml.count("<w:tr>") + xml.count("<w:tr ") == 2
    assert "bolt" in xml and "qty" in xml and "---" not in xml


def _table(tmp_path: Path, content: str) -> list[list[str]]:
    _write(tmp_path, content)
    (table,) = Document(str(tmp_path / "o.docx")).tables
    return [[cell.text for cell in row.cells] for row in table.rows]


def test_an_all_dash_data_row_is_kept(tmp_path) -> None:
    rows = _table(tmp_path, "| a | b |\n| --- | --- |\n| - | - |\n| c | d |\n")
    assert rows == [["a", "b"], ["-", "-"], ["c", "d"]]


def test_an_empty_first_cell_keeps_the_columns(tmp_path) -> None:
    assert _table(tmp_path, "| a | b |\n|---|---|\n||4|\n") == [["a", "b"], ["", "4"]]


def test_a_text_artifact_keeps_its_characters_as_they_are(tmp_path) -> None:
    xml = _write(tmp_path, "# not a heading\n*star* ke\x00pt\n\nsecond", kind="text")
    assert "# not a heading" in xml and "*star* kept" in xml and "Heading1" not in xml


def test_a_text_artifact_keeps_blank_lines_and_trailing_spaces(tmp_path) -> None:
    _write(tmp_path, "one  \n\n   \ntwo\t\n", kind="text")
    lines = [p.text for p in Document(str(tmp_path / "o.docx")).paragraphs]
    assert lines == ["one  ", "", "   ", "two\t"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_is_owner_only(tmp_path) -> None:
    _write(tmp_path, "hello")
    assert (tmp_path / "o.docx").stat().st_mode & 0o777 == 0o600


def _refused(tmp_path, capsys, code, content="hello", kind="markdown") -> str:
    """Run ``--docx`` expecting an exit with *code*; return what it printed to stderr."""
    with pytest.raises(SystemExit) as exc:
        _show(["--docx", str(tmp_path / "o.docx")], {"content": content, "kind": kind})
    assert exc.value.code == code
    return capsys.readouterr().err


@pytest.mark.parametrize("kind", ["widget", "html"])
def test_other_kinds_write_nothing(tmp_path, capsys, kind) -> None:
    assert f"this one is {kind}" in _refused(tmp_path, capsys, 2, "<b>x</b>", kind)
    assert not (tmp_path / "o.docx").exists()


def test_an_existing_file_is_left_alone_and_the_path_is_not_printed(tmp_path, capsys) -> None:
    (tmp_path / "o.docx").write_bytes(b"mine")
    err = _refused(tmp_path, capsys, 1)
    assert (tmp_path / "o.docx").read_bytes() == b"mine"
    assert "o.docx" in err and str(tmp_path) not in err


def test_a_sensitive_path_is_refused(tmp_path, capsys) -> None:
    with patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True):
        err = _refused(tmp_path, capsys, 1)
    assert "refusing to write sensitive path" in err and not (tmp_path / "o.docx").exists()


def test_a_failed_save_removes_only_the_file_it_made(tmp_path, capsys) -> None:
    with patch("docx.document.Document.save", side_effect=OSError("disk full")):
        err = _refused(tmp_path, capsys, 1)
    assert "OSError" in err and not (tmp_path / "o.docx").exists()


@pytest.mark.parametrize("block", ["line\n\n", "<div>x</div>\n\n"])
def test_too_large_a_document_writes_nothing(tmp_path, capsys, block) -> None:
    assert "too large" in _refused(tmp_path, capsys, 1, block * 5001)
    assert not (tmp_path / "o.docx").exists()


def test_a_table_over_the_cell_cap_is_refused_before_it_is_built(tmp_path, capsys) -> None:
    table = "|a|b|c|d|e|f|\n|-|-|-|-|-|-|\n" + "|1|2|3|4|5|6|\n" * 2000
    with patch("docx.table.Table.cell") as cell:
        assert "too large" in _refused(tmp_path, capsys, 1, table)
    assert not cell.called and not (tmp_path / "o.docx").exists()


def test_meta_and_docx_are_mutually_exclusive(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["artifact", "show", "s1", "--meta", "--docx", str(tmp_path / "o.docx")])
    assert exc.value.code == 2 and "not allowed with" in capsys.readouterr().err


def test_without_the_flag_the_body_prints_as_before(capsys) -> None:
    _show([], {"content": _MARKDOWN, "kind": "markdown"})
    assert capsys.readouterr().out == _MARKDOWN + "\n"

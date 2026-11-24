"""``artifact show --docx``: one Word paragraph per source line, only headings styled."""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse
from docx import Document  # type: ignore[import-untyped]

from kiro_crew import cli_commands as cc
from kiro_crew.cli_parser_agent import register_artifact_parser
from kiro_crew.config.loader import KiroCrewConfig


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kirocrew")
    register_artifact_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def _show(argv: list[str], payload: dict | None = None):  # artifact show on a fake gateway
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch(
            "kiro_crew.cli_commands._gateway_send",
            side_effect=[_FakeResponse(payload or {})],
        ) as send,
    ):
        cc._artifact(_parse(["artifact", "show", "s1", *argv]))
    return send


def _write(tmp_path: Path, content: str, kind: str = "markdown") -> list[tuple[str, str]]:
    target = tmp_path / "out.docx"
    _show(["--docx", str(target)], {"kind": kind, "content": content})
    return [(p.style.name, p.text) for p in Document(str(target)).paragraphs]


def test_headings_are_styled_and_everything_else_is_the_source_line(tmp_path) -> None:
    got = _write(tmp_path, "# A\n\nbody **x**\n- item\n| a | b |\n## B ##\n")
    assert got == [
        ("Heading 1", "A"),
        ("Normal", ""),
        ("Normal", "body **x**"),
        ("Normal", "- item"),
        ("Normal", "| a | b |"),
        ("Heading 2", "B"),
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("#tag", [("Normal", "#tag")]),
        ("#######  x", [("Normal", "#######  x")]),
        ("    # x", [("Normal", "    # x")]),
        ("#\tx", [("Heading 1", "x")]),
        ("#", [("Heading 1", "")]),
        ("# foo#", [("Heading 1", "foo#")]),
        ("# foo #", [("Heading 1", "foo")]),
        ("Title\n===\nafter", [("Heading 1", "Title"), ("Normal", "after")]),
        ("a\nb\n---\nafter", [("Heading 2", "a b"), ("Normal", "after")]),
        ("> # quoted\n- # item", [("Normal", "> # quoted"), ("Normal", "- # item")]),
    ],
)
def test_heading_rules(tmp_path, source, expected) -> None:
    assert _write(tmp_path, source) == expected


def test_a_fenced_or_indented_block_keeps_its_comments_as_text(tmp_path) -> None:
    source = (
        "```sh\n# in backticks\n```\n~~~~\n# in tildes\n`````\n~~~\n# still in tildes\n"
        "~~~~~\n```x`y\n# real\n    # indented code\n\n~~~\n# unclosed"
    )
    got = _write(tmp_path, source)
    assert [text for style, text in got if style != "Normal"] == ["real"]
    assert [text for _, text in got if text.startswith("#")] == [
        "# in backticks",
        "# in tildes",
        "# still in tildes",
        "# unclosed",
    ]
    assert ("Normal", "    # indented code") in got
    assert len(got) == 15


def test_a_text_artifact_has_no_headings_and_keeps_every_character(tmp_path) -> None:
    got = _write(tmp_path, "# not a heading\r\n  lead and trail  \r\n\ttab\r\n\r\nend", kind="text")
    assert got == [
        ("Normal", "# not a heading"),
        ("Normal", "  lead and trail  "),
        ("Normal", "\ttab"),
        ("Normal", ""),
        ("Normal", "end"),
    ]


def test_control_characters_are_dropped_and_the_file_opens(tmp_path) -> None:
    got = _write(tmp_path, "a\x00b\x1bc\ud800d\ufffee\n# h\x0b1\n")
    assert got == [("Normal", "abcde"), ("Heading 1", "h1")]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_the_file_is_owner_only(tmp_path) -> None:
    _write(tmp_path, "x")
    assert (tmp_path / "out.docx").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["widget", "html", "json", "svg", "image"])
def test_other_kinds_write_nothing(tmp_path, kind, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _show(["--docx", str(tmp_path / "out.docx")], {"kind": kind, "content": "x"})
    assert exc.value.code == 2
    assert kind in capsys.readouterr().err
    assert not (tmp_path / "out.docx").exists()


def test_an_existing_file_is_left_alone_and_the_path_is_not_printed(tmp_path, capsys) -> None:
    target = tmp_path / "out.docx"
    target.write_bytes(b"mine")
    with pytest.raises(SystemExit) as exc:
        _show(["--docx", str(target)], {"kind": "text", "content": "x"})
    assert exc.value.code == 1
    assert target.read_bytes() == b"mine"
    err = capsys.readouterr().err
    assert "out.docx" in err
    assert str(tmp_path) not in err


def test_a_sensitive_path_is_refused_before_anything_is_fetched(tmp_path, capsys) -> None:
    target = tmp_path / "out.docx"
    with (
        patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True) as gate,
        patch("kiro_crew.cli_commands._gateway_send") as send,
        pytest.raises(SystemExit) as exc,
    ):
        cc._artifact(_parse(["artifact", "show", "s1", "--docx", str(target)]))
    assert exc.value.code == 1
    gate.assert_called_once_with(str(target.resolve()))
    send.assert_not_called()
    assert not target.exists()
    assert str(tmp_path) not in capsys.readouterr().err


def test_an_empty_name_is_refused_before_anything_is_fetched(capsys) -> None:
    with patch("kiro_crew.cli_commands._gateway_send") as send, pytest.raises(SystemExit) as exc:
        cc._artifact(_parse(["artifact", "show", "s1", "--docx", ""]))
    assert exc.value.code == 1
    send.assert_not_called()
    assert capsys.readouterr().out == ""


def test_a_flush_that_fails_at_close_removes_only_the_file_it_made(
    tmp_path, monkeypatch, capsys
) -> None:
    @contextmanager
    def close_fails(*args, **kwargs):  # the buffered tail fails to flush, as on a full disk
        with open(*args, **kwargs) as handle:
            yield handle
        raise OSError(28, "No space left on device", str(tmp_path))

    (tmp_path / "keep.docx").write_bytes(b"keep")
    monkeypatch.setattr("kiro_crew.artifact_docx.open", close_fails, raising=False)
    with pytest.raises(SystemExit) as exc:
        _show(["--docx", str(tmp_path / "out.docx")], {"kind": "text", "content": "x"})
    assert exc.value.code == 1
    assert not (tmp_path / "out.docx").exists()
    assert (tmp_path / "keep.docx").read_bytes() == b"keep"
    err = capsys.readouterr().err
    assert "OSError" in err and str(tmp_path) not in err


def test_a_filesystem_that_refuses_the_lockdown_still_gets_the_file(tmp_path) -> None:
    refused = PermissionError(1, "Operation not permitted")  # FAT and exFAT refuse a chmod
    with patch("kiro_crew.artifact_docx.platform_compat.restrict_to_owner", side_effect=refused):
        _show(["--docx", str(tmp_path / "out.docx")], {"kind": "text", "content": "x"})
    assert Document(str(tmp_path / "out.docx")).paragraphs[0].text == "x"


@pytest.mark.parametrize(
    "content", ["a\n" * 20_001, "x" * 1_000_001], ids=["too-many-lines", "too-many-characters"]
)
def test_over_the_line_cap_or_char_cap_writes_nothing(tmp_path, content, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _show(["--docx", str(tmp_path / "out.docx")], {"kind": "markdown", "content": content})
    assert exc.value.code == 1
    assert "too large" in capsys.readouterr().err
    assert not (tmp_path / "out.docx").exists()


def test_the_paragraphs_written_never_exceed_the_source_lines(tmp_path) -> None:
    source = "# a\nb\n---\n\n```\n# c\n```\nd\n"
    got = _write(tmp_path, source)
    assert len(got) <= len(source.splitlines()) == 8
    assert len(got) == 7  # the two-line setext heading is one paragraph


def test_meta_and_docx_are_mutually_exclusive(tmp_path, capsys) -> None:
    assert _parse(["artifact", "show", "s1", "--docx", "o.docx"]).docx == "o.docx"
    with pytest.raises(SystemExit) as exc:
        _parse(["artifact", "show", "s1", "--meta", "--docx", str(tmp_path / "o.docx")])
    assert exc.value.code == 2


def test_without_the_flag_the_body_prints_as_before(capsys) -> None:
    _show([], {"kind": "markdown", "content": "# T\nbody"})
    assert capsys.readouterr().out == "# T\nbody\n"

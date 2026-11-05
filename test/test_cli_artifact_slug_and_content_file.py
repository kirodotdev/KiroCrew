"""``kirocrew artifact`` keeps the slug in one URL segment and reports a bad --content-file.

The slug is spliced into ``/api/artifacts/<slug>``. A slug holding ``/`` would
reach another route (``artifact delete x/publish`` unpublished ``x`` and printed
"Deleted"), so it is refused with exit 2 before any request, as are the empty
slug and the ``.``/``..`` dot segments; every other character is
percent-encoded. A ``--content-file`` that is
missing, a folder or not UTF-8 prints one ``Error:`` line and exits 1.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse

from kiro_crew import cli_commands as cc
from kiro_crew.cli_parser_agent import register_artifact_parser
from kiro_crew.config.loader import KiroCrewConfig


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kirocrew")
    register_artifact_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def _run(args: argparse.Namespace, reply: dict | None = None):
    """Run the command against a fake gateway; return the mocked sender."""
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch(
            "kiro_crew.cli_commands._gateway_send",
            side_effect=[_FakeResponse(reply or {"slug": "s1", "version": 2, "content": "c"})],
        ) as send,
        # A terminal stdin, so a metadata-only update does not read stdin.
        patch("sys.stdin.isatty", return_value=True),
    ):
        cc._artifact(args)
    return send


def _run_refused(args: argparse.Namespace) -> int:
    """Run a command that must exit before any request; return its exit code."""
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch("kiro_crew.cli_commands._gateway_send") as send,
    ):
        with pytest.raises(SystemExit) as exc:
            cc._artifact(args)
    assert not send.called
    return exc.value.code


_SLUG_ACTIONS = [
    ["show"],
    ["show", "--version", "2"],
    ["delete"],
    ["versions"],
    ["update", "--name", "n"],
]


@pytest.mark.parametrize("action", _SLUG_ACTIONS, ids=lambda a: "-".join(a))
@pytest.mark.parametrize("slug", ["x/publish", "", ".", ".."])
def test_a_slug_with_a_slash_or_empty_is_refused_before_any_request(action, slug, capsys) -> None:
    args = _parse(["artifact", action[0], slug, *action[1:]])
    assert _run_refused(args) == 2
    out = capsys.readouterr()
    assert "invalid artifact slug" in out.err
    assert "Deleted" not in out.out


@pytest.mark.parametrize(
    ("action", "suffix"),
    [
        (["show"], "/api/artifacts/a%3Fb%23c%25d"),
        (["show", "--version", "2"], "/api/artifacts/a%3Fb%23c%25d/versions/2"),
        (["delete"], "/api/artifacts/a%3Fb%23c%25d"),
        (["versions"], "/api/artifacts/a%3Fb%23c%25d/versions"),
        (["update", "--name", "n"], "/api/artifacts/a%3Fb%23c%25d"),
    ],
    ids=lambda v: "-".join(v) if isinstance(v, list) else "",
)
def test_the_slug_is_percent_encoded_as_one_segment(action, suffix) -> None:
    args = _parse(["artifact", action[0], "a?b#c%d", *action[1:]])
    send = _run(args, {"versions": [1], "slug": "a?b#c%d", "version": 2, "content": "c"})
    assert send.call_args[0][0].full_url.endswith(suffix)


def test_a_plain_slug_is_sent_unchanged() -> None:
    send = _run(_parse(["artifact", "delete", "my-page"]))
    assert send.call_args[0][0].full_url.endswith("/api/artifacts/my-page")


def _bad_files(tmp_path: Path) -> dict[str, Path]:
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    return {"missing": tmp_path / "nope.html", "folder": tmp_path, "not-utf8": binary}


@pytest.mark.parametrize("kind", ["missing", "folder", "not-utf8"])
@pytest.mark.parametrize(
    "argv",
    [["save", "--name", "n"], ["update", "s1"]],
    ids=["save", "update"],
)
def test_a_bad_content_file_is_one_error_line_and_exit_1(kind, argv, tmp_path, capsys) -> None:
    path = _bad_files(tmp_path)[kind]
    args = _parse(["artifact", *argv, "--content-file", str(path)])
    assert _run_refused(args) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("Error: could not read --content-file ")
    assert "\n" not in err
    assert "Traceback" not in err


def test_a_content_file_name_with_controls_stays_on_one_inert_line(tmp_path, capsys) -> None:
    path = tmp_path / "a\nOK: forged\x1b[2Jb.html"
    args = _parse(["artifact", "save", "--name", "n", "--content-file", str(path)])
    assert _run_refused(args) == 1
    err = capsys.readouterr().err.strip()
    assert "\n" not in err
    assert "\x1b" not in err

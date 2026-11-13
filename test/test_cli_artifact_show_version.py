"""``kirocrew artifact show --version N`` reads version N, and N starts at 1.

Versions start at 1, so the flag refuses anything below 1 at parse time with
exit 2 and a message. The command asks for exactly the version it is given:
``0`` is a version number, not the same as passing no version.
"""

from __future__ import annotations

import argparse
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


def _show(args: argparse.Namespace) -> str:
    """Run ``artifact show`` against a fake gateway and return the URL it asked for."""
    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch(
            "kiro_crew.cli_commands._gateway_send",
            side_effect=[_FakeResponse({"content": "body"})],
        ) as send,
    ):
        cc._artifact(args)
    return send.call_args[0][0].full_url


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_version_below_one_is_refused_at_parse_time(value, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["artifact", "show", "s1", "--version", value])
    assert exc.value.code == 2
    assert "must be 1 or more" in capsys.readouterr().err


def test_a_non_number_version_is_refused_at_parse_time(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["artifact", "show", "s1", "--version", "two"])
    assert exc.value.code == 2
    assert "must be a whole number" in capsys.readouterr().err


def test_a_version_reads_that_version() -> None:
    args = _parse(["artifact", "show", "s1", "--version", "3"])
    assert _show(args).endswith("/api/artifacts/s1/versions/3")


def test_no_version_reads_the_current_one() -> None:
    args = _parse(["artifact", "show", "s1"])
    assert _show(args).endswith("/api/artifacts/s1")


def test_a_version_of_zero_that_skips_the_parser_is_not_read_as_current() -> None:
    """The command asks for what it is given; ``0`` is not the same as no version."""
    args = argparse.Namespace(artifact_action="show", slug="s1", version=0, meta=False)
    assert _show(args).endswith("/api/artifacts/s1/versions/0")

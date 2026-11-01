"""Count flags refuse values that would silently mean something else.

``logs -n`` takes 0 or more (0 is a valid "no backlog", useful with ``-f``).
``pod logs -n``, ``security events -n``, ``memory retired --limit``,
``memory backup --keep`` and ``memory carve --limit`` take 1 or more: the macOS
and Windows pod tails slice ``[-lines:]``, where 0 means the whole file; 0
events prints "no events recorded"; SQLite reads ``LIMIT -1`` as no limit; the
backup pruner and the carve page quietly turn 0 into 1. ``memory carve
--offset`` takes 0 or more, because a negative skip has no meaning. A bad value is refused by argparse: exit 2 and the reason on stderr,
before any command runs. Every count type, ``artifact show --version`` included,
words its refusal the same way, because they share ``cli_arg_types``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kiro_crew import cli_parser_admin, cli_parser_agent, cli_parser_gateway, cli_server
from kiro_crew.service.common import Platform


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kirocrew")
    sub = root.add_subparsers(dest="command")
    cli_parser_gateway.register_logs_parser(sub)
    cli_parser_admin.register_pod_parser(sub)
    cli_parser_admin.register_security_parser(sub)
    cli_parser_agent.register_memory_parser(sub)
    return root


_REFUSED = [
    pytest.param(["logs", "-n", "-5"], "must be 0 or more", id="logs-negative"),
    pytest.param(["pod", "logs", "wt", "-n", "0"], "must be 1 or more", id="pod-zero"),
    pytest.param(["pod", "logs", "wt", "-n", "-1"], "must be 1 or more", id="pod-negative"),
    pytest.param(["security", "events", "-n", "0"], "must be 1 or more", id="events-zero"),
    pytest.param(["security", "events", "-n", "-3"], "must be 1 or more", id="events-negative"),
    pytest.param(["memory", "retired", "--limit", "0"], "must be 1 or more", id="retired-zero"),
    pytest.param(
        ["memory", "retired", "--limit", "-1"], "must be 1 or more", id="retired-negative"
    ),
    pytest.param(["memory", "backup", "--keep", "0"], "must be 1 or more", id="backup-keep-zero"),
    pytest.param(
        ["memory", "backup", "--keep", "-2"], "must be 1 or more", id="backup-keep-negative"
    ),
    pytest.param(["memory", "carve", "--limit", "0"], "must be 1 or more", id="carve-limit-zero"),
    pytest.param(
        ["memory", "carve", "--limit", "-1"], "must be 1 or more", id="carve-limit-negative"
    ),
    pytest.param(
        ["memory", "carve", "--offset", "-1"], "must be 0 or more", id="carve-offset-negative"
    ),
    pytest.param(["logs", "-n", "ten"], "must be a whole number", id="logs-not-a-number"),
]


@pytest.mark.parametrize(("argv", "reason"), _REFUSED)
def test_a_bad_count_exits_2_with_the_reason(
    argv: list[str], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(argv)
    assert exc.value.code == 2
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "attr", "value"),
    [
        pytest.param(["logs", "-n", "0"], "lines", 0, id="logs-zero"),
        pytest.param(["pod", "logs", "wt", "-n", "1"], "lines", 1, id="pod-one"),
        pytest.param(["pod", "logs", "wt"], "lines", 50, id="pod-default"),
        pytest.param(["security", "events", "-n", "1"], "limit", 1, id="events-one"),
        pytest.param(["memory", "retired", "--limit", "1"], "limit", 1, id="retired-one"),
        pytest.param(["logs"], "lines", 100, id="logs-default"),
        pytest.param(["security", "events"], "limit", 20, id="events-default"),
        pytest.param(["memory", "retired"], "limit", 20, id="retired-default"),
        pytest.param(["memory", "backup", "--keep", "1"], "keep", 1, id="backup-keep-one"),
        pytest.param(["memory", "backup"], "keep", None, id="backup-keep-default"),
        pytest.param(["memory", "carve", "--limit", "1"], "limit", 1, id="carve-limit-one"),
        pytest.param(["memory", "carve"], "limit", 50, id="carve-limit-default"),
        pytest.param(["memory", "carve", "--offset", "0"], "offset", 0, id="carve-offset-zero"),
        pytest.param(["memory", "carve"], "offset", 0, id="carve-offset-default"),
    ],
)
def test_an_in_range_count_is_kept(argv: list[str], attr: str, value: int) -> None:
    assert getattr(_parser().parse_args(argv), attr) == value


class _Exec(Exception):
    pass


def test_logs_zero_lines_reaches_tail_as_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-n 0`` reaches the tail as 0, not as the 100-line default."""
    (tmp_path / "gateway.log").write_text("old line\n", encoding="utf-8")
    monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.LAUNCHD)
    monkeypatch.setattr(cli_server.svc_macos, "PLIST_PATH", tmp_path / "absent.plist")
    monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cli_server, "sel", lambda: argparse.Namespace(log_api_access=lambda **kw: None)
    )
    seen: list[list[str]] = []

    def _execvp(_file: str, argv: list[str]) -> None:
        seen.append(argv)
        raise _Exec

    monkeypatch.setattr(cli_server.os, "execvp", _execvp)

    args = _parser().parse_args(["logs", "-n", "0", "-f"])
    with pytest.raises(_Exec):
        cli_server._logs_cmd(args)

    assert seen == [["tail", "-n", "0", "-f", str(tmp_path / "gateway.log")]]


def test_the_parser_modules_share_one_positive_int_type() -> None:
    """A second copy would refuse the same bad count with different wording."""
    from kiro_crew import cli_arg_types

    assert not hasattr(cli_parser_agent, "_positive_int")
    assert cli_parser_agent.positive_int is cli_arg_types.positive_int
    assert cli_parser_admin.positive_int is cli_arg_types.positive_int

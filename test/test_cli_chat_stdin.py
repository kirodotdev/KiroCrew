"""``kirocrew chat -m -`` reads the single message from a piped stdin."""

import argparse
import io
import re
import sys
from pathlib import Path

import pytest

from kiro_crew import cli, cli_chat_stdin
from kiro_crew.cli_chat_stdin import (
    MAX_STDIN_MESSAGE_BYTES,
    StdinMessageError,
    read_stdin_message,
    resolve_chat_message,
)
from kiro_crew.cli_parser_agent import register_chat_parser


class _EndlessStream:
    """A pipe that never reaches end of file, like ``yes |``."""

    def __init__(self) -> None:
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        assert size > 0, "an unbounded read would never return"
        self.read_sizes.append(size)
        return b"y" * size


def test_the_piped_text_is_sent_unchanged() -> None:
    text = 'Review: say "hi" `uname` $HOME\n'
    assert read_stdin_message(io.BytesIO(text.encode()), is_tty=False) == text


def test_a_terminal_stdin_is_refused_before_any_read() -> None:
    stream = _EndlessStream()
    with pytest.raises(StdinMessageError, match="terminal"):
        read_stdin_message(stream, is_tty=True)  # type: ignore[arg-type]
    assert stream.read_sizes == []


@pytest.mark.parametrize("data", [b"", b"  \n\t\n"])
def test_an_empty_stdin_is_refused(data: bytes) -> None:
    with pytest.raises(StdinMessageError, match="empty"):
        read_stdin_message(io.BytesIO(data), is_tty=False)


def test_a_message_at_the_cap_is_accepted() -> None:
    data = b"a" * MAX_STDIN_MESSAGE_BYTES
    assert len(read_stdin_message(io.BytesIO(data), is_tty=False)) == MAX_STDIN_MESSAGE_BYTES


def test_a_message_over_the_cap_is_refused_with_its_size() -> None:
    with pytest.raises(StdinMessageError, match=f"over {MAX_STDIN_MESSAGE_BYTES} bytes"):
        read_stdin_message(io.BytesIO(b"a" * (MAX_STDIN_MESSAGE_BYTES + 1)), is_tty=False)


def test_an_endless_pipe_is_refused_after_reading_one_past_the_cap() -> None:
    stream = _EndlessStream()
    with pytest.raises(StdinMessageError, match="over"):
        read_stdin_message(stream, is_tty=False)  # type: ignore[arg-type]
    assert sum(stream.read_sizes) == MAX_STDIN_MESSAGE_BYTES + 1
    assert max(stream.read_sizes) < MAX_STDIN_MESSAGE_BYTES


def test_non_utf8_bytes_are_refused() -> None:
    with pytest.raises(StdinMessageError, match="not UTF-8"):
        read_stdin_message(io.BytesIO(b"ok \xff\xfe"), is_tty=False)


def test_any_other_message_is_passed_through_without_reading_stdin(monkeypatch) -> None:
    def no_read(*_a, **_k):
        raise AssertionError("stdin must not be read")

    monkeypatch.setattr(cli_chat_stdin, "read_stdin_message", no_read)
    assert resolve_chat_message("hello") == "hello"
    assert resolve_chat_message(None) is None
    assert resolve_chat_message("--") == "--"


def _published_limits(text: str) -> set[str]:
    """Every size limit *text* states, e.g. ``{"1 MiB"}``."""
    return set(re.findall(r"\d+(?:\.\d+)?\s*(?:[KMG]i?B|bytes)", text))


def test_the_help_and_the_spec_state_the_measured_cap() -> None:
    assert MAX_STDIN_MESSAGE_BYTES == 1024 * 1024
    stated = {"1 MiB"}

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_chat_parser(sub, argparse.ArgumentParser(add_help=False))
    (action,) = [a for a in sub.choices["chat"]._actions if a.dest == "message"]
    assert _published_limits(action.help or "") == stated

    spec = (Path(__file__).resolve().parents[1] / "docs/system-specs/modules/cli.md").read_text(
        encoding="utf-8"
    )
    (row,) = [line for line in spec.splitlines() if line.startswith("| `kirocrew chat -m -` |")]
    assert _published_limits(row) == stated


def _run_cli(monkeypatch, stdin_bytes: bytes, argv: list[str]) -> list[str | None]:
    sent: list[str | None] = []
    monkeypatch.setattr(
        cli, "_run_chat", lambda message, model, agent=None, run=None: sent.append(message)
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_bytes)))
    monkeypatch.setattr(sys, "argv", ["kirocrew", "chat", *argv])
    cli.main()
    return sent


def test_cli_chat_sends_the_piped_message(monkeypatch) -> None:
    assert _run_cli(monkeypatch, b"diff --git a b\n", ["-m", "-"]) == ["diff --git a b\n"]


def test_cli_chat_refuses_an_empty_pipe_before_starting_a_backend(monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, b"", ["-m", "-"])
    assert e.value.code == 1
    assert capsys.readouterr().err.startswith("Error: the message on stdin is empty")


def test_headless_refuses_the_stdin_dash_before_any_run(monkeypatch, capsys) -> None:
    from kiro_crew import port_resolution

    def no_run(*_a, **_k):
        raise AssertionError("a headless run must not start")

    monkeypatch.setattr(port_resolution, "resolve_client_port", no_run)
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, b"diff --git a b\n", ["--headless", "-m", "-"])
    assert e.value.code == 2
    assert "--message-file" in capsys.readouterr().err


def test_cli_chat_keeps_a_literal_message(monkeypatch) -> None:
    assert _run_cli(monkeypatch, b"ignored", ["-m", "check my CRs"]) == ["check my CRs"]
